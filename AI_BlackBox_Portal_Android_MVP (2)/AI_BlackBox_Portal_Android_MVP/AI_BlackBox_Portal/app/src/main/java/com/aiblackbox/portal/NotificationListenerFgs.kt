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
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import com.aiblackbox.portal.data.remote.Notifier
import com.aiblackbox.portal.data.remote.ObservationBuilder
import com.aiblackbox.portal.data.remote.PhoneActionDispatcher
import com.aiblackbox.portal.data.remote.REMOTE_CONTROL_PORT
import com.aiblackbox.portal.data.remote.RemoteActionDispatcher
import com.aiblackbox.portal.data.remote.RemoteControlServer
import com.aiblackbox.portal.data.remote.RemoteSessionBus
import com.aiblackbox.portal.data.remote.RemoteTaskHandlerHolder
import com.aiblackbox.portal.data.local.AutonomyStore
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.NotificationSubscriptionStore
import com.aiblackbox.portal.overlay.AndroidPhoneController
import com.aiblackbox.portal.overlay.DeviceCapabilities
import com.aiblackbox.portal.overlay.OverlayConfirmUi
import com.aiblackbox.portal.overlay.OverlayCredentialHandoff
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking

/**
 * Foreground service that hosts the inbound remote-control listener
 * ([RemoteControlServer]) on [REMOTE_CONTROL_PORT] — the SINGLE owner of that socket
 * (MN.4). It exists so the device can RECEIVE a server push and post a REAL system
 * notification **deterministically, with NO model/LLM in the path**, even when the app
 * is backgrounded or closed, and (via [BootReceiver]'s direct start in the exempt
 * BOOT_COMPLETED receiver context, MN.5) surviving a reboot.
 *
 * **Why a NEW service (decoupling from Gemma).** The listener used to be hosted by
 * [LocalModelService] and was gated behind a Gemma-backed
 * [com.aiblackbox.portal.data.remote.remoteTaskHandlerFactory]; a device with no model
 * installed therefore had NO listener and could not receive `/notify`. This service
 * hosts the listener INDEPENDENT of the Gemma engine: it starts with no model present
 * and never consults that gate.
 *
 * **Single binding on the control port.** [REMOTE_CONTROL_PORT] (8765) can be bound by
 * exactly ONE server. This FGS owns it. `control_phone`'s `/task` + `/status` (which
 * need the Gemma task handler) still work: [LocalModelService] publishes its live
 * [com.aiblackbox.portal.data.remote.RemoteTaskRunner] into
 * [RemoteTaskHandlerHolder], which this server reads PER REQUEST via its
 * `handlerProvider`. When no model service is hosting, the holder yields
 * [com.aiblackbox.portal.data.remote.NoopRemoteTaskHandler] — `/healthz` reports
 * not-ready and `/notify` keeps working. So the model service no longer binds the
 * socket at all; it only injects (and clears) the task handler. No socket rebind ever
 * happens on a model load/unload.
 *
 * **MODEL-FREE notification path.** `/notify` invokes [Notifier], wired directly to
 * [BlackBoxNotificationManager.showTaskNotification] (a plain
 * NotificationManagerCompat.notify). No Gemma, no network round-trip, no LLM.
 *
 * **Idempotent retries.** The bus's `notif_id` is mapped to a stable (tag, id) so a
 * retried push COLLAPSES onto the same notification instead of stacking duplicates.
 *
 * **Lifecycle.** START (or a bare/sticky start): become a foreground service within
 * the ~5s contract with a low-importance ongoing notification, then bind the listener
 * (best-effort; a bind failure leaves the FGS up so a later start can retry).
 * `connectedDevice` FGS type (mirroring [LocalModelService] / [TerminalForegroundService])
 * dodges the dataSync runtime timeout. START_STICKY so the OS re-creates us after a
 * kill — and a null redelivery simply re-binds the (model-free) listener.
 *
 * **Best-effort + graceful.** Every start/bind is wrapped so a platform refusal can
 * never crash; the worst case is "notifications not received while backgrounded",
 * never an app crash.
 */
class NotificationListenerFgs : Service() {

    private var server: RemoteControlServer? = null

