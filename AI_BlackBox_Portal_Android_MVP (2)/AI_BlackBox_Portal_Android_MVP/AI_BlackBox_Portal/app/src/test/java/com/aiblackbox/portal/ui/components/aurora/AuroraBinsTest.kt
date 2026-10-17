package com.aiblackbox.portal.ui.components.aurora

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [auroraBins] — the SECOND seam beside [auroraVoices], carrying the log-frequency spectrum for the
 * same speakers.
 *
 * It is a separate channel rather than more floats inside [AuroraVoice] because AuroraVoice is
 * exactly [AuroraAnalyser.BANDS] wide by contract (sheet j follows band j, and the renderer's whole
 * geometry is built on that), so widening it would change the shipped ribbon. Everything the voices
 * seam had to get right — null is ABSENCE not silence, one shared silent instance so an idle
 * producer conflates, a defensive copy across the audio/frame thread boundary, content equality —
 * has to be got right identically here, which is what these tests pin.
 */
class AuroraBinsTest {

    private fun bins(vararg v: Float) = FloatArray(AuroraAnalyser.BINS) { v.getOrElse(it) { 0f } }

    @Test
    fun `human and AI spectra stay two separate carriers`() {
        val list = auroraBins(human = bins(0.9f), ai = bins(0.1f))
        assertEquals(2, list.size)
        assertEquals(setOf(AuroraSpeaker.HUMAN, AuroraSpeaker.AI), list.map { it.speaker }.toSet())
        assertEquals(0.9f, list.first { it.speaker == AuroraSpeaker.HUMAN }.bin(0), 1e-6f)
        assertEquals(0.1f, list.first { it.speaker == AuroraSpeaker.AI }.bin(0), 1e-6f)
    }

    @Test
    fun `a source with no audio is absent rather than silent`() {
        assertTrue(auroraBins().isEmpty())
        assertTrue(auroraBins(human = null, ai = null).isEmpty())
        // Present but silent is a carrier of zeros, exactly as it is for the bands.
        assertEquals(1, auroraBins(human = AURORA_SILENT_BINS).size)
    }

    @Test
    fun `the human is listed first so two-speaker containers have a stable order`() {
        val list = auroraBins(human = bins(0.5f), ai = bins(0.5f))
        assertEquals(AuroraSpeaker.HUMAN, list[0].speaker)
        assertEquals(AuroraSpeaker.AI, list[1].speaker)
    }

    @Test
    fun `the shared silent bins are one instance so repeated silence conflates`() {
        // The same reasoning AURORA_SILENT_BANDS is built on: a producer publishes through a
        // StateFlow, StateFlow drops a value that equals the one it holds, and FloatArray compares
        // by IDENTITY — so republishing THIS instance costs nothing while a fresh FloatArray(24)
        // per idle tick would recompose the screen 30 times a second in silence.
        assertSame(AURORA_SILENT_BINS, AURORA_SILENT_BINS)
        assertEquals(AuroraAnalyser.BINS, AURORA_SILENT_BINS.size)
        for (v in AURORA_SILENT_BINS) assertEquals(0f, v, 0f)
    }

    @Test
    fun `bins are copied defensively and compare by content`() {
        // A scratch array reused in place must not mutate a carrier already handed to the frame
        // thread, and two carriers holding equal numbers must compare EQUAL (a data class over a
        // FloatArray compares by identity and would recompose forever).
        val scratch = bins(0.4f, 0.5f)
        val a = AuroraBins(AuroraSpeaker.HUMAN, scratch)
        scratch[0] = 0.9f
        assertEquals(0.4f, a.bin(0), 1e-6f)

        val b = AuroraBins(AuroraSpeaker.HUMAN, bins(0.4f, 0.5f))
        assertEquals(a, b)
        assertEquals(a.hashCode(), b.hashCode())
        assertNotEquals(a, AuroraBins(AuroraSpeaker.AI, bins(0.4f, 0.5f)))
        assertNotEquals(a, AuroraBins(AuroraSpeaker.HUMAN, bins(0.4f, 0.6f)))
    }

    /**
     * Unlike [AuroraVoice.band], an out-of-range bin is EMPTY rather than the nearest one. A band is
     * a sheet that must be driven by something; a bin is a slice of the spectrum, and repeating the
     * top bin upward would paint sibilance the microphone never heard — the same lie the Nyquist
     * rule drops bins to avoid (see `bins above the Nyquist margin read zero instead of aliasing`).
     */
    @Test
    fun `bin is total — any index, any float, never a throw`() {
        val v = AuroraBins(AuroraSpeaker.AI, bins(0.5f))
        assertEquals(0.5f, v.bin(0), 1e-6f)
        assertEquals(0f, v.bin(-1), 0f)
        assertEquals(0f, v.bin(AuroraAnalyser.BINS), 0f)
        assertEquals(0f, v.bin(9999), 0f)

        val short = AuroraBins(AuroraSpeaker.AI, floatArrayOf(0.3f, 0.2f))
        assertEquals(0.3f, short.bin(0), 1e-6f)
        assertEquals("a bin nobody measured is empty, not a copy of its neighbour", 0f, short.bin(5), 0f)
        assertEquals(0f, AuroraBins(AuroraSpeaker.AI, FloatArray(0)).bin(0), 0f)

        // NaN is checked explicitly: every comparison against it is false, so coerceIn passes it
        // straight through (JLS 15.20.1) and one NaN poisons every ring that averages the channel.
        val dirty = AuroraBins(AuroraSpeaker.HUMAN, bins(Float.NaN, -2f, 5f))
        assertEquals(0f, dirty.bin(0), 0f)
        assertEquals(0f, dirty.bin(1), 0f)
        assertEquals(1f, dirty.bin(2), 0f)
    }

    @Test
    fun `a spectrum is audible only when a bin actually carries something`() {
        assertFalse(AuroraBins(AuroraSpeaker.HUMAN, AURORA_SILENT_BINS).hasEnergy())
        assertFalse(AuroraBins(AuroraSpeaker.HUMAN, bins(Float.NaN)).hasEnergy())
        assertTrue(AuroraBins(AuroraSpeaker.HUMAN, bins(0f, 0f, 0.4f)).hasEnergy())
    }
}
