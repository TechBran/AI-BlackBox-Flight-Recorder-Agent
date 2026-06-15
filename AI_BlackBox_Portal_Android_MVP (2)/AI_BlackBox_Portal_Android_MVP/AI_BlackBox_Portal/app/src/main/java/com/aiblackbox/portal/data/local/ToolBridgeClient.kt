package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.ToolExecuteRequest
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import com.aiblackbox.portal.data.model.ToolSearchRequest
import com.aiblackbox.portal.data.model.ToolSearchResponse
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.io.IOException

/**
 * Android client for the hub's two-hop on-device tool bridge — the endpoints the
 * on-device Gemma agent loop calls BACK into so the phone-resident model can
 * discover and run BlackBox tools (Orchestrator/routes/local_routes.py):
 *   - POST /local/tools/search   → [searchTools] (semantic tool discovery)
 *   - POST /local/tools/execute  → [execute]     (run a discovered tool)
 *
 * Mirrors [com.aiblackbox.portal.data.api.LocalModelApi]: it reuses
 * [BlackBoxApi]'s base URL + lenient kotlinx.serialization `json`, so the
 * orchestrator host is never hardcoded and request bodies are built from
 * `@Serializable` DTOs rather than hand-concatenated strings.
 *
 * **Graceful offline contract (Task 3.4).** The on-device model works OFFLINE,
 * but its tools live on the BlackBox over the network. A single tool call that
 * can't reach the mesh must degrade GRACEFULLY — it must NOT throw and fault the
 * whole turn. So both methods catch the [IOException] raised on a transport
 * failure or a non-2xx (which [BlackBoxApi.post] maps to an `IOException`) and
 * return a structured "needs connection" result the model can verbalize:
 *   - [execute]     → `ToolResult(success = false, result = <offline message>)`.
 *   - [searchTools] → `emptyList()` (a `List` can't carry a distinct offline
 *     signal; FcLoop surfaces an empty result as graceful feedback).
 * Only [IOException] is caught: a `SerializationException` (a malformed body) is
 * a real bug, not "offline", and still propagates.
 */
class ToolBridgeClient(private val api: BlackBoxApi) : ToolBridge {

    private val json get() = api.json

    /**
     * POST /local/tools/search — discover up to [k] tool schemas matching [query].
     *
     * `operator` is intentionally omitted: the backend treats tool discovery as
     * operator-agnostic/global. Returns an empty list when the backend reports
     * `success=false` or omits the `tools` array; otherwise the parsed schemas
     * (each with its arbitrary `parameters` JSON-Schema object intact).
     *
     * Do NOT pass a blank [query] — the backend answers 400. Per the Task 3.4
     * graceful-offline contract that 400 (like any non-2xx or a dead socket) is an
     * [IOException] which is CAUGHT here and turned into an empty list rather than
     * thrown: a `List` return cannot carry a distinct "offline" signal, so an
     * empty list means "no tools available — possibly offline", and FcLoop
     * surfaces that as explicit graceful feedback to the model. A
     * `SerializationException` (a malformed body — a real bug) still propagates.
     */
    override suspend fun searchTools(query: String, k: Int): List<ToolSchema> {
        val body = json.encodeToString(
            ToolSearchRequest.serializer(),
            ToolSearchRequest(query = query, k = k),
        )
        return try {
            val responseText = api.post("/local/tools/search", body)
            val parsed = json.decodeFromString(ToolSearchResponse.serializer(), responseText)
            if (!parsed.success) emptyList() else parsed.tools
        } catch (_: IOException) {
            // Transport failure / non-2xx → the mesh is unreachable. Degrade to an
            // empty result instead of faulting the turn.
            emptyList()
        }
    }

    /**
     * POST /local/tools/execute — run [tool] with [params] for [operator].
     *
     * Returns a [ToolResult] whose `result` is the tool's raw output as a
     * nullable JsonElement (string, object, list, number, or null) — the caller
     * inspects/casts it as needed.
     *
     * Per the Task 3.4 graceful-offline contract, a transport failure or a non-2xx
     * (both surface as an [IOException] from [BlackBoxApi.post]) is CAUGHT here and
     * returned as `ToolResult(success = false, result = <offline message>)` rather
     * than thrown — so a single unreachable tool call does not fault the whole
     * turn; the model can verbalize the failure and continue. A
     * `SerializationException` (a malformed body — a real bug) still propagates.
     */
    override suspend fun execute(
        tool: String,
        params: JsonObject,
        operator: String,
    ): ToolResult {
        val body = json.encodeToString(
            ToolExecuteRequest.serializer(),
            ToolExecuteRequest(tool = tool, params = params, operator = operator),
        )
        return try {
            val responseText = api.post("/local/tools/execute", body)
            json.decodeFromString(ToolResult.serializer(), responseText)
        } catch (e: IOException) {
            ToolResult(
                success = false,
                result = JsonPrimitive(
                    "$tool is unavailable right now — couldn't reach BlackBox (${e.message ?: "offline"})",
                ),
            )
        }
    }
}
