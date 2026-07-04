package com.aiblackbox.portal.ui.cli_agent

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Task 7 regression tests for the ExtraKeysBar byte-emission seam.
 *
 * The reported bug: the Esc button "stopped sending Escape." A byte-path trace
 * showed the emission logic is sound — the on-device failure was tap-delivery
 * loss (Esc was LazyRow item[0] competing with the row's horizontal scroll;
 * now pinned in a fixed leading slot). These tests LOCK the emission contract
 * so a real "wrong byte / swallowed byte" regression can never hide again:
 * a tap on Esc must resolve to exactly one byte 0x1B and hand it to the
 * onKeyBytes sink, under EVERY sticky-modifier combination.
 */
class ExtraKeysBarTest {

    private val ESC_BYTE: Byte = 0x1b

    // ── Esc always resolves to a lone 0x1B, regardless of modifiers ──────────

    @Test
    fun `Esc with no modifiers resolves to a single 0x1B`() {
        val out = resolveKeyBytes(
            bytes = byteArrayOf(ESC_BYTE),
            isLetter = false,
            shiftBytes = null,
            ctrlArmed = false,
            altArmed = false,
            shiftArmed = false,
        )
        assertArrayEquals(byteArrayOf(0x1b), out)
    }

    @Test
    fun `Esc is a lone 0x1B under every sticky-modifier combination`() {
        // Esc carries isLetter=false and shiftBytes=null, so NONE of the
        // Ctrl/Alt/Shift branches can ever transform it. Exhaustively prove it.
        for (ctrl in booleanArrayOf(false, true)) {
            for (alt in booleanArrayOf(false, true)) {
                for (shift in booleanArrayOf(false, true)) {
                    val out = resolveKeyBytes(
                        bytes = byteArrayOf(ESC_BYTE),
                        isLetter = false,
                        shiftBytes = null,
                        ctrlArmed = ctrl,
                        altArmed = alt,
                        shiftArmed = shift,
                    )
                    assertArrayEquals(
                        "Esc must stay [0x1b] for ctrl=$ctrl alt=$alt shift=$shift",
                        byteArrayOf(0x1b),
                        out,
                    )
                }
            }
        }
    }

    // ── The onKeyBytes wiring delivers exactly one 0x1B to the client ────────

    @Test
    fun `tapping Esc hands exactly one 0x1B to the onKeyBytes sink`() {
        // Model the Esc button's action: fireKey(KeySpec("Esc", ESC)) →
        // resolveKeyBytes(...) → onKeyBytes(out). A fake sink captures every
        // call, standing in for terminalView.setTopRow(0); client.sendBytes(bytes).
        val sent = mutableListOf<ByteArray>()
        val onKeyBytes: (ByteArray) -> Unit = { sent.add(it) }

        onKeyBytes(
            resolveKeyBytes(
                bytes = byteArrayOf(ESC_BYTE),
                isLetter = false,
                shiftBytes = null,
                ctrlArmed = false,
                altArmed = false,
                shiftArmed = false,
            ),
        )

        assertEquals("exactly one send", 1, sent.size)
        assertArrayEquals(byteArrayOf(0x1b), sent[0])
    }

    // ── Non-Esc keys still honor modifiers (guards against over-fixing) ──────

    @Test
    fun `Ctrl plus ascii letter maps to the control character`() {
        // Ctrl+a → 0x01 (a=0x61, 0x61 & 0x1f = 0x01).
        val out = resolveKeyBytes(
            bytes = byteArrayOf('a'.code.toByte()),
            isLetter = true,
            shiftBytes = null,
            ctrlArmed = true,
            altArmed = false,
            shiftArmed = false,
        )
        assertArrayEquals(byteArrayOf(0x01), out)
    }

    @Test
    fun `Alt plus letter prepends ESC`() {
        val out = resolveKeyBytes(
            bytes = byteArrayOf('b'.code.toByte()),
            isLetter = true,
            shiftBytes = null,
            ctrlArmed = false,
            altArmed = true,
            shiftArmed = false,
        )
        assertArrayEquals(byteArrayOf(0x1b, 'b'.code.toByte()), out)
    }

    @Test
    fun `Shift with an alternate sequence uses shiftBytes`() {
        // e.g. Shift+PgUp = ESC[5;2~ instead of bare ESC[5~.
        val shiftSeq = byteArrayOf(
            0x1b, '['.code.toByte(), '5'.code.toByte(),
            ';'.code.toByte(), '2'.code.toByte(), '~'.code.toByte(),
        )
        val out = resolveKeyBytes(
            bytes = byteArrayOf(0x1b, '['.code.toByte(), '5'.code.toByte(), '~'.code.toByte()),
            isLetter = false,
            shiftBytes = shiftSeq,
            ctrlArmed = false,
            altArmed = false,
            shiftArmed = true,
        )
        assertArrayEquals(shiftSeq, out)
    }
}
