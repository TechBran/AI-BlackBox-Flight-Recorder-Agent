package com.aiblackbox.portal

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat

/**
 * Foreground service that keeps the app process alive when backgrounded.
 * Ensures SSE streams, TTS generation, task polling, and snapshots complete.
 *
 * Started when a chat request or generation begins.
 * Stopped when all active tasks complete.
 */
class BackgroundTaskService : Service() {

    companion object {
        private const val TAG = "BGTaskService"
        const val CHANNEL_ID = "blackbox_tasks"
        const val NOTIFICATION_ID = NotificationIds.BACKGROUND_TASK
        const val ACTION_START = "com.aiblackbox.portal.START_TASKS"
        const val ACTION_STOP = "com.aiblackbox.portal.STOP_TASKS"
        const val EXTRA_TASK_LABEL = "task_label"

        private var _isRunning = false
        fun isRunning() = _isRunning
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                val label = intent?.getStringExtra(EXTRA_TASK_LABEL) ?: "Processing..."
                val notification = buildNotification(label)
                if (!FgsSupport.startForegroundSafely(
                        this, NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC, TAG,
                    )
                ) {
                    // Refused (background re-creation): stop rather than linger unprotected.
                    stopSelf()
                    return START_NOT_STICKY
                }
                _isRunning = true
                Log.d(TAG, "Started: $label")
            }
        }
        return START_STICKY
    }

    // dataSync is capped at 6 hours a day on Android 15+.
    override fun onTimeout(startId: Int, fgsType: Int) {
        _isRunning = false
        FgsSupport.stopOnTimeout(this, TAG, fgsType)
    }

    override fun onDestroy() {
        _isRunning = false
        Log.d(TAG, "Stopped")
        super.onDestroy()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Background Tasks",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps AI tasks running when app is in background"
                setShowBadge(false)
            }
            val mgr = getSystemService(NotificationManager::class.java)
            mgr?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(label: String): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, NativeMainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("AI BlackBox")
            .setContentText(label)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .build()
    }
}
