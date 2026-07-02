package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.overlay.DevicePosture
import com.aiblackbox.portal.overlay.DeviceCapabilities
import com.aiblackbox.portal.overlay.FormFactor
import com.aiblackbox.portal.overlay.HingeOrientation
import com.aiblackbox.portal.overlay.PostureState
import com.aiblackbox.portal.overlay.UiNode
import com.aiblackbox.portal.overlay.WindowInfo
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M1.2) Tests for the `observation` wire type + the pure tree-first cadence helpers
 * ([treeIsBlind] / [shouldCaptureScreenshot]) + the [ObservationBuilder] (with fakes,
 * no Android). Serialization must conform to `docs/schema/observation.json`.
 */
class ObservationTest {

    // Mirrors the server's WIRE_JSON (defaults emitted, nulls dropped).
    private val json = Json { encodeDefaults = true; explicitNulls = false }

    private val phoneCap = DeviceCapabilities(FormFactor.PHONE, hasScreenshot = true, supportsCoordinateGesture = true, displayId = 0)
    private val xrCap = DeviceCapabilities(FormFactor.XR_HEADSET, hasScreenshot = false, supportsCoordinateGesture = false, displayId = 0)

    private fun node(text: String, id: Int = 0) = UiNode(
        nodeId = id, role = "TextView", text = text, resourceId = "",
        bounds = "0,0,10,10", clickable = true, editable = false, isPassword = false,
    )

    // ---- serialization conforms to observation.json -------------------------

    @Test fun `observation carries msg schema_version tree capability timestamp`() {
        val obs = Observation(uiTree = listOf(node("OK")), deviceCapability = phoneCap, timestamp = 123L)
        val s = json.encodeToString(obs)
        assertTrue(s, s.contains("\"msg\":\"observation\""))
        assertTrue(s, s.contains("\"schema_version\":\"1.3\""))
        assertTrue(s, s.contains("\"ui_tree\":"))
        assertTrue(s, s.contains("\"device_capability\":"))
        assertTrue(s, s.contains("\"timestamp\":123"))
    }

    // ---- (M5) window topology + posture-change flag -------------------------

    @Test fun `observation emits window_topology with camelCase entry keys`() {
        val obs = Observation(
            uiTree = emptyList(),
            deviceCapability = phoneCap,
            windowTopology = listOf(
                WindowInfo(displayId = 0, appPackage = "com.app", bounds = "0,0,1080,2400", isSystemBar = false),
                WindowInfo(displayId = 0, appPackage = "com.android.systemui", bounds = "0,0,1080,96", isSystemBar = true),
            ),
        )
        val s = json.encodeToString(obs)
        assertTrue(s, s.contains("\"window_topology\":"))
        assertTrue(s, s.contains("\"displayId\":0"))
        assertTrue(s, s.contains("\"appPackage\":\"com.app\""))
        assertTrue(s, s.contains("\"bounds\":\"0,0,1080,2400\""))
        assertTrue(s, s.contains("\"isSystemBar\":true"))
    }

    @Test fun `posture_changed defaults false and is emitted true when set`() {
        assertFalse(json.encodeToString(Observation(uiTree = emptyList(), deviceCapability = phoneCap))
            .contains("\"posture_changed\":true"))
        val changed = Observation(uiTree = emptyList(), deviceCapability = phoneCap, postureChanged = true)
        assertTrue(json.encodeToString(changed).contains("\"posture_changed\":true"))
    }

    @Test fun `foldable posture rides inside device_capability on the wire`() {
        val foldCap = DeviceCapabilities(
            FormFactor.FOLDABLE, hasScreenshot = true, supportsCoordinateGesture = true, displayId = 0,
            posture = DevicePosture(PostureState.HALF_OPENED, HingeOrientation.VERTICAL),
        )
        val s = json.encodeToString(Observation(uiTree = emptyList(), deviceCapability = foldCap))
        assertTrue(s, s.contains("\"formFactor\":\"foldable\""))
        assertTrue(s, s.contains("\"posture\":"))
        assertTrue(s, s.contains("\"state\":\"half_opened\""))
        assertTrue(s, s.contains("\"orientation\":\"vertical\""))
    }

    @Test fun `non-foldable omits posture from the wire`() {
        // explicitNulls=false → a null device_capability.posture is dropped, matching
        // device_capability.json (optional). Check the `"posture":` KEY specifically — the
        // observation's own `posture_changed` flag legitimately contains the substring "posture".
        assertFalse(json.encodeToString(Observation(uiTree = emptyList(), deviceCapability = phoneCap))
            .contains("\"posture\":"))
    }

