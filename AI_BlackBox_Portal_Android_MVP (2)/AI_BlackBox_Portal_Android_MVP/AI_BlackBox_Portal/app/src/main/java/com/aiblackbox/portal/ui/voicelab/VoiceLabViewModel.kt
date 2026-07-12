package com.aiblackbox.portal.ui.voicelab

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.repository.CloneResult
import com.aiblackbox.portal.data.repository.DesignPreview
import com.aiblackbox.portal.data.repository.ElevenLabsStatus
import com.aiblackbox.portal.data.repository.ElevenVoice
import com.aiblackbox.portal.data.repository.SharedVoice
import com.aiblackbox.portal.data.repository.VoiceLabException
import com.aiblackbox.portal.data.repository.VoiceLabRepository
import com.aiblackbox.portal.data.repository.XaiVoice
import com.aiblackbox.portal.data.voice.AudioRecorderManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream

// =============================================================================
// VoiceLabViewModel — drives the three Voice Lab zones (Task 25).
//
//   Clone  : record (AudioRecorderManager) or pick a file → name + consent →
//            multipart POST /elevenlabs/voices/clone.
//   Design : description → POST /design → 3 preview candidates → "Use this" →
//            name → POST /design/save.
//   Manage : GET /elevenlabs/voices → my_voices list → DELETE (with in_use warn).
//
// Status gating (GET /elevenlabs/status) decides whether the screen renders at
// all (configured) and whether the Clone zone is enabled (instant_voice_cloning).
//
// Recording reuses AudioRecorderManager (MediaRecorder → .m4a + getMaxAmplitude
// for the level meter) — the hard-won, lifecycle-safe mic capture path. We do
// NOT touch AudioRecord directly.
// =============================================================================

enum class CloneState { IDLE, SUBMITTING, DONE }
enum class DesignState { IDLE, GENERATING, READY, SAVING }
enum class RecordState { IDLE, RECORDING }

/** A captured/picked clip queued for cloning. file lives in cacheDir. */
data class ClonePart(val file: File, val displayName: String, val fromRecording: Boolean)

class VoiceLabViewModel(application: Application) : AndroidViewModel(application) {

    private var api: BlackBoxApi? = null
    private var repo: VoiceLabRepository? = null
    private val recorder = AudioRecorderManager(application)
    private var originBase = ""

    // ── Status / gating ──────────────────────────────────────────────────────
    private val _status = MutableStateFlow<ElevenLabsStatus?>(null)
    val status: StateFlow<ElevenLabsStatus?> = _status.asStateFlow()
    private val _statusLoaded = MutableStateFlow(false)
    val statusLoaded: StateFlow<Boolean> = _statusLoaded.asStateFlow()

    // ── Snackbar channel ─────────────────────────────────────────────────────
    private val _message = MutableStateFlow<String?>(null)
    val message: StateFlow<String?> = _message.asStateFlow()
    fun clearMessage() { _message.value = null }

    // ── Clone zone ───────────────────────────────────────────────────────────
    private val _cloneState = MutableStateFlow(CloneState.IDLE)
    val cloneState: StateFlow<CloneState> = _cloneState.asStateFlow()
    private val _cloneParts = MutableStateFlow<List<ClonePart>>(emptyList())
    val cloneParts: StateFlow<List<ClonePart>> = _cloneParts.asStateFlow()
    private val _cloneError = MutableStateFlow<String?>(null)
    val cloneError: StateFlow<String?> = _cloneError.asStateFlow()

    // Recording sub-state.
    private val _recordState = MutableStateFlow(RecordState.IDLE)
    val recordState: StateFlow<RecordState> = _recordState.asStateFlow()
    private val _recordElapsedMs = MutableStateFlow(0L)
    val recordElapsedMs: StateFlow<Long> = _recordElapsedMs.asStateFlow()
    private val _recordAmplitude = MutableStateFlow(0)
    val recordAmplitude: StateFlow<Int> = _recordAmplitude.asStateFlow()
    private var recordTicker: Job? = null
    private var recordStartedAt = 0L