    /**
     * (M1.3) The actuator seam POST /action dispatches through — built lazily so it exists
     * only when the listener actually binds. Wraps [AndroidPhoneController.fromService]
     * (which degrades gracefully to "accessibility service not enabled" when a11y is off,
     * so it is safe to always wire) with the live [DeviceCapabilities] for the coordinate
     * gate.
     *
     * (M4) SAFETY & AUTONOMY on the boot-survivable REMOTE `/action` path: the controller is
     * wired with the REAL smart gates rather than the M1 blanket-deny stopgap. Autonomy comes
     * from the target device's per-device [AutonomyStore] (default [AutonomyMode.PERMISSION] —
     * SAFE; read fresh per dispatch so the latest user setting applies). A high-consequence
     * action (send/pay/delete/post; send_email/send_sms/send_intent) surfaces the real
     * [OverlayConfirmUi] — a SYSTEM-overlay Allow/Deny prompt ON THIS device — which fails-safe
     * to DENY on timeout OR when the overlay permission is missing (never silently allows). A
     * password/payment field routes to [OverlayCredentialHandoff] (the user types the secret
     * directly; the model's text is discarded). Benign navigation/typing/open_app/scroll never
     * gate. In YOLO mode the owner has opted into unattended high-consequence actions.
     *
     * Built lazily so it exists only when the listener actually binds; [AndroidPhoneController]
     * degrades gracefully to "accessibility service not enabled" when a11y is off, so it is
     * safe to always wire.
     */
    private val actionDispatcher: RemoteActionDispatcher by lazy {
        val appContext = applicationContext
        val autonomy = AutonomyStore.fromContext(appContext)
        PhoneActionDispatcher(
            controller = AndroidPhoneController.fromService(
                appContext,
                mode = { autonomy.load() },
                confirm = OverlayConfirmUi(appContext),
                credentialHandoff = OverlayCredentialHandoff(appContext),
            ),
            capability = { DeviceCapabilities.detect(appContext) },
            // observationProvider intentionally null on the /action follow-on: /stream is the
            // canonical observation path (M0 README decision 2) — avoids a double-observe race.
        )
    }

