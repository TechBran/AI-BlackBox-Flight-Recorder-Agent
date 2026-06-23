package com.aiblackbox.portal.data.voice

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.util.Log
import java.nio.ByteOrder
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * Decodes an audio file's PCM and builds a per-window RMS amplitude envelope
 * (peak-normalized 0..1) — the ACTUAL loudness of the audio over time. The
 * playback ribbon samples this at the live position, so it tracks the speech
 * smoothly + in sync and flattens to 0 on real silence, on every device
 * (no Visualizer). Handles mp3/wav/etc via MediaExtractor + MediaCodec.
 */
object AudioEnvelope {
    private const val TAG = "AudioEnvelope"

    // Shape the RMS envelope: a small noise GATE keeps true silence/hum FLAT,
    // then a LIFT (gamma < 1) raises everything above it so quiet-but-present
    // speech jumps up too — lots of movement on words, not just loud peaks.
    private const val GATE = 0.05f   // below this (normalized) -> flat
    private const val LIFT = 0.55f   // <1 lifts the speech range up

    /** Decode [path] -> (envelope 0..1 per [windowMs], windowMs). Null on failure. Blocking — call off the main thread. */
    fun decode(path: String, windowMs: Int = 20): Pair<FloatArray, Int>? {
        val extractor = MediaExtractor()
        var codec: MediaCodec? = null
        return try {
            extractor.setDataSource(path)
            var track = -1
            var format: MediaFormat? = null
            for (i in 0 until extractor.trackCount) {
                val f = extractor.getTrackFormat(i)
                if (f.getString(MediaFormat.KEY_MIME)?.startsWith("audio/") == true) { track = i; format = f; break }
            }
            if (track < 0 || format == null) return null
            val mime = format.getString(MediaFormat.KEY_MIME) ?: return null
            val sampleRate = format.getInteger(MediaFormat.KEY_SAMPLE_RATE)
            val channels = if (format.containsKey(MediaFormat.KEY_CHANNEL_COUNT)) format.getInteger(MediaFormat.KEY_CHANNEL_COUNT) else 1
            extractor.selectTrack(track)

            codec = MediaCodec.createDecoderByType(mime)
            codec.configure(format, null, null, 0)
            codec.start()

            val samplesPerWindow = (sampleRate.toLong() * windowMs / 1000L * channels).toInt().coerceAtLeast(1)
            val env = ArrayList<Float>(2048)
            var sumSq = 0.0
            var count = 0
            val info = MediaCodec.BufferInfo()
            var inEos = false
            var outEos = false

            while (!outEos) {
                if (!inEos) {
                    val inIdx = codec.dequeueInputBuffer(10_000)
                    if (inIdx >= 0) {
                        val inBuf = codec.getInputBuffer(inIdx)
                        val sz = if (inBuf != null) extractor.readSampleData(inBuf, 0) else -1
                        if (sz < 0) {
                            codec.queueInputBuffer(inIdx, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                            inEos = true
                        } else {
                            codec.queueInputBuffer(inIdx, 0, sz, extractor.sampleTime, 0)
                            extractor.advance()
                        }
                    }
                }
                val outIdx = codec.dequeueOutputBuffer(info, 10_000)
                if (outIdx >= 0) {
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) outEos = true
                    if (info.size > 0) {
                        val outBuf = codec.getOutputBuffer(outIdx)
                        if (outBuf != null) {
                            outBuf.order(ByteOrder.LITTLE_ENDIAN)
                            val shorts = outBuf.asShortBuffer()
                            val n = shorts.remaining()
                            var i = 0
                            while (i < n) {
                                val s = shorts.get().toInt()
                                sumSq += (s * s).toDouble()
                                count++
                                if (count >= samplesPerWindow) {
                                    env.add((sqrt(sumSq / count) / 32768.0).toFloat())
                                    sumSq = 0.0; count = 0
                                }
                                i++
                            }
                        }
                    }
                    codec.releaseOutputBuffer(outIdx, false)
                }
            }
            if (count > 0) env.add((sqrt(sumSq / count) / 32768.0).toFloat())
            if (env.isEmpty()) return null
            // Peak-normalize so the loudest window reads ~0.95 (consistent fullness
            // regardless of the clip's absolute volume).
            val peak = env.maxOrNull() ?: 0f
            val norm = if (peak > 0.001f) 1.0f / peak else 1f
            FloatArray(env.size) {
                val v = (env[it] * norm).coerceIn(0f, 1f)
                if (v <= GATE) 0f else ((v - GATE) / (1f - GATE)).pow(LIFT)
            } to windowMs
        } catch (e: Exception) {
            Log.w(TAG, "decode failed for $path: ${e.message}")
            null
        } finally {
            try { codec?.stop() } catch (_: Exception) {}
            try { codec?.release() } catch (_: Exception) {}
            try { extractor.release() } catch (_: Exception) {}
        }
    }
}
