# Local Audio — Streaming STT + Batch STT + Kokoro TTS Implementation Plan

> **For Claude:** superpowers:subagent-driven-development. Work on `main` (staging-box-as-production). Extends the auto-ingest framework (`docs/plans/2026-07-12-auto-ingest-local-capabilities.md`).

**Goal:** Make a registered custom server's **local audio** (Speaches on the gemma box, `:8080/v1`) fully usable in the BlackBox: **realtime streaming STT** (the primary ask), batch STT, and **Kokoro TTS (~50 voices)** — selectable in the wizard and routed by `provider=local`.

**The core problem (why this is new work):** the box fronts Speaches behind Caddy, so the audio models are served at `/v1/audio/*` + `/v1/realtime` but are **NOT in `/v1/models`** (which Caddy routes to llama-swap). Name-classification auto-detect can't see them. So audio needs **endpoint-capability probing** + a **per-server audio config** (capabilities + model ids), not the `/v1/models` modality map.

**Validated decisions (Brandon):** (1) audio model ids = **wizard field prefilled with Speaches defaults**, editable (portable to any local audio server); (2) scope = **full local audio** (streaming + batch STT + TTS).

**Live-proven facts (2026-07-13, from this box):**
- Batch STT: `POST :8080/v1/audio/transcriptions model=deepdml/faster-whisper-large-v3-turbo-ct2` → 200, exact transcript.
- TTS: `POST :8080/v1/audio/speech model=speaches-ai/Kokoro-82M-v1.0-ONNX voice=af_heart` → 200 MP3.
- Streaming: `ws://:8080/v1/realtime?model=<STT>&intent=transcription&api_key=<key>` → PCM16@24k append → server-VAD → `conversation.item.input_audio_transcription.completed.transcript` (per-utterance final). Trailing silence (or explicit commit) triggers the final.
- `/v1/audio/*` GET → **405** (path exists); `/v1/models` → chat/image only.

**Defaults:** `SPEACHES_STT_DEFAULT = "deepdml/faster-whisper-large-v3-turbo-ct2"`, `SPEACHES_TTS_DEFAULT = "speaches-ai/Kokoro-82M-v1.0-ONNX"`. Realtime audio = PCM16 mono 24000 Hz.

---

## Phase 1 — Per-server audio config + resolvers (`custom_servers.py`)

**Registry field** (per server): `"audio": {"stt": bool, "tts": bool, "streaming": bool, "stt_model": str, "tts_model": str}`. Add to `add_server` (default `{}`), `_PATCHABLE_FIELDS`, `_validate_field_types` (dict with bool/str fields).

**Resolvers:**
```python
SPEACHES_STT_DEFAULT = "deepdml/faster-whisper-large-v3-turbo-ct2"
SPEACHES_TTS_DEFAULT = "speaches-ai/Kokoro-82M-v1.0-ONNX"

def resolve_audio(kind: str):
    """kind in {'stt','tts','streaming'}. Return (server, model_id) for the first
    enabled server whose audio config advertises `kind`, or None. model_id from
    audio.stt_model/tts_model (streaming uses stt_model)."""
    mkey = "tts_model" if kind == "tts" else "stt_model"
    dflt = SPEACHES_TTS_DEFAULT if kind == "tts" else SPEACHES_STT_DEFAULT
    for srv in list_servers(enabled_only=True):
        au = srv.get("audio") or {}
        if au.get(kind):
            return srv, (au.get(mkey) or dflt)
    return None

def has_audio(kind: str) -> bool:
    try:
        return resolve_audio(kind) is not None
    except Exception:
        return False
```
Tests: `test_local_audio.py` — resolve_audio picks the server with the capability + model id; defaults when model id absent; has_audio true/false.

**Commit:** `feat(audio): per-server audio config (capabilities + model ids) + resolve_audio`

---

## Phase 2 — Capability probing at `/validate` (`validators.py`, `onboarding_routes.py`)

