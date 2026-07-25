package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: METEOR FALL (id "meteors") — the SPARSEST field in the catalogue.
//
// A faithful native port of the web module
//   Portal/modules/fx/effects/meteors.js
// (its physics tests: Portal/modules/fx/effects/meteors.test.mjs).
//
// A quiet two-layer starfield that never stops, and — every 13 to 27 seconds —
// ONE hairline streak that rips diagonally across a corner and is gone before you
// are sure you saw it. The meteor pool is 8 and almost always ZERO of them are
// alive. The effect is the WAIT; the streak is the punctuation.
//
// Registration line the catalogue needs (FieldRegistry.kt, appended, never moved):
//     FieldEffect("meteors", "Meteor Fall") { n, r -> MeteorsSim(n, r) },
//
// ── Three things here are load-bearing and look like omissions if you skim ────
//
//   1. THE SCHEDULE READS NOTHING. Spawn timing comes from a private, dt-driven
//      clock ([clockSec]) plus [rand] — never `active`, never `nowMs`, never any
//      other engine signal. A streak that fired when generation started would be
//      read as a status indicator, and once a user believes a backdrop MEANS
//      something they watch it instead of the chat. [rearm] therefore leaves the
//      schedule ALONE; it is the one hook that correlates with activity. (This is
//      the one place this effect deliberately declines the FieldSim contract's
//      "stop respawning while idle" convention: visibility is EmberOverlay's job
//      — it parks the loop — and the schedule must stay uncorrelated.)
//
//   2. THE EDGE BUDGET IS SOLVED AT SPAWN, NOT CLAMPED AT DRAW. Every streak is
//      aimed OUTWARD from its corner, and the spawn x is capped so that the head
//      (which only ever moves further out) AND the tail reaching back inward both
//      start inside the outer [METEOR_EDGE_LIMIT] band. So no stroked pixel can
//      enter the centre 40% of the width where chat text is read — no per-frame
//      test, no clipping, and it is provable from the spawn parameters alone.
//
//   3. THE STREAK GRADIENT IS CACHED, NEVER BUILT. The look is a linear gradient
//      from the head back to `head - velocity × k`. A gradient bakes its
//      endpoints, so a naive moving one is a new Shader every frame per meteor —
//      the #1 perf trap in this engine. Here every gradient the effect can ever
//      need (2 kinds × [METEOR_RAMP_STEPS] life positions) is baked ONCE through
//      FieldResources.bake, and a reusable local Matrix re-aims the cached shader
//      down each streak. Zero allocation in the hot path.
//      (The web module solves the same problem the other way — CanvasGradient has
//      no local matrix, so it quantizes rgba() strings and strokes 9 segments.
//      Both produce the same picture; the segment count is kept identical so the
//      taper reads the same.)
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here is
// authored at FIELD_REFERENCE_DENSITY and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment it is used, so the
// field has the same APPARENT size on a 2.0 tablet and a 3.5 phone. Density may
// reach NOTHING else — not a count, not an alpha, not a silence, not a lifetime.
// (Same law the web module states as "devicePixelRatio must NEVER reach particle
// geometry": there the canvas is CSS px so the fix is to ignore dpr; here the
// canvas is device px so the fix is to multiply by scale.)
//
// dt. Velocities here are PIXELS PER SECOND and are multiplied by dtSec directly —
// no `dt * 60` normalizer. The schedule clock is advanced BY dtSec too, never read
// off nowMs, so a parked frame loop resumes its silence where it left it instead
// of teleporting to a new common phase (and a 120 Hz panel sees exactly as many
// meteors as a 60 Hz one).
// =============================================================================

import android.graphics.LinearGradient
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.Shader
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * WEB CSS px → reference-density DEVICE px.
 *
 * The web module authors every length in CSS px and multiplies by
 * apparentScale(cssBox), which clamps to 0.5 on a phone-shaped viewport. A CSS px
 * is a dp, and FIELD_REFERENCE_DENSITY turns a dp into 3.1 device px:
 *     0.5 × 3.1 = 1.55
 * Keeping this factor explicit lets every tuning number below stay BYTE-IDENTICAL
 * to meteors.js, so the two can be diffed forever.
 */
private const val WEB_TO_REF_PX = 1.55f

private const val TAU = 6.2831855f

