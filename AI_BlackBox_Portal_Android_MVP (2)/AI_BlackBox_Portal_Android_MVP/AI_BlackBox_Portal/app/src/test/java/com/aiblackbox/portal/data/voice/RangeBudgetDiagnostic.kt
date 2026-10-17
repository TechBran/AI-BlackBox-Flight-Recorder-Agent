package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import com.aiblackbox.portal.ui.components.aurora.AuroraEnvelope
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import com.aiblackbox.portal.ui.components.wave.styles.ParticleWave
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.pow
import kotlin.math.sin

/**
 * MEASUREMENT-ONLY diagnostic (no assertions that constrain production): where the dynamic range
 * goes, stage by stage, on the SHIPPED COMPOSER PATH.
 *
 * Opt-in, like every *Diagnostic — excluded from testDebugUnitTest (see app/build.gradle). Run:
 *   ./gradlew :app:testDebugUnitTest --offline -PincludeDiagnostics --tests '*RangeBudgetDiagnostic*' -i
 *
 * Two sections that used to live here are ASSERTED now, in the unit gate: the tap equivalence
 * (TapEquivalenceTest) and the gates re-scored at the shipped window (WaveformDynamicsGatesTest).
 *
 * The shipped composer path as of M0-M8 is:
 *   AudioRecord(UNPROCESSED|VOICE_RECOGNITION, 24 kHz) -> MicBuffer read (~40 ms)
 *     -> AudioAnalysisTap(hop 20 ms)          <- THE BAND WINDOW IS THE HOP, so 20 ms, not 40
 *       -> AuroraAnalyser.analyze             <- AGC, HEADROOM, soft knee, warm scale
 *         -> StateFlow<FloatArray> bands
 *           -> AuroraVoice -> AuroraEnvelope  <- Aurora.displayCurve + attack/release
 *             -> Aurora.amplitude             <- LINEAR in drive: no range effect
 */
class RangeBudgetDiagnostic {

    private val SR = 24000
    private val HOP_MS = AudioAnalysisTap.HOP_MS.toInt()          // 20
    private val CAL = AuroraCalibration.DEFAULT                   // the processed profile; UNPROCESSED differs (floor + expander)
    private val DISPLAY_HZ = 120                                  // Fold 6

    // ------------------------------------------------------------------ statistics

    private fun pct(sorted: List<Float>, p: Double): Double {
        if (sorted.isEmpty()) return Double.NaN
        val i = Math.round(p / 100.0 * (sorted.size - 1)).toInt().coerceIn(0, sorted.size - 1)
        return sorted[i].toDouble()
    }

    private class Stat(xs: List<Float>) {
        val s = xs.sorted()
        val n = xs.size
        val p10: Double
        val p50: Double
        val p90: Double
        val p99: Double
        val mean: Double
        val max: Double

        init {
            fun q(p: Double): Double {
                if (s.isEmpty()) return Double.NaN
                val i = Math.round(p / 100.0 * (s.size - 1)).toInt().coerceIn(0, s.size - 1)
                return s[i].toDouble()
            }
            p10 = q(10.0); p50 = q(50.0); p90 = q(90.0); p99 = q(99.0)
            mean = if (xs.isEmpty()) Double.NaN else xs.sumOf { it.toDouble() } / xs.size
            max = if (s.isEmpty()) Double.NaN else s.last().toDouble()
        }

        val swing: Double get() = if (p50 > 0.0) p90 / p50 else Double.POSITIVE_INFINITY
        val fullRange: Double get() = if (p10 > 0.0) p90 / p10 else Double.POSITIVE_INFINITY
    }

    private fun row(label: String, st: Stat): String = String.format(
        "  %-34s p10=%.4f p50=%.4f p90=%.4f max=%.4f  swing(p90/p50)=%6.3f  p90/p10=%7.3f",
        label, st.p10, st.p50, st.p90, st.max, st.swing, st.fullRange,
    )

    // ------------------------------------------------------------------ signal

    private fun toPcm16(samples: FloatArray, from: Int, len: Int): ByteArray {
        val b = ByteArray(len * 2)
        for (k in 0 until len) {
            val x = (samples[from + k] * 32767f).toInt().coerceIn(-32768, 32767)
            b[2 * k] = (x and 0xFF).toByte()
            b[2 * k + 1] = ((x shr 8) and 0xFF).toByte()
        }
        return b
    }

    /** One analysed hop, with every intermediate the band channel computes. */
    private class Hop(
        val sampleCentre: Int,
        val elapsedMs: Float,
        val raw: FloatArray,      // v, pre-AGC band energy
        val ref: FloatArray,      // peaks[b] AFTER this frame's update (the slow session reference)
        val fast: FloatArray,     // fastRefs[b] (the fast follower)
        val out: FloatArray,      // what analyze() returns
        val warm: Float,
    )

