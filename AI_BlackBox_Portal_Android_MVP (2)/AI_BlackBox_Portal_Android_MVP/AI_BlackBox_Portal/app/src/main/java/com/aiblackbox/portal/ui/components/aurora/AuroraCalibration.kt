package com.aiblackbox.portal.ui.components.aurora

import kotlin.math.ln
import kotlin.math.pow

/**
 * One [AuroraAnalyser] dynamics calibration — the whole measured set, named, so it can be swapped
 * as a set.
 *
 * WHY A SET AND NOT A DOZEN LOOSE CONSTANTS. Every number below was measured against a stream the
 * platform had ALREADY flattened: a Fold 6 speaker-to-mic loop through VOICE_COMMUNICATION, i.e.
 * through the device's own AGC and noise suppression. The band seeds and floors describe that
 * stream's levels, NOISE_LIVE describes its residual hiss and noiseSubtract how much of that hiss
 * comes off before anything is normalised, and HEADROOM/the knee place that stream's typical
 * speech under the ceiling. Open the mic on a source that bypasses the platform's processing and
 * all of them are invalidated at once — not one at a time, and not by an amount any of them can be
 * corrected for individually. So the swap is a profile, and the profile is per SOURCE.
 *
 * [DEFAULT] is the shipped calibration, value for value (see AuroraCalibrationTest, which pins that
 * against [AuroraAnalyser]'s own constants). The constants stay where they are, with the
 * measurements that produced them written beside them; this type names WHICH SET a stream is being
 * read with, it does not restate the numbers.
 *
 * WHAT IS DELIBERATELY NOT IN HERE: the perceptual curve. A profile describes a SOURCE — what this
 * microphone, at this distance, through this platform's processing, puts into the analyser. The
 * curve that turns normalised energy into drawn height describes the EYE, is the same for every
 * source, and lives in exactly one place for the whole app ([Aurora.DISPLAY_GAMMA], applied by the
 * renderer). An earlier revision carried `gamma` here; it was the analyser's half of a lift the
 * renderer was applying anyway, so the live chain compressed at 0.41 and no profile could have
 * fixed that, because the duplication was structural rather than per-source.
 *
 * @param id names the stream this profile describes; it appears in test failures and nowhere else.
 */
