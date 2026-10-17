package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraBins
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraSpectrum
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.ln

// =============================================================================
// WaveformDemoDriver — the deterministic synthetic conversation the Settings
// preview animates, and the ONLY input a style renders in preview / screenshot
// tests. It exists because a live preview needs a voice to draw, and a settings
// screen has no microphone and no model audio — so it FAKES a believable two-way
// exchange: the human speaks, they briefly overlap, the AI answers, a pause, and
// it repeats. Every style therefore gets exercised in BOTH the one-voice case and
// the two-voice (overlap) case with zero audio hardware.
//
// It is PURE functions of time — `voicesAt(timeSec)` and `binsAt(timeSec)` — with
// an INJECTED clock (the time argument): the composable preview feeds it
// withFrameNanos wall clock; a JVM test feeds it fixed sample points. Determinism
// is by construction (no internal RNG state, no wall-clock reads) so `(seed, t)`
// returns byte-identical output every call — which is exactly what lets the preview
// be screenshot-compared frame-for-frame during on-device iteration, and what a
// unit test pins. No Compose, no android.* — all of the shaping math is
// JVM-testable like the aurora math it feeds.
//
// TWO CHANNELS, because a live surface has two. The four analyser BANDS are what a
// style has always been driven by here; the 24-bin log SPECTRUM is what six of the
// ten styles read on a real microphone, and a preview lacking it would silently run
// their four-band fallback and advertise a look the composer does not draw. See
// [binsAt] for what makes the synthetic spectrum a spectrum rather than four bands
// widened, and for why its range is taken from the live tap's own ceiling.
//
// SHAPE OF THE SIGNAL. Each voice's turn is a train of syllabic bursts, not a
// steady tone: real speech is ~3–5 syllables/second, each a raised-cosine swell
// with a gap after it, which is what makes a waveform read as "talking" rather
// than "a sine generator". The four analyser bands are driven with slightly
// different per-band phase offsets and per-speaker profiles so the bands move
// independently (the human reads brighter/jitterier, the AI warmer/calmer),
// exactly the band-varied energy [AuroraAnalyser] produces from real audio. A
// per-turn attack/release envelope ramps the turn in and out so a style's
// present→absent transition (aurora's smooth retire) is exercised too.
//
// The turn structure is one repeating period (see [Timeline]); looping the phase
// keeps the demo predictable and the screenshot comparison stable. Determinism
// holds either way — `voicesAt` reads nothing but its arguments.
// =============================================================================

/**
 * The turn layout of one demo period, in seconds. Windows may OVERLAP — that
 * overlap IS the two-voice moment every style must render, so it is a first-class
 * part of the timeline, not an accident. A speaker is PRESENT (a voice in the
 * list) exactly while the phase is inside its `[start, end)` window; outside every
 * window the list is empty (the pause), which drives each style's absent/retire
 * path.
 *
 * @property humanStart / [humanEnd] the human's speaking window.
 * @property aiStart / [aiEnd] the AI's window; it opens BEFORE the human's closes,
 *   so `[aiStart, humanEnd)` is the overlap both voices are drawn in.
 * @property periodSec the full loop length; `[max(humanEnd, aiEnd), periodSec)` is
 *   the trailing pause where neither speaks.
 */
class Timeline(
    val humanStart: Double = 0.0,
    val humanEnd: Double = 2.5,
    val aiStart: Double = 2.0,
    val aiEnd: Double = 4.5,
    val periodSec: Double = 5.2,
) {
    init {
        // A malformed timeline would make voicesAt silently draw nothing; fail loud
        // at construction instead. (Only the DEFAULT ships, but a future tweak pass
        // edits these numbers and this catches an inverted or over-long window.)
        require(humanStart < humanEnd) { "human window must be non-empty" }
        require(aiStart < aiEnd) { "ai window must be non-empty" }
        require(aiStart < humanEnd) { "the two windows must overlap (the two-voice moment)" }
        require(periodSec >= humanEnd && periodSec >= aiEnd) { "period must contain both windows" }
    }

    companion object {
        /** The shipped layout: human 2.5 s, 0.5 s overlap, AI 2.5 s, ~0.7 s pause. */
        val DEFAULT = Timeline()
    }
}

/**
 * The synthetic-voice generator. Construct once (a fixed [seed] pins the per-syllable
 * loudness pattern) and call [voicesAt] every frame with the current wall clock.
 *
 * @param seed pins the deterministic per-syllable amplitude jitter. The same seed
 *   and the same time always yield the same bands; a different seed gives a
 *   different (but equally believable) cadence.
 * @param timeline the turn layout (see [Timeline]); the default is the shipped one.
 */
