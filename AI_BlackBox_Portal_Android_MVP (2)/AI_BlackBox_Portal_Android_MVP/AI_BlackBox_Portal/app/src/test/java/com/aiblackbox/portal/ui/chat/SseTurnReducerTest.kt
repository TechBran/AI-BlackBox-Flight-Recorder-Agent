package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.api.SSEEvent
import com.aiblackbox.portal.data.api.SseFrameParser
import com.aiblackbox.portal.data.model.TokenCount
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The chat's SSE handler as a pure reducer (audit A04-chat-viewmodel-17/-18, rank 76),
 * driven by the recorded /chat/stream fixtures (src/test/resources/sse) through the real
 * frame parser. ChatSseGoldenTest pins what a whole turn leaves in the view model; this
 * pins the event-by-event behaviour the view model cannot show the JVM: the chat-state
 * transitions, the Signal line's timeline and the CU and robotics writes.
 */
class SseTurnReducerTest {

    private fun fixture(name: String): List<SSEEvent> =
        SseFrameParser.parse(javaClass.classLoader!!.getResource("sse/$name.sse")!!.readText())

    private fun provider(name: String) = javaClass.classLoader!!.getResource("sse/$name.provider")!!.readText().trim()

    /** Every step of a fixture, folded the way ChatViewModel.applySseEvent folds it. */
    private class Run(val steps: List<SseStep>, val states: List<ChatState>) {
        val turn: SseTurnState get() = steps.last().turn
        val finalChatState: ChatState get() = states.last()
        val signals: List<String?> get() = steps.mapNotNull {
            when (val s = it.signal) {
                SignalUpdate.Unchanged -> null
                is SignalUpdate.Show -> s.label
                SignalUpdate.Clear -> "<clear>"
            }
        }
    }

    private fun run(name: String, model: String = "", base: String = "http://box:9091"): Run {
        val p = provider(name)
        var turn = SseTurnReducer.start(p, model)
        var chat = ChatState.STREAMING // sendViaSSE starts every turn STREAMING
        val steps = mutableListOf<SseStep>()
        val states = mutableListOf<ChatState>()
        for (e in fixture(name)) {
            val step = SseTurnReducer.reduce(turn, e, chat, p, base, nowMs = 42)
            turn = step.turn
            step.chatState?.let { chat = it }
            steps += step
            states += chat
        }
        return Run(steps, states)
    }

    private fun golden(name: String): JsonObject =
        Json.parseToJsonElement(javaClass.classLoader!!.getResource("sse/$name.golden.json")!!.readText()).jsonObject

    // ── the frame parser ─────────────────────────────────────────────────────

    @Test fun `frames decode JSON strings, keep objects raw and drop keepalives`() {
        val events = SseFrameParser.parse(
            ": keepalive\n\nevent: content\ndata: \"a \\u2014 b\\n\\\"q\\\"\"\n\n" +
                "event: usage\ndata: {\"prompt_tokens\": 1}\n\nevent: content\ndata: \"\"\n\n" +
                "data: \"no event name\"\n\nevent: done\ndata: {\"content\": \"x\"}"
        )
        assertEquals(listOf("content", "usage", "content", "message", "done"), events.map { it.event })
        assertEquals("a — b\n\"q\"", events[0].data)
        assertEquals("{\"prompt_tokens\": 1}", events[1].data)
        assertEquals("", events[2].data)
        // The last frame counts without its trailing blank line.
        assertEquals("{\"content\": \"x\"}", events[4].data)
    }

    @Test fun `every fixture parses to the events it was written with`() {
        val names = fixture("anthropic_retrieval").map { it.event }
        assertEquals(
            listOf("stream_start", "system_activity", "system_activity", "system_activity", "content_start",
                "content", "content", "usage", "done", "stream_end"),
            names,
        )
    }

    // ── whole turns agree with the view model's golden ───────────────────────

    @Test fun `each fixture's reply is what the golden turn shows`() {
        for (name in listOf(
            "anthropic_retrieval", "gemini_thinking", "openai_tool_image", "xai_structured_tool", "media_tasks",
            "error_only", "error_then_content", "done_fills_gaps", "empty_completion", "computer_use",
            "cu_queue_next", "robotics_mission",
        )) {
            val turn = run(name).turn
            val assistant = golden(name)["messages"]!!.jsonArray.last().jsonObject
            assertEquals(name, assistant["content"]!!.jsonPrimitive.content, ChatViewModel.combineContentAndErrors(turn.content, turn.errors))
            val reasoning = assistant["reasoning"]
            assertEquals(name, if (reasoning is JsonNull) null else reasoning!!.jsonPrimitive.content, turn.reasoning.ifBlank { null })
            assertEquals(name, assistant["model"]!!.jsonPrimitive.content, turn.model)
        }
        // The queued turn has no bubble of its own.
        assertTrue(run("cu_queued").turn.queued)
        assertEquals(1, golden("cu_queued")["messages"]!!.jsonArray.size)
    }

