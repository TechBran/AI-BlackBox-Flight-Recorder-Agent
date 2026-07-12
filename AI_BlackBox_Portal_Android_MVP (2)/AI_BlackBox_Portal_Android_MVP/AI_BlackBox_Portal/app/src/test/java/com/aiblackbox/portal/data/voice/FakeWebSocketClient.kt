package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow
import okhttp3.OkHttpClient

/**
 * Test double for [WebSocketClient]. connect() returns a Channel-backed flow the
 * test pushes [WsMessage]s into; closing the channel ends the collect (= socket
 * gone, exactly like the real callbackFlow). send() records outbound frames and
 * returns a settable result. close() mirrors okhttp: Disconnected, then closed.
 */
class FakeWebSocketClient : WebSocketClient(OkHttpClient()) {
    val incoming = Channel<WsMessage>(Channel.UNLIMITED)
    val sent = mutableListOf<String>()
    var sendResult = true
    var closeCount = 0
    var lastUrl: String? = null

    override fun connect(url: String): Flow<WsMessage> {
        lastUrl = url
        return incoming.receiveAsFlow()
    }

    override fun send(text: String): Boolean {
        sent += text
        return sendResult
    }

    override fun close() {
        closeCount++
        incoming.trySend(WsMessage.Disconnected)
        incoming.close()
    }
}
