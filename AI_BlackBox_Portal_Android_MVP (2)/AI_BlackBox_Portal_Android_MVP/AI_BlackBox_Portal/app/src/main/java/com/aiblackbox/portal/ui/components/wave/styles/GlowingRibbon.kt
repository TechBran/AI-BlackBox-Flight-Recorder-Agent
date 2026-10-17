package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.wave.WaveformFrame
import com.aiblackbox.portal.ui.components.wave.WaveformRenderer
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.sin
import kotlin.math.sqrt

// =============================================================================
// Glowing Ribbon — reference sheet concept #1, the "premium braid".
//
// A BUNDLE of ~7 fine strands (~1.25 dp each) per voice. Every strand is a smooth
// wave of a few humps across the full width sharing one base shape, but each strand
// carries its OWN phase offset, amplitude, frequency and travel-speed offset, so
// they fan apart and cross through one another like woven silk rather than
// stacking into a single fat rope.
//
// THE STRANDS ARE DRIVEN BY THE SPECTRUM, AND SO IS THEIR SHAPE (M7). Strand j owns
// a contiguous SLICE of the frequency map — a real ~3-bin slice of the 24-bin log
// spectrum when a producer publishes one, and the four-band round robin the style
// shipped with when nobody does (every live call site today; see
// [RibbonMath.strandLevel]). On top of that the SHAPE of the wave is bent by two
// volume-blind descriptors of the spectrum, which is what separates this style from
// the version it replaces:
//   • [RibbonMath.centroid] — where the energy sits, low to high — bends the
//     FREQUENCY, so a dark voice draws long broad arcs and a bright one a tighter
//     ripple.
//   • [RibbonMath.spread] — how far the energy is smeared across the map — sets the
//     depth of a travelling PHASE modulation, so a sustained vowel stays a clean
//     sine while a broadband, consonant-heavy passage makes the strand writhe.
//   • each strand's own drive LEADS its phase, so the bundle shears with the
//     spectrum: strands on the loud part of the map run ahead of the quiet ones.
// Both descriptors are ratios, so they are blind to loudness by construction: turn
// the same voice down and the ribbon gets shorter without changing shape. That is
// the M7 rule stated as an equation, and `equal_level_spectra_with_different
// _content_draw_different_shapes` is the test that holds it.
//
// This is an INSTANTANEOUS style (no history ring). It is the braided evolution of
// Aurora, and it keeps Aurora v33's identity rule verbatim:
//   • DIRECTION IS IDENTITY. Two voices in one container are told apart the way
//     Aurora tells them apart: the human bundle travels LEFT→RIGHT (dir +1), the
//     AI bundle RIGHT→LEFT (dir −1), forever. Colour is then free to carry HEAT,
//     not identity — the human bundle is reds fading to WHITE on the hottest
//     strands, the AI bundle blues fading to white — and the two braid through
//     each other, unmistakable within a second of watching.
// What it does NOT keep is Aurora's crest-travel band: see [RibbonMath.crestSpeed].
//
// QUIET relaxes the bundle to a FEW low-amplitude idle strands (a live mic keeps
// breathing between words): the caller's [WaveformFrame.restLevel] is spread
// across the strands by [RibbonMath.idleWeight] so a handful glow faintly at rest
// and the rest fall to nothing. A player bar passes restLevel 0, so on the file's
// silence every strand collapses to zero drive and NOTHING is drawn — the flat
// player-bar rest state Classic/Aurora/Mirrored all honour.
//
// VISUAL BUDGET (Aurora v33 — a REQUIREMENT, not a suggestion):
//   • OCCLUSION is ZERO. Every pass is BlendMode.Plus (additive light), so the
//     particle field behind is only ever ADDED to, never dimmed — the ≤55% SrcOver
//     coverage ceiling is met trivially (there are no SrcOver passes). "Airy by
//     construction": the strands are thin lines on near-black, not filled bodies.
//   • WHITE-OUT is bounded, and the M7 glow rebalance did not move that bound. The
//     strands are phase-SPREAD (they fan apart), so at any single pixel only a few
//     can coincide; the taper eases to a non-zero floor rather than pinching every
//     strand to one shared tip, so the ends fan out too instead of piling N strands
//     onto one point. The design bound is [RibbonMath.MAX_COINCIDENT_STRANDS] = 4
//     strands within a stroke width at a shared pixel (opposite-travel two-voice
//     crossings are transient and sub-pixel); 4 × ([RibbonMath.CORE_ALPHA_MAX] +
//     [RibbonMath.GLOW_ALPHA_MAX]) = 1.52, under Aurora's 1.6 ceiling — the SAME
//     1.52 the pre-M7 split produced, because the rebalance moved light between the
//     two passes rather than adding any.
//   • NO MESA at 40 dp. Every dimension is height-relative and the amplitude is
//     budgeted (peak excursion + two-voice split stays a clear margin inside the
//     rails at every shipped height/density), so nothing clips. The 40 dp player
//     bar additionally DEGRADES to FEWER strands (4) and a smaller amplitude, so a
//     short bar is a calm braid, never a ripple-mush (see [RibbonMath.isSmall]).
//   • MOTION is dt-free: the whole picture is a pure function of
//     [WaveformFrame.phaseSec] (the shared wall-clock phase) and the CURRENT
//     spectrum, so a 60 Hz phone and a 120 Hz Fold animate at the identical speed —
//     no frame count and no integrated state ever enter.
//
// A later agent replaces ONLY the body below; the public [ribbonRenderer] factory
// name and its registration lambda in WaveformRegistry never move. Every number
// and every decision lives in [RibbonMath], JVM-testable with no Compose/Android
// in sight (the aurora tradition); the renderer is "all draw, no logic".
// =============================================================================

