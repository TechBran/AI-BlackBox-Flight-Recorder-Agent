package com.aiblackbox.portal.overlay

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M1.1) Unit tests for the PURE parts of [DeviceCapabilities]: the runtime classifiers
 * ([DeviceCapabilities.classifyFormFactor] / [screenshotAvailable][DeviceCapabilities.screenshotAvailable] /
 * [coordinateGestureSupported][DeviceCapabilities.coordinateGestureSupported]) and the
 * wire serialization (keys + enum values must match `docs/schema/device_capability.json`
 * EXACTLY). The `Context`-reading [DeviceCapabilities.detect] shell is device-verified.
 */
class DeviceCapabilitiesTest {

    // Encoder mirroring the wire encoder (encodeDefaults so the required displayId is emitted).
    private val json = Json { encodeDefaults = true }

    // ---- classifyFormFactor: sw-dp + XR probe -------------------------------

    @Test fun `phone below 600dp classifies phone`() {
        assertEquals(FormFactor.PHONE, DeviceCapabilities.classifyFormFactor(411, isXr = false))
        assertEquals(FormFactor.PHONE, DeviceCapabilities.classifyFormFactor(599, isXr = false))
    }

    @Test fun `600dp and above classifies tablet`() {
        assertEquals(FormFactor.TABLET, DeviceCapabilities.classifyFormFactor(600, isXr = false))
        assertEquals(FormFactor.TABLET, DeviceCapabilities.classifyFormFactor(800, isXr = false))
    }

    @Test fun `xr probe wins regardless of sw-dp`() {
        assertEquals(FormFactor.XR_HEADSET, DeviceCapabilities.classifyFormFactor(411, isXr = true))
        assertEquals(FormFactor.XR_HEADSET, DeviceCapabilities.classifyFormFactor(900, isXr = true))
    }

    // ---- (M5.5) foldable classification via the FoldingFeature posture probe -

    @Test fun `an observed hinge classifies foldable regardless of sw-dp`() {
        // A Fold folded (narrow sw-dp) OR unfolded (wide sw-dp) both report FOLDABLE when a
        // FoldingFeature was observed (isFoldable = a non-null posture on the detect() path).
        assertEquals(FormFactor.FOLDABLE, DeviceCapabilities.classifyFormFactor(411, isXr = false, isFoldable = true))
        assertEquals(FormFactor.FOLDABLE, DeviceCapabilities.classifyFormFactor(700, isXr = false, isFoldable = true))
    }

    @Test fun `xr still wins over a foldable probe`() {
        assertEquals(FormFactor.XR_HEADSET, DeviceCapabilities.classifyFormFactor(700, isXr = true, isFoldable = true))
    }

    @Test fun `no hinge keeps the sw-dp phone-tablet split (back-compat default)`() {
        assertEquals(FormFactor.PHONE, DeviceCapabilities.classifyFormFactor(411, isXr = false, isFoldable = false))
        assertEquals(FormFactor.TABLET, DeviceCapabilities.classifyFormFactor(600, isXr = false, isFoldable = false))
        // Default isFoldable=false preserves the M1.1 two-arg behavior exactly.
        assertEquals(FormFactor.PHONE, DeviceCapabilities.classifyFormFactor(411, isXr = false))
    }

    // ---- capability flags reflect real availability -------------------------

    @Test fun `screenshot available on phone-tablet at API 30+ but not XR`() {
        assertTrue(DeviceCapabilities.screenshotAvailable(FormFactor.PHONE, 30))
        assertTrue(DeviceCapabilities.screenshotAvailable(FormFactor.TABLET, 34))
        assertFalse(DeviceCapabilities.screenshotAvailable(FormFactor.PHONE, 29)) // pre-API-30
        assertFalse(DeviceCapabilities.screenshotAvailable(FormFactor.XR_HEADSET, 34)) // XR unconfirmed
    }

    @Test fun `coordinate gesture supported everywhere except XR`() {
        assertTrue(DeviceCapabilities.coordinateGestureSupported(FormFactor.PHONE))
        assertTrue(DeviceCapabilities.coordinateGestureSupported(FormFactor.TABLET))
        assertTrue(DeviceCapabilities.coordinateGestureSupported(FormFactor.FOLDABLE))
        assertFalse(DeviceCapabilities.coordinateGestureSupported(FormFactor.XR_HEADSET))
    }

    // ---- serialization matches device_capability.json EXACTLY ---------------

    @Test fun `phone serializes with the exact schema keys and lowercase enum`() {
        val cap = DeviceCapabilities(FormFactor.PHONE, hasScreenshot = true, supportsCoordinateGesture = true, displayId = 0)
        val s = json.encodeToString(cap)
        assertTrue(s, s.contains("\"formFactor\":\"phone\""))
        assertTrue(s, s.contains("\"hasScreenshot\":true"))
        assertTrue(s, s.contains("\"supportsCoordinateGesture\":true"))
        assertTrue(s, s.contains("\"displayId\":0"))
    }

    @Test fun `xr serializes as xr_headset with degraded flags`() {
        val cap = DeviceCapabilities(FormFactor.XR_HEADSET, hasScreenshot = false, supportsCoordinateGesture = false, displayId = 0)
        val s = json.encodeToString(cap)
        assertTrue(s, s.contains("\"formFactor\":\"xr_headset\""))
        assertTrue(s, s.contains("\"hasScreenshot\":false"))
        assertTrue(s, s.contains("\"supportsCoordinateGesture\":false"))
    }

