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
import com.aiblackbox.portal.data.model.CronContact
import com.aiblackbox.portal.data.model.CronContactsResponse
import com.aiblackbox.portal.data.model.CronJobsResponse
import com.aiblackbox.portal.data.model.CronPreviewRequest
import com.aiblackbox.portal.data.model.CronPreviewResponse
import com.aiblackbox.portal.data.repository.ModelCatalogRepository
import com.aiblackbox.portal.data.repository.ModelCatalogs
import com.aiblackbox.portal.data.store.ModelSelection
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
    // The model catalogs the chat composer, Settings and SMS share (ModelCatalogs):
    // the SAME GET /models/{key} copy, so cron never keeps its own cache.
    private var catalogs: ModelCatalogRepository? = null
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
    // the Auto option ("" -> "Auto - …") first. Hydrated from /models/{key} through the
    // shared catalog (its TTL and last-good copy); Auto alone before any catalog is known.
    private val _modelsForProvider = MutableStateFlow<List<Pair<String, String>>>(emptyList())
    val modelsForProvider: StateFlow<List<Pair<String, String>>> = _modelsForProvider.asStateFlow()

    // -- Live operator list (Fix 4) --
    // Sourced from GET /operators -> {operators:[...], default:"..."} so the
    // create/edit dialog's Operator field is a DROPDOWN of real operators, never
    // a free-text box hardcoded to "Brandon". Falls back to whatever it last had.
    private val _operators = MutableStateFlow<List<String>>(emptyList())
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    private val _defaultOperator = MutableStateFlow("")
    /** The box default operator (GET /operators .default). "" until loaded. Used
     *  as the dialog's operator default when creating a NEW job. */
    val defaultOperator: StateFlow<String> = _defaultOperator.asStateFlow()

    // -- Per-operator contacts for the SMS/voice delivery-target picker (Fix 5) --
    // GET /api/cron/contacts?operator=<op>. Re-fetched whenever the selected
    // operator changes; the dialog shows these alongside a manual E.164 field.
    private val _previewContacts = MutableStateFlow<List<CronContact>>(emptyList())
    val previewContacts: StateFlow<List<CronContact>> = _previewContacts.asStateFlow()

    // -- Next-run preview (M5c — POST /api/cron/preview while editing) --
    // Mirrors Portal M5b: a debounced preview of the next ~3 fire times for the
    // schedule the editor currently describes. The screen renders this StateFlow
    // near the schedule field; an invalid mid-edit cron shows a subtle hint
    // rather than crashing/toasting.
    private val _schedulePreview = MutableStateFlow(SchedulePreview())
    val schedulePreview: StateFlow<SchedulePreview> = _schedulePreview.asStateFlow()

    // -- The box's scheduling zone (A12-voice-cron-14) --
    // Every schedule is read against the box's clock, which the phone may not share,
    // so the Simple editor labels its time fields "box time (<zone>)". From the
    // tz_abbrev the job list and the preview carry; "" until a box that sends it answers.
    private val _boxZone = MutableStateFlow("")
    val boxZone: StateFlow<String> = _boxZone.asStateFlow()

    private var previewJob: Job? = null
    // Monotonic token so a stale in-flight response can't clobber a newer one
    // (debounce stops most overlap, but a slow request could still land late).
    private var previewSeq = 0L
    private var lastPreviewedSchedule: String? = null

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
        catalogs = ModelCatalogs.forApi(newApi)
        loadJobs()
        loadOperators()
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
                parsed.tzAbbrev?.takeIf { it.isNotBlank() }?.let { _boxZone.value = it }
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
                // The route only starts the run (202); its outcome lands in History.
                api.post("/api/cron/jobs/$jobId/run", "{}")
                _actionMessage.value = "Job started; its result will appear in History"
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
        clearSchedulePreview()
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
                clearSchedulePreview()
                loadJobs()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to save: ${e.message}"
            } finally {
                _isSaving.value = false
            }
        }
    }

    // -------------------------------------------------------------------------
    // Next-run preview (M5c) — mirrors Portal refreshNextRunPreview
    // -------------------------------------------------------------------------

    /** UI state for the next-run preview shown near the schedule field. */
    data class SchedulePreview(
        val runs: List<String> = emptyList(),  // labels in the phone's zone, zone named
        val invalid: Boolean = false,           // mid-edit invalid cron (400)
        val visible: Boolean = false            // hidden until there's something to show
    )

    /**
     * Debounce (~300ms) a preview of the next [count] fire times for [schedule]
     * via POST /api/cron/preview, then publish the result to [schedulePreview].
     * Uses the SAME cron string the save path builds (the screen passes it in).
     *
     * A blank schedule hides the preview. A 400 (the user is mid-typing an
     * invalid cron) sets `invalid` for a subtle hint — never a toast/crash. Any
     * other failure (route not live, network drop) just hides the preview. A
     * monotonic seq + a "did the schedule actually change" guard keep a stale
     * late response from clobbering a newer one.
     */
    fun previewSchedule(schedule: String, count: Int = 3) {
        val trimmed = schedule.trim()
        if (trimmed.isBlank()) {
            clearSchedulePreview()
            return
        }
        // No-op if the schedule hasn't changed since the last preview request —
        // avoids redundant POSTs when an unrelated form field triggers a refresh.
        if (trimmed == lastPreviewedSchedule) return
        lastPreviewedSchedule = trimmed

        val api = api ?: return
        val seq = ++previewSeq
        previewJob?.cancel()
        previewJob = viewModelScope.launch {
            delay(300)  // debounce — one POST per typing pause
            try {
                val body = json.encodeToString(CronPreviewRequest(schedule = trimmed, count = count))
                val response = api.post("/api/cron/preview", body)
                if (seq != previewSeq) return@launch  // superseded by a newer request
                val parsed = json.decodeFromString(CronPreviewResponse.serializer(), response)
                parsed.tzAbbrev?.takeIf { it.isNotBlank() }?.let { _boxZone.value = it }
                val labels = parsed.nextRuns.map { CronTimeFormat.absolute(it) }
                _schedulePreview.value = SchedulePreview(
                    runs = labels,
                    invalid = false,
                    visible = true
                )
            } catch (e: Exception) {
                if (seq != previewSeq) return@launch
                // BlackBoxApi.errorFor surfaces the backend `detail` for a 400; the
                // preview route raises 400 "Invalid cron expression '…'" mid-edit.
                // Treat that as the subtle invalid hint; anything else hides quietly.
                val msg = e.message.orEmpty()
                if (msg.contains("Invalid cron", ignoreCase = true) ||
                    msg.contains("HTTP 400")
                ) {
                    _schedulePreview.value = SchedulePreview(invalid = true, visible = true)
                } else {
                    _schedulePreview.value = SchedulePreview(visible = false)
                }
            }
        }
    }

    /** Reset the preview (dialog closed / blank schedule). */
    fun clearSchedulePreview() {
        previewJob?.cancel()
        previewSeq++
        lastPreviewedSchedule = null
        _schedulePreview.value = SchedulePreview()
    }

    // -------------------------------------------------------------------------
    // Live model selector (M4.4) — the shared ModelCatalogRepository (rank 76)
    // -------------------------------------------------------------------------

    /** Canonical catalog provider keys (order = picker order), matching the
     *  backend M4.1 column + Portal M4.3 + chat composer. NOT the words
     *  gemini/claude/grok. */
    val cronProviders: List<String> =
        listOf("google", "openai", "anthropic", "xai", "custom", "computer-use")

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

    /** Normalize any provider string (canonical key OR a legacy word) to the
     *  canonical catalog key. Unknown values default to [defaultCronProvider]. */
    fun canonicalProviderKey(provider: String?): String {
        val p = provider?.trim()?.lowercase().orEmpty()
        if (p.isEmpty()) return defaultCronProvider
        if (p in cronProviders) return p
        return legacyModelToProvider[p] ?: defaultCronProvider
    }

    /** Derive the canonical provider for a job lacking an explicit `provider`
     *  (legacy rows): try the coarse word map, else the known catalogs
     *  ([cronProviderForModelId]), else default. Mirrors Portal deriveProviderFromModel. */
    fun deriveProviderForJob(job: CronJob): String {
        job.provider?.takeIf { it.isNotBlank() }?.let { return canonicalProviderKey(it) }
        val model = job.model.trim()
        if (model.isEmpty()) return defaultCronProvider
        legacyModelToProvider[model.lowercase()]?.let { return it }
        return cronProviderForModelId(model, cronProviders.associateWith { catalogs?.cached(it)?.ids.orEmpty() })
            ?: defaultCronProvider
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

    /** The shared catalog's known copy (memory, else the last-good disk copy), else Auto alone. */
    private fun offlineModels(canonicalKey: String): List<Pair<String, String>> =
        catalogs?.cached(canonicalKey)?.options()?.ifEmpty { null } ?: Constants.AUTO_ONLY

    /** Fetch the model list for [provider] (canonical key OR a legacy word) and
     *  publish it to [modelsForProvider], from the catalog the chat composer shares
     *  (ModelCatalogs: GET /models/{key}, 5-minute TTL, never cached for custom). The
     *  known copy is published at once so the dropdown is never empty. */
    fun selectProvider(provider: String) {
        val key = canonicalProviderKey(provider)
        providerForCurrentList = key
        _modelsForProvider.value = offlineModels(key)

        val catalogs = catalogs ?: return
        viewModelScope.launch {
            val catalog = catalogs.refresh(key) ?: return@launch // the known copy stays
            val withAuto = catalog.options()
            // Only publish if this is still the selected provider (guards a
            // fast provider switch from clobbering the newer list).
            if (withAuto.isNotEmpty() && providerForCurrentList == key) {
                _modelsForProvider.value = withAuto
            }
            Log.d(TAG, "Fetched ${catalog.models.size} models for $key")
        }
    }

    /**
     * The id the editor should hold for a job's stored [model] once [provider]'s fresh
     * live catalog is known: a same-family successor or Auto when the catalog no longer
     * lists it (ModelSelection.replacementFor), else [model] unchanged. Saving the job is
     * what writes the replacement; nothing is changed on the box behind the operator.
     */
    fun reconciledModel(provider: String, model: String): String {
        val catalog = catalogs?.cached(canonicalProviderKey(provider))
            ?.takeIf { it.isAuthoritative } ?: return model
        return ModelSelection.replacementFor(model, catalog.ids) ?: model
    }

    companion object {
        /**
         * The cron provider whose catalog in [idsByProvider] lists [model], else the one
         * its id names (claude-, gemini-, gpt-/o<digit>, grok-, alias::model), else null.
         * The fallback keeps a legacy job's provider when no catalog has loaded yet.
         */
        internal fun cronProviderForModelId(model: String, idsByProvider: Map<String, List<String>>): String? {
            idsByProvider.entries.firstOrNull { model in it.value }?.let { return it.key }
            val id = model.lowercase()
            return when {
                "::" in id -> "custom"
                id.startsWith("claude-") -> "anthropic"
                id.startsWith("gemini-") -> "google"
                id.startsWith("grok-") -> "xai"
                id.startsWith("gpt-") || Regex("o\\d.*").matches(id) -> "openai"
                else -> null
            }
        }
    }

    // -------------------------------------------------------------------------
    // Operators (Fix 4) + Contacts (Fix 5)
    // -------------------------------------------------------------------------

    /** Hydrate the live operator list from GET /operators. Best-effort: on
     *  failure the existing list (possibly empty) is kept and the dialog falls
     *  back gracefully. */
    fun loadOperators() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val response = api.get("/operators")
                val obj = json.parseToJsonElement(response).jsonObject
                val ops = obj["operators"]?.jsonArray?.mapNotNull {
                    try { it.jsonPrimitive.content } catch (_: Exception) { null }
                }.orEmpty()
                if (ops.isNotEmpty()) _operators.value = ops
                obj["default"]?.jsonPrimitive?.content?.let { _defaultOperator.value = it }
            } catch (e: Exception) {
                Log.d(TAG, "Operator fetch failed: ${e.message}")
            }
        }
    }

    /** Fetch the per-operator contact book for the SMS/voice delivery-target
     *  picker (GET /api/cron/contacts?operator=<op>). Publishes to
     *  [previewContacts]; a blank operator or any failure clears the list so the
     *  manual phone field is still usable. Re-call when the operator changes. */
    fun fetchContacts(operator: String) {
        val api = api ?: return
        val op = operator.trim()
        if (op.isEmpty()) {
            _previewContacts.value = emptyList()
            return
        }
        viewModelScope.launch {
            try {
                val encoded = java.net.URLEncoder.encode(op, "UTF-8")
                val response = api.get("/api/cron/contacts?operator=$encoded")
                val parsed = json.decodeFromString(CronContactsResponse.serializer(), response)
                _previewContacts.value = parsed.contacts
            } catch (e: Exception) {
                Log.d(TAG, "Contact fetch failed for $op: ${e.message}")
                _previewContacts.value = emptyList()
            }
        }
    }

    /** Clear the cached contacts (dialog dismissed / delivery switched away). */
    fun clearContacts() {
        _previewContacts.value = emptyList()
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
                    parsed.tzAbbrev?.takeIf { it.isNotBlank() }?.let { _boxZone.value = it }
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
        previewJob?.cancel()
    }
}
