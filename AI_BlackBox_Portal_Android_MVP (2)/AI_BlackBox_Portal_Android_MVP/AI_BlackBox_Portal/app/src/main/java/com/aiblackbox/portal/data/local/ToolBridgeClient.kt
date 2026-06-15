package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.ToolExecuteRequest
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import com.aiblackbox.portal.data.model.ToolSearchRequest
import com.aiblackbox.portal.data.model.ToolSearchResponse
import kotlinx.serialization.json.JsonObject

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
 * Offline / graceful-degradation handling is a SEPARATE later task — a non-2xx
 * from either endpoint surfaces as the [java.io.IOException] thrown by
 * [BlackBoxApi.post], which is allowed to propagate here.
 */
class ToolBridgeClient(private val api: BlackBoxApi) {

    private val json get() = api.json

    /**
     * POST /local/tools/search — discover up to [k] tool schemas matching [query].
     *
     * `operator` is intentionally omitted: the backend treats tool discovery as
     * operator-agnostic/global. Returns an empty list when the backend reports
     * `success=false` or omits the `tools` array; otherwise the parsed schemas
     * (each with its arbitrary `parameters` JSON-Schema object intact).
     *
     * Do NOT pass a blank [query] — the backend answers 400 (which would
     * propagate as an IOException out of [BlackBoxApi.post]).
     */
    suspend fun searchTools(query: String, k: Int = 5): List<ToolSchema> {
        val body = json.encodeToString(
            ToolSearchRequest.serializer(),
            ToolSearchRequest(query = query, k = k),
        )
        val responseText = api.post("/local/tools/search", body)
        val parsed = json.decodeFromString(ToolSearchResponse.serializer(), responseText)
        if (!parsed.success) return emptyList()
        return parsed.tools
    }

    /**
     * POST /local/tools/execute — run [tool] with [params] for [operator].
     *
     * Returns a [ToolResult] whose `result` is the tool's raw output as a
     * nullable JsonElement (string, object, list, number, or null) — the caller
     * inspects/casts it as needed.
     */
    suspend fun execute(
        tool: String,
        params: JsonObject = JsonObject(emptyMap()),
        operator: String,
    ): ToolResult {
        val body = json.encodeToString(
            ToolExecuteRequest.serializer(),
            ToolExecuteRequest(tool = tool, params = params, operator = operator),
        )
        val responseText = api.post("/local/tools/execute", body)
        return json.decodeFromString(ToolResult.serializer(), responseText)
    }
}
