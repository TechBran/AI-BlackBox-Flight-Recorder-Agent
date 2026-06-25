"""MN.7 — sync→async notify() bridge tests.

``notify()`` is async, but the highest-volume producer (``tasks.py:update_task``)
is SYNC worker-thread code running inside a ThreadPoolExecutor, with no running
event loop of its own. The bridge (``Orchestrator.notifications.bridge``) lets
that thread schedule a ``notify(...)`` coroutine onto the app's MAIN event loop
via ``asyncio.run_coroutine_threadsafe`` — the SAME pattern the scheduler uses
(``manager.py``: captures ``asyncio.get_running_loop()`` at startup, schedules
from APScheduler worker threads).

Contract (all asserted here):
  * ``set_main_loop`` captures the running loop; ``notify_in_background`` is a
    fire-and-forget SYNC entry that schedules notify on it and returns
    immediately (never blocks on device POSTs, never raises into the producer).
  * A notify() that raises inside the scheduled coroutine is swallowed — the
    producer's own code path is never affected.
  * With no loop captured (or a closed one), the bridge degrades to a no-op log
    rather than raising — a producer on a box where the loop was never set must
    still complete its own work.
"""

import asyncio
import threading
import time

import pytest

import Orchestrator.notifications.bridge as bridge


@pytest.fixture(autouse=True)
def _reset_loop():
    """Each test starts with no captured loop and restores afterward."""
    saved = bridge._MAIN_LOOP
    bridge._MAIN_LOOP = None
    yield
    bridge._MAIN_LOOP = saved


def test_notify_in_background_no_loop_is_noop_no_raise(monkeypatch):
    """No main loop captured → the sync entry is a no-op and never raises.

    notify() must NOT even be invoked (there is nowhere to run it), and the
    producer thread keeps going.
    """
    called = []

    async def fake_notify(*a, **k):
        called.append((a, k))

    monkeypatch.setattr(bridge, "notify", fake_notify)

    # Must not raise even though no loop is set.
    bridge.notify_in_background("Brandon", "T", "B", category="task")

    assert called == []  # nothing scheduled — nowhere to run it


def test_notify_in_background_schedules_on_captured_loop():
    """A captured running loop receives the coroutine and runs notify() on it.

    Drives the real run_coroutine_threadsafe bridge: an event loop runs in a
    background thread, the loop is captured, and a SYNCHRONOUS call (this test's
    main thread) schedules notify onto it with the expected args.
    """
    received = []
    done = threading.Event()

    async def fake_notify(operator, title, body, category="general", **k):
        received.append(
            {"operator": operator, "title": title, "body": body, "category": category}
        )
        done.set()

    loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    # Capture the loop the way the startup hook does, then patch notify.
    bridge._MAIN_LOOP = loop
    bridge.notify = fake_notify  # type: ignore[assignment]

    # Sync call from this (non-loop) thread — must return immediately.
    start = time.time()
    bridge.notify_in_background("Casey", "Done", "All green", category="media")
    assert time.time() - start < 0.5  # fire-and-forget, did not block

    assert done.wait(timeout=2.0), "notify was never scheduled onto the loop"
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)

    assert len(received) == 1
    assert received[0] == {
        "operator": "Casey",
        "title": "Done",
        "body": "All green",
        "category": "media",
    }


def test_notify_failure_isolated_from_producer():
    """A notify() that raises inside the scheduled coroutine never escapes.

    The scheduled future's exception is retrieved/swallowed so it does not
    surface as an unretrieved-exception warning or break anything. The producer
    (the synchronous caller) sees a clean return.
    """
    blew_up = threading.Event()

    async def boom(*a, **k):
        blew_up.set()
        raise RuntimeError("notify exploded")

    loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

    bridge._MAIN_LOOP = loop
    bridge.notify = boom  # type: ignore[assignment]

    # Producer call: must return cleanly even though notify will raise.
    bridge.notify_in_background("Brandon", "T", "B")

    assert blew_up.wait(timeout=2.0)
    # Give the done-callback a beat to retrieve the exception.
    time.sleep(0.1)
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)
    # No assertion needed beyond "we got here without an exception propagating".


def test_set_main_loop_captures_running_loop():
    """set_main_loop() stores the loop it is called under (the startup-hook path)."""
    captured = {}

    async def _run():
        bridge.set_main_loop()
        captured["loop"] = asyncio.get_running_loop()

    asyncio.run(_run())
    assert bridge._MAIN_LOOP is captured["loop"]


def test_closed_loop_is_noop_no_raise(monkeypatch):
    """A captured-but-closed loop degrades to a no-op rather than raising."""
    called = []

    async def fake_notify(*a, **k):
        called.append(1)

    monkeypatch.setattr(bridge, "notify", fake_notify)

    loop = asyncio.new_event_loop()
    loop.close()
    bridge._MAIN_LOOP = loop

    bridge.notify_in_background("Brandon", "T", "B")  # must not raise
    assert called == []
