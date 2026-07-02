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

    // ---- Task W3 follow-up: cloud tool vault under the NATIVE loop -----------
    //
    // The NATIVE (engine-driven) path can't use the manual FcLoop's two-turn
    // search->inject->call tiering (the engine owns the loop). Instead the cloud
    // vault is exposed to the native loop as TWO native tools the ENGINE drives the
    // same way it drives phone actions: [FIND_BLACKBOX_TOOL] discovers a capability
    // by description, then [RUN_BLACKBOX_TOOL] runs the chosen one. Their execute
    // bodies (built in [ChatViewModel.streamLocalNativeAgentTurn]) call the cloud
    // [ToolBridge] ONLY -- never the [PhoneController] (the W3 separation guarantee).

    /** Native discovery tool: find a BlackBox capability by description. */
    const val FIND_BLACKBOX_TOOL = "find_blackbox_tool"

    /** Native invocation tool: run a discovered BlackBox capability by name. */
    const val RUN_BLACKBOX_TOOL = "run_blackbox_tool"

    /**
     * HEADLESS web-search tool. Unlike the find/run META-tools above, this is a
     * DIRECT cloud capability: its execute body calls the cloud [ToolBridge]'s
     * `web_search` tool (over HTTP) and the search RESULTS come back into the
     * conversation. It is HEADLESS by design — it must NOT fire an Android browser
     * intent (which backgrounds the app and gets the on-device model evicted), so it
     * lives in the cloud/bridge group, NOT in [INTENT_ACTIONS] / [LOCAL_PHONE_TOOLS].
     */
    const val WEB_SEARCH = "web_search"

    /**
     * The names of the native cloud tools whose execute bodies route to the cloud
     * [ToolBridge] (NEVER the [PhoneController]): the two discovery/invocation
     * META-tools ([FIND_BLACKBOX_TOOL] / [RUN_BLACKBOX_TOOL]) plus the DIRECT
     * headless [WEB_SEARCH] capability. All are advertised ONLY when a cloud bridge
     * is wired and are kept disjoint from [LOCAL_PHONE_TOOLS] by construction.
     */
    val CLOUD_TOOLS: Set<String> = setOf(FIND_BLACKBOX_TOOL, RUN_BLACKBOX_TOOL, WEB_SEARCH)

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
     * The names of the on-device INTENT ACTIONS. A tool call whose name is in this
     * set is dispatched LOCALLY through the [PhoneController] to the
     * [com.aiblackbox.portal.overlay.IntentActuator] — NEVER through the cloud
     * [ToolBridge]. Unlike the gesture [PHONE_ACTUATORS] (which need a read_screen →
     * tap/type loop), each of these is a DETERMINISTIC, SINGLE-SHOT stock-Android OS
     * intent (show a map, dial, draft an SMS/email, set an alarm/timer, etc.) that
     * fully satisfies the request in one call.
     *
     * COMPREHENSIVE (decision 9 / M1.5): expanded from the original 15 to the phone's
     * FULL capability via the fast intent path — the complete common-intents catalog
     * (alarms/timers, calendar view, camera still+video, contacts pick/view, file
     * pickers, maps + turn-by-turn navigation, media play-from-search, dial, SMS,
     * email, sharing) plus `open_url` (ANY http/deep-link URI), `open_settings` (ANY
     * `Settings.ACTION_*` panel), and the guarded generic `send_intent` for the long
     * tail. The legacy `open_settings_panel` is kept for back-compat. The
     * decision-4 safety gates apply: `send_email`/`send_sms`/`send_intent` are
     * high-consequence (confirm in Permission mode); credential handoff still holds.
     */
    val INTENT_ACTIONS: Set<String> = setOf(
        // --- original common-intents catalog ---
        "flashlight_on", "flashlight_off", "create_contact", "send_email", "show_map",
        "open_wifi_settings", "create_calendar_event", "open_url", "dial", "send_sms",
        "set_alarm", "set_timer", "share_text", "open_settings_panel", "take_photo",
        // --- decision-9 comprehensive additions ---
        "capture_video", "show_alarms", "view_calendar", "pick_contact", "view_contacts",
        "pick_file", "create_document", "navigate", "play_media", "open_settings",
        "send_intent",
    )

    /**
     * The FULL set of tool names [FcLoop] routes LOCALLY through the
     * [PhoneController] (never the cloud [ToolBridge]): the gesture
     * [PHONE_ACTUATORS] plus the [INTENT_ACTIONS]. [FcLoop] routes on this set.
     */
    val LOCAL_PHONE_TOOLS: Set<String> = PHONE_ACTUATORS + INTENT_ACTIONS

    /**
     * The actions that STILL WORK with accessibility OFF (intent_only_mode): every
     * [INTENT_ACTIONS] entry PLUS the two dedicated actions that now fire via the process-wide
     * Application Context (NO a11y) — `open_app`
     * ([com.aiblackbox.portal.overlay.IntentActuator.openApp]) and `home`
     * ([com.aiblackbox.portal.overlay.IntentActuator.goHome]).
     *
     * `open_app`/`home` are DELIBERATELY NOT added to [INTENT_ACTIONS]: they keep their dedicated
     * dispatch names / wire variants (`open_app` is its own `open_app` action; `home` is a
     * `global_action`) and are [PHONE_ACTUATORS], and [INTENT_ACTIONS] must stay DISJOINT from every
     * gesture/global/coordinate dispatch name so an `intent` frame can't smuggle one (see
     * RemoteActionChannel I1). This set is used ONLY to describe what remains available when a11y is
     * off — [com.aiblackbox.portal.overlay.AndroidPhoneController.INTENT_ONLY_MODE_DETAIL].
     *
     * ROUTING PRINCIPLE (intent-layer first): anything that CAN route through the Application
     * Context (no a11y) does; accessibility is reserved for what genuinely needs it (screen
     * inspection, fine-grained UI manipulation, and `back`/`recents` which have no intent path).
     */
    val INTENT_ONLY_AVAILABLE_ACTIONS: Set<String> = INTENT_ACTIONS + setOf("open_app", "home")

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
            description = "Open a URL or app deep link (http/https or any app scheme like geo:, tel:, spotify:). One primitive for all links. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("uri") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The URL or deep-link URI to open (http/https or an app scheme)."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("uri")) })
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

        // ---- Decision-9 comprehensive additions -----------------------------

        ToolSchema(
            name = "capture_video",
            description = "Open the camera in video mode. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "show_alarms",
            description = "Open the clock app's list of alarms. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "view_calendar",
            description = "Open the calendar, optionally at a given time. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("datetime") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Optional time to open at, format YYYY-MM-DDTHH:MM:SS."))
                    }
                }
            },
        ),
        ToolSchema(
            name = "pick_contact",
            description = "Open the contact picker for the user to choose a contact. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "view_contacts",
            description = "Open the contacts list. Direct action — no read_screen/tap.",
            parameters = noParams(),
        ),
        ToolSchema(
            name = "pick_file",
            description = "Open the system file picker for the user to choose a file. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("mime") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Optional MIME type filter (e.g. image/*, application/pdf). Defaults to */*."))
                    }
                }
            },
        ),
        ToolSchema(
            name = "create_document",
            description = "Open the system 'save new file' picker. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("mime") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Optional MIME type for the new file (e.g. text/plain). Defaults to application/octet-stream."))
                    }
                    putJsonObject("filename") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Optional suggested file name."))
                    }
                }
            },
        ),
        ToolSchema(
            name = "navigate",
            description = "Start turn-by-turn navigation to a place/address. destination=where to. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("destination") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Place or address to navigate to."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("destination")) })
            },
        ),
        ToolSchema(
            name = "play_media",
            description = "Play music/media matching a search query. query=what to play. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("query") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("What to play (artist, song, album, genre, or 'any')."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("query")) })
            },
        ),
        ToolSchema(
            name = "open_settings",
            description = "Open ANY system settings panel by key (e.g. wifi, bluetooth, location, display, sound, airplane, data_usage, apps, security, accessibility, battery, storage, date, language, nfc, hotspot, wireless). panel=which one. Unknown keys return the valid list. Direct action — no read_screen/tap.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("panel") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Which settings panel to open (e.g. wifi, bluetooth, location, battery, accessibility)."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("panel")) })
            },
        ),
        ToolSchema(
            name = "send_intent",
            description = "Advanced escape-hatch: fire a custom Android intent for something no other tool covers. action=the intent action (e.g. android.intent.action.VIEW). Optional uri, mime, package, extras. Dangerous actions (silent call/install/delete/wipe) are rejected; high-consequence use is confirmed. Prefer a specific tool when one exists.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("action") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The Android intent action string (e.g. android.intent.action.VIEW)."))
                    }
                    putJsonObject("uri") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Optional data URI. file:/content:/intent:/javascript:/data: schemes are rejected."))
                    }
                    putJsonObject("mime") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Optional MIME type for the intent's data."))
                    }
                    putJsonObject("package") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("Optional target package name to restrict which app handles it."))
                    }
                    putJsonObject("extras") {
                        put("type", JsonPrimitive("object"))
                        put("description", JsonPrimitive("Optional string extras as a flat {key: value} object (values are sent as strings)."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("action")) })
            },
        ),
    )

    // ---- Task W3 follow-up: native cloud-vault tool schemas -----------------

    /**
     * The HEADLESS web-search schema (name == [WEB_SEARCH]). Advertised in the
     * cloud/bridge group ONLY when a cloud bridge is wired: on the native engine
     * path via [cloudTools] (-> ChatViewModel.buildCloudNativeTools), and in the
     * manual [FcLoop] loop's advertised set. Its dispatch routes to
     * [ToolBridge.execute] ("web_search") so the RESULTS come back into the turn --
     * NEVER an Android browser intent. Params: `query` (required) +
     * `search_recency_filter` (optional).
     */
    val webSearchSchema = ToolSchema(
        name = WEB_SEARCH,
        description = "Search the web and read the results to answer the user \u2014 you get the results back. Do NOT use this to find your own tools/capabilities \u2014 use find_blackbox_tool for that.",
        parameters = buildJsonObject {
            put("type", JsonPrimitive("object"))
            putJsonObject("properties") {
                putJsonObject("query") {
                    put("type", JsonPrimitive("string"))
                    put("description", JsonPrimitive("What to search the web for."))
                }
                putJsonObject("search_recency_filter") {
                    put("type", JsonPrimitive("string"))
                    put("description", JsonPrimitive("Optional recency window for results (e.g. day, week, month, year)."))
                }
            }
            put("required", buildJsonArray { add(JsonPrimitive("query")) })
        },
    )

    /**
     * The native cloud tool schemas offered to the native engine loop when a cloud
     * bridge is wired: the two discovery/invocation META-tools
     * ([FIND_BLACKBOX_TOOL] / [RUN_BLACKBOX_TOOL]) plus the DIRECT headless
     * [webSearchSchema]. [ChatViewModel.streamLocalNativeAgentTurn] turns each into
     * a [NativeTool] whose execute calls the cloud [ToolBridge], and offers them to
     * the native engine loop ALONGSIDE the phone/intent tools.
     *
     * Descriptions are deliberately TERSE and model-steering (small on-device
     * model): search FIRST to find a capability, then call it by the chosen name;
     * web_search reads the web and returns the results.
     */
    fun cloudTools(): List<ToolSchema> = listOf(
        ToolSchema(
            name = FIND_BLACKBOX_TOOL,
            description = "Find a BlackBox capability by description (e.g. roll dice, generate an image, search memory). Returns matching tool names to use with run_blackbox_tool. This searches YOUR OWN tools \u2014 NOT the web.",
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
        ),
        ToolSchema(
            name = RUN_BLACKBOX_TOOL,
            description = "Run a BlackBox tool you found with find_blackbox_tool. name = the tool's name; args = its arguments.",
            parameters = buildJsonObject {
                put("type", JsonPrimitive("object"))
                putJsonObject("properties") {
                    putJsonObject("name") {
                        put("type", JsonPrimitive("string"))
                        put("description", JsonPrimitive("The tool name returned by find_blackbox_tool."))
                    }
                    putJsonObject("args") {
                        put("type", JsonPrimitive("object"))
                        put("description", JsonPrimitive("The chosen tool's arguments, as a JSON object."))
                    }
                }
                put("required", buildJsonArray { add(JsonPrimitive("name")) })
            },
        ),
        webSearchSchema,
    )
}
