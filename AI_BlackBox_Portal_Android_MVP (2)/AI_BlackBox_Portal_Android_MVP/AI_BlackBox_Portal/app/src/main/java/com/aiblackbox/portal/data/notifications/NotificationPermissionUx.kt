package com.aiblackbox.portal.data.notifications

import android.Manifest
import android.os.Build

/**
 * M4 — the ask-once-ever policy for POST_NOTIFICATIONS, as a PURE decision, mirroring
 * [com.aiblackbox.portal.data.location.LocationPermissionUx] exactly.
 *
 * ## Why this exists
 * The manifest has DECLARED `POST_NOTIFICATIONS` since long before M3, but nothing in the
 * native app ever REQUESTED it. On Android 13+ a declared-but-unrequested permission is
 * simply denied, so on a fresh install the whole M3 delivery path — the lock-screen
 * navigation prompt that a 07:30 cron job pushes, and every task alert — posts into a void.
 * Device check during M3 validation: `POST_NOTIFICATIONS: granted=false`, app notification
 * importance NONE. That is exactly the silent dead end M3 was built to eliminate, so the
 * runtime request is not optional polish; it is the other half of M3.
 *
 * (`PortalActivity`, the WebView wrapper, has always asked — but `PairingActivity` routes
 * to `NativeMainActivity` in native mode, so the ask was never reached on the real device.)
 *
 * ## The policy (identical to the location one, deliberately)
 * Ask ONCE with a plain rationale; if denied, degrade **silently and forever**. No second
 * prompt, no banner, no nag, no error — a phone that said no simply never sees a BlackBox
 * notification, and the box reports that truthfully over the wire
 * ([com.aiblackbox.portal.overlay.NavigationNotifyOutcome.PERMISSION_MISSING]) instead of
 * claiming a delivery that did not happen. Recovering is the operator's own trip to system
 * settings, which Settings deep-links to.
 *
 * ## Ordering against the location ask (M1)
 * The two asks are separated by TRIGGER, not by timing luck:
 *  - **Notifications ask first**, once, on the first composition of `NativeMainActivity`.
 *    There is no "in context" moment for it: the notification it unlocks arrives while the
 *    app is closed, so foreground-at-startup is the only time we can ask at all.
 *  - **Location asks second**, on the first send (M1, unchanged).
 * They cannot collide, because a send requires a composer tap that the modal rationale
 * covers — and [shouldAsk] takes `anotherPromptVisible` on BOTH sides so the guarantee is
 * enforced rather than assumed. A deferred ask does **not** burn its latch: it is simply
 * re-evaluated on the next trigger, so deferring can never cost an operator their one ask.
 */
object NotificationPermissionUx {

    /** The one permission requested. Nothing else rides along with it. */
    const val PERMISSION: String = Manifest.permission.POST_NOTIFICATIONS

    /** Below this, notifications need no runtime grant — asking is impossible and wrong. */
    const val MIN_SDK_REQUIRING_GRANT: Int = Build.VERSION_CODES.TIRAMISU // 33

    /**
     * The rationale, in the operator's terms. It names the two things that actually stop
     * working — scheduled navigation and task alerts — and it says the failure is SILENT,
     * because "you will not be told" is the part that makes a denial expensive.
     */
    const val RATIONALE_TITLE = "Turn on notifications?"
    const val RATIONALE_BODY =
        "Scheduled navigation and task alerts arrive as notifications. A job address sent " +
            "at 07:30 lands on your lock screen and one tap opens Maps; a long job that " +
            "finishes while the app is closed tells you it is done.\n\n" +
            "Without this, those silently do not arrive — nothing appears and nothing warns " +
            "you that it was missed. You can turn it off any time in Settings."
    const val RATIONALE_CONFIRM = "Allow"
    const val RATIONALE_DISMISS = "Not now"

