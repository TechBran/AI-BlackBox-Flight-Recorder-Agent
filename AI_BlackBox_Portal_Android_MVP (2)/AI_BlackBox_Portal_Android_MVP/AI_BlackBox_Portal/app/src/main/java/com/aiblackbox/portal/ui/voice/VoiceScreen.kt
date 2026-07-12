package com.aiblackbox.portal.ui.voice

import android.Manifest
import android.app.Application
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.util.Base64
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.ui.text.input.KeyboardType
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
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.voice.TranscriptEntry
import com.aiblackbox.portal.data.voice.VoiceAgentPreset
import com.aiblackbox.portal.data.voice.VoiceAgentPresets
import com.aiblackbox.portal.data.voice.VoiceBackend
import com.aiblackbox.portal.data.voice.VoiceCatalog
import com.aiblackbox.portal.data.voice.isChipRole
import com.aiblackbox.portal.data.voice.mergeTranscript
import com.aiblackbox.portal.data.voice.modelsOrFallback
import com.aiblackbox.portal.data.voice.shouldHoldMic
import com.aiblackbox.portal.data.voice.toolChipText
import com.aiblackbox.portal.data.voice.voicesOrFallback
import com.aiblackbox.portal.data.voice.VoiceClient
import com.aiblackbox.portal.data.voice.VoiceEvent
import com.aiblackbox.portal.data.voice.VoiceSessionConfig
import com.aiblackbox.portal.data.voice.VoiceState
import com.aiblackbox.portal.util.Constants
import com.aiblackbox.portal.ui.components.ContextProvenance
import com.aiblackbox.portal.ui.components.SnapshotPeekSheet
import android.view.HapticFeedbackConstants
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral250
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface
import androidx.compose.foundation.border
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

enum class WaveSpeaker { USER, AI, IDLE }

class VoiceViewModel(application: Application) : AndroidViewModel(application) {
    private val store = BlackBoxStore(application)
    private val audioManager = application.getSystemService(android.content.Context.AUDIO_SERVICE) as android.media.AudioManager
    private var voiceClient: VoiceClient? = null

    // P3.12: per-provider catalogs hydrated from GET {statusPath} at screen open.
    private val _catalogs = MutableStateFlow<Map<VoiceBackend, VoiceCatalog>>(emptyMap())
    val catalogs: StateFlow<Map<VoiceBackend, VoiceCatalog>> = _catalogs.asStateFlow()
    private var catalogFetchJob: Job? = null

    private val _backend = MutableStateFlow(VoiceBackend.GEMINI_LIVE)
    val backend: StateFlow<VoiceBackend> = _backend.asStateFlow()

    private val _voice = MutableStateFlow(Constants.DEFAULT_GEMINI_LIVE_VOICE)
    val voice: StateFlow<String> = _voice.asStateFlow()

    // ── P3.13: persisted voice-agent settings (DataStore write-through via
    // BlackBoxStore.getString/setString; keys prefixed "va_"). null ↔ "".
    private val _realtimeModel = MutableStateFlow(Constants.LIVE_MODEL_DEFAULTS["realtime"] ?: "")
    val realtimeModel: StateFlow<String> = _realtimeModel.asStateFlow()
    private val _realtimeVadType = MutableStateFlow("server_vad")
    val realtimeVadType: StateFlow<String> = _realtimeVadType.asStateFlow()
    private val _realtimeVadEagerness = MutableStateFlow("medium")
    val realtimeVadEagerness: StateFlow<String> = _realtimeVadEagerness.asStateFlow()
    private val _realtimeIdleTimeoutText = MutableStateFlow("")
    val realtimeIdleTimeoutText: StateFlow<String> = _realtimeIdleTimeoutText.asStateFlow()
    private val _geminiModel = MutableStateFlow(Constants.LIVE_MODEL_DEFAULTS["gemini-live"] ?: "")
    val geminiModel: StateFlow<String> = _geminiModel.asStateFlow()
    private val _geminiVadStart = MutableStateFlow<String?>(null)
    val geminiVadStart: StateFlow<String?> = _geminiVadStart.asStateFlow()
    private val _geminiVadEnd = MutableStateFlow<String?>(null)
    val geminiVadEnd: StateFlow<String?> = _geminiVadEnd.asStateFlow()
    private val _geminiThinkingLevel = MutableStateFlow<String?>(null)
    val geminiThinkingLevel: StateFlow<String?> = _geminiThinkingLevel.asStateFlow()
    private val _grokModel = MutableStateFlow(Constants.LIVE_MODEL_DEFAULTS["grok-live"] ?: "")
    val grokModel: StateFlow<String> = _grokModel.asStateFlow()
    private val _grokReasoningEffort = MutableStateFlow<String?>(null)
    val grokReasoningEffort: StateFlow<String?> = _grokReasoningEffort.asStateFlow()
    private val _selectedPresetId = MutableStateFlow("")
    val selectedPresetId: StateFlow<String> = _selectedPresetId.asStateFlow()
    // ── Translation mode (P6a) — OpenAI + Gemini only; Grok has no translate model.
    // DataStore-persisted like every sibling setting (P3.13 pattern; keys "va_").
    private val _translateEnabled = MutableStateFlow(false)
    val translateEnabled: StateFlow<Boolean> = _translateEnabled.asStateFlow()
    private val _translateLang = MutableStateFlow("es")
    val translateLang: StateFlow<String> = _translateLang.asStateFlow()
    private val _translateLangOther = MutableStateFlow("")
    val translateLangOther: StateFlow<String> = _translateLangOther.asStateFlow()
    // P3.13: voice-agent preset roster from GET /voice-agents (P4 registry;
    // 404-tolerant — empty list pre-P4, dropdown hides). P4.11 builds on this fetch.
    private val _presets = MutableStateFlow<List<VoiceAgentPreset>>(emptyList())
    val presets: StateFlow<List<VoiceAgentPreset>> = _presets.asStateFlow()

    private val _voiceState = MutableStateFlow(VoiceState.DISCONNECTED)
    val voiceState: StateFlow<VoiceState> = _voiceState.asStateFlow()

    private val _transcript = MutableStateFlow<List<TranscriptEntry>>(emptyList())
    val transcript: StateFlow<List<TranscriptEntry>> = _transcript.asStateFlow()

    // P3.17: server transcript + locally-injected entries (tool chips, typed text).
    private val _serverTranscript = MutableStateFlow<List<TranscriptEntry>>(emptyList())
    private val _localEntries = MutableStateFlow<List<TranscriptEntry>>(emptyList())

    // Plan Task 10: typed retrieval provenance from the voice WS dispatcher.
    private val _provenance = MutableStateFlow<Provenance?>(null)
    val provenance: StateFlow<Provenance?> = _provenance.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    // P3.16: transient server status line ("Connecting to Gemini Live...", etc).
    private val _statusText = MutableStateFlow("")
    val statusText: StateFlow<String> = _statusText.asStateFlow()

    // Mic state
    private val _isMicActive = MutableStateFlow(false)
    val isMicActive: StateFlow<Boolean> = _isMicActive.asStateFlow()

    // Live waveform inputs — real RMS amplitude (0f..1f) + who is speaking.
    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    // Two writers by design: the mic loop sets USER (only when isAISpeaking != true) and the
    // playback drain sets AI; stopMic()/stopAudioPlayback() reset it back to IDLE when idle.
    private val _waveSpeaker = MutableStateFlow(WaveSpeaker.IDLE)
    val waveSpeaker: StateFlow<WaveSpeaker> = _waveSpeaker.asStateFlow()

    private var currentOperator = ""  // empty-until-store-emits (never hard-code operator; fresh-box rule)

