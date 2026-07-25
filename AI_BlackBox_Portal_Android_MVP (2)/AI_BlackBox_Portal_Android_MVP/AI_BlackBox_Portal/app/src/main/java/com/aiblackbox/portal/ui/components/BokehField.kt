package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: BOKEH DEPTH (id "bokeh") — huge, softly defocused discs lolling in the
// foreground while razor-sharp pinpricks of dust creep past far behind them.
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/bokeh.js
// (its physics tests: Portal/modules/fx/effects/bokeh.test.mjs). Every tuning
// number below is BYTE-IDENTICAL to that file so the two surfaces can be diffed
// forever; nothing here was re-tuned by feel.
//
// The depth is not faked with a blur filter — it is FOUR CORRELATED PARALLAX
// PLANES (Rising Stars has three) where size, alpha, speed, lifetime and SPRITE
// FORM all move together:
//
//     near  → huge, dim, slow, long-lived, ANNULUS
//     far   → tiny, bright, quick, short-lived, solid blob
//
// The annulus is the whole trick. A defocused point light does not image as a
// glowing dot, it images as the lens aperture: a disc with a bright RIM and a
// flat, dim interior. Drawing the same solid radial blob at 200 px just looks
// like a smudge; the rim is what reads as "out of focus" instead of "big glow".
// So this file bakes ONE new sprite form (bakeBokehDonut, keyed "bokeh.donut")
// and reuses the shared solid-blob bake (bakeRadialSprite) for the far planes —
// both across the same five-stop ramp, so all four planes share one palette.
//
// The parallax is deliberately INVERTED from a camera pan (where near moves
// fastest): the art direction is a lazy defocused foreground with dust hurrying
// past behind it. Slow-and-huge in front is what sells "lens", not "landscape".
//
// ── LEGIBILITY (this is a BACKDROP behind chat text) ─────────────────────────
// The near discs are enormous — a 200 px disc at a "modest-looking" 15% alpha
// would put more light on the screen than the body text does. ALPHA IS THE
// ENTIRE BUDGET here: every large disc is hard-capped under 6%, and bokehAlpha()
// CLAMPS to the plane cap so no envelope, breathe or gain can ever lift it. The
// caps, the population formula and the published ink budget are ported exactly
// from the web module, where they were measured against real WCAG contrast
// against --muted (#C9C9C9 = BbxDim); BokehFieldTest re-measures all three.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here
// is authored at FIELD_REFERENCE_DENSITY and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment it is used, so the
// field has the same APPARENT size on a 2.0 tablet and a 3.5 phone. Density may
// reach NOTHING else — not a count (counts come from dp area), not an alpha, not
// a lifetime. This is the Android spelling of the web module's law: geometry
// comes from apparentScale(CSS box) and NEVER from devicePixelRatio.
//
// dt. Velocities here are PIXELS PER SECOND and are multiplied by dtSec directly
// — no `dt * 60` normalizer (Rising Stars is the other convention; never mix
// them inside one effect). The bob clock is a PRIVATE accumulator advanced by
// dtSec rather than a read of nowMs, so a parked frame loop resumes the loll
// where it left it instead of teleporting the field on the first frame back.
// =============================================================================

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.drawscope.CanvasDrawScope
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.LayoutDirection
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Colour ramp, indexed by LIFE (premium rule: never a flat colour). A disc is
 * born cool blue-white — the far side of focus — and warms as it ages, ending
 * near the Portal accent. Real lenses do exactly this at the edges of the frame
 * (longitudinal chromatic aberration), so the drift reads as optics, not as a
 * colour cycle. Five stops, shared by BOTH sprite forms.
 */
internal val BOKEH_RAMP = arrayOf(
    intArrayOf(188, 212, 255),   // cool blue-white
    intArrayOf(214, 226, 255),   // periwinkle
    intArrayOf(240, 236, 228),   // neutral warm-white
    intArrayOf(255, 214, 168),   // champagne
    intArrayOf(255, 172, 142),   // warm rose
)

