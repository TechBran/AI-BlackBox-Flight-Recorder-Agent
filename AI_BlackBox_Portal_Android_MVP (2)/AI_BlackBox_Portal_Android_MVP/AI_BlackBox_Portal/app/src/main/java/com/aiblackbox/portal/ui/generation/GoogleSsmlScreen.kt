package com.aiblackbox.portal.ui.generation

import android.app.Application
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.performPressFeedback
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
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.ui.components.AudioPlayerBar
import com.aiblackbox.portal.ui.components.EmberBackdrop
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
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
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

// =============================================================================
// GoogleVoice — data class for parsed voice entries from /tts/voices
// =============================================================================

data class GoogleVoice(
    val name: String,
    val gender: String,
    val languageCodes: List<String>
)

// =============================================================================
// GoogleSsmlViewModel — manages voice list, language selection, and generation
// =============================================================================

class GoogleSsmlViewModel(application: Application) : AndroidViewModel(application) {

    private val store = BlackBoxStore(application)
    private var api: BlackBoxApi? = null

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
    }

    // ── State flows ──

    private val _voices = MutableStateFlow<List<GoogleVoice>>(emptyList())
    val voices: StateFlow<List<GoogleVoice>> = _voices.asStateFlow()

    private val _languages = MutableStateFlow<List<String>>(emptyList())
    val languages: StateFlow<List<String>> = _languages.asStateFlow()

    private val _selectedLanguage = MutableStateFlow("")
    val selectedLanguage: StateFlow<String> = _selectedLanguage.asStateFlow()

    private val _filteredVoices = MutableStateFlow<List<GoogleVoice>>(emptyList())
    val filteredVoices: StateFlow<List<GoogleVoice>> = _filteredVoices.asStateFlow()

    private val _status = MutableStateFlow("")
    val status: StateFlow<String> = _status.asStateFlow()

    private val _isGenerating = MutableStateFlow(false)
    val isGenerating: StateFlow<Boolean> = _isGenerating.asStateFlow()

    private val _audioUrl = MutableStateFlow<String?>(null)
    val audioUrl: StateFlow<String?> = _audioUrl.asStateFlow()

    private var currentOperator = "Brandon"

    init {
        viewModelScope.launch { store.operator.collect { currentOperator = it } }
    }

    // ── Public API ──

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        fetchVoices()
    }

    fun selectLanguage(langCode: String) {
        _selectedLanguage.value = langCode
        _filteredVoices.value = _voices.value.filter { voice ->
            voice.languageCodes.any { it == langCode }
        }
    }

    fun generate(voiceName: String, text: String, pitch: Float, rate: Float, operator: String) {
        val api = api ?: return
        if (text.isBlank() || voiceName.isBlank()) return

        _isGenerating.value = true
        _status.value = "Submitting..."
        _audioUrl.value = null

        viewModelScope.launch {
            try {
                val isSsml = text.trimStart().startsWith("<speak")

                val body = buildJsonObject {
                    put("voice_name", voiceName)
                    if (isSsml) {
                        put("ssml", text)
                    } else {
                        put("text", text)
                    }
                    put("speaking_rate", rate.toDouble())
                    put("pitch", pitch.toDouble())
                    put("operator", operator)
                }.toString()

                val response = api.post("/generate/google_ssml", body)
                val responseObj = json.parseToJsonElement(response).jsonObject
                val taskId = responseObj["task_id"]?.jsonPrimitive?.content ?: ""

                if (taskId.isBlank()) {
                    _status.value = "Error: No task ID returned"
                    _isGenerating.value = false
                    return@launch
                }

                _status.value = "Generating... (task ${taskId.take(8)})"
                pollTask(taskId)
            } catch (e: Exception) {
                _status.value = "Error: ${e.message}"
                _isGenerating.value = false
            }
        }
    }

    // ── Private helpers ──

    private fun fetchVoices() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                _status.value = "Loading voices..."
                val response = api.get("/tts/voices")
                val root = json.parseToJsonElement(response).jsonObject
                val voicesArray = root["voices"]?.jsonArray ?: return@launch

                val parsed = voicesArray.map { element ->
                    val obj = element.jsonObject
                    val name = obj["name"]?.jsonPrimitive?.content ?: ""
                    val gender = obj["ssmlGender"]?.jsonPrimitive?.content ?: ""
                    val langCodes = obj["languageCodes"]?.jsonArray?.map { lc ->
                        lc.jsonPrimitive.content
                    } ?: emptyList()
                    GoogleVoice(name = name, gender = gender, languageCodes = langCodes)
                }

                _voices.value = parsed

                val uniqueLangs = parsed
                    .flatMap { it.languageCodes }
                    .distinct()
                    .sorted()
                _languages.value = uniqueLangs

                // Auto-select en-US if available, otherwise first language
                val defaultLang = if ("en-US" in uniqueLangs) "en-US"
                else uniqueLangs.firstOrNull() ?: ""
                if (defaultLang.isNotBlank()) {
                    selectLanguage(defaultLang)
                }

                _status.value = "${parsed.size} voices loaded"
            } catch (e: Exception) {
                _status.value = "Failed to load voices: ${e.message}"
            }
        }
    }

    private suspend fun pollTask(taskId: String) {
        val api = api ?: return
        while (true) {
            try {
                val response = api.get("/tasks/status/$taskId")
                val statusObj = json.parseToJsonElement(response).jsonObject
                val taskStatus = statusObj["status"]?.jsonPrimitive?.content ?: ""

                when (taskStatus.uppercase()) {
                    "COMPLETED" -> {
                        val resultUrl = statusObj["result_url"]?.jsonPrimitive?.content
                        _audioUrl.value = resultUrl
                        _status.value = "Audio ready"
                        _isGenerating.value = false
                        return
                    }
                    "FAILED" -> {
                        val error = statusObj["error"]?.jsonPrimitive?.content ?: "Generation failed"
                        _status.value = "Error: $error"
                        _isGenerating.value = false
                        return
                    }
                    else -> {
                        _status.value = "Generating... ($taskStatus)"
                    }
                }
            } catch (e: Exception) {
                _status.value = "Poll error: ${e.message}"
                _isGenerating.value = false
                return
            }
            delay(1000)
        }
    }
}

