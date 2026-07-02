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
 *   non-zero (DeX / external / multi-display) is the M5 large-screen work.
 * - [posture] (M5.5): a foldable's hinge posture (FLAT / HALF_OPENED + orientation) when the
 *   device is a foldable — null on non-foldables (dropped from the wire). A posture CHANGE
 *   between observations invalidates coordinate actions (see `FoldingFeatureMonitor`); the loop
 *   re-observes before its next coordinate tap.
 *
 * ## [detect] is HONEST runtime detection
 * Form factor is classified from the live `smallestScreenWidthDp` (phone < 600dp ≤
 * tablet), an XR probe (UiMode `VR_HEADSET` or an XR/VR system feature), and — M5.5 — a
 * FOLDABLE probe: when a [posture] is supplied (a live [FoldingFeature] was observed) the
 * device reports `formFactor=foldable` regardless of its current sw-dp. When XR can't be
 * determined it falls back to phone (never guesses XR); no posture → sw-dp classification
 * (today's behavior). `hasScreenshot` / `supportsCoordinateGesture` reflect the REAL
 * availability for the detected class + API level — not optimistic constants. The pure
 * classifiers ([classifyFormFactor] / [screenshotAvailable] / [coordinateGestureSupported])
 * are framework-free and unit-tested; [detect] is the thin `Context`-reading shell
 * (device-verified).
 */
@Serializable
data class DeviceCapabilities(
    val formFactor: FormFactor,
    val hasScreenshot: Boolean,
    val supportsCoordinateGesture: Boolean,
    val displayId: Int = 0,
    // (M5.5) The foldable hinge posture (FLAT / HALF_OPENED + orientation), or null on a
    // non-foldable / when unknown. Dropped from the wire when null (explicitNulls=false).
    val posture: DevicePosture? = null,
    // (M8.1) Whether the on-device AccessibilityService is enabled. When FALSE (disabled /
    // OS-revoked, e.g. Advanced Protection) the SCREEN paths are gone — hasScreenshot +
    // supportsCoordinateGesture are also forced false and the tree is empty — so the loop knows
    // the device is INTENT-ONLY. Additive + back-compat (default true); [detect] sets it live.
    val accessibilityEnabled: Boolean = true,
) {
    companion object {
        /** `smallestScreenWidthDp` at/above which a device is classified a tablet
         *  (the Android sw600dp large-screen breakpoint). Below → phone. */
        const val TABLET_MIN_SW_DP = 600

        /**
         * Honest runtime detection from the live [context]. Reads the smallest-width
         * dp + an XR probe + the supplied foldable [posture] (M5.5), then derives the
         * capability flags for that class. Never throws — any probe failure degrades to
         * the phone profile (the safe, most capable-assumption default for an unknown
         * handheld).
         *
         * @param posture the current foldable posture from `FoldingFeatureMonitor` (null on a
         *   non-foldable / when the monitor hasn't observed a hinge). A non-null posture makes
         *   the device report `formFactor=foldable` and carries the posture on the wire. The
         *   default reads the process-wide monitor so existing call-sites gain posture for free;
         *   tests pass an explicit posture (or null).
         */
        fun detect(
            context: Context,
            posture: DevicePosture? = FoldingFeatureMonitor.instance.currentPosture(),
            // (M8.1) live a11y-enabled probe — the connected service singleton. A disabled /
            // OS-revoked service clears the instance, so the wire capability reports intent-only.
            accessibilityEnabled: Boolean = BlackBoxA11yService.isConnected(),
        ): DeviceCapabilities {
            val smallestWidthDp = runCatching {
                context.resources.configuration.smallestScreenWidthDp
            }.getOrDefault(0)
            val xr = isXr(context)
            val formFactor = classifyFormFactor(smallestWidthDp, xr, isFoldable = posture != null)
            // (M8.1) with a11y off, neither the silent screenshot nor a dispatchGesture works — so
            // the loop must not be told they're available. Force both false; the loop then relies on
            // the intent path (and the /action dispatcher returns intent_only_mode for screen actions).
            val a11y = accessibilityEnabled
            return DeviceCapabilities(
                formFactor = formFactor,
                hasScreenshot = a11y && screenshotAvailable(formFactor, Build.VERSION.SDK_INT),
                supportsCoordinateGesture = a11y && coordinateGestureSupported(formFactor),
                // Multi-display / DeX addressing is threaded via displayId (default display now).
                displayId = 0,
                posture = posture,
                accessibilityEnabled = a11y,
            )
        }

        /**
         * (M6 / I1) The SINGLE authoritative "is this an XR headset" probe over the live
         * [context]. This is the SAME test that drives the wire capability — [detect] feeds this
         * result into [classifyFormFactor], which returns [FormFactor.XR_HEADSET] iff this is true
         * (XR wins over foldable/sw-dp), so `isXr(context) == (detect(context).formFactor ==
         * XR_HEADSET)`. It is ALSO the probe `OverlayService`/`PortalActivity` delegate to for their
         * overlay-vs-phone routing, so the consent-surface routing can never diverge from the wire
         * classification (a headset must run the XR overlay UI, or the M6.3 in-headset consent banner
         * never surfaces). True when the UiMode reports a VR headset OR any known XR/VR system feature
         * ([XR_SYSTEM_FEATURES]) is present; unknown / probe failure → false, so a handheld is never
         * mis-classified as XR. Framework-touching; the pure decision is [isXrForm].
         */
        fun isXr(context: Context): Boolean = try {
            val uiMode = (context.getSystemService(Context.UI_MODE_SERVICE) as? UiModeManager)
                ?.currentModeType
            val pm = context.packageManager
            isXrForm(isVrHeadsetUiMode = uiMode == Configuration.UI_MODE_TYPE_VR_HEADSET) {
                pm.hasSystemFeature(it)
            }
        } catch (e: Exception) {
            false
        }

        /**
         * PURE XR decision: true when the UiMode is a VR headset ([isVrHeadsetUiMode]) OR any
         * [XR_SYSTEM_FEATURES] entry is present per [hasFeature]. Framework-free (the caller supplies
         * the two facts), so the exact probe [detect] / [OverlayService] / [PortalActivity] run is
         * JVM-unit-testable — including the OpenXR-only / head-tracking-only cases the old
         * single-feature (`xr.api.spatial`) probe silently missed.
         */
        fun isXrForm(isVrHeadsetUiMode: Boolean, hasFeature: (String) -> Boolean): Boolean =
            isVrHeadsetUiMode ||
                XR_SYSTEM_FEATURES.any { runCatching { hasFeature(it) }.getOrDefault(false) }

        /** System features that identify an XR / VR headset. Kept internal so the probe set is
         *  inspectable; extended additively as Android XR evolves. The WHOLE set (not just the
         *  spatial API) is the shared probe used by [detect], [OverlayService], and [PortalActivity]. */
        internal val XR_SYSTEM_FEATURES = listOf(
            "android.software.xr.api.spatial",   // Android XR spatial API
            "android.software.xr.api.openxr",    // OpenXR runtime
            "android.hardware.vr.headtracking",  // legacy VR head-tracking
        )

        /**
         * PURE: classify the form factor from the smallest-width dp + XR probe + (M5.5) a
         * foldable probe. XR wins (a headset can report a large sw-dp); then a device with an
         * observed hinge ([isFoldable]) is a `foldable` regardless of its CURRENT sw-dp (a Fold
         * reports foldable whether folded-narrow or unfolded-wide); otherwise sw600dp+ is a
         * tablet, below is a phone. `glasses` remains a reserved wire value (glasses drive the
         * paired phone). Back-compat: the default `isFoldable=false` preserves the M1.1 behavior
         * for callers that don't pass a posture.
         */
        fun classifyFormFactor(
            smallestWidthDp: Int,
            isXr: Boolean,
            isFoldable: Boolean = false,
        ): FormFactor = when {
            isXr -> FormFactor.XR_HEADSET
            isFoldable -> FormFactor.FOLDABLE
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
