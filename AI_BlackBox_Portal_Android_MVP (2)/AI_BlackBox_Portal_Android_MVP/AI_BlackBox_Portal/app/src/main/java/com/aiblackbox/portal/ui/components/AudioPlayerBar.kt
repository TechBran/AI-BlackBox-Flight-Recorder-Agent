package com.aiblackbox.portal.ui.components

import android.view.HapticFeedbackConstants
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.voice.VoiceWaveform
import com.aiblackbox.portal.ui.voice.WaveSpeaker
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

// =============================================================================
// AudioPlayerBar — production-grade audio player
//
//   - Flowing red "ribbon" waveform (VoiceWaveform) that pulses while playing
//   - Thin progress track + playhead beneath the ribbon (visible + scrubbable)
//   - Play/pause + tap-to-seek + horizontal-drag-to-seek
//   - Red accent on black background
// =============================================================================

private const val POLL_MS = 33L // ~30fps for smooth animation

// Red accent palette on pure black
private val WaveRed = Color(0xFFEF4444)
private val WaveRedDim = Color(0xFFDC2626)
private val WaveRedGlow = Color(0x40EF4444)
private val WaveUnplayed = Color(0xFF2A2A2A)
private val WaveBg = Color(0xFF000000)
private val TimeColor = Color(0xCCEF4444)

