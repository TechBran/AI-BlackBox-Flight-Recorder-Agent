#!/usr/bin/env python3
"""Orchestrator-side integration for the on-box Qwen3-TTS llama-swap member.

The SINGLE seam between the TTS routes / Voice Lab and the on-box Qwen3-TTS
server (LocalModels/qwen_tts_server, run as the `qwen-tts` llama-swap member
on :9098). It provides:
  - the 9 CustomVoice preset voices (static) + saved clone/design profiles,
  - the dynamic `qwen` catalog group (present only when the stack is healthy
    AND TTS is enabled — fail-open, like the ElevenLabs/local dynamic groups),
  - synthesis via the llama-swap /v1/audio/speech proxy (body-`model`
    auto-routed to the qwen-tts member),
  - the /upstream/qwen-tts/… URL builder for clone/design/save — NON-OpenAI
    paths llama-swap does NOT auto-route by body-model (spec §5.4, open #245).

Everything fails soft: on a box without the local stack, _tts_available() is
False and the catalog group is simply absent (the cloud groups remain).
Profile listing reads Manifest/voices/qwen/ from disk so building the catalog
NEVER wakes the audio group (no cross-group swap just to render the picker).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import requests

# llama-swap member id — the value put in the request `model` field so
# llama-swap auto-routes /v1/audio/speech to our qwen-tts server.
QWEN_TTS_MODEL = "qwen-tts"

# Generous timeout: a cold audio-group swap (~5-8s, §5.2) PLUS slow 1.7B batch
# synthesis on the RTX 2000 Ada (RTF ~2.4-4x, §5.4) can run long; llama-swap
# holds the request through the swap. The client shows the D10 "loading models…"
# affordance during the wait. Streaming (G3) will cut first-byte latency later.
QWEN_TTS_TIMEOUT = 300  # seconds

# The 9 CustomVoice presets shipped with Qwen3-TTS (spec §5.4, §14-verified).
# (raw_voice_token, short_description). Descriptions are ours (the model card
# ships none) and only populate the picker's "name - description" line.
QWEN_PRESET_VOICES: List[Tuple[str, str]] = [
    ("Vivian", "Warm, expressive"),
    ("Serena", "Calm, measured"),
    ("Uncle_Fu", "Deep, avuncular"),
    ("Dylan", "Bright, youthful"),
    ("Eric", "Neutral, clear"),
    ("Ryan", "Confident, direct"),
    ("Aiden", "Friendly, relaxed"),
    ("Ono_Anna", "Soft, gentle"),
    ("Sohee", "Light, melodic"),
]

# Streaming-tier expectation-setting copy (correction [25] / §5.4). ONE
# canonical string reused by the Voice Lab tab and (later) the wizard
# local_models step so the UI never over-promises 1.7B streaming on the 2000 Ada.
QWEN_STREAM_TIER_NOTE = (
    "On-box streaming uses the 0.6B voice tier for low latency; the 1.7B "
    "voices are used for batch/file quality. (Streaming size is finalized by "
    "the G3 benchmark on your GPU.)"
)


# --------------------------------------------------------------------------
# local_stack seams (isolated so the catalog/synth code has ONE place to reach
# the resolver, and tests patch exactly here — no need for local_stack to be
# importable in a unit test).
# --------------------------------------------------------------------------
def _tts_available() -> bool:
    """True when the on-box stack is healthy AND TTS is enabled."""
    try:
        from Orchestrator import local_stack
        return bool(local_stack.is_healthy() and local_stack.enabled("tts"))
    except Exception:
        return False


def _base_url() -> str:
    """llama-swap front-door base, e.g. http://127.0.0.1:9098/v1 (no trailing /)."""
    from Orchestrator import local_stack
    return local_stack.base_url().rstrip("/")


