package com.aiblackbox.portal.ui.components.aurora

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin

/**
 * [AudioAnalysisTap] — the seam that turns a live PCM16 stream into analysis FRAMES on a schedule
 * of its own, carrying the legacy four bands and the new 24 bins together.
 *
 * Two properties are load-bearing and everything below serves them:
 *
 *   PARITY. The band channel must be what [AuroraAnalyser] already produces, to the last bit of a
 *   1e-6 comparison, for the equivalent window — otherwise the tap is a re-tune of the shipped
 *   ribbon wearing a plumbing change's clothes.
 *
 *   DELIVERY INDEPENDENCE. `AudioRecord.getMinBufferSize` is the device's decision, not ours, and
 *   the socket frames on the playback path are the server's; before the tap, that number set the
 *   analysis window AND the analysis rate. Frame count, sample offsets and every published number
 *   must therefore be IDENTICAL whether the same audio arrives 512 samples at a time or 4096.
 */
class AudioAnalysisTapTest {

    /** One published frame, copied out — the tap's own buffers move on. */
    private class Cap(
        val offset: Long,
        val atNs: Long,
        val bands: FloatArray,
        val bins: FloatArray,
    )

    /**
     * Deterministic speech-shaped PCM16-LE: four partials under a slow swell, so the analyser's
     * auto-gain genuinely moves across a run instead of sitting on a constant.
     *
     * @param from sample index the run starts at, so a stream can be generated in pieces and still
     *   be one continuous signal.
     */
    private fun signal(rate: Int, samples: Int, from: Int = 0): ByteArray {
        val b = ByteArray(samples * 2)
        for (j in 0 until samples) {
            val t = (from + j).toDouble() / rate
            val env = 0.25 + 0.7 * (0.5 - 0.5 * cos(2 * PI * t / 0.8))
            val v = env * (
                0.60 * sin(2 * PI * 180 * t) +
                    0.30 * sin(2 * PI * 700 * t) +
                    0.15 * sin(2 * PI * 2200 * t) +
                    0.05 * sin(2 * PI * 5200 * t)
                )
            write(b, j, (v * 26000).toInt())
        }
        return b
    }

    private fun tone(rate: Int, samples: Int, freq: Double, amp: Double = 0.6): ByteArray {
        val b = ByteArray(samples * 2)
        for (i in 0 until samples) {
            write(b, i, (amp * 32000 * sin(2 * PI * freq * i / rate)).toInt())
        }
        return b
    }

    private fun write(b: ByteArray, i: Int, value: Int) {
        val s = value.coerceIn(-32768, 32767)
        b[2 * i] = (s and 0xFF).toByte()
        b[2 * i + 1] = ((s shr 8) and 0xFF).toByte()
    }

    /** The SAME decode [AuroraAnalyser.sampleAt] performs — restated here to be independent of it. */
    private fun sample(pcm: ByteArray, i: Int): Float =
        ((pcm[2 * i + 1].toInt() shl 8) or (pcm[2 * i].toInt() and 0xFF)) / 32768f

    private fun collect(tap: AudioAnalysisTap, pcm: ByteArray, chunkSamples: Int): List<Cap> {
        val out = ArrayList<Cap>()
        var i = 0
        while (i < pcm.size) {
            val len = minOf(chunkSamples * 2, pcm.size - i)
            tap.offer(pcm.copyOfRange(i, i + len), len) { f ->
                out += Cap(f.sampleOffset, f.atNs, f.bands.copyOf(), f.bins.copyOf())
            }
            i += len
        }
        return out
    }

    private fun argmax(v: FloatArray): Int {
        var best = 0
        for (i in v.indices) if (v[i] > v[best]) best = i
        return best
    }

    private fun loudest(v: FloatArray): Float {
        var m = 0f
        for (x in v) if (x > m) m = x
        return m
    }

    // ------------------------------------------------------------------- legacy parity