/**
 * Build a Glowing Ribbon renderer. THE public surface a later maintainer keeps
 * stable: `WaveformStyles` registers this style by calling this function, so its
 * name/shape must not change even if the body does.
 */
internal fun ribbonRenderer(): WaveformRenderer = RibbonRenderer()

private class RibbonRenderer : WaveformRenderer {

    /**
     * Per-strand path scratch, rewound (never reset) between strands and kept alive
     * across frames — the renderer is remembered per selection, so this is built
     * once and its native point/verb storage is reused, exactly as AuroraWaveform
     * hoists its Paths out of the draw path. One strand is drawn twice (glow then
     * core) off the same built path.
     */
    private val strandPath = Path()

    /**
     * The current speaker's ascending-frequency levels, refilled in place once per
     * speaker per frame — 24 entries when that speaker publishes a spectrum, the
     * first 4 when it does not. Sized for the larger case and ALWAYS read against
     * the returned count, never `size`, so the stale tail left by a previous
     * speaker's spectrum can never leak into a four-band read.
     */
    private val levels = FloatArray(AuroraAnalyser.BINS)

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        val small = RibbonMath.isSmall(h, RibbonMath.SMALL_HEIGHT_DP * density)
        // Instantaneous style: "two voices" is simply both speakers present this
        // frame (no scrolling tail to keep a departed voice alive, unlike the
        // history styles). A voice that leaves the list stops being drawn.
        val twoVoice = frame.feed.present(AuroraSpeaker.HUMAN) && frame.feed.present(AuroraSpeaker.AI)
        val strands = RibbonMath.strandCount(small)
        val steps = RibbonMath.sampleCount(w)
        val restFloor = RibbonMath.sanitizeRest(frame.restLevel)

        // Widths are density/height-relative (never fixed px), floored so a strand
        // cannot sub-pixel-vanish on a tall surface. Built once per frame — every
        // strand shares them — and the glow is the wider, lower-alpha halo pass.
        val coreW = max(RibbonMath.STRAND_WIDTH_DP * density, h * RibbonMath.MIN_STRAND_FRACTION)
        val glowW = coreW * (if (small) RibbonMath.GLOW_MULT_SMALL else RibbonMath.GLOW_MULT)
        val coreStroke = Stroke(width = coreW, cap = StrokeCap.Round, join = StrokeJoin.Round)
        val glowStroke = Stroke(width = glowW, cap = StrokeCap.Round, join = StrokeJoin.Round)

