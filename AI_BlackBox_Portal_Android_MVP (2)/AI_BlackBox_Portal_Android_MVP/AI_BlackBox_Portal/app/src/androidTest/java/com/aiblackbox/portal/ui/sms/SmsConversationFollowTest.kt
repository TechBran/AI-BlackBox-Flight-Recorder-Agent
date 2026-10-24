package com.aiblackbox.portal.ui.sms

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasScrollToIndexAction
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.unit.dp
import org.junit.Rule
import org.junit.Test

/**
 * The open SMS conversation follows new messages while the reader is at the bottom,
 * and only then. Its follow flag used to come from scroll-in-progress events, which
 * the view's own animateScrollToItem raised as well (and whose end it never saw), so
 * after the automatic scroll that opens a thread a reader resting at the bottom no
 * longer saw new messages arrive. SmsFollow is the rule (SmsInboxModelTest); this pins
 * it on a real LazyColumn.
 */
class SmsConversationFollowTest {
    @get:Rule
    val compose = createComposeRule()

    private fun msg(id: Int, body: String = "message $id") = SmsMsg(
        id = id,
        direction = if (id % 2 == 1) "inbound" else "outbound",
        body = body,
        timestamp = "2026-10-01T12:00:00+00:00",
        contactName = "",
        status = "delivered",
    )

    private fun show(
        messages: MutableState<List<SmsMsg>>,
        draft: MutableState<String> = mutableStateOf(""),
        onSend: () -> Unit = {},
    ) {
        compose.setContent {
            Box(Modifier.size(360.dp, 640.dp)) {
                SmsConversationView(
                    phone = "5550100123",
                    contactName = "",
                    messages = messages.value,
                    isSending = false,
                    lines = emptyList(),
                    selectedFromNumber = null,
                    draft = draft.value,
                    onDraftChange = { draft.value = it },
                    onSelectFromNumber = {},
                    onSend = onSend,
                    onBack = {},
                )
            }
        }
        compose.waitForIdle()
    }

    private fun arrive(messages: MutableState<List<SmsMsg>>, next: SmsMsg) {
        compose.runOnIdle { messages.value = messages.value + next }
        compose.waitForIdle()
    }

    @Test
    fun aReaderAtTheBottomSeesEveryNewMessage() {
        val messages = mutableStateOf((1..40).map { msg(it) })
        show(messages)
        compose.onNodeWithText("message 40").assertIsDisplayed()
        // Several in a row: the first automatic scroll must not end the following.
        for (next in 41..43) {
            arrive(messages, msg(next))
            compose.onNodeWithText("message $next").assertIsDisplayed()
        }
    }

    @Test
    fun theNewestPageKeepsFollowingAsItsOldestDropsOut() {
        // The box serves a fixed-size newest page: the count stays at 40.
        val messages = mutableStateOf((1..40).map { msg(it) })
        show(messages)
        for (next in 41..43) {
            compose.runOnIdle { messages.value = messages.value.drop(1) + msg(next) }
            compose.waitForIdle()
            compose.onNodeWithText("message $next").assertIsDisplayed()
        }
    }

    @Test
    fun aReaderReadingHistoryIsNotPulledDown() {
        val messages = mutableStateOf((1..40).map { msg(it) })
        show(messages)
        compose.onNode(hasScrollToIndexAction()).performScrollToIndex(0)
        compose.waitForIdle()
        arrive(messages, msg(41))
        compose.onNodeWithText("message 1").assertIsDisplayed()
        compose.onNodeWithText("message 41").assertDoesNotExist()
    }

    @Test
    fun theReadersOwnSendComesIntoViewFromHistory() {
        val messages = mutableStateOf((1..40).map { msg(it) })
        val draft = mutableStateOf("on my way")
        // As the view model does on success: the message is listed, the draft cleared.
        show(messages, draft, onSend = {
            messages.value = messages.value + msg(42, "on my way")
            draft.value = ""
        })
        compose.onNode(hasScrollToIndexAction()).performScrollToIndex(0)
        compose.waitForIdle()
        compose.onNodeWithText("\u2197").performClick()
        compose.waitForIdle()
        compose.onNodeWithText("on my way").assertIsDisplayed()
    }
}