    // Audio I/O
    private var audioRecord: AudioRecord? = null
    private var audioTrack: AudioTrack? = null
    private val audioTrackLock = Object()
    @Volatile private var isRecordingAudio = false

    // Decoupled audio playback queue (matching OverlayService pattern)
    private val audioPlaybackQueue = java.util.concurrent.ConcurrentLinkedQueue<ByteArray>()
    private var audioPlaybackJob: Job? = null
    private var audioCollectorJob: Job? = null  // Must be cancelled to prevent duplicate collectors
    private var aiEnvelopeJob: Job? = null       // Releases (decays) the AI waveform between chunks

    // Pre-buffering state
    @Volatile private var preBufferAccumulated = 0
    @Volatile private var preBufferReady = false

    companion object {
        const val PRE_BUFFER_THRESHOLD_BYTES = 12_000  // ~250ms at 24kHz mono PCM16
        // Below this RMS the user is considered silent → waveform returns to IDLE (breathing).
        const val USER_SPEECH_THRESHOLD = 0.02f
        // AI waveform envelope release per ~33ms frame (attack = chunk RMS via maxOf).
        // Lets the ribbon fall during the model's pauses instead of freezing at the
        // last chunk's level — halves roughly every 140ms.
        const val AI_AMP_RELEASE = 0.83f  // fall rate (~halves every 125ms): fluid but still tracks the model
    }

