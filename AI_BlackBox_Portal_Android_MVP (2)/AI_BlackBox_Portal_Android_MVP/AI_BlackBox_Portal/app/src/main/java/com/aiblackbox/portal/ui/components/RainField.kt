package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: RAINFALL (id "rain").
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/rain.js
// (its physics tests: Portal/modules/fx/effects/rain.test.mjs).
//
// Fine near-vertical streaks falling at three depths — the near ones long and
// fast, the far ones short and slow — all leaning together in the wind.
//
// VELOCITY-STRETCHED BILLBOARDS. A drop is not a dot: it is a stroked segment
// from its head back to `head - velocity * k`, so streak length is DERIVED from
// speed rather than authored. Everything that changes a drop's speed (depth
// plane, per-drop variance, the acceleration toward terminal velocity after it
// enters frame) shows up in the geometry for free. No sprite, no gradient, no
// blur — the motion cue is the whole effect, which is also why this is the one
// field in the catalogue that touches neither FieldSprites nor drawSpriteF.
//
// PER-COLUMN WIND SHEAR. A single uniform lean angle is the cheap-version
// giveaway: it reads as a hatch pattern, not as weather. The lean here is a
// continuous standing wave across x that also breathes slowly in time, plus a
// per-drop jitter, so no two drops are exactly parallel while the field still
// leans as one.
//
// ── LEGIBILITY (non-negotiable — this is a BACKDROP behind chat text) ────────
//
// Vertical streaks run parallel to both the message column and the scroll
// direction, the one geometry that competes directly with reading. Three
// mitigations, all carried over from the web module as NUMBERS rather than good
// intentions, and all asserted in RainFieldTest:
//
//   1. the lean is held inside 10–15° off vertical (base 12.5 ± shear ± jitter),
//      enough to break the parallel with the text column;
//   2. the near plane never exceeds [RAIN_NEAR_ALPHA_CEILING] (25%) alpha at the
//      DEFAULT dial, heavy drops included — `RAIN_PLANES[2].alpha ×
//      RAIN_HEAVY.alpha = 0.2358`. The operator's Intensity dial multiplies that
//      at the draw site (see [rainDrawnAlpha]); asking for a brighter backdrop is
//      an explicit, reversible choice, and it is exactly what rain.js does with
//      its own `inten` term (`p.a = alphaMul × alphaEnvelope(u) × inten`);
//   3. population is capped at [RAIN_MAX_DROPS] (900) whatever the viewport asks.
//
// SrcOver, NOT additive. Rain is not emissive; under FIELD_BLEND (Plus) every
// crossing streak would sum toward white and eat the contrast of the chat text
// on top. This effect therefore bakes its OWN stroke paint through
// FieldResources.bake rather than borrowing FieldPaints.sprite (which is wired
// for additive sprite blits) or FieldPaints.matrix (a fill paint whose style
// would leak into MatrixSim).
//
// No trail/fade layer either: the velocity stretch already IS the trail, and a
// translucent fade would smear a second softer copy of every streak AND leave a
// permanent tint sitting between the text and the page background.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here
// is authored at FIELD_REFERENCE_DENSITY and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment it is USED, never
// baked into stored state, so the field has the same APPARENT size on a 2.0
// tablet and a 3.5 phone. Speeds, widths, streak lengths and the top/bottom
// bands are all stored in WEB CSS px and pass through `g`; positions are the
// only device-px state. Density may reach nothing else — not a count (counts
// come from dp AREA), not an alpha, not the lean, not the shear frequency.
// (Same law rain.js states as "`g` is apparentScale — GEOMETRY only, and it
// never sees devicePixelRatio". On the web the canvas is in CSS px so the fix is
// to ignore dpr; on Android the canvas is in device px so the fix is × scale.)
//
// dt. Velocities here are PIXELS PER SECOND and are multiplied by dtSec directly
// — no `dt * 60` normalizer. (Rising Stars is the other convention: its
// constants are px-per-60Hz-tick, so IT needs the normalizer. Pick one, say
// which in a comment, and never mix them inside one effect.) The only rate that
// is not a plain velocity — the exponential approach to terminal velocity — is
// clamped exactly as rain.js clamps it, so a long stall cannot overshoot.
// =============================================================================

import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.math.tan

// -----------------------------------------------------------------------------
// The legibility budget, as numbers other code (and the test) can read.
// -----------------------------------------------------------------------------

/** The near plane is the only layer bright enough to matter; keep it under this
 *  and body text keeps its contrast. `RAIN_PLANES.last().alpha ×
 *  RAIN_HEAVY.alpha` must stay below it — asserted in RainFieldTest. */
internal const val RAIN_NEAR_ALPHA_CEILING = 0.25f

