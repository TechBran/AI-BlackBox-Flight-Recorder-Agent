package com.aiblackbox.portal.ui.components

// =============================================================================
// ParticleField — the 3-mode background particle field behind the chat + the
// generation screens. A faithful native port of the web module
//   Portal/modules/ember-fx.js
// which itself implements Appendix A of
//   docs/plans/2026-07-13-system-telemetry-stream-design.md
//
// Three selectable FIELD looks (orthogonal to the OFF/GENERATING/ALWAYS
// visibility setting carried by EmberMode / LocalEmberMode):
//   • stars   Rising Stars  — DEFAULT. 3 parallax depth layers, power-skewed
//                sizes (mostly tiny, few bright), de-synced twinkle (per-star
//                phase+speed), glow sprite only on the hero layer, crisp cores.
//                Cheapest mode; highest count budget.
//   • embers  Embers        — curl-noise divergence-free swirl, blackbody
//                white-hot→deep-red ramp by particle life, pre-baked sprite atlas
//                drawn twice (faint glow + bright core) additively, buoyancy +
//                drag + sparks, ground heat-glow.
//   • matrix  Matrix        — column rain, bright leading glyph + fading green
//                trail, per-column speed variance, katakana + digits.
//
// Architecture (all performance-first, per Appendix A):
//   • ONE Compose Canvas per overlay reads a frame clock (mutableLongState) so
//     only the DRAW phase invalidates each animation frame — never recomposition.
//   • The sims (StarSim/EmberSim/MatrixSim) are UI-free plain Kotlin (no Compose
//     imports touched by resize/update) so the physics is unit-testable on the
//     JVM (see ParticleFieldTest). Drawing is separate DrawScope extensions.
//   • Object pooling: fixed-capacity arrays reused across frames; embers use a
//     dead-slot free list; matrix reuses a CharArray(1). No per-frame allocation
//     in the hot path (the one per-frame Brush for the ember ground-glow mirrors
//     the web and is a single allocation, not per-particle).
//   • Additive blend (BlendMode.Plus, SrcOver fallback < API 28) builds the
//     white-hot core exactly like the web's globalCompositeOperation='lighter'.
//   • Pre-baked radial sprites (ImageBitmap) so the hot loop never allocates a
//     Shader (the #1 perf trap — per-particle Brush.radialGradient).
//   • Delta-timed updates (dt clamped ≤ 0.05 s) so motion is frame-rate
//     independent; counts scale by logical (dp) screen area, clamped to the
//     Appendix-A phone caps (Stars 400–800 · Embers 200–400 · Matrix 40–80 cols).
//   • DPI: all spatial lengths (sizes, velocities, radii, noise sampling) are
//     multiplied by scale = density / REF_DENSITY so the field looks the same
//     apparent size on any phone (scale = 1 on the reference device).
//
// These constants are the on-device tuning dials — the FIELD is visual and is
// device-validated by the operator, not by unit assertions on coordinates.
// =============================================================================

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.geometry.Offset
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
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

// =============================================================================
// ParticleMode — the persisted FIELD look, and the CompositionLocal that carries
// it down to EmberOverlay (provided ONCE at the activity root from the
// `particle_mode` preference; default STARS). Orthogonal to EmberMode, which
// governs OFF/GENERATING/ALWAYS visibility.
// =============================================================================
object ParticleMode {
    const val STARS = "stars"
    const val EMBERS = "embers"
    const val MATRIX = "matrix"

    /** Persisted values in preferred display order (drives the settings picker). */
    val ALL = listOf(STARS, EMBERS, MATRIX)

    /** Normalize any stored/legacy value to a known mode; unknown/null → STARS. */
    fun parse(raw: String?): String = when (raw?.trim()?.lowercase()) {
        EMBERS -> EMBERS
        MATRIX -> MATRIX
        else -> STARS
    }

    /** Human label for the picker. */
    fun label(mode: String): String = when (parse(mode)) {
        EMBERS -> "Embers"
        MATRIX -> "Matrix"
        else -> "Rising Stars"
    }
}

