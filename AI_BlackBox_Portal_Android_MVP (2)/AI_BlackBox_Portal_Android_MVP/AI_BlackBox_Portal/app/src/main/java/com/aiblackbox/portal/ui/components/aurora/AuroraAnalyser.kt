package com.aiblackbox.portal.ui.components.aurora

import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * Four-band spectral analysis of PCM16 mono audio via Goertzel probes — microseconds per
 * frame, no FFT, UI-grade band energies for the Aurora ribbon.
 *
 * Bands (speech-tuned):
 *   [0] LOW      ~120-220 Hz  — voicing / bass body
 *   [1] MID-LOW  ~500-800 Hz  — vowel energy (the "voice" itself)
 *   [2] MID-HIGH ~1.6-2.6 kHz — formants / consonant colour
 *   [3] HIGH     ~5-6.8 kHz   — sibilance ("s", "sh" sparkle)
 *
 * Ported from whisper-everywhere's `object AudioBands` (2026-07-18). Every probe frequency,
 * band gain and AGC constant below is MEASURED on-device — do not "tidy" them. The structural
 * departures from that original are deliberate and each one is a bug fix; they are called out
 * at the code they affect so nobody restores parity and reintroduces the bug:
 *
 *   D1 It was a singleton. BlackBox analyses the human's mic and the AI's playback AT THE SAME
 *      TIME (red ribbon + blue ribbon in one container), so a shared auto-gain reference let
 *      the AI's loudness rescale the human's ribbon. One instance per stream now.
 *   D2 It hardcoded 16 kHz. Our real rates are 24 kHz (composer mic, GPT-realtime voice mic,
 *      voice AI playback), 16 kHz (other voice backends) and whatever a decoded TTS file says
 *      — so a fixed 16 kHz mis-probed three of four sources by 1.5x. It is a parameter now.
 *   D3 Warm-up counted CHUNKS (60), so the identical constant meant 1.9 s of ramp on a 32 ms
 *      feed and 6.0 s on a 100 ms feed — the reported "takes a few seconds for the visualizer
 *      to catch up". It counts elapsed TIME now.
 *   D4 The auto-gain started from a hardcoded guess and crept toward reality at 0.995/frame,
 *      so the first second was mis-scaled on top of the warm-up ramp. It is seeded from the
 *      first analysed chunk now.
 *
 * Pure Kotlin on purpose: no android.*, no Compose. The renderer consumes the output; this
 * class is fully unit-testable on the JVM (see AuroraAnalyserTest).
 *
 * NOT thread-safe by design — one instance belongs to one audio stream, fed from that
 * stream's reader thread.
 *
 * @param sampleRate the stream's real sample rate in Hz (D2).
 * @param nowNs injectable clock so the time-based warm-up (D3) is testable without sleeping.
 */
