# Frontier-Driven Android / Tablet / XR Device Control — Implementation Plan

**Date:** 2026-06-30 · **Status:** PLAN (awaiting approval; no code written yet) · **Design basis:** `docs/plans/2026-06-30-android-control-layer-research.md` (research + §5.5 locked decisions + §7 target design)

## Overview

Move the device-control **brain** from the on-device Gemma (~75s cold-load) to a **cloud frontier model**, keeping the phone's already-built actuation layer as the **hands**. The device (phone/tablet/XR) becomes a provider-agnostic endpoint over the existing Tailscale 8765 channel; a server-side ReAct loop drives it with hybrid tree+screenshot grounding. Gemma survives as an explicit opt-in provider behind the same seam.

**This is a rewire.** Reused unchanged: `Actuators` (tap/type/swipe/scroll/back/home), `UiTreeReader`, `ScreenCapture`, `IntentActuator`, `AndroidPhoneController`, `RemoteControlServer` (8765), `RemoteTaskHandlerHolder`, the confirm-gate / credential-handoff / password-redaction safety primitives, and the Tailscale auth + operator isolation. New: the cloud agent loop, the observation/action wire contract, streaming, per-provider coordinate adapter, origin-aware routing + device registry UI, the consent banner/kill-switch, and the tablet display-addressing fixes.

**MVP = M0 → M2** (a frontier model drives your own phone through the full hybrid loop). Then routing, safety, form-factors, and provider breadth layer on. **XR (M6) validates continuously from M1 onward** on the Galaxy XR.

### Sequencing & parallelism
```
M0 contract ──▶ M1 phone hands ──▶ M2 cloud brain (MVP) ──▶ M3 routing ──▶ M4 safety ──▶ M8 hardening/dist
                     │                                          │
                     └──▶ M6 XR (validated continuously) ───────┘
                     └──▶ M5 tablet/large-screen (parallel after M1)
                                                            M7 provider-agnostic (after M2+M4)
```
Dependency spine: M0→M1→M2 is strictly ordered (contract → hands → brain). M5 (tablet) and M6 (XR) branch off M1. M3/M4 gate the "everything available, multi-device" posture. M7 (Claude/OpenAI) needs M2+M4. M8 closes.

### Cross-cutting foundations (defined once in M0/M1, referenced everywhere)
- **Wire contract** (M0): `observation` (ui_tree + device_capability + optional screenshot), `action` (element / coordinate / global / intent), `action_result`. JSON schemas in `docs/schema/`.
- **DeviceCapabilities** (M1): `{formFactor, hasScreenshot, supportsCoordinateGesture, displayId}` — advertised per device; the loop degrades gracefully (XR = node+intent, no coordinate/screenshot).
- **Safety primitives** (already exist; extended to the remote path in M4): high-consequence confirm-gate, credential-handoff, password-field redaction, autonomy YOLO/confirm-each.
- **Distribution** (M8): sideload/enterprise only (Play prohibits autonomous a11y agents); never claim `isAccessibilityTool`; graceful fallback to the intent path if a11y is OS-revoked.

---