def upstream_url(path: str) -> str:
    """Build a /upstream/qwen-tts/<path> URL (auto-loads the member, honors
    group swap/exclusivity) for NON-OpenAI paths llama-swap won't auto-route."""
    base = _base_url()                                   # …:9098/v1
    root = base[:-3] if base.endswith("/v1") else base   # …:9098
    return f"{root}/upstream/{QWEN_TTS_MODEL}{path}"


# --------------------------------------------------------------------------
# Voice profiles (clones + saved designs) — persisted by the qwen-tts server
# under Manifest/voices/qwen/{slug}/profile.json (spec §5.4). Read from disk.
# --------------------------------------------------------------------------
def _profiles_root():
    from Orchestrator.utils.paths import manifest_dir
    return manifest_dir() / "voices" / "qwen"


def list_profiles() -> List[Dict[str, Any]]:
    """Return saved clone/design profiles, newest first. Each dict carries at
    least {slug, name, variant}. Fail-soft: any error -> []."""
    out: List[Dict[str, Any]] = []
    try:
        root = _profiles_root()
        if not root.is_dir():
            return []
        for d in sorted(root.iterdir(), key=lambda p: p.name):
            if not d.is_dir():
                continue
            pf = d / "profile.json"
            if not pf.is_file():
                continue
            try:
                meta = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "slug": d.name,
                "name": meta.get("name") or d.name,
                "variant": meta.get("variant", "custom"),
                "operator": meta.get("operator", ""),
                "created": meta.get("created", ""),
            })
    except Exception:
        return []
    # newest first when a created timestamp exists, else stable name order
    out.sort(key=lambda m: m.get("created", ""), reverse=True)
    return out


def delete_profile(slug: str) -> bool:
    """Remove a saved profile directory. Delete is a pure filesystem op (no
    server round-trip): voices are lazy-loaded from disk per request, so a
    removed dir simply won't be found next time. Returns True if it existed."""
    import shutil
    # slug is a directory name; refuse path traversal.
    if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
        return False
    d = _profiles_root() / slug
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


# --------------------------------------------------------------------------
# Catalog group + synthesis
# --------------------------------------------------------------------------
def catalog_group() -> Optional[Dict[str, Any]]:
    """Return the dynamic 'qwen' TTS catalog group, or None when the on-box
    TTS capability is unavailable (fail-open — the picker keeps its cloud
    groups). Voice ids are `qwen:<voice-or-slug>`; saved profiles are
    star-prefixed like ElevenLabs My Voices."""
    if not _tts_available():
        return None
    voices: List[Dict[str, str]] = [
        {"id": f"qwen:{tok}", "name": tok.replace("_", " "), "description": desc}
        for tok, desc in QWEN_PRESET_VOICES
    ]
    for p in list_profiles():
        voices.append({
            "id": f"qwen:{p['slug']}",
            "name": f"⭐ {p['name']}",
            "description": p.get("variant", "custom"),
        })
    if not voices:
        return None
    return {"id": "qwen", "label": "Qwen3-TTS (On-Box)",
            "dynamic": True, "voices": voices}


def synthesize(voice: str, text: str, response_format: str = "wav",
               stream: bool = False) -> "requests.Response":
    """POST the llama-swap /v1/audio/speech proxy (body-`model` auto-routed to
    the qwen-tts member). `voice` is the BARE token (preset name or profile
    slug — the caller strips any `qwen:` prefix). Returns the raw Response so
    the route decides stream-vs-file. Raises on transport error.

    NB: the M6 server emits WAV/PCM only — its /v1/audio/speech 400s any
    response_format not in ('wav','pcm') (Task 6.4, `test_speech_bad_format_400`).
    Default 'wav' (a proper RIFF container the browser plays); callers pass
    'wav'/'pcm', never 'mp3'/'opus' (there is no mp3/opus encoder in the server)."""
    req = {
        "model": QWEN_TTS_MODEL,
        "input": text,
        "voice": voice,
        "response_format": response_format,
        "stream": stream,
    }
    return requests.post(f"{_base_url()}/audio/speech", json=req,
                         timeout=QWEN_TTS_TIMEOUT)
