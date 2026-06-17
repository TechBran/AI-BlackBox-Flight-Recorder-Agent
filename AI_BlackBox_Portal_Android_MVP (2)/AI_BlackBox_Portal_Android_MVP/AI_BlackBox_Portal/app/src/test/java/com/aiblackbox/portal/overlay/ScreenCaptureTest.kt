package com.aiblackbox.portal.overlay

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM unit tests for the on-device screen-capture redaction (Task W4.2):
 *  - [shouldRefuseCapture] — the PURE password-redaction decision.
 *  - [OverlayScreenCapture] — the gate ORDERING (password checked first, before any
 *    frame is grabbed) and the result mapping. All framework facts are injected
 *    lambdas, so this needs no Service / MediaProjection / AccessibilityNodeInfo.
 *
 * The actual MediaProjection capture + the live focus query are framework/device-
 * verified (the host JVM can't run them); only the decision + ordering are unit-tested.
 */
class ScreenCaptureTest {

    // ---- shouldRefuseCapture: the pure decision --------------------------------

    @Test
    fun `shouldRefuseCapture refuses iff a password field is focused`() {
        assertTrue("password focused -> refuse the capture", shouldRefuseCapture(true))
        assertFalse("no password focused -> allow the capture", shouldRefuseCapture(false))
    }

    // ---- OverlayScreenCapture: gate ordering + result mapping ------------------

    @Test
    fun `capture refuses BEFORE grabbing a frame when a password is focused`() = runTest {
        var captureInvoked = false
        val cap = OverlayScreenCapture(
            passwordFocused = { true },
            overlayRunning = { true },
            overlayCapture = { cb -> captureInvoked = true; cb(byteArrayOf(1, 2, 3)) },
        )
        val result = cap.capture()
        assertEquals(ScreenCaptureResult.RefusedPassword, result)
        // SECURITY: the frame grab must NOT run when a password is focused — the
        // redaction gate short-circuits before any screenshot is taken.
        assertFalse("must not capture a frame of a password screen", captureInvoked)
    }

    @Test
    fun `capture returns Unavailable with the no-overlay reason when the overlay is down`() = runTest {
        var captureInvoked = false
        val cap = OverlayScreenCapture(
            passwordFocused = { false },
            overlayRunning = { false },
            overlayCapture = { cb -> captureInvoked = true; cb(byteArrayOf(1)) },
        )
        val result = cap.capture()
        assertTrue(result is ScreenCaptureResult.Unavailable)
        assertEquals(CAPTURE_UNAVAILABLE_NO_OVERLAY, (result as ScreenCaptureResult.Unavailable).reason)
        assertFalse("no frame grab when the overlay/projection is down", captureInvoked)
    }

    @Test
    fun `capture returns Success with the PNG bytes on a clean grab`() = runTest {
        val png = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47) // PNG magic-ish
        val cap = OverlayScreenCapture(
            passwordFocused = { false },
            overlayRunning = { true },
            overlayCapture = { cb -> cb(png) },
        )
        val result = cap.capture()
        assertTrue(result is ScreenCaptureResult.Success)
        assertTrue("PNG bytes carried through verbatim", png.contentEquals((result as ScreenCaptureResult.Success).pngBytes))
    }

    @Test
    fun `capture returns Unavailable with the no-frame reason when the grab yields null`() = runTest {
        val cap = OverlayScreenCapture(
            passwordFocused = { false },
            overlayRunning = { true },
            overlayCapture = { cb -> cb(null) },
        )
        val result = cap.capture()
        assertEquals(
            CAPTURE_UNAVAILABLE_NO_FRAME,
            (result as ScreenCaptureResult.Unavailable).reason,
        )
    }

    @Test
    fun `capture treats an empty byte array as no-frame (not a valid Success)`() = runTest {
        val cap = OverlayScreenCapture(
            passwordFocused = { false },
            overlayRunning = { true },
            overlayCapture = { cb -> cb(byteArrayOf()) },
        )
        val result = cap.capture()
        assertTrue("empty bytes -> Unavailable, never a Success", result is ScreenCaptureResult.Unavailable)
    }

    @Test
    fun `Success equality is structural over the PNG bytes`() {
        val a = ScreenCaptureResult.Success(byteArrayOf(1, 2, 3))
        val b = ScreenCaptureResult.Success(byteArrayOf(1, 2, 3))
        val c = ScreenCaptureResult.Success(byteArrayOf(9))
        assertEquals(a, b)
        assertEquals(a.hashCode(), b.hashCode())
        assertFalse(a == c)
    }
}