class AuroraCalibration(
    val id: String,
    val gains: FloatArray = AuroraAnalyser.GAINS,
    val peakInit: FloatArray = AuroraAnalyser.PEAK_INIT,
    val peakFloor: FloatArray = AuroraAnalyser.PEAK_FLOOR,
    val noiseLive: FloatArray = AuroraAnalyser.NOISE_LIVE,
    val noiseSubtract: Float = AuroraAnalyser.NOISE_SUBTRACT,
    val headroom: Float = AuroraAnalyser.HEADROOM,
    val knee: Float = AuroraAnalyser.KNEE,
    val riseBlend: Float = AuroraAnalyser.RISE_BLEND,
    val acquireTauSec: Float = AuroraAnalyser.ACQUIRE_TAU_SEC,
    val steadyRiseTauSec: Float = AuroraAnalyser.STEADY_RISE_TAU_SEC,
    val steadyDecay: Float = AuroraAnalyser.STEADY_DECAY,
    val steadyDecayRefSec: Float = AuroraAnalyser.STEADY_DECAY_REF_SEC,
    val fastReleaseTauSec: Float = AuroraAnalyser.FAST_RELEASE_TAU_SEC,
) {

    init {
        require(gains.size == AuroraAnalyser.BANDS) { "$id: gains must be ${AuroraAnalyser.BANDS} wide" }
        require(peakInit.size == AuroraAnalyser.BANDS) { "$id: peakInit must be ${AuroraAnalyser.BANDS} wide" }
        require(peakFloor.size == AuroraAnalyser.BANDS) { "$id: peakFloor must be ${AuroraAnalyser.BANDS} wide" }
        require(noiseLive.size == AuroraAnalyser.BANDS) { "$id: noiseLive must be ${AuroraAnalyser.BANDS} wide" }
        require(steadyDecay > 0f && steadyDecay < 1f) { "$id: steadyDecay must be a per-chunk fraction" }
        // A negative depth would ADD the floor back rather than remove it — an amplifier for the
        // room, which is the defect the expander exists to delete. Zero is legal and means "this
        // source has no floor worth removing" (a synthesised stream), so the lower bound is not
        // strict.
        require(noiseSubtract >= 0f) { "$id: noiseSubtract must not be negative, was $noiseSubtract" }
        // The whole point of the second reference is that it is FASTER than the first. A profile
        // where it is not has not been re-measured, it has been mistyped: the session reference
        // would then be chasing a follower slower than itself and the pair would behave as one.
        require(fastReleaseTauSec < steadyRiseTauSec) {
            "$id: the fast follower ($fastReleaseTauSec s) must release faster than the session " +
                "reference rises ($steadyRiseTauSec s)"
        }
        require(fastReleaseTauSec > 0f) { "$id: the follower release must be positive" }
    }

    /**
     * [steadyDecay] as a time constant — derived, never written out, so the identity
     * `exp(-steadyDecayRefSec / steadyDecayTauSec) == steadyDecay` holds by construction in every
     * profile. See [AuroraAnalyser.STEADY_DECAY_TAU_SEC] for what that release is and why it is
     * anchored to a chunk size the analyser no longer requires anyone to feed it.
     */
    val steadyDecayTauSec: Float = -steadyDecayRefSec / ln(steadyDecay)

    companion object {

        /**
         * The shipped calibration: a platform-PROCESSED capture (VOICE_COMMUNICATION or
         * VOICE_RECOGNITION, both AGC-flattened), and the AI playback stream, which arrives already
         * levelled by the model's own synthesis. Byte-identical to what the analyser has always
         * done — this is the profile every stream used before per-source profiles existed, and
         * moving any number in it is a re-tune of the shipped ribbon. That includes the expander
         * added for the bare-transducer sources: its depth is ZERO here, so these streams are
         * analysed exactly as they were, and [AuroraAnalyser.NOISE_SUBTRACT] records the measurement
         * that says they have to be.
         */
        val DEFAULT = AuroraCalibration("processed")

        /**
         * THE ROOM, as a number an operator can re-measure: the RMS level of pink room tone that a
         * BARE transducer reads in an ordinary quiet room, in dBFS. Everything about [UNPROCESSED]'s
         * floor is derived from this one constant, so re-measuring the room is a one-line change.
         *
         * WHERE -40 COMES FROM, given that nobody has yet recorded a bare Fold mic in the operator's
         * room. It is bounded from both sides by the one piece of on-device evidence in the file.
         * Pink noise at -40 dBFS measures [PINK_BAND_ENERGY_AT_MINUS_40_DBFS] through this
         * analyser's own probes, and the processed profile's `NOISE_LIVE[0] = 0.006` is 1.26x of
         * that — i.e. that guard describes a -38 dBFS pink room. But it is a guard, sized 2-3x above
         * the floor it was measured against (see [HISS_GUARD_INFLATION]), so the floor it was
         * measured against was nearer -46 dBFS, and a transducer with the platform's processing
         * bypassed sits somewhere ABOVE that and below the guard. -40 dBFS is inside that bracket.
         *
         * IT IS ALSO THE CHOICE WITH MARGIN WHERE MARGIN MATTERS. The number this stage exists to
         * clear is pink room tone at -45 dBFS, which the low band rejects with 1.75x to spare here,
         * while every dB of assumed room is paid for in quiet speech — a voice whose band energy
         * lands under the assumption cannot be told from the room it is under, by this or any other
         * subtraction. Erring quiet costs rejection in a room nobody has measured; erring loud costs
         * the quietest talker the ribbon has to draw.
         *
         * TO RE-MEASURE IT: record the room through `AudioSource.UNPROCESSED` with nobody speaking
         * and read the RMS in dBFS. Raising it rejects a louder room and eats more quiet speech
         * (measured: ~1.5% of full-gain drawn height per 2 dB, and rather more of a quiet talker's);
         * lowering it does the reverse. A calibration decision to be made against a measurement of a
         * real room — not a knob to turn until the ribbon looks calm.
         */
        internal const val UNPROCESSED_ROOM_TONE_DBFS = -40f

        /**
         * Per-band energy of PINK noise at -40 dBFS RMS, measured through [AuroraAnalyser]'s four
         * Goertzel probes at 16 kHz (the composer path's rate) with [AuroraAnalyser.GAINS] applied —
         * i.e. in the same units [AuroraAnalyser.NOISE_LIVE] is in. Room tone is pink, not white:
         * the energy FALLS with frequency, which is the opposite of the hiss shape below, and is why
         * the two have to be combined per band rather than scaled as one.
         *
         * Anchored at -40 dBFS because that is where it was measured; [pinkFloorAt] moves it.
         */
        private val PINK_BAND_ENERGY_AT_MINUS_40_DBFS =
            floatArrayOf(0.0049f, 0.0032f, 0.0031f, 0.0029f)

        /**
         * How much margin the processed profile's [AuroraAnalyser.NOISE_LIVE] carries OVER the floor
         * it was measured against — its own note says "sized ~2-3x the measured live noise band
         * energy". Dividing it back out recovers an estimate of the transducer's own HISS, which is
         * the part of the floor that does not go away when the platform stops processing the stream
         * (and, with no noise suppressor attached anywhere — see MicSourcePolicy.effectsFor — is
         * present in full). 2.5 is the middle of the stated range; it is an estimate of a margin,
         * not a measurement, and it is named here so it can be replaced by one.
         */
        private const val HISS_GUARD_INFLATION = 2.5f

        /** [PINK_BAND_ENERGY_AT_MINUS_40_DBFS] moved to [dbfs] — pink energy is linear in amplitude. */
        private fun pinkFloorAt(dbfs: Float): FloatArray {
            val scale = 10f.pow((dbfs + 40f) / 20f)
            return FloatArray(AuroraAnalyser.BANDS) { PINK_BAND_ENERGY_AT_MINUS_40_DBFS[it] * scale }
        }

        /**
         * The bare transducer's floor for a room at [dbfs]: the ROOM (pink at that level) or the
         * TRANSDUCER'S OWN HISS (the processed guard with its margin divided out), whichever is
         * louder in that band. Two different noises, each dominant somewhere: the room wins the low
         * band (pink tone is bass-heavy), the hiss wins the top two (it rises with frequency, and
         * [AuroraAnalyser.GAINS] lifts it further).
         *
         * A function, and [UNPROCESSED_NOISE_LIVE] its one evaluation, so the derivation is
         * executable rather than a comment beside a literal — AuroraCalibrationTest moves the room
         * and checks the floor moves with it, which is the only thing that makes
         * [UNPROCESSED_ROOM_TONE_DBFS] a control instead of decoration.
         */
        internal fun unprocessedNoiseLiveAt(dbfs: Float): FloatArray {
            val pink = pinkFloorAt(dbfs)
            return FloatArray(AuroraAnalyser.BANDS) {
                maxOf(pink[it], AuroraAnalyser.NOISE_LIVE[it] / HISS_GUARD_INFLATION)
            }
        }

        /** [unprocessedNoiseLiveAt] the assumed room: [0.0049, 0.0040, 0.0056, 0.0120]. */
        internal val UNPROCESSED_NOISE_LIVE: FloatArray =
            unprocessedNoiseLiveAt(UNPROCESSED_ROOM_TONE_DBFS)

        /**
         * ...and ALL of it comes off, where the processed profile removes NONE of its guard (see
         * [AuroraAnalyser.NOISE_SUBTRACT] for why those two numbers are so far apart: they multiply
         * quantities that mean different things, and only one of them is a floor). A floor derived
         * AS a floor can be subtracted as one, and the depth then states something checkable rather
         * than tuned — room tone AT the assumed level renders nothing, and every dB above it renders.
         */
        internal const val UNPROCESSED_NOISE_SUBTRACT = 1.0f

        /**
         * `MediaRecorder.AudioSource.UNPROCESSED` — the raw transducer, with the platform's AGC,
         * noise suppression and gain shaping all bypassed. On a device that really implements it
         * (a Fold 6 does) this is the COMPOSER and DICTATION mic; see MicSourcePolicy.
         *
         * IT IS NO LONGER [DEFAULT]'s NUMBERS. It carried them as containment while the source was
         * wired; this is the re-measurement that slot existed for, and it moves exactly two things —
         * the floor, which is now derived from a room rather than inherited from an AGC-flattened
         * stream, and the depth of the expander that removes it. Everything else (the gains, the
         * seeds, the time constants, HEADROOM, the knee) is unchanged and still shared, because
         * nothing measured about THOSE changes when the platform stops processing the stream.
         *
         * MEASURED end-to-end through the composer path (16 kHz, 20 ms reads), typical frame, as
         * LINEAR band output — this file's own units, so the numbers do not rot when the renderer's
         * curve moves. For scale, [Aurora.DISPLAY_GAMMA] draws 0.10 of this at roughly a third of
         * the ribbon's height and 0.006 at its resting sliver.
         *
         *   signal                    before                       after
         *   digital silence           0.0000 flat                  0.0000 flat
         *   pink -50 dBFS             0.058 0.022 0.016 0.007      0.0000 flat
         *   pink -45 dBFS             0.103 0.040 0.028 0.012      0.0000 flat
         *   pink -40 dBFS             0.183 0.070 0.050 0.022      0.002  0     0     0
         *   pink -35 dBFS             0.311 0.125 0.088 0.038      0.176  0.045 0     0
         *   white -45 dBFS (hiss)     0.040 0.033 0.046 0.031      0.0000 flat
         *   white -40 dBFS (hiss)     0.071 0.059 0.082 0.056      0.0000 flat
         *   voiced, every band above
         *     its floor               0.330 0.329 0.327 0.328      0.322 0.323 0.320 0.317
         *
         * The last two rows are the whole claim: the floor goes, the voice stays (98% of its height,
         * and 90-95% for a talker 12 dB quieter), and a room LOUDER than the assumption still draws,
         * bass first — this is an expander, not a switch. The two white rows are why the floor is not
         * derived from the room alone: hiss rises with frequency where a room falls, and a pink-only
         * floor would have left the sibilance sheet lit at hiss the ribbon has no business drawing.
         */
        val UNPROCESSED = AuroraCalibration(
            "unprocessed",
            noiseLive = UNPROCESSED_NOISE_LIVE,
            noiseSubtract = UNPROCESSED_NOISE_SUBTRACT,
        )
    }
}