class AuroraAnalyser(
    val sampleRate: Int,
    private val nowNs: () -> Long = System::nanoTime,
) {

    init {
        require(sampleRate > 0) { "sampleRate must be positive, was $sampleRate" }
    }

    /**
     * Goertzel coefficients derived from THIS stream's rate (D2). Probes at or near Nyquist
     * are dropped rather than kept: they cannot measure anything, they can only alias
     * lower-frequency content into the band. An 8 kHz stream (mu-law telephony TTS) therefore
     * reports no sibilance band at all, which is the truth — rather than a ribbon sparkling
     * at a folded-over vowel.
     */
    private val coeffs: Array<FloatArray> = probeCoeffs(sampleRate)

    /** Per-band auto-gain reference. Seeded from the first chunk of each session (D4). */
    private val peaks = FloatArray(BANDS) { PEAK_INIT[it] }

    private var sessionOpen = false
    private var seeded = false
    private var sessionStartNs = 0L
    private var lastAnalyzeNs = 0L

    /**
     * Output attenuation for the session's opening moments, [WARM_FLOOR]..1. No fixed
     * reference fits both far-field and close dictation, so instead of slamming a calibration
     * the OUTPUT crescendos over [WARMUP_MS]. Exposed because it is the property D3 is about
     * (and it is a pure function of elapsed session time, nothing else).
     *
     * `@Volatile` only because this one field is written on the audio thread and is the field
     * a renderer would sensibly read from the frame thread — it does not make the class
     * thread-safe, and analyze() still belongs to exactly one thread.
     */
    @Volatile
    var warmScale: Float = WARM_FLOOR
        private set

    /** Drop the session: the next chunk starts a fresh warm-up and re-seeds the gain. */
    fun reset() {
        sessionOpen = false
        seeded = false
        sessionStartNs = 0L
        lastAnalyzeNs = 0L
        warmScale = WARM_FLOOR
        for (b in peaks.indices) peaks[b] = PEAK_INIT[b]
    }

    /**
     * Analyse one live PCM16-LE mono chunk -> 4 band energies, each roughly 0..1.
     *
     * @param lenBytes valid bytes in [pcm] (readers hand back short reads).
     * @param rawOut optional: filled with the PRE-auto-gain band energies. Tests and
     *   calibration tooling read these; the auto-gain deliberately normalises them away, so
     *   this is the only way to see absolute level. Production callers pass nothing.
     */
    fun analyze(pcm: ByteArray, lenBytes: Int, rawOut: FloatArray? = null): FloatArray {
        val out = FloatArray(BANDS)
        // Clamp rather than trust: this runs on an audio-reader thread, and an over-long
        // length (a stale count after a short read) must not take the mic down with an
        // index crash.
        val n = lenBytes.coerceIn(0, pcm.size) / 2
        // Too short to probe: report silence and leave the session untouched, so a runt read
        // can neither re-ramp the warm-up nor drag the gain reference down.
        if (n < MIN_SAMPLES) return out

        val now = nowNs()
        // A >1 s hole in the feed means a new session (new speaker, new distance, new device).
        // `sessionOpen` is what makes the FIRST-EVER chunk open a session too — comparing
        // against a zero timestamp does not, and an injected clock legitimately starts at 0.
        val newSession = !sessionOpen || (now - lastAnalyzeNs) > SESSION_GAP_NS
        if (newSession) {
            sessionOpen = true
            seeded = false
            sessionStartNs = now
            for (b in peaks.indices) peaks[b] = PEAK_INIT[b]
        }
        val dtSec = if (newSession) 0f
        else ((now - lastAnalyzeNs) / 1e9).toFloat().coerceIn(0f, MAX_DT_SEC)
        lastAnalyzeNs = now

        // D3: elapsed TIME, not frames. Chunk duration varies by source (32 ms mic reads,
        // 100 ms socket frames), so anything counted per-frame ramps at a different speed on
        // every stream. Do NOT reintroduce a frame counter here.
        val elapsedMs = (now - sessionStartNs) / 1_000_000f
        warmScale = WARM_FLOOR + (1f - WARM_FLOOR) * (elapsedMs / WARMUP_MS).coerceIn(0f, 1f)

        // Acquisition = the same window as the warm-up ramp. While it runs, the gain reference
        // is allowed to fall FAST (time-constant based, so it behaves identically at any chunk
        // size) — that is what stops a door slam or a plosive on the very first chunk from
        // defining the session's loudness and leaving the ribbon dead for seconds afterwards.
        // Once acquisition ends the reference reverts to the original's measured behaviour.
        val acquiring = elapsedMs < WARMUP_MS
        val fall = if (acquiring) 1f - exp(-dtSec / ACQUIRE_TAU_SEC) else 0f

        for (b in 0 until BANDS) {
            val bandCoeffs = coeffs[b]
            var sum = 0f
            for (c in bandCoeffs) sum += goertzel(c, n) { i -> sampleAt(pcm, i) }
            // Speech energy falls off with frequency — GAINS lift the upper bands into a
            // comparable range before the per-band auto-gain sees them.
            val v = if (bandCoeffs.isEmpty()) 0f else sum / bandCoeffs.size * GAINS[b]
            rawOut?.set(b, v)

            val ref = peaks[b]
            val next = when {
                // D4: seed from the FIRST chunk instead of converging toward it. The clamp is
                // asymmetric on purpose. Seeding too LOW costs ~4 frames of saturation (the
                // 0.6 rise blend climbs fast, ~130 ms) — invisible. Seeding too HIGH costs
                // SECONDS of dead ribbon, because the release is slow. The measured far-field
                // p90 sits 2-3x BELOW PEAK_INIT, i.e. the common case is quieter than the old
                // cold-start guess, so trusting the observation downward is where the win is;
                // above PEAK_INIT we keep the measured prior and let the blend climb.
                !seeded -> v.coerceIn(PEAK_FLOOR[b], PEAK_INIT[b])

                // Acquisition: rise on the measured 0.6 blend (a plosive still spikes the
                // ribbon once, as it should, but cannot instantly rescale the session), fall
                // on the time-based constant.
                acquiring -> ref + (v - ref) * (if (v > ref) RISE_BLEND else fall)

                else -> {
                    // Steady state, byte-for-byte the original: decay toward the floor first,
                    // then blend a rise up from the DECAYED value. Note the decay is
                    // per-frame, so its real-time constant does vary with chunk size — that is
                    // inherited, measured behaviour, deliberately left alone. Acquisition
                    // above is what covers the case where it actually hurt.
                    val decayed = maxOf(ref * STEADY_DECAY, PEAK_FLOOR[b])
                    if (v > decayed) decayed + (v - decayed) * RISE_BLEND else decayed
                }
            }
            peaks[b] = next.coerceAtLeast(PEAK_FLOOR[b])

            // Auto-gain: normalise against the per-band reference. Absolute levels vary wildly
            // between devices and distances; without this the bands sit at 0.1-0.3 and the
            // ribbon looks sleepy. With it, each speaker's OWN dynamics span the full 0..1.
            out[b] = ((v / peaks[b]) * warmScale).coerceIn(0f, 1f)
        }
        seeded = true
        return out
    }

    companion object {
        const val BANDS = 4

        /**
         * Goertzel probe pairs per band. `internal` (like the constants below) so tests can
         * assert against the REAL values rather than restating them and drifting.
         */
        internal val PROBES = arrayOf(
            floatArrayOf(120f, 220f),
            floatArrayOf(500f, 800f),
            floatArrayOf(1600f, 2600f),
            floatArrayOf(5000f, 6800f),
        )

        /** Speech energy falls off with frequency — lift the upper bands into a comparable range. */
        internal val GAINS = floatArrayOf(1.0f, 1.4f, 2.6f, 4.0f)

        /**
         * Per-band auto-gain constants, from an on-device acoustic measurement (Fold 6
         * speaker->mic loop of real speech, 2026-07-18, 305 voiced chunks): far-field p90 per
         * band = [0.017, 0.045, 0.077, 0.006]; near-field dictation runs several times hotter.
         * INIT sits ~2-3x the far-field p90 (mid-ground toward near-field) and now doubles as
         * the seed ceiling; FLOOR ~= p90/6 so quiet-room noise cannot define "loud" — an
         * earlier global 0.08 floor sat ABOVE the sibilance band's measured maximum and kept
         * it permanently dead in far-field use.
         */
        internal val PEAK_INIT = floatArrayOf(0.05f, 0.10f, 0.12f, 0.02f)
        internal val PEAK_FLOOR = floatArrayOf(0.004f, 0.008f, 0.012f, 0.002f)

        internal const val RISE_BLEND = 0.6f
        internal const val STEADY_DECAY = 0.995f

        /** Output scale at the instant a session starts; ramps to 1 over [WARMUP_MS]. */
        internal const val WARM_FLOOR = 0.35f

        /**
         * D3: ~600 ms, chosen so the crescendo reads as intent rather than lag. The original's
         * 60-chunk equivalent was 1.9 s on a 32 ms feed and 6.0 s on a 100 ms one.
         */
        internal const val WARMUP_MS = 600f

        /** A hole this long in the feed means a new session (kept from the original). */
        private const val SESSION_GAP_NS = 1_000_000_000L

        /**
         * Acquisition release time constant. Over one [WARMUP_MS] window the reference sheds
         * e^-4 (~98%) of any excess regardless of chunk size — the whole point of expressing
         * it in seconds rather than frames.
         */
        private const val ACQUIRE_TAU_SEC = 0.15f

        /** Clamp for a stalled reader so one late chunk cannot dump the whole acquisition. */
        private const val MAX_DT_SEC = 0.5f

        /**
         * Below this a chunk cannot resolve the low band at all — report silence. `internal`
         * because the offline path applies the SAME rule to its trailing partial window, and two
         * copies of this number would drift.
         */
        internal const val MIN_SAMPLES = 64

        /** A probe must sit clear of Nyquist or it measures aliases, not content. */
        private const val NYQUIST_MARGIN = 0.45f

        /** Mirrors AudioEnvelope.decode's window so the two envelopes line up 1:1. */
        internal const val OFFLINE_WINDOW_MS = 20

        /**
         * Silence shaping, same constants and same reasoning as AudioEnvelope.decode: a small
         * noise GATE keeps true silence/hum FLAT, then a LIFT (gamma < 1) raises everything
         * above it so quiet-but-present speech still animates.
         */
        private const val GATE = 0.05f
        private const val LIFT = 0.55f

        /**
         * OFFLINE entry point — for audio that is fully known before playback starts (the TTS
         * path pre-decodes the whole file; see AudioEnvelope.decode).
         *
         * There is no warm-up, no auto-gain and no clock here: with the entire clip in hand the
         * right normalisation is simply its own per-band peak. That DELETES the startup lag on
         * that path rather than shortening it, and makes the result deterministic — the same
         * bytes always produce the same envelope, at any sample rate.
         *
         * @return per-window band energies (`[window][band]`, each 0..1, peak-normalised per
         *   band over the whole clip) paired with the window length in ms — the same shape
         *   AudioEnvelope.decode returns for its RMS envelope.
         */
        fun analyzeOffline(
            pcm: ShortArray,
            sampleRate: Int,
            sampleCount: Int = pcm.size,
            windowMs: Int = OFFLINE_WINDOW_MS,
        ): Pair<Array<FloatArray>, Int> {
            // Delegated, not duplicated: [AuroraOfflineBands] IS this analysis, and a caller that
            // already holds the whole clip is just the easy way to feed it. Two implementations of
            // a windowing rule is two places for the window boundaries to drift apart.
            val stream = AuroraOfflineBands(sampleRate, windowMs)
            val total = sampleCount.coerceIn(0, pcm.size)
            for (i in 0 until total) stream.add(pcm[i])
            return stream.finish() to windowMs
        }

        /**
         * Goertzel coefficients for [sampleRate], one row per band, probes at or near Nyquist
         * dropped. Shared by the live analyser and the offline pass so a rate means the same thing
         * on both paths.
         */
        internal fun probeCoeffs(sampleRate: Int): Array<FloatArray> = Array(BANDS) { b ->
            PROBES[b].filter { it < sampleRate * NYQUIST_MARGIN }
                .map { goertzelCoeff(it, sampleRate) }
                .toFloatArray()
        }

        /**
         * Normalise raw offline band energies IN PLACE- each band against its own peak over the
         * whole clip, then the same GATE/LIFT shaping the RMS envelope gets.
         *
         * This is the pass that makes the offline path deterministic and lag-free, and it is also
         * why the offline path is two-pass: nothing can be published until the last window has been
         * measured. Peak-normalising a rolling prefix instead would rescale the ribbon mid-clip.
         */
        internal fun normalizeOfflineBands(windows: List<FloatArray>) {
            for (b in 0 until BANDS) {
                var peak = 0f
                for (w in windows.indices) if (windows[w][b] > peak) peak = windows[w][b]
                // A band whose loudest moment never clears its measured noise floor has no
                // content — normalising it would amplify hiss to full scale.
                if (peak < PEAK_FLOOR[b]) {
                    for (w in windows.indices) windows[w][b] = 0f
                    continue
                }
                val norm = 1f / peak
                for (w in windows.indices) {
                    val v = (windows[w][b] * norm).coerceIn(0f, 1f)
                    windows[w][b] = if (v <= GATE) 0f else ((v - GATE) / (1f - GATE)).pow(LIFT)
                }
            }
        }

        /** PCM16-LE convenience overload of [analyzeOffline] (what the live path speaks). */
        fun analyzeOffline(
            pcm: ByteArray,
            sampleRate: Int,
            lenBytes: Int = pcm.size,
            windowMs: Int = OFFLINE_WINDOW_MS,
        ): Pair<Array<FloatArray>, Int> {
            val n = lenBytes.coerceIn(0, pcm.size) / 2
            val shorts = ShortArray(n) { i ->
                (((pcm[2 * i + 1].toInt() shl 8) or (pcm[2 * i].toInt() and 0xFF))).toShort()
            }
            return analyzeOffline(shorts, sampleRate, n, windowMs)
        }

        private fun goertzelCoeff(freq: Float, sampleRate: Int): Float =
            (2.0 * cos(2.0 * Math.PI * freq / sampleRate)).toFloat()

        /** One PCM16-LE sample of [pcm] as -1..1. Sign-extends the high byte, masks the low. */
        private fun sampleAt(pcm: ByteArray, i: Int): Float {
            val lo = pcm[2 * i].toInt() and 0xFF
            val hi = pcm[2 * i + 1].toInt()
            return ((hi shl 8) or lo) / 32768f
        }
    }
}

