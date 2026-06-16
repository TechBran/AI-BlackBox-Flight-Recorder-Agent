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
}