    // ── event by event ───────────────────────────────────────────────────────

    @Test fun `a retrieval turn narrates its telemetry, then generation, then dissolves`() {
        val r = run("anthropic_retrieval", model = "")
        assertEquals(
            listOf(
                "brain · claude-opus-5-5 · 200k window",
                "embed · gemini-embedding-001 · 3072d",
                "search · 4 snapshots",
                // The real model from stream_start, not the provider the turn was sent as.
                "generating · claude-opus-5-5",
                "<clear>", // done
                "<clear>", // stream_end
            ),
            r.signals,
        )
        assertEquals(ChatState.IDLE, r.finalChatState)
        assertEquals(TokenCount(35646, 12), r.turn.tokens)
        assertEquals(listOf("SNAP-20261029-0001", "SNAP-20261029-0002"), r.turn.provenance!!.recent)
        // "generating" shows once, though both content_start and content ask for it.
        assertTrue(r.turn.signal.generating)
    }

    @Test fun `thinking moves the chat to THINKING and the first content back to STREAMING`() {
        val r = run("gemini_thinking")
        val byEvent = fixture("gemini_thinking").map { it.event }.zip(r.states)
        assertEquals(ChatState.THINKING, byEvent.first { it.first == "thinking_start" }.second)
        assertEquals(ChatState.THINKING, byEvent.first { it.first == "thinking_end" }.second)
        assertEquals(ChatState.STREAMING, byEvent.first { it.first == "content" }.second)
        assertEquals(ChatState.IDLE, r.finalChatState)
        assertEquals("Considering the question. Checking memory.", r.turn.reasoning)
    }

    @Test fun `a structured tool_start names its tool, its result echoes it`() {
        val r = run("xai_structured_tool")
        assertEquals(
            listOf("tool · web_search", "tool · web_search · ok", "generating · grok-4.3", "<clear>", "<clear>"),
            r.signals,
        )
        assertEquals("web_search", r.turn.signal.lastTool)
    }

    @Test fun `a string tool_start and a tool round leave the reply untouched`() {
        val r = run("openai_tool_image")
        assertEquals(listOf("tool · generate_image", "tool · generate_image · ok", "generating · gpt-5.5", "<clear>", "<clear>"), r.signals)
        assertEquals("Here is your fox.", r.turn.content)
        assertEquals(listOf("image:img-task-1"), r.turn.mediaTasks)
        assertEquals(TokenCount(20785, 10), r.turn.tokens)
    }

    @Test fun `media tasks are typed, a bare string task id included`() {
        assertEquals(listOf("video:vid-task-9", "music:mus-task-3"), run("media_tasks").turn.mediaTasks)
    }

    @Test fun `errors accumulate apart from content and set ERROR, which stream_end resets`() {
        val only = run("error_only")
        assertEquals("", only.turn.content)
        assertEquals("Anthropic credits are exhausted.", only.turn.errors)
        val byEvent = fixture("error_only").map { it.event }.zip(only.states)
        assertEquals(ChatState.ERROR, byEvent.first { it.first == "error" }.second)
        // Recorded behaviour (ChatSseGoldenTest): the stream_end that always follows
        // resets the chat to IDLE; the finalize still treats the turn as failed.
        assertEquals(ChatState.IDLE, only.finalChatState)
        assertTrue(ChatViewModel.streamOutcomeIsFailure(only.turn.content, only.turn.errors))

        val mid = run("error_then_content")
        assertEquals("Partial answer and the rest.", mid.turn.content)
        assertEquals("upstream hiccup, retrying", mid.turn.errors)
        assertFalse(ChatViewModel.streamOutcomeIsFailure(mid.turn.content, mid.turn.errors))
    }

    @Test fun `done replaces shorter buffers, never two queued replies`() {
        val gaps = run("done_fills_gaps").turn
        assertEquals("Hello, the full reply.", gaps.content)
        assertEquals("a much longer chain of reasoning", gaps.reasoning)

        val next = run("cu_queue_next")
        assertTrue(next.turn.queueAppend)
        assertEquals("First task done." + ChatViewModel.cuQueueSeparator("now check email") + "Inbox has 3 messages.", next.turn.content)
        val queueNext = next.steps[fixture("cu_queue_next").indexOfFirst { it.event == "cu_queue_next" }]
        assertEquals("running", queueNext.cu.status)
        assertEquals(ChatState.STREAMING, queueNext.chatState)
        assertEquals("stopped", next.steps[fixture("cu_queue_next").indexOfFirst { it.event == "cu_stopped" }].cu.status)
    }

