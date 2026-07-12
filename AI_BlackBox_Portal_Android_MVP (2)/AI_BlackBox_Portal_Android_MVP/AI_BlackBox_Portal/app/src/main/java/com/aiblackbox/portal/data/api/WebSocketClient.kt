package com.aiblackbox.portal.data.api

import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.buffer
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.emitAll
import kotlinx.coroutines.flow.flow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit

sealed class WsMessage {
    data class Text(val text: String) : WsMessage()
    data class Error(val error: Throwable) : WsMessage()
    data class Closing(val code: Int, val reason: String) : WsMessage()
    data object Connected : WsMessage()
    data object Disconnected : WsMessage()
}

open class WebSocketClient(baseClient: OkHttpClient) {

    // WS-specific client matching OverlayService's proven config
    private val client: OkHttpClient = baseClient.newBuilder()
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .connectTimeout(30, TimeUnit.SECONDS)
        .pingInterval(30, TimeUnit.SECONDS)
        .build()

    private var webSocket: WebSocket? = null

    open fun connect(url: String): Flow<WsMessage> = flow {
        android.util.Log.d("WebSocket", "Connecting to: $url")
        emitAll(connectInternal(url))
    }.catch { e ->
        android.util.Log.e("WebSocket", "Flow exception: ${e.message}", e)
        emit(WsMessage.Error(e))
        emit(WsMessage.Disconnected)
    }
    // Large buffer for rapid audio_delta messages (~50 per second)
    .buffer(capacity = 1024, onBufferOverflow = BufferOverflow.SUSPEND)

    private fun connectInternal(url: String): Flow<WsMessage> = callbackFlow {
        val request = Request.Builder().url(url).build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                android.util.Log.d("WebSocket", "Connected")
                trySend(WsMessage.Connected)
            }

            override fun onMessage(ws: WebSocket, text: String) {
                // Use trySend — if buffer full, DROP_OLDEST handles it upstream
                trySend(WsMessage.Text(text))
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                android.util.Log.d("WebSocket", "onClosing: code=$code, reason=$reason")
                trySend(WsMessage.Closing(code, reason))
                // Respond to server's close frame
                ws.close(code, reason)
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                android.util.Log.e("WebSocket", "onFailure: ${t.message}, code=${response?.code}")
                trySend(WsMessage.Error(t))
                trySend(WsMessage.Disconnected)
                channel.close()
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                android.util.Log.d("WebSocket", "onClosed: code=$code, reason=$reason")
                trySend(WsMessage.Disconnected)
                channel.close()
            }
        })

        awaitClose {
            android.util.Log.d("WebSocket", "Flow cancelled, closing socket")
            try {
                webSocket?.close(1000, "Client closed")
            } catch (_: Exception) {}
            webSocket = null
        }
    }

    open fun send(text: String): Boolean = webSocket?.send(text) ?: false

    open fun close() {
        try {
            webSocket?.close(1000, "Client closed")
        } catch (_: Exception) {}
        webSocket = null
    }
}
