"""Live-voice state machine (audit rank 50: O09-live-voice-providers-02, -07,
-01 and O10-telephony-twilio-08).

Pins, for the three /ws/<provider> bridges and the phone bridge that clones them:

  * A quiet user is not a dead socket. The old keepalive loops declared the
    upstream STALE after 60 s without an inbound frame and forced a reconnect;
    Gemini, OpenAI and Grok all send nothing while the user is silent, so the
    journal shows two forced reconnects in one 3-minute session. Liveness is the
    transport's job (websockets ping/pong + ConnectionClosed): three silent
    minutes on a simulated clock must produce ZERO reconnects.
  * Gemini's generationComplete and turnComplete are separate frames. Handling
    both as "turn over" emitted response_complete twice and split one reply into
    two assistant entries. One reply = one assistant entry + one response_complete.
  * A client re-attaching to a session id inherits the previous handler's
    teardown flags. The connect branch must reset them, and an older handler's
    finally must not tear down the upstream a newer attach owns.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import websockets
from fastapi.testclient import TestClient
from websockets.frames import Close

import Orchestrator.routes.gemini_live_routes as glr
import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
from Orchestrator.checkpoint import app
from Orchestrator.models import (
    GEMINI_LIVE_SESSIONS,
    GROK_LIVE_SESSIONS,
    REALTIME_SESSIONS,
    GeminiLiveSession,
    GrokLiveSession,
    RealtimeSession,
)
from Orchestrator.tests.gemini_live_fakes import FakeGeminiWS, make_session
from Orchestrator.tests.voice_ws_fakes import FakePortalWS, FakeUpstreamWS

_REAL_SLEEP = asyncio.sleep
SILENT_SECONDS = 180


def _closed(code=1011, reason="keepalive ping timeout"):
    return websockets.exceptions.ConnectionClosedError(Close(code, reason), None)


class _Clock:
    """Simulated wall clock: every asyncio.sleep(d) advances it by d instantly."""

    def __init__(self, monkeypatch, stop_after):
        self.t = 1_000_000.0
        self.slept = 0.0
        self.stop_after = stop_after
        self.on_stop = None
        monkeypatch.setattr(time, "time", lambda: self.t)
        monkeypatch.setattr(asyncio, "sleep", self.sleep)

    async def sleep(self, delay, result=None):
        self.t += delay
        self.slept += delay
        if self.slept >= self.stop_after and self.on_stop is not None:
            self.on_stop()
        await _REAL_SLEEP(0)
        return result


# ---------------------------------------------------------------------------
# Three silent minutes -> zero reconnects (O09-live-voice-providers-02)
# ---------------------------------------------------------------------------
ROUTES = [
    pytest.param(glr, GeminiLiveSession, "gemini_ws", "gemini_keepalive_loop",
                 "gemini_reconnect", id="gemini"),
    pytest.param(gk, GrokLiveSession, "grok_ws", "grok_keepalive_loop",
                 "grok_reconnect", id="grok"),
    pytest.param(rt, RealtimeSession, "openai_ws", "openai_keepalive_loop",
                 "openai_reconnect", id="openai"),
]


@pytest.mark.parametrize("module,cls,ws_attr,loop_name,reconnect_name", ROUTES)
def test_three_silent_minutes_cause_no_reconnect(monkeypatch, module, cls, ws_attr,
                                                 loop_name, reconnect_name):
    reconnects = []

    async def fake_reconnect(session):
        reconnects.append(session)

    monkeypatch.setattr(module, reconnect_name, fake_reconnect)

    async def run():
        session = cls(session_id=f"silent-{ws_attr}", operator="system")
        upstream = FakeUpstreamWS()
        setattr(session, ws_attr, upstream)
        session.portal_ws = FakePortalWS()
        clock = _Clock(monkeypatch, SILENT_SECONDS)
        clock.on_stop = lambda: setattr(session, "intentional_disconnect", True)
        await asyncio.wait_for(getattr(module, loop_name)(session), timeout=5)
        await _REAL_SLEEP(0)  # let any spawned reconnect task run
        return upstream

    upstream = asyncio.run(run())
    assert reconnects == [], (
        f"{loop_name} forced {len(reconnects)} reconnect(s) during "
        f"{SILENT_SECONDS}s of ordinary silence")
    # The silence keepalive itself kept flowing the whole time.
    assert len(upstream.sent) >= SILENT_SECONDS // 15 - 1


def test_phone_bridge_three_silent_minutes_cause_no_reconnect(monkeypatch):
    """The phone bridge cloned the same 60 s timer (O10-telephony-twilio-08):
    a caller who listens or is on hold for a minute got a reconnect that
    rebuilt the session without the call's context."""
    from Orchestrator.phone.bridge import PhoneAIBridge
    from Orchestrator.phone.session import AIBackend, PhoneSession

    for backend in (AIBackend.OPENAI_REALTIME, AIBackend.GROK_LIVE, AIBackend.GEMINI_LIVE):
        phone = PhoneSession(session_id="silent-call", ai_backend=backend)
        bridge = PhoneAIBridge(phone)
        reconnects = []

        async def fake_reconnect():
            reconnects.append(backend)
            return True

        bridge._reconnect_openai = fake_reconnect
        bridge._reconnect_grok = fake_reconnect
        bridge._openai_reconnecting = False
        bridge._grok_reconnecting = False
        bridge._send_openai_keepalive = AsyncMock()
        bridge._send_grok_keepalive = AsyncMock()
        bridge._send_gemini_keepalive = AsyncMock()

        async def run():
            bridge._running = True
            clock = _Clock(monkeypatch, SILENT_SECONDS)
            clock.on_stop = lambda: setattr(bridge, "_running", False)
            await asyncio.wait_for(bridge._keepalive_loop(), timeout=5)
            await _REAL_SLEEP(0)

        asyncio.run(run())
        assert reconnects == [], f"{backend.value}: silence forced a reconnect"


