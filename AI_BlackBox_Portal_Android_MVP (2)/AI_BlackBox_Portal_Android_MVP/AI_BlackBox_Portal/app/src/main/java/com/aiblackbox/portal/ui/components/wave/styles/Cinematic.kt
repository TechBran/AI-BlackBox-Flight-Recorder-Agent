package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
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
import com.aiblackbox.portal.ui.theme.EmberWhiteHot
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.floor

// =============================================================================
// Cinematic TTS Wave (reference sheet #8, tagged "TTS OUTPUT") — the glow
// showcase. A dense SYMMETRIC "voiceprint mass": many thin vertical bars (~1.5 dp
// wide, ~1 dp gap) mirrored about the centre line, drawn LIVE from the shared
// history ring (newest at the RIGHT, older trailing left), the way a whole clip
// renders as a classic audio waveform. The mass swells with the envelope and
// GLOWS: every bar shades from a deep tint at low level to WHITE-HOT at its peak,
// wrapped in a soft additive (BlendMode.Plus) bloom, so a loud passage reads as a
// bright burning core over the near-black field.
//
// This file is ALL LOOK, no plumbing (see WaveformRenderer). VoiceVisualizer owns
// the frame loop, the shared [WaveformFeed], the dt/idle-park/lifecycle
// discipline; this renderer only reads the frame's history rings + levels and
// paints. The maths lives in [Cinematic] as pure functions so the geometry,
// budgets and colour ladder are JVM-testable without a device — the DrawScope
// body below is only a traversal of them. See CinematicGlowTest.
//
// The six things this style must honour, each pinned by a test:
//
//   V1 NEWEST-RIGHT, SCROLLING. Bar i counts back from the leading (right) edge:
//      bar 0 is the newest sample ([EnvelopeRing.newest] 0), bar i the sample i
//      steps older. The ring is decimated on a TIME schedule by the feed, so the
//      mass scrolls at the same wall-clock speed at 60 and 120 Hz (the dt
//      independence is the feed's, inherited — this renderer is a pure function of
//      the ring's contents, so identical rings draw identically). A scrolling
//      style, so [needsFrames] is OVERRIDDEN to keep frames while a ring still
//      holds motion that has not scrolled off, not merely while a voice is audible.
//
//   V2 SYMMETRIC MASS, ONE VOICE vs TWO. One voice draws the FULL symmetric mass —
//      each bar mirrored equally above and below the centre line, its own tint
//      (red for the human, blue for the AI). TWO voices SPLIT the mass: the human
//      owns the TOP half (bars grow UP from centre, red-tinted) and the AI the
//      BOTTOM half (bars grow DOWN, blue-tinted), so the two are told apart by
//      HALF and by COLOUR at once. A speaker draws in its half only while the OTHER
//      is also shown; alone, it expands to the full symmetric mirror — the one mode
//      flip, and a meaningful one (the mass "makes room" the instant the second
//      voice joins). See [barTopPx]/[barBottomPx] and [Mode].
//
//   V3 RED-DOMINANT WHITE-HOT SHADING. A bar's colour is a pure function of its
//      LEVEL: deep tint → mid tint → white-hot as the level rises (see [barColor]).
//      Quiet edge bars sit deep red/blue; the tallest peaks burn white. Hue is the
//      SPEAKER's (red human / blue AI, the locked convention — this is NOT a
//      band-coloured style like Aurora); brightness is the loudness.
//
//   V4 THE GLOW BUDGET (inherited from Aurora v33, a REQUIREMENT not a flourish).
//      Bars never overlap horizontally and the two voices occupy DISJOINT y-halves,
//      so the only OCCLUDING pass — the SrcOver body — covers any point at most
//      ONCE: per-point occlusion is a single [bodyAlpha] ≤ [BODY_ALPHA_MAX] ≤ 0.55,
//      and the particle field survives everywhere, strongest through the quiet
//      parts. The glow ([glowAlpha]) and the burning core ([hotAlpha]) are the two
//      BlendMode.Plus passes — additive LIGHT that brightens instead of hiding — and
//      their worst-case additive stack (overlapping neighbour glows + one hot) is
//      bounded well under Aurora's white-out limit. Pinned by a test, exactly as
//      AuroraWaveformTest bounds its passes.
//
//   V5 NO MESA AT 40 dp. The bar excursion is a fraction of the height with
//      [HALF_FRACTION_FULL] < 0.5, so a full-drive bar can never reach the rail and
//      flat-top — true at ANY height by construction. The 40 dp player bar ALSO
//      gets a defined degrade: [halfFraction] reduces the height RANGE on a small
//      surface (calmer, still glowing) rather than a harsh full-height picket
//      fence. Both are pinned.
//
//   V6 THE BURN IS SPECTRAL. Loudness alone decided everything here once, so a
//      bass note and a sibilant of equal level painted the identical mass. Each bar
//      now also reads the feed's four PER-BAND history rings at its own index, and
//      the share of that instant's energy sitting in the two HIGH bands decides how
//      far up the bar the white-hot spine CLIMBS ([spineFraction]) and how early the
//      colour ladder crosses into white ([hotKnee]). Loudness still owns the bar's
//      HEIGHT and how fiercely it burns ([hotAlpha]); the spectrum owns what the
//      burn looks like. Relative to the loudest band, never absolute — see
//      [brightness] for why that is the only reading that works on a real mic. Zero
//      brightness reproduces the shipped ladder exactly, which the test pins.
// =============================================================================

