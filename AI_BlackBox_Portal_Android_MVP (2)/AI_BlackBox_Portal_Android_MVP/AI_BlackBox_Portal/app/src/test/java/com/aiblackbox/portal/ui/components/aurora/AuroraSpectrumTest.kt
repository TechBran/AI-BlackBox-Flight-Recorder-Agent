package com.aiblackbox.portal.ui.components.aurora

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * The spectrum core: a real FFT, a Hann window and a 24-bin log-frequency map.
 *
 * This is a PURE layer with no consumer — nothing here asserts anything about the ribbon. It exists
 * so a later milestone can drive bins from real spectral content instead of the four Goertzel
 * probes, and every claim that milestone will lean on is measured here first:
 *
 *   - the radix-2 butterflies actually compute a DFT (checked against a naive one, not against
 *     itself),
 *   - the Hann window is EARNED, not assumed — the leakage test below is the whole argument for it,
 *   - a tone lands in the same log bin at 16k / 24k / 44.1k / 48k, which is the property that lets
 *     one calibration serve every source we have,
 *   - the map is amplitude-calibrated, so a bin reading is a number with a meaning rather than an
 *     arbitrary scale that a consumer would have to re-tune per rate.
 */
class AuroraSpectrumTest {

    /** Every rate a real source hands us (see AuroraAnalyser's D2), and the map is defined at all. */
    private val rates = intArrayOf(16000, 24000, 44100, 48000)

    private fun tone(sampleRate: Int, n: Int, freq: Double, amp: Double = 1.0) =
        FloatArray(n) { i -> (amp * sin(2 * PI * freq * i / sampleRate)).toFloat() }

    private fun argmax(v: FloatArray): Int {
        var best = 0
        for (i in v.indices) if (v[i] > v[best]) best = i
        return best
    }

    private fun loudestOther(v: FloatArray, skip: Int): Float {
        var best = 0f
        for (i in v.indices) if (i != skip && v[i] > best) best = v[i]
        return best
    }

    // ------------------------------------------------------------------ the FFT itself

    /**
     * The butterflies against a naive O(N^2) DFT. A hand-rolled radix-2 transform is exactly the
     * kind of code that can be self-consistently wrong — bit-reversal off by a stage, a twiddle
     * with the wrong sign, the real-packing unpack step subtly mirrored — and every one of those
     * still produces a plausible-looking spectrum. The only honest check is an independent
     * implementation, so this is one, written the slow obvious way.
     */
    @Test
    fun `the radix-2 real FFT agrees with a naive DFT`() {
        val n = 256
        // Deterministic pseudo-noise: broadband, so every output bin is exercised, and no bin is
        // accidentally zero (which would make a sign error invisible).
        var seed = 12345L
        val x = FloatArray(n) {
            seed = seed * 6364136223846793005L + 1442695040888963407L
            ((seed shr 33).toInt() / 2.147483647e9).toFloat()
        }

        val fast = AuroraFft(n).magnitudes(x)
        assertEquals(n / 2 + 1, fast.size)

        for (k in 0..n / 2) {
            var re = 0.0
            var im = 0.0
            for (i in 0 until n) {
                val a = -2.0 * PI * k * i / n
                re += x[i] * cos(a)
                im += x[i] * sin(a)
            }
            val slow = sqrt(re * re + im * im).toFloat()
            assertEquals("bin $k", slow, fast[k], 1e-2f)
        }
    }

    @Test
    fun `the FFT rejects sizes it cannot transform`() {
        // Radix-2 only. A non-power-of-two would silently transform the wrong length.
        for (bad in intArrayOf(0, 1, 2, 100, 255, 1000)) {
            var threw = false
            try {
                AuroraFft(bad)
            } catch (e: IllegalArgumentException) {
                threw = true
            }
            assertTrue("size $bad must be rejected", threw)
        }
        AuroraFft(4)
        AuroraFft(1024)
    }

    // -------------------------------------------------------------- why Hann, measured