## M0 — Channel contract & provider-agnostic seams
**Goal:** one wire contract both sides build against; the handler seam accepts a cloud (not just Gemma) brain.
**Depends on:** none.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 0.1 | Observation schema | `docs/schema/{observation,ui_node,device_capability}.json` | `ui_tree` (array of `UiTreeReader` `UiNode`: node_id/role/text/resource_id/bounds/clickable/editable/is_password) + `device_capability` + optional base64 screenshot. **Invariant:** `is_password` node ⇒ text = `·····`. | schemas validate against real `UiTreeReader.readScreen()` output; password nodes never carry raw text |
| 0.2 | Action schema | `docs/schema/action*.json` | union: `element_click`/`element_set_text` (by resource_id/node_id → `Actuators.tap`/`type`), `coordinate_tap`/`coordinate_swipe` (→ `dispatchGesture`), `global_action` (back/home/recents → `performGlobalAction`), `intent` (→ `IntentActuator`, validated against `INTENT_ACTIONS`) | each action validates; unknown action rejected |
| 0.3 | Streaming endpoint scaffold | `…/data/remote/RemoteControlServer.kt` | add `/ws` (or SSE `/stream/{taskId}`) carrying `observation`↑ / `action`↓ / `action_result`; **keep `/task`+`/status` for Gemma back-compat**; observation cadence = tree-first, screenshot on demand | integration test streams observation to a mock client, dispatches an action; latency < poll baseline |
| 0.4 | Provider-agnostic handler | `…/data/remote/{RemoteControlServer,FrontierRemoteTaskHandler}.kt` | `RemoteTaskHandler` interface is already generic — add `FrontierRemoteTaskHandler` that bridges the 8765 channel to the Orchestrator loop; `RemoteTaskHandlerHolder.set()` swaps handlers with no socket rebind | holder swaps frontier↔Gemma without breaking routes; both `healthz()` work |
| 0.5 | Enable silent screenshot | `…/res/xml/accessibility_service_config.xml` | add `android:canTakeScreenshot="true"` (keep `canRetrieveWindowContent`+`canPerformGestures`) | Android 30+: `takeScreenshot()` succeeds without a system dialog |

**Acceptance:** schemas validate against live device output; streaming carries obs/action at < 1s/step; handler holder swaps provider without rebinding 8765; password redaction holds on the wire.

---

## M1 — Phone "hands" endpoint
**Goal:** the observation + action channels wired to the existing actuators, plus the consent surface. **XR validation (M6) starts here.**
**Depends on:** M0.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 1.1 | `DeviceCapabilities` model | `…/overlay/DeviceCapabilities.kt` (new) | `{formFactor (PHONE/TABLET/XR_HEADSET/GLASSES), hasScreenshot, supportsCoordinateGesture, displayId}` + `detect()`; emitted on every observation | phone → all-true; XR stub → coordinate/screenshot false |
| 1.2 | Observation channel | `…/overlay/{UiTreeReader,ScreenCapture,OverlayService}.kt` | serve tree (password-redacted) + capability + optional silent `AccessibilityService.takeScreenshot()`; tree-first cadence | observation carries tree+capability; screenshot only when tree-blind/requested; no consent dialog |
| 1.3 | Action channel | `…/overlay/{Actuators,IntentActuator,AndroidPhoneController}.kt` | `/action` → `AndroidPhoneController.dispatch` → `Actuators` (`ACTION_CLICK` L120 / `ACTION_SET_TEXT` L227 / `dispatchGesture` L386/404 / `performGlobalAction` L372) + `IntentActuator` | element_click → correct node; coordinate → dispatchGesture; intent → correct OS intent |
| 1.4 | Consent banner + kill switch | `…/overlay/OverlayService.kt`, `…/PortalActivity.kt` | persistent "AI is controlling this device" banner while a session is active (via a `RemoteSessionBus` flow) + instant STOP that aborts the session | banner shows during control; STOP halts actuation + clears banner < 1s |
| 1.5 | **Comprehensive intent layer (full phone capability)** — decision 9 | `…/overlay/IntentActuator.kt` (+ `IntentActions.kt`), `AndroidManifest.xml` (`<queries>`) | expand from 14 intents to the FULL Android capability: complete common-intents catalog (alarms/timers, calendar, camera, contacts, email, file pickers, maps+navigation, media play-from-search, dial/call, SMS/MMS, web view, sharing) + `open_app(pkg)` (any installed app) + `open_url(uri)` (ACTION_VIEW any http/deep-link — covers all deep links) + `open_settings(panel)` (any `Settings.ACTION_*` panel) + a guarded generic `send_intent(action,uri?,extras?,mime?)` for the long tail; handle Android 11+ package visibility so any app resolves/launches; each is a typed tool the frontier model calls directly (decision-4 confirm/credential gates apply) | each catalog category fires the correct OS intent; `open_url` resolves an arbitrary deep link; `open_settings` reaches any panel; `send_intent` gated for high-consequence; package resolution works across installed apps |

