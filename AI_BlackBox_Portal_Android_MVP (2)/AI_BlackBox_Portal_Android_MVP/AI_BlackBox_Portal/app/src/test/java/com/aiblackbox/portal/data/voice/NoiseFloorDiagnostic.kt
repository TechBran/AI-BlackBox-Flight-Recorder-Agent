package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AnalysisFrame
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import com.aiblackbox.portal.ui.components.aurora.AuroraSpectrum
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import org.junit.Test
import kotlin.math.abs
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * MEASUREMENT ONLY — prints, asserts nothing about production behaviour.
 *
 * Feeds silence / calibrated room noise / synthetic speech through the REAL production seam
 * ([AudioAnalysisTap], which owns the shipped [AuroraAnalyser] band channel and the raw
 * [AuroraSpectrum] bin channel) and reports what each channel renders in each case.
 */
class NoiseFloorDiagnostic {

    // ------------------------------------------------------------------ signals

    private fun silence(n: Int) = FloatArray(n)

    /** Uniform white noise, RMS-calibrated to [dbfs] (0 dBFS == 1.0 RMS). */
    private fun white(n: Int, dbfs: Double, seed: Long = 0x5EED1234L): FloatArray {
        var rng = seed
        val x = FloatArray(n) {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            ((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f
        }
        return rescale(x, dbfs)
    }

    /** Paul Kellet pink filter over white, RMS-calibrated to [dbfs]. Room tone is pink, not white. */
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
        return rescale(x, dbfs)
    }

    private fun rescale(x: FloatArray, dbfs: Double): FloatArray {
        var acc = 0.0
        for (v in x) acc += v.toDouble() * v
        val rms = sqrt(acc / x.size)
        val g = (10.0.pow(dbfs / 20.0) / rms).toFloat()
        for (i in x.indices) x[i] = (x[i] * g).coerceIn(-1f, 1f)
        return x
    }

    private fun rmsDbfs(x: FloatArray): Double {
        var acc = 0.0
        for (v in x) acc += v.toDouble() * v
        val r = sqrt(acc / x.size)
        return if (r <= 0.0) Double.NEGATIVE_INFINITY else 20.0 * kotlin.math.log10(r)
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

    // ------------------------------------------------------------------ feeding

    private class Run(val bands: MutableList<FloatArray>, val bins: MutableList<FloatArray>)

    /**
     * Feed [samples] through a real tap in [chunkMs] reads and collect the frames whose audio
     * position is at or past [scoreFromSec].
     */
    private fun feed(
        samples: FloatArray,
        sampleRate: Int,
        chunkMs: Float,
        calibration: AuroraCalibration,
        scoreFromSec: Double,
    ): Run {
        val chunkSamples = Math.round(sampleRate * chunkMs / 1000f)
        val chunkNs = (chunkMs * 1_000_000.0).toLong()
        var clock = 0L
        val tap = AudioAnalysisTap(sampleRate, calibration = calibration, nowNs = { clock })
        val run = Run(ArrayList(), ArrayList())
        val scoreFrom = (scoreFromSec * sampleRate).toLong()
        val onFrame: (AnalysisFrame) -> Unit = { f ->
            if (f.sampleOffset >= scoreFrom) {
                run.bands.add(f.bands.copyOf())
                run.bins.add(f.bins.copyOf())
            }
        }
        var i = 0
        while (i < samples.size) {
            val len = minOf(chunkSamples, samples.size - i)
            tap.offer(toPcm16(samples, i, len), len * 2, onFrame)
            clock += chunkNs
            i += len
        }
        return run
    }

    /** RAW (pre-auto-gain) band energies, from a standalone analyser fed the tap's own hop. */
    private fun rawBands(
        samples: FloatArray,
        sampleRate: Int,
        calibration: AuroraCalibration,
        scoreFromSec: Double,
    ): List<FloatArray> {
        val hop = Math.round(sampleRate * AudioAnalysisTap.HOP_MS / 1000f)
        var clock = 0L
        val a = AuroraAnalyser(sampleRate, { clock }, calibration)
        val out = ArrayList<FloatArray>()
        val raw = FloatArray(AuroraAnalyser.BANDS)
        var i = 0
        while (i + hop <= samples.size) {
            a.analyze(toPcm16(samples, i, hop), hop * 2, raw)
            if (i >= scoreFromSec * sampleRate) out.add(raw.copyOf())
            clock += (AudioAnalysisTap.HOP_MS * 1_000_000.0).toLong()
            i += hop
        }
        return out
    }

    // ------------------------------------------------------------------ stats

    private fun pct(sorted: List<Float>, p: Double): Double {
        if (sorted.isEmpty()) return Double.NaN
        val i = Math.round(p / 100.0 * (sorted.size - 1)).toInt().coerceIn(0, sorted.size - 1)
        return sorted[i].toDouble()
    }

    private fun col(run: List<FloatArray>, idx: Int): List<Float> = run.map { it[idx] }.sorted()

    private fun f3(v: Double) = String.format("%.3f", v)
    private fun f4(v: Double) = String.format("%.4f", v)

    /** Mean |x[t] - x[t-1]| per frame — the "twitch" measure. */
    private fun jitter(run: List<FloatArray>, idx: Int): Double {
        if (run.size < 2) return Double.NaN
        var acc = 0.0
        for (t in 1 until run.size) acc += abs(run[t][idx] - run[t - 1][idx]).toDouble()
        return acc / (run.size - 1)
    }

    // ------------------------------------------------------------------ cases

    private class Case(val label: String, val samples: FloatArray)

    private fun cases(sampleRate: Int, seconds: Double): List<Case> {
        val n = (seconds * sampleRate).toInt()
        val (speech, _) = AuroraDynamicsHarness.speechClip(
            sampleRate = sampleRate, totalMs = (seconds * 1000).toInt(), gain = 1f,
        )
        val (speechNormal, _) = AuroraDynamicsHarness.speechClip(
            sampleRate = sampleRate, totalMs = (seconds * 1000).toInt(), gain = 0.33f,
        )
        return listOf(
            Case("digital silence", silence(n)),
            Case("white -60dBFS", white(n, -60.0)),
            Case("white -50dBFS", white(n, -50.0)),
            Case("white -40dBFS", white(n, -40.0)),
            Case("pink  -50dBFS", pink(n, -50.0)),
            Case("pink  -40dBFS", pink(n, -40.0)),
            Case("speech gain1.0", speech),
            Case("speech gain0.33", speechNormal),
        )
    }

    @Test
    fun `noise floor end to end`() {
        val seconds = 20.0
        val scoreFrom = 14.0 // last 6 s: every reference has had 2+ taus to settle

        for ((label, rate, chunkMs, calib) in listOf(
            Quad("COMPOSER  (PcmAudioRecorder)", 16000, 20f, AuroraCalibration.UNPROCESSED),
            Quad("DICTATION (SttStreamClient)", 24000, 40f, AuroraCalibration.UNPROCESSED),
            Quad("VOICE     (VoiceScreen mic)", 24000, 40f, AuroraCalibration.DEFAULT),
        )) {
            println("")
            println("==================================================================")
            println("$label  rate=${rate}Hz read=${chunkMs}ms calibration=${calib.id}")
            val tap = AudioAnalysisTap(rate, calibration = calib)
            println("  hopSamples=${tap.hopSamples} (${"%.1f".format(tap.hopSamples * 1000f / rate)}ms)  fftSize=${tap.fftSize} (${"%.1f".format(tap.fftSize * 1000f / rate)}ms)")
            println("==================================================================")

            val perCaseBands = LinkedHashMap<String, List<FloatArray>>()
            val perCaseBins = LinkedHashMap<String, List<FloatArray>>()

            for (c in cases(rate, seconds)) {
                val run = feed(c.samples, rate, chunkMs, calib, scoreFrom)
                perCaseBands[c.label] = run.bands
                perCaseBins[c.label] = run.bins
                val raw = rawBands(c.samples, rate, calib, scoreFrom)

                println("")
                println("--- ${c.label}   (input RMS ${f3(rmsDbfs(c.samples))} dBFS, ${run.bands.size} frames scored)")
                print("    BAND out p50 : ")
                for (b in 0 until AuroraAnalyser.BANDS) print("${f4(pct(col(run.bands, b), 50.0))}  ")
                println()
                print("    BAND out p90 : ")
                for (b in 0 until AuroraAnalyser.BANDS) print("${f4(pct(col(run.bands, b), 90.0))}  ")
                println()
                print("    BAND drawn   : ")
                for (b in 0 until AuroraAnalyser.BANDS) {
                    print("${f4(Aurora.displayCurve(pct(col(run.bands, b), 50.0).toFloat()).toDouble())}  ")
                }
                println("   <- sqrt() height of the p50")
                print("    BAND jitter  : ")
                for (b in 0 until AuroraAnalyser.BANDS) print("${f4(jitter(run.bands, b))}  ")
                println()
                if (raw.isNotEmpty()) {
                    print("    BAND raw p50 : ")
                    for (b in 0 until AuroraAnalyser.BANDS) print("${f4(pct(col(raw, b), 50.0))}  ")
                    print("   NOISE_LIVE=")
                    for (b in 0 until AuroraAnalyser.BANDS) print("${calib.noiseLive[b]} ")
                    println()
                }

                val levels = run.bands.map { arr ->
                    var m = 0f
                    for (j in 0 until AuroraAnalyser.BANDS) {
                        val w = WaveformFeed.LEVEL_WEIGHTS[j] * arr[j]
                        if (w > m) m = w
                    }
                    m
                }.sorted()
                println("    LEVEL p50=${f4(pct(levels, 50.0))} p90=${f4(pct(levels, 90.0))}")

                print("    BIN  p50     : ")
                for (k in 0 until AuroraAnalyser.BINS) print("${f3(pct(col(run.bins, k), 50.0))} ")
                println()
                print("    BIN  p90     : ")
                for (k in 0 until AuroraAnalyser.BINS) print("${f3(pct(col(run.bins, k), 90.0))} ")
                println()
                val over = (0 until AuroraAnalyser.BINS).count { pct(col(run.bins, it), 50.0) >= 1.0 }
                val lit = (0 until AuroraAnalyser.BINS).count { pct(col(run.bins, it), 50.0) >= 0.02 }
                println("    BIN  bins at/over the 1.0 clamp: $over/24;  bins p50>=0.02: $lit/24")
            }

            // ---------------- contrast tables
            println("")
            println("### CONTRAST (speech gain1.0 p50 / noise p50), $label")
            val speechBands = perCaseBands["speech gain1.0"]!!
            val speechBins = perCaseBins["speech gain1.0"]!!
            for (noise in listOf("digital silence", "white -50dBFS", "white -40dBFS", "pink  -50dBFS", "pink  -40dBFS")) {
                val nb = perCaseBands[noise]!!
                val nbin = perCaseBins[noise]!!
                print("    vs $noise  bands: ")
                for (b in 0 until AuroraAnalyser.BANDS) {
                    val s = pct(col(speechBands, b), 50.0)
                    val q = pct(col(nb, b), 50.0)
                    print(if (q > 0) "${f3(s / q)}x  " else "inf  ")
                }
                val sBin = (0 until AuroraAnalyser.BINS).sumOf { pct(col(speechBins, it), 50.0) }
                val qBin = (0 until AuroraAnalyser.BINS).sumOf { pct(col(nbin, it), 50.0) }
                print("| bins(sum): ")
                print(if (qBin > 0) "${f3(sBin / qBin)}x" else "inf")
                println()
            }
        }

        // ---------------- bin frequency map, once
        println("")
        println("### BIN MAP (log bins, geometric centres)")
        for (k in 0 until AuroraAnalyser.BINS) {
            print("${k}:${Math.round(AuroraSpectrum.centreHz(k))}Hz ")
        }
        println()
    }

    private data class Quad(
        val label: String, val rate: Int, val chunkMs: Float, val calib: AuroraCalibration,
    )

    /** Silence → speech → silence, so the "after speech" floor is separated from the cold one. */
    @Test
    fun `floor after speech`() {
        val rate = 16000
        val (speech, _) = AuroraDynamicsHarness.speechClip(sampleRate = rate, totalMs = 6000, gain = 1f)
        for (dbfs in listOf(-60.0, -50.0, -40.0)) {
            val tail = pink((10.0 * rate).toInt(), dbfs, seed = 0x777L)
            val clip = speech + tail
            val run = feed(clip, rate, 20f, AuroraCalibration.UNPROCESSED, (speech.size / rate).toDouble() + 6.0)
            println("")
            println("--- after 6s speech, then pink ${dbfs}dBFS, scoring the last 4 s (${run.bands.size} frames)")
            print("    BAND p50: ")
            for (b in 0 until AuroraAnalyser.BANDS) print("${f4(pct(col(run.bands, b), 50.0))}  ")
            print(" p90: ")
            for (b in 0 until AuroraAnalyser.BANDS) print("${f4(pct(col(run.bands, b), 90.0))}  ")
            println()
            print("    BIN  p50: ")
            for (k in 0 until AuroraAnalyser.BINS) print("${f3(pct(col(run.bins, k), 50.0))} ")
            println()
        }
    }

    /**
     * THE MAIN TABLE: what a steady room tone at a given acoustic level renders as, end to end.
     * Composer config (16 kHz, UNPROCESSED profile), scored after every reference has settled.
     */
    @Test
    fun `room tone level sweep`() {
        val rate = 16000
        val secs = 20.0
        val n = (secs * rate).toInt()
        println("")
        println("### ROOM-TONE SWEEP — composer path (16 kHz, calibration=unprocessed), last 6 s of 20 s")
        println("dBFS  colour | RAW band energies              | BAND out (linear)              | DRAWN height sqrt()            | max bin | LEVEL")
        for (dbfs in listOf(-70.0, -65.0, -60.0, -55.0, -50.0, -45.0, -40.0, -35.0, -30.0)) {
            for (colour in listOf("pink", "white")) {
                val s = if (colour == "pink") pink(n, dbfs) else white(n, dbfs)
                val run = feed(s, rate, 20f, AuroraCalibration.UNPROCESSED, 14.0)
                val raw = rawBands(s, rate, AuroraCalibration.UNPROCESSED, 14.0)
                val sb = StringBuilder()
                sb.append(String.format("%-5.0f %-6s| ", dbfs, colour))
                for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(col(raw, b), 50.0))} ")
                sb.append("| ")
                for (b in 0 until AuroraAnalyser.BANDS) sb.append("${f4(pct(col(run.bands, b), 50.0))} ")
                sb.append("| ")
                for (b in 0 until AuroraAnalyser.BANDS) {
                    sb.append("${f4(Aurora.displayCurve(pct(col(run.bands, b), 50.0).toFloat()).toDouble())} ")
                }
                sb.append("|  ${f3((0 until AuroraAnalyser.BINS).maxOf { pct(col(run.bins, it), 50.0) })}  | ")
                val lv = run.bands.map { arr ->
                    var m = 0f
                    for (j in 0 until AuroraAnalyser.BANDS) {
                        val w = WaveformFeed.LEVEL_WEIGHTS[j] * arr[j]; if (w > m) m = w
                    }
                    m
                }.sorted()
                sb.append(f3(pct(lv, 50.0)))
                println(sb)
            }
        }
        println("")
        println("### ANALYTIC CEILING — output when raw band energy == NOISE_LIVE (denominator pinned)")
        for (b in 0 until AuroraAnalyser.BANDS) {
            val lin = AuroraAnalyser.softKnee(1f / AuroraAnalyser.HEADROOM, AuroraAnalyser.KNEE)
            println("    band $b: NOISE_LIVE=${AuroraAnalyser.NOISE_LIVE[b]}  PEAK_FLOOR=${AuroraAnalyser.PEAK_FLOOR[b]}  out=${f4(lin.toDouble())}  drawn=${f4(Aurora.displayCurve(lin).toDouble())}")
        }
        println("    (PEAK_FLOOR < NOISE_LIVE in every band, so in a silence the denominator IS NOISE_LIVE*HEADROOM)")
    }

