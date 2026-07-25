package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: LOW FOG (id "fog") — the CHEAPEST field in the catalogue and the one
// with the highest legibility risk.
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/fog.js
// (its physics tests: Portal/modules/fx/effects/fog.test.mjs).
//
// Barely-there veils of grey drifting across the black at different speeds, so
// the backdrop reads as weather with depth instead of flat paint. The whole
// effect is 8–20 ENORMOUS soft radial quads per frame — no noise, no shader, no
// per-pixel work. The 64 px-sprite-blown-up-to-1400 px mush that is a DEFECT for
// embers (visible blur on a 6 px spark) is the FEATURE here: it is exactly the
// soft-edged falloff a fog bank needs, for free.
//
// ── THE LEGIBILITY BUDGET — the reason this file is careful ──────────────────
//
// This is a BACKDROP behind chat body text. Unlike embers or stars, which put
// light in a few small places, fog raises the AVERAGE luminance of the backdrop
// UNIFORMLY — precisely what erodes the contrast of the text sitting on top of
// it. So the ceiling is enforced in CODE, twice, exactly as the web module does:
//
//   FOG_MAX_VEIL_ALPHA   a HARD per-quad ceiling, applied with coerceAtMost at
//                        BOTH the compute site (update) and the draw site
//                        (render). No tuning value in the kinds table may exceed
//                        it, and FogFieldTest asserts that.
//   FOG_ALPHA_BUDGET     a HARD field-wide ceiling on the SUM of the quad
//                        alphas. A per-quad cap alone is not enough: twenty 11%
//                        veils stacked are a white wash. update() normalises the
//                        whole field down to the budget once per frame
//                        ([FogSim.budgetK]), so the worst-case luminance lift is
//                        bounded no matter what the population does.
//
// COMPOSITING IS SrcOver — deliberately NOT the engine's additive FIELD_BLEND.
// Fog is not emissive; under Plus/'lighter' the overlaps would blow out to white,
// which is the exact failure the budget exists to prevent. That is why this
// effect draws through its OWN baked paint (see FOG_PAINT_KEY) instead of
// FieldPaints.sprite, which carries the additive blend every other field wants.
// It is the one and only reason this file does not use drawSpriteF.
//
// ── The three premium rules, as they land here ──────────────────────────────
//   1. SIZE OVER LIFE with a per-particle envelope: fogSwell(age) dilates a veil
//      0.62 → 1.0 monotonically as it ages, multiplied by the veil's OWN
//      sizeEnv (0.82–1.24) and its own breathe oscillation, so no two dilate to
//      the same size and none of them is a constant blob.
//   2. LIFE-INDEXED COLOUR RAMP: FOG_RAMP is walked by age (hue0 + round(age×2)),
//      so a veil is born cool slate (fresh, backlit) and ages through neutral
//      into warm dust. Never a flat grey — that is what stops twenty overlapping
//      quads from reading as one sheet.
//   3. TWO SUB-POPULATIONS: banks (slow, deep, huge) and wisps (quick, small,
//      shredded). The speed difference IS the parallax.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here
// is authored at FIELD_REFERENCE_DENSITY and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment it is used, so the
// field has the same APPARENT size on a 2.0 tablet and a 3.5 phone. Density
// reaches exactly two things: geometry (through [FogSim.g]) and the dp-derived
// population target — never an alpha, never a period, never a lifetime.
//
// dt. All motion is PIXELS PER SECOND multiplied by dtSec directly — no
// `dt * 60` normalizer (the web module already uses this convention, so its
// constants transfer byte-for-byte). The bob/breathe oscillators run off a
// PRIVATE clock advanced by dtSec, NOT off nowMs: a parked frame loop must
// resume the weather where it left it rather than teleporting every veil to a
// new common phase on resume.
// =============================================================================

import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

// -----------------------------------------------------------------------------
// The legibility ceilings. These are MEASURED values from the web side (real
// WCAG contrast over the accumulated field) — they are not style, and they are
// not to be re-tuned by feel on this surface.
// -----------------------------------------------------------------------------

/** HARD per-quad alpha ceiling. Not a convention — a coerceAtMost, twice.
 *  (Raised 0.06 → 0.115 on the web on 2026-07-24: a veil so faint it is
 *  invisible has no value and its budget is protecting nothing. The web's
 *  fog.js prose still says "6%" in one line; 0.115 is the shipped constant and
 *  its own test asserts `MAX_VEIL_ALPHA <= 0.14`.) */
