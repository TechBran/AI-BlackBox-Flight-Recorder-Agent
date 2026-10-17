package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * THE DIRECT FEED AT THE HOP IS THE SHIPPED TAP — the claim every waveform measurement leans on,
 * asserted rather than printed.
 *
 * [AuroraDynamicsHarness] measures the ribbon two ways: by handing a real analyser chunks directly
 * (the `chunkMs` functions the characterisation tests and the diagnostics use), and by delivering
 * mic-sized reads to a real [AudioAnalysisTap] the way every capture loop does (the `ViaTap`
 * functions the requirements are scored on). This file pins that the two are the SAME MEASUREMENT
 * at the tap's hop, to the bit, for every stream the app ships. Without it a direct-feed number at
 * 20 ms is only a number that happens to share the shipped window's length.
 *
 * It was RangeBudgetDiagnostic's section 0, which printed "worst per-band absolute difference =
 * 0.0" into a report nobody was obliged to read. AudioAnalysisTapTest already proves frame parity
 * on its own short synthetic signal; what is added here is the harness's actual speech clips, the
 * whole of each (warm-up, acquisition and the settled auto-gain), and every shipped (rate,
 * calibration) pair — the expander included, which is only switched on by one of them.
 *
 * Equality is exact on purpose. Every input is deterministic and both paths run the same analyser
 * on the same bytes on the same clock, so any difference at all means the paths have diverged.
 */
class TapEquivalenceTest {

    /** The tap's hop, as the integer chunk the direct feeds take. */
    private val hopMs = AudioAnalysisTap.HOP_MS.toInt()

    @Test
    fun `the hop is a whole number of milliseconds, so a direct chunk can be exactly the shipped window`() {
        assertEquals("HOP_MS", hopMs.toFloat(), AudioAnalysisTap.HOP_MS, 0f)
    }

    @Test
    fun `the live distribution through the tap is the direct feed at the hop, for every shipped stream`() {
        for (s in AuroraDynamicsHarness.SHIPPED_STREAMS) {
            val direct = AuroraDynamicsHarness.liveDistribution(
                chunkMs = hopMs, sampleRate = s.sampleRate, calibration = s.calibration,
            )
            // The mic read, a read that does not divide the hop, and a socket-frame-sized delivery:
            // which one a device hands over is not ours to choose, and none of them may matter.
            for (readMs in listOf(AuroraDynamicsHarness.SHIPPED_READ_MS, 33, 100)) {
                val viaTap = AuroraDynamicsHarness.liveDistributionViaTap(s.sampleRate, s.calibration, readMs)
                assertEquals("$s, ${readMs} ms reads", direct, viaTap)
            }
            // Guard: two empty or silent distributions are equal too, and prove nothing.
            assertTrue("$s scored no frames", direct.pooled.n > 0)
            assertTrue("$s never lit its dominant sheet", direct.perBand[direct.dominantBand].p90 > 0.0)
        }
    }

    @Test
    fun `the dynamic-range session through the tap is the direct feed at the hop, for every shipped stream`() {
        for (s in AuroraDynamicsHarness.SHIPPED_STREAMS) {
            val direct = AuroraDynamicsHarness.dynamicRange(
                chunkMs = hopMs, sampleRate = s.sampleRate, calibration = s.calibration,
            )
            val viaTap = AuroraDynamicsHarness.dynamicRangeViaTap(
                sampleRate = s.sampleRate, calibration = s.calibration,
            )
            assertEquals("$s passages", direct.keys.toList(), viaTap.keys.toList())
            for (label in direct.keys) {
                assertEquals("$s passage '$label'", direct.getValue(label), viaTap.getValue(label))
                assertTrue("$s passage '$label' scored no frames", direct.getValue(label).pooled.n > 0)
            }
        }
    }

    @Test
    fun `the silence after speech through the tap is the direct feed at the hop, for every shipped stream`() {
        for (s in AuroraDynamicsHarness.SHIPPED_STREAMS) {
            val direct = AuroraDynamicsHarness.silenceAfterSpeech(
                chunkMs = hopMs, sampleRate = s.sampleRate, calibration = s.calibration,
            )
            val viaTap = AuroraDynamicsHarness.silenceAfterSpeechViaTap(
                sampleRate = s.sampleRate, calibration = s.calibration,
            )
            assertEquals("$s", direct, viaTap)
            assertTrue("$s scored no frames", direct.pooled.n > 0)
        }
    }
}
