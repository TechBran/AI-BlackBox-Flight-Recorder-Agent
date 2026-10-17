package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import kotlin.math.exp
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * The noise-gate study's measuring instrument — its signals and its re-implementation of the
 * analyser — in a file of its own, so the one property the study rests on is ASSERTED in the unit
 * gate (NoiseGateSimulationTest) while the study itself, NoiseGateCandidateDiagnostic, stays an
 * opt-in printer.
 *
 * [simulate] is a faithful re-implementation of [AuroraAnalyser]'s dynamics driven from the RAW band
 * energies the real analyser reports ([trace]), so a candidate NOISE GATE can be inserted and the
 * predicted output measured on the same signals without touching production. Its predictions are
 * worth exactly as much as its fidelity, and nothing used to hold that: the study validated itself
 * by PRINTING the disagreement ("must agree to <1e-3") inside a build that stayed green whatever it
 * printed.
 */
internal object NoiseGateSimulation {

    // -------------------------------------------------------------- signals (same as the floor diag)

    fun white(n: Int, dbfs: Double, seed: Long = 0x5EED1234L): FloatArray {
        var rng = seed
        val x = FloatArray(n) {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            ((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f
        }
        return rescale(x, dbfs)
    }

    fun pink(n: Int, dbfs: Double, seed: Long = 0x1234_5EEDL): FloatArray {
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
        return rescale(x, dbfs)
    }

    private fun rescale(x: FloatArray, dbfs: Double): FloatArray {
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

    /** RAW band energies AND the real analyser's output, hop-aligned. */
    fun trace(samples: FloatArray, rate: Int, cal: AuroraCalibration): Pair<List<FloatArray>, List<FloatArray>> {
        val hop = Math.round(rate * AudioAnalysisTap.HOP_MS / 1000f)
        var clock = 0L
        val a = AuroraAnalyser(rate, { clock }, cal)
        val raws = ArrayList<FloatArray>()
        val outs = ArrayList<FloatArray>()
        val raw = FloatArray(AuroraAnalyser.BANDS)
        var i = 0
        while (i + hop <= samples.size) {
            val o = a.analyze(toPcm16(samples, i, hop), hop * 2, raw)
            raws.add(raw.copyOf()); outs.add(o.copyOf())
            clock += (AudioAnalysisTap.HOP_MS * 1_000_000.0).toLong()
            i += hop
        }
        return raws to outs
    }

    // -------------------------------------------------------------- the simulation

    /**
     * @param subtractK how many tracked noise floors are subtracted from the raw band energy.
     *   0 == the shipped behaviour exactly, the calibration's own expander included (asserted by
     *   NoiseGateSimulationTest). Above 0 the tracked floor takes the shipped expander's PLACE.
     * @param floorUpTau seconds for the tracker to RISE toward a level (slow — it must not learn a
     *   held vowel as noise).
     * @param floorDownTau seconds for the tracker to FALL toward a quieter level (fast — the floor
     *   is the minimum, and a quiet gap must be recognised as one within a syllable).
     * @param noiseLiveScale multiplies NOISE_LIVE. With the numerator gated, NOISE_LIVE no longer
     *   has to hold the noise down, so it can come off the speech level.
     */
    fun simulate(
        raws: List<FloatArray>,
        dt: Float,
        cal: AuroraCalibration,
        subtractK: Float,
        floorUpTau: Float = 12f,
        floorDownTau: Float = 0.5f,
        noiseLiveScale: Float = 1f,
    ): List<FloatArray> {
        val peaks = FloatArray(AuroraAnalyser.BANDS) { cal.peakInit[it] }
        val fastRefs = FloatArray(AuroraAnalyser.BANDS) { cal.peakInit[it] }
        val floorTrack = FloatArray(AuroraAnalyser.BANDS)
        var seeded = false
        val out = ArrayList<FloatArray>()
        val upA = 1f - exp(-dt / floorUpTau)
        val downA = 1f - exp(-dt / floorDownTau)
        var elapsedMs = 0f
        for (r in raws) {
            val warmScale = AuroraAnalyser.WARM_FLOOR +
                (1f - AuroraAnalyser.WARM_FLOOR) * (elapsedMs / AuroraAnalyser.WARMUP_MS).coerceIn(0f, 1f)
            val acquiring = elapsedMs < AuroraAnalyser.WARMUP_MS
            val fall = if (acquiring) 1f - exp(-dt / cal.acquireTauSec) else 0f
            val steadyRise = if (acquiring) 0f else 1f - exp(-dt / cal.steadyRiseTauSec)
            val steadyDecay = exp(-dt / cal.steadyDecayTauSec)
            val fastFall = exp(-dt / cal.fastReleaseTauSec)
            val row = FloatArray(AuroraAnalyser.BANDS)
            for (b in 0 until AuroraAnalyser.BANDS) {
                val vRaw = r[b]
                if (!seeded) floorTrack[b] = vRaw
                else {
                    val a = if (vRaw < floorTrack[b]) downA else upA
                    floorTrack[b] += (vRaw - floorTrack[b]) * a
                }
                // subtractK = 0 is the SHIPPED stage: the calibration's own downward expander, exactly
                // as analyze() applies it. Leaving it out is what let this drift — on a profile whose
                // depth is not zero (UNPROCESSED) the "shipped" baseline was an analyser that no longer
                // existed. A candidate is an ALTERNATIVE to that stage rather than an addition to it,
                // so it takes its tracked floor off the raw energy in the expander's place, which is
                // what every candidate row in the study has always measured.
                val v = if (subtractK <= 0f) {
                    (vRaw - cal.noiseSubtract * cal.noiseLive[b]).coerceAtLeast(0f)
                } else {
                    (vRaw - subtractK * floorTrack[b]).coerceAtLeast(0f)
                }

                val prevFast = fastRefs[b]
                val fast = if (!seeded) v else maxOf(v, prevFast * fastFall)
                fastRefs[b] = fast
                val ref = peaks[b]
                val next = when {
                    !seeded -> fast.coerceIn(cal.peakFloor[b], cal.peakInit[b])
                    acquiring -> ref + (v - ref) * (if (v > ref) cal.riseBlend else fall)
                    else -> {
                        val decayed = maxOf(ref * steadyDecay, cal.peakFloor[b])
                        if (fast > decayed) decayed + (fast - decayed) * steadyRise else decayed
                    }
                }
                peaks[b] = next.coerceAtLeast(cal.peakFloor[b])
                val denom = maxOf(peaks[b], cal.noiseLive[b] * noiseLiveScale) * cal.headroom
                row[b] = (AuroraAnalyser.softKnee(v / denom, cal.knee) * warmScale).coerceIn(0f, 1f)
            }
            seeded = true
            elapsedMs += dt * 1000f
            out.add(row)
        }
        return out
    }
}
