package com.aiblackbox.portal.overlay

/**
 * (M3) The PURE delivery layer for `navigate` — how a navigation request reaches the
 * screen, and what we are allowed to CLAIM about it.
 *
 * M2 shipped one delivery mechanism: build a `google.navigation:` intent and
 * `startActivity` it. That works — and is device-proven — only while the app has a
 * visible window. With the app backgrounded, Android's Background Activity Launch
 * restriction DISCARDS the launch **without throwing**, so the actuator reported
 * `success:true, "started navigation"` and nothing opened (measured on a Galaxy Z Fold 6).
 *
 * The one path the platform permits from the background is a **notification the user
 * taps**: a tap is a user gesture, and the activity launch that follows it is exempt.
 * So navigation has two deliveries, and a mode that chooses between them:
 *
 * | `delivery` | app foreground | app background |
 * |---|---|---|
 * | `auto` (default) | [NavigationDelivery.DIRECT] | [NavigationDelivery.NOTIFY] |
 * | `direct` | [NavigationDelivery.DIRECT] | [NavigationDelivery.DIRECT] → refuses honestly |
 * | `notify` | [NavigationDelivery.NOTIFY] | [NavigationDelivery.NOTIFY] |
 *
 * `direct` while backgrounded is deliberately NOT silently upgraded to a notification:
 * the caller asked for a specific mechanism, and the honest answer is
 * [BACKGROUND_LAUNCH_BLOCKED_DETAIL] — a refusal, not a lie and not a substitution.
 *
 * Everything here is framework-free so the whole table, the parse, the dedup key and the
 * notification text are pinned on the host JVM. The Android half (posting the
 * notification, firing the intent) is [com.aiblackbox.portal.BlackBoxNotificationManager]
 * and [IntentActuator].
 */

// ---- delivery mode (the `delivery` action param) --------------------------------

/** How the caller asked for the navigation to be delivered. */
enum class DeliveryMode {
    /** Direct launch when the app is foreground, tappable notification otherwise. */
    AUTO,

    /** Always a direct `startActivity`; refuses honestly when it cannot reach the screen. */
    DIRECT,

    /** Always a tappable navigation notification, even with the app in front. */
    NOTIFY,
}

/** What the actuator will actually DO, after resolving [DeliveryMode] against the app's
 *  live foreground state. */
enum class NavigationDelivery {
    /** `startActivity` the `google.navigation:` intent now. */
    DIRECT,

    /** Post a notification whose "Navigate" action fires that same intent on tap. */
    NOTIFY,
}

/**
 * The ONLY accepted `delivery` values. An unknown value is REJECTED (never silently
 * coerced to a default) — a model that invents `delivery:"push"` must be told, not
 * quietly given a mechanism it did not ask for.
 */
val NAVIGATION_DELIVERY_MODES: Set<String> = setOf("auto", "direct", "notify")

/**
 * PURE: parse the `delivery` arg. Absent/blank → [DeliveryMode.AUTO] (the default).
 * A whitelisted value (trimmed, case-insensitive) → that mode. Anything else → `null`,
 * meaning INVALID — see [deliveryRejectionReason].
 */
fun parsedDeliveryMode(raw: String?): DeliveryMode? {
    val d = raw?.trim()?.lowercase()
    if (d.isNullOrEmpty()) return DeliveryMode.AUTO
    return when (d) {
        "auto" -> DeliveryMode.AUTO
        "direct" -> DeliveryMode.DIRECT
        "notify" -> DeliveryMode.NOTIFY
        else -> null
    }
}

/**
 * PURE: the rejection reason for an unusable `delivery` value, or `null` to proceed.
 *
 * The phrase intentionally starts with `invalid navigation` so the existing remote error
 * classifier ([com.aiblackbox.portal.data.remote.classifyActuatorError]) already maps it
 * to `invalid_argument` — the same treatment as an invalid `mode`/`avoid`, with no new
 * classifier branch needed.
 */
fun deliveryRejectionReason(raw: String?): String? =
    if (parsedDeliveryMode(raw) == null)
        "invalid navigation delivery — use one of: auto (direct when the app is open, " +
            "otherwise a tappable notification), direct, notify"
    else null

