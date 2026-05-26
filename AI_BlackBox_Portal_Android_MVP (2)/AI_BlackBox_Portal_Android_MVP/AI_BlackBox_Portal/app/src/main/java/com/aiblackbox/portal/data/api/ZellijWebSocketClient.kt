package com.aiblackbox.portal.data.api

import android.util.Log
import androidx.annotation.VisibleForTesting
import com.aiblackbox.portal.BuildConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.io.IOException
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * Kotlin client for zellij-web's two-socket protocol (zellij 0.44.3).
 *
 * Replaces [com.aiblackbox.portal.ui.cli_agent.CliAgentWebSocket] which talked
 * to the orchestrator's tmux-bridged WebSocket. Same public surface shape so
 * the T19 swap in TerminalScreen.kt is a one-line constructor change.
 *
 * ## Wire shape (see docs/notes/2026-05-25-zellij-ws-protocol.md)
 *
 * Two WebSockets per session, opened in order:
 *
 * 1. **Terminal WS** — `{wsScheme}://{host}/ws/terminal/{sessionName}?web_client_id={uuid}`
 *    Carries raw PTY bytes both directions. Session name is in the path
 *    (NOT the query string — query-form attaches to an empty name and
 *    zellij-web auto-creates an orphan session, the Phase 3 T11c
 *    "keystone" failure mode).
 * 2. **Control WS** — `{wsScheme}://{host}/ws/control`
 *    JSON envelope:
 *    ```
 *    {"web_client_id":"<uuid>","payload":{"type":"TerminalResize","rows":N,"cols":N}}
 *    ```
 *
 * ## Lifecycle
 *
 *   connect()
 *     → version probe (GET /info/version, warn on mismatch, never fail)
 *     → auth pre-flight (POST /command/login, sets session_token cookie)
 *     → open terminal WS
 *     → on first inbound message → open control WS
 *     → on control WS onOpen → send initial TerminalResize
 *
 * Close code 4001 = "intentional disconnect by host" → DO NOT reconnect.
 * Any other non-1000 code → reconnect with backoff [1, 2, 4, 8, 16] s.
 */