/** Hard population ceiling, whatever the viewport asks for (rain.js MAX_DROPS). */
internal const val RAIN_MAX_DROPS = 900

/** ANDROID-ONLY floor, in drops. The web engine's viewport term bottoms out at
 *  0.4 so it never needed one; here the Intensity × Quality dial reaches 0.1 and
 *  a dozen streaks reads as "the effect is broken", not as drizzle. It only ever
 *  ADDS drops at the sparsest dial, so it cannot touch the ink budget above. */
internal const val RAIN_MIN_DROPS = 24

/** Bands above/below the viewport where drops fade in and out, in WEB CSS px. */
internal const val RAIN_TOP_BAND = 60f
internal const val RAIN_BOT_BAND = 60f

/** Horizontal over-spawn / wrap margin, in WEB CSS px (rain.js: ±60). */
internal const val RAIN_SPAWN_MARGIN = 60f

/**
 * WEB CSS px → reference-density DEVICE px.
 *
 * rain.js authors every length in CSS px and multiplies by
 * apparentScale(cssBox), which clamps to 0.5 on a phone-shaped viewport. A CSS
 * px is a dp, and FIELD_REFERENCE_DENSITY turns a dp into 3.1 device px:
 *     0.5 × 3.1 = 1.55
 * Keeping this factor explicit lets every tuning number below stay
 * BYTE-IDENTICAL to rain.js, so the two can be diffed forever.
 */
internal const val RAIN_WEB_TO_REF_PX = 1.55f

private const val RAIN_TAU = 6.2831855f
private const val RAIN_DEG = 0.017453292f          // π / 180

// -----------------------------------------------------------------------------
// Tunables. `lean` and the population dial are deliberately the two knobs a
// future weather mode would drive: swing the lean toward 15 and push the count
// past 1 and the same simulation is a squall; drop them and it is drizzle.
// Everything else is art direction that should NOT move with the weather.
// -----------------------------------------------------------------------------
internal const val RAIN_LEAN = 12.5f               // degrees off vertical, the field's base angle
internal const val RAIN_LEAN_SHEAR = 2.0f          // ± degrees of per-column shear on top of the base
internal const val RAIN_LEAN_JITTER = 0.35f        // ± degrees of per-drop jitter, so a column is not a hatch
private const val RAIN_SHEAR_HZ = 0.055f           // how slowly the shear pattern breathes (cycles/sec)
private const val RAIN_SHEAR_BANDS = 19f           // gust bands across the viewport width
private const val RAIN_STREAK = 0.095f             // SECONDS of travel a streak represents (length = |v| × this)

/** Exponential approach to terminal velocity, per second (rain.js accelK). */
private const val RAIN_ACCEL = 2.2f

/** A drop enters frame at this fraction of terminal velocity, so the streak
 *  GROWS as it falls instead of arriving at full length. */
private const val RAIN_ENTRY_SPEED = 0.72f

/** Below this the streak is invisible and costs a draw call for nothing. */
private const val RAIN_MIN_ALPHA = 0.002f

/**
 * THE THREE DEPTH PLANES — sub-populations 1–3.
 *
 * Far reads as haze, near as rain. SPEED drives length (velocity-stretched
 * billboards), so the speed spread IS the depth cue; alpha and width only
 * reinforce it. `tint` biases where the plane ENTERS the colour ramp, so the far
 * plane also sits at the pale end for its whole life — aerial perspective and
 * the life ramp on one axis.
 *
 * Speeds and widths are WEB CSS px (see [RAIN_WEB_TO_REF_PX]); alpha and `heavy`
 * are dimensionless fractions.
 */
internal class RainPlane(
    val count: Int,
    val speedMin: Float, val speedMax: Float,
    val width: Float,
    val alpha: Float,
    val len: Float,
    val heavy: Float,
    val tint: Int,
)

