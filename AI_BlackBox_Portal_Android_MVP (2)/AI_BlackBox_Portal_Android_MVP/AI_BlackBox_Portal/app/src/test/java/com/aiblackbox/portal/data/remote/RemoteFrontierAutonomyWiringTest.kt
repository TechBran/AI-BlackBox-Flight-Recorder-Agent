package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.data.local.AutonomyStore
import com.aiblackbox.portal.overlay.AutonomyMode
import com.aiblackbox.portal.overlay.ConfirmUi
import com.aiblackbox.portal.overlay.CredentialAction
import com.aiblackbox.portal.overlay.CredentialHandoff
import com.aiblackbox.portal.overlay.CREDENTIAL_FIELD_DESCRIPTION
import com.aiblackbox.portal.overlay.FailSafeDenyConfirmUi
import com.aiblackbox.portal.overlay.credentialDecision
import com.aiblackbox.portal.overlay.describeAction
import com.aiblackbox.portal.overlay.describeIntent
import com.aiblackbox.portal.overlay.isHighConsequence
import com.aiblackbox.portal.overlay.shouldConfirm
import com.aiblackbox.portal.overlay.shouldConfirmIntent
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M4) The SAFETY-DECISION contract the frontier remote paths are now wired with — the same
 * seams both [com.aiblackbox.portal.NotificationListenerFgs] (frontier `/action`) and
 * [RemoteTaskRunner] (Gemma) construct their [com.aiblackbox.portal.overlay.AndroidPhoneController]
 * with as of M4: a per-device [AutonomyStore] mode reader (default PERMISSION), the real
 * [com.aiblackbox.portal.overlay.OverlayConfirmUi] (high-consequence → on-device Allow/Deny,
 * fail-safe DENY), and the real [com.aiblackbox.portal.overlay.OverlayCredentialHandoff].
 *
 * The overlays themselves are framework/device code (WindowManager + real views), verified on
 * device (the actual on-screen confirm dialog). What is JVM-unit-testable — and asserted here —
 * is the DECISION logic those overlays serve: WHEN the confirm/handoff seam is consulted, that
 * allow→fires / deny→refused, that YOLO bypasses, that a fail-safe (deny) confirm refuses a
 * high-consequence action, that a password field routes to the handoff with the model's text
 * discarded and never leaked, and that the autonomy store round-trips + defaults SAFE.
 *
 * Mirrors the composition the actuator runs: `if (!shouldConfirm(mode, high)) fire
 * else fire = confirm.confirm(describeAction(...))` (see [com.aiblackbox.portal.overlay.Actuators]).
 */
class RemoteFrontierAutonomyWiringTest {

    /** Records whether the confirm seam was consulted + what it was shown; returns [answer]. */
    private class RecordingConfirmUi(private val answer: Boolean) : ConfirmUi {
        var calls = 0
        var lastDescription: String? = null
        override suspend fun confirm(description: String): Boolean {
            calls++
            lastDescription = description
            return answer
        }
    }

    /** Records whether the credential handoff was invoked + what it was shown; returns [answer]. */
    private class RecordingHandoff(private val answer: Boolean) : CredentialHandoff {
        var calls = 0
        var lastDescription: String? = null
        override suspend fun requestUserEntry(fieldDescription: String): Boolean {
            calls++
            lastDescription = fieldDescription
            return answer
        }
    }

    /** In-memory [AutonomyStore] so the reader wiring is JVM-testable without Android prefs. */
    private class FakeAutonomyStore(private var mode: AutonomyMode) : AutonomyStore {
        override fun load(): AutonomyMode = mode
        override fun save(mode: AutonomyMode) { this.mode = mode }
    }

    /** The exact TAP gate the actuator runs, composed with a real mode reader + confirm seam. */
    private fun tapWouldFire(mode: () -> AutonomyMode, confirm: ConfirmUi, label: String?): Boolean =
        runBlocking {
            val high = isHighConsequence("tap", label, isPasswordTarget = false)
            if (!shouldConfirm(mode(), high)) true else confirm.confirm(describeAction("tap", label))
        }

    /** The exact INTENT gate the actuator runs, composed with a real mode reader + confirm seam. */
    private fun intentWouldFire(mode: () -> AutonomyMode, confirm: ConfirmUi, name: String, arg: String?): Boolean =
        runBlocking {
            if (!shouldConfirmIntent(mode(), name)) true else confirm.confirm(describeIntent(name, arg))
        }

    // ── PERMISSION + high-consequence → confirm seam consulted (allow fires, deny refuses) ──

