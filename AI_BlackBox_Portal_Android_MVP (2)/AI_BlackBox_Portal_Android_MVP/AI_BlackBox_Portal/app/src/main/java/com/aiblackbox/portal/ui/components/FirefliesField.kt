package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: FIREFLIES (id "fireflies") — the QUIETEST field in the catalogue, and
// the WORKED EXAMPLE for ADDING_AN_EFFECT.md.
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/fireflies.js
// (its physics tests: Portal/modules/fx/effects/fireflies.test.mjs).
//
// Fifteen to forty warm points wandering slowly through the dark, each breathing
// on its own private rhythm. This is a BACKDROP that sits behind chat text, not a
// screensaver: the brief is "never quite still, never busy", and the population
// bounds are the mechanism — a handful of small points means the average screen
// luminance barely moves, so body text keeps its contrast at the brightest frame
// the field can produce.
//
// A fork of the Rising Stars sim with three deliberate divergences:
//
//   1. NO upward bias. StarParticle carries a baseVy that is always negative; a
//      firefly's heading is free, and the field's mean vertical drift is ~0 by
//      construction.
//   2. Heading comes from a LOW-FREQUENCY FLOW FIELD, not a random walk. A random
//      walk re-rolls the direction every frame and reads as jitter no matter how
//      small the steps; sampling a smooth noise field and steering TOWARD it
//      curves the path, and a curved path is what reads as alive.
//   3. Brightness is a PRIVATE pulse, not a shared sine of the clock. THE RULE
//      THAT MAKES OR BREAKS THIS EFFECT: the pulses must be desynchronised in
//      BOTH period AND phase. Shared period + random phase re-converges into
//      visible beating; shared phase + random period flashes the whole field in
//      unison on the first cycle. Both are drawn per particle, from a continuous
//      range, so the population can never return to a common phase.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here is
// therefore authored at FIELD_REFERENCE_DENSITY and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment it is used, so the
// field has the same APPARENT size on a 2.0 tablet and a 3.5 phone. Density must
// never reach anything else — not a count, not an alpha, not a period. (This is
// the same law the web module states as "devicePixelRatio must NEVER reach
// particle geometry"; on the web the canvas is in CSS px so the fix is to ignore
// dpr, on Android the canvas is in device px so the fix is to multiply by scale.)
//
// dt. Velocities here are PIXELS PER SECOND and are multiplied by dtSec directly
// — no `dt * 60` normalizer. (Rising Stars is the other convention: its constants
// are px-per-60Hz-tick, so IT needs the normalizer. Pick one, say which in a
// comment, and never mix them inside one effect.)
// =============================================================================

import androidx.compose.ui.graphics.drawscope.DrawScope
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin

// -----------------------------------------------------------------------------
// Population. On a tablet the field may not become busy; on a phone it may not
// empty out. Everything between is the count dial's + the screen area's business.
// -----------------------------------------------------------------------------
internal const val FIREFLY_MIN_POP = 15
internal const val FIREFLY_MAX_POP = 40
private const val FIREFLY_BASE_POP = 24          // at a reference phone, dial 1.0

/** dp² of the phone the population was tuned on (≈ 412 × 915). Population grows
 *  with the SQUARE ROOT of area, so unfolding a Fold adds a few fireflies rather
 *  than 2.2× of them — density-of-light is what the legibility budget cares
 *  about, and that is per-area. */
private const val FIREFLY_REF_AREA_DP = 412f * 915f

/**
 * WEB CSS px → reference-density DEVICE px.
 *
 * The web module authors every length in CSS px and multiplies by
 * apparentScale(cssBox), which clamps to 0.5 on a phone-shaped viewport. A CSS px
 * is a dp, and FIELD_REFERENCE_DENSITY turns a dp into 3.1 device px:
 *     0.5 × 3.1 = 1.55
 * Keeping this factor explicit lets every KINDS number below stay BYTE-IDENTICAL
 * to fireflies.js, so the two can be diffed forever.
 */
