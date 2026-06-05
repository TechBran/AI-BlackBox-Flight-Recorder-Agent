# Streaming STT (Multi-Provider) — Design

**Status:** Design / brainstorming output. Precedes the Superpowers implementation plan.
**Date:** 2026-06-04
**Author:** Claude (Opus 4.8) with Brandon

---

## Goal

Replace the single-provider, push-to-talk, file-based Whisper transcription with a **multi-provider, live-streaming** speech-to-text layer that works **everywhere transcription happens** across the web Portal and the Android MVP. When a user taps a mic button anywhere in the BlackBox, transcript text appears **live and editable** in the target box as they speak. The provider is chosen once in the onboarding wizard — **OpenAI** or **Google** — with automatic fallback to whichever credential is configured. Add a separate **Translate** feature (any target language) on both surfaces.

Built as **production code installable on anyone's machine** — no hardcoded paths/keys, every dependency declared, graceful capability gating when a provider isn't configured.

## Guiding principle (Brandon, 2026-06-04)

> "…in the future, when we update or if things update, then we're just updating the model name instead of the whole architecture."

Everything below is a **provider-adapter abstraction with config-driven model names**. Each provider uses its own native realtime mechanism; the clients only ever see one uniform contract. Swapping to a future model = changing a string in `config.py`.

---

## Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| The onboarding selector | **Binary: streaming via OpenAI _or_ streaming via Google** | One choice drives both the streaming and file surfaces. |
| OpenAI streaming model | **`gpt-realtime-whisper`** (the GPT Realtime transcribe model) | Streaming transcription session; live deltas; per-audio-minute pricing. |
| OpenAI file model | **`gpt-4o-transcribe`** (default), `whisper-1` selectable | OpenAI routes file/request-response workflows here; realtime-whisper isn't a file model. |
| Google streaming model | **Google Cloud Speech-to-Text v2 streaming (`chirp_2`)** via **service-account JSON** | Brandon's explicit pick: "streaming via Google… JSON service account only, that's fine — that's the option we want." Dedicated ASR, true gRPC streaming. **Not** Gemini Live. |
| Google file model | **Cloud Speech v2 `Recognize` (`chirp_2`)**, same service account | One auth + one SDK for the whole Google provider (coherent). |
| Google auth | **`GOOGLE_APPLICATION_CREDENTIALS` (service-account JSON)**; project derived from the JSON's `project_id` | Upload path already exists (`credentials_routes.py`); no extra env var for users. |
| Translate scope | **Any target language**, as a separate generative step (Gemini-key or GPT), independent of the STT provider | Cloud Speech translation is limited/asymmetric language pairs; a generative translate gives true any-target. |
| Transport | **Per-provider native, behind one uniform client WS (`/ws/stt`)** | OpenAI = realtime **WebSocket** bridge; Google = Cloud Speech v2 **gRPC** streaming bridge. Clients (web/Android) only ever see `/ws/stt` with `stt_delta`/`stt_final`. STT pushes audio up and pulls *text* down, so WebRTC's audio-playback benefit doesn't apply; keys/creds stay server-side (BYOK, Tailscale perimeter). |
| Coverage | **Everything, phased in one plan** | All 11 entry points; core composers first, edges later. |
| Workflow | **Local on this machine, feature branch in the main repo; push to GitHub only after all features tested & verified** | Same flow as the waveform/TTS/operator/gmail work. |

---

## The two STT surfaces

STT here is two surfaces that want different models. The provider abstraction spans both; the **same** onboarding provider choice governs both.

| Surface | Trigger | Client transport | Upstream (OpenAI) | Upstream (Google) |
|---|---|---|---|---|
| **Streaming** (headline) | Mic buttons everywhere | `/ws/stt` (uniform) | realtime WS, `gpt-realtime-whisper` | gRPC `StreamingRecognize`, `chirp_2` |
| **File** (fallback) | Attach-audio, MCP `speech_to_text`, telephony/phone | `POST /stt` (multipart) | `/v1/audio/transcriptions`, `gpt-4o-transcribe` | Cloud Speech v2 `Recognize`, `chirp_2` |

File surface is for inputs with no live mic (uploaded files, the MCP tool, Twilio/phone audio).

---

## Architecture

### Provider abstraction (the core)

New package `Orchestrator/stt/` (mirrors `Orchestrator/onboarding/` style):

```
Orchestrator/stt/
  __init__.py
  catalog.py         # build_stt_catalog() — providers × surfaces × models + capability flags
  providers.py       # STTProvider base + OpenAISTT, GoogleSpeechSTT
  resolve.py         # resolve_stt_provider(): STT_PROVIDER if set & available, else the single available one
  file_transcribe.py # unified file path (replaces inline whisper calls in tts_routes.py)
  streaming.py       # streaming session adapters (OpenAI WS bridge, Google gRPC bridge) for /ws/stt
  translate.py       # any-target translation (generative: Gemini-key or GPT)
```

