# Android Live-Stream Focal Follow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the live edge of Android model thinking or answer text just above a fixed center Signal/status rail, while pausing immediately for user scrolling and resuming after five idle seconds.

**Architecture:** Add a small, testable follow-policy state machine and a reusable Compose coordinator shared by the main provider chat and CLI-agent chat. Chat bubbles report the measured bottom of the actively growing reasoning or answer section; the coordinator scrolls that edge toward a fixed focal target and renders the one-line status rail beneath it without duplicating response text.

**Tech Stack:** Kotlin 2.x, Jetpack Compose Foundation/Material 3, `LazyListState`, Kotlin coroutines 1.10.2, JUnit 4, `kotlinx-coroutines-test`, Compose UI instrumentation tests.

## Global Constraints

- Apply the behavior to `ChatScreen` and `AgentChatScreen`, covering all providers routed through them.
- Keep full reasoning and answer text in the ordinary `UiMessage`/`ChatBubble`; never duplicate live prose in an overlay.
- Put the live text edge immediately above a viewport-fixed, one-line Signal/status rail near vertical center.
- User-originated scrolling pauses follow immediately, reveals the down arrow, and restarts a five-second idle timer.
- Five uninterrupted seconds resumes automatically; tapping the down arrow resumes immediately.
- The Signal/status rail remains visible and live while list follow is suspended.
- Thinking-to-answer changes the tracked anchor without moving the focal target.
- Reduced-motion mode uses an immediate correction instead of a normal animated correction.
- Do not change provider transports, persistence payloads, reasoning storage, or model output.

---

## File Map

- Create `app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`: pure follow policy, live phase/snapshot types, shared Compose follow coordinator, anchor reporting, user-scroll suspension, and focal geometry constants.
- Create `app/src/test/java/com/aiblackbox/portal/ui/chat/LiveStreamFollowPolicyTest.kt`: deterministic policy and five-second resume tests.
- Modify `app/src/main/java/com/aiblackbox/portal/ui/components/ChatBubble.kt`: expose the measured reasoning/answer live edge for only the active message.
- Modify `app/src/main/java/com/aiblackbox/portal/ui/chat/ChatScreen.kt`: replace bottom-pin effects with the shared coordinator and move `SignalLine` to the center focal rail.
- Modify `app/src/main/java/com/aiblackbox/portal/ui/chat/AgentChatScreen.kt`: adopt the shared coordinator and map existing CLI thinking/tool/session state into one status label.
- Create `app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`: Compose integration coverage for anchor placement, pause, delayed resume, immediate resume, and status visibility.

All Android paths below are relative to:

`AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal`

---

### Task 1: Build the deterministic follow policy

**Files:**
- Create: `app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Create: `app/src/test/java/com/aiblackbox/portal/ui/chat/LiveStreamFollowPolicyTest.kt`

**Interfaces:**
- Produces: `LiveStreamPhase`, `LiveStreamSnapshot`, `LiveStreamFollowPolicy`, `FOLLOW_RESUME_DELAY_MS`.
- `LiveStreamFollowPolicy.onUserScroll(nowMs: Long)` suspends follow and sets the deadline.
- `LiveStreamFollowPolicy.onUserScrollSettled(nowMs: Long)` resets the deadline after fling/wheel activity.
- `LiveStreamFollowPolicy.tick(nowMs: Long): Boolean` resumes once and reports whether a return-to-live scroll is required.
- `LiveStreamFollowPolicy.resumeNow(): Boolean` resumes immediately and reports whether a scroll is required.
- `LiveStreamFollowPolicy.stop()` clears suspension and disables follow for a terminal stream.

- [ ] **Step 1: Write the failing policy tests**

```kotlin
package com.aiblackbox.portal.ui.chat

import org.junit.Assert.*
import org.junit.Test