/**
 * Streaming form of [AuroraAnalyser.analyzeOffline], for a caller already walking the clip's PCM
 * sample by sample (the TTS decode loop) that should not have to hold the clip to get bands.
 *
 * Feed it MONO samples in order, then call [finish] once.
 *
 * **Memory is O(windows), not O(samples)** — four floats per 20 ms, so ~240 KB for a five-minute
 * clip whatever its sample rate. That is the whole reason this class exists: an earlier revision
 * buffered the decoded PCM into one array (Goertzel needs contiguous samples, and the per-band
 * normalisation needs the whole clip measured before anything is published) and capped it at 4M
 * samples, which silently dropped the band pass past ~2.9 min at 24 kHz / ~1.6 min at 44.1 kHz.
 * `elevenlabs_music` writes songs of up to FIVE minutes through the same player bar, so the cap
 * meant generated music always fell back to one RMS value smeared across all four sheets. The cap
 * is deliberately GONE rather than raised — do not reintroduce one, and do not "optimise" this
 * back into a whole-clip buffer.
 *
 * Windowing, gains, Nyquist rule and normalisation are the companion's, unchanged; only the
 * plumbing differs, and [AuroraAnalyser.analyzeOffline] runs through here too so the two can
 * never disagree.
 *
 * NOT thread-safe — one instance belongs to one decode.
 *
 * @param sampleRate the clip's real rate in Hz. Positive; the caller decides what an unusable
 *   container rate means (see AudioEnvelope.bandStream), because losing the bands is recoverable
 *   and losing the whole envelope is not.
 */
