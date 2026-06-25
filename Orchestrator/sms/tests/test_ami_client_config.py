import asyncio
import inspect
import urllib.parse

from Orchestrator.sms.ami_client import (
    AMISMSClient,
    _encode_sms_body,
    _strip_unrenderable,
)


def test_no_hardcoded_secret_in_source():
    src = inspect.getsource(AMISMSClient.__init__)
    assert "6157Ego8" not in src, "hardcoded AMI secret must be removed"
    assert "192.168.1.200" not in src, "hardcoded default host must be removed"


def test_defaults_are_empty():
    c = AMISMSClient()
    assert c.host == ""
    assert c.secret == ""
    assert c.username == ""
    assert c.port == 5038


def test_explicit_creds_stored():
    c = AMISMSClient(host="10.0.0.9", username="u", secret="s", port=5038)
    assert c.host == "10.0.0.9" and c.username == "u" and c.secret == "s"


def test_encode_sms_body_neutralizes_framing_breakers():
    """Newlines and quotes must not survive raw — they would truncate/corrupt
    the line-oriented AMI Command field. They round-trip back on decode."""
    body = 'DAILY AI BRIEFING (1/3)\r\n\n- item one\n- item "two" 50% off'
    enc = _encode_sms_body(body)
    assert "\n" not in enc and "\r" not in enc, "raw newline would truncate the AMI field"
    assert '"' not in enc, "raw quote would break the quoted CLI arg"
    # the gateway (and the symmetric inbound parser) decodes it back to the original
    assert urllib.parse.unquote(enc) == body


def test_strip_unrenderable_drops_astral_keeps_bmp():
    """Supplementary-plane emoji (garbled on the TG200) are removed; BMP symbols
    (which render correctly) and the orphaned space are kept/tidied."""
    assert _strip_unrenderable("\U0001F4CC ACTION REQUIRED") == " ACTION REQUIRED"  # 📌 dropped
    # ⚖ (U+2696) + variation selector and the em-dash are BMP — they stay.
    kept = "⚖️ GEOPOLITICS — live"
    assert _strip_unrenderable(kept) == kept
    # a doubled space left by a dropped emoji collapses to one
    assert _strip_unrenderable("A \U0001F680 B") == "A B"


def test_send_sms_command_is_single_line(monkeypatch):
    """A multi-line body must be embedded as ONE line in the AMI action, so the
    Command field is never truncated (the header-only SMS bug)."""
    c = AMISMSClient(host="h", username="u", secret="s")
    c._authenticated = True
    sent = {}

    async def fake_send_raw(data):
        sent["data"] = data

    monkeypatch.setattr(c, "_send_raw", fake_send_raw)
    asyncio.run(c.send_sms("+14105551234", "Header line\n\nBody one\nBody two"))

    payload = sent["data"]
    assert payload.endswith("\r\n\r\n"), "action must end with the blank-line terminator"
    head = payload[:-4]               # strip the action terminator
    assert head.count("\n") == 1, (   # only the single \r\n between Action: and Command:
        f"Command field is not single-line — a body newline leaked: {head!r}"
    )
    assert "%0A" in payload, "newlines should be percent-encoded, not dropped"
    assert "Header line" in payload and "Body two" in payload, "body content was lost"
