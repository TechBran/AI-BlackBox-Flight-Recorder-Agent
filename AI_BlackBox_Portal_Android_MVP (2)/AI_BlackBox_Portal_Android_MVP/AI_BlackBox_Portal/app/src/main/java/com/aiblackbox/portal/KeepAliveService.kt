package com.aiblackbox.portal

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

class KeepAliveService : Service() {

    companion object {
        private const val TAG = "KeepAliveService"
        private const val CHANNEL_ID_SERVICE = "blackbox_service"
        private const val NOTIFICATION_ID_SERVICE = NotificationIds.KEEP_ALIVE
    }

    override fun onBind(intent: Intent?): IBinder? {
        return null
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val notificationIntent = Intent(this, PortalActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            notificationIntent,
            PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID_SERVICE)
            .setContentTitle("BlackBox Portal is Active")
            .setContentText("Maintaining connection to portal...")
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()

        if (!FgsSupport.startForegroundSafely(
                this, NOTIFICATION_ID_SERVICE, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC, TAG,
            )
        ) {
            // Refused (a background re-creation): a sticky non-foreground service only
            // waits to be killed, so stop instead of being restarted into the same refusal.
            stopSelf()
            return START_NOT_STICKY
        }

        return START_STICKY
    }

    // dataSync is capped at 6 hours a day on Android 15+; this service runs for as long
    // as the WebView activity lives, so it reaches the cap on a long evening.
    override fun onTimeout(startId: Int, fgsType: Int) {
        FgsSupport.stopOnTimeout(this, TAG, fgsType)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val serviceChannel = NotificationChannel(
                CHANNEL_ID_SERVICE,
                "BlackBox Background Service",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps the app alive to receive notifications"
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(serviceChannel)
        }
    }
}
