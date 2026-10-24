package com.aiblackbox.portal.ui.voice

import com.aiblackbox.portal.data.voice.VoiceState
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Audit rank 67, Android half (A12-voice-cron-08, -02, A14-ui-misc-09): voice sessions,
 * the phone's audio routing and the pollers get a lifecycle.
 *  - Leaving the Voice screen or backgrounding the app left the realtime socket open
 *    (billing), the capture loop running and MODE_IN_COMMUNICATION applied to the rest of
 *    the app; a server-side end left the phone in the in-call audio path with no way to
 *    restore it. The session now ends on stop (not on a Fold unfold's configuration
 *    change) and routing is restored on every way a session ends.
 *  - SMS and Timeline polled every 10s while backgrounded (the SMS poll even marked unseen
 *    texts read); they now poll only while RESUMED, and the screens collect with the
 *    lifecycle.
 */
class ScreenLifecycleTest {

    @Test fun `stopping the screen ends a live session`() {
        for (state in VoiceState.values().filter { it != VoiceState.DISCONNECTED }) {
            assertTrue(state.name, endVoiceSessionOnStop(changingConfigurations = false, state = state))
        }
    }

    @Test fun `a configuration change (Fold unfold, rotation) keeps the session`() {
        assertFalse(endVoiceSessionOnStop(changingConfigurations = true, state = VoiceState.CONNECTED))
    }

    @Test fun `nothing to end when already disconnected`() {
        assertFalse(endVoiceSessionOnStop(changingConfigurations = false, state = VoiceState.DISCONNECTED))
    }

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText().replace("\r\n", "\n")
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel")
        return ""
    }

    private val ui = "src/main/java/com/aiblackbox/portal/ui"

    @Test fun `the voice screen ends its session on stop and restores routing on every end`() {
        val src = source("$ui/voice/VoiceScreen.kt")
        assertTrue("LifecycleStartEffect" in src && "endVoiceSessionOnStop(" in src)
        assertFalse("collect with the lifecycle", ".collectAsState()" in src)
        val collector = src.substring(src.indexOf("voiceClient?.state?.collect"))
        assertTrue("a server-side end restores routing", "restoreAudioRouting()" in collector.substring(0, 1500))
        for (fn in listOf("fun disconnect()", "override fun onCleared()")) {
            val body = src.substring(src.indexOf(fn))
            assertTrue("$fn restores routing", "restoreAudioRouting()" in body.substring(0, 900))
        }
    }

    @Test fun `SMS and Timeline poll only while resumed`() {
        for (rel in listOf("$ui/sms/SmsInboxScreen.kt", "$ui/timeline/TimelineScreen.kt")) {
            val src = source(rel)
            val effect = src.substring(src.indexOf("LifecycleResumeEffect("))
            val block = effect.substring(0, effect.indexOf("onPauseOrDispose"))
            assertTrue("$rel starts polling on resume", "startPolling(" in block)
            assertTrue("$rel stops polling on pause", "stopPolling()" in effect.substring(0, 400))
            assertFalse("$rel collects with the lifecycle", ".collectAsState()" in src)
        }
    }
}
