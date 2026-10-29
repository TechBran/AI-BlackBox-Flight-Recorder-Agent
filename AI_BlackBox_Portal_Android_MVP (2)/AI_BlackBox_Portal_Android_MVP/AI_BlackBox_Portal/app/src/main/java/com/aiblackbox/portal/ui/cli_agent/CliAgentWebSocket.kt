package com.aiblackbox.portal.ui.cli_agent

import android.util.Log
import com.aiblackbox.portal.data.api.HttpClients
import com.aiblackbox.portal.data.model.CliAgentErrorFrame
import com.aiblackbox.portal.data.model.CliAgentKillFrame
import com.aiblackbox.portal.data.model.CliAgentPasteFrame
import com.aiblackbox.portal.data.model.CliAgentResizeFrame
import com.aiblackbox.portal.data.model.CliAgentSessionInfoFrame
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Binary + text WebSocket client for `/cli-agent/ws/{session_id}`.
 *
 * Frame conventions (locked in design doc):
 *   - Binary frames = raw PTY bytes (both directions)
 *   - Text frames   = JSON control messages
 *
 * Outbound text frames:
 *   {"type":"resize","cols":N,"rows":N}
 *   {"type":"paste","text":"..."}
 *   {"type":"kill"}
 *
 * Inbound text frames:
 *   {"type":"session_info","state":"created"|"attaching"}
 *   {"type":"error","code":"...","message":"..."}
 *
 * Reconnect: exponential backoff 250ms → 500ms → 1000ms → 2000ms (capped).
 * Resets to 250ms on a successful (re)connect. User-initiated `close()` disables reconnect.
 */
