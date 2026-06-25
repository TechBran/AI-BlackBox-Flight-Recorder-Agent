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

class BlackBoxNotificationManager(private val context: Context) {

    companion object {
        private const val CHANNEL_ID_TASKS = "blackbox_tasks"
        private const val CHANNEL_ID_SYSTEM = "blackbox_system"
        private const val CHANNEL_ID_DOWNLOADS = "blackbox_downloads"
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

            val notificationManager = context.getSystemService(NotificationManager::class.java)
            if (notificationManager != null) {
                notificationManager.createNotificationChannel(taskChannel)
                notificationManager.createNotificationChannel(systemChannel)
                notificationManager.createNotificationChannel(downloadChannel)
                Log.d(TAG, "Notification channels created: $CHANNEL_ID_TASKS, $CHANNEL_ID_SYSTEM, $CHANNEL_ID_DOWNLOADS")
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
