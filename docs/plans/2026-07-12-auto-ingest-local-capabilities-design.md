# Auto-Ingest Local Model Capabilities — Design

**Date:** 2026-07-12
**Status:** Approved — decisions validated with Brandon (this session)
**Implementation plan:** `docs/plans/2026-07-12-auto-ingest-local-capabilities.md`
**Builds on:** the shipped local-image provider (`docs/plans/2026-07-12-local-image-generation-design.md`), which is the first instance of this pattern.

---

## Goal / vision

When a user registers ONE OpenAI-compatible LAN server (API key + base URL) in the onboarding wizard, the BlackBox should **auto-detect every capability that server offers and wire each discovered model into the right subsystem** — chat, image, text-to-speech, speech-to-text — with **zero per-modality setup**. A person customizing their own BlackBox inserts a key + URL and *has it all*; nobody types "add image."

## Validated decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Detection vs. safety | **Auto-detect + confirm** — name-classify each discovered model into a modality (seed guess), show it in the wizard pre-filled, user accepts-all in one click or corrects one outlier. The **confirmed map is authoritative** (also retires the image classifier's name-guess fragility). |
| 2 | v1 modality scope | **Chat** (already works), **Image** (folded into the framework), **TTS** (`/v1/audio/speech`), **STT** (`/v1/audio/transcriptions`, file-transcribe only). |
| 3 | Deferred | **Embeddings** (guard-locked `EMBEDDING_MODELS` table + full-corpus re-embed on activation), **Music** (no OpenAI-standard endpoint, no seam), **Agent/CLI** (PTY/zellij bridge to local binaries, not HTTP). |

## The key finding (from the subsystem map)

- The registry (`custom_servers.py` / `credentials/custom_models.json`) has **no `capabilities` concept today** — modality is inferred at read time by a name heuristic (`is_image_model`).
- `/validate` (`validators.py:147`) only calls `models.list()` = `GET {base_url}/models`. **No capability detection.**
- **TTS and STT already have OpenAI-shaped adapters** — `_openai_tts_chunk` POSTs `/v1/audio/speech` (`tts_routes.py:294`), `_openai_transcribe` POSTs `/v1/audio/transcriptions` (`file_transcribe.py:54`). A local model needs **no new protocol code** — just "reuse that adapter body with base_url/key from the registry."
- So the framework = **one new detect→confirm→persist layer** + **tiny per-modality dispatch branches**.

## Architecture (4 layers)

**Layer 1 — Classify (seed).** A single `classify_model(model_id) -> "chat"|"image"|"tts"|"stt"|"embedding"` in `custom_servers.py`, built from per-modality name-pattern allowlists (extends the shipped `is_image_model`). Default = `chat`. Zero-cost (operates on the already-fetched model list; NO endpoint probing that could trigger a costly model load/eviction).

**Layer 2 — Persist an authoritative map.** The registry gains a per-server **`model_modalities: {model_id: modality}`** field (and a derived per-server `capabilities` set). Added to `add_server`, `_PATCHABLE_FIELDS`, `_validate_field_types`.

**Layer 3 — Detect + confirm at registration.** `/validate` returns the classified seed map alongside the model list; the wizard (`Portal/onboarding/steps/api_keys.js`) renders a modality dropdown per discovered model (pre-filled from the seed); on Add/Save the confirmed map persists. **Single web surface** — Android loads the wizard in its WebView, so no separate native build.

**Layer 4 — The unified resolver + registrars.** One `model_modality(server, model_id)` lookup — **persisted map first, name-pattern fallback** (so servers registered before this feature still work). Each modality's registrar uses it:
- **Chat** — `_fetch_custom_models` filters to `modality == "chat"` (generalizes today's `is_image_model` exclusion; also parks `embedding` models out of the chat picker).
- **Image** — already built; `resolve_image_server` / `_local_image_available` / the chat filter switch to the unified resolver (name-pattern becomes fallback).
- **TTS** — new: `resolve_tts_server()` + an `elif provider == "local"` branch in `tts_batch` (reuses `_openai_tts_chunk`'s body) + a dynamic `"local"` group in `build_tts_catalog`.
- **STT** — new: `resolve_stt_server()` + an `if provider == "local"` branch in `transcribe_bytes` (reuses `_openai_transcribe`'s body) + `"local"` in `build_stt_catalog` / `stt_availability` / `resolve_stt_provider`.

## Two design details (validated)

- **TTS voices:** a local `/v1/audio/speech` model advertises no voice list. Probe an optional `GET {base_url}/audio/voices`; if absent, register **one default voice per local TTS model** (nameable in the confirm step). The `voice` param is passed through as chosen.
- **STT is file-transcribe only in v1:** live `/ws/stt` streaming needs the OpenAI *realtime WS* protocol, which local whisper.cpp servers almost never speak. File transcription (`transcribe_bytes`) works everywhere; streaming is a fast-follow.

## Backward compatibility

The unified resolver falls back to name-pattern when `model_modalities` is absent, so the already-registered `gemma-box` server (and the shipped image provider) keep working with no re-validation. Re-validating a server refreshes its seed map for confirmation.

## Non-goals / deferred

- Embeddings routing (needs a `/v1/embeddings` dims-probe + a dynamic entry into the guard-locked `EMBEDDING_MODELS` table + a full-corpus re-embed on activation — the single most expensive auto-ingest action; deliberately out).
- Music generation (no OpenAI-standard endpoint; would need a whole new vertical).
- Agent/CLI (terminal binaries over a PTY bridge; no HTTP hook point).
- Live streaming STT for local models.
- Endpoint-capability probing (name-classification of the model list already infers capability; a zero-cost `OPTIONS` probe is a possible future seed-improver).

## Risks & mitigations

- **Name-classifier false positives** → the confirm step makes the map user-authoritative; misclassification is a one-click fix, not a silent failure.
- **A model mis-classified as a capability the server lacks** → the first call fails loudly (adapter `raise_for_status`), never a silent empty success.
- **TTS/STT dispatch are hardcoded `if/elif`** → adding a `"local"` branch is additive and small (the adapter body already exists); no refactor of the existing providers.
- **MCP lean-venv** → all registry/classifier helpers stay stdlib-only; any cross-module use in `availability.py` stays lazy + fail-soft (per the shipped image pattern).