/**
 * ONE PARALLAX PLANE — the sub-population unit of this effect.
 *
 * Lengths ([rMin]/[rMax], [speedMin]/[speedMax], [bob]) are WEB CSS px (see
 * [WEB_TO_REF_PX]); times are seconds; [alpha] is a hard CEILING, not a target.
 * [donut] picks the sprite FORM — the axis that makes near and far read as two
 * different optical objects rather than one population at two sizes.
 */
internal class BokehPlane(
    val id: String,
    val count: Int,
    val rMin: Float, val rMax: Float,
    val alpha: Float,
    val speedMin: Float, val speedMax: Float,
    val lifeMin: Float, val lifeMax: Float,
    val bob: Float,
    val donut: Boolean,
)

/**
 * THE FOUR SUB-POPULATIONS, declared FAR → NEAR so the array order is also the
 * painter's order (dust behind, big discs in front). Compositing is additive and
 * therefore commutative, so the order is for the reader, not for the pixels.
 *
 * Neighbouring planes are STRICTLY separated in size, alpha and speed (asserted
 * in BokehFieldTest) — that strictness is what stops the screen flattening into
 * confetti. Lifetimes deliberately overlap: nothing should die on a schedule.
 */
internal val BOKEH_PLANES = arrayOf(
    //           id        count  r (web px)   alpha    speed (web px/s) life (s)     bob   donut
    BokehPlane("dust",  46, 0.9f, 2.0f, 0.42f, 34f, 48f, 7f, 15f, 11f, false),
    BokehPlane("grain", 18, 4.0f, 9.0f, 0.13f, 20f, 29f, 12f, 22f, 9f, false),
    BokehPlane("inner", 9, 22f, 44f, 0.052f, 11f, 17f, 22f, 38f, 7f, true),
    BokehPlane("near", 5, 48f, 96f, 0.042f, 6f, 10f, 30f, 52f, 5f, true),
)

// -----------------------------------------------------------------------------
// The published legibility budget. Constants rather than a comment, because a
// budget that lives only in a comment is not a budget — BokehFieldTest asserts
// all three against the live field.
//
//   LARGE_DISC   at or above this DRAWN radius (web CSS px) a disc covers text,
//                not gaps…
//   ALPHA_CAP    …so its alpha must stay under this. Both near planes are
//                configured below it and bokehAlpha() clamps, so it cannot drift.
//   INK_BUDGET   Σ alpha·πr² over the whole field, as a fraction of the viewport.
//                A conservative over-estimate (every sprite falls off to zero long
//                before its nominal radius), set just above the measured worst
//                case so that adding a plane or lifting an alpha TRIPS the test
//                instead of shipping.
// -----------------------------------------------------------------------------
internal const val BOKEH_LARGE_DISC_PX = 24f
internal const val BOKEH_NEAR_ALPHA_CAP = 0.06f
internal const val BOKEH_INK_BUDGET = 0.0125f

/** Fraction of life spent fading up… */
private const val BOKEH_FADE_IN = 0.12f

/** …and down. Long, because a big dim disc popping is very visible. */
private const val BOKEH_FADE_OUT = 0.28f

/**
 * The web's apparentScale() clamped on a phone-shaped box (short edge / 900,
 * floored at 0.5). It is also the unit the LEGIBILITY thresholds above are
 * expressed in, so a test can read a drawn radius in web CSS px as
 * `bokehRadius(p, BOKEH_APPARENT)`.
 */
internal const val BOKEH_APPARENT = 0.5f

/**
 * WEB CSS px → reference-density DEVICE px.
 *
 * A CSS px is a dp, and FIELD_REFERENCE_DENSITY turns a dp into 3.1 device px:
 *     0.5 × 3.1 = 1.55
 * Keeping this factor explicit lets every number in [BOKEH_PLANES] stay
 * byte-identical to bokeh.js, so the two can be diffed forever.
 */
private const val WEB_TO_REF_PX = BOKEH_APPARENT * FIELD_REFERENCE_DENSITY

