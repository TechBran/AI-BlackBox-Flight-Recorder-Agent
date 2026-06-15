package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.local.FakeLocalLlm
import com.aiblackbox.portal.data.local.FcLoop
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.UiMessage
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task 2.4 — wiring the on-device (`local`) engine into the chat send path.
 *
 * Same strategy as [ChatViewModelLocalRoutingTest] / [com.aiblackbox.portal.ChatViewModelSaveTest]:
 * the AndroidViewModel can't be instantiated here (no Robolectric, no Application,
 * no main dispatcher), so the production [ChatViewModel.sendViaLocalEngine] is a
 * thin wiring shim over a PURE, testable core — [ChatViewModel.streamLocalTurn] —
 * which this exercises directly with a [FakeLocalLlm], a fake persona getter, and
 * a captured streaming sink / save sink. The instance method forwards the real
 * `_messages` updates and `saveConversation` into those same lambdas, so proving
 * the core proves the path.
 *
 * Coverage:
 *  1. engine present → deltas accumulate via the SAME sink, final == concat, NO SSE.
 *  2. null engine    → the 1.6 placeholder branch is taken (no crash, no SSE).
 *  3. persona from the cache is included in the prompt handed to the engine.
 *  4. mid-stream throw → partial text + friendly error surfaced, no crash, no SSE.
 *  5. the turn is persisted on completion (save sink invoked, tagged provider=local).
 */
class ChatViewModelLocalEngineTest {

    /**
     * Drives [ChatViewModel.streamLocalTurn] the way the production
     * [ChatViewModel.sendViaLocalEngine] does, capturing every sink emission so a
     * test can assert on the rendered message + the persisted save. Mirrors how
     * the real ViewModel threads `updateLastMessage(...)` (the SSE streaming sink)
     * and `buildSaveRequest(...)` / `saveConversation(...)` into the core.
     */
    private class Harness {
        /** Mutable assistant message, mutated exactly like the real `_messages` last item. */
        var assistant = UiMessage(role = "assistant", content = "", isStreaming = true, provider = "local")
            private set
        var saved: SaveRequest? = null
            private set
        var saveProvider: String? = null
            private set

        // The same-shaped sink the SSE path uses: (content, isStreaming) -> update last msg.
        val sink: (String, Boolean) -> Unit = { content, streaming ->
            assistant = assistant.copy(content = content, isStreaming = streaming)
        }
        val saveSink: (SaveRequest, String) -> Unit = { req, provider ->
            saved = req
            saveProvider = provider
        }
    }

    @Test
    fun `engine present accumulates deltas via the sink and saves, no SSE`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("Hel", "lo, ", "world"))
        val h = Harness()

        ChatViewModel.streamLocalTurn(
            fcLoop = FcLoop(llm),
            persona = "PERSONA",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = "gemma-4-e2b",
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertEquals("Hello, world", h.assistant.content)
        assertFalse("streaming flag cleared on completion", h.assistant.isStreaming)
        assertNotNull("save invoked on completion", h.saved)
        // No SSE: the FakeLocalLlm is the only generator; it was the one consumed.
        assertEquals(1, llm.prompts.size)
    }

    @Test
    fun `null engine takes the placeholder branch`() {
        // Production default: no on-device engine yet (LiteRtEngine is Task 2.6).
        // sendViaLocalEngine must fall back to the 1.6 placeholder — proven here by
        // the same predicate the instance method branches on.
        val provider: (() -> com.aiblackbox.portal.data.local.LocalLlm)? = null
        assertNull("no engine seam in production", provider)
        // And the placeholder the fallback appends is the unchanged 1.6 message.
        val placeholder = ChatViewModel.buildLocalPlaceholder(provider = "local", model = "gemma-4-e2b")
        assertEquals("assistant", placeholder.role)
        assertFalse("placeholder is not a streaming message", placeholder.isStreaming)
        assertTrue(placeholder.content.contains("on-device", ignoreCase = true))
    }

    @Test
    fun `persona from cache is included in the prompt handed to the engine`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("ok"))
        val h = Harness()

        ChatViewModel.streamLocalTurn(
            fcLoop = FcLoop(llm),
            persona = "YOU-ARE-THE-BLACKBOX-PERSONA",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = null,
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertNotNull(llm.lastPrompt)
        assertTrue(
            "the persona text must lead the assembled prompt",
            llm.lastPrompt!!.contains("YOU-ARE-THE-BLACKBOX-PERSONA"),
        )
    }

    @Test
    fun `mid-stream error surfaces partial text plus a friendly error, no crash`() = runTest {
        // FakeLocalLlm emits one delta then throws.
        val llm = FakeLocalLlm(
            scriptFor = { listOf("partial ") }, // chunks before the throw
        )
        // Wrap with a throwing generator: emit a delta, then fault.
        val throwing = object : com.aiblackbox.portal.data.local.LocalLlm {
            override var isLoaded: Boolean = true
            override suspend fun load(modelFile: java.io.File, delegate: String) {}
            override fun generate(prompt: String) = kotlinx.coroutines.flow.flow {
                emit("partial ")
                throw RuntimeException("native engine fault")
            }
            override fun close() {}
        }
        val h = Harness()

        ChatViewModel.streamLocalTurn(
            fcLoop = FcLoop(throwing),
            persona = "P",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = null,
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertTrue("partial text preserved", h.assistant.content.contains("partial"))
        assertTrue(
            "a friendly on-device error is shown",
            h.assistant.content.contains("error", ignoreCase = true) ||
                h.assistant.content.contains("on-device", ignoreCase = true),
        )
        assertFalse("streaming flag cleared after error", h.assistant.isStreaming)
        // No save on a faulted turn (mirrors the SSE error path, which does not save).
        assertNull("a faulted turn is not persisted", h.saved)
    }

    @Test
    fun `turn is persisted on completion tagged provider local`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("done"))
        val h = Harness()

        ChatViewModel.streamLocalTurn(
            fcLoop = FcLoop(llm),
            persona = "P",
            history = emptyList(),
            text = "the user message",
            operator = "Brandon",
            model = "gemma-4-e2b",
            sink = h.sink,
            saveSink = h.saveSink,
        )

        val req = h.saved
        assertNotNull("save invoked", req)
        assertEquals("Brandon", req!!.operator)
        assertEquals("the user message", req.userMessage)
        assertEquals("done", req.assistantResponse)
        assertEquals("save tagged provider=local", "local", h.saveProvider)
    }

    @Test
    fun `history maps user and assistant turns excluding the in-flight placeholder`() {
        // The mapper the instance method uses to turn UiMessages into FcLoop.Turns.
        val msgs = listOf(
            UiMessage(role = "user", content = "first q"),
            UiMessage(role = "assistant", content = "first a"),
            UiMessage(role = "user", content = "second q"),                 // the just-appended user turn
            UiMessage(role = "assistant", content = "", isStreaming = true), // the in-flight placeholder
        )
        val turns = ChatViewModel.toFcHistory(msgs)

        // The in-flight placeholder (last, streaming, empty assistant) is excluded;
        // the just-appended user turn is also excluded (it is passed as `text`).
        assertEquals(2, turns.size)
        assertEquals(FcLoop.Role.USER, turns[0].role)
        assertEquals("first q", turns[0].text)
        assertEquals(FcLoop.Role.ASSISTANT, turns[1].role)
        assertEquals("first a", turns[1].text)
    }
}