/** Provided once at the activity root from the persisted setting; read by EmberOverlay. */
val LocalParticleMode = staticCompositionLocalOf { ParticleMode.STARS }

// -----------------------------------------------------------------------------
// Shared field tunables
// -----------------------------------------------------------------------------
/** Cap the post-generation drain so the frame loop can never idle-spin. Must
 *  outlast the DurationSlow alpha fade so the field animates through the fade. */
internal const val DRAIN_MAX_MS = 650.0

/** Density the look was tuned on (Fold-class ≈ 3.1). scale = 1 there; other DPIs
 *  scale all spatial lengths so embers/stars keep the same apparent size. */
internal const val FIELD_REFERENCE_DENSITY = 3.1f

/** Additive blend — mirrors the web's globalCompositeOperation='lighter'.
 *  BlendMode.Plus is unreliable on the HW canvas before API 28, so fall back to
 *  SrcOver there; over pure black it degrades gracefully with no rebuild.
 *  (The fade layer uses CompositingStrategy.ModulateAlpha — see EmberOverlay — so
 *  alpha multiplies per draw op and additive survives the fade.) */
internal val FIELD_BLEND =
    if (android.os.Build.VERSION.SDK_INT < 28) BlendMode.SrcOver else BlendMode.Plus

// =============================================================================
// FieldSim — the UI-free contract every field implements. resize/update/rearm
// touch only plain Kotlin state (no Compose types) so they run in JVM tests.
// =============================================================================
sealed interface FieldSim {
    /** Set the field size + DPI. Spawns lazily on the first valid size; a size
     *  change grows/trims (or rebuilds) in place — never a full re-scatter of a
     *  live field on a no-op resize (the Canvas calls this every frame). */
    fun resize(width: Float, height: Float, scale: Float, density: Float)

    /** Advance one frame. [nowMs] is a millis time base; [dtSec] is delta seconds
     *  (already clamped ≤ 0.05). [active] gates spawning (generation in progress). */
    fun update(nowMs: Double, dtSec: Float, active: Boolean)

    /** Fresh start on a new activation (embers clear so each turn rises fresh). */
    fun rearm()
}

/** Build the sim for a mode. Unknown modes resolve to STARS via ParticleMode.parse. */
fun newFieldSim(mode: String): FieldSim = when (ParticleMode.parse(mode)) {
    ParticleMode.EMBERS -> EmberSim()
    ParticleMode.MATRIX -> MatrixSim()
    else -> StarSim()
}

// =============================================================================
// Sprite atlas — pre-baked radial blobs (ImageBitmap) so the hot draw loop never
// allocates a Shader. Baked ONCE per density.
// =============================================================================
/** Blackbody-ish ember ramp: white-hot core → deep ember red (web RAMP). */
private val RAMP = arrayOf(
    intArrayOf(255, 255, 240),
    intArrayOf(255, 238, 150),
    intArrayOf(255, 182, 64),
    intArrayOf(255, 110, 22),
    intArrayOf(201, 44, 6),
    intArrayOf(92, 16, 5),
)
private const val SPRITE_PX = 64

class FieldSprites(val ember: List<ImageBitmap>, val star: ImageBitmap) {
    // The SAME baked sprites as platform Bitmaps. The sub-pixel draw path
    // (nativeCanvas.drawBitmap with a float RectF destination — see drawSpriteF)
    // needs the platform type; converting once here keeps the hot loop free of
    // per-draw casts. asAndroidBitmap() is a cast on an AndroidImageBitmap, not a copy.
    internal val emberNative: List<android.graphics.Bitmap> = ember.map { it.asAndroidBitmap() }
    internal val starNative: android.graphics.Bitmap = star.asAndroidBitmap()
}

/**
 * Per-overlay scratch objects for the draw phase. Allocated ONCE (remembered by
 * EmberOverlay) and reused every frame so the hot loop allocates nothing:
 *   • [sprite]  additive bitmap paint — blend + bitmap filtering set once here so
 *               it matches what DrawScope.drawImage would have configured
 *               (FilterQuality.Low → FILTER_BITMAP, FIELD_BLEND → PLUS/ADD).
 *   • [matrix]  the monospace glyph paint (text size set per draw).
 *   • [dst]     the destination rect handed to every sprite draw.
 */
