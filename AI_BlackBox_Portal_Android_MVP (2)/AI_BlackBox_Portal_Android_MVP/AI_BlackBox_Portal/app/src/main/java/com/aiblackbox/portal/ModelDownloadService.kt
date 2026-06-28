package com.aiblackbox.portal

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.LocalModelApi
import com.aiblackbox.portal.data.local.DownloadProgressBus
import com.aiblackbox.portal.data.local.LocalModelManager
import com.aiblackbox.portal.data.model.LocalBundle
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json

/**
 * Foreground service that DOWNLOADS an on-device Gemma bundle so the transfer
 * survives screen navigation (Phase C, durable downloads).
 *
 * **Why.** [com.aiblackbox.portal.ui.settings.LocalModelViewModel] used to run the
 * multi-GB `install()` inside its own coroutine scope, which `dispose()` cancels
 * when the user leaves the Model Manager screen — killing an in-flight download.
 * Moving the transfer into a started foreground service (DATA_SYNC type) lets it
 * run independently of any ViewModel; it publishes live progress to the pure-Kotlin
 * [DownloadProgressBus], which the ViewModel observes (and re-attaches to on a fresh
 * VM, since the bus is a process-wide StateFlow).
 *
 * **Lifecycle.** Started-only (`onBind` = null) via [start]. On START it decodes the
 * [LocalBundle] (+ operator/delegate/origin/deviceId) from intent extras, becomes a
 * foreground service with a progress notification, runs the full
 * [LocalModelManager.install] flow on a service-owned IO scope, throttles progress to
 * whole-percent changes (the bus + notification update only on a percent tick), and on
 * the terminal result publishes SUCCESS or FAILED to the bus before leaving the
 * foreground + stopping. [START_NOT_STICKY]: a half-finished multi-GB download is NOT
 * silently auto-restarted by the OS — the user re-taps, and the `.part` lets it resume.
 *
 * Mirrors [LocalModelService]'s channel / notification / `startForeground` /
 * `stopForeground` idioms; it has no unit test (framework glue, exactly like
 * [LocalModelService]) — its `install()` orchestration is covered by
 * `LocalModelManagerTest`, the bus by `DownloadProgressBusTest`, and the ViewModel's
 * reaction by `LocalModelViewModelTest`.
 */
class ModelDownloadService : Service() {