# ---------------------------------------------------------------------------
# generationComplete != turnComplete (O09-live-voice-providers-07)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_reply_is_one_assistant_entry_and_one_response_complete():
    session = make_session()

    await glr.handle_gemini_message(
        session, {"serverContent": {"inputTranscription": {"text": "what time is it"}}})
    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "It is "}}})
    session.is_speaking = True
    # The model is done GENERATING; playback (and trailing transcription) goes on.
    await glr.handle_gemini_message(session, {"serverContent": {"generationComplete": True}})
    assert session.is_speaking is False
    assert session.portal_ws.frames("response_complete") == []
    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "noon."}}})
    await glr.handle_gemini_message(session, {"serverContent": {"turnComplete": True}})

    assistant = [m["content"] for m in session.conversation if m["role"] == "assistant"]
    users = [m["content"] for m in session.conversation if m["role"] == "user"]
    assert assistant == ["It is noon."]
    assert users == ["what time is it"]
    assert [m["role"] for m in session.conversation] == ["user", "assistant"]
    done = session.portal_ws.frames("response_complete")
    assert len(done) == 1
    assert done[0]["data"]["transcript"] == "It is noon."


@pytest.mark.asyncio
async def test_interrupted_turn_still_flushes_once_on_turn_complete():
    session = make_session()
    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "Let me ex"}}})
    await glr.handle_gemini_message(session, {"serverContent": {"interrupted": True}})
    await glr.handle_gemini_message(session, {"serverContent": {"turnComplete": True}})

    assert [m["content"] for m in session.conversation] == ["Let me ex"]
    assert len(session.portal_ws.frames("response_complete")) == 1


# ---------------------------------------------------------------------------
# Re-attach: connect resets the teardown flags (O09-live-voice-providers-01)
# ---------------------------------------------------------------------------
ENDPOINTS = [
    pytest.param("gemini", id="gemini"),
    pytest.param("grok", id="grok"),
    pytest.param("openai", id="openai"),
]


