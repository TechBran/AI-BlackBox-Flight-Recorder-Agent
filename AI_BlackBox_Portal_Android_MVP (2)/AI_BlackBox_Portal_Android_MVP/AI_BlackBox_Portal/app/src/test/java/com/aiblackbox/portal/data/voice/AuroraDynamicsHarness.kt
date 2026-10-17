package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.voice.AudioEnvelope
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import com.aiblackbox.portal.ui.components.aurora.AuroraEnvelope
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.WaveformDemoDriver
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.sin

/**
 * The waveform MEASUREMENT FLOOR: the synthetic signals, the feeding, and the summary statistics
 * that the waveform characterisation tests assert against.
 *
 * This is the promoted form of the throwaway `AuroraDynamicsDiagnostic` printer that preceded it
 * (since deleted).
 * The difference is the whole point: that one PRINTED numbers a human had to read and believe,
 * this one RETURNS them, so the numbers become assertions and a change in what the ribbon draws
 * becomes a red build instead of a paragraph of console output nobody re-reads. Everything here is
 * a pure function of its arguments — no clock, no RNG state, no android.* — so a given input
 * always yields byte-identical statistics.
 *
 * It measures SIX things, because those are the places the analyser work moves numbers:
 *
 *  1. [liveDistribution]     what the mic path actually puts on screen (band levels, post warm-up).
 *  2. [previewDistribution]  what the SETTINGS preview puts on screen — which never touches the
 *                            analyser at all (see [WaveformDemoDriver]), so the two can and do
 *                            disagree wildly. Quantifying that gap is the point.
 *  3. [dynamicRange]         how sustained loud, ordinary and whispered passages separate from each
 *                            other WITHIN ONE session — the pair of properties a single auto-gain
 *                            reference cannot deliver together, so they are scored together.
 *  4. [silenceAfterSpeech]   what a long silence draws once every reference has had time to decay.
 *  5. [analyserExponent] / [rendererExponent]  each stage's perceptual exponent, measured
 *                            END-TO-END through the real code so a duplicate curve cannot hide.
 *  6. [offlineShaping]       the offline gate/lift, recovered EMPIRICALLY (its constants are
 *                            private) so the player bar's composed exponent can be pinned too.
 *
 * The first, third and fourth also exist THROUGH THE REAL [AudioAnalysisTap] ([liveDistributionViaTap],
 * [dynamicRangeViaTap], [silenceAfterSpeechViaTap]), over every stream in [SHIPPED_STREAMS]. That is
 * the path production runs, and so the one the requirements in WaveformDynamicsGatesTest are scored
 * on: the direct-feed versions take a free chunk size, and a chunk the tap does not ship is a window
 * the operator never sees. TapEquivalenceTest proves the two agree to the bit at the tap's hop.
 *
 * Statistic definitions are fixed here, once, because a percentile is meaningless without them:
 * every distribution is taken over frames where a voice is PRESENT (silence between utterances is
 * excluded, gaps *within* an utterance are NOT — they are part of what the ribbon draws), and
 * percentiles are nearest-rank on the sorted sample.
 */
object AuroraDynamicsHarness {

    // ------------------------------------------------------------------ statistics

    /**
     * One band's (or one pooled sample's) shape.
     *
     * @property zeroFraction share of frames that are EXACTLY zero. Distinguishes "quiet" from
     *   "silent": the demo driver emits true zeros in every syllable gap, the analyser essentially
     *   never does, and that difference is visible on screen as motion that touches the floor vs
     *   motion that hovers above it.
     */
    data class BandStats(
        val p50: Double,
        val p90: Double,
        val mean: Double,
        val max: Double,
        val zeroFraction: Double,
        val n: Int,
    ) {
        /** Peak-to-typical swing: how far a loud moment rises above an ordinary one. */
        val swing: Double get() = if (p50 > 0.0) p90 / p50 else Double.POSITIVE_INFINITY
    }

    /**
     * A full four-band picture.
     *
     * @property bandSpread ratio of the loudest band's p50 to the quietest band's p50 — how
     *   unevenly the four sheets are driven. 1.0 would mean all four sheets sit at the same
     *   typical height (a flat, characterless ribbon); a large value means one sheet dominates
     *   and the other three are near the floor.
     */
    data class Distribution(
        val perBand: List<BandStats>,
        val pooled: BandStats,
    ) {
        val bandSpread: Double
            get() {
                val p50s = perBand.map { it.p50 }
                val lo = p50s.min()
                return if (lo > 0.0) p50s.max() / lo else Double.POSITIVE_INFINITY
            }

        /**
         * [bandSpread] computed on MEANS rather than medians. Both are reported because they
         * answer different questions: the median version says how uneven the four sheets look at
         * a typical instant, the mean version how uneven their total energy is over an utterance.
         */
        val bandSpreadOfMeans: Double
            get() {
                val means = perBand.map { it.mean }
                val lo = means.min()
                return if (lo > 0.0) means.max() / lo else Double.POSITIVE_INFINITY
            }

        /** Index of the band with the highest typical level. */
        val dominantBand: Int get() = perBand.indices.maxBy { perBand[it].p50 }
    }