    @Test fun `every form factor uses its schema wire value`() {
        fun ff(f: FormFactor) = json.encodeToString(DeviceCapabilities(f, false, false, 0))
        assertTrue(ff(FormFactor.PHONE).contains("\"phone\""))
        assertTrue(ff(FormFactor.TABLET).contains("\"tablet\""))
        assertTrue(ff(FormFactor.FOLDABLE).contains("\"foldable\""))
        assertTrue(ff(FormFactor.XR_HEADSET).contains("\"xr_headset\""))
        assertTrue(ff(FormFactor.GLASSES).contains("\"glasses\""))
    }

    // ---- (M5.5) posture serialization matches device_capability.json --------

    @Test fun `foldable posture serializes with the schema keys and lowercase enums`() {
        // Wire encoder drops nulls so a non-foldable omits posture entirely.
        val wire = Json { encodeDefaults = true; explicitNulls = false }
        val cap = DeviceCapabilities(
            FormFactor.FOLDABLE, hasScreenshot = true, supportsCoordinateGesture = true, displayId = 0,
            posture = DevicePosture(PostureState.HALF_OPENED, HingeOrientation.VERTICAL),
        )
        val s = wire.encodeToString(cap)
        assertTrue(s, s.contains("\"posture\":"))
        assertTrue(s, s.contains("\"state\":\"half_opened\""))
        assertTrue(s, s.contains("\"orientation\":\"vertical\""))
    }

    @Test fun `posture orientation is omitted when unknown`() {
        val wire = Json { encodeDefaults = true; explicitNulls = false }
        val s = wire.encodeToString(DevicePosture(PostureState.FLAT))
        assertTrue(s, s.contains("\"state\":\"flat\""))
        assertFalse("unknown orientation must be dropped", s.contains("orientation"))
    }

    @Test fun `non-foldable omits posture entirely`() {
        val wire = Json { encodeDefaults = true; explicitNulls = false }
        val cap = DeviceCapabilities(FormFactor.PHONE, hasScreenshot = true, supportsCoordinateGesture = true, displayId = 0)
        assertFalse(wire.encodeToString(cap).contains("posture"))
    }

    // ---- (M6 / I1) the ONE XR probe shared by detect() + OverlayService/PortalActivity ----
    // isXrForm is the pure decision behind DeviceCapabilities.isXr(context); the overlay-routing
    // call-sites (OverlayService/PortalActivity.isXrDevice) now delegate to the SAME probe that
    // feeds detect(), so the consent-surface routing can't diverge from the wire capability.

    @Test fun `isXrForm true for a VR-headset UiMode alone`() {
        // UiMode says VR headset, NO system features present → still XR.
        assertTrue(DeviceCapabilities.isXrForm(isVrHeadsetUiMode = true) { false })
    }

    @Test fun `isXrForm true for openxr or head-tracking alone (cases the old single-feature probe missed)`() {
        // The OLD OverlayService/PortalActivity probe keyed ONLY off xr.api.spatial; a headset
        // exposing openxr / legacy head-tracking (but not spatial) must STILL classify as XR.
        assertTrue(DeviceCapabilities.isXrForm(isVrHeadsetUiMode = false) { it == "android.software.xr.api.openxr" })
        assertTrue(DeviceCapabilities.isXrForm(isVrHeadsetUiMode = false) { it == "android.hardware.vr.headtracking" })
    }

    @Test fun `isXrForm false when neither UiMode nor any XR feature is present`() {
        assertFalse(DeviceCapabilities.isXrForm(isVrHeadsetUiMode = false) { false })
        // an unrelated feature does not trip it
        assertFalse(DeviceCapabilities.isXrForm(isVrHeadsetUiMode = false) { it == "android.hardware.camera" })
    }

    @Test fun `isXr probe agrees with detect formFactor for the UiMode-only and openxr-only cases`() {
        // The probe result IS the isXr argument detect() feeds classifyFormFactor, and
        // classifyFormFactor returns XR_HEADSET iff isXr — so isXrDevice (overlay routing) and the
        // wire capability (detect().formFactor==XR_HEADSET) can NEVER diverge. Prove it for both
        // XR-positive probe cases, across handheld sw-dp and a co-present foldable hinge.
        val uiModeOnly = DeviceCapabilities.isXrForm(isVrHeadsetUiMode = true) { false }
        val openxrOnly = DeviceCapabilities.isXrForm(isVrHeadsetUiMode = false) { it == "android.software.xr.api.openxr" }
        for ((probe, sw, foldable) in listOf(
            Triple(uiModeOnly, 411, false),
            Triple(uiModeOnly, 900, true),
            Triple(openxrOnly, 411, false),
            Triple(openxrOnly, 700, true),
        )) {
            val classifiesXr =
                DeviceCapabilities.classifyFormFactor(sw, isXr = probe, isFoldable = foldable) == FormFactor.XR_HEADSET
            assertEquals("probe must agree with detect()'s XR classification", probe, classifiesXr)
            assertTrue("both probe cases ARE XR", classifiesXr)
        }
    }

    @Test fun `a non-XR probe never classifies XR (overlay routing matches wire)`() {
        val notXr = DeviceCapabilities.isXrForm(isVrHeadsetUiMode = false) { false }
        assertFalse(notXr)
        assertFalse(DeviceCapabilities.classifyFormFactor(411, isXr = notXr, isFoldable = false) == FormFactor.XR_HEADSET)
        // even with a foldable hinge co-present, a non-XR probe stays non-XR (foldable, not XR)
        assertFalse(DeviceCapabilities.classifyFormFactor(700, isXr = notXr, isFoldable = true) == FormFactor.XR_HEADSET)
    }
}
