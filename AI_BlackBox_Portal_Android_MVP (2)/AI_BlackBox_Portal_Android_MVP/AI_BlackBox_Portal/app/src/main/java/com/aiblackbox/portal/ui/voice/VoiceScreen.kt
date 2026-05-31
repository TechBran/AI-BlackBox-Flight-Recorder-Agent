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
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
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
import com.aiblackbox.portal.data.voice.VoiceBackend
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
import kotlinx.coroutines.Job
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

enum class WaveSpeaker { USER, AI, IDLE }

class VoiceViewModel(application: Application) : AndroidViewModel(application) {
    private val store = BlackBoxStore(application)
    private val audioManager = application.getSystemService(android.content.Context.AUDIO_SERVICE) as android.media.AudioManager
    private var voiceClient: VoiceClient? = null

    private val _backend = MutableStateFlow(VoiceBackend.GEMINI_LIVE)
    val backend: StateFlow<VoiceBackend> = _backend.asStateFlow()

    private val _voice = MutableStateFlow(Constants.DEFAULT_GEMINI_LIVE_VOICE)
    val voice: StateFlow<String> = _voice.asStateFlow()

    private val _voiceState = MutableStateFlow(VoiceState.DISCONNECTED)
    val voiceState: StateFlow<VoiceState> = _voiceState.asStateFlow()

    private val _transcript = MutableStateFlow<List<TranscriptEntry>>(emptyList())
    val transcript: StateFlow<List<TranscriptEntry>> = _transcript.asStateFlow()

    // Plan Task 10: typed retrieval provenance from the voice WS dispatcher.
    private val _provenance = MutableStateFlow<Provenance?>(null)
    val provenance: StateFlow<Provenance?> = _provenance.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    // Mic state
    private val _isMicActive = MutableStateFlow(false)
    val isMicActive: StateFlow<Boolean> = _isMicActive.asStateFlow()

    // Live waveform inputs — real RMS amplitude (0f..1f) + who is speaking.
    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    private val _waveSpeaker = MutableStateFlow(WaveSpeaker.IDLE)
    val waveSpeaker: StateFlow<WaveSpeaker> = _waveSpeaker.asStateFlow()

    private var currentOperator = "Brandon"

    // Audio I/O
    private var audioRecord: AudioRecord? = null
    private var audioTrack: AudioTrack? = null
    private val audioTrackLock = Object()
    @Volatile private var isRecordingAudio = false

    // Decoupled audio playback queue (matching OverlayService pattern)
    private val audioPlaybackQueue = java.util.concurrent.ConcurrentLinkedQueue<ByteArray>()
    private var audioPlaybackJob: Job? = null
    private var audioCollectorJob: Job? = null  // Must be cancelled to prevent duplicate collectors

    // Pre-buffering state
    @Volatile private var preBufferAccumulated = 0
    @Volatile private var preBufferReady = false

    companion object {
        const val PRE_BUFFER_THRESHOLD_BYTES = 12_000  // ~250ms at 24kHz mono PCM16
    }

    init {
        viewModelScope.launch { store.operator.collect { currentOperator = it } }
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || voiceClient != null) return
        try {
            val wsUrl = origin.replace("https://", "wss://").replace("http://", "ws://")
            voiceClient = VoiceClient(BlackBoxApi(origin).getClient(), wsUrl)

            // Collect state changes
            viewModelScope.launch {
                voiceClient?.state?.collect { state ->
                    _voiceState.value = state
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
            // Collect transcripts
            viewModelScope.launch {
                voiceClient?.transcript?.collect { _transcript.value = it }
            }
            // Plan Task 10: collect retrieval provenance pushed by the WS dispatcher
            viewModelScope.launch {
                voiceClient?.provenance?.collect { _provenance.value = it }
            }
            // Collect error events
            viewModelScope.launch {
                voiceClient?.events?.collect { event ->
                    if (event is VoiceEvent.Error) {
                        _error.value = event.message
                    }
                }
            }
            android.util.Log.d("VoiceVM", "Initialized: wsUrl=$wsUrl")
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "Initialize failed: ${e.message}", e)
            _error.value = "Init failed: ${e.message}"
        }
    }

