"""ElevenLabs single-source-of-truth catalog: live models/voices/user with TTL cache.

The provider API IS the source of truth. Every downstream feature (TTS picker,
status endpoint, character limits) derives from these fetchers rather than from
hardcoded facts -- config.py holds only OUR choices, never provider facts.

All HTTP flows through ONE choke point, ``_get_json``, so auth + error mapping
(in ``client.py``) exist exactly once and tests can mock the whole network with
a single monkeypatch.

Caching: a module-level ``_cache`` keyed by logical name ("models"/"voices"/
"user"). A value younger than ``TTL_SECONDS`` is returned without an HTTP call;
``force=True`` bypasses it. No key configured -> fetchers return ``None`` (the
feature is simply hidden) without raising.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from Orchestrator.elevenlabs import client

TTL_SECONDS = 300

# logical name -> (fetched_at_epoch, value)
_cache: dict[str, tuple[float, Any]] = {}


def _get_json(path: str, params: dict | None = None) -> dict:
    """The single ElevenLabs HTTP choke point. Raises RuntimeError on non-2xx."""
    resp = requests.get(
        client.BASE_URL + path,
        headers=client.auth_headers(),
        params=params,
        timeout=15,
    )
    if not (200 <= resp.status_code < 300):
        body = None
        try:
            body = resp.json()
        except Exception:
            body = None  # error body may not be JSON; map_error tolerates None
        raise RuntimeError(client.map_error(resp.status_code, body))
    return resp.json()


def _cached(name: str, force: bool, producer):
    """Return cached value if fresh; otherwise call ``producer`` and store it.

    ``producer`` is invoked only when a (re)fetch is needed -- so the no-key
    guard inside each public fetcher runs before any network attempt.
    """
    if not force:
        hit = _cache.get(name)
        if hit is not None:
            fetched_at, value = hit
            if (time.time() - fetched_at) < TTL_SECONDS:
                return value
    value = producer()
    _cache[name] = (time.time(), value)
    return value


def _has_key() -> bool:
    """True only when an ElevenLabs key is configured (no network touched)."""
    try:
        return client.auth_headers() is not None
    except RuntimeError:
        return False


def _build_description(voice: dict) -> str:
    """Human label from (accent, gender, age) labels, else the voice's own description."""
    labels = voice.get("labels") or {}
    parts = [labels.get(k) for k in ("accent", "gender", "age")]
    parts = [p for p in parts if p]
    if parts:
        return ", ".join(parts)
    return voice.get("description") or ""


def _normalize_voice(voice: dict) -> dict:
    """Map a raw ElevenLabs voice to the catalog voice shape + additive fields."""
    return {
        "id": f"elevenlabs:{voice.get('voice_id')}",
        "name": voice.get("name"),
        "description": _build_description(voice),
        "preview_url": voice.get("preview_url"),
        "category": voice.get("category"),
    }


def get_models(force: bool = False) -> list | None:
    """GET /v1/models -- raw list passthrough, cached. None if no key."""
    if not _has_key():
        return None
    return _cached("models", force, lambda: _get_json("/v1/models"))


def get_voices(force: bool = False) -> dict | None:
    """Grouped voices {"my_voices": [...], "premade": [...]}, cached. None if no key.

    Paginates /v2/voices via ``next_page_token`` until ``has_more`` is False.
    Grouping uses ``is_owner`` as the primary signal: owned voices (or
    cloned/generated categories) -> my_voices; everything else (incl. shared
    LIBRARY voices with category "professional") -> premade.
    """
    if not _has_key():
        return None

    def produce() -> dict:
        all_voices: list[dict] = []
        params: dict = {"page_size": 100}
        while True:
            data = _get_json("/v2/voices", params=params)
            all_voices.extend(data.get("voices") or [])
            if not data.get("has_more"):
                break
            token = data.get("next_page_token")
            if not token:
                break
            params = {"page_size": 100, "next_page_token": token}

        my_voices: list[dict] = []
        premade: list[dict] = []
        for v in all_voices:
            owned = bool(v.get("is_owner")) or v.get("category") in ("cloned", "generated")
            (my_voices if owned else premade).append(_normalize_voice(v))
        return {"my_voices": my_voices, "premade": premade}

    return _cached("voices", force, produce)


def get_user(force: bool = False) -> dict | None:
    """GET /v1/user -- normalized plan/capabilities dict, cached. None if no key.

    Capability flags come straight from the API's explicit booleans; they are
    NOT inferred from the tier name.
    """
    if not _has_key():
        return None

    def produce() -> dict:
        data = _get_json("/v1/user")
        sub = data.get("subscription") or {}
        limit = sub.get("character_limit") or 0
        used = sub.get("character_count") or 0
        return {
            "tier": sub.get("tier"),
            "credits_remaining": limit - used,
            "credits_limit": limit,
            "can_use_instant_voice_cloning": bool(sub.get("can_use_instant_voice_cloning")),
            "can_use_professional_voice_cloning": bool(sub.get("can_use_professional_voice_cloning")),
            "raw": data,
        }

    return _cached("user", force, produce)


def bust_voices_cache() -> None:
    """Clear ONLY the voices cache entry (e.g. after a clone/design mutation)."""
    _cache.pop("voices", None)
