package com.aiblackbox.portal.ui.voicelab

import android.Manifest
import android.content.pm.PackageManager
import android.view.HapticFeedbackConstants
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.repository.DesignPreview
import com.aiblackbox.portal.data.repository.ElevenVoice
import com.aiblackbox.portal.data.repository.SharedVoice
import com.aiblackbox.portal.ui.components.AudioPlayerBar
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import com.aiblackbox.portal.ui.theme.SolidGreen

// =============================================================================
// VoiceLabScreen — ElevenLabs Voice Lab (Task 25).
//
// Three zones in one scrollable column:
//   1) Clone   — record (AudioRecorderManager) OR pick file(s) + name + consent
//                checkbox (Clone disabled until name + ≥1 clip + consent).
//   2) Design  — description → 3 preview cards (AudioPlayerBar) → "Use this" →
//                name → save.
//   3) Manage  — my_voices list with delete (confirm; in_use warning via snackbar).
//
// Gating (GET /elevenlabs/status):
//   - not configured  → "Configure ElevenLabs in the Portal" empty state.
//   - cloning disabled → Clone zone shows an upgrade hint (Design/Manage still work).
// =============================================================================

@Composable
fun VoiceLabScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: VoiceLabViewModel = viewModel(),
) {
    val view = LocalView.current
    val context = LocalContext.current

    val statusLoaded by viewModel.statusLoaded.collectAsState()
    val status by viewModel.status.collectAsState()
    val message by viewModel.message.collectAsState()

    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    LaunchedEffect(message) {
        message?.let {
            snackbarHostState.showSnackbar(it)
            viewModel.clearMessage()
        }
    }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        containerColor = BbxBlack,
        snackbarHost = { SnackbarHost(snackbarHostState) },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(start = 16.dp, end = 16.dp, bottom = 24.dp, top = 100.dp)
        ) {
            // Header
            Text(
                "Voice Lab",
                style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
                color = BbxWhite,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "ElevenLabs" + (status?.tier?.takeIf { it.isNotBlank() }?.let { " · $it" } ?: ""),
                style = MaterialTheme.typography.bodySmall,
                color = Neutral500,
            )
            Spacer(Modifier.height(16.dp))

            when {
                !statusLoaded -> {
                    LoadingBlock("Checking ElevenLabs…")
                }
                status?.configured != true -> {
                    NotConfiguredCard()
                }
                else -> {
                    CloneZone(viewModel, context, view)
                    Spacer(Modifier.height(16.dp))
                    DesignZone(viewModel, view)
                    Spacer(Modifier.height(16.dp))
                    BrowseLibraryZone(viewModel, view)
                    Spacer(Modifier.height(16.dp))
                    ManageZone(viewModel, view)
                }
            }
        }
    }
}

// =============================================================================
// Empty / loading states
// =============================================================================

