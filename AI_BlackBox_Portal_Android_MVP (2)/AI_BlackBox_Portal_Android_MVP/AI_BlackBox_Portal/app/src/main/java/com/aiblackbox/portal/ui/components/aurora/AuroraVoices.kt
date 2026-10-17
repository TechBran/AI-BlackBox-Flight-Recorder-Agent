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
 * Shared zero-energy BINS for a producer that is running but silent — the spectrum channel's twin
 * of [AURORA_SILENT_BANDS], and load bearing for exactly the same reason: `StateFlow` drops a value
 * that `equals` the one it holds and `FloatArray` compares by IDENTITY, so republishing THIS
 * instance makes an idle tick free while a fresh `FloatArray(24)` would recompose the screen at the
 * producer's rate through every silence.
 *
 * NEVER write to it. [AuroraBins] copies on construction, which is what makes one shared array safe.
 */
internal val AURORA_SILENT_BINS = FloatArray(AuroraAnalyser.BINS)

/**
 * One live voice's SPECTRUM — a speaker identity plus its [AuroraAnalyser.BINS] log-frequency bin
 * levels, as produced by [AuroraSpectrum] through [AudioAnalysisTap].
 *
 * A SECOND channel beside [AuroraVoice] rather than more floats inside it. AuroraVoice is exactly
 * [AuroraAnalyser.BANDS] wide by contract — sheet j follows band j, and the shipped ribbon's whole
 * geometry is built on that — so widening it would change the default look, which is not this
 * channel's to change. A style that wants spectral detail reads this; every style that does not is
 * untouched by its existence.
 *
 * Deliberately NOT a data class, for the reason spelled out on [AuroraVoice]: a `FloatArray`
 * property in a data class compares by identity, so equal spectra would recompose forever, while
 * one scratch array reused in place would compare EQUAL after its contents changed and the ribbon
 * would freeze. Content equality plus a defensive copy is the only combination that behaves, and
 * the copy also gets the bins safely from the audio thread that measured them to the frame thread
 * that draws them.
 *
 * VALUE RANGE. Bins arrive amplitude-calibrated and RAW (no auto-gain, no headroom, no gamma — see
 * [AudioAnalysisTap]); what [bin] hands a renderer is that number NaN-scrubbed and clamped to the
 * 0..1 display range every consumer downstream already assumes. Any perceptual curve belongs
 * BETWEEN the tap and this carrier, where the band channel's own calibration lives, not sprinkled
 * over the readers.
 */
class AuroraBins(val speaker: AuroraSpeaker, bins: FloatArray) {

    private val bins: FloatArray = bins.copyOf()

    /**
     * Level of bin [index], clamped to 0..1, and TOTAL — every index and every float value,
     * including NaN, produces a usable number, because this is read inside a draw loop.
     *
     * An index this spectrum does not carry reads 0, which is where it differs from
     * [AuroraVoice.band] and the difference is deliberate: a band is a SHEET that must be driven by
     * something, but a bin is a slice of the spectrum, and repeating the top bin upward would paint
     * sibilance the microphone never heard — the same lie the Nyquist rule drops bins to avoid.
     *
     * NaN is checked EXPLICITLY because `coerceIn` does not catch it (JLS 15.20.1); one NaN reaching
     * a history ring poisons every window average taken over it afterwards.
     */
    fun bin(index: Int): Float {
        if (index < 0 || index >= bins.size) return 0f
        val v = bins[index]
        return if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
    }

    /** True while any bin carries enough to be worth a frame. Mirrors [AuroraVoice.hasEnergy]. */
    internal fun hasEnergy(): Boolean {
        for (i in bins.indices) if (bin(i) > Aurora.ENERGY_FLOOR) return true
        return false
    }

    override fun equals(other: Any?): Boolean =
        this === other || (other is AuroraBins && speaker == other.speaker && bins.contentEquals(other.bins))

    override fun hashCode(): Int = 31 * speaker.hashCode() + bins.contentHashCode()

    override fun toString(): String = "AuroraBins($speaker, ${bins.contentToString()})"
}

/**
 * Build the spectrum list for one container from up to two live sources — [auroraVoices]'s twin,
 * with the same rule: null is ABSENCE (no spectrum for that speaker this frame), an array is
 * PRESENCE, all-zero included (a producer that is running but silent must keep its spectrum on
 * screen — that is what [AURORA_SILENT_BINS] is for). Order is HUMAN then AI, matching the voice
 * list.
 *
 * With ONE addition [auroraVoices] deliberately does not share: an array too short to BE a spectrum
 * is absence, not a silent spectrum. The two carriers read a missing index in opposite ways, and
 * each is right for what it carries — [AuroraVoice.band] spreads a short array over the remaining
 * sheets, because one RMS level legitimately drives all four, while [AuroraBins.bin] reads an index
 * it does not carry as 0, precisely so nothing paints sibilance the microphone never heard. So a
 * short bands array is a coarse measurement and a short bins array is mostly zeros pretending to be
 * one. Presence is what sends a style down its bin path; sending it there with nothing to read draws
 * a dead ribbon where the four bands would have drawn a live one.
 *
 * In practice this catches the empty array a call site's deferred-read lambda holds before its
 * producer starts (Composer's `recordingBins`, mirroring `recordingBands`), which is exactly the
 * case that must read as "no spectrum yet".
 */
fun auroraBins(human: FloatArray? = null, ai: FloatArray? = null): List<AuroraBins> {
    val h = human?.takeIf { isSpectrum(it) }
    val a = ai?.takeIf { isSpectrum(it) }
    if (h == null && a == null) return emptyList()
    val bins = ArrayList<AuroraBins>(2)
    if (h != null) bins.add(AuroraBins(AuroraSpeaker.HUMAN, h))
    if (a != null) bins.add(AuroraBins(AuroraSpeaker.AI, a))
    return bins
}

/** Wide enough to be a reading of the whole map. A WIDER array is fine — consumers read the first
 *  [AuroraAnalyser.BINS] of it — but a narrower one is missing the top of the spectrum. */
private fun isSpectrum(values: FloatArray): Boolean = values.size >= AuroraAnalyser.BINS

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
