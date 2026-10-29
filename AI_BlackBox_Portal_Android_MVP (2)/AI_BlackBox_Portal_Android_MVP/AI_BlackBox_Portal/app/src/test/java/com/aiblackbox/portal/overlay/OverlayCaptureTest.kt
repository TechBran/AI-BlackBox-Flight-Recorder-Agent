package com.aiblackbox.portal.overlay

import android.app.Application
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.File
import java.time.Duration

/**
 * The overlay's capture side, out of OverlayService (rank 76). A MediaProjection cannot be
 * granted on Robolectric, so these pin what every capture does without one: no frame, no
 * share, and the refresh that asks the starting screen for a new projection (a broadcast to
 * this package, then the service stops), with the overlay told what to show.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class OverlayCaptureTest {

    private val app: Application = RuntimeEnvironment.getApplication()

    private class FakeHost : OverlayCapture.Host {
        val toasts = mutableListOf<String>()
        val statuses = mutableListOf<String>()
        var stopped = 0
        var screenshots = mutableListOf<File?>()
        override fun updateStatus(status: String) { statuses += status }
        override fun showToast(message: String) { toasts += message }
        override fun hideOverlayForCapture(): Boolean = false
        override fun restoreOverlayAfterCapture(wasExpanded: Boolean) {}
        override fun onScreenshot(file: File?) { screenshots += file }
        override fun onRecordingStarted() {}
        override fun onRecordingSaved(file: File) {}
        override fun onRecordingStopped() {}
        override fun stopService() { stopped++ }
    }

    private fun capture(host: FakeHost) = OverlayCapture(
        app, Handler(Looper.getMainLooper()), { app.getSystemService(Context.WINDOW_SERVICE) as WindowManager }, host,
    )

    @Test fun `without a projection nothing is captured and a refresh is asked for`() {
        val host = FakeHost()
        val c = capture(host)
        c.refreshScreenMetrics()
        assertFalse(c.available())
        assertFalse(c.hasPersistentReader())
        assertNull(c.latestFrameJpegBase64())

        c.takeScreenshot()
        assertEquals(listOf("Refreshing capture permission..."), host.toasts)
        val sent = shadowOf(app).broadcastIntents.single()
        assertEquals(OverlayService.ACTION_REFRESH_PROJECTION, sent.action)
        assertEquals(app.packageName, sent.`package`)
        assertFalse(sent.getBooleanExtra(OverlayService.EXTRA_RECORDING_MODE, true))
        assertEquals("the service waits before stopping", 0, host.stopped)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(500))
        assertEquals(1, host.stopped)
        assertTrue("no screenshot was attempted", host.screenshots.isEmpty())
    }

    @Test fun `a vision frame without a projection answers null at once`() {
        val host = FakeHost()
        var got: ByteArray? = byteArrayOf(1)
        var called = false
        capture(host).captureFramePngBytes { called = true; got = it }
        assertTrue(called)
        assertNull(got)
        assertEquals(listOf("Refreshing capture permission..."), host.toasts)
    }

    @Test fun `recording without a projection says so and does not start`() {
        val host = FakeHost()
        val c = capture(host)
        c.toggleRecording()
        assertEquals(listOf("Recording permission not granted"), host.toasts)
        assertFalse(c.isRecording)
        // Stopping what never started changes nothing.
        c.stopRecording()
        assertTrue(host.statuses.isEmpty())
    }

    @Test fun `the persistent display needs a projection`() {
        val host = FakeHost()
        var result: Boolean? = null
        capture(host).setupPersistentScreenshotDisplay { result = it }
        assertEquals(false, result)
    }
}