// -----------------------------------------------------------------------------
// Population + schedule. THE LEGIBILITY BUDGET LIVES IN THESE NUMBERS — they were
// measured against real WCAG contrast on the web side. The pool is a hard ceiling
// and is deliberately NOT scaled by the count dial: a meteor field that got busy
// at Ultra would stop being a meteor field.
// -----------------------------------------------------------------------------
internal const val METEOR_POOL = 8

/** Seconds of quiet before the FIRST streak of a session. */
internal const val METEOR_FIRST_MIN = 9f
internal const val METEOR_FIRST_MAX = 19f

/** …and between every event after that (measured mean gap ≈ 19 s). */
internal const val METEOR_SILENCE_MIN = 16f
internal const val METEOR_SILENCE_MAX = 34f

/** Occasionally 2-3 arrive in quick succession, then a long lull. */
internal const val METEOR_SHOWER_CHANCE = 0.12f
internal const val METEOR_SHOWER_EXTRA = 2

/** Spacing WITHIN a shower. */
internal const val METEOR_BURST_GAP_MIN = 0.30f
internal const val METEOR_BURST_GAP_MAX = 1.35f

/** Radians off vertical (25°-53°) — diagonal, never a rain. */
internal const val METEOR_ANGLE_MIN = 0.44f
internal const val METEOR_ANGLE_MAX = 0.92f

/** No stroked pixel crosses this fraction of the width, in from either edge. */
internal const val METEOR_EDGE_LIMIT = 0.28f

/** The centre 40% (0.30..0.70 on BOTH axes) is for reading. */
internal const val METEOR_CENTRE_BOX = 0.30f

/** Star alpha multiplier inside that box. 0.40 × 0.42 = 0.168 — the ceiling the
 *  web measured contrast against, preserved exactly. */
internal const val METEOR_CENTRE_DIM = 0.42f

/** Streak lifetime, seconds, clamped. BRIEF is the whole point. */
internal const val METEOR_LIFE_MIN = 0.35f
internal const val METEOR_LIFE_MAX = 1.60f

/** How far along the colour ramp a streak drifts as it ages. */
internal const val METEOR_AGE_SHIFT = 0.22f

/** Tail resolution: the number of tapering sub-strokes. */
internal const val METEOR_SEGMENTS = 9

internal const val METEOR_FIREBALL_CHANCE = 0.26f

/** Starfield: a LOW, viewport-independent count (it is the sky, not a field),
 *  scaled only by the intensity × quality dial and clamped hard at both ends. */
internal const val METEOR_STAR_COUNT = 44
internal const val METEOR_STAR_FAR_FRACTION = 0.68f
internal const val METEOR_STAR_MIN = 18
internal const val METEOR_STAR_MAX = 90

/**
 * THE TWO METEOR SUB-POPULATIONS.
 *
 *   dust     — hairline: fastest, thinnest, blue-white, a blink.
 *   fireball — slower, thicker, warmer, lingers one beat longer. The rare one.
 *
 * They differ in every axis that is visible — speed, tail ratio, width, alpha,
 * where they START on the colour ramp, how far along it they travel, and how much
 * bloom they carry — because two populations that differ only in size just look
 * like one population at two distances.
 *
 * Speeds/widths are WEB CSS px (see [WEB_TO_REF_PX]); `tailK` is in seconds
 * (tail = speed × k, so the tail LENGTH ENCODES SPEED); the rest is dimensionless.
 */
internal class MeteorKind(
    val speedMin: Float, val speedMax: Float,
    val tailKMin: Float, val tailKMax: Float,
    val widthMin: Float, val widthMax: Float,
    val alphaMin: Float, val alphaMax: Float,
    val ramp0Min: Float, val ramp0Max: Float,
    val span: Float,
    val glow: Float,
)

internal val METEOR_DUST = MeteorKind(
    speedMin = 900f, speedMax = 1420f, tailKMin = 0.090f, tailKMax = 0.135f,
    widthMin = 0.85f, widthMax = 1.40f, alphaMin = 0.42f, alphaMax = 0.60f,
    ramp0Min = 0.02f, ramp0Max = 0.12f, span = 0.72f, glow = 4.6f,
)

