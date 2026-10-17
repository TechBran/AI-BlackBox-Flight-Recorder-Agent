package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import kotlin.math.exp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * RATE HONESTY: the analyser must behave the same per unit of WALL CLOCK, whatever size the
 * caller's buffer happens to be.
 *
 * This is a PROPERTY test, not a characterisation — nothing here pins a level, so nothing here
 * needs editing when the ribbon is deliberately re-calibrated. It asserts only that the same audio,
 * covering the same wall clock, arrives at the same place regardless of how it was cut up. The
 * absolute numbers live next door in [WaveformDynamicsCharacterisationTest].
 *
 * WHY THIS MATTERS BEYOND TIDINESS. Chunk size is not a free parameter of the analyser: it is
 * whatever the caller upstream happens to hand over — 40 ms or 80 ms of mic depending on the
 * device's `getMinBufferSize` (see [MicBufferTest]), ~100 ms of socket frame on the AI playback
 * path. While the analyser answers differently per chunk size, every one of those is a different
 * calibration, no on-device measurement generalises off the device it was taken on, and any fix to
 * the mic read length silently re-tunes the ribbon as a side effect. Once this test passes, a
 * buffer-size change is a change to temporal RESOLUTION only, and can be made on its own merits.
 *
 * WHAT IS MEASURED AND WHY IT IS A STEADY TONE. The probes are single-bin Goertzel over a
 * RECTANGULAR window, so the chunk length is also the analysis window: on real speech, changing it
 * changes the raw band energies themselves. That is a genuine and separate rate-dependence (pinned
 * as such in [WaveformDynamicsCharacterisationTest]) and it would swamp this measurement. An
 * UNCHANGING tone removes it — every chunk sees the same signal at the same amplitude — leaving the
 * auto-gain as the only thing that can still differ.
 *
 * NOT COVERED, deliberately: the ACQUISITION rise inside the opening warm-up window is still a
 * per-chunk blend (RISE_BLEND), so the first ~600 ms of a session is not rate-invariant. It is a
 * bounded transient under an output attenuator that is itself time-based, and it leaves no trace in
 * the settled state asserted here — the runs below start from acquisition ends that differ by a few
 * percent and still converge onto each other.
 */
class AuroraRateInvarianceTest {

    /**
     * The buffer sizes a real source hands us, plus the extremes on either side. 16 ms is a small
     * `getMinBufferSize` on a fast device; 40 ms is the corrected mic read; 80 ms is what that read
     * was before [MicBufferTest]'s defect was fixed, and is still what a device with a larger
     * platform minimum will produce; 100 ms is the AI playback socket frame; 160 ms is a stalled
     * reader catching up. Which of these a given phone lands on is not ours to choose — that is
     * the whole reason the analyser has to answer the same way for all of them.
     */
    private val chunkSizesMs = intArrayOf(16, 32, 40, 80, 100, 160)

    /**
     * The residual this test tolerates, and exactly what it is — read this before loosening it.
     *
     * Invariance is not bit-exact, and the reason is arithmetic rather than sloppiness. The
     * reference is updated by applying the release over the WHOLE step and only then letting the
     * rise respond to the result, so the rise always sees a reference one step of release stale.
     * The settled balance therefore sits `dt / (2 * STEADY_DECAY_TAU_SEC)` above the continuous
     * one. That is a DERIVED quantity, not a fudge, and it was checked against the measurement
     * before this tolerance was written: predicted 0.125 / 0.251 / 0.313 / 0.627 / 0.783 / 1.253%
     * across the sweep below, band-0 settled levels 0.8016 / 0.8015 / 0.8008 / 0.7992 / 0.7988 /
     * 0.7963 — a 0.67% spread against the 78% spread this same measurement showed before the
     * release was made time-based.
     *
     * Removing it would mean solving the two opposing rates JOINTLY over the step instead of one
     * after the other — a restructure of the rise/fall asymmetry, not a change of constant, and
     * one that would also move the 32 ms anchor. Deliberately not done here: this milestone buys
     * rate honesty without touching the calibration.
     *
     * 7e-3 on a 0..1 scale, against an observed worst case of 5.8e-3 (band 3, 16 ms vs 160 ms).
     * Tightening it is welcome if the residual is actually removed; loosening it is not a fix.
     */
    private val SPLIT_RESIDUAL_TOL = 7e-3

    @Test
    fun `one unchanging tone settles to the same height at every chunk size`() {
        val runs = chunkSizesMs.map { it to AuroraDynamicsHarness.steadyToneBands(chunkMs = it) }

        for (b in 0 until AuroraAnalyser.BANDS) {
            val levels = runs.map { it.second[b] }
            val reference = levels.first()
            for ((i, chunkMs) in chunkSizesMs.withIndex()) {
                assertEquals(
                    "band $b at ${chunkMs}ms vs ${chunkSizesMs[0]}ms " +
                        "(all: ${runs.map { "${it.first}ms=%.4f".format(it.second[b]) }})",
                    reference,
                    levels[i],
                    SPLIT_RESIDUAL_TOL,
                )
            }
        }
    }

    /**
     * The one comparison the mic read-length fix turns on: 40 ms against 80 ms is exactly the move
     * that correcting the doubled read makes (see [MicBufferTest]). Asserted on its own, tighter
     * than the full sweep, because "the buffer fix does not re-calibrate the ribbon" is a claim
     * that deserves its own red light rather than being folded into a range.
     */
    @Test
    fun `correcting the mic read from 80ms to 40ms does not move the settled height`() {
        val at40 = AuroraDynamicsHarness.steadyToneBands(chunkMs = 40)
        val at80 = AuroraDynamicsHarness.steadyToneBands(chunkMs = 80)
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertEquals(
                "band $b: 40ms=${"%.4f".format(at40[b])} vs 80ms=${"%.4f".format(at80[b])}",
                at80[b],
                at40[b],
                3e-3,
            )
        }
    }

    @Test
    fun `the settled height does not drift monotonically with chunk size`() {
        // The failure mode this guards is specifically a SHIFTED EQUILIBRIUM, which shows up as a
        // monotone ramp across the sweep rather than as scatter. Stated as a ratio so it survives
        // any later re-calibration of the absolute level.
        val levels = chunkSizesMs.map { AuroraDynamicsHarness.steadyToneBands(chunkMs = it)[0] }
        val ratio = levels.max() / levels.min()
        assertTrue(
            "band0 settled level spans ${"%.3f".format(ratio)}x across ${chunkSizesMs.toList()} " +
                "(levels: ${levels.map { "%.4f".format(it) }})",
            ratio < 1.01,
        )
    }

    /**
     * The release constant, stated as the identity that makes the conversion checkable by hand:
     * the time-based release must reproduce the measured per-chunk constant EXACTLY at the chunk
     * size it was tuned at. That is what keeps this milestone a rate-honesty change and not a
     * re-calibration wearing its clothes.
     */
    @Test
    fun `the time-based release reproduces the measured per-chunk constant at its reference chunk`() {
        assertEquals(
            "one ${AuroraAnalyser.STEADY_DECAY_REF_SEC * 1000f} ms chunk of release",
            AuroraAnalyser.STEADY_DECAY,
            exp(-AuroraAnalyser.STEADY_DECAY_REF_SEC / AuroraAnalyser.STEADY_DECAY_TAU_SEC),
            1e-6f,
        )
    }
}
