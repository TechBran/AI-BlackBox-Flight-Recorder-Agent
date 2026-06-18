package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.CompleteRequest
import com.aiblackbox.portal.data.model.ToolCallRecord
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Unit tests for [TurnClient] — the Android client for the two server-bracketed
 * on-device turn endpoints:
 *   - POST /local/turn/prepare   → assemble the per-turn package server-side
 *   - POST /local/turn/complete  → persist + mint the finished turn
 *
 * Hermetic: a real OkHttp call path is exercised against MockWebServer
 * (mockwebserver3, already a testImplementation dep), so the actual BlackBoxApi
 * base-URL + OkHttpClient + kotlinx.serialization parse paths run — matching
 * ToolBridgeClientTest's convention.
 *
 * OFFLINE contract under test: TurnClient returns a NULLABLE response and `null`
 * is the explicit OFFLINE / unreachable signal (Task 11 degraded mode keys off
 * it). Both a transport failure and a non-2xx surface as an IOException out of
 * [BlackBoxApi.post]; both are caught → null. A SerializationException (a
 * malformed body — a real bug) is intentionally NOT caught and still propagates.
 */
class TurnClientTest {

    private lateinit var server: MockWebServer
    private lateinit var client: TurnClient

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        // BlackBoxApi expects a baseUrl WITHOUT a trailing slash so that the
        // path concatenation ("$baseUrl$path") preserves the leading slash.
        val baseUrl = server.url("").toString().trimEnd('/')
        client = TurnClient(BlackBoxApi(baseUrl))
    }

    @After fun tearDown() {
        server.close()
    }

    private fun enqueueJson(body: String, code: Int = 200) {
        server.enqueue(
            MockResponse.Builder()
                .code(code)
                .headers(headersOf("Content-Type", "application/json"))
                .body(body)
                .build()
        )
    }

    // -------------------------------------------------------------------------
    // A. prepare() — decodes a full 200 package and posts to the right path.
    // -------------------------------------------------------------------------

    @Test
    fun `prepare decodes a 200 package`() = runBlocking {
        enqueueJson(
            """
            {"success":true,"turn_id":"TURN-42","system_prompt":"You are the BlackBox.",
             "tools":[
               {"name":"search_snapshots","description":"Semantic search over the ledger.",
                "parameters":{"type":"object","properties":{"query":{"type":"string"}}}}
             ],
             "provenance":{"semantic":["SNAP-1"],"checkpoint":["SNAP-2"]},
             "budget":{"package_chars":1234,"cap_chars":16000}}
            """.trimIndent()
        )

        val resp = client.prepare(prompt = "what did we do", operator = "Brandon")

        assertNotNull("a 200 package must decode to a non-null response", resp)
        assertEquals("TURN-42", resp!!.turnId)
        assertEquals("You are the BlackBox.", resp.systemPrompt)
        assertEquals(1, resp.tools.size)
        assertEquals("search_snapshots", resp.tools[0].name)
        assertEquals(16000, resp.budget.capChars)

        val recorded = server.takeRequest()
        assertEquals("/local/turn/prepare", recorded.target)
        assertEquals("POST", recorded.method)
        val sentBody = recorded.body!!.utf8()
        assertTrue("prompt in body", sentBody.contains("\"prompt\":\"what did we do\""))
        assertTrue("operator in body", sentBody.contains("\"operator\":\"Brandon\""))
    }

    // -------------------------------------------------------------------------
    // B. prepare() — a non-2xx (IOException out of BlackBoxApi.post) → null.
    // -------------------------------------------------------------------------

    @Test
    fun `prepare returns null on non-2xx`() = runBlocking {
        enqueueJson("""{"detail":"boom"}""", code = 500)
        val resp = client.prepare(prompt = "anything", operator = "Brandon")
        assertNull("a non-2xx must yield null (offline signal), not throw", resp)
    }

    // -------------------------------------------------------------------------
    // C. prepare() — a transport failure (dead socket) → null.
    // -------------------------------------------------------------------------

    @Test
    fun `prepare returns null on transport failure`() = runBlocking {
        server.close() // socket refused → connection failure → IOException in BlackBoxApi
        val resp = client.prepare(prompt = "anything", operator = "Brandon")
        assertNull("an unreachable mesh must yield null (offline signal), not throw", resp)
    }

    // -------------------------------------------------------------------------
    // D. complete() — decodes a 200 and posts to the right path.
    // -------------------------------------------------------------------------

    @Test
    fun `complete decodes a 200`() = runBlocking {
        enqueueJson("""{"success":true,"snap_id":"SNAP-X","checkpoint_triggered":true}""")

        val req = CompleteRequest(
            turnId = "TURN-42",
            operator = "Brandon",
            prompt = "what did we do",
            finalResponse = "We shipped the ledger.",
            toolTranscript = listOf(
                ToolCallRecord(
                    name = "search_snapshots",
                    args = buildJsonObject { put("query", JsonPrimitive("ledger")) },
                    result = "1 hit",
                ),
            ),
        )
        val resp = client.complete(req)

        assertNotNull("a 200 must decode to a non-null response", resp)
        assertTrue(resp!!.success)
        assertEquals("SNAP-X", resp.snapId)
        assertEquals(true, resp.checkpointTriggered)

        val recorded = server.takeRequest()
        assertEquals("/local/turn/complete", recorded.target)
        assertEquals("POST", recorded.method)
        val sentBody = recorded.body!!.utf8()
        assertTrue("turn_id in body", sentBody.contains("\"turn_id\":\"TURN-42\""))
        assertTrue("final_response in body", sentBody.contains("\"final_response\":\"We shipped the ledger.\""))
    }

    // -------------------------------------------------------------------------
    // E. complete() — a non-2xx → null (same offline contract).
    // -------------------------------------------------------------------------

    @Test
    fun `complete returns null on non-2xx`() = runBlocking {
        enqueueJson("""{"detail":"bad request"}""", code = 400)
        val resp = client.complete(CompleteRequest(turnId = "TURN-42", operator = "Brandon"))
        assertNull("a non-2xx must yield null (offline signal), not throw", resp)
    }
}