def _endpoint_spec(name):
    if name == "gemini":
        return SimpleNamespace(module=glr, cls=GeminiLiveSession, registry=GEMINI_LIVE_SESSIONS,
                               path="/ws/gemini-live/", ws_attr="gemini_ws", key="GOOGLE_API_KEY",
                               connect="connect_to_gemini", configure="configure_gemini_session",
                               listener="gemini_listener", keepalive="gemini_keepalive_loop",
                               reconnect="gemini_reconnect", save="save_session_to_blackbox")
    if name == "grok":
        return SimpleNamespace(module=gk, cls=GrokLiveSession, registry=GROK_LIVE_SESSIONS,
                               path="/ws/grok-live/", ws_attr="grok_ws", key="XAI_API_KEY",
                               connect="connect_to_grok", configure="configure_grok_session",
                               listener="grok_listener", keepalive="grok_keepalive_loop",
                               reconnect="grok_reconnect", save="save_grok_session_to_blackbox")
    return SimpleNamespace(module=rt, cls=RealtimeSession, registry=REALTIME_SESSIONS,
                           path="/ws/realtime/", ws_attr="openai_ws", key="OPENAI_API_KEY",
                           connect="connect_to_openai", configure="configure_openai_session",
                           listener="openai_listener", keepalive="openai_keepalive_loop",
                           reconnect="openai_reconnect", save="save_session_to_blackbox")


@pytest.mark.parametrize("name", ENDPOINTS)
def test_reattach_connect_resets_teardown_state(monkeypatch, name):
    """The previous handler's finally left intentional_disconnect=True and a
    used-up reconnect budget. A client re-attaching to the same session id
    (all three clients do, within the reaper grace) must get a session whose
    keepalive survives its first tick and whose upstream close reconnects."""
    spec = _endpoint_spec(name)
    sid = f"reattach-{name}"
    stale = spec.cls(session_id=sid, operator="system")
    stale.intentional_disconnect = True
    stale.is_reconnecting = True
    stale.reconnect_count = stale.max_reconnects
    stale.status = "disconnected"
    spec.registry[sid] = stale

    monkeypatch.setattr(spec.module, spec.key, "test-key")
    monkeypatch.setattr(spec.module, "WEBSOCKETS_AVAILABLE", True)

    async def fake_connect(session, *a, **k):
        setattr(session, spec.ws_attr, FakeGeminiWS(closing_exc=_closed()))
        session.status = "connected"
        return True

    monkeypatch.setattr(spec.module, spec.connect, fake_connect)
    monkeypatch.setattr(spec.module, spec.configure, AsyncMock())
    monkeypatch.setattr(spec.module, spec.save, AsyncMock())

    seen = {}
    reconnects = []

    async def fake_keepalive(session):
        # The real loop breaks on its first tick when either is falsy/true.
        seen["keepalive_would_break"] = (
            not getattr(session, spec.ws_attr) or session.intentional_disconnect)
        await asyncio.sleep(3600)

    async def fake_reconnect(session):
        reconnects.append(session.session_id)

    real_listener = getattr(spec.module, spec.listener)
    monkeypatch.setattr(spec.module, spec.keepalive, fake_keepalive)
    monkeypatch.setattr(spec.module, spec.reconnect, fake_reconnect)
    monkeypatch.setattr(spec.module, spec.listener, real_listener)

    try:
        client = TestClient(app)
        with client.websocket_connect(spec.path + sid) as ws:
            ws.send_text(json.dumps({"type": "connect", "operator": "system"}))
            assert ws.receive_json()["type"] == "status"
            while ws.receive_json()["type"] != "connected":
                pass  # the listener's close notice may arrive first
            seen["reconnect_count"] = stale.reconnect_count
            ws.send_text(json.dumps({"type": "ping"}))
            while ws.receive_json()["type"] != "pong":
                pass
            ws.send_text(json.dumps({"type": "disconnect"}))
    finally:
        spec.registry.pop(sid, None)

    assert seen.get("keepalive_would_break") is False, (
        "re-attached session kept intentional_disconnect=True: its keepalive dies on tick one")
    assert seen["reconnect_count"] == 0, "reconnect budget carried over from the old connection"
    assert reconnects == [sid], (
        "an upstream close on the re-attached session must trigger a reconnect")


# ---------------------------------------------------------------------------
# Generation token: an old handler's teardown leaves a newer attach alone
# ---------------------------------------------------------------------------
TEARDOWNS = [
    pytest.param(glr, GeminiLiveSession, "gemini_ws", "save_session_to_blackbox", id="gemini"),
    pytest.param(gk, GrokLiveSession, "grok_ws", "save_grok_session_to_blackbox", id="grok"),
    pytest.param(rt, RealtimeSession, "openai_ws", "save_session_to_blackbox", id="openai"),
]