// ── ANDROID PRESENCE BOOST (Brandon, device-validated on the Fold 2026-07-25) ──
// "Rainfall looks pretty good, could be a bit brighter. For the scale I have the
// scale all the way up and I just barely can see the rain."
//
// Two separate faults, fixed together. The dial one is in [RainSim.render] (rain
// draws with its OWN stroke paint, so it never saw FieldPaints.alphaScale and the
// Intensity slider was adding drops of identical faintness). This table is the
// other: STROKE WIDTH IN DEVICE PIXELS.
//
// A web width of 0.55 CSS px lands on Android at 0.55 × 0.5 (apparentScale on a
// phone-shaped box) × 3.1 (density) = 0.85 DEVICE px. Antialiasing spreads a
// sub-pixel stroke across two pixel columns at roughly a third coverage each, and
// THEN the alpha below multiplies that — so the far plane, 52% of the population,
// was rendering at an effective ~0.03 alpha per pixel. Below roughly 1.5 device
// px a stroke stops reading as a line and starts reading as noise (the identical
// failure just fixed in SlipstreamField.kt). The widths are therefore lifted so
// every plane clears [RAIN_MIN_STROKE_DEVICE_PX], and the alphas by a smaller
// step on top:
//
//   plane   width web→device px      alpha
//   far     0.55→1.00   0.85→1.55    0.085→0.105
//   mid     0.90→1.30   1.40→2.02    0.140→0.165
//   near    1.45→1.90   2.25→2.95    0.190→0.205   (×1.15 heavy = 0.2358 < 0.25)
//
// This DIVERGES from rain.js and that is correct: the cross-surface contract is
// the same APPARENT RESULT, and the same numbers demonstrably do not survive the
// density difference. Do not "restore parity" — parity here means matching the
// web's measured ink coverage (0.059% of the viewport, at 9.5–11.1:1 against
// #C9C9C9 body text where AA needs 4.5:1), which the numbers above do: they land
// Android at ~0.05%, still just UNDER the web field the contrast was measured on.
// The width RATIO is deliberately compressed (1:1.64:2.64 → 1:1.30:1.90) rather
// than shifted wholesale — depth is carried by SPEED→length here, width only
// reinforces it, and shifting the whole table would have put the heavy near drops
// over 5 device px, which reads as a bar rather than a streak.
internal val RAIN_PLANES = arrayOf(
    RainPlane(130, 130f, 190f, 1.00f, 0.105f, 0.55f, 0.00f, 0),   // far   — haze
    RainPlane(80, 300f, 390f, 1.30f, 0.165f, 0.85f, 0.05f, 1),    // mid
    RainPlane(42, 560f, 700f, 1.90f, 0.205f, 1.15f, 0.09f, 2),    // near  — the only bright plane
)

/**
 * Presence floor for a stroke, in DEVICE px at [FIELD_REFERENCE_DENSITY]. Below
 * this a stroke antialiases into noise instead of reading as a line — see the
 * ANDROID PRESENCE BOOST note above. Every plane's `width × RAIN_WEB_TO_REF_PX`
 * must clear it; asserted in RainFieldTest.
 */
internal const val RAIN_MIN_STROKE_DEVICE_PX = 1.45f

/** …and the ceiling that keeps it a streak rather than a bar: the widest stroke
 *  the config can produce (near plane × a heavy drop) must stay under this. */
internal const val RAIN_MAX_STROKE_DEVICE_PX = 5.0f

/**
 * SUB-POPULATION 4 — the occasional fat drop, in the two closer planes only.
 * Its alpha multiplier stays UNDER the ceiling by construction: a heavy drop is
 * bigger and faster, not brighter.
 */
internal class RainHeavy(val speed: Float, val width: Float, val alpha: Float, val len: Float)

internal val RAIN_HEAVY = RainHeavy(speed = 1.18f, width = 1.5f, alpha = 1.15f, len = 1.25f)

/** Sum of the authored plane counts — the baseline the dial multiplies. */
internal val RAIN_BASE_COUNT: Int = RAIN_PLANES.sumOf { it.count }

/**
 * COLOUR RAMP, indexed by fall progress. Pale hazy blue-white at the top (rain
 * high in the frame is lit and far away) deepening to slate at the bottom. A
 * drop's plane biases its STARTING stop; `u` walks it three stops further down.
 *
 * This field does NOT use the shared warm blackbody atlas — rain is cold, and
 * res.atlas would bake seven sprites it never draws. The ramp is packed to
 * 0x00RRGGBB ONCE at class-load so the draw phase never builds a colour.
 */
internal val RAIN_RAMP = arrayOf(
    intArrayOf(226, 240, 250), intArrayOf(198, 222, 242), intArrayOf(168, 202, 232),
    intArrayOf(138, 180, 220), intArrayOf(110, 156, 202), intArrayOf(86, 130, 180),
    intArrayOf(66, 106, 152),
)
internal val RAIN_RAMP_TOP = RAIN_RAMP.size - 1
private val RAIN_RGB = IntArray(RAIN_RAMP.size) { i ->
    val c = RAIN_RAMP[i]
    (c[0] shl 16) or (c[1] shl 8) or c[2]
}

/**
 * Reference viewport for the population, in dp² — the web engine's fieldScale()
 * constant (1440 × 900). The viewport term is LINEAR in area (not the √area
 * fireflies uses: fireflies budgets density-of-LIGHT, rain budgets a curtain)
 * and clamps to [0.40, 2.40], which is why a phone-shaped box lands on the floor
 * and gets a denser-per-dp field than a desktop window would.
 */