    @Test fun `absent screenshot is omitted from the wire`() {
        val s = json.encodeToString(Observation(uiTree = emptyList(), deviceCapability = phoneCap))
        assertFalse(s, s.contains("screenshot"))
    }

    @Test fun `present screenshot is emitted`() {
        val s = json.encodeToString(Observation(uiTree = emptyList(), deviceCapability = phoneCap, screenshot = "QUJD"))
        assertTrue(s, s.contains("\"screenshot\":\"QUJD\""))
    }

    // ---- treeIsBlind --------------------------------------------------------

    @Test fun `empty tree is blind`() {
        assertTrue(treeIsBlind(emptyList()))
    }

    @Test fun `tree with no visible text is blind`() {
        assertTrue(treeIsBlind(listOf(node(""), node(""))))
    }

    @Test fun `tree with any text is not blind`() {
        assertFalse(treeIsBlind(listOf(node(""), node("Submit"))))
    }

    // ---- shouldCaptureScreenshot (tree-first cadence) -----------------------

    @Test fun `rich tree on a capable device does NOT capture`() {
        assertFalse(shouldCaptureScreenshot(phoneCap, listOf(node("Submit")), requested = false))
    }

    @Test fun `blind tree on a capable device DOES capture`() {
        assertTrue(shouldCaptureScreenshot(phoneCap, emptyList(), requested = false))
    }

    @Test fun `explicit request captures even on a rich tree`() {
        assertTrue(shouldCaptureScreenshot(phoneCap, listOf(node("Submit")), requested = true))
    }

    @Test fun `xr never captures even when requested or blind`() {
        assertFalse(shouldCaptureScreenshot(xrCap, emptyList(), requested = true))
        assertFalse(shouldCaptureScreenshot(xrCap, listOf(node("Submit")), requested = true))
    }

    // ---- ObservationBuilder (fakes) -----------------------------------------

    @Test fun `builder embeds a base64 screenshot when the tree is blind`() = runBlocking {
        val builder = ObservationBuilder(
            readTree = { emptyList() },                 // blind
            capability = { phoneCap },
            captureScreenshot = { byteArrayOf(1, 2, 3) },
            encodeBase64 = { "ENCODED" },
            clock = { 7L },
        )
        val obs = builder.build()
        assertEquals("ENCODED", obs.screenshot)
        assertEquals(7L, obs.timestamp)
        assertEquals(phoneCap, obs.deviceCapability)
    }

    @Test fun `builder ships tree-only when the tree is rich`() = runBlocking {
        var captured = false
        val builder = ObservationBuilder(
            readTree = { listOf(node("Submit")) },       // rich
            capability = { phoneCap },
            captureScreenshot = { captured = true; byteArrayOf(1) },
            encodeBase64 = { "ENCODED" },
        )
        val obs = builder.build()
        assertNull(obs.screenshot)
        assertFalse("must not even attempt a capture on a rich tree", captured)
    }

    @Test fun `builder never captures on a device without screenshot`() = runBlocking {
        var captured = false
        val builder = ObservationBuilder(
            readTree = { emptyList() },                  // blind, but...
            capability = { xrCap },                      // ...no screenshot capability
            captureScreenshot = { captured = true; byteArrayOf(1) },
            encodeBase64 = { "ENCODED" },
        )
        val obs = builder.build(requestScreenshot = true)
        assertNull(obs.screenshot)
        assertFalse(captured)
    }

    @Test fun `builder degrades to tree-only when capture returns null`() = runBlocking {
        val builder = ObservationBuilder(
            readTree = { emptyList() },
            capability = { phoneCap },
            captureScreenshot = { null },                // refused / unavailable
            encodeBase64 = { "ENCODED" },
        )
        assertNull(builder.build().screenshot)
    }

    @Test fun `builder threads window topology and posture-change flag`() = runBlocking {
        val windows = listOf(WindowInfo(0, "com.app", "0,0,10,10", false))
        val builder = ObservationBuilder(
            readTree = { listOf(node("Submit")) },
            capability = { phoneCap },
            captureScreenshot = { null },
            readTopology = { windows },
            postureChanged = { true },
        )
        val obs = builder.build()
        assertEquals(windows, obs.windowTopology)
        assertTrue("posture-change flag must be surfaced", obs.postureChanged)
    }

    @Test fun `builder defaults to empty topology and no posture change`() = runBlocking {
        val obs = ObservationBuilder(
            readTree = { emptyList() },
            capability = { phoneCap },
            captureScreenshot = { null },
        ).build()
        assertTrue(obs.windowTopology.isEmpty())
        assertFalse(obs.postureChanged)
    }
}