    // ── Design zone ──────────────────────────────────────────────────────────
    private val _designState = MutableStateFlow(DesignState.IDLE)
    val designState: StateFlow<DesignState> = _designState.asStateFlow()
    private val _designPreviews = MutableStateFlow<List<DesignPreview>>(emptyList())
    val designPreviews: StateFlow<List<DesignPreview>> = _designPreviews.asStateFlow()
    private val _designText = MutableStateFlow("")
    val designText: StateFlow<String> = _designText.asStateFlow()
    private val _designError = MutableStateFlow<String?>(null)
    val designError: StateFlow<String?> = _designError.asStateFlow()

    // ── Manage zone ──────────────────────────────────────────────────────────
    private val _myVoices = MutableStateFlow<List<ElevenVoice>>(emptyList())
    val myVoices: StateFlow<List<ElevenVoice>> = _myVoices.asStateFlow()
    private val _voicesLoading = MutableStateFlow(false)
    val voicesLoading: StateFlow<Boolean> = _voicesLoading.asStateFlow()

    // ── Browse Library zone ────────────────────────────────────────────────────
    private val _libraryQuery = MutableStateFlow("")
    val libraryQuery: StateFlow<String> = _libraryQuery.asStateFlow()
    private val _libraryResults = MutableStateFlow<List<SharedVoice>>(emptyList())
    val libraryResults: StateFlow<List<SharedVoice>> = _libraryResults.asStateFlow()
    private val _librarySearching = MutableStateFlow(false)
    val librarySearching: StateFlow<Boolean> = _librarySearching.asStateFlow()
    private val _librarySearched = MutableStateFlow(false)
    val librarySearched: StateFlow<Boolean> = _librarySearched.asStateFlow()
    private val _libraryAddingId = MutableStateFlow<String?>(null)
    val libraryAddingId: StateFlow<String?> = _libraryAddingId.asStateFlow()

    // ── xAI (Grok) custom voices zone ────────────────────────────────────────
    private val _xaiConfigured = MutableStateFlow(false)
    val xaiConfigured: StateFlow<Boolean> = _xaiConfigured.asStateFlow()
    private val _xaiVoices = MutableStateFlow<List<XaiVoice>>(emptyList())
    val xaiVoices: StateFlow<List<XaiVoice>> = _xaiVoices.asStateFlow()
    private val _xaiCloneState = MutableStateFlow(CloneState.IDLE)
    val xaiCloneState: StateFlow<CloneState> = _xaiCloneState.asStateFlow()
    private val _xaiCloneError = MutableStateFlow<String?>(null)
    val xaiCloneError: StateFlow<String?> = _xaiCloneError.asStateFlow()
    private val _xaiClonePart = MutableStateFlow<ClonePart?>(null)
    val xaiClonePart: StateFlow<ClonePart?> = _xaiClonePart.asStateFlow()

    companion object {
        private const val MAX_RECORD_MS = 5 * 60_000L // 5 min cap
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        originBase = origin
        api = BlackBoxApi(origin)
        repo = VoiceLabRepository(api!!)
        refreshStatus()
        loadXaiVoices()   // xAI zone gates on its own key, independent of ElevenLabs
    }

    /** Prefix relative preview/audio urls with the server origin (for playback). */
    fun absoluteUrl(url: String): String =
        if (url.isBlank() || url.startsWith("http")) url else "$originBase$url"

    // ── Status ────────────────────────────────────────────────────────────────
    fun refreshStatus() {
        val repo = repo ?: return
        viewModelScope.launch {
            try {
                val s = repo.fetchStatus()
                _status.value = s
                if (s.configured) loadVoices()
            } catch (_: Exception) {
                _status.value = ElevenLabsStatus(configured = false)
            } finally {
                _statusLoaded.value = true
            }
        }
    }

