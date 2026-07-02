package com.aiblackbox.portal.overlay

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.ui.draw.clip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/**
 * Glow mode states for the holographic orb bubble.
 * Each state defines an inner color and an outer (diffused) color.
 */
enum class GlowMode(
    val innerColor: Color,
    val outerColor: Color
) {
    RED_SPEAKING(
        innerColor = Color(0xFFFF4444),
        outerColor = Color(0x66FF4444)
    ),
    BLUE_AI(
        innerColor = Color(0xFF1E90FF),
        outerColor = Color(0x661E90FF)
    ),
    IDLE_CONNECTED(
        innerColor = Color(0xFF00D4FF),
        outerColor = Color(0xFF8A2BE2)
    ),
    IDLE_AMBIENT(
        innerColor = Color(0x8000D4FF),
        outerColor = Color(0x808A2BE2)
    )
}

// Brand colors
private val BbxBlack = Color(0xFF0E0E10)
private val BbxDark = Color(0xFF17181B)
private val BbxRed = Color(0xFFE10600)

/**
 * XR holographic orb bubble composable.
 *
 * Renders three concentric layers inside a 160dp Box:
 * 1. Outer glow ring (160dp) - diffused radial gradient
 * 2. Inner glow ring (130dp) - tighter radial gradient
 * 3. Core orb (96dp) - dark radial gradient with accent border and status text
 *
 * Tapping the bubble toggles the expanded state via [OverlayBridge.toggleExpanded].
 */
