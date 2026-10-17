package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.EnvelopeRing
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import com.aiblackbox.portal.ui.components.wave.WaveformFrame
import com.aiblackbox.portal.ui.components.wave.WaveformRenderer
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.WaveBlue

// =============================================================================
// Mirrored Stereo Pulse — reference sheet concept #2 (tagged MIC INPUT).
//
// Dense THIN vertical bars across the full width, each mirrored symmetrically
// about the horizontal centre line — a bar grows UP and DOWN equally. The bars
// are the speaker's ENVELOPE HISTORY, newest flush to the right and scrolling
// left; a thin RED core row runs along the centre line, the "red centre band"
// from Brandon's sheet, sitting at the base of every bar. White bars, louder =
// taller AND slightly brighter, quiet = short stubble near the centre that is
// alive but small.
//
// This is a SCROLLING style, so its input is the per-speaker HISTORY RING
// ([WaveformFeed.history]), never the instantaneous bands — column c is a past
// level sample, and the whole strip marches left as [WaveformFeed] pushes new
// samples at its fixed 50 Hz cadence. That cadence is decimated off wall-clock
// time, NOT frame count, so the scroll speed is identical at 60 and 120 Hz by
// construction (the feed's job, inherited here for free) — this renderer adds no
// phase of its own, so its picture is a pure function of the ring contents and
// therefore refresh-rate independent with nothing more to prove.
//
// THE X AXIS IS TIME, AND STAYS TIME. That is this style's identity — it is a VU
// strip, not a spectrum analyser, and the one thing no change here may take away.
// So the SPECTRUM is read INSIDE the bar instead: each column's bar is stacked out
// of one stripe per analyser band, low nearest the centre line and air at the tip,
// each stripe as tall a share of the bar as its band's share of that instant's
// energy and lit by that band's own loudness. The bar's HEIGHT is still purely the
// level ring, so the silhouette scrolling left is exactly the one that shipped;
// what changed is that a rumble now fills it from the middle while a sibilant
// pushes the light out to the tip, which is what an old stereo VU display does and
// what a single scalar could never show. The stripes come from the feed's PER-BAND
// history rings, indexed by the same column as the level ring (both are pushed in
// one feed loop, so a stripe and the height it divides describe one instant).
//
// THE TWO-VOICE CONTRACT (plan's table: "human bars grow UP from centre, AI bars
// grow DOWN"). The MIRROR is the speaker split:
//   - both speakers active  → human owns the UP half (red core), AI owns the
//     DOWN half (blue core). The symmetric strip splits cleanly at the centre
//     line, each half its own voice's history — the mirror carries the identity.
//   - one speaker active     → it takes BOTH halves (full mirror), coloured by
//     its identity (red = human, blue = AI). This is the ordinary single-voice
//     case and the shape the reference sheet draws.
// "Active" is present-OR-ring-still-holds-motion, so a voice that just stopped
// keeps scrolling its tail off the left before the other voice reclaims the full
// mirror — see [needsFrames] and [MirroredPulse.speakerActive].
//
// VISUAL BUDGET (inherited from Aurora v33, a REQUIREMENT not a suggestion):
//   - OCCLUSION is ZERO. Every pass is BlendMode.Plus (additive light), so the
//     particle field behind is never dimmed, only added to — the ≤55% SrcOver
//     coverage ceiling is met trivially because there are no SrcOver passes.
//   - WHITE-OUT is bounded. Bars never overlap each other horizontally (disjoint
//     columns) and the band stripes inside one bar are disjoint VERTICALLY, so at
//     any pixel at most ONE stripe plus its own core stack: the worst additive sum
//     is [MirroredPulse.BODY_ALPHA_MAX] + [MirroredPulse.CORE_ALPHA] = 1.52, under
//     Aurora's 1.6 white-out ceiling — the stripes cost nothing here because they
//     tile the bar rather than layering over it.
//   - NO MESA at 40 dp. Every dimension is height-relative and the amplitude is
//     budgeted to [MirroredPulse.AMP_FRACTION] of the available half so a
//     full-drive bar never reaches a rail. The 40 dp player bar gets a defined
//     small-height degrade: FEWER, chunkier bars (a wider gap) and SHORTER bars
//     (an amplitude scale) so it reads as texture, not slabs — "same language",
//     as the sheet asks.
//   - NaN-safe: the feed sanitises every ring sample on the way in, and the pure
//     geometry guards NaN explicitly (coerceIn does not catch it — JLS 15.20.1).
//
// The maths lives in [MirroredPulse] as pure functions so the geometry, budgets
// and degrade are JVM-testable without a device; this renderer is only a
// traversal of them. See MirroredPulseTest.
// =============================================================================