    /**
     * THE parity proof. Not one call compared against one call — the whole run, frame by frame,
     * against an independently driven [AuroraAnalyser] fed the same windows on the same clock. That
     * covers the DYNAMICS too (warm-up, seeding, the auto-gain's rise and release), which a
     * single-frame comparison would miss entirely: the tap's analyser is a stateful stream process
     * and the interesting way to break it is to change what "one step of the stream" means.
     */
    @Test
    fun `the band channel is exactly what AuroraAnalyser produces for the same window`() {
        val rate = 24000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val hop = tap.hopSamples
        val pcm = signal(rate, hop * 24)

        val frames = collect(tap, pcm, 512)
        assertEquals(24, frames.size)

        var refNs = 0L
        val ref = AuroraAnalyser(rate, nowNs = { refNs })
        for (k in frames.indices) {
            refNs = frames[k].atNs
            val window = pcm.copyOfRange(k * hop * 2, (k + 1) * hop * 2)
            val expect = ref.analyze(window, window.size)
            for (b in 0 until AuroraAnalyser.BANDS) {
                assertEquals("frame $k band $b", expect[b], frames[k].bands[b], 1e-6f)
            }
        }
        // Guard: a tap that published four zeros forever would pass the loop above.
        //
        // Stated against the ANALYSER'S OWN definition of an ordinary frame rather than a literal,
        // so that recalibrating the ribbon cannot silently turn this guard off. When the auto-gain
        // reference sits at the current level — which is what a steady signal produces — the
        // analyser's output is softKnee(1 / HEADROOM) by construction, so "lit" means the loudest
        // published frame reached at least half of that. A literal 0.3 lived here until the
        // 2026-08-11 recalibration raised HEADROOM to 4.5 and moved that operating point to 0.22,
        // at which point the number was measuring the calibration rather than the tap.
        val lit = AuroraAnalyser.softKnee(1f / AuroraAnalyser.HEADROOM) * 0.5f
        assertTrue(
            "the bands must actually light up (loudest ${frames.map { loudest(it.bands) }.maxOrNull()} vs $lit)",
            (frames.map { loudest(it.bands) }.maxOrNull() ?: 0f) > lit,
        )
    }

    /**
     * The band window is the HOP — a contiguous, non-overlapping partition of the stream, exactly
     * the shape the analyser has always been fed. Overlapping it would hand the same audio to a
     * stateful auto-gain twice and double-count energy into a reference that is supposed to track
     * time. (The FFT channel is stateless per frame and therefore free to overlap; it does.)
     */
    @Test
    fun `consecutive band windows tile the stream without overlap or gaps`() {
        val rate = 16000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val hop = tap.hopSamples
        val frames = collect(tap, signal(rate, hop * 10), 4096)

        assertEquals(10, frames.size)
        for (k in frames.indices) assertEquals((k + 1).toLong() * hop, frames[k].offset)
        assertEquals(hop.toLong(), tap.bandWindowSamples.toLong())
    }

    // -------------------------------------------------------- delivery independence

    @Test
    fun `the hop schedule is independent of the delivery chunk size`() {
        val rate = 16000
        val pcm = signal(rate, 8000)
        val runs = intArrayOf(512, 2048, 4096).map {
            collect(AudioAnalysisTap(rate, nowNs = { 0L }), pcm, it)
        }

        val a = runs[0]
        assertTrue("guard: the run must produce several frames", a.size >= 10)
        for (r in runs) {
            assertEquals("frame count", a.size, r.size)
            for (k in a.indices) {
                assertEquals("offset $k", a[k].offset, r[k].offset)
                assertEquals("clock $k", a[k].atNs, r[k].atNs)
                // Same window, same arithmetic, same order — this is exact, not approximate.
                assertArrayEquals("bands $k", a[k].bands, r[k].bands, 0f)
                assertArrayEquals("bins $k", a[k].bins, r[k].bins, 0f)
            }
        }
    }

    @Test
    fun `an arbitrary chunk size is the same stream as one big offer`() {
        val rate = 44100
        val pcm = signal(rate, 30000)
        val whole = collect(AudioAnalysisTap(rate, nowNs = { 0L }), pcm, 1 shl 20)
        for (chunk in intArrayOf(1, 333, 7777)) {
            val split = collect(AudioAnalysisTap(rate, nowNs = { 0L }), pcm, chunk)
            assertEquals("chunk $chunk frame count", whole.size, split.size)
            for (k in whole.indices) {
                assertEquals("chunk $chunk offset $k", whole[k].offset, split[k].offset)
                assertArrayEquals("chunk $chunk bands $k", whole[k].bands, split[k].bands, 0f)
                assertArrayEquals("chunk $chunk bins $k", whole[k].bins, split[k].bins, 0f)
            }
        }
    }

