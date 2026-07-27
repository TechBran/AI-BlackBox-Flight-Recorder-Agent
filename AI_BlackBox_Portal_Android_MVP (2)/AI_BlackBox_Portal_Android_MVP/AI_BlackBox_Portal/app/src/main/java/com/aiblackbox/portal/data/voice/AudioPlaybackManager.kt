package com.aiblackbox.portal.data.voice

import android.media.MediaPlayer
import android.util.Log
import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
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
 * The playback ribbon is driven by the ACTUAL audio bytes: on load the file's PCM is decoded into
 * per-window envelopes (AudioEnvelope), and a ~60fps tick samples them at the live playback
 * position. This tracks the speech smoothly + in sync, flattens to 0 on real silence, and works on
 * every device (no Visualizer / no device-specific flakiness). [amplitudeReady] turns true once the
 * decode lands; until then callers may show a fallback.
 *
 * [bands] is what the Aurora ribbon consumes and it degrades in one place rather than at the call
 * site: four real band energies when the clip's band pass ran, a single RMS value when it did not
 * (a track that declared no usable sample rate, or one too short to probe — clip LENGTH is no
 * longer a reason), and silence before the decode finishes. A one-value array still animates all
 * four sheets — see AuroraVoice.band.
 */
object AudioPlaybackManager {
    private const val TAG = "AudioPlayback"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private var mediaPlayer: MediaPlayer? = null
    private var currentUrl: String? = null
    private var autoPlayOnPrepare = false

    // Decoded envelopes of the current clip (peak-normalized 0..1).
    @Volatile private var envelope: FloatArray? = null
    @Volatile private var bandEnvelope: Array<FloatArray> = emptyArray()
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
    //
    // Still computed and still exposed: it is what [bands] degrades TO for a clip too long to
    // analyse, and it is what the previous renderer consumed — which is on disk pending Brandon's
    // on-device review of the Aurora ribbon. It goes when that renderer does.
    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    /**
     * Aurora band energies at the live playback position. Silent (and CONFLATING — one shared
     * instance, so a paused bar costs no recompositions) whenever nothing is playing.
     */
    private val _bands = MutableStateFlow(AURORA_SILENT_BANDS)
    val bands: StateFlow<FloatArray> = _bands.asStateFlow()

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
                _bands.value = AURORA_SILENT_BANDS
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
        _bands.value = AURORA_SILENT_BANDS
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
        bandEnvelope = emptyArray()
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
        _bands.value = AURORA_SILENT_BANDS
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
        bandEnvelope = emptyArray()
        _amplitudeReady.value = false
        decodeJob = scope.launch(Dispatchers.IO) {
            val result = AudioEnvelope.decode(url)
            if (result != null && currentUrl == url) {
                envelope = result.rms
                bandEnvelope = result.bands
                envWindowMs = result.windowMs
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
                    val level = sampleEnvelope(env, posMs, envWindowMs)
                    _amplitude.value = level
                    // Real bands when the clip's band pass ran; otherwise the RMS level alone,
                    // which AuroraVoice spreads across all four sheets. Degrading HERE keeps the
                    // player bar from having to know the difference. Rare now that the band pass
                    // has no length limit — a bogus container rate is what is left.
                    val table = bandEnvelope
                    _bands.value = if (table.isEmpty()) floatArrayOf(level)
                    else sampleBands(table, posMs, envWindowMs)
                }
                delay(16)  // ~60fps
            }
        }
    }

    private fun stopAmplitudeTick() {
        ampJob?.cancel(); ampJob = null
    }
}

// -----------------------------------------------------------------------------
// Envelope sampling — pure, top-level + internal so it is unit-testable without
// touching MediaPlayer (the object above cannot be constructed on the JVM).
// -----------------------------------------------------------------------------

/** RMS loudness at [posMs], linearly interpolated between windows and clamped at both ends. */
internal fun sampleEnvelope(env: FloatArray, posMs: Int, windowMs: Int): Float {
    // The empty/zero-window guards are new: the caller has always checked isNotEmpty() first, but
    // this is a scrubbable ribbon and `env.first()` on an empty array is a crash one refactor away.
    if (env.isEmpty() || windowMs <= 0) return 0f
    val fidx = posMs.toFloat() / windowMs
    val i0 = fidx.toInt()
    if (i0 < 0) return env.first()
    if (i0 >= env.size - 1) return env.last()
    val frac = fidx - i0
    return env[i0] * (1f - frac) + env[i0 + 1] * frac
}

/**
 * Band energies at [posMs], interpolated and clamped exactly like [sampleEnvelope].
 *
 * Clamping matters more here than it looks: the band table is legitimately allowed to be one window
 * SHORTER than the RMS envelope it is sampled alongside (their trailing-window rules differ), so
 * the end of every clip indexes past it.
 */
internal fun sampleBands(bands: Array<FloatArray>, posMs: Int, windowMs: Int): FloatArray {
    if (bands.isEmpty() || windowMs <= 0) return FloatArray(AuroraAnalyser.BANDS)
    val fidx = posMs.toFloat() / windowMs
    val i0 = fidx.toInt()
    // Copies, never the table's own row: the result is published through a StateFlow and would
    // otherwise alias a value the ribbon already believes it holds.
    if (i0 < 0) return bands.first().copyOf()
    if (i0 >= bands.size - 1) return bands.last().copyOf()
    val frac = fidx - i0
    val a = bands[i0]
    val b = bands[i0 + 1]
    return FloatArray(AuroraAnalyser.BANDS) { j ->
        a.getOrElse(j) { 0f } * (1f - frac) + b.getOrElse(j) { 0f } * frac
    }
}