internal const val FOG_MAX_VEIL_ALPHA = 0.115f

/** HARD ceiling on the SUM of the field's quad alphas (worst-case stack). */
internal const val FOG_ALPHA_BUDGET = 0.58f

/** Population bounds. Twenty giant quads is the ENTIRE per-frame cost. */
internal const val FOG_MIN_VEILS = 8
internal const val FOG_MAX_VEILS = 20

/**
 * WEB CSS px → reference-density DEVICE px.
 *
 * The web module authors every length in CSS px and multiplies by
 * apparentScale(cssBox), which clamps to 0.5 on a phone-shaped viewport. A CSS
 * px is a dp, and FIELD_REFERENCE_DENSITY turns a dp into 3.1 device px:
 *     0.5 × 3.1 = 1.55
 * Keeping this factor explicit lets every number in the kinds table below stay
 * BYTE-IDENTICAL to fog.js, so the two surfaces can be diffed forever.
 *
 * Note it is applied to VELOCITY as well as size. The web applies `st.geo` to
 * radii but not to `vx`; on a phone-shaped box geo IS 0.5, so scaling both by
 * the same factor reproduces the web-on-a-phone rendering exactly — and it is
 * the only spelling that satisfies the Android DPI law (a velocity that skipped
 * `scale` would make the fog cross a 3.5×-density screen slower).
 */
internal const val FOG_WEB_TO_REF_PX = 1.55f

/** dp² of the viewport the web's `fieldScale()` normalises against (1440×900).
 *  Population is viewport-AREA derived, in logical (dp) units — never pixels. */
private const val FOG_REF_AREA_DP = 1440f * 900f

private const val FOG_TAU = 6.2831855f
private const val FOG_RAD_TO_DEG = 57.29578f

/** Memo keys for this effect's private FieldResources assets. Prefixed with the
 *  effect id so two effects can never collide in the shared `extras` map. */
private const val FOG_SPRITE_KEY = "fog.ramp"
private const val FOG_PAINT_KEY = "fog.paint"

/**
 * Colour ramp, indexed by LIFE (young → old). Never a flat grey: a veil is born
 * cool slate (fresh, backlit) and ages through neutral into warm dust, which is
 * what stops twenty overlapping quads from reading as one grey sheet.
 */
internal val FOG_RAMP = arrayOf(
    intArrayOf(172, 186, 204),   // cool slate — freshly formed
    intArrayOf(186, 190, 196),   // neutral grey
    intArrayOf(197, 192, 184),   // dust
    intArrayOf(190, 180, 168),   // warm, dissipating
)

/**
 * THE TWO SUB-POPULATIONS.
 *
 *   bank — the slow deep layer that gives the black its depth: huge, ponderous,
 *          long-lived, barely rotating, sitting low in the frame.
 *   wisp — the quicker shreds in front of them: smaller, thinner, shorter-lived,
 *          bobbing harder and spinning ~3× faster.
 *
 * The SPEED difference IS the parallax, and the SIZE difference is what makes it
 * read as two layers rather than one population at two distances.
 *
 * [rxMin]/[rxMax] is the LONG (horizontal) radius in WEB CSS px (see
 * [FOG_WEB_TO_REF_PX]); the quad is an ellipse (rx / aspect) because a rotating
 * radially-symmetric circle is invisible — rotation only reads on a
 * non-circular quad. Times are seconds; spin is rad/s; the rest is
 * dimensionless. Every [alphaMax] here is below [FOG_MAX_VEIL_ALPHA] by
 * construction, and FogFieldTest asserts it.
 */
internal class FogVeilKind(
    val share: Float,
    val rxMin: Float, val rxMax: Float,
    val aspectMin: Float, val aspectMax: Float,
    val alphaMin: Float, val alphaMax: Float,
    val vxMin: Float, val vxMax: Float,
    val vy: Float,
    val bobMin: Float, val bobMax: Float,
    val spinMin: Float, val spinMax: Float,
    val lifeMin: Float, val lifeMax: Float,
    val yMin: Float, val yMax: Float,
    val breatheMin: Float, val breatheMax: Float,
    val hue0Lo: Int, val hue0Hi: Int,
)