    // ------------------------------------------------------------------ reader hardening

    /**
     * `AudioRecord.read` hands back SHORT reads into a full-size buffer, so the bytes past
     * `lenBytes` are the PREVIOUS read's — live audio, plausible, and wrong. Consuming them would
     * corrupt the ring with a repeat of audio the stream already carried.
     */
    @Test
    fun `a short read consumes lenBytes and ignores the stale tail`() {
        val rate = 16000
        val hop = AudioAnalysisTap(rate).hopSamples
        val real = signal(rate, hop * 3)

        // The same audio in an over-sized buffer whose tail is loud garbage from a previous read.
        val padded = ByteArray(real.size + hop * 2)
        real.copyInto(padded)
        for (i in real.size / 2 until padded.size / 2) write(padded, i, if (i % 2 == 0) 32000 else -32000)

        val exact = collect(AudioAnalysisTap(rate, nowNs = { 0L }), real, 4096)
        val short = ArrayList<Cap>()
        AudioAnalysisTap(rate, nowNs = { 0L }).offer(padded, real.size) { f ->
            short += Cap(f.sampleOffset, f.atNs, f.bands.copyOf(), f.bins.copyOf())
        }

        assertEquals(exact.size, short.size)
        for (k in exact.indices) {
            assertArrayEquals("bands $k", exact[k].bands, short[k].bands, 0f)
            assertArrayEquals("bins $k", exact[k].bins, short[k].bins, 0f)
        }
    }

    @Test
    fun `an over-long lenBytes is clamped to the buffer instead of crashing`() {
        val rate = 16000
        val hop = AudioAnalysisTap(rate).hopSamples
        val pcm = signal(rate, hop * 3)

        val clamped = ArrayList<Cap>()
        AudioAnalysisTap(rate, nowNs = { 0L }).offer(pcm, pcm.size + 9999) { f ->
            clamped += Cap(f.sampleOffset, f.atNs, f.bands.copyOf(), f.bins.copyOf())
        }
        val honest = collect(AudioAnalysisTap(rate, nowNs = { 0L }), pcm, 1 shl 20)

        assertEquals(honest.size, clamped.size)
        for (k in honest.indices) assertArrayEquals(honest[k].bands, clamped[k].bands, 0f)
    }

    @Test
    fun `an odd or empty lenBytes never splits a sample`() {
        val rate = 16000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val hop = tap.hopSamples
        var frames = 0
        // Half a sample past a whole hop: the stray byte is dropped, the hop still fires.
        tap.offer(signal(rate, hop + 1), hop * 2 + 1) { frames++ }
        assertEquals(1, frames)
        // Degenerate lengths are silence, not a crash.
        tap.offer(ByteArray(0), 0) { frames++ }
        tap.offer(ByteArray(8), -4) { frames++ }
        tap.offer(ByteArray(1), 1) { frames++ }
        assertEquals(1, frames)
    }

    @Test
    fun `a run shorter than the hop publishes nothing and carries its remainder`() {
        val rate = 24000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val hop = tap.hopSamples
        var frames = 0
        var offset = 0L
        tap.offer(signal(rate, hop - 1), (hop - 1) * 2) { frames++ }
        assertEquals("no frame before the hop boundary", 0, frames)
        tap.offer(signal(rate, 1, from = hop - 1), 2) { f -> frames++; offset = f.sampleOffset }
        assertEquals(1, frames)
        assertEquals("the remainder carried across the offer", hop.toLong(), offset)
    }

    // -------------------------------------------------------------- the two channels