@Composable
private fun LoadingBlock(label: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        CircularProgressIndicator(modifier = Modifier.size(18.dp), color = BbxAccent, strokeWidth = 2.dp)
        Spacer(Modifier.size(10.dp))
        Text(label, color = BbxDim, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun NotConfiguredCard() {
    GlassCard(modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(RadiusMd)) {
        Column(modifier = Modifier.padding(18.dp)) {
            Text("ElevenLabs not configured", color = BbxWhite, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(8.dp))
            Text(
                "Add your ElevenLabs API key in the Portal to enable voice cloning, " +
                    "design, and management.",
                color = BbxDim,
                style = MaterialTheme.typography.bodyMedium,
            )
        }
    }
}

// =============================================================================
// Zone 1 — Clone
// =============================================================================

@Composable
private fun CloneZone(
    viewModel: VoiceLabViewModel,
    context: android.content.Context,
    view: android.view.View,
) {
    val status by viewModel.status.collectAsState()
    val cloneState by viewModel.cloneState.collectAsState()
    val parts by viewModel.cloneParts.collectAsState()
    val cloneError by viewModel.cloneError.collectAsState()
    val recordState by viewModel.recordState.collectAsState()
    val elapsedMs by viewModel.recordElapsedMs.collectAsState()
    val amplitude by viewModel.recordAmplitude.collectAsState()

    var name by remember { mutableStateOf("") }
    var description by remember { mutableStateOf("") }
    var removeNoise by remember { mutableStateOf(false) }
    var consent by remember { mutableStateOf(false) }

    val cloningEnabled = status?.instantVoiceCloning == true
    val isRecording = recordState == RecordState.RECORDING

    // RECORD_AUDIO permission gate (same approach as WhisperMicButton).
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) viewModel.startRecording() }

    // File picker — audio types (GetContent like FileAttachment.rememberFilePicker).
    val filePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri -> uri?.let { viewModel.addPickedFile(it) } }

    SectionCard(title = "🎙  Clone a voice") {
        if (!cloningEnabled) {
            UpgradeHint(
                "Instant Voice Cloning isn't available on your ElevenLabs tier. " +
                    "Upgrade to clone — Design and Manage still work below."
            )
            Spacer(Modifier.height(12.dp))
        }

        // ── Capture clips: record + upload ──
        Text("Voice samples", style = MaterialTheme.typography.labelMedium, color = BbxDim)
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            // Record / Stop pill
            RecordButton(
                isRecording = isRecording,
                enabled = cloningEnabled,
                elapsedMs = elapsedMs,
                amplitude = amplitude,
                onClick = {
                    if (!cloningEnabled) return@RecordButton
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    if (isRecording) {
                        viewModel.stopRecording()
                    } else {
                        val granted = ContextCompat.checkSelfPermission(
                            context, Manifest.permission.RECORD_AUDIO
                        ) == PackageManager.PERMISSION_GRANTED
                        if (granted) viewModel.startRecording()
                        else permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                    }
                },
            )
            // Upload pill
            PillAction(
                label = "Upload",
                enabled = cloningEnabled && !isRecording,
                onClick = {
                    view.performHapticFeedback(HapticFeedbackConstants.CONTEXT_CLICK)
                    filePicker.launch("audio/*")
                },
            )
        }

        // ── Queued clips ──
        if (parts.isNotEmpty()) {
            Spacer(Modifier.height(12.dp))
            parts.forEachIndexed { idx, part ->
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 3.dp)
                        .clip(RoundedCornerShape(RadiusSm))
                        .background(Neutral150OrSurface())
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        part.displayName,
                        color = BbxWhite,
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.weight(1f),
                    )
                    Box(
                        modifier = Modifier
                            .size(28.dp)
                            .clip(CircleShape)
                            .clickable(enabled = cloneState != CloneState.SUBMITTING) {
                                viewModel.removeClonePart(idx)
                            },
                        contentAlignment = Alignment.Center,
                    ) {
                        Icon(Icons.Filled.Close, contentDescription = "Remove", tint = Neutral700, modifier = Modifier.size(18.dp))
                    }
                }
            }
        }

        Spacer(Modifier.height(14.dp))

        // ── Name ──
        FieldLabel("Voice name")
        InputBox(
            value = name,
            onValueChange = { name = it },
            placeholder = "e.g. My Narrator",
            enabled = cloningEnabled,
            singleLine = true,
        )

        Spacer(Modifier.height(12.dp))

        // ── Description (optional) ──
        FieldLabel("Description (optional)")
        InputBox(
            value = description,
            onValueChange = { description = it },
            placeholder = "Tone, accent, intended use…",
            enabled = cloningEnabled,
            minHeight = 56.dp,
        )

        Spacer(Modifier.height(12.dp))

        // ── Remove background noise toggle ──
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(
                checked = removeNoise,
                onCheckedChange = { removeNoise = it },
                enabled = cloningEnabled,
                colors = SwitchDefaults.colors(
                    checkedTrackColor = SolidGreen,
                    checkedThumbColor = BbxWhite,
                ),
            )
            Spacer(Modifier.size(10.dp))
            Text("Remove background noise", color = BbxDim, style = MaterialTheme.typography.bodyMedium)
        }

        Spacer(Modifier.height(12.dp))

        // ── Consent checkbox (gate) ──
        ConsentRow(
            checked = consent,
            enabled = cloningEnabled,
            onToggle = { consent = !consent },
        )

        Spacer(Modifier.height(16.dp))

        // ── Submit ──
        val canSubmit = cloningEnabled &&
            name.isNotBlank() &&
            parts.isNotEmpty() &&
            consent &&
            cloneState != CloneState.SUBMITTING
        PrimaryButton(
            label = if (cloneState == CloneState.SUBMITTING) "Cloning…" else "Clone voice",
            enabled = canSubmit,
            loading = cloneState == CloneState.SUBMITTING,
            onClick = {
                view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                viewModel.submitClone(name, description, removeNoise, consent)
            },
        )

        AnimatedVisibility(visible = cloneError != null) {
            cloneError?.let { ErrorText(it) }
        }
    }
}