    // Service-owned scope (NOT a ViewModel scope — this whole point is to OUTLIVE the
    // ViewModel). SupervisorJob so a child failure can't tear the scope down; the actual
    // work is on Dispatchers.IO. Cancelled in onDestroy.
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val bundleJson = intent?.getStringExtra(EXTRA_BUNDLE)
        if (bundleJson == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        val bundle = try {
            JSON.decodeFromString(LocalBundle.serializer(), bundleJson)
        } catch (e: Exception) {
            Log.w(TAG, "bad bundle extra (${e.javaClass.simpleName}); stopping")
            stopSelf()
            return START_NOT_STICKY
        }
        val operator = intent.getStringExtra(EXTRA_OPERATOR) ?: "system"
        val delegate = intent.getStringExtra(EXTRA_DELEGATE) ?: "cpu"
        val origin = intent.getStringExtra(EXTRA_ORIGIN) ?: ""
        val deviceId = intent.getStringExtra(EXTRA_DEVICE_ID) ?: ""

        // Become foreground IMMEDIATELY (startForegroundService contract: ~5s) and seed
        // the bus so the ViewModel reflects RUNNING even before the first byte arrives.
        startForegroundWith(buildNotification(bundle.displayName, 0, indeterminate = true))
        DownloadProgressBus.update(
            DownloadProgressBus.State(bundle.slug, 0f, DownloadProgressBus.Status.RUNNING),
        )

        scope.launch {
            // install() is NOT fully throw-safe: verify() reads the downloaded file and
            // can throw an IOException AFTER a successful transfer, and writeSidecar()/
            // mkdirs() can throw on a storage error. Wrap it so ANY throw becomes a
            // retryable FAILED — otherwise an uncaught throw would skip the terminal bus
            // update (stuck at RUNNING), skip stopForeground/stopSelf (leaked foreground
            // notification + a service that never stops), and leave the ViewModel's
            // busySlug set (the Model Manager row wedged on a spinner the user can't
            // retry). Mirrors LocalModelService's warm try/catch (graceful stop). The
            // terminal publish + stopForeground + stopSelf below run on BOTH paths.
            val result: Result<*> = try {
                val manager = LocalModelManager.fromContext(
                    applicationContext,
                    LocalModelApi(BlackBoxApi(origin)),
                    deviceId,
                )
                // Throttle: the real download fires onProgress on every ~64KB chunk
                // (thousands per multi-GB bundle). Publish to the bus + update the
                // notification only when the whole-percent value changes.
                // NOTE: this whole-percent throttle is duplicated in
                // LocalModelViewModelTest's `serviceSeam` (a faithful copy that must stay
                // in sync with this block).
                var lastPct = -1
                manager.install(bundle, operator, delegate) { soFar, total ->
                    val frac = if (total > 0) (soFar.toFloat() / total).coerceIn(0f, 1f) else -1f
                    val pct = if (frac < 0) 0 else (frac * 100).toInt()
                    if (pct != lastPct) {
                        lastPct = pct
                        DownloadProgressBus.update(
                            DownloadProgressBus.State(bundle.slug, frac, DownloadProgressBus.Status.RUNNING),
                        )
                        updateNotification(buildNotification(bundle.displayName, pct, indeterminate = frac < 0))
                    }
                }
            } catch (e: Throwable) {
                Result.failure<Any>(e)
            }

            DownloadProgressBus.update(
                if (result.isSuccess) {
                    DownloadProgressBus.State(bundle.slug, 1f, DownloadProgressBus.Status.SUCCESS)
                } else {
                    DownloadProgressBus.State(
                        bundle.slug,
                        0f,
                        DownloadProgressBus.Status.FAILED,
                        result.exceptionOrNull()?.message ?: "download failed",
                    )
                },
            )
            stopForegroundCompat()
            stopSelf()
        }

        // A download is NOT auto-restarted on an OS kill; the user re-taps (resume via .part).
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun startForegroundWith(notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ServiceCompat.startForeground(
                this,
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun updateNotification(notification: Notification) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr?.notify(NOTIFICATION_ID, notification)
    }

    private fun stopForegroundCompat() {
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Model Download",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Downloads the on-device AI model in the background"
                setShowBadge(false)
            }
            val mgr = getSystemService(NotificationManager::class.java)
            mgr?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(modelName: String, pct: Int, indeterminate: Boolean): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, NativeMainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_IMMUTABLE,
        )
        val title = if (modelName.isBlank()) "Downloading on-device model" else "Downloading $modelName"
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(if (indeterminate) "Starting…" else "$pct%")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setProgress(100, pct, indeterminate)
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .build()
    }

    companion object {
        private const val TAG = "ModelDownloadService"
        const val CHANNEL_ID = "blackbox_model_download"
        const val NOTIFICATION_ID = 9094

        // The bundle crosses the Intent boundary as JSON (LocalBundle is @Serializable);
        // ignoreUnknownKeys keeps it forward-compatible with future catalog fields.
        private val JSON = Json { ignoreUnknownKeys = true }

        const val EXTRA_BUNDLE = "extra_bundle_json"
        const val EXTRA_OPERATOR = "extra_operator"
        const val EXTRA_DELEGATE = "extra_delegate"
        const val EXTRA_ORIGIN = "extra_origin"
        const val EXTRA_DEVICE_ID = "extra_device_id"

        /**
         * Best-effort START of the durable download. NEVER throws into the caller: a
         * platform refusal (background-start limits, etc.) is swallowed + logged, and
         * a FAILED state is published to the [DownloadProgressBus] so the ViewModel
         * clears its busy state instead of hanging on a download that never began.
         */
        @JvmStatic
        fun start(
            context: Context,
            bundle: LocalBundle,
            operator: String,
            delegate: String,
            origin: String,
            deviceId: String,
        ) {
            val intent = Intent(context, ModelDownloadService::class.java).apply {
                putExtra(EXTRA_BUNDLE, JSON.encodeToString(LocalBundle.serializer(), bundle))
                putExtra(EXTRA_OPERATOR, operator)
                putExtra(EXTRA_DELEGATE, delegate)
                putExtra(EXTRA_ORIGIN, origin)
                putExtra(EXTRA_DEVICE_ID, deviceId)
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Throwable) {
                Log.w(TAG, "start refused (${e.javaClass.simpleName})")
                DownloadProgressBus.update(
                    DownloadProgressBus.State(
                        bundle.slug,
                        0f,
                        DownloadProgressBus.Status.FAILED,
                        "couldn't start download service",
                    ),
                )
            }
        }
    }
}
