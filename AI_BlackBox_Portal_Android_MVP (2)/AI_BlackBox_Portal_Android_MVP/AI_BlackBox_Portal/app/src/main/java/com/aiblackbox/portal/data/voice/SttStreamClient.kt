package com.aiblackbox.portal.data.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import com.aiblackbox.portal.ui.voice.rmsAmplitude
import com.aiblackbox.portal.util.Constants
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
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

/**
 * Live multi-provider transcription client for the backend's `/ws/stt` WebSocket.
 *
 * Mirrors [VoiceClient]'s structure: an internal [WebSocketClient], a private
 * [CoroutineScope] for the connection + capture loops, and StateFlow/SharedFlow
 * exposure. Captures mic PCM16 at a FIXED 24 kHz (required — OpenAI realtime
 * transcription rejects < 24000; Google + the web client all use 24000), sends
 * it base64-encoded to the server, and emits transcript events.
 *
 * Wire protocol (provider-agnostic — backend resolves the wizard-selected
 * provider when `provider` is ""):
 *   - Client → server: `{"type":"stt_start","target":...,"provider":...,"lang":...,"sample_rate":24000}`
 *     then repeated `{"type":"stt_audio","pcm":"<base64 PCM16>"}` then `{"type":"stt_stop"}`.
 *   - Server → client: `{"type":"stt_delta","text":"<CUMULATIVE interim>","target":...}`,
 *     `{"type":"stt_final","text":"<full final>","target":...}`,
 *     `{"type":"stt_error","message":...}`.
 *
 * The `stt_delta.text` is CUMULATIVE (full interim so far); this client emits it
 * verbatim — the applier that replaces the interim region lives downstream.
 *
 * Requires RECORD_AUDIO permission; the CALLER is expected to gate that (existing
 * mic handlers use withMicPermission). This client does not request permission.
 */
class SttStreamClient(private val client: OkHttpClient, private val baseWsUrl: String) {

    // Per-session socket client: each start() gets its OWN instance so an old
    // session's socket lifecycle can never touch a newer session's (closes I2).
    private var wsClient: WebSocketClient = WebSocketClient(client)
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _events = MutableSharedFlow<SttEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<SttEvent> = _events.asSharedFlow()

    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    private val _isStreaming = MutableStateFlow(false)
    val isStreaming: StateFlow<Boolean> = _isStreaming.asStateFlow()

    private var scope: CoroutineScope? = null
    private var connectionJob: Job? = null
    private var captureJob: Job? = null

    // Monotonic id per start(); used to drop late events and skip teardown from a
    // previous session once a new one has begun (prevents cross-press bleed).
    @Volatile private var sessionEpoch = 0
    @Volatile private var graceJob: Job? = null

    @Volatile
    private var audioRecord: AudioRecord? = null

    companion object {
        // FIXED — do NOT branch by backend. OpenAI realtime transcription rejects
        // anything < 24000; Google accepts 24000; the web client uses 24000.
        private const val SAMPLE_RATE = 24000
        // Grace period after stt_stop so a trailing stt_final still arrives before close.
        private const val STOP_GRACE_MS = 1200L
        private const val TAG = "SttStreamClient"
    }

