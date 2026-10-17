package com.aiblackbox.portal.ui.components.wave

import androidx.compose.runtime.Composable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.aurora.AuroraWaveform
import com.aiblackbox.portal.ui.components.wave.styles.cinematicRenderer
import com.aiblackbox.portal.ui.components.wave.styles.classicRenderer
import com.aiblackbox.portal.ui.components.wave.styles.mirroredPulseRenderer
import com.aiblackbox.portal.ui.components.wave.styles.orbRenderer
import com.aiblackbox.portal.ui.components.wave.styles.particleWaveRenderer
import com.aiblackbox.portal.ui.components.wave.styles.ribbonRenderer
import com.aiblackbox.portal.ui.components.wave.styles.segmentedEqRenderer
import com.aiblackbox.portal.ui.components.wave.styles.sharpLineRenderer
import com.aiblackbox.portal.ui.components.wave.styles.softBlobRenderer

// =============================================================================
// WaveformRegistry — THE catalogue of selectable waveform styles, and the ONE
// place a style is registered. The exact twin of the particle FieldRegistry:
//
//   ONE list  →  the settings picker, the persisted-id whitelist, and the
//   VoiceVisualizer dispatch all derive from it. There is no second copy of the
//   style list anywhere, so adding a style cannot leave a look you can select
//   but not draw (or persist but not select).
//
// Adding/finishing a style is: write (or fill in) styles/YourStyle.kt, then add
// ONE line to [CATALOGUE]. Nothing else in the codebase hard-codes a style id.
//
// PERSISTED IDS ARE FOREVER (the FieldRegistry rule). Each `id` is the exact
// string written to DataStore (`waveform_style`) and read back by every future
// build — lowercase, no spaces, NEVER renamed. `aurora` is FIRST, which makes it
// both the default AND the fallback for any unknown/absent/garbage stored value,
// so an existing user who has never touched the setting keeps today's ribbon.
// =============================================================================

/**
 * How a style is realised. Two genuinely different kinds, which is why this is a
 * sealed hierarchy rather than one lambda:
 *
 *  - [Draw]: a per-frame [WaveformRenderer] driven by VoiceVisualizer's shared
 *    feed + frame loop. Classic and the eight reference-sheet styles are these —
 *    all look, no plumbing.
 *  - [SelfContained]: a whole composable that owns its OWN engine and frame loop.
 *    Aurora alone is this: wrapping the shipped [AuroraWaveform] verbatim is what
 *    keeps the default byte-for-byte identical to today rather than a
 *    re-implementation that has to be proven equal.
 */
sealed interface WaveformContent {

    /** A draw renderer; VoiceVisualizer owns the feed and the loop and calls it each frame. */
    class Draw(val create: () -> WaveformRenderer) : WaveformContent

    /**
     * A self-contained composable that draws itself, loop and all. It receives the
     * same four things AuroraWaveform's call sites pass today, so the wrapper is a
     * pure forwarding: [voices], the [Modifier], `pauseWhenIdle`, and `restLevel`.
     */
    class SelfContained(
        val content: @Composable (
            voices: List<AuroraVoice>,
            modifier: Modifier,
            pauseWhenIdle: Boolean,
            restLevel: Float,
        ) -> Unit,
    ) : WaveformContent
}

/**
 * One entry in the catalogue.
 *
 * @property id     the PERSISTED value. Byte-identical forever — written to
 *                  DataStore (`waveform_style`) and read back by every future
 *                  build. Lowercase, no spaces. Never rename one.
 * @property label  the human string the settings picker shows.
 * @property content how the style draws (see [WaveformContent]).
 */
class WaveformStyleEntry(
    val id: String,
    val label: String,
    val content: WaveformContent,
)

object WaveformStyles {

