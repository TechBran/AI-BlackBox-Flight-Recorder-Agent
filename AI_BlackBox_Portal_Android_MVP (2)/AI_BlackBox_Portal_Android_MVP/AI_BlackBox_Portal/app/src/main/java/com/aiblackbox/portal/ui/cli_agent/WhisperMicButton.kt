package com.aiblackbox.portal.ui.cli_agent

// WhisperMicButton — tap-to-record state machine that injects streaming-STT
// transcripts as a single bracketed paste into the active terminal session.
//
// State machine: idle 🎤 → recording 🔴 → transcribing ⏳ → idle 🎤
// Long-press during recording cancels (discards transcript).
//
// Recording: SttStreamClient — the unified live-transcription client over the
// backend's /ws/stt WebSocket (PCM16 @24kHz). This shares ONE transcription
// path with the chat composer's live dictation. Because the CLI terminal has
// no editable buffer (the transcript is pasted into the PTY as one
// bracketed-paste), interim deltas are IGNORED — only the FINAL transcript is
// pasted via onTranscript: (String) -> Unit. The caller wraps that in a
// {"type":"paste","text":transcript} text frame.

import android.Manifest
import android.content.pm.PackageManager
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.voice.SttEvent
import com.aiblackbox.portal.data.voice.SttStreamClient
import com.aiblackbox.portal.ui.components.MicIcon

/**
 * Mic-button state for the WhisperMicButton state machine.
 *
 *   Idle         — tap to begin streaming capture.
 *   Recording    — red icon w/ pulse; tap to stop, long-press to cancel.
 *   Transcribing — spinner while the trailing stt_final is awaited.
 */
private enum class MicState { Idle, Recording, Transcribing }

@Composable
fun WhisperMicButton(
    onTranscript: (String) -> Unit,
    api: BlackBoxApi,
    @Suppress("UNUSED_PARAMETER") operator: String,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current

    // Unified streaming-STT client; survives recompositions, rebuilt only if api changes.
    val sttClient = remember(api) {
        val wsUrl = api.getBaseUrl()
            .replace("https://", "wss://")
            .replace("http://", "ws://")
        SttStreamClient(api.getClient(), wsUrl)
    }

    var state by remember { mutableStateOf(MicState.Idle) }
    // Set true by long-press / cap-during-cancel: the next Final is discarded.
    var cancelRequested by remember { mutableStateOf(false) }

    // Keep the latest callback without restarting the events collector.
    val currentOnTranscript by rememberUpdatedState(onTranscript)

    fun beginStreaming() {
        cancelRequested = false
        state = MicState.Recording
        sttClient.start()
    }

    // Permission launcher. On grant → start streaming. On deny → toast + idle.
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            beginStreaming()
        } else {
            Toast.makeText(
                context,
                "Microphone permission required",
                Toast.LENGTH_SHORT
            ).show()
            state = MicState.Idle
        }
    }

    // Collect transcript events. Final → one-shot paste; Error → toast; Delta → ignore.
    LaunchedEffect(sttClient) {
        sttClient.events.collect { event ->
            when (event) {
                is SttEvent.Final -> {
                    if (cancelRequested) {
                        // Discarded by long-press/cancel — do NOT paste.
                        cancelRequested = false
                    } else if (event.text.isNotBlank()) {
                        currentOnTranscript(event.text)
                    }
                    state = MicState.Idle
                }
                is SttEvent.Error -> {
                    cancelRequested = false
                    Toast.makeText(context, event.message, Toast.LENGTH_SHORT).show()
                    state = MicState.Idle
                }
                is SttEvent.Delta -> {
                    // Cumulative interim — IGNORED. PTY paste is one-shot on Final.
                }
            }
        }
    }

    // Visual: pulsing alpha for the recording state.
    val infiniteTransition = rememberInfiniteTransition(label = "micPulse")
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue = 0.5f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 600, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse,
        ),
        label = "micPulseAlpha",
    )

    val iconScale by animateFloatAsState(
        targetValue = if (state == MicState.Recording) 1.1f else 1f,
        label = "micIconScale",
    )

    // Stop the stream + release mic/WS if the Composable leaves composition.
    DisposableEffect(sttClient) {
        onDispose {
            sttClient.stop()
        }
    }

    val shape = RoundedCornerShape(6.dp)
    val isRecording = state == MicState.Recording
    val isTranscribing = state == MicState.Transcribing

    Box(
        modifier = modifier
            .defaultMinSize(minWidth = 44.dp, minHeight = 36.dp)
            .height(36.dp)
            .clip(shape)
            .background(
                if (isRecording) MaterialTheme.colorScheme.errorContainer
                else MaterialTheme.colorScheme.surface
            )
            .border(
                width = 1.dp,
                color = if (isRecording) MaterialTheme.colorScheme.error
                else MaterialTheme.colorScheme.outlineVariant,
                shape = shape,
            )
            .pointerInput(state) {
                detectTapGestures(
                    onTap = {
                        when (state) {
                            MicState.Idle -> {
                                // Permission gate.
                                val granted = ContextCompat.checkSelfPermission(
                                    context,
                                    Manifest.permission.RECORD_AUDIO,
                                ) == PackageManager.PERMISSION_GRANTED
                                if (granted) {
                                    beginStreaming()
                                } else {
                                    permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                                }
                            }
                            MicState.Recording -> {
                                // Stop streaming; the trailing Final drives → Idle (paste).
                                state = MicState.Transcribing
                                sttClient.stop()
                            }
                            MicState.Transcribing -> {
                                // Ignore taps while awaiting the final transcript.
                            }
                        }
                    },
                    onLongPress = {
                        if (state == MicState.Recording) {
                            // Discard: suppress the next Final, stop, return to idle.
                            cancelRequested = true
                            sttClient.stop()
                            state = MicState.Idle
                            Toast.makeText(
                                context,
                                "Recording cancelled",
                                Toast.LENGTH_SHORT
                            ).show()
                        }
                    },
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        when {
            isTranscribing -> {
                CircularProgressIndicator(
                    modifier = Modifier.size(20.dp),
                    strokeWidth = 2.dp,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
            isRecording -> {
                MicIcon(
                    modifier = Modifier
                        .size(20.dp)
                        .scale(iconScale)
                        .alpha(pulseAlpha),
                    color = MaterialTheme.colorScheme.error,
                )
            }
            else -> {
                MicIcon(
                    modifier = Modifier.size(20.dp),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}