    /**
     * Open the WS, send stt_start once connected, and begin streaming mic audio.
     * No-op if already streaming.
     */
    fun start(provider: String = "", lang: String = "en", target: String = "prompt") {
        // Preempt any pending stop()-grace and fully tear down a prior session so its
        // socket/scope/late-events can't bleed into this one.
        graceJob?.cancel()
        graceJob = null
        if (_isStreaming.value || connectionJob != null) {
            shutdown()
        }
        val epoch = ++sessionEpoch
        _isStreaming.value = true
        _amplitude.value = 0f

        val newScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = newScope
        val sessionWs = WebSocketClient(client)
        wsClient = sessionWs

        val url = baseWsUrl + Constants.WS_STT
        android.util.Log.d(TAG, "Connecting to: $url")

        connectionJob = newScope.launch {
            try {
                sessionWs.connect(url).collect { msg ->
                    when (msg) {
                        is WsMessage.Connected -> {
                            val startMsg = buildJsonObject {
                                put("type", "stt_start")
                                put("target", target)
                                put("provider", provider)
                                put("lang", lang)
                                put("sample_rate", SAMPLE_RATE)
                            }
                            sessionWs.send(startMsg.toString())
                            android.util.Log.d(TAG, "Sent stt_start (provider='$provider', lang='$lang', target='$target')")
                            startCapture(newScope, sessionWs)
                        }
                        is WsMessage.Text -> parseMessage(msg.text, epoch)
                        is WsMessage.Closing -> {
                            android.util.Log.w(TAG, "Server closing: ${msg.code} ${msg.reason}")
                            cleanup()
                        }
                        is WsMessage.Error -> {
                            android.util.Log.e(TAG, "WS error: ${msg.error.message}")
                            if (epoch == sessionEpoch) _events.emit(SttEvent.Error(msg.error.message ?: "Connection error"))
                            cleanup()
                        }
                        is WsMessage.Disconnected -> {
                            android.util.Log.d(TAG, "WS disconnected")
                            cleanup()
                        }
                    }
                }
            } catch (e: Exception) {
                android.util.Log.e(TAG, "Connection loop error: ${e.message}", e)
                if (epoch == sessionEpoch) _events.emit(SttEvent.Error(e.message ?: "Connection error"))
                cleanup()
            }
        }
    }

    /**
     * Stop capture and end the session: send stt_stop, release the mic, then
     * close the WS after a short grace so a trailing stt_final can arrive.
     * Idempotent and leak-safe.
     */
    fun stop() {
        if (!_isStreaming.value && audioRecord == null && connectionJob == null) return
        _isStreaming.value = false

        // Stop + release the mic immediately (signals capture loop to exit too).
        releaseAudioRecord()
        _amplitude.value = 0f

        val s = scope
        val sWs = wsClient
        val sConn = connectionJob
        val sCap = captureJob
        if (s != null) {
            val epoch = sessionEpoch
            graceJob = s.launch {
                try {
                    sWs.send(buildJsonObject { put("type", "stt_stop") }.toString())
                    android.util.Log.d(TAG, "Sent stt_stop")
                } catch (_: Exception) {}
                delay(STOP_GRACE_MS)
                teardownSession(epoch, s, sConn, sCap, sWs)
            }
        } else {
            shutdown()
        }
    }

    // -------------------------------------------------------------------------
    // Mic capture — AudioRecord @24kHz -> base64 PCM16 -> WebSocket
    // -------------------------------------------------------------------------
    private fun startCapture(s: CoroutineScope, ws: WebSocketClient) {
        captureJob?.cancel()
        val bufferSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        ) * 2