async def _forever():
    await asyncio.sleep(3600)


@pytest.mark.parametrize("module,cls,ws_attr,save_name", TEARDOWNS)
def test_superseded_handler_teardown_leaves_new_upstream(monkeypatch, module, cls,
                                                         ws_attr, save_name):
    from Orchestrator.routes.voice_ws_shared import claim_session

    save = AsyncMock()
    monkeypatch.setattr(module, save_name, save)

    async def run():
        session = cls(session_id=f"gen-{ws_attr}", operator="system")
        old_portal, new_portal = FakePortalWS(), FakePortalWS()

        session.portal_ws = old_portal
        old_gen = claim_session(session)
        old_keepalive = asyncio.create_task(_forever())

        # The client re-attached (network blip) before the old handler noticed:
        session.portal_ws = new_portal
        claim_session(session)
        new_upstream = FakeUpstreamWS()
        setattr(session, ws_attr, new_upstream)
        session.listener_task = asyncio.create_task(_forever())
        session.keepalive_task = asyncio.create_task(_forever())
        session.status = "connected"
        session.conversation = [{"role": "user", "content": "still talking",
                                 "timestamp": "2026-10-23T00:00:00Z"}]

        await module._end_portal_session(session, old_portal, old_gen, old_keepalive)

        assert getattr(session, ws_attr) is new_upstream
        assert new_upstream.closed is False
        assert not session.listener_task.done()
        assert not session.keepalive_task.done()
        assert session.portal_ws is new_portal
        assert session.status == "connected"
        assert session.intentional_disconnect is False
        assert len(session.conversation) == 1, "the newer owner saves the conversation"
        assert old_keepalive.cancelled() or old_keepalive.done()
        save.assert_not_awaited()

        # The CURRENT owner's teardown does tear everything down.
        await module._end_portal_session(session, new_portal, session.generation, None)
        assert getattr(session, ws_attr) is None
        assert new_upstream.closed is True
        assert session.listener_task is None
        assert session.status == "disconnected"
        assert session.portal_ws is None
        save.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("module,cls,ws_attr,reconnect_name,connect_name", [
    pytest.param(glr, GeminiLiveSession, "gemini_ws", "gemini_reconnect", "connect_to_gemini",
                 id="gemini"),
    pytest.param(gk, GrokLiveSession, "grok_ws", "grok_reconnect", "connect_to_grok",
                 id="grok"),
    pytest.param(rt, RealtimeSession, "openai_ws", "openai_reconnect", "connect_to_openai",
                 id="openai"),
])
def test_detached_reconnect_from_old_generation_bails(monkeypatch, module, cls, ws_attr,
                                                      reconnect_name, connect_name):
    """A reconnect spawned before a re-attach sleeps through its backoff; the
    re-attach reset intentional_disconnect, so only the generation tells it
    that the session is no longer its to re-dial."""
    from Orchestrator.routes.voice_ws_shared import claim_session

    dialed = []

    async def fake_connect(session, *a, **k):
        dialed.append(session)
        return True

    monkeypatch.setattr(module, connect_name, fake_connect)

    async def run():
        session = cls(session_id=f"old-reconnect-{ws_attr}", operator="system")
        claim_session(session)
        new_upstream = FakeUpstreamWS()

        real_sleep = asyncio.sleep

        async def backoff_with_reattach(delay, result=None):
            # During the backoff the client re-attached and connected afresh.
            claim_session(session)
            setattr(session, ws_attr, new_upstream)
            session.intentional_disconnect = False
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", backoff_with_reattach)
        await getattr(module, reconnect_name)(session)
        monkeypatch.setattr(asyncio, "sleep", real_sleep)
        return session, new_upstream

    session, new_upstream = asyncio.run(run())
    assert dialed == [], "a superseded reconnect must not re-dial"
    assert getattr(session, ws_attr) is new_upstream
    assert new_upstream.closed is False


