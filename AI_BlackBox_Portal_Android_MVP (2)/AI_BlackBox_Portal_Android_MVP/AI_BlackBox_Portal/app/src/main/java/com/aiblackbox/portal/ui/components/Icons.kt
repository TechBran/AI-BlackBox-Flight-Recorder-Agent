package com.aiblackbox.portal.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.theme.Neutral500

/**
 * Microphone icon matching web Portal ctlMic SVG.
 * Microphone capsule + arc cradle + stem + base.
 */
@Composable
fun MicIcon(
    modifier: Modifier = Modifier.size(18.dp),
    color: Color = Neutral500,
    strokeWidth: Float = 2f
) {
    Canvas(modifier = modifier) {
        val w = size.width
        val h = size.height
        val s = w / 24f
        val stroke = Stroke(width = strokeWidth * s, cap = StrokeCap.Round, join = StrokeJoin.Round)

        // Microphone capsule (pill shape: rect 9,4 -> 15,12 with rounded top/bottom)
        val capsule = Path().apply {
            moveTo(9f * s, 7f * s)
            // Top arc
            arcTo(Rect(9f * s, 1f * s, 15f * s, 7f * s), 180f, 180f, false)
            // Right side down
            lineTo(15f * s, 11f * s)
            // Bottom arc
            arcTo(Rect(9f * s, 9f * s, 15f * s, 15f * s), 0f, 180f, false)
            // Left side up
            close()
        }
        drawPath(capsule, color, style = stroke)

        // Cradle arc (U-shape under capsule)
        val cradle = Path().apply {
            moveTo(19f * s, 10f * s)
            lineTo(19f * s, 12f * s)
            arcTo(Rect(5f * s, 5f * s, 19f * s, 19f * s), 0f, 180f, false)
            lineTo(5f * s, 10f * s)
        }
        drawPath(cradle, color, style = stroke)

        // Stem
        drawLine(color, Offset(12f * s, 19f * s), Offset(12f * s, 23f * s), strokeWidth = strokeWidth * s, cap = StrokeCap.Round)

        // Base
        drawLine(color, Offset(8f * s, 23f * s), Offset(16f * s, 23f * s), strokeWidth = strokeWidth * s, cap = StrokeCap.Round)
    }
}

/**
 * Speaker / volume icon matching web Portal ctlSpeak SVG.
 * Speaker cone + two sound-wave arcs.
 */
@Composable
fun SpeakerIcon(
    modifier: Modifier = Modifier.size(18.dp),
    color: Color = Neutral500,
    strokeWidth: Float = 2f
) {
    Canvas(modifier = modifier) {
        val w = size.width
        val h = size.height
        val s = w / 24f
        val stroke = Stroke(width = strokeWidth * s, cap = StrokeCap.Round, join = StrokeJoin.Round)

        // Speaker cone polygon: 11,5 -> 6,9 -> 2,9 -> 2,15 -> 6,15 -> 11,19
        val cone = Path().apply {
            moveTo(11f * s, 5f * s)
            lineTo(6f * s, 9f * s)
            lineTo(2f * s, 9f * s)
            lineTo(2f * s, 15f * s)
            lineTo(6f * s, 15f * s)
            lineTo(11f * s, 19f * s)
            close()
        }
        drawPath(cone, color, style = stroke)

        // Inner sound wave arc (~45 degrees each side of center)
        val innerArc = Path().apply {
            moveTo(15.54f * s, 8.46f * s)
            arcTo(
                Rect(12.54f * s, 5.46f * s, 18.54f * s, 18.54f * s),
                -45f, 90f, false
            )
        }
        drawPath(innerArc, color, style = stroke)

        // Outer sound wave arc
        val outerArc = Path().apply {
            moveTo(19.07f * s, 4.93f * s)
            arcTo(
                Rect(12.07f * s, -2.07f * s, 26.07f * s, 19.07f * s),
                -18f, 56f, false
            )
        }
        drawPath(outerArc, color, style = stroke)
    }
}

/**
 * Attach / plus icon for the composer attach button.
 */
@Composable
fun AttachIcon(
    modifier: Modifier = Modifier.size(18.dp),
    color: Color = Neutral500,
    strokeWidth: Float = 2.5f
) {
    Canvas(modifier = modifier) {
        val w = size.width
        val h = size.height
        val cx = w / 2f
        val cy = h / 2f
        val len = w * 0.35f
        // Horizontal bar
        drawLine(color, Offset(cx - len, cy), Offset(cx + len, cy), strokeWidth = strokeWidth, cap = StrokeCap.Round)
        // Vertical bar
        drawLine(color, Offset(cx, cy - len), Offset(cx, cy + len), strokeWidth = strokeWidth, cap = StrokeCap.Round)
    }
}

