"""Tests for real SIM/GSM status pulled over AMI.

These exercise the deterministic PARSERS without any sockets:
  - AMISMSClient.get_all_spans() — parses `gsm show spans` + `gsm show span N`
  - gateway_manager.check_gateway_status() — maps spans into sim_slots,
    pulling phone_number from the configured gateway ports[] (never AMI).

The canned _body strings below are the EXACT output captured from the live
TG200 (NeoGate TG, Boa server, no REST).
"""
import pytest

from Orchestrator.sms.ami_client import AMISMSClient
from Orchestrator.asterisk import gateway_manager as gm


# --- Real captured TG200 output ------------------------------------------

_SPANS_BODY = (
    "GSM span 2: Power on, Up, Active, Standard\n"
    "GSM span 3: Power on, Down, Active, Standard"
)

_SPAN2_DETAIL = (
    "D-channel: 2\n"
    "Status: Power on, Up, Active, Standard\n"
    "Type: CPE\n"
    "Manufacturer: Quectel\n"
    "Model Name: EC21\n"
    "Model IMEI: 864395065175647\n"
    "Revision: EC21AFAR05A06M4G_30.002.30.002\n"
    "Network Name: AT&T\n"
    "Network Status: Registered (Home network)\n"
    "Signal Quality (0,31): 19\n"
    "BER value (0,7): -1\n"
    "SIM IMSI: 310410475394916\n"
    "SIM SMS Center Number: +13123149810\n"
    "PDD: 0\n"
    "ASR: 0\n"
    "ACD: 0\n"
    "Last event: D-Channel Up\n"
    "State: READY"
)

# Span 3 is Down — minimal/empty detail (the gateway returns little when down)
_SPAN3_DETAIL = (
    "D-channel: 3\n"
    "Status: Power on, Down, Active, Standard"
)


def _fake_client():
    """An AMISMSClient that is 'authenticated' and answers SMSCommands from
    the canned bodies above, without ever touching a socket."""
    c = AMISMSClient()
    c._authenticated = True

    async def fake_send_action(action, **params):
        cmd = params.get("Command", "")
        if cmd == "gsm show spans":
            return {"_body": _SPANS_BODY}
        if cmd == "gsm show span 2":
            return {"_body": _SPAN2_DETAIL}
        if cmd == "gsm show span 3":
            return {"_body": _SPAN3_DETAIL}
        return {"_body": ""}

    c._send_action = fake_send_action
    return c


# --- get_all_spans parser tests ------------------------------------------

@pytest.mark.asyncio
async def test_get_all_spans_parses_live_output():
    c = _fake_client()
    spans = await c.get_all_spans()

    by_span = {s["span"]: s for s in spans}
    assert set(by_span) == {2, 3}

    s2 = by_span[2]
    assert s2["up"] is True
    assert s2["carrier"] == "AT&T"
    assert s2["registered"] is True
    assert s2["signal_raw"] == 19
    assert s2["signal"] == round(19 / 31 * 100) == 61
    assert s2["state"] == "READY"

    s3 = by_span[3]
    assert s3["up"] is False


@pytest.mark.asyncio
async def test_get_all_spans_empty_when_not_authenticated():
    c = AMISMSClient()
    assert c._authenticated is False
    assert await c.get_all_spans() == []


@pytest.mark.asyncio
async def test_get_all_spans_unknown_signal_is_none():
    """CSQ of 99 (or absent) means unknown -> signal/signal_raw None."""
    c = AMISMSClient()
    c._authenticated = True

    async def fake_send_action(action, **params):
        cmd = params.get("Command", "")
        if cmd == "gsm show spans":
            return {"_body": "GSM span 2: Power on, Up, Active, Standard"}
        if cmd == "gsm show span 2":
            return {
                "_body": (
                    "Network Name: AT&T\n"
                    "Network Status: Registered (Home network)\n"
                    "Signal Quality (0,31): 99\n"
                    "State: READY"
                )
            }
        return {"_body": ""}

    c._send_action = fake_send_action
    spans = await c.get_all_spans()
    assert spans[0]["signal"] is None
    assert spans[0]["signal_raw"] is None


@pytest.mark.asyncio
async def test_get_all_spans_never_raises_on_error():
    c = AMISMSClient()
    c._authenticated = True

    async def boom(action, **params):
        raise ConnectionError("socket gone")

    c._send_action = boom
    # Must not raise — returns what it has (nothing).
    assert await c.get_all_spans() == []


# --- check_gateway_status SIM mapping ------------------------------------

@pytest.mark.asyncio
async def test_check_gateway_status_maps_sims_from_ami(monkeypatch):
    class FakeAMI:
        connected = True

        async def get_all_spans(self):
            return [
                {
                    "span": 2, "up": True, "carrier": "AT&T",
                    "registered": True, "signal": 61, "signal_raw": 19,
                    "state": "READY",
                },
                {
                    "span": 3, "up": False, "carrier": "",
                    "registered": False, "signal": None, "signal_raw": None,
                    "state": "",
                },
            ]

    # Patch get_ami_client where check_gateway_status imports it from.
    import Orchestrator.sms as sms_mod
    monkeypatch.setattr(sms_mod, "get_ami_client", lambda gid: FakeAMI())

    gateway = {
        "id": "abc123",
        "name": "Test TG200",
        "ip": "10.0.0.50",
        "trunk_name": "tg200",
        "http_port": 80,
        "ports": [
            {"span": 2, "slot": 0, "phone_number": "+14103497272"},
            {"span": 3, "slot": 1, "phone_number": ""},
        ],
    }

    status = await gm.check_gateway_status(gateway)
    slots = {s["span"]: s for s in status["sim_slots"]}
    assert set(slots) == {2, 3}

    s2 = slots[2]
    assert s2["slot"] == 0
    assert s2["status"] == "up"
    assert s2["carrier"] == "AT&T"
    assert s2["signal"] == 61
    assert s2["registered"] is True
    # phone_number comes from the gateway port, NOT from AMI.
    assert s2["phone_number"] == "+14103497272"

    s3 = slots[3]
    assert s3["status"] == "down"
    assert s3["phone_number"] == ""


@pytest.mark.asyncio
async def test_check_gateway_status_no_rest_call(monkeypatch):
    """The fictional /api/v1.0/gsm REST path must be gone from the source."""
    import inspect
    src = inspect.getsource(gm.check_gateway_status)
    assert "/api/v1.0/gsm" not in src
