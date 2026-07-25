package com.aiblackbox.portal.ui.components

// =============================================================================
// ParticleTuning — the two COUNT dials for the particle field, and the
// CompositionLocals that carry them from the persisted settings down to
// EmberOverlay. Both are orthogonal to:
//   • EmberMode      — OFF / GENERATING / ALWAYS visibility (when it shows)
//   • ParticleMode   — the FIELD look (what it shows)
// These say HOW MUCH of it shows.
//
//   Intensity  a continuous user dial, 0.25×…2.0×, default 1.0.
//   Quality    a device tier, Low 0.5× / High 1.0× / Ultra 1.6×, default High.
//
// They multiply into ONE number — countScale — which every sim applies to its
// baseline particle budget. **At the defaults (1.0 × High) countScale is exactly
// 1.0 and scaleCount() is the identity**, so the shipped look is bit-for-bit
// what it was before this setting existed. That identity is unit-pinned.
//
// Everything here is plain Kotlin (no Android/Compose types beyond the two
// CompositionLocal declarations) so the maths is JVM-unit-testable — see
// ParticleTuningTest. The parse functions are also the STORAGE normalizers:
// DataStore can hand back an absent key, or a value written by an older/newer
// build, or (for a float) a NaN/∞ — none of those may crash, and none of them
// may ever resolve to zero particles.
// =============================================================================

import androidx.compose.runtime.staticCompositionLocalOf
import kotlin.math.roundToInt

/**
 * Quality tier — a particle-count multiplier so a weak device degrades
 * gracefully instead of dropping frames, and a strong one can be pushed.
 * Persisted as the lowercase string; unknown/absent → [DEFAULT] (High).
 */
object ParticleQuality {
    const val LOW = "low"
    const val HIGH = "high"
    const val ULTRA = "ultra"

    /** The shipped default — 1.0×, i.e. exactly today's field. */
    const val DEFAULT = HIGH

    /** Persisted values in display order (drives the settings picker generically). */
    val ALL = listOf(LOW, HIGH, ULTRA)

    /** Normalize any stored/legacy/garbage value to a known tier; unknown → HIGH. */
    fun parse(raw: String?): String = when (raw?.trim()?.lowercase()) {
        LOW -> LOW
        ULTRA -> ULTRA
        else -> HIGH
    }

    /** Human label for the picker. */
    fun label(quality: String?): String = when (parse(quality)) {
        LOW -> "Low"
        ULTRA -> "Ultra"
        else -> "High"
    }

    /** Count multiplier for the tier. HIGH is 1.0 — the identity, by contract. */
    fun multiplier(quality: String?): Float = when (parse(quality)) {
        LOW -> 0.5f
        ULTRA -> 1.6f
        else -> 1.0f
    }
}

object ParticleTuning {
    // ── Intensity: the user-facing slider ──
    const val INTENSITY_DEFAULT = 1.0f
    const val INTENSITY_MIN = 0.25f
    const val INTENSITY_MAX = 2.0f

    /** Slider granularity: 0.25 steps across [INTENSITY_MIN]..[INTENSITY_MAX]. */
    const val INTENSITY_STEP = 0.25f

    // ── The combined multiplier every sim consumes ──
    // Floored well above zero: a field with no particles is indistinguishable
    // from a broken field, so the dial can thin it out but never empty it.
    const val COUNT_SCALE_MIN = 0.1f
    const val COUNT_SCALE_MAX = 3.0f

    /**
     * Normalize a stored intensity. Absent (null), NaN or ±∞ → [INTENSITY_DEFAULT];
     * anything else is clamped into the legal range. Never throws.
     */
    fun parseIntensity(raw: Float?): Float {
        if (raw == null || raw.isNaN() || raw.isInfinite()) return INTENSITY_DEFAULT
        return raw.coerceIn(INTENSITY_MIN, INTENSITY_MAX)
    }

    /** Same contract for a value that arrived as text (legacy/exported prefs). */
    fun parseIntensity(raw: String?): Float =
        parseIntensity(raw?.trim()?.takeIf { it.isNotEmpty() }?.toFloatOrNull())

    /**
     * Clamp an already-combined multiplier. NaN/∞ → 1.0 (today's look) rather
     * than the range floor: a corrupt value must degrade to the DEFAULT, not to
     * the sparsest field we can draw.
     */
    fun sanitizeScale(raw: Float): Float {
        if (raw.isNaN() || raw.isInfinite()) return 1.0f
        return raw.coerceIn(COUNT_SCALE_MIN, COUNT_SCALE_MAX)
    }

    /**
     * The one number the sims take: intensity × quality, clamped and quantized to
     * 2 decimals (so dragging the slider can't re-key the sim on float noise).
     * `countScale(1.0f, "high") == 1.0f` exactly — the pinned identity.
     */
    fun countScale(intensity: Float?, quality: String?): Float {
        val combined = parseIntensity(intensity) * ParticleQuality.multiplier(quality)
        return quantize(sanitizeScale(combined))
    }

    /**
     * Apply a count scale to a baseline particle budget.
     * Guarantees ≥ 1 for any positive baseline — never 0, never negative.
     */
    fun scaleCount(base: Int, scale: Float): Int {
        if (base <= 0) return 0
        return (base * sanitizeScale(scale)).roundToInt().coerceAtLeast(1)
    }

    /** "100%" — the caption next to the slider. */
    fun intensityLabel(intensity: Float?): String =
        "${(parseIntensity(intensity) * 100f).roundToInt()}%"

    private fun quantize(v: Float): Float = (v * 100f).roundToInt() / 100f
}

/** Provided once at the activity root from `particle_intensity`; read by EmberOverlay. */
val LocalParticleIntensity = staticCompositionLocalOf { ParticleTuning.INTENSITY_DEFAULT }

/** Provided once at the activity root from `particle_quality`; read by EmberOverlay. */
val LocalParticleQuality = staticCompositionLocalOf { ParticleQuality.DEFAULT }
