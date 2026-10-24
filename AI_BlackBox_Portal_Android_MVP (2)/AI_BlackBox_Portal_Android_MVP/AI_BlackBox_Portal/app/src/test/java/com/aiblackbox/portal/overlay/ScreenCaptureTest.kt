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
            passwordFocus = { PasswordFocus.PASSWORD },
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
            passwordFocus = { PasswordFocus.NOT_PASSWORD },
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
            passwordFocus = { PasswordFocus.NOT_PASSWORD },
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
            passwordFocus = { PasswordFocus.NOT_PASSWORD },
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
            passwordFocus = { PasswordFocus.NOT_PASSWORD },
            overlayRunning = { true },
            overlayCapture = { cb -> cb(byteArrayOf()) },
        )
        val result = cap.capture()
        assertTrue("empty bytes -> Unavailable, never a Success", result is ScreenCaptureResult.Unavailable)
    }

    // ---- (A03-overlay-rest-04) autonomous capture fails CLOSED on "can't tell" -------

    @Test
    fun `the tri-state decision refuses UNKNOWN only for an autonomous capture`() {
        assertTrue(shouldRefuseCapture(PasswordFocus.PASSWORD, autonomous = false))
        assertTrue(shouldRefuseCapture(PasswordFocus.PASSWORD, autonomous = true))
        assertTrue("autonomous + can't tell -> refuse", shouldRefuseCapture(PasswordFocus.UNKNOWN, autonomous = true))
        assertFalse("user-invoked keeps today's behaviour", shouldRefuseCapture(PasswordFocus.UNKNOWN, autonomous = false))
        assertFalse(shouldRefuseCapture(PasswordFocus.NOT_PASSWORD, autonomous = true))
        assertFalse(shouldRefuseCapture(PasswordFocus.NOT_PASSWORD, autonomous = false))
    }

    @Test
    fun `the silent frontier capture refuses when the focus cannot be read`() = runTest {
        var captureInvoked = false
        val cap = A11yScreenCapture(
            passwordFocus = { PasswordFocus.UNKNOWN },
            a11yCapture = { cb -> captureInvoked = true; cb(byteArrayOf(1)) },
        )
        assertEquals(ScreenCaptureResult.RefusedPassword, cap.capture())
        assertFalse("a blind tree must not ship a frame to the cloud model", captureInvoked)
    }

    @Test
    fun `the frontier fallback capture is autonomous too`() = runTest {
        var overlayInvoked = false
        val cap = PreferredScreenCapture(
            silent = A11yScreenCapture(passwordFocus = { PasswordFocus.NOT_PASSWORD },
                a11yCapture = { cb -> cb(null) }),                 // silent path unavailable
            fallback = OverlayScreenCapture(
                passwordFocus = { PasswordFocus.UNKNOWN },
                overlayRunning = { true },
                overlayCapture = { cb -> overlayInvoked = true; cb(byteArrayOf(1)) },
                autonomous = true,
            ),
        )
        assertEquals(ScreenCaptureResult.RefusedPassword, cap.capture())
        assertFalse(overlayInvoked)
    }

    @Test
    fun `a user-invoked overlay capture still proceeds when the focus cannot be read`() = runTest {
        val cap = OverlayScreenCapture(
            passwordFocus = { PasswordFocus.UNKNOWN },
            overlayRunning = { true },
            overlayCapture = { cb -> cb(byteArrayOf(7)) },
        )
        assertTrue(cap.capture() is ScreenCaptureResult.Success)
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