internal const val RAIN_REF_AREA_DP = 1440f * 900f
private const val RAIN_VIEWPORT_MIN = 0.40f        // ember-fx.js fieldScale()
private const val RAIN_VIEWPORT_MAX = 2.40f
private const val RAIN_BUILD_MIN = 0.35f           // rain.js build(): clamp on env.scale
private const val RAIN_BUILD_MAX = 2.40f

/**
 * The viewport half of the population multiplier — everything except the
 * operator's dial. Exported so RainFieldTest can predict the pool size instead
 * of restating the formula (and drifting from it).
 */
internal fun rainViewportScale(width: Float, height: Float, density: Float): Float {
    if (width <= 0f || height <= 0f || density <= 0f) return RAIN_VIEWPORT_MIN
    val areaDp = (width / density) * (height / density)
    // Two clamps, deliberately kept separate so both sides stay diffable: the
    // first is ember-fx.js fieldScale(), the second is rain.js build()'s own
    // clamp on env.scale. The second is a no-op today; it is here so that a
    // change to either file shows up as a change to one line here.
    return (areaDp / RAIN_REF_AREA_DP)
        .coerceIn(RAIN_VIEWPORT_MIN, RAIN_VIEWPORT_MAX)
        .coerceIn(RAIN_BUILD_MIN, RAIN_BUILD_MAX)
}

/**
 * The full population multiplier: the operator's dial × the viewport, held
 * between the Android drop floor and the 900-drop cap.
 *
 * Applying the cap HERE rather than per plane is the one place this improves on
 * rain.js: the web truncates the last layer once the running total reaches
 * MAX_DROPS (`i < want && n < MAX_DROPS`), which on a big tablet at a high dial
 * deletes the NEAR plane outright — the brightest, most characteristic third of
 * the effect. Scaling the multiplier instead honours the same cap to the drop
 * while keeping all three depths.
 */
internal fun rainPopulationScale(
    countScale: Float,
    width: Float,
    height: Float,
    density: Float,
): Float = (ParticleTuning.sanitizeScale(countScale) * rainViewportScale(width, height, density))
    .coerceIn(
        RAIN_MIN_DROPS.toFloat() / RAIN_BASE_COUNT,
        RAIN_MAX_DROPS.toFloat() / RAIN_BASE_COUNT,
    )

/** How many drops plane [plane] gets at population multiplier [d]. Never zero —
 *  a depth cue needs all three planes, however thin the dial. */
internal fun rainPlaneCount(plane: Int, d: Float): Int =
    max(1, (RAIN_PLANES[plane].count * d).roundToInt())

// -----------------------------------------------------------------------------
// Pure envelope maths — no state, no allocation, unit-testable on its own.
// `u` is FALL PROGRESS: 0 at the top band, 1 at the recycle line. Spatial, not a
// timer, so every per-life curve lands exactly on the recycle line — no pop.
// -----------------------------------------------------------------------------

/**
 * SIZE over life. Rises fast out of the dark band above the viewport, holds
 * through the readable middle, tapers as the drop leaves the bottom. Multiplied
 * by a PER-DROP envelope (`lenMul`) so two drops at the same speed and the same
 * height are still different lengths — the size-over-life premium rule.
 */
internal fun rainLengthEnvelope(u: Float): Float {
    val arrive = if (u < 0.16f) u / 0.16f else 1f
    val leave = if (u > 0.88f) (1f - u) / 0.12f else 1f
    return (0.5f + 0.5f * arrive * (2f - arrive)) * (0.78f + 0.22f * leave)
}

/**
 * ALPHA over life. Zero at both ends — a drop must never pop into or out of
 * existence at the edge of the viewport, which is the tell that a field is
 * recycling rather than falling.
 */
internal fun rainAlphaEnvelope(u: Float): Float {
    val inK = if (u < 0.08f) u / 0.08f else 1f
    val outK = if (u > 0.82f) (1f - u) / 0.18f else 1f
    return inK * outK
}

/** The `u` band in which BOTH envelopes above are saturated at 1. Derived from
 *  their break points (length saturates at 0.16, alpha at 0.82), NOT a separate
 *  tunable — it exists so the renderer can batch that majority without a float
 *  equality test on a value that is only 1.0 to within an ULP. */
private const val RAIN_FLAT_LO = 0.16f
private const val RAIN_FLAT_HI = 0.82f

/** Two multiplied waves → a shear field with no repeating period you can read. */
internal fun rainShearWave(bx: Float, t: Float): Float =
    sin(bx + t * RAIN_TAU * RAIN_SHEAR_HZ) *
        cos(bx * 0.61f - t * RAIN_TAU * RAIN_SHEAR_HZ * 0.61f)

