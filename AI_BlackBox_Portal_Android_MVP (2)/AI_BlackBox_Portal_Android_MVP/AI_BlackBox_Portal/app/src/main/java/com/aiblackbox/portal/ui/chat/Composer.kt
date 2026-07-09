package com.aiblackbox.portal.ui.chat

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.data.model.ChatProvider
import com.aiblackbox.portal.ui.components.AttachIcon
import com.aiblackbox.portal.ui.components.MicIcon
import com.aiblackbox.portal.ui.components.RecordAudioIcon
import com.aiblackbox.portal.ui.components.SendIcon
import android.view.HapticFeedbackConstants
import android.widget.Toast
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import com.aiblackbox.portal.ui.components.SpeakerIcon
import com.aiblackbox.portal.ui.voice.VoiceWaveform
import com.aiblackbox.portal.ui.voice.WaveSpeaker
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.GlassComposerInput
import com.aiblackbox.portal.ui.theme.GlassProviderPill
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.glassSurface
import com.aiblackbox.portal.util.Constants

// =============================================================================
// Composer — aligned with Portal .composer
//
// Portal structure:
//   .composer (transparent, fixed bottom, pointer-events: none)
//     .input-row
//       .textarea-wrapper (frosted glass bubble: rgba(28,28,30,0.92))
//         btnAttach | textarea | ctlMic | btnSend
//     .control-row
//       .provider-model-bubble (rgba(28,28,30,0.88))
//         providerSelect | divider | modelSelect
//       .toolbar-btn (auto-TTS, circular)
//
// Layout order: input row FIRST, control row SECOND (below input)
// =============================================================================

