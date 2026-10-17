package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.EnvelopeRing
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import com.aiblackbox.portal.ui.components.wave.WaveformFrame
import com.aiblackbox.portal.ui.components.wave.WaveformRenderer
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.sin

// =============================================================================
// Circular Orb — reference sheet concept 6 ("BOTH"). A radial instrument, not a
// scope: a dark central ORB rimmed by a white-hot glowing RING, short radial
// TICKS bristling off the rim whose lengths ARE the spectrum, and two DOTTED
// trails running left and right from the orb on the centre line whose dots carry
// the envelope HISTORY flowing OUTWARD (newest nearest the orb). It is the only
// shipped style that combines the instantaneous spectrum AND the history ring in
// one look — the ring/ticks read "now", the dot trails read "the last three
// seconds".
//
// THE RIM IS A REAL SPECTRUM. Each spoke reads the loudest of the log-frequency
// BINS it owns (see AuroraSpectrum), the spokes of one side PARTITIONING the bins,
// and the rim is MIRRORED about the horizontal axis so both halves carry the whole
// spectrum: bass at the two poles the dot trails run out from, sibilance at the top
// and bottom of the arc. At the shipped TICK_COUNT that is one bin per spoke — 24
// distinct lengths where the previous version's `i mod BANDS` spread four values
// around the rim twelve times, lively but a lie about where a sound was. Halving
// the spokes on a short bar halves the resolution without dropping a bin.
// A speaker with no spectrum published falls back to the four bands on the SAME
// axis (OrbMath.bandForTick), so wiring a producer in changes the rim's detail, not
// its layout, and no curve is applied to a bin on the way (OrbMath.tickDrive).
//
// WHY A DARK DISC AT ALL. The reference is a bright ring around a dark core, and
// the dark core is what makes the ring read as a RING rather than a filled blob
// of glow. That disc is the ONLY occluding (SrcOver) pass in the whole style —
// everything else is additive light (BlendMode.Plus) that brightens the near-
// black background and never dims the particle field behind it. So the field-
// survives budget reduces to ONE number: the disc's area as a fraction of the
// box. It is a fraction of HEIGHT, so on a wide bar it is a small dot in a long
// strip; the worst case is a perfectly SQUARE container (the large Settings
// preview), and even there it stays well under the ~55% Aurora-v33 ceiling
// ([OrbMath.occlusionFraction], pinned by test at both sizes).
//
// TWO-VOICE CONTRACT (the plan's table, locked): HUMAN = the outer radial ticks
// (red family); AI = the ring pulse itself PLUS a second inner ring (blue
// family); the dot trails split left = human, right = AI. ONE voice drives the
// whole instrument in its own colour (red if human, blue if AI) and both trails
// mirror that single speaker's history. The role→element mapping is pure and
// test-pinned ([OrbMath.tickSpeaker]/[ringSpeaker]/[trailSpeaker]/[innerRingVisible])
// so the invariant "ticks are human, ring is AI" can never silently drift.
//
// COLOUR. Ticks and rings are white-hot at the core with a coloured BLOOM: the
// human family blooms brand red ([BbxAccent]), the AI family blooms brand blue
// ([WaveBlue]). A tick brightens from cool grey/white toward its family's hot
// colour as its drive climbs, so the LOUDEST frequencies are the reddest/bluest
// spikes on the rim — the "hottest red" note on the reference sheet.
//
// MOTION is dt-free by construction: the trails scroll because the shared feed
// decimates history on a wall-clock cadence (identical at 60/120 Hz), and the
// idle ring "breathes" off [WaveformFrame.phaseSec] (a wall-clock phase), never
// off a frame count. As a HISTORY style it OVERRIDES needsFrames so a departed
// voice's dot trail keeps flowing outward until it drains, then the surface parks.
//
// 40 dp DEGRADE ([OrbMath.isSmall]): the ring GROWS to ~80% of the (short) height
// while the ticks SHRINK (half the count, a third of the reach) so the rim + its
// longest tick still land inside the box with a margin — a short player bar never
// mesas or clips. The trails are kept (the plan: "shrink ring + keep dotted side
// lines"), just shorter for the narrower bar.
//
// A later agent replaces ONLY the body below; the public [orbRenderer] factory
// name and its registration lambda never move. Every number and every pure
// decision lives in [OrbMath], JVM-testable with no Compose/Android in sight
// (the aurora tradition); the renderer is all draw, no logic.
// =============================================================================

/**
 * Build a Circular Orb renderer. THE public surface a later maintainer keeps
 * stable: `WaveformStyles` registers this style by calling this function, so its
 * name/shape must not change even if the body does.
 */