    init {
        viewModelScope.launch { store.operator.collect { currentOperator = it } }
        // P3.13: one-shot restore of persisted voice-agent settings.
        viewModelScope.launch {
            val savedBackend = store.getString("va_backend").first()
            VoiceBackend.entries.firstOrNull { it.id == savedBackend }?.let { _backend.value = it }
            val savedVoice = store.getString("va_voice_${_backend.value.id}").first()
            _voice.value = savedVoice.ifBlank { defaultVoiceFor(_backend.value) }
            store.getString("va_model_realtime").first().takeIf { it.isNotBlank() }?.let { _realtimeModel.value = it }
            store.getString("va_vad_type").first().takeIf { it.isNotBlank() }?.let { _realtimeVadType.value = it }
            store.getString("va_vad_eagerness").first().takeIf { it.isNotBlank() }?.let { _realtimeVadEagerness.value = it }
            store.getString("va_idle_timeout").first().takeIf { it.isNotBlank() }?.let { _realtimeIdleTimeoutText.value = it }
            store.getString("va_model_gemini-live").first().takeIf { it.isNotBlank() }?.let { _geminiModel.value = it }
            store.getString("va_gem_vad_start").first().takeIf { it.isNotBlank() }?.let { _geminiVadStart.value = it }
            store.getString("va_gem_vad_end").first().takeIf { it.isNotBlank() }?.let { _geminiVadEnd.value = it }
            store.getString("va_gem_thinking").first().takeIf { it.isNotBlank() }?.let { _geminiThinkingLevel.value = it }
            store.getString("va_model_grok-live").first().takeIf { it.isNotBlank() }?.let { _grokModel.value = it }
            store.getString("va_grok_effort").first().takeIf { it.isNotBlank() }?.let { _grokReasoningEffort.value = it }
            store.getString("va_preset").first().takeIf { it.isNotBlank() }?.let { _selectedPresetId.value = it }
            _translateEnabled.value = store.getString("va_translate_on").first() == "true"
            store.getString("va_translate_lang").first().takeIf { it.isNotBlank() }?.let { _translateLang.value = it }
            store.getString("va_translate_lang_other").first().takeIf { it.isNotBlank() }?.let { _translateLangOther.value = it }
        }
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || voiceClient != null) return
        try {
            val wsUrl = origin.replace("https://", "wss://").replace("http://", "ws://")
            voiceClient = VoiceClient(BlackBoxApi(origin).getClient(), wsUrl)

            // P3.12: hydrate models/voices/model_default/presets from the /status
            // endpoints (provider-API-as-SoT). Constants lists are fallbacks-only.
            catalogFetchJob?.cancel()
            catalogFetchJob = viewModelScope.launch(Dispatchers.IO) {
                val api = BlackBoxApi(origin)
                VoiceBackend.entries.forEach { b ->
                    try {
                        VoiceCatalog.parse(api.get(b.statusPath))?.let { cat ->
                            _catalogs.value = _catalogs.value + (b to cat)
                            cat.modelDefault?.let { def ->
                                when (b) {
                                    VoiceBackend.GPT_REALTIME ->
                                        if (store.getString("va_model_realtime").first().isBlank()) _realtimeModel.value = def
                                    VoiceBackend.GEMINI_LIVE ->
                                        if (store.getString("va_model_gemini-live").first().isBlank()) _geminiModel.value = def
                                    VoiceBackend.GROK_LIVE ->
                                        if (store.getString("va_model_grok-live").first().isBlank()) _grokModel.value = def
                                }
                            }
                            android.util.Log.d("VoiceVM", "Catalog ${b.id}: " +
                                "${cat.models.size} models, ${cat.voices.size} voices, " +
                                "${cat.presets.size} presets, default=${cat.modelDefault}")
                        }
                    } catch (e: Exception) {
                        android.util.Log.w("VoiceVM", "Catalog fetch ${b.id} failed: ${e.message}")
                    }
                }
            }

            // P3.13: hydrate voice-agent presets from GET /voice-agents
            // ({"agents":[{id,name,provider,...}]}). 404-tolerant pre-P4.
            viewModelScope.launch(Dispatchers.IO) {
                try {
                    _presets.value = VoiceAgentPresets.parse(BlackBoxApi(origin).get("/voice-agents"))
                    android.util.Log.d("VoiceVM", "Presets: ${_presets.value.size}")
                } catch (e: Exception) {
                    android.util.Log.w("VoiceVM", "voice-agents fetch failed (pre-P4 box?): ${e.message}")
                }
            }

            // Collect state changes
            viewModelScope.launch {
                voiceClient?.state?.collect { state ->
                    _voiceState.value = state
                    if (state == VoiceState.CONNECTED) _statusText.value = ""
                    // Auto-init audio when connected (matches OverlayService onOpen)
                    if (state == VoiceState.CONNECTED && audioTrack == null) {
                        try {
                            initAudioPlayback()
                            delay(200)
                            startMic()
                        } catch (e: Exception) {
                            android.util.Log.e("VoiceVM", "Audio init: ${e.message}", e)
                            _error.value = "Audio init failed: ${e.message}"
                        }
                    }
                    // Clean up audio on disconnect
                    if (state == VoiceState.DISCONNECTED || state == VoiceState.ERROR) {
                        stopMic()
                        stopAudioPlayback()
                    }
                }
            }
            // Collect transcripts (P3.17: server transcript merged with local chips/typed text)
            viewModelScope.launch {
                voiceClient?.transcript?.collect {
                    _serverTranscript.value = it
                    _transcript.value = mergeTranscript(it, _localEntries.value)
                }
            }
            // Plan Task 10: collect retrieval provenance pushed by the WS dispatcher
            viewModelScope.launch {
                voiceClient?.provenance?.collect { _provenance.value = it }
            }
            // Collect health/status events (P3.16)
            viewModelScope.launch {
                voiceClient?.events?.collect { event ->
                    when (event) {
                        is VoiceEvent.Error -> _error.value = event.message
                        is VoiceEvent.Status -> _statusText.value = event.message
                        is VoiceEvent.Tool -> addLocalEntry(
                            TranscriptEntry(role = event.kind,
                                text = toolChipText(event.kind, event.name, event.detail))
                        )
                        else -> Unit
                    }
                }
            }
            android.util.Log.d("VoiceVM", "Initialized: wsUrl=$wsUrl")
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "Initialize failed: ${e.message}", e)
            _error.value = "Init failed: ${e.message}"
        }
    }

    private fun persist(key: String, value: String) {
        viewModelScope.launch { store.setString(key, value) }
    }

    private fun defaultVoiceFor(backend: VoiceBackend): String = when (backend) {
        VoiceBackend.GPT_REALTIME -> Constants.DEFAULT_GPT_REALTIME_VOICE
        VoiceBackend.GEMINI_LIVE -> Constants.DEFAULT_GEMINI_LIVE_VOICE
        VoiceBackend.GROK_LIVE -> Constants.DEFAULT_GROK_LIVE_VOICE
    }

    fun setBackend(backend: VoiceBackend) {
        _backend.value = backend
        persist("va_backend", backend.id)
        // Restore this backend's persisted voice, else its canonical default.
        viewModelScope.launch {
            val saved = store.getString("va_voice_${backend.id}").first()
            _voice.value = saved.ifBlank { defaultVoiceFor(backend) }
        }
    }

    fun setVoice(voice: String) {
        _voice.value = voice
        persist("va_voice_${_backend.value.id}", voice)
    }

    fun setRealtimeModel(v: String) { _realtimeModel.value = v; persist("va_model_realtime", v) }
    fun setRealtimeVadType(v: String) { _realtimeVadType.value = v; persist("va_vad_type", v) }
    fun setRealtimeVadEagerness(v: String) { _realtimeVadEagerness.value = v; persist("va_vad_eagerness", v) }
    fun setRealtimeIdleTimeout(v: String) { _realtimeIdleTimeoutText.value = v; persist("va_idle_timeout", v) }
    fun setGeminiModel(v: String) { _geminiModel.value = v; persist("va_model_gemini-live", v) }
    fun setGeminiVadStart(v: String?) { _geminiVadStart.value = v; persist("va_gem_vad_start", v ?: "") }
    fun setGeminiVadEnd(v: String?) { _geminiVadEnd.value = v; persist("va_gem_vad_end", v ?: "") }
    fun setGeminiThinkingLevel(v: String?) { _geminiThinkingLevel.value = v; persist("va_gem_thinking", v ?: "") }
    fun setGrokModel(v: String) { _grokModel.value = v; persist("va_model_grok-live", v) }
    fun setGrokReasoningEffort(v: String?) { _grokReasoningEffort.value = v; persist("va_grok_effort", v ?: "") }
    fun setPreset(id: String) { _selectedPresetId.value = id; persist("va_preset", id) }
    fun setTranslateEnabled(v: Boolean) { _translateEnabled.value = v; persist("va_translate_on", if (v) "true" else "false") }
    fun setTranslateLang(v: String) { _translateLang.value = v; persist("va_translate_lang", v) }
    fun setTranslateLangOther(v: String) { _translateLangOther.value = v; persist("va_translate_lang_other", v) }

    private fun resolvedTranslateLang(): String =
        if (_translateLang.value == "__other__") _translateLangOther.value.trim().ifBlank { "en" }
        else _translateLang.value

    /** P3.13: assemble the per-provider session config from persisted settings. */
    fun buildSessionConfig(): VoiceSessionConfig? {
        val preset = _selectedPresetId.value.takeIf { it.isNotBlank() }
        return when (_backend.value) {
            VoiceBackend.GPT_REALTIME -> VoiceSessionConfig(
                model = _realtimeModel.value.takeIf { it.isNotBlank() },
                vadType = _realtimeVadType.value.takeIf { it.isNotBlank() },
                vadEagerness = if (_realtimeVadType.value == "semantic_vad") _realtimeVadEagerness.value else null,
                idleTimeoutMs = if (_realtimeVadType.value == "server_vad")
                    _realtimeIdleTimeoutText.value.trim().toIntOrNull() else null,
                agentId = preset,
                mode = if (_translateEnabled.value) "translate" else null,
                targetLanguage = if (_translateEnabled.value) resolvedTranslateLang() else null,
            )
            VoiceBackend.GEMINI_LIVE -> {
                val thinkingAllowed = _geminiModel.value in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS
                VoiceSessionConfig(
                    model = _geminiModel.value.takeIf { it.isNotBlank() },
                    vadStart = _geminiVadStart.value,
                    vadEnd = _geminiVadEnd.value,
                    thinkingLevel = if (thinkingAllowed) _geminiThinkingLevel.value else null,
                    agentId = preset,
                    mode = if (_translateEnabled.value) "translate" else null,
                    targetLanguage = if (_translateEnabled.value) resolvedTranslateLang() else null,
                )
            }
            VoiceBackend.GROK_LIVE -> VoiceSessionConfig(
                model = _grokModel.value.takeIf { it.isNotBlank() },
                reasoningEffort = _grokReasoningEffort.value,
                agentId = preset,
            )
        }
    }

    fun connect() {
        val sessionConfig = buildSessionConfig()
        _error.value = null
        stopMic()
        stopAudioPlayback()
        _transcript.value = emptyList()
        _serverTranscript.value = emptyList()
        _localEntries.value = emptyList()
        _provenance.value = null
        // Enable communication mode for strong system-level AEC
        audioManager.mode = android.media.AudioManager.MODE_IN_COMMUNICATION
        // Force loudspeaker output (not earpiece) — use modern API on Android 12+
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
            val speakers = audioManager.availableCommunicationDevices
                .filter { it.type == android.media.AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
            if (speakers.isNotEmpty()) {
                audioManager.setCommunicationDevice(speakers.first())
                android.util.Log.d("VoiceVM", "Routed communication audio to loudspeaker via setCommunicationDevice")
            }
        } else {
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = true
        }
        voiceClient?.connect(_backend.value, currentOperator, _voice.value, viewModelScope, sessionConfig)
    }

    fun disconnect() {
        stopMic()
        stopAudioPlayback()
        try {
            voiceClient?.disconnect()
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "Disconnect: ${e.message}")
        }
        // Restore normal audio routing
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
            audioManager.clearCommunicationDevice()
        } else {
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = false
        }
        audioManager.mode = android.media.AudioManager.MODE_NORMAL
        _voiceState.value = VoiceState.DISCONNECTED
    }

    fun toggleMic() {
        if (isRecordingAudio) stopMic() else startMic()
    }

    /** P3.14 barge-in: flush queued AI audio locally + cancel the response server-side. */
    fun interrupt() {
        try { voiceClient?.sendInterrupt() } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "interrupt: ${e.message}")
        }
        audioPlaybackQueue.clear()
        preBufferAccumulated = 0
        preBufferReady = false
        synchronized(audioTrackLock) {
            try {
                audioTrack?.pause()
                audioTrack?.flush()
                audioTrack?.play()
            } catch (_: Exception) {}
        }
        _amplitude.value = 0f
        _waveSpeaker.value = WaveSpeaker.IDLE
    }

    private fun addLocalEntry(entry: TranscriptEntry) {
        _localEntries.value = _localEntries.value + entry
        _transcript.value = mergeTranscript(_serverTranscript.value, _localEntries.value)
    }

    /** P3.18: typed text during a voice session — shows as a local user bubble. */
    fun sendTypedText(text: String) {
        val t = text.trim()
        if (t.isEmpty()) return
        try {
            voiceClient?.sendText(t)
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "sendText: ${e.message}")
            _error.value = "Send failed: ${e.message}"
            return
        }
        addLocalEntry(TranscriptEntry(role = "user", text = t))
    }

    // -------------------------------------------------------------------------
    // Mic input — AudioRecord -> base64 PCM16 -> WebSocket
    // -------------------------------------------------------------------------
    fun startMic() {
        val app = getApplication<Application>()
        if (ContextCompat.checkSelfPermission(app, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            _error.value = "Microphone permission required"
            return
        }

        val sampleRate = when (_backend.value) {
            VoiceBackend.GPT_REALTIME -> 24000
            else -> 16000
        }

        val bufferSize = AudioRecord.getMinBufferSize(
            sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        ) * 2

        try {
            // Use VOICE_COMMUNICATION for stronger echo cancellation —
            // it uses speaker output as AEC reference signal (critical for voice agent)
            val record = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferSize
            )

            // Check STATE_INITIALIZED before starting
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                android.util.Log.e("VoiceVM", "AudioRecord failed to initialize")
                record.release()
                _error.value = "Microphone initialization failed"
                return
            }

            // Attach explicit AcousticEchoCanceler + NoiseSuppressor for devices
            // where the platform doesn't enable them automatically
            try {
                if (android.media.audiofx.AcousticEchoCanceler.isAvailable()) {
                    val aec = android.media.audiofx.AcousticEchoCanceler.create(record.audioSessionId)
                    aec?.enabled = true
                    android.util.Log.d("VoiceVM", "AcousticEchoCanceler enabled")
                }
                if (android.media.audiofx.NoiseSuppressor.isAvailable()) {
                    val ns = android.media.audiofx.NoiseSuppressor.create(record.audioSessionId)
                    ns?.enabled = true
                    android.util.Log.d("VoiceVM", "NoiseSuppressor enabled")
                }
            } catch (e: Exception) {
                android.util.Log.w("VoiceVM", "Audio effects not available: ${e.message}")
            }

            audioRecord = record
            record.startRecording()
            isRecordingAudio = true
            _isMicActive.value = true
            android.util.Log.d("VoiceVM", "Mic started: ${sampleRate}Hz, buffer=$bufferSize")

            viewModelScope.launch(Dispatchers.IO) {
                var wasSendingAudio = false
                try {
                    val buffer = ShortArray(bufferSize / 2)
                    while (isRecordingAudio) {
                        // Read via the LOCAL record (not the nullable field) so a
                        // concurrent stopMic() can never swap/null it mid-read.
                        val readCount = record.read(buffer, 0, buffer.size)
                        if (readCount > 0) {
                            val amp = rmsAmplitude(buffer, readCount)
                            // P3.15: provider-conditional mic hold — Grok holds during AI
                            // speech (echo-prone); OpenAI/Gemini stay open behind AEC so
                            // server VAD hears barge-ins. Do NOT send audio_commit here.
                            val client = voiceClient
                            if (client != null) {
                                val timeSinceStop = System.currentTimeMillis() - client.aiStoppedSpeakingAt
                                if (shouldHoldMic(_backend.value, client.isAISpeaking.value, timeSinceStop)) {
                                    wasSendingAudio = false
                                    continue
                                }
                            }

                            // Convert shorts to little-endian bytes
                            val bytes = ByteArray(readCount * 2)
                            for (i in 0 until readCount) {
                                bytes[i * 2] = (buffer[i].toInt() and 0xFF).toByte()
                                bytes[i * 2 + 1] = (buffer[i].toInt() shr 8 and 0xFF).toByte()
                            }
                            val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                            try {
                                voiceClient?.sendAudioChunk(base64)
                                wasSendingAudio = true
                                _amplitude.value = amp
                                if (voiceClient?.isAISpeaking?.value != true) {
                                    _waveSpeaker.value =
                                        if (amp > USER_SPEECH_THRESHOLD) WaveSpeaker.USER
                                        else WaveSpeaker.IDLE
                                }
                            } catch (e: Exception) {
                                android.util.Log.e("VoiceVM", "Send audio chunk failed: ${e.message}")
                            }
                        }
                    }
                    // Send audio_commit when mic loop ends
                    if (wasSendingAudio) {
                        voiceClient?.sendAudioCommit()
                    }
                } catch (e: Exception) {
                    android.util.Log.e("VoiceVM", "Mic loop error: ${e.message}", e)
                    isRecordingAudio = false
                    _isMicActive.value = false
                } finally {
                    // Release the AudioRecord on the SAME thread as read(). Doing
                    // it from stopMic() on the caller thread raced an in-flight
                    // read() and aborted natively in AudioRecord::releaseBuffer
                    // (SIGABRT crash on "start speaking", fixed 2026-06-06).
                    try { record.stop() } catch (_: Exception) {}
                    try { record.release() } catch (_: Exception) {}
                    if (audioRecord === record) audioRecord = null
                }
            }
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "Mic start failed: ${e.message}", e)
            _error.value = "Mic start failed: ${e.message}"
        }
    }

    fun stopMic() {
        // Only SIGNAL the loop to stop; it sends audio_commit then releases the
        // AudioRecord in its own finally (same thread as read). Releasing here on
        // the caller thread raced an in-flight read() and crashed natively in
        // AudioRecord::releaseBuffer (fixed 2026-06-06).
        isRecordingAudio = false
        _isMicActive.value = false
        _amplitude.value = 0f
        if (_waveSpeaker.value == WaveSpeaker.USER) _waveSpeaker.value = WaveSpeaker.IDLE
        android.util.Log.d("VoiceVM", "Mic stop requested")
    }

    // -------------------------------------------------------------------------
    // Audio playback — decoupled queue matching OverlayService pattern
    // -------------------------------------------------------------------------
    private fun initAudioPlayback() {
        val client = voiceClient ?: return

        synchronized(audioTrackLock) {
            try {
                audioTrack?.stop()
                audioTrack?.release()
            } catch (_: Exception) {}
            audioTrack = null
        }

        // Reset pre-buffer state
        audioPlaybackQueue.clear()
        preBufferAccumulated = 0
        preBufferReady = false

        val outputSampleRate = 24000
        val channelConfig = AudioFormat.CHANNEL_OUT_MONO
        val audioFormat = AudioFormat.ENCODING_PCM_16BIT

        val minBufferSize = AudioTrack.getMinBufferSize(outputSampleRate, channelConfig, audioFormat)

        if (minBufferSize == AudioTrack.ERROR || minBufferSize == AudioTrack.ERROR_BAD_VALUE) {
            android.util.Log.e("VoiceVM", "Invalid AudioTrack buffer size: $minBufferSize")
            _error.value = "Audio output not available"
            return
        }

        val bufferSize = maxOf(minBufferSize * 4, 16384)

        try {
            // USAGE_VOICE_COMMUNICATION pairs with MODE_IN_COMMUNICATION for full AEC pipeline.
            // setCommunicationDevice(BUILTIN_SPEAKER) ensures loudspeaker routing.
            val track = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(outputSampleRate)
                        .setChannelMask(channelConfig)
                        .setEncoding(audioFormat)
                        .build()
                )
                .setBufferSizeInBytes(bufferSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()

            if (track.state != AudioTrack.STATE_INITIALIZED) {
                android.util.Log.e("VoiceVM", "AudioTrack failed to initialize")
                track.release()
                _error.value = "Audio output failed to initialize"
                return
            }

            synchronized(audioTrackLock) {
                audioTrack = track
                track.play()
            }
            android.util.Log.d("VoiceVM", "AudioTrack initialized: ${outputSampleRate}Hz, buffer=$bufferSize")

            // Start playback drain immediately — it gates on preBufferReady internally
            startPlaybackDrain()

            // Decoupled audio collector — receives chunks into queue (never blocks)
            audioCollectorJob?.cancel()
            audioCollectorJob = viewModelScope.launch(Dispatchers.IO) {
                client.audioOutput.collect { base64Chunk ->
                    try {
                        val pcmBytes = Base64.decode(base64Chunk, Base64.NO_WRAP)
                        audioPlaybackQueue.offer(pcmBytes)

                        // Track pre-buffer accumulation
                        if (!preBufferReady) {
                            preBufferAccumulated += pcmBytes.size
                            if (preBufferAccumulated >= PRE_BUFFER_THRESHOLD_BYTES) {
                                preBufferReady = true
                            }
                        }
                    } catch (e: Exception) {
                        android.util.Log.e("VoiceVM", "Audio decode error: ${e.message}")
                    }
                }
            }
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "AudioTrack creation failed: ${e.message}", e)
            _error.value = "Audio output error: ${e.message}"
        }
    }

    // Dedicated playback drain loop — writes to AudioTrack from queue
    // Starts immediately; gates on preBufferReady so short responses still play
    private fun startPlaybackDrain() {
        audioPlaybackJob?.cancel()
        audioPlaybackJob = viewModelScope.launch(Dispatchers.IO) {
            android.util.Log.d("VoiceVM", "Playback drain started")
            try {
                while (isActive) {
                    // Wait for pre-buffer threshold OR response_complete (queue has data but AI stopped)
                    if (!preBufferReady) {
                        val hasData = !audioPlaybackQueue.isEmpty()
                        val aiDone = voiceClient?.isAISpeaking?.value != true && hasData
                        if (!aiDone) {
                            delay(10)
                            continue
                        }
                        // AI finished with < 12KB — play what we have
                        preBufferReady = true
                        android.util.Log.d("VoiceVM", "Pre-buffer bypassed (AI done, ${preBufferAccumulated}B)")
                    }

                    val chunk = audioPlaybackQueue.poll()
                    if (chunk != null) {
                        synchronized(audioTrackLock) {
                            val track = audioTrack ?: return@synchronized
                            if (track.state == AudioTrack.STATE_INITIALIZED) {
                                track.write(chunk, 0, chunk.size)
                                // Sample loudness at the moment the chunk is committed to the
                                // audio device, so the waveform stays in sync with what's heard
                                // (the ~12KB pre-buffer + drain otherwise lag enqueue by ~256ms).
                                // Attack: jump UP to this chunk's loudness; the envelope job
                                // releases between chunks so quiet syllables + pauses show.
                                _waveSpeaker.value = WaveSpeaker.AI
                                _amplitude.value = maxOf(_amplitude.value, rmsAmplitudeFromBytes(chunk))
                            }
                        }
                    } else {
                        if (voiceClient?.isAISpeaking?.value != true) {
                            delay(50)
                            if (audioPlaybackQueue.isEmpty()) {
                                delay(100)
                            }
                        } else {
                            delay(5)
                        }
                    }
                }
            } catch (e: Exception) {
                if (e !is kotlinx.coroutines.CancellationException) {
                    android.util.Log.e("VoiceVM", "Playback drain error: ${e.message}", e)
                }
            }
        }

        // AI waveform envelope: release amplitude toward 0 each frame so the ribbon
        // tracks the model's cadence and dips during pauses. Attacks come from the
        // chunk writes above (maxOf). Guarded on speaker == AI so the mic path is intact.
        aiEnvelopeJob?.cancel()
        aiEnvelopeJob = viewModelScope.launch {
            while (isActive) {
                delay(33)
                if (_waveSpeaker.value == WaveSpeaker.AI) {
                    val c = _amplitude.value
                    _amplitude.value = if (c > 0.001f) c * AI_AMP_RELEASE else 0f
                }
            }
        }
    }

    private fun stopAudioPlayback() {
        _amplitude.value = 0f
        _waveSpeaker.value = WaveSpeaker.IDLE
        audioCollectorJob?.cancel()
        audioCollectorJob = null
        audioPlaybackJob?.cancel()
        audioPlaybackJob = null
        aiEnvelopeJob?.cancel()
        aiEnvelopeJob = null
        audioPlaybackQueue.clear()
        preBufferAccumulated = 0
        preBufferReady = false
        synchronized(audioTrackLock) {
            try {
                audioTrack?.stop()
                audioTrack?.release()
            } catch (_: Exception) {}
            audioTrack = null
        }
        android.util.Log.d("VoiceVM", "AudioTrack stopped")
    }

    override fun onCleared() {
        super.onCleared()
        stopMic()
        stopAudioPlayback()
        try { voiceClient?.disconnect() } catch (_: Exception) {}
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
            audioManager.clearCommunicationDevice()
        }
        audioManager.mode = android.media.AudioManager.MODE_NORMAL
    }
}