class LiveStreamFollowPolicyTest {
    @Test fun `user input suspends immediately and resumes only after five idle seconds`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertTrue(policy.isSuspended)
        assertTrue(policy.showReturnToLive)
        assertFalse(policy.tick(5_999))
        assertTrue(policy.tick(6_000))
        assertFalse(policy.isSuspended)
    }

    @Test fun `continued interaction resets the five second deadline`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.onUserScrollSettled(4_000)
        assertFalse(policy.tick(8_999))
        assertTrue(policy.tick(9_000))
    }

    @Test fun `down arrow resumes immediately`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertTrue(policy.resumeNow())
        assertFalse(policy.isSuspended)
        assertFalse(policy.showReturnToLive)
    }

    @Test fun `terminal stream disables delayed return`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.stop()
        assertFalse(policy.tick(20_000))
        assertFalse(policy.isActive)
    }

    @Test fun `programmatic follow never enters suspended state`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onProgrammaticScrollStarted()
        policy.onProgrammaticScrollFinished()
        assertFalse(policy.isSuspended)
    }
}
```

- [ ] **Step 2: Run the tests and verify the missing-type failure**

Run:

```bash
cd 'AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal'
./gradlew testDebugUnitTest --tests com.aiblackbox.portal.ui.chat.LiveStreamFollowPolicyTest
```

Expected: compilation fails because `LiveStreamFollowPolicy` does not exist.

- [ ] **Step 3: Add the minimal policy and stream snapshot types**

```kotlin
package com.aiblackbox.portal.ui.chat

internal const val FOLLOW_RESUME_DELAY_MS = 5_000L

internal enum class LiveStreamPhase { IDLE, THINKING, ANSWERING, TOOL }

internal data class LiveStreamSnapshot(
    val messageId: String?,
    val reasoningLength: Int,
    val answerLength: Int,
    val phase: LiveStreamPhase,
    val statusLabel: String?,
) {
    val isActive: Boolean get() = phase != LiveStreamPhase.IDLE
    val followKey: Triple<String?, Int, Int>
        get() = Triple(messageId, reasoningLength, answerLength)
}

internal class LiveStreamFollowPolicy {
    var isActive: Boolean = false
        private set
    var isSuspended: Boolean = false
        private set
    var programmaticScroll: Boolean = false
        private set
    private var resumeAtMs: Long? = null

    val showReturnToLive: Boolean get() = isActive && isSuspended

    fun start() { isActive = true }

    fun stop() {
        isActive = false
        isSuspended = false
        programmaticScroll = false
        resumeAtMs = null
    }

    fun onUserScroll(nowMs: Long) {
        if (!isActive || programmaticScroll) return
        isSuspended = true
        resumeAtMs = nowMs + FOLLOW_RESUME_DELAY_MS
    }

    fun onUserScrollSettled(nowMs: Long) {
        if (isSuspended) resumeAtMs = nowMs + FOLLOW_RESUME_DELAY_MS
    }

    fun onProgrammaticScrollStarted() { programmaticScroll = true }
    fun onProgrammaticScrollFinished() { programmaticScroll = false }

    fun tick(nowMs: Long): Boolean {
        val deadline = resumeAtMs ?: return false
        if (!isActive || !isSuspended || nowMs < deadline) return false
        isSuspended = false
        resumeAtMs = null
        return true
    }

    fun resumeNow(): Boolean {
        if (!isActive || !isSuspended) return false
        isSuspended = false
        resumeAtMs = null
        return true
    }
}
```

- [ ] **Step 4: Run the focused unit test**

Run the command from Step 2.

Expected: `LiveStreamFollowPolicyTest` passes.

- [ ] **Step 5: Commit the policy**

```bash
git add app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt app/src/test/java/com/aiblackbox/portal/ui/chat/LiveStreamFollowPolicyTest.kt
git commit -m "test(android): define live stream follow policy"
```

---

### Task 2: Report the live reasoning or answer edge from ChatBubble

**Files:**
- Modify: `app/src/main/java/com/aiblackbox/portal/ui/components/ChatBubble.kt:86-365`
- Create: `app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: `UiMessage.isThinking`, `UiMessage.isStreaming`.
- Produces: `LiveTextSection`, `onLiveEdgePositioned: (LiveTextSection, Float) -> Unit`.
- The callback reports `boundsInWindow().bottom` only for the actively growing section.

