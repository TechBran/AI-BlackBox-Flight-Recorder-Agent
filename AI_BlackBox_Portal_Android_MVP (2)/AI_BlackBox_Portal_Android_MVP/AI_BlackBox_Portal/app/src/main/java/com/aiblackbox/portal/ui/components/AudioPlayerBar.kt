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
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS
import com.aiblackbox.portal.ui.components.aurora.AuroraWaveform
import com.aiblackbox.portal.ui.components.aurora.auroraVoices
import com.aiblackbox.portal.ui.theme.RadiusMd
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

// =============================================================================
// AudioPlayerBar — production-grade audio player
//
//   - Flowing Aurora "ribbon" waveform that pulses while playing
//   - Thin progress track + playhead beneath the ribbon (visible + scrubbable)
//   - Play/pause + tap-to-seek + horizontal-drag-to-seek
//   - Red accent on black background
// =============================================================================

private const val POLL_MS = 33L // ~30fps for smooth animation

// Red accent palette on pure black
// WaveRed still paints the progress track + playhead. Its gradient partner went with the ribbon:
// the Aurora renderer takes no colour override — the speaker owns the colour (this bar is the AI).
private val WaveRed = Color(0xFFEF4444)
private val WaveRedGlow = Color(0x40EF4444)
private val WaveUnplayed = Color(0xFF2A2A2A)
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
    val outputBands by mgr.bands.collectAsState()
    val amplitudeReady by mgr.amplitudeReady.collectAsState()
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
            // Transparent by design: the player bar sits over the Ember backdrop,
            // so no opaque fill here — embers glow through. (Was Color(0xFF000000).)
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

        // Drive the ribbon from the REAL audio output — AudioPlaybackManager samples the decoded
        // envelopes at the live playback position, so it dances with the actual speech. Until that
        // decode lands there is nothing to sample, so a gentle synthetic pulse stands in (one value
        // drives all four sheets) rather than showing a dead ribbon over audible audio.
        //
        // NEVER absent. This bar's voice is always PRESENT — all-zero bands when the clip is
        // silent for a moment AND when nothing is playing at all — so the ribbon rests FLAT
        // (`restLevel = 0f` below) instead of retiring off the surface. Absence is what the
        // renderer culls, and culling here would be a regression, not a saving: VoiceWaveform
        // painted its line at amplitude 0 whether or not the clip was playing, so every
        // not-playing bar in a message list would go from a flat ribbon to an empty 40dp box and
        // a silent passage mid-clip would blink the ribbon out and back. It costs nothing either
        // — a present, silent, settled voice stops asking for frames exactly as an absent one does
        // (see AuroraEngine.needsFrames), which is what `pauseWhenIdle` below is for.
        val ribbonBands: FloatArray = when {
            !thisPlaying -> AURORA_SILENT_BANDS
            amplitudeReady -> outputBands
            else -> floatArrayOf(0.12f + breathe * 0.18f)
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
            // Same renderer as the mic and voice-screen ribbons. This is the AI talking, so the
            // ribbon is BLUE here where the mic's is red — the identity is the speaker's, not the
            // surface's, which is why the old overrideColors pair is gone.
            //
            // No sensitivity knob either: the offline analyser normalises each band against that
            // band's own peak over the whole clip, so a quiet TTS reply and a loud one both use the
            // full ribbon without a per-call-site gain to keep in sync with the audio pipeline.
            AuroraWaveform(
                voices = auroraVoices(ai = ribbonBands),
                modifier = Modifier.fillMaxSize(),
                // A message list can hold dozens of these; every one that is not playing must stop
                // asking for frames. The bar is the one surface where this genuinely idles.
                pauseWhenIdle = true,
                // FLAT on silence, not Aurora's breathing baseline — this is what VoiceWaveform's
                // `idleLevel = 0f` bought here and it has to survive the renderer swap. It matters
                // on THIS surface specifically: a silent passage mid-clip is the file's own silence
                // (the decoded envelope gates it to exactly 0), the bar is only 40dp tall so a
                // 0.06 floor is a visible ~1.1dp of permanent swing, and it sits directly on the
                // progress track the user scrubs. A live mic wants the opposite and keeps the
                // default. Pinned by a test — see AuroraWaveformTest.
                restLevel = 0f,
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
