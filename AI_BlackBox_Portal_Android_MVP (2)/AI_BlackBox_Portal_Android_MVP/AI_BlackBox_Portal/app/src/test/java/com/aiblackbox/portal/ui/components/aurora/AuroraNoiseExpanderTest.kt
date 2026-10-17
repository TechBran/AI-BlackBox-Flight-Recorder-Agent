package com.aiblackbox.portal.ui.components.aurora

import com.aiblackbox.portal.data.voice.AuroraDynamicsHarness
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * THE DOWNWARD EXPANDER — what the live band channel does with a room that is not silent.
 *
 * THE DEFECT, in the operator's words after running v35 on a Fold: the ribbon "constantly has too
 * much going on". Measured, it was not a tuning miss but a missing STAGE. The live path had no noise
 * subtraction at all (the only gate in the file, in `normalizeOfflineBands`, belongs to the offline
 * player bar and the mic never reaches it), and the live denominator `max(peaks, noiseLive)*headroom`
 * DIVIDES by the floor without ever REMOVING it. A band sitting at exactly its own noise constant
 * therefore emitted 1/HEADROOM = 0.2222, which [Aurora.DISPLAY_GAMMA] draws at roughly HALF the
 * ribbon's height — permanently, for a room with nobody in it. Measured on the composer path
 * (16 kHz, unprocessed profile), LINEAR band output for pink room tone:
 *
 *     -50 dBFS  0.058 0.022 0.016 0.007
 *     -45 dBFS  0.103 0.040 0.028 0.012    <- and full-gain speech emitted 0.254 0.066 0.026 0.011
 *     -40 dBFS  0.183 0.070 0.050 0.022
 *
 * i.e. at -45 dBFS an empty room was LEVEL with speech on sheets 2 and 3, and at -40 it was louder.
 * This file is the requirement that fixed that, and the two halves of it are inseparable: a gate
 * that also kills quiet speech is a regression, not a fix, so every noise assertion here is paired
 * with a signal assertion that fails if the expander is turned up past the truth.
 *
 * THE ASSERTIONS ARE IN DRAWN HEIGHT — [Aurora.displayCurve] of the linear output above — because
 * "how much is on screen" is the question being asked, and they read the renderer's curve live so
 * they cannot drift from it. The tables in the prose stay linear for the opposite reason: they are
 * history, and history should not need re-measuring when the curve moves.
 */
class AuroraNoiseExpanderTest {

    // ------------------------------------------------------------------ signals

    /**
     * Paul Kellet pink filter over a fixed LCG, RMS-calibrated to [dbfs]. Room tone is pink, not
     * white, and it is deterministic here so a threshold means the same thing on every JVM forever.
     */
    private fun pink(n: Int, dbfs: Double, seed: Long = 0x1234_5EEDL): FloatArray {
        var rng = seed
        var b0 = 0.0; var b1 = 0.0; var b2 = 0.0; var b3 = 0.0; var b4 = 0.0; var b5 = 0.0; var b6 = 0.0
        val x = FloatArray(n) {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            val w = (((rng ushr 40).toInt() and 0xFFFFFF) / 8388608.0 - 1.0)
            b0 = 0.99886 * b0 + w * 0.0555179
            b1 = 0.99332 * b1 + w * 0.0750759
            b2 = 0.96900 * b2 + w * 0.1538520
            b3 = 0.86650 * b3 + w * 0.3104856
            b4 = 0.55000 * b4 + w * 0.5329522
            b5 = -0.7616 * b5 - w * 0.0168980
            val out = b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362
            b6 = w * 0.115926
            (out * 0.11).toFloat()
        }
        var acc = 0.0
        for (v in x) acc += v.toDouble() * v
        val g = (10.0.pow(dbfs / 20.0) / sqrt(acc / x.size)).toFloat()
        for (i in x.indices) x[i] = (x[i] * g).coerceIn(-1f, 1f)
        return x
    }