class FieldPaints {
    val sprite: android.graphics.Paint =
        android.graphics.Paint(android.graphics.Paint.FILTER_BITMAP_FLAG).apply {
            isAntiAlias = true
            // Single source of truth: FIELD_BLEND (Plus ≥ 28, SrcOver below) — the
            // platform spellings of Plus are BlendMode.PLUS (API 29+) and the
            // equivalent PorterDuff ADD xfermode on 28. SrcOver is the paint default.
            if (FIELD_BLEND == BlendMode.Plus) {
                if (android.os.Build.VERSION.SDK_INT >= 29) {
                    blendMode = android.graphics.BlendMode.PLUS
                } else {
                    xfermode = android.graphics.PorterDuffXfermode(android.graphics.PorterDuff.Mode.ADD)
                }
            }
        }
    val matrix: android.graphics.Paint =
        android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply {
            typeface = android.graphics.Typeface.MONOSPACE
        }
    val dst: android.graphics.RectF = android.graphics.RectF()
}

/**
 * Draw a pre-baked sprite centred on ([cx],[cy]) with a **float** destination.
 *
 * Compose's `DrawScope.drawImage` only offers an IntOffset/IntSize destination,
 * which snaps every sprite to whole pixels — that quantization is what makes the
 * slow far-layer stars visibly STEP on a 120 Hz panel (they move well under one
 * pixel per frame, so several frames round to the same integer and then it jumps).
 * `nativeCanvas.drawBitmap(bitmap, src, RectF, paint)` keeps the sub-pixel
 * position, which is the whole point. Apparent size is unchanged (the diameter is
 * the same, just no longer rounded); a floor of 1 px keeps the tiniest trail
 * sprites from thinning out.
 */
private fun DrawScope.drawSpriteF(
    bmp: android.graphics.Bitmap,
    cx: Float,
    cy: Float,
    rad: Float,
    a: Float,
    paints: FieldPaints,
) {
    if (a <= 0.003f || rad <= 0.1f) return   // same cull thresholds as the Int path
    val half = rad.coerceAtLeast(0.5f)       // ≥ 1 px wide, as dstSize.coerceAtLeast(1) was
    paints.dst.set(cx - half, cy - half, cx + half, cy + half)
    paints.sprite.alpha = (a.coerceIn(0f, 1f) * 255f).roundToInt()
    drawContext.canvas.nativeCanvas.drawBitmap(bmp, null, paints.dst, paints.sprite)
}

private fun buildRadialSprite(r: Int, g: Int, b: Int, density: Density): ImageBitmap {
    val bitmap = ImageBitmap(SPRITE_PX, SPRITE_PX)
    // Fully-qualified bitmap-backed Canvas factory (distinct from the @Composable Canvas).
    val canvas = androidx.compose.ui.graphics.Canvas(bitmap)
    val drawScope = CanvasDrawScope()
    val radius = SPRITE_PX / 2f
    val center = Offset(radius, radius)
    val base = Color(r / 255f, g / 255f, b / 255f, 1f)
    drawScope.draw(density, LayoutDirection.Ltr, canvas, Size(SPRITE_PX.toFloat(), SPRITE_PX.toFloat())) {
        drawCircle(
            brush = Brush.radialGradient(
                colorStops = arrayOf(
                    0.0f to base,
                    0.35f to base.copy(alpha = 0.5f),
                    1.0f to base.copy(alpha = 0f),
                ),
                center = center,
                radius = radius,
            ),
            radius = radius,
            center = center,
        )
    }
    return bitmap
}

/** Bake the 6 ember-ramp sprites + 1 warm-white star sprite ONCE. */
fun buildFieldSprites(density: Density): FieldSprites = FieldSprites(
    ember = RAMP.map { buildRadialSprite(it[0], it[1], it[2], density) },
    star = buildRadialSprite(255, 252, 246, density),
)

