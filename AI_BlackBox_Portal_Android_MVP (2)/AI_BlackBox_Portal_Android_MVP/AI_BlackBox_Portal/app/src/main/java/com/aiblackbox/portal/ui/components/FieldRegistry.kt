package com.aiblackbox.portal.ui.components

// =============================================================================
// FieldRegistry — THE catalogue of particle-field effects, and the ONE place an
// effect is registered. The Kotlin twin of the web engine's
//   Portal/modules/fx/registry.js
//
// Before this file the same effect list was written out FOUR times:
// ParticleMode.ALL, ParticleMode.parse, ParticleMode.label and newFieldSim —
// plus a fifth, the `when (sim)` draw switch in ParticleField.kt. Adding an
// effect meant editing five places, and forgetting one produced a look you could
// select but not persist (or could persist but not draw), silently. Now:
//
//   ONE list  →  ALL, parse, label, newFieldSim all derive from it.
//   The draw switch is gone entirely (FieldSim.render — see ParticleField.kt).
//
// Adding an effect is: write YourField.kt, add ONE line to [CATALOGUE].
// The full recipe is in ADDING_AN_EFFECT.md, next to this file.
// =============================================================================

/**
 * One entry in the catalogue.
 *
 * @property id      the PERSISTED value. Byte-identical forever — it is written
 *                   to DataStore (`particle_mode`) and read back by every future
 *                   build. Lowercase, no spaces. Never rename one.
 * @property label   the human string the settings picker shows.
 * @property create  builds a fresh sim. `countScale` is the intensity × quality
 *                   particle-count multiplier (see ParticleTuning); `rand` is the
 *                   injectable randomness seam (see FieldRandom). Both are
 *                   constructor-time by design: the budget is baked into pool
 *                   sizes, so a change rebuilds the sim rather than mutating it.
 */
class FieldEffect(
    val id: String,
    val label: String,
    val create: (countScale: Float, rand: FieldRandom) -> FieldSim,
)

object FieldEffects {

    // =========================================================================
    // ═══════════════════ THE REGISTRATION POINT ═══════════════════
    // Add your effect HERE and nowhere else. Order is DISPLAY ORDER in the
    // settings picker; the first entry is also the fallback for an unknown or
    // absent stored value, so `stars` stays first.
    // =========================================================================
    val CATALOGUE: List<FieldEffect> = listOf(
        FieldEffect(ParticleMode.STARS, "Rising Stars") { n, _ -> StarSim(n) },
        FieldEffect(ParticleMode.EMBERS, "Embers") { n, _ -> EmberSim(n) },
        FieldEffect(ParticleMode.MATRIX, "Matrix") { n, _ -> MatrixSim(n) },
        FieldEffect(ParticleMode.FIREFLIES, "Fireflies") { n, r -> FirefliesSim(n, r) },
        FieldEffect(ParticleMode.SLIPSTREAM, "Slipstream") { n, r -> SlipstreamSim(n, r) },
        FieldEffect(ParticleMode.BOKEH, "Bokeh Depth") { n, r -> BokehSim(n, r) },
        FieldEffect(ParticleMode.METEORS, "Meteor Fall") { n, r -> MeteorsSim(n, r) },
        FieldEffect(ParticleMode.CINDERS, "Cinders on the Wind") { n, r -> CindersSim(n, r) },
        FieldEffect(ParticleMode.SNOW, "Drift Snow") { n, r -> SnowSim(n, r) },
        FieldEffect(ParticleMode.RAIN, "Rainfall") { n, r -> RainSim(n, r) },
        FieldEffect(ParticleMode.FOG, "Low Fog") { n, r -> FogSim(n, r) },
        FieldEffect(ParticleMode.AURORA, "Aurora Curtain") { n, r -> AuroraSim(n, r) },
        FieldEffect(ParticleMode.LEDGER, "Ledger Rain") { n, r -> LedgerSim(n, r) },
    )

    /** The fallback effect — first in the catalogue, i.e. Rising Stars. */
    val default: FieldEffect get() = CATALOGUE[0]

    private val byId: Map<String, FieldEffect> = CATALOGUE.associateBy { it.id }

    /** Persisted ids in display order. THE whitelist; nothing else may hard-code one. */
    val ids: List<String> = CATALOGUE.map { it.id }

    /**
     * Coerce any stored / legacy / newer-build / garbage value onto a real effect.
     * A build that has never heard of an id MUST fall back rather than crash —
     * that is the whole reason this is the only lookup in the codebase.
     */
    fun resolve(id: String?): FieldEffect =
        byId[id?.trim()?.lowercase()] ?: default
}

// =============================================================================
// ParticleMode — the persisted FIELD look. The constants stay (call sites and
// tests reference them by name), but ALL / parse / label are now DERIVED from
// FieldEffects.CATALOGUE instead of restating it.
//
// Orthogonal to EmberMode, which governs OFF/GENERATING/ALWAYS visibility.
// The CompositionLocal that carries it (LocalParticleMode) lives in
// ParticleField.kt with the rest of the engine.
// =============================================================================
object ParticleMode {
    // ── Shipped ids. PERSISTENCE CONTRACT: never change these strings. ──
    const val STARS = "stars"
    const val EMBERS = "embers"
    const val MATRIX = "matrix"
    const val FIREFLIES = "fireflies"
    const val SLIPSTREAM = "slipstream"
    const val BOKEH = "bokeh"
    const val METEORS = "meteors"
    const val CINDERS = "cinders"
    const val SNOW = "snow"
    const val RAIN = "rain"
    const val FOG = "fog"
    const val AURORA = "aurora"
    const val LEDGER = "ledger"

    /** Persisted values in preferred display order (drives the settings picker). */
    val ALL: List<String> get() = FieldEffects.ids

    /** Normalize any stored/legacy/unknown value to a known mode; unknown/null → STARS. */
    fun parse(raw: String?): String = FieldEffects.resolve(raw).id

    /** Human label for the picker. */
    fun label(mode: String?): String = FieldEffects.resolve(mode).label
}

/**
 * Build the sim for a mode. Unknown modes resolve to the default (STARS).
 *
 * [countScale] is the combined intensity × quality particle-count multiplier
 * (see ParticleTuning). It is a CONSTRUCTOR parameter, not mutable state: the
 * budget is baked into pool sizes at spawn, so EmberOverlay keys `remember` on it
 * and a change builds a fresh sim — the same clean re-init a mode switch gets.
 * Defaults to 1.0 (today's counts) so every existing call site is unchanged.
 *
 * [rand] is the injectable randomness seam — production passes the default;
 * JVM tests pass a seeded generator to make an effect's physics reproducible.
 */
fun newFieldSim(
    mode: String,
    countScale: Float = 1f,
    rand: FieldRandom = SystemFieldRandom,
): FieldSim = FieldEffects.resolve(mode).create(countScale, rand)
