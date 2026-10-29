"""Every live-voice provider ends a dead session the same way (audit
O09-live-voice-providers-15, the P1.7 terminal-disconnect handling).

Only Gemini had it. On Grok and OpenAI a session that gave up reconnecting, or
whose listener died on an unexpected error, left the client WebSocket open
answering pings: Portal and Android kept showing "connected", and the reaper
never evicted the session (it only reaps sessions without a client). And an
upstream close was not forwarded, though setup rejections arrive ONLY as WS
close frames. The shared LiveVoiceBridge now does all three for every provider.
"""
import asyncio

import pytest

from Orchestrator import models
from Orchestrator.tests import voice_golden_harness as harness
from Orchestrator.tests.gemini_live_fakes import FakePortalWS

PROVIDERS = ["gemini", "openai", "grok"]
WS_ATTR = {"gemini": "gemini_ws", "openai": "openai_ws", "grok": "grok_ws"}
CLS = {"gemini": models.GeminiLiveSession, "openai": models.RealtimeSession,
       "grok": models.GrokLiveSession}
LISTENER = {"gemini": "gemini_listener", "openai": "openai_listener", "grok": "grok_listener"}
NAME = {"gemini": "Gemini", "openai": "OpenAI", "grok": "Grok"}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_giving_up_closes_the_client_socket(provider):
    out = asyncio.run(harness.scenario_give_up(provider))
    assert out["client"][-1] == {
        "type": "disconnected", "data": "Connection lost after multiple reconnection attempts"}
    assert out["client_closed"] == [1011, "max reconnects reached"], (
        "a session that gave up must close its client WS, not answer pings forever")
    assert len(out["saves"]) == 1, "the transcript is saved once, at the give-up"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_unexpected_close_is_forwarded_with_its_code(provider):
    out = asyncio.run(harness.scenario_reconnect(provider))
    errors = [f for f in out["client"] if f["type"] == "error"]
    assert errors == [{
        "type": "error",
        "data": f"{NAME[provider]} connection closed (code=1011): internal error",
        "code": 1011,
        "reason": "internal error",
    }]
    kinds = [f["type"] for f in out["client"]]
    assert kinds.index("error") < kinds.index("reconnecting") < kinds.index("reconnected")


class _BrokenUpstream:
    """A provider socket whose read fails with something that is not a close."""

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise RuntimeError("decoder exploded")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_listener_error_tells_the_client_and_closes(provider):
    spec = harness._spec(provider)

    async def run():
        session = CLS[provider](session_id=f"broken-{provider}", operator="system")
        session.portal_ws = FakePortalWS()
        setattr(session, WS_ATTR[provider], _BrokenUpstream())
        await getattr(spec.mod, LISTENER[provider])(session)
        return session

    session = asyncio.run(run())
    assert session.status == "error"
    assert session.portal_ws.frames("error") == [
        {"type": "error", "data": f"{NAME[provider]} listener error: decoder exploded"}]
    assert session.portal_ws.closed == [(1011, f"{provider} listener error")]


class _ClosingUpstream:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise harness._closed_error(1006, "abnormal")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_old_socket_closing_mid_reconnect_is_not_a_disconnect(provider):
    """The reconnect owns the status and the client notices while it runs; a
    'disconnected' from the old socket's listener contradicted its
    'reconnecting' and started the reaper's grace clock mid-recovery."""
    spec = harness._spec(provider)

    async def run():
        session = CLS[provider](session_id=f"midreconnect-{provider}", operator="system")
        session.portal_ws = FakePortalWS()
        session.status = "connected"
        session.is_reconnecting = True
        setattr(session, WS_ATTR[provider], _ClosingUpstream())
        await getattr(spec.mod, LISTENER[provider])(session)
        return session

    session = asyncio.run(run())
    assert session.portal_ws.sent == []
    assert session.status == "connected"
