package com.aiblackbox.portal.data.local

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [ResidentTools] — the on-device tool schema source of truth. Task
 * IA-3 adds the INTENT ACTIONS ([ResidentTools.INTENT_ACTIONS] /
 * [ResidentTools.intentActions]) and the [ResidentTools.LOCAL_PHONE_TOOLS] union
 * that [FcLoop] routes locally. These are pure (no Android framework), so they run
 * on the JVM with no device.
 */
class ResidentToolsTest {

    /**
     * The comprehensive decision-9 intent-action catalog (26 names): the original 15
     * (web_search moved to CLOUD_TOOLS) + the 11 decision-9 additions.
     */
    private val expectedIntentActions = setOf(
        // original 15
        "flashlight_on", "flashlight_off", "create_contact", "send_email", "show_map",
        "open_wifi_settings", "create_calendar_event", "open_url", "dial", "send_sms",
        "set_alarm", "set_timer", "share_text", "open_settings_panel", "take_photo",
        // decision-9 additions
        "capture_video", "show_alarms", "view_calendar", "pick_contact", "view_contacts",
        "pick_file", "create_document", "navigate", "play_media", "open_settings",
        "send_intent",
    )

    @Test
    fun `INTENT_ACTIONS holds exactly the 26 comprehensive-catalog names`() {
        assertEquals("INTENT_ACTIONS must be the 26 catalog names", expectedIntentActions, ResidentTools.INTENT_ACTIONS)
        assertEquals("INTENT_ACTIONS has 26 entries", 26, ResidentTools.INTENT_ACTIONS.size)
        assertTrue("web_search is not a phone intent action", "web_search" !in ResidentTools.INTENT_ACTIONS)
        // open_app is a PHONE_ACTUATOR (gesture layer), NOT an intent action.
        assertTrue("open_app is not an intent action", "open_app" !in ResidentTools.INTENT_ACTIONS)
        // The decision-9 primitives are present.
        assertTrue("open_url present", "open_url" in ResidentTools.INTENT_ACTIONS)
        assertTrue("open_settings present", "open_settings" in ResidentTools.INTENT_ACTIONS)
        assertTrue("send_intent present", "send_intent" in ResidentTools.INTENT_ACTIONS)
    }

    @Test
    fun `intentActions schema names equal INTENT_ACTIONS with no dupes and count 26`() {
        val schemas = ResidentTools.intentActions()
        assertEquals("intentActions() returns 26 schemas", 26, schemas.size)
        val names = schemas.map { it.name }
        assertEquals("no duplicate intent-action schema names", names.size, names.toSet().size)
        assertEquals(
            "intentActions() names == INTENT_ACTIONS",
            ResidentTools.INTENT_ACTIONS,
            names.toSet(),
        )
    }

    @Test
    fun `LOCAL_PHONE_TOOLS is the union of PHONE_ACTUATORS and INTENT_ACTIONS`() {
        assertEquals(
            "LOCAL_PHONE_TOOLS == PHONE_ACTUATORS + INTENT_ACTIONS",
            ResidentTools.PHONE_ACTUATORS + ResidentTools.INTENT_ACTIONS,
            ResidentTools.LOCAL_PHONE_TOOLS,
        )
        // The gesture set and the intent set are disjoint, so the union has both fully.
        assertTrue(
            "every PHONE_ACTUATOR is in LOCAL_PHONE_TOOLS",
            ResidentTools.PHONE_ACTUATORS.all { it in ResidentTools.LOCAL_PHONE_TOOLS },
        )
        assertTrue(
            "every INTENT_ACTION is in LOCAL_PHONE_TOOLS",
            ResidentTools.INTENT_ACTIONS.all { it in ResidentTools.LOCAL_PHONE_TOOLS },
        )
    }

    @Test
    fun `INTENT_ONLY_AVAILABLE_ACTIONS is INTENT_ACTIONS plus the app-context open_app and home`() {
        // What still works with a11y OFF: the intent catalog PLUS the two dedicated Application-
        // Context actions (open_app / home) that were moved off the a11y path.
        val expected = ResidentTools.INTENT_ACTIONS + setOf("open_app", "home")
        assertEquals(expected, ResidentTools.INTENT_ONLY_AVAILABLE_ACTIONS)
        assertTrue("open_app is available without a11y", "open_app" in ResidentTools.INTENT_ONLY_AVAILABLE_ACTIONS)
        assertTrue("home is available without a11y", "home" in ResidentTools.INTENT_ONLY_AVAILABLE_ACTIONS)
        // ...but they stay OUT of INTENT_ACTIONS (they keep their own dispatch names / wire variants;
        // INTENT_ACTIONS must stay disjoint from gesture/global/coordinate names — RemoteActionChannel I1).
        assertTrue("open_app stays out of INTENT_ACTIONS", "open_app" !in ResidentTools.INTENT_ACTIONS)
        assertTrue("home stays out of INTENT_ACTIONS", "home" !in ResidentTools.INTENT_ACTIONS)
        // both remain PHONE_ACTUATORS (local routing unchanged).
        assertTrue("open_app is still a PHONE_ACTUATOR", "open_app" in ResidentTools.PHONE_ACTUATORS)
        assertTrue("home is still a PHONE_ACTUATOR", "home" in ResidentTools.PHONE_ACTUATORS)
    }