internal fun orbRenderer(): WaveformRenderer = OrbRenderer()

private class OrbRenderer : WaveformRenderer {

    /** Reused per-frame scratch for the tick speaker's two channels — the rim reads 48
     *  spokes off them, and a fresh array per frame would be garbage the orb pays for. */
    private val bandScratch = FloatArray(AuroraAnalyser.BANDS)
    private val binScratch = FloatArray(AuroraAnalyser.BINS)

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        val small = OrbMath.isSmall(h, OrbMath.SMALL_HEIGHT_DP * density)
        val cx = w / 2f
        val cy = h / 2f
        val centre = Offset(cx, cy)

        val ringR = OrbMath.ringRadiusPx(h, small)
        val discR = OrbMath.orbDiscRadiusPx(h, small)

        // Which single speaker owns the instrument when only one is active, and
        // whether both are (present OR still draining a trail — a departed voice's
        // history keeps its half of the layout until it has flowed off).
        val feed = frame.feed
        val twoVoice = OrbMath.activeCount(feed) >= 2
        val solo = OrbMath.soloSpeaker(feed)

        // -- 1. The dark orb. The ONE occluding pass; everything after is additive.
        //       Near-black so it barely darkens an already near-black field, but
        //       enough to carve the ring's silhouette. Its coverage is the whole
        //       field-survives budget (pinned < 55% even square).
        drawCircle(
            color = BbxBlack,
            radius = discR,
            center = centre,
            alpha = OrbMath.ORB_DISC_ALPHA,
            blendMode = BlendMode.SrcOver,
        )

        // -- 2. Dot trails (drawn under the rim so the ring's glow sits on top).
        drawTrails(frame, cx, cy, ringR, w, h, small, twoVoice, solo)

        // -- 3. The ring(s). AI (or the solo voice) drives the rim pulse; the inner
        //       blue ring appears only when both voices are live.
        val ringSpk = OrbMath.ringSpeaker(twoVoice, solo)
        val ringLevel = OrbMath.ringLevel(feed, ringSpk, frame.restLevel)
        drawRing(centre, ringR, ringLevel, frame.phaseSec, small, familyOf(ringSpk))

        if (OrbMath.innerRingVisible(twoVoice)) {
            val innerLevel = OrbMath.ringLevel(feed, AuroraSpeaker.AI, frame.restLevel)
            drawRing(
                centre,
                ringR * OrbMath.INNER_RING_FRACTION,
                innerLevel,
                frame.phaseSec,
                small,
                AI_FAMILY,
                inner = true,
            )
        }

