package com.aiblackbox.portal.ui.cu

import android.annotation.SuppressLint
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.statusBars
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
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
    // Host insets → page (Fold fit pass 2026-07-23): the WebView draws
    // edge-to-edge under the status bar, and the activity floats its composer
    // + model pills OVER the page bottom. ?ti=&bi= (CSS px == dp) let the
    // served viewer pad its topbar clear of the status bar and lift its
    // switcher/extra-keys bars above the composer stack. The /cu/view/auto
    // 302 preserves the params server-side.
    val density = LocalDensity.current
    val topInsetDp = with(density) {
        WindowInsets.statusBars.getTop(this).toDp().value.toInt()
    }
    // Nav bar + measured-composer allowance (input bubble + pill row + gaps —
    // mirrors CuScreen's bottomClearance constant).
    val bottomInsetDp = with(density) {
        WindowInsets.navigationBars.getBottom(this).toDp().value.toInt()
    } + 118
    AndroidView(
        modifier = modifier.fillMaxSize(),
        factory = { ctx ->
            WebView(ctx).apply {
                // ROOT CAUSE of the v1.5.0–1.5.4 black screen (paint-probe
                // verdict 2026-07-23: viewport 599x0 CSS px = full inner-screen
                // WIDTH x ZERO height, stream healthy, desktop rendered into a
                // 2x1px sliver): a WebView constructed WITHOUT LayoutParams
                // inside Compose's AndroidView can get measured at zero height
                // on the first pass and the renderer stays locked there. The
                // canonical fix — explicit MATCH_PARENT params so every
                // measure pass resolves full-size. (The wizard WebView dodges
                // this via Column+weight; this one sits in a nav-destination
                // Box and does not.)
                layoutParams = android.view.ViewGroup.LayoutParams(
                    android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                    android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                )
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
                loadUrl("${baseUrl.trimEnd('/')}/cu/view/$sessionId" +
                        "?ti=$topInsetDp&bi=$bottomInsetDp")
            }
        },
        // Release the WebView (JS/DOM-storage engine) on teardown to avoid a leak
        // every time the composable leaves composition (mirrors WizardWebViewScreen).
        onRelease = { it.destroy() },
    )
}