    // ── Recording (AudioRecorderManager) ───────────────────────────────────────
    /** Caller must have RECORD_AUDIO granted before invoking. */
    fun startRecording() {
        if (_recordState.value == RecordState.RECORDING) return
        val ok = recorder.startRecording()
        if (!ok) {
            _message.value = "Couldn't start recording (mic busy?)"
            return
        }
        recordStartedAt = System.currentTimeMillis()
        _recordElapsedMs.value = 0L
        _recordState.value = RecordState.RECORDING
        recordTicker = viewModelScope.launch {
            while (isActive && _recordState.value == RecordState.RECORDING) {
                val elapsed = System.currentTimeMillis() - recordStartedAt
                _recordElapsedMs.value = elapsed
                _recordAmplitude.value = recorder.getMaxAmplitude()
                if (elapsed >= MAX_RECORD_MS) { stopRecording(); break }
                delay(60)
            }
        }
    }

    fun stopRecording() {
        if (_recordState.value != RecordState.RECORDING) return
        recordTicker?.cancel(); recordTicker = null
        _recordState.value = RecordState.IDLE
        _recordAmplitude.value = 0
        val file = recorder.stopRecording()
        if (file != null && file.exists() && file.length() > 0) {
            val secs = (_recordElapsedMs.value / 1000).coerceAtLeast(1)
            addClonePart(ClonePart(file, "Recording (${secs}s)", fromRecording = true))
        } else {
            _message.value = "Recording was empty"
        }
    }

    /** Copy a picked content Uri into cacheDir and queue it as a clone part. */
    fun addPickedFile(uri: Uri) {
        viewModelScope.launch {
            try {
                val part = withContext(Dispatchers.IO) { copyUriToCache(uri) }
                addClonePart(part)
            } catch (e: Exception) {
                _message.value = "Couldn't read file: ${e.message}"
            }
        }
    }

    private fun copyUriToCache(uri: Uri): ClonePart {
        val ctx = getApplication<Application>()
        val name = queryDisplayName(uri) ?: "upload_${System.currentTimeMillis()}.audio"
        val dest = File(ctx.cacheDir, "voicelab_${System.currentTimeMillis()}_$name")
        ctx.contentResolver.openInputStream(uri)?.use { input ->
            FileOutputStream(dest).use { output -> input.copyTo(output) }
        } ?: throw IllegalStateException("Cannot open $uri")
        return ClonePart(dest, name, fromRecording = false)
    }

    private fun queryDisplayName(uri: Uri): String? {
        val ctx = getApplication<Application>()
        var name: String? = null
        ctx.contentResolver.query(uri, null, null, null, null)?.use { c ->
            val idx = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
            if (c.moveToFirst() && idx >= 0) name = c.getString(idx)
        }
        return name
    }

    private fun addClonePart(part: ClonePart) {
        _cloneParts.value = _cloneParts.value + part
        _cloneError.value = null
    }

    fun removeClonePart(index: Int) {
        val list = _cloneParts.value.toMutableList()
        if (index in list.indices) {
            val removed = list.removeAt(index)
            runCatching { removed.file.delete() }
            _cloneParts.value = list
        }
    }

    // ── Clone submit ───────────────────────────────────────────────────────────
    fun submitClone(name: String, description: String, removeNoise: Boolean, consent: Boolean) {
        val repo = repo ?: return
        val parts = _cloneParts.value
        if (name.isBlank() || parts.isEmpty() || !consent) {
            _cloneError.value = "Name, at least one clip, and consent are required."
            return
        }
        _cloneState.value = CloneState.SUBMITTING
        _cloneError.value = null
        viewModelScope.launch {
            try {
                val result: CloneResult = repo.cloneVoice(
                    name = name.trim(),
                    files = parts.map { it.file },
                    consent = consent,
                    description = description.trim(),
                    removeBackgroundNoise = removeNoise,
                )
                _cloneState.value = CloneState.DONE
                _message.value = if (result.requiresVerification)
                    "Voice cloned — verification required before use."
                else
                    "Voice \"${name.trim()}\" cloned."
                // Clean up local clips + reset the zone, then refresh My Voices.
                parts.forEach { runCatching { it.file.delete() } }
                _cloneParts.value = emptyList()
                loadVoices()
            } catch (e: VoiceLabException) {
                _cloneState.value = CloneState.IDLE
                _cloneError.value = when (e.status) {
                    422 -> "Consent is required to clone a voice."
                    400 -> "Clone rejected: ${e.message}"
                    else -> e.message
                }
            } catch (e: Exception) {
                _cloneState.value = CloneState.IDLE
                _cloneError.value = e.message ?: "Clone failed"
            }
        }
    }

