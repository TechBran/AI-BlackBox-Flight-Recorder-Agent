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
import androidx.compose.ui.graphics.drawscope.CanvasDrawScope
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
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
 *  BlendMode.Plus is unreliable on the HW canvas before API 28 (and we wrap the
 *  Canvas in an alpha graphicsLayer → offscreen buffer), so fall back to SrcOver
 *  there; over pure black it degrades gracefully with no rebuild. */
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

class FieldSprites(val ember: List<ImageBitmap>, val star: ImageBitmap)

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
// Field: RISING STARS (parallax + de-synced twinkle) — DEFAULT
// =============================================================================
// Per-layer size / rise-speed / alpha ranges (web LAYERS, sizes bumped ~1.6× for
// high-DPI phone visibility). Layer 2 (foreground/hero) gets the glow sprite.
private val STAR_SIZE = arrayOf(
    floatArrayOf(0.8f, 1.6f),   // far / tiny
    floatArrayOf(1.6f, 2.9f),   // mid / small
    floatArrayOf(2.9f, 4.2f),   // fore / hero
)
private val STAR_VY = floatArrayOf(0.4f, 0.7f, 1.0f)
private val STAR_ALPHA = arrayOf(
    floatArrayOf(0.4f, 0.6f),
    floatArrayOf(0.6f, 0.8f),
    floatArrayOf(0.8f, 1.0f),
)

class Star {
    var x = 0f; var y = 0f; var size = 0f; var vy = 0f; var vx = 0f
    var base = 0f; var amp = 0f; var tw = 0f; var seed = 0f
    var hue = 0; var glow = false

    fun init(width: Float, height: Float, scale: Float) {
        val roll = Math.random()
        val layer = if (roll > 0.9) 2 else if (roll > 0.6) 1 else 0
        val sk = Math.pow(Math.random(), 2.2) // power-skewed: mostly tiny, few bright
        val sMin = STAR_SIZE[layer][0]; val sMax = STAR_SIZE[layer][1]
        x = (Math.random() * width).toFloat()
        y = (Math.random() * height).toFloat()
        size = ((sMin + (sMax - sMin) * sk) * scale).toFloat()
        vy = (-(8 + Math.random() * 17) * STAR_VY[layer] * scale).toFloat()
        vx = ((Math.random() - 0.5) * 4 * scale).toFloat()
        base = (STAR_ALPHA[layer][0] + (STAR_ALPHA[layer][1] - STAR_ALPHA[layer][0]) * Math.random()).toFloat()
        amp = (0.25 + Math.random() * 0.2).toFloat()
        tw = (0.8 + Math.random() * 1.7).toFloat()
        seed = (Math.random() * 6.28).toFloat()
        hue = if (Math.random() < 0.7) 255 else if (Math.random() < 0.5) 250 else 34
        glow = layer == 2
    }
}

class StarSim : FieldSim {
    var width = 0f; private set
    var height = 0f; private set
    private var scale = 1f
    private val _stars = ArrayList<Star>()
    val stars: List<Star> get() = _stars

    /** Responsive count from logical (dp) area, clamped to the Stars 400–800 cap. */
    private fun targetCount(): Int {
        val areaDp = (width / scaledDensity) * (height / scaledDensity)
        return (areaDp / 480f).roundToInt().coerceIn(400, 800)
    }
    private var scaledDensity = FIELD_REFERENCE_DENSITY

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val sameSize = width == this.width && height == this.height && _stars.isNotEmpty()
        this.width = width; this.height = height; this.scale = scale; this.scaledDensity = density
        if (sameSize) return
        val want = targetCount()
        // Grow/trim IN PLACE so existing stars keep positions (no resize "pop").
        while (_stars.size < want) _stars.add(Star().apply { init(width, height, scale) })
        if (_stars.size > want) while (_stars.size > want) _stars.removeAt(_stars.size - 1)
    }

    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        // Stars drift continuously (active only affects whether the loop keeps
        // running; during the drain they keep rising until the deadline).
        for (s in _stars) {
            s.y += s.vy * dtSec
            s.x += s.vx * dtSec
            if (s.y < -6f * scale) { s.y = height + 6f * scale; s.x = (Math.random() * width).toFloat() }
        }
    }

    override fun rearm() { /* stars self-heal; nothing to reset */ }
}

private fun starColor(hue: Int): Color = when (hue) {
    34 -> Color(255 / 255f, 205 / 255f, 110 / 255f)   // warm amber
    250 -> Color(200 / 255f, 215 / 255f, 255 / 255f)  // cool blue-white
    else -> Color(248 / 255f, 247 / 255f, 255 / 255f) // near-white
}

private fun DrawScope.drawStars(sim: StarSim, starSprite: ImageBitmap, nowMs: Double) {
    val ts = nowMs * 0.001
    for (s in sim.stars) {
        val al = (s.base + sin(ts * s.tw + s.seed) * s.amp).coerceIn(0.0, 1.0).toFloat()
        if (al <= 0f) continue
        if (s.glow) {
            val gr = s.size * 7f
            drawImage(
                image = starSprite,
                srcOffset = IntOffset.Zero,
                srcSize = IntSize(SPRITE_PX, SPRITE_PX),
                dstOffset = IntOffset((s.x - gr).roundToInt(), (s.y - gr).roundToInt()),
                dstSize = IntSize((gr * 2f).roundToInt(), (gr * 2f).roundToInt()),
                alpha = al * 0.5f,
                blendMode = FIELD_BLEND,
            )
        }
        drawCircle(
            color = starColor(s.hue).copy(alpha = al),
            radius = s.size,
            center = Offset(s.x, s.y),
            blendMode = FIELD_BLEND,
        )
    }
}