# ---------------------------------------------------------------------------
# Phone bridge: ONE owner for the Gemini reconnect
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_phone_owned_gemini_session_is_not_reconnected_by_the_route(monkeypatch):
    """The phone bridge runs gemini_listener inside its own reconnect loop. The
    route's listener ALSO spawned gemini_reconnect on the same close, so two
    dialers raced for session.gemini_ws (one orphaned upstream per drop)."""
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)
    session = make_session(external_reconnect=True)
    session.gemini_ws = FakeGeminiWS(closing_exc=_closed())

    await glr.gemini_listener(session)
    await asyncio.sleep(0)

    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_owned_gemini_goaway_closes_for_the_owner_to_redial(monkeypatch):
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)
    session = make_session(external_reconnect=True)
    upstream = FakeGeminiWS()
    session.gemini_ws = upstream

    await glr.handle_gemini_message(session, {"goAway": {"timeLeft": "0s"}})
    for _ in range(5):
        await asyncio.sleep(0)

    reconnect.assert_not_awaited()
    upstream.close.assert_awaited()


@pytest.mark.parametrize("module,cls,ws_attr,reconnect_name,connect_name,configure_name,"
                         "listener_name,loop_name", [
    pytest.param(glr, GeminiLiveSession, "gemini_ws", "gemini_reconnect", "connect_to_gemini",
                 "configure_gemini_session", "gemini_listener", "gemini_keepalive_loop",
                 id="gemini"),
    pytest.param(gk, GrokLiveSession, "grok_ws", "grok_reconnect", "connect_to_grok",
                 "configure_grok_session", "grok_listener", "grok_keepalive_loop", id="grok"),
    pytest.param(rt, RealtimeSession, "openai_ws", "openai_reconnect", "connect_to_openai",
                 "configure_openai_session", "openai_listener", "openai_keepalive_loop",
                 id="openai"),
])
def test_reconnect_restores_the_keepalive(monkeypatch, module, cls, ws_attr, reconnect_name,
                                          connect_name, configure_name, listener_name,
                                          loop_name):
    """With the message-age timer gone, ConnectionClosed from the listener or
    the keepalive is the liveness signal; the keepalive loop exits when its
    socket closes, so a reconnected session must get a live one back."""
    started = []

    async def fake_connect(session, *a, **k):
        setattr(session, ws_attr, FakeUpstreamWS())
        return True

    async def fake_loop(session):
        started.append(session)
        await asyncio.sleep(3600)

    async def fake_listener(session):
        await asyncio.sleep(3600)

    monkeypatch.setattr(module, connect_name, fake_connect)
    monkeypatch.setattr(module, configure_name, AsyncMock())
    monkeypatch.setattr(module, listener_name, fake_listener)
    monkeypatch.setattr(module, loop_name, fake_loop)

    async def run():
        session = cls(session_id=f"ka-{ws_attr}", operator="system")
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        session.keepalive_task = finished      # the old loop already exited
        await getattr(module, reconnect_name)(session)
        await _REAL_SLEEP(0)
        alive = session.keepalive_task is not None and not session.keepalive_task.done()
        for t in (session.keepalive_task, session.listener_task):
            if t is not None:
                t.cancel()
        return session, alive

    session, alive = asyncio.run(run())
    assert started == [session]
    assert alive


# ---------------------------------------------------------------------------
# Ownership holds THROUGH the old owner's teardown (review of rank 50)
# ---------------------------------------------------------------------------
# The Portal re-attaches 1 s after a drop that both ends saw (a Tailscale idle
# reap, say) while the old handler's teardown is still running: retiring the
# upstream, then a /chat/save that mints inline (2-4 s on the dev box). Checked
# only on entry, the old teardown's tail then nulled the new attach's client
# socket, marked it disconnected and freed its buffers: deaf and reapable while
# its upstream ran.
async def _reattach(session, ws_attr, new_portal, *, retire=True):
    """What a re-attaching handler does: claim, then (connect) retire + reset + dial."""
    from Orchestrator.routes.voice_ws_shared import claim_session, reset_for_connect, retire_upstream

    session.portal_ws = new_portal
    claim_session(session)
    if retire:
        await retire_upstream(session, ws_attr)
    reset_for_connect(session)
    new_upstream = FakeUpstreamWS()
    setattr(session, ws_attr, new_upstream)
    session.listener_task = asyncio.create_task(_forever())
    session.keepalive_task = asyncio.create_task(_forever())
    session.status = "connected"
    return new_upstream


