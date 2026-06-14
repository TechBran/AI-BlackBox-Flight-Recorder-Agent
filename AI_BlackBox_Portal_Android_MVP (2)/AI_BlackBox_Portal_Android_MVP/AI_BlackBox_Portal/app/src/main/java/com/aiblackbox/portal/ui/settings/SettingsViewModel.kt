package com.aiblackbox.portal.ui.settings

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.BlackBoxStore
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class RegisteredApp(
    val appId: String = "",
    val name: String = "",
    val port: Int = 0,
    val directory: String = "",
    val operator: String = "",
    val createdAt: String = ""
)

class SettingsViewModel(application: Application) : AndroidViewModel(application) {
    val store = BlackBoxStore(application)
    internal var api: BlackBoxApi? = null

    private val _operators = MutableStateFlow(listOf("Brandon"))
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    // ── On-device (local) provider gating (Task 1.6) ──
    // True when the current operator has a disk-present, sha-verified on-device
    // model → the Provider dropdown offers the LOCAL provider. Default false
    // until loaded so we never flash LOCAL before we know it's installed.
    private val _localAvailable = MutableStateFlow(false)
    val localAvailable: StateFlow<Boolean> = _localAvailable.asStateFlow()
    private var providerPicker: com.aiblackbox.portal.ui.chat.ProviderPickerViewModel? = null
    private var currentOperator: String = "Brandon"

    init {
        viewModelScope.launch { store.operator.collect { currentOperator = it } }
    }

    private val _apps = MutableStateFlow<List<RegisteredApp>>(emptyList())
    val apps: StateFlow<List<RegisteredApp>> = _apps.asStateFlow()

    // Voice catalog — defaults to the full offline fallback so the picker is never
    // empty; loadVoiceCatalog() swaps in the live /tts/catalog when reachable.
    private val _voiceGroups = MutableStateFlow(com.aiblackbox.portal.data.repository.TTS_VOICE_GROUPS)
    val voiceGroups: StateFlow<List<com.aiblackbox.portal.data.repository.VoiceGroup>> = _voiceGroups.asStateFlow()