// -----------------------------------------------------------------------------
// One drop. A POOLED slot: recycled in place, never reallocated, so the hot path
// allocates nothing at all.
//
// Everything is stored in WEB CSS px (density-free) EXCEPT [x]/[y]/[tx]/[ty],
// which are canvas device px — see the DPI note in the file header.
// -----------------------------------------------------------------------------
class RainDrop internal constructor() {
    /** Depth plane, 0 (far haze) … 2 (near). Assigned when the pool is built and
     *  never by a recycle — a drop does not change depth when it falls off the
     *  bottom. A rebuild re-partitions the pool into plane blocks, which is the
     *  only time this moves. */
    var plane = 0
        internal set

    var x = 0f                 // head, device px
    var y = 0f
    var tx = 0f                // tail = head − velocity × kEff, device px
    var ty = 0f
    var vx = 0f                // web CSS px/s
    var vy = 0f
    var vt = 0f                // terminal velocity, web CSS px/s
    var u = 0f                 // fall progress, 0 → 1
    var wid = 0f               // stroke width, web CSS px
    var a = 0f                 // final alpha, already through the life envelope
    var ramp = 0               // index into RAIN_RAMP
    var lean = 0f              // degrees off vertical, this frame
    var lenMul = 1f            // per-drop length envelope depth  ── premium rule 1
    var widMul = 0f            // web CSS px
    var alphaMul = 0f          // plane × heavy alpha, BEFORE the life envelope
    var tint = 0               // ramp entry stop (plane-biased)
    var jit = 0f               // per-drop lean jitter, degrees
    var heavy = false
    var dead = false
    /** Both life envelopes saturated ⇒ this drop's alpha and width are exactly
     *  its plane's, so the renderer may batch it. Derived, never authored. */
    var flat = false
        internal set
}

/**
 * RainSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so RainFieldTest can drive the whole
 * simulation from a seeded generator.
 */
class RainSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    private var density = FIELD_REFERENCE_DENSITY
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<RainDrop>()

    /** 1 / (fall span in device px) — the denominator behind every life curve. */
    private var uk = 1f

    /** Viewport the current pool was built for, so a rebuild can re-spread the
     *  survivors instead of stranding them (see [build]). */
    private var builtWidth = 0f
    private var builtG = 0f

    /** Per-plane pool sizes. A field, not a local, so [build] allocates nothing. */
    private val wants = IntArray(RAIN_PLANES.size)

    /** Test/inspection view of the pool. Never called from the hot path. */
    val drops: List<RainDrop> get() = pool.asList()

    // ── Render scratch. NOT simulation state: update() never reads any of it, so
    // render stays a pure read of the physics. Sized in build(), never in a loop.
    private var seg = FloatArray(0)
    private val bucketCount = IntArray(RAIN_BUCKETS)
    private val bucketStart = IntArray(RAIN_BUCKETS)
    private val bucketFill = IntArray(RAIN_BUCKETS)

    /** Web CSS px → device px for THIS screen. Applied at use, never baked into
     *  stored state, so a density or size change re-scales the live field. */
    private val g: Float get() = scale * RAIN_WEB_TO_REF_PX

    private fun rnd(): Float = rand.next().toFloat()
    private fun pick(lo: Float, hi: Float): Float = lo + rnd() * (hi - lo)

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        // drawWithCache calls this on every size pass; a live field must never
        // re-scatter because nothing changed.
        if (width == this.width && height == this.height &&
            scale == this.scale && density == this.density && pool.isNotEmpty()
        ) return
        this.width = width; this.height = height
        this.scale = scale; this.density = density
        build()
    }

    /**
     * (Re)build the pool for the current viewport. Population, geometry scale and
     * the fall-progress denominator are all viewport-derived, so a genuine resize
     * has to rebuild — but IN PLACE: slot objects are reused, survivors keep
     * their fall, and only new slots (or slots whose plane moved) are re-seeded.
     */
    private fun build() {
        val gg = g
        uk = 1f / max(1f, height + (RAIN_TOP_BAND + RAIN_BOT_BAND) * gg)

        // ── population ──
        val d = rainPopulationScale(countScale, width, height, density)
        var total = 0
        for (li in RAIN_PLANES.indices) {
            // The multiplier is already capped, so the second clamp can only ever
            // fire on a rounding edge — it is here so the pool can never outrun
            // the buffer sized from it.
            wants[li] = min(rainPlaneCount(li, d), max(0, RAIN_MAX_DROPS - total))
            total += wants[li]
        }

        // ── re-spread the SURVIVORS across the new width first ──
        // A real width growth (unfolding a Fold, rotating to landscape) reveals a
        // strip the live field has no drops in, and rain only enters from the top
        // — at the far plane's 250 px/s that strip stays empty for ten seconds.
        // Rescaling x keeps a uniform distribution uniform, so it fills instantly
        // with no visible re-scatter. (Height changes self-heal: drops recycle at
        // the CURRENT bottom edge.)
        if (builtWidth > 0f && pool.isNotEmpty() && (builtWidth != width || builtG != gg)) {
            val oldM = RAIN_SPAWN_MARGIN * builtG
            val newM = RAIN_SPAWN_MARGIN * gg
            val k = (width + 2f * newM) / max(1f, builtWidth + 2f * oldM)
            for (p in pool) {
                p.x = (p.x + oldM) * k - newM
                // Collapse the streak too: update() re-derives it from the new
                // velocity next frame, and a tail left at the OLD x would draw one
                // frame of a streak stretched across the resize.
                p.tx = p.x
                p.ty = p.y
            }
        }

        // ── grow / trim IN PLACE, then re-seed only what actually changed ──
        val old = pool
        if (old.size != total) {
            pool = Array(total) { i -> if (i < old.size) old[i] else RainDrop() }
            seg = FloatArray(4 * total)
        }
        var idx = 0
        for (li in RAIN_PLANES.indices) {
            var k = 0
            while (k < wants[li]) {
                val p = pool[idx]
                if (idx >= old.size || p.plane != li) {
                    p.plane = li
                    recycle(p, initial = true)
                }
                idx++; k++
            }
        }
        builtWidth = width
        builtG = gg
    }

    /**
     * (Re)seed one drop IN PLACE.
     *
     * [initial] staggers it anywhere down the fall instead of putting it in the
     * band above the viewport, so a build (and a rearm) produces a full field
     * rather than a curtain dropping in from above.
     */
    private fun recycle(p: RainDrop, initial: Boolean) {
        val plane = RAIN_PLANES[p.plane]
        val gg = g
        val heavy = rnd() < plane.heavy
        p.heavy = heavy
        // Terminal velocity in WEB CSS px/s — density-free, so `g` reaches it only
        // at the moment it moves a coordinate.
        p.vt = pick(plane.speedMin, plane.speedMax) * (if (heavy) RAIN_HEAVY.speed else 1f)
        p.vy = p.vt * RAIN_ENTRY_SPEED
        p.lean = RAIN_LEAN
        p.vx = p.vy * tan(RAIN_LEAN * RAIN_DEG)
        p.lenMul = plane.len * (if (heavy) RAIN_HEAVY.len else 1f) * (0.8f + rnd() * 0.45f)
        // NOTE (the one deliberate divergence from rain.js): the web multiplies
        // the width by a continuous per-drop (0.85 + rand × 0.3) jitter. Width is
        // a PAINT property, and on Android a distinct paint property costs a
        // distinct draw call — a continuous width jitter would put every drop in
        // its own batch and undo the whole point of drawLines. It is worth ±15%
        // of a 1.6–3.0 device px line, while the per-drop LENGTH envelope
        // (lenMul, continuous, and free because it lives in the vertex data)
        // already carries the "no two drops identical" premium rule. Dropping it
        // leaves the mean width and therefore the ink budget unchanged.
        p.widMul = plane.width * (if (heavy) RAIN_HEAVY.width else 1f)
        p.alphaMul = plane.alpha * (if (heavy) RAIN_HEAVY.alpha else 1f)
        p.tint = plane.tint + (rnd() * 1.999f).toInt()        // plane-biased ramp entry
        p.jit = (rnd() - 0.5f) * 2f * RAIN_LEAN_JITTER
        val m = RAIN_SPAWN_MARGIN * gg
        p.x = rnd() * (width + 2f * m) - m
        p.y = if (initial) {
            rnd() * (height + (RAIN_TOP_BAND + RAIN_BOT_BAND) * gg) - RAIN_TOP_BAND * gg
        } else {
            -RAIN_TOP_BAND * gg - rnd() * (height * 0.25f + 40f * gg)
        }
        p.u = 0f; p.wid = 0f; p.a = 0f; p.ramp = p.tint
        p.tx = p.x; p.ty = p.y
        p.dead = false; p.flat = false
    }

    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        val gg = g
        val t = (nowMs * 0.001).toFloat()
        // Dimensionless: 19 gust bands across the viewport whatever its pixel
        // size, so the shear pattern is identical at every DPI.
        val bandK = RAIN_TAU * RAIN_SHEAR_BANDS / max(1f, width)
        val recycleY = height + RAIN_BOT_BAND * gg
        val leftEdge = -RAIN_SPAWN_MARGIN * gg
        val rightEdge = width + RAIN_SPAWN_MARGIN * gg
        val wrapSpan = width + 2f * RAIN_SPAWN_MARGIN * gg
        val topBand = RAIN_TOP_BAND * gg
        // Clamped exponential approach — a rate per SECOND, so it is delta-timed,
        // and clamped so a long stall cannot overshoot terminal velocity.
        val accelK = min(1f, RAIN_ACCEL * dtSec)

        for (p in pool) {
            if (p.dead) continue

            // Wind: base lean + continuous per-column shear + per-drop jitter.
            val lean = RAIN_LEAN + RAIN_LEAN_SHEAR * rainShearWave(p.x * bandK, t) + p.jit
            p.lean = lean
            p.vy += (p.vt - p.vy) * accelK
            p.vx = p.vy * tan(lean * RAIN_DEG)
            p.x += p.vx * gg * dtSec
            p.y += p.vy * gg * dtSec

            if (p.y > recycleY) {
                // Idle: let the field drain instead of raining over a quiet chat.
                if (!active) { p.dead = true; p.a = 0f; continue }
                recycle(p, initial = false)
            } else if (p.x < leftEdge) {
                p.x += wrapSpan            // wrap BEFORE the tail is derived, or the
            } else if (p.x > rightEdge) {  // streak spans the viewport for a frame
                p.x -= wrapSpan
            }

            // Fall progress drives every per-life curve. SPATIAL, not a timer, so
            // the fade-out lands exactly on the recycle line — no pop.
            val u = ((p.y + topBand) * uk).coerceIn(0f, 1f)
            p.u = u
            val le = rainLengthEnvelope(u)
            val kEff = RAIN_STREAK * p.lenMul * le          // seconds of travel this streak shows
            p.tx = p.x - p.vx * gg * kEff
            p.ty = p.y - p.vy * gg * kEff
            p.wid = p.widMul * (0.65f + 0.35f * le)
            p.a = p.alphaMul * rainAlphaEnvelope(u)
            // COLOUR RAMP indexed by fall progress: the plane's tint is the entry
            // stop, `u` walks three stops deeper as the drop falls.
            p.ramp = min(RAIN_RAMP_TOP, p.tint + (u * 3f + 0.5f).toInt())
            p.flat = u >= RAIN_FLAT_LO && u <= RAIN_FLAT_HI
        }
    }

    /** Rain resumes as a full field, not a drought: revive whatever drained while
     *  the chat was idle, staggered down the fall so it does not arrive as a wall
     *  — and WITHOUT touching the drops that are still falling. */
    override fun rearm() {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        for (p in pool) if (p.dead) recycle(p, initial = true)
    }

    /**
     * Draw. Every drop is ONE line segment, so this field never blits a sprite —
     * it hands whole runs of segments to `Canvas.drawLines` out of a FloatArray
     * that was sized in [build] and is refilled (never reallocated) each frame.
     *
     * TWO TIERS, because `drawLines` takes exactly one Paint and rain wants three
     * per-drop paint properties (colour, alpha, width):
     *
     *  • BATCHED — the ~two thirds of the field whose life envelopes are both
     *    saturated (`flat`). Their alpha IS their plane's and their width IS
     *    their plane's, so they group losslessly by (plane, heavy, ramp stop):
     *    ≤ 42 buckets, filled by a counting sort, one drawLines each. Bucket
     *    count is bounded by the CONFIG, not by the population, so this is what
     *    keeps a 900-drop tablet field off the per-drop path.
     *  • EXACT — the drops inside the top/bottom fade bands, where alpha and
     *    width are still moving. They are a minority and they get a plain
     *    drawLine apiece, at their exact values: the anti-pop fade at the
     *    viewport edges is the one thing that must not be quantized.
     *
     * BOTH tiers multiply through [rainDrawnAlpha]. This field bakes its own
     * stroke paint instead of borrowing FieldPaints.sprite, so it does NOT go
     * through drawSpriteF and would otherwise never see the Intensity dial's
     * brightness multiplier — which is exactly the bug Brandon hit on the Fold
     * ("I have the scale all the way up and I just barely can see the rain").
     */
    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (pool.isEmpty() || seg.isEmpty()) return
        // Hoisted: one lookup per FRAME, never per drop. Baked through
        // FieldResources so it is memoized per (density, effect) and cannot leak
        // its STROKE style into another effect's paint.
        val paint = res.bake(RAIN_PAINT_KEY) { _ -> rainStrokePaint() }
        val nc = drawContext.canvas.nativeCanvas
        val gg = g
        // The Intensity dial's BRIGHTNESS multiplier. Read once per frame, never
        // per drop; FieldPaints is per-overlay mutable scratch and the dial can
        // move while the field is live.
        val ab = paints.alphaScale

        // pass 1 — count
        for (b in bucketCount.indices) bucketCount[b] = 0
        for (p in pool) {
            if (p.dead || !p.flat || p.a <= RAIN_MIN_ALPHA) continue
            bucketCount[rainBucketOf(p)]++
        }
        // prefix sum → each bucket's slice of the shared segment buffer
        var off = 0
        for (b in bucketStart.indices) {
            bucketStart[b] = off
            bucketFill[b] = off
            off += bucketCount[b]
        }
        // pass 2 — scatter the segments into their slices
        for (p in pool) {
            if (p.dead || !p.flat || p.a <= RAIN_MIN_ALPHA) continue
            val b = rainBucketOf(p)
            val slot = bucketFill[b]
            bucketFill[b] = slot + 1
            val i = slot * 4
            seg[i] = p.tx; seg[i + 1] = p.ty; seg[i + 2] = p.x; seg[i + 3] = p.y
        }
        // pass 3 — one drawLines per non-empty bucket
        for (b in bucketStart.indices) {
            val n = bucketCount[b]
            if (n == 0) continue
            val plane = RAIN_PLANES[b / (2 * RAIN_RAMP_N)]
            val heavy = (b / RAIN_RAMP_N) % 2 == 1
            paint.color = rainArgb(
                rainDrawnAlpha(plane.alpha * (if (heavy) RAIN_HEAVY.alpha else 1f), ab),
                b % RAIN_RAMP_N,
            )
            paint.strokeWidth = plane.width * (if (heavy) RAIN_HEAVY.width else 1f) * gg
            nc.drawLines(seg, bucketStart[b] * 4, n * 4, paint)
        }
        // pass 4 — the fade bands, exact, per drop
        for (p in pool) {
            if (p.dead || p.flat || p.a <= RAIN_MIN_ALPHA) continue
            paint.color = rainArgb(rainDrawnAlpha(p.a, ab), p.ramp)
            paint.strokeWidth = p.wid * gg
            nc.drawLine(p.tx, p.ty, p.x, p.y, paint)
        }
    }
}