@pytest.mark.parametrize("module,cls,ws_attr,save_name", TEARDOWNS)
def test_reattach_while_the_old_owner_saves_keeps_the_new_attach(monkeypatch, module, cls,
                                                                 ws_attr, save_name):
    from Orchestrator.live_session_reaper import is_reapable
    from Orchestrator.routes.voice_ws_shared import claim_session

    saved = []

    async def run():
        save_started, release_save = asyncio.Event(), asyncio.Event()

        async def slow_save(s):
            saved.append([m["content"] for m in s.conversation])
            save_started.set()
            await release_save.wait()        # /chat/save + inline perform_mint
            del s.conversation[:1]           # conversation_releaser: exactly the saved turn

        monkeypatch.setattr(module, save_name, slow_save)
        session = cls(session_id=f"save-race-{ws_attr}", operator="system")
        old_portal, new_portal = FakePortalWS(), FakePortalWS()
        session.portal_ws = old_portal
        old_gen = claim_session(session)
        setattr(session, ws_attr, FakeUpstreamWS())
        session.listener_task = asyncio.create_task(_forever())
        session.conversation = [{"role": "user", "content": "old attach turn",
                                 "timestamp": "2026-10-23T00:00:00Z"}]

        teardown = asyncio.create_task(module._end_portal_session(session, old_portal, old_gen, None))
        await save_started.wait()
        new_upstream = await _reattach(session, ws_attr, new_portal)
        session.transcript_buffer = "a reply the new attach is speaking"
        session.conversation.append({"role": "user", "content": "new attach turn",
                                     "timestamp": "2026-10-23T00:00:05Z"})
        release_save.set()
        await teardown

        now = time.time()
        state = dict(
            portal_ws_is_new=session.portal_ws is new_portal,
            status=session.status,
            upstream_current=getattr(session, ws_attr) is new_upstream,
            upstream_open=not new_upstream.closed,
            listener_alive=not session.listener_task.done(),
            keepalive_alive=not session.keepalive_task.done(),
            transcript_buffer=session.transcript_buffer,
            conversation=[m["content"] for m in session.conversation],
            reapable=is_reapable(session, now + 3600),
            intentional=session.intentional_disconnect,
        )
        session.listener_task.cancel()
        session.keepalive_task.cancel()
        return state

    state = asyncio.run(run())
    assert saved == [["old attach turn"]]
    assert state == dict(portal_ws_is_new=True, status="connected", upstream_current=True,
                         upstream_open=True, listener_alive=True, keepalive_alive=True,
                         transcript_buffer="a reply the new attach is speaking",
                         conversation=["new attach turn"], reapable=False,
                         intentional=False), state


@pytest.mark.parametrize("module,cls,ws_attr,save_name", TEARDOWNS)
def test_reattach_while_the_old_owner_retires_keeps_the_new_upstream(monkeypatch, module, cls,
                                                                     ws_attr, save_name):
    """retire_upstream awaits each cancellation; a re-attach that connects in
    between installs its own upstream, listener and keepalive, which the old
    owner's retire must neither close nor forget."""
    from Orchestrator.routes.voice_ws_shared import claim_session

    save = AsyncMock()
    monkeypatch.setattr(module, save_name, save)

    async def run():
        unwinding, finish_unwind = asyncio.Event(), asyncio.Event()

        async def old_listener():
            try:
                await asyncio.sleep(3600)
            finally:
                unwinding.set()
                await finish_unwind.wait()   # e.g. a cancelled tool stopping a CU task

        session = cls(session_id=f"retire-race-{ws_attr}", operator="system")
        old_portal, new_portal = FakePortalWS(), FakePortalWS()
        session.portal_ws = old_portal
        old_gen = claim_session(session)
        old_upstream = FakeUpstreamWS()
        setattr(session, ws_attr, old_upstream)
        session.listener_task = asyncio.create_task(old_listener())
        await asyncio.sleep(0)

        teardown = asyncio.create_task(module._end_portal_session(session, old_portal, old_gen, None))
        await unwinding.wait()
        # The re-attach's connect ran to completion meanwhile (its own retire
        # found nothing more to stop than the old owner already is).
        new_upstream = await _reattach(session, ws_attr, new_portal, retire=False)
        finish_unwind.set()
        await teardown

        state = dict(
            old_closed=old_upstream.closed,
            upstream_current=getattr(session, ws_attr) is new_upstream,
            upstream_open=not new_upstream.closed,
            listener_alive=session.listener_task is not None and not session.listener_task.done(),
            keepalive_alive=session.keepalive_task is not None and not session.keepalive_task.done(),
            portal_ws_is_new=session.portal_ws is new_portal,
            status=session.status,
        )
        for t in (session.listener_task, session.keepalive_task):
            if t is not None:
                t.cancel()
        return state, save

    state, save = asyncio.run(run())
    assert state == dict(old_closed=True, upstream_current=True, upstream_open=True,
                         listener_alive=True, keepalive_alive=True, portal_ws_is_new=True,
                         status="connected"), state
    save.assert_not_awaited()   # the newer owner saves the conversation


