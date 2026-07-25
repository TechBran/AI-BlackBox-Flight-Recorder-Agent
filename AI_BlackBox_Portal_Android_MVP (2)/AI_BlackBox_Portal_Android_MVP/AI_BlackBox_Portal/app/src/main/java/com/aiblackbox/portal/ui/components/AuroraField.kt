package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: AURORA CURTAIN (id "aurora").
//
// A native port of the web module
//   Portal/modules/fx/effects/aurora.js
// (its physics tests: Portal/modules/fx/effects/aurora.test.mjs), REVAMPED
// 2026-07-25 after it shipped INVISIBLE on a Galaxy Fold 6 — see the post-mortem
// block below, which is the most important thing in this file.
//
// Slow vertical curtains of crimson-through-violet light hanging in the TOP half
// of the canvas, brightest at their base, folding through each other like silk.
//
// How the volumetric look is faked, and why each half of it exists:
//
//   ONE baked strip sprite per tint — a vertical alpha ramp (transparent →
//   accent → transparent) multiplied by a HORIZONTAL soft-edge window — blitted
//   28-46 times as narrow overlapping rays. Baking happens ONCE (via
//   FieldResources.bake), never per frame — a per-strip gradient Shader would be
//   ~46 Shader allocations a frame, which is the #1 perf trap this engine's
//   pre-baked-sprite idiom exists to avoid.
//
//   A 2-3 OCTAVE SINE SUM drives each strip's x-offset and height. The sum is
//   deliberately NOT smoothed: adjacent strips carry a per-strip PHASE STEP, so
//   the octaves fall out of alignment along x and the field breaks into the hard
//   vertical bands that read as "curtain". A single smooth octave — or one shared
//   offset for the whole sheet — reads as a drifting blob, which is the failure
//   mode this effect is most likely to regress into. Per-strip BRIGHTNESS
//   variance ([AuroraStrip.bright]) and a per-strip BASE offset
//   ([AuroraStrip.baseJit]) carry the same idea into tone and into the base line,
//   so neighbouring rays differ in intensity and the bottom edge is ragged
//   instead of one ruled horizontal line.
//
// ── POST-MORTEM: why the first port was invisible on device (Fold 6, 2026-07-25)
//
//   Brandon: "Aurora curtain — I don't see anything happening here at all."
//   He was right, and it was NOT a geometry bug. The strips were laid out
//   correctly, in the right band, at the right size, moving. They were simply
//   too dark to exist. Measured by replaying the shipped blit arithmetic for
//   1000 frames at 1080×2400:
//
//     • per-strip alpha handed to paint.alpha: median 0.0123, max 0.0275
//       → android.graphics.Paint takes an 8-BIT alpha, so that is 3/255 and
//         7/255. The baked ramp is also 8-bit and PREMULTIPLIED, so a gradient
//         stop of 0.30 became premul red 76, then × 3/255 = 0.9 → rounded to 1,
//         and every stop under ≈0.17 rounded to 0. More than half of every
//         curtain quantized to pure black before it ever reached the screen.
//         The web canvas composites in float and has no such floor — this loss
//         is Android-specific and is invisible in the .js.
//     • realized on-screen additive alpha: typical lit pixel 0.0149, i.e. RED
//       CHANNEL 4 out of 255 over the app's #000000 background. The single
//       brightest pixel across 1000 frames reached 18/255.
//
//   Root cause of the darkness: STRIP_ALPHA was defined as PEAK_ALPHA /
//   MAX_STACK, where MAX_STACK = 6 is the ANALYTIC WORST CASE — six strips
//   overlapping one column at once. That case is real but it is not what a pixel
//   sees, because six strips only stack that deep with their vertical gradients
//   at wildly different offsets. Measured, the peak column across 1000 frames
//   used 44% of the 0.165 budget and the TYPICAL lit pixel used 9% of it. The
//   effect was paying the worst case everywhere and got ~11× less light than its
//   own legibility budget actually permitted.
//
//   THE FIX (both halves are needed):
//     1. Per-strip alpha is now tuned DIRECTLY ([AURORA_STRIP_ALPHA]) for the
//        realized stack instead of being divided by the never-realized worst
//        case, and the ceiling itself was raised (see the contrast table below).
//     2. The ceiling is enforced at RUNTIME by a measured per-frame normalizer
//        ([AuroraSim.budgetGain]) instead of by arithmetic pessimism. update()
//        buckets every strip's alpha into a column histogram, using each
//        bucket's MAXIMUM window value (the window is unimodal, so that is an
//        exact upper bound of the true per-pixel column sum), and derives a gain
//        that can only ever DIM. The typical frame now lands just under the
//        budget instead of at 9% of it, and the hard ceiling still holds.
//
//   Result at 1080×2400, same replay: per-strip paint alpha median 15/255 (max
//   37/255 — clear of the quantization floor), realized peak pixel 52-70/255,
//   typical lit pixel 13/255. ~3.5× the light, and every strip survives 8-bit.
//
// ── LEGIBILITY BUDGET (non-negotiable — this is a backdrop behind chat text) ──
//
//   Curtains are large bright REGIONS rather than sparse particles, so the usual
//   "particles are small, it'll be fine" reasoning does not apply and the budget
//   is enforced arithmetically instead of by eye:
//
//     • every strip's bottom edge is clamped to AURORA_BAND_FRAC (top 55%) of the
//       canvas, so the light is always ABOVE the newest messages;
//     • the baked ramp's LAST stop is alpha 0, so the light has already reached
//       zero by the time it gets to that clamp line;
//     • additive blending means overlapping strips SUM, so the per-frame
//       normalizer above bounds the total additive alpha in any column to
//       AURORA_PEAK_ALPHA. AuroraFieldTest asserts the MEASURED column sum, not
//       the arithmetic — it drives the very same [AuroraSim.blit] the renderer
//       draws from, so the probe can never drift.
//
//   Contrast, computed against the app's real colours (#000000 background,
//   #C9C9C9 body text, WCAG 2.x relative luminance) for the WORST tint (accent
//   crimson, the most luminous of the five):
//
//     column alpha 0.165 (the old web ceiling) → 10.97 : 1
//     column alpha 0.320 (AURORA_PEAK_ALPHA)   →  8.44 : 1
//     column alpha 0.512 (0.320 × the 1.6 max  →  5.63 : 1
//                         of the Intensity dial's brightnessScale)
//     WCAG AA for body text needs                4.50 : 1
//
//   So the ceiling holds AA with margin even with the Intensity dial pinned, and
//   at the default dial it sits inside the 9.5-11.1 : 1 band the web fields were
//   measured at. Being too dim to see is ALSO a failure: an effect nobody can
//   see has no value and its budget is protecting nothing.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Everything here is either a
// FRACTION of the canvas (band height, base line, strip pitch, base wobble —
// automatically density-free) or a web CSS px length multiplied by [AuroraSim.g]
// at the moment it is used. Density reaches nothing else: not the strip count
// (that is dp-box derived, via apparentScale), not an alpha, not a rate. (Same
// law the web module states as "devicePixelRatio must NEVER reach particle
// geometry".)
//
// dt. Rates here are PER SECOND and are multiplied by dtSec directly — no
// `dt * 60` normalizer. The animation clock is a PRIVATE dt-accumulated Double
// ([AuroraSim.t]), never nowMs: every sine below is then a function of INTEGRATED
// time, so 600 frames at 60 Hz and 1200 at 120 Hz land on the same field, and a
// parked frame loop resumes the fold where it left it instead of teleporting the
// whole sheet to a new common phase.
//
// ── Deliberate divergences from aurora.js ──
//
// The cross-surface contract is the same APPARENT RESULT, not the same numbers:
// web constants are CSS px at an apparent scale near 1.0, Android multiplies by
// an apparent scale of 0.5 AND a density of 3.1, and Android's 8-bit paint alpha
// has a quantization floor the web's float compositor does not. Do NOT "restore
// parity" on any of these — each one was measured on the device.
//
//  1. ALPHA. PEAK_ALPHA 0.165 → 0.320 and STRIP_ALPHA is no longer
//     PEAK_ALPHA / MAX_STACK but a directly tuned 0.15, bounded at runtime by
//     the measured normalizer. Reason + measurements: the post-mortem above.
//  2. HORIZONTAL WINDOW. aurora.js bakes a strip 8 px wide with a purely
//     vertical gradient, i.e. a hard-edged rectangle of uniform-in-x light. At
//     the old 3/255 that was invisible; at the new 15-37/255 it is a picket
//     fence, because a 48-px-pitch strip stretched to ~83 device px puts a step
//     discontinuity every 49 px. [auroraXWindow] tapers each ray to zero at its
//     own edges, so rays cross-fade into each other and a crossing BRIGHTENS —
//     which is the "folding through each other" the effect is named for.
//  3. BASE WOBBLE. WOBBLE_PX = 9 CSS px is 1% of the 900 px reference box; on
//     Android geo clamps to 0.5, so it landed at 14 device px on a 2400 px
//     screen — 0.6% of height, i.e. no visible undulation at all. It is now a
//     FRACTION of the band ([AURORA_BASE_WOBBLE_FRAC]), which is also more
//     correct: it is a feature of the curtain, not of the pixel grid.
//  4. RAGGED BASE + PER-RAY BRIGHTNESS. Every strip in a web sheet shares one
//     baseY0 and one alpha ceiling, so the sheet's bottom edge is a ruled line
//     and its rays are uniformly bright — the "slab" read. Both are now
//     per-strip (see [AuroraStrip.baseJit] / [AuroraStrip.bright]).
//  5. The gradient is baked ONCE at a fixed AURORA_BAKE_H, not per resize at the
//     band height. The strip is a pure function of NORMALISED y, and the blit
//     scales the source into a float destination rect anyway, so a viewport-sized
//     bake buys nothing here — while FieldResources.bake is memoized per
//     (density, effect) with no invalidation hook, so a viewport-keyed bake would
//     leak a bitmap set per distinct height.
//  6. The Intensity dial. On the web it is `env.intensity` clamped to ≤ 1 in
//     draw(); on Android it arrives as FieldPaints.alphaScale (0.55-1.6×) and is
//     applied in render(). It can BRIGHTEN, which is the whole point of the
//     2026-07-25 dial fix — the contrast table above is what makes that safe.
//  7. Strip slots stay a dense, in-order 0..n-1 array across a resize (the web
//     appends new strips out of slot order). Additive blending is commutative, so
//     draw order is irrelevant either way; dense order just makes the pool
//     rebuild a plain `Array(n) { … }` with no compaction pass.
// =============================================================================

