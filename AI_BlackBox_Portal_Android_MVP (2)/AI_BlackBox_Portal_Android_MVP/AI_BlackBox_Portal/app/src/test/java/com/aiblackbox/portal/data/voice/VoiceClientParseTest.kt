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

    @Test
    fun `server reconnecting drives RECONNECTING and reconnected restores CONNECTED`() = runTest {
        startConnected()
        serverSends("""{"type":"audio_delta","data":"AAAA"}""")
        assertEquals(VoiceState.SPEAKING, voice.state.value)

        serverSends("""{"type":"reconnecting","message":"Gemini connection lost - reconnecting"}""")
        assertEquals(VoiceState.RECONNECTING, voice.state.value)
        assertFalse(voice.isAISpeaking.value)
        assertEquals(
            "Gemini connection lost - reconnecting",
            events.filterIsInstance<VoiceEvent.Reconnecting>().single().message
        )

        serverSends("""{"type":"reconnected"}""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertTrue(events.last() is VoiceEvent.Reconnected)
    }

    @Test
    fun `terminal disconnected flips to ERROR closes socket and surfaces reason`() = runTest {
        startConnected()
        serverSends("""{"type":"disconnected","data":"Connection lost after multiple reconnection attempts"}""")
        assertEquals(VoiceState.ERROR, voice.state.value)
        assertEquals(
            "Connection lost after multiple reconnection attempts",
            events.filterIsInstance<VoiceEvent.ServerDisconnected>().single().reason
        )
        // Error also emitted so existing VoiceScreen error surfacing fires unchanged
        assertTrue(events.any { it is VoiceEvent.Error })
        assertEquals(1, fake.closeCount)
    }

    @Test
    fun `unknown message types are inert - no state change no events no crash`() = runTest {
        startConnected()
        val eventsBefore = events.size
        // NOT in this list: tool_call / tool_result / image_task / video_task /
        // music_task — those five get real parsing (VoiceEvent.Tool) in P3.9a.
        serverSends("""{"type":"some_future_frame","data":"x"}""")
        serverSends("""{"type":"session_stats","data":{"turns":3}}""")
        serverSends("""not even json""")
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertEquals(eventsBefore, events.size)
    }

    @Test
    fun `CONNECTING times out to ERROR when backend never becomes ready`() = runTest {
        startConnected(confirm = false)
        assertEquals(VoiceState.CONNECTING, voice.state.value)
        advanceTimeBy(VoiceClient.CONNECT_TIMEOUT_MS + 1)
        runCurrent()
        assertEquals(VoiceState.ERROR, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Error && it.message.contains("ready") })
        assertTrue(fake.closeCount >= 1)
    }

    @Test
    fun `connect timeout does not fire once backend confirmed`() = runTest {
        startConnected()
        advanceTimeBy(VoiceClient.CONNECT_TIMEOUT_MS + 1)
        runCurrent()
        assertEquals(VoiceState.CONNECTED, voice.state.value)
    }

    @Test
    fun `audio send failure on a live session returns false and drops the socket`() = runTest {
        startConnected()
        fake.sendResult = false
        val ok = voice.sendAudioChunk("QUJD")
        runCurrent()
        assertFalse(ok)
        assertEquals(1, fake.closeCount)
    }

    @Test
    fun `audio send success returns true and keeps the socket`() = runTest {
        startConnected()
        assertTrue(voice.sendAudioChunk("QUJD"))
        runCurrent()
        assertEquals(0, fake.closeCount)
        assertTrue(fake.sent.any { it.contains("\"type\":\"audio_input\"") })
    }
}
