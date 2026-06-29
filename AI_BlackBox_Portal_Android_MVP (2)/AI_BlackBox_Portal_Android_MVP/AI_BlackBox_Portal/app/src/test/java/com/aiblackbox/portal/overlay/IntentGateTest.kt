package com.aiblackbox.portal.overlay

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the PURE intent confirm-gate decisions (Task IA-1).
 *
 * Mirrors [ConfirmGateTest] for the *intent* surface: which named intents are
 * high-consequence ([isHighConsequenceIntent]), when the actuator must ask the
 * user ([shouldConfirmIntent]), and the user-facing confirm string
 * ([describeIntent]).
 *
 * SAFETY framing: only the two intents that fire a PREFILLED outbound message
 * with a recipient — `send_email` / `send_sms` — gate. Everything else either
 * has a final user tap inside the launched UI (dialer, maps, calendar editor,
 * web) or is benign (flashlight/settings/search), so it must NOT gate (over-
 * gating trains the user to rubber-stamp). And the confirm string must NEVER
 * carry a message body — only the recipient/number ever reaches [describeIntent].
 */
class IntentGateTest {

    // ---- isHighConsequenceIntent -------------------------------------------

    @Test
    fun `send_email and send_sms are high-consequence`() {
        assertTrue(isHighConsequenceIntent("send_email"))
        assertTrue(isHighConsequenceIntent("send_sms"))
    }

    @Test
    fun `isHighConsequenceIntent is case-insensitive and trimmed`() {
        assertTrue(isHighConsequenceIntent("Send_Email"))
        assertTrue(isHighConsequenceIntent("  SEND_SMS  "))
    }

    @Test
    fun `benign intents are not high-consequence`() {
        val benign = listOf(
            "flashlight_on", "flashlight_off", "show_map", "open_url",
            "dial", "create_calendar_event", "open_settings",
            "set_timer", "set_alarm",
        )
        for (name in benign) {
            assertFalse("$name should NOT be high-consequence", isHighConsequenceIntent(name))
        }
    }

    // ---- shouldConfirmIntent ----------------------------------------------

    @Test
    fun `PERMISSION mode confirms a high-consequence intent`() {
        assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_email"))
        assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_sms"))
    }

    @Test
    fun `YOLO mode never confirms even a high-consequence intent`() {
        assertFalse(shouldConfirmIntent(AutonomyMode.YOLO, "send_email"))
        assertFalse(shouldConfirmIntent(AutonomyMode.YOLO, "send_sms"))
    }

    @Test
    fun `benign intents never confirm in either mode`() {
        for (mode in listOf(AutonomyMode.PERMISSION, AutonomyMode.YOLO)) {
            assertFalse(shouldConfirmIntent(mode, "show_map"))
            assertFalse(shouldConfirmIntent(mode, "open_url"))
            assertFalse(shouldConfirmIntent(mode, "flashlight_on"))
            assertFalse(shouldConfirmIntent(mode, "dial"))
        }
    }

    // ---- describeIntent ----------------------------------------------------

    @Test
    fun `describeIntent for send_email names the recipient and the word email`() {
        val desc = describeIntent("send_email", "alice@example.com")
        assertEquals("Send an email to \"alice@example.com\"", desc)
        assertTrue(desc.contains("alice@example.com"))
        assertTrue(desc.lowercase().contains("email"))
    }

    @Test
    fun `describeIntent for send_email with no recipient is still generic and safe`() {
        assertEquals("Send an email", describeIntent("send_email", null))
        assertEquals("Send an email", describeIntent("send_email", "  "))
    }

    @Test
    fun `describeIntent for send_sms names the number`() {
        val desc = describeIntent("send_sms", "+15551234567")
        assertEquals("Send a text to \"+15551234567\"", desc)
        assertTrue(desc.contains("+15551234567"))
    }

    @Test
    fun `describeIntent for send_sms with no number is still generic and safe`() {
        assertEquals("Send a text message", describeIntent("send_sms", null))
        assertEquals("Send a text message", describeIntent("send_sms", ""))
    }

    @Test
    fun `describeIntent for any other intent is a plain Run name`() {
        assertEquals("Run show_map", describeIntent("show_map", null))
        assertEquals("Run flashlight_on", describeIntent("flashlight_on", "ignored"))
    }

    @Test
    fun `describeIntent never leaks anything beyond the recipient`() {
        // SECURITY: describeIntent only ever receives the recipient/number, never a
        // body. The exact output is fully determined by (name, primaryArg) — there
        // is no path for any other text to appear. Pin the exact strings.
        assertEquals("Send an email to \"a@b.com\"", describeIntent("send_email", "a@b.com"))
        assertEquals("Send a text to \"5551234\"", describeIntent("send_sms", "5551234"))
    }
}