// =============================================================================
// Field: EMBERS (curl-noise divergence-free swirl + blackbody ramp + additive
// sprite bloom + buoyancy/drag/sparks + ground heat-glow)
// =============================================================================
class Emb {
    var x = 0f; var y = 0f; var vx = 0f; var vy = 0f
    var life = 0f; var decay = 0f; var r = 0f; var spark = false; var alive = false
}

class EmberSim : FieldSim {
    var width = 0f; private set
    var height = 0f; private set
    private var scale = 1f
    private var pool = emptyArray<Emb>()
    private var spawnAcc = 0f

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
        if (pool.size != want) pool = Array(want) { Emb() }
    }

    /** Spawn one ember into the first dead slot; pool-full → skip (self-limits). */
    private fun spawnOne() {
        val p = pool.firstOrNull { !it.alive } ?: return
        val spark = Math.random() < 0.12
        val ml = 0.8 + Math.random() * 0.7
        p.x = (width * (0.08 + Math.random() * 0.84)).toFloat()
        p.y = (height + (10 + Math.random() * 20) * scale).toFloat()
        p.vx = ((Math.random() - 0.5) * 60 * scale).toFloat()
        p.vy = (-(60 + Math.random() * 130) * scale).toFloat()
        p.life = 1f
        p.decay = (1.0 / ml).toFloat()
        p.r = ((if (spark) 1.6 + Math.random() * 1.4 else 6 + Math.random() * 11) * scale).toFloat()
        p.spark = spark
        p.alive = true
    }

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
        for (p in pool) {
            if (!p.alive) continue
            p.life -= p.decay * dtSec
            if (p.life <= 0f) { p.alive = false; continue }
            // curl(ψ) via central differences → swirl velocity (px, so ×scale).
            val cvx = pot(p.x, p.y + eps, ts) - pot(p.x, p.y - eps, ts)
            val cvy = -(pot(p.x + eps, p.y, ts) - pot(p.x - eps, p.y, ts))
            p.vx += (cvx * 7200 * dtSec * scale).toFloat()
            p.vy += (cvy * 7200 * dtSec * scale).toFloat()
            p.vy -= 52f * p.life * dtSec * scale          // buoyancy ∝ heat
            if (p.spark) p.vy += 120f * dtSec * scale      // sparks arc under gravity
            p.vx *= (1f - 1.3f * dtSec); p.vy *= (1f - 0.9f * dtSec) // drag
            p.x += p.vx * dtSec; p.y += p.vy * dtSec
            if (p.x < -50f * scale || p.x > width + 50f * scale || p.y < -50f * scale) p.alive = false
        }
    }

    override fun rearm() {
        for (p in pool) p.alive = false
        spawnAcc = 0f
    }
}

private fun DrawScope.drawEmbers(sim: EmberSim, sprites: List<ImageBitmap>, @Suppress("UNUSED_PARAMETER") nowMs: Double) {
    val w = sim.width; val h = sim.height
    if (w <= 0f || h <= 0f) return
    // Ground heat-glow (single per-frame Brush, mirrors the web — additive).
    val gh = h * 0.32f
    val a = 0.14f
    drawRect(
        brush = Brush.verticalGradient(
            colorStops = arrayOf(
                0.0f to Color(120 / 255f, 20 / 255f, 5 / 255f, 0f),
                0.5f to Color(170 / 255f, 28 / 255f, 6 / 255f, a * 0.5f),
                1.0f to Color(226 / 255f, 60 / 255f, 10 / 255f, a),
            ),
            startY = h - gh,
            endY = h,
        ),
        topLeft = Offset(0f, h - gh),
        size = Size(w, gh),
        blendMode = FIELD_BLEND,
    )
    val last = RAMP.size - 1
    for (p in sim.alivePool) {
        if (!p.alive) continue
        val idx = ((1f - p.life) * last).roundToInt().coerceIn(0, last)
        val spr = if (p.spark) sprites[0] else sprites[idx]
        val al = ((if (p.spark) 0.75f else 0.4f) * p.life).coerceIn(0f, 1f)
        // Faint big glow (cheap bloom) …
        val grr = p.r * (if (p.spark) 4f else 4.2f)
        if (grr > 0f) {
            val d = (grr * 2f).roundToInt()
            drawImage(
                image = spr, srcOffset = IntOffset.Zero, srcSize = IntSize(SPRITE_PX, SPRITE_PX),
                dstOffset = IntOffset((p.x - grr).roundToInt(), (p.y - grr).roundToInt()),
                dstSize = IntSize(d, d), alpha = al * 0.22f, blendMode = FIELD_BLEND,
            )
        }
        // … + bright core.
        val cr = p.r * (if (p.spark) 1.6f else 1.9f)
        if (cr > 0f) {
            val d = (cr * 2f).roundToInt()
            drawImage(
                image = spr, srcOffset = IntOffset.Zero, srcSize = IntSize(SPRITE_PX, SPRITE_PX),
                dstOffset = IntOffset((p.x - cr).roundToInt(), (p.y - cr).roundToInt()),
                dstSize = IntSize(d, d), alpha = al, blendMode = FIELD_BLEND,
            )
        }
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
    matrixPaint: android.graphics.Paint,
    nowMs: Double,
) {
    when (sim) {
        is StarSim -> drawStars(sim, sprites.star, nowMs)
        is EmberSim -> drawEmbers(sim, sprites.ember, nowMs)
        is MatrixSim -> drawMatrix(sim, matrixPaint, nowMs)
    }
}
