# Streaming STT (Multi-Provider) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task.

**Goal:** Live-streaming, multi-provider speech-to-text everywhere transcription happens (web Portal + Android MVP), selectable in onboarding (OpenAI `gpt-realtime-whisper` or Google Cloud Speech v2 `chirp_2`), plus an any-target Translate feature.

**Architecture:** A provider-adapter layer (`Orchestrator/stt/`) with config-driven model names. Clients speak one uniform WebSocket contract (`/ws/stt`); internally the OpenAI adapter bridges to an OpenAI realtime WS session and the Google adapter bridges to a Cloud Speech v2 gRPC `StreamingRecognize` stream. The file path (`POST /stt`) becomes multi-provider while keeping its `{"text":...}` response for back-compat. Capability-gated so any box runs with whichever credential is present.

**Tech Stack:** Python/FastAPI, pytest + pytest-asyncio, `requests`, `websockets`, `google-cloud-speech` (speech_v2), OpenAI Realtime API, Kotlin/Jetpack Compose, vanilla JS modules.

**Design doc:** `docs/plans/2026-06-04-streaming-stt-multiprovider-design.md`

**Context / conventions (read once):**
- Branch: `feat/streaming-stt-multiprovider` in the **main repo** (no worktree — per user). Push to GitHub only after all features are tested locally.
- Tests live in `Orchestrator/tests/test_*.py`, imported as `from Orchestrator.stt... import ...`. Run from repo root: `Orchestrator/venv/bin/pytest Orchestrator/tests/<file> -v` (pytest.ini sets `pythonpath=.`).
- Routes attach to the shared app: `from Orchestrator.checkpoint import app` then `@app.post(...)` (see `tts_routes.py:33`).
- Config constants are import-time frozen from env; capability flags mirror `USE_CLOUD_TTS = bool(creds and exists)` (`config.py:593`).
- MCP tool defs are one canonical `parameters`-shaped entry in `tool_registry.py`; converters emit all formats. MCP edits take effect next Claude Code session.
- **Production rule:** every provider path must capability-gate gracefully (missing credential → "not configured", never a crash). No hardcoded paths/keys.

---

## PHASE 0 — Backend foundation

### Task 0.1: STT config registry

**Files:**
- Modify: `Orchestrator/config.py:629` (after the existing `STT_MODEL` line)
- Test: `Orchestrator/tests/test_stt_config.py` (create)

**Step 1: Write the failing test**
```python
# Orchestrator/tests/test_stt_config.py
import importlib, Orchestrator.config as cfg

def test_stt_model_registry_defaults():
    assert cfg.STT_OPENAI_STREAM == "gpt-realtime-whisper"
    assert cfg.STT_OPENAI_FILE == "gpt-4o-transcribe"
    assert cfg.STT_GOOGLE_MODEL == "chirp_2"
    assert cfg.STT_GOOGLE_REGION == "us-central1"
    assert cfg.STT_OPENAI_DELAY in ("minimal","low","medium","high","xhigh")
    assert cfg.STT_MODEL == "whisper-1"   # legacy fallback preserved
```

**Step 2: Run — expect FAIL** (`AttributeError`)
`Orchestrator/venv/bin/pytest Orchestrator/tests/test_stt_config.py -v`

**Step 3: Implement** (append after `config.py:629`)
```python
# --- STT provider/model registry (swap a string to upgrade the model) ---
STT_PROVIDER       = os.getenv("STT_PROVIDER", "").strip().lower()   # "" = auto
STT_OPENAI_STREAM  = os.getenv("STT_OPENAI_STREAM", "gpt-realtime-whisper").strip()
STT_OPENAI_FILE    = os.getenv("STT_OPENAI_FILE",   "gpt-4o-transcribe").strip()
STT_OPENAI_DELAY   = os.getenv("STT_OPENAI_DELAY",  "low").strip()
STT_GOOGLE_MODEL   = os.getenv("STT_GOOGLE_MODEL",  "chirp_2").strip()
STT_GOOGLE_REGION  = os.getenv("STT_GOOGLE_REGION", "us-central1").strip()
STT_OPENAI_AVAILABLE = bool(OPENAI_API_KEY)
STT_GOOGLE_AVAILABLE = bool(GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(GOOGLE_APPLICATION_CREDENTIALS))
```

