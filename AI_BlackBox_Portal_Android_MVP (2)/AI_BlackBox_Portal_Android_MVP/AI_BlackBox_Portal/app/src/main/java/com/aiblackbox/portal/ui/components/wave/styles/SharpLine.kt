package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.EnvelopeRing
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import com.aiblackbox.portal.ui.components.wave.WaveformFrame
import com.aiblackbox.portal.ui.components.wave.WaveformRenderer
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.max
import kotlin.math.roundToInt
import kotlin.math.sin

// =============================================================================
// Sharp Reactive Line — reference sheet concept 5 ("MIC INPUT").
//
// A single THIN polyline (~1.75 dp) running the full width on the vertical
// centre, drawn from the speaker's envelope HISTORY ring: the NEWEST sample sits
// at the RIGHT edge and older samples trail LEFT, so the whole line scrolls
// leftward as time passes — the true past N seconds of loudness, straight from
// the shared feed (no synthesised wobble). It is deliberately JAGGED: straight
// segments between samples, NO bezier / no smoothing, because the sharp peaks ARE
// the design — Brandon's note on the soft waveform was literally "peaks could be
// sharper", and this style is the answer.
//
// COLOUR is per-SEGMENT, decided by loudness relative to the VISIBLE window:
//   • a segment whose amplitude clears ~the 70th percentile of the samples on
//     screen (and an absolute floor, so a silent window is never "all peaks")
//     renders RED — dim brand red at the threshold, brightening toward the hot
//     accent at the tallest peak — with a subtle additive Plus-blend GLOW pass on
//     those red peaks ONLY;
//   • everything else is a white / light-grey line (grey when quiet, brightening
//     toward white as it approaches the red threshold).
// The 70th-percentile gate is what makes the peaks POP: only the top slice of the
// visible window turns red, so the eye reads "these are the loud moments" rather
// than a uniformly coloured rope.
//
// SHARPNESS IS SPECTRAL, which is the one thing a single scalar could never give
// this style. Each column also reads the feed's four PER-BAND history rings at the
// same index, and the share of that column's energy sitting in the two HIGH bands
// pulls alternate columns down ([SharpLineMath.jaggedLevel]): a sibilant or a
// consonant attack breaks the line into real zig-zag spikes, while a body-heavy
// vowel of the identical loudness draws the smooth envelope it always drew. The
// jag only ever SUBTRACTS, so the amplitude budget below is inherited untouched,
// and a dull line is byte-for-byte the line that shipped. Colour answers the
// spectrum too, but only in DEGREE: the red gate stays a pure loudness decision,
// and brightness merely decides how hot a peak burns ([SharpLineMath.spectralHeat]).
//
// QUIET is a near-flat line with a tiny deterministic NOISE texture — never a
// dead-flat rule across the box. The noise is a hair of the height, shimmering
// off the shared wall-clock phase (so it is 60-vs-120 Hz identical) and it is
// added to GEOMETRY only, never to the loudness used for the red decision, so a
// silent window stays grey and textured, it does not flicker red.
//
// TWO VOICES: two independent polylines drawn in ONE pass off the shared feed —
// the human red-accented/white slightly ABOVE centre, the AI blue-accented/white
// slightly BELOW centre (a small fixed offset; the lines MAY cross, which is
// intended). Each voice keeps its own per-segment red/blue-or-grey logic against
// its OWN window, so a loud human and a quiet AI read correctly at once.
//
// BUDGET (Aurora v33, a REQUIREMENT not a suggestion):
//   • Every stroke is drawn with BlendMode.Plus — light, not paint. The particle
//     field behind is therefore NEVER occluded (SrcOver coverage = 0%, far under
//     the ~55% ceiling); the line adds glow to the near-black background, which
//     is exactly the white/red-on-black aesthetic.
//   • White-out is bounded because the marks are THIN (a ~1.75 dp core; a ~4 dp
//     glow only on the red slice, at low alpha) so the additive sum over any
//     pixel stays small.
//   • All geometry is height-RELATIVE (amplitude, offsets, noise, line widths are
//     fractions/dp of the box), and the peaks are amplitude-scaled to sit inside
//     the box with a margin at every height, so nothing mesas or clips at the
//     40 dp player bar — a small-height DEGRADE additionally trims the amplitude
//     and the glow so a short bar never white-outs (see [SharpLineMath.isSmall]).
//   • Motion is dt-free by construction: the scroll comes from the feed's
//     time-decimated ring and the shimmer from [WaveformFrame.phaseSec], so a
//     frame-count never enters the picture — 60 Hz and 120 Hz are identical.
//   • As a HISTORY style it OVERRIDES needsFrames to keep painting while the ring
//     still holds un-scrolled energy after the voice stops, then parks.
//
// A later agent replaces ONLY the body below; the public [sharpLineRenderer]
// factory name and its registration lambda never move. The drawing pulls every
// number and every pure decision from [SharpLineMath], which is JVM-testable with
// no Compose/Android in sight (the aurora tradition).
// =============================================================================

