"""P1.5 — reconnect respawns the Gemini listener; counter = consecutive failures.

Recon finding #1 (THE months-of-flakiness bug): gemini_listener was spawned
exactly once at WS connect; gemini_reconnect re-dialed and re-sent setup but
NOTHING ever read the new socket — a permanently mute session that reported
"reconnected", looping forever because reconnect_count reset to 0 on every
"success". Pattern ported from phone/bridge.py:_gemini_listener_with_reconnect.

Pins: (a) a successful gemini_reconnect spawns a NEW gemini_listener task;
(b) reconnect_count is NOT reset by gemini_reconnect itself; (c) setupComplete
(proof a listener READ the new socket) resets it.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import FakeGeminiWS, make_session


@pytest.mark.asyncio
async def test_successful_reconnect_respawns_listener_and_keeps_count(monkeypatch):
    session = make_session()
    session.gemini_ws = None  # old socket already gone

    async def fake_connect(sess):
        sess.gemini_ws = FakeGeminiWS()
        sess.status = "connected"
        return True

    spawned = []

    async def fake_listener(sess):
        spawned.append(sess)

    monkeypatch.setattr(glr, "connect_to_gemini", fake_connect)
    monkeypatch.setattr(glr, "configure_gemini_session", AsyncMock())
    monkeypatch.setattr(glr, "gemini_listener", fake_listener)

    await glr.gemini_reconnect(session)
    await asyncio.sleep(0)  # let the spawned listener task run

    assert spawned == [session], "reconnect must spawn a fresh gemini_listener"
    assert session.listener_task is not None
    assert session.reconnect_count == 1, (
        "reconnect_count must NOT reset on 'setup sent' — only setupComplete "
        "(a real read from the new socket) may reset it"
    )
    assert session.is_reconnecting is False
    assert session.status == "connected"


@pytest.mark.asyncio
async def test_setup_complete_resets_failure_count():
    session = make_session()
    session.reconnect_count = 3

    await glr.handle_gemini_message(session, {"setupComplete": {}})

    assert session.reconnect_count == 0


@pytest.mark.asyncio
async def test_reconnect_bails_when_session_torn_down(monkeypatch):
    """P1.5-fix: a detached reconnect that resumes after teardown must NOT
    re-dial or respawn a listener (which would resurrect a clientless session
    the reaper can't evict)."""
    session = make_session()
    session.intentional_disconnect = True  # teardown already ran

    connect = AsyncMock(return_value=True)
    listener_spawned = []

    async def fake_listener(sess):
        listener_spawned.append(sess)

    monkeypatch.setattr(glr, "connect_to_gemini", connect)
    monkeypatch.setattr(glr, "configure_gemini_session", AsyncMock())
    monkeypatch.setattr(glr, "gemini_listener", fake_listener)

    await glr.gemini_reconnect(session)

    connect.assert_not_awaited()          # bailed after backoff, before dialing
    assert listener_spawned == []          # no listener respawned
    assert session.listener_task is None
    assert session.is_reconnecting is False