/**
 * Build a Mirrored Stereo Pulse renderer. THE public surface the registry keeps
 * stable: WaveformStyles registers this style by calling [mirroredPulseRenderer],
 * so the function name/shape must not change even when the body does.
 */
internal fun mirroredPulseRenderer(): WaveformRenderer = MirroredPulseRenderer()

private class MirroredPulseRenderer : WaveformRenderer {

    /**
     * The current column's four band levels, refilled per column and consumed
     * immediately. A field rather than a local so the per-column spectral read
     * allocates NOTHING inside the draw loop (a renderer is long-lived by contract,
     * exactly so it can hold scratch like this).
     */
    private val bands = FloatArray(AuroraAnalyser.BANDS)

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        val feed = frame.feed
        val oneDp = 1.dp.toPx()

        // Small-height degrade: below SMALL_HEIGHT_DP (the 40 dp player bar) the
        // bars go fewer + chunkier + shorter. The 52 dp composer strip and the
        // 140 dp voice screen keep the dense language.
        val small = MirroredPulse.isSmallHeight(h, MirroredPulse.SMALL_HEIGHT_DP * oneDp)
        val pitch = MirroredPulse.pitchPx(oneDp, small)
        if (pitch <= 0f) return
        val barW = MirroredPulse.barWidthPx(oneDp)
        val count = MirroredPulse.columnCount(w, pitch)
        if (count <= 0) return

        val inset = MirroredPulse.railInsetPx(h, MirroredPulse.INSET_MIN_DP * oneDp)
        val availHalf = MirroredPulse.availableHalfPx(h, inset)
        val degrade = MirroredPulse.amplitudeDegrade(small)
        val coreHalf = MirroredPulse.coreHalfPx(h, MirroredPulse.CORE_MIN_DP * oneDp, availHalf)
        val centre = h / 2f
        // Anchor the NEWEST column flush to the RIGHT edge; any sub-pitch remainder
        // sits as a hair of empty space on the LEFT (the oldest, already-faded end),
        // which is where a leftover is least visible.
        val originX = w - count * pitch

        val ringH = feed.history(AuroraSpeaker.HUMAN)
        val ringA = feed.history(AuroraSpeaker.AI)
        val presentH = feed.present(AuroraSpeaker.HUMAN)
        val presentA = feed.present(AuroraSpeaker.AI)

        // "Active" is present OR ring-still-scrolling, so a departed voice keeps
        // draining its tail before the survivor reclaims the full mirror.
        val humanActive = MirroredPulse.speakerActive(presentH, ringH)
        val aiActive = MirroredPulse.speakerActive(presentA, ringA)
        if (!humanActive && !aiActive) return
        val twoVoice = humanActive && aiActive

