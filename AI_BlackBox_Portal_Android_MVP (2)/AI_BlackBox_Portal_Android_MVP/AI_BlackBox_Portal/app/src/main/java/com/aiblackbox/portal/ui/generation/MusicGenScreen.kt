package com.aiblackbox.portal.ui.generation

import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.ui.components.EmberBackdrop
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.voice.LabeledDropdown
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.DurationBase
import com.aiblackbox.portal.ui.theme.EaseStandard
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.PillShape
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface

/**
 * Music genre presets matching Portal's MUSIC_PRESETS in generation-modals.js
 */
private data class MusicPreset(
    val key: String,
    val label: String,
    val prompt: String,
    val icon: String
)

private val MUSIC_PRESETS = listOf(
    MusicPreset(
        "epic", "Epic Orchestral",
        "Orchestral instrumental with brass fanfares, timpani, and strings building in intensity",
        "E"
    ),
    MusicPreset(
        "chill", "Chill Lo-fi",
        "Mellow piano chords with soft drums and gentle bass, relaxed tempo",
        "C"
    ),
    MusicPreset(
        "electronic", "Electronic",
        "Synthesizer arpeggios with four-on-the-floor drums and bass, upbeat tempo",
        "S"
    ),
    MusicPreset(
        "acoustic", "Acoustic",
        "Fingerpicked acoustic guitar with light percussion, warm and intimate",
        "A"
    ),
    MusicPreset(
        "jazz", "Jazz",
        "Piano trio with walking bass and brushed drums, medium swing tempo",
        "J"
    ),
    MusicPreset(
        "classical", "Classical",
        "String quartet with flowing melodies and delicate harmonies",
        "Q"
    ),
    MusicPreset(
        "ambient", "Ambient",
        "Evolving synthesizer pads with subtle textures and reverb, slow and meditative",
        "~"
    ),
    MusicPreset(
        "rock", "Rock",
        "Electric guitar riffs with drums and bass, energetic tempo",
        "R"
    )
)

// Green tint for music info banner (matches Portal's music-gen-info)
private val MusicGreenTint = Color(0xFF27D980)

/**
 * Music generation backends. Lyria (Google) = 30s instrumental, restricted
 * vocabulary. ElevenLabs = up to 5 min, vocals, any genre/style/mood/lyrics.
 */
private enum class MusicModel(val id: String, val label: String) {
    LYRIA("lyria", "Lyria-002 (Google)"),
    ELEVENLABS("elevenlabs", "ElevenLabs Music")
}