    /**
     * Fraction of total spectral power sitting more than [skirt] FFT bins away from [centreBin] —
     * i.e. how much of one tone's energy the window smears across the rest of the spectrum.
     */
    private fun farLeakage(mag: FloatArray, centreBin: Int, skirt: Int = 3): Double {
        var total = 0.0
        var far = 0.0
        for (k in mag.indices) {
            val p = mag[k].toDouble() * mag[k]
            total += p
            if (abs(k - centreBin) > skirt) far += p
        }
        return far / total
    }

    /**
     * THE ARGUMENT FOR THE HANN WINDOW, as a measurement rather than an assertion.
     *
     * A rectangular window is not inherently leaky — on a tone that sits exactly on an FFT bin it
     * is perfect, and the first half of this test shows that. The catch is that real audio is NEVER
     * on-bin: bin spacing here is 15.6 Hz at 16 kHz, and no voice, instrument or TTS sample obliges
     * by landing on a multiple of it. Half a bin off is the worst case and the ordinary one.
     *
     * There, truncating a sine mid-cycle makes the analysed frame discontinuous at its edges, and
     * the transform reports that step as broadband content: measured 5.8% of the tone's energy lands
     * more than three bins away, spread right across the spectrum. On a 24-bin LOG map that is not a
     * cosmetic blur — the top bins are wide and cheap to fill, so a strong low vowel would light the
     * sibilance end of the ribbon with energy that is not in the room. Hann tapers the frame to zero
     * at both edges, removing the discontinuity: the same tone leaks 0.007%, ~830x less.
     *
     * That is what buys the FFT path its right to exist over the Goertzel probes it sits beside,
     * which are single-bin and rectangular (AuroraAnalyser.goertzel) and would smear the same way if
     * they were ever asked to resolve 24 bins instead of 4.
     */
    @Test
    fun `Hann earns its place — an off-bin tone leaks two orders of magnitude less than rectangular`() {
        val rate = 16000
        val n = 1024
        val binHz = rate.toDouble() / n
        val centreBin = 100

        val hann = AuroraSpectrum(rate, n, SpectrumWindow.HANN)
        val rect = AuroraSpectrum(rate, n, SpectrumWindow.RECTANGULAR)

        // Control: exactly on a bin, where the rectangular window is at its best.
        val onBin = tone(rate, n, centreBin * binHz)
        val rectOn = farLeakage(rect.magnitudes(onBin), centreBin)
        val hannOn = farLeakage(hann.magnitudes(onBin), centreBin)
        assertTrue("rectangular must be clean ON bin (got $rectOn)", rectOn < 1e-4)
        assertTrue("Hann must be clean ON bin too (got $hannOn)", hannOn < 1e-4)

        // The real case: half a bin off, which is where every real signal lives.
        val offBin = tone(rate, n, (centreBin + 0.5) * binHz)
        val rectOff = farLeakage(rect.magnitudes(offBin), centreBin)
        val hannOff = farLeakage(hann.magnitudes(offBin), centreBin)

        assertTrue(
            "guard: the rectangular window must actually be shown failing off-bin (got $rectOff)",
            rectOff > 1e-2,
        )
        assertTrue(
            "off-bin leakage: rect=$rectOff hann=$hannOff (${rectOff / hannOff}x)",
            hannOff < rectOff / 100.0,
        )
        // ...and it is the OFF-BIN case that does it, not the window being bad everywhere.
        assertTrue("rectangular off-bin must be far worse than its own on-bin", rectOff > rectOn * 100)
    }

    // ---------------------------------------------------------------- the log map

