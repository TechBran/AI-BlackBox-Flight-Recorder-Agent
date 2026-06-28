package com.aiblackbox.portal.ui.components

// =============================================================================
// EmberParticles — cinematic ember overlay shown while the AI is generating.
//
// Faithful port of the website ember system:
//   Apps/landing-page/app.js:191-518  (initCinematicParticles('embers'))
//
// The website renders a fixed full-screen <canvas> of 120 rising embers across
// 3 depth layers, with weighted-random fire colors, gentle multi-sine
// turbulence + flicker, short trails and additive ("lighter") glow. This port
// keeps the EXACT constants and physics; the only behaviour dropped is the
// reference's mouse-interaction term (there is no mouse on Android).
//
// Architecture:
//   • EmberSimulation / Particle — UI-FREE plain Kotlin (no Compose imports) so
//     the physics is unit-testable on the JVM (see EmberSimulationTest).
//   • buildGlowSprite — pre-bakes one radial-glow ImageBitmap per palette color
//     ONCE, so the per-frame draw never allocates a Shader.
//   • EmberOverlay / EmberBackdrop — Compose layers that drive the sim with a
//     battery-safe withFrameNanos loop that stops once drained.
// =============================================================================

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.drawscope.CanvasDrawScope
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.LayoutDirection
import com.aiblackbox.portal.ui.theme.DurationSlow
import com.aiblackbox.portal.ui.theme.EmberColors
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

// =============================================================================
// EmberMode — the 3 persisted backdrop modes + the CompositionLocal that carries
// the active mode down to EmberOverlay. Provided ONCE at the activity root from
// the persisted setting (default ALWAYS); EmberOverlay reads it to derive the
// effective-active from the screen's "is generating" flag:
//   OFF        → never show (drain + stop)
//   GENERATING → follow the passed `active` flag
//   ALWAYS     → always show
// =============================================================================
object EmberMode { const val OFF = "off"; const val GENERATING = "generating"; const val ALWAYS = "always" }

// Provided once at the activity root from the persisted setting; read by EmberOverlay.
val LocalEmberMode = androidx.compose.runtime.staticCompositionLocalOf { EmberMode.ALWAYS }

// -----------------------------------------------------------------------------
// Tunables — based on the website `configs.embers`, with the particle COUNT
// DOUBLED and sizes/glow BUMPED for visibility on a high-DPI phone (the website's
// CSS px are used here as physical px, so unscaled embers read small). These are
// the dials for Brandon's on-device tuning; further LocalDensity scaling of
// sizes+velocities is the remaining knob if they should be bigger still.
// -----------------------------------------------------------------------------
private val LAYER_COUNT = intArrayOf(80, 100, 60)          // far, mid, foreground (2x website)
private val LAYER_SPEED = floatArrayOf(0.3f, 0.5f, 0.8f)
private val SIZE_MIN = floatArrayOf(0.8f, 1.5f, 2.2f)      // ~1.5x website for high-DPI visibility
private val SIZE_MAX = floatArrayOf(1.6f, 3f, 4.5f)
private val LAYER_OPACITY = floatArrayOf(0.25f, 0.4f, 0.7f)
private val COLOR_WEIGHTS = doubleArrayOf(0.3, 0.3, 0.2, 0.15, 0.05)
private const val TOTAL_PARTICLES = 240                     // 80 + 100 + 60 (doubled)
private const val GLOW_INTENSITY = 14f                      // glow radius = size * 14 (bumped)
private const val TURBULENCE = 0.6
private const val RISE_SPEED = 0.8
private const val FLICKER_SPEED = 0.015
private const val TRAIL_LENGTH = 2
private const val DRAIN_MAX_MS = 650.0   // cap the post-generation drain (battery parity with web)

