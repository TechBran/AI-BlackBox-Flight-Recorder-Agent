"""Webhook -> session attach flow with a fake xAI WS.

Verifies: registry entry keyed phone-xai-<call_id>; preset-driven configure;
listener+keepalive spawned; finalize mirrors the portal-WS finally block
(save via P1b path, disconnected status, last_activity stamped, payload
released); the never-reap-non-disconnected invariant holds throughout."""
import asyncio
import time
from datetime import datetime, timezone

import pytest

import Orchestrator.routes.grok_live_routes as glr
from Orchestrator.live_session_reaper import is_reapable
from Orchestrator.models import GROK_LIVE_SESSIONS
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS  # P1b shared double
from Orchestrator.xai_phone import call_bridge


@pytest.fixture(autouse=True)
def clean_registry():
    GROK_LIVE_SESSIONS.clear()
    yield
    GROK_LIVE_SESSIONS.clear()


@pytest.fixture
def wired(monkeypatch):
    """Wire fakes for every grok_live_routes function call_bridge late-imports."""
    state = {"connects": [], "configured": {}, "saves": [], "hangup": asyncio.Event()}

    async def fake_connect(session, model=None, conversation_id=None, call_id=None):
        state["connects"].append(call_id)
        session.grok_ws = FakeUpstreamWS()
        session.status = "connected"
        if call_id:
            session.call_id = call_id
        return True

    async def fake_configure(session, operator, voice="Ara", custom_role=""):
        state["configured"].update(operator=operator, voice=voice, custom_role=custom_role)

    async def fake_listener(session):
        await state["hangup"].wait()          # "xAI closed the WS" when set

    async def fake_keepalive(session):
        await asyncio.sleep(3600)

    async def fake_save(session):
        state["saves"].append(session.session_id)

    monkeypatch.setattr(glr, "connect_to_grok", fake_connect)
    monkeypatch.setattr(glr, "configure_grok_session", fake_configure)
    monkeypatch.setattr(glr, "grok_listener", fake_listener)
    monkeypatch.setattr(glr, "grok_keepalive_loop", fake_keepalive)
    monkeypatch.setattr(glr, "save_grok_session_to_blackbox", fake_save)
    monkeypatch.setattr(call_bridge, "_resolve_default_preset",
                        lambda: {"voice": "Eve", "instructions": "Front-desk agent."})
    return state


@pytest.mark.asyncio
async def test_attach_creates_connected_session_with_preset(wired):
    sid = await call_bridge.attach_call("call-42")
    assert sid == "phone-xai-call-42"
    session = GROK_LIVE_SESSIONS[sid]
    assert session.status == "connected"
    assert session.call_id == "call-42"
    assert session.operator == "system"
    assert wired["connects"] == ["call-42"]
    assert wired["configured"] == {"operator": "system", "voice": "Eve",
                                   "custom_role": "Front-desk agent."}
    # never-reap-non-disconnected invariant: a live call (no portal_ws!) is safe
    assert not is_reapable(session, time.time() + 10_000)
    wired["hangup"].set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_finalize_on_ws_close_saves_and_becomes_reapable(wired):
    sid = await call_bridge.attach_call("call-7")
    session = GROK_LIVE_SESSIONS[sid]
    wired["hangup"].set()                     # call ends
    await asyncio.sleep(0.05)
    assert session.status == "disconnected"
    assert session.intentional_disconnect is True   # no reconnect churn on a dead call
    assert session.grok_ws is None
    assert wired["saves"] == [sid]                  # transcript persisted (P1b /chat/save path)
    now = datetime.now(timezone.utc).timestamp()
    assert not is_reapable(session, now)            # grace window holds
    assert is_reapable(session, now + 121)          # evicted after grace


@pytest.mark.asyncio
async def test_duplicate_webhook_does_not_double_attach(wired):
    sid1 = await call_bridge.attach_call("call-9")
    sid2 = await call_bridge.attach_call("call-9")
    assert sid1 == sid2
    assert wired["connects"] == ["call-9"]          # connected exactly once
    wired["hangup"].set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_connect_failure_leaves_reapable_session(wired, monkeypatch):
    async def failing_connect(session, model=None, conversation_id=None, call_id=None):
        return False
    monkeypatch.setattr(glr, "connect_to_grok", failing_connect)
    sid = await call_bridge.attach_call("call-dead")
    assert sid is None
    session = GROK_LIVE_SESSIONS["phone-xai-call-dead"]
    assert session.status == "disconnected"
    now = datetime.now(timezone.utc).timestamp()
    assert is_reapable(session, now + 121)


def test_no_default_preset_resolves_empty(monkeypatch):
    from Orchestrator.xai_phone import provisioning as pv
    monkeypatch.setattr(pv, "get_default_preset_id", lambda: None)
    assert call_bridge._resolve_default_preset() == {}