    fun setBackend(backend: VoiceBackend) { _backend.value = backend }
    fun setVoice(voice: String) { _voice.value = voice }

    fun connect(sessionConfig: VoiceSessionConfig? = null) {
        _error.value = null
        stopMic()
        stopAudioPlayback()
        _transcript.value = emptyList()
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
                        val readCount = audioRecord?.read(buffer, 0, buffer.size) ?: 0
                        if (readCount > 0) {
                            val amp = rmsAmplitude(buffer, readCount)
                            // Auto-mute while AI is speaking WITH post-speech delay
                            val client = voiceClient
                            if (client != null) {
                                val isSpeaking = client.isAISpeaking.value
                                val timeSinceStop = System.currentTimeMillis() - client.aiStoppedSpeakingAt
                                val inPostSpeechDelay = !isSpeaking && timeSinceStop < VoiceClient.POST_SPEECH_DELAY_MS

                                if (isSpeaking || inPostSpeechDelay) {
                                    // Just skip sending — do NOT send audio_commit here.
                                    // GPT/Grok have server-side VAD that auto-detects turns.
                                    // Sending audio_commit mid-stream triggers duplicate responses.
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
                                    _waveSpeaker.value = WaveSpeaker.USER
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
                }
            }
        } catch (e: Exception) {
            android.util.Log.e("VoiceVM", "Mic start failed: ${e.message}", e)
            _error.value = "Mic start failed: ${e.message}"
        }
    }

    fun stopMic() {
        isRecordingAudio = false  // Signals mic loop to exit, which sends audio_commit
        _isMicActive.value = false
        _amplitude.value = 0f
        if (_waveSpeaker.value == WaveSpeaker.USER) _waveSpeaker.value = WaveSpeaker.IDLE
        try {
            audioRecord?.stop()
            audioRecord?.release()
        } catch (_: Exception) {}
        audioRecord = null
        android.util.Log.d("VoiceVM", "Mic stopped")
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
                        _waveSpeaker.value = WaveSpeaker.AI
                        _amplitude.value = rmsAmplitudeFromBytes(pcmBytes)
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
    }

