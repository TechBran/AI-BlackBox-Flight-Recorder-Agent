# On-Box Audio (STT + TTS) — Design Doc

**Date:** 2026-07-22
**Status:** Design (grounded consolidation of the 9-cartographer surface map)
**Scope:** On-box Whisper STT (Speaches/faster-whisper) + full Qwen3-TTS surface (normal TTS + 3s zero-shot clone + text-described voice design) as llama-swap `:9098` audio-group members, layered additively over cloud + the existing LAN custom-server (Kokoro/Speaches) audio path.
**Related plans:** `docs/plans/2026-07-20-local-model-stack-design.md` (§5.3 STT, §5.4 TTS, §7 tiers, §8 install), `docs/plans/2026-07-20-local-model-stack-implementation.md` (M0/M5/M6/M7/M8, G3/G4 in M10), `docs/plans/2026-07-22-zero-terminal-onboarding-flow.md` (§4.4 audio jobs, §7 gaps), `docs/plans/2026-07-13-local-audio-streaming-stt-tts.md` (the SHIPPED LAN-audio baseline that must remain working).

---

## 0. Reality check — this is a COMPLETION phase, not a greenfield build

The 9 cartographers agree: nearly all of M0/M5/M6/M7/M8 is already coded and **committed to `main`** (commits `511e3593..8656ec04`, clean tree, 0 ahead of origin). What is LIVE in the tree today:

- **STT** — `Orchestrator/stt/resolve.py::resolve_stt_provider` (line 82) ranks the `onbox` token above custom-server `local`; batch (`file_transcribe._onbox_transcribe`) and streaming (`stt_ws_routes._onbox_bridge`, Design-B direct WS to `:9099`) are wired with the D10 loading affordance and D12 serialization.
- **TTS** — `Orchestrator/qwen_tts.py` seam, `qwen:` branches in `POST /tts` (line 244) and `POST /tts/batch` (line 469), the fail-open `qwen` catalog group, clone/design proxy via `/upstream/qwen-tts`, and the whole in-repo `LocalModels/qwen_tts_server/` FastAPI member (3 variants, FREE-BEFORE-LOAD, consent-gated clone, 2-step design).
- **Voice-list revamp** — `Portal/modules/tts-stt.js::populateVoiceCatalog` (line 1892) rebuilds `#ttsVoiceSelect` as `<optgroup>` per provider; Android `TtsRepository.fetchCatalog` parses `VoiceGroup`; Voice Lab has a Qwen zone.
- **Wizard** — `local_models` step (STT+TTS rows), `transcription` step (onbox STT card), `POST /local-models/capability` mirroring `STT_PROVIDER`.

**On this DEV box the stack is OFF** (no `[local_models]` section → `local_stack.master_enabled()` = False → `is_healthy()` = False), so every dynamic group is simply absent and all cloud/LAN paths run unchanged. The stack runs on **MS02** (`192.168.1.153`, RTX 2000 Ada, 16 GB).

**Therefore the real remaining work is a short, specific list** (§6, §7): the Whisper download button, per-variant Qwen download buttons, pinning the real HF repo ids + streaming fork, a GPU-fit Whisper selection, and the two GPU gates (G3 TTS, G4 STT). Everything else is verification + polish on already-shipped code.

---

## 1. Goal + Additive Invariant

### Goal (folding in the user's already-made decisions)

1. **Whisper = the best model that fits the box GPU, local on the box** — parity with the network-GPU whisper the user relies on today. Expose an explicit **download button** for it (today Speaches auto-pulls invisibly on first use).
2. **TTS = the FULL Qwen3-TTS surface** — normal TTS + 3s zero-shot clone + text-described voice design — since we hold the weights. All of it slots into the existing cloning UI (Voice Lab), adding a text-to-voice/design affordance.
3. **Download buttons for ALL Qwen3-TTS variants AND the Whisper model.**
4. **The STT endpoint switch in the wizard re-points EVERY STT consumer** through the single choke point.
5. **Selecting a Qwen voice** in the (grouped-by-provider) voice list routes to the on-box member.
6. **Strictly ADDITIVE** — keep cloud providers + the existing Kokoro/Speaches LAN pipeline.
7. **Build on the dev box → push → pull + test on MS02**, with G3 (TTS synthesis) + G4 (STT streaming) gates.

