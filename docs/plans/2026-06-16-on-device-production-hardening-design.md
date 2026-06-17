# On-device Gemma — production-hardening design (Edge-Gallery-grounded)

> Brainstorming/design doc (superpowers: brainstorming → writing-plans → subagent
> execution). Branch `feat/local-gemma-impl`. Status: DESIGN — pending user
> decisions (see "Open decisions"). Captures the Edge Gallery deep-research that
> grounds the plan. Companion to the intent-actions work already landed (commits
> 102e26cc..7a1b237; flashlight + show_map device-validated 2026-06-16).

## Why we paused
The intent layer + 16K context + fresh-per-turn are device-validated (the model on
the phone fired the torch + opened Maps end-to-end). But we'd drifted into reactive
demo-mode. Before the **snapshot-ledger stage** (recall + minting) goes in, we want
the on-device model *configured correctly for production* and as close to Google's
proven Edge Gallery patterns as possible — for reliability.

## Edge Gallery research (ground truth — file refs in `google-ai-edge/gallery`)
Runtime: `runtime/LlmModelHelper.kt` (interface), `ui/llmchat/LlmChatModelHelper.kt`
(concrete), `data/Model.kt`, `data/ModelAllowlist.kt`, `worker/DownloadWorker.kt`,
`data/DownloadRepository.kt`, `ui/modelmanager/ModelManagerViewModel.kt` (52KB),
`customtasks/agentchat/AgentTools.kt`.

1. **Keep-warm / pinning.** The engine is built **once** and held on the model:
   `model.instance = LlmModelInstance(engine, conversation)`. `initialize()` is
   separate from `runInference()`; `cleanUp()` closes conversation+engine and nulls
   `instance`. So the warm engine survives across turns.
2. **Fresh-per-turn.** `resetConversation()` `conversation.close()` then
   `engine.createConversation(...)` a NEW one **on the same warm engine** — Google's
   version of what we just shipped (LOCAL_HISTORY_WINDOW_TURNS=0). Confirms our call.
3. **maxTokens is PER-MODEL config.** `maxNumTokens = model.getIntConfigValue(
   MAX_TOKENS, DEFAULT_MAX_TOKEN)`. Each model in the allowlist declares its window
   + sampler (topK/topP/temperature via `SamplerConfig`). We hardcoded 16384.
4. **Vision is first-class.** `EngineConfig(visionBackend = GPU)` at init (per
   `supportImage`) + `runInference(images: List<Bitmap>)` →
   `Content.ImageBytes(bitmap.toPng())`. That's the entire 4.4 path. `Model.llmSupportImage`
   is a per-model flag.
5. **Reliability levers we don't use:** `ExperimentalFlags.enableConversationConstrainedDecoding`
   (structured/constrained decoding — likely fixes malformed `<|tool_call>` emissions);
   speculative decoding (`Capabilities(modelPath).hasSpeculativeDecodingSupport()`, opt-in flag).
6. **Agent loop / done-detection.** Gallery tools return structured
   `{"status":"succeeded"|"failed", "result"/"error":…}`; the loop is driven by
   litertlm **native tool-calling** (engine loops, emits `onDone`). OUR `FcLoop` sets
   `automaticToolCalling=false` and feeds results back as PLAIN-TEXT `Tool:` turns
   while re-advertising tools → the small model re-calls / never sees a clean "done"
   (the symptom Brandon observed). **Root-cause hypothesis for the loop-repeat.**
7. **Model manager UX.** `data/ModelAllowlist.kt` (the catalog: name, downloadUrl,
   sizeBytes, per-model config + capabilities), `worker/DownloadWorker.kt`
   (WorkManager download w/ progress + resume), `data/DownloadRepository.kt`,
   `ModelManagerViewModel` (download status: NOT_DOWNLOADED / DOWNLOADING(progress) /
   DOWNLOADED / FAILED), `ui/modelmanager/ModelList.kt` (the picker UI). HF-gated
   models carry an `accessToken`.

## Our current state + the gaps (per workstream)
- **W1 Lifecycle/pinning.** We already keep the engine warm (singleton `LiteRtEngine`,
  `load()` idempotent). GAP: load is LAZY on first turn (~75s wait); no preload, no
  warm-on-app-open, no idle release. The "another model calls this model as a tool"
  path needs it pre-warm.
