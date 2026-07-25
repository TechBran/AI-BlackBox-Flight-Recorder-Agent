package com.aiblackbox.portal.overlay

/**
 * (M3) The process-wide "does this app currently have a visible activity?" flag — the ONLY
 * thing that tells the actuator whether a `startActivity` can actually reach the screen.
 *
 * ## Why this exists (the measured defect)
 * Android's **Background Activity Launch (BAL)** restrictions silently DISCARD an activity
 * launch made from a process with no visible window. `Context.startActivity` does **not**
 * throw when that happens — it returns normally and logs to logcat — so the shipped M2
 * `navigate` actuator reported `{"success": true, "detail": "started navigation"}` while
 * Google Maps never opened. Measured on a real Galaxy Z Fold 6 with the app backgrounded:
 * success:true, no Maps activity, launcher still in front ten seconds later.
 *
 * A false success is worse than a failure: an unattended cron would tell the operator
 * "I have started navigation to your job site" while the phone sat in a pocket doing
 * nothing, and they would only discover it when they needed the directions.
 *
 * ## Why a tracked flag and not a query
 * **There is no API that answers "may I launch an activity right now?"** The platform
 * exposes no `canStartActivity()`, and `ActivityManager.RunningAppProcessInfo.importance`
 * is both process-wide-fuzzy and unreliable across OEM skins. The reliable signal is the
 * app's own lifecycle: `ProcessLifecycleOwner` transitions to STARTED when any activity of
 * this process becomes visible and to STOPPED when the last one leaves. That is exactly
 * the BAL "app has a visible window" exemption, observed from the inside.
 *
 * A foreground *service* deliberately does NOT count: since Android 10, an FGS alone does
 * not grant an activity-launch exemption for an app targeting a modern SDK (this app is
 * `targetSdk 36`), which is precisely why the shipped listener FGS could not make the
 * launch work.
 *
 * ## Fail-CLOSED
 * The flag starts `false`. An un-wired process (or one whose Application never ran the
 * observer) therefore reports "not foreground", which routes navigation to the tappable
 * notification instead of to a launch that would be discarded. The safe failure is an
 * extra notification; the unsafe failure is a lie.
 *
 * Framework-free by design (a single `@Volatile` Boolean + accessors) so the whole
 * decision table is host-JVM unit-testable. The Android wiring — the
 * `ProcessLifecycleOwner` observer — lives in
 * [com.aiblackbox.portal.PortalApplication], which calls [onForeground]/[onBackground].
 */
object AppForegroundState {

    /** Written from the main thread (lifecycle callbacks), read from NanoHTTPD worker
     *  threads and coroutine dispatchers — hence `@Volatile`. */
    @Volatile
    private var foreground: Boolean = false

    /**
     * `true` when this process currently has at least one STARTED (visible) activity, i.e.
     * when a direct `startActivity` can actually reach the screen. Fail-closed: `false`
     * until the lifecycle observer has reported a foreground transition.
     */
    fun isForeground(): Boolean = foreground

    /** ProcessLifecycleOwner ON_START — an activity of this process became visible. */
    fun onForeground() {
        foreground = true
    }

    /** ProcessLifecycleOwner ON_STOP — the last visible activity of this process left. */
    fun onBackground() {
        foreground = false
    }

    /** Test seam ONLY: drive the flag directly (no Android lifecycle on the host JVM). */
    internal fun setForegroundForTest(value: Boolean) {
        foreground = value
    }
}
