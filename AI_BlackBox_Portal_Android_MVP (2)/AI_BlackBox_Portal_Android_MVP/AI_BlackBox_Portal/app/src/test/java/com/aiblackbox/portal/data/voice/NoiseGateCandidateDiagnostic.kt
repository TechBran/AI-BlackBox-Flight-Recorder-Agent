package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.voice.NoiseGateSimulation.pink
import com.aiblackbox.portal.data.voice.NoiseGateSimulation.simulate
import com.aiblackbox.portal.data.voice.NoiseGateSimulation.trace
import com.aiblackbox.portal.data.voice.NoiseGateSimulation.white
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import org.junit.Test
import kotlin.math.exp

/**
 * MEASUREMENT ONLY. Inserts a candidate NOISE GATE into [NoiseGateSimulation.simulate] — a faithful
 * re-implementation of [AuroraAnalyser]'s dynamics driven from the RAW band energies the real
 * analyser reports — and measures the predicted output on the same signals, without touching
 * production.
 *
 * The simulation's fidelity used to be this file's step 1, printed (and, once the shipped expander
 * landed under it, printed as a 0.2 disagreement inside a green build). It is asserted in the unit
 * gate now, by NoiseGateSimulationTest. [simulateGated] below still carries its own copy of the dynamics
 * and is NOT covered by that test.
 *
 * Opt-in, like every *Diagnostic: excluded from testDebugUnitTest, run with -PincludeDiagnostics
 * (see app/build.gradle).
 */
class NoiseGateCandidateDiagnostic {

    // -------------------------------------------------------------- stats

    private fun pct(run: List<FloatArray>, b: Int, p: Double): Double {
        val s = run.map { it[b] }.sorted()
        if (s.isEmpty()) return Double.NaN
        return s[Math.round(p / 100.0 * (s.size - 1)).toInt().coerceIn(0, s.size - 1)].toDouble()
    }

    private fun f4(v: Double) = String.format("%.4f", v)

    private fun tail(run: List<FloatArray>, fromSec: Double, dt: Float) =
        run.drop((fromSec / dt).toInt()).ifEmpty { run }

    @Test
    fun `candidate noise gate`() {
        val rate = 16000
        val cal = AuroraCalibration.UNPROCESSED
        val dt = AudioAnalysisTap.HOP_MS / 1000f
        val secs = 20.0
        val n = (secs * rate).toInt()

        data class Sig(val label: String, val s: FloatArray, val voicedOnly: Boolean)

        val (speech, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 20000, gain = 1f)
        val (speechQuiet, _) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 20000, gain = 0.33f)
        val sigs = listOf(
            Sig("pink -50dBFS", pink(n, -50.0), false),
            Sig("pink -45dBFS", pink(n, -45.0), false),
            Sig("pink -40dBFS", pink(n, -40.0), false),
            Sig("white -45dBFS", white(n, -45.0), false),
            Sig("speech g1.00", speech, true),
            Sig("speech g0.33", speechQuiet, true),
        )
        val hop = Math.round(rate * AudioAnalysisTap.HOP_MS / 1000f)

