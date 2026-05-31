package com.aiblackbox.portal.ui.voice

import kotlin.math.min
import kotlin.math.sqrt

/**
 * Root-mean-square loudness of a PCM16 buffer, normalized to 0f..1f.
 *
 * Pure function (no Android deps) so it is unit-testable on the JVM and can be
 * called cheaply from the mic read loop and the playback drain. One pass over
 * samples we already hold — no extra audio reads.
 *
 * @param buffer signed PCM16 samples.
 * @param count number of valid samples in [buffer] (e.g. AudioRecord.read return).
 */
fun rmsAmplitude(buffer: ShortArray, count: Int): Float {
    val n = min(count, buffer.size)
    if (n <= 0) return 0f
    var sumSquares = 0.0
    for (i in 0 until n) {
        val s = buffer[i] / 32768.0  // normalize to -1.0..1.0
        sumSquares += s * s
    }
    return sqrt(sumSquares / n).toFloat().coerceIn(0f, 1f)
}
