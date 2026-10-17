package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * A13-ui-ops-05 — the Computer-Use header's STOP and New buttons.
 *
 * The server resolves the session to stop from the OPERATOR (Orchestrator
 * browser/session_manager.get_session matches `s.operator == operator`, and with
 * no operator it falls back to `_operator_sessions[""]`, which never exists), so
 * a stop body without the operator was a silent no-op: `{"success": false}` —
 * while the phone flipped its UI to "stopped" and hid the STOP button. New only
 * cleared local flows and never told the server.
 *
 * Same strategy as [ChatViewModelLiveModelIdsTest]: the AndroidViewModel can't be
 * built on the plain JVM, so the bodies, the request + reply parse, and the
 * status mapping are pure companion cores the instance methods wire up.
 */
class ChatViewModelCuControlTest {
    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi

    @Before fun setUp() {
        server = MockWebServer(); server.start()
        api = BlackBoxApi(server.url("").toString().trimEnd('/'))
    }
    @After fun tearDown() = server.close()

    private fun obj(raw: String): JsonObject = Json.parseToJsonElement(raw).jsonObject
    private fun JsonObject.str(key: String): String? = this[key]?.jsonPrimitive?.content

    // ── Request bodies ──

    @Test fun `stop body names the operator, the session and the model`() {
        val body = obj(ChatViewModel.buildCuStopBody("op-a", "sess-1", "cu-model-x"))
        assertEquals("op-a", body.str("operator"))
        assertEquals("sess-1", body.str("session_id"))
        assertEquals("cu-model-x", body.str("model"))
    }

    @Test fun `stop body before the session id arrives still names the operator`() {
        // The cu_session SSE event may not have landed yet; the server then stops the
        // operator's most recent session (Portal chat-send.js stopCUTask parity).
        val body = obj(ChatViewModel.buildCuStopBody("op-a", null, ""))
        assertEquals("op-a", body.str("operator"))
        assertEquals("", body.str("session_id"))
    }

    @Test fun `new-session body names the operator and the model`() {
        val body = obj(ChatViewModel.buildCuNewSessionBody("op-b", "cu-model-y"))
        assertEquals("op-b", body.str("operator"))
        assertEquals("cu-model-y", body.str("model"))
    }

    // ── Request + reply ──

    @Test fun `a confirmed stop is a success and posts to cu-stop`() = runTest {
        server.enqueue(MockResponse.Builder()
            .body("""{"success":true,"message":"Stop requested for session sess-1"}""").build())

        val result = ChatViewModel.requestCuControl(
            api, "/chat/cu-stop", ChatViewModel.buildCuStopBody("op-a", "sess-1", ""))

        assertTrue(result.success)
        assertNull(result.error)
        val req = server.takeRequest()
        assertEquals("/chat/cu-stop", req.target)
        assertEquals("op-a", obj(req.body!!.utf8()).str("operator"))
    }

    @Test fun `the server's refusal is a failure carrying its reason`() = runTest {
        server.enqueue(MockResponse.Builder()
            .body("""{"success":false,"error":"No running CU task"}""").build())

        val result = ChatViewModel.requestCuControl(api, "/chat/cu-stop", "{}")

        assertFalse(result.success)
        assertEquals("No running CU task", result.error)
        // The server answered: its refusal is a statement about the session.
        assertTrue(result.refused)
    }

    @Test fun `an HTTP error is a failure, not an exception`() = runTest {
        server.enqueue(MockResponse.Builder().code(500).body("""{"detail":"boom"}""").build())

        val result = ChatViewModel.requestCuControl(api, "/chat/cu-new-session", "{}")

        assertFalse(result.success)
        assertEquals("boom", result.error)
        // The request failed: that says nothing about whether a task is running.
        assertFalse(result.refused)
    }

    @Test fun `an unreadable or unconfirmed reply is a failure`() = runTest {
        server.enqueue(MockResponse.Builder().body("<html>proxy error</html>").build())
        server.enqueue(MockResponse.Builder().body("""{"message":"ok?"}""").build())

        val garbage = ChatViewModel.requestCuControl(api, "/chat/cu-stop", "{}")
        val silent = ChatViewModel.requestCuControl(api, "/chat/cu-stop", "{}")

        assertFalse(garbage.success)
        assertFalse(garbage.error.isNullOrBlank())
        assertFalse(garbage.refused)
        assertFalse(silent.success)
        assertFalse(silent.error.isNullOrBlank())
    }

    @Test fun `no api client is a failure and sends nothing`() = runTest {
        val result = ChatViewModel.requestCuControl(null, "/chat/cu-stop", "{}")
        assertFalse(result.success)
        assertFalse(result.error.isNullOrBlank())
        assertFalse(result.refused)
        assertEquals(0, server.requestCount)
    }

    // ── Status mapping ──

    @Test fun `the UI says stopped only when the server confirmed the stop`() {
        val ok = ChatViewModel.CuControlResult(success = true, error = null)
        val refused = ChatViewModel.CuControlResult(success = false, error = "No running CU task")

        assertEquals("stopped", ChatViewModel.cuStatusAfterStop("running", ok))
        // A refused or failed stop leaves the agent's status alone, so STOP stays on screen.
        assertEquals("running", ChatViewModel.cuStatusAfterStop("running", refused))
        assertEquals("complete", ChatViewModel.cuStatusAfterStop("complete", refused))
    }

    // ── Server status (review of A13-ui-ops-05) ──
    //
    // /chat/cu-new-session stops and destroys the operator's most recent browser
    // session AND their Gemini session, whoever started them (a cron job, the
    // use_computer tool, the Portal, this phone before an app restart), so New
    // cannot trust the phone's own cuStatus to decide whether to ask first.

