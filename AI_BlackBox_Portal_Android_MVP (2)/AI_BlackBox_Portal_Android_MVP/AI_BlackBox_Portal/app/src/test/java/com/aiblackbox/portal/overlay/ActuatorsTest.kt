package com.aiblackbox.portal.overlay

import android.accessibilityservice.AccessibilityService
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the PURE helpers of the gesture Actuators (Task 4.3).
 *
 * Only the framework-free math/mappings are unit-tested here:
 *  - [swipeCoords] — the centered-swipe coordinate math.
 *  - [globalActionFor] — the name → GLOBAL_ACTION_* constant mapping.
 *
 * The framework methods on [Actuators] (tap/type/swipe/scroll/open_app/back/home)
 * build [android.os.Bundle], [android.accessibilityservice.GestureDescription],
 * [android.graphics.Path] and [android.content.Intent] and call into a live
 * [BlackBoxA11yService] — none of which exist in the unit-test android.jar (they
 * are `Stub!` throws). Those are device-verified in the 4.5 (wiring) and 4.8
 * (end-to-end) on-device runs, NOT instrumented here.
 */
class ActuatorsTest {

    // ---- swipeCoords: centered-swipe coordinate math (pure) ----------------

    @Test
    fun `swipeCoords up swipes from lower-center to upper-center`() {
        val w = 1080
        val h = 1920
        val c = swipeCoords("up", w, h)!!
        val (startX, startY, endX, endY) = c.let { Quad(it[0], it[1], it[2], it[3]) }
        // A swipe "up" (scroll content up) drags the finger upward: startY > endY.
        assertTrue("up: startY ($startY) should be below endY ($endY)", startY > endY)
        // Both x near horizontal center.
        assertEquals("up: startX ≈ width/2", w / 2, startX)
        assertEquals("up: endX ≈ width/2", w / 2, endX)
        assertInBounds(c, w, h)
    }

    @Test
    fun `swipeCoords down swipes from upper-center to lower-center`() {
        val w = 1080
        val h = 1920
        val c = swipeCoords("down", w, h)!!
        val startY = c[1]
        val endY = c[3]
        assertTrue("down: startY ($startY) should be above endY ($endY)", startY < endY)
        assertEquals("down: startX ≈ width/2", w / 2, c[0])
        assertEquals("down: endX ≈ width/2", w / 2, c[2])
        assertInBounds(c, w, h)
    }

    @Test
    fun `swipeCoords left swipes from right-center to left-center`() {
        val w = 1080
        val h = 1920
        val c = swipeCoords("left", w, h)!!
        val startX = c[0]
        val endX = c[2]
        assertTrue("left: startX ($startX) should be right of endX ($endX)", startX > endX)
        assertEquals("left: startY ≈ height/2", h / 2, c[1])
        assertEquals("left: endY ≈ height/2", h / 2, c[3])
        assertInBounds(c, w, h)
    }

    @Test
    fun `swipeCoords right swipes from left-center to right-center`() {
        val w = 1080
        val h = 1920
        val c = swipeCoords("right", w, h)!!
        val startX = c[0]
        val endX = c[2]
        assertTrue("right: startX ($startX) should be left of endX ($endX)", startX < endX)
        assertEquals("right: startY ≈ height/2", h / 2, c[1])
        assertEquals("right: endY ≈ height/2", h / 2, c[3])
        assertInBounds(c, w, h)
    }

    @Test
    fun `swipeCoords is case-insensitive`() {
        // Documented behavior: direction matching is case-insensitive.
        assertNotNull(swipeCoords("UP", 1080, 1920))
        assertNotNull(swipeCoords("Down", 1080, 1920))
        assertNotNull(swipeCoords("  left  ", 1080, 1920)) // trimmed too
    }

    @Test
    fun `swipeCoords returns null for unknown direction`() {
        assertNull(swipeCoords("diagonal", 1080, 1920))
        assertNull(swipeCoords("", 1080, 1920))
        assertNull(swipeCoords("upward", 1080, 1920))
    }

