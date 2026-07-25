package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: SLIPSTREAM (id "slipstream") — hair-thin luminous threads combing
// through invisible turbulence.
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/slipstream.js
// (its physics tests: Portal/modules/fx/effects/slipstream.test.mjs).
//
// Each thread strokes the segment from where it WAS to where it IS, so the
// segment IS the thread — nothing is blitted and this is the one field in the
// catalogue that draws no sprite at all. The flow field is taken VERBATIM from
// EmberSim.pot(): the same 2-octave divergence-free curl-noise potential, the
// same finite-difference eps. One deliberate difference in how it is USED — a
// thread is ADVECTED by the curl (its velocity chases the field) rather than
// ACCELERATED by it as an ember is. Advecting a divergence-free field is what
// keeps the density even: threads comb and fold without piling into blobs.
//
// THE DETAIL A NAIVE PORT MISSES: a curl potential this cheap has fixed
// attractors, and a field of long-lived particles walks onto them and goes
// STATIC within seconds — a beautiful first frame and a dead one at t=5s. So
// ~2.5% of the population is scatter-reset every frame, on top of ordinary
// lifetimes, expressed as a rate PER SECOND (never a per-frame percentage, or a
// 120 Hz panel would churn the field twice as fast as a 60 Hz one). Because the
// envelope is zero at birth, a reset is invisible.
//
// ── THE TRAIL: how this differs from the web engine ──────────────────────────
//
// The web declares `clearPolicy: 'fade:rgba(6,9,16,0.12)'` and lets the CANVAS
// keep the trail: last frame's segments survive, veiled 12% darker, so a segment
// is still ~10% visible ~18 frames later. Compose's Canvas has no persistence —
// it clears every frame — and FieldSim has no per-effect clear policy to hook.
// So the trail is drawn EXPLICITLY, exactly the way MatrixSim emulates the web's
// smear: each thread keeps a short ring buffer of its last positions and strokes
// them as a fading tail, one segment per past frame, alpha × (1 - 0.12)^age.
// Same decay constant, same mechanism, one frame's worth of geometry instead of
// a framebuffer's worth of history. The accumulated veil comes out slightly
// QUIETER than the web's (Σ 0.88^n over 12 terms = 6.5× a single frame, vs the
// web's steady-state 1/0.12 = 8.3×), which is the safe direction for a backdrop.
//
// ── THE LEGIBILITY BUDGET (this is a backdrop behind chat text) ──────────────
//
//   • stroke width is hard-capped at SLIPSTREAM_MAX_LINE_PX (1.05 web CSS px)
//     AFTER the geometry scale, so a bigger screen gets MORE threads, never
//     fatter ones. Nothing wide enough to sit behind a glyph stem can exist.
//   • stroke alpha is hard-capped at SLIPSTREAM_MAX_ALPHA (0.30); the brightest
//     quantised level a thread can actually be drawn at is 0.2625.
//   • population is viewport-area driven and clamped to 420..2600 threads.
// All three are the web's numbers, unchanged, and all three are asserted from
// the DRAWN OUTPUT in SlipstreamFieldTest.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here
// is authored at FIELD_REFERENCE_DENSITY and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment it is used, so the
// field has the same APPARENT size on a 2.0 tablet and a 3.5 phone. Density
// reaches nothing else — not a count, not an alpha, not a width bucket, not a
// lifetime. (The web spells the same law "devicePixelRatio must NEVER reach
// particle geometry".)
//
// dt. Velocities here are PIXELS PER SECOND and are multiplied by dtSec directly
// — no `dt * 60` normalizer. So is the scatter-reset rate, and so is the
// velocity chase (`min(1, resp * dt)`, clamped so a long stall settles ON the
// target instead of overshooting past it). Rising Stars is the other convention;
// never mix them inside one effect.
// =============================================================================

import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

