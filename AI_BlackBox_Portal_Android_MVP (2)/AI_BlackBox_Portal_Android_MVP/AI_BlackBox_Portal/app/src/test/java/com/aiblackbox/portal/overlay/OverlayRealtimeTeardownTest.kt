package com.aiblackbox.portal.overlay

import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * A02-overlay-service-02: when the server dropped the overlay's realtime socket
 * (restart, Wi-Fi or tailnet blip, provider timeout), onClosing/onFailure only changed
 * the label: the mic kept streaming into a dead socket with the mute button disabled,
 * the screen-share job kept running and the AudioTrack stayed open. Every end of a
 * session now runs the same teardown the user's disconnect runs. Also: a reply shorter
 * than the pre-buffer threshold never ended pre-buffering, so it never played.
 *
 * The session lives in OverlayRealtimeSession (out of OverlayService, rank 76); this pins
 * the wiring in its source, and OverlayRealtimeSessionTest drives it over a real socket.
 */
class OverlayRealtimeTeardownTest {

    private val src by lazy {
        var dir: File? = File(".").absoluteFile
        var found: String? = null
        while (dir != null && found == null) {
            for (rel in listOf("src/main/java/com/aiblackbox/portal/overlay/OverlayRealtimeSession.kt",
                "app/src/main/java/com/aiblackbox/portal/overlay/OverlayRealtimeSession.kt")) {
                val f = File(dir, rel)
                if (f.isFile) found = f.readText().replace("\r\n", "\n")
            }
            dir = dir.parentFile
        }
        found ?: run { fail("OverlayRealtimeSession.kt not found"); "" }
    }

    private fun body(signature: String, length: Int = 1600): String {
        val start = src.indexOf(signature)
        assertTrue("$signature not found", start >= 0)
        return src.substring(start, minOf(src.length, start + length))
    }

    @Test fun `a server close and a failure tear the session down like a disconnect`() {
        for (callback in listOf("override fun onClosing(", "override fun onFailure(")) {
            val cb = body(callback, 700)
            assertTrue("$callback runs the full teardown", "teardownRealtimeSession(" in cb)
            assertTrue("$callback ignores a socket that is no longer the session's", "webSocket !== realtimeWebSocket" in cb)
        }
        assertTrue("the user's disconnect uses the same teardown", "teardownRealtimeSession(" in body("fun disconnectRealtime()", 300))
    }

    @Test fun `the teardown stops the mic, the screen share and the audio track`() {
        val teardown = body("private fun teardownRealtimeSession(")
        for (step in listOf("stopRealtimeAudioStreaming()", "stopScreenSharing()", "releaseAudioTrack()", "isRealtimeConnected = false")) {
            assertTrue("teardown does $step", step in teardown)
        }
    }

    @Test fun `response_complete ends pre-buffering so a short reply plays`() {
        val branch = body("\"response_complete\" -> {", 900)
        assertTrue("isPreBuffering = false" in branch)
    }
}
