package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: AURORA CURTAIN (id "aurora").
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/aurora.js
// (its physics tests: Portal/modules/fx/effects/aurora.test.mjs).
//
// Slow vertical curtains of crimson-through-violet light hanging in the TOP half
// of the canvas, brightest at their base, folding through each other like silk.
//
// How the volumetric look is faked, and why each half of it exists:
//
//   ONE baked vertical gradient per tint (transparent → accent → transparent),
//   blitted 28-46 times as narrow overlapping strips. Baking happens ONCE (via
//   FieldResources.bake), never per frame — a per-strip gradient Shader would be
//   ~46 Shader allocations a frame, which is the #1 perf trap this engine's
//   pre-baked-sprite idiom exists to avoid.
//
//   A 2-3 OCTAVE SINE SUM drives each strip's x-offset and height. The sum is
//   deliberately NOT smoothed: adjacent strips carry a per-strip PHASE STEP, so
//   the octaves fall out of alignment along x and the field breaks into the hard
//   vertical bands that read as "curtain". A single smooth octave — or one shared
//   offset for the whole sheet — reads as a drifting blob, which is the failure
//   mode this effect is most likely to regress into.
//
// ── LEGIBILITY BUDGET (non-negotiable — this is a backdrop behind chat text) ──
//
//   Curtains are large bright REGIONS rather than sparse particles, so the usual
//   "particles are small, it'll be fine" reasoning does not apply and the budget
//   is enforced arithmetically instead of by eye:
//
//     • every strip's bottom edge is clamped to AURORA_BAND_FRAC (top 55%) of the
//       canvas, so nothing is ever drawn over the message area;
//     • the baked gradient's LAST stop is alpha 0, so the light has already
//       reached zero by the time it gets to that clamp line;
//     • additive blending means overlapping strips SUM, so the per-strip alpha is
//       AURORA_PEAK_ALPHA / AURORA_MAX_STACK, where MAX_STACK is the analytic
//       worst case derived below. The total additive alpha in any column can
//       therefore never exceed AURORA_PEAK_ALPHA (16.5%). AuroraFieldTest asserts
//       the MEASURED column sum, not the arithmetic — it drives the very same
//       [AuroraSim.blit] the renderer draws from, so the probe can never drift.
//
// The MAX_STACK derivation (keep this true if you retune OVERLAP or WOBBLE_FRAC):
// strips sit one stripW apart and are drawn at most OVERLAP=1.7 stripW wide, so
// tiling alone stacks at most 2 per sheet. Each strip's x-offset is bounded to
// WOBBLE_FRAC=0.5 stripW, so two neighbours can close by at most 1.0 stripW —
// one more overlap, giving 3 per sheet. Two sheets ⇒ 6.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Everything here is either a
// FRACTION of the canvas (band height, base line, strip pitch — automatically
// density-free) or a web CSS px length multiplied by [AuroraSim.g] at the moment
// it is used. Density reaches nothing else: not the strip count (that is dp-box
// derived, via apparentScale), not an alpha, not a rate. (Same law the web module
// states as "devicePixelRatio must NEVER reach particle geometry".)
//
// dt. Rates here are PER SECOND and are multiplied by dtSec directly — no
// `dt * 60` normalizer. The animation clock is a PRIVATE dt-accumulated Double
// ([AuroraSim.t]), never nowMs: every sine below is then a function of INTEGRATED
// time, so 600 frames at 60 Hz and 1200 at 120 Hz land on the same field, and a
// parked frame loop resumes the fold where it left it instead of teleporting the
// whole sheet to a new common phase.
//
// ── Deliberate divergences from aurora.js (everything else is byte-identical) ──
//
//  1. The gradient is baked ONCE at a fixed AURORA_BAKE_H, not per resize at the
//     band height. The strip is a pure function of NORMALISED y, and the blit
//     scales the source into a float destination rect anyway, so a viewport-sized
//     bake buys nothing here — while FieldResources.bake is memoized per
//     (density, effect) with no invalidation hook, so a viewport-keyed bake would
//     leak a bitmap set per distinct height.
//  2. No `env.intensity` gain in the draw. On Android the user's intensity dial is
//     folded into countScale (strip COUNT) and the fade is the overlay's
//     graphicsLayer alpha (ModulateAlpha, so it multiplies per draw op and is
//     always ≤ 1). Both routes can only ever DIM the field, so the budget holds.
//  3. Strip slots stay a dense, in-order 0..n-1 array across a resize (the web
//     appends new strips out of slot order). Additive blending is commutative, so
//     draw order is irrelevant either way; dense order just makes the pool
//     rebuild a plain `Array(n) { … }` with no compaction pass.
// =============================================================================

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
import kotlin.math.floor
import kotlin.math.roundToInt
import kotlin.math.sin

private const val AURORA_TAU = 6.2831855f

/** Curtains live in the TOP fraction of the canvas. Nothing draws below it. */
internal const val AURORA_BAND_FRAC = 0.55f

/** Ceiling on the TOTAL additive alpha any single column may accumulate. */
internal const val AURORA_PEAK_ALPHA = 0.165f

/** Analytic worst-case simultaneous strips over one column (see header). */
internal const val AURORA_MAX_STACK = 6