        // ---- the candidate (the simulation's fidelity is NoiseGateSimulationTest's to assert)
        for (k in listOf(0f, 1.0f, 1.5f, 2.0f)) {
            for (nlScale in listOf(1.0f, 0.35f)) {
                println("")
                println("### subtractK=$k  NOISE_LIVE x $nlScale   (band p50 | drawn p50 | band p90)")
                for (sig in sigs) {
                    val (raws, _) = trace(sig.s, rate, cal)
                    val sim = simulate(raws, dt, cal, k, noiseLiveScale = nlScale)
                    val scored = if (sig.voicedOnly) {
                        sim.filterIndexed { t, _ ->
                            val centre = t * hop + hop / 2
                            t * dt > 2.0 && voiced.any { centre in it }
                        }
                    } else tail(sim, 14.0, dt)
                    val sb = StringBuilder("    ${"%-14s".format(sig.label)}")
                    for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(scored, b, 50.0))} ")
                    sb.append("| ")
                    for (b in 0 until AuroraAnalyser.BANDS) {
                        sb.append("${f4(Aurora.displayCurve(pct(scored, b, 50.0).toFloat()).toDouble())} ")
                    }
                    sb.append("| ")
                    for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(scored, b, 90.0))} ")
                    println(sb)
                }
            }
        }
    }

    /**
     * The REAL candidate: a per-band noise-floor tracker feeding a GLOBAL gate with hysteresis and
     * hold, multiplying the analyser's output. Plus optional floor subtraction on the numerator.
     */
    private fun simulateGated(
        raws: List<FloatArray>,
        dt: Float,
        cal: AuroraCalibration,
        openK: Float,
        closeK: Float,
        holdSec: Float,
        closeTau: Float,
        openTau: Float,
        subtractK: Float,
        floorUpTau: Float = 12f,
        floorDownTau: Float = 0.5f,
        perBand: Boolean = false,
    ): List<FloatArray> {
        val peaks = FloatArray(AuroraAnalyser.BANDS) { cal.peakInit[it] }
        val fastRefs = FloatArray(AuroraAnalyser.BANDS) { cal.peakInit[it] }
        val floorTrack = FloatArray(AuroraAnalyser.BANDS)
        val gain = FloatArray(AuroraAnalyser.BANDS) { 0f }
        val holdLeft = FloatArray(AuroraAnalyser.BANDS) { 0f }
        var seeded = false
        val out = ArrayList<FloatArray>()
        val upA = 1f - exp(-dt / floorUpTau)
        val downA = 1f - exp(-dt / floorDownTau)
        val openA = 1f - exp(-dt / openTau)
        val closeA = 1f - exp(-dt / closeTau)
        var elapsedMs = 0f
        for (r in raws) {
            val warmScale = AuroraAnalyser.WARM_FLOOR +
                (1f - AuroraAnalyser.WARM_FLOOR) * (elapsedMs / AuroraAnalyser.WARMUP_MS).coerceIn(0f, 1f)
            val acquiring = elapsedMs < AuroraAnalyser.WARMUP_MS
            val fall = if (acquiring) 1f - exp(-dt / cal.acquireTauSec) else 0f
            val steadyRise = if (acquiring) 0f else 1f - exp(-dt / cal.steadyRiseTauSec)
            val steadyDecay = exp(-dt / cal.steadyDecayTauSec)
            val fastFall = exp(-dt / cal.fastReleaseTauSec)
            // floors first, then the gate decision (global = the loudest band's excess).
            var globalExcess = 0f
            for (b in 0 until AuroraAnalyser.BANDS) {
                val vRaw = r[b]
                if (!seeded) floorTrack[b] = vRaw
                else {
                    val a = if (vRaw < floorTrack[b]) downA else upA
                    floorTrack[b] += (vRaw - floorTrack[b]) * a
                }
                val e = if (floorTrack[b] > 0f) vRaw / floorTrack[b] else 0f
                if (e > globalExcess) globalExcess = e
            }
            val row = FloatArray(AuroraAnalyser.BANDS)
            for (b in 0 until AuroraAnalyser.BANDS) {
                val vRaw = r[b]
                val excess = if (perBand) {
                    if (floorTrack[b] > 0f) vRaw / floorTrack[b] else 0f
                } else globalExcess
                if (excess >= openK) holdLeft[b] = holdSec
                else if (excess >= closeK) { /* sustain the current hold */ }
                else holdLeft[b] = (holdLeft[b] - dt).coerceAtLeast(0f)
                val target = if (holdLeft[b] > 0f) 1f else 0f
                gain[b] += (target - gain[b]) * (if (target > gain[b]) openA else closeA)

                val v = if (subtractK <= 0f) vRaw else (vRaw - subtractK * floorTrack[b]).coerceAtLeast(0f)
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
                val denom = maxOf(peaks[b], cal.noiseLive[b]) * cal.headroom
                row[b] = (AuroraAnalyser.softKnee(v / denom, cal.knee) * warmScale * gain[b]).coerceIn(0f, 1f)
            }
            seeded = true
            elapsedMs += dt * 1000f
            out.add(row)
        }
        return out
    }

    @Test
    fun `candidate hysteretic gate`() {
        val rate = 16000
        val cal = AuroraCalibration.UNPROCESSED
        val dt = AudioAnalysisTap.HOP_MS / 1000f
        val n = (20.0 * rate).toInt()
        val (speech, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 20000, gain = 1f)
        val (speechQ, _) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 20000, gain = 0.33f)
        val hop = Math.round(rate * AudioAnalysisTap.HOP_MS / 1000f)
        val sigs = listOf(
            Triple("pink -50dBFS", pink(n, -50.0), false),
            Triple("pink -45dBFS", pink(n, -45.0), false),
            Triple("pink -40dBFS", pink(n, -40.0), false),
            Triple("white -45dBFS", white(n, -45.0), false),
            Triple("speech g1.00", speech, true),
            Triple("speech g0.33", speechQ, true),
        )
        for (cfg in listOf(
            floatArrayOf(4.0f, 2.5f, 0.20f, 0.060f, 0.008f, 1.0f, 0f),
            floatArrayOf(5.0f, 3.0f, 0.25f, 0.060f, 0.008f, 1.0f, 0f),
            floatArrayOf(6.0f, 3.5f, 0.25f, 0.060f, 0.008f, 1.0f, 0f),
            floatArrayOf(6.0f, 3.5f, 0.25f, 0.060f, 0.008f, 0.0f, 0f),
            floatArrayOf(8.0f, 4.0f, 0.30f, 0.060f, 0.008f, 1.0f, 0f),
        )) {
            val perBand = cfg[6] > 0.5f
            println("")
            println("### GATE openK=${cfg[0]} closeK=${cfg[1]} hold=${cfg[2]}s closeTau=${cfg[3]}s subtractK=${cfg[5]} ${if (perBand) "PER-BAND" else "GLOBAL"}")
            println("    signal        | band p50                        | drawn p50                       | band p90")
            for ((label, s, voicedOnly) in sigs) {
                val (raws, _) = trace(s, rate, cal)
                val sim = simulateGated(raws, dt, cal, cfg[0], cfg[1], cfg[2], cfg[3], cfg[4], cfg[5], perBand = perBand)
                val scored = if (voicedOnly) {
                    sim.filterIndexed { t, _ ->
                        val centre = t * hop + hop / 2
                        t * dt > 2.0 && voiced.any { centre in it }
                    }
                } else tail(sim, 14.0, dt)
                val sb = StringBuilder("    ${"%-14s".format(label)}")
                for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(scored, b, 50.0))} ")
                sb.append("| ")
                for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(Aurora.displayCurve(pct(scored, b, 50.0).toFloat()).toDouble())} ")
                sb.append("| ")
                for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(scored, b, 90.0))} ")
                println(sb)
            }
        }
    }

    /** The DISCRIMINATOR's margin: v/floorTrack percentiles for noise vs speech. */
    @Test
    fun `gate discriminator margin`() {
        val rate = 16000
        val cal = AuroraCalibration.UNPROCESSED
        val dt = AudioAnalysisTap.HOP_MS / 1000f
        val n = (20.0 * rate).toInt()
        val (speech, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 20000, gain = 1f)
        val (speechQ, _) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 20000, gain = 0.33f)
        val hop = Math.round(rate * AudioAnalysisTap.HOP_MS / 1000f)
        println("")
        println("### GATE DISCRIMINATOR  max_b(v[b]/floorTrack[b]), floor tracker up-tau 12s / down-tau 0.5s")
        println("    signal          p50     p90     p99     max")
        for ((label, sig, voicedOnly) in listOf(
            Triple("pink -50dBFS", pink(n, -50.0), false),
            Triple("pink -45dBFS", pink(n, -45.0), false),
            Triple("pink -40dBFS", pink(n, -40.0), false),
            Triple("white -45dBFS", white(n, -45.0), false),
            Triple("speech g1.00", speech, true),
            Triple("speech g0.33", speechQ, true),
        )) {
            val (raws, _) = trace(sig, rate, cal)
            val floorTrack = FloatArray(AuroraAnalyser.BANDS)
            val upA = 1f - exp(-dt / 12f)
            val downA = 1f - exp(-dt / 0.5f)
            var seeded = false
            val ex = ArrayList<Float>()
            for ((t, r) in raws.withIndex()) {
                var g = 0f
                for (b in 0 until AuroraAnalyser.BANDS) {
                    val v = r[b]
                    if (!seeded) floorTrack[b] = v
                    else floorTrack[b] += (v - floorTrack[b]) * (if (v < floorTrack[b]) downA else upA)
                    val e = if (floorTrack[b] > 0f) v / floorTrack[b] else 0f
                    if (e > g) g = e
                }
                seeded = true
                val centre = t * hop + hop / 2
                val keep = if (voicedOnly) t * dt > 2.0 && voiced.any { centre in it } else t * dt > 14.0
                if (keep) ex.add(g)
            }
            val s2 = ex.sorted()
            fun q(p: Double) = s2[Math.round(p / 100.0 * (s2.size - 1)).toInt().coerceIn(0, s2.size - 1)]
            println("    ${"%-14s".format(label)} ${String.format("%7.2f %7.2f %7.2f %7.2f", q(50.0), q(90.0), q(99.0), s2.last())}")
        }
    }

    /**
     * THE FAILURE MODE: a GAPLESS passage. If the floor tracker keeps rising while the gate is
     * open it eventually learns the speech as the floor and the gate shuts mid-monologue.
     * Measured with and without freezing the tracker's RISE while the gate is open.
     */
    @Test
    fun `gapless passage does not close the gate`() {
        val rate = 16000
        val cal = AuroraCalibration.UNPROCESSED
        val dt = AudioAnalysisTap.HOP_MS / 1000f
        val secs = 30.0
        val n = (secs * rate).toInt()
        // A gapless voiced signal: four probe-centred carriers, amplitude-modulated at 4 Hz but
        // never reaching zero (a sustained vowel-like passage with no inter-word silence), riding
        // on a -50 dBFS pink floor.
        val floorNoise = pink(n, -50.0)
        val gapless = FloatArray(n) { i ->
            val tt = i.toDouble() / rate
            val am = 0.65 + 0.35 * kotlin.math.cos(2.0 * Math.PI * 4.0 * tt)
            var c = 0.0
            for (f in doubleArrayOf(150.0, 650.0, 2000.0, 5800.0)) c += kotlin.math.sin(2.0 * Math.PI * f * i / rate)
            ((c * 0.25 * am * 0.4) + floorNoise[i]).toFloat().coerceIn(-1f, 1f)
        }
        for (freeze in listOf(false, true)) {
            val (raws, _) = trace(gapless, rate, cal)
            val floorTrack = FloatArray(AuroraAnalyser.BANDS)
            val upA = 1f - exp(-dt / 12f)
            val downA = 1f - exp(-dt / 0.5f)
            var seeded = false
            var hold = 0f
            var gain = 0f
            val openA = 1f - exp(-dt / 0.008f)
            val closeA = 1f - exp(-dt / 0.060f)
            val gains = ArrayList<Float>()
            for (r in raws) {
                var g = 0f
                for (b in 0 until AuroraAnalyser.BANDS) {
                    val v = r[b]
                    if (!seeded) floorTrack[b] = v
                    else {
                        val rising = v > floorTrack[b]
                        // freeze the RISE while the gate is open — the minimum-statistics rule.
                        val a = if (!rising) downA else if (freeze && gain > 0.5f) 0f else upA
                        floorTrack[b] += (v - floorTrack[b]) * a
                    }
                    val e = if (floorTrack[b] > 0f) v / floorTrack[b] else 0f
                    if (e > g) g = e
                }
                seeded = true
                if (g >= 6.0f) hold = 0.25f else if (g < 3.5f) hold = (hold - dt).coerceAtLeast(0f)
                val target = if (hold > 0f) 1f else 0f
                gain += (target - gain) * (if (target > gain) openA else closeA)
                gains.add(gain)
            }
            val perSec = (1.0 / dt).toInt()
            val sb = StringBuilder()
            for (sec in 0 until secs.toInt()) {
                val w = gains.subList((sec * perSec).coerceAtMost(gains.size), ((sec + 1) * perSec).coerceAtMost(gains.size))
                sb.append(if (w.isEmpty()) "-" else String.format("%.0f", w.average() * 9))
            }
            println("")
            println("### GAPLESS 30 s, floor-rise-frozen-while-open=$freeze")
            println("    per-second mean gate gain (0..9): $sb")
            println("    gate open fraction over the last 10 s: ${String.format("%.3f", gains.takeLast(perSec * 10).average())}")
        }
    }

    /**
     * MINIMUM-STATISTICS tracker: floor[b] = min over a sliding window of a short-smoothed v.
     * No seeding pathology (any pause inside the window finds the floor) and it cannot learn
     * speech unless the window contains no pause at all.
     */
    private fun minStatExcess(
        raws: List<FloatArray>, dt: Float, windowSec: Float, smoothSec: Float,
    ): Pair<List<Float>, List<FloatArray>> {
        val w = Math.round(windowSec / dt).coerceAtLeast(1)
        val a = 1f - exp(-dt / smoothSec)
        val sm = FloatArray(AuroraAnalyser.BANDS)
        val ring = Array(AuroraAnalyser.BANDS) { FloatArray(w) { Float.MAX_VALUE } }
        var head = 0
        var filled = 0
        var seeded = false
        val ex = ArrayList<Float>()
        val floors = ArrayList<FloatArray>()
        for (r in raws) {
            var g = 0f
            val fl = FloatArray(AuroraAnalyser.BANDS)
            for (b in 0 until AuroraAnalyser.BANDS) {
                sm[b] = if (!seeded) r[b] else sm[b] + (r[b] - sm[b]) * a
                ring[b][head] = sm[b]
                var m = Float.MAX_VALUE
                for (k in 0 until maxOf(1, filled + 1)) if (ring[b][k] < m) m = ring[b][k]
                fl[b] = m
                val e = if (m > 0f) r[b] / m else 0f
                if (e > g) g = e
            }
            seeded = true
            head = (head + 1) % w
            if (filled < w - 1) filled++
            ex.add(g)
            floors.add(fl)
        }
        return ex to floors
    }

    @Test
    fun `minimum statistics tracker`() {
        val rate = 16000
        val cal = AuroraCalibration.UNPROCESSED
        val dt = AudioAnalysisTap.HOP_MS / 1000f
        val secs = 30.0
        val n = (secs * rate).toInt()
        val floorNoise = pink(n, -50.0)
        val gapless = FloatArray(n) { i ->
            val tt = i.toDouble() / rate
            val am = 0.65 + 0.35 * kotlin.math.cos(2.0 * Math.PI * 4.0 * tt)
            var c = 0.0
            for (f in doubleArrayOf(150.0, 650.0, 2000.0, 5800.0)) c += kotlin.math.sin(2.0 * Math.PI * f * i / rate)
            ((c * 0.25 * am * 0.4) + floorNoise[i]).toFloat().coerceIn(-1f, 1f)
        }
        val (speech, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 30000, gain = 0.33f)
        val hop = Math.round(rate * AudioAnalysisTap.HOP_MS / 1000f)
        println("")
        println("### MINIMUM-STATISTICS excess (window 4 s, smoothing 100 ms)")
        println("    signal                     p05     p50     p90     p99     max")
        for ((label, sig, voicedOnly) in listOf(
            Triple("pink -50dBFS", pink(n, -50.0), false),
            Triple("pink -40dBFS", pink(n, -40.0), false),
            Triple("white -45dBFS", white(n, -45.0), false),
            Triple("GAPLESS from frame 0", gapless, false),
            Triple("speech g0.33 (voiced)", speech, true),
        )) {
            val (raws, _) = trace(sig, rate, cal)
            val (ex, _) = minStatExcess(raws, dt, 4f, 0.10f)
            val kept = ex.filterIndexed { t, _ ->
                val centre = t * hop + hop / 2
                if (voicedOnly) t * dt > 5.0 && voiced.any { centre in it } else t * dt > 5.0
            }.sorted()
            fun q(pp: Double) = kept[Math.round(pp / 100.0 * (kept.size - 1)).toInt().coerceIn(0, kept.size - 1)]
            println("    ${"%-24s".format(label)} ${String.format("%7.2f %7.2f %7.2f %7.2f %7.2f", q(5.0), q(50.0), q(90.0), q(99.0), kept.last())}")
        }
    }

    /** THE RECOMMENDED DESIGN, end to end: min-stat floor + hysteretic gate, gate starts OPEN. */
    @Test
    fun `recommended design end to end`() {
        val rate = 16000
        val cal = AuroraCalibration.UNPROCESSED
        val dt = AudioAnalysisTap.HOP_MS / 1000f
        val n = (30.0 * rate).toInt()
        val (speech, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 30000, gain = 1f)
        val (speechQ, _) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 30000, gain = 0.33f)
        val floorNoise = pink(n, -50.0)
        val gapless = FloatArray(n) { i ->
            val tt = i.toDouble() / rate
            val am = 0.65 + 0.35 * kotlin.math.cos(2.0 * Math.PI * 4.0 * tt)
            var c = 0.0
            for (f in doubleArrayOf(150.0, 650.0, 2000.0, 5800.0)) c += kotlin.math.sin(2.0 * Math.PI * f * i / rate)
            ((c * 0.25 * am * 0.4) + floorNoise[i]).toFloat().coerceIn(-1f, 1f)
        }
        val hop = Math.round(rate * AudioAnalysisTap.HOP_MS / 1000f)
        val openK = 6.0f; val closeK = 3.5f; val holdSec = 0.25f
        val openA = 1f - exp(-dt / 0.008f); val closeA = 1f - exp(-dt / 0.060f)
        println("")
        println("### RECOMMENDED: min-stat(4s,100ms) floor, openK=$openK closeK=$closeK hold=${holdSec}s, gate starts OPEN")
        println("    signal            open%  | band p50                        | drawn p50                       | band p90")
        for ((label, sig, voicedOnly) in listOf(
            Triple("digital silence", FloatArray(n), false),
            Triple("pink -50dBFS", pink(n, -50.0), false),
            Triple("pink -45dBFS", pink(n, -45.0), false),
            Triple("pink -40dBFS", pink(n, -40.0), false),
            Triple("white -45dBFS", white(n, -45.0), false),
            Triple("GAPLESS tone", gapless, false),
            Triple("speech g1.00", speech, true),
            Triple("speech g0.33", speechQ, true),
        )) {
            val (raws, outs) = trace(sig, rate, cal)
            val (ex, _) = minStatExcess(raws, dt, 4f, 0.10f)
            var hold = holdSec
            var gain = 1f
            val gated = ArrayList<FloatArray>()
            val gains = ArrayList<Float>()
            for (t in outs.indices) {
                val e = ex[t]
                if (e >= openK) hold = holdSec else if (e < closeK) hold = (hold - dt).coerceAtLeast(0f)
                val target = if (hold > 0f) 1f else 0f
                gain += (target - gain) * (if (target > gain) openA else closeA)
                gains.add(gain)
                gated.add(FloatArray(AuroraAnalyser.BANDS) { b -> outs[t][b] * gain })
            }
            val idx = outs.indices.filter { t ->
                val centre = t * hop + hop / 2
                if (voicedOnly) t * dt > 6.0 && voiced.any { centre in it } else t * dt > 14.0
            }
            val scored = idx.map { gated[it] }
            val openPct = idx.map { gains[it] }.average() * 100
            val sb = StringBuilder("    ${"%-17s".format(label)}${String.format("%5.1f", openPct)}  | ")
            for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(scored, b, 50.0))} ")
            sb.append("| ")
            for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(Aurora.displayCurve(pct(scored, b, 50.0).toFloat()).toDouble())} ")
            sb.append("| ")
            for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(scored, b, 90.0))} ")
            println(sb)
        }
    }
}