        // -- 4. Radial ticks. HUMAN in two-voice; the solo voice otherwise.
        val tickSpk = OrbMath.tickSpeaker(twoVoice, solo)
        drawTicks(feed, centre, ringR, h, small, familyOf(tickSpk), tickSpk, frame.restLevel)
    }

    // ---------------------------------------------------------------- ring

    private fun DrawScope.drawRing(
        centre: Offset,
        radius: Float,
        level: Float,
        phaseSec: Float,
        small: Boolean,
        family: Family,
        inner: Boolean = false,
    ) {
        if (radius <= 0f) return
        val coreW = max(OrbMath.RING_CORE_W_DP * density, radius * OrbMath.RING_MIN_CORE_FRACTION)
        // Breathe the glow width off the wall-clock phase so an idle ring still
        // lives; loudness widens it further. Both bounded so the annulus never
        // fattens into the tick zone.
        val breathe = OrbMath.breathe(phaseSec)
        val glowW = coreW * OrbMath.ringGlowWidthMult(level, breathe, small)
        val glowA = OrbMath.ringGlowAlpha(level, breathe)
        val coreA = if (inner) OrbMath.INNER_RING_CORE_ALPHA else OrbMath.RING_CORE_ALPHA

        // Additive glow halo, then the white-hot core. The core is a hair of the
        // family colour mixed toward white by loudness — a loud ring is whiter-hot.
        val bloom = family.hot
        val core = mix(bloom, BbxWhite, OrbMath.ringWhiteness(level))
        drawCircle(
            color = bloom,
            radius = radius,
            center = centre,
            alpha = glowA,
            style = Stroke(width = glowW),
            blendMode = BlendMode.Plus,
        )
        drawCircle(
            color = core,
            radius = radius,
            center = centre,
            alpha = coreA,
            style = Stroke(width = coreW),
            blendMode = BlendMode.Plus,
        )
    }

    // ---------------------------------------------------------------- ticks

    private fun DrawScope.drawTicks(
        feed: WaveformFeed,
        centre: Offset,
        ringR: Float,
        h: Float,
        small: Boolean,
        family: Family,
        speaker: AuroraSpeaker,
        restLevel: Float,
    ) {
        val count = OrbMath.tickCount(small)
        val gap = OrbMath.TICK_GAP_FRACTION * h
        val coreW = max(OrbMath.TICK_CORE_W_DP * density, h * OrbMath.TICK_MIN_CORE_FRACTION)
        val glowW = coreW * OrbMath.TICK_GLOW_MULT
        // The rim is driven by the SPECTRUM when this speaker publishes one, and by the
        // four bands on the same axis when it does not.
        for (b in bandScratch.indices) bandScratch[b] = feed.band(speaker, b)
        val bins = if (feed.hasSpectrum(speaker)) {
            for (k in binScratch.indices) binScratch[k] = feed.bin(speaker, k)
            binScratch
        } else {
            null
        }
        // The rim honours the caller's rest floor like every other surface in the app —
        // it is the ONLY element of this style that used to ignore it, so a live mic's
        // rim collapsed to nothing between words while the ring beside it kept breathing.
        val restFloor = if (feed.present(speaker)) restLevel else 0f
        for (i in 0 until count) {
            val angle = OrbMath.tickAngle(i, count)
            val drive = OrbMath.tickDrive(bins, bandScratch, i, count, restFloor)
            val len = OrbMath.tickLengthPx(drive, h, small)
            val ux = cos(angle)
            val uy = sin(angle)
            val r0 = ringR + gap
            val r1 = r0 + len
            val p0 = Offset(centre.x + ux * r0, centre.y + uy * r0)
            val p1 = Offset(centre.x + ux * r1, centre.y + uy * r1)

            val heat = OrbMath.tickHeat(drive)
            val col = mix(mix(BbxDim, BbxWhite, OrbMath.tickCoolness(drive)), family.hot, heat)
            // Glow only on the hotter ticks so the additive sum stays low on the
            // quiet flush ticks; the core is always drawn.
            if (heat > OrbMath.TICK_GLOW_GATE) {
                drawLine(
                    color = family.hot,
                    start = p0, end = p1,
                    strokeWidth = glowW,
                    cap = StrokeCap.Round,
                    alpha = OrbMath.TICK_GLOW_ALPHA * heat,
                    blendMode = BlendMode.Plus,
                )
            }
            drawLine(
                color = col,
                start = p0, end = p1,
                strokeWidth = coreW,
                cap = StrokeCap.Round,
                alpha = OrbMath.TICK_CORE_ALPHA,
                blendMode = BlendMode.Plus,
            )
        }
    }

    // ---------------------------------------------------------------- trails

    private fun DrawScope.drawTrails(
        frame: WaveformFrame,
        cx: Float,
        cy: Float,
        ringR: Float,
        w: Float,
        h: Float,
        small: Boolean,
        twoVoice: Boolean,
        solo: AuroraSpeaker,
    ) {
        val spacing = OrbMath.dotSpacingPx(density, small)
        val startGap = OrbMath.DOT_START_GAP_FRACTION * h
        val start = ringR + startGap
        val baseR = OrbMath.DOT_RADIUS_DP * density
        for (leftSide in booleanArrayOf(true, false)) {
            val dir = if (leftSide) -1f else 1f
            val speaker = OrbMath.trailSpeaker(leftSide, twoVoice, solo)
            val ring = frame.feed.history(speaker)
            val present = frame.feed.present(speaker)
            val floor = if (present) frame.restLevel.coerceIn(0f, 1f) else 0f
            val count = OrbMath.dotCount(w, start, spacing)
            val family = familyOf(speaker)
            for (i in 0 until count) {
                // Curved and floor-BLENDED like every other drawn extent in the app: a
                // trail dot's size and brightness both come off this, and read raw the
                // trail was a row of identical specks at any ordinary speaking level.
                val sample = Aurora.restBlend(floor, Aurora.displayCurve(ring.newest(i)))
                val bright = OrbMath.dotBrightness(sample, i, count)
                if (bright < OrbMath.DOT_MIN_VISIBLE) continue
                val x = cx + dir * (start + i * spacing)
                if (x < 0f || x > w) break
                drawCircle(
                    color = family.hot,
                    radius = OrbMath.dotRadiusPx(baseR, sample, i, count),
                    center = Offset(x, cy),
                    alpha = OrbMath.DOT_ALPHA * bright,
                    blendMode = BlendMode.Plus,
                )
            }
        }
    }

    /**
     * A scrolling/history style (the dot trails). The default audible-only park
     * would FREEZE a half-flowed trail the instant a speaker stopped. Keep frames
     * while any present voice is audible OR any history ring still holds energy
     * that has not yet flowed off; park once every ring has drained.
     */
    override fun needsFrames(voices: List<AuroraVoice>, feed: WaveformFeed): Boolean {
        for (i in voices.indices) if (voices[i].hasEnergy()) return true
        for (i in SPEAKERS.indices) {
            if (OrbMath.ringHasEnergy(feed.history(SPEAKERS[i]))) return true
        }
        return false
    }

    // ---------------------------------------------------------------- colour

    private fun familyOf(speaker: AuroraSpeaker): Family =
        if (speaker == AuroraSpeaker.HUMAN) HUMAN_FAMILY else AI_FAMILY

    /** Straight channel-wise lerp; Color is Compose so it stays out of [OrbMath]. */
    private fun mix(a: Color, b: Color, t: Float): Color {
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

        /** The human family blooms brand red; the AI family blooms brand blue. */
        private val HUMAN_FAMILY = Family(BbxAccent)
        private val AI_FAMILY = Family(WaveBlue)
    }

    /** A speaker's "hot" bloom colour — the tint white-hot marks brighten toward. */
    private class Family(val hot: Color)
}