/**
 * Build a Cinematic renderer. THE public surface the registry depends on:
 * [com.aiblackbox.portal.ui.components.wave.WaveformStyles] registers Cinematic by
 * calling this, so the function name/shape must not change even though the body did.
 */
internal fun cinematicRenderer(): WaveformRenderer = CinematicRenderer()

private class CinematicRenderer : WaveformRenderer {

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        // dp → px once, off the DrawScope's density. Everything below is derived
        // from these, so the same renderer serves the 40 dp player bar, the 52 dp
        // composer strip and the 140 dp voice screen with no size parameter.
        val barWidthPx = maxOf(1f, Cinematic.BAR_WIDTH_DP.dp.toPx())
        val gapPx = Cinematic.BAR_GAP_DP.dp.toPx()
        val pitchPx = barWidthPx + gapPx
        val glowWidthPx = Cinematic.GLOW_WIDTH_DP.dp.toPx()
        val glowPadYpx = Cinematic.GLOW_PAD_Y_DP.dp.toPx()
        val hotWidthPx = barWidthPx * Cinematic.HOT_WIDTH_FRACTION
        val centreGapPx = Cinematic.CENTRE_GAP_DP.dp.toPx()
        val minSliverPx = Cinematic.MIN_SLIVER_DP.dp.toPx()

        val heightDp = h / density
        val restLevel = frame.restLevel.coerceIn(0f, 1f)
        val barCount = Cinematic.barCount(w, pitchPx)
        val centreY = h / 2f

        // Which speakers have anything to draw this frame (present, or a history
        // tail still scrolling off). The layout mode of EACH depends on the OTHER.
        val feed = frame.feed
        val speakers = SPEAKERS
        val shown = BooleanArray(speakers.size) { i ->
            val s = speakers[i]
            feed.present(s) || Cinematic.historyEnergy(feed.history(s))
        }

