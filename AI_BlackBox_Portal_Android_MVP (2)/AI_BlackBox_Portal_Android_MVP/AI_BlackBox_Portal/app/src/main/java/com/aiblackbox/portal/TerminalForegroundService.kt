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
import com.aiblackbox.portal.ui.cli_agent.TerminalSessionManager

/**
 * Foreground service that keeps the app process WARM while backgrounded so the
 * live terminal WebSockets owned by [TerminalSessionManager] are not reclaimed
 * (Phase 3 of the terminal-session-persistence work -- see
 * docs/plans/2026-06-22-zellij-terminal-session-persistence.md).
 *
 * **Why.** [TerminalSessionManager] is a process-lived `object` that holds the
 * durable [com.aiblackbox.portal.data.api.ZellijWebSocketClient]s. When the app
 * is backgrounded (a long compile / agent run), Android may reclaim the whole
 * process, killing those sockets. A foreground service keeps the process alive
 * (and shows a user-visible notification of how many terminals are live), so the
 * sockets stay flowing in the background -- the whole point of the feature.
 *
 * **Lifecycle (driven by the manager's count transitions).**
 *  - START ([ACTION_START], or a bare start): becomes a foreground service with
 *    a notification reflecting the current live-session count. Started by
 *    [TerminalSessionManager] when the live-client count goes 0 -> 1.
 *  - UPDATE ([ACTION_UPDATE]): refreshes the notification count in place.
 *    Issued by the manager on any count change while >= 1 session is live.
 *  - STOP ([ACTION_STOP]) / onDestroy: leaves the foreground + stops. Issued by
 *    the manager when the last session is killed (count -> 0).
 *
 * **Android 14+ typed FGS.** Uses the `connectedDevice` type (mirroring
 * [LocalModelService]) -- the terminals are network-reached sessions on a
 * connected BlackBox device, and `connectedDevice` is NOT subject to the
 * dataSync runtime timeout. [ServiceCompat.startForeground] is always called
 * with [ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE] within the
 * startForegroundService ~5s contract.
 *
 * **Best-effort + graceful.** EVERY start/stop here is wrapped so a platform
 * refusal (background-start limits) can never crash: the worst case is "no
 * warm-keeping in the background", never a broken terminal. On true app
 * swipe-away / force-stop the FGS dies WITH the process -- that is fine, Phase
 * 2's reattach-by-name resumes the still-running server sessions on next launch.
 *
 * **START_STICKY** so the OS re-creates the service after a kill. On a null-intent
 * redelivery (sticky restart) the service reads the LIVE count from
 * [TerminalSessionManager]: if zero sessions are live (process state lost on a
 * full kill), it stops itself instead of showing a stale "terminals running"
 * notification with nothing behind it.
 */