    fun resetCloneDone() { if (_cloneState.value == CloneState.DONE) _cloneState.value = CloneState.IDLE }

    // ── Design ───────────────────────────────────────────────────────────────
    fun design(description: String, text: String) {
        val repo = repo ?: return
        if (description.isBlank()) {
            _designError.value = "Describe the voice first."
            return
        }
        _designState.value = DesignState.GENERATING
        _designError.value = null
        _designPreviews.value = emptyList()
        viewModelScope.launch {
            try {
                val result = repo.designVoice(description.trim(), text.trim())
                _designText.value = result.text
                _designPreviews.value = result.previews
                _designState.value = DesignState.READY
                if (result.previews.isEmpty()) _designError.value = "No previews returned."
            } catch (e: VoiceLabException) {
                _designState.value = DesignState.IDLE
                _designError.value = e.message
            } catch (e: Exception) {
                _designState.value = DesignState.IDLE
                _designError.value = e.message ?: "Design failed"
            }
        }
    }

    fun saveDesigned(generatedVoiceId: String, name: String, description: String) {
        val repo = repo ?: return
        if (name.isBlank()) { _designError.value = "Name the voice before saving."; return }
        _designState.value = DesignState.SAVING
        _designError.value = null
        viewModelScope.launch {
            try {
                val voiceId = repo.saveDesignedVoice(generatedVoiceId, name.trim(), description.trim())
                _message.value = if (voiceId.isNotBlank()) "Voice \"${name.trim()}\" saved." else "Voice saved."
                _designPreviews.value = emptyList()
                _designText.value = ""
                _designState.value = DesignState.IDLE
                loadVoices()
            } catch (e: VoiceLabException) {
                _designState.value = DesignState.READY
                _designError.value = e.message
            } catch (e: Exception) {
                _designState.value = DesignState.READY
                _designError.value = e.message ?: "Save failed"
            }
        }
    }

    fun clearDesignError() { _designError.value = null }

    // ── Manage ───────────────────────────────────────────────────────────────
    fun loadVoices() {
        val repo = repo ?: return
        _voicesLoading.value = true
        viewModelScope.launch {
            try {
                _myVoices.value = repo.fetchVoices().myVoices
            } catch (_: Exception) {
                // keep whatever we had; surface nothing loud (status gate covers config)
            } finally {
                _voicesLoading.value = false
            }
        }
    }

    fun deleteVoice(voiceId: String) {
        val repo = repo ?: return
        viewModelScope.launch {
            try {
                val res = repo.deleteVoice(voiceId)
                if (res.inUse.isNotEmpty()) {
                    _message.value = "Deleted, but it was in use by: ${res.inUse.joinToString(", ")}"
                } else {
                    _message.value = "Voice deleted."
                }
                loadVoices()
            } catch (e: VoiceLabException) {
                _message.value = "Delete failed: ${e.message}"
            } catch (e: Exception) {
                _message.value = "Delete failed: ${e.message}"
            }
        }
    }

    // ── Browse Library ─────────────────────────────────────────────────────────
    fun setLibraryQuery(q: String) { _libraryQuery.value = q }

    /** Search the public community library (search on submit/button — no debounce). */
    fun searchLibrary(query: String = _libraryQuery.value) {
        val repo = repo ?: return
        if (_librarySearching.value) return
        _libraryQuery.value = query
        _librarySearching.value = true
        viewModelScope.launch {
            try {
                _libraryResults.value = repo.searchLibrary(query)
                _librarySearched.value = true
            } catch (e: VoiceLabException) {
                _message.value = e.message ?: "Library search failed"
            } catch (e: Exception) {
                _message.value = e.message ?: "Library search failed"
            } finally {
                _librarySearching.value = false
            }
        }
    }