// =============================================================================
// Particle — one ember. Plain Kotlin (no Compose), mirrors the website's
// Particle class (constructor → reset, update(time), and the field set the
// renderer reads). `colorIndex` indexes into the EmberColors palette.
// =============================================================================
class Particle(val layerIndex: Int) {
    var x = 0f
    var y = 0f
    var size = 0f
    var baseSize = 0f
    var colorIndex = 0
    var vx = 0f
    var vy = 0f
    var baseVy = 0f
    var opacity = 0f
    var baseOpacity = 0f
    var dead = false

    // Organic-motion phases (doubles — multiplied by the ms time base).
    private var oscillationOffset = 0.0
    private var oscillationSpeed = 0.0
    private var oscillationAmplitude = 0.0
    private var flickerOffset = 0.0
    private var flickerSpeedJitter = 0.0

    // Trail ring buffer (newest at index 0), up to TRAIL_LENGTH points.
    val trailX = FloatArray(TRAIL_LENGTH)
    val trailY = FloatArray(TRAIL_LENGTH)
    val trailSize = FloatArray(TRAIL_LENGTH)
    val trailOpacity = FloatArray(TRAIL_LENGTH)
    var trailLen = 0
        private set

    /** Spawn at the bottom with fresh random properties (website Particle.reset). */
    fun reset(width: Float, height: Float) {
        x = (Math.random() * width).toFloat()
        y = (height + Math.random() * 100).toFloat()
        val sMin = SIZE_MIN[layerIndex]
        val sMax = SIZE_MAX[layerIndex]
        size = (sMin + Math.random() * (sMax - sMin)).toFloat()
        baseSize = size
        colorIndex = pickColorIndex()

        val speed = LAYER_SPEED[layerIndex]
        vx = ((Math.random() - 0.5) * 2.0 * speed).toFloat()
        vy = (-(0.5 + Math.random() * 0.5) * RISE_SPEED * speed).toFloat()
        baseVy = vy

        oscillationOffset = Math.random() * Math.PI * 2
        oscillationSpeed = 0.005 + Math.random() * 0.008
        oscillationAmplitude = 5 + Math.random() * 10

        flickerOffset = Math.random() * Math.PI * 2
        flickerSpeedJitter = FLICKER_SPEED * (0.8 + Math.random() * 0.4)

        opacity = LAYER_OPACITY[layerIndex]
        baseOpacity = LAYER_OPACITY[layerIndex]

        trailLen = 0
        dead = false
    }

    /**
     * Advance one frame. [timeMs] is a millis-like time base (the website uses
     * performance.now()). When the ember leaves the field: if [active] it
     * respawns at the bottom; otherwise it is marked [dead] (drain to a stop).
     */
    fun update(timeMs: Double, width: Float, height: Float, active: Boolean) {
        // Revive drained embers when generation restarts; stay parked otherwise.
        if (dead) {
            if (active) reset(width, height) else return
        }

        // Very gentle turbulence / wind (multi-sine, no repeat).
        val turbX = (sin(timeMs * 0.0003 + oscillationOffset) * TURBULENCE * 0.3).toFloat()
        val turbY = (cos(timeMs * 0.0004 + oscillationOffset) * TURBULENCE * 0.15).toFloat()
        val oscillation =
            (sin(timeMs * oscillationSpeed + oscillationOffset) * oscillationAmplitude * 0.002).toFloat()

        // Smooth horizontal easing; vertical rise from baseVy.
        vx += turbX * 0.005f + oscillation - vx * 0.02f
        vy = baseVy + turbY * 0.005f
        x += vx
        y += vy

        // Record trail (captures pre-flicker size/opacity, like the website).
        pushTrail(x, y, size, opacity)

        // Multi-sine flicker — eases opacity & size toward a breathing target.
        val flicker1 = sin(timeMs * flickerSpeedJitter + flickerOffset)
        val flicker2 = sin(timeMs * flickerSpeedJitter * 0.7 + flickerOffset * 1.3)
        val flicker = ((flicker1 + flicker2 * 0.5) / 1.5).toFloat()

        val targetOpacity = baseOpacity * (0.7f + flicker * 0.3f)
        opacity += (targetOpacity - opacity) * 0.05f
        val targetSize = baseSize * (0.9f + flicker * 0.1f)
        size += (targetSize - size) * 0.05f

        // Fade out near the top.
        if (y < height * 0.2f) {
            val life = y / (height * 0.2f)
            opacity *= life
        }

        // Off-screen: recycle while active, else die.
        if (y < -50f || x < -50f || x > width + 50f) {
            if (active) reset(width, height) else dead = true
        }
    }

