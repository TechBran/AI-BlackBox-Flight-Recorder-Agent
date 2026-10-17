package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraBins
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import kotlin.math.ceil

// =============================================================================
// WaveformFeed — the ONE data layer every waveform style draws from.
//
// The input to the whole system stays exactly what Aurora already consumes:
// a `List<AuroraVoice>`, one entry per audible speaker, each carrying the
// v33-fixed [AuroraAnalyser]'s four perceptual band drives. Nothing about that
// contract changes — the analyser's reactivity fixes therefore benefit every
// style automatically, and the three call sites keep handing over the same list
// they hand Aurora today.
//
// What the feed ADDS on top, and the reason it exists, is HISTORY. Aurora is a
// pure function of the CURRENT bands — it has no memory, and does not need one.
// But half the reference-sheet styles are scrolling waveforms (Mirrored Pulse,
// Cinematic, Sharp Line, the Classic scope): a bar that marches leftward across
// the surface is drawn from the PAST N seconds of level, not the present frame.
// So the feed maintains, per speaker, a rolling ring of recent overall-level
// samples — the shared "true past" every history style reads the same way.
//
// The ring is filled on a TIME schedule (~50 Hz), NOT once per drawn frame. A
// per-frame ring would hold 60 samples/s on a 60 Hz phone and 120 on a 120 Hz
// Fold, so the same three seconds of speech would scroll at half the width on
// one device and full on the other. Decimating to a fixed cadence off the
// accumulated `dtSec` makes the ring refresh-rate independent by construction —
// the identical wall-clock of audio fills the ring identically at 60 or 120 Hz,
// which is the same discipline [AuroraAnalyser] applies to its warm-up (D3) and
// AuroraWaveform applies to its phase (C3). A JVM test pins it.
//
// A SECOND channel rides alongside, and rides alongside is the whole design: a
// per-speaker `List<AuroraBins>` of log-frequency spectrum levels, carried by an
// ADDITIONAL step() overload into ADDITIONAL rings. Nothing about the level or
// band channel changes when bins are present (a test pins that both forms of
// step produce identical level/band history), so the styles that are
// spectrum-blind today stay byte-for-byte what they were, and a call site with
// no bins to publish keeps calling the two-argument step. Bins are a separate
// channel rather than wider voices because AuroraVoice is exactly four wide by
// contract — see AuroraBins for why that is not negotiable.
//
// A THIRD thing the feed owns, added after the v35 Fold walk ("constantly too
// much going on", "isn't fluent", "twitching and not really having much
// movement"): THE SMOOTHER, and there is exactly one of it.
//
// Measured through the real tap, a band on room tone moves 0.0257 between
// consecutive 20 ms frames against a median of 0.0578 — a 44% jump every frame —
// and the DIRECTION of that movement reverses on 65–72% of frames. Independent
// coin flips reverse on 66.7%, so what the geometry was being handed was, frame
// for frame, indistinguishable from noise. Eight of the ten shipped styles apply
// no temporal smoothing of their own (only Soft Blob and Particle Wave carry
// envelopes), so every one of them drew that noise directly.
//
// The filter therefore lives HERE, once, rather than in nine renderers: [band],
// [level] and [bin] return an ATTACK/RELEASE-smoothed value, and the history
// rings are filled from the same smoothed stream. Smoothing the rings is not
// incidental — it is the only way the fix reaches the RATIO channels, because
// MirroredPulse.bandShare, Cinematic.brightness and SharpLineMath.brightness read
// [bandHistory], not the instantaneous band (ClassicMath.bandLayerShare and
// RibbonMath.centroid/spread read [band] directly). A steadied outline over four
// still-boiling bands would only have moved the twitch into the colour.
//
// Anything that genuinely wants the MEASUREMENT keeps it: [rawLevel], [rawBand]
// and [rawBin] are the unfiltered numbers, unchanged.
//
// The filter is the codebase's existing idiom, not a new one — [Aurora.approach]'s
// exponential with a fast attack and a slower release, the same shape (and the same
// 0.020 s attack) the Particle Wave and Soft Blob envelopes used before those were
// deleted in favour of this one. The release is measured for THIS layer rather than
// inherited; [RELEASE_TAU_SEC] carries the table. It composes exactly over dt
// (Aurora C3), so 60 Hz and 120 Hz land in the same place
// at the same wall clock, and an ARRIVING speaker SNAPS to its first measurement
// rather than ramping in from nothing (a stream that has just started has nothing
// to filter against). A DEPARTED speaker drops to silence at once for the mirror
// reason, which is also what keeps the history-style idle-park gates draining.
//
// And INTERPOLATION, the other half of "fluent": the renderer draws at 60–120 Hz
// off rings that only advance at [CADENCE_HZ], so a scrolling style can otherwise
// only jump a whole column at a time. [subFrame] exposes how far the current
// publish interval has run (the remainder the feed was already carrying in
// `accumSec`), and [publishedLevel]/[publishedBand]/[publishedBin] expose the
// value pushed at the last interval, so a scrolling style shifts x by
// −subFrame·columnPitch and an instantaneous one can lerp(published, live,
// subFrame). The published sample IS the live smoothed value at the instant it
// was pushed, so that lerp is continuous across a publish rather than jumping.
//
// The feed is deliberately pure Kotlin — no Compose, no android.* — so all of
// its ring/decimation/level/smoothing behaviour is unit-testable on the JVM
// exactly like the aurora math. VoiceVisualizer owns the single instance and
// steps it from the frame loop; the renderers only READ it.
//
// NOT thread-safe: one feed belongs to one VoiceVisualizer, stepped and read on
// that composable's frame thread.
// =============================================================================

