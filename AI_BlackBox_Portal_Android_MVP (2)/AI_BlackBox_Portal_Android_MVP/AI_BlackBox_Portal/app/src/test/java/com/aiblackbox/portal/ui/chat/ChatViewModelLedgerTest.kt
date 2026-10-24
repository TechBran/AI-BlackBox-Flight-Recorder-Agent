package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.model.UiMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Audit rank 59 (A04-chat-viewmodel-16, -02, -14, -15, -01): what a finished cloud turn
 * writes into the immutable ledger and into the operator's history file.
 *
 *  - the turn is saved under the operator, provider and model it was SENT with, never
 *    the live fields an operator switch rewrote mid-stream;
 *  - a computer-use turn is never POSTed to /chat/save (the backend CU loops save and
 *    mint it themselves, as the Portal knows);
 *  - error-event text never reaches the ledger, and an empty or queued turn saves nothing;
 *  - the streaming bubble is addressed by its id, so a late update cannot rewrite a
 *    message in another operator's conversation;
 *  - switching operators always replaces the conversation, even with an empty new one.
 *
 * The AndroidViewModel cannot be stood up on the plain JVM, so the rules are pure
 * companion functions tested here, and the wiring tests pin that the ViewModel uses them.
 */
class ChatViewModelLedgerTest {

    // ── what /chat/save receives ─────────────────────────────────────────────

    @Test fun `a computer-use turn is never saved from the phone`() {
        assertNull(ChatViewModel.ledgerResponseFor("computer-use", "clicked the button"))
    }

    @Test fun `error-event text never reaches the ledger`() {
        // The bubble shows content + error text; only the real content is minted.
        val shown = ChatViewModel.combineContentAndErrors("Here is the answer.", "tool fetch_url failed: 502")
        assertTrue(shown.contains("502"))
        val saved = ChatViewModel.ledgerResponseFor("anthropic", "Here is the answer.")
        assertEquals("Here is the answer.", saved)
    }

    @Test fun `an error-only stream saves nothing`() {
        assertNull(ChatViewModel.ledgerResponseFor("gemini", ""))
    }

    @Test fun `an empty or queued completion saves nothing`() {
        assertNull(ChatViewModel.ledgerResponseFor("gemini", ""))
        assertNull(ChatViewModel.ledgerResponseFor("openai", "   \n"))
    }

    @Test fun `an ordinary turn saves its real content`() {
        assertEquals("hello", ChatViewModel.ledgerResponseFor("gemini", "hello"))
    }

    // ── the CU queue (cu_queued / cu_queue_next) ─────────────────────────────

    @Test fun `the queued-prompt separator matches the Portal`() {
        assertEquals("\n\n---\n\n**➡ Next:** open settings\n\n", ChatViewModel.cuQueueSeparator("open settings"))
    }

    @Test fun `a queued prompt drops only its own empty placeholder`() {
        val user = UiMessage(role = "user", content = "second prompt")
        val placeholder = UiMessage(role = "assistant", content = "", isStreaming = true)
        val earlier = UiMessage(role = "assistant", content = "first reply")
        val out = ChatViewModel.dropQueuedPlaceholder(listOf(earlier, user, placeholder), placeholder.id)
        assertEquals(listOf(earlier, user), out)
    }

    // ── the streaming bubble is addressed by id ──────────────────────────────

    @Test fun `an update for a turn that is no longer on screen changes nothing`() {
        val otherOperatorsConversation = listOf(
            UiMessage(role = "user", content = "theirs"),
            UiMessage(role = "assistant", content = "their reply"),
        )
        val out = ChatViewModel.updateMessageById(otherOperatorsConversation, "gone-turn-id") {
            it.copy(content = "my late stream text")
        }
        assertSame("the other conversation is untouched", otherOperatorsConversation, out)
    }

