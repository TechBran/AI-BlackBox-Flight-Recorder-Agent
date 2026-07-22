# On-Box Audio (STT + TTS) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Complete + validate the on-box audio phase — download-button-driven Whisper STT (best model that fits the GPU) and the full Qwen3-TTS surface (TTS + 3s clone + text-described design) as llama-swap `:9098` audio-group members — layered strictly additively over cloud + the existing Kokoro/Speaches LAN pipeline, with a provider-first two-step voice picker on all three surfaces.

**Architecture:** This is a COMPLETION phase — M5–M8 of the local-model-stack plan are already coded and committed on `main` (`511e3593..8656ec04`). The stack is inert on the dev box (no `[local_models]` section) and runs on MS02 (RTX 2000 Ada 16 GB). See the grounded design doc `docs/plans/2026-07-22-onbox-audio-tts-stt-design.md` for every attach-point line-ref. This plan closes the 7 remaining gaps + runs the GPU gates.

**Tech Stack:** FastAPI (Orchestrator), llama-swap audio group (Speaches static `:9099` + in-repo `qwen_tts_server`), HF `snapshot_download`, vanilla-JS onboarding steps + Portal modules, Jetpack Compose (Android), pytest + node `.render.test.mjs`.

**Additive invariant (non-negotiable, verified per task):** every on-box surface is fail-open/inert without the local stack; cloud + LAN `local:` paths run byte-for-byte unchanged; `onbox` ranks above custom-server `local` but below cloud tie-breaks and any explicit credentialed pick. Full checklist: design doc §7.

**User decisions folded in:** best-fit Whisper auto-selected (no manual dropdown in phase 1); full Qwen3-TTS surface; per-variant + whisper download buttons; **provider-first two-step voice picker** (chosen 2026-07-22); keep cloud group order; pin repo-ids + streaming fork at G3 on MS02.

**Milestone split:**
- **Dev-box buildable + inert (M-A…M-E)** — build + unit-test here, push, they stay dark until a box has the stack.
- **MS02-only (M-F, M-G)** — real HF repo-id pin, streaming-fork install, and the G3/G4/G5/G6 GPU gates.

---

## M-A: Backend download manifest + per-artifact status

**Context:** `Orchestrator/localstack_downloads.py:58 DOWNLOAD_MANIFEST` has ONE audio key `qwen-tts` (bundled 3-variant hf_snapshot into `_qwen_tts_model_dir()`), no whisper. `_stream_hf_snapshot` (`:210`) hardcodes its dest to `_qwen_tts_model_dir()`. `local_models_routes.py:60-77` builds one status row per MEMBER; `downloadable` = `m["model"] in DOWNLOAD_MANIFEST`.

### Task A1: Generalize `_stream_hf_snapshot` for a per-artifact dest

**Files:**
- Modify: `Orchestrator/localstack_downloads.py` (`_stream_hf_snapshot` ~210, add a dest resolver near `_qwen_tts_model_dir` ~34)
- Test: `Orchestrator/tests/test_localstack_downloads.py`

**Steps (TDD):**
1. Write a failing test: a manifest artifact carrying `dest_dir="speaches_cache"` streams into the Speaches HF cache dir (monkeypatched tmp), a Qwen variant streams into `weights/qwen3-tts/<variant>`.
2. Add `_speaches_cache_dir()` sibling of `_qwen_tts_model_dir()` (reads `HF_HOME`/Speaches cache env, else a documented default under the localstack root).
3. Add a per-artifact `dest_dir` resolver: manifest entries may name a dest bucket (`qwen_variant:<name>` → `weights/qwen3-tts/<name>`, `speaches_cache` → `_speaches_cache_dir()`); default preserves today's `_qwen_tts_model_dir()`.
4. Thread it through `_stream_hf_snapshot`; keep the existing bundled-`qwen-tts` behavior byte-identical (regression test).
5. Run tests; commit.

### Task A2: Split `qwen-tts` into per-variant keys + add the `whisper` key

**Files:**
- Modify: `Orchestrator/localstack_downloads.py` (`DOWNLOAD_MANIFEST` ~58-88)
- Test: `Orchestrator/tests/test_localstack_downloads.py`