/**
 * All of Circular Orb's geometry, budget arithmetic and role→element DECISIONS as
 * pure functions of their inputs — no Compose, no android.*, so every number the
 * look depends on is pinned by [CircularOrbTest] on the JVM, exactly as the aurora
 * math is. The renderer above reads these and paints; it holds no numbers of its
 * own.
 */
internal object OrbMath {

    // ---- ring / disc geometry ---------------------------------------------

    /**
     * Ring radius as a fraction of height. 0.35 → a 0.70·h DIAMETER, the reference
     * sheet's "~70% of height". On a short bar the degrade GROWS it to 0.40 (a
     * 0.80·h diameter — the reference sheet's "orb ~80% of height") because the
     * ticks shrink out of the way there, so the ring can claim the freed room.
     */
    const val RING_RADIUS_FRACTION = 0.35f
    const val RING_RADIUS_FRACTION_SMALL = 0.40f

    /**
     * The dark disc's radius as a fraction of the RING radius. 0.90 keeps the disc
     * just inside the ring so the bright rim reads as a rim, not a filled glow, and
     * shaves the occluding area a further ~19% under the ring circle for budget
     * headroom.
     */
    const val ORB_DISC_FRACTION_OF_RING = 0.90f

    /** The inner (AI, two-voice) ring as a fraction of the outer ring radius. */
    const val INNER_RING_FRACTION = 0.66f

    /** Dark-disc fill alpha (SrcOver). Near-black on near-black: carves, barely dims. */
    const val ORB_DISC_ALPHA = 0.72f

    fun ringRadiusPx(heightPx: Float, small: Boolean): Float =
        heightPx * (if (small) RING_RADIUS_FRACTION_SMALL else RING_RADIUS_FRACTION)

    fun orbDiscRadiusPx(heightPx: Float, small: Boolean): Float =
        ringRadiusPx(heightPx, small) * ORB_DISC_FRACTION_OF_RING

    /**
     * The dark disc's coverage of the box, 0..1 — the ONE field-survives budget,
     * since every other pass is additive. π·r² over w·h. The disc radius is a
     * fraction of HEIGHT, so a wide bar is a tiny dot; the worst case is a SQUARE
     * box (w == h), which is what the test pins under the ~0.55 ceiling.
     */
    fun occlusionFraction(widthPx: Float, heightPx: Float, small: Boolean): Float {
        if (widthPx <= 0f || heightPx <= 0f) return 0f
        val r = orbDiscRadiusPx(heightPx, small)
        return (Math.PI.toFloat() * r * r) / (widthPx * heightPx)
    }

    // ---- ring glow / colour -----------------------------------------------

    /** Core rim stroke, dp. Bright and thin — the white-hot line of the rim. */
    const val RING_CORE_W_DP = 2.0f

    /** Floor on core width as a fraction of ring radius, so a tiny orb keeps a rim. */
    const val RING_MIN_CORE_FRACTION = 0.035f

    /** Outer-ring core alpha (additive Plus). Bright by design — the white-hot ring. */
    const val RING_CORE_ALPHA = 0.80f