**Step 4: Run — expect PASS.**
**Step 5: Commit** `feat(stt): add STT provider/model config registry`

---

### Task 0.2: Provider resolution + fallback

**Files:**
- Create: `Orchestrator/stt/__init__.py` (empty), `Orchestrator/stt/resolve.py`
- Test: `Orchestrator/tests/test_stt_resolve.py`

**Step 1: Failing test**
```python
from Orchestrator.stt.resolve import resolve_stt_provider

def test_explicit_wins_when_available():
    assert resolve_stt_provider("openai", openai_ok=True, google_ok=True) == "openai"
    assert resolve_stt_provider("google", openai_ok=True, google_ok=True) == "google"

def test_single_available_auto():
    assert resolve_stt_provider("", openai_ok=True,  google_ok=False) == "openai"
    assert resolve_stt_provider("", openai_ok=False, google_ok=True)  == "google"

def test_explicit_but_unavailable_falls_back():
    assert resolve_stt_provider("google", openai_ok=True, google_ok=False) == "openai"

def test_none_available_returns_none():
    assert resolve_stt_provider("", openai_ok=False, google_ok=False) is None
```

**Step 2: Run — FAIL.**
**Step 3: Implement** `Orchestrator/stt/resolve.py`
```python
from Orchestrator import config

def resolve_stt_provider(provided=None, *, openai_ok=None, google_ok=None):
    """Return 'openai' | 'google' | None. Explicit choice wins if available;
    else the single available provider; else None. Args override config (testing)."""
    provided = (provided if provided is not None else config.STT_PROVIDER or "").strip().lower()
    openai_ok = config.STT_OPENAI_AVAILABLE if openai_ok is None else openai_ok
    google_ok = config.STT_GOOGLE_AVAILABLE if google_ok is None else google_ok
    avail = {"openai": openai_ok, "google": google_ok}
    if provided in avail and avail[provided]:
        return provided
    live = [p for p, ok in avail.items() if ok]
    return live[0] if len(live) >= 1 else None
```
(When both available and no explicit choice, prefers `openai` by dict order — document; wizard always sets an explicit choice anyway.)

**Step 4: PASS. Step 5: Commit** `feat(stt): provider resolution with credential fallback`

---

### Task 0.3: STT capabilities catalog + `GET /stt/catalog`

**Files:**
- Create: `Orchestrator/stt/catalog.py`
- Modify: `Orchestrator/routes/tts_routes.py` (after `/tts/catalog`, ~line 792)
- Test: `Orchestrator/tests/test_stt_catalog.py`

**Step 1: Failing test** (mirror `test_tts_catalog.py` style)
```python
from Orchestrator.stt.catalog import build_stt_catalog

def test_two_providers_in_order():
    assert [p["id"] for p in build_stt_catalog()] == ["openai", "google"]

def test_provider_shape():
    for p in build_stt_catalog():
        assert p["label"] and p["blurb"]
        assert "available" in p and "streaming" in p["models"] and "file" in p["models"]

def test_models_reflect_config():
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["openai"]["models"]["streaming"] == "gpt-realtime-whisper"
    assert g["google"]["models"]["streaming"] == "chirp_2"
```

**Step 2: FAIL. Step 3: Implement** `build_stt_catalog()` reading `config.STT_*` + availability flags, returning:
```python
[{"id":"openai","label":"OpenAI","available":STT_OPENAI_AVAILABLE,
  "blurb":"gpt-realtime-whisper streaming + gpt-4o-transcribe files. API key.",
  "models":{"streaming":STT_OPENAI_STREAM,"file":STT_OPENAI_FILE}},
 {"id":"google","label":"Google","available":STT_GOOGLE_AVAILABLE,
  "blurb":"Cloud Speech v2 chirp_2 streaming + files. Service-account JSON.",
  "models":{"streaming":STT_GOOGLE_MODEL,"file":STT_GOOGLE_MODEL}}]
```
Add route:
```python
@app.get("/stt/catalog")
async def stt_catalog():
    from Orchestrator.stt.catalog import build_stt_catalog
    from Orchestrator.stt.resolve import resolve_stt_provider
    return {"providers": build_stt_catalog(), "resolved": resolve_stt_provider(),
            "default": (build_stt_catalog() and resolve_stt_provider())}
```

