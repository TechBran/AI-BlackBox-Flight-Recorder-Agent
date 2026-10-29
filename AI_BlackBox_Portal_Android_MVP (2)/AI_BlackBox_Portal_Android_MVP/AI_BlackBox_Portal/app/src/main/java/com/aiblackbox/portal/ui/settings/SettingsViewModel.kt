package com.aiblackbox.portal.ui.settings

import android.app.Application
import android.content.Context
import android.widget.Toast
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.repository.ModelCatalogRepository
import com.aiblackbox.portal.data.repository.ModelCatalogs
import com.aiblackbox.portal.data.repository.UpdateRepository
import com.aiblackbox.portal.ui.updates.RestartWait
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.OperatorResolver
import com.aiblackbox.portal.ui.chat.ChatViewModel
import com.aiblackbox.portal.ui.chat.ProviderPickerViewModel
import com.aiblackbox.portal.util.Constants
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** PUT body for /operator/persona/{operator}. Built with Json.encodeToString —
 *  NEVER string interpolation (persona text is multi-line/quoted and would break JSON). */
@Serializable
data class PersonaPutBody(val persona: String)

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

    // The box's roster (GET /operators); empty until it loads — never a seed name.
    private val _operators = MutableStateFlow<List<String>>(emptyList())
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    // ── On-device (local) provider gating (Task 1.6) ──
    // True when the current operator has a disk-present, sha-verified on-device
    // model → the Provider dropdown offers the LOCAL provider. Default false
    // until loaded so we never flash LOCAL before we know it's installed.
    private val _localAvailable = MutableStateFlow(false)
    val localAvailable: StateFlow<Boolean> = _localAvailable.asStateFlow()
    private var providerPicker: ProviderPickerViewModel? = null

    // The store's operator, live: read at each use (suspend paths read store.operator
    // directly), never a copy kept from when the sheet opened. "" = unresolved.
    private val operatorNow: StateFlow<String> =
        store.operator.stateIn(viewModelScope, SharingStarted.Eagerly, "")

    private val _apps = MutableStateFlow<List<RegisteredApp>>(emptyList())
    val apps: StateFlow<List<RegisteredApp>> = _apps.asStateFlow()

    // Voice catalog — defaults to the full offline fallback so the picker is never
    // empty; loadVoiceCatalog() swaps in the live /tts/catalog when reachable.
    private val _voiceGroups = MutableStateFlow(com.aiblackbox.portal.data.repository.TTS_VOICE_GROUPS)
    val voiceGroups: StateFlow<List<com.aiblackbox.portal.data.repository.VoiceGroup>> = _voiceGroups.asStateFlow()

    // The box default voice from /tts/catalog `default` (null until loaded, offline,
    // or from an older server): what an operator with no saved voice hears, so the
    // picker shows it instead of a pinned id (VoicePicker.effectiveVoice).
    private val _defaultVoiceId = MutableStateFlow<String?>(null)
    val defaultVoiceId: StateFlow<String?> = _defaultVoiceId.asStateFlow()

    // The Model dropdown's rows for the active provider, from the catalog the chat
    // composer, cron and SMS share (ModelCatalogs, rank 76); see [loadModels].
    private val _modelOptions = MutableStateFlow<List<Pair<String, String>>>(emptyList())
    val modelOptions: StateFlow<List<Pair<String, String>>> = _modelOptions.asStateFlow()
    private var modelOptionsProvider: String? = null

    fun loadVoiceCatalog() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val catalog = com.aiblackbox.portal.data.repository.TtsRepository(api).fetchCatalogWithDefault()
                _voiceGroups.value = catalog.groups
                _defaultVoiceId.value = catalog.defaultVoiceId
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
    // D10: flips true when a slow on-box preview is likely waiting on a GPU
    // group swap ("Loading models…"), so the UI can distinguish "generating"
    // from "loading the model in".
    private val _previewSlow = MutableStateFlow(false)
    val previewSlow: StateFlow<Boolean> = _previewSlow.asStateFlow()
    private var previewPlayer: android.media.MediaPlayer? = null

    fun clearPreviewError() { _previewError.value = null }

    fun previewVoice(voiceId: String) {
        val api = api ?: return
        if (_previewing.value) return
        _previewing.value = true
        _previewError.value = null
        _previewSlow.value = false
        val cfgForSlow = com.aiblackbox.portal.data.repository.TtsRepository.parseVoice(voiceId)
        val slowJob = if (cfgForSlow.provider == com.aiblackbox.portal.data.repository.TtsRepository.ON_BOX_PROVIDER) viewModelScope.launch {
            com.aiblackbox.portal.data.repository.TtsRepository.awaitSlowFirstByte(
                stillInFlight = { _previewing.value },
                onSlow = { _previewSlow.value = true },
            )
        } else null
        viewModelScope.launch {
            try {
                val repo = com.aiblackbox.portal.data.repository.TtsRepository(api)
                val text = "Hello! This is a preview of the selected voice."
                val cfg = com.aiblackbox.portal.data.repository.TtsRepository.parseVoice(voiceId, _voiceGroups.value)
                val operator = store.operator.first()
                val url = when {
                    cfg.provider == "elevenlabs" ->
                        repo.generateElevenLabsTts(text, cfg.voice, operator = operator).audio_url
                    cfg.provider == "gemini-pro" || cfg.provider == "gemini-flash" -> {
                        val sub = repo.generateGeminiTts(text, cfg.voice, cfg.model, operator = operator)
                        repo.pollGeminiTaskForUrl(sub.task_id)
                    }
                    // openai / local / qwen — synchronous /tts/batch, provider passed
                    // through (M0 generic-provider fix). Without this a qwen/local
                    // preview fell into the Gemini poll and failed.
                    else ->
                        repo.generateTts(text, cfg.voice, cfg.model, provider = cfg.provider, operator = operator).audio_url
                }
                if (url.isNotBlank()) playPreview(url)
                else throw Exception("No audio url returned")
            } catch (e: Exception) {
                _previewError.value = "Preview failed: ${e.message}"
            } finally {
                slowJob?.cancel()
                _previewSlow.value = false
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
        // Cancel the picker's own CoroutineScope (separate from viewModelScope)
        // so it doesn't leak.
        providerPicker?.dispose()
        previewPlayer?.apply { setOnCompletionListener(null); setOnErrorListener(null); release() }
        previewPlayer = null
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        // First persona load now that api is ready — the reactive collector's
        // startup emissions were no-ops while api was null. The operator is read
        // from the store now, not from a copy that may not have arrived yet.
        viewModelScope.launch { loadOperatorPersona(store.operator.first()) }
        loadOperators()
        loadApps()

        // On-device (LOCAL) provider gating (Task 1.6): disk reads off-main.
        val picker = ProviderPickerViewModel.fromContext(
            context = getApplication(),
            api = api!!,
            operatorProvider = { operatorNow.value },
            ioDispatcher = Dispatchers.IO,
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

    /**
     * Hydrate [modelOptions] for [provider]: the shared catalog's known copy at once, then
     * the box's (GET /models/{provider}); Constants' entry for a provider the box has no
     * catalog for, or before any catalog is known. A fresh live catalog also replaces the
     * stored picks it no longer lists (BlackBoxStore.reconcileModelIds). The sheet used to
     * read Constants.MODEL_CONFIG alone, a hand-kept list that went stale.
     */
    fun loadModels(provider: String) {
        modelOptionsProvider = provider
        val fallback = Constants.MODEL_CONFIG[provider].orEmpty()
        val api = api
        val apiProvider = ModelCatalogRepository.apiProviderFor(provider)
        if (api == null || apiProvider == null) {
            _modelOptions.value = fallback
            return
        }
        val catalogs = ModelCatalogs.forApi(api)
        _modelOptions.value = catalogs.cached(apiProvider)?.options()?.ifEmpty { null } ?: fallback
        viewModelScope.launch {
            val catalog = catalogs.refresh(apiProvider) ?: return@launch
            if (modelOptionsProvider != provider) return@launch
            catalog.options().takeIf { it.isNotEmpty() }?.let { _modelOptions.value = it }
            if (catalog.isAuthoritative) store.reconcileModelIds(provider, catalog.ids)
        }
    }

    // W22: the SAME switch the composer pill makes (BlackBoxStore.setProviderAndModel) —
    // Auto at once, so the previous provider's id is never sent to this one, then this
    // provider's remembered pick only if its live list still offers it.
    fun setProvider(provider: String) {
        viewModelScope.launch {
            store.setProviderAndModel(provider) { ChatViewModel.fetchLiveModelIds(api, it) }
        }
    }

    // W22: the shared pick path — global key (active provider) + model_<provider> together;
    // the on-device Model Manager's "Use" (provider "local") writes model_local ONLY.
    fun setModel(model: String, provider: String) {
        viewModelScope.launch { store.selectModel(provider, model) }
    }

    fun setOperator(operator: String) {
        viewModelScope.launch { store.setOperator(operator) }
    }

    fun setStreamingEnabled(enabled: Boolean) {
        viewModelScope.launch { store.setStreamingEnabled(enabled) }
    }

    fun setEmberMode(value: String) { viewModelScope.launch { store.setEmberMode(value) } }

    /**
     * M1 — stop/resume attaching location to prompts WITHOUT touching the OS permission.
     * Off is honoured immediately (ChatViewModel mirrors the flow), and no location is
     * even read on the next send.
     */
    fun setLocationAttachEnabled(value: Boolean) {
        viewModelScope.launch { store.setLocationAttachEnabled(value) }
    }

    fun setParticleMode(value: String) { viewModelScope.launch { store.setParticleMode(value) } }

    fun setParticleIntensity(value: Float) { viewModelScope.launch { store.setParticleIntensity(value) } }

    fun setParticleQuality(value: String) { viewModelScope.launch { store.setParticleQuality(value) } }

    // Voice WAVEFORM style — the single GLOBAL look for the voice layer. Persists
    // immediately; NativeMainActivity's LocalWaveformStyle collector then repaints
    // all three call sites (composer, player bar, voice screen) live. Value is
    // normalised in the store (WaveformStyles.parse), so an unknown id can never land.
    fun setWaveformStyle(value: String) { viewModelScope.launch { store.setWaveformStyle(value) } }

    fun setOperatorVoice(operator: String, voiceValue: String) {
        viewModelScope.launch { store.setOperatorVoice(operator, voiceValue) }
    }

    // ── Per-operator System Prompt (persona) ──
    // _operatorPersona = the effective persona (custom override OR lean default,
    // resolved server-side). _personaIsCustom drives the "Custom" vs "built-in
    // default" caption. Loaded per-operator; reloaded after a save/reset so the
    // UI reflects the just-persisted state without a second round-trip guess.
    private val _operatorPersona = MutableStateFlow("")
    val operatorPersona: StateFlow<String> get() = _operatorPersona
    private val _personaIsCustom = MutableStateFlow(false)
    val personaIsCustom: StateFlow<Boolean> get() = _personaIsCustom

    init {
        // Rehydrate the persona reactively on EVERY operator change — including a
        // switch made from the main-screen operator pill while this sheet is closed
        // (the sheet's own effect can't fire when it isn't composed). This mirrors
        // how voice already rehydrates via its per-operator Flow.
        //   - distinctUntilChanged: store.operator re-emits on every preference
        //     write, not just on operator changes, so we'd otherwise reload on each
        //     voice/model save.
        //   - clear first: never show the previous operator's persona for the new
        //     operator while the async reload is in flight.
        //   - no-op until api is ready; initialize() does the first load.
        viewModelScope.launch {
            store.operator.distinctUntilChanged().collect { op ->
                _operatorPersona.value = ""
                _personaIsCustom.value = false
                loadOperatorPersona(op)
            }
        }
    }

    fun loadOperatorPersona(operator: String) {
        val a = api ?: return
        if (operator.isBlank()) return  // unresolved: nobody's persona to show
        viewModelScope.launch {
            try {
                val resp = a.get("/operator/persona/" + android.net.Uri.encode(operator))
                val obj = Json.parseToJsonElement(resp).jsonObject
                _operatorPersona.value = obj["persona"]?.jsonPrimitive?.content ?: ""
                _personaIsCustom.value = obj["is_custom"]?.jsonPrimitive?.booleanOrNull ?: false
            } catch (e: Exception) {
                android.util.Log.w("Persona", "load failed", e)
            }
        }
    }

    fun setOperatorPersona(operator: String, text: String) {
        val a = api ?: return
        if (operator.isBlank()) return
        viewModelScope.launch {
            try {
                // Build the JSON body via the serializer, NOT interpolation: persona
                // is multi-line/quoted and would corrupt a hand-built string.
                val body = Json.encodeToString(PersonaPutBody(text))
                a.put("/operator/persona/" + android.net.Uri.encode(operator), body)
                loadOperatorPersona(operator)
            } catch (e: Exception) {
                android.util.Log.w("Persona", "save failed", e)
            }
        }
    }

    fun resetOperatorPersona(operator: String) {
        val a = api ?: return
        if (operator.isBlank()) return
        viewModelScope.launch {
            try {
                a.delete("/operator/persona/" + android.net.Uri.encode(operator))
                loadOperatorPersona(operator)
            } catch (e: Exception) {
                android.util.Log.w("Persona", "reset failed", e)
            }
        }
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

    /**
     * Cancel all stuck tasks (POST /tasks/cancel-all). The sheet confirms first;
     * the result ("N tasks cancelled" or the error) is shown, not dropped
     * (A11-settings-generation-13).
     */
    fun cancelAllTasks() {
        val api = api ?: return
        viewModelScope.launch {
            showToast(try {
                SystemActions.cancelledMessage(api.post("/tasks/cancel-all", "{}"))
            } catch (e: Exception) {
                "Could not cancel tasks: ${e.message}"
            })
        }
    }

    /** A Toast from the application context: a restart wait can outlive the sheet. */
    private fun showToast(message: String) {
        Toast.makeText(getApplication<Application>(), message, Toast.LENGTH_LONG).show()
    }

    /**
     * Fetch a pairing token (POST /pair/start) minted for [operator], the one opening the
     * dialog — matches the Portal flow ([PairingPayload.startBody]).
     */
    suspend fun fetchPairToken(operator: String): Pair<String, Long>? {
        return try {
            val response = api?.post("/pair/start", PairingPayload.startBody(operator)) ?: return null
            val json = Json { ignoreUnknownKeys = true }
            val obj = json.parseToJsonElement(response).jsonObject
            val token = obj["token"]?.jsonPrimitive?.content ?: return null
            val exp = obj["exp"]?.jsonPrimitive?.content?.toLongOrNull() ?: 0L
            Pair(token, exp)
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Restart the BlackBox service (POST /restart). The sheet confirms first.
     * A refusal in the 200 body (sudo denied) is reported; otherwise wait for
     * /health's boot_id to change, so "back online" means a new process
     * (A11-settings-generation-13, A13-ui-ops-01).
     */
    fun restartService() {
        val api = api ?: return
        viewModelScope.launch {
            val probe: suspend () -> String? = {
                try {
                    UpdateRepository.parseBootId(restartJson, api.get("/health"))
                } catch (_: Exception) {
                    null
                }
            }
            val priorBootId = probe()
            val refusal = try {
                SystemActions.restartRefusal(api.post("/restart", "{}"))
            } catch (_: Exception) {
                null  // the restart can cut this very request off
            }
            if (refusal != null) {
                showToast(refusal)
                return@launch
            }
            showToast("Restart requested. BlackBox will be back in about 60–90 seconds.")
            showToast(SystemActions.restartOutcomeMessage(RestartWait.await(priorBootId, probe)))
        }
    }

    private val restartJson = Json { ignoreUnknownKeys = true; isLenient = true }

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
                // GET /operators serves a STRING array; the shared parser reads it (the old
                // per-object read threw on every entry and left a seed name in the list).
                val response = api?.get("/operators") ?: return@launch
                OperatorResolver.parseRoster(response)?.let { _operators.value = it.operators }
            } catch (_: Exception) {
                // Keep the last known list on failure
            }
        }
    }
}