    @Test
    fun `the map is 24 log-spaced bins and BANDS is untouched`() {
        // BINS is a NEW channel beside the four Goertzel bands, not a replacement for them.
        assertEquals(24, AuroraAnalyser.BINS)
        assertEquals(4, AuroraAnalyser.BANDS)
        assertNotEquals(AuroraAnalyser.BINS, AuroraAnalyser.BANDS)

        val edges = FloatArray(AuroraAnalyser.BINS + 1) { AuroraSpectrum.edgeHz(it) }
        assertEquals(AuroraSpectrum.MIN_HZ, edges.first(), 1e-3f)
        assertEquals(AuroraSpectrum.MAX_HZ, edges.last(), 1e-2f)

        // Log spacing means a CONSTANT ratio between neighbours — that is what makes each bin the
        // same musical interval wide, which is what a spectral display wants.
        val ratio = edges[1] / edges[0]
        for (i in 0 until AuroraAnalyser.BINS) {
            assertEquals("edge $i ratio", ratio, edges[i + 1] / edges[i], 1e-4f)
            assertTrue("edges must increase", edges[i + 1] > edges[i])
        }
        assertTrue("a 24-bin log map over ~6.5 octaves is ~1.2x per bin, got $ratio", ratio in 1.15f..1.25f)
    }

    /**
     * The top of the range is not a taste decision: it is the analyser's existing Nyquist rule
     * (AuroraAnalyser.NYQUIST_MARGIN) evaluated at the LOWEST rate the map has to be fully defined
     * at. Pin the derivation rather than the number, so re-measuring the margin moves both together
     * instead of leaving the map quietly probing above what the lowest rate can carry.
     */
    @Test
    fun `the top of the map is the analyser's own Nyquist rule at the lowest mapped rate`() {
        assertEquals(
            AuroraAnalyser.NYQUIST_MARGIN * AuroraSpectrum.MAP_RATE_HZ,
            AuroraSpectrum.MAX_HZ,
            1e-3f,
        )
        assertEquals(16000f, AuroraSpectrum.MAP_RATE_HZ, 0f)
    }

    /**
     * The property the exact tone-to-bin mapping below rests on: at every mapped rate, each of the
     * 24 log bins owns at least one FFT bin of its own. It is not free — the bottom bin is only
     * ~17 Hz wide, so it takes a window long enough to resolve that (see FFT_WINDOW_SEC). If a
     * future change shortens the window or lowers MIN_HZ, the low bins start sharing FFT bins,
     * neighbouring bins become indistinguishable, and this fails BEFORE the accuracy test does —
     * which is the point of stating it separately.
     */
    @Test
    fun `every log bin owns at least one FFT bin at every mapped rate`() {
        for (rate in rates) {
            val s = AuroraSpectrum(rate)
            for (b in 0 until AuroraAnalyser.BINS) {
                assertTrue(
                    "rate $rate bin $b (${AuroraSpectrum.edgeHz(b)}..${AuroraSpectrum.edgeHz(b + 1)} Hz) " +
                        "owns no FFT bin at ${"%.2f".format(s.fftResolutionHz)} Hz resolution",
                    s.fftBinCount(b) >= 1,
                )
            }
        }
    }

    @Test
    fun `the FFT size is derived from the rate so the window is the same length in TIME`() {
        for (rate in rates) {
            val s = AuroraSpectrum(rate)
            val windowSec = s.fftSize.toFloat() / rate
            assertTrue(
                "rate $rate -> ${s.fftSize} = ${"%.1f".format(windowSec * 1000)} ms",
                windowSec >= AuroraSpectrum.FFT_WINDOW_SEC && windowSec < AuroraSpectrum.FFT_WINDOW_SEC * 2,
            )
            assertEquals("must be a power of two", 0, s.fftSize and (s.fftSize - 1))
        }
    }

    // ------------------------------------------------------- tone-to-bin accuracy

    @Test
    fun `a tone at each bin centre lands in that bin, at every mapped rate`() {
        for (rate in rates) {
            val s = AuroraSpectrum(rate)
            for (b in 0 until AuroraAnalyser.BINS) {
                val f = AuroraSpectrum.centreHz(b).toDouble()
                val bins = s.analyze(tone(rate, s.fftSize, f, 0.5))
                assertEquals(AuroraAnalyser.BINS, bins.size)
                assertEquals("rate $rate: ${f.toInt()} Hz must land in bin $b", b, argmax(bins))
                // With teeth: the winner must be clearly the winner, not a coin flip against its
                // neighbour. 1.3x is set against a measured worst case of 1.46x, which occurs in the
                // narrow low bins where one Hann main lobe is wider than one log bin.
                val other = loudestOther(bins, b)
                assertTrue(
                    "rate $rate bin $b won by only ${bins[b] / other}x",
                    bins[b] > other * 1.3f,
                )
            }
        }
    }

