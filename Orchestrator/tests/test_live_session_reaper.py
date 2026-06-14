"""Regression tests for the live voice-session memory leak (2026-06-14).

Root cause: GEMINI_LIVE_SESSIONS / GROK_LIVE_SESSIONS / REALTIME_SESSIONS were
append-only — a session object (holding the full transcript + a base64 PCM16
audio buffer) was inserted on connect and NEVER removed on disconnect, so RAM
grew unbounded with voice usage until the process recycled.

The disconnect handlers intentionally keep a session briefly for reconnect/resume
(they null the websockets and set status="disconnected"). The fix is a bounded
reaper that evicts disconnected sessions past a short grace window and releases
the heavy audio payload immediately — WITHOUT ever touching an active session.

These tests pin the reaper's safety invariants (the load-bearing one being
"never reap a session that isn't explicitly disconnected", which protects
in-flight phone-bridge realtime sessions that legitimately have no portal_ws).
"""
from datetime import datetime, timezone, timedelta

from Orchestrator.models import GeminiLiveSession, GrokLiveSession, RealtimeSession
from Orchestrator.live_session_reaper import (
    is_reapable,
    reap,
    release_payload,
    DISCONNECTED_GRACE_SEC,
)

NOW = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()


def _iso(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def _disconnected(age_sec: float, cls=GeminiLiveSession):
    s = cls(session_id="s", status="disconnected")
    s.portal_ws = None
    s.last_activity = _iso(age_sec)
    return s


# --- release_payload: drop the heavy buffers, keep the (already-saved) transcript ---

def test_release_payload_drops_audio_and_transcript_keeps_conversation():
    s = GeminiLiveSession(session_id="s")
    s.user_audio_buffer = ["b64audiochunk"] * 500
    s.transcript_buffer = "half a sentence..."
    s.conversation = [{"role": "user", "text": "hi"}]

    release_payload(s)

    assert s.user_audio_buffer == []          # the big leaker — cleared
    assert s.transcript_buffer == ""          # scratch — cleared
    assert s.conversation == [{"role": "user", "text": "hi"}]  # kept for grace/resume


def test_release_payload_is_safe_on_sessions_without_audio_buffer():
    # GrokLiveSession/RealtimeSession have no user_audio_buffer attribute.
    s = GrokLiveSession(session_id="s")
    s.transcript_buffer = "x"
    release_payload(s)  # must not raise
    assert s.transcript_buffer == ""


# --- is_reapable: only disconnected-past-grace sessions, never active ones ---

def test_active_session_with_live_portal_is_never_reaped():
    s = GeminiLiveSession(session_id="s", status="connected")
    s.portal_ws = object()                    # live client attached
    s.last_activity = _iso(5 * 3600)          # 5h old, but ACTIVE
    assert is_reapable(s, NOW_TS) is False


def test_disconnected_past_grace_is_reaped():
    assert is_reapable(_disconnected(DISCONNECTED_GRACE_SEC + 30), NOW_TS) is True


def test_disconnected_within_grace_is_kept():
    assert is_reapable(_disconnected(DISCONNECTED_GRACE_SEC - 30), NOW_TS) is False


def test_non_disconnected_session_without_portal_is_not_reaped():
    # Load-bearing: an in-flight phone-bridge realtime session has status
    # "connected" and NO portal_ws. It must survive a sweep even when old.
    s = RealtimeSession(session_id="phone-abc", status="connected")
    s.portal_ws = None
    s.last_activity = _iso(5 * 3600)
    assert is_reapable(s, NOW_TS) is False


def test_disconnected_with_missing_timestamp_is_reaped():
    s = GrokLiveSession(session_id="s", status="disconnected")
    s.portal_ws = None
    s.last_activity = ""                      # unparseable/never stamped
    assert is_reapable(s, NOW_TS) is True


# --- reap: removes exactly the reapable entries from a registry ---

def test_reap_removes_only_stale_disconnected_entries():
    active = GeminiLiveSession(session_id="active", status="connected")
    active.portal_ws = object()
    sessions = {
        "old_disconnected": _disconnected(DISCONNECTED_GRACE_SEC + 60),
        "fresh_disconnected": _disconnected(10),
        "active": active,
    }

    removed = reap(sessions, NOW_TS)

    assert removed == ["old_disconnected"]
    assert set(sessions) == {"fresh_disconnected", "active"}


def test_reap_empty_registry_is_noop():
    assert reap({}, NOW_TS) == []
