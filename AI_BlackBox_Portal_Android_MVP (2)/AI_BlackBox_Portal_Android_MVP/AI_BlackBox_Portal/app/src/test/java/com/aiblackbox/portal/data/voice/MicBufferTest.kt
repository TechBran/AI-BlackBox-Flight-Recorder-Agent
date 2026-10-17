package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The mic read-length arithmetic.
 *
 * WAS A CHARACTERISATION, IS NOW A SPECIFICATION — that transition is the point of this file's
 * history. M0 pinned the defect: every `read()` blocked for TWICE its intended duration, because
 * the deliberately doubled hardware buffer was then divided by the bytes-per-sample factor and the
 * two twos cancelled, so a BYTE count of the doubled buffer arrived at the loop as a SAMPLE count
 * of one read. The numbers below are the same measurements with the corrected expectations.
 *
 * WHY THIS COULD ONLY BE FIXED AFTER THE ANALYSER. The read length is not merely a transport
 * detail: the chunk it produces IS what the Aurora analyser is handed. While the AGC release was
 * counted per chunk rather than per second, halving the read also moved the auto-gain's operating
 * point, so this correction could not be told apart from a re-calibration of the ribbon. It can
 * now — see [AuroraRateInvarianceTest], which measures the 40 ms vs 80 ms move at 0.2%.
 *
 * Note where the fix landed: in [MicBuffer.readLengthSamples] alone. Both capture loops
 * ([SttStreamClient.startCapture] and VoiceViewModel.startMic()) already route through it, so
 * neither needed touching — which is what the M0 extraction bought.
 *
 * Rates are the two real ones (see [VoiceSampleRates]); the `getMinBufferSize` byte counts are
 * plausible Fold-6-class values, and nothing here depends on the exact device number — covering
 * the intended duration is a property of the arithmetic, so it is asserted as a ratio over a sweep.
 */
class MicBufferTest {

    /** Plausible platform minimums, in BYTES, across the rates and devices we ship on. */
    private val minBufferBytesSweep = intArrayOf(640, 1280, 1920, 2048, 2560, 3840, 4096, 7104)
    private val rates = intArrayOf(16000, 24000)

    // -------------------------------------------------------------- the corrected read

    @Test
    fun `a mic read covers exactly its intended duration`() {
        for (rate in rates) {
            for (minBytes in minBufferBytesSweep) {
                val recordBytes = MicBuffer.recordBufferBytes(minBytes)
                val samples = MicBuffer.readLengthSamples(recordBytes)

                val actualMs = MicBuffer.readDurationMs(samples, rate)
                val intendedMs = MicBuffer.minBufferDurationMs(minBytes, rate)

                assertEquals(
                    "read duration / intended duration at ${rate}Hz, minBuffer=$minBytes B",
                    1.0,
                    actualMs / intendedMs,
                    1e-9,
                )
            }
        }
    }

    @Test
    fun `the read length in samples is the platform minimum converted from bytes`() {
        // The fix in one line: the two factors of two are now distinct named quantities — the
        // hardware headroom the record buffer carries, and the bytes->samples conversion — so
        // they no longer cancel by accident.
        for (minBytes in minBufferBytesSweep) {
            assertEquals(
                "readLengthSamples(recordBufferBytes($minBytes))",
                minBytes / 2,
                MicBuffer.readLengthSamples(MicBuffer.recordBufferBytes(minBytes)),
            )
        }
    }

    @Test
    fun `the concrete read duration at both shipped rates is one platform buffer`() {
        // Spelled out as absolute milliseconds because these are the chunk sizes the analyser
        // actually sees. 1920 B min buffer = 960 samples = 40 ms; the loop used to read 1920
        // samples = 80 ms, which is why 80 ms is the chunk the analyser was characterised at.
        assertEquals(
            40.0,
            MicBuffer.readDurationMs(MicBuffer.readLengthSamples(MicBuffer.recordBufferBytes(1920)), 24000),
            1e-9,
        )
        assertEquals(
            40.0,
            MicBuffer.minBufferDurationMs(1920, 24000),
            1e-9,
        )
        // 16 kHz backends (Gemini/Grok): 1280 B = 640 samples = 40 ms, read as 1280 = 80 ms before.
        assertEquals(
            40.0,
            MicBuffer.readDurationMs(MicBuffer.readLengthSamples(MicBuffer.recordBufferBytes(1280)), 16000),
            1e-9,
        )
        assertEquals(
            40.0,
            MicBuffer.minBufferDurationMs(1280, 16000),
            1e-9,
        )
    }