class TerminalForegroundService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                // Self-heal a reordered cross-thread STOP: if a concurrent launch
                // re-populated the session map after this STOP was queued, stay
                // foreground rather than tearing down a live terminal's anchor.
                val count = currentCount()
                if (count > 0) {
                    startForegroundWith(buildNotification(count))
                    _isRunning = true
                    return START_STICKY
                }
                _isRunning = false
                stopForegroundCompat()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_START, ACTION_UPDATE -> {
                // Become foreground IMMEDIATELY (startForegroundService contract)
                // with a notification reflecting the current live count. ACTION_START
                // and ACTION_UPDATE are identical here -- both just (re)post the
                // count notification; the only difference is intent (start vs refresh).
                val count = currentCount()
                startForegroundWith(buildNotification(count))
                _isRunning = true
                if (count <= 0) {
                    // Spurious/reordered start with nothing live: satisfy the FGS
                    // contract, then stand down rather than show a stale 0-session
                    // notification.
                    _isRunning = false
                    stopForegroundCompat()
                    stopSelf()
                    return START_NOT_STICKY
                }
            }
            else -> {
                // A NULL-intent START_STICKY REDELIVERY after the OS killed + re-created
                // us. The process-lived TerminalSessionManager state may be GONE (a full
                // process kill clears the singleton's map), so read the live count: if no
                // sessions are live there is nothing to keep warm -- stop instead of
                // showing a stale notification. (If sessions somehow survived, keep the
                // FGS up reflecting them.) We must still call startForeground first to
                // satisfy the contract before we can legally stop.
                val count = currentCount()
                startForegroundWith(buildNotification(count))
                _isRunning = true
                if (count <= 0) {
                    Log.d(TAG, "sticky redelivery with no live sessions; stopping")
                    _isRunning = false
                    stopForegroundCompat()
                    stopSelf()
                    return START_NOT_STICKY
                }
            }
        }
        // START_STICKY: keep the warm-keeper alive across OS kills. A null redelivery
        // lands in the else branch above, which self-stops when no sessions are live.
        return START_STICKY
    }

    /** Live terminal-session count, best-effort (never throws into onStartCommand). */
    private fun currentCount(): Int =
        runCatching { TerminalSessionManager.activeCount() }.getOrDefault(0)

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
            // Platform may refuse a (background) FGS start under Android 14+
            // restrictions. Never crash onStartCommand — log + carry on; the
            // null-redelivery/STOP paths still reconcile against the live count.
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
                "Terminal Sessions",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Keeps terminal sessions running while the app is in the background"
                setShowBadge(false)
            }
            val mgr = getSystemService(NotificationManager::class.java)
            mgr?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(count: Int): Notification {
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, NativeMainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
            PendingIntent.FLAG_IMMUTABLE,
        )
        val safe = if (count < 0) 0 else count
        val text = "$safe active session${if (safe == 1) "" else "s"}"
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Terminals running")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .build()
    }

    override fun onDestroy() {
        _isRunning = false
        Log.d(TAG, "stopped")
        super.onDestroy()
    }

    companion object {
        private const val TAG = "TerminalFgService"

        // Distinct channel + notification id from LocalModelService (9093 / "blackbox_local_model").
        const val CHANNEL_ID = "blackbox_terminal_sessions"
        const val NOTIFICATION_ID = 9094
        const val ACTION_START = "com.aiblackbox.portal.START_TERMINAL_FGS"
        const val ACTION_STOP = "com.aiblackbox.portal.STOP_TERMINAL_FGS"
        const val ACTION_UPDATE = "com.aiblackbox.portal.UPDATE_TERMINAL_FGS"

        @Volatile
        private var _isRunning = false
        fun isRunning() = _isRunning

        /**
         * Best-effort START of the warm-keeping service. NEVER throws into the
         * caller: a platform refusal (background-start limits, etc.) is swallowed
         * and logged, leaving terminals working (just not background-warm).
         * Called by [TerminalSessionManager] when the live count goes 0 -> 1.
         */
        @JvmStatic
        fun start(context: Context) {
            val intent = Intent(context, TerminalForegroundService::class.java).apply {
                action = ACTION_START
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Throwable) {
                Log.w(TAG, "start refused (${e.javaClass.simpleName}); terminals still work")
            }
        }

        /**
         * Best-effort UPDATE of the live-count notification. If the service is not
         * running this is a no-op start, which is harmless (the service immediately
         * posts the current count and stays up while sessions are live). Never throws.
         * Called by [TerminalSessionManager] on any count change while >= 1 live.
         */
        @JvmStatic
        fun updateCount(context: Context) {
            val intent = Intent(context, TerminalForegroundService::class.java).apply {
                action = ACTION_UPDATE
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Throwable) {
                Log.w(TAG, "updateCount refused (${e.javaClass.simpleName})")
            }
        }

        /**
         * Best-effort STOP (the last terminal was killed). Never throws.
         * Called by [TerminalSessionManager] when the live count returns to 0.
         */
        @JvmStatic
        fun stop(context: Context) {
            val intent = Intent(context, TerminalForegroundService::class.java).apply {
                action = ACTION_STOP
            }
            runCatching { context.startService(intent) }
        }
    }
}
