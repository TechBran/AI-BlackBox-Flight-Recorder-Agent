package com.aiblackbox.portal.ui.cu

import android.annotation.SuppressLint
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView

/** Watch-only CU live view: loads ${baseUrl}/cu/view/{sessionId}, which serves
 *  the Orchestrator's noVNC viewer (the JS runs in the WebView). */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun CuLiveViewScreen(baseUrl: String, sessionId: String, modifier: Modifier = Modifier) {
    AndroidView(
        modifier = modifier.fillMaxSize(),
        factory = { ctx ->
            WebView(ctx).apply {
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true
                settings.mediaPlaybackRequiresUserGesture = false
                // Defense-in-depth (mirrors WizardWebViewScreen): this loads only an
                // http(s) server origin (the noVNC viewer) with no JS bridge/file
                // upload, so deny local file:// and content:// access outright.
                settings.allowFileAccess = false
                settings.allowContentAccess = false
                webViewClient = WebViewClient()
                loadUrl("${baseUrl.trimEnd('/')}/cu/view/$sessionId")
            }
        },
        // Release the WebView (JS/DOM-storage engine) on teardown to avoid a leak
        // every time the composable leaves composition (mirrors WizardWebViewScreen).
        onRelease = { it.destroy() },
    )
}
