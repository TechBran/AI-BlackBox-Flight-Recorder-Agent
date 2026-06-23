package com.aiblackbox.portal.data.voice

import android.media.MediaPlayer
import android.media.audiofx.Visualizer
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlin.math.sqrt

/**
 * Singleton audio playback manager — survives composable disposal (scrolling).
 * Only stops when explicitly paused/stopped or when release() is called (app close).
 *
 * While playing, a [Visualizer] taps the player's audio session and publishes a
 * real-time output [amplitude] (0f..1f) so UI (e.g. AudioPlayerBar's red ribbon)
 * reacts to the ACTUAL audio instead of a synthetic timer. Requires RECORD_AUDIO
 * (already granted for mic/STT). If the Visualizer cannot attach on a given
 * device, [visualizerActive] stays false and callers fall back to a synthetic
 * pulse.
 */
object AudioPlaybackManager {
    private const val TAG = "AudioPlayback"

    private var mediaPlayer: MediaPlayer? = null
    private var currentUrl: String? = null
    private var autoPlayOnPrepare = false

    // Real-time output visualizer (amplitude source for the ribbon).
    private var visualizer: Visualizer? = null

    private val _isPlaying = MutableStateFlow(false)
    val isPlaying: StateFlow<Boolean> = _isPlaying.asStateFlow()

    private val _isPrepared = MutableStateFlow(false)
    val isPrepared: StateFlow<Boolean> = _isPrepared.asStateFlow()

    private val _duration = MutableStateFlow(0L)
    val duration: StateFlow<Long> = _duration.asStateFlow()

    private val _position = MutableStateFlow(0f)
    val position: StateFlow<Float> = _position.asStateFlow()

    private val _activeUrl = MutableStateFlow<String?>(null)
    val activeUrl: StateFlow<String?> = _activeUrl.asStateFlow()

    private val _hasError = MutableStateFlow(false)
    val hasError: StateFlow<Boolean> = _hasError.asStateFlow()

    // Real-time output amplitude (0f..1f) from the Visualizer while playing.
    // Dips toward 0 on silence; exactly 0 (with visualizerActive=false) when the
    // Visualizer is unavailable so callers can choose a synthetic fallback.
    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    private val _visualizerActive = MutableStateFlow(false)
    val visualizerActive: StateFlow<Boolean> = _visualizerActive.asStateFlow()

    /** Load and prepare audio from URL. If already loaded, does nothing. */
    fun load(url: String) {
        if (url == currentUrl && mediaPlayer != null) return
        stop()
        currentUrl = url
        _activeUrl.value = url
        _hasError.value = false
        _isPrepared.value = false
        _position.value = 0f
        autoPlayOnPrepare = false

        try {
            val player = MediaPlayer()
            player.setDataSource(url)
            player.setOnPreparedListener { mp ->
                _duration.value = mp.duration.toLong()
                _isPrepared.value = true
                // Tap the output for a real amplitude signal before playback starts.
                attachVisualizer(mp.audioSessionId)
                // Auto-play if play() was called before prepare finished
                if (autoPlayOnPrepare) {
                    autoPlayOnPrepare = false
                    mp.start()
                    _isPlaying.value = true
                    enableVisualizer(true)
                }
            }
            player.setOnCompletionListener {
                _isPlaying.value = false
                _position.value = 0f
                enableVisualizer(false)
                try { it.seekTo(0) } catch (_: Exception) {}
            }
            player.setOnErrorListener { _, what, extra ->
                Log.e(TAG, "MediaPlayer error: what=$what extra=$extra url=$url")
                _hasError.value = true
                _isPlaying.value = false
                autoPlayOnPrepare = false
                enableVisualizer(false)
                true
            }
            player.prepareAsync()
            mediaPlayer = player
        } catch (e: Exception) {
            Log.e(TAG, "Failed to load: ${e.message}", e)
            _hasError.value = true
        }
    }

    /** Load and immediately play (queues auto-play if still preparing) */
    fun loadAndPlay(url: String) {
        if (url == currentUrl && mediaPlayer != null && _isPrepared.value) {
            // Already loaded and ready — just play
            play()
            return
        }
        load(url)
        autoPlayOnPrepare = true
    }

    fun play() {
        val mp = mediaPlayer ?: return
        if (!_isPrepared.value) {
            // Not ready yet — queue it
            autoPlayOnPrepare = true
            return
        }
        try {
            mp.start()
            _isPlaying.value = true
            enableVisualizer(true)
        } catch (e: Exception) {
            Log.e(TAG, "Play failed: ${e.message}")
            _hasError.value = true
        }
    }

    fun pause() {
        autoPlayOnPrepare = false
        try { mediaPlayer?.pause() } catch (_: Exception) {}
        _isPlaying.value = false
        enableVisualizer(false)
    }

    fun togglePlayPause() {
        if (_isPlaying.value) pause() else play()
    }

    fun seekTo(fraction: Float) {
        val mp = mediaPlayer ?: return
        if (!_isPrepared.value) return
        val seekMs = (fraction * mp.duration).toInt()
        mp.seekTo(seekMs)
        _position.value = fraction
    }

    /** Update position — call from a polling coroutine */
    fun updatePosition() {
        try {
            val mp = mediaPlayer
            if (mp != null && _isPrepared.value && _isPlaying.value) {
                val dur = mp.duration.toLong()
                if (dur > 0) {
                    _position.value = mp.currentPosition.toFloat() / dur.toFloat()
                }
            }
        } catch (_: Exception) {}
    }