/**
 * Send arrow icon (upward arrow).
 */
@Composable
fun SendIcon(
    modifier: Modifier = Modifier.size(18.dp),
    color: Color = Color.White,
    strokeWidth: Float = 2.5f
) {
    Canvas(modifier = modifier) {
        val w = size.width
        val h = size.height
        val cx = w / 2f
        val armLen = w * 0.25f
        // Arrow shaft (vertical)
        drawLine(color, Offset(cx, h * 0.7f), Offset(cx, h * 0.2f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
        // Left arrowhead
        drawLine(color, Offset(cx, h * 0.2f), Offset(cx - armLen, h * 0.45f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
        // Right arrowhead
        drawLine(color, Offset(cx, h * 0.2f), Offset(cx + armLen, h * 0.45f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
    }
}

/**
 * Record audio icon — filled circle (record button) for raw audio capture.
 * Shown next to mic button when provider is Google/Gemini.
 */
@Composable
fun RecordAudioIcon(
    modifier: Modifier = Modifier.size(18.dp),
    color: Color = Neutral500,
    filled: Boolean = false
) {
    Canvas(modifier = modifier) {
        val w = size.width
        val h = size.height
        val cx = w / 2f
        val cy = h / 2f
        val radius = w * 0.35f
        if (filled) {
            drawCircle(color = color, radius = radius, center = Offset(cx, cy))
        } else {
            drawCircle(
                color = color,
                radius = radius,
                center = Offset(cx, cy),
                style = Stroke(width = 2f)
            )
            drawCircle(color = color, radius = radius * 0.5f, center = Offset(cx, cy))
        }
    }
}

/**
 * Voice-agent icon — a soundwave / equalizer (five vertical bars of differing
 * heights, tall in the middle). Opens the LIVE voice-agent screen, so it is
 * deliberately distinct from [MicIcon] (Whisper dictation into the text box).
 * Mirrors the Portal ctlVoiceAgent SVG (bars at x = 4,8,12,16,20).
 */
@Composable
fun VoiceAgentIcon(
    modifier: Modifier = Modifier.size(18.dp),
    color: Color = Neutral500,
    strokeWidth: Float = 2f
) {
    Canvas(modifier = modifier) {
        val s = size.width / 24f
        val sw = strokeWidth * s
        // Five vertical bars centered on y=12; heights grow toward the middle.
        drawLine(color, Offset(4f * s, 10f * s), Offset(4f * s, 14f * s), strokeWidth = sw, cap = StrokeCap.Round)
        drawLine(color, Offset(8f * s, 7f * s), Offset(8f * s, 17f * s), strokeWidth = sw, cap = StrokeCap.Round)
        drawLine(color, Offset(12f * s, 4f * s), Offset(12f * s, 20f * s), strokeWidth = sw, cap = StrokeCap.Round)
        drawLine(color, Offset(16f * s, 7f * s), Offset(16f * s, 17f * s), strokeWidth = sw, cap = StrokeCap.Round)
        drawLine(color, Offset(20f * s, 10f * s), Offset(20f * s, 14f * s), strokeWidth = sw, cap = StrokeCap.Round)
    }
}

/**
 * Copy / clipboard icon for bubble action row.
 */
@Composable
fun CopyIcon(
    modifier: Modifier = Modifier.size(16.dp),
    color: Color = Neutral500,
    strokeWidth: Float = 1.5f
) {
    Canvas(modifier = modifier) {
        val w = size.width
        val h = size.height
        val s = Stroke(width = strokeWidth, cap = StrokeCap.Round, join = StrokeJoin.Round)
        // Back rectangle (offset upper-right)
        drawRoundRect(color, topLeft = Offset(w * 0.25f, w * 0.05f), size = Size(w * 0.6f, h * 0.6f), cornerRadius = CornerRadius(w * 0.08f), style = s)
        // Front rectangle (offset lower-left)
        drawRoundRect(color, topLeft = Offset(w * 0.15f, w * 0.25f), size = Size(w * 0.6f, h * 0.6f), cornerRadius = CornerRadius(w * 0.08f), style = s)
    }
}