private const val WEB_TO_REF_PX = 1.55f

private const val TAU = 6.2831855f

/** Soft turn-back band, in web CSS px. Fireflies steer away from the edges rather
 *  than wrapping — a point that teleports across the screen is the single most
 *  obvious "these are particles" tell, and a hard bounce is the second. The band
 *  has to be wider than the distance a firefly covers while it turns around. */
private const val FIREFLY_EDGE = 26f

/** How much harder a firefly veers inside the turn-back band. Steering alone at
 *  the base rate loses the race against its own speed. */
private const val FIREFLY_EDGE_URGENCY = 2.5f

/** Big-glow alpha as a fraction of the particle's own gain (cheap bloom: one wide
 *  faint pass + one small bright core — the embers idiom, already proven here). */
private const val FIREFLY_GLOW_ALPHA = 0.18f

/** Fraction of the lit part of a cycle spent rising. */
private const val FIREFLY_ATTACK = 0.28f

/**
 * THE TWO SUB-POPULATIONS.
 *
 *   lamp  — the ambient breathers: large, slow, long-lived, never fully dark (a
 *           floor of steady glow), a long lazy period and a wide duty cycle.
 *   spark — the signallers: small, quicker, shorter-lived, a hard blink with a
 *           real dark gap between flashes (floor ~0, narrow duty).
 *
 * They differ in every axis that is visible — size, speed, turn rate, lifetime,
 * pulse period, duty, floor, swell, colour band — because two populations that
 * differ only in size just look like one population at two distances.
 *
 * Lengths are WEB CSS px (see WEB_TO_REF_PX); times are seconds; the rest is
 * dimensionless. `hue0..hue1` index the shared 6-stop blackbody atlas.
 */
internal class FireflyKind(
    val rMin: Float, val rMax: Float,
    val speedMin: Float, val speedMax: Float,
    val turn: Float,
    val lifeMin: Float, val lifeMax: Float,
    val periodMin: Float, val periodMax: Float,
    val dutyMin: Float, val dutyMax: Float,
    val floorMin: Float, val floorMax: Float,
    val swellMin: Float, val swellMax: Float,
    val hue0: Float, val hue1: Float,
    val glow: Float, val core: Float, val gain: Float,
)

internal val FIREFLY_LAMP = FireflyKind(
    rMin = 2.2f, rMax = 3.9f, speedMin = 5f, speedMax = 13f, turn = 0.85f,
    lifeMin = 22f, lifeMax = 40f, periodMin = 2.6f, periodMax = 5.4f,
    dutyMin = 0.55f, dutyMax = 0.82f, floorMin = 0.18f, floorMax = 0.34f,
    swellMin = 0.18f, swellMax = 0.42f, hue0 = 1.35f, hue1 = 3.60f,
    glow = 4.5f, core = 1.45f, gain = 0.62f,
)

internal val FIREFLY_SPARK = FireflyKind(
    rMin = 1.3f, rMax = 2.3f, speedMin = 14f, speedMax = 30f, turn = 1.70f,
    lifeMin = 12f, lifeMax = 24f, periodMin = 1.1f, periodMax = 2.4f,
    dutyMin = 0.16f, dutyMax = 0.34f, floorMin = 0.00f, floorMax = 0.06f,
    swellMin = 0.30f, swellMax = 0.70f, hue0 = 0.55f, hue1 = 2.50f,
    glow = 3.4f, core = 1.15f, gain = 0.80f,
)

/** Every third slot is a spark. Deterministic rather than a coin flip so the mix
 *  is exactly 1:2 at ANY population — a 15-particle phone field still gets both
 *  sub-populations, which a per-particle rand() cannot promise. */
internal fun isFireflySpark(index: Int): Boolean = index % 3 == 1

