package com.aiblackbox.portal.ui.cu

import android.annotation.SuppressLint
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView

/** CU live view (D9: WebView reuse): loads ${baseUrl}/cu/view/{sessionId} — the
 *  Orchestrator-served noVNC viewer page. Every gesture/cursor/extra-keys
 *  feature of the streaming client (design 2026-07-23) lands here with zero
 *  Kotlin UI code because the client IS the served page. The WebSocket stream
 *  (WS /cu/view/{sid}/ws) rides the page's JS — javaScriptEnabled covers it;
 *  WebView has no separate WS switch. */
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
                // Multi-touch pass-through: the served page's gesture layer
                // (touch-action:none overlay) owns pinch-zoom/pan/touchpad
                // gestures. Disable the WebView's OWN zoom so it never
                // consumes a pinch before the page sees the raw touches.
                settings.setSupportZoom(false)
                settings.builtInZoomControls = false
                // Respect the page's viewport meta (the viewer sizes itself).
                settings.useWideViewPort = true
                // Defense-in-depth (mirrors WizardWebViewScreen): this loads only an
                // http(s) server origin (the noVNC viewer) with no JS bridge/file
                // upload, so deny local file:// and content:// access outright.
                settings.allowFileAccess = false
                settings.allowContentAccess = false
                // BLACK-SCREEN HUNT (Brandon field find 2026-07-23): the served
                // page rendered PURE BLACK in this WebView while identical in
                // Chrome. The /cu/view/diag beacons PROVED the page loads, the
                // whole ES-module chain boots, and the RFB stream CONNECTS in
                // this WebView — so the loss is in view-surface rendering, not
                // networking or JS. v1.5.4 therefore aligns this settings block
                // with the field-proven WizardWebViewScreen config:
                //  - NO setLayerType(LAYER_TYPE_HARDWARE): forcing an explicit
                //    hardware layer on a WebView is a documented trigger for
                //    exactly this black-surface symptom (the WebView manages
                //    its own compositor surface; an outer HW layer can swallow
                //    it). The wizard WebView works without it.
                //  - MIXED_CONTENT_COMPATIBILITY_MODE: wizard parity.
                settings.mixedContentMode =
                    android.webkit.WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
                // Darkening opt-outs stay (harmless, defensive): never let the
                // WebView re-shade the already-dark page or its canvas.
                if (android.os.Build.VERSION.SDK_INT >= 33) {
                    settings.isAlgorithmicDarkeningAllowed = false
                }
                @Suppress("DEPRECATION")
                if (android.os.Build.VERSION.SDK_INT in 29..32) {
                    settings.forceDark = android.webkit.WebSettings.FORCE_DARK_OFF
                }
                setBackgroundColor(android.graphics.Color.BLACK)
                webViewClient = WebViewClient()
                loadUrl("${baseUrl.trimEnd('/')}/cu/view/$sessionId")
            }
        },
        // Release the WebView (JS/DOM-storage engine) on teardown to avoid a leak
        // every time the composable leaves composition (mirrors WizardWebViewScreen).
        onRelease = { it.destroy() },
    )
}
