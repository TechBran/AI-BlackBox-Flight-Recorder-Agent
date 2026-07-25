package com.aiblackbox.portal.ui.components

// =============================================================================
// EmberParticles — the background particle-FIELD overlay shown behind the chat
// and the generation screens. Historically an ember-only effect; it is now the
// mount point for the switchable field catalogue (Rising Stars / Embers / Matrix
// / Fireflies / …, registered in FieldRegistry.kt, engine in ParticleField.kt).
// This file owns only:
//   • EmberMode / LocalEmberMode  — the OFF/GENERATING/ALWAYS VISIBILITY setting
//     (unchanged; the persisted `ember_mode` preference).
//   • EmberOverlay / EmberBackdrop — the Compose layers + the battery-safe frame
//     loop that drives the selected field. The name is kept for its many call
//     sites (ChatScreen + the six generation screens); it is field-agnostic.
//
// The FIELD look is orthogonal and comes from LocalParticleMode (default STARS).
// The activation TRIGGER is unchanged: call sites still pass `active` = "this
// screen is generating"; EmberMode maps that to effective visibility.
// =============================================================================

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import com.aiblackbox.portal.ui.theme.DurationSlow

// =============================================================================
// EmberMode — the 3 persisted VISIBILITY modes + the CompositionLocal carrying
// the active mode down to EmberOverlay. Provided ONCE at the activity root from
// the persisted `ember_mode` setting (default ALWAYS); EmberOverlay derives the
// effective-active from the screen's "is generating" flag:
//   OFF        → never show
//   GENERATING → follow the passed `active` flag
//   ALWAYS     → always show
// =============================================================================
object EmberMode { const val OFF = "off"; const val GENERATING = "generating"; const val ALWAYS = "always" }

// Provided once at the activity root from the persisted setting; read by EmberOverlay.
val LocalEmberMode = androidx.compose.runtime.staticCompositionLocalOf { EmberMode.ALWAYS }

// =============================================================================
// Field suppression — "something opaque is covering the field, stop drawing it".
//
// The particle field is a BACKDROP: when a full-bleed opaque surface (the system
// settings sheet, a dialog) is on top of it, every frame it renders is invisible
// AND costs GPU + battery. The overlay can't see those surfaces (they compose in
// a different subtree), so they raise a hand here instead: a shared counter any
// covering surface increments while it is on screen, which EmberOverlay reads to
// park its frame loop. A COUNT, not a flag, so overlapping surfaces (sheet →
// dialog) can't un-suppress each other on the way out.
// =============================================================================
@Stable
class FieldSuppression {
    var count by mutableIntStateOf(0)
        private set
    val suppressed: Boolean get() = count > 0
    fun acquire() { count++ }
    fun release() { if (count > 0) count-- }
}

/** One process-wide instance by default, so a covering surface never has to be a
 *  descendant of a provider to be heard (they aren't — sheets compose elsewhere). */
private val GlobalFieldSuppression = FieldSuppression()
val LocalFieldSuppressed = staticCompositionLocalOf { GlobalFieldSuppression }

/**
 * Call from any composable that fully covers the backdrop (sheets, dialogs): the
 * field's frame loop parks while it is composed and resumes on dispose.
 */
@Composable
fun SuppressParticleField() {
    val suppression = LocalFieldSuppressed.current
    DisposableEffect(suppression) {
        suppression.acquire()
        onDispose { suppression.release() }
    }
}

/** True when the OS "remove animations" / animator-scale-0 accessibility path is
 *  on — the particle field IS motion, so it is fully disabled under reduced
 *  motion (matches SignalLine + the web module's prefers-reduced-motion skip). */
private fun fieldReduceMotion(context: android.content.Context): Boolean = try {
    android.provider.Settings.Global.getFloat(
        context.contentResolver,
        android.provider.Settings.Global.ANIMATOR_DURATION_SCALE,
        1f,
    ) == 0f
} catch (_: Exception) {
    false
}