internal val METEOR_FIREBALL = MeteorKind(
    speedMin = 620f, speedMax = 950f, tailKMin = 0.150f, tailKMax = 0.200f,
    widthMin = 1.50f, widthMax = 2.50f, alphaMin = 0.62f, alphaMax = 0.80f,
    ramp0Min = 0.18f, ramp0Max = 0.28f, span = 0.62f, glow = 6.2f,
)

/**
 * THE TWO STAR SUB-POPULATIONS.
 *
 *   far  — dust: barely there, drifts imperceptibly, slow twinkle.
 *   near — brighter, faster drift, quicker twinkle.
 *
 * Radii/drift are WEB CSS px (× g at use); `tw` is the twinkle rate in rad/s.
 */
internal class MeteorStarLayer(
    val rMin: Float, val rMax: Float,
    val aMin: Float, val aMax: Float,
    val drift: Float,
    val twMin: Float, val twMax: Float,
)

internal val METEOR_STAR_FAR = MeteorStarLayer(
    rMin = 0.35f, rMax = 0.95f, aMin = 0.10f, aMax = 0.22f,
    drift = 1.6f, twMin = 0.35f, twMax = 0.85f,
)

internal val METEOR_STAR_NEAR = MeteorStarLayer(
    rMin = 0.95f, rMax = 1.75f, aMin = 0.20f, aMax = 0.40f,
    drift = 3.2f, twMin = 0.70f, twMax = 1.60f,
)

// -----------------------------------------------------------------------------
// Colour. Both ramps are PURE DATA (packed 0xRRGGBB ints, no android.graphics),
// so touching anything in this file from a JVM test never loads a graphics stub.
// -----------------------------------------------------------------------------
/** white-hot → ice blue → pale gold → amber → deep ember. Indexed by LIFE. */
internal val METEOR_RAMP = arrayOf(
    intArrayOf(255, 255, 255),
    intArrayOf(214, 236, 255),
    intArrayOf(255, 236, 190),
    intArrayOf(255, 168, 86),
    intArrayOf(188, 58, 20),
)

/** cool blue-white → warm white. Indexed by each star's own tone + twinkle. */
internal val METEOR_STAR_RAMP = arrayOf(
    intArrayOf(176, 206, 255),
    intArrayOf(226, 236, 255),
    intArrayOf(255, 250, 236),
    intArrayOf(255, 232, 196),
)

/** Ramp quantization — the web's RAMP_STEPS, kept so the two look identical. */
internal const val METEOR_RAMP_STEPS = 20

/** Colour stops per baked tail gradient. */
private const val METEOR_TAIL_STOPS = 10

/** Sample [ramp] at 0..1 and pack it as 0xRRGGBB. Allocation-free. */
internal fun meteorRampRgb(ramp: Array<IntArray>, t: Float): Int {
    val f = t.coerceIn(0f, 1f) * (ramp.size - 1)
    val i = min(ramp.size - 2, f.toInt())
    val k = f - i
    val a = ramp[i]
    val b = ramp[i + 1]
    val r = (a[0] + (b[0] - a[0]) * k).roundToInt()
    val g = (a[1] + (b[1] - a[1]) * k).roundToInt()
    val bl = (a[2] + (b[2] - a[2]) * k).roundToInt()
    return (r shl 16) or (g shl 8) or bl
}

/** Quantize a 0..1 ramp position onto the [METEOR_RAMP_STEPS] LUT (web styleAt). */
internal fun meteorRampIndex(t: Float): Int = when {
    t <= 0f -> 0
    t >= 1f -> METEOR_RAMP_STEPS - 1
    else -> (t * (METEOR_RAMP_STEPS - 1) + 0.5f).toInt()
}

/** Pre-quantized ramps: built ONCE at class-init, pure ints, no Android types. */
private val METEOR_HEAD_RGB =
    IntArray(METEOR_RAMP_STEPS) { meteorRampRgb(METEOR_RAMP, it / (METEOR_RAMP_STEPS - 1f)) }
private val METEOR_STAR_RGB =
    IntArray(METEOR_RAMP_STEPS) { meteorRampRgb(METEOR_STAR_RAMP, it / (METEOR_RAMP_STEPS - 1f)) }