// =============================================================================
// Field: RISING STARS — the ORIGINAL warm rising-ember field, restored from the
// pre-3-mode engine (721a045^ EmberParticles.kt). This is the look Brandon
// confirmed he wants for "Rising Stars" ("I liked the way it looked before").
// UI-free StarParticle/StarSim physics; drawn with the warm ember sprites.
// =============================================================================
private val STAR_LAYER_COUNT = intArrayOf(80, 100, 60)        // far, mid, foreground
private val STAR_LAYER_SPEED = floatArrayOf(0.3f, 0.5f, 0.8f)
private val STAR_SMIN = floatArrayOf(0.8f, 1.5f, 2.2f)
private val STAR_SMAX = floatArrayOf(1.6f, 3f, 4.5f)
private val STAR_LAYER_OPACITY = floatArrayOf(0.25f, 0.4f, 0.7f)
private val STAR_COLOR_WEIGHTS = doubleArrayOf(0.3, 0.3, 0.2, 0.15, 0.05)
private const val STAR_TURBULENCE = 0.6
private const val STAR_RISE_SPEED = 0.8
private const val STAR_FLICKER_SPEED = 0.015
private const val STAR_TRAIL_LEN = 2
private const val STAR_GLOW = 14f

class StarParticle(val layerIndex: Int) {
    var x = 0f; var y = 0f; var size = 0f; var baseSize = 0f; var colorIndex = 0
    var vx = 0f; var vy = 0f; var baseVy = 0f
    var opacity = 0f; var baseOpacity = 0f; var dead = false
    private var oscOffset = 0.0; private var oscSpeed = 0.0; private var oscAmp = 0.0
    private var flickOffset = 0.0; private var flickJitter = 0.0
    val trailX = FloatArray(STAR_TRAIL_LEN); val trailY = FloatArray(STAR_TRAIL_LEN)
    val trailSize = FloatArray(STAR_TRAIL_LEN); val trailOpacity = FloatArray(STAR_TRAIL_LEN)
    var trailLen = 0; private set

    fun reset(width: Float, height: Float, scale: Float) {
        x = (Math.random() * width).toFloat()
        y = (height + Math.random() * 100 * scale).toFloat()
        val sMin = STAR_SMIN[layerIndex]; val sMax = STAR_SMAX[layerIndex]
        size = ((sMin + Math.random() * (sMax - sMin)) * scale).toFloat(); baseSize = size
        colorIndex = pickColorIndex()
        val speed = STAR_LAYER_SPEED[layerIndex]
        vx = ((Math.random() - 0.5) * 2.0 * speed * scale).toFloat()
        vy = (-(0.5 + Math.random() * 0.5) * STAR_RISE_SPEED * speed * scale).toFloat(); baseVy = vy
        oscOffset = Math.random() * Math.PI * 2
        oscSpeed = 0.005 + Math.random() * 0.008
        oscAmp = (5 + Math.random() * 10) * scale
        flickOffset = Math.random() * Math.PI * 2
        flickJitter = STAR_FLICKER_SPEED * (0.8 + Math.random() * 0.4)
        opacity = STAR_LAYER_OPACITY[layerIndex]; baseOpacity = opacity
        trailLen = 0; dead = false
    }

