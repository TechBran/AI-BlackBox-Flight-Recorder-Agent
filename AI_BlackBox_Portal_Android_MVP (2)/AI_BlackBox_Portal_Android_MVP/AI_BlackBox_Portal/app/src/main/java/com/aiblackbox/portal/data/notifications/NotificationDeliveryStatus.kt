package com.aiblackbox.portal.data.notifications

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.aiblackbox.portal.BlackBoxNotificationManager

/**
 * M4 — the Android-dependent HALF of the notification state: read the three switches off
 * the platform and hand them to the pure [NotificationPermissionUx.effectiveState].
 *
 * Deliberately split from the policy file so the policy stays host-JVM testable. Nothing
 * here decides anything; it only reads. Every read is wrapped so a hostile OEM
 * implementation degrades to "assume on" rather than crashing a Settings sheet.
 */
fun readNotificationDeliveryState(context: Context): NotificationDeliveryState {
    val granted = runCatching {
        ContextCompat.checkSelfPermission(context, NotificationPermissionUx.PERMISSION) ==
            PackageManager.PERMISSION_GRANTED
    }.getOrDefault(false)

    // App-level master switch. This is the one that was OFF on the Fold during M3
    // validation (app notification importance NONE) — a permission-only check would have
    // called that state "granted".
    val appEnabled = runCatching {
        NotificationManagerCompat.from(context).areNotificationsEnabled()
    }.getOrDefault(true)

    // The navigation channel specifically. A null channel means it has not been created
    // yet (no BlackBoxNotificationManager constructed in this process); it will be created
    // at post time at IMPORTANCE_HIGH, so absent == fine.
    val channelEnabled = runCatching {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            true
        } else {
            val channel = context.getSystemService(NotificationManager::class.java)
                ?.getNotificationChannel(BlackBoxNotificationManager.CHANNEL_ID_NAVIGATION)
            channel == null || channel.importance != NotificationManager.IMPORTANCE_NONE
        }
    }.getOrDefault(true)

    return NotificationPermissionUx.effectiveState(
        sdkInt = Build.VERSION.SDK_INT,
        hasPermission = granted,
        appNotificationsEnabled = appEnabled,
        navigationChannelEnabled = channelEnabled,
    )
}

/**
 * Deep-link to this app's notification settings — the ONLY recovery path we offer after a
 * denial, and it is passive: it lives in Settings behind a caption the operator went
 * looking for. We never re-prompt.
 */
fun openNotificationSettings(context: Context) {
    val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
    } else {
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            .setData(Uri.fromParts("package", context.packageName, null))
    }
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { context.startActivity(intent) }
}
