package com.aiblackbox.portal.ui.components.aurora

import android.util.Log
import com.aiblackbox.portal.BuildConfig
import java.util.Locale
import kotlin.math.exp

/**
 * One analysis frame: the four legacy band drives and the [AuroraAnalyser.BINS] log-frequency bins,
 * measured over windows that END at the SAME instant of the same stream.
 *
 * Two readings of one moment, published together on purpose. Handed to a renderer separately they
 * would arrive from different chunks at different cadences, and the ribbon would show a band peak
 * one frame before (or after) the spectrum that explains it.
 *
 * The arrays BELONG TO THE CALLER — they are freshly built per frame, not the tap's scratch, so a
 * producer can publish them straight into a `StateFlow` without copying (which is what the whole
 * channel exists for). That costs 28 floats per hop, ~50 times a second; handing over live scratch
 * instead would cost a copy at every call site and one silent cross-thread bug at the site that
 * forgot.
 *
 * @property sampleOffset how many samples of this stream have been consumed at the END of both
 *   windows — the frame's position in the audio, monotonic from the tap's first sample.
 * @property atNs the AUDIO-domain timestamp of that same instant (see [AudioAnalysisTap]'s clock
 *   note), which is the clock the band channel's dynamics ran on.
 * @property bands [AuroraAnalyser.BANDS] perceptual band drives, 0..1 — the shipped ribbon's input,
 *   unchanged.
 * @property bins [AuroraAnalyser.BINS] log-frequency levels, NORMALISED — one shared gain per frame
 *   (see [AudioAnalysisTap]'s bin-channel note), so the SHAPE is the spectrum's exactly and the
 *   SCALE is usable as geometry: 0..1/[AudioAnalysisTap.BIN_HEADROOM], reaching the top of that
 *   whenever the frame is as loud as the recent past. No warm-up, no gamma, no knee, no gate.
 *   Multiply by [AudioAnalysisTap.binDivisor] to recover the amplitude-calibrated level.
 */
class AnalysisFrame(
    val sampleOffset: Long,
    val atNs: Long,
    val bands: FloatArray,
    val bins: FloatArray,
)