// -----------------------------------------------------------------------------
// Render helpers. Batch key = (plane, heavy, ramp stop) — the exact set of
// properties that reach the Paint, and nothing that scales with the population.
// -----------------------------------------------------------------------------
// Both DERIVED, never restated: adding a ramp stop or a depth plane must grow the
// bucket table with it, or rainBucketOf() walks off the end of it.
private val RAIN_RAMP_N = RAIN_RAMP.size
private val RAIN_BUCKETS = RAIN_PLANES.size * 2 * RAIN_RAMP_N      // planes × heavy × ramp
private const val RAIN_PAINT_KEY = "rain.stroke"

private fun rainBucketOf(p: RainDrop): Int =
    ((p.plane shl 1) or (if (p.heavy) 1 else 0)) * RAIN_RAMP_N + p.ramp

/**
 * The alpha a streak is actually STROKED at: the drop's life-envelope alpha [a]
 * times the Intensity dial's brightness multiplier [alphaScale]
 * (FieldPaints.alphaScale, from ParticleTuning.brightnessScale).
 *
 * The ONE seam where the dial reaches this effect, because rain is a line field:
 * it bakes its own stroke paint and never calls drawSpriteF, which is where every
 * sprite-based field picks the multiplier up for free. Both render tiers go
 * through here so the batched and the exact path can never drift apart, and so
 * RainFieldTest can assert the dial actually moves the drawn alpha without
 * needing a Canvas.
 *
 * At the default dial `alphaScale` is exactly 1, so this is byte-for-byte the old
 * behaviour — and it is the same term rain.js applies as `inten`.
 */
internal fun rainDrawnAlpha(a: Float, alphaScale: Float): Float {
    if (!alphaScale.isFinite()) return a.coerceIn(0f, 1f)
    return (a * alphaScale).coerceIn(0f, 1f)
}

private fun rainArgb(a: Float, ramp: Int): Int =
    ((a.coerceIn(0f, 1f) * 255f).roundToInt() shl 24) or RAIN_RGB[ramp]

/**
 * The stroke paint. SrcOver on purpose — NOT FIELD_BLEND: rain is not emissive,
 * and under Plus every crossing streak would sum toward white behind the chat
 * text. Round caps mirror the web's `ctx.lineCap = 'round'`, which is what keeps
 * a streak tapering INTO the sub-pixel grid at the ends of the fade bands (where
 * the width envelope dips to ~0.8x) instead of ending on a hard square.
 */
private fun rainStrokePaint(): android.graphics.Paint =
    android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply {
        style = android.graphics.Paint.Style.STROKE
        strokeCap = android.graphics.Paint.Cap.ROUND
    }
