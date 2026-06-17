package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.serialization.json.JsonObject

/**
 * In-test [ToolBridge] double for [FcLoop.runAgent]. Stands in for
 * [ToolBridgeClient] so the tool loop is exercisable offline, on the JVM, with no
 * network (no MockWebServer needed at this layer).
 *
 * **Scriptable:**
 *  - [searchMap] — query → schemas returned by [searchTools]; an unknown query
 *    returns an empty list.
 *  - [executeFn] — `(tool, params) -> ToolResult` for [execute]; defaults to a
 *    success with null result.
 *
 * **Records** [searchCalls] (queries, in order) and [executeCalls]
 * (`tool to params`, in order) for assertions.
 */
class FakeToolBridge(
    private val searchMap: Map<String, List<ToolSchema>> = emptyMap(),
    private val executeFn: (tool: String, params: JsonObject) -> ToolResult =
        { _, _ -> ToolResult(success = true, result = null) },
) : ToolBridge {

    /** Every query passed to [searchTools], in order. */
    val searchCalls: MutableList<String> = mutableListOf()

    /** Every (tool, params) passed to [execute], in order. */
    val executeCalls: MutableList<Pair<String, JsonObject>> = mutableListOf()

    /** Every `operator` passed to [execute], in order (parallel to [executeCalls]). */
    val executeOperators: MutableList<String> = mutableListOf()

    /** Every `k` passed to [searchTools], in order (parallel to [searchCalls]). */
    val searchKs: MutableList<Int> = mutableListOf()

    override suspend fun searchTools(query: String, k: Int): List<ToolSchema> {
        searchCalls.add(query)
        searchKs.add(k)
        return searchMap[query] ?: emptyList()
    }

    override suspend fun execute(
        tool: String,
        params: JsonObject,
        operator: String,
    ): ToolResult {
        executeCalls.add(tool to params)
        executeOperators.add(operator)
        return executeFn(tool, params)
    }
}