- **W2 maxTokens/config.** DONE-ish: global `DEFAULT_MAX_TOKENS=16384` (commit 7a1b237).
  GAP: not per-model; no sampler config surfaced; no constrained/speculative decoding.
- **W3 Agent-loop reliability.** GAP: plain-text tool-result feedback + re-advertised
  tools → loop-repeat / no done. Candidate fixes below.
- **W4 Vision (Task 4.4).** GAP: not built. Clear Gallery path (Content.ImageBytes +
  visionBackend). Screenshot source = MediaProjection (we already hold the perms).
- **W5 Model manager / picker.** We have `LocalModelManager`/`LocalModelApi`/
  `LocalModelSection`. GAP vs Gallery: no in-app allowlist-driven picker with
  download+progress+installed/downloadable states landing the file where BlackBox needs it.
- **W6 Model selection.** GAP: stop recommending E2B (weak at agent loops); E4B = default.
- **W7 Autonomy 404 + device attestation.** `/local/device/autonomy` 404s for an
  unattested device. GAP: device attestation flow not wired; must route cleanly
  before the ledger stage.
- **W8 Snapshot ledger (NEXT stage, not now).** Recall (snapshot semantic-search as
  the memory) + minting on-device turns. Design W1–W7 so this slots in cleanly.

## Proposed design (per workstream)
- **W1:** Preload the engine when the app/chat opens (a `warmUp()` on a background
  scope), keep it warm while the app is alive; optional idle-release after N minutes
  (RAM). Surface readiness in the UI ("model ready"). Decision needed (see below).
- **W2:** Move max-tokens + sampler into per-model config (Gallery's ConfigKeys
  pattern); keep 16384 as the E4B default. Add constrained decoding (W3).
- **W3 (highest reliability leverage):** EITHER (a) adopt litertlm native
  `automaticToolCalling` (engine drives the loop, closest to Gallery) OR (b) keep our
  tiered FcLoop but (i) feed native tool-response framing + a `status` field, (ii) add
  an explicit "you have the result — answer or call the next tool, don't repeat" system
  cue, (iii) enable constrained decoding to kill malformed tool-call tokens, (iv) lower
  maxIterations + de-dupe identical consecutive calls. Decision needed.
- **W4:** `MediaProjection` screenshot → `Content.ImageBytes` when `read_screen`
  returns a thin/empty tree; per-model `supportImage`; REDACT (never capture while a
  password field is focused). Security review on the capture surface.
- **W5:** Allowlist-driven model picker in settings (Gallery pattern): catalog with
  installed/downloadable state, download via WorkManager with progress, land the file
  where `LocalModelManager` expects it, auto-detect. E4B recommended; E2B labeled.
- **W6:** Default/recommend E4B; mark E2B "experimental / weaker at multi-step."
- **W7:** Wire device attestation (`/local/device/attest`) so `/local/device/autonomy`
  + status resolve; keep the autonomy toggle locally authoritative (AutonomyStore) with
  the backend as the attested mirror. Routes cleanly into W8.

## Decisions (LOCKED 2026-06-16)
1. **Pinning** = **warm while app open** — preload the engine on app/chat launch,
   keep it resident the whole time the app is alive (instant every request, incl.
   model-as-a-tool); release on app close. No idle-release in v1.
2. **Agent loop** = **Hybrid** — register the ~24 resident phone/intent actuators as a
   FIXED native-tool-calling set (litertlm `automaticToolCalling` with OpenApiTool
   `execute` callbacks → PhoneController/IntentActuator in-process), so the ENGINE
   drives the phone-control loop + emits a clean `onDone` (fixes the loop-repeat).
   Keep the manual tiered FcLoop (search_tools discovery → bridge execute) ONLY for
   the broader remote cloud tool vault. The two coexist (see plan W3).
3. **Execution** = **one plan, strict dependency order** (W2 config → W6 selection →
   W1 preload → W3 hybrid loop → W4 vision → W5 picker → W7 attestation; W8 ledger
   next stage). See `2026-06-16-on-device-production-hardening-PLAN.md`.

## Method
Finalize this design with Brandon's decisions → `writing-plans` (bite-sized,
TDD, Gallery file refs per task) → subagent-driven execution (implementer → spec
review → code review) → device-validate → snapshot. Fold in remaining set task
(4.4 vision lands as W4).
