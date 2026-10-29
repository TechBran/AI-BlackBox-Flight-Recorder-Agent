package com.aiblackbox.portal.overlay

import android.animation.AnimatorSet
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.BitmapFactory
import android.graphics.drawable.GradientDrawable
import android.media.ImageReader
import android.media.MediaPlayer
import android.media.projection.MediaProjection
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Base64
import android.util.Log
import android.view.Gravity
import android.view.LayoutInflater
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
import com.aiblackbox.portal.PairingActivity
import com.aiblackbox.portal.R
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.HttpClients
import com.aiblackbox.portal.data.repository.TaskPoller
import com.aiblackbox.portal.data.remote.RemoteSessionBus
import com.aiblackbox.portal.data.store.OperatorPrompt
import com.aiblackbox.portal.data.store.OperatorResolver
import com.aiblackbox.portal.overlay.OverlayRealtimeSession.RealtimeModel
import kotlinx.coroutines.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import android.provider.MediaStore
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
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
                svc.capture.captureFramePngBytes(callback)
            }
        }
    }

    // ---- XR detection ----
    // (M6 / I1) Delegate to the SINGLE authoritative XR probe (DeviceCapabilities.isXr — UiMode
    // VR_HEADSET OR any XR_SYSTEM_FEATURE), the SAME probe that drives the wire device_capability
    // (detect().formFactor==xr_headset). Keying off one feature (xr.api.spatial) here would let a
    // headset get correct gating/capture-independence yet still run the PHONE overlay UI, so the
    // in-headset consent banner (M6.3) would silently never show.
    private val isXrDevice: Boolean by lazy {
        DeviceCapabilities.isXr(this)
    }

    private lateinit var windowManager: WindowManager
    private lateinit var overlayView: View
    private lateinit var expandedPanel: View
    private var bubbleGlow: View? = null
    private var bubbleGlowAnimator: AnimatorSet? = null
    private var isExpanded = false

    private var serverUrl: String = ""
    // The operator handed over by the starting screen ("" if it had none) until the box's
    // roster arrives (fetchOperators); never a seed name.
    private var currentOperator: String = ""
    private var operatorsList: List<String> = emptyList()

    private val handler = Handler(Looper.getMainLooper())
    private val coroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    // The MediaProjection, its persistent display, screenshots, frames and recording:
    // OverlayCapture (rank 76). What it shows or attaches comes back through CaptureHost.
    private val capture = OverlayCapture(this, handler, { windowManager }, CaptureHost())

    // The bubble and panel windows, the consent banner and the recording stop button:
    // OverlayWindows (rank 76). The bubble's and panel's contents stay here.
    private val windows = OverlayWindows(
        context = this,
        handler = handler,
        windowManager = { windowManager },
        isXrDevice = { isXrDevice },
        showToast = { showToast(it) },
        isRecording = { capture.isRecording },
        onStopRecording = { capture.stopRecording() },
    )

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

    // Chat state
    private val attachedScreenshots = mutableListOf<File>()
    private var attachedRecording: File? = null

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
    // REALTIME MODE - GPT Realtime, Gemini Live & Grok Live
    // =========================================================================

    // The live voice session (socket, playback, mic loop, screen share, transcript):
    // OverlayRealtimeSession (rank 76). The service shows what it reports (RealtimeHost).
    private val realtime = OverlayRealtimeSession(this, handler, coroutineScope, RealtimeHost())

    // Transcript panel state (the entries themselves are the session's)
    private var isTranscriptExpanded = false

    // Realtime UI elements
    private var modelSpinner: Spinner? = null
    private var voiceSpinner: Spinner? = null
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

        // (M5.4) Size the capture from the CURRENT WINDOW METRICS (API 30+), not the deprecated
        // defaultDisplay.getMetrics — so a tablet / unfolded Fold / multi-window captures at its
        // true resolution instead of a phone-narrow metric.
        capture.refreshScreenMetrics()

        createNotificationChannel()

        // Register XR bridge command handler if running on XR device
        if (isXrDevice) {
            OverlayBridge.registerCommandListener(XrCommandHandler())
        }

        // (M1.4) Observe the remote-control session bus so the "AI is controlling this
        // device" consent banner + kill switch show whenever a session is active.
        RemoteSessionBus.addListener(controlSessionListener)

        Log.d(TAG, "OverlayService created (isXrDevice=$isXrDevice)")
    }

    // ---- (M1.4) Remote-control consent banner + kill switch -----------------

    /**
     * Bus listener: show the banner while a session is active, hide it when it ends/is
     * killed. Marshalled onto the main thread ([handler]) because it may fire from the
     * server's worker thread. Never throws (windowManager churn is wrapped).
     */
    private val controlSessionListener = RemoteSessionBus.Listener { session ->
        handler.post {
            if (isXrDevice) {
                // (M6.3) XR: the TYPE_APPLICATION_OVERLAY banner doesn't surface in the 3D
                // compositor, so drive the in-headset panel banner via the bridge state instead.
                // showControlBanner()/hideControlBanner() are no-ops on XR (kept for symmetry).
                OverlayBridge.updateState { it.copy(controlSessionActive = session != null) }
            } else {
                if (session != null) windows.showControlBanner() else windows.hideControlBanner()
            }
        }
    }

    /**
     * Inner class that maps OverlayBridge commands from the XR Activity
     * to existing service methods. Only instantiated on XR devices.
     */
    private inner class XrCommandHandler : OverlayBridge.CommandListener {
        override fun toggleConnect() {
            handler.post {
                if (realtime.isRealtimeConnected) realtime.disconnectRealtime() else realtime.connectRealtime()
            }
        }
        override fun toggleMic() {
            handler.post { realtime.toggleRealtimeAudio() }
        }
        override fun toggleScreenShare() {
            handler.post { realtime.toggleScreenSharing() }
        }
        override fun toggleExpanded() {
            handler.post { this@OverlayService.toggleExpanded() }
        }
        override fun selectModel(model: String) {
            handler.post {
                realtime.currentRealtimeModel = when (model) {
                    "GPT Realtime" -> RealtimeModel.GPT_REALTIME
                    "Grok Live" -> RealtimeModel.GROK_LIVE
                    else -> RealtimeModel.GEMINI_LIVE
                }
                syncBridgeState()
            }
        }
        override fun selectVoice(voice: String) {
            handler.post {
                realtime.selectedVoice = voice
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
            handler.post { capture.takeScreenshot() }
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

        // (M6.3) The in-headset consent kill switch. Routes to the process-wide session bus,
        // which clears the active session (every surface drops its banner) AND records the kill
        // so any subsequent /action frame for the task is refused by PhoneActionDispatcher.
        // Fail-safe + idempotent; the bus listener flips controlSessionActive back to false.
        override fun stopRemoteControl() {
            RemoteSessionBus.stop()
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
        currentOperator = intent?.getStringExtra(EXTRA_OPERATOR)?.trim().orEmpty()

        // Initialize MediaProjection after foreground started
        if (pendingResultCode == Activity.RESULT_OK && pendingResultData != null) {
            capture.start(pendingResultCode, pendingResultData!!)
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

        // (M1.4) Stop observing the session bus + remove the consent banner if present.
        RemoteSessionBus.removeListener(controlSessionListener)
        windows.hideControlBanner()

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
            windows.removeAll()
        }

        // Cleanup resources
        capture.stopRecording()

        // Cleanup realtime mode resources (mic, share, playback, socket)
        realtime.shutdown()

        // Cleanup the persistent screenshot display and the projection (only now)
        capture.release()

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

    private fun fetchOperators() {
        if (serverUrl.isEmpty()) return

        coroutineScope.launch {
            try {
                val client = HttpClients.base.newBuilder()
                    .connectTimeout(10, TimeUnit.SECONDS)
                    .readTimeout(10, TimeUnit.SECONDS)
                    .build()

                val request = Request.Builder()
                    .url("$serverUrl/operators")
                    .get()
                    .build()

                val response = client.newCall(request).execute()

                if (response.isSuccessful) {
                    val roster = OperatorResolver.parseRoster(response.body?.string() ?: "")
                    if (roster != null && roster.operators.isNotEmpty()) {
                        // The hand-over operator while the box lists it, else the shared
                        // rule's pick, else the box default — shown in the spinner to change.
                        val start = OperatorResolver.forSpinner(currentOperator, roster)
                        withContext(Dispatchers.Main) {
                            operatorsList = roster.operators
                            currentOperator = start
                            setupOperatorSpinner(start)
                            syncBridgeState()  // the XR panel's picker (no spinner there)
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

    /** XR path: launch the transparent Activity that floats as a Home Space panel (plain Compose). */
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
                isConnected = realtime.isRealtimeConnected,
                isRecording = realtime.isRealtimeRecording,
                isAISpeaking = realtime.isAISpeaking,
                isScreenSharing = realtime.isScreenSharing,
                isExpanded = isExpanded,
                isUserMuted = realtime.isUserMuted,
                // Note: statusText TextView is null on XR (panel never inflated),
                // so we preserve whatever was set via updateRealtimeStatus()
                statusText = current.statusText,
                currentModel = when (realtime.currentRealtimeModel) {
                    RealtimeModel.GPT_REALTIME -> "GPT Realtime"
                    RealtimeModel.GROK_LIVE -> "Grok Live"
                    else -> "Gemini Live"
                },
                currentVoice = realtime.selectedVoice,
                currentOperator = currentOperator,
                voices = when (realtime.currentRealtimeModel) {
                    RealtimeModel.GPT_REALTIME -> realtime.gptVoices.toList()
                    RealtimeModel.GROK_LIVE -> realtime.grokVoices.toList()
                    else -> realtime.geminiVoices.toList()
                },
                operators = operatorsList,
                transcriptEntries = realtime.transcriptEntries.map {
                    OverlayBridge.TranscriptEntry(it.speaker, it.text, it.timestamp)
                },
                liveResponseText = realtime.currentAIResponse.toString()
            )
        }
    }

    private fun createFloatingBubble() {
        overlayView = LayoutInflater.from(this).inflate(R.layout.overlay_bubble, null)

        // Get reference to bubble glow view
        bubbleGlow = overlayView.findViewById(R.id.bubbleGlow)

        // Click to expand
        overlayView.setOnClickListener {
            toggleExpanded()
        }

        // On screen, draggable
        windows.addBubble(overlayView)
    }

    private fun createExpandedPanel() {
        expandedPanel = LayoutInflater.from(this).inflate(R.layout.overlay_expanded, null)

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

        expandedPanel.visibility = View.GONE

        windows.addPanel(expandedPanel, dragHandle)
    }

    private fun setupExpandedPanelButtons() {
        // Screenshot button container - click on entire button area
        expandedPanel.findViewById<View>(R.id.btnScreenshotContainer)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "Screenshot button clicked")
            showToast("Taking screenshot...")
            capture.takeScreenshot()
        }

        // Record button container - toggles recording
        expandedPanel.findViewById<View>(R.id.btnRecordContainer)?.setOnClickListener {
            animateButtonClick(it)
            Log.d(TAG, "Record button clicked")
            capture.toggleRecording()
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
                windows.updatePanelForKeyboard(true)
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
                val client = HttpClients.base.newBuilder()
                    .connectTimeout(30, TimeUnit.SECONDS)
                    .writeTimeout(60, TimeUnit.SECONDS)
                    .readTimeout(60, TimeUnit.SECONDS)
                    .build()

                // Use the /tts endpoint - returns raw audio data. No voice or
                // model: the box speaks its default (GET /tts/catalog "default")
                // with that provider's configured model, so the overlay works on
                // a box without an OpenAI key instead of pinning onyx/tts-1-hd.
                val requestBody = JSONObject().apply {
                    put("text", ttsText)
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

    // Screenshots, frames, recording and the projection refresh live in OverlayCapture.

    /**
     * What capture shows and attaches through the overlay: the status line, the bubble and
     * panel it hides from the frame, the screenshot and recording attachments, the floating
     * recording stop button, and the service stop a projection refresh needs.
     */
    private inner class CaptureHost : OverlayCapture.Host {
        override fun updateStatus(status: String) = this@OverlayService.updateStatus(status)
        override fun showToast(message: String) = this@OverlayService.showToast(message)
        override fun hideOverlayForCapture(): Boolean {
            val wasExpanded = isExpanded
            overlayView.visibility = View.GONE
            expandedPanel.visibility = View.GONE
            return wasExpanded
        }
        override fun restoreOverlayAfterCapture(wasExpanded: Boolean) {
            if (wasExpanded) {
                expandedPanel.visibility = View.VISIBLE
            } else {
                overlayView.visibility = View.VISIBLE
            }
        }
        override fun onScreenshot(file: File?) {
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
        override fun onRecordingStarted() {
            overlayView.visibility = View.GONE
            expandedPanel.visibility = View.GONE
            windows.showRecordingStopButton()
        }
        override fun onRecordingSaved(file: File) {
            attachedRecording?.delete()
            attachedRecording = file
            updateAttachmentCount()
            showToast("Recording saved!")
        }
        override fun onRecordingStopped() {
            windows.hideRecordingStopButton()

            overlayView.visibility = View.GONE
            expandedPanel.visibility = View.VISIBLE
            isExpanded = true
        }
        override fun stopService() = stopSelf()
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

    /**
     * True (and says so) while no operator is resolved: the starting screen handed none
     * over and the box's roster has not filled the operator spinner (unreachable, or it
     * lists nobody). A prompt or a realtime session as "" would run as no one.
     */
    private fun operatorBlocks(): Boolean {
        OperatorResolver.sendBlockedReason(currentOperator, OperatorPrompt.CHOOSE) ?: return false
        showToast("Choose an operator first: pick one in this panel's operator menu.")
        return true
    }

    private fun sendPromptToAI() {
        val prompt = promptInput?.text?.toString()?.trim() ?: ""

        if (prompt.isEmpty() && attachedScreenshots.isEmpty() && attachedRecording == null) {
            showToast("Enter a prompt or attach screenshots/recording")
            return
        }
        if (operatorBlocks()) return

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

        val client = HttpClients.base.newBuilder()
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

        // Step 2: Poll for task completion through the shared TaskPoller (rank 76): a
        // blip is retried, 'cancelled' ends the wait, the box's error_message is shown.
        withContext(Dispatchers.Main) {
            updateStatus("Processing...")
        }
        val outcome = withTimeoutOrNull(OverlayChatReply.CEILING_MS) {
            TaskPoller(BlackBoxApi(serverUrl)).await(taskId, intervalMs = 1000) { s ->
                Log.d(TAG, "Task $taskId status: ${s.status}")
                if (s.status == "processing" || s.status == "pending") {
                    handler.post { updateStatus("Processing... ${s.progress}%") }
                }
            }
        }
        if (outcome == null) {
            Log.e(TAG, "Task polling timeout")
            return "Request timed out"
        }
        return OverlayChatReply.textFor(outcome)
    }

    private fun encodeFileToBase64(file: File): String {
        val bytes = file.readBytes()
        return Base64.encodeToString(bytes, Base64.NO_WRAP)
    }

    private suspend fun uploadFile(file: File, mimeType: String): String? {
        if (serverUrl.isEmpty()) return null

        val client = HttpClients.base.newBuilder()
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
        val voices = realtime.voicesForModel()

        val adapter = ArrayAdapter(this, R.layout.spinner_item_dark, voices)
        adapter.setDropDownViewResource(R.layout.spinner_dropdown_dark)
        voiceSpinner?.adapter = adapter

        voiceSpinner?.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                realtime.selectedVoice = voices[position]
                Log.d(TAG, "Voice selected: ${realtime.selectedVoice}")
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

        if (newModel != realtime.currentRealtimeModel) {
            // Disconnect if connected
            if (realtime.isRealtimeConnected) {
                realtime.disconnectRealtime()
            }

            realtime.currentRealtimeModel = newModel
            Log.d(TAG, "Realtime model selected: $newModel")

            // Every realtime model shows the screen share/camera buttons: Gemini Live and GPT
            // Realtime take video_frame messages (startScreenSharing refuses Grok, audio only).
            btnScreenShareContainer?.visibility = View.VISIBLE
            btnCameraContainer?.visibility = View.VISIBLE

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
            if (realtime.isRealtimeConnected) {
                realtime.disconnectRealtime()
            } else {
                realtime.connectRealtime()
            }
        }

        // Mic button - toggle realtime audio streaming
        expandedPanel.findViewById<ImageButton>(R.id.btnMic)?.setOnClickListener {
            animateButtonClick(it)
            if (!realtime.isRealtimeConnected) {
                showToast("Connect first to use microphone")
                return@setOnClickListener
            }
            realtime.toggleRealtimeAudio()
        }

        // Screen share button (works for both Gemini Live and GPT Realtime)
        btnScreenShare?.setOnClickListener {
            animateButtonClick(it)
            if (!realtime.isRealtimeConnected) {
                showToast("Connect first")
                return@setOnClickListener
            }
            realtime.toggleScreenSharing()
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

    /**
     * What the live voice session shows and reads through the overlay: the phone panel's
     * views, or the XR bridge; and the screen frames it shares, from the persistent display.
     */
    private inner class RealtimeHost : OverlayRealtimeSession.Host {
        override val serverUrl: String get() = this@OverlayService.serverUrl
        override val operator: String get() = currentOperator
        override fun operatorBlocks(): Boolean = this@OverlayService.operatorBlocks()
        override fun showToast(message: String) = this@OverlayService.showToast(message)
        override fun updateStatus(status: String) = updateRealtimeStatus(status)
        override fun updateRealtimeUi() = updateRealtimeUI()
        override fun updateMicUi(isActive: Boolean, userMuted: Boolean, aiSpeaking: Boolean) =
            updateMicUI(isActive, userMuted, aiSpeaking)
        override fun updateBubbleGlow() = this@OverlayService.updateBubbleGlow()
        override fun updateScreenShareUi(isActive: Boolean) = updateScreenShareUI(isActive)
        override fun syncBridgeState() = this@OverlayService.syncBridgeState()
        override fun showConnecting(connecting: Boolean) {
            if (isXrDevice) {
                OverlayBridge.updateState { it.copy(isConnecting = connecting) }
            } else {
                progressBar?.visibility = if (connecting) View.VISIBLE else View.GONE
            }
        }
        override fun hideProgress() {
            progressBar?.visibility = View.GONE
        }
        override fun showTranscriptEntry(speaker: String, text: String, count: Int) =
            this@OverlayService.showTranscriptEntry(speaker, text, count)
        override fun updateLiveResponse(text: String) = this@OverlayService.updateLiveResponse(text)
        override fun screenCaptureAvailable(): Boolean = capture.available()
        override fun ensureScreenFrames(onReady: (Boolean) -> Unit) {
            if (!capture.hasPersistentReader()) {
                capture.setupPersistentScreenshotDisplay { success ->
                    if (success) handler.post { onReady(true) } else onReady(false)
                }
            } else {
                onReady(true)
            }
        }
        override fun latestScreenFrameJpegBase64(): String? = capture.latestFrameJpegBase64()
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
                if (realtime.isScreenSharing) {
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
                realtime.isRealtimeRecording && !realtime.isAISpeaking -> {
                    // Listening state - red pulsing glow
                    glow.setBackgroundResource(R.drawable.bubble_glow_red)
                    glow.visibility = View.VISIBLE
                    startGlowPulseAnimation(glow)
                    Log.d(TAG, "Bubble glow: RED (listening)")
                }
                realtime.isAISpeaking -> {
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
    /** Show a transcript line the session added ([count] lines so far). */
    private fun showTranscriptEntry(speaker: String, text: String, count: Int) {
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
            transcriptCount?.text = "$count messages"

            // Show section if hidden
            if (transcriptSection?.visibility == View.GONE && count > 0) {
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

    private fun toggleTranscript() {
        isTranscriptExpanded = !isTranscriptExpanded
        transcriptScroll?.visibility = if (isTranscriptExpanded) View.VISIBLE else View.GONE
        transcriptExpandIcon?.rotation = if (isTranscriptExpanded) 180f else 0f
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
                if (realtime.isRealtimeConnected) android.R.drawable.ic_media_pause
                else android.R.drawable.ic_media_play
            )
            connectLabel?.text = if (realtime.isRealtimeConnected) "End" else "Connect"

            // Enable/disable controls based on connection state
            btnScreenShare?.isEnabled = realtime.isRealtimeConnected
            btnScreenShare?.alpha = if (realtime.isRealtimeConnected) 1.0f else 0.5f

            val btnMic = expandedPanel.findViewById<ImageButton>(R.id.btnMic)
            btnMic?.isEnabled = realtime.isRealtimeConnected
            btnMic?.alpha = if (realtime.isRealtimeConnected) 1.0f else 0.5f
        }
    }

    // =========================================================================
    // END REALTIME MODE IMPLEMENTATION
    // =========================================================================
}
