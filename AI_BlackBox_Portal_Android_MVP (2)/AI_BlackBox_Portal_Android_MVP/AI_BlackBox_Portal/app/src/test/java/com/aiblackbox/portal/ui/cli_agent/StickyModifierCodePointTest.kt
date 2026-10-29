package com.aiblackbox.portal.ui.cli_agent

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * A08-cli-agent-ui-01: the bar's sticky Ctrl/Alt did nothing — no bar key is a letter,
 * and letters from the soft keyboard arrive through TerminalViewClient.onCodePoint,
 * which never consulted the bar. Tapping Ctrl then "c" sent a plain "c", so there was
 * no way to send Ctrl+C from the on-screen keyboard. The modifier state is now hoisted
 * ([StickyModifiers]) and applied to typed code points.
 */
class StickyModifierCodePointTest {

    private fun cp(c: Char) = c.code

    @Test fun `Ctrl armed on the bar turns a typed c into ETX and is consumed`() {
        val m = StickyModifiers()
        m.ctrl = StickyState.Pending
        assertArrayEquals(byteArrayOf(0x03), m.bytesForCodePoint(cp('c'), ctrlDown = false))
        assertEquals("one-shot", StickyState.Off, m.ctrl)
        assertArrayEquals("the next letter is plain", "c".toByteArray(), m.bytesForCodePoint(cp('c'), ctrlDown = false))
    }

    @Test fun `a locked Ctrl survives keys`() {
        val m = StickyModifiers()
        m.ctrl = StickyState.Locked
        assertArrayEquals(byteArrayOf(0x04), m.bytesForCodePoint(cp('d'), false))
        assertArrayEquals(byteArrayOf(0x12), m.bytesForCodePoint(cp('r'), false))
        assertEquals(StickyState.Locked, m.ctrl)
    }

    @Test fun `Alt prefixes ESC and Ctrl plus Alt combine`() {
        val m = StickyModifiers()
        m.alt = StickyState.Pending
        assertArrayEquals(byteArrayOf(0x1b, 'b'.code.toByte()), m.bytesForCodePoint(cp('b'), false))
        m.ctrl = StickyState.Pending
        m.alt = StickyState.Pending
        assertArrayEquals(byteArrayOf(0x1b, 0x03), m.bytesForCodePoint(cp('c'), false))
    }

    @Test fun `a hardware Ctrl still works and multi-byte text passes through`() {
        val m = StickyModifiers()
        assertArrayEquals(byteArrayOf(0x1a), m.bytesForCodePoint(cp('z'), ctrlDown = true))
        val euro = 0x20AC
        assertArrayEquals(String(Character.toChars(euro)).toByteArray(Charsets.UTF_8), m.bytesForCodePoint(euro, false))
    }

    @Test fun `both terminal screens route typed code points through the shared modifiers`() {
        for (screen in listOf("TerminalScreen", "ZellijTerminalScreen")) {
            val src = source("src/main/java/com/aiblackbox/portal/ui/cli_agent/$screen.kt")
            assertTrue("$screen applies the bar's modifiers to typed letters", "stickyModifiers.bytesForCodePoint(" in src)
            assertTrue("$screen shares them with the bar", "modifiers = stickyModifiers" in src)
        }
    }

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel")
        return ""
    }
}
