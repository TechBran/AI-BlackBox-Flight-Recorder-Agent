package com.aiblackbox.portal

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Origin checks for the legacy WebView wrapper ([PortalActivity]) —
 * A01-activities-root-01 / X04-security-clients-infra-05.
 *
 * That WebView exposes native bridges (mic PCM streaming, file writes into
 * Downloads, the overlay, the file picker) to whatever page it shows, and grants
 * page mic capture. Before, a link in a chat reply navigated it to a third-party
 * page that then owned every bridge, and the mic grant matched any https origin
 * or anything containing ".ts.net". Navigation, the bridges and the mic grant
 * are now all keyed to ONE origin: the paired box's.
 *
 * Pure (OkHttp's [HttpUrl] parser, no android.net.Uri) so it is unit-tested on
 * the JVM. HttpUrl parses the way the browser does where it matters here: the
 * host is lowercased, userinfo is split off (`https://box@evil.example` is
 * evil.example), a backslash ends the authority, and the port is the effective
 * one (443 for a bare https URL).
 */
internal object PortalWebGuard {

    /**
     * The web origin of [url] — `scheme://host[:port]`, the port omitted when it is
     * the scheme's default — or null when [url] is not an http(s) URL with a host
     * (about:, data:, javascript:, file:, content:, intent:, mailto:, junk).
     *
     * A `blob:` URL has the origin of the URL inside it, as in the browser. A
     * trailing dot on the host is the same DNS name and normalizes away, so
     * `box.example.` and `box.example` compare equal.
     */
    fun originOf(url: String?): String? {
        val raw = url?.trim().orEmpty()
        if (raw.isEmpty()) return null
        val target = if (raw.startsWith("blob:", ignoreCase = true)) raw.substring(5) else raw
        val parsed = target.toHttpUrlOrNull() ?: return null
        val host = parsed.host.removeSuffix(".")
        if (host.isEmpty()) return null
        val hostPart = if (':' in host) "[$host]" else host
        return if (parsed.port == HttpUrl.defaultPort(parsed.scheme)) {
            "${parsed.scheme}://$hostPart"
        } else {
            "${parsed.scheme}://$hostPart:${parsed.port}"
        }
    }

    /** True only when [url] is on [pairedOrigin] (itself an [originOf] result). */
    fun isPairedOrigin(url: String?, pairedOrigin: String?): Boolean {
        if (pairedOrigin == null) return false
        return originOf(url) == pairedOrigin
    }

    /**
     * shouldInterceptRequest refuses this request: a main-frame load off the paired box.
     * WebView never asks shouldOverrideUrlLoading about a form POST, so this is where an
     * off-box `<form method=post>` (a prompt-injected chat reply can carry one) is
     * stopped before the foreign page commits inside the app. Subresources are untouched.
     */
    fun blocksMainFrameLoad(url: String?, isForMainFrame: Boolean, pairedOrigin: String?): Boolean =
        isForMainFrame && !isPairedOrigin(url, pairedOrigin)

    /**
     * The client for PortalActivity's downloads into Downloads: every hop, redirects
     * included, must be on [pairedOrigin]. Checking only the first URL let a box URL that
     * redirects (an OAuth start, a proxied App's 302) write another origin's bytes into
     * Downloads. An off-box hop fails the call with an IOException before it is sent.
     */
    fun pairedOnlyDownloadClient(pairedOrigin: String?): OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .addNetworkInterceptor { chain ->
                val url = chain.request().url.toString()
                if (!isPairedOrigin(url, pairedOrigin)) {
                    throw IOException("Download left the BlackBox (${originOf(url) ?: "no origin"})")
                }
                chain.proceed(chain.request())
            }
            .build()

    /**
     * A file name a page asked to save, reduced to its last path segment so it
     * cannot climb out of Downloads (the pre-Q path writes `File(downloadsDir, name)`).
     */
    fun safeDownloadName(name: String): String {
        val leaf = name.substringAfterLast('/').substringAfterLast('\\').trim()
        return if (leaf.isEmpty() || leaf == "." || leaf == "..") "download" else leaf
    }
}