    private fun stopAudioPlayback() {
        _amplitude.value = 0f
        _waveSpeaker.value = WaveSpeaker.IDLE
        audioCollectorJob?.cancel()
        audioCollectorJob = null
        audioPlaybackJob?.cancel()
        audioPlaybackJob = null
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

// Provider-specific voice lists.
// T13 (plan 2026-05-19): VOICES_GPT + VOICES_GEMINI now sourced from Constants.kt
// (single source of truth). VOICES_GROK unchanged — Grok Live out of scope.
private val VOICES_GPT = Constants.VOICES_GPT_REALTIME
private val VOICES_GEMINI = Constants.VOICES_GEMINI_LIVE
private val VOICES_GROK = listOf("Ara", "Rex", "Sal", "Eve", "Leo")

private fun voicesForBackend(backend: VoiceBackend) = when (backend) {
    VoiceBackend.GPT_REALTIME -> VOICES_GPT
    VoiceBackend.GEMINI_LIVE -> VOICES_GEMINI
    VoiceBackend.GROK_LIVE -> VOICES_GROK
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
    val voice by viewModel.voice.collectAsState()
    val voiceState by viewModel.voiceState.collectAsState()
    val transcript by viewModel.transcript.collectAsState()
    val provenance by viewModel.provenance.collectAsState()
    val error by viewModel.error.collectAsState()
    val isMicActive by viewModel.isMicActive.collectAsState()
    val listState = rememberLazyListState()
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
    // Reset voice when backend changes — pick the per-backend canonical default
    // (Constants.DEFAULT_*_VOICE), falling back to first in the list.
    LaunchedEffect(backend) {
        val default = when (backend) {
            VoiceBackend.GPT_REALTIME -> Constants.DEFAULT_GPT_REALTIME_VOICE
            VoiceBackend.GEMINI_LIVE -> Constants.DEFAULT_GEMINI_LIVE_VOICE
            VoiceBackend.GROK_LIVE -> voicesForBackend(backend).first()
        }
        viewModel.setVoice(default)
    }

    val isConnected = voiceState != VoiceState.DISCONNECTED && voiceState != VoiceState.ERROR

    // ── Live-models config state (T13 plan 2026-05-19) ──
    // OpenAI Realtime config
    var realtimeModel by remember {
        mutableStateOf(Constants.LIVE_MODEL_DEFAULTS["realtime"] ?: "")
    }
    var realtimeVadType by remember { mutableStateOf("server_vad") }
    var realtimeVadEagerness by remember { mutableStateOf("medium") }
    // Idle timeout as a text field so the user can clear it (null = backend default).
    var realtimeIdleTimeoutText by remember { mutableStateOf("") }

    // Gemini Live config — null entries map to backend default ("auto").
    var geminiModel by remember {
        mutableStateOf(Constants.LIVE_MODEL_DEFAULTS["gemini-live"] ?: "")
    }
    var geminiVadStart by remember { mutableStateOf<String?>(null) }
    var geminiVadEnd by remember { mutableStateOf<String?>(null) }
    var geminiThinkingLevel by remember { mutableStateOf<String?>(null) }

    // Builder: assemble the optional config to pass to viewModel.connect().
    fun buildSessionConfig(): VoiceSessionConfig? = when (backend) {
        VoiceBackend.GPT_REALTIME -> VoiceSessionConfig(
            model = realtimeModel.takeIf { it.isNotBlank() },
            vadType = realtimeVadType.takeIf { it.isNotBlank() },
            vadEagerness = if (realtimeVadType == "semantic_vad") realtimeVadEagerness else null,
            idleTimeoutMs = if (realtimeVadType == "server_vad")
                realtimeIdleTimeoutText.trim().toIntOrNull() else null,
        )
        VoiceBackend.GEMINI_LIVE -> {
            val thinkingAllowed = geminiModel in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS
            VoiceSessionConfig(
                model = geminiModel.takeIf { it.isNotBlank() },
                vadStart = geminiVadStart,
                vadEnd = geminiVadEnd,
                thinkingLevel = if (thinkingAllowed) geminiThinkingLevel else null,
            )
        }
        VoiceBackend.GROK_LIVE -> null
    }

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

        // ── Backend selector (disabled while connected) ──
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            VoiceBackend.entries.forEach { b ->
                val isSelected = b == backend
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(16.dp))
                        .background(if (isSelected) BbxAccent.copy(alpha = 0.2f) else Neutral200)
                        .then(if (isSelected) Modifier.border(1.dp, BbxAccent.copy(alpha = 0.4f), RoundedCornerShape(16.dp)) else Modifier)
                        .clickable(enabled = !isConnected) {
                            view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                            viewModel.setBackend(b)
                        }
                        .padding(horizontal = 12.dp, vertical = 6.dp)
                ) {
                    Text(b.displayName, style = MaterialTheme.typography.labelSmall,
                        color = if (isSelected) BbxAccent else Neutral500)
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        // ── Voice selector — provider-specific (disabled while connected).
        // T13 review: VoiceClient has no post-connect session.update outbound path,
        // so changing voice mid-session only updates local _voice.value — the audible
        // voice keeps the old setting until next connect(). Gemini Live additionally
        // doesn't support mid-session voice change (voice is in the setup message).
        // Treat voice the same as model/vad: bound at connect time, gated while CONNECTED.
        Text("Voice", style = MaterialTheme.typography.labelLarge, color = BbxDim)
        Spacer(Modifier.height(4.dp))
        Row(
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState())
        ) {
            voicesForBackend(backend).forEach { v ->
                val isSelected = v == voice
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(12.dp))
                        .background(if (isSelected) SolidGreen.copy(alpha = 0.15f) else Neutral200)
                        .then(if (isSelected) Modifier.border(1.dp, SolidGreen.copy(alpha = 0.4f), RoundedCornerShape(12.dp)) else Modifier)
                        .clickable(enabled = !isConnected) {
                            view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                            viewModel.setVoice(v)
                        }
                        .padding(horizontal = 10.dp, vertical = 4.dp)
                ) {
                    Text(voiceLabel(backend, v), style = MaterialTheme.typography.labelSmall,
                        color = if (isSelected) SolidGreen else Neutral500)
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        // ── Per-provider live-models config (T13 plan 2026-05-19) ──
        // Model + vad_type dropdowns: disabled while CONNECTED (audit I4 — schema-binding
        // at upstream WS connect time, switching requires Disconnect → change → Reconnect).
        // Voice/eagerness/idle_timeout/thinking_level: always enabled (hot-swappable mid-session).
        when (backend) {
            VoiceBackend.GPT_REALTIME -> RealtimeConfigBlock(
                connected = isConnected,
                model = realtimeModel,
                onModelChange = { realtimeModel = it },
                vadType = realtimeVadType,
                onVadTypeChange = { realtimeVadType = it },
                vadEagerness = realtimeVadEagerness,
                onVadEagernessChange = { realtimeVadEagerness = it },
                idleTimeoutText = realtimeIdleTimeoutText,
                onIdleTimeoutChange = { realtimeIdleTimeoutText = it },
            )
            VoiceBackend.GEMINI_LIVE -> GeminiConfigBlock(
                connected = isConnected,
                model = geminiModel,
                onModelChange = { geminiModel = it },
                vadStart = geminiVadStart,
                onVadStartChange = { geminiVadStart = it },
                vadEnd = geminiVadEnd,
                onVadEndChange = { geminiVadEnd = it },
                thinkingLevel = geminiThinkingLevel,
                onThinkingLevelChange = { geminiThinkingLevel = it },
            )
            VoiceBackend.GROK_LIVE -> Unit // out of scope
        }

        Spacer(Modifier.height(20.dp))

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
                        .clickable {
                            view.performHapticFeedback(
                                if (isConnected) HapticFeedbackConstants.CONTEXT_CLICK
                                else HapticFeedbackConstants.CONFIRM
                            )
                            if (isConnected) viewModel.toggleMic() else viewModel.connect(buildSessionConfig())
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
            }

            // Disconnect button
            if (isConnected) {
                Box(
                    modifier = Modifier
                        .size(44.dp)
                        .clip(CircleShape)
                        .glassSurface(shape = CircleShape, bg = Neutral200)
                        .clickable {
                            view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                            viewModel.disconnect()
                        },
                    contentAlignment = Alignment.Center
                ) {
                    Text("\u23F9", style = MaterialTheme.typography.bodyLarge, color = BbxAccent)
                }
            }
        }
        Spacer(Modifier.height(16.dp))

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

        // ── Transcript ──
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxWidth().weight(1f),
            contentPadding = PaddingValues(top = 4.dp, bottom = 240.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            items(transcript) { entry ->
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

/**
 * Generic chip-row picker. Renders a label + horizontally scrollable row of
 * selectable chips. Selection is highlighted with the accent color.
 *
 * @param enabled if false, chips are visually dimmed and clicks are blocked
 *   (used for model/vad chips while CONNECTED — audit I4).
 */
@Composable
private fun ChipRowPicker(
    label: String,
    options: List<Pair<String, String>>,  // (id, displayName)
    selectedId: String?,
    enabled: Boolean = true,
    onSelect: (String) -> Unit,
) {
    val view = LocalView.current
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(label, style = MaterialTheme.typography.labelLarge, color = BbxDim)
        Spacer(Modifier.height(4.dp))
        Row(
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState())
        ) {
            options.forEach { (id, name) ->
                val isSelected = id == selectedId
                val baseColor = if (enabled) BbxAccent else Neutral500
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(12.dp))
                        .background(
                            if (isSelected) baseColor.copy(alpha = if (enabled) 0.18f else 0.08f)
                            else Neutral200
                        )
                        .then(
                            if (isSelected) Modifier.border(
                                1.dp, baseColor.copy(alpha = if (enabled) 0.4f else 0.2f),
                                RoundedCornerShape(12.dp)
                            ) else Modifier
                        )
                        .clickable(enabled = enabled) {
                            view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                            onSelect(id)
                        }
                        .padding(horizontal = 10.dp, vertical = 4.dp)
                ) {
                    Text(
                        name,
                        style = MaterialTheme.typography.labelSmall,
                        color = when {
                            isSelected && enabled -> baseColor
                            isSelected -> Neutral500
                            !enabled -> Neutral300
                            else -> Neutral500
                        }
                    )
                }
            }
        }
        Spacer(Modifier.height(10.dp))
    }
}

