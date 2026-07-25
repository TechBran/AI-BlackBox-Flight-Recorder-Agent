package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: DRIFT SNOW (id "snow").
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/snow.js
// (its physics tests: Portal/modules/fx/effects/snow.test.mjs).
//
// Three depth planes of soft, out-of-focus flakes sifting DOWN, each flake
// fluttering on its own sine, with an occasional field-wide GUST that shoves the
// whole population sideways for about a second and then settles.
//
// The depth illusion is a CORRELATION, not a trick: within a plane, size, fall
// speed, sway amplitude, blur spread and gust response all move together, so a
// big near flake is fast and smeared while a small far one is slow and crisp.
// Break the correlation (equal speeds, equal sharpness) and three planes read as
// one flat field of dots. SnowFieldTest pins that monotonicity.
//
// ── Three decisions that look like omissions and are not ─────────────────────
//
//   BLEND IS SrcOver, NOT ADDITIVE. This is a BACKDROP behind chat text.
//     White-on-black at 250 flakes under additive blend blows the overlaps out
//     to paper white and takes the body-text contrast with it. A non-emissive
//     field means OCCLUSION, not accumulation. Every other shipped field draws
//     through FieldPaints.sprite, which carries FIELD_BLEND (PLUS/ADD ≥ API 28)
//     — so snow deliberately does NOT use it and bakes its own SrcOver paint
//     through FieldResources instead. Same reason the population is capped at
//     [SNOW_MAX_FLAKES] rather than scaling freely with the viewport.
//     COROLLARY, and the bug it caused: because snow bypasses [drawSpriteF] it
//     does NOT inherit that helper's `a * paints.alphaScale` multiply, so when
//     Intensity gained a BRIGHTNESS channel the whole dial was a no-op here. The
//     multiply is applied by hand in [render] instead — see [snowDrawAlpha].
//
//   ONE BAKED SPRITE PER FLAKE, NOT CONCENTRIC ARCS. The web shipped a flake as
//     1–3 stacked filled arcs at decreasing alpha; every arc fill has a hard
//     edge, so the stack produced visible concentric bands and Brandon called it
//     "a bubble inside a bubble" the moment he saw it (2026-07-24). A soft
//     falloff cannot be faked with hard-edged shapes — it needs an actual
//     gradient. So: bake ONE tinted radial sprite per ramp stop (memoized in
//     FieldResources, never in the hot path twice) and blit it. Smooth AND ~3x
//     fewer draw calls per flake. `soft` now widens the SPRITE instead of adding
//     halo rings, which is what being out of focus actually is.
//
//   `life` IS FALL PROGRESS (0 at the top margin, 1 at the bottom), not an age
//     counter. A flake's life in this sim genuinely IS how far it has fallen, and
//     indexing the size/alpha envelope by it means every flake fades in above the
//     top edge and out below the bottom one — so recycling is invisible, with no
//     popping and no bookkeeping.
//
// ── THE THREE PREMIUM RULES, as they land here ───────────────────────────────
//   1. SIZE OVER LIFE with a per-particle envelope: [snowSizeCurve] is shared,
//      the AMPLITUDE ([SnowFlake.sizeEnv], 0.78–1.28) is drawn per flake.
//   2. LIFE-INDEXED COLOUR RAMP: 5 stops (moonlit steel-blue high → near-white
//      mid-fall → dusty settle low) resampled to [SNOW_RAMP_STEPS], indexed by
//      `life + bias`; the per-flake bias de-banks it so two flakes at the same
//      height are not the same colour.
//   3. THREE SUB-POPULATIONS (far / mid / near), correlated into depth.
//
// ── BRIGHTNESS PASS 2026-07-25 — a DELIBERATE divergence from snow.js ────────
//
// Brandon, Fold 6 (density 3.1, 120 Hz OLED), Intensity at maximum: "it does look
// like snow particles are moving, looks a lot better than before. We could make it
// a bit brighter, the white particles could be a shade brighter or two … while I
// can see the snow it is just not so bright."
//
// Three things were dimming it, fixed together. DO NOT "restore parity" with the
// .js constants — the web numbers are correct FOR THE WEB and were never dim
// there; the cross-surface contract is the same APPARENT RESULT, not the same
// literals:
//
//   1. THE DIAL DID NOT REACH THE ALPHA. Intensity only ever scaled COUNT here,
//      and the new brightness channel rides [drawSpriteF], which snow bypasses
//      (see the SrcOver note above). Turning the dial up added more flakes of
//      identical faintness. [render] now multiplies by paints.alphaScale itself.
//
//   2. THE SPRITE SPENT ITS ALPHA ON ITS OWN TAIL. The gaussian shoulder's
//      area-weighted mean alpha was ~0.23 of peak — i.e. ~77% of the nominal
//      per-plane ceiling was being thrown away into a halo too faint to see at
//      3.1 density, where a mid flake is only ~5 device px across to begin with.
//      Widening the shoulder (see the sprite stops) carries ~0.36 of peak — about
//      1.6x the light per flake at an UNCHANGED peak alpha, so the worst-case
//      single pixel behind a glyph did not get any brighter. This is the cheapest
//      brightness in the file and the reason the ceilings only had to move a
//      little; the falloff still reaches zero smoothly, so there is still no
//      discernible edge (the "bubble inside a bubble" failure stays fixed).
//
//   3. THE CEILINGS WERE AUTHORED FOR CSS px AT SCALE ~1. A web flake covers
//      ~1.55x the apparent radius of ours before antialiasing; sub-pixel geometry
//      then antialiases the outer ramp toward invisibility BEFORE alpha is
//      applied. The per-plane ceilings are lifted ~1.2x (0.30/0.40/0.34 →
//      0.36/0.48/0.42), keeping the web's far < near < mid ORDERING, which is the
//      part that carries the legibility logic (near flakes are big and blurred, so
//      they stay below mid). The colour ramp's cold ENDS are lifted the same way:
//      a flake fading in at the top was drawing rgb(150,176,210) at a quarter
//      alpha, which is grey dust, not snow.
//
// The budget is still a budget: [SNOW_MAX_FLAKES] is untouched, the blend is still
// SrcOver, and [SNOW_ALPHA_CAP] is a NEW hard guard rail that no plane ceiling ×
// life curve × dial setting can cross. SnowFieldTest pins the properties (ordering,
// cap, bounded lift) rather than the old literals.
//
// ── The two Android-specific contracts ───────────────────────────────────────
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here is
// authored at the WEB's CSS-px scale, stored UNSCALED, and multiplied by
// `g = scale × SNOW_WEB_TO_REF_PX` at the moment it is used — so the field has
// the same APPARENT size on a 2.0 tablet and a 3.5 phone. (The web stores its
// lengths pre-multiplied and rescales them on every resize; keeping the factor
// out of stored state is the Android idiom — see ADDING_AN_EFFECT.md §4 — and it
// deletes the whole rescale-on-resize step.) Density may reach NOTHING else: not
// a count (those come from dp AREA), not an alpha, not a period.
//
// dt. Velocities here are PIXELS PER SECOND and are multiplied by dtSec directly
// — no `dt * 60` normalizer. The sway clock and the gust clock are ACCUMULATED
// from dtSec rather than read off nowMs: an absolute timestamp goes stale across
// a parked frame loop and fires a gust the instant the app comes back.
// =============================================================================

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.drawscope.CanvasDrawScope
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.LayoutDirection
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