/**
 * The live-audio seam: PCM16-LE chunks in, [AnalysisFrame]s out on a schedule of the TAP'S choosing.
 *
 * WHY IT EXISTS. Until now the analysis window and the analysis rate were both decided by whatever
 * `AudioRecord.getMinBufferSize` returned on the device (or by the size of the socket frame the
 * playback path happened to receive) — the chunk an [AuroraAnalyser] was handed WAS the read. That
 * is the caller's buffer sizing leaking into the ribbon's temporal resolution. The tap owns a PCM
 * ring and a SAMPLE-DOMAIN hop scheduler instead: the same audio produces the same frames at the
 * same offsets whether it is delivered 512 samples at a time or 4096, and a device with a big
 * minimum buffer no longer gets a coarser ribbon than one with a small one.
 *
 * TWO WINDOWS, ONE INSTANT.
 *  - The BAND window is the hop itself — a contiguous, non-overlapping partition of the stream,
 *    RECTANGULAR (the Goertzel probes simply run over the samples), which is exactly the shape
 *    [AuroraAnalyser] has always been fed. It must stay that way: the analyser is a stateful stream
 *    process whose auto-gain tracks time, and overlapping windows would feed it the same audio
 *    twice and double-count energy into that reference.
 *  - The BIN window is [AuroraSpectrum]'s, HANN-windowed and ~60 ms long, SLIDING over the shorter
 *    hop. Frequency resolution is bought with window LENGTH and cannot be bought back by updating
 *    more often, so the two are separated: the window stays long, the hop is what makes it move.
 *
 * THE CLOCK IS THE AUDIO'S. Each frame's dynamics run at `anchor + offset/sampleRate`, not at the
 * wall clock, because the tap deliberately decouples frames from delivery: five hops carved out of
 * one 100 ms socket frame would otherwise all report the same instant and the analyser would see
 * four dt-0 steps — the rate-honesty bug M1 fixed, reintroduced through the plumbing. The wall clock
 * is consulted for exactly one thing, a DELIVERY stall (a held mic, a paused drain), which a sample
 * clock cannot see at all: a gap past [AuroraAnalyser.SESSION_GAP_NS] moves the anchor forward by
 * the gap, so the analyser sees the hole and starts a new session exactly as it does today.
 *
 * WHAT DYNAMICS THE BIN CHANNEL GETS: A GAIN, AND ONLY A GAIN. It shipped RAW — amplitude-calibrated
 * absolute level, no dynamics of any kind — so that whatever the spectrum ribbon needed could be
 * chosen once, visibly, with real numbers in hand. This is that choice, and the numbers said the
 * channel was missing exactly one thing. Measured through this tap on the composer path: the loudest
 * bin of ordinary speech read p50 0.019 / p90 0.064, and NONE of the 24 bins ever reached 1.0 — a
 * bar chart drawn against a 0..1 geometry range would have sat in the bottom fifteenth of it, which
 * is why wiring the channel to a style would have shown nothing. What it did NOT need was noise
 * rejection: room tone reads 0.001-0.005 per bin against a speech peak of 0.192, a 59x contrast the
 * bands never had. So this is gain, deliberately and only — no gate, no floor subtraction, no curve.
 *
 * HOW MUCH GAIN, AND WHY IT IS NOT A CONSTANT. Speech level varies by 10-20 dB between a phone at
 * arm's length and one held to the face, so the multiplier that puts ordinary speech in range
 * (measured: ~10x) puts loud speech at 1.9 and clamps the whole top decile flat. The gain is
 * therefore ONE reference divided into EVERY bin — [BIN_REF_FLOOR], [BIN_HEADROOM] and
 * [BIN_REF_RELEASE_TAU_SEC] below say exactly how — which is a fixed 10.2x for anything at or below
 * the floor (room tone, a whisper: absolute level preserved, contrast preserved to the last decimal
 * because a shared divisor cannot change a ratio) and backs off only where a constant would have
 * clamped. One reference for all 24 bins, never per-bin: per-bin normalisation would drive every bar
 * to the same height and delete the spectrum, which is the only thing this channel measures.
 *
 * Note that the app already HAS one perceptual curve and exactly one — [Aurora.DISPLAY_GAMMA] in the
 * renderer — so a bin channel that reaches the renderer will be curved there; a bin driving geometry
 * directly is the case that stays linear, and this normalisation deliberately adds no curve to it.
 *
 * Pure Kotlin (no android.*), so every property above is asserted on the JVM in AudioAnalysisTapTest.
 *
 * NOT thread-safe, and for the same reason [AuroraAnalyser] is not (D1): the ring, the schedule and
 * the analyser ARE the instance. One tap per stream, fed from that stream's reader thread — the mic
 * and the AI playback are analysed at the same time and must not share one.
 *
 * @param sampleRate the stream's real rate in Hz.
 * @param hopMs audio per published frame; the analysis cadence.
 * @param calibration the band channel's measured constant set — the CAPTURE SOURCE's, since that
 *   is what the numbers describe (see [AuroraCalibration]). Handed straight to the analyser this
 *   tap owns; the bin channel has no dynamics to calibrate.
 * @param nowNs injectable wall clock, used ONLY for stall detection (see above) and, from that same
 *   one read per [offer], the rate meter.
 * @param label names this stream in the W32 rate meter ("composer-mic/UNPROCESSED"); an empty label
 *   turns the meter off.
 * @param rateReport where the meter's line goes: logcat in debug builds, nowhere in release.
 */
