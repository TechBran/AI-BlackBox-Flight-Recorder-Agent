package com.aiblackbox.portal.data.local

import android.content.Context
import com.aiblackbox.portal.overlay.AutonomyMode

/**
 * Local, on-device persistence of the device AUTONOMY POSTURE (Phase 4, Task 4.6).
 *
 * The autonomy mode (YOLO vs Permission) is chosen by the user via the Task 1.5
 * toggle, which POSTs it to the hub (`POST /local/device/autonomy`). But the
 * on-device phone-control agent runs LOCALLY and must know the mode WITHOUT a
 * network round-trip every action — and must fail SAFE (gate) when it has never
 * been set or can't be read. So we mirror the chosen mode into a tiny SharedPref
 * here when the toggle is flipped, and the actuator gate reads it back.
 *
 * ## Fail-safe default
 * [load] returns [AutonomyMode.PERMISSION] (the SAFE, gating posture) whenever no
 * value has been stored — first run, a cleared pref, or any unrecognized value.
 * The agent must NEVER silently default to YOLO. (The [com.aiblackbox.portal.overlay.Actuators]
 * constructor default of YOLO is only for un-wired call-sites/tests; the
 * production reader supplied through here defaults to PERMISSION.)
 *
 * ## Pattern
 * Mirrors [PersonaCache]/[PersonaStore]: a tiny seam ([AutonomyStore]) with the
 * SINGLE Android-touching implementation ([SharedPrefsAutonomyStore]) confined to
 * [fromContext]; unit tests substitute an in-memory fake.
 */
interface AutonomyStore {
    /** The persisted posture, or [AutonomyMode.PERMISSION] (safe default) if none/unknown. */
    fun load(): AutonomyMode

    /** Persist [mode] as the device's autonomy posture, replacing any prior. */
    fun save(mode: AutonomyMode)

    companion object {
        /** Canonical wire string for [AutonomyMode.YOLO] (matches the backend + Task 1.5). */
        const val WIRE_YOLO = "yolo"

        /** Canonical wire string for [AutonomyMode.PERMISSION] (matches the backend + Task 1.5). */
        const val WIRE_PERMISSION = "permission"

        /**
         * Map a wire mode string ("yolo"/"permission", case-insensitive) to an
         * [AutonomyMode]. Anything else — including null/blank — maps to the SAFE
         * [AutonomyMode.PERMISSION]. Pure; shared by the store + any caller that
         * only has the string form (e.g. the Task 1.5 toggle).
         */
        fun parse(wire: String?): AutonomyMode =
            if (wire?.trim()?.lowercase() == WIRE_YOLO) AutonomyMode.YOLO else AutonomyMode.PERMISSION

        /** The wire string for [mode] (inverse of [parse]). */
        fun wireOf(mode: AutonomyMode): String =
            if (mode == AutonomyMode.YOLO) WIRE_YOLO else WIRE_PERMISSION

        private const val PREFS_NAME = "bbx_autonomy"
        private const val KEY_MODE = "mode"

        /** Build the SharedPreferences-backed store. The only Android-touching path. */
        fun fromContext(context: Context): AutonomyStore =
            SharedPrefsAutonomyStore(context.applicationContext)
    }

    /** Production store: the device posture in a dedicated SharedPreferences file. */
    private class SharedPrefsAutonomyStore(context: Context) : AutonomyStore {
        private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        // No stored value (null) → parse(null) → PERMISSION (safe default).
        override fun load(): AutonomyMode = parse(prefs.getString(KEY_MODE, null))

        override fun save(mode: AutonomyMode) {
            prefs.edit().putString(KEY_MODE, wireOf(mode)).apply()
        }
    }
}
