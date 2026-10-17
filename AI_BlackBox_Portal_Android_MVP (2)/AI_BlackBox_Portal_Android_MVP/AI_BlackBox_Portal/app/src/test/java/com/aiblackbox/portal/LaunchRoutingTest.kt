package com.aiblackbox.portal

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A01-activities-root-04 — a notification tap lands where the launcher would:
 * the native UI by default, the legacy WebView only for an operator who chose it,
 * the pairing screen when the phone is not paired. Before, every task and /notify
 * tap opened the WebView wrapper (KeepAliveService + wake lock + web Portal) even
 * in native mode.
 */
class LaunchRoutingTest {

    @Test fun `unpaired lands on pairing`() {
        assertEquals(LaunchTarget.PAIRING, resolveLaunchTarget(origin = null, useNativeUi = true))
        assertEquals(LaunchTarget.PAIRING, resolveLaunchTarget(origin = "", useNativeUi = true))
        assertEquals(LaunchTarget.PAIRING, resolveLaunchTarget(origin = "   ", useNativeUi = false))
    }

    @Test fun `paired lands on the native UI unless the web UI was chosen`() {
        assertEquals(LaunchTarget.NATIVE, resolveLaunchTarget("http://192.168.1.50:9091", useNativeUi = true))
        assertEquals(LaunchTarget.WEB, resolveLaunchTarget("http://192.168.1.50:9091", useNativeUi = false))
    }

    @Test fun `each notification kind has its own PendingIntent identity`() {
        // Task taps and /notify taps never share (and overwrite) one PendingIntent:
        // the action is part of a PendingIntent's identity, so a /notify id that
        // happens to equal the task request code still gets its own.
        assertNotEquals(
            BlackBoxNotificationManager.ACTION_OPEN_FROM_TASK,
            BlackBoxNotificationManager.ACTION_OPEN_FROM_REMOTE,
        )
        assertTrue(BlackBoxNotificationManager.ACTION_OPEN_FROM_TASK.startsWith("com.aiblackbox.portal."))
        assertTrue(BlackBoxNotificationManager.ACTION_OPEN_FROM_REMOTE.startsWith("com.aiblackbox.portal."))
    }
}