// -----------------------------------------------------------------------------
// Pure envelope maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------
private fun smooth(t: Float): Float = t * t * (3f - 2f * t)     // smoothstep, no corners

/**
 * One flash of the private pulse. [u] is the particle's own phase in [0,1) and
 * [duty] the lit fraction of its cycle; the rest of the cycle is dark (or, for a
 * lamp, its floor). Fast attack / slow decay, smoothstepped at both ends so the
 * peak has no visible corner — a triangle wave reads as a blinking LED.
 */
internal fun fireflyPulse(u: Float, duty: Float): Float {
    if (duty <= 0f || u >= duty) return 0f
    val k = u / duty
    val t = if (k < FIREFLY_ATTACK) k / FIREFLY_ATTACK
    else 1f - (k - FIREFLY_ATTACK) / (1f - FIREFLY_ATTACK)
    return smooth(t)
}

/** SIZE over life: swells in over the first 12%, tapers to 55% over the last 25%.
 *  Multiplied per particle by (1 + swell × breath) at draw time, so no two
 *  fireflies pump by the same amount and none of them is a constant dot. */
internal fun fireflySizeOverLife(life: Float): Float =
    smooth(min(1f, (1f - life) * 8f)) * (0.55f + 0.45f * smooth(min(1f, life * 4f)))

/** ALPHA over life: fade in at birth, fade out at death, so a recycled slot never
 *  pops. Never negative — life is culled at ≤ 0 before this runs. */
internal fun fireflyLifeFade(life: Float): Float =
    min(1f, (1f - life) * 8f) * min(1f, life * 4f)

/**
 * Low-frequency flow field → a heading in radians. Two octaves of sine at
 * ~1/700 px and ~1/1100 px, drifting at ~1/20 Hz: slow enough in BOTH space and
 * time that neighbours share a lazy common current while each still curves its
 * own way. [k] is the particle's private offset into the field so that two
 * fireflies in the same place never fly in lockstep.
 *
 * [x] and [y] are in WEB CSS px (device px ÷ geometry scale), so the spatial
 * frequencies hold across DPIs — the same trick EmberSim's curl-noise plays.
 */
internal fun fireflyFlow(x: Float, y: Float, t: Double, k: Float): Float =
    ((sin(x * 0.0013 + t * 0.055 + k) +
        cos(y * 0.0017 - t * 0.041 + k * 1.7) +
        0.5 * sin((x - y) * 0.0009 + t * 0.029)) * 1.25).toFloat()

/** Shortest signed turn from angle [from] to angle [to], in (-PI, PI]. */
internal fun fireflyTurnTo(from: Float, to: Float): Float {
    var d = (to - from) % TAU
    if (d > Math.PI.toFloat()) d -= TAU else if (d < -Math.PI.toFloat()) d += TAU
    return d
}

/** 0 inside the soft band, ramping to 1 at (and past) the edge. */
private fun edgePush(x: Float, y: Float, w: Float, h: Float, m: Float): Float {
    val d = min(min(x, y), min(w - x, h - y))
    if (d >= m) return 0f
    return if (m > 0f) min(1f, 1f - d / m) else 1f
}

// -----------------------------------------------------------------------------
// One firefly. A POOLED slot: recycled in place, never reallocated, so the hot
// path allocates nothing at all. `spark` is a property of the slot, not of the
// draw — a firefly does not change species when it is recycled.
// -----------------------------------------------------------------------------
class Firefly internal constructor(val spark: Boolean) {
    var x = 0f; var y = 0f
    var h = 0f          // heading, radians
    var sp = 0f         // speed along that heading, web CSS px/s
    var r0 = 0f         // base radius, web CSS px
    var swell = 0f      // per-particle size-envelope depth
    var nk = 0f         // private offset into the flow field
    var decay = 0f      // 1 / lifetime-in-seconds
    var period = 1f     // pulse period, seconds  ── desync axis 1
    var t = 0f          // pulse phase, seconds   ── desync axis 2
    var duty = 0f       // lit fraction of the cycle
    var floor = 0f      // brightness that never goes out (lamps only)
    var life = 1f       // 1 → 0
    var dead = false
}