    private fun statsOf(xs: List<Float>): BandStats {
        if (xs.isEmpty()) return BandStats(Double.NaN, Double.NaN, Double.NaN, Double.NaN, Double.NaN, 0)
        val s = xs.sorted()
        fun pct(p: Double): Double {
            val i = Math.round(p / 100.0 * (s.size - 1)).toInt().coerceIn(0, s.size - 1)
            return s[i].toDouble()
        }
        return BandStats(
            p50 = pct(50.0),
            p90 = pct(90.0),
            mean = xs.sumOf { it.toDouble() } / xs.size,
            max = s.last().toDouble(),
            zeroFraction = xs.count { it == 0f }.toDouble() / xs.size,
            n = xs.size,
        )
    }

    private fun distributionOf(frames: List<FloatArray>): Distribution = Distribution(
        perBand = (0 until AuroraAnalyser.BANDS).map { b -> statsOf(frames.map { it[b] }) },
        pooled = statsOf(frames.flatMap { row -> row.toList() }),
    )

    // ------------------------------------------------------- synthetic speech (live path)

    /** Probe-centred carrier: one sine per band, so all four bands carry real energy. */
    private val CARRIER_HZ = doubleArrayOf(150.0, 650.0, 2000.0, 5800.0)

    /** Noise floor filling the gaps between words, at -50 dBFS. */
    private const val NOISE_FULL_SCALE = 0.00316