    private fun pushTrail(px: Float, py: Float, ps: Float, po: Float) {
        // Shift existing points one slot toward the tail (cap at TRAIL_LENGTH).
        var i = minOf(trailLen, TRAIL_LENGTH - 1)
        while (i > 0) {
            trailX[i] = trailX[i - 1]
            trailY[i] = trailY[i - 1]
            trailSize[i] = trailSize[i - 1]
            trailOpacity[i] = trailOpacity[i - 1]
            i--
        }
        trailX[0] = px
        trailY[0] = py
        trailSize[0] = ps
        trailOpacity[0] = po
        if (trailLen < TRAIL_LENGTH) trailLen++
    }

    private fun pickColorIndex(): Int {
        val r = Math.random()
        var cumulative = 0.0
        for (i in COLOR_WEIGHTS.indices) {
            cumulative += COLOR_WEIGHTS[i]
            if (r < cumulative) return i
        }
        return 0
    }
}

// =============================================================================
// EmberSimulation — owns the 120 particles and the field dimensions. UI-free
// and deterministic-in-shape (positions use Math.random, but counts / velocity
// sign / reset behaviour are invariant). Drive it with update(); render by
// reading the particle list.
// =============================================================================
class EmberSimulation {
    private var width = 0f
    private var height = 0f
    private val _particles = ArrayList<Particle>(TOTAL_PARTICLES)

    /** Read-only view for the renderer and tests. */
    val particles: List<Particle> get() = _particles

    /**
     * Set the field size; lazily spawn the 120 particles on the first valid
     * size, staggering their initial Y across 1.5× the field so they don't all
     * rise in lockstep (website "Stagger initial positions").
     */
    fun resize(newWidth: Float, newHeight: Float) {
        if (newWidth <= 0f || newHeight <= 0f) return
        if (newWidth == width && newHeight == height && _particles.isNotEmpty()) return
        width = newWidth
        height = newHeight
        if (_particles.isEmpty()) spawnAll()
    }

    private fun spawnAll() {
        _particles.clear()
        for (layer in LAYER_COUNT.indices) {
            repeat(LAYER_COUNT[layer]) {
                val p = Particle(layer)
                p.reset(width, height)
                // Stagger initial Y across 1.5× the field height.
                p.y = (Math.random() * height * 1.5).toFloat()
                _particles.add(p)
            }
        }
    }

    /** Advance every particle one frame. [timeNanos] is the frame clock. */
    fun update(timeNanos: Long, active: Boolean) {
        if (width <= 0f || height <= 0f) return
        val timeMs = timeNanos / 1_000_000.0
        for (p in _particles) p.update(timeMs, width, height, active)
    }

    /** True once no live (non-dead) particles remain — the loop can stop. */
    fun isDrained(): Boolean {
        if (_particles.isEmpty()) return true
        for (p in _particles) if (!p.dead) return false
        return true
    }

    /** Visit every live (non-dead) particle, back layer first (depth order). */
    fun forEachLive(action: (Particle) -> Unit) {
        for (p in _particles) if (!p.dead) action(p)
    }

    /** Force every particle dead — caps the post-generation drain to a deadline. */
    fun killAll() {
        for (p in _particles) p.dead = true
    }

