# On-device Gemma — production-hardening implementation plan

> **For Claude:** Execute via superpowers:subagent-driven-development (implementer →
> spec review → code review per task), worktree only, explicit-path commits,
> `Co-Authored-By: Claude Opus 4.8`. Research + rationale: companion
> `2026-06-16-on-device-production-hardening-design.md` (Edge Gallery ground truth +
> file refs). Decisions LOCKED there. Branch `feat/local-gemma-impl`.

**Goal:** Bring the on-device Gemma agent to production quality, matching Google Edge
Gallery's proven patterns — warm/pinned model, per-model config, reliable agent-loop
termination (hybrid native tool-calling), vision, a Gallery-style model installer,
E4B-default selection, and clean device-attestation — so the snapshot-ledger stage
(W8, next) slots in cleanly.

**Decisions:** pinning = warm-while-app-open; agent loop = HYBRID (phone/intent =
fixed native tool-calling; cloud vault = manual tiering); one plan, dep order.

**Android module:** `…/AI_BlackBox_Portal/` (gradlew). Unit gate:
`./gradlew :app:testDebugUnitTest --offline`; build gate: `:app:assembleDebug`.
Device-validate on the Fold 6 per `feedback-ondevice-device-test-method` (reinstall
resets a11y → user re-enables; warm `:9099` worktree backend + repoint origin; restore
after). Host-JVM caveat: litertlm classes are Java-21 → pure cores take primitives.

---

## W0 — Decouple intent actions from accessibility (Gallery parity; do EARLY)
**Why:** Edge Gallery's intent actions need ZERO accessibility — `startActivity`/
`CameraManager` work from any Context. OUR 16 intents currently fire through the
AccessibilityService Context (`IntentActuator(service: () -> BlackBoxA11yService?)`),
so they wrongly require a11y on (and return "accessibility service not enabled" when
off). Brandon: a user shouldn't have to grant accessibility just to turn on the
flashlight. Accessibility must be needed ONLY for the gesture/vision layer (read_screen
/tap arbitrary UI).
- **W0.1** Give `IntentActuator` an Application `Context` seam (app-context holder /
  inject `appContext`) and fire intents + `CameraManager` through it — NOT the a11y
  service. Keep `FLAG_ACTIVITY_NEW_TASK` (non-Activity context). The autonomy gate +
  leak discipline are unchanged.
- **W0.2** `AndroidPhoneController.fromService` supplies the app context to the
  IntentActuator (the gesture Actuators still use the a11y service; intents no longer
  do). Net: the 16 intent actions work with the a11y service OFF; accessibility becomes
  opt-in for gestures/vision only.
- **W0.3** Tests: IntentActuator no longer returns "not enabled" when a11y is off
  (fake app context); device-validate flashlight fires with the a11y service disabled.
  Update the IntentActuator KDoc (remove the v1 a11y-context forward-note — now done).

## W2 — Per-model config (foundation; do FIRST)
Gallery ref: `data/Model.kt` (`llmMaxToken`, `llmSupportImage`, `configs/configValues`,
`getIntConfigValue`), `ui/llmchat/LlmChatModelHelper.kt` (ConfigKeys), `data/Config.kt`.

- **W2.1** Extend the on-device model descriptor (the sidecar `*.json` +
  `LocalModelManager`/`InstalledModel`) with per-model fields: `maxTokens` (default
  16384), `supportImage` (bool), `samplerTopK/topP/temperature` (optional),
  `recommended` (bool), `contextNote`. PURE parse/merge + tests.
- **W2.2** Thread the descriptor into `LiteRtEngine` construction: replace the single
  `DEFAULT_MAX_TOKENS` use with the per-model `maxTokens` (keep 16384 as the fallback
  default); add `supportImage` → `EngineConfig(visionBackend = GPU)` plumbing (no
  behavior change until W4). Wire a `SamplerConfig` from the descriptor in
  `createConversation` (mirrors Gallery). Tests for the pure config→config mapping.
- **W2.3** Keep `ChatViewModel`/wiring reading the descriptor when constructing the
  engine. Build + unit green.