@Composable
fun AudioPlayerBar(
    audioUrl: String,
    modifier: Modifier = Modifier
) {
    val view = LocalView.current
    val mgr = com.aiblackbox.portal.data.voice.AudioPlaybackManager

    val activeUrl by mgr.activeUrl.collectAsState()
    val isThisActive = activeUrl == audioUrl
    val isPlaying by mgr.isPlaying.collectAsState()
    val isPrepared by mgr.isPrepared.collectAsState()
    val duration by mgr.duration.collectAsState()
    val position by mgr.position.collectAsState()
    val hasError by mgr.hasError.collectAsState()
    val outputAmplitude by mgr.amplitude.collectAsState()
    val visualizerActive by mgr.visualizerActive.collectAsState()
    var isSeeking by remember { mutableStateOf(false) }
    var seekPosition by remember { mutableFloatStateOf(0f) }

    val thisPlaying = isThisActive && isPlaying
    val thisPrepared = isThisActive && isPrepared
    val displayPosition = if (isSeeking) seekPosition else if (isThisActive) position else 0f
    val displayDuration = if (isThisActive) duration else 0L

    // Smooth animated position for fluid playhead movement
    val animatedPosition by animateFloatAsState(
        targetValue = displayPosition,
        animationSpec = spring(dampingRatio = 0.8f, stiffness = 300f),
        label = "wavePos"
    )

    // Subtle breathing animation when playing
    val infiniteTransition = rememberInfiniteTransition(label = "wavePulse")
    val breathe by infiniteTransition.animateFloat(
        initialValue = 0f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(2000, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "breathe"
    )

    // Position polling
    LaunchedEffect(isThisActive, isPlaying) {
        while (isThisActive && isPlaying && isActive) {
            if (!isSeeking) mgr.updatePosition()
            delay(POLL_MS)
        }
    }

    val playBtnBg by animateColorAsState(
        targetValue = if (thisPlaying) WaveRed else WaveRed.copy(alpha = 0.2f),
        animationSpec = tween(200), label = "btnBg"
    )
    val playIconColor by animateColorAsState(
        targetValue = if (thisPlaying) Color.Black else WaveRed,
        animationSpec = tween(200), label = "btnIcon"
    )

    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(WaveBg)
            .padding(horizontal = 10.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        // ── Play/Pause ──
        Box(
            modifier = Modifier
                .size(34.dp)
                .clip(CircleShape)
                .background(playBtnBg)
                .clickFeedback {
                    if (!isThisActive) mgr.loadAndPlay(audioUrl)
                    else mgr.togglePlayPause()
                },
            contentAlignment = Alignment.Center
        ) {
            if (thisPlaying) {
                PauseIcon(Modifier.size(14.dp), playIconColor)
            } else {
                PlayIcon(Modifier.size(14.dp), playIconColor)
            }
        }

        // ── Waveform (flowing red ribbon) + thin seek/progress track ──
        var canvasWidth by remember { mutableFloatStateOf(1f) }

        // Drive the ribbon from the REAL audio output (Visualizer RMS) while
        // playing, so it dances with the actual speech. If the Visualizer could
        // not attach on this device, fall back to a gentle synthetic pulse so
        // the ribbon still shows life. The ribbon also flows via VoiceWaveform's
        // own continuous phase animation.
        val ribbonAmplitude = when {
            !thisPlaying -> 0f
            visualizerActive -> (outputAmplitude * 1.5f).coerceIn(0f, 1f)
            else -> 0.12f + breathe * 0.18f
        }

        Box(
            modifier = Modifier
                .weight(1f)
                .height(40.dp)
                .onSizeChanged { canvasWidth = it.width.toFloat() }
                .pointerInput(isThisActive) {
                    detectTapGestures { offset ->
                        if (canvasWidth > 0) {
                            val frac = (offset.x / canvasWidth).coerceIn(0f, 1f)
                            view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                            if (!isThisActive) mgr.loadAndPlay(audioUrl)
                            seekPosition = frac
                            mgr.seekTo(frac)
                        }
                    }
                }
                .pointerInput(isThisActive) {
                    detectHorizontalDragGestures(
                        onDragStart = { isSeeking = true },
                        onDragEnd = {
                            if (isThisActive) mgr.seekTo(seekPosition)
                            isSeeking = false
                        },
                        onDragCancel = { isSeeking = false },
                        onHorizontalDrag = { change, _ ->
                            if (canvasWidth > 0) {
                                seekPosition = (change.position.x / canvasWidth).coerceIn(0f, 1f)
                            }
                        }
                    )
                }
        ) {
            // Flowing ribbon, forced to the red palette regardless of speaker.
            VoiceWaveform(
                amplitude = ribbonAmplitude,
                speaker = WaveSpeaker.USER,
                modifier = Modifier.fillMaxSize(),
                height = 40.dp,
                overrideColors = WaveRed to WaveRedDim,
                pauseWhenIdle = true,
            )

            // Thin progress track + playhead beneath the ribbon so position is
            // visible and scrubbable (the ribbon itself shows no played/unplayed).
            Canvas(modifier = Modifier.fillMaxSize()) {
                val cW = size.width
                val cH = size.height
                val trackY = cH * 0.92f                 // sit the track near the bottom
                val trackH = 2.dp.toPx()
                val frac = animatedPosition.coerceIn(0f, 1f)

                // Unplayed track (full width)
                drawLine(
                    color = WaveUnplayed,
                    start = Offset(0f, trackY),
                    end = Offset(cW, trackY),
                    strokeWidth = trackH,
                    cap = StrokeCap.Round
                )
                // Played portion
                if (frac > 0f) {
                    drawLine(
                        color = WaveRed,
                        start = Offset(0f, trackY),
                        end = Offset(frac * cW, trackY),
                        strokeWidth = trackH,
                        cap = StrokeCap.Round
                    )
                }
                // Playhead — thin bright line spanning the ribbon + a dot on the track
                val px = frac * cW
                drawLine(
                    color = WaveRed.copy(alpha = if (thisPlaying) 0.9f else 0.5f),
                    start = Offset(px, cH * 0.05f),
                    end = Offset(px, trackY),
                    strokeWidth = 1.5f
                )
                drawCircle(
                    color = WaveRed,
                    radius = 3.5f,
                    center = Offset(px, trackY)
                )
            }
        }

        // ── Time ──
        val currentMs = if (thisPrepared && displayDuration > 0) (displayPosition * displayDuration).toLong() else 0L
        Text(
            text = formatMs(currentMs),
            fontSize = 11.sp,
            fontFamily = FontFamily.Monospace,
            fontWeight = FontWeight.Medium,
            color = TimeColor,
            maxLines = 1
        )
    }
}

// =============================================================================
// Icons
// =============================================================================

@Composable
private fun PlayIcon(modifier: Modifier = Modifier.size(14.dp), color: Color = WaveRed) {
    Canvas(modifier = modifier) {
        val path = Path().apply {
            moveTo(size.width * 0.25f, size.height * 0.12f)
            lineTo(size.width * 0.85f, size.height * 0.5f)
            lineTo(size.width * 0.25f, size.height * 0.88f)
            close()
        }
        drawPath(path, color)
    }
}

@Composable
private fun PauseIcon(modifier: Modifier = Modifier.size(14.dp), color: Color = WaveRed) {
    Canvas(modifier = modifier) {
        val barW = size.width * 0.22f
        val gap = size.width * 0.12f
        val startX = (size.width - barW * 2 - gap) / 2
        drawRoundRect(color, Offset(startX, size.height * 0.15f), Size(barW, size.height * 0.7f), CornerRadius(barW * 0.3f))
        drawRoundRect(color, Offset(startX + barW + gap, size.height * 0.15f), Size(barW, size.height * 0.7f), CornerRadius(barW * 0.3f))
    }
}

private fun formatMs(ms: Long): String {
    val totalSec = ms / 1000
    return "%d:%02d".format(totalSec / 60, totalSec % 60)
}
