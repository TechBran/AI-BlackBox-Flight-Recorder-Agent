package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * The always-resident tool set for the on-device agent loop, plus the tiering cap.
 *
 * The small on-device model must NEVER see the whole 100+ tool vault at once, so
 * the only function resident in every turn is [SEARCH_TOOLS]: a discovery hop the
 * model calls to find the capability it needs. The schemas that search returns are
 * injected as callable functions for the NEXT turn — capped at
 * [MAX_INJECTED_SCHEMAS] so the per-turn tool list stays bounded. (Phase 4 appends
 * the ~12 phone actuators to the resident set.)
 */
object ResidentTools {

    /** Name of the resident discovery function. */
    const val SEARCH_TOOLS = "search_tools"

    /**
     * Tiering cap: at most this many DISCOVERED schemas are injected per turn, so
     * the small on-device model never sees the whole vault.
     */
    const val MAX_INJECTED_SCHEMAS = 5

    /** The one function resident in every turn. Phase 4 appends the phone actuators. */
    val searchToolsSchema = ToolSchema(
        name = SEARCH_TOOLS,
        description = "Search the BlackBox tool catalog by natural-language intent and make the best-matching tools callable on the next turn. Call this FIRST when you need a capability you don't currently have (e.g. generate an image, search memory).",
        parameters = buildJsonObject {
            put("type", JsonPrimitive("object"))
            putJsonObject("properties") {
                putJsonObject("query") {
                    put("type", JsonPrimitive("string"))
                    put("description", JsonPrimitive("What capability you need, in natural language."))
                }
            }
            put("required", buildJsonArray { add(JsonPrimitive("query")) })
        },
    )

    /** The tool schemas resident in every agent turn. */
    fun resident(): List<ToolSchema> = listOf(searchToolsSchema)
}
