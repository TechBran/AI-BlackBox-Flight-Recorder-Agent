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
    """Normalize ElevenLabs error responses to one-line BlackBox messages."""
    detail = (body or {}).get("detail")
    status = detail.get("status") if isinstance(detail, dict) else None
    if status in _ERROR_HINTS:
        return _ERROR_HINTS[status]
    if status_code == 401:
        return _ERROR_HINTS["auth_error"]
    msg = detail.get("message") if isinstance(detail, dict) else (detail or "")
    return f"ElevenLabs error {status_code}: {str(msg)[:160]}"
