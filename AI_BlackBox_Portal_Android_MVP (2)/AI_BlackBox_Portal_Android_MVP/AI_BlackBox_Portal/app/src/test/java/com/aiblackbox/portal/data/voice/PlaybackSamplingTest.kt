package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotSame
import org.junit.Test

/**
 * The TTS ribbon is driven by sampling a pre-decoded envelope at the live playback position — the
 * one place where a scrub to either end, or a table that is one window shorter than its sibling,
 * lands on an out-of-bounds index and takes the player down.
 *
 * The band table legitimately differs in length from the RMS envelope: the RMS pass always emits a
 * trailing partial window, the offline band pass drops one shorter than its minimum probe length.
 * Both are sampled by the SAME position, so both samplers have to clamp rather than assume.
 */
class PlaybackSamplingTest {

    private val bands = arrayOf(
        floatArrayOf(0.0f, 0.1f, 0.2f, 0.3f),
        floatArrayOf(1.0f, 0.9f, 0.8f, 0.7f),
        floatArrayOf(0.5f, 0.5f, 0.5f, 0.5f),
    )

    @Test
    fun `an undecoded clip samples as silence rather than crashing`() {
        val out = sampleBands(emptyArray(), posMs = 500, windowMs = 20)

        assertEquals(AuroraAnalyser.BANDS, out.size)
        assertEquals(0f, out.max(), 0f)
    }

    @Test
    fun `a zero window is silence too`() {
        // windowMs is carried from the decoder; a 0 would divide by zero and hand the ribbon NaN,
        // which the envelope can never recover from (it is absorbing).
        assertEquals(0f, sampleBands(bands, posMs = 500, windowMs = 0).max(), 0f)
    }

    @Test
    fun `a position before the clip clamps to the first window`() {
        val out = sampleBands(bands, posMs = -100, windowMs = 20)

        assertEquals(0.1f, out[1], 1e-6f)
    }

    @Test
    fun `a position past the end clamps to the last window`() {
        val out = sampleBands(bands, posMs = 10_000, windowMs = 20)

        assertEquals(0.5f, out[0], 1e-6f)
    }

    @Test
    fun `a position between two windows interpolates`() {
        // 30ms with a 20ms window = window 1 plus half of the way to window 2.
        val out = sampleBands(bands, posMs = 30, windowMs = 20)

        assertEquals(0.75f, out[0], 1e-6f)
        assertEquals(0.7f, out[1], 1e-6f)
    }

    @Test
    fun `sampling never hands back the table's own row`() {
        // The result is published through a StateFlow and copied into an AuroraVoice; handing out
        // the live row would let a future writer mutate a value the ribbon already believes it has.
        val out = sampleBands(bands, posMs = 0, windowMs = 20)

        assertNotSame(bands[0], out)
        assertEquals(0.0f, out[0], 0f)
    }

    @Test
    fun `the RMS sampler clamps at both ends the same way`() {
        val env = floatArrayOf(0.2f, 0.8f)

        assertEquals(0.2f, sampleEnvelope(env, posMs = -50, windowMs = 20), 1e-6f)
        assertEquals(0.8f, sampleEnvelope(env, posMs = 9_000, windowMs = 20), 1e-6f)
        assertEquals(0.5f, sampleEnvelope(env, posMs = 10, windowMs = 20), 1e-6f)
    }

    @Test
    fun `an empty RMS envelope is silence`() {
        assertEquals(0f, sampleEnvelope(FloatArray(0), posMs = 10, windowMs = 20), 0f)
    }
}
