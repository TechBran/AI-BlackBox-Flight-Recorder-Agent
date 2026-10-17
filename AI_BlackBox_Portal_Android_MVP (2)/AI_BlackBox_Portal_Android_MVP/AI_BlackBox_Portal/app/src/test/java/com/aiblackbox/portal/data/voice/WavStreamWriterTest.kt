package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * The Gemini recording is uploaded as 16 kHz mono PCM16 WAV, and the FILENAME EXTENSION is the
 * entire contract: the uploader sends `application/octet-stream` and the server keeps only the
 * suffix, so `.wav` is what tells three different providers what the bytes are. `audio/wav` is on
 * Gemini's, OpenAI's and ElevenLabs' accepted lists; `.m4a` is not on Gemini's.
 *
 * 16 kHz is not a compromise either: Gemini downsamples to 16 kbps mono and charges 32 tokens/sec
 * whatever it is given, the analyser's top probe is 6800 Hz (so anything above ~15.1 kHz works),
 * and 44.1 kHz would put a five-minute recording near 30 MB — past OpenAI's hard 25 MB cap.
 *
 * A RIFF header cannot be written until the payload length is known, which for a live recording is
 * only at stop. Hence the placeholder-then-patch shape tested here: get it wrong and the file is a
 * plausible 44-byte-headed blob that every decoder truncates or rejects.
 */
class WavStreamWriterTest {

    @get:Rule
    val temp = TemporaryFolder()

    private fun le32(b: ByteArray, at: Int): Int =
        (b[at].toInt() and 0xFF) or ((b[at + 1].toInt() and 0xFF) shl 8) or
            ((b[at + 2].toInt() and 0xFF) shl 16) or ((b[at + 3].toInt() and 0xFF) shl 24)

    private fun le16(b: ByteArray, at: Int): Int =
        (b[at].toInt() and 0xFF) or ((b[at + 1].toInt() and 0xFF) shl 8)

    private fun ascii(b: ByteArray, at: Int) = String(b, at, 4, Charsets.US_ASCII)

    private fun write(file: File, sampleRate: Int, vararg chunks: ByteArray) {
        val w = WavStreamWriter(file, sampleRate)
        w.open()
        chunks.forEach { w.write(it, it.size) }
        w.close()
    }

    @Test
    fun `the header is the canonical 44 bytes and the payload follows it`() {
        val file = temp.newFile("a.wav")
        write(file, 16000, ByteArray(100))
        assertEquals(WavStreamWriter.HEADER_BYTES + 100, file.length().toInt())
    }

    @Test
    fun `the RIFF and data sizes are patched to what was actually recorded`() {
        val file = temp.newFile("b.wav")
        // Two writes: the sizes must count the whole stream, not the last buffer.
        write(file, 16000, ByteArray(40), ByteArray(60))
        val b = file.readBytes()
        assertEquals("RIFF", ascii(b, 0))
        assertEquals("WAVE", ascii(b, 8))
        assertEquals("chunk size is everything after the first 8 bytes", 36 + 100, le32(b, 4))
        assertEquals("data", ascii(b, 36))
        assertEquals(100, le32(b, 40))
    }

    @Test
    fun `the fmt chunk declares 16 kHz mono PCM16`() {
        val file = temp.newFile("c.wav")
        write(file, 16000, ByteArray(8))
        val b = file.readBytes()
        assertEquals("fmt ", ascii(b, 12))
        assertEquals("PCM subchunk is 16 bytes", 16, le32(b, 16))
        assertEquals("uncompressed PCM", 1, le16(b, 20))
        assertEquals("mono", 1, le16(b, 22))
        assertEquals(16000, le32(b, 24))
        assertEquals("byte rate = rate x block align", 16000 * 2, le32(b, 28))
        assertEquals("block align = channels x bytes per sample", 2, le16(b, 32))
        assertEquals(16, le16(b, 34))
    }

    @Test
    fun `the declared rate follows the rate the hardware actually gave us`() {
        // AudioRecord guarantees only 44100 Hz; a device that hands back something else must not
        // have its recording labelled 16 kHz, or every consumer plays it at the wrong speed.
        val file = temp.newFile("d.wav")
        write(file, 44100, ByteArray(4))
        val b = file.readBytes()
        assertEquals(44100, le32(b, 24))
        assertEquals(44100 * 2, le32(b, 28))
    }

    @Test
    fun `a partial buffer writes only its valid bytes`() {
        // AudioRecord.read hands back SHORT reads into a full-size buffer; the tail is the previous
        // read's audio. Writing it would splice stale speech into the upload.
        val file = temp.newFile("e.wav")
        val w = WavStreamWriter(file, 16000)
        w.open()
        w.write(ByteArray(64) { 7 }, 10)
        w.close()
        assertEquals(WavStreamWriter.HEADER_BYTES + 10, file.length().toInt())
    }

    @Test
    fun `a recording with no audio at all is still a valid empty wav`() {
        val file = temp.newFile("f.wav")
        write(file, 16000)
        val b = file.readBytes()
        assertEquals(WavStreamWriter.HEADER_BYTES, b.size)
        assertEquals(36, le32(b, 4))
        assertEquals(0, le32(b, 40))
    }

    @Test
    fun `close is idempotent, so a stop after a failure cannot corrupt the sizes`() {
        val file = temp.newFile("g.wav")
        val w = WavStreamWriter(file, 16000)
        w.open()
        w.write(ByteArray(20), 20)
        w.close()
        w.close()
        assertEquals(20, le32(file.readBytes(), 40))
    }

    @Test
    fun `a write after close is refused rather than appended past the patched size`() {
        val file = temp.newFile("h.wav")
        val w = WavStreamWriter(file, 16000)
        w.open()
        w.write(ByteArray(20), 20)
        w.close()
        w.write(ByteArray(20), 20)
        assertEquals(WavStreamWriter.HEADER_BYTES + 20, file.length().toInt())
    }

    @Test
    fun `the recording filename carries the wav suffix and its timestamp`() {
        val name = pcmRecordingFileName(1754870400000L)
        assertEquals("recording_1754870400000.wav", name)
        assertTrue("the suffix IS the downstream contract", name.endsWith(".wav"))
    }
}
