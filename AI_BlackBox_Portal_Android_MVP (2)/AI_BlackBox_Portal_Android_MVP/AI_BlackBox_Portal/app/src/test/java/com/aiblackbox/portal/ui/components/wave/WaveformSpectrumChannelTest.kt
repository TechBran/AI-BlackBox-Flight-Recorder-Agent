package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BINS
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraBins
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The two channels [WaveformFeed] grew for the spectrum work — PER-BAND history (always) and
 * PER-BIN history (lazily) — plus the additive [WaveformFrame] input that carries the spectrum to a
 * renderer.
 *
 * Deliberately a SEPARATE file from WaveformFeedTest. That file pins the shipped feed — the level
 * ring, the decimation, [WaveformFeed.LEVEL_WEIGHTS] — and the whole design claim of this milestone
 * is that carrying bins does not require touching any of it. If a line of WaveformFeedTest ever has
 * to change to accommodate this channel, the additive design has failed and the change is the news.
 */
class WaveformSpectrumChannelTest {

    private fun human(vararg bands: Float) = AuroraVoice(AuroraSpeaker.HUMAN, bands)
    private fun ai(vararg bands: Float) = AuroraVoice(AuroraSpeaker.AI, bands)

    private fun spectrum(speaker: AuroraSpeaker, vararg v: Float) =
        AuroraBins(speaker, FloatArray(AuroraAnalyser.BINS) { v.getOrElse(it) { 0f } })

    // ------------------------------------------------------- per-band history (always)

    @Test
    fun `per-band rings fill through the shipped step, with no spectrum in sight`() {
        val feed = WaveformFeed()
        // The OLD two-argument step: a style that wants one band's history must not have to wait
        // for a producer that publishes spectra.
        repeat(120) { feed.step(1f / 60f, listOf(human(0.8f, 0.4f, 0.2f, 0.1f))) }

        for (b in 0 until AuroraAnalyser.BANDS) {
            val ring = feed.bandHistory(AuroraSpeaker.HUMAN, b)
            assertTrue("band $b ring should have filled, was ${ring.size}", ring.size in 98..101)
        }
        assertEquals(0.8f, feed.bandHistory(AuroraSpeaker.HUMAN, 0).newest(0), 1e-6f)
        assertEquals(0.4f, feed.bandHistory(AuroraSpeaker.HUMAN, 1).newest(0), 1e-6f)
        assertEquals(0.2f, feed.bandHistory(AuroraSpeaker.HUMAN, 2).newest(0), 1e-6f)
        assertEquals(0.1f, feed.bandHistory(AuroraSpeaker.HUMAN, 3).newest(0), 1e-6f)
    }

    @Test
    fun `band history decimates on the same cadence as the level ring`() {
        val feed60 = WaveformFeed()
        repeat(120) { feed60.step(1f / 60f, listOf(human(0.5f, 0.5f, 0.5f, 0.5f))) }
        val feed120 = WaveformFeed()
        repeat(240) { feed120.step(1f / 120f, listOf(human(0.5f, 0.5f, 0.5f, 0.5f))) }

        val a = feed60.bandHistory(AuroraSpeaker.HUMAN, 1)
        val b = feed120.bandHistory(AuroraSpeaker.HUMAN, 1)
        assertEquals(feed60.history(AuroraSpeaker.HUMAN).size, a.size)
        assertTrue("60 Hz (${a.size}) and 120 Hz (${b.size}) must match within 1", kotlin.math.abs(a.size - b.size) <= 1)
    }

    @Test
    fun `an absent speaker's band history is silence, not the other speaker's`() {
        val feed = WaveformFeed()
        repeat(60) { feed.step(1f / 60f, listOf(human(0.9f, 0.9f, 0.9f, 0.9f))) }
        assertEquals(0f, feed.bandHistory(AuroraSpeaker.AI, 0).newest(0), 0f)
        // Out-of-range band indices are clamped rather than thrown, like every other feed read.
        assertEquals(0.9f, feed.bandHistory(AuroraSpeaker.HUMAN, 99).newest(0), 1e-6f)
        assertEquals(0.9f, feed.bandHistory(AuroraSpeaker.HUMAN, -1).newest(0), 1e-6f)
    }

    // --------------------------------------------------------- per-bin history (lazy)