        for (si in SPEAKERS.indices) {
            val speaker = SPEAKERS[si]
            if (!frame.feed.present(speaker)) continue

            val count = fillLevels(frame, speaker)
            // The two shape descriptors, computed ONCE for the whole bundle: they
            // describe the voice, not the strand, and every strand of one voice is
            // bent by the same spectrum.
            val centroid = RibbonMath.centroid(levels, count)
            val spread = RibbonMath.spread(levels, count)

            val dir = RibbonMath.travelDirection(speaker)
            val centre = RibbonMath.centreY(speaker, h, twoVoice)

            for (j in 0 until strands) {
                val slice = RibbonMath.strandLevel(levels, count, j, strands)
                val drive = RibbonMath.strandDrive(slice, restFloor, j)
                // A strand with no drive at all (player-bar silence) paints nothing —
                // the flat rest state. A present-but-silent live mic keeps its idle
                // strands above this via restFloor.
                if (drive <= RibbonMath.MIN_VISIBLE_DRIVE) continue

                val amp = RibbonMath.ampPx(h, drive, j, twoVoice, small)
                val colour = strandColor(speaker, drive)

                strandPath.rewind()
                for (i in 0..steps) {
                    val p = i.toFloat() / steps
                    val x = p * w
                    val y = RibbonMath.strandY(
                        p, h, centre, amp, frame.phaseSec, dir, j, centroid, spread, drive,
                    )
                    if (i == 0) strandPath.moveTo(x, y) else strandPath.lineTo(x, y)
                }

                // Additive halo then a brighter additive core — light, not paint, so
                // the particle field behind is never dimmed. Idle strands still glow
                // faintly (the BRIGHT_FLOOR term), loud strands drive toward white.
                drawPath(strandPath, colour, alpha = RibbonMath.glowAlpha(drive), style = glowStroke, blendMode = BlendMode.Plus)
                drawPath(strandPath, colour, alpha = RibbonMath.coreAlpha(drive), style = coreStroke, blendMode = BlendMode.Plus)
            }
        }
    }

    // Default needsFrames (audible-only park) is correct: an instantaneous style
    // shows nothing new once every present voice is silent, and — like Aurora at
    // its baseline — a present-but-silent live mic parks on its idle braid until
    // room noise lifts a band again. No history tail to keep scrolling. It reads the
    // PASSED voices, never the feed: audibility taken from the feed park-deadlocks
    // the loop, because the feed only advances on a frame the park is withholding.

    /**
     * Load [speaker]'s ascending-frequency levels into [levels] and return how many
     * are valid — [AuroraAnalyser.BINS] when a real spectrum is riding along this
     * frame, [AuroraAnalyser.BANDS] otherwise.
     *
     * The fallback is not a degraded path. The player bar is fed by the OFFLINE decode,
     * which produces four bands and runs no transform at all, so those four are
     * genuinely everything that exists on that surface and the braid has to look
     * finished on them.
     *
     * BOTH channels are read from the FEED, never from [WaveformFrame.spectra] directly.
     * The frame's list is the carrier exactly as the producer published it, at the
     * ~50 Hz analysis hop; this draws at 60-120 Hz, so reading it raw holds one value
     * for two or three frames and then jumps — measured through the real tap at 120 Hz,
     * 58% of drawn frames would be byte-identical to the one before. That is the
     * stepping half of the v35 "isn't fluent" report, manufactured inside the style.
     * The feed's single smoother is what turns that staircase into motion (0% repeats,
     * half the step), and it is already where the four bands below come from.
     */
    private fun fillLevels(frame: WaveformFrame, speaker: AuroraSpeaker): Int {
        val feed = frame.feed
        if (feed.hasSpectrum(speaker)) {
            for (k in 0 until AuroraAnalyser.BINS) levels[k] = feed.bin(speaker, k)
            return AuroraAnalyser.BINS
        }
        for (b in 0 until AuroraAnalyser.BANDS) levels[b] = feed.band(speaker, b)
        return AuroraAnalyser.BANDS
    }

    /**
     * The strand's colour by HEAT (its own drive), not by identity — identity is
     * carried by the travel direction. A 3-stop ramp: dim brand colour at the
     * quiet floor, the accent mid, WHITE on the hottest strands (the "fading to
     * white" the reference sheet calls for). Human rides the RED family, AI the
     * BLUE family, so the two bundles stay tellable apart by hue too even as they
     * braid through each other.
     */
    private fun strandColor(speaker: AuroraSpeaker, heat: Float): Color {
        val h = heat.coerceIn(0f, 1f)
        val dim = if (speaker == AuroraSpeaker.HUMAN) BbxRed else WaveBlue
        val mid = if (speaker == AuroraSpeaker.HUMAN) BbxAccent else AI_MID
        return if (h < 0.5f) lerpColor(dim, mid, h / 0.5f)
        else lerpColor(mid, BbxWhite, (h - 0.5f) / 0.5f)
    }

    /** Channel-wise lerp; kept private (Color is Compose, the math is in [RibbonMath]). */
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
         * The AI's mid-ramp accent — a light sibling of [WaveBlue] (the same value
         * SharpLine's AI hot uses), so the AI bundle brightens toward white the same
         * way the human's red does, keeping the two voices visually symmetric.
         */
        private val AI_MID = Color(0xFFA9D0FF)
    }
}

/**
 * All of Glowing Ribbon's geometry, strand offsets, spectral descriptors, drive
 * shaping and brightness DECISIONS as pure functions of their inputs — no Compose,
 * no android.*, so every number the look depends on is pinned by [GlowingRibbonTest]
 * on the JVM, exactly as the aurora math is. The renderer above then reads these and
 * paints.
 */
internal object RibbonMath {

    // ---- strand count ------------------------------------------------------

    /**
     * Full-surface strand count per voice. 7 sits in the reference sheet's 6–8
     * band: enough to read as a woven bundle, few enough that two bundles (14
     * strands) never crowd the additive budget.
     */
    const val STRANDS_FULL = 7

    /**
     * Degraded strand count on a short surface (the 40 dp player bar). 4 keeps the
     * braid legible where 7 would ripple-mush at a fraction of the height — the
     * plan's "40 dp: fewer strands (4)".
     */
    const val STRANDS_SMALL = 4

    fun strandCount(small: Boolean): Int = if (small) STRANDS_SMALL else STRANDS_FULL

    // ---- stroke weight -----------------------------------------------------

    /**
     * Core strand weight in dp. 1.25 dp sits in the reference sheet's 1–1.5 dp
     * band: THIN is the whole point — the braid is airy, and a thin line keeps the
     * additive white-out sum low where strands cross.
     */
    const val STRAND_WIDTH_DP = 1.25f

    /**
     * Floor on the core weight as a fraction of height, so on a pathologically tiny
     * surface a strand never falls below a hairline that sub-pixel-vanishes. On a
     * real 40 dp bar the dp value wins; this only guards a degenerate box.
     */
    const val MIN_STRAND_FRACTION = 0.012f

    /** Glow-halo weight as a multiple of the core. The soft additive bloom. */
    const val GLOW_MULT = 3.0f

    /** Reduced glow multiple on a small surface, so a 40 dp bar cannot white-out. */
    const val GLOW_MULT_SMALL = 2.0f

    // ---- brightness / white-out budget -------------------------------------

