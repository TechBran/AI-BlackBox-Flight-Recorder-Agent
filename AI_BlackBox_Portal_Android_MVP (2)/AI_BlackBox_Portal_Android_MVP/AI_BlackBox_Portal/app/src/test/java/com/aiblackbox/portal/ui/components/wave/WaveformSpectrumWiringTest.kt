package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BINS
import com.aiblackbox.portal.ui.components.aurora.AnalysisFrame
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraBins
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.aurora.auroraBins
import com.aiblackbox.portal.ui.components.wave.styles.SoftBlob
import com.aiblackbox.portal.ui.components.wave.styles.Cinematic
import com.aiblackbox.portal.ui.components.wave.styles.ClassicMath
import com.aiblackbox.portal.ui.components.wave.styles.MirroredPulse
import com.aiblackbox.portal.ui.components.wave.styles.OrbMath
import com.aiblackbox.portal.ui.components.wave.styles.ParticleWave
import com.aiblackbox.portal.ui.components.wave.styles.RibbonMath
import com.aiblackbox.portal.ui.components.wave.styles.SegmentedEqMath
import com.aiblackbox.portal.ui.components.wave.styles.SharpLineMath
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin

/**
 * M7 WIRED. The ten styles were built spectrum-aware and then shipped blind: not one of the four
 * [VoiceVisualizer] call sites passed `spectra`, so every style silently ran its four-band fallback
 * and the milestone produced no visible change at all. This file pins the wiring — that a spectrum
 * REACHES a style, that what reaches it is worth drawing, and that the styles which cannot have one
 * still work.
 *
 * The three things it asserts, in the order they can go wrong:
 *
 *  1. THE SEAM ([auroraBins]) turns a producer's array into presence or absence correctly, including
 *     for the empty array a call site's default lambda hands it.
 *  2. THE MAGNITUDE. Wiring DEAD bins would be worse than not wiring them: Segmented EQ and Circular
 *     Orb draw their geometry straight off a bin, so a channel whose peak sat at 0.06 (which is
 *     where it sat before the gain landed) would turn two twitchy styles into two flat ones. The
 *     drive is therefore measured end to end through the REAL [AudioAnalysisTap] and the REAL
 *     [WaveformFeed] and required to be usable.
 *  3. THE M7 ACCEPTANCE, ON THE WIRED PATH. Two spectra at the SAME overall level must draw
 *     differently. "Same level" is exact here rather than approximate, because both spectra ride the
 *     SAME voice: level is the four bands' weighted max ([WaveformFeed.LEVEL_WEIGHTS]) and the
 *     spectrum does not enter it, so a difference in output can only have come from the spectrum.
 *
 * The preview's synthetic spectrum ([WaveformDemoDriver.binsAt]) is pinned here too, for the reason
 * [WaveformStylePreview] exists at all: an operator picks a style from that preview, and a preview
 * without a spectrum would advertise the four-band fallback for six styles that no longer run it.
 */
class WaveformSpectrumWiringTest {

    private val BINS = AuroraAnalyser.BINS
    private val BANDS = AuroraAnalyser.BANDS

    private fun human(vararg bands: Float) = AuroraVoice(AuroraSpeaker.HUMAN, bands)

    // =========================================================================
    // 1. the seam: what counts as a spectrum
    // =========================================================================