import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.BlendMode
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
import kotlin.math.floor
import kotlin.math.roundToInt
import kotlin.math.sin

private const val AURORA_TAU = 6.2831855f

/** Curtains live in the TOP fraction of the canvas. Nothing draws below it. */
internal const val AURORA_BAND_FRAC = 0.55f

/**
 * Ceiling on the TOTAL additive alpha any single column may accumulate.
 * 0.320 → 8.44 : 1 against #C9C9C9 body text, and 5.63 : 1 with the Intensity
 * dial pinned at its 1.6× brightness cap. See the contrast table in the header.
 * DIVERGENCE from aurora.js's 0.165 — measured, deliberate, do not "restore".
 */
internal const val AURORA_PEAK_ALPHA = 0.32f

/**
 * Analytic worst-case simultaneous strips over one column: strips sit one stripW
 * apart and are drawn at most OVERLAP = 1.7 stripW wide, so tiling alone stacks
 * at most 2 per sheet; each strip's x-offset is bounded to WOBBLE_FRAC = 0.5
 * stripW, so two neighbours can close by at most 1.0 stripW — one more overlap,
 * giving 3 per sheet, and two sheets ⇒ 6.
 *
 * This is still the honest geometric bound and a test pins it. What it is NO
 * LONGER is the divisor for [AURORA_STRIP_ALPHA]: dividing by a worst case that
 * a pixel essentially never sees is exactly what made the field invisible on the
 * Fold (post-mortem in the header). The ceiling is enforced at runtime instead,
 * by [AuroraSim.budgetGain].
 */