private const val SNOW_TAU = 6.2831855f

/**
 * WEB CSS px → reference-density DEVICE px.
 *
 * The web module authors every length in CSS px and multiplies by
 * apparentScale(cssBox), which clamps to 0.5 on a phone-shaped viewport. A CSS px
 * is a dp, and FIELD_REFERENCE_DENSITY turns a dp into 3.1 device px:
 *     0.5 × 3.1 = 1.55
 * Keeping this factor explicit lets every tuning number below stay
 * BYTE-IDENTICAL to snow.js, so the two can be diffed forever.
 */
private const val SNOW_WEB_TO_REF_PX = 1.55f

// -----------------------------------------------------------------------------
// LEGIBILITY BUDGET. A hard ceiling, not a target — see the header.
// -----------------------------------------------------------------------------
/** Never more flakes than this, at any viewport, at any dial setting. */
internal const val SNOW_MAX_FLAKES = 250

/** Spawn/despawn band above and below the viewport, in WEB CSS px. */
internal const val SNOW_MARGIN = 22f

/** How much harder the wind drives the field DOWN while it is blowing. */
private const val SNOW_LEAN = 0.12f

// -----------------------------------------------------------------------------
// The three sub-populations. Every field is a range sampled per flake, so no two
// flakes in a plane are identical; the PLANES differ from each other in the
// correlated way that produces depth.
//
//   size                 radius, WEB CSS px  (× g at use, never × density directly)
//   fall / sway          WEB CSS px per SECOND — sway is a velocity amplitude
//   swayW                flutter angular frequency, rad/s (dimensionless in px)
//   soft                 blur spread: how far past the core the halo reaches
//   alpha                THE PER-PLANE ALPHA CEILING (see the legibility budget).
//                        DIVERGES from snow.js by ~1.2x — header, brightness pass
//                        item 3. What must NOT change is the ORDERING
//                        far < near < mid: the near plane is the big blurred one,
//                        so it stays under mid no matter how bright the field gets.
//   gust                 share of the global gust this plane rides (parallax)
//
// (The web's vestigial `rings` field is gone: it counted the stacked arcs the
// baked sprite replaced. `soft` carries the defocus now.)
// -----------------------------------------------------------------------------
internal class SnowPlaneCfg(
    val count: Int,
    val sizeMin: Float, val sizeMax: Float,
    val fallMin: Float, val fallMax: Float,
    val swayMin: Float, val swayMax: Float,
    val swayWMin: Float, val swayWMax: Float,
    val soft: Float,
    val alpha: Float,
    val gust: Float,
)