**Acceptance:** a cloud-shaped action request executes through the existing actuators; **the frontier model reaches the phone's FULL capability via the fast intent path** (any app, any deep link, any settings panel, the full common-intents catalog) with vision/accessibility reserved only for in-app taps intents can't express; observation stream feeds a closed loop with **no on-device inference**; banner + kill switch work; password fields redacted.
**Risks:** `takeScreenshot()` availability varies → tree-only fallback; banner Z-order must not pollute the a11y tree; kill-switch responsiveness relies on the streaming channel (M0.3).

---

## M2 — Cloud "brain" (Gemini-mobile) + streaming — **the MVP**
**Goal:** a server-side frontier ReAct loop drives the phone end-to-end.
**Depends on:** M0, M1.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 2.1 | Frontier loop | `Orchestrator/frontier_agent_loop.py` (new), `Orchestrator/gemini_cu/agent_loop.py` | `run_frontier_loop(device, task, model, operator)`: call Gemini 3.5 Flash `environment:'mobile'` (NOT ADB); feed tree+screenshot; parse actions (open_app/click/type/scroll/long_press…); relay over the M0 streaming channel; loop to done | mock Gemini + mock channel: multi-step sequence marshals correctly |
| 2.2 | Hybrid grounding | `Orchestrator/frontier_agent_loop.py` (grounding helper) | `snap_coordinate_to_element(coord, tree)`: prefer nearest actionable a11y node (resource-id) → `element_click`; fall back to raw coordinate for tree-blind | snaps within bounds; nearest when between; raw when tree missing |
| 2.3 | Streaming consumer | `ToolVault/tools/control_device/executor.py` (new) | new `control_device` tool wraps the loop: resolve device, read capability, run loop over the streaming channel, return final result. `control_phone` becomes the Gemma-opt-in legacy path | e2e: loop drives a mock device open-app→type→tap→done |
| 2.4 | Adaptive timeout | `Orchestrator/frontier_agent_loop.py` | per-action (~10s) / per-turn (~30s) / session (adaptive, cold-load gone) timeouts + bounded retries with structured errors (no_device/lost_contact/timeout) | timeout + device-drop handled; structured error surfaced to the model |
| 2.5 | Config knob | `Orchestrator/config.py`, `Orchestrator/routes/gemini_cu_routes.py` | `[computer_use] frontier_provider` default = Gemini mobile; route device-control chat to the frontier loop; legacy ADB `control_android_device` path preserved | route selects loop by config; legacy path intact |

**Acceptance:** Gemini-mobile completes a real multi-step task on your phone (open app → type → submit); element-snap grounding confirmed; streaming beats the old 2s poll; legacy Gemma path still works.
**Risks:** streaming fragility over Tailscale; coordinate denorm accuracy across resolutions; model action-parsing robustness; element-snap edge cases on dynamic UIs.

---

## M3 — Origin-aware routing + onboarding device registry
**Goal:** the request targets the right device per the locked rule, configured in the managed-state hub.
**Depends on:** M2.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 3.1 | Origin id on `ToolContext` | `Orchestrator/toolvault/context.py` | add `origin_device_id: Optional[str]` threaded through the executor chain | instantiates set/None; serializable |
| 3.2 | Device model fields | `Orchestrator/device_registry/models.py` | add `is_primary: bool`, `default_provider: Optional[str]` (gemma/gemini/claude/openai/null); one primary per owner | serialization + single-primary validation |
| 3.3 | Registry methods | `Orchestrator/device_registry/registry.py` | `get/set_primary_device`, `get/set_default_provider` (atomic clear-then-set) | atomic primary swap; getters/setters persist |
| 3.4 | `resolve_device` | `Orchestrator/local_provider/mesh.py` | rule: explicit `target` → that node; else `origin_device_id` → that node; else operator's **primary**; else error. **Never silent-retarget** (mismatched origin → `ValueError`) | all four paths + mismatch error, against sample tailscale status |
| 3.5 | Executor routing | `ToolVault/tools/control_phone/executor.py` (+ `control_device`) | swap `mesh.resolve_origin` → `mesh.resolve_device(operator, origin, target=params['device'])`; error kinds `invalid_target` / `no_primary_device` | explicit override, origin default, primary fallback, invalid-target error; back-compat |
| 3.6 | Origin from each surface | `…/data/local/FcLoop.kt` (Android self-id), `Orchestrator/routes/{phone_routes,mcp_routes}.py` (Portal/MCP → operator primary) | each surface stamps the origin: Android app = its own tailnet id; Portal/MCP = operator's primary device | posted task carries origin; MCP token→operator→primary; explicit param overrides |
| 3.7 | Hub device UI + endpoints | `Orchestrator/routes/onboarding_routes.py`, `Portal/onboarding/{index.html,onboarding.js,onboarding.css}` | `?mode=manage` "Devices" card: list devices, designate primary, per-device default provider (Gemma opt-in), add/edit; endpoints `GET/POST /onboarding/devices…` | hub lists devices; set-primary clears old; provider persists; operator-isolated |