internal const val AURORA_MAX_STACK = 6

/**
 * Per-strip alpha ceiling — tuned directly against the REALIZED stack, not
 * derived from [AURORA_MAX_STACK].
 *
 * At 1080×2400 this puts the per-strip alpha handed to paint.alpha at a median
 * of 15/255 and a max of 37/255, i.e. clear of the 8-bit quantization floor that
 * erased the old 3/255 strips, while the measured column sum lands at 0.24-0.32
 * — just under, never over, [AURORA_PEAK_ALPHA]. Raising this is safe from a
 * legibility standpoint (the normalizer clamps) but costs dynamics: the more
 * frames the normalizer has to dim, the flatter the field's own breathing.
 */
internal const val AURORA_STRIP_ALPHA = 0.15f

/** Drawn width as a multiple of the slot pitch — strips cross-fade, no seams. */
internal const val AURORA_OVERLAP = 1.7f

/** Hard bound on |x-offset| as a fraction of stripW. MAX_STACK depends on it. */
internal const val AURORA_WOBBLE_FRAC = 0.5f

/**
 * Vertical wobble of a curtain's base, as a fraction of the BAND height.
 * DIVERGENCE from aurora.js's `WOBBLE_PX = 9` CSS px — see header note 3.
 */
internal const val AURORA_BASE_WOBBLE_FRAC = 0.035f

/** Below this a blit is invisible and costs a drawBitmap. Skip it. Dimensionless,
 *  so — unlike every length here — it is NOT scaled by g. */
private const val AURORA_MIN_ALPHA = 0.0006f

/** Degenerate-height cull, in WEB CSS px. A length, so it scales with g like the
 *  rest — otherwise a 2× density screen would cull a different set of strips. */
private const val AURORA_MIN_H = 0.5f

/** Baked strip size. Width now carries the horizontal window (header note 2), so
 *  it can no longer be 8 px; 48 upsamples cleanly to the ~83 device px a strip is
 *  drawn at. Height only sets resampling quality of a piecewise-linear ramp, so
 *  256 samples is already exact between stops. 48 × 256 × 4 B × 5 tints ≈ 245 KB,
 *  once, for the whole effect. */
private const val AURORA_BAKE_W = 48
private const val AURORA_BAKE_H = 256

/** Sample count for the baked horizontal window. [auroraXWindow] is smooth, so
 *  17 linearly-interpolated stops are visually exact. */
private const val AURORA_WIN_STOPS = 17

/**
 * Column buckets for the per-frame budget probe. Finer = a tighter (less
 * pessimistic) bound on the true per-pixel column sum, at ~n_strips × span
 * additions a frame. 128 over a 1080 px screen is ~8.4 px per bucket, ~10 buckets
 * per strip, ~360 adds a frame — noise next to 36 drawBitmap calls.
 */
private const val AURORA_HIST_COLS = 128

/** FieldResources key for the baked ramp. Namespaced by effect id, per the recipe. */
private const val AURORA_BANDS_KEY = "aurora.bands"

// -----------------------------------------------------------------------------
// apparentScale — the web engine's Portal/modules/fx/sizing.js, ported verbatim.
// GEOMETRY only: it takes the dp box (a CSS px IS a dp) and never density, which
// is exactly the separation that keeps the field the same apparent size on a 2.0
// tablet and a 3.5 phone.
// -----------------------------------------------------------------------------
private const val AURORA_REF_SHORT_EDGE_DP = 900f
private const val AURORA_APPARENT_MIN = 0.5f
private const val AURORA_APPARENT_MAX = 2.0f

