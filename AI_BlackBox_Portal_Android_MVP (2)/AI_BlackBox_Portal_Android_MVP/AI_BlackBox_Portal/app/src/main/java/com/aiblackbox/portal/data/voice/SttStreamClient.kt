package com.aiblackbox.portal.data.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.util.Base64
import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BINS
import com.aiblackbox.portal.ui.components.aurora.AnalysisFrame
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.voice.rmsAmplitude
import com.aiblackbox.portal.util.Constants
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.DelicateCoroutinesApi
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
import java.util.concurrent.atomic.AtomicBoolean

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
 *     `{"type":"stt_error","message":...,"code":...,"provider":...,"switchable":...}`,
 *     `{"type":"stt_status","state":"provider_override"|"loading_models"|...}`,
 *     `{"type":"stt_done"}` — TERMINAL, always the server's last frame (2026-07-09).
 *
 * An `stt_error` ends the session (audit rank 52, A08-cli-agent-ui-07 / W24): capture stops at
 * once and the reconnect loop does not resume, so an exhausted provider is not re-dialed and no
 * UI can leave the mic streaming behind an idle button. `stt_status` frames feed [status]. Frame
 * parsing lives in [SttFrames]; tests/fixtures/stt_ws_frames.json pins it for this app, the
 * server and the Portal.
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
 *
 * @param unprocessedSupported the device's answer to
 *   `AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED`, from
 *   [MicSourcePolicy.supportsUnprocessed]. Passed in rather than read here because this client
 *   holds no Context on purpose — and because the capability is the ONLY thing that may decide
 *   whether the unprocessed source is opened (see [MicSourcePolicy]).
 * @param wsFactory opens one socket per reconnect leg. Testability seam, as in [VoiceClient]:
 *   production uses the real [WebSocketClient]; unit tests inject a fake.
 * @param recordFactory builds the mono PCM16 `AudioRecord` for one source, rate and buffer size.
 *   Production always takes the default (the platform constructor); it is a seam so a JVM test can
 *   hand the capture loop a recorder that actually delivers audio and reports which thread stopped
 *   and released it.
 */