class AuroraOfflineBands(
    sampleRate: Int,
    windowMs: Int = AuroraAnalyser.OFFLINE_WINDOW_MS,
) {

    init {
        require(sampleRate > 0) { "sampleRate must be positive, was $sampleRate" }
        require(windowMs > 0) { "windowMs must be positive, was $windowMs" }
    }

    private val perWindow = (sampleRate.toLong() * windowMs / 1000L).toInt().coerceAtLeast(1)
    private val coeffs = AuroraAnalyser.probeCoeffs(sampleRate)

    /** One window of normalised samples, refilled IN PLACE. The point of the exercise. */
    private val window = FloatArray(perWindow)
    private var filled = 0

    /** Raw, un-normalised band energies per completed window — [finish] shapes them. */
    private val windows = ArrayList<FloatArray>()

    private var finished = false

    /**
     * Feed one MONO PCM16 sample, in order.
     *
     * Mono because Goertzel probes a SINGLE stream: hand it interleaved stereo and every frequency
     * reads at double, folding the top band into whatever aliases there. Downmix first.
     *
     * No guard against calling this after [finish]: it is invoked once per sample (13M times for a
     * five-minute 44.1 kHz clip) and the check belongs where it costs nothing.
     */
    fun add(sample: Short) {
        window[filled++] = sample / 32768f
        if (filled == perWindow) flush(perWindow)
    }

    /**
     * Close the clip and return its per-window bands (`[window][band]`, each 0..1). Empty when
     * nothing long enough to probe arrived. One-shot.
     */
    fun finish(): Array<FloatArray> {
        check(!finished) { "finish() is one-shot- the normalisation it applies is not idempotent" }
        finished = true
        // A trailing partial window counts only if it is long enough to probe, the same rule the
        // live path applies to a runt read: a 3-sample tail is noise, not a frame.
        if (filled >= AuroraAnalyser.MIN_SAMPLES) flush(filled)
        filled = 0
        AuroraAnalyser.normalizeOfflineBands(windows)
        return windows.toTypedArray()
    }

    private fun flush(len: Int) {
        val row = FloatArray(AuroraAnalyser.BANDS)
        for (b in 0 until AuroraAnalyser.BANDS) {
            val bandCoeffs = coeffs[b]
            if (bandCoeffs.isEmpty()) continue
            var sum = 0f
            for (c in bandCoeffs) sum += goertzel(c, len) { i -> window[i] }
            // GAINS are applied even though the per-band normalisation cancels any constant
            // factor: it keeps these values in the units PEAK_FLOOR was measured in, which is what
            // makes the "a band that never clears its noise floor has no content" test meaningful.
            row[b] = sum / bandCoeffs.size * AuroraAnalyser.GAINS[b]
        }
        windows.add(row)
        filled = 0
    }
}

/**
 * Normalised amplitude (~1 for a full-scale sine on [coeff]'s bin) over [n] samples read
 * through [sample]. `inline` so the live path pays nothing for the accessor lambda that lets
 * the byte-backed and short-backed callers share this one loop.
 */
private inline fun goertzel(coeff: Float, n: Int, sample: (Int) -> Float): Float {
    var s1 = 0f
    var s2 = 0f
    var i = 0
    while (i < n) {
        val s0 = sample(i) + coeff * s1 - s2
        s2 = s1
        s1 = s0
        i++
    }
    val power = (s1 * s1 + s2 * s2 - coeff * s1 * s2).coerceAtLeast(0f)
    // sqrt(power)/n ~= amplitude/2 for an on-bin sine; x4 maps full-scale to ~1 with headroom.
    return sqrt(power / (n.toFloat() * n)) * 4f
}