    /**
     * A call site's deferred-read lambda defaults to `FloatArray(0)` (Composer's `recordingBins`
     * mirrors its `recordingBands`), and a zero-length array is NOT a silent spectrum — it is no
     * spectrum. The distinction is the whole wiring: presence sends six styles down their bin path,
     * and sending them there with nothing to read would draw a flat, dead ribbon where the bands
     * would have drawn a live one.
     *
     * This is where [auroraBins] parts company with [com.aiblackbox.portal.ui.components.aurora.auroraVoices],
     * and the reason is each carrier's own documented rule: AuroraVoice SPREADS a short array over
     * the remaining sheets (one RMS level legitimately drives all four), while AuroraBins reads an
     * index it does not carry as 0 precisely so nothing paints sibilance the microphone never heard.
     * A short bands array is a coarse measurement; a short bins array is mostly a lie.
     */
    @Test
    fun `an array too short to be a spectrum is absence, not a silent spectrum`() {
        assertTrue("no producer at all", auroraBins().isEmpty())
        assertTrue("the default lambda's empty array", auroraBins(human = FloatArray(0)).isEmpty())
        assertTrue("a truncated array", auroraBins(human = FloatArray(BINS - 1)).isEmpty())
        assertTrue("a single level is not a spectrum", auroraBins(ai = floatArrayOf(0.5f)).isEmpty())

        // ...and a real, full-width spectrum IS presence, all-zero included: a producer that is
        // running but silent must keep its spectrum PRESENT, exactly as a running-but-silent voice
        // stays present (see AURORA_SILENT_BINS).
        assertEquals(1, auroraBins(human = FloatArray(BINS)).size)
        assertEquals(1, auroraBins(human = AURORA_SILENT_BINS).size)
        assertEquals(2, auroraBins(human = FloatArray(BINS), ai = FloatArray(BINS)).size)
        // A WIDER array is still a spectrum — every consumer reads the first BINS of it.
        assertEquals(1, auroraBins(human = FloatArray(BINS + 8)).size)

        // Mixed: one real producer and one that has not started.
        val mixed = auroraBins(human = FloatArray(BINS) { 0.5f }, ai = FloatArray(0))
        assertEquals(1, mixed.size)
        assertEquals(AuroraSpeaker.HUMAN, mixed[0].speaker)
    }

    // =========================================================================
    // 2. the magnitude, measured through the real tap and the real feed
    // =========================================================================

    /**
     * THE GATE THAT MADE WIRING SAFE. Before the bin channel was given a gain, the loudest bin of
     * ordinary speech read p50 0.019 / p90 0.064 of the 0..1 range a bar is drawn in, and NONE of
     * the 24 bins ever reached 1.0 — Segmented EQ would have drawn every column in the bottom
     * fifteenth of its stack and Circular Orb every tick as a stub. This measures what a style
     * receives today, all the way through: real tap at the composer's rate and hop, real feed at
     * 120 Hz (the Fold), real style math.
     *
     * The bound is deliberately far below what is measured (p90 0.70 for the raw channel, 0.79 for
     * Segmented EQ's drawn drive): it is a floor under "is this channel worth drawing", not a pin on
     * the gain, which [com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTapTest] owns.
     */
    @Test
    fun `a wired style receives a usable drive from the real microphone path`() {
        val m = measureThroughTheRealPath(gain = 1.0)
        assertTrue(
            "the loudest bin a style reads is only ${m.loudestBinP90} — wiring this would draw a dead ribbon",
            m.loudestBinP90 > 0.3f,
        )
        assertTrue(
            "Segmented EQ's tallest column would only reach ${m.segmentedEqP90} of its stack",
            m.segmentedEqP90 > 0.3f,
        )
        // ...and the channel is not simply pinned at its ceiling either: quiet speech must draw
        // measurably shorter than ordinary speech, or the normalisation has eaten the dynamics.
        val quiet = measureThroughTheRealPath(gain = 0.2)
        assertTrue(
            "quiet ${quiet.loudestBinP90} must read below ordinary ${m.loudestBinP90}",
            quiet.loudestBinP90 < m.loudestBinP90 * 0.8f,
        )
    }