**Steps:**
1. Failing test: `DOWNLOAD_MANIFEST` contains `qwen-tts-base`, `qwen-tts-custom-voice`, `qwen-tts-voice-design` (each single-repo hf_snapshot → its variant dir) and `whisper` (two CT2 repos → `speaches_cache`). Repo ids are the current placeholders with an explicit `"repo_pending_g3": True` marker so a shipped-but-unpinned key is machine-detectable and the UI can disable the button with a clear reason instead of 404-ing.
2. Add the four keys; keep a bundled `qwen-tts` convenience key (all three variants) OR retire it per design D-2 — retain it, marked `bundled: True`, so existing status rows don't vanish.
3. Wire each variant dir to match `variant_manager.backend.load(variant, model_dir)`.
4. Run tests; commit.

### Task A3: Persist download-state at new terminal-success points

**Files:**
- Modify: `Orchestrator/localstack_downloads.py` (success branches of the hf_snapshot streamer)
- Test: `Orchestrator/tests/test_localstack_downloads.py`

**Steps:** multi-file artifacts fail `_member_gguf_present` (`local_stack.py:230`), so their "downloaded" truth lives only in `Manifest/local_models/downloads.json`. Call `local_stack.record_download_state(<artifact-key>, state="downloaded")` at each new terminal success (lazy import, per the existing pattern). Failing test asserts state persisted; commit.

### Task A4: Per-artifact rows in `/local-models/status`

**Files:**
- Modify: `Orchestrator/routes/local_models_routes.py` (models loop ~60-77)
- Test: `Orchestrator/tests/test_local_models_routes.py`

**Steps:**
1. Failing test: `GET /local-models/status` enumerates the 3 Qwen variants + whisper as **artifact children** under the `qwen-tts`/`speaches` members, each with `{key, label, downloadable, downloaded, size_gb, repo_pending_g3}`.
2. Extend the loop to emit `artifacts: [...]` per audio member from the manifest keys mapped to that member (map: whisper→speaches; qwen-tts-*→qwen-tts). Keep the member-level `downloadable` for back-compat.
3. Assert inert-when-off (no `[local_models]` → members absent → no artifact rows). Commit.

---

## M-B: GPU-fit "best Whisper" selection

**Context:** `local_stack.py:354-355 ONBOX_STT_STREAM_MODEL/ONBOX_STT_BATCH_MODEL` are fixed constants; consumers read `stt_stream_model()`/`stt_batch_model()`. User wants "best that fits the GPU," auto.

### Task B1: Hardware-probe best-fit, fresh-read sidecar

**Files:**
- Modify: `Orchestrator/local_stack.py` (add a best-fit resolver; `stt_stream_model()`/`stt_batch_model()` read it live)
- Reference: `Orchestrator/rerank.py:103` (RERANK_MODELS), hardware probe `hardware.probe().vram_mb`
- Test: `Orchestrator/tests/test_local_stack_stt_fit.py` (new)

