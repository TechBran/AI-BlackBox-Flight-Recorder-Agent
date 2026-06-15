package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Unit tests for [ToolBridgeClient] — the Android client for the hub's two-hop
 * on-device tool-bridge endpoints:
 *   - POST /local/tools/search   → discover tool schemas by semantic query
 *   - POST /local/tools/execute  → run a discovered tool and return its result
 *
 * Hermetic: a real OkHttp call path is exercised against MockWebServer
 * (mockwebserver3, already a testImplementation dep), so the actual BlackBoxApi
 * base-URL + OkHttpClient + kotlinx.serialization parse paths run — matching
 * LocalModelApiTest's convention.
 *
 * Coverage:
 *   1. searchTools()  — parses {"success":true,"tools":[...]} into List<ToolSchema>,
 *      preserving the nested arbitrary `parameters` JsonObject.
 *   2. searchTools()  — sends POST /local/tools/search with {query, k} in the body.
 *   3. searchTools()  — returns empty list when {"success":false} (or tools absent).
 *   4. execute()  — posts {tool, params, operator} and parses {success, result}.
 *   5. execute()  — tolerates a STRING result and a null result (JsonElement? modeling).
 *   6. execute()  — a tool-ran-but-failed {"success":false, result} flows through
 *      intact (distinct from a transport IOException); array result parses; the
 *      default k=5 is emitted on the wire.
 */
class ToolBridgeClientTest {

    private lateinit var server: MockWebServer
    private lateinit var client: ToolBridgeClient

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        // BlackBoxApi expects a baseUrl WITHOUT a trailing slash so that the
        // path concatenation ("$baseUrl$path") preserves the leading slash.
        val baseUrl = server.url("").toString().trimEnd('/')
        client = ToolBridgeClient(BlackBoxApi(baseUrl))
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
    // 1. searchTools() — parse tools + preserve nested parameters JsonObject.
    // -------------------------------------------------------------------------

    @Test
    fun `searchTools parses tools and preserves nested parameters object`() = runTest {
        enqueueJson(
            """
            {"success":true,"tools":[
              {"name":"search_snapshots","description":"Semantic search over the ledger.",
               "parameters":{"type":"object","properties":{"query":{"type":"string"}},
               "required":["query"]}}
            ]}
            """.trimIndent()
        )

        val tools = client.searchTools("find past work", k = 3)

        assertEquals(1, tools.size)
        val t = tools[0]
        assertEquals("search_snapshots", t.name)
        assertEquals("Semantic search over the ledger.", t.description)
        // The arbitrary JSON-Schema parameters object must survive intact.
        assertEquals(
            "nested parameters.type must survive",
            "object",
            t.parameters["type"]?.jsonPrimitive?.content,
        )
        val props = t.parameters["properties"]?.jsonObject
        assertTrue("parameters.properties must survive", props?.containsKey("query") == true)
    }

    // -------------------------------------------------------------------------
    // 2. searchTools() — request shape: POST /local/tools/search {query, k}.
    // -------------------------------------------------------------------------

    @Test
    fun `searchTools posts query and k to the search endpoint`() = runTest {
        enqueueJson("""{"success":true,"tools":[]}""")

        client.searchTools("grocery list helper", k = 7)

        val recorded = server.takeRequest()
        assertEquals("/local/tools/search", recorded.target)
        assertEquals("POST", recorded.method)
        val sentBody = recorded.body!!.utf8()
        assertTrue("query in body", sentBody.contains("\"query\":\"grocery list helper\""))
        assertTrue("k in body", sentBody.contains("\"k\":7"))
    }

    // -------------------------------------------------------------------------
    // 3. searchTools() — {"success":false} → empty list (graceful, no crash).
    // -------------------------------------------------------------------------

    @Test
    fun `searchTools returns empty list when success is false`() = runTest {
        enqueueJson("""{"success":false,"error":"query required"}""")
        val tools = client.searchTools("anything", k = 5)
        assertTrue("must be empty on success=false", tools.isEmpty())
    }

    @Test
    fun `searchTools returns empty list when tools field is absent`() = runTest {
        enqueueJson("""{"success":true}""")
        val tools = client.searchTools("anything", k = 5)
        assertTrue("must be empty when tools missing", tools.isEmpty())
    }

    // -------------------------------------------------------------------------
    // 4. execute() — POST {tool, params, operator} → {success, result}.
    // -------------------------------------------------------------------------

    @Test
    fun `execute posts tool params operator and parses object result`() = runTest {
        enqueueJson(
            """{"success":true,"result":{"count":42,"label":"done"}}"""
        )

        val params = buildJsonObject {
            put("query", JsonPrimitive("milk"))
        }
        val result = client.execute(tool = "search_snapshots", params = params, operator = "Brandon")

        assertTrue("success true", result.success)
        // result JsonElement must be intact, an object with the emitted fields.
        val obj = (result.result as? JsonObject)
        assertEquals("42", obj?.get("count")?.jsonPrimitive?.content)
        assertEquals("done", obj?.get("label")?.jsonPrimitive?.content)

        val recorded = server.takeRequest()
        assertEquals("/local/tools/execute", recorded.target)
        assertEquals("POST", recorded.method)
        val sentBody = recorded.body!!.utf8()
        assertTrue("tool in body", sentBody.contains("\"tool\":\"search_snapshots\""))
        assertTrue("operator in body", sentBody.contains("\"operator\":\"Brandon\""))
        // The nested params object's key must be carried through.
        assertTrue("params.query in body", sentBody.contains("\"query\":\"milk\""))
    }

