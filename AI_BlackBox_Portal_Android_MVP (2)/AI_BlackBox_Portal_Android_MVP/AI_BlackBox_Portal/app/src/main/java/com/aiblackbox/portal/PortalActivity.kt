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
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.URLUtil
import android.widget.Button
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import androidx.core.net.toUri
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.aiblackbox.portal.data.store.OperatorResolver
import com.aiblackbox.portal.overlay.DeviceCapabilities
import com.aiblackbox.portal.overlay.FoldingFeatureMonitor
import com.aiblackbox.portal.overlay.OverlayService
import com.aiblackbox.portal.ui.webview.BbxWebChromeClient
import com.aiblackbox.portal.ui.webview.BbxWebViewClient
import com.aiblackbox.portal.ui.webview.FileChooserLauncher
import com.aiblackbox.portal.ui.webview.RendererCrashBudget
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
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.roundToInt

// Renderer crashes counted across recreate(), which is how a crash is recovered here.
private val portalRendererCrashes = RendererCrashBudget()

class PortalActivity : AppCompatActivity() {

    private val prefs by lazy { getSharedPreferences("bbx_prefs", MODE_PRIVATE) }
    // (M6 / I1) Same authoritative XR probe as the wire capability + OverlayService, so this
    // Activity's XR-vs-phone overlay routing can't diverge from DeviceCapabilities.detect(): a
    // headset classified xr_headset on the wire but NOT exposing exactly xr.api.spatial must still
    // take the XR path here, or the in-headset consent banner never surfaces (I1).
    private val isXrDevice: Boolean by lazy {
        DeviceCapabilities.isXr(this)
    }
    private lateinit var web: WebView
    private lateinit var retryPanel: View
    private lateinit var notificationManager: BlackBoxNotificationManager
    private var mediaRecorder: MediaRecorder? = null
    private var audioFile: File? = null
    private var isRecording = false
    private var wakeLock: PowerManager.WakeLock? = null
    private val TAG = "BlackBoxBridge"

    // (A01-activities-root-01 / X04-security-clients-infra-05) The ONE origin this WebView may
    // stay on and hand its bridges to: the paired box (scheme://host[:port] of the URL loadPortal
    // loads), set once in onCreate. Null — unpaired or unparseable — refuses everything.
    @Volatile private var pairedOrigin: String? = null
    // Origin of the page in the MAIN frame, kept by the WebViewClient callbacks (UI thread) and
    // read by the @JavascriptInterface methods, which run on the JavaBridge thread — where
    // web.url must not be called. Null (between pages, or unknown) refuses like a foreign page.
    @Volatile private var pageOrigin: String? = null

    // Audio streaming for Realtime API. The streaming thread owns its AudioRecord outright (a local,
    // released in its own finally); everything else holds only this session's stop signal — one per
    // session, so a quick stop + start can never leave the old thread reading into the new session
    // the way one shared flag would. Volatile: the JS bridge thread starts, the UI thread stops.
    @Volatile private var isStreaming = false
    @Volatile private var streamingLive: AtomicBoolean? = null
    @Volatile private var streamingThread: Thread? = null

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

