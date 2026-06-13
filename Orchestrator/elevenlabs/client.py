"""ElevenLabs provider core: key resolution, auth headers, error normalization.

ALL ElevenLabs HTTP/WS traffic flows through this module's helpers so auth,
retries, and error mapping exist exactly once. Key is fresh-read from .env
(same mechanism as Orchestrator/stt/resolve.py) so an onboarding-saved key
works without a service restart.
"""
from __future__ import annotations
import os

BASE_URL = "https://api.elevenlabs.io"
WS_BASE_URL = "wss://api.elevenlabs.io"

# Provider error taxonomy -> short human-readable BlackBox messages.
_ERROR_HINTS = {
    "auth_error": "ElevenLabs auth failed - check ELEVENLABS_API_KEY",
    "quota_exceeded": "ElevenLabs quota exceeded - add credits or upgrade plan",
    "rate_limited": "ElevenLabs rate limit hit - retry shortly",
    "commit_throttled": "ElevenLabs STT commits throttled - slow commit cadence",
    "queue_overflow": "ElevenLabs STT audio queue overflow - reduce chunk rate",
    "resource_exhausted": "ElevenLabs concurrency limit reached for your plan",
    "session_time_limit_exceeded": "ElevenLabs STT session hit max duration - reconnect",
    "chunk_size_exceeded": "ElevenLabs STT chunk too large - send smaller chunks",
    "insufficient_audio_activity": "ElevenLabs STT heard no speech",
}


def _env_file_path() -> str:
    from Orchestrator.onboarding.secrets_writer import ENV_FILE
    return str(ENV_FILE)


def resolve_api_key() -> str | None:
    """Fresh-read ELEVENLABS_API_KEY: .env first, os.environ fallback."""
    try:
        from dotenv import dotenv_values
        env = dotenv_values(_env_file_path())
    except Exception:
        env = {}
    key = (env.get("ELEVENLABS_API_KEY") or os.getenv("ELEVENLABS_API_KEY") or "").strip()
    return key or None


def auth_headers(key: str | None = None) -> dict:
    k = key or resolve_api_key()
    if not k:
        raise RuntimeError("No ElevenLabs API key configured")
    return {"xi-api-key": k}


def map_error(status_code: int, body: dict | None) -> str:
    """Normalize ElevenLabs error responses to one-line BlackBox messages.

    Handles BOTH wire shapes the API uses:
    - HTTP error bodies: ``{"detail": {"status": "quota_exceeded"}}`` or
      ``{"detail": "some message"}``.
    - Realtime-WS error frames: ``{"message_type": "auth_error", "error": "..."}``
      (verified live: a bad key yields exactly this shape).
    """
    body = body or {}
    detail = body.get("detail")
    # Taxonomy-code candidates from every known location; first match wins.
    candidates = [
        detail.get("status") if isinstance(detail, dict) else None,
        body.get("status"),
        body.get("message_type"),  # realtime WS frames carry the code here
    ]
    code = next((c for c in candidates if c in _ERROR_HINTS), None)
    if code:
        return _ERROR_HINTS[code]
    if status_code == 401:
        return _ERROR_HINTS["auth_error"]
    # Human-message fallback across both shapes.
    if isinstance(detail, dict):
        msg = detail.get("message") or ""
    elif isinstance(detail, str):
        msg = detail
    else:
        msg = ""
    msg = msg or body.get("error") or body.get("message") or ""
    return f"ElevenLabs error {status_code}: {str(msg)[:160]}"


def classify_realtime_frame(event: dict | None) -> str | None:
    """Return the taxonomy code if a Scribe realtime frame is an error frame,
    else None.

    Error frames carry the code in ``message_type`` (e.g. ``"auth_error"``)
    and/or a top-level ``"error"`` string. Normal frames
    (``partial_transcript``, ``committed_transcript``, ``session_started``)
    have neither, so this returns None for them.
    """
    event = event or {}
    mt = event.get("message_type", "") or ""
    if mt in _ERROR_HINTS or "error" in mt:
        return mt
    if event.get("error"):
        return mt or "error"
    return None
