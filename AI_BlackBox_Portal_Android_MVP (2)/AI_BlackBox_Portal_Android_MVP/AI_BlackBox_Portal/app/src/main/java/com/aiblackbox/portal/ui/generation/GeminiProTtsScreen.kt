package com.aiblackbox.portal.ui.generation

import android.app.Application
import androidx.compose.animation.AnimatedVisibility
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
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
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
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.ui.components.AudioPlayerBar
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.DurationBase
import com.aiblackbox.portal.ui.theme.EaseStandard
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral150
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

// =============================================================================
// Gemini Pro TTS Voice Data
// =============================================================================

data class GeminiVoice(val name: String, val desc: String)

// Sourced from the single canonical Gemini voice list in TtsRepository
// (GEMINI_TTS_VOICE_PAIRS) — no separate copy to drift. Yields the same
// 30 (name, description) voices used everywhere else in the app.
val GEMINI_PRO_VOICES = com.aiblackbox.portal.data.repository.GEMINI_TTS_VOICE_PAIRS
    .map { (n, d) -> GeminiVoice(n, d) }

// =============================================================================
// ViewModel
// =============================================================================

class GeminiProTtsViewModel(application: Application) : AndroidViewModel(application) {
    private val store = BlackBoxStore(application)
    private var api: BlackBoxApi? = null

    private val _isMultiSpeaker = MutableStateFlow(false)
    val isMultiSpeaker: StateFlow<Boolean> = _isMultiSpeaker.asStateFlow()

    private val _status = MutableStateFlow("")
    val status: StateFlow<String> = _status.asStateFlow()

    private val _isGenerating = MutableStateFlow(false)
    val isGenerating: StateFlow<Boolean> = _isGenerating.asStateFlow()

    private val _audioUrl = MutableStateFlow<String?>(null)
    val audioUrl: StateFlow<String?> = _audioUrl.asStateFlow()

    private var currentOperator = "Brandon"
    private var originBase = ""

    init {
        viewModelScope.launch { store.operator.collect { currentOperator = it } }
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        originBase = origin
        api = BlackBoxApi(origin)
    }

    fun setMultiSpeaker(value: Boolean) {
        _isMultiSpeaker.value = value
    }

    fun generate(
        text: String,
        voiceName: String,
        isMulti: Boolean,
        speaker1Name: String,
        speaker1Voice: String,
        speaker2Name: String,
        speaker2Voice: String,
        operator: String
    ) {
        val api = api ?: return
        _isGenerating.value = true
        _status.value = "Submitting..."
        _audioUrl.value = null

        viewModelScope.launch {
            try {
                val body = if (isMulti) {
                    buildJsonObject {
                        put("text", text)
                        put("multi_speaker", true)
                        put("speakers", buildJsonArray {
                            add(buildJsonObject {
                                put("name", speaker1Name)
                                put("voice", speaker1Voice)
                            })
                            add(buildJsonObject {
                                put("name", speaker2Name)
                                put("voice", speaker2Voice)
                            })
                        })
                        put("model", "gemini-2.5-pro-tts")
                        put("operator", operator)
                    }.toString()
                } else {
                    buildJsonObject {
                        put("text", text)
                        put("voice_name", voiceName)
                        put("multi_speaker", false)
                        put("model", "gemini-2.5-pro-tts")
                        put("operator", operator)
                    }.toString()
                }

                val response = api.post("/generate/gemini_tts", body)
                val responseObj = api.json.parseToJsonElement(response).jsonObject
                val taskId = responseObj["task_id"]?.jsonPrimitive?.content
                    ?: throw Exception("No task_id in response")

                _status.value = "Generating... (task ${taskId.take(8)})"

                // Poll until complete
                while (true) {
                    delay(1000)
                    val statusResponse = api.get("/tasks/status/$taskId")
                    val statusObj = api.json.parseToJsonElement(statusResponse).jsonObject
                    val taskStatus = statusObj["status"]?.jsonPrimitive?.content ?: "pending"

                    when {
                        taskStatus.equals("completed", ignoreCase = true) -> {
                            val resultUrl = statusObj["result_url"]?.jsonPrimitive?.content
                            if (resultUrl != null) {
                                _audioUrl.value = if (resultUrl.startsWith("http")) {
                                    resultUrl
                                } else {
                                    "$originBase$resultUrl"
                                }
                            }
                            _status.value = "Audio ready"
                            _isGenerating.value = false
                            return@launch
                        }
                        taskStatus.equals("failed", ignoreCase = true) -> {
                            val errorMsg = statusObj["error_message"]?.jsonPrimitive?.content
                                ?: "Generation failed"
                            _status.value = "Error: $errorMsg"
                            _isGenerating.value = false
                            return@launch
                        }
                        else -> {
                            _status.value = "Generating... (task ${taskId.take(8)})"
                        }
                    }
                }
            } catch (e: Exception) {
                _status.value = "Error: ${e.message}"
                _isGenerating.value = false
            }
        }
    }