@Composable
fun Composer(
    value: TextFieldValue,
    onValueChange: (TextFieldValue) -> Unit,
    onSend: () -> Unit,
    onAttach: () -> Unit = {},
    onWhisper: () -> Unit = {},
    onRecordAudio: () -> Unit = {},
    isStreaming: Boolean = false,
    isRecording: Boolean = false,
    isRecordingAudio: Boolean = false,
    recordingAmplitude: () -> Float = { 0f },
    provider: String = "gemini",
    model: String = "",
    onProviderChange: (String) -> Unit = {},
    onModelChange: (String) -> Unit = {},
    autoTtsEnabled: Boolean = false,
    onAutoTtsToggle: () -> Unit = {},
    providerLabel: String = "",
    // Task 1.6: gate the on-device LOCAL provider — only offer it when the
    // operator has a disk-present, sha-verified model installed.
    localAvailable: Boolean = false,
    // Task W1: on-device engine readiness — drives the "loading…/ready" suffix on
    // the provider pill while the model warms (so the cold load is visible, not a
    // surprise on first send). Only affects the pill when the LOCAL provider is active.
    localEngineState: LocalEngineState = LocalEngineState.IDLE,
    // Fired when the provider dropdown opens — host re-checks local availability
    // (and fires a best-effort re-attest) so a just-installed model shows up.
    onProviderMenuOpen: () -> Unit = {},
    liveModels: List<Pair<String, String>> = emptyList(),
    // Task 7.1: custom-provider model load status (id → "loaded"/"unloaded",
    // from GET /models/custom via ChatViewModel.customModelStatus). Rows whose
    // status is "loaded" get a warm dot — the model is resident in the server's
    // RAM, so the first token is instant. Empty for every other provider.
    customModelStatus: Map<String, String> = emptyMap(),
    attachments: List<AttachmentItem> = emptyList(),
    onRemoveAttachment: (Int) -> Unit = {},
    modifier: Modifier = Modifier
) {
    val view = LocalView.current
    val ctx = LocalContext.current
    val hasText = value.text.isNotBlank() || attachments.isNotEmpty()
    val sendScale by animateFloatAsState(
        targetValue = if (hasText && !isStreaming) 1f else 0.85f,
        animationSpec = spring(dampingRatio = 0.6f),
        label = "sendScale"
    )
    val sendColor by animateColorAsState(
        targetValue = if (hasText && !isStreaming) BbxAccent else Neutral500,
        label = "sendColor"
    )

    // Determine if record audio button should be visible (Google/Gemini providers only)
    val showRecordAudio = provider == "gemini" || provider == "google"

    Column(
        modifier = modifier
            .fillMaxWidth()
            .navigationBarsPadding()
            .imePadding()
            .padding(horizontal = 12.dp, vertical = 8.dp)
    ) {
        // ── Attachment preview strip (above input bubble) ──
        AttachmentPreview(
            attachments = attachments,
            onRemove = onRemoveAttachment
        )

        // ── Row 1: Input bubble ──
        // Matches Portal .textarea-wrapper (frosted glass with shadow).
        // Inner Column: [waveform ribbon (only while recording)] above
        // [icons + text field] — the text field is ALWAYS rendered, never
        // swapped out for the waveform.
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .glassSurface(
                    shape = RoundedCornerShape(24.dp),
                    bg = GlassComposerInput,
                    elevation = 8.dp,
                    borderOverride = androidx.compose.ui.graphics.Color.Transparent,
                )
                .padding(horizontal = 4.dp, vertical = 4.dp)
        ) {
            // Waveform ribbon — full-width row DIRECTLY ABOVE the input row,
            // only composed while recording/streaming (Whisper or raw audio).
            if (isRecording || isRecordingAudio) {
                VoiceWaveform(
                    amplitude = recordingAmplitude(),
                    speaker = WaveSpeaker.USER,
                    height = 52.dp,
                    // Lift speech-level RMS into the short composer ribbon so it
                    // reads clearly. Tunable — raise for more swing, lower if it
                    // pins at full on normal speech.
                    sensitivity = 3.0f,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 8.dp, vertical = 2.dp)
                )
            }

            // Input row: icons + the always-visible text field.
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.Bottom
            ) {
                // Attach button (inside bubble, matches Portal .input-action-btn)
                IconButton(
                    onClick = {
                        view.performPressFeedback()
                        onAttach()
                    },
                    modifier = Modifier.size(40.dp)
                ) {
                    AttachIcon(modifier = Modifier.size(20.dp), color = BbxAccent)
                }

                // Text field — ALWAYS visible (never swapped for the waveform).
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .padding(vertical = 8.dp)
                ) {
                    if (value.text.isEmpty()) {
                        Text(
                            text = "Type a message\u2026",
                            style = MaterialTheme.typography.bodyLarge.copy(
                                fontSize = 16.sp,
                                color = BbxAccent.copy(alpha = 0.4f)
                            )
                        )
                    }
                    BasicTextField(
                        value = value,
                        onValueChange = onValueChange,
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(min = 24.dp, max = 144.dp),
                        textStyle = MaterialTheme.typography.bodyLarge.copy(
                            color = BbxWhite,
                            fontSize = 16.sp,
                            lineHeight = 22.sp
                        ),
                        cursorBrush = SolidColor(BbxAccent),
                        maxLines = 6,
                        readOnly = isStreaming
                    )
                }

                // Raw audio record button (inside bubble, only for Gemini/Google)
                if (showRecordAudio) {
                    IconButton(
                        onClick = {
                            view.performPressFeedback()
                            onRecordAudio()
                        },
                        modifier = Modifier.size(36.dp)
                    ) {
                        RecordAudioIcon(
                            modifier = Modifier.size(18.dp),
                            color = if (isRecordingAudio) BbxAccent else Neutral500,
                            filled = isRecordingAudio
                        )
                    }
                }

                // Whisper mic button (inside bubble)
                IconButton(
                    onClick = { view.performPressFeedback(); onWhisper() },
                    modifier = Modifier.size(36.dp)
                ) {
                    MicIcon(
                        modifier = Modifier.size(18.dp),
                        color = if (isRecording) BbxAccent else Neutral500
                    )
                }

                // Send button (inside bubble)
                IconButton(
                    onClick = { view.performPressFeedback(); if (hasText && !isStreaming) onSend() },
                    modifier = Modifier
                        .size(40.dp)
                        .scale(sendScale)
                ) {
                    Box(
                        modifier = Modifier
                            .size(36.dp)
                            .clip(CircleShape)
                            .background(sendColor),
                        contentAlignment = Alignment.Center
                    ) {
                        SendIcon(modifier = Modifier.size(18.dp), color = BbxWhite)
                    }
                }
            }
        }

        // ── Row 2: Provider/Model pill + Auto-TTS toggle ──
        // Matches Portal .control-row
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(top = 8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Provider/Model merged pill
            // Matches Portal .provider-model-bubble (rgba(28,28,30,0.88))
            var showProviderMenu by remember { mutableStateOf(false) }
            var showModelMenu by remember { mutableStateOf(false) }

            Row(
                modifier = Modifier
                    .glassSurface(
                        shape = RoundedCornerShape(20.dp),
                        bg = GlassProviderPill,
                        elevation = 4.dp,
                        borderOverride = androidx.compose.ui.graphics.Color.Transparent,
                    ),
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Provider dropdown
                Box {
                    Text(
                        // Task W1: append a readiness suffix for the on-device pill
                        // (loading…/ready/⚠) so the model warm is visible.
                        text = providerPillLabel(provider, localEngineState),
                        modifier = Modifier
                            .clickFeedback { onProviderMenuOpen(); showProviderMenu = true }
                            .padding(horizontal = 14.dp, vertical = 10.dp),
                        style = MaterialTheme.typography.labelMedium.copy(
                            fontWeight = FontWeight.Medium,
                            fontSize = 13.sp
                        ),
                        color = BbxAccent
                    )
                    DropdownMenu(
                        expanded = showProviderMenu,
                        onDismissRequest = { showProviderMenu = false }
                    ) {
                        // Task 1.6: hide LOCAL unless a verified on-device model is installed.
                        ChatProvider.entries
                            .filter { !it.isLocal || localAvailable }
                            .forEach { p ->
                            DropdownMenuItem(
                                text = {
                                    Text(
                                        p.displayName,
                                        color = if (p.id == provider) BbxAccent else BbxWhite,
                                        fontWeight = if (p.id == provider) FontWeight.Bold else FontWeight.Normal
                                    )
                                },
                                onClick = {
                                    view.performPressFeedback()
                                    onProviderChange(p.id)
                                    showProviderMenu = false
                                }
                            )
                        }
                    }
                }

                // Divider (matches Portal .bubble-divider)
                Box(
                    Modifier
                        .width(1.dp)
                        .height(20.dp)
                        .background(GlassBorder)
                )

                // Model dropdown — prefer live models from API, fall back to Constants
                Box {
                    val models = liveModels.ifEmpty { Constants.MODEL_CONFIG[provider] ?: emptyList() }
                    val displayModel = models.find { it.first == model }?.second ?: "Auto"
                    Text(
                        text = displayModel,
                        modifier = Modifier
                            .clickFeedback { showModelMenu = true }
                            .padding(horizontal = 14.dp, vertical = 10.dp),
                        style = MaterialTheme.typography.labelMedium.copy(
                            fontSize = 12.sp
                        ),
                        color = BbxAccent.copy(alpha = 0.7f)
                    )
                    DropdownMenu(
                        expanded = showModelMenu,
                        onDismissRequest = { showModelMenu = false }
                    ) {
                        models.forEach { (id, name) ->
                            DropdownMenuItem(
                                text = {
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        // Task 7.1: warm dot — this custom model is
                                        // resident in its server's RAM ("loaded"), so
                                        // picking it means an instant first token.
                                        if (customModelStatus[id] == "loaded") {
                                            Box(
                                                Modifier
                                                    .size(8.dp)
                                                    .background(
                                                        androidx.compose.ui.graphics.Color(0xFFFF9800),
                                                        CircleShape
                                                    )
                                                    // A11y: DropdownMenuItem merges child
                                                    // semantics → TalkBack reads
                                                    // "loaded, <model name>".
                                                    .semantics { contentDescription = "loaded" }
                                            )
                                            Spacer(Modifier.width(6.dp))
                                        }
                                        Text(
                                            name,
                                            color = if (id == model) BbxAccent else BbxWhite,
                                            fontWeight = if (id == model) FontWeight.Bold else FontWeight.Normal
                                        )
                                    }
                                },
                                onClick = {
                                    view.performPressFeedback()
                                    onModelChange(id)
                                    showModelMenu = false
                                }
                            )
                        }
                    }
                }
            }

            Spacer(Modifier.weight(1f))

            // Auto-TTS toggle (matches Portal .toolbar-btn circular)
            IconButton(
                onClick = {
                    view.performPressFeedback()
                    onAutoTtsToggle()
                    val msg = if (!autoTtsEnabled) "Auto-TTS ON — responses will be spoken"
                              else "Auto-TTS OFF"
                    Toast.makeText(ctx, msg, Toast.LENGTH_SHORT).show()
                },
                modifier = Modifier.size(42.dp)
            ) {
                Box(
                    modifier = Modifier
                        .size(42.dp)
                        .glassSurface(
                            shape = CircleShape,
                            bg = if (autoTtsEnabled)
                                BbxAccent.copy(alpha = 0.15f)
                            else
                                GlassProviderPill,
                            elevation = 4.dp,
                            borderOverride = androidx.compose.ui.graphics.Color.Transparent,
                        ),
                    contentAlignment = Alignment.Center
                ) {
                    SpeakerIcon(
                        modifier = Modifier.size(18.dp),
                        color = if (autoTtsEnabled) BbxAccent else Neutral500
                    )
                }
            }
        }
    }
}

