package com.aiblackbox.portal.overlay

import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import kotlinx.coroutines.suspendCancellableCoroutine
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.resume

/**
 * Production [ConfirmUi] (Phase 4, Task 4.6): a SYSTEM overlay that asks the user
 * to Allow or Deny a high-consequence phone action before the on-device agent
 * fires it (in [AutonomyMode.PERMISSION]).
 *
 * ## Why a SYSTEM overlay, not an in-app dialog
 * When Gemma drives the phone, the user is looking at ANOTHER app (the one being
 * operated). An in-app Compose dialog would render behind that app and be
 * invisible. So this draws over everything using `TYPE_APPLICATION_OVERLAY`
 * (SYSTEM_ALERT_WINDOW — already declared in the manifest), the same window type
 * the existing assistant bubble uses.
 *
 * ## Suspends until the user answers — with an I1 fail-safe TIMEOUT
 * [confirm] adds the overlay, then suspends via [suspendCancellableCoroutine]
 * until a button is tapped (Allow → true, Deny → false), removing the view on the
 * main thread either way. If coroutine is cancelled, the view is torn down and the
 * default is DENY (fail-safe: a cancelled confirmation never silently allows). If
 * the overlay permission isn't granted or the window can't be added, it also
 * fails SAFE by returning DENY (the actuator then reports "user declined" rather
 * than firing an un-confirmed high-consequence action).
 *
 * **(I1, M4) Timeout.** The suspend is wrapped in [awaitConfirmOrDeny] at
 * [DEFAULT_CONFIRM_TIMEOUT_MS]: a PERMISSION prompt raised while NOBODY is at the
 * device cannot block indefinitely (which would pin the NanoHTTPD worker thread that
 * ran the remote dispatch and hang the cloud loop). On timeout it tears the overlay
 * view down (no leak) and returns DENY.
 *
 * ## Leak discipline
 * [description] is built by [describeAction] and NEVER contains a typed secret or
 * a password node's text (the gate feeds it a null label for password targets).
 * This class adds nothing to it and logs only the DECISION, never the description.
 *
 * ## Verification
 * This is framework/device code (WindowManager, real views) — it is NOT
 * unit-tested. Its on-device verification (overlay renders over the target app,
 * Allow/Deny resolve correctly, fail-safe paths) is deferred to Task 4.8. The pure
 * decision core it serves ([isHighConsequence]/[shouldConfirm]/[describeAction])
 * is exhaustively unit-tested in `ConfirmGateTest`.
 *
 * @param context an application context (the overlay is app-scoped, not tied to
 *   an Activity lifecycle).
 */
class OverlayConfirmUi(context: Context) : ConfirmUi {

    private val appContext = context.applicationContext
    private val main = Handler(Looper.getMainLooper())

    override suspend fun confirm(description: String): Boolean {
        // Fail safe: if we can't legally draw an overlay, DENY (never silently fire).
        if (!canDrawOverlay()) {
            Log.w(TAG, "overlay permission not granted -> denying high-consequence action")
            return false
        }
        val wm = appContext.getSystemService(Context.WINDOW_SERVICE) as? WindowManager
            ?: return false

        // Hoisted to method scope so BOTH the button/cancel callbacks AND the I1 fail-safe
        // timeout teardown share ONE single-shot guard (resume-once + remove-view-once).
        val finished = AtomicBoolean(false)
        val viewRef = AtomicReference<View?>(null)
        fun removeOverlay() {
            main.post { viewRef.getAndSet(null)?.let { v -> runCatching { wm.removeView(v) } } }
        }

        // I1 (M4): a PERMISSION confirm with nobody at the device must NOT block forever
        // (it would pin the NanoHTTPD worker thread that ran the dispatch + hang the cloud
        // loop). awaitConfirmOrDeny caps the wait at DEFAULT_CONFIRM_TIMEOUT_MS → on timeout
        // it tears the overlay down (onTimeout) and DENIES. The existing missing-overlay /
        // exception / cancel DENY paths are preserved.
        return awaitConfirmOrDeny(
            timeoutMs = DEFAULT_CONFIRM_TIMEOUT_MS,
            onTimeout = {
                if (finished.compareAndSet(false, true)) {
                    Log.w(TAG, "autonomy confirm timed out -> denying high-consequence action")
                    removeOverlay()
                }
            },
        ) {
            suspendCancellableCoroutine { cont ->
                // Built + added + removed on the main thread (View/WindowManager rule).
                main.post {
                    fun finish(allowed: Boolean) {
                        if (!finished.compareAndSet(false, true)) return
                        viewRef.getAndSet(null)?.let { v -> runCatching { wm.removeView(v) } }
                        Log.i(TAG, "autonomy confirm: ${if (allowed) "allowed" else "denied"}")
                        if (cont.isActive) cont.resume(allowed)
                    }

                    try {
                        val v = buildView(description, onAllow = { finish(true) }, onDeny = { finish(false) })
                        viewRef.set(v)
                        wm.addView(v, layoutParams())
                    } catch (e: Exception) {
                        Log.w(TAG, "failed to show confirm overlay (${e.javaClass.simpleName}) -> denying")
                        finish(false) // fail safe
                        return@post
                    }

                    // If the coroutine is cancelled (turn stopped OR the I1 timeout), tear
                    // down + DENY. Guarded by `finished`, so it never double-removes.
                    cont.invokeOnCancellation { main.post { finish(false) } }
                }
            }
        }
    }

    private fun canDrawOverlay(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(appContext)

    private fun layoutParams(): WindowManager.LayoutParams {
        @Suppress("DEPRECATION")
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else
            WindowManager.LayoutParams.TYPE_PHONE
        return WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            // Focusable so the buttons receive taps; dim behind so it reads as modal.
            WindowManager.LayoutParams.FLAG_DIM_BEHIND,
            android.graphics.PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.CENTER
            dimAmount = 0.5f
        }
    }

    private fun buildView(description: String, onAllow: () -> Unit, onDeny: () -> Unit): View {
        val pad = dp(20)
        val card = LinearLayout(appContext).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            background = GradientDrawable().apply {
                cornerRadius = dp(16).toFloat()
                setColor(Color.parseColor("#1E1E1E"))
            }
        }

        card.addView(TextView(appContext).apply {
            text = "Confirm action"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            setPadding(0, 0, 0, dp(8))
        })

        card.addView(TextView(appContext).apply {
            text = description
            setTextColor(Color.parseColor("#CCCCCC"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            setPadding(0, 0, 0, dp(16))
        })

        val buttonRow = LinearLayout(appContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END
        }
        buttonRow.addView(Button(appContext).apply {
            text = "Deny"
            setOnClickListener { onDeny() }
        })
        buttonRow.addView(Button(appContext).apply {
            text = "Allow"
            setOnClickListener { onAllow() }
        })
        card.addView(buttonRow)
        return card
    }

    private fun dp(v: Int): Int =
        (v * appContext.resources.displayMetrics.density).toInt()

    companion object {
        private const val TAG = "OverlayConfirmUi"
    }
}
