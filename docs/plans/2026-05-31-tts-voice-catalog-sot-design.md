# TTS Voice Catalog — Single Source of Truth + Android Parity + Preview (Design)

**Date:** 2026-05-31
**Problem:** The Android Settings "Voice Preferences" picker shows only **6 of 30** Gemini voices (OpenAI shows all 11). The 30-voice Gemini catalog is duplicated across 4+ locations with 3 divergent descriptor conventions, and has drifted.

## Decisions (from investigation + user)

| Question | Decision |
|---|---|
| Which groups on Android | **Full parity with web portal:** OpenAI TTS HD (11) + Gemini **Flash** TTS (30) + Gemini **Pro** TTS (30) |
| Where the catalog lives | **Backend endpoint = true single source of truth.** Web + Android both fetch it |
| Preview/play button | **Single "preview-selected" ▶ button** next to the picker (mirrors web portal `btnPreviewVoice`) |
| Preview generation | **On-demand** (mirror portal): synthesize a fixed phrase live; OpenAI instant, Gemini via async poll |

## Current state (investigation findings)

Duplicated Gemini catalog (all 30 = Zephyr→Sulafat unless noted):
- `Portal/index-modular.html` — hardcoded `<option>`s: Gemini Flash (30) + Gemini Pro (30); descriptors `"Bright, cheerful"`
- Android `ui/generation/GeminiProTtsScreen.kt` `GEMINI_PRO_VOICES` (30) — same descriptor style
- Android `util/Constants.kt` `VOICES_GEMINI_LIVE` (30) — one-word descriptors (this is the **live voice agent**, separate feature)
- Android `data/repository/TtsRepository.kt` `TTS_VOICE_GROUPS` — **Gemini Pro = 6** (the bug), divergent descriptors; OpenAI = 11 (complete)
- Backend `Orchestrator/config.py` `GEMINI_LIVE_VOICES` (30) + descriptors (one-word)

Consumers / mechanics:
- Settings picker: `ui/settings/SettingsSheet.kt:417` renders `TTS_VOICE_GROUPS`; selection stored via `viewModel.setOperatorVoice(operator, id)`, read via `store.getOperatorVoice(operator)`.
- Android TTS synth: OpenAI = `POST /tts/batch` (sync, returns audio_url); Gemini = `POST /generate/gemini_tts` (async → `task_id` → poll `GET /tasks/status/{id}` → audio_url). Pattern already implemented in `GeminiProTtsScreen.kt:201-235`.
- Audio playback: `ui/components/AudioPlayerBar` (reusable) or transient `MediaPlayer` (see `NativeMainActivity.kt:324`).
- Portal preview: `Portal/modules/tts-stt.js:1646` `btnPreviewVoice` → `generateTTSAudioWithVoice(previewText, cfg)` → play.
- `parseVoice()` in `TtsRepository.kt` currently knows only `openai` + `gemini-pro` (and mis-maps gemini-pro → `gemini-2.5-flash-tts`). Needs `gemini-flash` (→ `gemini-2.5-flash-tts`) and `gemini-pro` (→ `gemini-2.5-pro-tts`).

## Target architecture

### Backend — the single source of truth
- `config.py`: define the canonical catalog ONCE.
  - `GEMINI_TTS_VOICE_DESCRIPTIONS: dict[name -> "Bright, cheerful"]` (30 entries, the portal/GeminiProTts convention) — shared by Flash + Pro groups so names+descriptions exist once.
  - `OPENAI_TTS_VOICES: list[(id,name,desc)]` (11).
  - A `TTS_CATALOG` builder producing groups: OpenAI HD, Gemini Flash (`gemini-flash:<Name>`, model `gemini-2.5-flash-tts`), Gemini Pro (`gemini-pro:<Name>`, model `gemini-2.5-pro-tts`).
- `tts_routes.py`: `GET /tts/catalog` → `{"groups":[{"id","label","voices":[{"id","name","description"}...]}...]}`.

### Android — consume + preview
- `TtsRepository`: `suspend fun fetchCatalog(): List<VoiceGroup>` (GET `/tts/catalog`, parse). Keep `TTS_VOICE_GROUPS` as an **offline fallback, expanded to the full catalog** (so offline shows everything, not 6). Fix `parseVoice()` for `gemini-flash` + `gemini-pro` with correct models.
- `SettingsSheet` (+ its VM): fetch catalog on open into state (fallback to constant on failure); render groups from it. Add a single ▶ **preview button** next to the TTS Voice dropdown:
  - On tap: synth fixed phrase ("Hello! This is a preview of the selected voice.") for the selected voice id via provider-aware path (OpenAI sync; Gemini submit+poll), then play via MediaPlayer. Show loading ("…") + disabled while in flight; toast on error. Cancel/guard against overlapping taps.
- Align `GeminiProTtsScreen.GEMINI_PRO_VOICES` to also source from the catalog (consume the `gemini-pro` group) — secondary step, same SoT.
- **Boundary:** the live Voice Agent voices (`Constants.VOICES_GEMINI_LIVE`, served by existing `/gemini-live/voices`) are OUT OF SCOPE — different feature, already backend-sourced.

### Web portal — consume SoT
- `tts-stt.js`: on init, fetch `/tts/catalog` and build `#ttsVoiceSelect` optgroups dynamically; remove the hardcoded `<option>`s from `index-modular.html` (or keep as static fallback). Preserve default `gemini-pro:Charon`. Preview button already works.

## YAGNI / non-goals
- No pre-generated sample clips (on-demand only).
- No per-voice-row play buttons (single preview-selected).
- Live Voice Agent voice list unchanged.
- No new TTS providers.

## Success criteria
- Android Settings voice picker lists OpenAI (11) + Gemini Flash (30) + Gemini Pro (30), fetched from `/tts/catalog`.
- ▶ preview plays the selected voice (OpenAI instant; Gemini after poll) with loading state.
- Web portal dropdown is populated from the same `/tts/catalog`.
- Catalog names + descriptions defined exactly once (backend); Android offline fallback matches.