    fun loadVoiceCatalog() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                _voiceGroups.value = com.aiblackbox.portal.data.repository.TtsRepository(api).fetchCatalog()
            } catch (_: Exception) { /* keep the offline default already in _voiceGroups */ }
        }
    }

    // ── Voice preview (single ▶ button next to the TTS Voice dropdown) ──
    // On-demand: OpenAI is synchronous; Gemini flash/pro submit → poll → play.
    // _previewing gates the UI (loading state + overlap guard); _previewError
    // surfaces failures (no pre-existing error flow in this VM to reuse).
    private val _previewing = MutableStateFlow(false)
    val previewing: StateFlow<Boolean> = _previewing.asStateFlow()
    private val _previewError = MutableStateFlow<String?>(null)
    val previewError: StateFlow<String?> = _previewError.asStateFlow()
    private var previewPlayer: android.media.MediaPlayer? = null

    fun clearPreviewError() { _previewError.value = null }

    fun previewVoice(voiceId: String) {
        val api = api ?: return
        if (_previewing.value) return
        _previewing.value = true
        _previewError.value = null
        viewModelScope.launch {
            try {
                val repo = com.aiblackbox.portal.data.repository.TtsRepository(api)
                val text = "Hello! This is a preview of the selected voice."
                val cfg = com.aiblackbox.portal.data.repository.TtsRepository.parseVoice(voiceId)
                val url = when (cfg.provider) {
                    "openai" -> repo.generateTts(text, cfg.voice, cfg.model).audio_url
                    "elevenlabs" -> repo.generateElevenLabsTts(text, cfg.voice).audio_url
                    else -> {
                        val sub = repo.generateGeminiTts(text, cfg.voice, cfg.model)
                        repo.pollGeminiTaskForUrl(sub.task_id)
                    }
                }
                if (url.isNotBlank()) playPreview(url)
                else throw Exception("No audio url returned")
            } catch (e: Exception) {
                _previewError.value = "Preview failed: ${e.message}"
            } finally {
                _previewing.value = false
            }
        }
    }

    private fun playPreview(url: String) {
        previewPlayer?.apply { setOnCompletionListener(null); setOnErrorListener(null); release() }
        previewPlayer = null
        // Mirror GeminiProTtsScreen: relative urls need the server origin prefixed.
        val base = api?.getBaseUrl() ?: ""
        val src = if (url.startsWith("http")) url else "$base$url"
        previewPlayer = android.media.MediaPlayer().apply {
            setDataSource(src)
            setOnPreparedListener { start() }
            setOnCompletionListener { it.release(); previewPlayer = null }
            setOnErrorListener { mp, _, _ -> mp.release(); previewPlayer = null; true }
            prepareAsync()
        }
    }

    override fun onCleared() {
        super.onCleared()
        previewPlayer?.apply { setOnCompletionListener(null); setOnErrorListener(null); release() }
        previewPlayer = null
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        loadOperators()
        loadApps()

        // On-device (LOCAL) provider gating (Task 1.6): disk reads off-main.
        val picker = com.aiblackbox.portal.ui.chat.ProviderPickerViewModel.fromContext(
            context = getApplication(),
            api = api!!,
            operatorProvider = { currentOperator },
            ioDispatcher = kotlinx.coroutines.Dispatchers.IO,
        )
        providerPicker = picker
        viewModelScope.launch { picker.localAvailable.collect { _localAvailable.value = it } }
        refreshLocalAvailability()
    }

    /**
     * Recompute on-device (LOCAL) availability + fire the best-effort re-attest.
     * Call when the Provider dropdown opens so a just-installed/just-deleted
     * model is reflected and the BlackBox's binding record stays current.
     */
    fun refreshLocalAvailability() {
        providerPicker?.refresh()
    }

    fun setProvider(provider: String) {
        viewModelScope.launch { store.setProvider(provider) }
    }

    fun setModel(model: String, provider: String) {
        viewModelScope.launch {
            store.setModel(model)
            store.setString("model_$provider", model)
        }
    }

    fun setOperator(operator: String) {
        viewModelScope.launch { store.setOperator(operator) }
    }

    fun setStreamingEnabled(enabled: Boolean) {
        viewModelScope.launch { store.setStreamingEnabled(enabled) }
    }

    fun setOperatorVoice(operator: String, voiceValue: String) {
        viewModelScope.launch { store.setOperatorVoice(operator, voiceValue) }
    }

    fun addOperator(name: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val body = """{"name":"$name"}"""
                api.post("/operator/add", body)
                store.setOperator(name)
            } catch (_: Exception) {}
        }
    }

    fun setUseNative(context: Context, useNative: Boolean) {
        context.getSharedPreferences("bbx_prefs", Context.MODE_PRIVATE)
            .edit().putBoolean("use_native_ui", useNative).apply()
    }

    /** Trigger a conversation checkpoint (POST /checkpoint) */
    fun triggerCheckpoint() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/checkpoint", "{}")
            } catch (_: Exception) {}
        }
    }

    /** Disconnect Gmail for an operator */
    fun disconnectGmail(operator: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/gmail/disconnect/$operator", "{}")
            } catch (_: Exception) {}
        }
    }

    /** Cancel all stuck tasks (POST /tasks/cancel-all) */
    fun cancelAllTasks() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/tasks/cancel-all", "{}")
            } catch (_: Exception) {}
        }
    }

    /** Fetch pairing token from backend (POST /pair/start) — matches Portal flow */
    suspend fun fetchPairToken(): Pair<String, Long>? {
        return try {
            val response = api?.post("/pair/start", "{}") ?: return null
            val json = Json { ignoreUnknownKeys = true }
            val obj = json.parseToJsonElement(response).jsonObject
            val token = obj["token"]?.jsonPrimitive?.content ?: return null
            val exp = obj["exp"]?.jsonPrimitive?.content?.toLongOrNull() ?: 0L
            Pair(token, exp)
        } catch (_: Exception) {
            null
        }
    }

    /** Restart the BlackBox service (POST /restart) */
    fun restartService() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/restart", "{}")
            } catch (_: Exception) {}
        }
    }

    private fun loadApps() {
        viewModelScope.launch {
            try {
                val response = api?.get("/agent/apps") ?: return@launch
                val json = Json { ignoreUnknownKeys = true }
                val obj = json.parseToJsonElement(response).jsonObject
                val appsList = obj["apps"]?.jsonArray?.mapNotNull { elem ->
                    val a = elem.jsonObject
                    RegisteredApp(
                        appId = a["app_id"]?.jsonPrimitive?.content ?: "",
                        name = a["name"]?.jsonPrimitive?.content ?: "Unnamed",
                        port = a["port"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                        directory = a["directory"]?.jsonPrimitive?.content ?: "",
                        operator = a["operator"]?.jsonPrimitive?.content ?: "",
                        createdAt = a["created_at"]?.jsonPrimitive?.content ?: ""
                    )
                } ?: emptyList()
                _apps.value = appsList
            } catch (_: Exception) {}
        }
    }

    private fun loadOperators() {
        viewModelScope.launch {
            try {
                val response = api?.get("/operators") ?: return@launch
                val json = Json { ignoreUnknownKeys = true }
                val obj = json.parseToJsonElement(response).jsonObject
                val ops = obj["operators"]?.jsonArray?.mapNotNull { elem ->
                    elem.jsonObject["operator"]?.jsonPrimitive?.content
                } ?: emptyList()
                if (ops.isNotEmpty()) _operators.value = ops
            } catch (_: Exception) {
                // Keep default operator list on failure
            }
        }
    }
}