/** FAR — small, slow, crisp, faint. Barely notices the wind. */
internal val SNOW_FAR = SnowPlaneCfg(
    count = 112, sizeMin = 0.6f, sizeMax = 1.15f, fallMin = 11f, fallMax = 19f,
    swayMin = 3f, swayMax = 8f, swayWMin = 1.1f, swayWMax = 2.0f,
    soft = 0.30f, alpha = 0.36f, gust = 0.32f,      // web 0.30
)

/** MID — the body of the field. */
internal val SNOW_MID = SnowPlaneCfg(
    count = 88, sizeMin = 1.3f, sizeMax = 2.3f, fallMin = 23f, fallMax = 38f,
    swayMin = 8f, swayMax = 16f, swayWMin = 1.4f, swayWMax = 2.6f,
    soft = 0.85f, alpha = 0.48f, gust = 0.68f,      // web 0.40
)

/** NEAR — big, fast, blurred. Rides the gust hardest: that is PARALLAX (screen
 *  displacement scales with proximity), not aerodynamics. */
internal val SNOW_NEAR = SnowPlaneCfg(
    count = 44, sizeMin = 2.6f, sizeMax = 4.8f, fallMin = 46f, fallMax = 78f,
    swayMin = 13f, swayMax = 26f, swayWMin = 1.8f, swayWMax = 3.2f,
    soft = 1.55f, alpha = 0.42f, gust = 1.15f,      // web 0.34
)

internal val SNOW_PLANES = arrayOf(SNOW_FAR, SNOW_MID, SNOW_NEAR)

/** Gust state machine, in SECONDS. Strength is WEB CSS px/s. */
internal object SnowGust {
    const val WAIT_MIN = 4.5f
    const val WAIT_MAX = 11f
    const val HOLD_MIN = 0.7f
    const val HOLD_MAX = 1.6f
    const val STRENGTH_MIN = 38f
    const val STRENGTH_MAX = 96f

    /** Exponential-approach rate, per second. */
    const val EASE = 2.4f
}

// -----------------------------------------------------------------------------
// Population planning — the Kotlin twin of snow.js `planeCounts(env.scale)`.
//
// Density (HOW MANY) comes from the viewport's dp area × the user's dial;
// GEOMETRY (HOW BIG) comes from `g`. Conflating the two is the shipped dpr bug
// in a new costume, so they never meet.
// -----------------------------------------------------------------------------

/** The web's reference viewport, in CSS px² — a 1440 × 900 desktop is scale 1. */
private const val SNOW_REF_VIEWPORT_DP = 1440f * 900f

// The web's fieldScale() clamps (ember-fx.js): the viewport term first, then the
// combined product. A phone (≈ 412 × 915 dp) sits under the viewport floor, so it
// ships ~0.4 × the desktop population — which is exactly what a phone browser
// gets today, and what the legibility budget was measured against.
private const val SNOW_VIEWPORT_MIN = 0.4f
private const val SNOW_VIEWPORT_MAX = 2.4f
private const val SNOW_SCALE_FLOOR = 0.12f
private const val SNOW_SCALE_CEIL = 4.0f

/**
 * The web engine's `fieldScale()`: "how busy is this viewport", folded with the
 * operator's Intensity × Quality dial ([countScale], i.e. the web's
 * countMultiplier). [areaDp] is the canvas area in LOGICAL px² — never device px².
 */
internal fun snowFieldScale(areaDp: Float, countScale: Float): Float {
    if (!areaDp.isFinite() || areaDp <= 0f) return countScale
    val viewport = (areaDp / SNOW_REF_VIEWPORT_DP).coerceIn(SNOW_VIEWPORT_MIN, SNOW_VIEWPORT_MAX)
    return (viewport * countScale).coerceIn(SNOW_SCALE_FLOOR, SNOW_SCALE_CEIL)
}