- [ ] **Step 1: Add a failing Compose test for reasoning-edge and answer-edge reporting**

```kotlin
@Test fun thinkingReportsReasoningEdgeAndAnswerStreamingReportsAnswerEdge() {
    var section: LiveTextSection? = null
    compose.setContent {
        ChatBubble(
            message = UiMessage(
                id = "live", role = "assistant", content = "",
                reasoning = "working through it", isStreaming = true, isThinking = true,
            ),
            onLiveEdgePositioned = { reported, _ -> section = reported },
        )
    }
    compose.waitForIdle()
    assertEquals(LiveTextSection.REASONING, section)

    compose.setContent {
        ChatBubble(
            message = UiMessage(
                id = "live", role = "assistant", content = "answer",
                reasoning = "done thinking", isStreaming = true, isThinking = false,
            ),
            onLiveEdgePositioned = { reported, _ -> section = reported },
        )
    }
    compose.waitForIdle()
    assertEquals(LiveTextSection.ANSWER, section)
}
```

Include the required rule/imports at the top of the new test file:

```kotlin
@get:Rule val compose = createComposeRule()
```

- [ ] **Step 2: Run the instrumented test and verify it fails to compile**

Run:

```bash
./gradlew connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.aiblackbox.portal.ui.chat.LiveStreamFocalFollowTest
```

Expected: compilation fails because `LiveTextSection` and `onLiveEdgePositioned` do not exist. If no emulator/device is attached, run `./gradlew compileDebugAndroidTestKotlin` to verify the same compile failure and reserve execution for the device gate in Task 5.

- [ ] **Step 3: Add the edge-reporting interface and modifiers**

Add beside `ChatBubble`:

```kotlin
enum class LiveTextSection { REASONING, ANSWER }
```

Add a defaulted parameter:

```kotlin
onLiveEdgePositioned: ((LiveTextSection, Float) -> Unit)? = null,
```

Attach this modifier to the expanded reasoning `Text`:

```kotlin
Modifier.onGloballyPositioned { coordinates ->
    if (message.isThinking) {
        onLiveEdgePositioned?.invoke(
            LiveTextSection.REASONING,
            coordinates.boundsInWindow().bottom,
        )
    }
}
```

Attach the equivalent modifier to the `MarkdownText` that renders `cleanContent`:

```kotlin
Modifier
    .fillMaxWidth()
    .onGloballyPositioned { coordinates ->
        if (message.isStreaming && !message.isThinking) {
            onLiveEdgePositioned?.invoke(
                LiveTextSection.ANSWER,
                coordinates.boundsInWindow().bottom,
            )
        }
    }
```

For an empty answer that shows only `StreamingCursor`, attach the answer modifier to a wrapping `Box`; this guarantees a measurable live edge before the first nonblank answer chunk.

- [ ] **Step 4: Compile and execute the focused test when a device is available**

Run the command from Step 2.

Expected: the test passes, or `compileDebugAndroidTestKotlin` passes when device execution is deferred.

- [ ] **Step 5: Commit the anchor seam**

```bash
git add app/src/main/java/com/aiblackbox/portal/ui/components/ChatBubble.kt app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt
git commit -m "feat(android): expose live message text edge"
```

---

### Task 3: Implement the shared Compose focal-follow coordinator

**Files:**
- Modify: `app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Modify: `app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: `LazyListState`, `LiveStreamSnapshot`, edge positions from `ChatBubble`, reduced-motion state.
- Produces: `LiveStreamFollowState`, `rememberLiveStreamFollowState(...)`, `LiveStreamFocalRail(...)`.
- `LiveStreamFollowState.reportEdge(section, yInWindow)` receives measurements.
- `LiveStreamFollowState.resumeNow()` implements the down-arrow action.
- `LiveStreamFollowState.showReturnToLive` drives arrow visibility.

- [ ] **Step 1: Add failing coordinator tests**

Extend the Compose test file with a small harness containing a `LazyColumn`, a tall live item, and `LiveStreamFocalRail`. Add semantic tags `live-stream-rail`, `live-stream-edge`, and `return-to-live` in production code. Assert:

