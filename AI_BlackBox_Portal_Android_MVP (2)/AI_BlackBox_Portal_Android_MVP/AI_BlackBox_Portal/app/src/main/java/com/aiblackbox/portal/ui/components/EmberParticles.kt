package com.aiblackbox.portal.ui.components

// =============================================================================
// EmberParticles — the background particle-FIELD overlay shown behind the chat
// and the generation screens. Historically an ember-only effect; it is now the
// mount point for the switchable 3-mode field (Rising Stars / Embers / Matrix,
// see ParticleField.kt). This file owns only:
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
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
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
    // Pre-bake the sprite atlas ONCE per density (never in the hot loop).
    val sprites = remember(density) { buildFieldSprites(density) }
    // One reusable monospace paint for the Matrix field (text size set per draw).
    val matrixPaint = remember {
        android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG).apply {
            typeface = android.graphics.Typeface.MONOSPACE
        }
    }
    // Fresh sim per FIELD mode → switching modes re-inits cleanly (no residue).
    val sim = remember(particleMode) { newFieldSim(particleMode) }

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
    LaunchedEffect(running, sim) {
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

    // Soft fade in/out so the field doesn't pop on/off.
    val alpha by animateFloatAsState(
        targetValue = if (effectiveActive) 1f else 0f,
        animationSpec = tween(DurationSlow),
        label = "fieldAlpha",
    )

    Canvas(
        modifier = modifier
            .fillMaxSize()
            .graphicsLayer { this.alpha = alpha },
    ) {
        // Keep the sim sized to the canvas (spawns once; cheap no-op after).
        sim.resize(size.width, size.height, scale, density.density)
        // Reading the frame clock HERE invalidates only the DRAW phase (never
        // recomposition) each animation frame.
        val nowMs = frame.longValue / 1_000_000.0
        drawParticleField(sim, sprites, matrixPaint, nowMs)
    }
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