    @Test fun `a computer-use turn keeps the Signal dark and writes the CU state`() {
        val r = run("computer_use")
        assertEquals("only the turn-end clears reach the Signal", listOf("<clear>", "<clear>"), r.signals)
        val events = fixture("computer_use").map { it.event }
        fun cuAt(event: String) = r.steps[events.indexOf(event)].cu
        assertEquals(CuUpdate(sessionId = "cu-sess-1", deviceId = "blackbox"), cuAt("cu_session"))
        assertEquals(CuUpdate(status = "running", step = 1, stepTotal = 5), cuAt("cu_step"))
        assertEquals(CuUpdate(status = "running", step = 1, screenshotUrl = "http://box:9091/ui/uploads/cu_step1.png?t=42"), cuAt("cu_screenshot"))
        assertEquals(CuUpdate(status = "running", step = 2, actionLabel = "left_click (640, 360)"), cuAt("cu_action"))
        assertEquals(CuUpdate(), cuAt("cu_bash_output"))
        assertTrue(r.turn.content.startsWith("\n`[left_click (640, 360)]` \n```\n$ ls /tmp\na.txt\nb.txt\n```\n"))
        assertTrue(r.turn.signal.suppressed)
    }

    @Test fun `a queued CU prompt marks the turn queued and labels the action`() {
        val r = run("cu_queued")
        assertTrue(r.turn.queued)
        assertEquals("Prompt queued (#2)", r.steps[1].cu.actionLabel)
        assertEquals("", r.turn.content)
    }

    @Test fun `a robotics mission writes the ER state and ends the mission on done`() {
        val r = run("robotics_mission")
        val events = fixture("robotics_mission").map { it.event }
        fun erAt(event: String) = r.steps[events.indexOf(event)].er
        assertEquals(ErUpdate(missionActive = true, status = "mission_active"), erAt("er_mission_start"))
        assertEquals(ErUpdate(status = "connected", cameraFrame = "data:image/jpeg;base64,AAAA", camera = "front"), erAt("er_frame"))
        assertEquals(ErUpdate(status = "connected", reasoning = "The cup is on the left table."), erAt("er_reasoning"))
        assertEquals(ErUpdate(status = "navigating"), erAt("er_nav_status"))
        assertEquals(ErUpdate(status = "searching"), erAt("er_status"))
        assertEquals(ErUpdate(status = "offline"), erAt("er_error"))
        assertEquals(ErUpdate(missionActive = false, status = "stopped"), erAt("er_stopped"))
        assertEquals(ErUpdate(missionActive = false), erAt("done"))
        assertEquals(ErUpdate(), erAt("er_queued"))
    }

    @Test fun `odd data never throws and changes nothing it cannot read`() {
        var turn = SseTurnReducer.start("anthropic", "claude-opus-5-5")
        for (e in listOf(
            SSEEvent("usage", "not json"), SSEEvent("stream_start", "[1,2]"), SSEEvent("cu_step", "{}"),
            SSEEvent("done", "plain text"), SSEEvent("tool_start", "{\"other\":1}"), SSEEvent("tool_result", " "),
            SSEEvent("never_heard_of_it", "{}"),
        )) {
            turn = SseTurnReducer.reduce(turn, e, ChatState.STREAMING, "anthropic", "http://box", 0).turn
        }
        assertNull(turn.tokens)
        assertNull(turn.model)
        assertEquals("", turn.content)
        assertEquals("", turn.signal.lastTool)
        // An empty cu_step reads as step 0 of 0, as it always did.
        assertEquals(CuUpdate(status = "running", step = 0, stepTotal = 0),
            SseTurnReducer.reduce(turn, SSEEvent("cu_step", "{}"), ChatState.STREAMING, "computer-use", "", 0).cu)
    }

    @Test fun `the first Signal label names the sent model until stream_start names the real one`() {
        assertEquals("claude-opus-5-5", SseTurnReducer.start("anthropic", "claude-opus-5-5").signal.modelLabel)
        assertEquals("anthropic", SseTurnReducer.start("anthropic", "").signal.modelLabel)
        assertEquals("model", SseTurnReducer.start("", "").signal.modelLabel)
        val s = SseTurnReducer.reduce(SseTurnReducer.start("gemini", ""), SSEEvent("content", "hi"), ChatState.STREAMING, "gemini", "", 0)
        assertEquals(SignalUpdate.Show("generating · gemini"), s.signal)
    }
}