    /** Add a library voice to the account, then refresh My Voices + snackbar. */
    fun addLibraryVoice(sv: SharedVoice) {
        val repo = repo ?: return
        if (_libraryAddingId.value != null) return
        _libraryAddingId.value = sv.voiceId
        viewModelScope.launch {
            try {
                repo.addLibraryVoice(sv.publicOwnerId, sv.voiceId, sv.name)
                _message.value = "\"${sv.name}\" added to your voices."
                loadVoices() // reuse the existing My Voices refresh
            } catch (e: VoiceLabException) {
                _message.value = "Add failed: ${e.message}"
            } catch (e: Exception) {
                _message.value = "Add failed: ${e.message}"
            } finally {
                _libraryAddingId.value = null
            }
        }
    }

    // ── xAI (Grok) custom voices ───────────────────────────────────────────────
    fun loadXaiVoices() {
        val repo = repo ?: return
        viewModelScope.launch {
            try {
                val res = repo.fetchXaiVoices()
                _xaiConfigured.value = res.configured
                _xaiVoices.value = res.voices
            } catch (_: Exception) {
                _xaiConfigured.value = false   // unreachable == unconfigured (zone hides)
            }
        }
    }

    /** Queue ONE picked clip (xAI clones from a single ≤120s reference). */
    fun addXaiPickedFile(uri: Uri) {
        viewModelScope.launch {
            try {
                val part = withContext(Dispatchers.IO) { copyUriToCache(uri) }
                _xaiClonePart.value?.let { runCatching { it.file.delete() } }
                _xaiClonePart.value = part
                _xaiCloneError.value = null
            } catch (e: Exception) {
                _message.value = "Couldn't read file: ${e.message}"
            }
        }
    }

    fun clearXaiPart() {
        _xaiClonePart.value?.let { runCatching { it.file.delete() } }
        _xaiClonePart.value = null
    }

    fun submitXaiClone(name: String, description: String, consent: Boolean) {
        val repo = repo ?: return
        val part = _xaiClonePart.value
        if (name.isBlank() || part == null || !consent) {
            _xaiCloneError.value = "Name, one clip (max 120s), and consent are required."
            return
        }
        _xaiCloneState.value = CloneState.SUBMITTING
        _xaiCloneError.value = null
        viewModelScope.launch {
            try {
                repo.cloneXaiVoice(name.trim(), part.file, consent, description.trim())
                _xaiCloneState.value = CloneState.IDLE
                _message.value = "Grok voice \"${name.trim()}\" cloned."
                clearXaiPart()
                loadXaiVoices()
            } catch (e: VoiceLabException) {
                _xaiCloneState.value = CloneState.IDLE
                _xaiCloneError.value = when (e.status) {
                    422 -> "Consent is required to clone a voice."
                    400 -> "Clone rejected: ${e.message}"
                    else -> e.message
                }
            } catch (e: Exception) {
                _xaiCloneState.value = CloneState.IDLE
                _xaiCloneError.value = e.message ?: "Clone failed"
            }
        }
    }

    fun deleteXaiVoice(voiceId: String) {
        val repo = repo ?: return
        viewModelScope.launch {
            try {
                repo.deleteXaiVoice(voiceId)
                _message.value = "Grok voice deleted."
                loadXaiVoices()
            } catch (e: Exception) {
                _message.value = "Delete failed: ${e.message}"
            }
        }
    }

    override fun onCleared() {
        super.onCleared()
        recordTicker?.cancel()
        if (recorder.isCurrentlyRecording()) recorder.stopRecording()
        // Drop any un-submitted clips.
        _cloneParts.value.forEach { runCatching { it.file.delete() } }
        _xaiClonePart.value?.let { runCatching { it.file.delete() } }
    }
}
