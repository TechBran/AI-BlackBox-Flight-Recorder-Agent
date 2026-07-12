"""P1.7 — silence kill: WS close code/reason reach the client; terminal
disconnect CLOSES the portal WS.

Silence layer 1 of the 2026-07-11 outage: Google rejects a bad setup by
CLOSING the socket with a code/reason (1007 'items: missing field'); the old
listener printed it and reconnected with the same broken setup while the
portal WS sat open answering pings ("Connected — listening" forever).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import FakeGeminiWS, make_session


@pytest.mark.asyncio
async def test_close_code_and_reason_forwarded_as_error(monkeypatch):
    exc = ConnectionClosedError(
        Close(1007, "function_declarations[53]...items: missing field"), None
    )
    session = make_session(gemini_ws=FakeGeminiWS(closing_exc=exc))
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)

    await glr.gemini_listener(session)
    await asyncio.sleep(0)

    errors = session.portal_ws.frames("error")
    assert len(errors) == 1
    assert errors[0]["code"] == 1007
    assert "1007" in errors[0]["data"]          # data stays a string (client contract)
    assert "missing field" in errors[0]["reason"]
    reconnect.assert_awaited_once()             # close still triggers reconnect


@pytest.mark.asyncio
async def test_mid_reconnect_close_sends_no_contradictory_disconnected():
    exc = ConnectionClosedError(Close(1000, ""), None)
    session = make_session(gemini_ws=FakeGeminiWS(closing_exc=exc))
    session.is_reconnecting = True  # gemini_reconnect owns this close

    await glr.gemini_listener(session)

    assert session.portal_ws.frames("disconnected") == []
    assert session.portal_ws.frames("error") == []
    assert session.status != "disconnected"  # reaper grace clock must NOT start mid-recovery


@pytest.mark.asyncio
async def test_max_reconnects_closes_portal_ws(monkeypatch):
    session = make_session()
    session.reconnect_count = session.max_reconnects
    monkeypatch.setattr(glr, "save_session_to_blackbox", AsyncMock())

    await glr.gemini_reconnect(session)

    assert session.portal_ws.frames("disconnected"), "client must be told"
    assert session.portal_ws.closed, "portal WS must be CLOSED on terminal disconnect"
    assert session.portal_ws.closed[0][0] == 1011
