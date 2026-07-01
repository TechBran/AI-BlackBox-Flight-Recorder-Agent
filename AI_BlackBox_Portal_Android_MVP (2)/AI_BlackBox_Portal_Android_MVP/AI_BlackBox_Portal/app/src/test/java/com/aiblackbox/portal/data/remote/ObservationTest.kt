package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.overlay.DeviceCapabilities
import com.aiblackbox.portal.overlay.FormFactor
import com.aiblackbox.portal.overlay.UiNode
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
        assertTrue(s, s.contains("\"schema_version\":\"1.1\""))
        assertTrue(s, s.contains("\"ui_tree\":"))
        assertTrue(s, s.contains("\"device_capability\":"))
        assertTrue(s, s.contains("\"timestamp\":123"))
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
}
