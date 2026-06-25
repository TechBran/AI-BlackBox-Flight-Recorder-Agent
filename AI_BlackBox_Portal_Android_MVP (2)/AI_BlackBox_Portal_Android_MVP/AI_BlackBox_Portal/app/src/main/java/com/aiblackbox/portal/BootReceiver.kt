package com.aiblackbox.portal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters

/**
 * Re-starts the model-free notification listener after a device reboot (MN.5) so the
 * phone is a reachable notification target without the user re-opening the app.
 *
 * **Why a WorkManager hand-off instead of a direct start.** On Android 12+ a
 * [BroadcastReceiver] handling `ACTION_BOOT_COMPLETED` runs in a BACKGROUND context;
 * calling `startForegroundService()` from there throws
 * `ForegroundServiceStartNotAllowedException`. WorkManager's worker runs in a context
 * where starting the FGS is permitted (and is itself boot-persistent), so we enqueue a
 * [BootStartWorker] OneTimeWorkRequest and let it bring up [NotificationListenerFgs].
 *
 * Registered (exported, with the BOOT_COMPLETED intent-filter) in the manifest;
 * requires the `RECEIVE_BOOT_COMPLETED` permission. Best-effort: a failure to enqueue
 * is swallowed (the listener simply comes up the next time the app is opened).
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_LOCKED_BOOT_COMPLETED &&
            // Some OEMs replace the AOSP boot broadcast with a quick-boot variant.
            action != "android.intent.action.QUICKBOOT_POWERON"
        ) {
            return
        }
        Log.d(TAG, "boot completed ($action); enqueuing listener (re)start")
        runCatching {
            val request = OneTimeWorkRequestBuilder<BootStartWorker>().build()
            WorkManager.getInstance(context.applicationContext)
                .enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.KEEP, request)
        }.onFailure {
            Log.w(TAG, "failed to enqueue boot worker (${it.javaClass.simpleName})")
        }
    }

    companion object {
        private const val TAG = "BootReceiver"
        const val WORK_NAME = "blackbox_notify_listener_boot_start"
    }
}

/**
 * Brings up [NotificationListenerFgs] from a context where starting a foreground
 * service is permitted (run by WorkManager after boot, MN.5). [NotificationListenerFgs.start]
 * is itself best-effort + non-throwing, so this always reports success — a platform
 * refusal is already logged inside it and is not worth a WorkManager retry storm.
 */
class BootStartWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        runCatching { NotificationListenerFgs.start(applicationContext) }
            .onFailure { Log.w(TAG, "listener start from boot worker refused (${it.javaClass.simpleName})") }
        return Result.success()
    }

    companion object {
        private const val TAG = "BootStartWorker"
    }
}