        for (i in speakers.indices) {
            if (!shown[i]) continue
            val speaker = speakers[i]
            // Alone → the full symmetric mirror; with the other voice → this
            // speaker's assigned half. (Two speakers, so "the other" is 1 - i.)
            val otherShown = shown[speakers.size - 1 - i]
            val mode = if (otherShown) {
                if (speaker == AuroraSpeaker.HUMAN) Mode.HALF_UP else Mode.HALF_DOWN
            } else {
                Mode.SYMMETRIC
            }
            val present = feed.present(speaker)
            val ring = feed.history(speaker)
            // The four per-band rings, read at the SAME bar index as the level ring:
            // the feed pushes level and bands in one loop, so bar i is one instant of
            // audio in all five (V6).
            val bandRing0 = feed.bandHistory(speaker, 0)
            val bandRing1 = feed.bandHistory(speaker, 1)
            val bandRing2 = feed.bandHistory(speaker, 2)
            val bandRing3 = feed.bandHistory(speaker, 3)

            for (bar in 0 until barCount) {
                val sample = ring.newest(bar)
                // THE ONE PERCEPTUAL CURVE and the ONE floor rule, both called out of
                // Aurora rather than restated. This style read its history RAW, so at
                // a live mic's typical level its bars stood a few percent of the box
                // tall and the strip read as a flat rule; the curve is what makes an
                // ordinary syllable a bar you can see.
                //
                // Present-but-silent rests at EXACTLY the caller's floor (breathing on
                // a live mic, flat 0 on the player bar) because [Aurora.restBlend] maps
                // level 0 onto it; a departed tail has no floor, so it scrolls cleanly
                // off. Both already 0..1, NaN-safe.
                val floor = if (present) restLevel else 0f
                val level = Aurora.restBlend(floor, Aurora.displayCurve(sample))

                var ext = Cinematic.extentPx(h, heightDp, level)
                // A present voice always shows at least a hairline flat line, so an
                // idle player bar is a thin sliver not an empty box (Classic/Aurora
                // both do this). A departed tail at zero draws nothing.
                if (present) ext = maxOf(ext, minSliverPx)
                if (ext <= 0f) continue

                val left = Cinematic.barLeftPx(bar, w, pitchPx, barWidthPx)
                val centreX = left + barWidthPx / 2f
                val top = Cinematic.barTopPx(centreY, ext, mode, centreGapPx)
                val bottom = Cinematic.barBottomPx(centreY, ext, mode, centreGapPx)
                val barH = bottom - top
                if (barH <= 0f) continue

                // What is IN this bar, not just how much of it there is (V6).
                val brightness = Cinematic.brightness(
                    bandRing0.newest(bar), bandRing1.newest(bar),
                    bandRing2.newest(bar), bandRing3.newest(bar),
                )

                // Pass 1 — GLOW: a wide, soft additive halo, bleeding past the bar
                // ends like Aurora's bloom (it is drawn over a transparent surface,
                // so there is nothing to clip and pulling it in would harden the
                // glow to an edge). BlendMode.Plus — light, never occlusion.
                drawRect(
                    color = Cinematic.glowColor(speaker),
                    topLeft = Offset(centreX - glowWidthPx / 2f, top - glowPadYpx),
                    size = Size(glowWidthPx, barH + 2f * glowPadYpx),
                    alpha = Cinematic.glowAlpha(level),
                    blendMode = BlendMode.Plus,
                )
                // Pass 2 — BODY: the crisp bar, the ONLY occluding pass (default
                // SrcOver). Held to [bodyAlpha] ≤ [BODY_ALPHA_MAX]; bars never
                // overlap, so this is the whole per-point occlusion and the field
                // shows through (V4).
                drawRect(
                    color = Cinematic.barColor(speaker, level, brightness),
                    topLeft = Offset(left, top),
                    size = Size(barWidthPx, barH),
                    alpha = Cinematic.bodyAlpha(level),
                )
                // Pass 3 — HOT CORE: a narrow white-hot additive spine that only
                // lights on LOUD bars ([hotAlpha] ∝ level²). This is the "burning
                // core" — the second BlendMode.Plus pass. How FIERCELY it burns is
                // the loudness; how FAR up the bar it climbs is the spectrum, so a
                // sibilant burns to the tip and a bass note burns at the base only.
                val hotA = Cinematic.hotAlpha(level)
                if (hotA > 0f) {
                    val spineExt = Cinematic.spineExtentPx(ext, brightness)
                    val spineTop = Cinematic.barTopPx(centreY, spineExt, mode, centreGapPx)
                    val spineH = Cinematic.barBottomPx(centreY, spineExt, mode, centreGapPx) - spineTop
                    if (spineH > 0f) {
                        drawRect(
                            color = Cinematic.HOT_COLOR,
                            topLeft = Offset(centreX - hotWidthPx / 2f, spineTop),
                            size = Size(hotWidthPx, spineH),
                            alpha = hotA,
                            blendMode = BlendMode.Plus,
                        )
                    }
                }
            }
        }
    }

    /**
     * A scrolling style keeps frames while a history ring still holds motion that
     * has not yet scrolled off, not only while a voice is audible — otherwise a
     * half-finished scroll would freeze the instant the speaker fell silent (see
     * [WaveformRenderer.needsFrames]).
     */
    override fun needsFrames(voices: List<AuroraVoice>, feed: WaveformFeed): Boolean {
        // Audibility MUST be read from the passed voices, not feed.level(): a
        // parked host has not stepped the feed yet, so a loud voice arriving at
        // an un-stepped feed reads level==0 and the style would never wake
        // (park deadlock — WaveformRegistryTest's audibility invariant).
        for (i in voices.indices) if (voices[i].hasEnergy()) return true
        for (i in SPEAKERS.indices) {
            val s = SPEAKERS[i]
            if (feed.level(s) > Cinematic.ENERGY_FLOOR) return true
            if (Cinematic.historyEnergy(feed.history(s))) return true
        }
        return false
    }

    private companion object {
        private val SPEAKERS = AuroraSpeaker.entries
    }
}

