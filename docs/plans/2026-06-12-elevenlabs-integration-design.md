# ElevenLabs Full-Platform Integration — Design

**Date:** 2026-06-12
**Status:** Validated with Brandon (brainstorming session) — NOT yet committed; implementation plan to follow
**Companion doc:** `docs/plans/2026-06-12-elevenlabs-integration-plan.md` (phase-by-phase implementation plan)

## Goal

Bring ElevenLabs into the BlackBox as a first-class provider across every capability their API offers: BYOK key onboarding, streaming + batch STT, the full voice catalog with cloning/design, music, sound effects, and audio utilities — with a phased path to the phone stack and a future-work appendix for the rest.

## Decision Log (validated 2026-06-12)

| Decision | Outcome |
|---|---|
| Voice selector | **Hybrid**: keep grouped dropdown; add live ElevenLabs group (My Voices → Premade → "Browse library…" search modal) |
| Cloning UX | **Both**: dedicated Voice Lab Portal panel AND agent tools (`elevenlabs_clone_voice`, `elevenlabs_design_voice`) |
| Frontend surfaces | Every UI feature ships on **3 surfaces**: Portal web, Android Kotlin MVP (`AI_BlackBox_Portal_Android_MVP (2)/`), WebView wrappers (Tauri/WebKitGTK) |
| Scope (real phases) | Core five + **sound effects + voice changer + isolator + Scribe batch upgrade**. Dubbing, forced alignment, ElevenAgents, image/video → future-work appendix |
| Live voice paths | TTS/STT request/response surfaces now; **phone (μ-law) as a later planned phase**; realtime conversational agents = future-work note |
| Audio defaults | **Quality-first**: flagship model (`eleven_v3`) + max output quality the plan allows; cheaper/faster tiers are explicit user downgrades, never silent |
| Tool naming | **Provider-explicit**: `elevenlabs_music`, `elevenlabs_sound_effects`, etc. Existing `generate_music` (Lyria) renamed `lyria_music` for traceability |
| Source of truth | **The provider's API**: model lists, voices, plan features fetched live (`GET /v1/models`, `/v2/voices`, `/v1/user`) with short-TTL caches. config.py holds our choices, never provider facts |
| Diarization | **Extra care**: Scribe batch is not just a provider swap — full touchpoint sweep of everywhere transcription flows in the BlackBox |

## Research Digest — ElevenLabs API surface (June 2026)

Auth: `xi-api-key` header (single-use tokens available for client-side STT). Regional endpoints exist (`api.us.elevenlabs.io` etc.); default `api.elevenlabs.io`.

