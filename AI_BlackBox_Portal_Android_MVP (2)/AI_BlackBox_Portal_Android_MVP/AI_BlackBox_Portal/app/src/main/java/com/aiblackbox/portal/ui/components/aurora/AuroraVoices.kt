package com.aiblackbox.portal.ui.components.aurora

/**
 * The seam between the app's four audio sources and [AuroraWaveform].
 *
 * Every source ends up as "band energies for a speaker, or nothing at all", and every call site
 * gets that decision wrong in the same way if it is left to spell it out itself — passing a
 * zero-filled array for a stream that has STOPPED leaves a resting ribbon parked on screen, and
 * passing nothing for a stream that is merely QUIET makes the ribbon vanish between words. Null is
 * absence, an array (even an all-zero one) is presence.
 */

/**
 * Shared zero-energy bands for a producer that is running but silent.
 *
 * The single instance is load bearing, not a micro-optimisation: producers publish these through a
 * `StateFlow`, `StateFlow` drops a value that `equals` the one it already holds, and `FloatArray`
 * compares by IDENTITY. Re-publishing THIS instance therefore conflates, so an idle 30 Hz "still
 * silent" tick costs nothing; a fresh `FloatArray(4)` each time would emit all thirty and recompose
 * the screen thirty times a second while nobody is speaking.
 *
 * NEVER write to it. Consumers only ever read (and [AuroraVoice] copies on construction), which is
 * what makes sharing one array across every producer safe.
 */
internal val AURORA_SILENT_BANDS = FloatArray(AuroraAnalyser.BANDS)

/**
 * Build the voice list for one [AuroraWaveform] from up to two live sources.
 *
 * @param human band energies from the microphone, or null when no mic stream is running.
 * @param ai band energies from the model's audio, or null when nothing is playing.
 *
 * Both may be non-null at once — that is the voice screen, and the reason the analysers behind them
 * are separate instances. Order is HUMAN then AI so a container's list is stable across frames; the
 * renderer interleaves by layer regardless, so this is for the caller's benefit, not the painter's.
 */
fun auroraVoices(human: FloatArray? = null, ai: FloatArray? = null): List<AuroraVoice> {
    if (human == null && ai == null) return emptyList()
    val voices = ArrayList<AuroraVoice>(2)
    if (human != null) voices.add(AuroraVoice(AuroraSpeaker.HUMAN, human))
    if (ai != null) voices.add(AuroraVoice(AuroraSpeaker.AI, ai))
    return voices
}