/**
 * PURE: the decision table above. [appForeground] is the app's OWN process state
 * ([AppForegroundState.isForeground]) — the only observable proxy for "a direct launch
 * can actually reach the screen".
 */
fun navigationDeliveryPlan(mode: DeliveryMode, appForeground: Boolean): NavigationDelivery =
    when (mode) {
        DeliveryMode.DIRECT -> NavigationDelivery.DIRECT
        DeliveryMode.NOTIFY -> NavigationDelivery.NOTIFY
        DeliveryMode.AUTO -> if (appForeground) NavigationDelivery.DIRECT else NavigationDelivery.NOTIFY
    }

/**
 * PURE: what the actuator must actually DO, once the argument envelope and the
 * `resolveActivity` preflight have both passed. This is THE decision — [IntentActuator]
 * calls exactly this function, so the table below is production behaviour and not a
 * parallel description of it.
 *
 * [NavigationAction.Refuse] is the fix for the measured defect: a `direct` delivery with
 * no visible window is REFUSED, because attempting it produces a silent no-op that
 * `startActivity` reports as success.
 */
sealed class NavigationAction {
    /** Fire the intent now — the app has a visible window, so the launch will land. */
    object Launch : NavigationAction()

    /** Post the tappable prompt — the tap is what makes the eventual launch legal. */
    object Notify : NavigationAction()

    /** Do nothing and say so. [detail] is a token-prefixed, human-readable refusal. */
    data class Refuse(val detail: String) : NavigationAction()
}

/**
 * PURE: resolve [mode] + the app's live foreground state into the action to take.
 *
 * `auto` + background → [NavigationAction.Notify] (the delivery that works).
 * `direct` + background → [NavigationAction.Refuse] with [BACKGROUND_LAUNCH_BLOCKED_DETAIL]
 * — never a silent substitution, and never the old `success:true, "started navigation"`.
 */
fun navigationAction(mode: DeliveryMode, appForeground: Boolean): NavigationAction =
    when (navigationDeliveryPlan(mode, appForeground)) {
        NavigationDelivery.NOTIFY -> NavigationAction.Notify
        NavigationDelivery.DIRECT ->
            if (appForeground) NavigationAction.Launch
            else NavigationAction.Refuse(BACKGROUND_LAUNCH_BLOCKED_DETAIL)
    }

// ---- honest failure details -----------------------------------------------------
//
// Each starts with a TOKEN that [classifyActuatorError] lifts verbatim into the wire
// `error` field, followed by an em-dash and a sentence a human would understand. Same
// shape as the shipped `intent_only_mode` detail. NONE of them contains the destination
// (leak discipline: actuator details are fixed phrases or a package name, never args).

/** Wire error kind for "the OS discarded the launch because we are backgrounded". */
const val BACKGROUND_LAUNCH_BLOCKED: String = "background_launch_blocked"

/** Wire error kind for "POST_NOTIFICATIONS was never granted, so nothing was delivered". */
const val NOTIFICATION_PERMISSION_MISSING: String = "notification_permission_missing"

/** Wire error kind for "the notification path itself failed / is not wired". */
const val NOTIFICATION_DELIVERY_FAILED: String = "notification_delivery_failed"

/**
 * The honest answer M2 could not give. Returned INSTEAD of `success:true, "started
 * navigation"` when a direct launch is dispatched with the app backgrounded — the launch
 * is not attempted at all, because attempting it produces a silent no-op the actuator
 * cannot detect.
 */
const val BACKGROUND_LAUNCH_BLOCKED_DETAIL: String =
    "$BACKGROUND_LAUNCH_BLOCKED — the BlackBox app is not in the foreground, so Android " +
        "discards the navigation launch and NOTHING opened. Open the app on the phone first, " +
        "or use delivery=notify to send a tappable navigation notification."

/** POST_NOTIFICATIONS (Android 13+) is not granted → the prompt could not be delivered. */
const val NOTIFICATION_PERMISSION_MISSING_DETAIL: String =
    "$NOTIFICATION_PERMISSION_MISSING — the phone has not granted BlackBox permission to post " +
        "notifications, so the navigation prompt was NOT delivered. Grant notifications for " +
        "BlackBox on the device."

/** No notifier wired, or the platform refused the post. */
const val NOTIFICATION_DELIVERY_FAILED_DETAIL: String =
    "$NOTIFICATION_DELIVERY_FAILED — the navigation notification could not be posted on this " +
        "device, so nothing was delivered."