### Core audio APIs
- **TTS** — `POST /v1/text-to-speech/{voice_id}` (+ `/stream`, + WebSocket `stream-input`, multi-context WS). Models: `eleven_v3` (most expressive, 70+ langs, audio tags, ~5k char), `eleven_flash_v2_5` (~75ms, 50% cheaper, 40k chars), `eleven_multilingual_v2` (stable long-form, ~10k chars). Output: MP3 22.05–44.1kHz/32–192kbps, PCM 16–48kHz, **μ-law 8kHz** (telephony), A-law, Opus 48kHz. Prosody continuity via previous/next text + request-id params. Voice settings: stability, similarity, style, speed.
- **STT streaming** — `WS GET /v1/speech-to-text/realtime` (Scribe v2 realtime, ~150ms). Input: base64 `input_audio_chunk` messages, PCM 8/16/22.05/24/44.1/48kHz or μ-law 8kHz (default `pcm_16000`). Output messages: `session_started`, `partial_transcript`, `committed_transcript`(`_with_timestamps`). Commit strategies: `manual` or `vad` (default; threshold 0.4, silence 1.5s). Options: `language_code`, `keyterms` (bias, up to 1,000), `no_verbatim` (drop filler), `include_timestamps`, `include_language_detection`. Documented error taxonomy: `auth_error`, `quota_exceeded`, `commit_throttled`, `rate_limited`, `queue_overflow`, `resource_exhausted`, `session_time_limit_exceeded`, `input_error`, `chunk_size_exceeded`, `insufficient_audio_activity`, `transcriber_error`.
- **STT batch** — `POST /v1/speech-to-text` (Scribe v1/v2): 90+ languages, **diarization up to 32 speakers**, word timestamps, audio-event tags (laughter etc.), entity detection (56 categories), keyterms, no-verbatim. Files to 3GB / 10hrs (1hr multichannel ≤5 channels). Audio + video container formats accepted. Async via webhooks.
- **Music** — `POST /v1/music` (Music v1 model id `music_v1`; "Music v2" marketed tier): `prompt` XOR `composition_plan`; `music_length_ms` 3,000–600,000 (capability page: max 5 min); composition plan = global pos/neg styles + sections (name, pos/neg local styles, `duration_ms` 3k–120k, lyric `lines` ≤200 chars each); `force_instrumental`, `seed`, `respect_sections_durations`, C2PA signing; `output_format` mp3/pcm/opus. Plus `POST /v1/music/upload` (extract composition plan from existing audio; returns `song_id`). Commercially cleared. ~$0.15/min.
- **Sound effects** — `POST /v1/text-to-sound-effects`: text prompt, `duration_seconds` 0.1–30, `prompt_influence`, **looping** (seamless loops), WAV 48kHz for non-looping; ~40 credits/sec when duration pinned.
- **Voice changer** — `POST /v1/speech-to-speech/{voice_id}`: re-voice audio preserving prosody/emotion.
- **Voice isolator** — `POST /v1/audio-isolation`: strip background noise.
- **Dubbing** (async jobs), **Forced alignment** (word-level text↔audio timing) — future-work.

### Voice management
- **Voices v2** — `GET /v2/voices`: search, `category` (premade/cloned/generated/professional), `voice_type` (personal/community/default/workspace/saved), `page_size` ≤100, `next_page_token` pagination. Per voice: `voice_id`, name, category, labels (accent/gender/age), `preview_url`, samples, description.
- **IVC** — `POST /v1/voices/add`: name + audio files (+ description, labels, `remove_background_noise`). Returns `voice_id` + `requires_verification`. ~1min clean audio recommended. Starter+ plan.
- **PVC** — separate `/v1/voices/pvc/*` API: hours of audio, identity verification, highest fidelity. Creator+ plan. (Planned as Voice Lab follow-on, not phase-blocking.)
- **Voice Design** — `POST /v1/text-to-voice/design` (`voice_description`, optional `text` 100–1000 chars, `model_id` `eleven_multilingual_ttv_v2`|`eleven_ttv_v3`, `guidance_scale`, `loudness`, `should_enhance`) → preview list (base64 mp3 + `generated_voice_id`); save via `POST /v1/text-to-voice` with chosen `generated_voice_id`.
- **Shared voice library** — thousands of community voices; library search + add-to-account endpoints.
- **Voice remixing** — transform existing voices (future-work).

### Discovery/admin endpoints (our SoT)
- `GET /v1/models` — model list with capability metadata (limits, languages). **Internal catalogs derive from this.**
- `GET /v1/user` — subscription tier, feature gates, credit balance. **Validator + status endpoint derive from this.**
- `GET /v2/voices` — the live voice catalog.

### Pricing/gating (for onboarding messaging; do not hardcode — read from `/v1/user`)
Free (PAYG) → Starter $6 (IVC) → Creator $22 (PVC, 100k credits) → Pro $99 (10 concurrent) → Scale/Business. TTS $0.05–0.10/1k chars; STT $0.22–0.39/hr; music $0.15/min. Higher-bitrate output formats gated to paid tiers.

### Bigger-platform products (future-work appendix)
ElevenAgents (full conversational agent platform: WS protocol, Twilio/SIP, tools, knowledge bases, workflows, testing, ZRM privacy mode), Reception AI, Studio long-form production, Audio Native embeds, image & video generation.

## Architecture

