"""AMI action injection through the SMS gateway client (audit O12-messaging-09).

AMI is a line protocol: CR/LF ends a header field and a blank line ends the
whole action. send_sms dropped `destination` (a model tool argument —
reachable from prompt injection in an inbound SMS or email) and `span`
straight into the `Command:` header, and _send_action joined arbitrary values
into headers, so a value carrying "\\r\\n\\r\\nAction: ..." terminated the
packet and injected a second action on the gateway.
"""
from __future__ import annotations

import asyncio

import pytest

from Orchestrator.sms.ami_client import AMISMSClient, is_valid_sms_destination

_INJECTION = "\r\n\r\nAction: Command\r\nCommand: core restart now\r\n\r\n"


def _client(monkeypatch):
    c = AMISMSClient(host="h", username="u", secret="s")
    c._authenticated = True
    sent = []

    async def fake_send_raw(data):
        sent.append(data)
        # Answer any pending _send_action future so it returns promptly.
        for fut in list(c._pending.values()):
            if not fut.done():
                fut.set_result({"Response": "Success"})

    monkeypatch.setattr(c, "_send_raw", fake_send_raw)
    return c, sent


def _actions(payload: str) -> list[str]:
    """Split a raw AMI write into its actions (blank-line separated)."""
    return [blk for blk in payload.split("\r\n\r\n") if blk]


def _assert_single_action(payload: str, action: str) -> None:
    assert payload.endswith("\r\n\r\n")
    blocks = _actions(payload)
    assert len(blocks) == 1, f"payload carries {len(blocks)} actions: {payload!r}"
    lines = blocks[0].split("\r\n")
    assert lines[0] == f"Action: {action}"
    assert sum(1 for ln in lines if ln.startswith("Action:")) == 1
    assert "\n" not in blocks[0].replace("\r\n", ""), "a bare LF survived in a field"
    assert "\r" not in blocks[0].replace("\r\n", ""), "a bare CR survived in a field"


# --- destination validation -----------------------------------------------------


@pytest.mark.parametrize("dest", [
    "+14105551234", "14105551234", "4105551234", "911", "22000",
    "123456789012345", "+123456789012345",
])
def test_valid_destinations(dest):
    assert is_valid_sms_destination(dest)


@pytest.mark.parametrize("dest", [
    "12", "+12", "+1234567890123456", "1234567890123456", "", "+", "++14105551234",
    "+1 410 555 1234", "410-555-1234", "(410) 5551234", "+14105551234\n",
    "+14105551234\r\nAction: Logoff", "+1410555abcd", "١٢٣٤",
    None, 14105551234, 'x"' + _INJECTION,
])
def test_invalid_destinations(dest):
    assert not is_valid_sms_destination(dest)


def test_send_sms_refuses_an_injecting_destination(monkeypatch):
    c, sent = _client(monkeypatch)
    result = asyncio.run(c.send_sms('+14105551234"' + _INJECTION, "hi"))
    assert result["success"] is False
    assert "destination" in result["error"].lower()
    assert sent == [], "nothing may be written to the gateway"


@pytest.mark.parametrize("dest", ["+1 410 555 1234", "abc", "12"])
def test_send_sms_refuses_malformed_destinations(monkeypatch, dest):
    c, sent = _client(monkeypatch)
    result = asyncio.run(c.send_sms(dest, "hi"))
    assert result["success"] is False and sent == []


@pytest.mark.parametrize("span", ["2" + _INJECTION, "2", True, 0, -1, 2.0, None])
def test_send_sms_refuses_a_non_integer_span(monkeypatch, span):
    c, sent = _client(monkeypatch)
    result = asyncio.run(c.send_sms("+14105551234", "hi", span=span))
    assert result["success"] is False
    assert "span" in result["error"].lower()
    assert sent == []


def test_send_sms_valid_send_is_one_action(monkeypatch):
    c, sent = _client(monkeypatch)
    result = asyncio.run(c.send_sms("+14105551234", "line one\r\n\r\nAction: Logoff", span=3))
    assert result["success"] is True
    (payload,) = sent
    _assert_single_action(payload, "SMSCommand")
    assert 'Command: gsm send sms 3 +14105551234 "' in payload


# --- _send_action / _authenticate strip CR/LF from every value -----------------------


def test_send_action_strips_crlf_from_values(monkeypatch):
    c, sent = _client(monkeypatch)
    asyncio.run(c._send_action("SMSCommand", Command="gsm show span 2" + _INJECTION))
    (payload,) = sent
    _assert_single_action(payload, "SMSCommand")
    assert "Command: gsm show span 2Action: Command" in payload  # flattened, inert


def test_send_action_strips_crlf_from_action_and_keys(monkeypatch):
    c, sent = _client(monkeypatch)
    asyncio.run(c._send_action("Ping\r\n\r\nAction: Logoff", **{"X\r\nAction": "v"}))
    (payload,) = sent
    _assert_single_action(payload, "PingAction: Logoff")


def test_authenticate_strips_crlf_from_credentials(monkeypatch):
    c = AMISMSClient(host="h", username="u" + _INJECTION, secret="s\r\nAction: Logoff")
    sent = []

    async def fake_send_raw(data):
        sent.append(data)

    async def fake_read_message(timeout=10):
        return {"Response": "Success"}

    monkeypatch.setattr(c, "_send_raw", fake_send_raw)
    monkeypatch.setattr(c, "_read_message", fake_read_message)
    assert asyncio.run(c._authenticate()) is True
    (payload,) = sent
    _assert_single_action(payload, "Login")