/**
 * A fixed-capacity ring of recent level samples for ONE speaker, addressed
 * NEWEST-FIRST — index 0 is the most recently pushed value, which is what a
 * scrolling renderer wants (the newest sample is drawn at the leading edge and
 * older ones trail off behind it).
 *
 * Every read is empty-safe: an index outside `0 until size` returns 0f rather
 * than throwing, so a renderer walking a fixed column count over a ring that
 * has not filled yet paints silence for the not-yet-populated columns instead
 * of taking the frame down. Values are clamped to 0..1 on the way in (NaN → 0),
 * so nothing downstream ever has to re-validate them.
 */
class EnvelopeRing internal constructor(val capacity: Int) {

    private val buf = FloatArray(capacity.coerceAtLeast(1))

    /** Index the NEXT push writes to; one past the newest sample. */
    private var head = 0

    /** How many real samples the ring holds, 0..capacity. */
    var size = 0
        private set

    internal fun push(value: Float) {
        // Sanitise here, once, at the single write site: NaN is checked
        // explicitly because every comparison against it is false, so coerceIn
        // would pass it straight through (JLS 15.20.1) and a single NaN in the
        // ring would poison every renderer that averages a window of it.
        val v = if (value.isNaN()) 0f else value.coerceIn(0f, 1f)
        buf[head] = v
        head = (head + 1) % buf.size
        if (size < buf.size) size++
    }

    internal fun clear() {
        head = 0
        size = 0
        buf.fill(0f)
    }

    /**
     * The sample [fromNewest] steps back from the leading edge — 0 is the newest,
     * `size - 1` the oldest still held. Anything out of range is 0f (see class
     * doc): a not-yet-full ring reads as silence in its empty tail.
     */
    fun newest(fromNewest: Int): Float {
        if (fromNewest < 0 || fromNewest >= size) return 0f
        // head - 1 is the newest; walk backwards, wrapping into positive range.
        val idx = ((head - 1 - fromNewest) % buf.size + buf.size) % buf.size
        return buf[idx]
    }
}