### 1. Provider core — `Orchestrator/elevenlabs/`
- `client.py` — single auth point (`ELEVENLABS_API_KEY` from `.env`), base URL, timeouts, retry policy, error normalization (their error taxonomy → BlackBox-style errors, mapped once).
- Capability modules: `stt.py`, `tts.py`, `voices.py`, `music.py`, `sfx.py`, `transform.py` (changer + isolator).
- `catalog.py` — SoT layer: cached fetchers for `/v1/models`, `/v2/voices`, `/v1/user` (short TTL ~5min; voice cache busted on clone/design/delete). **No provider facts in config.py** — only our defaults (e.g. default model choice, default voice settings).
- Routes: `GET /elevenlabs/status` (key present, plan tier, per-feature availability, credit balance — mirrors `/embeddings/status`; all frontends hydrate from it; no key → ElevenLabs UI hides everywhere, no fallback behavior).

### 2. Onboarding (BYOK)
- Flip the existing placeholder card in `Portal/onboarding/steps/optional_integrations.js:37` to an active key input.
- `validators.py`: `validate_elevenlabs()` → cheap `GET /v1/user`; surface plan tier + unlocked features in the result detail ("key valid — Creator plan, voice cloning available"), not just a checkmark.
- Key saved via existing `/onboarding/save` flow; rehydration shows configured state with Replace.

### 3. STT
**Streaming (third `/ws/stt` provider):** connect to Scribe realtime WS; send PCM-16k base64 chunks (existing capture rate); map `partial_transcript`→interim, `committed_transcript`→final into the existing `InterimAccumulator` (segment semantics ≈ Google chirp_2 — cumulative-style normalization reused). VAD commit mode (matches current UX). `keyterms` wired but empty initially; later feed operator names + BlackBox vocabulary. `/stt/catalog` gains `elevenlabs` entry — Android (`SttStreamClient.kt`, `SettingsSheet.kt`) inherits via catalog contract; verify Kotlin parser tolerates the third entry.

**Batch (extra-care phase):** `/stt` file path gains Scribe v2 with diarization/timestamps/audio-events/entities. Response normalized to current transcript shape + rich extras under an `elevenlabs` detail key (additive). **Diarization touchpoint sweep** — wire speaker-labeled transcripts through every consumer:
1. `/stt`, `/stt/json` endpoints
2. `speech_to_text` ToolVault tool → returns speaker-labeled segments, not flat text
3. Session-upload audio attachments ("transcribe this meeting — who said what")
4. Phone-call recordings (TG200/Asterisk): diarized call logs, caller vs agent
5. **Snapshot minting**: diarized meeting transcripts minted into the ledger = searchable, speaker-attributed memory
6. Production/analyze-audio workflows

### 4. TTS catalog + hybrid voice selector
- `build_tts_catalog` keeps static OpenAI/Gemini groups; dynamic merge layer appends ElevenLabs group from cached `/v2/voices`: **My Voices** (cloned/generated/professional) first, then **Premade**. IDs namespaced `elevenlabs:{voice_id}`. Additive fields (`preview_url`, labels) — keep `TtsVoiceParseTest` shape valid.
- `/tts` routing: `elevenlabs:` prefix → their convert endpoint. **Defaults: `eleven_v3` + highest output quality the plan allows (`mp3_44100_192` Creator+); `flash_v2_5`/lower bitrates = explicit downgrades only** (auto-route to flash only where the path technically demands latency, and say so). Long text → existing `/tts/stitch` chunking (premium models have tighter char limits).
- **Browse-library modal** (web) / Compose sheet (Android): search shared library, preview via `preview_url`, "Add to my account" → appears in My Voices. WebView wrappers: audio playback sanity check (WebKitGTK autoplay policy).

### 5. Voice Lab + agent tools
**Portal panel, three zones:**
- *Clone*: mic record (getUserMedia path from STT, ≥60s guided, level meter) or upload; `remove_background_noise` on by default for mic; name/description/labels.
- *Design*: description → 3 preview cards (inline playback) → save chosen.
- *My Voices*: preview/rename/delete; delete shows where the voice is in use first.
- Panel renders from `/elevenlabs/status`; Free tier shows upgrade explainer, not dead buttons.

