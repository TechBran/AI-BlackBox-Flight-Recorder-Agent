package com.aiblackbox.portal.overlay

import android.app.UiModeManager
import android.content.Context
import android.content.res.Configuration
import android.os.Build
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * (M1.1) The device-capability descriptor advertised on every `observation` so the
 * cloud frontier loop degrades gracefully per device class. The wire shape is the
 * M0 contract `docs/schema/device_capability.json` — the JSON keys serialized here
 * (`formFactor` / `hasScreenshot` / `supportsCoordinateGesture` / `displayId`) MATCH
 * that schema EXACTLY, and [FormFactor] serializes to the schema's lowercase enum
 * (`phone` / `tablet` / `foldable` / `xr_headset` / `glasses`).
 *
 * ## What each flag drives (server-side, M2)
 * - [formFactor]: the physical class. phone/tablet/foldable → all actuation paths;
 *   `xr_headset` → node + intent only (no coordinate gesture, capture unconfirmed);
 *   `glasses` → drive the paired phone (compute lives on the phone).
 * - [hasScreenshot]: whether the device can put a silent
 *   `AccessibilityService.takeScreenshot()` frame on the wire (API 30+, non-XR). When
 *   false the loop runs tree-only and never requests a screenshot (enforced on the
 *   wire by `observation.json`).
 * - [supportsCoordinateGesture]: whether `dispatchGesture`-based `coordinate_tap` /
 *   `coordinate_swipe` are meaningful. False on XR (per-panel 3D compositor, no flat
 *   framebuffer) → the loop uses `element_click` + intents; coordinate actions are
 *   skipped/reported on-device.
 * - [displayId]: the target display for capture + gesture. 0 = Display.DEFAULT_DISPLAY;
 *   non-zero (DeX / external / multi-display) lands in M5.
 *
 * ## [detect] is HONEST runtime detection
 * Form factor is classified from the live `smallestScreenWidthDp` (phone < 600dp ≤
 * tablet) and an XR probe (UiMode `VR_HEADSET` or an XR/VR system feature). When XR
 * can't be determined it falls back to phone (never guesses XR). `hasScreenshot` /
 * `supportsCoordinateGesture` reflect the REAL availability for the detected class +
 * API level — not optimistic constants. The pure classifiers ([classifyFormFactor] /
 * [screenshotAvailable] / [coordinateGestureSupported]) are framework-free and
 * unit-tested; [detect] is the thin `Context`-reading shell (device-verified).
 */
@Serializable
data class DeviceCapabilities(
    val formFactor: FormFactor,
    val hasScreenshot: Boolean,
    val supportsCoordinateGesture: Boolean,
    val displayId: Int = 0,
) {
    companion object {
        /** `smallestScreenWidthDp` at/above which a device is classified a tablet
         *  (the Android sw600dp large-screen breakpoint). Below → phone. */
        const val TABLET_MIN_SW_DP = 600

        /**
         * Honest runtime detection from the live [context]. Reads the smallest-width
         * dp + an XR probe, then derives the capability flags for that class. Never
         * throws — any probe failure degrades to the phone profile (the safe, most
         * capable-assumption default for an unknown handheld).
         */
        fun detect(context: Context): DeviceCapabilities {
            val smallestWidthDp = runCatching {
                context.resources.configuration.smallestScreenWidthDp
            }.getOrDefault(0)
            val isXr = detectIsXr(context)
            val formFactor = classifyFormFactor(smallestWidthDp, isXr)
            return DeviceCapabilities(
                formFactor = formFactor,
                hasScreenshot = screenshotAvailable(formFactor, Build.VERSION.SDK_INT),
                supportsCoordinateGesture = coordinateGestureSupported(formFactor),
                // Multi-display / DeX addressing is M5; the default display for now.
                displayId = 0,
            )
        }

        /**
         * Best-effort XR probe. True when the UiMode reports a VR headset OR any known
         * XR/VR system feature is present (Android XR spatial API — the same heuristic
         * [OverlayService] uses — or OpenXR / VR head-tracking). Unknown → false, so
         * [detect] never mis-classifies a handheld as XR. Framework-touching, so it
         * lives outside the pure classifiers.
         */
        private fun detectIsXr(context: Context): Boolean = try {
            val uiMode = (context.getSystemService(Context.UI_MODE_SERVICE) as? UiModeManager)
                ?.currentModeType
            if (uiMode == Configuration.UI_MODE_TYPE_VR_HEADSET) {
                true
            } else {
                val pm = context.packageManager
                XR_SYSTEM_FEATURES.any { runCatching { pm.hasSystemFeature(it) }.getOrDefault(false) }
            }
        } catch (e: Exception) {
            false
        }

        /** System features that identify an XR / VR headset. Kept internal so the
         *  probe set is inspectable; extended additively as Android XR evolves. */
        internal val XR_SYSTEM_FEATURES = listOf(
            "android.software.xr.api.spatial",   // Android XR spatial API (matches OverlayService)
            "android.software.xr.api.openxr",    // OpenXR runtime
            "android.hardware.vr.headtracking",  // legacy VR head-tracking
        )

        /**
         * PURE: classify the form factor from the smallest-width dp + XR probe. XR wins
         * (a headset can report a large sw-dp); otherwise sw600dp+ is a tablet, below is
         * a phone. `foldable` / `glasses` are reserved wire values (posture detection is
         * M5; glasses drive the paired phone) — [detect] does not emit them, matching the
         * M1.1 scope (phone/tablet by sw-dp, XR by feature, else phone). NOTE: a foldable
         * therefore reports as `phone` (folded, narrow sw-dp) or `tablet` (unfolded, wide
         * sw-dp) by its CURRENT smallest-width; the dedicated `foldable`/posture value is M5.
         */
        fun classifyFormFactor(smallestWidthDp: Int, isXr: Boolean): FormFactor = when {
            isXr -> FormFactor.XR_HEADSET
            smallestWidthDp >= TABLET_MIN_SW_DP -> FormFactor.TABLET
            else -> FormFactor.PHONE
        }

        /**
         * PURE: whether the silent `AccessibilityService.takeScreenshot()` path is
         * available for [formFactor] at [sdkInt]. Requires API 30 (Android R) and is
         * FALSE on XR (headset-view capture unconfirmed) → the loop stays tree-only.
         */
        fun screenshotAvailable(formFactor: FormFactor, sdkInt: Int): Boolean =
            formFactor != FormFactor.XR_HEADSET && sdkInt >= Build.VERSION_CODES.R

        /**
         * PURE: whether `dispatchGesture`-based coordinate actuation is meaningful for
         * [formFactor]. FALSE on XR (no flat framebuffer / per-panel compositor);
         * true on every handheld/large-screen class.
         */
        fun coordinateGestureSupported(formFactor: FormFactor): Boolean =
            formFactor != FormFactor.XR_HEADSET
    }
}

/**
 * The physical device class. Serializes to the lowercase wire values in
 * `device_capability.json` (`phone` / `tablet` / `foldable` / `xr_headset` /
 * `glasses`) via the per-entry [SerialName]s.
 */
@Serializable
enum class FormFactor {
    @SerialName("phone") PHONE,
    @SerialName("tablet") TABLET,
    @SerialName("foldable") FOLDABLE,
    @SerialName("xr_headset") XR_HEADSET,
    @SerialName("glasses") GLASSES,
}