    /**
     * (M1.4) Fail-safe kill switch: a bus listener that posts/cancels the ongoing
     * "AI is controlling this device" STOP notification. This is the notification-based
     * stop that works even when the overlay (banner) permission is missing. The richer
     * overlay banner ([OverlayService]) observes the SAME bus when it is running.
     */
    private val sessionListener = RemoteSessionBus.Listener { session ->
        if (session != null) postControlBanner() else cancelControlBanner()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        createControlNotificationChannel()
        RemoteSessionBus.addListener(sessionListener)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopListener()
                _isRunning = false
                stopForegroundCompat()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_STOP_CONTROL -> {
                // (M1.4) The kill switch fired (STOP action on the control banner
                // notification). Abort the in-flight session; the bus listener cancels the
                // banner. Keep the FGS + socket alive so the device stays reachable.
                RemoteSessionBus.stop()
                return START_STICKY
            }
            else -> {
                // ACTION_START, a bare start, OR a null-intent START_STICKY redelivery:
                // become foreground IMMEDIATELY (contract) then (re)bind the listener.
                startForegroundWith(buildNotification())
                _isRunning = true
                startListenerIfNeeded()
            }
        }
        // START_STICKY: keep the model-free listener alive across OS kills so the phone
        // stays a reachable notification target.
        return START_STICKY
    }

    /**
     * Bind the single [RemoteControlServer] on [REMOTE_CONTROL_PORT], model-free.
     * Idempotent (a second start while bound is a no-op). The Gemma task handler is read
     * per request from [RemoteTaskHandlerHolder]; the `/notify` poster is wired to
     * [BlackBoxNotificationManager]; the subscription allow-list re-check reads the
     * device-local [NotificationSubscriptionStore]. NEVER throws into the service.
     */
    private fun startListenerIfNeeded() {
        if (server != null) return
        val appContext = applicationContext
        val notificationManager = BlackBoxNotificationManager(appContext)
        val subscriptionStore = NotificationSubscriptionStore(appContext)
        val notifier = Notifier { title, body, category, operator, notifId ->
            postSystemNotification(notificationManager, title, body, category, operator, notifId)
        }
        runCatching {
            RemoteControlServer(
                port = REMOTE_CONTROL_PORT,
                handlerProvider = { RemoteTaskHandlerHolder.current() },
                notifier = notifier,
                operatorProvider = { boundOperator(appContext) },
                subscriptionPredicate = { op -> subscriptionStore.isSubscribed(op) },
                // (M1.3) POST /action → the live actuators; (M1.2) GET /stream emits one
                // real observation. Both degrade gracefully when a11y is off.
                actionDispatcherProvider = { actionDispatcher },
                observationProvider = { ObservationBuilder.fromDevice(appContext).build() },
            ).also {
                it.startServer()
                server = it
            }
            Log.d(TAG, "notification/control listener started on :$REMOTE_CONTROL_PORT")
        }.onFailure {
            Log.w(TAG, "listener start refused (${it.javaClass.simpleName}); FGS stays up to retry")
            server = null
        }
    }

    /** Best-effort stop of the listener socket. Never throws. */
    private fun stopListener() {
        runCatching { server?.stopServer() }
        server = null
    }

    /**
     * Post a REAL system notification (MODEL-FREE) for an inbound `/notify`. Empty body
     * → show title + category only (metadata-only cross-operator push). The bus's
     * `notif_id` maps to a stable (tag, id) so retries COLLAPSE via the (tag, id) overload
     * instead of stacking. Runs on the listener's worker thread; never throws back into it.
     */
    private fun postSystemNotification(
        manager: BlackBoxNotificationManager,
        title: String,
        body: String,
        category: String,
        operator: String,
        notifId: String,
    ) {
        runCatching {
            // Render: prefer the server-supplied title; if absent, fall back to category
            // (so a metadata-only push still has a heading). Body may be empty — in that
            // case surface the category as the line (or nothing) rather than an empty text.
            val shownTitle = title.ifBlank { category.ifBlank { "Notification" } }
            val shownBody = when {
                body.isNotBlank() -> body
                category.isNotBlank() && title.isNotBlank() -> category
                else -> ""
            }
            manager.showRemoteNotification(
                title = shownTitle,
                body = shownBody,
                operator = operator.ifBlank { null },
                category = category.ifBlank { null },
                notifId = notifId.ifBlank { null },
            )
            Log.d(TAG, "posted system notification (notifId=${notifId.ifBlank { "<none>" }})")
        }.onFailure {
            Log.w(TAG, "failed to post notification (${it.javaClass.simpleName})")
        }
    }

    /** The device's bound operator (BlackBoxStore), or "" — the scope POST /task is
     *  authorized against. Read per request on the listener's worker thread. */
    private fun boundOperator(appContext: Context): String =
        runCatching { runBlocking { BlackBoxStore(appContext).operator.first() } }.getOrDefault("")

    override fun onDestroy() {
        _isRunning = false
        stopListener()
        RemoteSessionBus.removeListener(sessionListener)
        runCatching { cancelControlBanner() }
        Log.d(TAG, "stopped; control listener released")
        super.onDestroy()
    }

    /**
     * (M1.4) The high-visibility "AI is controlling this device" ongoing notification with
     * a STOP action — the fail-safe kill switch that works with NO overlay permission. STOP
     * routes back to this FGS ([ACTION_STOP_CONTROL]) → [RemoteSessionBus.stop]. Best-effort;
     * never throws (a notifications-disabled device still has the in-app overlay banner /
     * server-side kill).
     */
    private fun postControlBanner() {
        runCatching {
            val stopIntent = Intent(this, NotificationListenerFgs::class.java).apply {
                action = ACTION_STOP_CONTROL
            }
            val stopPending = PendingIntent.getService(
                this, 1, stopIntent,
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
            val notification = NotificationCompat.Builder(this, CONTROL_CHANNEL_ID)
                .setContentTitle("AI is controlling this device")
                .setContentText("Tap STOP to end remote control immediately.")
                .setSmallIcon(android.R.drawable.ic_menu_view)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_STATUS)
                .addAction(android.R.drawable.ic_menu_close_clear_cancel, "STOP", stopPending)
                .build()
            NotificationManagerCompat.from(this).notify(CONTROL_NOTIFICATION_ID, notification)
        }.onFailure {
            Log.w(TAG, "control banner notification failed (${it.javaClass.simpleName})")
        }
    }

    /** Cancel the control banner notification when the session ends / is killed. */
    private fun cancelControlBanner() {
        runCatching { NotificationManagerCompat.from(this).cancel(CONTROL_NOTIFICATION_ID) }
    }

    private fun createControlNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CONTROL_CHANNEL_ID,
                "Remote Control Active",
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = "Shown while an AI session is controlling this device; carries the STOP kill switch"
                setShowBadge(true)
            }
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        }
    }

    private fun startForegroundWith(notification: Notification) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceCompat.startForeground(
                    this,
                    NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
                )
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
        } catch (t: Throwable) {
            Log.w(TAG, "startForeground refused (${t.javaClass.simpleName}); not foregrounded", t)
        }
    }

    private fun stopForegroundCompat() {
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Remote Notifications",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Keeps the device reachable for BlackBox notifications in the background"
                setShowBadge(false)
            }
            val mgr = getSystemService(NotificationManager::class.java)
            mgr?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): Notification {
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
            .setContentText("Listening for notifications")
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .build()
    }

    companion object {
        private const val TAG = "NotifListenerFgs"

        /** Distinct channel + notification id from the other FGSs (9093 / 9094). */
        const val CHANNEL_ID = "blackbox_remote_notify"
        const val NOTIFICATION_ID = 9095
        const val ACTION_START = "com.aiblackbox.portal.START_NOTIFY_LISTENER"
        const val ACTION_STOP = "com.aiblackbox.portal.STOP_NOTIFY_LISTENER"

        /** (M1.4) The consent-banner kill-switch: distinct channel + id + STOP action. */
        const val CONTROL_CHANNEL_ID = "blackbox_remote_control_active"
        const val CONTROL_NOTIFICATION_ID = 9096
        const val ACTION_STOP_CONTROL = "com.aiblackbox.portal.STOP_REMOTE_CONTROL"

        @Volatile
        private var _isRunning = false
        fun isRunning() = _isRunning

        /**
         * Best-effort START of the model-free listener FGS. NEVER throws into the caller:
         * a platform refusal (background-start limits) is swallowed + logged. Call on app
         * launch and from the boot worker so the device is always a reachable notification
         * target.
         */
        @JvmStatic
        fun start(context: Context) {
            val intent = Intent(context, NotificationListenerFgs::class.java).apply {
                action = ACTION_START
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Throwable) {
                Log.w(TAG, "start refused (${e.javaClass.simpleName})")
            }
        }

        /** Best-effort STOP. Never throws. */
        @JvmStatic
        fun stop(context: Context) {
            val intent = Intent(context, NotificationListenerFgs::class.java).apply {
                action = ACTION_STOP
            }
            runCatching { context.startService(intent) }
        }
    }
}