**Steps (TDD):**
1. Failing tests: a 16 GB GPU → stream `deepdml/faster-whisper-large-v3-turbo-ct2` + batch `Systran/faster-whisper-large-v3`; an 8 GB GPU → a smaller int8 tier; CPU-only → int8 small/base. Selection is read from a small `WHISPER_FIT` table keyed by probed VRAM.
2. Store the resolved ids in a fresh-read sidecar (mirror the reranker's fresh-read discipline) so a wizard change (future) needs no restart; today it's derived purely from the probe.
3. `stt_stream_model()`/`stt_batch_model()` return the fit-resolved ids (fall back to today's constants if the probe fails). Commit.
4. **Additive check:** when the stack is off, these functions are never called by any live path (cloud STT unaffected) — assert.

---

## M-C: Wizard two-card audio section + download buttons

**Context:** `Portal/onboarding/steps/local_models.js:207 renderCapRow` renders ONE download control per capability — can't express 4 audio downloads. `embeddings.js` is the proven two-card, in-card-multi-download template.

### Task C1: Dedicated audio two-card section

**Files:**
- Modify: `Portal/onboarding/steps/local_models.js` (add an audio section; reuse `startDownload` ~266 NDJSON)
- Reference: `Portal/onboarding/steps/embeddings.js` (two-card shell ~69-134, in-card download btn ~698, `startEmbDownload` NDJSON ~1033)
- Modify: `Portal/onboarding/onboarding.css` (reuse `ob-emb-*`/add `ob-audio-*`)
- Test: `Portal/onboarding/steps/audio_section.render.test.mjs` (new)

**Steps:**
1. Failing render test: the audio section renders **two cards** — STT (Whisper: one download button + a "best fit for your GPU: <model>" note, no manual dropdown) and TTS (three per-variant Qwen download buttons + labels/sizes), each button wired to `startDownload({artifact:<key>})`; a `repo_pending_g3` artifact renders a disabled button with "pinned during first GPU bring-up," not a live 404 button.
2. Build the section consuming the A4 artifact rows; inert when the stack is off (assert no cards).
3. `node --check`; onboarding steps load via dynamic import (no `?v=` bump needed — note it). Commit.

---

## M-D: Provider-first two-step voice picker (web + Android + WebView)

**Context (user-chosen UX):** replace the single grouped 100+-voice dropdown with **provider select → voice select**. Preserve the `provider:voice` value contract, default voice, change events, and the fail-open static fallback everywhere. Cloud group order unchanged; on-box appended.

### Task D1: Web two-step picker

**Files:**
- Modify: `Portal/index.html` (~534-612 — the `#ttsVoiceSelect` + static optgroup fallback → a provider `<select>` + a voice `<select>`)
- Modify: `Portal/modules/tts-stt.js` (`populateVoiceCatalog` ~1892 → populate provider select from group ids, voice select from the chosen group; keep exported signature + `selectId` behavior)
- Test: `Portal/modules/tts-stt.voicepicker.test.mjs` (new)

**Steps (TDD):**
1. Failing test: given a `/tts/catalog` with N groups, the provider select lists the N providers; selecting a provider populates the voice select with only that provider's voices; the emitted value is still `provider:voice`; the default resolves to `TTS_DEFAULT_VOICE`'s provider+voice; an unreachable catalog keeps a static fallback (both selects).
2. Refactor `populateVoiceCatalog`: build provider `<select>` from groups, wire an onchange that fills the voice `<select>` from the selected group, restore the previously-selected `provider:voice`, re-emit the `change` event on `#ttsVoiceSelect` (or its replacement) so every existing listener keeps working. Preserve the `selectId` fast-path (added voice → auto-select its provider+voice).
3. Keep `#ttsVoiceSelect` as the canonical value holder (hidden or the voice select itself) so downstream code reading `#ttsVoiceSelect.value` is untouched — minimize blast radius.
4. `node --check`; bump `index.html ?v=genui323`. Commit.

### Task D2: Android two-step picker

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/…/ui/settings/SettingsSheet.kt` (~435-495 single grouped dropdown → provider dropdown + voice dropdown)
- Modify (if needed): the ViewModel exposing `voiceGroups` (`~446`) to also expose a selected-provider state
- Test: an Android unit test for the provider→voice derivation (`./gradlew :app:testDebugUnitTest --offline`)

**Steps:** Replace the single `DropdownMenu` that flattens `allVoiceGroups` with two: provider (`allVoiceGroups.map { it.label/id }`) then voice (`selectedGroup.voices`). Preserve `generateWithVoice` provider contract, D10 slow-first-byte affordance, and the `provider:voice` persisted preference. **Keep the offline `TTS_VOICE_GROUPS` fallback cloud-only** (qwen is dynamic-catalog-only — design §1/§7 require it ABSENT from the compiled-in fallback so a stack-less/offline box never advertises non-functional on-box voices; the committed `QwenVoiceRoutingTest.offlineFallback_hasNoQwenGroup` guards this; whisper is STT-only and produces no voices). The two-step picker surfaces `qwen` automatically whenever the live `/tts/catalog` provides it. Unit-test the derivation; commit. (Device validation is a manual follow-up — list in manual_steps.)

### Task D3: WebView + fallback parity

**Files:**
- Verify: `WizardWebViewScreen.kt` inherits the web two-step (no native port); the chat-TTS WebView picker inherits `tts-stt.js`.
- Modify: any Android/web static fallback list to the two-step shape.

**Steps:** Confirm no residual flat/single-dropdown list on any surface; document the three-surface parity in manual_steps for on-device verification. Commit if edits needed.

---

## M-E: STT choke-point completeness verification

### Task E1: Audit every STT consumer honors `STT_PROVIDER=onbox`

**Files:**
- Inspect: `Orchestrator/stt/resolve.py`, `file_transcribe.py:40`, `stt_ws_routes.py:172`, `catalog.py:57`, the `/stt`, `/stt/json`, `/stt/translate` routes, the ToolVault `speech_to_text` enum, and the Gemini/Grok Live-voice bridges + telephony (µ-law) paths.
- Test: `Orchestrator/tests/test_stt_resolver_coverage.py` (new) — a parametrized test asserting each batch/stream entry point dispatches through `resolve_stt_provider`.

**Steps:** Identify any consumer that bypasses `resolve_stt_provider` (design doc flags Live-voice/telephony as suspect). For each bypass: either route it through the resolver (preferred) or document why it is provider-pinned (e.g. Gemini Live is inherently Gemini). Add the coverage test. Commit. This is the concrete guarantee behind "switch the STT endpoint → everything uses it."

---

## M-F: MS02 bring-up — repo-id pin + streaming fork + G3 (TTS)

> Runs on MS02 only (RTX 2000 Ada). Blocks the streaming-fork ship and the button un-gating.

### Task F1: Pin real Qwen3-TTS HF repo ids
Verify the actual `Qwen/Qwen3-TTS-1.7B-{Base,CustomVoice,VoiceDesign}` (or correct) repo ids resolve on MS02; replace the placeholders + clear `repo_pending_g3`. Commit from the dev box after confirming on MS02.

### Task F2: Install the streaming inference fork
`LocalModels/qwen_tts_server/requirements.txt:8-10` names `kunzite-app/Qwen3-TTS-streaming` in a comment only → `TorchQwenBackend` ImportErrors. Confirm `load_variant()`/`stream_generate_pcm()`/`design_previews()` signatures via `smoke_gpu.py`, then add a pinned `git+https://…@<commit>` line (single source of truth) + installer pip step. Keep `QWEN_TTS_STREAMING` default-OFF until G3 passes.

### Task F3: G3 gate — TTS synthesis
Harness `diagnostics/localstack/tts_rtf.py` + `smoke_gpu.py` → `eval/results/2026-07-*-g3-tts.json`. PASS = fork confirmed + preset WAV round-trip; RTF per variant (streaming variant < 0.9 — expect the 1.7B to fail → default streaming to a 0.6B build, 1.7B batch-only); first-packet latency; sample-rate-read-from-output; FREE-BEFORE-LOAD holds (no OOM ping-pong, `QWEN_TTS_MIN_FREE_MB=5000`); clone + design round-trip on real weights.

---

## M-G: MS02 — G4 (STT) + G5/G6

### Task G1: G4 gate — STT streaming parity
Harness `diagnostics/localstack/stt_parity.py` → `eval/results/2026-07-*-g4-stt-parity.json`. PASS = latency/quality parity (turbo-ct2 stream) vs the gemma-box path in Portal + Android mic flows; `/v1/realtime` event schema captured from the live pre-1.0 server; D10 affordance (`stt_status{loading_models}`, ~30 s ceiling then `stt_error`, never a silent cloud switch); bridge mechanics intact (24 kHz resample, trailing-silence stop, hallucination filter, `stt_done`); whisper prefetch via the download button (no invisible first-use pull).

### Task G2: G5 swap latency + G6 eviction safety
Run the committed harnesses: G5 audio↔retrieval cross-group swap (~5–8 s first voice turn after a search) and G6 streaming-STT eviction safety under D12 serialization. Record to `eval/results/`.

---

## Final

- Dispatch a final code reviewer over the whole audio completion diff (additive-preserve checklist §7 as the rubric).
- `/snapshot-dev` documenting the completion + gate results (supersede/reference the design-doc snapshot).
- Deploy: push → pull on MS02 → restart `blackbox.service` + `blackbox-models.service` → re-run G3/G4 → confirm live TTS synthesis + STT streaming end-to-end.
