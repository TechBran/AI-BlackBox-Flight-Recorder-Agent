package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.model.UiMessage
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Coverage for the clear-on-device-conversation logic ([ChatViewModel.clearLocalConversation]).
 *
 * Same strategy as [ChatViewModelLocalWarmTest]: the AndroidViewModel can't be
 * instantiated on the plain JVM (no Application / main dispatcher), so the actual
 * work is factored into a PURE, injectable core --
 * [ChatViewModel.performClearLocalConversation] -- which (a) emits an empty message
 * list and (b) persists the empty list for the current operator through the SAME
 * [com.aiblackbox.portal.data.store.ChatHistoryStore.save] seam history is saved
 * through elsewhere. The instance method just wires that core to its own
 * `_messages` flow + `historyStore`. This exercises the core directly.
 */
class ChatViewModelClearConversationTest {

    @Test fun `clear empties the in-memory messages`() = runBlocking {
        var emitted: List<UiMessage>? = null
        ChatViewModel.performClearLocalConversation(
            operator = "Brandon",
            emit = { emitted = it },
            save = { _, _ -> },
        )
        assertEquals(emptyList<UiMessage>(), emitted)
    }

    @Test fun `clear persists an EMPTY list for the current operator`() = runBlocking {
        var savedOperator: String? = null
        var savedMessages: List<UiMessage>? = null
        ChatViewModel.performClearLocalConversation(
            operator = "Sarah",
            emit = {},
            save = { op, msgs -> savedOperator = op; savedMessages = msgs },
        )
        assertEquals("Sarah", savedOperator)
        assertEquals(emptyList<UiMessage>(), savedMessages)
    }

    @Test fun `clear emits before it persists`() = runBlocking {
        val order = mutableListOf<String>()
        ChatViewModel.performClearLocalConversation(
            operator = "Brandon",
            emit = { order.add("emit") },
            save = { _, _ -> order.add("save") },
        )
        assertEquals(listOf("emit", "save"), order)
        assertTrue(order.isNotEmpty())
    }
}