/** Which way a speaker's bars grow from the centre line (V2). */
internal enum class Mode { SYMMETRIC, HALF_UP, HALF_DOWN }

// =============================================================================
// Geometry + budget + colour maths — pure, no Compose draw state, unit-testable.
// =============================================================================

internal object Cinematic {

    // ------------------------------------------------------------ bar layout

    /**
     * Bar width and the gap after it, in dp. A dense voiceprint wants many THIN
     * bars: ~1.5 dp of ink, ~1 dp of gap, a 2.5 dp pitch. The gap is what reads as
     * "bars" rather than a solid block — and, with the body ≤ [BODY_ALPHA_MAX], is
     * why no two bodies ever composite at one point (V4).
     */
    const val BAR_WIDTH_DP = 1.5f
    const val BAR_GAP_DP = 1.0f

    /** The pitch (bar + gap) in dp — the ONE number the white-out overlap is derived from. */
    const val PITCH_DP = BAR_WIDTH_DP + BAR_GAP_DP

    /**
     * Bar-count clamp. A narrow composer strip stays smooth at [MIN_BARS]; a wide
     * surface is capped a hair above the ring's ~150-sample capacity so it fills
     * without asking the ring for hundreds of already-empty older columns (reads as
     * silence anyway) or drawing a 4K wall of rects.
     */
    const val MIN_BARS = 16
    const val MAX_BARS = 220

    /**
     * How many bars fit across [widthPx] at [pitchPx], clamped to [MIN_BARS]..[MAX_BARS].
     * A degenerate width still yields [MIN_BARS] (never 0, never a throw).
     */
    fun barCount(widthPx: Float, pitchPx: Float): Int {
        if (pitchPx <= 0f || widthPx <= 0f || widthPx.isNaN()) return MIN_BARS
        return (widthPx / pitchPx).toInt().coerceIn(MIN_BARS, MAX_BARS)
    }

    /**
     * The left edge of bar [index], counted NEWEST-FIRST from the RIGHT (V1): bar 0
     * sits flush against the right edge (its newest sample at the leading edge), bar
     * i is [pitchPx] further left. The rightmost bar is the present moment; the
     * waveform is born at the right and marches away to the left.
     */
    fun barLeftPx(index: Int, widthPx: Float, pitchPx: Float, barWidthPx: Float): Float =
        widthPx - index * pitchPx - barWidthPx

    // -------------------------------------------------------------- vertical

    /**
     * Peak bar excursion either side of the centre line, as a fraction of the
     * height, at FULL surface size. STRICTLY below 0.5 so a full-drive bar's tip
     * lands `height * (0.5 - HALF_FRACTION_FULL)` = 0.04·h clear of the rail and can
     * never flat-top into a mesa (V5) — the same guarantee, by construction, that
     * Aurora budgets its amplitude for, and the defect this project has twice had to
     * remove. A symmetric one-voice bar spans `centre ± this·level`; a two-voice
     * half-bar reaches `this·level` into its own half.
     */
    const val HALF_FRACTION_FULL = 0.46f