internal fun auroraApparentScale(widthDp: Float, heightDp: Float): Float =
    (minOf(widthDp, heightDp) / AURORA_REF_SHORT_EDGE_DP)
        .coerceIn(AURORA_APPARENT_MIN, AURORA_APPARENT_MAX)

/**
 * Colour ramp, crimson (the Portal accent, rgb(255,74,74)) through magenta to
 * violet. Indexed by a strip's LIFE, so a curtain visibly cools as it folds shut.
 * The wrap from index-high back to index-low at life 1 → 0 is invisible because
 * [auroraFoldEnvelope] has already taken the alpha to zero at both ends.
 */
internal val AURORA_TINTS = arrayOf(
    intArrayOf(255, 74, 74),     // accent crimson
    intArrayOf(236, 62, 104),
    intArrayOf(198, 54, 146),    // magenta
    intArrayOf(150, 60, 190),
    intArrayOf(104, 68, 214),    // violet
)

/**
 * Alpha profile of the baked strip, top → bottom, as [offset, alpha].
 * The peak sits at 0.80 because an aurora is brightest at its BASE, and the final
 * stop MUST stay 0 — that zero is what keeps the light off the chat text.
 *
 * The shoulders are fatter than aurora.js's (0.22→0.30 / 0.55→0.62 became
 * 0.20→0.36 / 0.52→0.70): the shipped profile spent its whole upper third under
 * Android's 8-bit alpha floor, so the top of every curtain quantized to nothing
 * and what did survive read as a detached blob rather than a hanging sheet.
 */
internal val AURORA_GRAD_STOPS = arrayOf(
    floatArrayOf(0.00f, 0.00f),
    floatArrayOf(0.20f, 0.36f),
    floatArrayOf(0.52f, 0.70f),
    floatArrayOf(0.80f, 1.00f),
    floatArrayOf(0.93f, 0.36f),
    floatArrayOf(1.00f, 0.00f),
)

/**
 * HORIZONTAL profile of one ray, across its drawn width (u in 0..1).
 *
 * Unimodal, 1 at the centre, 0 at both edges, with a deliberately FLAT TOP
 * (mean 0.75, so a ray keeps most of its light) and steep shoulders. Two
 * properties the rest of the file depends on:
 *   • zero at u = 0 and u = 1 ⇒ no hard vertical seam where rays abut, which is
 *     what lets the brightness raise happen without a picket-fence look;
 *   • unimodal ⇒ the maximum over any interval is either the crest (if the
 *     interval spans u = 0.5) or the larger endpoint, which is what makes
 *     [AuroraSim.measureBudget]'s bucket bound EXACT rather than approximate.
 *
 * Baked into the sprite ([bakeAuroraBands]) and mirrored by the test probe, so
 * what is measured is what is drawn.
 */
internal fun auroraXWindow(u: Float): Float {
    if (!(u > 0f) || u >= 1f) return 0f                       // also catches NaN
    val d = abs(2f * u - 1f)
    return 1f - d * d * d
}

/**
 * THE TWO SUB-POPULATIONS. Not two copies with jitter — they differ in count,
 * fold rate, band position, height, octave frequencies, ramp region and
 * brightness, so the two sheets pass through each other at different speeds
 * instead of moving as one slab.
 *
 * The `o*` rows are the x octaves and the `h*` rows the height octaves, each
 * spelled out as (frequency Hz, amplitude, phase-step-per-strip) — the flattened
 * form of aurora.js's `oct` / `hOct` arrays, so the draw loop reads primitives off
 * one object instead of walking nested arrays.
 *
 * The x amplitudes of each sheet MUST sum to ≤ AURORA_WOBBLE_FRAC or MAX_STACK
 * lies (a test asserts it).
 */
internal class AuroraSheet(
    val key: String,
    val base: Int, val min: Int, val max: Int,
    val baseFrac: Float, val heightFrac: Float, val alphaMul: Float,
    /** Max UPWARD per-strip offset of the base line, as a fraction of height —
     *  the ragged bottom edge (header note 4). Never downward: the clamp line is
     *  the legibility contract and a strip may only ever sit further above it. */
    val baseJit: Float,
    val hue0: Float, val hueSpan: Float,
    val rateMin: Float, val rateMax: Float,
    val of0: Float, val ox0: Float, val os0: Float,
    val of1: Float, val ox1: Float, val os1: Float,
    val of2: Float, val ox2: Float, val os2: Float,
    val hf0: Float, val ha0: Float, val hs0: Float,
    val hf1: Float, val ha1: Float, val hs1: Float,
)

