# Final Review Fixes Report

Date: 2026-07-14
Branch: `feat/android-live-stream-focal-follow`
Base reviewed: `4d5a9e30`
Execution mode: offline only; no ADB, device installation, instrumentation, or connected tests were used.

## Changes

- Added a reusable `TOOL_FALLBACK` live edge at the active assistant message bottom. CLI TOOL phase selects it only when thinking/answer prose is not active; existing thinking and answer precedence is unchanged.
- Replaced loose edge-before-rail checks with density-aware assertions for `LIVE_EDGE_GAP` (12dp, 1dp tolerance), including main thinking/answer transition, long main output, Claude, Gemini, long agent output, and tool-only output.
- Added compile-valid Compose coverage for accessibility `ScrollBy`, repeated interaction deadline reset, suspended rail-label updates, terminal transition during suspension, and reduced-motion immediate correction.
- Added the smallest reduced-motion test seam (`reducedMotionOverride: Boolean? = null`); production still defaults to Android animator settings.
- Added a pure `reduceActiveTool` seam and regression coverage for content, tool result, error, disconnect, and completion. `AgentViewModel` now applies it before event handling.
- Added a semantic live label to the focal rail so status updates remain testable and accessible while suspended.

## TDD Evidence

RED 1:

`./gradlew compileDebugAndroidTestKotlin --offline`

Failed as expected because the new reduced-motion harness passed a missing third argument to `rememberLiveStreamFollowState` (`Too many arguments`).

RED 2:

`./gradlew testDebugUnitTest --tests com.aiblackbox.portal.AgentEventProvenanceTest --offline`

Failed as expected because `reduceActiveTool` did not exist (`Unresolved reference 'reduceActiveTool'`).

GREEN focused:

`./gradlew testDebugUnitTest --tests com.aiblackbox.portal.AgentEventProvenanceTest --tests com.aiblackbox.portal.ui.chat.LiveStreamFollowPolicyTest --offline`

Result: `BUILD SUCCESSFUL`.

## Required Offline Verification

- `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin assembleDebug --offline` — `BUILD SUCCESSFUL` (both requested tasks completed).
- `git diff --check` — exit 0, no whitespace errors.

The Android UI tests were compiled but deliberately not executed because the task prohibits connected/instrumented/device activity.

## Preserved Workspace State

Unrelated dirty and untracked files outside the scoped Android focal-follow implementation were not staged or modified by this work.

## Concerns

- Runtime geometry and input behavior remain device-gated. Compilation verifies the UI suite is structurally valid, but the new Compose assertions require later execution in an authorized connected-test environment.
- Existing project warnings (deprecated Android APIs and Gradle compatibility notices) remain unchanged and are outside this review scope.

## Final Re-review Follow-up

Date: 2026-07-14

### Changes

- Replaced the shared generic edge tag with production constants for reasoning, answer, and tool-fallback anchors. TOOL-only coverage now selects the fallback unambiguously, verifies the exact 12dp gap, and asserts reasoning/answer anchors are absent.
- Added a controlled-clock thinking-to-answer handoff test that samples each animation frame, bounds the phase-change/frame displacement, requires monotonic convergence, and still requires the settled 12dp gap.

### TDD RED

`./gradlew compileDebugAndroidTestKotlin --offline`

Result: expected compilation failure with unresolved references for `LIVE_REASONING_EDGE_TAG`, `LIVE_ANSWER_EDGE_TAG`, and `LIVE_TOOL_FALLBACK_EDGE_TAG`. This demonstrated the distinct production anchor-tag contract did not yet exist.

### Offline GREEN Verification

- `./gradlew testDebugUnitTest --tests com.aiblackbox.portal.AgentEventProvenanceTest --tests com.aiblackbox.portal.ui.chat.LiveStreamFollowPolicyTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0, no whitespace errors.

No ADB, phone, installation, instrumentation execution, or connected test command was used. The frame-by-frame Compose test is compile-verified offline and remains runtime device-gated.

## Final Cursor/Callback Correction

Date: 2026-07-14

- Corrected the TOOL-only UI expectation: the blank answer cursor remains visually composed and tagged, while the exact 12dp assertion continues to target the tool-fallback edge.
- Added and production-wired `cliLiveEdgeSection`, a pure selector proving callback precedence: THINKING to REASONING, ANSWERING to ANSWER, TOOL to TOOL_FALLBACK, and IDLE to null.
- Named the frame handoff limits (`handoffMaxFrameStep` and `handoffMonotonicTolerancePx`).

TDD RED:

`./gradlew testDebugUnitTest --tests com.aiblackbox.portal.AgentEventProvenanceTest --offline`

Result: expected compilation failure because `cliLiveEdgeSection` did not exist.

Offline GREEN:

- Focused JVM command including `AgentEventProvenanceTest` and `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL`.
- `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

No connected or device command was used.

## Bottom Signal Residence Review

Date: 2026-07-14; base `eadba48d`.

### Changes

- Native shell now computes one effective bottom inset from `IME union navigationBars`. The host applies it once: Composer opts out of its legacy internal system padding, while both Composer and the Signal residence consume the same inset contract.
- Signal residence is the lowest app-owned row above the occupied inset; Composer and its provider/model/Auto-TTS controls sit directly above it.
- `BottomFocalGeometry` now exposes centralized app-owned/total clearance, occupied inset, readiness, and a nullable target. Unmeasured startup emits no global target and uses the locally measured rail fallback; fallback geometry is visibly clamped at zero.
- Main and agent message viewports consume total bottom clearance, while return controls consume app-owned clearance plus the shared inset exactly once.
- Added a production-faithful Compose shell harness using the real `Composer`, real main chat residence/content, controllable inset, and controllable composer height. It asserts ordering, controls clearance, viewport exclusion, occupied inset, and dynamic recomputation.

### TDD Evidence

RED command:

`./gradlew testDebugUnitTest --tests com.aiblackbox.portal.ui.chat.LiveStreamFollowPolicyTest --offline`

Expected failure: missing `effectiveBottomInsetPx`, `isReady`, and `bottomClearancePx` contracts. After the first implementation, the visibility assertion also failed because startup residence top was `-60`; production was then clamped to the visible range before GREEN.

### Offline Verification

- Focused `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL` (11 tests, zero failures).
- `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

Concern: the production-faithful Compose shell test is compile-verified only because connected/instrumentation execution was explicitly prohibited. No ADB, phone, install, or connected-test command was used.
