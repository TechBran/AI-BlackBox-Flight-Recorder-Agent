package com.aiblackbox.portal.data.local

import android.content.Context

/**
 * Persisted user SETTING for the on-device (`local`) engine AUTO-WARM-on-open.
 *
 * ## Why
 * On a foreground/open the [com.aiblackbox.portal.ui.chat.ChatViewModel.preloadLocalEngine]
 * path eagerly warms the ~3.66GB on-device model into GPU RAM (~10-75s) so the first
 * send is instant. That is the right default for a daily-driver, but some users would
 * rather NOT pay that RAM/time cost on every open and prefer the model to load LAZILY
 * on the first send instead. This setting (surfaced later by the on-device-model
 * settings screen) lets them opt out: when DISABLED, the auto path skips the warm and
 * a deliberate send warms on demand.
 *
 * This is a NEW, standalone setting. The per-model context-window / sampler overrides
 * already persist via [LocalModelManager]'s `ModelConfig` JSON — those are NOT
 * duplicated here. This store carries ONLY the global auto-warm boolean.
 *
 * ## Default
 * [autoWarmEnabled] returns TRUE when never set (first run / cleared pref): the
 * instant-first-send behavior is the default; opting out is an explicit user choice.
 * Distinct from [WarmInflightStore], whose crash-loop guard is a SEPARATE, automatic
 * safety mechanism (this setting is a user PREFERENCE; the guard can still suppress an
 * auto-warm even when this setting is ENABLED).
 *
 * ## Pattern
 * Mirrors [WarmInflightStore]/[AutonomyStore]/[PersonaCache]: a tiny seam
 * ([LocalWarmPrefs]) with the SINGLE Android-touching implementation
 * ([SharedPrefsLocalWarmPrefs]) confined to [fromContext]; unit tests substitute an
 * in-memory fake (no SharedPreferences). Its OWN dedicated prefs file (separate from
 * the warm-inflight guard's) so the two concerns never collide.
 */
interface LocalWarmPrefs {
    /**
     * The persisted auto-warm-on-open setting, or `true` (the default) if never set.
     * When `false`, the AUTO (preload) path skips the warm and the model loads lazily
     * on the first send.
     */
    fun autoWarmEnabled(): Boolean

    /** Persist the auto-warm-on-open setting ([v] = true to auto-warm, false to opt out). */
    fun setAutoWarmEnabled(v: Boolean)

    companion object {
        /** The default when no value has been stored: auto-warm ON (instant first send). */
        const val DEFAULT_AUTO_WARM: Boolean = true

        private const val PREFS_NAME = "bbx_local_warm_prefs"
        private const val KEY_AUTO_WARM = "auto_warm"

        /** Build the SharedPreferences-backed store. The only Android-touching path. */
        fun fromContext(context: Context): LocalWarmPrefs =
            SharedPrefsLocalWarmPrefs(context.applicationContext)
    }

    /** Production store: the auto-warm setting in a dedicated SharedPreferences file. */
    private class SharedPrefsLocalWarmPrefs(context: Context) : LocalWarmPrefs {
        private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        override fun autoWarmEnabled(): Boolean =
            prefs.getBoolean(KEY_AUTO_WARM, DEFAULT_AUTO_WARM)

        override fun setAutoWarmEnabled(v: Boolean) {
            prefs.edit().putBoolean(KEY_AUTO_WARM, v).apply()
        }
    }
}
