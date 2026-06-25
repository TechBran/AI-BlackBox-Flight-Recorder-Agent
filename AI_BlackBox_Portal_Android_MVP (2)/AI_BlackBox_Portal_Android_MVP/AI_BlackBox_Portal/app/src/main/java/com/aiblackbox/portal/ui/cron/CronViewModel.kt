package com.aiblackbox.portal.ui.cron

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.CronHistoryEntry
import com.aiblackbox.portal.data.model.CronHistoryResponse
import com.aiblackbox.portal.data.model.CronJob
import com.aiblackbox.portal.data.model.CronJobCreateRequest
import com.aiblackbox.portal.data.model.CronJobsResponse
import com.aiblackbox.portal.data.repository.ChatRepository
import com.aiblackbox.portal.util.Constants
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

private const val TAG = "CronViewModel"

class CronViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null
    // Reuses the chat composer's repository — getModels(key) hits the SAME
    // GET /models/{key} endpoint the chat picker uses (no HTTP reimplemented).
    private var repository: ChatRepository? = null
    private val json = Json { ignoreUnknownKeys = true; isLenient = true; encodeDefaults = true }

    // -- Raw state --
    private val _allJobs = MutableStateFlow<List<CronJob>>(emptyList())
    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    // -- Search & filter --
    private val _searchQuery = MutableStateFlow("")
    val searchQuery: StateFlow<String> = _searchQuery.asStateFlow()

    private val _statusFilter = MutableStateFlow("all")
    val statusFilter: StateFlow<String> = _statusFilter.asStateFlow()

    // -- Derived filtered list --
    private val _filteredJobs = MutableStateFlow<List<CronJob>>(emptyList())
    val filteredJobs: StateFlow<List<CronJob>> = _filteredJobs.asStateFlow()

    // -- History --
    private val _historyEntries = MutableStateFlow<List<CronHistoryEntry>>(emptyList())
    val historyEntries: StateFlow<List<CronHistoryEntry>> = _historyEntries.asStateFlow()

    private val _historyLoading = MutableStateFlow(false)
    val historyLoading: StateFlow<Boolean> = _historyLoading.asStateFlow()

    // -- Edit/Create dialog state --
    private val _editingJob = MutableStateFlow<CronJob?>(null)
    val editingJob: StateFlow<CronJob?> = _editingJob.asStateFlow()

    private val _showEditDialog = MutableStateFlow(false)
    val showEditDialog: StateFlow<Boolean> = _showEditDialog.asStateFlow()

    private val _showHistoryDialog = MutableStateFlow(false)
    val showHistoryDialog: StateFlow<Boolean> = _showHistoryDialog.asStateFlow()

    private val _showDeleteConfirm = MutableStateFlow<String?>(null)
    val showDeleteConfirm: StateFlow<String?> = _showDeleteConfirm.asStateFlow()

    private val _isSaving = MutableStateFlow(false)
    val isSaving: StateFlow<Boolean> = _isSaving.asStateFlow()

    private val _actionMessage = MutableStateFlow<String?>(null)
    val actionMessage: StateFlow<String?> = _actionMessage.asStateFlow()

    // -- Live model selector (M4.4) --
    // Models for the currently-selected provider, as (id, displayName) pairs with
    // the Auto option ("" -> "Auto - …") first. Hydrated from /models/{key} via the
    // shared chat repository; falls back to Constants.MODEL_CONFIG offline.
    private val _modelsForProvider = MutableStateFlow<List<Pair<String, String>>>(emptyList())
    val modelsForProvider: StateFlow<List<Pair<String, String>>> = _modelsForProvider.asStateFlow()

    // 5-min in-memory cache keyed by canonical provider key (mirrors chat).
    private val modelsCache = mutableMapOf<String, Pair<Long, List<Pair<String, String>>>>()
    private val modelsCacheTtlMs = 5 * 60 * 1_000L

    // -- Polling --
    private var pollJob: Job? = null

    init {
        // Combine raw jobs + search + filter into filtered list
        viewModelScope.launch {
            combine(_allJobs, _searchQuery, _statusFilter) { jobs, query, filter ->
                var result = jobs
                if (filter != "all") {
                    result = result.filter { it.status == filter }
                }
                if (query.isNotBlank()) {
                    val q = query.lowercase()
                    result = result.filter {
                        it.name.lowercase().contains(q) ||
                                it.prompt.lowercase().contains(q)
                    }
                }
                result
            }.collect { _filteredJobs.value = it }
        }
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        val newApi = BlackBoxApi(origin)
        api = newApi
        repository = ChatRepository(newApi)
        loadJobs()
        startPolling()
    }

    // -------------------------------------------------------------------------
    // Search & Filter
    // -------------------------------------------------------------------------

    fun setSearchQuery(query: String) {
        _searchQuery.value = query
    }

    fun setStatusFilter(filter: String) {
        _statusFilter.value = filter
        loadJobs()
    }

    // -------------------------------------------------------------------------
    // CRUD
    // -------------------------------------------------------------------------

    fun loadJobs() {
        val api = api ?: return
        _isLoading.value = true
        viewModelScope.launch {
            try {
                val status = _statusFilter.value
                val path = if (status != "all") "/api/cron/jobs?status=$status" else "/api/cron/jobs"
                val response = api.get(path)
                val parsed = json.decodeFromString(CronJobsResponse.serializer(), response)
                _allJobs.value = parsed.jobs
                _error.value = null
            } catch (e: Exception) {
                _error.value = "Failed to load jobs: ${e.message}"
                _allJobs.value = emptyList()
            } finally {
                _isLoading.value = false
            }
        }
    }

    fun runJob(jobId: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                _actionMessage.value = "Running job..."
                api.post("/api/cron/jobs/$jobId/run", "{}")
                _actionMessage.value = "Job executed"
                loadJobs()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to run: ${e.message}"
            }
        }
    }

    fun pauseJob(jobId: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/api/cron/jobs/$jobId/pause", "{}")
                _actionMessage.value = "Job paused"
                loadJobs()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to pause: ${e.message}"
            }
        }
    }

    fun resumeJob(jobId: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/api/cron/jobs/$jobId/resume", "{}")
                _actionMessage.value = "Job resumed"
                loadJobs()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to resume: ${e.message}"
            }
        }
    }

    fun requestDelete(jobId: String) {
        _showDeleteConfirm.value = jobId
    }

    fun cancelDelete() {
        _showDeleteConfirm.value = null
    }

    fun confirmDelete() {
        val jobId = _showDeleteConfirm.value ?: return
        _showDeleteConfirm.value = null
        deleteJob(jobId)
    }

    private fun deleteJob(jobId: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.delete("/api/cron/jobs/$jobId")
                _actionMessage.value = "Job deleted"
                loadJobs()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to delete: ${e.message}"
            }
        }
    }

    // -------------------------------------------------------------------------
    // Create / Edit Dialog
    // -------------------------------------------------------------------------

    fun openCreateDialog() {
        _editingJob.value = null
        _showEditDialog.value = true
    }

    fun openEditDialog(jobId: String) {
        _editingJob.value = _allJobs.value.find { it.id == jobId }
        _showEditDialog.value = true
    }

    fun dismissEditDialog() {
        _showEditDialog.value = false
        _editingJob.value = null
    }

    fun saveJob(
        name: String,
        prompt: String,
        schedule: String,
        frequencyHint: String,
        provider: String,
        model: String,
        delivery: String,
        deliveryTarget: String,
        operator: String,
        oneShot: Boolean
    ) {
        val api = api ?: return
        if (_isSaving.value) return
        _isSaving.value = true

        viewModelScope.launch {
            try {
                val existingId = _editingJob.value?.id
                // Normalize to the canonical catalog key the backend + chat + Portal
                // store; `model` is the specific id ("" = Auto / provider default).
                // Built via the @Serializable body class (never string interpolation).
                val requestBody = json.encodeToString(
                    CronJobCreateRequest(
                        name = name,
                        prompt = prompt,
                        schedule = schedule,
                        frequencyHint = frequencyHint.ifBlank { null },
                        provider = canonicalProviderKey(provider),
                        model = model,
                        delivery = delivery,
                        deliveryTarget = deliveryTarget.ifBlank { null },
                        operator = operator,
                        oneShot = oneShot
                    )
                )

                if (existingId != null) {
                    api.put("/api/cron/jobs/$existingId", requestBody)
                    _actionMessage.value = "Job updated"
                } else {
                    api.post("/api/cron/jobs", requestBody)
                    _actionMessage.value = "Job created"
                }

                _showEditDialog.value = false
                _editingJob.value = null
                loadJobs()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to save: ${e.message}"
            } finally {
                _isSaving.value = false
            }
        }
    }

    // -------------------------------------------------------------------------
    // Live model selector (M4.4) — mirrors ChatViewModel.fetchLiveModels
    // -------------------------------------------------------------------------

    /** Canonical catalog provider keys (order = picker order), matching the
     *  backend M4.1 column + Portal M4.3 + chat composer. NOT the words
     *  gemini/claude/grok. */
    val cronProviders: List<String> =
        listOf("google", "openai", "anthropic", "xai", "computer-use")

    val defaultCronProvider: String = "google"

    // Legacy cron jobs (pre-M4.1) stored a coarse WORD in `model` with no
    // `provider`. Map those words to a canonical key so an edited legacy job can
    // still preselect a sensible provider. Mirrors Portal LEGACY_MODEL_TO_PROVIDER.
    private val legacyModelToProvider = mapOf(
        "gemini" to "google",
        "google" to "google",
        "openai" to "openai",
        "gpt" to "openai",
        "claude" to "anthropic",
        "anthropic" to "anthropic",
        "grok" to "xai",
        "xai" to "xai",
        "computer-use" to "computer-use"
    )

    // Constants.MODEL_CONFIG is keyed by Android's WORD provider keys, so the
    // offline fallback needs the canonical key → MODEL_CONFIG word bridge.
    private val canonicalToConfigKey = mapOf(
        "google" to "gemini",
        "openai" to "openai",
        "anthropic" to "anthropic",
        "xai" to "xai",
        "computer-use" to "computer-use"
    )

    /** Normalize any provider string (canonical key OR a legacy word) to the
     *  canonical catalog key. Unknown values default to [defaultCronProvider]. */
    fun canonicalProviderKey(provider: String?): String {
        val p = provider?.trim()?.lowercase().orEmpty()
        if (p.isEmpty()) return defaultCronProvider
        if (p in cronProviders) return p
        return legacyModelToProvider[p] ?: defaultCronProvider
    }

    /** Derive the canonical provider for a job lacking an explicit `provider`
     *  (legacy rows): try the coarse word map, else scan the offline catalogs
     *  for the specific id, else default. Mirrors Portal deriveProviderFromModel. */
    fun deriveProviderForJob(job: CronJob): String {
        job.provider?.takeIf { it.isNotBlank() }?.let { return canonicalProviderKey(it) }
        val model = job.model.trim()
        if (model.isEmpty()) return defaultCronProvider
        legacyModelToProvider[model.lowercase()]?.let { return it }
        for (key in cronProviders) {
            val cfgKey = canonicalToConfigKey[key] ?: continue
            if (Constants.MODEL_CONFIG[cfgKey]?.any { it.first == model } == true) return key
        }
        return defaultCronProvider
    }

    /** Resolve a job's stored `model` to a SPECIFIC model id for the picker. A
     *  legacy coarse provider word (gemini/claude/grok/openai/anthropic/xai/…)
     *  carries no specific id — it means "Auto for that provider" — so it maps to
     *  "" (Auto). A real model id is returned verbatim. */
    fun specificModelId(job: CronJob): String {
        val model = job.model.trim()
        if (model.isEmpty()) return ""
        if (model.lowercase() in legacyModelToProvider) return ""
        return model
    }

    /** Friendly display name for a (provider, modelId) pair from the live/offline
     *  catalog. "" = Auto. Never crashes — falls back to the raw id. */
    fun friendlyModelName(provider: String, modelId: String): String {
        val key = canonicalProviderKey(provider)
        if (key == "computer-use" && modelId.isBlank()) return "Computer Use"
        val list = _modelsForProvider.value.takeIf { it.isNotEmpty() && providerForCurrentList == key }
            ?: offlineModels(key)
        list.firstOrNull { it.first == modelId }?.let { return it.second }
        return if (modelId.isBlank()) "Auto" else modelId
    }

    // Which provider the current _modelsForProvider list belongs to (so a stale
    // list isn't reused for friendlyModelName of a different provider).
    private var providerForCurrentList: String = defaultCronProvider

    private fun offlineModels(canonicalKey: String): List<Pair<String, String>> {
        val cfgKey = canonicalToConfigKey[canonicalKey] ?: return listOf("" to "Auto - Latest")
        val cfg = Constants.MODEL_CONFIG[cfgKey] ?: return listOf("" to "Auto - Latest")
        // MODEL_CONFIG already prepends an Auto ("") entry for these providers.
        return if (cfg.any { it.first == "" }) cfg else listOf("" to "Auto - Latest") + cfg
    }

    /** Fetch the model list for [provider] (canonical key OR a legacy word) and
     *  publish it to [modelsForProvider]. Reuses ChatRepository.getModels (the
     *  same GET /models/{key} the chat composer uses); on failure falls back to
     *  the offline Constants catalog so the dropdown is never empty. */
    fun selectProvider(provider: String) {
        val key = canonicalProviderKey(provider)
        providerForCurrentList = key

        // Cache hit — instant, no network.
        val cached = modelsCache[key]
        val now = System.currentTimeMillis()
        if (cached != null && now - cached.first < modelsCacheTtlMs) {
            _modelsForProvider.value = cached.second
            return
        }

        // Seed with the offline catalog immediately so the dropdown is populated
        // while the network fetch (if any) is in flight.
        _modelsForProvider.value = offlineModels(key)

        val repo = repository ?: return
        viewModelScope.launch {
            try {
                val response = repo.getModels(key)
                val obj = json.parseToJsonElement(response).jsonObject
                val modelsArr = obj["models"]?.jsonArray ?: return@launch
                val models = modelsArr.mapNotNull { el ->
                    try {
                        val m = el.jsonObject
                        val id = m["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                        val name = m["name"]?.jsonPrimitive?.content ?: id
                        id to name
                    } catch (_: Exception) { null }
                }
                if (models.isNotEmpty()) {
                    // Auto ("") first — resolves server-side to the provider default.
                    // For computer-use, label Auto with the default model name when
                    // known (mirrors chat + Portal); otherwise a plain "Auto - Latest".
                    val autoLabel = if (key == "computer-use") {
                        val defaultId = obj["default_id"]?.jsonPrimitive?.content
                        val defaultName = models.firstOrNull { it.first == defaultId }?.second
                        if (defaultName != null) "Auto - $defaultName" else "Auto - Latest"
                    } else {
                        "Auto - Latest"
                    }
                    val withAuto = listOf("" to autoLabel) + models
                    // Only publish if this is still the selected provider (guards a
                    // fast provider switch from clobbering the newer list).
                    if (providerForCurrentList == key) {
                        _modelsForProvider.value = withAuto
                    }
                    modelsCache[key] = System.currentTimeMillis() to withAuto
                    Log.d(TAG, "Fetched ${models.size} models for $key")
                }
            } catch (e: Exception) {
                Log.d(TAG, "Model fetch failed for $key, using offline catalog: ${e.message}")
                // Offline seed already published above.
            }
        }
    }

    // -------------------------------------------------------------------------
    // History
    // -------------------------------------------------------------------------

    fun openHistory(jobId: String) {
        _showHistoryDialog.value = true
        loadHistory(jobId)
    }

    fun dismissHistory() {
        _showHistoryDialog.value = false
        _historyEntries.value = emptyList()
    }

    private fun loadHistory(jobId: String) {
        val api = api ?: return
        _historyLoading.value = true
        viewModelScope.launch {
            try {
                val response = api.get("/api/cron/jobs/$jobId/history")
                val parsed = json.decodeFromString(CronHistoryResponse.serializer(), response)
                _historyEntries.value = parsed.history
            } catch (_: Exception) {
                _historyEntries.value = emptyList()
            } finally {
                _historyLoading.value = false
            }
        }
    }

    // -------------------------------------------------------------------------
    // Polling
    // -------------------------------------------------------------------------

    private fun startPolling() {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            while (isActive) {
                delay(5000)
                try {
                    val status = _statusFilter.value
                    val path = if (status != "all") "/api/cron/jobs?status=$status" else "/api/cron/jobs"
                    val response = api?.get(path) ?: continue
                    val parsed = json.decodeFromString(CronJobsResponse.serializer(), response)
                    _allJobs.value = parsed.jobs
                } catch (_: Exception) {
                    // Silent poll failure
                }
            }
        }
    }

    fun clearActionMessage() {
        _actionMessage.value = null
    }

    override fun onCleared() {
        super.onCleared()
        pollJob?.cancel()
    }
}
