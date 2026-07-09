package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.model.UiMessage
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Retry-on-failed-send (retry chip under a failed user bubble).
 *
 * Same strategy as [ChatViewModelLocalRoutingTest] / [com.aiblackbox.portal.ChatViewModelSaveTest]:
 * the AndroidViewModel can't be instantiated here (no Robolectric, no Application,
 * no main dispatcher), so the production paths are thin shims over PURE companion
 * cores exercised directly:
 *  - [ChatViewModel.markSendFailedOnUserTurn] — the sendViaSSE catch (and the
 *    local-engine fault tail) flip `sendFailed` on the user turn ONLY when
 *    nothing usable arrived;
 *  - [ChatViewModel.retryRemoval] — retryMessage's REPLACE step: drop the error
 *    assistant bubble + the failed user turn, hand back the user message
 *    (text + images retained) for the re-fire.
 *
 * Also proves old persisted history WITHOUT the new `sendFailed` field still
 * deserializes (default false) through the same Json config ChatHistoryStore uses.
 */
class ChatViewModelRetryTest {

    private fun user(id: String, content: String, images: List<String> = emptyList(), failed: Boolean = false) =
        UiMessage(id = id, role = "user", content = content, images = images, sendFailed = failed)

    private fun assistant(id: String, content: String) =
        UiMessage(id = id, role = "assistant", content = content)

    // =========================================================================
    // markSendFailedOnUserTurn — failure flips sendFailed on the user turn
    // =========================================================================

    @Test
    fun `failure with nothing usable arrived flips sendFailed on the user turn`() {
        val msgs = listOf(
            user("u1", "hello"),
            assistant("a1", "Error: boom"),
        )
        val out = ChatViewModel.markSendFailedOnUserTurn(msgs, "u1", usableContentArrived = false)
        assertTrue(out.first { it.id == "u1" }.sendFailed)
        // The error assistant bubble is untouched
        assertEquals("Error: boom", out.first { it.id == "a1" }.content)
        assertFalse(out.first { it.id == "a1" }.sendFailed)
    }

    @Test
    fun `failure with partial content arrived does NOT flip sendFailed`() {
        val msgs = listOf(
            user("u1", "hello"),
            assistant("a1", "partial answer then the stream died"),
        )
        val out = ChatViewModel.markSendFailedOnUserTurn(msgs, "u1", usableContentArrived = true)
        assertFalse(out.first { it.id == "u1" }.sendFailed)
    }

    @Test
    fun `only the matching user turn is flagged`() {
        val msgs = listOf(
            user("u1", "first"),
            assistant("a1", "fine reply"),
            user("u2", "second"),
            assistant("a2", "Error: boom"),
        )
        val out = ChatViewModel.markSendFailedOnUserTurn(msgs, "u2", usableContentArrived = false)
        assertFalse(out.first { it.id == "u1" }.sendFailed)
        assertTrue(out.first { it.id == "u2" }.sendFailed)
    }

    // =========================================================================
    // retryRemoval — REPLACE step: error bubble + failed user turn removed,
    // user message (text + images) handed back for the re-fire
    // =========================================================================

    @Test
    fun `retryRemoval removes error bubble and user turn and retains text plus images`() {
        val images = listOf("http://host/up/1.png", "http://host/up/2.png")
        val msgs = listOf(
            user("u1", "earlier"),
            assistant("a1", "earlier reply"),
            user("u2", "send me", images = images, failed = true),
            assistant("a2", "Error: connection refused"),
        )
        val (remaining, removed) = ChatViewModel.retryRemoval(msgs, "u2")

        // The failed turn is fully gone — no duplicate user message, no error bubble
        assertEquals(listOf("u1", "a1"), remaining.map { it.id })
        // Text + images retained for the re-fire
        assertEquals("send me", removed?.content)
        assertEquals(images, removed?.images)
    }

    @Test
    fun `re-fired turn appends exactly one fresh user message with cleared flag`() {
        val msgs = listOf(
            user("u1", "send me", failed = true),
            assistant("a1", "Error: boom"),
        )
        val (remaining, removed) = ChatViewModel.retryRemoval(msgs, "u1")
        // Simulate what sendViaSSE does on the re-fire: append a FRESH user turn
        val refired = remaining + UiMessage(role = "user", content = removed!!.content, images = removed.images)

        val userTurns = refired.filter { it.role == "user" && it.content == "send me" }
        assertEquals(1, userTurns.size)                 // never duplicated
        assertFalse(userTurns.single().sendFailed)      // flag cleared (fresh default)
        assertTrue(refired.none { it.role == "assistant" }) // error bubble gone
    }

    @Test
    fun `retryRemoval without a following assistant bubble removes only the user turn`() {
        val msgs = listOf(
            user("u1", "earlier"),
            user("u2", "failed last", failed = true),
        )
        val (remaining, removed) = ChatViewModel.retryRemoval(msgs, "u2")
        assertEquals(listOf("u1"), remaining.map { it.id })
        assertEquals("failed last", removed?.content)
    }

    @Test
    fun `retryRemoval with unknown id is a no-op`() {
        val msgs = listOf(user("u1", "hello"), assistant("a1", "reply"))
        val (remaining, removed) = ChatViewModel.retryRemoval(msgs, "nope")
        assertEquals(msgs, remaining)
        assertNull(removed)
    }

    @Test
    fun `retryRemoval does not match an assistant message by id`() {
        val msgs = listOf(user("u1", "hello"), assistant("a1", "reply"))
        val (remaining, removed) = ChatViewModel.retryRemoval(msgs, "a1")
        assertEquals(msgs, remaining)
        assertNull(removed)
    }

    // =========================================================================
    // Persistence compatibility — old history without the field deserializes
    // =========================================================================

    @Test
    fun `old persisted history without sendFailed deserializes with default false`() {
        // The same Json config ChatHistoryStore uses
        val json = Json {
            ignoreUnknownKeys = true
            isLenient = true
            encodeDefaults = true
        }
        val legacy = """
            [
              {"id":"u1","role":"user","content":"hello","timestamp":1},
              {"id":"a1","role":"assistant","content":"hi","timestamp":2}
            ]
        """.trimIndent()
        val messages = json.decodeFromString<List<UiMessage>>(legacy)
        assertEquals(2, messages.size)
        assertFalse(messages[0].sendFailed)
        assertFalse(messages[1].sendFailed)
    }

    @Test
    fun `sendFailed round-trips through the history store Json config`() {
        val json = Json {
            ignoreUnknownKeys = true
            isLenient = true
            encodeDefaults = true
        }
        val original = listOf(user("u1", "hello", failed = true))
        val decoded = json.decodeFromString<List<UiMessage>>(json.encodeToString(kotlinx.serialization.serializer(), original))
        assertTrue(decoded.single().sendFailed)
    }
}