@pytest.mark.parametrize("module,cls,ws_attr,reconnect_name,connect_name", [
    pytest.param(glr, GeminiLiveSession, "gemini_ws", "gemini_reconnect", "connect_to_gemini",
                 id="gemini"),
    pytest.param(gk, GrokLiveSession, "grok_ws", "grok_reconnect", "connect_to_grok",
                 id="grok"),
    pytest.param(rt, RealtimeSession, "openai_ws", "openai_reconnect", "connect_to_openai",
                 id="openai"),
])
def test_every_spawned_reconnect_is_cancelled_by_a_reattach(monkeypatch, module, cls, ws_attr,
                                                            reconnect_name, connect_name):
    """The listener's close handler and the keepalive can both spawn a reconnect
    in one tick, before either sets is_reconnecting. Only the LAST spawn used
    to be tracked, but the FIRST is the one that dials: a re-attach cancelled
    the no-op one, and the real one's dial then replaced the new upstream."""
    from Orchestrator.routes.voice_ws_shared import (claim_session, reset_for_connect,
                                                     retire_upstream, spawn_reconnect)

    dials = []
    dial_done = None

    async def slow_connect(session, *a, **k):
        dials.append(session)
        await dial_done.wait()               # the TLS dial in progress
        setattr(session, ws_attr, FakeUpstreamWS())
        return True

    monkeypatch.setattr(module, connect_name, slow_connect)

    async def run():
        nonlocal dial_done
        dial_done = asyncio.Event()
        session = cls(session_id=f"double-spawn-{ws_attr}", operator="system")
        session.portal_ws = FakePortalWS()
        claim_session(session)
        reconnect = getattr(module, reconnect_name)
        spawned = [spawn_reconnect(session, reconnect(session)),
                   spawn_reconnect(session, reconnect(session))]
        while not dials:                     # past the 0.5 s backoff, now dialing
            await _REAL_SLEEP(0.05)

        # The client re-attaches and connects afresh; then the old dial returns.
        claim_session(session)
        await retire_upstream(session, ws_attr)
        reset_for_connect(session)
        new_upstream = FakeUpstreamWS()
        setattr(session, ws_attr, new_upstream)
        dial_done.set()
        for _ in range(10):
            await _REAL_SLEEP(0)
        return session, new_upstream, spawned

    session, new_upstream, spawned = asyncio.run(run())
    assert len(dials) == 1, "only one of the two reconnects dials"
    assert all(t.done() for t in spawned), \
        "both reconnects, the dialing one included, end with the re-attach"
    assert getattr(session, ws_attr) is new_upstream and not new_upstream.closed, \
        "the old reconnect's dial replaced the re-attached session's upstream"


class _FakeDialedWS(FakeUpstreamWS):
    pass


