package com.aiblackbox.portal.overlay

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the PURE core of the autonomy confirm-gate (Phase 4, Task 4.6).
 *
 * The gate's SAFETY decision is three small pure functions —
 * [isHighConsequence], [shouldConfirm], [describeAction] — plus the [ConfirmUi]
 * seam. These are exhaustively unit-tested here because they are the entire
 * decision surface: "does this action need the user's OK before it fires?"
 *
 * The framework actuation (the live [Actuators] tap/type that the gate wraps,
 * and the system-overlay [ConfirmUi]) calls into a real accessibility service +
 * WindowManager and is device-verified in Task 4.8 — NOT here. What we CAN test
 * here is the full decision pipeline through a fake [ConfirmUi], which is exactly
 * the dangerous part: a benign action must never gate (annoying) and a
 * high-consequence action must never skip the gate in Permission mode
 * (dangerous), and a password's text must never reach the confirm message (leak).
 */
class ConfirmGateTest {

    // ---- isHighConsequence: high-consequence TAPS -------------------------

    @Test
    fun `tap on each high-consequence keyword is high-consequence`() {
        // Every keyword that, tapped, commits/sends/spends/destroys/grants.
        val labels = listOf(
            "Send", "Post", "Pay", "Buy", "Purchase", "Order now",
            "Delete", "Remove", "Uninstall", "Install", "Confirm",
            "Submit", "Transfer", "Allow", "Grant", "Accept", "Agree",
            "Sign in", "Signin", "Log in", "Login", "Checkout", "Place order",
        )
        for (label in labels) {
            assertTrue(
                "tap \"$label\" should be high-consequence",
                isHighConsequence("tap", label, isPasswordTarget = false),
            )
        }
    }

    @Test
    fun `keyword match is case-insensitive and substring`() {
        // The label may be a fuller button string that CONTAINS the keyword.
        assertTrue(isHighConsequence("tap", "SEND MESSAGE", false))
        assertTrue(isHighConsequence("tap", "Pay $42.00 now", false))
        assertTrue(isHighConsequence("tap", "Confirm purchase", false))
        assertTrue(isHighConsequence("tap", "  Delete account  ", false))
    }

    // ---- isHighConsequence: benign TAPS -----------------------------------

    @Test
    fun `tap on benign labels is not high-consequence`() {
        // Navigation / dismissal / inert labels — must NOT gate (over-gating is
        // its own failure: it trains the user to rubber-stamp confirmations).
        val benign = listOf(
            "Cancel", "Back", "Settings", "Close", "Search",
            "Alice Johnson", "Inbox", "Home", "Edit", "Share",
            "Next", "Previous", "Menu",
        )
        for (label in benign) {
            assertFalse(
                "tap \"$label\" should NOT be high-consequence",
                isHighConsequence("tap", label, isPasswordTarget = false),
            )
        }
    }

    @Test
    fun `OK is benign — it does not commit by itself`() {
        // Deliberate call (documented): a bare "OK" acknowledges; it is not in
        // the keyword set, so it does NOT gate. (A genuinely committing button is
        // labeled Confirm/Submit/Pay/etc., which DO gate.)
        assertFalse(isHighConsequence("tap", "OK", false))
        assertFalse(isHighConsequence("tap", "Okay", false))
    }

    @Test
    fun `null or blank tap label is treated as benign`() {
        // DELIBERATE "don't over-gate" choice (flagged for 4.8 review): with no
        // label we cannot judge a plain tap, so we do NOT gate it.
        assertFalse(isHighConsequence("tap", null, false))
        assertFalse(isHighConsequence("tap", "", false))
        assertFalse(isHighConsequence("tap", "   ", false))
    }

    // ---- isHighConsequence: non-tap / non-type actions are benign ---------

    @Test
    fun `navigation and read actions are never high-consequence`() {
        // Even with an alarming label, these don't commit anything.
        for (action in listOf("read_screen", "back", "home", "swipe", "scroll", "open_app")) {
            assertFalse(
                "$action should never be high-consequence",
                isHighConsequence(action, "Send Pay Delete", isPasswordTarget = false),
            )
        }
    }

    // ---- isHighConsequence: TYPE -----------------------------------------

    @Test
    fun `typing into a password target is high-consequence`() {
        // Label is null for a password field (we never read its text) — the
        // password-target flag alone makes it high-consequence.
        assertTrue(isHighConsequence("type", null, isPasswordTarget = true))
    }

    @Test
    fun `typing into a non-password field is not high-consequence`() {
        assertFalse(isHighConsequence("type", "Search box", isPasswordTarget = false))
        assertFalse(isHighConsequence("type", null, isPasswordTarget = false))
    }

    // ---- (C1, M4) non-English high-consequence keywords -------------------