**The adapter pattern absorbs the gRPC-vs-WS difference.** The client always speaks the uniform `/ws/stt` contract; internally the OpenAI adapter bridges to an OpenAI realtime WS session, the Google adapter bridges to a Cloud Speech v2 gRPC `StreamingRecognize` stream. Neither web nor Android ever knows the difference — that's what makes the model/provider swappable without touching clients.

**Config-driven model names** (`Orchestrator/config.py`, mirroring the existing `STT_MODEL`):

```python
# --- STT provider/model registry (swap a string to upgrade) ---
STT_PROVIDER       = os.getenv("STT_PROVIDER", "")           # "" = auto (whichever cred present)
STT_OPENAI_STREAM  = os.getenv("STT_OPENAI_STREAM", "gpt-realtime-whisper")
STT_OPENAI_FILE    = os.getenv("STT_OPENAI_FILE",   "gpt-4o-transcribe")
STT_OPENAI_DELAY   = os.getenv("STT_OPENAI_DELAY",  "low")   # gpt-realtime-whisper latency knob
STT_GOOGLE_MODEL   = os.getenv("STT_GOOGLE_MODEL",  "chirp_2")
STT_GOOGLE_REGION  = os.getenv("STT_GOOGLE_REGION", "us-central1")  # chirp_2 GA regions
# legacy STT_MODEL stays as the whisper-1 file fallback
```

**Capability gating** (mirrors `USE_CLOUD_TTS = bool(creds and exists)`):

```python
STT_OPENAI_AVAILABLE = bool(OPENAI_API_KEY)
STT_GOOGLE_AVAILABLE = bool(GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(GOOGLE_APPLICATION_CREDENTIALS))
```

The wizard fallback ("if only one is set up, that one works") falls out of `resolve_stt_provider()` for free.

### `GET /stt/catalog` (mirror of `/tts/catalog`)

```python
@app.get("/stt/catalog")
def stt_catalog():
    return {
      "providers": build_stt_catalog(),    # [{id, label, available, streaming, file, blurb, models:{...}}]
      "resolved": resolve_stt_provider(),  # what will actually be used given creds + STT_PROVIDER
      "default": STT_PROVIDER or resolve_stt_provider(),
    }
```

Consumed by the onboarding wizard, the Portal, and Android.

---

## Backend changes

### 1. Streaming transcription over `/ws/stt`

The app already runs `/ws/realtime`, `/ws/gemini-live`, `/ws/grok-live` carrying mic PCM up and transcript text down for the voice agents. Add a transcription-only WS endpoint `/ws/stt` using the same proxy pattern:

**Uniform client contract (provider-agnostic):**
```
↑ {type:"stt_start", target:"prompt", lang?:"en"}
↑ {type:"stt_audio", pcm:"<base64 16k mono PCM16>"}   # reuse Android startAudioStreaming + web capture
↑ {type:"stt_stop"}
↓ {type:"stt_delta", text:"...", target:"prompt"}     # incremental
↓ {type:"stt_final", text:"...", target:"prompt"}
↓ {type:"stt_error", message:"..."}
```

- **OpenAI adapter:** open a realtime session `type:"transcription"`, `audio.input.transcription.model = STT_OPENAI_STREAM`, `delay = STT_OPENAI_DELAY`, manual commit. Map `conversation.item.input_audio_transcription.delta` → `stt_delta`, `.completed` → `stt_final`.
- **Google adapter:** `google-cloud-speech` `SpeechClient` (regional endpoint), `StreamingRecognize` with `model=STT_GOOGLE_MODEL`, `language_codes`, `interim_results=true`. Map interim results → `stt_delta`, `is_final` → `stt_final`. Reads creds from `GOOGLE_APPLICATION_CREDENTIALS`; project from the JSON's `project_id`; recognizer `projects/{project}/locations/{region}/recognizers/_`.
- Apply the shared `whisper_filter.is_whisper_hallucination()` to finals.
- Creds/keys never leave the backend.

### 2. File path — multi-provider `POST /stt` + `/stt/json`

Refactor the two endpoints in `Orchestrator/routes/tts_routes.py` (~308, ~331) to delegate to `stt/file_transcribe.py`, branching on resolved provider:
- OpenAI → `STT_OPENAI_FILE` via `/v1/audio/transcriptions`.
- Google → Cloud Speech v2 `Recognize` (`chirp_2`) with inline audio.
- Response stays `{"text": "..."}` — **every existing caller keeps working unchanged** (MCP `speech_to_text`, phone bridge, Twilio).
Parametrize the 3 stray hardcoded `'whisper-1'` strings (`twilio_routes.py:~1499`, `phone/bridge.py:~2940`, `realtime_routes.py:~521`) to read from config.