    private fun toPcm16(s: FloatArray, from: Int, len: Int): ByteArray {
        val b = ByteArray(len * 2)
        for (k in 0 until len) {
            val v = Math.round(s[from + k] * 32767f).coerceIn(-32768, 32767)
            b[2 * k] = (v and 0xFF).toByte()
            b[2 * k + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return b
    }

    // ------------------------------------------------------------------ the composer path

    private val rate = 16000
    private val readMs = 20f

    /**
     * Feed [samples] through a REAL [AudioAnalysisTap] the way the composer mic does (16 kHz,
     * 20 ms reads, the capture source's profile) and return the band frames from [fromSec] on —
     * late enough that every reference in the chain has had several time constants to settle.
     */
    private fun bands(
        samples: FloatArray,
        calibration: AuroraCalibration,
        fromSec: Double = 14.0,
    ): List<FloatArray> {
        val chunk = Math.round(rate * readMs / 1000f)
        var clock = 0L
        val tap = AudioAnalysisTap(rate, calibration = calibration, nowNs = { clock })
        val out = ArrayList<FloatArray>()
        val from = (fromSec * rate).toLong()
        var i = 0
        while (i < samples.size) {
            val len = minOf(chunk, samples.size - i)
            tap.offer(toPcm16(samples, i, len), len * 2) { f ->
                if (f.sampleOffset >= from) out.add(f.bands.copyOf())
            }
            clock += (readMs * 1_000_000f).toLong()
            i += len
        }
        return out
    }

    private fun p50(frames: List<FloatArray>, b: Int): Float {
        val s = frames.map { it[b] }.sorted()
        return s[Math.round(0.5 * (s.size - 1)).toInt().coerceIn(0, s.size - 1)]
    }

    /** Drawn height of the typical frame on sheet [b] — the number the eye is given. */
    private fun drawn(frames: List<FloatArray>, b: Int): Float = Aurora.displayCurve(p50(frames, b))

    private fun report(frames: List<FloatArray>): String =
        (0 until AuroraAnalyser.BANDS).joinToString(" ") { "%.4f".format(drawn(frames, it)) }

    // ================================================== 1. the room must go quiet

    /**
     * THE HEADLINE REQUIREMENT. Pink room tone at -45 dBFS — an ordinary quiet room through a bare
     * transducer — must be essentially invisible on EVERY sheet.
     *
     * 0.05 of full height is the threshold because [Aurora.BASELINE] is 0.06: below it the sheet is
     * inside the ribbon's own resting sliver, which is what "nothing is happening" looks like. All
     * four sheets used to sit well clear of it, the low one at a third of the ribbon.
     */
    @Test
    fun `pink room tone at -45 dBFS is invisible on every sheet`() {
        val frames = bands(pink((20.0 * rate).toInt(), -45.0), AuroraCalibration.UNPROCESSED)
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertTrue(
                "sheet $b draws ${drawn(frames, b)} through -45 dBFS room tone, must be < 0.05 " +
                    "(it emitted 0.103/0.040/0.028/0.012 linear before this change; " +
                    "drawn now: ${report(frames)})",
                drawn(frames, b) < 0.05f,
            )
        }
    }

    /** ...and quieter rooms cannot be louder than that one. */
    @Test
    fun `a quieter room draws less than a louder one, all the way down`() {
        val n = (20.0 * rate).toInt()
        val louder = bands(pink(n, -45.0), AuroraCalibration.UNPROCESSED)
        val quieter = bands(pink(n, -50.0), AuroraCalibration.UNPROCESSED)
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertTrue(
                "sheet $b: -50 dBFS draws ${drawn(quieter, b)}, -45 dBFS draws ${drawn(louder, b)}",
                drawn(quieter, b) <= drawn(louder, b) + 1e-6f,
            )
            assertTrue(
                "sheet $b draws ${drawn(quieter, b)} through -50 dBFS room tone, must be < 0.05",
                drawn(quieter, b) < 0.05f,
            )
        }
    }

    // ================================================== 2. digital silence, still exactly zero

    /**
     * Digital silence was already exactly 0.0000 and must stay there — an expander that leaks a
     * subtraction artefact into a stream of zero bytes would be a new defect, not a fix. Asserted on
     * BOTH profiles because they carry different floors to subtract.
     */
    @Test
    fun `digital silence is exactly zero on every band and both profiles`() {
        for (cal in listOf(AuroraCalibration.DEFAULT, AuroraCalibration.UNPROCESSED)) {
            val frames = bands(FloatArray((6.0 * rate).toInt()), cal, fromSec = 1.0)
            assertTrue("${cal.id}: no frames scored", frames.isNotEmpty())
            for (f in frames) {
                for (b in 0 until AuroraAnalyser.BANDS) assertEquals("${cal.id} band $b", 0f, f[b], 0f)
            }
        }
    }

