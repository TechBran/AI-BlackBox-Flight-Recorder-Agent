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
import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.local.LiteRtEngine
import com.aiblackbox.portal.data.local.LocalEngineHolder
import com.aiblackbox.portal.data.local.LocalModelManager
import com.aiblackbox.portal.data.local.SamplerSettings
import com.aiblackbox.portal.data.local.shouldWarm
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.remote.REMOTE_CONTROL_PORT
import com.aiblackbox.portal.data.remote.RemoteControlServer
import com.aiblackbox.portal.data.remote.remoteTaskHandlerFactory
import com.aiblackbox.portal.data.store.BlackBoxStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Foreground service that PINS the on-device Gemma engine in RAM (Task R2-C).
 *
 * **Why.** The on-device model's cold load is ~10-75s. The engine used to be owned
 * by [com.aiblackbox.portal.ui.chat.ChatViewModel] and died with the ViewModel /
 * process, so each fresh VM paid the cold load again. This service warm-loads the
 * engine ONCE, stores it in the PROCESS-level [LocalEngineHolder], and stays in the
 * foreground so Android does not reclaim the process while backgrounded -- the model
 * stays resident and the next turn (incl. the model-as-a-tool path) is instant.
 *
 * **Lifecycle.**
 *  - START ([ACTION_START], or a bare start): immediately becomes a foreground
 *    service with a "preparing" notification (Android requires startForeground
 *    within ~5s of startForegroundService), then on [Dispatchers.IO] resolves the
 *    ACTIVE installed bundle (the `model_local` slug pref, falling back to the
 *    alphabetically-first installed bundle), builds the engine via
 *    [LiteRtEngine.fromInstalled], `load()`s it (the ~10-75s warm), stores it in
 *    [LocalEngineHolder], and updates the notification to "On-device model ready".
 *  - STOP ([ACTION_STOP]) / onDestroy: [LocalEngineHolder.clearAndClose] releases
 *    the native engine and the service leaves the foreground.
 *
 * **Graceful + additive (the R2-C safety guarantee).** EVERY failure mode here is a
 * silent no-op for the chat: no installed model, a resolve failure, or a load throw
 * just stops the service and leaves [LocalEngineHolder] empty, so the ViewModel
 * builds + uses its OWN engine exactly as it did before R2-C (see
 * [com.aiblackbox.portal.data.local.engineSourceFor]). The worst case is "no
 * startup-latency win", never a broken chat. The service never re-throws into the
 * framework. It is also idempotent: a second start while a warm is in flight is
 * ignored; [LiteRtEngine.load] is itself Mutex-idempotent, so even a racing VM-side
 * load can't double-initialize.
 *
 * Started by [com.aiblackbox.portal.ui.chat.ChatViewModel.preloadLocalEngine] when
 * the LOCAL provider becomes the active one (mirroring the W1 warm trigger).
 */
class LocalModelService : Service() {

    // Service-owned scope for the warm load (NOT viewModelScope — this outlives any
    // VM). SupervisorJob so one failed child can't tear the scope down; cancelled in
    // onDestroy. The warm runs on Dispatchers.IO via withContext inside the launch.
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    // Guards against a second concurrent warm if START is delivered twice. Volatile:
    // read/written only on Main (onStartCommand + the launch's Main continuation).
    @Volatile
    private var warmJob: Job? = null