    // ── Shipped ids. PERSISTENCE CONTRACT: never change these strings. ──
    const val AURORA = "aurora"
    const val CLASSIC = "classic"
    const val RIBBON = "ribbon"
    const val MIRRORED_PULSE = "mirrored_pulse"
    const val SEGMENTED_EQ = "segmented_eq"
    const val SOFT_BLOB = "soft_blob"
    const val SHARP_LINE = "sharp_line"
    const val ORB = "orb"
    const val PARTICLE_WAVE = "particle_wave"
    const val CINEMATIC = "cinematic"

    // =========================================================================
    // ═══════════════════ THE REGISTRATION POINT ═══════════════════
    // Add your style HERE and nowhere else. Order is DISPLAY ORDER in the
    // settings picker; the first entry (aurora) is ALSO the fallback for an
    // unknown/absent stored value, so it stays first.
    // =========================================================================
    val CATALOGUE: List<WaveformStyleEntry> = listOf(
        // Aurora — the shipped default, wrapped verbatim so it is pixel-identical.
        WaveformStyleEntry(
            AURORA, "Aurora Bands",
            WaveformContent.SelfContained { voices, modifier, pauseWhenIdle, restLevel ->
                AuroraWaveform(
                    voices = voices,
                    modifier = modifier,
                    pauseWhenIdle = pauseWhenIdle,
                    restLevel = restLevel,
                )
            },
        ),
        // Classic — the pre-aurora VoiceWaveform scope, adapted to the feed. Real.
        draw(CLASSIC, "Classic Lines") { classicRenderer() },
        // The eight reference-sheet styles, each a real renderer in wave/styles/. A style's
        // look changes in its own file; its registration line here never moves.
        draw(RIBBON, "Glowing Ribbon") { ribbonRenderer() },
        draw(MIRRORED_PULSE, "Mirrored Pulse") { mirroredPulseRenderer() },
        draw(SEGMENTED_EQ, "Segmented Equalizer") { segmentedEqRenderer() },
        draw(SOFT_BLOB, "Soft Blob Wave") { softBlobRenderer() },
        draw(SHARP_LINE, "Sharp Reactive Line") { sharpLineRenderer() },
        draw(ORB, "Circular Orb") { orbRenderer() },
        draw(PARTICLE_WAVE, "Particle Wave") { particleWaveRenderer() },
        draw(CINEMATIC, "Cinematic Glow") { cinematicRenderer() },
    )

    /** The fallback style — first in the catalogue, i.e. Aurora. */
    val default: WaveformStyleEntry get() = CATALOGUE[0]

    private val byId: Map<String, WaveformStyleEntry> = CATALOGUE.associateBy { it.id }

    /** Persisted ids in display order. THE whitelist; nothing else may hard-code one. */
    val ids: List<String> = CATALOGUE.map { it.id }

    /** Human label for the picker; unknown → the default's label. */
    fun label(id: String?): String = resolve(id).label

    /**
     * Coerce any stored / legacy / newer-build / garbage value onto a real style.
     * A build that has never heard of an id MUST fall back rather than crash —
     * the whole reason this is the only lookup in the codebase.
     */
    fun resolve(id: String?): WaveformStyleEntry =
        byId[id?.trim()?.lowercase()] ?: default

    /** Normalise any stored/legacy/unknown value to a known id; unknown/null → aurora. */
    fun parse(raw: String?): String = resolve(raw).id

    /** Terse [WaveformContent.Draw] entry constructor for the registration list. */
    private fun draw(
        id: String,
        label: String,
        create: () -> WaveformRenderer,
    ): WaveformStyleEntry = WaveformStyleEntry(id, label, WaveformContent.Draw(create))
}

/**
 * The persisted global waveform style, carried down the tree exactly like
 * [com.aiblackbox.portal.ui.components.LocalParticleMode] carries the field look.
 *
 * NativeMainActivity collects `store.waveformStyle` and provides it here once; the
 * three call sites read `LocalWaveformStyle.current` and hand it to
 * [VoiceVisualizer]. Default is the persisted default id (`aurora`), so a
 * composable rendered outside the provider (a preview, a test host) still resolves
 * to the shipped look rather than crashing.
 */
val LocalWaveformStyle = staticCompositionLocalOf { WaveformStyles.AURORA }