private val MUSIC_MODEL_OPTIONS: List<Pair<String, String>> =
    MusicModel.entries.map { it.id to it.label }

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun MusicGenScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: GenerationViewModel = viewModel()
) {
    val view = LocalView.current
    var selectedModel by remember { mutableStateOf(MusicModel.LYRIA) }
    var prompt by remember { mutableStateOf("") }
    var negPrompt by remember { mutableStateOf("") }
    var selectedPreset by remember { mutableStateOf<String?>(null) }
    // ElevenLabs-only params
    var durationSec by remember { mutableStateOf(30f) }
    var forceInstrumental by remember { mutableStateOf(false) }
    val state by viewModel.state.collectAsState()
    val resultUrl by viewModel.resultUrl.collectAsState()
    val error by viewModel.error.collectAsState()
    val taskStatus by viewModel.taskStatus.collectAsState()

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    // Ember backdrop behind the (scrolling) content while generating. The
    // overlay is a SIBLING of the scroll, so it stays fixed full-screen.
    EmberBackdrop(
        active = state == GenState.SUBMITTING || state == GenState.POLLING,
        modifier = modifier,
    ) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 100.dp)
    ) {
        // Header
        Text(
            "Music Generation",
            style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            color = BbxWhite
        )
        Spacer(Modifier.height(4.dp))
        Text(
            when (selectedModel) {
                MusicModel.LYRIA -> "Lyria-002 · 30s instrumental"
                MusicModel.ELEVENLABS -> "ElevenLabs Music · up to 5 min, vocals"
            },
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500
        )
        Spacer(Modifier.height(16.dp))

        // Model selector
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(start = 14.dp, end = 14.dp, top = 14.dp, bottom = 4.dp)) {
                LabeledDropdown(
                    label = "Model",
                    options = MUSIC_MODEL_OPTIONS,
                    selectedId = selectedModel.id,
                    onSelect = { id ->
                        selectedModel = MusicModel.entries.firstOrNull { it.id == id } ?: MusicModel.LYRIA
                    }
                )
            }
        }
        Spacer(Modifier.height(16.dp))

        // Genre Presets card — Lyria only (its prompts are tuned for the
        // restricted instrument/tempo/texture vocabulary).
        if (selectedModel == MusicModel.LYRIA) {
            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(RadiusMd)
            ) {
                Column(modifier = Modifier.padding(14.dp)) {
                    Text(
                        "Genre Presets",
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                        color = BbxDim
                    )
                    Spacer(Modifier.height(10.dp))
                    FlowRow(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        MUSIC_PRESETS.forEach { preset ->
                            val selected = selectedPreset == preset.key
                            val interactionSource = remember { MutableInteractionSource() }
                            val pressed by interactionSource.collectIsPressedAsState()
                            val chipScale by animateFloatAsState(
                                targetValue = if (pressed) 0.92f else 1f,
                                animationSpec = tween(DurationBase, easing = EaseStandard),
                                label = "presetScale"
                            )
                            val chipBg by animateColorAsState(
                                targetValue = if (selected) MusicGreenTint.copy(alpha = 0.15f) else Neutral200,
                                animationSpec = tween(DurationBase, easing = EaseStandard),
                                label = "presetBg"
                            )
                            val chipBorder by animateColorAsState(
                                targetValue = if (selected) MusicGreenTint.copy(alpha = 0.5f) else Neutral300,
                                animationSpec = tween(DurationBase, easing = EaseStandard),
                                label = "presetBorder"
                            )

                            Box(
                                modifier = Modifier
                                    .scale(chipScale)
                                    .clip(PillShape)
                                    .background(chipBg)
                                    .border(1.dp, chipBorder, PillShape)
                                    .clickFeedback(
                                        interactionSource = interactionSource,
                                        indication = null
                                    ) {
                                        if (selected) {
                                            // Deselect
                                            selectedPreset = null
                                        } else {
                                            selectedPreset = preset.key
                                            prompt = preset.prompt
                                        }
                                    }
                                    .padding(horizontal = 14.dp, vertical = 8.dp)
                            ) {
                                Text(
                                    preset.label,
                                    style = MaterialTheme.typography.labelMedium.copy(
                                        fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal
                                    ),
                                    color = if (selected) MusicGreenTint else Neutral500
                                )
                            }
                        }
                    }
                }
            }
            Spacer(Modifier.height(16.dp))
        }

        // Prompt input
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Text(
                    "Prompt",
                    style = MaterialTheme.typography.labelMedium,
                    color = BbxDim
                )
                Spacer(Modifier.height(8.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 80.dp)
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(Neutral100)
                        .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                        .padding(12.dp)
                ) {
                    if (prompt.isEmpty()) {
                        Text(
                            when (selectedModel) {
                                MusicModel.LYRIA -> "Describe the music you want to create..."
                                MusicModel.ELEVENLABS ->
                                    "any genre, style, mood, or lyrics — e.g. 'upbeat synthwave with vocals about flying'"
                            },
                            color = Neutral500,
                            style = MaterialTheme.typography.bodyLarge
                        )
                    }
                    BasicTextField(
                        value = prompt,
                        onValueChange = {
                            prompt = it
                            // Clear preset selection if user edits the prompt
                            if (selectedPreset != null) {
                                val presetPrompt = MUSIC_PRESETS.find { p -> p.key == selectedPreset }?.prompt
                                if (it != presetPrompt) selectedPreset = null
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                        textStyle = MaterialTheme.typography.bodyLarge.copy(color = BbxWhite),
                        cursorBrush = SolidColor(BbxAccent)
                    )
                }

                // Negative prompt — Lyria only (ElevenLabs has no restricted
                // vocabulary, so it has no exclude field).
                if (selectedModel == MusicModel.LYRIA) {
                    Spacer(Modifier.height(14.dp))
                    Text(
                        "Exclude (optional)",
                        style = MaterialTheme.typography.labelMedium,
                        color = BbxDim
                    )
                    Spacer(Modifier.height(6.dp))
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(min = 48.dp)
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(Neutral100)
                            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                            .padding(12.dp)
                    ) {
                        if (negPrompt.isEmpty()) {
                            Text(
                                "vocals, singing...",
                                color = Neutral500,
                                style = MaterialTheme.typography.bodyMedium
                            )
                        }
                        BasicTextField(
                            value = negPrompt,
                            onValueChange = { negPrompt = it },
                            modifier = Modifier.fillMaxWidth(),
                            textStyle = MaterialTheme.typography.bodyMedium.copy(color = BbxWhite),
                            cursorBrush = SolidColor(BbxAccent)
                        )
                    }
                }
            }
        }
        Spacer(Modifier.height(16.dp))

        // ElevenLabs-only params: duration + instrumental toggle.
        if (selectedModel == MusicModel.ELEVENLABS) {
            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(RadiusMd)
            ) {
                Column(modifier = Modifier.padding(14.dp)) {
                    // Duration slider (3–300s → music_length_ms)
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            "Duration",
                            style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                            color = BbxDim
                        )
                        Text(
                            "${durationSec.toInt()}s",
                            style = MaterialTheme.typography.labelMedium.copy(
                                fontFamily = FontFamily.Monospace
                            ),
                            color = Neutral700
                        )
                    }
                    Spacer(Modifier.height(4.dp))
                    Slider(
                        value = durationSec,
                        onValueChange = { durationSec = it },
                        valueRange = 3f..300f,
                        modifier = Modifier.fillMaxWidth(),
                        colors = SliderDefaults.colors(
                            thumbColor = BbxAccent,
                            activeTrackColor = BbxAccent,
                            inactiveTrackColor = Neutral300
                        )
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("3s", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                        Text("5 min", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                    }
                    Spacer(Modifier.height(14.dp))

                    // Instrumental-only toggle (force_instrumental)
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                "Instrumental only",
                                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                                color = BbxDim
                            )
                            Text(
                                "No vocals",
                                style = MaterialTheme.typography.labelSmall,
                                color = Neutral500
                            )
                        }
                        Switch(
                            checked = forceInstrumental,
                            onCheckedChange = {
                                view.performPressFeedback()
                                forceInstrumental = it
                            },
                            colors = SwitchDefaults.colors(
                                checkedThumbColor = BbxWhite,
                                checkedTrackColor = BbxAccent,
                                uncheckedThumbColor = Neutral500,
                                uncheckedTrackColor = Neutral200
                            )
                        )
                    }
                }
            }
            Spacer(Modifier.height(16.dp))
        }

        Spacer(Modifier.height(4.dp))

        // Generate button
        val btnInteraction = remember { MutableInteractionSource() }
        val btnPressed by btnInteraction.collectIsPressedAsState()
        val btnScale by animateFloatAsState(
            targetValue = if (btnPressed) 0.96f else 1f,
            animationSpec = tween(DurationBase, easing = EaseStandard),
            label = "btnScale"
        )
        val isGenerating = state == GenState.SUBMITTING || state == GenState.POLLING
        val btnEnabled = prompt.isNotBlank() && !isGenerating

        Box(
            modifier = Modifier
                .fillMaxWidth()
                .scale(btnScale)
                .clip(RoundedCornerShape(RadiusLg))
                .background(
                    if (btnEnabled) BbxAccent
                    else BbxAccent.copy(alpha = 0.4f)
                )
                .clickFeedback(
                    interactionSource = btnInteraction,
                    indication = null,
                    enabled = btnEnabled
                ) {
                    when (selectedModel) {
                        MusicModel.LYRIA -> viewModel.generateMusic(prompt, negPrompt)
                        MusicModel.ELEVENLABS -> viewModel.generateElevenLabsMusic(
                            prompt = prompt,
                            musicLengthMs = (durationSec.toInt()) * 1000,
                            forceInstrumental = forceInstrumental
                        )
                    }
                }
                .padding(vertical = 14.dp),
            contentAlignment = Alignment.Center
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.Center
            ) {
                if (isGenerating) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(18.dp),
                        color = BbxWhite,
                        strokeWidth = 2.dp
                    )
                    Spacer(Modifier.width(10.dp))
                }
                Text(
                    when (state) {
                        GenState.IDLE -> "Generate Music"
                        GenState.SUBMITTING -> "Submitting..."
                        GenState.POLLING -> "Generating..."
                        GenState.COMPLETED -> "Done!"
                        GenState.FAILED -> "Retry"
                    },
                    style = MaterialTheme.typography.labelLarge.copy(
                        fontWeight = FontWeight.Bold,
                        fontSize = 15.sp
                    ),
                    color = BbxWhite
                )
            }
        }

        // Error display
        AnimatedVisibility(
            visible = error != null,
            enter = fadeIn() + scaleIn(initialScale = 0.95f),
            exit = fadeOut()
        ) {
            error?.let {
                Spacer(Modifier.height(10.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(BbxAccent.copy(alpha = 0.1f))
                        .border(1.dp, BbxAccent.copy(alpha = 0.3f), RoundedCornerShape(RadiusMd))
                        .padding(12.dp)
                ) {
                    Text(it, color = BbxAccent, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        // Status when polling
        AnimatedVisibility(
            visible = state == GenState.POLLING,
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Column(modifier = Modifier.padding(top = 12.dp)) {
                taskStatus?.let { status ->
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(MusicGreenTint.copy(alpha = 0.05f))
                            .padding(12.dp)
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(14.dp),
                                color = MusicGreenTint,
                                strokeWidth = 2.dp
                            )
                            Spacer(Modifier.width(10.dp))
                            Column {
                                Text(
                                    "Task ${status.taskId.take(8)}...",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = BbxDim
                                )
                                Text(
                                    "Music generation takes 1-2 minutes",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Neutral500,
                                    fontSize = 11.sp
                                )
                            }
                        }
                    }
                }
            }
        }

        // Result
        AnimatedVisibility(
            visible = resultUrl != null,
            enter = fadeIn() + scaleIn(initialScale = 0.9f),
            exit = fadeOut()
        ) {
            resultUrl?.let { url ->
                Column(modifier = Modifier.padding(top = 20.dp)) {
                    // Success banner
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(SolidGreen.copy(alpha = 0.1f))
                            .border(1.dp, SolidGreen.copy(alpha = 0.3f), RoundedCornerShape(RadiusMd))
                            .padding(14.dp)
                    ) {
                        Column {
                            Text(
                                "Music Ready",
                                style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold),
                                color = SolidGreen
                            )
                            Spacer(Modifier.height(4.dp))
                            Text(
                                url,
                                style = MaterialTheme.typography.bodySmall,
                                color = BbxDim,
                                maxLines = 2
                            )
                        }
                    }
                    Spacer(Modifier.height(12.dp))

                    // Generate another button
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusLg))
                            .glassSurface(
                                shape = RoundedCornerShape(RadiusLg),
                                bg = GlassFloatingBubble
                            )
                            .clickFeedback {
                                viewModel.reset()
                                prompt = ""
                                negPrompt = ""
                                selectedPreset = null
                                durationSec = 30f
                                forceInstrumental = false
                            }
                            .padding(vertical = 12.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "Generate Another",
                            color = BbxAccent,
                            style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Medium)
                        )
                    }
                }
            }
        }

        Spacer(Modifier.height(180.dp))
    }
    }
}
