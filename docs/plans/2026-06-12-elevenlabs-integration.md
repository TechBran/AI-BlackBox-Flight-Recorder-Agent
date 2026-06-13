# ElevenLabs Full-Platform Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> **Design doc (read first):** `docs/plans/2026-06-12-elevenlabs-integration-design.md` — all decisions, research digest, and risk table live there.
> **Status:** PLAN ONLY — nothing committed/pushed yet. Execution starts on branch `feat/elevenlabs-integration` when Brandon green-lights.

**Goal:** Integrate ElevenLabs as a first-class BYOK provider across the BlackBox: onboarding key + validation, streaming STT (third `/ws/stt` provider), diarized batch STT swept through every transcription touchpoint, dynamic TTS voice catalog with hybrid selector, Voice Lab (cloning + design) with agent tools, music/SFX/audio-utility tools, and a phone-path phase.

**Architecture:** One provider package `Orchestrator/elevenlabs/` owns all ElevenLabs traffic (client + capability modules + SoT catalog layer with TTL caches). Everything user-visible derives from the provider's own discovery endpoints (`/v1/models`, `/v2/voices`, `/v1/user`) — config.py holds only OUR defaults, never provider facts. Frontends hydrate from `/elevenlabs/status` and the existing `/tts/catalog` + `/stt/catalog` contracts (additive changes only — the Android Kotlin app parses these natively).

**Tech Stack:** FastAPI (star-import route modules), `requests` (sync paths) / `aiohttp` + `websockets` (async/WS), ToolVault v2 modules (`schema.json` + `executor.py`), Portal vanilla-JS modules, Android Compose (`AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal/`), pytest (hermetic — mocked HTTP, fixture transcripts, NO live key in tests).

**Conventions that bind every task:**
- **Quality-first defaults**: flagship model (`eleven_v3`), max output quality; cheaper/faster = explicit, visible downgrades.
- **Provider-explicit tool names**: `elevenlabs_*`; Lyria rename `generate_music` → `lyria_music` (Task 29).
- **Three surfaces**: any UI change ships Portal web + Android Kotlin + WebView-wrapper check.
- **Doc-verify ritual**: ElevenLabs publishes every doc page as fetchable markdown (append `.md` to the docs URL). Each task touching their API starts by fetching the named doc page and verifying field names before coding — exact wire-field names below are from June 2026 research and may drift.
- **Test runner**: `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/<file> -v`
- **ToolVault loop**: edit module → `Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate` → `curl -X POST http://localhost:9091/toolvault/reload`. Frozen `BLACKBOX_TOOLS_*`/`CHAT_TOOLS_*` arrays need a service restart (pre-authorized: `sudo systemctl restart blackbox.service`, 60–90s warm-up).

---

## Task 0: Branch setup

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
git checkout -b feat/elevenlabs-integration
```
No commit. (When committing later: stage explicit paths only — never `git add -A`.)

---

# Phase 1 — Provider core, onboarding key, status endpoint

## Task 1: Provider package + key resolution

**Files:**
- Create: `Orchestrator/elevenlabs/__init__.py` (empty)
- Create: `Orchestrator/elevenlabs/client.py`
- Test: `Orchestrator/tests/test_elevenlabs_client.py`

**Step 1: Write the failing test**

```python
"""Hermetic tests for the ElevenLabs client core. No network, no live key."""
import pytest
from Orchestrator.elevenlabs import client as el