@Composable
fun XrBubbleContent(state: OverlayBridge.OverlayState) {
    // Derive glow mode from overlay state
    val glowMode = when {
        state.isRecording && !state.isAISpeaking -> GlowMode.RED_SPEAKING
        state.isAISpeaking -> GlowMode.BLUE_AI
        state.isConnected -> GlowMode.IDLE_CONNECTED
        else -> GlowMode.IDLE_AMBIENT
    }

    val isActive = glowMode == GlowMode.RED_SPEAKING || glowMode == GlowMode.BLUE_AI

    // --- Static scale animations ---
    val innerGlowScale by animateFloatAsState(
        targetValue = if (isActive) 1.15f else 1.0f,
        animationSpec = tween(
            durationMillis = 800,
            easing = FastOutSlowInEasing
        ),
        label = "innerGlowScale"
    )

    val outerGlowScale by animateFloatAsState(
        targetValue = if (isActive) 1.3f else 1.0f,
        animationSpec = tween(
            durationMillis = 1200,
            easing = LinearOutSlowInEasing
        ),
        label = "outerGlowScale"
    )

    val glowAlpha by animateFloatAsState(
        targetValue = when {
            isActive -> 1.0f
            glowMode == GlowMode.IDLE_CONNECTED -> 0.6f
            else -> 0.5f
        },
        animationSpec = tween(durationMillis = 1000),
        label = "glowAlpha"
    )

    // --- Pulsing animations (only applied when active) ---
    val infiniteTransition = rememberInfiniteTransition(label = "glowPulse")

    val innerPulse by infiniteTransition.animateFloat(
        initialValue = 1.0f,
        targetValue = 1.15f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1000),
            repeatMode = RepeatMode.Reverse
        ),
        label = "innerPulse"
    )

    val outerPulse by infiniteTransition.animateFloat(
        initialValue = 1.0f,
        targetValue = 1.3f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1000),
            repeatMode = RepeatMode.Reverse
        ),
        label = "outerPulse"
    )

    // Combine static scale with pulse (pulse only when active)
    val finalInnerScale = if (isActive) innerGlowScale * innerPulse else innerGlowScale
    val finalOuterScale = if (isActive) outerGlowScale * outerPulse else outerGlowScale

    // --- Tap spring animation ---
    val coroutineScope = rememberCoroutineScope()
    val tapScale = remember { Animatable(1.0f) }

    // --- Status text properties ---
    val statusText: String
    val statusColor: Color
    when {
        // (M6.3) A remote-control session takes visual priority on the collapsed orb, so the
        // user always sees (and can expand to STOP) that the AI is controlling the headset.
        state.controlSessionActive -> {
            statusText = "CTRL"
            statusColor = BbxRed
        }
        state.isAISpeaking -> {
            statusText = "AI"
            statusColor = Color(0xFF1E90FF)
        }
        state.isRecording -> {
            statusText = "REC"
            statusColor = Color(0xFFFF4444)
        }
        state.isConnected -> {
            statusText = "ON"
            statusColor = Color(0xFF00D4FF)
        }
        else -> {
            statusText = "BBX"
            statusColor = Color(0xFF666666)
        }
    }

    Box(
        contentAlignment = Alignment.Center,
        modifier = Modifier
            .size(160.dp)
            .clip(CircleShape)
            .background(BbxBlack)
            .graphicsLayer {
                scaleX = tapScale.value
                scaleY = tapScale.value
            }
            .pointerInput(Unit) {
                detectTapGestures {
                    OverlayBridge.toggleExpanded()
                    coroutineScope.launch {
                        tapScale.animateTo(
                            targetValue = 1.1f,
                            animationSpec = spring(
                                dampingRatio = 0.4f,
                                stiffness = 400f
                            )
                        )
                        tapScale.animateTo(
                            targetValue = 1.0f,
                            animationSpec = spring(
                                dampingRatio = 0.4f,
                                stiffness = 400f
                            )
                        )
                    }
                }
            }
    ) {
        // Layer 1: Outer glow ring (160dp)
        Canvas(
            modifier = Modifier
                .size(160.dp)
                .align(Alignment.Center)
                .graphicsLayer {
                    scaleX = finalOuterScale
                    scaleY = finalOuterScale
                    alpha = glowAlpha
                }
        ) {
            drawGlowCircle(
                color = glowMode.outerColor,
                radiusFraction = 0.5f
            )
        }

        // Layer 2: Inner glow ring (130dp)
        Canvas(
            modifier = Modifier
                .size(130.dp)
                .align(Alignment.Center)
                .graphicsLayer {
                    scaleX = finalInnerScale
                    scaleY = finalInnerScale
                    alpha = glowAlpha
                }
        ) {
            drawGlowCircle(
                color = glowMode.innerColor,
                radiusFraction = 0.5f
            )
        }

        // Layer 3: Core orb (96dp)
        Canvas(
            modifier = Modifier
                .size(96.dp)
                .align(Alignment.Center)
        ) {
            val center = Offset(size.width / 2f, size.height / 2f)
            val radius = size.minDimension / 2f

            // Dark radial gradient fill
            drawCircle(
                brush = Brush.radialGradient(
                    colors = listOf(BbxBlack, BbxDark),
                    center = center,
                    radius = radius
                ),
                radius = radius,
                center = center
            )

            // Thin 2dp accent border ring
            val borderWidthPx = 2.dp.toPx()
            drawCircle(
                color = BbxRed,
                radius = radius - borderWidthPx / 2f,
                center = center,
                style = androidx.compose.ui.graphics.drawscope.Stroke(
                    width = borderWidthPx
                )
            )
        }

        // Status text centered on core orb
        Text(
            text = statusText,
            color = statusColor,
            fontSize = 16.sp,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.align(Alignment.Center)
        )
    }
}

/**
 * Draws a radial gradient circle that fades from [color] at the center to transparent at the edge.
 *
 * @param color The glow color at the center of the gradient.
 * @param radiusFraction Fraction of the minimum dimension to use as the gradient radius.
 */
private fun DrawScope.drawGlowCircle(
    color: Color,
    radiusFraction: Float
) {
    val center = Offset(size.width / 2f, size.height / 2f)
    val radius = size.minDimension * radiusFraction

    drawCircle(
        brush = Brush.radialGradient(
            colors = listOf(color, Color.Transparent),
            center = center,
            radius = radius
        ),
        radius = radius,
        center = center
    )
}