/**
 * The shared feed. Wraps the live `List<AuroraVoice>` and, per speaker, keeps a
 * [EnvelopeRing] of recent overall-level samples decimated to [cadenceHz].
 *
 * @param historySec how far back the rings remember, in seconds. ~3 s is the
 *   reference sheet's scroll depth — long enough to read as a waveform, short
 *   enough that the whole history is on screen at once on a wide surface.
 * @param cadenceHz how many level samples per second the rings hold. 50 Hz is
 *   comfortably above any shipped analyser update rate (~30–50 Hz) yet a small,
 *   fixed ring (~150 samples) regardless of display refresh — the point of
 *   decimating on time rather than per frame.
 */
class WaveformFeed(
    val historySec: Float = HISTORY_SEC,
    val cadenceHz: Float = CADENCE_HZ,
) {

    /** Samples a full history spans. `ceil` so the ring can never be short of the window. */
    val capacity: Int = ceil(historySec * cadenceHz).toInt().coerceAtLeast(1)

    /** Wall-clock seconds between decimated samples — the ring's fixed step. */
    private val intervalSec: Float = 1f / cadenceHz.coerceAtLeast(1f)

    private val speakers = AuroraSpeaker.entries
    private val rings = Array(speakers.size) { EnvelopeRing(capacity) }

    /**
     * Per-speaker, per-BAND history, filled on every [step] whether or not anything reads it. Four
     * rings a speaker is ~2 KB total, and a style that scrolls one band (a spectrogram column, a
     * two-tone scope) must not have to wait for a producer to opt in before it has a past to draw.
     */
    private val bandRings =
        Array(speakers.size) { Array(AuroraAnalyser.BANDS) { EnvelopeRing(capacity) } }

    /**
     * Per-speaker, per-BIN history, built the first time that speaker actually carries a spectrum.
     * LAZY because it is an order of magnitude bigger (24 rings a speaker) and, unlike the bands,
     * costs that for nothing at all unless a producer publishes bins — which today's call sites do
     * not. Once built it stays: reallocating on the frame thread mid-session is worse than holding
     * 28 KB for the life of a visualizer.
     */
    private val binRings = arrayOfNulls<Array<EnvelopeRing>>(speakers.size)

    /** Per-speaker leftover time not yet spent on a decimated push. Carries the remainder. */
    private val accumSec = FloatArray(speakers.size)

    // ------------------------------------------------------------ the smoother
    //
    // Two banks per channel: the MEASUREMENT exactly as the producer published it,
    // and the FILTERED value every style reads. Flat arrays rather than per-speaker
    // objects because this is walked on the frame thread at up to 120 Hz and must
    // allocate nothing. The bin banks are eager (2 × 24 floats a speaker ≈ 200 B in
    // total) unlike the bin RINGS, which stay lazy because they are 150 times
    // bigger — the reason for the lazy rings does not apply at this size.

    private val presentFlags = BooleanArray(speakers.size)
    private val spectrumFlags = BooleanArray(speakers.size)

    private val rawLevels = FloatArray(speakers.size)
    private val rawBands = Array(speakers.size) { FloatArray(AuroraAnalyser.BANDS) }
    private val rawBins = Array(speakers.size) { FloatArray(AuroraAnalyser.BINS) }

    private val smoothLevels = FloatArray(speakers.size)
    private val smoothBands = Array(speakers.size) { FloatArray(AuroraAnalyser.BANDS) }
    private val smoothBins = Array(speakers.size) { FloatArray(AuroraAnalyser.BINS) }

    /**
     * Whether this speaker's voice / spectrum was already live on the previous step. FALSE means the
     * next measurement is the FIRST of a stream and is taken as-is (see the header): there is no
     * previous value to filter against, and a ramp would be a swell the microphone never made.
     */
    private val voiceLive = BooleanArray(speakers.size)
    private val spectrumLive = BooleanArray(speakers.size)

    /** The current live list, kept so [level]/[band]/[present] read the same frame the ring did. */
    private var voices: List<AuroraVoice> = emptyList()

    /** The current spectra, same contract as [voices] — read by [bin] / [hasSpectrum]. */
    private var spectra: List<AuroraBins> = emptyList()

    /** The live voice list handed to the last [step]. Renderers that want the raw bands read here. */
    val voiceList: List<AuroraVoice> get() = voices

    /** The live spectrum list handed to the last [step]. Empty unless a producer publishes bins. */
    val spectrumList: List<AuroraBins> get() = spectra

    /**
     * Advance the feed by [dtSec] of wall clock with the current [voices].
     *
     * Called once per frame by VoiceVisualizer. The live state ([level], [band],
     * [present]) updates every call, and the smoother advances by exactly this
     * frame's dt; the history rings only accept a sample each time [intervalSec] of
     * real time has accumulated, so the rings fill at [cadenceHz] whatever the frame
     * rate and [subFrame] carries the fraction in between.
     */
    fun step(dtSec: Float, voices: List<AuroraVoice>) = step(dtSec, voices, emptyList())

    /**
     * The same step, with the spectrum channel carried ALONGSIDE the voices.
     *
     * An overload rather than a widened signature, and an ADDITION rather than a change: the level
     * and band channels behave identically whichever form is called (pinned), so a call site that
     * has no bins to publish keeps the two-argument form and keeps meaning exactly what it meant.
     *
     * @param spectra one entry per speaker whose spectrum is live this frame. Absent is absent —
     *   the same rule the voice list follows, so a producer that stops publishing bins does not
     *   leave a stale spectrum parked on screen.
     */
    fun step(dtSec: Float, voices: List<AuroraVoice>, spectra: List<AuroraBins>) {
        this.voices = voices
        this.spectra = spectra
        // A dt-of-0 frame (first frame of a composition, a resume, a wake) or a
        // garbage negative/NaN dt advances no history — mirror AuroraWaveform.
        val dt = if (dtSec.isNaN() || dtSec <= 0f) 0f else dtSec
        // ONE pair of blend factors for the whole frame. The factor depends only on
        // dt and the tau, so the ~58 channels a two-speaker frame filters cost two
        // exp() calls between them instead of 58. dt 0 ⇒ factor 0 ⇒ nothing moves,
        // which is the same "a dt-0 frame advances nothing" rule the rings follow.
        val kAttack = Aurora.blendAt(dt, ATTACK_TAU_SEC)
        val kRelease = Aurora.blendAt(dt, RELEASE_TAU_SEC)
        for (i in speakers.indices) {
            val speaker = speakers[i]
            val voice = find(voices, speaker)
            val bins = findBins(spectra, speaker)
            presentFlags[i] = voice != null
            spectrumFlags[i] = bins != null

            // --- the measurement, sanitised once at this single read site -------
            val rawBand = rawBands[i]
            val rawBin = rawBins[i]
            rawLevels[i] = levelOf(voice)
            for (b in 0 until AuroraAnalyser.BANDS) rawBand[b] = voice?.band(b) ?: 0f
            for (k in 0 until AuroraAnalyser.BINS) rawBin[k] = bins?.bin(k) ?: 0f

            // --- the filtered stream every style reads --------------------------
            val smoothBand = smoothBands[i]
            val smoothBin = smoothBins[i]
            if (voice == null) {
                // ABSENT is the end of the stream, not a quiet measurement. Releasing
                // over it would park a departed voice's tail in every history ring and
                // deadlock the idle-park gates that wait for those rings to drain.
                voiceLive[i] = false
                smoothLevels[i] = 0f
                smoothBand.fill(0f)
            } else if (!voiceLive[i]) {
                voiceLive[i] = true
                smoothLevels[i] = rawLevels[i]
                for (b in 0 until AuroraAnalyser.BANDS) smoothBand[b] = rawBand[b]
            } else {
                // The LEVEL is filtered as a level — [levelOf]'s weighted max taken on
                // the measurement and then smoothed — rather than recombined from the
                // smoothed bands. Two spectra at the same overall level must read as
                // the same level while their bands cross over, which max-of-filtered
                // does not give (a releasing band would hold the level up).
                smoothLevels[i] = follow(smoothLevels[i], rawLevels[i], kAttack, kRelease)
                for (b in 0 until AuroraAnalyser.BANDS) {
                    smoothBand[b] = follow(smoothBand[b], rawBand[b], kAttack, kRelease)
                }
            }
            if (bins == null) {
                spectrumLive[i] = false
                smoothBin.fill(0f)
            } else if (!spectrumLive[i]) {
                spectrumLive[i] = true
                for (k in 0 until AuroraAnalyser.BINS) smoothBin[k] = rawBin[k]
            } else {
                for (k in 0 until AuroraAnalyser.BINS) {
                    smoothBin[k] = follow(smoothBin[k], rawBin[k], kAttack, kRelease)
                }
            }

            // Present → make sure this speaker HAS bin rings; absent → keep pushing into the ones
            // it already has, if any, so a departed producer scrolls away instead of freezing.
            val perBin = if (bins != null) binRingsFor(i) else binRings[i]
            accumSec[i] += dt
            // Spend the accumulated time in whole intervals, pushing the SMOOTHED
            // value each time (the ratio channels read these rings — see the header).
            // dt is capped upstream (MAX_FRAME_DT), so this is a handful of iterations
            // at most; the guard is belt-and-braces against a pathological dt so one
            // stalled frame cannot spin the whole ring.
            var guard = 0
            while (accumSec[i] >= intervalSec && guard < capacity) {
                rings[i].push(smoothLevels[i])
                for (b in 0 until AuroraAnalyser.BANDS) bandRings[i][b].push(smoothBand[b])
                if (perBin != null) {
                    for (k in 0 until AuroraAnalyser.BINS) perBin[k].push(smoothBin[k])
                }
                accumSec[i] -= intervalSec
                guard++
            }
        }
    }

    /**
     * One channel's filter step: rise on [kAttack], fall on [kRelease]. Both factors are
     * `1 − e^(−dt/τ)` for their tau ([Aurora.blendAt]), so this is exactly [Aurora.approach] with
     * the exponential evaluated once for the whole frame instead of once per channel.
     */
    private fun follow(current: Float, target: Float, kAttack: Float, kRelease: Float): Float {
        val k = if (target > current) kAttack else kRelease
        return current + (target - current) * k
    }

    /** Drop all history and remembered state — the next [step] starts a fresh ring. */
    fun reset() {
        for (r in rings) r.clear()
        for (bands in bandRings) for (r in bands) r.clear()
        for (bins in binRings) if (bins != null) for (r in bins) r.clear()
        accumSec.fill(0f)
        voices = emptyList()
        spectra = emptyList()
        presentFlags.fill(false)
        spectrumFlags.fill(false)
        rawLevels.fill(0f)
        smoothLevels.fill(0f)
        for (row in rawBands) row.fill(0f)
        for (row in rawBins) row.fill(0f)
        for (row in smoothBands) row.fill(0f)
        for (row in smoothBins) row.fill(0f)
        // Unseen again, so the next arrival SNAPS rather than ramping in from the
        // silence a reset left behind — a reset is a new stream, not a gap in one.
        voiceLive.fill(false)
        spectrumLive.fill(false)
    }

    /**
     * The recent-level history ring for [speaker], newest-first. Never null.
     *
     * Holds the SMOOTHED level, like every other channel: what a scrolling style paints is a
     * picture, and a picture of the raw measurement is a picture of the noise (see the header). A
     * DEPARTED speaker still pushes hard zeroes, so the idle-park gates that watch this ring drain
     * behave exactly as they did before the smoother existed.
     */
    fun history(speaker: AuroraSpeaker): EnvelopeRing = rings[speaker.ordinal]

    /**
     * The recent history of ONE band for [speaker], newest-first, SMOOTHED. Always populated (see
     * [bandRings]); an out-of-range band clamps rather than throws, like every other feed read.
     *
     * This is the channel the RATIO consumers read — MirroredPulse.bandShare, Cinematic.brightness
     * and SharpLineMath.brightness all take their four inputs from here — which is why the rings
     * carry the filtered stream rather than the measurement.
     */
    fun bandHistory(speaker: AuroraSpeaker, band: Int): EnvelopeRing =
        bandRings[speaker.ordinal][band.coerceIn(0, AuroraAnalyser.BANDS - 1)]

    /**
     * The recent history of ONE spectrum bin for [speaker], newest-first, SMOOTHED.
     *
     * Before any spectrum has arrived this is [EMPTY_RING] — a single shared, never-written ring
     * that reads as silence — so a renderer walking 24 bins on a feed nobody publishes bins to
     * paints a flat spectrum instead of branching, and does it without allocating in a draw loop.
     */
    fun binHistory(speaker: AuroraSpeaker, bin: Int): EnvelopeRing =
        binRings[speaker.ordinal]?.get(bin.coerceIn(0, AuroraAnalyser.BINS - 1)) ?: EMPTY_RING

    /**
     * One spectrum bin level for [speaker] (0..1), SMOOTHED (see the header), 0 when absent.
     *
     * An index outside the spectrum reads 0 rather than clamping to the top bin — [AuroraBins.bin]'s
     * rule, kept: repeating the top bin upward would paint sibilance the microphone never heard.
     */
    fun bin(speaker: AuroraSpeaker, index: Int): Float =
        if (index < 0 || index >= AuroraAnalyser.BINS) 0f else smoothBins[speaker.ordinal][index]

    /** The same bin as [bin], UNFILTERED — the measurement, for anything that wants it raw. */
    fun rawBin(speaker: AuroraSpeaker, index: Int): Float =
        if (index < 0 || index >= AuroraAnalyser.BINS) 0f else rawBins[speaker.ordinal][index]

    /** Whether [speaker] published a spectrum this frame — the bin channel's [present]. */
    fun hasSpectrum(speaker: AuroraSpeaker): Boolean = spectrumFlags[speaker.ordinal]

    /**
     * The speaker's overall level (0..1), SMOOTHED, 0 when absent.
     *
     * The combination rule is unchanged and still the one pinned definition ([levelOf]'s weighted
     * max); the filter is applied to that scalar, so two spectra the rule calls equally loud stay
     * equally loud through the filter as well.
     */
    fun level(speaker: AuroraSpeaker): Float = smoothLevels[speaker.ordinal]

    /** The same level as [level], UNFILTERED — [levelOf] on this frame's measurement. */
    fun rawLevel(speaker: AuroraSpeaker): Float = rawLevels[speaker.ordinal]

    /**
     * One band drive for [speaker] (0..1), SMOOTHED, 0 when absent.
     *
     * The index clamps rather than reading 0 outside the range, which is [AuroraVoice.band]'s rule
     * kept intact: a producer with fewer bands than sheets drives the remaining ones from its last.
     */
    fun band(speaker: AuroraSpeaker, index: Int): Float =
        smoothBands[speaker.ordinal][index.coerceIn(0, AuroraAnalyser.BANDS - 1)]

    /** The same band as [band], UNFILTERED — the measurement this frame carried. */
    fun rawBand(speaker: AuroraSpeaker, index: Int): Float =
        rawBands[speaker.ordinal][index.coerceIn(0, AuroraAnalyser.BANDS - 1)]

    /** Whether [speaker] is PRESENT this frame (a voice in the list), regardless of loudness. */
    fun present(speaker: AuroraSpeaker): Boolean = presentFlags[speaker.ordinal]

    // ------------------------------------------------- sub-interval interpolation

    /**
     * How far the CURRENT publish interval has run, 0..1 — 0 immediately after a sample was pushed,
     * approaching 1 just before the next one is.
     *
     * The rings advance at [cadenceHz] while the renderer draws at 60–120 Hz, so without this a
     * scrolling style can only move in whole columns and reads as stepping rather than flowing. A
     * scrolling renderer shifts x by `−subFrame · columnPitch`; an instantaneous one can
     * `lerp(publishedX(speaker), x(speaker), subFrame)`.
     *
     * One value for the whole feed, not one per speaker: every speaker accumulates the same frame
     * dt, so their remainders are equal by construction.
     */
    val subFrame: Float
        get() {
            val f = accumSec[0] / intervalSec
            return if (f.isNaN()) 0f else f.coerceIn(0f, 1f)
        }

    /**
     * The level sample pushed at the LAST publish instant — the anchor [subFrame] interpolates
     * from. Identical to `history(speaker).newest(0)`, named so a style reading one channel does not
     * have to reach for the ring, and 0 before anything has been published.
     */
    fun publishedLevel(speaker: AuroraSpeaker): Float = rings[speaker.ordinal].newest(0)

    /** [publishedLevel]'s band twin — `bandHistory(speaker, index).newest(0)`. */
    fun publishedBand(speaker: AuroraSpeaker, index: Int): Float =
        bandHistory(speaker, index).newest(0)

    /** [publishedLevel]'s bin twin — 0 until a producer has published a spectrum. */
    fun publishedBin(speaker: AuroraSpeaker, index: Int): Float =
        binHistory(speaker, index).newest(0)

    /** Indexed loop, no capturing lambda — this runs inside the per-frame [step]. */
    private fun find(voices: List<AuroraVoice>, speaker: AuroraSpeaker): AuroraVoice? {
        for (i in voices.indices) if (voices[i].speaker == speaker) return voices[i]
        return null
    }

    /** [find]'s twin for the spectrum channel, indexed for the same reason. */
    private fun findBins(spectra: List<AuroraBins>, speaker: AuroraSpeaker): AuroraBins? {
        for (i in spectra.indices) if (spectra[i].speaker == speaker) return spectra[i]
        return null
    }

    private fun binRingsFor(index: Int): Array<EnvelopeRing> {
        val existing = binRings[index]
        if (existing != null) return existing
        val created = Array(AuroraAnalyser.BINS) { EnvelopeRing(capacity) }
        binRings[index] = created
        return created
    }

    companion object {
        /**
         * The ring [binHistory] hands back before any spectrum has been published: capacity 1,
         * never pushed (only a feed can push, and no feed holds this one), so every read is the
         * empty-safe 0f the class doc promises. Shared because it is returned from a per-frame,
         * per-bin call and a fresh instance there would be 24 allocations a frame for nothing.
         */
        private val EMPTY_RING = EnvelopeRing(1)

        /** How far back the history rings remember, seconds. */
        const val HISTORY_SEC = 3.0f

        /** Decimated ring fill rate, Hz — fixed regardless of display refresh. */
        const val CADENCE_HZ = 50f

        /**
         * How fast a channel RISES, as a time constant in seconds.
         *
         * 20 ms is one analyser hop: a transient is 63% drawn on the very next frame that sees it
         * and 90% within 46 ms, so nothing about the attack costs the reactivity the v33 analyser
         * work bought (a JVM test pins the 90%-within-3τ figure precisely so a later "just smooth it
         * a bit more" cannot quietly trade it away). It is deliberately the same number Particle
         * Wave and Soft Blob already use for their own envelopes — one idiom, one value.
         */
        const val ATTACK_TAU_SEC = 0.020f

        /**
         * How slowly a channel FALLS, as a time constant in seconds.
         *
         * The asymmetry is the whole filter: rising fast keeps every transient, falling six times
         * slower is what turns i.i.d. noise into coherent motion, because a value can only reverse
         * downward at a sixth of the speed it rose.
         *
         * 0.120 is CHOSEN AGAINST A MEASURED TRADE, not inherited. Two things pull opposite ways —
         * the twitch on room tone (which a long release kills) and the syllable rhythm of speech
         * (which a long release flattens, and "not much movement" was half the same device
         * report). Measured on the tap's own room tone, and on a 4.5 Hz syllabic envelope carrying
         * that same noise:
         *
         * ```
         *   τ_release   noise reversals   frame-to-frame jump   syllable swing kept   90% fall
         *      0.10       0.494-0.511            ÷4.0                   65%             230 ms
         *      0.12       0.473-0.487            ÷4.5                   60%             276 ms
         *      0.15       0.447-0.466            ÷5.2                   53%             345 ms
         *      0.20       0.425-0.439            ÷6.4                   45%             461 ms
         * ```
         *
         * (raw: 0.665 reversals — coin flips.) 0.200 is where Particle Wave and Soft Blob set their
         * own now-deleted envelopes, and it is deliberately NOT taken here: those sat on top of this
         * layer, and the same 460 ms fall is the number the Particle Wave envelope was deleted for.
         * 0.120 keeps the reversal fraction a quarter below coin flips and the jump 4.5× smaller
         * while giving up only 40% of the syllable swing.
         *
         * The residual cost is a ~24% upward bias on pure noise: an attack/release follower rides
         * the peaks of whatever it is given, so a noise FLOOR is the analyser's to subtract, never
         * this filter's to hide.
         */
        const val RELEASE_TAU_SEC = 0.120f

        /**
         * The feed's own filter step, exposed so a style that wants to advance a value in step with
         * the feed replicates it rather than inventing a second curve: rise on [ATTACK_TAU_SEC],
         * fall on [RELEASE_TAU_SEC], through the shared [Aurora.approach] exponential. dt ≤ 0 or NaN
         * advances nothing.
         */
        fun smoothStep(current: Float, target: Float, dtSec: Float): Float {
            if (dtSec.isNaN() || dtSec <= 0f) return current
            val tau = if (target > current) ATTACK_TAU_SEC else RELEASE_TAU_SEC
            return Aurora.approach(current, target, dtSec, tau)
        }

        /**
         * The per-band weights [levelOf] applies before taking the MAX. This is
         * the ONE documented, test-pinned definition of a speaker's "overall
         * level" that every style shares, so two styles never disagree about how
         * loud a voice is.
         *
         * WEIGHTED MAX, not a sum or an average: what a listener reads as "this
         * speaker is active right now" is dominated by its LOUDEST band at the
         * moment — a bright sibilant "s" or a booming vowel each light the level
         * on their own, and a max captures that where a sum would wash four
         * partially-lit bands into a permanent mid-level mush and an average would
         * bury a single sharp peak.
         *
         * The upper two bands are weighted slightly DOWN (0.85, 0.70) because
         * [AuroraAnalyser.GAINS] already lifted them ~2.6–4x into a comparable
         * range for the per-band ribbon; for a single scalar "loudness" that lift
         * over-counts sibilance, so the weights gently restore the voicing/vowel
         * bands' natural dominance. The lower two stay at 1.0 — they carry the body
         * of speech and should read at face value.
         *
         * Pinned by a test against known band inputs so a later tweak is a
         * deliberate, visible change rather than a silent drift.
         */
        internal val LEVEL_WEIGHTS = floatArrayOf(1.0f, 1.0f, 0.85f, 0.70f)

        /**
         * A voice's overall level (0..1): the weighted MAX of its four band drives
         * (see [LEVEL_WEIGHTS]). Null (an absent speaker) is 0. Every band read
         * goes through [AuroraVoice.band], which is NaN-safe and clamps to 0..1, so
         * the result is always a usable number.
         */
        internal fun levelOf(voice: AuroraVoice?): Float {
            if (voice == null) return 0f
            var m = 0f
            for (j in 0 until AuroraAnalyser.BANDS) {
                val w = LEVEL_WEIGHTS[j.coerceIn(0, LEVEL_WEIGHTS.size - 1)] * voice.band(j)
                if (w > m) m = w
            }
            return m.coerceIn(0f, 1f)
        }
    }
}