/** OpenAI Realtime config dropdowns: model, vad_type, vad_eagerness OR idle_timeout. */
@Composable
private fun RealtimeConfigBlock(
    connected: Boolean,
    model: String,
    onModelChange: (String) -> Unit,
    vadType: String,
    onVadTypeChange: (String) -> Unit,
    vadEagerness: String,
    onVadEagernessChange: (String) -> Unit,
    idleTimeoutText: String,
    onIdleTimeoutChange: (String) -> Unit,
) {
    val modelOpts = Constants.MODEL_CONFIG["realtime"].orEmpty()
    ChipRowPicker(
        label = "Model",
        options = modelOpts,
        selectedId = model,
        enabled = !connected,  // audit I4: model bound at upstream WS connect time
        onSelect = onModelChange,
    )
    ChipRowPicker(
        label = "Turn detection",
        options = Constants.OPENAI_REALTIME_VAD_TYPES.map { it to it },
        selectedId = vadType,
        enabled = !connected,  // audit I4: vad_type schema differs between server/semantic
        onSelect = onVadTypeChange,
    )
    // Conditional: eagerness only meaningful for semantic_vad
    if (vadType == "semantic_vad") {
        ChipRowPicker(
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
    model: String,
    onModelChange: (String) -> Unit,
    vadStart: String?,
    onVadStartChange: (String?) -> Unit,
    vadEnd: String?,
    onVadEndChange: (String?) -> Unit,
    thinkingLevel: String?,
    onThinkingLevelChange: (String?) -> Unit,
) {
    val modelOpts = Constants.MODEL_CONFIG["gemini-live"].orEmpty()
    ChipRowPicker(
        label = "Model",
        options = modelOpts,
        selectedId = model,
        enabled = !connected,  // audit I4: model bound at setup time
        onSelect = onModelChange,
    )
    // "auto" entry maps to null (lets backend use its default).
    val sensitivityOpts: List<Pair<String, String>> =
        listOf("__auto__" to "auto") + Constants.GEMINI_LIVE_VAD_SENSITIVITIES.map { it to it }
    val toNullable: (String) -> String? = { if (it == "__auto__") null else it }
    val toSelectedId: (String?) -> String = { it ?: "__auto__" }

    ChipRowPicker(
        label = "VAD start sensitivity",
        options = sensitivityOpts,
        selectedId = toSelectedId(vadStart),
        enabled = !connected,  // audit I4: VAD configured at setup time
        onSelect = { onVadStartChange(toNullable(it)) },
    )
    ChipRowPicker(
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
        ChipRowPicker(
            label = "Thinking level",
            options = thinkingOpts,
            selectedId = toSelectedId(thinkingLevel),
            enabled = true,  // hot-swappable mid-session (per plan: voice/eagerness/idle/thinking always enabled)
            onSelect = { onThinkingLevelChange(toNullable(it)) },
        )
    }
}