    fun update(timeMs: Double, width: Float, height: Float, active: Boolean, scale: Float, dtSec: Float = 1f / 60f) {
        if (dead) { if (active) reset(width, height, scale) else return }
        // dt-normalize to the 60fps reference tick the physics constants were tuned
        // at: velocities are px-per-60Hz-tick, so without this the field runs ~2x
        // fast on a 120Hz display (the Fold) vs the approved 60Hz look.
        val dt = (dtSec * 60f).coerceIn(0.25f, 4f)
        val turbX = (sin(timeMs * 0.0003 + oscOffset) * STAR_TURBULENCE * 0.3 * scale).toFloat()
        val turbY = (cos(timeMs * 0.0004 + oscOffset) * STAR_TURBULENCE * 0.15 * scale).toFloat()
        val oscillation = (sin(timeMs * oscSpeed + oscOffset) * oscAmp * 0.002).toFloat()
        vx += (turbX * 0.005f + oscillation) * dt - vx * (0.02f * dt)
        vy = baseVy + turbY * 0.005f
        x += vx * dt; y += vy * dt
        pushTrail(x, y, size, opacity)
        val f1 = sin(timeMs * flickJitter + flickOffset)
        val f2 = sin(timeMs * flickJitter * 0.7 + flickOffset * 1.3)
        val flicker = ((f1 + f2 * 0.5) / 1.5).toFloat()
        opacity += (baseOpacity * (0.7f + flicker * 0.3f) - opacity) * (0.05f * dt).coerceAtMost(1f)
        size += (baseSize * (0.9f + flicker * 0.1f) - size) * (0.05f * dt).coerceAtMost(1f)
        if (y < height * 0.2f) opacity *= (y / (height * 0.2f))
        if (y < -50f * scale || x < -50f * scale || x > width + 50f * scale) {
            if (active) reset(width, height, scale) else dead = true
        }
    }

    private fun pushTrail(px: Float, py: Float, ps: Float, po: Float) {
        var i = minOf(trailLen, STAR_TRAIL_LEN - 1)
        while (i > 0) { trailX[i] = trailX[i-1]; trailY[i] = trailY[i-1]; trailSize[i] = trailSize[i-1]; trailOpacity[i] = trailOpacity[i-1]; i-- }
        trailX[0] = px; trailY[0] = py; trailSize[0] = ps; trailOpacity[0] = po
        if (trailLen < STAR_TRAIL_LEN) trailLen++
    }

    private fun pickColorIndex(): Int {
        val r = Math.random(); var c = 0.0
        for (i in STAR_COLOR_WEIGHTS.indices) { c += STAR_COLOR_WEIGHTS[i]; if (r < c) return i }
        return 0
    }
}

class StarSim : FieldSim {
    private var width = 0f; private var height = 0f; private var scale = 1f
    private val parts = ArrayList<StarParticle>(240)
    val particles: List<StarParticle> get() = parts

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        if (width == this.width && height == this.height && parts.isNotEmpty()) return
        val prevWidth = this.width
        this.width = width; this.height = height; this.scale = scale
        if (parts.isEmpty()) { spawnAll(); return }
        // A real WIDTH GROWTH (unfolding a Fold, rotating to landscape) reveals a
        // strip the live field has no particles in: every star was seeded across
        // the OLD width and only rises, so the new area stays empty until each
        // star exits the top and respawns — minutes at the 0.3–0.8 layer speeds.
        // Resample x across the new width instead: a uniform distribution stays
        // uniform, so the field fills instantly with no visible re-scatter.
        // (Height changes self-heal: stars respawn at the CURRENT bottom edge.)
        if (prevWidth > 0f && width > prevWidth + 1f) {
            val k = width / prevWidth
            for (p in parts) {
                p.x *= k
                for (i in 0 until p.trailLen) p.trailX[i] *= k
            }
        }
    }
    private fun spawnAll() {
        parts.clear()
        for (layer in STAR_LAYER_COUNT.indices) repeat(STAR_LAYER_COUNT[layer]) {
            val p = StarParticle(layer); p.reset(width, height, scale)
            p.y = (Math.random() * height * 1.5).toFloat(); parts.add(p)
        }
    }
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (width <= 0f || height <= 0f) return
        for (p in parts) p.update(nowMs, width, height, active, scale, dtSec)
    }
    override fun rearm() {
        if (width <= 0f || height <= 0f || parts.isEmpty()) return
        for (p in parts) if (!p.dead) return   // only re-stagger once fully drained
        for (p in parts) { p.reset(width, height, scale); p.y = (Math.random() * height * 1.5).toFloat() }
    }
}