@pytest.mark.parametrize("module,cls,ws_attr,connect_name,key", [
    pytest.param(glr, GeminiLiveSession, "gemini_ws", "connect_to_gemini", "GOOGLE_API_KEY",
                 id="gemini"),
    pytest.param(gk, GrokLiveSession, "grok_ws", "connect_to_grok", "XAI_API_KEY", id="grok"),
    pytest.param(rt, RealtimeSession, "openai_ws", "connect_to_openai", "OPENAI_API_KEY",
                 id="openai"),
])
def test_a_dial_superseded_midway_never_replaces_the_new_upstream(monkeypatch, module, cls,
                                                                  ws_attr, connect_name, key):
    """connect_to_<provider> wrote session.<ws> when its dial returned, whoever
    owned the session by then: a detached reconnect dialing across a re-attach
    replaced (and orphaned) the newer attach's upstream."""
    from Orchestrator.routes.voice_ws_shared import claim_session

    monkeypatch.setattr(module, key, "test-key")
    monkeypatch.setattr(module, "WEBSOCKETS_AVAILABLE", True)
    session = cls(session_id=f"dial-race-{ws_attr}", operator="system")
    claim_session(session)
    new_upstream = FakeUpstreamWS()
    dialed = _FakeDialedWS()

    async def connect(url, **kwargs):
        # A re-attach claims the session and connects while this dial is open.
        claim_session(session)
        setattr(session, ws_attr, new_upstream)
        return dialed

    monkeypatch.setattr(module.websockets, "connect", connect)
    ok = asyncio.run(getattr(module, connect_name)(session))

    assert ok is False
    assert getattr(session, ws_attr) is new_upstream and not new_upstream.closed
    assert dialed.closed, "the superseded dial is closed, not leaked"


# ---------------------------------------------------------------------------
# A turn whose turnComplete never comes is still saved (review of rank 50)
# ---------------------------------------------------------------------------
# turnComplete waits for PLAYBACK. Moving the flush there (one entry per turn)
# opened a window: an End tap or a hang-up during the reply (the goodbye) left
# both sides of the last exchange in the buffers, and release_payload() threw
# them away. The base code flushed on generationComplete and saved them.
async def _speak_without_turn_complete(session):
    await glr.handle_gemini_message(
        session, {"serverContent": {"inputTranscription": {"text": "say goodbye"}}})
    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "Goodbye, have a great day."}}})
    await glr.handle_gemini_message(session, {"serverContent": {"generationComplete": True}})


def _capture_saves(monkeypatch):
    summaries = []

    async def fake_save(**kwargs):
        summaries.append(kwargs["session_summary"])
        if kwargs.get("on_safe"):
            kwargs["on_safe"]()
        return True

    monkeypatch.setattr(glr, "save_voice_transcript", fake_save)
    return summaries


def test_portal_end_during_the_reply_saves_the_last_exchange(monkeypatch):
    from Orchestrator.routes.voice_ws_shared import claim_session

    summaries = _capture_saves(monkeypatch)

    async def run():
        session = make_session()
        generation = claim_session(session)
        session.gemini_ws = FakeUpstreamWS()
        await _speak_without_turn_complete(session)
        await glr._end_portal_session(session, session.portal_ws, generation, None)
        return session

    session = asyncio.run(run())
    assert len(summaries) == 1
    assert "[User]: say goodbye" in summaries[0]
    assert "[AI]: Goodbye, have a great day." in summaries[0]
    assert summaries[0].index("[User]: say goodbye") < summaries[0].index("[AI]: Goodbye")
    assert session.conversation == [] and session.transcript_buffer == ""


def test_phone_hangup_during_the_reply_saves_the_last_exchange(monkeypatch):
    """The phone bridge's stop() saves through save_session_to_blackbox."""
    summaries = _capture_saves(monkeypatch)

    async def run():
        session = make_session()
        await _speak_without_turn_complete(session)
        await glr.save_session_to_blackbox(session)

    asyncio.run(run())
    assert len(summaries) == 1 and "Goodbye, have a great day." in summaries[0]
    assert "say goodbye" in summaries[0]


@pytest.mark.asyncio
async def test_a_completed_turn_is_not_saved_twice():
    session = make_session()
    await _speak_without_turn_complete(session)
    await glr.handle_gemini_message(session, {"serverContent": {"turnComplete": True}})
    glr._flush_open_turn(session)
    assert [m["role"] for m in session.conversation] == ["user", "assistant"]