    fun stop() {
        releaseVisualizer()
        try {
            mediaPlayer?.let { mp ->
                if (mp.isPlaying) mp.stop()
                mp.release()
            }
        } catch (_: Exception) {}
        mediaPlayer = null
        currentUrl = null
        _activeUrl.value = null
        _isPlaying.value = false
        _isPrepared.value = false
        _duration.value = 0L
        _position.value = 0f
        _hasError.value = false
    }

    /** Call from Activity onDestroy */
    fun release() {
        stop()
    }

    /**
     * App went to background: pause output capture (no on-screen consumer).
     * Playback continues; only the Visualizer is disabled.
     */
    fun onAppBackground() {
        if (_isPlaying.value) enableVisualizer(false)
    }

    /** App returned to foreground: resume capture if still playing. */
    fun onAppForeground() {
        if (_isPlaying.value) enableVisualizer(true)
    }

    // =========================================================================
    // Visualizer - real-time output amplitude for the ribbon
    // =========================================================================

    // Envelope smoothing applied in the capture callback: instant attack (track
    // loud syllables) + slower release (ribbon glides down) so motion reads as
    // natural, not strobing. The UI layer eases on top of this.
    private const val RELEASE = 0.80f

    // Silence watchdog: some emulators/OEM builds attach + enable a Visualizer
    // successfully but only ever emit an all-128 (silent) buffer. Without this,
    // visualizerActive would stay true with a dead ribbon and the synthetic
    // fallback would never fire. If we see no real signal for SILENT_FRAME_LIMIT
    // consecutive frames we declare the engine dead; once ANY real signal
    // arrives we trust it (quiet passages just dip the ribbon).
    private const val SIGNAL_EPS = 0.008f
    private const val SILENT_FRAME_LIMIT = 30   // ~1.5s at the ~20Hz capture rate
    @Volatile private var sawSignal = false
    @Volatile private var silentFrames = 0

    private fun attachVisualizer(sessionId: Int) {
        releaseVisualizer()
        // Construct first; only the ctor allocates the native engine.
        val vis = try {
            Visualizer(sessionId)
        } catch (e: Exception) {
            Log.w(TAG, "Visualizer ctor failed: ${e.message}")
            return
        }
        // Assign the field immediately so a throw during configuration below is
        // recoverable via releaseVisualizer() (otherwise the native engine leaks).
        visualizer = vis
        try {
            val sizeRange = Visualizer.getCaptureSizeRange()  // e.g. [128, 1024]
            vis.setCaptureSize(512.coerceIn(sizeRange[0], sizeRange[1]))
            vis.setDataCaptureListener(
                object : Visualizer.OnDataCaptureListener {
                    override fun onWaveFormDataCapture(v: Visualizer?, waveform: ByteArray?, samplingRate: Int) {
                        if (waveform == null || waveform.isEmpty()) return
                        // PCM8 waveform is centered at 128; RMS of the deviation -> 0f..1f.
                        var sumSq = 0.0
                        for (b in waveform) {
                            val centered = (b.toInt() and 0xFF) - 128
                            sumSq += (centered * centered).toDouble()
                        }
                        val rms = (sqrt(sumSq / waveform.size) / 128.0).toFloat().coerceIn(0f, 1f)
                        // Silence watchdog (see fields above).
                        if (!sawSignal) {
                            if (rms > SIGNAL_EPS) {
                                sawSignal = true
                                _visualizerActive.value = true   // real signal (possibly after leading silence)
                            } else if (++silentFrames >= SILENT_FRAME_LIMIT) {
                                _visualizerActive.value = false  // attached but dead -> UI uses synthetic pulse
                            }
                        }
                        // Atomic read-modify-write so concurrent callbacks don't lose updates.
                        _amplitude.update { prev -> if (rms >= prev) rms else (prev * RELEASE + rms * (1f - RELEASE)) }
                    }
                    override fun onFftDataCapture(v: Visualizer?, fft: ByteArray?, samplingRate: Int) {}
                },
                Visualizer.getMaxCaptureRate(),  // milliHz; system-capped (~20Hz). UI eases the rest.
                true,   // capture waveform
                false   // no FFT
            )
        } catch (e: Exception) {
            Log.w(TAG, "Visualizer config failed: ${e.message}")
            releaseVisualizer()
        }
    }

    private fun enableVisualizer(enabled: Boolean) {
        val vis = visualizer
        if (vis == null) {
            _visualizerActive.value = false
            if (!enabled) _amplitude.value = 0f
            return
        }
        try {
            vis.setEnabled(enabled)
            _visualizerActive.value = enabled
            if (enabled) { sawSignal = false; silentFrames = 0 }
        } catch (e: Exception) {
            Log.w(TAG, "Visualizer enable($enabled) failed: ${e.message}")
            _visualizerActive.value = false
        }
        if (!enabled) _amplitude.value = 0f
    }

    private fun releaseVisualizer() {
        try { visualizer?.setEnabled(false) } catch (_: Exception) {}
        try { visualizer?.release() } catch (_: Exception) {}
        visualizer = null
        _visualizerActive.value = false
        _amplitude.value = 0f
    }
}