**Step 4: PASS** (catalog test + a TestClient route test asserting 200 + keys). **Step 5: Commit** `feat(stt): /stt/catalog capabilities endpoint`

---

### Task 0.4: Unified file transcription (provider-branched), keep `{"text":...}`

**Files:**
- Create: `Orchestrator/stt/file_transcribe.py`
- Modify: `Orchestrator/routes/tts_routes.py:308-328` (`/stt`) and `:331-403` (`/stt/json`)
- Test: `Orchestrator/tests/test_stt_file_transcribe.py`

**Step 1: Failing tests** (mock upstreams; assert provider routing + `{"text":...}`)
```python
from unittest.mock import patch, MagicMock
from Orchestrator.stt import file_transcribe as ft

def test_openai_file_uses_configured_model():
    with patch.object(ft, "_openai_transcribe", return_value="hello") as m, \
         patch.object(ft, "resolve_stt_provider", return_value="openai"):
        assert ft.transcribe_bytes(b"x", "audio/wav") == "hello"
        m.assert_called_once()

def test_google_file_branch():
    with patch.object(ft, "_google_transcribe", return_value="bonjour") as m, \
         patch.object(ft, "resolve_stt_provider", return_value="google"):
        assert ft.transcribe_bytes(b"x", "audio/wav") == "bonjour"
        m.assert_called_once()

def test_no_provider_raises():
    with patch.object(ft, "resolve_stt_provider", return_value=None):
        import pytest
        with pytest.raises(RuntimeError):
            ft.transcribe_bytes(b"x", "audio/wav")
```

**Step 2: FAIL. Step 3: Implement** `transcribe_bytes(audio_bytes, content_type, *, provider=None, filename="audio.webm")`:
- `provider = provider or resolve_stt_provider()`; if None → `raise RuntimeError("no STT provider configured")`.
- `_openai_transcribe`: existing multipart POST to `OPENAI_STT_URL` with `model=STT_OPENAI_FILE` (move the logic out of the route).
- `_google_transcribe`: `google.cloud.speech_v2` `SpeechClient` (regional endpoint `f"{STT_GOOGLE_REGION}-speech.googleapis.com"`), `RecognizeRequest(recognizer=f"projects/{project}/locations/{region}/recognizers/_", config=RecognitionConfig(auto_decoding_config=..., language_codes=["en-US"], model=STT_GOOGLE_MODEL), content=audio_bytes)`; project from the SA JSON `project_id`. Return first alternative transcript.
- Refactor `/stt` and `/stt/json` to call `transcribe_bytes(...)` and keep returning `{"text": text}`.

**Step 4: PASS. Step 5: Commit** `feat(stt): multi-provider file transcription behind /stt`

---

### Task 0.5: Add `google-cloud-speech` dependency

**Files:** Modify `requirements.txt` (and any `requirements-*.txt` the service installs from); install into `Orchestrator/venv`.
**Steps:** add `google-cloud-speech>=2.27` → `Orchestrator/venv/bin/pip install google-cloud-speech` → `Orchestrator/venv/bin/python -c "from google.cloud import speech_v2; print('ok')"` (expect `ok`). Re-run Task 0.4 Google test unskipped. **Commit** `chore(stt): add google-cloud-speech dependency`.

---

### Task 0.6: Parametrize stray hardcoded `whisper-1`

**Files:** `Orchestrator/routes/twilio_routes.py:~1499`, `Orchestrator/phone/bridge.py:~2940`, `Orchestrator/routes/realtime_routes.py:~521`.
**Steps:** replace literal `'whisper-1'` with the config constant (`STT_MODEL` for telephony file paths; `STT_OPENAI_STREAM` for the realtime input-transcription config at `realtime_routes.py:521`). Add a grep test:
```python
def test_no_hardcoded_whisper1_outside_config():
    import subprocess
    out = subprocess.run(["grep","-rn","whisper-1","Orchestrator","--include=*.py"],
                         capture_output=True,text=True).stdout
    offenders = [l for l in out.splitlines() if "config.py" not in l and "test_" not in l]
    assert offenders == [], offenders
```
**Commit** `refactor(stt): read STT model from config, no hardcoded whisper-1`

---

### Task 0.7: Streaming adapters — provider event → uniform event mapping (pure)

**Files:**
- Create: `Orchestrator/stt/streaming.py`
- Test: `Orchestrator/tests/test_stt_streaming_map.py`

