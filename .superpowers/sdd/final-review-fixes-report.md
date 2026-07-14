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

## Unready Bottom-Clearance Follow-up

Date: 2026-07-14; base `3bbc162e`.

Added an exact regression and implementation ensuring unready geometry reserves the full fallback Composer (200px in the fixture), permanent Signal residence (60px), and occupied inset (300px): app-owned clearance is 260px, total clearance is 560px, and `liveTargetYPx` remains null.

RED: `./gradlew testDebugUnitTest --tests com.aiblackbox.portal.ui.chat.LiveStreamFollowPolicyTest --offline` ran 12 tests and failed the new exact clearance assertion.

GREEN:

- Focused `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL` (12 tests).
- Full `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

`BottomFocalGeometry` remains public because it is exposed by existing public chat/navigation composable signatures; narrowing it would require a broader API-visibility refactor. No device or connected command was used.

## Page-fill Measurement Generation Review

Date: 2026-07-14; base `d3952d34`.

### Fixes

- Removed the unused `LatestLiveOverflow` test helper and replaced it with production-used generation-stamped `LatestLiveMeasurement` conflation.
- Normal follow consumes only a strictly newer measurement generation. Repeated requests against unchanged edge/target layout are suppressed.
- Return now uses one non-restarted loop. After every animated or reduced-motion correction it blocks for a newer layout measurement before checking arrival or scrolling again, preventing stale-distance spin and overscroll.
- Removed the no-op `onPhaseChanged`; mode continuity is the honest default behavior.
- Arrival tolerance is density-aware (1dp).
- Controlled-clock return tests no longer use a fixed 500ms disappearance as proof: they advance frame by frame and only accept arrow disappearance when the measured edge has reached the target. Added active-return interruption coverage.

### TDD RED

`./gradlew testDebugUnitTest --tests com.aiblackbox.portal.ui.chat.LiveStreamFollowPolicyTest --offline`

Result: compilation failed because the new production seam `LatestLiveMeasurement` did not exist. Tests specify latest-generation conflation, latest edge/target consumption, and stale-generation suppression.

### Offline GREEN

- Focused `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL`.
- Full `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

Concern: controlled-clock Compose behavior is compile-verified only because device/instrumentation execution was prohibited. No ADB or connected command was used.

## Atomic Frame Measurement Follow-up

Date: 2026-07-14; base `9b9634c8`.

- Replaced callback-level generation with production-used `FrameLiveMeasurementConflater`: edge/target callbacks stage values, one next-frame commit snapshots the latest pair, and a frame yields at most one generation/correction.
- Return startup drains stale buffered measurement notifications before waiting for post-start frame commits.
- Added unit proof that multiple edge/target callbacks within a frame yield one latest-pair generation, no second consume, and distinct later frames yield distinct generations.
- Added a controlled-clock external-edge Compose harness for both reasoning and answer: negative/zero overflow causes zero movement, first positive crossing causes first movement, burst callbacks conflate to the latest displacement, and subsequent measurements remain pinned.
- Added completed-response measured-arrival return coverage over main, `claude-agents`, and `gemini-agents` shared coordinator routes.

RED: focused `LiveStreamFollowPolicyTest` compilation failed because `FrameLiveMeasurementConflater` did not exist.

GREEN:

- Focused `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL`.
- Full `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

Concern: controlled-clock Compose tests are compile-verified but not executed because ADB/device/instrumentation use was prohibited.

## Compose Test Validity Cleanup

Date: 2026-07-14; base `c72d7403`.

- Split controlled page-fill coverage into independent reasoning and answer `@Test` cases; each test calls `setContent` exactly once.
- Split completion return into independent main, Claude-agent, and Gemini-agent cases; each calls `setContent` exactly once.
- Removed the synthetic completion harness. Main completion now drives real `MainChatContent`; Claude and Gemini completion drive real `AgentLiveMessageContent`. All three use actual long `ChatBubble` answer anchors, real message-list suspension, terminal state transition, arrow action, and measurement-based arrival verification.
- The deterministic external-edge harness remains only for precise page-fill boundary/burst offset assertions; it is paired with separate real-screen integration coverage already in the suite.

Offline verification:

- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- Focused `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL`.
- Full `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

No production behavior changed. Compose tests remain compile-verified only because ADB/device execution was prohibited.

## Stable Completed-Response Return Anchor

Date: 2026-07-14; base `2ea87f13`.

- Added policy/state `requiresReturnDestination`, true only while `SUSPENDED` or `RETURNING`; normal completion while following does not enable extra tracking.
- Added distinct nonvisual `COMPLETED_RETURN_EDGE_TAG` / `LiveTextSection.COMPLETED_RETURN` at the completed assistant bubble bottom.
- Main and agent production screens select this anchor only after phase becomes IDLE while return remains pending. It continues reporting through layout/scroll changes and is removed with the arrow after measured arrival.
- Main, Claude, and Gemini completion tests now assert the completed anchor exists through transit, measure arrival against that stable tag, and assert it disappears only after arrow dismissal.

TDD RED:

- Focused JVM compilation failed because `requiresReturnDestination` did not exist.
- Android test contract referenced the absent `COMPLETED_RETURN_EDGE_TAG` before production implementation.

Offline GREEN:

- Focused `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- Full `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

Concern: completion Compose cases are compile-verified only because device/instrumentation execution was prohibited. No ADB command was used.

## Completed-History True-Bottom Return

Date: 2026-07-14; base `6e058ca1`.

- Split completed-history return from live focal return with `returningToCompletedBottom` policy state.
- Completed arrow taps now smoothly `animateScrollToItem` the stable final LazyColumn item (immediate `scrollToItem` under reduced motion), while live streaming returns retain focal-edge measurement behavior.
- Arrow dismissal for completed history is driven only by observed `canScrollForward == false`; the policy now consumes this transition even during `RETURNING`, avoiding the previously lost distinct value after mode transition.
- User input cancels a completed return back to tap-only `COMPLETED_HISTORY` with no five-second timer. A new stream cancels any completed-return job and starts `FILLING` with completed UI/anchor cleared.
- Main, Claude, and Gemini completion tests now use bounded frame loops to prove true list-bottom arrival, stable `!canScrollForward` on the following frame, and arrow absence. Manual-bottom coverage uses a bounded loop rather than a fixed swipe count.

TDD RED: focused policy compilation failed because `returningToCompletedBottom` did not exist; new tests cover true-bottom-only dismissal, interruption semantics, and new-stream cancellation.

Offline GREEN:

- Focused `LiveStreamFollowPolicyTest` — `BUILD SUCCESSFUL` (29 tests).
- Full `./gradlew testDebugUnitTest --offline` — `BUILD SUCCESSFUL`.
- `./gradlew compileDebugAndroidTestKotlin --offline` — `BUILD SUCCESSFUL`.
- `./gradlew assembleDebug --offline` — `BUILD SUCCESSFUL`.
- `git diff --check` — exit 0.

Concern: real-screen completed-history Compose tests are compile-verified only because device/instrumentation execution was prohibited. No ADB command was used.
