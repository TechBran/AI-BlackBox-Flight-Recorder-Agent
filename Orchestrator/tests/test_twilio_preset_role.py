"""POST /twilio/call resolves role='preset:<id>' server-side (P4).

Calls the route function directly (twilio_routes registers on the shared app
via decorators — no router to mount). Unknown preset must fail LOUDLY before
any Twilio interaction; resolution must run before the backend_map.
"""
import asyncio
import pytest

from Orchestrator.voice_agents import registry as va
from Orchestrator.routes import twilio_routes as tw


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "REGISTRY_PATH", str(tmp_path / "voice_agents.json"))


def _call(role, greeting=""):
    req = tw.OutboundCallRequest(to="+15551234567", role=role, greeting=greeting)
    return asyncio.run(tw.initiate_outbound_call(req)), req


def test_unknown_preset_errors_before_anything_else(reg):
    result, _ = _call("preset:va-nope")
    assert "error" in result
    assert "preset" in result["error"].lower()


def test_preset_substitutes_role_backend_greeting(reg):
    p = va.add_preset(name="Pizza", provider="grok-live",
                      instructions="You order pizzas.", greeting="Hello!")
    result, req = _call(f"preset:{p['id']}")
    # Resolution mutates the request before the Twilio-cred checks (which
    # error out on this box-less test env — that's fine, we assert the merge).
    assert req.role == "You order pizzas."
    assert req.backend == "grok_live"          # preset provider drives backend
    assert req.greeting == "Hello!"


def test_plain_role_untouched(reg):
    _, req = _call("Be a pirate", greeting="hi")
    assert req.role == "Be a pirate" and req.greeting == "hi"