    /**
     * WHY A STYLE MUST READ [WaveformFeed.bin] AND NEVER [WaveformFrame.spectra].
     *
     * The producer publishes one frame per 20 ms hop (~50 Hz) and the renderer draws at 60-120 Hz,
     * so the carrier the frame hands over is a STAIRCASE: measured here on the real tap at 120 Hz,
     * 58% of drawn frames read a value byte-identical to the frame before, then the value jumps. The
     * feed's smoother is what turns that into motion — 0% repeats, and the mean step halves.
     *
     * That is the "isn't fluent" half of the v35 Fold report, and it is exactly what a style would
     * have re-created by reading the raw list. Two styles (Classic, Glowing Ribbon) DID read it,
     * back when the list was always empty and it could not matter; wiring the channel is what makes
     * it matter, so both now read the feed like the other four.
     */
    @Test
    fun `the raw spectrum carrier steps at the publish rate and the feed does not`() {
        val frames = tapFrames(gain = 1.0)
        val feed = WaveformFeed()
        val dt = 1f / 120f
        val hopSec = AudioAnalysisTap.HOP_MS / 1000f
        var acc = 0f
        var fi = 0
        var steps = 0
        var rawRepeats = 0
        var feedRepeats = 0
        var compared = 0
        var prevRaw = -1f
        var prevFeed = -1f
        while (fi < frames.size) {
            acc += dt
            while (acc >= hopSec && fi < frames.size) { acc -= hopSec; fi++ }
            if (fi >= frames.size) break
            val carrier = AuroraBins(AuroraSpeaker.HUMAN, frames[fi].bins)
            feed.step(dt, listOf(AuroraVoice(AuroraSpeaker.HUMAN, frames[fi].bands)), listOf(carrier))
            steps++
            if (steps < 60) continue
            val raw = carrier.bin(11)
            val smoothed = feed.bin(AuroraSpeaker.HUMAN, 11)
            if (prevRaw >= 0f) {
                compared++
                if (raw == prevRaw) rawRepeats++
                if (smoothed == prevFeed) feedRepeats++
            }
            prevRaw = raw
            prevFeed = smoothed
        }
        assertTrue("not enough frames to measure ($compared)", compared > 200)
        val rawRepeatFraction = rawRepeats.toFloat() / compared
        val feedRepeatFraction = feedRepeats.toFloat() / compared
        assertTrue(
            "the raw carrier should repeat on most drawn frames, was $rawRepeatFraction",
            rawRepeatFraction > 0.4f,
        )
        assertTrue(
            "the feed must move EVERY frame, but repeated on $feedRepeatFraction of them",
            feedRepeatFraction < 0.01f,
        )
    }

    // =========================================================================
    // 3. the M7 acceptance, per style, on the wired path
    // =========================================================================

    /**
     * The six styles that CONSUME the spectrum: with the spectrum wired, each draws a different
     * picture for two frames the level channel calls identical. Both spectra ride the same voice, so
     * "identical level" is exact and asserted, not assumed.
     *
     * Every read below is [WaveformFeed.bin] / [WaveformFeed.band] — the same channels the renderers
     * read inside their draw — and every drive is the one the style actually paints with, so a style
     * that stopped consulting its bins would fail here rather than merely look different.
     */
    @Test
    fun `every spectrum-consuming style draws two equal-level spectra differently`() {
        val voice = human(0.55f, 0.40f, 0.22f, 0.11f)
        // Energy at the bottom of the map versus at the top, at the same peak height.
        val low = FloatArray(BINS) { if (it < 8) 0.62f else 0.02f }
        val high = FloatArray(BINS) { if (it >= 16) 0.62f else 0.02f }

        val a = settledFeed(voice, low)
        val b = settledFeed(voice, high)
        assertEquals(
            "the two frames must be at the same overall level for this to prove anything",
            a.level(AuroraSpeaker.HUMAN), b.level(AuroraSpeaker.HUMAN), 0f,
        )
        assertTrue("the spectrum must have reached the feed", a.hasSpectrum(AuroraSpeaker.HUMAN))
        assertTrue("the spectrum must have reached the feed", b.hasSpectrum(AuroraSpeaker.HUMAN))

        val binsA = binsOf(a)
        val binsB = binsOf(b)
        val bandsA = bandsOf(a)
        val bandsB = bandsOf(b)
        // Identical bands by construction — the ONLY difference between the two frames is spectral.
        assertTrue(bandsA.contentEquals(bandsB))

        // -- Segmented EQ: one column per bin range, drawn as a lit LED count.
        assertDrivesDiffer("Segmented EQ") { bins, bands ->
            FloatArray(24) { c -> SegmentedEqMath.columnDrive(bins, bands, c, 24) }
        }
        // -- Circular Orb: one radial tick per bin range.
        assertDrivesDiffer("Circular Orb") { bins, bands ->
            FloatArray(24) { i -> OrbMath.tickDrive(bins, bands, i, 24, 0f) }
        }
        // -- Soft Blob: spectral slices become lobe radii.
        assertDrivesDiffer("Soft Blob") { bins, bands ->
            FloatArray(SoftBlob.SLICES.size).also { SoftBlob.sliceDrives(bands, bins, 0.5f, it) }
        }
        // -- Particle Wave: spectral slices become per-depth surfaces.
        assertDrivesDiffer("Particle Wave") { bins, bands ->
            FloatArray(ParticleWave.SLICES.size).also { ParticleWave.sliceDrives(bands, bins, 0.5f, it) }
        }
        // -- Classic: three ribbons sharing out one height by spectral group.
        assertDrivesDiffer("Classic") { bins, bands ->
            FloatArray(ClassicMath.LAYERS) { layer ->
                ClassicMath.layerShare(bins, bands[0], bands[1], bands[2], bands[3], layer)
            }
        }
        // -- Glowing Ribbon: strand drives plus the centroid/spread that bend the braid.
        assertDrivesDiffer("Glowing Ribbon") { bins, bands ->
            val levels = bins ?: bands
            val n = if (bins != null) BINS else BANDS
            FloatArray(9) { i ->
                when (i) {
                    0 -> RibbonMath.centroid(levels, n)
                    1 -> RibbonMath.spread(levels, n)
                    else -> RibbonMath.strandLevel(levels, n, i - 2, 7)
                }
            }
        }
    }

