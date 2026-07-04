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
import java.util.UUID
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
 *     → open terminal WS (orchestrator app-proxy injects master cookie)
 *     → on first inbound message → open control WS
 *     → on control WS onOpen → send initial TerminalResize
 *
 * ## Auth (Phase 5, 2026-05-26 — master-token model)
 *
 * The Android client no longer participates in zellij's auth handshake.
 * The orchestrator's `/app-proxy/9097/` reverse proxy (matching any path
 * under that prefix) injects a master `session_token` cookie on every
 * upstream forward, so this client opens the WebSocket with no cookie /
 * no `/command/login` / no `/session` POST.
 * The `web_client_id` query param is still required by zellij; we
 * generate a UUID client-side (zellij-web accepts any UUID). See
 * SNAP-20260526-6798 + docs/plans/2026-05-24-zellij-cli-agent-rewrite.md
 * Phase 4 RESULTS for why.
 *
 * Close code 4001 = "intentional disconnect by host" → DO NOT reconnect.
 * Any other non-1000 code → reconnect with backoff [1, 2, 4, 8, 16] s.
 */
class ZellijWebSocketClient(
    private val origin: String,
    private val sessionName: String,
    /**
     * Initial web_client_id (a fresh UUID by default). Phase 5
     * (2026-05-26): the orchestrator owns AUTH via a master cookie
     * injected on the upstream proxy, but zellij still REQUIRES a
     * server-assigned web_client_id (from POST /session) in the WS
     * upgrade query param — a client-generated UUID makes zellij
     * silently accept then disconnect. So this initial value is
     * overwritten by [fetchServerWebClientId] during [connect].
     */
    initialWebClientId: String = UUID.randomUUID().toString(),
    private val coroutineScope: CoroutineScope,
) {
    @Volatile private var webClientId: String = initialWebClientId

    interface Listener {
        fun onConnected()
        /** Matches `TerminalEmulator.append(byte[], int)` signature so T19 can pass-through. */
        fun onBytes(bytes: ByteArray, length: Int)
        fun onSwitchedSession(newSessionName: String)
        fun onDisconnected(code: Int, reason: String, willReconnect: Boolean)
        fun onError(throwable: Throwable)
    }

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // long-lived WS
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    @Volatile private var wsTerminal: WebSocket? = null
    @Volatile private var wsControl: WebSocket? = null

    /** Last grid dimensions sent — replayed on reconnect and on control-WS open. */
    @Volatile private var lastCols: Int = 80
    @Volatile private var lastRows: Int = 24

    /**
     * Last MEASURED grid dimensions, reported synchronously by the renderer's
     * onSizeChanged. May be AHEAD of [lastCols]/[lastRows] (last SENT) when a
     * rotation happened while the socket was down or while the heavyweight
     * resize was being debounced. Used to reconcile the reconnect /
     * QueryTerminalSize reply so it carries the CURRENT size, not a stale
     * last-sent value. 0 until the renderer first reports a size.
     */
    @Volatile private var measuredCols: Int = 0
    @Volatile private var measuredRows: Int = 0

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
     * Idempotent. Fires version probe → POST /session → opens terminal WS.
     *
     * Phase 5 (2026-05-26): /command/login is gone — the orchestrator's
     * reverse proxy injects a master session cookie on every upstream
     * forward. But /session POST is STILL required: zellij assigns a
     * fresh `web_client_id` per call, and the WS upgrade silently
     * accepts then disconnects if the query param isn't a value /session
     * minted. The orchestrator can't do /session on the client's behalf
     * because the returned id has to be threaded into THIS client's WS
     * URL (one id per WS connection).
     *
     * All HTTP calls use OkHttp's BLOCKING `execute()` so they MUST run
     * on `Dispatchers.IO` — the caller's `coroutineScope` is typically
     * `rememberCoroutineScope()` (AndroidUiDispatcher = main thread) and
     * would trip StrictMode's `NetworkOnMainThreadException`.
     * `openTerminalSocket` is safe on the caller's dispatcher because
     * `httpClient.newWebSocket(...)` returns immediately.
     */
    fun connect(listener: Listener) {
        if (userClosed.get()) {
            logw(TAG, "connect() ignored — instance was permanently closed")
            return
        }
        this.listener = listener
        logd(TAG, "Auth handled by orchestrator proxy (master-token model)")
        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    probeVersion()
                    fetchServerWebClientId()
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

    /**
     * Record the renderer's CURRENT measured grid. Called synchronously from
     * onSizeChanged — cheap, no wire I/O — so it can run on every intermediate
     * size while the heavyweight [sendResize] is debounced. The reconnect /
     * QueryTerminalSize reply reconciles against this (see [resolveReplaySize])
     * so it reflects the latest measurement even before the debounced resize
     * fires. Ignores non-positive dims.
     */
    fun updateMeasuredSize(cols: Int, rows: Int) {
        if (cols > 0 && rows > 0) {
            measuredCols = cols
            measuredRows = rows
        }
    }

    /**
     * The size a reconnect / QueryTerminalSize reply should carry: the CURRENT
     * measured grid when known, else the last-sent value. Pure logic lives in
     * [reconcileReplaySize] for unit testing.
     */
    private fun resolveReplaySize(): Pair<Int, Int> =
        reconcileReplaySize(measuredCols, measuredRows, lastCols, lastRows)

    /**
     * Force zellij to repaint the full screen. Used on REATTACH: a returning
     * renderer gets a brand-new (empty) TerminalView and the live socket only
     * streams NEW output, so the existing screen would stay blank until the
     * user produces output. zellij re-renders its whole frame on a
     * TerminalResize, so we toggle one row to guarantee a dimension change
     * even when the size is unchanged. No-op if closed or the control WS is
     * not open yet (a fresh attach repaints on its own).
     */
    fun requestRepaint() {
        if (userClosed.get()) return
        if (wsControl == null) return
        val c = lastCols
        val r = lastRows
        if (r > 1) {
            sendResize(c, r - 1)
            sendResize(c, r)
        } else {
            sendResize(c, r)
        }
    }

    /**
     * Detach the renderer WITHOUT closing the socket.
     *
     * Phase 1 (2026-06-22, session persistence): leaving the terminal
     * Composable used to call [close], which sets `userClosed` permanently
     * and defeats the reconnect machinery — orphaning a live server session.
     * [detach] instead just drops the [listener] (so no more bytes are
     * forwarded to a dead [com.termux.view.TerminalView]) while keeping
     * both WebSockets open AND keeping `userClosed` false so reconnect stays
     * usable. [TerminalSessionManager] keeps this instance alive across
     * navigation; the next mount calls [rebindListener] to resume rendering.
     *
     * Idempotent and non-destructive: calling [detach] on an instance that
     * was already [close]d is a harmless no-op (the socket is already gone).
     */
    fun detach() {
        logd(TAG, "detach() — renderer unbound, socket kept alive")
        listener = null
    }

    /**
     * Re-attach a (new) renderer to a live client WITHOUT reconnecting.
     *
     * Used by [TerminalSessionManager] when the user returns to a session
     * that already has a live socket: a fresh [com.termux.view.TerminalView]
     * is built each mount (see ZellijTerminalScreen), so we swap the
     * listener slot in place. The open WebSocket(s) keep flowing; no
     * `POST /session`, no new socket. If the socket had dropped while
     * detached, the reconnect machinery (keyed on `currentSessionName`)
     * will have re-opened it under the covers and this new listener picks
     * up from there.
     *
     * No-op if the instance was permanently [close]d.
     */
    fun rebindListener(listener: Listener) {
        if (userClosed.get()) {
            logw(TAG, "rebindListener() ignored — instance was permanently closed")
            return
        }
        logd(TAG, "rebindListener() — renderer re-bound to live client")
        this.listener = listener
        // If the socket is still open, surface a connected state to the new
        // renderer immediately so its banner clears without waiting for the
        // next inbound byte. If it's mid-reconnect, the terminal onOpen will
        // fire onConnected as usual.
        if (wsTerminal != null) safeOnConnected()
    }

    /**
     * Permanent close — disables reconnect and closes both sockets cleanly.
     *
     * Phase 1: this is now the ONLY teardown path that sets `userClosed`. It
     * must be reached ONLY by an explicit kill (the X button), NEVER by
     * navigation / dispose — use [detach] for that.
     */
    fun close() {
        if (!userClosed.compareAndSet(false, true)) return
        logd(TAG, "close() — user-initiated")
        listener = null
        try { reconnectJob.getAndSet(null)?.cancel() } catch (_: Throwable) {}
        try { wsTerminal?.close(NORMAL_CLOSURE, "client closing") } catch (_: Throwable) {}
        try { wsControl?.close(NORMAL_CLOSURE, "client closing") } catch (_: Throwable) {}
        wsTerminal = null
        wsControl = null
    }

    /** True once [close] has permanently torn this instance down. */
    fun isClosed(): Boolean = userClosed.get()

    /** True while a live terminal socket is open (not closed, not mid-reconnect-gap). */
    fun hasOpenSocket(): Boolean = wsTerminal != null && !userClosed.get()

    /** Last grid dimensions this client knows about — for [TerminalSessionManager] bookkeeping. */
    fun lastColsRows(): Pair<Int, Int> = lastCols to lastRows

    /** Effective session name (flips on a SwitchedSession control frame). */
    fun effectiveSessionName(): String = currentSessionName

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
     * `POST {origin}{APP_PROXY_PREFIX}/session` with `{}` — zellij assigns
     * a fresh `web_client_id` per call. The orchestrator's proxy injects
     * the master session cookie on this request, so it succeeds without
     * any client-side auth. The returned id is what the WS upgrade query
     * param MUST use; a client-generated UUID causes zellij to silently
     * accept then disconnect (T23 keystone bug).
     *
     * Overwrites [webClientId] in-place so the URL builders pick up the
     * new value on next `terminalUrl()`/`controlUrl()` call.
     */
    private fun fetchServerWebClientId() {
        val url = "${httpOrigin()}$APP_PROXY_PREFIX/session"
        val req = Request.Builder()
            .url(url)
            .post(byteArrayOf('{'.code.toByte(), '}'.code.toByte()).toRequestBody("application/json".toMediaType()))
            .build()
        httpClient.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw java.io.IOException("Session init failed: HTTP ${resp.code} ${resp.message}")
            }
            val body = resp.body?.string().orEmpty()
            val newId = try {
                JSONObject(body).optString("web_client_id").takeIf { it.isNotBlank() }
            } catch (t: Throwable) {
                logw(TAG, "Session response JSON parse failed: ${t.message}")
                null
            }
            if (newId == null) {
                throw java.io.IOException("Session response missing 'web_client_id': $body")
            }
            webClientId = newId
            logd(TAG, "Server-assigned web_client_id received")
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
            // Reconcile: a rotation may have happened while this socket was
            // down, so replay the CURRENT measured size, not a stale last-sent.
            val (c, r) = resolveReplaySize()
            logd(TAG, "Control WS onOpen — sending TerminalResize ${c}x${r} (reconciled)")
            sendResize(c, r)
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
                // Reply with the CURRENT measured size (reconciled), not a
                // possibly-stale last-sent value — a rotation may have happened
                // while the resize was debounced or the socket was down.
                val (c, r) = resolveReplaySize()
                sendResize(c, r)
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
                        // Phase 5 (2026-05-26): no auth pre-flight, but
                        // /session POST is still required to get a fresh
                        // web_client_id from zellij for this WS connection.
                        // Master cookie injected by orchestrator proxy.
                        withContext(Dispatchers.IO) {
                            fetchServerWebClientId()
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
    @VisibleForTesting internal fun resolveReplaySizeForTest(): Pair<Int, Int> = resolveReplaySize()

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

        /**
         * Guard for the debounced resize send: only push a grid that is
         * positive in both dims AND differs from the last size we sent. Every
         * send is a heavyweight zellij session reflow, so a burst that
         * collapses back to the already-sent size must send nothing. Pure so
         * the debounce path in ZellijTerminalScreen can be verified here.
         */
        @JvmStatic
        internal fun shouldSendResize(
            cols: Int,
            rows: Int,
            lastCols: Int,
            lastRows: Int,
        ): Boolean = cols > 0 && rows > 0 && (cols != lastCols || rows != lastRows)

        /**
         * Pick the size a reconnect / QueryTerminalSize reply should carry:
         * the CURRENT measured grid when both dims are positive, else the
         * last-sent value. Reconciles a rotation that landed while the socket
         * was down or the resize was debounced. Falls back as a PAIR (never
         * mixes a measured col with a stale row) so the reply is always a grid
         * that actually existed.
         */
        @JvmStatic
        internal fun reconcileReplaySize(
            measuredCols: Int,
            measuredRows: Int,
            lastCols: Int,
            lastRows: Int,
        ): Pair<Int, Int> =
            if (measuredCols > 0 && measuredRows > 0) measuredCols to measuredRows
            else lastCols to lastRows

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