    /**
     * The reduced excursion at the 40 dp player bar (V5's degrade). At full size a
     * 0.46 range on a 40 dp bar is a near-solid, harsh picket fence; shrinking the
     * RANGE (not the glow) keeps the small surface calm and premium while it still
     * blooms. Interpolated between [DEGRADE_SMALL_H_DP] and [DEGRADE_FULL_H_DP].
     */
    const val HALF_FRACTION_SMALL = 0.34f

    /** At/below this height (dp) the small excursion applies — 40 dp player bar sits here. */
    const val DEGRADE_SMALL_H_DP = 44f

    /** At/above this height (dp) the full excursion applies — the 140 dp voice screen. */
    const val DEGRADE_FULL_H_DP = 96f

    /** A present-but-silent voice still shows a hairline flat line (dp). */
    const val MIN_SLIVER_DP = 1.0f

    /**
     * A whisper of dead space at the centre line in the two-voice split, so the
     * human's top-half mass and the AI's bottom-half mass read as two touching
     * blocks, not one seam — and so their bodies never share the centre pixel.
     */
    const val CENTRE_GAP_DP = 0.5f

    /**
     * The height-relative excursion for a surface [heightDp] tall: [HALF_FRACTION_FULL]
     * on a tall surface, [HALF_FRACTION_SMALL] on the 40 dp bar, smoothstepped
     * between. Always < 0.5, so V5's no-mesa guarantee holds at every height.
     */
    fun halfFraction(heightDp: Float): Float {
        if (heightDp.isNaN()) return HALF_FRACTION_SMALL
        return when {
            heightDp <= DEGRADE_SMALL_H_DP -> HALF_FRACTION_SMALL
            heightDp >= DEGRADE_FULL_H_DP -> HALF_FRACTION_FULL
            else -> {
                val t = ((heightDp - DEGRADE_SMALL_H_DP) / (DEGRADE_FULL_H_DP - DEGRADE_SMALL_H_DP))
                    .coerceIn(0f, 1f)
                val s = t * t * (3f - 2f * t) // smoothstep
                HALF_FRACTION_SMALL + (HALF_FRACTION_FULL - HALF_FRACTION_SMALL) * s
            }
        }
    }

    /**
     * A bar's excursion in px for [level] (0..1) on a [heightPx]/[heightDp] surface —
     * the band's loudness IS how far the bar reaches. NaN-safe: a NaN level is 0
     * (JLS 15.20.1 lets NaN slip past `coerceIn`, so it is caught explicitly), which
     * is what keeps a poisoned feed from stranding a bar rather than merely uglifying
     * it.
     */
    fun extentPx(heightPx: Float, heightDp: Float, level: Float): Float {
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        return heightPx * halfFraction(heightDp) * l
    }

    /** The top y of a bar of half-excursion [ext] in [mode] about [centreY]. */
    fun barTopPx(centreY: Float, ext: Float, mode: Mode, centreGapPx: Float): Float = when (mode) {
        Mode.SYMMETRIC -> centreY - ext
        Mode.HALF_UP -> centreY - centreGapPx - ext
        Mode.HALF_DOWN -> centreY + centreGapPx
    }

    /** The bottom y of a bar of half-excursion [ext] in [mode] about [centreY]. */
    fun barBottomPx(centreY: Float, ext: Float, mode: Mode, centreGapPx: Float): Float = when (mode) {
        Mode.SYMMETRIC -> centreY + ext
        Mode.HALF_UP -> centreY - centreGapPx
        Mode.HALF_DOWN -> centreY + centreGapPx + ext
    }

    // ----------------------------------------------------------- alpha budget

