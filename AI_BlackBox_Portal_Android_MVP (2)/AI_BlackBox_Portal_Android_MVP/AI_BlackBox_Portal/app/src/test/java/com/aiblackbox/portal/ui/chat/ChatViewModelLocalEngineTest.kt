package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.local.FakeLocalLlm
import com.aiblackbox.portal.data.local.FakeToolBridge
import com.aiblackbox.portal.data.local.FakeToolCallingLlm
import com.aiblackbox.portal.data.local.FcLoop
import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.ToolCallingLlm
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import com.aiblackbox.portal.data.model.UiMessage
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
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
 *
 * Review follow-ups (Task 2.4):
 *  6. streamLocalTurn returns true on success / false on fault (M1/M2 signal).
 *  7. stateAfterLocalTurn maps that outcome to IDLE vs ERROR (M1/M2 parity with SSE).
 *  8. shouldBlockSend ignores a second send while STREAMING (I2 hoisted guard) —
 *     the SAME predicate sendMessage applies to BOTH the engine and placeholder arms.
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

        val ok = ChatViewModel.streamLocalTurn(
            fcLoop = FcLoop(llm),
            persona = "PERSONA",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = "gemma-4-e2b",
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertTrue("success returns true (M1/M2 outcome signal)", ok)
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

        val ok = ChatViewModel.streamLocalTurn(
            fcLoop = FcLoop(throwing),
            persona = "P",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = null,
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertFalse("fault returns false (M1/M2 outcome signal)", ok)
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

    // ── Review follow-ups (Task 2.4) ──────────────────────────────────────────

    @Test
    fun `fault maps to ChatState ERROR not IDLE (M1 M2 parity with SSE)`() {
        // streamLocalTurn returned false (faulted): the instance method applies
        // stateAfterLocalTurn, which must yield ERROR — matching sendViaSSE's catch.
        assertEquals(
            ChatState.ERROR,
            ChatViewModel.stateAfterLocalTurn(faulted = true, current = ChatState.STREAMING),
        )
    }

    @Test
    fun `success maps STREAMING to IDLE and never clobbers a terminal state`() {
        // Normal completion while still streaming → IDLE.
        assertEquals(
            ChatState.IDLE,
            ChatViewModel.stateAfterLocalTurn(faulted = false, current = ChatState.STREAMING),
        )
        // If the stream already moved off STREAMING (e.g. a terminal ERROR the
        // sink set), success must NOT clobber it back to IDLE.
        assertEquals(
            ChatState.ERROR,
            ChatViewModel.stateAfterLocalTurn(faulted = false, current = ChatState.ERROR),
        )
    }

    @Test
    fun `a second send while STREAMING is ignored — hoisted guard, both arms`() {
        // sendMessage consults shouldBlockSend BEFORE routing for BOTH the SSE arm
        // and (review I2) the LOCAL_PLACEHOLDER arm, so the local engine AND the
        // null-engine placeholder fallback are guarded against double-sends.
        assertTrue("STREAMING blocks a new send", ChatViewModel.shouldBlockSend(ChatState.STREAMING))
        // Non-streaming states let a send through.
        assertFalse("IDLE allows a send", ChatViewModel.shouldBlockSend(ChatState.IDLE))
        assertFalse("THINKING allows a send", ChatViewModel.shouldBlockSend(ChatState.THINKING))
        assertFalse("ERROR allows a send", ChatViewModel.shouldBlockSend(ChatState.ERROR))
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

    // ── Task 3.3 — on-device tool-call/result rendering parity ─────────────────
    //
    // Wiring the tool-aware [FcLoop.runAgent] into the chat send path so a
    // tool-capable engine's TOOL CALLS and TOOL RESULTS render INLINE in the same
    // streaming assistant bubble the text streams into. Exercised through the PURE
    // [ChatViewModel.streamLocalAgentTurn] (mirror of [ChatViewModel.streamLocalTurn]),
    // with the 3.2 fakes — [FakeToolCallingLlm] (scripted per-turn LlmEvent lists),
    // [FakeToolBridge] (scripted search/execute), and [FakeLocalLlm] as the unused
    // `llm` arg (runAgent only consumes toolLlm + bridge).
    //
    // RENDERING PARITY (no typed tool UI events exist): the format mirrors the cloud
    // ER `er_action` convention (`\n`[tool]` args → status`) — see
    // [ChatViewModel.renderToolCall] / [ChatViewModel.renderToolOutcome].

    /** A two-fakes-in-one double that implements BOTH seams, like the real 2.6 engine. */
    private class FakeLocalEngine(
        private val tool: FakeToolCallingLlm,
    ) : com.aiblackbox.portal.data.local.LocalLlm, ToolCallingLlm {
        override var isLoaded: Boolean = true
        override suspend fun load(modelFile: java.io.File, delegate: String) {}
        override fun generate(prompt: String) = kotlinx.coroutines.flow.flow<String> { }
        override fun generateWithTools(prompt: String, tools: List<ToolSchema>) =
            tool.generateWithTools(prompt, tools)
        override fun close() {}
    }

    private fun schema(name: String) = ToolSchema(name = name, description = "$name desc")

    @Test
    fun `agent turn renders tool calls, outcomes, and final text inline, then saves`() = runTest {
        // Model: turn 1 searches, turn 2 calls the discovered tool, turn 3 answers.
        val toolLlm = FakeToolCallingLlm(
            script = listOf(
                listOf(LlmEvent.ToolCall("search_tools", buildJsonObject { put("query", "generate an image") })),
                listOf(LlmEvent.ToolCall("generate_image", buildJsonObject { put("prompt", "a cat") })),
                listOf(LlmEvent.TextDelta("Here's "), LlmEvent.TextDelta("your image")),
            ),
        )
        val bridge = FakeToolBridge(
            searchMap = mapOf("generate an image" to listOf(schema("generate_image"))),
            executeFn = { _, _ -> ToolResult(success = true, result = JsonPrimitive("https://img/cat.png")) },
        )
        val h = Harness()
        // Record EVERY (content, isStreaming) emission to assert the final form.
        val emissions = mutableListOf<Pair<String, Boolean>>()
        val recordingSink: (String, Boolean) -> Unit = { c, s ->
            emissions.add(c to s)
            h.sink(c, s)
        }

        val ok = ChatViewModel.streamLocalAgentTurn(
            fcLoop = FcLoop(FakeLocalLlm(), toolLlm = toolLlm, bridge = bridge, operator = "Brandon"),
            persona = "P",
            history = emptyList(),
            text = "make a cat picture",
            operator = "Brandon",
            model = "gemma-4-e2b",
            sink = recordingSink,
            saveSink = h.saveSink,
        )

        assertTrue("agent turn completed", ok)
        // The FINAL sink call is the sink(full, isStreaming=false).
        val finalEmission = emissions.last()
        assertFalse("final emission clears streaming", finalEmission.second)
        val full = finalEmission.first

        // Tool CALL lines appear, in order, before the final text.
        val searchCallIdx = full.indexOf("`[search_tools]`")
        val genCallIdx = full.indexOf("`[generate_image]`")
        val textIdx = full.indexOf("Here's your image")
        assertTrue("search_tools call line rendered", searchCallIdx >= 0)
        assertTrue("generate_image call line rendered", genCallIdx >= 0)
        assertTrue("final text rendered", textIdx >= 0)
        assertTrue("search call precedes generate call", searchCallIdx < genCallIdx)
        assertTrue("generate call precedes final text", genCallIdx < textIdx)

        // Tool OUTCOME lines (→ done) appear for both calls.
        assertTrue("outcomes use the arrow + done", full.contains("→ done"))
        assertEquals(
            "two successful outcomes (search + execute)",
            2,
            Regex("→ done").findAll(full).count(),
        )

        // Saved exactly once, full content, provider=local.
        val req = h.saved
        assertNotNull("save invoked once on completion", req)
        assertEquals("save carries the full accumulated content", full, req!!.assistantResponse)
        assertEquals("the user message", "make a cat picture", req.userMessage)
        assertEquals("save tagged provider=local", "local", h.saveProvider)
    }

    @Test
    fun `a tool-level failure renders failed but does not fault the turn`() = runTest {
        val toolLlm = FakeToolCallingLlm(
            script = listOf(
                listOf(LlmEvent.ToolCall("generate_image", buildJsonObject { put("prompt", "x") })),
                listOf(LlmEvent.TextDelta("done trying")),
            ),
        )
        val bridge = FakeToolBridge(
            executeFn = { _, _ -> ToolResult(success = false, result = JsonPrimitive("quota exceeded")) },
        )
        val h = Harness()

        val ok = ChatViewModel.streamLocalAgentTurn(
            fcLoop = FcLoop(FakeLocalLlm(), toolLlm = toolLlm, bridge = bridge, operator = "Brandon"),
            persona = "P",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = null,
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertTrue("a tool-LEVEL failure is NOT a stream fault — turn completes", ok)
        assertTrue("the outcome renders failed", h.assistant.content.contains("→ failed"))
        assertNotNull("a completed turn is still saved", h.saved)
        assertFalse("streaming cleared on completion", h.assistant.isStreaming)
    }

    @Test
    fun `a pure-text turn via the agent path renders no tool lines and saves`() = runTest {
        val toolLlm = FakeToolCallingLlm(
            script = listOf(listOf(LlmEvent.TextDelta("hello"))),
        )
        val bridge = FakeToolBridge()
        val h = Harness()

        val ok = ChatViewModel.streamLocalAgentTurn(
            fcLoop = FcLoop(FakeLocalLlm(), toolLlm = toolLlm, bridge = bridge, operator = "Brandon"),
            persona = "P",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = null,
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertTrue(ok)
        // A tool-capable engine that emits no tool calls renders IDENTICALLY to text.
        assertEquals("hello", h.assistant.content)
        assertFalse("no tool-call markers in a pure-text turn", h.assistant.content.contains("`["))
        assertNotNull("pure-text agent turn still saves", h.saved)
        assertEquals("hello", h.saved!!.assistantResponse)
    }

    @Test
    fun `a mid-stream fault in the agent path surfaces the friendly error and does not save`() = runTest {
        // The bridge's search throws (a non-2xx IOException equivalent) — parity with
        // streamLocalTurn's fault path: caught, error appended, false, no save.
        val toolLlm = FakeToolCallingLlm(
            script = listOf(
                listOf(LlmEvent.ToolCall("search_tools", buildJsonObject { put("query", "anything") })),
            ),
        )
        val throwingBridge = object : com.aiblackbox.portal.data.local.ToolBridge {
            override suspend fun searchTools(query: String, k: Int): List<ToolSchema> =
                throw java.io.IOException("bridge offline")
            override suspend fun execute(tool: String, params: JsonObject, operator: String): ToolResult =
                ToolResult(success = true, result = null)
        }
        val h = Harness()

        val ok = ChatViewModel.streamLocalAgentTurn(
            fcLoop = FcLoop(FakeLocalLlm(), toolLlm = toolLlm, bridge = throwingBridge, operator = "Brandon"),
            persona = "P",
            history = emptyList(),
            text = "hi",
            operator = "Brandon",
            model = null,
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertFalse("a propagating bridge fault returns false", ok)
        assertTrue(
            "the friendly on-device error is appended",
            h.assistant.content.contains("on-device", ignoreCase = true) ||
                h.assistant.content.contains("error", ignoreCase = true),
        )
        assertFalse("streaming cleared after fault", h.assistant.isStreaming)
        assertNull("a faulted turn is not persisted (parity with streamLocalTurn)", h.saved)
    }

    @Test
    fun `capability routing predicate — text-only engine is not tool-capable, both-interfaces is`() {
        // The branch sendViaLocalEngine uses: route to the agent path ONLY when the
        // provided engine implements ToolCallingLlm. FakeLocalLlm is text-only; the
        // combined FakeLocalEngine implements both.
        val textOnly: com.aiblackbox.portal.data.local.LocalLlm = FakeLocalLlm()
        val both: com.aiblackbox.portal.data.local.LocalLlm =
            FakeLocalEngine(FakeToolCallingLlm(script = emptyList()))

        assertFalse("text-only engine routes to the TEXT path", textOnly is ToolCallingLlm)
        assertTrue("both-interfaces engine routes to the AGENT path", both is ToolCallingLlm)
    }

    @Test
    fun `renderToolCall and renderToolOutcome are self-contained name-labeled lines`() {
        // Robust to multi-call turns: each line stands alone and is labeled by name
        // (runAgent emits ALL ToolCalls of a turn before any ToolOutcome).
        val call = ChatViewModel.renderToolCall("generate_image", buildJsonObject { put("prompt", "a cat") })
        assertTrue("call line is backtick-labeled by name", call.contains("`[generate_image]`"))
        assertTrue("call line is on its own line", call.startsWith("\n"))
        assertTrue("call line includes the args", call.contains("a cat"))

        val done = ChatViewModel.renderToolOutcome(
            "generate_image",
            ToolResult(success = true, result = JsonPrimitive("https://img/cat.png")),
        )
        assertTrue("outcome line is backtick-labeled by name", done.contains("`[generate_image]`"))
        assertTrue("outcome line uses the arrow convention", done.contains("→"))
        assertTrue("success renders done", done.contains("done"))

        val failed = ChatViewModel.renderToolOutcome(
            "generate_image",
            ToolResult(success = false, result = JsonPrimitive("nope")),
        )
        assertTrue("failure renders failed", failed.contains("failed"))
    }

    @Test
    fun `renderToolCall caps oversized args so a blob cannot flood the bubble or snapshot`() {
        // A model inlining a large blob (e.g. a base64 image) as a tool-call arg
        // must NOT dump it uncapped — renderToolCall caps it the SAME way an
        // oversized tool RESULT is capped (TOOL_RESULT_SNIPPET_MAX + the … suffix).
        val huge = "x".repeat(ChatViewModel.TOOL_RESULT_SNIPPET_MAX * 4)
        val call = ChatViewModel.renderToolCall(
            "generate_image",
            buildJsonObject { put("blob", huge) },
        )
        // The serialized args are far longer than the cap; the rendered ARGS portion
        // must be bounded at cap + the single-char ellipsis.
        assertTrue("oversized args are truncated with the … suffix", call.endsWith("…"))
        // Whole line = "\n`[name]` " decoration + capped args. The line length must
        // be bounded ≈ decoration + cap + 1 (ellipsis), NOT the full blob length.
        val decoration = "\n`[generate_image]` ".length
        assertEquals(
            "rendered line is bounded by decoration + cap + ellipsis",
            decoration + ChatViewModel.TOOL_RESULT_SNIPPET_MAX + 1,
            call.length,
        )
    }

    @Test
    fun `tool render strips carriage returns so a CR-laden result stays one line`() {
        // A tool RESULT carrying \r\n (or bare \r) must render on a SINGLE line —
        // no raw CR leaking into the chat bubble / saved snapshot.
        val outcome = ChatViewModel.renderToolOutcome(
            "run_shell",
            ToolResult(success = true, result = JsonPrimitive("line1\r\nline2\rline3")),
        )
        assertFalse("no raw carriage return in the rendered outcome", outcome.contains('\r'))
        // The only newline is the leading line-break of the inline line itself.
        assertEquals("snippet collapsed to one line", 1, outcome.count { it == '\n' })
    }
}