        for (c in 0 until count) {
            // Centre the thin bar inside its pitch cell (a hair of gap either side).
            val barX = originX + c * pitch + (pitch - barW) / 2f

            if (twoVoice) {
                // Split: human UP (red core), AI DOWN (blue core) — the mirror
                // becomes the speaker split.
                val lh = MirroredPulse.columnLevel(ringH, presentH, frame.restLevel, c, count)
                readBands(feed, AuroraSpeaker.HUMAN, c, count)
                drawSide(barX, barW, centre, availHalf, coreHalf, degrade, lh, up = true, coreColor = BbxAccent)
                val la = MirroredPulse.columnLevel(ringA, presentA, frame.restLevel, c, count)
                readBands(feed, AuroraSpeaker.AI, c, count)
                drawSide(barX, barW, centre, availHalf, coreHalf, degrade, la, up = false, coreColor = WaveBlue)
            } else if (humanActive) {
                // Full mirror, human identity (red core), both halves.
                val l = MirroredPulse.columnLevel(ringH, presentH, frame.restLevel, c, count)
                readBands(feed, AuroraSpeaker.HUMAN, c, count)
                drawSide(barX, barW, centre, availHalf, coreHalf, degrade, l, up = true, coreColor = BbxAccent)
                drawSide(barX, barW, centre, availHalf, coreHalf, degrade, l, up = false, coreColor = BbxAccent)
            } else {
                // Full mirror, AI identity (blue core), both halves.
                val l = MirroredPulse.columnLevel(ringA, presentA, frame.restLevel, c, count)
                readBands(feed, AuroraSpeaker.AI, c, count)
                drawSide(barX, barW, centre, availHalf, coreHalf, degrade, l, up = true, coreColor = WaveBlue)
                drawSide(barX, barW, centre, availHalf, coreHalf, degrade, l, up = false, coreColor = WaveBlue)
            }
        }
    }

    /** Fill [bands] with column [column]'s four band levels for [speaker]. Indexed, no allocation. */
    private fun readBands(feed: WaveformFeed, speaker: AuroraSpeaker, column: Int, count: Int) {
        for (b in 0 until AuroraAnalyser.BANDS) {
            bands[b] = MirroredPulse.columnBand(feed.bandHistory(speaker, b), column, count)
        }
    }

    /**
     * Paint ONE side of one column: the white bar body — stacked as one stripe per
     * band, low at the centre line and air at the tip — and the coloured core
     * segment at its base. Both additive (BlendMode.Plus) — light, never paint, so
     * the field behind survives.
     *
     * Nothing is drawn where the level is below [MirroredPulse.CORE_DRAW_EPS]: a
     * truly silent column (the player bar rests at `restLevel = 0`, so its file
     * silence floors to 0) leaves the strip clear there rather than parking a
     * permanent coloured rule across a blank bar — while a live mic
     * (`restLevel > 0`) floors every column to a low stubble, so its whole strip
     * keeps a breathing centre band. That is the reference's "red centre band"
     * following the waveform on the player bar and glowing continuously on the mic.
     */
    private fun DrawScope.drawSide(
        barX: Float,
        barW: Float,
        centre: Float,
        availHalf: Float,
        coreHalf: Float,
        degrade: Float,
        level: Float,
        up: Boolean,
        coreColor: Color,
    ) {
        if (level <= MirroredPulse.CORE_DRAW_EPS) return

        val ext = MirroredPulse.barHalfExtentPx(availHalf, level, degrade)
        if (ext > MirroredPulse.MIN_BAR_PX) {
            // The bar's HEIGHT is `ext` (the level's) whatever the spectrum does; the
            // stripes only divide it up. A stripe thinner than a bar is worth is
            // skipped, which is also what keeps a one-band column at the shipped draw
            // cost instead of four rects for three hairlines.
            for (seg in 0 until MirroredPulse.SEGMENTS) {
                val from = MirroredPulse.segmentStartPx(ext, bands[0], bands[1], bands[2], bands[3], seg)
                val to = MirroredPulse.segmentEndPx(ext, bands[0], bands[1], bands[2], bands[3], seg)
                val thickness = to - from
                if (thickness <= MirroredPulse.MIN_BAR_PX) continue
                // `from`/`to` are distances OUTWARD from the centre line, so the up
                // side mirrors them and the down side takes them as they are.
                val stripeTop = if (up) centre - to else centre + from
                drawRect(
                    color = BbxWhite,
                    topLeft = Offset(barX, stripeTop),
                    size = Size(barW, thickness),
                    alpha = MirroredPulse.segmentAlpha(bands[seg]),
                    blendMode = BlendMode.Plus,
                )
            }
        }
        // The core straddles the centre line on this side, at the bar's base.
        val coreTop = if (up) centre - coreHalf else centre
        drawRect(
            color = coreColor,
            topLeft = Offset(barX, coreTop),
            size = Size(barW, coreHalf),
            alpha = MirroredPulse.CORE_ALPHA,
            blendMode = BlendMode.Plus,
        )
    }

    /**
     * Idle-park gate — override REQUIRED for a scrolling style (see
     * [WaveformRenderer.needsFrames]). Keep frames while any present voice is
     * audible (the default) AND while either speaker's history ring still holds
     * motion that has not scrolled off, or a half-finished scroll would freeze the
     * instant the speaker stopped instead of draining away over the ring's window.
     */
    override fun needsFrames(voices: List<AuroraVoice>, feed: WaveformFeed): Boolean {
        for (i in voices.indices) if (voices[i].hasEnergy()) return true
        if (MirroredPulse.ringHasMotion(feed.history(AuroraSpeaker.HUMAN))) return true
        if (MirroredPulse.ringHasMotion(feed.history(AuroraSpeaker.AI))) return true
        return false
    }
}

// =============================================================================
// Geometry + budgets — pure, no Compose draw calls, unit-testable on the JVM.
// =============================================================================

internal object MirroredPulse {