    /** Inner (AI, blue) ring core alpha — a touch softer so it reads as secondary. */
    const val INNER_RING_CORE_ALPHA = 0.60f

    /** Glow-halo alpha at idle (min) and at full loudness (max). Kept low: additive. */
    const val RING_GLOW_ALPHA_MIN = 0.10f
    const val RING_GLOW_ALPHA_MAX = 0.30f

    /** Glow-halo width as a multiple of the core, at idle and full loudness. */
    const val RING_GLOW_MULT_MIN = 3.0f
    const val RING_GLOW_MULT_MAX = 6.0f

    /** Small-surface glow-multiplier scale, so a 40 dp bar's halo cannot fatten out. */
    const val RING_GLOW_MULT_SMALL_SCALE = 0.6f

    /** How much idle breathing modulates the glow (a hair — the ring lives, it does not throb). */
    const val BREATHE_DEPTH = 0.25f

    /** Idle breathe rate, radians of wall-clock phase per second. Slow. */
    const val BREATHE_RATE = 1.4f

    /** How much loudness whitens the ring core toward pure white (0 = family colour). */
    const val RING_WHITEN = 0.7f

    /** Breathing factor in 0..1 off the shared wall-clock phase — dt/refresh independent. */
    fun breathe(phaseSec: Float): Float {
        val p = if (phaseSec.isNaN()) 0f else phaseSec
        return 0.5f + 0.5f * sin(p * BREATHE_RATE)
    }

    /** Ring glow alpha, bounded [MIN..MAX], lifted by loudness and gently by the breathe. */
    fun ringGlowAlpha(level: Float, breathe: Float): Float {
        val l = safe01(level)
        val b = breathe.coerceIn(0f, 1f)
        val lifted = l + BREATHE_DEPTH * (b - 0.5f) * (1f - l) // breathe fades out as it gets loud
        val a = RING_GLOW_ALPHA_MIN + lifted.coerceIn(0f, 1f) * (RING_GLOW_ALPHA_MAX - RING_GLOW_ALPHA_MIN)
        return a.coerceIn(RING_GLOW_ALPHA_MIN, RING_GLOW_ALPHA_MAX)
    }

    /** Ring glow WIDTH multiple, bounded, lifted by loudness + breathe, trimmed when small. */
    fun ringGlowWidthMult(level: Float, breathe: Float, small: Boolean): Float {
        val l = safe01(level)
        val b = (breathe.coerceIn(0f, 1f) - 0.5f) * BREATHE_DEPTH
        val t = (l + b).coerceIn(0f, 1f)
        var m = RING_GLOW_MULT_MIN + t * (RING_GLOW_MULT_MAX - RING_GLOW_MULT_MIN)
        if (small) m *= RING_GLOW_MULT_SMALL_SCALE
        return m
    }

    /** How white-hot the ring core is, 0..1 — loud rings whiten toward pure white. */
    fun ringWhiteness(level: Float): Float = (safe01(level) * RING_WHITEN).coerceIn(0f, 1f)

    /** NaN/out-of-range → a usable 0..1 (coerceIn alone lets NaN through, JLS 15.20.1). */
    private fun safe01(v: Float): Float = if (v.isNaN()) 0f else v.coerceIn(0f, 1f)

    /**
     * The ring's driving level: the speaker's overall level, floored by the
     * caller's restLevel while the speaker is PRESENT so a paused player bar still
     * shows a faint glowing rim rather than a dead circle. Absent → 0.
     */
    fun ringLevel(feed: WaveformFeed, speaker: AuroraSpeaker, restLevel: Float): Float {
        val floor = if (feed.present(speaker)) restLevel.coerceIn(0f, 1f) else 0f
        return Aurora.restBlend(floor, Aurora.displayCurve(feed.level(speaker)))
    }

    // ---- ticks -------------------------------------------------------------

    /** Tick count at full size (in the reference sheet's 40–60 band) and halved when small. */
    const val TICK_COUNT = 48
    const val TICK_COUNT_SMALL = 24

    /** Gap between the rim and a tick's inner end, fraction of height — ticks float off the ring. */
    const val TICK_GAP_FRACTION = 0.010f

    /** Idle (flush) tick length and the extra reach at full band drive, fractions of height. */
    const val TICK_BASE_FRACTION = 0.015f
    const val TICK_SPAN_FRACTION = 0.090f

    /** Small-surface tick base/reach — a third of the reach so the short bar never clips. */
    const val TICK_BASE_FRACTION_SMALL = 0.010f
    const val TICK_SPAN_FRACTION_SMALL = 0.045f