```kotlin
compose.onNodeWithTag("live-stream-rail").assertIsDisplayed()
compose.onNodeWithTag("live-stream-edge").assertIsDisplayed()
compose.onNodeWithTag("return-to-live").assertDoesNotExist()

compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
compose.onNodeWithTag("return-to-live").assertIsDisplayed()
compose.mainClock.advanceTimeBy(4_999)
compose.onNodeWithTag("return-to-live").assertIsDisplayed()
compose.mainClock.advanceTimeBy(1)
compose.onNodeWithTag("return-to-live").assertDoesNotExist()
```

Add a second test that clicks `return-to-live` and verifies immediate disappearance without advancing the clock.

- [ ] **Step 2: Run the focused instrumented test and verify failure**

Use Task 2 Step 2's command.

Expected: compile failure because the coordinator interfaces and semantic tags do not exist.

- [ ] **Step 3: Implement shared state and focal correction**

Add these constants and public surface to `LiveStreamFollow.kt`:

```kotlin
internal val FOCAL_RAIL_OFFSET = 36.dp
internal val LIVE_EDGE_GAP = 12.dp

@Stable
internal class LiveStreamFollowState internal constructor(
    val listState: LazyListState,
    private val scope: CoroutineScope,
    private val reducedMotion: () -> Boolean,
) {
    private val policy = LiveStreamFollowPolicy()
    var edgeY by mutableFloatStateOf(Float.NaN)
        private set
    var targetY by mutableFloatStateOf(Float.NaN)
        private set
    var showReturnToLive by mutableStateOf(false)
        private set
    private var correctionJob: Job? = null

    fun reportEdge(yInWindow: Float) { edgeY = yInWindow }
    fun setTarget(yInWindow: Float) { targetY = yInWindow }
    fun setActive(active: Boolean) {
        if (active) policy.start() else policy.stop()
        showReturnToLive = policy.showReturnToLive
    }
    fun suspendForUserInput(nowMs: Long) {
        correctionJob?.cancel()
        policy.onUserScroll(nowMs)
        showReturnToLive = policy.showReturnToLive
    }
    fun settleUserInput(nowMs: Long) = policy.onUserScrollSettled(nowMs)
    fun resumeNow() {
        if (policy.resumeNow()) correctToTarget()
        showReturnToLive = policy.showReturnToLive
    }
    fun tick(nowMs: Long) {
        if (policy.tick(nowMs)) correctToTarget()
        showReturnToLive = policy.showReturnToLive
    }
    fun correctToTarget() {
        if (!policy.isActive || policy.isSuspended || edgeY.isNaN() || targetY.isNaN()) return
        val delta = edgeY - targetY
        if (kotlin.math.abs(delta) < 1f) return
        correctionJob?.cancel()
        correctionJob = scope.launch {
            policy.onProgrammaticScrollStarted()
            try {
                if (reducedMotion()) listState.scrollBy(delta)
                else listState.animateScrollBy(delta, tween(durationMillis = 180))
            } finally {
                policy.onProgrammaticScrollFinished()
            }
        }
    }
}
```

Implement `rememberLiveStreamFollowState` with `rememberLazyListState` supplied by the caller, and add effects that:

1. call `setActive(snapshot.isActive)`;
2. call `correctToTarget()` when `snapshot.followKey`, phase, or measured edge changes;
3. observe `listState.isScrollInProgress` while `policy.programmaticScroll` is false, suspending at start and resetting the five-second deadline when it settles; and
4. run a cancellable five-second delay that invokes `tick(SystemClock.uptimeMillis())`.

Use `LocalViewConfiguration`, `Settings.Global.ANIMATOR_DURATION_SCALE`, or the project's existing reduced-motion helper pattern from `SignalLine.kt`; do not add a dependency.

Implement `LiveStreamFocalRail` as a `Box` aligned around viewport center. Measure the rail's top with `onGloballyPositioned` and set the live-edge target to `railTop - LIVE_EDGE_GAP`. Put `SignalLine(label)` inside the rail and apply `testTag("live-stream-rail")`.

- [ ] **Step 4: Run policy tests and compile Compose tests**

