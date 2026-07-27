package com.aiblackbox.portal.ui.components.aurora

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin

/**
 * The four defects this analyser fixes relative to the AudioBands original it was ported from
 * (whisper-everywhere, 2026-07-18) each get a test that FAILS on the original behaviour:
 *
 *  D1 global singleton      -> per-stream instances (mic and AI playback run simultaneously)
 *  D2 hardcoded 16 kHz      -> sampleRate is a constructor parameter (3 of our 4 sources are 24 kHz)
 *  D3 warm-up counts CHUNKS -> warm-up counts elapsed TIME (the "takes a few seconds to catch up" bug)
 *  D4 AGC starts on a guess -> the reference is seeded from the first analysed chunk
 *
 * Plus the offline entry point, which has no warm-up at all because the whole clip is known
 * up front (the TTS playback path pre-decodes the file — see AudioEnvelope).
 */
class AuroraAnalyserTest {

    // ---------------------------------------------------------------- clock

    /**
     * Injectable clock. Tests must never sleep — warm-up is time-based (D3), so the only way
     * to test it deterministically is to drive time by hand.
     */
    private class FakeClock {
        var ns = 0L
            private set
        val supplier: () -> Long = { ns }
        fun advanceMs(ms: Double) { ns += (ms * 1_000_000.0).toLong() }
    }

    // --------------------------------------------------------------- signals