    /**
     * The three styles that are BAND-driven by design — their "spectral" input is the four-band
     * channel, read out of [WaveformFeed.bandHistory], and no bin enters their geometry.
     *
     * Asserted rather than left unsaid, because "this style ignores the spectrum" and "this style
     * silently fell back" look identical from outside. These three answer a coarser question by
     * choice (how bright is this column, out of four bands), which is why carrying a spectrum leaves
     * them bit-identical — and why the M7 requirement is met for them through their OWN
     * equal-level tests (CinematicGlowTest, SharpLineTest, MirroredPulseTest) rather than here.
     */
    @Test
    fun `the band-driven styles are unmoved by a spectrum, and say so`() {
        val voice = human(0.55f, 0.40f, 0.22f, 0.11f)
        val low = FloatArray(BINS) { if (it < 8) 0.62f else 0.02f }
        val high = FloatArray(BINS) { if (it >= 16) 0.62f else 0.02f }
        val a = settledFeed(voice, low)
        val b = settledFeed(voice, high)

        fun bandHistoryDrives(feed: WaveformFeed): FloatArray {
            val h = FloatArray(BANDS) { feed.bandHistory(AuroraSpeaker.HUMAN, it).newest(0) }
            return floatArrayOf(
                Cinematic.brightness(h[0], h[1], h[2], h[3]),
                SharpLineMath.brightness(h[0], h[1], h[2], h[3]),
                MirroredPulse.bandShare(h[0], h[1], h[2], h[3], 0),
                MirroredPulse.bandShare(h[0], h[1], h[2], h[3], 3),
            )
        }
        assertTrue(
            "a band-driven style must be untouched by which spectrum rides alongside",
            bandHistoryDrives(a).contentEquals(bandHistoryDrives(b)),
        )
        // ...and they are not simply dead: the BANDS still move them.
        val warm = settledFeed(human(0.60f, 0.30f, 0.05f, 0.02f), low)
        assertFalse(
            "the band channel must still drive them",
            bandHistoryDrives(a).contentEquals(bandHistoryDrives(warm)),
        )
    }

    /**
     * Aurora, the tenth style and the default, takes no spectrum AT ALL — it is a self-contained
     * composable that receives only the voices, so a call site cannot hand it one however hard it
     * tries. That is the property that keeps the default look pixel-identical while the other nine
     * gain a channel, and it is stated here because "the default did not change" is the one claim
     * this wiring must not quietly break.
     */
    @Test
    fun `aurora cannot see a spectrum, whatever a call site passes`() {
        val entry = WaveformStyles.resolve("aurora")
        assertTrue(
            "aurora must stay a self-contained module; a Draw renderer WOULD be handed the spectrum",
            entry.content is WaveformContent.SelfContained,
        )
    }