/**
 * Per-plane populations for a viewport scale, clamped to the legibility budget.
 * Ported line-for-line from snow.js so the two can be diffed.
 *
 * @return one count per plane, each ≥ 1, summing to ≤ [SNOW_MAX_FLAKES].
 */
internal fun snowPlaneCounts(scale: Float): IntArray {
    val s = if (scale.isFinite() && scale > 0f) scale else 1f
    val out = IntArray(SNOW_PLANES.size)
    var total = 0
    for (i in SNOW_PLANES.indices) {
        out[i] = max(3, (SNOW_PLANES[i].count * s).roundToInt())
        total += out[i]
    }
    if (total > SNOW_MAX_FLAKES) {
        val k = SNOW_MAX_FLAKES.toFloat() / total
        total = 0
        for (i in out.indices) {
            out[i] = max(1, (out[i] * k).toInt())
            total += out[i]
        }
        // The per-plane floor of 1 can in principle push the sum back over budget.
        // The budget wins: trim the densest plane until it fits.
        while (total > SNOW_MAX_FLAKES) {
            var big = 0
            for (i in 1 until out.size) if (out[i] > out[big]) big = i
            out[big]--
            total--
        }
    }
    return out
}

// -----------------------------------------------------------------------------
// Pure envelope maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------

/**
 * Size/alpha envelope over life: swell in over the first 18% of the fall, hold,
 * taper over the last 22%. Smoothstepped so the fade has no visible corner, and
 * clamped outside 0..1 so a flake parked above the top edge is simply invisible.
 *
 * @param life 0..1 fall progress
 */
internal fun snowSizeCurve(life: Float): Float {
    val s = max(0f, min(min(1f, life / 0.18f), min(1f, (1f - life) / 0.22f)))
    return s * s * (3f - 2f * s)
}

/** SIZE over life: never below 62% of the flake's own radius, never above 100%. */
internal fun snowSizeOverLife(life: Float): Float = 0.62f + 0.38f * snowSizeCurve(life)

/** ALPHA over life, as a FRACTION of the plane's ceiling ([SnowPlaneCfg.alpha]).
 *  Bounded to 0.10 … 1.0, so `cfg.alpha` is a genuine ceiling and the darkest a
 *  flake ever gets is 10% of it — it fades, it never pops. */
internal fun snowAlphaOverLife(life: Float): Float = 0.10f + 0.90f * snowSizeCurve(life)

/**
 * The hard ceiling on ANY drawn flake alpha, after the plane ceiling, the life
 * curve and the Intensity dial have all had their say.
 *
 * The dial ([ParticleTuning.brightnessScale]) tops out at √2 ≈ 1.41, so at the
 * shipped ceilings the brightest thing on screen is 0.48 × 1.0 × 1.41 ≈ 0.68 and
 * this never binds. It exists so it CANNOT be crossed later: at SrcOver over
 * black, alpha IS the composited luminance, and a white flake core much past ~0.7
 * starts to approach #C9C9C9 body text — at which point a glyph sitting on a flake
 * loses its edge. A guard rail, not a tuning knob.
 */
internal const val SNOW_ALPHA_CAP = 0.72f

/**
 * The exact alpha [render] draws a flake with — extracted as a PURE function so
 * the legibility contract is assertable from a plain JVM test (render itself is a
 * DrawScope extension and is not).
 *
 * [alphaScale] is FieldPaints.alphaScale, the Intensity dial's BRIGHTNESS channel.
 * Snow bakes its own SrcOver paint and therefore bypasses [drawSpriteF], which is
 * where every other sprite field picks that multiply up for free — so it is
 * applied here by hand. Without this the dial only ever added more flakes of
 * identical faintness (Brandon, Fold, 2026-07-25).
 */
internal fun snowDrawAlpha(planeAlpha: Float, life: Float, alphaScale: Float): Float =
    min(SNOW_ALPHA_CAP, planeAlpha * snowAlphaOverLife(life) * alphaScale)

// -----------------------------------------------------------------------------
// Colour ramp — moonlit steel-blue high, near-white mid-fall, dusty settle low.
//
// The web resamples these 5 stops into a 48-entry LUT of `rgb(...)` strings so
// the draw loop allocates nothing. Here the same 48 entries index 48 TINTED
// SPRITES, baked lazily on first request (see SnowSprites). Keeping the step
// count identical keeps the ramp maths diffable against snow.js.
// -----------------------------------------------------------------------------
// The COLD ENDS are lifted toward white vs snow.js (brightness pass item 3, see
// the header). `life` indexes this ramp, so stop 0 is what a flake wears while it
// is fading IN at the top of the screen and stop 4 what it wears fading OUT at the
// bottom — i.e. the two stops that spend the most time at low envelope alpha. The
// web's rgb(150,176,210) at a quarter alpha over black composites to about
// rgb(38,44,53): grey dust. Lifting the ends keeps the same journey (steel-blue
// high → near-white mid-fall → dusty settle low) and the same COLD hue (blue ≥ red
// at every stop, which SnowFieldTest pins) while reading as snow rather than soot.
private val SNOW_RAMP = arrayOf(
    intArrayOf(188, 208, 234),      // web 150,176,210
    intArrayOf(220, 234, 250),      // web 200,220,242
    intArrayOf(246, 251, 255),      // web 238,247,255
    intArrayOf(232, 242, 253),      // web 216,232,249
    intArrayOf(204, 220, 240),      // web 178,198,224
)