## W6 — Model selection (rides on W2; small)
- **W6.1** In the model catalog/allowlist (W5 builds the full UI; here just the data):
  mark **gemma-4-E4B = recommended default**; mark **E2B = "experimental — weaker at
  multi-step agent loops"** (not recommended). Surface `recommended`/`contextNote` from
  W2.1. Picker (W5) renders it. Tests on the catalog flags.

## W1 — Preload / keep-warm (warm while app open)
Gallery ref: `model.instance` warm engine; `initialize()` separate from inference.
- **W1.1** Add `LiteRtEngine.warmUp()` (idempotent `load()` on a background scope) +
  an `isWarm`/readiness `StateFlow`.
- **W1.2** Trigger warm-up when the chat/app opens for the `local` provider (e.g.
  `ChatViewModel.init`/first composition or `NativeMainActivity.onCreate`) on
  `Dispatchers.IO`, NOT lazily on first send. Keep warm for the app lifetime; release
  in `onCleared`/app close (existing `close()`).
- **W1.3** Surface readiness in the UI (the provider pill / a small "model ready"
  state) so the user sees warm vs loading; first-send no longer eats the ~75s.
- **W1.4** Ensure the warm engine is the SAME instance the agent loop uses (singleton
  seam already exists). Tests: readiness state transitions (pure/VM-test where feasible);
  device-validate first-send latency.

## W3 — Hybrid agent loop (highest reliability leverage; SPIKE first)
Gallery ref: `customtasks/agentchat/*` (native tool-calling, structured
`{"status":…}` results), `LlmChatModelHelper` (`automaticToolCalling`,
`enableConversationConstrainedDecoding`), litertlm `OpenApiTool.execute`.
Root cause being fixed: manual FcLoop feeds tool results as PLAIN TEXT + re-advertises
tools → small model repeats calls / never sees "done".

- **W3.0 SPIKE (design + device experiment, timeboxed):** Confirm litertlm
  `automaticToolCalling = true` with `OpenApiTool.execute` callbacks works on-device:
  register 1-2 phone tools (e.g. `flashlight_on`, `show_map`) as native tools whose
  `execute` calls the actuators in-process; verify the engine drives the loop, executes
  the tool, and emits a single clean `onDone` (no repeat). Document the message/turn
  shape. Decide the cloud-vault coexistence mechanism (below) from what the spike shows.
- **W3.1** Native phone-control path: a new `LiteRtEngine.generateWithToolsNative` (or
  a mode on the existing call) that builds a conversation with `automaticToolCalling =
  true` + `enableConversationConstrainedDecoding = true` and registers the resident
  phone+intent actuators as `OpenApiTool`s whose `execute(args)` → `PhoneController`
  /`IntentActuator` (reusing IA dispatch + the autonomy gate, which stays at the
  actuator). The engine loops + terminates; we stream its events to the UI.
- **W3.2** Cloud-vault path: KEEP the manual tiered `FcLoop` (search_tools → bridge
  execute) for remote tools. Selection: phone-control turns use the native path; a turn
  that needs cloud tools uses the manual path. Concrete coexistence (pick per spike):
  (a) always-native with a single `search_cloud_tools` native tool whose `execute`
  runs the bridge search and RETURNS results as text the model acts on via a follow-up
  native `call_cloud_tool(name,args)` tool; OR (b) route by intent — native for phone,
  manual for cloud — chosen up front. Prefer (a) if the spike shows native nesting works.
- **W3.3** Reliability hardening (applies to whichever manual path remains): structured
  tool-result framing with a `status` field (Gallery shape), an anti-repeat guard
  (drop/penalize an identical consecutive tool call), an explicit "you have the result —
  answer or call the next tool; do not repeat" system cue, constrained decoding to kill
  malformed `<|tool_call>` tokens, and a sane `maxIterations` with a terminal "stopped"
  event. Pure-core tests for the de-dupe + framing; device-validate the loop-repeat is gone.
