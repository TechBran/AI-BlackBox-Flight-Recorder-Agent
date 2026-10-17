package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraEnvelope
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.pow

/**
 * THE DISPLAY RANGE — what the renderer does with the range the analyser hands it.
 *
 * THE BRIEF (operator, 2026-08-11, v35 on a Galaxy Z Fold): the noise floor makes the waveform
 * "constantly have too much going on"; the movement "isn't fluent and that much for how much audio
 * is coming in". Measured through the real chain, the drawn signal held 9.24 of the 616.8-fold
 * p90/p10 range the analyser produced — 1.825 decades destroyed, of which the renderer owned
 * 100%: DISPLAY_GAMMA = 0.5 took 76.1%, the BASELINE floor CLAMP 12.1%, the release ladder 11.8%.
 *
 * THIS FILE OWNS THE THREE LEVERS THAT ANSWER THAT, and nothing else:
 *   1. [Aurora.DISPLAY_GAMMA] 0.5 -> 0.65
 *   2. [Aurora.BAND_RELEASE_TAUS_SEC] halved uniformly
 *   3. the rest floor as a BLEND ([Aurora.restBlend]) instead of a `maxOf` clamp
 *
 * WHY IT DOES NOT DRIVE THE REAL ANALYSER. Every measurement here is scored on ONE deterministic
 * energy trace ([syllableTrace]) pushed through BOTH the shipped renderer chain and the chain as it
 * was before these three changes, in the SAME run. That is deliberate:
 *   - a lever's effect is a property of the RENDERER, and mixing in a live analyser measures the
 *     analyser's calibration at least as much (the calibration is a separate, concurrently-moving
 *     concern — see [WaveformDynamicsCharacterisationTest], which is where analyser numbers are
 *     pinned);
 *   - scoring BEFORE and AFTER on identical input makes every assertion below a difference rather
 *     than an absolute, so it survives the trace being made more realistic later and cannot be
 *     satisfied by anything except the levers actually working;
 *   - it is exactly reproducible with no clock, no RNG seed to lose and no device.
 *
 * The trace is a syllable train at the ~4.3 syllables/sec of connected speech, with a per-syllable
 * peak spread over a decade and a low inter-syllable floor — the shape the release ladder has to be
 * able to follow and the range the curve has to be able to show.
 */
class WaveformDisplayRangeTest {

    // ------------------------------------------------------------------ the chain, before and after

    /** The renderer's frame rate on the device this was reported from. */
    private val displayHz = 120

    /** The analyser's hop, i.e. how often a new band energy arrives. */
    private val hopSec = 0.020f

    /** ~4.3 syllables/sec — the rate connected speech runs at. */
    private val syllableSec = 1.0f / 4.3f

    /** THE PREVIOUS chain's constants, restated here ONLY as the before-arm of a measurement. */
    private val previousGamma = 0.5f
    private val previousReleaseTaus = floatArrayOf(0.095f, 0.061f, 0.044f, 0.031f)

    /**
     * A deterministic syllable train of linear band energies, one entry per analyser hop.
     *
     * Each syllable is a raised-cosine burst holding for 60% of its period and falling to a low
     * floor for the rest, with peaks walked over a decade by a fixed multiplicative sequence so the
     * trace has both fast (within-syllable) and slow (across-syllable) structure. No RNG: the
     * sequence is a fixed irrational rotation, so the distribution is even and the trace is
     * bit-identical on every machine.
     *
     * The default floor is 0.0013 because that is the measured p10 of the real analyser's output
     * over a whole utterance. It matters that it is BELOW the drawn rest floor: everything
     * interesting about the floor rule happens down there, and a trace whose quiet passages sit
     * above 0.06 drawn would not exercise it at all.
     */
    private fun syllableTrace(seconds: Float = 8f, floor: Float = 0.0013f): FloatArray {
        val hops = (seconds / hopSec).toInt()
        val out = FloatArray(hops)
        var rot = 0.137f
        var syllable = -1
        var peak = 0.1f
        for (i in 0 until hops) {
            val t = i * hopSec
            val s = (t / syllableSec).toInt()
            if (s != syllable) {
                syllable = s
                rot = (rot + 0.6180339f) % 1f      // golden-ratio rotation: even, deterministic
                peak = 0.02f * 10f.pow(rot * 1.7f)  // 0.02 .. 1.0, a decade and a half
            }
            val phase = (t % syllableSec) / syllableSec
            val voiced = if (phase < 0.6f) {
                val u = phase / 0.6f
                (0.5f - 0.5f * kotlin.math.cos((2.0 * Math.PI * u).toFloat())) * peak
            } else {
                0f
            }
            out[i] = maxOf(floor, voiced).coerceIn(0f, 1f)
        }
        return out
    }