    /** Re-stagger the field on a fresh activation after a full drain, so the next
     *  generation doesn't rise in lockstep (the 1.5x-height Y-stagger otherwise
     *  only happens at first spawn). No-op while any particle is still alive. */
    fun rearm() {
        if (width <= 0f || height <= 0f || _particles.isEmpty()) return
        if (!isDrained()) return
        for (p in _particles) {
            p.reset(width, height)
            p.y = (Math.random() * height * 1.5).toFloat()
        }
    }
}

// =============================================================================
// Glow sprite pre-baking — one radial-glow ImageBitmap per palette color, built
// ONCE so the hot draw loop never allocates a RadialGradient shader. Baked at
// full opacity (stops 0.8 → 0.4 → 0.1 → 0, matching the website gradient); the
// per-particle alpha is modulated at draw time via drawImage(alpha = ...).
// =============================================================================
private const val GLOW_SPRITE_PX = 64

private fun buildGlowSprite(color: Color, sizePx: Int, density: Density): ImageBitmap {
    val bitmap = ImageBitmap(sizePx, sizePx)
    // Fully-qualified: the bitmap-backed Canvas factory, distinct from the
    // foundation @Composable Canvas used in EmberOverlay.
    val canvas = androidx.compose.ui.graphics.Canvas(bitmap)
    val drawScope = CanvasDrawScope()
    val radius = sizePx / 2f
    val center = Offset(radius, radius)
    drawScope.draw(density, LayoutDirection.Ltr, canvas, Size(sizePx.toFloat(), sizePx.toFloat())) {
        drawCircle(
            brush = Brush.radialGradient(
                colorStops = arrayOf(
                    0.0f to color.copy(alpha = 0.8f),
                    0.1f to color.copy(alpha = 0.4f),
                    0.4f to color.copy(alpha = 0.1f),
                    1.0f to color.copy(alpha = 0f),
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

// Additive blend — mirrors the website's globalCompositeOperation = 'lighter'.
// BlendMode.Plus can be spotty on the hardware-accelerated Canvas before API 28
// (and we wrap the Canvas in an alpha graphicsLayer, which forces an offscreen
// buffer), so fall back to SrcOver there — over pure black it degrades gracefully
// at runtime, no rebuild needed.
private val EMBER_BLEND =
    if (android.os.Build.VERSION.SDK_INT < 28) BlendMode.SrcOver else BlendMode.Plus

// =============================================================================
// EmberOverlay — the drawing layer. A pure, non-interactive background: it never
// intercepts touch. Runs a battery-safe frame loop that starts when [active]
// goes true and stops once the field has drained after [active] goes false.
// =============================================================================
@Composable
fun EmberOverlay(active: Boolean, modifier: Modifier = Modifier) {
    // Apply the persisted backdrop mode: OFF forces off, ALWAYS forces on, and
    // GENERATING follows the screen's `active` flag. Everything below drives off
    // effectiveActive so the boolean call sites keep meaning "is generating".
    val mode = LocalEmberMode.current
    val effectiveActive = when (mode) {
        EmberMode.OFF -> false
        EmberMode.ALWAYS -> true
        else -> active            // "generating": follow the screen's flag
    }
    val sim = remember { EmberSimulation() }
    val density = LocalDensity.current
    // Pre-bake the 5 glow sprites ONCE (one per palette color).
    val sprites = remember(density) {
        EmberColors.map { buildGlowSprite(it, GLOW_SPRITE_PX, density) }
    }

    // Battery-safe frame loop. `active` is read via rememberUpdatedState so the
    // loop (keyed on `running`, not `active`) sees the CURRENT value — otherwise it
    // would capture active=true at launch and never drain or stop (silent 60fps leak).
    val currentActive by rememberUpdatedState(effectiveActive)
    var running by remember { mutableStateOf(false) }
    val frame = remember { mutableLongStateOf(0L) }
    val drainStart = remember { longArrayOf(0L) } // frame nanos the drain began (0 = not draining)
    LaunchedEffect(effectiveActive) { if (effectiveActive) { sim.rearm(); running = true } }
    LaunchedEffect(running) {
        while (running) {
            withFrameNanos { t ->
                // Bound the drain: once generation ends, force-cull after DRAIN_MAX_MS
                // instead of running the ~1-2 min a slow bottom particle needs to clear.
                if (!currentActive) {
                    if (drainStart[0] == 0L) drainStart[0] = t
                    else if ((t - drainStart[0]) / 1_000_000.0 > DRAIN_MAX_MS) sim.killAll()
                } else {
                    drainStart[0] = 0L
                }
                sim.update(t, currentActive)
                frame.longValue = t
            }
            if (!currentActive && sim.isDrained()) running = false
        }
    }

    // Soft fade in/out so embers don't pop on/off.
    val alpha by animateFloatAsState(
        targetValue = if (effectiveActive) 1f else 0f,
        animationSpec = tween(DurationSlow),
        label = "emberAlpha",
    )

    Canvas(
        modifier = modifier
            .fillMaxSize()
            .graphicsLayer { this.alpha = alpha },
    ) {
        // Keep the sim sized to the canvas (spawns once, cheap no-op after).
        sim.resize(size.width, size.height)
        // Reading the frame clock here invalidates only the DRAW phase (not
        // recomposition) each animation frame.
        @Suppress("UNUSED_VARIABLE")
        val tick = frame.longValue

        sim.forEachLive { p ->
            val a = p.opacity.coerceIn(0f, 1f)
            if (a <= 0f) return@forEachLive

            // Trail (faded, behind the glow).
            for (i in 0 until p.trailLen) {
                val f = 1f - i.toFloat() / TRAIL_LENGTH
                val tSize = p.trailSize[i] * f
                val tAlpha = (p.trailOpacity[i] * f * 0.5f).coerceIn(0f, 1f)
                if (tSize > 0f && tAlpha > 0f) {
                    drawCircle(
                        color = EmberColors[p.colorIndex].copy(alpha = tAlpha),
                        radius = tSize,
                        center = Offset(p.trailX[i], p.trailY[i]),
                        blendMode = EMBER_BLEND,
                    )
                }
            }

            // Outer radial glow (pre-baked sprite scaled to size * glowIntensity).
            val glowR = p.size * GLOW_INTENSITY
            val diameter = (glowR * 2f).roundToInt()
            if (diameter > 0) {
                drawImage(
                    image = sprites[p.colorIndex],
                    srcOffset = IntOffset.Zero,
                    srcSize = IntSize(GLOW_SPRITE_PX, GLOW_SPRITE_PX),
                    dstOffset = IntOffset(
                        (p.x - glowR).roundToInt(),
                        (p.y - glowR).roundToInt(),
                    ),
                    dstSize = IntSize(diameter, diameter),
                    alpha = a,
                    blendMode = EMBER_BLEND,
                )
            }

            // Bright white-hot core.
            if (p.size > 0f) {
                drawCircle(
                    color = Color.White.copy(alpha = a),
                    radius = p.size,
                    center = Offset(p.x, p.y),
                    blendMode = EMBER_BLEND,
                )
            }
        }
    }
}

// =============================================================================
// EmberBackdrop — convenience wrapper that puts the ember overlay BEHIND its
// content. Use for screens whose root is a scrolling Column: the overlay is a
// SIBLING (never inside the scroll), so it stays full-screen and fixed.
// NOTE: the incoming `modifier` is applied to the wrapper Box (not the content),
// so layout/padding/test-tags meant for the content belong INSIDE `content`.
// =============================================================================
@Composable
fun EmberBackdrop(
    active: Boolean,
    modifier: Modifier = Modifier,
    content: @Composable BoxScope.() -> Unit,
) {
    Box(modifier.fillMaxSize()) {
        EmberOverlay(active = active, modifier = Modifier.matchParentSize())
        content()
    }
}
