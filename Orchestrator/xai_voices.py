"""xAI Custom Voices provider module (voice-upgrade pass, workstream 5).

Thin sync httpx client for ``https://api.x.ai/v1/custom-voices`` (clone from a
<=120s reference clip; the resulting voice_id is usable as a Grok voice-session
voice — recon: scratchpad recon/xaiResearch.json, wire shapes confirmed by
diagnostics/xai_custom_voices_probe.py).

The REST routes (routes/xai_voice_routes.py), the ToolVault executor
(ToolVault/tools/xai_clone_voice/), and the Grok live-session voice validation
(routes/grok_live_routes.py) ALL consume THIS module — one provider seam,
mirroring Orchestrator/elevenlabs/voices. Key is resolved FRESH per call
(never frozen at import) so wizard-pasted keys work without a restart.

Errors: unconfigured -> ``list_custom_voices`` returns None / mutators raise
RuntimeError("xAI not configured..."); provider 4xx/5xx -> RuntimeError with
the human message (routes map these to HTTP 400, same contract as elevenlabs).
"""
import os
import time

import httpx

XAI_VOICES_URL = "https://api.x.ai/v1/custom-voices"
_TIMEOUT = 30.0
_CLONE_TIMEOUT = 120.0  # uploads a reference clip; give it headroom

# 60s validity cache for Grok-session voice validation (is_custom_voice).
CACHE_TTL_SECS = 60.0
_cache: dict = {"ts": 0.0, "ids": frozenset()}


def resolve_api_key() -> str:
    """Fresh read every call — a key pasted in the wizard works with NO restart."""
    return os.getenv("XAI_API_KEY", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {resolve_api_key()}"}


def _raise_for_error(resp) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error") or resp.text
        except Exception:
            detail = resp.text
        raise RuntimeError(f"xAI error {resp.status_code}: {str(detail)[:300]}")


def _bust_cache() -> None:
    _cache["ts"] = 0.0


def list_custom_voices():
    """All cloned voices on the account, or None when no key is configured.

    Tolerates every probed envelope: bare list, {"voices": [...]}, {"data": [...]}.
    Raises RuntimeError on provider errors.
    """
    if not resolve_api_key():
        return None
    resp = httpx.get(XAI_VOICES_URL, headers=_headers(), timeout=_TIMEOUT)
    _raise_for_error(resp)
    body = resp.json()
    if isinstance(body, list):
        return body
    return body.get("voices") or body.get("data") or []


def clone_voice(name: str, audio_path: str, description: str | None = None) -> dict:
    """Clone a custom voice from ONE local reference clip (xAI enforces <=120s
    server-side). Returns the provider's voice object (voice_id + name)."""
    if not resolve_api_key():
        raise RuntimeError("xAI not configured - set XAI_API_KEY (onboarding wizard)")
    with open(audio_path, "rb") as fh:
        files = {"file": (os.path.basename(audio_path), fh, "application/octet-stream")}
        data = {"name": name}
        if description:
            data["description"] = description
        resp = httpx.post(XAI_VOICES_URL, headers=_headers(), data=data,
                          files=files, timeout=_CLONE_TIMEOUT)
    _raise_for_error(resp)
    _bust_cache()
    return resp.json()


def delete_voice(voice_id: str) -> None:
    if not resolve_api_key():
        raise RuntimeError("xAI not configured - set XAI_API_KEY (onboarding wizard)")
    resp = httpx.delete(f"{XAI_VOICES_URL}/{voice_id}", headers=_headers(), timeout=_TIMEOUT)
    _raise_for_error(resp)
    _bust_cache()


def voice_id_of(voice: dict) -> str:
    """Canonical id extraction — tolerates voice_id | id key naming."""
    return str(voice.get("voice_id") or voice.get("id") or "")


def is_custom_voice(voice_id: str) -> bool:
    """True iff ``voice_id`` is a cloned voice on this account. 60s TTL cache;
    FAIL-OPEN to catalog-only: no key / xAI unreachable -> False (the caller
    falls back to the built-in voice catalog). A failed refresh keeps the stale
    id set (graceful degradation) and does NOT stamp ts, so the next call retries.
    """
    if not voice_id or not resolve_api_key():
        return False
    now = time.time()
    if now - _cache["ts"] > CACHE_TTL_SECS:
        try:
            voices = list_custom_voices() or []
            _cache["ids"] = frozenset(voice_id_of(v) for v in voices)
            _cache["ts"] = now
        except Exception:
            pass  # keep stale ids; retry on next call
    return voice_id in _cache["ids"]