    @Test
    fun `bin rings cost nothing until a spectrum actually arrives`() {
        val feed = WaveformFeed()
        repeat(120) { feed.step(1f / 60f, listOf(human(0.5f, 0f, 0f, 0f))) }

        assertFalse(feed.hasSpectrum(AuroraSpeaker.HUMAN))
        val ring = feed.binHistory(AuroraSpeaker.HUMAN, 0)
        assertEquals(0, ring.size)
        assertEquals(0f, ring.newest(0), 0f)
        assertEquals(0f, feed.bin(AuroraSpeaker.HUMAN, 0), 0f)
        // One shared empty ring, not a fresh allocation per read — this is a per-frame call.
        assertSame(ring, feed.binHistory(AuroraSpeaker.HUMAN, 3))
        assertSame(ring, feed.binHistory(AuroraSpeaker.AI, 0))
    }

    @Test
    fun `bin rings fill once a spectrum arrives`() {
        val feed = WaveformFeed()
        val voices = listOf(human(0.5f, 0f, 0f, 0f))
        val spectra = listOf(spectrum(AuroraSpeaker.HUMAN, 0.7f, 0.6f, 0.5f))
        repeat(120) { feed.step(1f / 60f, voices, spectra) }

        assertTrue(feed.hasSpectrum(AuroraSpeaker.HUMAN))
        val ring = feed.binHistory(AuroraSpeaker.HUMAN, 0)
        assertTrue("bin ring should have filled, was ${ring.size}", ring.size in 98..101)
        assertEquals(0.7f, ring.newest(0), 1e-6f)
        assertEquals(0.6f, feed.binHistory(AuroraSpeaker.HUMAN, 1).newest(0), 1e-6f)
        assertEquals(0f, feed.binHistory(AuroraSpeaker.HUMAN, AuroraAnalyser.BINS - 1).newest(0), 0f)
        // Instantaneous reads follow the same frame the ring did.
        assertEquals(0.5f, feed.bin(AuroraSpeaker.HUMAN, 2), 1e-6f)
        assertEquals(0f, feed.bin(AuroraSpeaker.AI, 2), 0f)
        assertFalse(feed.hasSpectrum(AuroraSpeaker.AI))
    }

    @Test
    fun `two speakers keep independent bin history`() {
        val feed = WaveformFeed()
        repeat(120) {
            feed.step(
                1f / 60f,
                listOf(human(0.6f, 0f, 0f, 0f), ai(0.2f, 0f, 0f, 0f)),
                listOf(spectrum(AuroraSpeaker.HUMAN, 0.8f), spectrum(AuroraSpeaker.AI, 0.3f)),
            )
        }
        assertEquals(0.8f, feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(0), 1e-6f)
        assertEquals(0.3f, feed.binHistory(AuroraSpeaker.AI, 0).newest(0), 1e-6f)
    }

    @Test
    fun `a spectrum that stops updating leaves the history it wrote`() {
        val feed = WaveformFeed()
        val voices = listOf(human(0.5f, 0f, 0f, 0f))
        repeat(60) { feed.step(1f / 60f, voices, listOf(spectrum(AuroraSpeaker.HUMAN, 0.9f))) }
        val filled = feed.binHistory(AuroraSpeaker.HUMAN, 0).size
        // The producer goes away (no spectrum this frame) — the rings hold what they had and take
        // silence from here, exactly as the level ring does for a departed voice.
        repeat(10) { feed.step(1f / 60f, voices) }
        assertFalse(feed.hasSpectrum(AuroraSpeaker.HUMAN))
        assertEquals(0f, feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(0), 0f)
        assertEquals(0.9f, feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(9), 1e-6f)
        assertTrue(feed.binHistory(AuroraSpeaker.HUMAN, 0).size >= filled)
    }

    @Test
    fun `dirty bins are sanitised before they reach a renderer`() {
        val feed = WaveformFeed()
        val dirty = AuroraBins(
            AuroraSpeaker.HUMAN,
            FloatArray(AuroraAnalyser.BINS).also { it[0] = Float.NaN; it[1] = -3f; it[2] = 7f },
        )
        repeat(10) { feed.step(1f / 60f, listOf(human(0.5f, 0f, 0f, 0f)), listOf(dirty)) }
        assertEquals(0f, feed.bin(AuroraSpeaker.HUMAN, 0), 0f)
        assertEquals(0f, feed.bin(AuroraSpeaker.HUMAN, 1), 0f)
        assertEquals(1f, feed.bin(AuroraSpeaker.HUMAN, 2), 0f)
        assertEquals(0f, feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(0), 0f)
        assertEquals(1f, feed.binHistory(AuroraSpeaker.HUMAN, 2).newest(0), 0f)
    }

    // ------------------------------------------------------------------- additivity

