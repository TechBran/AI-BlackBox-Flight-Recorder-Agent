package com.aiblackbox.portal.data.voice

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.util.Log
import com.aiblackbox.portal.ui.components.aurora.AuroraOfflineBands
import java.nio.ByteOrder
import kotlin.math.pow
import kotlin.math.sqrt

/**
 * Decodes an audio file's PCM into two per-window envelopes — the RMS amplitude (the ACTUAL
 * loudness of the audio over time) and the four Aurora band energies. The playback ribbon samples
 * them at the live position, so it tracks the speech smoothly + in sync and flattens to 0 on real
 * silence, on every device (no Visualizer). Handles mp3/wav/etc via MediaExtractor + MediaCodec.
 *
 * This is the ONE audio source with no live tap: the file is fully known before playback starts, so
 * the bands come from [AuroraOfflineBands] over the whole clip rather than from a live analyser.
 * That is what deletes the warm-up ramp on this path instead of merely shortening it — playing a
 * two-second TTS reply twice produces the identical ribbon both times.
 */
object AudioEnvelope {
    private const val TAG = "AudioEnvelope"

    // Shape the RMS envelope: a small noise GATE keeps true silence/hum FLAT,
    // then a LIFT (gamma < 1) raises everything above it so quiet-but-present
    // speech jumps up too — lots of movement on words, not just loud peaks.
    // (the offline band pass applies the same two constants to the bands, for the same reason.)
    private const val GATE = 0.05f   // below this (normalized) -> flat
    private const val LIFT = 0.55f   // <1 lifts the speech range up

    /**
     * Both envelopes one decode produced, sampled together at the playback position.
     *
     * Named `Envelopes` and not `Result`: a nested `Result` would shadow `kotlin.Result` throughout
     * this object's body, and this file is one `runCatching` away from that being a real trap.
     *
     * Deliberately NOT a data class: array properties would give it identity-based equals, and a
     * generated `copy`/`equals` that silently means the wrong thing is worse than none at all
     * (the same trap [com.aiblackbox.portal.ui.components.aurora.AuroraVoice] documents).
     *
     * @param rms per-window loudness, 0..1, peak-normalised over the clip.
     * @param bands per-window band energies (`[window][band]`, each 0..1), or EMPTY only when the
     *   decoded PCM has no usable sample rate or the decoder produced nothing long enough to probe.
     *   Clip LENGTH does not enter into it — see [AuroraOfflineBands].
     * @param windowMs the window both envelopes are sliced at.
     *
     * `bands.size` may be one shorter than `rms.size`: the RMS pass always flushes a trailing
     * partial window, the offline band pass drops one too short to probe. Both are sampled by the
     * same position, so both samplers clamp.
     */
    class Envelopes(
        val rms: FloatArray,
        val bands: Array<FloatArray>,
        val windowMs: Int,
    )