**Step 1: Failing tests** (map synthetic provider events → our contract)
```python
from Orchestrator.stt.streaming import map_openai_event, map_google_result

def test_openai_delta_and_final():
    assert map_openai_event({"type":"conversation.item.input_audio_transcription.delta","delta":"Hel"}) \
        == {"type":"stt_delta","text":"Hel"}
    assert map_openai_event({"type":"conversation.item.input_audio_transcription.completed","transcript":"Hello"}) \
        == {"type":"stt_final","text":"Hello"}
    assert map_openai_event({"type":"something.else"}) is None

def test_google_interim_and_final():
    assert map_google_result("Hel", is_final=False) == {"type":"stt_delta","text":"Hel"}
    assert map_google_result("Hello", is_final=True) == {"type":"stt_final","text":"Hello"}
```

**Step 2: FAIL. Step 3: Implement** the two pure mappers in `streaming.py`. **Step 4: PASS. Step 5: Commit** `feat(stt): streaming provider→client event mappers`

---

### Task 0.8: `/ws/stt` endpoint (OpenAI WS bridge + Google gRPC bridge)

**Files:**
- Modify: `Orchestrator/routes/realtime_routes.py` (or a new `Orchestrator/routes/stt_ws_routes.py` registered like the other WS routes) — add `@app.websocket("/ws/stt")`.
- Test: `Orchestrator/tests/test_stt_ws.py`

**Step 1: Failing test** (FastAPI `TestClient` websocket; mock the upstream bridge so no network)
```python
from fastapi.testclient import TestClient
from unittest.mock import patch
from Orchestrator.checkpoint import app

def test_ws_stt_streams_deltas_then_final():
    async def fake_bridge(ws, provider, lang):
        await ws.send_json({"type":"stt_delta","text":"Hel","target":"prompt"})
        await ws.send_json({"type":"stt_final","text":"Hello","target":"prompt"})
    with patch("Orchestrator.routes.stt_ws_routes.run_stt_bridge", fake_bridge):
        with TestClient(app).websocket_connect("/ws/stt") as ws:
            ws.send_json({"type":"stt_start","target":"prompt","provider":"openai"})
            assert ws.receive_json()["type"] == "stt_delta"
            assert ws.receive_json()["type"] == "stt_final"
```

**Step 2: FAIL. Step 3: Implement** `/ws/stt`:
- On `stt_start`: resolve provider; dispatch to `run_stt_bridge(ws, provider, lang)`.
- **OpenAI bridge:** open `websockets.connect` to OpenAI realtime, send `session.update` with `type:"transcription"`, `audio.input.transcription.model=STT_OPENAI_STREAM`, `delay=STT_OPENAI_DELAY`, `turn_detection=null`; forward client `stt_audio` PCM as `input_audio_buffer.append`; on `stt_stop` send `input_audio_buffer.commit`; relay events via `map_openai_event` → client; drop `None`; run finals through `is_whisper_hallucination` (skip if hallucination).
- **Google bridge:** `SpeechClient` async/streaming `streaming_recognize` with `interim_results=True`, `model=STT_GOOGLE_MODEL`; pump client PCM into the request generator; relay results via `map_google_result`.
- Always close upstream on `stt_stop`/disconnect (per-minute billing + gRPC cleanup).
- If provider None → send `stt_error`.

**Step 4: PASS. Step 5: Commit** `feat(stt): /ws/stt streaming endpoint (OpenAI WS + Google gRPC)`

---

### Task 0.9: `POST /stt/translate` (any-target generative)

**Files:** Create `Orchestrator/stt/translate.py`; add route in `tts_routes.py`. Test `Orchestrator/tests/test_stt_translate.py`.
**Steps (TDD):** `translate_text(text, target_lang)` → prefer Gemini (`GOOGLE_API_KEY`) else GPT (`gpt-4o`); mock both, assert routing + `{"text":...,"target_lang":...}`; route accepts `{text|audio, target_lang}` (audio → `transcribe_bytes` first). **Commit** `feat(stt): /stt/translate any-target translation`

---

### Task 0.10: Onboarding allowlist `STT_PROVIDER`

