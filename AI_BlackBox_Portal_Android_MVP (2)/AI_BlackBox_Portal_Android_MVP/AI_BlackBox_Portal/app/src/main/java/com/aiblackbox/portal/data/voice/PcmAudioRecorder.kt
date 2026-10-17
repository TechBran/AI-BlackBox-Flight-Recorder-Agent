package com.aiblackbox.portal.data.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.util.Log
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BINS
import com.aiblackbox.portal.ui.components.aurora.AnalysisFrame
import com.aiblackbox.portal.ui.components.aurora.AudioAnalysisTap
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The composer's raw-audio recorder: PCM to a WAV file AND to the shared analysis tap, from one
 * capture loop.
 *
 * WHAT IT REPLACES. This button's ribbon was frozen. It was fed
 * `floatArrayOf(getMaxAmplitude() / 32767f)` — one peak number out of a `MediaRecorder`, read once,
 * with nothing recomposing it while recording. `MediaRecorder` yields no PCM at all, so there was
 * no spectrum to draw and no second frame to draw it on. An `AudioRecord` feeding
 * [AudioAnalysisTap] puts this surface on the same pipeline as the dictation mic and the player
 * bar: real bands, real bins, at the tap's own cadence.
 *
 * WHY NOT CHANGE [AudioRecorderManager]. It is SHARED with Voice Lab's ElevenLabs voice cloning.
 * Changing its capture format in place would silently change what gets cloned, which is not a
 * side effect a waveform fix is allowed to have.
 *
 * WHY 16 kHz MONO PCM16 WAV. Gemini downsamples to 16 kbps mono and bills 32 tokens/sec whatever it
 * is handed, so a bigger file buys nothing; `audio/wav` is on Gemini's, OpenAI's and ElevenLabs'
 * accepted lists while `.m4a` is not on Gemini's; 16 kHz clears the analyser's floor (the top
 * Goertzel probe is 6800 Hz and probes must sit under `sampleRate * 0.45`, i.e. above 15111 Hz);
 * and 44.1 kHz would put a five-minute take near 30 MB, past OpenAI's hard 25 MB cap.
 *
 * Constructible on the JVM on purpose — the Android surface is opened inside [start] — so the
 * resting contract and the no-microphone path are unit-tested.
 *
 * @param outputDir where the take is written (the caller's cache dir).
 * @param unprocessedSupported [MicSourcePolicy.supportsUnprocessed] for this device, resolved by
 *   the caller because it needs a `Context` and this class deliberately does not take one.
 * @param recordFactory builds the mono PCM16 `AudioRecord` for one source, rate and buffer size.
 *   Production always takes the default (the platform constructor); it is a seam so a JVM test can
 *   hand the capture loop a recorder that actually delivers audio and reports which thread stopped
 *   and released it — the read()/release() race is invisible to a stub that never opens.
 */
class PcmAudioRecorder(
    private val outputDir: File,
    private val unprocessedSupported: Boolean,
    private val recordFactory: (androidSource: Int, sampleRate: Int, bufferBytes: Int) -> AudioRecord =
        { androidSource, sampleRate, bufferBytes ->
            AudioRecord(
                androidSource,
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferBytes,
            )
        },
) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val _bands = MutableStateFlow(AURORA_SILENT_BANDS)
    val bands: StateFlow<FloatArray> = _bands.asStateFlow()

    private val _bins = MutableStateFlow(AURORA_SILENT_BINS)
    val bins: StateFlow<FloatArray> = _bins.asStateFlow()

    @Volatile private var capturing = false
    private var writer: WavStreamWriter? = null
    private var file: File? = null

    /**
     * The current take's stop signal — one per take, flipped by [stop]. Per take and not one shared
     * flag, because a quick stop() + start() would flip a shared flag straight back on while the old
     * take's loop was still inside read(), and that loop would carry on into the new take.
     */
    private var takeLive: AtomicBoolean? = null

    val isRecording: Boolean get() = capturing

    /**
     * Open the mic and start writing. False when no source would open at all, in which case nothing
     * is left behind — no half-written header, no ribbon stuck at its last value.
     */
    fun start(): Boolean {
        if (capturing) return true

        val minBufferBytes = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        val frames = MicBuffer.framesForMs(READ_MS, SAMPLE_RATE)
        val bufferBytes = MicBuffer.recordBufferBytesForFrames(frames, minBufferBytes)

        // SIMPLEX: nothing of ours is playing while this mic is open, so there is no echo to
        // cancel. UNPROCESSED is only ever in the chain when the DEVICE says it implements it —
        // see MicSourcePolicy for why a try/catch cannot detect that.
        val opened = MicSourcePolicy.openFirst(
            MicSourcePolicy.simplexChain(unprocessedSupported)
        ) { source ->
            try {
                val candidate = recordFactory(source.androidSource, SAMPLE_RATE, bufferBytes)
                if (candidate.state == AudioRecord.STATE_INITIALIZED) {
                    candidate
                } else {
                    Log.w(TAG, "${source.name} did not initialize — trying the next source")
                    try { candidate.release() } catch (_: Exception) {}
                    null
                }
            } catch (e: Exception) {
                Log.w(TAG, "${source.name} construct failed: ${e.message}", e)
                null
            }
        }
        if (opened == null) {
            Log.e(TAG, "No mic source could be opened")
            return false
        }
        val rec = opened.record

        // What the hardware ACTUALLY gave us, which is what the header must declare: AudioRecord
        // guarantees only 44100 Hz, so the requested rate is a request. Both the tap's probe
        // frequencies and the file's playback speed depend on getting this from the device.
        val reportedRate = try { rec.sampleRate } catch (_: Exception) { 0 }
        val actualRate = if (reportedRate > 0) reportedRate else SAMPLE_RATE

        val out = File(outputDir, pcmRecordingFileName(System.currentTimeMillis()))
        val wav = WavStreamWriter(out, actualRate)
        try {
            outputDir.mkdirs()
            wav.open()
        } catch (e: Exception) {
            Log.e(TAG, "Could not open ${out.name}: ${e.message}", e)
            try { rec.release() } catch (_: Exception) {}
            out.delete()
            return false
        }

        try {
            rec.startRecording()
        } catch (e: Exception) {
            Log.e(TAG, "startRecording failed: ${e.message}", e)
            wav.close()
            out.delete()
            try { rec.release() } catch (_: Exception) {}
            return false
        }

        writer = wav
        file = out
        val live = AtomicBoolean(true)
        takeLive = live
        capturing = true

        // The tap's calibration travels with the source that opened — the analyser's constants
        // describe a stream shape, and UNPROCESSED is a different shape from a platform-processed
        // one. A fresh tap per take, because it owns the session's auto-gain.
        val tap = AudioAnalysisTap(actualRate, calibration = opened.source.calibration, label = "composer-mic/${opened.source}")
        Log.d(TAG, "Recording ${out.name}: ${actualRate}Hz, source=${opened.source.name}")

        // The loop owns this take outright: it reads [rec] and writes [wav] through LOCALS, never the
        // fields, so a newer take swapping the fields can never be read from or written into by it.
        // Never cancelled, on purpose — a launch cancelled before it is dispatched never runs its
        // body, and the body's finally is the ONLY place this take's recorder is released.
        scope.launch {
            // Hoisted: the tap calls this synchronously once per hop, and allocating a lambda per
            // read would put a capture in the audio path for nothing.
            val publish: (AnalysisFrame) -> Unit = { frame ->
                _bands.value = frame.bands
                _bins.value = frame.bins
            }
            val buffer = ByteArray(frames * MicBuffer.BYTES_PER_SAMPLE)
            try {
                while (live.get()) {
                    val readCount = rec.read(buffer, 0, buffer.size)
                    // A stop() that landed while this read was in flight ends the take HERE: the
                    // chunk belongs to a take that is over, and its file is already patched shut.
                    if (!live.get()) break
                    if (readCount > 0) {
                        // Analysed and written from the SAME bytes, and only the valid ones: a
                        // short read leaves the previous read's audio in the tail of the buffer.
                        tap.offer(buffer, readCount, publish)
                        wav.write(buffer, readCount)
                    } else if (readCount < 0) {
                        Log.w(TAG, "AudioRecord.read error $readCount — ending the take")
                        break
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Capture loop error: ${e.message}", e)
            } finally {
                // Stopped and released HERE, on the SAME thread as read(). stop() used to do it on
                // the caller's thread (the composer tap, on main), which raced this loop's in-flight
                // read() — the native abort in AudioRecord::releaseBuffer
                // (feedback_android_audiorecord_race).
                try { rec.stop() } catch (_: Exception) {}
                try { rec.release() } catch (_: Exception) {}
                silenceRibbon()
            }
        }
        return true
    }

    /**
     * Stop and return the finished WAV, or null if nothing was captured.
     *
     * Only SIGNALS the capture loop: it stops and releases the mic itself, in its own finally, within
     * one read (see the loop for why it must be that thread). The loop is told to stop BEFORE the
     * writer is closed, and [WavStreamWriter] drops a write that loses that race, so the size patch
     * describes every byte the file actually holds; the read in flight at the tap is not part of the
     * take.
     */
    fun stop(): File? {
        if (!capturing) return null
        capturing = false
        takeLive?.set(false)
        takeLive = null
        writer?.close()
        writer = null
        silenceRibbon()
        val out = file
        file = null
        Log.d(TAG, "Recording stopped: ${out?.name} (${out?.length() ?: 0} bytes)")
        return out
    }

    /** Abandon a take: stop everything and delete the file. */
    fun cancel() {
        stop()?.delete()
    }

    /** Both channels fall TOGETHER — a ribbon left holding its last bands freezes on screen. */
    private fun silenceRibbon() {
        _bands.value = AURORA_SILENT_BANDS
        _bins.value = AURORA_SILENT_BINS
    }

    companion object {
        private const val TAG = "PcmAudioRecorder"

        /** See the class note: the provider floor, the analyser floor and the size cap all agree. */
        const val SAMPLE_RATE = 16000

        /** Audio per read. Matches [AudioAnalysisTap.HOP_MS] so one read is about one frame. */
        const val READ_MS = 20f
    }
}