/** Per-segment width taper, pre-computed so the draw loop never calls pow(). */
private val METEOR_SEG_TAPER =
    FloatArray(METEOR_SEGMENTS) { (1f - (it + 1f) / METEOR_SEGMENTS).pow(1.25f) }

private fun meteorArgb(alpha: Float, rgb: Int): Int =
    ((alpha.coerceIn(0f, 1f) * 255f).roundToInt() shl 24) or rgb

// -----------------------------------------------------------------------------
// Pure envelope maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------
private const val METEOR_ATTACK = 0.14f

/**
 * SIZE OVER LIFE, shared by width, head radius and alpha. Smoothstep attack so a
 * streak fades IN over its first few pixels instead of popping into existence,
 * then a long power taper so it thins to nothing rather than being culled
 * mid-stroke. Multiplied per particle by its own `envMul`, so no two streaks share
 * an envelope and none of them is a constant bar of light.
 *
 * @param t age: 0 at birth → 1 at death.
 */
internal fun meteorEnvelope(t: Float): Float {
    if (t <= 0f || t >= 1f) return 0f
    val a = if (t < METEOR_ATTACK) t / METEOR_ATTACK else 1f
    return a * a * (3f - 2f * a) * (1f - t).pow(1.35f)
}

// -----------------------------------------------------------------------------
// The pooled slots. Both are recycled in place, never reallocated, so the hot
// path allocates nothing at all. Fields are public for the JVM physics test.
// -----------------------------------------------------------------------------

/**
 * One streak. `speed`, `tail` and `width` are DEVICE px: unlike the starfield,
 * an in-flight meteor is retired by [MeteorsSim.resize], so a baked geometry scale
 * can never go stale — and its edge budget was solved against the old width, which
 * is exactly why it must be retired.
 */
class Meteor internal constructor() {
    var alive = false
    var kind = 0            // 0 dust · 1 fireball
    var x = 0f
    var y = 0f
    var ux = 0f             // unit heading, OUTWARD: |x − W/2| only ever grows
    var uy = 0f
    var speed = 0f          // device px/s
    var tail = 0f           // device px, = speed × tailK (capped by the edge budget)
    var life = 1f           // 1 → 0
    var decay = 1f          // 1 / lifetime-in-seconds
    var envMul = 1f         // per-particle envelope depth
    var width = 0f          // device px, head half-width at envelope 1
    var alpha = 0f
    var ramp0 = 0f          // where on the colour ramp this streak is BORN
    var span = 0.7f         // …and how far along it the tail travels
    var glow = 5f
}

/**
 * One star. Geometry is stored in WEB px and multiplied by `g` at use (the
 * fireflies idiom) so a density change re-scales the live sky instead of
 * stranding it — stars SURVIVE a resize, meteors do not.
 */
class MeteorStar internal constructor(val far: Boolean) {
    var x = 0f              // device px
    var y = 0f
    var r = 1f              // web px
    var a = 0.2f
    var vx = 0f             // web px/s
    var tw = 0f             // twinkle phase  ── desync axis 1
    var tws = 1f            // twinkle rate   ── desync axis 2
    var envMul = 1f         // per-star size envelope depth
    var tone = 0.5f         // where this star sits on the colour ramp
    var k = 0.8f            // current twinkle, 0..1 (written by update, read by draw)
}

/**
 * MeteorsSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning) and
 * reaches the STARFIELD only — the meteor pool is a fixed legibility cap.
 * [rand] is the injectable randomness seam, so MeteorsFieldTest can drive the
 * whole simulation (including the schedule) from a seeded generator.
 */
class MeteorsSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<Meteor>()
    private var stars = emptyArray<MeteorStar>()

    /** THE schedule. A private, dt-driven clock in seconds — deliberately NOT
     *  `nowMs`, and deliberately correlated with no engine signal whatsoever.
     *  Double because a Float would accumulate enough error over a minute to move
     *  a spawn by a frame between 60 Hz and 120 Hz. */
    private var clock = 0.0
    private var nextAt = 0.0
    private var burst = 0

    /** Streaks fired since init. Test/inspection only. */
    var spawned = 0
        private set

    /** Test/inspection views. Never called from the hot path. */
    val meteors: List<Meteor> get() = pool.asList()
    val sky: List<MeteorStar> get() = stars.asList()
    val clockSec: Double get() = clock
    val nextAtSec: Double get() = nextAt

    /** Web CSS px → device px for THIS screen. Applied at use, never baked into
     *  surviving state, so a density or size change re-scales the live sky. */
    private val g: Float get() = scale * WEB_TO_REF_PX

    private fun rnd(): Float = rand.next().toFloat()
    private fun pick(lo: Float, hi: Float): Float = lo + rnd() * (hi - lo)

    // -------------------------------------------------------------------------
    // Lifecycle
    // -------------------------------------------------------------------------

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        if (pool.isEmpty()) { initField(width, height, scale); return }
        // The Canvas calls this every frame: a no-op resize must never disturb a
        // live field (and must never re-roll the schedule).
        if (width == this.width && height == this.height && scale == this.scale) return
        val sx = if (this.width > 0f) width / this.width else 1f
        val sy = if (this.height > 0f) height / this.height else 1f
        this.width = width
        this.height = height
        this.scale = scale
        // The sky is rescaled into the new box — it does not blink out on a rotate.
        for (s in stars) { s.x *= sx; s.y *= sy }
        // In-flight streaks are RETIRED: their edge budget was solved against the
        // OLD width, and a resize is rare enough that losing a sub-second streak
        // is free. The SCHEDULE is untouched — a resize is not an event.
        for (p in pool) p.alive = false
    }

    /** First valid size: build both pools and draw the opening silence. */
    private fun initField(w: Float, h: Float, sc: Float) {
        width = w; height = h; scale = sc
        clock = 0.0; burst = 0; spawned = 0
        pool = Array(METEOR_POOL) { Meteor() }
        // Star COUNT is viewport-independent by design (it is a sky, not a field);
        // only the intensity × quality dial moves it, and it is clamped at both
        // ends so a corrupt dial can neither empty nor crowd the reading area.
        val want = ParticleTuning.scaleCount(METEOR_STAR_COUNT, countScale)
            .coerceIn(METEOR_STAR_MIN, METEOR_STAR_MAX)
        val split = (want * METEOR_STAR_FAR_FRACTION).roundToInt()
        // Layer is a property of the SLOT (index-derived, not a coin flip), so both
        // sub-populations exist at any count — the fireflies idiom.
        stars = Array(want) { i -> MeteorStar(i < split).also { resetStar(it) } }
        nextAt = pick(METEOR_FIRST_MIN, METEOR_FIRST_MAX).toDouble()
    }

    private fun resetStar(s: MeteorStar) {
        val layer = if (s.far) METEOR_STAR_FAR else METEOR_STAR_NEAR
        s.x = rnd() * width
        s.y = rnd() * height
        s.r = pick(layer.rMin, layer.rMax)               // web px
        s.a = pick(layer.aMin, layer.aMax)
        s.vx = (rnd() - 0.5f) * 2f * layer.drift         // web px/s
        s.tw = rnd() * TAU                               // phase …
        s.tws = pick(layer.twMin, layer.twMax)           // … AND rate: never in unison
        s.envMul = 0.85f + rnd() * 0.30f
        s.tone = rnd()
        s.k = 0.8f
    }

    /**
     * Activation must NOT reach the schedule: firing a streak here would make the
     * backdrop a status indicator, which is the one thing it may never be. The
     * only thing left to do is build the field if it has never had a size.
     */
    override fun rearm() {
        if (pool.isEmpty() && width > 0f && height > 0f) initField(width, height, scale)
    }

    // -------------------------------------------------------------------------
    // Schedule + spawn
    // -------------------------------------------------------------------------

    /** Draw the next wait. Long silences, occasionally broken by a tight shower. */
    private fun scheduleNext() {
        val gap = when {
            burst > 0 -> { burst--; pick(METEOR_BURST_GAP_MIN, METEOR_BURST_GAP_MAX) }
            rnd() < METEOR_SHOWER_CHANCE -> {
                burst = 1 + (rnd() * METEOR_SHOWER_EXTRA).toInt()
                pick(METEOR_BURST_GAP_MIN, METEOR_BURST_GAP_MAX)
            }
            else -> pick(METEOR_SILENCE_MIN, METEOR_SILENCE_MAX)
        }
        // gap is always ≥ 0.30 s → the while() in update() always terminates.
        nextAt = clock + gap
    }

    /**
     * Aim one streak. EVERYTHING THE LEGIBILITY BUDGET DEPENDS ON IS DECIDED HERE:
     * the corner, the outward heading, and the x cap that keeps head AND tail
     * inside the outer band for the whole flight. Nothing is clamped at draw time.
     */
    private fun spawnMeteor() {
        val w = width
        val h = height
        if (w <= 0f || h <= 0f) return
        var p: Meteor? = null
        for (m in pool) if (!m.alive) { p = m; break }
        if (p == null) return                                  // saturated: skip, never grow the pool

        val kind = if (rnd() < METEOR_FIREBALL_CHANCE) 1 else 0
        val k = if (kind == 1) METEOR_FIREBALL else METEOR_DUST
        val side = if (rnd() < 0.5f) -1f else 1f               // which corner
        val ang = pick(METEOR_ANGLE_MIN, METEOR_ANGLE_MAX)
        val sinA = sin(ang)
        val cosA = cos(ang)
        val gg = g
        val speed = pick(k.speedMin, k.speedMax) * gg

        // Tail length ENCODES SPEED (v × k), then is capped twice: by the viewport,
        // and by how far it may reach back toward the centre before it violates the
        // budget.
        val room = (METEOR_EDGE_LIMIT - 0.015f) * w / max(0.25f, sinA)
        val tail = min(min(speed * pick(k.tailKMin, k.tailKMax), 0.34f * min(w, h)), room)

        // Head x is capped so head + (tail reaching inward) stays in the outer band.
        val xMin = 0.015f * w
        val xMax = METEOR_EDGE_LIMIT * w - sinA * tail
        val x0 = xMin + rnd() * max(0f, xMax - xMin)

        p.x = if (side < 0f) x0 else w - x0
        p.y = (-0.10f + rnd() * 0.30f) * h                     // top edge band
        p.ux = side * sinA                                     // OUTWARD
        p.uy = cosA
        p.speed = speed
        p.tail = tail
        p.kind = kind

        // Die just BEFORE leaving frame so the envelope closes instead of being
        // cut off mid-stroke.
        val tExitX = (x0 + tail) / (speed * sinA)
        val tExitY = (h + tail - p.y) / (speed * cosA)
        val span = min(tExitX, tExitY) * (0.85f + rnd() * 0.20f)
        p.life = 1f
        p.decay = 1f / max(METEOR_LIFE_MIN, min(METEOR_LIFE_MAX, span))
        p.envMul = 0.82f + rnd() * 0.42f                       // per-particle envelope
        p.width = pick(k.widthMin, k.widthMax) * gg
        p.alpha = pick(k.alphaMin, k.alphaMax)
        p.ramp0 = pick(k.ramp0Min, k.ramp0Max)
        p.span = k.span
        p.glow = k.glow
        p.alive = true
        spawned++
    }

    // -------------------------------------------------------------------------
    // Frame
    // -------------------------------------------------------------------------

    /**
     * [nowMs] is deliberately unread (see the header): the schedule and the
     * twinkle both run off the private dt clock. [active] is deliberately unread
     * too — a streak that correlated with generation would read as a status light.
     */
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        clock += dtSec
        // while(), not if(): one long stall must not swallow several events.
        while (clock >= nextAt) { spawnMeteor(); scheduleNext() }

        val gg = g
        val cull = 8f * gg
        for (p in pool) {
            if (!p.alive) continue
            p.life -= p.decay * dtSec
            p.x += p.ux * p.speed * dtSec
            p.y += p.uy * p.speed * dtSec
            val m = p.tail + cull                              // the TAIL must clear too
            if (p.life <= 0f || p.y - m > height || p.x + m < 0f || p.x - m > width) {
                p.alive = false
                p.life = 0f
            }
        }

        val margin = 2f * gg                                   // hoisted: once per frame
        val span = width + margin * 2f
        for (s in stars) {
            s.x += s.vx * gg * dtSec
            // Wrap, not respawn: the sky is continuous. The margin is a LENGTH, so
            // it scales with density like everything else spatial.
            if (s.x < -margin) s.x += span else if (s.x > width + margin) s.x -= span
            // Twinkle: private phase AND private rate, off the dt clock.
            s.k = 0.5f + 0.5f * sin((clock * s.tws + s.tw).toFloat())
        }
    }

    // -------------------------------------------------------------------------
    // Draw
    // -------------------------------------------------------------------------

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (width <= 0f || height <= 0f || stars.isEmpty()) return
        // Hoisted: ONE lookup per FRAME, never per particle. MeteorGfx bakes the
        // 40 tail gradients + this effect's two paints on first draw, and lives for
        // the whole (density, effect) life — nothing here allocates again.
        val gfx = res.bake(METEOR_GFX_KEY) { MeteorGfx() }
        val halo = res.atlas.starNative                        // shared white-hot blob
        val nc = drawContext.canvas.nativeCanvas
        val fill = gfx.fill
        val stroke = gfx.stroke
        val gg = g
        // The reading box, recomputed once per frame (never per particle).
        val bx0 = METEOR_CENTRE_BOX * width
        val bx1 = width - bx0
        val by0 = METEOR_CENTRE_BOX * height
        val by1 = height - by0

        // ── starfield: soft halo pass, then the crisp cores. Two passes, as on the
        // web: under the SrcOver fallback (< API 28) the halos must go underneath.
        for (s in stars) {
            val dim = if (s.x > bx0 && s.x < bx1 && s.y > by0 && s.y < by1) METEOR_CENTRE_DIM else 1f
            val rad = s.r * gg * (0.82f + 0.30f * s.k) * s.envMul   // size over twinkle
            drawSpriteF(halo, s.x, s.y, rad * 4.5f, s.a * (0.55f + 0.45f * s.k) * dim * 0.35f, paints)
        }
        for (s in stars) {
            val dim = if (s.x > bx0 && s.x < bx1 && s.y > by0 && s.y < by1) METEOR_CENTRE_DIM else 1f
            val rad = s.r * gg * (0.82f + 0.30f * s.k) * s.envMul
            val al = s.a * (0.55f + 0.45f * s.k) * dim
            if (al <= 0.003f) continue
            // COLOUR RAMP indexed by the star's own tone AND its current twinkle:
            // a star cools slightly as it dims, which is what stops the sky reading
            // as one flat blue-white dust.
            fill.color = meteorArgb(al, METEOR_STAR_RGB[meteorRampIndex(s.tone * 0.8f + 0.2f * (1f - s.k))])
            // 0.35 px is a RESOLUTION floor (device px, like drawSpriteF's own), not
            // a geometry length — it must not scale, or a dense screen grows dots.
            nc.drawCircle(s.x, s.y, max(0.35f, rad), fill)
        }

        // ── meteors: bloom, then the tapering tail, then the head core ──
        for (p in pool) {
            if (!p.alive) continue
            val t = 1f - p.life
            val e = meteorEnvelope(t) * p.envMul               // SIZE over life × per-particle depth
            if (e <= 0f) continue
            val hw = p.width * e
            val al = p.alpha * e
            if (al <= 0.003f) continue
            // COLOUR RAMP indexed by life: a streak is born white-hot at `ramp0`
            // and drifts toward amber as it ages, each kind over its own band.
            val headIdx = meteorRampIndex(p.ramp0 + METEOR_AGE_SHIFT * t)
            val tx = p.ux * p.tail                             // head → head − velocity×k
            val ty = p.uy * p.tail
            // Cheap bloom (one wide faint pass), the embers/fireflies idiom. This is
            // a RADIAL falloff at 30% of an already-low alpha, so its visible extent
            // is a fraction of its radius — the same latitude the web takes with it.
            drawSpriteF(halo, p.x, p.y, hw * p.glow, al * 0.30f, paints)
            if (p.tail > 0.5f) {
                // Re-aim the CACHED gradient down this streak, then stroke the tail
                // as segments whose width tapers to a point. Colour and alpha come
                // from the shader, so the taper is continuous instead of nine steps.
                gfx.aimTail(p.kind, headIdx, p.x, p.y, -tx, -ty)
                stroke.alpha = (al.coerceIn(0f, 1f) * 255f).roundToInt()
                for (j in 0 until METEOR_SEGMENTS) {
                    val a = j / METEOR_SEGMENTS.toFloat()
                    val b = (j + 1f) / METEOR_SEGMENTS
                    stroke.strokeWidth = max(0.35f, hw * METEOR_SEG_TAPER[j])
                    nc.drawLine(p.x - tx * a, p.y - ty * a, p.x - tx * b, p.y - ty * b, stroke)
                }
            }
            fill.color = meteorArgb(al, METEOR_HEAD_RGB[headIdx])
            nc.drawCircle(p.x, p.y, max(0.4f, hw * 1.15f), fill)
        }
    }
}

