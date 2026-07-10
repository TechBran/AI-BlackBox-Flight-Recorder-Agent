package com.aiblackbox.portal.ui.webview

import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxWhite
import org.json.JSONObject

/**
 * CU Live-view (G3-T13 / M3.3) — the CHEAP path per the plan: an in-app WebView
 * wrapper of the web `cu-interact` live viewer. A native Compose live-view is a
 * later nicety and is deliberately NOT built here.
 *
 * Mirrors [WizardWebViewScreen] (the app's established WebView-wrapper pattern):
 * same WebSettings block, plain [WebViewClient] to keep same-origin SPA
 * navigation inside the WebView, and the goBack-while-canGoBack BackHandler.
 *
 * SECURITY / INERTNESS:
 *   - The WebView loads ONLY the SERVER's Portal URL (`$origin/ui/`). No agent
 *     stdout / progress_text is ever loaded or rendered as markup here.
 *   - `cu-interact` renders SCREENSHOTS fetched from the server
 *     (`/browser/screenshot/live`) — it does not render agent text.
 *   - [deviceId] is passed to the page via [JSONObject.quote] (a properly escaped
 *     JS string literal), NOT string-interpolated into HTML/JS — so a hostile
 *     device_id cannot break out of the string.
 *
 * There is no standalone `cu-interact` URL (it is an SPA module opened by a JS
 * call), so on page load we dynamically import it and call
 * `open(undefined, <deviceId>)` — exactly what the Portal top-bar "Live" button
 * does (`ui-setup.js openLiveView`). The overlay it creates addresses the task's
 * real device.
 */
@Composable
fun CuLiveWebViewScreen(
    origin: String,
    deviceId: String,
    onClose: () -> Unit,
) {
    val webViewRef = remember { mutableStateOf<WebView?>(null) }

    BackHandler {
        val web = webViewRef.value
        if (web != null && web.canGoBack()) web.goBack() else onClose()
    }

    // Safe JS string literal for the device id (quotes + escapes; e.g. "blackbox").
    val deviceLiteral = JSONObject.quote(deviceId)
    // Open the interactive CU viewer against the task's device. Dynamic import so
    // it works regardless of SPA init state; relative imports inside the module
    // resolve against /ui/modules/. Fully self-contained — appends its own modal.
    val openScript =
        "(function(){try{import('/ui/modules/cu-interact.js')" +
            ".then(function(m){ if(m && typeof m.open==='function'){ m.open(undefined, $deviceLiteral); } })" +
            ".catch(function(e){ console.error('cu-live import failed', e); });}" +
            "catch(e){ console.error('cu-live', e); }})();"

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(BbxBlack),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(start = 16.dp, end = 12.dp, top = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "Live View",
                color = BbxWhite,
                fontSize = 18.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.weight(1f),
            )
            Box(
                modifier = Modifier
                    .size(36.dp)
                    .clip(CircleShape)
                    .background(Color(0xCC1C1C1E))
                    .border(1.dp, Color(0x33FFFFFF), CircleShape)
                    .clickable { onClose() },
                contentAlignment = Alignment.Center,
            ) {
                Text("✕", color = BbxWhite, fontSize = 16.sp, fontWeight = FontWeight.Medium)
            }
        }

        AndroidView(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f),
            factory = { ctx ->
                WebView(ctx).apply {
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    settings.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
                    // M-2 (G3-T13): defense-in-depth. This WebView only ever loads an
                    // http(s) server origin and exposes no JS bridge, so deny local
                    // file:// and content:// access outright.
                    settings.allowFileAccess = false
                    settings.allowContentAccess = false
                    webViewClient = object : WebViewClient() {
                        override fun onPageFinished(view: WebView?, url: String?) {
                            super.onPageFinished(view, url)
                            // Open the cu-interact overlay for the task's device once
                            // the Portal document is loaded.
                            view?.evaluateJavascript(openScript, null)
                        }
                    }
                    // SERVER URL ONLY — the Portal SPA host for cu-interact.
                    loadUrl("$origin/ui/")
                    webViewRef.value = this
                }
            },
            onRelease = { it.destroy() },
        )
    }
}