    @Test fun permission_high_consequence_consults_confirm_and_allow_fires() {
        val store = FakeAutonomyStore(AutonomyMode.PERMISSION)
        val confirm = RecordingConfirmUi(answer = true)
        val fired = tapWouldFire({ store.load() }, confirm, "Send")
        assertTrue("allow must let the action fire", fired)
        assertEquals("the confirm seam MUST be consulted in PERMISSION for a high-consequence tap", 1, confirm.calls)
        assertEquals("Tap \"Send\"", confirm.lastDescription)
    }

    @Test fun permission_high_consequence_consults_confirm_and_deny_refuses() {
        val store = FakeAutonomyStore(AutonomyMode.PERMISSION)
        val confirm = RecordingConfirmUi(answer = false)
        val fired = tapWouldFire({ store.load() }, confirm, "Delete account")
        assertFalse("deny must refuse the action", fired)
        assertEquals(1, confirm.calls)
    }

    @Test fun permission_high_consequence_intents_consult_the_confirm_seam() {
        val store = FakeAutonomyStore(AutonomyMode.PERMISSION)
        val allow = RecordingConfirmUi(answer = true)
        assertTrue(intentWouldFire({ store.load() }, allow, "send_sms", "+15551234567"))
        assertEquals(1, allow.calls)

        val deny = RecordingConfirmUi(answer = false)
        assertFalse(intentWouldFire({ store.load() }, deny, "send_email", "a@b.com"))
        assertEquals(1, deny.calls)
    }

    // ── YOLO → the confirm seam is NEVER consulted ──

    @Test fun yolo_never_consults_the_confirm_seam_for_high_consequence() {
        val store = FakeAutonomyStore(AutonomyMode.YOLO)
        val confirm = RecordingConfirmUi(answer = false) // would DENY if ever consulted
        val fired = tapWouldFire({ store.load() }, confirm, "Pay \$42.00")
        assertTrue("YOLO high-consequence must fire unattended", fired)
        assertEquals("the confirm seam must NOT be consulted in YOLO", 0, confirm.calls)
    }

    @Test fun yolo_never_consults_confirm_for_high_consequence_intents() {
        val store = FakeAutonomyStore(AutonomyMode.YOLO)
        val confirm = RecordingConfirmUi(answer = false)
        assertTrue(intentWouldFire({ store.load() }, confirm, "send_intent", "android.intent.action.VIEW"))
        assertEquals(0, confirm.calls)
    }

    // ── benign actions never gate (either mode) ──

    @Test fun benign_actions_never_consult_confirm_in_either_mode() {
        for (mode in listOf(AutonomyMode.PERMISSION, AutonomyMode.YOLO)) {
            val store = FakeAutonomyStore(mode)
            for (label in listOf("Back", "Settings", "Home", "John Smith", null)) {
                val confirm = RecordingConfirmUi(answer = false)
                assertTrue("benign tap must fire in $mode", tapWouldFire({ store.load() }, confirm, label))
                assertEquals("benign tap must never consult confirm ($mode, $label)", 0, confirm.calls)
            }
        }
    }

    // ── fail-safe: a DENY confirm (overlay-missing / timeout stand-in) refuses ──

    @Test fun fail_safe_deny_confirm_refuses_high_consequence_in_permission() {
        // OverlayConfirmUi returns false when the overlay permission is missing OR the prompt
        // times out (device-verified). FailSafeDenyConfirmUi is that exact deny-by-default
        // primitive; here it stands in for the fail-safe path — the DECISION must be REFUSE.
        val store = FakeAutonomyStore(AutonomyMode.PERMISSION)
        for (label in listOf("Send", "Pay \$42.00", "Confirm purchase", "Delete account", "Submit")) {
            assertFalse("fail-safe DENY must refuse \"$label\"", tapWouldFire({ store.load() }, FailSafeDenyConfirmUi, label))
        }
        // And it is genuinely deny-by-default with no UI.
        assertFalse(runBlocking { FailSafeDenyConfirmUi.confirm("anything") })
    }

    // ── credential handoff: password field → handoff invoked, model text discarded + never leaked ──