        val record = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferSize
            )
        } catch (e: Exception) {
            android.util.Log.e(TAG, "AudioRecord construct failed: ${e.message}", e)
            s.launch { _events.emit(SttEvent.Error("Mic start failed: ${e.message}")) }
            stop()
            return
        }

        if (record.state != AudioRecord.STATE_INITIALIZED) {
            android.util.Log.e(TAG, "AudioRecord failed to initialize")
            try { record.release() } catch (_: Exception) {}
            s.launch { _events.emit(SttEvent.Error("Microphone initialization failed")) }
            stop()
            return
        }

        audioRecord = record
        try {
            record.startRecording()
        } catch (e: Exception) {
            android.util.Log.e(TAG, "startRecording failed: ${e.message}", e)
            releaseAudioRecord()
            s.launch { _events.emit(SttEvent.Error("Mic start failed: ${e.message}")) }
            stop()
            return
        }
        android.util.Log.d(TAG, "Mic started: ${SAMPLE_RATE}Hz, buffer=$bufferSize")

        captureJob = s.launch {
            try {
                val buffer = ShortArray(bufferSize / 2)
                while (_isStreaming.value) {
                    val rec = audioRecord ?: break
                    val readCount = rec.read(buffer, 0, buffer.size)
                    if (readCount > 0) {
                        _amplitude.value = rmsAmplitude(buffer, readCount)
                        // Convert shorts to little-endian PCM16 bytes.
                        val bytes = ByteArray(readCount * 2)
                        for (i in 0 until readCount) {
                            bytes[i * 2] = (buffer[i].toInt() and 0xFF).toByte()
                            bytes[i * 2 + 1] = (buffer[i].toInt() shr 8 and 0xFF).toByte()
                        }
                        val b64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                        val audioMsg = buildJsonObject {
                            put("type", "stt_audio")
                            put("pcm", b64)
                        }
                        if (!ws.send(audioMsg.toString())) {
                            android.util.Log.w(TAG, "stt_audio send failed — connection dead")
                            break
                        }
                    }
                }
            } catch (e: Exception) {
                android.util.Log.e(TAG, "Capture loop error: ${e.message}", e)
            } finally {
                releaseAudioRecord()
                _amplitude.value = 0f
            }
        }
    }

    private suspend fun parseMessage(raw: String, epoch: Int) {
        if (epoch != sessionEpoch) return   // a newer session started — drop late events
        try {
            val obj = json.parseToJsonElement(raw).jsonObject
            val type = obj["type"]?.jsonPrimitive?.content ?: return
            when (type) {
                "stt_delta" -> {
                    val text = obj["text"]?.jsonPrimitive?.content ?: ""
                    _events.emit(SttEvent.Delta(text)) // cumulative — emit verbatim
                }
                "stt_final" -> {
                    val text = obj["text"]?.jsonPrimitive?.content ?: ""
                    _events.emit(SttEvent.Final(text))
                }
                "stt_error" -> {
                    val msg = obj["message"]?.jsonPrimitive?.content ?: "Transcription error"
                    _events.emit(SttEvent.Error(msg))
                    android.util.Log.e(TAG, "Server error: $msg")
                }
            }
        } catch (e: Exception) {
            android.util.Log.e(TAG, "Parse error: ${e.message}")
        }
    }

    /** Stop + release the mic in a single guarded path used everywhere. */
    private fun releaseAudioRecord() {
        val rec = audioRecord ?: return
        audioRecord = null
        try { rec.stop() } catch (_: Exception) {}
        try { rec.release() } catch (_: Exception) {}
    }

    /** Tear down the WS + coroutine scope. Used after the stop() grace period. */
    private fun shutdown() {
        captureJob?.cancel()
        captureJob = null
        connectionJob?.cancel()
        connectionJob = null
        wsClient.close()
        releaseAudioRecord()
        _amplitude.value = 0f
        _isStreaming.value = false
        scope?.cancel()
        scope = null
    }

    /**
     * Tear down ONE session's own captured resources. Safe to run even after a
     * newer session has started: it only cancels/closes the captured objects, and
     * it clears the shared fields/state only if this is still the active session
     * (identity-guarded), so it can never tear down a newer session (closes I1).
     */
    private fun teardownSession(
        epoch: Int,
        sScope: CoroutineScope,
        sConn: Job?,
        sCap: Job?,
        sWs: WebSocketClient,
    ) {
        sCap?.cancel()
        sConn?.cancel()
        try { sWs.close() } catch (_: Exception) {}
        sScope.cancel()
        if (epoch == sessionEpoch) {
            if (captureJob === sCap) captureJob = null
            if (connectionJob === sConn) connectionJob = null
            if (scope === sScope) scope = null
            _isStreaming.value = false
            _amplitude.value = 0f
        }
    }

    /** Cleanup on unexpected WS termination (error/closing/disconnected). */
    private fun cleanup() {
        _isStreaming.value = false
        releaseAudioRecord()
        _amplitude.value = 0f
        captureJob?.cancel()
        captureJob = null
    }
}

sealed class SttEvent {
    data class Delta(val text: String) : SttEvent()   // cumulative interim
    data class Final(val text: String) : SttEvent()
    data class Error(val message: String) : SttEvent()
}