**Files:** `Orchestrator/routes/onboarding_routes.py:30` (`ALLOWED_REVEAL_KEYS`) + current-config payload (~477-483).
**Steps (TDD):** test that `POST /onboarding/save {"secrets":{"STT_PROVIDER":"google"}}` persists and `GET /onboarding/current-config` reports it; add `"STT_PROVIDER"` (and optional `STT_OPENAI_FILE`, `STT_GOOGLE_MODEL`) to the allowlist + config payload. **Commit** `feat(onboarding): allow STT_PROVIDER selection`

---

### Task 0.11: Phase 0 review gate
Run full STT suite: `Orchestrator/venv/bin/pytest Orchestrator/tests/test_stt_*.py -v` (all green). Restart service (`sudo systemctl restart blackbox.service`, ~60-90s) and probe `curl -s localhost:9091/stt/catalog | python3 -m json.tool`. **Commit** any fixes. Dispatch code-review of Phase 0.

---

## PHASE 1 — Onboarding wizard step

### Task 1.1: `transcription` step registration
**Files:** `Portal/onboarding/onboarding.js:5` (add `"transcription"` to `STEPS` after `optional_integrations`); label map `:13-21`.

### Task 1.2: `transcription.js` step UI
**Files:** Create `Portal/onboarding/steps/transcription.js` (clone the card pattern from `steps/cli_agents.js`).
- Fetch `GET /stt/catalog` + `GET /onboarding/current-config`; render two radio cards (OpenAI / Google) with `available` badges + `blurb`.
- OpenAI card: needs `OPENAI_API_KEY` (link back to api_keys step if absent).
- Google card: needs service-account JSON — embed/link the existing uploader (`credentials_routes.py` upload) and show `GOOGLE_APPLICATION_CREDENTIALS` presence.
- Selection → `POST /onboarding/save {"secrets":{"STT_PROVIDER":id}}`; preselect the only-available one.

**Verification:** load the wizard locally, select each provider, confirm `.env` gets `STT_PROVIDER`, confirm `/stt/catalog` `resolved` matches. **Commit** `feat(onboarding): STT provider selection step`

---

## PHASE 2 — Core editable composers (web)

### Task 2.1: Shared streaming client `Portal/modules/stt-stream.js`
**Files:** Create `Portal/modules/stt-stream.js`.
- `sttStream.start(targetId, buttonId, {lang})`: open `ws://<host>/ws/stt`, `getUserMedia`, capture PCM16 @16k (reuse the AudioContext/ScriptProcessor pattern from `gemini-live.js`), send `stt_start` + `stt_audio` frames.
- On `stt_delta`/`stt_final`: apply to `#<targetId>` via a **delta applier** (track interim range; replace on each delta; commit on final; preserve user edits made outside the interim range).
- `sttStream.stop()`: send `stt_stop`, close, finalize.
- Native bridge: if `isNativeAndroid()`, feed PCM from `AndroidMic.startAudioStreaming` / `onNativeAudioChunk` instead of `getUserMedia`.

**Verification:** unit-test the delta applier in isolation if a JS test runner exists; otherwise manual. **Commit** `feat(stt): shared streaming STT client (web)`

### Task 2.2: Convert main composer mic (#1)
**Files:** `Portal/modules/ui-setup.js:957`, `Portal/modules/tts-stt.js:1779/1861` → route `#ctlMic` to `sttStream.start("prompt","ctlMic")`/`.stop()`. Keep old `/stt` path as fallback if provider streaming unavailable.

### Task 2.3: Convert gen-modal mics ×3 (#2)
**Files:** `Portal/modules/generation-modals.js:129/415/978` → `sttStream` against `generationPrompt`/`musicPrompt`/`videoPrompt`.

### Task 2.4: Convert Gemini recorder transcript (#3)
**Files:** `Portal/modules/gemini-recorder.js` → transcript now streams into `#prompt` (keep the WAV file-attach behavior).

**Verification (2.2-2.4):** real mic → live editable text in each box under both providers. Screenshot. **Commit** each task.

---

## PHASE 3 — Core composers (Android) + waveform restructure

### Task 3.1: `SttStreamClient.kt`
**Files:** Create `…/data/voice/SttStreamClient.kt` (parallel `VoiceClient.kt`) → `/ws/stt`, reuse `AudioRecord` PCM capture, expose a `Flow<SttEvent>` (Delta/Final/Error).