    /** Tick core weight (dp) and its floor as a fraction of height. */
    const val TICK_CORE_W_DP = 1.6f
    const val TICK_MIN_CORE_FRACTION = 0.012f

    /** Tick glow-pass width multiple and its alpha (only on hot ticks). */
    const val TICK_GLOW_MULT = 2.4f
    const val TICK_GLOW_ALPHA = 0.20f

    /** Tick core alpha (additive). */
    const val TICK_CORE_ALPHA = 0.80f

    /** Above this heat a tick gets a glow pass; below it, core only (keeps quiet ticks cheap/dim). */
    const val TICK_GLOW_GATE = 0.15f

    fun tickCount(small: Boolean): Int = if (small) TICK_COUNT_SMALL else TICK_COUNT

    /** Angle of tick [i] of [count], radians, evenly spaced from 0. */
    fun tickAngle(i: Int, count: Int): Float {
        val n = count.coerceAtLeast(1)
        return (2.0 * Math.PI).toFloat() * (i.toFloat() / n)
    }

    /**
     * How many spokes are on ONE side of the rim's mirror — half the count, at least
     * one. The rim is symmetric about the horizontal axis (the axis the dot trails run
     * along), so each side carries the whole spectrum and the two halves agree.
     */
    fun sideCount(count: Int): Int = (count.coerceAtLeast(1) / 2).coerceAtLeast(1)

    /**
     * Tick [i]'s position along ONE side, 0 at the rim's right-hand pole (angle 0) and
     * [sideCount] - 1 at its left. Ticks reflected across the horizontal axis share a
     * position, which is what makes the rim a mirrored pair of spectra rather than one
     * wrapped one — a wrap would put sibilance hard against bass at the seam.
     */
    fun sidePosition(i: Int, count: Int): Int {
        val n = count.coerceAtLeast(1)
        val c = Math.floorMod(i, n)
        return minOf(c, n - 1 - c).coerceIn(0, sideCount(count) - 1)
    }

    /**
     * First spectrum bin read by tick [i]. The spokes of one side PARTITION the bins
     * (`floor` on both edges, so one spoke's end is the next one's start): at the full
     * [TICK_COUNT] that is exactly one bin per spoke, and at [TICK_COUNT_SMALL] two —
     * halving the spokes halves the RESOLUTION without dropping a bin, so a peak can
     * never become invisible just because the surface is a player bar.
     */
    fun binRangeStart(i: Int, count: Int): Int {
        val side = sideCount(count)
        val p = sidePosition(i, count)
        return (p.toLong() * AuroraAnalyser.BINS / side).toInt()
            .coerceIn(0, AuroraAnalyser.BINS - 1)
    }

    /** One past the last bin read by tick [i]; never empty (see [binRangeStart]). */
    fun binRangeEnd(i: Int, count: Int): Int {
        val side = sideCount(count)
        val p = sidePosition(i, count)
        val end = ((p + 1).toLong() * AuroraAnalyser.BINS / side).toInt()
        return end.coerceIn(binRangeStart(i, count) + 1, AuroraAnalyser.BINS)
    }

    /**
     * Which of the four bands drives tick [i] when no spectrum is published — the SAME
     * frequency layout as [binRangeStart], at the band channel's own resolution (a
     * quarter of each side's spokes per band). It used to be `i mod BANDS`, which spread
     * four values around the rim twelve times over: lively, and a lie about where a
     * sound was. Turning the spectrum on now changes the rim's RESOLUTION, not the place
     * a sound appears on it.
     */
    fun bandForTick(i: Int, count: Int): Int {
        val side = sideCount(count)
        val p = sidePosition(i, count)
        return (p.toLong() * AuroraAnalyser.BANDS / side).toInt()
            .coerceIn(0, AuroraAnalyser.BANDS - 1)
    }

