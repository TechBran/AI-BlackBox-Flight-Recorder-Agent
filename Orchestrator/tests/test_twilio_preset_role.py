"""POST /twilio/call resolves role='preset:<id>' server-side (P4).

Calls the route function directly (twilio_routes registers on the shared app
via decorators — no router to mount). Unknown preset must fail LOUDLY before
any Twilio interaction; resolution must run before the backend_map.

Hermetic (audit O21-python-tests-01): these tests used to run the handler end
to end with whatever PHONE_ENABLED / Twilio credentials the box's .env held, so
on a configured box every run dialled the fixture number for real. The phone
config is now pinned to fake values and requests.post is a recorder, so the
handler's whole path runs and no call can ever leave the process.
"""
import asyncio

import pytest

from Orchestrator.voice_agents import registry as va
from Orchestrator.routes import twilio_routes as tw


class _FakeTwilioResponse:
    status_code = 201
    text = ""

    def json(self):
        return {"sid": "CA-test"}


@pytest.fixture
def dialled(monkeypatch):
    """Configured-looking phone settings, outbound HTTP recorded, never sent."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _FakeTwilioResponse()

    monkeypatch.setattr(tw, "PHONE_ENABLED", True)
    monkeypatch.setattr(tw, "TWILIO_ACCOUNT_SID", "AC-test-sid")
    monkeypatch.setattr(tw, "TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(tw, "TWILIO_PHONE_NUMBER", "+15550000000")
    monkeypatch.setattr(tw, "TWILIO_WEBHOOK_BASE_URL", "https://box.test")
    monkeypatch.setattr(tw.requests, "post", fake_post)
    before = set(tw.PHONE_SESSIONS)
    yield calls
    for session_id in set(tw.PHONE_SESSIONS) - before:
        tw.PHONE_SESSIONS.pop(session_id, None)


@pytest.fixture
def reg(tmp_path, monkeypatch, dialled):
    monkeypatch.setattr(va, "REGISTRY_PATH", str(tmp_path / "voice_agents.json"))
    return dialled


def _call(role, greeting=""):
    req = tw.OutboundCallRequest(to="+15551234567", role=role, greeting=greeting)
    return asyncio.run(tw.initiate_outbound_call(req)), req


def test_unknown_preset_errors_before_anything_else(reg):
    result, _ = _call("preset:va-nope")
    assert "error" in result
    assert "preset" in result["error"].lower()
    assert reg == []  # never reached Twilio


def test_preset_substitutes_role_backend_greeting(reg):
    p = va.add_preset(name="Pizza", provider="grok-live",
                      instructions="You order pizzas.", greeting="Hello!")
    result, req = _call(f"preset:{p['id']}")
    # Resolution mutates the request before the Twilio-cred checks.
    assert req.role == "You order pizzas."
    assert req.backend == "grok_live"          # preset provider drives backend
    assert req.greeting == "Hello!"
    # The resolved preset is what the (recorded) dial carries.
    assert result["status"] == "initiated" and result["backend"] == "grok_live"
    session = tw.PHONE_SESSIONS[result["session_id"]]
    assert session.outbound_role == "You order pizzas."
    assert session.outbound_greeting == "Hello!"
    assert len(reg) == 1 and reg[0]["auth"] == ("AC-test-sid", "test-token")


def test_plain_role_untouched(reg):
    _, req = _call("Be a pirate", greeting="hi")
    assert req.role == "Be a pirate" and req.greeting == "hi"


def test_phone_disabled_never_dials(reg, monkeypatch):
    monkeypatch.setattr(tw, "PHONE_ENABLED", False)
    result, _ = _call("Be a pirate")
    assert result == {"error": "Phone service is not enabled"}
    assert reg == []