// Provider-specific voice lists — OFFLINE FALLBACKS (P3.12): the hydrated
// catalog from GET {statusPath} wins when present.
private fun voicesForBackend(backend: VoiceBackend, catalog: VoiceCatalog?): List<String> = when (backend) {
    VoiceBackend.GPT_REALTIME -> catalog.voicesOrFallback(Constants.VOICES_GPT_REALTIME)
    VoiceBackend.GEMINI_LIVE -> catalog.voicesOrFallback(Constants.VOICES_GEMINI_LIVE)
    VoiceBackend.GROK_LIVE -> catalog.voicesOrFallback(Constants.VOICES_GROK_LIVE)
}

/** Format a voice name with character descriptor for Gemini Live. */
private fun voiceLabel(backend: VoiceBackend, voice: String): String = when (backend) {
    VoiceBackend.GEMINI_LIVE ->
        Constants.GEMINI_VOICE_DESCRIPTORS[voice]?.let { "$voice ($it)" } ?: voice
    else -> voice
}

@Composable
fun VoiceScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: VoiceViewModel = viewModel()
) {
    val view = LocalView.current
    val context = LocalContext.current
    val backend by viewModel.backend.collectAsState()
    val catalogs by viewModel.catalogs.collectAsState()
    val voice by viewModel.voice.collectAsState()
    val voiceState by viewModel.voiceState.collectAsState()
    val transcript by viewModel.transcript.collectAsState()
    val provenance by viewModel.provenance.collectAsState()
    val error by viewModel.error.collectAsState()
    val statusText by viewModel.statusText.collectAsState()
    val isMicActive by viewModel.isMicActive.collectAsState()
    val amplitude by viewModel.amplitude.collectAsState()
    val waveSpeaker by viewModel.waveSpeaker.collectAsState()
    val listState = rememberLazyListState()
    val settingsScroll = rememberScrollState()
    var peekSnapId by remember { mutableStateOf<String?>(null) }
    // Request mic permission on first open
    val micPermLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            android.widget.Toast.makeText(context, "Microphone enabled", android.widget.Toast.LENGTH_SHORT).show()
        }
    }
    LaunchedEffect(Unit) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            micPermLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    // Toast feedback on every state change so user always knows what's happening
    LaunchedEffect(voiceState) {
        val msg = when (voiceState) {
            VoiceState.CONNECTING -> "Connecting..."
            VoiceState.RECONNECTING -> "Reconnecting..."
            VoiceState.CONNECTED -> "Connected — listening"
            VoiceState.SPEAKING -> null // Don't toast during speech
            VoiceState.LISTENING -> null
            VoiceState.ERROR -> null // Error shown via error state below
            VoiceState.DISCONNECTED -> if (isMicActive) "Disconnected" else null
        }
        if (msg != null) {
            android.widget.Toast.makeText(context, msg, android.widget.Toast.LENGTH_SHORT).show()
        }
    }
    // Toast on error with haptic
    LaunchedEffect(error) {
        error?.let {
            view.performHapticFeedback(HapticFeedbackConstants.REJECT)
            android.widget.Toast.makeText(context, it, android.widget.Toast.LENGTH_LONG).show()
        }
    }
    LaunchedEffect(transcript.size) {
        if (transcript.isNotEmpty()) listState.animateScrollToItem(transcript.size - 1)
    }
    val isConnected = voiceState != VoiceState.DISCONNECTED && voiceState != VoiceState.ERROR

    // Task 8: collapsible settings pane — auto-collapses once a session connects.
    var settingsExpanded by remember { mutableStateOf(true) }
    LaunchedEffect(isConnected) { if (isConnected) settingsExpanded = false }

    // ── Live-models config — hoisted to the ViewModel, DataStore-persisted (P3.13) ──
    val realtimeModel by viewModel.realtimeModel.collectAsState()
    val realtimeVadType by viewModel.realtimeVadType.collectAsState()
    val realtimeVadEagerness by viewModel.realtimeVadEagerness.collectAsState()
    val realtimeIdleTimeoutText by viewModel.realtimeIdleTimeoutText.collectAsState()
    val geminiModel by viewModel.geminiModel.collectAsState()
    val geminiVadStart by viewModel.geminiVadStart.collectAsState()
    val geminiVadEnd by viewModel.geminiVadEnd.collectAsState()
    val geminiThinkingLevel by viewModel.geminiThinkingLevel.collectAsState()
    val grokModel by viewModel.grokModel.collectAsState()
    val grokReasoningEffort by viewModel.grokReasoningEffort.collectAsState()
    val selectedPresetId by viewModel.selectedPresetId.collectAsState()
    val translateEnabled by viewModel.translateEnabled.collectAsState()
    val translateLang by viewModel.translateLang.collectAsState()
    val translateLangOther by viewModel.translateLangOther.collectAsState()
    val presets by viewModel.presets.collectAsState()

    // Pulse animation for mic recording + AI speaking
    val pulse = rememberInfiniteTransition(label = "pulse")
    val pulseScale by pulse.animateFloat(
        initialValue = 1f, targetValue = 1.12f,
        animationSpec = infiniteRepeatable(tween(800), RepeatMode.Reverse), label = "scale"
    )
    val glowAlpha by pulse.animateFloat(
        initialValue = 0.2f, targetValue = 0.6f,
        animationSpec = infiniteRepeatable(tween(1200), RepeatMode.Reverse), label = "glow"
    )

    Column(
        modifier = modifier
            .fillMaxSize()
            .statusBarsPadding()
            // Extra top padding to clear the floating operator pill (~80dp for pill + spacing)
            .padding(start = 16.dp, end = 16.dp, top = 80.dp, bottom = 16.dp)
    ) {
        // ── Header ──
        Text("\uD83C\uDF99\uFE0F Voice Agent", style = MaterialTheme.typography.headlineMedium, color = BbxWhite)
        Spacer(Modifier.height(12.dp))

        error?.let { err ->
            Text(err, style = MaterialTheme.typography.bodySmall, color = BbxAccent,
                modifier = Modifier.padding(bottom = 8.dp))
        }

        // P3.16: session-health banner — driven by the P3a RECONNECTING state and
        // the terminal disconnected handling (backend no longer fails silently).
        if (voiceState == VoiceState.RECONNECTING) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(Neutral200)
                    .border(1.dp, GlassBorder, RoundedCornerShape(10.dp))
                    .padding(horizontal = 12.dp, vertical = 8.dp)
            ) {
                Text("⟳", color = BbxAccent)
                Spacer(Modifier.width(8.dp))
                Text(
                    "Reconnecting to ${backend.displayName}…",
                    style = MaterialTheme.typography.bodySmall, color = BbxWhite
                )
            }
            Spacer(Modifier.height(8.dp))
        }
        // Terminal server disconnect / reconnect exhaustion land at ERROR (P3a
        // preserves ERROR through the socket teardown); a user hangup lands at
        // DISCONNECTED. Show the session-ended banner for BOTH terminal states,
        // but only when a session actually happened (transcript non-empty).
        if ((voiceState == VoiceState.ERROR || voiceState == VoiceState.DISCONNECTED) &&
            transcript.isNotEmpty()
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(Neutral200)
                    .border(1.dp, GlassBorder, RoundedCornerShape(10.dp))
                    .padding(horizontal = 12.dp, vertical = 8.dp)
            ) {
                Text(
                    "Session ended — tap ▶ to reconnect",
                    style = MaterialTheme.typography.bodySmall, color = BbxRed
                )
            }
            Spacer(Modifier.height(8.dp))
        }

        // ── Collapsible settings pane (auto-collapses on connect) ──
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .glassSurface(shape = RoundedCornerShape(16.dp), bg = Neutral100)
                .padding(12.dp)
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth().clickFeedback {
                    settingsExpanded = !settingsExpanded
                }
            ) {
                Text("⚙️", style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.width(8.dp))
                Text(
                    if (settingsExpanded) "Settings" else "${backend.displayName} · $voice",
                    style = MaterialTheme.typography.titleSmall,
                    color = BbxWhite,
                    modifier = Modifier.weight(1f),
                )
                Text(if (settingsExpanded) "▴" else "▾", color = BbxDim)
            }
            androidx.compose.animation.AnimatedVisibility(visible = settingsExpanded) {
                // Bounded + internally scrollable so an expanded pane (6 Gemini
                // dropdowns) never pushes the pinned mic/waveform off-screen.
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(max = 300.dp)
                        .verticalScroll(settingsScroll)
                        .padding(top = 12.dp)
                ) {
                    // ── Backend selector (disabled while connected) ──
                    LabeledDropdown(
                        label = "Backend",
                        options = VoiceBackend.entries.map { it.id to it.displayName },
                        selectedId = backend.id,
                        enabled = !isConnected,
                        onSelect = { id -> VoiceBackend.entries.firstOrNull { it.id == id }?.let(viewModel::setBackend) },
                    )

                    // ── Voice selector — provider-specific (disabled while connected).
                    // T13 review: VoiceClient has no post-connect session.update outbound path,
                    // so changing voice mid-session only updates local _voice.value — the audible
                    // voice keeps the old setting until next connect(). Gemini Live additionally
                    // doesn't support mid-session voice change (voice is in the setup message).
                    // Treat voice the same as model/vad: bound at connect time, gated while CONNECTED.
                    LabeledDropdown(
                        label = "Voice",
                        options = voicesForBackend(backend, catalogs[backend]).map { it to voiceLabel(backend, it) },
                        selectedId = voice,
                        enabled = !isConnected,
                        onSelect = viewModel::setVoice,
                    )

                    // P3.13: voice-agent preset — hydrated from GET /voice-agents,
                    // filtered to this backend's provider alias; hidden when none
                    // (fresh box / pre-P4 box). Selection rides the agentId connect
                    // param established in P3.12.
                    val presetOpts = presets.filter { it.provider == backend.id }
                    if (presetOpts.isNotEmpty()) {
                        LabeledDropdown(
                            label = "Agent preset",
                            options = listOf("" to "None") + presetOpts.map { it.id to it.name },
                            selectedId = selectedPresetId,
                            enabled = !isConnected,
                            onSelect = viewModel::setPreset,
                        )
                    }

                    // ── Translation mode (P6a) — greyed out for Grok (no translate model) ──
                    val translateSupported = backend != VoiceBackend.GROK_LIVE
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                            if (translateSupported) "Translate mode" else "Translate mode (not supported)",
                            style = MaterialTheme.typography.labelLarge,
                            color = if (translateSupported) BbxDim else Neutral500,
                            modifier = Modifier.weight(1f),
                        )
                        Switch(
                            checked = translateEnabled && translateSupported,
                            onCheckedChange = viewModel::setTranslateEnabled,
                            enabled = translateSupported && !isConnected,
                        )
                    }
                    if (translateEnabled && translateSupported) {
                        val langOpts = Constants.TRANSLATE_LANGUAGES
                            .map { (id, label) -> id to "$label ($id)" } +
                            ("__other__" to "Other (type below)")
                        LabeledDropdown(
                            label = "Target language",
                            options = langOpts,
                            selectedId = translateLang,
                            enabled = !isConnected,
                            onSelect = viewModel::setTranslateLang,
                        )
                        if (translateLang == "__other__") {
                            OutlinedTextField(
                                value = translateLangOther,
                                onValueChange = { new ->
                                    viewModel.setTranslateLangOther(
                                        new.filter { it.isLetterOrDigit() || it == '-' }.take(12))
                                },
                                placeholder = { Text("BCP-47, e.g. sw", color = Neutral500) },
                                singleLine = true,
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedTextColor = BbxWhite,
                                    unfocusedTextColor = BbxWhite,
                                ),
                                modifier = Modifier.fillMaxWidth().widthIn(max = 200.dp),
                            )
                            Spacer(Modifier.height(10.dp))
                        }
                    }

                    // ── Per-provider live-models config (T13 plan 2026-05-19) ──
                    // Model + vad_type dropdowns: disabled while CONNECTED (audit I4 — schema-binding
                    // at upstream WS connect time, switching requires Disconnect → change → Reconnect).
                    // Voice/eagerness/idle_timeout/thinking_level: always enabled (hot-swappable mid-session).
                    when (backend) {
                        VoiceBackend.GPT_REALTIME -> RealtimeConfigBlock(
                            connected = isConnected,
                            modelOptions = catalogs[VoiceBackend.GPT_REALTIME]
                                .modelsOrFallback(Constants.MODEL_CONFIG["realtime"].orEmpty()),
                            model = realtimeModel,
                            onModelChange = viewModel::setRealtimeModel,
                            vadType = realtimeVadType,
                            onVadTypeChange = viewModel::setRealtimeVadType,
                            vadEagerness = realtimeVadEagerness,
                            onVadEagernessChange = viewModel::setRealtimeVadEagerness,
                            idleTimeoutText = realtimeIdleTimeoutText,
                            onIdleTimeoutChange = viewModel::setRealtimeIdleTimeout,
                        )
                        VoiceBackend.GEMINI_LIVE -> GeminiConfigBlock(
                            connected = isConnected,
                            modelOptions = catalogs[VoiceBackend.GEMINI_LIVE]
                                .modelsOrFallback(Constants.MODEL_CONFIG["gemini-live"].orEmpty()),
                            model = geminiModel,
                            onModelChange = viewModel::setGeminiModel,
                            vadStart = geminiVadStart,
                            onVadStartChange = viewModel::setGeminiVadStart,
                            vadEnd = geminiVadEnd,
                            onVadEndChange = viewModel::setGeminiVadEnd,
                            thinkingLevel = geminiThinkingLevel,
                            onThinkingLevelChange = viewModel::setGeminiThinkingLevel,
                        )
                        VoiceBackend.GROK_LIVE -> GrokConfigBlock(
                            connected = isConnected,
                            modelOptions = catalogs[VoiceBackend.GROK_LIVE]
                                .modelsOrFallback(Constants.MODEL_CONFIG["grok-live"].orEmpty()),
                            model = grokModel,
                            onModelChange = viewModel::setGrokModel,
                            reasoningEffort = grokReasoningEffort,
                            onReasoningEffortChange = viewModel::setGrokReasoningEffort,
                        )
                    }
                }
            }
        }
        Spacer(Modifier.height(16.dp))

        // ── Central mic button + status + disconnect ──
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth()
        ) {
            val stateColor by animateColorAsState(
                when {
                    isConnected && isMicActive && voiceState != VoiceState.SPEAKING -> BbxRed
                    voiceState == VoiceState.SPEAKING -> BbxAccent
                    voiceState == VoiceState.CONNECTED || voiceState == VoiceState.LISTENING -> SolidGreen
                    voiceState == VoiceState.CONNECTING -> Neutral500
                    else -> Neutral300
                }, label = "stateColor"
            )

            val shouldPulse = (isMicActive && isConnected) || voiceState == VoiceState.SPEAKING

            // Large mic button (80dp — matches Portal's prominent mic)
            Box(contentAlignment = Alignment.Center) {
                // Glow ring behind button when active
                if (shouldPulse) {
                    Box(
                        modifier = Modifier
                            .size(96.dp)
                            .scale(pulseScale)
                            .clip(CircleShape)
                            .background(stateColor.copy(alpha = glowAlpha))
                    )
                }
                Box(
                    modifier = Modifier
                        .size(80.dp)
                        .scale(if (shouldPulse) pulseScale else 1f)
                        .clip(CircleShape)
                        .background(stateColor)
                        .clickFeedback {
                            if (isConnected) viewModel.toggleMic() else viewModel.connect()
                        },
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        when {
                            !isConnected -> "\u25B6"
                            isMicActive -> "\uD83C\uDFA4"
                            else -> "\uD83D\uDD07"
                        },
                        style = MaterialTheme.typography.headlineMedium,
                        color = BbxWhite
                    )
                }
            }

            Spacer(Modifier.width(16.dp))

            Column(modifier = Modifier.weight(1f)) {
                Text(
                    when {
                        !isConnected -> voiceState.name.replace("_", " ").lowercase()
                            .replaceFirstChar { it.uppercase() }
                        isMicActive && voiceState == VoiceState.SPEAKING -> "AI Speaking"
                        isMicActive -> "Listening..."
                        else -> "Mic Muted"
                    },
                    style = MaterialTheme.typography.titleMedium,
                    color = stateColor
                )
                Text(
                    "${backend.displayName} \u00B7 $voice",
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500
                )
                if (statusText.isNotBlank() &&
                    (voiceState == VoiceState.CONNECTING || voiceState == VoiceState.RECONNECTING)
                ) {
                    Text(statusText, style = MaterialTheme.typography.labelSmall, color = BbxDim)
                }
            }

            // Disconnect button
            if (isConnected) {
                Box(
                    modifier = Modifier
                        .size(44.dp)
                        .clip(CircleShape)
                        .glassSurface(shape = CircleShape, bg = Neutral200)
                        .clickFeedback {
                            viewModel.disconnect()
                        },
                    contentAlignment = Alignment.Center
                ) {
                    Text("\u23F9", style = MaterialTheme.typography.bodyLarge, color = BbxAccent)
                }
            }
        }
        Spacer(Modifier.height(16.dp))

        // ── HD flowing-ribbon waveform — tap to barge-in while the AI speaks (P3.14) ──
        Box(
            modifier = Modifier.fillMaxWidth().clickFeedback {
                if (voiceState == VoiceState.SPEAKING) viewModel.interrupt()
            }
        ) {
            VoiceWaveform(
                amplitude = amplitude,
                speaker = waveSpeaker,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        Spacer(Modifier.height(12.dp))

        // P3.18: typed input to the live agent (VoiceClient.sendText).
        if (isConnected) {
            var typedText by remember { mutableStateOf("") }
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                OutlinedTextField(
                    value = typedText,
                    onValueChange = { typedText = it },
                    placeholder = { Text("Type to the agent…", color = Neutral500) },
                    singleLine = true,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                    ),
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                IconButton(onClick = {
                    val t = typedText.trim()
                    if (t.isNotEmpty()) {
                        viewModel.sendTypedText(t)
                        typedText = ""
                    }
                }) {
                    Text("➤", color = BbxAccent, style = MaterialTheme.typography.titleMedium)
                }
            }
            Spacer(Modifier.height(8.dp))
        }

        // Plan Task 10: retrieval provenance from voice WS dispatcher.
        // Renders above transcript because voice has no per-turn bubble.
        var voiceProvExpanded by remember { mutableStateOf(false) }
        // Voice provenance is session-scoped (emitted once at WS connect + on reconfigure).
        // At session open user_text="" so keyword/semantic come back empty by design — only
        // recent+checkpoint populate. A per-turn refresh on user utterance is a future enhancement.
        provenance?.takeIf { !it.isEmpty() }?.let { prov ->
            ContextProvenance(
                provenance = prov,
                expanded = voiceProvExpanded,
                onToggle = { voiceProvExpanded = !voiceProvExpanded },
                onSnapshotClick = { peekSnapId = it },
            )
            Spacer(Modifier.height(8.dp))
        }

        // ── Transcript — the ONLY scrolling region. The settings/mic/waveform above
        // are pinned (non-weighted children of the fixed outer Column), so the
        // waveform + connect/disconnect controls never scroll away as text builds up.
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxWidth().weight(1f),
            contentPadding = PaddingValues(top = 4.dp, bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            items(transcript) { entry ->
                if (isChipRole(entry.role)) {
                    // P3.17: compact tool-activity chip
                    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                        Box(
                            modifier = Modifier
                                .clip(RoundedCornerShape(50))
                                .background(Neutral200)
                                .border(1.dp, GlassBorder, RoundedCornerShape(50))
                                .padding(horizontal = 12.dp, vertical = 5.dp)
                        ) {
                            Text(entry.text, style = MaterialTheme.typography.labelSmall, color = BbxDim)
                        }
                    }
                } else {
                    val isUser = entry.role == "user"
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start
                    ) {
                        Box(
                            modifier = Modifier
                                .widthIn(max = 280.dp)
                                .clip(RoundedCornerShape(12.dp))
                                .background(if (isUser) Neutral250 else Neutral100)
                                .border(1.dp, GlassBorder, RoundedCornerShape(12.dp))
                                .padding(10.dp)
                        ) {
                            Text(entry.text, style = MaterialTheme.typography.bodyMedium, color = BbxWhite)
                        }
                    }
                }
            }
        }
    }

    peekSnapId?.let { snapId ->
        SnapshotPeekSheet(
            snapId = snapId,
            origin = origin,
            onDismiss = { peekSnapId = null }
        )
    }
}

