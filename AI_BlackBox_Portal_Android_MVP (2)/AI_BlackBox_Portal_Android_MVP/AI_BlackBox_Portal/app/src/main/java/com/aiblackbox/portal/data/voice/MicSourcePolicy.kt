package com.aiblackbox.portal.data.voice

import android.content.Context
import android.media.AudioManager
import android.media.MediaRecorder
import com.aiblackbox.portal.BuildConfig
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration

/**
 * A capture source this app will open a mic on, with the two things that must travel WITH it: the
 * platform constant it opens, and the [AuroraCalibration] its stream is read with.
 *
 * The calibration column is the containment: the analyser's constants were measured on a stream the
 * platform had already AGC-flattened, so a source that bypasses that processing cannot inherit them
 * (see [AuroraCalibration]). Keeping the pairing here means a new source cannot be added without
 * answering "and which calibration is that?".
 */
enum class MicSource(val androidSource: Int, val calibration: AuroraCalibration) {

    /**
     * The raw transducer — no AGC, no noise suppression, no platform gain shaping. The best input
     * a recogniser (or a spectrum ribbon) can be given, AND the one that must never be opened on
     * faith: see [MicSourcePolicy.supportsUnprocessed].
     */
    UNPROCESSED(MediaRecorder.AudioSource.UNPROCESSED, AuroraCalibration.UNPROCESSED),

    /** Platform-processed, tuned for recognisers: the fallback when UNPROCESSED is not real. */
    VOICE_RECOGNITION(MediaRecorder.AudioSource.VOICE_RECOGNITION, AuroraCalibration.DEFAULT),

    /** The generic source. Last resort — whatever the device decides "recording" means. */
    MIC(MediaRecorder.AudioSource.MIC, AuroraCalibration.DEFAULT),

    /**
     * The DUPLEX source: platform-processed AND echo-cancelled against what the loudspeaker is
     * playing. Only correct where the mic is open while audio is playing out of the same device.
     */
    VOICE_COMMUNICATION(MediaRecorder.AudioSource.VOICE_COMMUNICATION, AuroraCalibration.DEFAULT),
}

/**
 * Which platform audio effects a capture path attaches to its `AudioRecord` session.
 *
 * A table rather than inline `isAvailable()` calls at each site, because these are not local
 * conveniences: an effect changes the audio the ribbon draws AND the audio the recogniser hears,
 * and both call sites used to make that decision independently and silently.
 */
class MicEffects(val echoCanceler: Boolean, val noiseSuppressor: Boolean)

/**
 * WHICH MIC SOURCE, decided as a pure function of the one answer the platform actually gives.
 *
 * THE TRAP THIS EXISTS TO AVOID: `MediaRecorder.AudioSource.UNPROCESSED` FAILS SILENTLY. AOSP's
 * contract is that a device without unprocessed capture "behaves like DEFAULT otherwise" — the
 * `AudioRecord` constructs, initialises and records happily, it just hands back a processed stream
 * wearing the unprocessed name. So a try/catch ladder or an `AudioRecord.state` check can NEVER
 * detect it (OverlayService's recording path is exactly that shape, and it cannot). The only signal
 * that exists is `AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED`, and it is a string.
 *
 * That is why capability and openability are kept apart below: [supportsUnprocessed] decides
 * whether UNPROCESSED is in the chain at all, and [openFirst] only ever walks past a source that
 * genuinely could not be opened — a hard failure with a state to check, which is a different
 * question with a different answer.
 */
object MicSourcePolicy {

    /**
     * The duplex path's source, named once. Not in [simplexChain] by construction: echo
     * cancellation is processing bought to solve a problem a simplex mic does not have.
     */
    val DUPLEX_SOURCE = MicSource.VOICE_COMMUNICATION

    /**
     * Does this device really implement unprocessed capture?
     *
     * @param property the raw `AudioManager.getProperty(PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)`
     *   string — `"true"`, `"false"`, or null on the many devices that never implemented it. Only
     *   an explicit yes counts; anything else is a device that has not said yes.
     */
    fun supportsUnprocessed(property: String?): Boolean = "true".equals(property, ignoreCase = true)

    /** [supportsUnprocessed] against the live platform. The API is 24+; minSdk is 26. */
    fun supportsUnprocessed(context: Context): Boolean {
        val am = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
        return supportsUnprocessed(
            am?.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)
        )
    }

    /**
     * Capture sources for a SIMPLEX mic (nothing is playing, so there is no echo to cancel), best
     * first. The first entry is the policy's choice; the rest exist only for a source that will not
     * open at all.
     *
     * @param forcedSource a [MicSource] name that pins the FIRST entry, for the W10 transcript A/B
     *   only (a debug APK built with `-PforceMicSource=NAME`). Empty in every normal build, and an
     *   unknown name is ignored, so the shipped chain is unchanged unless the A/B build asks.
     */
    fun simplexChain(
        unprocessedSupported: Boolean,
        forcedSource: String = BuildConfig.FORCE_MIC_SOURCE,
    ): List<MicSource> {
        val pinned = MicSource.entries.firstOrNull { it.name == forcedSource }
        if (pinned != null) {
            return listOf(pinned) + listOf(MicSource.VOICE_RECOGNITION, MicSource.MIC).filter { it != pinned }
        }
        return if (unprocessedSupported) {
            listOf(MicSource.UNPROCESSED, MicSource.VOICE_RECOGNITION, MicSource.MIC)
        } else {
            listOf(MicSource.VOICE_RECOGNITION, MicSource.MIC)
        }
    }

    /** The effects a path attaches for [source]. */
    fun effectsFor(source: MicSource): MicEffects = MicEffects(
        // Duplex only, and NOT optional there: the voice screen's mic stays open while the model
        // speaks through the loudspeaker, so without AEC the model transcribes its own voice the
        // moment the user barges in.
        echoCanceler = source == DUPLEX_SOURCE,
        // Nowhere. Noise suppression reshapes the spectrum the ribbon is drawing, and no path
        // depends on it — the composer mic never attached it at all, and the duplex path's AEC is
        // a separate effect that keeps working without it.
        noiseSuppressor = false,
    )

    /** A source that opened, and what it opened. */
    class OpenedMic<T : Any>(val source: MicSource, val record: T)

    /**
     * Open the first source in [chain] that [open] can produce a usable recorder for, and report
     * WHICH — the caller needs it, because the calibration travels with the source.
     *
     * [open] returns null for "this source did not open" (constructor threw, or the recorder came
     * back uninitialised) and must release anything it half-built before it does. Null here means
     * no source opened at all, which is a mic failure, not a fallback.
     */
    fun <T : Any> openFirst(chain: List<MicSource>, open: (MicSource) -> T?): OpenedMic<T>? {
        for (source in chain) {
            val opened = open(source) ?: continue
            return OpenedMic(source, opened)
        }
        return null
    }
}