// -----------------------------------------------------------------------------
// THE WEB TUNING TABLE. Every number below is byte-identical to the SLIPSTREAM
// object in slipstream.js so the two surfaces can be diffed forever. Lengths are
// WEB CSS px, times are seconds, alphas are dimensionless.
// -----------------------------------------------------------------------------

/** Threads at geometry scale 1.0, before the viewport-area term. */
internal const val SLIPSTREAM_PER_SCALE = 1600

/** Population floor/ceiling. The ceiling is a legibility number as much as a
 *  performance one: a bigger screen gets more threads, never fatter ones. */
internal const val SLIPSTREAM_MIN_THREADS = 420
internal const val SLIPSTREAM_MAX_THREADS = 2600

/** curl → px/s. 4200 × a typical curl of .03 ≈ 125 px/s. */
internal const val SLIPSTREAM_ADVECT = 4200f

/** Finite-difference step for the curl, in web CSS px. Same as EmberSim's. */
internal const val SLIPSTREAM_EPS = 3.0

/** Scatter-resets per thread per SECOND (= 2.5% of the field per 60 Hz frame). */
internal const val SLIPSTREAM_RESET_PER_SEC = 1.5f

/** How far outside the box a thread may drift before it is recycled (CSS px). */
internal const val SLIPSTREAM_MARGIN = 24f

/** THE legibility cap on stroke width, in web CSS px, applied POST geometry
 *  scale. Every quantised width below is inside it by construction. */
internal const val SLIPSTREAM_MAX_LINE_PX = 1.05f

/** THE other one: no stroke is ever issued at more than this alpha. */
internal const val SLIPSTREAM_MAX_ALPHA = 0.30f

/** Below this a thread is skipped, not drawn at 0. */
internal const val SLIPSTREAM_MIN_ALPHA = 0.015f

/** Quantised stroke widths (all ≤ SLIPSTREAM_MAX_LINE_PX) and the edges that
 *  bisect them. Stroke state is the expensive part of stroking 2,000 segments,
 *  not the segments, so width × colour × alpha is quantised into buckets and
 *  each bucket is issued as ONE batched drawLines call.
 *
 *  NOTE, carried over deliberately: the top bucket (1.0) is unreachable, because
 *  the cap (1.05) is applied BEFORE the quantisation and sits below the upper
 *  edge (1.2). That is true of slipstream.js too — the cap was tightened from
 *  1.5 and the edges were not followed down. Kept byte-identical rather than
 *  "fixed" so the two surfaces stay diffable; the field uses 0.45 and 0.7. */
internal val SLIPSTREAM_WIDTHS = floatArrayOf(0.45f, 0.7f, 1.0f)
internal val SLIPSTREAM_WIDTH_EDGES = floatArrayOf(0.82f, 1.2f)

/** Alpha quantisation levels. */
internal const val SLIPSTREAM_ALPHA_LEVELS = 4

/**
 * THE THREE SUB-POPULATIONS. Weights sum to 1.
 *
 *   filament — the hair; the bulk of the field, rides the curl exactly
 *   veil     — slower, wider, laggier: broad folding sheets behind the hair
 *   glint    — sparse, quick, short-lived bright strands that pick out shear
 *
 * They differ on every visible axis — gain, drift, width, alpha, lifetime and
 * response rate — because two populations that differ only in size just look
 * like one population at two distances.
 *
 * [lifeBase]/[lifeSpan] reproduce the web's `1 / (life[0] + rand() * life[1])`
 * verbatim: life[1] is used as a RANGE, not as an upper bound, so a filament
 * lives 2.2–5.0 s rather than 2.2–2.8 s. Deliberately not "fixed" — the two
 * surfaces have to stay diffable.
 */
internal class SlipKind(
    val w: Float,
    val gain: Float,
    val dx: Float, val dy: Float,
    val width: Float,
    val alpha: Float,
    val lifeBase: Float, val lifeSpan: Float,
    val resp: Float,
)