    /**
     * Feed the clip through a real analyser at the SHIPPED hop, on the audio clock, exactly as
     * [AudioAnalysisTap] drives it (contiguous non-overlapping windows of hopSamples, dt = hop).
     * TapEquivalenceTest proves that feed bit-identical to routing through the real tap.
     */
    private fun run(clip: FloatArray, hopMs: Int = HOP_MS): List<Hop> {
        val hopSamples = SR * hopMs / 1000
        var nowNs = 0L
        val analyser = AuroraAnalyser(SR, { nowNs }, CAL)
        val hops = ArrayList<Hop>()
        var idx = 0
        val raw = FloatArray(AuroraAnalyser.BANDS)
        while (idx + hopSamples <= clip.size) {
            val out = analyser.analyze(toPcm16(clip, idx, hopSamples), hopSamples * 2, raw)
            hops.add(
                Hop(
                    sampleCentre = idx + hopSamples / 2,
                    elapsedMs = nowNs / 1e6f,
                    raw = raw.copyOf(),
                    ref = analyser.peaks.copyOf(),
                    fast = analyser.fastRefs.copyOf(),
                    out = out.copyOf(),
                    warm = analyser.warmScale,
                ),
            )
            nowNs += hopMs * 1_000_000L
            idx += hopSamples
        }
        return hops
    }

    private fun voicedPostWarmup(hops: List<Hop>, voiced: List<IntRange>): List<Hop> =
        hops.filter { h -> h.elapsedMs >= AuroraAnalyser.WARMUP_MS && voiced.any { h.sampleCentre in it } }

    // =========================================================================
    // 1. STAGE-BY-STAGE RANGE BUDGET
    // =========================================================================

    @Test
    fun stageBudget() {
        val (clip, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = SR)
        for (hopMs in listOf(HOP_MS, 40)) {
            val hops = run(clip, hopMs)
            val v = voicedPostWarmup(hops, voiced)
            println("")
            println("=== 1. STAGE BUDGET — composer path, ${hopMs} ms analysis window, ${v.size} voiced frames")
            if (hopMs == HOP_MS) println("    (${hopMs} ms == AudioAnalysisTap.HOP_MS: THE SHIPPED WINDOW)")
            else println("    (${hopMs} ms == the pre-tap mic read, i.e. what M6 was measured at)")

            for (b in 0 until AuroraAnalyser.BANDS) {
                val raw = v.map { it.raw[b] }
                val ref = v.map { it.ref[b] }
                val fast = v.map { it.fast[b] }
                val nSlow = v.map { it.raw[b] / it.ref[b] }
                val nFloor = v.map { it.raw[b] / maxOf(it.ref[b], CAL.noiseLive[b]) }
                val ratio = v.map { it.raw[b] / (maxOf(it.ref[b], CAL.noiseLive[b]) * CAL.headroom) }
                val knee = ratio.map { AuroraAnalyser.softKnee(it, CAL.knee) }
                val out = v.map { it.out[b] }
                val curved = out.map { Aurora.displayCurve(it) }

                println("")
                println("  --- BAND $b " + if (b == 0) "(DOMINANT)" else "")
                println(row("S0 raw band energy v", Stat(raw)))
                println(row("   slow reference peaks[b]", Stat(ref)))
                println(row("   fast follower fastRefs[b]", Stat(fast)))
                println(row("S1 v / slowRef  (AGC)", Stat(nSlow)))
                println(row("S2 v / max(ref,NOISE_LIVE)", Stat(nFloor)))
                println(row("S3 / HEADROOM=${CAL.headroom}", Stat(ratio)))
                println(row("S4 GAMMA (analyser: none)", Stat(ratio)))
                println(row("S5 softKnee(knee=${CAL.knee})", Stat(knee)))
                println(row("S6 * warmScale = analyse() out", Stat(out)))
                println(row("S7 displayCurve (gamma 0.5)", Stat(curved)))

                val fracs = listOf(
                    "S0->S1 AGC slow-ref normalisation" to Stat(raw).swing / Stat(nSlow).swing,
                    "S1->S2 NOISE_LIVE floor" to Stat(nSlow).swing / Stat(nFloor).swing,
                    "S2->S3 HEADROOM (constant divisor)" to Stat(nFloor).swing / Stat(ratio).swing,
                    "S3->S5 soft knee" to Stat(ratio).swing / Stat(knee).swing,
                    "S5->S6 warm scale" to Stat(knee).swing / Stat(out).swing,
                    "S6->S7 DISPLAY_GAMMA 0.5" to Stat(out).swing / Stat(curved).swing,
                )
                println("    swing cost per stage (>1 destroys range):")
                for ((name, f) in fracs) println(String.format("      %-38s x%.4f", name, f))
            }

            // --- the renderer envelope, run at the real display rate on the real frames
            val env = renderEnvelope(v, hops, DISPLAY_HZ)
            println("")
            println("  --- RENDERER ENVELOPE (AuroraEnvelope @ ${DISPLAY_HZ} fps, restLevel=${Aurora.BASELINE})")
            for (b in 0 until AuroraAnalyser.BANDS) {
                val pre = env.pre[b]
                val post = env.post[b]
                println(row("S7 target (pre-envelope) band $b", Stat(pre)))
                println(row("S8 drawn drive (post-envelope) $b", Stat(post)))
                println(
                    String.format(
                        "      envelope swing cost band %d: x%.4f   (p50 %.4f -> %.4f, p90 %.4f -> %.4f)",
                        b, Stat(pre).swing / Stat(post).swing,
                        Stat(pre).p50, Stat(post).p50, Stat(pre).p90, Stat(post).p90,
                    ),
                )
            }

            // --- the scalar "overall level" every scrolling style draws
            val lvlOut = v.map { WaveformFeed.levelOf(AuroraVoice(AuroraSpeaker.HUMAN, it.out)) }
            println("")
            println(row("WaveformFeed.levelOf(analyser out)", Stat(lvlOut)))
            println(
                "    (the 7 styles with no perceptual curve — Classic, Cinematic, MirroredPulse, " +
                    "SharpLine, GlowingRibbon, SegmentedEq, CircularOrb — draw THIS, linearly)",
            )

            // headline
            val b0out = Stat(v.map { it.out[0] })
            val b0drawn = Stat(env.post[0])
            println("")
            println(
                String.format(
                    "  HEADLINE @%dms: analyser-out swing %.3f -> drawn swing %.3f  (M6 reported 2.254 at 40 ms)",
                    hopMs, b0out.swing, b0drawn.swing,
                ),
            )
        }
    }