internal val AURORA_SHEETS = arrayOf(
    AuroraSheet(
        key = "crimson", base = 24, min = 18, max = 28,
        baseFrac = 0.52f, heightFrac = 0.78f, alphaMul = 1.00f, baseJit = 0.08f,
        // Rate ranges of the two sheets must not TOUCH (a test asserts it): a
        // violet strip folding at exactly crimson speed blurs the two-population
        // read that makes the field look layered rather than noisy.
        hue0 = 0.0f, hueSpan = 2.2f, rateMin = 0.030f, rateMax = 0.050f,
        of0 = 0.11f, ox0 = 0.30f, os0 = 0.55f,
        of1 = 0.27f, ox1 = 0.14f, os1 = 1.30f,
        of2 = 0.63f, ox2 = 0.06f, os2 = 2.70f,
        hf0 = 0.09f, ha0 = 0.22f, hs0 = 0.40f,
        hf1 = 0.23f, ha1 = 0.11f, hs1 = 1.10f,
    ),
    AuroraSheet(
        key = "violet", base = 15, min = 10, max = 18,
        baseFrac = 0.40f, heightFrac = 0.62f, alphaMul = 0.85f, baseJit = 0.07f,
        hue0 = 2.0f, hueSpan = 2.0f, rateMin = 0.055f, rateMax = 0.095f,
        of0 = 0.17f, ox0 = 0.26f, os0 = 0.90f,
        of1 = 0.41f, ox1 = 0.16f, os1 = 2.10f,
        of2 = 0.88f, ox2 = 0.08f, os2 = 3.60f,
        hf0 = 0.14f, ha0 = 0.26f, hs0 = 0.75f,
        hf1 = 0.35f, ha1 = 0.13f, hs1 = 1.90f,
    ),
)

/** Fast swell, long ebb. 0 at both ends, 1 at the crest. */
private const val AURORA_FOLD_RISE = 0.28f

/**
 * SIZE-OVER-LIFE curve. A curtain opens quickly and closes slowly; the returned
 * 0..1 envelope drives height, width and alpha together so a fold fades out
 * instead of popping. Multiplied per strip by its own `env0` at the call site —
 * the curve is the shape, `env0` is that strip's personal amplitude, so no two
 * curtains open to the same size.
 */
internal fun auroraFoldEnvelope(t: Float): Float {
    if (!(t > 0f) || t >= 1f) return 0f                       // also catches NaN
    if (t < AURORA_FOLD_RISE) {
        val u = t / AURORA_FOLD_RISE
        return u * u * (3f - 2f * u)
    }
    val u = 1f - (t - AURORA_FOLD_RISE) / (1f - AURORA_FOLD_RISE)
    return u * u * (3f - 2f * u)
}

/**
 * Strips per sheet. GEOMETRY-derived (never density), then put through the user's
 * count dial and clamped, so the curtain banding period stays readable on a phone
 * and doesn't turn to fringe on a 4K panel. The clamp is what keeps the whole
 * field inside its 24-48 population band at every corner of the dial.
 *
 * The pre-dial term is computed in Double so it rounds byte-identically to
 * aurora.js's `stripCount` (Float would put base × 0.9 on the wrong side of .5).
 */
internal fun auroraStripCount(sh: AuroraSheet, geo: Float, countScale: Float): Int {
    val base = (sh.base * (0.80 + 0.20 * geo)).roundToInt()
    return ParticleTuning.scaleCount(base, countScale).coerceIn(sh.min, sh.max)
}

/**
 * Bake one strip sprite per tint, ONCE. Called through FieldResources.bake, so
 * this runs on the first frame of the effect's life and never again.
 *
 * TWO passes, and the second one is the point: the vertical ramp is laid down
 * first, then the horizontal window is multiplied INTO its alpha with
 * BlendMode.DstIn (dstAlpha × srcAlpha). That is what turns a hard-edged
 * rectangle into a soft-edged ray — see header note 2. Doing it in the bake keeps
 * the hot loop at one drawBitmap per strip with zero shaders.
 */
private fun bakeAuroraBands(density: Density): Array<android.graphics.Bitmap> {
    // Shared by all five tints; white so DstIn reads only its alpha channel.
    val window = Array(AURORA_WIN_STOPS) { i ->
        val u = i / (AURORA_WIN_STOPS - 1f)
        u to Color(1f, 1f, 1f, auroraXWindow(u))
    }
    return Array(AURORA_TINTS.size) { i ->
        val c = AURORA_TINTS[i]
        val bitmap = ImageBitmap(AURORA_BAKE_W, AURORA_BAKE_H)
        // Fully-qualified bitmap-backed Canvas factory (distinct from @Composable Canvas).
        val canvas = androidx.compose.ui.graphics.Canvas(bitmap)
        val base = Color(c[0] / 255f, c[1] / 255f, c[2] / 255f, 1f)
        val stops = Array(AURORA_GRAD_STOPS.size) { s ->
            AURORA_GRAD_STOPS[s][0] to base.copy(alpha = AURORA_GRAD_STOPS[s][1])
        }
        val w = AURORA_BAKE_W.toFloat()
        val h = AURORA_BAKE_H.toFloat()
        CanvasDrawScope().draw(density, LayoutDirection.Ltr, canvas, Size(w, h)) {
            drawRect(brush = Brush.verticalGradient(colorStops = stops, startY = 0f, endY = h))
            drawRect(
                brush = Brush.horizontalGradient(colorStops = window, startX = 0f, endX = w),
                blendMode = BlendMode.DstIn,
            )
        }
        bitmap.asAndroidBitmap()
    }
}

/**
 * One strip. Every field the hot loop touches is flattened onto this object at
 * build/resize time — [AuroraSim.blit] does arithmetic on numbers only, with no
 * nested lookups into [AURORA_SHEETS] and no allocation.
 *
 * A POOLED slot: a resize keeps the surviving strips (and therefore their fold
 * phase); slots are never reallocated per frame.
 */