    @Test
    fun `tap on common non-English send pay delete buy labels is high-consequence`() {
        // I2 partial mitigation: a handful of localized verbs are covered (substring,
        // case-insensitive) so a Spanish/French/German/Italian commit button still gates.
        val labels = listOf(
            "Enviar", "Envoyer", "Senden", "Invia",          // send
            "Pagar", "Payer", "Bezahlen", "Pagare",          // pay (pagare ⊃ pagar)
            "Eliminar", "Borrar", "Supprimer", "Löschen",    // delete
            "Comprar", "Acheter", "Kaufen",                  // buy
            "Confirmar", "Confirmer", "Bestätigen",          // confirm (romance ⊃ "confirm")
        )
        for (label in labels) {
            assertTrue(
                "tap \"$label\" should be high-consequence (localized keyword)",
                isHighConsequence("tap", label, isPasswordTarget = false),
            )
        }
    }

    @Test
    fun `documented residual — an uncovered-locale label can still slip the keyword gate`() {
        // KNOWN GAP (I2): the localized set is partial, so e.g. Japanese "送信" (send) or
        // Polish "Wyślij" (send) are NOT caught — a LABELED element tap in an uncovered
        // locale under-gates. This asserts the documented limitation (not a bug) so the
        // gap is visible; the C1 coordinate fail-safe covers the tree-blind path instead.
        assertFalse(isHighConsequence("tap", "送信", isPasswordTarget = false))
        assertFalse(isHighConsequence("tap", "Wyślij", isPasswordTarget = false))
    }

    // ---- (C1, M4) isHighConsequenceCoordinateTap: the coordinate fail-safe ----