### 3. Translate

`stt/translate.py` + `POST /stt/translate {text|audio, target_lang}`:
- Generative, any-target: prefer Gemini (`GOOGLE_API_KEY`) or GPT (`gpt-4o`) — independent of the STT provider so it works whichever STT is selected.
- If given audio, transcribe first (resolved STT provider) then translate.
Returns `{"text": "...", "target_lang": "..."}`.

### 4. MCP `speech_to_text` tool

Stays file-based; now provider-aware via the refactored `/stt`. Optional `provider` / `translate_to` params (config-default if omitted). No new MCP surface — it already proxies to `/stt`. (MCP edits take effect next Claude Code session.)

### 5. Dependency + production install

- Add **`google-cloud-speech`** to `requirements*.txt`; install into `Orchestrator/venv`.
- Capability-gate so a box without the service account simply shows Google as "not configured" and uses OpenAI (and vice-versa) — never crashes.

---

## Onboarding wizard

Mirror `Portal/onboarding/steps/cli_agents.js` (provider-selector cards).

- Add `"transcription"` to `STEPS` in `Portal/onboarding/onboarding.js:5`.
- New `Portal/onboarding/steps/transcription.js`: two radio cards — **OpenAI** and **Google** — each showing availability from `GET /stt/catalog`, a one-line "what it's good at" blurb, and selection writing `STT_PROVIDER`.
  - **Spell out the difference:** OpenAI = `gpt-realtime-whisper` streaming + `gpt-4o-transcribe` files, prompt-steerable, key-based; Google = Cloud Speech v2 `chirp_2` streaming + file, multilingual ASR, **service-account JSON**.
  - **Credentials wiring:** OpenAI needs `OPENAI_API_KEY` (existing API-keys step). Google needs the service-account JSON — **reuse the existing upload flow** in `credentials_routes.py` (upload → `update_env(GOOGLE_APPLICATION_CREDENTIALS)`); surface its presence via `GET /current-config` (already returns `GOOGLE_APPLICATION_CREDENTIALS`). The step links to / embeds that uploader if the JSON isn't present.
  - If only one credential present → preselect it, note the other needs its credential.
- Save `STT_PROVIDER` via the existing `POST /save` → `update_env`. **Allowlist `STT_PROVIDER`** (+ optional model overrides) in `onboarding_routes.py:31` (`ALLOWED_REVEAL_KEYS`) and the current-config payload.

---

## Web Portal changes

Shared streaming client `Portal/modules/stt-stream.js`: opens `/ws/stt`, streams mic PCM (reuse capture in `tts-stt.js` / live-voice modules), applies `stt_delta`/`stt_final` to a target element with **live editing** (interim range replaced per delta, committed on final, manual edits preserved). Mic buttons call `sttStream.start(targetId, buttonId)` / `.stop()` instead of record→`/stt`.

Per entry point (from the inventory):
1. **Main composer mic** `#ctlMic` → `#prompt` (`ui-setup.js:957`, `tts-stt.js:1779/1861`).
2. **Gen-modal mics ×3** → `#generationPrompt` / `#musicPrompt` / `#videoPrompt` (`generation-modals.js:129/415/978`).
3. **Gemini recorder** `#ctlRecordAudio` → `#prompt` (`gemini-recorder.js`) — keep file-attach; transcript now streams.
4. **Live voice agents (×3)** already stream — repoint their **user** transcript to the new adapter so user text streams too; AI deltas unchanged.
5. **Translate UI**: target-language picker + Translate affordance near the composer (reusable for any STT target) → `/stt/translate`.
6. **Orphan buttons** (`#micVideoExtensionPrompt`, `#micGoogleSSML`, `#micGeminiPro`): wire to `sttStream` (later task).

---

## Android changes

### Waveform restructure (Brandon's explicit ask)

- Adopt the HD ribbon (`ui/voice/VoiceWaveform.kt`) as the **standard transcription waveform**; retire the inline `WhisperWaveform` in `Composer.kt`.
- **Move the waveform to its own row directly above the prompt box.** Today (`Composer.kt:173-213`) the waveform and `BasicTextField` share one slot (mutually exclusive). Restructure into `[ VoiceWaveform row ]` over `[ editable text field ]` so the field stays visible and fills with live deltas while the ribbon rides above it.
- Amplitude from the live RMS source already used by `VoiceScreen` (`AudioAmplitude.rmsAmplitude`), now driven by the streaming mic loop.

### Streaming client

`data/voice/SttStreamClient.kt` (parallels `VoiceClient.kt`) over `/ws/stt`, reusing `AudioRecord` PCM capture. Emits delta/final into the target Compose state.

