package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import kotlin.math.sin
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * THE MILESTONE'S ACCEPTANCE CRITERION, in the only place a JVM can hold it: the capture path's
 * analysis cadence and analysis WINDOW must be the same on every device.
 *
 * Until now the chunk an analyser was handed WAS the `AudioRecord.read`, and that read is sized from
 * `getMinBufferSize` — a platform number that differs by a factor of twenty across real phones (see
 * [MicBufferTest] for the arithmetic that turns it into a read length). So the red ribbon's temporal
 * resolution, and the per-frame dt the auto-gain runs on, were both decided by the handset. Any
 * calibration measured on one device described that device.
 *
 * This composes the two pure pieces the capture loops now use — [MicBuffer]'s read sizing and
 * [AudioAnalysisTap]'s hop scheduler — over a sweep of platform minimums, and asserts the analysis
 * comes out identical. AudioAnalysisTapTest already proves the hop schedule is chunk-size
 * independent in the abstract; what is pinned HERE is that the numbers a real capture loop feeds it
 * are inside that guarantee, at both mic rates the app opens.
 *
 * The wiring itself (that the loops call the tap rather than the analyser) is not JVM-observable —
 * `AudioRecord` cannot be constructed here. That half is device-validated.
 */
class CaptureAnalysisRateTest {

    /**
     * Representative `AudioRecord.getMinBufferSize` returns in BYTES, spanning what real handsets
     * report: a fast device's small minimum through a conservative one's large one. Which of these
     * a given phone lands on is not ours to choose — that is the whole point.
     */
    private val minBufferBytes = intArrayOf(640, 1280, 1920, 3840, 7680, 15360)

    private fun readLengthFor(minBufferSizeBytes: Int): Int =
        MicBuffer.readLengthSamples(MicBuffer.recordBufferBytes(minBufferSizeBytes))

    /** One second of speech-band tone as PCM16-LE, the stream every sweep below is cut out of. */
    private fun oneSecond(sampleRate: Int): ByteArray {
        val out = ByteArray(sampleRate * 2)
        for (i in 0 until sampleRate) {
            val v = (12000 * sin(2.0 * Math.PI * 220.0 * i / sampleRate)).toInt()
            out[i * 2] = (v and 0xFF).toByte()
            out[i * 2 + 1] = (v shr 8 and 0xFF).toByte()
        }
        return out
    }

    /** Feed a whole stream through one tap in reads of [readSamples], collecting frame offsets. */
    private fun offsetsFor(sampleRate: Int, readSamples: Int): List<Long> {
        val pcm = oneSecond(sampleRate)
        val tap = AudioAnalysisTap(sampleRate)
        val offsets = ArrayList<Long>()
        var at = 0
        while (at < pcm.size) {
            val take = minOf(readSamples * 2, pcm.size - at)
            tap.offer(pcm.copyOfRange(at, at + take), take) { offsets.add(it.sampleOffset) }
            at += take
        }
        return offsets
    }

    @Test
    fun `the analysis cadence is the tap's hop, not the device's minimum buffer`() {
        for (sampleRate in intArrayOf(16000, 24000)) {
            val reads = minBufferBytes.map { readLengthFor(it) }
            // Guard the sweep itself: under the OLD rule these read lengths WERE the analysis
            // cadence, so if they ever stop differing this test has quietly stopped measuring.
            assertTrue(
                "read lengths must differ for this to prove anything: $reads",
                reads.distinct().size == reads.size,
            )

            val reference = offsetsFor(sampleRate, reads.first())
            assertEquals("50 Hz over one second at $sampleRate", 50, reference.size)
            for (read in reads) {
                assertEquals(
                    "read length $read samples at ${sampleRate}Hz",
                    reference,
                    offsetsFor(sampleRate, read),
                )
            }
        }
    }

    @Test
    fun `the analysis window is one hop of audio at both mic rates`() {
        // Not the same claim as the cadence: a Goertzel probe over a RECTANGULAR window reads the
        // window LENGTH into the band energies themselves, so a 40 ms device and an 80 ms device
        // used to be two different calibrations of the same ribbon.
        for (backend in VoiceBackend.entries) {
            val rate = VoiceSampleRates.mic(backend)
            val tap = AudioAnalysisTap(rate)

            assertEquals(
                "${backend.id} window ms",
                AudioAnalysisTap.HOP_MS.toDouble(),
                tap.bandWindowSamples * 1000.0 / rate,
                1e-9,
            )
        }
    }
}
