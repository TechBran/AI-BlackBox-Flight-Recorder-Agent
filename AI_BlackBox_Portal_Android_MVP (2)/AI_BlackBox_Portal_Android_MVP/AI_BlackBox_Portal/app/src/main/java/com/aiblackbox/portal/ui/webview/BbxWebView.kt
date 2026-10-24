package com.aiblackbox.portal.ui.webview

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.ContextWrapper
import android.net.Uri
import android.util.Log
import android.webkit.JsPromptResult
import android.webkit.JsResult
import android.webkit.RenderProcessGoneDetail
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "BbxWebView"

/**
 * Delivers a WebView answer exactly once. A file chooser whose ValueCallback is never
 * called, or a JS dialog whose JsResult is never confirmed or cancelled, wedges the page
 * (the next <input type=file> never opens; the page's script stays blocked), so every
 * path — picked, cancelled, failed to launch, screen gone — must end in ONE delivery,
 * and a late second one must be dropped.
 */
class OnceValueCallback<T>(private val deliver: (T?) -> Unit) {
    private val done = AtomicBoolean(false)

    /** Deliver [value] (null = cancelled) if nothing was delivered yet; true if this call did. */
    fun complete(value: T?): Boolean {
        if (!done.compareAndSet(false, true)) return false
        deliver(value)
        return true
    }
}

/** Opens a picker for a WebView <input type=file>; must call [onResult] exactly once. */
fun interface FileChooserLauncher {
    /** Start the picker; false if it could not start (the caller then answers null). */
    fun launch(params: WebChromeClient.FileChooserParams, onResult: (Array<Uri>?) -> Unit): Boolean
}

/**
 * The app's WebChromeClient (audit rank 62). Without one, Android's WebView makes JS
 * confirm() return false, drops alert() and never opens a picker for <input type=file>:
 * the onboarding wizard's "Remove operator", its credential-file upload and its update
 * failure alerts all silently did nothing. Hosts with more to handle (permissions,
 * console) subclass it.
 *
 * @param fileChooser the host's picker, or null when the page needs none (the file input
 *   is then answered "cancelled" at once, never left hanging).
 */
open class BbxWebChromeClient(private val fileChooser: FileChooserLauncher? = null) : WebChromeClient() {

    override fun onJsAlert(view: WebView, url: String?, message: String?, result: JsResult): Boolean {
        val once = OnceValueCallback<Boolean> { result.confirm() }
        showDialog(view, once) {
            setMessage(message.orEmpty())
            setPositiveButton(android.R.string.ok) { _, _ -> once.complete(true) }
        }
        return true
    }

    override fun onJsConfirm(view: WebView, url: String?, message: String?, result: JsResult): Boolean {
        val once = OnceValueCallback<Boolean> { ok -> if (ok == true) result.confirm() else result.cancel() }
        showDialog(view, once) {
            setMessage(message.orEmpty())
            setPositiveButton(android.R.string.ok) { _, _ -> once.complete(true) }
            setNegativeButton(android.R.string.cancel) { _, _ -> once.complete(false) }
        }
        return true
    }

    override fun onJsPrompt(
        view: WebView,
        url: String?,
        message: String?,
        defaultValue: String?,
        result: JsPromptResult,
    ): Boolean {
        val input = EditText(view.context).apply { setText(defaultValue.orEmpty()) }
        val once = OnceValueCallback<String> { text -> if (text != null) result.confirm(text) else result.cancel() }
        showDialog(view, once) {
            setMessage(message.orEmpty())
            setView(input)
            setPositiveButton(android.R.string.ok) { _, _ -> once.complete(input.text.toString()) }
            setNegativeButton(android.R.string.cancel) { _, _ -> once.complete(null) }
        }
        return true
    }

    override fun onShowFileChooser(
        webView: WebView,
        filePathCallback: ValueCallback<Array<Uri>>,
        fileChooserParams: FileChooserParams,
    ): Boolean {
        val once = OnceValueCallback<Array<Uri>> { filePathCallback.onReceiveValue(it) }
        val started = runCatching { fileChooser?.launch(fileChooserParams) { once.complete(it) } ?: false }
            .getOrDefault(false)
        if (!started) once.complete(null)
        return true
    }

    /** Show a JS dialog; dismissing it (or failing to show it) answers "cancel". */
    private fun <T> showDialog(view: WebView, once: OnceValueCallback<T>, build: AlertDialog.Builder.() -> Unit) {
        val activity = view.context.findActivity()
        if (activity == null || activity.isFinishing || activity.isDestroyed) {
            once.complete(null)
            return
        }
        try {
            AlertDialog.Builder(activity)
                .apply(build)
                .setOnDismissListener { once.complete(null) }
                .show()
        } catch (e: Exception) {
            Log.w(TAG, "JS dialog not shown (${e.javaClass.simpleName})")
            once.complete(null)
        }
    }
}

/**
 * The app's WebViewClient base (audit rank 62): a renderer crash or an out-of-memory kill
 * of the renderer no longer takes the whole app down (the default answer, false, makes
 * Android kill the host process — chat, voice and running work with it). The dead
 * WebView must not be used again: [onRendererGone] replaces it (a Compose host remounts a
 * fresh one, an Activity recreates itself).
 */
open class BbxWebViewClient(private val onRendererGone: () -> Unit) : WebViewClient() {
    override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
        Log.w(TAG, "WebView renderer gone (crashed=${detail.didCrash()}); replacing the WebView")
        runCatching { onRendererGone() }
        return true
    }
}

/**
 * A [FileChooserLauncher] for a Compose screen: the platform picker the page's
 * <input type=file> describes (accept types, multiple). A chooser still open when the
 * screen leaves composition is answered "cancelled".
 */
@Composable
fun rememberWebFileChooser(): FileChooserLauncher {
    val pending = remember { arrayOfNulls<(Array<Uri>?) -> Unit>(1) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val callback = pending[0]
        pending[0] = null
        callback?.invoke(WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data))
    }
    DisposableEffect(Unit) {
        onDispose {
            pending[0]?.invoke(null)
            pending[0] = null
        }
    }
    return remember(launcher) {
        FileChooserLauncher { params, onResult ->
            // A chooser still pending is answered first: one open picker at a time.
            pending[0]?.invoke(null)
            pending[0] = onResult
            try {
                launcher.launch(params.createIntent())
                true
            } catch (e: Exception) {
                Log.w(TAG, "file chooser not started (${e.javaClass.simpleName})")
                pending[0] = null
                false
            }
        }
    }
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