    @Test
    fun `swipeCoords on a small screen still yields in-bounds coords`() {
        // A tiny screen must not produce negative or out-of-bounds points.
        for (dir in listOf("up", "down", "left", "right")) {
            val c = swipeCoords(dir, 100, 100)!!
            assertInBounds(c, 100, 100)
        }
    }

    @Test
    fun `swipeCoords on a 1x1 degenerate screen stays in-bounds`() {
        // Defensive: never go negative even for a 1x1 surface.
        for (dir in listOf("up", "down", "left", "right")) {
            val c = swipeCoords(dir, 1, 1)!!
            assertInBounds(c, 1, 1)
        }
    }

    // ---- (M5.1) extent math uses the PROVIDED window bounds ----------------
    //
    // The M5.1 fix feeds swipe/scroll the CURRENT window bounds (WindowMetrics), not a
    // phone-narrow display metric. The pure swipeCoords is what turns those bounds into a
    // centered gesture, so feeding tablet vs phone bounds must produce DIFFERENT centers.

    @Test
    fun `swipeCoords centers on the provided bounds — tablet differs from phone`() {
        val phone = swipeCoords("up", 1080, 2400)!!   // portrait phone
        val tablet = swipeCoords("up", 2560, 1600)!!  // landscape tablet / unfolded Fold
        // Vertical swipe is centered horizontally: midX = width/2, which must differ by width.
        assertEquals(540, phone[0])
        assertEquals(1280, tablet[0])
        assertTrue("tablet centered swipe is not the phone-narrow center", tablet[0] != phone[0])
    }

    @Test
    fun `swipeCoords horizontal center tracks the provided height`() {
        val phone = swipeCoords("left", 1080, 2400)!!
        val tablet = swipeCoords("left", 2560, 1600)!!
        // Horizontal swipe centers vertically: midY = height/2.
        assertEquals(1200, phone[1])
        assertEquals(800, tablet[1])
        assertInBounds(phone, 1080, 2400)
        assertInBounds(tablet, 2560, 1600)
    }

    // ---- (M5.2) shouldSetDisplayId: display-addressed gesture gate (pure) ---
    //
    // The plumbing decision behind GestureDescription.Builder.setDisplayId — call it only on
    // API 30+ (setDisplayId landed in R) AND for a NON-default display, so single-display
    // behavior is byte-for-byte unchanged (no builder.setDisplayId call) and a DeX / external
    // display is addressed. (The framework builder.setDisplayId call itself is device-verified,
    // per this file's pure-decision / framework-verified split.)

    @Test
    fun `shouldSetDisplayId only for a non-default display on API 30+`() {
        // Default display (0) → never set, on any API (preserves today's behavior exactly).
        assertFalse(shouldSetDisplayId(android.view.Display.DEFAULT_DISPLAY, 34))
        assertFalse(shouldSetDisplayId(0, 30))
        // Non-default display on API 30+ → address it.
        assertTrue(shouldSetDisplayId(2, 30))
        assertTrue(shouldSetDisplayId(1, 34))
        // Non-default display but pre-30 → cannot (setDisplayId unavailable) → false.
        assertFalse(shouldSetDisplayId(2, 29))
    }

    // ---- globalActionFor: name → GLOBAL_ACTION_* (pure) --------------------

    @Test
    fun `globalActionFor maps back home and recents to the right constants`() {
        assertEquals(AccessibilityService.GLOBAL_ACTION_BACK, globalActionFor("back"))
        assertEquals(AccessibilityService.GLOBAL_ACTION_HOME, globalActionFor("home"))
        // (M1.3) recents is now mapped (was previously null — the M0 gap).
        assertEquals(AccessibilityService.GLOBAL_ACTION_RECENTS, globalActionFor("recents"))
        assertEquals(AccessibilityService.GLOBAL_ACTION_RECENTS, globalActionFor("  Recents "))
    }