/**
 * Build a Sharp Reactive Line renderer. THE public surface a later maintainer
 * keeps stable: `WaveformStyles` registers this style by calling this function,
 * so its name/shape must not change even if the body does.
 */
internal fun sharpLineRenderer(): WaveformRenderer = SharpLineRenderer()

private class SharpLineRenderer : WaveformRenderer {

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        val small = SharpLineMath.isSmall(h, SharpLineMath.SMALL_HEIGHT_DP * density)
        // A voice is "active" (drawn) while it is present OR its ring still holds a
        // tail that has not yet scrolled off — a history style must let a departed
        // speaker's past scroll away, not blink out (the feed keeps pushing 0 into
        // its ring, so the tail marches left to flat on its own).
        val twoVoice = activeCount(frame.feed) >= 2
        val amp = SharpLineMath.ampPx(h, twoVoice = twoVoice, small = small)
        val coreW = max(SharpLineMath.LINE_WIDTH_DP * density, h * SharpLineMath.MIN_LINE_FRACTION)
        val glowW = coreW * (if (small) SharpLineMath.GLOW_MULT_SMALL else SharpLineMath.GLOW_MULT)

        for (i in SPEAKERS.indices) {
            val speaker = SPEAKERS[i]
            val ring = frame.feed.history(speaker)
            val present = frame.feed.present(speaker)
            if (!present && !SharpLineMath.ringHasEnergy(ring)) continue

            val cols = ring.capacity
            val centre = SharpLineMath.centreY(speaker, h, twoVoice)
            val threshold = SharpLineMath.percentileThreshold(ring, SharpLineMath.HOT_PERCENTILE)
            val hotAccent = if (speaker == AuroraSpeaker.HUMAN) BbxAccent else AI_HOT
            val hotDim = if (speaker == AuroraSpeaker.HUMAN) BbxRed else WaveBlue
            // A PRESENT-but-silent voice is lifted off dead centre by the caller's
            // restLevel (a paused player bar shows a faint textured sliver). A
            // DEPARTED voice gets no floor, so its tail scrolls all the way to flat.
            val floor = if (present) frame.restLevel.coerceIn(0f, 1f) else 0f

            // The four per-band rings, indexed by the SAME column as the level ring:
            // the feed pushes level and bands in one loop, so column k is one instant
            // of audio in all five.
            val bandRing0 = frame.feed.bandHistory(speaker, 0)
            val bandRing1 = frame.feed.bandHistory(speaker, 1)
            val bandRing2 = frame.feed.bandHistory(speaker, 2)
            val bandRing3 = frame.feed.bandHistory(speaker, 3)

            // Vertex y for column k (0 = newest/right). Raw sample drives the red
            // decision; the spectral jag and the noise shape the DRAWN y only, never
            // the decision — so a bright column reads sharper without being promoted
            // into a peak, and a silent window stays grey and textured.
            // TWO DOMAINS, ON PURPOSE, and the split is the whole subtlety here.
            //
            // [rawAt] is the DECISION domain: the raw ring sample, floored, compared
            // against [SharpLineMath.percentileThreshold] — which is itself a
            // percentile OF RAW SAMPLES — to answer "is this segment one of the loud
            // ones". Curving both sides of that comparison would only move the gate
            // (a monotone function cannot reorder a percentile), while curving one
            // side would break it, so the red/heat/cool ramps stay exactly where they
            // are calibrated.
            //
            // [drawnAt] is the GEOMETRY domain: the SAME sample through the app's one
            // perceptual curve ([Aurora.displayCurve]) and lifted onto the floor by
            // the one floor rule ([Aurora.restBlend]). This is the fix — the line used
            // to be drawn straight off the raw sample, so at a live mic's ordinary
            // level it sat a few percent off dead centre and read as a flat rule with
            // texture on it. The curve is the only thing between the sample and a y.
            fun rawAt(k: Int): Float = max(floor, ring.newest(k))
            fun drawnAt(k: Int): Float = Aurora.restBlend(floor, Aurora.displayCurve(ring.newest(k)))
            fun brightAt(k: Int): Float = SharpLineMath.brightness(
                bandRing0.newest(k), bandRing1.newest(k), bandRing2.newest(k), bandRing3.newest(k),
            )
            fun yAt(k: Int, bright: Float): Float =
                centre - SharpLineMath.jaggedLevel(drawnAt(k), bright, k) * amp +
                    SharpLineMath.noiseOffset(k, frame.phaseSec) * h * SharpLineMath.NOISE_FRACTION

            // Walk newest→oldest, drawing one straight segment per adjacent pair.
            // Per-segment colour is the whole point, so segments are individual
            // draws (not one path): a segment is RED when EITHER end clears the
            // window's hot gate, else white/grey. Glow only on the red segments.
            var k = 0
            // Carried across iterations: column k+1's brightness is column k's on the
            // next step, so each column is read out of its band rings exactly once.
            var bright0 = brightAt(0)
            while (k < cols - 1) {
                val bright1 = brightAt(k + 1)
                val x0 = SharpLineMath.columnX(k, cols, w)
                val x1 = SharpLineMath.columnX(k + 1, cols, w)
                val p0 = Offset(x0, yAt(k, bright0))
                val p1 = Offset(x1, yAt(k + 1, bright1))

                val segAmp = max(rawAt(k), rawAt(k + 1))
                val segBright = max(bright0, bright1)
                bright0 = bright1
                if (SharpLineMath.isHot(segAmp, threshold)) {
                    val heat = SharpLineMath.spectralHeat(
                        SharpLineMath.redIntensity(segAmp, threshold),
                        segBright,
                    )
                    val col = lerpColor(hotDim, hotAccent, heat)
                    // Additive glow pass on the peak first, then the bright core.
                    drawLine(
                        color = col,
                        start = p0, end = p1,
                        strokeWidth = glowW,
                        cap = StrokeCap.Round,
                        alpha = SharpLineMath.GLOW_ALPHA * (0.4f + 0.6f * heat),
                        blendMode = BlendMode.Plus,
                    )
                    drawLine(
                        color = col,
                        start = p0, end = p1,
                        strokeWidth = coreW,
                        cap = StrokeCap.Round,
                        alpha = SharpLineMath.CORE_ALPHA,
                        blendMode = BlendMode.Plus,
                    )
                } else {
                    // Cool segment: grey when quiet, toward white as it nears the
                    // red threshold. No glow — keeps the additive sum low.
                    val cool = SharpLineMath.coolIntensity(segAmp, threshold)
                    drawLine(
                        color = lerpColor(BbxDim, BbxWhite, cool),
                        start = p0, end = p1,
                        strokeWidth = coreW,
                        cap = StrokeCap.Round,
                        alpha = SharpLineMath.CORE_ALPHA * (0.7f + 0.3f * cool),
                        blendMode = BlendMode.Plus,
                    )
                }
                k++
            }
        }
    }

    /**
     * A scrolling/history style: the default (audible-only) park would FREEZE a
     * half-finished scroll the instant a speaker stopped. Keep frames while any
     * present voice is audible OR its ring still holds un-scrolled energy in the
     * visible window; park once every ring has drained to the floor.
     */
    override fun needsFrames(voices: List<AuroraVoice>, feed: WaveformFeed): Boolean {
        for (i in voices.indices) if (voices[i].hasEnergy()) return true
        // Scan EVERY ring, not just present speakers — a departed voice's tail is
        // still scrolling off and must keep frames until it has drained to flat.
        for (i in SPEAKERS.indices) {
            if (SharpLineMath.ringHasEnergy(feed.history(SPEAKERS[i]))) return true
        }
        return false
    }

    /** Speakers that are present OR still have a scrolling tail — drives two-voice layout. */
    private fun activeCount(feed: WaveformFeed): Int {
        var n = 0
        for (i in SPEAKERS.indices) {
            val s = SPEAKERS[i]
            if (feed.present(s) || SharpLineMath.ringHasEnergy(feed.history(s))) n++
        }
        return n
    }

    /** Straight channel-wise lerp; kept private (Color is Compose, math is in [SharpLineMath]). */
    private fun lerpColor(a: Color, b: Color, t: Float): Color {
        val u = t.coerceIn(0f, 1f)
        return Color(
            red = a.red + (b.red - a.red) * u,
            green = a.green + (b.green - a.green) * u,
            blue = a.blue + (b.blue - a.blue) * u,
            alpha = a.alpha + (b.alpha - a.alpha) * u,
        )
    }

    companion object {
        private val SPEAKERS = AuroraSpeaker.entries

        /**
         * The AI's "hot" accent — a lighter sibling of [WaveBlue] so the AI's loud
         * segments brighten the same way the human's red brightens toward the
         * accent, keeping the two voices visually symmetric (red family vs blue
         * family) rather than one glowing and one flat.
         */
        private val AI_HOT = Color(0xFFA9D0FF)
    }
}