// =============================================================================
// Effect-private draw resources. Baked ONCE per (density, effect) through
// FieldResources.bake — never constructed in update/render, and never on the JVM
// test path (nothing in the physics touches android.graphics).
// =============================================================================
private const val METEOR_GFX_KEY = "meteors.gfx"

/** The additive blend, spelled the way FieldPaints spells it (FIELD_BLEND is the
 *  single source of truth: Plus ≥ API 28, SrcOver below). */
private fun applyFieldBlend(paint: Paint) {
    if (FIELD_BLEND == BlendMode.Plus) {
        if (android.os.Build.VERSION.SDK_INT >= 29) {
            paint.blendMode = android.graphics.BlendMode.PLUS
        } else {
            paint.xfermode = android.graphics.PorterDuffXfermode(android.graphics.PorterDuff.Mode.ADD)
        }
    }
}

/**
 * One gradient for every (kind × quantized head position) the effect can ever
 * ask for: 2 × [METEOR_RAMP_STEPS] = 40, built once, ~10 stops each.
 *
 * The gradient is authored along the UNIT x-axis (0,0)→(1,0); [aimTail] maps that
 * unit segment onto head→tail with a reusable local Matrix. That is what makes a
 * moving gradient free — the alternative is a new Shader per meteor per frame.
 */