// ───────────────────────────────────────────────────────────────────────────
// T13 (plan 2026-05-19): per-provider live-models config blocks.
// Pattern matches the existing voice chip-row selector for visual consistency.
// ───────────────────────────────────────────────────────────────────────────

/** OpenAI Realtime config dropdowns: model, vad_type, vad_eagerness OR idle_timeout. */
@Composable
private fun RealtimeConfigBlock(
    connected: Boolean,
    modelOptions: List<Pair<String, String>>,
    model: String,
    onModelChange: (String) -> Unit,
    vadType: String,
    onVadTypeChange: (String) -> Unit,
    vadEagerness: String,
    onVadEagernessChange: (String) -> Unit,
    idleTimeoutText: String,
    onIdleTimeoutChange: (String) -> Unit,
) {
    LabeledDropdown(
        label = "Model",
        options = modelOptions,
        selectedId = model,
        enabled = !connected,  // audit I4: model bound at upstream WS connect time
        onSelect = onModelChange,
    )
    LabeledDropdown(
        label = "Turn detection",
        options = Constants.OPENAI_REALTIME_VAD_TYPES.map { it to it },
        selectedId = vadType,
        enabled = !connected,  // audit I4: vad_type schema differs between server/semantic
        onSelect = onVadTypeChange,
    )
    // Conditional: eagerness only meaningful for semantic_vad
    if (vadType == "semantic_vad") {
        LabeledDropdown(
            label = "Eagerness",
            options = Constants.OPENAI_REALTIME_VAD_EAGERNESS.map { it to it },
            selectedId = vadEagerness,
            enabled = true,  // hot-swappable mid-session
            onSelect = onVadEagernessChange,
        )
    }
    // Conditional: idle_timeout only meaningful for server_vad
    if (vadType == "server_vad") {
        Text("Idle timeout (ms)", style = MaterialTheme.typography.labelLarge, color = BbxDim)
        Spacer(Modifier.height(4.dp))
        OutlinedTextField(
            value = idleTimeoutText,
            onValueChange = { new -> onIdleTimeoutChange(new.filter { it.isDigit() }.take(7)) },
            placeholder = { Text("30000", color = Neutral500) },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            colors = OutlinedTextFieldDefaults.colors(
                focusedTextColor = BbxWhite,
                unfocusedTextColor = BbxWhite,
            ),
            modifier = Modifier
                .fillMaxWidth()
                .widthIn(max = 200.dp),
        )
        Spacer(Modifier.height(10.dp))
    }
}