    @Test fun password_field_routes_to_handoff_and_the_model_text_is_never_leaked() = runBlocking {
        val handoff = RecordingHandoff(answer = true)
        val secret = "hunter2"
        val typedSink = mutableListOf<String>()

        // Mirror Actuators.type's password branch: DISCARD the model's text, call the handoff
        // with ONLY the generic field description.
        val decision = credentialDecision(isPasswordTarget = true, hasSavedCredential = false)
        assertEquals(CredentialAction.USER_HANDOFF, decision)
        val entered = when (decision) {
            CredentialAction.USER_HANDOFF, CredentialAction.SYSTEM_AUTOFILL ->
                handoff.requestUserEntry(CREDENTIAL_FIELD_DESCRIPTION) // `secret` is NEVER read here
            CredentialAction.TYPE_NORMAL -> { typedSink += secret; true }
        }

        assertTrue(entered)
        assertEquals("the handoff MUST be invoked for a password field", 1, handoff.calls)
        assertTrue("the model's text must be DISCARDED (never typed)", typedSink.isEmpty())
        assertEquals("the handoff prompt is the generic field description", CREDENTIAL_FIELD_DESCRIPTION, handoff.lastDescription)
        assertFalse("the secret must NEVER reach the handoff prompt", handoff.lastDescription!!.contains(secret))
    }

    @Test fun password_handoff_is_mode_independent_yolo_still_hands_off() = runBlocking {
        // The credential handoff is a HARD safety floor, not gated by autonomy — even in YOLO a
        // password field diverts to the user (the model never types a secret).
        val handoff = RecordingHandoff(answer = false)
        val decision = credentialDecision(isPasswordTarget = true, hasSavedCredential = false)
        assertEquals(CredentialAction.USER_HANDOFF, decision)
        val entered = handoff.requestUserEntry(CREDENTIAL_FIELD_DESCRIPTION)
        assertFalse("declined handoff → not entered", entered)
        assertEquals(1, handoff.calls)
    }

    @Test fun a_non_password_type_types_the_text_and_never_touches_the_handoff() = runBlocking {
        // Actually EXERCISE the non-password branch of Actuators.type's `when`: a TYPE_NORMAL
        // decision must TYPE the model's text (into the field), NOT divert to the handoff.
        val handoff = RecordingHandoff(answer = true) // would "succeed" IF ever consulted
        val modelText = "flight BA249"
        val typedSink = mutableListOf<String>()

        val decision = credentialDecision(isPasswordTarget = false, hasSavedCredential = false)
        assertEquals(CredentialAction.TYPE_NORMAL, decision)
        val result = when (decision) {
            CredentialAction.USER_HANDOFF, CredentialAction.SYSTEM_AUTOFILL ->
                handoff.requestUserEntry(CREDENTIAL_FIELD_DESCRIPTION)
            CredentialAction.TYPE_NORMAL -> { typedSink += modelText; true }
        }

        assertTrue("a non-password type must proceed", result)
        assertEquals("the model's text MUST be typed into a normal field", listOf(modelText), typedSink)
        assertEquals("a non-password field must NOT consult the handoff", 0, handoff.calls)
        assertNull("the handoff was never shown", handoff.lastDescription)
    }

    // ── per-device AutonomyStore: round-trip + SAFE default ──

    @Test fun autonomy_store_parses_and_defaults_to_permission_when_unset() {
        assertEquals(AutonomyMode.YOLO, AutonomyStore.parse("yolo"))
        assertEquals(AutonomyMode.YOLO, AutonomyStore.parse("YOLO")) // case-insensitive
        assertEquals(AutonomyMode.PERMISSION, AutonomyStore.parse("permission"))
        // The SAFE default the production reader (load()) falls back to when unset/unknown.
        assertEquals(AutonomyMode.PERMISSION, AutonomyStore.parse(null))
        assertEquals(AutonomyMode.PERMISSION, AutonomyStore.parse(""))
        assertEquals(AutonomyMode.PERMISSION, AutonomyStore.parse("garbage"))
    }

    @Test fun autonomy_store_wire_round_trips_both_modes() {
        for (mode in listOf(AutonomyMode.PERMISSION, AutonomyMode.YOLO)) {
            assertEquals(mode, AutonomyStore.parse(AutonomyStore.wireOf(mode)))
        }
        assertEquals(AutonomyStore.WIRE_YOLO, AutonomyStore.wireOf(AutonomyMode.YOLO))
        assertEquals(AutonomyStore.WIRE_PERMISSION, AutonomyStore.wireOf(AutonomyMode.PERMISSION))
    }

    @Test fun autonomy_store_reader_reflects_the_saved_mode() {
        val store = FakeAutonomyStore(AutonomyMode.PERMISSION)
        val read = { store.load() }
        assertEquals(AutonomyMode.PERMISSION, read())
        store.save(AutonomyMode.YOLO)
        assertEquals("the reader must reflect a later save", AutonomyMode.YOLO, read())
    }
}