/**
 * All of Sharp Line's geometry and per-segment colour DECISIONS as pure functions
 * of their inputs — no Compose, no android.*, so every number the look depends on
 * is pinned by [SharpLineMathTest] on the JVM, exactly as the aurora math is. The
 * renderer above is then "all draw, no logic": it reads these and paints.
 */
internal object SharpLineMath {

    // ---- line weight -------------------------------------------------------

    /**
     * Core polyline weight in dp. 1.75 dp sits in the reference sheet's 1.5–2 dp
     * band: THIN is essential — a thick line rounds its own corners and the sharp
     * peaks (the whole point) turn to mush.
     */
    const val LINE_WIDTH_DP = 1.75f

    /**
     * Floor on the core weight as a fraction of height, so on a very short surface
     * the line never falls below a hairline that sub-pixel-vanishes. At 40 dp this
     * is ~0.8 px, still under the dp minimum, so the dp value wins on real bars —
     * this only guards a pathologically tiny box.
     */
    const val MIN_LINE_FRACTION = 0.02f

    /** Glow-pass weight as a multiple of the core, on RED peaks only. */
    const val GLOW_MULT = 3.2f

    /** Reduced glow multiple on a small surface, so a 40 dp bar cannot white-out. */
    const val GLOW_MULT_SMALL = 2.0f