/**
 * The provider pill's label text (Task W1). For every provider it is the plain
 * [ChatProvider.displayName]; for the ON-DEVICE (LOCAL) provider it appends a
 * readiness suffix reflecting [LocalEngineState] so the model warm is visible:
 *  - [LocalEngineState.WARMING] -> " \u00b7 loading\u2026"
 *  - [LocalEngineState.READY]   -> " \u00b7 ready"
 *  - [LocalEngineState.ERROR]   -> " \u00b7 \u26a0" (load failed; the send still
 *    retries the lazy load, so this is informational)
 *  - [LocalEngineState.IDLE]    -> no suffix (not warmed / not the active provider)
 *
 * Non-local providers ignore the engine state entirely. Pure (no Compose) so the
 * mapping is unit-testable.
 */
internal fun providerPillLabel(providerId: String, localEngineState: LocalEngineState): String {
    val base = ChatProvider.fromId(providerId).displayName
    if (!ChatProvider.fromId(providerId).isLocal) return base
    val suffix = when (localEngineState) {
        LocalEngineState.WARMING -> " \u00b7 loading\u2026"
        LocalEngineState.READY -> " \u00b7 ready"
        LocalEngineState.ERROR -> " \u00b7 \u26a0"
        LocalEngineState.IDLE -> ""
    }
    return base + suffix
}
