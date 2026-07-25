package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: CINDERS ON THE WIND (id "cinders").
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/cinders.js
// (its physics tests: Portal/modules/fx/effects/cinders.test.mjs).
//
// On the web, cinders is a PARAMETER FORK of effects/embers.js. Here it is the
// same fork of [EmberSim]: the curl-noise potential, the blackbody sprite ramp,
// the two-pass additive bloom, the dead-slot free list and the horizontal wrap
// are transcribed unchanged. What moves is the FORCE BUDGET, so the same field
// reads as WEATHER instead of a fire:
//
//   buoyancy   9 → 0.9      an ember floats up on its own heat; a cinder has
//                           stopped burning and has nothing left to lift it, so
//                           it goes where the air goes.
//   curl gain  5200 → 1900  the swirl stops BEING the motion and becomes an
//                           undulation ON a current. Above ~2500 the eddies win
//                           and motes visibly travel upwind, which reads as
//                           chaos rather than weather.
//   spark sink 60 → 18      heavy sparks still settle, but only a little.
//   + WIND                  one global vector every mote accelerates toward,
//                           with gusts that briefly scatter the field.
//
// EmberSim is NOT touched — ParticleFieldTest pins its behaviour. This is a
// separate class with its own constants, exactly as the web keeps two modules.
//
// ── The three premium rules, carried over from the web module ──────────────
//   1. SIZE OVER LIFE with a per-particle envelope: [cinderSizeOverLife] ×
//      `p.swell`, drawn from [CINDER_SWELL_MIN]..[CINDER_SWELL_MAX]. No mote is
//      the same size twice and no two motes swell by the same amount.
//   2. LIFE-INDEXED COLOUR RAMP: [cinderRampIndex] walks the shared 6-stop
//      blackbody atlas from white-hot at birth to deep ember red at burnout,
//      each sub-population over its own stretch of it.
//   3. TWO SUB-POPULATIONS: a CINDER is a spent flake — big, draggy, low sail,
//      it rides the mean current. A SPARK is still hot — tiny, bright, more sail
//      and over twice the gust gain, so a gust throws the sparks across the
//      frame while the cinders merely lean into it.
//
// ── LEGIBILITY ─────────────────────────────────────────────────────────────
// This is a BACKDROP behind chat text, and the budget is the APPROVED EMBERS
// BUDGET: the same per-particle alpha ceiling (0.85 spark / 0.55 cinder), bloom
// multipliers no larger than Embers', the same dp-area population rule and hard
// cap, and a geometry multiplier ([cinderGeom]) that may only ever SHRINK the
// field below Embers, never grow it. The size-over-life peak (0.9385) times the
// top of the swell range (1.06) is 0.9948 — under 1.0 BY CONSTRUCTION, which is
// what guarantees a drawn radius never exceeds the spawn radius and therefore
// that no mote is ever a bigger blob over a glyph than Embers'.
// CindersFieldTest asserts every one of those rather than describing them.
//
// ── The two Android-specific contracts ─────────────────────────────────────
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Velocities and radii are
// stored in WEB CSS px (the units cinders.js is authored in) and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment they are USED, never
// baked into stored state — so a density or size change re-scales the live field
// instead of stranding it, and the constants below stay byte-identical to the
// .js so the two surfaces can be diffed forever. Density reaches exactly two
// other things, both of which are dp-derived and therefore density-INVARIANT for
// a given physical screen: the population ([targetMax], px → dp area, the
// documented count exception) and [cinderGeom] (px → dp box, the port of the
// web's `apparentScale`, which is "a function of the CSS box ONLY"). A test
// pins that: same seed at 2× density gives exactly 2× every coordinate, the same
// population and the same stored geometry.
//
// Note the difference from FirefliesSim, which folds the web's apparentScale into
// a single WEB_TO_REF_PX = 1.55 constant. It can, because fireflies.js multiplies
// its own lengths by apparentScale. cinders.js does NOT — it applies the
// multiplier to the DRAWN RADIUS ONLY — so the factor is carried here as an
// explicit [cinderGeom] at draw time instead, and the raw lengths keep EmberSim's
// convention (web CSS px × scale). That is also what makes the legibility budget
// meaningful: this field is measured against the Android EmberSim, so it has to
// be in the Android EmberSim's units.
//
// dt. Velocities are PIXELS PER SECOND and are multiplied by dtSec directly — no
// `dt * 60` normalizer, the same convention as EmberSim and FirefliesSim.
// =============================================================================

