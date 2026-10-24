package com.aiblackbox.portal.data.agent

import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.ui.chat.PendingAgentPrompt
import com.aiblackbox.portal.ui.chat.agentPromptOperator
import com.aiblackbox.portal.ui.chat.deliverHeldAgentPrompts
import com.aiblackbox.portal.ui.chat.finishStreamingBubble
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Audit rank 60 (A09-data-local-model-01, A05-chat-ui-01, -02): the CLI-agent session.
 *  - reconnects back off 1s, 2s, 4s ... 30s and give up after the cap (connect() reset
 *    the counter on every retry, so it hammered every second forever), and a
 *    server-side session_ended is not followed by a reconnect to the dead session;
 *  - a prompt sent while the agent screen is not composed is held, not dropped, and is
 *    taken exactly once;
 *  - an error or disconnect at idle leaves the last finished answer alone.
 */
class AgentSessionLifecycleTest {

    /** A [WebSocketClient] whose socket the test drives; records when each connect happened. */
    private class RecordingWs(private val now: () -> Long) : WebSocketClient(OkHttpClient()) {
        val incoming = Channel<WsMessage>(Channel.UNLIMITED)
        val connects = mutableListOf<Long>()
        override fun connect(url: String): Flow<WsMessage> {
            connects += now()
            return incoming.receiveAsFlow()
        }
        override fun send(text: String): Boolean = true
        override fun close() {}
    }

    private fun TestScope.client(ws: RecordingWs) =
        ClaudeAgentClient(OkHttpClient(), "ws://box.test", "/ws/agent", ws)

    /** A foreground scope on the test scheduler (advanceUntilIdle drives its delays). */
    private fun TestScope.sessionScope() = CoroutineScope(StandardTestDispatcher(testScheduler) + Job())

    @Test fun `the reconnect delay doubles from one second and is capped`() {
        val delays = (1..AGENT_MAX_RECONNECT_ATTEMPTS).map { agentReconnectDelayMs(it) }
        assertEquals(listOf(1_000L, 2_000L, 4_000L, 8_000L, 16_000L, 30_000L), delays.take(6))
        assertTrue(delays.drop(5).all { it == 30_000L })
        assertNull("past the cap there is no further attempt", agentReconnectDelayMs(AGENT_MAX_RECONNECT_ATTEMPTS + 1))
    }

    @Test fun `successive drops back off instead of retrying every second`() = runTest {
        val ws = RecordingWs { testScheduler.currentTime }
        val c = client(ws)
        val session = sessionScope()
        c.connect("s1", session)
        runCurrent()
        repeat(4) {
            ws.incoming.send(WsMessage.Disconnected)
            advanceUntilIdle()
        }
        assertEquals(listOf(1_000L, 2_000L, 4_000L, 8_000L), ws.connects.zipWithNext { a, b -> b - a })
        session.cancel()
    }

    @Test fun `after the cap the session gives up with an error`() = runTest {
        val ws = RecordingWs { testScheduler.currentTime }
        val c = client(ws)
        val events = mutableListOf<AgentEvent>()
        backgroundScope.launch { c.events.collect { events += it } }
        runCurrent() // subscribed before anything is emitted
        val session = sessionScope()
        c.connect("s1", session)
        runCurrent()
        repeat(AGENT_MAX_RECONNECT_ATTEMPTS + 1) {
            ws.incoming.send(WsMessage.Disconnected)
            advanceUntilIdle()
        }
        assertEquals("one connect plus the capped retries", 1 + AGENT_MAX_RECONNECT_ATTEMPTS, ws.connects.size)
        assertEquals(AgentConnectionState.ERROR, c.connectionState.value)
        runCurrent() // let the background collector take the last event
        assertTrue(events.any { it is AgentEvent.Error && it.message.contains("reconnect", ignoreCase = true) })
        session.cancel()
    }

    @Test fun `a session the server ended is not reconnected`() = runTest {
        val ws = RecordingWs { testScheduler.currentTime }
        val c = client(ws)
        val session = sessionScope()
        c.connect("s1", session)
        runCurrent()
        ws.incoming.send(WsMessage.Text("""{"type":"session_ended","data":"idle timeout"}"""))
        ws.incoming.send(WsMessage.Disconnected)
        advanceUntilIdle()
        assertEquals("no reconnect to a dead session", 1, ws.connects.size)
        assertNull(c.sessionId.value)
        session.cancel()
    }

    // ── the prompt handoff and the idle bubble ───────────────────────────────

    @Test fun `a held prompt is taken exactly once`() {
        val pending = MutableStateFlow<PendingAgentPrompt?>(PendingAgentPrompt("run the tests"))
        val p = pending.value!!
        assertTrue(pending.compareAndSet(p, null))
        assertFalse("a second take (recomposition) gets nothing", pending.compareAndSet(p, null))
    }