internal val SLIPSTREAM_KINDS = arrayOf(
    SlipKind(w = 0.72f, gain = 1.00f, dx = 16f, dy = -6f, width = 0.62f, alpha = 0.26f, lifeBase = 2.2f, lifeSpan = 2.8f, resp = 6.0f),
    SlipKind(w = 0.22f, gain = 0.55f, dx = -10f, dy = 4f, width = 1.05f, alpha = 0.13f, lifeBase = 3.6f, lifeSpan = 4.4f, resp = 2.4f),
    SlipKind(w = 0.06f, gain = 1.35f, dx = 26f, dy = -14f, width = 0.45f, alpha = 0.30f, lifeBase = 0.9f, lifeSpan = 1.0f, resp = 9.0f),
)

/** Colour ramp indexed by LIFE: entering the flow → ice at the peak of the
 *  envelope → cooling out. Additive blend, so these stay cold and quiet. */
internal val SLIPSTREAM_RAMP = arrayOf(
    intArrayOf(70, 104, 208),
    intArrayOf(96, 168, 236),
    intArrayOf(168, 226, 255),
    intArrayOf(140, 176, 246),
    intArrayOf(84, 96, 170),
)

internal val SLIP_RAMP_N = SLIPSTREAM_RAMP.size                       // 5
internal const val SLIP_ALPHA_N = SLIPSTREAM_ALPHA_LEVELS             // 4
internal val SLIP_WIDTH_N = SLIPSTREAM_WIDTHS.size                    // 3
internal val SLIP_BUCKETS = SLIP_RAMP_N * SLIP_ALPHA_N * SLIP_WIDTH_N // 60

/**
 * The 20 packed ARGB stroke colours (colour × alpha level), built once at class
 * init. The web builds the equivalent `rgba(...)` strings at import for exactly
 * the same reason: composing one per segment would be the single largest source
 * of per-frame garbage in the engine.
 *
 * Representative alpha per level is the MIDPOINT of the level, so the darkest
 * tail rounds UP by at most 0.0375 and the top level lands at 0.2625 — the cap
 * is approached, never exceeded.
 */
internal val SLIPSTREAM_STYLES: IntArray = IntArray(SLIP_RAMP_N * SLIP_ALPHA_N) { i ->
    val ci = i / SLIP_ALPHA_N
    val ai = i % SLIP_ALPHA_N
    val a = SLIPSTREAM_MAX_ALPHA * (ai + 0.5f) / SLIP_ALPHA_N
    val rgb = SLIPSTREAM_RAMP[ci]
    ((a * 255f).toInt() shl 24) or (rgb[0] shl 16) or (rgb[1] shl 8) or rgb[2]
}

// -----------------------------------------------------------------------------
// Android-side geometry + trail constants
// -----------------------------------------------------------------------------

/**
 * apparentScale() on a phone-shaped box — the web's geometry multiplier clamps
 * to 0.5 there, and Android is always phone-shaped by this contract (see
 * ADDING_AN_EFFECT.md §4: reference px = web CSS px × 0.5 × 3.1 = × 1.55).
 * Held as its own constant rather than folded into a single WEB_TO_REF_PX
 * because slipstream needs BOTH conversions separately: the web multiplies some
 * lengths by apparentScale (advect, thread width) and leaves others in raw box
 * CSS px (margin, the eps sampling step, the width cap and its bucket edges).
 */
private const val SLIP_APPARENT = 0.5f

/**
 * How many past frames of each thread are stroked as its tail. The web gets this
 * from the canvas smear; Compose clears every frame, so we redraw the history —
 * the same trick MatrixSim plays with MATRIX_TRAIL.
 */
internal const val SLIPSTREAM_TRAIL = 12

/** The web's clearPolicy smear alpha: a surviving segment is veiled by 12% per
 *  frame, i.e. multiplied by 0.88. Applied here as an explicit per-age fade. */