private fun DrawScope.drawStars(sim: StarSim, sprites: FieldSprites, paints: FieldPaints, nowMs: Double) {
    val emberN = sprites.emberNative.size
    for (p in sim.particles) {
        if (p.dead || p.opacity <= 0.003f) continue
        val spr = sprites.emberNative[(4 - p.colorIndex).coerceIn(0, emberN - 1)] // warm: red↔deep-ember
        for (i in 0 until p.trailLen) {
            val fade = 1f - i / STAR_TRAIL_LEN.toFloat()
            drawSpriteF(spr, p.trailX[i], p.trailY[i], p.trailSize[i] * fade, p.trailOpacity[i] * fade * 0.5f, paints)
        }
        drawSpriteF(spr, p.x, p.y, p.size * STAR_GLOW, p.opacity * 0.45f, paints)    // soft glow
        drawSpriteF(spr, p.x, p.y, p.size * 1.7f, p.opacity, paints)                 // bright core
        drawSpriteF(sprites.starNative, p.x, p.y, p.size * 0.9f, p.opacity * 0.9f, paints) // white-hot center
    }
}

// =============================================================================
// Field: EMBERS (curl-noise divergence-free swirl + blackbody ramp + additive
// sprite bloom + buoyancy/drag/sparks + ground heat-glow)
// =============================================================================
class Emb {
    var x = 0f; var y = 0f; var vx = 0f; var vy = 0f
    var life = 0f; var decay = 0f; var r = 0f; var spark = false; var alive = false
    var hue0 = 0; var fade = 0f
}

class EmberSim : FieldSim {
    var width = 0f; private set
    var height = 0f; private set
    private var scale = 1f
    private var pool = emptyArray<Emb>()
    private var spawnAcc = 0f
    // Dead-slot FREE LIST (the pooling the header comment promises). Spawn used to
    // linear-scan the pool for the first !alive slot; with the pool ~85% full and
    // ~340 spawns/sec that burns ~1,100 wasted probes per frame. An IntArray stack
    // of dead indices makes spawn O(1): pop on spawn, push on death.
    private var freeIdx = IntArray(0)
    private var freeCount = 0

    val alivePool: List<Emb> get() = pool.asList()

