package com.aiblackbox.portal

import android.Manifest
import android.app.Activity
import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.OpenableColumns
import android.provider.Settings
import android.util.Base64
import android.util.Log
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.URLUtil
import android.widget.Button
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import androidx.core.net.toUri
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.aiblackbox.portal.overlay.FoldingFeatureMonitor
import com.aiblackbox.portal.overlay.OverlayService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import org.json.JSONObject
import java.io.File
import java.io.IOException
import kotlin.math.roundToInt

class PortalActivity : AppCompatActivity() {

    private val prefs by lazy { getSharedPreferences("bbx_prefs", MODE_PRIVATE) }
    private val isXrDevice: Boolean by lazy {
        packageManager.hasSystemFeature("android.software.xr.api.spatial")
    }
    private lateinit var web: WebView
    private lateinit var retryPanel: View
    private lateinit var notificationManager: BlackBoxNotificationManager
    private var mediaRecorder: MediaRecorder? = null
    private var audioFile: File? = null
    private var isRecording = false
    private var wakeLock: PowerManager.WakeLock? = null
    private val TAG = "BlackBoxBridge"

    // Audio streaming for Realtime API
    private var audioRecord: AudioRecord? = null
    private var isStreaming = false
    private var streamingThread: Thread? = null

    // Track recording mode for Android 14+ MediaProjection refresh
    private var pendingRecordingMode = false

