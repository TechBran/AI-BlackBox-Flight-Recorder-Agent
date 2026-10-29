package com.aiblackbox.portal.overlay

import android.content.Context
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.util.Log
import android.view.Gravity
import android.view.LayoutInflater
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import com.aiblackbox.portal.R
import com.aiblackbox.portal.data.remote.RemoteSessionBus

private const val TAG = "OverlayService"

/** A recording's elapsed time as `mm:ss`. */
internal fun formatRecordingTime(ms: Long): String {
    val seconds = ((ms / 1000) % 60).toInt()
    val minutes = ((ms / 1000) / 60).toInt()
    return String.format("%02d:%02d", minutes, seconds)
}

/**
 * The overlay's floating windows, out of OverlayService (audit A02-overlay-service-12, rank 76):
 * the bubble and the expanded panel as WindowManager windows (their params, dragging, the
 * keyboard flags), the remote-control consent banner, and the floating recording stop button
 * with its timer. Moved verbatim. What goes inside the bubble and panel (inflating them, their
 * buttons and fields) stays with the service; this class only puts them on screen.
 */
internal class OverlayWindows(
    private val context: Context,
    private val handler: Handler,
    private val windowManager: () -> WindowManager,
    /** XR draws its own in-headset panel, so the phone banner is never added there. */
    private val isXrDevice: () -> Boolean,
    private val showToast: (String) -> Unit,
    /** Whether a recording is running: the stop button's timer ticks only while it is. */
    private val isRecording: () -> Boolean,
    /** The stop button (or its bar) was tapped. */
    private val onStopRecording: () -> Unit,
    /** The stop button's view; the layout by default, a stand-in in tests. */
    private val inflateRecordingStopButton: () -> View = {
        LayoutInflater.from(context).inflate(R.layout.overlay_recording_stop, null)
    },
) {
    private var bubble: View? = null
    private var panel: View? = null

    /** The persistent "AI is controlling this device" banner overlay, or null when idle. */
    internal var controlBannerView: View? = null
        private set

    // Recording stop button UI
    internal var recordingStopButton: View? = null
        private set
    private var recordingTimer: TextView? = null
    private var recordingIndicator: View? = null
    private var recordingStartTime: Long = 0L
    private val recordingTimerRunnable = object : Runnable {
        override fun run() {
            if (isRecording()) {
                val elapsed = System.currentTimeMillis() - recordingStartTime
                recordingTimer?.text = formatRecordingTime(elapsed)
                // Pulse the recording indicator
                recordingIndicator?.animate()?.alpha(if (recordingIndicator?.alpha == 1.0f) 0.3f else 1.0f)?.setDuration(500)?.start()
                handler.postDelayed(this, 1000)
            }
        }
    }

    // ==================== Bubble & panel ====================

    /** Put the collapsed bubble on screen near the top-left; dragging it moves it, a tap clicks it. */
    fun addBubble(view: View) {
        bubble = view
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = 0
            y = 200
        }

        // Make bubble draggable
        setupDragListener(view, view, params)

        try {
            windowManager().addView(view, params)
            Log.d(TAG, "Floating bubble created")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add bubble view", e)
            showToast("Failed to create overlay: ${e.message}")
        }
    }

    /**
     * Put the expanded panel on screen (full width, focusable for the prompt's keyboard);
     * dragging [dragHandle] moves it. The panel's own visibility is the caller's.
     */
    fun addPanel(view: View, dragHandle: View?) {
        panel = view
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE,
            // Allow focus for keyboard input
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            x = 0
            y = 100
            // Set soft input mode
            softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE
        }

        // Make panel draggable by handle
        if (dragHandle != null) {
            setupDragListener(dragHandle, view, params)
        }

        try {
            windowManager().addView(view, params)
            Log.d(TAG, "Expanded panel created")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add expanded panel", e)
        }
    }

    fun updatePanelForKeyboard(showKeyboard: Boolean) {
        val expandedPanel = panel ?: return
        try {
            val params = expandedPanel.layoutParams as WindowManager.LayoutParams
            if (showKeyboard) {
                params.flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                               WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
            } else {
                params.flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                               WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH
            }
            windowManager().updateViewLayout(expandedPanel, params)
        } catch (e: Exception) {
            Log.e(TAG, "Error updating panel for keyboard", e)
        }
    }

    /**
     * Dragging [touchView] moves [window] (the bubble itself, or the panel by its handle);
     * a touch that moves no more than 10 px is a click on [touchView].
     */
    private fun setupDragListener(touchView: View, window: View, params: WindowManager.LayoutParams) {
        var initialX = 0
        var initialY = 0
        var initialTouchX = 0f
        var initialTouchY = 0f
        var isDragging = false

        touchView.setOnTouchListener { v, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    initialX = params.x
                    initialY = params.y
                    initialTouchX = event.rawX
                    initialTouchY = event.rawY
                    isDragging = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val deltaX = (event.rawX - initialTouchX).toInt()
                    val deltaY = (event.rawY - initialTouchY).toInt()

                    if (Math.abs(deltaX) > 10 || Math.abs(deltaY) > 10) {
                        isDragging = true
                    }

                    params.x = initialX + deltaX
                    params.y = initialY + deltaY

                    try {
                        windowManager().updateViewLayout(window, params)
                    } catch (e: Exception) {
                        Log.e(TAG, "Error updating view layout", e)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (!isDragging) {
                        v.performClick()
                    }
                    true
                }
                else -> false
            }
        }
    }

    /**
     * The service is going away: take the bubble, the panel and the stop button off screen.
     * Best-effort, as before: a failure is logged and stops the rest.
     */
    fun removeAll() {
        try {
            bubble?.let { windowManager().removeView(it) }
            panel?.let { windowManager().removeView(it) }
            // Cleanup recording stop button
            removeRecordingStopButton()
        } catch (e: Exception) {
            Log.e(TAG, "Error removing views", e)
        }
    }

    // ---- (M1.4) Remote-control consent banner + kill switch -----------------

    /**
     * Add the top-of-screen consent banner (a "🔴 AI is controlling this device" label +
     * a STOP button that fires [RemoteSessionBus.stop]). Built programmatically (no XML) so
     * it adds no layout resource. Phone/tablet path only — XR renders its own in-headset
     * panel (M6.3), and the always-on notification banner ([NotificationListenerFgs]) is the
     * universal fail-safe. Idempotent + best-effort.
     */
    fun showControlBanner() {
        if (isXrDevice()) return
        if (controlBannerView != null) return
        runCatching {
            val density = context.resources.displayMetrics.density
            fun dp(v: Int) = (v * density).toInt()

            val row = LinearLayout(context).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(16), dp(10), dp(16), dp(10))
                setBackgroundColor(0xEEB00020.toInt()) // strong red, mostly opaque
            }
            val label = TextView(context).apply {
                text = "🔴  AI is controlling this device"
                setTextColor(0xFFFFFFFF.toInt())
                textSize = 14f
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            val stop = TextView(context).apply {
                text = "STOP"
                setTextColor(0xFFB00020.toInt())
                textSize = 14f
                setPadding(dp(18), dp(8), dp(18), dp(8))
                setBackgroundColor(0xFFFFFFFF.toInt())
                setOnClickListener { RemoteSessionBus.stop() }
            }
            row.addView(label)
            row.addView(stop)

            val params = WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                else
                    @Suppress("DEPRECATION")
                    WindowManager.LayoutParams.TYPE_PHONE,
                // Not focusable so it never steals input from the app being driven; the STOP
                // button still receives touches (FLAG_NOT_FOCUSABLE keeps key focus away only).
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
                PixelFormat.TRANSLUCENT,
            ).apply {
                gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            }
            windowManager().addView(row, params)
            controlBannerView = row
            Log.i(TAG, "control consent banner shown")
        }.onFailure {
            Log.w(TAG, "control banner add failed (${it.javaClass.simpleName})")
        }
    }

    /** Remove the consent banner. Best-effort; never throws. */
    fun hideControlBanner() {
        val view = controlBannerView ?: return
        controlBannerView = null
        runCatching { windowManager().removeView(view) }
        Log.i(TAG, "control consent banner hidden")
    }

    // ==================== Recording Stop Button ====================

    private var recordingStopButtonParams: WindowManager.LayoutParams? = null

    private fun createRecordingStopButton() {
        if (recordingStopButton != null) return

        recordingStopButton = inflateRecordingStopButton()

        recordingStopButtonParams = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            else
                @Suppress("DEPRECATION")
                WindowManager.LayoutParams.TYPE_PHONE,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            x = 0
            y = 50  // Near top of screen
        }

        // Get UI references
        recordingTimer = recordingStopButton?.findViewById(R.id.recordingTimer)
        recordingIndicator = recordingStopButton?.findViewById(R.id.recordingIndicator)
        val stopBtn = recordingStopButton?.findViewById<ImageView>(R.id.btnStopRecording)

        // Setup stop button click listener - stops recording
        stopBtn?.setOnClickListener {
            Log.d(TAG, "Recording stop button clicked")
            onStopRecording()
        }

        // Also allow clicking anywhere on the bar to stop
        recordingStopButton?.setOnClickListener {
            Log.d(TAG, "Recording stop bar clicked")
            onStopRecording()
        }

        // Make it draggable
        setupRecordingStopDragListener()

        try {
            windowManager().addView(recordingStopButton, recordingStopButtonParams)
            Log.d(TAG, "Recording stop button created")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add recording stop button", e)
        }
    }

    private fun setupRecordingStopDragListener() {
        var initialX = 0
        var initialY = 0
        var initialTouchX = 0f
        var initialTouchY = 0f
        var isDragging = false

        recordingStopButton?.setOnTouchListener { v, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    initialX = recordingStopButtonParams?.x ?: 0
                    initialY = recordingStopButtonParams?.y ?: 0
                    initialTouchX = event.rawX
                    initialTouchY = event.rawY
                    isDragging = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val deltaX = (event.rawX - initialTouchX).toInt()
                    val deltaY = (event.rawY - initialTouchY).toInt()

                    if (Math.abs(deltaX) > 10 || Math.abs(deltaY) > 10) {
                        isDragging = true
                    }

                    recordingStopButtonParams?.x = initialX + deltaX
                    recordingStopButtonParams?.y = initialY + deltaY

                    try {
                        windowManager().updateViewLayout(recordingStopButton, recordingStopButtonParams)
                    } catch (e: Exception) {
                        Log.e(TAG, "Error updating recording stop button layout", e)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (!isDragging) {
                        v.performClick()
                    }
                    true
                }
                else -> false
            }
        }
    }

    fun showRecordingStopButton() {
        if (recordingStopButton == null) {
            createRecordingStopButton()
        }
        recordingStopButton?.visibility = View.VISIBLE

        // Start timer
        recordingStartTime = System.currentTimeMillis()
        recordingTimer?.text = "00:00"
        recordingIndicator?.alpha = 1.0f
        handler.post(recordingTimerRunnable)

        Log.d(TAG, "Recording stop button shown")
    }

    fun hideRecordingStopButton() {
        handler.removeCallbacks(recordingTimerRunnable)
        recordingStopButton?.visibility = View.GONE
        Log.d(TAG, "Recording stop button hidden")
    }

    fun removeRecordingStopButton() {
        handler.removeCallbacks(recordingTimerRunnable)
        try {
            recordingStopButton?.let {
                windowManager().removeView(it)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error removing recording stop button", e)
        }
        recordingStopButton = null
        recordingTimer = null
        recordingIndicator = null
        recordingStopButtonParams = null
    }
}