**Acceptance:** call `control_device` from Portal with no `device` → hits your primary; from the phone → hits the phone; explicit `device` → any tailnet node; never a silent retarget.
**Risks:** Android tailnet self-identity access from `FcLoop`; primary-update races (file lock); MCP token→operator must be enforced to avoid escalation.

---

## M4 — Safety & autonomy on the remote frontier path
**Goal:** full-suite actuation with the smart gates enforced for a cloud driver.
**Depends on:** M2 (routing M3 helps target the confirm to the right device).

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 4.1 | Remove the blanket allowlist | `…/data/remote/{RemoteAllowlist,RemoteTaskRunner}.kt` | delete/deprecate `RemoteAllowlist`; remove `isAllowedRemote()` from `buildRemoteDeviceTools` (L168-185) so all actuators + all 14 intents dispatch | tool list contains all actuators+intents; `send_email` no longer auto-"refused" |
| 4.2 | Per-device autonomy mode | `…/data/remote/RemoteTaskRunner.kt` | replace hardcoded `YOLO` (L64) with the target device's `AutonomyStore.load()` reader | PERMISSION consults confirm gates; YOLO does not |
| 4.3 | Confirm-gate on target device | `…/data/remote/RemoteTaskRunner.kt` | wire `OverlayConfirmUi(appContext)` so high-consequence (send/pay/delete/post) prompts **on the target device**; fail-safe = DENY if overlay perm missing | on-device: remote high-consequence action → confirm overlay → allow/deny honored |
| 4.4 | Credential-handoff on frontier path | `…/data/remote/RemoteTaskRunner.kt` | wire `OverlayCredentialHandoff(appContext)`; model's text on a password/payment field is **discarded**, user types it | password type → handoff invoked; model text never logged/forwarded |
| 4.5 | Prompt honesty | `…/data/remote/RemoteTaskRunner.kt` | rewrite `remotePrompt()` from "only safe actions" → "all actions available; high-consequence prompts the user; credentials are user-entered" | prompt reflects the new contract |

**Acceptance (device):** remote high-consequence tap → confirm overlay on the target device; remote password type → user-typed handoff, model blind. `RemoteTaskRunnerTest` covers PERMISSION-gates / YOLO-bypass / handoff / no-allowlist-refusals.
**Note:** the existing `Actuators`/`IntentActuator` gates are reused unchanged — this milestone only *wires* them into the frontier path.

---

## M5 — Tablet / large-screen / foldable / DeX
**Goal:** capture + actuation are display-addressed and posture-aware. **Parallel after M1.**
**Depends on:** M1.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 5.1 | Fix swipe/scroll bounds | `…/overlay/Actuators.kt` | replace deprecated `resources.displayMetrics` (L266-309) with `windowManager.getCurrentWindowMetrics()` (API 30+) | tablet swipe uses full-screen bounds; folded posture respected |
| 5.2 | Display-addressed gestures | `…/overlay/Actuators.kt` | `dispatchTap`/`dispatchSwipe` accept `displayId` → `GestureDescription.Builder.setDisplayId()` (default DEFAULT_DISPLAY) | setDisplayId called; DeX external-display gesture targets correct display |
| 5.3 | Window topology | `…/overlay/UiTreeReader.kt` | emit `windowTopology[{displayId, appPackage, bounds, isSystemBar}]` via `getWindows()`; document display-relative bounds | multi-window reports distinct window bounds + displayIds |
| 5.4 | Display-sized capture | `…/overlay/{ScreenCapture,OverlayService}.kt` | size VirtualDisplay from WindowMetrics; handle Android 14 `onCapturedContentResize` (invalidate coords on resize) | tablet screenshot at true resolution; rotate/fold re-emits resolution |
| 5.5 | Posture invalidation | `…/overlay/FoldingFeatureMonitor.kt` (new) | Jetpack `FoldingFeature` (FLAT/HALF_OPENED); include posture in observation; posture change ⇒ re-observe before next coordinate action | posture change detected; coords recomputed before next gesture |

