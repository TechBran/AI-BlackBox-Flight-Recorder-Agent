package com.aiblackbox.portal

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Re-arms the model-free notification listener after a device reboot (MN.5) so the
 * phone is a reachable notification target without the user re-opening the app.
 *
 * **Why a DIRECT start (not a WorkManager hand-off).** The prior implementation
 * enqueued a WorkManager `CoroutineWorker` and started the FGS from `doWork()`. That
 * was WRONG: the `BOOT_COMPLETED` background-start exemption is tied to the broadcast
 * receiver's `onReceive()` context window — a WorkManager worker runs in a PLAIN
 * background context that does NOT inherit that exemption, so the deferred
 * `startForegroundService()` threw `ForegroundServiceStartNotAllowedException`, which
 * was then silently swallowed. The old KDoc claiming "WorkManager runs in a context
 * where starting the FGS is permitted" was false.
 *
 * **Why the direct start IS legal here.** On Android 14/15+ a `BOOT_COMPLETED` receiver
 * may NOT start the foreground-service types `dataSync`, `camera`, `mediaPlayback`,
 * `phoneCall`, `mediaProjection`, or `microphone`. [NotificationListenerFgs] is a
 * `connectedDevice` FGS — NOT on that blocked list — so starting it straight from
 * `onReceive()` (the exempt context) is permitted by the platform. It also needs no
 * while-in-use permission, so the "can't create while in background" caveat does not
 * apply. We therefore start it INLINE from this receiver, where the exemption is live.
 *
 * Registered (exported, with the BOOT_COMPLETED intent-filter) in the manifest; requires
 * the `RECEIVE_BOOT_COMPLETED` permission. [NotificationListenerFgs.start] is itself
 * best-effort + non-throwing — a platform refusal (e.g. an OEM that is stricter than
 * AOSP) is logged inside it, and the listener still arms on the next app open, with the
 * server-side durable snapshot inbox covering notifications until then.
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
        Log.d(TAG, "boot completed ($action); arming notification listener (connectedDevice FGS)")
        // Start DIRECTLY from the receiver — this is the exempt BOOT_COMPLETED context.
        // connectedDevice is not in the boot-blocked FGS-type set, so the start is legal.
        // NotificationListenerFgs.start is non-throwing; do NOT wrap in a worker (a worker
        // would forfeit the boot exemption and the start would be refused).
        NotificationListenerFgs.start(context.applicationContext)
    }

    companion object {
        private const val TAG = "BootReceiver"
    }
}