// =============================================================================
// EmberOverlay — the drawing layer. A pure, non-interactive background: it never
// intercepts touch. Runs a battery-safe frame loop that starts when the field
// becomes effectively-active and stops once the bounded drain elapses after it
// goes inactive (ALWAYS mode never drains → runs continuously by design).
// =============================================================================
@Composable
fun EmberOverlay(active: Boolean, modifier: Modifier = Modifier) {
    val visibility = LocalEmberMode.current
    val particleMode = ParticleMode.parse(LocalParticleMode.current)
    // How MUCH field: the intensity slider × the quality tier, as one quantized
    // count multiplier. Defaults (1.0 × High) resolve to exactly 1.0 → today's
    // counts. Quantized in ParticleTuning so a slider drag can't re-key the sim
    // on float noise; a real change rebuilds the sim (counts are baked at spawn).
    val countScale = ParticleTuning.countScale(
        LocalParticleIntensity.current,
        LocalParticleQuality.current,
    )
    val context = LocalContext.current
    // Reduced motion fully disables the field (all visibility modes).
    val reduceMotion = remember { fieldReduceMotion(context) }
    val effectiveActive = if (reduceMotion) false else when (visibility) {
        EmberMode.OFF -> false
        EmberMode.ALWAYS -> true
        else -> active            // "generating": follow the screen's flag
    }

    val density = LocalDensity.current
    val scale = density.density / FIELD_REFERENCE_DENSITY
    // The per-effect RESOURCE BAG: shared sprite atlas (baked lazily, once) plus
    // whatever private assets this effect bakes. Keyed on (density, effect) so a
    // look never inherits the previous one's assets and an effect that needs no
    // atlas (matrix) never pays to bake one. Never touched in the hot loop.
    val res = remember(density, particleMode) { FieldResources(density) }
    // Reusable draw-phase scratch (sprite paint + matrix glyph paint + dst rect).
    // PER-OVERLAY, deliberately: it holds a MUTABLE Paint/RectF and sharing it
    // between two overlays would let one frame's alpha leak into another's.
    val paints = remember { FieldPaints() }
    // Fresh sim per FIELD mode (and per count multiplier) → switching either
    // re-inits cleanly with no residue.
    val sim = remember(particleMode, countScale) { newFieldSim(particleMode, countScale) }

    // Battery-safe frame loop. effectiveActive is read via rememberUpdatedState so
    // the loop (keyed on `running`/`sim`, not the flag) sees the CURRENT value —
    // otherwise it would capture the launch value and never drain or stop.
    val currentActive by rememberUpdatedState(effectiveActive)
    var running by remember { mutableStateOf(false) }
    val frame = remember { mutableLongStateOf(0L) }
    val drainStart = remember { longArrayOf(0L) } // frame nanos the drain began (0 = not draining)
    val lastNanos = remember { longArrayOf(0L) }   // previous frame nanos (delta timing)

    // Start (or, on a mode switch, restart) the loop whenever the field becomes
    // effectively-active. Keyed on `sim` too so switching modes rearms the new sim.
    LaunchedEffect(effectiveActive, sim) {
        if (effectiveActive) { sim.rearm(); lastNanos[0] = 0L; drainStart[0] = 0L; running = true }
    }
    // Park the loop when the field cannot be SEEN. Two independent gates, both
    // pure battery (neither changes the look):
    //   • repeatOnLifecycle(STARTED) — withFrameNanos is Choreographer-driven and
    //     keeps firing while the activity is stopped/backgrounded.
    //   • LocalFieldSuppressed — an opaque sheet/dialog is covering the field.
    // On resume the loop picks up where it left off (lastNanos reset → dt 0, so
    // no accumulated-time jump).
    val lifecycleOwner = LocalLifecycleOwner.current
    val suppressed = LocalFieldSuppressed.current.suppressed
    LaunchedEffect(running, sim, suppressed, lifecycleOwner) {
        if (suppressed) return@LaunchedEffect
        lifecycleOwner.lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            lastNanos[0] = 0L
            while (running) {
                var stop = false
                withFrameNanos { t ->
                    val dt = if (lastNanos[0] == 0L) 0f
                        else ((t - lastNanos[0]).toDouble() / 1_000_000_000.0).coerceAtMost(0.05).toFloat()
                    lastNanos[0] = t
                    // Bound the drain: once inactive, keep animating through the fade,
                    // then force-stop at the deadline so the loop can't idle-spin.
                    if (!currentActive) {
                        if (drainStart[0] == 0L) drainStart[0] = t
                        else if ((t - drainStart[0]) / 1_000_000.0 > DRAIN_MAX_MS) stop = true
                    } else {
                        drainStart[0] = 0L
                    }
                    sim.update(t / 1_000_000.0, dt, currentActive)
                    frame.longValue = t
                }
                if (stop) running = false
            }
        }
    }

    // Soft fade in/out so the field doesn't pop on/off.
    val alpha by animateFloatAsState(
        targetValue = if (effectiveActive) 1f else 0f,
        animationSpec = tween(DurationSlow),
        label = "fieldAlpha",
    )

    // Spacer + drawWithCache is exactly what foundation's Canvas() is, minus the
    // trap: size-derived SETUP (sim.resize) belongs in the cache block, which runs
    // only when the size actually changes, so onDrawBehind stays a pure
    // read-and-draw instead of mutating sim state on every one of 120 frames/sec.
    Spacer(
        modifier = modifier
            .fillMaxSize()
            .graphicsLayer {
                this.alpha = alpha
                // The default (Auto) strategy promotes alpha < 1 to a full-screen
                // OFFSCREEN BUFFER, and additive blending against a transparent
                // buffer is a no-op — i.e. the field silently lost its additive
                // bloom for the whole fade. ModulateAlpha sets
                // hasOverlappingRendering=false so alpha multiplies per draw op.
                compositingStrategy = CompositingStrategy.ModulateAlpha
            }
            .drawWithCache {
                // Keep the sim sized to the canvas (spawns once; re-runs on size change).
                sim.resize(size.width, size.height, scale, density.density)
                onDrawBehind {
                    // Reading the frame clock HERE invalidates only the DRAW phase
                    // (never recomposition) each animation frame.
                    val nowMs = frame.longValue / 1_000_000.0
                    drawParticleField(sim, res, paints, nowMs)
                }
            },
    )
}

// =============================================================================
// EmberBackdrop — convenience wrapper that puts the field overlay BEHIND its
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