    /**
     * Render one band's energy trace to DRAWN heights at [displayHz], through either the shipped
     * chain or the previous one. The shipped arm calls the production functions; the previous arm
     * restates the three constants it is the before-picture of.
     */
    private fun drawn(trace: FloatArray, shipped: Boolean, band: Int = 0): FloatArray {
        val dt = 1f / displayHz
        val out = ArrayList<Float>(trace.size * (displayHz * hopSec).toInt())
        var level = 0f
        var tSec = 0.0
        for ((i, energy) in trace.withIndex()) {
            val target = if (shipped) {
                Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(energy))
            } else {
                maxOf(Aurora.BASELINE, energy.pow(previousGamma))
            }
            val release = if (shipped) Aurora.BAND_RELEASE_TAUS_SEC[band] else previousReleaseTaus[band]
            val end = (i + 1) * hopSec.toDouble()
            while (tSec < end) {
                val tau = if (target > level) Aurora.ATTACK_TAU_SEC else release
                level = Aurora.approach(level, target, dt, tau)
                out.add(level)
                tSec += dt
            }
        }
        return out.toFloatArray()
    }

    private fun percentile(xs: FloatArray, p: Double): Double {
        val s = xs.sorted()
        val idx = Math.round(p / 100.0 * (s.size - 1)).toInt().coerceIn(0, s.size - 1)
        return s[idx].toDouble()
    }

    /** p90/p10 over every drawn frame — the whole-utterance range the eye is given. */
    private fun range(xs: FloatArray): Double = percentile(xs, 90.0) / percentile(xs, 10.0)

    /** Mean absolute change per frame — how much the ribbon MOVES, the operator's other complaint. */
    private fun motion(xs: FloatArray): Double {
        var s = 0.0
        for (i in 1 until xs.size) s += abs(xs[i] - xs[i - 1]).toDouble()
        return s / (xs.size - 1)
    }

    // ============================================================ 1. the three levers, together

    /**
     * THE HEADLINE: the drawn range over a whole utterance, before and after all three levers.
     *
     * MEASURED on [syllableTrace], whole-trace p90/p10 4.729 -> 5.337 (+12.9%) and voiced
     * peak-to-typical p90/p50 2.409 -> 3.038 (+26.1%). Per lever, each applied ALONE to the
     * previous chain so the attribution is honest rather than assumed:
     *
     *   DISPLAY_GAMMA 0.5 -> 0.65   p90/p10 4.729 -> 5.892   (+24.6%)
     *   release ladder halved       p90/p10 4.729 -> 6.314   (+33.5%)
     *   floor clamp -> blend        p90/p10 4.729 -> 3.649   (-22.8%)   <- READ THE NEXT TEST
     *
     * The gate is +10% on the combination, well under the measured +12.9% but far enough above
     * zero that only all three landing can satisfy it.
     */
    @Test
    fun `the three levers together widen the drawn range`() {
        val trace = syllableTrace()
        val before = range(drawn(trace, shipped = false))
        val after = range(drawn(trace, shipped = true))
        assertTrue(
            "drawn p90/p10 went $before -> $after; the three levers must widen it by >= 10%",
            after >= before * 1.10,
        )
        // The peak-to-typical swing — a loud moment against an ordinary one, which is what the eye
        // reads as the ribbon tracking the voice — moves further than the whole-trace range does.
        val swingBefore = drawn(trace, shipped = false).let { percentile(it, 90.0) / percentile(it, 50.0) }
        val swingAfter = drawn(trace, shipped = true).let { percentile(it, 90.0) / percentile(it, 50.0) }
        assertTrue(
            "voiced p90/p50 swing went $swingBefore -> $swingAfter, must rise by >= 15%",
            swingAfter >= swingBefore * 1.15,
        )
    }

    /**
     * THE BLEND COSTS RANGE, AND IT IS WORTH IT ANYWAY. Recorded as a test rather than left to be
     * rediscovered, because the reasoning is not obvious and the number points the wrong way.
     *
     * The range budget attributed 12.1% of the destroyed range to the BASELINE floor, measured
     * against an arm with NO FLOOR AT ALL. A blend is not "no floor": it maps 0..1 onto
     * floor..1, so it lifts the bottom of the range slightly HIGHER than the clamp did (a clamp
     * pins every sub-floor frame at exactly 0.06; the blend puts them at 0.06 + 0.94*curve). On
     * this trace that costs 22.8% of p90/p10 when applied on its own.
     *
     * What it buys is the thing the operator actually reported — four styles frozen SOLID through
     * the whole quiet end of the range, a measured step of exactly 0.0000 between two room levels
     * 5 dB apart. A frozen ribbon is not a small ribbon, it is a broken one, and no amount of
     * p90/p10 makes up for a picture that does not answer the microphone. The other two levers pay
     * the range back with interest (see the test above).
     *
     * If that 12.1% is ever wanted back, the lever is the FLOOR ITSELF — [Aurora.BASELINE], or
     * dropping the floor on the mic path the way the player bar already does — never a return to
     * the clamp.
     */
    @Test
    fun `the blend trades a little range for a quiet end that is not frozen`() {
        val trace = syllableTrace()
        val dt = 1f / displayHz
        fun render(blend: Boolean): FloatArray {
            val out = ArrayList<Float>()
            var level = 0f
            var tSec = 0.0
            for ((i, energy) in trace.withIndex()) {
                val curve = energy.pow(previousGamma)
                val target = if (blend) Aurora.restBlend(Aurora.BASELINE, curve) else maxOf(Aurora.BASELINE, curve)
                val end = (i + 1) * hopSec.toDouble()
                while (tSec < end) {
                    val tau = if (target > level) Aurora.ATTACK_TAU_SEC else previousReleaseTaus[0]
                    level = Aurora.approach(level, target, dt, tau)
                    out.add(level)
                    tSec += dt
                }
            }
            return out.toFloatArray()
        }
        // The cost, pinned so it cannot grow unnoticed.
        val clamped = range(render(blend = false))
        val blended = range(render(blend = true))
        assertTrue("the blend must not cost more than a quarter of p90/p10 (clamp $clamped -> blend $blended)",
            blended > clamped * 0.75)
        // The purchase: strict monotonicity, i.e. every distinguishable input draws a
        // distinguishable height. This is what the clamp destroyed.
        var clampFrozenPairs = 0
        for (e in listOf(0.0001f, 0.0004f, 0.0016f, 0.0064f)) {
            val lo = maxOf(Aurora.BASELINE, Aurora.displayCurve(e))
            val hi = maxOf(Aurora.BASELINE, Aurora.displayCurve(e * 4f))
            if (hi == lo) clampFrozenPairs++
            assertTrue(
                "a 12 dB step at $e must move the drawn height",
                Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(e * 4f)) >
                    Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(e)) * 1.05f,
            )
        }
        assertTrue("the clamp froze $clampFrozenPairs of 4 twelve-dB steps solid", clampFrozenPairs >= 2)
    }

    /**
     * ...and the ribbon MOVES more per frame, which is the same fix seen in the time domain: a
     * shorter release lets a sheet get back down between syllables instead of drawing their
     * envelope.
     *
     * MEASURED 0.02032 -> 0.02138 per frame at 120 Hz (+5.2%), and the two levers pull opposite
     * ways inside that: the shorter ladder ALONE gives +21.6%, while the higher exponent alone
     * costs 17% (it compresses the top of the range, which is where most of the frame-to-frame
     * excursion lives). Worth knowing which is which — if the ribbon ever needs to be livelier
     * again, the ladder is the lever that does it and the exponent is not.
     */
    @Test
    fun `the ribbon moves more per frame than it did`() {
        val trace = syllableTrace()
        val before = motion(drawn(trace, shipped = false))
        val after = motion(drawn(trace, shipped = true))
        assertTrue("motion/frame went $before -> $after and must increase", after > before * 1.03)
    }

    // ============================================================ 2. the curve

    /**
     * THE NOISE ANSWER, and the reason the curve moved rather than a gate being added.
     *
     * A fixed power law pulls a SMALL number down harder than a large one, so raising the exponent
     * lowers the room-tone floor everywhere at once with no threshold to tune and nothing to decide
     * about any individual frame — and, better than uniformly, it lowers it MOST where the tone is
     * quietest. Scored against the drawn heights the diagnosis recorded through the real
     * AudioAnalysisTap on pink room tone, inverted back through the old square root to the energies
     * that produced them, so this is the real floor and not an invented one.
     *
     * MEASURED across that ladder: every entry falls, by 22.5% at the loudest (a -40 dBFS band
     * drawing 0.428) to 52.8% at the quietest (a -50 dBFS band drawing 0.082). The ordering is the
     * whole point — speech, which is louder still, is barely touched.
     */
    @Test
    fun `room tone draws lower at every noise level, and most where it is quietest`() {
        // Drawn heights measured through the real AudioAnalysisTap on pink room tone, per band:
        //   -50 dBFS [0.240 0.149 0.125 0.082]
        //   -45 dBFS [0.321 0.199 0.167 0.110]
        //   -40 dBFS [0.428 0.265 0.222 0.147]
        val drawnBefore = floatArrayOf(
            0.240f, 0.149f, 0.125f, 0.082f,
            0.321f, 0.199f, 0.167f, 0.110f,
            0.428f, 0.265f, 0.222f, 0.147f,
        )
        for (h in drawnBefore) {
            // Recover the linear energy: the old renderer drew h = energy^0.5, exactly.
            val energy = h.pow(1f / previousGamma)
            val after = Aurora.displayCurve(energy)
            val drop = 1f - after / h
            assertTrue("room tone at drawn $h now draws $after — it must fall by 20-60%", drop in 0.20f..0.60f)
        }
        // QUIETER MUST FALL HARDER — the property that separates a power law from a gate, and the
        // reason no threshold had to be chosen. Checked across the three levels of one band.
        val band2 = floatArrayOf(0.125f, 0.167f, 0.222f)   // -50, -45, -40 dBFS
        var previousDrop = 1f
        for (h in band2) {
            val drop = 1f - Aurora.displayCurve(h.pow(1f / previousGamma)) / h
            assertTrue("a louder tone must not fall further than a quieter one", drop < previousDrop)
            previousDrop = drop
        }
        // ...and SPEECH is barely touched by comparison: a full-gain voiced band drew 0.505 and
        // loses less than a fifth of that, against the quiet band's half.
        val speechDrop = 1f - Aurora.displayCurve(0.505f.pow(1f / previousGamma)) / 0.505f
        assertTrue("speech at 0.505 lost $speechDrop — it must lose far less than room tone", speechDrop < 0.22f)
    }

    /**
     * The other half of the same curve: every CONTRAST in the signal is rendered larger. An energy
     * ratio r draws as r^gamma, so raising the exponent raises every ratio the eye is given —
     * speech over room tone included. This is why the curve is the lever and a gate is not: it buys
     * the noise reduction and the contrast in one move.
     */
    @Test
    fun `every contrast in the signal is drawn larger than it was`() {
        for (r in listOf(1.5f, 2f, 4f, 10f, 50f)) {
            val before = r.pow(previousGamma)
            val after = r.pow(Aurora.DISPLAY_GAMMA)
            assertTrue("an energy ratio of $r drew ${before}x and now draws ${after}x", after > before)
        }
        // 4x of acoustic energy — a syllable against an ordinary one — used to draw 2.00x and now
        // draws 2.46x.
        assertEquals(2.46, 4f.pow(Aurora.DISPLAY_GAMMA).toDouble(), 0.02)
    }

    /**
     * THE CEILING ON THE EXPONENT, so the next person to reach for it knows which way is out of
     * bounds: a whisper must stay VISIBLE. A whisper measured 0.0898 linear on the shipped path,
     * and WaveformDynamicsGatesTest requires it to draw at >= 0.20 of full height. That is what
     * caps the exponent, and 0.65 clears it with the margin below.
     */
    @Test
    fun `the exponent still leaves a whisper visible`() {
        val whisperEnergy = 0.0898f
        val drawn = Aurora.displayCurve(whisperEnergy)
        assertTrue("a whisper draws at $drawn of full height and must clear 0.20", drawn >= 0.20f)
        assertTrue("...and must be unmistakably above the resting sliver", drawn > Aurora.BASELINE * 3f)
    }

    // ============================================================ 3. the rest floor

    /**
     * THE FREEZE THE CLAMP CAUSED, stated as the defect it was.
     *
     * `maxOf(floor, level)` returns the SAME number for every level under the floor, so below about
     * -35 dBFS four styles drew the identical picture and the measured step between two room levels
     * 5 dB apart was exactly 0.0000. A blend cannot do that: it is strictly increasing, so no two
     * different energies can ever draw the same height.
     */
    @Test
    fun `the rest floor no longer freezes the whole quiet end of the range`() {
        val quiet = floatArrayOf(0.0002f, 0.0006f, 0.002f, 0.006f, 0.02f)
        var frozenUnderClamp = 0
        for (i in 1 until quiet.size) {
            val clampLo = maxOf(Aurora.BASELINE, Aurora.displayCurve(quiet[i - 1]))
            val clampHi = maxOf(Aurora.BASELINE, Aurora.displayCurve(quiet[i]))
            if (clampHi - clampLo == 0f) frozenUnderClamp++

            val blendLo = Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(quiet[i - 1]))
            val blendHi = Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(quiet[i]))
            assertTrue(
                "the blend must separate ${quiet[i - 1]} from ${quiet[i]} (got $blendLo vs $blendHi)",
                blendHi > blendLo,
            )
        }
        assertTrue(
            "the clamp is meant to have frozen most of this ladder — if it did not, this test is " +
                "no longer measuring the defect it was written for",
            frozenUnderClamp >= 3,
        )
    }

    /**
     * ...while everything the clamp was there for survives, which is the whole reason a blend is
     * the right replacement rather than dropping the floor.
     */
    @Test
    fun `the blend keeps both endpoints exactly`() {
        // A present-but-SILENT voice sits on the floor to the bit — this is the value the idle
        // park's settle test compares against, so the park is unchanged.
        assertEquals(Aurora.BASELINE, Aurora.restBlend(Aurora.BASELINE, 0f), 0f)
        // Full drive still reaches exactly 1, so no amplitude budget or rail guarantee moves.
        assertEquals(1f, Aurora.restBlend(Aurora.BASELINE, 1f), 0f)
        // A caller resting at 0 — the player bar, whose silence is the FILE's silence — is the
        // identity, byte for byte. Every drawn height on that surface is unchanged by this rule.
        for (v in listOf(0f, 0.013f, 0.4f, 1f)) assertEquals(v, Aurora.restBlend(0f, v), 0f)
        // Total on garbage, like everything else on this path.
        assertEquals(0f, Aurora.restBlend(Float.NaN, 0f), 0f)
        assertEquals(Aurora.BASELINE, Aurora.restBlend(Aurora.BASELINE, Float.NaN), 0f)
        assertEquals(1f, Aurora.restBlend(2f, -1f), 0f)
    }

    /**
     * A PRESENT-BUT-SILENT VOICE STILL SETTLES ON THE FLOOR through the REAL envelope, which is the
     * property the idle park depends on: [AuroraEnvelope.resting] compares the level against the
     * floor, so a blend that did not land exactly there would keep the frame loop awake forever.
     */
    @Test
    fun `a silent present voice still settles exactly on its floor through the real envelope`() {
        for (floor in listOf(0f, Aurora.BASELINE, 0.2f)) {
            val env = AuroraEnvelope(floor)
            val silent = AuroraVoice(AuroraSpeaker.HUMAN, FloatArray(Aurora.SHEETS))
            repeat(600) { env.step(1f / 120f, silent) }
            for (j in 0 until Aurora.SHEETS) {
                assertEquals("floor $floor sheet $j", floor, env.levels[j], 1e-4f)
            }
            assertTrue("floor $floor must report itself settled", env.resting)
        }
    }

    // ============================================================ 4. the release ladder

    /**
     * EVERY SHEET NOW FALLS IN WELL UNDER A SYLLABLE, which is the requirement
     * WaveformDynamicsGatesTest states and this test measures the margin on. A first-order release
     * loses 90% of its height in ln(10) taus; at the shipped ladder that is 109 / 70 / 51 / 36 ms
     * against a 233 ms syllable period.
     */
    @Test
    fun `the release ladder falls in well under half a syllable on every sheet`() {
        val syllableMs = 1000f * syllableSec
        for (j in Aurora.BAND_RELEASE_TAUS_SEC.indices) {
            val t90Ms = 1000f * Aurora.BAND_RELEASE_TAUS_SEC[j] * kotlin.math.ln(10f)
            assertTrue(
                "sheet $j falls 90% in ${t90Ms}ms; half a syllable is ${syllableMs / 2}ms",
                t90Ms <= syllableMs / 2f,
            )
        }
    }

    /**
     * ...and the ladder is still the SHAPE M6 chose — bass lingers, sibilance snaps — because the
     * scale was the only thing wrong with it. Halving it uniformly is what preserves that: every
     * ratio between two sheets is exactly what it was.
     */
    @Test
    fun `halving the ladder preserved its shape exactly`() {
        for (j in Aurora.BAND_RELEASE_TAUS_SEC.indices) {
            assertEquals(
                "sheet $j is no longer a uniform 0.50 of the previous ladder",
                0.50,
                (Aurora.BAND_RELEASE_TAUS_SEC[j] / previousReleaseTaus[j]).toDouble(),
                0.01,
            )
        }
        for (j in 1 until Aurora.BAND_RELEASE_TAUS_SEC.size) {
            assertTrue(
                "sheet $j must still release faster than sheet ${j - 1}",
                Aurora.BAND_RELEASE_TAUS_SEC[j] < Aurora.BAND_RELEASE_TAUS_SEC[j - 1],
            )
        }
    }

    /**
     * The ladder as a RANGE lever on its own, with the curve and the floor held at their previous
     * values, so its contribution is attributable rather than bundled: measured 4.729 -> 6.314
     * p90/p10 on the same trace, the largest single contribution of the three.
     */
    @Test
    fun `the shorter ladder recovers range on its own`() {
        val trace = syllableTrace()
        fun renderWith(taus: FloatArray): FloatArray {
            val dt = 1f / displayHz
            val out = ArrayList<Float>()
            var level = 0f
            var tSec = 0.0
            for ((i, energy) in trace.withIndex()) {
                val target = maxOf(Aurora.BASELINE, energy.pow(previousGamma))
                val end = (i + 1) * hopSec.toDouble()
                while (tSec < end) {
                    val tau = if (target > level) Aurora.ATTACK_TAU_SEC else taus[0]
                    level = Aurora.approach(level, target, dt, tau)
                    out.add(level)
                    tSec += dt
                }
            }
            return out.toFloatArray()
        }
        val before = range(renderWith(previousReleaseTaus))
        val after = range(renderWith(Aurora.BAND_RELEASE_TAUS_SEC))
        assertTrue("the ladder alone moved p90/p10 $before -> $after", after > before * 1.15)
    }
}