    // The inbound remote-control listener (control_phone). Started alongside the
    // foreground service when a task handler is registered; stopped on STOP/destroy.
    private var remoteServer: RemoteControlServer? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopRemoteControlServer()
                stopForegroundCompat()
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                // Become foreground IMMEDIATELY (startForegroundService contract).
                startForegroundWith(buildNotification(TEXT_PREPARING))
                _isRunning = true
                startWarmIfNeeded()
                startRemoteControlServerIfPossible()
            }
        }
        // START_STICKY: if the OS kills us under memory pressure, re-deliver a null
        // intent so we re-warm (the holder was lost with the process anyway).
        return START_STICKY
    }

    /**
     * Resolve the active bundle, build + warm-load the engine, store it in the
     * process holder, and update the notification — all best-effort. Any failure
     * stops the service WITHOUT crashing (the VM falls back to its own engine).
     * Idempotent: a second call while a warm is in flight is ignored.
     */
    private fun startWarmIfNeeded() {
        if (warmJob?.isActive == true) return // a warm is already running
        warmJob = scope.launch {
            try {
                val resolved = withContext(Dispatchers.IO) { resolveActiveBundle() }
                if (resolved == null) {
                    // No installed model (or nothing resolvable) — nothing to pin.
                    Log.d(TAG, "no installed on-device model to pin; stopping")
                    stopSelfGracefully()
                    return@launch
                }
                val (engine, modelPath, delegate) = resolved
                // IDEMPOTENT WARM: start() fires on every provider toggle / model
                // switch. If the holder ALREADY holds an engine for this exact bundle,
                // it is pinned + warm — skip build/load/set entirely. Otherwise the
                // set() below would close the live engine the consumer borrowed
                // (localEngineFromHolder=true), forcing the ~10-75s cold reload R2-C
                // prevents and leaking the superseded engine. We only build + set when
                // the holder is empty OR holds a DIFFERENT model (a real switch, where
                // closing the superseded engine in set() is correct).
                if (!shouldWarm(
                        holderHasEngine = LocalEngineHolder.getOrNull() != null,
                        holderModelPath = LocalEngineHolder.modelPath,
                        targetModelPath = modelPath,
                    )
                ) {
                    updateNotification(buildNotification(TEXT_READY))
                    Log.d(TAG, "on-device model already pinned for this bundle; warm skipped")
                    return@launch
                }
                withContext(Dispatchers.IO) { engine.load(File(modelPath), delegate) }
                // Hand the WARM engine to the process holder (service owns it now).
                LocalEngineHolder.set(engine, modelPath, delegate)
                updateNotification(buildNotification(TEXT_READY))
                Log.d(TAG, "on-device model pinned + ready (process-resident)")
            } catch (e: Throwable) {
                // GRACEFUL: a warm failure must never crash; leave the holder empty
                // so the ViewModel builds its own engine. Log the CLASS name only
                // (never a message — it could carry a device/path detail).
                Log.w(TAG, "warm-load failed (${e.javaClass.simpleName}); VM fallback still works")
                runCatching { LocalEngineHolder.clearAndClose() }
                stopSelfGracefully()
            }
        }
    }

    /**
     * Resolve the ACTIVE installed bundle and build (NOT yet load) its engine on IO.
     * Mirrors [com.aiblackbox.portal.ui.chat.ChatViewModel.localProviderOrWire]'s
     * resolution: honor the `model_local` slug pref, else the alphabetically-first
     * installed bundle. Returns (engine, absoluteModelPath, delegate) or null when
     * there is no installed model. [installedModels] is hermetic (never touches the
     * network), so the [LocalModelDownloader] passed to the manager is a no-op stub
     * — the service has no hub origin and needs none for a disk scan.
     */
    private suspend fun resolveActiveBundle(): Triple<LiteRtEngine, String, String>? {
        val activeSlug = runCatching {
            BlackBoxStore(applicationContext).getString(PREF_ACTIVE_LOCAL_MODEL).first()
        }.getOrDefault("")
        val manager = LocalModelManager.fromContext(
            applicationContext,
            NoopDownloader,
            deviceId = "android-device",
        )
        val installed = runCatching { manager.installedModels() }.getOrDefault(emptyList())
        val bundle = installed.firstOrNull { it.slug == activeSlug }
            ?: installed.firstOrNull()
            ?: return null
        val cfg = bundle.config
        val delegate = "cpu"
        val engine = LiteRtEngine.fromInstalled(
            applicationContext,
            bundle.file,
            delegate = delegate,
            maxTokens = cfg.maxTokens ?: LiteRtEngine.DEFAULT_MAX_TOKENS,
            sampler = SamplerSettings(
                topK = cfg.topK,
                topP = cfg.topP,
                temperature = cfg.temperature,
            ),
            supportImage = cfg.supportImage,
        )
        return Triple(engine, bundle.file.absolutePath, delegate)
    }

    /** Leave the foreground + stop, keeping any (empty) holder state. */
    private fun stopSelfGracefully() {
        stopForegroundCompat()
        stopSelf()
    }

    /**
     * Best-effort start of the inbound remote-control listener (control_phone). A
     * no-op (logged) unless a [remoteTaskHandlerFactory] is registered (Task 6) — a
     * listener with nothing safe to run stays OFF. NEVER throws into the service: a
     * bind failure leaves remoteServer null and the rest of the service intact.
     */
    private fun startRemoteControlServerIfPossible() {
        if (remoteServer != null) return
        val factory = remoteTaskHandlerFactory
        if (factory == null) {
            Log.d(TAG, "no remote task handler registered; inbound control listener stays off")
            return
        }
        runCatching {
            RemoteControlServer(REMOTE_CONTROL_PORT, factory(applicationContext)).also {
                it.startServer()
                remoteServer = it
            }
            Log.d(TAG, "remote control listener started on :$REMOTE_CONTROL_PORT")
        }.onFailure {
            Log.w(TAG, "remote control listener start refused (${it.javaClass.simpleName})")
            remoteServer = null
        }
    }

    /** Best-effort stop of the inbound listener. Never throws. */
    private fun stopRemoteControlServer() {
        runCatching { remoteServer?.stopServer() }
        remoteServer = null
    }

    override fun onDestroy() {
        _isRunning = false
        stopRemoteControlServer()
        // Release the pinned engine — the service owns it, so its lifecycle ends here.
        runCatching { LocalEngineHolder.clearAndClose() }
        scope.cancel()
        Log.d(TAG, "stopped; on-device engine released")
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
                "On-Device Model",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Keeps the on-device AI model loaded for instant replies"
                setShowBadge(false)
            }
            val mgr = getSystemService(NotificationManager::class.java)
            mgr?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, NativeMainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("AI BlackBox")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .build()
    }

    /**
     * No-op [LocalModelDownloader] for the disk-only [installedModels] scan. Its
     * methods are never reached by the scan (verified against LocalModelManager); a
     * defensive call returns an empty/false result rather than throwing.
     */
    private object NoopDownloader : LocalModelDownloader {
        override suspend fun download(
            slug: String,
            destFile: File,
            onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
        ): Result<File> = Result.failure(UnsupportedOperationException("download not supported in LocalModelService"))

        override suspend fun attest(req: AttestRequest): Boolean = false
    }

    companion object {
        private const val TAG = "LocalModelService"
        const val CHANNEL_ID = "blackbox_local_model"
        const val NOTIFICATION_ID = 9093
        const val ACTION_START = "com.aiblackbox.portal.START_LOCAL_MODEL"
        const val ACTION_STOP = "com.aiblackbox.portal.STOP_LOCAL_MODEL"

        // The DataStore key the Model Manager persists the active on-device slug under
        // (mirrors ChatViewModel's "model_local"). Read here so the pinned engine is
        // the SAME bundle the chat will use.
        private const val PREF_ACTIVE_LOCAL_MODEL = "model_local"

        private const val TEXT_PREPARING = "Loading on-device model…"
        private const val TEXT_READY = "On-device model ready"

        private var _isRunning = false
        fun isRunning() = _isRunning

        /**
         * Best-effort START of the pinning service. NEVER throws into the caller: a
         * platform refusal (background-start limits, etc.) is swallowed and logged,
         * leaving the ViewModel's own-engine fallback intact (the R2-C guarantee).
         * Called when the LOCAL provider becomes active.
         */
        @JvmStatic
        fun start(context: Context) {
            val intent = Intent(context, LocalModelService::class.java).apply {
                action = ACTION_START
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Throwable) {
                Log.w(TAG, "start refused (${e.javaClass.simpleName}); VM fallback still works")
            }
        }

        /** Best-effort STOP (e.g. the local provider is no longer active). Never throws. */
        @JvmStatic
        fun stop(context: Context) {
            val intent = Intent(context, LocalModelService::class.java).apply {
                action = ACTION_STOP
            }
            runCatching { context.startService(intent) }
        }
    }
}