internal const val SLIPSTREAM_SMEAR = 0.12f

/** (1 - smear)^age, precomputed: no pow() in the segment loop. */
internal val SLIPSTREAM_FADE = FloatArray(SLIPSTREAM_TRAIL) { age ->
    var f = 1f
    repeat(age) { f *= (1f - SLIPSTREAM_SMEAR) }
    f
}

/** Ring capacity: one point more than the number of segments it can produce. */
private const val SLIP_TRAIL_POINTS = SLIPSTREAM_TRAIL + 1

/** Alpha → level index multiplier (web: `aq = ALPHA_N / maxAlpha`). */
private const val SLIP_ALPHA_Q = SLIP_ALPHA_N / SLIPSTREAM_MAX_ALPHA

/** The web engine's fieldScale() reference box, in CSS px (= dp here), and the
 *  clamp it applies to the viewport-area term before the count is derived. */
private const val SLIP_REF_AREA_DP = 1440f * 900f
private const val SLIP_VIEWPORT_MIN = 0.4f
private const val SLIP_VIEWPORT_MAX = 2.4f

// -----------------------------------------------------------------------------
// Pure maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------

/**
 * 2-octave sine curl-noise potential ψ → divergence-free swirl. Byte-identical
 * to EmberSim.pot() and to embers.js on purpose: both fields must swirl the same
 * way. NOT shared with EmberSim — an art-direction tweak to one effect must
 * never silently re-tune the other. This is the one duplication in the module.
 *
 * [x] and [y] are in web CSS px (device px ÷ the box geometry scale), so the
 * spatial frequencies hold across DPIs.
 */
internal fun slipPot(x: Double, y: Double, t: Double): Double =
    sin(x * 0.0065 + t * 0.22) * cos(y * 0.0065 - t * 0.16) +
        0.5 * sin(x * 0.013 - t * 0.31) * cos(y * 0.013 + t * 0.26)

/**
 * THE ENVELOPE. `sqrt(4u(1-u))` over the particle's life [u] = 1 - life: opens
 * fast, holds a fat plateau, closes fast. Both the width AND the alpha ride it,
 * which is what makes a birth — or a scatter reset — invisible: at u = 0 it is
 * exactly 0, so a re-seated thread is not drawn at all on the frame it moved.
 * Guarded so a clamped-away negative can never produce a NaN width.
 */
internal fun slipArch(u: Float): Float {
    val a4 = 4f * u * (1f - u)
    return if (a4 > 0f) sqrt(a4) else 0f
}

/** Width bucket for a per-particle width in PRE-apparent web CSS px. */
internal fun slipWidthBucket(w: Float): Int =
    if (w < SLIPSTREAM_WIDTH_EDGES[0]) 0 else if (w < SLIPSTREAM_WIDTH_EDGES[1]) 1 else 2

// -----------------------------------------------------------------------------
// One thread. A POOLED slot: recycled in place, never reallocated, so the hot
// path allocates nothing. The ring buffer is the Android-only part — it holds
// the tail the web gets for free from the canvas smear.
// -----------------------------------------------------------------------------
class SlipThread internal constructor() {
    var x = 0f; var y = 0f                  // device px
    var vx = 0f; var vy = 0f                // device px/s
    var life = 1f                           // 1 → 0
    var decay = 1f                          // 1 / lifetime-in-seconds
    var gain = 1f                           // how hard this kind rides the curl
    var dx = 0f; var dy = 0f                // constant drift, web CSS px/s
    var resp = 1f                           // velocity chase rate, per second
    var wb = 0.5f                           // base width, pre-apparent CSS px
    var ab = 0.2f                           // base alpha
    var wEnv = 1f                           // per-particle envelope depth
    var tint = 0                            // ramp offset: two colour families
    var ci = 0                              // colour stop, life-indexed
    var wi = -1                             // width bucket, or -1 = not drawn
    var alpha = 0f                          // head-segment alpha (pre-fade)

