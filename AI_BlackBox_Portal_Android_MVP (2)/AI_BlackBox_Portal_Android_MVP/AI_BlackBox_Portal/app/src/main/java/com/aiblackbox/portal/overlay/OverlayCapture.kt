package com.aiblackbox.portal.overlay

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.util.Base64
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private const val TAG = "OverlayService"

/**
 * The overlay's screen capture, out of OverlayService (audit A02-overlay-service-12, rank 76):
 * the MediaProjection, the persistent screenshot display that keeps it valid on Android 14+,
 * screenshots to a file, single PNG frames for on-device vision, JPEG frames for the live
 * screen share, screen recording, and the projection refresh. Moved verbatim; what it shows
 * or attaches goes through [Host], which the service implements.
 */
internal class OverlayCapture(
    private val context: Context,
    private val handler: Handler,
    private val windowManager: () -> WindowManager,
    private val host: Host,
) {
    /** What capture needs from the overlay: its status line, toasts and windows. */
    interface Host {
        fun updateStatus(status: String)
        fun showToast(message: String)
        /** Hide the bubble and panel so they are not in the frame; true when the panel was open. */
        fun hideOverlayForCapture(): Boolean
        fun restoreOverlayAfterCapture(wasExpanded: Boolean)
        /** A screenshot to attach ([file] non-null) or a failed one. */
        fun onScreenshot(file: File?)
        /** Recording began: the overlay hides and the floating stop button shows. */
        fun onRecordingStarted()
        /** The finished recording to attach. */
        fun onRecordingSaved(file: File)
        /** Recording ended: the stop button goes and the panel comes back. */
        fun onRecordingStopped()
        /** The projection is being refreshed: the service restarts with a new one. */
        fun stopService()
    }

    var mediaProjection: MediaProjection? = null
        private set

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

    var isRecording = false
        private set

    // Track current VirtualDisplay mode to avoid conflicts
    private enum class DisplayMode { NONE, SCREENSHOT, RECORDING }
    private var currentDisplayMode = DisplayMode.NONE

    // Track when recording stopped to add delay before screenshot
    private var lastRecordingStopTime: Long = 0L

    // Track if MediaProjection has been invalidated (Android 14+ releases token after recording)
    var mediaProjectionInvalidated = false
        private set

    private var screenWidth = 0
    private var screenHeight = 0
    private var screenDensity = 0

    /** Whether a valid projection exists to capture from. */
    fun available(): Boolean = mediaProjection != null && !mediaProjectionInvalidated

    /** Whether the persistent display's reader exists (frames can be read now). */
    fun hasPersistentReader(): Boolean = persistentImageReader != null

    /**
     * Take the MediaProjection the starting screen was granted ([resultCode], [data]) and
     * watch it: a stop marks it invalid (the next capture asks for a refresh), a resize
     * drops the stale-sized display. Below Android 14 the persistent display is created
     * right away; 14+ waits for the first screenshot or recording.
     */
    fun start(resultCode: Int, data: Intent) {
        try {
            val projectionManager = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            mediaProjection = projectionManager.getMediaProjection(resultCode, data)

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
                        host.showToast("Screen capture session has ended.")
                    }
                }

                // (M5.4) Android 14+: the captured content resolution changed (fold/unfold,
                // rotate, multi-window resize). Invalidate the stale-sized capture + refresh
                // the cached dims so no coordinate is derived from a stale resolution.
                override fun onCapturedContentResize(width: Int, height: Int) {
                    onCapturedContentResized(width, height)
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
            host.showToast("Screen capture unavailable: ${e.message}")
        }
    }

    /** The service is going away: release the persistent display and stop the projection. */
    fun release() {
        // Cleanup persistent screenshot display (only now, at service destruction)
        persistentScreenshotDisplay?.release()
        persistentScreenshotDisplay = null
        persistentImageReader?.close()
        persistentImageReader = null

        mediaProjection?.stop()
        mediaProjection = null
    }

    /**
     * (M5.4) Refresh the cached capture dimensions from the CURRENT WINDOW METRICS.
     *
     * On API 30+ uses [WindowManager.getCurrentWindowMetrics] `.bounds` — the correct extent on a
     * tablet / unfolded Fold / multi-window / rotated screen — instead of the deprecated
     * `defaultDisplay.getMetrics`. Pre-30 keeps the legacy `defaultDisplay.getMetrics` path (so
     * older devices are unchanged). Density comes from `resources.displayMetrics` (WindowMetrics
     * exposes no density pre-34). Called on create AND whenever [MediaProjection.Callback.onCapturedContentResize]
     * reports a resolution change, so the VirtualDisplay/ImageReader are never sized to stale dims
     * (which would misplace every coordinate). Never throws.
     */
    fun refreshScreenMetrics() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val bounds = windowManager().currentWindowMetrics.bounds
                screenWidth = bounds.width()
                screenHeight = bounds.height()
            } else {
                val metrics = DisplayMetrics()
                @Suppress("DEPRECATION")
                windowManager().defaultDisplay.getMetrics(metrics)
                screenWidth = metrics.widthPixels
                screenHeight = metrics.heightPixels
            }
            screenDensity = context.resources.displayMetrics.densityDpi
        } catch (e: Exception) {
            Log.w(TAG, "refreshScreenMetrics failed (${e.javaClass.simpleName}); keeping ${screenWidth}x${screenHeight}")
        }
    }

    /**
     * (M5.4) Android 14+ `onCapturedContentResize` handler: the captured content changed
     * resolution (fold/unfold, rotation, multi-window resize). INVALIDATE the cached capture:
     * update the dims from the new (w,h) AND release the persistent screenshot display + reader so
     * the NEXT capture rebuilds an ImageReader/VirtualDisplay at the new size — never leaving a
     * screenshot (and the coordinates derived from it) at a stale resolution. The cloud loop
     * re-observes each step (M2) and, on a fold, the FoldingFeatureMonitor posture-change flag
     * additionally tells it to re-observe before its next coordinate action. Runs on [handler].
     */
    private fun onCapturedContentResized(width: Int, height: Int) {
        handler.post {
            Log.d(TAG, "onCapturedContentResize: ${screenWidth}x${screenHeight} -> ${width}x${height}; invalidating capture")
            screenWidth = width
            screenHeight = height
            // Density can change with the display; re-read it too.
            runCatching { screenDensity = context.resources.displayMetrics.densityDpi }
            // Drop the stale-sized persistent display + reader; next capture recreates at new dims.
            runCatching { persistentScreenshotDisplay?.release() }
            persistentScreenshotDisplay = null
            runCatching { persistentImageReader?.close() }
            persistentImageReader = null
        }
    }

    /**
     * Creates a persistent VirtualDisplay for screenshots that stays alive for the service lifetime.
     * This is the key to keeping MediaProjection valid on Android 14+.
     * By never releasing this VirtualDisplay, subsequent createVirtualDisplay() calls (for recording) succeed.
     *
     * @param onComplete Optional callback invoked when setup completes (success or final failure).
     *                   Boolean parameter is true if setup succeeded.
     */
    fun setupPersistentScreenshotDisplay(onComplete: ((Boolean) -> Unit)? = null) {
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

    // ==================== Screenshot ====================

    fun takeScreenshot() {
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
        host.updateStatus("Preparing capture...")

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
        host.updateStatus("Capturing...")
        host.showToast("Capturing screenshot...")

        // Hide overlay temporarily
        val wasExpanded = host.hideOverlayForCapture()

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
                    host.restoreOverlayAfterCapture(wasExpanded)
                    host.onScreenshot(file)
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
            // (M4) A fold/rotate at a capture boundary fires onCapturedContentResized(), which
            // closes + nulls persistentImageReader between this block being scheduled and run — our
            // captured `reader` would then be CLOSED and acquireLatestImage() would throw
            // IllegalStateException. Detect the swap up front and return a clean null (the next
            // capture rebuilds the reader at the new size). MediaProjection stays valid; benign.
            if (persistentImageReader !== reader) {
                Log.d(TAG, "attemptImageCaptureFromPersistent: reader reset mid-capture (fold/rotate) -> null")
                callback(null)
                return@postDelayed
            }
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
                    val file = File(context.cacheDir, "screenshot_$timestamp.png")
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

    // ==================== Single-frame PNG capture (Task W4 — on-device vision) ====================

    /**
     * Capture ONE screen frame as PNG BYTES (in memory, never a file), for the
     * on-device "look at my screen" vision path (Task W4.2). REUSES the existing
     * MediaProjection + the persistent ImageReader that already power the
     * screenshot/screen-share features — no new projection, no new consent flow
     * (the projection consent is already granted when the overlay is running).
     *
     * Unlike [takeScreenshot] this does NOT write a file, attach a screenshot, or
     * toast — the bytes are EPHEMERAL (handed to the model's prompt and dropped). It
     * DOES hide the floating overlay briefly so the BlackBox bubble isn't in the
     * captured frame, then restores it.
     *
     * Calls [callback] on the main thread with the PNG bytes, or null when capture
     * is unavailable (no/invalid MediaProjection, or no frame after retries). Never
     * throws.
     */
    fun captureFramePngBytes(callback: (ByteArray?) -> Unit) {
        if (mediaProjection == null || mediaProjectionInvalidated) {
            Log.w(TAG, "captureFramePngBytes: no valid MediaProjection -> null")
            requestMediaProjectionRefresh()
            callback(null)
            return
        }

        val proceed = {
            // Hide overlay so the bubble/panel isn't in the frame.
            val wasExpanded = host.hideOverlayForCapture()
            handler.postDelayed({
                attemptFramePngFromPersistent(attempt = 1, maxAttempts = 3) { bytes ->
                    handler.post {
                        host.restoreOverlayAfterCapture(wasExpanded)
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
            // (M4) A fold/rotate at a capture boundary fires onCapturedContentResized(), which
            // closes + nulls persistentImageReader between this block being scheduled and run —
            // our captured `reader` would then be CLOSED and acquireLatestImage() would throw
            // IllegalStateException (caught below, but churning retries). Detect the swap up front
            // and return a clean null (the loop re-observes at the new size). MediaProjection stays
            // valid; this is benign.
            if (persistentImageReader !== reader) {
                Log.d(TAG, "captureFramePngBytes: reader reset mid-capture (fold/rotate) -> null")
                callback(null)
                return@postDelayed
            }
            try {
                val image = reader.acquireLatestImage()
                if (image != null) {
                    // Fix 3 (final-pass review): ALWAYS close the acquired Image, even
                    // if copyPixelsFromBuffer / compress throws after acquire -- an
                    // unclosed Image leaks an ImageReader buffer slot under repeated
                    // faults and eventually starves the reader.
                    val bytes = try {
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

                        val cropped = Bitmap.createBitmap(bitmap, 0, 0, screenWidth, screenHeight)
                        bitmap.recycle()

                        val baos = ByteArrayOutputStream()
                        cropped.compress(Bitmap.CompressFormat.PNG, 100, baos)
                        cropped.recycle()
                        baos.toByteArray()
                    } finally {
                        image.close()
                    }
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

    // ==================== Live screen share frames ====================

    /** The persistent display's newest frame as base64 JPEG (quality 70), or null when none is ready. */
    fun latestFrameJpegBase64(): String? {
        val reader = persistentImageReader ?: return null
        val image = reader.acquireLatestImage() ?: return null

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

            // Cleanup
            if (croppedBitmap != bitmap) {
                croppedBitmap.recycle()
            }
            bitmap.recycle()
            return base64Frame
        } catch (e: Exception) {
            Log.e(TAG, "Error capturing frame", e)
            return null
        } finally {
            image.close()
        }
    }

    // ==================== Projection refresh ====================

    /**
     * Request MediaProjection refresh by broadcasting to PortalActivity.
     * This stops the overlay service and requests a new MediaProjection permission.
     * Used on Android 14+ where MediaProjection becomes invalid after use.
     * @param forRecording If true, the new MediaProjection will be used for recording (skip persistent display)
     */
    fun requestMediaProjectionRefresh(forRecording: Boolean = false) {
        Log.d(TAG, "Requesting MediaProjection refresh (forRecording=$forRecording)...")
        host.showToast(if (forRecording) "Requesting recording permission..." else "Refreshing capture permission...")

        // Broadcast to PortalActivity to restart the overlay with fresh permission
        val intent = Intent(OverlayService.ACTION_REFRESH_PROJECTION)
        intent.setPackage(context.packageName)
        intent.putExtra(OverlayService.EXTRA_RECORDING_MODE, forRecording)  // Pass recording mode flag
        context.sendBroadcast(intent)

        // Stop this service - PortalActivity will restart it with new permission
        handler.postDelayed({
            host.stopService()
        }, 500)
    }

    // ==================== Recording ====================

    fun toggleRecording() {
        if (isRecording) {
            stopRecording()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        if (mediaProjection == null) {
            host.showToast("Recording permission not granted")
            return
        }

        // Android 14+ restriction: Can only call createVirtualDisplay() ONCE per MediaProjection
        // If persistent screenshot display exists, we need to switch modes
        if (persistentScreenshotDisplay != null && Build.VERSION.SDK_INT >= 34) {
            Log.d(TAG, "Android 14+ detected: Switching from screenshot mode to recording mode")
            host.showToast("Switching to recording mode...")

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
        host.updateStatus("Preparing...")

        handler.postDelayed({
            startRecordingInternal()
        }, delayMs)
    }

    private fun startRecordingInternal() {
        if (mediaProjection == null) {
            Log.w(TAG, "startRecordingInternal: mediaProjection is null, cannot record.")
            host.showToast("Recording permission lost")
            host.updateStatus("Ready")
            // Because the projection is null, we should trigger a refresh.
            requestMediaProjectionRefresh()
            return
        }

        Log.d(TAG, "Starting screen recording...")
        host.updateStatus("Recording...")

        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val outputFile = File(context.cacheDir, "recording_$timestamp.mp4")

        try {
            mediaRecorder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                MediaRecorder(context)
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
                host.updateStatus("Ready")

                // Android 14+ FIX: Request a NEW MediaProjection for recording mode
                // User will grant permission once, then recording works for all future attempts
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                    handler.post {
                        requestMediaProjectionRefresh(forRecording = true)  // Pass recording mode flag
                    }
                } else {
                    // Older Android: Try to recover with screenshot display
                    handler.post {
                        host.showToast("Recording failed. Please restart overlay.")
                        setupPersistentScreenshotDisplay { success ->
                            if (success) Log.d(TAG, "Re-established screenshot display after recording failure.")
                        }
                    }
                }
                return
            }


            if (recordingDisplay == null) {
                Log.e(TAG, "Failed to create recording VirtualDisplay (was null)")
                host.showToast("Recording failed - display error")
                mediaRecorder?.release()
                mediaRecorder = null
                currentDisplayMode = DisplayMode.NONE
                host.updateStatus("Ready")
                return
            }

            currentDisplayMode = DisplayMode.RECORDING
            Log.d(TAG, "Recording VirtualDisplay created (separate from screenshot display)")

            mediaRecorder?.start()
            isRecording = true

            // Hide overlay UI and show floating stop button
            host.onRecordingStarted()

            host.showToast("Recording started - tap stop button when done")
            Log.d(TAG, "Recording started: ${outputFile.absolutePath}")

        } catch (e: Exception) {
            Log.e(TAG, "Error starting recording", e)
            host.showToast("Recording failed: ${e.message}")
            host.updateStatus("Recording failed")
            mediaRecorder?.release()
            mediaRecorder = null
            virtualDisplay?.release()
            virtualDisplay = null
        }
    }

    fun stopRecording() {
        if (!isRecording) return

        Log.d(TAG, "Stopping recording...")
        host.updateStatus("Saving recording...")

        try {
            mediaRecorder?.stop()
            mediaRecorder?.release()

            // Find the recording file
            val recordings = context.cacheDir.listFiles { file ->
                file.name.startsWith("recording_") && file.name.endsWith(".mp4")
            }?.sortedByDescending { it.lastModified() }

            val recordingFile = recordings?.firstOrNull()
            if (recordingFile != null && recordingFile.exists()) {
                // Clear old recording and set new one
                host.onRecordingSaved(recordingFile)
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

            host.showToast("Recording saved!")
            Log.d(TAG, "Recording stopped, mode reset to NONE.")
            host.updateStatus("Ready")

            host.onRecordingStopped()
        }
    }
}
