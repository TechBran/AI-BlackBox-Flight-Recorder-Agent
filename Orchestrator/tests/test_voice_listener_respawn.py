"""P1b: OpenAI + Grok reconnect paths must respawn their upstream listener task.

Two properties per route:
  1. A successful reconnect CANCELS the old listener (bound to the closed ws)
     and RESPAWNS a fresh one on the NEW ws — otherwise the session is a
     permanently mute one-way pipe that still reports "reconnected".
  2. A reconnect that RESUMES AFTER the WS endpoint tore the session down
     (intentional_disconnect) must BAIL — never re-dial, never respawn a
     listener, never flip status back to "connected". Both reconnects run
     DETACHED via bare create_task and sleep for backoff, so a Portal drop
     mid-reconnect could otherwise resurrect a clientless session the reaper
     (status=="disconnected" only) can never evict. Same hazard class the
     Gemini P1.5 fix (786f892 + dd74f42) closed.
"""
import asyncio

import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import GrokLiveSession, RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS, FakeStreamWS, FakePortalWS


async def _stuck_task(started):
    started.set()
    await asyncio.sleep(3600)


def _respawn_check(monkeypatch, module, session, reconnect_fn,
                   listener_name, connect_name, configure_name, ws_attr):
    async def run():
        started = asyncio.Event()
        session.listener_task = asyncio.create_task(_stuck_task(started))
        await started.wait()
        old_task = session.listener_task

        spawned = []

        async def fake_listener(s):
            spawned.append(s)

        async def fake_connect(s, *a, **k):
            setattr(s, ws_attr, FakeUpstreamWS())
            return True

        async def fake_configure(s, *a, **k):
            return None

        monkeypatch.setattr(module, listener_name, fake_listener)
        monkeypatch.setattr(module, connect_name, fake_connect)
        monkeypatch.setattr(module, configure_name, fake_configure)

        await reconnect_fn(session)

        # Old listener must be cancelled (it is bound to the OLD ws object).
        try:
            await asyncio.wait_for(old_task, timeout=2)
        except asyncio.CancelledError:
            pass
        assert old_task.done(), "old listener task was never cancelled"

        await asyncio.sleep(0)  # let the respawned task run
        assert spawned == [session], "reconnect must respawn the listener on the NEW ws"
        assert session.listener_task is not old_task
        assert session.status == "connected"
    asyncio.run(run())


def _bail_after_teardown_check(monkeypatch, module, session, reconnect_fn,
                               listener_name, connect_name, configure_name):
    """Teardown ran (intentional_disconnect) before/while the reconnect slept:
    the after-backoff guard must bail before dialing or respawning."""
    async def run():
        session.intentional_disconnect = True  # WS endpoint finally already ran

        dialed = []
        spawned = []

        async def fake_connect(s, *a, **k):
            dialed.append(s)
            return True

        async def fake_configure(s, *a, **k):
            return None

        async def fake_listener(s):
            spawned.append(s)

        monkeypatch.setattr(module, connect_name, fake_connect)
        monkeypatch.setattr(module, configure_name, fake_configure)
        monkeypatch.setattr(module, listener_name, fake_listener)

        await reconnect_fn(session)
        await asyncio.sleep(0)

        assert dialed == [], "reconnect must not re-dial a torn-down session"
        assert spawned == [], "reconnect must not respawn a listener after teardown"
        assert session.listener_task is None
        assert session.status != "connected", "must not flip a torn-down session to connected"
        assert session.is_reconnecting is False
    asyncio.run(run())


def _bail_during_reconfigure_check(monkeypatch, module, session, reconnect_fn,
                                   listener_name, connect_name, configure_name, ws_attr):
    """Teardown completes DURING reconfigure — after the dial succeeds but
    before the status flip. The point-of-no-return guard must still bail,
    closing the just-opened socket rather than respawning a listener."""
    async def run():
        spawned = []

        async def fake_connect(s, *a, **k):
            setattr(s, ws_attr, FakeUpstreamWS())
            return True

        async def fake_configure(s, *a, **k):
            s.intentional_disconnect = True  # endpoint finally runs mid-reconfigure

        async def fake_listener(s):
            spawned.append(s)

        monkeypatch.setattr(module, connect_name, fake_connect)
        monkeypatch.setattr(module, configure_name, fake_configure)
        monkeypatch.setattr(module, listener_name, fake_listener)

        await reconnect_fn(session)
        await asyncio.sleep(0)

        assert spawned == [], "must not respawn a listener onto a torn-down session"
        assert session.listener_task is None
        assert session.status != "connected", "must not flip a torn-down session to connected"
        assert getattr(session, ws_attr) is None, "socket opened during the dial must be closed"
        assert session.is_reconnecting is False
    asyncio.run(run())