    /** Concurrent-ember cap from logical (dp) area, clamped to Embers 200–400. */
    private fun targetMax(density: Float): Int {
        val areaDp = (width / density) * (height / density)
        return (areaDp / 900f).roundToInt().coerceIn(200, 400)
    }

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        this.width = width; this.height = height; this.scale = scale
        val want = targetMax(density)
        if (pool.size != want) {
            pool = Array(want) { Emb() }
            freeIdx = IntArray(want)
            for (i in 0 until want) freeIdx[i] = i   // every slot dead → every slot free
            freeCount = want
            seedFull()                                // seed full → whole screen at rest
        }
    }

    /** Spawn one ember ANYWHERE on screen (full-screen floating), gentle drift. */
    private fun spawnOne() {
        if (freeCount == 0) return                    // pool saturated (was: linear scan)
        val p = pool[freeIdx[--freeCount]]
        val spark = Math.random() < 0.08
        val ml = 2.6 + Math.random() * 3.6                       // long life → floats across
        p.x = (Math.random() * width).toFloat()
        p.y = (Math.random() * height).toFloat()                 // anywhere, not just the bottom
        p.vx = ((Math.random() - 0.5) * 16 * scale).toFloat()
        p.vy = (-(4 + Math.random() * 14) * scale).toFloat()     // gentle float, not a bottom jet
        p.life = 1f
        p.decay = (1.0 / ml).toFloat()
        p.r = ((if (spark) 1.2 + Math.random() * 1.3 else 2.5 + Math.random() * 6) * scale).toFloat()
        p.spark = spark
        p.hue0 = (Math.random() * 3).toInt()                     // 0..2 varied warm heat
        p.fade = (Math.random() * 6.28).toFloat()
        p.alive = true
    }
    private fun seedFull() { for (i in pool.indices) spawnOne() }

    // 2-octave sine curl-noise potential ψ, sampled in LOGICAL space (÷scale) so
    // the web spatial frequencies hold across DPIs → divergence-free swirl.
    private fun pot(x: Float, y: Float, t: Double): Double {
        val lx = x / scale; val ly = y / scale
        return sin(lx * 0.0065 + t * 0.22) * cos(ly * 0.0065 - t * 0.16) +
            0.5 * sin(lx * 0.013 - t * 0.31) * cos(ly * 0.013 + t * 0.26)
    }

    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty()) return
        val ts = nowMs * 0.001
        val eps = 3f * scale
        if (active) {
            // Keep the pool ~full while generating (avg life ≈ 1.15 s).
            val perSec = pool.size * 0.85f
            spawnAcc += perSec * dtSec
            while (spawnAcc >= 1f) { spawnOne(); spawnAcc -= 1f }
        }
        for (i in pool.indices) {
            val p = pool[i]
            if (!p.alive) continue
            p.life -= p.decay * dtSec
            if (p.life <= 0f) { kill(p, i); continue }
            // curl(ψ) via central differences → swirl velocity (px, so ×scale).
            val cvx = pot(p.x, p.y + eps, ts) - pot(p.x, p.y - eps, ts)
            val cvy = -(pot(p.x + eps, p.y, ts) - pot(p.x - eps, p.y, ts))
            p.vx += (cvx * 5200 * dtSec * scale).toFloat()
            p.vy += (cvy * 5200 * dtSec * scale).toFloat()
            p.vy -= 9f * p.life * dtSec * scale            // gentle buoyancy (slow float up)
            if (p.spark) p.vy += 60f * dtSec * scale        // sparks drift down a touch
            p.vx *= (1f - 0.9f * dtSec); p.vy *= (1f - 0.9f * dtSec) // drag
            p.x += p.vx * dtSec; p.y += p.vy * dtSec
            // wrap horizontally so the field stays full across the whole width
            if (p.x < -20f * scale) p.x = width + 20f * scale
            else if (p.x > width + 20f * scale) p.x = -20f * scale
            if (p.y < -30f * scale) kill(p, i)              // floated off the top → recycle
        }
    }

    /** Retire slot [i] and return it to the free list. Only ever called on a live
     *  slot (both call sites are inside `if (!p.alive) continue`), so an index can
     *  never be pushed twice. */
    private fun kill(p: Emb, i: Int) {
        p.alive = false
        freeIdx[freeCount++] = i
    }

    override fun rearm() {
        spawnAcc = 0f
        seedFull()   // refill the whole screen on re-activation
    }
}

private fun DrawScope.drawEmbers(
    sim: EmberSim,
    sprites: List<android.graphics.Bitmap>,
    paints: FieldPaints,
    nowMs: Double,
) {
    val w = sim.width; val h = sim.height
    if (w <= 0f || h <= 0f) return
    // NO ground heat-glow — real embers float across the WHOLE screen, not a
    // fireball at the base.
    val ts = nowMs * 0.001
    val last = RAMP.size - 1
    for (p in sim.alivePool) {
        if (!p.alive) continue
        val idx = if (p.spark) 0 else (p.hue0 + ((1f - p.life) * 3f).roundToInt()).coerceIn(0, last)
        val spr = sprites[idx]
        val breathe = (0.6 + 0.4 * sin(ts + p.fade)).toFloat()   // soft per-ember flicker
        val al = ((if (p.spark) 0.85f else 0.55f) * minOf(1f, p.life * 1.4f) * breathe).coerceIn(0f, 1f)
        // Faint big glow (cheap bloom) …
        drawSpriteF(spr, p.x, p.y, p.r * (if (p.spark) 4f else 4.2f), al * 0.22f, paints)
        // … + bright core.
        drawSpriteF(spr, p.x, p.y, p.r * (if (p.spark) 1.6f else 1.9f), al, paints)
    }
}

// =============================================================================
// Field: MATRIX (column rain — bright leading glyph + fading green trail)
// =============================================================================
// Compose Canvas clears every frame (no HTML-canvas persistence), so the web's
// translucent-smear trail is emulated by drawing a short fading tail per column.
private const val MATRIX_TRAIL = 12