    private class EnvOut(val pre: Array<MutableList<Float>>, val post: Array<MutableList<Float>>)

    /**
     * Run a REAL [AuroraEnvelope] at [fps] over the analyser's 50 Hz frames — the renderer's actual
     * job — and collect the pre-envelope target and the post-envelope drawn drive at display frames
     * whose audio instant is in the scored set.
     */
    private fun renderEnvelope(scored: List<Hop>, all: List<Hop>, fps: Int): EnvOut {
        val env = AuroraEnvelope(Aurora.BASELINE)
        val dt = 1f / fps
        val pre = Array(AuroraAnalyser.BANDS) { mutableListOf<Float>() }
        val post = Array(AuroraAnalyser.BANDS) { mutableListOf<Float>() }
        val scoredSet = scored.map { it.sampleCentre }.toHashSet()
        var hopIdx = 0
        var tSec = 0.0
        while (hopIdx < all.size) {
            val hop = all[hopIdx]
            val hopEndSec = (hopIdx + 1) * (HOP_MS / 1000.0)
            val voice = AuroraVoice(AuroraSpeaker.HUMAN, hop.out)
            while (tSec < hopEndSec) {
                env.step(dt, voice)
                if (hop.sampleCentre in scoredSet) {
                    for (b in 0 until AuroraAnalyser.BANDS) {
                        pre[b].add(maxOf(Aurora.BASELINE, Aurora.displayCurve(hop.out[b])))
                        post[b].add(env.levels[b])
                    }
                }
                tSec += dt
            }
            hopIdx++
        }
        return EnvOut(pre, post)
    }

    // =========================================================================
    // 2. THE DUAL-REFERENCE AGC
    // =========================================================================

    @Test
    fun dualReference() {
        // loud (3 s) -> quiet (3 s), the transition the follower is supposed to make local.
        val loud = AuroraDynamicsHarness.speechClip(sampleRate = SR, totalMs = 4000, gain = 0.99f)
        val quiet = AuroraDynamicsHarness.speechClip(sampleRate = SR, totalMs = 4000, gain = 0.25f)
        val clip = loud.first + quiet.first
        val switchAt = loud.first.size
        val hops = run(clip)

        println("")
        println("=== 2. DUAL-REFERENCE AGC — loud(gain .99, 4 s) then quiet(gain .25, 4 s), band 0")
        println("  FAST_RELEASE_TAU_SEC=${AuroraAnalyser.FAST_RELEASE_TAU_SEC}  STEADY_RISE_TAU_SEC=${AuroraAnalyser.STEADY_RISE_TAU_SEC}  STEADY_DECAY_TAU_SEC=${AuroraAnalyser.STEADY_DECAY_TAU_SEC}")
        println(String.format("  %8s %10s %10s %10s %10s %10s", "t(ms)", "raw v", "fast", "slow", "fast/slow", "out"))
        val switchHop = hops.indexOfFirst { it.sampleCentre >= switchAt }
        for (dtMs in listOf(-500, -100, 0, 100, 200, 300, 500, 1000, 2000, 3000)) {
            val k = (switchHop + dtMs / HOP_MS).coerceIn(0, hops.size - 1)
            val h = hops[k]
            println(
                String.format(
                    "  %8d %10.5f %10.5f %10.5f %10.4f %10.4f",
                    dtMs, h.raw[0], h.fast[0], h.ref[0], h.fast[0] / h.ref[0], h.out[0],
                ),
            )
        }

        // How much does each reference actually MOVE inside a settled passage?
        val steady = hops.filter { it.elapsedMs in 1500f..3800f }
        for (b in 0 until AuroraAnalyser.BANDS) {
            val rawS = Stat(steady.map { it.raw[b] })
            val fastS = Stat(steady.map { it.fast[b] })
            val slowS = Stat(steady.map { it.ref[b] })
            println(
                String.format(
                    "  band %d inside ONE settled passage: raw p90/p10=%.3f  fast p90/p10=%.4f  slow p90/p10=%.4f  " +
                        "fast/slow median=%.4f",
                    b, rawS.fullRange, fastS.fullRange, slowS.fullRange,
                    steady.map { it.fast[b] / it.ref[b] }.sorted().let { it[it.size / 2] },
                ),
            )
        }
        println("  (a follower that produced LOCAL contrast would move a lot INSIDE a passage;")
        println("   a follower the slow reference has fully absorbed shows fast/slow pinned near a constant.)")

        // The counterfactual: what does the output look like with NO fast follower (M1 behaviour)?
        println("")
        println("  COUNTERFACTUAL — the same clip with the follower's release set to 3 different taus:")
        for (tau in listOf(0.05f, 0.30f, 1.0f)) {
            val cal = AuroraCalibration("probe-$tau", fastReleaseTauSec = tau)
            val h2 = runWith(clip, cal)
            val vv = h2.filter { it.elapsedMs >= AuroraAnalyser.WARMUP_MS }
            val st = Stat(vv.map { it.out[0] })
            println(String.format("    fastReleaseTau=%.3fs -> band0 p50=%.4f p90=%.4f swing=%.3f", tau, st.p50, st.p90, st.swing))
        }
    }

