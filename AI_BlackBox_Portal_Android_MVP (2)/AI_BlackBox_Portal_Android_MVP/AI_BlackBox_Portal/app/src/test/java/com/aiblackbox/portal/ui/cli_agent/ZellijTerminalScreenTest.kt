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

    @Test
    fun `PgUp button selects the wheel in a mouse-tracking TUI`() {
        // The PgUp/PgDn BUTTON handler (deliverButtonScroll) now shares this
        // exact selection with the swipe path. In claude (mouse tracking on,
        // alt buffer on) the button must resolve to WHEEL — bare PgUp is
        // ignored there, which was the "button scroll does nothing" bug.
        assertEquals(ScrollBranch.WHEEL, scrollBranchFor(mouseTracking = true, altBuffer = true))
        assertEquals(ScrollBranch.WHEEL, scrollBranchFor(mouseTracking = true, altBuffer = false))
        // And the button in a no-mouse pager stays PAGE; in a normal shell, LOCAL.
        assertEquals(ScrollBranch.PAGE, scrollBranchFor(mouseTracking = false, altBuffer = true))
        assertEquals(ScrollBranch.LOCAL, scrollBranchFor(mouseTracking = false, altBuffer = false))
    }

    @Test
    fun `page key bytes carry ESC and the PgUp-PgDn discriminator`() {
        // Shared by the swipe (deliverScroll) and button (deliverButtonScroll).
        assertArrayEquals(
            byteArrayOf(0x1b, '['.code.toByte(), '5'.code.toByte(), '~'.code.toByte()),
            pageKeyBytes(scrollUp = true),
        )
        assertArrayEquals(
            byteArrayOf(0x1b, '['.code.toByte(), '6'.code.toByte(), '~'.code.toByte()),
            pageKeyBytes(scrollUp = false),
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

    // ── Swipe physics: gain derivation (row height vs fallback) ──────────

    @Test
    fun `row height derives from the live view metrics`() {
        // A 1520px-tall view showing 38 rows → 40px per row. This replaces
        // the hardcoded 20f that made every swipe scroll ~2x the finger.
        assertEquals(40f, terminalRowHeightPx(viewHeightPx = 1520, rows = 38), 0.001f)
        assertEquals(37.5f, terminalRowHeightPx(viewHeightPx = 1500, rows = 40), 0.001f)
    }

    @Test
    fun `row height falls back pre-layout and on a zero-row emulator`() {
        // view.height == 0 before the first layout pass; emu.mRows can be 0
        // transiently. Both must yield the safe fallback, never divide by zero.
        assertEquals(FALLBACK_ROW_HEIGHT_PX, terminalRowHeightPx(viewHeightPx = 0, rows = 38), 0.001f)
        assertEquals(FALLBACK_ROW_HEIGHT_PX, terminalRowHeightPx(viewHeightPx = 1520, rows = 0), 0.001f)
        assertEquals(FALLBACK_ROW_HEIGHT_PX, terminalRowHeightPx(viewHeightPx = -5, rows = -1), 0.001f)
    }

    @Test
    fun `wheel step honors the notch-per-row tuning knob`() {
        // Default 1.0 → one notch per row-height of travel (same px as a row).
        assertEquals(40f, pixelsPerScrollStep(40f, ScrollBranch.WHEEL, wheelNotchPerRow = 1.0f), 0.001f)
        // 2.0 → two notches per row (half the px per notch): faster wheel.
        assertEquals(20f, pixelsPerScrollStep(40f, ScrollBranch.WHEEL, wheelNotchPerRow = 2.0f), 0.001f)
        // PAGE and LOCAL are strictly 1:1 with the row height.
        assertEquals(40f, pixelsPerScrollStep(40f, ScrollBranch.PAGE), 0.001f)
        assertEquals(40f, pixelsPerScrollStep(40f, ScrollBranch.LOCAL), 0.001f)
    }

    // ── Swipe physics: px→step accumulator ───────────────────────────────

    @Test
    fun `accumulator emits whole steps and keeps the remainder`() {
        val acc = ScrollLineAccumulator()
        assertEquals(0, acc.add(30f, 40f)) // 30px of a 40px row: nothing yet
        assertEquals(1, acc.add(30f, 40f)) // 60px total → 1 step, 20px kept
        assertEquals(0, acc.add(15f, 40f)) // 35px remainder
        assertEquals(1, acc.add(5f, 40f))  // exactly 40px → second step
    }

    @Test
    fun `accumulator handles downward travel symmetrically`() {
        val acc = ScrollLineAccumulator()
        assertEquals(-2, acc.add(-85f, 40f)) // trunc toward zero, -5px kept
        assertEquals(0, acc.add(-30f, 40f))  // -35px remainder
        assertEquals(-1, acc.add(-10f, 40f)) // -45px → one more step
    }

    @Test
    fun `accumulator reset drops the remainder`() {
        val acc = ScrollLineAccumulator()
        assertEquals(0, acc.add(39f, 40f))
        acc.reset()
        // Post-reset the 39px are gone: another 39px still yields nothing.
        assertEquals(0, acc.add(39f, 40f))
    }

    @Test
    fun `accumulator guards a non-positive step size`() {
        val acc = ScrollLineAccumulator()
        assertEquals(0, acc.add(100f, 0f))
        assertEquals(0, acc.add(100f, -40f))
    }

    @Test
    fun `decaying fling deltas through the accumulator deliver the full travel`() {
        // The fling feeds per-frame pixel DELTAS through the same accumulator
        // as the finger. A synthetic decay series (geometric, like
        // splineBasedDecay's tail) must integrate to the same whole-step count
        // as the raw travel — chunking must never lose or invent rows.
        val acc = ScrollLineAccumulator()
        var delta = 120f
        var totalPx = 0f
        var steps = 0
        while (delta > 1f) {
            totalPx += delta
            steps += acc.add(delta, 40f)
            delta *= 0.85f
        }
        assertEquals((totalPx / 40f).toInt(), steps)
    }

    // ── Swipe physics: PAGE→arrows conversion + per-tick coalesce ────────

    @Test
    fun `arrow key bytes are ESC-bracket-A up and ESC-bracket-B down`() {
        // PAGE branch now emits per-LINE arrows (upstream Termux parity), not
        // a full PgUp/PgDn per notch (the old 100px-drag = 5-pages lurch).
        assertArrayEquals(
            byteArrayOf(0x1b, '['.code.toByte(), 'A'.code.toByte()),
            arrowKeyBytes(scrollUp = true),
        )
        assertArrayEquals(
            byteArrayOf(0x1b, '['.code.toByte(), 'B'.code.toByte()),
            arrowKeyBytes(scrollUp = false),
        )
    }

    @Test
    fun `PAGE steps coalesce to the per-tick arrow cap`() {
        assertEquals(1, coalescedArrowCount(1))
        assertEquals(PAGE_MAX_ARROWS_PER_TICK, coalescedArrowCount(5))
        assertEquals(-PAGE_MAX_ARROWS_PER_TICK, coalescedArrowCount(-9))
        assertEquals(0, coalescedArrowCount(0))
        // Excess beyond the cap is DROPPED (collapsed), never queued.
        assertEquals(2, coalescedArrowCount(100, maxPerTick = 2))
    }

    // ── Swipe physics: WHEEL rate-cap + coalescing pacer ─────────────────

    @Test
    fun `wheel pacer emits the first notches immediately from burst budget`() {
        // A slow deliberate one-row scroll must not lag a frame waiting for
        // budget: the budget STARTS full at the burst allowance.
        val pacer = WheelNotchPacer()
        pacer.add(2)
        assertEquals(2, pacer.drain(16_666_667L))
    }

    @Test
    fun `wheel pacer caps sustained emission near the configured rate at 60, 90 and 120Hz`() {
        // The Fold runs 120Hz: the truncation-based budget math at the 8.33ms
        // frame dt is exactly where an off-by-one would hide (0.25 notch of
        // budget per frame). The ~30/s cap must hold at every common refresh
        // rate — neither exceeding it nor starving real scrolling.
        val rates = listOf(
            60 to 16_666_667L,
            90 to 11_111_111L,
            120 to 8_333_333L,
        )
        for ((hz, frameNs) in rates) {
            val pacer = WheelNotchPacer()
            var emitted = 0
            repeat(hz) { // ~1 simulated second of frames at this rate
                pacer.add(10) // the finger keeps producing far beyond the cap
                emitted += kotlin.math.abs(pacer.drain(frameNs))
            }
            // Budget math: the burst allowance up-front, then ~30/s accrual →
            // ≈ 33 notches over the second. NEVER the ~hz*10 requested.
            val upperBound = (WHEEL_MAX_NOTCHES_PER_SEC + WHEEL_BURST_BUDGET + 1f).toInt()
            assertTrue("${hz}Hz: emitted=$emitted > $upperBound", emitted <= upperBound)
            assertTrue(
                "${hz}Hz: emitted=$emitted too low (cap starving scroll)",
                emitted >= (WHEEL_MAX_NOTCHES_PER_SEC * 0.8f).toInt(),
            )
        }
    }

    @Test
    fun `queued wheel notches clear instead of emitting once mouse tracking drops`() {
        // Drain-time branch guard: if the TUI exits mouse tracking mid-fling
        // (claude quitting on its own), the queued notches are STALE —
        // emitting them would land as literal ESC[<6x;1;1M text on the shell
        // prompt. The drain tick must clear the backlog and emit NOTHING.
        val pacer = WheelNotchPacer()
        pacer.add(6)
        assertEquals(0, drainWheelTick(pacer, mouseTrackingNow = false, frameDtNanos = 16_666_667L))
        assertEquals(false, pacer.hasPending)
        // With tracking still on, the same tick emits normally (rate budget).
        pacer.add(2)
        assertEquals(2, drainWheelTick(pacer, mouseTrackingNow = true, frameDtNanos = 16_666_667L))
    }

    @Test
    fun `wheel pacer collapses backlog beyond the cap instead of queueing`() {
        // A monster swipe requests 500 notches; only the clamped backlog may
        // ever reach the wire — the rest collapses (never hundreds of queued
        // Tailscale round-trips scrolling on long after the finger stopped).
        val pacer = WheelNotchPacer()
        pacer.add(500)
        var total = 0
        var frames = 0
        while (pacer.hasPending && frames < 1_000) {
            total += kotlin.math.abs(pacer.drain(16_666_667L))
            frames++
        }
        assertEquals(WHEEL_MAX_BACKLOG_NOTCHES, total)
    }

    @Test
    fun `wheel pacer preserves direction sign`() {
        val pacer = WheelNotchPacer()
        pacer.add(-5)
        val first = pacer.drain(16_666_667L)
        assertTrue("expected negative emission, got $first", first < 0)
        // Opposite directions cancel in the backlog (coalescing).
        val cancelling = WheelNotchPacer()
        cancelling.add(3)
        cancelling.add(-3)
        assertEquals(false, cancelling.hasPending)
    }

    @Test
    fun `wheel pacer clear drops the backlog`() {
        // Touch-to-stop / programmatic reset (setTopRow(0) paths) must kill
        // pending notches instantly — no ghost scrolling under fresh input.
        val pacer = WheelNotchPacer()
        pacer.add(6)
        pacer.clear()
        assertEquals(false, pacer.hasPending)
        assertEquals(0, pacer.drain(16_666_667L))
    }

    // ── Swipe physics: fling threshold ───────────────────────────────────

    @Test
    fun `fling starts only beyond the velocity threshold in either direction`() {
        assertEquals(false, shouldFling(0f))
        assertEquals(false, shouldFling(FLING_MIN_VELOCITY_PX_PER_S - 1f))
        assertTrue(shouldFling(FLING_MIN_VELOCITY_PX_PER_S))
        assertTrue(shouldFling(2_000f))
        assertTrue(shouldFling(-2_000f))
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
