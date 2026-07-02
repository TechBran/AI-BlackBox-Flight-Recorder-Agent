package com.aiblackbox.portal.overlay

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (C1) Proves the FAIL-SAFE autonomy PRIMITIVES — [AndroidPhoneController.M1_REMOTE_AUTONOMY_MODE]
 * (PERMISSION) + [AndroidPhoneController.M1_REMOTE_CONFIRM] ([FailSafeDenyConfirmUi]) — DENY
 * high-consequence actions while leaving benign navigation/typing/open_app/scroll and
 * non-high-consequence intents working.
 *
 * As of **M4** the live `/action` wiring no longer uses these constants — it wires the real
 * [com.aiblackbox.portal.overlay.OverlayConfirmUi] + per-device
 * [com.aiblackbox.portal.data.local.AutonomyStore] (see `RemoteFrontierAutonomyWiringTest`). These
 * remain the named fail-safe primitives (the correct SAFE fallback for any surface with no real
 * confirm UI), and this test guards that they still behave as the SAFE deny-by-default posture.
 * The actuator's gate is `if (shouldConfirm*(mode, …)) { if (!confirm.confirm(…)) DENY }`; this
 * test composes the real gate decisions with the primitive to prove the net outcome.
 */
class RemotePhoneControllerAutonomyTest {

    private val mode = AndroidPhoneController.M1_REMOTE_AUTONOMY_MODE
    private val confirm = AndroidPhoneController.M1_REMOTE_CONFIRM

    /** The actuator's gate outcome for a TAP under the M1 remote wiring: true == would fire. */
    private fun tapWouldFire(label: String?): Boolean = runBlocking {
        val high = isHighConsequence("tap", label, isPasswordTarget = false)
        if (!shouldConfirm(mode(), high)) true else confirm.confirm(describeAction("tap", label))
    }

    /** The actuator's gate outcome for an INTENT under the M1 remote wiring: true == would fire. */
    private fun intentWouldFire(name: String, primaryArg: String?): Boolean = runBlocking {
        if (!shouldConfirmIntent(mode(), name)) true else confirm.confirm(describeIntent(name, primaryArg))
    }

    // ---- the posture is the SAFE one (not the un-wired YOLO/auto-approve default) ----

    @Test fun `the remote posture is PERMISSION not YOLO`() {
        assertEquals(AutonomyMode.PERMISSION, mode())
    }

    @Test fun `the remote confirm is fail-safe deny`() = runBlocking {
        assertFalse(confirm.confirm("Send an email to \"x@y.com\""))
        assertFalse(confirm.confirm("anything at all"))
        assertTrue(confirm === FailSafeDenyConfirmUi)
    }

    // ---- high-consequence actions are DENIED (the C1 fix) ----

    @Test fun `high-consequence taps are denied under the M1 wiring`() {
        for (label in listOf("Send", "Pay \$42.00", "Confirm purchase", "Delete account",
                "Submit", "Install", "Log in", "Place order")) {
            assertFalse("tap \"$label\" must be DENIED", tapWouldFire(label))
        }
    }

    @Test fun `high-consequence intents are denied under the M1 wiring`() {
        assertFalse(intentWouldFire("send_email", "alice@example.com"))
        assertFalse(intentWouldFire("send_sms", "+15551234567"))
        assertFalse(intentWouldFire("send_intent", "android.intent.action.VIEW"))
    }

    @Test fun `a type into a password target is gated (and denied) under the M1 wiring`() = runBlocking {
        // The credential handoff diverts a password type first (separately fail-safe), but the
        // gate alone would already DENY it under this posture.
        val high = isHighConsequence("type", targetLabel = null, isPasswordTarget = true)
        assertTrue(shouldConfirm(mode(), high))
        assertFalse(confirm.confirm(describeAction("type", null)))
    }

    // ---- benign actions still WORK (so the M2 MVP can drive it) ----

    @Test fun `benign taps still fire under the M1 wiring`() {
        for (label in listOf("Back", "Settings", "Home", "Next", "John Smith", null)) {
            assertTrue("tap \"$label\" must FIRE", tapWouldFire(label))
        }
    }

    @Test fun `benign and user-finalized intents still fire under the M1 wiring`() {
        for (name in listOf("show_map", "open_url", "dial", "navigate", "flashlight_on",
                "open_settings", "set_timer", "take_photo")) {
            assertTrue("$name must FIRE", intentWouldFire(name, null))
        }
    }
}
