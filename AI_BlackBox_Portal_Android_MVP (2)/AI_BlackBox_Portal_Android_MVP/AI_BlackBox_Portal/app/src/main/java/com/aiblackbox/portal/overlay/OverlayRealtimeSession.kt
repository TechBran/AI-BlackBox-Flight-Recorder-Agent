package com.aiblackbox.portal.overlay

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.os.Handler
import android.util.Base64
import android.util.Log
import androidx.core.content.ContextCompat
import com.aiblackbox.portal.data.api.HttpClients
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.DelicateCoroutinesApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "OverlayService"

/**
 * The overlay's live voice session — GPT Realtime, Gemini Live or Grok Live over the box's
 * /ws/{realtime,gemini-live,grok-live}/{id} socket — out of OverlayService (audit
 * A02-overlay-service-12, rank 76): the socket and its messages, the AI audio playback
 * (AudioTrack, sequential queue, Gemini pre-buffer), the mic capture loop, the 1 FPS screen
 * share, the Gemini-side Whisper transcription of the user, and the transcript. Moved
 * verbatim; what it shows on screen goes through [Host], which the service implements
 * (the phone panel's views, or the XR bridge).
 */
internal class OverlayRealtimeSession(
    private val context: Context,
    private val handler: Handler,
    private val scope: CoroutineScope,
    private val host: Host,
) {
    /** What the session needs from the overlay: its box, its operator and its screen. */
    interface Host {
        val serverUrl: String
        val operator: String
        /** True (and the reason shown) when no operator is resolved to talk as. */
        fun operatorBlocks(): Boolean
        fun showToast(message: String)
        fun updateStatus(status: String)
        /** The connect/mic/share controls follow [isConnected]. */
        fun updateRealtimeUi()
        fun updateMicUi(isActive: Boolean, userMuted: Boolean = false, aiSpeaking: Boolean = false)
        fun updateBubbleGlow()
        fun updateScreenShareUi(isActive: Boolean)
        fun syncBridgeState()
        /** The connecting state: shown while the socket opens, cleared once it is open or ends. */
        fun showConnecting(connecting: Boolean)
        /** A new transcript line ([count] lines so far). */
        fun showTranscriptEntry(speaker: String, text: String, count: Int)
        fun updateLiveResponse(text: String)
        /** Whether a valid MediaProjection exists to share the screen from. */
        fun screenCaptureAvailable(): Boolean
        /** Make sure the screen frames can be read; [onReady] says whether they can. */
        fun ensureScreenFrames(onReady: (Boolean) -> Unit)
        /** The newest screen frame as base64 JPEG, or null when none is ready. */
        fun latestScreenFrameJpegBase64(): String?
    }

    // Realtime model selection
    enum class RealtimeModel { GPT_REALTIME, GEMINI_LIVE, GROK_LIVE }
    var currentRealtimeModel: RealtimeModel = RealtimeModel.GEMINI_LIVE

    // Realtime connection state
    private var realtimeWebSocket: WebSocket? = null
    private var realtimeSessionId: String = ""
    var isRealtimeConnected = false
        private set
    var isRealtimeRecording = false  // Mic active for realtime streaming
        private set
    var isScreenSharing = false
        private set

    // Screen share for realtime models (1 FPS frame capture) - supports both Gemini Live and GPT Realtime
    private var screenShareJob: Job? = null
    private val SCREEN_SHARE_INTERVAL_MS = 1000L  // 1 FPS

    // Realtime audio streaming (separate from STT recording). The capture loop owns its AudioRecord
    // outright (a local, released in its own finally); everything else holds only this mic session's
    // stop signal — one per session, so stopping one can never stop (or be undone by) a newer one.
    private var realtimeCaptureLive: AtomicBoolean? = null

    // Audio playback for AI responses
    private var audioTrack: android.media.AudioTrack? = null
    private val audioTrackLock = Object()  // Lock for thread-safe AudioTrack access
    var isAISpeaking = false
        private set
    var isUserMuted = false  // User manually muted via button press
        private set

    // Audio playback completion tracking (prevents feedback loop)
    private var responseCompleteReceived = false  // Backend said response is done
    private var aiStoppedSpeakingAt = 0L  // Timestamp when audio ACTUALLY finished playing
    private val POST_SPEECH_DELAY_MS = 800L  // Delay after audio finishes before accepting mic input

    // Sequential audio queue to prevent out-of-order playback
    private val audioPlaybackQueue = java.util.concurrent.LinkedBlockingQueue<ByteArray>()
    private var audioPlaybackJob: Job? = null

    // Pre-buffering to prevent audio clipping (especially for Gemini Live)
    private var isPreBuffering = false
    private var preBufferBytesAccumulated = 0
    private val PRE_BUFFER_THRESHOLD_BYTES = 12000  // ~250ms of 24kHz mono PCM16 audio

    // Buffer for user audio to transcribe with Whisper
    private val realtimeAudioBuffer = mutableListOf<ByteArray>()
    private var realtimeAudioBufferLock = Object()
    private val MAX_AUDIO_BUFFER_SIZE = 500  // Limit buffer to ~500 chunks (~15-30 seconds of audio)

    // Voice options per model
    val geminiVoices = arrayOf("Charon", "Puck", "Kore", "Aoede", "Fenrir", "Orus")
    val gptVoices = arrayOf("ash", "alloy", "ballad", "coral", "echo", "sage", "shimmer", "verse")
    val grokVoices = arrayOf("Ara", "Rex", "Sal", "Eve", "Leo")
    var selectedVoice: String = "Charon"  // Default voice

    // Transcript management
    data class TranscriptEntry(val speaker: String, val text: String, val timestamp: Long = System.currentTimeMillis())
    val transcriptEntries = mutableListOf<TranscriptEntry>()
    val currentAIResponse = StringBuilder()

    // OkHttp client for WebSocket (reuse existing if possible)
    private val realtimeHttpClient by lazy {
        HttpClients.base.newBuilder()
            .readTimeout(0, TimeUnit.MILLISECONDS)  // No timeout for WebSocket
            .connectTimeout(30, TimeUnit.SECONDS)
            .build()
    }

    // Counter for audio chunks (diagnostic)
    private var audioChunkCount = 0
    private var totalAudioBytesReceived = 0L

    /** The voices the selected model offers. */
    fun voicesForModel(): Array<String> = when (currentRealtimeModel) {
        RealtimeModel.GEMINI_LIVE -> geminiVoices
        RealtimeModel.GPT_REALTIME -> gptVoices
        RealtimeModel.GROK_LIVE -> grokVoices
    }

    fun connectRealtime() {
        val serverUrl = host.serverUrl
        if (serverUrl.isEmpty()) {
            host.showToast("Server URL not set")
            return
        }
        if (host.operatorBlocks()) return

        realtimeSessionId = UUID.randomUUID().toString()

        val endpoint = when (currentRealtimeModel) {
            RealtimeModel.GPT_REALTIME -> "/ws/realtime/$realtimeSessionId"
            RealtimeModel.GEMINI_LIVE -> "/ws/gemini-live/$realtimeSessionId"
            RealtimeModel.GROK_LIVE -> "/ws/grok-live/$realtimeSessionId"
        }

        // Build WebSocket URL
        val wsProtocol = if (serverUrl.startsWith("https")) "wss" else "ws"
        val baseUrl = serverUrl.replace("https://", "").replace("http://", "")
        val wsUrl = "$wsProtocol://$baseUrl$endpoint"

        Log.d(TAG, "Connecting to realtime: $wsUrl")
        host.updateStatus("Connecting...")
        host.showConnecting(true)

        val request = Request.Builder().url(wsUrl).build()
        realtimeWebSocket = realtimeHttpClient.newWebSocket(request, RealtimeWebSocketListener())
    }

    fun disconnectRealtime() {
        Log.d(TAG, "Disconnecting realtime session")
        teardownRealtimeSession("Disconnected", closeReason = "User disconnected")
    }

    /**
     * End the realtime session completely, whoever ended it (A02-overlay-service-02).
     * The user's disconnect, the server closing the socket and a dropped connection all
     * come here: before, onClosing/onFailure only flipped the label, so the mic kept
     * streaming into a dead socket (privacy indicator lit, mute button disabled), the
     * screen-share job kept spinning and the AudioTrack stayed open. Main thread only.
     */
    private fun teardownRealtimeSession(status: String, closeReason: String? = null) {
        // Stop audio and screen sharing
        stopRealtimeAudioStreaming()
        stopScreenSharing()

        // Release audio playback
        releaseAudioTrack()

        // Reset mic state
        isUserMuted = false
        isAISpeaking = false
        responseCompleteReceived = false
        isPreBuffering = false
        host.updateBubbleGlow()  // Clear bubble glow on disconnect

        // NOTE: Session transcript is saved by the BACKEND when WebSocket closes
        // (see gemini_live_routes.py save_session_to_blackbox); the overlay never saves it.

        // Close WebSocket (completes the close handshake when the server started it)
        val socket = realtimeWebSocket
        realtimeWebSocket = null
        if (closeReason != null) socket?.close(1000, closeReason)

        isRealtimeConnected = false
        host.updateRealtimeUi()
        host.updateStatus(status)
        host.showConnecting(false)
    }

    /** The service is going away: stop the mic, the share and the playback, close the socket. */
    fun shutdown() {
        stopRealtimeAudioStreaming()
        stopScreenSharing()
        releaseAudioTrack()
        // NOTE: Session transcript is saved by the BACKEND when WebSocket closes
        // (see gemini_live_routes.py save_session_to_blackbox); the overlay never saves it.
        realtimeWebSocket?.close(1000, "Service destroyed")
        realtimeWebSocket = null
        isRealtimeConnected = false
    }

    private inner class RealtimeWebSocketListener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            Log.d(TAG, "Realtime WebSocket opened")
            handler.post {
                isRealtimeConnected = true
                host.updateRealtimeUi()
                host.updateStatus("Connected")
                host.showConnecting(false)

                // Send connect message with selected voice
                val connectMsg = JSONObject().apply {
                    put("type", "connect")
                    put("operator", host.operator)
                    put("voice", selectedVoice)
                }
                webSocket.send(connectMsg.toString())

                // Reset audio state for new session
                isAISpeaking = false
                responseCompleteReceived = false
                aiStoppedSpeakingAt = 0L  // Ensures no initial post-speech delay

                // Initialize audio playback
                initAudioPlayback()

                // Auto-start microphone (continuous listening mode)
                handler.postDelayed({
                    if (isRealtimeConnected && !isRealtimeRecording) {
                        startRealtimeAudioStreaming()
                        Log.d(TAG, "Auto-started microphone for continuous listening")
                    }
                }, 500)  // Small delay to ensure connection is fully established
            }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val msg = JSONObject(text)
                handler.post { handleRealtimeMessage(msg) }
            } catch (e: Exception) {
                Log.e(TAG, "Error parsing realtime message", e)
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            Log.d(TAG, "Realtime WebSocket closing: $code - $reason")
            handler.post {
                // A late close from a socket the user already replaced is not this session's.
                if (webSocket !== realtimeWebSocket) return@post
                teardownRealtimeSession("Disconnected", closeReason = "Closing")
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.e(TAG, "Realtime WebSocket failed", t)
            handler.post {
                if (webSocket !== realtimeWebSocket) return@post
                teardownRealtimeSession("Connection failed")
                host.showToast("Connection failed: ${t.message}")
            }
        }
    }

    private fun handleRealtimeMessage(msg: JSONObject) {
        val type = msg.optString("type", "")
        val data = msg.optString("data", "")

        when (type) {
            "connected" -> {
                host.updateStatus("Ready")
                Log.d(TAG, "Realtime session ready")
            }
            "setup_complete" -> {
                Log.d(TAG, "Realtime setup complete")
            }
            "audio_delta" -> {
                // Mark AI as speaking on first audio chunk (mic streaming auto-mutes via inline check)
                if (!isAISpeaking) {
                    isAISpeaking = true
                    responseCompleteReceived = false  // Reset for new response
                    resetAudioDiagnostics()  // Reset counters and pre-buffering for new response
                    Log.d(TAG, "AI started speaking - mic auto-muted")
                    host.updateMicUi(isRealtimeRecording, aiSpeaking = true)
                    host.updateBubbleGlow()  // Update bubble glow state (blue for AI speaking)
                    host.updateStatus("AI Speaking")

                    // Transcribe user audio with Whisper - ONLY for Gemini Live
                    // GPT Realtime already provides user_transcript via OpenAI's built-in Whisper
                    if (currentRealtimeModel == RealtimeModel.GEMINI_LIVE) {
                        transcribeRealtimeAudioBuffer()
                    }
                }

                // Play audio response via AudioTrack
                if (data.isNotEmpty()) {
                    playAudioChunk(data)
                }
            }
            "transcript_delta" -> {
                // Live AI transcription
                currentAIResponse.append(data)
                host.updateLiveResponse(currentAIResponse.toString())
            }
            "user_transcript" -> {
                // User's speech transcription
                addTranscriptEntry("User", data)
            }
            "response_complete" -> {
                // Finalize AI response
                finalizeLiveResponse()

                // Mark response as complete - mic will resume when audio ACTUALLY finishes playing
                // (detected by audio playback job when queue is empty)
                responseCompleteReceived = true
                // A reply shorter than the pre-buffer threshold never ended pre-buffering,
                // so the playback job waited forever: the reply never played and the mic
                // never resumed. The whole reply is in; play what there is.
                isPreBuffering = false
                Log.d(TAG, "response_complete received - waiting for audio queue to empty")
            }
            "tool_call" -> {
                Log.d(TAG, "Tool call: $data")
            }
            "tool_result" -> {
                Log.d(TAG, "Tool result received")
            }
            "provenance" -> {
                // Plan Task 10: backend emits {"type":"provenance","data":{recent,keyword,semantic,checkpoint}}
                // Overlay has no per-turn bubble — log lineage so it appears in logcat for debugging.
                val provData = msg.optJSONObject("data")
                if (provData != null) {
                    val recent = provData.optJSONArray("recent")?.length() ?: 0
                    val keyword = provData.optJSONArray("keyword")?.length() ?: 0
                    val semantic = provData.optJSONArray("semantic")?.length() ?: 0
                    val checkpoint = provData.optJSONArray("checkpoint")?.length() ?: 0
                    Log.d(TAG, "provenance: recent=$recent keyword=$keyword semantic=$semantic checkpoint=$checkpoint")
                }
            }
            "error" -> {
                host.showToast("Error: $data")
                Log.e(TAG, "Realtime error: $data")
            }
        }
    }

    // Initialize AudioTrack for playing AI audio responses
    private fun initAudioPlayback() {
        try {
            // Stop existing playback job
            audioPlaybackJob?.cancel()
            audioPlaybackJob = null
            audioPlaybackQueue.clear()

            // Release existing AudioTrack if any
            synchronized(audioTrackLock) {
                if (audioTrack != null) {
                    try {
                        audioTrack?.stop()
                        audioTrack?.release()
                    } catch (e: Exception) {
                        Log.w(TAG, "Error releasing old AudioTrack", e)
                    }
                    audioTrack = null
                }
            }

            // Both Gemini Live and GPT Realtime output 24kHz PCM16 audio
            val outputSampleRate = 24000
            val channelConfig = AudioFormat.CHANNEL_OUT_MONO
            val audioFormat = AudioFormat.ENCODING_PCM_16BIT

            val minBufferSize = android.media.AudioTrack.getMinBufferSize(
                outputSampleRate, channelConfig, audioFormat
            )

            if (minBufferSize == android.media.AudioTrack.ERROR ||
                minBufferSize == android.media.AudioTrack.ERROR_BAD_VALUE) {
                Log.e(TAG, "Invalid AudioTrack buffer size")
                return
            }

            // Use larger buffer for smoother playback
            val bufferSize = maxOf(minBufferSize * 4, 16384)

            val track = android.media.AudioTrack.Builder()
                .setAudioAttributes(
                    android.media.AudioAttributes.Builder()
                        .setUsage(android.media.AudioAttributes.USAGE_MEDIA)
                        .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SPEECH)
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
                .setTransferMode(android.media.AudioTrack.MODE_STREAM)
                .build()

            if (track.state == android.media.AudioTrack.STATE_INITIALIZED) {
                synchronized(audioTrackLock) {
                    audioTrack = track
                    track.play()
                }
                Log.d(TAG, "AudioTrack initialized: ${outputSampleRate}Hz, buffer=${bufferSize}")

                // Start the single audio playback job that processes queue sequentially
                startAudioPlaybackJob()
            } else {
                Log.e(TAG, "AudioTrack failed to initialize")
                track.release()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to initialize AudioTrack", e)
        }
    }

    // Single job that processes audio queue sequentially (prevents out-of-order playback)
    private fun startAudioPlaybackJob() {
        audioPlaybackJob?.cancel()
        audioPlaybackJob = scope.launch(Dispatchers.IO) {
            Log.d(TAG, "Audio playback job started")
            while (isActive) {
                try {
                    // Wait during pre-buffering phase (let queue fill up for smoother playback)
                    if (isPreBuffering) {
                        delay(50)  // Check every 50ms if pre-buffering is complete
                        continue
                    }

                    // Block until audio data is available (with 100ms timeout to check isActive)
                    val audioBytes = audioPlaybackQueue.poll(100, java.util.concurrent.TimeUnit.MILLISECONDS)
                    if (audioBytes != null) {
                        synchronized(audioTrackLock) {
                            val track = audioTrack
                            if (track != null &&
                                track.state == android.media.AudioTrack.STATE_INITIALIZED &&
                                track.playState != android.media.AudioTrack.PLAYSTATE_STOPPED) {
                                track.write(audioBytes, 0, audioBytes.size)
                            }
                        }
                    } else {
                        // Queue is empty - check if response is complete (audio actually finished)
                        if (responseCompleteReceived && isAISpeaking && audioPlaybackQueue.isEmpty()) {
                            Log.d(TAG, "Audio playback ACTUALLY finished - queue empty after response_complete")
                            isAISpeaking = false
                            aiStoppedSpeakingAt = System.currentTimeMillis()
                            responseCompleteReceived = false  // Reset for next response

                            // Restart mic if user hasn't manually muted and mic isn't already running
                            handler.post {
                                host.updateStatus("Connected")
                                if (!isUserMuted && !isRealtimeRecording && isRealtimeConnected) {
                                    Log.d(TAG, "Restarting mic after AI finished speaking")
                                    startRealtimeAudioStreaming()  // This calls updateBubbleGlow()
                                } else {
                                    host.updateMicUi(isRealtimeRecording, userMuted = isUserMuted, aiSpeaking = false)
                                    host.updateBubbleGlow()  // Update bubble glow when AI stops (and mic not restarting)
                                }
                            }
                        }
                    }
                } catch (e: InterruptedException) {
                    break
                } catch (e: Exception) {
                    Log.e(TAG, "Error in audio playback job", e)
                }
            }
            Log.d(TAG, "Audio playback job ended")
        }
    }

    // Release AudioTrack resources
    fun releaseAudioTrack() {
        // Stop playback job first
        audioPlaybackJob?.cancel()
        audioPlaybackJob = null
        audioPlaybackQueue.clear()

        synchronized(audioTrackLock) {
            try {
                audioTrack?.stop()
                audioTrack?.release()
                audioTrack = null
                isAISpeaking = false
                Log.d(TAG, "AudioTrack released")
            } catch (e: Exception) {
                Log.e(TAG, "Error releasing AudioTrack", e)
            }
        }
    }

    // Queue audio chunk for sequential playback
    private fun playAudioChunk(base64Data: String) {
        try {
            val audioBytes = Base64.decode(base64Data, Base64.DEFAULT)

            // Diagnostic logging (every 10th chunk to avoid spam)
            audioChunkCount++
            totalAudioBytesReceived += audioBytes.size
            if (audioChunkCount % 10 == 1) {
                Log.d(TAG, "Audio chunk #$audioChunkCount: ${audioBytes.size} bytes, queue size: ${audioPlaybackQueue.size}, total received: ${totalAudioBytesReceived / 1024}KB, preBuffering: $isPreBuffering")
            }

            // Start pre-buffering on first chunk of new response (Gemini Live benefits most from this)
            if (audioChunkCount == 1 && currentRealtimeModel == RealtimeModel.GEMINI_LIVE) {
                isPreBuffering = true
                preBufferBytesAccumulated = 0
                Log.d(TAG, "Starting pre-buffer for Gemini Live audio")
            }

            // Track accumulated bytes during pre-buffering
            if (isPreBuffering) {
                preBufferBytesAccumulated += audioBytes.size
                if (preBufferBytesAccumulated >= PRE_BUFFER_THRESHOLD_BYTES) {
                    isPreBuffering = false
                    Log.d(TAG, "Pre-buffer complete: ${preBufferBytesAccumulated} bytes accumulated, starting playback")
                }
            }

            // Add to queue - the playback job will process it in order
            audioPlaybackQueue.offer(audioBytes)
            isAISpeaking = true
        } catch (e: Exception) {
            Log.e(TAG, "Error decoding audio chunk", e)
        }
    }

    // Reset audio diagnostic counters (call when starting new response)
    private fun resetAudioDiagnostics() {
        audioChunkCount = 0
        totalAudioBytesReceived = 0L
        isPreBuffering = false
        preBufferBytesAccumulated = 0
    }

    // Screen sharing for Gemini Live vision
    fun toggleScreenSharing() {
        if (isScreenSharing) {
            stopScreenSharing()
        } else {
            startScreenSharing()
        }
    }

    private fun startScreenSharing() {
        // Screen sharing is supported for both Gemini Live and GPT Realtime
        // Grok Voice API only supports audio/text - no vision capability
        if (currentRealtimeModel == RealtimeModel.GROK_LIVE) {
            host.showToast("Screen sharing not available - Grok is audio/text only")
            return
        }

        if (!host.screenCaptureAvailable()) {
            host.showToast("Screen capture not available")
            return
        }

        // Ensure persistent display exists
        host.ensureScreenFrames { success ->
            if (success) {
                startScreenShareCapture()
            } else {
                host.showToast("Failed to setup screen capture")
            }
        }
    }

    private fun startScreenShareCapture() {
        isScreenSharing = true
        host.updateScreenShareUi(true)
        host.syncBridgeState()

        screenShareJob = scope.launch {
            Log.d(TAG, "Starting screen share capture at 1 FPS")
            while (isActive && isScreenSharing) {
                captureAndSendFrame()
                delay(SCREEN_SHARE_INTERVAL_MS)
            }
        }
    }

    fun stopScreenSharing() {
        isScreenSharing = false
        screenShareJob?.cancel()
        screenShareJob = null
        host.updateScreenShareUi(false)
        host.syncBridgeState()
        Log.d(TAG, "Screen sharing stopped")
    }

    private fun captureAndSendFrame() {
        if (!isRealtimeConnected || realtimeWebSocket == null) return
        val base64Frame = host.latestScreenFrameJpegBase64() ?: return

        // Send video frame
        val frameMsg = JSONObject().apply {
            put("type", "video_frame")
            put("data", base64Frame)
        }
        realtimeWebSocket?.send(frameMsg.toString())
    }

    // Realtime audio streaming
    fun toggleRealtimeAudio() {
        // This is user-initiated mute/unmute
        if (isUserMuted) {
            // User is currently muted, wants to unmute
            isUserMuted = false
            if (!isAISpeaking) {
                startRealtimeAudioStreaming()
                Log.d(TAG, "User unmuted microphone")
            } else {
                // AI is speaking, will auto-resume when AI finishes
                host.updateMicUi(false, aiSpeaking = true)
                Log.d(TAG, "User unmuted, will resume when AI finishes")
            }
        } else if (isRealtimeRecording) {
            // User is listening, wants to mute
            isUserMuted = true
            stopRealtimeAudioStreaming()
            host.updateMicUi(false, userMuted = true)
            Log.d(TAG, "User muted microphone")
        } else {
            // Not recording, not muted - start recording
            isUserMuted = false
            startRealtimeAudioStreaming()
            Log.d(TAG, "User started microphone")
        }
    }

    // Transcribe buffered realtime audio with Whisper and add to transcript
    private fun transcribeRealtimeAudioBuffer() {
        // Get current sample rate for the active model
        val sampleRate = when (currentRealtimeModel) {
            RealtimeModel.GEMINI_LIVE -> 16000
            RealtimeModel.GPT_REALTIME -> 24000
            else -> 16000
        }

        // Copy and clear the buffer
        val audioChunks: List<ByteArray>
        synchronized(realtimeAudioBufferLock) {
            if (realtimeAudioBuffer.isEmpty()) {
                Log.d(TAG, "No audio to transcribe")
                return
            }
            audioChunks = realtimeAudioBuffer.toList()
            realtimeAudioBuffer.clear()
        }

        // Calculate total size and combine chunks
        val totalSize = audioChunks.sumOf { it.size }
        if (totalSize < 1000) {
            Log.d(TAG, "Audio too short to transcribe: $totalSize bytes")
            return
        }

        val combinedAudio = ByteArray(totalSize)
        var offset = 0
        for (chunk in audioChunks) {
            System.arraycopy(chunk, 0, combinedAudio, offset, chunk.size)
            offset += chunk.size
        }

        Log.d(TAG, "Transcribing ${combinedAudio.size} bytes of realtime audio at ${sampleRate}Hz")

        // Transcribe in background
        scope.launch {
            try {
                transcribeRealtimeWithWhisper(combinedAudio, sampleRate)
            } catch (e: Exception) {
                Log.e(TAG, "Error transcribing realtime audio", e)
            }
        }
    }

    // Transcribe realtime audio and add result to transcript
    private suspend fun transcribeRealtimeWithWhisper(audioData: ByteArray, sampleRate: Int) {
        val serverUrl = host.serverUrl
        if (serverUrl.isEmpty()) {
            Log.e(TAG, "No server URL for transcription")
            return
        }

        try {
            // Save audio to temp WAV file
            val audioFile = File(context.cacheDir, "realtime_input_${System.currentTimeMillis()}.wav")
            writeWavFileWithSampleRate(audioFile, audioData, sampleRate)
            Log.d(TAG, "Realtime WAV saved: ${audioFile.absolutePath}, size=${audioFile.length()}")

            val client = HttpClients.base.newBuilder()
                .connectTimeout(30, TimeUnit.SECONDS)
                .writeTimeout(60, TimeUnit.SECONDS)
                .readTimeout(60, TimeUnit.SECONDS)
                .build()

            val requestBody = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart(
                    "file",
                    audioFile.name,
                    audioFile.asRequestBody("audio/wav".toMediaType())
                )
                .build()

            val request = Request.Builder()
                .url("$serverUrl/stt")
                .post(requestBody)
                .build()

            val response = client.newCall(request).execute()
            Log.d(TAG, "Realtime STT response: ${response.code}")

            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                val json = JSONObject(body)
                val text = json.optString("text", "").trim()

                if (text.isNotEmpty()) {
                    Log.d(TAG, "User transcription: $text")
                    withContext(Dispatchers.Main) {
                        addTranscriptEntry("User", text)
                    }
                } else {
                    Log.d(TAG, "Empty transcription result")
                }
            } else {
                Log.e(TAG, "STT failed: ${response.code}")
            }

            // Cleanup
            audioFile.delete()

        } catch (e: Exception) {
            Log.e(TAG, "Error in realtime transcription", e)
        }
    }

    @OptIn(DelicateCoroutinesApi::class)
    fun startRealtimeAudioStreaming() {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            host.showToast("Microphone permission required")
            return
        }

        // End any previous capture loop (so its AudioRecord cannot leak) by SIGNAL only: the loop
        // stops and releases its own AudioRecord in its own finally, on the thread that reads it,
        // within one read. Stopping/releasing it from here, on main, raced that loop's in-flight
        // read() — the native abort in AudioRecord::releaseBuffer (feedback_android_audiorecord_race).
        realtimeCaptureLive?.set(false)
        realtimeCaptureLive = null

        // Sample rate depends on model
        val sampleRate = when (currentRealtimeModel) {
            RealtimeModel.GEMINI_LIVE -> 16000
            RealtimeModel.GPT_REALTIME -> 24000
            else -> 16000
        }

        val bufferSize = AudioRecord.getMinBufferSize(
            sampleRate,
            CHANNEL_CONFIG,
            AUDIO_FORMAT
        ) * 2

        // Held HERE until the capture loop takes it, and released here (in the finally below) only
        // if it never got that far — never read, so no read() can be in flight.
        var notYetLooping: AudioRecord? = null
        try {
            val record = AudioRecord(
                android.media.MediaRecorder.AudioSource.VOICE_RECOGNITION,
                sampleRate,
                CHANNEL_CONFIG,
                AUDIO_FORMAT,
                bufferSize
            )
            notYetLooping = record

            if (record.state != AudioRecord.STATE_INITIALIZED) {
                host.showToast("Failed to initialize audio")
                return
            }

            record.startRecording()
            isRealtimeRecording = true
            host.updateMicUi(true)
            host.updateBubbleGlow()  // Update bubble glow state (red for listening)

            // Clear the audio buffer for new recording session
            synchronized(realtimeAudioBufferLock) {
                realtimeAudioBuffer.clear()
            }

            val live = AtomicBoolean(true)
            realtimeCaptureLive = live
            // ATOMIC: onDestroy cancels the service scope, and a launch cancelled before it is
            // dispatched never runs its body — whose finally is the only release of [record].
            scope.launch(start = CoroutineStart.ATOMIC) {
                val buffer = ShortArray(bufferSize / 2)
                try {
                    while (isActive && live.get()) {
                        // The LOCAL record, never a field a re-entry could swap or null mid-read.
                        val readCount = record.read(buffer, 0, buffer.size)
                        // A stop that landed while this read was in flight ends the session HERE: its
                        // audio_commit is already sent, so this chunk must not follow it.
                        if (!live.get()) break
                        if (readCount > 0) {
                            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after (prevents feedback)
                            val timeSinceAIStopped = System.currentTimeMillis() - aiStoppedSpeakingAt
                            val inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS
                            if (isAISpeaking || inPostSpeechDelay) {
                                // Don't send mic audio while AI is speaking or in post-speech delay
                                continue
                            }

                            // Convert shorts to bytes (PCM16)
                            val bytes = ByteArray(readCount * 2)
                            for (i in 0 until readCount) {
                                bytes[i * 2] = (buffer[i].toInt() and 0xFF).toByte()
                                bytes[i * 2 + 1] = (buffer[i].toInt() shr 8 and 0xFF).toByte()
                            }

                            // Buffer audio for Whisper transcription (with size limit)
                            synchronized(realtimeAudioBufferLock) {
                                if (realtimeAudioBuffer.size < MAX_AUDIO_BUFFER_SIZE) {
                                    realtimeAudioBuffer.add(bytes.copyOf())
                                } else if (realtimeAudioBuffer.size == MAX_AUDIO_BUFFER_SIZE) {
                                    // Remove oldest chunk to make room (sliding window)
                                    realtimeAudioBuffer.removeAt(0)
                                    realtimeAudioBuffer.add(bytes.copyOf())
                                }
                            }

                            val base64Audio = Base64.encodeToString(bytes, Base64.NO_WRAP)

                            // Send audio chunk to realtime API
                            val audioMsg = JSONObject().apply {
                                put("type", "audio_input")
                                put("data", base64Audio)
                            }
                            realtimeWebSocket?.send(audioMsg.toString())
                        }
                    }
                } finally {
                    // Stopped and released HERE, on the SAME thread as read() — see the note at
                    // the top of this function.
                    try { record.stop() } catch (_: Exception) {}
                    try { record.release() } catch (_: Exception) {}
                }
            }
            notYetLooping = null

            Log.d(TAG, "Realtime audio streaming started at ${sampleRate}Hz")

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start audio streaming", e)
            host.showToast("Failed to start microphone")
        } finally {
            notYetLooping?.let { r -> try { r.release() } catch (_: Exception) {} }
        }
    }

    fun stopRealtimeAudioStreaming() {
        isRealtimeRecording = false
        // Only SIGNAL the capture loop: it stops and releases the AudioRecord in its own finally, on
        // the thread that reads it, within one read. Doing it here, on main, raced that loop's
        // in-flight read() — the native abort in AudioRecord::releaseBuffer
        // (feedback_android_audiorecord_race).
        realtimeCaptureLive?.set(false)
        realtimeCaptureLive = null

        host.updateMicUi(false)
        host.updateBubbleGlow()  // Update bubble glow state

        // Send audio commit to trigger response
        if (isRealtimeConnected) {
            val commitMsg = JSONObject().put("type", "audio_commit")
            realtimeWebSocket?.send(commitMsg.toString())
        }

        Log.d(TAG, "Realtime audio streaming stopped")
    }

    // Transcript management
    fun addTranscriptEntry(speaker: String, text: String) {
        if (text.isBlank()) return
        transcriptEntries.add(TranscriptEntry(speaker, text))
        host.showTranscriptEntry(speaker, text, transcriptEntries.size)
    }

    private fun finalizeLiveResponse() {
        val response = currentAIResponse.toString()
        if (response.isNotBlank()) {
            addTranscriptEntry("AI", response)
        }
        currentAIResponse.clear()
        host.updateLiveResponse("")
    }

    private companion object {
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT

        // Write WAV file with specific sample rate (for realtime audio)
        fun writeWavFileWithSampleRate(file: File, audioData: ByteArray, sampleRate: Int) {
            val channels = 1
            val bitsPerSample = 16
            val byteRate = sampleRate * channels * bitsPerSample / 8
            val blockAlign = channels * bitsPerSample / 8

            FileOutputStream(file).use { fos ->
                // RIFF header
                fos.write("RIFF".toByteArray())
                fos.write(intToByteArray(36 + audioData.size))
                fos.write("WAVE".toByteArray())

                // fmt chunk
                fos.write("fmt ".toByteArray())
                fos.write(intToByteArray(16))  // Chunk size
                fos.write(shortToByteArray(1))  // Audio format (PCM)
                fos.write(shortToByteArray(channels.toShort()))
                fos.write(intToByteArray(sampleRate))
                fos.write(intToByteArray(byteRate))
                fos.write(shortToByteArray(blockAlign.toShort()))
                fos.write(shortToByteArray(bitsPerSample.toShort()))

                // data chunk
                fos.write("data".toByteArray())
                fos.write(intToByteArray(audioData.size))
                fos.write(audioData)
            }
        }

        // Little-endian byte helpers for writeWavFileWithSampleRate's WAV header.
        fun intToByteArray(value: Int): ByteArray {
            return byteArrayOf(
                (value and 0xff).toByte(),
                ((value shr 8) and 0xff).toByte(),
                ((value shr 16) and 0xff).toByte(),
                ((value shr 24) and 0xff).toByte()
            )
        }

        fun shortToByteArray(value: Short): ByteArray {
            return byteArrayOf(
                (value.toInt() and 0xff).toByte(),
                ((value.toInt() shr 8) and 0xff).toByte()
            )
        }
    }
}