    // =========================================================================
    // the settings preview's synthetic spectrum
    // =========================================================================

    /**
     * The preview is the ACTUAL [VoiceVisualizer] and the operator chooses a style from it, so it has
     * to show the same channels the composer will. [WaveformDemoDriver.binsAt] is the spectrum half
     * of that promise: present for exactly the speakers [WaveformDemoDriver.voicesAt] returns, in the
     * same order, so the preview can never draw a spectrum for a speaker who is not there.
     */
    @Test
    fun `the demo driver publishes a spectrum for exactly the speakers it publishes a voice for`() {
        val driver = WaveformDemoDriver()
        var sawTwo = false
        var sawOne = false
        var sawNone = false
        var t = 0.0
        while (t < driver.timeline.periodSec * 2) {
            val voices = driver.voicesAt(t)
            val spectra = driver.binsAt(t)
            assertEquals("at t=$t", voices.size, spectra.size)
            for (i in voices.indices) assertEquals("at t=$t", voices[i].speaker, spectra[i].speaker)
            when (voices.size) {
                0 -> sawNone = true
                1 -> sawOne = true
                2 -> sawTwo = true
            }
            t += 1.0 / 60.0
        }
        assertTrue("the demo must exercise the two-voice overlap", sawTwo)
        assertTrue(sawOne)
        assertTrue("the demo must exercise the pause", sawNone)
    }

    @Test
    fun `the demo spectrum is deterministic, bounded, and in the real channel's range`() {
        val a = WaveformDemoDriver()
        val b = WaveformDemoDriver()
        val ceiling = 1f / AudioAnalysisTap.BIN_HEADROOM

        var peak = 0f
        var t = 0.0
        while (t < a.timeline.periodSec) {
            val x = a.binsAt(t)
            val y = b.binsAt(t)
            assertEquals(x.size, y.size)
            for (i in x.indices) {
                for (k in 0 until BINS) {
                    val v = x[i].bin(k)
                    assertEquals("determinism at t=$t bin $k", v, y[i].bin(k), 0f)
                    assertTrue("bin $k out of range at t=$t: $v", v in 0f..1f)
                    if (v > peak) peak = v
                }
                // NaN/absent handling is AuroraBins', but the width is the driver's to get right.
                assertEquals(0f, x[i].bin(BINS), 0f)
            }
            t += 1.0 / 120.0
        }
        // Loud syllables must reach the same ceiling the real tap publishes, so a bar that fills its
        // stack in the preview fills it on a device too — and must not EXCEED it, or the preview
        // would promise a headroom the microphone path does not have.
        assertTrue("the preview never gets loud (peak $peak vs ceiling $ceiling)", peak > ceiling * 0.85f)
        assertTrue("the preview overdrives past the real ceiling ($peak > $ceiling)", peak <= ceiling + 1e-4f)

        // A different seed is a different (equally believable) spectrum, not the same one.
        val other = WaveformDemoDriver(seed = 0xC0FFEEL)
        val at = a.binsAt(1.0)[0]
        val ot = other.binsAt(1.0)[0]
        var differs = false
        for (k in 0 until BINS) if (abs(at.bin(k) - ot.bin(k)) > 1e-4f) differs = true
        assertTrue("the seed must pin the spectrum's texture too", differs)
    }