    /**
     * The requirement stated directly: the SAME frequency is the SAME bin on every source we have.
     * Measured on the analyser's own probe frequencies, so the two channels describe the same
     * spectrum and a later consumer can reason about both at once.
     */
    @Test
    fun `one frequency maps to one bin whatever the sample rate`() {
        for (probe in AuroraAnalyser.PROBES.flatMap { it.toList() }) {
            val landed = rates.map { rate ->
                val s = AuroraSpectrum(rate)
                argmax(s.analyze(tone(rate, s.fftSize, probe.toDouble(), 0.5)))
            }
            assertEquals(
                "probe $probe Hz landed in different bins per rate: ${rates.toList()} -> $landed",
                1,
                landed.distinct().size,
            )
        }
    }

    /**
     * Amplitude calibration. A bin reading is `sqrt(band power / window ENBW)`, which makes a
     * full-scale sine read ~1.0 whatever the rate, the FFT size or the window — so a consumer can
     * be written against one operating point instead of a per-rate fudge factor. The floor is
     * 0.8165 (= sqrt(2/3)) and it is not slop: it is the narrow low bins, which are barely wider
     * than one FFT bin and therefore hold only two of the three FFT bins a Hann main lobe occupies.
     */
    @Test
    fun `a full-scale on-bin sine reads about one in every bin at every rate`() {
        for (rate in rates) {
            val s = AuroraSpectrum(rate)
            for (b in 0 until AuroraAnalyser.BINS) {
                val f = s.fftBinCentreHz(s.fftBinFor(b)).toDouble()
                val v = s.analyze(tone(rate, s.fftSize, f, 1.0))[b]
                assertTrue("rate $rate bin $b (${f.toInt()} Hz) read $v", v in 0.80f..1.02f)
            }
        }
    }

    @Test
    fun `the calibration does not depend on which window is used`() {
        val rate = 16000
        val hann = AuroraSpectrum(rate)
        val rect = AuroraSpectrum(rate, hann.fftSize, SpectrumWindow.RECTANGULAR)
        // A bin wide enough to hold a whole Hann main lobe — narrow bins clip it (see above), and
        // that is a resolution limit rather than a calibration one.
        val b = 20
        val f = hann.fftBinCentreHz(hann.fftBinFor(b)).toDouble()
        val x = tone(rate, hann.fftSize, f, 1.0)
        assertEquals(rect.analyze(x)[b], hann.analyze(x)[b], 1e-3f)
    }

    // ------------------------------------------------------------- rate edge cases

    /**
     * 8 kHz mu-law telephony TTS (see CLAUDE.md) carries nothing above 4 kHz, so the bins above the
     * Nyquist margin must read a flat ZERO rather than being fed folded-over vowel energy. Same rule
     * the Goertzel probes already apply — see `probes above Nyquist are dropped instead of aliasing`
     * in AuroraAnalyserTest — so a rate the map is not fully defined at loses the top of the ribbon
     * and nothing else.
     */
    @Test
    fun `bins above the Nyquist margin read zero instead of aliasing`() {
        val rate = 8000
        val s = AuroraSpectrum(rate)
        val bins = s.analyze(tone(rate, s.fftSize, 700.0, 0.3))

        var dropped = 0
        for (b in 0 until AuroraAnalyser.BINS) {
            if (AuroraSpectrum.edgeHz(b) >= AuroraAnalyser.NYQUIST_MARGIN * rate) {
                assertEquals("bin $b is above 8 kHz Nyquist", 0f, bins[b], 0f)
                assertEquals("bin $b must own no FFT bin", 0, s.fftBinCount(b))
                dropped++
            }
        }
        assertTrue("guard: 8 kHz must actually drop some bins", dropped >= 3)
        assertEquals("the 700 Hz tone must still register", 11, argmax(bins))
    }