/** Recycle (and re-enter) on the DRAWN radius × this: a 200 px disc must be
 *  fully clear of the edge, or it visibly winks out with a third of it on screen. */
private const val BOKEH_EDGE_CLEAR = 1.35f

/** …plus a flat pad, in web CSS px (scaled with everything else at use). */
private const val BOKEH_EDGE_PAD = 8f

/** The web's reference viewport (1440 × 900 CSS px) for the population term, and
 *  the clamps ember-fx.js puts on it. Counts come from dp AREA, never from pixels. */
private const val BOKEH_REF_AREA_DP = 1440f * 900f
private const val BOKEH_VIEWPORT_MIN = 0.4f
private const val BOKEH_VIEWPORT_MAX = 2.4f
private const val BOKEH_SCALE_FLOOR = 0.12f
private const val BOKEH_SCALE_CEIL = 4.0f

/** Clamp the step. A parked frame loop hands back one enormous dt, and at these
 *  speeds a 4 s jump would teleport the entire field sideways. (EmberOverlay
 *  already clamps to 0.05; this is the web's own belt-and-braces, kept so the
 *  physics is safe wherever it is driven from.) */
private const val BOKEH_DT_MAX = 0.1f

/** Below this the platform draws nothing anyway (drawSpriteF's own cull, i.e.
 *  under 1/255 of paint alpha): pure cost, no pixels. The web's floor is 0.0008
 *  because a Canvas2D globalAlpha is not 8-bit quantized. */
private const val BOKEH_ALPHA_FLOOR = 0.003f

private const val BOKEH_TAU = 6.283f

// Spawn modes for the recycler.
private const val MODE_STAGGER = 0   // first fill / re-target: anywhere on screen, MID-life
private const val MODE_DRIFT = 1     // life expired: anywhere on screen, but fresh, so it fades in from 0
private const val MODE_EDGE = 2      // walked off the left: re-enter from the right, fresh

// -----------------------------------------------------------------------------
// Pure per-particle maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------

/**
 * SIZE over life (premium rule: never a constant size). A disc swells as it ages
 * — the imaginary lens racking further out of focus — easing out so the growth is
 * fastest while it is still faint. 0.74 at birth → 1.20 at death.
 */
internal fun bokehSizeCurve(life: Float): Float {
    val a = 1f - life                       // age, 0 → 1
    return 0.74f + 0.46f * a * (2f - a)
}

/**
 * Drawn radius. [geo] is device px per web CSS px (`scale × WEB_TO_REF_PX`) at
 * draw time; pass [BOKEH_APPARENT] to read the web's own CSS-px number instead.
 * p.sizeEnv scatters the whole curve PER PARTICLE, so two discs with the same
 * base radius never track each other.
 */
internal fun bokehRadius(p: BokehDisc, geo: Float): Float =
    p.baseR * geo * p.sizeEnv * bokehSizeCurve(p.life)

/**
 * Drawn alpha. The plane's alpha is a CEILING, not a target: the fade envelope,
 * the breathe and any [gain] may only ever take light away. That clamp is what
 * makes the legibility budget an invariant instead of a hope.
 *
 * The breathe reads [nowMs] directly (rather than a dt-advanced clock) because
 * its phase offset is PER PARTICLE and fixed for the particle's life: a clock
 * jump translates the whole ~10 s breath, it can never collapse the field onto a
 * common phase. render() must not mutate, so nowMs is also the only clock it has.
 */
internal fun bokehAlpha(p: BokehDisc, nowMs: Double, gain: Float = 1f): Float {
    val cap = p.plane.alpha
    val a = 1f - p.life
    val fadeIn = if (a < BOKEH_FADE_IN) a / BOKEH_FADE_IN else 1f
    val fadeOut = if (p.life < BOKEH_FADE_OUT) p.life / BOKEH_FADE_OUT else 1f
    val breathe = 0.82f + 0.18f * sin(nowMs * 0.0006 + p.fade).toFloat()   // ~10 s, lazy
    val g = if (gain.isFinite()) gain else 1f
    val raw = cap * fadeIn * fadeOut * breathe * g
    return if (raw > 0f) (if (raw < cap) raw else cap) else 0f
}