In `validate_custom`, after `models.list()`, probe (short timeout, best-effort, fail-soft — never break validate):
```python
def _probe(path, method="GET"):  # 405/200/426/400 => exists; 404/000 => no
    import httpx
    try:
        r = httpx.request(method, f"{base_url}{path}", headers=hdr, timeout=6)
        return r.status_code not in (404,)
    except Exception:
        return False
audio = {
    "stt": _probe("/audio/transcriptions"),
    "tts": _probe("/audio/speech"),
    "streaming": _probe("/realtime"),
}
```
Return `audio` (+ default model ids) in the detail. In the `/validate` success stamp (`onboarding_routes.py`), **merge** the probed audio config under any existing (preserve wizard-set model ids): `merged_audio = {**probed, **existing_audio_bool_only}` — keep existing `stt_model`/`tts_model` if set, else the Speaches defaults. `CustomServerCreate`/`Patch` gain an optional `audio` field.

> Realtime probe: a GET to `/v1/realtime` on a WS endpoint returns non-404 (426/400/upgrade) — good enough for capability. If it 404s but `/audio/transcriptions` is 405, still set streaming=false (batch-only server).

Tests: `test_validate_audio.py` — mock httpx per path → assert the detected `audio` dict; add persists it.

**Commit:** `feat(audio): /validate probes audio endpoints -> per-server capabilities`

---

## Phase 3 — Availability / catalog rewire to the audio config

Switch the LOCAL audio detection from the `/v1/models` modality map to the audio config:
- `custom_servers.resolve_tts_server` → `resolve_audio("tts")`; add `resolve_stt_audio` = `resolve_audio("stt")`.
- `stt/resolve.py:local_stt_available()` → `has_audio("stt")`.
- `stt/catalog.py` local entry → `available` from `has_audio("stt")`; **models.streaming = "realtime" when `has_audio("streaming")`, else None** (so the wizard shows local supports streaming).
- `tts_routes.py /tts/catalog` local group → gated on `has_audio("tts")`.
- `availability._local_image_available` unchanged (image still uses the modality map).

Tests: catalog/availability reflect the audio config (mock has_audio).

**Commit:** `feat(audio): STT/TTS catalogs + availability read the audio config`

---

## Phase 4 — Batch STT re-point (`stt/file_transcribe.py`)

`_local_transcribe` uses `resolve_audio("stt")` → `(srv, stt_model)`; POST `{base_url}/audio/transcriptions` multipart `{file, model=stt_model}` + Bearer key. (Replaces `resolve_stt_server` which read last_models — now empty for Speaches.)

Test: `transcribe_bytes(provider="local")` posts to `/audio/transcriptions` with the resolved model + key.

**Commit:** `feat(audio): batch STT routes via the audio config (turbo whisper)`

---

## Phase 5 — Realtime streaming STT bridge (`stt_ws_routes.py`) — THE PRIMARY DELIVERABLE

New `_local_bridge(websocket, *, target, lang, sample_rate)` modeled on `_openai_bridge` but adapted for Speaches:
- Connect `ws://{host}/v1/realtime?model={stt_model}&intent=transcription` with `Authorization: Bearer {key}` (host+key+model from `resolve_audio("streaming")`).
- **No session.update with input_audio_format** (Speaches fixes pcm16 + errors on it). Rely on server VAD.
- `client_to_openai`-equivalent: read client `stt_audio` (base64 PCM16 @ `sample_rate`) → **resample to 24 kHz** if `sample_rate != 24000` (numpy interp) → base64 → `input_audio_buffer.append`. On `stt_stop` → `input_audio_buffer.commit` (push-to-talk cut) then signal stop.
- `openai_to_client`-equivalent: read events; on `conversation.item.input_audio_transcription.completed` → emit `{"type":"stt_final","text":transcript,"target":target}` (filter hallucinations via `is_whisper_hallucination`); box sends per-utterance finals only (no interim `stt_delta`). Surface `error` events as `stt_error`.
- Wire `run_stt_bridge`: `elif provider == "local": await _local_bridge(...)` (before the else-raise).
- `resolve_stt_provider`: allow local for streaming when a streaming server exists — replace the blanket `local_ok=False` at the WS entry with `local_ok=has_audio("streaming")` (still file-only-excluded when the box has batch STT but no `/v1/realtime`).