import androidx.compose.ui.graphics.drawscope.DrawScope
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * THE WIND. Deliberately mutable and global, exactly like the web module's
 * exported `WIND`: a future weather mode drives the field by writing these and
 * needs no other seam into the simulation. (A test that writes them must restore
 * them in a `finally` — there is one field of weather for the whole app.)
 *
 *   [speed]    px/s. The drag-balanced TERMINAL lateral velocity of a sail = 1
 *              mote, NOT an acceleration — the wind term is pre-multiplied by
 *              [CINDER_DRAG] so the number means what it says.
 *   [tilt]     vertical share of the current (negative = a shallow upward rake).
 *   [gust]     gust peak as a fraction of [speed], added on top of it.
 *   [scatter]  extra curl gain at gust peak — the "briefly scatter them" part.
 */
object CinderWind {
    var speed = 210f
    var tilt = -0.03f
    var gust = 0.9f
    var scatter = 0.9f
}

// -----------------------------------------------------------------------------
// The force budget. Every one of these is the web module's number, unchanged.
// The `EMBER_` twins are the parent field's values, recorded here purely so the
// fork's whole point is testable (and diffable) rather than a claim in a comment.
// -----------------------------------------------------------------------------
/** Swirl gain. Embers: 5200 — an undulation ON the current, not the motion. */
internal const val CINDER_CURL = 1900f
internal const val EMBER_CURL = 5200f

/** Per-second velocity decay, unchanged from Embers. */
internal const val CINDER_DRAG = 0.9f

/** Dropped toward zero. Embers: 9 — a cinder has no heat left to climb on. */
internal const val CINDER_BUOYANCY = 0.9f
internal const val EMBER_BUOYANCY = 9f

/** Heavy sparks still settle a little. Embers: 60. */
internal const val CINDER_SPARK_SINK = 18f
internal const val EMBER_SPARK_SINK = 60f

/** Gusts are EVENTS, not a wobble: below this threshold the air is simply calm. */
private const val CINDER_GUST_ONSET = 0.55

/** Central-difference step for the curl potential, in web CSS px. */
private const val CINDER_EPS = 3f

/** Horizontal wrap margin, web CSS px. Nothing is ever spawned off-screen-left
 *  and marched across; motes simply reappear on the other edge. */
private const val CINDER_WRAP = 20f

/** Backstop inherited from EmberSim: a mote that floated off the top goes back
 *  to the free list. With buoyancy dropped to 0.9 the field has almost no lift,
 *  so this effectively only ever fires for a mote that spawned in the top few
 *  pixels — but it is what keeps a long stall from stranding one above the frame
 *  in a FIXED pool (the web list has no such slot to strand). */
private const val CINDER_TOP_CULL = 30f

/**
 * Per-particle envelope multiplier for the size-over-life curve. The top of the
 * range times the curve's peak (0.9385) is 0.9948 — under 1.0, which is what
 * guarantees a drawn radius never exceeds the spawn radius, and therefore that no
 * mote is ever a bigger blob over a glyph than the approved Embers field's.
 */
internal const val CINDER_SWELL_MIN = 0.78f
internal const val CINDER_SWELL_MAX = 1.06f

/** Share of the population that is still hot. */
internal const val CINDER_SPARK_ODDS = 0.14

/** How much of the current a mote catches. */
internal const val CINDER_SAIL_FLAKE = 0.85f
internal const val CINDER_SAIL_SPARK = 1.25f

/** How much harder a gust throws it. */
internal const val CINDER_GUST_GAIN_FLAKE = 0.7f
internal const val CINDER_GUST_GAIN_SPARK = 1.6f

/** Per-particle ALPHA CEILING — Embers', to the float. This is the legibility
 *  number: the brightest a single mote can ever be over a glyph. */
internal const val CINDER_ALPHA_SPARK = 0.85f
internal const val CINDER_ALPHA_FLAKE = 0.55f