    @Test fun `an update reaches its own bubble even when it is not the last message`() {
        val mine = UiMessage(role = "assistant", content = "", isStreaming = true)
        val after = UiMessage(role = "user", content = "typed meanwhile")
        val out = ChatViewModel.updateMessageById(listOf(mine, after), mine.id) { it.copy(content = "done", isStreaming = false) }
        assertEquals("done", out[0].content)
        assertFalse(out[0].isStreaming)
        assertEquals(after, out[1])
    }

    @Test fun `no id keeps the legacy last-message behaviour`() {
        val a = UiMessage(role = "assistant", content = "a")
        val b = UiMessage(role = "assistant", content = "b")
        val out = ChatViewModel.updateMessageById(listOf(a, b), null) { it.copy(content = "b2") }
        assertEquals(listOf("a", "b2"), out.map { it.content })
    }

    // ── loading an operator's history ────────────────────────────────────────

    @Test fun `an operator with no saved history gets an empty conversation`() {
        assertEquals(emptyList<UiMessage>(), ChatViewModel.restoredHistory(emptyList()))
    }

    @Test fun `restored history clears transient flags and is capped`() {
        val stuck = UiMessage(role = "assistant", content = "x", isStreaming = true, isThinking = true, ttsGenerating = true)
        val many = List(12) { UiMessage(role = "user", content = "m$it") } + stuck
        val out = ChatViewModel.restoredHistory(many, cap = 5)
        assertEquals(5, out.size)
        val last = out.last()
        assertFalse(last.isStreaming || last.isThinking || last.ttsGenerating)
    }

    // ── wiring: the ViewModel uses the rules above ───────────────────────────

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText().replace("\r\n", "\n")
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }

    private val vm by lazy { source("src/main/java/com/aiblackbox/portal/ui/chat/ChatViewModel.kt") }

    /** From `fun <name>(` to the next member at class indentation. */
    private fun functionBody(src: String, name: String): String {
        val start = Regex("fun $name\\(").find(src)?.range?.first ?: -1
        assertTrue("fun $name( not found", start >= 0)
        val next = Regex("\n    (?:(?:private|internal|override|suspend|public|@VisibleForTesting) )*fun ")
            .find(src, start + 1)?.range?.first ?: src.length
        return src.substring(start, next)
    }

    @Test fun `a cloud turn reads operator, provider and model once, at send time`() {
        val body = functionBody(vm, "sendViaSSE")
        val capture = body.indexOf("val turn = SseTurn(")
        assertTrue("sendViaSSE captures the turn context", capture >= 0)
        // Everything after the capture statement's closing parenthesis.
        val afterCapture = body.substring(body.indexOf("\n        )", capture))
        for (field in listOf("currentOperator", "currentProvider", "currentModel")) {
            assertFalse("sendViaSSE reads $field after capturing the turn", Regex("\\b$field\\b").containsMatchIn(afterCapture))
        }
        assertFalse("processSSEEvent must use the turn's provider", Regex("\\bcurrentProvider\\b").containsMatchIn(functionBody(vm, "processSSEEvent")))
        assertFalse("saveConversation must use the turn's operator", Regex("\\bcurrentOperator\\b").containsMatchIn(functionBody(vm, "saveConversation")))
        assertTrue("the finalize goes through ledgerResponseFor", "ledgerResponseFor(" in body)
    }

    @Test fun `an operator switch finalizes the in-flight turn before loading the new history`() {
        val init = vm.substring(vm.indexOf("store.operator.collect"))
        val finalize = init.indexOf("finalizeInFlightTurn(")
        val load = init.indexOf("loadHistory(op)")
        assertTrue("the collector finalizes the turn", finalize >= 0)
        assertTrue("the turn is finalized before the new history loads", finalize < load)
        val loadBody = functionBody(vm, "loadHistory")
        assertTrue("loadHistory always assigns", "_messages.value = restoredHistory(" in loadBody)
        assertFalse("loadHistory must not keep the old conversation for an empty new one", "isNotEmpty()" in loadBody)
    }
}
