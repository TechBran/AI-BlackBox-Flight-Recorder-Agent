package com.aiblackbox.portal.ui.chat

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeDown
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.ui.components.ChatBubble
import com.aiblackbox.portal.ui.components.LiveTextSection
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class LiveStreamFocalFollowTest {
    @get:Rule
    val compose = createComposeRule()

    @Test
    fun thinkingReportsReasoningEdgeAndAnswerStreamingReportsAnswerEdge() {
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
        compose.onNodeWithTag("live-stream-edge").assertIsDisplayed()

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
        compose.onNodeWithTag("live-stream-edge").assertIsDisplayed()
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
}

@Composable
private fun FollowHarness(
    initialItem: Int = 1,
    onListState: (LazyListState) -> Unit = {},
) {
    val listState = rememberLazyListState(initialFirstVisibleItemIndex = initialItem)
    onListState(listState)
    val snapshot = LiveStreamSnapshot(
        messageId = "live",
        reasoningLength = 12,
        answerLength = 0,
        phase = LiveStreamPhase.THINKING,
        statusLabel = "Thinking",
    )
    val followState = rememberLiveStreamFollowState(listState, snapshot)
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
                        .height(1_200.dp)
                        .testTag("live-stream-edge")
                        .onGloballyPositioned { coordinates ->
                            followState.reportEdge(coordinates.boundsInWindow().bottom)
                        },
                )
            }
        }
        LiveStreamFocalRail("Thinking", followState)
    }
}
