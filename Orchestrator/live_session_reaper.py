"""Bounded reaper for the live voice-session registries (memory-leak fix 2026-06-14).

GEMINI_LIVE_SESSIONS / GROK_LIVE_SESSIONS / REALTIME_SESSIONS (Orchestrator/models.py)
were append-only: each voice session inserted an object on connect — carrying the
full `conversation` transcript plus a base64 PCM16 `user_audio_buffer` — and nothing
ever removed it on disconnect. Over a day of voice usage the dicts accumulated dead
session objects, growing RAM unbounded until the process hit MemoryHigh and recycled.

The disconnect handlers intentionally keep a session briefly for reconnect/resume
(they null the websockets and set `status="disconnected"`). This module bounds that:

  * `release_payload()` — called from each disconnect `finally`, drops the heavy
    audio/transcript buffers immediately (the conversation is already persisted to
    the BlackBox by then), so a lingering session is cheap.
  * the periodic reaper — evicts sessions that are explicitly `status="disconnected"`
    and past a short grace window. It NEVER evicts a session that isn't disconnected,
    which keeps in-flight sessions safe — including phone-bridge realtime sessions
    that legitimately have no `portal_ws`.

Transcript durability (audit rank 51, O09-live-voice-providers-13): a failed
/chat/save used to be "kept for a later retry" that nothing ran, and eviction
dropped it. Each sweep now retries the voice-transcript spool
(Orchestrator/voice_transcript_spool.py, written ahead of every save), the
first pass runs right after startup, and a reapable session still holding
conversation turns is saved before it is evicted.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("blackbox.live_reaper")

# How long a disconnected session is retained for reconnect/resume before eviction.
DISCONNECTED_GRACE_SEC = 120.0
# How often the background sweep runs.
REAP_INTERVAL_SEC = 60.0
# The startup spool pass waits this long, so the server is already answering
# the /chat/save the retry posts to.
STARTUP_RETRY_DELAY_SEC = 20.0
# A session whose turns could neither be saved nor spooled is kept for further
# save attempts, but not forever: past this age it is evicted with an error.
UNSAVED_MAX_AGE_SEC = 24 * 3600.0


def _epoch(iso: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp to epoch seconds; None if missing/unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def is_reapable(session: Any, now: float, *, grace: float = DISCONNECTED_GRACE_SEC) -> bool:
    """True if a live-voice session should be evicted from its registry.

    Safe by construction:
      * a session with a live `portal_ws` is NEVER reaped (active client attached);
      * a session not explicitly `status="disconnected"` is NEVER reaped (this is
        what protects in-flight phone-bridge realtime sessions, which have no portal_ws);
      * a disconnected session is reaped only after `grace` seconds (preserving the
        reconnect/resume window). A disconnected session with no parseable
        `last_activity` is treated as stale and reaped.
    """
    if getattr(session, "portal_ws", None) is not None:
        return False
    if getattr(session, "status", None) != "disconnected":
        return False
    last = _epoch(getattr(session, "last_activity", None))
    if last is None:
        return True
    return (now - last) >= grace


def release_payload(session: Any) -> None:
    """Drop the heavy, no-longer-needed buffers from a disconnected session.

    Call from the disconnect `finally` AFTER the conversation has been persisted
    to the BlackBox. Clears the base64 audio buffer (the bulk of per-session RAM)
    and the transcript scratch buffer. The `conversation` list is left intact for
    the brief reconnect/resume window; the reaper frees it when it evicts the entry.
    """
    if hasattr(session, "user_audio_buffer"):
        session.user_audio_buffer = []
    if hasattr(session, "transcript_buffer"):
        session.transcript_buffer = ""
    if hasattr(session, "input_transcript_buffer"):
        session.input_transcript_buffer = ""


async def reap(sessions: dict, now: Optional[float] = None, *,
               grace: float = DISCONNECTED_GRACE_SEC,
               save: Optional[Callable[[Any], Awaitable[Any]]] = None) -> list[str]:
    """Remove reapable sessions from `sessions` in place; return the removed ids.

    With `save` (the route's save_*_to_blackbox), a reapable session that still
    holds conversation turns is saved first, and evicted only once they are
    safe (saved or spooled: the save function clears the conversation then).
    One whose turns stay unsaved is kept for the next sweep, up to
    UNSAVED_MAX_AGE_SEC; a session with no operator can never be saved and is
    evicted as before.
    """
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    removed: list[str] = []
    for sid, s in list(sessions.items()):
        if not is_reapable(s, now, grace=grace):
            continue
        if save is not None and getattr(s, "conversation", None) and getattr(s, "operator", ""):
            try:
                await save(s)
            except Exception as e:  # noqa: BLE001 — one session must not stop the sweep
                logger.error("[LIVE-REAPER] save of %s before eviction failed: %s", sid, e)
            if getattr(s, "conversation", None):
                last = _epoch(getattr(s, "last_activity", None))
                if last is not None and (now - last) < UNSAVED_MAX_AGE_SEC:
                    logger.warning("[LIVE-REAPER] %s still holds %d unsaved turns; kept for "
                                   "the next sweep", sid, len(s.conversation))
                    continue
                logger.error("[LIVE-REAPER] evicting %s with %d turns that could not be "
                             "saved or spooled", sid, len(s.conversation))
            # A client may have re-attached while the save ran.
            if not is_reapable(s, now, grace=grace):
                continue
        if sessions.get(sid) is s:
            sessions.pop(sid, None)
            removed.append(sid)
    return removed


def _registries() -> "dict[str, tuple[dict, Callable[[Any], Awaitable[Any]]]]":
    # Imported lazily to avoid any import cycle with models/routes.
    from Orchestrator.models import (
        GEMINI_LIVE_SESSIONS,
        GROK_LIVE_SESSIONS,
        REALTIME_SESSIONS,
    )
    from Orchestrator.routes import gemini_live_routes, grok_live_routes, realtime_routes
    return {
        "gemini_live": (GEMINI_LIVE_SESSIONS, gemini_live_routes.save_session_to_blackbox),
        "grok_live": (GROK_LIVE_SESSIONS, grok_live_routes.save_grok_session_to_blackbox),
        "realtime": (REALTIME_SESSIONS, realtime_routes.save_session_to_blackbox),
    }


async def retry_transcripts(*, ignore_schedule: bool = False) -> "dict[str, int]":
    """Retry the spooled voice transcripts that are due; never raises."""
    try:
        from Orchestrator.routes.voice_ws_shared import retry_spooled_transcripts
        counts = await retry_spooled_transcripts(ignore_schedule=ignore_schedule)
    except Exception as e:  # noqa: BLE001 — a maintenance pass must never raise.
        logger.error("[LIVE-REAPER] transcript spool retry failed (non-fatal): %s", e)
        return {}
    if any(counts.values()):
        logger.info("[LIVE-REAPER] transcript spool retry: %s", counts)
    return counts


async def reap_all(now: Optional[float] = None) -> "dict[str, int]":
    """Sweep all three live-session registries (saving before evicting), then
    retry the transcript spool. Returns {registry: removed_count}."""
    counts: dict[str, int] = {}
    for name, (registry, save) in _registries().items():
        removed = await reap(registry, now, save=save)
        if removed:
            counts[name] = len(removed)
    if counts:
        logger.info("[LIVE-REAPER] evicted stale sessions: %s", counts)
    await retry_transcripts()
    return counts


async def _reaper_loop(interval: float = REAP_INTERVAL_SEC,
                       startup_delay: float = STARTUP_RETRY_DELAY_SEC) -> None:
    # Startup pass: transcripts a crash, restart or outage left in the spool
    # go out as soon as the server answers again, not one sweep later.
    try:
        await asyncio.sleep(startup_delay)
        await retry_transcripts(ignore_schedule=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a maintenance loop must never die.
        logger.error("[LIVE-REAPER] startup spool retry failed (non-fatal): %s", e)
    while True:
        try:
            await asyncio.sleep(interval)
            await reap_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a maintenance loop must never die.
            logger.error("[LIVE-REAPER] sweep failed (non-fatal): %s", e)


def start_reaper(interval: float = REAP_INTERVAL_SEC) -> "asyncio.Task":
    """Start the periodic reaper on the running event loop (call from app startup)."""
    return asyncio.create_task(_reaper_loop(interval))