    /** Speech scored on VOICED frames only — the honest speech-vs-noise comparison. */
    @Test
    fun `voiced speech vs room tone`() {
        val rate = 16000
        val secs = 20.0
        for (gain in listOf(1.0f, 0.33f)) {
            val (clip, voiced) = AuroraDynamicsHarness.speechClip(
                sampleRate = rate, totalMs = (secs * 1000).toInt(), gain = gain,
            )
            val hop = Math.round(rate * AudioAnalysisTap.HOP_MS / 1000f)
            var clock = 0L
            val tap = AudioAnalysisTap(rate, calibration = AuroraCalibration.UNPROCESSED, nowNs = { clock })
            val bands = ArrayList<FloatArray>()
            val bins = ArrayList<FloatArray>()
            val onFrame: (AnalysisFrame) -> Unit = { f ->
                val centre = (f.sampleOffset - hop / 2).toInt()
                if (f.sampleOffset > rate && voiced.any { centre in it }) {
                    bands.add(f.bands.copyOf()); bins.add(f.bins.copyOf())
                }
            }
            var i = 0
            val chunk = Math.round(rate * 20f / 1000f)
            while (i < clip.size) {
                val len = minOf(chunk, clip.size - i)
                tap.offer(toPcm16(clip, i, len), len * 2, onFrame)
                clock += 20_000_000L
                i += len
            }
            println("")
            println("### VOICED-ONLY speech, gain=$gain, composer path (${bands.size} voiced frames)")
            print("    BAND p50: ")
            for (b in 0 until AuroraAnalyser.BANDS) print("${f4(pct(col(bands, b), 50.0))} ")
            print(" p90: ")
            for (b in 0 until AuroraAnalyser.BANDS) print("${f4(pct(col(bands, b), 90.0))} ")
            print(" swing: ")
            for (b in 0 until AuroraAnalyser.BANDS) {
                val p50 = pct(col(bands, b), 50.0)
                print(if (p50 > 0) "${f3(pct(col(bands, b), 90.0) / p50)}x " else "inf ")
            }
            println()
            print("    DRAWN p50: ")
            for (b in 0 until AuroraAnalyser.BANDS) print("${f4(Aurora.displayCurve(pct(col(bands, b), 50.0).toFloat()).toDouble())} ")
            print(" p90: ")
            for (b in 0 until AuroraAnalyser.BANDS) print("${f4(Aurora.displayCurve(pct(col(bands, b), 90.0).toFloat()).toDouble())} ")
            println()
            print("    BIN p50: ")
            for (k in 0 until AuroraAnalyser.BINS) print("${f3(pct(col(bins, k), 50.0))} ")
            println()
            print("    BIN p90: ")
            for (k in 0 until AuroraAnalyser.BINS) print("${f3(pct(col(bins, k), 90.0))} ")
            println()
            println("    BIN max over all bins: p50=${f3((0 until AuroraAnalyser.BINS).maxOf { pct(col(bins, it), 50.0) })} p90=${f3((0 until AuroraAnalyser.BINS).maxOf { pct(col(bins, it), 90.0) })}  <- SegmentedEq/CircularOrb bar height fraction, NO curve applied")
        }
    }

    /** What a full-scale sine reads as on the bin channel — the calibration reference point. */
    @Test
    fun `bin channel reference points`() {
        val rate = 16000
        val n = rate * 4
        for (amp in listOf(1.0, 0.1, 0.01, 0.00316)) {
            val s = FloatArray(n) { (kotlin.math.sin(2.0 * Math.PI * 1000.0 * it / rate) * amp).toFloat() }
            val run = feed(s, rate, 20f, AuroraCalibration.UNPROCESSED, 2.0)
            val peak = (0 until AuroraAnalyser.BINS).maxOf { pct(col(run.bins, it), 50.0) }
            val which = (0 until AuroraAnalyser.BINS).maxByOrNull { pct(col(run.bins, it), 50.0) }
            println("    1 kHz sine amplitude $amp (${f3(20.0 * kotlin.math.log10(amp))} dBFS peak) -> peak bin $which reads ${f4(peak)}")
        }
    }
}