### Task 3.2: Waveform above the prompt box
**Files:** `…/ui/chat/Composer.kt:158-213` — restructure so `VoiceWaveform` (from `ui/voice/VoiceWaveform.kt`) sits in its **own row directly above** the `BasicTextField`, both visible during recording; retire the inline `WhisperWaveform` (`Composer.kt:408-521`). Drive amplitude from the streaming mic loop (`AudioAmplitude.rmsAmplitude`).
**Verification:** screenshot before/after; confirm send/attach controls unaffected.

### Task 3.3: Convert chat mic (#8) + record-for-Gemini (#8b)
**Files:** `NativeMainActivity.kt:624` (`onWhisper`) and `:650` (`onRecordAudio`) → stream deltas into `chatViewModel.inputText`; keep audio-attach for 8b.

### Task 3.4: Retire dead `TtsRepository.transcribe()`
**Files:** `…/data/repository/TtsRepository.kt:217-221`.

**Verification:** build APK, real-device dictation shows live text with waveform above the box. **Commit** each task.

---

## PHASE 4 — Live-voice user-transcript streaming

### Task 4.1: Repoint web live-voice user transcript
**Files:** `Portal/modules/gemini-live.js`, `gpt-realtime.js`, `grok-live.js` — emit user transcript as streaming deltas via the new adapter (or surface the provider-native input-transcription deltas) instead of post-hoc `/stt/json`. Keep AI deltas unchanged.
**Verification:** user text appears live in each voice agent's transcript panel. **Commit.**

---

## PHASE 5 — Translate feature (UI)

### Task 5.1: Web translate UI
**Files:** add a target-language picker + "Translate" affordance near the composer (`Portal/index.html` + a small module) → `POST /stt/translate`. Reusable for any STT target.

### Task 5.2: Android translate UI
**Files:** target-language control + Translate action in `ui/chat/Composer.kt` (and later overlay) → `/stt/translate`.

**Verification:** translate a captured utterance into ≥2 target languages on both surfaces. **Commit each.**

---

## PHASE 6 — Edges

### Task 6.1: Android CLI-agent mic streaming (#9 ×2)
**Files:** `ui/cli_agent/WhisperMicButton.kt`, `TerminalScreen.kt:606`, `ZellijTerminalScreen.kt:556` — stream deltas as incremental PTY paste (buffer + flush on final to avoid paste spam).

### Task 6.2: Android XR overlay mic (#11)
**Files:** `overlay/OverlayService.kt` → streaming `setText` into `EditText promptInput`.

### Task 6.3: Wire orphan web mic buttons
**Files:** `#micVideoExtensionPrompt` (`index.html:949`), `#micGoogleSSML` (`:1047`), `#micGeminiPro` (`:1132`) → `sttStream` against their natural targets.

### Task 6.4 (optional): Android generation-screen mics
Add mic buttons to Image/Music/Video/SSML/GeminiTTS screens if desired.

**Verification:** each edge surface dictates live. **Commit each.**

---

## Final gate (before GitHub push)

1. `Orchestrator/venv/bin/pytest Orchestrator/tests/ -q` — full suite green.
2. **Production-install simulation:** with only `OPENAI_API_KEY` → Google shows "not configured", OpenAI streams; with only service-account JSON → reverse; neither → graceful disable, no crash. (Toggle via `.env` + restart.)
3. **Back-compat:** `/stt`, `/stt/json`, MCP `speech_to_text`, phone path still return `{"text":...}`.
4. **Provider parity:** same utterance through OpenAI and Google, both stream deltas + finalize, on web and Android.
5. Final code-review (superpowers:code-reviewer) of the whole branch.
6. `/snapshot-dev` to record the work.
7. On Brandon's go: merge `feat/streaming-stt-multiprovider` → `main`, push.

---

## Notes / gotchas for the implementer
- `gpt-realtime-whisper` server-side WS: confirm the transcription session accepts the `delay` knob; benchmark on real audio (Brandon: prove with data, not theory).
- Cloud Speech v2 `chirp_2` **streaming** supports a limited language set vs batch — default `en-US`, expose `lang`, document the limit.
- Read `project_id` from the service-account JSON — do **not** add a separate project env var.
- Close every streaming session on stop/disconnect (per-minute billing + gRPC cleanup).
- Android composer layout change: screenshot-verify; Brandon prefers pixel-checked layout over verbal description.
- MCP tool changes (if adding `provider`/`translate_to`) only take effect in a new Claude Code session.