    /**
     * The preview's spectrum has to be worth previewing: a synthetic channel that was just the four
     * bands widened to 24 would draw Segmented EQ as four plateaus and advertise a look no device
     * produces. Two properties make it a spectrum — bin-to-bin STRUCTURE inside a single band's
     * frequency range, and a coarse shape that still AGREES with the bands published at the same
     * instant (they are two readings of one synthetic voice, exactly as they are of a real one).
     */
    @Test
    fun `the demo spectrum has real structure and still agrees with the demo bands`() {
        val driver = WaveformDemoDriver()
        // A loud instant inside the human's turn.
        var best = 0.0
        var bestPeak = 0f
        var t = 0.2
        while (t < 2.2) {
            val v = driver.binsAt(t)
            if (v.isNotEmpty()) {
                var m = 0f
                for (k in 0 until BINS) if (v[0].bin(k) > m) m = v[0].bin(k)
                if (m > bestPeak) { bestPeak = m; best = t }
            }
            t += 1.0 / 120.0
        }
        val bins = driver.binsAt(best)[0]
        val voice = driver.voicesAt(best)[0]

        // STRUCTURE: neighbouring bins differ. A 4-band staircase would be flat across each run.
        var neighbourChanges = 0
        for (k in 1 until BINS) if (abs(bins.bin(k) - bins.bin(k - 1)) > 0.01f) neighbourChanges++
        assertTrue(
            "only $neighbourChanges of ${BINS - 1} neighbouring bins differ — this is a staircase, not a spectrum",
            neighbourChanges >= BINS - 4,
        )

        // AGREEMENT: the band the voice says is loudest must own the loudest region of the spectrum.
        var loudBand = 0
        for (j in 1 until BANDS) if (voice.band(j) > voice.band(loudBand)) loudBand = j
        var quietBand = 0
        for (j in 1 until BANDS) if (voice.band(j) < voice.band(quietBand)) quietBand = j
        assertTrue("degenerate instant", voice.band(loudBand) > voice.band(quietBand) + 0.05f)
        assertTrue(
            "the spectrum contradicts the bands published at the same instant",
            groupPeak(bins, loudBand) > groupPeak(bins, quietBand),
        )
    }

    // =========================================================================
    // helpers
    // =========================================================================

    /** The peak of the bins whose centre frequency falls in band [band]'s probe span. */
    private fun groupPeak(bins: AuroraBins, band: Int): Float {
        var m = 0f
        for (k in 0 until BINS) {
            if (WaveformDemoDriver.bandForBin(k) == band && bins.bin(k) > m) m = bins.bin(k)
        }
        return m
    }

    private fun settledFeed(voice: AuroraVoice, bins: FloatArray): WaveformFeed {
        val feed = WaveformFeed()
        val spectra = listOf(AuroraBins(AuroraSpeaker.HUMAN, bins))
        repeat(240) { feed.step(1f / 120f, listOf(voice), spectra) }
        return feed
    }

    private fun binsOf(feed: WaveformFeed) = FloatArray(BINS) { feed.bin(AuroraSpeaker.HUMAN, it) }
    private fun bandsOf(feed: WaveformFeed) = FloatArray(BANDS) { feed.band(AuroraSpeaker.HUMAN, it) }

    /**
     * Run one style's drive function over the two equal-level frames and require the results to
     * differ — AND require the same function on the four-band FALLBACK to be identical across them,
     * which is what proves the difference came from the spectrum rather than from the test.
     */
    private fun assertDrivesDiffer(style: String, drives: (FloatArray?, FloatArray) -> FloatArray) {
        val voice = human(0.55f, 0.40f, 0.22f, 0.11f)
        val low = FloatArray(BINS) { if (it < 8) 0.62f else 0.02f }
        val high = FloatArray(BINS) { if (it >= 16) 0.62f else 0.02f }
        val a = settledFeed(voice, low)
        val b = settledFeed(voice, high)
        val bands = bandsOf(a)

        val withA = drives(binsOf(a), bands)
        val withB = drives(binsOf(b), bands)
        assertNotEquals(
            "$style draws two equal-level spectra identically — it is not reading the spectrum",
            withA.toList(), withB.toList(),
        )
        // The fallback is the control: identical bands, so identical output, so the difference above
        // is the spectrum's and nothing else's.
        assertEquals(
            "$style's band fallback should be blind to the spectrum",
            drives(null, bands).toList(), drives(null, bandsOf(b)).toList(),
        )
    }

    // ------------------------------------------------- the real microphone path

    private class PathMeasurement(val loudestBinP90: Float, val segmentedEqP90: Float)

    private fun pct(xs: List<Float>, p: Double): Float {
        val s = xs.sorted()
        val i = Math.round(p / 100.0 * (s.size - 1)).toInt().coerceIn(0, s.size - 1)
        return s[i]
    }