    @Test
    fun `both channels ride one frame at one sample offset`() {
        val rate = 48000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val frames = collect(tap, signal(rate, tap.hopSamples * 5), 1024)

        assertEquals(5, frames.size)
        for ((k, f) in frames.withIndex()) {
            assertEquals(AuroraAnalyser.BANDS, f.bands.size)
            assertEquals(AuroraAnalyser.BINS, f.bins.size)
            assertEquals((k + 1).toLong() * tap.hopSamples, f.offset)
            // One offset, one instant: the clock is the AUDIO's, derived from the offset.
            assertEquals(f.offset * 1_000_000_000L / rate, f.atNs)
        }
    }

    /**
     * The bin channel is a Hann-windowed FFT of the most recent [AudioAnalysisTap.fftSize] samples
     * — pinned by reconstructing that window from the source and running [AuroraSpectrum] over it
     * independently. The rectangular comparison is the teeth: degrade the tap's window and this
     * fails.
     */
    @Test
    fun `the bin channel is the Hann spectrum of the most recent window`() {
        val rate = 16000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val pcm = signal(rate, tap.hopSamples * 12)
        val last = collect(tap, pcm, 2048).last()

        val hann = AuroraSpectrum(rate)
        assertEquals(tap.fftSize, hann.fftSize)
        val start = (last.offset - hann.fftSize).toInt()
        val window = FloatArray(hann.fftSize) { i -> sample(pcm, start + i) }

        // The published bins are that spectrum divided by ONE number (the tap's bin normalisation is
        // a gain and nothing else — see AudioAnalysisTap's bin-channel note), so multiplying the
        // frame back by the divisor it reports must reproduce the spectrum. That is a stronger claim
        // than the equality it replaces: it pins the shape, the amplitude calibration behind it, AND
        // that the gain is shared across all 24 bins rather than applied per bin.
        val expected = hann.analyze(window).copyOf()
        val restored = FloatArray(AuroraAnalyser.BINS) { last.bins[it] * tap.binDivisor }
        assertArrayEquals(expected, restored, 1e-6f)

        val rect = AuroraSpectrum(rate, hann.fftSize, SpectrumWindow.RECTANGULAR)
        val rectBins = rect.analyze(window).copyOf()
        var maxDiff = 0f
        for (b in 0 until AuroraAnalyser.BINS) maxDiff = maxOf(maxDiff, abs(rectBins[b] - restored[b]))
        assertTrue("a rectangular window must be measurably different, was $maxDiff", maxDiff > 1e-3f)
    }

    /**
     * THE BIN CHANNEL'S GAIN, as the two properties a consumer draws geometry against.
     *
     * It shipped RAW, and measured through this tap the loudest bin of ordinary speech reached 0.064
     * of the 0..1 range a bar is drawn in (p90; typical 0.019) — unusable as geometry, not because
     * it was wrong but because it was small. The normalisation is one shared divisor per frame, so:
     *
     *  1. a frame at its own recent peak lands at exactly 1/[AudioAnalysisTap.BIN_HEADROOM] and
     *     NOTHING can exceed it — the reference takes any rise instantly, so the divisor is never
     *     smaller than the loudest bin it divides. The bound is arithmetic, not a clamp;
     *  2. the spectrum's shape is untouched (proved by the restore above).
     */
    @Test
    fun `the bin channel is normalised into a usable range with a ceiling it cannot pass`() {
        val rate = 16000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val pcm = signal(rate, tap.hopSamples * 20)
        val frames = collect(tap, pcm, 2048)

        val ceiling = 1f / AudioAnalysisTap.BIN_HEADROOM
        var loudest = 0f
        for (f in frames) {
            for (v in f.bins) {
                assertTrue("a bin read $v, past the ceiling $ceiling", v <= ceiling + 1e-6f)
                if (v > loudest) loudest = v
            }
        }
        assertEquals("audio this loud must REACH the ceiling, not approach it", ceiling, loudest, 1e-4f)
        // ...and it is a GAIN that gets it there: the raw spectrum this frame was divided down from
        // never reaches the ceiling on its own at any level the reference is allowed to floor.
        assertTrue(
            "the divisor ${tap.binDivisor} is not a gain — the channel is being attenuated",
            tap.binDivisor < 1f,
        )
    }

