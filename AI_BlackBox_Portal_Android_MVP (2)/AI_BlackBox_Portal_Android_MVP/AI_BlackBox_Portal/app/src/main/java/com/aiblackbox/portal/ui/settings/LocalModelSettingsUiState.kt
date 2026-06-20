package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.data.local.LiteRtEngine
import com.aiblackbox.portal.data.local.resolveMaxTokens
import com.aiblackbox.portal.ui.chat.LocalEngineState

// ===========================================================================
// PURE mappers behind [LocalModelSettingsScreen].
//
// The Composable is not unit-tested on the JVM gate, so every decision/format
// choice lives here as a pure function (primitives + the engine constants only,
// no Android, no Compose) and the screen just applies it. Mirrors the
// LocalModelSection / LiteRtMappers convention. Tested by LocalModelSettingsTest.
//
// All thresholds/ranges are read from [LiteRtEngine] -- never hardcoded -- so a
// future GPU-safety re-tune flows through automatically.
// ===========================================================================

/**
 * The context-window WARNING for a chosen [value], or null when none is needed.
 *
 * [LiteRtEngine.DEFAULT_MAX_TOKENS] (6144) is the RECOMMENDED, GPU-survivable
 * window; at or below it there is no warning. ABOVE it the user owns the OOM risk
 * (the value is still HONORED -- [resolveMaxTokens] only refuses the absurd), so we
 * surface a clear caution that mentions the recommended threshold.
 */
fun windowWarning(value: Int): String? =
    if (value > LiteRtEngine.DEFAULT_MAX_TOKENS) {
        "Above ${LiteRtEngine.DEFAULT_MAX_TOKENS} may run out of memory on GPU " +
            "(the model falls back to slower CPU, or the load can fail)."
    } else {
        null
    }

/**
 * Friendly, user-facing label for the on-device engine's load state -- the
 * "loaded?" line in the status readout. Each [LocalEngineState] maps to a distinct
 * non-blank string (asserted by the tests).
 */
fun engineStatusLabel(state: LocalEngineState): String = when (state) {
    LocalEngineState.IDLE -> "Not loaded (loads on first send)"
    LocalEngineState.WARMING -> "Loading model into memory..."
    LocalEngineState.READY -> "Ready (model loaded)"
    LocalEngineState.ERROR -> "Failed to load (error)"
}

/**
 * Human-readable free-RAM headroom from a raw available-bytes value (e.g.
 * `ActivityManager.MemoryInfo.availMem`). Delegates to the shared [humanBytes]
 * formatter so MB/GB rounding matches the model-size rows. Never blank.
 */
fun formatFreeRam(availBytes: Long): String = humanBytes(availBytes.coerceAtLeast(0L))

/**
 * Clamp a slider value into the engine's sane, GPU-honored range. This is exactly
 * [resolveMaxTokens] (MIN_TOKENS..ABSOLUTE_MAX_TOKENS) -- NOT a re-implementation
 * that could drift -- so the slider and the engine agree on the effective window.
 */
fun clampWindow(value: Int): Int = resolveMaxTokens(value)
