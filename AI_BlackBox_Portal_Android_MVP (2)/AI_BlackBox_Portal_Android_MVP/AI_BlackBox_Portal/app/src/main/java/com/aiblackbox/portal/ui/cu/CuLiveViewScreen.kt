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
    AndroidView(modifier = modifier.fillMaxSize(), factory = { ctx ->
        WebView(ctx).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            webViewClient = WebViewClient()
            loadUrl("${baseUrl.trimEnd('/')}/cu/view/$sessionId")
        }
    })
}