/** Cheap bloom: one wide faint pass + one small bright core. Multipliers are
 *  Embers' (which draws 4.0/4.2 wide and 1.6/1.9 core) or smaller — never more. */
private const val CINDER_GLOW_SPARK = 3.5f
private const val CINDER_GLOW_FLAKE = 4.2f
private const val CINDER_CORE_SPARK = 1.5f
private const val CINDER_CORE_FLAKE = 1.9f
private const val CINDER_GLOW_ALPHA = 0.22f

/** Population, in the units EmberSim already uses: one mote per 900 dp² of
 *  screen, clamped to the Appendix-A Embers band, then scaled by the user's
 *  intensity × quality dial. Identical to Embers by design — the web asserts the
 *  two fields share a population target, and that is the budget this field is
 *  measured against. */
internal const val CINDER_MIN_POP = 200
internal const val CINDER_MAX_POP = 400
private const val CINDER_DP2_PER_MOTE = 900f

/** The web module's hard cap. Unreachable here (a fixed pool of ≤ 400 × the 3.0
 *  dial maximum is 1200) but kept so the ported ceiling is explicit. */
internal const val CINDER_HARD_CAP = 3000

/** Reference short edge for [cinderGeom], in dp. A box this size draws at 1.0 —
 *  it is the size the look was tuned at, so it stays 900 (web: `sizing.js`). */
private const val CINDER_REF_SHORT_EDGE_DP = 900f

/** Below this the field would read as sparse dust (web: `MIN_APPARENT`). */
private const val CINDER_GEOM_MIN = 0.5f

// -----------------------------------------------------------------------------
// Pure maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------

/**
 * GEOMETRY MULTIPLIER: a function of the dp BOX ONLY.
 *
 * The Android spelling of the web's `Math.min(1, apparentScale(w, h))`. Two
 * properties, both asserted:
 *   • it never sees `density` except to convert device px → dp, so it is
 *     INVARIANT under a density change at a fixed physical size — the same law
 *     the web states as "apparentScale never sees devicePixelRatio";
 *   • it is a CEILING at 1.0, so a large viewport can never spend more ink than
 *     the approved Embers field. It may only ever shrink the field.
 * A phone-shaped box (≈ 412 dp short edge) lands on the 0.5 floor, which is why
 * cinders sits well UNDER the Embers budget on a phone rather than merely at it.
 */
internal fun cinderGeom(widthDp: Float, heightDp: Float): Float =
    (min(widthDp, heightDp) / CINDER_REF_SHORT_EDGE_DP).coerceIn(CINDER_GEOM_MIN, 1f)

/**
 * Gust envelope in [0, 1] as a pure function of ABSOLUTE time — never of frame
 * count — so a gust lands at the same wall-clock moment at 60 Hz and 120 Hz, and
 * a headless test can ask for one directly.
 *
 * Two incommensurate sines so the cadence never reads as metronomic, thresholded
 * at [CINDER_GUST_ONSET] (most of the time the air is simply calm) and squared,
 * so the onset is a shove rather than a ramp.
 *
 * This is the ONE clock in the effect that is read off [nowMs] rather than
 * advanced by dt, and deliberately: the whole field is in the SAME AIR, so there
 * is no per-particle phase to teleport on resume — one global sample per frame is
 * the point, and wall-clock is what makes it frame-rate independent.
 *
 * @param ts seconds
 * @param phase this field's private weather offset
 */
internal fun cinderGustAt(ts: Double, phase: Float): Float {
    val raw = sin(ts * 0.41 + phase) * 0.62 + sin(ts * 0.17 + phase * 1.7) * 0.38
    if (raw <= CINDER_GUST_ONSET) return 0f
    val g = ((raw - CINDER_GUST_ONSET) / (1.0 - CINDER_GUST_ONSET)).toFloat()
    return g * g
}

/**
 * SIZE OVER LIFE. A cinder catches the air as it lights (bloom over the first
 * ~15% of life), rides at full size, then burns down — so a mote is never the
 * same size twice. Peak is 0.9385 at life 0.85 and, times [CINDER_SWELL_MAX],
 * the ceiling is under 1.0 BY CONSTRUCTION. That is the arithmetic the whole
 * legibility budget rests on.
 */
