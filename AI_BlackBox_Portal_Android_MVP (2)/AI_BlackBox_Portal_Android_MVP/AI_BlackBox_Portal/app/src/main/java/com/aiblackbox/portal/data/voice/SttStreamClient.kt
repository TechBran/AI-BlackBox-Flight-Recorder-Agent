package com.aiblackbox.portal.data.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import com.aiblackbox.portal.ui.voice.rmsAmplitude
import com.aiblackbox.portal.util.Constants
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
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
 *     `{"type":"stt_error","message":...}`,
 *     `{"type":"stt_done"}` — TERMINAL, always the server's last frame (2026-07-09).
 *
 * The `stt_delta.text` is CUMULATIVE (full interim so far); this client emits it
 * verbatim — the applier that replaces the interim region lives downstream.
 *
 * Stop handshake (2026-07-09, fixes the intermittent lost-final/stuck-spinner
 * bug): stop() sends stt_stop and then WAITS (bounded) for the server's
 * terminal `stt_done` — which arrives right after the trailing `stt_final` —
 * instead of sleeping a blind grace. Happy path tears down as soon as the
 * final lands (usually faster than the old 1200ms). If the socket is already
 * dead (send fails) or the server never terminates, the newest partial is
 * committed as a fallback [SttEvent.Final] (the reconnect path's mechanism)
 * so the words the user watched appear are not dropped. [SttEvent.SessionEnded]
 * is then emitted so UI state machines (CLI mic spinner, composer) always exit.
 * A server-side hallucination-filtered stop sends an authoritative EMPTY final
 * first, which clears the pending interim — the fallback then correctly no-ops.
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

    // Reconnect-resume (Brandon 2026-07-05): a network/proxy drop mid-recording
    // (the ~30s Tailscale idle-reap of the WS) must NOT end the session — the WS
    // lifetime tracks the mic button, so we transparently reconnect and continue
    // for as long as _isStreaming stays true. userStopped distinguishes a user
    // stop() (do NOT reconnect) from an unexpected drop. lastInterim is the newest
    // cumulative partial, replayed as a Final before a reconnect so the fresh
    // server session's deltas append after it (no transcribed words lost). Bounded.
    @Volatile private var userStopped = false
    @Volatile private var reconnectAttempts = 0
    @Volatile private var lastInterim = ""
    private val maxReconnects = 40  // ~1 per 30s cap → ~20 min continuous dictation

    // Terminal-marker coordination (2026-07-09): completed when the server's
    // {"type":"stt_done"} arrives (or when the socket + reconnect loop are fully
    // over, so a server that died without one can't park stop() until its
    // backstop). Re-armed per start() AND per reconnect leg, so a stale
    // completion can never satisfy a future stop() instantly.
    @Volatile private var doneSignal = CompletableDeferred<Unit>()

    @Volatile
    private var audioRecord: AudioRecord? = null

    companion object {
        // FIXED — do NOT branch by backend. OpenAI realtime transcription rejects
        // anything < 24000; Google accepts 24000; the web client uses 24000.
        private const val SAMPLE_RATE = 24000
        // Max wait after stt_stop for the server's terminal stt_done (which
        // arrives right after the trailing stt_final). Replaces the old blind
        // 1200ms grace, which raced providers with NO flush deadline (Google
        // gRPC — journal-proven lost finals). The server bounds its own drains
        // at 5-8s, so this is the disaster backstop, not the expected wait.
        // `internal` so UI backstops layered ABOVE it (CliMicButton's
        // TRANSCRIBING_TIMEOUT_MS) can assert their ordering against the real
        // constant in unit tests.
        internal const val STOP_BACKSTOP_MS = 10_000L
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
        userStopped = false
        reconnectAttempts = 0
        lastInterim = ""
        doneSignal = CompletableDeferred()

        val newScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = newScope

        val url = baseWsUrl + Constants.WS_STT
        android.util.Log.d(TAG, "Connecting to: $url")
        val startMsgText = buildJsonObject {
            put("type", "stt_start")
            put("target", target)
            put("provider", provider)
            put("lang", lang)
            put("sample_rate", SAMPLE_RATE)
        }.toString()

        connectionJob = newScope.launch {
            // Reconnect loop: hold ONE logical STT session across transient WS drops
            // (the ~30s Tailscale idle-reap) for as long as the user holds the mic.
            // Each iteration is one physical socket; a drop mid-recording reconnects
            // and resumes rather than ending the session.
            while (isActive && _isStreaming.value && epoch == sessionEpoch) {
                val sessionWs = WebSocketClient(client)
                wsClient = sessionWs
                try {
                    sessionWs.connect(url).collect { msg ->
                        when (msg) {
                            is WsMessage.Connected -> {
                                sessionWs.send(startMsgText)
                                android.util.Log.d(TAG, "Sent stt_start (provider='$provider', lang='$lang', target='$target')")
                                startCapture(newScope, sessionWs)
                            }
                            is WsMessage.Text -> parseMessage(msg.text, epoch)
                            is WsMessage.Closing ->
                                android.util.Log.w(TAG, "Server closing: ${msg.code} ${msg.reason}")
                            is WsMessage.Error ->
                                android.util.Log.e(TAG, "WS error: ${msg.error.message}")
                            is WsMessage.Disconnected ->
                                android.util.Log.d(TAG, "WS disconnected")
                        }
                    }
                } catch (e: Exception) {
                    android.util.Log.e(TAG, "Connection loop error: ${e.message}", e)
                }
                // The collect returned = THIS socket closed. Stop this leg's capture
                // so a fresh AudioRecord binds to the next socket.
                releaseAudioRecord()
                // User stop(), a newer session, or a clean end after our stt_stop →
                // end the logical session WITHOUT reconnecting.
                if (userStopped || !_isStreaming.value || epoch != sessionEpoch) break
                // FOLLOW-UP (not implemented): a received stt_done with NO stop
                // pending marks a DELIBERATE server-side session end — it could
                // discriminate that from a transport drop here and skip the
                // reconnect. Today doneSignal is simply re-armed below and the
                // drop heuristics decide.
                // Unexpected mid-recording drop → reconnect + resume.
                reconnectAttempts++
                if (reconnectAttempts > maxReconnects) {
                    if (epoch == sessionEpoch) _events.emit(
                        SttEvent.Error("Transcription connection lost — please try again"))
                    break
                }
                // Commit the newest partial so the FRESH session's deltas append after
                // it (the server restarts its cumulative transcript at empty on the new
                // socket). No transcribed words are lost across the seam.
                if (lastInterim.isNotBlank() && epoch == sessionEpoch) {
                    _events.emit(SttEvent.Final(lastInterim))
                    lastInterim = ""
                }
                // A fresh server session begins on the next socket — re-arm the
                // terminal signal so a stale stt_done from THIS leg can't satisfy
                // a future stop() instantly.
                if (epoch == sessionEpoch) doneSignal = CompletableDeferred()
                android.util.Log.w(TAG, "STT WS dropped mid-session — reconnecting & resuming (attempt $reconnectAttempts)")
                delay(200)  // brief backoff; _isStreaming stays TRUE so the mic stays lit
            }
            // Logical session over — flip to idle ONCE (do not cancel our own job here).
            releaseAudioRecord()
            _amplitude.value = 0f
            if (epoch == sessionEpoch) {
                // Socket + reconnect loop are done: nothing further can arrive, so
                // a stop() waiting on stt_done must not park until its backstop.
                doneSignal.complete(Unit)
                _isStreaming.value = false
            }
        }
    }

    /**
     * Stop capture and end the session: send stt_stop, release the mic, then
     * wait (bounded by [STOP_BACKSTOP_MS]) for the server's terminal `stt_done`
     * — which arrives right after the trailing stt_final — before tearing the
     * WS down. If stt_stop can't be delivered (socket dead) the wait is skipped
     * entirely. If the stop window closes with NO stt_final having arrived, the
     * newest partial is committed as a fallback [SttEvent.Final] (the reconnect
     * path's mechanism) so the utterance isn't dropped. [SttEvent.SessionEnded]
     * always follows. Idempotent and leak-safe.
     */
    fun stop() {
        if (!_isStreaming.value && audioRecord == null && connectionJob == null) return
        if (graceJob?.isActive == true) return  // a stop is already in flight
        // Mark user-initiated so the reconnect loop exits instead of resuming when
        // its socket closes after the stt_stop below.
        userStopped = true
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
            val done = doneSignal
            graceJob = s.launch {
                var sent = false
                try {
                    sent = sWs.send(buildJsonObject { put("type", "stt_stop") }.toString())
                } catch (_: Exception) {}
                android.util.Log.d(TAG, "Sent stt_stop (delivered=$sent)")
                if (sent) {
                    // Bounded wait for the terminal stt_done. Happy path: the
                    // server sends final+done as soon as the provider flushes
                    // (typically well under the old blind 1200ms grace). The
                    // connection collector stays alive through this window, so a
                    // late trailing final still delivers. Backstop only fires if
                    // the server/provider wedges (its own drains cap at 5-8s).
                    withTimeoutOrNull(STOP_BACKSTOP_MS) { done.await() }
                } else {
                    android.util.Log.w(TAG, "stt_stop send failed — socket dead, skipping stt_done wait")
                }
                // No stt_final made it home (dead socket / server timeout / done
                // without a final): commit the newest partial so the words the
                // user watched appear aren't dropped — the exact mechanism the
                // reconnect path uses. parseMessage clears lastInterim on every
                // real final (including the authoritative EMPTY final a
                // hallucination-filtered stop sends), so this no-ops whenever a
                // final DID arrive.
                if (epoch == sessionEpoch && lastInterim.isNotBlank()) {
                    android.util.Log.w(TAG, "stop window closed without a final — committing newest partial")
                    _events.emit(SttEvent.Final(lastInterim))
                    lastInterim = ""
                }
                // Terminal event for UI state machines (CLI mic spinner exit,
                // composer cleanup) — the session is over, nothing follows.
                if (epoch == sessionEpoch) _events.emit(SttEvent.SessionEnded)
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
                    lastInterim = text  // newest partial — replayed as a Final on reconnect
                    _events.emit(SttEvent.Delta(text)) // cumulative — emit verbatim
                }
                "stt_final" -> {
                    val text = obj["text"]?.jsonPrimitive?.content ?: ""
                    lastInterim = ""  // committed — nothing pending for a reconnect
                    _events.emit(SttEvent.Final(text))
                }
                "stt_error" -> {
                    val msg = obj["message"]?.jsonPrimitive?.content ?: "Transcription error"
                    _events.emit(SttEvent.Error(msg))
                    android.util.Log.e(TAG, "Server error: $msg")
                }
                "stt_done" -> {
                    // Terminal marker — ALWAYS the server's last frame (after any
                    // trailing final / error). Wakes the stop() coordinator so
                    // teardown happens the moment the session is truly over.
                    android.util.Log.d(TAG, "Received stt_done (terminal)")
                    doneSignal.complete(Unit)
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
            // Nest the StateFlow resets under the same identity guard: in the fast
            // re-press race a stale grace must NOT flip a newer session's flags —
            // the capture loop gates on `while (_isStreaming.value)`, so clobbering
            // it to false would silently kill the new mic.
            if (scope === sScope) {
                scope = null
                _isStreaming.value = false
                _amplitude.value = 0f
            }
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

    /**
     * Terminal (2026-07-09): the stop window is over and NOTHING further will be
     * emitted for this session — any trailing/fallback [Final] has already been
     * delivered. UI state machines key their exit on this (never on a timer).
     */
    object SessionEnded : SttEvent()
}