**Acceptance:** tablet swipe computes within full bounds; foldable uses correct coords per posture (re-observe on change); DeX taps route by displayId; multi-window observation carries topology.

---

## M6 — XR (Galaxy XR) co-equal — node+intent, capture-independent
**Goal:** the XR headset works as a hands endpoint via node+intent actuation; validate the two unknowns on real hardware. **Runs continuously from M1.**
**Depends on:** M1 (contract/hands); validated alongside all milestones.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 6.1 | XR capability gating | `…/overlay/{DeviceCapabilities,AndroidPhoneController,Actuators}.kt` | XR ⇒ `supportsCoordinateGesture=false`; `AndroidPhoneController` skips+logs coordinate actions; `Actuators.tap(coord)` returns `Unsupported`; node `ACTION_CLICK` + intents pass through | XR: coordinate skipped/logged, node click works; phone: both work |
| 6.2 | Capture-independent loop | `…/overlay/UiTreeReader.kt`, `Orchestrator/frontier_agent_loop.py` | `treeIsRich` flag; when `hasScreenshot=false` (XR) run **tree+intent only**, never request a screenshot | XR loop completes a task with zero screenshots |
| 6.3 | XR consent surface | `…/overlay/OverlayService.kt` (+ `XrOverlayActivity`/`XrBubbleContent`) | render the "AI is controlling this device" banner + kill switch as an in-headset panel element | banner visible in-headset; kill stops loop |
| 6.4 | **XR validation harness** | `docs/plans/M6_XR_VALIDATION.md` (new) | on real Galaxy XR: (1) sideload+enable a11y, (2) `ACTION_CLICK` a 2D panel?, (3) `dispatchGesture` on a panel?, (4) capture the panel view?, (5) intent launch into a panel?, (6) full frontier task via node+intent | all steps pass/documented; **capture success/failure recorded** before M6 closure |

**Acceptance:** Galaxy XR reports `formFactor=XR_HEADSET`; a frontier model opens an app + clicks a node via intent+`ACTION_CLICK` with no coordinate fallback; capture-independence proven (loop works even if `hasScreenshot=false`). Glasses remain "drive the paired phone" (reuse phone stack).
**Risks:** the two unconfirmed unknowns — mitigated by the capability-flag degradation (a surprise → tree+intent still works, not a contract rework).

---

## M7 — Provider-agnostic (Claude / OpenAI + Gemma opt-in)
**Goal:** a per-backend coordinate adapter so any frontier model drives the same actuators.
**Depends on:** M2, M4.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 7.1 | Coordinate adapter | `Orchestrator/coordinate_adapter.py` (new) | base `CoordinateAdapter`; `Gemini` (0-999↔px), `Anthropic` (abs-px, ≤1568/2576 downscale+rescale), `OpenAI` (abs-px); factory keyed off `config.py` CU backend regex (L165) | Gemini 500,500@1080×2400 → ~540,1200; Anthropic rescale within 2px |
| 7.2 | Provider-tagged actions | `…/data/remote/RemoteControlServer.kt`, `…/overlay/AndroidPhoneController.kt` | `/action` carries `provider`; controller prefers node `ACTION_CLICK`, else coordinate; same tailnet+operator auth | provider-tagged action dispatches; node-first preference holds |
| 7.3 | Loop abstraction | `Orchestrator/frontier_agent_loop.py` | `FrontierAgentLoop` base; `Gemini`/`Anthropic`/`OpenAI` subclasses each bind their adapter | each loop returns its adapter; actions in native space converted at the boundary |
| 7.4 | Gemma opt-in behind seam | `ToolVault/tools/control_phone/executor.py`, `…/data/remote/RemoteControlServer.kt` | `provider=gemma` → on-device `/task` (legacy); `provider=frontier|absent` → streaming action path | provider routing selects Gemma vs frontier |
| 7.5 | Provider selection wiring | `Orchestrator/device_registry/*` | executor reads the device's `default_provider` (M3), instantiates the matching loop/adapter | registry provider → correct adapter/loop |