    private fun runWith(clip: FloatArray, cal: AuroraCalibration, hopMs: Int = HOP_MS): List<Hop> {
        val hopSamples = SR * hopMs / 1000
        var nowNs = 0L
        val analyser = AuroraAnalyser(SR, { nowNs }, cal)
        val hops = ArrayList<Hop>()
        var idx = 0
        val raw = FloatArray(AuroraAnalyser.BANDS)
        while (idx + hopSamples <= clip.size) {
            val out = analyser.analyze(toPcm16(clip, idx, hopSamples), hopSamples * 2, raw)
            hops.add(
                Hop(idx + hopSamples / 2, nowNs / 1e6f, raw.copyOf(), analyser.peaks.copyOf(), analyser.fastRefs.copyOf(), out.copyOf(), analyser.warmScale),
            )
            nowNs += hopMs * 1_000_000L
            idx += hopSamples
        }
        return hops
    }

    // =========================================================================
    // 3. RENDERER-SIDE ENVELOPES
    // =========================================================================

    @Test
    fun rendererEnvelopes() {
        println("")
        println("=== 3. RENDERER-SIDE ENVELOPES")
        val syllableMs = 1000.0 / 4.3
        println(String.format("  connected speech syllable period @4.3 syl/s = %.1f ms", syllableMs))
        println("  Aurora.ATTACK_TAU_SEC  = ${Aurora.ATTACK_TAU_SEC}  (t90 rise = ${"%.1f".format(2.302585 * Aurora.ATTACK_TAU_SEC * 1000)} ms)")
        println("  Aurora.RELEASE_TAU_SEC = ${Aurora.RELEASE_TAU_SEC} (departed voice only)")
        for (b in 0 until AuroraAnalyser.BANDS) {
            val tau = Aurora.BAND_RELEASE_TAUS_SEC[b]
            val t90 = 2.302585 * tau * 1000
            println(
                String.format(
                    "  BAND_RELEASE_TAUS_SEC[%d] = %.3f s -> t90 fall = %6.1f ms = %.2f syllable periods %s",
                    b, tau, t90, t90 / syllableMs, if (t90 < syllableMs) "OK" else "TOO SLOW",
                ),
            )
        }
        // ParticleWave's THIRD envelope (attack 0.020 s, release 0.200 s -> t90 fall 460 ms,
        // 1.98 syllable periods) is DELETED. What it cost is measured below, kept as the
        // evidence for the deletion; the style is now a pure function of the current frame.
        println("  ParticleWave private envelope: DELETED (was tau_r 0.200 s, t90 460 ms = 1.98 syllables)")

        // measured cost of each envelope on the same shipped frames
        val (clip, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = SR)
        val hops = run(clip)
        val scored = voicedPostWarmup(hops, voiced)
        val scoredSet = scored.map { it.sampleCentre }.toHashSet()

        for (fps in listOf(60, 120)) {
            val e = renderEnvelope(scored, hops, fps)
            println(
                String.format(
                    "  @%3d fps  band0 pre-envelope swing=%.3f  post-envelope swing=%.3f  cost x%.3f",
                    fps, Stat(e.pre[0]).swing, Stat(e.post[0]).swing, Stat(e.pre[0]).swing / Stat(e.post[0]).swing,
                ),
            )
        }

        // What ParticleWave's stacked envelope USED to cost, reproduced locally from the
        // constants it carried (attack 0.020 s / release 0.200 s) on the SAME curved drive.
        // The style no longer owns these; they are restated here only so the deletion keeps
        // a number attached to it.
        val pwAttackTau = 0.020f
        val pwReleaseTau = 0.200f
        val dt = 1f / DISPLAY_HZ
        var pw = 0f
        val pwPre = mutableListOf<Float>()
        val pwPost = mutableListOf<Float>()
        var tSec = 0.0
        for ((i, h) in hops.withIndex()) {
            val target = Aurora.displayCurve(h.out[0])
            val end = (i + 1) * (HOP_MS / 1000.0)
            while (tSec < end) {
                val tau = if (target > pw) pwAttackTau else pwReleaseTau
                pw += (target - pw) * (1f - exp(-dt / tau))
                if (h.sampleCentre in scoredSet) { pwPre.add(target); pwPost.add(pw) }
                tSec += dt
            }
        }
        println(
            String.format(
                "  ParticleWave stacked envelope (DELETED; measured as it was): pre swing=%.3f -> post swing=%.3f  cost x%.3f",
                Stat(pwPre).swing, Stat(pwPost).swing, Stat(pwPre).swing / Stat(pwPost).swing,
            ),
        )

        // The feed's own decimation: 50 Hz ring vs 120 Hz display
        println("  WaveformFeed.CADENCE_HZ = ${WaveformFeed.CADENCE_HZ} (ring push rate; no smoothing, sample-and-hold)")
    }