    /** Position history, newest-first via [head]. Device px, absolute. */
    internal val tx = FloatArray(SLIP_TRAIL_POINTS)
    internal val ty = FloatArray(SLIP_TRAIL_POINTS)
    internal var head = 0                   // where the NEXT point goes
    var trailLen = 0; internal set          // points held, ≤ SLIP_TRAIL_POINTS

    /** Segments this thread can stroke right now (0 on the frame it re-seated). */
    val segments: Int get() = min(SLIPSTREAM_TRAIL, trailLen - 1).coerceAtLeast(0)

    internal fun push(px: Float, py: Float) {
        tx[head] = px; ty[head] = py
        head++; if (head == SLIP_TRAIL_POINTS) head = 0
        if (trailLen < SLIP_TRAIL_POINTS) trailLen++
    }

    /** Drop the tail. THE line that prevents the classic trail-renderer bug: a
     *  recycled thread that keeps its old history strokes a bright bar clean
     *  across the viewport — a legibility failure, not just an artefact. */
    internal fun clearTrail() { head = 0; trailLen = 0 }
}

/**
 * SlipstreamSim — the effect. Registered in FieldRegistry.kt; nothing else in
 * the codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so SlipstreamFieldTest can drive the
 * whole simulation — including the drawn output — from a seeded generator.
 */
class SlipstreamSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<SlipThread>()

    /** Test/inspection view of the pool. Never called from the hot path. */
    val threads: List<SlipThread> get() = pool.asList()

    /** Scatter-reset telemetry (the web's `st.resets`); asserted by the test. */
    var resets = 0; private set

    private var resetAcc = 0f
    /** Field clock in seconds, advanced by [update] and reused by resize/rearm
     *  so a re-seated thread joins the flow the field has RIGHT NOW. */
    private var ts = 0.0

    /**
     * device px per POST-apparent web CSS px — i.e. per CSS px of the box the
     * web module measures in. Everything the web leaves unscaled (margin, the
     * eps sampling step, the width cap and its bucket edges) converts with this.
     */
    private val gp: Float get() = scale * FIELD_REFERENCE_DENSITY

    /**
     * device px per PRE-apparent web CSS px — a length the web multiplies by
     * apparentScale() before use (the advection rate, a thread's base width).
     * At the reference density this is the recipe's WEB_TO_REF_PX = 1.55.
     */
    private val g: Float get() = gp * SLIP_APPARENT

    private fun rnd(): Float = rand.next().toFloat()

    // ── Draw scratch. Written by buildSegments(), read by render(). NOT sim
    //    state: nothing in resize/update/rearm ever reads it, so render stays
    //    read-only with respect to the physics. Sized with the pool, never in
    //    the hot path. ──
    private var segBin = IntArray(0)
    private var segPts = FloatArray(0)
    private val counts = IntArray(SLIP_BUCKETS)
    private val starts = IntArray(SLIP_BUCKETS + 1)
    private val cursor = IntArray(SLIP_BUCKETS)

    /** Bucket-ordered segment geometry: x0, y0, x1, y1 per segment. */
    internal val segmentPoints: FloatArray get() = segPts

    /** First segment index of bucket [b] in [segmentPoints] (b ≤ SLIP_BUCKETS). */
    internal fun bucketStart(b: Int): Int = starts[b]

    /**
     * Device-px stroke width for bucket [b] — THE cap, post-scale. Used by
     * render() and asserted by the test, so there is one definition of "how wide
     * did we actually stroke" and it cannot drift.
     */
    internal fun strokeWidthPx(b: Int): Float =
        min(SLIPSTREAM_WIDTHS[b % SLIP_WIDTH_N], SLIPSTREAM_MAX_LINE_PX) * SLIP_APPARENT * gp

    /** The hard ceiling every stroke must sit under, in device px. */
    internal val maxStrokeWidthPx: Float get() = SLIPSTREAM_MAX_LINE_PX * SLIP_APPARENT * gp

    /**
     * Thread count: viewport-AREA driven (never dpr driven), from LOGICAL dp
     * area exactly like the web's fieldScale(), then the operator's dial, then
     * the population clamp.
     */
    private fun threadCount(density: Float): Int {
        val areaDp = (width / density) * (height / density)
        val viewport = (areaDp / SLIP_REF_AREA_DP).coerceIn(SLIP_VIEWPORT_MIN, SLIP_VIEWPORT_MAX)
        val n = ParticleTuning.scaleCount(SLIPSTREAM_PER_SCALE, countScale * viewport)
        return n.coerceIn(SLIPSTREAM_MIN_THREADS, SLIPSTREAM_MAX_THREADS)
    }

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val same = width == this.width && height == this.height &&
            scale == this.scale && pool.isNotEmpty()
        if (same) return
        this.width = width; this.height = height; this.scale = scale
        val want = threadCount(density)
        val prev = pool.size
        if (prev != want) {
            // Grow/trim IN PLACE: survivors keep their slot object AND their
            // place on the life curve, because a window drag (or an unfold) is
            // not a reason for the whole braid to blink.
            val old = pool
            pool = Array(want) { i -> if (i < prev) old[i] else SlipThread().also { reseat(it, true) } }
            segBin = IntArray(want * SLIPSTREAM_TRAIL)
            segPts = FloatArray(want * SLIPSTREAM_TRAIL * 4)
        }
        // Anything the new (smaller) box left outside itself is re-seated — and
        // a survivor's tail is in absolute device px, so a geometry change makes
        // it stale: drop it rather than stroke across the resize.
        for (i in 0 until min(prev, want)) {
            val p = pool[i]
            if (p.x > width || p.y > height) reseat(p, true) else p.clearTrail()
        }
    }

    /**
     * Re-seat one thread anywhere in the box. [initial] staggers it along the
     * life curve so a fresh field does not pulse in unison.
     *
     * The two lines that matter: the tail is dropped (a recycled thread with a
     * stale previous position strokes a bright line clean across the viewport),
     * and the velocity is seeded FROM the field so a new thread joins the flow
     * at once instead of visibly drifting into it.
     */
    private fun reseat(p: SlipThread, initial: Boolean) {
        val k = pickKind()
        p.x = rnd() * width; p.y = rnd() * height
        p.gain = k.gain; p.dx = k.dx; p.dy = k.dy; p.resp = k.resp
        p.wb = k.width; p.ab = k.alpha
        p.life = if (initial) 0.05f + rnd() * 0.9f else 1f
        p.decay = 1f / (k.lifeBase + rnd() * k.lifeSpan)
        p.wEnv = 0.8f + rnd() * 0.5f          // per-particle envelope multiplier
        p.tint = if (rnd() < 0.5f) 0 else 1   // ramp offset: two colour families
        p.ci = 0; p.wi = -1; p.alpha = 0f     // invisible until its envelope opens
        p.clearTrail()
        val box = gp
        val lx = (p.x / box).toDouble()
        val ly = (p.y / box).toDouble()
        val cvx = (slipPot(lx, ly + SLIPSTREAM_EPS, ts) - slipPot(lx, ly - SLIPSTREAM_EPS, ts)).toFloat()
        val cvy = -(slipPot(lx + SLIPSTREAM_EPS, ly, ts) - slipPot(lx - SLIPSTREAM_EPS, ly, ts)).toFloat()
        p.vx = cvx * SLIPSTREAM_ADVECT * g * k.gain + k.dx * box
        p.vy = cvy * SLIPSTREAM_ADVECT * g * k.gain + k.dy * box
    }

    /** Weighted draw over the three sub-populations. One rand() call, always. */
    private fun pickKind(): SlipKind {
        val r = rnd()
        var c = 0f
        for (k in SLIPSTREAM_KINDS) { c += k.w; if (r < c) return k }
        return SLIPSTREAM_KINDS[0]
    }

    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        ts = nowMs * 0.001
        val box = gp                       // device px per box CSS px
        val adv = SLIPSTREAM_ADVECT * g    // curl → device px/s
        val margin = SLIPSTREAM_MARGIN * box
        val w = width
        val h = height
        val n = pool.size

        // Scatter-reset: a FRACTION PER SECOND, with the remainder carried in an
        // accumulator, so a 120 Hz display does the SAME number of resets per
        // second — not twice as many. Without it the field walks onto the
        // potential's fixed attractors and freezes within seconds.
        resetAcc += n * SLIPSTREAM_RESET_PER_SEC * dtSec
        while (resetAcc >= 1f) {
            reseat(pool[(rnd() * n).toInt().coerceIn(0, n - 1)], false)
            resetAcc -= 1f
            resets++
        }

        // No `active` gate: the population is FIXED, so there is no spawn ramp to
        // hold back and nothing to thin out (MatrixSim is the same). The engine's
        // own bounded drain + alpha fade ends the field.
        for (p in pool) {
            p.life -= p.decay * dtSec
            if (p.life <= 0f) reseat(p, false)
            // ── curl of ψ by central difference, sampled in LOGICAL (box CSS px)
            //    space so the spatial frequency is the same on every screen ──
            val lx = (p.x / box).toDouble()
            val ly = (p.y / box).toDouble()
            val cvx = (slipPot(lx, ly + SLIPSTREAM_EPS, ts) - slipPot(lx, ly - SLIPSTREAM_EPS, ts)).toFloat()
            val cvy = -(slipPot(lx + SLIPSTREAM_EPS, ly, ts) - slipPot(lx - SLIPSTREAM_EPS, ly, ts)).toFloat()
            // Advection, not acceleration: chase the field at a per-kind rate.
            // Clamped to 1 so a long stall settles ON the target, never past it.
            val kk = min(1f, p.resp * dtSec)
            p.vx += ((cvx * adv * p.gain + p.dx * box) - p.vx) * kk
            p.vy += ((cvy * adv * p.gain + p.dy * box) - p.vy) * kk
            p.x += p.vx * dtSec
            p.y += p.vy * dtSec
            if (p.x < -margin || p.x > w + margin || p.y < -margin || p.y > h + margin) {
                reseat(p, false)
            }
            // ── shading: size AND alpha ride ONE arch over the particle's life ──
            val u = 1f - p.life
            val arch = slipArch(u)
            val a = p.ab * arch
            if (a < SLIPSTREAM_MIN_ALPHA) {
                p.wi = -1; p.alpha = 0f
            } else {
                p.alpha = a
                // Width is quantised in PRE-apparent CSS px, then the apparent
                // factor is applied to the DRAWN width (strokeWidthPx). The web
                // scales first and quantises against absolute edges, which works
                // on a desktop viewport but collapses all three buckets into one
                // at the 0.5 apparent scale Android is pinned to — i.e. the
                // size-over-life curve would be quantised away entirely. Same
                // table, same cap (asserted post-scale), live envelope.
                p.wi = slipWidthBucket(min(p.wb * arch * p.wEnv, SLIPSTREAM_MAX_LINE_PX))
                // COLOUR RAMP indexed by life, with a per-particle offset so the
                // field runs two colour families rather than one gradient.
                p.ci = min(SLIP_RAMP_N - 1, (u * SLIP_RAMP_N).toInt() + p.tint)
            }
            // The segment drawn this frame is (previous point) → (x, y). Pushing
            // AFTER the reset checks is what pins a re-seated thread's tail to
            // nothing: it contributes zero segments on the frame it jumped.
            p.push(p.x, p.y)
        }
    }

    /** Fresh braid each turn: re-seat the whole field, staggered along the life
     *  curve so it re-forms out of nothing instead of flashing in and pulsing. */
    override fun rearm() {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        resetAcc = 0f
        for (p in pool) reseat(p, true)
    }

    /**
     * Bucket every visible segment and lay them out contiguously in [segPts].
     *
     * A counting sort into the 60 style buckets — histogram, prefix sums,
     * scatter. Three linear passes over preallocated arrays, so batching costs
     * no allocation and no comparison sort, and the draw phase issues at most
     * ONE state change + one drawLines per bucket however big the field is.
     *
     * @return the number of segments written.
     */
    internal fun buildSegments(): Int {
        counts.fill(0)
        var total = 0
        for (i in pool.indices) {
            val p = pool[i]
            val base = i * SLIPSTREAM_TRAIL
            val segs = p.segments
            val drawable = p.wi >= 0
            for (s in 0 until SLIPSTREAM_TRAIL) {
                var b = -1
                if (drawable && s < segs) {
                    // The explicit smear: this segment is `s` frames old.
                    val a = p.alpha * SLIPSTREAM_FADE[s]
                    if (a >= SLIPSTREAM_MIN_ALPHA) {
                        val ai = min(SLIP_ALPHA_N - 1, (a * SLIP_ALPHA_Q).toInt())
                        b = (p.ci * SLIP_ALPHA_N + ai) * SLIP_WIDTH_N + p.wi
                        counts[b]++
                        total++
                    }
                }
                segBin[base + s] = b
            }
        }
        var acc = 0
        for (b in 0 until SLIP_BUCKETS) { starts[b] = acc; cursor[b] = acc; acc += counts[b] }
        starts[SLIP_BUCKETS] = acc
        if (acc == 0) return 0
        for (i in pool.indices) {
            val p = pool[i]
            val base = i * SLIPSTREAM_TRAIL
            for (s in 0 until SLIPSTREAM_TRAIL) {
                val b = segBin[base + s]
                if (b < 0) continue
                // Ring walk: point 0 is the newest, segment s runs s+1 → s.
                var i0 = p.head - 1 - s; if (i0 < 0) i0 += SLIP_TRAIL_POINTS
                var i1 = i0 - 1; if (i1 < 0) i1 += SLIP_TRAIL_POINTS
                val o = cursor[b]++ * 4
                segPts[o] = p.tx[i1]; segPts[o + 1] = p.ty[i1]
                segPts[o + 2] = p.tx[i0]; segPts[o + 3] = p.ty[i0]
            }
        }
        return total
    }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (pool.isEmpty()) return
        if (buildSegments() == 0) return
        val nc = drawContext.canvas.nativeCanvas
        // The shared scratch paint, already carrying FIELD_BLEND (additive —
        // crossing threads must ADD light, the web's 'lighter') and antialiasing.
        // Style/cap/join/strokeWidth are inert for drawBitmap, so configuring
        // them here can never disturb a sprite-drawing effect that reuses the
        // same FieldPaints after a mode switch. This field bakes NOTHING: it is
        // the only one that never touches res.atlas.
        val paint = paints.sprite
        paint.style = android.graphics.Paint.Style.STROKE
        // A near-stationary thread must read as a dot, not a hard 1px square.
        paint.strokeCap = android.graphics.Paint.Cap.ROUND
        paint.strokeJoin = android.graphics.Paint.Join.ROUND
        for (b in 0 until SLIP_BUCKETS) {
            val from = starts[b]
            val to = starts[b + 1]
            if (from == to) continue
            paint.color = SLIPSTREAM_STYLES[b / SLIP_WIDTH_N]
            paint.strokeWidth = strokeWidthPx(b)
            // count is a number of FLOATS (4 per segment), not a segment count.
            nc.drawLines(segPts, from * 4, (to - from) * 4, paint)
        }
    }
}
