package com.aiblackbox.portal.data.remote

import android.content.Context
import com.aiblackbox.portal.overlay.DeviceCapabilities
import com.aiblackbox.portal.overlay.PreferredScreenCapture
import com.aiblackbox.portal.overlay.ScreenCaptureResult
import com.aiblackbox.portal.overlay.UiNode
import com.aiblackbox.portal.overlay.UiTreeReader

/**
 * (M1.2) Builds a schema-conforming [Observation] from the LIVE device: the
 * password-redacted `ui_tree` ([UiTreeReader.readNodes]), the `device_capability`
 * ([DeviceCapabilities.detect]), and an OPTIONAL silent screenshot under the tree-first
 * cadence. This is the DEVICE-SIDE `buildObservation()` capability that
 * [RemoteControlServer]'s `/stream` + `/action` follow-on emit on demand. It does NOT
 * drive the observe/act cadence loop — that is the cloud brain (M2); here we only
 * produce one observation whenever asked.
 *
 * ## Tree-first cadence
 * [shouldCaptureScreenshot] decides per call: a screenshot is captured ONLY when the
 * device advertises `hasScreenshot` AND (the caller explicitly requested vision OR the
 * tree is blind/sparse). A rich tree ships with NO picture (less cloud data + latency);
 * an `hasScreenshot=false` device (XR) never captures.
 *
 * ## Silent + redacted capture
 * The production capture ([PreferredScreenCapture]) prefers the silent
 * `AccessibilityService.takeScreenshot()` over MediaProjection and keeps the
 * password-refusal gate — a refusal (or any unavailability) yields NO screenshot
 * (tree-only). NOTE: the gate is FOCUSED-password-field only — it is not complete
 * pixel redaction; a visible-but-unfocused password field can still enter a frame
 * (plan's documented open question, deferred). Do not over-claim full redaction here.
 *
 * ## Purity
 * Every framework touch-point is a constructor seam (tree read / capability / capture /
 * base64 / clock), so [build] unit-tests with plain fakes (no Android). [fromDevice]
 * wires the real device sources.
 */
class ObservationBuilder(
    private val readTree: () -> List<UiNode>,
    private val capability: () -> DeviceCapabilities,
    private val captureScreenshot: suspend () -> ByteArray?,
    private val encodeBase64: (ByteArray) -> String = { bytes ->
        android.util.Base64.encodeToString(bytes, android.util.Base64.NO_WRAP)
    },
    private val clock: () -> Long = { System.currentTimeMillis() },
) {

    /**
     * Produce one observation. [requestScreenshot] = the model explicitly asked for
     * vision this step; otherwise the tree-first rule ([shouldCaptureScreenshot]) still
     * captures a screenshot when the tree is blind. A capture that comes back
     * null/empty (refused, unavailable) simply yields a tree-only observation — never a
     * failure.
     */
    suspend fun build(requestScreenshot: Boolean = false): Observation {
        val tree = readTree()
        val cap = capability()
        val screenshotB64: String? =
            if (shouldCaptureScreenshot(cap, tree, requestScreenshot)) {
                captureScreenshot()
                    ?.takeIf { it.isNotEmpty() }
                    ?.let { encodeBase64(it) }
            } else {
                null
            }
        return Observation(
            uiTree = tree,
            deviceCapability = cap,
            screenshot = screenshotB64,
            timestamp = clock(),
        )
    }

    companion object {
        /**
         * Production wiring from an app [Context]: reads the live redacted tree, detects
         * the capability, and captures via [PreferredScreenCapture] (silent a11y path
         * first, MediaProjection fallback; password-gated). A refused/unavailable capture
         * degrades to tree-only.
         */
        fun fromDevice(context: Context): ObservationBuilder {
            val appContext = context.applicationContext
            return ObservationBuilder(
                readTree = { UiTreeReader.fromService().readNodes() },
                capability = { DeviceCapabilities.detect(appContext) },
                captureScreenshot = {
                    when (val r = PreferredScreenCapture().capture()) {
                        is ScreenCaptureResult.Success -> r.pngBytes
                        // RefusedPassword / Unavailable -> tree-only (no screenshot).
                        else -> null
                    }
                },
            )
        }
    }
}