# ---------------------------------------------------------------------------
# OpenAI (realtime_routes) — P1.29
# ---------------------------------------------------------------------------
def test_openai_reconnect_respawns_listener(monkeypatch):
    session = RealtimeSession(session_id="t-rt-respawn", operator="system")
    _respawn_check(monkeypatch, rt, session, rt.openai_reconnect,
                   "openai_listener", "connect_to_openai",
                   "configure_openai_session", "openai_ws")


def test_openai_reconnect_bails_when_torn_down(monkeypatch):
    session = RealtimeSession(session_id="t-rt-bail", operator="system")
    _bail_after_teardown_check(monkeypatch, rt, session, rt.openai_reconnect,
                               "openai_listener", "connect_to_openai",
                               "configure_openai_session")


def test_openai_reconnect_bails_during_reconfigure(monkeypatch):
    session = RealtimeSession(session_id="t-rt-bail2", operator="system")
    _bail_during_reconfigure_check(monkeypatch, rt, session, rt.openai_reconnect,
                                   "openai_listener", "connect_to_openai",
                                   "configure_openai_session", "openai_ws")


# ---------------------------------------------------------------------------
# Grok (grok_live_routes) — P1.30
# ---------------------------------------------------------------------------
def test_grok_reconnect_respawns_listener(monkeypatch):
    session = GrokLiveSession(session_id="t-gk-respawn", operator="system")
    _respawn_check(monkeypatch, gk, session, gk.grok_reconnect,
                   "grok_listener", "connect_to_grok",
                   "configure_grok_session", "grok_ws")


def test_grok_reconnect_bails_when_torn_down(monkeypatch):
    session = GrokLiveSession(session_id="t-gk-bail", operator="system")
    _bail_after_teardown_check(monkeypatch, gk, session, gk.grok_reconnect,
                               "grok_listener", "connect_to_grok",
                               "configure_grok_session")


def test_grok_reconnect_bails_during_reconfigure(monkeypatch):
    session = GrokLiveSession(session_id="t-gk-bail2", operator="system")
    _bail_during_reconfigure_check(monkeypatch, gk, session, gk.grok_reconnect,
                                   "grok_listener", "connect_to_grok",
                                   "configure_grok_session", "grok_ws")


# --- Final-review fix: reconnect_count resets on a PROVEN-HEALTHY server frame,
#     not on dial-success (else a handshake-then-drop loop makes max_reconnects
#     unreachable — the mute-reconnect class the Gemini setupComplete-reset fixed). ---

def test_openai_listener_resets_reconnect_count_on_server_frame(monkeypatch):
    async def _run():
        session = RealtimeSession(session_id="rc-openai")
        session.reconnect_count = 3
        session.openai_ws = FakeStreamWS(['{"type":"session.updated"}'])
        session.portal_ws = FakePortalWS()
        monkeypatch.setattr(rt, "handle_openai_message", _noop_async)
        await rt.openai_listener(session)
        assert session.reconnect_count == 0, "a real server frame must reset the failure count"
    asyncio.run(_run())


def test_grok_listener_resets_reconnect_count_on_server_frame(monkeypatch):
    async def _run():
        session = GrokLiveSession(session_id="rc-grok")
        session.reconnect_count = 3
        session.grok_ws = FakeStreamWS(['{"type":"session.updated"}'])
        session.portal_ws = FakePortalWS()
        monkeypatch.setattr(gk, "handle_grok_message", _noop_async)
        await gk.grok_listener(session)
        assert session.reconnect_count == 0, "a real server frame must reset the failure count"
    asyncio.run(_run())


async def _noop_async(*args, **kwargs):
    return None
