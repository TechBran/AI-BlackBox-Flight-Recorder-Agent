package com.aiblackbox.portal.data.wellness

import android.Manifest
import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import com.aiblackbox.portal.BlackBoxNotificationManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * Rule W33 (check A12): health notices are private. The `blackbox_wellness` channel hides
 * contents on the lock screen, every notice carries the neutral public version, and a `/notify`
 * push in category `wellness` lands on that channel rather than the ordinary task channel.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class WellnessNotificationPrivacyTest {

    private val app: Application = RuntimeEnvironment.getApplication()
    private val nm = app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    private fun posted(): List<Notification> = shadowOf(nm).allNotifications

    @Test
    fun `the wellness channel is private`() {
        BlackBoxNotificationManager(app)
        val channel = nm.getNotificationChannel(BlackBoxNotificationManager.CHANNEL_ID_WELLNESS)
        assertNotNull(channel)
        assertEquals(Notification.VISIBILITY_PRIVATE, channel.lockscreenVisibility)
    }

    @Test
    fun `a notice is private with a neutral public version`() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        BlackBoxNotificationManager(app).showWellnessNotice("Finish setting up Health on this phone",
            "Open BlackBox, Settings, Health", "wellness-setup")
        val n = posted().single()
        assertEquals(BlackBoxNotificationManager.CHANNEL_ID_WELLNESS, n.channelId)
        assertEquals(Notification.VISIBILITY_PRIVATE, n.visibility)
        val pub = n.publicVersion!!
        assertEquals(BlackBoxNotificationManager.WELLNESS_PUBLIC_TEXT, pub.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
        assertFalse(pub.extras.containsKey(Notification.EXTRA_TEXT) &&
            pub.extras.getCharSequence(Notification.EXTRA_TEXT)?.isNotEmpty() == true)
    }

    @Test
    fun `a wellness push from the bus goes to the private channel, others do not`() {
        shadowOf(app).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        val manager = BlackBoxNotificationManager(app)
        manager.showRemoteNotification("Finish setting up Health on this phone", "Open BlackBox, Settings, Health",
            operator = WellnessFixtures.OPERATOR, category = "wellness", notifId = "n-1")
        manager.showRemoteNotification("Task done", "Your image is ready", category = "task", notifId = "n-2")
        val byChannel = posted().groupBy { it.channelId }
        assertEquals(1, byChannel[BlackBoxNotificationManager.CHANNEL_ID_WELLNESS]?.size)
        assertTrue(byChannel.keys.any { it != BlackBoxNotificationManager.CHANNEL_ID_WELLNESS })
        val wellness = byChannel.getValue(BlackBoxNotificationManager.CHANNEL_ID_WELLNESS).single()
        assertEquals(Notification.VISIBILITY_PRIVATE, wellness.visibility)
    }

    @Test
    fun `the worker progress notification is private and silent`() {
        val n = BlackBoxNotificationManager(app).buildWellnessProgress("Importing health history 42%", 42)
        assertEquals(BlackBoxNotificationManager.CHANNEL_ID_WELLNESS, n.channelId)
        assertEquals(Notification.VISIBILITY_PRIVATE, n.visibility)
        assertEquals(BlackBoxNotificationManager.WELLNESS_PUBLIC_TEXT,
            n.publicVersion!!.extras.getCharSequence(Notification.EXTRA_TITLE).toString())
        assertTrue(n.flags and Notification.FLAG_ONGOING_EVENT != 0)
    }
}