    @Test
    fun `globalActionFor is case-insensitive and trims`() {
        // Documented behavior: case-insensitive + trimmed.
        assertEquals(AccessibilityService.GLOBAL_ACTION_BACK, globalActionFor("BACK"))
        assertEquals(AccessibilityService.GLOBAL_ACTION_HOME, globalActionFor("  Home "))
    }

    @Test
    fun `globalActionFor returns null for unknown names`() {
        assertNull(globalActionFor(""))
        assertNull(globalActionFor("backk"))
        assertNull(globalActionFor("overview"))
    }

    // ---- (M1.3) new actuator methods degrade gracefully with no service ----
    //
    // The coordinate tap / recents entry points exist and short-circuit to the
    // "not enabled" result BEFORE touching any framework (Path/GestureDescription/
    // performGlobalAction are Stub! throws in the unit-test android.jar), so a
    // null-service Actuators can exercise the graceful path here. The actual
    // dispatchGesture / performGlobalAction are device-verified.

    @Test
    fun `coordinate tap with no service returns not-enabled gracefully`() = runBlocking {
        // Default mode is YOLO, so the C1 coordinate gate does not fire; the labeler
        // (default, null service) yields None but YOLO never confirms → proceeds to the
        // service check → graceful not-enabled.
        val actuators = Actuators({ null })
        val r = actuators.tap(100, 200)
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", r.detail)
    }

    // ---- (C1, M4) coordinate-tap autonomy gate through the REAL tap(x,y) ---
    //
    // The gate runs BEFORE the service check, so a null-service Actuators with an
    // INJECTED coordinateLabeler exercises the full gate: a denied high-consequence
    // coordinate returns "user declined"; an allowed one passes the gate and then hits
    // the graceful not-enabled (no service). This proves a coordinate_tap can NEVER
    // bypass the confirm-gate (the compose-then-send bypass C1 closes).

    /** Records whether the confirm seam was consulted + returns a fixed answer. */
    private class RecordingConfirm(private val answer: Boolean) : ConfirmUi {
        var calls = 0
        var lastDescription: String? = null
        override suspend fun confirm(description: String): Boolean {
            calls++; lastDescription = description; return answer
        }
    }

    private fun gatedActuators(
        mode: AutonomyMode,
        confirm: ConfirmUi,
        hit: CoordinateHit,
    ) = Actuators(
        service = { null },
        mode = { mode },
        confirm = confirm,
        coordinateLabeler = { _, _ -> hit },
    )

    @Test
    fun `PERMISSION unresolved coordinate tap consults confirm and deny refuses`() = runBlocking {
        val confirm = RecordingConfirm(answer = false)
        val r = gatedActuators(AutonomyMode.PERMISSION, confirm, CoordinateHit.None).tap(10, 20)
        assertFalse(r.success)
        assertEquals("a denied coordinate tap returns a clean user-declined", "user declined", r.detail)
        assertEquals("confirm must be consulted for a tree-blind coordinate", 1, confirm.calls)
        assertEquals("Tap this control", confirm.lastDescription)
    }

    @Test
    fun `PERMISSION unresolved coordinate tap allow passes the gate`() = runBlocking {
        val confirm = RecordingConfirm(answer = true)
        val r = gatedActuators(AutonomyMode.PERMISSION, confirm, CoordinateHit.None).tap(10, 20)
        assertEquals("confirm consulted", 1, confirm.calls)
        // Allowed → past the gate → then the (null) service check → graceful not-enabled.
        assertEquals("an allowed coordinate tap is NOT declined", "accessibility service not enabled", r.detail)
    }

    @Test
    fun `PERMISSION dangerous-label coordinate tap consults confirm and deny refuses`() = runBlocking {
        val confirm = RecordingConfirm(answer = false)
        val r = gatedActuators(AutonomyMode.PERMISSION, confirm, CoordinateHit.Node("Send")).tap(10, 20)
        assertFalse(r.success)
        assertEquals("user declined", r.detail)
        assertEquals(1, confirm.calls)
        assertEquals("Tap \"Send\"", confirm.lastDescription)
    }

