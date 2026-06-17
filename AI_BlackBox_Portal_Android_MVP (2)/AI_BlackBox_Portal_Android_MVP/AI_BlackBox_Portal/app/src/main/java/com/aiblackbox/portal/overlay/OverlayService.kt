package com.aiblackbox.portal.overlay

import android.animation.AnimatorSet
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.Manifest
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.ImageReader
import android.media.MediaPlayer
import android.media.MediaMetadataRetriever
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Base64
import android.util.DisplayMetrics
import android.util.Log
import android.view.Gravity
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.ProgressBar
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.aiblackbox.portal.PairingActivity
import com.aiblackbox.portal.R
import kotlinx.coroutines.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import android.provider.MediaStore
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.TimeUnit

class OverlayService : Service() {

    companion object {
        private const val TAG = "OverlayService"
        private const val CHANNEL_ID = "blackbox_overlay"
        private const val NOTIFICATION_ID = 1001

        // Intent extras
        const val EXTRA_RESULT_CODE = "result_code"
        const val EXTRA_RESULT_DATA = "result_data"
        const val EXTRA_SERVER_URL = "server_url"
        const val EXTRA_OPERATOR = "operator"
        const val EXTRA_RECORDING_MODE = "recording_mode"  // Android 14+ fix

        // Actions
        const val ACTION_STOP = "com.aiblackbox.portal.STOP_OVERLAY"
        const val ACTION_REFRESH_PROJECTION = "com.aiblackbox.portal.REFRESH_MEDIA_PROJECTION"

        // Audio recording constants
        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT

        private var instance: OverlayService? = null

        fun isRunning(): Boolean = instance != null

        /**
         * Capture ONE screen frame as PNG bytes via the RUNNING overlay service's
         * existing MediaProjection (Task W4.2 — on-device vision). Delegates to
         * [captureFramePngBytes] on the live [instance]; calls [callback] with null
         * when the overlay service isn't running (no projection available — the
         * caller falls back with a clear message). The [ScreenCapture] seam
         * ([OverlayScreenCapture]) wraps this into a suspend API; this static hop
         * exists because the projection + ImageReader live on the Service, not the VM.
         */
        fun captureScreenPng(callback: (ByteArray?) -> Unit) {
            val svc = instance
            if (svc == null) {
                callback(null)
            } else {
                svc.captureFramePngBytes(callback)
            }
        }
    }

    // ---- XR detection ----
    private val isXrDevice: Boolean by lazy {
        packageManager.hasSystemFeature("android.software.xr.api.spatial")
    }

    private lateinit var windowManager: WindowManager
    private lateinit var overlayView: View
    private lateinit var expandedPanel: View
    private var bubbleGlow: View? = null
    private var bubbleGlowAnimator: AnimatorSet? = null
    private var mediaProjection: MediaProjection? = null

    // Persistent screenshot infrastructure (created once, NEVER released until service stops)
    // This keeps MediaProjection alive on Android 14+
    private var persistentScreenshotDisplay: VirtualDisplay? = null
    private var persistentImageReader: ImageReader? = null

    // Recording infrastructure (separate from screenshots, created/released per session)
    private var recordingDisplay: VirtualDisplay? = null
    private var mediaRecorder: MediaRecorder? = null

    // Legacy variables - kept for compatibility but transitioning away
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null

    private var isRecording = false
    private var isExpanded = false

    // Track current VirtualDisplay mode to avoid conflicts
    private enum class DisplayMode { NONE, SCREENSHOT, RECORDING }
    private var currentDisplayMode = DisplayMode.NONE

    // Track when recording stopped to add delay before screenshot
    private var lastRecordingStopTime: Long = 0L

    // Track if MediaProjection has been invalidated (Android 14+ releases token after recording)
    private var mediaProjectionInvalidated = false

    private var serverUrl: String = ""
    private var currentOperator: String = "Brandon"
    private var operatorsList: List<String> = listOf("Brandon", "default")

    private val handler = Handler(Looper.getMainLooper())
    private val coroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private var screenWidth = 0
    private var screenHeight = 0
    private var screenDensity = 0

    // TTS via BlackBox
    private var autoTtsEnabled = true
    private var mediaPlayer: MediaPlayer? = null
    private var currentTtsFile: File? = null
    private var isUserSeeking = false

    // TTS Player UI elements
    private var ttsPlayerBar: LinearLayout? = null
    private var btnTtsPlayPause: ImageButton? = null
    private var ttsSeekBar: SeekBar? = null
    private var ttsCurrentTime: TextView? = null
    private var ttsDuration: TextView? = null
    private var btnTtsStop: ImageButton? = null

    // Seek bar update runnable
    private val seekBarUpdateRunnable = object : Runnable {
        override fun run() {
            mediaPlayer?.let { mp ->
                if (mp.isPlaying && !isUserSeeking) {
                    val currentPos = mp.currentPosition
                    val duration = mp.duration
                    if (duration > 0) {
                        ttsSeekBar?.progress = (currentPos * 100 / duration)
                        ttsCurrentTime?.text = formatTime(currentPos)
                    }
                }
            }
            handler.postDelayed(this, 200)
        }
    }

    // STT via BlackBox (Whisper)
    private var audioRecord: AudioRecord? = null
    private var isListening = false
    private var recordingJob: Job? = null

    // Chat state
    private val attachedScreenshots = mutableListOf<File>()
    private var attachedRecording: File? = null

    // Recording stop button UI
    private var recordingStopButton: View? = null
    private var recordingTimer: TextView? = null
    private var recordingIndicator: View? = null
    private var recordingStartTime: Long = 0L
    private val recordingTimerRunnable = object : Runnable {
        override fun run() {
            if (isRecording) {
                val elapsed = System.currentTimeMillis() - recordingStartTime
                recordingTimer?.text = formatRecordingTime(elapsed)
                // Pulse the recording indicator
                recordingIndicator?.animate()?.alpha(if (recordingIndicator?.alpha == 1.0f) 0.3f else 1.0f)?.setDuration(500)?.start()
                handler.postDelayed(this, 1000)
            }
        }
    }

    // UI elements
    private var promptInput: EditText? = null
    private var responseText: TextView? = null
    private var statusText: TextView? = null
    private var statusIndicator: View? = null
    private var progressBar: ProgressBar? = null
    private var attachmentCount: TextView? = null
    private var ttsLabel: TextView? = null
    private var operatorSpinner: Spinner? = null
    private var attachmentPreviewScroll: HorizontalScrollView? = null
    private var attachmentPreviewContainer: LinearLayout? = null

    // =========================================================================
    // REALTIME MODE - GPT Realtime & Gemini Live Integration
    // =========================================================================

    // Realtime model selection
    enum class RealtimeModel { NONE, GPT_REALTIME, GEMINI_LIVE, GROK_LIVE }
    private var currentRealtimeModel: RealtimeModel = RealtimeModel.GEMINI_LIVE

    // Realtime connection state
    private var realtimeWebSocket: WebSocket? = null
    private var realtimeSessionId: String = ""
    private var isRealtimeConnected = false
    private var isRealtimeRecording = false  // Mic active for realtime streaming
    private var isScreenSharing = false

    // Screen share for realtime models (1 FPS frame capture) - supports both Gemini Live and GPT Realtime
    private var screenShareJob: Job? = null
    private val SCREEN_SHARE_INTERVAL_MS = 1000L  // 1 FPS

    // Realtime audio streaming (separate from STT recording)
    private var realtimeAudioRecord: AudioRecord? = null
    private var realtimeAudioJob: Job? = null

    // Audio playback for AI responses
    private var audioTrack: android.media.AudioTrack? = null
    private val audioTrackLock = Object()  // Lock for thread-safe AudioTrack access
    private var isAISpeaking = false
    private var isUserMuted = false  // User manually muted via button press
    private var wasMicActiveBeforeAI = false  // Track mic state before AI started speaking

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
    private val geminiVoices = arrayOf("Charon", "Puck", "Kore", "Aoede", "Fenrir", "Orus")
    private val gptVoices = arrayOf("ash", "alloy", "ballad", "coral", "echo", "sage", "shimmer", "verse")
    private val grokVoices = arrayOf("Ara", "Rex", "Sal", "Eve", "Leo")

    // Transcript management
    data class TranscriptEntry(val speaker: String, val text: String, val timestamp: Long = System.currentTimeMillis())
    private val transcriptEntries = mutableListOf<TranscriptEntry>()
    private var currentAIResponse = StringBuilder()
    private var isTranscriptExpanded = false

    // Realtime UI elements
    private var modelSpinner: Spinner? = null
    private var voiceSpinner: Spinner? = null
    private var selectedVoice: String = "Charon"  // Default voice
    private var btnScreenShare: ImageButton? = null
    private var btnScreenShareContainer: View? = null
    private var btnCamera: ImageButton? = null
    private var btnCameraContainer: View? = null
    private var btnConnect: ImageButton? = null
    private var btnMinimize: ImageButton? = null
    private var liveResponseText: TextView? = null
    private var transcriptSection: LinearLayout? = null
    private var transcriptHeader: View? = null
    private var transcriptScroll: ScrollView? = null
    private var transcriptContainer: LinearLayout? = null
    private var transcriptCount: TextView? = null
    private var transcriptExpandIcon: ImageView? = null
    private var screenShareLabel: TextView? = null
    private var micLabel: TextView? = null
    private var connectLabel: TextView? = null