    @Test
    fun `an unresolved coordinate is high-consequence (fail-safe)`() {
        // Tree-blind: (x,y) resolved to NO node → confirm by default.
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.None))
    }

    @Test
    fun `a resolved-but-unlabeled coordinate is high-consequence (fail-safe)`() {
        // Icon-only / password / blank-label node → still confirm by default.
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.Node(null)))
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.Node("")))
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.Node("   ")))
    }

    @Test
    fun `a coordinate resolving to a dangerous label is high-consequence`() {
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.Node("Send")))
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.Node("Pay $42.00")))
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.Node("Delete account")))
        assertTrue(isHighConsequenceCoordinateTap(CoordinateHit.Node("Enviar"))) // localized too
    }

    @Test
    fun `a coordinate resolving to a clearly-benign label is not high-consequence`() {
        // The ONLY case a coordinate tap may skip the confirm: it hit-tested to a benign
        // labeled element.
        for (label in listOf("Settings", "Back", "Cancel", "John Smith", "Search")) {
            assertFalse(
                "coordinate tap on benign \"$label\" should not gate",
                isHighConsequenceCoordinateTap(CoordinateHit.Node(label)),
            )
        }
    }

    // ---- (C1, M4) the coordinate GATE composition (mirrors Actuators.coordinateGate) ----

    /** The exact coordinate gate the actuator runs, expressed against the pure fns + seam. */
    private suspend fun coordinateGateAllows(mode: AutonomyMode, hit: CoordinateHit, ui: ConfirmUi): Boolean {
        val hc = isHighConsequenceCoordinateTap(hit)
        if (shouldConfirm(mode, hc)) {
            return ui.confirm(describeAction("tap", (hit as? CoordinateHit.Node)?.label))
        }
        return true
    }

    @Test
    fun `PERMISSION + unresolved coordinate consults confirm — deny refuses`() = runBlocking {
        val ui = FakeConfirmUi(answer = false)
        val allowed = coordinateGateAllows(AutonomyMode.PERMISSION, CoordinateHit.None, ui)
        assertFalse("a denied unresolved coordinate tap must be refused", allowed)
        assertEquals("confirm must be consulted for a tree-blind coordinate", 1, ui.calls)
        assertEquals("Tap this control", ui.lastDescription)
    }

    @Test
    fun `PERMISSION + unresolved coordinate consults confirm — allow fires`() = runBlocking {
        val ui = FakeConfirmUi(answer = true)
        assertTrue(coordinateGateAllows(AutonomyMode.PERMISSION, CoordinateHit.None, ui))
        assertEquals(1, ui.calls)
    }

    @Test
    fun `PERMISSION + dangerous coordinate label surfaces the label in the confirm`() = runBlocking {
        val ui = FakeConfirmUi(answer = false)
        assertFalse(coordinateGateAllows(AutonomyMode.PERMISSION, CoordinateHit.Node("Send"), ui))
        assertEquals(1, ui.calls)
        assertEquals("Tap \"Send\"", ui.lastDescription)
    }

    @Test
    fun `PERMISSION + benign coordinate label never consults confirm`() = runBlocking {
        val ui = FakeConfirmUi(answer = false) // would refuse IF consulted
        assertTrue(coordinateGateAllows(AutonomyMode.PERMISSION, CoordinateHit.Node("Settings"), ui))
        assertEquals("a benign labeled coordinate must not gate", 0, ui.calls)
    }

    @Test
    fun `YOLO + unresolved coordinate never consults confirm`() = runBlocking {
        val ui = FakeConfirmUi(answer = false) // would refuse IF consulted
        assertTrue(coordinateGateAllows(AutonomyMode.YOLO, CoordinateHit.None, ui))
        assertEquals("YOLO fires even a tree-blind coordinate unattended", 0, ui.calls)
    }

    // ---- shouldConfirm: the mode gate -------------------------------------

    @Test
    fun `PERMISSION mode confirms a high-consequence action`() {
        assertTrue(shouldConfirm(AutonomyMode.PERMISSION, isHighConsequence = true))
    }

    @Test
    fun `YOLO mode never confirms even a high-consequence action`() {
        assertFalse(shouldConfirm(AutonomyMode.YOLO, isHighConsequence = true))
    }

    @Test
    fun `PERMISSION mode does not confirm a benign action`() {
        assertFalse(shouldConfirm(AutonomyMode.PERMISSION, isHighConsequence = false))
    }

    @Test
    fun `YOLO mode does not confirm a benign action`() {
        assertFalse(shouldConfirm(AutonomyMode.YOLO, isHighConsequence = false))
    }

    // ---- describeAction: the human-readable confirm message ----------------

    @Test
    fun `describeAction renders a readable tap message`() {
        assertEquals("Tap \"Send\"", describeAction("tap", "Send"))
        assertEquals("Tap \"Confirm purchase\"", describeAction("tap", "Confirm purchase"))
    }

    @Test
    fun `describeAction for a labelless tap is still readable`() {
        assertEquals("Tap this control", describeAction("tap", null))
        assertEquals("Tap this control", describeAction("tap", ""))
    }

    @Test
    fun `describeAction for a password type never includes any text`() {
        // SECURITY: the gate passes label=null for a password target, so the
        // secret can never be in the message. Assert the generic phrasing AND
        // that a hypothetical secret is absent.
        val secret = "hunter2"
        val desc = describeAction("type", null)
        assertEquals("Type into password field", desc)
        assertFalse("password text must never appear in the confirm message", desc.contains(secret))
    }

    @Test
    fun `describeAction for a non-password type names the field, not the text`() {
        // The label here is the FIELD name (e.g. "Search"), never the typed text.
        assertEquals("Type into \"Search\"", describeAction("type", "Search"))
    }

    // ---- end-to-end decision pipeline through a fake ConfirmUi -------------

    /** A fake [ConfirmUi] that records the description it was shown and returns a fixed answer. */
    private class FakeConfirmUi(private val answer: Boolean) : ConfirmUi {
        var calls = 0
        var lastDescription: String? = null
        override suspend fun confirm(description: String): Boolean {
            calls++
            lastDescription = description
            return answer
        }
    }

    /**
     * The exact gate decision the actuator runs, expressed against the pure
     * functions + the [ConfirmUi] seam. Returns true if the action is ALLOWED to
     * proceed. (The real [Actuators] runs this same sequence around its
     * framework actuation, which is device-verified in 4.8.)
     */
    private suspend fun gateAllows(
        mode: AutonomyMode,
        action: String,
        label: String?,
        isPasswordTarget: Boolean,
        ui: ConfirmUi,
    ): Boolean {
        val hc = isHighConsequence(action, label, isPasswordTarget)
        if (shouldConfirm(mode, hc)) {
            return ui.confirm(describeAction(action, label))
        }
        return true
    }

    @Test
    fun `PERMISSION + high-consequence routes through confirm — deny blocks`() = runBlocking {
        val ui = FakeConfirmUi(answer = false)
        val allowed = gateAllows(AutonomyMode.PERMISSION, "tap", "Send", false, ui)
        assertFalse("a denied confirmation must block the action", allowed)
        assertEquals("confirm must be consulted", 1, ui.calls)
        assertEquals("Tap \"Send\"", ui.lastDescription)
    }

    @Test
    fun `PERMISSION + high-consequence routes through confirm — allow proceeds`() = runBlocking {
        val ui = FakeConfirmUi(answer = true)
        val allowed = gateAllows(AutonomyMode.PERMISSION, "tap", "Pay", false, ui)
        assertTrue("an allowed confirmation must let the action proceed", allowed)
        assertEquals(1, ui.calls)
    }

    @Test
    fun `YOLO + high-consequence bypasses confirm entirely`() = runBlocking {
        val ui = FakeConfirmUi(answer = false) // would block IF consulted
        val allowed = gateAllows(AutonomyMode.YOLO, "tap", "Delete", false, ui)
        assertTrue("YOLO must proceed without confirming", allowed)
        assertEquals("confirm must NOT be consulted in YOLO", 0, ui.calls)
    }

    @Test
    fun `PERMISSION + benign never consults confirm`() = runBlocking {
        val ui = FakeConfirmUi(answer = false) // would block IF consulted
        val allowed = gateAllows(AutonomyMode.PERMISSION, "tap", "Cancel", false, ui)
        assertTrue("a benign action must proceed without gating", allowed)
        assertEquals("confirm must NOT be consulted for a benign action", 0, ui.calls)
    }

    @Test
    fun `PERMISSION + password type confirm message carries no secret`() = runBlocking {
        // Even when a (future, non-refused) sensitive type reaches the gate, the
        // confirm message is generic — the typed text is never in it.
        val ui = FakeConfirmUi(answer = true)
        gateAllows(AutonomyMode.PERMISSION, "type", /* label */ null, isPasswordTarget = true, ui)
        assertEquals(1, ui.calls)
        assertEquals("Type into password field", ui.lastDescription)
    }
}