class SttStreamClient(
    private val client: OkHttpClient,
    private val baseWsUrl: String,
    private val unprocessedSupported: Boolean,
    private val wsFactory: (OkHttpClient) -> WebSocketClient = { WebSocketClient(it) },
    private val recordFactory: (androidSource: Int, sampleRate: Int, bufferBytes: Int) -> AudioRecord =
        { androidSource, sampleRate, bufferBytes ->
            AudioRecord(
                androidSource,
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferBytes,
            )
        },
) {

    // Per-session socket client: each start() gets its OWN instance so an old
    // session's socket lifecycle can never touch a newer session's (closes I2).
    private var wsClient: WebSocketClient = WebSocketClient(client)
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _events = MutableSharedFlow<SttEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<SttEvent> = _events.asSharedFlow()

    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    /**
     * Aurora band energies for the composer's ribbon, from THIS client's own tap.
     *
     * Its own, not a shared one: the app can have this mic and the voice screen's AI playback live
     * at the same moment, and a shared auto-gain reference would let one stream rescale the other's
     * ribbon.
     */
    private val _bands = MutableStateFlow(AURORA_SILENT_BANDS)
    val bands: StateFlow<FloatArray> = _bands.asStateFlow()

    /**
     * The same instants' log-frequency SPECTRUM — the tap's second channel, raw and uncompressed
     * (see [AudioAnalysisTap]). Published beside the bands rather than instead of them: they are two
     * readings of one hop and a renderer that wants both must not have to align two cadences.
     */
    private val _bins = MutableStateFlow(AURORA_SILENT_BINS)
    val bins: StateFlow<FloatArray> = _bins.asStateFlow()

    /**
     * Dropped (not reset()) per start(), then built by the first leg that actually opens a mic, and
     * read ONCE per socket leg into a capture-loop local.
     *
     * A tap belongs to one thread (it owns a PCM ring and a stateful analyser). start() runs on the
     * caller's while the previous session's capture loop may still be draining an in-flight
     * AudioRecord.read(), so resetting in place would let that straggler chunk interleave with the
     * reset and leave the new session's warm-up mid-write. Handing the new session a NEW object
     * means the straggler scribbles harmlessly on an orphan instead. The reconnect legs of ONE
     * session deliberately share it — same speaker, same room, so the gain reference should survive
     * the drop rather than re-seed on every leg.
     *
     * Built LATE, in [startCapture], because its calibration belongs to the source that opened
     * ([MicSource.calibration]) — which is not known until the AudioRecord is up.
     */
    @Volatile private var tap: AudioAnalysisTap? = null

    private val _isStreaming = MutableStateFlow(false)
    val isStreaming: StateFlow<Boolean> = _isStreaming.asStateFlow()

    /**
     * The current session's `stt_status`: the provider the server really streams with when it
     * overrode the pick, and whether the on-box models are warming. Clean at every start() and
     * whenever the session ends, so a chip rendered from it lives exactly as long as the session.
     */
    private val _status = MutableStateFlow(SttUiStatus())
    val status: StateFlow<SttUiStatus> = _status.asStateFlow()

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

    // The current leg's recorder, for identity only: its capture loop reads, stops and releases it
    // through a local, and clears this only while it still names that leg (see [startCapture]).
    @Volatile
    private var audioRecord: AudioRecord? = null

    // The current capture leg's stop signal — one per leg, flipped (the recorder itself never
    // touched) by whoever ends that leg. See [startCapture] for why a leg needs its OWN signal.
    @Volatile
    private var captureLive: AtomicBoolean? = null

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
        _status.value = SttUiStatus()
        silenceRibbon()
        // A new press is a new speaker, a new distance and a new room: a fresh tap re-seeds the
        // gain and re-runs the warm-up rather than inheriting the last press's.
        tap = null
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
            // The current leg's capture signal, as startCapture handed it back. This loop ends ITS
            // leg through this local, never through the shared field, which by then may name a
            // newer session's leg.
            var legCapture: AtomicBoolean? = null
            // Reconnect loop: hold ONE logical STT session across transient WS drops
            // (the ~30s Tailscale idle-reap) for as long as the user holds the mic.
            // Each iteration is one physical socket; a drop mid-recording reconnects
            // and resumes rather than ending the session.
            while (isActive && _isStreaming.value && epoch == sessionEpoch) {
                val sessionWs = wsFactory(client)
                wsClient = sessionWs
                try {
                    sessionWs.connect(url).collect { msg ->
                        when (msg) {
                            is WsMessage.Connected -> {
                                sessionWs.send(startMsgText)
                                android.util.Log.d(TAG, "Sent stt_start (provider='$provider', lang='$lang', target='$target')")
                                legCapture = startCapture(newScope, sessionWs)
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
                // so a fresh AudioRecord binds to the next socket — by SIGNAL only: the
                // leg's capture loop releases its own recorder, on its own thread, within
                // one read. Releasing it from here raced that loop's in-flight read().
                legCapture?.set(false)
                // User stop(), a newer session, a server stt_error (terminal: it
                // cleared _isStreaming), or a clean end after our stt_stop → end the
                // logical session WITHOUT reconnecting.
                if (userStopped || !_isStreaming.value || epoch != sessionEpoch) break
                // FOLLOW-UP (not implemented): a bare stt_done with NO stop pending
                // and no stt_error also marks a deliberate server-side end; it could
                // skip the reconnect too. Today doneSignal is simply re-armed below
                // and the drop heuristics decide.
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
            legCapture?.set(false)
            silenceRibbon()
            if (epoch == sessionEpoch) {
                // Socket + reconnect loop are done: nothing further can arrive, so
                // a stop() waiting on stt_done must not park until its backstop.
                doneSignal.complete(Unit)
                _isStreaming.value = false
                _status.value = SttUiStatus()
            }
        }
    }

    /**
     * Stop capture and end the session: send stt_stop, signal the mic off, then
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
        _status.value = SttUiStatus()

        // End the capture leg by SIGNAL only: its loop stops and releases the mic itself, in its
        // own finally, within one read. Releasing it here, on the caller's thread (the CLI mic tap,
        // on main), raced that loop's in-flight read() — the native abort in
        // AudioRecord::releaseBuffer (feedback_android_audiorecord_race).
        signalCaptureStop()
        silenceRibbon()

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
    /**
     * Open this leg's mic and start its capture loop. Returns the leg's stop signal, or null when no
     * mic started.
     *
     * The loop OWNS the recorder from here: it reads, stops and releases it through a LOCAL, on its
     * one thread, and everything else only flips the returned signal. Releasing it from any other
     * thread — stop() on main, the connection loop at a leg's end, shutdown() on a re-press, or an
     * older leg's loop releasing the shared field — raced an in-flight read() and aborts natively in
     * AudioRecord::releaseBuffer (feedback_android_audiorecord_race).
     *
     * The signal is per LEG rather than the shared [_isStreaming] alone: a quick stop() + start()
     * flips that straight back on while the old loop is still inside read(), and the old loop would
     * carry on. It is started ATOMIC because [teardownSession] and [shutdown] cancel the scope — a
     * launch cancelled before it is dispatched never runs its body, and the body's finally is the
     * only place this leg's recorder is released.
     */
    @OptIn(DelicateCoroutinesApi::class)
    private fun startCapture(s: CoroutineScope, ws: WebSocketClient): AtomicBoolean? {
        captureJob?.cancel()
        signalCaptureStop()
        val bufferSize = MicBuffer.recordBufferBytes(
            AudioRecord.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
        )

        // SIMPLEX capture: this mic is the composer's, and nothing of ours is playing while it is
        // open, so there is no echo to cancel and no reason to pay VOICE_COMMUNICATION's processing
        // for one. The chain (and the fact that UNPROCESSED is only ever in it when the DEVICE says
        // so) is [MicSourcePolicy]'s; falling past an entry here means that source would not open
        // AT ALL, which is a different failure from the silent degrade the policy guards.
        val opened = MicSourcePolicy.openFirst(
            MicSourcePolicy.simplexChain(unprocessedSupported)
        ) { source ->
            try {
                val candidate = recordFactory(source.androidSource, SAMPLE_RATE, bufferSize)
                if (candidate.state == AudioRecord.STATE_INITIALIZED) {
                    candidate
                } else {
                    android.util.Log.w(TAG, "${source.name} did not initialize — trying the next source")
                    try { candidate.release() } catch (_: Exception) {}
                    null
                }
            } catch (e: Exception) {
                android.util.Log.w(TAG, "${source.name} construct failed: ${e.message}", e)
                null
            }
        }

        if (opened == null) {
            android.util.Log.e(TAG, "No mic source could be opened")
            s.launch { _events.emit(SttEvent.Error("Microphone initialization failed")) }
            stop()
            return null
        }
        val record = opened.record

        // Built here, on the FIRST leg that gets a mic, from the calibration of the source that
        // actually opened — the analyser's constants describe a stream shape, and this is where the
        // stream's shape is finally known. Later legs of the same session reuse it (same speaker,
        // same room); read ONCE into a local so a start() racing this loop can only swap what the
        // NEXT leg picks up.
        val sessionTap = tap
            ?: AudioAnalysisTap(SAMPLE_RATE, calibration = opened.source.calibration, label = "stt-mic/${opened.source}").also { tap = it }
        android.util.Log.d(TAG, "Mic source: ${opened.source.name}")

        try {
            record.startRecording()
        } catch (e: Exception) {
            android.util.Log.e(TAG, "startRecording failed: ${e.message}", e)
            // Never handed to a loop, so nothing can be reading it: releasing it here is safe.
            try { record.release() } catch (_: Exception) {}
            s.launch { _events.emit(SttEvent.Error("Mic start failed: ${e.message}")) }
            stop()
            return null
        }
        android.util.Log.d(TAG, "Mic started: ${SAMPLE_RATE}Hz, buffer=$bufferSize")

        val live = AtomicBoolean(true)
        audioRecord = record
        captureLive = live
        captureJob = s.launch(start = CoroutineStart.ATOMIC) {
            // Hoisted out of the read loop on purpose: the tap calls this synchronously, on this
            // thread, once per completed hop — allocating a lambda per read would put a capture in
            // the audio path for nothing. The frame's arrays are freshly built and belong to us, so
            // they go straight into the flows with no copy.
            val publish: (AnalysisFrame) -> Unit = { frame ->
                _bands.value = frame.bands
                _bins.value = frame.bins
            }
            try {
                val buffer = ShortArray(MicBuffer.readLengthSamples(bufferSize))
                while (isActive && live.get() && _isStreaming.value) {
                    val readCount = record.read(buffer, 0, buffer.size)
                    // A stop that landed while this read was in flight ends the leg HERE: the chunk
                    // belongs to a leg that is over, and stt_stop may already be on the wire.
                    if (!live.get()) break
                    if (readCount > 0) {
                        _amplitude.value = rmsAmplitude(buffer, readCount)
                        // Convert shorts to little-endian PCM16 bytes.
                        val bytes = ByteArray(readCount * 2)
                        for (i in 0 until readCount) {
                            bytes[i * 2] = (buffer[i].toInt() and 0xFF).toByte()
                            bytes[i * 2 + 1] = (buffer[i].toInt() shr 8 and 0xFF).toByte()
                        }
                        // Analysed BEFORE the send, on the same bytes the server gets: the ribbon
                        // reports the microphone, so it keeps moving through a transport drop
                        // while the reconnect loop resumes underneath it. The tap decides the
                        // cadence now — a read shorter than a hop publishes nothing and carries its
                        // audio forward, a longer one publishes every hop it completes.
                        sessionTap.offer(bytes, bytes.size, publish)
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
                // Stopped and released HERE, on the SAME thread as read() — see the KDoc above.
                try { record.stop() } catch (_: Exception) {}
                try { record.release() } catch (_: Exception) {}
                // Only while the field still names this leg: after a reconnect or a re-press it
                // names a newer leg's recorder, which is that leg's loop's alone to release.
                if (audioRecord === record) audioRecord = null
                silenceRibbon()
            }
        }
        return live
    }

    private suspend fun parseMessage(raw: String, epoch: Int) {
        if (epoch != sessionEpoch) return   // a newer session started — drop late events
        try {
            val obj = json.parseToJsonElement(raw).jsonObject
            if (obj["type"]?.jsonPrimitive?.content == "stt_done") {
                // Terminal marker — ALWAYS the server's last frame (after any
                // trailing final / error). Wakes the stop() coordinator so
                // teardown happens the moment the session is truly over.
                android.util.Log.d(TAG, "Received stt_done (terminal)")
                doneSignal.complete(Unit)
                return
            }
            when (val event = SttFrames.parse(obj) ?: return) {
                is SttEvent.Delta -> {
                    lastInterim = event.text  // newest partial — replayed as a Final on reconnect
                    _events.emit(event) // cumulative — emit verbatim
                }
                is SttEvent.Final -> {
                    lastInterim = ""  // committed — nothing pending for a reconnect
                    _events.emit(event)
                }
                is SttEvent.Error -> {
                    // TERMINAL: stop capture now and keep the reconnect loop from
                    // resuming when the server closes the socket. Before, capture
                    // streamed on and the reconnect re-dialed the same exhausted
                    // provider while the CLI mic showed idle (A08-cli-agent-ui-07, W24).
                    android.util.Log.e(TAG, "Server error (${event.code}, provider=${event.provider}): ${event.message}")
                    _isStreaming.value = false
                    _status.value = SttUiStatus()
                    signalCaptureStop()
                    silenceRibbon()
                    _events.emit(event)
                }
                is SttEvent.Status -> {
                    _status.value = _status.value.reduce(event)
                    _events.emit(event)
                }
                is SttEvent.SessionEnded -> Unit  // client-side only, never parsed
            }
        } catch (e: Exception) {
            android.util.Log.e(TAG, "Parse error: ${e.message}")
        }
    }

    /**
     * The ribbon inputs for "nothing is being captured", in one place because they must always fall
     * together — a teardown that zeroed the amplitude but left the bands at their last value would
     * park a frozen ribbon on the composer for as long as the screen stayed open.
     */
    private fun silenceRibbon() {
        _amplitude.value = 0f
        _bands.value = AURORA_SILENT_BANDS
        _bins.value = AURORA_SILENT_BINS
    }

    /**
     * End the current capture leg by SIGNAL only. Its loop stops and releases the recorder itself,
     * on its own thread, within one read — see [startCapture] for why nothing else may.
     */
    private fun signalCaptureStop() {
        captureLive?.set(false)
    }

    /** Tear down the WS + coroutine scope. Used after the stop() grace period. */
    private fun shutdown() {
        captureJob?.cancel()
        captureJob = null
        connectionJob?.cancel()
        connectionJob = null
        wsClient.close()
        signalCaptureStop()
        silenceRibbon()
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
                silenceRibbon()
            }
        }
    }
}

sealed class SttEvent {
    data class Delta(val text: String) : SttEvent()   // cumulative interim
    data class Final(val text: String) : SttEvent()

    /**
     * The session is over (an stt_error is terminal, and so is a client-side failure).
     * [code]/[provider]/[switchable] come from the server (see [SttFrames]); [switchable] means
     * another provider could serve the dictation, so the UI offers the switch-provider prompt.
     */
    data class Error(
        val message: String,
        val code: String = SttFrames.CODE_PROVIDER_ERROR,
        val provider: String? = null,
        val switchable: Boolean = false,
    ) : SttEvent()

    /** A server `stt_status` frame; [SttStreamClient.status] holds its effect on the session. */
    data class Status(
        val state: String,
        val provider: String? = null,
        val overriddenFrom: String? = null,
    ) : SttEvent()

    /**
     * Terminal (2026-07-09): the stop window is over and NOTHING further will be
     * emitted for this session — any trailing/fallback [Final] has already been
     * delivered. UI state machines key their exit on this (never on a timer).
     */
    object SessionEnded : SttEvent()
}
