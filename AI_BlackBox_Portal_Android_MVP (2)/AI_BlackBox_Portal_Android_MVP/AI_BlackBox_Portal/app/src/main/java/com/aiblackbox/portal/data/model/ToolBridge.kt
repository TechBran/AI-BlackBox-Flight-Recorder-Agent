package com.aiblackbox.portal.data.model

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * One tool's discoverable schema, as returned inside the `tools` array of
 * POST /local/tools/search → {"success": bool, "tools": [<this>...]}
 * (Orchestrator/routes/local_routes.py:local_tools_search).
 *
 * `parameters` is an ARBITRARY JSON Schema object that varies per tool, so it is
 * modeled as a raw [JsonObject] (NOT a fixed data class) and carried through
 * verbatim — the on-device Gemma loop hands it straight to the model as the
 * tool's parameter schema. The lenient Json config (ignoreUnknownKeys) tolerates
 * any extra per-tool fields the backend may add.
 */
@Serializable
data class ToolSchema(
    val name: String = "",
    val description: String = "",
    val parameters: JsonObject = JsonObject(emptyMap()),
)

/** POST /local/tools/search → {"success": bool, "tools": [...]}. */
@Serializable
data class ToolSearchResponse(
    val success: Boolean = false,
    val tools: List<ToolSchema> = emptyList(),
)

/**
 * POST /local/tools/execute → {"success": bool, "result": <arbitrary>}
 * (Orchestrator/routes/local_routes.py:local_tools_execute).
 *
 * `result` is whatever the executed tool emitted — a string, object, list,
 * number, or `null` — so it is modeled as a nullable [JsonElement] rather than a
 * typed field. The caller inspects/casts it as needed.
 */
@Serializable
data class ToolResult(
    val success: Boolean = false,
    val result: JsonElement? = null,
)

/**
 * Body for POST /local/tools/search: `{"query": str, "k": int}`.
 *
 * `operator` is optional/global for search (the backend treats tool discovery as
 * operator-agnostic), so it is omitted here. A blank `query` is a 400 on the
 * backend — callers must not send one.
 */
@Serializable
data class ToolSearchRequest(
    val query: String,
    val k: Int = 5,
)

/**
 * Body for POST /local/tools/execute: `{"tool": str, "params": object, "operator": str}`.
 *
 * `params` is the (possibly empty) argument object for the tool, carried as a
 * raw [JsonObject] so any tool's argument shape passes through untyped.
 */
@Serializable
data class ToolExecuteRequest(
    val tool: String,
    val params: JsonObject = JsonObject(emptyMap()),
    val operator: String,
)
