package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.serialization.json.JsonObject
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

    /** The tool schemas resident in every agent turn.
     *
     * Search-only by design: the phone actuators ([phoneActuators]) are NOT
     * included here. [FcLoop] appends them per-turn ONLY when a [PhoneController]
     * is wired, so a device with no accessibility service / no controller never
     * advertises phone actions to the model. */
    fun resident(): List<ToolSchema> = listOf(searchToolsSchema)

    // ---- Phase 4.5: on-device phone actuators -------------------------------

    /**
     * The names of the resident on-device phone actuators. A tool call whose name
     * is in this set is dispatched LOCALLY through the [PhoneController] (the
     * accessibility service) — never through the cloud [ToolBridge]. [FcLoop]
     * routes on this set.
     */
    val PHONE_ACTUATORS: Set<String> = setOf(
        "read_screen", "tap", "type", "swipe", "scroll", "open_app", "back", "home",
    )

    /** A `{"type":"object"}` schema with no properties (read_screen/back/home). */
    private fun noParams(): JsonObject = buildJsonObject {
        put("type", JsonPrimitive("object"))
        putJsonObject("properties") { }
    }

    /** A required cardinal-direction enum param ("up"/"down"/"left"/"right"). */
    private fun directionSchema(actionDescription: String): JsonObject = buildJsonObject {
        put("type", JsonPrimitive("object"))
        putJsonObject("properties") {
            putJsonObject("direction") {
                put("type", JsonPrimitive("string"))
                put("enum", buildJsonArray {
                    add(JsonPrimitive("up")); add(JsonPrimitive("down"))
                    add(JsonPrimitive("left")); add(JsonPrimitive("right"))
                })
                put("description", JsonPrimitive(actionDescription))
            }
        }
        put("required", buildJsonArray { add(JsonPrimitive("direction")) })
    }

    /**
     * The on-device phone-actuator schemas (names == [PHONE_ACTUATORS]). [FcLoop]
     * appends these to the per-turn tool list ONLY when a [PhoneController] is
     * wired. Descriptions are deliberately TERSE (small on-device model) and tell
     * the model the workflow: call `read_screen` FIRST to get node_ids, then
     * `tap`/`type` by node_id; `type` REFUSES password fields.
     */
    fun phoneActuators(): List<ToolSchema> = listOf(
        ToolSchema(
            name = "read_screen",
            description = "Read the phone's current screen as a JSON list of actionable nodes (each with a node_id). Call this FIRST to learn what's on screen and get node_ids for tap/type.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "tap",
            description = "Tap the screen element with the given node_id (from read_screen).",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("node_id") {
                        put("type", JsonPrimitive("integer"))
                        put("description", JsonPrimitive("Element node_id from read_screen."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("node_id")) })
            },
        ),
        ToolSchema(
            name = "type",
            description = "Type text into the editable element with the given node_id (from read_screen). REFUSES password fields — do not use for passwords.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("node_id") {
                        put("type", JsonPrimitive("integer"))
                        put("description", JsonPrimitive("Editable element node_id from read_screen."))
                    }
                    putJsonObject("text") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The text to type."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("node_id")); add(JsonPrimitive("text")) })
            },
        ),
        ToolSchema(
            name = "swipe",
            description = "Swipe the screen in a cardinal direction.",
            parameters = directionSchema("Swipe direction."),
        ),
        ToolSchema(
            name = "scroll",
            description = "Scroll the screen in a cardinal direction.",
            parameters = directionSchema("Scroll direction."),
        ),
        ToolSchema(
            name = "open_app",
            description = "Launch an installed app by its package name (e.g. com.android.settings).",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("package") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The app's package name."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("package")) })
            },
        ),
        ToolSchema(
            name = "back",
            description = "Press the system Back button.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "home",
            description = "Go to the system Home screen.",
            parameters = noParams(),
        ),
    )
}
