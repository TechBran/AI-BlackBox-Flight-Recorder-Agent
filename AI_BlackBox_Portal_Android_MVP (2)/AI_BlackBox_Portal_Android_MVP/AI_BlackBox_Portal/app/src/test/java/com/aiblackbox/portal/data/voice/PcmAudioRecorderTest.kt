package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BINS
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * The Gemini composer's recorder.
 *
 * WHAT WAS BROKEN. The ribbon over this button was FROZEN: it was fed
 * `floatArrayOf(getMaxAmplitude() / 32767f)` read once, from a `MediaRecorder`, which yields no PCM
 * at all — one peak number, no spectrum, and nothing recomposing it while recording. Replacing the
 * recorder with an `AudioRecord` feeding the shared [com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap]
 * puts this surface on the same waveform pipeline as every other one.
 *
 * WHY A NEW CLASS. [AudioRecorderManager] is SHARED with Voice Lab's ElevenLabs cloning; changing
 * its format in place would silently change what gets cloned.
 *
 * The class is constructible on the JVM by design (its Android surface is `AudioRecord`, opened
 * only inside `start()`), so the resting contract and the no-microphone path are both testable
 * here. Everything about the FILE it writes is pinned in [WavStreamWriterTest].
 */
class PcmAudioRecorderTest {

    @get:Rule
    val temp = TemporaryFolder()

    private fun recorder() = PcmAudioRecorder(temp.root, unprocessedSupported = false)

    @Test
    fun `a resting recorder publishes the SHARED silent arrays, never fresh ones`() {
        // Identity: StateFlow drops a value equal to the one it holds, and FloatArray compares by
        // identity, so a fresh FloatArray(4) per tick would recompose the composer at the tap rate
        // through every silence.
        val rec = recorder()
        assertSame(AURORA_SILENT_BANDS, rec.bands.value)
        assertSame(AURORA_SILENT_BINS, rec.bins.value)
    }

    @Test
    fun `the resting bands are the analyser's width`() {
        assertEquals(AuroraAnalyser.BANDS, recorder().bands.value.size)
        assertEquals(AuroraAnalyser.BINS, recorder().bins.value.size)
    }

    @Test
    fun `a recorder that never started is not recording and stops to nothing`() {
        val rec = recorder()
        assertFalse(rec.isRecording)
        assertNull("nothing was captured, so there is no file to upload", rec.stop())
    }

    @Test
    fun `no microphone means start fails cleanly and leaves no half-written file behind`() {
        // On the JVM stub every AudioRecord comes back uninitialised, which is exactly the shape of
        // a device that refuses every source in the chain.
        val rec = recorder()
        assertFalse("start must report failure rather than pretend", rec.start())
        assertFalse(rec.isRecording)
        assertSame("a failed start leaves the ribbon resting", AURORA_SILENT_BANDS, rec.bands.value)
        assertEquals(
            "a failed start must not leave a 44-byte orphan in the cache dir",
            0, temp.root.listFiles()?.size ?: 0,
        )
    }

    @Test
    fun `a failed start can be retried`() {
        val rec = recorder()
        assertFalse(rec.start())
        assertFalse(rec.start())
        assertNull(rec.stop())
    }

    @Test
    fun `the capture rate is the analyser floor, not below it`() {
        // The top Goertzel probe is 6800 Hz and the analyser needs probes under sampleRate * 0.45,
        // so 15111 Hz is the arithmetic minimum. 16 kHz clears it; anything lower would silently
        // fold the top band.
        assertTrue(
            "SAMPLE_RATE ${PcmAudioRecorder.SAMPLE_RATE} cannot resolve the 6800 Hz probe",
            6800f < PcmAudioRecorder.SAMPLE_RATE * 0.45f,
        )
    }
}