**Acceptance:** Gemini validated end-to-end; Claude/OpenAI adapters in place (DIY-on-Android, field-test flagged); Gemma reachable as opt-in behind the same seam.

---

## M8 — Hardening & distribution
**Goal:** ship-ready across phone/tablet/XR; compliant distribution; graceful capability loss. (Only the genuinely-new hardening items — capture/capability/allowlist/banner/streaming/registry already land in M0–M7.)
**Depends on:** M0–M7.

| # | Task | Files | Change | Test |
|---|---|---|---|---|
| 8.1 | a11y-revocation fallback | `…/overlay/{AndroidPhoneController,Actuators}.kt`, `…/data/remote/RemoteTaskRunner.kt` | if the a11y service is disabled/OS-revoked (Advanced Protection), return `intent_only_mode` with the 14 available intents; intent actions still fire; resume tree/gesture when re-enabled | disable a11y → intent_only_mode + intents work → re-enable resumes |
| 8.2 | Global incident kill | `…/data/remote/{RemoteControlServer,RemoteTaskRunner}.kt` | `POST /kill/{taskId}` + operator-scoped `POST /kill-all`; cancel coroutine scope; status→`killed` | kill marks killed; kill-all stops all operator tasks < 500ms; operator-scoped auth |
| 8.3 | Telemetry | `…/data/remote/RemoteTaskRunner.kt`, `…/overlay/Actuators.kt` | per-step latency + action outcome to a local SQLite (`Manifest/telemetry.db`, 7-day retention); `GET /telemetry/{taskId}` + `/telemetry/summary` | per-step latencies recorded; summary aggregates; retention enforced |
| 8.4 | E2E smoke matrix | `Orchestrator/validation/e2e_smoke_matrix.py` (new) | canonical tasks × {phone, tablet, XR}: tap/read/type/swipe/screenshot, send_sms confirm-gate, credential handoff, large-screen swipe (WindowMetrics), XR node+intent | phone must PASS to release; per-device capability coverage reported |
| 8.5 | Sideload/enterprise packaging | `…/app/build.gradle`, `…/AndroidManifest.xml` | signed release APK (enterprise key); **never claim `isAccessibilityTool`**; deploy via sideload/MDM; artifact in `Artifacts/`; never submitted to Play | signed APK sideloads; a11y enable-able; no Play submission path |

**Acceptance:** graceful intent-only degradation when a11y is revoked; global kill works; telemetry captured; smoke matrix green on phone (+ tablet/XR coverage documented); signed sideload APK.

---

## Open questions (resolve during build; none blocking)
- SSE vs WebSocket framing for M0.3 (frame-based JSON recommended).
- Whether to also redact password regions in screenshots (tree-only redaction may leak an unfocused password field into a frame).
- Kill switch = hard abort vs finish-current-action.
- Screenshot downscale on-device vs on-server (recommend on-server in the adapter).
- Frontier loop conversation history persistence across multi-turn device tasks (recommend persist, mirroring current CU).
- Banner shows provider name vs generic message.

## Test & verification strategy
- **Unit:** schemas (M0), coordinate adapters (M7), `resolve_device` routing (M3), `RemoteTaskRunner` safety (M4), grounding snap (M2).
- **Device (Fold + tablet + Galaxy XR):** every milestone's acceptance row; XR harness (M6.4) run continuously.
- **E2E:** the smoke matrix (M8.4); phone PASS gates release.
- Ship on `main` (staging-as-prod), device-validate before push, snapshot per milestone.