    @Test
    fun `PERMISSION benign-label coordinate tap does not consult confirm`() = runBlocking {
        val confirm = RecordingConfirm(answer = false) // would refuse IF consulted
        val r = gatedActuators(AutonomyMode.PERMISSION, confirm, CoordinateHit.Node("Settings")).tap(10, 20)
        assertEquals("a benign labeled coordinate must not gate", 0, confirm.calls)
        assertEquals("accessibility service not enabled", r.detail) // passed gate, no service
    }

    @Test
    fun `YOLO coordinate tap never consults confirm even when unresolved`() = runBlocking {
        val confirm = RecordingConfirm(answer = false) // would refuse IF consulted
        val r = gatedActuators(AutonomyMode.YOLO, confirm, CoordinateHit.None).tap(10, 20)
        assertEquals("YOLO fires a tree-blind coordinate unattended", 0, confirm.calls)
        assertEquals("accessibility service not enabled", r.detail)
    }

    @Test
    fun `recents with no service returns not-enabled gracefully`() {
        val actuators = Actuators({ null })
        val r = actuators.recents()
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", r.detail)
    }

    @Test
    fun `coordinate swipe with no service returns not-enabled gracefully`() {
        val actuators = Actuators({ null })
        assertFalse(actuators.swipe(0, 0, 100, 100).success)
        assertFalse(actuators.swipe(0, 0, 100, 100, 500L).success)
    }

    // ---- (M2 / F1) pressKeyPlan: the PURE key → plan routing --------------

    @Test
    fun `pressKeyPlan routes enter to ImeEnter`() {
        assertTrue(pressKeyPlan("enter") is PressKeyPlan.ImeEnter)
        assertTrue(pressKeyPlan("  Enter ") is PressKeyPlan.ImeEnter)   // case-insensitive + trimmed
    }

    @Test
    fun `pressKeyPlan routes back home recents to the right global actions`() {
        assertEquals(
            AccessibilityService.GLOBAL_ACTION_BACK,
            (pressKeyPlan("back") as PressKeyPlan.Global).action)
        assertEquals(
            AccessibilityService.GLOBAL_ACTION_HOME,
            (pressKeyPlan("home") as PressKeyPlan.Global).action)
        assertEquals(
            AccessibilityService.GLOBAL_ACTION_RECENTS,
            (pressKeyPlan("recents") as PressKeyPlan.Global).action)
    }

    @Test
    fun `pressKeyPlan routes tab delete and unknown to Unsupported`() {
        assertTrue(pressKeyPlan("tab") is PressKeyPlan.Unsupported)
        assertTrue(pressKeyPlan("delete") is PressKeyPlan.Unsupported)
        assertTrue(pressKeyPlan("f13") is PressKeyPlan.Unsupported)
        assertTrue(pressKeyPlan("") is PressKeyPlan.Unsupported)
    }

    @Test
    fun `pressKey with no service returns not-enabled gracefully`() {
        val actuators = Actuators({ null })
        val r = actuators.pressKey("enter")
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", r.detail)
    }

    // ---- helpers ----------------------------------------------------------

    private data class Quad(val a: Int, val b: Int, val c: Int, val d: Int)

    private fun assertInBounds(coords: IntArray, width: Int, height: Int) {
        assertEquals("coords must be [startX,startY,endX,endY]", 4, coords.size)
        val (sx, sy, ex, ey) = Quad(coords[0], coords[1], coords[2], coords[3])
        assertTrue("startX $sx in [0,$width]", sx in 0..width)
        assertTrue("endX $ex in [0,$width]", ex in 0..width)
        assertTrue("startY $sy in [0,$height]", sy in 0..height)
        assertTrue("endY $ey in [0,$height]", ey in 0..height)
    }
}