    // =========================================================================
    // 4. THE NOISE FLOOR
    // =========================================================================

    @Test
    fun noiseFloor() {
        println("")
        println("=== 4. NOISE FLOOR — what the ribbon draws on ROOM TONE ALONE, after 4 s of speech")
        println("  NOISE_LIVE = ${CAL.noiseLive.toList()}  PEAK_FLOOR = ${CAL.peakFloor.toList()}")
        println(String.format("  %8s %10s %10s %10s %10s %10s", "dBFS", "b0 drawn", "b1 drawn", "b2 drawn", "b3 drawn", "level"))
        for (dbfs in listOf(-60, -55, -50, -45, -40, -35, -30, -25)) {
            val amp = 10.0.pow(dbfs / 20.0).toFloat()
            val (speech, _) = AuroraDynamicsHarness.speechClip(sampleRate = SR, totalMs = 4000)
            var rng = 0x1234_5EEDL
            val quiet = FloatArray(SR * 6) {
                rng = rng * 6364136223846793005L + 1442695040888963407L
                (((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f) * amp
            }
            val hops = run(speech + quiet)
            val tail = hops.filter { it.sampleCentre >= speech.size + SR * 4 }
            val drawn = FloatArray(AuroraAnalyser.BANDS) { b ->
                Stat(tail.map { Aurora.displayCurve(it.out[b]) }).p90.toFloat()
            }
            val lvl = Stat(tail.map { Aurora.displayCurve(WaveformFeed.levelOf(AuroraVoice(AuroraSpeaker.HUMAN, it.out))) }).p90
            val jitter = motion(tail.map { Aurora.displayCurve(it.out[2]) })
            println(
                String.format(
                    "  %8d %10.4f %10.4f %10.4f %10.4f %10.4f   b2 jitter/hop=%.4f",
                    dbfs, drawn[0], drawn[1], drawn[2], drawn[3], lvl, jitter,
                ),
            )
        }
        // Apples-to-apples: the ENVELOPED drawn motion per DISPLAY frame, speech vs room tone.
        println("")
        println("  ENVELOPED drawn motion per display frame (120 fps), band 2 — speech vs room tone only:")
        run {
            val (speechOnly, _) = AuroraDynamicsHarness.speechClip(sampleRate = SR)
            val speechMotion = motion(drawnAll(run(speechOnly), 4.5f, 0.80f, 0.5f, 0f, true, band = 2))
            println(String.format("    SPEECH (the whole utterance)          %.5f /frame = %.2f /sec", speechMotion, speechMotion * DISPLAY_HZ))
            for (dbfs in listOf(-60, -50, -45, -40, -35)) {
                val a = 10.0.pow(dbfs / 20.0).toFloat()
                var r = 0x777L
                val (sp, _) = AuroraDynamicsHarness.speechClip(sampleRate = SR, totalMs = 4000)
                val q = FloatArray(SR * 6) {
                    r = r * 6364136223846793005L + 1442695040888963407L
                    (((r ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f) * a
                }
                val hh = run(sp + q).filter { it.sampleCentre >= sp.size + SR * 2 }
                val m = motion(drawnAll(hh, 4.5f, 0.80f, 0.5f, 0f, true, band = 2))
                println(String.format("    ROOM TONE @ %d dBFS                  %.5f /frame = %.2f /sec  (%.0f%% of speech)", dbfs, m, m * DISPLAY_HZ, 100.0 * m / speechMotion))
            }
        }
        println("")
        println("  (drawn = Aurora.displayCurve(band), p90 over the last 2 s of silence — the height the")
        println("   sheet actually reaches. Aurora.BASELINE=${Aurora.BASELINE} is the resting sliver it should be near.)")

        // and what the noise does to the AGC reference itself
        println("")
        println("  What the room tone does to the reference (band 0), -40 dBFS case:")
        val amp = 10.0.pow(-40.0 / 20.0).toFloat()
        val (speech, _) = AuroraDynamicsHarness.speechClip(sampleRate = SR, totalMs = 4000)
        var rng = 0x1234_5EEDL
        val quiet = FloatArray(SR * 6) {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            (((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f) * amp
        }
        val hops = run(speech + quiet)
        for (b in 0 until AuroraAnalyser.BANDS) {
            val tail = hops.filter { it.sampleCentre >= speech.size + SR * 4 }
            println(
                String.format(
                    "    band %d: raw p50=%.5f  slowRef p50=%.5f  NOISE_LIVE=%.4f  denom=%.5f  out p50=%.4f p90=%.4f",
                    b,
                    Stat(tail.map { it.raw[b] }).p50, Stat(tail.map { it.ref[b] }).p50,
                    CAL.noiseLive[b],
                    maxOf(Stat(tail.map { it.ref[b] }).p50, CAL.noiseLive[b].toDouble()) * CAL.headroom,
                    Stat(tail.map { it.out[b] }).p50, Stat(tail.map { it.out[b] }).p90,
                ),
            )
        }
    }

    // =========================================================================
    // 5. LEVERS — predicted swing per change
    // =========================================================================

    @Test
    fun levers() {
        val (clip, voiced) = AuroraDynamicsHarness.speechClip(sampleRate = SR)
        val hops = run(clip)
        val v = voicedPostWarmup(hops, voiced)

        println("")
        println("=== 5. LEVERS — predicted DRAWN swing (band 0) and typical drawn height")
        println("  baseline: HEADROOM=${CAL.headroom} KNEE=${CAL.knee} DISPLAY_GAMMA=${Aurora.DISPLAY_GAMMA}")
        println(String.format("  %-46s %10s %10s %10s", "lever", "drawn p50", "drawn p90", "swing"))

        fun report(label: String, headroom: Float, knee: Float, gamma: Float, envelope: Boolean) {
            val ratios = v.map { it.raw[0] / (maxOf(it.ref[0], CAL.noiseLive[0]) * headroom) }
            val analyserOut = ratios.map { AuroraAnalyser.softKnee(it, knee).coerceIn(0f, 1f) }
            val drawn: List<Float> = if (!envelope) {
                analyserOut.map { it.coerceAtLeast(0f).pow(gamma) }
            } else {
                // real envelope, real display rate, on the modified analyser output
                val env = AuroraEnvelope(Aurora.BASELINE)
                val dt = 1f / DISPLAY_HZ
                val outs = mutableListOf<Float>()
                var tSec = 0.0
                for ((i, o) in analyserOut.withIndex()) {
                    val bands = FloatArray(AuroraAnalyser.BANDS) { o }
                    val voice = AuroraVoice(AuroraSpeaker.HUMAN, bands)
                    val end = (i + 1) * (HOP_MS / 1000.0)
                    // custom gamma: step the envelope on the curved target by hand
                    while (tSec < end) {
                        val target = maxOf(Aurora.BASELINE, o.coerceAtLeast(0f).pow(gamma))
                        val tau = if (target > env.levels[0]) Aurora.ATTACK_TAU_SEC else Aurora.BAND_RELEASE_TAUS_SEC[0]
                        // AuroraEnvelope.step applies its OWN gamma, so drive it manually via approach
                        env.levels[0] = Aurora.approach(env.levels[0], target, dt, tau)
                        outs.add(env.levels[0])
                        tSec += dt
                    }
                    voice.hashCode() // keep the AuroraVoice construction honest / unused-warning free
                }
                outs
            }
            val st = Stat(drawn)
            println(String.format("  %-46s %10.4f %10.4f %10.4f", label, st.p50, st.p90, st.swing))
        }

        report("BASELINE (shipped)", CAL.headroom, CAL.knee, Aurora.DISPLAY_GAMMA, true)
        report("BASELINE, no renderer envelope", CAL.headroom, CAL.knee, Aurora.DISPLAY_GAMMA, false)
        println("  --- gamma sweep (everything else shipped):")
        for (g in listOf(0.5f, 0.6f, 0.65f, 0.7f, 0.8f, 1.0f)) {
            report("DISPLAY_GAMMA=$g", CAL.headroom, CAL.knee, g, true)
        }
        println("  --- knee sweep:")
        for (k in listOf(0.80f, 0.90f, 0.95f, 0.999f)) {
            report("KNEE=$k", CAL.headroom, k, Aurora.DISPLAY_GAMMA, true)
        }
        println("  --- headroom sweep:")
        for (h in listOf(3.5f, 4.5f, 6.0f, 8.0f, 12.0f)) {
            report("HEADROOM=$h", h, CAL.knee, Aurora.DISPLAY_GAMMA, true)
        }
        println("  --- combined candidates:")
        report("HEADROOM=6 + GAMMA=0.7", 6.0f, CAL.knee, 0.7f, true)
        report("HEADROOM=8 + GAMMA=0.7 + KNEE=0.95", 8.0f, 0.95f, 0.7f, true)
        report("HEADROOM=8 + GAMMA=0.75 + KNEE=0.95", 8.0f, 0.95f, 0.75f, true)

        // What the analyser could possibly deliver: the raw signal's own swing.
        println("")
        val rawSt = Stat(v.map { it.raw[0] })
        println(String.format("  CEILING: raw band-0 energy swing p90/p50 = %.3f, p90/p10 = %.3f", rawSt.swing, rawSt.fullRange))
        println("  (no stage downstream can create range that is not in this number; gamma<1 and the knee only remove it.)")

        // AGC counterfactual: freeze the reference entirely (upper bound on what the AGC costs).
        val frozenRef = Stat(v.map { it.ref[0] }).p50.toFloat()
        val frozen = v.map { (it.raw[0] / (frozenRef * CAL.headroom)) }
            .map { AuroraAnalyser.softKnee(it, CAL.knee).coerceIn(0f, 1f) }
            .map { Aurora.displayCurve(it) }
        println(String.format("  AGC frozen at its median reference: drawn p50=%.4f p90=%.4f swing=%.4f", Stat(frozen).p50, Stat(frozen).p90, Stat(frozen).swing))
    }

    // =========================================================================
    // 6. Sanity: the preview reference the operator is comparing against
    // =========================================================================

    @Test
    fun previewReference() {
        val p = AuroraDynamicsHarness.previewDistribution(AuroraSpeaker.HUMAN)
        println("")
        println("=== 6. PREVIEW (WaveformDemoDriver) — the 17x reference")
        println(String.format("  pooled p50=%.4f p90=%.4f swing=%.3f zeroFrac=%.3f", p.pooled.p50, p.pooled.p90, p.pooled.swing, p.pooled.zeroFraction))
        for (b in 0 until AuroraAnalyser.BANDS) {
            println(String.format("  band %d p50=%.4f p90=%.4f swing=%.3f", b, p.perBand[b].p50, p.perBand[b].p90, p.perBand[b].swing))
        }
        println("  NOTE: the preview feeds the demo driver's bands STRAIGHT into the visualizer — no analyser,")
        println("  no AGC, no knee — and its bands go to TRUE ZERO between syllables, which is what makes the ratio huge.")

        // The preview's own path still passes through the renderer envelope + curve for aurora.
        val driver = com.aiblackbox.portal.ui.components.wave.WaveformDemoDriver()
        val env = AuroraEnvelope(Aurora.BASELINE)
        val fps = DISPLAY_HZ
        val drawn = mutableListOf<Float>()
        val n = (driver.timeline.periodSec * 4 * fps).toInt()
        for (f in 0 until n) {
            val t = f.toDouble() / fps
            val voice = driver.voicesAt(t).firstOrNull { it.speaker == AuroraSpeaker.HUMAN }
            env.step(1f / fps, voice)
            if (voice != null) drawn.add(env.levels[0])
        }
        println(String.format("  preview band0 AFTER the same renderer curve+envelope: p50=%.4f p90=%.4f swing=%.3f", Stat(drawn).p50, Stat(drawn).p90, Stat(drawn).swing))
    }

    // =========================================================================
    // 7. THE FLOOR, and MOTION — the two things the operator actually described
    // =========================================================================

    /** Connected-speech syllable period, ms — the yardstick every release tau is scored against. */
    private val syllableRefMs = 1000.0 / 4.3

    /** Mean |delta| per DISPLAY frame, in units of full height — "how much does it move". */
    private fun motion(xs: List<Float>): Double {
        if (xs.size < 2) return Double.NaN
        var s = 0.0
        for (i in 1 until xs.size) s += abs(xs[i] - xs[i - 1]).toDouble()
        return s / (xs.size - 1)
    }

    /** Drawn band-0 drive over EVERY post-warm-up frame (gaps included), given a shaping. */
    private fun drawnAll(
        hops: List<Hop>,
        headroom: Float,
        knee: Float,
        gamma: Float,
        expandFloor: Float,
        envelope: Boolean,
        band: Int = 0,
    ): List<Float> {
        val dt = 1f / DISPLAY_HZ
        var lvl = 0f
        val outs = mutableListOf<Float>()
        var tSec = 0.0
        for ((i, h) in hops.withIndex()) {
            val ratio = h.raw[band] / (maxOf(h.ref[band], CAL.noiseLive[band]) * headroom)
            var o = AuroraAnalyser.softKnee(ratio, knee).coerceIn(0f, 1f) * h.warm
            if (expandFloor > 0f) o = ((o - expandFloor) / (1f - expandFloor)).coerceIn(0f, 1f)
            val target = maxOf(Aurora.BASELINE, o.coerceAtLeast(0f).pow(gamma))
            val end = (i + 1) * (HOP_MS / 1000.0)
            while (tSec < end) {
                if (envelope) {
                    val tau = if (target > lvl) Aurora.ATTACK_TAU_SEC else Aurora.BAND_RELEASE_TAUS_SEC[band]
                    lvl = Aurora.approach(lvl, target, dt, tau)
                } else {
                    lvl = target
                }
                if (h.elapsedMs >= AuroraAnalyser.WARMUP_MS) outs.add(lvl)
                tSec += dt
            }
        }
        return outs
    }

    @Test
    fun floorAndMotion() {
        val (clip, _) = AuroraDynamicsHarness.speechClip(sampleRate = SR)
        val hops = run(clip)

        println("")
        println("=== 7. THE FLOOR AND THE MOTION — band 0, ALL post-warm-up frames (gaps included)")
        println("    the operator watches the whole utterance, not the voiced frames only.")
        println(
            String.format(
                "  %-44s %8s %8s %8s %8s %9s %10s",
                "shaping", "p10", "p50", "p90", "p99", "p90/p10", "motion/frm",
            ),
        )

        fun show(label: String, headroom: Float, knee: Float, gamma: Float, floor: Float, env: Boolean) {
            val d = drawnAll(hops, headroom, knee, gamma, floor, env)
            val st = Stat(d)
            println(
                String.format(
                    "  %-44s %8.4f %8.4f %8.4f %8.4f %9.3f %10.5f",
                    label, st.p10, st.p50, st.p90, st.p99, st.fullRange, motion(d),
                ),
            )
        }

        show("SHIPPED (H4.5 knee.80 gamma.50)", 4.5f, 0.80f, 0.5f, 0f, true)
        show("  ... with no renderer envelope", 4.5f, 0.80f, 0.5f, 0f, false)
        show("gamma 0.70", 4.5f, 0.80f, 0.7f, 0f, true)
        show("gamma 1.00 (linear)", 4.5f, 0.80f, 1.0f, 0f, true)
        show("expander floor 0.05", 4.5f, 0.80f, 0.5f, 0.05f, true)
        show("expander floor 0.10", 4.5f, 0.80f, 0.5f, 0.10f, true)
        show("expander floor 0.15", 4.5f, 0.80f, 0.5f, 0.15f, true)
        show("expander 0.10 + gamma 0.70", 4.5f, 0.80f, 0.7f, 0.10f, true)
        show("expander 0.15 + gamma 0.70 + H 6.0", 6.0f, 0.80f, 0.7f, 0.15f, true)
        show("expander 0.15 + gamma 0.65 + H 6.0 + knee .95", 6.0f, 0.95f, 0.65f, 0.15f, true)

        // The release ladder as a MOTION lever — the operator's "isn't moving that fluently".
        println("")
        println("  RELEASE-LADDER SWEEP (band 0, gamma .5, H4.5, knee .80; tau scaled uniformly):")
        for (scale in listOf(1.0f, 0.7f, 0.5f, 0.35f, 0.25f)) {
            val tau = Aurora.BAND_RELEASE_TAUS_SEC[0] * scale
            val dt = 1f / DISPLAY_HZ
            var lvl = 0f
            val outs = mutableListOf<Float>()
            var tSec = 0.0
            for ((i, h) in hops.withIndex()) {
                val target = maxOf(Aurora.BASELINE, Aurora.displayCurve(h.out[0]))
                val end = (i + 1) * (HOP_MS / 1000.0)
                while (tSec < end) {
                    lvl = Aurora.approach(lvl, target, dt, if (target > lvl) Aurora.ATTACK_TAU_SEC else tau)
                    if (h.elapsedMs >= AuroraAnalyser.WARMUP_MS) outs.add(lvl)
                    tSec += dt
                }
            }
            val st = Stat(outs)
            println(
                String.format(
                    "    tau x%.2f = %.4f s (t90 %5.1f ms, %.2f syllables): p10=%.4f p50=%.4f p90=%.4f  p90/p10=%6.3f  motion=%.5f (%.0f%% of preview)",
                    scale, tau, 2.302585 * tau * 1000, 2.302585 * tau * 1000 / syllableRefMs,
                    st.p10, st.p50, st.p90, st.fullRange, motion(outs), 100.0 * motion(outs) / 0.03412,
                ),
            )
        }

        // the preview, put through the identical renderer chain, as the reference
        val driver = com.aiblackbox.portal.ui.components.wave.WaveformDemoDriver()
        val env = AuroraEnvelope(Aurora.BASELINE)
        val drawn = mutableListOf<Float>()
        val n = (driver.timeline.periodSec * 4 * DISPLAY_HZ).toInt()
        for (f in 0 until n) {
            val voice = driver.voicesAt(f.toDouble() / DISPLAY_HZ).firstOrNull { it.speaker == AuroraSpeaker.HUMAN }
            env.step(1f / DISPLAY_HZ, voice)
            if (voice != null) drawn.add(env.levels[0])
        }
        val ps = Stat(drawn)
        println(
            String.format(
                "  %-44s %8.4f %8.4f %8.4f %8.4f %9.3f %10.5f",
                "PREVIEW through the same curve+envelope", ps.p10, ps.p50, ps.p90, ps.p99, ps.fullRange, motion(drawn),
            ),
        )
        println("")
        println("  NOTE the stage the '17x' preview number is taken at: the demo driver's BANDS swing 14.7-18.2x,")
        println("  which is the SAME stage as the live analyser's 3.278 — not the same stage as a drawn height.")

        // Where the floor comes from: gamma lifting vs envelope lifting.
        val raw0 = hops.filter { it.elapsedMs >= AuroraAnalyser.WARMUP_MS }.map { it.out[0] }
        println("")
        println(row("analyser out, ALL frames", Stat(raw0)))
        println(row("  after gamma 0.5 only", Stat(raw0.map { maxOf(Aurora.BASELINE, it.pow(0.5f)) })))
        println(row("  after gamma+envelope (drawn)", Stat(drawnAll(hops, 4.5f, 0.80f, 0.5f, 0f, true))))
    }

    // unused-import guards
    @Suppress("unused") private fun keepImports() {
        ln(1.0); exp(1.0); sin(1.0); PI.toString(); pct(emptyList(), 50.0)
    }
}