    /**
     * Peak core-stroke alpha (heat = 1), and peak glow-halo alpha.
     *
     * THE M7 REBALANCE, and what it is and is not allowed to move. The operator's
     * note was "the glow is great but underused": the split was 0.30 core against
     * 0.08 glow, and since the halo covers [GLOW_MULT]× the width of the core, that
     * is 0.30 of light in the hard line against 0.24 in the halo — the bloom was the
     * MINORITY of the ribbon's light despite being the thing that makes it look lit.
     * It is now 0.22 / 0.16, i.e. 0.22 against 0.48, and the glow carries roughly
     * 69% of the light ([glowLightShare]).
     *
     * The constraint that made it a rebalance rather than a brightening: the two
     * still SUM to 0.38, so [worstAdditiveStack] is 1.52 — bit-identical to the
     * pre-M7 value and still under [WHITE_OUT_CEILING]. Per-pixel white-out is the
     * budget that actually binds on an additive style, and it did not move; what
     * moved is which of the two passes spends it. Raising the pair's SUM is the
     * thing to refuse here, not raising the glow.
     */
    const val CORE_ALPHA_MAX = 0.22f
    const val GLOW_ALPHA_MAX = 0.16f

    /**
     * Brightness a PRESENT strand keeps at zero heat, as a fraction of the peak —
     * so the idle braid glows faintly rather than going invisible, and a strand
     * ramps from this floor up to full as its band lights. Absent strands are
     * culled upstream (drive ≤ [MIN_VISIBLE_DRIVE]); this only floors the ones
     * that are drawn.
     */
    const val BRIGHT_FLOOR = 0.30f

    /**
     * The most strands the design allows to coincide within one stroke width at a
     * single pixel. Strands are phase-SPREAD (they fan apart) and the taper eases
     * to a non-zero floor so the ends fan out too instead of pinching to one shared
     * tip; opposite-travel two-voice crossings are transient and sub-pixel. So a
     * handful, not the whole bundle, can pile up at once.
     */
    const val MAX_COINCIDENT_STRANDS = 4

    /** Aurora's additive white-out ceiling, carried over. */
    const val WHITE_OUT_CEILING = 1.6f

    /** Peak brightness ramp: [BRIGHT_FLOOR] at heat 0, 1.0 at heat 1. NaN-safe. */
    private fun brightRamp(heat: Float): Float {
        val h = if (heat.isNaN()) 0f else heat.coerceIn(0f, 1f)
        return BRIGHT_FLOOR + (1f - BRIGHT_FLOOR) * h
    }

    /** Core-stroke alpha for a strand at [heat] (0..1). */
    fun coreAlpha(heat: Float): Float = CORE_ALPHA_MAX * brightRamp(heat)

    /** Glow-halo alpha for a strand at [heat] (0..1). */
    fun glowAlpha(heat: Float): Float = GLOW_ALPHA_MAX * brightRamp(heat)

    /** The worst additive stack the budget permits at any pixel — pinned under [WHITE_OUT_CEILING]. */
    fun worstAdditiveStack(): Float = MAX_COINCIDENT_STRANDS * (CORE_ALPHA_MAX + GLOW_ALPHA_MAX)

    /**
     * The glow's share of the light a strand puts on screen, weighting each pass by
     * the width it actually covers. The measure the M7 rebalance is stated in, and
     * the reason a bare alpha comparison is the wrong one: the halo is three times
     * as wide as the core, so equal alphas would not be an equal contribution.
     */
    fun glowLightShare(): Float {
        val glow = GLOW_ALPHA_MAX * GLOW_MULT
        return glow / (glow + CORE_ALPHA_MAX)
    }

    // ---- spectral descriptors ----------------------------------------------
    //
    // Both work over ANY array of ascending-frequency levels — the 24 log bins when
    // a spectrum is published, the four bands when one is not — because both ARE
    // spectra, differing only in resolution, and a style should not need two
    // implementations of "where is the energy".
    //
    // Both are RATIOS, and that is the load-bearing property: multiplying every
    // level by the same factor leaves them unchanged. So they describe CONTENT and
    // never loudness, which is exactly the split this style needs — loudness goes to
    // the amplitude, content goes to the shape. A descriptor that drifted with
    // volume would make a ribbon that just gets busier when you talk louder.

    /** What [centroid] returns when there is nothing to weigh: dead centre, no tilt. */
    const val NEUTRAL_CENTROID = 0.5f

    /** NaN-scrubbed, clamped read — every descriptor goes through it so none can be poisoned. */
    private fun levelAt(levels: FloatArray, i: Int): Float {
        val v = levels[i]
        return if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
    }

    /**
     * Spectral centre of mass over the first [n] levels: 0 when all the energy is in
     * the lowest entry, 1 when it is all in the highest, [NEUTRAL_CENTROID] on
     * silence (nothing to be biased about) and on a degenerate array.
     */
    fun centroid(levels: FloatArray, n: Int = levels.size): Float {
        val count = n.coerceIn(0, levels.size)
        if (count < 2) return NEUTRAL_CENTROID
        var weight = 0f
        var moment = 0f
        for (i in 0 until count) {
            val v = levelAt(levels, i)
            weight += v
            moment += v * i
        }
        if (weight <= 0f) return NEUTRAL_CENTROID
        return (moment / weight / (count - 1)).coerceIn(0f, 1f)
    }