    /**
     * A deterministic speech-like clip: [wordMs] words of 4 Hz syllable bursts at cycling peak
     * amplitudes, separated by [silMs] of -50 dBFS noise. Returns the samples paired with the
     * half-open voiced spans, so callers can score voiced frames only.
     *
     * @param gain scales the WORDS and not the noise floor between them, so a gain change is a
     *   talker getting louder or quieter in a room whose hiss stays put — which is the only way to
     *   ask whether the ribbon still separates the two.
     */
    fun speechClip(
        sampleRate: Int = 24000,
        wordMs: Int = 500,
        silMs: Int = 300,
        totalMs: Int = 8000,
        gain: Float = 1f,
    ): Pair<FloatArray, List<IntRange>> {
        val peaks = floatArrayOf(0.5f * gain, 1.0f * gain, 0.25f * gain, 0.8f * gain)
        val out = ArrayList<Float>(sampleRate * totalMs / 1000 + sampleRate)
        val voiced = ArrayList<IntRange>()
        val wordSamples = sampleRate * wordMs / 1000
        val silSamples = sampleRate * silMs / 1000
        var t = 0
        var wi = 0
        // A fixed LCG rather than kotlin.random so the clip is identical on every JVM forever.
        var rng = 0x5EED1234L
        fun noise(): Float {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            return (((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f) * NOISE_FULL_SCALE.toFloat()
        }
        while (t * 1000 / sampleRate < totalMs) {
            val peak = peaks[wi % peaks.size]
            val start = t
            for (k in 0 until wordSamples) {
                val tt = k.toDouble() / sampleRate
                val syllable = ((1.0 - cos(2.0 * PI * 4.0 * tt)) / 2.0).toFloat()
                out.add((carrier(t, sampleRate) * syllable * peak).coerceIn(-1f, 1f))
                t++
            }
            voiced.add(start until t)
            for (k in 0 until silSamples) { out.add(noise()); t++ }
            wi++
        }
        return out.toFloatArray() to voiced
    }

    private fun carrier(i: Int, sampleRate: Int): Float {
        var c = 0.0
        for (f in CARRIER_HZ) c += sin(2.0 * PI * f * i / sampleRate)
        return (c * 0.25).toFloat()
    }

    private fun toPcm16(samples: FloatArray, from: Int, len: Int): ByteArray {
        val b = ByteArray(len * 2)
        for (k in 0 until len) {
            val x = (samples[from + k] * 32767f).toInt().coerceIn(-32768, 32767)
            b[2 * k] = (x and 0xFF).toByte()
            b[2 * k + 1] = ((x shr 8) and 0xFF).toByte()
        }
        return b
    }

    /**
     * Feed [speechClip] through a real [AuroraAnalyser] in [chunkMs] chunks on an injected clock,
     * and summarise the VOICED, post-warm-up output — i.e. exactly the numbers the mic ribbon is
     * driven by while somebody is talking.
     *
     * [chunkMs] is a parameter because it is not a free choice in production: it is set by the mic
     * read length (see [MicBuffer]), so a change there lands here.
     */
    fun liveDistribution(
        chunkMs: Int = 80,
        sampleRate: Int = 24000,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
    ): Distribution {
        val (clip, voiced) = speechClip(sampleRate)
        val chunkSamples = sampleRate * chunkMs / 1000
        val chunkNs = chunkMs * 1_000_000L
        var nowNs = 0L
        val analyser = AuroraAnalyser(sampleRate, { nowNs }, calibration)
        val frames = ArrayList<FloatArray>()
        var idx = 0
        while (idx < clip.size) {
            val len = minOf(chunkSamples, clip.size - idx)
            val out = analyser.analyze(toPcm16(clip, idx, len), len * 2)
            val elapsedMs = nowNs / 1e6f
            val centre = idx + len / 2
            if (elapsedMs >= AuroraAnalyser.WARMUP_MS && voiced.any { centre in it }) {
                frames.add(out.copyOf())
            }
            nowNs += chunkNs
            idx += len
        }
        return distributionOf(frames)
    }

    // ------------------------------------------------- sustained level contrast (one run)

    /**
     * One sustained stretch of the speech clip at one loudness.
     *
     * @param gain multiplies every word peak, so a passage is the SAME speech louder or quieter —
     *   not different speech. That is what makes the passages comparable at all.
     */
    data class Passage(val label: String, val gain: Float, val seconds: Double)

    /**
     * The passage schedule the dynamic-range gates are scored on, and why it is shaped like this.
     *
     * NORMAL first, so the auto-gain has established an operating point before anything is asked of
     * it. LOUD and WHISPER are each preceded by a NORMAL passage, so both are measured as a
     * DEPARTURE from the same reference state rather than from each other — a whisper straight
     * after a shout would measure the release, not the contrast. Every passage is 3 s: long enough
     * that a follower in the 0.3-0.5 s range has fully settled and a session reference on a 6 s
     * constant has NOT, which is precisely the separation the two references exist to exploit.
     *
     * LOUD is 3x normal and WHISPER is 4x below it. The absolute gains are anchored so that LOUD
     * lands exactly on [speechClip]'s own full level — the level [liveDistribution] characterises —
     * which puts the whole schedule in the regime the shipped calibration was measured in, and
     * introduces no clipping beyond what that clip already has. Anchoring the schedule several
     * times quieter instead would score the analyser in its noise-floored regime, where the
     * denominator is NOISE_LIVE rather than the auto-gain and the answers say nothing about it.
     */
    val DYNAMIC_RANGE_PASSAGES = listOf(
        Passage("normal", 0.33f, 3.0),
        Passage("loud", 0.99f, 3.0),
        Passage("normal-again", 0.33f, 3.0),
        Passage("whisper", 0.0825f, 3.0),
    )

    /**
     * ONE analyser run across [passages], scored per passage — the measurement the dual-reference
     * AGC exists to pass, and the reason it has to be a single run.
     *
     * Sustained contrast and whisper separation were previously measured in separate runs, which
     * quietly let each be tuned against its own fresh session. They are in tension: an auto-gain
     * fast enough to keep loud speech off the ceiling normalises a sustained loud passage away, and
     * one slow enough to preserve that passage cannot keep ordinary speech off the ceiling. Scoring
     * both from the same continuous session is what makes the tension visible — a change that buys
     * one by spending the other cannot pass this.
     *
     * Each passage is scored over its FINAL [scoreTailSec], voiced frames only, so what is compared
     * is the SETTLED height of a sustained level and not the transient on entering it.
     */
    fun dynamicRange(
        passages: List<Passage> = DYNAMIC_RANGE_PASSAGES,
        chunkMs: Int = 40,
        sampleRate: Int = 24000,
        scoreTailSec: Double = 1.0,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
    ): Map<String, Distribution> {
        val (samples, voiced, scored) = dynamicRangeSession(passages, sampleRate, scoreTailSec)
        val chunkSamples = sampleRate * chunkMs / 1000
        var nowNs = 0L
        val analyser = AuroraAnalyser(sampleRate, { nowNs }, calibration)
        val frames = LinkedHashMap<String, ArrayList<FloatArray>>()
        for (p in passages) frames[p.label] = ArrayList()
        var idx = 0
        while (idx < samples.size) {
            val len = minOf(chunkSamples, samples.size - idx)
            val out = analyser.analyze(toPcm16(samples, idx, len), len * 2)
            val centre = idx + len / 2
            if (nowNs / 1e6f >= AuroraAnalyser.WARMUP_MS && voiced.any { centre in it }) {
                for ((label, span) in scored) if (centre in span) frames[label]!!.add(out.copyOf())
            }
            nowNs += chunkMs * 1_000_000L
            idx += len
        }
        return frames.mapValues { (_, f) -> distributionOf(f) }
    }

    /** [dynamicRange]'s one continuous session: its samples, voiced spans, and each passage's scored tail. */
    private fun dynamicRangeSession(
        passages: List<Passage>,
        sampleRate: Int,
        scoreTailSec: Double,
    ): Triple<FloatArray, List<IntRange>, Map<String, IntRange>> {
        val clip = ArrayList<Float>()
        val voiced = ArrayList<IntRange>()
        val scored = LinkedHashMap<String, IntRange>()
        for (p in passages) {
            val start = clip.size
            val (seg, segVoiced) = speechClip(
                sampleRate = sampleRate,
                totalMs = (p.seconds * 1000).toInt(),
                gain = p.gain,
            )
            for (s in seg) clip.add(s)
            for (r in segVoiced) voiced.add((r.first + start)..(r.last + start))
            val tail = (scoreTailSec * sampleRate).toInt()
            scored[p.label] = maxOf(start, clip.size - tail) until clip.size
        }
        return Triple(clip.toFloatArray(), voiced, scored)
    }

    /**
     * What the ribbon does through a LONG silence that follows speech — the other half of the same
     * question, and the one a contrast-hungry auto-gain fails: a reference that chases quiet content
     * downward eventually amplifies room tone to full height.
     *
     * Scored over the final [scoreTailSec] of [silenceSec] of -50 dBFS room noise after
     * [speechSec] of ordinary speech, so the reference has had every chance to decay into it.
     */
    fun silenceAfterSpeech(
        speechSec: Double = 4.0,
        silenceSec: Double = 6.0,
        scoreTailSec: Double = 2.0,
        chunkMs: Int = 40,
        sampleRate: Int = 24000,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
    ): Distribution {
        val (clip, scoreFrom) = silenceAfterSpeechClip(speechSec, silenceSec, scoreTailSec, sampleRate)
        val chunkSamples = sampleRate * chunkMs / 1000
        var nowNs = 0L
        val analyser = AuroraAnalyser(sampleRate, { nowNs }, calibration)
        val frames = ArrayList<FloatArray>()
        var idx = 0
        while (idx < clip.size) {
            val len = minOf(chunkSamples, clip.size - idx)
            val out = analyser.analyze(toPcm16(clip, idx, len), len * 2)
            if (idx + len / 2 >= scoreFrom) frames.add(out.copyOf())
            nowNs += chunkMs * 1_000_000L
            idx += len
        }
        return distributionOf(frames)
    }

    /** [silenceAfterSpeech]'s clip — speech, then room tone — and the first sample it is scored from. */
    private fun silenceAfterSpeechClip(
        speechSec: Double,
        silenceSec: Double,
        scoreTailSec: Double,
        sampleRate: Int,
    ): Pair<FloatArray, Int> {
        val (speech, _) = speechClip(sampleRate = sampleRate, totalMs = (speechSec * 1000).toInt())
        var rng = 0x1234_5EEDL
        val quiet = FloatArray((silenceSec * sampleRate).toInt()) {
            rng = rng * 6364136223846793005L + 1442695040888963407L
            (((rng ushr 40).toInt() and 0xFFFFFF) / 8388608f - 1f) * NOISE_FULL_SCALE.toFloat()
        }
        val clip = speech + quiet
        return clip to clip.size - (scoreTailSec * sampleRate).toInt()
    }

    // ------------------------------------------------- the shipped path: through the real tap

    /**
     * One stream the app actually builds an [AudioAnalysisTap] for, named by the two things that
     * decide what its ribbon draws: the rate the stream runs at, and the calibration that travels
     * with its source (MicSource carries it; see [AuroraCalibration]).
     */
    data class ShippedStream(val sampleRate: Int, val calibration: AuroraCalibration) {
        override fun toString(): String = "${calibration.id}@${sampleRate / 1000}kHz"
    }

    /**
     * EVERY (rate, calibration) pair a production tap is built with, so a requirement scored over
     * this list is scored over what ships rather than over the one stream it was first written
     * against:
     *
     *  - processed @ 24 kHz   the AI playback tap; the GPT-realtime duplex mic (VOICE_COMMUNICATION);
     *                         dictation (SttStreamClient) on a device without unprocessed capture.
     *  - processed @ 16 kHz   the Gemini/Grok duplex mic; the composer recorder (PcmAudioRecorder)
     *                         on a device without unprocessed capture.
     *  - unprocessed @ 16 kHz the composer recorder where UNPROCESSED is real (a Fold 6).
     *  - unprocessed @ 24 kHz dictation where UNPROCESSED is real.
     */
    val SHIPPED_STREAMS: List<ShippedStream> = listOf(
        ShippedStream(24000, AuroraCalibration.DEFAULT),
        ShippedStream(16000, AuroraCalibration.DEFAULT),
        ShippedStream(16000, AuroraCalibration.UNPROCESSED),
        ShippedStream(24000, AuroraCalibration.UNPROCESSED),
    )

    /**
     * The read length the tap-driven measurements deliver audio in — a streaming capture loop's
     * read on a 1920-byte platform minimum at 24 kHz (see [MicBuffer]). Realism rather than a
     * calibration: a tap's frames do not depend on how its audio is delivered (AudioAnalysisTapTest,
     * CaptureAnalysisRateTest), and TapEquivalenceTest re-proves that on these very clips.
     */
    const val SHIPPED_READ_MS = 40

    /** One frame the tap published, placed in the clip the way the direct feeds place a chunk. */
    private class TapFrame(val centre: Int, val elapsedMs: Float, val bands: FloatArray)

    /**
     * Deliver [clip] to a real [AudioAnalysisTap] in [readMs] reads, exactly as a capture loop
     * does, and collect every frame it publishes. The band window is therefore whatever the tap
     * ships ([AudioAnalysisTap.HOP_MS]) — chosen by production, not by the measurement.
     *
     * The tap's wall clock is pinned: it consults it ONLY to detect a delivery stall, and a
     * deterministic measurement must never see one. Its dynamics run on the audio clock, whose
     * first frame is this measurement's zero, so [TapFrame.elapsedMs] is the analyser's own elapsed
     * session time and the warm-up cut lands where the direct feeds put it.
     */
    private fun throughTap(
        clip: FloatArray,
        sampleRate: Int,
        calibration: AuroraCalibration,
        readMs: Int,
    ): List<TapFrame> {
        val tap = AudioAnalysisTap(sampleRate, calibration = calibration, nowNs = { 0L })
        val hop = tap.hopSamples
        val readSamples = sampleRate * readMs / 1000
        val frames = ArrayList<TapFrame>()
        var firstNs = -1L
        var idx = 0
        while (idx < clip.size) {
            val len = minOf(readSamples, clip.size - idx)
            tap.offer(toPcm16(clip, idx, len), len * 2) { f ->
                if (firstNs < 0L) firstNs = f.atNs
                // The frame's window is the hop that ENDS at its offset; its arrays are its own.
                val start = (f.sampleOffset - hop).toInt()
                frames.add(TapFrame(start + hop / 2, (f.atNs - firstNs) / 1e6f, f.bands))
            }
            idx += len
        }
        return frames
    }

    /**
     * [liveDistribution] on the SHIPPED path: the same speech clip, the same voiced post-warm-up
     * scoring, delivered through a real [AudioAnalysisTap] built the way production builds one.
     */
    fun liveDistributionViaTap(
        sampleRate: Int = 24000,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
        readMs: Int = SHIPPED_READ_MS,
    ): Distribution {
        val (clip, voiced) = speechClip(sampleRate)
        return distributionOf(
            throughTap(clip, sampleRate, calibration, readMs)
                .filter { f -> f.elapsedMs >= AuroraAnalyser.WARMUP_MS && voiced.any { f.centre in it } }
                .map { it.bands },
        )
    }

    /** [dynamicRange] on the SHIPPED path — one continuous session, through a real tap. */
    fun dynamicRangeViaTap(
        passages: List<Passage> = DYNAMIC_RANGE_PASSAGES,
        sampleRate: Int = 24000,
        scoreTailSec: Double = 1.0,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
        readMs: Int = SHIPPED_READ_MS,
    ): Map<String, Distribution> {
        val (samples, voiced, scored) = dynamicRangeSession(passages, sampleRate, scoreTailSec)
        val frames = LinkedHashMap<String, ArrayList<FloatArray>>()
        for (p in passages) frames[p.label] = ArrayList()
        for (f in throughTap(samples, sampleRate, calibration, readMs)) {
            if (f.elapsedMs >= AuroraAnalyser.WARMUP_MS && voiced.any { f.centre in it }) {
                for ((label, span) in scored) if (f.centre in span) frames[label]!!.add(f.bands)
            }
        }
        return frames.mapValues { (_, f) -> distributionOf(f) }
    }

    /** [silenceAfterSpeech] on the SHIPPED path — the same clip and tail, through a real tap. */
    fun silenceAfterSpeechViaTap(
        speechSec: Double = 4.0,
        silenceSec: Double = 6.0,
        scoreTailSec: Double = 2.0,
        sampleRate: Int = 24000,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
        readMs: Int = SHIPPED_READ_MS,
    ): Distribution {
        val (clip, scoreFrom) = silenceAfterSpeechClip(speechSec, silenceSec, scoreTailSec, sampleRate)
        return distributionOf(
            throughTap(clip, sampleRate, calibration, readMs)
                .filter { it.centre >= scoreFrom }
                .map { it.bands },
        )
    }

    // ------------------------------------------------------------- preview path

    /**
     * Summarise what the SETTINGS preview draws for [speaker], sampled at [fps] over [periods]
     * full demo loops.
     *
     * Scored over frames where the speaker is PRESENT — including the silent gap inside each
     * syllable, because the preview renders those gaps and they are a large part of why this
     * distribution looks nothing like [liveDistribution].
     */
    fun previewDistribution(
        speaker: AuroraSpeaker = AuroraSpeaker.HUMAN,
        fps: Int = 60,
        periods: Int = 4,
        driver: WaveformDemoDriver = WaveformDemoDriver(),
    ): Distribution {
        val frames = ArrayList<FloatArray>()
        val totalFrames = (driver.timeline.periodSec * periods * fps).toInt()
        for (f in 0 until totalFrames) {
            val t = f.toDouble() / fps
            val voice = driver.voicesAt(t).firstOrNull { it.speaker == speaker } ?: continue
            frames.add(FloatArray(AuroraAnalyser.BANDS) { j -> voice.band(j) })
        }
        return distributionOf(frames)
    }

    // ------------------------------------------------------------- AGC release isolation

    /**
     * The settled output level for an UNCHANGING tone, fed in [chunkMs] chunks.
     *
     * This is the measurement that isolates the frame-counted AGC release, and it took a wrong
     * turn to find, so the reasoning is recorded here.
     *
     * The obvious probe — the same speech clip at two chunk sizes — does NOT isolate it. Measured:
     * making the release time-based barely moves those numbers, because the Goertzel probes are
     * single-bin over a RECTANGULAR window, so changing the chunk length changes the bin
     * resolution and hence the raw band energies themselves. That effect dominates, and it is a
     * SEPARATE rate-dependence from the AGC one.
     *
     * A release time-constant probe does not isolate it either: the decay is opposed by the
     * time-based STEADY_RISE, so a level change re-settles at roughly the same wall-clock speed
     * either way.
     *
     * What the frame-counted release actually shifts is the EQUILIBRIUM the reference parks at.
     * With per-chunk decay `d` and per-chunk rise `s`, a constant band energy `v` settles the
     * reference at `s*v / (1 - d + s*d)`. `s` is time-based (it carries dtSec) but `d` is not, so
     * the balance point moves with chunk size: ~0.52*v at 32 ms against ~0.73*v at 80 ms. The
     * reference is the output's denominator, so the SAME steady tone renders at a different
     * height purely because of the buffer size upstream. Holding the tone constant removes the
     * window confound entirely — both runs analyse identical audio at identical amplitude.
     *
     * @return mean band-0 output over the final quarter of the run, once the reference has parked.
     */
    fun steadyToneLevel(
        chunkMs: Int,
        sampleRate: Int = 24000,
        totalMs: Int = 16000,
        amplitude: Double = 0.5,
        hz: Double = 220.0,
    ): Double {
        val total = sampleRate * totalMs / 1000
        val clip = FloatArray(total) { i -> (sin(2.0 * PI * hz * i / sampleRate) * amplitude).toFloat() }
        return settledBands(clip, chunkMs, sampleRate)[0]
    }

    /**
     * [steadyToneLevel] widened to ALL FOUR bands: the settled output for a steady signal that
     * drives every band at once, one sine per band at one of that band's own probe frequencies.
     *
     * This is the measurement the rate-invariance property is asserted on, and the reason it can be
     * is that a settled output does not depend on the signal's absolute level at all. The reference
     * parks proportional to the band energy `v`, and the output normalises `v` against that
     * reference, so `v` cancels: what survives is the AGC's equilibrium FRACTION and nothing else.
     * That is what makes the four tones safe together despite their mutual spectral leakage — the
     * leakage does move `v` a few percent as the window length changes, but it moves the reference
     * with it, so it cannot masquerade as (or mask) the equilibrium shift being measured. It is also
     * why the amplitude below is not a tuned number.
     *
     * @return mean per-band output over the final quarter of the run.
     */
    fun steadyToneBands(
        chunkMs: Int,
        sampleRate: Int = 24000,
        totalMs: Int = 16000,
        amplitude: Double = 0.5,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
    ): DoubleArray {
        val total = sampleRate * totalMs / 1000
        val perTone = amplitude / BAND_TONE_HZ.size
        val clip = FloatArray(total) { i ->
            var x = 0.0
            for (f in BAND_TONE_HZ) x += sin(2.0 * PI * f * i / sampleRate) * perTone
            x.toFloat()
        }
        return settledBands(clip, chunkMs, sampleRate, calibration)
    }

    /** One probe frequency per band — on-probe so each tone lands squarely in its own band. */
    private val BAND_TONE_HZ = doubleArrayOf(220.0, 800.0, 2600.0, 6800.0)

    /**
     * Feed [clip] through a real analyser in [chunkMs] chunks on an injected clock and report the
     * mean per-band output over the final quarter — i.e. once the auto-gain reference has parked.
     */
    private fun settledBands(
        clip: FloatArray,
        chunkMs: Int,
        sampleRate: Int,
        calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
    ): DoubleArray {
        val chunkSamples = sampleRate * chunkMs / 1000
        var nowNs = 0L
        val analyser = AuroraAnalyser(sampleRate, { nowNs }, calibration)
        val frames = ArrayList<FloatArray>()
        var idx = 0
        while (idx + chunkSamples <= clip.size) {
            frames.add(analyser.analyze(toPcm16(clip, idx, chunkSamples), chunkSamples * 2))
            nowNs += chunkMs * 1_000_000L
            idx += chunkSamples
        }
        val tail = frames.takeLast((frames.size / 4).coerceAtLeast(2))
        return DoubleArray(AuroraAnalyser.BANDS) { b ->
            tail.sumOf { it[b].toDouble() } / tail.size
        }
    }

    // ------------------------------------------------------------- compression exponents

    /**
     * The exponent the RENDERER alone applies, measured end-to-end by settling a real
     * [AuroraEnvelope] on constant input and fitting the log-log slope of settled level vs band
     * drive. Measured rather than restated so it tracks the renderer, not a copy of it.
     */
    fun rendererExponent(a: Float = 0.10f, b: Float = 0.40f): Double =
        logSlope(a.toDouble(), settledLevel(a), b.toDouble(), settledLevel(b))

    /** Drive a real envelope to rest on a constant band value and report the settled sheet level. */
    fun settledLevel(band: Float, sheet: Int = 0): Double {
        // restLevel 0 so nothing floors the measurement; 4 s at 120 Hz is far past every tau.
        val env = AuroraEnvelope(restLevel = 0f)
        val voice = AuroraVoice(AuroraSpeaker.HUMAN, FloatArray(AuroraAnalyser.BANDS) { band })
        repeat(480) { env.step(1f / 120f, voice) }
        return env.levels[sheet].toDouble()
    }

    /**
     * The exponent the ANALYSER alone applies, measured END-TO-END through a real analyser rather
     * than restated from its constants — the same discipline [offlineShaping] applies to the
     * offline path, and for the same reason: a harness that copies a literal cannot detect the
     * literal moving.
     *
     * HOW A STATIC CURVE IS MEASURED THROUGH AN AUTO-GAIN. Settle the analyser on a steady tone
     * until its reference has parked, then probe with ONE chunk at a fraction of that amplitude.
     * One chunk is far shorter than every reference time constant, so the denominator is frozen at
     * the settled value and the output moves only along the analyser's static input curve. The
     * probe is DOWNWARD on purpose: below the settled level both the fast follower and the slow
     * reference are in release, which is amplitude-independent, so two probes of different depth
     * see the SAME denominator and the ratio between them is the curve and nothing else. Each probe
     * runs on its own fresh analyser, so the measurement is order-independent.
     *
     * @param r1 @param r2 probe amplitudes as a fraction of the settled tone. Both are chosen deep
     *   enough to land below the soft knee, where the chain is a clean power law — the region
     *   ordinary speech occupies once HEADROOM has pushed it off the ceiling.
     */
    fun analyserExponent(
        r1: Double = 0.15,
        r2: Double = 0.35,
        chunkMs: Int = 40,
        sampleRate: Int = 24000,
    ): Double = logSlope(r1, probeAfterSettling(r1, chunkMs, sampleRate), r2, probeAfterSettling(r2, chunkMs, sampleRate))

    /**
     * Settle a fresh analyser on a full-amplitude tone, then hand it one chunk at [fraction] of
     * that amplitude and return the band-0 output of that chunk.
     */
    private fun probeAfterSettling(fraction: Double, chunkMs: Int, sampleRate: Int): Double {
        val hz = 220.0
        val amplitude = 0.5
        val chunkSamples = sampleRate * chunkMs / 1000
        val settleChunks = 12_000 / chunkMs
        var nowNs = 0L
        // THE EXPANDER IS SWITCHED OFF FOR THIS ONE MEASUREMENT, and only this one. What is being
        // fitted here is a POWER LAW — two points on a log-log line — and the expander is not one:
        // it subtracts a constant, which reads as a curve to any two-point fit and would report a
        // false exponent (measured: 1.016 at the shipped depth, against a 0.01 tolerance) for a
        // stage that bends nothing. The claim this measurement protects — that no perceptual curve
        // hides inside analyze() — is unweakened, because a gamma applied to the ratio would still
        // show with the expander off. The expander has its own gates, in AuroraNoiseExpanderTest.
        val analyser = AuroraAnalyser(
            sampleRate,
            { nowNs },
            AuroraCalibration("no-expander", noiseSubtract = 0f),
        )
        var i = 0
        fun chunkAt(scale: Double): ByteArray {
            val s = FloatArray(chunkSamples) { k ->
                (sin(2.0 * PI * hz * (i + k) / sampleRate) * amplitude * scale).toFloat()
            }
            i += chunkSamples
            return toPcm16(s, 0, chunkSamples)
        }
        repeat(settleChunks) {
            analyser.analyze(chunkAt(1.0), chunkSamples * 2)
            nowNs += chunkMs * 1_000_000L
        }
        val out = analyser.analyze(chunkAt(fraction), chunkSamples * 2)
        return out[0].toDouble()
    }

    /**
     * The LIVE composed exponent: what the mic path's displayed amplitude does as the acoustic
     * level changes, analyser and renderer together.
     */
    fun liveDisplayExponent(): Double = analyserExponent() * rendererExponent()

    /**
     * The offline (player bar) gate and lift, RECOVERED EMPIRICALLY from real
     * [AuroraAnalyser.analyzeOffline] output — the constants themselves are private, and
     * restating them here is exactly the drift this harness exists to prevent.
     *
     * The gate is a hard threshold, so it is found by bisecting for the normalised level at which
     * the output stops being exactly zero. With the gate known, the lift is the log-log slope in
     * the post-gate variable `(v - gate) / (1 - gate)`.
     *
     * @return gate and lift, plus the composed player-bar exponent (lift then the renderer).
     */
    fun offlineShaping(sampleRate: Int = 24000): OfflineShaping {
        // Normalised band level is linear in amplitude (Goertzel magnitude is), so a clip whose
        // loudest segment is amplitude 1.0 makes a segment at amplitude `a` land at v ~= a.
        fun outputAt(amplitude: Double): Double = offlineProbe(amplitude, sampleRate)

        var lo = 0.0
        var hi = 1.0
        repeat(60) {
            val mid = (lo + hi) / 2
            if (outputAt(mid) > 0.0) hi = mid else lo = mid
        }
        val gate = (lo + hi) / 2

        // Fit the lift well clear of the gate, where the measurement is not dominated by it.
        val a1 = gate + (1.0 - gate) * 0.35
        val a2 = gate + (1.0 - gate) * 0.85
        val u1 = (a1 - gate) / (1.0 - gate)
        val u2 = (a2 - gate) / (1.0 - gate)
        val lift = logSlope(u1, outputAt(a1), u2, outputAt(a2))
        return OfflineShaping(gate = gate, lift = lift, renderer = rendererExponent())
    }

    /**
     * The RMS envelope's shaping exponent, recovered the same empirical way [offlineShaping]
     * recovers the band pass's — bisect for the gate, then fit the log-log slope above it.
     *
     * This is the player bar's DEGRADED path (a clip whose band pass could not run), and it reaches
     * the renderer through exactly the same AuroraVoice, so it is a third place a perceptual curve
     * can hide. Measured through the real [AudioEnvelope.shapeRms] rather than restated.
     */
    fun rmsShapingExponent(): Double {
        fun out(v: Double) = AudioEnvelope.shapeRms(v.toFloat()).toDouble()
        var lo = 0.0
        var hi = 1.0
        repeat(60) {
            val mid = (lo + hi) / 2
            if (out(mid) > 0.0) hi = mid else lo = mid
        }
        val gate = (lo + hi) / 2
        val a1 = gate + (1.0 - gate) * 0.35
        val a2 = gate + (1.0 - gate) * 0.85
        return logSlope((a1 - gate) / (1.0 - gate), out(a1), (a2 - gate) / (1.0 - gate), out(a2))
    }

    data class OfflineShaping(val gate: Double, val lift: Double, val renderer: Double) {
        /** The player bar's composed exponent: the offline lift, then the renderer's lift again. */
        val composed: Double get() = lift * renderer
    }

    /**
     * One offline band reading for a segment at [amplitude], relative to a full-scale reference
     * segment in the same clip (which is what sets the per-band normalising peak).
     */
    private fun offlineProbe(amplitude: Double, sampleRate: Int): Double {
        // Band 0 and one of its own probe frequencies: the low band is the only one a single
        // steady tone drives hard enough to clear PEAK_FLOOR, so it is the one band whose
        // normalisation is a clean identity (a segment at amplitude `a` reads v = a).
        val band = 0
        val hz = 220.0
        val segMs = 240
        val segSamples = sampleRate * segMs / 1000
        val pcm = ShortArray(segSamples * 2)
        for (i in 0 until segSamples) {
            val ref = sin(2.0 * PI * hz * i / sampleRate)
            pcm[i] = (ref * 32767.0).toInt().coerceIn(-32768, 32767).toShort()
        }
        for (i in 0 until segSamples) {
            val v = sin(2.0 * PI * hz * (segSamples + i) / sampleRate) * amplitude
            pcm[segSamples + i] = (v * 32767.0).toInt().coerceIn(-32768, 32767).toShort()
        }
        val (windows, windowMs) = AuroraAnalyser.analyzeOffline(pcm, sampleRate)
        val perWindow = sampleRate * windowMs / 1000
        // Read the middle of the second segment, clear of both window boundaries.
        val w = (segSamples + segSamples / 2) / perWindow
        return windows[w.coerceIn(0, windows.size - 1)][band].toDouble()
    }

    // ------------------------------------------------------------------ small helpers

    private fun logSlope(x1: Double, y1: Double, x2: Double, y2: Double): Double =
        ln(y2 / y1) / ln(x2 / x1)

    /** Absolute difference, for readable tolerance assertions in the tests. */
    fun near(a: Double, b: Double, tol: Double): Boolean = abs(a - b) <= tol
}
