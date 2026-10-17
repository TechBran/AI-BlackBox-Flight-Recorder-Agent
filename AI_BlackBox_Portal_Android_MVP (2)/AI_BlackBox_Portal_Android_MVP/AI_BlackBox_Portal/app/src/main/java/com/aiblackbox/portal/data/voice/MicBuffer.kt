package com.aiblackbox.portal.data.voice

/**
 * The mic capture buffer arithmetic, named once.
 *
 * Both capture loops — [SttStreamClient.startCapture] and VoiceViewModel.startMic() — size their
 * AudioRecord and their read buffer from here. That arithmetic decides how much audio arrives per
 * `read()`, which is not just a transport detail: the chunk an
 * [com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser] is handed IS the read, so the read
 * length sets the ribbon's temporal resolution. Two copies of a rule that load-bearing is the shape
 * that drifts, and it could not be measured while it was spelled out inline next to an
 * `android.media.AudioRecord` a JVM test cannot construct — which is exactly how it stayed wrong.
 *
 * Pure + unit-tested, like [VoiceSampleRates] beside it.
 *
 * THE UNIT TRAP THIS EXISTS TO MAKE VISIBLE: `AudioRecord.getMinBufferSize` returns BYTES, and the
 * read buffer is a `ShortArray` measured in SAMPLES. Two different factors of two live in these
 * four lines — the deliberate hardware headroom and the bytes-to-samples conversion — and while
 * they were spelled with the same literal they cancelled, unnoticed, for the whole life of the
 * capture path. They are separate named quantities now; keep them that way.
 */
object MicBuffer {

    /** PCM16 mono: two bytes per sample. A unit conversion, and nothing else. */
    const val BYTES_PER_SAMPLE = 2

    /**
     * How many platform-minimum buffers the AudioRecord is given. Slack against scheduling jitter,
     * so the recorder does not overrun while the capture loop is busy encoding and sending — a
     * HARDWARE sizing decision that has nothing to do with [BYTES_PER_SAMPLE] and only ever
     * coincided with it numerically.
     */
    const val HEADROOM_BUFFERS = 2

    /** The byte size handed to the `AudioRecord` constructor: [HEADROOM_BUFFERS] × the minimum. */
    fun recordBufferBytes(minBufferSizeBytes: Int): Int = minBufferSizeBytes * HEADROOM_BUFFERS

    /**
     * The `ShortArray` length the capture loop reads into, from [recordBufferBytes].
     *
     * One read is meant to be one platform minimum buffer's worth of audio — the headroom in the
     * record buffer is there to absorb jitter, not to be consumed in a single blocking read. So
     * this undoes the headroom factor and THEN converts bytes to samples. It used to do only the
     * latter, applied to the doubled buffer, which cancelled to `getMinBufferSize` SAMPLES — twice
     * what that many BYTES describes — and every read blocked for twice its intended duration.
     * See [readDurationMs] against [minBufferDurationMs].
     *
     * A negative `getMinBufferSize` error code (`ERROR` = -1, `ERROR_BAD_VALUE` = -2) is handed
     * back unchanged rather than divided. Integer division truncates toward zero, so dividing
     * would turn -1 into a plausible-looking 0 and leave the capture loop spinning on empty reads;
     * staying negative keeps it a hard failure at the `ShortArray` allocation, which is what it was
     * before. (In practice the `AudioRecord` constructor rejects the negative size first — this is
     * belt and braces on an arithmetic helper, not the live path.)
     */
    fun readLengthSamples(recordBufferBytes: Int): Int =
        if (recordBufferBytes < 0) recordBufferBytes
        else recordBufferBytes / (HEADROOM_BUFFERS * BYTES_PER_SAMPLE)

    /**
     * FRAMES of audio in [ms] at [sampleRate] — how a capture loop should say how much audio it
     * wants per read.
     *
     * The alternative, scaling whatever `getMinBufferSize` returned, makes the chunk size a
     * HARDWARE property: it differs by device and by rate, and the chunk is what sets the
     * analyser's cadence and the file writer's granularity. A frame count is the same everywhere.
     * (The streaming path above still derives from the platform minimum — it has shipped that way
     * and its correction is measured in [readLengthSamples]; this is for new capture loops.)
     *
     * Never zero: a zero-length read spins the capture loop on empty buffers at 100% CPU.
     */
    fun framesForMs(ms: Float, sampleRate: Int): Int =
        Math.round(sampleRate * ms / 1000f).coerceAtLeast(1)

    /**
     * The `AudioRecord` buffer size in bytes for a loop reading [frames] at a time, with
     * [HEADROOM_BUFFERS] of slack — floored at [minBufferSizeBytes], because `AudioRecord` refuses
     * anything under the platform minimum outright and a device with a large one would otherwise
     * never open its mic at all.
     *
     * A negative [minBufferSizeBytes] (`ERROR` = -1, `ERROR_BAD_VALUE` = -2) is not a size and
     * cannot floor anything; the frame-derived size stands, and the `AudioRecord` constructor
     * reports the real failure.
     */
    fun recordBufferBytesForFrames(frames: Int, minBufferSizeBytes: Int): Int =
        maxOf(frames * BYTES_PER_SAMPLE * HEADROOM_BUFFERS, minBufferSizeBytes)

    /** Wall-clock duration of one read of [readLengthSamples] at [sampleRate]. */
    fun readDurationMs(readLengthSamples: Int, sampleRate: Int): Double =
        readLengthSamples * 1000.0 / sampleRate

    /**
     * Wall-clock duration of ONE platform minimum buffer — the duration a read is meant to take,
     * and the yardstick [readDurationMs] is measured against.
     */
    fun minBufferDurationMs(minBufferSizeBytes: Int, sampleRate: Int): Double =
        (minBufferSizeBytes / BYTES_PER_SAMPLE) * 1000.0 / sampleRate
}