    /**
     * Tick [i]'s drive, 0..1 and NaN-safe on every input: the LOUDEST real bin the spoke
     * covers when [bins] is published, else the band covering the same slice of the
     * frequency axis. Max rather than mean for the reason the segmented EQ gives — a
     * narrow formant is the interesting part, and averaging it flat is what makes a
     * spectrum display look dead.
     *
     * THE ONE CURVE AND THE ONE FLOOR, both taken from Aurora and neither restated
     * here — [Aurora.displayCurve] then [Aurora.restBlend]. This function used to
     * return the raw measurement with NO curve and NO floor at all, which made the rim
     * the worst-served surface in the app on two counts at once: a spoke stood at the
     * band's or bin's face value (measured 0.03 and 0.01 on the upper bands, and
     * 0.0001–0.0006 per raw bin), i.e. a flush stub that never grew, and between words
     * it collapsed to the flush base while every other style kept a resting presence.
     *
     * The curve is the app's single one — a second copy here is exactly the drift
     * WaveformPerceptualCurveTest exists to catch — and [restFloor] is the caller's
     * rest level, already 0 for an absent speaker and for the player bar, where the
     * blend is the identity and file silence still draws a bare rim.
     */
    fun tickDrive(
        bins: FloatArray?,
        bands: FloatArray,
        i: Int,
        count: Int,
        restFloor: Float = 0f,
    ): Float {
        val raw = if (bins == null) {
            valueOf(bands, bandForTick(i, count))
        } else {
            var m = 0f
            val lo = binRangeStart(i, count)
            val hi = binRangeEnd(i, count)
            for (k in lo until hi) {
                val v = valueOf(bins, k)
                if (v > m) m = v
            }
            m
        }
        return Aurora.restBlend(restFloor, Aurora.displayCurve(raw))
    }

    /** Sanitise one array read: out-of-range index or NaN → 0, else clamp 0..1. */
    private fun valueOf(values: FloatArray, index: Int): Float {
        if (index < 0 || index >= values.size) return 0f
        val v = values[index]
        return if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
    }

    /** A tick's length in px: flush base + drive·span, per size. Always ≥ 0. */
    fun tickLengthPx(drive: Float, heightPx: Float, small: Boolean): Float {
        val d = if (drive.isNaN()) 0f else drive.coerceIn(0f, 1f)
        val base = (if (small) TICK_BASE_FRACTION_SMALL else TICK_BASE_FRACTION) * heightPx
        val span = (if (small) TICK_SPAN_FRACTION_SMALL else TICK_SPAN_FRACTION) * heightPx
        return base + d * span
    }

    /**
     * A tick's outer extent from centre in px at drive [drive] — ring + gap + length.
     * The no-clip / no-mesa invariant is that this stays < height/2 even at full
     * drive, for BOTH sizes (pinned by test).
     */
    fun tickOuterExtentPx(drive: Float, heightPx: Float, small: Boolean): Float =
        ringRadiusPx(heightPx, small) + TICK_GAP_FRACTION * heightPx + tickLengthPx(drive, heightPx, small)

    /** How hot (family-coloured) a tick is, 0..1 — drives its bloom toward the hot colour. */
    fun tickHeat(drive: Float): Float = if (drive.isNaN()) 0f else drive.coerceIn(0f, 1f)

    /** How bright a COOL tick is, grey→white before it starts turning hot. */
    fun tickCoolness(drive: Float): Float =
        if (drive.isNaN()) 0f else (drive * 1.4f).coerceIn(0f, 1f)

    // ---- dotted trails -----------------------------------------------------

    /** Dot spacing (dp) along a trail — the reference sheet's ~4 dp. Wider when small (fewer, clearer). */
    const val DOT_SPACING_DP = 4.0f
    const val DOT_SPACING_DP_SMALL = 5.0f

    /** Base dot radius (dp). Tiny — trails are texture, never mass; keeps additive sum negligible. */
    const val DOT_RADIUS_DP = 1.4f

    /** Where a trail begins beyond the rim, fraction of height. */
    const val DOT_START_GAP_FRACTION = 0.030f

    /** Hard cap on dots per side, so a very wide bar cannot spawn thousands of draws. */
    const val DOT_MAX_COUNT = 48

    /** Trail base alpha (additive), scaled by per-dot brightness. */
    const val DOT_ALPHA = 0.75f

    /** Idle brightness floor so a silent trail is a faint dotted line, not nothing. */
    const val DOT_FLOOR = 0.12f

    /** Below this a dot is skipped (invisible) — saves the draw. */
    const val DOT_MIN_VISIBLE = 0.02f

    fun dotSpacingPx(density: Float, small: Boolean): Float =
        (if (small) DOT_SPACING_DP_SMALL else DOT_SPACING_DP) * density

    /**
     * How many dots fit in one trail: the room from the trail start to the box edge
     * divided by the spacing, capped at [DOT_MAX_COUNT]. Never negative.
     */
    fun dotCount(widthPx: Float, startPx: Float, spacingPx: Float): Int {
        if (spacingPx <= 0f) return 0
        val room = widthPx / 2f - startPx
        if (room <= 0f) return 0
        return (room / spacingPx).toInt().coerceIn(0, DOT_MAX_COUNT)
    }