    /**
     * THE additivity proof: the same audio through the old two-argument step and through the new
     * three-argument one must leave the level and band channels bit-identical. The spectrum rides
     * ALONGSIDE; it does not touch what was already there.
     */
    @Test
    fun `carrying a spectrum changes nothing about the level or band channels`() {
        val voices = listOf(human(0.7f, 0.5f, 0.3f, 0.1f), ai(0.2f, 0.4f, 0.6f, 0.8f))
        val spectra = listOf(spectrum(AuroraSpeaker.HUMAN, 0.9f), spectrum(AuroraSpeaker.AI, 0.4f))

        val plain = WaveformFeed()
        val withSpectrum = WaveformFeed()
        repeat(200) {
            plain.step(1f / 60f, voices)
            withSpectrum.step(1f / 60f, voices, spectra)
        }

        for (speaker in AuroraSpeaker.entries) {
            assertEquals(plain.level(speaker), withSpectrum.level(speaker), 0f)
            val a = plain.history(speaker)
            val b = withSpectrum.history(speaker)
            assertEquals(a.size, b.size)
            for (i in 0 until a.size) assertEquals("level $speaker $i", a.newest(i), b.newest(i), 0f)
            for (band in 0 until AuroraAnalyser.BANDS) {
                val x = plain.bandHistory(speaker, band)
                val y = withSpectrum.bandHistory(speaker, band)
                assertEquals(x.size, y.size)
                for (i in 0 until x.size) assertEquals("band $band $i", x.newest(i), y.newest(i), 0f)
            }
        }
    }

    @Test
    fun `a dt-zero frame updates the spectrum readers but not their history`() {
        val feed = WaveformFeed()
        feed.step(0f, listOf(human(0.5f, 0f, 0f, 0f)), listOf(spectrum(AuroraSpeaker.HUMAN, 0.6f)))
        assertEquals(0, feed.binHistory(AuroraSpeaker.HUMAN, 0).size)
        assertEquals(0, feed.bandHistory(AuroraSpeaker.HUMAN, 0).size)
        assertEquals(0.6f, feed.bin(AuroraSpeaker.HUMAN, 0), 1e-6f)
    }

    @Test
    fun `reset clears both new channels`() {
        val feed = WaveformFeed()
        repeat(120) {
            feed.step(1f / 60f, listOf(human(0.5f, 0.5f, 0.5f, 0.5f)), listOf(spectrum(AuroraSpeaker.HUMAN, 0.7f)))
        }
        feed.reset()
        assertEquals(0, feed.bandHistory(AuroraSpeaker.HUMAN, 0).size)
        assertEquals(0, feed.binHistory(AuroraSpeaker.HUMAN, 0).size)
        assertEquals(0f, feed.bin(AuroraSpeaker.HUMAN, 0), 0f)
        assertFalse(feed.hasSpectrum(AuroraSpeaker.HUMAN))
    }

    @Test
    fun `an idle producer's shared silent bins are carried without allocating`() {
        // The producer republishes AURORA_SILENT_BINS while it is running but silent; the feed must
        // take it as a PRESENT spectrum of zeros rather than an absent one.
        val feed = WaveformFeed()
        repeat(60) {
            feed.step(1f / 60f, listOf(human(0f, 0f, 0f, 0f)), listOf(AuroraBins(AuroraSpeaker.HUMAN, AURORA_SILENT_BINS)))
        }
        assertTrue(feed.hasSpectrum(AuroraSpeaker.HUMAN))
        assertEquals(0f, feed.bin(AuroraSpeaker.HUMAN, 0), 0f)
        assertEquals(0f, feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(0), 0f)
    }

    // ------------------------------------------------------------ the renderer input

    /**
     * [WaveformFrame] takes new inputs as ADDED FIELDS WITH DEFAULTS — the rule its own header
     * states — so every existing style keeps compiling and every existing construction site keeps
     * meaning what it meant. A style that never heard of a spectrum sees an empty list.
     */
    @Test
    fun `the frame carries a spectrum and defaults to none`() {
        val feed = WaveformFeed()
        val voices = listOf(human(0.5f, 0f, 0f, 0f))
        val old = WaveformFrame(dtSec = 0.016f, phaseSec = 1f, voices = voices, feed = feed, restLevel = 0f)
        assertTrue(old.spectra.isEmpty())

        val spectra = listOf(spectrum(AuroraSpeaker.HUMAN, 0.6f))
        val new = WaveformFrame(
            dtSec = 0.016f,
            phaseSec = 1f,
            voices = voices,
            feed = feed,
            restLevel = 0f,
            spectra = spectra,
        )
        assertEquals(1, new.spectra.size)
        assertEquals(0.6f, new.spectra[0].bin(0), 1e-6f)
    }
}
