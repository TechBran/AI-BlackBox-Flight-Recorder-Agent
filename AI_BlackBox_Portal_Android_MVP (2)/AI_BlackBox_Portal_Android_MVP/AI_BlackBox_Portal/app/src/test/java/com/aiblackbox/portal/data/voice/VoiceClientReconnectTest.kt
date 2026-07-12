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
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Reconnect-with-resume state machine (pattern ported from SttStreamClient.kt:166-235). */
@OptIn(ExperimentalCoroutinesApi::class)
class VoiceClientReconnectTest {

    private val fakes = mutableListOf<FakeWebSocketClient>()
    private val events = mutableListOf<VoiceEvent>()
    private lateinit var voice: VoiceClient

    private fun TestScope.startConfirmed() {
        voice = VoiceClient(
            OkHttpClient(), "ws://box.test",
            wsFactory = { FakeWebSocketClient().also { f -> fakes.add(f) } },
        )
        voice.events.onEach { events.add(it) }.launchIn(backgroundScope)
        voice.connect(VoiceBackend.GEMINI_LIVE, "op-test", "Orus", backgroundScope)
        runCurrent()
        confirmLeg(0)
    }

    private fun TestScope.confirmLeg(i: Int) {
        fakes[i].incoming.trySend(WsMessage.Connected); runCurrent()
        fakes[i].incoming.trySend(WsMessage.Text("""{"type":"connected"}""")); runCurrent()
    }

    /** Server-side drop: transport Disconnected, then the flow ends. */
    private fun TestScope.dropLeg(i: Int) {
        fakes[i].incoming.trySend(WsMessage.Disconnected)
        fakes[i].incoming.close()
        runCurrent()
    }

    @Test
    fun `dropped leg reconnects with backoff and resumes on server confirm`() = runTest {
        startConfirmed()
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertEquals(1, fakes.size)

        dropLeg(0)
        assertEquals(VoiceState.RECONNECTING, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Reconnecting })

        advanceTimeBy(VoiceClient.RECONNECT_BASE_DELAY_MS + 1); runCurrent()
        assertEquals(2, fakes.size)  // fresh leg socket opened
        confirmLeg(1)
        assertEquals(VoiceState.CONNECTED, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Reconnected })
        // Fresh session id per leg — server builds a clean session
        assertNotEquals(fakes[0].lastUrl, fakes[1].lastUrl)
    }

    @Test
    fun `reconnect attempts are bounded - exhaustion ends in ERROR`() = runTest {
        startConfirmed()
        dropLeg(0)
        for (attempt in 1..VoiceClient.MAX_RECONNECTS) {
            assertEquals(VoiceState.RECONNECTING, voice.state.value)
            advanceTimeBy(VoiceClient.RECONNECT_BASE_DELAY_MS * attempt + 1); runCurrent()
            // fresh leg opened — kill it before it confirms
            fakes.last().incoming.close(); runCurrent()
        }
        assertEquals(VoiceState.ERROR, voice.state.value)
        assertTrue(events.any { it is VoiceEvent.Error && it.message.contains("reconnect") })
    }

    @Test
    fun `user disconnect never reconnects`() = runTest {
        startConfirmed()
        voice.disconnect(); runCurrent()
        assertEquals(VoiceState.DISCONNECTED, voice.state.value)
        advanceTimeBy(120_000); runCurrent()
        assertEquals(1, fakes.size)  // no new leg
        assertEquals(VoiceState.DISCONNECTED, voice.state.value)
    }

    @Test
    fun `server terminal disconnected never reconnects`() = runTest {
        startConfirmed()
        fakes[0].incoming.trySend(WsMessage.Text(
            """{"type":"disconnected","data":"Connection lost after multiple reconnection attempts"}"""))
        runCurrent()
        assertEquals(VoiceState.ERROR, voice.state.value)
        advanceTimeBy(120_000); runCurrent()
        assertEquals(1, fakes.size)  // server said dead — stay dead
        assertEquals(VoiceState.ERROR, voice.state.value)
    }
}
