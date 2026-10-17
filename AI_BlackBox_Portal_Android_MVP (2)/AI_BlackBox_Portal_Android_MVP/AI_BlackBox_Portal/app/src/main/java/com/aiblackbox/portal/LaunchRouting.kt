package com.aiblackbox.portal

import android.app.Activity
import android.content.Context
import com.aiblackbox.portal.util.Constants

/**
 * Which UI an app entry lands on — the ONE rule the launcher ([PairingActivity]) and
 * every notification tap share (A01-activities-root-04). Before, notification taps
 * always opened the legacy WebView [PortalActivity] (KeepAliveService, a wake lock,
 * the web Portal) even for a native-UI operator.
 */
internal enum class LaunchTarget { PAIRING, NATIVE, WEB }

/** Not paired → pairing; paired → the native UI unless the operator chose the web UI. */
internal fun resolveLaunchTarget(origin: String?, useNativeUi: Boolean): LaunchTarget = when {
    origin.isNullOrBlank() -> LaunchTarget.PAIRING
    useNativeUi -> LaunchTarget.NATIVE
    else -> LaunchTarget.WEB
}

internal fun LaunchTarget.activityClass(): Class<out Activity> = when (this) {
    LaunchTarget.PAIRING -> PairingActivity::class.java
    LaunchTarget.NATIVE -> NativeMainActivity::class.java
    LaunchTarget.WEB -> PortalActivity::class.java
}

/** [resolveLaunchTarget] over the pairing store (the same prefs PairingActivity reads). */
internal fun Context.currentLaunchTarget(): LaunchTarget {
    val prefs = getSharedPreferences(Constants.PREFS_NAME, Context.MODE_PRIVATE)
    return resolveLaunchTarget(
        origin = prefs.getString(Constants.KEY_ORIGIN, null),
        // Default true — PairingActivity's default; Settings writes the operator's choice.
        useNativeUi = prefs.getBoolean(Constants.KEY_USE_NATIVE, true),
    )
}