class WaveformDemoDriver(
    val seed: Long = DEFAULT_SEED,
    val timeline: Timeline = Timeline.DEFAULT,
) {

    /**
     * The voices present at [timeSec], HUMAN before AI (the order [auroraVoices]
     * uses, so a container's list is stable). Empty during the pause. Each present
     * voice carries four NaN-free, 0..1 band drives shaped for [timeSec]. A pure
     * function of `(seed, timeline, timeSec)` — no state, no clock read.
     */
    fun voicesAt(timeSec: Double): List<AuroraVoice> {
        // Loop the phase so the demo repeats cleanly; a garbage/NaN time parks at 0.
        val t = if (timeSec.isNaN()) 0.0 else timeSec
        val phase = floorMod(t, timeline.periodSec)

        val voices = ArrayList<AuroraVoice>(2)
        bandsFor(AuroraSpeaker.HUMAN, phase, timeline.humanStart, timeline.humanEnd)
            ?.let { voices.add(AuroraVoice(AuroraSpeaker.HUMAN, it)) }
        bandsFor(AuroraSpeaker.AI, phase, timeline.aiStart, timeline.aiEnd)
            ?.let { voices.add(AuroraVoice(AuroraSpeaker.AI, it)) }
        return voices
    }

    /**
     * The SPECTRA present at [timeSec] — [voicesAt]'s second channel, for exactly the same speakers
     * in the same order, and absent for the same instants.
     *
     * WHY THE PREVIEW SYNTHESISES ONE. [WaveformStylePreview] is the real [VoiceVisualizer], and its
     * whole claim is that what an operator previews is what they will see in the composer. Six of the
     * ten styles now read the spectrum on a live mic; a preview with no spectrum would run their
     * four-band FALLBACK and advertise a look no device produces — Segmented EQ as four plateaus
     * instead of twenty-four columns. A settings screen has no microphone, so the honest way to keep
     * the promise is to synthesise the second channel exactly as the first one is synthesised.
     *
     * WHAT MAKES IT A SPECTRUM RATHER THAN THE BANDS WIDENED. Three things, in the order they matter:
     * the coarse shape is the four band drives of the SAME instant interpolated across log frequency
     * ([bandEnvelopeAt] — two readings of one voice, as they are of a real one, and the frequencies
     * it interpolates between are derived from [AuroraAnalyser.PROBES] so the two channels cannot
     * drift apart); a moving FORMANT peak slides through the bins across each syllable, which is the
     * vowel colour a four-band channel cannot carry; and a per-bin, per-syllable TEXTURE makes
     * neighbouring bins differ, so a bar chart of it looks like a spectrum and not like a staircase.
     *
     * THE RANGE IS THE REAL CHANNEL'S. The loudest bin of a frame is that frame's loudest BAND drive
     * times [AudioAnalysisTap.BIN_HEADROOM]'s reciprocal — the exact ceiling the live tap publishes,
     * approached on the demo's loudest syllables and never passed — so a column that fills its stack
     * in the preview fills it on a device, and one that does not, does not.
     *
     * Pure, deterministic, allocation-per-call like [voicesAt] (this is a settings preview at 60-120
     * Hz, not an audio path). Same `(seed, timeline, timeSec)` in, same bins out, forever.
     */
    fun binsAt(timeSec: Double): List<AuroraBins> {
        val t = if (timeSec.isNaN()) 0.0 else timeSec
        val phase = floorMod(t, timeline.periodSec)

        val spectra = ArrayList<AuroraBins>(2)
        binsFor(AuroraSpeaker.HUMAN, phase, timeline.humanStart, timeline.humanEnd)
            ?.let { spectra.add(AuroraBins(AuroraSpeaker.HUMAN, it)) }
        binsFor(AuroraSpeaker.AI, phase, timeline.aiStart, timeline.aiEnd)
            ?.let { spectra.add(AuroraBins(AuroraSpeaker.AI, it)) }
        return spectra
    }

    /**
     * [AuroraAnalyser.BINS] log-frequency levels for [speaker] at [phase], or null outside its
     * window — the same absence rule [bandsFor] follows, so the two channels can never disagree
     * about who is speaking.
     */
    private fun binsFor(
        speaker: AuroraSpeaker,
        phase: Double,
        start: Double,
        end: Double,
    ): FloatArray? {
        val bands = bandsFor(speaker, phase, start, end) ?: return null

        // Which syllable this is, so the texture and the formant move at syllable rate rather than
        // per frame. Re-derived here rather than returned from bandsFor, which stays a pure
        // four-float producer; the arithmetic is two multiplies.
        val u = (phase - start).toFloat()
        val rate = if (speaker == AuroraSpeaker.HUMAN) HUMAN_SYLLABLE_HZ else AI_SYLLABLE_HZ
        val pos = u * rate
        val syllable = floor(pos).toInt()
        val frac = pos - syllable

        // The formant sweeps across the syllable, low to high and back — the mouth opening and
        // closing. A different span per speaker so the two voices read as two different mouths.
        val span = if (speaker == AuroraSpeaker.HUMAN) HUMAN_FORMANT_BINS else AI_FORMANT_BINS
        val sweep = 0.5f - 0.5f * cos(2f * PI.toFloat() * frac)
        val formantBin = lerp(span[0], span[1], sweep)

        val bins = FloatArray(AuroraAnalyser.BINS)
        var peakShape = 0f
        for (k in bins.indices) {
            // Coarse shape: the band drives, interpolated across log frequency.
            val envelope = bandEnvelopeAt(k, bands)
            // Texture: fixed for the whole syllable, so this is vowel colour and not per-frame
            // noise — the thing the v35 Fold walk called "twitching" is exactly what a per-frame
            // random here would manufacture.
            val texture = lerp(BIN_TEXTURE_MIN, 1f, rand01(seed, speaker.ordinal * 64 + k, syllable))
            val d = (k - formantBin) / FORMANT_WIDTH_BINS
            val formant = 1f + FORMANT_GAIN * exp(-0.5f * d * d)
            val shape = envelope * texture * formant
            bins[k] = shape
            if (shape > peakShape) peakShape = shape
        }

        // SHAPE and LEVEL are combined the way the live channel combines them: ONE divisor for the
        // whole frame, so the spectrum's shape survives exactly, and the frame's own loudness
        // (its loudest band) then decides how tall the whole thing is. A frame as loud as the
        // driver ever gets therefore peaks at the tap's own ceiling and a quiet syllable draws
        // proportionally shorter — the preview keeps the dynamics instead of peak-normalising every
        // frame to full height, which would show a whisper and a shout as the same picture.
        var loudestBand = 0f
        for (b in bands) if (b > loudestBand) loudestBand = b
        if (peakShape <= 0f || loudestBand <= 0f) {
            // A syllable gap: present (the speaker is mid-turn) but carrying nothing. Zeros, not
            // absence — absence is leaving the window, which bandsFor already returned null for.
            bins.fill(0f)
            return bins
        }
        val scale = loudestBand * BIN_CEILING / peakShape
        for (k in bins.indices) bins[k] = (bins[k] * scale).coerceIn(0f, 1f)
        return bins
    }

    /**
     * Bin [bin]'s level from the four [bands], interpolated in LOG frequency between the band probe
     * centres. Below the first centre and above the last the nearest band's drive is held, since
     * there is nothing to interpolate toward.
     */
    private fun bandEnvelopeAt(bin: Int, bands: FloatArray): Float {
        val f = ln(AuroraSpectrum.centreHz(bin).toDouble()).toFloat()
        if (f <= BAND_CENTRE_LN[0]) return bands[0]
        val last = AuroraAnalyser.BANDS - 1
        if (f >= BAND_CENTRE_LN[last]) return bands[last]
        for (j in 0 until last) {
            val lo = BAND_CENTRE_LN[j]
            val hi = BAND_CENTRE_LN[j + 1]
            if (f <= hi) return lerp(bands[j], bands[j + 1], (f - lo) / (hi - lo))
        }
        return bands[last]
    }

    /**
     * The four band drives for [speaker] at [phase] within its `[start, end)`
     * window, or null when the phase is outside the window (the speaker is absent —
     * NOT a zero-filled array, which would read as "present but silent" and park a
     * resting ribbon on screen instead of retiring it; see [auroraVoices]).
     */
    private fun bandsFor(
        speaker: AuroraSpeaker,
        phase: Double,
        start: Double,
        end: Double,
    ): FloatArray? {
        if (phase < start || phase >= end) return null

        val u = (phase - start).toFloat()          // seconds into this turn
        val d = (end - start).toFloat()            // turn length
        // Attack/release so the turn swells in and tapers out — this is what
        // exercises each style's present→absent retire at the window edges.
        val gate = edge(u, ATTACK_SEC) * edge(d - u, RELEASE_SEC)

        // The syllable train. Human speaks a touch faster than the AI, so the two
        // are visually distinguishable even before colour.
        val rate = if (speaker == AuroraSpeaker.HUMAN) HUMAN_SYLLABLE_HZ else AI_SYLLABLE_HZ
        val pos = u * rate
        val syllable = floor(pos).toInt()
        val frac = pos - syllable                  // 0..1 within the current syllable
        // Per-syllable loudness, deterministic in (seed, speaker, syllable index):
        // some syllables land harder than others, the way stressed ones do.
        val amp = lerp(SYLLABLE_MIN_AMP, 1f, rand01(seed, speaker.ordinal, syllable))
        val profile = if (speaker == AuroraSpeaker.HUMAN) HUMAN_PROFILE else AI_PROFILE

        val bands = FloatArray(AuroraAnalyser.BANDS)
        for (j in bands.indices) {
            // Each band rides the syllable with its own phase offset, so the four
            // bands peak at slightly different instants — the asynchronous, band-
            // varied shimmer real speech has, not four identical copies.
            val pj = fracf(frac + BAND_PHASE[j])
            val burst = syllableBurst(pj)
            bands[j] = (gate * amp * profile[j] * burst).coerceIn(0f, 1f)
        }
        return bands
    }

    companion object {
        /**
         * Default seed. Arbitrary but FIXED — the whole point is that the demo (and
         * therefore every preview screenshot) is identical build to build.
         */
        const val DEFAULT_SEED = 0x5EEDL

        // ── Syllable cadence ────────────────────────────────────────────────
        /** Human syllable rate, Hz — the brisk end of natural speech (~4/s). */
        private const val HUMAN_SYLLABLE_HZ = 4.4f
        /** AI syllable rate, Hz — slightly slower, so the AI reads calmer/steadier. */
        private const val AI_SYLLABLE_HZ = 3.4f
        /** Fraction of each syllable that is VOICED; the remainder is the gap after it. */
        private const val SYLLABLE_DUTY = 0.62f
        /** Quietest a syllable gets, as a fraction of full — even soft syllables are audible. */
        private const val SYLLABLE_MIN_AMP = 0.5f

        // ── Turn envelope ───────────────────────────────────────────────────
        /** Seconds a turn ramps UP at its start (smoothstep). */
        private const val ATTACK_SEC = 0.18f
        /** Seconds a turn ramps DOWN before its end (smoothstep) — the retire tail. */
        private const val RELEASE_SEC = 0.30f

        /**
         * Per-speaker band weighting. The human is lifted in the upper (sibilance)
         * bands and the AI in the lower (body/vowel) bands, so the two voices differ
         * in TIMBRE as well as colour — the same way [AuroraAnalyser] would report a
         * bright voice vs a warm one. Indices follow the analyser: 0 body, 1 vowel,
         * 2 upper-mid, 3 sibilance.
         */
        private val HUMAN_PROFILE = floatArrayOf(0.85f, 1.0f, 0.92f, 0.78f)
        private val AI_PROFILE = floatArrayOf(1.0f, 0.92f, 0.72f, 0.5f)

        /**
         * Per-band phase offsets (fractions of a syllable). Non-zero and distinct so
         * the four bands crest at different moments — asynchronous band motion, not
         * one waveform copied four times.
         */
        private val BAND_PHASE = floatArrayOf(0.0f, 0.13f, 0.26f, 0.39f)

        // ── The spectrum channel ────────────────────────────────────────────

        /**
         * Each band's representative frequency, as a natural log — the GEOMETRIC mean of its
         * Goertzel probe pair, which is the middle of the band on the log axis a spectrum is laid
         * out on. DERIVED from [AuroraAnalyser.PROBES] rather than written down, so moving a probe
         * moves the synthetic spectrum with it and the two channels cannot drift apart.
         */
        private val BAND_CENTRE_LN = FloatArray(AuroraAnalyser.BANDS) { b ->
            val probes = AuroraAnalyser.PROBES[b]
            var lnSum = 0.0
            for (p in probes) lnSum += ln(p.toDouble())
            (lnSum / probes.size).toFloat()
        }

        /**
         * Which of the four bands owns bin [bin] — the band whose [BAND_CENTRE_LN] is nearest on the
         * log axis. Exposed because it is the statement of how the two synthetic channels relate,
         * and a test that could not ask the question would have to restate the mapping and drift.
         */
        internal fun bandForBin(bin: Int): Int {
            val f = ln(AuroraSpectrum.centreHz(bin.coerceIn(0, AuroraAnalyser.BINS - 1)).toDouble())
                .toFloat()
            var best = 0
            var bestD = Float.MAX_VALUE
            for (b in 0 until AuroraAnalyser.BANDS) {
                val d = kotlin.math.abs(f - BAND_CENTRE_LN[b])
                if (d < bestD) { bestD = d; best = b }
            }
            return best
        }

        /**
         * Quietest a bin's per-syllable texture gets, as a fraction of the band envelope. Real
         * speech spectra are jagged bin to bin; a flat 1.0 here would draw the four bands widened to
         * twenty-four, which is the exact look this channel exists to stop the preview advertising.
         * Fixed for the whole syllable, never per frame — a per-frame value would be the twitch the
         * v35 Fold walk reported, manufactured on purpose.
         */
        private const val BIN_TEXTURE_MIN = 0.45f

        /** How much the formant peak lifts the bins under it, as a fraction. */
        private const val FORMANT_GAIN = 0.55f

        /** The formant's half-width in bins (~1.21x per bin, so ~2 bins is a plausible resonance). */
        private const val FORMANT_WIDTH_BINS = 2.0f

        /**
         * Where the formant sweeps between, in bin index, across one syllable. The human's runs
         * higher and wider (a brighter, more mobile mouth) and the AI's lower and shorter, matching
         * the same contrast [HUMAN_PROFILE]/[AI_PROFILE] draw in the band channel.
         */
        private val HUMAN_FORMANT_BINS = floatArrayOf(7f, 15f)
        private val AI_FORMANT_BINS = floatArrayOf(5f, 11f)

        /**
         * The top of the range the LIVE channel publishes, and therefore the top of this one.
         *
         * [AudioAnalysisTap.BIN_HEADROOM]'s reciprocal is where a real frame at its own recent peak
         * lands, and it can never exceed it — the bound is arithmetic (one shared divisor that is
         * never smaller than the loudest bin it divides), not a clamp. Matching it is what makes the
         * preview honest about how full a bar gets on a device. DERIVED from the tap's constant
         * rather than copied, so the preview follows the channel if the headroom ever moves.
         */
        private val BIN_CEILING = 1f / AudioAnalysisTap.BIN_HEADROOM

        /**
         * One syllable's raised-cosine swell: 0 → 1 → 0 across the voiced portion
         * `[0, SYLLABLE_DUTY)`, then silence in the gap. `p` is 0..1 within the
         * syllable. The cosine gives the soft attack/decay a spoken syllable has, vs
         * a triangle's hard corners.
         */
        private fun syllableBurst(p: Float): Float {
            if (p >= SYLLABLE_DUTY) return 0f
            val q = p / SYLLABLE_DUTY                  // 0..1 across the voiced part
            return 0.5f - 0.5f * cos(2f * PI.toFloat() * q)
        }

        /** Smoothstep edge: 0 at x=0, 1 at x>=width; width<=0 is fully open. */
        private fun edge(x: Float, width: Float): Float {
            if (width <= 0f) return 1f
            val e = (x / width).coerceIn(0f, 1f)
            return e * e * (3f - 2f * e)
        }

        private fun lerp(a: Float, b: Float, t: Float): Float = a + (b - a) * t

        /** Fractional part in [0,1) for a small positive offset sum. */
        private fun fracf(v: Float): Float = v - floor(v)

        /** Positive floating modulo — phase folding never goes negative. */
        private fun floorMod(a: Double, m: Double): Double {
            val r = a % m
            return if (r < 0.0) r + m else r
        }

        /**
         * A deterministic float in [0,1) from three integers — a SplitMix-style
         * finalizer over the mixed inputs. Pure and stateless (no shared RNG), which
         * is what keeps [voicesAt] a pure function: the same `(seed, a, b)` always
         * hashes to the same value, on any JVM, forever.
         */
        internal fun rand01(seed: Long, a: Int, b: Int): Float {
            var h = seed
            h = h xor (a.toLong() * -0x61c8864680b583ebL)   // ×golden-ratio odd const
            h = h xor (b.toLong() * -0x7ee3623a03d3c765L)
            h = (h xor (h ushr 30)) * -0x40a7b892e31b1a47L
            h = (h xor (h ushr 27)) * -0x6b2fb644ecceee15L
            h = h xor (h ushr 31)
            // Top 24 bits → a stable [0,1) with no float-rounding surprises.
            return (((h ushr 40).toInt() and 0xFFFFFF).toFloat()) / 0x1000000.toFloat()
        }
    }
}
