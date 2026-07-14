package com.aiblackbox.portal.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReturnToLiveHostTest {
    @Test
    fun completedHistoryPublishesImmediatelyWithoutStreaming() {
        val host = ReturnToLiveHostState()
        var resumes = 0

        host.register("main", visible = true, returning = false) { resumes++ }

        assertTrue(host.visible)
        host.resume()
        assertEquals(1, resumes)
    }

    @Test
    fun newestChatOwnerReplacesMainAndStaleDisposalCannotClearIt() {
        val host = ReturnToLiveHostState()
        var mainResumes = 0
        var claudeResumes = 0
        val main = host.register("main", visible = true, returning = false) { mainResumes++ }
        val claude = host.register("claude", visible = true, returning = true) { claudeResumes++ }

        main.dispose()
        host.resume()

        assertTrue(host.visible)
        assertTrue(host.returning)
        assertEquals(0, mainResumes)
        assertEquals(1, claudeResumes)
    }

    @Test
    fun activeOwnerCanPublishAndRouteDisposalClearsCallback() {
        val host = ReturnToLiveHostState()
        var resumes = 0
        val registration = host.register("gemini", visible = false, returning = false) { resumes++ }

        registration.publish(visible = true, returning = true)
        assertTrue(host.visible)
        assertTrue(host.returning)

        registration.dispose()
        host.resume()
        assertFalse(host.visible)
        assertFalse(host.returning)
        assertEquals(0, resumes)
    }
}