/** Ramp stop for this particle right now: per-particle start + a warm drift with age. */
internal fun bokehRampIndex(p: BokehDisc): Int {
    val i = p.hue0 + ((1f - p.life) * 2f).roundToInt()
    return if (i < 0) 0 else if (i > BOKEH_RAMP.size - 1) BOKEH_RAMP.size - 1 else i
}

// -----------------------------------------------------------------------------
// Sprites — baked once per (density, effect) through FieldResources, never in a
// draw or update loop.
// -----------------------------------------------------------------------------
private const val BOKEH_DONUT_KEY = "bokeh.donut"
private const val BOKEH_SOLID_KEY = "bokeh.solid"

/** Baked at 128 px because the near plane magnifies it past 200 px and a 64 px
 *  source visibly softens the one edge that carries the whole illusion. */
private const val BOKEH_DONUT_PX = 128

/**
 * THE ANNULUS. Centre alpha is 0.10 rather than 0 — a truly hollow centre reads
 * as a soap bubble, whereas a dim, flat interior with a hot rim at 0.93 reads as
 * a defocused aperture. Stops are byte-identical to bokeh.js's bakeDonut().
 */
private fun bakeBokehDonut(r: Int, g: Int, b: Int, density: Density): ImageBitmap {
    val bitmap = ImageBitmap(BOKEH_DONUT_PX, BOKEH_DONUT_PX)
    // Fully-qualified bitmap-backed Canvas factory (distinct from the @Composable Canvas).
    val canvas = androidx.compose.ui.graphics.Canvas(bitmap)
    val side = BOKEH_DONUT_PX.toFloat()
    val radius = side / 2f
    val base = Color(r / 255f, g / 255f, b / 255f, 1f)
    CanvasDrawScope().draw(density, LayoutDirection.Ltr, canvas, Size(side, side)) {
        // fillRect, like the web: outside the gradient radius Clamp extends the
        // last stop, which is fully transparent.
        drawRect(
            brush = Brush.radialGradient(
                colorStops = arrayOf(
                    0.0f to base.copy(alpha = 0.10f),
                    0.55f to base.copy(alpha = 0.16f),
                    0.82f to base.copy(alpha = 0.55f),
                    0.93f to base,                        // the rim
                    0.985f to base.copy(alpha = 0.35f),
                    1.0f to base.copy(alpha = 0f),
                ),
                center = Offset(radius, radius),
                radius = radius,
            ),
        )
    }
    return bitmap
}

/**
 * One sprite per ramp stop, in the requested FORM. The far planes take the
 * shared solid-blob bake — one bake, one palette — and are drawn tiny, which is
 * what keeps the dust reading as a pinprick rather than a smudge.
 */
private fun bakeBokehAtlas(density: Density, donut: Boolean): Array<android.graphics.Bitmap> =
    Array(BOKEH_RAMP.size) { i ->
        val c = BOKEH_RAMP[i]
        val bmp = if (donut) bakeBokehDonut(c[0], c[1], c[2], density)
        else bakeRadialSprite(c[0], c[1], c[2], density)
        // asAndroidBitmap() is a cast on an AndroidImageBitmap, not a copy.
        bmp.asAndroidBitmap()
    }

// -----------------------------------------------------------------------------
// One disc. A POOLED slot: recycled in place, never reallocated, so the hot path
// allocates nothing at all. `plane` is a property of the SLOT — a disc does not
// change depth when it is recycled, which is what keeps the planes correlated.
// -----------------------------------------------------------------------------
class BokehDisc internal constructor(internal val plane: BokehPlane) {
    var x = 0f; var y = 0f
    var vx = 0f          // web CSS px/s, ALWAYS negative: the plane drift IS the parallax cue
    var baseR = 1f       // web CSS px
    var sizeEnv = 1f     // per-particle size-envelope depth
    var bobAmp = 0f      // web CSS px/s of vertical loll
    var bobRate = 0f     // rad/s — a slow lolling, not a wobble
    var bobPhase = 0f
    var fade = 0f        // private offset into the ~10 s breathe
    var hue0 = 0         // where on the ramp this one starts
    var decay = 0f       // 1 / lifetime-in-seconds
    var life = 1f        // 1 → 0
}

