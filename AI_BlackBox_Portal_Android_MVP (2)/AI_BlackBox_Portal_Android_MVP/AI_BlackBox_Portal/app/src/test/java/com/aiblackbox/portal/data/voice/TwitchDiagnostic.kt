package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AnalysisFrame
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.wave.styles.Cinematic
import com.aiblackbox.portal.ui.components.wave.styles.ClassicMath
import com.aiblackbox.portal.ui.components.wave.styles.MirroredPulse
import com.aiblackbox.portal.ui.components.wave.styles.OrbMath
import com.aiblackbox.portal.ui.components.wave.styles.ParticleWave
import com.aiblackbox.portal.ui.components.wave.styles.RibbonMath
import com.aiblackbox.portal.ui.components.wave.styles.SegmentedEqMath
import com.aiblackbox.portal.ui.components.wave.styles.SharpLineMath
import com.aiblackbox.portal.ui.components.wave.styles.SoftBlob
import org.junit.Test
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * MEASUREMENT ONLY — a printing diagnostic, no assertions on production behaviour.
 *
 * Answers: how much of what the geometry sees, per style, is COHERENT MOTION and how much is
 * frame-to-frame JITTER, on silence / room noise / speech.
 */
class TwitchDiagnostic {

    // ------------------------------------------------------------------ statistics

    data class Twitch(
        val label: String,
        val p50: Double,
        val p90: Double,
        val mean: Double,
        val max: Double,
        /** mean |x[t] - x[t-1]| — how far the value moves between analysis frames. */
        val meanStep: Double,
        /** fraction of consecutive steps that REVERSE direction. 0.67 = white noise. */
        val reversalFraction: Double,
        /** meanStep / p50 — dimensionless twitchiness of the typical value. */
        val jitter: Double,
        val n: Int,
    ) {
        /** Peak-to-typical swing — the operator's "dynamic range" complaint, as a number. */
        val swing: Double get() = if (p50 > 1e-9) p90 / p50 else Double.NaN

        override fun toString(): String = String.format(
            "%-26s p50=%.4f p90=%.4f max=%.4f swing=%5.2fx | step=%.4f rev=%.2f (%4.1f/s) jitter=%.2f n=%d",
            label, p50, p90, max, swing, meanStep, reversalFraction, reversalFraction * 50.0, jitter, n,
        )
    }

    private fun twitch(label: String, xs: List<Float>): Twitch {
        if (xs.size < 3) return Twitch(label, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, xs.size)
        val s = xs.sorted()
        fun pct(p: Double): Double = s[(Math.round(p / 100.0 * (s.size - 1))).toInt()].toDouble()
        var stepSum = 0.0
        var reversals = 0
        var comparable = 0
        var prevD = 0.0
        for (i in 1 until xs.size) {
            val d = (xs[i] - xs[i - 1]).toDouble()
            stepSum += abs(d)
            if (i > 1 && d != 0.0 && prevD != 0.0) {
                comparable++
                if (d * prevD < 0) reversals++
            }
            prevD = d
        }
        val p50 = pct(50.0)
        val step = stepSum / (xs.size - 1)
        return Twitch(
            label = label,
            p50 = p50,
            p90 = pct(90.0),
            mean = xs.sumOf { it.toDouble() } / xs.size,
            max = s.last().toDouble(),
            meanStep = step,
            reversalFraction = if (comparable > 0) reversals.toDouble() / comparable else 0.0,
            jitter = if (p50 > 1e-9) step / p50 else Double.NaN,
            n = xs.size,
        )
    }

    // ------------------------------------------------------------------ signals

    private fun silence(sampleRate: Int, ms: Int) = FloatArray(sampleRate * ms / 1000)