/** Gemini Live config dropdowns: model, vad_start, vad_end, thinking_level (3.1 only). */
@Composable
private fun GeminiConfigBlock(
    connected: Boolean,
    modelOptions: List<Pair<String, String>>,
    model: String,
    onModelChange: (String) -> Unit,
    vadStart: String?,
    onVadStartChange: (String?) -> Unit,
    vadEnd: String?,
    onVadEndChange: (String?) -> Unit,
    thinkingLevel: String?,
    onThinkingLevelChange: (String?) -> Unit,
) {
    LabeledDropdown(
        label = "Model",
        options = modelOptions,
        selectedId = model,
        enabled = !connected,  // audit I4: model bound at setup time
        onSelect = onModelChange,
    )
    // "auto" entry maps to null (lets backend use its default).
    val sensitivityOpts: List<Pair<String, String>> =
        listOf("__auto__" to "auto") + Constants.GEMINI_LIVE_VAD_SENSITIVITIES.map { it to it }
    val toNullable: (String) -> String? = { if (it == "__auto__") null else it }
    val toSelectedId: (String?) -> String = { it ?: "__auto__" }

    LabeledDropdown(
        label = "VAD start sensitivity",
        options = sensitivityOpts,
        selectedId = toSelectedId(vadStart),
        enabled = !connected,  // audit I4: VAD configured at setup time
        onSelect = { onVadStartChange(toNullable(it)) },
    )
    LabeledDropdown(
        label = "VAD end sensitivity",
        options = sensitivityOpts,
        selectedId = toSelectedId(vadEnd),
        enabled = !connected,
        onSelect = { onVadEndChange(toNullable(it)) },
    )
    // Conditional: thinking_level only for 3.1
    if (model in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS) {
        val thinkingOpts: List<Pair<String, String>> =
            listOf("__auto__" to "auto") + Constants.GEMINI_LIVE_THINKING_LEVELS.map { it to it }
        LabeledDropdown(
            label = "Thinking level",
            options = thinkingOpts,
            selectedId = toSelectedId(thinkingLevel),
            enabled = true,  // hot-swappable mid-session (per plan: voice/eagerness/idle/thinking always enabled)
            onSelect = { onThinkingLevelChange(toNullable(it)) },
        )
    }
}

/** P3.19: Grok Live config — model + reasoning.effort (high|none). */
@Composable
private fun GrokConfigBlock(
    connected: Boolean,
    modelOptions: List<Pair<String, String>>,
    model: String,
    onModelChange: (String) -> Unit,
    reasoningEffort: String?,
    onReasoningEffortChange: (String?) -> Unit,
) {
    LabeledDropdown(
        label = "Model",
        options = modelOptions,
        selectedId = model,
        enabled = !connected,  // bound at upstream WS connect time (?model=)
        onSelect = onModelChange,
    )
    val effortOpts: List<Pair<String, String>> =
        listOf("__auto__" to "auto") + Constants.GROK_LIVE_REASONING_EFFORTS.map { it to it }
    LabeledDropdown(
        label = "Reasoning effort",
        options = effortOpts,
        selectedId = reasoningEffort ?: "__auto__",
        enabled = !connected,
        onSelect = { onReasoningEffortChange(if (it == "__auto__") null else it) },
    )
}
