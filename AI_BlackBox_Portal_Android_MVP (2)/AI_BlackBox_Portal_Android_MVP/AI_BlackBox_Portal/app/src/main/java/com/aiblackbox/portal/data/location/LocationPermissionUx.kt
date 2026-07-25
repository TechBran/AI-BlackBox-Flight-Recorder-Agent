package com.aiblackbox.portal.data.location

import android.Manifest

/**
 * M1 — the ask-once-ever policy for the location permission, as a PURE decision so the
 * "never nag" guarantee is unit-tested rather than trusted.
 *
 * The rule Brandon set: ask on FIRST use with a plain rationale; if denied, degrade
 * **silently and forever**. There is no second prompt, no banner, no "location is off"
 * nag, and no error — a denied phone simply sends prompts exactly as it does today.
 * Recovering is the operator's own trip to system settings, which is the correct place
 * for a decision they already made once.
 */
object LocationPermissionUx {

    /** While-in-use only. ACCESS_BACKGROUND_LOCATION is not here and must never be. */
    val REQUESTED_PERMISSIONS: Array<String> = arrayOf(
        Manifest.permission.ACCESS_COARSE_LOCATION,
        Manifest.permission.ACCESS_FINE_LOCATION,
    )

    /**
     * The rationale, in plain language. It says what the operator gets, not what the app
     * wants, and it never implies tracking — because there is none.
     */
    const val RATIONALE_TITLE = "Share your location?"
    const val RATIONALE_BODY =
        "The BlackBox can attach your location to a message you send, so the assistant " +
            "can answer questions about where you are — nearby places, directions, local " +
            "conditions.\n\n" +
            "It is read only when you send a message, never in the background, and you can " +
            "turn it off any time in Settings."
    const val RATIONALE_CONFIRM = "Allow"
    const val RATIONALE_DISMISS = "Not now"

    /**
     * Should the rationale be shown on this send?
     *
     * Every guard is a hard no:
     *  - toggle off  → the operator has already opted out; asking would be nagging.
     *  - already asked → the ask-once latch. Covers both "granted" and "denied" — this
     *    is the clause that makes a denial permanent.
     *  - already granted → nothing to ask for.
     */
    fun shouldAsk(attachEnabled: Boolean, hasPermission: Boolean, alreadyAsked: Boolean): Boolean =
        attachEnabled && !hasPermission && !alreadyAsked

    /**
     * Whether a location will actually be attached to an outgoing turn. Both switches
     * must be on: the operator's toggle AND the OS grant. Used by Settings to describe
     * the true state instead of implying the toggle alone is enough.
     */
    fun willAttach(attachEnabled: Boolean, hasPermission: Boolean): Boolean =
        attachEnabled && hasPermission
}