    /**
     * ...and QUIET content keeps its absolute level, which is the whole reason the reference has a
     * floor. Below [AudioAnalysisTap.BIN_REF_FLOOR] the normalisation stops adapting and becomes one
     * fixed multiplier, so two quiet signals 4x apart still draw 4x apart — a peak-normaliser
     * without that floor would draw room tone, a whisper and a shout at the same height.
     */
    @Test
    fun `below the reference floor the gain is fixed, so quiet content keeps its level`() {
        val rate = 16000
        fun loudestBinOf(amplitude: Double): Float {
            val tap = AudioAnalysisTap(rate, nowNs = { 0L })
            val bin = 12
            val freq = AuroraSpectrum(rate).let { it.fftBinCentreHz(it.fftBinFor(bin)).toDouble() }
            val pcm = tone(rate, tap.fftSize * 3, freq, amplitude)
            return collect(tap, pcm, 2048).last().bins.max()
        }

        // Both amplitudes sit below the floor (0.07), where the divisor is pinned.
        val quiet = loudestBinOf(0.01)
        val louder = loudestBinOf(0.04)
        assertTrue("guard: both probes must stay under the reference floor", louder < 1f / AudioAnalysisTap.BIN_HEADROOM)
        assertEquals(
            "4x the amplitude must draw 4x as tall below the floor (got $quiet and $louder)",
            4.0, (louder / quiet).toDouble(), 0.25,
        )
    }

    @Test
    fun `a tone lands in its mapped bin through the tap at every rate`() {
        // The mapping is AuroraSpectrum's (500 Hz -> bin 9); what is asserted here is that the tap
        // hands it the right samples at the right rate, which is the part the adapter can get wrong.
        for (rate in intArrayOf(16000, 24000, 44100, 48000)) {
            val tap = AudioAnalysisTap(rate, nowNs = { 0L })
            val pcm = tone(rate, tap.fftSize * 3, 500.0)
            val last = collect(tap, pcm, 512).last()
            assertEquals("rate $rate", 9, argmax(last.bins))
            assertTrue("rate $rate level ${last.bins[9]}", last.bins[9] > 0.4f)
        }
    }

    /**
     * The opening frames see a partly-filled window and MEASURE quieter in proportion, which is
     * [AuroraSpectrum]'s documented zero-padding rather than a defect to suppress: withholding
     * frames until the window filled would stall the ribbon for a window's worth of audio at the
     * start of every session, on top of the analyser's own warm-up ramp.
     *
     * The swell is measured on the RESTORED level (published x [AudioAnalysisTap.binDivisor]), not
     * on the published one, and that is not a dodge — it is where the property now lives. The bin
     * normalisation divides every frame by its own recent peak, and a window filling up is exactly
     * a rising peak, so the published channel deliberately hides the ramp: what a viewer sees at the
     * start of a session is the spectrum, not the FFT warming up. The tap's job here — hand the
     * spectrum a growing window and publish every frame — is unchanged, and this still fails if it
     * withholds a frame, pads with the wrong samples, or re-uses a stale window.
     */
    @Test
    fun `the opening frames are zero-padded and swell as the window fills`() {
        val rate = 16000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val bin = 12
        val freq = AuroraSpectrum(rate).let { it.fftBinCentreHz(it.fftBinFor(bin)).toDouble() }
        val frames = collect(tap, tone(rate, tap.hopSamples * 8, freq), 4096)

        val full = tap.fftSize / tap.hopSamples // frames until the window is first full
        assertTrue("guard: the window must span several hops", full >= 3)
        // The divisor is per-frame state and only the LAST one is readable, so the ramp is
        // reconstructed the way a consumer would have to: run the same growing windows through an
        // independent AuroraSpectrum. If the tap fed it anything else, the levels below diverge.
        val spectrum = AuroraSpectrum(rate)
        val pcm = tone(rate, tap.hopSamples * 8, freq)
        val raw = FloatArray(full + 2) { k ->
            val end = (k + 1) * tap.hopSamples
            val take = minOf(end, tap.fftSize)
            val window = FloatArray(tap.fftSize) { i ->
                if (i < take) sample(pcm, end - take + i) else 0f
            }
            spectrum.analyze(window)[bin]
        }
        for (k in 1 until full) {
            assertTrue(
                "frame $k (${raw[k]}) must measure louder than frame ${k - 1} (${raw[k - 1]})",
                raw[k] > raw[k - 1],
            )
        }
        // Once full, a steady tone reads steady — the window slides, the level does not. True of
        // both the measurement and what is published (the reference has nothing left to chase).
        assertEquals(raw[full], raw[full + 1], 1e-3f)
        assertEquals(frames[full].bins[bin], frames[full + 1].bins[bin], 1e-3f)
        // ...and it lands on the tone's own amplitude. The calibration promises an on-bin sine reads
        // what it is (AuroraSpectrumTest measures 0.80..1.02 of it, the narrow-bin floor included),
        // so this is where a tap that halved, rescaled or mis-decoded the PCM shows up.
        val amplitude = 0.6f * 32000f / 32768f
        assertTrue(
            "a full window must recover the tone's amplitude ($amplitude), was ${raw[full]}",
            raw[full] in (amplitude * 0.80f)..(amplitude * 1.03f),
        )
        // Every frame was published — the ramp is hidden by the gain, not by withholding.
        assertEquals("frames must not be withheld while the window fills", 8, frames.size)
    }

