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
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("blackbox.live_reaper")

# How long a disconnected session is retained for reconnect/resume before eviction.
DISCONNECTED_GRACE_SEC = 120.0
# How often the background sweep runs.
REAP_INTERVAL_SEC = 60.0


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


def reap(sessions: dict, now: Optional[float] = None, *, grace: float = DISCONNECTED_GRACE_SEC) -> list[str]:
    """Remove reapable sessions from `sessions` in place; return the removed ids."""
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    removed = [sid for sid, s in list(sessions.items()) if is_reapable(s, now, grace=grace)]
    for sid in removed:
        sessions.pop(sid, None)
    return removed


def _registries() -> "dict[str, dict]":
    # Imported lazily to avoid any import cycle with models/routes.
    from Orchestrator.models import (
        GEMINI_LIVE_SESSIONS,
        GROK_LIVE_SESSIONS,
        REALTIME_SESSIONS,
    )
    return {
        "gemini_live": GEMINI_LIVE_SESSIONS,
        "grok_live": GROK_LIVE_SESSIONS,
        "realtime": REALTIME_SESSIONS,
    }


def reap_all(now: Optional[float] = None) -> "dict[str, int]":
    """Sweep all three live-session registries. Returns {registry: removed_count}."""
    counts: dict[str, int] = {}
    for name, registry in _registries().items():
        removed = reap(registry, now)
        if removed:
            counts[name] = len(removed)
    if counts:
        logger.info("[LIVE-REAPER] evicted stale sessions: %s", counts)
    return counts


async def _reaper_loop(interval: float = REAP_INTERVAL_SEC) -> None:
    while True:
        try:
            await asyncio.sleep(interval)
            reap_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a maintenance loop must never die.
            logger.error("[LIVE-REAPER] sweep failed (non-fatal): %s", e)


def start_reaper(interval: float = REAP_INTERVAL_SEC) -> "asyncio.Task":
    """Start the periodic reaper on the running event loop (call from app startup)."""
    return asyncio.create_task(_reaper_loop(interval))
