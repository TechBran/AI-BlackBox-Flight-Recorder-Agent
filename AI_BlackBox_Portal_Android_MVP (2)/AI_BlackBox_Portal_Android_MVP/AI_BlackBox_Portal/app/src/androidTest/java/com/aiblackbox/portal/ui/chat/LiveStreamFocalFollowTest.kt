package com.aiblackbox.portal.ui.chat

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertContentDescriptionEquals
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeDown
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.ui.components.ChatBubble
import com.aiblackbox.portal.ui.components.LiveTextSection
import com.aiblackbox.portal.ui.components.LIVE_ANSWER_EDGE_TAG
import com.aiblackbox.portal.ui.components.LIVE_REASONING_EDGE_TAG
import com.aiblackbox.portal.ui.components.LIVE_TOOL_FALLBACK_EDGE_TAG
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class LiveStreamFocalFollowTest {
    @get:Rule
    val compose = createComposeRule()

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
            MainChatContent(
                messages = listOf(message),
                chatState = state,
                signalLabel = "Thinking",
            )
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
        compose.onNodeWithTag("return-to-live").assertIsDisplayed()
        compose.onNodeWithTag("live-stream-rail").assertIsDisplayed()
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
    fun terminalTransitionDuringSuspensionRemovesReturnControlAndLeavesListStable() {
        lateinit var finish: () -> Unit
        lateinit var listState: LazyListState
        compose.setContent {
            var active by remember { mutableStateOf(true) }
            finish = { active = false }
            FollowHarness(active = active, onListState = { listState = it })
        }
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        val before = listState.firstVisibleItemIndex to listState.firstVisibleItemScrollOffset
        compose.runOnIdle { finish() }
        compose.waitForIdle()

        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
        assertEquals(before, listState.firstVisibleItemIndex to listState.firstVisibleItemScrollOffset)
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
        compose.onNodeWithTag(LIVE_ANSWER_EDGE_TAG).assertDoesNotExist()
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
        val maxFrameStep = with(compose.density) { 64.dp.toPx() }
        assertTrue("phase handoff jumped from $reasoningEdge to ${positions.first()}",
            kotlin.math.abs(positions.first() - reasoningEdge) <= maxFrameStep)
        positions.zipWithNext().forEach { (before, after) ->
            assertTrue("correction must not jump more than $maxFrameStep px per frame",
                kotlin.math.abs(after - before) <= maxFrameStep)
        }
        val target = railTop() - with(compose.density) { LIVE_EDGE_GAP.toPx() }
        val errors = positions.map { kotlin.math.abs(it - target) }
        errors.zipWithNext().forEach { (before, after) ->
            assertTrue("handoff must progress monotonically: $before -> $after", after <= before + 1f)
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
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    @Test
    fun returnToLiveClickResumesImmediately() {
        compose.setContent { FollowHarness() }
        compose.onNodeWithTag("messages").performTouchInput { swipeDown() }
        compose.onNodeWithTag("return-to-live").assertIsDisplayed().performClick()
        compose.onNodeWithTag("return-to-live").assertDoesNotExist()
    }

    private fun edgeBottom(tag: String): Float =
        compose.onNodeWithTag(tag).fetchSemanticsNode().boundsInRoot.bottom

    private fun railTop(): Float =
        compose.onNodeWithTag("live-stream-rail").fetchSemanticsNode().boundsInRoot.top

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
    }
}