    // --------------------------------------------------------------- clock and session

    @Test
    fun `the analysis clock advances by exactly one hop of audio per frame`() {
        val rate = 44100
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        // Delivered in one lump: wall time does not move, and the frames must still be spaced.
        val frames = collect(tap, signal(rate, tap.hopSamples * 6), 1 shl 20)
        for (k in 1 until frames.size) {
            val expected = frames[k].offset * 1_000_000_000L / rate -
                frames[k - 1].offset * 1_000_000_000L / rate
            assertEquals(expected, frames[k].atNs - frames[k - 1].atNs)
        }
        assertTrue("a hop must be about 20 ms", frames[1].atNs - frames[0].atNs in 19_000_000L..21_000_000L)
    }

    /**
     * A DELIVERY stall (the mic held through the AI's turn, a paused playback drain) is invisible to
     * a clock derived from samples — no samples, no elapsed time. The tap therefore watches the wall
     * clock for exactly one thing: a gap long enough that [AuroraAnalyser] would have called it a new
     * session. Losing that would leave the previous speaker's auto-gain and a finished warm-up in
     * place for whoever speaks next.
     */
    @Test
    fun `a delivery stall re-anchors the audio clock so a new session still starts`() {
        val rate = 16000
        var wall = 0L
        val tap = AudioAnalysisTap(rate, nowNs = { wall })
        val hop = tap.hopSamples
        val hopNs = hop.toLong() * 1_000_000_000L / rate

        var last = FloatArray(AuroraAnalyser.BANDS)
        var n = 0
        // ~1.5 s of steady speech: the warm-up finishes and the gain settles.
        while (n < 75) {
            tap.offer(signal(rate, hop, from = n * hop), hop * 2) { f -> last = f.bands.copyOf() }
            n++
            wall += hopNs
        }
        // Guard, against the analyser's own operating point rather than a literal — see the same
        // reasoning on the band-parity test above. Was `> 0.2f`, which the 2026-08-11 recalibration
        // turned into a statement about HEADROOM instead of about the stall.
        val settled = loudest(last)
        assertTrue(
            "guard: the ribbon must be lit before the stall ($settled)",
            settled > AuroraAnalyser.softKnee(1f / AuroraAnalyser.HEADROOM) * 0.5f,
        )

        // Five seconds of nothing delivered, then the same audio resumes.
        wall += 5_000_000_000L
        var afterStall = FloatArray(AuroraAnalyser.BANDS)
        tap.offer(signal(rate, hop, from = n * hop), hop * 2) { f -> afterStall = f.bands.copyOf() }

        // THE CLAIM, stated as an EQUALITY against a brand-new tap fed the same resumed audio: if
        // the stall started a session then the previous speaker's auto-gain and finished warm-up
        // are gone, and there is nothing left for this frame to be but the first frame of a new
        // stream. Exact, to the bit, on all four bands — the same shape the reset test below uses.
        //
        // THIS REPLACED A LEVEL COMPARISON ("the frame after the stall must read below 0.75x the
        // settled level"), and it is worth recording why rather than repeating the mistake. A new
        // session does two opposing things at once: it restarts the warm-up, which ATTENUATES by
        // WARM_FLOOR, and it reseeds the gain reference from the current chunk, which under a large
        // HEADROOM LIFTS. Those two happened to leave a 25% net drop at the old calibration and
        // very nearly cancel at the 2026-08-11 one (measured 0.94x), so the old assertion had
        // quietly stopped being able to fail. Re-pointing it at a threshold that 0.94 clears would
        // have kept a test that tests nothing; asserting the state directly cannot go stale,
        // because it does not depend on any calibrated level at all.
        val resumed = AudioAnalysisTap(rate, nowNs = { wall })
        var freshFirst = FloatArray(AuroraAnalyser.BANDS)
        resumed.offer(signal(rate, hop, from = n * hop), hop * 2) { f -> freshFirst = f.bands.copyOf() }
        assertArrayEquals(
            "after a 5 s stall the tap must read exactly as a brand-new one does on the same audio",
            freshFirst,
            afterStall,
            0f,
        )

        // ...and the teeth: with no stall, the very next frame is NOT a fresh session — it carries
        // the settled reference and a finished warm-up, so it must NOT match the brand-new tap.
        var quiet = 0
        var loudRun = FloatArray(AuroraAnalyser.BANDS)
        val steady = AudioAnalysisTap(rate, nowNs = { wall })
        wall = 0L
        while (quiet < 76) {
            steady.offer(signal(rate, hop, from = quiet * hop), hop * 2) { f -> loudRun = f.bands.copyOf() }
            quiet++
            wall += hopNs
        }
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertNotEquals(
                "band $b: an uninterrupted stream must not re-warm (${loudRun.toList()})",
                freshFirst[b].toDouble(),
                loudRun[b].toDouble(),
                1e-4,
            )
        }
        assertTrue("guard: the un-stalled stream is still lit (${loudest(loudRun)})", loudest(loudRun) > 0f)
    }

    @Test
    fun `reset drops the ring, the schedule and the analyser session`() {
        val rate = 16000
        val tap = AudioAnalysisTap(rate, nowNs = { 0L })
        val hop = tap.hopSamples
        var frames = 0
        // Half a hop in, then reset: the remainder must NOT count toward the next frame.
        tap.offer(signal(rate, hop / 2), hop) { frames++ }
        tap.reset()
        tap.offer(signal(rate, hop / 2), hop) { frames++ }
        assertEquals("the carried remainder must have been dropped", 0, frames)

        var first = FloatArray(AuroraAnalyser.BANDS)
        tap.offer(signal(rate, hop / 2, from = hop / 2), hop) { f -> frames++; first = f.bands.copyOf() }
        assertEquals(1, frames)

        // A reset tap behaves like a brand-new one on the same audio.
        val fresh = AudioAnalysisTap(rate, nowNs = { 0L })
        var freshFirst = FloatArray(AuroraAnalyser.BANDS)
        fresh.offer(signal(rate, hop), hop * 2) { f -> freshFirst = f.bands.copyOf() }
        assertArrayEquals(freshFirst, first, 0f)
    }

    // ------------------------------------------------------------------------ shape

    @Test
    fun `the tap sizes itself from the rate, not from the caller`() {
        for (rate in intArrayOf(8000, 16000, 24000, 44100, 48000)) {
            val tap = AudioAnalysisTap(rate)
            assertEquals("rate $rate fft", AuroraSpectrum.fftSizeFor(rate), tap.fftSize)
            // ~20 ms of audio per frame, whatever the rate.
            assertEquals("rate $rate hop", Math.round(rate * 0.020f), tap.hopSamples)
            assertTrue("rate $rate must probe a usable window", tap.hopSamples >= AuroraAnalyser.MIN_SAMPLES)
        }
    }

    /**
     * D1's rule, inherited: the human's mic and the AI's playback are analysed AT THE SAME TIME,
     * and anything shared — the ring, the schedule, the auto-gain reference, the FFT scratch — lets
     * one stream rescale the other's ribbon mid-sentence.
     *
     * Asserted as INTERFERENCE, not as "the two readings differ": the auto-gain's whole job is to
     * normalise level away, so a loud tone and a quiet one legitimately converge on similar band
     * values. What must never happen is one tap's output CHANGING because another tap ran.
     */
    @Test
    fun `each tap is independent — one stream, one instance`() {
        val rate = 16000
        val hop = AudioAnalysisTap(rate).hopSamples
        val quiet = { k: Int -> tone(rate, hop, 200.0, 0.03) }
        val loud = { k: Int -> signal(rate, hop, from = k * hop) }

        val solo = ArrayList<Cap>()
        val alone = AudioAnalysisTap(rate, nowNs = { 0L })
        for (k in 0 until 12) {
            alone.offer(quiet(k), hop * 2) { f -> solo += Cap(f.sampleOffset, f.atNs, f.bands.copyOf(), f.bins.copyOf()) }
        }

        val shared = ArrayList<Cap>()
        val mic = AudioAnalysisTap(rate, nowNs = { 0L })
        val ai = AudioAnalysisTap(rate, nowNs = { 0L })
        for (k in 0 until 12) {
            mic.offer(quiet(k), hop * 2) { f -> shared += Cap(f.sampleOffset, f.atNs, f.bands.copyOf(), f.bins.copyOf()) }
            ai.offer(loud(k), hop * 2) { }
        }

        assertEquals(solo.size, shared.size)
        for (k in solo.indices) {
            assertArrayEquals("bands $k", solo[k].bands, shared[k].bands, 0f)
            assertArrayEquals("bins $k", solo[k].bins, shared[k].bins, 0f)
        }
    }

    // ── W32: debug rate meter, from the clock read the tap already makes ─────────────────

    @Test
    fun `the rate meter reports frames per wall second without any extra clock reads (W32)`() {
        var clock = 0L
        var reads = 0
        val reports = mutableListOf<String>()
        val tap = AudioAnalysisTap(
            24_000,
            nowNs = { reads++; clock },
            label = "composer-mic/UNPROCESSED",
            rateReport = { reports += it },
        )
        val chunk = ByteArray(2_400 * 2)            // 100 ms at 24 kHz, delivered in real time
        repeat(30) {
            tap.offer(chunk) {}
            clock += 100_000_000L
        }
        assertEquals("one clock read per offer, exactly as before the meter", 30, reads)
        assertTrue("expected at least one report over 3 s", reports.isNotEmpty())
        val first = reports.first()
        assertTrue(first, first.startsWith("composer-mic/UNPROCESSED:"))
        val fps = Regex("""([0-9]+\.[0-9]) frames/s""").find(first)!!.groupValues[1].toFloat()
        assertEquals(50f, fps, 0.5f)
    }

    @Test
    fun `a bursty reader shows up as the same audio rate over wall time`() {
        // Five hops arriving at once every 100 ms is still 50 frames per wall second; the meter
        // measures what the screen gets, not how the reader chunks it.
        var clock = 0L
        val reports = mutableListOf<String>()
        val tap = AudioAnalysisTap(16_000, nowNs = { clock }, label = "stt-mic", rateReport = { reports += it })
        val chunk = ByteArray(1_600 * 2)            // 100 ms at 16 kHz
        repeat(25) { tap.offer(chunk) {}; clock += 100_000_000L }
        val fps = Regex("""([0-9]+\.[0-9]) frames/s""").find(reports.first())!!.groupValues[1].toFloat()
        assertEquals(50f, fps, 0.5f)
    }

    @Test
    fun `an unlabelled tap never reports`() {
        var clock = 0L
        val reports = mutableListOf<String>()
        val tap = AudioAnalysisTap(24_000, nowNs = { clock }, rateReport = { reports += it })
        repeat(40) { tap.offer(ByteArray(2_400 * 2)) {}; clock += 100_000_000L }
        assertTrue(reports.isEmpty())
    }
}