    // ------------------------------------------------------- extraction fidelity

    @Test
    fun `the hardware buffer is twice the platform minimum`() {
        // This half was always correct and had to survive the fix: doubling the AudioRecord buffer
        // buys slack against scheduling jitter. Only the read length derived from it was wrong.
        for (minBytes in minBufferBytesSweep) {
            assertEquals(minBytes * 2, MicBuffer.recordBufferBytes(minBytes))
        }
    }

    @Test
    fun `a getMinBufferSize error code still propagates to a failed allocation`() {
        // AudioRecord.ERROR = -1, ERROR_BAD_VALUE = -2. Open-coded, these reached ShortArray() and
        // threw, and they must still throw: a hard failure is recoverable, whereas a zero-length
        // buffer would spin the capture loop on empty reads forever.
        //
        // This is the one place the fix could have silently regressed. Dividing by a LARGER factor
        // truncates a small negative toward zero — ERROR's -1 would have become a plausible-looking
        // 0 — so [MicBuffer.readLengthSamples] hands error codes straight back instead.
        for (err in intArrayOf(-1, -2)) {
            val samples = MicBuffer.readLengthSamples(MicBuffer.recordBufferBytes(err))
            assertTrue("readLengthSamples must stay negative for $err, was $samples", samples < 0)
            assertThrows(NegativeArraySizeException::class.java) { ShortArray(samples) }
        }
    }

    // ------------------------------------------------- frame-sized reads (M8)

    /**
     * The recording path sizes its read in FRAMES of audio rather than by scaling whatever
     * `getMinBufferSize` returned. The difference is not cosmetic: the platform minimum is a
     * HARDWARE number that varies by device and rate, so a read derived from it delivers a
     * different amount of audio per chunk on every phone — and the chunk is what the analyser's
     * cadence and the WAV writer's granularity are made of. A frame count is the same everywhere.
     */
    @Test
    fun `a frame count is the audio duration asked for, at any rate`() {
        assertEquals(320, MicBuffer.framesForMs(20f, 16000))
        assertEquals(480, MicBuffer.framesForMs(20f, 24000))
        assertEquals(160, MicBuffer.framesForMs(10f, 16000))
    }

    @Test
    fun `a frame count is never zero, however short the window`() {
        // A zero-length read would spin the capture loop at 100% CPU on empty buffers.
        assertTrue(MicBuffer.framesForMs(0f, 16000) >= 1)
        assertTrue(MicBuffer.framesForMs(0.001f, 16000) >= 1)
    }

    @Test
    fun `the record buffer holds several reads of headroom`() {
        val frames = MicBuffer.framesForMs(20f, 16000)
        assertEquals(
            frames * MicBuffer.BYTES_PER_SAMPLE * MicBuffer.HEADROOM_BUFFERS,
            MicBuffer.recordBufferBytesForFrames(frames, minBufferSizeBytes = 640),
        )
    }

    @Test
    fun `the platform minimum is a FLOOR the frame count can never undercut`() {
        // AudioRecord refuses a buffer under getMinBufferSize outright, so a device with a large
        // minimum must win over the frame arithmetic — otherwise the mic simply never opens there.
        val frames = MicBuffer.framesForMs(20f, 16000)   // 320 frames = 1280 bytes = 2560 with headroom
        assertEquals(7104, MicBuffer.recordBufferBytesForFrames(frames, minBufferSizeBytes = 7104))
    }

    @Test
    fun `a getMinBufferSize error code cannot shrink the buffer to nonsense`() {
        // -1/-2 are error codes, not sizes. Unlike the streaming path (which propagates them so the
        // allocation fails loudly), the recorder has its own frame-derived size and simply uses it:
        // the AudioRecord constructor is the thing that will report the real failure.
        val frames = MicBuffer.framesForMs(20f, 16000)
        val expected = frames * MicBuffer.BYTES_PER_SAMPLE * MicBuffer.HEADROOM_BUFFERS
        for (err in intArrayOf(-1, -2)) {
            assertEquals(expected, MicBuffer.recordBufferBytesForFrames(frames, err))
        }
    }
}
