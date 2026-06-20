package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.data.local.LiteRtEngine
import com.aiblackbox.portal.ui.chat.LocalEngineState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the PURE mappers behind [LocalModelSettingsScreen] (the on-device
 * model settings screen). Fully hermetic plain JUnit -- no Android, no IO, no
 * Compose. The Composable itself is not unit-tested on the JVM gate; instead the
 * decision/format logic is extracted into these pure cores
 * ([windowWarning], [engineStatusLabel], [formatFreeRam], [clampWindow]) and the
 * Composable just applies them, matching the LiteRtMappers / LocalModelRow
 * convention.
 *
 * The constants (recommended 6144 / range) are read from [LiteRtEngine] -- never
 * hardcoded -- so a future tuning change re-flows here.
 */
class LocalModelSettingsTest {

    // -- windowWarning -------------------------------------------------------

    @Test fun `no warning at or below the recommended default`() {
        assertNull(windowWarning(LiteRtEngine.DEFAULT_MAX_TOKENS))
        assertNull(windowWarning(LiteRtEngine.DEFAULT_MAX_TOKENS - 1))
        assertNull(windowWarning(LiteRtEngine.MIN_TOKENS))
    }

    @Test fun `warning above the recommended default`() {
        val warn = windowWarning(LiteRtEngine.DEFAULT_MAX_TOKENS + 1)
        assertNotNull(warn)
        assertTrue(warn!!.contains(LiteRtEngine.DEFAULT_MAX_TOKENS.toString()))
    }

    @Test fun `warning at the absolute maximum`() {
        assertNotNull(windowWarning(LiteRtEngine.ABSOLUTE_MAX_TOKENS))
    }

    // -- engineStatusLabel ---------------------------------------------------

    @Test fun `engine status label maps every state to friendly text`() {
        val labels = LocalEngineState.values().map { engineStatusLabel(it) }
        labels.forEach { assertTrue(it.isNotBlank()) }
        assertEquals(labels.size, labels.toSet().size)
    }

    @Test fun `ready state reads as loaded`() {
        assertTrue(engineStatusLabel(LocalEngineState.READY).contains("Ready", ignoreCase = true))
    }

    @Test fun `warming state reads as loading`() {
        val l = engineStatusLabel(LocalEngineState.WARMING)
        assertTrue(l.contains("Loading", ignoreCase = true) || l.contains("reload", ignoreCase = true))
    }

    @Test fun `error state reads as failed`() {
        val l = engineStatusLabel(LocalEngineState.ERROR)
        assertTrue(l.contains("error", ignoreCase = true) || l.contains("fail", ignoreCase = true))
    }

    // -- formatFreeRam -------------------------------------------------------

    @Test fun `format free ram in megabytes`() {
        assertEquals("512 MB", formatFreeRam(512L * 1024 * 1024))
    }

    @Test fun `format free ram in gigabytes`() {
        assertEquals("2 GB", formatFreeRam(2L * 1024 * 1024 * 1024))
    }

    @Test fun `format free ram is never blank for zero`() {
        assertTrue(formatFreeRam(0L).isNotBlank())
    }

    // -- clampWindow ---------------------------------------------------------

    @Test fun `clamp window honors values inside the sane range`() {
        assertEquals(LiteRtEngine.DEFAULT_MAX_TOKENS, clampWindow(LiteRtEngine.DEFAULT_MAX_TOKENS))
        assertEquals(8000, clampWindow(8000))
    }

    @Test fun `clamp window raises below-min up to the floor`() {
        assertEquals(LiteRtEngine.MIN_TOKENS, clampWindow(0))
        assertEquals(LiteRtEngine.MIN_TOKENS, clampWindow(LiteRtEngine.MIN_TOKENS - 100))
    }

    @Test fun `clamp window lowers above-max down to the ceiling`() {
        assertEquals(LiteRtEngine.ABSOLUTE_MAX_TOKENS, clampWindow(LiteRtEngine.ABSOLUTE_MAX_TOKENS + 9999))
    }

    @Test fun `clamp window equals resolveMaxTokens`() {
        listOf(0, 100, 512, 6144, 16384, 99999).forEach {
            assertEquals(com.aiblackbox.portal.data.local.resolveMaxTokens(it), clampWindow(it))
        }
    }
}