internal fun cinderSizeOverLife(life: Float): Float {
    val age = 1f - life
    val bloom = if (age < 0.15f) 0.62f + 0.38f * (age / 0.15f) else 1f
    return bloom * (0.59f + 0.41f * life)
}

/** Soft per-mote flicker. Off the wall clock, like EmberSim's — a brightness
 *  wobble has no phase to lose, and [fade] keeps the field from breathing as one. */
internal fun cinderBreathe(ts: Double, fade: Float): Float =
    (0.6 + 0.4 * sin(ts * 1.3 + fade)).toFloat()

/** THE ALPHA CEILING, as a function so the test can sweep it: a spark can never
 *  exceed [CINDER_ALPHA_SPARK], a flake [CINDER_ALPHA_FLAKE] — Embers' numbers. */
internal fun cinderAlpha(spark: Boolean, life: Float, breathe: Float): Float =
    ((if (spark) CINDER_ALPHA_SPARK else CINDER_ALPHA_FLAKE) *
        min(1f, life * 1.4f) * breathe).coerceIn(0f, 1f)

/** Blackbody ramp indexed by LIFE: white-hot at birth, deep ember red at
 *  burnout. Sparks start hotter and cool over a shorter stretch of the atlas;
 *  flakes start from their own [hue0] band so the field is not one temperature. */
internal fun cinderRampIndex(spark: Boolean, hue0: Int, life: Float, top: Int): Int =
    if (spark) ((1f - life) * 1.5f).roundToInt().coerceIn(0, top)
    else (hue0 + ((1f - life) * 3f).roundToInt()).coerceIn(0, top)

/** Cheap 2-octave sine curl-noise potential ψ → divergence-free swirl. Verbatim
 *  from EmberSim/embers.js, sampled in LOGICAL (web CSS px) space so the spatial
 *  frequencies hold across DPIs. */
private fun cinderPot(x: Float, y: Float, t: Double): Double =
    sin(x * 0.0065 + t * 0.22) * cos(y * 0.0065 - t * 0.16) +
        0.5 * sin(x * 0.013 - t * 0.31) * cos(y * 0.013 + t * 0.26)

// -----------------------------------------------------------------------------
// One mote. A POOLED slot: recycled in place, never reallocated, so the hot path
// allocates nothing at all. Unlike Firefly, species is NOT a property of the slot
// — a cinder that burns out is replaced by whatever the wind brings next.
//
// x/y are DEVICE px; vx/vy/r are WEB CSS px (see the DPI note in the header).
// -----------------------------------------------------------------------------
class Cinder {
    var x = 0f; var y = 0f
    var vx = 0f; var vy = 0f      // web CSS px/s
    var life = 0f                 // 1 → 0
    var decay = 0f                // 1 / lifetime-in-seconds
    var r = 0f                    // spawn radius, web CSS px
    var spark = false             // ── sub-population ──
    var sail = 0f                 // share of the current this mote catches
    var gain = 0f                 // how much harder a gust throws it
    var swell = 0f                // per-particle size-envelope multiplier
    var hue0 = 0                  // 0..2 varied warm heat (flakes)
    var fade = 0f                 // private flicker offset
    var alive = false
}

/**
 * CindersSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so CindersFieldTest can drive the
 * whole simulation from a seeded generator.
 */
class CindersSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    /** Draw-time geometry multiplier; a function of the dp box only. */
    private var geom = 1f
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<Cinder>()
    /** Dead-slot FREE LIST (EmberSim's, verbatim): pop on spawn, push on death,
     *  so spawning never linear-scans a nearly-full pool. */
    private var freeIdx = IntArray(0)
    private var freeCount = 0
    private var spawnAcc = 0f

    /** This field's own weather — the web's `st.gustPhase`. Drawn from [rand] at
     *  construction so two overlays never gust in unison; writable so a test can
     *  put a gust inside its window. */
    internal var gustPhase: Float = (rand.next() * 6.28).toFloat()

    /** Test/inspection view of the pool. Never called from the hot path. */
    val motes: List<Cinder> get() = pool.asList()

    private fun rnd(): Float = rand.next().toFloat()

    /**
     * Concurrent-mote cap from logical (dp) area, clamped to the Embers 200–400
     * band, then scaled by the user's intensity × quality dial (≥ 1, never empty)
     * and by the ported hard cap. Counts come from dp area, never from pixels —
     * the documented exception to "density reaches nothing but geometry".
     */
    private fun targetMax(density: Float): Int {
        val areaDp = (width / density) * (height / density)
        val base = (areaDp / CINDER_DP2_PER_MOTE).roundToInt().coerceIn(CINDER_MIN_POP, CINDER_MAX_POP)
        return ParticleTuning.scaleCount(base, countScale).coerceAtMost(CINDER_HARD_CAP)
    }

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val sameSize = width == this.width && height == this.height && pool.isNotEmpty()
        this.width = width; this.height = height; this.scale = scale
        // Population is viewport-independent (motes wrap and re-spawn); only the
        // pool size and the geometry multiplier track the box. This runs every
        // frame from drawWithCache, so geom is never stale — the web has to
        // re-check it inside draw() for the same reason.
        geom = cinderGeom(width / density, height / density)
        val want = targetMax(density)
        if (pool.size != want) {
            pool = Array(want) { Cinder() }
            freeIdx = IntArray(want)
            for (i in 0 until want) freeIdx[i] = i   // every slot dead → every slot free
            freeCount = want
            seedFull()                               // seed full → the wind is already blowing
            return
        }
        // No re-scatter on a no-op resize: a live field must never blink, and
        // every stored length is density-free so it survives a DPI change intact.
        if (sameSize) return
        // A shrunken viewport must not strand anyone. The pool is FIXED, so an
        // off-screen slot is a hole in the field until it burns out — the web's
        // growable list has no such slot to strand, which is why this guard is
        // Android-only. (x is also self-healing via the wrap, one frame later.)
        for (p in pool) {
            if (p.x > width) p.x = width
            if (p.y > height) p.y = height
        }
    }

    /**
     * (Re)seed one mote IN PLACE, from the free list. Spawned ANYWHERE on screen
     * and already travelling — the horizontal wrap is what keeps the field full
     * once everything is moving, so nothing is ever marched in from off-frame.
     */
    private fun spawnOne() {
        if (freeCount == 0) return                   // pool saturated
        val p = pool[freeIdx[--freeCount]]
        val spark = rand.next() < CINDER_SPARK_ODDS
        val ml = 2.6 + rand.next() * 3.6             // life range unchanged: Embers' drain curve
        p.x = rnd() * width
        p.y = rnd() * height
        p.vx = CinderWind.speed * (0.3f + rnd() * 0.5f)   // born travelling, not from rest
        p.vy = (rnd() - 0.5f) * 12f
        p.life = 1f
        p.decay = (1.0 / ml).toFloat()
        p.r = if (spark) 1.1f + rnd() * 1.5f else 2.9f + rnd() * 6.7f
        p.spark = spark
        p.sail = if (spark) CINDER_SAIL_SPARK else CINDER_SAIL_FLAKE
        p.gain = if (spark) CINDER_GUST_GAIN_SPARK else CINDER_GUST_GAIN_FLAKE
        p.swell = CINDER_SWELL_MIN + rnd() * (CINDER_SWELL_MAX - CINDER_SWELL_MIN)
        p.fade = rnd() * 6.28f
        p.hue0 = (rnd() * 3f).toInt()
        p.alive = true
    }

    private fun seedFull() { for (i in pool.indices) spawnOne() }

    /** Retire slot [i] and return it to the free list. Only ever called on a live
     *  slot (both call sites sit inside `if (!p.alive) continue`, and the first
     *  one `continue`s), so an index can never be pushed twice. */
    private fun kill(p: Cinder, i: Int) {
        p.alive = false
        freeIdx[freeCount++] = i
    }

    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        val ts = nowMs * 0.001
        val sc = scale
        if (active) {
            // Keep the pool full while generating; the free list caps it, so this
            // can only ever backfill what actually died (EmberSim's rate).
            spawnAcc += pool.size * 0.85f * dtSec
            while (spawnAcc >= 1f) { spawnOne(); spawnAcc -= 1f }
        }
        // ONE gust sample per FRAME, not per particle: the whole field is in the
        // same air. Everything below is per-second and multiplied by dtSec, so the
        // field travels the same distance at 60 Hz and 120 Hz.
        val g = cinderGustAt(ts, gustPhase)
        val curl = CINDER_CURL * (1f + CinderWind.scatter * g)
        val base = CinderWind.speed * CINDER_DRAG
        val gustMul = CinderWind.gust * g
        val tilt = CinderWind.tilt
        val wrap = CINDER_WRAP * sc
        val topCull = CINDER_TOP_CULL * sc
        for (i in pool.indices) {
            val p = pool[i]
            if (!p.alive) continue
            p.life -= p.decay * dtSec
            if (p.life <= 0f) { kill(p, i); continue }
            // curl-noise swirl — same field as Embers, lower gain, so it bends the
            // current into long shallow arcs instead of overwhelming it. Sampled
            // in LOGICAL space (÷ scale) so the eddies are the same apparent size
            // on every screen.
            val lx = p.x / sc
            val ly = p.y / sc
            val cvx = cinderPot(lx, ly + CINDER_EPS, ts) - cinderPot(lx, ly - CINDER_EPS, ts)
            val cvy = -(cinderPot(lx + CINDER_EPS, ly, ts) - cinderPot(lx - CINDER_EPS, ly, ts))
            p.vx += (cvx * curl * dtSec).toFloat()
            p.vy += (cvy * curl * dtSec).toFloat()
            // The current: pre-multiplied by drag, so the drag-balanced terminal
            // vx is exactly speed × sail × (1 + gust). Sparks carry BOTH the higher
            // sail and the higher gust gain, so a gust throws them across the frame
            // while the cinders merely lean into it.
            val s = base * p.sail * (1f + gustMul * p.gain)
            p.vx += s * dtSec
            p.vy += s * tilt * dtSec
            p.vy -= CINDER_BUOYANCY * p.life * dtSec   // buoyancy dropped toward zero
            if (p.spark) p.vy += CINDER_SPARK_SINK * dtSec
            p.vx *= (1f - CINDER_DRAG * dtSec); p.vy *= (1f - CINDER_DRAG * dtSec)
            p.x += p.vx * sc * dtSec; p.y += p.vy * sc * dtSec
            // Wrap horizontally so the field stays full across the whole width —
            // with a steady current this is the ONLY thing keeping it populated.
            if (p.x < -wrap) p.x = width + wrap
            else if (p.x > width + wrap) p.x = -wrap
            if (p.y < -topCull) kill(p, i)
        }
    }

    /** Fresh weather each turn. The web starts empty and lets the current fill the
     *  frame; a FIXED pool fills instantly instead (EmberSim's `seedFull`), which
     *  is the same end state one second earlier and never shows a bare screen.
     *  Slots go back through the free list — nothing is allocated. */
    override fun rearm() {
        spawnAcc = 0f
        seedFull()
    }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        val ramp = res.atlas.emberNative      // hoisted: one lookup per FRAME, not per mote
        val top = ramp.size - 1
        val ts = nowMs * 0.001
        val sc = scale
        val gm = geom
        for (p in pool) {
            if (!p.alive) continue
            val al = cinderAlpha(p.spark, p.life, cinderBreathe(ts, p.fade))
            if (al <= 0f) continue
            val spr = ramp[cinderRampIndex(p.spark, p.hue0, p.life, top)]
            // SIZE = spawn radius × life curve × this mote's own envelope depth,
            // then the geometry ceiling. Never constant, never above the spawn size.
            val r = p.r * sc * p.swell * cinderSizeOverLife(p.life) * gm
            // Faint big glow (cheap bloom) …
            drawSpriteF(spr, p.x, p.y, r * (if (p.spark) CINDER_GLOW_SPARK else CINDER_GLOW_FLAKE),
                al * CINDER_GLOW_ALPHA, paints)
            // … + bright core.
            drawSpriteF(spr, p.x, p.y, r * (if (p.spark) CINDER_CORE_SPARK else CINDER_CORE_FLAKE),
                al, paints)
        }
    }
}