**ToolVault v2 modules** (provider-explicit names): `elevenlabs_clone_voice` (session-upload paths → IVC → bust voice cache; required `confirm_consent` boolean in schema — agent must ask), `elevenlabs_design_voice` (two-step: previews saved to media files → confirm pick), `elevenlabs_list_voices`, `elevenlabs_delete_voice`.

**Consent guardrail:** Portal consent checkbox + tool-schema `confirm_consent` — mirrors ElevenLabs ToS.

**Android:** Compose Voice Lab screen reusing the `AudioRecord` stack (release-in-finally pattern per `feedback_android_audiorecord_race`), multipart upload to the same backend routes.

### 6. Music, SFX, utilities (ToolVault modules)
- `elevenlabs_music` — separate from (renamed) `lyria_music`. Simple prompt + `music_length_ms`, or full `composition_plan` (sections/styles/lyrics) for produced work; `force_instrumental`. Output → `media_files/`, task pattern + `get_task_status` like Lyria.
- `lyria_music` rename migration — blast radius: chat injector, MCP server, phone bridge `unified_tool_map`, frozen `BLACKBOX_TOOLS_*`/`CHAT_TOOLS_*` fallback arrays (restart required), validate + `/toolvault/reload`.
- `elevenlabs_sound_effects` — 0.1–30s, `loop` for seamless ambience, WAV 48k.
- `elevenlabs_voice_changer` — re-voice recordings into any catalog/cloned voice.
- `elevenlabs_isolate_voice` — noise-strip; also called internally pre-clone.

### 7. Phone path (later phase)
μ-law 8kHz TTS output → TG200/Twilio announcement + recap-call paths (no transcoding). Scribe realtime accepts μ-law 8k input for future call transcription. Realtime conversational agents stay provider-native (future-work).

## Phase map (each independently shippable)
1. Provider core + onboarding key + `/elevenlabs/status` + SoT catalog layer
2. STT streaming (third `/ws/stt` provider)
3. Scribe batch + diarization touchpoint sweep (**extra care**)
4. TTS catalog + hybrid selector (web → Android → WebView)
5. Voice Lab + cloning/design tools
6. Music + SFX + utilities tools (incl. `lyria_music` rename migration)
7. Phone path: μ-law TTS into TG200/Twilio
- **Appendix (future-work):** dubbing, forced alignment, ElevenAgents platform, Reception AI, image/video gen, voice remixing, PVC full flow, Audio Native, realtime cascaded voice agents, provider-explicit rename sweep for `generate_image`/`generate_video`

## Testing strategy
- Unit: client error mapping, catalog merge/caching, WS message normalization (recorded Scribe transcripts as fixtures — hermetic, no live key in CI, following the embeddings-registry test pattern).
- Contract: `/tts/catalog` + `/stt/catalog` additive-shape tests; Android `TtsVoiceParseTest` extended for the EL group.
- Live smoke (manual, keyed): one script per capability — validate key, stream 5s mic, transcribe diarized fixture, synthesize v3 sample, clone from fixture consent-stubbed, 10s music, 3s SFX.
- Three-surface verification checklist per UI phase (web hard-refresh, Android build, Tauri webview).

## Risks
| Risk | Mitigation |
|---|---|
| Per-plan feature gating (Free key, dead features) | Status-driven UI from `/v1/user`; explainers not dead buttons |
| Provider capability drift | **SoT-from-API**: live `/v1/models` + `/v2/voices` + `/v1/user`, short-TTL caches; no hardcoded facts |
| `/v2/voices` cache staleness after clone/design | Explicit cache bust on mutating operations |
| Three-surface drift | Additive contracts + Kotlin parser checks + per-phase checklist |
| Credit burn invisibility | `/elevenlabs/status` surfaces balance; tools report credits used where API returns it |
| `lyria_music` rename breakage | Dedicated migration task; parity check before/after; restart for frozen arrays |
| WS session limits (`session_time_limit_exceeded`) | Reconnect-and-resume in provider, same as existing STT providers |
