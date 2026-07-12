"""P1.6 — goAway.timeLeft is honored: graceful reconnect BEFORE the deadline.

Google warns via goAway {timeLeft} ~before the ~10-min connection cut. The old
code reconnected IMMEDIATELY on goAway (throwing away the remaining window);
the fix schedules the reconnect for (timeLeft - margin), falling back to
immediate when the field is missing/unparseable (= old behavior).
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import make_session


def test_goaway_delay_parsing():
    f = glr._goaway_delay_seconds
    assert f({"timeLeft": "10s"}) == pytest.approx(8.0)   # 2s safety margin
    assert f({"timeLeft": "9.5s"}) == pytest.approx(7.5)
    assert f({"timeLeft": "1s"}) == 0.0                    # floored at 0
    assert f({"timeLeft": {"seconds": 5}}) == pytest.approx(3.0)
    assert f({}) == 0.0                                    # missing -> immediate
    assert f({"timeLeft": "garbage"}) == 0.0               # unparseable -> immediate


@pytest.mark.asyncio
async def test_goaway_zero_timeleft_reconnects_immediately(monkeypatch):
    session = make_session()
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)

    await glr.handle_gemini_message(session, {"goAway": {"timeLeft": "0s"}})
    await asyncio.sleep(0.05)

    reconnect.assert_awaited_once_with(session)


@pytest.mark.asyncio
async def test_goaway_long_timeleft_defers_reconnect(monkeypatch):
    session = make_session()
    reconnect = AsyncMock()
    monkeypatch.setattr(glr, "gemini_reconnect", reconnect)

    await glr.handle_gemini_message(session, {"goAway": {"timeLeft": "600s"}})
    await asyncio.sleep(0.05)

    reconnect.assert_not_awaited()  # scheduled ~598s out, not fired now

    # Cancel the deferred task so the loop closes clean.
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
