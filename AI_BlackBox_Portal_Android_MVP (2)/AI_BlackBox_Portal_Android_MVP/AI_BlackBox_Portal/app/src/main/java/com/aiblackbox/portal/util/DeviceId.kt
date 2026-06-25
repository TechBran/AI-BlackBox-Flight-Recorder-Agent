package com.aiblackbox.portal.util

import android.content.Context

/**
 * Single source of the stable per-device identifier (ANDROID_ID), hoisted out of the
 * two ViewModels that each had a private copy
 * ([com.aiblackbox.portal.ui.settings.LocalModelViewModel] and
 * [com.aiblackbox.portal.ui.chat.ProviderPickerViewModel]). Also the subscription key
 * the notification subsystem scopes a device's allow-list under, so all surfaces must
 * agree on EXACTLY this value.
 */
object DeviceId {

    /** Fallback used when ANDROID_ID is unavailable (matches the prior inline default). */
    const val FALLBACK = "android-device"

    /** Stable per-device id: ANDROID_ID, falling back to [FALLBACK]. Never throws. */
    @Suppress("HardwareIds")
    fun stable(context: Context): String {
        val androidId = runCatching {
            android.provider.Settings.Secure.getString(
                context.contentResolver,
                android.provider.Settings.Secure.ANDROID_ID,
            )
        }.getOrNull()
        return androidId?.takeIf { it.isNotBlank() } ?: FALLBACK
    }
}
