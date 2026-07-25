package com.aiblackbox.portal

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.FileProvider
import com.aiblackbox.portal.data.notifications.NotificationDeliveryState
import com.aiblackbox.portal.data.notifications.readNotificationDeliveryState
import com.aiblackbox.portal.overlay.NAVIGATION_ACTION_LABEL
import com.aiblackbox.portal.overlay.NavigationNotifier
import com.aiblackbox.portal.overlay.NavigationNotifyOutcome
import com.aiblackbox.portal.overlay.NavigationPush
import com.aiblackbox.portal.overlay.navigationNotificationText
import com.aiblackbox.portal.overlay.navigationNotificationTitle
import com.aiblackbox.portal.overlay.navigationUri

class BlackBoxNotificationManager(private val context: Context) : NavigationNotifier {

    companion object {
        private const val CHANNEL_ID_TASKS = "blackbox_tasks"
        private const val CHANNEL_ID_SYSTEM = "blackbox_system"
        private const val CHANNEL_ID_DOWNLOADS = "blackbox_downloads"

        /** (M3) The navigation-prompt channel. HIGH importance + a NAVIGATION category so
         *  it heads-up and surfaces on a LOCKED screen — that is the cron case.
         *  Public since M4 so `readNotificationDeliveryState` inspects the SAME channel
         *  this class posts to, by construction rather than by a duplicated string. */
        const val CHANNEL_ID_NAVIGATION = "blackbox_navigation"
        private const val TAG = "BlackBoxBridge"
        private const val DOWNLOAD_NOTIFICATION_ID = 9999
    }

    init {
        createNotificationChannels()
    }

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            // Task completion channel
            val taskChannel = NotificationChannel(
                CHANNEL_ID_TASKS,
                "BlackBox Tasks",
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply {
                description = "Notifications for completed tasks (images, videos, audio)"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 200, 100, 200)
            }

            // System notifications channel
            val systemChannel = NotificationChannel(
                CHANNEL_ID_SYSTEM,
                "BlackBox System",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "System status and updates"
            }