    fun reset() {
        _isGenerating.value = false
        _status.value = ""
        _audioUrl.value = null
    }
}

// =============================================================================
// Accent colors for speaker cards
// =============================================================================

private val Speaker1Accent = Color(0xFFBB86FC) // Purple
private val Speaker2Accent = Color(0xFF4DD0E1) // Cyan

// =============================================================================
// Composable
// =============================================================================

@Composable
fun GeminiProTtsScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: GeminiProTtsViewModel = viewModel()
) {
    val view = LocalView.current
    val isMulti by viewModel.isMultiSpeaker.collectAsState()
    val status by viewModel.status.collectAsState()
    val isGenerating by viewModel.isGenerating.collectAsState()
    val audioUrl by viewModel.audioUrl.collectAsState()

    var text by remember { mutableStateOf("") }
    var selectedVoice by remember { mutableStateOf("Charon") }
    var speaker1Name by remember { mutableStateOf("Alice") }
    var speaker1Voice by remember { mutableStateOf("Kore") }
    var speaker2Name by remember { mutableStateOf("Bob") }
    var speaker2Voice by remember { mutableStateOf("Puck") }

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 100.dp)
    ) {
        // ── Header ──
        Text(
            "Gemini Pro TTS",
            style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            color = BbxWhite
        )
        Spacer(Modifier.height(4.dp))
        Text(
            "Multi-speaker AI voice synthesis",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500
        )
        Spacer(Modifier.height(20.dp))

        // ── Multi-Speaker Toggle ──
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 14.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    "Multi-Speaker Mode",
                    style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Medium),
                    color = BbxWhite
                )
                Switch(
                    checked = isMulti,
                    onCheckedChange = { checked ->
                        view.performPressFeedback()
                        viewModel.setMultiSpeaker(checked)
                    },
                    colors = SwitchDefaults.colors(
                        checkedThumbColor = BbxWhite,
                        checkedTrackColor = BbxAccent,
                        checkedBorderColor = BbxAccent,
                        uncheckedThumbColor = Neutral500,
                        uncheckedTrackColor = Neutral200,
                        uncheckedBorderColor = Neutral300
                    )
                )
            }
        }
        Spacer(Modifier.height(16.dp))

        // ── Single Speaker Voice Selection ──
        AnimatedVisibility(
            visible = !isMulti,
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(RadiusMd)
            ) {
                Column(modifier = Modifier.padding(14.dp)) {
                    Text(
                        "Voice",
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                        color = BbxDim
                    )
                    Spacer(Modifier.height(8.dp))
                    VoiceDropdown(
                        selectedVoice = selectedVoice,
                        onVoiceSelected = { voice ->
                            selectedVoice = voice
                        }
                    )
                }
            }
        }

        // ── Multi Speaker Configuration ──
        AnimatedVisibility(
            visible = isMulti,
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Column {
                // Speaker 1
                GlassCard(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(RadiusMd)
                ) {
                    Row(modifier = Modifier.fillMaxWidth()) {
                        // Left accent bar
                        Box(
                            modifier = Modifier
                                .width(4.dp)
                                .heightIn(min = 120.dp)
                                .background(
                                    Speaker1Accent,
                                    RoundedCornerShape(
                                        topStart = RadiusMd,
                                        bottomStart = RadiusMd
                                    )
                                )
                        )
                        Column(
                            modifier = Modifier
                                .weight(1f)
                                .padding(14.dp)
                        ) {
                            Text(
                                "Speaker 1",
                                style = MaterialTheme.typography.labelLarge.copy(
                                    fontWeight = FontWeight.Bold
                                ),
                                color = Speaker1Accent
                            )
                            Spacer(Modifier.height(10.dp))
                            Text(
                                "Name",
                                style = MaterialTheme.typography.labelSmall,
                                color = BbxDim
                            )
                            Spacer(Modifier.height(4.dp))
                            Box(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clip(RoundedCornerShape(RadiusSm))
                                    .background(Neutral100)
                                    .border(
                                        1.dp,
                                        GlassBorder,
                                        RoundedCornerShape(RadiusSm)
                                    )
                                    .padding(horizontal = 12.dp, vertical = 10.dp)
                            ) {
                                BasicTextField(
                                    value = speaker1Name,
                                    onValueChange = { speaker1Name = it },
                                    modifier = Modifier.fillMaxWidth(),
                                    textStyle = MaterialTheme.typography.bodyMedium.copy(
                                        color = BbxWhite
                                    ),
                                    cursorBrush = SolidColor(BbxAccent),
                                    singleLine = true
                                )
                            }
                            Spacer(Modifier.height(10.dp))
                            Text(
                                "Voice",
                                style = MaterialTheme.typography.labelSmall,
                                color = BbxDim
                            )
                            Spacer(Modifier.height(4.dp))
                            VoiceDropdown(
                                selectedVoice = speaker1Voice,
                                onVoiceSelected = { voice ->
                                    speaker1Voice = voice
                                }
                            )
                        }
                    }
                }
                Spacer(Modifier.height(12.dp))

                // Speaker 2
                GlassCard(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(RadiusMd)
                ) {
                    Row(modifier = Modifier.fillMaxWidth()) {
                        // Left accent bar
                        Box(
                            modifier = Modifier
                                .width(4.dp)
                                .heightIn(min = 120.dp)
                                .background(
                                    Speaker2Accent,
                                    RoundedCornerShape(
                                        topStart = RadiusMd,
                                        bottomStart = RadiusMd
                                    )
                                )
                        )
                        Column(
                            modifier = Modifier
                                .weight(1f)
                                .padding(14.dp)
                        ) {
                            Text(
                                "Speaker 2",
                                style = MaterialTheme.typography.labelLarge.copy(
                                    fontWeight = FontWeight.Bold
                                ),
                                color = Speaker2Accent
                            )
                            Spacer(Modifier.height(10.dp))
                            Text(
                                "Name",
                                style = MaterialTheme.typography.labelSmall,
                                color = BbxDim
                            )
                            Spacer(Modifier.height(4.dp))
                            Box(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clip(RoundedCornerShape(RadiusSm))
                                    .background(Neutral100)
                                    .border(
                                        1.dp,
                                        GlassBorder,
                                        RoundedCornerShape(RadiusSm)
                                    )
                                    .padding(horizontal = 12.dp, vertical = 10.dp)
                            ) {
                                BasicTextField(
                                    value = speaker2Name,
                                    onValueChange = { speaker2Name = it },
                                    modifier = Modifier.fillMaxWidth(),
                                    textStyle = MaterialTheme.typography.bodyMedium.copy(
                                        color = BbxWhite
                                    ),
                                    cursorBrush = SolidColor(BbxAccent),
                                    singleLine = true
                                )
                            }
                            Spacer(Modifier.height(10.dp))
                            Text(
                                "Voice",
                                style = MaterialTheme.typography.labelSmall,
                                color = BbxDim
                            )
                            Spacer(Modifier.height(4.dp))
                            VoiceDropdown(
                                selectedVoice = speaker2Voice,
                                onVoiceSelected = { voice ->
                                    speaker2Voice = voice
                                }
                            )
                        }
                    }
                }
                Spacer(Modifier.height(10.dp))

                // Multi-speaker hint
                Text(
                    "Format: [${speaker1Name}] Hello!\n[${speaker2Name}] Hi there!",
                    style = MaterialTheme.typography.bodySmall.copy(fontStyle = FontStyle.Italic),
                    color = Neutral500,
                    modifier = Modifier.padding(horizontal = 4.dp),
                    lineHeight = 18.sp
                )
            }
        }
        Spacer(Modifier.height(16.dp))

        // ── Text Input ──
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Text(
                    "Text",
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                    color = BbxDim
                )
                Spacer(Modifier.height(8.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 150.dp)
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(Neutral100)
                        .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                        .padding(12.dp)
                ) {
                    if (text.isEmpty()) {
                        Text(
                            if (isMulti) "[Alice] Hello, how are you?\n[Bob] I'm doing great, thanks!"
                            else "Enter text to speak...",
                            color = Neutral500,
                            style = MaterialTheme.typography.bodyLarge
                        )
                    }
                    BasicTextField(
                        value = text,
                        onValueChange = { text = it },
                        modifier = Modifier.fillMaxWidth(),
                        textStyle = MaterialTheme.typography.bodyLarge.copy(color = BbxWhite),
                        cursorBrush = SolidColor(BbxAccent)
                    )
                }
            }
        }
        Spacer(Modifier.height(10.dp))

        // ── Style Hint ──
        Text(
            "Tip: Use emotional cues like (excitedly) or (whispering) for expressive speech",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500,
            modifier = Modifier.padding(horizontal = 4.dp),
            lineHeight = 16.sp
        )
        Spacer(Modifier.height(20.dp))

        // ── Generate Button ──
        val btnInteraction = remember { MutableInteractionSource() }
        val btnPressed by btnInteraction.collectIsPressedAsState()
        val btnScale by animateFloatAsState(
            targetValue = if (btnPressed) 0.96f else 1f,
            animationSpec = tween(DurationBase, easing = EaseStandard),
            label = "btnScale"
        )
        val btnEnabled = text.isNotBlank() && !isGenerating

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
                    viewModel.generate(
                        text = text,
                        voiceName = selectedVoice,
                        isMulti = isMulti,
                        speaker1Name = speaker1Name,
                        speaker1Voice = speaker1Voice,
                        speaker2Name = speaker2Name,
                        speaker2Voice = speaker2Voice,
                        operator = "Brandon"
                    )
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
                    if (isGenerating) "Generating..." else "Generate Audio",
                    style = MaterialTheme.typography.labelLarge.copy(
                        fontWeight = FontWeight.Bold,
                        fontSize = 15.sp
                    ),
                    color = BbxWhite
                )
            }
        }

        // ── Status ──
        AnimatedVisibility(
            visible = status.isNotEmpty(),
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 12.dp)
                    .clip(RoundedCornerShape(RadiusMd))
                    .background(
                        if (status.startsWith("Error"))
                            BbxAccent.copy(alpha = 0.08f)
                        else
                            Neutral150.copy(alpha = 0.6f)
                    )
                    .padding(12.dp)
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (isGenerating) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(14.dp),
                            color = BbxAccent,
                            strokeWidth = 2.dp
                        )
                        Spacer(Modifier.width(10.dp))
                    }
                    Text(
                        status,
                        style = MaterialTheme.typography.bodySmall,
                        color = if (status.startsWith("Error")) BbxAccent else BbxDim
                    )
                }
            }
        }

        // ── Audio Player ──
        AnimatedVisibility(
            visible = audioUrl != null,
            enter = fadeIn() + scaleIn(initialScale = 0.9f),
            exit = fadeOut()
        ) {
            audioUrl?.let { url ->
                Column(modifier = Modifier.padding(top = 20.dp)) {
                    // Success banner
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(SolidGreen.copy(alpha = 0.1f))
                            .border(
                                1.dp,
                                SolidGreen.copy(alpha = 0.3f),
                                RoundedCornerShape(RadiusMd)
                            )
                            .padding(14.dp)
                    ) {
                        Column {
                            Text(
                                "Audio Ready",
                                style = MaterialTheme.typography.labelLarge.copy(
                                    fontWeight = FontWeight.Bold
                                ),
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

                    // Audio player bar
                    AudioPlayerBar(audioUrl = url)

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
                                text = ""
                            }
                            .padding(vertical = 12.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "Generate Another",
                            color = BbxAccent,
                            style = MaterialTheme.typography.labelLarge.copy(
                                fontWeight = FontWeight.Medium
                            )
                        )
                    }
                }
            }
        }

        // Bottom padding to clear the Composer overlay
        Spacer(Modifier.height(180.dp))
    }
}