// =============================================================================
// GoogleSsmlScreen — Composable UI for Google Cloud TTS / SSML generation
// =============================================================================

@Composable
fun GoogleSsmlScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: GoogleSsmlViewModel = viewModel()
) {
    val view = LocalView.current

    // ── Collect ViewModel state ──
    val languages by viewModel.languages.collectAsState()
    val selectedLanguage by viewModel.selectedLanguage.collectAsState()
    val filteredVoices by viewModel.filteredVoices.collectAsState()
    val status by viewModel.status.collectAsState()
    val isGenerating by viewModel.isGenerating.collectAsState()
    val audioUrl by viewModel.audioUrl.collectAsState()

    // ── Local UI state ──
    var selectedVoice by remember { mutableStateOf<GoogleVoice?>(null) }
    var textInput by remember { mutableStateOf("") }
    var pitch by remember { mutableFloatStateOf(0f) }
    var speed by remember { mutableFloatStateOf(1f) }
    var langDropdownExpanded by remember { mutableStateOf(false) }
    var voiceDropdownExpanded by remember { mutableStateOf(false) }

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    // Reset selected voice when language changes and voice is no longer in filtered list
    LaunchedEffect(filteredVoices) {
        if (selectedVoice != null && selectedVoice !in filteredVoices) {
            selectedVoice = filteredVoices.firstOrNull()
        } else if (selectedVoice == null && filteredVoices.isNotEmpty()) {
            selectedVoice = filteredVoices.first()
        }
    }

    // Ember backdrop behind the (scrolling) content while generating. The
    // overlay is a SIBLING of the scroll, so it stays fixed full-screen.
    EmberBackdrop(active = isGenerating, modifier = modifier) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 100.dp)
    ) {
        // ── Header ──
        Text(
            "Google Audio (SSML)",
            style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            color = BbxWhite
        )
        Spacer(Modifier.height(4.dp))
        Text(
            "Google Cloud Text-to-Speech",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500
        )
        Spacer(Modifier.height(16.dp))

        // ── Language Dropdown ──
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Text(
                    "Language",
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                    color = BbxDim
                )
                Spacer(Modifier.height(8.dp))
                Box {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusSm))
                            .background(Neutral100)
                            .border(1.dp, Neutral300, RoundedCornerShape(RadiusSm))
                            .clickFeedback { langDropdownExpanded = true }
                            .padding(horizontal = 12.dp, vertical = 10.dp)
                    ) {
                        Text(
                            text = selectedLanguage.ifBlank { "Select language..." },
                            style = MaterialTheme.typography.bodyMedium,
                            color = if (selectedLanguage.isNotBlank()) BbxWhite else Neutral500
                        )
                    }
                    DropdownMenu(
                        expanded = langDropdownExpanded,
                        onDismissRequest = { langDropdownExpanded = false },
                        modifier = Modifier
                            .heightIn(max = 300.dp)
                            .glassSurface(
                                shape = RoundedCornerShape(RadiusMd),
                                bg = Neutral150
                            )
                    ) {
                        languages.forEach { lang ->
                            val isSelected = lang == selectedLanguage
                            DropdownMenuItem(
                                text = {
                                    Text(
                                        lang,
                                        color = if (isSelected) BbxAccent else BbxWhite,
                                        style = MaterialTheme.typography.bodyMedium.copy(
                                            fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal
                                        )
                                    )
                                },
                                onClick = {
                                    view.performPressFeedback()
                                    viewModel.selectLanguage(lang)
                                    selectedVoice = null
                                    langDropdownExpanded = false
                                },
                                modifier = Modifier.background(
                                    if (isSelected) BbxAccent.copy(alpha = 0.1f) else Color.Transparent
                                )
                            )
                        }
                    }
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        // ── Voice Dropdown ──
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
                Box {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusSm))
                            .background(Neutral100)
                            .border(1.dp, Neutral300, RoundedCornerShape(RadiusSm))
                            .clickFeedback { voiceDropdownExpanded = true }
                            .padding(horizontal = 12.dp, vertical = 10.dp)
                    ) {
                        if (selectedVoice != null) {
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                Text(
                                    selectedVoice!!.name,
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = BbxWhite,
                                    modifier = Modifier.weight(1f)
                                )
                                Text(
                                    selectedVoice!!.gender,
                                    style = MaterialTheme.typography.labelSmall,
                                    color = Neutral700
                                )
                            }
                        } else {
                            Text(
                                "Select voice...",
                                style = MaterialTheme.typography.bodyMedium,
                                color = Neutral500
                            )
                        }
                    }
                    DropdownMenu(
                        expanded = voiceDropdownExpanded,
                        onDismissRequest = { voiceDropdownExpanded = false },
                        modifier = Modifier
                            .heightIn(max = 300.dp)
                            .glassSurface(
                                shape = RoundedCornerShape(RadiusMd),
                                bg = Neutral150
                            )
                    ) {
                        filteredVoices.forEach { voice ->
                            val isSelected = voice == selectedVoice
                            DropdownMenuItem(
                                text = {
                                    Row(
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                                        modifier = Modifier.fillMaxWidth()
                                    ) {
                                        Text(
                                            voice.name,
                                            color = if (isSelected) BbxAccent else BbxWhite,
                                            style = MaterialTheme.typography.bodyMedium.copy(
                                                fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal
                                            ),
                                            modifier = Modifier.weight(1f)
                                        )
                                        Text(
                                            voice.gender,
                                            color = if (isSelected) BbxAccent.copy(alpha = 0.7f) else Neutral700,
                                            style = MaterialTheme.typography.labelSmall
                                        )
                                    }
                                },
                                onClick = {
                                    view.performPressFeedback()
                                    selectedVoice = voice
                                    voiceDropdownExpanded = false
                                },
                                modifier = Modifier.background(
                                    if (isSelected) BbxAccent.copy(alpha = 0.1f) else Color.Transparent
                                )
                            )
                        }
                    }
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        // ── Text / SSML Input ──
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Text(
                    "Text or SSML",
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
                        .border(1.dp, Neutral300, RoundedCornerShape(RadiusMd))
                        .padding(12.dp)
                ) {
                    if (textInput.isEmpty()) {
                        Text(
                            "Enter text or SSML markup...",
                            color = Neutral500,
                            style = MaterialTheme.typography.bodyMedium
                        )
                    }
                    BasicTextField(
                        value = textInput,
                        onValueChange = { textInput = it },
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(min = 150.dp),
                        textStyle = MaterialTheme.typography.bodyMedium.copy(
                            color = BbxWhite,
                            fontFamily = if (textInput.trimStart().startsWith("<speak"))
                                FontFamily.Monospace else FontFamily.Default
                        ),
                        cursorBrush = SolidColor(BbxAccent)
                    )
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        // ── Pitch Slider ──
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        "Pitch",
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                        color = BbxDim
                    )
                    Text(
                        "%.1f".format(pitch),
                        style = MaterialTheme.typography.labelMedium.copy(
                            fontFamily = FontFamily.Monospace
                        ),
                        color = Neutral700
                    )
                }
                Spacer(Modifier.height(4.dp))
                Slider(
                    value = pitch,
                    onValueChange = { pitch = it },
                    valueRange = -20f..20f,
                    steps = ((20f - (-20f)) / 0.1f).toInt() - 1,
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
                    Text("-20", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                    Text("0", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                    Text("+20", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        // ── Speed Slider ──
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        "Speed",
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                        color = BbxDim
                    )
                    Text(
                        "%.2fx".format(speed),
                        style = MaterialTheme.typography.labelMedium.copy(
                            fontFamily = FontFamily.Monospace
                        ),
                        color = Neutral700
                    )
                }
                Spacer(Modifier.height(4.dp))
                Slider(
                    value = speed,
                    onValueChange = { speed = it },
                    valueRange = 0.25f..4f,
                    steps = ((4f - 0.25f) / 0.05f).toInt() - 1,
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
                    Text("0.25x", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                    Text("1x", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                    Text("4x", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                }
            }
        }
        Spacer(Modifier.height(20.dp))

        // ── Generate Button ──
        val canGenerate = textInput.isNotBlank() && selectedVoice != null && !isGenerating

        Box(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(RadiusLg))
                .background(
                    if (canGenerate) BbxAccent else BbxAccent.copy(alpha = 0.4f)
                )
                .clickFeedback(enabled = canGenerate) {
                    selectedVoice?.let { voice ->
                        viewModel.generate(
                            voiceName = voice.name,
                            text = textInput,
                            pitch = pitch,
                            rate = speed,
                            operator = "Brandon"
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
                    if (isGenerating) "Generating..." else "Generate Audio",
                    style = MaterialTheme.typography.labelLarge.copy(
                        fontWeight = FontWeight.Bold,
                        fontSize = 15.sp
                    ),
                    color = BbxWhite
                )
            }
        }

        // ── Status Text ──
        AnimatedVisibility(
            visible = status.isNotBlank(),
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Text(
                status,
                style = MaterialTheme.typography.bodySmall,
                color = Neutral500,
                modifier = Modifier.padding(top = 10.dp)
            )
        }

        // ── Audio Player ──
        AnimatedVisibility(
            visible = audioUrl != null,
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            audioUrl?.let { url ->
                Column(modifier = Modifier.padding(top = 16.dp)) {
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
                        Text(
                            "Audio Ready",
                            style = MaterialTheme.typography.labelLarge.copy(
                                fontWeight = FontWeight.Bold
                            ),
                            color = SolidGreen
                        )
                    }
                    Spacer(Modifier.height(12.dp))

                    // Audio player bar
                    AudioPlayerBar(audioUrl = url)
                }
            }
        }

        // Bottom padding to clear the Composer overlay
        Spacer(Modifier.height(180.dp))
    }
    }
}