    /**
     * The widest a normalised spectrum's standard deviation can be: half the energy
     * pinned at each end of the map. Dividing by it is what puts [spread] on 0..1.
     */
    private const val MAX_CENTROID_SD = 0.5f

    /**
     * How far the energy is smeared across the first [n] levels: 0 for a single
     * tone (or silence), 1 for energy split between the two extremes. A flat
     * spectrum sits at 0.75 over four bands and tends to 0.58 as the resolution
     * rises, so the swirl is always well short of its cap on real speech.
     */
    fun spread(levels: FloatArray, n: Int = levels.size): Float {
        val count = n.coerceIn(0, levels.size)
        if (count < 2) return 0f
        val c = centroid(levels, count)
        var weight = 0f
        var variance = 0f
        for (i in 0 until count) {
            val v = levelAt(levels, i)
            val d = i.toFloat() / (count - 1) - c
            weight += v
            variance += v * d * d
        }
        if (weight <= 0f) return 0f
        return (sqrt(variance / weight) / MAX_CENTROID_SD).coerceIn(0f, 1f)
    }

    /**
     * The level strand [j] of a bundle of [strands] reads out of the first [n]
     * entries of [levels].
     *
     * ONE rule covering both resolutions: when there are at least as many levels as
     * strands, strand j owns a CONTIGUOUS SLICE of them and reads its mean — with a
     * 24-bin spectrum that spreads the braid across the whole log-frequency map,
     * ~3.4 bins a strand, which is the spectral uplift. When there are fewer levels
     * than strands (the four bands every shipped call site feeds today) the strands
     * ROUND ROBIN them, which is byte-for-byte the mapping the style shipped with,
     * so nothing about today's device behaviour changes until a producer publishes
     * bins.
     */
    fun strandLevel(levels: FloatArray, n: Int, j: Int, strands: Int): Float {
        val count = n.coerceIn(0, levels.size)
        if (count <= 0) return 0f
        val s = strands.coerceAtLeast(1)
        val idx = j.coerceAtLeast(0) % s
        if (s > count) return levelAt(levels, idx % count)
        val lo = idx * count / s
        val hi = maxOf(lo + 1, (idx + 1) * count / s)
        var sum = 0f
        var taken = 0
        var k = lo
        while (k < hi && k < count) {
            sum += levelAt(levels, k)
            taken++
            k++
        }
        return if (taken == 0) 0f else sum / taken
    }

    // ---- drive shaping -----------------------------------------------------

    /**
     * THE perceptual lift the braid applies to a level before it drives the geometry —
     * quiet-but-present bands still animate. It is [Aurora.displayCurve], CALLED, not a
     * private square root: this function used to be `sqrt(...)`, which is a SECOND copy
     * of the app's one curve and therefore froze the braid at the exponent Aurora
     * shipped in v33 while Aurora itself moved. The app has exactly one curve and every
     * consumer calls it, so a re-tune there moves this style with it.
     *
     * NaN/out-of-range absorbed, never propagated (a single NaN into a strand amplitude
     * is unrecoverable).
     */
    fun shaped(drive: Float): Float {
        if (drive.isNaN()) return 0f
        return Aurora.displayCurve(drive.coerceIn(0f, 1f))
    }

    /** A NaN/out-of-range rest floor is absorbed to 0 (a garbage floor must not poison a strand). */
    fun sanitizeRest(restLevel: Float): Float =
        if (restLevel.isNaN()) 0f else restLevel.coerceIn(0f, 1f)

    /**
     * How much of the caller's rest floor strand [j] keeps at idle. Only a FEW
     * strands hold the floor (the plan's "few low-amplitude idle strands"): even
     * strands keep it in full, odd strands a quarter, so a silent live mic settles
     * to a sparse, breathing braid rather than an even flat comb.
     */
    fun idleWeight(j: Int): Float = if (j % 2 == 0) 1.0f else 0.25f

    /**
     * Strand [j]'s drive (0..1): its live, curve-shaped spectral level BLENDED ONTO its
     * idle floor (rest × idleWeight) by the app's one floor rule, [Aurora.restBlend].
     * So a paused player bar (rest 0) leaves a silent strand at exactly 0 (drawn
     * nothing — the blend is the identity at floor 0), a live mic keeps a few idle
     * strands alive, and speech lifts every mapped strand.
     *
     * BLENDED, not `max`-ed. Under a max, every spectral level below a strand's idle
     * weight drew the IDENTICAL strand, so on a live mic the even strands sat frozen at
     * their idle height through the whole quiet end of the range and only the loud
     * moments moved them. The blend keeps the idle floor exactly (level 0 → floor) and
     * still lets everything above it move.
     */
    fun strandDrive(bandDrive: Float, restFloor: Float, j: Int): Float {
        val idle = restFloor * idleWeight(j)
        val active = shaped(bandDrive)
        return Aurora.restBlend(idle, active)
    }

    // ---- the wave: travel, then the spectrum's own terms -------------------