// =============================================================================
// Voice Dropdown Composable
// =============================================================================

@Composable
private fun VoiceDropdown(
    selectedVoice: String,
    onVoiceSelected: (String) -> Unit
) {
    val feedback = rememberPressFeedback()
    var expanded by remember { mutableStateOf(false) }
    val selectedDesc = GEMINI_PRO_VOICES.find { it.name == selectedVoice }?.desc ?: ""

    Box(modifier = Modifier.fillMaxWidth()) {
        // Trigger
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(RadiusSm))
                .background(Neutral100)
                .border(1.dp, GlassBorder, RoundedCornerShape(RadiusSm))
                .clickFeedback { expanded = true }
                .padding(horizontal = 12.dp, vertical = 12.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Text(
                    "$selectedVoice — $selectedDesc",
                    style = MaterialTheme.typography.bodyMedium,
                    color = BbxWhite
                )
                Text(
                    "\u25BE",
                    style = MaterialTheme.typography.bodyMedium,
                    color = Neutral700
                )
            }
        }

        // Dropdown menu
        DropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false },
            modifier = Modifier
                .background(Neutral200)
                .heightIn(max = 300.dp)
        ) {
            GEMINI_PRO_VOICES.forEach { voice ->
                DropdownMenuItem(
                    text = {
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                voice.name,
                                style = MaterialTheme.typography.bodyMedium.copy(
                                    fontWeight = if (voice.name == selectedVoice)
                                        FontWeight.Bold else FontWeight.Normal
                                ),
                                color = if (voice.name == selectedVoice) BbxAccent else BbxWhite
                            )
                            Text(
                                "— ${voice.desc}",
                                style = MaterialTheme.typography.bodySmall,
                                color = Neutral500
                            )
                        }
                    },
                    onClick = {
                        feedback()
                        onVoiceSelected(voice.name)
                        expanded = false
                    },
                    modifier = Modifier.background(
                        if (voice.name == selectedVoice) Neutral300.copy(alpha = 0.5f)
                        else Color.Transparent
                    )
                )
            }
        }
    }
}
