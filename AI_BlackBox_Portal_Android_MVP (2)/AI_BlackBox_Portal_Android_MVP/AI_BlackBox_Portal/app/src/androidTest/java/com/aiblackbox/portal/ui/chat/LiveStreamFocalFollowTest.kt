package com.aiblackbox.portal.ui.chat

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.Alignment
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertContentDescriptionEquals
import androidx.compose.ui.test.assertHeightIsEqualTo
import androidx.compose.ui.test.assertWidthIsEqualTo
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeDown
import androidx.compose.ui.test.swipeUp
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.ui.components.ChatBubble
import com.aiblackbox.portal.ui.components.LiveTextSection
import com.aiblackbox.portal.ui.components.LIVE_ANSWER_EDGE_TAG
import com.aiblackbox.portal.ui.components.LIVE_REASONING_EDGE_TAG
import com.aiblackbox.portal.ui.components.LIVE_TOOL_FALLBACK_EDGE_TAG
import com.aiblackbox.portal.ui.components.COMPLETED_RETURN_EDGE_TAG
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class LiveStreamFocalFollowTest {
    private val handoffMaxFrameStep = 64.dp
    private val handoffMonotonicTolerancePx = 1f
    @get:Rule
    val compose = createComposeRule()

    @Test
    fun controlledReasoningEdgeFillsThenFollowsLatestBurstOncePerFrame() =
        assertControlledPageFill(LiveStreamPhase.THINKING)

    @Test
    fun controlledAnswerEdgeFillsThenFollowsLatestBurstOncePerFrame() =
        assertControlledPageFill(LiveStreamPhase.ANSWERING)

    @Test
    fun completedMainContentReturnsOnlyAfterMeasuredArrival() =
        assertProductionCompletionReturn("main")

    @Test
    fun completedClaudeAgentContentReturnsOnlyAfterMeasuredArrival() =
        assertProductionCompletionReturn("claude-agents")

    @Test
    fun completedGeminiAgentContentReturnsOnlyAfterMeasuredArrival() =
        assertProductionCompletionReturn("gemini-agents")

    @Test
    fun completedMainHistoryShortcutIsTapOnlyAndClearsAtManualBottom() =
        assertCompletedHistoryShortcut("main")

    @Test
    fun completedClaudeHistoryShortcutIsTapOnlyAndClearsAtManualBottom() =
        assertCompletedHistoryShortcut("claude-agents")

    @Test
    fun completedGeminiHistoryShortcutIsTapOnlyAndClearsAtManualBottom() =
        assertCompletedHistoryShortcut("gemini-agents")

    @Test
    fun activityHostUsesOneFullTargetEightDpAboveMeasuredComposer() {
        val host = ReturnToLiveHostState().also {
            it.register("main", visible = true, returning = false) {}
            it.register("gemini-agents", visible = true, returning = true) {}
        }
        compose.setContent {
            val density = androidx.compose.ui.platform.LocalDensity.current
            Box(Modifier.fillMaxSize()) {
                Spacer(
                    Modifier
                        .align(Alignment.BottomCenter)
                        .height(180.dp)
                        .fillMaxSize()
                        .testTag("measured-composer"),
                )
                ReturnToLiveHost(
                    state = host,
                    composerTopPx = with(density) { 500.dp.toPx() },
                )
            }
        }

        compose.onNodeWithTag("return-to-live")
            .assertIsDisplayed()
            .assertHeightIsEqualTo(48.dp)
            .assertWidthIsEqualTo(48.dp)
        assertEquals(1, compose.onAllNodesWithTag("return-to-live").fetchSemanticsNodes().size)
        val arrow = compose.onNodeWithTag("return-to-live").fetchSemanticsNode().boundsInRoot
        val expectedBottom = with(compose.density) { 500.dp.toPx() - 8.dp.toPx() }
        assertTrue("arrow bottom ${arrow.bottom} must be 8dp north of composer", kotlin.math.abs(arrow.bottom - expectedBottom) <= 1f)
    }

    @Test
    fun completedHistoryReturnReachesBottomOfSingleItemTallerThanViewport() {
        lateinit var complete: () -> Unit
        lateinit var listState: LazyListState
        compose.mainClock.autoAdvance = false
        compose.setContent {
            var streaming by remember { mutableStateOf(true) }
            complete = { streaming = false }
            listState = rememberLazyListState()
            ProductionReturnHostHarness {
                MainChatContent(
                    messages = listOf(assistantMessage(0, 12_000, false).copy(isStreaming = streaming)),
                    chatState = if (streaming) ChatState.STREAMING else ChatState.IDLE,
                    signalLabel = "Responding",
                    listState = listState,
                )
            }
        }

        compose.mainClock.advanceTimeBy(500)
        compose.runOnIdle { complete() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.mainClock.advanceTimeByFrame()
        assertTrue(compose.runOnIdle { listState.canScrollForward })
        assertSingleReturnArrowDisplayed()
        compose.onNodeWithTag("return-to-live").performClick()

        advanceUntilTrueListBottom(listState, maxFrames = 180)
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    @Test
    fun productionComposerAndSignalResidenceShareOneDynamicBottomInsetContract() {
        lateinit var update: (Int, Int) -> Unit
        compose.setContent {
            var insetDp by remember { mutableStateOf(120) }
            var extraComposerDp by remember { mutableStateOf(0) }
            update = { inset, extra -> insetDp = inset; extraComposerDp = extra }
            BottomResidenceShellHarness(insetDp.dp, extraComposerDp.dp)
        }

        fun assertShell() {
            compose.waitForIdle()
            val root = compose.onNodeWithTag("bottom-shell").fetchSemanticsNode().boundsInRoot
            val composer = compose.onNodeWithTag("real-composer").fetchSemanticsNode().boundsInRoot
            val controls = compose.onNodeWithTag("composer-controls").fetchSemanticsNode().boundsInRoot
            val autoTts = compose.onNodeWithTag("composer-auto-tts").fetchSemanticsNode().boundsInRoot
            val rail = compose.onNodeWithTag("live-stream-rail").fetchSemanticsNode().boundsInRoot
            val messages = compose.onNodeWithTag("messages").fetchSemanticsNode().boundsInRoot
            assertTrue("composer controls must sit directly above residence", composer.bottom <= rail.top)
            assertTrue("provider/model controls must clear residence", controls.bottom <= rail.top)
            assertTrue("Auto-TTS must clear residence", autoTts.bottom <= rail.top)
            // Scroll-behind contract (revised 2026-07-15): the list's draw bounds
            // reach the window bottom so chat text visibly scrolls BEHIND the
            // transparent composer; clearance lives in contentPadding, not bounds.
            assertTrue("messages must extend behind the composer glass", messages.bottom >= composer.top)
            assertTrue("residence must remain above occupied inset", rail.bottom < root.bottom)
        }

        assertShell()
        val firstRailTop = railTop()
        compose.runOnIdle { update(240, 48) }
        assertShell()
        assertTrue("rail must recompute when effective inset changes", railTop() < firstRailTop)
    }

    @Test
    fun returnArrowBottomTracksMeasuredPromptTopAcrossLayoutChanges() {
        lateinit var update: (Int, Int) -> Unit
        compose.setContent {
            var insetDp by remember { mutableStateOf(120) }
            var extraComposerDp by remember { mutableStateOf(0) }
            update = { inset, extra -> insetDp = inset; extraComposerDp = extra }
            BottomResidenceShellHarness(insetDp.dp, extraComposerDp.dp, active = true)
        }
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }

        fun assertAnchored() {
            compose.waitForIdle()
            val arrow = compose.onNodeWithTag("return-to-live").fetchSemanticsNode().boundsInRoot
            val composer = compose.onNodeWithTag("real-composer").fetchSemanticsNode().boundsInRoot
            val tolerance = with(compose.density) { 1.dp.toPx() }
            val expectedBottom = composer.top - with(compose.density) { 8.dp.toPx() }
            assertTrue("arrow bottom ${arrow.bottom} must stay 8dp above prompt top ${composer.top}",
                kotlin.math.abs(arrow.bottom - expectedBottom) <= tolerance)
        }

        assertAnchored()
        compose.runOnIdle { update(240, 72) }
        assertAnchored()
    }

    @Test
    fun mainSignalResidenceKeepsItsHeightWhenLabelDisappears() {
        lateinit var clearLabel: () -> Unit
        compose.setContent {
            var label: String? by remember { mutableStateOf("Thinking") }
            clearLabel = { label = null }
            MainChatContent(
                messages = listOf(assistantMessage(0, 20, false)),
                chatState = ChatState.IDLE,
                signalLabel = label,
            )
        }

        compose.onNodeWithTag("live-stream-rail").assertHeightIsEqualTo(SIGNAL_RESIDENCE_HEIGHT)
        compose.runOnIdle { clearLabel() }
        compose.onNodeWithTag("live-stream-rail")
            .assertHeightIsEqualTo(SIGNAL_RESIDENCE_HEIGHT)
            .assertContentDescriptionEquals("")
    }

    @Test
    fun mainChatFollowsGrowingReasoningAndAnswerAndKeepsSignalWhileSuspended() {
        compose.mainClock.autoAdvance = false
        lateinit var update: (UiMessage, ChatState) -> Unit
        compose.setContent {
            var message by remember {
                mutableStateOf(assistantMessage(reasoningLength = 20, answerLength = 0, thinking = true))
            }
            var state by remember { mutableStateOf(ChatState.THINKING) }
            update = { nextMessage, nextState ->
                message = nextMessage
                state = nextState
            }
            ProductionReturnHostHarness {
                MainChatContent(
                    messages = listOf(message),
                    chatState = state,
                    signalLabel = "Thinking",
                )
            }
        }

        assertExactLiveEdgeGap(LIVE_REASONING_EDGE_TAG)
        compose.runOnIdle {
            update(assistantMessage(reasoningLength = 200, answerLength = 0, thinking = true), ChatState.THINKING)
        }
        assertExactLiveEdgeGap(LIVE_REASONING_EDGE_TAG)
        compose.runOnIdle {
            update(assistantMessage(reasoningLength = 200, answerLength = 20, thinking = false), ChatState.STREAMING)
        }
        assertExactLiveEdgeGap(LIVE_ANSWER_EDGE_TAG)
        compose.runOnIdle {
            update(assistantMessage(reasoningLength = 200, answerLength = 3_000, thinking = false), ChatState.STREAMING)
        }
        assertExactLiveEdgeGap(LIVE_ANSWER_EDGE_TAG)

        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        assertSingleReturnArrowDisplayed()
        compose.onNodeWithTag("live-stream-rail").assertIsDisplayed()
        val arrowBottom = compose.onNodeWithTag("return-to-live").fetchSemanticsNode().boundsInRoot.bottom
        assertTrue("return arrow must stay above the Signal residence", arrowBottom <= railTop())
    }

    @Test
    fun claudeAgentFollowsGrowingReasoningAndAnswer() {
        assertCliAgentFollowsGrowingReasoningAndAnswer("claude-agents")
    }

    @Test
    fun geminiAgentFollowsGrowingReasoningAndAnswer() {
        assertCliAgentFollowsGrowingReasoningAndAnswer("gemini-agents")
    }

    @Test
    fun claudeAgentReservesEmptyBottomSignalResidence() {
        assertAgentReservesEmptyBottomSignalResidence("agents")
    }

    @Test
    fun geminiAgentReservesEmptyBottomSignalResidence() {
        assertAgentReservesEmptyBottomSignalResidence("gemini-agents")
    }

    @Test
    fun thinkingReportsReasoningEdge() {
        var section: LiveTextSection? = null
        compose.setContent {
            ChatBubble(
                message = UiMessage(
                    id = "live",
                    role = "assistant",
                    content = "",
                    reasoning = "working through it",
                    isStreaming = true,
                    isThinking = true,
                ),
                onLiveEdgePositioned = { reported, _ -> section = reported },
            )
        }
        compose.waitForIdle()
        assertEquals(LiveTextSection.REASONING, section)
        compose.onNodeWithTag(LIVE_REASONING_EDGE_TAG).assertIsDisplayed()

    }

    @Test
    fun answerStreamingReportsAnswerEdge() {
        var section: LiveTextSection? = null
        compose.setContent {
            ChatBubble(
                message = UiMessage(
                    id = "live",
                    role = "assistant",
                    content = "answer",
                    reasoning = "done thinking",
                    isStreaming = true,
                    isThinking = false,
                ),
                onLiveEdgePositioned = { reported, _ -> section = reported },
            )
        }
        compose.waitForIdle()
        assertEquals(LiveTextSection.ANSWER, section)
    }

    @Test
    fun boundaryUserInputSuspendsEvenWhenListCannotMove() {
        compose.setContent { FollowHarness(initialItem = 0) }

        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }

        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
    }

    @Test
    fun accessibilityScrollSuspendsFollow() {
        compose.setContent { FollowHarness() }
        compose.waitForIdle()

        compose.onNodeWithTag("messages").performSemanticsAction(SemanticsActions.ScrollBy) {
            it(0f, 120f)
        }

        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
    }

    @Test
    fun repeatedInteractionResetsFiveSecondWindow() {
        compose.mainClock.autoAdvance = false
        compose.setContent { FollowHarness() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.mainClock.advanceTimeBy(4_000)
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.mainClock.advanceTimeBy(4_999)
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        compose.mainClock.advanceTimeBy(1)
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    @Test
    fun railLabelUpdatesWhileFollowIsSuspended() {
        lateinit var updateLabel: (String) -> Unit
        compose.setContent {
            var label by remember { mutableStateOf("Using Read") }
            updateLabel = { label = it }
            FollowHarness(label = label)
        }
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.runOnIdle { updateLabel("Using Grep") }

        compose.onNodeWithTag("live-stream-rail").assertIsDisplayed()
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        compose.onNodeWithTag("live-stream-rail").assertContentDescriptionEquals("Using Grep")
    }

    @Test
    fun terminalTransitionFromSettledParkGlidesToTrueBottomAndClearsArrow() {
        lateinit var finish: () -> Unit
        lateinit var listState: LazyListState
        compose.setContent {
            var active by remember { mutableStateOf(true) }
            finish = { active = false }
            FollowHarness(active = active, onListState = { listState = it })
        }
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.runOnIdle { finish() }
        compose.waitForIdle()

        // Revised contract (2026-07-15): completion from a SETTLED park auto-glides
        // to the TRUE bottom (the fresh reply is where the eyes go); the arrow
        // clears on landing. Only an actively-dragging finger retains the park.
        assertFalse(
            "completion must land at the true bottom",
            compose.runOnIdle { listState.canScrollForward },
        )
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    @Test
    fun reducedMotionCorrectsImmediately() {
        compose.mainClock.autoAdvance = false
        lateinit var listState: LazyListState
        compose.setContent { FollowHarness(reducedMotion = true, onListState = { listState = it }) }
        compose.mainClock.advanceTimeByFrame()
        val before = listState.firstVisibleItemScrollOffset
        compose.mainClock.advanceTimeByFrame()
        assertTrue("immediate correction must not require animation time", listState.firstVisibleItemScrollOffset != before)
        assertExactLiveEdgeGap(LIVE_ANSWER_EDGE_TAG)
    }

    @Test
    fun toolOnlyPhaseUsesMessageBottomAsFallbackAnchor() {
        compose.setContent {
            AgentLiveMessageContent(
                messages = listOf(assistantMessage(0, 0, false)),
                provider = "claude-agents",
                status = "Running",
                activeTool = ToolIndicatorData("Read", "", "file.kt"),
                isThinking = false,
                isStreaming = true,
            )
        }
        compose.onNodeWithTag(LIVE_REASONING_EDGE_TAG).assertDoesNotExist()
        // The blank streaming cursor is still visually composed; TOOL callback
        // precedence is proven separately by the pure phase selector test.
        compose.onNodeWithTag(LIVE_ANSWER_EDGE_TAG).assertIsDisplayed()
        assertExactLiveEdgeGap(LIVE_TOOL_FALLBACK_EDGE_TAG)
    }

    @Test
    fun thinkingToAnswerHandoffMovesSmoothlyFrameByFrameToExactGap() {
        compose.mainClock.autoAdvance = false
        lateinit var answer: () -> Unit
        compose.setContent {
            var message by remember { mutableStateOf(assistantMessage(240, 0, true)) }
            var state by remember { mutableStateOf(ChatState.THINKING) }
            answer = {
                message = assistantMessage(240, 40, false)
                state = ChatState.STREAMING
            }
            MainChatContent(listOf(message), state, "Responding")
        }
        assertExactLiveEdgeGap(LIVE_REASONING_EDGE_TAG)
        val reasoningEdge = edgeBottom(LIVE_REASONING_EDGE_TAG)
        compose.runOnIdle { answer() }
        compose.mainClock.advanceTimeByFrame()

        val positions = mutableListOf(edgeBottom(LIVE_ANSWER_EDGE_TAG))
        repeat(14) {
            compose.mainClock.advanceTimeByFrame()
            positions += edgeBottom(LIVE_ANSWER_EDGE_TAG)
        }
        val maxFrameStep = with(compose.density) { handoffMaxFrameStep.toPx() }
        assertTrue("phase handoff jumped from $reasoningEdge to ${positions.first()}",
            kotlin.math.abs(positions.first() - reasoningEdge) <= maxFrameStep)
        positions.zipWithNext().forEach { (before, after) ->
            assertTrue("correction must not jump more than $maxFrameStep px per frame",
                kotlin.math.abs(after - before) <= maxFrameStep)
        }
        val target = railTop() - with(compose.density) { LIVE_EDGE_GAP.toPx() }
        val errors = positions.map { kotlin.math.abs(it - target) }
        errors.zipWithNext().forEach { (before, after) ->
            assertTrue(
                "handoff must progress monotonically: $before -> $after",
                after <= before + handoffMonotonicTolerancePx,
            )
        }
        assertExactLiveEdgeGap(LIVE_ANSWER_EDGE_TAG)
    }

    @Test
    fun measuredEdgePerformsARealCorrectiveScrollWithoutShowingReturnArrow() {
        compose.mainClock.autoAdvance = false
        lateinit var observedListState: LazyListState
        compose.setContent {
            FollowHarness(onListState = { observedListState = it })
        }
        compose.mainClock.advanceTimeByFrame()
        val offsetBefore = observedListState.firstVisibleItemScrollOffset
        compose.mainClock.advanceTimeBy(500)
        val offsetAfter = observedListState.firstVisibleItemScrollOffset

        assertTrue("expected correction to move the list", offsetAfter != offsetBefore)
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    @Test
    fun programmaticCorrectionDoesNotSelfSuspendAfterItsScrollLifecycleCompletes() {
        compose.setContent { FollowHarness() }

        compose.waitForIdle()

        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    @Test
    fun userScrollShowsReturnToLiveUntilFiveIdleSecondsHavePassed() {
        compose.mainClock.autoAdvance = false
        compose.setContent { FollowHarness() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("live-stream-rail").assertIsDisplayed()
        compose.onNodeWithTag(LIVE_ANSWER_EDGE_TAG).assertIsDisplayed()
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()

        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        compose.mainClock.advanceTimeBy(4_999)
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        compose.mainClock.advanceTimeBy(1)
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        advanceUntilMeasuredArrival()
    }

    @Test
    fun returnToLiveClickResumesImmediately() {
        compose.mainClock.autoAdvance = false
        compose.setContent { FollowHarness() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.onNodeWithTag("return-to-live").assertIsDisplayed().performClick()
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        advanceUntilMeasuredArrival()
    }

    @Test
    fun userInputInterruptsActiveReturnAndKeepsArrowVisible() {
        compose.mainClock.autoAdvance = false
        compose.setContent { FollowHarness() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.onNodeWithTag("return-to-live").performClick()
        compose.mainClock.advanceTimeByFrame()
        assertSingleReturnArrowDisplayed()
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.mainClock.advanceTimeByFrame()
        assertSingleReturnArrowDisplayed()
    }

    private fun assertControlledPageFill(phase: LiveStreamPhase) {
        lateinit var setOverflow: (Float) -> Unit
        lateinit var listState: LazyListState
        compose.mainClock.autoAdvance = false
        compose.setContent {
            var overflow by remember { mutableStateOf(-20f) }
            setOverflow = { overflow = it }
            ControlledEdgeHarness(phase, overflow) { listState = it }
        }
        compose.mainClock.advanceTimeByFrame()
        val initial = listState.firstVisibleItemScrollOffset
        compose.runOnIdle { setOverflow(0f) }
        compose.mainClock.advanceTimeByFrame()
        assertEquals("$phase must not move at zero overflow", initial, listState.firstVisibleItemScrollOffset)
        compose.runOnIdle { setOverflow(8f) }
        compose.mainClock.advanceTimeByFrame()
        compose.mainClock.advanceTimeByFrame()
        val crossed = listState.firstVisibleItemScrollOffset
        assertTrue("$phase first crossing must cause first movement", crossed > initial)
        compose.runOnIdle { setOverflow(12f); setOverflow(24f); setOverflow(40f) }
        compose.mainClock.advanceTimeByFrame()
        compose.mainClock.advanceTimeByFrame()
        val burst = listState.firstVisibleItemScrollOffset
        assertTrue("$phase burst must consume latest overflow without lag", burst - crossed >= 39)
        compose.runOnIdle { setOverflow(6f) }
        compose.mainClock.advanceTimeByFrame()
        compose.mainClock.advanceTimeByFrame()
        assertTrue("$phase must remain pinned on later measurements",
            listState.firstVisibleItemScrollOffset > burst)
    }

    private fun assertProductionCompletionReturn(route: String) {
        lateinit var complete: () -> Unit
        lateinit var listState: LazyListState
        compose.mainClock.autoAdvance = false
        compose.setContent {
            var streaming by remember { mutableStateOf(true) }
            complete = { streaming = false }
            listState = rememberLazyListState()
            val message = assistantMessage(0, 3_000, false).copy(isStreaming = streaming)
            ProductionReturnHostHarness {
                if (route == "main") {
                    MainChatContent(
                        messages = listOf(message),
                        chatState = if (streaming) ChatState.STREAMING else ChatState.IDLE,
                        signalLabel = "Responding",
                        listState = listState,
                    )
                } else {
                    AgentLiveMessageContent(
                        messages = listOf(message),
                        provider = route,
                        status = "Responding",
                        activeTool = null,
                        isThinking = false,
                        isStreaming = streaming,
                        listState = listState,
                    )
                }
            }
        }
        compose.mainClock.advanceTimeBy(500)
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        assertSingleReturnArrowDisplayed()
        compose.runOnIdle { complete() }
        // Revised contract (2026-07-15): completion AUTO-glides to the true bottom
        // without an arrow tap; the completed-return anchor and the arrow clear on
        // landing (device-independent: instant under reduced motion, paged tweens
        // otherwise — advanceUntilTrueListBottom handles both).
        advanceUntilTrueListBottom(listState)
        compose.onNodeWithTag(COMPLETED_RETURN_EDGE_TAG).assertDoesNotExist()
    }

    private fun assertCompletedHistoryShortcut(route: String) {
        lateinit var complete: () -> Unit
        lateinit var listState: LazyListState
        compose.mainClock.autoAdvance = false
        compose.setContent {
            var streaming by remember { mutableStateOf(true) }
            complete = { streaming = false }
            listState = rememberLazyListState()
            val message = assistantMessage(0, 3_000, false).copy(isStreaming = streaming)
            ProductionReturnHostHarness {
                if (route == "main") {
                    MainChatContent(
                        messages = listOf(message),
                        chatState = if (streaming) ChatState.STREAMING else ChatState.IDLE,
                        signalLabel = "Responding",
                        listState = listState,
                    )
                } else {
                    AgentLiveMessageContent(
                        messages = listOf(message),
                        provider = route,
                        status = "Responding",
                        activeTool = null,
                        isThinking = false,
                        isStreaming = streaming,
                        listState = listState,
                    )
                }
            }
        }

        compose.mainClock.advanceTimeBy(500)
        compose.runOnIdle { complete() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.mainClock.advanceTimeByFrame()
        assertSingleReturnArrowDisplayed()
        compose.onNodeWithTag(COMPLETED_RETURN_EDGE_TAG).assertIsDisplayed()

        val positionBeforeIdle = compose.runOnIdle {
            listState.firstVisibleItemIndex to listState.firstVisibleItemScrollOffset
        }
        compose.mainClock.advanceTimeBy(FOLLOW_RESUME_DELAY_MS + 1_000)
        val positionAfterIdle = compose.runOnIdle {
            listState.firstVisibleItemIndex to listState.firstVisibleItemScrollOffset
        }
        assertEquals("completed history must not auto-return", positionBeforeIdle, positionAfterIdle)
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()

        compose.onNodeWithTag("return-to-live").performClick()
        advanceUntilTrueListBottom(listState)
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()

        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.mainClock.advanceTimeByFrame()
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        repeat(30) {
            if (compose.runOnIdle { listState.canScrollForward }) {
                compose.onNodeWithTag("messages").performTouchInput { swipeUp() }
                compose.mainClock.advanceTimeByFrame()
            }
        }
        assertTrue("manual scrolling must reach the true bottom", compose.runOnIdle {
            !listState.canScrollForward
        })
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    private fun advanceUntilTrueListBottom(listState: LazyListState, maxFrames: Int = 30) {
        repeat(maxFrames) {
            compose.mainClock.advanceTimeByFrame()
            if (!compose.runOnIdle { listState.canScrollForward }) {
                compose.mainClock.advanceTimeByFrame()
                assertTrue("completed return must remain at true list bottom",
                    compose.runOnIdle { !listState.canScrollForward })
                compose.onNodeWithTag("return-to-live").assertDoesNotExist()
                return
            }
            compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        }
        throw AssertionError("completed return did not reach true list bottom")
    }

    private fun advanceUntilMeasuredArrival(edgeTag: String = LIVE_ANSWER_EDGE_TAG) {
        var lastMeasuredGap = Float.POSITIVE_INFINITY
        repeat(30) {
            compose.mainClock.advanceTimeByFrame()
            val edgeNodes = compose.onAllNodesWithTag(edgeTag).fetchSemanticsNodes()
            if (edgeNodes.isNotEmpty()) {
                lastMeasuredGap = kotlin.math.abs(
                    edgeNodes.single().boundsInRoot.bottom -
                        (railTop() - with(compose.density) { LIVE_EDGE_GAP.toPx() }),
                )
            }
            if (compose.onAllNodesWithTag("return-to-live").fetchSemanticsNodes().isEmpty()) {
                val tolerance = with(compose.density) { 1.dp.toPx() }
                assertTrue("arrow hid before measured arrival: gap=$lastMeasuredGap",
                    lastMeasuredGap <= tolerance)
                return
            }
        }
        throw AssertionError("return arrow did not hide after measured arrival")
    }

    private fun edgeBottom(tag: String): Float =
        compose.onNodeWithTag(tag).fetchSemanticsNode().boundsInRoot.bottom

    private fun railTop(): Float =
        compose.onNodeWithTag("live-stream-rail").fetchSemanticsNode().boundsInRoot.top

    private fun assertSingleReturnArrowDisplayed() {
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        assertEquals(1, compose.onAllNodesWithTag("return-to-live").fetchSemanticsNodes().size)
    }

    private fun assertExactLiveEdgeGap(edgeTag: String) {
        compose.mainClock.advanceTimeBy(500)
        compose.waitForIdle()
        val edge = edgeBottom(edgeTag)
        val rail = railTop()
        val expectedPx = with(compose.density) { LIVE_EDGE_GAP.toPx() }
        val tolerancePx = with(compose.density) { 1.dp.toPx() }
        assertTrue(
            "expected gap ${expectedPx}px (+/-${tolerancePx}px), was ${rail - edge}px",
            kotlin.math.abs((rail - edge) - expectedPx) <= tolerancePx,
        )
    }

    private fun assertCliAgentFollowsGrowingReasoningAndAnswer(provider: String) {
        compose.mainClock.autoAdvance = false
        lateinit var update: (UiMessage, Boolean, ToolIndicatorData?) -> Unit
        compose.setContent {
            var message by remember {
                mutableStateOf(assistantMessage(reasoningLength = 20, answerLength = 0, thinking = true))
            }
            var thinking by remember { mutableStateOf(true) }
            var activeTool by remember { mutableStateOf<ToolIndicatorData?>(null) }
            update = { nextMessage, nextThinking, nextTool ->
                message = nextMessage
                thinking = nextThinking
                activeTool = nextTool
            }
            AgentLiveMessageContent(
                messages = listOf(message),
                provider = provider,
                status = "Running",
                activeTool = activeTool,
                isThinking = thinking,
                isStreaming = true,
            )
        }

        assertExactLiveEdgeGap(LIVE_REASONING_EDGE_TAG)
        compose.runOnIdle {
            update(assistantMessage(200, 0, true), true, null)
        }
        assertExactLiveEdgeGap(LIVE_REASONING_EDGE_TAG)
        compose.runOnIdle {
            update(
                assistantMessage(200, 0, false),
                false,
                ToolIndicatorData(name = "Read", icon = "", detail = "file.kt"),
            )
        }
        compose.onNodeWithTag("live-stream-rail").assertIsDisplayed()
        compose.runOnIdle {
            update(assistantMessage(200, 20, false), false, null)
        }
        assertExactLiveEdgeGap(LIVE_ANSWER_EDGE_TAG)
        compose.runOnIdle {
            update(assistantMessage(200, 3_000, false), false, null)
        }
        assertExactLiveEdgeGap(LIVE_ANSWER_EDGE_TAG)
    }

    private fun assertAgentReservesEmptyBottomSignalResidence(provider: String) {
        compose.setContent {
            AgentLiveMessageContent(
                messages = listOf(assistantMessage(0, 20, false)),
                provider = provider,
                status = "",
                activeTool = null,
                isThinking = false,
                isStreaming = false,
                bottomFocalGeometry = BottomFocalGeometry(
                    residenceTopPx = 940f,
                    residenceBottomPx = 1_000f,
                    composerTopPx = 700f,
                    composerBottomPx = 940f,
                    liveTargetYPx = 688f,
                ),
            )
        }

        compose.onNodeWithTag("live-stream-rail")
            .assertHeightIsEqualTo(SIGNAL_RESIDENCE_HEIGHT)
            .assertContentDescriptionEquals("")
    }
}

private fun assistantMessage(reasoningLength: Int, answerLength: Int, thinking: Boolean) = UiMessage(
    id = "live",
    role = "assistant",
    content = "a".repeat(answerLength),
    reasoning = "r".repeat(reasoningLength),
    isStreaming = true,
    isThinking = thinking,
)

@Composable
private fun ProductionReturnHostHarness(
    composerTop: androidx.compose.ui.unit.Dp = 700.dp,
    content: @Composable () -> Unit,
) {
    val host = remember { ReturnToLiveHostState() }
    val density = androidx.compose.ui.platform.LocalDensity.current
    Box(Modifier.fillMaxSize()) {
        androidx.compose.runtime.CompositionLocalProvider(LocalReturnToLiveHost provides host) {
            content()
        }
        ReturnToLiveHost(host, composerTopPx = with(density) { composerTop.toPx() })
    }
}

@Composable
private fun ControlledEdgeHarness(
    phase: LiveStreamPhase,
    overflowPx: Float,
    onListState: (LazyListState) -> Unit,
) {
    val listState = rememberLazyListState()
    onListState(listState)
    val followState = rememberLiveStreamFollowState(
        listState,
        LiveStreamSnapshot("live", 1, 1, phase, "live"),
    )
    androidx.compose.runtime.LaunchedEffect(overflowPx) {
        followState.setTarget(500f)
        followState.reportEdge(500f + overflowPx)
    }
    LazyColumn(state = listState, modifier = Modifier.fillMaxSize()) {
        items(30) { Spacer(Modifier.height(100.dp)) }
    }
}

@Composable
private fun BottomResidenceShellHarness(
    effectiveInset: androidx.compose.ui.unit.Dp,
    extraComposerHeight: androidx.compose.ui.unit.Dp,
    active: Boolean = false,
) {
    val density = androidx.compose.ui.platform.LocalDensity.current
    var windowBottom by remember { mutableStateOf(Float.NaN) }
    var composerTop by remember { mutableStateOf(Float.NaN) }
    var composerBottom by remember { mutableStateOf(Float.NaN) }
    val returnHost = remember { ReturnToLiveHostState() }
    val geometry = calculateBottomFocalGeometry(
        windowBottomPx = windowBottom,
        effectiveBottomInsetPx = with(density) { effectiveInset.toPx() },
        composerTopPx = composerTop,
        composerBottomPx = composerBottom,
        residenceHeightPx = with(density) { SIGNAL_RESIDENCE_HEIGHT.toPx() },
        breathingGapPx = with(density) { LIVE_EDGE_GAP.toPx() },
        fallbackComposerHeightPx = with(density) { FALLBACK_COMPOSER_HEIGHT.toPx() },
    )
    Box(
        Modifier.fillMaxSize().testTag("bottom-shell").onGloballyPositioned {
            windowBottom = it.boundsInWindow().bottom
        },
    ) {
        androidx.compose.runtime.CompositionLocalProvider(LocalReturnToLiveHost provides returnHost) {
            MainChatContent(
                messages = listOf(assistantMessage(0, 40, false)),
                chatState = if (active) ChatState.STREAMING else ChatState.IDLE,
                signalLabel = "Ready",
                bottomFocalGeometry = geometry,
            )
        }
        Column(
            Modifier
                .align(Alignment.BottomCenter)
                .padding(bottom = SIGNAL_RESIDENCE_HEIGHT + effectiveInset)
                .testTag("real-composer")
                .onGloballyPositioned {
                    val bounds = it.boundsInWindow()
                    composerTop = bounds.top
                    composerBottom = bounds.bottom
                },
        ) {
            Spacer(Modifier.height(extraComposerHeight))
            Composer(
                value = TextFieldValue(""),
                onValueChange = {},
                onSend = {},
                provider = "gemini",
                model = "",
                applySystemBottomInsets = false,
            )
        }
        ReturnToLiveHost(returnHost, composerTop)
    }
}

@Composable
private fun FollowHarness(
    initialItem: Int = 1,
    active: Boolean = true,
    label: String = "Thinking",
    reducedMotion: Boolean? = null,
    onListState: (LazyListState) -> Unit = {},
) {
    val listState = rememberLazyListState(initialFirstVisibleItemIndex = initialItem)
    onListState(listState)
    val snapshot = LiveStreamSnapshot(
        messageId = "live",
        reasoningLength = 12,
        answerLength = 0,
        phase = if (active) LiveStreamPhase.THINKING else LiveStreamPhase.IDLE,
        statusLabel = label,
    )
    val followState = rememberLiveStreamFollowState(listState, snapshot, reducedMotion)
    val returnHost = remember { ReturnToLiveHostState() }
    val registration = remember(returnHost) {
        returnHost.register("harness", followState.showReturnToLive, followState.returningToLive, followState::resumeNow)
    }
    // Reactive publish — mirrors the production screens; a SideEffect only runs on
    // recomposition and misses idle-time visibility flips (the production bug).
    androidx.compose.runtime.LaunchedEffect(registration, followState) {
        androidx.compose.runtime.snapshotFlow {
            followState.showReturnToLive to followState.returningToLive
        }.collect { (visible, returning) ->
            registration.publish(visible, returning, followState::resumeNow)
        }
    }
    androidx.compose.runtime.DisposableEffect(registration) {
        onDispose { registration.dispose() }
    }
    Box(Modifier.fillMaxSize()) {
        LazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .liveStreamUserInput(followState)
                .testTag("messages"),
        ) {
            item { Spacer(Modifier.height(300.dp)) }
            item {
                Spacer(
                    Modifier
                        .height(600.dp)
                        .testTag(LIVE_ANSWER_EDGE_TAG)
                        .onGloballyPositioned { coordinates ->
                            followState.reportEdge(coordinates.boundsInWindow().bottom)
                        },
                )
            }
            item { Spacer(Modifier.height(1_200.dp)) }
        }
        if (active) LiveStreamFocalRail(label, followState)
        ReturnToLiveHost(returnHost, composerTopPx = 700f)
    }
}