class CliAgentWebSocket(
    private val baseUrl: String,
    private val sessionId: String,
    private val params: Map<String, String>,
    private val callbacks: Callbacks,
) {

    interface Callbacks {
        fun onOpen(state: String)
        fun onBytes(bytes: ByteArray)
        fun onError(code: String, message: String)
        fun onClosed(code: Int, reason: String)
        fun onReconnecting(attemptDelayMs: Long)
    }

    // OkHttp client — match WebSocketClient.kt's proven config.
    private val client: OkHttpClient = HttpClients.base.newBuilder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // Critical: no read timeout for long-lived WS
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    private val json: Json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
        isLenient = true
    }

    private val scope: CoroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    @Volatile private var webSocket: WebSocket? = null

    // Set when the operator explicitly calls close(); suppresses reconnect.
    private val userClosed: AtomicBoolean = AtomicBoolean(false)

    // Guards against double-launching reconnect coroutines.
    private val reconnecting: AtomicBoolean = AtomicBoolean(false)

    @Volatile private var reconnectJob: Job? = null

    @Volatile private var backoffMs: Long = INITIAL_BACKOFF_MS

    /** Idempotent. Starts (or reconnects) the WebSocket. */
    fun connect() {
        if (userClosed.get()) {
            Log.w(TAG, "connect() ignored — instance was permanently closed")
            return
        }
        if (webSocket != null) {
            Log.d(TAG, "connect() ignored — socket already open")
            return
        }

        val url = buildUrl()
        Log.d(TAG, "Connecting: $url")

        val request = Request.Builder().url(url).build()
        webSocket = client.newWebSocket(request, listener)
    }

    /** Send raw PTY bytes as a binary frame. Returns false if the socket isn't open. */
    fun sendBytes(bytes: ByteArray): Boolean {
        val ws = webSocket ?: return false
        return ws.send(bytes.toByteString(0, bytes.size))
    }

    /** Send a resize control frame. */
    fun sendResize(cols: Int, rows: Int) {
        val frame = CliAgentResizeFrame(cols = cols, rows = rows)
        sendText(json.encodeToString(frame))
    }

    /** Send a paste control frame; server wraps in bracketed-paste sequences. */
    fun sendPaste(text: String) {
        val frame = CliAgentPasteFrame(text = text)
        sendText(json.encodeToString(frame))
    }

    /** Send a kill control frame (alternative to REST DELETE). */
    fun sendKill() {
        val frame = CliAgentKillFrame()
        sendText(json.encodeToString(frame))
    }

    /** Permanent close — disables reconnect and tears down the coroutine scope. */
    fun close() {
        if (!userClosed.compareAndSet(false, true)) return
        Log.d(TAG, "close() — user-initiated, disabling reconnect")
        try {
            reconnectJob?.cancel()
        } catch (_: Throwable) {}
        reconnectJob = null
        try {
            webSocket?.close(NORMAL_CLOSURE, "Client closed")
        } catch (_: Throwable) {}
        webSocket = null
        try {
            scope.cancel()
        } catch (_: Throwable) {}
    }

    // -- Internals ---------------------------------------------------------

    private fun sendText(payload: String): Boolean {
        val ws = webSocket
        if (ws == null) {
            Log.w(TAG, "sendText dropped — socket not open: $payload")
            return false
        }
        return ws.send(payload)
    }

    private fun buildUrl(): HttpUrl {
        // OkHttp's HttpUrl uses http/https schemes internally; ws/wss are
        // converted by the client. Use the Kotlin extension toHttpUrl()
        // (the project uses OkHttp 5.x) and property accessors — the
        // older Java-style HttpUrl.parse() / .scheme() / .host() / .port()
        // API is deprecated in modern OkHttp Kotlin.
        val parsed = normalizeScheme(baseUrl).toHttpUrl()
        val builder = HttpUrl.Builder()
            .scheme(parsed.scheme)
            .host(parsed.host)
            .port(parsed.port)
            .addPathSegments("cli-agent/ws/$sessionId")
        params.forEach { (k, v) -> builder.addQueryParameter(k, v) }
        return builder.build()
    }

    private fun normalizeScheme(url: String): String = when {
        url.startsWith("wss://", ignoreCase = true) -> "https://" + url.substring(6)
        url.startsWith("ws://", ignoreCase = true) -> "http://" + url.substring(5)
        else -> url
    }

    private fun scheduleReconnect() {
        if (userClosed.get()) return
        if (!reconnecting.compareAndSet(false, true)) return

        val delayMs = backoffMs
        // Bump backoff for next attempt (capped).
        backoffMs = (backoffMs * 2).coerceAtMost(MAX_BACKOFF_MS)

        Log.d(TAG, "Reconnecting in ${delayMs}ms")
        callbacks.onReconnecting(delayMs)

        reconnectJob = scope.launch {
            try {
                delay(delayMs)
                if (!userClosed.get()) {
                    connect()
                }
            } finally {
                reconnecting.set(false)
            }
        }
    }

    private fun handleTextFrame(text: String) {
        val type = try {
            val obj = json.parseToJsonElement(text) as? JsonObject ?: return
            obj["type"]?.jsonPrimitive?.content
        } catch (e: Throwable) {
            Log.w(TAG, "Failed to parse text frame: ${e.message}")
            null
        }

        when (type) {
            "session_info" -> {
                val info = try {
                    json.decodeFromString(CliAgentSessionInfoFrame.serializer(), text)
                } catch (e: Throwable) {
                    Log.w(TAG, "Bad session_info frame: ${e.message}")
                    null
                }
                if (info != null) {
                    // Connection is live — reset backoff so the next failure starts fresh.
                    backoffMs = INITIAL_BACKOFF_MS
                    callbacks.onOpen(info.state)
                }
            }

            "error" -> {
                val err = try {
                    json.decodeFromString(CliAgentErrorFrame.serializer(), text)
                } catch (e: Throwable) {
                    Log.w(TAG, "Bad error frame: ${e.message}")
                    null
                }
                if (err != null) {
                    callbacks.onError(err.code, err.message)
                }
            }

            else -> {
                // Unknown control frame — ignore for forward compatibility.
                Log.d(TAG, "Ignoring unknown text frame type=$type")
            }
        }
    }

    private val listener: WebSocketListener = object : WebSocketListener() {
        override fun onOpen(ws: WebSocket, response: Response) {
            Log.d(TAG, "WebSocket onOpen (HTTP ${response.code})")
            // We don't fire callbacks.onOpen here — we wait for the server's
            // session_info text frame, whose state field is what the UI needs.
        }

        override fun onMessage(ws: WebSocket, text: String) {
            handleTextFrame(text)
        }

        override fun onMessage(ws: WebSocket, bytes: ByteString) {
            callbacks.onBytes(bytes.toByteArray())
        }

        override fun onClosing(ws: WebSocket, code: Int, reason: String) {
            Log.d(TAG, "WebSocket onClosing: code=$code reason=$reason")
            try {
                ws.close(NORMAL_CLOSURE, null)
            } catch (_: Throwable) {}
        }

        override fun onClosed(ws: WebSocket, code: Int, reason: String) {
            Log.d(TAG, "WebSocket onClosed: code=$code reason=$reason")
            webSocket = null
            callbacks.onClosed(code, reason)
            if (!userClosed.get()) {
                scheduleReconnect()
            }
        }

        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
            Log.e(TAG, "WebSocket onFailure: ${t.message} (code=${response?.code})", t)
            webSocket = null
            // Surface the failure as a closed event so UI can react uniformly.
            callbacks.onClosed(response?.code ?: ABNORMAL_CLOSURE, t.message ?: "failure")
            if (!userClosed.get()) {
                scheduleReconnect()
            }
        }
    }

    companion object {
        private const val TAG = "CliAgentWebSocket"
        private const val INITIAL_BACKOFF_MS: Long = 250L
        private const val MAX_BACKOFF_MS: Long = 2000L
        private const val NORMAL_CLOSURE: Int = 1000
        private const val ABNORMAL_CLOSURE: Int = 1006
    }
}