```bash
./gradlew testDebugUnitTest --tests com.aiblackbox.portal.ui.chat.LiveStreamFollowPolicyTest
./gradlew compileDebugAndroidTestKotlin
```

Expected: both commands pass. Run the focused connected test too when a device is attached.

- [ ] **Step 5: Commit the shared coordinator**

```bash
git add app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt
git commit -m "feat(android): add shared focal follow coordinator"
```

---

### Task 4: Integrate every main-chat provider

**Files:**
- Modify: `app/src/main/java/com/aiblackbox/portal/ui/chat/ChatScreen.kt:55-195`
- Modify: `app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: `LiveStreamSnapshot`, `rememberLiveStreamFollowState`, `LiveStreamFocalRail`, `ChatBubble.onLiveEdgePositioned`.
- Produces: main-chat reasoning and answer follow for every provider already represented by `ChatViewModel.messages` and `chatState`.

- [ ] **Step 1: Add a failing main-chat regression test**

Add a test harness around the screen's extracted message-list content (keep network initialization out of the harness). Feed the same assistant message ID first with reasoning lengths 20 and 200 while `ChatState.THINKING`, then with answer lengths 20 and 200 while `ChatState.STREAMING`. Assert after each update that `live-stream-edge` remains above `live-stream-rail`. Also assert the rail label remains displayed after `swipeDown()` while `return-to-live` is visible.

- [ ] **Step 2: Run the focused test and verify it fails against bottom-pin behavior**

Run Task 2 Step 2's connected-test command.

Expected: the reasoning update does not retain the focal relationship, or the new harness cannot compile before integration.

- [ ] **Step 3: Replace current bottom-pin effects with the shared snapshot**

Build the snapshot from the newest assistant message:

```kotlin
val liveMessage = messages.lastOrNull { it.role == "assistant" }
val liveSnapshot = LiveStreamSnapshot(
    messageId = liveMessage?.id,
    reasoningLength = liveMessage?.reasoning?.length ?: 0,
    answerLength = liveMessage?.content?.length ?: 0,
    phase = when (chatState) {
        ChatState.THINKING -> LiveStreamPhase.THINKING
        ChatState.STREAMING -> LiveStreamPhase.ANSWERING
        else -> LiveStreamPhase.IDLE
    },
    statusLabel = signalLabel,
)
val followState = rememberLiveStreamFollowState(listState, liveSnapshot)
```

Delete the `LaunchedEffect(messages.size)` and `LaunchedEffect(lastContentLength)` bottom-pin effects. New-message placement is handled by the active snapshot; idle history loading must not trigger focal follow.

Pass `onLiveEdgePositioned` only to the bubble whose ID equals `liveSnapshot.messageId`, and only accept the section matching the current phase:

```kotlin
onLiveEdgePositioned = if (message.id == liveSnapshot.messageId) { section, y ->
    val expected = if (liveSnapshot.phase == LiveStreamPhase.THINKING)
        LiveTextSection.REASONING else LiveTextSection.ANSWER
    if (section == expected) followState.reportEdge(y)
} else null
```

Render `LiveStreamFocalRail(signalLabel, followState)` instead of the existing bottom-composer `SignalLine`. Drive the existing down arrow from `followState.showReturnToLive`; its click calls `followState.resumeNow()`.

- [ ] **Step 4: Run main-chat tests and the Android unit suite**

```bash
./gradlew testDebugUnitTest
./gradlew compileDebugAndroidTestKotlin
```

Expected: all JVM tests and instrumented-test compilation pass. Run the focused connected test when a device is present.

- [ ] **Step 5: Commit main-chat integration**

```bash
git add app/src/main/java/com/aiblackbox/portal/ui/chat/ChatScreen.kt app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt
git commit -m "feat(android): center main chat live stream focus"
```

---

### Task 5: Integrate Claude Code and Gemini CLI-agent chat

**Files:**
- Modify: `app/src/main/java/com/aiblackbox/portal/ui/chat/AgentChatScreen.kt:600-750`
- Modify: `app/src/test/java/com/aiblackbox/portal/AgentEventProvenanceTest.kt`
- Modify: `app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: the same shared snapshot/coordinator and bubble anchor API as Task 4.
- Produces: `cliLiveStatusLabel(...)` and focal follow for both `claude-agents` and `gemini-agents`.