    // ---- alpha budget ------------------------------------------------------

    /** Core stroke alpha. Additive (Plus) so this brightens, never occludes. */
    const val CORE_ALPHA = 0.85f

    /** Peak-glow base alpha (scaled up with heat in the renderer). Kept low so the additive sum stays bounded. */
    const val GLOW_ALPHA = 0.22f

    // ---- amplitude ---------------------------------------------------------

    /**
     * One-voice peak reach as a fraction of height from the centre. 0.44 leaves a
     * ~6% margin above and below at full loudness, so a sample of 1.0 never
     * touches the edge (no mesa) while still using most of the box.
     */
    const val AMP_FRACTION = 0.44f

    /**
     * Two-voice amplitude scale. Each voice is offset off centre by
     * [OFFSET_FRACTION] and must still fit its peaks inside the box; 0.80 × 0.44 =
     * 0.352, and 0.352 + 0.07 = 0.422 < 0.5, so even a full-loudness human peak
     * (drawn above centre) stays inside the top edge, and the AI inside the bottom.
     */
    const val TWO_VOICE_AMP_SCALE = 0.80f

    /**
     * Small-surface amplitude scale (applied on top of the mode scale). Trims the
     * reach on a 40 dp bar so the tiny noise texture + peaks never crowd the edge.
     */
    const val SMALL_AMP_SCALE = 0.82f

    /**
     * Vertical offset of each voice from centre in two-voice mode, as a fraction of
     * height. Small on purpose: the human rides just above, the AI just below, and
     * the lines are ALLOWED to cross when both are loud (the reference sheet shows
     * them interleaving, not stacked in separate lanes).
     */
    const val OFFSET_FRACTION = 0.07f

    // ---- noise texture -----------------------------------------------------

    /**
     * Quiet-line texture amplitude as a fraction of height. 0.03 is a hair — a
     * living jitter so a silent line is "never dead-flat" (the spec) without
     * reading as signal. Added to drawn geometry only, never to loudness.
     */
    const val NOISE_FRACTION = 0.03f

    /**
     * How fast the noise texture shimmers, radians of phase per second fed into the
     * hash. Slow — the quiet line breathes, it does not buzz. Off the shared
     * wall-clock phase so it is refresh-rate independent.
     */
    const val NOISE_RATE = 2.3f

    // ---- red gate ----------------------------------------------------------

    /**
     * Percentile of the visible window a segment must clear to render RED. 0.70 =
     * only the loudest ~30% of what is on screen turns red, which is what makes the
     * peaks POP against a mostly-white line.
     */
    const val HOT_PERCENTILE = 0.70f

