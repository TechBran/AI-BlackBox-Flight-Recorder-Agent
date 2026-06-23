package com.aiblackbox.portal.ui.voice

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.SolidGreen
import kotlin.math.PI
import kotlin.math.sin

// Per-speaker gain: the mic is close + loud, the model's stream is quieter, so
// the AI side needs a bigger lift to read on the ribbon. Tuning knobs.
private const val USER_GAIN = 2.2f         // mic feel — unchanged
private const val AI_GAIN = 4.5f           // model output is quieter; lift it to read
private const val IDLE_LEVEL = 0.08f       // gentle breathing baseline when silent
private val AI_TEAL = Color(0xFF1FB5A6)

/**
 * Flowing "ribbon" waveform: three layered translucent sine paths whose height
 * tracks [amplitude] (0f..1f) and whose palette tracks [speaker]. Phase animates
 * continuously so the ribbon always flows; amplitude is eased so loud transients
 * glide instead of snapping.
 */
@Composable
fun VoiceWaveform(
    amplitude: Float,
    speaker: WaveSpeaker,
    modifier: Modifier = Modifier,
    height: Dp = 140.dp,
    // Extra display lift on top of the per-speaker gain. Default 1f leaves the
    // voice screen exactly as tuned; the small composer ribbon turns this up so
    // speech-level RMS (~0.05-0.15) reads clearly in its shorter height.
    sensitivity: Float = 1f,
    // Optional gradient override (c1 to c2). When non-null it replaces the
    // per-speaker palette — e.g. the audio player passes its red pair so the
    // ribbon reads red regardless of [speaker]. Default null = existing callers
    // (voice screen, composer mic) are unchanged.
    overrideColors: Pair<Color, Color>? = null,
    // When true, pause the continuous phase animation while the ribbon is at
    // rest (amplitude ~0) so idle bars in a list stop repainting at 60fps.
    // Default false keeps the always-flowing behavior for the voice/mic ribbons.
    pauseWhenIdle: Boolean = false,
) {
    val gain = when (speaker) {
        WaveSpeaker.AI -> AI_GAIN
        else -> USER_GAIN
    }
    val eased by animateFloatAsState(
        targetValue = (amplitude * gain * sensitivity).coerceIn(0f, 1f),
        // Easing: fluid but responsive — tracks the audio without trailing or twitching.
        animationSpec = tween(70),
        label = "amp",
    )

    val phaseState = rememberInfiniteTransition(label = "wave").animateFloat(
        initialValue = 0f,
        targetValue = (2f * PI).toFloat(),
        animationSpec = infiniteRepeatable(tween(2200, easing = LinearEasing), RepeatMode.Restart),
        label = "phase",
    )

    val (c1, c2) = overrideColors ?: when (speaker) {
        WaveSpeaker.USER -> BbxAccent to BbxRed
        WaveSpeaker.AI -> SolidGreen to AI_TEAL
        WaveSpeaker.IDLE -> BbxDim to BbxDim
    }
    val color1 by animateColorAsState(c1, tween(400), label = "c1")
    val color2 by animateColorAsState(c2, tween(400), label = "c2")

    val level = if (speaker == WaveSpeaker.IDLE) IDLE_LEVEL else (IDLE_LEVEL + eased).coerceIn(0f, 1f)

    // When pauseWhenIdle is set (list TTS bars), stop reading the phase State
    // while at rest so the Canvas stops repainting every frame. IDLE breathing
    // and active motion still animate; default (false) is byte-for-byte unchanged.
    val animatePhase = !pauseWhenIdle || speaker == WaveSpeaker.IDLE || eased > 0.015f

    Canvas(modifier = modifier.fillMaxWidth().height(height)) {
        val phase = if (animatePhase) phaseState.value else 0f
        val brush = Brush.horizontalGradient(
            listOf(color1.copy(alpha = 0f), color2, color1.copy(alpha = 0f))
        )
        drawRibbon(level * 0.9f, 0.9f, 1.6f, phase, brush)
        drawRibbon(level * 0.6f, 0.5f, 2.4f, phase + 1.1f, brush)
        drawRibbon(level * 0.35f, 0.3f, 3.3f, phase + 2.3f, brush)
    }
}

private fun DrawScope.drawRibbon(
    heightFraction: Float,
    alpha: Float,
    freq: Float,
    phase: Float,
    brush: Brush,
) {
    val midY = size.height / 2f
    val amp = (size.height / 2f) * heightFraction
    val path = Path().apply {
        moveTo(0f, midY)
        val steps = 64
        for (i in 0..steps) {
            val x = size.width * i / steps
            val t = i.toFloat() / steps
            val envelope = sin(PI * t).toFloat()  // taper ends to the center line
            val y = midY + sin(t * freq * 2f * PI.toFloat() + phase) * amp * envelope
            lineTo(x, y)
        }
    }
    drawPath(path = path, brush = brush, alpha = alpha, style = Stroke(width = 4.dp.toPx()))
}