    // BroadcastReceiver for MediaProjection refresh requests from OverlayService
    private val mediaProjectionRefreshReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action == OverlayService.ACTION_REFRESH_PROJECTION) {
                // Get recording mode flag from the broadcast
                pendingRecordingMode = intent.getBooleanExtra(OverlayService.EXTRA_RECORDING_MODE, false)
                Log.d(TAG, "Received MediaProjection refresh request (recordingMode=$pendingRecordingMode) - restarting overlay")
                // Small delay to let OverlayService fully stop
                web.postDelayed({
                    requestOverlayPermission()
                }, 1000)
            }
        }
    }

    private val micPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (!granted) {
            Toast.makeText(this, "Microphone permission is required for voice input.", Toast.LENGTH_LONG).show()
        }
    }

    private val notificationPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            notificationManager.showTaskNotification("BlackBox Ready", "Notifications are enabled!")
        } else {
            Toast.makeText(this, "Notification permission is recommended for alerts.", Toast.LENGTH_LONG).show()
        }
    }

    private val fileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            result.data?.data?.let { uri ->
                handleFileSelection(uri)
            }
        }
    }

    // Overlay permission request result
    private val overlayPermLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { _ ->
        if (hasOverlayPermission()) {
            // Now request screen capture permission
            requestScreenCapturePermission()
        } else {
            Toast.makeText(this, "Overlay permission required for floating bubble", Toast.LENGTH_LONG).show()
        }
    }

    // MediaProjection permission launcher
    private val mediaProjectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            startOverlayService(result.resultCode, result.data!!)
        } else {
            Toast.makeText(this, "Screen capture permission denied", Toast.LENGTH_SHORT).show()
        }
    }

    private fun hasMicPerm(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED

    private fun ensureMicPermission() {
        if (!hasMicPerm()) {
            micPermLauncher.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun hasNotificationPerm(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
                    PackageManager.PERMISSION_GRANTED
        } else {
            true // Notifications don't require runtime permission on Android 12 and below
        }
    }

    private fun ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (!hasNotificationPerm()) {
                notificationPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    // ============ OVERLAY PERMISSIONS ============

    private fun hasOverlayPermission(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Settings.canDrawOverlays(this)
        } else {
            true
        }
    }

    private fun requestOverlayPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !hasOverlayPermission()) {
            val intent = Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:$packageName")
            )
            overlayPermLauncher.launch(intent)
        } else {
            // Permission already granted, proceed to screen capture
            requestScreenCapturePermission()
        }
    }

    private fun requestScreenCapturePermission() {
        val projectionManager = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        mediaProjectionLauncher.launch(projectionManager.createScreenCaptureIntent())
    }

    private fun startOverlayService(resultCode: Int, resultData: Intent) {
        val origin = prefs.getString("origin", "") ?: ""
        val baseUrl = normalizeOrigin(origin).removeSuffix("/ui/")

        val intent = Intent(this, OverlayService::class.java).apply {
            putExtra(OverlayService.EXTRA_RESULT_CODE, resultCode)
            putExtra(OverlayService.EXTRA_RESULT_DATA, resultData)
            putExtra(OverlayService.EXTRA_SERVER_URL, baseUrl)
            putExtra(OverlayService.EXTRA_OPERATOR, "Brandon")
            putExtra(OverlayService.EXTRA_RECORDING_MODE, pendingRecordingMode)  // Android 14+ fix
        }

        // Reset the flag after using it
        pendingRecordingMode = false

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }

        if (isXrDevice) {
            Toast.makeText(this, "BlackBox spatial overlay started", Toast.LENGTH_SHORT).show()
            // Minimize Portal so only the floating overlay panel is visible
            moveTaskToBack(true)
        } else {
            Toast.makeText(this, "BlackBox overlay started", Toast.LENGTH_SHORT).show()
        }
    }

    private fun stopOverlayService() {
        val intent = Intent(this, OverlayService::class.java).apply {
            action = OverlayService.ACTION_STOP
        }
        startService(intent)
        Toast.makeText(this, "BlackBox overlay stopped", Toast.LENGTH_SHORT).show()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val origin = prefs.getString("origin", null)
        if (origin.isNullOrBlank()) {
            startActivity(Intent(this, PairingActivity::class.java))
            finish(); return
        }

        setContentView(R.layout.activity_portal)

        // (M5.5 / I1) Drive the foldable POSTURE monitor from repeatOnLifecycle(STARTED): the
        // collection re-arms on every STARTED and is cancelled below STARTED. This is load-bearing
        // on a Fold, where the cover↔main display switch RECREATES this Activity (the prior
        // lifecycleScope — and its collector — is cancelled on onDestroy); a once-only start() in
        // onCreate would leave posture tracking dead after the first recreation. The monitor's
        // start() is idempotent per scope (cancels any prior collector — no double-collection). It
        // populates the process-wide FoldingFeatureMonitor the device-control observation reads
        // (formFactor=foldable + posture + the posture-change re-observe flag). Graceful no-op on a
        // non-foldable / when androidx.window can't attach.
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                runCatching { FoldingFeatureMonitor.instance.start(this@PortalActivity, this) }
            }
        }

        // Enable edge-to-edge display (transparent status and navigation bars)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            // Android 11+ modern approach
            window.setDecorFitsSystemWindows(false)
            window.statusBarColor = android.graphics.Color.TRANSPARENT
            window.navigationBarColor = android.graphics.Color.TRANSPARENT
        } else {
            // Android 10 and below
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or android.view.View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            )
            window.statusBarColor = android.graphics.Color.TRANSPARENT
            window.navigationBarColor = android.graphics.Color.TRANSPARENT
        }

        web = findViewById(R.id.portalWeb)
        retryPanel = findViewById(R.id.retryPanel)
        notificationManager = BlackBoxNotificationManager(this)

        ensureMicPermission()
        ensureNotificationPermission()
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true)
        }

        // Register for MediaProjection refresh broadcasts from OverlayService (Android 14+ auto-refresh)
        val refreshFilter = IntentFilter(OverlayService.ACTION_REFRESH_PROJECTION)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(mediaProjectionRefreshReceiver, refreshFilter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(mediaProjectionRefreshReceiver, refreshFilter)
        }

        // Start Foreground Service to keep app alive
        startKeepAliveService()
        // Acquire WakeLock
        acquireWakeLock()

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            mediaPlaybackRequiresUserGesture = false
        }

        val webAppInterface = WebAppInterface()
        // Primary Bridge for Notifications
        web.addJavascriptInterface(webAppInterface, "AndroidBridge")
        // Legacy/Other Bridges
        web.addJavascriptInterface(webAppInterface, "AndroidMic")
        web.addJavascriptInterface(FilePickerInterface(), "AndroidFilePicker")
        // Generic "Android" interface for downloadFile and other JS calls expecting window.Android
        web.addJavascriptInterface(webAppInterface, "Android")

        // Use custom OkHttp download instead of Android DownloadManager
        // DownloadManager runs outside VPN tunnel and has issues with Android 10+ storage
        web.setDownloadListener { url, _, contentDisposition, mimetype, _ ->
            try {
                val fileName = URLUtil.guessFileName(url, contentDisposition, mimetype)
                Log.d(TAG, "Download triggered via setDownloadListener: $fileName from $url")
                // Delegate to our OkHttp-based download function that works with VPN and MediaStore
                webAppInterface.downloadFile(url, fileName)
            } catch (e: Exception) {
                Toast.makeText(applicationContext, "Error setting up download: ${e.message}", Toast.LENGTH_LONG).show()
                e.printStackTrace()
            }
        }

        web.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?,
                req: WebResourceRequest?,
                err: WebResourceError?
            ) {
                if (req?.isForMainFrame == true) {
                    runOnUiThread {
                        retryPanel.visibility = View.VISIBLE
                    }
                    Log.e("WebViewError", "Error loading main frame: ${err?.description} (Code: ${err?.errorCode}) URL: ${req?.url}")
                }
                super.onReceivedError(view, req, err)
            }
            
            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                // Inject flags to help JS detection
                view?.evaluateJavascript("window.isAndroidWebWrapper = true;", null)
                Log.d(TAG, "Page finished loading: $url")
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    val wantsAudio = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                    val isSecureOrigin = request.origin?.toString()?.startsWith("https://") == true ||
                            request.origin?.toString()?.startsWith("http://localhost") == true ||
                            request.origin?.toString()?.contains(".ts.net") == true

                    if (wantsAudio && isSecureOrigin && hasMicPerm()) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    } else {
                        Log.w("PermissionRequest", "Denying permission. wantsAudio=$wantsAudio, isSecure=$isSecureOrigin, hasMicPerm=${hasMicPerm()}")
                        request.deny()
                    }
                }
            }

            override fun onConsoleMessage(consoleMessage: android.webkit.ConsoleMessage?): Boolean {
                consoleMessage?.let {
                    Log.d("WebViewConsole", "${it.message()} -- From line ${it.lineNumber()} of ${it.sourceId()}")
                }
                return super.onConsoleMessage(consoleMessage)
            }
        }

        findViewById<Button>(R.id.btnRetry).setOnClickListener { loadPortal(origin) }
        findViewById<Button>(R.id.btnRescan).setOnClickListener {
            prefs.edit { remove("origin") }
            startActivity(Intent(this, PairingActivity::class.java))
            finish()
        }
        findViewById<Button>(R.id.btnOpenTailscale).setOnClickListener {
            val i = packageManager.getLaunchIntentForPackage("com.tailscale.ipn")
            if (i != null) startActivity(i) else Toast.makeText(this, "Install Tailscale", Toast.LENGTH_SHORT).show()
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) {
                    web.goBack()
                } else {
                    finish()
                }
            }
        })

        loadPortal(origin)
    }

    private fun startKeepAliveService() {
        val intent = Intent(this, KeepAliveService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    private fun stopKeepAliveService() {
        val intent = Intent(this, KeepAliveService::class.java)
        stopService(intent)
    }

    private fun acquireWakeLock() {
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "BlackBoxPortal:WakeLock")
        wakeLock?.acquire(10 * 60 * 1000L /*10 minutes*/)
    }

    private fun releaseWakeLock() {
        if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        val contentHeight = web.contentHeight * web.scale
        val scrollY = web.scrollY
        val scrollRatio = if (contentHeight > 0) scrollY / contentHeight else 0f

        web.postDelayed({
            val newContentHeight = web.contentHeight * web.scale
            if (newContentHeight > 0) {
                val newScrollY = (newContentHeight * scrollRatio).roundToInt()
                web.scrollTo(web.scrollX, newScrollY)
                Log.d("ScrollPreserve", "Restored scroll to $newScrollY (Ratio: $scrollRatio)")
            }
        }, 100)
    }

    private fun normalizeOrigin(url: String): String {
        var u = url.trim()
        val tsNet = Regex("https?://[^/]*\\.ts\\.net", RegexOption.IGNORE_CASE)
        val isTsNet = tsNet.containsMatchIn(u)

        if (isTsNet) {
            u = u.replaceFirst(Regex("^http://", RegexOption.IGNORE_CASE), "https://")
            u = u.replace(Regex("^(https://[^/]+):\\d+(?=/|$)", RegexOption.IGNORE_CASE), "$1")
        }

        if (!u.endsWith("/ui/")) {
            u = u.removeSuffix("/")
            u = "$u/ui/"
        }

        Log.d("NormalizeOrigin", "Input: $url, Output: $u")
        return u
    }

    private fun loadPortal(origin: String) {
        val normalizedUrl = normalizeOrigin(origin)
        Log.d("LoadPortal", "Loading URL: $normalizedUrl")
        retryPanel.visibility = View.GONE
        web.loadUrl(normalizedUrl)
    }

    private fun handleFileSelection(uri: Uri) {
        val mimeType = contentResolver.getType(uri) ?: "application/octet-stream"

        var fileName = "uploaded_file"
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (nameIndex != -1) {
                    fileName = cursor.getString(nameIndex)
                }
            }
        }

        // Check file size
        var fileSize = 0L
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (sizeIndex != -1) {
                    fileSize = cursor.getLong(sizeIndex)
                }
            }
        }

        val maxUploadSize = 500L * 1024 * 1024 // 500MB - matches server limit
        if (fileSize > maxUploadSize) {
            val sizeMB = fileSize / (1024 * 1024)
            Toast.makeText(this, "File too large: ${sizeMB}MB. Maximum is 500MB.", Toast.LENGTH_LONG).show()
            return
        }

        // For small files (<5MB), use the legacy Base64 path (fast, no network hop)
        if (fileSize in 1..5_000_000) {
            handleFileSelectionBase64(uri, mimeType, fileName)
            return
        }

        // For large files, upload directly from Kotlin via OkHttp (streaming, low memory)
        handleFileSelectionDirectUpload(uri, mimeType, fileName)
    }

    /** Legacy path for small files - Base64 encode and pass to JS */
    private fun handleFileSelectionBase64(uri: Uri, mimeType: String, fileName: String) {
        try {
            val inputStream = contentResolver.openInputStream(uri)
            val bytes = inputStream?.readBytes()
            inputStream?.close()

            if (bytes != null) {
                val encodedFile = Base64.encodeToString(bytes, Base64.NO_WRAP)
                val safeFileName = fileName
                    .replace("\\", "\\\\")
                    .replace("'", "\\'")
                    .replace("\"", "\\\"")
                    .replace("\n", "\\n")
                    .replace("\r", "\\r")

                val script = "window.onNativeFileSelected('$encodedFile', '$mimeType', '$safeFileName');"
                web.post {
                    web.evaluateJavascript(script) { result ->
                        Log.d("JSCallback", "onNativeFileSelected result: $result")
                    }
                }
            } else {
                Toast.makeText(this, "Failed to read file bytes", Toast.LENGTH_SHORT).show()
            }
        } catch (e: Exception) {
            e.printStackTrace()
            Toast.makeText(this, "Failed to process file: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    /** Direct upload path for large files - streams via OkHttp, no Base64 */
    private fun handleFileSelectionDirectUpload(uri: Uri, mimeType: String, fileName: String) {
        // Show upload toast
        runOnUiThread {
            Toast.makeText(this, "Uploading $fileName...", Toast.LENGTH_SHORT).show()
        }

        CoroutineScope(Dispatchers.IO).launch {
            try {
                val origin = prefs.getString("origin", "") ?: ""
                val baseUrl = normalizeOrigin(origin).removeSuffix("/ui/")
                if (baseUrl.isBlank()) {
                    withContext(Dispatchers.Main) {
                        Toast.makeText(this@PortalActivity, "Server URL not configured", Toast.LENGTH_SHORT).show()
                    }
                    return@launch
                }

                // Stream file content directly from ContentResolver to OkHttp
                val inputStream = contentResolver.openInputStream(uri)
                    ?: throw IOException("Cannot open file")

                val requestBody = object : okhttp3.RequestBody() {
                    override fun contentType(): okhttp3.MediaType? {
                        return mimeType.toMediaTypeOrNull()
                    }
                    override fun contentLength(): Long = -1L // Unknown, stream it
                    override fun writeTo(sink: okio.BufferedSink) {
                        val buffer = ByteArray(1024 * 1024) // 1MB chunks
                        inputStream.use { stream ->
                            var bytesRead: Int
                            while (stream.read(buffer).also { bytesRead = it } != -1) {
                                sink.write(buffer, 0, bytesRead)
                            }
                        }
                    }
                }

                val multipartBody = okhttp3.MultipartBody.Builder()
                    .setType(okhttp3.MultipartBody.FORM)
                    .addFormDataPart("file", fileName, requestBody)
                    .build()

                val request = okhttp3.Request.Builder()
                    .url("$baseUrl/upload")
                    .post(multipartBody)
                    .build()

                val client = okhttp3.OkHttpClient.Builder()
                    .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
                    .writeTimeout(5, java.util.concurrent.TimeUnit.MINUTES)
                    .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
                    .build()

                val response = client.newCall(request).execute()
                val responseBody = response.body?.string()

                if (response.isSuccessful && responseBody != null) {
                    val json = org.json.JSONObject(responseBody)
                    val uploadUrl = json.getString("url")
                    Log.d("DirectUpload", "File uploaded successfully: $uploadUrl")

                    // Notify JS that file was uploaded directly (pass URL instead of base64)
                    val safeFileName = fileName
                        .replace("\\", "\\\\")
                        .replace("'", "\\'")
                        .replace("\"", "\\\"")
                        .replace("\n", "\\n")
                        .replace("\r", "\\r")

                    val script = "window.onNativeFileUploaded('$uploadUrl', '$mimeType', '$safeFileName');"
                    withContext(Dispatchers.Main) {
                        web.evaluateJavascript(script) { result ->
                            Log.d("JSCallback", "onNativeFileUploaded result: $result")
                        }
                        Toast.makeText(this@PortalActivity, "Uploaded $fileName", Toast.LENGTH_SHORT).show()
                    }
                } else {
                    val errorDetail = responseBody ?: response.message
                    Log.e("DirectUpload", "Upload failed: ${response.code} - $errorDetail")
                    withContext(Dispatchers.Main) {
                        Toast.makeText(this@PortalActivity, "Upload failed: ${response.code}", Toast.LENGTH_LONG).show()
                    }
                }
            } catch (e: Exception) {
                Log.e("DirectUpload", "Upload error", e)
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@PortalActivity, "Upload failed: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    inner class FilePickerInterface {
        @Suppress("unused")
        @JavascriptInterface
        fun openFileChooser(acceptType: String) {
            runOnUiThread {
                try {
                    val mimeTypes = acceptType.split(",").map { it.trim() }.toTypedArray()

                    val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                        type = mimeTypes.firstOrNull() ?: "*/*"
                        addCategory(Intent.CATEGORY_OPENABLE)
                        if (mimeTypes.isNotEmpty()) {
                            putExtra(Intent.EXTRA_MIME_TYPES, mimeTypes)
                        }
                    }

                    if (intent.resolveActivity(packageManager) != null) {
                        fileChooserLauncher.launch(intent)
                    } else {
                        Toast.makeText(this@PortalActivity, "No app found to handle file selection.", Toast.LENGTH_LONG).show()
                    }
                } catch (e: Exception) {
                    Toast.makeText(this@PortalActivity, "Error opening file chooser: ${e.message}", Toast.LENGTH_LONG).show()
                    e.printStackTrace()
                }
            }
        }
    }

    inner class WebAppInterface {

        // PRIMARY METHOD - Matches app.js call: AndroidBridge.showNotification(title, body)
        @JavascriptInterface
        fun showNotification(title: String, body: String) {
            Log.d(TAG, "showNotification called (simple): $title - $body")
            runOnUiThread {
                notificationManager.showTaskNotification(title, body)
            }
        }

        // SECONDARY METHOD - Matches complex calls if any
        @JavascriptInterface
        fun showNotification(title: String, body: String, operator: String?, taskType: String?, isSuccess: Boolean) {
            Log.d(TAG, "showNotification called (complex): $title - $body")
            runOnUiThread {
                notificationManager.showTaskNotification(title, body, operator, taskType, isSuccess)
            }
        }

        @JavascriptInterface
        fun vibrate(pattern: String) {
            Log.d(TAG, "vibrate called (pattern): $pattern")
            runOnUiThread {
                try {
                    val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                        val vibratorManager = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
                        vibratorManager?.defaultVibrator
                    } else {
                        @Suppress("DEPRECATION")
                        getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
                    }

                    vibrator?.let { v ->
                        val patternArray = pattern.split(",").mapNotNull { it.trim().toLongOrNull() }.toLongArray()
                        if (patternArray.isNotEmpty()) {
                            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                                val effect = VibrationEffect.createWaveform(patternArray, -1)
                                v.vibrate(effect)
                            } else {
                                @Suppress("DEPRECATION")
                                v.vibrate(patternArray, -1)
                            }
                        }
                    }
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        }

        @JavascriptInterface
        fun requestNotificationPermission(): Boolean {
            Log.d(TAG, "requestNotificationPermission called")
            runOnUiThread {
                ensureNotificationPermission()
            }
            return hasNotificationPerm()
        }

        @JavascriptInterface
        fun isNotificationEnabled(): Boolean {
            val enabled = hasNotificationPerm()
            Log.d(TAG, "isNotificationEnabled called: $enabled")
            return enabled
        }
        
        @JavascriptInterface
        fun isAndroidApp(): Boolean = true

        @JavascriptInterface
        fun log(message: String) {
            Log.d(TAG, "JS LOG: $message")
        }

        // ============ OVERLAY CONTROL ============

        /**
         * Start the BlackBox floating overlay
         * JS: AndroidBridge.startOverlay()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun startOverlay() {
            Log.d(TAG, "startOverlay called from JS")
            runOnUiThread {
                if (OverlayService.isRunning()) {
                    Toast.makeText(this@PortalActivity, "Overlay already running", Toast.LENGTH_SHORT).show()
                } else if (isXrDevice) {
                    // XR: skip SYSTEM_ALERT_WINDOW (Activity-based overlay),
                    // but still request MediaProjection for screen sharing
                    requestScreenCapturePermission()
                } else {
                    requestOverlayPermission()
                }
            }
        }

        /**
         * Stop the BlackBox floating overlay
         * JS: AndroidBridge.stopOverlay()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun stopOverlay() {
            Log.d(TAG, "stopOverlay called from JS")
            runOnUiThread {
                stopOverlayService()
            }
        }

        /**
         * Check if overlay is currently running
         * JS: AndroidBridge.isOverlayRunning()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun isOverlayRunning(): Boolean {
            return OverlayService.isRunning()
        }

        /**
         * Check if overlay permission is granted
         * JS: AndroidBridge.hasOverlayPermission()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun hasOverlayPerm(): Boolean {
            return hasOverlayPermission()
        }

        /**
         * Download a file using OkHttp (which uses the app's network context including Tailscale VPN)
         * This is necessary because Android's DownloadManager runs outside the VPN tunnel
         * and cannot access Tailscale URLs.
         *
         * Uses MediaStore API for Android 10+ (API 29+) for proper Downloads folder access.
         * Shows native download notifications with tap-to-open functionality.
         */
        @Suppress("unused")
        @JavascriptInterface
        fun downloadFile(url: String, filename: String) {
            Log.d(TAG, "downloadFile called: url=$url, filename=$filename")

            CoroutineScope(Dispatchers.Main).launch {
                // Show "Downloading..." notification on main thread FIRST
                val notificationId = notificationManager.showDownloadStarted(filename)
                Log.d(TAG, "Download notification started with ID: $notificationId")

                // Now do the actual download on IO thread
                withContext(Dispatchers.IO) {
                try {
                    val client = OkHttpClient.Builder()
                        .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
                        .readTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
                        .build()

                    val request = Request.Builder()
                        .url(url)
                        .build()

                    Log.d(TAG, "Downloading from: $url")
                    val response = client.newCall(request).execute()

                    if (response.isSuccessful) {
                        val bytes = response.body?.bytes()
                        if (bytes != null) {
                            Log.d(TAG, "Downloaded ${bytes.size} bytes, saving to Downloads...")

                            // Determine MIME type from filename extension
                            val mimeType = when {
                                filename.endsWith(".pdf", true) -> "application/pdf"
                                filename.endsWith(".docx", true) -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                filename.endsWith(".csv", true) -> "text/csv"
                                filename.endsWith(".txt", true) -> "text/plain"
                                filename.endsWith(".mp3", true) -> "audio/mpeg"
                                filename.endsWith(".wav", true) -> "audio/wav"
                                filename.endsWith(".mp4", true) -> "video/mp4"
                                filename.endsWith(".png", true) -> "image/png"
                                filename.endsWith(".jpg", true) || filename.endsWith(".jpeg", true) -> "image/jpeg"
                                else -> "application/octet-stream"
                            }

                            val savedPath: String
                            var fileUri: Uri? = null

                            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                                // Android 10+ (API 29+): Use MediaStore
                                val contentValues = android.content.ContentValues().apply {
                                    put(android.provider.MediaStore.Downloads.DISPLAY_NAME, filename)
                                    put(android.provider.MediaStore.Downloads.MIME_TYPE, mimeType)
                                    put(android.provider.MediaStore.Downloads.IS_PENDING, 1)
                                }

                                val resolver = contentResolver
                                val uri = resolver.insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, contentValues)
                                    ?: throw IOException("Failed to create MediaStore entry")

                                resolver.openOutputStream(uri)?.use { outputStream ->
                                    outputStream.write(bytes)
                                }

                                // Mark as complete
                                contentValues.clear()
                                contentValues.put(android.provider.MediaStore.Downloads.IS_PENDING, 0)
                                resolver.update(uri, contentValues, null, null)

                                savedPath = "Downloads/$filename"
                                fileUri = uri
                                Log.d(TAG, "File saved via MediaStore: $uri")
                            } else {
                                // Android 9 and below: Use direct file access
                                val downloadsDir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
                                val file = File(downloadsDir, filename)
                                file.writeBytes(bytes)

                                // Notify MediaScanner so file shows up immediately
                                android.media.MediaScannerConnection.scanFile(
                                    this@PortalActivity,
                                    arrayOf(file.absolutePath),
                                    arrayOf(mimeType),
                                    null
                                )

                                savedPath = file.absolutePath
                                fileUri = Uri.fromFile(file)
                                Log.d(TAG, "File saved directly: $savedPath")
                            }

                            withContext(Dispatchers.Main) {
                                // Update notification to "Download complete" with tap-to-open
                                if (fileUri != null) {
                                    notificationManager.showDownloadComplete(notificationId, filename, fileUri, mimeType)
                                }

                                Toast.makeText(
                                    this@PortalActivity,
                                    "Saved to $savedPath (${bytes.size / 1024} KB)",
                                    Toast.LENGTH_SHORT
                                ).show()

                                // Notify JavaScript of success
                                web.evaluateJavascript(
                                    "window.onAndroidDownloadComplete && window.onAndroidDownloadComplete('$filename', true);",
                                    null
                                )
                            }
                        } else {
                            throw IOException("Empty response body")
                        }
                    } else {
                        throw IOException("Download failed: ${response.code}")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Download error: ${e.message}", e)
                    withContext(Dispatchers.Main) {
                        // Update notification to show failure
                        notificationManager.showDownloadFailed(notificationId, filename, e.message ?: "Unknown error")

                        Toast.makeText(
                            this@PortalActivity,
                            "Download failed: ${e.message}",
                            Toast.LENGTH_LONG
                        ).show()

                        // Notify JavaScript of failure
                        val safeError = e.message?.replace("'", "\\'") ?: "Unknown error"
                        web.evaluateJavascript(
                            "window.onAndroidDownloadComplete && window.onAndroidDownloadComplete('$filename', false, '$safeError');",
                            null
                        )
                    }
                }
            }  // end withContext(Dispatchers.IO)
            }  // end CoroutineScope(Dispatchers.Main).launch
        }

        // Kept for backward compatibility with "AndroidMic" usage
        @Suppress("unused")
        @JavascriptInterface
        fun vibrate(duration: Int) {
             Log.d(TAG, "vibrate called (duration): $duration")
            runOnUiThread {
                try {
                    val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                        val vibratorManager = getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager
                        vibratorManager?.defaultVibrator
                    } else {
                        @Suppress("DEPRECATION")
                        getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
                    }

                    vibrator?.let {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            val effect = VibrationEffect.createOneShot(
                                duration.toLong(),
                                VibrationEffect.DEFAULT_AMPLITUDE
                            )
                            it.vibrate(effect)
                        } else {
                            @Suppress("DEPRECATION")
                            it.vibrate(duration.toLong())
                        }
                    }
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        }

        @Suppress("unused")
        @JavascriptInterface
        fun startRecording() {
            Log.d(TAG, "startRecording called - isRecording=$isRecording, isStreaming=$isStreaming")
            runOnUiThread {
                Log.d(TAG, "startRecording runOnUiThread - checking permissions")
                if (!hasMicPerm()) {
                    Log.e(TAG, "startRecording - mic permission denied")
                    web.post {
                        web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Permission denied');", null)
                    }
                    return@runOnUiThread
                }

                if (isRecording) {
                    Log.w(TAG, "startRecording - already recording, rejecting")
                    web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Already recording');", null) }
                    return@runOnUiThread
                }

                // Stop any active streaming before starting MediaRecorder
                if (isStreaming) {
                    Log.d(TAG, "startRecording - stopping active streaming first")
                    stopAudioStreamingInternal()
                }

                Log.d(TAG, "startRecording - starting MediaRecorder setup")
                try {
                    val cachePath = externalCacheDir ?: cacheDir
                    cachePath.mkdirs()
                    audioFile = File(cachePath, "recording_${System.currentTimeMillis()}.m4a")

                    mediaRecorder = if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
                        MediaRecorder(this@PortalActivity)
                    } else {
                        @Suppress("DEPRECATION")
                        MediaRecorder()
                    }.apply {
                        setAudioSource(MediaRecorder.AudioSource.MIC)
                        setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                        setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                        setAudioEncodingBitRate(128000)
                        setAudioSamplingRate(44100)
                        setOutputFile(audioFile!!.absolutePath)

                        setOnErrorListener { _, what, extra ->
                            Log.e("MediaRecorderError", "Recording Error - what: $what, extra: $extra")
                            runOnUiThread {
                                web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Recording error code: $what');", null) }
                                stopRecordingInternal()
                            }
                        }

                        prepare()
                        Log.d(TAG, "startRecording - MediaRecorder prepared, starting...")
                        start()
                        Log.d(TAG, "startRecording - MediaRecorder started successfully")
                    }

                    isRecording = true
                    Log.d(TAG, "startRecording - calling onNativeMicStart callback")
                    web.post { web.evaluateJavascript("window.onNativeMicStart && window.onNativeMicStart();", null) }

                } catch (e: Exception) {
                    Log.e(TAG, "startRecording - EXCEPTION: ${e.message}", e)
                    e.printStackTrace()
                    web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Start failed: ${e.message?.replace("'", "\\'")}');", null) }
                    stopRecordingInternal()
                }
            }
        }

        @Suppress("unused")
        @JavascriptInterface
        fun stopRecording() {
            runOnUiThread { stopRecordingInternal() }
        }

        /**
         * Stop recording and return the audio data as base64 instead of uploading to Whisper.
         * Use this for Gemini Pipeline or other custom audio processing.
         * JS: AndroidMic.stopRecordingAndGetAudio()
         * Callback: window.onNativeMicAudio(base64Data, mimeType, fileName)
         */
        @Suppress("unused")
        @JavascriptInterface
        fun stopRecordingAndGetAudio() {
            Log.d(TAG, "stopRecordingAndGetAudio called")
            runOnUiThread { stopRecordingAndReturnAudio() }
        }

        @Suppress("unused")
        @JavascriptInterface
        fun isRecording(): Boolean = isRecording

        /**
         * Release the microphone without uploading to Whisper.
         * Call this before using getUserMedia in WebView to avoid "mic busy" errors.
         * JS: AndroidMic.releaseMic()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun releaseMic() {
            Log.d(TAG, "releaseMic called - releasing native mic resources")
            runOnUiThread {
                try {
                    // Stop any streaming first
                    stopAudioStreamingInternal()

                    mediaRecorder?.apply {
                        if (isRecording) {
                            try { stop() } catch (e: IllegalStateException) {
                                Log.w(TAG, "MediaRecorder stop failed (may not be recording): ${e.message}")
                            }
                        }
                        try { reset() } catch (e: Exception) {
                            Log.w(TAG, "MediaRecorder reset failed: ${e.message}")
                        }
                        try { release() } catch (e: Exception) {
                            Log.w(TAG, "MediaRecorder release failed: ${e.message}")
                        }
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "releaseMic error: ${e.message}")
                } finally {
                    mediaRecorder = null
                    isRecording = false
                    // Delete any temp audio file without uploading
                    audioFile?.let { file ->
                        if (file.exists()) {
                            val deleted = file.delete()
                            Log.d(TAG, "Deleted temp audio file: $deleted")
                        }
                    }
                    audioFile = null
                    Log.d(TAG, "Native mic resources released")
                    // Notify JavaScript that mic is released
                    web.post {
                        web.evaluateJavascript("window.onNativeMicReleased && window.onNativeMicReleased();", null)
                    }
                }
            }
        }

        // ============ AUDIO STREAMING FOR REALTIME API ============

        /**
         * Start streaming audio for Realtime API.
         * Uses AudioRecord to capture raw PCM audio and streams it as base64 chunks.
         * JS: AndroidMic.startAudioStreaming()
         * Callback: window.onNativeAudioChunk(base64Data) - called for each audio chunk
         */
        @Suppress("unused")
        @JavascriptInterface
        fun startAudioStreaming() {
            Log.d(TAG, "startAudioStreaming called")
            if (!hasMicPerm()) {
                web.post {
                    web.evaluateJavascript("window.onNativeStreamError && window.onNativeStreamError('Permission denied');", null)
                }
                return
            }

            if (isStreaming) {
                Log.w(TAG, "Already streaming audio")
                return
            }

            // Stop any existing recording first
            if (isRecording) {
                Log.d(TAG, "Stopping existing recording before streaming")
                stopRecordingInternal()
            }

            try {
                // Configure AudioRecord for 24kHz mono PCM16 (OpenAI Realtime format)
                val sampleRate = 24000
                val channelConfig = AudioFormat.CHANNEL_IN_MONO
                val audioFormat = AudioFormat.ENCODING_PCM_16BIT
                val bufferSize = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat) * 2

                Log.d(TAG, "Creating AudioRecord: sampleRate=$sampleRate, bufferSize=$bufferSize")

                audioRecord = AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    sampleRate,
                    channelConfig,
                    audioFormat,
                    bufferSize
                )

                if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                    Log.e(TAG, "AudioRecord failed to initialize")
                    web.post {
                        web.evaluateJavascript("window.onNativeStreamError && window.onNativeStreamError('Failed to initialize audio capture');", null)
                    }
                    audioRecord?.release()
                    audioRecord = null
                    return
                }

                audioRecord?.startRecording()
                isStreaming = true

                Log.d(TAG, "AudioRecord started, beginning streaming thread")

                // Notify JavaScript that streaming started
                web.post {
                    web.evaluateJavascript("window.onNativeStreamStart && window.onNativeStreamStart();", null)
                }

                // Start streaming thread
                streamingThread = Thread {
                    val buffer = ShortArray(bufferSize / 2)  // 16-bit samples
                    val byteBuffer = ByteArray(bufferSize)

                    while (isStreaming && audioRecord != null) {
                        try {
                            val readCount = audioRecord?.read(buffer, 0, buffer.size) ?: 0

                            if (readCount > 0) {
                                // Convert shorts to bytes (little-endian PCM16)
                                for (i in 0 until readCount) {
                                    byteBuffer[i * 2] = (buffer[i].toInt() and 0xFF).toByte()
                                    byteBuffer[i * 2 + 1] = (buffer[i].toInt() shr 8 and 0xFF).toByte()
                                }

                                // Encode to base64
                                val base64Data = Base64.encodeToString(byteBuffer, 0, readCount * 2, Base64.NO_WRAP)

                                // Send to JavaScript
                                runOnUiThread {
                                    web.evaluateJavascript(
                                        "window.onNativeAudioChunk && window.onNativeAudioChunk('$base64Data');",
                                        null
                                    )
                                }
                            }
                        } catch (e: Exception) {
                            Log.e(TAG, "Error reading audio: ${e.message}")
                            break
                        }
                    }

                    Log.d(TAG, "Streaming thread ended")
                }
                streamingThread?.start()

            } catch (e: Exception) {
                Log.e(TAG, "Error starting audio streaming: ${e.message}", e)
                web.post {
                    web.evaluateJavascript(
                        "window.onNativeStreamError && window.onNativeStreamError('${e.message?.replace("'", "\\'")}');",
                        null
                    )
                }
                stopAudioStreamingInternal()
            }
        }

        /**
         * Stop streaming audio.
         * JS: AndroidMic.stopAudioStreaming()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun stopAudioStreaming() {
            Log.d(TAG, "stopAudioStreaming called")
            runOnUiThread {
                stopAudioStreamingInternal()
                web.post {
                    web.evaluateJavascript("window.onNativeStreamStop && window.onNativeStreamStop();", null)
                }
            }
        }

        /**
         * Check if currently streaming audio.
         * JS: AndroidMic.isAudioStreaming()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun isAudioStreaming(): Boolean = isStreaming

        // ============ ADB QUICK PAIR HELPERS ============

        /**
         * Get the device's Tailscale IP address by reading the tun0/tailscale0 network interface.
         * Returns the IP as a string, or empty string if not found.
         * JS: AndroidBridge.getDeviceTailscaleIp()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun getDeviceTailscaleIp(): String {
            try {
                val interfaces = java.net.NetworkInterface.getNetworkInterfaces()
                while (interfaces.hasMoreElements()) {
                    val iface = interfaces.nextElement()
                    val name = iface.name.lowercase()
                    if (name == "tun0" || name.startsWith("tailscale")) {
                        val addrs = iface.inetAddresses
                        while (addrs.hasMoreElements()) {
                            val addr = addrs.nextElement()
                            if (!addr.isLoopbackAddress && addr is java.net.Inet4Address) {
                                val ip = addr.hostAddress ?: ""
                                Log.d(TAG, "getDeviceTailscaleIp: found $ip on $name")
                                return ip
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "getDeviceTailscaleIp error: ${e.message}")
            }
            Log.w(TAG, "getDeviceTailscaleIp: no Tailscale interface found")
            return ""
        }

        /**
         * Open Android Developer Options (Wireless Debugging settings).
         * JS: AndroidBridge.openWirelessDebuggingSettings()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun openWirelessDebuggingSettings() {
            Log.d(TAG, "openWirelessDebuggingSettings called")
            runOnUiThread {
                try {
                    val intent = Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS)
                    startActivity(intent)
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to open developer settings: ${e.message}")
                    Toast.makeText(
                        this@PortalActivity,
                        "Could not open Developer Options",
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
        }
    }

    /**
     * Internal function to stop audio streaming
     */
    private fun stopAudioStreamingInternal() {
        Log.d(TAG, "stopAudioStreamingInternal called")
        isStreaming = false

        try {
            streamingThread?.interrupt()
            streamingThread = null
        } catch (e: Exception) {
            Log.w(TAG, "Error stopping streaming thread: ${e.message}")
        }

        try {
            audioRecord?.apply {
                try { stop() } catch (e: IllegalStateException) {
                    Log.w(TAG, "AudioRecord stop failed: ${e.message}")
                }
                try { release() } catch (e: Exception) {
                    Log.w(TAG, "AudioRecord release failed: ${e.message}")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping AudioRecord: ${e.message}")
        } finally {
            audioRecord = null
            Log.d(TAG, "Audio streaming stopped and resources released")
        }
    }

    private fun stopRecordingInternal() {
        Log.d(TAG, "stopRecordingInternal called - isRecording=$isRecording")
        if (!isRecording) {
            Log.w(TAG, "stopRecordingInternal - not recording, returning early")
            return
        }

        try {
            Log.d(TAG, "stopRecordingInternal - stopping MediaRecorder")
            mediaRecorder?.apply {
                try { stop() } catch (e: IllegalStateException) {
                    Log.e(TAG, "stopRecordingInternal - stop() failed: ${e.message}")
                    e.printStackTrace()
                }
                try { release() } catch (e: Exception) {
                    Log.e(TAG, "stopRecordingInternal - release() failed: ${e.message}")
                    e.printStackTrace()
                }
            }
            Log.d(TAG, "stopRecordingInternal - MediaRecorder stopped and released")
        } catch (e: Exception) {
            Log.e(TAG, "stopRecordingInternal - EXCEPTION: ${e.message}", e)
            e.printStackTrace()
            web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Stop/Release failed: ${e.message?.replace("'", "\\'")}');", null) }
        } finally {
            mediaRecorder = null
            isRecording = false

            val currentAudioFile = audioFile
            if (currentAudioFile != null && currentAudioFile.exists() && currentAudioFile.length() > 0) {
                Log.d(TAG, "stopRecordingInternal - Audio file ready: ${currentAudioFile.absolutePath}, Size: ${currentAudioFile.length()}")
                uploadToWhisper(currentAudioFile)
            } else {
                Log.w(TAG, "stopRecordingInternal - Audio file is null, doesn't exist, or is empty. Skipping upload.")
                web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('No valid audio file recorded');", null) }
            }
            audioFile = null
        }
    }

    /**
     * Stop recording and return the audio as base64 via JavaScript callback.
     * Used for Gemini Pipeline - same recording mechanism as Whisper but returns data instead of transcribing.
     */
    private fun stopRecordingAndReturnAudio() {
        if (!isRecording) {
            Log.w(TAG, "stopRecordingAndReturnAudio called but not recording")
            web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Not recording');", null) }
            return
        }

        try {
            mediaRecorder?.apply {
                try { stop() } catch (e: IllegalStateException) { e.printStackTrace() }
                try { release() } catch (e: Exception) { e.printStackTrace() }
            }
        } catch (e: Exception) {
            e.printStackTrace()
            web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Stop/Release failed: ${e.message?.replace("'", "\\'")}');", null) }
        } finally {
            mediaRecorder = null
            isRecording = false

            val currentAudioFile = audioFile
            if (currentAudioFile != null && currentAudioFile.exists() && currentAudioFile.length() > 0) {
                Log.d(TAG, "Audio file ready for return: ${currentAudioFile.absolutePath}, Size: ${currentAudioFile.length()}")
                returnAudioAsBase64(currentAudioFile)
            } else {
                Log.w(TAG, "Audio file is null, doesn't exist, or is empty.")
                web.post { web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('No valid audio file recorded');", null) }
            }
            audioFile = null
        }
    }

    /**
     * Read audio file and return as base64 via JavaScript callback.
     */
    private fun returnAudioAsBase64(file: File) {
        Log.d(TAG, "Reading audio file for base64 return: ${file.name}")
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val bytes = file.readBytes()
                val base64Data = Base64.encodeToString(bytes, Base64.NO_WRAP)
                val mimeType = "audio/aac"  // M4A contains AAC audio - Gemini expects audio/aac
                val fileName = file.name

                Log.d(TAG, "Audio encoded: ${bytes.size} bytes -> ${base64Data.length} base64 chars")

                withContext(Dispatchers.Main) {
                    // Call JavaScript callback with the audio data
                    web.evaluateJavascript(
                        "window.onNativeMicAudio && window.onNativeMicAudio('$base64Data', '$mimeType', '$fileName');",
                        null
                    )
                    Log.d(TAG, "Called onNativeMicAudio callback")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error reading audio file: ${e.message}", e)
                withContext(Dispatchers.Main) {
                    web.evaluateJavascript(
                        "window.onNativeMicError && window.onNativeMicError('Failed to read audio: ${e.message?.replace("'", "\\'")}');",
                        null
                    )
                }
            } finally {
                // Clean up the temp file
                val deleted = file.delete()
                Log.d(TAG, "Deleted temp audio file: $deleted")
            }
        }
    }

    private fun uploadToWhisper(file: File) {
        Log.d("UploadWhisper", "Attempting to upload file: ${file.name}")
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val origin = prefs.getString("origin", "") ?: ""
                val baseUrl = normalizeOrigin(origin).removeSuffix("/ui/")
                if (baseUrl.isBlank()) {
                    throw IOException("Origin URL is not set.")
                }

                val client = OkHttpClient.Builder()
                    .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
                    .writeTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
                    .readTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
                    .build()

                val mediaType = "audio/m4a".toMediaTypeOrNull()
                val requestBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart(
                        "file",
                        file.name,
                        file.asRequestBody(mediaType)
                    )
                    .build()

                val request = Request.Builder()
                    .url("$baseUrl/stt")
                    .post(requestBody)
                    .build()

                Log.d("UploadWhisper", "Request URL: ${request.url}")

                val response = client.newCall(request).execute()
                val responseCode = response.code
                val responseBody = response.body?.string()

                Log.d("UploadWhisper", "Response Code: $responseCode")
                Log.d("UploadWhisper", "Response Body: ${responseBody?.take(500)}")

                withContext(Dispatchers.Main) {
                    if (response.isSuccessful && responseBody != null) {
                        try {
                            val json = JSONObject(responseBody)
                            val text = json.optString("text", "").trim()

                            if (text.isNotEmpty()) {
                                val escapedText = text
                                    .replace("\\", "\\\\")
                                    .replace("'", "\\'")
                                    .replace("\"", "\\\"")
                                    .replace("\n", "\\n")
                                    .replace("\r", "\\r")
                                web.evaluateJavascript(
                                    "window.onNativeMicTranscript && window.onNativeMicTranscript('$escapedText');",
                                    null
                                )
                            } else {
                                web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('No transcript received from server');", null)
                            }
                        } catch (e: org.json.JSONException) {
                            web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Failed to parse server response');", null)
                            e.printStackTrace()
                        }
                    } else {
                        val errorDetail = responseBody ?: "Unknown error"
                        web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Upload failed: $responseCode - ${errorDetail.take(100).replace("'", "\\'")}');", null)
                    }
                }

            } catch (e: IOException) {
                e.printStackTrace()
                withContext(Dispatchers.Main) {
                    web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('Network or file error: ${e.message?.replace("'", "\\'")}');", null)
                }
            } catch (e: Exception) {
                e.printStackTrace()
                withContext(Dispatchers.Main) {
                    web.evaluateJavascript("window.onNativeMicError && window.onNativeMicError('An unexpected error occurred: ${e.message?.replace("'", "\\'")}');", null)
                }
            } finally {
                val deleted = file.delete()
                Log.d("UploadWhisper", "Deleted temp file ${file.name}: $deleted")
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        // Unregister the MediaProjection refresh receiver
        try {
            unregisterReceiver(mediaProjectionRefreshReceiver)
        } catch (e: Exception) {
            Log.w(TAG, "Receiver already unregistered: ${e.message}")
        }
        stopKeepAliveService()
        releaseWakeLock()
        stopRecordingInternal()
        web.destroy()
    }
}
