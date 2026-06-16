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
import kotlin.coroutines.resume

/**
 * Production [CredentialHandoff] (Phase 4, Task 4.7): a SYSTEM overlay that asks
 * the user to type their PASSWORD directly into the target app's field, then tap
 * Done — so the on-device agent never sees the secret.
 *
 * ## Why this exists
 * When the on-device Gemma agent tries to fill a password field, [Actuators.type]
 * DISCARDS the model's attempted text and calls [requestUserEntry]. This overlay
 * appears OVER the app the agent is driving (the user is looking at THAT app, not
 * the Portal), prompts the user to enter the secret in the field below it, and
 * suspends until the user taps **Done** (→ `true`, the agent resumes and can e.g.
 * tap Sign In) or **Cancel** (→ `false`, the agent reports the decline). The
 * password is typed by the user straight into the real field — it never passes
 * through the model in either direction (read_screen redacts it in 4.2; the model's
 * attempted text is dropped in 4.7).
 *
 * ## Why a SYSTEM overlay (mirrors [OverlayConfirmUi])
 * `TYPE_APPLICATION_OVERLAY` (SYSTEM_ALERT_WINDOW, declared in the manifest) draws
 * over everything. It is deliberately NOT focusable / NOT modal-blocking the field:
 * the user must be able to tap into and type in the app's password field BENEATH
 * this prompt, so the window does not steal input focus (FLAG_NOT_FOCUSABLE) and
 * sits at the bottom so it doesn't cover the field/keyboard.
 *
 * ## Leak discipline
 * [requestUserEntry] is fed only the GENERIC [CREDENTIAL_FIELD_DESCRIPTION] ("the
 * password field") — never the model's attempted text or any field content. This
 * class echoes that generic description and logs only the DECISION (entered /
 * cancelled), never any text.
 *
 * ## Fail SAFE
 * If the overlay permission isn't granted, the WindowManager is unavailable, the
 * window can't be added, or the coroutine is cancelled, [requestUserEntry] returns
 * `false` (declined) — a credential entry is NEVER silently treated as done.
 *
 * ## Verification
 * Framework/device code (WindowManager, real views) — NOT unit-tested. Its
 * on-device verification (renders over the target app, the user can type into the
 * field beneath, Done/Cancel resolve correctly, fail-safe paths) is deferred to
 * Task 4.8. The pure decision it serves ([credentialDecision]) and the routing in
 * [Actuators.type] are unit-tested.
 *
 * @param context an application context (the overlay is app-scoped, not tied to an
 *   Activity lifecycle).
 */
class OverlayCredentialHandoff(context: Context) : CredentialHandoff {

    private val appContext = context.applicationContext
    private val main = Handler(Looper.getMainLooper())

    override suspend fun requestUserEntry(fieldDescription: String): Boolean {
        // Fail safe: if we can't legally draw an overlay, DECLINE (never silently
        // claim the user entered a credential).
        if (!canDrawOverlay()) {
            Log.w(TAG, "overlay permission not granted -> declining credential handoff")
            return false
        }
        return suspendCancellableCoroutine { cont ->
            val wm = appContext.getSystemService(Context.WINDOW_SERVICE) as? WindowManager
            if (wm == null) {
                cont.resume(false)
                return@suspendCancellableCoroutine
            }

            // Built + added + removed on the main thread (View/WindowManager rule).
            main.post {
                var view: View? = null
                // Guard so we resume exactly once and always tear the view down.
                val finished = booleanArrayOf(false)
                fun finish(entered: Boolean) {
                    if (finished[0]) return
                    finished[0] = true
                    view?.let { v -> runCatching { wm.removeView(v) } }
                    Log.i(TAG, "credential handoff: ${if (entered) "entered" else "cancelled"}")
                    if (cont.isActive) cont.resume(entered)
                }

                try {
                    view = buildView(
                        fieldDescription,
                        onDone = { finish(true) },
                        onCancel = { finish(false) },
                    )
                    wm.addView(view, layoutParams())
                } catch (e: Exception) {
                    Log.w(TAG, "failed to show credential overlay (${e.javaClass.simpleName}) -> declining")
                    finish(false) // fail safe
                    return@post
                }

                // If the coroutine is cancelled (turn stopped), tear down + DECLINE.
                cont.invokeOnCancellation { main.post { finish(false) } }
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
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            // NOT focusable: the user must be able to tap into and type in the app's
            // password field BENEATH this prompt. Don't dim/steal focus.
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            android.graphics.PixelFormat.TRANSLUCENT,
        ).apply {
            // Sit at the bottom so it doesn't cover the field / soft keyboard.
            gravity = Gravity.BOTTOM
        }
    }

    private fun buildView(fieldDescription: String, onDone: () -> Unit, onCancel: () -> Unit): View {
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
            text = "Enter your password"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            setPadding(0, 0, 0, dp(8))
        })

        card.addView(TextView(appContext).apply {
            // GENERIC description only — never the model's attempted text.
            text = "Please type your password into $fieldDescription yourself, then tap Done. " +
                "The assistant never sees it."
            setTextColor(Color.parseColor("#CCCCCC"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            setPadding(0, 0, 0, dp(16))
        })

        val buttonRow = LinearLayout(appContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END
        }
        buttonRow.addView(Button(appContext).apply {
            text = "Cancel"
            setOnClickListener { onCancel() }
        })
        buttonRow.addView(Button(appContext).apply {
            text = "Done"
            setOnClickListener { onDone() }
        })
        card.addView(buttonRow)
        return card
    }

    private fun dp(v: Int): Int =
        (v * appContext.resources.displayMetrics.density).toInt()

    companion object {
        private const val TAG = "OverlayCredentialHandoff"
    }
}