private val MATRIX_GLYPHS: CharArray = buildString {
    for (c in 0x30A0..0x30FE) append(c.toChar())   // katakana
    append("0123456789ABCDEF<>*+")                  // digits / symbols
}.toCharArray()

class MCol {
    var y = 0f; var sp = 0f; var glyph = MATRIX_GLYPHS[0]; var last = 0.0
}

class MatrixSim : FieldSim {
    var width = 0f; private set
    var height = 0f; private set
    var fontSizePx = 0f; private set
    private var cols = emptyArray<MCol>()
    val columns: List<MCol> get() = cols.asList()

    private fun randGlyph(): Char = MATRIX_GLYPHS[(Math.random() * MATRIX_GLYPHS.size).toInt()]

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val sameSize = width == this.width && height == this.height && cols.isNotEmpty()
        this.width = width; this.height = height
        if (sameSize) return
        // columns from logical width, clamped to the Matrix 40–80 cap; the glyph
        // size then tiles the width exactly.
        val widthDp = width / density
        val n = (widthDp / 10f).roundToInt().coerceIn(40, 80)
        fontSizePx = width / n
        cols = Array(n) { MCol() }
        for (c in cols) {
            c.y = (Math.random() * -height).toFloat()
            c.sp = (fontSizePx * (3 + Math.random() * 6)).toFloat()
            c.glyph = randGlyph()
            c.last = 0.0
        }
    }

    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        for (c in cols) {
            if (nowMs - c.last > 36) { c.glyph = randGlyph(); c.last = nowMs }
            c.y += c.sp * dtSec
            if (c.y > height + Math.random() * height * 0.3) {
                c.y = (Math.random() * -40).toFloat()
                c.sp = (fontSizePx * (3 + Math.random() * 6)).toFloat()
            }
        }
    }

    override fun rearm() { /* matrix rains continuously; nothing to reset */ }
}

private fun argb(a: Float, r: Int, g: Int, b: Int): Int =
    ((a.coerceIn(0f, 1f) * 255f).toInt() shl 24) or (r shl 16) or (g shl 8) or b

private fun DrawScope.drawMatrix(sim: MatrixSim, paint: android.graphics.Paint, nowMs: Double) {
    if (sim.fontSizePx <= 0f) return
    paint.textSize = sim.fontSizePx
    val topToBaseline = -paint.fontMetrics.ascent // web textBaseline='top' → Android baseline
    val fs = sim.fontSizePx
    val len = MATRIX_GLYPHS.size
    val phase = (nowMs / 90.0).toInt()
    val buf = CharArray(1)
    val nc = drawContext.canvas.nativeCanvas
    sim.columns.forEachIndexed { i, c ->
        val x = i * fs
        // Fading green trail above the leading glyph.
        var j = 1
        while (j <= MATRIX_TRAIL) {
            val top = c.y - j * fs
            if (top in -fs..sim.height) {
                val a = 0.5f * (1f - j.toFloat() / MATRIX_TRAIL)
                paint.color = argb(a, 40, 220, 90)
                buf[0] = MATRIX_GLYPHS[(((i * 7 + j + phase) % len) + len) % len]
                nc.drawText(buf, 0, 1, x, top + topToBaseline, paint)
            }
            j++
        }
        // Bright leading glyph.
        paint.color = argb(0.95f, 200, 255, 210)
        buf[0] = c.glyph
        nc.drawText(buf, 0, 1, x, c.y + topToBaseline, paint)
    }
}

// =============================================================================
// Dispatch — one entry point the Canvas calls; picks the draw routine per mode.
// =============================================================================
internal fun DrawScope.drawParticleField(
    sim: FieldSim,
    sprites: FieldSprites,
    paints: FieldPaints,
    nowMs: Double,
) {
    when (sim) {
        is StarSim -> drawStars(sim, sprites, paints, nowMs)
        is EmberSim -> drawEmbers(sim, sprites.emberNative, paints, nowMs)
        is MatrixSim -> drawMatrix(sim, paints.matrix, nowMs)
    }
}
