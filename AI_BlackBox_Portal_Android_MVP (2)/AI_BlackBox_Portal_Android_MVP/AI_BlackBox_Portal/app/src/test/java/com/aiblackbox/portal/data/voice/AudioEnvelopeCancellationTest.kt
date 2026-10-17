package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A whole-file MediaCodec decode holds a HARDWARE decoder, which several devices have a global
 * handful of. Abandoning one — scrolling past the bar that asked for it — has to end the decode,
 * and a decode that ends anywhere other than through its `finally` leaks that decoder for the life
 * of the process.
 *
 * The MediaCodec loop itself is not unit-testable; the policy that stops it is, and that is what is
 * pinned here. [AudioEnvelope.decode] consults it once per output buffer.
 */
class AudioEnvelopeCancellationTest {

    @Test
    fun `an active decode is not cancelled`() {
        val gate = DecodeCancellation { true }
        assertFalse(gate.isCancelled())
        assertFalse(gate.isCancelled())
    }

    @Test
    fun `a predicate that goes false cancels`() {
        var live = true
        val gate = DecodeCancellation { live }
        assertFalse(gate.isCancelled())
        live = false
        assertTrue(gate.isCancelled())
    }

    @Test
    fun `cancellation LATCHES, so a predicate that flickers back cannot resurrect the decode`() {
        // The live predicate is "is this still the clip we were asked for", and tapping A then B
        // then A again flips it back to true — with a NEW decode already running for the second A.
        // Two decodes writing one clip's envelope is the bug the latch makes unrepresentable.
        var live = true
        val gate = DecodeCancellation { live }
        live = false
        assertTrue(gate.isCancelled())
        live = true
        assertTrue("once abandoned, always abandoned", gate.isCancelled())
    }

    @Test
    fun `the predicate is not polled again once it has latched`() {
        var polls = 0
        val gate = DecodeCancellation { polls++; false }
        gate.isCancelled()
        gate.isCancelled()
        gate.isCancelled()
        assertEquals(1, polls)
    }

    @Test
    fun `a decode that finds no audio track returns null instead of throwing`() {
        // Exercises the real early-return path THROUGH the finally that releases the extractor.
        assertNull(AudioEnvelope.decode("/does/not/exist.mp3"))
    }

    @Test
    fun `a decode cancelled before it starts returns null`() {
        assertNull(AudioEnvelope.decode("/does/not/exist.mp3", isActive = { false }))
    }
}