internal val FOG_BANK = FogVeilKind(
    share = 0.6f,
    rxMin = 200f, rxMax = 450f, aspectMin = 1.7f, aspectMax = 2.6f,
    alphaMin = 0.046f, alphaMax = 0.074f,
    vxMin = 4f, vxMax = 11f, vy = 0.8f,
    bobMin = 3f, bobMax = 9f, spinMin = 0.003f, spinMax = 0.010f,
    lifeMin = 42f, lifeMax = 78f, yMin = 0.42f, yMax = 1.06f,
    breatheMin = 0.04f, breatheMax = 0.10f, hue0Lo = 0, hue0Hi = 1,
)

internal val FOG_WISP = FogVeilKind(
    share = 0.4f,
    rxMin = 90f, rxMax = 200f, aspectMin = 2.2f, aspectMax = 3.4f,
    alphaMin = 0.027f, alphaMax = 0.050f,
    vxMin = 14f, vxMax = 34f, vy = 2.2f,
    bobMin = 6f, bobMax = 16f, spinMin = 0.012f, spinMax = 0.030f,
    lifeMin = 18f, lifeMax = 34f, yMin = 0.20f, yMax = 1.02f,
    breatheMin = 0.08f, breatheMax = 0.16f, hue0Lo = 1, hue0Hi = 2,
)

// -----------------------------------------------------------------------------
// Pure maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------

/** smoothstep with the input CLAMPED (the web spelling — envelope() feeds it
 *  values well outside [0,1] and relies on the clamp). */
private fun fogSmooth(t: Float): Float {
    val s = if (t < 0f) 0f else if (t > 1f) 1f else t
    return s * s * (3f - 2f * s)
}

/** ALPHA envelope over life (1 → 0): eases in as the veil forms, out as it
 *  dissipates, so a recycled slot never pops into or out of existence. */
internal fun fogEnvelope(life: Float): Float =
    min(fogSmooth((1f - life) / 0.18f), fogSmooth(life / 0.30f))

/** SIZE-OVER-LIFE curve: a veil dilates monotonically as it ages, 0.62 → 1.0.
 *  Multiplied by the particle's own sizeEnv (and its own breathe) at draw time,
 *  so no two veils dilate to the same size. [age] is 1 - life. */
internal fun fogSwell(age: Float): Float = 0.62f + 0.38f * fogSmooth(age)

/**
 * The web's `fieldScale()`, in Android units: "how busy is this viewport",
 * folded with the operator's count dial.
 *
 * [areaDp] is the viewport area in LOGICAL (dp) units — never device pixels, so
 * a 2× density screen gets the same population, not four times as much fog.
 * Both clamps are the engine's, verbatim.
 */
internal fun fogFieldScale(areaDp: Float, countScale: Float): Float =
    ((areaDp / FOG_REF_AREA_DP).coerceIn(0.4f, 2.4f) * countScale).coerceIn(0.12f, 4.0f)

/** Population target. Bounded HARD at both ends — this is a backdrop, and the
 *  ceiling is what keeps the frame at ~20 quads on the largest viewport. */
internal fun fogVeilCount(fieldScale: Float): Int =
    (9f + 5f * fieldScale).roundToInt().coerceIn(FOG_MIN_VEILS, FOG_MAX_VEILS)

/**
 * THE DRAW-SITE CEILING. Per-quad ceiling #2: [budgetK] is always ≤ 1 so this is
 * a backstop, and it is the line that must never be deleted. Shared with
 * FogFieldTest so the assertion tests the shipping code, not a copy of it.
 */
internal fun fogQuadAlpha(a: Float, budgetK: Float): Float =
    (a * budgetK).coerceAtMost(FOG_MAX_VEIL_ALPHA)

