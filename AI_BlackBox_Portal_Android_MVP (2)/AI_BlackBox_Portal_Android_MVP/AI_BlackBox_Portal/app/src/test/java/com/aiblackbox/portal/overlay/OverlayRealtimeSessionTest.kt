package com.aiblackbox.portal.overlay

import android.app.Application
import android.os.Handler
import android.os.Looper
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The overlay's live voice session, out of OverlayService (rank 76), against a real
 * WebSocket (MockWebServer) on Robolectric: the endpoint per model, the connect message,
 * the operator gate, and what each box message makes the overlay show (status, the live
 * reply, transcript lines, errors), down to a clean disconnect.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class OverlayRealtimeSessionTest {

    private lateinit var server: MockWebServer
    private lateinit var scope: CoroutineScope
    private val fromPhone = CopyOnWriteArrayList<String>()
    @Volatile private var boxSocket: WebSocket? = null

    /** Records what the session asks the overlay to show. */
    private class FakeHost(override val serverUrl: String) : OverlayRealtimeSession.Host {
        override var operator: String = "op"
        var blocked = false
        val statuses = mutableListOf<String>()
        val toasts = mutableListOf<String>()
        val transcript = mutableListOf<Pair<String, String>>()
        val live = mutableListOf<String>()
        var connecting: Boolean? = null
        var uiRefreshes = 0
        override fun operatorBlocks() = blocked
        override fun showToast(message: String) { toasts += message }
        override fun updateStatus(status: String) { statuses += status }
        override fun updateRealtimeUi() { uiRefreshes++ }
        override fun updateMicUi(isActive: Boolean, userMuted: Boolean, aiSpeaking: Boolean) {}
        override fun updateBubbleGlow() {}
        override fun updateScreenShareUi(isActive: Boolean) {}
        override fun syncBridgeState() {}
        override fun showConnecting(connecting: Boolean) { this.connecting = connecting }
        override fun showTranscriptEntry(speaker: String, text: String, count: Int) { transcript += speaker to text }
        override fun updateLiveResponse(text: String) { live += text }
        override fun screenCaptureAvailable() = false
        override fun ensureScreenFrames(onReady: (Boolean) -> Unit) = onReady(false)
        override fun latestScreenFrameJpegBase64(): String? = null
    }

    @Before fun setUp() {
        server = MockWebServer()
        server.enqueue(MockResponse.Builder().webSocketUpgrade(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { boxSocket = webSocket }
            override fun onMessage(webSocket: WebSocket, text: String) { fromPhone += text }
            // Complete the phone's close handshake, as the box does.
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(code, null) }
        }).build())
        server.start()
        scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    }

    @After fun tearDown() {
        scope.cancel()
        server.close()
    }

    private fun pumpUntil(what: String, done: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (System.currentTimeMillis() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (done()) return
            Thread.sleep(5)
        }
        fail("timed out waiting for $what")
    }

    private fun session(host: FakeHost) = OverlayRealtimeSession(
        RuntimeEnvironment.getApplication(), Handler(Looper.getMainLooper()), scope, host,
    )

    private fun box(type: String, data: String = "") =
        boxSocket!!.send(JSONObject().put("type", type).put("data", data).toString())

    @Test fun `a session connects as the operator with its voice, and shows what the box says`() {
        val host = FakeHost(server.url("").toString().trimEnd('/'))
        val s = session(host)
        s.selectedVoice = "Puck"
        s.connectRealtime()
        assertEquals(true, host.connecting)
        assertEquals("Connecting...", host.statuses.first())

        pumpUntil("the socket to open") { s.isRealtimeConnected && fromPhone.isNotEmpty() }
        assertEquals(false, host.connecting)
        val connect = JSONObject(fromPhone.first())
        assertEquals("connect", connect.getString("type"))
        assertEquals("op", connect.getString("operator"))
        assertEquals("Puck", connect.getString("voice"))
        assertTrue(server.takeRequest().target.startsWith("/ws/gemini-live/"))

        box("connected")
        box("transcript_delta", "Hel")
        box("transcript_delta", "lo")
        box("user_transcript", "hi there")
        box("response_complete")
        box("error", "quota")
        pumpUntil("the box's messages") { host.toasts.contains("Error: quota") }

        assertTrue(host.statuses.contains("Connected"))
        assertTrue(host.statuses.contains("Ready"))
        assertEquals(listOf("Hel", "Hello", ""), host.live)
        assertEquals(listOf("User" to "hi there", "AI" to "Hello"), host.transcript)
        assertEquals(2, s.transcriptEntries.size)
        assertEquals("", s.currentAIResponse.toString())

        s.disconnectRealtime()
        assertFalse(s.isRealtimeConnected)
        assertEquals("Disconnected", host.statuses.last())
    }

    @Test fun `each model has its own endpoint`() {
        val host = FakeHost(server.url("").toString().trimEnd('/'))
        val s = session(host)
        s.currentRealtimeModel = OverlayRealtimeSession.RealtimeModel.GROK_LIVE
        s.connectRealtime()
        pumpUntil("the socket to open") { s.isRealtimeConnected }
        assertTrue(server.takeRequest().target.startsWith("/ws/grok-live/"))
        assertEquals(listOf("Ara", "Rex", "Sal", "Eve", "Leo"), s.voicesForModel().toList())
        s.disconnectRealtime()
    }

    @Test fun `no operator or no box address opens nothing`() {
        val blocked = FakeHost(server.url("").toString().trimEnd('/')).apply { blocked = true }
        session(blocked).connectRealtime()
        assertTrue(blocked.statuses.isEmpty())

        val noBox = FakeHost("")
        session(noBox).connectRealtime()
        assertEquals(listOf("Server URL not set"), noBox.toasts)
        assertEquals(0, server.requestCount)
    }

    @Test fun `the box closing the socket ends the whole session`() {
        val host = FakeHost(server.url("").toString().trimEnd('/'))
        val s = session(host)
        s.connectRealtime()
        pumpUntil("the socket to open") { s.isRealtimeConnected && boxSocket != null }

        boxSocket!!.close(1001, "restarting")
        pumpUntil("the teardown") { !s.isRealtimeConnected }
        assertEquals("Disconnected", host.statuses.last())
        assertEquals(false, host.connecting)
        assertFalse("the mic is off", s.isRealtimeRecording)
        assertFalse(s.isAISpeaking)
        assertFalse(s.isUserMuted)
        assertTrue("no failure toast for a clean close", host.toasts.isEmpty())
    }

    @Test fun `a failed connection ends the session and says so`() {
        // A box that is gone: the socket fails instead of opening.
        val gone = MockWebServer().apply { start() }
        val url = gone.url("").toString().trimEnd('/')
        gone.close()
        val host = FakeHost(url)
        val s = session(host)
        s.connectRealtime()
        assertEquals(true, host.connecting)

        pumpUntil("the failure") { host.toasts.isNotEmpty() }
        assertFalse(s.isRealtimeConnected)
        assertEquals("Connection failed", host.statuses.last())
        assertEquals("the spinner goes", false, host.connecting)
        assertFalse(s.isRealtimeRecording)
        assertTrue(host.toasts.single(), host.toasts.single().startsWith("Connection failed: "))
    }

    @Test fun `grok cannot share the screen, and no projection means no share`() {
        val host = FakeHost(server.url("").toString().trimEnd('/'))
        val s = session(host)
        s.currentRealtimeModel = OverlayRealtimeSession.RealtimeModel.GROK_LIVE
        s.toggleScreenSharing()
        s.currentRealtimeModel = OverlayRealtimeSession.RealtimeModel.GPT_REALTIME
        s.toggleScreenSharing()
        assertEquals(listOf("Screen sharing not available - Grok is audio/text only", "Screen capture not available"), host.toasts)
        assertFalse(s.isScreenSharing)
    }
}
