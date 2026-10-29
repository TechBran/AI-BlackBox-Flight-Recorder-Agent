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
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.lifecycle.compose.LifecycleStartEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
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
import com.aiblackbox.portal.data.store.OperatorPrompt
import com.aiblackbox.portal.data.store.OperatorResolver
import com.aiblackbox.portal.data.voice.MicBuffer
import com.aiblackbox.portal.data.voice.MicSourcePolicy
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
import com.aiblackbox.portal.data.voice.VoiceSampleRates
import com.aiblackbox.portal.data.voice.VoiceSessionConfig
import com.aiblackbox.portal.data.voice.VoiceState
import com.aiblackbox.portal.util.Constants
import com.aiblackbox.portal.ui.components.ContextProvenance
import com.aiblackbox.portal.ui.components.SnapshotPeekSheet
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BINS
import com.aiblackbox.portal.ui.components.aurora.AnalysisFrame
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.PlaybackDelayLine
import com.aiblackbox.portal.ui.components.aurora.auroraBins
import com.aiblackbox.portal.ui.components.aurora.auroraVoices
import com.aiblackbox.portal.ui.components.wave.LocalWaveformStyle
import com.aiblackbox.portal.ui.components.wave.VoiceVisualizer
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
    // True while connect()'s in-call routing (MODE_IN_COMMUNICATION + loudspeaker) is on.
    @Volatile
    private var audioRoutingApplied = false
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
    // ── Gemini affective dialog + proactive audio (P6b) — 2.5 native-audio only.
    // DataStore-persisted like every sibling setting (P3.13 pattern; keys "va_").
    private val _geminiAffective = MutableStateFlow(false)
    val geminiAffective: StateFlow<Boolean> = _geminiAffective.asStateFlow()
    private val _geminiProactive = MutableStateFlow(false)
    val geminiProactive: StateFlow<Boolean> = _geminiProactive.asStateFlow()
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
    //
    // Kept alive alongside the band flows below while the Aurora ribbon is under on-device review
    // (VoiceWaveform, its only consumer, is still on disk for the same reason). It is also the one
    // thing the bands CANNOT express: waveSpeaker is single-valued, and the ribbon's whole point is
    // that the human and the AI can be live at the same instant.
    private val _waveSpeaker = MutableStateFlow(WaveSpeaker.IDLE)
    val waveSpeaker: StateFlow<WaveSpeaker> = _waveSpeaker.asStateFlow()

    // ── Aurora ribbon inputs ────────────────────────────────────────────────────────────────
    // Two INDEPENDENT analysers feed these, because this screen is the case both the analyser and
    // the renderer were built around: the human's mic and the model's playback are audible at the
    // same moment, at DIFFERENT sample rates, and one shared auto-gain reference would let the
    // model's loudness rescale the human's ribbon (and vice versa).

    private val _micBands = MutableStateFlow(AURORA_SILENT_BANDS)
    val micBands: StateFlow<FloatArray> = _micBands.asStateFlow()

    private val _aiBands = MutableStateFlow(AURORA_SILENT_BANDS)
    val aiBands: StateFlow<FloatArray> = _aiBands.asStateFlow()

    // The same instants' log-frequency SPECTRUM — each tap's second channel, raw and uncompressed
    // (see AudioAnalysisTap). Published beside the bands rather than folded into them: they are two
    // readings of one hop, and a renderer that wants both must not have to align two cadences.

    private val _micBins = MutableStateFlow(AURORA_SILENT_BINS)
    val micBins: StateFlow<FloatArray> = _micBins.asStateFlow()

    private val _aiBins = MutableStateFlow(AURORA_SILENT_BINS)
    val aiBins: StateFlow<FloatArray> = _aiBins.asStateFlow()

    /**
     * Whether the model's audio is reaching the speaker right now — the AI ribbon's presence.
     *
     * Separate from [voiceState] == SPEAKING, which flips on the first chunk ARRIVING; this one
     * tracks chunks being COMMITTED to the AudioTrack, so the blue ribbon appears with the sound
     * rather than a pre-buffer ahead of it, and retires when the queue actually runs dry.
     */
    private val _aiAudible = MutableStateFlow(false)
    val aiAudible: StateFlow<Boolean> = _aiAudible.asStateFlow()

    private var currentOperator = ""  // empty-until-store-emits (never hard-code operator; fresh-box rule)

    // Audio I/O
    private var audioRecord: AudioRecord? = null
    private var audioTrack: AudioTrack? = null
    private val audioTrackLock = Object()
    @Volatile private var isRecordingAudio = false
    // The live mic session's OWN stop signal, flipped by stopMic(). Per session and not only the
    // shared flag above: a quick stopMic() + startMic() flips that flag straight back on while the
    // old loop is still inside read(), and the old loop would carry on with the old mic open.
    @Volatile private var micLive: java.util.concurrent.atomic.AtomicBoolean? = null

    /**
     * Guards the blue ribbon's (bands, audible, last-write-at) triple.
     *
     * SEPARATE from [audioTrackLock] deliberately. The drain holds that one across a BLOCKING
     * `AudioTrack.write`, so anything taking it to publish or retire the ribbon would stall for up
     * to a whole track buffer — and the retirement tick runs on the main thread. This lock is only
     * ever held for those three field writes. Lock order is audioTrackLock -> aiVisualLock, never
     * the reverse.
     */
    private val aiVisualLock = Object()

    /** elapsedRealtime of the last blue-ribbon publish. Guarded by [aiVisualLock]. */
    private var lastAiWriteAtMs = 0L

    /**
     * The AI playback stream's analysis, and the delay line that decides WHEN each frame is true.
     *
     * Both are created with the AudioTrack in [initAudioPlayback] and only ever touched under
     * [audioTrackLock] — the drain thread writes and pumps them, [interrupt] rewinds them on a
     * barge-in, [stopAudioPlayback] drops them, and that lock is what serialises the three. One tap
     * per stream (D1): the mic's is a separate instance built at a different rate.
     */
    private var aiTap: AudioAnalysisTap? = null
    private var aiDelayLine: PlaybackDelayLine? = null

    /** Reused AudioTimestamp scratch. Only ever filled under [audioTrackLock], like the two above. */
    private val aiTimestamp = android.media.AudioTimestamp()

    /**
     * True from the moment the drain takes a chunk off the queue until its write returns.
     *
     * Across that span the queue is empty and no write has completed — exactly the shape the
     * retirement check would otherwise read as "nothing is playing", while a chunk is at that
     * instant being handed to the device. A write blocks roughly one chunk's duration, which for a
     * long chunk can outlast [aiTrackPlayoutMs].
     */
    @Volatile private var aiWriteInFlight = false

    /**
     * How long the AudioTrack's own buffer takes to play out, in ms — set in [initAudioPlayback]
     * from the buffer that track was actually built with, so a device whose minimum buffer pushes
     * past the 16 KB floor holds the ribbon proportionally longer. Zero until a track exists.
     */
    @Volatile private var aiTrackPlayoutMs = 0L

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
            _geminiAffective.value = store.getString("va_gem_affective").first() == "true"
            _geminiProactive.value = store.getString("va_gem_proactive").first() == "true"
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
                    // Clean up audio on disconnect — including a server-side end or
                    // exhausted reconnects, after which the Disconnect button is gone:
                    // put the phone back on its normal audio path (A12-voice-cron-02).
                    if (state == VoiceState.DISCONNECTED || state == VoiceState.ERROR) {
                        stopMic()
                        stopAudioPlayback()
                        restoreAudioRouting()
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
    fun setGeminiAffective(v: Boolean) { _geminiAffective.value = v; persist("va_gem_affective", if (v) "true" else "false") }
    fun setGeminiProactive(v: Boolean) { _geminiProactive.value = v; persist("va_gem_proactive", if (v) "true" else "false") }

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
                val affectiveAllowed = _geminiModel.value in Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
                VoiceSessionConfig(
                    model = _geminiModel.value.takeIf { it.isNotBlank() },
                    vadStart = _geminiVadStart.value,
                    vadEnd = _geminiVadEnd.value,
                    thinkingLevel = if (thinkingAllowed) _geminiThinkingLevel.value else null,
                    affective = if (affectiveAllowed && _geminiAffective.value) true else null,
                    proactive = if (affectiveAllowed && _geminiProactive.value) true else null,
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
        // A voice session runs as the resolved operator (persona, memory, ledger); the
        // store's operator is "" until the box resolves one, and the server cannot build
        // context for "" — so say what to do instead of failing generically.
        OperatorResolver.sendBlockedReason(currentOperator, OperatorPrompt.CHOOSE)?.let {
            _error.value = it
            return
        }
        val sessionConfig = buildSessionConfig()
        _error.value = null
        stopMic()
        stopAudioPlayback()
        _transcript.value = emptyList()
        _serverTranscript.value = emptyList()
        _localEntries.value = emptyList()
        _provenance.value = null
        // Enable communication mode for strong system-level AEC
        audioRoutingApplied = true
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
        restoreAudioRouting()
        _voiceState.value = VoiceState.DISCONNECTED
    }

    /**
     * Undo connect()'s routing: clear the loudspeaker communication device (or the
     * speakerphone flag below API 31) and leave MODE_IN_COMMUNICATION. Only when connect()
     * applied it, so stopping a screen that never connected leaves the app's audio alone.
     * Called on every way a session ends (A12-voice-cron-02).
     */
    private fun restoreAudioRouting() {
        if (!audioRoutingApplied) return
        audioRoutingApplied = false
        try {
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
                audioManager.clearCommunicationDevice()
            } else {
                @Suppress("DEPRECATION")
                audioManager.isSpeakerphoneOn = false
            }
            audioManager.mode = android.media.AudioManager.MODE_NORMAL
        } catch (e: Exception) {
            android.util.Log.w("VoiceVM", "restore audio routing: ${e.javaClass.simpleName}")
        }
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
            // flush() discards what the track was holding AND zeroes its playback position, so
            // every pending frame describes audio nobody will hear and every offset still in
            // flight is measured against a counter that no longer exists. Both sides of the
            // presentation clock rewind here, together, or the next utterance publishes early.
            aiTap?.reset()
            aiDelayLine?.reset()
        }
        _amplitude.value = 0f
        // Barge-in flushed the queue, so the blue ribbon has to go with it — the point of a
        // barge-in is that the AI's turn is OVER, and a ribbon still riding the flushed chunk's
        // spectrum reads as the model talking over the interruption. This is the one retirement
        // that does NOT wait for the track to play out: `flush()` above just discarded everything
        // the track was holding, so there is nothing left to hear.
        clearAiAudible()
        _waveSpeaker.value = WaveSpeaker.IDLE
    }

    /**
     * Publish the blue ribbon for a frame whose audio is AUDIBLE NOW, and stamp when that was.
     *
     * `AudioTrack.write` on a MODE_STREAM track blocks only until the bytes have been COPIED into
     * the track's own buffer; it says nothing about them having been played. That buffer holds
     * [aiTrackPlayoutMs] of audio, so the queue runs dry that long before the last sound leaves the
     * speaker — which is why the retirement tick cannot key off an empty queue alone, and why this
     * is no longer called from the write. [PlaybackDelayLine] holds each frame until the track's own
     * position reaches it; what arrives here is the sound in the room.
     */
    private fun markAiAudible(frame: AnalysisFrame) {
        synchronized(aiVisualLock) {
            lastAiWriteAtMs = android.os.SystemClock.elapsedRealtime()
            _aiBands.value = frame.bands
            _aiBins.value = frame.bins
            _aiAudible.value = true
        }
    }

    /** Retire the blue ribbon now. Under [aiVisualLock] so it cannot cross a chunk being published. */
    private fun clearAiAudible() {
        synchronized(aiVisualLock) {
            lastAiWriteAtMs = 0L
            _aiBands.value = AURORA_SILENT_BANDS
            _aiBins.value = AURORA_SILENT_BINS
            _aiAudible.value = false
        }
    }

    /**
     * Publish whichever analysed frame the speaker has reached, if any. Drain thread only.
     *
     * Called every drain iteration rather than only after a write, because the TAIL of an utterance
     * is released long after the last write returned — the track is still playing a bufferful while
     * the drain idles on an empty queue.
     *
     * Takes [audioTrackLock] to read the track's position, then [aiVisualLock] to publish: the
     * documented order, and the same one the write path uses.
     */
    private fun publishAudibleAiFrame() {
        val frame = synchronized(audioTrackLock) {
            val line = aiDelayLine ?: return
            val track = audioTrack ?: return
            // Same guard the write path uses: asking an uninitialised track for its position is
            // what throws, and nothing has been written to one anyway.
            if (track.state != AudioTrack.STATE_INITIALIZED) return
            line.release(renderedSamples(track))
        } ?: return
        markAiAudible(frame)
    }

    /**
     * Frames the track has actually rendered, best source first, or null when it will not say —
     * see [PlaybackDelayLine] for why null is a supported answer rather than a failure. Caller
     * holds [audioTrackLock] (this fills the shared [aiTimestamp]).
     *
     * `getTimestamp` is preferred because it is corrected for the device's own output latency, so
     * it answers "what the ear is hearing" rather than "what the mixer has consumed" — but it is
     * documented best-effort and refuses right after `play()`. `getPlaybackHeadPosition` is the
     * coarser rung that is always there. Both count from the same zero the delay line does: a
     * `flush()` or `stop()` resets them, which is why [interrupt] rewinds the line with the track.
     */
    private fun renderedSamples(track: AudioTrack): Long? = try {
        if (track.getTimestamp(aiTimestamp)) {
            PlaybackDelayLine.renderedAt(
                framePosition = aiTimestamp.framePosition,
                timestampNs = aiTimestamp.nanoTime,
                nowNs = System.nanoTime(),
                sampleRate = VoiceSampleRates.AI_OUTPUT,
            )
        } else {
            PlaybackDelayLine.headPositionFrames(track.playbackHeadPosition)
        }
    } catch (_: Exception) {
        null
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
    @OptIn(kotlinx.coroutines.DelicateCoroutinesApi::class)
    fun startMic() {
        val app = getApplication<Application>()
        if (ContextCompat.checkSelfPermission(app, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            _error.value = "Microphone permission required"
            return
        }

        val sampleRate = VoiceSampleRates.mic(_backend.value)
        // DUPLEX capture, and the reason this path does NOT follow the composer's simplex source
        // chain: this mic is open while the model is speaking out of the same device's loudspeaker.
        // VOICE_COMMUNICATION (and the AEC below) is what keeps the model from transcribing its own
        // voice when the user barges in — dropping either is a functional regression, not a
        // cleaner signal. See [MicSourcePolicy].
        val micSource = MicSourcePolicy.DUPLEX_SOURCE
        val micEffects = MicSourcePolicy.effectsFor(micSource)
        // Built HERE, from the rate the AudioRecord below is about to open at, and captured by this
        // session's capture loop. A field would have to be rebuilt whenever the backend changed
        // (16 kHz Gemini/Grok vs 24 kHz realtime); a per-session local cannot drift from its own
        // stream by construction, and each new session gets a fresh warm-up and gain seed for free.
        // ONE per stream (D1) — the AI playback tap runs concurrently and must not be this one.
        val micTap = AudioAnalysisTap(sampleRate, calibration = micSource.calibration, label = "voice-mic/$micSource")

        val bufferSize = MicBuffer.recordBufferBytes(
            AudioRecord.getMinBufferSize(
                sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
        )

        try {
            val record = AudioRecord(
                micSource.androidSource,
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

            // Attach the explicit effects this path asks for, for devices where the platform does
            // not enable them automatically. They are RETAINED and released with the record below:
            // as unowned locals they outlived every reference to them, so the platform kept the
            // effect attached to a dead session and the NEXT mic session inherited whatever state
            // the garbage collector had left behind.
            var aec: android.media.audiofx.AcousticEchoCanceler? = null
            var ns: android.media.audiofx.NoiseSuppressor? = null
            try {
                if (micEffects.echoCanceler && android.media.audiofx.AcousticEchoCanceler.isAvailable()) {
                    aec = android.media.audiofx.AcousticEchoCanceler.create(record.audioSessionId)
                    aec?.enabled = true
                    android.util.Log.d("VoiceVM", "AcousticEchoCanceler enabled")
                }
                if (micEffects.noiseSuppressor && android.media.audiofx.NoiseSuppressor.isAvailable()) {
                    ns = android.media.audiofx.NoiseSuppressor.create(record.audioSessionId)
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

            // A startMic() over a live session ends the old loop rather than orphaning its signal
            // where no stopMic() could ever reach it.
            micLive?.set(false)
            val live = java.util.concurrent.atomic.AtomicBoolean(true)
            micLive = live
            // ATOMIC: a launch cancelled before it is dispatched (onCleared) never runs its body,
            // and the body's finally is the only place this record is released.
            viewModelScope.launch(Dispatchers.IO, start = kotlinx.coroutines.CoroutineStart.ATOMIC) {
                var wasSendingAudio = false
                // Hoisted out of the read loop on purpose: the tap calls this synchronously, on
                // this thread, once per completed hop — allocating a lambda per read would put a
                // capture in the audio path for nothing. The frame's arrays are freshly built and
                // belong to us, so they go straight into the flows with no copy.
                val publishMic: (AnalysisFrame) -> Unit = { frame ->
                    _micBands.value = frame.bands
                    _micBins.value = frame.bins
                }
                try {
                    val buffer = ShortArray(MicBuffer.readLengthSamples(bufferSize))
                    while (live.get()) {
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
                                    // Grok's held mic stops feeding the tap, so without this the
                                    // red ribbon would freeze at the last syllable before the hold
                                    // and sit there through the model's whole answer. The tap is
                                    // deliberately NOT reset here: a hold is the same speaker
                                    // pausing, and its own stall detection already re-anchors (and
                                    // so starts a new analyser session) if the hold outlasts
                                    // AuroraAnalyser.SESSION_GAP_NS — which is exactly what the
                                    // analyser did on this path before.
                                    _micBands.value = AURORA_SILENT_BANDS
                                    _micBins.value = AURORA_SILENT_BINS
                                    continue
                                }
                            }

                            // Convert shorts to little-endian bytes
                            val bytes = ByteArray(readCount * 2)
                            for (i in 0 until readCount) {
                                bytes[i * 2] = (buffer[i].toInt() and 0xFF).toByte()
                                bytes[i * 2 + 1] = (buffer[i].toInt() shr 8 and 0xFF).toByte()
                            }
                            // The mic's OWN tap, at the mic's OWN rate — the AI's runs concurrently
                            // at 24 kHz on the playback side. The tap decides the cadence now: a
                            // read shorter than a hop publishes nothing and carries its audio
                            // forward, a longer one publishes every hop it completes.
                            micTap.offer(bytes, bytes.size, publishMic)
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
                    // Effects first: they are attached to this record's audio session, so they
                    // have to go before the session they are bound to does.
                    try { aec?.release() } catch (_: Exception) {}
                    try { ns?.release() } catch (_: Exception) {}
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
        micLive?.set(false)
        micLive = null
        _isMicActive.value = false
        _amplitude.value = 0f
        _micBands.value = AURORA_SILENT_BANDS
        _micBins.value = AURORA_SILENT_BINS
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

        val outputSampleRate = VoiceSampleRates.AI_OUTPUT
        val channelConfig = AudioFormat.CHANNEL_OUT_MONO
        val audioFormat = AudioFormat.ENCODING_PCM_16BIT

        val minBufferSize = AudioTrack.getMinBufferSize(outputSampleRate, channelConfig, audioFormat)

        if (minBufferSize == AudioTrack.ERROR || minBufferSize == AudioTrack.ERROR_BAD_VALUE) {
            android.util.Log.e("VoiceVM", "Invalid AudioTrack buffer size: $minBufferSize")
            _error.value = "Audio output not available"
            return
        }

        val bufferSize = maxOf(minBufferSize * 4, 16384)
        // Mono PCM16 is 2 bytes a frame, so this is how much audio the track can be holding when a
        // write returns — and therefore how long after the LAST write the speaker is still busy.
        // The blue ribbon is held that long past an empty queue; see [markAiAudible].
        aiTrackPlayoutMs = bufferSize.toLong() * 1000L / (2L * outputSampleRate)

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
                // Built here, from the rate and the buffer THIS track was just opened with, and
                // replaced wholesale with it: a stream's analysis, its presentation clock and the
                // device it is measuring all begin and end together. A SEPARATE tap from the mic's
                // — see the band flows' note.
                aiTap = AudioAnalysisTap(outputSampleRate, label = "voice-ai")
                aiDelayLine = PlaybackDelayLine(bufferSize / 2)  // bytes -> frames: mono PCM16
                track.play()
            }
            android.util.Log.d("VoiceVM", "AudioTrack initialized: ${outputSampleRate}Hz, buffer=$bufferSize")

            // Start playback drain immediately — it gates on preBufferReady internally.
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
            // Hoisted out of the write path: the tap calls this synchronously, once per completed
            // hop, while this thread holds audioTrackLock. Queueing is all it may do — publishing
            // here would put the ribbon back ahead of the sound, which is the bug.
            val queueFrame: (AnalysisFrame) -> Unit = { frame -> aiDelayLine?.offer(frame) }
            try {
                while (isActive) {
                    // Every iteration, not only after a write: the last bufferful of an utterance
                    // becomes audible while this loop is idling on an empty queue, and those frames
                    // are the model's closing word.
                    publishAudibleAiFrame()

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
                        // Raised BEFORE the (blocking) write and dropped after it — the queue is
                        // empty for that whole span, and the retirement tick must not read that as
                        // silence. See the field.
                        aiWriteInFlight = true
                        try {
                            synchronized(audioTrackLock) {
                                val track = audioTrack ?: return@synchronized
                                if (track.state == AudioTrack.STATE_INITIALIZED) {
                                    val wrote = track.write(chunk, 0, chunk.size)
                                    // Sample loudness at the moment the chunk is committed to the
                                    // audio device, so the waveform stays in sync with what's heard
                                    // (the ~12KB pre-buffer + drain otherwise lag enqueue by ~256ms).
                                    // Attack: jump UP to this chunk's loudness; the envelope job
                                    // releases between chunks so quiet syllables + pauses show.
                                    _waveSpeaker.value = WaveSpeaker.AI
                                    _amplitude.value = maxOf(_amplitude.value, rmsAmplitudeFromBytes(chunk))
                                    // The bands are analysed here and published LATER, when the
                                    // speaker reaches them — the two calls take the byte count the
                                    // track ACCEPTED (never chunk.size) so the tap's offsets and
                                    // the delay line's position stay one number. A negative return
                                    // is an error code, not a length.
                                    if (wrote > 0) {
                                        aiDelayLine?.accepted(wrote)
                                        aiTap?.offer(chunk, wrote, queueFrame)
                                    }
                                }
                            }
                        } finally {
                            aiWriteInFlight = false
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
                // The bands do not decay here (the ribbon has its own per-band release) but they DO
                // have to stop: the last chunk's spectrum would otherwise hold the blue ribbon up
                // for as long as the screen stayed open.
                //
                // An empty queue is NOT proof playback is over. `track.write` blocks only until the
                // bytes are copied into the AudioTrack's OWN buffer, not until they are rendered,
                // so the queue drains a whole buffer (aiTrackPlayoutMs — ~341ms at the 16KB floor,
                // 24kHz mono PCM16) before the model's last word is heard. Retiring on the queue
                // alone dropped the blue ribbon a third of a second into the AI still talking.
                // Three guards, all load bearing: the model has stopped generating, the queue is
                // empty, and no chunk is mid-write; then the playout wait finishes the job.
                //
                // That wait is the buffer's FULL playout, i.e. an UPPER bound, and it is now
                // measured from the last AUDIBLE frame rather than the last write — the delay line
                // moved the stamp, and the ribbon consequently lingers by up to one more buffer
                // than it used to. Kept that way on purpose: the wait must never expire BEFORE the
                // delay line's tail is published, because a retire followed by a straggling frame
                // is a present->absent->present flip that costs a full retire/return of the ribbon,
                // while lingering is invisible (the sheets are already resting on the renderer's
                // own per-band decay). The exact answer is the track's position against frames
                // written — which the drain now has, but reading it needs audioTrackLock, held
                // across the blocking write, and this tick is on the main thread.
                //
                // Publishing the SHARED silent arrays is what stops this 30 Hz tick from
                // recomposing the ribbon while nothing is happening.
                if (voiceClient?.isAISpeaking?.value != true &&
                    audioPlaybackQueue.isEmpty() &&
                    !aiWriteInFlight
                ) {
                    // Re-checked under the same lock the drain publishes on, so a chunk landing on
                    // a tick boundary cannot be blanked by a decision taken just before it arrived.
                    synchronized(aiVisualLock) {
                        if (_aiAudible.value &&
                            android.os.SystemClock.elapsedRealtime() - lastAiWriteAtMs >= aiTrackPlayoutMs
                        ) {
                            _aiBands.value = AURORA_SILENT_BANDS
                            _aiBins.value = AURORA_SILENT_BINS
                            _aiAudible.value = false
                        }
                    }
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
            // Dropped WITH the track they measure: a frame still held here describes audio on a
            // device that no longer exists, and the next session builds its own pair.
            aiTap = null
            aiDelayLine = null
        }
        // Retired LAST, and the ORDER is the whole point: the drain can publish ONE MORE frame
        // while its cancel() is pending. Both of its publish paths — the write, and
        // publishAudibleAiFrame() at the top of the loop — are plain blocking code inside
        // synchronized(audioTrackLock) with no suspension point, so `audioPlaybackJob?.cancel()`
        // above cannot interrupt a drain already sitting in `track.write`; that write finishes and
        // the next iteration publishes. Clearing FIRST therefore left _aiAudible=true with
        // aiEnvelopeJob already cancelled: nothing alive to retire it, so hanging up mid-utterance
        // (the common case) left the blue ribbon animating on the dead session's last spectrum.
        // Once the block above has nulled audioTrack, both paths bail on their
        // `audioTrack ?: return` guard and this clear is the last word. (No playout wait either —
        // the track is stopped and released, so what it still held will never be heard.)
        clearAiAudible()
        android.util.Log.d("VoiceVM", "AudioTrack stopped")
    }

    override fun onCleared() {
        super.onCleared()
        stopMic()
        stopAudioPlayback()
        try { voiceClient?.disconnect() } catch (_: Exception) {}
        // Also resets the speakerphone flag below API 31, which this used to skip.
        restoreAudioRouting()
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

/**
 * BOTH speakers, in ONE container, at the same time: red is the operator's mic, blue is the model.
 * This is the screen the two-voice renderer exists for — during a barge-in they are genuinely both
 * live, and the phase weave is what keeps them apart.
 *
 * A null side is ABSENT, not silent: a muted mic shows no red ribbon at all rather than a resting
 * one implying it is listening.
 *
 * It is a composable of its OWN, taking flows rather than collected values, purely so the audio
 * feed's ~30-50 Hz of band updates invalidate THIS scope and nothing else. Collected at the top of
 * [VoiceScreen] they would recompose the transcript list, the settings panel, the controls and the
 * typed-input field on every single chunk. The composer's ribbon dodges the same trap by deferring
 * its read behind a `() -> FloatArray` lambda (Composer.kt's `recordingBands`); this screen has
 * five flows to read rather than one, so it takes the flows and collects them here.
 *
 * [isMicActive] is a plain value on purpose- the status line and the mic button above already read
 * it, and it changes at human speed, so routing it through this scope would buy nothing.
 */
@Composable
private fun VoiceRibbon(
    micBands: StateFlow<FloatArray>,
    aiBands: StateFlow<FloatArray>,
    micBins: StateFlow<FloatArray>,
    aiBins: StateFlow<FloatArray>,
    aiAudible: StateFlow<Boolean>,
    isMicActive: Boolean,
    modifier: Modifier = Modifier,
) {
    val mic by micBands.collectAsStateWithLifecycle()
    val ai by aiBands.collectAsStateWithLifecycle()
    val micSpectrum by micBins.collectAsStateWithLifecycle()
    val aiSpectrum by aiBins.collectAsStateWithLifecycle()
    val aiIsAudible by aiAudible.collectAsStateWithLifecycle()
    VoiceVisualizer(
        // The persisted global style (aurora by default); resolved + dispatched by
        // VoiceVisualizer, unknown/absent → aurora, so this screen is unchanged until picked.
        styleId = LocalWaveformStyle.current,
        voices = auroraVoices(
            human = mic.takeIf { isMicActive },
            ai = ai.takeIf { aiIsAudible },
        ),
        // The SAME tap's spectrum, gated by the SAME two conditions as the bands above and in the
        // same order. Crossing them — a spectrum for a speaker whose voice is absent, or the other
        // way round — would put one speaker's two channels out of step, and a style reads both.
        spectra = auroraBins(
            human = micSpectrum.takeIf { isMicActive },
            ai = aiSpectrum.takeIf { aiIsAudible },
        ),
        // Never park while a voice is PRESENT here: this is a dedicated foreground surface whose
        // ribbon is the only sign the session is alive, and a frozen wave reads as a hung app. With
        // no voice at all the renderer parks regardless of this flag (it draws nothing in that
        // state), and its lifecycle gate still stops it when the screen is not visible.
        pauseWhenIdle = false,
        modifier = modifier,
    )
}

/**
 * Whether stopping the Voice screen (navigating away, or the app going to the background)
 * ends the session: yes for any live or failed session, except across a configuration
 * change. Pure for unit tests (A12-voice-cron-08).
 */
internal fun endVoiceSessionOnStop(changingConfigurations: Boolean, state: VoiceState): Boolean =
    !changingConfigurations && state != VoiceState.DISCONNECTED

/**
 * Whether the Voice screen holds the display on: while a session is connecting or live.
 * Without it the screen timeout would end a hands-free conversation after ~30 s
 * ([endVoiceSessionOnStop] ends the session when the display turns off). Pure for tests.
 */
internal fun voiceKeepsScreenOn(state: VoiceState): Boolean =
    state != VoiceState.DISCONNECTED && state != VoiceState.ERROR

private tailrec fun android.content.Context.findActivity(): android.app.Activity? = when (this) {
    is android.app.Activity -> this
    is android.content.ContextWrapper -> baseContext.findActivity()
    else -> null
}

@Composable
fun VoiceScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: VoiceViewModel = viewModel()
) {
    val view = LocalView.current
    val context = LocalContext.current
    val backend by viewModel.backend.collectAsStateWithLifecycle()
    val catalogs by viewModel.catalogs.collectAsStateWithLifecycle()
    val voice by viewModel.voice.collectAsStateWithLifecycle()
    val voiceState by viewModel.voiceState.collectAsStateWithLifecycle()
    val transcript by viewModel.transcript.collectAsStateWithLifecycle()
    val provenance by viewModel.provenance.collectAsStateWithLifecycle()
    val error by viewModel.error.collectAsStateWithLifecycle()
    val statusText by viewModel.statusText.collectAsStateWithLifecycle()
    val isMicActive by viewModel.isMicActive.collectAsStateWithLifecycle()
    // micBands/aiBands/aiAudible are deliberately NOT collected here — see [VoiceRibbon].
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

    // Leaving the screen or backgrounding the app ends the live session
    // (A12-voice-cron-08): without a microphone foreground service Android silences the
    // mic in the background, while the provider socket kept billing and the in-call
    // audio mode stayed applied to the rest of the app. disconnect() only signals the
    // capture loop (it releases its own recorder). A configuration change (a Fold
    // unfold, a rotation) is not a stop: the view model and the session survive it.
    LifecycleStartEffect(viewModel) {
        onStopOrDispose {
            val changing = context.findActivity()?.isChangingConfigurations == true
            if (endVoiceSessionOnStop(changing, viewModel.voiceState.value)) viewModel.disconnect()
        }
    }

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

    // A live session keeps the display on, as a call screen does: the screen timing out
    // stops the activity, which ends the session (see the stop handler above), and a
    // hands-free conversation never touches the screen to hold it on.
    val keepOn = voiceKeepsScreenOn(voiceState)
    DisposableEffect(view, keepOn) {
        view.keepScreenOn = keepOn
        onDispose { view.keepScreenOn = false }
    }

    // Task 8: collapsible settings pane — auto-collapses once a session connects.
    var settingsExpanded by remember { mutableStateOf(true) }
    LaunchedEffect(isConnected) { if (isConnected) settingsExpanded = false }

    // ── Live-models config — hoisted to the ViewModel, DataStore-persisted (P3.13) ──
    val realtimeModel by viewModel.realtimeModel.collectAsStateWithLifecycle()
    val realtimeVadType by viewModel.realtimeVadType.collectAsStateWithLifecycle()
    val realtimeVadEagerness by viewModel.realtimeVadEagerness.collectAsStateWithLifecycle()
    val realtimeIdleTimeoutText by viewModel.realtimeIdleTimeoutText.collectAsStateWithLifecycle()
    val geminiModel by viewModel.geminiModel.collectAsStateWithLifecycle()
    val geminiVadStart by viewModel.geminiVadStart.collectAsStateWithLifecycle()
    val geminiVadEnd by viewModel.geminiVadEnd.collectAsStateWithLifecycle()
    val geminiThinkingLevel by viewModel.geminiThinkingLevel.collectAsStateWithLifecycle()
    val geminiAffective by viewModel.geminiAffective.collectAsStateWithLifecycle()
    val geminiProactive by viewModel.geminiProactive.collectAsStateWithLifecycle()
    val grokModel by viewModel.grokModel.collectAsStateWithLifecycle()
    val grokReasoningEffort by viewModel.grokReasoningEffort.collectAsStateWithLifecycle()
    val selectedPresetId by viewModel.selectedPresetId.collectAsStateWithLifecycle()
    val translateEnabled by viewModel.translateEnabled.collectAsStateWithLifecycle()
    val translateLang by viewModel.translateLang.collectAsStateWithLifecycle()
    val translateLangOther by viewModel.translateLangOther.collectAsStateWithLifecycle()
    val presets by viewModel.presets.collectAsStateWithLifecycle()

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
                            affective = geminiAffective,
                            onAffectiveChange = viewModel::setGeminiAffective,
                            proactive = geminiProactive,
                            onProactiveChange = viewModel::setGeminiProactive,
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
            VoiceRibbon(
                micBands = viewModel.micBands,
                aiBands = viewModel.aiBands,
                micBins = viewModel.micBins,
                aiBins = viewModel.aiBins,
                aiAudible = viewModel.aiAudible,
                isMicActive = isMicActive,
                modifier = Modifier.fillMaxWidth().height(140.dp),
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
    affective: Boolean,
    onAffectiveChange: (Boolean) -> Unit,
    proactive: Boolean,
    onProactiveChange: (Boolean) -> Unit,
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
    // Affective dialog + proactive audio — 2.5 native-audio family only (v1alpha,
    // deprecated line). Setup+URL-version binding: locked while connected (audit I4).
    val affectiveCapable = model in Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text(
            "Affective dialog — 2.5 only (deprecated line)",
            style = MaterialTheme.typography.labelLarge,
            color = BbxDim,
            modifier = Modifier.weight(1f),
        )
        Switch(
            checked = affective && affectiveCapable,
            onCheckedChange = onAffectiveChange,
            enabled = !connected && affectiveCapable,
        )
    }
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text(
            "Proactive audio — 2.5 only (deprecated line)",
            style = MaterialTheme.typography.labelLarge,
            color = BbxDim,
            modifier = Modifier.weight(1f),
        )
        Switch(
            checked = proactive && affectiveCapable,
            onCheckedChange = onProactiveChange,
            enabled = !connected && affectiveCapable,
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