    /** Android 13+ is the only place a runtime grant exists. */
    fun isGrantRequired(sdkInt: Int): Boolean = sdkInt >= MIN_SDK_REQUIRING_GRANT

    /**
     * Should the rationale be shown right now?
     *
     * Every guard is a hard no:
     *  - below API 33 → the OS grants notifications implicitly; a prompt would be a bug.
     *  - already granted → nothing to ask for.
     *  - already asked → the ask-once latch. Covers granted AND denied — this is the
     *    clause that makes a denial permanent and silent.
     *  - another permission prompt visible → DEFER (the latch is untouched, so the next
     *    trigger asks). This is what keeps the M1 location ask and this one from stacking.
     */
    fun shouldAsk(
        sdkInt: Int,
        hasPermission: Boolean,
        alreadyAsked: Boolean,
        anotherPromptVisible: Boolean = false,
    ): Boolean =
        isGrantRequired(sdkInt) && !hasPermission && !alreadyAsked && !anotherPromptVisible

    /**
     * The TRUE end-to-end delivery state, not just the permission bit.
     *
     * Three independent switches can each swallow a notification, and the device found
     * during M3 validation had two of them off at once. Reporting only the permission
     * would let Settings claim "granted" while the shade is importance NONE — a caption
     * that lies is worse than no caption.
     *
     * Precedence is outermost-first: no grant → app-level off → this channel off.
     */
    fun effectiveState(
        sdkInt: Int,
        hasPermission: Boolean,
        appNotificationsEnabled: Boolean,
        navigationChannelEnabled: Boolean,
    ): NotificationDeliveryState = when {
        isGrantRequired(sdkInt) && !hasPermission -> NotificationDeliveryState.PERMISSION_MISSING
        !appNotificationsEnabled -> NotificationDeliveryState.APP_DISABLED
        !navigationChannelEnabled -> NotificationDeliveryState.CHANNEL_DISABLED
        else -> NotificationDeliveryState.DELIVERS
    }

    /**
     * The Settings caption for a state. Every non-delivering caption says the words
     * "will NOT arrive" — the operator must be able to read the dead end, not infer it.
     */
    fun caption(state: NotificationDeliveryState): String = when (state) {
        NotificationDeliveryState.DELIVERS ->
            "On — scheduled navigation and task alerts arrive on this phone."
        NotificationDeliveryState.PERMISSION_MISSING ->
            "Off — Android has not granted BlackBox permission to post notifications, so " +
                "scheduled navigation and task alerts will NOT arrive."
        NotificationDeliveryState.APP_DISABLED ->
            "Off — notifications for BlackBox are switched off in system settings, so " +
                "scheduled navigation and task alerts will NOT arrive."
        NotificationDeliveryState.CHANNEL_DISABLED ->
            "Navigation prompts are switched off in system settings. Task alerts still " +
                "arrive, but a scheduled navigation push will NOT."
    }

    /** True when Settings should offer the deep link to fix it. Never a dialog, never a nag. */
    fun needsAttention(state: NotificationDeliveryState): Boolean =
        state != NotificationDeliveryState.DELIVERS

    /** Label for the (passive, Settings-only) escape hatch out of a denied state. */
    const val OPEN_SETTINGS_LABEL = "Open notification settings"
}

/**
 * The honest, end-to-end answer to "will a BlackBox notification actually reach this
 * phone?". Four states because "not granted", "the whole app is muted" and "the navigation
 * channel specifically is muted" are different problems with different fixes, and none of
 * them may be reported as working.
 */
enum class NotificationDeliveryState {
    /** Everything is on. A push will land. */
    DELIVERS,

    /** POST_NOTIFICATIONS was never granted (Android 13+). Nothing lands. */
    PERMISSION_MISSING,

    /** Notifications for the whole app are off in system settings. Nothing lands. */
    APP_DISABLED,

    /** The navigation channel is at IMPORTANCE_NONE. Navigation prompts do not land. */
    CHANNEL_DISABLED,
}