/** Success detail for the notification delivery. Deliberately NOT "started navigation" —
 *  nothing has started; a prompt is waiting for a tap. */
const val NAVIGATION_NOTIFY_POSTED_DETAIL: String =
    "navigation notification posted — waiting for the user to tap Navigate"

// ---- the notification payload ---------------------------------------------------

/** The action-button label. A tap on it is the BAL exemption. */
const val NAVIGATION_ACTION_LABEL: String = "Navigate"

/**
 * Everything needed to post ONE navigation notification. [destination] is REQUIRED and
 * non-blank by construction of every call site — a notification that hides where it is
 * about to send you is not consent, so the payload can never exist without it.
 *
 * [travelMode] / [avoid] / [packageName] are the already-whitelisted values from the
 * `navigate` envelope ([navigationRejectionReason] / [navigationTargetPackage]), passed
 * through so the tap reproduces the EXACT intent a direct launch would have fired.
 */
data class NavigationPush(
    val destination: String,
    val travelMode: String? = null,
    val avoid: String? = null,
    val packageName: String? = null,
    val dedupKey: String,
)

/**
 * PURE: the STABLE dedup key for a navigation prompt.
 *
 * A retrying cron (or a bus redelivery) must not stack a second "Navigate to the job
 * site" prompt on top of the first. An [explicit] key from the caller wins (the backend
 * bus already mints one per push); otherwise the key is derived from the DESTINATION, so
 * two independent pushes for the same place collapse onto one notification while a push
 * for a different place gets its own.
 */
fun navigationDedupKey(explicit: String?, destination: String): String {
    val e = explicit?.trim()
    if (!e.isNullOrEmpty()) return e
    return "nav:" + destination.trim().lowercase()
}

/** PURE: the human label for a whitelisted travel mode, or `null` when unspecified. */
fun travelModeLabel(mode: String?): String? = when (normalizedNavigationMode(mode)) {
    "d" -> "Driving"
    "b" -> "Bicycling"
    "l" -> "Two-wheeler"
    "w" -> "Walking"
    else -> null
}

/**
 * PURE: the notification TITLE. Carries the destination so it is legible on a locked
 * screen without expanding. Long free-text addresses are ellipsized for the title only —
 * [navigationNotificationText] always carries the destination in full.
 */
fun navigationNotificationTitle(destination: String): String {
    val d = destination.trim()
    val short = if (d.length <= TITLE_DESTINATION_MAX) d
    else d.take(TITLE_DESTINATION_MAX - 1).trimEnd() + "…"
    return "Navigate to $short"
}

/**
 * PURE: the notification BODY. ALWAYS contains the full destination — this is the consent
 * surface, and a prompt that hides where it is about to send you is not consent. The
 * travel mode is appended when one was specified.
 */
fun navigationNotificationText(destination: String, travelMode: String? = null): String {
    val d = destination.trim()
    val label = travelModeLabel(travelMode)
    return if (label == null) d else "$d · $label"
}

private const val TITLE_DESTINATION_MAX = 40

// ---- the Android seam -----------------------------------------------------------

/** The outcome of trying to post a navigation notification. Three states, because
 *  "permission was never granted" and "the platform refused" must not both be reported
 *  as a generic failure — and neither may be reported as success. */
enum class NavigationNotifyOutcome {
    /** Delivered. A prompt is on the phone; nothing has navigated yet. */
    POSTED,

    /** POST_NOTIFICATIONS is not granted (Android 13+). Nothing was delivered. */
    PERMISSION_MISSING,

    /** The platform refused the post, or no poster is wired. Nothing was delivered. */
    FAILED,
}

/**
 * The seam through which a navigation prompt reaches the system notification shade.
 * Production: [com.aiblackbox.portal.BlackBoxNotificationManager.showNavigationNotification].
 * A fun interface so both call sites (the `/action` actuator and the `/notify` route) are
 * host-JVM testable with a fake.
 */
fun interface NavigationNotifier {
    /** Post (or idempotently re-post, keyed on [NavigationPush.dedupKey]) the prompt. */
    fun postNavigation(push: NavigationPush): NavigationNotifyOutcome
}