internal const val SNOW_RAMP_STEPS = 48

/** Ramp stop [i] (0 … [SNOW_RAMP_STEPS] − 1) as packed 0xRRGGBB. Pure maths — no
 *  Android graphics — so the ramp is assertable from a plain JVM test. */
internal fun snowRampStop(i: Int): Int {
    val last = SNOW_RAMP.size - 1
    val u = (i.coerceIn(0, SNOW_RAMP_STEPS - 1).toFloat() / (SNOW_RAMP_STEPS - 1)) * last
    val a = min(last, u.toInt())
    val b = min(last, a + 1)
    val f = u - a
    val c0 = SNOW_RAMP[a]
    val c1 = SNOW_RAMP[b]
    val r = (c0[0] + (c1[0] - c0[0]) * f).roundToInt()
    val g = (c0[1] + (c1[1] - c0[1]) * f).roundToInt()
    val bl = (c0[2] + (c1[2] - c0[2]) * f).roundToInt()
    return (r shl 16) or (g shl 8) or bl
}

// -----------------------------------------------------------------------------
// The flake sprite.
//
// The stop list is a gaussian-ish SHOULDER rather than the shared atlas's tighter
// 0 / 0.35 / 1 curve (bakeRadialSprite) — an out-of-focus flake has no
// discernible edge at all, which is precisely what the arcs got wrong.
//
// The shoulder is WIDER than snow.js's [[0,1],[.22,.82],[.42,.46],[.62,.18],
// [.82,.05],[1,0]] — brightness pass item 2 in the header. Area-weighted
// (∫2u·α du over the disc) the web curve carries ~0.23 of peak alpha and this one
// ~0.36: about 1.6x the light per flake with the PEAK alpha unchanged, so the
// worst case for text contrast (a glyph sitting on a flake core) did not move at
// all. That matters here in a way it does not on the web: at density 3.1 a mid
// flake is ~5 device px across, so the web's outer ramp lands under 1.5 device px
// and antialiases into noise before alpha is ever applied. The tail still eases to
// zero, so there is still no edge to see.
// -----------------------------------------------------------------------------
private const val SNOW_SPRITE_PX = 64

/**
 * The flake falloff, core → rim, as parallel (stop, alpha) arrays.
 *
 * Kept as data rather than a literal inside the bake so SnowFieldTest can
 * INTEGRATE it: the number that decides whether a backdrop is legible is the
 * AREA-WEIGHTED MEAN alpha of a flake (a glyph stroke spans many device px), not
 * the single sub-pixel core, and that integral is the assertion that replaced the
 * old "never raise cfg.alpha" literal. Two arrays, not an Array<Pair>, so the
 * constant costs no boxed pairs at class-init.
 */
internal val SNOW_SPRITE_STOPS = floatArrayOf(0.00f, 0.30f, 0.50f, 0.70f, 0.88f, 1.00f)
internal val SNOW_SPRITE_ALPHAS = floatArrayOf(1.00f, 0.92f, 0.60f, 0.26f, 0.07f, 0.00f)

private fun bakeFlakeSprite(r: Int, g: Int, b: Int, density: Density): android.graphics.Bitmap {
    val bitmap = ImageBitmap(SNOW_SPRITE_PX, SNOW_SPRITE_PX)
    // Fully-qualified bitmap-backed Canvas factory (distinct from the @Composable Canvas).
    val canvas = androidx.compose.ui.graphics.Canvas(bitmap)
    val drawScope = CanvasDrawScope()
    val radius = SNOW_SPRITE_PX / 2f
    val center = Offset(radius, radius)
    val base = Color(r / 255f, g / 255f, b / 255f, 1f)
    drawScope.draw(
        density,
        LayoutDirection.Ltr,
        canvas,
        Size(SNOW_SPRITE_PX.toFloat(), SNOW_SPRITE_PX.toFloat()),
    ) {
        drawCircle(
            brush = Brush.radialGradient(
                // Built from the arrays above so the bake and the legibility test
                // can never drift apart. Allocates — and may: this runs at most 48
                // times per density, never in a frame.
                colorStops = Array<Pair<Float, Color>>(SNOW_SPRITE_STOPS.size) { i ->
                    SNOW_SPRITE_STOPS[i] to base.copy(alpha = SNOW_SPRITE_ALPHAS[i])
                },
                center = center,
                radius = radius,
            ),
            radius = radius,
            center = center,
        )
    }
    return bitmap.asAndroidBitmap()
}