@Composable
private fun RecordButton(
    isRecording: Boolean,
    enabled: Boolean,
    elapsedMs: Long,
    amplitude: Int,
    onClick: () -> Unit,
) {
    val infinite = rememberInfiniteTransition(label = "recPulse")
    val pulse by infinite.animateFloat(
        initialValue = 0.55f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(600, easing = LinearEasing), RepeatMode.Reverse),
        label = "recPulseAlpha",
    )
    val bg = if (isRecording) BbxRed.copy(alpha = 0.18f) else Neutral200
    val border = if (isRecording) BbxRed else Neutral300
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusMd))
            .background(if (enabled) bg else Neutral100)
            .border(1.dp, if (enabled) border else Neutral300, RoundedCornerShape(RadiusMd))
            .clickable(enabled = enabled) { onClick() }
            .padding(horizontal = 16.dp, vertical = 10.dp),
        contentAlignment = Alignment.Center,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(10.dp)
                    .clip(CircleShape)
                    .background(
                        if (isRecording) BbxRed.copy(alpha = pulse)
                        else if (enabled) SolidGreen else Neutral500
                    )
            )
            Spacer(Modifier.size(8.dp))
            Text(
                if (isRecording) "Stop · ${formatElapsed(elapsedMs)}" else "Record",
                color = if (enabled) BbxWhite else Neutral500,
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

@Composable
private fun ConsentRow(checked: Boolean, enabled: Boolean, onToggle: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusSm))
            .clickable(enabled = enabled) { onToggle() }
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Box(
            modifier = Modifier
                .size(22.dp)
                .clip(RoundedCornerShape(RadiusSm))
                .background(if (checked && enabled) SolidGreen else Neutral200)
                .border(
                    1.dp,
                    if (checked && enabled) SolidGreen else Neutral300,
                    RoundedCornerShape(RadiusSm),
                ),
            contentAlignment = Alignment.Center,
        ) {
            if (checked) Text("✓", color = BbxBlack, fontWeight = FontWeight.Bold)
        }
        Spacer(Modifier.size(10.dp))
        Text(
            "I confirm I have the rights and consent to clone this voice, and accept " +
                "ElevenLabs' voice cloning terms.",
            color = if (enabled) BbxDim else Neutral500,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.weight(1f),
        )
    }
}

// =============================================================================
// Zone 2 — Design
// =============================================================================

@Composable
private fun DesignZone(viewModel: VoiceLabViewModel, view: android.view.View) {
    val designState by viewModel.designState.collectAsState()
    val previews by viewModel.designPreviews.collectAsState()
    val designError by viewModel.designError.collectAsState()

    var description by remember { mutableStateOf("") }
    var sampleText by remember { mutableStateOf("") }

    SectionCard(title = "✨  Design a voice") {
        FieldLabel("Voice description")
        InputBox(
            value = description,
            onValueChange = { description = it },
            placeholder = "A warm, middle-aged British narrator with a calm cadence…",
            minHeight = 72.dp,
        )
        Spacer(Modifier.height(12.dp))
        FieldLabel("Preview text (optional)")
        InputBox(
            value = sampleText,
            onValueChange = { sampleText = it },
            placeholder = "Text the previews will speak…",
            minHeight = 56.dp,
        )
        Spacer(Modifier.height(16.dp))
        PrimaryButton(
            label = if (designState == DesignState.GENERATING) "Generating…" else "Generate previews",
            enabled = description.isNotBlank() && designState != DesignState.GENERATING && designState != DesignState.SAVING,
            loading = designState == DesignState.GENERATING,
            onClick = {
                view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                viewModel.design(description, sampleText)
            },
        )

        AnimatedVisibility(visible = designError != null) {
            designError?.let { ErrorText(it) }
        }

        if (previews.isNotEmpty()) {
            Spacer(Modifier.height(16.dp))
            Text(
                "Pick a preview",
                style = MaterialTheme.typography.labelMedium,
                color = BbxDim,
            )
            Spacer(Modifier.height(10.dp))
            previews.forEachIndexed { idx, preview ->
                DesignPreviewCard(
                    index = idx,
                    preview = preview,
                    absoluteUrl = viewModel.absoluteUrl(preview.audioUrl),
                    saving = designState == DesignState.SAVING,
                    onSave = { name, desc -> viewModel.saveDesigned(preview.generatedVoiceId, name, desc) },
                )
                Spacer(Modifier.height(10.dp))
            }
        }
    }
}