    // OkHttp client for WebSocket (reuse existing if possible)
    private val realtimeHttpClient by lazy {
        OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)  // No timeout for WebSocket
            .connectTimeout(30, TimeUnit.SECONDS)
            .build()
    }

    // =========================================================================
    // END REALTIME MODE VARIABLES
    // =========================================================================

    // MediaProjection data for deferred initialization
    private var pendingResultCode: Int = Activity.RESULT_CANCELED
    private var pendingResultData: Intent? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager

        // Get screen metrics
        val metrics = DisplayMetrics()
        @Suppress("DEPRECATION")
        windowManager.defaultDisplay.getMetrics(metrics)
        screenWidth = metrics.widthPixels
        screenHeight = metrics.heightPixels
        screenDensity = metrics.densityDpi

        createNotificationChannel()

        // Register XR bridge command handler if running on XR device
        if (isXrDevice) {
            OverlayBridge.registerCommandListener(XrCommandHandler())
        }

        Log.d(TAG, "OverlayService created (isXrDevice=$isXrDevice)")
    }

    /**
     * Inner class that maps OverlayBridge commands from the XR Activity
     * to existing service methods. Only instantiated on XR devices.
     */
    private inner class XrCommandHandler : OverlayBridge.CommandListener {
        override fun toggleConnect() {
            handler.post {
                if (isRealtimeConnected) disconnectRealtime() else connectRealtime()
            }
        }
        override fun toggleMic() {
            handler.post { toggleRealtimeAudio() }
        }
        override fun toggleScreenShare() {
            handler.post { toggleScreenSharing() }
        }
        override fun toggleExpanded() {
            handler.post { this@OverlayService.toggleExpanded() }
        }
        override fun selectModel(model: String) {
            handler.post {
                currentRealtimeModel = when (model) {
                    "GPT Realtime" -> RealtimeModel.GPT_REALTIME
                    "Grok Live" -> RealtimeModel.GROK_LIVE
                    else -> RealtimeModel.GEMINI_LIVE
                }
                syncBridgeState()
            }
        }
        override fun selectVoice(voice: String) {
            handler.post {
                selectedVoice = voice
                syncBridgeState()
            }
        }
        override fun selectOperator(operator: String) {
            handler.post {
                currentOperator = operator
                syncBridgeState()
            }
        }
        override fun openCamera() {
            handler.post { takeScreenshot() }
        }
        override fun openPortal() {
            handler.post {
                val intent = android.content.Intent(this@OverlayService, com.aiblackbox.portal.NativeMainActivity::class.java).apply {
                    addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
                }
                startActivity(intent)
            }
        }
        override fun minimize() {
            handler.post {
                isExpanded = false
                syncBridgeState()
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "onStartCommand: action=${intent?.action}, intent=$intent")

        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
        }

        // CRITICAL: On Android 14+, if service is restarted automatically (null intent),
        // we cannot use MEDIA_PROJECTION service type. Stop the service to prevent crash.
        if (intent == null && Build.VERSION.SDK_INT >= 34) {
            Log.w(TAG, "Service restarted with null intent on Android 14+ - stopping to prevent SecurityException")
            stopSelf()
            return START_NOT_STICKY
        }

        // Get MediaProjection data FIRST - handle API 33+ properly
        // We need this before starting foreground on Android 14+ to determine service type
        val intentResultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED) ?: Activity.RESULT_CANCELED
        val intentResultData = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent?.getParcelableExtra(EXTRA_RESULT_DATA, Intent::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent?.getParcelableExtra<Intent>(EXTRA_RESULT_DATA)
        }

        // Check if we have valid MediaProjection data
        val hasMediaProjection = (intentResultCode == Activity.RESULT_OK && intentResultData != null)
        Log.d(TAG, "onStartCommand: hasMediaProjection=$hasMediaProjection, resultCode=$intentResultCode")

        // Start foreground with appropriate service type (required for Android 10+)
        // On Android 14+ (API 34+), we can ONLY use MEDIA_PROJECTION type if we have valid projection data
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val serviceType = if (hasMediaProjection) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            } else {
                // No projection data - just use microphone type (realtime voice still works)
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
            try {
                startForeground(NOTIFICATION_ID, createNotification(), serviceType)
                Log.d(TAG, "Started foreground service with type: $serviceType, hasMediaProjection=$hasMediaProjection")
            } catch (e: SecurityException) {
                Log.e(TAG, "SecurityException starting foreground - falling back to microphone only", e)
                // Fallback: try with just microphone type
                try {
                    startForeground(NOTIFICATION_ID, createNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
                    Log.d(TAG, "Fallback: Started foreground with MICROPHONE type only")
                } catch (e2: Exception) {
                    Log.e(TAG, "Failed to start foreground service even with fallback", e2)
                    stopSelf()
                    return START_NOT_STICKY
                }
            }
        } else {
            startForeground(NOTIFICATION_ID, createNotification())
        }

        // Store projection data
        pendingResultCode = intentResultCode
        pendingResultData = intentResultData

        // Get other extras
        serverUrl = intent?.getStringExtra(EXTRA_SERVER_URL) ?: ""
        currentOperator = intent?.getStringExtra(EXTRA_OPERATOR) ?: "default"
        val requestedRecordingMode = intent?.getBooleanExtra(EXTRA_RECORDING_MODE, false) ?: false

        // Initialize MediaProjection after foreground started
        if (pendingResultCode == Activity.RESULT_OK && pendingResultData != null) {
            try {
                val projectionManager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
                mediaProjection = projectionManager.getMediaProjection(pendingResultCode, pendingResultData!!)

                mediaProjection?.registerCallback(object : MediaProjection.Callback() {
                    override fun onStop() {
                        Log.w(TAG, "MediaProjection.Callback onStop() triggered.")
                        // This can be triggered by the SecurityException or if the user revokes permission.
                        // Don't cause a refresh loop. Just mark as invalid. The user will be prompted
                        // to refresh on their next action (e.g., taking a screenshot).
                        mediaProjectionInvalidated = true
                        handler.post {
                            persistentScreenshotDisplay?.release()
                            persistentScreenshotDisplay = null
                            showToast("Screen capture session has ended.")
                        }
                    }
                }, handler)


                Log.d(TAG, "MediaProjection obtained successfully on Android API ${Build.VERSION.SDK_INT}")

                // Android 14+ FIX: Don't create ANY display on startup
                // Use numeric check (API 34+) instead of constant to ensure compatibility
                val isAndroid14Plus = Build.VERSION.SDK_INT >= 34
                Log.d(TAG, "Is Android 14+ (API 34+)? $isAndroid14Plus")

                if (isAndroid14Plus) {
                    Log.d(TAG, "Android 14+: MediaProjection ready. Display will be created on first screenshot or record.")
                    Log.d(TAG, "Skipping persistent display creation to preserve MediaProjection token for user's choice")
                } else {
                    // Older Android: Can have multiple displays, so create persistent display immediately
                    handler.postDelayed({
                        Log.d(TAG, "Starting persistent screenshot display setup (screen: ${screenWidth}x${screenHeight}@${screenDensity})")
                        setupPersistentScreenshotDisplay { success ->
                            if (success) {
                                Log.d(TAG, "STARTUP: Persistent screenshot display created successfully - MediaProjection anchored!")
                            } else {
                                Log.e(TAG, "STARTUP: Persistent screenshot display setup FAILED - may need permission refresh on first use")
                            }
                        }
                    }, 1000)  // 1 second delay to ensure screen dimensions and MediaProjection are ready
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to get MediaProjection", e)
                showToast("Screen capture unavailable: ${e.message}")
            }
        } else {
            Log.w(TAG, "No MediaProjection data - screenshot/recording will be unavailable")
        }

        // Create overlay UI
        createOverlayUI()

        // Fetch operators list from server
        fetchOperators()

        // Use START_NOT_STICKY to prevent automatic restarts without MediaProjection data
        // If the service is killed, user must re-launch from the app to get fresh projection
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        super.onDestroy()
        instance = null

        // Cleanup XR bridge — signal activity to finish, then reset
        if (isXrDevice) {
            OverlayBridge.updateState { it.copy(shouldFinish = true) }
            OverlayBridge.unregisterCommandListener()
            // Delay reset slightly so the Activity can observe shouldFinish
            handler.postDelayed({ OverlayBridge.reset() }, 500)
        }

        // Cleanup bubble glow animation
        bubbleGlowAnimator?.cancel()
        bubbleGlowAnimator = null

        // Cleanup views (phone path only — XR path never creates these views)
        if (!isXrDevice) {
            try {
                if (::overlayView.isInitialized) {
                    windowManager.removeView(overlayView)
                }
                if (::expandedPanel.isInitialized) {
                    windowManager.removeView(expandedPanel)
                }
                // Cleanup recording stop button
                removeRecordingStopButton()
            } catch (e: Exception) {
                Log.e(TAG, "Error removing views", e)
            }
        }

        // Cleanup resources
        stopRecording()
        stopAudioRecording()

        // Cleanup realtime mode resources
        stopRealtimeAudioStreaming()
        stopScreenSharing()
        releaseAudioTrack()
        // NOTE: Session transcript is saved by the BACKEND when WebSocket closes
        // (see gemini_live_routes.py save_session_to_blackbox)
        // Removed duplicate save here to prevent two snapshots from same session
        // if (isRealtimeConnected) {
        //     saveSessionToBlackBox()
        // }
        realtimeWebSocket?.close(1000, "Service destroyed")
        realtimeWebSocket = null
        isRealtimeConnected = false

        // Cleanup persistent screenshot display (only now, at service destruction)
        persistentScreenshotDisplay?.release()
        persistentScreenshotDisplay = null
        persistentImageReader?.close()
        persistentImageReader = null

        mediaProjection?.stop()
        mediaProjection = null

        // Cleanup TTS player
        handler.removeCallbacks(seekBarUpdateRunnable)
        mediaPlayer?.release()
        mediaPlayer = null
        currentTtsFile?.delete()
        currentTtsFile = null

        coroutineScope.cancel()

        // Clear attachments
        attachedScreenshots.forEach { it.delete() }
        attachedScreenshots.clear()
        attachedRecording?.delete()

        Log.d(TAG, "OverlayService destroyed")
    }

    /**
     * Creates a persistent VirtualDisplay for screenshots that stays alive for the service lifetime.
     * This is the key to keeping MediaProjection valid on Android 14+.
     * By never releasing this VirtualDisplay, subsequent createVirtualDisplay() calls (for recording) succeed.
     *
     * @param onComplete Optional callback invoked when setup completes (success or final failure).
     *                   Boolean parameter is true if setup succeeded.
     */
    private fun setupPersistentScreenshotDisplay(onComplete: ((Boolean) -> Unit)? = null) {
        setupPersistentScreenshotDisplayWithRetries(attempt = 1, maxAttempts = 3, onComplete = onComplete)
    }

    /**
     * Try to set up persistent display with retries.
     * On Android 14+, sometimes the MediaProjection isn't immediately ready to create VirtualDisplays.
     */
    private fun setupPersistentScreenshotDisplayWithRetries(attempt: Int, maxAttempts: Int, onComplete: ((Boolean) -> Unit)? = null) {
        if (mediaProjection == null) {
            Log.w(TAG, "Cannot setup persistent display - no MediaProjection")
            onComplete?.invoke(false)
            return
        }

        if (persistentScreenshotDisplay != null) {
            Log.d(TAG, "Persistent screenshot display already exists")
            onComplete?.invoke(true)
            return
        }

        Log.d(TAG, "Setting up persistent screenshot display ${screenWidth}x${screenHeight} (attempt $attempt/$maxAttempts)")

        try {
            // Clean up any previous failed attempts
            persistentImageReader?.close()
            persistentImageReader = null

            persistentImageReader = ImageReader.newInstance(
                screenWidth, screenHeight,
                PixelFormat.RGBA_8888, 5  // Buffer for multiple captures
            )

            persistentScreenshotDisplay = mediaProjection?.createVirtualDisplay(
                "PersistentScreenshot",
                screenWidth, screenHeight, screenDensity,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                persistentImageReader?.surface, null, handler
            )

            if (persistentScreenshotDisplay != null) {
                Log.d(TAG, "SUCCESS: Persistent screenshot display created on attempt $attempt - MediaProjection anchored!")
                mediaProjectionInvalidated = false  // Reset invalidation flag - we have a working display
                onComplete?.invoke(true)
            } else {
                Log.w(TAG, "Failed to create persistent screenshot display (returned null) - attempt $attempt")
                handlePersistentDisplaySetupFailure(attempt, maxAttempts, null, onComplete)
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "SecurityException creating persistent display on attempt $attempt - MediaProjection may not be ready yet", e)
            handlePersistentDisplaySetupFailure(attempt, maxAttempts, e, onComplete)
        } catch (e: Exception) {
            Log.e(TAG, "Error setting up persistent screenshot display on attempt $attempt", e)
            handlePersistentDisplaySetupFailure(attempt, maxAttempts, e, onComplete)
        }
    }

    /**
     * Handle failure of persistent display setup - retry with exponential backoff or give up
     */
    private fun handlePersistentDisplaySetupFailure(attempt: Int, maxAttempts: Int, error: Exception?, onComplete: ((Boolean) -> Unit)? = null) {
        if (attempt < maxAttempts) {
            // Retry with exponential backoff: 500ms, 1000ms, 2000ms
            val delayMs = 500L * (1 shl (attempt - 1))
            Log.d(TAG, "Will retry persistent display setup in ${delayMs}ms (attempt ${attempt + 1}/$maxAttempts)")
            handler.postDelayed({
                setupPersistentScreenshotDisplayWithRetries(attempt + 1, maxAttempts, onComplete)
            }, delayMs)
        } else {
            // All retries exhausted
            Log.e(TAG, "FAILED: Could not create persistent display after $maxAttempts attempts")
            // Only mark as invalidated if we got a SecurityException (actual permission issue)
            if (error is SecurityException && Build.VERSION.SDK_INT >= 34) {
                Log.e(TAG, "SecurityException on final attempt - MediaProjection token may be invalid")
                mediaProjectionInvalidated = true
            } else {
                // Other errors (null return, etc.) - don't mark as invalidated yet
                // The MediaProjection might still be valid, just had trouble creating the display
                Log.w(TAG, "Persistent display setup failed but MediaProjection may still be valid - will try on-demand")
            }
            onComplete?.invoke(false)
        }
    }

    private fun fetchOperators() {
        if (serverUrl.isEmpty()) return

        coroutineScope.launch {
            try {
                val client = OkHttpClient.Builder()
                    .connectTimeout(10, TimeUnit.SECONDS)
                    .readTimeout(10, TimeUnit.SECONDS)
                    .build()

                val request = Request.Builder()
                    .url("$serverUrl/operators")
                    .get()
                    .build()

                val response = client.newCall(request).execute()

                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "{}"
                    val json = JSONObject(body)
                    val operators = json.optJSONArray("operators")
                    val defaultOp = json.optString("default", "default")

                    if (operators != null && operators.length() > 0) {
                        val list = mutableListOf<String>()
                        for (i in 0 until operators.length()) {
                            list.add(operators.getString(i))
                        }
                        operatorsList = list

                        withContext(Dispatchers.Main) {
                            setupOperatorSpinner(defaultOp)
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error fetching operators", e)
            }
        }
    }

    private fun setupOperatorSpinner(defaultOperator: String) {
        operatorSpinner?.let { spinner ->
            // Use custom layouts with white text for dark theme
            val adapter = ArrayAdapter(
                this,
                R.layout.spinner_item,
                operatorsList
            ).apply {
                setDropDownViewResource(R.layout.spinner_dropdown_item)
            }

            spinner.adapter = adapter

            // Set current selection
            val index = operatorsList.indexOf(currentOperator)
            if (index >= 0) {
                spinner.setSelection(index)
            } else {
                val defaultIndex = operatorsList.indexOf(defaultOperator)
                if (defaultIndex >= 0) {
                    spinner.setSelection(defaultIndex)
                }
            }

            spinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
                override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                    currentOperator = operatorsList[position]
                    Log.d(TAG, "Operator changed to: $currentOperator")
                }

                override fun onNothingSelected(parent: AdapterView<*>?) {}
            }
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "BlackBox Overlay",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Shows when BlackBox overlay is active"
                setShowBadge(false)
            }

            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    private fun createNotification(): Notification {
        val stopIntent = Intent(this, OverlayService::class.java).apply {
            action = ACTION_STOP
        }
        val stopPendingIntent = PendingIntent.getService(
            this, 0, stopIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val openIntent = Intent(this, PairingActivity::class.java)
        val openPendingIntent = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("BlackBox Overlay Active")
            .setContentText("Tap to open, or use floating bubble")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentIntent(openPendingIntent)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stopPendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun createOverlayUI() {
        if (isXrDevice) {
            // Initialize lateinit vars to empty views to prevent UninitializedPropertyAccessException
            // if any cleanup/callback code touches them during the service lifecycle.
            overlayView = View(this)
            expandedPanel = View(this)
            launchXrOverlayActivity()
            return
        }
        // Phone path — UNCHANGED
        // Create floating bubble (collapsed state)
        createFloatingBubble()

        // Create expanded panel (hidden initially)
        createExpandedPanel()
    }

    /** XR path: launch the transparent Activity that hosts SpatialPanels. */
    private fun launchXrOverlayActivity() {
        Log.d(TAG, "launchXrOverlayActivity — about to startActivity")
        val intent = android.content.Intent(this, XrOverlayActivity::class.java).apply {
            addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        startActivity(intent)
        Log.d(TAG, "Launched XrOverlayActivity — syncing initial bridge state")
        // Sync initial state so the Activity sees current values on first composition
        syncBridgeState()
    }

    /**
     * Copy all relevant service state into OverlayBridge so the XR Activity can observe it.
     * Only called when isXrDevice == true. Called from every state-changing method.
     */
    private fun syncBridgeState() {
        if (!isXrDevice) return
        OverlayBridge.updateState { current ->
            current.copy(
                isConnected = isRealtimeConnected,
                isRecording = isRealtimeRecording,
                isAISpeaking = isAISpeaking,
                isScreenSharing = isScreenSharing,
                isExpanded = isExpanded,
                isUserMuted = isUserMuted,
                // Note: statusText TextView is null on XR (panel never inflated),
                // so we preserve whatever was set via updateRealtimeStatus()
                statusText = current.statusText,
                currentModel = when (currentRealtimeModel) {
                    RealtimeModel.GPT_REALTIME -> "GPT Realtime"
                    RealtimeModel.GROK_LIVE -> "Grok Live"
                    else -> "Gemini Live"
                },
                currentVoice = selectedVoice,
                currentOperator = currentOperator,
                voices = when (currentRealtimeModel) {
                    RealtimeModel.GPT_REALTIME -> gptVoices.toList()
                    RealtimeModel.GROK_LIVE -> grokVoices.toList()
                    else -> geminiVoices.toList()
                },
                operators = operatorsList,
                transcriptEntries = transcriptEntries.map {
                    OverlayBridge.TranscriptEntry(it.speaker, it.text, it.timestamp)
                },
                liveResponseText = currentAIResponse.toString()
            )
        }
    }

    private fun createFloatingBubble() {
        overlayView = LayoutInflater.from(this).inflate(R.layout.overlay_bubble, null)

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 0
            y = 200
        }

        // Make bubble draggable
        setupDragListener(overlayView, params)

        // Get reference to bubble glow view
        bubbleGlow = overlayView.findViewById(R.id.bubbleGlow)

        // Click to expand
        overlayView.setOnClickListener {
            toggleExpanded()
        }

        try {
            windowManager.addView(overlayView, params)
            Log.d(TAG, "Floating bubble created")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add bubble view", e)
            showToast("Failed to create overlay: ${e.message}")
        }
    }

    private fun createExpandedPanel() {
        expandedPanel = LayoutInflater.from(this).inflate(R.layout.overlay_expanded, null)

        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE,
            // Allow focus for keyboard input
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            x = 0
            y = 100
            // Set soft input mode
            softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
        }

        // Get UI references (legacy - kept for compatibility)
        promptInput = expandedPanel.findViewById(R.id.promptInput)
        responseText = expandedPanel.findViewById(R.id.responseText)
        statusText = expandedPanel.findViewById(R.id.statusText)
        statusIndicator = expandedPanel.findViewById(R.id.statusIndicator)
        progressBar = expandedPanel.findViewById(R.id.progressBar)
        attachmentCount = expandedPanel.findViewById(R.id.attachmentCount)
        ttsLabel = expandedPanel.findViewById(R.id.ttsLabel)
        operatorSpinner = expandedPanel.findViewById(R.id.operatorSpinner)
        attachmentPreviewScroll = expandedPanel.findViewById(R.id.attachmentPreviewScroll)
        attachmentPreviewContainer = expandedPanel.findViewById(R.id.attachmentPreviewContainer)

        // TTS Player UI (legacy)
        ttsPlayerBar = expandedPanel.findViewById(R.id.ttsPlayerBar)
        btnTtsPlayPause = expandedPanel.findViewById(R.id.btnTtsPlayPause)
        ttsSeekBar = expandedPanel.findViewById(R.id.ttsSeekBar)
        ttsCurrentTime = expandedPanel.findViewById(R.id.ttsCurrentTime)
        ttsDuration = expandedPanel.findViewById(R.id.ttsDuration)
        btnTtsStop = expandedPanel.findViewById(R.id.btnTtsStop)

        // Realtime mode UI elements
        modelSpinner = expandedPanel.findViewById(R.id.modelSpinner)
        voiceSpinner = expandedPanel.findViewById(R.id.voiceSpinner)
        btnScreenShare = expandedPanel.findViewById(R.id.btnScreenShare)
        btnScreenShareContainer = expandedPanel.findViewById(R.id.btnScreenShareContainer)
        btnCamera = expandedPanel.findViewById(R.id.btnCamera)
        btnCameraContainer = expandedPanel.findViewById(R.id.btnCameraContainer)
        btnConnect = expandedPanel.findViewById(R.id.btnConnect)
        btnMinimize = expandedPanel.findViewById(R.id.btnMinimize)
        liveResponseText = expandedPanel.findViewById(R.id.liveResponseText)
        transcriptSection = expandedPanel.findViewById(R.id.transcriptSection)
        transcriptHeader = expandedPanel.findViewById(R.id.transcriptHeader)
        transcriptScroll = expandedPanel.findViewById(R.id.transcriptScroll)
        transcriptContainer = expandedPanel.findViewById(R.id.transcriptContainer)
        transcriptCount = expandedPanel.findViewById(R.id.transcriptCount)
        transcriptExpandIcon = expandedPanel.findViewById(R.id.transcriptExpandIcon)
        screenShareLabel = expandedPanel.findViewById(R.id.screenShareLabel)
        micLabel = expandedPanel.findViewById(R.id.micLabel)
        connectLabel = expandedPanel.findViewById(R.id.connectLabel)

        // Setup model spinner
        setupModelSpinner()

        // Setup realtime mode controls
        setupRealtimeControls()

        // Setup TTS player controls (legacy)
        setupTtsPlayerControls()

        // Setup button listeners
        setupExpandedPanelButtons()

        // Make panel draggable by handle
        val dragHandle = expandedPanel.findViewById<View>(R.id.dragHandle)
        if (dragHandle != null) {
            setupDragListener(dragHandle, params, isHandle = true)
        }

        expandedPanel.visibility = View.GONE

        try {
            windowManager.addView(expandedPanel, params)
            Log.d(TAG, "Expanded panel created")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add expanded panel", e)
        }
    }

    private fun setupExpandedPanelButtons() {
        // Screenshot button container - click on entire button area
        expandedPanel.findViewById<View>(R.id.btnScreenshotContainer)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "Screenshot button clicked")
            showToast("Taking screenshot...")
            takeScreenshot()
        }

        // Record button container - toggles recording
        expandedPanel.findViewById<View>(R.id.btnRecordContainer)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "Record button clicked")
            toggleRecording()
        }

        // NOTE: Mic button listener is set in setupRealtimeControls() for realtime mode
        // Don't set it here to avoid overriding the realtime toggle behavior

        // Send button - send prompt to AI (header button, ImageButton)
        expandedPanel.findViewById<ImageButton>(R.id.btnSend)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "Send button clicked")
            sendPromptToAI()
        }

        // TTS toggle button (header button, ImageButton)
        expandedPanel.findViewById<ImageButton>(R.id.btnTts)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "TTS toggle clicked")
            toggleAutoTts()
        }

        // Close button (header button, ImageButton)
        expandedPanel.findViewById<ImageButton>(R.id.btnClose)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "Close button clicked")
            toggleExpanded()
        }

        // Open Portal button container
        expandedPanel.findViewById<View>(R.id.btnPortalContainer)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "Portal button clicked")
            openPortal()
        }

        // Handle keyboard show/hide for input
        promptInput?.setOnFocusChangeListener { _, hasFocus ->
            if (hasFocus) {
                updatePanelForKeyboard(true)
            }
        }
    }

    private fun animateButtonClick(view: View) {
        // Visual feedback: scale down then up
        view.animate()
            .scaleX(0.85f)
            .scaleY(0.85f)
            .setDuration(50)
            .withEndAction {
                view.animate()
                    .scaleX(1.0f)
                    .scaleY(1.0f)
                    .setDuration(100)
                    .start()
            }
            .start()
    }

    private fun updatePanelForKeyboard(showKeyboard: Boolean) {
        try {
            val params = expandedPanel.layoutParams as WindowManager.LayoutParams
            if (showKeyboard) {
                params.flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                               WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
            } else {
                params.flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                               WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
            }
            windowManager.updateViewLayout(expandedPanel, params)
        } catch (e: Exception) {
            Log.e(TAG, "Error updating panel for keyboard", e)
        }
    }

    private fun setupDragListener(view: View, params: WindowManager.LayoutParams, isHandle: Boolean = false) {
        var initialX = 0
        var initialY = 0
        var initialTouchX = 0f
        var initialTouchY = 0f
        var isDragging = false

        view.setOnTouchListener { v, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    initialX = params.x
                    initialY = params.y
                    initialTouchX = event.rawX
                    initialTouchY = event.rawY
                    isDragging = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val deltaX = (event.rawX - initialTouchX).toInt()
                    val deltaY = (event.rawY - initialTouchY).toInt()

                    if (Math.abs(deltaX) > 10 || Math.abs(deltaY) > 10) {
                        isDragging = true
                    }

                    params.x = initialX + deltaX
                    params.y = initialY + deltaY

                    try {
                        val targetView = if (isHandle) expandedPanel else view
                        if (targetView == overlayView) {
                            windowManager.updateViewLayout(overlayView, params)
                        } else {
                            windowManager.updateViewLayout(expandedPanel, params)
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "Error updating view layout", e)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (!isDragging) {
                        v.performClick()
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun toggleExpanded() {
        isExpanded = !isExpanded

        if (isXrDevice) {
            // XR path: just sync bridge, XrOverlayActivity handles UI
            syncBridgeState()
            return
        }

        // Phone path — UNCHANGED
        if (isExpanded) {
            overlayView.visibility = View.GONE
            expandedPanel.visibility = View.VISIBLE
            updateAttachmentCount()
        } else {
            overlayView.visibility = View.VISIBLE
            expandedPanel.visibility = View.GONE
            // Hide keyboard if showing
            promptInput?.let {
                val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
                imm.hideSoftInputFromWindow(it.windowToken, 0)
            }
        }
    }

    // ==================== Whisper STT via BlackBox ====================

    // Store recorded audio data to transcribe after manual stop
    private var pendingAudioData: ByteArray? = null

    private fun toggleWhisperRecording() {
        if (isListening) {
            // Second press - stop recording and transcribe
            stopAudioRecording(transcribe = true)
        } else {
            // First press - start recording
            startAudioRecording()
        }
    }

    private fun startAudioRecording() {
        // Check permission
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            showToast("Microphone permission required")
            Log.e(TAG, "RECORD_AUDIO permission not granted")
            return
        }

        try {
            val bufferSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
            Log.d(TAG, "AudioRecord minBufferSize: $bufferSize")

            if (bufferSize == AudioRecord.ERROR || bufferSize == AudioRecord.ERROR_BAD_VALUE) {
                Log.e(TAG, "Invalid buffer size: $bufferSize")
                showToast("Audio config not supported")
                return
            }

            // Release any existing AudioRecord
            audioRecord?.release()
            audioRecord = null

            // Use VOICE_RECOGNITION source - optimized for speech recognition
            // Also try DEFAULT if VOICE_RECOGNITION fails
            audioRecord = try {
                AudioRecord(
                    android.media.MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE,
                    CHANNEL_CONFIG,
                    AUDIO_FORMAT,
                    bufferSize * 4
                )
            } catch (e: Exception) {
                Log.w(TAG, "VOICE_RECOGNITION failed, trying MIC source", e)
                AudioRecord(
                    android.media.MediaRecorder.AudioSource.MIC,
                    SAMPLE_RATE,
                    CHANNEL_CONFIG,
                    AUDIO_FORMAT,
                    bufferSize * 4
                )
            }

            Log.d(TAG, "AudioRecord state: ${audioRecord?.state}, expected: ${AudioRecord.STATE_INITIALIZED}")

            if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord not initialized, state=${audioRecord?.state}")
                showToast("Failed to initialize audio recording")
                audioRecord?.release()
                audioRecord = null
                return
            }

            isListening = true
            pendingAudioData = null

            // Visual feedback - show recording state
            updateStatus("Recording... Tap mic to stop")
            showToast("Recording... Tap mic button to stop")

            // Highlight mic button (full opacity, tinted red)
            expandedPanel.findViewById<ImageButton>(R.id.btnMic)?.let { micBtn ->
                micBtn.alpha = 1.0f
                micBtn.setColorFilter(android.graphics.Color.RED)
            }

            audioRecord?.startRecording()

            // Verify recording actually started
            val recordingState = audioRecord?.recordingState
            Log.d(TAG, "AudioRecord recordingState: $recordingState, expected: ${AudioRecord.RECORDSTATE_RECORDING}")

            if (recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                Log.e(TAG, "AudioRecord failed to start recording")
                showToast("Failed to start recording")
                audioRecord?.release()
                audioRecord = null
                isListening = false
                return
            }

            Log.d(TAG, "Audio recording started successfully - manual stop required")

            // Start recording job - NO AUTO-STOP, records until user presses button again
            recordingJob = coroutineScope.launch {
                val audioData = ByteArrayOutputStream()
                val buffer = ByteArray(bufferSize)
                var totalBytesRead = 0

                while (isActive && isListening) {
                    val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1
                    if (read > 0) {
                        audioData.write(buffer, 0, read)
                        totalBytesRead += read
                    } else if (read < 0) {
                        Log.e(TAG, "AudioRecord read error: $read")
                    }
                }

                // Save recorded data for transcription
                pendingAudioData = audioData.toByteArray()
                Log.d(TAG, "Recording loop ended, totalBytesRead=$totalBytesRead, pendingAudioData size=${pendingAudioData?.size ?: 0}")
            }

        } catch (e: Exception) {
            Log.e(TAG, "Error starting audio recording", e)
            showToast("Audio recording failed: ${e.message}")
            isListening = false
            audioRecord?.release()
            audioRecord = null
        }
    }

    private fun stopAudioRecording(transcribe: Boolean = false) {
        Log.d(TAG, "stopAudioRecording called, transcribe=$transcribe, isListening=$isListening")
        isListening = false

        // Wait for recording job to save data - longer delay to ensure data is captured
        handler.postDelayed({
            Log.d(TAG, "Stop delay expired, cancelling recording job")

            // Don't cancel the job - let it finish naturally (isListening=false will stop the loop)
            // Just wait for it to complete
            recordingJob = null

            try {
                Log.d(TAG, "Stopping AudioRecord...")
                audioRecord?.stop()
                audioRecord?.release()
                Log.d(TAG, "AudioRecord stopped and released")
            } catch (e: Exception) {
                Log.e(TAG, "Error stopping audio recording", e)
            }
            audioRecord = null

            // Reset mic button appearance
            expandedPanel.findViewById<ImageButton>(R.id.btnMic)?.let { micBtn ->
                micBtn.alpha = 0.5f
                micBtn.clearColorFilter()
            }

            Log.d(TAG, "pendingAudioData size=${pendingAudioData?.size ?: 0}")

            if (transcribe && pendingAudioData != null && pendingAudioData!!.size > 1000) {
                val dataSize = pendingAudioData!!.size
                val durationSec = dataSize.toFloat() / (SAMPLE_RATE * 2)  // 16-bit = 2 bytes per sample

                // Analyze audio level to check if we captured real audio
                val audioLevel = analyzeAudioLevel(pendingAudioData!!)
                Log.d(TAG, "Audio data: $dataSize bytes, ~${durationSec}s duration, level=$audioLevel")

                if (audioLevel < 100) {
                    Log.w(TAG, "Audio level very low - might be silence or capture issue")
                    showToast("Warning: Audio level very low (${audioLevel})")
                }

                updateStatus("Processing speech...")
                showToast("Processing ${durationSec.toInt()}s audio (level: $audioLevel)...")

                // Transcribe the recorded audio
                coroutineScope.launch {
                    transcribeWithWhisper(pendingAudioData!!)
                    pendingAudioData = null
                }
            } else if (transcribe) {
                val size = pendingAudioData?.size ?: 0
                Log.w(TAG, "Recording too short: $size bytes")
                updateStatus("Recording too short")
                showToast("Recording too short ($size bytes) - please speak longer")
                pendingAudioData = null
            } else {
                updateStatus("Ready")
                pendingAudioData = null
            }
        }, 300)  // Longer delay to ensure recording job finishes
    }

    private suspend fun transcribeWithWhisper(audioData: ByteArray) {
        Log.d(TAG, "transcribeWithWhisper called, audioData size=${audioData.size}, serverUrl=$serverUrl")

        if (serverUrl.isEmpty()) {
            Log.e(TAG, "No server URL configured for STT")
            withContext(Dispatchers.Main) {
                updateStatus("No server configured")
                showToast("No server configured for speech recognition")
            }
            return
        }

        try {
            // Save audio to temp WAV file
            val audioFile = File(cacheDir, "voice_input_${System.currentTimeMillis()}.wav")
            writeWavFile(audioFile, audioData)
            Log.d(TAG, "WAV file saved: ${audioFile.absolutePath}, size=${audioFile.length()}")

            val client = OkHttpClient.Builder()
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

            Log.d(TAG, "Sending STT request to: $serverUrl/stt")

            val request = Request.Builder()
                .url("$serverUrl/stt")
                .post(requestBody)
                .build()

            val response = client.newCall(request).execute()
            Log.d(TAG, "STT response code: ${response.code}")

            withContext(Dispatchers.Main) {
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: "{}"
                    Log.d(TAG, "STT response body: $body")

                    val json = JSONObject(body)
                    val text = json.optString("text", "")
                    Log.d(TAG, "Extracted text: '$text', length=${text.length}")

                    if (text.isNotEmpty()) {
                        Log.d(TAG, "Setting text to promptInput, promptInput=${promptInput != null}")

                        // Extra safety - get promptInput reference again
                        val input = promptInput ?: expandedPanel.findViewById<EditText>(R.id.promptInput)
                        if (input != null) {
                            input.setText(text)
                            input.setSelection(text.length)
                            Log.d(TAG, "Text set successfully to: '${input.text}'")
                            showToast("Transcribed: ${text.take(30)}...")
                        } else {
                            Log.e(TAG, "promptInput is NULL - cannot set text!")
                            showToast("Error: input field not found")
                        }
                        updateStatus("Ready")
                    } else {
                        Log.w(TAG, "STT returned empty text")
                        updateStatus("No speech detected")
                        showToast("No speech detected")
                    }
                } else {
                    val errorBody = response.body?.string()
                    Log.e(TAG, "STT failed: ${response.code}, body: $errorBody")
                    updateStatus("Transcription failed")
                    showToast("STT failed: ${response.code}")
                }
            }

            // Cleanup
            audioFile.delete()

        } catch (e: Exception) {
            Log.e(TAG, "Error transcribing audio", e)
            withContext(Dispatchers.Main) {
                updateStatus("Transcription error")
                showToast("Transcription error: ${e.message}")
            }
        }
    }

    private fun analyzeAudioLevel(audioData: ByteArray): Int {
        // Analyze 16-bit PCM audio to get average absolute amplitude
        // This helps detect if we're capturing silence vs actual audio
        if (audioData.size < 2) return 0

        var sum: Long = 0
        var count = 0

        // Sample every 100th sample to speed up analysis
        var i = 0
        while (i < audioData.size - 1) {
            // Convert 2 bytes to 16-bit signed sample (little-endian)
            val sample = (audioData[i].toInt() and 0xFF) or
                        ((audioData[i + 1].toInt()) shl 8)
            sum += kotlin.math.abs(sample)
            count++
            i += 200  // Skip samples for faster analysis
        }

        return if (count > 0) (sum / count).toInt() else 0
    }

    private fun writeWavFile(file: File, pcmData: ByteArray) {
        FileOutputStream(file).use { out ->
            val totalDataLen = pcmData.size + 36
            val byteRate = SAMPLE_RATE * 1 * 16 / 8  // mono, 16-bit

            // WAV header
            out.write("RIFF".toByteArray())
            out.write(intToByteArray(totalDataLen))
            out.write("WAVE".toByteArray())
            out.write("fmt ".toByteArray())
            out.write(intToByteArray(16))  // Subchunk1Size
            out.write(shortToByteArray(1.toShort()))  // AudioFormat (PCM)
            out.write(shortToByteArray(1.toShort()))  // NumChannels (mono)
            out.write(intToByteArray(SAMPLE_RATE))  // SampleRate
            out.write(intToByteArray(byteRate))  // ByteRate
            out.write(shortToByteArray(2.toShort()))  // BlockAlign
            out.write(shortToByteArray(16.toShort()))  // BitsPerSample
            out.write("data".toByteArray())
            out.write(intToByteArray(pcmData.size))
            out.write(pcmData)
        }
    }

    private fun intToByteArray(value: Int): ByteArray {
        return byteArrayOf(
            (value and 0xff).toByte(),
            ((value shr 8) and 0xff).toByte(),
            ((value shr 16) and 0xff).toByte(),
            ((value shr 24) and 0xff).toByte()
        )
    }

    private fun shortToByteArray(value: Short): ByteArray {
        return byteArrayOf(
            (value.toInt() and 0xff).toByte(),
            ((value.toInt() shr 8) and 0xff).toByte()
        )
    }

    // ==================== TTS via BlackBox ====================

    private fun toggleAutoTts() {
        autoTtsEnabled = !autoTtsEnabled
        expandedPanel.findViewById<ImageButton>(R.id.btnTts)?.alpha = if (autoTtsEnabled) 1.0f else 0.5f
        ttsLabel?.text = if (autoTtsEnabled) "TTS: On" else "TTS: Off"
        ttsLabel?.setTextColor(if (autoTtsEnabled) 0xFF00D4FF.toInt() else 0xFF888888.toInt())
        showToast(if (autoTtsEnabled) "Auto TTS enabled" else "Auto TTS disabled")
    }

    private fun setupTtsPlayerControls() {
        // Play/Pause button
        btnTtsPlayPause?.setOnClickListener {
            animateButtonClick(it)
            mediaPlayer?.let { mp ->
                if (mp.isPlaying) {
                    mp.pause()
                    btnTtsPlayPause?.setImageResource(android.R.drawable.ic_media_play)
                } else {
                    mp.start()
                    btnTtsPlayPause?.setImageResource(android.R.drawable.ic_media_pause)
                    handler.post(seekBarUpdateRunnable)
                }
            }
        }

        // Stop button - stop and hide player
        btnTtsStop?.setOnClickListener {
            animateButtonClick(it)
            stopTtsPlayback()
        }

        // SeekBar listener
        ttsSeekBar?.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
                if (fromUser) {
                    mediaPlayer?.let { mp ->
                        val duration = mp.duration
                        if (duration > 0) {
                            val newPos = (progress * duration / 100)
                            ttsCurrentTime?.text = formatTime(newPos)
                        }
                    }
                }
            }

            override fun onStartTrackingTouch(seekBar: SeekBar?) {
                isUserSeeking = true
            }

            override fun onStopTrackingTouch(seekBar: SeekBar?) {
                isUserSeeking = false
                seekBar?.let { sb ->
                    mediaPlayer?.let { mp ->
                        val duration = mp.duration
                        if (duration > 0) {
                            val newPos = (sb.progress * duration / 100)
                            mp.seekTo(newPos)
                        }
                    }
                }
            }
        })
    }

    private fun stopTtsPlayback() {
        handler.removeCallbacks(seekBarUpdateRunnable)
        mediaPlayer?.let { mp ->
            if (mp.isPlaying) {
                mp.stop()
            }
            mp.release()
        }
        mediaPlayer = null
        ttsPlayerBar?.visibility = View.GONE
        currentTtsFile?.delete()
        currentTtsFile = null
        updateStatus("Ready")
    }

    private fun formatTime(ms: Int): String {
        val seconds = (ms / 1000) % 60
        val minutes = (ms / 1000) / 60
        return String.format("%d:%02d", minutes, seconds)
    }

    private fun formatRecordingTime(ms: Long): String {
        val seconds = ((ms / 1000) % 60).toInt()
        val minutes = ((ms / 1000) / 60).toInt()
        return String.format("%02d:%02d", minutes, seconds)
    }

    private fun showTtsPlayerBar(duration: Int) {
        ttsPlayerBar?.visibility = View.VISIBLE
        ttsSeekBar?.progress = 0
        ttsCurrentTime?.text = "0:00"
        ttsDuration?.text = formatTime(duration)
        btnTtsPlayPause?.setImageResource(android.R.drawable.ic_media_pause)
    }

    private fun speakResponseWithBlackBox(text: String) {
        if (!autoTtsEnabled) {
            Log.d(TAG, "TTS disabled, skipping speech")
            return
        }
        if (serverUrl.isEmpty()) {
            Log.w(TAG, "No server URL for TTS")
            return
        }

        // OpenAI TTS supports up to 4096 characters - allow full messages
        val ttsText = if (text.length > 4000) text.take(4000) + "..." else text
        Log.d(TAG, "Starting TTS for ${ttsText.length} chars (original: ${text.length})")

        updateStatus("Speaking...")
        showToast("Speaking response...")

        coroutineScope.launch {
            try {
                val client = OkHttpClient.Builder()
                    .connectTimeout(30, TimeUnit.SECONDS)
                    .writeTimeout(60, TimeUnit.SECONDS)
                    .readTimeout(60, TimeUnit.SECONDS)
                    .build()

                // Use the /tts endpoint - returns raw audio data
                val requestBody = JSONObject().apply {
                    put("text", ttsText)
                    put("voice", "onyx")  // Deep voice for AI responses
                    put("model", "tts-1-hd")
                }.toString().toRequestBody("application/json".toMediaType())

                Log.d(TAG, "TTS request to: $serverUrl/tts")

                val request = Request.Builder()
                    .url("$serverUrl/tts")
                    .post(requestBody)
                    .build()

                val response = client.newCall(request).execute()
                Log.d(TAG, "TTS response code: ${response.code}")

                if (response.isSuccessful) {
                    val audioBytes = response.body?.bytes()
                    Log.d(TAG, "TTS received ${audioBytes?.size ?: 0} bytes")

                    if (audioBytes != null && audioBytes.isNotEmpty()) {
                        // Save to temp file and play
                        val audioFile = File(cacheDir, "tts_response_${System.currentTimeMillis()}.mp3")
                        audioFile.writeBytes(audioBytes)
                        Log.d(TAG, "TTS saved to: ${audioFile.absolutePath}")

                        withContext(Dispatchers.Main) {
                            playAudioFile(audioFile)
                        }
                    } else {
                        Log.e(TAG, "TTS returned empty audio data")
                        withContext(Dispatchers.Main) {
                            updateStatus("TTS returned empty audio")
                        }
                    }
                } else {
                    val errorBody = response.body?.string()
                    Log.e(TAG, "TTS request failed: ${response.code}, body: $errorBody")
                    withContext(Dispatchers.Main) {
                        updateStatus("TTS failed: ${response.code}")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error with TTS", e)
                withContext(Dispatchers.Main) {
                    updateStatus("TTS error: ${e.message}")
                    showToast("TTS error: ${e.message}")
                }
            }
        }
    }

    private fun playAudioFile(file: File) {
        Log.d(TAG, "playAudioFile: ${file.absolutePath}, exists=${file.exists()}, size=${file.length()}")

        if (!file.exists() || file.length() == 0L) {
            Log.e(TAG, "Audio file doesn't exist or is empty")
            updateStatus("Audio file error")
            return
        }

        try {
            // Stop any existing playback and cleanup old file (auto-replace behavior)
            handler.removeCallbacks(seekBarUpdateRunnable)
            mediaPlayer?.let { mp ->
                if (mp.isPlaying) {
                    mp.stop()
                }
                mp.release()
            }
            mediaPlayer = null
            currentTtsFile?.delete()  // Delete previous TTS file

            // Store new file reference
            currentTtsFile = file

            mediaPlayer = MediaPlayer().apply {
                setDataSource(file.absolutePath)
                setOnPreparedListener { mp ->
                    Log.d(TAG, "MediaPlayer prepared, duration=${mp.duration}ms, starting playback")

                    // Show player bar with duration
                    showTtsPlayerBar(mp.duration)

                    mp.start()
                    showToast("Playing TTS...")
                    updateStatus("Playing...")

                    // Start seek bar updates
                    handler.post(seekBarUpdateRunnable)
                }
                setOnCompletionListener { mp ->
                    Log.d(TAG, "MediaPlayer playback completed")
                    handler.removeCallbacks(seekBarUpdateRunnable)
                    handler.post {
                        // Update UI to show completed state
                        btnTtsPlayPause?.setImageResource(android.R.drawable.ic_media_play)
                        ttsSeekBar?.progress = 100
                        updateStatus("Ready")
                        // Don't delete file or hide player - keep it available for replay
                    }
                }
                setOnErrorListener { mp, what, extra ->
                    Log.e(TAG, "MediaPlayer error: what=$what, extra=$extra")
                    handler.removeCallbacks(seekBarUpdateRunnable)
                    mp.release()
                    mediaPlayer = null
                    handler.post {
                        ttsPlayerBar?.visibility = View.GONE
                        updateStatus("Playback error")
                        showToast("Audio playback error")
                    }
                    true
                }
                Log.d(TAG, "MediaPlayer preparing async...")
                prepareAsync()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error playing audio", e)
            updateStatus("Playback error: ${e.message}")
            showToast("Audio error: ${e.message}")
        }
    }

    // ==================== Screenshot & Recording ====================

    private fun takeScreenshot() {
        Log.d(TAG, "takeScreenshot called, mediaProjection=${mediaProjection != null}, invalidated=$mediaProjectionInvalidated, persistentDisplay=${persistentScreenshotDisplay != null}")

        if (mediaProjection == null || mediaProjectionInvalidated) {
            Log.e(TAG, "MediaProjection is not valid. Requesting refresh.")
            requestMediaProjectionRefresh()
            return
        }

        // If persistent display is alive, MediaProjection is valid
        if (persistentScreenshotDisplay != null) {
            mediaProjectionInvalidated = false
            Log.d(TAG, "Persistent display active - proceeding with screenshot")
            proceedWithScreenshot()
            return
        }


        // Persistent display doesn't exist but MediaProjection might still be valid
        // Try to set up persistent display with callback
        Log.d(TAG, "Persistent display not ready, setting up now before screenshot...")
        updateStatus("Preparing capture...")

        setupPersistentScreenshotDisplay { success ->
            if (success) {
                Log.d(TAG, "Persistent display setup successful via callback, proceeding with screenshot")
                mediaProjectionInvalidated = false
                proceedWithScreenshot()
            } else {
                // MediaProjection is definitely invalid
                Log.e(TAG, "Cannot screenshot - MediaProjection invalidated during persistent display setup")
                requestMediaProjectionRefresh()
            }
        }
    }

    /**
     * Proceed with taking screenshot after validation checks pass
     */
    private fun proceedWithScreenshot() {
        Log.d(TAG, "Taking screenshot... screen=${screenWidth}x${screenHeight}@${screenDensity}")
        updateStatus("Capturing...")
        showToast("Capturing screenshot...")

        // Hide overlay temporarily
        val wasExpanded = isExpanded
        overlayView.visibility = View.GONE
        expandedPanel.visibility = View.GONE

        // Calculate delay - need extra time after recording stopped (Android 14+ requirement)
        val timeSinceRecordingStop = System.currentTimeMillis() - lastRecordingStopTime
        val minDelayAfterRecording = if (Build.VERSION.SDK_INT >= 34) 800L else 500L
        val extraDelay = if (lastRecordingStopTime > 0 && timeSinceRecordingStop < minDelayAfterRecording) {
            val needed = minDelayAfterRecording - timeSinceRecordingStop
            Log.d(TAG, "Recently stopped recording, adding ${needed}ms delay before screenshot")
            needed
        } else {
            0L
        }

        // Small delay to let views hide + extra delay if needed after recording
        handler.postDelayed({
            captureScreen { file ->
                handler.post {
                    // Restore overlay state
                    if (wasExpanded) {
                        expandedPanel.visibility = View.VISIBLE
                    } else {
                        overlayView.visibility = View.VISIBLE
                    }

                    if (file != null) {
                        attachedScreenshots.add(file)
                        updateAttachmentCount()
                        showToast("Screenshot added (${attachedScreenshots.size})")
                        updateStatus("Ready")
                    } else {
                        showToast("Screenshot failed")
                        updateStatus("Capture failed")
                    }
                }
            }
        }, 200 + extraDelay)
    }

    private fun captureScreen(callback: (File?) -> Unit) {
        Log.d(TAG, "captureScreen starting... persistentDisplay=${persistentScreenshotDisplay != null}")
        try {
            // PRIORITY: Use persistent screenshot display if available (Android 14+ fix)
            if (persistentScreenshotDisplay != null && persistentImageReader != null) {
                Log.d(TAG, "Using persistent screenshot display - no VirtualDisplay recreation needed")
                attemptImageCaptureFromPersistent(callback, attempt = 1, maxAttempts = 3)
                return
            }

            // FALLBACK: Setup persistent display if not yet created - USE CALLBACK to wait for all retries
            if (mediaProjection != null && persistentScreenshotDisplay == null && !mediaProjectionInvalidated) {
                Log.d(TAG, "Persistent display not ready, setting up now with callback...")
                setupPersistentScreenshotDisplay { success ->
                    // This callback is invoked after all retries complete
                    if (success && persistentScreenshotDisplay != null && persistentImageReader != null) {
                        Log.d(TAG, "Persistent display setup succeeded, capturing from persistent")
                        attemptImageCaptureFromPersistent(callback, attempt = 1, maxAttempts = 3)
                    } else {
                        Log.e(TAG, "Cannot capture - persistent display setup failed, requesting refresh.")
                        requestMediaProjectionRefresh()
                        callback(null)
                    }
                }
                return
            }

            // If we've reached here, the projection is invalid.
            Log.e(TAG, "Cannot capture - MediaProjection is invalid or null.")
            requestMediaProjectionRefresh()
            callback(null)


        } catch (e: Exception) {
            Log.e(TAG, "Error in captureScreen", e)
            callback(null)
        }
    }

    /**
     * Capture image from the persistent ImageReader - no VirtualDisplay creation needed.
     * This is the fast path that keeps MediaProjection alive.
     */
    private fun attemptImageCaptureFromPersistent(callback: (File?) -> Unit, attempt: Int, maxAttempts: Int) {
        val reader = persistentImageReader
        if (reader == null) {
            Log.e(TAG, "Persistent ImageReader is null")
            callback(null)
            return
        }

        val delays = listOf(100L, 200L, 400L)
        val delay = delays.getOrElse(attempt - 1) { 400L }

        handler.postDelayed({
            try {
                val image = reader.acquireLatestImage()
                if (image != null) {
                    Log.d(TAG, "Captured image from persistent display on attempt $attempt: ${image.width}x${image.height}")

                    // Convert image to file (same logic as attemptImageCapture)
                    val planes = image.planes
                    val buffer = planes[0].buffer
                    val pixelStride = planes[0].pixelStride
                    val rowStride = planes[0].rowStride
                    val rowPadding = rowStride - pixelStride * screenWidth

                    val bitmap = Bitmap.createBitmap(
                        screenWidth + rowPadding / pixelStride,
                        screenHeight,
                        Bitmap.Config.ARGB_8888
                    )
                    bitmap.copyPixelsFromBuffer(buffer)
                    image.close()

                    // Crop to actual screen size
                    val croppedBitmap = Bitmap.createBitmap(
                        bitmap, 0, 0, screenWidth, screenHeight
                    )
                    bitmap.recycle()

                    // Save to file
                    val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(Date())
                    val file = File(cacheDir, "screenshot_$timestamp.png")
                    FileOutputStream(file).use { out ->
                        croppedBitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
                    }
                    croppedBitmap.recycle()

                    Log.d(TAG, "Screenshot saved from persistent display: ${file.absolutePath}, size=${file.length()}")
                    callback(file)
                } else if (attempt < maxAttempts) {
                    Log.d(TAG, "No image on persistent attempt $attempt, retrying...")
                    attemptImageCaptureFromPersistent(callback, attempt + 1, maxAttempts)
                } else {
                    Log.e(TAG, "Failed to capture from persistent display after $maxAttempts attempts")
                    callback(null)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error capturing from persistent display", e)
                if (attempt < maxAttempts) {
                    attemptImageCaptureFromPersistent(callback, attempt + 1, maxAttempts)
                } else {
                    callback(null)
                }
            }
        }, delay)
    }

    /**
     * Legacy screenshot capture - creates new VirtualDisplay each time.
     * Used as fallback when persistent display isn't available.
     */
    private fun captureScreenLegacy(callback: (File?) -> Unit) {
        Log.d(TAG, "Using legacy screenshot capture (creates new VirtualDisplay)")
        try {
            // If we were in recording mode, release those resources first
            if (currentDisplayMode == DisplayMode.RECORDING) {
                Log.d(TAG, "Switching from recording mode to screenshot mode")
                virtualDisplay?.release()
                virtualDisplay = null
                currentDisplayMode = DisplayMode.NONE
            }

            // Reuse existing screenshot VirtualDisplay if available
            if (currentDisplayMode == DisplayMode.SCREENSHOT && virtualDisplay != null && imageReader != null) {
                Log.d(TAG, "Reusing existing screenshot VirtualDisplay")
                attemptImageCapture(callback, attempt = 1, maxAttempts = 3)
                return
            }

            // Need to create new screenshot VirtualDisplay
            Log.d(TAG, "Creating new screenshot VirtualDisplay")

            // Clean up any existing resources
            virtualDisplay?.release()
            virtualDisplay = null
            imageReader?.close()
            imageReader = null

            Log.d(TAG, "Creating ImageReader ${screenWidth}x${screenHeight}")
            imageReader = ImageReader.newInstance(
                screenWidth, screenHeight,
                PixelFormat.RGBA_8888, 5
            )

            Log.d(TAG, "Creating VirtualDisplay...")
            try {
                virtualDisplay = mediaProjection?.createVirtualDisplay(
                    "Screenshot",
                    screenWidth, screenHeight, screenDensity,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    imageReader?.surface, null, handler
                )
            } catch (se: SecurityException) {
                Log.e(TAG, "SecurityException creating VirtualDisplay - MediaProjection invalidated", se)
                mediaProjectionInvalidated = true
                imageReader?.close()
                imageReader = null

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    handler.post { requestMediaProjectionRefresh() }
                } else {
                    handler.post {
                        showToast("Restart overlay for screenshots")
                        updateStatus("Restart overlay for screenshots")
                    }
                }
                callback(null)
                return
            }

            if (virtualDisplay == null) {
                Log.e(TAG, "Failed to create virtual display for screenshot")
                handler.post {
                    showToast("Screenshot failed - restart overlay")
                    updateStatus("Capture permission lost")
                }
                callback(null)
                return
            }

            currentDisplayMode = DisplayMode.SCREENSHOT
            Log.d(TAG, "VirtualDisplay created successfully, mode set to SCREENSHOT")

            // Try multiple times with increasing delays
            attemptImageCapture(callback, attempt = 1, maxAttempts = 3)

        } catch (e: Exception) {
            Log.e(TAG, "Error setting up screen capture", e)
            callback(null)
        }
    }

    private fun attemptImageCapture(callback: (File?) -> Unit, attempt: Int, maxAttempts: Int) {
        val delay = 200L + (attempt * 150L)  // 350ms, 500ms, 650ms
        Log.d(TAG, "Attempting image capture (attempt $attempt/$maxAttempts) with ${delay}ms delay")

        handler.postDelayed({
            Log.d(TAG, "Acquiring image from ImageReader (attempt $attempt)...")
            try {
                val image = imageReader?.acquireLatestImage()
                if (image != null) {
                    Log.d(TAG, "Image acquired: ${image.width}x${image.height}")
                    val planes = image.planes
                    val buffer = planes[0].buffer
                    val pixelStride = planes[0].pixelStride
                    val rowStride = planes[0].rowStride
                    val rowPadding = rowStride - pixelStride * screenWidth

                    Log.d(TAG, "Creating bitmap from buffer...")
                    val bitmap = Bitmap.createBitmap(
                        screenWidth + rowPadding / pixelStride,
                        screenHeight,
                        Bitmap.Config.ARGB_8888
                    )
                    bitmap.copyPixelsFromBuffer(buffer)
                    image.close()

                    // Crop to actual screen size
                    val croppedBitmap = Bitmap.createBitmap(
                        bitmap, 0, 0, screenWidth, screenHeight
                    )
                    bitmap.recycle()

                    // Save to file
                    val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(Date())
                    val file = File(cacheDir, "screenshot_$timestamp.png")
                    FileOutputStream(file).use { out ->
                        croppedBitmap.compress(Bitmap.CompressFormat.PNG, 100, out)
                    }
                    croppedBitmap.recycle()

                    Log.d(TAG, "Screenshot saved: ${file.absolutePath}, size=${file.length()}")

                    // DON'T cleanup - keep VirtualDisplay alive for subsequent screenshots
                    // cleanupCapture() is now only called when switching to recording mode
                    callback(file)
                } else {
                    Log.w(TAG, "Image is null on attempt $attempt")
                    if (attempt < maxAttempts) {
                        // Retry
                        attemptImageCapture(callback, attempt + 1, maxAttempts)
                    } else {
                        Log.e(TAG, "Failed to acquire image after $maxAttempts attempts")
                        // On failure, cleanup and reset mode so we can try fresh next time
                        cleanupCapture()
                        currentDisplayMode = DisplayMode.NONE
                        showToast("Screenshot failed after retries")
                        callback(null)
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error capturing screen on attempt $attempt", e)
                cleanupCapture()
                currentDisplayMode = DisplayMode.NONE
                showToast("Screenshot error: ${e.message}")
                callback(null)
            }
        }, delay)
    }

    private fun cleanupCapture() {
        try {
            virtualDisplay?.release()
            virtualDisplay = null
            imageReader?.close()
            imageReader = null
        } catch (e: Exception) {
            Log.e(TAG, "Error cleaning up capture resources", e)
        }
    }

    // ==================== Single-frame PNG capture (Task W4 — on-device vision) ====================

    /**
     * Capture ONE screen frame as PNG BYTES (in memory, never a file), for the
     * on-device "look at my screen" vision path (Task W4.2). REUSES the existing
     * MediaProjection + the persistent ImageReader that already power the
     * screenshot/screen-share features — no new projection, no new consent flow
     * (the projection consent is already granted when the overlay is running).
     *
     * Unlike [takeScreenshot] this does NOT write a file, add to
     * [attachedScreenshots], or toast — the bytes are EPHEMERAL (handed to the
     * model's prompt and dropped). It DOES hide the floating overlay briefly so the
     * BlackBox bubble isn't in the captured frame, then restores it.
     *
     * Calls [callback] on the main thread with the PNG bytes, or null when capture
     * is unavailable (no/invalid MediaProjection, or no frame after retries). Never
     * throws.
     */
    private fun captureFramePngBytes(callback: (ByteArray?) -> Unit) {
        if (mediaProjection == null || mediaProjectionInvalidated) {
            Log.w(TAG, "captureFramePngBytes: no valid MediaProjection -> null")
            requestMediaProjectionRefresh()
            callback(null)
            return
        }

        val proceed = {
            // Hide overlay so the bubble/panel isn't in the frame.
            val wasExpanded = isExpanded
            overlayView.visibility = View.GONE
            expandedPanel.visibility = View.GONE
            handler.postDelayed({
                attemptFramePngFromPersistent(attempt = 1, maxAttempts = 3) { bytes ->
                    handler.post {
                        if (wasExpanded) expandedPanel.visibility = View.VISIBLE
                        else overlayView.visibility = View.VISIBLE
                        callback(bytes)
                    }
                }
            }, 200)
        }

        if (persistentScreenshotDisplay != null && persistentImageReader != null) {
            mediaProjectionInvalidated = false
            proceed()
        } else {
            setupPersistentScreenshotDisplay { success ->
                if (success && persistentScreenshotDisplay != null && persistentImageReader != null) {
                    mediaProjectionInvalidated = false
                    proceed()
                } else {
                    Log.e(TAG, "captureFramePngBytes: persistent display setup failed -> null")
                    requestMediaProjectionRefresh()
                    callback(null)
                }
            }
        }
    }

    /**
     * Acquire one frame from the persistent ImageReader and compress it to PNG
     * bytes (mirrors [attemptImageCaptureFromPersistent] but returns bytes, not a
     * file). Retries with backoff up to [maxAttempts]; calls back null on failure.
     */
    private fun attemptFramePngFromPersistent(
        attempt: Int,
        maxAttempts: Int,
        callback: (ByteArray?) -> Unit,
    ) {
        val reader = persistentImageReader
        if (reader == null) {
            callback(null)
            return
        }
        val delays = listOf(100L, 200L, 400L)
        val delay = delays.getOrElse(attempt - 1) { 400L }
        handler.postDelayed({
            try {
                val image = reader.acquireLatestImage()
                if (image != null) {
                    val planes = image.planes
                    val buffer = planes[0].buffer
                    val pixelStride = planes[0].pixelStride
                    val rowStride = planes[0].rowStride
                    val rowPadding = rowStride - pixelStride * screenWidth

                    val bitmap = Bitmap.createBitmap(
                        screenWidth + rowPadding / pixelStride,
                        screenHeight,
                        Bitmap.Config.ARGB_8888,
                    )
                    bitmap.copyPixelsFromBuffer(buffer)
                    image.close()

                    val cropped = Bitmap.createBitmap(bitmap, 0, 0, screenWidth, screenHeight)
                    bitmap.recycle()

                    val baos = ByteArrayOutputStream()
                    cropped.compress(Bitmap.CompressFormat.PNG, 100, baos)
                    cropped.recycle()
                    val bytes = baos.toByteArray()
                    Log.d(TAG, "captureFramePngBytes: captured ${bytes.size} PNG bytes")
                    callback(bytes)
                } else if (attempt < maxAttempts) {
                    attemptFramePngFromPersistent(attempt + 1, maxAttempts, callback)
                } else {
                    Log.e(TAG, "captureFramePngBytes: no frame after $maxAttempts attempts")
                    callback(null)
                }
            } catch (e: Exception) {
                Log.e(TAG, "captureFramePngBytes: error acquiring frame", e)
                if (attempt < maxAttempts) attemptFramePngFromPersistent(attempt + 1, maxAttempts, callback)
                else callback(null)
            }
        }, delay)
    }

    // ==================== Recording Stop Button ====================

    private var recordingStopButtonParams: WindowManager.LayoutParams? = null

    private fun createRecordingStopButton() {
        if (recordingStopButton != null) return

        recordingStopButton = LayoutInflater.from(this).inflate(R.layout.overlay_recording_stop, null)

        recordingStopButtonParams = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            x = 0
            y = 50  // Near top of screen
        }

        // Get UI references
        recordingTimer = recordingStopButton?.findViewById(R.id.recordingTimer)
        recordingIndicator = recordingStopButton?.findViewById(R.id.recordingIndicator)
        val stopBtn = recordingStopButton?.findViewById<ImageView>(R.id.btnStopRecording)

        // Setup stop button click listener - stops recording
        stopBtn?.setOnClickListener {
            Log.d(TAG, "Recording stop button clicked")
            stopRecording()
        }

        // Also allow clicking anywhere on the bar to stop
        recordingStopButton?.setOnClickListener {
            Log.d(TAG, "Recording stop bar clicked")
            stopRecording()
        }

        // Make it draggable
        setupRecordingStopDragListener()

        try {
            windowManager.addView(recordingStopButton, recordingStopButtonParams)
            Log.d(TAG, "Recording stop button created")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add recording stop button", e)
        }
    }

    private fun setupRecordingStopDragListener() {
        var initialX = 0
        var initialY = 0
        var initialTouchX = 0f
        var initialTouchY = 0f
        var isDragging = false

        recordingStopButton?.setOnTouchListener { v, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    initialX = recordingStopButtonParams?.x ?: 0
                    initialY = recordingStopButtonParams?.y ?: 0
                    initialTouchX = event.rawX
                    initialTouchY = event.rawY
                    isDragging = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val deltaX = (event.rawX - initialTouchX).toInt()
                    val deltaY = (event.rawY - initialTouchY).toInt()

                    if (Math.abs(deltaX) > 10 || Math.abs(deltaY) > 10) {
                        isDragging = true
                    }

                    recordingStopButtonParams?.x = initialX + deltaX
                    recordingStopButtonParams?.y = initialY + deltaY

                    try {
                        windowManager.updateViewLayout(recordingStopButton, recordingStopButtonParams)
                    } catch (e: Exception) {
                        Log.e(TAG, "Error updating recording stop button layout", e)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (!isDragging) {
                        v.performClick()
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun showRecordingStopButton() {
        if (recordingStopButton == null) {
            createRecordingStopButton()
        }
        recordingStopButton?.visibility = View.VISIBLE

        // Start timer
        recordingStartTime = System.currentTimeMillis()
        recordingTimer?.text = "00:00"
        recordingIndicator?.alpha = 1.0f
        handler.post(recordingTimerRunnable)

        Log.d(TAG, "Recording stop button shown")
    }

    private fun hideRecordingStopButton() {
        handler.removeCallbacks(recordingTimerRunnable)
        recordingStopButton?.visibility = View.GONE
        Log.d(TAG, "Recording stop button hidden")
    }

    private fun removeRecordingStopButton() {
        handler.removeCallbacks(recordingTimerRunnable)
        try {
            recordingStopButton?.let {
                windowManager.removeView(it)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error removing recording stop button", e)
        }
        recordingStopButton = null
        recordingTimer = null
        recordingIndicator = null
        recordingStopButtonParams = null
    }

    /**
     * Request MediaProjection refresh by broadcasting to PortalActivity.
     * This stops the overlay service and requests a new MediaProjection permission.
     * Used on Android 14+ where MediaProjection becomes invalid after use.
     * @param forRecording If true, the new MediaProjection will be used for recording (skip persistent display)
     */
    private fun requestMediaProjectionRefresh(forRecording: Boolean = false) {
        Log.d(TAG, "Requesting MediaProjection refresh (forRecording=$forRecording)...")
        showToast(if (forRecording) "Requesting recording permission..." else "Refreshing capture permission...")

        // Broadcast to PortalActivity to restart the overlay with fresh permission
        val intent = Intent(ACTION_REFRESH_PROJECTION)
        intent.setPackage(packageName)
        intent.putExtra(EXTRA_RECORDING_MODE, forRecording)  // Pass recording mode flag
        sendBroadcast(intent)

        // Stop this service - PortalActivity will restart it with new permission
        handler.postDelayed({
            stopSelf()
        }, 500)
    }

    private fun toggleRecording() {
        if (isRecording) {
            stopRecording()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        if (mediaProjection == null) {
            showToast("Recording permission not granted")
            return
        }

        // Android 14+ restriction: Can only call createVirtualDisplay() ONCE per MediaProjection
        // If persistent screenshot display exists, we need to switch modes
        if (persistentScreenshotDisplay != null && Build.VERSION.SDK_INT >= 34) {
            Log.d(TAG, "Android 14+ detected: Switching from screenshot mode to recording mode")
            showToast("Switching to recording mode...")

            // Release screenshot display first
            persistentScreenshotDisplay?.release()
            persistentScreenshotDisplay = null
            persistentImageReader?.close()
            persistentImageReader = null

            // Try to create recording display with existing MediaProjection
            // If this fails (Android 14+ token exhausted), we'll request a new MediaProjection
            proceedWithRecording()
            return
        }

        // If MediaProjection is already known to be invalidated, request refresh
        if (mediaProjectionInvalidated) {
            Log.e(TAG, "MediaProjection is invalid. Requesting a new permission.")
            requestMediaProjectionRefresh()
            return
        }

        proceedWithRecording()
    }


    /**
     * Common path for starting recording once all checks pass
     */
    private fun proceedWithRecording() {
        // KEEP persistent screenshot display alive - it keeps MediaProjection valid!
        // MediaProjection CAN support multiple VirtualDisplays simultaneously.
        // The persistent display ensures the token remains valid for both screenshots AND recording.
        Log.d(TAG, "Starting recording while keeping persistent screenshot display alive")

        // Release any legacy displays, just in case.
        if (virtualDisplay != null) {
            Log.d(TAG, "Releasing legacy VirtualDisplay before recording")
            virtualDisplay?.release()
            virtualDisplay = null
            imageReader?.close()
            imageReader = null
        }
        currentDisplayMode = DisplayMode.NONE


        // Small delay to ensure clean state.
        val delayMs = 300L
        Log.d(TAG, "Waiting ${delayMs}ms before creating recording VirtualDisplay...")
        updateStatus("Preparing...")

        handler.postDelayed({
            startRecordingInternal()
        }, delayMs)
    }

    private fun startRecordingInternal() {
        if (mediaProjection == null) {
            Log.w(TAG, "startRecordingInternal: mediaProjection is null, cannot record.")
            showToast("Recording permission lost")
            updateStatus("Ready")
            // Because the projection is null, we should trigger a refresh.
            requestMediaProjectionRefresh()
            return
        }

        Log.d(TAG, "Starting screen recording...")
        updateStatus("Recording...")

        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val outputFile = File(cacheDir, "recording_$timestamp.mp4")

        try {
            mediaRecorder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                MediaRecorder(this)
            } else {
                @Suppress("DEPRECATION")
                MediaRecorder()
            }

            mediaRecorder?.apply {
                setVideoSource(MediaRecorder.VideoSource.SURFACE)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setOutputFile(outputFile.absolutePath)
                setVideoEncoder(MediaRecorder.VideoEncoder.H264)
                setVideoSize(screenWidth, screenHeight)
                setVideoFrameRate(30)
                setVideoEncodingBitRate(5 * 1024 * 1024)
                prepare()
            }

            try {
                // Create recording display SEPARATE from persistent screenshot display
                // Both can coexist - that's the key to keeping MediaProjection valid
                recordingDisplay = mediaProjection?.createVirtualDisplay(
                    "Recording",
                    screenWidth, screenHeight, screenDensity,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    mediaRecorder?.surface, null, handler
                )
            } catch (se: SecurityException) {
                // Android 14+ restriction: MediaProjection exhausted after first createVirtualDisplay
                Log.e(TAG, "SecurityException creating recording VirtualDisplay - Android 14+ requires new MediaProjection", se)
                mediaProjectionInvalidated = true
                mediaRecorder?.release()
                mediaRecorder = null
                currentDisplayMode = DisplayMode.NONE
                updateStatus("Ready")

                // Android 14+ FIX: Request a NEW MediaProjection for recording mode
                // User will grant permission once, then recording works for all future attempts
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    handler.post {
                        requestMediaProjectionRefresh(forRecording = true)  // Pass recording mode flag
                    }
                } else {
                    // Older Android: Try to recover with screenshot display
                    handler.post {
                        showToast("Recording failed. Please restart overlay.")
                        setupPersistentScreenshotDisplay { success ->
                            if (success) Log.d(TAG, "Re-established screenshot display after recording failure.")
                        }
                    }
                }
                return
            }


            if (recordingDisplay == null) {
                Log.e(TAG, "Failed to create recording VirtualDisplay (was null)")
                showToast("Recording failed - display error")
                mediaRecorder?.release()
                mediaRecorder = null
                currentDisplayMode = DisplayMode.NONE
                updateStatus("Ready")
                return
            }

            currentDisplayMode = DisplayMode.RECORDING
            Log.d(TAG, "Recording VirtualDisplay created (separate from screenshot display)")

            mediaRecorder?.start()
            isRecording = true

            // Hide overlay UI and show floating stop button
            overlayView.visibility = View.GONE
            expandedPanel.visibility = View.GONE
            showRecordingStopButton()

            showToast("Recording started - tap stop button when done")
            Log.d(TAG, "Recording started: ${outputFile.absolutePath}")

        } catch (e: Exception) {
            Log.e(TAG, "Error starting recording", e)
            showToast("Recording failed: ${e.message}")
            updateStatus("Recording failed")
            mediaRecorder?.release()
            mediaRecorder = null
            virtualDisplay?.release()
            virtualDisplay = null
        }
    }

    private fun stopRecording() {
        if (!isRecording) return

        Log.d(TAG, "Stopping recording...")
        updateStatus("Saving recording...")

        try {
            mediaRecorder?.stop()
            mediaRecorder?.release()

            // Find the recording file
            val recordings = cacheDir.listFiles { file ->
                file.name.startsWith("recording_") && file.name.endsWith(".mp4")
            }?.sortedByDescending { it.lastModified() }

            val recordingFile = recordings?.firstOrNull()
            if (recordingFile != null && recordingFile.exists()) {
                // Clear old recording and set new one
                attachedRecording?.delete()
                attachedRecording = recordingFile
                updateAttachmentCount()
                showToast("Recording saved!")
            }

        } catch (e: Exception) {
            Log.e(TAG, "Error stopping recording", e)
        } finally {
            mediaRecorder = null
            recordingDisplay?.release()
            recordingDisplay = null
            isRecording = false
            currentDisplayMode = DisplayMode.NONE
            lastRecordingStopTime = System.currentTimeMillis()

            // Persistent screenshot display remains active - no need to re-establish!
            // MediaProjection stays valid because we never released the persistent display.
            Log.d(TAG, "Recording stopped. Persistent screenshot display remains active - MediaProjection still valid.")

            showToast("Recording saved!")
            Log.d(TAG, "Recording stopped, mode reset to NONE.")
            updateStatus("Ready")

            hideRecordingStopButton()

            overlayView.visibility = View.GONE
            expandedPanel.visibility = View.VISIBLE
            isExpanded = true
        }
    }


    // ==================== Chat & Attachments ====================

    private fun clearAttachments() {
        attachedScreenshots.forEach { it.delete() }
        attachedScreenshots.clear()
        attachedRecording?.delete()
        attachedRecording = null
        updateAttachmentCount()
        showToast("Attachments cleared")
    }

    private fun updateAttachmentCount() {
        val count = attachedScreenshots.size + (if (attachedRecording != null) 1 else 0)
        attachmentCount?.text = if (count > 0) "$count" else ""
        attachmentCount?.visibility = if (count > 0) View.VISIBLE else View.GONE

        // Update attachment preview area
        updateAttachmentPreview()
    }

    private fun updateAttachmentPreview() {
        val container = attachmentPreviewContainer ?: return
        val scroll = attachmentPreviewScroll ?: return

        // Clear existing previews
        container.removeAllViews()

        val hasAttachments = attachedScreenshots.isNotEmpty() || attachedRecording != null

        if (!hasAttachments) {
            scroll.visibility = View.GONE
            return
        }

        scroll.visibility = View.VISIBLE

        // Add screenshot previews
        attachedScreenshots.forEachIndexed { index, file ->
            val previewView = createAttachmentThumbnail(file, isVideo = false, index = index)
            container.addView(previewView)
        }

        // Add recording preview
        attachedRecording?.let { file ->
            val previewView = createAttachmentThumbnail(file, isVideo = true, index = -1)
            container.addView(previewView)
        }
    }

    private fun createAttachmentThumbnail(file: File, isVideo: Boolean, index: Int): View {
        val thumbSize = 60  // dp
        val density = resources.displayMetrics.density
        val thumbPx = (thumbSize * density).toInt()
        val marginPx = (4 * density).toInt()

        // Container with remove badge
        val frameLayout = android.widget.FrameLayout(this).apply {
            layoutParams = LinearLayout.LayoutParams(thumbPx + marginPx * 2, thumbPx + marginPx).apply {
                setMargins(marginPx, 0, marginPx, 0)
            }
        }

        // Thumbnail image
        val imageView = ImageView(this).apply {
            layoutParams = android.widget.FrameLayout.LayoutParams(thumbPx, thumbPx).apply {
                gravity = Gravity.CENTER
            }
            scaleType = ImageView.ScaleType.CENTER_CROP
            setBackgroundColor(0xFF333333.toInt())

            if (isVideo) {
                // Video icon placeholder
                setImageResource(android.R.drawable.ic_media_play)
                scaleType = ImageView.ScaleType.CENTER
            } else {
                // Load image thumbnail in background
                try {
                    val options = BitmapFactory.Options().apply {
                        inSampleSize = 4  // Downsample for thumbnail
                    }
                    val bitmap = BitmapFactory.decodeFile(file.absolutePath, options)
                    if (bitmap != null) {
                        setImageBitmap(bitmap)
                    } else {
                        setImageResource(android.R.drawable.ic_menu_gallery)
                        scaleType = ImageView.ScaleType.CENTER
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error loading thumbnail", e)
                    setImageResource(android.R.drawable.ic_menu_gallery)
                    scaleType = ImageView.ScaleType.CENTER
                }
            }
        }

        // Remove button (X) overlay
        val removeBtn = TextView(this).apply {
            layoutParams = android.widget.FrameLayout.LayoutParams(
                (20 * density).toInt(),
                (20 * density).toInt()
            ).apply {
                gravity = Gravity.TOP or Gravity.END
            }
            text = "X"
            textSize = 10f
            setTextColor(0xFFFFFFFF.toInt())
            setBackgroundColor(0xFFFF4444.toInt())
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, 0)

            setOnClickListener {
                // Remove this attachment
                if (isVideo) {
                    attachedRecording?.delete()
                    attachedRecording = null
                    showToast("Recording removed")
                } else if (index >= 0 && index < attachedScreenshots.size) {
                    attachedScreenshots[index].delete()
                    attachedScreenshots.removeAt(index)
                    showToast("Screenshot removed")
                }
                updateAttachmentCount()
            }
        }

        frameLayout.addView(imageView)
        frameLayout.addView(removeBtn)

        // Click to view full image (optional)
        imageView.setOnClickListener {
            if (!isVideo) {
                showToast("Screenshot ${index + 1}")
            } else {
                showToast("Recording attached")
            }
        }

        return frameLayout
    }

    private fun updateStatus(status: String) {
        statusText?.text = status

        // Determine color based on status
        val color = when {
            status == "Ready" || status == "Connected" -> 0xFF44FF44.toInt()  // Green
            status == "Disconnected" -> 0xFF666666.toInt()  // Gray
            status.contains("Speaking") || status.contains("AI") -> 0xFFFFAA00.toInt()  // Orange
            else -> 0xFFFF4444.toInt()  // Red for processing/errors
        }

        statusText?.setTextColor(color)

        // Update status indicator circle color
        (statusIndicator?.background as? GradientDrawable)?.setColor(color)
    }

    private fun sendPromptToAI() {
        val prompt = promptInput?.text?.toString()?.trim() ?: ""

        if (prompt.isEmpty() && attachedScreenshots.isEmpty() && attachedRecording == null) {
            showToast("Enter a prompt or attach screenshots/recording")
            return
        }

        updateStatus("Sending...")
        progressBar?.visibility = View.VISIBLE

        coroutineScope.launch {
            try {
                val response = sendChatRequest(prompt)

                withContext(Dispatchers.Main) {
                    progressBar?.visibility = View.GONE

                    if (response != null) {
                        responseText?.text = response
                        responseText?.visibility = View.VISIBLE

                        // Scroll to show response
                        expandedPanel.findViewById<ScrollView>(R.id.responseScroll)?.fullScroll(View.FOCUS_DOWN)

                        // Auto TTS via BlackBox
                        if (autoTtsEnabled) {
                            speakResponseWithBlackBox(response)
                        } else {
                            updateStatus("Ready")
                        }

                        // Clear prompt and attachments after successful send
                        promptInput?.setText("")
                        clearAttachments()
                    } else {
                        updateStatus("Request failed")
                        showToast("Failed to get response")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending prompt", e)
                withContext(Dispatchers.Main) {
                    progressBar?.visibility = View.GONE
                    updateStatus("Error: ${e.message}")
                    showToast("Error: ${e.message}")
                }
            }
        }
    }

    private suspend fun sendChatRequest(prompt: String): String? {
        if (serverUrl.isEmpty()) {
            Log.w(TAG, "No server URL configured")
            return null
        }

        val client = OkHttpClient.Builder()
            .connectTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .build()

        // Build message content
        val contentArray = JSONArray()

        // Add text prompt
        if (prompt.isNotEmpty()) {
            contentArray.put(JSONObject().apply {
                put("type", "text")
                put("text", prompt)
            })
        }

        // Add screenshots as base64 images
        for ((index, screenshot) in attachedScreenshots.withIndex()) {
            try {
                Log.d(TAG, "Encoding screenshot $index: path=${screenshot.absolutePath}, exists=${screenshot.exists()}, size=${screenshot.length()}")
                val base64 = encodeFileToBase64(screenshot)
                Log.d(TAG, "Screenshot $index base64 length: ${base64.length} chars (~${base64.length * 3 / 4 / 1024} KB)")
                contentArray.put(JSONObject().apply {
                    put("type", "image")
                    put("source", JSONObject().apply {
                        put("type", "base64")
                        put("media_type", "image/png")
                        put("data", base64)
                    })
                })
                Log.d(TAG, "Screenshot $index added to content array")
            } catch (e: Exception) {
                Log.e(TAG, "Error encoding screenshot $index", e)
            }
        }

        // Track if we have video to force Gemini provider (only provider that supports video)
        var hasVideo = false

        // Upload video and send via video_url content type for Gemini analysis
        if (attachedRecording != null) {
            try {
                val videoUrl = uploadFile(attachedRecording!!, "video/mp4")
                if (videoUrl != null) {
                    hasVideo = true
                    // Use video_url content type - Gemini can analyze full video up to 2 minutes
                    contentArray.put(JSONObject().apply {
                        put("type", "video_url")
                        put("video_url", JSONObject().apply {
                            put("url", videoUrl)
                        })
                    })
                    Log.d(TAG, "Video uploaded for Gemini analysis: $videoUrl")
                } else {
                    Log.e(TAG, "Video upload returned null URL")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error uploading video", e)
            }
        }

        // Build request body
        val requestBody = JSONObject().apply {
            put("operator", currentOperator)
            // Use Gemini provider when video is attached (only provider that supports video analysis)
            if (hasVideo) {
                put("provider", "gemini")
                Log.d(TAG, "Using Gemini provider for video analysis")
            }
            put("messages", JSONArray().apply {
                put(JSONObject().apply {
                    put("role", "user")
                    put("content", contentArray)
                })
            })
        }

        // Step 1: Submit chat request and get task_id
        val request = Request.Builder()
            .url("$serverUrl/chat")
            .post(requestBody.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val response = client.newCall(request).execute()

        if (!response.isSuccessful) {
            Log.e(TAG, "Chat request failed: ${response.code}")
            return null
        }

        val responseBody = response.body?.string()
        val json = JSONObject(responseBody ?: "{}")

        // Check if we got a task_id (async) or direct response
        val taskId = json.optString("task_id", null)
        if (taskId.isNullOrEmpty()) {
            // Try direct response (legacy format)
            return json.optString("response", null)
        }

        Log.d(TAG, "Chat task created: $taskId")

        // Step 2: Poll for task completion
        withContext(Dispatchers.Main) {
            updateStatus("Processing...")
        }

        val maxPolls = 120  // 2 minutes max
        val pollInterval = 1000L  // 1 second

        for (i in 0 until maxPolls) {
            delay(pollInterval)

            val statusRequest = Request.Builder()
                .url("$serverUrl/tasks/$taskId")
                .get()
                .build()

            try {
                val statusResponse = client.newCall(statusRequest).execute()
                if (statusResponse.isSuccessful) {
                    val statusBody = statusResponse.body?.string()
                    val statusJson = JSONObject(statusBody ?: "{}")
                    val status = statusJson.optString("status", "")

                    Log.d(TAG, "Task $taskId status: $status")

                    when (status) {
                        "completed" -> {
                            // Get response from result_data - try multiple field names
                            val resultData = statusJson.optJSONObject("result_data")
                            val response = resultData?.optString("ui_reply", null)
                                ?: resultData?.optString("reply", null)
                                ?: resultData?.optString("text", null)
                                ?: resultData?.optString("response", null)
                                ?: statusJson.optString("result_url", null)
                            Log.d(TAG, "Task completed, response length: ${response?.length ?: 0}")
                            return response ?: "Task completed but no response"
                        }
                        "error", "failed" -> {
                            val error = statusJson.optString("error", "Unknown error")
                            Log.e(TAG, "Task failed: $error")
                            return "Error: $error"
                        }
                        "processing", "pending" -> {
                            // Continue polling
                            val progress = statusJson.optInt("progress", 0)
                            withContext(Dispatchers.Main) {
                                updateStatus("Processing... ${progress}%")
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error polling task status", e)
            }
        }

        Log.e(TAG, "Task polling timeout")
        return "Request timed out"
    }

    private fun encodeFileToBase64(file: File): String {
        val bytes = file.readBytes()
        return Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    /**
     * Extract key frames from a video file for AI analysis.
     * Uses MediaMetadataRetriever to get frames at regular intervals.
     */
    private fun extractVideoFrames(videoFile: File, maxFrames: Int = 5): List<Bitmap> {
        val frames = mutableListOf<Bitmap>()
        val retriever = MediaMetadataRetriever()

        try {
            retriever.setDataSource(videoFile.absolutePath)

            // Get video duration in milliseconds
            val durationStr = retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
            val duration = durationStr?.toLongOrNull() ?: 0L

            if (duration <= 0) {
                Log.w(TAG, "Could not get video duration")
                // Try to get at least one frame
                val bitmap = retriever.getFrameAtTime(0)
                bitmap?.let { frames.add(it) }
                return frames
            }

            Log.d(TAG, "Video duration: ${duration}ms")

            // Calculate interval between frames
            val interval = duration / (maxFrames + 1)

            for (i in 1..maxFrames) {
                val timeUs = (interval * i) * 1000 // Convert ms to microseconds
                try {
                    val bitmap = retriever.getFrameAtTime(timeUs, MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
                    if (bitmap != null) {
                        // Scale down if too large (max 1280px on longest side)
                        val scaledBitmap = scaleBitmapIfNeeded(bitmap, 1280)
                        frames.add(scaledBitmap)
                        Log.d(TAG, "Extracted frame $i at ${interval * i}ms")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error extracting frame $i", e)
                }
            }

        } catch (e: Exception) {
            Log.e(TAG, "Error setting up MediaMetadataRetriever", e)
        } finally {
            try {
                retriever.release()
            } catch (e: Exception) {
                Log.e(TAG, "Error releasing MediaMetadataRetriever", e)
            }
        }

        return frames
    }

    /**
     * Scale bitmap down if it exceeds max dimension to save bandwidth and memory.
     */
    private fun scaleBitmapIfNeeded(bitmap: Bitmap, maxDimension: Int): Bitmap {
        val width = bitmap.width
        val height = bitmap.height
        val maxSide = maxOf(width, height)

        if (maxSide <= maxDimension) {
            return bitmap
        }

        val scale = maxDimension.toFloat() / maxSide
        val newWidth = (width * scale).toInt()
        val newHeight = (height * scale).toInt()

        val scaled = Bitmap.createScaledBitmap(bitmap, newWidth, newHeight, true)
        if (scaled != bitmap) {
            bitmap.recycle()
        }
        return scaled
    }

    private suspend fun uploadFile(file: File, mimeType: String): String? {
        if (serverUrl.isEmpty()) return null

        val client = OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()

        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                "file",
                file.name,
                file.asRequestBody(mimeType.toMediaType())
            )
            .addFormDataPart("operator", currentOperator)
            .build()

        val request = Request.Builder()
            .url("$serverUrl/upload")
            .post(requestBody)
            .build()

        val response = client.newCall(request).execute()

        return if (response.isSuccessful) {
            val responseBody = response.body?.string()
            val json = JSONObject(responseBody ?: "{}")
            json.optString("url", null)
        } else {
            null
        }
    }

    private fun openPortal() {
        val intent = Intent(this, PairingActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        startActivity(intent)

        // Collapse overlay
        toggleExpanded()
    }

    private fun showToast(message: String) {
        handler.post {
            Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
        }
    }

    // =========================================================================
    // REALTIME MODE IMPLEMENTATION
    // =========================================================================

    private fun setupModelSpinner() {
        val models = arrayOf("Gemini Live", "GPT Realtime", "Grok Live")
        val adapter = ArrayAdapter(this, R.layout.spinner_item_dark, models)
        adapter.setDropDownViewResource(R.layout.spinner_dropdown_dark)
        modelSpinner?.adapter = adapter

        modelSpinner?.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                onModelSelected(position)
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }

        // Setup voice spinner with default voices
        setupVoiceSpinner()
    }

    private fun setupVoiceSpinner() {
        val voices = when (currentRealtimeModel) {
            RealtimeModel.GEMINI_LIVE -> geminiVoices
            RealtimeModel.GPT_REALTIME -> gptVoices
            RealtimeModel.GROK_LIVE -> grokVoices
            else -> geminiVoices
        }

        val adapter = ArrayAdapter(this, R.layout.spinner_item_dark, voices)
        adapter.setDropDownViewResource(R.layout.spinner_dropdown_dark)
        voiceSpinner?.adapter = adapter

        voiceSpinner?.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                selectedVoice = voices[position]
                Log.d(TAG, "Voice selected: $selectedVoice")
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }
    }

    private fun onModelSelected(position: Int) {
        val newModel = when (position) {
            0 -> RealtimeModel.GEMINI_LIVE
            1 -> RealtimeModel.GPT_REALTIME
            2 -> RealtimeModel.GROK_LIVE
            else -> RealtimeModel.GEMINI_LIVE
        }

        if (newModel != currentRealtimeModel) {
            // Disconnect if connected
            if (isRealtimeConnected) {
                disconnectRealtime()
            }

            currentRealtimeModel = newModel
            Log.d(TAG, "Realtime model selected: $newModel")

            // Update UI visibility - show screen share/camera buttons for both realtime models
            // Both Gemini Live and GPT Realtime now support vision via video_frame messages
            val visionButtonsVisible = (newModel != RealtimeModel.NONE)
            btnScreenShareContainer?.visibility = if (visionButtonsVisible) View.VISIBLE else View.GONE
            btnCameraContainer?.visibility = if (visionButtonsVisible) View.VISIBLE else View.GONE

            // Update voice spinner for new model
            setupVoiceSpinner()

            updateRealtimeStatus("Ready")
        }
    }

    private fun setupRealtimeControls() {
        // Minimize button
        btnMinimize?.setOnClickListener {
            animateButtonClick(it)
            toggleExpanded()
        }

        // Connect button
        btnConnect?.setOnClickListener {
            animateButtonClick(it)
            if (isRealtimeConnected) {
                disconnectRealtime()
            } else {
                connectRealtime()
            }
        }

        // Mic button - toggle realtime audio streaming
        expandedPanel.findViewById<ImageButton>(R.id.btnMic)?.setOnClickListener {
            animateButtonClick(it)
            if (!isRealtimeConnected) {
                showToast("Connect first to use microphone")
                return@setOnClickListener
            }
            toggleRealtimeAudio()
        }

        // Screen share button (works for both Gemini Live and GPT Realtime)
        btnScreenShare?.setOnClickListener {
            animateButtonClick(it)
            if (!isRealtimeConnected) {
                showToast("Connect first")
                return@setOnClickListener
            }
            toggleScreenSharing()
        }

        // Camera button (opens native camera)
        btnCamera?.setOnClickListener {
            animateButtonClick(it)
            openCamera()
        }

        // Transcript header toggle
        transcriptHeader?.setOnClickListener {
            toggleTranscript()
        }
    }

    private fun connectRealtime() {
        if (serverUrl.isEmpty()) {
            showToast("Server URL not set")
            return
        }

        realtimeSessionId = UUID.randomUUID().toString()

        val endpoint = when (currentRealtimeModel) {
            RealtimeModel.GPT_REALTIME -> "/ws/realtime/$realtimeSessionId"
            RealtimeModel.GEMINI_LIVE -> "/ws/gemini-live/$realtimeSessionId"
            RealtimeModel.GROK_LIVE -> "/ws/grok-live/$realtimeSessionId"
            else -> return
        }

        // Build WebSocket URL
        val wsProtocol = if (serverUrl.startsWith("https")) "wss" else "ws"
        val baseUrl = serverUrl.replace("https://", "").replace("http://", "")
        val wsUrl = "$wsProtocol://$baseUrl$endpoint"

        Log.d(TAG, "Connecting to realtime: $wsUrl")
        updateRealtimeStatus("Connecting...")
        if (isXrDevice) {
            OverlayBridge.updateState { it.copy(isConnecting = true) }
        } else {
            progressBar?.visibility = View.VISIBLE
        }

        val request = Request.Builder().url(wsUrl).build()
        realtimeWebSocket = realtimeHttpClient.newWebSocket(request, RealtimeWebSocketListener())
    }

    private fun disconnectRealtime() {
        Log.d(TAG, "Disconnecting realtime session")

        // Stop audio and screen sharing
        stopRealtimeAudioStreaming()
        stopScreenSharing()

        // Release audio playback
        releaseAudioTrack()

        // Reset mic state
        isUserMuted = false
        isAISpeaking = false
        wasMicActiveBeforeAI = false
        updateBubbleGlow()  // Clear bubble glow on disconnect

        // NOTE: Session transcript is saved by the BACKEND when WebSocket closes
        // (see gemini_live_routes.py save_session_to_blackbox)
        // Removed duplicate save here to prevent two snapshots from same session
        // saveSessionToBlackBox()

        // Close WebSocket
        realtimeWebSocket?.close(1000, "User disconnected")
        realtimeWebSocket = null

        isRealtimeConnected = false
        updateRealtimeUI()
        updateRealtimeStatus("Disconnected")
    }

    private inner class RealtimeWebSocketListener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            Log.d(TAG, "Realtime WebSocket opened")
            handler.post {
                isRealtimeConnected = true
                updateRealtimeUI()
                updateRealtimeStatus("Connected")
                if (isXrDevice) {
                    OverlayBridge.updateState { it.copy(isConnecting = false) }
                } else {
                    progressBar?.visibility = View.GONE
                }

                // Send connect message with selected voice
                val connectMsg = JSONObject().apply {
                    put("type", "connect")
                    put("operator", currentOperator)
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
                isRealtimeConnected = false
                updateRealtimeUI()
                updateRealtimeStatus("Disconnected")
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.e(TAG, "Realtime WebSocket failed", t)
            handler.post {
                isRealtimeConnected = false
                updateRealtimeUI()
                updateRealtimeStatus("Connection failed")
                progressBar?.visibility = View.GONE
                showToast("Connection failed: ${t.message}")
            }
        }
    }

    private fun handleRealtimeMessage(msg: JSONObject) {
        val type = msg.optString("type", "")
        val data = msg.optString("data", "")

        when (type) {
            "connected" -> {
                updateRealtimeStatus("Ready")
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
                    updateMicUI(isRealtimeRecording, aiSpeaking = true)
                    updateBubbleGlow()  // Update bubble glow state (blue for AI speaking)
                    updateRealtimeStatus("AI Speaking")

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
                updateLiveResponse(currentAIResponse.toString())
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
                showToast("Error: $data")
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
        audioPlaybackJob = coroutineScope.launch(Dispatchers.IO) {
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
                                updateRealtimeStatus("Connected")
                                if (!isUserMuted && !isRealtimeRecording && isRealtimeConnected) {
                                    Log.d(TAG, "Restarting mic after AI finished speaking")
                                    startRealtimeAudioStreaming()  // This calls updateBubbleGlow()
                                } else {
                                    updateMicUI(isRealtimeRecording, userMuted = isUserMuted, aiSpeaking = false)
                                    updateBubbleGlow()  // Update bubble glow when AI stops (and mic not restarting)
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
    private fun releaseAudioTrack() {
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

    // Counter for audio chunks (diagnostic)
    private var audioChunkCount = 0
    private var totalAudioBytesReceived = 0L

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
    private fun toggleScreenSharing() {
        if (isScreenSharing) {
            stopScreenSharing()
        } else {
            startScreenSharing()
        }
    }

    private fun startScreenSharing() {
        // Screen sharing now supported for both Gemini Live and GPT Realtime
        if (currentRealtimeModel == RealtimeModel.NONE) {
            showToast("Select a realtime model first")
            return
        }

        // Grok Voice API only supports audio/text - no vision capability
        if (currentRealtimeModel == RealtimeModel.GROK_LIVE) {
            showToast("Screen sharing not available - Grok is audio/text only")
            return
        }

        if (mediaProjection == null || mediaProjectionInvalidated) {
            showToast("Screen capture not available")
            return
        }

        // Ensure persistent display exists
        if (persistentImageReader == null) {
            setupPersistentScreenshotDisplay { success ->
                if (success) {
                    handler.post { startScreenShareCapture() }
                } else {
                    showToast("Failed to setup screen capture")
                }
            }
        } else {
            startScreenShareCapture()
        }
    }

    private fun startScreenShareCapture() {
        isScreenSharing = true
        updateScreenShareUI(true)
        syncBridgeState()

        screenShareJob = coroutineScope.launch {
            Log.d(TAG, "Starting screen share capture at 1 FPS")
            while (isActive && isScreenSharing) {
                captureAndSendFrame()
                delay(SCREEN_SHARE_INTERVAL_MS)
            }
        }
    }

    private fun stopScreenSharing() {
        isScreenSharing = false
        screenShareJob?.cancel()
        screenShareJob = null
        updateScreenShareUI(false)
        syncBridgeState()
        Log.d(TAG, "Screen sharing stopped")
    }

    private fun captureAndSendFrame() {
        if (!isRealtimeConnected || realtimeWebSocket == null) return

        val reader = persistentImageReader ?: return
        val image = reader.acquireLatestImage() ?: return

        try {
            val planes = image.planes
            val buffer = planes[0].buffer
            val pixelStride = planes[0].pixelStride
            val rowStride = planes[0].rowStride
            val rowPadding = rowStride - pixelStride * image.width

            // Create bitmap
            val bitmap = Bitmap.createBitmap(
                image.width + rowPadding / pixelStride,
                image.height,
                Bitmap.Config.ARGB_8888
            )
            bitmap.copyPixelsFromBuffer(buffer)

            // Crop to actual size if needed
            val croppedBitmap = if (rowPadding > 0) {
                Bitmap.createBitmap(bitmap, 0, 0, image.width, image.height)
            } else {
                bitmap
            }

            // Compress to JPEG
            val baos = ByteArrayOutputStream()
            croppedBitmap.compress(Bitmap.CompressFormat.JPEG, 70, baos)
            val base64Frame = Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP)

            // Send video frame
            val frameMsg = JSONObject().apply {
                put("type", "video_frame")
                put("data", base64Frame)
            }
            realtimeWebSocket?.send(frameMsg.toString())

            // Cleanup
            if (croppedBitmap != bitmap) {
                croppedBitmap.recycle()
            }
            bitmap.recycle()

        } catch (e: Exception) {
            Log.e(TAG, "Error capturing frame", e)
        } finally {
            image.close()
        }
    }

    private fun updateScreenShareUI(isActive: Boolean) {
        handler.post {
            btnScreenShare?.isActivated = isActive
            screenShareLabel?.text = if (isActive) "Sharing" else "Screen"
            screenShareLabel?.setTextColor(if (isActive) 0xFF44FF44.toInt() else 0xFF888888.toInt())
        }
    }

    // Camera launcher - opens native camera app as a shortcut
    private fun openCamera() {
        try {
            // Try to launch the default camera app directly
            val cameraIntent = Intent(Intent.ACTION_MAIN).apply {
                addCategory(Intent.CATEGORY_LAUNCHER)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }

            // Try to find the camera app
            val pm = packageManager
            val cameraApps = listOf(
                "com.android.camera",           // Stock Android
                "com.android.camera2",          // Stock Android 2
                "com.google.android.GoogleCamera",  // Google Camera
                "com.sec.android.app.camera",   // Samsung
                "com.huawei.camera",            // Huawei
                "com.oneplus.camera",           // OnePlus
                "com.oppo.camera",              // Oppo
                "com.miui.camera",              // Xiaomi
                "com.motorola.camera3"          // Motorola
            )

            var launched = false
            for (packageName in cameraApps) {
                try {
                    val launchIntent = pm.getLaunchIntentForPackage(packageName)
                    if (launchIntent != null) {
                        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                        startActivity(launchIntent)
                        launched = true
                        Log.d(TAG, "Launched camera app: $packageName")
                        break
                    }
                } catch (e: Exception) {
                    // Try next camera app
                }
            }

            // Fallback: try generic camera intent
            if (!launched) {
                try {
                    val fallbackIntent = Intent(MediaStore.INTENT_ACTION_STILL_IMAGE_CAMERA)
                    fallbackIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    startActivity(fallbackIntent)
                    launched = true
                    Log.d(TAG, "Launched camera via STILL_IMAGE_CAMERA intent")
                } catch (e: Exception) {
                    Log.w(TAG, "STILL_IMAGE_CAMERA intent failed", e)
                }
            }

            // Last fallback: video camera
            if (!launched) {
                val videoIntent = Intent(MediaStore.INTENT_ACTION_VIDEO_CAMERA)
                videoIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                startActivity(videoIntent)
                launched = true
                Log.d(TAG, "Launched camera via VIDEO_CAMERA intent")
            }

            if (launched) {
                if (isScreenSharing) {
                    showToast("Camera opened - AI can see your screen")
                } else {
                    showToast("Tip: Enable screen share so AI can see the camera")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to open camera", e)
            showToast("Failed to open camera app")
        }
    }

    // Realtime audio streaming
    private fun toggleRealtimeAudio() {
        // This is user-initiated mute/unmute
        if (isUserMuted) {
            // User is currently muted, wants to unmute
            isUserMuted = false
            if (!isAISpeaking) {
                startRealtimeAudioStreaming()
                Log.d(TAG, "User unmuted microphone")
            } else {
                // AI is speaking, will auto-resume when AI finishes
                updateMicUI(false, aiSpeaking = true)
                Log.d(TAG, "User unmuted, will resume when AI finishes")
            }
        } else if (isRealtimeRecording) {
            // User is listening, wants to mute
            isUserMuted = true
            stopRealtimeAudioStreaming()
            updateMicUI(false, userMuted = true)
            Log.d(TAG, "User muted microphone")
        } else {
            // Not recording, not muted - start recording
            isUserMuted = false
            startRealtimeAudioStreaming()
            Log.d(TAG, "User started microphone")
        }
    }

    // Pause mic temporarily while AI is speaking (auto-mute for feedback prevention)
    private fun pauseMicForAI() {
        if (!isRealtimeRecording) return

        Log.d(TAG, "Auto-muting mic while AI speaks")
        isAISpeaking = true

        // Fully stop and release AudioRecord to prevent resource exhaustion
        try {
            realtimeAudioJob?.cancel()
            realtimeAudioJob = null
            realtimeAudioRecord?.stop()
            realtimeAudioRecord?.release()
            realtimeAudioRecord = null
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping audio record", e)
        }

        isRealtimeRecording = false
        updateMicUI(false, aiSpeaking = true)

        // Transcribe the buffered user audio with Whisper - ONLY for Gemini Live
        // GPT Realtime already provides user_transcript via OpenAI's built-in Whisper
        if (currentRealtimeModel == RealtimeModel.GEMINI_LIVE) {
            transcribeRealtimeAudioBuffer()
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
        coroutineScope.launch {
            try {
                transcribeRealtimeWithWhisper(combinedAudio, sampleRate)
            } catch (e: Exception) {
                Log.e(TAG, "Error transcribing realtime audio", e)
            }
        }
    }

    // Transcribe realtime audio and add result to transcript
    private suspend fun transcribeRealtimeWithWhisper(audioData: ByteArray, sampleRate: Int) {
        if (serverUrl.isEmpty()) {
            Log.e(TAG, "No server URL for transcription")
            return
        }

        try {
            // Save audio to temp WAV file
            val audioFile = File(cacheDir, "realtime_input_${System.currentTimeMillis()}.wav")
            writeWavFileWithSampleRate(audioFile, audioData, sampleRate)
            Log.d(TAG, "Realtime WAV saved: ${audioFile.absolutePath}, size=${audioFile.length()}")

            val client = OkHttpClient.Builder()
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

    // Write WAV file with specific sample rate (for realtime audio)
    private fun writeWavFileWithSampleRate(file: File, audioData: ByteArray, sampleRate: Int) {
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

    // Resume mic after AI finishes speaking
    private fun resumeMicAfterAI() {
        isAISpeaking = false

        // Only resume if user hasn't manually muted
        if (isUserMuted) {
            Log.d(TAG, "AI finished speaking, mic stays muted (user muted)")
            updateMicUI(false, userMuted = true)
            return
        }

        if (!isRealtimeConnected) {
            Log.d(TAG, "AI finished speaking, not connected")
            return
        }

        Log.d(TAG, "AI finished speaking, resuming mic")
        startRealtimeAudioStreaming()
    }

    private fun startRealtimeAudioStreaming() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED) {
            showToast("Microphone permission required")
            return
        }

        // Release existing AudioRecord if any (prevent resource leak)
        if (realtimeAudioRecord != null) {
            try {
                realtimeAudioJob?.cancel()
                realtimeAudioJob = null
                realtimeAudioRecord?.stop()
                realtimeAudioRecord?.release()
            } catch (e: Exception) {
                Log.w(TAG, "Error releasing old AudioRecord", e)
            }
            realtimeAudioRecord = null
        }

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

        try {
            realtimeAudioRecord = AudioRecord(
                android.media.MediaRecorder.AudioSource.VOICE_RECOGNITION,
                sampleRate,
                CHANNEL_CONFIG,
                AUDIO_FORMAT,
                bufferSize
            )

            if (realtimeAudioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                showToast("Failed to initialize audio")
                return
            }

            realtimeAudioRecord?.startRecording()
            isRealtimeRecording = true
            updateMicUI(true)
            updateBubbleGlow()  // Update bubble glow state (red for listening)

            // Clear the audio buffer for new recording session
            synchronized(realtimeAudioBufferLock) {
                realtimeAudioBuffer.clear()
            }

            realtimeAudioJob = coroutineScope.launch {
                val buffer = ShortArray(bufferSize / 2)
                while (isActive && isRealtimeRecording) {
                    val readCount = realtimeAudioRecord?.read(buffer, 0, buffer.size) ?: 0
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
            }

            Log.d(TAG, "Realtime audio streaming started at ${sampleRate}Hz")

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start audio streaming", e)
            showToast("Failed to start microphone")
        }
    }

    private fun stopRealtimeAudioStreaming() {
        isRealtimeRecording = false
        realtimeAudioJob?.cancel()
        realtimeAudioJob = null

        realtimeAudioRecord?.stop()
        realtimeAudioRecord?.release()
        realtimeAudioRecord = null

        updateMicUI(false)
        updateBubbleGlow()  // Update bubble glow state

        // Send audio commit to trigger response
        if (isRealtimeConnected) {
            val commitMsg = JSONObject().put("type", "audio_commit")
            realtimeWebSocket?.send(commitMsg.toString())
        }

        Log.d(TAG, "Realtime audio streaming stopped")
    }

    private fun updateMicUI(isActive: Boolean, userMuted: Boolean = false, aiSpeaking: Boolean = false) {
        if (isXrDevice) { syncBridgeState(); return }
        handler.post {
            val btnMic = expandedPanel.findViewById<ImageButton>(R.id.btnMic)

            // Set button visual states (don't use isEnabled - it blocks clicks!)
            btnMic?.isActivated = isActive  // Red pulsing when listening

            when {
                userMuted -> {
                    micLabel?.text = "Tap to unmute"
                    micLabel?.setTextColor(0xFFFF4444.toInt())  // Red
                    btnMic?.alpha = 0.4f  // Dim to show muted
                    btnMic?.isActivated = false
                }
                isActive -> {
                    micLabel?.text = "Listening..."
                    micLabel?.setTextColor(0xFF44FF44.toInt())  // Green
                    btnMic?.alpha = 1.0f
                }
                aiSpeaking -> {
                    micLabel?.text = "AI Speaking..."
                    micLabel?.setTextColor(0xFFFFAA00.toInt())  // Orange
                    btnMic?.alpha = 0.7f
                }
                else -> {
                    micLabel?.text = "Tap to talk"
                    micLabel?.setTextColor(0xFFAAAAAA.toInt())  // Gray
                    btnMic?.alpha = 1.0f
                }
            }

            Log.d(TAG, "Mic UI updated: active=$isActive, muted=$userMuted, aiSpeaking=$aiSpeaking")
        }
    }

    /**
     * Update bubble glow based on realtime session state.
     * - Red pulsing glow when listening (recording user audio)
     * - Dark blue pulsing glow when AI is speaking
     * - No glow when idle
     */
    private fun updateBubbleGlow() {
        if (isXrDevice) { syncBridgeState(); return }
        handler.post {
            val glow = bubbleGlow ?: return@post

            // Cancel any existing animation
            bubbleGlowAnimator?.cancel()
            bubbleGlowAnimator = null

            when {
                isRealtimeRecording && !isAISpeaking -> {
                    // Listening state - red pulsing glow
                    glow.setBackgroundResource(R.drawable.bubble_glow_red)
                    glow.visibility = View.VISIBLE
                    startGlowPulseAnimation(glow)
                    Log.d(TAG, "Bubble glow: RED (listening)")
                }
                isAISpeaking -> {
                    // AI speaking state - dark blue pulsing glow
                    glow.setBackgroundResource(R.drawable.bubble_glow_blue)
                    glow.visibility = View.VISIBLE
                    startGlowPulseAnimation(glow)
                    Log.d(TAG, "Bubble glow: BLUE (AI speaking)")
                }
                else -> {
                    // Idle state - no glow
                    glow.visibility = View.GONE
                    Log.d(TAG, "Bubble glow: NONE (idle)")
                }
            }
        }
    }

    /**
     * Start pulsing animation on the bubble glow view.
     * Creates a smooth scale and alpha pulse effect.
     */
    private fun startGlowPulseAnimation(view: View) {
        val scaleX = ObjectAnimator.ofFloat(view, "scaleX", 1.0f, 1.15f, 1.0f).apply {
            duration = 1000
            repeatCount = ValueAnimator.INFINITE
            repeatMode = ValueAnimator.RESTART
        }
        val scaleY = ObjectAnimator.ofFloat(view, "scaleY", 1.0f, 1.15f, 1.0f).apply {
            duration = 1000
            repeatCount = ValueAnimator.INFINITE
            repeatMode = ValueAnimator.RESTART
        }
        val alpha = ObjectAnimator.ofFloat(view, "alpha", 1.0f, 0.6f, 1.0f).apply {
            duration = 1000
            repeatCount = ValueAnimator.INFINITE
            repeatMode = ValueAnimator.RESTART
        }

        bubbleGlowAnimator = AnimatorSet().apply {
            playTogether(scaleX, scaleY, alpha)
            start()
        }
    }

    // Transcript management
    private fun addTranscriptEntry(speaker: String, text: String) {
        if (text.isBlank()) return

        transcriptEntries.add(TranscriptEntry(speaker, text))

        if (isXrDevice) { syncBridgeState(); return }

        handler.post {
            // Create entry view
            val entryLayout = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(0, 8, 0, 8)
            }

            val speakerView = TextView(this).apply {
                this.text = speaker
                textSize = 10f
                setTextColor(if (speaker == "User") 0xFF4CAF50.toInt() else 0xFF2196F3.toInt())
            }

            val contentView = TextView(this).apply {
                this.text = text
                textSize = 12f
                setTextColor(0xFFCCCCCC.toInt())
            }

            entryLayout.addView(speakerView)
            entryLayout.addView(contentView)
            transcriptContainer?.addView(entryLayout)

            // Update count
            transcriptCount?.text = "${transcriptEntries.size} messages"

            // Show section if hidden
            if (transcriptSection?.visibility == View.GONE && transcriptEntries.isNotEmpty()) {
                transcriptSection?.visibility = View.VISIBLE
            }

            // Auto-scroll to bottom
            transcriptScroll?.post {
                transcriptScroll?.fullScroll(ScrollView.FOCUS_DOWN)
            }
        }
    }

    private fun updateLiveResponse(text: String) {
        if (isXrDevice) {
            OverlayBridge.updateState { it.copy(liveResponseText = text) }
            return
        }
        handler.post {
            liveResponseText?.text = text
            liveResponseText?.visibility = if (text.isNotEmpty()) View.VISIBLE else View.GONE
        }
    }

    private fun finalizeLiveResponse() {
        val response = currentAIResponse.toString()
        if (response.isNotBlank()) {
            addTranscriptEntry("AI", response)
        }
        currentAIResponse.clear()
        updateLiveResponse("")
    }

    private fun toggleTranscript() {
        isTranscriptExpanded = !isTranscriptExpanded
        transcriptScroll?.visibility = if (isTranscriptExpanded) View.VISIBLE else View.GONE
        transcriptExpandIcon?.rotation = if (isTranscriptExpanded) 180f else 0f
    }

    private fun saveSessionToBlackBox() {
        if (transcriptEntries.isEmpty()) {
            Log.d(TAG, "No transcript to save")
            return
        }

        val transcript = transcriptEntries.joinToString("\n\n") { entry ->
            "[${entry.speaker}]: ${entry.text}"
        }

        val modelName = when (currentRealtimeModel) {
            RealtimeModel.GPT_REALTIME -> "GPT-4o Realtime"
            RealtimeModel.GEMINI_LIVE -> "Gemini Live"
            else -> "Unknown"
        }

        val dateFormat = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
        val sessionSummary = """
=== $modelName Voice Session ===
Session ID: $realtimeSessionId
Operator: $currentOperator
Timestamp: ${dateFormat.format(Date())}
Messages: ${transcriptEntries.size}

--- Transcript ---
$transcript
--- End Session ---
        """.trimIndent()

        coroutineScope.launch {
            try {
                val client = OkHttpClient.Builder()
                    .connectTimeout(30, TimeUnit.SECONDS)
                    .readTimeout(30, TimeUnit.SECONDS)
                    .build()

                val body = JSONObject().apply {
                    put("operator", currentOperator)
                    put("messages", JSONArray().apply {
                        put(JSONObject().apply {
                            put("role", "user")
                            put("content", "[Voice Session Transcript]\n$sessionSummary")
                        })
                    })
                    put("provider", "google")
                    put("model", "gemini-2.5-pro")
                    put("streaming", false)
                    put("auto_checkpoint", false)
                }.toString()

                val request = Request.Builder()
                    .url("$serverUrl/chat")
                    .post(body.toRequestBody("application/json".toMediaType()))
                    .build()

                client.newCall(request).execute().use { response ->
                    if (response.isSuccessful) {
                        Log.d(TAG, "Session saved to BlackBox")
                        handler.post { showToast("Session saved") }
                    } else {
                        Log.e(TAG, "Failed to save session: ${response.code}")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error saving session", e)
            }
        }

        // Clear transcript for next session
        transcriptEntries.clear()
        handler.post {
            transcriptContainer?.removeAllViews()
            transcriptCount?.text = "0 messages"
        }
    }

    private fun updateRealtimeStatus(status: String) {
        if (isXrDevice) {
            OverlayBridge.updateState { it.copy(statusText = status) }
            return
        }
        handler.post {
            statusText?.text = status

            val color = when {
                status.contains("Connected") || status.contains("Ready") -> 0xFF44FF44.toInt()  // Green
                status.contains("Speaking") || status.contains("AI") -> 0xFFFFAA00.toInt()  // Orange
                status.contains("Disconnected") -> 0xFF666666.toInt()  // Gray
                status.contains("failed") || status.contains("Error") -> 0xFFFF4444.toInt()  // Red
                else -> 0xFF888888.toInt()  // Gray
            }

            statusText?.setTextColor(color)

            // Update status indicator circle color
            (statusIndicator?.background as? GradientDrawable)?.setColor(color)
        }
    }

    private fun updateRealtimeUI() {
        if (isXrDevice) { syncBridgeState(); return }
        handler.post {
            // Update connect button
            btnConnect?.setImageResource(
                if (isRealtimeConnected) android.R.drawable.ic_media_pause
                else android.R.drawable.ic_media_play
            )
            connectLabel?.text = if (isRealtimeConnected) "End" else "Connect"

            // Enable/disable controls based on connection state
            btnScreenShare?.isEnabled = isRealtimeConnected
            btnScreenShare?.alpha = if (isRealtimeConnected) 1.0f else 0.5f

            val btnMic = expandedPanel.findViewById<ImageButton>(R.id.btnMic)
            btnMic?.isEnabled = isRealtimeConnected
            btnMic?.alpha = if (isRealtimeConnected) 1.0f else 0.5f
        }
    }

    // =========================================================================
    // END REALTIME MODE IMPLEMENTATION
    // =========================================================================
}