### The Additive Invariant (non-negotiable)

Every on-box audio surface is **fail-open / inert** on a box without the local stack. The `qwen` catalog group is absent, `onbox_stt_available()` is False, and every existing cloud + LAN `local:` path runs byte-for-byte unchanged. `onbox` is a NEW provider token ranked *above* custom-server `local` but *below* cloud tie-breaks and *below* any explicit credentialed user pick (D2/D9). The tree stays runnable at every commit. See the full checklist in §7.

---

## 2. Grounded Architecture

### 2.1 The two members under one front door

`Orchestrator/local_stack.py:48-57` declares `MEMBERS` — the two audio members plus retrieval:

| member | capability | group | port / route |
|--------|-----------|-------|--------------|
| `speaches` | `stt` | `audio` | **static `:9099`** (direct WS; llama-swap can't proxy WS, upstream #754) |
| `qwen-tts` | `tts` | `audio` | via `:9098` body-model auto-route + `/upstream/qwen-tts/…` |
| `embed-qwen3-8b` / `rerank-qwen3-8b` | `retrieval` | `retrieval` | via `:9098` |

The **audio group is `swap:false` (co-resident)** and **`exclusive:true` vs the retrieval group** (`installer/templates/llama-swap-config.yaml.template`). Speaches is pinned to a **static `:9099`** so the streaming STT bridge can open a raw WebSocket (`local_stack.py:349 SPEACHES_STATIC_PORT = 9099`, `:374 speaches_realtime_ws_url()`). Everything else fronts through `:9098/v1` (`base_url()`).

**D12 serialization** (`local_stack.py:419 voice_session()`, `:434 retrieval_gate()`, `:453 retrieval_gate_sync()`) sequences on-box voice streams ahead of the retrieval group so a search never fights a TTS/STT stream for VRAM. New audio code wraps `voice_session()`; retrieval consumers pass through `retrieval_gate()`.

### 2.2 STT: one choke point re-points every consumer

`Orchestrator/stt/resolve.py::resolve_stt_provider` (line 82) is **the** resolver every STT surface funnels through. The re-point mechanism:

```
Wizard STT card (transcription.js:223 choose)
  → POST /onboarding/save {secrets:{STT_PROVIDER:"onbox"}}   (transcription.js:230-233)
     └─ OR POST /local-models/capability {capability:"stt",enabled:true}
        → local_stack_routes.py:85 update_env({"STT_PROVIDER":"onbox"})
  → resolve.py:68 _fresh_stt_provider() re-reads STT_PROVIDER from .env EACH CALL (no restart)
  → resolve.py:82 resolve_stt_provider() returns "onbox" when onbox_stt_available()  (resolve.py:55)
```

Because the value is **fresh-read per request**, the single write re-points **every** consumer with no restart:

- **Batch** — `Orchestrator/stt/file_transcribe.py:40` dispatch → `_onbox_transcribe` (proxied `POST :9098/v1/audio/transcriptions`, 429 retry/backoff, `stt_batch_model()` = `Systran/faster-whisper-large-v3`).
- **Streaming** — `Orchestrator/routes/stt_ws_routes.py:128-130` resolves with `onbox_ok=onbox_stt_available()`; `:172` dispatches `_onbox_bridge` → warms the audio group via `/upstream/speaches/health`, emits `stt_status{state:"loading_models"}` (D10, ~30 s ceiling then `stt_error`, never a silent cloud switch), then opens a **direct WS to `ws://127.0.0.1:9099/v1/realtime?model=<enc>&intent=transcription`** inside `voice_session()`. Preserves 24 kHz resample, ~0.7 s trailing-silence stop, `is_whisper_hallucination` filter, per-utterance finals, terminal `stt_done`.
- **Catalog** — `Orchestrator/stt/catalog.py:57-69` appends the `onbox` provider card (streaming = `stt_stream_model()` = `deepdml/faster-whisper-large-v3-turbo-ct2`, file = `stt_batch_model()`) when `onbox_stt_available()`.

The onbox model ids are currently **hardcoded constants** (`local_stack.py:354-355`) — GPU-fit selection is the one STT gap that needs a small new surface (§6.4).

### 2.3 TTS: a Qwen voice routes through the catalog to `127.0.0.1:9098`

```
Voice picker option "qwen:Vivian"  (web optgroup / Android VoiceGroup)
  → POST /tts {voice:"qwen:Vivian"}  (or provider=="qwen")
  → tts_routes.py:244  qwen: branch (BEFORE the OpenAI default)
  → qwen_tts.py:172 synthesize()
       POST http://127.0.0.1:9098/v1/audio/speech {model:"qwen-tts", input, voice, response_format, stream}
  → llama-swap body-model auto-routes "qwen-tts" to the qwen-tts member
  → LocalModels/qwen_tts_server/app.py:120 /v1/audio/speech → VariantManager (one variant VRAM-resident)
```

- `GET /tts/catalog` (`tts_routes.py:1091`) assembles static cloud groups (`config.py:663 build_tts_catalog` → `openai` + `gemini-flash` + `gemini-pro`), then **appends** dynamic groups `elevenlabs`, `local` (custom-server), and `qwen` (`tts_routes.py:1130 qwen_tts.catalog_group()`). The `qwen` group (`qwen_tts.py:149`) = `{id:"qwen", label:"Qwen3-TTS (On-Box)", dynamic:true, voices:[qwen:<9 presets> + qwen:<saved-slug> star-prefixed]}`, **present only when `_tts_available()`** (`qwen_tts.py:67` = `is_healthy() and enabled('tts')`).
- **Non-OpenAI voice paths** (clone/design/save) go through `/upstream/qwen-tts/…` (`qwen_tts.py:82 upstream_url()`) because llama-swap does **not** body-model auto-route those.
- **WAV/PCM only** — the member has no mp3/opus encoder; `synthesize()` 400s other formats. Sample rate is **read from output**, never hardcoded 24 kHz.

### 2.4 Provider precedence (both capabilities)

Explicit credentialed pick (D9) > cloud tie-breaks > **onbox** > custom-server `local` > none. The on-box default is a **wizard-time seed**, not a runtime takeover — Brandon's recorded "ElevenLabs for STT AND TTS" is never overridden at runtime.

---

## 3. Concrete Work Per Surface

Each item cites the exact attach point. "DONE" = already committed and only needs MS02 verification; "GAP" = new work.

### 3.1 Backend — download manifest (GAP, the core remaining backend work)

`Orchestrator/localstack_downloads.py:58-88 DOWNLOAD_MANIFEST` today has ONE audio key `qwen-tts` (an `hf_snapshot` of all 3 variant repos, ~13.5 GB, one button) and **no whisper entry** (deliberately auto-pulled). Changes:

1. **Split `qwen-tts` into 3 per-variant artifacts** — `qwen-tts-base`, `qwen-tts-custom-voice`, `qwen-tts-voice-design`, each an `hf_snapshot` of one repo into `_qwen_tts_model_dir()/<variant>` (matching `variant_manager.backend.load(variant, model_dir)`). Keep the bundled `qwen-tts` key as an "all variants" convenience OR retire it (see Decision D-3).
2. **Add a `whisper`/`speaches` artifact** pulling the two CT2 repos (`deepdml/faster-whisper-large-v3-turbo-ct2` stream + `Systran/faster-whisper-large-v3` batch, from `local_stack.py:354-355`) into the **Speaches HF cache dir**, NOT `_qwen_tts_model_dir()`.
3. **Generalize `_stream_hf_snapshot`** (`localstack_downloads.py:210`) — its dest is hardcoded to `_qwen_tts_model_dir()`; add a per-artifact `dest_dir` resolver so whisper lands in the Speaches cache while Qwen variants keep landing in `LocalModels/weights/qwen3-tts/<variant>`. Add a sibling of `_qwen_tts_model_dir()` (`:34`) for the Speaches cache.
4. **Pin real repo ids** — the current `Qwen/Qwen3-TTS-1.7B-{Base,CustomVoice,VoiceDesign}` ids are **placeholders** flagged "confirmed at G3"; a download 404s until verified on MS02.
5. **Call `record_download_state`** at each new terminal-success point — multi-file artifacts fail `_member_gguf_present` (`local_stack.py:230-247`) so their "downloaded" truth depends entirely on the state file (`Manifest/local_models/downloads.json`), else status reads them pending forever.

`downloadable` surfaces **automatically** once a key exists: `local_models_routes.py:77 "downloadable": m["model"] in _dl.DOWNLOAD_MANIFEST`. (Note: today the flag is keyed **per member id**; per-variant + whisper buttons need the status payload to enumerate **artifacts**, or to map the extra manifest keys onto the `speaches`/`qwen-tts` members — see §3.2.)

### 3.2 Backend — status/download routes (mostly DONE; small extension)

`Orchestrator/routes/local_models_routes.py` — `GET /local-models/status` (`:40`) and `POST /local-models/download` (`:104`, NDJSON, 404 unknown / 507 <40 GB / 409 running) are DONE. Extension: the `models[]` loop (`:60-77`) builds one row per MEMBER; to render 4 audio download buttons (3 Qwen variants + whisper) the loop must emit **per-artifact** rows/flags (add manifest-key children under the `speaches`/`qwen-tts` members). No new route needed.

### 3.3 Backend — STT choke point (DONE; verify completeness)

`resolve.py` + `file_transcribe.py:40` + `stt_ws_routes.py:172` + `catalog.py:57` are DONE. **Verify** every STT consumer honors `STT_PROVIDER=onbox`: `/stt`, `/stt/json`, `/stt/translate` (all via `transcribe_bytes`), the ToolVault `speech_to_text` enum (M0 Task 0.2 added `local`+`onbox`), and spot-check the Gemini/Grok Live-voice bridges + telephony (µ-law) paths, which may not route through `resolve_stt_provider`.

### 3.4 Backend — GPU-fit Whisper selection (GAP)

`ONBOX_STT_STREAM_MODEL`/`ONBOX_STT_BATCH_MODEL` (`local_stack.py:354-355`) are fixed constants; every consumer reads them via `stt_stream_model()`/`stt_batch_model()`. "Best model that fits the GPU" needs a small **STT model registry + GPU-fit pick** (mirror the `rerank.py` `RERANK_MODELS` + `hardware.probe().vram_mb` pattern; `Orchestrator/rerank.py:103` registry, `:1451 model_catalog`, `:1512 status`). Minimum viable: a `hardware.probe()`-driven default (large-v3-turbo stream + large-v3 batch on a 16 GB GPU; int8 on CPU) with the two ids stored in a fresh-read sidecar/config the resolver reads live. Full: a wizard dropdown + a `/…/select` route cloned from `rerank_routes.py:46 POST /rerank/select`. See Decision D-4.

### 3.5 Backend — TTS synth + clone/design/text-to-voice (DONE)

`Orchestrator/qwen_tts.py` (`catalog_group`, `synthesize`, `upstream_url`, `list_profiles`, `delete_profile`) + `tts_routes.py:244/469` (synth branches) + `tts_routes.py:1144-1218` (`/qwen/voices` list/clone/design/design-save/delete) are DONE. The member (`LocalModels/qwen_tts_server/app.py`) implements `/v1/audio/speech`, `/v1/voices/clone` (consent!='true'→422, <3s→422), `/v1/voices/design`, `/v1/voices/design/save`. `sanitize_for_speech()` applies to qwen exactly as to every provider. Only backend gap: the **streaming inference fork** is not installed (§4).

### 3.6 Frontend web — Voice Lab clone/design/text-to-voice UI (DONE)

`Portal/voice-lab.js` Zone 5 (`vlabQwenZone`, DOM ~194-233, logic ~826-1046) already exposes clone (3s + consent), text-described design (preview → save), and manage, gated on `GET /local-models/status` health via `qwenTabAvailable()`, refreshing `populateVoiceCatalog('qwen:<slug>')` after every mutation. This is the "full surface slots into the existing cloning UI" requirement — already met. Verify design/clone round-trips on real weights (blocked on §4).

### 3.7 Frontend web — voice-list grouping (DONE)

`Portal/modules/tts-stt.js:1892 populateVoiceCatalog` rebuilds `#ttsVoiceSelect` from `/tts/catalog` as `<optgroup>` per group (`:1903 createElement("optgroup")`). The hardcoded optgroups in `Portal/index.html:536-612` are a cloud-only static fallback replaced at runtime. **Confirm** no residual flat list; decide group order (Decision D-5).

### 3.8 Frontend web — wizard audio section + download buttons (GAP)

`Portal/onboarding/steps/local_models.js`:
- `CAPS` (`:35`) already includes `stt`+`tts`; `renderCapRow` (`:207`) renders **one** Download control per capability (`modelForCap` returns the first member) — it **cannot express 4 downloads**. `isDownloadable` (`:141`) = `m.downloadable`; the `stt` row currently shows the `autoNote` (`:236`) "pulled automatically on first use" instead of a button.
- **Work:** expand the `stt`+`tts` rows into a **dedicated audio two-card section** mirroring `Portal/onboarding/steps/embeddings.js` (two-card shell `:69-134`, in-card `embDownloadBtnHtml` `:698`, `startEmbDownload` NDJSON `:1033`). STT card = whisper download + (optional) model selector; TTS card = 3 per-variant download buttons. Reuse `startDownload` (`local_models.js:266`) with per-artifact keys. Ship a `render.test.mjs` like `embeddings.render.test.mjs`.
- `Portal/onboarding/steps/transcription.js` — STT provider picker (`PROVIDERS:35` incl. `onbox:57`; `choose:223` → `/onboarding/save`) is DONE; extend the onbox card with a whisper sub-selector only if §3.4 ships a selector.

### 3.9 Frontend Android — parity (DONE; add fallback groups)

`TtsRepository.kt` `fetchCatalog()` (~278) → `List<VoiceGroup>`; `SettingsSheet.kt` (~435-495) renders group header + voices; `generateWithVoice` passes the provider generically (M0 Task 0.1), `ON_BOX_PROVIDER="qwen"`, D10 slow-first-byte affordance. Parity work: add `qwen`/whisper groups to the offline `TTS_VOICE_GROUPS` fallback; mirror any web group-order change; the Android `LocalModelSection.kt`/`ModelDownloadService.kt` download surface must gain per-variant + whisper parity with `local_models.js`.

### 3.10 Frontend WebService/WebView — parity (VERIFY)

`WizardWebViewScreen.kt` reuses the **same web wizard** (download buttons + STT picker) inside a WebView — no native port needed; it inherits §3.8 automatically. The chat-TTS picker likewise inherits the web `<optgroup>`. **Verify** the WebView surfaces the grouped catalog + Voice Lab Qwen zone identically (three-surfaces rule) and that no WebView-only flat list remains.

---

## 4. Download-Manifest Additions + Installer / llama-swap Changes

### 4.1 Manifest additions (`Orchestrator/localstack_downloads.py:58`)

| new key | kind | repos / file | dest | ~GB |
|---------|------|--------------|------|-----|
| `qwen-tts-base` | hf_snapshot | `Qwen/Qwen3-TTS-1.7B-Base` † | `weights/qwen3-tts/base` | ~4.5 |
| `qwen-tts-custom-voice` | hf_snapshot | `…-CustomVoice` † | `…/custom_voice` | ~4.5 |
| `qwen-tts-voice-design` | hf_snapshot | `…-VoiceDesign` † | `…/voice_design` | ~4.5 |
| `whisper` (speaches) | hf_snapshot | `deepdml/faster-whisper-large-v3-turbo-ct2` + `Systran/faster-whisper-large-v3` | **Speaches HF cache** | ~1.5–3 |

† repo ids are **placeholders pending G3** — pin on MS02 first bring-up before shipping the buttons.

Generalize `_stream_hf_snapshot` (`:210`) for a per-artifact `dest_dir`; add a Speaches-cache resolver next to `_qwen_tts_model_dir` (`:34`). Re-check `MIN_FREE_GB=40` (`local_stack.py:61 DISK_GATE_MB`) covers the added ~1.5–3 GB whisper.

### 4.2 Installer (`installer/templates/blackbox-install-localstack.sh`)

- **Streaming fork** — `LocalModels/qwen_tts_server/requirements.txt:8-10` names `kunzite-app/Qwen3-TTS-streaming` (`load_variant()`, `stream_generate_pcm()`) only in a **comment**; there is no installable `git+` line, so `TorchQwenBackend` (`variant_manager.py:145`) ImportErrors on first synth ("starts but cannot synthesize"). Add a **pinned `git+https://…@<commit>`** line (or an explicit `pip` step in the qwen-tts venv section, `:328-351`) **once G3 confirms the fork signatures on MS02** (Task 6.9). `smoke_gpu.py` is the manual harness.
- **Speaches whisper cache** — optionally set `HF_HOME` on the Speaches member (`:308-326`) so the wizard whisper download writes where Speaches reads; confirm the Speaches model-download API vs raw `snapshot_download`. Speaches is pinned `SPEACHES_PIN=…@v0.9.0-rc.3` (`:71`).
- **Non-fatal** — a Speaches/qwen-tts provisioning failure must leave the retrieval group + cloud paths fully functional (installer already does this).

### 4.3 llama-swap (`installer/templates/llama-swap-config.yaml.template`)

Already correct: `audio` group `swap:false exclusive:true` co-resident `speaches` (static `:9099`) + `qwen-tts` (uvicorn `qwen_tts_server.app:app`); env contract `QWEN_TTS_VENV`/`SPEACHES_VENV`/`QWEN_TTS_STREAMING`/`QWEN_TTS_MODEL_DIR`. If a specific whisper model must be pinned at the member level (best-fit), the `speaches` member cmd/env (`:68-73`, names none today) must reference it.

---

## 5. Gate Criteria (run on MS02, RTX 2000 Ada 16 GB)

### G3 — TTS synthesis (harness `diagnostics/localstack/tts_rtf.py` + `LocalModels/qwen_tts_server/smoke_gpu.py`)

Blocks the streaming-fork ship + decides the streaming variant. Write result to `eval/results/*-g3-tts.json`. PASS =:
1. **Fork confirmed** — `load_variant()`, `handle.generate()`, `stream_generate_pcm()`, `design_previews()` signatures verified against the pinned commit; a preset synth round-trips WAV.
2. **RTF** — measure real-time factor per variant. **Streaming variant must be < 0.9 RTF.** The spec pre-warns the 1.7B streaming near-certainly FAILS < 0.9 on the 2000 Ada → expect to default streaming to a **0.6B** build and keep 1.7B as batch-only. `QWEN_TTS_STREAMING` stays default-OFF (StreamingResponse-over-full-gen fallback) until G3 passes.
3. **First-packet latency** measured for the streaming path.
4. **Sample rate read from output** (never assume 24 kHz).
5. **FREE-BEFORE-LOAD holds** — variant-transition VRAM peak stays under the card; the `QWEN_TTS_MIN_FREE_MB=5000` floor (`settings.py`) is respected; no OOM ping-ponging Base↔CustomVoice↔VoiceDesign.
6. **Clone (3s zero-shot) + design (text-described) round-trip** on real weights → the `{generated_voice_id, audio_b64, sample_rate}` preview contract the UI expects.

### G4 — STT streaming parity (harness `diagnostics/localstack/stt_parity.py`)

Write to `eval/results/*-g4-stt-parity.json`. PASS =:
1. **Latency/quality parity** — onbox Speaches `large-v3-turbo` streaming vs today's gemma-box path, measured in the **Portal + Android mic flows** (partial cadence, final WER on a reference clip).
2. **`/v1/realtime` event schema captured** from the running pre-1.0-rc server (the Design-B bridge assumes an event shape that must be verified live).
3. **D10 affordance** — `stt_status{loading_models}` fires on cold audio-group warm; ~30 s ceiling then `stt_error`, never a silent cloud switch.
4. **Bridge mechanics intact** — 24 kHz resample, trailing-silence stop, hallucination filter, `stt_done`.
5. **Whisper prefetch** — a reference clip round-trips `/ws/stt` after the download button completes (no invisible first-use cold pull).

**Adjacent gates (committed harnesses, unrun):** G5 cross-group swap latency (audio↔retrieval, expect ~5–8 s first voice turn after a search), G6 streaming-STT eviction safety under D12 serialization.

---

## 6. Gap Summary (what actually remains)

1. **Whisper download button** — no manifest entry today (auto-pull); add a `whisper` hf_snapshot into the Speaches cache + flip `downloadable`.
2. **Per-variant Qwen download buttons** — split the single bundled `qwen-tts` artifact into 3 keys + render 3 buttons (new audio two-card wizard section).
3. **Real HF repo ids** — placeholders pending G3 (will 404 until pinned on MS02).
4. **Streaming fork** — comment only in `requirements.txt`; add a pinned `git+` line / installer pip step after G3 confirms signatures.
5. **GPU-fit Whisper selection** — ids hardcoded; add a hardware-probe default (+ optional wizard selector/registry mirroring `rerank.py`).
6. **G3 + G4 never run** — `eval/results/` empty of audio gates; primary remaining validation work, all on MS02.
7. **Verification** — every STT consumer honors `STT_PROVIDER=onbox` (Live-voice/telephony spot-check); WebView three-surfaces parity; Android per-variant/whisper download parity; fresh-box installer re-run.

---

## 7. Additive-Preserve Checklist (every path that must keep working)

- [ ] **Cloud TTS** — OpenAI HD, Gemini Flash/Pro (`config.py:663 build_tts_catalog` static groups), ElevenLabs (dynamic group + Voice Lab zones 1-3), xAI/Grok (Voice Lab zone 4). `qwen` is inserted as a new branch **before** the OpenAI default only (`tts_routes.py:244`); existing branches untouched.
- [ ] **Cloud STT** — OpenAI, Google chirp_2, ElevenLabs Scribe (batch + streaming + diarization). `onbox` added **above** `local`, **below** cloud tie-breaks; cloud tie-break order unchanged (`resolve.py:82`).
- [ ] **Existing LAN custom-server audio** — Kokoro TTS + gemma-box Speaches via `onboarding/custom_servers.py` (`resolve_audio`/`has_audio`, `KOKORO_VOICES`), the `local:` voice prefix / `provider=="local"` branches in `/tts` + `/tts/batch`, and the `local` STT card. This is a SEPARATE path from the on-box stack and stays exactly as today (design §2/§5.5).
- [ ] **Brandon's explicit credentialed picks** (recorded "ElevenLabs for STT AND TTS") are NEVER overridden at runtime by the on-box default (D2/D9) — the on-box default is a wizard-time seed only.
- [ ] **Active EMBEDDING model is NEVER health-switched** — audio degradation is per-capability and independent; only a wizard re-embed cutover writes `active.json`.
- [ ] **`sanitize_for_speech()`** applies to qwen exactly as to every provider (Gemini `(frantically)` cues preserved, Spanish-safe).
- [ ] **Streaming bridge mechanics** carried over verbatim by `_onbox_bridge`: 24 kHz resample, ~0.7 s trailing-silence stop, per-utterance finals, `is_whisper_hallucination` filter, `stt_done`.
- [ ] **Fail-open catalog** — cloud groups always render even when `elevenlabs`/`local`/`qwen` are absent; `populateVoiceCatalog` static fallback stands when `/tts/catalog` is unreachable.
- [ ] **Retrieval group** (embed/rerank) + D12 `voice_session`/`retrieval_gate` — audio work must not regress the audio↔retrieval exclusive swap.
- [ ] **Grouped voice picker on all 3 surfaces** (web `<optgroup>`, Android `VoiceGroup`, WebView) — extend groups, never flatten.
- [ ] **Voice profiles** under `Manifest/voices/qwen/{slug}/` persist across restarts, never in git.
- [ ] **Download singleton semantics** — `POST /local-models/download` NDJSON, 40 GB disk gate, 404/507/409 (`local_models_routes.py:104`).
- [ ] **The tree stays runnable at every commit** — every new audio surface is inert on the dev box (stack OFF).

---

## 8. Open Decisions (recommendations in the schema block)

Captured as `consolidated_decisions` for a quick user confirm — each already has a recommended default so implementation can proceed without blocking.