    /**
     * Base humps across the width for strand [j] at a neutral spectrum.
     * `sin(p·π·freq)` draws freq/2 humps, so [BASE_FREQ] = 5.0 is 2.5 humps — a
     * smooth, silk-like curve, fewer than Aurora's 4–5 because a braid wants broad
     * arcs its strands can weave through, not a busy ripple. A small per-strand step
     * means the strands do not travel as one slab and their crossing nodes drift
     * apart.
     */
    const val BASE_FREQ = 5.0f
    const val FREQ_STEP = 0.35f

    /**
     * How hard the spectral [centroid] bends the frequency, as a fraction either
     * side of the base. 0.9 gives a ×0.55 … ×1.45 sweep, i.e. 1.4 – 3.6 humps on the
     * lead strand: a dark voice draws long broad arcs, a bright one a tighter
     * ripple, and the difference is obvious at a glance without either extreme
     * ceasing to be a wave.
     *
     * It multiplies rather than adds so the per-strand ladder ([FREQ_STEP]) stays
     * proportional at every spectrum, and staying under 2.0 is what guarantees the
     * frequency is positive for every centroid.
     */
    const val FREQ_TILT = 0.9f

    /** Strand [j]'s humps-across-the-width, bent by where the voice's energy sits. */
    fun frequency(j: Int, centroid: Float): Float {
        val c = if (centroid.isNaN()) NEUTRAL_CENTROID else centroid.coerceIn(0f, 1f)
        val base = BASE_FREQ + j.coerceAtLeast(0) * FREQ_STEP
        return base * (1f + FREQ_TILT * (c - NEUTRAL_CENTROID))
    }

    /**
     * Crest travel speed for strand [j], in WIDTHS PER SECOND — the observable
     * quantity, and RECALIBRATED in M7.
     *
     * It used to sit in Aurora v33's 0.25–0.37 widths/sec, which sounds slow and
     * measures slower: a crest took 2.7–4.0 SECONDS to cross the ribbon, so on the
     * time scale of a spoken phrase the braid was a still picture with a drift. The
     * operator asked for "much more fluid motion", quantified as a crest crossing in
     * roughly [CROSS_SEC_MIN]–[CROSS_SEC_MAX], and this band is that requirement
     * inverted — 1/1.0 to 1/0.6 widths/sec, about 4.4× the old speed at the top.
     * [crossSeconds] states it the way it was specified so the two cannot drift.
     *
     * Spread across the strand set as a per-strand SHEAR so the bundle reads with
     * parallax depth as it weaves; at these speeds the shear is worth ~0.4 s of
     * crossing time between the slowest and fastest strand instead of the old 1.3 s
     * spread of a motion nobody could see. Spread over [STRANDS_FULL] − 1 regardless
     * of the current count, so even the 4-strand degrade keeps every speed inside
     * the band.
     */
    const val SPEED_MIN = 1.0f
    const val SPEED_MAX = 1.65f

    /** The calibration as the operator stated it: seconds for a crest to cross the width. */
    const val CROSS_SEC_MAX = 1.0f
    const val CROSS_SEC_MIN = 0.606f

    fun crestSpeed(j: Int): Float {
        val span = (STRANDS_FULL - 1).coerceAtLeast(1)
        val t = (j.coerceAtLeast(0).toFloat() / span).coerceIn(0f, 1f)
        return SPEED_MIN + (SPEED_MAX - SPEED_MIN) * t
    }

    /** How long strand [j]'s crest takes to cross the full width, in seconds. */
    fun crossSeconds(j: Int): Float = 1f / crestSpeed(j)

    /**
     * Strand [j]'s angular rate in RADIANS PER SECOND, solved back from its crest
     * [crestSpeed]: a crest sits where `p·π·freq − dir·phase·ω` is constant, so
     * holding that fixed the crest advances `ω / (π·freq)` widths/sec. Hence
     * ω = speed · π · freq — and because the frequency is bent by the spectrum, ω
     * must be recomputed from the SAME centroid, which is what keeps the crest speed
     * a constant of the calibration rather than something the voice can change. Fed
     * [WaveformFrame.phaseSec] (wall-clock seconds) directly — no frame count — so
     * 60 and 120 Hz are identical.
     */
    fun omega(j: Int, centroid: Float): Float = crestSpeed(j) * PI.toFloat() * frequency(j, centroid)

    /**
     * How far strand [j]'s wave is phase-shifted from its neighbours. Spreading the
     * strands over [PHASE_SPREAD] of a cycle is what fans them into a bundle with
     * vertical THICKNESS (at a given x they sit at different heights) instead of a
     * single overdrawn line, and their differing [frequency]s then carry those
     * offsets in and out of coincidence — the weave. Spread over [STRANDS_FULL] so
     * the pattern is stable whatever the current count.
     */
    val PHASE_SPREAD = PI.toFloat()

    fun phaseOffset(j: Int): Float = PHASE_SPREAD * (j.coerceAtLeast(0).toFloat() / STRANDS_FULL)