    /** The `required` array of a schema's parameters, or empty if absent. */
    private fun requiredOf(name: String): Set<String> {
        val params: JsonObject = ResidentTools.intentActions().first { it.name == name }.parameters
        val req = params["required"] as? JsonArray ?: return emptySet()
        return req.map { (it as JsonPrimitive).content }.toSet()
    }

    /** The `properties` object of a schema's parameters (empty object if absent). */
    private fun propertiesOf(name: String): JsonObject {
        val params: JsonObject = ResidentTools.intentActions().first { it.name == name }.parameters
        return params["properties"] as? JsonObject ?: JsonObject(emptyMap())
    }

    /** The declared JSON Schema `type` of a single property, or null if unset. */
    private fun typeOf(action: String, property: String): String? {
        val prop = propertiesOf(action)[property] as? JsonObject ?: return null
        return (prop["type"] as? JsonPrimitive)?.content
    }

    /** Whether the schema's parameters declare a `required` key at all. */
    private fun hasRequiredKey(name: String): Boolean {
        val params: JsonObject = ResidentTools.intentActions().first { it.name == name }.parameters
        return params["required"] != null
    }

    @Test
    fun `send_email requires to`() {
        assertTrue("send_email lists 'to' as required", "to" in requiredOf("send_email"))
    }

    @Test
    fun `set_alarm requires hour and minutes`() {
        val req = requiredOf("set_alarm")
        assertTrue("set_alarm requires hour", "hour" in req)
        assertTrue("set_alarm requires minutes", "minutes" in req)
    }

    @Test
    fun `show_map requires query`() {
        assertTrue("show_map lists 'query' as required", "query" in requiredOf("show_map"))
    }

    @Test
    fun `flashlight_on has no required params`() {
        assertTrue("flashlight_on must have an empty/absent required array", requiredOf("flashlight_on").isEmpty())
    }

    /**
     * The EXACT required-param set for every intent action that declares required
     * params. Closes code-review note M2 (prior tests spot-checked only 4 of 16).
     */
    private val expectedRequiredByAction: Map<String, Set<String>> = mapOf(
        "send_email" to setOf("to"),
        "show_map" to setOf("query"),
        "create_calendar_event" to setOf("datetime"),
        "open_url" to setOf("uri"),
        "dial" to setOf("number"),
        "send_sms" to setOf("number"),
        "set_alarm" to setOf("hour", "minutes"),
        "set_timer" to setOf("seconds"),
        "share_text" to setOf("text"),
        // decision-9 additions with required params
        "navigate" to setOf("destination"),
        "play_media" to setOf("query"),
        "open_settings" to setOf("panel"),
        "send_intent" to setOf("action"),
    )

    @Test
    fun `every required-param action declares EXACTLY its expected required set`() {
        for ((action, expected) in expectedRequiredByAction) {
            assertEquals("$action required set", expected, requiredOf(action))
        }
    }

    /** The intent actions that take no required params (optional-only or none). */
    private val noRequiredActions = setOf(
        "flashlight_on", "flashlight_off", "open_wifi_settings",
        "take_photo", "create_contact", "open_settings_panel",
        // decision-9 additions with no required params (optional-only or none)
        "capture_video", "show_alarms", "view_calendar", "pick_contact",
        "view_contacts", "pick_file", "create_document",
    )

    @Test
    fun `no-required actions have an absent or empty required array`() {
        for (action in noRequiredActions) {
            // Absent `required` key OR a present-but-empty array both satisfy.
            assertTrue(
                "$action must have no required params (absent key or empty array)",
                !hasRequiredKey(action) || requiredOf(action).isEmpty(),
            )
            assertTrue("$action required set must be empty", requiredOf(action).isEmpty())
        }
    }

    @Test
    fun `set_alarm and set_timer integer params are typed integer`() {
        assertEquals("set_alarm.hour type", "integer", typeOf("set_alarm", "hour"))
        assertEquals("set_alarm.minutes type", "integer", typeOf("set_alarm", "minutes"))
        assertEquals("set_timer.seconds type", "integer", typeOf("set_timer", "seconds"))
    }

    @Test
    fun `open_settings requires a panel param`() {
        assertTrue("open_settings requires 'panel'", "panel" in requiredOf("open_settings"))
        assertEquals("open_settings.panel is a string", "string", typeOf("open_settings", "panel"))
    }

    @Test
    fun `send_intent requires action and exposes the uri mime package extras long-tail params`() {
        assertEquals("send_intent requires exactly {action}", setOf("action"), requiredOf("send_intent"))
        val props = propertiesOf("send_intent")
        for (p in listOf("action", "uri", "mime", "package", "extras")) {
            assertTrue("send_intent must declare '$p'", props[p] is JsonObject)
        }
        assertEquals("send_intent.action is a string", "string", typeOf("send_intent", "action"))
        // extras is a free-form object of string values.
        assertEquals("send_intent.extras is an object", "object", typeOf("send_intent", "extras"))
    }

