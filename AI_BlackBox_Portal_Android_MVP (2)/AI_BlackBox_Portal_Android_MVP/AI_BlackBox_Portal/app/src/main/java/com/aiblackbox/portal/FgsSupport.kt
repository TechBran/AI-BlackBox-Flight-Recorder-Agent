package com.aiblackbox.portal

import android.app.Notification
import android.app.Service
import android.os.Build
import android.util.Log
import androidx.core.app.ServiceCompat

/**
 * Every notification id the app posts under (A01-activities-root-06). Each foreground
 * service and each notification family gets its own; before, the model download and the
 * terminal service both used 9094 (each overwrote the other's notification and the first
 * to stop removed the survivor's), and a download id could land on the keep-alive
 * service's 9999.
 */
object NotificationIds {
    const val OVERLAY = 1001
    const val BACKGROUND_TASK = 9091
    const val LOCAL_MODEL = 9093
    const val TERMINAL = 9094
    const val NOTIFICATION_LISTENER = 9095
    const val REMOTE_CONTROL_BANNER = 9096
    const val MODEL_DOWNLOAD = 9097
    const val KEEP_ALIVE = 9999

    /** The fixed ids above (unit-tested to be distinct). */
    val FIXED = listOf(
        OVERLAY, BACKGROUND_TASK, LOCAL_MODEL, TERMINAL, NOTIFICATION_LISTENER,
        REMOTE_CONTROL_BANNER, MODEL_DOWNLOAD, KEEP_ALIVE,
    )

    /** Per-file download notifications use this band, which no fixed id falls in. */
    const val DOWNLOAD_BASE = 20_000
    const val DOWNLOAD_SPAN = 1_000

    fun downloadId(nowMs: Long): Int = DOWNLOAD_BASE + (nowMs % DOWNLOAD_SPAN).toInt()

    /** One-off alerts start above every fixed id and the download band. */
    const val GENERAL_BASE = 100_000

    fun generalId(nowMs: Long): Int = GENERAL_BASE + (nowMs % (Int.MAX_VALUE - GENERAL_BASE)).toInt()
}

/**
 * The two things every foreground service must get right (A01-activities-root-02, -09).
 */
object FgsSupport {

    /**
     * startForeground that never throws into the framework. Android 12+ refuses a
     * foreground start from the background (ForegroundServiceStartNotAllowedException),
     * which is exactly what a START_STICKY service hits when the OS re-creates it after
     * an OOM kill; uncaught, that crashes the process in a loop. Returns false on a
     * refusal so the service can stop itself (or carry on, where it deliberately does).
     */
    fun startForegroundSafely(
        service: Service,
        id: Int,
        notification: Notification,
        type: Int,
        tag: String,
    ): Boolean = try {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ServiceCompat.startForeground(service, id, notification, type)
        } else {
            service.startForeground(id, notification)
        }
        true
    } catch (t: Throwable) {
        // Class name only: the message can carry device detail.
        Log.w(tag, "startForeground refused (${t.javaClass.simpleName})")
        false
    }

    /**
     * Service.onTimeout: Android 15+ caps time-limited types (dataSync: 6 hours a day)
     * and, if the service has not stopped within seconds of this call, kills the app
     * with "A foreground service of type dataSync did not stop within its timeout".
     * Leave the foreground and stop now.
     */
    fun stopOnTimeout(service: Service, tag: String, fgsType: Int) {
        Log.w(tag, "foreground-service time limit reached (type=$fgsType); stopping")
        runCatching { ServiceCompat.stopForeground(service, ServiceCompat.STOP_FOREGROUND_REMOVE) }
        service.stopSelf()
    }
}
