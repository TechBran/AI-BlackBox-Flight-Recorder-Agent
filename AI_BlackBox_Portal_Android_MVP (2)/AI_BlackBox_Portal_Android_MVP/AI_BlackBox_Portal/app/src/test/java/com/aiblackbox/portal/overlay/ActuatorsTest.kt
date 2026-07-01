package com.aiblackbox.portal.overlay

import android.accessibilityservice.AccessibilityService
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
    fun `coordinate tap with no service returns not-enabled gracefully`() {
        val actuators = Actuators({ null })
        val r = actuators.tap(100, 200)
        assertFalse(r.success)
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