class AudioAnalysisTap(
    val sampleRate: Int,
    hopMs: Float = HOP_MS,
    calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
    private val nowNs: () -> Long = System::nanoTime,
    private val label: String = "",
    private val rateReport: (String) -> Unit = ::debugRateReport,
) {

    init {
        require(sampleRate > 0) { "sampleRate must be positive, was $sampleRate" }
        require(hopMs > 0f) { "hopMs must be positive, was $hopMs" }
    }

    /**
     * Samples between published frames. Floored at [AuroraAnalyser.MIN_SAMPLES] because below that
     * a window cannot resolve the low band at all and the analyser reports silence — a hop that
     * short would publish a dead ribbon at a great rate.
     */
    val hopSamples: Int =
        Math.round(sampleRate * hopMs / 1000f).coerceAtLeast(AuroraAnalyser.MIN_SAMPLES)

    /**
     * The band channel's window, which IS the hop — named so the decision is visible and pinned.
     * See the two-windows note above for why it does not overlap.
     */
    val bandWindowSamples: Int = hopSamples

    /** The bin channel's window: [AuroraSpectrum]'s rate-derived transform size. */
    val fftSize: Int = AuroraSpectrum.fftSizeFor(sampleRate)

    private val analyser = AuroraAnalyser(sampleRate, { frameNs }, calibration)
    private val spectrum = AuroraSpectrum(sampleRate)

    /** Longest window any channel reads, and therefore how much past the ring has to hold. */
    private val capacitySamples = maxOf(hopSamples, fftSize)

    /** The past, as the bytes that arrived — so the band channel re-reads the ORIGINAL PCM16-LE. */
    private val ring = ByteArray(capacitySamples * 2)

    /** Where the next sample lands, in samples. */
    private var writeIndex = 0

    /** Samples accumulated toward the next hop boundary. Carries across offers. */
    private var pending = 0

    /** Samples consumed since the last [reset] — the frame offset, and the audio clock's input. */
    private var totalSamples = 0L

    private var anchorNs = 0L
    private var anchored = false

    /** The instant the frame being emitted describes; what the analyser's injected clock reads. */
    private var frameNs = 0L

    /**
     * The bin channel's reference: a PEAK HOLD on the loudest bin of the frame, released on
     * [BIN_REF_RELEASE_TAU_SEC]. Rises instantly, which is what bounds the published bins at
     * 1/[BIN_HEADROOM] by construction rather than by a clamp — the divisor is never smaller than
     * the loudest bin it divides.
     */
    private var binRef = 0f

    /** [BIN_REF_RELEASE_TAU_SEC] as a per-frame factor. One hop of audio, in seconds — so the release is the same length of TIME at any rate or hop. */
    private val binRefFall: Float =
        exp(-(hopSamples.toFloat() / sampleRate) / BIN_REF_RELEASE_TAU_SEC)

    /**
     * What the most recent frame's bins were divided by: `max(binRef, BIN_REF_FLOOR) * BIN_HEADROOM`.
     * Multiply a published bin by this to recover [AuroraSpectrum]'s amplitude-calibrated level —
     * the normalisation is a gain, and this is the gain, so nothing is destroyed by applying it.
     *
     * `@Volatile` for the same narrow reason [AuroraAnalyser.warmScale] is: written on the audio
     * thread, read from the frame thread. It does not make this class thread-safe.
     */
    @Volatile
    var binDivisor: Float = BIN_REF_FLOOR * BIN_HEADROOM
        private set

    // W32 rate meter: frames published per WALL second, so a device run shows whether the screen
    // really gets ~1000/HOP_MS frames a second (the sample clock guarantees it in audio time only).
    private var rateArmed = false
    private var rateWindowStartNs = 0L
    private var rateFrames = 0

    /** Linear window scratch, filled from the ring per frame. */
    private val bandBytes = ByteArray(hopSamples * 2)
    private val binBytes = ByteArray(fftSize * 2)
    private val binFloats = FloatArray(fftSize)

    /**
     * Feed one live PCM16-LE mono chunk and publish every frame it completes, in order.
     *
     * @param lenBytes valid bytes in [pcm]. Readers hand back SHORT reads into a full-size buffer,
     *   so the tail is the previous read's audio — plausible, and wrong. Clamped to the array (an
     *   over-long stale count must not take the mic down with an index crash) and rounded down to
     *   whole samples, exactly as [AuroraAnalyser.analyze] does with the same input.
     * @param onFrame called once per completed hop, on the calling thread, before this returns. A
     *   producer that publishes into a `StateFlow` should hoist its lambda rather than allocate one
     *   per chunk.
     */
    fun offer(pcm: ByteArray, lenBytes: Int = pcm.size, onFrame: (AnalysisFrame) -> Unit) {
        val samples = lenBytes.coerceIn(0, pcm.size) / 2
        if (samples <= 0) return

        val wall = nowNs()
        if (!anchored) {
            anchorNs = wall
            anchored = true
        } else {
            // The ONLY use of wall time: a delivery stall leaves no gap in a sample clock, so
            // without this the analyser would carry a finished warm-up and the previous speaker's
            // auto-gain straight into whatever comes back.
            val stalled = wall - (anchorNs + samplesToNs(totalSamples))
            if (stalled > AuroraAnalyser.SESSION_GAP_NS) anchorNs += stalled
        }
        if (label.isNotEmpty()) meterRate(wall)

        var i = 0
        while (i < samples) {
            val take = minOf(samples - i, hopSamples - pending)
            var k = 0
            while (k < take) {
                val src = 2 * (i + k)
                val dst = 2 * writeIndex
                ring[dst] = pcm[src]
                ring[dst + 1] = pcm[src + 1]
                writeIndex++
                if (writeIndex == capacitySamples) writeIndex = 0
                k++
            }
            i += take
            pending += take
            totalSamples += take
            if (pending >= hopSamples) {
                pending = 0
                emit(onFrame)
            }
        }
    }

    /**
     * Drop the stream: the ring, the part-filled hop, the offsets and the analyser's session. The
     * next [offer] behaves exactly like the first one a fresh tap would see.
     */
    fun reset() {
        ring.fill(0)
        writeIndex = 0
        pending = 0
        totalSamples = 0L
        anchorNs = 0L
        anchored = false
        frameNs = 0L
        binRef = 0f
        binDivisor = BIN_REF_FLOOR * BIN_HEADROOM
        analyser.reset()
        rateArmed = false
    }

    /**
     * Report frames per wall second about every [RATE_WINDOW_NS], measured at the START of an offer
     * so the window covers exactly the frames that audio already produced. Reuses [offer]'s single
     * clock read; never reads the clock itself.
     */
    private fun meterRate(wall: Long) {
        if (!rateArmed) {
            rateArmed = true
            rateWindowStartNs = wall
            rateFrames = 0
            return
        }
        val elapsed = wall - rateWindowStartNs
        if (elapsed < RATE_WINDOW_NS) return
        val fps = rateFrames * 1e9 / elapsed
        rateReport(String.format(Locale.ROOT,
            "%s: %.1f frames/s over %.2fs wall (expected %.1f; sr=%d hop=%d)",
            label, fps, elapsed / 1e9, sampleRate.toDouble() / hopSamples, sampleRate, hopSamples))
        rateWindowStartNs = wall
        rateFrames = 0
    }

    private fun emit(onFrame: (AnalysisFrame) -> Unit) {
        frameNs = anchorNs + samplesToNs(totalSamples)

        copyOut(bandBytes, hopSamples)
        // The analyser is handed the ORIGINAL bytes, not a re-encode, so the band channel is the
        // shipped one by construction rather than by a proof about round-tripping.
        val bands = analyser.analyze(bandBytes, bandBytes.size)

        // Early frames have less past than the window: zero-padded and quieter, in proportion (see
        // AuroraSpectrum). Withholding them instead would freeze the ribbon for a window's worth of
        // audio at the start of every session, on top of the analyser's own warm-up ramp.
        val available = minOf(totalSamples, fftSize.toLong()).toInt()
        copyOut(binBytes, available)
        for (k in 0 until available) binFloats[k] = AuroraAnalyser.sampleAt(binBytes, k)
        val raw = spectrum.analyze(binFloats, 0, available)

        // ONE divisor for all 24 bins (see the bin-channel note): the reference takes any rise
        // immediately and releases on a time constant, so it is never below the loudest bin of this
        // frame and the published values cannot exceed 1/BIN_HEADROOM — the bound is arithmetic, not
        // a clamp, which is why nothing here has to decide what to do about an overflow.
        var loudest = 0f
        for (v in raw) if (v > loudest) loudest = v
        binRef = maxOf(loudest, binRef * binRefFall)
        binDivisor = maxOf(binRef, BIN_REF_FLOOR) * BIN_HEADROOM
        // The frame owns its arrays (see AnalysisFrame) — this is the copy that was already being
        // made, with the gain folded into it rather than a second pass over 24 floats.
        val bins = FloatArray(AuroraAnalyser.BINS) { raw[it] / binDivisor }

        rateFrames++
        onFrame(AnalysisFrame(totalSamples, frameNs, bands, bins))
    }

    /** The most recent [samples] samples, oldest-first, into [dst]. At most two runs — it wraps. */
    private fun copyOut(dst: ByteArray, samples: Int) {
        if (samples <= 0) return
        val start = ((writeIndex - samples) % capacitySamples + capacitySamples) % capacitySamples
        val head = minOf(samples, capacitySamples - start)
        System.arraycopy(ring, start * 2, dst, 0, head * 2)
        if (head < samples) System.arraycopy(ring, 0, dst, head * 2, (samples - head) * 2)
    }

    /**
     * Sample count to nanoseconds. Integer arithmetic so the clock is exact and monotonic rather
     * than drifting by a float's worth per frame; the product overflows only past ~9.2e9 samples
     * (four days at 24 kHz), well beyond any session.
     */
    private fun samplesToNs(samples: Long): Long = samples * 1_000_000_000L / sampleRate

    companion object {

        /**
         * Audio per frame, ~50 Hz. Chosen to match [AuroraAnalyser.OFFLINE_WINDOW_MS], so a stream
         * and a pre-decoded file describe the same audio with the same granularity — the live
         * ribbon and the player bar's envelope stop being two different measurements of one sound.
         * It also sits at the shared feed's ring cadence, so the history ring is fed about one
         * analysis frame per slot rather than repeating or dropping.
         *
         * Kept as its own constant rather than derived from either: they agree today for a reason,
         * but the offline window mirrors AudioEnvelope's RMS decode while this is a UI cadence, and
         * a change to one is not automatically right for the other.
         */
        const val HOP_MS = 20f

        /** How often the W32 rate meter reports, in wall time. */
        const val RATE_WINDOW_NS = 2_000_000_000L

        /** The meter's default sink: logcat tag `AuroraTapRate`, debug builds only. */
        fun debugRateReport(line: String) {
            if (BuildConfig.DEBUG) Log.d("AuroraTapRate", line)
        }

        /**
         * The bin reference's FLOOR — the level below which the normalisation stops adapting and
         * becomes a fixed `1/(FLOOR*HEADROOM)` = 10.2x multiplier.
         *
         * This is the constant that keeps the channel honest about quiet, and it is placed by
         * measurement from both sides. It sits just ABOVE the loudest bin of ordinary speech
         * (measured p90 0.064 through the composer path), so ordinary speech and everything quieter
         * than it — a whisper, room tone, a distant talker — is multiplied by the SAME number and
         * keeps its absolute level and its 59x noise contrast exactly. And it sits far BELOW loud
         * speech (p90 0.192), which is the region a fixed multiplier would have clamped, so that is
         * the only region the reference is allowed to touch.
         *
         * MEASURED at 20 ms hops on the composer path, loudest bin, before -> after:
         *
         *   signal                        raw p50/p90     published p50/p90
         *   pink room tone -45 dBFS       0.0017 0.0022   0.017 0.022   (target: under 0.05)
         *   pink room tone -40 dBFS       0.0030 0.0038   0.030 0.039
         *   white hiss     -45 dBFS       0.0031 0.0034   0.032 0.035
         *   whisper                       0.0049 0.0159   0.050 0.162
         *   ORDINARY speech               0.0195 0.0635   0.193 0.636   (target: 0.5..0.8)
         *   LOUD speech                   0.0590 0.1924   0.221 0.714   (a constant 10.2x: 1.96)
         *
         * The last two rows are the whole argument for a reference over a multiplier: 12 dB of
         * talker level moves the peak from 0.64 to 0.71 instead of from 0.65 to a clamp.
         */
        const val BIN_REF_FLOOR = 0.07f

        /**
         * How far under the top of the range a frame at its own recent peak lands: the loudest bin
         * reads exactly `1/BIN_HEADROOM` = 0.714 there, and can never read more (the reference rises
         * instantly, so the divisor is never below what it divides).
         *
         * 1.4 rather than 1.0 for the reason the band channel keeps [AuroraAnalyser.HEADROOM]: a
         * consumer drawing geometry needs somewhere for a loud moment to GO, and a channel whose
         * every peak touches the ceiling has already spent that. It is not a curve — every bin in
         * the frame is divided by the same number, so the spectrum's shape is untouched.
         */
        const val BIN_HEADROOM = 1.4f

        /**
         * The reference's release. Pinned from both sides, measured on speech at 20 ms hops as the
         * ratio between the loudest bin's typical frame and its loud frame (p50/p90) — i.e. how much
         * of the utterance's own dynamics survive the normalisation:
         *
         *   0.8 s -> 0.41    1.5 s -> 0.31    3.0 s -> 0.30
         *
         * From BELOW by the syllable, exactly as [AuroraAnalyser.FAST_RELEASE_TAU_SEC] is: a
         * reference that falls inside a ~230 ms syllable trough lifts the troughs toward the peaks
         * and flattens the bars — visible above as 0.8 s keeping a third less contrast than 1.5 s.
         * From ABOVE by the sentence: the reference is what a loud transient leaves behind, so a
         * constant much longer than a phrase means one door slam holds the whole spectrum down for
         * the rest of it. 1.5 s already buys 97% of the contrast that 3.0 s does, with half the
         * memory of a bang.
         */
        const val BIN_REF_RELEASE_TAU_SEC = 1.5f
    }
}
