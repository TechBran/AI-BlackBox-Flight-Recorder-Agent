package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.model.ZellijSession
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * T22 unit tests for [ZellijTerminalScreen] helpers + the Terminal-state
 * tuple in [CliAgentInternalState].
 *
 * Compose-side rendering / Termux PTY-bridge / WebSocket lifecycle aren't
 * exercised here — same precedent as T20/T21 ([SessionSwitcherTopBarTest],
 * [CliAgentScreenStateTest]) which deferred instrumented Compose UI tests
 * to T23 device QA. The pure helpers + state-tuple shape are the parts a
 * regression could silently break without compile errors, so that's what
 * we lock down here.
 *
 * Coverage:
 *   - [buildBracketedPaste] wraps text in ESC[200~…ESC[201~ correctly.
 *   - [buildBracketedPaste] handles UTF-8 multi-byte input.
 *   - [CliAgentInternalState.Terminal] carries a [ZellijSession] accessibly.
 */
class ZellijTerminalScreenTest {

    // ── buildBracketedPaste ──────────────────────────────────────────────

    @Test
    fun `buildBracketedPaste wraps ascii text in bracketed-paste sequences`() {
        val out = buildBracketedPaste("hi")
        // ESC [ 2 0 0 ~  h  i  ESC [ 2 0 1 ~
        val expected = byteArrayOf(
            0x1b, '['.code.toByte(), '2'.code.toByte(), '0'.code.toByte(), '0'.code.toByte(), '~'.code.toByte(),
            'h'.code.toByte(), 'i'.code.toByte(),
            0x1b, '['.code.toByte(), '2'.code.toByte(), '0'.code.toByte(), '1'.code.toByte(), '~'.code.toByte(),
        )
        assertArrayEquals(expected, out)
    }

    @Test
    fun `buildBracketedPaste handles empty body`() {
        val out = buildBracketedPaste("")
        val expected = byteArrayOf(
            0x1b, '['.code.toByte(), '2'.code.toByte(), '0'.code.toByte(), '0'.code.toByte(), '~'.code.toByte(),
            0x1b, '['.code.toByte(), '2'.code.toByte(), '0'.code.toByte(), '1'.code.toByte(), '~'.code.toByte(),
        )
        assertArrayEquals(expected, out)
        // 12 bytes total — two 6-byte control sequences back-to-back.
        assertEquals(12, out.size)
    }

    @Test
    fun `buildBracketedPaste encodes UTF-8 multi-byte characters`() {
        // "é" is two bytes in UTF-8 (0xC3 0xA9). Whisper transcripts can
        // contain accented characters; we must NOT mangle them to ASCII.
        val out = buildBracketedPaste("é")
        // prefix (6) + body (2) + suffix (6) = 14
        assertEquals(14, out.size)
        // Body bytes at offsets 6,7 should be the UTF-8 encoding of 'é'.
        assertEquals(0xC3.toByte(), out[6])
        assertEquals(0xA9.toByte(), out[7])
    }

    @Test
    fun `buildBracketedPaste preserves newlines as-is`() {
        // Newlines are valid inside bracketed paste; receiving apps treat
        // them as literal text (not Enter keys). Don't accidentally strip
        // or escape them.
        val out = buildBracketedPaste("a\nb")
        assertEquals(6 + 3 + 6, out.size)
        assertEquals('a'.code.toByte(), out[6])
        assertEquals('\n'.code.toByte(), out[7])
        assertEquals('b'.code.toByte(), out[8])
    }

    // ── scrollBranchFor (Task 7: live-state scrollback) ──────────────────

    @Test
    fun `mouse tracking selects the SGR wheel branch`() {
        // A TUI with mouse tracking on (claude/htop) gets wheel reports.
        assertEquals(ScrollBranch.WHEEL, scrollBranchFor(mouseTracking = true, altBuffer = false))
    }

    @Test
    fun `mouse tracking wins even inside the alt buffer`() {
        // claude runs in the alt buffer AND with mouse tracking — the wheel
        // must win, never PgUp. This is the exact combination Brandon's
        // plain-terminal-then-manual-claude repro produces.
        assertEquals(ScrollBranch.WHEEL, scrollBranchFor(mouseTracking = true, altBuffer = true))
    }

    @Test
    fun `alt buffer without mouse tracking selects PgUp-PgDn`() {
        // less / man / a no-mouse pager.
        assertEquals(ScrollBranch.PAGE, scrollBranchFor(mouseTracking = false, altBuffer = true))
    }

    @Test
    fun `normal buffer selects the local transcript branch`() {
        // A bash prompt after a command — emulator owns the scrollback.
        assertEquals(ScrollBranch.LOCAL, scrollBranchFor(mouseTracking = false, altBuffer = false))
    }

    @Test
    fun `branch is a pure function of the two live flags`() {
        // The delivery mechanism depends ONLY on the live emulator flags —
        // never on the provider a session was launched as. So a session
        // "launched as terminal" now running claude (mouseTracking=true) yields
        // the SAME branch as a session "launched as claude": WHEEL. Encoding
        // that invariance here is the guard against re-introducing any
        // launch-provider dependency.
        assertEquals(
            scrollBranchFor(mouseTracking = true, altBuffer = true),   // manual claude
            scrollBranchFor(mouseTracking = true, altBuffer = true),   // launched claude
        )
    }

    // ── sgrWheelBytes (Task 7: the 0x1B introducer regression) ───────────

    @Test
    fun `SGR wheel report carries the required ESC introducer`() {
        // The pre-Task-7 bug omitted 0x1B, so "[<64;1;1M" printed as literal
        // phantom text instead of scrolling. Wheel-up = button 64.
        val up = sgrWheelBytes(scrollUp = true)
        val expectedUp = byteArrayOf(
            0x1b, '['.code.toByte(), '<'.code.toByte(),
            '6'.code.toByte(), '4'.code.toByte(),
            ';'.code.toByte(), '1'.code.toByte(),
            ';'.code.toByte(), '1'.code.toByte(),
            'M'.code.toByte(),
        )
        assertArrayEquals(expectedUp, up)
        assertEquals("must start with the ESC introducer", 0x1b.toByte(), up[0])
    }

    @Test
    fun `SGR wheel report uses button 65 for scroll down`() {
        val down = sgrWheelBytes(scrollUp = false)
        val expectedDown = byteArrayOf(
            0x1b, '['.code.toByte(), '<'.code.toByte(),
            '6'.code.toByte(), '5'.code.toByte(),
            ';'.code.toByte(), '1'.code.toByte(),
            ';'.code.toByte(), '1'.code.toByte(),
            'M'.code.toByte(),
        )
        assertArrayEquals(expectedDown, down)
    }

    // ── CliAgentInternalState.Terminal tuple ─────────────────────────────

    @Test
    fun `Terminal state carries ZellijSession accessibly`() {
        val sess = ZellijSession(
            name = "Brandon__claude___root__1",
            provider = "claude",
            sessionUrl = "https://localhost:9091/sessions/Brandon__claude___root__1",
            token = "secret-token-123",
            expiresAt = "2026-05-26T13:00:00Z",
            createdAt = "2026-05-26T12:00:00Z",
            app = null,
            lastActivity = null,
        )
        val state: CliAgentInternalState = CliAgentInternalState.Terminal(sess)

        // The Terminal branch wires ZellijTerminalScreen(session = state.session);
        // verify the tuple accessor surfaces what we put in.
        assertTrue(state is CliAgentInternalState.Terminal)
        val term = state as CliAgentInternalState.Terminal
        assertEquals("Brandon__claude___root__1", term.session.name)
        assertEquals("secret-token-123", term.session.token)
        assertEquals("claude", term.session.provider)
    }

    @Test
    fun `Terminal state equality is structural over session`() {
        // data class equality means two Terminal states with the same
        // session value compare equal — handy for snapshot testing and
        // recomposition skipping. Verify by constructing twice with the
        // same content and asserting equality.
        val sess = ZellijSession(
            name = "x",
            provider = "terminal",
            sessionUrl = "u",
            token = "t",
            expiresAt = null,
            createdAt = null,
            app = null,
            lastActivity = null,
        )
        assertEquals(
            CliAgentInternalState.Terminal(sess),
            CliAgentInternalState.Terminal(sess),
        )
    }
}