    /**
     * Absolute loudness floor for a red segment, regardless of percentile. Without
     * it, a SILENT window (every sample ~0) still has a 70th percentile of ~0 and
     * every segment would clear it — the line would go fully red the moment it went
     * quiet. This floor means "quiet stays grey".
     */
    const val HOT_FLOOR = 0.14f

    /**
     * Below this many real samples the window is too short to have a meaningful
     * percentile, so nothing is hot yet (the just-started line paints grey until
     * enough history exists to say what "loud" means here).
     */
    const val MIN_HOT_SAMPLES = 4

    /** Loudness above which a ring is considered still-moving for the idle park. */
    const val REST_EPS = 0.02f

    /** Below this height (px) the small-surface degrade engages. ~52 dp: the 40 dp bar is well inside it. */
    const val SMALL_HEIGHT_DP = 52f

    /** True when the surface is short enough to need the degraded amplitude/glow. */
    fun isSmall(heightPx: Float, smallHeightPx: Float): Boolean =
        heightPx > 0f && heightPx < smallHeightPx

    /** Peak reach in px for the current mode/surface. Always < height/2 so peaks never clip. */
    fun ampPx(heightPx: Float, twoVoice: Boolean, small: Boolean): Float {
        var f = AMP_FRACTION
        if (twoVoice) f *= TWO_VOICE_AMP_SCALE
        if (small) f *= SMALL_AMP_SCALE
        return heightPx * f
    }

    /** Vertical centre for a speaker: h/2 alone; human above / AI below in two-voice mode. */
    fun centreY(speaker: AuroraSpeaker, heightPx: Float, twoVoice: Boolean): Float {
        if (!twoVoice) return heightPx / 2f
        val off = heightPx * OFFSET_FRACTION
        return if (speaker == AuroraSpeaker.HUMAN) heightPx / 2f - off else heightPx / 2f + off
    }

    /**
     * X of column [k] (0 = newest) across [width] over [cols] history columns.
     * Newest at the RIGHT edge (x = width), oldest at the LEFT (x = 0). Guards a
     * single-column ring (cols ≤ 1) against a divide-by-zero by pinning to the
     * right edge.
     */
    fun columnX(k: Int, cols: Int, width: Float): Float {
        val denom = (cols - 1).coerceAtLeast(1)
        val frac = k.toFloat() / denom
        return width * (1f - frac)
    }

    /**
     * The [p]-percentile loudness of the [ring]'s VISIBLE samples (0..size-1),
     * used as the red gate. Returns 1f (nothing hot) when the window is shorter
     * than [MIN_HOT_SAMPLES] or empty — a not-yet-warmed line stays grey. Samples
     * are already 0..1 and NaN-free (the ring sanitises on push).
     */
    fun percentileThreshold(ring: EnvelopeRing, p: Float): Float {
        val n = ring.size
        if (n < MIN_HOT_SAMPLES) return 1f
        val arr = FloatArray(n) { ring.newest(it) }
        arr.sort()
        val idx = ((n - 1) * p.coerceIn(0f, 1f)).roundToInt().coerceIn(0, n - 1)
        return arr[idx]
    }

    /** A segment is RED when it clears BOTH the window percentile and the absolute floor. */
    fun isHot(segAmp: Float, threshold: Float): Boolean {
        if (segAmp.isNaN()) return false
        return segAmp >= threshold && segAmp >= HOT_FLOOR
    }

    /**
     * How red a hot segment is, 0..1: 0 at the threshold, 1 at full loudness —
     * drives dim-red → bright-accent and the glow alpha, so the TALLEST peak is
     * the brightest red. Guards threshold==1 (zero span) by returning 1.
     */
    fun redIntensity(segAmp: Float, threshold: Float): Float {
        val span = 1f - threshold
        if (span <= 1e-4f) return 1f
        return ((segAmp - threshold) / span).coerceIn(0f, 1f)
    }

    /**
     * How bright a COOL (non-red) segment is, 0..1: grey at silence, toward white
     * as it approaches the red threshold, so a loud-but-not-peak segment still
     * reads as louder. Guards threshold==0 (would divide by zero) → 0.
     */
    fun coolIntensity(segAmp: Float, threshold: Float): Float {
        if (threshold <= 1e-4f) return 0f
        return (segAmp / threshold).coerceIn(0f, 1f)
    }