private fun buildTailGradient(headT: Float, span: Float): LinearGradient {
    val colors = IntArray(METEOR_TAIL_STOPS)
    val stops = FloatArray(METEOR_TAIL_STOPS)
    for (j in 0 until METEOR_TAIL_STOPS) {
        val a = j / (METEOR_TAIL_STOPS - 1f)
        stops[j] = a
        // Colour walks `span` further along the ramp toward the tail; alpha falls
        // off as (1−a)^1.9 so the streak thins into black instead of ending.
        colors[j] = meteorArgb((1f - a).pow(1.9f), meteorRampRgb(METEOR_RAMP, headT + span * a))
    }
    return LinearGradient(0f, 0f, 1f, 0f, colors, stops, Shader.TileMode.CLAMP)
}

private class MeteorGfx {
    /** Tail stroke. Round caps, exactly like the web's lineCap='round'. */
    val stroke: Paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        applyFieldBlend(this)
    }

    /** Star cores + meteor heads: solid colour off the quantized ramps. */
    val fill: Paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        applyFieldBlend(this)
    }

    private val aim = Matrix()
    private val vals = FloatArray(9)
    private val tails: Array<LinearGradient> = Array(2 * METEOR_RAMP_STEPS) { i ->
        val kind = i / METEOR_RAMP_STEPS
        val step = i % METEOR_RAMP_STEPS
        buildTailGradient(
            step / (METEOR_RAMP_STEPS - 1f),
            if (kind == 1) METEOR_FIREBALL.span else METEOR_DUST.span,
        )
    }

    /**
     * Point the cached gradient down one streak: ([hx],[hy]) is the head and
     * ([tx],[ty]) the vector to the tail end. The matrix is a similarity (the
     * perpendicular fills the second column) so it stays invertible, which Skia
     * requires — a degenerate local matrix silently drops the shader.
     */
    fun aimTail(kind: Int, headIdx: Int, hx: Float, hy: Float, tx: Float, ty: Float) {
        val sh = tails[kind * METEOR_RAMP_STEPS + headIdx]
        vals[0] = tx; vals[1] = -ty; vals[2] = hx
        vals[3] = ty; vals[4] = tx; vals[5] = hy
        vals[6] = 0f; vals[7] = 0f; vals[8] = 1f
        aim.setValues(vals)
        sh.setLocalMatrix(aim)
        // Re-binding is REQUIRED, not belt-and-braces: setLocalMatrix discards the
        // native shader instance the Paint is holding, so without this the streak
        // would draw with the PREVIOUS aim (or vanish).
        stroke.shader = sh
    }
}