/** Per-strip alpha ceiling. The budget, divided by the worst case. */
internal const val AURORA_STRIP_ALPHA = AURORA_PEAK_ALPHA / AURORA_MAX_STACK

/** Drawn width as a multiple of the slot pitch — strips cross-fade, no seams. */
internal const val AURORA_OVERLAP = 1.7f

/** Hard bound on |x-offset| as a fraction of stripW. MAX_STACK depends on it. */
internal const val AURORA_WOBBLE_FRAC = 0.5f

/** Vertical wobble of a curtain's base, in WEB CSS px (scaled by [AuroraSim.g]). */
private const val AURORA_WOBBLE_PX = 9f

/** Below this a blit is invisible and costs a drawBitmap. Skip it. Dimensionless,
 *  so — unlike every length here — it is NOT scaled by g. */
private const val AURORA_MIN_ALPHA = 0.0006f

/** Degenerate-height cull, in WEB CSS px. A length, so it scales with g like the
 *  rest — otherwise a 2× density screen would cull a different set of strips. */
private const val AURORA_MIN_H = 0.5f

/** Baked strip size. The gradient is purely vertical, so the width can be tiny;
 *  the height only sets resampling quality, since the blit scales it (see the
 *  divergence note in the header). 8 × 512 × 4 B × 5 tints ≈ 80 KB, once. */
private const val AURORA_BAKE_W = 8
private const val AURORA_BAKE_H = 512

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
 */
internal val AURORA_GRAD_STOPS = arrayOf(
    floatArrayOf(0.00f, 0.00f),
    floatArrayOf(0.22f, 0.30f),
    floatArrayOf(0.55f, 0.62f),
    floatArrayOf(0.80f, 1.00f),
    floatArrayOf(0.93f, 0.34f),
    floatArrayOf(1.00f, 0.00f),
)

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
        baseFrac = 0.52f, heightFrac = 0.78f, alphaMul = 1.00f,
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
        baseFrac = 0.40f, heightFrac = 0.62f, alphaMul = 0.85f,
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
 * Bake one vertical gradient per tint, ONCE. Called through FieldResources.bake,
 * so this runs on the first frame of the effect's life and never again.
 */
private fun bakeAuroraBands(density: Density): Array<android.graphics.Bitmap> =
    Array(AURORA_TINTS.size) { i ->
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
        }
        bitmap.asAndroidBitmap()
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

    /** Test/inspection view of the pool. Never called from the hot path. */
    val strips: List<AuroraStrip> get() = pool.asList()

    /** The integrated clock, for the frame-rate-independence test. */
    val clock: Double get() = t

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
        p.alphaMax = AURORA_STRIP_ALPHA * sh.alphaMul
        return p
    }

    /** Resolve every viewport-dependent quantity. Everything below is either a
     *  FRACTION of the canvas (density-free by construction) or a web CSS px
     *  length × g — the two spellings of the DPI law. */
    private fun layout() {
        val bandH = maxOf(1f, height * AURORA_BAND_FRAC)
        bandBottom = height * AURORA_BAND_FRAC
        val gg = g
        minDrawH = AURORA_MIN_H * gg
        for (p in pool) {
            val sh = AURORA_SHEETS[p.sheet]
            val sw = width / (if (p.sheet == 0) counts0 else counts1)
            p.stripW = sw
            p.slotX = (p.slot + 0.5f) * sw
            p.drawW = sw * AURORA_OVERLAP
            p.hMax = bandH * sh.heightFrac
            p.baseY0 = height * sh.baseFrac
            p.ay = AURORA_WOBBLE_PX * gg
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
     * Resolve strip [index]'s blit for the CURRENT frame into [out] as
     * `(left, top, right, bottom, alpha, tintIndex)`; returns false when the strip
     * contributes nothing. Pure arithmetic — no allocation, no state mutation — so
     * [render] and AuroraFieldTest's legibility probe run the EXACT same code and
     * the measured alpha budget is the one that reaches the screen.
     */
    fun blit(index: Int, out: FloatArray): Boolean {
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
        val a = (s.alphaMax * (0.30f + 0.70f * fold) *
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

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (pool.isEmpty()) return
        // Hoisted: one hash probe (and, on the first frame, one bake) per FRAME.
        val bands = res.bake(AURORA_BANDS_KEY) { d -> bakeAuroraBands(d) }
        val nc = drawContext.canvas.nativeCanvas
        val paint = paints.sprite          // additive blend + bitmap filtering, set once
        val dst = paints.dst
        val o = scratch
        for (i in pool.indices) {
            if (!blit(i, o)) continue
            dst.set(o[0], o[1], o[2], o[3])
            // 8-bit paint alpha is the platform's globalAlpha. It quantizes a ~2.7%
            // strip to 7/255 and floors anything under 1/510 to invisible, which is
            // the same rounding the web canvas applies — it can only ever cost
            // light, never add it, so the column budget stays sound.
            paint.alpha = (o[4].coerceIn(0f, 1f) * 255f).roundToInt()
            nc.drawBitmap(bands[o[5].toInt()], null, dst, paint)
        }
    }
}