/**
 * BokehSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so BokehFieldTest can drive the whole
 * simulation from a seeded generator.
 */
class BokehSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f

    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<BokehDisc>()

    /** Live per-plane population. The CHANGE GUARD: unless one of these actually
     *  moved, a resize only refreshes geometry and leaves every disc where it was. */
    private val counts = IntArray(BOKEH_PLANES.size) { -1 }

    /** Private bob clock, advanced by dt — never read off nowMs (see the header). */
    private var clockSec = 0.0

    /** Test/inspection view of the pool. Never called from the hot path. */
    val discs: List<BokehDisc> get() = pool.asList()

    /** Web CSS px → device px for THIS screen. Applied at use, never baked into
     *  stored state, so a density or size change re-scales the live field. */
    private val g: Float get() = scale * WEB_TO_REF_PX

    private fun rnd(): Float = rand.next().toFloat()
    private fun pick(lo: Float, hi: Float): Float = lo + rnd() * (hi - lo)

    /**
     * "How busy is this viewport", the web's fieldScale(): a dp-AREA term clamped
     * to [0.4, 2.4], times the operator's dial, clamped to [0.12, 4]. Density
     * reaches this only by turning device px into dp — never the geometry.
     */
    private fun fieldScale(density: Float): Float {
        val d = if (density > 0f) density else 1f
        val areaDp = (width / d) * (height / d)
        val viewport = (areaDp / BOKEH_REF_AREA_DP).coerceIn(BOKEH_VIEWPORT_MIN, BOKEH_VIEWPORT_MAX)
        return (viewport * countScale).coerceIn(BOKEH_SCALE_FLOOR, BOKEH_SCALE_CEIL)
    }

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        // A box change RESAMPLES the live field instead of stranding it: a uniform
        // distribution stays uniform, so an unfold or a rotate fills instantly with
        // no visible re-scatter, and nobody is left parked outside the new box
        // waiting out a 52 s lifetime. (StarSim does the same with x on a widen.)
        if (pool.isNotEmpty() && this.width > 0f && this.height > 0f &&
            (width != this.width || height != this.height)
        ) {
            val kx = width / this.width
            val ky = height / this.height
            for (p in pool) { p.x *= kx; p.y *= ky }
        }
        this.width = width; this.height = height; this.scale = scale
        retarget(density, MODE_STAGGER)
    }

    /**
     * Resolve the population for the current viewport.
     *
     * The change guard matters: resize() fires from drawWithCache on every layout
     * change. Rebuilding the pool there would re-scatter the field mid-sentence,
     * so unless a per-plane COUNT actually moved this returns immediately.
     */
    private fun retarget(density: Float, mode: Int) {
        val s = fieldScale(density)
        var changed = pool.isEmpty()
        for (i in BOKEH_PLANES.indices) {
            val want = ParticleTuning.scaleCount(BOKEH_PLANES[i].count, s)
            if (counts[i] != want) { counts[i] = want; changed = true }
        }
        if (!changed) return
        val old = pool
        var total = 0
        for (c in counts) total += c
        val next = ArrayList<BokehDisc>(total)          // RESIZE-only allocation; never per frame
        for (i in BOKEH_PLANES.indices) {
            val plane = BOKEH_PLANES[i]
            var kept = 0
            for (p in old) {                            // survivors keep their slot AND their state
                if (kept >= counts[i]) break
                if (p.plane === plane) { next.add(p); kept++ }
            }
            while (kept < counts[i]) {
                next.add(BokehDisc(plane).also { reset(it, mode) })
                kept++
            }
        }
        pool = next.toTypedArray()
    }

    /**
     * Re-roll one disc IN PLACE. This is the recycler — nothing here allocates.
     *
     * Only [MODE_STAGGER] (the first fill) starts mid-life. A life-expiry respawn
     * must start at 1 so the fade-in envelope hides it; otherwise discs pop into
     * existence at full brightness in the middle of the screen.
     */
    private fun reset(p: BokehDisc, mode: Int) {
        val pl = p.plane
        val gg = g
        p.baseR = pick(pl.rMin, pl.rMax)
        p.sizeEnv = 0.82f + rnd() * 0.42f
        p.vx = -pick(pl.speedMin, pl.speedMax)          // ALWAYS leftward
        p.bobAmp = pl.bob * (0.6f + rnd() * 0.8f)
        p.bobRate = 0.09f + rnd() * 0.16f
        p.bobPhase = rnd() * BOKEH_TAU
        p.fade = rnd() * BOKEH_TAU
        p.hue0 = (rnd() * 3f).toInt()                   // 0..2
        p.decay = 1f / pick(pl.lifeMin, pl.lifeMax)
        p.life = if (mode == MODE_STAGGER) 0.06f + rnd() * 0.94f else 1f
        p.x = if (mode == MODE_EDGE) width + p.baseR * gg * BOKEH_EDGE_CLEAR + BOKEH_EDGE_PAD * gg
        else rnd() * width
        p.y = rnd() * height
    }

    /**
     * [active] is deliberately unused. Every other field thins out when a turn
     * ends, but these lifetimes are 7–52 s against a 650 ms drain: nothing
     * measurable would thin, and killing slots outright is exactly the blink this
     * effect exists not to have. The web module reads env.active nowhere either.
     */
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        val d = if (dtSec > 0f) (if (dtSec < BOKEH_DT_MAX) dtSec else BOKEH_DT_MAX) else 0f
        clockSec += d
        val t = clockSec
        val gg = g
        for (p in pool) {
            p.life -= p.decay * d
            if (p.life <= 0f) { reset(p, MODE_DRIFT); continue }
            p.x += p.vx * gg * d                                            // delta-timed: same speed at 60 and 120 Hz
            // Vertical loll, zero net drift. NOTE a deliberate divergence from the
            // web: bokeh.js is the one place there that leaves an amplitude out of
            // its geometry scale, so bob is relatively twice as tall on a phone as
            // on a desktop. Here the DPI law is a test, so bob scales with gg like
            // every other length and the field stays internally consistent.
            p.y += sin(t * p.bobRate + p.bobPhase).toFloat() * p.bobAmp * gg * d
            if (p.x < -(p.baseR * gg * BOKEH_EDGE_CLEAR + BOKEH_EDGE_PAD * gg)) reset(p, MODE_EDGE)
        }
    }

    /**
     * A backdrop must not blink when a turn starts: leave the field mid-flight,
     * exactly where the user left it. Nothing ever goes dark here (see [update]),
     * so there is nothing to revive — and the population is already topped up by
     * the resize that owns it.
     */
    override fun rearm() { /* deliberately nothing — see the doc above. */ }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        // Hoisted: one hash probe per FRAME, not per particle. Both lambdas capture
        // nothing, so they are singletons — no per-frame allocation.
        val donut = res.bake(BOKEH_DONUT_KEY) { d -> bakeBokehAtlas(d, true) }
        val solid = res.bake(BOKEH_SOLID_KEY) { d -> bakeBokehAtlas(d, false) }
        val gg = g
        for (p in pool) {
            val al = bokehAlpha(p, nowMs)
            if (al < BOKEH_ALPHA_FLOOR) continue
            val r = bokehRadius(p, gg)
            val spr = (if (p.plane.donut) donut else solid)[bokehRampIndex(p)]
            drawSpriteF(spr, p.x, p.y, r, al, paints)
        }
    }
}
