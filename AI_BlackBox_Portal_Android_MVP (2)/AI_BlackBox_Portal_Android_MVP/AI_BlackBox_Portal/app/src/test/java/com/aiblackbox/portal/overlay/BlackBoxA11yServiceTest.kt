package com.aiblackbox.portal.overlay

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the pure [isAccessibilityServiceEnabled] parser (Task 4.1).
 *
 * Mirrors how Android persists ENABLED_ACCESSIBILITY_SERVICES: a colon-separated
 * list of `pkg/component` entries, where our service may appear in the fully
 * qualified long form or the relative short form. No framework access — the live
 * Settings.Secure read stays in the composable/VM, only the parser is tested.
 */
class BlackBoxA11yServiceTest {

    private val pkg = "com.aiblackbox.portal"
    private val cls = "com.aiblackbox.portal.overlay.BlackBoxA11yService"

    @Test
    fun `long fully-qualified form matches`() {
        val setting = "$pkg/$cls"
        assertTrue(isAccessibilityServiceEnabled(setting, pkg, cls))
    }

    @Test
    fun `short relative form matches`() {
        val setting = "$pkg/.overlay.BlackBoxA11yService"
        assertTrue(isAccessibilityServiceEnabled(setting, pkg, cls))
    }

    @Test
    fun `null setting returns false`() {
        assertFalse(isAccessibilityServiceEnabled(null, pkg, cls))
    }

    @Test
    fun `empty setting returns false`() {
        assertFalse(isAccessibilityServiceEnabled("", pkg, cls))
    }

    @Test
    fun `absent service returns false`() {
        val setting = "com.other.app/com.other.app.SomeService"
        assertFalse(isAccessibilityServiceEnabled(setting, pkg, cls))
    }

    @Test
    fun `a different service of our package does not match`() {
        val setting = "$pkg/$pkg.overlay.OverlayService"
        assertFalse(isAccessibilityServiceEnabled(setting, pkg, cls))
    }

    @Test
    fun `our service among several colon-separated entries matches (long form)`() {
        val setting = listOf(
            "com.other.app/com.other.app.SomeService",
            "$pkg/$cls",
            "com.third.app/.TheirService",
        ).joinToString(":")
        assertTrue(isAccessibilityServiceEnabled(setting, pkg, cls))
    }

    @Test
    fun `our service among several colon-separated entries matches (short form)`() {
        val setting = listOf(
            "com.other.app/com.other.app.SomeService",
            "$pkg/.overlay.BlackBoxA11yService",
        ).joinToString(":")
        assertTrue(isAccessibilityServiceEnabled(setting, pkg, cls))
    }

    @Test
    fun `entries with surrounding whitespace still match`() {
        val setting = " $pkg/$cls : com.other.app/com.other.app.X "
        assertTrue(isAccessibilityServiceEnabled(setting, pkg, cls))
    }
}