    // ------------------------------------------------------------- bar layout

    /**
     * Bar width, dp. The reference sheet's THIN bars (~2 dp). Constant across
     * heights — thinness is the look; density is traded via the GAP instead so a
     * 40 dp bar keeps the same 2 dp bar rather than shrinking it to a hairline.
     */
    const val BAR_WIDTH_DP = 2.0f

    /** Gap between bars, dp. ~1.5 dp → a 3.5 dp pitch, the sheet's dense stripe. */
    const val BAR_GAP_DP = 1.5f

    /**
     * Gap between bars at small height, dp. WIDER (4 dp → 6 dp pitch) so the 40 dp
     * player bar draws FEWER, chunkier bars — "same language", not a shrunken copy
     * of the dense strip that would alias into a grey smear at that size.
     */
    const val BAR_GAP_DP_SMALL = 4.0f

    /**
     * Below this height (dp) the small-height degrade engages. Set at 48 dp so the
     * 40 dp player bar goes compact while the 52 dp composer strip and 140 dp voice
     * screen keep the full dense language.
     */
    const val SMALL_HEIGHT_DP = 48f

    /** A bar body thinner than this (px) is not worth a draw call — its core carries it. */
    const val MIN_BAR_PX = 0.5f

    fun isSmallHeight(heightPx: Float, smallHeightPx: Float): Boolean = heightPx < smallHeightPx

    fun pitchPx(oneDpPx: Float, small: Boolean): Float =
        (BAR_WIDTH_DP + if (small) BAR_GAP_DP_SMALL else BAR_GAP_DP) * oneDpPx

    fun barWidthPx(oneDpPx: Float): Float = BAR_WIDTH_DP * oneDpPx

    /** How many whole bar cells fit across [widthPx]. 0 for a degenerate pitch/width. */
    fun columnCount(widthPx: Float, pitchPx: Float): Int =
        if (pitchPx <= 0f || widthPx <= 0f) 0 else (widthPx / pitchPx).toInt().coerceAtLeast(0)

    // ---------------------------------------------------------- rails + amplitude

    /** Keep bars clear of the very top/bottom edge, as a fraction of the height. */
    const val INSET_FRACTION = 0.08f

    /** Floor for the rail inset so it survives a tiny canvas (dp). */
    const val INSET_MIN_DP = 1.5f

    /**
     * Full-drive bar reach, as a fraction of the AVAILABLE half (the half-height
     * minus the rail inset). 0.9 < 1 by design: a full-drive bar reaches 90% of
     * the half and NEVER touches a rail, so nothing mesas at any height. A mirror
     * spends this both up and down about the shared centre; a split spends it once
     * per speaker into its own half. Same budget either way.
     */
    const val AMP_FRACTION = 0.9f

    /**
     * Amplitude scale at small height — SHORTER bars on the 40 dp player bar so the
     * strip reads as reactive texture rather than aggressive full-height slabs.
     */
    const val SMALL_AMP_SCALE = 0.8f

    fun railInsetPx(heightPx: Float, minPx: Float): Float =
        maxOf(minPx, heightPx * INSET_FRACTION)

    /** The half-height a bar has to grow into, once the rail inset is removed. Never negative. */
    fun availableHalfPx(heightPx: Float, insetPx: Float): Float =
        (heightPx / 2f - insetPx).coerceAtLeast(0f)

    fun amplitudeDegrade(small: Boolean): Float = if (small) SMALL_AMP_SCALE else 1f

