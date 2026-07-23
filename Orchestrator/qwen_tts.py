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
import os
import time
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


# llama-swap returns 429 "Too many requests" when the qwen-tts member's
# concurrencyLimit is exceeded (a concurrent or piled-up request — e.g. an
# Auto-TTS queue firing sentences in parallel, or a user retrying a slow batch).
# One GPU serializes synthesis anyway, so back off and retry rather than
# hard-failing the caller (which 502'd the whole batch).
#
# DEADLINE-based (audit 2026-07-22): the old fixed 5-attempt count capped total
# backoff at 13.5s, which could not outlast a REAL synth holding the member
# (a long batch runs minutes). Retry while elapsed < QWEN_TTS_429_DEADLINE_S
# (default 60s) with the same capped exponential backoff.
def _429_deadline_s() -> float:
    try:
        return float(os.environ.get("QWEN_TTS_429_DEADLINE_S", "60"))
    except ValueError:
        return 60.0


_QWEN_429_BACKOFF_BASE = 0.5
_QWEN_429_BACKOFF_MAX = 6.0

# test seams — the deadline tests drive a fake clock through these
_monotonic = time.monotonic
_sleep = time.sleep


def _post_retry_429(do_post) -> "requests.Response":
    """Run do_post() and retry 429s with capped exponential backoff until the
    QWEN_TTS_429_DEADLINE_S budget is spent; then return the last response
    (callers surface the 429 like any other non-200)."""
    deadline = _429_deadline_s()
    start = _monotonic()
    attempts = 0
    while True:
        r = do_post()
        if r.status_code == 429 and (_monotonic() - start) < deadline:
            attempts += 1
            _sleep(min(_QWEN_429_BACKOFF_BASE * (2 ** (attempts - 1)),
                       _QWEN_429_BACKOFF_MAX))
            continue
        return r


def synthesize(voice: str, text: str, response_format: str = "wav",
               stream: bool = False) -> "requests.Response":
    """POST the llama-swap /v1/audio/speech proxy (body-`model` auto-routed to
    the qwen-tts member). `voice` is the BARE token (preset name or profile
    slug — the caller strips any `qwen:` prefix). Returns the raw Response so
    the route decides stream-vs-file. Raises on transport error. A 429 (member
    concurrency limit) is retried with capped exponential backoff until the
    QWEN_TTS_429_DEADLINE_S budget elapses so contention with another on-box
    synth recovers instead of 502-ing the caller.

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
    return _post_retry_429(lambda: requests.post(
        f"{_base_url()}/audio/speech", json=req, timeout=QWEN_TTS_TIMEOUT))


# --------------------------------------------------------------------------
# A3 (2026-07-22): native batch synthesis — ALL of a reply's chunks in ONE
# member request so the GPU runs ONE padded generate per sub-batch instead of
# a sequential per-chunk loop (measured 3.3x at b=4 / 6.3x at b=8; transport
# consolidation itself is ~free — the win is GPU batching).
# --------------------------------------------------------------------------
class QwenBatchUnsupported(RuntimeError):
    """The member predates /v1/audio/speech/batch (404/405) — caller should
    fall back to the per-chunk sequential path."""


def native_batch_enabled() -> bool:
    """QWEN_TTS_NATIVE_BATCH gate (default ON). Off restores the per-chunk
    sequential loop exactly as before A3."""
    return os.environ.get("QWEN_TTS_NATIVE_BATCH", "1").strip().lower() in (
        "1", "true", "yes", "on")


def synthesize_batch(voice: str, texts: List[str],
                     response_format: str = "wav") -> List[bytes]:
    """POST /upstream/qwen-tts/v1/audio/speech/batch (NON-OpenAI path —
    llama-swap does not body-model auto-route it; /upstream auto-loads the
    member and honors group swap/exclusivity, like clone/design). Returns the
    decoded per-chunk WAV bytes in input order.

    Raises QwenBatchUnsupported on 404/405 (old member) and RuntimeError on any
    other non-200 — the /tts/batch route falls back to the sequential path on
    the former and surfaces the latter. 429s back off exactly like
    synthesize() (deadline-based). Timeout scales with the batch size: the
    member holds ONE request for the whole reply (sub-batched internally), so
    the flat 300s single-chunk budget gets a per-chunk allowance on top."""
    import base64
    req = {
        "model": QWEN_TTS_MODEL,
        "inputs": list(texts),
        "voice": voice,
        "response_format": response_format,
    }
    timeout = QWEN_TTS_TIMEOUT + 20 * len(texts)
    r = _post_retry_429(lambda: requests.post(
        upstream_url("/v1/audio/speech/batch"), json=req, timeout=timeout))
    if r.status_code in (404, 405):
        raise QwenBatchUnsupported(f"member has no batch endpoint (HTTP {r.status_code})")
    if r.status_code != 200:
        raise RuntimeError(f"Qwen TTS batch failed (HTTP {r.status_code}): {r.text[:200]}")
    data = r.json()
    wavs = data.get("wavs_b64")
    if not isinstance(wavs, list) or len(wavs) != len(texts):
        raise RuntimeError(
            f"Qwen TTS batch returned {len(wavs) if isinstance(wavs, list) else 'no'} "
            f"wavs for {len(texts)} inputs")
    return [base64.b64decode(w) for w in wavs]