Per entry point:
7. **Native WebView bridge** (`PortalActivity.kt` `AndroidMic`): WebView Portal uses web `stt-stream.js`; ensure the PCM bridge (`startAudioStreaming` / `onNativeAudioChunk`) feeds it (already does for voice agents).
8. **Native chat composer mic** → `chatViewModel.inputText` (`NativeMainActivity.kt:624`, `AudioRecorderManager`): switch to streaming deltas.
8b. **"Record for Gemini"** → `chatViewModel.inputText`: streaming + keep audio attach.
9. **CLI-agent mic ×2** (`WhisperMicButton.kt` in `TerminalScreen.kt:606` + `ZellijTerminalScreen.kt:556`): stream as incremental PTY paste (buffer + flush on final), later task.
11. **XR overlay mic** (`OverlayService.kt` → `EditText promptInput`): streaming `setText`, later task.
- **Translate UI**: target-language control + Translate action in composer (and overlay) → `/stt/translate`.
- **Android generation screens**: add mic buttons (none today) — later/optional task.

Retire dead `TtsRepository.transcribe()`.

---

## Conversion checklist & phasing (the 11 entry points)

**Phase 0 — Backend foundation**
- `Orchestrator/stt/` package, config registry, `build_stt_catalog()`, `GET /stt/catalog`, `resolve_stt_provider()`.
- File path multi-provider refactor of `/stt` + `/stt/json`; parametrize stray `whisper-1`.
- `/ws/stt` proxy + OpenAI (WS) & Google (gRPC) streaming adapters; uniform event contract.
- `POST /stt/translate`. Add `google-cloud-speech` dependency.
- Onboarding allowlist `STT_PROVIDER`; resolver + fallback.
- Tests (pytest): catalog shape; provider resolution/fallback (only-OpenAI / only-Google / both / neither); file transcribe per provider (mocked); streaming adapter event mapping (synthetic provider events → `stt_delta`/`stt_final`); translate routing; hallucination filter applied.

**Phase 1 — Onboarding wizard** — `transcription.js` step + registration + capability display + service-account hookup + save.

**Phase 2 — Core editable composers (web)** — `stt-stream.js` + delta applier; convert #1, #2 (×3), #3.

**Phase 3 — Core composers (Android) + waveform restructure** — `SttStreamClient.kt`; convert #8, #8b; waveform above prompt; retire inline `WhisperWaveform`.

**Phase 4 — Live-voice user-transcript streaming** — repoint web #4 (GPT/Gemini/Grok) user transcript to the new adapter.

**Phase 5 — Translate feature** (web + Android UI).

**Phase 6 — Edges** — #9 CLI-agent PTY paste (×2), #11 XR overlay, orphan web buttons, Android generation mics.

---

## Testing strategy

- **Backend unit (pytest, `Orchestrator/venv`):** as listed in Phase 0.
- **Live local validation (this machine):** real mic → each web composer + Android chat composer (with waveform-above-box) shows live editable text under **both** providers; wizard selection persists to `.env` and is honored; service-account upload → Google streaming works; translate round-trip (multiple targets).
- **Back-compat:** `/stt`, `/stt/json`, MCP `speech_to_text`, phone/Twilio still return `{"text":...}`.
- **Provider parity matrix:** same utterance through OpenAI and Google; both stream deltas + finalize.
- **Production-install check:** fresh-box simulation — only OpenAI key → Google shows "not configured", OpenAI works; only service account → reverse; neither → graceful disable, no crash.

---

## Out of scope / deferred

- **Gemini Live / Gemini `generateContent` for STT** — superseded by the service-account Cloud Speech v2 decision; not used for the Google STT provider. (Gemini may still be used for the *translate* step.)
- **Diarization / speaker labels** (`gpt-4o-transcribe-diarize`, etc.) — future file-path option.
- **Client-side WebRTC** — not pursued; the adapter boundary leaves it open for the OpenAI leg if ever needed.

---

## Open risks / watch-items

1. **`gpt-realtime-whisper` over backend WS** — confirm transcription-session works server-side via WS; benchmark the `delay` knob on real audio (prove with data).
2. **Cloud Speech v2 `chirp_2` streaming language coverage** — streaming supports a limited language set vs batch; default `en-US`, expose `lang` and document the limit.
3. **`google-cloud-speech` on the target Python/venv** — verify install + gRPC works in the service environment.
4. **Delta-editing UX** — interim-vs-committed text while the user also edits; define the merge rule once in the shared applier (web + Android) so behavior matches.
5. **Android composer layout** — moving the waveform to its own row must not regress send/attach controls; screenshot-verify.
6. **Per-minute billing** (`gpt-realtime-whisper` $0.017/min) — close sessions promptly on `stt_stop`.
7. **Region/project derivation** — read `project_id` from the service-account JSON; pick a `chirp_2` GA region (`us-central1` default), make it overridable.