    /**
     * NO PERCEPTUAL LIFT HERE — this used to be `sqrt(level)`, a SECOND copy of the
     * app's one curve, and it is deleted. The perceptual lift now happens exactly once
     * per column, in [columnLevel], through [Aurora.displayCurve]; leaving a private
     * square root behind it would compress the same level twice (effective exponent
     * 0.325) and is precisely the drift WaveformPerceptualCurveTest exists to catch.
     *
     * What survives is the SANITISER the geometry actually needs. NaN is guarded
     * EXPLICITLY: `coerceIn` passes NaN straight through (every comparison against NaN
     * is false, JLS 15.20.1) and one NaN would poison both the extent and the alpha.
     */
    fun drawn(level: Float): Float = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)

    /**
     * One side's bar extent in px — the already-curved column level against the
     * amplitude budget and the small-height degrade. LINEAR in the level (exactly as
     * Aurora's own `amplitude` is linear in its envelope's output), so it can never
     * exceed `AMP_FRACTION * availableHalf` (< the half), which is the no-mesa
     * guarantee.
     */
    fun barHalfExtentPx(availableHalfPx: Float, level: Float, degrade: Float): Float =
        availableHalfPx * AMP_FRACTION * degrade.coerceIn(0f, 1f) * drawn(level)

    // ------------------------------------------------------------- the core row

    /** Half the coloured core segment's height, as a fraction of the height. Short. */
    const val CORE_HALF_FRACTION = 0.05f

    /** Floor for the core half-height so it survives a tiny canvas (dp). */
    const val CORE_MIN_DP = 1.0f

    /**
     * A column with less level than this draws NOTHING (no body, no core): a truly
     * silent, zero-floor column (player-bar file silence) stays clear instead of
     * parking a permanent coloured rule, while a live mic's `restLevel` floor lifts
     * every column above it into the continuous breathing centre band.
     */
    const val CORE_DRAW_EPS = 0.005f

    /** The core segment's half-height in px, never taller than the available half. */
    fun coreHalfPx(heightPx: Float, minPx: Float, availableHalfPx: Float): Float =
        minOf(availableHalfPx, maxOf(minPx, heightPx * CORE_HALF_FRACTION))

    // ---------------------------------------------------------------- brightness

    /** A quiet bar is dim but visible. */
    const val BODY_ALPHA_MIN = 0.35f

    /**
     * A loud bar is bright. Held so [BODY_ALPHA_MAX] + [CORE_ALPHA] = 1.52 stays
     * under Aurora's 1.6 additive white-out ceiling — bars are disjoint columns, so
     * this body-plus-own-core sum is the worst additive stack anywhere on screen.
     */
    const val BODY_ALPHA_MAX = 0.80f

    /** The bright core row's additive alpha. See [BODY_ALPHA_MAX] for the budget. */
    const val CORE_ALPHA = 0.72f

    /** Aurora's additive white-out ceiling; the summed brightness at a point stays under it. */
    const val WHITE_OUT_CEILING = 1.6f

    /** Louder = brighter: the body alpha rises with the drawn column level, MIN..MAX. */
    fun bodyAlpha(level: Float): Float =
        BODY_ALPHA_MIN + (BODY_ALPHA_MAX - BODY_ALPHA_MIN) * drawn(level)

    // ------------------------------------------------- the spectral stripes

    /**
     * Stripes per bar — one per analyser band, stacked low-to-high from the centre line outward.
     * Tied to [AuroraAnalyser.BANDS] rather than chosen: this style shows what the analyser
     * measures, so a band that exists and no stripe for it would simply be a hidden band.
     */
    const val SEGMENTS = AuroraAnalyser.BANDS

    /** Below this total energy the column has no spectral shape to show and the stripes divide evenly. */
    private const val SHARE_EPS = 1e-4f

    /**
     * Band [band]'s share of this column's energy, 0..1, summing to 1 across the bands. A SUM here,
     * not the max the overall level uses: the question a stripe answers is "how much of this instant
     * belongs to me", which is a proportion, whereas the level asks "how loud is the loudest part",
     * which is a peak. A silent column (a rest-level stubble, whose bands are genuinely zero even
     * though its LEVEL is floored) divides evenly — no opinion, not a collapsed bar.
     */
    fun bandShare(b0: Float, b1: Float, b2: Float, b3: Float, band: Int): Float {
        val e0 = sanit(b0)
        val e1 = sanit(b1)
        val e2 = sanit(b2)
        val e3 = sanit(b3)
        val total = e0 + e1 + e2 + e3
        if (total <= SHARE_EPS) return 1f / SEGMENTS
        return when (band.coerceIn(0, SEGMENTS - 1)) {
            0 -> e0
            1 -> e1
            2 -> e2
            else -> e3
        } / total
    }

    /**
     * Where band [band]'s stripe STARTS, as a distance OUTWARD from the centre line inside a bar of
     * half-extent [extPx]. Cumulative over the lower bands, so the stripes tile [0, extPx] with no
     * seam and no overlap — which is both how the bar keeps the silhouette the level gave it and why
     * the white-out budget is unchanged (never two bodies at one pixel).
     */
    fun segmentStartPx(extPx: Float, b0: Float, b1: Float, b2: Float, b3: Float, band: Int): Float {
        val ext = if (extPx.isNaN() || extPx <= 0f) 0f else extPx
        var acc = 0f
        for (i in 0 until band.coerceIn(0, SEGMENTS - 1)) acc += bandShare(b0, b1, b2, b3, i)
        return ext * acc
    }

    /** Where band [band]'s stripe ENDS; the outermost stripe ends exactly at the bar's tip. */
    fun segmentEndPx(extPx: Float, b0: Float, b1: Float, b2: Float, b3: Float, band: Int): Float {
        val ext = if (extPx.isNaN() || extPx <= 0f) 0f else extPx
        return segmentStartPx(ext, b0, b1, b2, b3, band) + ext * bandShare(b0, b1, b2, b3, band)
    }

    /**
     * A stripe's brightness — [bodyAlpha] of ITS OWN band's level, not the column's. That is what
     * makes a quiet high band read as a dim tip above a bright body instead of a uniformly lit bar,
     * and it keeps the alpha inside the same pinned MIN..MAX budget the single body used.
     */
    fun segmentAlpha(bandLevel: Float): Float = bodyAlpha(bandLevel)

    /**
     * One band's level at column [column], read from that band's history ring NEWEST-FIRST — the
     * identical indexing [columnLevel] uses, because the feed pushes the level and the four bands in
     * one loop and a stripe must describe the same instant as the height it divides.
     *
     * Deliberately NOT floored by restLevel: the floor exists to keep a present-but-silent bar
     * visible, and the level ring already applies it. Flooring the bands too would invent a flat
     * spectrum out of silence and paint four equal stripes as if that were measured.
     */
    fun columnBand(ring: EnvelopeRing, column: Int, count: Int): Float {
        if (count <= 0) return 0f
        return ring.newest(count - 1 - column)
    }

    /** NaN is caught explicitly — `coerceIn` passes it straight through (JLS 15.20.1). */
    private fun sanit(v: Float): Float = if (v.isNaN()) 0f else v.coerceIn(0f, 1f)

    // --------------------------------------------------------- history → columns

    /**
     * The level drawn at column [column] (0 = leftmost/oldest, [count]-1 =
     * rightmost/newest), read from [ring] NEWEST-FIRST so the newest sample lands
     * flush at the right edge and older ones trail left. Columns past the ring's
     * filled tail read 0 (empty-safe), so a not-yet-full ring shows silence on its
     * left rather than throwing.
     *
     * THE ONE PERCEPTUAL CURVE goes on here, and this is the only stage of the style
     * that bends a level: [Aurora.displayCurve], called rather than restated. The
     * column used to be the RAW ring sample, which is why the strip read as stubble
     * on a live mic — the ordinary frame's level is a few percent of full scale and a
     * column that short is not a waveform.
     *
     * When the speaker is [present] the curved value is lifted ONTO [restLevel] by
     * [Aurora.restBlend] — a live mic (`restLevel > 0`) keeps a low stubble across the
     * whole strip ("alive but small") that still MOVES with the audio above it, where
     * the `maxOf` this replaces drew the identical column for every level under the
     * floor. The player bar (`restLevel = 0`) is unaffected to the bit: the blend is
     * the identity at floor 0, so file silence stays truly flat. A speaker that has
     * DEPARTED (present = false) gets no floor, so its tail drains to nothing as it
     * scrolls off. NaN-guarded on restLevel; ring samples are already sanitised by
     * [EnvelopeRing].
     */
    fun columnLevel(
        ring: EnvelopeRing,
        present: Boolean,
        restLevel: Float,
        column: Int,
        count: Int,
    ): Float {
        if (count <= 0) return 0f
        val raw = ring.newest(count - 1 - column)
        val floor = if (present && !restLevel.isNaN()) restLevel.coerceIn(0f, 1f) else 0f
        return Aurora.restBlend(floor, Aurora.displayCurve(raw))
    }

    // ------------------------------------------------------------- idle park

    /** A ring sample below this has effectively faded to silence — no motion to scroll. */
    const val MOTION_EPS = 0.01f

    /** Whether [ring] still holds a sample worth scrolling (drives the idle-park gate). */
    fun ringHasMotion(ring: EnvelopeRing): Boolean {
        for (i in 0 until ring.size) if (ring.newest(i) > MOTION_EPS) return true
        return false
    }

    /**
     * Whether a speaker should be drawn this frame: it is PRESENT, or its ring
     * still holds a tail scrolling off. The mirror/split decision keys off this so
     * a just-departed voice keeps draining before the survivor reclaims both halves.
     */
    fun speakerActive(present: Boolean, ring: EnvelopeRing): Boolean =
        present || ringHasMotion(ring)
}