    // The page's own <input type=file> (audit rank 62), answered exactly once: picked,
    // cancelled, or a second chooser replacing a pending one.
    private var pendingWebFileCallback: ((Array<Uri>?) -> Unit)? = null
    private val webFileChooserLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val callback = pendingWebFileCallback
        pendingWebFileCallback = null
        callback?.invoke(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
    }
    private val portalFileChooser = FileChooserLauncher { params, onResult ->
        pendingWebFileCallback?.invoke(null)
        pendingWebFileCallback = onResult
        try {
            webFileChooserLauncher.launch(params.createIntent())
            true
        } catch (e: Exception) {
            pendingWebFileCallback = null
            false
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

    /**
     * Hand the overlay the operator the web Portal has selected (its own localStorage pick),
     * read only off the paired box's page; "" when there is none, and the overlay resolves
     * one from the box's roster itself (OperatorResolver.forSpinner). Never a seed name.
     */
    private fun startOverlayService(resultCode: Int, resultData: Intent) {
        if (!isPortalPage()) {
            launchOverlayService(resultCode, resultData, operator = "")
            return
        }
        web.evaluateJavascript(
            "(function(){try{return localStorage.getItem('bbx_operator')}catch(e){return null}})()"
        ) { raw -> launchOverlayService(resultCode, resultData, OperatorResolver.fromJsResult(raw)) }
    }

    private fun launchOverlayService(resultCode: Int, resultData: Intent, operator: String) {
        val origin = prefs.getString("origin", "") ?: ""
        val baseUrl = normalizeOrigin(origin).removeSuffix("/ui/")

        val intent = Intent(this, OverlayService::class.java).apply {
            putExtra(OverlayService.EXTRA_RESULT_CODE, resultCode)
            putExtra(OverlayService.EXTRA_RESULT_DATA, resultData)
            putExtra(OverlayService.EXTRA_SERVER_URL, baseUrl)
            putExtra(OverlayService.EXTRA_OPERATOR, operator)
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
        // The origin of exactly what loadPortal loads (normalizeOrigin forces https and drops
        // the port for a .ts.net name), so the first page is recognized as the paired box.
        pairedOrigin = PortalWebGuard.originOf(normalizeOrigin(origin))

        // The model-free control/notification listener, as NativeMainActivity starts it: its
        // start registers this install's pairing secret with the box (audit X02/X04), so "open
        // the app once to re-pair" works in the WebView UI too. Non-throwing; started from a
        // foregrounded Activity so the FGS start is permitted.
        NotificationListenerFgs.start(this)

        // Edge-to-edge with transparent status and navigation bars and light icons. Android
        // 15+ enforces edge-to-edge and ignores bar colours (Window.statusBarColor is
        // deprecated there); below it enableEdgeToEdge sets the same window flags and colours.
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )
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
                // Delegate to our OkHttp-based download function that works with VPN and MediaStore.
                // Native, not a JS call — so it skips the bridge gate, but startDownload still only
                // fetches from the paired box (an iframe can trigger a download too).
                webAppInterface.startDownload(url, fileName)
            } catch (e: Exception) {
                Toast.makeText(applicationContext, "Error setting up download: ${e.message}", Toast.LENGTH_LONG).show()
                e.printStackTrace()
            }
        }

        // BbxWebViewClient (audit rank 62): a renderer crash recreates the activity (a fresh
        // WebView reloads the Portal) instead of Android killing the whole app.
        web.webViewClient = object : BbxWebViewClient(onRendererGone = { onPortalRendererGone() }) {
            /**
             * (A01-activities-root-01) Only the paired box stays in this WebView — a link in a chat
             * reply, a target=_blank, a redirect off-box all go to the system (a browser, the
             * dialer…) instead of becoming the page that owns every bridge. Subframes (embeds,
             * proxied Apps) load in place: the bridges gate on the MAIN frame's origin.
             */
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val req = request ?: return false
                if (!req.isForMainFrame) return false
                if (PortalWebGuard.isPairedOrigin(req.url?.toString(), pairedOrigin)) return false
                Log.i(TAG, "Off-box navigation handed to the system: ${PortalWebGuard.originOf(req.url?.toString()) ?: req.url?.scheme}")
                openExternally(req.url)
                return true
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                pageOrigin = PortalWebGuard.originOf(url)
                // A page that is not the box must not inherit a live mic started by the Portal:
                // evalInPortal already drops its chunks/transcript, and this frees the mic, which
                // the foreign page could not stop (its bridge calls are refused).
                if (!isPortalPage()) {
                    if (isStreaming) stopAudioStreamingInternal()
                    if (isRecording) stopRecordingInternal()
                }
                super.onPageStarted(view, url, favicon)
            }

            override fun doUpdateVisitedHistory(view: WebView?, url: String?, isReload: Boolean) {
                // Every main-frame history entry, including back/forward and pushState.
                pageOrigin = PortalWebGuard.originOf(url)
                super.doUpdateVisitedHistory(view, url, isReload)
            }

            override fun shouldInterceptRequest(
                view: WebView?,
                request: WebResourceRequest?
            ): android.webkit.WebResourceResponse? {
                // A main-frame load that skipped shouldOverrideUrlLoading — WebView never asks it
                // about a form POST, and a chat reply can carry an off-box <form method=post> —
                // is refused before the foreign page can commit in this URL-bar-less WebView. A
                // 204 aborts the navigation and leaves the Portal (and its bridges) on screen; a
                // GET goes to the system as shouldOverrideUrlLoading would. Background thread.
                if (request != null && PortalWebGuard.blocksMainFrameLoad(
                        request.url?.toString(), request.isForMainFrame, pairedOrigin)) {
                    val method = request.method ?: "GET"
                    Log.w(TAG, "Off-box main-frame $method refused: ${PortalWebGuard.originOf(request.url?.toString()) ?: request.url?.scheme}")
                    runOnUiThread {
                        if (method.equals("GET", ignoreCase = true)) {
                            openExternally(request.url)
                        } else {
                            Toast.makeText(applicationContext, "Blocked a form that sends to a site outside the BlackBox", Toast.LENGTH_SHORT).show()
                        }
                    }
                    return android.webkit.WebResourceResponse(
                        "text/plain", "utf-8", 204, "No Content", emptyMap(),
                        java.io.ByteArrayInputStream(ByteArray(0))
                    )
                }
                return super.shouldInterceptRequest(view, request)
            }

            override fun onReceivedError(
                view: WebView?,
                req: WebResourceRequest?,
                err: WebResourceError?
            ) {
                // An off-box load refused above is not the Portal failing to load: no retry panel.
                if (req?.isForMainFrame == true &&
                    !PortalWebGuard.blocksMainFrameLoad(req.url?.toString(), true, pairedOrigin)) {
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

        // BbxWebChromeClient (audit rank 62): JS confirm()/alert() show real dialogs (confirm()
        // returned false before) and the page's own <input type=file> opens the picker.
        web.webChromeClient = object : BbxWebChromeClient(portalFileChooser) {
            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    val wantsAudio = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                    // (A01-activities-root-01) Mic capture for the paired box ONLY — the exact
                    // origin, not "any https" or "contains .ts.net" (foo.ts.net.evil.example
                    // matched that), and never a foreign iframe asking on the Portal's behalf.
                    val fromPairedBox = PortalWebGuard.isPairedOrigin(request.origin?.toString(), pairedOrigin)

                    if (wantsAudio && fromPairedBox && hasMicPerm()) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    } else {
                        Log.w("PermissionRequest", "Denying permission. wantsAudio=$wantsAudio, fromPairedBox=$fromPairedBox, hasMicPerm=${hasMicPerm()}")
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

    // ============ ORIGIN GATE (A01-activities-root-01 / X04-security-clients-infra-05) ============

    /** The main frame is the paired box: the gate on every JS bridge and native→page callback. */
    private fun isPortalPage(): Boolean {
        val paired = pairedOrigin ?: return false
        return pageOrigin == paired
    }

    /**
     * First line of EVERY @JavascriptInterface method. Those run on the JavaBridge thread, so
     * this reads the @Volatile [pageOrigin] the WebViewClient keeps — never web.url (UI thread).
     */
    private fun bridgeAllowed(method: String): Boolean {
        if (isPortalPage()) return true
        Log.w(TAG, "JS bridge $method refused: page ${pageOrigin ?: "(unknown)"} is not the paired box")
        return false
    }

    /**
     * Hand an off-box URL to the system. CATEGORY_BROWSABLE limits it to activities that accept
     * links from a browser (as Chrome does); an intent: or file: URL simply finds no taker.
     */
    private fun openExternally(uri: Uri?) {
        if (uri == null) return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, uri).apply {
                addCategory(Intent.CATEGORY_BROWSABLE)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (e: Exception) { // ActivityNotFoundException, FileUriExposedException
            Log.w(TAG, "No app to open off-box link (${uri.scheme}): ${e.message}")
            Toast.makeText(this, "No app can open this link", Toast.LENGTH_SHORT).show()
        }
    }

    /**
     * `window.<fn> && window.<fn>(…)` with every argument JSON-encoded — JSONObject.quote for
     * strings — so a file name, server reply or exception message can never break out of its
     * string literal into script. [fn] is always one of our own callback names, never data.
     */
    private fun jsCall(fn: String, vararg args: Any?): String =
        args.joinToString(", ", prefix = "window.$fn && window.$fn(", postfix = ");") { arg ->
            when (arg) {
                null -> "null"
                is Boolean, is Int, is Long -> arg.toString()
                else -> JSONObject.quote(arg.toString())
            }
        }

    /**
     * Run [script] in the page only while it is the paired box: a mic chunk, transcript, picked
     * file or download result must never reach a page that has navigated elsewhere. UI thread.
     */
    private fun evalInPortal(script: String, callback: ValueCallback<String>? = null) {
        if (!isPortalPage()) {
            Log.w(TAG, "Dropped a native→page callback: page ${pageOrigin ?: "(unknown)"} is not the paired box")
            return
        }
        web.evaluateJavascript(script, callback)
    }

    private fun loadPortal(origin: String) {
        val normalizedUrl = normalizeOrigin(origin)
        Log.d("LoadPortal", "Loading URL: $normalizedUrl")
        retryPanel.visibility = View.GONE
        // The document about to run IS the paired box's, so the gate opens now rather than when
        // the posted onPageStarted lands — an early Portal script's bridge call must not race it.
        // (An off-box redirect never commits: shouldOverrideUrlLoading hands it to the system.)
        pageOrigin = PortalWebGuard.originOf(normalizedUrl)
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
                // The MIME type comes from the providing app and the name from the file itself:
                // both are JSON-quoted (jsCall), never spliced into the script raw.
                val script = jsCall("onNativeFileSelected", encodedFile, mimeType, fileName)
                web.post {
                    evalInPortal(script) { result ->
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
                    val script = jsCall("onNativeFileUploaded", uploadUrl, mimeType, fileName)
                    withContext(Dispatchers.Main) {
                        evalInPortal(script) { result ->
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

    // Every @JavascriptInterface method below opens with bridgeAllowed(): addJavascriptInterface
    // exposes these objects to WHATEVER page the WebView shows, so each call checks that page is
    // the paired box (A01-activities-root-01 / X04-security-clients-infra-05).
    inner class FilePickerInterface {
        @Suppress("unused")
        @JavascriptInterface
        fun openFileChooser(acceptType: String) {
            if (!bridgeAllowed("openFileChooser")) return
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
            if (!bridgeAllowed("showNotification")) return
            Log.d(TAG, "showNotification called (simple): $title - $body")
            runOnUiThread {
                notificationManager.showTaskNotification(title, body)
            }
        }

        // SECONDARY METHOD - Matches complex calls if any
        @JavascriptInterface
        fun showNotification(title: String, body: String, operator: String?, taskType: String?, isSuccess: Boolean) {
            if (!bridgeAllowed("showNotification")) return
            Log.d(TAG, "showNotification called (complex): $title - $body")
            runOnUiThread {
                notificationManager.showTaskNotification(title, body, operator, taskType, isSuccess)
            }
        }

        @JavascriptInterface
        fun vibrate(pattern: String) {
            if (!bridgeAllowed("vibrate")) return
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
            if (!bridgeAllowed("requestNotificationPermission")) return false
            Log.d(TAG, "requestNotificationPermission called")
            runOnUiThread {
                ensureNotificationPermission()
            }
            return hasNotificationPerm()
        }

        @JavascriptInterface
        fun isNotificationEnabled(): Boolean {
            if (!bridgeAllowed("isNotificationEnabled")) return false
            val enabled = hasNotificationPerm()
            Log.d(TAG, "isNotificationEnabled called: $enabled")
            return enabled
        }
        
        @JavascriptInterface
        fun isAndroidApp(): Boolean = bridgeAllowed("isAndroidApp")

        @JavascriptInterface
        fun log(message: String) {
            if (!bridgeAllowed("log")) return
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
            if (!bridgeAllowed("startOverlay")) return
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
            if (!bridgeAllowed("stopOverlay")) return
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
            if (!bridgeAllowed("isOverlayRunning")) return false
            return OverlayService.isRunning()
        }

        /**
         * Check if overlay permission is granted
         * JS: AndroidBridge.hasOverlayPermission()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun hasOverlayPerm(): Boolean {
            if (!bridgeAllowed("hasOverlayPerm")) return false
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
            if (!bridgeAllowed("downloadFile")) return
            startDownload(url, filename)
        }

        /**
         * The download itself — shared by the JS bridge above and the WebView's DownloadListener.
         * Fetches only from the paired box (the app's OkHttp rides the tailnet; it is not a
         * general fetch-anything-into-Downloads primitive): an off-box URL goes to the system
         * browser, which downloads it itself, and a redirect off the box fails the download
         * ([PortalWebGuard.pairedOnlyDownloadClient]). The name is reduced to a plain file name.
         */
        fun startDownload(url: String, requestedName: String) {
            if (!PortalWebGuard.isPairedOrigin(url, pairedOrigin)) {
                Log.w(TAG, "Off-box download handed to the system: ${PortalWebGuard.originOf(url) ?: "(no origin)"}")
                runOnUiThread { openExternally(Uri.parse(url)) }
                return
            }
            val filename = PortalWebGuard.safeDownloadName(requestedName)
            Log.d(TAG, "downloadFile called: url=$url, filename=$filename")

            CoroutineScope(Dispatchers.Main).launch {
                // Show "Downloading..." notification on main thread FIRST
                val notificationId = notificationManager.showDownloadStarted(filename)
                Log.d(TAG, "Download notification started with ID: $notificationId")

                // Now do the actual download on IO thread
                withContext(Dispatchers.IO) {
                try {
                    val client = PortalWebGuard.pairedOnlyDownloadClient(pairedOrigin)

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
                                evalInPortal(jsCall("onAndroidDownloadComplete", filename, true))
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
                        evalInPortal(jsCall("onAndroidDownloadComplete", filename, false, e.message ?: "Unknown error"))
                    }
                }
            }  // end withContext(Dispatchers.IO)
            }  // end CoroutineScope(Dispatchers.Main).launch
        }

        // Kept for backward compatibility with "AndroidMic" usage
        @Suppress("unused")
        @JavascriptInterface
        fun vibrate(duration: Int) {
            if (!bridgeAllowed("vibrate")) return
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
            if (!bridgeAllowed("startRecording")) return
            Log.d(TAG, "startRecording called - isRecording=$isRecording, isStreaming=$isStreaming")
            runOnUiThread {
                Log.d(TAG, "startRecording runOnUiThread - checking permissions")
                if (!hasMicPerm()) {
                    Log.e(TAG, "startRecording - mic permission denied")
                    web.post {
                        evalInPortal(jsCall("onNativeMicError", "Permission denied"))
                    }
                    return@runOnUiThread
                }

                if (isRecording) {
                    Log.w(TAG, "startRecording - already recording, rejecting")
                    web.post { evalInPortal(jsCall("onNativeMicError", "Already recording")) }
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
                                web.post { evalInPortal(jsCall("onNativeMicError", "Recording error code: $what")) }
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
                    web.post { evalInPortal(jsCall("onNativeMicStart")) }

                } catch (e: Exception) {
                    Log.e(TAG, "startRecording - EXCEPTION: ${e.message}", e)
                    e.printStackTrace()
                    web.post { evalInPortal(jsCall("onNativeMicError", "Start failed: ${e.message}")) }
                    stopRecordingInternal()
                }
            }
        }

        @Suppress("unused")
        @JavascriptInterface
        fun stopRecording() {
            if (!bridgeAllowed("stopRecording")) return
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
            if (!bridgeAllowed("stopRecordingAndGetAudio")) return
            Log.d(TAG, "stopRecordingAndGetAudio called")
            runOnUiThread { stopRecordingAndReturnAudio() }
        }

        @Suppress("unused")
        @JavascriptInterface
        fun isRecording(): Boolean = bridgeAllowed("isRecording") && isRecording

        /**
         * Release the microphone without uploading to Whisper.
         * Call this before using getUserMedia in WebView to avoid "mic busy" errors.
         * JS: AndroidMic.releaseMic()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun releaseMic() {
            if (!bridgeAllowed("releaseMic")) return
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
                        evalInPortal(jsCall("onNativeMicReleased"))
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
            if (!bridgeAllowed("startAudioStreaming")) return
            Log.d(TAG, "startAudioStreaming called")
            if (!hasMicPerm()) {
                web.post {
                    evalInPortal(jsCall("onNativeStreamError", "Permission denied"))
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

            // Held HERE until the streaming thread takes it, and released here (in the finally
            // below) only if it never got that far — never read, so no read() can be in flight.
            var notYetStreaming: AudioRecord? = null
            try {
                // Configure AudioRecord for 24kHz mono PCM16 (OpenAI Realtime format)
                val sampleRate = 24000
                val channelConfig = AudioFormat.CHANNEL_IN_MONO
                val audioFormat = AudioFormat.ENCODING_PCM_16BIT
                val bufferSize = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat) * 2

                Log.d(TAG, "Creating AudioRecord: sampleRate=$sampleRate, bufferSize=$bufferSize")

                val record = AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    sampleRate,
                    channelConfig,
                    audioFormat,
                    bufferSize
                )
                notYetStreaming = record

                if (record.state != AudioRecord.STATE_INITIALIZED) {
                    Log.e(TAG, "AudioRecord failed to initialize")
                    web.post {
                        evalInPortal(jsCall("onNativeStreamError", "Failed to initialize audio capture"))
                    }
                    return
                }

                record.startRecording()
                val live = AtomicBoolean(true)
                streamingLive = live
                isStreaming = true

                Log.d(TAG, "AudioRecord started, beginning streaming thread")

                // Notify JavaScript that streaming started
                web.post {
                    evalInPortal(jsCall("onNativeStreamStart"))
                }

                // Start streaming thread
                val thread = Thread {
                    val buffer = ShortArray(bufferSize / 2)  // 16-bit samples
                    val byteBuffer = ByteArray(bufferSize)

                    try {
                        while (live.get()) {
                            try {
                                // The LOCAL record, never a field a stop could null mid-read.
                                val readCount = record.read(buffer, 0, buffer.size)
                                // A stop that landed while this read was in flight ends the session
                                // HERE: this chunk belongs to a session that is over.
                                if (!live.get()) break

                                if (readCount > 0) {
                                    // Convert shorts to bytes (little-endian PCM16)
                                    for (i in 0 until readCount) {
                                        byteBuffer[i * 2] = (buffer[i].toInt() and 0xFF).toByte()
                                        byteBuffer[i * 2 + 1] = (buffer[i].toInt() shr 8 and 0xFF).toByte()
                                    }

                                    // Encode to base64
                                    val base64Data = Base64.encodeToString(byteBuffer, 0, readCount * 2, Base64.NO_WRAP)

                                    // Send to JavaScript — unless this session has ended by the time
                                    // the UI thread gets to it: a chunk must not reach a newer
                                    // session's handler, nor a WebView that onDestroy tore down —
                                    // nor a page that is no longer the paired box (evalInPortal).
                                    runOnUiThread {
                                        if (live.get()) {
                                            evalInPortal(jsCall("onNativeAudioChunk", base64Data))
                                        }
                                    }
                                }
                            } catch (e: Exception) {
                                Log.e(TAG, "Error reading audio: ${e.message}")
                                break
                            }
                        }
                    } finally {
                        // Stopped and released HERE, on the SAME thread as read() — see
                        // stopAudioStreamingInternal for why it must not happen anywhere else.
                        try { record.stop() } catch (_: Exception) {}
                        try { record.release() } catch (_: Exception) {}
                        Log.d(TAG, "Streaming thread ended")
                    }
                }
                streamingThread = thread
                thread.start()
                notYetStreaming = null

            } catch (e: Exception) {
                Log.e(TAG, "Error starting audio streaming: ${e.message}", e)
                web.post {
                    evalInPortal(jsCall("onNativeStreamError", e.message ?: "Unknown error"))
                }
                stopAudioStreamingInternal()
            } finally {
                notYetStreaming?.let { r -> try { r.release() } catch (_: Exception) {} }
            }
        }

        /**
         * Stop streaming audio.
         * JS: AndroidMic.stopAudioStreaming()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun stopAudioStreaming() {
            if (!bridgeAllowed("stopAudioStreaming")) return
            Log.d(TAG, "stopAudioStreaming called")
            runOnUiThread {
                stopAudioStreamingInternal()
                web.post {
                    evalInPortal(jsCall("onNativeStreamStop"))
                }
            }
        }

        /**
         * Check if currently streaming audio.
         * JS: AndroidMic.isAudioStreaming()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun isAudioStreaming(): Boolean = bridgeAllowed("isAudioStreaming") && isStreaming

        // ============ ADB QUICK PAIR HELPERS ============

        /**
         * Get the device's Tailscale IP address by reading the tun0/tailscale0 network interface.
         * Returns the IP as a string, or empty string if not found.
         * JS: AndroidBridge.getDeviceTailscaleIp()
         */
        @Suppress("unused")
        @JavascriptInterface
        fun getDeviceTailscaleIp(): String {
            if (!bridgeAllowed("getDeviceTailscaleIp")) return ""
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
            if (!bridgeAllowed("openWirelessDebuggingSettings")) return
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
     * Internal function to stop audio streaming.
     *
     * Only SIGNALS the streaming thread, then waits (bounded) for it: the thread stops and releases
     * the AudioRecord in its own finally, on the thread that reads it. Releasing it here, on the UI
     * thread, while that thread was blocked in read() — interrupt() does not unblock a native read —
     * is the native abort in AudioRecord::releaseBuffer (feedback_android_audiorecord_race). The
     * join keeps the release synchronous for the callers that need the mic free on return:
     * startRecording's MediaRecorder, and releaseMic ahead of the WebView's getUserMedia.
     */
    private fun stopAudioStreamingInternal() {
        Log.d(TAG, "stopAudioStreamingInternal called")
        isStreaming = false
        streamingLive?.set(false)
        streamingLive = null

        val thread = streamingThread ?: return
        streamingThread = null
        try {
            // A read returns within one buffer period (getMinBufferSize samples — a fraction of
            // this bound on any device), so 500 ms is the ceiling, not the expected wait.
            thread.join(500)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }
        if (thread.isAlive) {
            Log.w(TAG, "Streaming thread still in read() after 500ms — it releases the mic when that read returns")
        } else {
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
            web.post { evalInPortal(jsCall("onNativeMicError", "Stop/Release failed: ${e.message}")) }
        } finally {
            mediaRecorder = null
            isRecording = false

            val currentAudioFile = audioFile
            if (currentAudioFile != null && currentAudioFile.exists() && currentAudioFile.length() > 0) {
                Log.d(TAG, "stopRecordingInternal - Audio file ready: ${currentAudioFile.absolutePath}, Size: ${currentAudioFile.length()}")
                uploadToWhisper(currentAudioFile)
            } else {
                Log.w(TAG, "stopRecordingInternal - Audio file is null, doesn't exist, or is empty. Skipping upload.")
                web.post { evalInPortal(jsCall("onNativeMicError", "No valid audio file recorded")) }
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
            web.post { evalInPortal(jsCall("onNativeMicError", "Not recording")) }
            return
        }

        try {
            mediaRecorder?.apply {
                try { stop() } catch (e: IllegalStateException) { e.printStackTrace() }
                try { release() } catch (e: Exception) { e.printStackTrace() }
            }
        } catch (e: Exception) {
            e.printStackTrace()
            web.post { evalInPortal(jsCall("onNativeMicError", "Stop/Release failed: ${e.message}")) }
        } finally {
            mediaRecorder = null
            isRecording = false

            val currentAudioFile = audioFile
            if (currentAudioFile != null && currentAudioFile.exists() && currentAudioFile.length() > 0) {
                Log.d(TAG, "Audio file ready for return: ${currentAudioFile.absolutePath}, Size: ${currentAudioFile.length()}")
                returnAudioAsBase64(currentAudioFile)
            } else {
                Log.w(TAG, "Audio file is null, doesn't exist, or is empty.")
                web.post { evalInPortal(jsCall("onNativeMicError", "No valid audio file recorded")) }
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
                    evalInPortal(jsCall("onNativeMicAudio", base64Data, mimeType, fileName))
                    Log.d(TAG, "Called onNativeMicAudio callback")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error reading audio file: ${e.message}", e)
                withContext(Dispatchers.Main) {
                    evalInPortal(jsCall("onNativeMicError", "Failed to read audio: ${e.message}"))
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
                                // A transcript can say anything: JSON-quoted by jsCall, not hand-escaped.
                                evalInPortal(jsCall("onNativeMicTranscript", text))
                            } else {
                                evalInPortal(jsCall("onNativeMicError", "No transcript received from server"))
                            }
                        } catch (e: org.json.JSONException) {
                            evalInPortal(jsCall("onNativeMicError", "Failed to parse server response"))
                            e.printStackTrace()
                        }
                    } else {
                        val errorDetail = responseBody ?: "Unknown error"
                        evalInPortal(jsCall("onNativeMicError", "Upload failed: $responseCode - ${errorDetail.take(100)}"))
                    }
                }

            } catch (e: IOException) {
                e.printStackTrace()
                withContext(Dispatchers.Main) {
                    evalInPortal(jsCall("onNativeMicError", "Network or file error: ${e.message}"))
                }
            } catch (e: Exception) {
                e.printStackTrace()
                withContext(Dispatchers.Main) {
                    evalInPortal(jsCall("onNativeMicError", "An unexpected error occurred: ${e.message}"))
                }
            } finally {
                val deleted = file.delete()
                Log.d("UploadWhisper", "Deleted temp file ${file.name}: $deleted")
            }
        }
    }

    /**
     * The Portal's renderer died: recreate the activity for a fresh WebView, but only as
     * often as [portalRendererCrashes] allows, so a page that kills its renderer on every
     * load is not reloaded forever. Past that the dead WebView is taken off screen (it is
     * destroyed in onDestroy) and the user chooses Reload or Close.
     */
    private fun onPortalRendererGone() {
        if (portalRendererCrashes.recordCrash()) {
            recreate()
            return
        }
        (web.parent as? android.view.ViewGroup)?.removeView(web)
        if (isFinishing || isDestroyed) return
        android.app.AlertDialog.Builder(this)
            .setMessage("The BlackBox page crashed.")
            .setPositiveButton("Reload") { _, _ ->
                portalRendererCrashes.reset()
                recreate()
            }
            .setNegativeButton("Close") { _, _ -> finish() }
            .setCancelable(false)
            .show()
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
        // Before the WebView goes: a streaming thread left running would hold the mic open and keep
        // posting chunks to a destroyed WebView, and the recreated activity (Fold cover/main switch)
        // could never stop it.
        stopAudioStreamingInternal()
        web.destroy()
    }
}