    /** Flat-ish room noise at [dbfs] full-scale RMS, deterministic LCG. */
    private fun roomNoise(sampleRate: Int, ms: Int, dbfs: Double): FloatArray {
        val amp = Math.pow(10.0, dbfs / 20.0).toFloat()
        var rng = 0xB16B00B5L
        return FloatArray(sampleRate * ms / 1000) {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            (((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f) * amp
        }
    }

    private fun toPcm(samples: FloatArray, from: Int, len: Int): ByteArray {
        val b = ByteArray(len * 2)
        for (k in 0 until len) {
            val x = (samples[from + k] * 32767f).toInt().coerceIn(-32768, 32767)
            b[2 * k] = (x and 0xFF).toByte()
            b[2 * k + 1] = ((x shr 8) and 0xFF).toByte()
        }
        return b
    }

    /** The PRODUCTION path: PCM chunks -> AudioAnalysisTap -> AnalysisFrames (bands + bins). */
    private fun tapFrames(
        samples: FloatArray,
        sampleRate: Int,
        readMs: Int = 20,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
    ): List<AnalysisFrame> {
        var nowNs = 0L
        val tap = AudioAnalysisTap(sampleRate, calibration = calibration, nowNs = { nowNs })
        val out = ArrayList<AnalysisFrame>()
        val chunk = sampleRate * readMs / 1000
        var i = 0
        while (i < samples.size) {
            val len = minOf(chunk, samples.size - i)
            tap.offer(toPcm(samples, i, len), len * 2) { f ->
                out.add(AnalysisFrame(f.sampleOffset, f.atNs, f.bands.copyOf(), f.bins.copyOf()))
            }
            nowNs += len.toLong() * 1_000_000_000L / sampleRate
            i += len
        }
        return out
    }

    /** Frames past the analyser warm-up, so nothing below is measuring the ramp. */
    private fun settled(frames: List<AnalysisFrame>, sampleRate: Int): List<AnalysisFrame> {
        val warm = (AuroraAnalyser.WARMUP_MS / 1000f * sampleRate).toLong()
        return frames.filter { it.sampleOffset > warm }
    }

    private val RATE = 24000
    private val HOP_HZ = 50.0

    // ================================================================= 1. CHANNELS

    @Test
    fun `channel level and jitter on silence, room noise and speech`() {
        println("\n================ RAW CHANNELS (tap @ ${HOP_HZ}Hz, ${RATE}Hz) ================")
        val cases = listOf(
            "digital-silence" to silence(RATE, 6000),
            "room-noise -60dBFS" to roomNoise(RATE, 6000, -60.0),
            "room-noise -50dBFS" to roomNoise(RATE, 6000, -50.0),
            "room-noise -40dBFS" to roomNoise(RATE, 6000, -40.0),
            "speech" to AuroraDynamicsHarness.speechClip(RATE, totalMs = 8000).first,
        )
        for ((name, sig) in cases) {
            val f = settled(tapFrames(sig, RATE), RATE)
            println("\n--- $name  (${f.size} frames) ---")
            for (b in 0 until AuroraAnalyser.BANDS) {
                println("  BAND " + twitch("band$b", f.map { it.bands[b] }))
            }
            println("  BAND " + twitch("pooled", f.flatMap { fr -> fr.bands.toList() }))
            // Renderer curve: what the eye gets after Aurora.displayCurve (sqrt).
            println("  CURVED " + twitch("sqrt(pooled)", f.flatMap { fr -> fr.bands.map { Aurora.displayCurve(it) } }))
            val binSel = listOf(0, 5, 11, 17, 23)
            for (k in binSel) {
                println("  BIN  " + twitch("bin$k@${AuroraSpectrumCentre(k)}Hz", f.map { it.bins[k] }))
            }
            println("  BIN  " + twitch("pooled", f.flatMap { fr -> fr.bins.toList() }))
        }
    }

    private fun AuroraSpectrumCentre(k: Int): Int {
        // 80 Hz .. 7200 Hz, 24 geometric bins.
        val ratio = Math.pow(7200.0 / 80.0, (k + 0.5) / 24.0)
        return Math.round(80.0 * ratio).toInt()
    }

    // ================================================================= 2. STYLE DRIVES

    /**
     * The DRIVE each style hands its geometry, frame by frame, computed from the real tap output.
     * `bins = null` is TODAY (no call site publishes spectra); `bins = frame.bins` is what M7's
     * spectrum path would produce once a producer is wired.
     */
    @Test
    fun `per-style drive jitter`() {
        val cases = listOf(
            "room-noise -50dBFS" to roomNoise(RATE, 6000, -50.0),
            "room-noise -40dBFS" to roomNoise(RATE, 6000, -40.0),
            "room-noise -33dBFS" to roomNoise(RATE, 6000, -33.0),
            "speech" to AuroraDynamicsHarness.speechClip(RATE, totalMs = 8000).first,
        )
        for (withBins in listOf(false, true)) {
            for ((name, sig) in cases) {
                val frames = settled(tapFrames(sig, RATE), RATE)
                println(
                    "\n======== STYLE DRIVES — $name — spectrum=${if (withBins) "WIRED (M7 path)" else "ABSENT (ships today)"} ========",
                )
                for (row in styleDrives(frames, withBins)) println("  " + row)
            }
        }
    }

    private fun styleDrives(frames: List<AnalysisFrame>, withBins: Boolean): List<Twitch> {
        val out = ArrayList<Twitch>()
        fun binsOf(f: AnalysisFrame): FloatArray? = if (withBins) f.bins else null
        fun levelOf(f: AnalysisFrame): Float {
            // WaveformFeed.levelOf — weighted max.
            val w = floatArrayOf(1.0f, 1.0f, 0.85f, 0.70f)
            var m = 0f
            for (j in 0 until AuroraAnalyser.BANDS) {
                val v = w[j] * f.bands[j].coerceIn(0f, 1f)
                if (v > m) m = v
            }
            return m
        }

        // --- The SHARED scalar every level-driven style reads (WaveformFeed.levelOf).
        out.add(twitch("[feed] weighted-max level", frames.map { levelOf(it) }))
        out.add(twitch("[feed] band0 raw", frames.map { it.bands[0] }))

        // --- Aurora (default): displayCurve then a per-band attack/release envelope.
        run {
            val lv = FloatArray(AuroraAnalyser.BANDS) { Aurora.BASELINE }
            val series = ArrayList<Float>()
            val dt = 1f / HOP_HZ.toFloat()
            for (f in frames) {
                for (j in 0 until AuroraAnalyser.BANDS) {
                    val target = maxOf(Aurora.BASELINE, Aurora.displayCurve(f.bands[j]))
                    val tau = if (target > lv[j]) Aurora.ATTACK_TAU_SEC else Aurora.BAND_RELEASE_TAUS_SEC[j]
                    lv[j] += (target - lv[j]) * (1f - Math.exp((-dt / tau).toDouble()).toFloat())
                }
                series.add(lv[0])
            }
            out.add(twitch("aurora  band0 sheet", series))
        }

        // --- Classic: level -> layer amplitude, share from bands (or bin groups).
        run {
            val series = frames.map { f ->
                val bins = if (withBins) f.bins else null
                ClassicMath.layerAmplitude(
                    maxOf(Aurora.BASELINE, levelOf(f)),
                    ClassicMath.layerShare(bins, f.bands[0], f.bands[1], f.bands[2], f.bands[3], 0),
                    0,
                )
            }
            out.add(twitch("classic layer0 amp", series))
        }

        // --- Glowing Ribbon: strand 0 of 12, sqrt-shaped; plus its shape descriptors.
        run {
            val levels = FloatArray(AuroraAnalyser.BINS)
            val drive = ArrayList<Float>()
            val centroid = ArrayList<Float>()
            val spread = ArrayList<Float>()
            for (f in frames) {
                val count: Int
                if (withBins) {
                    for (k in 0 until AuroraAnalyser.BINS) levels[k] = f.bins[k].coerceIn(0f, 1f)
                    count = AuroraAnalyser.BINS
                } else {
                    for (b in 0 until AuroraAnalyser.BANDS) levels[b] = f.bands[b]
                    count = AuroraAnalyser.BANDS
                }
                drive.add(RibbonMath.strandDrive(RibbonMath.strandLevel(levels, count, 0, 12), Aurora.BASELINE, 0))
                centroid.add(RibbonMath.centroid(levels, count))
                spread.add(RibbonMath.spread(levels, count))
            }
            out.add(twitch("ribbon strand0 drive", drive))
            out.add(twitch("ribbon centroid", centroid))
            out.add(twitch("ribbon spread", spread))
        }

        // --- Mirrored Pulse: newest column extent. The style's private sqrt is DELETED; the
        // column is the level through the app's one curve, blended onto the rest floor, and
        // the bar geometry is linear in that.
        run {
            val series = frames.map {
                MirroredPulse.drawn(Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(levelOf(it))))
            }
            out.add(twitch("mirrored newest col", series))
        }

        // --- Segmented EQ: column 0 of 24 (max over its bins/band), + lit LED count of 12 rows.
        run {
            val drive = ArrayList<Float>()
            val lit = ArrayList<Float>()
            for (f in frames) {
                val d = maxOf(
                    Aurora.BASELINE,
                    SegmentedEqMath.columnDrive(binsOf(f), f.bands, 0, 24),
                )
                drive.add(d)
                lit.add(SegmentedEqMath.litCount(d, 12, 1).toFloat())
            }
            out.add(twitch("segeq col0 drive", drive))
            out.add(twitch("segeq col0 LIT rows/12", lit))
        }

        // --- Soft Blob: slice 0 lobe height (sliceDrives -> displayCurve).
        run {
            val row = FloatArray(SoftBlob.SLICES.size)
            val series = ArrayList<Float>()
            for (f in frames) {
                SoftBlob.sliceDrives(f.bands, binsOf(f), levelOf(f), row)
                series.add(SoftBlob.lobeHeight(row[0], Aurora.BASELINE))
            }
            out.add(twitch("softblob slice0 h", series))
        }

        // --- Sharp Line: newest vertex level (raw, no smoothing) — same series as mirrored's raw.
        run {
            val series = frames.map { maxOf(Aurora.BASELINE, levelOf(it)) }
            out.add(twitch("sharpline newest y", series))
        }

        // --- Circular Orb: spoke 0 of 48.
        run {
            val series = frames.map { OrbMath.tickDrive(binsOf(it), it.bands, 0, 48) }
            out.add(twitch("orb tick0 drive", series))
        }

        // --- Particle Wave: slice 0. The style's private attack/release envelope is
        // DELETED, so this is the drive exactly as the renderer now reads it — this
        // frame's slice, curved, blended onto the rest floor, with no memory.
        run {
            val goal = FloatArray(ParticleWave.SLICES.size)
            val series = ArrayList<Float>()
            val floor = ParticleWave.restFloor(Aurora.BASELINE)
            for (f in frames) {
                ParticleWave.sliceDrives(f.bands, binsOf(f), levelOf(f), goal)
                series.add(Aurora.restBlend(floor, ParticleWave.shaped(goal[0])))
            }
            out.add(twitch("particle slice0 drive", series))
        }

        // --- Cinematic: newest bar extent, on a 140dp-tall surface at density 3.
        run {
            val h = 140f * 3f
            val series = frames.map { Cinematic.extentPx(h, 140f, maxOf(Aurora.BASELINE, levelOf(it))) / h }
            out.add(twitch("cinematic newest ext", series))
        }
        return out
    }

    // ================================================================= 3. RATE / STEPPING

    @Test
    fun `analysis rate vs frame rate`() {
        val tap = AudioAnalysisTap(RATE)
        println("\n================ RATES ================")
        println("  tap hopMs                = ${AudioAnalysisTap.HOP_MS} ms")
        println("  tap hopSamples @${RATE}   = ${tap.hopSamples}  -> ${RATE.toFloat() / tap.hopSamples} Hz")
        val tap16 = AudioAnalysisTap(16000)
        println("  tap hopSamples @16000    = ${tap16.hopSamples} -> ${16000f / tap16.hopSamples} Hz")
        println("  tap fftSize @${RATE}      = ${tap.fftSize} (${tap.fftSize * 1000f / RATE} ms bin window)")
        println("  feed ring cadence        = 50 Hz, capacity ${(3.0f * 50f).toInt()} samples (3.0 s)")
        for (fps in listOf(60, 120)) {
            val hold = fps / 50.0
            println("  @${fps}Hz display: each analysis value is HELD for $hold frames, then JUMPS")
        }
    }

    // ================================================================= 4. NOISE FLOOR

    @Test
    fun `what the noise floor renders as`() {
        println("\n================ NOISE FLOOR -> DRAWN HEIGHT ================")
        println("  NOISE_LIVE      = ${AuroraAnalyser.NOISE_LIVE.toList()}")
        println("  PEAK_FLOOR      = ${AuroraAnalyser.PEAK_FLOOR.toList()}")
        println("  HEADROOM        = ${AuroraAnalyser.HEADROOM}")
        println("  denom at rest   = ${AuroraAnalyser.NOISE_LIVE.map { it * AuroraAnalyser.HEADROOM }}")
        for (db in listOf(-70.0, -60.0, -55.0, -50.0, -45.0, -40.0)) {
            val f = settled(tapFrames(roomNoise(RATE, 5000, db), RATE), RATE)
            val bands = (0 until AuroraAnalyser.BANDS).map { b -> f.map { it.bands[b] } }
            val drawn = bands.map { series -> series.map { Aurora.displayCurve(it) } }
            println(
                String.format(
                    "  %6.1f dBFS: band p50 = [%s]   DRAWN (sqrt) p50 = [%s]",
                    db,
                    bands.joinToString(", ") { String.format("%.3f", median(it)) },
                    drawn.joinToString(", ") { String.format("%.3f", median(it)) },
                ),
            )
        }
        // And what the BIN channel would show for the same input.
        for (db in listOf(-60.0, -50.0, -40.0)) {
            val f = settled(tapFrames(roomNoise(RATE, 5000, db), RATE), RATE)
            val pooled = f.flatMap { fr -> fr.bins.toList() }
            println(
                String.format(
                    "  %6.1f dBFS: BIN pooled p50=%.5f p90=%.5f max=%.5f (raw, ungated)",
                    db, median(pooled), pct(pooled, 90.0), pooled.max(),
                ),
            )
        }
    }

    /** Where the shared level scalar sits against the rest floor, per room level. */
    @Test
    fun `shared level vs rest floor across room levels`() {
        println("\n================ SHARED LEVEL vs REST FLOOR (BASELINE=${Aurora.BASELINE}) ================")
        val w = floatArrayOf(1.0f, 1.0f, 0.85f, 0.70f)
        for (db in listOf(-60.0, -50.0, -45.0, -40.0, -35.0, -30.0, -25.0)) {
            val f = settled(tapFrames(roomNoise(RATE, 5000, db), RATE), RATE)
            val lv = f.map { fr ->
                var m = 0f
                for (j in 0 until AuroraAnalyser.BANDS) {
                    val v = w[j] * fr.bands[j].coerceIn(0f, 1f)
                    if (v > m) m = v
                }
                m
            }
            val t = twitch(String.format("%6.1f dBFS", db), lv)
            val above = lv.count { it > Aurora.BASELINE }.toDouble() / lv.size
            println("  $t  aboveFloorFraction=${String.format("%.2f", above)}  drawnSqrt(p50)=${String.format("%.3f", sqrt(t.p50))}")
        }
    }

    /** Which styles have a MOTION driver that is not amplitude (the ribbon's crest). */
    @Test
    fun `motion drivers`() {
        println("\n================ MOTION DRIVERS ================")
        println("  ribbon crestSpeed j=0  = ${RibbonMath.crestSpeed(0)} widths/s -> ${RibbonMath.crossSeconds(0)} s to cross")
        println("  ribbon crestSpeed j=11 = ${RibbonMath.crestSpeed(11)} widths/s -> ${RibbonMath.crossSeconds(11)} s to cross")
        println("  ribbon SWIRL_SPEED     = ${RibbonMath.SWIRL_SPEED} widths/s")
        println("  ribbon spec band       = ${RibbonMath.CROSS_SEC_MIN}..${RibbonMath.CROSS_SEC_MAX} s")
    }

    /**
     * The SECOND channel every style carries: volume-blind RATIOS of the four bands (stripe
     * shares, spine brightness, spectral heat, centroid/spread). These are NOT floored by
     * restLevel, so they keep moving even when the amplitude is pinned flat at the rest floor.
     */
    @Test
    fun `ratio channels at rest and on speech`() {
        println("\n================ RATIO / SHAPE CHANNELS ================")
        for ((name, sig) in listOf(
            "room-noise -50dBFS" to roomNoise(RATE, 6000, -50.0),
            "speech" to AuroraDynamicsHarness.speechClip(RATE, totalMs = 8000).first,
        )) {
            val f = settled(tapFrames(sig, RATE), RATE)
            println("\n--- $name ---")
            println("  " + twitch("mirrored bandShare[0]", f.map { MirroredPulse.bandShare(it.bands[0], it.bands[1], it.bands[2], it.bands[3], 0) }))
            println("  " + twitch("mirrored bandShare[3]", f.map { MirroredPulse.bandShare(it.bands[0], it.bands[1], it.bands[2], it.bands[3], 3) }))
            println("  " + twitch("cinematic brightness", f.map { Cinematic.brightness(it.bands[0], it.bands[1], it.bands[2], it.bands[3]) }))
            println("  " + twitch("sharpline brightness", f.map { SharpLineMath.brightness(it.bands[0], it.bands[1], it.bands[2], it.bands[3]) }))
            println("  " + twitch("classic layerShare[0]", f.map { ClassicMath.bandLayerShare(it.bands[0], it.bands[1], it.bands[2], it.bands[3], 0) }))
        }
    }

    /**
     * TWO INDEPENDENT 50 Hz CLOCKS. The tap publishes an analysis frame every 20 ms of AUDIO;
     * WaveformFeed pushes a ring sample every 20 ms of FRAME-LOOP wall clock. Nothing couples
     * them, so some analysis values land in two ring slots and some never land at all.
     */
    @Test
    fun `feed ring vs audio publication aliasing`() {
        println("\n================ RING CADENCE vs AUDIO CADENCE ================")
        for (fps in listOf(60, 120)) {
            for (offsetMs in listOf(0.0, 7.0, 13.0)) {
                val feed = com.aiblackbox.portal.ui.components.wave.WaveformFeed()
                val frameDt = 1f / fps
                var audioT = offsetMs / 1000.0 // next audio publication instant
                var seq = 0 // monotonically increasing "which analysis frame is live"
                val pushed = ArrayList<Int>()
                // Re-implement the host loop's push schedule exactly (WaveformFeed.step).
                var accum = 0f
                var t = 0.0
                repeat(fps * 4) {
                    t += frameDt
                    while (t >= audioT) { seq++; audioT += 0.020 }
                    accum += frameDt
                    while (accum >= 0.020f) { pushed.add(seq); accum -= 0.020f }
                }
                val distinct = pushed.toSet().size
                val dup = pushed.size - distinct
                val dropped = (seq - distinct).coerceAtLeast(0)
                println(
                    String.format(
                        "  %3dHz offset%5.1fms: audio frames=%d  ring pushes=%d  distinct=%d  DUPLICATED=%d (%.0f%%)  DROPPED=%d (%.0f%%)",
                        fps, offsetMs, seq, pushed.size, distinct,
                        dup, 100.0 * dup / pushed.size, dropped, 100.0 * dropped / maxOf(seq, 1),
                    ),
                )
            }
        }
        println("  (feed.capacity=${com.aiblackbox.portal.ui.components.wave.WaveformFeed().capacity} columns over ${com.aiblackbox.portal.ui.components.wave.WaveformFeed.HISTORY_SEC}s)")
    }

    /**
     * SharpLine's "quiet line texture" — is it a breathing shimmer or per-frame hash noise?
     * Sampled at the DISPLAY rate, because that is what feeds it (frame.phaseSec).
     */
    @Test
    fun `sharpline noise texture at display rate`() {
        println("\n================ SHARPLINE noiseOffset(k, phaseSec) ================")
        println("  NOISE_RATE=${SharpLineMath.NOISE_RATE}  NOISE_FRACTION=${SharpLineMath.NOISE_FRACTION}")
        for (fps in listOf(60, 120)) {
            val series = ArrayList<Float>()
            var t = 0f
            repeat(fps * 3) {
                series.add(SharpLineMath.noiseOffset(7, t))
                t += 1f / fps
            }
            // lag-1 autocorrelation: ~1 = smooth, ~0 = independent each frame.
            val mean = series.average().toDouble()
            var num = 0.0
            var den = 0.0
            for (i in series.indices) {
                den += (series[i] - mean) * (series[i] - mean)
                if (i > 0) num += (series[i] - mean) * (series[i - 1] - mean)
            }
            val t1 = twitch("column7 @${fps}Hz", series)
            println(
                "  $t1  lag1-autocorr=${String.format("%.3f", num / den)}  " +
                    "drawn swing at 140dp = ${String.format("%.1f", t1.meanStep * SharpLineMath.NOISE_FRACTION * 140.0)} dp/frame",
            )
        }
    }

    private fun median(xs: List<Float>): Double = pct(xs, 50.0)

    private fun pct(xs: List<Float>, p: Double): Double {
        if (xs.isEmpty()) return Double.NaN
        val s = xs.sorted()
        return s[(Math.round(p / 100.0 * (s.size - 1))).toInt()].toDouble()
    }

    // ================================================================= 5. SPEECH DYNAMIC RANGE

    @Test
    fun `speech vs silence separation per channel`() {
        println("\n================ SPEECH vs NOISE SEPARATION ================")
        val (clip, voiced) = AuroraDynamicsHarness.speechClip(RATE, totalMs = 8000)
        val frames = settled(tapFrames(clip, RATE), RATE)
        val voicedF = frames.filter { fr -> voiced.any { fr.sampleOffset.toInt() in it } }
        val gapF = frames.filter { fr -> voiced.none { fr.sampleOffset.toInt() in it } }
        println("  voiced frames=${voicedF.size}  gap frames=${gapF.size}")
        for (b in 0 until AuroraAnalyser.BANDS) {
            val v = median(voicedF.map { it.bands[b] })
            val g = median(gapF.map { it.bands[b] })
            println(
                String.format(
                    "  band%d  voiced p50=%.4f  gap p50=%.4f  ratio=%.2fx   DRAWN voiced=%.3f gap=%.3f  drawn-ratio=%.2fx",
                    b, v, g, if (g > 0) v / g else Double.NaN,
                    sqrt(v), sqrt(g), if (g > 0) sqrt(v) / sqrt(g) else Double.NaN,
                ),
            )
        }
        val vb = voicedF.flatMap { it.bins.toList() }
        val gb = gapF.flatMap { it.bins.toList() }
        println(
            String.format(
                "  BINS   voiced p50=%.5f  gap p50=%.5f  ratio=%.2fx",
                median(vb), median(gb), if (median(gb) > 0) median(vb) / median(gb) else Double.NaN,
            ),
        )
    }
}