    /** Decode [path] into its envelopes. Null on failure. Blocking — call off the main thread. */
    fun decode(path: String, windowMs: Int = 20): Envelopes? {
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
            val trackSampleRate = format.getInteger(MediaFormat.KEY_SAMPLE_RATE)
            val trackChannels = if (format.containsKey(MediaFormat.KEY_CHANNEL_COUNT)) format.getInteger(MediaFormat.KEY_CHANNEL_COUNT) else 1
            extractor.selectTrack(track)

            codec = MediaCodec.createDecoderByType(mime)
            codec.configure(format, null, null, 0)
            codec.start()

            // The DECODER's output format is authoritative for the PCM we are about to analyse; the
            // extractor's track format read above is only the seed, kept for a decoder that never
            // reports one. MediaCodec is explicitly allowed to emit PCM at a different rate/channel
            // COUNT than the container declares — HE-AAC/AAC+ with SBR+PS is the classic case: the track
            // says 22050 Hz mono, the decoder outputs 44100 Hz stereo. The RMS envelope only
            // mis-sizes its window under that; the bands break outright — a wrong channel count
            // de-interleaves on the wrong stride so every band is garbage rather than mis-scaled,
            // and a wrong rate mis-probes every Goertzel frequency AND detaches the window count
            // from playback milliseconds, so sampleBands() would read the wrong position for the
            // whole clip. Do NOT move this back to the track format.
            var pcmSampleRate = trackSampleRate
            var pcmChannels = trackChannels
            // Built lazily on the first PCM buffer, never eagerly here: INFO_OUTPUT_FORMAT_CHANGED
            // always arrives BEFORE the first output buffer, so by then the two vars above hold the
            // decoder's own answer.
            var pass: DecodedPcmPass? = null
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
                if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    // What the decoder ACTUALLY emits, which is what everything below is sized from.
                    val outFormat = codec.outputFormat
                    if (outFormat.containsKey(MediaFormat.KEY_SAMPLE_RATE)) {
                        pcmSampleRate = outFormat.getInteger(MediaFormat.KEY_SAMPLE_RATE)
                    }
                    if (outFormat.containsKey(MediaFormat.KEY_CHANNEL_COUNT)) {
                        pcmChannels = outFormat.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
                    }
                    if (pcmSampleRate != trackSampleRate || pcmChannels != trackChannels) {
                        Log.d(TAG, "decoder PCM is ${pcmSampleRate}Hz x$pcmChannels, " +
                            "track declared ${trackSampleRate}Hz x$trackChannels")
                    }
                } else if (outIdx >= 0) {
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) outEos = true
                    if (info.size > 0) {
                        val outBuf = codec.getOutputBuffer(outIdx)
                        if (outBuf != null) {
                            val p = pass ?: DecodedPcmPass(pcmSampleRate, pcmChannels, windowMs)
                                .also { pass = it }
                            val samplesPerWindow = p.samplesPerWindow
                            outBuf.order(ByteOrder.LITTLE_ENDIAN)
                            val shorts = outBuf.asShortBuffer()
                            val n = shorts.remaining()
                            var i = 0
                            while (i < n) {
                                val s = shorts.get().toInt()
                                sumSq += (s * s).toDouble()
                                count++
                                // Interleaved channels are fine for RMS and WRONG for the band
                                // pass — see MonoDownmixer.
                                p.addSample(s)
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
            val rms = FloatArray(env.size) {
                val v = (env[it] * norm).coerceIn(0f, 1f)
                if (v <= GATE) 0f else ((v - GATE) / (1f - GATE)).pow(LIFT)
            }
            Envelopes(rms = rms, bands = pass?.finish() ?: emptyArray(), windowMs = windowMs)
        } catch (e: Exception) {
            Log.w(TAG, "decode failed for $path: ${e.message}")
            null
        } finally {
            try { codec?.stop() } catch (_: Exception) {}
            try { codec?.release() } catch (_: Exception) {}
            try { extractor.release() } catch (_: Exception) {}
        }
    }

    /**
     * The band accumulator for a track at [sampleRate], or null to skip the band pass.
     *
     * The guard is the whole reason this is a function: a container that declares a nonsense rate
     * would otherwise throw out of [AuroraOfflineBands]'s `require` and cost the clip its ENTIRE
     * envelope, when the honest degradation is losing the bands and keeping the RMS ribbon.
     */
    internal fun bandStream(sampleRate: Int, windowMs: Int): AuroraOfflineBands? =
        if (sampleRate > 0) AuroraOfflineBands(sampleRate, windowMs) else null
}

/**
 * Everything [AudioEnvelope.decode]'s inner loop needs that depends on the PCM FORMAT — the RMS
 * window length, the downmixer's stride and the band pass's probe frequencies — all derived from
 * ONE (rate, channels) pair.
 *
 * They live together so they cannot be built from DIFFERENT formats, which is the bug this class
 * exists to make unrepresentable: sizing the band pass from the container's declared format while
 * the decoder emits another one (HE-AAC SBR+PS) de-interleaves on the wrong stride and probes every
 * band at the wrong frequency. `decode` constructs this from the decoder's reported output format.
 *
 * NOT thread-safe — one instance belongs to one decode.
 */
internal class DecodedPcmPass(sampleRate: Int, channelCount: Int, windowMs: Int) {

    private val channels = channelCount.coerceAtLeast(1)

    /**
     * RMS window length in INTERLEAVED samples — the decode loop counts every channel, so this is
     * frames x channels. Never 0: a zero would make every single sample its own window.
     */
    val samplesPerWindow: Int =
        (sampleRate.toLong() * windowMs / 1000L * channels).toInt().coerceAtLeast(1)

    // Bands are accumulated AS the decoder hands PCM over — 4 floats per 20 ms window, not the
    // clip's PCM — so a five-minute generated song gets the same real bands a two-second TTS reply
    // does. null = no band pass at all; the RMS envelope is unaffected.
    private val bands = AudioEnvelope.bandStream(sampleRate, windowMs)
    private val mono = bands?.let { stream -> MonoDownmixer(channels) { stream.add(it) } }

    /** Feed one INTERLEAVED PCM sample, in decoder order. */
    fun addSample(sample: Int) {
        mono?.add(sample)
    }

    /** Per-window bands, or empty when there was no band pass. One-shot (see [AuroraOfflineBands]). */
    fun finish(): Array<FloatArray> = bands?.finish() ?: emptyArray()
}

/**
 * Downmixes a decoder's interleaved PCM to MONO, forwarding one sample per frame.
 *
 * Mono because Goertzel probes a single stream: run it over interleaved stereo and every frequency
 * reads at double, folding the top band into whatever aliases there. The RMS envelope beside it
 * genuinely does not care (a sum of squares is order-independent), which is why only the band pass
 * needs this.
 *
 * It FORWARDS rather than collects: the band pass consumes a window at a time, so nothing here has
 * to hold the clip and there is no length limit anywhere on this path.
 */
internal class MonoDownmixer(channels: Int, private val onSample: (Short) -> Unit) {

    private val channels = channels.coerceAtLeast(1)

    // The partial frame carried ACROSS add() calls. MediaCodec hands back whole frames in practice,
    // but assuming it does would misalign every channel from the first ragged buffer onward.
    private var frameSum = 0
    private var frameCount = 0

    /** Feed one interleaved sample, in decoder order. */
    fun add(sample: Int) {
        frameSum += sample
        if (++frameCount < channels) return
        val mono = frameSum / channels
        frameSum = 0
        frameCount = 0
        onSample(mono.toShort())
    }
}
