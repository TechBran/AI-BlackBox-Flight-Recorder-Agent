package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.ui.updates.RestartWait
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Settings → System's Restart Service and Cancel Tasks report what the backend
 * said (audit A11-settings-generation-13): before, both dropped the response,
 * so a restart that sudo refused (HTTP 200, {"status":"error"}) looked like
 * one that worked.
 */
class SystemActionsTest {

    @Test
    fun `a restart sudo refused is reported, not taken for a restart`() {
        assertEquals("Restart failed: sudo: a password is required",
            SystemActions.restartRefusal("""{"status":"error","message":"Restart failed: sudo: a password is required"}"""))
        assertEquals("Restart failed: boom",
            SystemActions.restartRefusal("""{"status":"error","error":"boom","trace":"..."}"""))
    }

    @Test
    fun `an accepted restart is not a refusal`() {
        assertNull(SystemActions.restartRefusal("""{"status":"success","message":"Service restart initiated"}"""))
        assertNull(SystemActions.restartRefusal(""))
    }

    @Test
    fun `cancel-all reports how many tasks it cancelled`() {
        assertEquals("Cancelled 3 tasks.", SystemActions.cancelledMessage("""{"cancelled":3}"""))
        assertEquals("Cancelled 1 task.", SystemActions.cancelledMessage("""{"cancelled":1}"""))
        assertEquals("No pending or running tasks to cancel.", SystemActions.cancelledMessage("""{"cancelled":0}"""))
        assertEquals("Cancel request sent.", SystemActions.cancelledMessage("not json"))
    }

    @Test
    fun `the restart outcome says whether a new process answered`() {
        assertTrue(SystemActions.restartOutcomeMessage(RestartWait.Outcome.Restarted(64_000)).contains("back online"))
        assertTrue(SystemActions.restartOutcomeMessage(RestartWait.Outcome.NotRestarted(182_000)).contains("did not restart"))
        assertTrue(SystemActions.restartOutcomeMessage(RestartWait.Outcome.StillDown(182_000)).contains("still down"))
    }
}