// -----------------------------------------------------------------------------
// One veil. A POOLED slot: recycled in place, never reallocated, so the hot path
// allocates nothing at all. `wisp` is a property of the SLOT, not of the draw —
// a bank does not become a wisp when it is recycled.
//
// Lengths (rx, ry, vx, vy, bobAmp) are WEB CSS px and are multiplied by
// FogSim.g at USE, never baked in, so a density or size change re-scales the
// live field instead of stranding it.
// -----------------------------------------------------------------------------
class FogVeil internal constructor(val wisp: Boolean) {
    var x = 0f; var y = 0f
    var rx = 0f                 // long (horizontal) radius, web CSS px
    var ry = 0f                 // short radius — always < rx, or rotation is invisible
    var sizeEnv = 1f            // per-particle size-envelope multiplier ── premium rule 1
    var alpha0 = 0f             // this veil's own alpha, before envelope + budget
    var a = 0f                  // CURRENT per-quad alpha (already ceiling-clamped)
    var vx = 0f                 // downwind drift, web CSS px/s
    var vy = 0f                 // slow vertical layering, web CSS px/s
    var bobAmp = 0f             // draw-time vertical bob, web CSS px
    var bobHz = 0f
    var breatheAmp = 0f         // draw-time size oscillation, dimensionless
    var breatheHz = 0f
    var phase = 0f              // private offset shared by bob + breathe
    var rot = 0f                // radians
    var spin = 0f               // rad/s, signed
    var life = 1f               // 1 → 0
    var decay = 0f              // 1 / lifetime-in-seconds
    var hue0 = 0                // base index into FOG_RAMP ── premium rule 2
}

/**
 * FogSim — the effect. Registered in FieldRegistry.kt as ("fog", "Low Fog");
 * nothing else in the codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning) —
 * it reaches the POPULATION only, exactly as the web folds its dials into
 * env.scale. It must never reach an alpha: the web's `env.intensity` is a hard
 * 1.0 baseline (ember-fx.js `FIELD.intensity`) and the legibility ceilings above
 * are calibrated against that.
 *
 * [rand] is the injectable randomness seam, so FogFieldTest can drive the whole
 * simulation from a seeded generator.
 */
class FogSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    /** Screen density, kept only to derive the dp-area population target.
     *  NOT named `density`: render() is a DrawScope member extension and
     *  DrawScope.density would shadow it. */
    private var deviceDensity = FIELD_REFERENCE_DENSITY
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<FogVeil>()

    /** ONE prevailing wind for the whole field (-1 or +1), drawn once at init.
     *  A counter-drifting veil would break the weather illusion instantly. */
    var wind = 1f
        private set

    /** Field-wide alpha normalizer, recomputed once per frame in [update].
     *  Always in (0, 1]: 1 when the field is under budget, ALPHA_BUDGET/sum when
     *  it is not. This is the ceiling a per-quad cap alone cannot give you. */
    var budgetK = 1f
        private set

    /** Private phase clock for the bob/breathe oscillators, advanced by dtSec.
     *  Double because a long session would run a Float out of precision. */
    private var clock = 0.0

    /** Test/inspection view of the pool. Never called from the hot path. */
    val veils: List<FogVeil> get() = pool.asList()

    /** Web CSS px → device px for THIS screen. Applied at use, never stored. */
    private val g: Float get() = scale * FOG_WEB_TO_REF_PX

    private fun rnd(): Float = rand.next().toFloat()
    private fun pick(lo: Float, hi: Float): Float = lo + rnd() * (hi - lo)

    private fun targetCount(): Int =
        fogVeilCount(fogFieldScale((width / deviceDensity) * (height / deviceDensity), countScale))

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val first = pool.isEmpty()
        val sameSize = !first && width == this.width && height == this.height
        this.width = width; this.height = height
        this.scale = scale; this.deviceDensity = density
        if (first) { initField(); return }
        if (sameSize) return
        // Population is viewport-derived; GEOMETRY is not, so only the count and
        // the apparent scale move. Drifting veils are left exactly where they are
        // — a rebuild on every resize would visibly restart the weather, and this
        // runs on every composer-driven size change. Nothing can be stranded: the
        // wrap in update() is computed against the CURRENT width/height, so a veil
        // outside a shrunken viewport re-enters on the very next frame.
        retarget(initial = false)
    }

    /** First valid size (or a rearm on an empty field): draw the wind, fill the
     *  pool with a STAGGERED set of lifetimes so the field cannot die as a cohort. */
    private fun initField() {
        wind = if (rnd() < 0.5f) -1f else 1f
        budgetK = 1f
        retarget(initial = true)
    }

    /**
     * Grow/trim the pool to the viewport's target IN PLACE. Survivors keep their
     * slot object — and therefore their drift, their phase and their age.
     *
     * Kinds are assigned the way the web assigns them: fill banks up to
     * round(target × bank share), then wisps. That makes the mix exact at ANY
     * population (8 veils → 5 banks / 3 wisps; 20 → 12 / 8), which a per-veil
     * coin flip could not promise — a field of twenty wisps and no banks has no
     * depth at all.
     */
    private fun retarget(initial: Boolean) {
        val want = targetCount()
        if (pool.size == want) return
        val old = pool
        val keep = min(old.size, want)
        var banks = 0
        for (i in 0 until keep) if (!old[i].wisp) banks++
        val wantBanks = (want * FOG_BANK.share).roundToInt()
        // Decide the new slots' species FIRST (a plain loop, not a captured var
        // inside the Array initialiser) — this runs on resize only, never in a frame.
        val newIsWisp = BooleanArray(want)
        for (i in keep until want) {
            val wisp = banks >= wantBanks
            if (!wisp) banks++
            newIsWisp[i] = wisp
        }
        pool = Array(want) { i ->
            if (i < keep) old[i] else FogVeil(newIsWisp[i]).also { reseed(it, initial) }
        }
    }

    /**
     * (Re)seed one veil IN PLACE. Every recycle reuses the same object, so the
     * pool is allocated once and update/render allocate nothing, ever.
     *
     * [initial] staggers the first fill across the screen and across the life
     * cycle; a recycled veil instead re-enters from the UPWIND edge, off-screen
     * by its own radius, and lets the envelope fade it in — a veil must never pop
     * into existence in the middle of the frame.
     *
     * The rand() call ORDER is the web module's, verbatim, so a seeded run can be
     * diffed against fog.js field by field.
     */
    private fun reseed(p: FogVeil, initial: Boolean) {
        val k = if (p.wisp) FOG_WISP else FOG_BANK
        p.rx = pick(k.rxMin, k.rxMax)
        p.ry = p.rx / pick(k.aspectMin, k.aspectMax)
        p.sizeEnv = 0.82f + rnd() * 0.42f
        p.alpha0 = pick(k.alphaMin, k.alphaMax)
        p.vx = wind * pick(k.vxMin, k.vxMax)        // one prevailing wind for the field
        p.vy = (rnd() - 0.5f) * 2f * k.vy           // slow vertical layering
        p.bobAmp = pick(k.bobMin, k.bobMax)
        p.bobHz = 0.05f + rnd() * 0.12f
        p.breatheAmp = pick(k.breatheMin, k.breatheMax)
        p.breatheHz = 0.04f + rnd() * 0.09f
        p.phase = rnd() * FOG_TAU
        p.rot = rnd() * FOG_TAU
        p.spin = pick(k.spinMin, k.spinMax) * (if (rnd() < 0.5f) -1f else 1f)
        p.decay = 1f / pick(k.lifeMin, k.lifeMax)
        p.hue0 = k.hue0Lo + (rnd() * (k.hue0Hi - k.hue0Lo + 1)).toInt()
        p.y = height * pick(k.yMin, k.yMax)
        if (initial) {
            p.x = rnd() * width
            p.life = 0.15f + rnd() * 0.85f          // stagger: they must not all die together
        } else {
            p.x = if (wind > 0f) -p.rx * g else width + p.rx * g
            p.life = 1f
        }
        p.a = 0f
    }

    /**
     * [active] is deliberately ignored, as it is on the web: fog is WEATHER, and
     * weather is continuous. There is nothing to drain and nothing to thin out —
     * EmberOverlay fades the whole canvas and parks the loop at DRAIN_MAX_MS,
     * which is the drain. [nowMs] is ignored too; see [clock].
     */
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        clock += dtSec.toDouble()
        val gg = g
        var sum = 0f
        for (p in pool) {
            p.life -= p.decay * dtSec
            if (p.life <= 0f) reseed(p, initial = false)   // recycle in place; the pool never grows
            // All motion is delta-timed: identical distance per second at 60 and 120 Hz.
            p.x += p.vx * gg * dtSec
            p.y += p.vy * gg * dtSec
            p.rot += p.spin * dtSec
            if (p.rot > FOG_TAU) p.rot -= FOG_TAU else if (p.rot < -FOG_TAU) p.rot += FOG_TAU
            // Wrap by the veil's OWN radius so a 1400 px bank never clips at an
            // edge — a hard-edged fog bank is the most obvious "these are quads"
            // tell there is.
            val mx = p.rx * gg
            val my = p.ry * gg
            if (p.x < -mx) p.x = width + mx else if (p.x > width + mx) p.x = -mx
            if (p.y < -my) p.y = height + my else if (p.y > height + my) p.y = -my
            // Per-quad ceiling #1 (compute site).
            p.a = (p.alpha0 * fogEnvelope(p.life)).coerceAtMost(FOG_MAX_VEIL_ALPHA)
            sum += p.a
        }
        // Field-wide ceiling: scale the WHOLE field down when the stack would
        // exceed the budget. One divide per frame buys a bounded luminance lift,
        // which is the only thing standing between this effect and unreadable
        // body text.
        budgetK = if (sum > FOG_ALPHA_BUDGET) FOG_ALPHA_BUDGET / sum else 1f
    }

    /** Weather is continuous: unlike embers there is nothing to drain and nothing
     *  to re-seed, so a rearm only has to guarantee the field EXISTS. Touching a
     *  live veil here would restart the weather on every generation. */
    override fun rearm() {
        if (pool.isNotEmpty() || width <= 0f || height <= 0f) return
        initField()
    }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (pool.isEmpty()) return
        // ── hoisted: one lookup per FRAME, never per particle ──
        // Our own grey ramp, baked through the shared radial bake (the same
        // gradient profile the web's makeSprite() uses) and memoized under our
        // own key. Fog never touches res.atlas, so the warm ember atlas is never
        // baked while this field is selected.
        val ramp: Array<android.graphics.Bitmap> = res.bake(FOG_SPRITE_KEY) { d ->
            Array(FOG_RAMP.size) { i ->
                val c = FOG_RAMP[i]
                bakeRadialSprite(c[0], c[1], c[2], d).asAndroidBitmap()
            }
        }
        // Our own SrcOver paint. FieldPaints.sprite carries FIELD_BLEND (additive
        // ≥ API 28) which every OTHER field wants and this one must not have —
        // see the header. Baked once per (density, effect) alongside the sprites,
        // so this is still zero allocation per frame; FieldResources is
        // remembered PER OVERLAY and only ever touched from the draw phase, so a
        // mutable paint living in it is safe by the same rule FieldPaints is.
        val paint: android.graphics.Paint = res.bake(FOG_PAINT_KEY) { _ ->
            android.graphics.Paint(android.graphics.Paint.FILTER_BITMAP_FLAG).apply {
                isAntiAlias = true
                // SrcOver is the Paint default and it is DELIBERATE: additive
                // would blow twenty overlapping veils out to white.
            }
        }
        val top = ramp.size - 1
        val ts = clock
        val gg = g
        val k = budgetK
        val canvas = drawContext.canvas.nativeCanvas
        val dst = paints.dst                       // per-overlay scratch; never ours to keep
        for (p in pool) {
            // Per-quad ceiling #2 (draw site). THE line that must never be deleted.
            val a = fogQuadAlpha(p.a, k)
            if (a <= 0.003f) continue              // same cull threshold as drawSpriteF
            val age = 1f - p.life
            // Breathe: a slow private size oscillation, on this veil's own Hz and
            // phase, so the field never pulses in unison.
            val breathe = 1f + p.breatheAmp * sin(ts * p.breatheHz + p.phase).toFloat()
            // SIZE = life curve × per-particle envelope × breathe. Never constant.
            val sw = fogSwell(age) * p.sizeEnv * breathe
            val rx = p.rx * gg * sw
            val ry = p.ry * gg * sw
            if (rx <= 0.1f || ry <= 0.1f) continue
            // COLOUR RAMP indexed by life: cool slate when fresh, warm dust when
            // dissipating, each sub-population starting on its own stop.
            val idx = (p.hue0 + (age * 2f).roundToInt()).coerceIn(0, top)
            val y = p.y + sin(ts * p.bobHz + p.phase).toFloat() * p.bobAmp * gg
            paint.alpha = (a * 255f).roundToInt()
            // Rotate about the veil's own centre. The quad is an ELLIPSE, which is
            // the only reason the (very slow) spin is visible at all.
            dst.set(-rx, -ry, rx, ry)
            val restore = canvas.save()
            canvas.translate(p.x, y)
            canvas.rotate(p.rot * FOG_RAD_TO_DEG)
            canvas.drawBitmap(ramp[idx], null, dst, paint)
            canvas.restoreToCount(restore)
        }
    }
}
