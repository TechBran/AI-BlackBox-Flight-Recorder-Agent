package com.aiblackbox.portal.data.voice

import android.media.MediaPlayer
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Singleton audio playback manager - survives composable disposal (scrolling).
 *
 * The playback ribbon's [amplitude] is driven by the ACTUAL audio bytes: on load
 * the file's PCM is decoded into a per-window RMS envelope (AudioEnvelope), and a
 * ~60fps tick samples that envelope at the live playback position. This tracks the
 * speech smoothly + in sync, flattens to 0 on real silence, and works on every
 * device (no Visualizer / no device-specific flakiness). [amplitudeReady] turns
 * true once the envelope is decoded; until then callers may show a fallback.
 */
object AudioPlaybackManager {
    private const val TAG = "AudioPlayback"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private var mediaPlayer: MediaPlayer? = null
    private var currentUrl: String? = null
    private var autoPlayOnPrepare = false

    // Decoded amplitude envelope of the current clip (peak-normalized 0..1).
    @Volatile private var envelope: FloatArray? = null
    @Volatile private var envWindowMs: Int = 20
    private var decodeJob: Job? = null
    private var ampJob: Job? = null

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

    // Ribbon amplitude (0..1), sampled from the decoded envelope at the live
    // playback position. 0 when not playing / before the envelope is ready.
    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    // True once the current clip's amplitude envelope has decoded.
    private val _amplitudeReady = MutableStateFlow(false)
    val amplitudeReady: StateFlow<Boolean> = _amplitudeReady.asStateFlow()

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
        decodeEnvelope(url)
        try {
            val player = MediaPlayer()
            player.setDataSource(url)
            player.setOnPreparedListener { mp ->
                _duration.value = mp.duration.toLong()
                _isPrepared.value = true
                if (autoPlayOnPrepare) {
                    autoPlayOnPrepare = false
                    mp.start()
                    _isPlaying.value = true
                    startAmplitudeTick()
                }
            }
            player.setOnCompletionListener {
                _isPlaying.value = false
                _position.value = 0f
                stopAmplitudeTick()
                _amplitude.value = 0f
                try { it.seekTo(0) } catch (_: Exception) {}
            }
            player.setOnErrorListener { _, what, extra ->
                Log.e(TAG, "MediaPlayer error: what=$what extra=$extra url=$url")
                _hasError.value = true
                _isPlaying.value = false
                autoPlayOnPrepare = false
                stopAmplitudeTick()
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
            play()
            return
        }
        load(url)
        autoPlayOnPrepare = true
    }

    fun play() {
        val mp = mediaPlayer ?: return
        if (!_isPrepared.value) {
            autoPlayOnPrepare = true
            return
        }
        try {
            mp.start()
            _isPlaying.value = true
            startAmplitudeTick()
        } catch (e: Exception) {
            Log.e(TAG, "Play failed: ${e.message}")
            _hasError.value = true
        }
    }

    fun pause() {
        autoPlayOnPrepare = false
        try { mediaPlayer?.pause() } catch (_: Exception) {}
        _isPlaying.value = false
        stopAmplitudeTick()
        _amplitude.value = 0f
    }

    fun togglePlayPause() {
        if (_isPlaying.value) pause() else play()
    }

    fun seekTo(fraction: Float) {
        val mp = mediaPlayer ?: return
        if (!_isPrepared.value) return
        mp.seekTo((fraction * mp.duration).toInt())
        _position.value = fraction
    }

    /** Update position - call from a polling coroutine */
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
        stopAmplitudeTick()
        decodeJob?.cancel(); decodeJob = null
        envelope = null
        _amplitudeReady.value = false
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
        _amplitude.value = 0f
    }

    /** Call from Activity onDestroy */
    fun release() {
        stop()
    }

    /** App backgrounded: stop the amplitude tick (no on-screen consumer). Playback continues. */
    fun onAppBackground() {
        stopAmplitudeTick()
    }

    /** App foregrounded: resume the amplitude tick if still playing. */
    fun onAppForeground() {
        if (_isPlaying.value) startAmplitudeTick()
    }

    // --- Amplitude envelope (decoded from the actual audio bytes) ------------

    private fun decodeEnvelope(url: String) {
        decodeJob?.cancel()
        envelope = null
        _amplitudeReady.value = false
        decodeJob = scope.launch(Dispatchers.IO) {
            val result = AudioEnvelope.decode(url)
            if (result != null && currentUrl == url) {
                envelope = result.first
                envWindowMs = result.second
                _amplitudeReady.value = true
            }
        }
    }

    private fun startAmplitudeTick() {
        if (ampJob?.isActive == true) return
        ampJob = scope.launch {
            while (isActive) {
                val mp = mediaPlayer
                val env = envelope
                if (mp != null && _isPlaying.value && env != null && env.isNotEmpty()) {
                    val posMs = try { mp.currentPosition } catch (_: Exception) { 0 }
                    _amplitude.value = sampleEnvelope(env, posMs)
                }
                delay(16)  // ~60fps
            }
        }
    }

    private fun stopAmplitudeTick() {
        ampJob?.cancel(); ampJob = null
    }

    private fun sampleEnvelope(env: FloatArray, posMs: Int): Float {
        val fidx = posMs.toFloat() / envWindowMs
        val i0 = fidx.toInt()
        if (i0 < 0) return env.first()
        if (i0 >= env.size - 1) return env.last()
        val frac = fidx - i0
        return env[i0] * (1f - frac) + env[i0 + 1] * frac
    }
}