    /**
     * Deterministic pseudo-noise in [-1, 1] for column [k] at wall-clock
     * [phaseSec] — the quiet-line texture. A pure hash of (k, quantised phase) so
     * it depends ONLY on its inputs, never on a frame count: the same phase yields
     * the same texture at 60 or 120 Hz. `sin`-hash (the fract-of-a-big-sine idiom
     * ParticleWave's sprite hash shares) — cheap, always finite, varies per column
     * so the line is never a flat rule.
     */
    fun noiseOffset(k: Int, phaseSec: Float): Float {
        val phase = if (phaseSec.isNaN()) 0f else phaseSec * NOISE_RATE
        val seed = k * 12.9898f + phase
        val s = sin(seed) * 43758.5453f
        val frac = s - kotlin.math.floor(s) // 0..1
        return frac * 2f - 1f // -1..1
    }

    /** True while any visible sample in [ring] is above [REST_EPS] — the scroll is still moving. */
    fun ringHasEnergy(ring: EnvelopeRing): Boolean {
        val n = ring.size
        for (i in 0 until n) if (ring.newest(i) > REST_EPS) return true
        return false
    }

    // ---- spectral sharpness -------------------------------------------------

    /**
     * How deep the jag cuts at full brightness, as a fraction of the column's level. 0.35 is a zig
     * you cannot miss at speech level yet still reads as one line rather than a comb.
     */
    const val JAG_DEPTH = 0.35f

    /** How much of the remaining heat a fully bright peak adds on top of its loudness heat. */
    const val HEAT_BRIGHT_GAIN = 0.5f

    /** Below this the column carries no usable spectral shape — no brightness, so no jag. */
    private const val SPECTRAL_EPS = 1e-4f

    /**
     * How much of this column's energy is HIGH, 0..1 — the two upper bands measured against the
     * loudest band in the same column.
     *
     * RELATIVE, deliberately. Absolute band levels are not comparable across bands on live audio:
     * the measured spread between the loudest and the quietest band is over an order of magnitude
     * (the upper bands sit near their noise floor while the body carries the voice), so a jag gated
     * on an absolute high-band level would never fire on a phone mic. A ratio asks the question that
     * actually matters here — "is the top of the spectrum what is loud right now" — and keeps
     * answering it if the band calibration moves underneath.
     */
    fun brightness(b0: Float, b1: Float, b2: Float, b3: Float): Float {
        val e0 = sanit(b0)
        val e1 = sanit(b1)
        val e2 = sanit(b2)
        val e3 = sanit(b3)
        val high = maxOf(e2, e3)
        val peak = maxOf(maxOf(e0, e1), high)
        if (peak <= SPECTRAL_EPS) return 0f
        return (high / peak).coerceIn(0f, 1f)
    }

    /**
     * The level actually DRAWN at column [column]: the envelope, cut by the spectral jag.
     *
     * This is the style's whole answer to "respond to spectral content, not just level". Sharpness
     * is what Sharp Line is for, so the spectrum drives the SHARPNESS: bright content pulls alternate
     * columns down and the polyline zig-zags between neighbours, while a dark, body-heavy passage
     * draws the smooth envelope it always drew. Loudness still sets the height.
     *
     * The jag only ever SUBTRACTS. That is not a detail: it means no spectrum can push a vertex
     * higher than the level did, so the amplitude budget that keeps peaks off the box edge (and the
     * test that pins it) is inherited unchanged rather than re-argued.
     */
    fun jaggedLevel(level: Float, brightness: Float, column: Int): Float {
        val l = sanit(level)
        if ((column and 1) == 0) return l
        return l * (1f - JAG_DEPTH * sanit(brightness))
    }

    /**
     * A hot segment's heat, 0..1, once its spectrum is taken into account: a bright peak burns
     * closer to the accent than a dull one of the SAME height. The red GATE stays purely a loudness
     * decision ([isHot]) — "these are the loud moments" is what the percentile is for — and only how
     * hot the peak burns answers the spectrum. Neutral at zero brightness, so a dull line is exactly
     * the shipped colour ramp.
     */
    fun spectralHeat(heat: Float, brightness: Float): Float {
        val h = sanit(heat)
        return (h + (1f - h) * HEAT_BRIGHT_GAIN * sanit(brightness)).coerceIn(0f, 1f)
    }

    /** NaN is caught explicitly — `coerceIn` passes it straight through (JLS 15.20.1). */
    private fun sanit(v: Float): Float = if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
}