    /**
     * The BODY (SrcOver, occluding) alpha for [level]. Rises with loudness so quiet
     * edge bars are faint and the field pours through them, loud bars nearly solid.
     * Capped at [BODY_ALPHA_MAX] ≤ 0.55: bars never overlap and the two voices are
     * in disjoint halves, so per-point occlusion is exactly ONE of these (V4).
     */
    const val BODY_ALPHA_MIN = 0.12f
    const val BODY_ALPHA_MAX = 0.52f

    fun bodyAlpha(level: Float): Float {
        val l = sanit(level)
        return BODY_ALPHA_MIN + (BODY_ALPHA_MAX - BODY_ALPHA_MIN) * l
    }

    /**
     * The GLOW (BlendMode.Plus) halo alpha for [level] — additive light, never
     * occlusion. Kept low so a stack of overlapping neighbour glows plus one hot
     * core stays under the white-out limit (see [glowOverlapCount] and the test).
     */
    const val GLOW_ALPHA_MIN = 0.04f
    const val GLOW_ALPHA_MAX = 0.22f

    fun glowAlpha(level: Float): Float {
        val l = sanit(level)
        val s = l * l * (3f - 2f * l) // smoothstep — the glow swells softly
        return GLOW_ALPHA_MIN + (GLOW_ALPHA_MAX - GLOW_ALPHA_MIN) * s
    }

    /**
     * The white-hot CORE (BlendMode.Plus) alpha for [level] — the "burning core".
     * `level²` so ONLY genuinely loud bars ignite: quiet speech shows the tint, a
     * shout burns white. 0 at silence, so a resting mass carries no hot pass at all.
     */
    const val HOT_ALPHA_MAX = 0.50f

    fun hotAlpha(level: Float): Float {
        val l = sanit(level)
        return HOT_ALPHA_MAX * l * l
    }

    /** The soft halo width, dp — /PITCH sets how many neighbour glows overlap (white-out budget). */
    const val GLOW_WIDTH_DP = 5.0f

    /** How far the glow bleeds past the bar's ends, dp — the tips bloom too. */
    const val GLOW_PAD_Y_DP = 1.5f

    /** The white-hot core is narrower than the bar, so it reads as a spine, not a repaint. */
    const val HOT_WIDTH_FRACTION = 0.6f

    /**
     * How many bars' glow haloes cover a single point at once (V4's white-out
     * budget). A glow of width [GLOW_WIDTH_DP] centred on a bar reaches
     * `GLOW_WIDTH_DP/2` each side; a neighbour n pitches away contributes when
     * `n·PITCH_DP ≤ GLOW_WIDTH_DP/2`. So `2·floor(GLOW_WIDTH_DP/(2·PITCH_DP)) + 1`
     * glows stack at the worst point. Density-independent (both terms scale with it).
     */
    fun glowOverlapCount(): Int =
        2 * floor(GLOW_WIDTH_DP / (2f * PITCH_DP)).toInt() + 1

    // ---------------------------------------------------------------- colour

    /**
     * Where the level ladder crosses from the coloured body into the white-hot
     * burn. Below the knee a bar shades deep → mid tint; above it, mid → white.
     */
    const val HOT_KNEE = 0.62f

    /**
     * How far a fully bright bar pulls the white-hot crossover DOWN the ladder. The physical
     * reading: sibilance and consonant attacks are what a burning core looks like, so bright content
     * whitens at a lower level than a rumble of the same loudness does.
     */
    const val HOT_KNEE_BRIGHT_SHIFT = 0.18f

    /**
     * The crossover level for a bar of this [brightness]. Clamped strictly inside the ladder because
     * both colour segments divide by it (or by `1 - it`) — a knee at either rail would be a NaN
     * colour, which is a stranded bar rather than an ugly one.
     */
    fun hotKnee(brightness: Float): Float =
        (HOT_KNEE - HOT_KNEE_BRIGHT_SHIFT * sanit(brightness)).coerceIn(0.10f, 0.90f)