Tests: `test_local_stream.py` — a fake realtime WS server (or monkeypatched `websockets.connect`) that emits a `...transcription.completed` → assert `_local_bridge` relays `stt_final`; resample path unit-tested (16k→24k length); resolve allows local streaming iff has_audio("streaming").

**Commit:** `feat(audio): local realtime streaming STT bridge (/v1/realtime, VAD, 24k resample)`

---

## Phase 6 — Kokoro TTS re-point + voices (`tts_routes.py`, `custom_servers.py`)

- `tts_batch` local branch: `resolve_audio("tts")` → `(srv, tts_model)`; POST `{base_url}/audio/speech` `{model=tts_model, input, voice=<bare>, response_format}` + Bearer.
- `list_local_tts_voices`: probe `GET {base_url}/audio/voices`; **fallback to the Kokoro roster** (af_heart, af_bella, af_nova, af_sarah, am_michael, am_onyx, am_echo, bf_emma, bf_isabella, bm_george, … — the ~50 from AUDIO-PIPELINE §5) so the picker is populated even without a voices endpoint.
- `/tts/catalog` local group unchanged (already appends when `has_audio("tts")` after Phase 3).

Tests: local TTS branch posts to `/audio/speech` with tts_model + key; voices fall back to the Kokoro roster.

**Commit:** `feat(audio): Kokoro TTS via the audio config + voice roster`

---

## Phase 7 — Wizard audio-config UI (`Portal/onboarding/steps/api_keys.js`)

In the custom-server confirm block: when `/validate` returns `audio.stt`/`audio.tts`, render a compact **"Audio"** sub-section — two text inputs (STT model id, TTS model id) prefilled from the persisted config or the Speaches defaults, with a "streaming ✓/file-only" note from `audio.streaming`. On change/blur, PATCH `{audio: {...}}`. (transcription.js already lists local STT — no change there.)

Verify: `node --check`. Live wizard check in Phase 8.

**Commit:** `feat(audio): wizard shows/edits per-server STT/TTS model ids`

---

## Phase 8 — Regression + live validation + snapshot

1. Full backend suite 0 failures; ToolVault validator exit 0.
2. Restart. Re-validate gemma-box → `audio: {stt:true, tts:true, streaming:true, stt_model:…, tts_model:…}` persisted.
3. **Live round-trips (all via the BlackBox, provider=local):**
   - **Streaming STT:** open `/ws/stt`, stream the fox WAV as PCM frames → `stt_final` "the quick brown fox…".
   - **Batch STT:** `POST /stt provider=local` with a WAV → exact text.
   - **TTS:** `POST /tts provider=local voice=af_heart` → MP3 → transcribe it back via local STT to confirm.
   - `/stt/catalog` local shows streaming; `/tts/catalog` shows the Local group with Kokoro voices.
4. Final review subagent (bridge concurrency/teardown, fail-loud, no key leak, fresh-box: no server → all audio simply absent).
5. `/snapshot-dev`.

---

## Definition of Done
- [ ] Local realtime streaming STT works via `/ws/stt provider=local` (per-utterance finals).
- [ ] Batch STT + Kokoro TTS route via `provider=local` using the audio config.
- [ ] Wizard shows local STT (streaming-capable) + editable audio model ids.
- [ ] Fresh-box safe (no audio server → capabilities absent, no errors); key sent to Speaches (Bearer) on every audio call incl. the WS.
- [ ] Full regression 0 fail; live round-trips green; snapshot minted.

## Notes / gotchas
- Speaches needs the Bearer key on **every** request incl. the WS (`&api_key=` or header).
- Realtime is per-session: on WS drop, the client reconnects a fresh session (don't resume a half-committed buffer).
- Model cold-load ~2–3 s STT / ~1 s Kokoro after 10 min idle; keep client timeouts ≥120 s; a session warm-up (1 s silence) is optional.
- Don't run image gen (`z-image`) mid-voice-session (it evicts the LLM on the shared Ada).
