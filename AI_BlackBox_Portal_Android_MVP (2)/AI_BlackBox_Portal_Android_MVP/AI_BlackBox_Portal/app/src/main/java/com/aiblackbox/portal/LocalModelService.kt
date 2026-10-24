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
import com.aiblackbox.portal.data.local.LocalEngineHolder
import com.aiblackbox.portal.data.local.pinWarmEngine
import com.aiblackbox.portal.data.local.resolveActiveEngine
import com.aiblackbox.portal.data.local.shouldWarm
import com.aiblackbox.portal.data.remote.RemoteTaskHandlerHolder
import com.aiblackbox.portal.data.remote.remoteTaskHandlerFactory
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

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
    // onDestroy. The disk scan and the load run on Dispatchers.IO inside pinWarmEngine.
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    // Guards against a second concurrent warm if START is delivered twice. Volatile:
    // read/written only on Main (onStartCommand + the launch's Main continuation).
    @Volatile
    private var warmJob: Job? = null

    // The Gemma-backed remote task handler (control_phone /task + /status). Published
    // into the process-level RemoteTaskHandlerHolder so the SINGLE control-port socket
    // — owned by NotificationListenerFgs, NOT this service (MN.4) — can dispatch /task
    // through it when a model is resident, then cleared on STOP/destroy so /task falls
    // back to the no-op. This service no longer binds the socket itself.
    private var remoteTaskHandler: com.aiblackbox.portal.data.remote.RemoteTaskHandler? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                clearRemoteTaskHandler()
                stopForegroundCompat()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_START -> {
                // EXPLICIT warm request (the chat path pins the engine while the app is
                // open). Become foreground IMMEDIATELY (startForegroundService contract),
                // warm the engine, and host the listener.
                if (!startForegroundWith(buildNotification(TEXT_PREPARING))) return refusedStart()
                _isRunning = true
                startWarmIfNeeded()
                publishRemoteTaskHandler()
            }
            else -> {
                // ACTION_START_LISTENER, OR a NULL-intent START_STICKY REDELIVERY after the
                // OS killed us. BOTH restart LISTENER-ONLY (no eager warm). This is the fix
                // for the cold-warm crash-LOOP: previously a null redelivery fell through to
                // startWarmIfNeeded(), so an OOM-killed warm respawned straight back into
                // another 3.66GB GPU load that OOM'd again — an unbounded restart/thrash
                // loop (the "phone goes crazy" symptom). control_phone wakes the engine on
                // demand and the chat path re-warms via an explicit ACTION_START, so the
                // safe restart posture is just the lightweight listener.
                if (!startForegroundWith(buildNotification(TEXT_LISTENER))) return refusedStart()
                _isRunning = true
                publishRemoteTaskHandler()
            }
        }
        // START_STICKY: keep the cheap LISTENER alive across OS kills so the phone stays a
        // reachable control target. A null redelivery lands in the else branch above =
        // listener-only, NEVER a re-warm (that loop is exactly what we just removed).
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
                val resolved = resolveActiveEngine(applicationContext)
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
                // The ONE process warm (audit rank 63): under the shared warm lock it
                // publishes the warming marker the chat waits on, brackets the load with
                // the crash-loop flag (a SIGKILL mid-load leaves it set, so the next
                // launch skips its auto-warm), pins the engine in the holder, and closes
                // it if the load fails. A chat or control_phone warm that is already
                // loading this bundle is reused instead of loaded twice.
                pinWarmEngine(applicationContext, engine, modelPath, delegate)
                updateNotification(buildNotification(TEXT_READY))
                Log.d(TAG, "on-device model pinned + ready (process-resident)")
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Throwable) {
                // GRACEFUL: a warm failure must never crash. pinWarmEngine already
                // closed the engine it could not load and cleared the marker and the
                // crash-loop flag; an engine another warmer pinned is left alone. Log the
                // CLASS name only (never a message — it could carry a device/path detail).
                Log.w(TAG, "warm-load failed (${e.javaClass.simpleName}); VM fallback still works")
                stopSelfGracefully()
            }
        }
    }

    /** Leave the foreground + stop, keeping any (empty) holder state. */
    private fun stopSelfGracefully() {
        stopForegroundCompat()
        stopSelf()
    }

    /**
     * Publish the Gemma-backed remote task handler so the SINGLE control-port socket —
     * owned by [NotificationListenerFgs] (MN.4), NOT this service — can dispatch
     * control_phone `/task` + `/status` through it, and ensure that listener FGS is up.
     *
     * A no-op (logged) unless a [remoteTaskHandlerFactory] is registered (Task 6) — there
     * is nothing safe to run otherwise. This service NO LONGER binds the socket itself;
     * it only injects the handler into [RemoteTaskHandlerHolder]. NEVER throws into the
     * service. Notifications (`/notify`) work model-free via the FGS regardless of this.
     */
    private fun publishRemoteTaskHandler() {
        // Make sure the model-free listener FGS owns the socket (idempotent, best-effort).
        runCatching { NotificationListenerFgs.start(applicationContext) }
        if (remoteTaskHandler != null) return
        val factory = remoteTaskHandlerFactory
        if (factory == null) {
            Log.d(TAG, "no remote task handler registered; control_phone /task stays no-op")
            return
        }
        runCatching {
            val handler = factory(applicationContext)
            RemoteTaskHandlerHolder.set(handler)
            remoteTaskHandler = handler
            Log.d(TAG, "remote task handler published; control_phone /task active")
        }.onFailure {
            Log.w(TAG, "remote task handler publish refused (${it.javaClass.simpleName})")
            remoteTaskHandler = null
        }
    }

    /** Best-effort clear of the published handler (control_phone /task falls back to the
     *  no-op; the FGS keeps the socket + /notify alive). Never throws. */
    private fun clearRemoteTaskHandler() {
        runCatching { RemoteTaskHandlerHolder.clear() }
        remoteTaskHandler = null
    }

    override fun onDestroy() {
        _isRunning = false
        clearRemoteTaskHandler()
        // Release the pinned engine — the service owns it, so its lifecycle ends here.
        runCatching { LocalEngineHolder.clearAndClose() }
        scope.cancel()
        Log.d(TAG, "stopped; on-device engine released")
        super.onDestroy()
    }

    /**
     * Guarded startForeground (A01-activities-root-09). START_STICKY means the OS
     * re-creates this service in the background after an OOM kill, where Android 12+
     * refuses a foreground start; uncaught, that refusal crashed the process in a loop.
     */
    private fun startForegroundWith(notification: Notification): Boolean =
        FgsSupport.startForegroundSafely(
            this, NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE, TAG,
        )

    /** A refused foreground start: stop rather than run on as an unprotected sticky service. */
    private fun refusedStart(): Int {
        _isRunning = false
        clearRemoteTaskHandler()
        stopSelf()
        return START_NOT_STICKY
    }

    // connectedDevice has no time limit today; stop cleanly if the platform ever imposes one.
    override fun onTimeout(startId: Int, fgsType: Int) {
        _isRunning = false
        clearRemoteTaskHandler()
        FgsSupport.stopOnTimeout(this, TAG, fgsType)
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

    companion object {
        private const val TAG = "LocalModelService"
        const val CHANNEL_ID = "blackbox_local_model"
        const val NOTIFICATION_ID = NotificationIds.LOCAL_MODEL
        const val ACTION_START = "com.aiblackbox.portal.START_LOCAL_MODEL"
        const val ACTION_START_LISTENER = "com.aiblackbox.portal.START_LISTENER"
        const val ACTION_STOP = "com.aiblackbox.portal.STOP_LOCAL_MODEL"

        private const val TEXT_PREPARING = "Loading on-device model…"
        private const val TEXT_READY = "On-device model ready"
        private const val TEXT_LISTENER = "Remote control ready"

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

        /**
         * Best-effort start of the inbound control_phone LISTENER ONLY (no engine warm).
         * Call on app launch so the phone is a reachable remote-control target regardless
         * of the active chat provider (control_phone wakes the engine on demand). Never throws.
         */
        @JvmStatic
        fun startListener(context: Context) {
            val intent = Intent(context, LocalModelService::class.java).apply {
                action = ACTION_START_LISTENER
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Throwable) {
                Log.w(TAG, "startListener refused (${e.javaClass.simpleName})")
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
