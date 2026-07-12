package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WsMessage
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for VoiceClient's server-message parsing and state machine,
 * driven through FakeWebSocketClient (no network, no Android framework —
 * android.util.Log is covered by unitTests.returnDefaultValues=true).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceClientParseTest {

    private lateinit var fake: FakeWebSocketClient
    private lateinit var voice: VoiceClient
    private val events = mutableListOf<VoiceEvent>()

    /** Connect through the fake socket; optionally complete the backend-ready handshake. */
    private fun TestScope.startConnected(confirm: Boolean = true) {
        fake = FakeWebSocketClient()
        voice = VoiceClient(OkHttpClient(), "ws://box.test", wsFactory = { fake })
        voice.events.onEach { events.add(it) }.launchIn(backgroundScope)
        voice.connect(VoiceBackend.GEMINI_LIVE, "op-test", "Orus", backgroundScope)
        runCurrent()
        fake.incoming.trySend(WsMessage.Connected)
        runCurrent()
        if (confirm) serverSends("""{"type":"connected"}""")
    }

    private fun TestScope.serverSends(json: String) {
        fake.incoming.trySend(WsMessage.Text(json))
        runCurrent()
    }

    @Test
    fun `transport open stays CONNECTING until server confirms backend ready`() = runTest {
        startConnected(confirm = false)
        assertEquals(VoiceState.CONNECTING, voice.state.value)
        // The connect handshake frame went out on transport open
        assertTrue(fake.sent.any { it.contains("\"type\":\"connect\"") && it.contains("op-test") })
        assertTrue(fake.lastUrl!!.contains("/ws/gemini-live/"))

        serverSends("""{"type":"connected"}""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
    }

    @Test
    fun `status frame emits Status event without changing state`() = runTest {
        startConnected()
        serverSends("""{"type":"status","message":"Connecting to Gemini Live..."}""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertEquals(
            "Connecting to Gemini Live...",
            events.filterIsInstance<VoiceEvent.Status>().single().message
        )
    }
}