/**
 * The 48 tinted flake sprites, baked ON FIRST REQUEST and never again — the same
 * lazy table snow.js keeps. Memoized in [FieldResources] under [SNOW_SPRITES_KEY],
 * so it survives every frame of the effect's life and dies with it.
 */
private class SnowSprites(private val density: Density) {
    private val cache = arrayOfNulls<android.graphics.Bitmap>(SNOW_RAMP_STEPS)

    fun get(i: Int): android.graphics.Bitmap {
        val hit = cache[i]
        if (hit != null) return hit
        val rgb = snowRampStop(i)
        val made = bakeFlakeSprite((rgb shr 16) and 0xFF, (rgb shr 8) and 0xFF, rgb and 0xFF, density)
        cache[i] = made
        return made
    }
}

private const val SNOW_SPRITES_KEY = "snow.sprites"
private const val SNOW_PAINT_KEY = "snow.paint"

/**
 * Snow's PRIVATE bitmap paint. Deliberately NOT [FieldPaints.sprite]: that one
 * carries [FIELD_BLEND] (additive from API 28 up), and additive is exactly what
 * a 250-flake white field behind body text must not do. Default Paint blending is
 * SrcOver, which is what we want, so there is nothing to set beyond filtering.
 */
private fun newSnowPaint(): android.graphics.Paint =
    android.graphics.Paint(android.graphics.Paint.FILTER_BITMAP_FLAG).apply { isAntiAlias = true }

/**
 * Blit one flake, centred, with a FLOAT destination — the same sub-pixel path
 * [drawSpriteF] takes (integer destinations make the slow far plane visibly STEP
 * on a 120 Hz panel), but through snow's own SrcOver paint instead of the shared
 * additive one. [dst] is [FieldPaints.dst], the per-overlay scratch rect.
 */
private fun DrawScope.drawFlake(
    bmp: android.graphics.Bitmap,
    cx: Float,
    cy: Float,
    rad: Float,
    a: Float,
    dst: android.graphics.RectF,
    paint: android.graphics.Paint,
) {
    val half = rad.coerceAtLeast(0.5f)          // ≥ 1 px wide, as drawSpriteF guarantees
    dst.set(cx - half, cy - half, cx + half, cy + half)
    paint.alpha = (a.coerceIn(0f, 1f) * 255f).roundToInt()
    drawContext.canvas.nativeCanvas.drawBitmap(bmp, null, dst, paint)
}

// -----------------------------------------------------------------------------
// One flake. A POOLED slot: recycled in place, never reallocated, so the hot path
// allocates nothing at all.
//
// UNLIKE the web, r / vy / swayAmp are stored in WEB CSS px (and px/s) and are
// multiplied by `g` at use — so a density or size change re-scales the live field
// for free instead of needing the web's rescale-every-field-on-resize pass.
// x and y are canvas coordinates and are therefore DEVICE px.
// -----------------------------------------------------------------------------
class SnowFlake internal constructor() {
    var x = 0f              // device px
    var y = 0f              // device px
    var r = 0f              // radius, web CSS px
    var vy = 0f             // fall speed, web CSS px/s  (always > 0: snow falls)
    var swayAmp = 0f        // flutter amplitude, web CSS px/s
    var swayW = 0f          // flutter angular frequency, rad/s
    var phase = 0f          // private flutter phase, radians
    var sizeEnv = 1f        // per-particle size-envelope multiplier (0.78 … 1.28)
    var bias = 0f           // per-particle colour-ramp offset (±0.14)
    var life = 0f           // FALL PROGRESS, 0 at the top margin → 1 at the bottom
}

/** One depth plane: its tuning + its pooled flakes. */
internal class SnowPlane(val cfg: SnowPlaneCfg) {
    /** Grown/trimmed IN PLACE on a resize; survivors keep their slot object. */
    var list: Array<SnowFlake> = emptyArray()
}

/**
 * SnowSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so SnowFieldTest can drive the whole
 * simulation from a seeded generator.
 */
class SnowSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f

    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    private val countScale = ParticleTuning.sanitizeScale(countScale)
    private var built = false

    private var planeArr = emptyArray<SnowPlane>()

    /** Test/inspection view of the planes. Never called from the hot path. */
    internal val planes: List<SnowPlane> get() = planeArr.asList()

    /** Test/inspection view of every flake, flattened. ALLOCATES — tests only. */
    val flakes: List<SnowFlake> get() = planeArr.flatMap { it.list.asList() }

    // ── Global gust state. ONE number for the WHOLE field — coherence is the
    //    entire point: 250 independently-gusting flakes is just noise. Stored in
    //    WEB CSS px/s (× g at use), and driven by an ACCUMULATED clock so a
    //    parked frame loop does not come back to a gust that was "due" all along.
    internal var gust = 0f
    internal var gustTarget = 0f
    internal var gustHold = 0f
    internal var gustWait = 0f

    /** Flutter clock, seconds, accumulated from dtSec (never read off nowMs). */
    private var clock = 0f

    /** Web CSS px → device px for THIS screen. Applied at use, never baked into
     *  stored state, so a density or size change re-scales the live field. */
    private val g: Float get() = scale * SNOW_WEB_TO_REF_PX

    private fun rnd(): Float = rand.next().toFloat()
    private fun pick(lo: Float, hi: Float): Float = lo + rnd() * (hi - lo)

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        // A live field must never re-scatter: drawWithCache re-runs this on every
        // composer-height change, and rebuilding would re-seed the whole field on
        // each keystroke.
        if (built && width == this.width && height == this.height && scale == this.scale) return
        this.width = width
        this.height = height
        this.scale = scale
        // Counts come from LOGICAL (dp) area — never from pixels, never from density.
        val counts = snowPlaneCounts(snowFieldScale((width / density) * (height / density), countScale))

        if (!built) {
            planeArr = Array(SNOW_PLANES.size) { i ->
                val pl = SnowPlane(SNOW_PLANES[i])
                pl.list = Array(counts[i]) { SnowFlake().also { f -> respawn(f, pl.cfg, initial = true) } }
                pl
            }
            scheduleGust()
            built = true
            return
        }

        for (i in planeArr.indices) {
            val pl = planeArr[i]
            val old = pl.list
            // A narrower viewport strands flakes off the right edge.
            for (j in old.indices) {
                val f = old[j]
                if (f.x > width) f.x = rnd() * width
            }
            // Only the count DELTA is spawned; survivors keep their slot, their
            // position and their flutter. (Geometry needs no rescale — it is
            // stored unscaled and multiplied by `g` at use.)
            if (old.size != counts[i]) {
                pl.list = Array(counts[i]) { j ->
                    if (j < old.size) old[j] else SnowFlake().also { f -> respawn(f, pl.cfg, initial = true) }
                }
            }
        }
    }

    /**
     * (Re)seed one flake IN PLACE.
     *
     * [initial] scatters it through the WHOLE column so the first frame is a
     * field, not a curtain arriving from off-screen; otherwise it re-enters just
     * above the top margin.
     */
    private fun respawn(f: SnowFlake, cfg: SnowPlaneCfg, initial: Boolean) {
        val m = SNOW_MARGIN * g
        f.x = rnd() * width
        f.y = if (initial) rnd() * (height + m * 2f) - m else -m - rnd() * height * 0.2f
        f.r = pick(cfg.sizeMin, cfg.sizeMax)
        f.vy = pick(cfg.fallMin, cfg.fallMax)
        f.swayAmp = pick(cfg.swayMin, cfg.swayMax)
        f.swayW = pick(cfg.swayWMin, cfg.swayWMax)
        f.phase = rnd() * SNOW_TAU
        // The curve is shared, the AMPLITUDE is not …
        f.sizeEnv = 0.78f + rnd() * 0.50f
        // … and the bias de-banks the shared colour ramp.
        f.bias = (rnd() - 0.5f) * 0.28f
        f.life = 0f
    }

    /** Park the gust: calm now, next gust no sooner than [SnowGust.WAIT_MIN]. */
    private fun scheduleGust() {
        gust = 0f
        gustTarget = 0f
        gustHold = 0f
        gustWait = pick(SnowGust.WAIT_MIN, SnowGust.WAIT_MAX)
    }

    /** Advance the global gust by [dt] seconds. */
    private fun stepGust(dt: Float) {
        if (gustHold > 0f) {
            gustHold -= dt
            if (gustHold <= 0f) {
                gustTarget = 0f
                gustWait = pick(SnowGust.WAIT_MIN, SnowGust.WAIT_MAX)
            }
        } else {
            gustWait -= dt
            if (gustWait <= 0f) {
                val mag = pick(SnowGust.STRENGTH_MIN, SnowGust.STRENGTH_MAX)
                gustTarget = (if (rnd() < 0.5f) -1f else 1f) * mag
                gustHold = pick(SnowGust.HOLD_MIN, SnowGust.HOLD_MAX)
            }
        }
        // Exponential approach, clamped so a long stall cannot overshoot the target.
        gust += (gustTarget - gust) * min(1f, SnowGust.EASE * dt)
    }

    /**
     * [active] is deliberately IGNORED: the pool is fixed and nothing spawns on
     * demand, so there is no generation-gated behaviour to gate. A backdrop that
     * froze mid-fall between turns would read as a hung screen. (This is the one
     * place snow diverges from the fireflies/stars "thin out when idle" idiom, and
     * it is the web module's decision, not a port slip.)
     *
     * [nowMs] is likewise unread — see the dt contract in the header.
     */
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (!built) return
        clock += dtSec
        stepGust(dtSec)
        val gg = g
        val m = SNOW_MARGIN * gg
        val span = max(1f, height + m * 2f)
        val lean = abs(gust) * SNOW_LEAN          // wind drives the field down a touch harder
        val wrap = width + m * 2f
        val bottom = height + m
        val t = clock
        for (i in planeArr.indices) {
            val pl = planeArr[i]
            val cfg = pl.cfg
            val list = pl.list
            // Per-plane constants hoisted out of the flake loop.
            val gustPx = gust * cfg.gust * gg
            val leanPx = lean * cfg.gust * gg
            for (j in list.indices) {
                val f = list[j]
                // Flutter is a VELOCITY, not a position offset: integrating the
                // sine gives the lazy S-path a flake actually falls in. A position
                // offset would make it a rigid metronome sliding down a rail.
                f.x += (sin(t * f.swayW + f.phase) * f.swayAmp * gg + gustPx) * dtSec
                f.y += (f.vy * gg + leanPx) * dtSec
                f.life = ((f.y + m) / span).coerceIn(0f, 1f)
                if (f.y > bottom) respawn(f, cfg, initial = false)
                else if (f.x < -m) f.x += wrap
                else if (f.x > width + m) f.x -= wrap
            }
        }
    }

    /** Nothing to revive — the field never empties. Just re-arm the gust clock, so
     *  a screen that was parked for an hour does not come back to a gust that was
     *  "due" the whole time. */
    override fun rearm() {
        if (!built) return
        scheduleGust()
    }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (!built) return
        // Hoisted: one hash probe per FRAME, never per particle. Neither touches
        // res.atlas — the shared warm-white ember stock cannot carry a cold ramp,
        // so snow bakes its own and the atlas is never built for this effect.
        val sprites = res.bake(SNOW_SPRITES_KEY) { d -> SnowSprites(d) }
        val paint = res.bake(SNOW_PAINT_KEY) { _ -> newSnowPaint() }
        val dst = paints.dst
        val gg = g
        val top = SNOW_RAMP_STEPS - 1
        // The Intensity dial's BRIGHTNESS channel. Hoisted: one field read per
        // frame, never per flake. Snow bakes its own SrcOver paint and so bypasses
        // drawSpriteF, which is where the rest of the catalogue picks this up —
        // without this line the dial only ever added more flakes of the same
        // faintness. See snowDrawAlpha.
        val aScale = paints.alphaScale
        for (i in planeArr.indices) {
            val pl = planeArr[i]
            val cfg = pl.cfg
            val list = pl.list
            // `soft` widens the SPRITE rather than adding halo rings: a blurrier
            // plane spreads the same light over a larger radius, which is what
            // being out of focus actually is.
            val spread = 1f + 0.85f * cfg.soft
            for (j in list.indices) {
                val f = list[j]
                // SIZE OVER LIFE × the flake's own envelope depth. Never constant.
                val r = f.r * gg * f.sizeEnv * snowSizeOverLife(f.life) * spread
                // ALPHA: the plane's ceiling × the same life curve × the Intensity
                // dial, hard-capped at SNOW_ALPHA_CAP. The ceilings are the
                // legibility budget — lift them only with a contrast argument and
                // a device check (see the header's brightness pass).
                val a = snowDrawAlpha(cfg.alpha, f.life, aScale)
                if (a <= 0.002f || r <= 0.05f) continue      // sub-pixel/invisible: skip the blit
                // COLOUR RAMP BY LIFE, de-banked by the flake's private bias.
                var idx = ((f.life + f.bias) * top).roundToInt()
                if (idx < 0) idx = 0 else if (idx > top) idx = top
                drawFlake(sprites.get(idx), f.x, f.y, r, a, dst, paint)
            }
        }
    }
}