    @Test
    fun `silence reads flat zero`() {
        val s = AuroraSpectrum(24000)
        for (v in s.analyze(FloatArray(s.fftSize))) assertEquals(0f, v, 0f)
    }

    @Test
    fun `a frame shorter than the window is zero-padded, not rejected`() {
        // A caller with a 40 ms mic read and a 64 ms window still gets a usable answer: quieter,
        // because the tone occupies only part of the window, but in the right bin.
        val rate = 16000
        val s = AuroraSpectrum(rate)
        val b = 12
        val f = AuroraSpectrum.centreHz(b).toDouble()
        val short = tone(rate, s.fftSize / 2, f, 1.0)
        val bins = s.analyze(short)
        assertEquals(b, argmax(bins))
        assertTrue("a half frame must read quieter than a full one (${bins[b]})", bins[b] < 0.9f)
        assertTrue("...but must not vanish", bins[b] > 0.3f)
    }

    @Test
    fun `an empty or negative-length frame is silence, not a crash`() {
        val s = AuroraSpectrum(16000)
        for (v in s.analyze(FloatArray(0))) assertEquals(0f, v, 0f)
        for (v in s.analyze(FloatArray(64), offset = 0, len = -5)) assertEquals(0f, v, 0f)
    }

    @Test
    fun `a frame longer than the window is analysed from its most recent samples`() {
        // A visualizer wants NOW, not the average of everything the caller has buffered.
        val rate = 16000
        val s = AuroraSpectrum(rate)
        val old = AuroraSpectrum.centreHz(6).toDouble()
        val recent = AuroraSpectrum.centreHz(18).toDouble()
        val long = FloatArray(s.fftSize * 3)
        val a = tone(rate, s.fftSize * 2, old, 1.0)
        val z = tone(rate, s.fftSize, recent, 1.0)
        a.copyInto(long, 0)
        z.copyInto(long, s.fftSize * 2)
        assertEquals(18, argmax(s.analyze(long)))
    }

    // --------------------------------------------------------------- allocation

    /**
     * The bin and magnitude arrays are OWNED by the instance and overwritten in place — this runs
     * per frame on an audio or frame thread, and a per-call FloatArray there is garbage the ribbon
     * pays for. Stated as a test because "returns the instance's own buffer" is a contract a caller
     * can get wrong (by holding the reference across frames), not an implementation detail.
     */
    @Test
    fun `analysis allocates nothing per call — the output buffers are instance-owned`() {
        val s = AuroraSpectrum(16000)
        val x = tone(16000, s.fftSize, 800.0, 0.5)
        assertSame(s.analyze(x), s.analyze(x))
        assertSame(s.magnitudes(x), s.magnitudes(x))

        // Reuse must not accumulate: the same input twice must give the same answer.
        val first = s.analyze(x).copyOf()
        val second = s.analyze(x)
        for (b in first.indices) assertEquals("bin $b", first[b], second[b], 0f)

        // ...and a different input must actually overwrite the old one.
        val quiet = tone(16000, s.fftSize, 800.0, 0.1)
        assertTrue("stale buffer", s.analyze(quiet)[12] < first[12] * 0.5f)
    }

    @Test
    fun `each spectrum instance is independent`() {
        // Same reason AuroraAnalyser is per-stream (D1): the mic and the AI playback are analysed
        // at the same time, and shared scratch would let one overwrite the other's frame.
        val mic = AuroraSpectrum(16000)
        val ai = AuroraSpectrum(16000)
        val low = tone(16000, mic.fftSize, AuroraSpectrum.centreHz(4).toDouble(), 0.5)
        val high = tone(16000, ai.fftSize, AuroraSpectrum.centreHz(20).toDouble(), 0.5)

        val solo = mic.analyze(low).copyOf()
        ai.analyze(high)
        val duet = mic.analyze(low)
        for (b in solo.indices) assertEquals("bin $b", solo[b], duet[b], 0f)
    }
}