    /**
     * A bar's colour for [level] (V3), on the ladder [brightness] chose: deep tint at the quiet
     * edges, the speaker's mid tint through speech, WHITE-HOT at the peaks. Hue is the SPEAKER's —
     * red for the human, blue for the AI, the locked convention (this is not a band-coloured style);
     * LOUDNESS drives how far up the ladder a bar sits, and the SPECTRUM moves where the white burn
     * begins. A pure two-segment lerp so the tests can pin the endpoints on channels.
     */
    fun barColor(speaker: AuroraSpeaker, level: Float, brightness: Float = 0f): Color {
        val l = sanit(level)
        val knee = hotKnee(brightness)
        val deep = if (speaker == AuroraSpeaker.HUMAN) HUMAN_DEEP else AI_DEEP
        val mid = if (speaker == AuroraSpeaker.HUMAN) HUMAN_MID else AI_MID
        return if (l <= knee) {
            lerp(deep, mid, l / knee)
        } else {
            lerp(mid, HOT_COLOR, (l - knee) / (1f - knee))
        }
    }

    // -------------------------------------------------------------- spectrum

    /**
     * How much of a bar's energy is HIGH, 0..1 — the two upper bands against the loudest band of the
     * same instant.
     *
     * RELATIVE on purpose. Absolute band levels are not comparable across bands on live audio: the
     * measured spread between the loudest and the quietest is over an order of magnitude (the upper
     * bands sit near their noise floor while the body carries the voice), so a burn gated on an
     * absolute high-band level would never light on a phone mic. The ratio asks the question the
     * look actually depends on — "is the top of the spectrum what is loud right now" — and survives
     * a re-calibration of the bands underneath it.
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

    /** Below this a bar carries no usable spectral shape and burns at its dull floor. */
    private const val SPECTRAL_EPS = 1e-4f

    /**
     * The shortest white-hot spine, as a fraction of the bar: what a completely dark bar still
     * burns. Not zero — the hot core is Cinematic's signature and a mass that lost it on a bass note
     * would read as a different style — but short enough that the climb is the thing you notice.
     */
    const val SPINE_MIN_FRACTION = 0.30f

    /**
     * How much of the bar the white-hot spine covers at this [brightness]. The spine grows OUT OF
     * THE CENTRE LINE, the same base the bar grows from, so it can never reach past the tip — the
     * no-mesa guarantee covers it with no second argument. A dark bar burns at its base; a sibilant
     * burns end to end.
     */
    fun spineFraction(brightness: Float): Float =
        SPINE_MIN_FRACTION + (1f - SPINE_MIN_FRACTION) * sanit(brightness)

    /** The spine's own half-excursion in px — [extentPx]'s twin, and always ≤ it. */
    fun spineExtentPx(extPx: Float, brightness: Float): Float {
        val e = if (extPx.isNaN() || extPx <= 0f) 0f else extPx
        return e * spineFraction(brightness)
    }

    /** The glow halo's hue — the speaker's mid tint (a coloured bloom, not a white wash). */
    fun glowColor(speaker: AuroraSpeaker): Color =
        if (speaker == AuroraSpeaker.HUMAN) HUMAN_MID else AI_MID

    /** Deep red / bright red — the human's tint floor and mid. */
    val HUMAN_DEEP = Color(0xFF7A0A08)
    val HUMAN_MID = BbxAccent

    /** Deep blue / bright blue — the AI's tint floor and mid. */
    val AI_DEEP = Color(0xFF0B2E63)
    val AI_MID = WaveBlue

    /** The white-hot peak — a warm ember white, shared by both tints (the burning core). */
    val HOT_COLOR = EmberWhiteHot

    // -------------------------------------------------------------- activity

    /** A ring "holds motion" if any sample it still carries is above this floor. */
    const val ENERGY_FLOOR = 0.02f

    /**
     * Whether [ring] still holds motion that has not scrolled off — the gate for a
     * speaker being SHOWN (its tail still drawing) and for [needsFrames] keeping the
     * scroll alive after the voice falls silent. Samples are already sanitised on
     * push, so this is a plain scan; O(size), a couple of hundred at most.
     */
    fun historyEnergy(ring: EnvelopeRing): Boolean {
        for (i in 0 until ring.size) if (ring.newest(i) > ENERGY_FLOOR) return true
        return false
    }

    private fun sanit(v: Float): Float = if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
}