    @Test
    fun `navigate and play_media declare their required string params`() {
        assertEquals("string", typeOf("navigate", "destination"))
        assertEquals("string", typeOf("play_media", "query"))
    }

    @Test
    fun `open_settings_panel which enum is exactly the 12 expected values`() {
        val whichProp = propertiesOf("open_settings_panel")["which"] as? JsonObject
        assertTrue("open_settings_panel must declare a 'which' property", whichProp != null)
        val enumArr = whichProp!!["enum"] as? JsonArray
        assertTrue("'which' must declare an enum", enumArr != null)
        val values = enumArr!!.map { (it as JsonPrimitive).content }.toSet()
        val expected = setOf(
            "wifi", "bluetooth", "location", "sound", "display", "battery",
            "nfc", "airplane", "data", "storage", "apps", "settings",
        )
        assertEquals("'which' enum has 12 entries", 12, enumArr.size)
        assertEquals("'which' enum values", expected, values)
    }

    // ---- Task R2-B: cloud meta-tools renamed find/run_blackbox_tool ----------

    /** The `required` array of a cloudTools() schema, or empty if absent. */
    private fun cloudRequiredOf(name: String): Set<String> {
        val params: JsonObject = ResidentTools.cloudTools().first { it.name == name }.parameters
        val req = params["required"] as? JsonArray ?: return emptySet()
        return req.map { (it as JsonPrimitive).content }.toSet()
    }

    /** The description of a cloudTools() schema. */
    private fun cloudDescOf(name: String): String =
        ResidentTools.cloudTools().first { it.name == name }.description

    @Test
    fun `CLOUD_TOOLS holds find_blackbox_tool, run_blackbox_tool and web_search`() {
        assertEquals(
            "CLOUD_TOOLS == {find_blackbox_tool, run_blackbox_tool, web_search}",
            setOf("find_blackbox_tool", "run_blackbox_tool", "web_search"),
            ResidentTools.CLOUD_TOOLS,
        )
        assertEquals("FIND_BLACKBOX_TOOL constant value", "find_blackbox_tool", ResidentTools.FIND_BLACKBOX_TOOL)
        assertEquals("RUN_BLACKBOX_TOOL constant value", "run_blackbox_tool", ResidentTools.RUN_BLACKBOX_TOOL)
        assertEquals("WEB_SEARCH constant value", "web_search", ResidentTools.WEB_SEARCH)
        // web_search is a CLOUD/bridge tool, NOT a phone intent action.
        assertTrue("web_search is in the cloud bridge group", "web_search" in ResidentTools.CLOUD_TOOLS)
        assertTrue("web_search is NOT a local phone tool", "web_search" !in ResidentTools.LOCAL_PHONE_TOOLS)
    }

    @Test
    fun `cloudTools schema names equal CLOUD_TOOLS`() {
        val names = ResidentTools.cloudTools().map { it.name }.toSet()
        assertEquals("cloudTools() names == CLOUD_TOOLS", ResidentTools.CLOUD_TOOLS, names)
        assertEquals("cloudTools() returns 3 schemas", 3, ResidentTools.cloudTools().size)
    }

    @Test
    fun `find_blackbox_tool requires query and run_blackbox_tool requires name`() {
        assertEquals("find_blackbox_tool required set", setOf("query"), cloudRequiredOf("find_blackbox_tool"))
        assertEquals("run_blackbox_tool required set", setOf("name"), cloudRequiredOf("run_blackbox_tool"))
    }

    @Test
    fun `find_blackbox_tool description disambiguates from the web and points at run_blackbox_tool`() {
        val d = cloudDescOf("find_blackbox_tool")
        assertTrue("find description says NOT the web", d.contains("NOT the web"))
        assertTrue("find description points at run_blackbox_tool", d.contains("run_blackbox_tool"))
    }

    @Test
    fun `run_blackbox_tool description references find_blackbox_tool`() {
        assertTrue(
            "run description references find_blackbox_tool",
            cloudDescOf("run_blackbox_tool").contains("find_blackbox_tool"),
        )
    }

    @Test
    fun `web_search is a cloud bridge tool with a networked results-come-back description`() {
        // web_search now lives in the cloud/bridge group (HEADLESS: results come back),
        // NOT in the phone intent actions.
        assertTrue("web_search is in CLOUD_TOOLS", ResidentTools.WEB_SEARCH in ResidentTools.CLOUD_TOOLS)
        assertTrue(
            "web_search is no longer a phone intent action",
            ResidentTools.intentActions().none { it.name == "web_search" },
        )
        val d = ResidentTools.cloudTools().first { it.name == "web_search" }.description
        assertTrue("web_search says you get the results back", d.contains("get the results back"))
        assertTrue("web_search forbids using it to find own tools", d.contains("Do NOT use this"))
        assertTrue("web_search redirects to find_blackbox_tool", d.contains("find_blackbox_tool"))
        // params: query required, search_recency_filter optional.
        assertEquals("web_search requires query", setOf("query"), cloudRequiredOf("web_search"))
    }
}
