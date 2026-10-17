package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * THE SIMULATION IS THE ANALYSER — the property every number NoiseGateCandidateDiagnostic prints
 * rests on, asserted rather than printed.
 *
 * [NoiseGateSimulation.simulate] re-implements [AuroraAnalyser]'s dynamics from the raw band
 * energies so a candidate stage can be inserted into them. With no candidate inserted
 * (`subtractK = 0`) it must reproduce the shipped analyser, frame for frame, on the signals the study
 * measures. That was the study's own "step 1", and it printed its verdict instead of asserting it:
 * when the shipped expander landed under it, the disagreement it printed went from below 1e-3 to
 * 0.19-0.22 on the unprocessed profile, the build stayed green, and every candidate number after it
 * was a prediction about an analyser that no longer existed.
 *
 * Run over every shipped (rate, calibration) pair rather than the study's one, because the profile
 * that broke it was the one whose expander depth is not zero, and a check on the other profile alone
 * would have passed straight through that.
 */
class NoiseGateSimulationTest {

    /** The study's own bar: "must agree to <1e-3". */
    private val TOLERANCE = 1e-3

    @Test
    fun `with no candidate inserted the simulation reproduces the shipped analyser on every shipped stream`() {
        val dt = AudioAnalysisTap.HOP_MS / 1000f
        val misses = ArrayList<String>()
        for (s in AuroraDynamicsHarness.SHIPPED_STREAMS) {
            val n = 20 * s.sampleRate
            fun speech(gain: Float) =
                AuroraDynamicsHarness.speechClip(sampleRate = s.sampleRate, totalMs = 20000, gain = gain).first
            // The study's signals: room tone either side of the unprocessed profile's assumed room,
            // transducer hiss, and speech at two levels — i.e. every regime the expander has an
            // opinion about, from fully removed through partly removed to untouched.
            val signals = listOf(
                "pink -50dBFS" to NoiseGateSimulation.pink(n, -50.0),
                "pink -45dBFS" to NoiseGateSimulation.pink(n, -45.0),
                "pink -40dBFS" to NoiseGateSimulation.pink(n, -40.0),
                "white -45dBFS" to NoiseGateSimulation.white(n, -45.0),
                "speech g1.00" to speech(1f),
                "speech g0.33" to speech(0.33f),
            )
            for ((label, samples) in signals) {
                val (raws, outs) = NoiseGateSimulation.trace(samples, s.sampleRate, s.calibration)
                val sim = NoiseGateSimulation.simulate(raws, dt, s.calibration, subtractK = 0f)
                assertEquals("$s $label frame count", outs.size, sim.size)
                var worst = 0.0
                for (t in outs.indices) for (b in 0 until AuroraAnalyser.BANDS) {
                    worst = maxOf(worst, abs(outs[t][b] - sim[t][b]).toDouble())
                }
                if (worst >= TOLERANCE) misses += "$s $label: worst |sim - production| = $worst"
            }
        }
        assertTrue("the simulation has drifted from the analyser:\n" + misses.joinToString("\n"), misses.isEmpty())
    }
}
