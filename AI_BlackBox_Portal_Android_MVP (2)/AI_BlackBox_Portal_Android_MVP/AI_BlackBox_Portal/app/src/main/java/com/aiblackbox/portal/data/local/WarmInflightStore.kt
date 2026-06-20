package com.aiblackbox.portal.data.local

import android.content.Context

/**
 * Crash-loop guard for the on-device (`local`) engine warm.
 *
 * ## Why
 * Warming the on-device engine ([com.aiblackbox.portal.data.local.LiteRtEngine.load])
 * pulls a ~3.66GB model into GPU RAM over ~10-75s. On a memory-pressured device the
 * OS can SIGKILL the WHOLE app process mid-load. A SIGKILL runs NO cleanup code
 * (no catch/finally), so the only reliable "did the last warm crash?" signal is a
 * flag PERSISTED to disk BEFORE the warm that is still set on the next process start.
 *
 * Without this guard the auto-warm in [com.aiblackbox.portal.ui.chat.ChatViewModel.preloadLocalEngine]
 * fires on every foreground, so a warm that OOMs becomes an unbounded
 * crash -> restart -> auto-warm -> crash LOOP that makes the phone unusable. This
 * store lets the auto path detect "the previous warm never completed" and SKIP the
 * auto-warm, converting the loop into a single failure with a manual retry (a
 * deliberate send still warms).
 *
 * ## Contract
 *  - Set [setInflight](true) IMMEDIATELY BEFORE a warm `load()` begins.
 *  - Set [setInflight](false) on warm SUCCESS and on a GRACEFUL (caught) failure.
 *  - On launch, read [isInflight]: if it is STILL true, the previous warm was killed
 *    mid-flight (no success/failure cleanup ran) -> the AUTO path must not warm again.
 *
 * ## Pattern
 * Mirrors [AutonomyStore]/[PersonaCache]: a tiny seam ([WarmInflightStore]) with the
 * SINGLE Android-touching implementation ([SharedPrefsWarmInflightStore]) confined to
 * [fromContext]; the AUTO-warm DECISION is the PURE [shouldAutoWarm] so unit tests
 * exercise the boolean logic without Android SharedPreferences.
 */
interface WarmInflightStore {
    /**
     * The persisted "warm in-flight" flag, or `false` if never set (first run, or a
     * clean prior warm that cleared it). `true` on the next process start means the
     * prior warm was SIGKILLed mid-load.
     */
    fun isInflight(): Boolean

    /** Persist the warm in-flight flag ([value] = true before a warm, false after). */
    fun setInflight(value: Boolean)

    companion object {
        /**
         * PURE decision for the AUTO (preload) warm path: warm only when the prior
         * warm is NOT still marked in-flight. If [prevWarmInflight] is true the last
         * warm never completed (the process was killed mid-warm) -> do NOT auto-warm,
         * which breaks the crash/restart loop. The USER-INITIATED send path does not
         * consult this (a deliberate send is the manual retry).
         */
        fun shouldAutoWarm(prevWarmInflight: Boolean): Boolean = !prevWarmInflight

        private const val PREFS_NAME = "bbx_local_warm"
        private const val KEY_INFLIGHT = "warm_inflight"

        /** Build the SharedPreferences-backed store. The only Android-touching path. */
        fun fromContext(context: Context): WarmInflightStore =
            SharedPrefsWarmInflightStore(context.applicationContext)
    }

    /** Production store: the warm in-flight flag in a dedicated SharedPreferences file. */
    private class SharedPrefsWarmInflightStore(context: Context) : WarmInflightStore {
        private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        override fun isInflight(): Boolean = prefs.getBoolean(KEY_INFLIGHT, false)

        override fun setInflight(value: Boolean) {
            // commit() (synchronous) so the flag is on disk BEFORE load() begins -- an
            // apply() could still be buffered when a SIGKILL hits mid-warm, losing the
            // very signal this guard depends on.
            prefs.edit().putBoolean(KEY_INFLIGHT, value).commit()
        }
    }
}