    /**
     * Distance falloff for dot [i] of [count], 1 nearest the orb → 0 at the far end.
     * Squared so the trail fades gently near the orb and quickly at the tail — the
     * "brightness falls off with distance" of the spec.
     */
    fun dotDistanceFalloff(i: Int, count: Int): Float {
        if (count <= 1) return 1f
        val t = 1f - i.toFloat() / count
        return (t * t).coerceIn(0f, 1f)
    }

    /**
     * A dot's brightness 0..1: its distance falloff times its history sample lifted
     * off the idle [DOT_FLOOR]. Newest sample (i = 0) is nearest the orb and
     * brightest; the same sample dims as it flows outward frame by frame.
     */
    fun dotBrightness(sample: Float, i: Int, count: Int): Float {
        val s = if (sample.isNaN()) 0f else sample.coerceIn(0f, 1f)
        val lit = DOT_FLOOR + (1f - DOT_FLOOR) * s
        return (dotDistanceFalloff(i, count) * lit).coerceIn(0f, 1f)
    }

    /** A dot's radius px: base, shrinking with distance and swelling a little with loudness. */
    fun dotRadiusPx(baseRadiusPx: Float, sample: Float, i: Int, count: Int): Float {
        val s = if (sample.isNaN()) 0f else sample.coerceIn(0f, 1f)
        val f = dotDistanceFalloff(i, count)
        return (baseRadiusPx * (0.45f + 0.55f * f) * (0.7f + 0.3f * s)).coerceAtLeast(0f)
    }

    // ---- role → element decisions (the two-voice contract) -----------------

    /** Ticks belong to HUMAN when both voices are live; to the solo voice otherwise. */
    fun tickSpeaker(twoVoice: Boolean, solo: AuroraSpeaker): AuroraSpeaker =
        if (twoVoice) AuroraSpeaker.HUMAN else solo

    /** The rim pulse belongs to AI when both are live; to the solo voice otherwise. */
    fun ringSpeaker(twoVoice: Boolean, solo: AuroraSpeaker): AuroraSpeaker =
        if (twoVoice) AuroraSpeaker.AI else solo

    /** The inner (blue, AI) ring shows ONLY when both voices are live. */
    fun innerRingVisible(twoVoice: Boolean): Boolean = twoVoice

    /**
     * Which speaker's history a trail carries: left = HUMAN, right = AI when both
     * are live; the solo voice on BOTH sides (a symmetric single-voice trail)
     * otherwise.
     */
    fun trailSpeaker(isLeft: Boolean, twoVoice: Boolean, solo: AuroraSpeaker): AuroraSpeaker {
        if (!twoVoice) return solo
        return if (isLeft) AuroraSpeaker.HUMAN else AuroraSpeaker.AI
    }

    // ---- activity / idle park ---------------------------------------------

    /** Loudness above which a history sample counts as "still flowing" for the park. */
    const val REST_EPS = 0.02f

    /** Below this height (px) the small-surface degrade engages. ~52 dp: the 40 dp bar is well inside. */
    const val SMALL_HEIGHT_DP = 52f

    fun isSmall(heightPx: Float, smallHeightPx: Float): Boolean =
        heightPx > 0f && heightPx < smallHeightPx

    /** True while any visible sample in [ring] is above [REST_EPS] — the trail is still flowing. */
    fun ringHasEnergy(ring: EnvelopeRing): Boolean {
        val n = ring.size
        for (i in 0 until n) if (ring.newest(i) > REST_EPS) return true
        return false
    }

    /** A speaker is "active" if present OR its trail is still flowing off. */
    fun isActive(feed: WaveformFeed, speaker: AuroraSpeaker): Boolean =
        feed.present(speaker) || ringHasEnergy(feed.history(speaker))

    /** How many speakers are active — ≥ 2 ⇒ two-voice layout. */
    fun activeCount(feed: WaveformFeed): Int {
        var n = 0
        for (i in SPEAKERS.indices) if (isActive(feed, SPEAKERS[i])) n++
        return n
    }

    /**
     * The single owning speaker when only one is active. When neither or both are,
     * HUMAN is the harmless default (its own draws are gated on presence/energy, so
     * a truly-silent frame paints nothing either way).
     */
    fun soloSpeaker(feed: WaveformFeed): AuroraSpeaker {
        val human = isActive(feed, AuroraSpeaker.HUMAN)
        val ai = isActive(feed, AuroraSpeaker.AI)
        return if (ai && !human) AuroraSpeaker.AI else AuroraSpeaker.HUMAN
    }

    private val SPEAKERS = AuroraSpeaker.entries
}
