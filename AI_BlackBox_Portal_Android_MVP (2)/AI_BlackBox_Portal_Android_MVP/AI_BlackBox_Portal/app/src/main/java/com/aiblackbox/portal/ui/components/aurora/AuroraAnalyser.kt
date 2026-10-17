package com.aiblackbox.portal.ui.components.aurora

import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.ln
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
 * @param calibration WHICH measured constant set this stream is read with ([AuroraCalibration]).
 *   The default is the shipped one; a capture source whose stream the platform has not processed
 *   invalidates the whole set at once and brings its own.
 */
class AuroraAnalyser(
    val sampleRate: Int,
    private val nowNs: () -> Long = System::nanoTime,
    val calibration: AuroraCalibration = AuroraCalibration.DEFAULT,
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

    /**
     * The SESSION reference, per band: where this speaker, at this distance, in this room, has been
     * sitting for the last several seconds. Seeded from the first chunk of each session (D4), then
     * moved on the slow constants so a sustained change in loudness survives to the ribbon rather
     * than being normalised away inside an utterance. It is the output's denominator.
     */
    // `internal` (was private) for MEASUREMENT ONLY: the range-budget harness has to see the two
    // references separately to say which of them is doing the normalising. Visibility only — no
    // behaviour, no new API for production, and nothing outside this class writes it.
    internal val peaks = FloatArray(BANDS) { calibration.peakInit[it] }

    /**
     * The FAST follower, per band: the local envelope of the band, fast to rise and released over
     * [AuroraCalibration.fastReleaseTauSec]. It is NOT the denominator — see the two-reference note
     * in [analyze] for what it is for.
     */
    internal val fastRefs = FloatArray(BANDS) { calibration.peakInit[it] }

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
        for (b in peaks.indices) {
            peaks[b] = calibration.peakInit[b]
            fastRefs[b] = calibration.peakInit[b]
        }
    }

    /**
     * Analyse one live PCM16-LE mono chunk -> 4 band energies, each roughly 0..1.
     *
     * @param lenBytes valid bytes in [pcm] (readers hand back short reads).
     * @param rawOut optional: filled with the PRE-auto-gain band energies — and pre-EXPANDER too,
     *   i.e. the absolute level of the audio including whatever floor it is sitting on. Tests and
     *   calibration tooling read these; the auto-gain deliberately normalises them away, so
     *   this is the only way to see absolute level, and it would stop being that if the floor were
     *   already taken out of it. Production callers pass nothing.
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
            for (b in peaks.indices) {
                peaks[b] = calibration.peakInit[b]
                fastRefs[b] = calibration.peakInit[b]
            }
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
        // Once acquisition ends the reference reverts to the measured steady-state behaviour.
        val acquiring = elapsedMs < WARMUP_MS
        val fall = if (acquiring) 1f - exp(-dtSec / calibration.acquireTauSec) else 0f

        // Steady-state RISE is time-based now, not a per-chunk blend. The ported original rose at
        // RISE_BLEND=0.6 PER CHUNK, so a sustained loudness change re-normalised in 16-176 ms (a
        // few chunks) and loud vs quiet connected speech rendered identically — measured: a 3.3x
        // louder sustained input settled to 0.90-0.98x of the SAME output (diagnostic PART 2). An
        // AGC reference must be MUCH slower than an utterance so sustained level survives to the
        // ribbon; STEADY_RISE_TAU_SEC (see its WHY for how 6.0 s was chosen against gate A) is that
        // constant.
        val steadyRise = if (acquiring) 0f else 1f - exp(-dtSec / calibration.steadyRiseTauSec)

        // Steady-state RELEASE, time-based for the same reason and in the same shape. It was
        // `ref * STEADY_DECAY` once per chunk with no dtSec, and the consequence was NOT a wrong
        // time constant — it was a wrong OPERATING POINT. The release opposes the rise above, so a
        // constant band energy v parks the reference at s*v / (1 - d + s*d); with the rise s
        // time-based and the release d per-chunk, that balance moved with the CALLER'S BUFFER SIZE.
        // Measured: one unchanging tone rendered 0.96 at 16 ms chunks against 0.54 at 160 ms, a
        // 1.78x range on identical audio. Chunk size is not ours to choose — it is whatever
        // getMinBufferSize gives on the device, or the socket frame the AI playback path yields —
        // so every buffer size was a different calibration and no on-device measurement
        // generalised. Now the only thing a buffer size changes is temporal resolution.
        val steadyDecay = exp(-dtSec / calibration.steadyDecayTauSec)

        // THE SECOND REFERENCE's release. Time-based like everything else here, so a buffer size
        // changes resolution and nothing else. There is no matching ATTACK constant — see below.
        val fastFall = exp(-dtSec / calibration.fastReleaseTauSec)

        for (b in 0 until BANDS) {
            val bandCoeffs = coeffs[b]
            var sum = 0f
            for (c in bandCoeffs) sum += goertzel(c, n) { i -> sampleAt(pcm, i) }
            // Speech energy falls off with frequency — GAINS lift the upper bands into a
            // comparable range before the per-band auto-gain sees them.
            val energy = if (bandCoeffs.isEmpty()) 0f else sum / bandCoeffs.size * calibration.gains[b]
            rawOut?.set(b, energy)

            // THE DOWNWARD EXPANDER: REMOVE the floor, do not merely divide by it.
            //
            // Everything below this line — the follower, the session reference, the ratio — then runs
            // on audio with the room taken out of it, which is why the subtraction is here and not
            // folded into the output. Divide-only was the defect: the denominator's
            // max(ref, NOISE_LIVE) floor stops a quiet band amplifying its hiss to FULL scale, but a
            // band sitting at exactly its own noise constant still emitted 1/HEADROOM = 0.2222,
            // which [Aurora.DISPLAY_GAMMA] draws at roughly HALF the ribbon's height — in an empty
            // room, forever. Measured on the composer path, pink room tone at -45 dBFS emitted
            // 0.103/0.040/0.028/0.012 against full-gain speech's 0.254/0.066/0.026/0.011: the room
            // was level with the voice on two of the four sheets and the ribbon could not tell them
            // apart. That is the "constantly too much going on" report, and no value of HEADROOM
            // could have fixed it, because dividing every frame by a constant cannot change the
            // ratio between two of them.
            //
            // Subtracting instead of dividing costs speech almost nothing, and the reason is the
            // chain below rather than the size of the constant: the references track the SUBTRACTED
            // value, so for any band whose speech energy is well above its floor the numerator and
            // the denominator lose the same offset and the quotient barely moves (measured: >= 96%
            // of its level on every sheet that carries speech above its floor). What the offset does
            // remove is the region where signal and floor are the same size — which is precisely the
            // region that had nothing to say.
            val v = (energy - calibration.noiseSubtract * calibration.noiseLive[b]).coerceAtLeast(0f)

            // TWO REFERENCES, AND WHAT EACH IS FOR. A single reference cannot do this job, and the
            // reason is not tuning — it is that one number is being asked two incompatible
            // questions. "How loud is it RIGHT NOW" has to be answered in a fraction of a second or
            // ordinary speech pins the ribbon to its ceiling. "How loud is this SPEAKER, in this
            // room" has to be answered over many seconds or a sustained shout is normalised away
            // before it can be drawn. A ~1000-config sweep of one reference cleared one or the
            // other and never both.
            //
            // So the fast follower answers the first question and the SESSION reference answers the
            // second BY WATCHING THE FOLLOWER RATHER THAN THE AUDIO. That indirection is the whole
            // mechanism. Fed raw band energy, a slow reference is dragged down four times a second
            // by the troughs between syllables and never climbs to the level of the speech around
            // it — measured, before this: the reference parked 2.6x BELOW the typical voiced frame,
            // so the ratio it divided into ran past the soft knee and the ribbon sat on the ceiling
            // whatever the speaker did. Fed the ENVELOPE, which bridges those troughs, it parks
            // where the speech actually is, and the headroom below the ceiling becomes real.
            //
            // The output still divides RAW v (not the follower) by that reference — the follower is
            // a measuring instrument, not a smoother. Putting it in the numerator would hand the
            // renderer a pre-smoothed envelope and take the syllables back out.
            //
            // The follower RISES INSTANTLY and releases on a time constant — a peak-hold envelope,
            // not a two-sided smoother. An attack constant was tried and measured worse on both
            // counts. It cost rate honesty: at 30 ms it is comparable to the chunk itself, so on a
            // 16 ms buffer the follower climbed only 41% of the way per chunk and under-read the
            // level, while on a 160 ms buffer it was effectively instant anyway — the settled
            // height then spanned 2.3% across the buffer sizes a real device hands us, against 0.8%
            // with it gone (AuroraRateInvarianceTest). And it cost contrast, slightly, for the same
            // reason. Nothing was bought: the impulse it was meant to reject cannot reach here,
            // because v is already an average over a whole analysis window.
            val prevFast = fastRefs[b]
            val fast = if (!seeded) v else maxOf(v, prevFast * fastFall)
            fastRefs[b] = fast

            val ref = peaks[b]
            val next = when {
                // D4: seed from the FIRST chunk instead of converging toward it. The clamp is
                // asymmetric on purpose. Seeding too LOW costs ~4 frames of saturation (the
                // 0.6 rise blend climbs fast, ~130 ms) — invisible. Seeding too HIGH costs
                // SECONDS of dead ribbon, because the release is slow. The measured far-field
                // p90 sits 2-3x BELOW PEAK_INIT, i.e. the common case is quieter than the old
                // cold-start guess, so trusting the observation downward is where the win is;
                // above PEAK_INIT we keep the measured prior and let the blend climb.
                !seeded -> fast.coerceIn(calibration.peakFloor[b], calibration.peakInit[b])

                // Acquisition: rise on the measured 0.6 blend (a plosive still spikes the
                // ribbon once, as it should, but cannot instantly rescale the session), fall
                // on the time-based constant.
                //
                // AND ON RAW v, NOT ON THE FOLLOWER — the one place the two references are not
                // chained. Acquisition is the regime where the reference is deliberately AGILE in
                // both directions (0.6 per chunk up, 0.15 s down) because it is hunting for an
                // operating point rather than holding one, and a 300 ms peak hold in front of a
                // 150 ms release is the follower fighting the acquisition it exists to help.
                // Measured with the follower in this branch: a door slam on the session's very
                // first chunk suppressed the NEXT frame to 6% of its clean level and the ribbon
                // stayed down for most of the warm-up — the exact defect the loud-first-chunk test
                // guards, reintroduced through the second reference. The follower's job is to keep
                // the SETTLED reference off the syllable troughs, and there are no settled troughs
                // to bridge in the first 600 ms.
                acquiring -> ref + (v - ref) * (if (v > ref) calibration.riseBlend else fall)

                else -> {
                    // Steady state: decay toward the floor first (release), then rise up from the
                    // DECAYED value on the TIME-BASED steadyRise instead of the original 0.6
                    // per-chunk blend. That is the whole live-path fix: a sustained louder input
                    // now takes seconds, not a few chunks, to be normalised away, so it stays
                    // visibly louder on the ribbon. Both halves are per-second now, so where the
                    // two settle is a property of the audio and nothing else.
                    val decayed = maxOf(ref * steadyDecay, calibration.peakFloor[b])
                    if (fast > decayed) decayed + (fast - decayed) * steadyRise else decayed
                }
            }
            peaks[b] = next.coerceAtLeast(calibration.peakFloor[b])

            // Auto-gain, and NO perceptual mapping. Normalise against the per-band reference
            // (absolute levels vary wildly between devices/distances; without this the bands sit at
            // 0.1-0.3 and the ribbon looks sleepy), but against max(ref, NOISE_LIVE[b])*HEADROOM
            // rather than ref, then a soft knee:
            //   - NOISE_LIVE floors the denominator so a weak-content band (the HIGH band with no
            //     sibilance above the room noise) cannot amplify what is LEFT of its hiss after the
            //     expander to full scale (C). The floor stays at the un-subtracted NOISE_LIVE on
            //     purpose: shrinking it by the same offset the numerator lost would hand the residue
            //     of the floor a bigger multiplier exactly where the measurement is least certain.
            //   - HEADROOM drops typical speech off the ceiling so the ribbon has room to swell on
            //     loud moments instead of pinning flat at 1.0.
            //   - softKnee keeps 0.8x and 1.0x word peaks distinct instead of both hard-clamping to
            //     1.000 — the measured "top saturates flat" defect (gate B).
            // What is NOT here is the perceptual curve. This output is LINEAR normalised energy;
            // the one curve in the pipeline is [Aurora.DISPLAY_GAMMA], applied by the renderer. An
            // analyser-side GAMMA=0.82 used to be applied here and the renderer's square root on
            // top of it, so the live chain compressed at 0.41 — the arithmetic behind a ribbon that
            // barely moved. Do not put a power law back here (WaveformPerceptualCurveTest measures
            // this path end-to-end and fails if anything does).
            val denom = maxOf(peaks[b], calibration.noiseLive[b]) * calibration.headroom
            out[b] = (softKnee(v / denom, calibration.knee) * warmScale).coerceIn(0f, 1f)
        }
        seeded = true
        return out
    }

    companion object {
        const val BANDS = 4

        /**
         * Width of the SEPARATE log-frequency spectrum channel ([AuroraSpectrum]), which is a
         * different measurement of the same audio rather than a finer [BANDS]. The two are
         * deliberately independent: [BANDS] is four Goertzel probes with a measured calibration
         * behind every constant in this file and an AuroraVoice that is exactly four wide, so it
         * cannot be widened without invalidating both. Anything that wants spectral detail rides
         * this channel instead.
         */
        const val BINS = 24

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

        /**
         * THE DYNAMICS CONSTANTS BELOW ARE ONE CALIBRATION, NOT TWELVE INDEPENDENT KNOBS. They were
         * measured together against a platform-PROCESSED capture, and the live path reads them
         * through [AuroraCalibration.DEFAULT] rather than directly, so a source that hands the
         * analyser a differently-shaped stream swaps the SET (see [AuroraCalibration]). Editing a
         * value here re-tunes every stream that has not been given its own profile.
         *
         * The offline path ([analyzeOffline]) deliberately stays on the constants: it peak-
         * normalises a whole known clip, so it has no auto-gain, no warm-up and no source.
         */

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

        /**
         * The measured steady-state release, in its original per-chunk form: the reference keeps
         * 99.5% of its height every [STEADY_DECAY_REF_SEC]. Kept in this form, rather than folded
         * into the time constant below, because it is the MEASUREMENT — the number that was tuned
         * on-device — and [STEADY_DECAY_TAU_SEC] is a derivation from it.
         */
        internal const val STEADY_DECAY = 0.995f

        /**
         * The chunk size [STEADY_DECAY] describes one chunk of. The analyser applied that constant
         * once per chunk at whatever size the caller used, so the constant only ever meant anything
         * at ONE size, and this names which one: 32 ms, the mic cadence this analyser was ported
         * and documented against (see D3 and the chunk-duration note in analyze).
         *
         * It is a calibration anchor, not a limit — no caller has to feed 32 ms chunks, and none
         * does. Its ONLY job is to make the conversion to a time constant carry the measurement
         * across unchanged: at 32 ms the release is bit-for-bit what it always was, so this change
         * is about rate honesty and is not a re-tune smuggled in beside one. Moving this number
         * WOULD be a re-tune — it moves the operating point at every chunk size at once (anchoring
         * at 16 ms instead would settle the same steady tone at 0.96 rather than 0.80) — so it is a
         * calibration decision to be made against measurements, not a constant to adjust to taste.
         */
        internal const val STEADY_DECAY_REF_SEC = 0.032f

        /**
         * [STEADY_DECAY] as a time constant: the release is `exp(-dtSec / TAU)`, mirroring the rise
         * beside it. Derived rather than written out (it is 6.38 s) so the identity
         * `exp(-REF/TAU) == STEADY_DECAY` holds by construction and cannot drift if either input
         * is re-measured.
         */
        internal val STEADY_DECAY_TAU_SEC: Float = -STEADY_DECAY_REF_SEC / ln(STEADY_DECAY)

        /**
         * Steady-state RISE time constant (seconds). Replaces the original's RISE_BLEND=0.6
         * per-chunk rise in steady state (acquisition still uses RISE_BLEND). The reference must
         * be MUCH slower than an utterance so a sustained loudness change survives to the ribbon
         * instead of being re-normalised away in a few chunks (the ported original re-normalised a
         * 3.3x louder sustained input to 0.90-0.98x of the SAME output within 16-176 ms).
         *
         * 6.0 s is what it takes to clear gate A on the STRONGEST band. Gate A measures the
         * settled/baseline output ratio 1.85 s after a 0.3x->1.0x continuous step; to read >=1.5x
         * there the reference must still be far from re-adapted at 1.85 s. The low band (0) has the
         * hottest, most sustained energy so its reference climbs fastest in absolute terms —
         * measured band-0 settled ratios: 0.8 s rise -> ~0.98x, 3.0 s -> 1.32x, 4.5 s -> 1.52x,
         * 6.0 s -> 1.60x. Bands 1 and 3 clear 1.5x far sooner (1.95x / 2.37x at 6.0 s). This is
         * deliberately slower than a typical broadcast-AGC attack: the flight-recorder ribbon wants
         * a stable operating point that only re-levels on genuinely sustained changes, not a fast
         * peak-follower. NOTE the unavoidable cost, proven by sweep (AuroraSweep, uncommitted): a
         * reference this slow sits below the strong bands' word peaks, so bands 0/1 word peaks still
         * ride the soft-knee ceiling (out(1.0)/out(0.8) ~ 1.0). A fast reference distinguishes them
         * but then fails gate A — the two are mutually exclusive on a single reference. Bands 2/3,
         * whose energy is weaker, get both (gate B out(1.0)/out(0.8) = 1.20).
         */
        internal const val STEADY_RISE_TAU_SEC = 6.0f

        /**
         * THE FAST FOLLOWER's release — the second reference, the one that measures "how loud is this
         * band right now" so the session reference above can be asked the slower question instead of
         * both at once. See the two-reference note in [analyze] for the mechanism.
         *
         * There is only one constant because the follower is a PEAK HOLD: it takes any rise
         * immediately and only the fall is timed. See the attack note at the code for the
         * measurements that removed the attack constant that was here first.
         *
         * RELEASE is 300 ms, and it is pinned from BOTH sides by measurement.
         *
         * From below by the SYLLABLE: connected speech runs ~4.3 syllables/sec, so the follower has
         * to bridge a ~230 ms trough rather than fall into it, and 300 ms still holds 46% across
         * one. It is also far short of the seconds a sustained passage lasts, so a genuinely quieter
         * passage IS followed down within it — that difference is what separates bridging syllables
         * from smoothing away a whisper.
         *
         * From ABOVE by the transient, which is the constraint that was not obvious: the follower
         * feeds the session reference, so anything it holds, the reference eventually chases. A door
         * slam on a session's opening chunk is ~30x ordinary speech, and at 500 ms of release the
         * follower was still handing the reference 3x the real level a full second later — the
         * ribbon measurably stayed down (the loud-first-chunk recovery went 0.973 at 150 ms, 0.945
         * at 250, 0.915 at 300, 0.884 at 350, against a floor of 0.90). That is the same
         * dead-ribbon-after-a-plosive defect D4 exists to prevent, so the briefed 0.3-0.5 s range is
         * really 0.3 at its top, not its bottom.
         *
         * It is far faster than [STEADY_RISE_TAU_SEC]; [AuroraCalibration] rejects a profile where
         * that is not true, because two references at the same speed are one reference.
         */
        internal const val FAST_RELEASE_TAU_SEC = 0.300f

        /**
         * Auto-gain headroom: output is normalised against max(ref, NOISE_LIVE)*HEADROOM, not ref.
         * Headroom drops typical speech off the ceiling so the ribbon has room to swell on loud
         * moments instead of pinning flat at 1.0.
         *
         * RE-DERIVED, 4.5, was 2.5. The old value was set under DOUBLE compression — an analyser
         * GAMMA of 0.82 with the renderer's square root on top of it — and against a single
         * reference that parked 2.6x below typical speech, so it was not a number that could be
         * carried across; both of the things it was balancing are gone. Measured again from
         * scratch against the M0 harness, on the dominant band at the 40 ms mic read that was the
         * analysis window before the AudioAnalysisTap, against everything the ribbon is required
         * to do at once:
         *
         *   H     typical  swing    sustained  whisper     whisper   silence
         *         (p50)    p90/p50  contrast   separation  visible   p90
         *   4.0   0.443    2.115    2.32       3.89        0.318     0.014
         *   4.5   0.394    2.254    2.44       3.89        0.300     0.013   <- here
         *   5.0   0.354    2.320    2.46       3.89        0.284     0.011
         *   5.5   0.322    2.323    2.46       3.89        0.271     0.010
         *
         * Every column but one improves with headroom, and the one that does not is the whole
         * tension: a whisper drawn against a ceiling that keeps rising eventually stops being
         * visible. 4.5 is where swing has reached 97% of everything it will ever reach (it flattens
         * at 2.32, and cannot go past ~3.3 in any case while a typical frame is also required to sit
         * above 0.30, since a p90 cannot exceed 1.0), while typical speech sits in the middle of the
         * target band rather than against either end and a whisper still draws at 0.30 of full
         * height — five times the ribbon's resting sliver.
         *
         * NOT RE-DERIVED AT THE WINDOW THAT SHIPS. The tap analyses every stream at its 20 ms hop,
         * where this value puts the typical column at 0.276 (0.261 through the unprocessed profile),
         * under the 0.30 floor. WaveformDynamicsGatesTest holds that as a pending operator decision
         * rather than a retune here: the look is tuned on the device.
         *
         * The gates these columns are scored against live in WaveformDynamicsGatesTest, which is
         * the file to read before moving this number — it says what each column has to clear and
         * why, and it fails rather than drifts if this is retuned by feel.
         */
        internal const val HEADROOM = 4.5f

        /**
         * Soft-knee ceiling. The gamma'd ratio is passed through a knee that is identity below
         * KNEE and compresses everything above it asymptotically toward 1 (never past). Without it
         * a 0.8x word and a 1.0x word both drove the ratio past 1.0 and HARD-clamped to exactly
         * 1.000 — the measured "top of the range saturates flat" defect (gate B, out(1.0)/out(0.8)
         * was 1.00). Where the words sit near the knee (bands 2/3) it keeps them monotonically
         * distinct (out(1.0)/out(0.8) = 1.20). 0.80 places the knee just above where typical speech
         * sits under HEADROOM. The soft-knee bounds the RATIO before warmScale, so warmScale is a
         * true output attenuator during the opening ramp (the ceiling no longer lets a loud first
         * chunk punch through the warm-up).
         */
        internal const val KNEE = 0.80f

        /**
         * LIVE output noise-floor, per band: the output denominator is max(ref, NOISE_LIVE[b]).
         * A band with real content keeps ref >> NOISE_LIVE (untouched); a weak-content band whose
         * auto-gain reference has decayed low no longer amplifies its noise floor to full scale.
         * The HIGH band is the one that needs it — with no sibilance above the room noise its ref
         * sits near PEAK_FLOOR and -50 dBFS hiss rendered p90 0.117 between phrases (gate C wants
         * <=0.08). Sized ~2-3x the measured live noise band energy; the lower bands sit well below
         * their real speech level so their auto-gain is unaffected in normal use.
         */
        internal val NOISE_LIVE = floatArrayOf(0.006f, 0.010f, 0.014f, 0.030f)

        /**
         * THE EXPANDER's depth: how many [NOISE_LIVE]s come OFF each band before it is normalised.
         * `v' = max(0, v - NOISE_SUBTRACT * NOISE_LIVE[b])`, per band, per frame.
         *
         * IT IS ZERO ON THIS PROFILE, AND THAT IS A MEASUREMENT RATHER THAN AN OMISSION. What
         * [NOISE_LIVE] holds here is not a floor — it is a DENOMINATOR GUARD deliberately sized
         * "~2-3x the measured live noise band energy" (see its own note) against a stream the
         * platform had ALREADY AGC-flattened. Subtracting a guard is subtracting the wrong quantity,
         * and on this profile there is provably no room to do it at all: the quietest thing the
         * ribbon is REQUIRED to keep drawing — the shipped whisper passage — carries less band
         * energy than the guard does (measured raw band-0 p50 0.0036 against NOISE_LIVE[0] 0.006),
         * so any depth is taking a bite out of the whisper before it touches the floor.
         *
         * MEASURED against the shipped gates (WaveformDynamicsGatesTest, whose numbers are the
         * specification and may not be loosened), sweeping this constant and nothing else. Levels
         * are LINEAR band output, so they do not rot when the renderer's curve moves; the whisper
         * gate is stated in DRAWN height (>= 0.20), which at [Aurora.DISPLAY_GAMMA] as it stands
         * means a linear whisper of at least 0.084:
         *
         *   depth  whisper (needs >=0.084)  silence p90     sustained  whisper sep  typical p50
         *                                   (needs <=0.08)  (>=1.5)    (>=2.0)      (0.30..0.55)
         *   0.00   0.0899                   0.0113          2.44       3.89         0.3934  <- here
         *   0.10   0.0677  <- gate FAILS    0.0000          2.53       4.96         0.3929
         *   0.15   0.0565                   0.0000          2.60       5.77         0.3915
         *   0.25   0.0343                   0.0000          2.78       8.86         0.3882
         *
         * The whisper gate has ~7% of headroom at depth zero, so this column is the whole decision:
         * there is no setting that buys the flat silence in column two without spending a gate that
         * says a quiet talker must stay on screen. A source whose floor is measured AS a floor has
         * no such problem and subtracts all of it — see [AuroraCalibration.UNPROCESSED], which is
         * the profile the composer and dictation mics actually run on when the device really
         * implements unprocessed capture, and which is where the report this stage exists to answer
         * came from.
         *
         * WHAT WOULD MOVE THIS OFF ZERO: a re-measured floor for the processed sources — the real
         * residual of a VOICE_COMMUNICATION stream, measured as a floor rather than inferred as a
         * guard — at which point the depth beside it becomes 1.0 and means the same thing it means
         * everywhere else. That is a measurement session with a device, not a constant to nudge.
         */
        internal const val NOISE_SUBTRACT = 0f

        /** Output scale at the instant a session starts; ramps to 1 over [WARMUP_MS]. */
        internal const val WARM_FLOOR = 0.35f

        /**
         * D3: ~600 ms, chosen so the crescendo reads as intent rather than lag. The original's
         * 60-chunk equivalent was 1.9 s on a 32 ms feed and 6.0 s on a 100 ms one.
         */
        internal const val WARMUP_MS = 600f

        /**
         * A hole this long in the feed means a new session (kept from the original). `internal`
         * because [AudioAnalysisTap] drives this analyser off the AUDIO's own clock, where a
         * DELIVERY stall leaves no trace at all, and has to watch the wall clock for exactly this
         * threshold to preserve the behaviour — two copies of it would drift.
         */
        internal const val SESSION_GAP_NS = 1_000_000_000L

        /**
         * Acquisition release time constant. Over one [WARMUP_MS] window the reference sheds
         * e^-4 (~98%) of any excess regardless of chunk size — the whole point of expressing
         * it in seconds rather than frames.
         */
        internal const val ACQUIRE_TAU_SEC = 0.15f

        /** Clamp for a stalled reader so one late chunk cannot dump the whole acquisition. */
        private const val MAX_DT_SEC = 0.5f

        /**
         * Below this a chunk cannot resolve the low band at all — report silence. `internal`
         * because the offline path applies the SAME rule to its trailing partial window, and two
         * copies of this number would drift.
         */
        internal const val MIN_SAMPLES = 64

        /**
         * A probe must sit clear of Nyquist or it measures aliases, not content. `internal` because
         * the spectrum channel applies the SAME rule to its FFT bins (and derives the top of its
         * frequency map from it) — two copies of this number would drift.
         */
        internal const val NYQUIST_MARGIN = 0.45f

        /** Mirrors AudioEnvelope.decode's window so the two envelopes line up 1:1. */
        internal const val OFFLINE_WINDOW_MS = 20

        /**
         * Silence shaping: a noise GATE that keeps true silence/hum FLAT, and NOTHING ELSE.
         *
         * GATE was 0.05 (matching AudioEnvelope.decode). Raised to 0.13 for the silence-flattening
         * the offline noise floor needs (gate D): peak-normalising a band pins its loudest window
         * to 1.0 regardless of absolute level, so a clip whose only real content is room tone would
         * otherwise render its hiss at full height.
         *
         * The LIFT = 0.55 power law that used to follow it is DELETED. It was this path's private
         * copy of the perceptual curve, and with the renderer applying its own square root on top
         * the player bar compressed at 0.275 — HARDER than the live path, on audio that had already
         * been peak-normalised. The one curve is [Aurora.DISPLAY_GAMMA] and the renderer applies it;
         * this pass hands over LINEAR gated energy. What survives here is a THRESHOLD, not a curve:
         * keeping silence flat is a different job from bending level, and it is a job only this path
         * (which peak-normalises a whole known clip, so its floor is relative to that clip) has.
         */
        private const val GATE = 0.13f

        /**
         * Offline noise-reference floor, one per band, between PEAK_FLOOR and PEAK_INIT (above the
         * measured per-band noise energy, below quiet-but-real speech). normalizeOfflineBands
         * divides each band by max(bandPeak, NOISE_REF[b]) instead of bandPeak alone: a real-content
         * band (bandPeak >> NOISE_REF) is untouched, but a band whose loudest moment is only its own
         * noise floor (bandPeak <= NOISE_REF) is divided by the larger NOISE_REF and the noise falls
         * under the gate — it renders ~0 instead of being amplified to full scale. Measured: -50 dBFS
         * noise in the empty HIGH band rendered mean 0.443 / p90 0.60 before (peak-norm blew the hiss
         * up); with this it rests ~0 (gate D). PEAK_FLOOR alone could not do this — the noise peak
         * (~0.003) clears PEAK_FLOOR[3]=0.002, so the old `peak < FLOOR -> zero` guard never fired.
         *
         * Sized per band, NOT at the PEAK_INIT scale: the HIGH band's noise (~0.003) sits close to
         * its real-content floor so it needs ~0.02 (=PEAK_INIT[3]) to be gated, but the lower bands
         * carry real speech an order of magnitude above their noise, so their refs stay near
         * PEAK_FLOOR — a PEAK_INIT-scale ref there (0.05-0.12) would clamp genuinely quiet speech
         * (e.g. a 0.03 amplitude tone lands at bandPeak ~0.04, which must NOT be treated as noise).
         */
        internal val NOISE_REF = floatArrayOf(0.006f, 0.012f, 0.018f, 0.020f)

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
         * whole clip, then the same GATE the RMS envelope gets (a threshold; NOT a curve — the one
         * perceptual curve is the renderer's, see [Aurora.DISPLAY_GAMMA]).
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
                // Normalise against max(bandPeak, NOISE_REF[b]): a real-content band is divided by
                // its own peak as before, but a band whose peak is only its noise floor is divided
                // by the larger NOISE_REF so the hiss falls under the gate rather than filling the
                // ribbon. This is what stops the empty HIGH band from sparkling during silence.
                val norm = 1f / maxOf(peak, NOISE_REF[b])
                for (w in windows.indices) {
                    val v = (windows[w][b] * norm).coerceIn(0f, 1f)
                    // Gate, then RE-SPAN the survivors back over 0..1 so the loudest window still
                    // reaches full height. Linear on purpose — see GATE.
                    windows[w][b] = if (v <= GATE) 0f else (v - GATE) / (1f - GATE)
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

        /**
         * Soft-knee ceiling: identity below the knee, then compresses toward 1 asymptotically so
         * two values that would both exceed 1.0 (a 0.8x and a 1.0x word) stay monotonically
         * distinct instead of both hard-clamping to 1.000. Continuous and monotone; never
         * returns > 1.
         *
         * The live path passes its profile's knee ([AuroraCalibration.knee]); the default is
         * [KNEE], the shipped measurement.
         */
        internal fun softKnee(x: Float, knee: Float = KNEE): Float =
            if (x <= knee) x
            else (knee + (1f - knee) * (1f - exp(-(x - knee) / (1f - knee))))

        private fun goertzelCoeff(freq: Float, sampleRate: Int): Float =
            (2.0 * cos(2.0 * Math.PI * freq / sampleRate)).toFloat()

        /**
         * One PCM16-LE sample of [pcm] as -1..1. Sign-extends the high byte, masks the low.
         *
         * `internal` because the FFT channel needs the SAME decode to hand [AuroraSpectrum] floats
         * from the same bytes ([AudioAnalysisTap] is the adapter). A second copy of three lines of
         * bit-twiddling is exactly the kind of duplicate that drifts by a sign or a mask and then
         * makes two readings of one stream disagree for a reason nobody can find.
         */
        internal fun sampleAt(pcm: ByteArray, i: Int): Float {
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
