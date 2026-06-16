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

    /**
     * The names of the on-device INTENT ACTIONS (Task IA-3). A tool call whose name
     * is in this set is dispatched LOCALLY through the [PhoneController] to the
     * [com.aiblackbox.portal.overlay.IntentActuator] — NEVER through the cloud
     * [ToolBridge]. Unlike the gesture [PHONE_ACTUATORS] (which need a read_screen →
     * tap/type loop), each of these is a DETERMINISTIC, SINGLE-SHOT stock-Android OS
     * intent (show a map, dial, draft an SMS/email, set an alarm/timer, etc.) that
     * fully satisfies the request in one call.
     */
    val INTENT_ACTIONS: Set<String> = setOf(
        "flashlight_on", "flashlight_off", "create_contact", "send_email", "show_map",
        "open_wifi_settings", "create_calendar_event", "open_url", "dial", "send_sms",
        "set_alarm", "set_timer", "share_text", "open_settings_panel", "take_photo",
        "web_search",
    )

    /**
     * The FULL set of tool names [FcLoop] routes LOCALLY through the
     * [PhoneController] (never the cloud [ToolBridge]): the gesture
     * [PHONE_ACTUATORS] plus the [INTENT_ACTIONS]. [FcLoop] routes on this set.
     */
    val LOCAL_PHONE_TOOLS: Set<String> = PHONE_ACTUATORS + INTENT_ACTIONS

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
     * the model the workflow: call `read_screen` FIRST, then `tap`/`type` —
     * PREFERRING each node's stable `resource_id` over the positional `node_id`;
     * `type` REFUSES password fields.
     */
    fun phoneActuators(): List<ToolSchema> = listOf(
        ToolSchema(
            name = "read_screen",
            description = "Read the phone's current screen as a JSON list of actionable nodes (each with a node_id and a resource_id). Call this FIRST to learn what's on screen and get handles for tap/type.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "tap",
            description = "Tap a screen element. Prefer resource_id (a stable handle from read_screen) when the node has one; fall back to node_id only if resource_id is empty. read_screen returns both for each node.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("resource_id") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Stable element resource_id from read_screen (preferred)."))
                    }
                    putJsonObject("node_id") {
                        put("type", JsonPrimitive("integer"))
                        put("description", JsonPrimitive("Element node_id from read_screen (use only if resource_id is empty)."))
                    }
                }
            },
        ),
        ToolSchema(
            name = "type",
            description = "Type text into an editable element. Prefer resource_id (a stable handle from read_screen) when the node has one; fall back to node_id only if resource_id is empty. read_screen returns both. REFUSES password fields — do not use for passwords.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("resource_id") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Stable editable element resource_id from read_screen (preferred)."))
                    }
                    putJsonObject("node_id") {
                        put("type", JsonPrimitive("integer"))
                        put("description", JsonPrimitive("Editable element node_id from read_screen (use only if resource_id is empty)."))
                    }
                    putJsonObject("text") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The text to type."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("text")) })
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

    // ---- Task IA-3: on-device intent actions --------------------------------

    /**
     * The on-device INTENT-ACTION schemas (names == [INTENT_ACTIONS]). [FcLoop]
     * appends these to the per-turn tool list ONLY when a [PhoneController] is
     * wired, and routes any matching call LOCALLY through the controller to the
     * [com.aiblackbox.portal.overlay.IntentActuator].
     *
     * Descriptions are deliberately TERSE (small on-device model) and
     * RELIABILITY-STEERING: each tells the model this is a ONE-SHOT direct action —
     * NO `read_screen`/`tap` loop is needed — so the small model doesn't fall back
     * to the slower, error-prone gesture path for something a single intent does.
     */
    fun intentActions(): List<ToolSchema> = listOf(
        ToolSchema(
            name = "flashlight_on",
            description = "Turn the flashlight on. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "flashlight_off",
            description = "Turn the flashlight off. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "create_contact",
            description = "Open a new-contact editor prefilled with these fields. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("first_name") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Contact's first name."))
                    }
                    putJsonObject("last_name") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Contact's last name."))
                    }
                    putJsonObject("phone_number") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Contact's phone number."))
                    }
                    putJsonObject("email") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Contact's email address."))
                    }
                }
            },
        ),
        ToolSchema(
            name = "send_email",
            description = "Open the email composer prefilled. to=recipient. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("to") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Recipient email address."))
                    }
                    putJsonObject("subject") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Email subject."))
                    }
                    putJsonObject("body") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Email body."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("to")) })
            },
        ),
        ToolSchema(
            name = "show_map",
            description = "Show a place/address in Maps. query=what to find. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("query") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Place or address to show."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("query")) })
            },
        ),
        ToolSchema(
            name = "open_wifi_settings",
            description = "Open the Wi-Fi settings screen. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "create_calendar_event",
            description = "Open a new calendar event prefilled. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("datetime") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Start time, format YYYY-MM-DDTHH:MM:SS."))
                    }
                    putJsonObject("title") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Event title."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("datetime")) })
            },
        ),
        ToolSchema(
            name = "open_url",
            description = "Open a web URL (http/https). Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("url") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The http/https URL to open."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("url")) })
            },
        ),
        ToolSchema(
            name = "dial",
            description = "Open the dialer prefilled with a number (does not call). Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("number") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Phone number to place in the dialer."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("number")) })
            },
        ),
        ToolSchema(
            name = "send_sms",
            description = "Open the messaging app prefilled. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("number") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Recipient phone number."))
                    }
                    putJsonObject("body") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Message body."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("number")) })
            },
        ),
        ToolSchema(
            name = "set_alarm",
            description = "Set an alarm. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("hour") {
                        put("type", JsonPrimitive("integer"))
                        put("description", JsonPrimitive("Hour, 0-23."))
                    }
                    putJsonObject("minutes") {
                        put("type", JsonPrimitive("integer"))
                        put("description", JsonPrimitive("Minutes, 0-59."))
                    }
                    putJsonObject("label") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Alarm label."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("hour")); add(JsonPrimitive("minutes")) })
            },
        ),
        ToolSchema(
            name = "set_timer",
            description = "Start a countdown timer. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("seconds") {
                        put("type", JsonPrimitive("integer"))
                        put("description", JsonPrimitive("Countdown length in seconds."))
                    }
                    putJsonObject("label") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Timer label."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("seconds")) })
            },
        ),
        ToolSchema(
            name = "share_text",
            description = "Open the share sheet with this text. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("text") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The text to share."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("text")) })
            },
        ),
        ToolSchema(
            name = "open_settings_panel",
            description = "Open a settings screen. which=which one. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("which") {
                        put("type", JsonPrimitive("string"))
                        put("enum", buildJsonArray {
                            add(JsonPrimitive("wifi")); add(JsonPrimitive("bluetooth"))
                            add(JsonPrimitive("location")); add(JsonPrimitive("sound"))
                            add(JsonPrimitive("display")); add(JsonPrimitive("battery"))
                            add(JsonPrimitive("nfc")); add(JsonPrimitive("airplane"))
                            add(JsonPrimitive("data")); add(JsonPrimitive("storage"))
                            add(JsonPrimitive("apps")); add(JsonPrimitive("settings"))
                        })
                        put("description", JsonPrimitive("Which settings screen to open."))
                    }
                }
            },
        ),
        ToolSchema(
            name = "take_photo",
            description = "Open the camera. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "web_search",
            description = "Run a web search. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("query") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Search query."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("query")) })
            },
        ),
    )
}