    private fun status(s: String) =
        MockResponse.Builder().body("""{"status":"$s","session_id":"sess-9","step":3,"total":15}""").build()

    @Test fun `cu-status is asked for the operator's session and its status read`() = runTest {
        server.enqueue(status("running"))

        val got = ChatViewModel.fetchCuStatus(api, "op a&b", sessionId = null, model = "cu-model-x")

        assertEquals("running", got)
        val url = server.takeRequest().url
        assertEquals("/chat/cu-status", url.encodedPath)
        assertEquals("op a&b", url.queryParameter("operator"))
        assertEquals("cu-model-x", url.queryParameter("model"))
        // No session id: the server reports the operator's most recent session, the one
        // /chat/cu-new-session would destroy.
        assertNull(url.queryParameter("session_id"))
    }

    @Test fun `cu-status names the session when the phone knows it`() = runTest {
        server.enqueue(status("complete"))

        ChatViewModel.fetchCuStatus(api, "op-a", sessionId = "sess-1", model = "")

        assertEquals("sess-1", server.takeRequest().url.queryParameter("session_id"))
    }

    @Test fun `an unreachable or unreadable cu-status is unknown, not idle`() = runTest {
        server.enqueue(MockResponse.Builder().code(502).body("bad gateway").build())
        server.enqueue(MockResponse.Builder().body("<html>proxy</html>").build())

        assertNull(ChatViewModel.fetchCuStatus(api, "op-a", null, ""))
        assertNull(ChatViewModel.fetchCuStatus(api, "op-a", null, ""))
        assertNull(ChatViewModel.fetchCuStatus(null, "op-a", null, ""))
    }

    @Test fun `the probes cover the browser session and the Gemini session`() {
        val backends = mapOf("" to "anthropic", "claude-cu" to "anthropic", "gemini-cu" to "google")

        // "" resolves browser-first on the server; a google-backend id resolves Gemini-first.
        assertEquals(listOf("", "claude-cu", "gemini-cu"),
            ChatViewModel.cuStatusProbeModels("claude-cu", backends))
        assertEquals(listOf("", "gemini-cu"), ChatViewModel.cuStatusProbeModels("gemini-cu", backends))
        // No catalog yet: the browser probe plus whatever model is selected.
        assertEquals(listOf(""), ChatViewModel.cuStatusProbeModels("", emptyMap()))
        assertEquals(listOf("", "some-model"), ChatViewModel.cuStatusProbeModels("some-model", emptyMap()))
    }

    @Test fun `a task running anywhere on the server counts as running`() = runTest {
        // The browser session is finished, but the operator's Gemini session is running.
        server.enqueue(status("complete"))
        server.enqueue(status("running"))

        val check = ChatViewModel.checkServerCuRunning(api, "op-a", listOf("", "gemini-cu"))

        assertEquals(ChatViewModel.CuRunCheck.RUNNING, check)
        assertEquals(2, server.requestCount)
    }

    @Test fun `a starting session counts as running`() = runTest {
        server.enqueue(status("starting"))
        assertEquals(ChatViewModel.CuRunCheck.RUNNING,
            ChatViewModel.checkServerCuRunning(api, "op-a", listOf("")))
    }

    @Test fun `nothing live on the server is clear, a failed probe is unknown`() = runTest {
        server.enqueue(status("idle"))
        server.enqueue(status("complete"))
        assertEquals(ChatViewModel.CuRunCheck.CLEAR,
            ChatViewModel.checkServerCuRunning(api, "op-a", listOf("", "gemini-cu")))

        server.enqueue(status("idle"))
        server.enqueue(MockResponse.Builder().code(503).body("warming up").build())
        assertEquals(ChatViewModel.CuRunCheck.UNKNOWN,
            ChatViewModel.checkServerCuRunning(api, "op-a", listOf("", "gemini-cu")))
    }

    @Test fun `New asks first whenever a task may be running`() {
        val clear = ChatViewModel.CuRunCheck.CLEAR
        val running = ChatViewModel.CuRunCheck.RUNNING
        val unknown = ChatViewModel.CuRunCheck.UNKNOWN

        // The case the review found: the phone is idle, a cron agent is running on the box.
        val serverOnly = ChatViewModel.newSessionQuestion("idle", running)
        assertNotNull(serverOnly)
        assertTrue(serverOnly!!.contains("A task is running"))
        assertNotNull(ChatViewModel.newSessionQuestion("running", clear))
        // The box could not be asked: still ask, never destroy a session silently.
        val unsure = ChatViewModel.newSessionQuestion("complete", unknown)
        assertNotNull(unsure)
        assertTrue(unsure!!.contains("Could not check"))
        // Nothing running anywhere: no dialog.
        assertNull(ChatViewModel.newSessionQuestion("idle", clear))
        assertNull(ChatViewModel.newSessionQuestion("complete", clear))
    }

    @Test fun `a refused stop takes the server's word that the task ended`() {
        // Stream dropped after the task finished: the phone still says running, and the
        // server refuses with "No running CU task". Its cu-status settles the screen.
        assertEquals("complete", ChatViewModel.cuStatusAfterRefusedStop("running", "complete"))
        assertEquals("stopped", ChatViewModel.cuStatusAfterRefusedStop("running", "stopped"))
        assertEquals("stopped", ChatViewModel.cuStatusAfterRefusedStop("running", "error"))
        assertEquals("stopped", ChatViewModel.cuStatusAfterRefusedStop("running", "idle"))
        // Still live on the server, or the server could not be asked: change nothing.
        assertEquals("running", ChatViewModel.cuStatusAfterRefusedStop("running", "running"))
        assertEquals("running", ChatViewModel.cuStatusAfterRefusedStop("running", "starting"))
        assertEquals("running", ChatViewModel.cuStatusAfterRefusedStop("running", null))
    }
}