- **W3.4** Preserve all Phase-4 safety: the autonomy gate + credential handoff stay at
  the actuator (native `execute` still goes through `AndroidPhoneController`/`Actuators`),
  and phone tools still NEVER hit the cloud bridge. Re-run the security review on the
  native path (a fresh tool-execution surface).

## W4 — Vision (Task 4.4)
Gallery ref: `runInference(images)` → `Content.ImageBytes(bitmap.toPng())`,
`EngineConfig(visionBackend = GPU)`, `Model.llmSupportImage`.
- **W4.1** `LiteRtEngine`: accept image input — add `Content.ImageBytes` to the
  conversation message when images are supplied; gate on per-model `supportImage`
  (W2) + `visionBackend`. Pure mapper test (bitmap→png bytes path is framework).
- **W4.2** Capture source: `MediaProjection` screenshot (perms already declared) when
  `read_screen` returns a thin/empty tree (Compose/WebView/games). REDACT: never
  capture while a password field is focused (reuse `isPasswordField`); a captured frame
  is ephemeral (prompt-only, not persisted to the ledger).
- **W4.3** Wire as a fallback in the phone-control loop: thin tree → offer a
  `look_at_screen` capability (or auto-attach a frame). Security review on the capture
  surface (mandatory — new sensitive surface).

## W5 — Gallery-style model installer / picker
Gallery ref: `data/ModelAllowlist.kt`, `worker/DownloadWorker.kt`,
`data/DownloadRepository.kt`, `ui/modelmanager/ModelList.kt` + `ModelManagerViewModel.kt`.
- **W5.1** An on-device-model **allowlist/catalog** (name, HF/download URL, sizeBytes,
  sha, per-model W2 config, capabilities, `recommended`). Source it from the backend
  (so it's updatable) or a bundled JSON; PURE parse + tests.
- **W5.2** A **download manager** (WorkManager `DownloadWorker` pattern): download with
  progress + resume + checksum, landing the file exactly where `LocalModelManager`
  expects it, then auto-detect. Status model: NOT_DOWNLOADED / DOWNLOADING(pct) /
  DOWNLOADED / FAILED. Tests on the pure status/progress reducer.
- **W5.3** Settings **picker UI** (`LocalModelSection`): list catalog with installed vs
  downloadable, a download button + progress bar, select-active-model, delete. E4B
  shown recommended (W6). Matches Gallery's `ModelList` UX.
- **W5.4** On selecting/installing, warm it (W1).

## W7 — Autonomy 404 / device attestation
Root cause: `/local/device/autonomy` (+ status) 404 for an UNATTESTED device
(`test_autonomy_unknown_device_404`). Backend route exists (`local_routes.py`).
- **W7.1** Wire device **attestation** (`POST /local/device/attest`) on first
  on-device use: register the device (id, operator, pubkey/attestation) in the
  `local_provider` registry so `/local/device/autonomy` + `/status` resolve.
- **W7.2** Make the autonomy toggle **locally authoritative** (AutonomyStore is the
  source of truth for the actuator gate) and the backend the **attested mirror**
  (best-effort sync; never blocks local gating). Fixes the 404 + degrades gracefully
  offline. Tests: attest→autonomy round-trip (backend tests exist); Android sync is
  best-effort.
- **W7.3** This is the device-identity foundation W8 (ledger recall + minting) builds on
  — keep the registry/attestation shape ledger-ready.

## W8 — Snapshot ledger (NEXT STAGE — design only here)
Recall (snapshot semantic-search as the on-device memory, replacing carried history)
+ minting on-device turns into the ledger. Out of scope for this plan; W1–W7 are
designed so it slots in (per-operator device attestation = identity; fresh-per-turn =
recall injects retrieved context; warm engine = fast).

## Cross-cutting
- After each workstream: build + unit green; device-validate the user-visible behavior.
- Final whole-effort code review + holistic security review (esp. W3 native exec + W4
  capture). Then snapshot + (W8 planning).
- Keep `feature-frontend-three-surfaces` in mind (Portal/Android/WebView) for any
  catalog/contract changes (additive).