    /** The composer's stream: 24 kHz UNPROCESSED mic, 40 ms reads, the tap's own 20 ms hop. */
    private fun tapFrames(gain: Double): List<AnalysisFrame> {
        val rate = 24000
        val tap = AudioAnalysisTap(rate, calibration = AuroraCalibration.UNPROCESSED, nowNs = { 0L })
        val pcm = syntheticSpeech(rate, rate * 4, gain)
        val out = ArrayList<AnalysisFrame>()
        val chunk = rate * 40 / 1000 * 2
        var i = 0
        while (i < pcm.size) {
            val len = minOf(chunk, pcm.size - i)
            tap.offer(pcm.copyOfRange(i, i + len), len) { f ->
                out += AnalysisFrame(f.sampleOffset, f.atNs, f.bands.copyOf(), f.bins.copyOf())
            }
            i += len
        }
        // Drop the first quarter: the analyser's warm-up and the bin reference's settle.
        return out.drop(out.size / 4)
    }

    private fun measureThroughTheRealPath(gain: Double): PathMeasurement {
        val frames = tapFrames(gain)
        val feed = WaveformFeed()
        val dt = 1f / 120f
        val hopSec = AudioAnalysisTap.HOP_MS / 1000f
        var acc = 0f
        var fi = 0
        var steps = 0
        val loudest = ArrayList<Float>()
        val segEq = ArrayList<Float>()
        val bandScratch = FloatArray(BANDS)
        val binScratch = FloatArray(BINS)
        while (fi < frames.size) {
            acc += dt
            while (acc >= hopSec && fi < frames.size) { acc -= hopSec; fi++ }
            if (fi >= frames.size) break
            val f = frames[fi]
            feed.step(
                dt,
                listOf(AuroraVoice(AuroraSpeaker.HUMAN, f.bands)),
                listOf(AuroraBins(AuroraSpeaker.HUMAN, f.bins)),
            )
            steps++
            if (steps < 60) continue
            var m = 0f
            for (k in 0 until BINS) {
                val v = feed.bin(AuroraSpeaker.HUMAN, k)
                binScratch[k] = v
                if (v > m) m = v
            }
            for (b in 0 until BANDS) bandScratch[b] = feed.band(AuroraSpeaker.HUMAN, b)
            loudest += m
            var tallest = 0f
            for (c in 0 until 24) {
                val d = Aurora.restBlend(
                    0f, Aurora.displayCurve(SegmentedEqMath.columnDrive(binScratch, bandScratch, c, 24)),
                )
                if (d > tallest) tallest = d
            }
            segEq += tallest
        }
        return PathMeasurement(pct(loudest, 90.0), pct(segEq, 90.0))
    }

    /**
     * A voiced vowel: a glottal harmonic stack shaped by three formants and modulated at a 4 Hz
     * syllable rate. Harmonics rather than four sine probes, because the point of the measurement is
     * a 24-bin LOG spectrum and four sines would light four bins and say nothing about the rest.
     */
    private fun syntheticSpeech(rate: Int, samples: Int, gain: Double): ByteArray {
        val b = ByteArray(samples * 2)
        val f0 = 120.0
        for (j in 0 until samples) {
            val t = j.toDouble() / rate
            val syllable = 0.15 + 0.85 * (0.5 - 0.5 * cos(2 * PI * 4.0 * t))
            var v = 0.0
            var h = 1
            while (h * f0 < rate * 0.45) {
                val f = h * f0
                val a = (1.0 / h) * (
                    0.90 / (1.0 + sq((f - 500.0) / 160.0)) +
                        0.45 / (1.0 + sq((f - 1500.0) / 220.0)) +
                        0.25 / (1.0 + sq((f - 2600.0) / 320.0)) +
                        0.02
                    )
                v += a * sin(2 * PI * f * t)
                h++
            }
            val s = ((v * 0.55 * syllable * gain).coerceIn(-1.0, 1.0) * 32000).toInt()
            b[2 * j] = (s and 0xFF).toByte()
            b[2 * j + 1] = ((s shr 8) and 0xFF).toByte()
        }
        return b
    }

    private fun sq(x: Double) = x * x
}
