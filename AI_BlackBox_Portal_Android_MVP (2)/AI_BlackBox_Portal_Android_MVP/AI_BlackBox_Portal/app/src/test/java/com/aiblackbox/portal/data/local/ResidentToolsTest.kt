package com.aiblackbox.portal.data.local

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [ResidentTools] — the on-device tool schema source of truth. Task
 * IA-3 adds the 16 INTENT ACTIONS ([ResidentTools.INTENT_ACTIONS] /
 * [ResidentTools.intentActions]) and the [ResidentTools.LOCAL_PHONE_TOOLS] union
 * that [FcLoop] routes locally. These are pure (no Android framework), so they run
 * on the JVM with no device.
 */
class ResidentToolsTest {

    /** The 16 required-by-spec intent-action names. */
    private val expectedIntentActions = setOf(
        "flashlight_on", "flashlight_off", "create_contact", "send_email", "show_map",
        "open_wifi_settings", "create_calendar_event", "open_url", "dial", "send_sms",
        "set_alarm", "set_timer", "share_text", "open_settings_panel", "take_photo",
        "web_search",
    )

    @Test
    fun `INTENT_ACTIONS holds exactly the 16 spec names`() {
        assertEquals("INTENT_ACTIONS must be the 16 spec names", expectedIntentActions, ResidentTools.INTENT_ACTIONS)
        assertEquals("INTENT_ACTIONS has 16 entries", 16, ResidentTools.INTENT_ACTIONS.size)
    }

    @Test
    fun `intentActions schema names equal INTENT_ACTIONS with no dupes and count 16`() {
        val schemas = ResidentTools.intentActions()
        assertEquals("intentActions() returns 16 schemas", 16, schemas.size)
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
        "open_url" to setOf("url"),
        "dial" to setOf("number"),
        "send_sms" to setOf("number"),
        "set_alarm" to setOf("hour", "minutes"),
        "set_timer" to setOf("seconds"),
        "share_text" to setOf("text"),
        "web_search" to setOf("query"),
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
}
