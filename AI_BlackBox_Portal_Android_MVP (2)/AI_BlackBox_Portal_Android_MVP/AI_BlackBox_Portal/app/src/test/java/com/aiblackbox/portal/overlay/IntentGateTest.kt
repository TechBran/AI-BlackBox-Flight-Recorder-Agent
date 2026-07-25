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
    fun `send_intent is high-consequence by default (decision 9)`() {
        // The guarded generic escape-hatch must go through the confirm-gate.
        assertTrue(isHighConsequenceIntent("send_intent"))
        assertTrue(isHighConsequenceIntent("  Send_Intent  "))
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

    // ---- the ORIGIN axis (navigate) ----------------------------------------
    //
    // `navigate` is benign when the owner asks for it ON the phone and consequential
    // when pushed in from the cloud (it seizes the foreground into turn-by-turn,
    // possibly mid-drive). So it is gated by ORIGIN, never wholesale. Full
    // both-directions coverage lives in NavigationIntentTest; these pin the
    // interaction with the EXISTING assertions in this file.

    @Test
    fun `navigate is never high-consequence wholesale`() {
        assertFalse(isHighConsequenceIntent("navigate"))
        assertFalse(isRemoteGatedIntent("navigate"))   // reverted — see `navigate confirms from NEITHER origin`
    }

    @Test
    fun `origin defaults to LOCAL so the pre-existing 2-arg gate is unchanged`() {
        // Every call-site that predates the origin axis keeps its exact behaviour.
        assertFalse(shouldConfirmIntent(AutonomyMode.PERMISSION, "navigate"))
        assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_email"))
        assertFalse(shouldConfirmIntent(AutonomyMode.YOLO, "send_email"))
    }

    @Test
    fun `navigate confirms from NEITHER origin`() {
        // Reverted the day it shipped. `navigate` WAS remote-gated; on a real
        // device every remote navigate came back `declined`, because the confirm
        // is a SYSTEM overlay needing SYSTEM_ALERT_WINDOW and the gate fail-safes
        // to DENY when it cannot be shown. The intent path itself needs neither
        // the overlay nor the a11y service — gating it re-introduced a dependency
        // on both. The unattended case (cron/scheduled) is M3's notification,
        // which is the consent step AND the only thing Android allows from the
        // background. See the rationale block on REMOTE_GATED_INTENTS.
        assertFalse(shouldConfirmIntent(AutonomyMode.PERMISSION, "navigate", ActionOrigin.REMOTE))
        assertFalse(shouldConfirmIntent(AutonomyMode.PERMISSION, "navigate", ActionOrigin.LOCAL))
        assertFalse(isRemoteGatedIntent("navigate"))
    }

    @Test
    fun `the remote-origin gate MECHANISM still works for a future member`() {
        // The membership is empty, not the mechanism — M3 wants this seam. Proven
        // through the pure predicate so emptying the set cannot silently rot the
        // origin plumbing that PhoneActionDispatcher threads through.
        assertFalse(shouldConfirmIntent(AutonomyMode.PERMISSION, "navigate", ActionOrigin.REMOTE))
        assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_sms", ActionOrigin.REMOTE))
        assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_sms", ActionOrigin.LOCAL))
        assertFalse(shouldConfirmIntent(AutonomyMode.YOLO, "send_sms", ActionOrigin.REMOTE))
    }

    @Test
    fun `no other intent becomes gated just because it is REMOTE`() {
        for (name in listOf("show_map", "open_url", "dial", "flashlight_on", "set_alarm")) {
            assertFalse(isRemoteGatedIntent(name))
            assertFalse(shouldConfirmIntent(AutonomyMode.PERMISSION, name, ActionOrigin.REMOTE))
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
    fun `send_intent gates in PERMISSION mode but not YOLO`() {
        assertTrue(shouldConfirmIntent(AutonomyMode.PERMISSION, "send_intent"))
        assertFalse(shouldConfirmIntent(AutonomyMode.YOLO, "send_intent"))
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
    fun `describeIntent for send_intent names the (non-sensitive) action`() {
        assertEquals(
            "Run the app action \"android.intent.action.VIEW\"",
            describeIntent("send_intent", "android.intent.action.VIEW"),
        )
        assertEquals("Run a custom app action", describeIntent("send_intent", null))
        assertEquals("Run a custom app action", describeIntent("send_intent", "  "))
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
