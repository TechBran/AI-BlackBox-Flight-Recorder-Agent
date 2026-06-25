"""MN.7 — the sync→async bridge for ``notify()``.

``notify()`` (``bus.py``) is a coroutine. Async producers ``await`` it directly.
But the highest-volume producer — ``tasks.py:update_task`` — runs in a SYNC
worker thread (a ``ThreadPoolExecutor`` inside the ``background_worker`` daemon
thread) that has NO event loop of its own. ``asyncio.run(notify(...))`` there
would spin up a throwaway loop (and break aiohttp sessions / the shared store
locks that expect the main loop); blocking on the result would stall a worker
on dead-device POST timeouts. Neither is acceptable.

The established codebase pattern (``scheduler/manager.py``) is:
  1. capture the app's MAIN event loop once, in an async startup hook
     (``asyncio.get_running_loop()``), and
  2. from a worker thread, schedule the coroutine onto it with
     ``asyncio.run_coroutine_threadsafe(coro, loop)``.

This module packages that for notifications as a fire-and-forget helper:

  * ``set_main_loop()``     — called from an async startup hook to capture the loop.
  * ``notify_in_background(...)`` — a SYNC entry that schedules ``notify(...)`` on the
    captured loop and returns IMMEDIATELY. It NEVER blocks on the result (the
    producer must not wait on device POST timeouts) and NEVER raises into the
    producer (a notify failure must never break the producer's own work). The
    scheduled future's exception is retrieved in a done-callback so it neither
    leaks as an unretrieved-exception warning nor surfaces anywhere.

If no loop has been captured (or it is closed), the helper degrades to a logged
no-op — a producer must still finish its own work on a box where the loop was
never set (e.g. a unit test importing tasks.py without a running app).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from Orchestrator.notifications.bus import notify

logger = logging.getLogger(__name__)

# The app's main event loop, captured once at startup. Module-level so the sync
# worker threads (which never see the loop directly) can reach it. Loop refs the
# loop itself holds are weak, but THIS is a strong module global — it outlives
# the startup hook's frame.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Capture the main event loop (call from an async startup hook).

    Defaults to ``asyncio.get_running_loop()`` so the common call site is simply
    ``set_main_loop()`` from inside an ``@app.on_event("startup")`` coroutine —
    mirroring ``scheduler.manager.start()``'s ``self._loop = get_running_loop()``.
    """
    global _MAIN_LOOP
    if loop is None:
        loop = asyncio.get_running_loop()
    _MAIN_LOOP = loop
    logger.info("[NOTIFY-BRIDGE] main event loop captured for sync→async notify")


def _swallow_future_exception(future) -> None:
    """Done-callback: retrieve the scheduled notify's exception so it never leaks.

    notify() is engineered never to raise, but if anything inside the scheduled
    coroutine ever did, this keeps it from surfacing as an unretrieved-exception
    warning AND guarantees it is fully isolated from the producer thread.
    """
    try:
        exc = future.exception()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — never propagate
        return
    if exc is not None:
        logger.warning("[NOTIFY-BRIDGE] background notify failed: %r", exc)


def notify_in_background(
    operator: str,
    title: str,
    body: str,
    category: str = "general",
    *,
    dedup_key: Optional[str] = None,
) -> None:
    """Fire-and-forget: schedule ``notify(...)`` on the main loop from sync code.

    Returns IMMEDIATELY — does not await the fan-out, does not block on the
    per-device POST timeouts, and NEVER raises into the producer. A missing /
    closed loop degrades to a logged no-op. Safe to call from any worker thread.
    """
    loop = _MAIN_LOOP
    if loop is None or loop.is_closed():
        logger.debug(
            "[NOTIFY-BRIDGE] no live main loop captured; dropping notify "
            "(op=%s cat=%s title=%r)",
            operator, category, title,
        )
        return
    try:
        future = asyncio.run_coroutine_threadsafe(
            notify(operator, title, body, category=category, dedup_key=dedup_key),
            loop,
        )
        future.add_done_callback(_swallow_future_exception)
    except Exception as e:  # noqa: BLE001 — scheduling itself must never break the producer
        logger.warning("[NOTIFY-BRIDGE] failed to schedule notify (non-fatal): %r", e)