@Composable
private fun DesignPreviewCard(
    index: Int,
    preview: DesignPreview,
    absoluteUrl: String,
    saving: Boolean,
    onSave: (String, String) -> Unit,
) {
    val view = LocalView.current
    var expanded by remember { mutableStateOf(false) }
    var saveName by remember { mutableStateOf("") }
    var saveDesc by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(Neutral100)
            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
            .padding(12.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "Preview ${index + 1}",
                color = BbxWhite,
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.Medium,
                modifier = Modifier.weight(1f),
            )
            val meta = buildString {
                if (preview.language.isNotBlank()) append(preview.language)
                if (preview.durationSecs > 0) {
                    if (isNotEmpty()) append(" · ")
                    append("${preview.durationSecs.toInt()}s")
                }
            }
            if (meta.isNotBlank()) {
                Text(meta, color = Neutral500, style = MaterialTheme.typography.bodySmall)
            }
        }
        Spacer(Modifier.height(8.dp))
        if (absoluteUrl.isNotBlank()) {
            // Reuse the shared waveform player (AudioPlaybackManager-backed).
            AudioPlayerBar(audioUrl = absoluteUrl, modifier = Modifier.fillMaxWidth())
        } else {
            Text("No audio preview", color = Neutral500, style = MaterialTheme.typography.bodySmall)
        }
        Spacer(Modifier.height(10.dp))
        if (!expanded) {
            PillAction(
                label = "Use this",
                enabled = !saving,
                onClick = {
                    view.performHapticFeedback(HapticFeedbackConstants.CONTEXT_CLICK)
                    expanded = true
                },
            )
        } else {
            FieldLabel("Voice name")
            InputBox(
                value = saveName,
                onValueChange = { saveName = it },
                placeholder = "Name this voice",
                singleLine = true,
            )
            Spacer(Modifier.height(8.dp))
            FieldLabel("Description (optional)")
            InputBox(
                value = saveDesc,
                onValueChange = { saveDesc = it },
                placeholder = "Optional notes",
                minHeight = 48.dp,
            )
            Spacer(Modifier.height(12.dp))
            PrimaryButton(
                label = if (saving) "Saving…" else "Save voice",
                enabled = saveName.isNotBlank() && !saving,
                loading = saving,
                onClick = {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    onSave(saveName, saveDesc)
                },
            )
        }
    }
}

// =============================================================================
// Zone 3 — Browse Library (community voices)
// =============================================================================

