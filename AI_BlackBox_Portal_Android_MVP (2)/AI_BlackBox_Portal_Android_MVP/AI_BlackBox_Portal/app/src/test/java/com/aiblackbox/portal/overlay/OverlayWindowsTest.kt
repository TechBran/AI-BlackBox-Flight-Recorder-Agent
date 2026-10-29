package com.aiblackbox.portal.overlay

import android.app.Application
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import com.aiblackbox.portal.R
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowWindowManagerImpl
import java.time.Duration

/**
 * The overlay's floating windows, out of OverlayService (rank 76): what goes on screen with
 * which window params, dragging versus tapping, the consent banner (phone only, once), and
 * the recording stop button whose timer ticks only while a recording runs.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class OverlayWindowsTest {

    private val app: Application = RuntimeEnvironment.getApplication()
    private val wm = app.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val onScreen: List<View> get() = Shadow.extract<ShadowWindowManagerImpl>(wm).views

    private var xr = false
    private var recording = true
    private var stops = 0
    private val toasts = mutableListOf<String>()

    /** The stop button's layout, rebuilt without resources: the three ids the class looks up. */
    private fun stopButtonStandIn(): View = FrameLayout(app).apply {
        addView(TextView(app).apply { id = R.id.recordingTimer })
        addView(View(app).apply { id = R.id.recordingIndicator })
        addView(ImageView(app).apply { id = R.id.btnStopRecording })
    }

    private val windows = OverlayWindows(
        context = app,
        handler = Handler(Looper.getMainLooper()),
        windowManager = { wm },
        isXrDevice = { xr },
        showToast = { toasts += it },
        isRecording = { recording },
        onStopRecording = { stops++ },
        inflateRecordingStopButton = { stopButtonStandIn() },
    )

    @After fun tearDown() {
        windows.hideControlBanner()
        windows.removeAll()
    }

    private fun touch(view: View, action: Int, x: Float, y: Float) {
        val now = SystemClock.uptimeMillis()
        val ev = MotionEvent.obtain(now, now, action, x, y, 0)
        view.dispatchTouchEvent(ev)
        ev.recycle()
    }

    @Test fun `the bubble goes top-left, not focusable, and a drag moves it without a click`() {
        val bubble = View(app)
        var clicks = 0
        bubble.setOnClickListener { clicks++ }
        windows.addBubble(bubble)

        assertTrue(bubble in onScreen)
        val params = bubble.layoutParams as WindowManager.LayoutParams
        assertEquals(WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, params.type)
        assertEquals(WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE, params.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE)
        assertEquals(0, params.x)
        assertEquals(200, params.y)

        touch(bubble, MotionEvent.ACTION_DOWN, 100f, 100f)
        touch(bubble, MotionEvent.ACTION_MOVE, 150f, 130f)
        touch(bubble, MotionEvent.ACTION_UP, 150f, 130f)
        assertEquals(50, params.x)
        assertEquals(230, params.y)
        assertEquals("a drag is not a tap", 0, clicks)

        touch(bubble, MotionEvent.ACTION_DOWN, 10f, 10f)
        touch(bubble, MotionEvent.ACTION_MOVE, 15f, 18f)
        touch(bubble, MotionEvent.ACTION_UP, 15f, 18f)
        assertEquals("a touch that moves 10 px or less is a tap", 1, clicks)
    }

    @Test fun `the panel goes full width with the keyboard flags and its handle drags it`() {
        val panel = LinearLayout(app)
        val handle = View(app)
        panel.addView(handle)
        windows.addPanel(panel, handle)

        assertTrue(panel in onScreen)
        val params = panel.layoutParams as WindowManager.LayoutParams
        assertEquals(WindowManager.LayoutParams.MATCH_PARENT, params.width)
        assertEquals(100, params.y)
        val keyboardFlags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
        assertEquals(keyboardFlags, params.flags and keyboardFlags)
        assertEquals(0, params.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE)
        assertEquals(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE, params.softInputMode)

        touch(handle, MotionEvent.ACTION_DOWN, 0f, 0f)
        touch(handle, MotionEvent.ACTION_MOVE, -40f, 60f)
        assertEquals(-40, params.x)
        assertEquals(160, params.y)

        windows.updatePanelForKeyboard(true)
        assertEquals(keyboardFlags, (panel.layoutParams as WindowManager.LayoutParams).flags and keyboardFlags)

        windows.removeAll()
        assertFalse(panel in onScreen)
    }

    @Test fun `the consent banner shows once on a phone and goes on hide`() {
        windows.showControlBanner()
        val banner = windows.controlBannerView
        assertNotNull(banner)
        assertTrue(banner in onScreen)
        windows.showControlBanner()
        assertSame("idempotent", banner, windows.controlBannerView)
        assertEquals(1, onScreen.count { it === banner })
        val row = banner as LinearLayout
        assertEquals("🔴  AI is controlling this device", (row.getChildAt(0) as TextView).text.toString())
        assertEquals("STOP", (row.getChildAt(1) as TextView).text.toString())

        windows.hideControlBanner()
        assertNull(windows.controlBannerView)
        assertFalse(banner in onScreen)
    }

    @Test fun `XR never gets the phone banner`() {
        xr = true
        windows.showControlBanner()
        assertNull(windows.controlBannerView)
    }

    @Test fun `the stop button stops the recording and its timer ticks only while one runs`() {
        windows.showRecordingStopButton()
        val button = windows.recordingStopButton!!
        assertTrue(button in onScreen)
        assertEquals(50, (button.layoutParams as WindowManager.LayoutParams).y)
        val timer = button.findViewById<TextView>(R.id.recordingTimer)
        assertEquals("00:00", timer.text.toString())

        button.findViewById<View>(R.id.btnStopRecording).performClick()
        assertEquals(1, stops)
        button.performClick()
        assertEquals("the whole bar stops it too", 2, stops)

        val looper = shadowOf(Looper.getMainLooper())
        timer.text = "x"
        looper.idleFor(Duration.ofMillis(1100))
        assertTrue("ticking while recording", timer.text.toString().matches(Regex("\\d\\d:\\d\\d")))

        windows.hideRecordingStopButton()
        assertEquals(View.GONE, button.visibility)
        timer.text = "x"
        looper.idleFor(Duration.ofMillis(3000))
        assertEquals("hidden: the timer stopped", "x", timer.text.toString())

        windows.showRecordingStopButton()
        assertSame("shown again, not rebuilt", button, windows.recordingStopButton)
        assertEquals(View.VISIBLE, button.visibility)
        recording = false
        timer.text = "x"
        looper.idleFor(Duration.ofMillis(3000))
        assertEquals("no recording: the timer does not tick", "x", timer.text.toString())

        windows.removeRecordingStopButton()
        assertNull(windows.recordingStopButton)
        assertFalse(button in onScreen)
    }

    @Test fun `recording time reads as minutes and seconds`() {
        assertEquals("00:00", formatRecordingTime(999))
        assertEquals("00:59", formatRecordingTime(59_999))
        assertEquals("01:05", formatRecordingTime(65_000))
        assertEquals("61:01", formatRecordingTime(3_661_000))
    }
}