    // -------------------------------------------------------------------------
    // 5. execute() — STRING result + null result both parse without throwing.
    // -------------------------------------------------------------------------

    @Test
    fun `execute parses a string result`() = runTest {
        enqueueJson("""{"success":true,"result":"hello"}""")
        val result = client.execute(tool = "echo", operator = "system")
        assertTrue(result.success)
        assertEquals("hello", result.result?.jsonPrimitive?.content)
    }

    @Test
    fun `execute parses a null result without throwing`() = runTest {
        enqueueJson("""{"success":true,"result":null}""")
        val result = client.execute(tool = "noop", operator = "system")
        assertTrue(result.success)
        assertNull("null result must stay null", result.result)
    }

    // -------------------------------------------------------------------------
    // 6. execute() — tool-ran-but-failed (success=false WITH a payload) is a
    //    distinct surface from a transport error (IOException). The on-device
    //    loop (Task 3.2) must be able to feed this failure back to the model, so
    //    the client must pass {success:false, result} through untouched, NOT map
    //    it to a thrown exception or an empty result.
    // -------------------------------------------------------------------------

    @Test
    fun `execute surfaces a tool failure (success false) with its payload intact`() = runTest {
        enqueueJson("""{"success":false,"result":"tool error: bad args"}""")
        val result = client.execute(tool = "search_snapshots", operator = "Brandon")
        assertFalse("tool failure must report success=false", result.success)
        assertEquals(
            "the failure payload must survive for the model to verbalize",
            "tool error: bad args",
            result.result?.jsonPrimitive?.content,
        )
    }

    // -------------------------------------------------------------------------
    // 6b. execute() — an ARRAY result parses (the KDoc promises list results, and
    //     Task 3.2 feeds `result` back to the on-device FC SDK verbatim).
    // -------------------------------------------------------------------------

    @Test
    fun `execute parses an array result`() = runTest {
        enqueueJson("""{"success":true,"result":[1,2,3]}""")
        val result = client.execute(tool = "list_recent_snapshots", operator = "system")
        assertTrue(result.success)
        val arr = result.result as? JsonArray
        assertEquals("array result must parse as JsonArray of 3", 3, arr?.size)
        assertEquals("1", arr?.get(0)?.jsonPrimitive?.content)
    }

    // -------------------------------------------------------------------------
    // 6c. searchTools() — the default k=5 is emitted on the wire (encodeDefaults
    //     is on). Guards the contract against a future encodeDefaults flip or a
    //     @SerialName slip on the request DTO.
    // -------------------------------------------------------------------------

    @Test
    fun `searchTools emits the default k when none is given`() = runTest {
        enqueueJson("""{"success":true,"tools":[]}""")
        client.searchTools("anything")  // k defaults to 5
        val sentBody = server.takeRequest().body!!.utf8()
        assertTrue("default k=5 must be on the wire", sentBody.contains("\"k\":5"))
    }

    // -------------------------------------------------------------------------
    // 7. (Task 3.4) Graceful offline: a transport failure (the mesh is
    //    unreachable) must NOT throw out of either bridge method. The on-device
    //    Gemma loop runs OFFLINE but its tools live on the BlackBox over the
    //    network; a single tool call that can't reach the mesh must degrade
    //    gracefully so the whole turn does not fault.
    //
    //    Offline is simulated by closing the MockWebServer BEFORE the call so the
    //    socket is refused → OkHttp raises an IOException inside BlackBoxApi.post.
    //    (Most portable across mockwebserver3 versions vs. a SocketPolicy.)
    // -------------------------------------------------------------------------

    @Test
    fun `execute returns a graceful failure ToolResult when the mesh is unreachable`() = runTest {
        server.close() // socket refused → connection failure → IOException in BlackBoxApi

        val result = client.execute(tool = "search_snapshots", operator = "Brandon")

        assertFalse("an unreachable mesh must yield success=false, not throw", result.success)
        val msg = result.result?.jsonPrimitive?.content
        assertTrue("a failure payload must be present for the model to verbalize", msg != null)
        assertTrue(
            "the offline message must name the tool that failed (got: $msg)",
            msg!!.contains("search_snapshots"),
        )
        assertTrue(
            "the offline message must mention not reaching BlackBox (got: $msg)",
            msg.contains("couldn't reach BlackBox"),
        )
    }

    @Test
    fun `searchTools returns an empty list when the mesh is unreachable`() = runTest {
        server.close() // socket refused → connection failure → IOException in BlackBoxApi

        // Must NOT throw — an empty list means "no tools available (possibly offline)".
        val tools = client.searchTools("find past work", k = 3)

        assertTrue("an unreachable mesh must yield emptyList, not throw", tools.isEmpty())
    }

    // -------------------------------------------------------------------------
    // 7b. (Task 3.4) A non-2xx (e.g. 500) reaches BlackBoxApi as an IOException
    //    too, so it now ALSO degrades gracefully for execute() rather than
    //    throwing — same offline contract as a dead socket.
    // -------------------------------------------------------------------------

    @Test
    fun `execute returns a graceful failure ToolResult on a non-2xx response`() = runTest {
        enqueueJson("""{"detail":"boom"}""", code = 500)

        val result = client.execute(tool = "do_thing", operator = "system")

        assertFalse("a non-2xx must yield success=false, not throw (3.4 behavior)", result.success)
        assertTrue(
            "the failure payload must name the tool",
            result.result?.jsonPrimitive?.content?.contains("do_thing") == true,
        )
    }
}