/**
 * FirefliesSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so FirefliesFieldTest can drive the
 * whole simulation from a seeded generator.
 */
class FirefliesSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<Firefly>()

    /** Test/inspection view of the pool. Never called from the hot path. */
    val flies: List<Firefly> get() = pool.asList()

    /** Web CSS px → device px for THIS screen. Applied at use, never baked into
     *  stored state, so a density or size change re-scales the live field. */
    private val g: Float get() = scale * WEB_TO_REF_PX

    private fun rnd(): Float = rand.next().toFloat()
    private fun pick(lo: Float, hi: Float): Float = lo + rnd() * (hi - lo)

    private fun targetPop(density: Float): Int {
        val areaDp = (width / density) * (height / density)
        val areaFactor = kotlin.math.sqrt((areaDp / FIREFLY_REF_AREA_DP).toDouble()).toFloat()
        val base = ParticleTuning.scaleCount(FIREFLY_BASE_POP, countScale * areaFactor)
        return base.coerceIn(FIREFLY_MIN_POP, FIREFLY_MAX_POP)
    }

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val sameSize = width == this.width && height == this.height && pool.isNotEmpty()
        this.width = width; this.height = height; this.scale = scale
        val want = targetPop(density)
        if (pool.size != want) {
            // Grow/trim IN PLACE: survivors keep their pulse, because a window
            // drag (or an unfold) is not a reason for the field to blink. Slot i
            // keeps its species — isFireflySpark is index-derived.
            val old = pool
            pool = Array(want) { i ->
                if (i < old.size) old[i]
                else Firefly(isFireflySpark(i)).also { recycle(it, initial = true) }
            }
        } else if (sameSize) {
            return
        }
        // A shrunken viewport must not leave anyone stranded off-screen.
        for (p in pool) {
            if (p.x > width) p.x = width
            if (p.y > height) p.y = height
        }
    }

    /**
     * (Re)seed one firefly IN PLACE.
     *
     * [initial] staggers the first fill so the population does not die (and
     * re-light) as a cohort — the same trick StarSim plays with y.
     */
    private fun recycle(p: Firefly, initial: Boolean) {
        val k = if (p.spark) FIREFLY_SPARK else FIREFLY_LAMP
        val m = FIREFLY_EDGE * 2.5f * g               // never born already fighting an edge
        p.x = m + rnd() * maxOf(1f, width - m * 2f)
        p.y = m + rnd() * maxOf(1f, height - m * 2f)
        p.h = rnd() * TAU                             // heading; NO upward bias
        p.sp = pick(k.speedMin, k.speedMax)
        p.r0 = pick(k.rMin, k.rMax)
        p.swell = pick(k.swellMin, k.swellMax)
        p.nk = rnd() * TAU                            // private offset into the flow field
        p.decay = 1f / pick(k.lifeMin, k.lifeMax)
        // ── desynchronisation: period AND phase, both continuous, both private ──
        p.period = pick(k.periodMin, k.periodMax)
        p.t = rnd() * p.period
        p.duty = pick(k.dutyMin, k.dutyMax)
        p.floor = pick(k.floorMin, k.floorMax)
        p.life = if (initial) 0.15f + rnd() * 0.85f else 1f
        p.dead = false
    }

    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        val ts = nowMs * 0.001
        val gg = g
        val band = FIREFLY_EDGE * 2.5f * gg
        val cx = width * 0.5f
        val cy = height * 0.5f
        for (p in pool) {
            if (p.dead) continue
            p.life -= p.decay * dtSec
            if (p.life <= 0f) {
                // While generating, a new firefly takes the slot; once idle the
                // field thins out instead of looping forever (StarSim semantics).
                if (active) recycle(p, initial = false) else { p.life = 0f; p.dead = true }
                continue
            }
            // Private pulse clock. Advanced by dt (NOT read off nowMs) so a parked
            // frame loop resumes the breath where it left it, rather than
            // teleporting the whole field to a new common phase.
            p.t = (p.t + dtSec) % p.period
            // Steer toward the flow field, with an inward bias near the edges.
            var target = fireflyFlow(p.x / gg, p.y / gg, ts, p.nk)
            val push = edgePush(p.x, p.y, width, height, band)
            if (push > 0f) target += fireflyTurnTo(target, atan2(cy - p.y, cx - p.x)) * push
            val k = if (p.spark) FIREFLY_SPARK else FIREFLY_LAMP
            // Clamped exponential approach — a rate per SECOND, so it is
            // delta-timed, and clamped so a long stall cannot overshoot the target
            // heading (the same guard StarSim puts on its opacity chase).
            p.h += fireflyTurnTo(p.h, target) *
                min(1f, k.turn * (1f + FIREFLY_EDGE_URGENCY * push) * dtSec)
            p.x += cos(p.h) * p.sp * gg * dtSec
            p.y += sin(p.h) * p.sp * gg * dtSec
            // Backstop: steering alone can lose a race against a viewport that
            // just shrank, so reflect off the wall rather than escape it.
            if (p.x < 0f) { p.x = 0f; p.h = Math.PI.toFloat() - p.h }
            else if (p.x > width) { p.x = width; p.h = Math.PI.toFloat() - p.h }
            if (p.y < 0f) { p.y = 0f; p.h = -p.h }
            else if (p.y > height) { p.y = height; p.h = -p.h }
        }
    }

    /** A new turn revives the slots that went dark while idle, WITHOUT touching
     *  the survivors — and every revived slot draws a fresh period and phase, so a
     *  rearm can never hand the field a common rhythm. */
    override fun rearm() {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        for (p in pool) if (p.dead) recycle(p, initial = false)
    }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        val atlas = res.atlas                 // hoisted: one lookup per FRAME, not per particle
        val ramp = atlas.emberNative
        val star = atlas.starNative
        val top = ramp.size - 1
        val gg = g
        for (p in pool) {
            if (p.dead) continue
            val k = if (p.spark) FIREFLY_SPARK else FIREFLY_LAMP
            // The breath: floor + its own flash, on its own period and phase.
            val e = p.floor + (1f - p.floor) * fireflyPulse(p.t / p.period, p.duty)
            // SIZE = life curve × per-particle envelope depth. Never constant.
            val r = p.r0 * gg * fireflySizeOverLife(p.life) * (1f + p.swell * e)
            val al = k.gain * e * fireflyLifeFade(p.life)
            if (al <= 0f) continue            // a spark in its dark gap costs nothing
            // COLOUR RAMP indexed by life: young fireflies sit at the pale-gold end
            // of the shared blackbody atlas and cool toward deep amber as they age,
            // each sub-population over its own band. Cross-faded between adjacent
            // stops so the ramp is continuous rather than six visible steps.
            val idx = (k.hue0 + (k.hue1 - k.hue0) * (1f - p.life)).coerceIn(0f, top.toFloat())
            val i0 = idx.toInt()
            val f = idx - i0
            val gr = r * k.glow
            val ga = al * FIREFLY_GLOW_ALPHA
            drawSpriteF(ramp[i0], p.x, p.y, gr, ga * (1f - f), paints)
            if (f > 0f) drawSpriteF(ramp[min(top, i0 + 1)], p.x, p.y, gr, ga * f, paints)
            // Hot core, weighted toward the top of the flash: the glow breathes,
            // the core blinks. Without this a flash just reads as a slow fade.
            drawSpriteF(star, p.x, p.y, r * k.core, al * (0.30f + 0.70f * e * e), paints)
        }
    }
}