    /**
     * The review repro (A05-chat-ui-01): a send with the agents provider navigates to a
     * NEW agent screen, whose client is not built and whose operator is still "" for its
     * first frames. The prompt must wait for the client, not be taken and refused.
     */
    @Test fun `a prompt queued before the agent screen exists is sent once its client is up`() = runTest {
        val pending = MutableStateFlow<PendingAgentPrompt?>(PendingAgentPrompt("run the tests", operator = "op-a"))
        val ready = MutableStateFlow(false)
        val screenOperator = MutableStateFlow("") // the new screen's store read has not landed
        val sent = mutableListOf<Pair<String, String>>()
        backgroundScope.launch {
            deliverHeldAgentPrompts(
                pending = pending,
                ready = ready,
                take = { pending.compareAndSet(it, null) },
                send = { sent += it.text to agentPromptOperator(it.operator, screenOperator.value) },
            )
        }
        runCurrent()
        assertTrue("nothing is sent before the client exists", sent.isEmpty())
        assertTrue("and the prompt is still held", pending.value != null)

        ready.value = true
        runCurrent()
        assertEquals("sent once, as the operator it was queued under", listOf("run the tests" to "op-a"), sent)
        assertNull(pending.value)

        ready.value = false
        ready.value = true
        runCurrent()
        assertEquals("never twice", 1, sent.size)
    }

    @Test fun `a prompt queued while the screen is up is sent at once`() = runTest {
        val pending = MutableStateFlow<PendingAgentPrompt?>(null)
        val ready = MutableStateFlow(true)
        val sent = mutableListOf<String>()
        backgroundScope.launch {
            deliverHeldAgentPrompts(pending, ready, take = { pending.compareAndSet(it, null) }, send = { sent += it.text })
        }
        runCurrent()
        pending.value = PendingAgentPrompt("first", "op-a")
        runCurrent()
        pending.value = PendingAgentPrompt("first", "op-a") // the same text again is a new prompt
        runCurrent()
        assertEquals(listOf("first", "first"), sent)
    }

    @Test fun `the queued operator wins over the screen's, which only fills a blank`() {
        assertEquals("op-a", agentPromptOperator("op-a", ""))
        assertEquals("op-a", agentPromptOperator("op-a", "op-b"))
        assertEquals("op-b", agentPromptOperator("", "op-b"))
        assertEquals("", agentPromptOperator("", ""))
    }

    @Test fun `the chat view model holds an agent prompt until the screen can send it`() {
        // Wiring: the AGENT route queues into a StateFlow (no SharedFlow with replay 0),
        // with the operator read at send time.
        val src = java.io.File(sourceRoot(), "ui/chat/ChatViewModel.kt").readText()
        assertFalse("no lossy SharedFlow handoff", "_agentPromptEvent" in src)
        assertTrue("ChatRoute.AGENT -> queueAgentPrompt(text)" in src)
        assertTrue("the prompt carries the send-time operator", "PendingAgentPrompt(text, operator = currentOperator)" in src)
        val screen = java.io.File(sourceRoot(), "ui/chat/AgentChatScreen.kt").readText().replace("\r\n", "\n")
        assertTrue("the screen waits for its client", "ready = viewModel.clientReady" in screen)
        assertTrue("and takes each prompt once", "take = chat::consumeAgentPrompt" in screen)
        assertTrue("sending under the queued operator", "viewModel.sendPrompt(prompt.text, agentPromptOperator(prompt.operator" in screen)
        val init = screen.substring(screen.indexOf("fun initialize(origin: String"))
        assertTrue("the client marks itself ready", "_clientReady.value = true" in init.substring(0, init.indexOf("\n    }")))
        val send = screen.substring(screen.indexOf("fun sendPrompt(text: String, operator: String"))
        assertTrue("the operator guard reads the resolved operator", "if (op.isBlank())" in send.substring(0, 600))
    }

    @Test fun `an error at idle leaves the finished answer alone`() {
        val done = listOf(UiMessage(role = "user", content = "q"), UiMessage(role = "assistant", content = "the answer"))
        assertSame(done, finishStreamingBubble(done, wasStreaming = false, content = "the answer\nSession closing: idle"))
    }

    @Test fun `an error mid-stream settles the live bubble`() {
        val live = listOf(UiMessage(role = "user", content = "q"), UiMessage(role = "assistant", content = "part", isStreaming = true))
        val out = finishStreamingBubble(live, wasStreaming = true, content = "part\nerror")
        assertEquals("part\nerror", out.last().content)
        assertFalse(out.last().isStreaming)
    }

    private fun sourceRoot(): java.io.File {
        var dir: java.io.File? = java.io.File(".").absoluteFile
        while (dir != null) {
            for (rel in listOf("src/main/java/com/aiblackbox/portal", "app/src/main/java/com/aiblackbox/portal")) {
                val f = java.io.File(dir, rel)
                if (f.isDirectory) return f
            }
            dir = dir.parentFile
        }
        error("source root not found")
    }
}