@Composable
private fun BrowseLibraryZone(viewModel: VoiceLabViewModel, view: android.view.View) {
    val query by viewModel.libraryQuery.collectAsState()
    val results by viewModel.libraryResults.collectAsState()
    val searching by viewModel.librarySearching.collectAsState()
    val searched by viewModel.librarySearched.collectAsState()
    val addingId by viewModel.libraryAddingId.collectAsState()

    SectionCard(title = "🌎  Browse library") {
        Text(
            "Search ElevenLabs' community voice library and add voices to your account.",
            color = BbxDim,
            style = MaterialTheme.typography.bodySmall,
        )
        Spacer(Modifier.height(12.dp))
        FieldLabel("Search voices")
        InputBox(
            value = query,
            onValueChange = { viewModel.setLibraryQuery(it) },
            placeholder = "e.g. calm narrator, deep male, British…",
            singleLine = true,
        )
        Spacer(Modifier.height(12.dp))
        PrimaryButton(
            label = if (searching) "Searching…" else "Search",
            enabled = query.isNotBlank() && !searching,
            loading = searching,
            onClick = {
                view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                viewModel.searchLibrary(query)
            },
        )

        if (searched && results.isEmpty() && !searching) {
            Spacer(Modifier.height(14.dp))
            Text(
                "No voices found. Try a different search.",
                color = Neutral500,
                style = MaterialTheme.typography.bodyMedium,
            )
        }

        if (results.isNotEmpty()) {
            Spacer(Modifier.height(16.dp))
            Text(
                "${results.size} result(s)",
                style = MaterialTheme.typography.labelMedium,
                color = BbxDim,
            )
            Spacer(Modifier.height(10.dp))
            results.forEach { voice ->
                LibraryVoiceCard(
                    voice = voice,
                    absoluteUrl = viewModel.absoluteUrl(voice.previewUrl),
                    adding = addingId == voice.voiceId,
                    addDisabled = addingId != null,
                    onAdd = {
                        view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                        viewModel.addLibraryVoice(voice)
                    },
                )
                Spacer(Modifier.height(10.dp))
            }
        }
    }
}

@Composable
private fun LibraryVoiceCard(
    voice: SharedVoice,
    absoluteUrl: String,
    adding: Boolean,
    addDisabled: Boolean,
    onAdd: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(Neutral100)
            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
            .padding(12.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    voice.name,
                    color = BbxWhite,
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.Medium,
                )
                val sub = listOf(voice.accent, voice.gender, voice.age)
                    .filter { it.isNotBlank() }
                    .joinToString(" · ")
                if (sub.isNotBlank()) {
                    Text(sub, color = Neutral500, style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        if (voice.description.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(
                voice.description,
                color = Neutral500,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 2,
            )
        }
        if (absoluteUrl.isNotBlank()) {
            Spacer(Modifier.height(10.dp))
            // Reuse the shared waveform player (AudioPlaybackManager-backed).
            AudioPlayerBar(audioUrl = absoluteUrl, modifier = Modifier.fillMaxWidth())
        }
        Spacer(Modifier.height(10.dp))
        PillAction(
            label = if (adding) "Adding…" else "Add",
            enabled = !addDisabled,
            onClick = onAdd,
        )
    }
}

// =============================================================================
// Zone 4 — Manage
// =============================================================================

@Composable
private fun ManageZone(viewModel: VoiceLabViewModel, view: android.view.View) {
    val voices by viewModel.myVoices.collectAsState()
    val loading by viewModel.voicesLoading.collectAsState()
    var confirmDelete by remember { mutableStateOf<ElevenVoice?>(null) }

    SectionCard(title = "🗂  My voices") {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                if (voices.isEmpty() && !loading) "No custom voices yet" else "${voices.size} voice(s)",
                color = BbxDim,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.weight(1f),
            )
            Box(
                modifier = Modifier
                    .size(34.dp)
                    .clip(CircleShape)
                    .clickable {
                        view.performHapticFeedback(HapticFeedbackConstants.CONTEXT_CLICK)
                        viewModel.loadVoices()
                    },
                contentAlignment = Alignment.Center,
            ) {
                if (loading) {
                    CircularProgressIndicator(modifier = Modifier.size(18.dp), color = BbxAccent, strokeWidth = 2.dp)
                } else {
                    Icon(Icons.Filled.Refresh, contentDescription = "Refresh", tint = BbxDim, modifier = Modifier.size(20.dp))
                }
            }
        }

        if (voices.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            voices.forEach { voice ->
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .clip(RoundedCornerShape(RadiusSm))
                        .background(Neutral100)
                        .border(1.dp, GlassBorder, RoundedCornerShape(RadiusSm))
                        .padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(voice.name, color = BbxWhite, fontWeight = FontWeight.Medium)
                        if (voice.description.isNotBlank()) {
                            Text(
                                voice.description,
                                color = Neutral500,
                                style = MaterialTheme.typography.bodySmall,
                                maxLines = 2,
                            )
                        }
                    }
                    Box(
                        modifier = Modifier
                            .size(34.dp)
                            .clip(CircleShape)
                            .clickable { confirmDelete = voice },
                        contentAlignment = Alignment.Center,
                    ) {
                        Icon(Icons.Filled.Delete, contentDescription = "Delete", tint = BbxRed, modifier = Modifier.size(20.dp))
                    }
                }
            }
        }
    }

    confirmDelete?.let { voice ->
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            title = { Text("Delete voice", color = BbxWhite) },
            text = { Text("Delete \"${voice.name}\"? This cannot be undone.", color = BbxDim) },
            confirmButton = {
                TextButton(onClick = {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    viewModel.deleteVoice(voice.id)
                    confirmDelete = null
                }) { Text("Delete", color = BbxRed) }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = null }) { Text("Cancel", color = BbxDim) }
            },
            containerColor = Neutral100,
            tonalElevation = 0.dp,
        )
    }
}