            // Downloads channel - high importance so user sees it
            val downloadChannel = NotificationChannel(
                CHANNEL_ID_DOWNLOADS,
                "Downloads",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "File download progress and completion"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 100, 50, 100)
            }

            // (M3) Navigation prompts. HIGH importance so the prompt heads-up rather than
            // sitting silently in the shade — a 07:30 cron push is worthless if it is not
            // seen. lockscreenVisibility PUBLIC so the DESTINATION is legible without
            // unlocking: a prompt that hides where it is about to send you is not consent.
            val navigationChannel = NotificationChannel(
                CHANNEL_ID_NAVIGATION,
                "Navigation",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "Navigation destinations pushed from the BlackBox — tap Navigate to start"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 250, 150, 250)
                lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
            }

            val notificationManager = context.getSystemService(NotificationManager::class.java)
            if (notificationManager != null) {
                notificationManager.createNotificationChannel(taskChannel)
                notificationManager.createNotificationChannel(systemChannel)
                notificationManager.createNotificationChannel(downloadChannel)
                notificationManager.createNotificationChannel(navigationChannel)
                Log.d(TAG, "Notification channels created: $CHANNEL_ID_TASKS, $CHANNEL_ID_SYSTEM, $CHANNEL_ID_DOWNLOADS, $CHANNEL_ID_NAVIGATION")
            } else {
                Log.e(TAG, "Failed to get NotificationManager service")
            }
        }
    }

    /**
     * Show download in progress notification
     */
    fun showDownloadStarted(filename: String): Int {
        val notificationId = DOWNLOAD_NOTIFICATION_ID + (System.currentTimeMillis() % 1000).toInt()
        Log.d(TAG, "showDownloadStarted: Creating notification for $filename with ID $notificationId")

        val notification = NotificationCompat.Builder(context, CHANNEL_ID_DOWNLOADS)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle("Downloading...")
            .setContentText(filename)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setOngoing(true) // Cannot be dismissed while downloading
            .setProgress(0, 0, true) // Indeterminate progress
            .build()

        try {
            val hasPermission = hasNotificationPermission()
            Log.d(TAG, "showDownloadStarted: hasNotificationPermission=$hasPermission")

            if (hasPermission) {
                NotificationManagerCompat.from(context).notify(notificationId, notification)
                Log.d(TAG, "Download started notification POSTED: $filename (ID=$notificationId)")
            } else {
                Log.w(TAG, "NO NOTIFICATION PERMISSION - notification will not show!")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error showing download notification: ${e.message}", e)
        }

        return notificationId
    }

    /**
     * Update download notification to show completion with tap-to-open
     */
    fun showDownloadComplete(notificationId: Int, filename: String, fileUri: Uri, mimeType: String) {
        // Create intent to open the file
        val openIntent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(fileUri, mimeType)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }

        val pendingIntent = PendingIntent.getActivity(
            context,
            notificationId,
            openIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ID_DOWNLOADS)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("Download complete")
            .setContentText(filename)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true) // Dismiss when clicked
            .setVibrate(longArrayOf(0, 100, 50, 100))
            .build()

        try {
            if (hasNotificationPermission()) {
                NotificationManagerCompat.from(context).notify(notificationId, notification)
                Log.d(TAG, "Download complete notification shown: $filename, tap to open")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error showing download complete notification", e)
        }
    }

    /**
     * Show download failed notification
     */
    fun showDownloadFailed(notificationId: Int, filename: String, error: String) {
        val notification = NotificationCompat.Builder(context, CHANNEL_ID_DOWNLOADS)
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("Download failed")
            .setContentText("$filename: $error")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .build()

        try {
            if (hasNotificationPermission()) {
                NotificationManagerCompat.from(context).notify(notificationId, notification)
                Log.d(TAG, "Download failed notification shown: $filename")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error showing download failed notification", e)
        }
    }

    /**
     * Cancel a notification by ID
     */
    fun cancelNotification(notificationId: Int) {
        NotificationManagerCompat.from(context).cancel(notificationId)
    }

    private fun hasNotificationPermission(): Boolean {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            androidx.core.content.ContextCompat.checkSelfPermission(
                context,
                android.Manifest.permission.POST_NOTIFICATIONS
            ) == android.content.pm.PackageManager.PERMISSION_GRANTED
    }

    /**
     * Post a REAL system notification for an inbound remote `/notify` (MN.4), MODEL-FREE
     * (plain [NotificationManagerCompat.notify], no LLM/Gemma anywhere in the path).
     *
     * **Idempotent retries.** The bus's [notifId] is mapped to a STABLE (tag, id): the
     * id is the (tag, id) notify overload's tag so a retried push with the SAME notifId
     * COLLAPSES onto the same notification instead of stacking a duplicate. When notifId
     * is null (no idempotency key supplied) we fall back to a fresh time-based id (no
     * collapse possible), preserving the old behaviour for keyless callers.
     *
     * **Metadata-only / empty body.** [body] may be empty (a metadata-only cross-operator
     * push). The caller passes title + category; an empty body simply shows the title
     * (and category, if provided) — [NotificationCompat] tolerates an empty content text.
     */
    fun showRemoteNotification(
        title: String,
        body: String,
        operator: String? = null,
        category: String? = null,
        notifId: String? = null,
    ) {
        Log.d(TAG, "showRemoteNotification: title=$title, hasBody=${body.isNotBlank()}, notifId=$notifId")

        val intent = Intent(context, PortalActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("from_notification", true)
            putExtra("notif_category", category)
            putExtra("notif_operator", operator)
        }

        // STABLE (tag, id): tag = notifId (the bus idempotency key); id = a stable hash of
        // it so the (tag, id) notify overload collapses retries. A NanoHTTPD worker may
        // deliver the same notifId more than once (bus retry) — using the SAME (tag, id)
        // makes the second post UPDATE the first rather than stack a duplicate.
        val tag: String? = notifId?.takeIf { it.isNotBlank() }
        val id: Int = tag?.let { stableNotificationId(it) }
            ?: (System.currentTimeMillis() % Int.MAX_VALUE).toInt()

        val pendingIntent = PendingIntent.getActivity(
            context,
            id, // distinct requestCode per (tag,id) so the PendingIntent isn't shared across notifs
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val notification = NotificationCompat.Builder(context, CHANNEL_ID_TASKS)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setVibrate(longArrayOf(0, 200, 100, 200))
            .build()

        try {
            if (hasNotificationPermission()) {
                NotificationManagerCompat.from(context).notify(tag, id, notification)
                Log.d(TAG, "Remote notification POSTED (tag=$tag, id=$id)")
            } else {
                Log.w(TAG, "Notification permission NOT granted, cannot show remote notification")
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "SecurityException showing remote notification", e)
        } catch (e: Exception) {
            Log.e(TAG, "Error showing remote notification", e)
        }
    }

    /**
     * (M3) Post the NAVIGATION prompt — the only delivery Android permits when the app is
     * not in the foreground.
     *
     * ## Why a notification at all
     * A remote `navigate` push with the app backgrounded is silently DISCARDED by the
     * Background Activity Launch restriction, and `startActivity` does not throw, so the
     * actuator cannot tell. The documented exemption is a **user tap**: the activity launch
     * that follows a tap on a notification action is allowed. So the box does not open Maps
     * — it asks, and the owner's tap opens Maps.
     *
     * ## What makes it work from a LOCKED screen (the cron case)
     *  - `CHANNEL_ID_NAVIGATION` is IMPORTANCE_HIGH → heads-up rather than a silent shade entry.
     *  - [NotificationCompat.CATEGORY_NAVIGATION] + PRIORITY_HIGH → correct ranking/ducking
     *    under Do-Not-Disturb-adjacent policies on pre-O and OEM skins.
     *  - VISIBILITY_PUBLIC (on the notification AND the channel) → the DESTINATION is readable
     *    on the lock screen. Consent requires seeing where you are being sent.
     *  - The action's [PendingIntent] is `getActivity`, so the system itself performs the
     *    launch on tap (inserting the keyguard unlock challenge when locked). We never
     *    re-enter our own process to `startActivity`, which would land back under BAL.
     *
     * ## Honest outcomes
     * Returns [NavigationNotifyOutcome.PERMISSION_MISSING] when the phone cannot show a
     * BlackBox notification at all — POST_NOTIFICATIONS never granted (Android 13+), app
     * notifications switched off, or this channel at IMPORTANCE_NONE — so the caller
     * reports that instead of claiming a delivery that silently went nowhere. Returns
     * [NavigationNotifyOutcome.FAILED] when the platform refuses the post. Never throws.
     *
     * ## Dedup
     * [NavigationPush.dedupKey] maps to a STABLE (tag, id) via [stableNotificationId], the
     * same mechanism [showRemoteNotification] uses: a retrying cron COLLAPSES onto the one
     * prompt instead of stacking a pile of "Navigate to the job site" cards. The
     * PendingIntent uses the same id as its requestCode with FLAG_UPDATE_CURRENT, so a
     * re-post with a changed destination updates the target rather than resurrecting the
     * first one's extras.
     */
    override fun postNavigation(push: NavigationPush): NavigationNotifyOutcome {
        // (M4) The permission bit alone is not the truth. `NotificationManagerCompat.notify`
        // is a SILENT no-op when the app's notifications are switched off, or when this
        // channel is at IMPORTANCE_NONE — both of which the Fold was in during M3
        // validation. Reporting POSTED in those states is precisely the lie M3 exists to
        // prevent, so all three mute switches map to PERMISSION_MISSING (one wire kind,
        // one fix: turn BlackBox notifications back on). The DELIVERS path is byte-for-byte
        // the device-proven M3 behaviour.
        val delivery = readNotificationDeliveryState(context)
        if (delivery != NotificationDeliveryState.DELIVERS) {
            Log.w(TAG, "navigation prompt NOT delivered: notifications unavailable ($delivery)")
            return NavigationNotifyOutcome.PERMISSION_MISSING
        }
        return try {
            // The EXACT intent a direct launch would have fired — same pure URI builder,
            // same whitelisted mode/avoid, same resolved package. The tap reproduces it.
            val navIntent = Intent(
                Intent.ACTION_VIEW,
                Uri.parse(navigationUri(push.destination, push.travelMode, push.avoid)),
            ).apply {
                push.packageName?.let { setPackage(it) }
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }

            val tag = push.dedupKey
            val id = stableNotificationId(tag)
            val navPending = PendingIntent.getActivity(
                context,
                id,
                navIntent,
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )

            val title = navigationNotificationTitle(push.destination)
            val text = navigationNotificationText(push.destination, push.travelMode)

            val notification = NotificationCompat.Builder(context, CHANNEL_ID_NAVIGATION)
                .setSmallIcon(android.R.drawable.ic_menu_directions)
                .setContentTitle(title)
                .setContentText(text)
                // Long free-text addresses must be fully readable when expanded.
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setCategory(NotificationCompat.CATEGORY_NAVIGATION)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                // Tapping the body OR the action button both navigate — either is a user
                // gesture, which is what makes the launch legal.
                .setContentIntent(navPending)
                .addAction(android.R.drawable.ic_menu_directions, NAVIGATION_ACTION_LABEL, navPending)
                .setAutoCancel(true)
                // A collapsed retry must not re-buzz the phone every time the cron retries.
                .setOnlyAlertOnce(true)
                .setVibrate(longArrayOf(0, 250, 150, 250))
                .build()

            NotificationManagerCompat.from(context).notify(tag, id, notification)
            // NEVER log the destination (leak discipline) — only the dedup id.
            Log.d(TAG, "navigation prompt POSTED (tag=$tag, id=$id)")
            NavigationNotifyOutcome.POSTED
        } catch (e: SecurityException) {
            Log.e(TAG, "SecurityException posting navigation prompt", e)
            NavigationNotifyOutcome.FAILED
        } catch (e: Exception) {
            Log.e(TAG, "Error posting navigation prompt (${e.javaClass.simpleName})", e)
            NavigationNotifyOutcome.FAILED
        }
    }

    /** Stable positive int id derived from a string key (the bus notif_id), so retries
     *  collapse deterministically. abs(hashCode) keeps it positive; 0 is avoided. */
    private fun stableNotificationId(key: String): Int {
        val h = key.hashCode()
        val abs = if (h == Int.MIN_VALUE) Int.MAX_VALUE else kotlin.math.abs(h)
        return if (abs == 0) 1 else abs
    }

    fun showTaskNotification(
        title: String,
        body: String,
        operator: String? = null,
        taskType: String? = null,
        isSuccess: Boolean = true
    ) {
        Log.d(TAG, "Notification Manager: Building notification - Title: $title, Body: $body")

        // Create intent to open app when notification clicked
        val intent = Intent(context, PortalActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra("from_notification", true)
            putExtra("task_type", taskType)
        }

        val pendingIntent = PendingIntent.getActivity(
            context,
            0,
            intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        // Choose icon based on success/failure (using standard android icons for now)
        val icon = if (isSuccess) {
            android.R.drawable.ic_dialog_info
        } else {
            android.R.drawable.ic_dialog_alert
        }

        // Build notification
        val notification = NotificationCompat.Builder(context, CHANNEL_ID_TASKS)
            .setSmallIcon(icon)
            .setContentTitle(title)
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true) // Dismiss when clicked
            .setVibrate(longArrayOf(0, 200, 100, 200))
            .build()

        // Generate unique notification ID
        val notificationId = (System.currentTimeMillis() % Int.MAX_VALUE).toInt()

        try {
            // Check permission before notifying
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || 
                androidx.core.content.ContextCompat.checkSelfPermission(context, android.Manifest.permission.POST_NOTIFICATIONS) == android.content.pm.PackageManager.PERMISSION_GRANTED) {
                
                NotificationManagerCompat.from(context).notify(notificationId, notification)
                Log.d(TAG, "Notification dispatched to system with ID: $notificationId")
            } else {
                Log.w(TAG, "Notification permission NOT granted, cannot show notification")
            }
        } catch (e: SecurityException) {
            Log.e(TAG, "SecurityException showing notification", e)
            e.printStackTrace()
        } catch (e: Exception) {
            Log.e(TAG, "Error showing notification", e)
            e.printStackTrace()
        }
    }
}