    /**
     * How far a full-drive strand runs AHEAD of a silent one, in radians.
     *
     * This is the second place the audio enters the phase, and the one that makes
     * the bundle move as a braid rather than a comb: strands mapped to the loud part
     * of the spectrum lead, strands on the quiet part lag, so the fan opens and
     * closes with the voice. ~0.9 rad is a seventh of a cycle — enough to read as
     * shear against [PHASE_SPREAD], small enough that the bundle never inverts its
     * ordering.
     */
    const val DRIVE_LEAD_RAD = 0.9f

    fun driveLead(drive: Float): Float {
        val d = if (drive.isNaN()) 0f else drive.coerceIn(0f, 1f)
        return DRIVE_LEAD_RAD * d
    }

    /**
     * The SWIRL — a second, slower travelling wave added to the strand's PHASE
     * rather than to its height, with a depth set by the spectral [spread].
     *
     * This is what stops the ribbon being a sine of the clock. Amplitude modulation
     * can only make a fixed shape taller; modulating the phase changes the shape
     * itself, so a sustained vowel (energy in one place, spread ≈ 0) draws a clean
     * arc while a broadband, consonant-heavy passage (spread → 1) makes the same
     * strand writhe. Scaled by the strand's own drive as well, so a silent idle
     * braid is calm.
     *
     * [SWIRL_DEPTH_MAX] × [SWIRL_FREQ] must stay strictly under the LOWEST frequency
     * the centroid tilt can produce (`frequency(0, 0)` = 2.75): above that the total
     * phase stops being monotone in p and the strand folds back over itself — a knot,
     * not a braid. 0.85 × 2.0 = 1.7 leaves a comfortable margin, and a test sweeps
     * the whole parameter space rather than trusting the inequality alone.
     */
    const val SWIRL_DEPTH_MAX = 0.85f
    const val SWIRL_FREQ = 2.0f

    /** Widths/sec the swirl itself travels — deliberately slower than any crest, so it beats against them. */
    const val SWIRL_SPEED = 0.45f

    val SWIRL_OMEGA = SWIRL_SPEED * PI.toFloat() * SWIRL_FREQ

    fun swirlDepth(spread: Float, drive: Float): Float {
        val s = if (spread.isNaN()) 0f else spread.coerceIn(0f, 1f)
        val d = if (drive.isNaN()) 0f else drive.coerceIn(0f, 1f)
        return SWIRL_DEPTH_MAX * s * d
    }

    /**
     * The strand's total phase at [p] — the travelling carrier, the drive lead and
     * the swirl summed. Exposed (rather than folded into [wave]) because the
     * no-folding invariant is a statement about THIS quantity being monotone in p,
     * and a test should be able to measure the thing the invariant is about.
     */
    fun totalPhase(
        p: Float,
        phaseSec: Float,
        dir: Float,
        j: Int,
        centroid: Float,
        spread: Float,
        drive: Float,
    ): Float {
        val phase = if (phaseSec.isNaN()) 0f else phaseSec
        val carrier = p * PI.toFloat() * frequency(j, centroid) - dir * phase * omega(j, centroid)
        val swirl = swirlDepth(spread, drive) *
            sin(p * PI.toFloat() * SWIRL_FREQ - dir * phase * SWIRL_OMEGA + phaseOffset(j))
        return carrier + phaseOffset(j) + driveLead(drive) + swirl
    }

    /**
     * The travelling wave for strand [j], −1..1, at [p] across the width. [dir] is
     * +1 for a bundle running left→right and −1 for right→left (identity, C1); it
     * enters as the SIGN of the time term because that is what a direction of travel
     * is. Every input is absorbed if it is garbage — a NaN [phaseSec] freezes the
     * frame, a NaN spectrum reads as neutral — because this runs in a draw loop.
     */
    fun wave(
        p: Float,
        phaseSec: Float,
        dir: Float,
        j: Int,
        centroid: Float,
        spread: Float,
        drive: Float,
    ): Float = sin(totalPhase(p, phaseSec, dir, j, centroid, spread, drive))

    // ---- amplitude / geometry ----------------------------------------------

    /**
     * Peak excursion either side of the strand's centre, as a fraction of height.
     * 0.40 leaves a clear margin at every shipped size (see [strandY]'s budget): a
     * full-drive single-voice strand reaches 0.40·h from centre, well inside the
     * half box.
     */
    const val AMP_FRACTION = 0.40f

    /**
     * Two-voice amplitude scale. Each bundle is nudged off centre by
     * [OFFSET_FRACTION] and must still fit inside its half: 0.40·0.78 = 0.312, and
     * 0.312 + 0.06 = 0.372 < 0.5, so a full-drive human peak (above centre) stays
     * off the top rail and the AI stays off the bottom, at every height.
     */
    const val TWO_VOICE_AMP_SCALE = 0.78f

    /** Small-surface amplitude scale (on top of the mode scale) — the 40 dp degrade. */
    const val SMALL_AMP_SCALE = 0.80f

    /**
     * Vertical nudge of each bundle off centre in two-voice mode, as a fraction of
     * height. SMALL on purpose: the bundles are meant to braid THROUGH each other,
     * not sit in separate lanes — the human just above centre, the AI just below,
     * free to cross.
     */
    const val OFFSET_FRACTION = 0.06f