// =============================================================================
// Shared small composables
// =============================================================================

@Composable
private fun SectionCard(title: String, content: @Composable () -> Unit) {
    GlassCard(modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(RadiusLg)) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                title,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                color = BbxWhite,
            )
            Spacer(Modifier.height(14.dp))
            content()
        }
    }
}

@Composable
private fun FieldLabel(text: String) {
    Text(text, style = MaterialTheme.typography.labelMedium, color = BbxDim)
    Spacer(Modifier.height(6.dp))
}

@Composable
private fun InputBox(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    enabled: Boolean = true,
    singleLine: Boolean = false,
    minHeight: androidx.compose.ui.unit.Dp = 44.dp,
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(min = minHeight)
            .clip(RoundedCornerShape(RadiusMd))
            .background(if (enabled) Neutral100 else Neutral100.copy(alpha = 0.5f))
            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
            .padding(12.dp)
    ) {
        if (value.isEmpty()) {
            Text(placeholder, color = Neutral500, style = MaterialTheme.typography.bodyMedium)
        }
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            enabled = enabled,
            singleLine = singleLine,
            modifier = Modifier.fillMaxWidth(),
            textStyle = MaterialTheme.typography.bodyMedium.copy(color = BbxWhite),
            cursorBrush = SolidColor(BbxAccent),
        )
    }
}

@Composable
private fun PrimaryButton(
    label: String,
    enabled: Boolean,
    loading: Boolean = false,
    onClick: () -> Unit,
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(if (enabled) BbxAccent else Neutral200)
            .clickable(enabled = enabled) { onClick() }
            .padding(vertical = 13.dp),
        contentAlignment = Alignment.Center,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (loading) {
                CircularProgressIndicator(modifier = Modifier.size(16.dp), color = BbxWhite, strokeWidth = 2.dp)
                Spacer(Modifier.size(8.dp))
            }
            Text(
                label,
                color = if (enabled) BbxWhite else Neutral500,
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun PillAction(label: String, enabled: Boolean, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusMd))
            .background(if (enabled) Neutral200 else Neutral100)
            .border(1.dp, Neutral300, RoundedCornerShape(RadiusMd))
            .clickable(enabled = enabled) { onClick() }
            .padding(horizontal = 16.dp, vertical = 10.dp),
        contentAlignment = Alignment.Center,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Filled.Add, contentDescription = null, tint = if (enabled) BbxWhite else Neutral500, modifier = Modifier.size(16.dp))
            Spacer(Modifier.size(6.dp))
            Text(
                label,
                color = if (enabled) BbxWhite else Neutral500,
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

@Composable
private fun UpgradeHint(text: String) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusSm))
            .background(BbxAccent.copy(alpha = 0.10f))
            .border(1.dp, BbxAccent.copy(alpha = 0.35f), RoundedCornerShape(RadiusSm))
            .padding(12.dp)
    ) {
        Text(text, color = BbxDim, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun ErrorText(text: String) {
    Text(
        text,
        color = BbxRed,
        style = MaterialTheme.typography.bodySmall.copy(fontStyle = FontStyle.Italic),
        modifier = Modifier.padding(top = 10.dp),
    )
}

// Local surface tint for queued-clip rows (Neutral150 isn't imported app-wide
// consistently; use a small helper so the row reads as a faint elevated chip).
@Composable
private fun Neutral150OrSurface() = Neutral100

private fun formatElapsed(ms: Long): String {
    val totalSec = ms / 1000
    val m = totalSec / 60
    val s = totalSec % 60
    return "%d:%02d".format(m, s)
}
