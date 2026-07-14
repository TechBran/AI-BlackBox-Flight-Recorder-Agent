package com.aiblackbox.portal.ui.chat

import org.junit.Assert.*
import org.junit.Test

class LiveStreamFollowPolicyTest {
    @Test fun `user input suspends immediately and resumes only after five idle seconds`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertTrue(policy.isSuspended)
        assertTrue(policy.showReturnToLive)
        assertFalse(policy.tick(5_999))
        assertTrue(policy.tick(6_000))
        assertFalse(policy.isSuspended)
    }

    @Test fun `continued interaction resets the five second deadline`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.onUserScrollSettled(4_000)
        assertFalse(policy.tick(8_999))
        assertTrue(policy.tick(9_000))
    }

    @Test fun `down arrow resumes immediately`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertTrue(policy.resumeNow())
        assertFalse(policy.isSuspended)
        assertFalse(policy.showReturnToLive)
    }

    @Test fun `terminal stream disables delayed return`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.stop()
        assertFalse(policy.tick(20_000))
        assertFalse(policy.isActive)
    }

    @Test fun `programmatic follow never enters suspended state`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onProgrammaticScrollStarted()
        policy.onProgrammaticScrollFinished()
        assertFalse(policy.isSuspended)
    }
}