    /**
     * Per-strand depth taper on the amplitude, so the strands are not all the same
     * height and the bundle has body. A 3-cycle pattern (1.00, 0.94, 0.88) keeps
     * every value ≤ 1, so it only ever REDUCES amplitude and never breaks the
     * no-clip budget.
     */
    const val DEPTH_STEP = 0.06f

    fun strandDepth(j: Int): Float = 1f - DEPTH_STEP * (j.coerceAtLeast(0) % 3)

    /** The amplitude fraction for the current mode/surface. */
    fun ampFraction(twoVoice: Boolean, small: Boolean): Float {
        var f = AMP_FRACTION
        if (twoVoice) f *= TWO_VOICE_AMP_SCALE
        if (small) f *= SMALL_AMP_SCALE
        return f
    }

    /** Strand [j]'s peak excursion in px: its drive × its depth × the mode fraction. */
    fun ampPx(height: Float, drive: Float, j: Int, twoVoice: Boolean, small: Boolean): Float =
        height * ampFraction(twoVoice, small) * strandDepth(j) * drive.coerceIn(0f, 1f)

    /** The bundle's centre line: dead centre alone; human above / AI below in two-voice mode. */
    fun centreY(speaker: AuroraSpeaker, height: Float, twoVoice: Boolean): Float {
        if (!twoVoice) return height / 2f
        val off = height * OFFSET_FRACTION
        return if (speaker == AuroraSpeaker.HUMAN) height / 2f - off else height / 2f + off
    }

    /**
     * How much of the WIDTH each end taper occupies, and the FLOOR it eases to
     * rather than pinching to zero. A non-zero floor is deliberate: if every strand
     * tapered to exactly 0 at the ends they would all converge on ONE point and
     * pile up N-fold additively (a white tip). Easing to [TAPER_END_FLOOR] instead
     * leaves the strands fanned by their phase offsets at the ends too, so the ends
     * stay as airy as the middle.
     */
    const val TAPER_FRACTION = 0.14f
    const val TAPER_END_FLOOR = 0.18f

    /**
     * 1 through the middle, easing to [TAPER_END_FLOOR] at both edges over
     * [TAPER_FRACTION] of the width (a cosine ease, same shape as Aurora's
     * [edgeTaper] but to a floor). A MULTIPLIER on the excursion.
     */
    fun taper(p: Float): Float {
        val x = p.coerceIn(0f, 1f)
        val d = minOf(x, 1f - x)
        if (d >= TAPER_FRACTION) return 1f
        val ease = 0.5f - 0.5f * cos((PI * d / TAPER_FRACTION).toFloat())
        return TAPER_END_FLOOR + (1f - TAPER_END_FLOOR) * ease
    }

    /**
     * Strand [j]'s screen y at [p]: its [centre] minus (screen y grows downward) the
     * tapered, amplitude-scaled [wave]. Clamped into the box as a GUARANTEE, not a
     * shaper — [AMP_FRACTION] is budgeted so a full-drive peak at any layer/mode
     * lands inside the rails, so the clamp is inert at every shipped size and only
     * guards a degenerate canvas.
     */
    fun strandY(
        p: Float,
        height: Float,
        centre: Float,
        amp: Float,
        phaseSec: Float,
        dir: Float,
        j: Int,
        centroid: Float,
        spread: Float,
        drive: Float,
    ): Float {
        val y = centre - taper(p) * amp * wave(p, phaseSec, dir, j, centroid, spread, drive)
        return y.coerceIn(0f, height)
    }

    // ---- sampling ----------------------------------------------------------

    /**
     * Vertices per strand across the width.
     *
     * The floor is set against the BUSIEST picture the style can draw, not the
     * resting one: a bright voice pushes the top strand to `frequency(6, 1)` ≈ 10.3,
     * i.e. ~5.1 humps, and [SAMPLE_MIN] has to keep that smooth on a narrow surface
     * or a loud sibilant turns the ribbon into a polygon. 40 vertices is ~7.8 per
     * hump at that worst case (pinned by a test), matching what the pre-M7 floor
     * gave at its own worst case. Capped so a wide surface does not draw hundreds of
     * segments per strand per frame.
     */
    const val SAMPLE_PITCH_PX = 10f
    const val SAMPLE_MIN = 40
    const val SAMPLE_MAX = 80

    fun sampleCount(width: Float): Int = (width / SAMPLE_PITCH_PX).toInt().coerceIn(SAMPLE_MIN, SAMPLE_MAX)

    // ---- degrade -----------------------------------------------------------

    /** Below this height (px) the small-surface degrade engages. ~52 dp: the 40 dp bar is inside it. */
    const val SMALL_HEIGHT_DP = 52f

    /** True when the surface is short enough to need the degraded strand count/amplitude. */
    fun isSmall(heightPx: Float, smallHeightPx: Float): Boolean =
        heightPx > 0f && heightPx < smallHeightPx

    // ---- identity ----------------------------------------------------------

    /** Direction of travel = identity (C1): human left→right (+1), AI right→left (−1). */
    fun travelDirection(speaker: AuroraSpeaker): Float =
        if (speaker == AuroraSpeaker.HUMAN) 1f else -1f

    /** Below this a strand is not worth drawing — the player-bar silence floor. */
    const val MIN_VISIBLE_DRIVE = 0.004f
}