def test_resolve_key_prefers_env_file(monkeypatch, tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text('ELEVENLABS_API_KEY="xi-from-file"\n')
    monkeypatch.setattr(el, "_env_file_path", lambda: str(envfile))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert el.resolve_api_key() == "xi-from-file"


def test_resolve_key_falls_back_to_os_environ(monkeypatch, tmp_path):
    monkeypatch.setattr(el, "_env_file_path", lambda: str(tmp_path / "missing.env"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-from-env")
    assert el.resolve_api_key() == "xi-from-env"


def test_resolve_key_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(el, "_env_file_path", lambda: str(tmp_path / "missing.env"))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert el.resolve_api_key() is None


def test_map_error_normalizes_provider_taxonomy():
    assert el.map_error(401, {"detail": {"status": "auth_error"}}).startswith("ElevenLabs auth")
    assert "quota" in el.map_error(429, {"detail": {"status": "quota_exceeded"}}).lower()
    assert el.map_error(500, {}).startswith("ElevenLabs error")
```

**Step 2: Run to verify it fails** — `... -m pytest Orchestrator/tests/test_elevenlabs_client.py -v` → FAIL (`ModuleNotFoundError`).

**Step 3: Implement `Orchestrator/elevenlabs/client.py`** (mirror the fresh-read pattern from `Orchestrator/stt/resolve.py:6-31` — that docstring explains WHY: wizard-saved keys must work without restart):

```python
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
```

**Step 4: Run tests** → 4 PASS.
**Step 5: Commit** — `git add Orchestrator/elevenlabs/ Orchestrator/tests/test_elevenlabs_client.py && git commit -m "feat(elevenlabs): provider core - key resolution + error normalization"`

## Task 2: SoT catalog layer (models / voices / user, TTL cache + bust)

**Files:**
- Create: `Orchestrator/elevenlabs/catalog.py`
- Test: `Orchestrator/tests/test_elevenlabs_catalog.py`

**Doc-verify:** fetch `https://elevenlabs.io/docs/api-reference/voices/search.md` and the models/user reference pages; confirm `GET /v1/models`, `GET /v2/voices` (params `page_size`, `next_page_token`, per-voice `voice_id/name/category/labels/preview_url`), `GET /v1/user` (subscription tier + character counts).

**Step 1: Failing tests** — monkeypatch `catalog._get_json` (the single HTTP choke point); assert: (a) `get_voices()` paginates until `has_more` false and groups `cloned/generated/professional` → `my_voices`, `premade` → `premade`; (b) results cached (second call: zero HTTP calls — count via mock); (c) `bust_voices_cache()` forces refetch; (d) `get_user()` returns `{"tier": ..., "credits_remaining": ..., "credits_limit": ...}`; (e) all three fetchers return `None` gracefully when no key configured.

**Step 3: Implement** — module-level cache dict `{key: (timestamp, value)}`, `TTL_SECONDS = 300`; public API:

```python
def get_models(force=False) -> list | None      # GET /v1/models, raw list passthrough
def get_voices(force=False) -> dict | None      # {"my_voices": [...], "premade": [...]}
def get_user(force=False) -> dict | None        # {"tier", "credits_remaining", "credits_limit", "raw"}
def bust_voices_cache() -> None
def _get_json(path, params=None) -> dict        # requests.get(BASE_URL+path, headers=auth_headers(), timeout=15)
```

Voice entries normalized to: `{"id": f"elevenlabs:{voice_id}", "name", "description" (from labels: "accent, gender, age"), "preview_url", "category"}` — matches the existing catalog voice shape (`config.build_tts_catalog` at `Orchestrator/config.py:501`) plus additive fields.

**Steps 4–5:** tests PASS; commit `feat(elevenlabs): SoT catalog layer - live models/voices/user with TTL cache`.

## Task 3: Onboarding validator

**Files:**
- Modify: `Orchestrator/onboarding/validators.py` (append after `validate_perplexity`, ~line 130; update docstring line 8: ElevenLabs moves Tier-2 → active)
- Test: `Orchestrator/tests/test_onboarding_validators.py` (extend existing if present, else create)

**Step 1: Failing test** — mock `requests.get`; assert `validate_elevenlabs("k")` returns `ok=True` with `detail` containing `tier` and a human `features` line; 401 → `ok=False` with the auth hint.

**Step 3: Implement** (follows `_measure` pattern, `validators.py:29`):

```python
def validate_elevenlabs(api_key: str) -> ValidationResult:
    """GET /v1/user - cheap metadata call. Surfaces plan tier + feature gates
    (IVC needs Starter+, PVC Creator+) so the wizard tells the customer what
    their key actually unlocks, not just that it works."""
    def _fn():
        import requests
        r = requests.get("https://api.elevenlabs.io/v1/user",
                         headers={"xi-api-key": api_key}, timeout=10)
        if r.status_code == 401:
            raise RuntimeError("Invalid ElevenLabs API key")
        r.raise_for_status()
        sub = r.json().get("subscription", {}) or {}
        tier = sub.get("tier", "free")
        paid = tier not in ("free", "")
        return {
            "tier": tier,
            "credits_remaining": (sub.get("character_limit", 0) or 0) - (sub.get("character_count", 0) or 0),
            "features": ("voice cloning available" if paid
                         else "free tier - voice cloning requires Starter+"),
        }
    return _measure(_fn)
```

**Step 5:** commit `feat(onboarding): ElevenLabs key validator with plan-tier detail`.

## Task 4: Validator dispatch + status endpoint

**Files:**
- Modify: `Orchestrator/routes/onboarding_routes.py:296` — add branch before the `else`:
  ```python
  elif req.provider == "elevenlabs":
      result = validators.validate_elevenlabs(creds["api_key"])
  ```
- Create: `Orchestrator/routes/elevenlabs_routes.py` — `GET /elevenlabs/status` (mirror `/embeddings/status` style; same `app` object the other route modules use):
  ```python
  @app.get("/elevenlabs/status")
  async def elevenlabs_status():
      """Single hydration point for ALL frontends (Portal/Android/wizard).
      No key -> {"configured": False} and every ElevenLabs UI hides."""
      from Orchestrator.elevenlabs.client import resolve_api_key
      from Orchestrator.elevenlabs import catalog
      if not resolve_api_key():
          return {"configured": False}
      user = catalog.get_user() or {}
      tier = user.get("tier", "free")
      paid = tier not in ("free", "")
      return {
          "configured": True, "tier": tier,
          "credits_remaining": user.get("credits_remaining"),
          "credits_limit": user.get("credits_limit"),
          "features": {
              "tts": True, "stt": True, "music": True, "sound_effects": True,
              "voice_changer": True, "voice_isolator": True,
              "instant_voice_cloning": paid, "professional_voice_cloning": tier in ("creator", "pro", "scale", "business"),
              "voice_design": True,
          },
      }
  ```
- Modify: `Orchestrator/app.py:95` area — add `from Orchestrator.routes.elevenlabs_routes import *`
- Test: `Orchestrator/tests/test_elevenlabs_status.py` (FastAPI TestClient; monkeypatch `resolve_api_key`/`get_user`; assert hidden vs configured shapes; assert IVC gate flips on tier)

**Step 5:** commit `feat(elevenlabs): /elevenlabs/status capability endpoint + validate dispatch`.

## Task 5: Onboarding wizard card goes live (Portal)

**Files:**
- Modify: `Portal/onboarding/steps/optional_integrations.js` — remove the `elevenlabs` entry from the v1.1-deferred list (lines ~36–41); add an active provider card modeled on the existing key-input cards in this file: password-type input for the API key → `POST /onboarding/validate {provider:"elevenlabs", credentials:{api_key}}` → on `ok` render the `detail.tier` + `detail.features` line ("✓ key valid — creator plan, voice cloning available") → `POST /onboarding/save {secrets:{ELEVENLABS_API_KEY: key}}`. Rehydrate: if `ELEVENLABS_API_KEY` present in `/onboarding/current-config`, render configured-state card with Replace (copy the Gmail rehydrate pattern documented at the top of this file).
- Modify: `Portal/index.html` — bump `?v=genuiXX`.

**Test:** manual — wizard page hard-refresh; enter junk key → clean per-provider error; enter real key → tier line renders; `.env` contains key; `GET /elevenlabs/status` flips to configured. (The wizard validate/save endpoints already have test coverage; frontend is verified by the three-surface checklist, Task 35.)

**Commit:** `feat(onboarding): activate ElevenLabs card - key input, tier-aware validation`.

## Task 6: Settings surface notes (Android + WebView)

Onboarding is web-wizard-only, so Android needs nothing for Phase 1. Verify the Tauri/WebView wizard renders the new card (it wraps the Portal): open desktop app → onboarding → ElevenLabs card present. Record result in the three-surface checklist (Task 35). No commit (verification only).

---

# Phase 2 — Streaming STT: third `/ws/stt` provider

## Task 7: Pure message mapper + accumulator arm

**Files:**
- Modify: `Orchestrator/stt/streaming.py` — add `map_elevenlabs_message()` + `InterimAccumulator.elevenlabs()`
- Test: `Orchestrator/tests/test_stt_accumulator.py` (extend — existing file, follow its style)

**Doc-verify:** `https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime.md` — message names below per June-2026 research: receive `session_started` / `partial_transcript` / `committed_transcript`(`_with_timestamps`).

**Step 1: Failing tests:**

```python
def test_elevenlabs_partial_is_cumulative_passthrough():
    acc = InterimAccumulator()
    assert acc.elevenlabs({"message_type": "partial_transcript", "text": "hello"}) == \
        {"type": "stt_delta", "text": "hello"}
    assert acc.elevenlabs({"message_type": "partial_transcript", "text": "hello world"}) == \
        {"type": "stt_delta", "text": "hello world"}

def test_elevenlabs_committed_emits_final_and_resets():
    acc = InterimAccumulator()
    acc.elevenlabs({"message_type": "partial_transcript", "text": "hello"})
    assert acc.elevenlabs({"message_type": "committed_transcript", "text": "hello world."}) == \
        {"type": "stt_final", "text": "hello world."}
    assert acc.elevenlabs({"message_type": "partial_transcript", "text": "next"})["text"] == "next"

def test_elevenlabs_ignores_session_and_unknown_messages():
    acc = InterimAccumulator()
    assert acc.elevenlabs({"message_type": "session_started"}) is None
```

**Step 3: Implement** — Scribe partials are cumulative (Google-like, `streaming.py:27` docstring): partial → `{"type":"stt_delta","text":...}` buffer-replace; committed → `{"type":"stt_final"}` + reset; everything else → `None`.

**Step 5:** commit `feat(stt): ElevenLabs Scribe realtime event mapper + accumulator arm`.

## Task 8: WebSocket bridge

**Files:**
- Modify: `Orchestrator/routes/stt_ws_routes.py` — add `_elevenlabs_bridge()` + dispatch branch in `run_stt_bridge()` (`stt_ws_routes.py:92`)

**Implementation contract** (mirror `_openai_bridge`, `stt_ws_routes.py:107` — sender task + receiver loop, asyncio decoupled so the client pump never blocks):
- Connect `websockets.connect(f"{WS_BASE_URL}/v1/speech-to-text/realtime?model_id=scribe_v2_realtime&commit_strategy=manual&sample_rate={sr}", additional_headers=auth_headers())` (+ `language_code` if client sent `lang`; keyterms wired but empty for now). **(Live-proven during execution: `commit_strategy=manual` gives cumulative partials + exactly one final on stt_stop — parity with openai/google push-to-talk. `additional_headers` is the websockets 15.x kwarg; `extra_headers` was removed in 14.0. Send `commit:true` on stt_stop to flush the tail.)**
- Up-pump: client binary PCM frames → `{"message_type":"input_audio_chunk","audio_base_64":b64,"sample_rate":sr,"commit":false}`.
- Down-pump: each provider JSON → `acc.elevenlabs(msg)` → forward non-None to client (the uniform `stt_delta`/`stt_final` contract).
- Provider `error`-class messages → `map_error()` → `{"type":"stt_error","message":...}`, close.
- `session_time_limit_exceeded` → transparent reconnect-and-resume (same approach as the existing long-session handling).

**Test:** unit-test the up-pump frame builder + down-pump filter as small pure helpers (extract `_el_audio_msg(b, sr)` and reuse Task 7 mapper tests); the socket loop itself is covered by the live smoke (Appendix A2). Run full `test_stt_ws.py` to confirm no regression.

**Step 5:** commit `feat(stt): ElevenLabs realtime bridge for /ws/stt`.

## Task 9: Availability, resolve, catalog

**Files:**
- Modify: `Orchestrator/stt/resolve.py` — `stt_availability()` returns a third flag from `elevenlabs.client.resolve_api_key()`; `resolve_stt_provider()` accepts `"elevenlabs"`.
- Modify: `Orchestrator/stt/catalog.py` — third entry:
  ```python
  {"id": "elevenlabs", "label": "ElevenLabs", "available": el_ok,
   "blurb": "Scribe v2 realtime streaming (~150ms) + Scribe v2 files with speaker diarization. Uses your ElevenLabs API key.",
   "models": {"streaming": config.ELEVENLABS_STT_STREAM_MODEL, "file": config.ELEVENLABS_STT_FILE_MODEL}}
  ```
- Modify: `Orchestrator/config.py` — add OUR default choices: `ELEVENLABS_STT_STREAM_MODEL = "scribe_v2_realtime"`, `ELEVENLABS_STT_FILE_MODEL = "scribe_v2"` (env-overridable like the other `STT_*` consts).
- Test: `Orchestrator/tests/test_stt_catalog.py` — extend: 3 providers; `available` follows key presence; shape additive (existing assertions untouched = contract proof).

**Step 5:** commit `feat(stt): ElevenLabs in stt catalog + provider resolution`.

## Task 10: Frontend pickup (three surfaces)

- **Portal:** STT provider picker is catalog-driven — verify the third provider renders and streams (mic button, watch `stt_delta` flow). Bump `?v=genuiXX`.
- **Android:** run `TtsVoiceParseTest`-equivalent for STT if present; verify `SttStreamClient.kt` + `SettingsSheet.kt` tolerate a third catalog entry (they render from `/stt/catalog`; expected: zero code change. If the picker hardcodes two rows, fix to iterate the list). Build: `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal" && ./gradlew testDebugUnitTest`.
- **WebView:** desktop app dictation mic with provider=elevenlabs (mic grant already fixed via enable_media_stream — regression-check only).

**Commit:** any Kotlin/JS fixes as `fix(stt): render N catalog providers on <surface>`.

---

# Phase 3 — Scribe batch + diarization touchpoint sweep (EXTRA CARE)

> Brandon: diarization (32 speakers) "may become a very very good cool tool... take extra care to fully implement this one correctly throughout everywhere in the black box that it touches."

## Task 11: Batch transcribe + normalizer

**Files:**
- Create: `Orchestrator/elevenlabs/stt.py`
- Create: `Orchestrator/tests/fixtures/elevenlabs_scribe_diarized.json` (recorded/hand-built response: 2+ speakers, word timestamps, one audio_event)
- Test: `Orchestrator/tests/test_elevenlabs_stt.py`

**Doc-verify:** `https://elevenlabs.io/docs/api-reference/speech-to-text/convert.md` — multipart `file`, `model_id=scribe_v2`, `diarize=true`, word timestamps, audio-event tagging, entity detection field names.

**Step 1: Failing tests** — feed fixture into `normalize_transcript()`; assert output shape:

```python
{
  "text": "...",                       # flat transcript (back-compat — existing consumers keep working)
  "language": "en",
  "provider": "elevenlabs",
  "segments": [                         # NEW: speaker-attributed segments
    {"speaker": "speaker_0", "start": 0.0, "end": 4.2, "text": "..."},
  ],
  "speakers": ["speaker_0", "speaker_1"],
  "events": [{"type": "laughter", "start": 9.1}],
  "entities": [...],
}
```
Also: `format_diarized(normalized)` → human-readable block (`[00:00] Speaker 1: ...`) used by the tool and snapshot paths.

**Step 3: Implement** `transcribe_file(path, *, diarize=True, language=None) -> dict` (requests multipart POST, `map_error` on failure) + the two pure functions.

**Step 5:** commit `feat(elevenlabs): Scribe v2 batch transcription with diarization normalizer`.

## Task 12: `/stt` + `/stt/json` provider routing

**Files:**
- Modify: `Orchestrator/routes/tts_routes.py:309` (`async def stt`) and `:329` (`stt_json`) — accept optional `provider` form/body field; `provider=="elevenlabs"` → `elevenlabs.stt.transcribe_file`; response carries the normalized dict (flat `text` field preserved at top level so existing clients parse unchanged).
- Test: `Orchestrator/tests/test_stt_routes_elevenlabs.py` — TestClient + monkeypatched transcribe; assert back-compat top-level `text` + new `segments` present; assert default provider unchanged when param absent.

**Step 5:** commit `feat(stt): provider=elevenlabs routing on /stt + /stt/json with diarized payload`.

## Task 13: `speech_to_text` tool upgrade (diarization-aware)

**Files:**
- Modify: `ToolVault/tools/speech_to_text/schema.json` — description: "Transcribe audio to text. provider='elevenlabs' adds speaker diarization (up to 32 speakers), word timestamps, and audio-event tags — use it for meetings/calls/interviews."; add params `provider` (enum: openai/google/elevenlabs), `diarize` (boolean, default true for elevenlabs).
- Modify: `ToolVault/tools/speech_to_text/executor.py` — pass provider through; when diarized, return `format_diarized()` text + attach normalized dict to `ToolResult.data`.
- Validate + reload: `python -m Orchestrator.toolvault.validate && curl -X POST http://localhost:9091/toolvault/reload`.
- Test: agent-level: "transcribe this recording — who said what?" with a 2-speaker fixture file.

**Step 5:** commit `feat(toolvault): speech_to_text diarization via ElevenLabs provider`.

## Task 14: Snapshot-minting touchpoint — diarized transcripts as memory

**Files:**
- Modify: `ToolVault/tools/speech_to_text/executor.py` — add `mint` param (boolean, default false): when true, POST the `format_diarized()` block to `/chat/save` (auto-mint, `turns_threshold=1`) with operator from `ctx.operator` — the transcript becomes a searchable, speaker-attributed snapshot. Follow `.claude/commands/snapshot-dev.md` mechanics: `/chat/save` direct persistence, NEVER a manual `/mint` after.
- Test: executor unit test with mocked `/chat/save`; assert payload contains speaker labels and NO second mint call.

**Step 5:** commit `feat(toolvault): mint diarized transcripts as snapshots`.

## Task 15: Remaining touchpoints — session uploads + phone recordings (exploratory)

- **Session uploads:** verify the chat attachment flow (`Portal/uploads/sessions/{id}/` per CLAUDE.md) → `speech_to_text(provider="elevenlabs")` path works end-to-end with an attached meeting recording. Expected: no code, just verification + a line in the tool description if needed.
- **Phone recordings:** locate call-recording artifacts (`grep -rn "record" Orchestrator/phone/ Orchestrator/routes/asterisk_routes.py Orchestrator/routes/cellular_routes.py`); if recordings exist on disk, add a follow-up note (do NOT build speculative integration — YAGNI; the capability is reachable via `speech_to_text` on the file path). Document findings in the design doc's future-work appendix.

**Commit:** docs-only if no code: `docs(elevenlabs): diarization touchpoint sweep findings`.

### Task 15 — EXECUTION FINDINGS (2026-06-13)

Touchpoint sweep complete. Status of every place transcription flows in the BlackBox:

| Touchpoint | Status | Notes |
|---|---|---|
| `POST /stt` (file upload) | ✅ wired | `provider=elevenlabs&diarize=true` → rich diarized payload (`segments`/`speakers`/`diarized_text`); flat `{"text"}` preserved for back-compat (Task 12). |
| `POST /stt/json` (base64 PCM) | ✅ wired | `provider` passthrough (flat text); diarization intentionally omitted — it serves Gemini Live quick transcription (YAGNI). |
| `speech_to_text` tool | ✅ wired | `provider`/`diarize`/`mint` params; diarized response returns speaker-attributed text + rich `data` (Tasks 13-14). |
| **Snapshot minting** | ✅ wired + **proven** | `mint=true` → `/chat/save` auto-mint. Live: a diarized meeting transcript minted as `SNAP-20260613-7029` (3072-dim embedding) and **surfaces in semantic search** for "quarterly revenue churn meeting review". Diarized transcripts ARE now speaker-attributed searchable memory. |
| Session-upload audio attachments | ✅ works, no code | The tool takes any `audio_path`, including `Portal/uploads/sessions/{id}/…`. Attach a recording in chat → "transcribe this — who said what?" works via `provider=elevenlabs`. |
| Phone-call recordings | ⏸️ deferred (no infra yet) | Grep confirmed **no call-audio recording mechanism exists today** — the "record" hits in `asterisk_routes.py` are data-records, not MixMonitor. When call recording lands, diarized call logs are reachable by pointing `speech_to_text` at the recording file path. No speculative build (YAGNI). → future-work. |

---

# Phase 4 — TTS: synthesis, hybrid catalog, selector (3 surfaces)

## Task 16: TTS synthesis module (quality-first)

**Files:**
- Create: `Orchestrator/elevenlabs/tts.py`
- Test: `Orchestrator/tests/test_elevenlabs_tts.py`

**Doc-verify:** `https://elevenlabs.io/docs/api-reference/text-to-speech/convert.md`.

**Implement** `synthesize(text, voice_id, *, model_id=None, output_format=None, voice_settings=None) -> bytes`:
- Defaults from config (Task 9 pattern): `ELEVENLABS_TTS_MODEL_DEFAULT = "eleven_v3"` — **quality-first; flash is an explicit caller choice, never silent.**
- `output_format` default `"mp3_44100_192"`; on a 4xx rejecting the format (free-tier gate), retry once with `"mp3_44100_128"` and `print("[ELEVENLABS] output format downgraded to 128kbps (plan tier)")` — visible, not silent.
- POST `/v1/text-to-speech/{raw_voice_id}?output_format=...` body `{"text", "model_id", "voice_settings"}`.
- Long text: callers chunk via the existing `chunk_text_for_tts` (`tts_routes.py:939`) + `/tts/stitch` — premium models carry ~5–10k char limits; read the live limit from `catalog.get_models()` (SoT) and expose `max_chars_for(model_id)`.

**Tests:** mocked requests — default model is `eleven_v3`; format downgrade path logs + retries once; `elevenlabs:` prefix stripped exactly once.
**Commit:** `feat(elevenlabs): TTS synthesis - quality-first defaults, tier-aware format`.

## Task 17: Hybrid `/tts/catalog` merge

**Files:**
- Modify: `Orchestrator/routes/tts_routes.py:791` (`tts_catalog`) — static groups from `build_tts_catalog()` + dynamic append when key configured:
  ```python
  groups = build_tts_catalog()
  from Orchestrator.elevenlabs import catalog as el_catalog
  v = el_catalog.get_voices()
  if v:
      voices = ([dict(x, name=f"⭐ {x['name']}") for x in v["my_voices"]] + v["premade"])
      groups.append({"id": "elevenlabs", "label": "ElevenLabs", "dynamic": True, "voices": voices})
  return {"groups": groups}
  ```
- Test: `Orchestrator/tests/test_tts_catalog_elevenlabs.py` — no key → exactly the original 3 groups (regression guard); with mocked voices → 4th group, My Voices first, ids `elevenlabs:`-prefixed, additive fields only.

**Commit:** `feat(tts): hybrid catalog - live ElevenLabs group merged into /tts/catalog`.

## Task 18: `/tts` + `/tts/batch` routing

**Files:**
- Modify: `Orchestrator/routes/tts_routes.py:113` (`tts_openai` — the main `/tts` route): when requested voice starts `"elevenlabs:"` → `elevenlabs.tts.synthesize`. Modify `:171` (`tts_batch`): accept `provider="elevenlabs"`, chunk per `max_chars_for(model)`, reuse stitch path.
- Modify: `ToolVault/tools/text_to_speech/schema.json` + `list_tts_voices` description — mention ElevenLabs voices exist and are provider-explicit (`elevenlabs:` ids). Validate + reload.
- Test: TestClient with mocked synthesize — `elevenlabs:` routes correctly; unknown prefix error message lists elevenlabs.

**Commit:** `feat(tts): route elevenlabs: voices through /tts and /tts/batch`.

## Task 19: Portal selector + library browse modal

**Files:**
- Modify: the Portal voice-picker module (locate: `grep -rn "tts/catalog" Portal/*.js Portal/**/*.js`) — render `dynamic` group with subgroup headers (My Voices ⭐ / Premade) + final entry `🔍 Browse voice library…`.
- Create: `Portal/voice-library.js` + `Portal/styles/features/_voice-library.css` — modal: search box → `GET /elevenlabs/library?search=` (thin backend proxy added in this task to keep the key server-side: `elevenlabs_routes.py`, GET shared-voices passthrough), result cards with `▶` preview (`preview_url` `<audio>`; reuse the audio-overlap-prevention pattern from CLAUDE.md), "+ Add to my account" → backend add → `bust_voices_cache()` → toast → selector refreshes. Use design tokens (`Portal/styles/_variables.css`); toast via `import { toastSuccess, toastError } from './core-utils.js'`.
- Modify: `Portal/index.html` `?v=genuiXX` bump.

**Doc-verify:** shared-library endpoints (`GET /v1/shared-voices`, add-to-account) before coding the proxy.
**Test:** manual web pass + screenshot for Brandon (pixel placement per his preference).
**Commit:** `feat(portal): ElevenLabs voice group + library browse modal`.

## Task 20: Android selector (Compose)

**Files (under `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal/`):**
- Modify: `app/src/main/java/com/aiblackbox/portal/data/repository/TtsRepository.kt` — confirm parser tolerates the 4th group + extra fields (`dynamic`, `preview_url`, `category`); extend the voice model with optional fields.
- Modify: `app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt` — subgroup headers for the ElevenLabs group (My Voices ⭐ above Premade).
- Test: extend `app/src/test/java/com/aiblackbox/portal/data/repository/TtsVoiceParseTest.kt` with a 4-group fixture including the new fields → parse green. `./gradlew testDebugUnitTest`.
- Library-browse on Android: v1 = defer to a "Manage voices in the Portal" link in the sheet (YAGNI: full Compose browse sheet is future-work; record in appendix).

**Commit:** `feat(android): ElevenLabs voice group in selector + parse tolerance`.

---

# Phase 5 — Voice Lab + cloning/design agent tools

## Task 21: Voices capability module

**Files:**
- Create: `Orchestrator/elevenlabs/voices.py`
- Test: `Orchestrator/tests/test_elevenlabs_voices.py` (mocked HTTP throughout)

**Doc-verify:** `https://elevenlabs.io/docs/api-reference/voices/ivc/create.md`, `.../text-to-voice/design.md`.

**API:**
```python
def clone_instant(name, file_paths, *, description=None, labels=None, remove_background_noise=True) -> dict
    # POST /v1/voices/add multipart  -> {"voice_id", "requires_verification"}; bust_voices_cache()
def design_previews(voice_description, *, text=None, model_id="eleven_ttv_v3") -> list
    # POST /v1/text-to-voice/design -> [{"generated_voice_id", "audio_path"(saved to media_files), "duration"}]
def design_save(generated_voice_id, name, description) -> dict   # POST /v1/text-to-voice; bust cache
def delete_voice(voice_id) -> dict                               # DELETE /v1/voices/{id}; bust cache
def voice_in_use(voice_id) -> list                               # scan operator TTS preferences for elevenlabs:{id}
```
**Tests:** multipart fields correct; cache bust called on every mutator; previews decoded from base64 → files under `media_files/`; `voice_in_use` finds a planted preference.
**Commit:** `feat(elevenlabs): voices module - IVC, design previews/save, delete with in-use check`.

## Task 22: Voice Lab backend routes

**Files:**
- Modify: `Orchestrator/routes/elevenlabs_routes.py` — add:
  - `POST /elevenlabs/voices/clone` (multipart: name + files + consent flag — **422 if `consent != "true"`**)
  - `POST /elevenlabs/voices/design` / `POST /elevenlabs/voices/design/save`
  - `GET /elevenlabs/voices` (grouped, from catalog), `DELETE /elevenlabs/voices/{voice_id}` (response includes `in_use` warnings)
  - `GET /elevenlabs/library?search=` + `POST /elevenlabs/library/add` (proxies from Task 19)
- Test: TestClient + monkeypatched voices module: consent missing → 422; delete returns in-use list; design save busts cache.

**Commit:** `feat(elevenlabs): Voice Lab routes - clone/design/manage with consent gate`.

## Task 23: ToolVault modules — cloning/design/list/delete

**Files (4 new module folders under `ToolVault/tools/` — follow `ToolVault/tools/ADDING_A_TOOL.md`, worked example `roll_dice/`):**
- `elevenlabs_clone_voice/` — params: `name` (req), `audio_paths` (req, array — session-upload paths), `confirm_consent` (req, boolean; description: "Must ask the user to confirm they have rights to this voice before calling. Refuse without explicit consent."), `description`, `labels`. Executor: consent false → `ToolResult(False, "consent not confirmed")`; else `voices.clone_instant` → "Voice '<name>' cloned — it's in your selector now (elevenlabs:<id>)."
- `elevenlabs_design_voice/` — two-step: without `generated_voice_id` → previews saved to media_files, returns the three with playback paths; with `generated_voice_id` + `name` → saves permanently.
- `elevenlabs_list_voices/`, `elevenlabs_delete_voice/` (delete surfaces in-use warnings, requires `confirm` boolean).
- Groups for all: `["chat", "chat_cu", "mcp"]`; category `"audio"`; tier 2.
- Validate + reload; restart for frozen arrays. Agent-level test: attach recording → "clone this as TestVoice" → appears in `/tts/catalog`.

**Commit:** `feat(toolvault): elevenlabs voice tools - clone/design/list/delete with consent gate`.

## Task 24: Portal Voice Lab panel

**Files:**
- Create: `Portal/voice-lab.js` + `Portal/styles/features/_voice-lab.css`; register the panel in the Portal nav (follow the cron-manager.js module registration pattern).
- Three zones per design doc: **Clone** (MediaRecorder mic capture ≥60s with level meter — reuse getUserMedia path from STT mic; or file upload; consent checkbox gates the submit), **Design** (description → 3 preview cards `<audio>` → name + Save), **My Voices** (list/preview/delete with in-use warning dialog).
- Hydrate from `/elevenlabs/status`: free tier → upgrade explainer replaces Clone submit; no key → panel hidden entirely.
- `?v=genuiXX` bump. Screenshot for Brandon before polish iterations.

**Commit:** `feat(portal): Voice Lab panel - record/upload clone, design previews, manage`.

## Task 25: Android Voice Lab screen

**Files (under the Android app):**
- Create: `app/src/main/java/com/aiblackbox/portal/ui/voicelab/VoiceLabScreen.kt` + `VoiceLabViewModel.kt`; nav entry in `navigation/NavGraph.kt`.
- Mic capture: reuse the existing `AudioRecord` stack — **release in the read-loop's `finally`, stop() only signals** (the documented SIGABRT race fix, `feedback_android_audiorecord_race`); WAV-wrap PCM; multipart to `/elevenlabs/voices/clone`.
- Design + My Voices zones mirror Portal; all logic stays backend-side.
- `./gradlew testDebugUnitTest` green; manual device pass.

**Commit:** `feat(android): Voice Lab screen - clone via mic/upload, design, manage`.

## Task 26: Instant-appearance verification

End-to-end: clone in Voice Lab → `/tts/catalog` shows the voice (cache busted, no restart) → selector shows it on web + Android → TTS with `elevenlabs:<new_id>` speaks. Fix anything stale. Commit fixes only.

---

# Phase 6 — Music, SFX, audio utilities + Lyria rename

## Task 27: Music backend (task pattern)

**Files:**
- Create: `Orchestrator/elevenlabs/music.py` — `compose(prompt=None, composition_plan=None, music_length_ms=None, force_instrumental=False, seed=None) -> bytes` (POST `/v1/music`, binary response; enforce prompt XOR plan).
- Modify: `Orchestrator/models.py:206` area — `ELEVENLABS_MUSIC = "elevenlabs_music"` TaskType.
- Modify: `Orchestrator/tasks.py:279` area — dispatch + `process_elevenlabs_music(task)` (mirror `process_lyria_music` at `tasks.py:880`: call module, save to `media_files/`, attach URLs).
- Modify: `Orchestrator/routes/elevenlabs_routes.py` — `POST /generate/elevenlabs_music` returning `task_id` (mirror `/generate/lyria_music`, `tts_routes.py:1416`).
- Test: `Orchestrator/tests/test_elevenlabs_music.py` — XOR validation; payload shape incl. composition_plan passthrough; task processor saves bytes (mocked).

**Commit:** `feat(elevenlabs): music compose - prompt or composition_plan, task pattern`.

## Task 28: `elevenlabs_music` tool

**Files:**
- Create: `ToolVault/tools/elevenlabs_music/` — description: "Generate full songs UP TO 5 MINUTES with vocals and lyrics using ElevenLabs Music (commercially cleared). Accepts a natural-language prompt (any genre/style vocabulary — unlike lyria_music there is NO restricted vocabulary) plus optional music_length_ms (3000–300000), force_instrumental, or a full composition_plan with sections and lyric lines for produced work."
- Params: `prompt`, `music_length_ms`, `force_instrumental`, `composition_plan` (object). Executor → `/generate/elevenlabs_music` → task_id + "use get_task_status".
- Validate + reload + restart (frozen arrays). Agent test: "make a 1-minute synthwave track with vocals about flight recorders."

**Commit:** `feat(toolvault): elevenlabs_music tool - 5-min songs with vocals`.

## Task 29: Lyria rename migration (`generate_music` → `lyria_music`)

> Provider-explicit naming directive. Blast radius is KNOWN — do these in one sitting:

**Files:**
- Rename: `git mv ToolVault/tools/generate_music ToolVault/tools/lyria_music`; edit `schema.json` `"name": "lyria_music"`, description gains "(Google Lyria-002)".
- Sweep references: `grep -rn "generate_music" Orchestrator/ ToolVault/ Portal/ --include="*.py" --include="*.js" --include="*.json" -l` — expected hits: chat injector frozen arrays (`BLACKBOX_TOOLS_*`/`CHAT_TOOLS_*` in chat_routes.py — three formats: Anthropic `input_schema`, OpenAI `parameters`, Gemini `parameters`), `Orchestrator/phone/bridge.py` `unified_tool_map`, MCP server tool list, CLAUDE.md docs. Update every one.
- Validate + reload + **restart** (frozen arrays only refresh on restart).
- Parity check: before/after `curl http://localhost:9091/toolvault/health`; agent smoke "make some lo-fi" → `lyria_music` fires; confirm `elevenlabs_music` and `lyria_music` both callable in one chat.
- Note in commit body: `generate_image`/`generate_video` rename sweep deferred to future-work.

**Commit:** `refactor(toolvault): rename generate_music -> lyria_music (provider-explicit naming)`.

## Task 30: Sound effects

**Files:**
- Create: `Orchestrator/elevenlabs/sfx.py` — `generate(text, duration_seconds=None, prompt_influence=None, loop=False) -> bytes` (POST `/v1/text-to-sound-effects`; WAV 48k for non-looping).
- Route: `POST /generate/elevenlabs_sound_effect` (sync — generations are seconds; save to media_files, return URL directly, no task).
- Create: `ToolVault/tools/elevenlabs_sound_effects/` — params `text` (req), `duration_seconds` (0.1–30), `loop` (boolean — "seamless loop for ambience: rain, engine hum"), `prompt_influence`. Description mentions production-app use.
- Tests: payload shape; duration bounds clamp. Validate + reload.

**Commit:** `feat(elevenlabs): sound effects - route + tool with looping`.

## Task 31: Voice changer + isolator

**Files:**
- Create: `Orchestrator/elevenlabs/transform.py` — `change_voice(audio_path, target_voice_id) -> bytes` (POST `/v1/speech-to-speech/{voice_id}` multipart), `isolate(audio_path) -> bytes` (POST `/v1/audio-isolation`).
- Routes: `POST /elevenlabs/voice-changer`, `POST /elevenlabs/isolate` (multipart or `{path}` for session files; output → media_files, return URL).
- Tools: `ToolVault/tools/elevenlabs_voice_changer/` (params `audio_path`, `target_voice` — accepts `elevenlabs:` id or voice name resolved via catalog), `ToolVault/tools/elevenlabs_isolate_voice/` (param `audio_path`; description notes it is also the pre-clean step before cloning noisy samples).
- Wire isolator pre-clean: in `voices.clone_instant`, `remove_background_noise=True` already covers it server-side — tool description points users at the native flag first (no double-processing).
- Tests: multipart construction; tools return media URLs. Validate + reload.

**Commit:** `feat(elevenlabs): voice changer + isolator - routes and tools`.

---

# Phase 7 — Phone path (μ-law) + wrap-up

## Task 32: μ-law TTS for telephony

**Files:**
- Modify: `Orchestrator/elevenlabs/tts.py` — accept `output_format="ulaw_8000"` passthrough.
- Exploratory first (timebox): `grep -rn "tts\|say\|announce" Orchestrator/phone/bridge.py Orchestrator/routes/asterisk_routes.py Orchestrator/routes/twilio_routes.py | head -40` — find where announcement/recap-call audio is synthesized today. Add `elevenlabs` as a selectable announcement voice provider THERE (config choice `PHONE_TTS_PROVIDER`), emitting μ-law 8k directly (no transcoding). Realtime conversational calls stay on `openai_realtime` (future-work).
- Test: unit for format passthrough; live: recap call to Brandon's phone with an ElevenLabs voice.

**Commit:** `feat(phone): ElevenLabs ulaw TTS option for announcements/recap calls`.

## Task 33: Docs + CLAUDE.md

- Update `CLAUDE.md`: multimodal section gains ElevenLabs capabilities (music/SFX/clone/design/changer/isolator + diarized STT); tool names provider-explicit; note `lyria_music` rename.
- Update `VOICE_REFERENCE.md` (referenced in CLAUDE.md): ElevenLabs dynamic voices — "voices come from /tts/catalog, never hardcode."
- Commit: `docs: ElevenLabs integration - CLAUDE.md + voice reference`.

## Task 34: Live smoke suite (Appendix A) + `/snapshot-dev`

Run every Appendix A script against the live service with the real key; record outputs. Then mint the dev snapshot (resolve operator dynamically — `get_current_operator`; `/chat/save` auto-mint; verify `[EMBEDDING] Successfully generated embedding` in journalctl).

## Task 35: Three-surface checklist (gate before merge talk)

| Feature | Portal web | Android | WebView (Tauri) |
|---|---|---|---|
| Onboarding card | ☐ | n/a | ☐ |
| STT provider picker + streaming | ☐ | ☐ | ☐ |
| Diarized transcribe via chat attachment | ☐ | ☐ | ☐ |
| Voice selector EL group | ☐ | ☐ | ☐ |
| Library browse | ☐ | link-out | ☐ |
| Voice Lab clone/design/manage | ☐ | ☐ | ☐ |
| Music/SFX tools from chat | ☐ | ☐ | ☐ |

Finish with superpowers:requesting-code-review → superpowers:finishing-a-development-branch (merge/PR decision is Brandon's).

---

# Appendix A — Live smoke scripts (manual, real key)

```bash
# A1: key + status
curl -s -X POST http://localhost:9091/onboarding/validate -H 'Content-Type: application/json' \
  -d '{"provider":"elevenlabs","credentials":{"api_key":"'$ELEVENLABS_API_KEY'"}}'
curl -s http://localhost:9091/elevenlabs/status | python3 -m json.tool

# A2: streaming STT — Portal mic with provider=elevenlabs; watch:
journalctl -u blackbox.service -f | grep "STT/WS"

# A3: diarized batch
curl -s -F file=@Orchestrator/tests/fixtures/two_speakers.wav -F provider=elevenlabs \
  http://localhost:9091/stt | python3 -m json.tool   # expect segments[] with speaker labels

# A4: TTS quality-first
curl -s http://localhost:9091/tts/catalog | python3 -c "import json,sys; print([g['label'] for g in json.load(sys.stdin)['groups']])"
# expect [... 'ElevenLabs']; then synthesize one premade + one cloned voice via /tts

# A5: music + SFX
curl -s -X POST http://localhost:9091/generate/elevenlabs_music \
  -H 'Content-Type: application/json' -d '{"prompt":"upbeat synthwave with vocals about flying","music_length_ms":60000,"operator":"system"}'
# poll get_task_status; play result. SFX: 5s rain loop via elevenlabs_sound_effects.

# A6: clone (consented) -> instant selector appearance -> speak with it
```

# Appendix B — Future-work (carried from design doc)

Dubbing, forced alignment, ElevenAgents platform, Reception AI, image/video gen, voice remixing, full PVC flow, Audio Native, cascaded realtime voice agents, Android library-browse sheet, keyterm biasing fed from operator/BlackBox vocabulary, `generate_image`/`generate_video` provider-explicit renames, phone-call live transcription via Scribe μ-law input.
