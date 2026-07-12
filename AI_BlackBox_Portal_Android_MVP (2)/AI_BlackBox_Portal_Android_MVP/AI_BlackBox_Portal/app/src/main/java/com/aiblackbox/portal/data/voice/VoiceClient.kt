package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.ui.chat.ChatViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import java.util.UUID

enum class VoiceBackend(val id: String, val displayName: String, val wsPath: String, val statusPath: String) {
    GPT_REALTIME("realtime", "GPT Realtime", "/ws/realtime", "/realtime/status"),
    GEMINI_LIVE("gemini-live", "Gemini Live", "/ws/gemini-live", "/gemini-live/status"),
    GROK_LIVE("grok-live", "Grok Live", "/ws/grok-live", "/grok-live/status")
}

enum class VoiceState { DISCONNECTED, CONNECTING, CONNECTED, SPEAKING, LISTENING, ERROR }

sealed class VoiceEvent {
    data class Transcript(val text: String, val isFinal: Boolean = false, val role: String = "assistant") : VoiceEvent()
    data class StatusChange(val state: VoiceState) : VoiceEvent()
    data class Error(val message: String) : VoiceEvent()
    data object Connected : VoiceEvent()
    data object Disconnected : VoiceEvent()
}

class VoiceClient(
    private val client: OkHttpClient,
    private val baseWsUrl: String,
    // Testability seam (voice upgrade pass P3.1): production uses the real
    // WebSocketClient; unit tests inject FakeWebSocketClient. The reconnect
    // loop (P3.8) also uses this to open a fresh socket per leg.
    private val wsFactory: (OkHttpClient) -> WebSocketClient = { WebSocketClient(it) },
) {
    private var wsClient = wsFactory(client)
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _state = MutableStateFlow(VoiceState.DISCONNECTED)
    val state: StateFlow<VoiceState> = _state.asStateFlow()

    private val _events = MutableSharedFlow<VoiceEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<VoiceEvent> = _events.asSharedFlow()

    private val _transcript = MutableStateFlow<List<TranscriptEntry>>(emptyList())
    val transcript: StateFlow<List<TranscriptEntry>> = _transcript.asStateFlow()

    // Plan Task 10: typed retrieval provenance pushed by the backend at session start
    // and on every reconfigure. Separate flow because voice has no per-turn bubble.
    private val _provenance = MutableStateFlow<Provenance?>(null)
    val provenance: StateFlow<Provenance?> = _provenance.asStateFlow()

    // AI speaking state — used by mic to auto-mute during AI output
    private val _isAISpeaking = MutableStateFlow(false)
    val isAISpeaking: StateFlow<Boolean> = _isAISpeaking.asStateFlow()

    // Track when AI stopped speaking for post-speech delay
    @Volatile
    var aiStoppedSpeakingAt: Long = 0L
        private set

    // Audio output flow — base64-encoded PCM16 chunks from the server
    private val _audioOutput = MutableSharedFlow<String>(extraBufferCapacity = 512)
    val audioOutput: SharedFlow<String> = _audioOutput.asSharedFlow()

    // Accumulator for streaming AI transcript deltas
    private var _currentAiText = MutableStateFlow("")

    private var connectionJob: Job? = null
    private var keepaliveJob: Job? = null
    private var currentOperator = ""
    private var currentVoice = ""
    private var scope: CoroutineScope? = null

    // Pong tracking for connection health monitoring
    @Volatile
    private var lastPongTime: Long = 0L

    companion object {
        const val POST_SPEECH_DELAY_MS = 1200L  // 1.2s — generous for speaker echo to die down
        const val KEEPALIVE_INTERVAL_MS = 15_000L
        const val PONG_TIMEOUT_MS = 30_000L
    }

    fun connect(
        backend: VoiceBackend,
        operator: String,
        voice: String,
        scope: CoroutineScope,
        sessionConfig: VoiceSessionConfig? = null,
    ) {
        this.scope = scope
        currentOperator = operator
        currentVoice = voice
        _state.value = VoiceState.CONNECTING
        connectionJob?.cancel()
        keepaliveJob?.cancel()

        connectionJob = scope.launch {
            val sessionId = UUID.randomUUID().toString()
            // T12 (plan 2026-05-19): extend URL query with optional model/vad fields.
            // Long-form Gemini naming (vad_sensitivity_start/_end) per Phase A backend.
            val url = buildString {
                append(baseWsUrl)
                append(backend.wsPath)
                append('/')
                append(sessionId)
                append("?operator=").append(operator)
                append("&voice=").append(voice)
                sessionConfig?.let { cfg ->
                    cfg.model?.let { append("&model=").append(it) }
                    cfg.vadType?.let { append("&vad_type=").append(it) }
                    cfg.vadEagerness?.let { append("&vad_eagerness=").append(it) }
                    cfg.idleTimeoutMs?.let { append("&idle_timeout_ms=").append(it) }
                    cfg.vadStart?.let { append("&vad_sensitivity_start=").append(it) }
                    cfg.vadEnd?.let { append("&vad_sensitivity_end=").append(it) }
                    cfg.thinkingLevel?.let { append("&thinking_level=").append(it) }
                }
            }
            android.util.Log.d("VoiceClient", "Connecting to: $url")
            wsClient.connect(url).collect { msg ->
                when (msg) {
                    is WsMessage.Connected -> {
                        // Stay at CONNECTING — don't set CONNECTED until server confirms
                        // backend (OpenAI/Gemini/Grok) is actually ready.
                        // This prevents mic from streaming before the backend accepts audio.
                        lastPongTime = System.currentTimeMillis()
                        // Send connect message — server will establish backend and reply "connected"
                        val connectMsg = buildJsonObject {
                            put("type", "connect")
                            put("operator", currentOperator)
                            put("voice", currentVoice)
                        }
                        wsClient.send(connectMsg.toString())
                        android.util.Log.d("VoiceClient", "WebSocket open, sent connect message, waiting for backend ready...")

                        // Start keepalive loop (server handles ping/pong even during setup)
                        startKeepalive()
                    }
                    is WsMessage.Text -> parseMessage(msg.text)
                    is WsMessage.Closing -> {
                        android.util.Log.w("VoiceClient", "Server closing: ${msg.code} ${msg.reason}")
                        _events.emit(VoiceEvent.Error("Session closed: ${msg.reason}"))
                        _state.value = VoiceState.ERROR
                    }
                    is WsMessage.Error -> {
                        _state.value = VoiceState.ERROR
                        _events.emit(VoiceEvent.Error(msg.error.message ?: "Connection error"))
                    }
                    is WsMessage.Disconnected -> {
                        _state.value = VoiceState.DISCONNECTED
                        _isAISpeaking.value = false
                        _currentAiText.value = ""
                        _events.emit(VoiceEvent.Disconnected)
                    }
                }
            }
        }
    }

    fun disconnect() {
        keepaliveJob?.cancel()
        keepaliveJob = null
        connectionJob?.cancel()
        wsClient.close()
        _state.value = VoiceState.DISCONNECTED
        _isAISpeaking.value = false
        _currentAiText.value = ""
        _provenance.value = null
    }

    /** Send a base64-encoded PCM16 audio chunk to the server (mic input). */
    fun sendAudioChunk(base64Audio: String) {
        val msg = buildJsonObject {
            put("type", "audio_input")
            put("data", base64Audio)
        }
        wsClient.send(msg.toString())
    }

    /** Signal end of user speech turn — server triggers AI response. */
    fun sendAudioCommit() {
        val msg = buildJsonObject { put("type", "audio_commit") }
        wsClient.send(msg.toString())
        android.util.Log.d("VoiceClient", "Sent audio_commit")
    }

    fun sendText(text: String) {
        val msg = buildJsonObject { put("type", "text"); put("text", text) }
        wsClient.send(msg.toString())
    }

    // Application-level keepalive matching Portal pattern
    private fun startKeepalive() {
        keepaliveJob?.cancel()
        keepaliveJob = scope?.launch {
            while (isActive) {
                delay(KEEPALIVE_INTERVAL_MS)
                if (_state.value == VoiceState.DISCONNECTED || _state.value == VoiceState.ERROR) break

                // Check for pong timeout
                val timeSincePong = System.currentTimeMillis() - lastPongTime
                if (timeSincePong > PONG_TIMEOUT_MS) {
                    android.util.Log.w("VoiceClient", "No pong in ${timeSincePong}ms — closing for reconnect")
                    _state.value = VoiceState.ERROR
                    _events.emit(VoiceEvent.Error("Connection timed out"))
                    wsClient.close()
                    break
                }

                // Send application-level ping
                val ping = buildJsonObject { put("type", "ping") }
                if (!wsClient.send(ping.toString())) {
                    android.util.Log.w("VoiceClient", "Ping send failed — connection dead")
                    _state.value = VoiceState.ERROR
                    _events.emit(VoiceEvent.Error("Connection lost"))
                    break
                }
            }
        }
    }

    private suspend fun parseMessage(raw: String) {
        try {
            val obj = json.parseToJsonElement(raw).jsonObject
            val type = obj["type"]?.jsonPrimitive?.content ?: return
            // data can be a string primitive or a JSON object — extract safely
            val data = try { obj["data"]?.jsonPrimitive?.content ?: "" } catch (_: Exception) { "" }

            when (type) {
                "connected", "setup_complete" -> {
                    _state.value = VoiceState.CONNECTED
                    android.util.Log.d("VoiceClient", "Server message: $type")
                }

                "audio_delta" -> {
                    // AI is producing audio — mark as speaking and emit chunk
                    if (!_isAISpeaking.value) {
                        _isAISpeaking.value = true
                        _state.value = VoiceState.SPEAKING
                    }
                    if (data.isNotEmpty()) {
                        _audioOutput.emit(data) // base64 PCM16 chunk
                    }
                }

                "transcript_delta" -> {
                    _currentAiText.value += data
                    val updatedList = _transcript.value.toMutableList()
                    if (updatedList.isNotEmpty() && updatedList.last().role == "assistant") {
                        updatedList[updatedList.lastIndex] =
                            updatedList.last().copy(text = _currentAiText.value)
                    } else {
                        updatedList.add(TranscriptEntry(role = "assistant", text = _currentAiText.value))
                    }
                    _transcript.value = updatedList
                }

                "user_transcript" -> {
                    // Suppress echo transcriptions — if AI is speaking or just stopped,
                    // this transcript is likely the AI's own words picked up by the mic
                    val timeSinceAiStopped = System.currentTimeMillis() - aiStoppedSpeakingAt
                    val isEchoWindow = _isAISpeaking.value || timeSinceAiStopped < POST_SPEECH_DELAY_MS
                    if (data.isNotBlank() && !isEchoWindow) {
                        _transcript.value = _transcript.value + TranscriptEntry(role = "user", text = data)
                    } else if (isEchoWindow) {
                        android.util.Log.d("VoiceClient", "Suppressed echo transcript: ${data.take(50)}")
                    }
                }

                "response_complete" -> {
                    // Track when AI stopped for post-speech delay
                    _isAISpeaking.value = false
                    aiStoppedSpeakingAt = System.currentTimeMillis()
                    _currentAiText.value = ""
                    _state.value = VoiceState.CONNECTED
                    android.util.Log.d("VoiceClient", "Response complete")
                }

                "transcript", "response" -> {
                    val text = obj["text"]?.jsonPrimitive?.content ?: data
                    val role = obj["role"]?.jsonPrimitive?.content ?: "assistant"
                    val isFinal = obj["final"]?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: true
                    if (text.isNotBlank()) {
                        _transcript.value = _transcript.value + TranscriptEntry(role = role, text = text)
                        _events.emit(VoiceEvent.Transcript(text, isFinal, role))
                    }
                }

                "speaking" -> {
                    _isAISpeaking.value = true
                    _state.value = VoiceState.SPEAKING
                }

                "listening" -> {
                    // Track when AI stopped for post-speech delay
                    _isAISpeaking.value = false
                    aiStoppedSpeakingAt = System.currentTimeMillis()
                    _state.value = VoiceState.LISTENING
                }

                // Handle pong for keepalive health monitoring
                "pong" -> {
                    lastPongTime = System.currentTimeMillis()
                }

                // Provenance — Plan Task 10: backend emits {"type":"provenance","data":{recent,keyword,semantic,checkpoint}}
                // Re-stringify the inner object and reuse ChatViewModel.parseProvenance.
                "provenance" -> {
                    val raw = try {
                        obj["data"]?.jsonObject?.toString()
                    } catch (_: Exception) { null }
                    if (raw != null) {
                        val parsed = ChatViewModel.parseProvenance(raw)
                        if (parsed != null) {
                            _provenance.value = parsed
                            android.util.Log.d(
                                "VoiceClient",
                                "provenance: recent=${parsed.recent.size} keyword=${parsed.keyword.size} " +
                                    "semantic=${parsed.semantic.size} checkpoint=${parsed.checkpoint.size}"
                            )
                        } else {
                            android.util.Log.w("VoiceClient", "provenance unparseable: ${raw.take(200)}")
                        }
                    }
                }

                "error" -> {
                    val msg = obj["message"]?.jsonPrimitive?.content ?: data
                    _events.emit(VoiceEvent.Error(msg))
                    _state.value = VoiceState.ERROR
                    android.util.Log.e("VoiceClient", "Server error: $msg")
                }
            }
        } catch (e: Exception) {
            android.util.Log.e("VoiceClient", "Parse error: ${e.message}")
        }
    }
}

data class TranscriptEntry(
    val role: String,
    val text: String,
    val timestamp: Long = System.currentTimeMillis()
)
