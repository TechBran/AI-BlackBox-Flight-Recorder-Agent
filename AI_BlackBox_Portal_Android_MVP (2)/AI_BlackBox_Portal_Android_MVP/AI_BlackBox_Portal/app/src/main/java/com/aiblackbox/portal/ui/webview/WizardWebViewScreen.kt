package com.aiblackbox.portal.ui.webview

import android.webkit.WebSettings
import android.webkit.WebView
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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

/**
 * In-app WebView host for the onboarding wizard (M4).
 *
 * Replaces the previous external-browser hand-off (`Intent(ACTION_VIEW, …)`)
 * for the "Manage Setup" / embeddings-step / reranker deep-links. Model and
 * reranker selection now live ONLY in the onboarding wizard, so keeping it
 * in-app means a selection change never leaves the app — and on close the
 * activity re-fetches update/embedding status so the native badge + Updates
 * screen reflect it (see [onClose] wiring in BlackBoxNavGraph → NativeMainActivity).
 *
 * URL construction (M4 fix): the target is built from the Compose [origin]
 * — the same `normalizeApiOrigin` base the ACTION_VIEW intents used — as
 * `"$origin/onboarding/$suffix"`. It intentionally does NOT reuse
 * PortalActivity.normalizeOrigin()/its `/ui/` suffix logic, which would yield
 * `$base/ui/onboarding/…` and 404. [suffix] is e.g. `?step=embeddings` or
 * `?mode=manage`.
 *
 * Only PortalActivity's WebSettings block is mirrored (javaScriptEnabled,
 * domStorageEnabled, MIXED_CONTENT_COMPATIBILITY_MODE), with the shared
 * [BbxWebViewClient] (navigation stays inside this WebView; a renderer crash remounts
 * it) and [BbxWebChromeClient] (JS dialogs and <input type=file> pickers work).
 *
 * KNOWN LIMITATION (m1): the onboarding SPA intercepts `target="_blank"`
 * links — the API-key "get your key" links and the Google-OAuth console /
 * service-account links — and POSTs them to `/onboarding/open-url`, which
 * spawns a browser ON THE BOX (the server host), not on this phone. Those
 * external hand-offs are therefore invisible here. Plain-text KEY PASTE
 * (e.g. Cohere / Voyage / Jina reranker keys, or any BYOK field) works fully
 * inside this WebView — that is the supported in-app path. This is a
 * server-side spawn we deliberately do NOT try to intercept/fix client-side.
 */
@Composable
fun WizardWebViewScreen(
    origin: String,
    suffix: String,
    onClose: () -> Unit,
) {
    // Hold the created WebView so system-back can drive in-WebView history
    // before popping the destination (mirrors PortalActivity's
    // OnBackPressedCallback: goBack() while canGoBack(), else exit).
    val webViewRef = remember { mutableStateOf<WebView?>(null) }
    // The page's <input type=file> opens the platform picker.
    val fileChooser = rememberWebFileChooser()
    // Bumped when the renderer dies, which remounts a fresh WebView.
    var rendererGeneration by remember { mutableIntStateOf(0) }

    BackHandler {
        val web = webViewRef.value
        if (web != null && web.canGoBack()) web.goBack() else onClose()
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(BbxBlack),
    ) {
        // Top bar with an explicit X-close (hard exit; back gesture does the
        // goBack-while-canGoBack dance above).
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .statusBarsPadding()
                .padding(start = 16.dp, end = 12.dp, top = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "Setup",
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

        // A renderer crash replaces the WebView with a fresh one instead of killing the app.
        key(rendererGeneration) {
            AndroidView(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f),
                factory = { ctx ->
                    WebView(ctx).apply {
                        // Reuse ONLY PortalActivity's WebSettings block — not its
                        // origin/`/ui/` URL logic.
                        settings.javaScriptEnabled = true
                        settings.domStorageEnabled = true
                        settings.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
                        // G3-T13 M-2 defense-in-depth: loads only an http(s) server origin
                        // (the onboarding SPA) with no JS bridge, so deny the PAGE local
                        // file:// and content:// URLs. A file the user picks for an
                        // <input type=file> is handed over by the chooser, not loaded.
                        settings.allowFileAccess = false
                        settings.allowContentAccess = false
                        // Keeps same-origin SPA navigation inside the WebView (no
                        // external-browser bounce on internal links) and survives a
                        // renderer crash (audit rank 62).
                        webViewClient = BbxWebViewClient(onRendererGone = { rendererGeneration++ })
                        // confirm()/alert() dialogs and <input type=file> pickers: the
                        // hub's Install Now, the wizard's "Remove operator", credential-file
                        // upload and update alerts silently did nothing without a chrome
                        // client (a WebView with none cancels every JS dialog).
                        webChromeClient = BbxWebChromeClient(fileChooser)
                        loadUrl("$origin/onboarding/$suffix")
                        webViewRef.value = this
                    }
                },
                onRelease = {
                    if (webViewRef.value === it) webViewRef.value = null
                    it.destroy()
                },
            )
        }
    }
}