class ZellijWebSocketClient(
    private val origin: String,
    private val sessionName: String,
    private val sessionToken: String,
    private val webClientId: String = UUID.randomUUID().toString(),
    private val coroutineScope: CoroutineScope,
) {

    interface Listener {
        fun onConnected()
        /** Matches `TerminalEmulator.append(byte[], int)` signature so T19 can pass-through. */
        fun onBytes(bytes: ByteArray, length: Int)
        fun onSwitchedSession(newSessionName: String)
        fun onDisconnected(code: Int, reason: String, willReconnect: Boolean)
        fun onError(throwable: Throwable)
    }

    private val cookieJar: SimpleCookieJar = SimpleCookieJar()

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .cookieJar(cookieJar)
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // long-lived WS
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    @Volatile private var wsTerminal: WebSocket? = null
    @Volatile private var wsControl: WebSocket? = null

    /** Last grid dimensions sent — replayed on reconnect and on control-WS open. */
    @Volatile private var lastCols: Int = 80
    @Volatile private var lastRows: Int = 24

    /** Set true by [close]; suppresses reconnects forever. */
    private val userClosed: AtomicBoolean = AtomicBoolean(false)

    /** Guards double-launching the reconnect coroutine. */
    private val reconnecting: AtomicBoolean = AtomicBoolean(false)

    /** Snapshot of the current reconnect-loop index, exposed for tests only. */
    @Volatile private var lastReconnectIdx: Int = 0

    private val reconnectJob: AtomicReference<Job?> = AtomicReference(null)

    /** Listener slot; populated by [connect]. */
    @Volatile private var listener: Listener? = null

    /** Effective session name — flips on SwitchedSession control message. */
    @Volatile private var currentSessionName: String = sessionName

    // --- Public surface ---------------------------------------------------

    /**
     * Idempotent. Fires version probe → auth pre-flight → opens terminal WS.
     *
     * `probeVersion` and `preflightAuth` use OkHttp's BLOCKING `execute()`
     * API (not `enqueue()`), so they MUST run on `Dispatchers.IO` — running
     * them on the caller's `coroutineScope` (which is typically
     * `rememberCoroutineScope()` → AndroidUiDispatcher = main thread) would
     * trigger StrictMode's `NetworkOnMainThreadException`. T23 device QA
     * caught this on the Z Fold 6: Brandon saw "Error: unknown" because
     * `preflightAuth` threw with a null message (StrictMode's exception
     * doesn't carry one). `openTerminalSocket` is OK on the caller's
     * dispatcher because `httpClient.newWebSocket(...)` returns
     * immediately; the actual connect happens on OkHttp's internal
     * dispatcher pool.
     */
    fun connect(listener: Listener) {
        if (userClosed.get()) {
            logw(TAG, "connect() ignored — instance was permanently closed")
            return
        }
        this.listener = listener
        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    probeVersion()
                    preflightAuth()
                }
                openTerminalSocket()
            } catch (t: Throwable) {
                loge(TAG, "connect() failed: ${t.message}", t)
                safeOnError(t)
                if (!userClosed.get()) scheduleReconnect()
            }
        }
    }

    /** Send raw bytes (keystrokes) on the terminal WS. */
    fun sendBytes(bytes: ByteArray) {
        val ws = wsTerminal
        if (ws == null) {
            logw(TAG, "sendBytes dropped — terminal WS not open (${bytes.size} bytes)")
            return
        }
        try {
            ws.send(bytes.toByteString(0, bytes.size))
        } catch (t: Throwable) {
            loge(TAG, "sendBytes failed", t)
            safeOnError(t)
        }
    }

    /** Send a TerminalResize control message; replayed on reconnect. */
    fun sendResize(cols: Int, rows: Int) {
        lastCols = cols
        lastRows = rows
        val ws = wsControl
        if (ws == null) {
            // Will be replayed when control WS opens.
            logd(TAG, "sendResize buffered (${cols}x${rows}) — control WS not open")
            return
        }
        val envelope = buildResizeEnvelope(webClientId, rows, cols)
        try {
            ws.send(envelope)
        } catch (t: Throwable) {
            loge(TAG, "sendResize failed", t)
            safeOnError(t)
        }
    }

    /** Permanent close — disables reconnect and closes both sockets cleanly. */
    fun close() {
        if (!userClosed.compareAndSet(false, true)) return
        logd(TAG, "close() — user-initiated")
        try { reconnectJob.getAndSet(null)?.cancel() } catch (_: Throwable) {}
        try { wsTerminal?.close(NORMAL_CLOSURE, "client closing") } catch (_: Throwable) {}
        try { wsControl?.close(NORMAL_CLOSURE, "client closing") } catch (_: Throwable) {}
        wsTerminal = null
        wsControl = null
    }

    // --- Internals: HTTP pre-flight --------------------------------------

    /**
     * `GET {origin}{APP_PROXY_PREFIX}/info/version` — defensive probe.
     * Logs WARN on mismatch or any error; NEVER fails the connect.
     */
    private fun probeVersion() {
        val url = "${httpOrigin()}$APP_PROXY_PREFIX/info/version"
        val req = Request.Builder().url(url).get().build()
        try {
            httpClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    logw(TAG, "Version probe HTTP ${resp.code} — older zellij? Continuing.")
                    return
                }
                val body = resp.body?.string()?.trim().orEmpty()
                if (body != EXPECTED_ZELLIJ_VERSION) {
                    logw(TAG, "Zellij version mismatch: got '$body', expected '$EXPECTED_ZELLIJ_VERSION'. Continuing — protocol MAY still be compatible.")
                } else {
                    logd(TAG, "Zellij version OK: $body")
                }
            }
        } catch (t: Throwable) {
            logw(TAG, "Version probe failed (${t.message}) — continuing")
        }
    }

    /**
     * `POST {origin}{APP_PROXY_PREFIX}/command/login` with `{auth_token,…}`
     * — sets the `session_token` cookie that [cookieJar] then attaches to
     * the WS upgrade request. Without this the upgrade returns 401
     * (visible as close code 1011).
     */
    private fun preflightAuth() {
        val url = "${httpOrigin()}$APP_PROXY_PREFIX/command/login"
        val payload = JSONObject().apply {
            put("auth_token", sessionToken)
            put("remember_me", false)
        }.toString()
        val req = Request.Builder()
            .url(url)
            .post(payload.toRequestBody(JSON_MEDIA))
            .build()
        httpClient.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw IOException("Auth pre-flight failed: HTTP ${resp.code} ${resp.message}")
            }
            logd(TAG, "Auth pre-flight OK (cookies=${cookieJar.size()})")
        }
    }

    // --- Internals: WebSocket lifecycle ----------------------------------

    private fun openTerminalSocket() {
        val url = terminalUrl()
        if (BuildConfig.DEBUG) {
            logd(TAG, "Opening terminal WS: $url")
        }
        val req = Request.Builder().url(url).build()
        val newWs = httpClient.newWebSocket(req, terminalListener)
        // Close-vs-open race: if close() landed between newWebSocket() and the
        // assignment below, the new socket would be orphaned. Detect and
        // clean up.
        if (userClosed.get()) {
            try { newWs.close(NORMAL_CLOSURE, "client closing") } catch (_: Throwable) {}
            return
        }
        wsTerminal = newWs
    }

    private fun openControlSocket() {
        if (wsControl != null) return
        val url = controlUrl()
        if (BuildConfig.DEBUG) {
            logd(TAG, "Opening control WS: $url")
        }
        val req = Request.Builder().url(url).build()
        val newWs = httpClient.newWebSocket(req, controlListener)
        if (userClosed.get()) {
            try { newWs.close(NORMAL_CLOSURE, "client closing") } catch (_: Throwable) {}
            return
        }
        wsControl = newWs
    }

    private val terminalListener: WebSocketListener = object : WebSocketListener() {
        override fun onOpen(ws: WebSocket, response: Response) {
            logd(TAG, "Terminal WS onOpen (HTTP ${response.code})")
            lastReconnectIdx = 0 // reset on successful (re)connect
            safeOnConnected()
        }

        override fun onMessage(ws: WebSocket, text: String) {
            // Both text and binary frames are PTY bytes; text is used for
            // ANSI sequences like title-changes. Feed straight through.
            if (wsControl == null) openControlSocket()
            val bytes = text.toByteArray(Charsets.UTF_8)
            safeOnBytes(bytes, bytes.size)
        }

        override fun onMessage(ws: WebSocket, bytes: ByteString) {
            if (wsControl == null) openControlSocket()
            val arr = bytes.toByteArray()
            safeOnBytes(arr, arr.size)
        }

        override fun onClosing(ws: WebSocket, code: Int, reason: String) {
            logd(TAG, "Terminal WS onClosing: code=$code reason=$reason")
            try { ws.close(NORMAL_CLOSURE, null) } catch (_: Throwable) {}
        }

        override fun onClosed(ws: WebSocket, code: Int, reason: String) {
            logd(TAG, "Terminal WS onClosed: code=$code reason=$reason")
            wsTerminal = null
            try { wsControl?.close(NORMAL_CLOSURE, "terminal closed") } catch (_: Throwable) {}
            wsControl = null
            handleSocketEnded(code, reason)
        }

        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
            loge(TAG, "Terminal WS onFailure: ${t.message} (HTTP ${response?.code})", t)
            wsTerminal = null
            try { wsControl?.close(NORMAL_CLOSURE, "terminal failed") } catch (_: Throwable) {}
            wsControl = null
            // Don't call safeOnError here — safeOnDisconnected below already
            // conveys the failure (with reason text). Two callbacks per event
            // makes listener wiring brittle.
            handleSocketEnded(ABNORMAL_CLOSURE, t.message ?: "failure")
        }
    }

    private val controlListener: WebSocketListener = object : WebSocketListener() {
        override fun onOpen(ws: WebSocket, response: Response) {
            logd(TAG, "Control WS onOpen — sending initial TerminalResize ${lastCols}x${lastRows}")
            sendResize(lastCols, lastRows)
        }

        override fun onMessage(ws: WebSocket, text: String) {
            handleControlMessage(text)
        }

        override fun onClosing(ws: WebSocket, code: Int, reason: String) {
            logd(TAG, "Control WS onClosing: code=$code reason=$reason")
            try { ws.close(NORMAL_CLOSURE, null) } catch (_: Throwable) {}
        }

        override fun onClosed(ws: WebSocket, code: Int, reason: String) {
            logd(TAG, "Control WS onClosed: code=$code reason=$reason")
            wsControl = null
            // Don't fan-out close here — terminal WS close drives reconnect.
        }

        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
            logw(TAG, "Control WS onFailure: ${t.message} — control will reopen with terminal", t)
            wsControl = null
            // Intentionally don't fan-out an onError here; control-WS failures
            // ride along with the terminal-WS lifecycle and would be reported
            // as a paired onDisconnected on that channel.
        }
    }

    /** Parse a JSON payload from the control WS and dispatch. */
    private fun handleControlMessage(text: String) {
        val obj = try {
            JSONObject(text)
        } catch (t: Throwable) {
            logw(TAG, "Bad control frame: ${t.message}")
            return
        }
        val type = obj.optString("type", "")
        if (type.isEmpty()) return
        when (type) {
            "SwitchedSession" -> {
                val newName = obj.optString("new_session_name", "")
                if (newName.isNotEmpty()) {
                    logd(TAG, "SwitchedSession → '$newName'")
                    currentSessionName = newName
                    safeOnSwitchedSession(newName)
                }
            }
            "QueryTerminalSize" -> {
                // Reply with the most recent dimensions.
                sendResize(lastCols, lastRows)
            }
            "SetConfig", "Log", "LogError" -> {
                // Theme/font/log frames — not actioned in T18. UI wiring in T19+.
                logd(TAG, "Control frame: $type")
            }
            else -> {
                logd(TAG, "Ignoring unknown control frame type=$type")
            }
        }
    }

    /** Routed close handler — decides reconnect vs final disconnect. */
    private fun handleSocketEnded(code: Int, reason: String) {
        when {
            userClosed.get() -> {
                // Closed by us; nothing to do.
                safeOnDisconnected(code, reason, willReconnect = false)
            }
            code == HOST_DISCONNECT_CODE -> {
                // 4001 — intentional disconnect by host. No reconnect.
                logd(TAG, "Host disconnect (4001) — not reconnecting")
                safeOnDisconnected(code, reason, willReconnect = false)
            }
            code == NORMAL_CLOSURE -> {
                // Clean close, but not user-initiated → still surface; no reconnect.
                safeOnDisconnected(code, reason, willReconnect = false)
            }
            else -> {
                safeOnDisconnected(code, reason, willReconnect = true)
                scheduleReconnect()
            }
        }
    }

    private fun scheduleReconnect() {
        if (userClosed.get()) return
        if (!reconnecting.compareAndSet(false, true)) return
        val job = coroutineScope.launch {
            try {
                for ((idx, sec) in BACKOFF_SCHEDULE_SECONDS.withIndex()) {
                    lastReconnectIdx = idx
                    logd(TAG, "Reconnect attempt ${idx + 1}/${BACKOFF_SCHEDULE_SECONDS.size} in ${sec}s")
                    delay(sec * 1000L)
                    if (userClosed.get()) return@launch
                    try {
                        // preflightAuth uses blocking httpClient.execute() —
                        // must run on Dispatchers.IO to avoid StrictMode's
                        // NetworkOnMainThreadException (same fix as connect()).
                        withContext(Dispatchers.IO) {
                            preflightAuth()
                        }
                        openTerminalSocket()
                        return@launch // success — terminal onOpen will reset lastReconnectIdx
                    } catch (t: Throwable) {
                        logw(TAG, "Reconnect attempt ${idx + 1} failed: ${t.message}")
                        if (idx == BACKOFF_SCHEDULE_SECONDS.lastIndex) {
                            logw(TAG, "Reconnect schedule exhausted — giving up")
                            safeOnError(t)
                        }
                        // else: fall through to next iteration
                    }
                }
            } finally {
                reconnecting.set(false)
            }
        }
        reconnectJob.set(job)
    }

    // --- URL helpers (companion-object pure functions for testability) ---

    private fun httpOrigin(): String = normalizeToHttpOrigin(origin)

    private fun wsOrigin(): String {
        val http = httpOrigin()
        return when {
            http.startsWith("https://", ignoreCase = true) -> "wss://" + http.substring(8)
            http.startsWith("http://", ignoreCase = true) -> "ws://" + http.substring(7)
            else -> http
        }
    }

    /**
     * Build the terminal WS URL. Session name MUST be in the path segment,
     * NOT the query string. See Phase 3 T11c "keystone" fix.
     */
    internal fun terminalUrl(): String =
        buildTerminalUrl(httpOrigin(), currentSessionName, webClientId)

    internal fun controlUrl(): String = buildControlUrl(httpOrigin())

    // --- Listener fan-out (try/catch so listener bugs don't kill us) ----

    private fun safeOnConnected() {
        try { listener?.onConnected() } catch (t: Throwable) {
            loge(TAG, "listener.onConnected threw", t)
        }
    }
    private fun safeOnBytes(bytes: ByteArray, length: Int) {
        try { listener?.onBytes(bytes, length) } catch (t: Throwable) {
            loge(TAG, "listener.onBytes threw", t)
        }
    }
    private fun safeOnSwitchedSession(newName: String) {
        try { listener?.onSwitchedSession(newName) } catch (t: Throwable) {
            loge(TAG, "listener.onSwitchedSession threw", t)
        }
    }
    private fun safeOnDisconnected(code: Int, reason: String, willReconnect: Boolean) {
        try { listener?.onDisconnected(code, reason, willReconnect) } catch (t: Throwable) {
            loge(TAG, "listener.onDisconnected threw", t)
        }
    }
    private fun safeOnError(throwable: Throwable) {
        try { listener?.onError(throwable) } catch (t: Throwable) {
            loge(TAG, "listener.onError threw", t)
        }
    }

    // --- Test hooks (internal so unit tests in the same module can poke) -

    @VisibleForTesting internal fun isReconnectScheduled(): Boolean = reconnecting.get()
    @VisibleForTesting internal fun currentBackoffIndex(): Int = lastReconnectIdx
    @VisibleForTesting internal fun currentSessionNameForTest(): String = currentSessionName
    @VisibleForTesting internal fun reconnectJobForTest(): Job? = reconnectJob.get()
    @VisibleForTesting internal fun setListenerForTest(l: Listener) { listener = l }
    @VisibleForTesting internal fun invokeOnDisconnectedForTest(
        code: Int, reason: String, willReconnect: Boolean
    ) = safeOnDisconnected(code, reason, willReconnect)
    @VisibleForTesting internal fun invokeOnErrorForTest(t: Throwable) = safeOnError(t)
    @VisibleForTesting internal fun scheduleReconnectForTest() = scheduleReconnect()

    // --- Cookie jar ------------------------------------------------------

    /**
     * Minimal in-memory cookie jar keyed by host. zellij-web sets a single
     * `session_token` cookie on `/command/login`; on the WS upgrade the
     * cookie is automatically attached because the host matches.
     */
    private class SimpleCookieJar : CookieJar {
        private val store: ConcurrentHashMap<String, MutableList<Cookie>> = ConcurrentHashMap()

        override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
            if (cookies.isEmpty()) return
            val bucket = store.getOrPut(url.host) { mutableListOf() }
            synchronized(bucket) {
                // Replace cookies with the same name; drop expired.
                val now = System.currentTimeMillis()
                bucket.removeAll { existing ->
                    cookies.any { it.name == existing.name } || existing.expiresAt < now
                }
                bucket.addAll(cookies.filter { it.expiresAt > now })
            }
        }

        override fun loadForRequest(url: HttpUrl): List<Cookie> {
            val bucket = store[url.host] ?: return emptyList()
            synchronized(bucket) {
                val now = System.currentTimeMillis()
                bucket.removeAll { it.expiresAt < now }
                return bucket.filter { it.matches(url) }.toList()
            }
        }

        fun size(): Int = store.values.sumOf { it.size }
    }

    companion object {
        private const val TAG = "ZellijWS"

        // Expected zellij version (T17 spike target). Mismatch → WARN, not fail.
        private const val EXPECTED_ZELLIJ_VERSION = "0.44.3"

        // Close codes.
        const val NORMAL_CLOSURE: Int = 1000
        const val ABNORMAL_CLOSURE: Int = 1006
        const val HOST_DISCONNECT_CODE: Int = 4001

        /** Reconnect backoff schedule in seconds. After exhausting → give up. */
        val BACKOFF_SCHEDULE_SECONDS: List<Int> = listOf(1, 2, 4, 8, 16)

        /**
         * Orchestrator reverse-proxy path prefix for zellij-web. All four
         * URL builders (version probe, auth pre-flight, terminal WS, control
         * WS) MUST go through this prefix because zellij-web binds
         * 127.0.0.1:9097 (localhost-only); Android over Tailscale can ONLY
         * reach the orchestrator's funnel-exposed 9091. Same architectural
         * decision as plan AC2 (desktop iframe uses same-origin
         * /app-proxy/9097/...). T23 device QA on Z Fold 6 surfaced this
         * when direct-to-9097 URLs produced "error unknown" because the
         * WS upgrade never reached the orchestrator's reverse-proxy.
         *
         * The "9097" must match `_ZELLIJ_WEB_PORT` in
         * `Orchestrator/cli_agent/zellij_client.py`. Coupling point.
         */
        internal const val APP_PROXY_PREFIX = "/app-proxy/9097"

        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        // --- Log shims --------------------------------------------------------
        // android.util.Log throws RuntimeException under plain JVM unit tests
        // (the framework stubs are not implemented). These shims swallow that
        // specific failure so we can revert `testOptions.returnDefaultValues =
        // true` (which would silently mask Log/JSON bugs project-wide). In
        // production the calls flow through normally; the catch is a no-op.
        @JvmStatic internal fun logd(tag: String, msg: String) {
            try { Log.d(tag, msg) } catch (_: Throwable) { /* unit-test stub */ }
        }
        @JvmStatic internal fun logw(tag: String, msg: String, t: Throwable? = null) {
            try { if (t != null) Log.w(tag, msg, t) else Log.w(tag, msg) } catch (_: Throwable) {}
        }
        @JvmStatic internal fun loge(tag: String, msg: String, t: Throwable? = null) {
            try { if (t != null) Log.e(tag, msg, t) else Log.e(tag, msg) } catch (_: Throwable) {}
        }

        // --- Pure-Kotlin helpers (no android.* dependencies, unit-testable) -

        /**
         * Normalize an origin to http(s):// form, stripping any trailing slash.
         * Accepts ws://, wss://, http://, https://, or a bare host string.
         */
        internal fun normalizeToHttpOrigin(origin: String): String = when {
            origin.startsWith("ws://", ignoreCase = true) -> "http://" + origin.substring(5)
            origin.startsWith("wss://", ignoreCase = true) -> "https://" + origin.substring(6)
            else -> origin
        }.trimEnd('/')

        /**
         * Build the terminal WS URL with the session name in the path segment
         * (NOT the query string — Phase 3 T11c keystone invariant). Returns a
         * ws:// or wss:// URL string.
         *
         * @param origin any of http://, https://, ws://, wss:// or bare host
         * @param sessionName zellij session name (will be percent-encoded as a path segment)
         * @param webClientId per-client UUID identifier (sent as query param)
         */
        @JvmStatic
        internal fun buildTerminalUrl(
            origin: String,
            sessionName: String,
            webClientId: String,
        ): String {
            val httpOrigin = normalizeToHttpOrigin(origin)
            val base: HttpUrl = httpOrigin.toHttpUrl()
            val built = HttpUrl.Builder()
                .scheme(base.scheme) // http/https; OkHttp swaps to ws/wss for newWebSocket
                .host(base.host)
                .port(base.port)
                // App-proxy prefix — see APP_PROXY_PREFIX kdoc above. The
                // hardcoded "9097" matches _ZELLIJ_WEB_PORT on the backend.
                // T23 device QA surfaced this: previously the URL went
                // directly to /ws/terminal/{name} on the orchestrator port,
                // which has no such route; the orchestrator's app-proxy
                // forwards /app-proxy/9097/* to localhost:9097.
                .addPathSegment("app-proxy")
                .addPathSegment("9097")
                .addPathSegment("ws")
                .addPathSegment("terminal")
                .addPathSegment(sessionName)
                .addQueryParameter("web_client_id", webClientId)
                .build()
            // Force ws/wss scheme in the final string so logs are clear; OkHttp
            // accepts either for newWebSocket but ws:// reads better.
            val s = built.toString()
            return when {
                s.startsWith("https://") -> "wss://" + s.substring(8)
                s.startsWith("http://") -> "ws://" + s.substring(7)
                else -> s
            }
        }

        /**
         * Build the control WS URL — no session segment, single fixed path.
         * Returns a ws:// or wss:// URL string.
         */
        @JvmStatic
        internal fun buildControlUrl(origin: String): String {
            val httpOrigin = normalizeToHttpOrigin(origin)
            val ws = when {
                httpOrigin.startsWith("https://", ignoreCase = true) -> "wss://" + httpOrigin.substring(8)
                httpOrigin.startsWith("http://", ignoreCase = true) -> "ws://" + httpOrigin.substring(7)
                else -> httpOrigin
            }
            // App-proxy prefix — same architectural decision as buildTerminalUrl.
            // T23 device QA fix.
            return "$ws$APP_PROXY_PREFIX/ws/control"
        }

        /**
         * Build the JSON envelope for a TerminalResize control message.
         * Shape is fixed and small: built by string template so we don't
         * pull org.json into pure-Kotlin testable code.
         *
         * Defensively escapes `"` and `\` in the web_client_id (a UUID won't
         * contain either, but a future caller might pass a custom id).
         */
        @JvmStatic
        internal fun buildResizeEnvelope(
            webClientId: String,
            rows: Int,
            cols: Int,
        ): String {
            val escapedId = escapeJsonString(webClientId)
            return """{"web_client_id":"$escapedId","payload":{"type":"TerminalResize","rows":$rows,"cols":$cols}}"""
        }

        /** Minimal JSON-string escaper — handles `"` and `\` (sufficient for UUIDs and our envelope fields). */
        private fun escapeJsonString(s: String): String {
            if (s.indexOf('"') < 0 && s.indexOf('\\') < 0) return s
            val sb = StringBuilder(s.length + 8)
            for (c in s) {
                when (c) {
                    '"' -> sb.append("\\\"")
                    '\\' -> sb.append("\\\\")
                    else -> sb.append(c)
                }
            }
            return sb.toString()
        }
    }
}