    /** PCM16-LE bytes for [durationMs] of [gen] (t in seconds) sampled at [sampleRate]. */
    private fun pcm16(sampleRate: Int, durationMs: Double, gen: (Double) -> Double): ByteArray {
        val n = (sampleRate * durationMs / 1000.0).toInt()
        val out = ByteArray(n * 2)
        for (i in 0 until n) {
            val v = (gen(i.toDouble() / sampleRate).coerceIn(-1.0, 1.0) * 32767.0).toInt()
            out[2 * i] = (v and 0xFF).toByte()
            out[2 * i + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    private fun tone(sampleRate: Int, durationMs: Double, freq: Double, amp: Double): ByteArray =
        pcm16(sampleRate, durationMs) { t -> amp * sin(2 * PI * freq * t) }

    /** 0.35..1.0 syllable-rate amplitude envelope — stays clear of the offline silence GATE. */
    private fun env(t: Double, hz: Double) = 0.35 + 0.65 * (0.5 + 0.5 * sin(2 * PI * hz * t))

    /**
     * One modulated tone per band, each with its own modulation rate — all four bands live.
     * The tones sit on probe frequencies so every band carries real energy rather than
     * leakage; a band that never clears its noise floor is reported flat (by design), which
     * would make the normalisation assertions vacuous.
     */
    private fun voiceLike(sampleRate: Int, durationMs: Double): ByteArray =
        pcm16(sampleRate, durationMs) { t ->
            0.18 * env(t, 1.3) * sin(2 * PI * 200 * t) +
                0.16 * env(t, 2.1) * sin(2 * PI * 800 * t) +
                0.12 * env(t, 3.3) * sin(2 * PI * 2600 * t) +
                0.08 * env(t, 4.7) * sin(2 * PI * 5000 * t)
        }

    private fun argmax(v: FloatArray): Int {
        var best = 0
        for (i in v.indices) if (v[i] > v[best]) best = i
        return best
    }

    /** Speech-level probe tone: band 1 energy lands between PEAK_FLOOR[1] and PEAK_INIT[1]. */
    private fun speechChunk(sampleRate: Int = 16000, chunkMs: Double = 32.0) =
        tone(sampleRate, chunkMs, 800.0, 0.03)

    // ------------------------------------------------------- D1 independence

    @Test
    fun `each analyser keeps its own auto-gain — one stream cannot rescale another`() {
        val quiet = speechChunk()
        val loud = tone(16000, 32.0, 800.0, 0.6)

        // Guard: the two streams really are 20x apart, so a shared AGC would visibly bite.
        val rq = FloatArray(AuroraAnalyser.BANDS)
        val rl = FloatArray(AuroraAnalyser.BANDS)
        AuroraAnalyser(16000, FakeClock().supplier).analyze(quiet, quiet.size, rq)
        AuroraAnalyser(16000, FakeClock().supplier).analyze(loud, loud.size, rl)
        assertTrue("guard: loud chunk must dwarf the quiet one", rl[1] > rq[1] * 8f)

        // Baseline: the quiet stream analysed on its own.
        val soloClock = FakeClock()
        val solo = AuroraAnalyser(16000, soloClock.supplier)
        val soloOut = FloatArray(20)
        repeat(20) {
            soloOut[it] = solo.analyze(quiet, quiet.size)[1]
            soloClock.advanceMs(32.0)
        }

        // Same quiet stream, but a loud stream is being analysed at the same time — this is the
        // red(human mic) + blue(AI playback) case that made the original's shared state a bug.
        val humanClock = FakeClock()
        val aiClock = FakeClock()
        val human = AuroraAnalyser(16000, humanClock.supplier)
        val ai = AuroraAnalyser(16000, aiClock.supplier)
        val duetOut = FloatArray(20)
        repeat(20) {
            ai.analyze(loud, loud.size)
            duetOut[it] = human.analyze(quiet, quiet.size)[1]
            aiClock.advanceMs(32.0)
            humanClock.advanceMs(32.0)
        }

        assertArrayEquals(soloOut, duetOut, 1e-6f)
    }

    // -------------------------------------------------------- D2 sample rate

    @Test
    fun `a 5 kHz tone lands in the sibilance band at both 16 kHz and 24 kHz`() {
        val raw16 = FloatArray(AuroraAnalyser.BANDS)
        val raw24 = FloatArray(AuroraAnalyser.BANDS)
        val t16 = tone(16000, 32.0, 5000.0, 0.2)
        val t24 = tone(24000, 32.0, 5000.0, 0.2)
        AuroraAnalyser(16000, FakeClock().supplier).analyze(t16, t16.size, raw16)
        AuroraAnalyser(24000, FakeClock().supplier).analyze(t24, t24.size, raw24)

        assertEquals("16 kHz band", 3, argmax(raw16))
        assertEquals("24 kHz band", 3, argmax(raw24))
        // Same tone, same duration, same level -> same reported energy regardless of rate.
        assertEquals(raw16[3], raw24[3], raw16[3] * 0.2f)
    }

    @Test
    fun `analysing 24 kHz audio as if it were 16 kHz mis-probes it`() {
        // This is exactly what the original did to our composer mic and AI playback streams.
        val t24 = tone(24000, 32.0, 5000.0, 0.2)
        val correct = FloatArray(AuroraAnalyser.BANDS)
        val wrong = FloatArray(AuroraAnalyser.BANDS)
        AuroraAnalyser(24000, FakeClock().supplier).analyze(t24, t24.size, correct)
        AuroraAnalyser(16000, FakeClock().supplier).analyze(t24, t24.size, wrong)
        assertTrue(
            "a 1.5x rate error must visibly break the band read (correct=${correct[3]} wrong=${wrong[3]})",
            wrong[3] < correct[3] * 0.5f,
        )
    }

    @Test
    fun `each probe pair claims its own band`() {
        for (b in 0 until AuroraAnalyser.BANDS) {
            val f = AuroraAnalyser.PROBES[b][0].toDouble()
            val raw = FloatArray(AuroraAnalyser.BANDS)
            val chunk = tone(16000, 32.0, f, 0.2)
            AuroraAnalyser(16000, FakeClock().supplier).analyze(chunk, chunk.size, raw)
            assertEquals("a ${f.toInt()} Hz tone must dominate band $b", b, argmax(raw))
        }
    }

    @Test
    fun `probes above Nyquist are dropped instead of aliasing`() {
        // mu-law telephony TTS is 8 kHz (see CLAUDE.md): nothing exists above 4 kHz there, so
        // the 5 kHz / 6.8 kHz sibilance probes must read zero rather than fold a vowel into
        // the band and make the ribbon sparkle at made-up content.
        val chunk = tone(8000, 32.0, 700.0, 0.3)
        val raw = FloatArray(AuroraAnalyser.BANDS)
        AuroraAnalyser(8000, FakeClock().supplier).analyze(chunk, chunk.size, raw)
        assertEquals(0f, raw[3], 0f)
        assertTrue("the 700 Hz vowel must still register", raw[1] > 0f)
    }

    // --------------------------------------------------------- D3 warm-up

    private class Fed(val analyser: AuroraAnalyser, val chunks: Int, val out: FloatArray)

    /**
     * Feed [chunkMs] chunks until exactly [totalMs] of session time has elapsed. The final step
     * is shortened so both cadences land a chunk ON [totalMs] — real capture jitters like this
     * anyway, and it lets two cadences be compared at the same instant.
     */
    private fun feedFor(chunkMs: Double, totalMs: Double, sampleRate: Int = 16000): Fed {
        val clock = FakeClock()
        val analyser = AuroraAnalyser(sampleRate, clock.supplier)
        val chunk = speechChunk(sampleRate, chunkMs)
        var t = 0.0
        var n = 0
        var out = FloatArray(AuroraAnalyser.BANDS)
        while (true) {
            out = analyser.analyze(chunk, chunk.size)
            n++
            if (t >= totalMs) break
            val step = minOf(chunkMs, totalMs - t)
            t += step
            clock.advanceMs(step)
        }
        return Fed(analyser, n, out)
    }

    @Test
    fun `warm-up finishes on elapsed time, not on chunk count`() {
        val fast = feedFor(chunkMs = 32.0, totalMs = AuroraAnalyser.WARMUP_MS.toDouble())
        val slow = feedFor(chunkMs = 100.0, totalMs = AuroraAnalyser.WARMUP_MS.toDouble())

        // The original scaled by THIS number, which is why a 100 ms feed took ~3x longer to
        // reach full scale than a 32 ms feed off the identical constant.
        assertNotEquals(fast.chunks, slow.chunks)

        assertEquals("32 ms cadence", 1f, fast.analyser.warmScale, 1e-6f)
        assertEquals("100 ms cadence", 1f, slow.analyser.warmScale, 1e-6f)
    }

    @Test
    fun `both cadences share the same warm scale at the same elapsed time`() {
        val half = (AuroraAnalyser.WARMUP_MS / 2f).toDouble()
        val fast = feedFor(chunkMs = 32.0, totalMs = half)
        val slow = feedFor(chunkMs = 100.0, totalMs = half)

        val expected = AuroraAnalyser.WARM_FLOOR + (1f - AuroraAnalyser.WARM_FLOOR) * 0.5f
        assertEquals(expected, fast.analyser.warmScale, 1e-5f)
        assertEquals(expected, slow.analyser.warmScale, 1e-5f)
        // ...and it shows up in the output the renderer actually consumes.
        assertEquals(fast.out[1], slow.out[1], 0.01f)
    }

    @Test
    fun `the first chunk of a session starts at the warm-up floor`() {
        val a = AuroraAnalyser(16000, FakeClock().supplier)
        val chunk = speechChunk()
        a.analyze(chunk, chunk.size)
        assertEquals(AuroraAnalyser.WARM_FLOOR, a.warmScale, 1e-6f)
    }

    @Test
    fun `a gap longer than a second starts a new session and re-ramps`() {
        val clock = FakeClock()
        val a = AuroraAnalyser(16000, clock.supplier)
        val chunk = speechChunk()
        repeat(30) { a.analyze(chunk, chunk.size); clock.advanceMs(32.0) }
        assertEquals(1f, a.warmScale, 1e-6f)

        clock.advanceMs(1500.0)
        a.analyze(chunk, chunk.size)
        assertEquals(AuroraAnalyser.WARM_FLOOR, a.warmScale, 1e-6f)
    }

    @Test
    fun `a short gap does not restart the warm-up`() {
        val clock = FakeClock()
        val a = AuroraAnalyser(16000, clock.supplier)
        val chunk = speechChunk()
        repeat(30) { a.analyze(chunk, chunk.size); clock.advanceMs(32.0) }
        clock.advanceMs(900.0)
        a.analyze(chunk, chunk.size)
        assertEquals(1f, a.warmScale, 1e-6f)
    }

    // ------------------------------------------------------------- D4 seeding

    @Test
    fun `the first chunk seeds the gain reference so quiet speech is already full scale`() {
        val chunk = speechChunk()
        val raw = FloatArray(AuroraAnalyser.BANDS)
        val out = AuroraAnalyser(16000, FakeClock().supplier).analyze(chunk, chunk.size, raw)

        assertTrue(
            "guard: the probe must sit BELOW the cold-start prior (raw=${raw[1]})",
            raw[1] < AuroraAnalyser.PEAK_INIT[1],
        )
        assertTrue("guard: ...and above the noise floor", raw[1] > AuroraAnalyser.PEAK_FLOOR[1])

        // Nothing but the warm-up ramp may attenuate the very first chunk. The original scaled
        // this by raw/PEAK_INIT as well, which is where "it takes a few seconds to catch up"
        // came from on top of the ramp.
        assertEquals(AuroraAnalyser.WARM_FLOOR, out[1], 1e-4f)
    }

    @Test
    fun `a loud first chunk does not leave the rest of the session collapsed`() {
        val speech = speechChunk()
        val slam = tone(16000, 32.0, 800.0, 0.9)
        val chunks = 25 // 768 ms — past the warm-up window

        fun run(first: ByteArray): FloatArray {
            val clock = FakeClock()
            val a = AuroraAnalyser(16000, clock.supplier)
            return FloatArray(chunks) { i ->
                val pcm = if (i == 0) first else speech
                val v = a.analyze(pcm, pcm.size)[1]
                clock.advanceMs(32.0)
                v
            }
        }

        val slammed = run(slam)
        val clean = run(speech)

        // Collapse first — it is the defect. (Assertion order matters here: the "reads hot"
        // check below also fails on the old behaviour, and if it ran first it would mask
        // whether the collapse assertions have any teeth at all.)
        // A transient may compress the next few frames, but never to a dead ribbon...
        for (i in 1 until chunks) {
            assertTrue(
                "frame $i collapsed (${slammed[i]} vs ${clean[i]})",
                slammed[i] >= clean[i] * 0.35f,
            )
        }
        // ...and by the end of the warm-up window the reference has re-acquired the real level.
        assertTrue(
            "still collapsed at the end of warm-up (${slammed[chunks - 1]} vs ${clean[chunks - 1]})",
            slammed[chunks - 1] >= clean[chunks - 1] * 0.9f,
        )
        // The slam is loud, so it is entitled to peg the ribbon on its own frame — seeding
        // means the reference is already right for it instead of lagging a few frames behind.
        assertEquals("the slam itself must read hot", 1f, slammed[0], 1e-6f)
    }

    @Test
    fun `a silent first chunk cannot seed the reference to zero`() {
        val clock = FakeClock()
        val a = AuroraAnalyser(16000, clock.supplier)
        val silence = ByteArray(1024)
        val out0 = a.analyze(silence, silence.size)
        for (v in out0) assertEquals(0f, v, 1e-6f)

        // A reference of ~0 would make the next chunk — any chunk — read fully saturated.
        clock.advanceMs(32.0)
        val whisper = tone(16000, 32.0, 800.0, 0.004)
        val out1 = a.analyze(whisper, whisper.size)
        assertTrue("a whisper after silence must not saturate (${out1[1]})", out1[1] < 1f)
    }

    @Test
    fun `reset returns the analyser to its cold-start state`() {
        val clock = FakeClock()
        val a = AuroraAnalyser(16000, clock.supplier)
        val chunk = speechChunk()
        repeat(30) { a.analyze(chunk, chunk.size); clock.advanceMs(32.0) }
        assertEquals(1f, a.warmScale, 1e-6f)

        a.reset()
        assertEquals(AuroraAnalyser.WARM_FLOOR, a.warmScale, 1e-6f)
        clock.advanceMs(32.0)
        a.analyze(chunk, chunk.size)
        assertEquals(AuroraAnalyser.WARM_FLOOR, a.warmScale, 1e-6f)
    }

    @Test
    fun `chunks too short to probe are ignored without disturbing the session`() {
        val clock = FakeClock()
        val a = AuroraAnalyser(16000, clock.supplier)
        val chunk = speechChunk()
        repeat(30) { a.analyze(chunk, chunk.size); clock.advanceMs(32.0) }
        val warm = a.warmScale

        val runt = ByteArray(40) // 20 samples
        val out = a.analyze(runt, runt.size)
        for (v in out) assertEquals(0f, v, 0f)
        assertEquals("a runt chunk must not re-ramp the session", warm, a.warmScale, 1e-6f)
    }

    @Test
    fun `an over-long length is clamped instead of crashing the audio thread`() {
        // A stale byte count after a short read must not take the mic reader down.
        val chunk = speechChunk()
        val out = AuroraAnalyser(16000, FakeClock().supplier).analyze(chunk, chunk.size * 4)
        assertTrue("the audio it did have must still be analysed", out[1] > 0f)
    }

    // ------------------------------------------------------------- offline

    @Test
    fun `offline analysis is deterministic`() {
        val pcm = voiceLike(16000, 1000.0)
        val (a, aw) = AuroraAnalyser.analyzeOffline(pcm, 16000)
        val (b, bw) = AuroraAnalyser.analyzeOffline(pcm, 16000)
        assertEquals(aw, bw)
        assertEquals(a.size, b.size)
        assertTrue("expected ~50 windows for 1 s at 20 ms, got ${a.size}", a.size >= 49)
        for (w in a.indices) assertArrayEquals("window $w", a[w], b[w], 0f)
    }

    @Test
    fun `offline envelopes match at 16 kHz and 24 kHz`() {
        val (at16, _) = AuroraAnalyser.analyzeOffline(voiceLike(16000, 1000.0), 16000)
        val (at24, _) = AuroraAnalyser.analyzeOffline(voiceLike(24000, 1000.0), 24000)
        assertEquals(at16.size, at24.size)
        for (w in at16.indices) {
            for (b in 0 until AuroraAnalyser.BANDS) {
                assertEquals(
                    "window $w band $b (16k=${at16[w][b]} 24k=${at24[w][b]})",
                    at16[w][b], at24[w][b], 0.06f,
                )
            }
        }
    }

    @Test
    fun `offline analysis has no warm-up — the first window is already full scale`() {
        // Loudest moment at the very start: the live path must ramp into it, the offline path
        // must not. This is the TTS playback lag, deleted rather than shortened. Same bytes
        // through both paths, so the ONLY difference measured here is the warm-up.
        val pcm = pcm16(16000, 500.0) { t ->
            (if (t < 0.1) 0.03 else 0.005) * sin(2 * PI * 800 * t)
        }
        val (windows, windowMs) = AuroraAnalyser.analyzeOffline(pcm, 16000)
        assertEquals(AuroraAnalyser.OFFLINE_WINDOW_MS, windowMs)
        assertEquals("first window must already be at full scale", 1f, windows[0][1], 0.03f)

        val live = AuroraAnalyser(16000, FakeClock().supplier)
        val firstChunk = pcm.copyOfRange(0, 1024)
        assertEquals(
            "the live path ramps from the warm floor — that is the contrast",
            AuroraAnalyser.WARM_FLOOR, live.analyze(firstChunk, firstChunk.size)[1], 1e-3f,
        )
    }

    @Test
    fun `offline analysis reports digital silence as flat zero`() {
        val silence = ByteArray(16000 * 2)
        val (windows, _) = AuroraAnalyser.analyzeOffline(silence, 16000)
        assertTrue(windows.isNotEmpty())
        for (w in windows.indices) {
            for (b in 0 until AuroraAnalyser.BANDS) assertEquals("window $w band $b", 0f, windows[w][b], 0f)
        }
    }

    @Test
    fun `offline analysis normalises each band over the whole clip`() {
        val pcm = voiceLike(16000, 1000.0)
        val (windows, _) = AuroraAnalyser.analyzeOffline(pcm, 16000)
        for (b in 0 until AuroraAnalyser.BANDS) {
            var max = 0f
            var min = 1f
            for (w in windows.indices) {
                if (windows[w][b] > max) max = windows[w][b]
                if (windows[w][b] < min) min = windows[w][b]
            }
            assertEquals("band $b must reach full scale somewhere in the clip", 1f, max, 1e-3f)
            assertTrue("band $b must also breathe below full scale (min=$min)", min < 0.9f)
        }
    }

    @Test
    fun `offline analysis accepts short PCM without crashing`() {
        val (windows, _) = AuroraAnalyser.analyzeOffline(ByteArray(20), 16000)
        assertEquals(0, windows.size)
    }

    /**
     * A FIVE-MINUTE 44.1 kHz clip must still produce one band row per 20 ms window, all the way to
     * the last one.
     *
     * That is not a hypothetical size: `elevenlabs_music` writes songs of up to five minutes and
     * they play through the same AudioPlayerBar as a two-second TTS reply. An earlier revision
     * buffered the clip's PCM to analyse it and capped that buffer at 4M samples, so anything past
     * ~1.6 min at this rate silently lost its bands and fell back to one RMS value driving all four
     * sheets. The cap is gone because the analysis holds WINDOWS, not samples — this test is what
     * fails if a length limit is ever reintroduced.
     *
     * Fed one sample at a time through the streaming entry point precisely because that proves no
     * whole-clip array is needed: 13.2M samples never exist together anywhere in this test.
     */
    @Test
    fun `a five-minute clip is analysed to its last window`() {
        val rate = 44100
        val windowMs = AuroraAnalyser.OFFLINE_WINDOW_MS
        val perWindow = rate * windowMs / 1000
        val total = rate * 300

        val stream = AuroraOfflineBands(rate, windowMs)
        // A 630 Hz square wave (integer half-period at this rate, so every window is identical) —
        // cheap to synthesise 13.2M times and it lands squarely in band 1's 500-800 Hz probes.
        val hi: Short = 8000
        val lo: Short = -8000
        for (i in 0 until total) stream.add(if ((i / 35) % 2 == 0) hi else lo)
        val windows = stream.finish()

        assertEquals("windows were dropped", total / perWindow, windows.size)
        assertTrue(
            "the last window came back empty - the tail of the clip was not analysed",
            windows.last()[1] > 0.5f,
        )
    }
}
