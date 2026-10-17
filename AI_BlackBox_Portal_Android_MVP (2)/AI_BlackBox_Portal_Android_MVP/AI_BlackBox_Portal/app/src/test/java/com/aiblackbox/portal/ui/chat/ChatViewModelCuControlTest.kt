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
    }

    @Test fun `an HTTP error is a failure, not an exception`() = runTest {
        server.enqueue(MockResponse.Builder().code(500).body("""{"detail":"boom"}""").build())

        val result = ChatViewModel.requestCuControl(api, "/chat/cu-new-session", "{}")

        assertFalse(result.success)
        assertEquals("boom", result.error)
    }

    @Test fun `an unreadable or unconfirmed reply is a failure`() = runTest {
        server.enqueue(MockResponse.Builder().body("<html>proxy error</html>").build())
        server.enqueue(MockResponse.Builder().body("""{"message":"ok?"}""").build())

        val garbage = ChatViewModel.requestCuControl(api, "/chat/cu-stop", "{}")
        val silent = ChatViewModel.requestCuControl(api, "/chat/cu-stop", "{}")

        assertFalse(garbage.success)
        assertFalse(garbage.error.isNullOrBlank())
        assertFalse(silent.success)
        assertFalse(silent.error.isNullOrBlank())
    }

    @Test fun `no api client is a failure and sends nothing`() = runTest {
        val result = ChatViewModel.requestCuControl(null, "/chat/cu-stop", "{}")
        assertFalse(result.success)
        assertFalse(result.error.isNullOrBlank())
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
}
