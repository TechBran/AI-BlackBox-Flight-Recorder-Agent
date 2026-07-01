package com.aiblackbox.portal.overlay

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.os.Build
import android.util.Log
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import java.io.ByteArrayOutputStream

/**
 * The consented on-device phone-control AccessibilityService (Phase 4).
 *
 * This is the user-enabled service that lets the on-device BlackBox (Gemma)
 * agent read the screen and perform taps/typing **on the owner's behalf** when
 * they ask it to control their phone. It is opt-in (the user enables it from
 * system Accessibility settings) and can be turned off at any time.
 *
 * **Task 4.1 scope — skeleton + enablement ONLY.** This class establishes the
 * service registration and the [Companion.instance] seam; it does NOT yet read
 * screen content, walk the node tree for data, capture screenshots, dispatch
 * gestures, or handle credentials. Those land in later tasks:
 *  - 4.2: [UiTreeReader] reads `instance?.rootInActiveWindow` (with password
 *    redaction) for the `read_screen` capability.
 *  - 4.3: Actuators call `instance?.performGlobalAction(...)` /
 *    `instance?.dispatchGesture(...)` for tap/type/swipe/scroll/back/home.
 *  - 4.6: the YOLO-vs-Permission autonomy gate wraps the actuators.
 *  - 4.7: credential handoff via Credential Manager / Autofill.
 *
 * The `onServiceConnected` body only LOGS the active window package as a
 * connectivity proof — no content extraction or redaction happens here.
 */
class BlackBoxA11yService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "BlackBoxA11yService connected")
        // Connectivity proof only — logs the foreground package name. No screen
        // content is read or extracted here; the UI-tree reader lands in 4.2.
        rootInActiveWindow?.let { Log.i(TAG, "root window pkg=${it.packageName}") }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // No-op for now. Event filtering/handling lands in later phase-4 tasks.
        if (event != null) {
            Log.d(TAG, "event type=${event.eventType}")
        }
    }

    override fun onInterrupt() {
        // No-op.
    }

    /**
     * (M1.2) SILENT screenshot via [AccessibilityService.takeScreenshot] — the frontier
     * observation's optional vision channel. Unlike the MediaProjection path
     * ([OverlayService.captureScreenPng]) this needs NO per-capture system dialog and NO
     * projection consent: it uses the accessibility grant the user already made (requires
     * `android:canTakeScreenshot` in the service config, added in M0.5, and API 30+).
     *
     * Delivers PNG bytes to [callback], or null when unavailable (pre-API-30, or the
     * framework refused / returned no frame). NEVER throws. The caller
     * ([A11yScreenCapture]) applies the password-refusal gate BEFORE calling this, and
     * the loop only asks for it under the tree-first cadence — so a credential screen is
     * never captured.
     */
    fun takeScreenshotPng(callback: (ByteArray?) -> Unit) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            callback(null)
            return
        }
        try {
            takeScreenshot(
                Display.DEFAULT_DISPLAY,
                mainExecutor,
                object : AccessibilityService.TakeScreenshotCallback {
                    override fun onSuccess(screenshot: AccessibilityService.ScreenshotResult) {
                        val bytes = try {
                            val buffer = screenshot.hardwareBuffer
                            val bitmap = Bitmap.wrapHardwareBuffer(buffer, screenshot.colorSpace)
                            val out = try {
                                if (bitmap == null) {
                                    null
                                } else {
                                    ByteArrayOutputStream().use { stream ->
                                        bitmap.compress(Bitmap.CompressFormat.PNG, 100, stream)
                                        stream.toByteArray()
                                    }
                                }
                            } finally {
                                // Release the graphics buffer regardless of compress outcome.
                                @Suppress("DEPRECATION")
                                try { buffer.close() } catch (_: Exception) {}
                                bitmap?.recycle()
                            }
                            out
                        } catch (e: Exception) {
                            // Never leak content into logs; class name only.
                            Log.w(TAG, "takeScreenshot decode failed (${e.javaClass.simpleName})")
                            null
                        }
                        callback(bytes)
                    }

                    override fun onFailure(errorCode: Int) {
                        Log.w(TAG, "takeScreenshot failed (code=$errorCode)")
                        callback(null)
                    }
                },
            )
        } catch (e: Exception) {
            Log.w(TAG, "takeScreenshot threw (${e.javaClass.simpleName})")
            callback(null)
        }
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    companion object {
        private const val TAG = "BlackBoxA11yService"

        /**
         * Live-instance seam. Set in [onServiceConnected], cleared in
         * [onUnbind]/[onDestroy]. Tasks 4.2 (reader) and 4.3 (actuators) reach
         * the connected service through this rather than rebinding.
         */
        @Volatile
        var instance: BlackBoxA11yService? = null

        /** True when the service is currently connected (user has enabled it). */
        fun isConnected(): Boolean = instance != null
    }
}

/**
 * Pure parser for [android.provider.Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES]:
 * returns whether *this app's* [BlackBoxA11yService] is one of the enabled
 * services in the colon-separated [enabledServicesSetting] string.
 *
 * Android stores enabled services as `pkg/component`, where the component can be
 * fully qualified (`pkg/pkg.overlay.BlackBoxA11yService`) or use the short
 * relative form (`pkg/.overlay.BlackBoxA11yService`). Both match. Kept free of
 * any framework access (the live `Settings.Secure` read stays in the caller) so
 * it is trivially unit-testable.
 *
 * @param enabledServicesSetting the raw colon-separated setting value, or null.
 * @param packageName this app's package (e.g. `com.aiblackbox.portal`).
 * @param serviceClass the fully-qualified service class name
 *   (e.g. `com.aiblackbox.portal.overlay.BlackBoxA11yService`).
 */
fun isAccessibilityServiceEnabled(
    enabledServicesSetting: String?,
    packageName: String,
    serviceClass: String,
): Boolean {
    if (enabledServicesSetting.isNullOrEmpty()) return false

    // The two component forms Android may persist for our service.
    val longForm = "$packageName/$serviceClass"
    val shortForm = if (serviceClass.startsWith("$packageName.")) {
        "$packageName/${serviceClass.removePrefix(packageName)}" // pkg/.overlay.Foo
    } else {
        null
    }

    return enabledServicesSetting
        .split(':')
        .map { it.trim() }
        .any { it.equals(longForm, ignoreCase = true) || (shortForm != null && it.equals(shortForm, ignoreCase = true)) }
}