    /**
     * Mic HISS, not room tone: white noise falls the other way across the spectrum, and the floors
     * this profile subtracts are shaped for both (see [AuroraCalibration.UNPROCESSED_NOISE_LIVE] —
     * the room wins the low band, the transducer's own hiss wins the top two). A pink-only floor
     * would leave sheet 3 lit at 0.0035 linear here — which is what it emitted before.
     */
    @Test
    fun `white hiss at -45 dBFS is invisible too`() {
        val frames = bands(white((20.0 * rate).toInt(), -45.0), AuroraCalibration.UNPROCESSED)
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertTrue(
                "sheet $b draws ${drawn(frames, b)} through -45 dBFS hiss, must be < 0.05 " +
                    "(it emitted 0.040/0.033/0.046/0.031 linear before this change; " +
                    "drawn now: ${report(frames)})",
                drawn(frames, b) < 0.05f,
            )
        }
    }

    /**
     * ...AND IT IS AN EXPANDER, NOT A GATE. A room LOUDER than the level the profile assumes must
     * still draw — otherwise the ribbon is lying about a room that really is noisy, and the next
     * report would be "it shows nothing at all". This is what pins the meaning of
     * [AuroraCalibration.UNPROCESSED_ROOM_TONE_DBFS]: at the assumed level the room renders nothing,
     * and every dB above it renders.
     */
    @Test
    fun `a room louder than the profile assumes still draws`() {
        val frames = bands(pink((20.0 * rate).toInt(), -35.0), AuroraCalibration.UNPROCESSED)
        assertTrue(
            "3 dB above the assumed room tone the low sheet draws ${drawn(frames, 0)}, " +
                "which is a gate, not an expander (${report(frames)})",
            drawn(frames, 0) > 0.15f,
        )
    }

    // ================================================== 3. and the voice must survive it

    /**
     * A voiced signal whose EVERY band clears its floor: one on-probe carrier per band, 4 Hz
     * syllables, 300 ms gaps of -50 dBFS noise between words.
     *
     * It exists because the shipped [AuroraDynamicsHarness.speechClip] cannot ask this question of
     * all four sheets. Measured raw, at full gain, that clip's bands sit at 7.3x / 0.30x / 0.12x /
     * 0.05x of their own noise floors — three of its four bands carry LESS energy than the room tone
     * the profile describes, which is exactly why -45 dBFS room tone drew as tall as it did on
     * sheets 2 and 3 before this change. No expander can keep content that is quieter than the
     * floor: the honest test of "does speech survive" needs speech that is above it.
     */
    private fun richVoiced(totalMs: Int, gain: Float): Pair<FloatArray, List<IntRange>> {
        val amps = doubleArrayOf(0.22, 0.16, 0.10, 0.09)
        val hz = doubleArrayOf(200.0, 800.0, 2600.0, 5000.0)
        val wordSamples = rate * 500 / 1000
        val silSamples = rate * 300 / 1000
        val out = ArrayList<Float>()
        val voiced = ArrayList<IntRange>()
        var t = 0
        var rng = 0x5EED1234L
        while (t * 1000 / rate < totalMs) {
            val start = t
            for (k in 0 until wordSamples) {
                val syl = (1.0 - kotlin.math.cos(2.0 * Math.PI * 4.0 * k / rate)) / 2.0
                var s = 0.0
                for (j in 0 until 4) s += amps[j] * kotlin.math.sin(2.0 * Math.PI * hz[j] * t / rate)
                out.add((s * syl * gain).coerceIn(-1.0, 1.0).toFloat())
                t++
            }
            voiced.add(start until t)
            for (k in 0 until silSamples) {
                rng = rng * 6364136223846793005L + 1442695040888963407L
                out.add((((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f) * 0.00316f)
                t++
            }
        }
        return out.toFloatArray() to voiced
    }

    /** Voiced frames only, through the composer path, on [cal]. */
    private fun voicedBands(
        clip: Pair<FloatArray, List<IntRange>>,
        cal: AuroraCalibration,
    ): List<FloatArray> {
        val chunk = Math.round(rate * readMs / 1000f)
        var clock = 0L
        val tap = AudioAnalysisTap(rate, calibration = cal, nowNs = { clock })
        val out = ArrayList<FloatArray>()
        var i = 0
        while (i < clip.first.size) {
            val len = minOf(chunk, clip.first.size - i)
            tap.offer(toPcm16(clip.first, i, len), len * 2) { f ->
                val centre = (f.sampleOffset - chunk / 2).toInt()
                if (f.sampleOffset > rate && clip.second.any { centre in it }) out.add(f.bands.copyOf())
            }
            clock += (readMs * 1_000_000f).toLong()
            i += len
        }
        return out
    }

    /** The same profile with the expander switched OFF — the only honest baseline for "how much". */
    private fun withoutExpander(cal: AuroraCalibration) = AuroraCalibration(
        "${cal.id}-no-expander",
        gains = cal.gains,
        peakInit = cal.peakInit,
        peakFloor = cal.peakFloor,
        noiseLive = cal.noiseLive,
        noiseSubtract = 0f,
        headroom = cal.headroom,
        knee = cal.knee,
    )

    /**
     * The requirement is stated per talker level because that is what it depends on, and the
     * dependence is the physics rather than a tuning choice: the expander removes a fixed quantity,
     * so the closer a voice sits to the floor the larger the fraction of it that WAS floor. An
     * ordinary talker loses ~1%; one 12 dB quieter loses ~11% on the sheet where the floor is
     * largest relative to the content (band 3, where the transducer's hiss is the floor).
     */
    @Test
    fun `a voice above the floor keeps its drawn height`() {
        for ((gain, floor) in listOf(1f to 0.90f, 0.25f to 0.85f)) {
            val clip = richVoiced(20000, gain)
            val on = voicedBands(clip, AuroraCalibration.UNPROCESSED)
            val off = voicedBands(clip, withoutExpander(AuroraCalibration.UNPROCESSED))
            for (b in 0 until AuroraAnalyser.BANDS) {
                val kept = drawn(on, b) / drawn(off, b)
                assertTrue(
                    "gain $gain sheet $b keeps ${"%.1f".format(kept * 100)}% of its drawn height " +
                        "(${drawn(on, b)} of ${drawn(off, b)}), must keep >= ${(floor * 100).toInt()}%",
                    kept >= floor,
                )
            }
        }
    }

    /**
     * ...and the SHIPPED clip, on the one sheet it drives above the floor. Measured: sheet 0 keeps
     * 95% while sheets 1-3 go to zero, and that is the clip rather than the expander — those three
     * bands carry 0.30x / 0.12x / 0.05x of their own floors (see [richVoiced]), i.e. less than the
     * room. Pinned here so the asymmetry is a stated, checked fact rather than a surprise for
     * whoever next reads the characterisation numbers.
     */
    @Test
    fun `the shipped speech clip keeps the sheet that carries it`() {
        val clip = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 20000, gain = 1f)
        val on = voicedBands(clip, AuroraCalibration.UNPROCESSED)
        val off = voicedBands(clip, withoutExpander(AuroraCalibration.UNPROCESSED))
        val kept = drawn(on, 0) / drawn(off, 0)
        assertTrue(
            "sheet 0 keeps ${"%.1f".format(kept * 100)}% (${drawn(on, 0)} of ${drawn(off, 0)})",
            kept >= 0.90f,
        )
        assertTrue(
            "guard: this clip's upper bands must really be at or under the floor, or this test is " +
                "measuring something else",
            drawn(on, 3) < 0.05f,
        )
    }

    /**
     * THE OTHER HALF OF THE FIX, and the one that makes it a fix rather than a mute button: a
     * WHISPER still reaches the eye.
     *
     * Scored on the shipped dynamic-range schedule — one continuous session, a whisper 4x quieter
     * than ordinary speech after a passage of ordinary speech — through the profile that carries
     * that measurement ([AuroraCalibration.DEFAULT]). The requirement is
     * WaveformDynamicsGatesTest's, restated here because the expander is what would break it: this
     * file owns a stage that can delete quiet audio, so it owns the proof that it does not.
     */
    @Test
    fun `a whisper is still visible after the expander`() {
        val run = AuroraDynamicsHarness.dynamicRange()
        val sheet = run.getValue("normal-again").dominantBand
        val whisper = run.getValue("whisper").perBand[sheet].p50
        val normal = run.getValue("normal-again").perBand[sheet].p50
        val onScreen = Aurora.displayCurve(whisper.toFloat())
        assertTrue(
            "a whisper draws $onScreen of full height on sheet $sheet, must be >= 0.20 " +
                "(the expander is subtracting more than the whisper contains)",
            onScreen >= 0.20f,
        )
        assertTrue(
            "...and must still be separated from ordinary speech (normal $normal vs whisper $whisper)",
            normal / whisper >= 2.0,
        )
    }

    /**
     * ...and the same requirement where the expander is actually ON, which has to be stated in the
     * only terms physics allows: a voice ABOVE the assumed room tone stays visible.
     *
     * The shipped whisper passage above cannot be the probe here, and the reason is worth stating
     * plainly rather than working around. Its words run -47..-35 dBFS RMS, straddling the room this
     * profile assumes, and its band-0 energy is 1.3x that of -45 dBFS pink — the very room tone the
     * headline requirement says must be invisible. Those two requirements are the SAME SIGNAL LEVEL,
     * so no subtraction can keep one and delete the other; what separates them in real audio is time
     * structure (a voice has syllables, a room does not), which is a different stage and not this
     * one. What this stage owes is a floor that does not eat a voice ABOVE it, and here it is: a
     * voice 6 dB over the assumed room tone still draws unmistakably.
     */
    @Test
    fun `a quiet voice above the assumed room tone stays clearly visible`() {
        // Scaled so the low band lands ~2x (6 dB over) the floor this profile subtracts.
        val quiet = richVoiced(12000, 0.09f)
        val frames = voicedBands(quiet, AuroraCalibration.UNPROCESSED)
        val loudestSheet = (0 until AuroraAnalyser.BANDS).maxByOrNull { drawn(frames, it) }!!
        assertTrue(
            "a voice 6 dB above the assumed room draws ${drawn(frames, loudestSheet)} on its " +
                "loudest sheet, must be >= 0.20 (${report(frames)})",
            drawn(frames, loudestSheet) >= 0.20f,
        )
    }

    /**
     * The depth is a real control, and deeper is quieter — MONOTONE. A profile is allowed to be
     * re-measured; what it may not do is have its depth turned up and draw MORE.
     */
    @Test
    fun `a deeper expander never draws louder`() {
        val clip = richVoiced(8000, 1f)
        var previous: List<FloatArray>? = null
        for (depth in floatArrayOf(0f, 0.5f, 1f, 2f)) {
            val cal = AuroraCalibration(
                "depth-$depth",
                noiseLive = AuroraCalibration.UNPROCESSED_NOISE_LIVE,
                noiseSubtract = depth,
            )
            val frames = voicedBands(clip, cal)
            previous?.let { prev ->
                for (b in 0 until AuroraAnalyser.BANDS) {
                    assertTrue(
                        "depth $depth sheet $b draws ${drawn(frames, b)} against the shallower " +
                            "${drawn(prev, b)}",
                        drawn(frames, b) <= drawn(prev, b) + 1e-6f,
                    )
                }
            }
            previous = frames
        }
    }

    /** Deterministic white noise, RMS-calibrated — mic hiss rather than room tone. */
    private fun white(n: Int, dbfs: Double, seed: Long = 0x5EED1234L): FloatArray {
        var rng = seed
        val x = FloatArray(n) {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            ((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f
        }
        var acc = 0.0
        for (v in x) acc += v.toDouble() * v
        val g = (10.0.pow(dbfs / 20.0) / sqrt(acc / x.size)).toFloat()
        for (i in x.indices) x[i] = (x[i] * g).coerceIn(-1f, 1f)
        return x
    }
}