class AuroraStrip internal constructor(val sheet: Int, val slot: Int) {
    var life = 0f          // 0 → 1, wraps ── the fold clock
    var rate = 0f          // life per SECOND
    var env0 = 1f          // per-strip size-envelope multiplier
    /** Per-strip brightness multiplier, 0.55..1. Neighbouring rays differ in TONE,
     *  not just in phase — the other half of the "banding, not blob" read. */
    var bright = 1f
    /** Per-strip upward offset of the base line, a fraction of canvas height. */
    var baseJit = 0f
    // x octaves: frequency (rad/s) + phase (random + per-strip step ⇒ travelling wave)
    var f0 = 0f; var p0 = 0f
    var f1 = 0f; var p1 = 0f
    var f2 = 0f; var p2 = 0f
    // height octaves
    var g0 = 0f; var q0 = 0f; var ah0 = 0f
    var g1 = 0f; var q1 = 0f; var ah1 = 0f
    // base wobble + shimmer
    var fy = 0f; var py = 0f
    var fs = 0f; var ps = 0f
    var hueLo = 0f; var hueSpan = 0f
    var alphaMax = 0f
    // filled by layout() — all device px
    var stripW = 0f; var slotX = 0f; var drawW = 0f
    var hMax = 0f; var baseY0 = 0f; var ay = 0f
    var ax0 = 0f; var ax1 = 0f; var ax2 = 0f
}

/**
 * AuroraSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so AuroraFieldTest can drive the whole
 * simulation from a seeded generator. rand() is drawn ONLY at build/resize — the
 * frame loop is pure arithmetic, which is what makes the physics reproducible.
 */
class AuroraSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    /** apparentScale: GEOMETRY only, from the dp box. Drives count + wobble. */
    private var geo = AURORA_APPARENT_MIN
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<AuroraStrip>()
    private var counts0 = 0
    private var counts1 = 0

    /** The dt-accumulated animation clock, seconds. Double: a Float would lose
     *  the sub-frame increment after an hour of uptime. */
    private var t = 0.0

    private var bandBottom = 0f
    private var minDrawH = 0f

    /** Draw scratch, allocated ONCE — [render] must not allocate. */
    private val scratch = FloatArray(6)

    /** Budget-probe scratch, separate from [scratch] so update() and a caller
     *  holding a blit result can never tread on each other. */
    private val probeOut = FloatArray(6)

    /** Column histogram for the per-frame budget probe. Fixed size, filled in
     *  place — [update] must not allocate either. */
    private val hist = FloatArray(AURORA_HIST_COLS)

    private var budgetK = 1f

    /** Test/inspection view of the pool. Never called from the hot path. */
    val strips: List<AuroraStrip> get() = pool.asList()

    /** The integrated clock, for the frame-rate-independence test. */
    val clock: Double get() = t

    /**
     * The measured legibility gain for the CURRENT frame, in (0, 1].
     *
     * This is the runtime half of the alpha budget (post-mortem in the header):
     * [update] measures what the frame is about to put into each column and, if
     * that busts [AURORA_PEAK_ALPHA], returns the ratio that brings it back to
     * the ceiling. It can only ever DIM — 1.0 means the field is under budget on
     * its own, which is the common case by design.
     */
    val budgetGain: Float get() = budgetK

    /** Web CSS px → device px for THIS screen. GEOMETRY × dp→px, applied at use and
     *  never baked into stored state, so a density or size change re-scales the
     *  live field. On a phone-shaped viewport geo clamps to 0.5, making this the
     *  1.55 × scale that FirefliesField.kt hard-codes as WEB_TO_REF_PX. */
    private val g: Float get() = geo * FIELD_REFERENCE_DENSITY * scale

    private fun rnd(): Float = rand.next().toFloat()

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f || density <= 0f) return
        // The Canvas calls this every time it re-caches; a live field must never
        // re-scatter (and a rebuild here would restart every curtain mid-fold).
        if (pool.isNotEmpty() && width == this.width && height == this.height && scale == this.scale) return
        this.width = width; this.height = height; this.scale = scale
        this.geo = auroraApparentScale(width / density, height / density)
        reconcile()
        layout()
        // The Canvas can draw once before the frame loop's first update() — without
        // this the very first frame of a new size would draw at an unmeasured gain.
        measureBudget()
    }

    /**
     * Grow/trim the pool to the wanted per-sheet counts, IN PLACE. Survivors keep
     * their slot object and therefore their fold phase, because the composer's
     * resize fires on every keystroke that changes its height and a rebuild there
     * would blink the whole curtain once a second while typing.
     */
    private fun reconcile() {
        val want0 = auroraStripCount(AURORA_SHEETS[0], geo, countScale)
        val want1 = auroraStripCount(AURORA_SHEETS[1], geo, countScale)
        if (pool.isNotEmpty() && want0 == counts0 && want1 == counts1) return
        val old = pool
        val have0 = counts0
        val have1 = counts1
        // Slots stay a DENSE, in-order 0..n-1 range per sheet, so the survivor for
        // any (sheet, slot) is at a computable index in the old array.
        pool = Array(want0 + want1) { i ->
            val sheet = if (i < want0) 0 else 1
            val slot = if (sheet == 0) i else i - want0
            val have = if (sheet == 0) have0 else have1
            val oldIdx = if (sheet == 0) slot else have0 + slot
            if (slot < have && oldIdx < old.size) old[oldIdx] else makeStrip(sheet, slot)
        }
        counts0 = want0
        counts1 = want1
    }

    /** Build one strip. The ONLY place rand() is drawn. */
    private fun makeStrip(sheetIdx: Int, slot: Int): AuroraStrip {
        val sh = AURORA_SHEETS[sheetIdx]
        val n = slot.toFloat()
        val p = AuroraStrip(sheetIdx, slot)
        p.life = rnd()                                       // staggered: sheets never pulse in unison
        p.rate = sh.rateMin + rnd() * (sh.rateMax - sh.rateMin)
        p.env0 = 0.78f + rnd() * 0.44f                       // per-strip envelope multiplier
        p.f0 = sh.of0 * AURORA_TAU; p.p0 = rnd() * AURORA_TAU + n * sh.os0
        p.f1 = sh.of1 * AURORA_TAU; p.p1 = rnd() * AURORA_TAU + n * sh.os1
        p.f2 = sh.of2 * AURORA_TAU; p.p2 = rnd() * AURORA_TAU + n * sh.os2
        p.g0 = sh.hf0 * AURORA_TAU; p.q0 = rnd() * AURORA_TAU + n * sh.hs0; p.ah0 = sh.ha0
        p.g1 = sh.hf1 * AURORA_TAU; p.q1 = rnd() * AURORA_TAU + n * sh.hs1; p.ah1 = sh.ha1
        p.fy = (0.07f + rnd() * 0.05f) * AURORA_TAU; p.py = rnd() * AURORA_TAU
        p.fs = (0.13f + rnd() * 0.10f) * AURORA_TAU; p.ps = rnd() * AURORA_TAU
        p.hueLo = sh.hue0 + (rnd() - 0.5f) * 0.4f
        p.hueSpan = sh.hueSpan
        p.bright = 0.55f + rnd() * 0.45f                     // per-ray tone → banding
        p.baseJit = rnd() * sh.baseJit                       // ragged bottom edge
        p.alphaMax = AURORA_STRIP_ALPHA * sh.alphaMul
        return p
    }

    /** Resolve every viewport-dependent quantity. Everything below is either a
     *  FRACTION of the canvas (density-free by construction) or a web CSS px
     *  length × g — the two spellings of the DPI law. */
    private fun layout() {
        val bandH = maxOf(1f, height * AURORA_BAND_FRAC)
        bandBottom = height * AURORA_BAND_FRAC
        minDrawH = AURORA_MIN_H * g
        for (p in pool) {
            val sh = AURORA_SHEETS[p.sheet]
            val sw = width / (if (p.sheet == 0) counts0 else counts1)
            p.stripW = sw
            p.slotX = (p.slot + 0.5f) * sw
            p.drawW = sw * AURORA_OVERLAP
            p.hMax = bandH * sh.heightFrac
            p.baseY0 = height * (sh.baseFrac - p.baseJit)
            p.ay = bandH * AURORA_BASE_WOBBLE_FRAC
            p.ax0 = sh.ox0 * sw
            p.ax1 = sh.ox1 * sw
            p.ax2 = sh.ox2 * sw
        }
    }

    /**
     * Advance the field. [active] is deliberately IGNORED: an aurora is ambient,
     * so it does not restart (or thin out) when a turn does — the overlay's alpha
     * fade and its parked frame loop are what turn the field off. Re-staggering
     * every life on activation would blink the whole sheet.
     */
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty()) return
        t += dtSec.toDouble()
        for (p in pool) {
            p.life += p.rate * dtSec
            if (p.life >= 1f) p.life -= floor(p.life)     // wrap; survives a long stall
        }
        measureBudget()
    }

    /**
     * THE LEGIBILITY ENFORCER. Bucket every strip's alpha into a column histogram
     * and derive the gain that keeps the worst column at or under
     * [AURORA_PEAK_ALPHA].
     *
     * The bound is EXACT, not sampled: for each bucket the strip contributes its
     * alpha times the MAXIMUM the ray's horizontal window reaches anywhere inside
     * that bucket. [auroraXWindow] is unimodal, so that maximum is the crest when
     * the bucket spans u = 0.5 and the larger endpoint otherwise. A per-pixel
     * probe can therefore never measure more than this histogram did — which is
     * what makes the test's measured-column assertion a real guarantee rather
     * than a lucky sampling.
     *
     * It also ignores y entirely (a strip contributes to a column over its whole
     * width regardless of where it hangs) and ignores the vertical ramp (≤ 1),
     * both of which only ever OVER-estimate. Conservative in the safe direction.
     */
    private fun measureBudget() {
        budgetK = 1f
        if (width <= 0f) return
        hist.fill(0f)
        val colW = width / AURORA_HIST_COLS
        val o = probeOut
        for (i in pool.indices) {
            if (!resolve(i, o)) continue
            val left = o[0]
            val w = o[2] - left
            if (!(w > 0f)) continue
            val a = o[4]
            var c = floor(left / colW).toInt()
            if (c < 0) c = 0
            var hi = floor(o[2] / colW).toInt()
            if (hi > AURORA_HIST_COLS - 1) hi = AURORA_HIST_COLS - 1
            while (c <= hi) {
                val uLo = (c * colW - left) / w
                val uHi = ((c + 1) * colW - left) / w
                val wMax = if (uLo <= 0.5f && uHi >= 0.5f) 1f
                    else maxOf(auroraXWindow(uLo), auroraXWindow(uHi))
                hist[c] += a * wMax
                c++
            }
        }
        var peak = 0f
        for (v in hist) if (v > peak) peak = v
        if (peak > AURORA_PEAK_ALPHA) budgetK = AURORA_PEAK_ALPHA / peak
    }

    /** A curtain does not restart when a turn does. Rearm therefore only guarantees
     *  the field EXISTS — which [resize] already does, since the pool is built
     *  there. Nothing to disturb. */
    override fun rearm() { /* ambient by design — see update() */ }

    /** Strip [index]'s horizontal fold offset at clock [tSec], in device px. The
     *  ONE implementation — [blit] calls it too, so a test can never drift from
     *  what is actually drawn. Bounded by construction to WOBBLE_FRAC × stripW. */
    fun foldOffset(index: Int, tSec: Double): Float {
        val s = pool[index]
        return (s.ax0 * sin(tSec * s.f0 + s.p0) +
            s.ax1 * sin(tSec * s.f1 + s.p1) +
            s.ax2 * sin(tSec * s.f2 + s.p2)).toFloat()
    }

    /**
     * Strip [index]'s blit BEFORE the frame's budget gain — the raw geometry and
     * alpha the effect wants. Split out from [blit] so [measureBudget] and the
     * renderer run identical arithmetic; nothing outside this class needs it.
     */
    private fun resolve(index: Int, out: FloatArray): Boolean {
        val s = pool[index]
        val fold = auroraFoldEnvelope(s.life)
        if (fold <= 0f) return false
        val tt = t
        // 3-octave sine sum → the horizontal fold.
        val xo = foldOffset(index, tt)
        // 2-octave sine sum → the height ripple that makes the base uneven.
        val hw = s.ah0 * sin(tt * s.g0 + s.q0) + s.ah1 * sin(tt * s.g1 + s.q1)
        // size-over-life curve × per-strip envelope multiplier × ripple
        val h = (s.hMax * (0.42f + 0.58f * fold) * s.env0 * (1.0 + hw)).toFloat()
        if (!(h > minDrawH)) return false
        val w = s.drawW * (0.76f + 0.24f * fold)
        var baseY = s.baseY0 + (s.ay * sin(tt * s.fy + s.py)).toFloat()
        if (baseY > bandBottom) baseY = bandBottom            // THE legibility clamp
        val a = (s.alphaMax * s.bright * (0.30f + 0.70f * fold) *
            (0.78 + 0.22 * sin(tt * s.fs + s.ps))).toFloat()
        if (a < AURORA_MIN_ALPHA) return false
        // COLOUR RAMP indexed by life: crimson at the fold's open, violet at its
        // close, each sheet over its own region of the 5-tint ramp.
        val idx = (s.hueLo + s.hueSpan * s.life + 0.5f).toInt()
            .coerceIn(0, AURORA_TINTS.size - 1)
        val left = s.slotX + xo - w * 0.5f
        out[0] = left
        out[1] = baseY - h
        out[2] = left + w
        out[3] = baseY
        out[4] = a
        out[5] = idx.toFloat()
        return true
    }

    /**
     * Resolve strip [index]'s blit for the CURRENT frame into [out] as
     * `(left, top, right, bottom, alpha, tintIndex)`; returns false when the strip
     * contributes nothing. Pure arithmetic — no allocation, no state mutation — so
     * [render] and AuroraFieldTest's legibility probe run the EXACT same code and
     * the measured alpha budget is the one that reaches the screen.
     *
     * `alpha` already carries [budgetGain], and is the alpha of a ray at its
     * horizontal CREST: the drawn sprite tapers it by [auroraXWindow] across the
     * width, which is why the test probe weights by that same window.
     */
    fun blit(index: Int, out: FloatArray): Boolean {
        if (!resolve(index, out)) return false
        val a = out[4] * budgetK
        if (a < AURORA_MIN_ALPHA) return false
        out[4] = a
        return true
    }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (pool.isEmpty()) return
        // The Intensity dial's BRIGHTNESS multiplier. This effect draws with its
        // own paint rather than through drawSpriteF, so it has to apply alphaScale
        // itself — otherwise the dial only ever changed the strip COUNT and a
        // maxed-out slider looked identical to a default one.
        val gain = paints.alphaScale
        if (!(gain > 0f)) return
        // Hoisted: one hash probe (and, on the first frame, one bake) per FRAME.
        val bands = res.bake(AURORA_BANDS_KEY) { d -> bakeAuroraBands(d) }
        val nc = drawContext.canvas.nativeCanvas
        val paint = paints.sprite          // additive blend + bitmap filtering, set once
        val dst = paints.dst
        val o = scratch
        for (i in pool.indices) {
            if (!blit(i, o)) continue
            dst.set(o[0], o[1], o[2], o[3])
            // 8-bit paint alpha is the platform's globalAlpha, and it is the reason
            // this effect shipped invisible: it floors anything under 1/510 to
            // nothing. The strip alphas above are now tuned to land at 15-37/255,
            // well clear of that floor. It can only ever cost light, never add it,
            // so the column budget stays sound.
            val q = ((o[4] * gain).coerceIn(0f, 1f) * 255f).roundToInt()
            if (q <= 0) continue
            paint.alpha = q
            nc.drawBitmap(bands[o[5].toInt()], null, dst, paint)
        }
    }
}