- [ ] **Step 1: Add failing CLI label and focal-follow tests**

Add JVM assertions for an internal pure mapping:

```kotlin
assertEquals("Thinking deeply", cliLiveStatusLabel(true, null, "Thinking..."))
assertEquals("Using · Read", cliLiveStatusLabel(false, "Read", "Running"))
assertEquals("Running", cliLiveStatusLabel(false, null, "Running"))
```

Add a Compose test that renders the agent harness for both provider values, grows `reasoning`, transitions to growing `content`, and verifies `live-stream-edge` remains above the same `live-stream-rail` in both phases.

- [ ] **Step 2: Run focused tests and verify the mapping/integration is missing**

```bash
./gradlew testDebugUnitTest --tests com.aiblackbox.portal.AgentEventProvenanceTest
./gradlew compileDebugAndroidTestKotlin
```

Expected: compilation fails because `cliLiveStatusLabel` and agent focal integration do not exist.

- [ ] **Step 3: Add the single-line CLI status mapping and coordinator**

Add the pure mapper near the reasoning phrases:

```kotlin
internal fun cliLiveStatusLabel(
    isThinking: Boolean,
    activeTool: String?,
    status: String,
): String? = when {
    isThinking -> "Thinking deeply"
    !activeTool.isNullOrBlank() -> "Using · $activeTool"
    status.isNotBlank() -> status
    else -> null
}
```

Build `LiveStreamSnapshot` from the newest assistant message, `isThinking`, `isStreaming`, and `activeTool`. Use `LiveStreamPhase.TOOL` when a tool is active and no thinking/answer delta is active. Replace the current answer-length-only `LaunchedEffect` with `rememberLiveStreamFollowState`.

Render `LiveStreamFocalRail` below the live edge. Remove the separate `ThinkingBar` and `ToolIndicatorBar` during an active stream so the focal rail is the only competing live-status presentation; retain the provider banner and non-streaming session metadata. Add the same down-arrow control and five-second behavior as main chat.

- [ ] **Step 4: Run all Android tests and build the debug APK**

```bash
./gradlew testDebugUnitTest
./gradlew compileDebugAndroidTestKotlin
./gradlew assembleDebug
```

Expected: all commands pass and `app/build/outputs/apk/debug/app-debug.apk` is produced.

- [ ] **Step 5: Run the connected acceptance suite on an attached device**

```bash
./gradlew connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.aiblackbox.portal.ui.chat.LiveStreamFocalFollowTest
```

Expected: all focal-follow tests pass. Manually verify one explicit-thinking provider, one answer-only provider, and Claude Code or Gemini CLI with a response longer than one screen. Confirm immediate pause, live rail visibility during pause, automatic return at five seconds, immediate arrow return, thinking-to-answer continuity, completion stability, and reduced-motion behavior.

- [ ] **Step 6: Commit CLI integration and final acceptance coverage**

```bash
git add app/src/main/java/com/aiblackbox/portal/ui/chat/AgentChatScreen.kt app/src/test/java/com/aiblackbox/portal/AgentEventProvenanceTest.kt app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt
git commit -m "feat(android): center CLI agent live stream focus"
```

---

## Final Verification

- [ ] Run `./gradlew testDebugUnitTest` and confirm zero failures.
- [ ] Run `./gradlew compileDebugAndroidTestKotlin` and confirm successful compilation.
- [ ] Run `./gradlew connectedDebugAndroidTest -Pandroid.testInstrumentationRunnerArguments.class=com.aiblackbox.portal.ui.chat.LiveStreamFocalFollowTest` on an attached device and confirm zero failures.
- [ ] Run `./gradlew assembleDebug` and confirm the debug APK is produced.
- [ ] Inspect `git diff --check` and confirm no whitespace errors.
- [ ] Inspect `git status --short` and confirm only intentional implementation files are included in the feature commits; preserve all unrelated pre-existing worktree changes.
