package com.aiblackbox.portal.data.voice

import java.io.File
import java.io.RandomAccessFile

/**
 * The name a raw recording is uploaded under.
 *
 * THE SUFFIX IS THE ENTIRE DOWNSTREAM CONTRACT: the uploader posts `application/octet-stream` and
 * the server preserves only the extension, so `.wav` is the one thing that tells Gemini, OpenAI and
 * ElevenLabs what the bytes are. `audio/wav` is on all three accepted lists; `.m4a` is not on
 * Gemini's.
 */
fun pcmRecordingFileName(timestampMs: Long): String = "recording_$timestampMs.wav"

/**
 * A 44-byte canonical RIFF/WAVE header over PCM16 mono, written as the audio arrives.
 *
 * WHY STREAMING AND NOT "BUFFER THEN WRITE". A recording's length is unknown until the operator
 * stops, and the two size fields in a RIFF header sit at byte 4 and byte 40 — before the payload.
 * Buffering the whole take to learn the length would put a five-minute recording in the heap for no
 * reason. So the header goes down with placeholder sizes, the PCM streams straight through, and
 * [close] seeks back and patches the two fields. Get that patch wrong and the file still LOOKS like
 * a wav; every decoder just truncates it to the size it claims.
 *
 * [sampleRate] is the rate the hardware ACTUALLY opened at, never the one that was requested:
 * `AudioRecord` guarantees only 44100 Hz, and a header that lies about the rate plays the whole
 * recording at the wrong speed for every consumer.
 *
 * THREAD-SAFE, and it has to be — this is the one class on the capture path that two threads
 * genuinely touch. The capture loop appends from the IO thread while the operator's tap calls
 * [close] from the main thread, and a `close` that seeks back to patch the header WHILE a write is
 * in flight corrupts the file at byte 0. The lock costs one uncontended monitor per read.
 */
class WavStreamWriter(private val file: File, private val sampleRate: Int) {

    private var out: RandomAccessFile? = null
    private var dataBytes = 0L

    /** Create the file and reserve the header. */
    @Synchronized
    fun open() {
        if (out != null) return
        val raf = RandomAccessFile(file, "rw")
        raf.setLength(0)
        raf.write(header(sampleRate, 0))
        out = raf
        dataBytes = 0
    }

    /**
     * Append [lenBytes] of PCM16-LE from [pcm].
     *
     * The length is explicit because `AudioRecord.read` hands back SHORT reads into a full-size
     * buffer — the tail is the previous read's audio, and writing it splices stale speech into the
     * upload. Silently ignored after [close] so a stop racing the capture loop cannot append past
     * the patched sizes.
     */
    @Synchronized
    fun write(pcm: ByteArray, lenBytes: Int) {
        val raf = out ?: return
        val n = lenBytes.coerceIn(0, pcm.size)
        if (n == 0) return
        raf.write(pcm, 0, n)
        dataBytes += n
    }

    /** Patch the two size fields and close. Idempotent. */
    @Synchronized
    fun close() {
        val raf = out ?: return
        out = null
        try {
            raf.seek(0)
            raf.write(header(sampleRate, dataBytes))
        } finally {
            try { raf.close() } catch (_: Exception) {}
        }
    }

    companion object {
        const val HEADER_BYTES = 44
        const val CHANNELS = 1
        const val BITS_PER_SAMPLE = 16

        private fun header(sampleRate: Int, dataBytes: Long): ByteArray {
            val blockAlign = CHANNELS * BITS_PER_SAMPLE / 8
            val byteRate = sampleRate * blockAlign
            val b = ByteArray(HEADER_BYTES)
            ascii(b, 0, "RIFF")
            le32(b, 4, 36 + dataBytes)
            ascii(b, 8, "WAVE")
            ascii(b, 12, "fmt ")
            le32(b, 16, 16)                 // PCM subchunk length
            le16(b, 20, 1)                  // uncompressed PCM
            le16(b, 22, CHANNELS)
            le32(b, 24, sampleRate.toLong())
            le32(b, 28, byteRate.toLong())
            le16(b, 32, blockAlign)
            le16(b, 34, BITS_PER_SAMPLE)
            ascii(b, 36, "data")
            le32(b, 40, dataBytes)
            return b
        }

        private fun ascii(b: ByteArray, at: Int, s: String) {
            for (i in s.indices) b[at + i] = s[i].code.toByte()
        }

        private fun le16(b: ByteArray, at: Int, v: Int) {
            b[at] = (v and 0xFF).toByte()
            b[at + 1] = ((v shr 8) and 0xFF).toByte()
        }

        private fun le32(b: ByteArray, at: Int, v: Long) {
            b[at] = (v and 0xFF).toByte()
            b[at + 1] = ((v shr 8) and 0xFF).toByte()
            b[at + 2] = ((v shr 16) and 0xFF).toByte()
            b[at + 3] = ((v shr 24) and 0xFF).toByte()
        }
    }
}
