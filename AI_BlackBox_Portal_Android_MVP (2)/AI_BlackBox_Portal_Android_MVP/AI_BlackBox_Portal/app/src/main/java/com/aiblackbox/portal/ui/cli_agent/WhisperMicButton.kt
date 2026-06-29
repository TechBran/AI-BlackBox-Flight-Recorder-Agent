package com.aiblackbox.portal.ui.cli_agent

// WhisperMicButton — tap-to-record state machine for the CLI-agent terminal.
// It streams live speech-to-text and pastes the FINAL transcript into the
// active terminal session as one bracketed paste.
//
// State machine: idle 🎤 → recording 🔴 → transcribing ⏳ → idle 🎤
// Long-press during recording cancels (discards transcript).
//
// Transport: SttStreamClient — the unified, PROVIDER-AGNOSTIC live-transcription
// client over the backend's /ws/stt WebSocket (PCM16 @24kHz). It sends
// provider:"" so the backend uses whatever STT provider the box is configured
// for (OpenAI realtime / ElevenLabs Scribe / Google) — this is NOT Whisper-locked.
// This shares ONE transcription path with the chat composer's live dictation.
//
// The terminal PTY has no editable buffer, so the cumulative interim `stt_delta`s
// are shown live in a floating preview chip above the mic, and the FINAL
// transcript is pasted via onTranscript: (String) -> Unit (the caller wraps it in
// a {"type":"paste","text":transcript} text frame).

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
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
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
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntRect
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Popup
import androidx.compose.ui.window.PopupPositionProvider
import androidx.compose.ui.window.PopupProperties
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

/**
 * Returns the most-recent [max] characters of [text] (prefixed with an ellipsis
 * when truncated) so the live preview chip always shows the latest words as a
 * cumulative transcript grows.
 */
internal fun previewTail(text: String, max: Int = 160): String =
    if (text.length > max) "…" + text.takeLast(max) else text

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
    // Set true by long-press cancel: the next Final is discarded (not pasted).
    var cancelRequested by remember { mutableStateOf(false) }

    // Latest CUMULATIVE interim transcript from stt_delta; shown live in the chip.
    var interimText by remember { mutableStateOf("") }

    // Keep the latest callback without restarting the events collector.
    val currentOnTranscript by rememberUpdatedState(onTranscript)

    fun beginStreaming() {
        cancelRequested = false
        interimText = ""
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

    // Collect transcript events. Final → one-shot paste; Error → toast; Delta → live chip.
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
                    interimText = ""
                    state = MicState.Idle
                }
                is SttEvent.Error -> {
                    cancelRequested = false
                    Toast.makeText(context, event.message, Toast.LENGTH_SHORT).show()
                    interimText = ""
                    state = MicState.Idle
                }
                is SttEvent.Delta -> {
                    // Cumulative interim — show it live in the preview chip.
                    interimText = event.text
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

    // Anchors the preview chip centered above the mic button (Popup floats over
    // the terminal — no editable buffer needed, no layout disturbance).
    val density = LocalDensity.current
    val gapPx = with(density) { 6.dp.roundToPx() }
    val chipPositionProvider = remember(gapPx) {
        object : PopupPositionProvider {
            override fun calculatePosition(
                anchorBounds: IntRect,
                windowSize: IntSize,
                layoutDirection: LayoutDirection,
                popupContentSize: IntSize,
            ): IntOffset {
                val x = anchorBounds.left + (anchorBounds.width - popupContentSize.width) / 2
                val y = anchorBounds.top - popupContentSize.height - gapPx
                val maxX = (windowSize.width - popupContentSize.width).coerceAtLeast(0)
                return IntOffset(x.coerceIn(0, maxX), y.coerceAtLeast(0))
            }
        }
    }

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
                            interimText = ""
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
        // Live transcription preview — floats above the mic while recording /
        // finalizing. The Popup occupies no layout space in the button Box.
        if ((isRecording || isTranscribing) && interimText.isNotBlank()) {
            Popup(
                popupPositionProvider = chipPositionProvider,
                properties = PopupProperties(focusable = false),
            ) {
                TranscriptPreviewChip(text = interimText)
            }
        }
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

@Composable
private fun TranscriptPreviewChip(text: String) {
    Surface(
        shape = RoundedCornerShape(10.dp),
        color = MaterialTheme.colorScheme.surface,
        contentColor = MaterialTheme.colorScheme.onSurface,
        tonalElevation = 3.dp,
        shadowElevation = 6.dp,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        modifier = Modifier.widthIn(max = 280.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            MicIcon(
                modifier = Modifier.size(14.dp),
                color = MaterialTheme.colorScheme.error,
            )
            Spacer(Modifier.width(8.dp))
            Text(
                text = previewTail(text),
                style = MaterialTheme.typography.bodySmall,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}
