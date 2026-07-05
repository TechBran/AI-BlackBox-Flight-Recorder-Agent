package com.aiblackbox.portal.ui.updates

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.EmbeddingsStatus
import com.aiblackbox.portal.data.model.RerankStatus
import com.aiblackbox.portal.data.model.UpdateStatus
import com.aiblackbox.portal.data.repository.UpdateRepository
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Maps the backend's update flow into a Compose-friendly state machine.
 *
 * UI states (parallel to Portal/modules/updates-manager.js):
 *   Loading             — first fetch in flight
 *   Error               — fetch itself failed (network down, etc.)
 *   GitNotInitialized   — backend reports git_initialized=false
 *   UpToDate            — commits_behind=0 AND commits_ahead=0
 *   LocalAhead          — commits_ahead>0 (unpushed local work)
 *   UpdatesAvailable    — commits_behind>0
 *   InProgress          — backend's in_progress=true (another runner is going)
 *   Failed              — last_state.phase=failed
 *   Interrupted         — last_state phase non-terminal AND not in_progress
 *
 * Once the user clicks Install:
 *   - status moves to RunningUpdate with a Flow<LogLine> for SSE events
 *   - on 'complete' SSE event with succeeded=true: AwaitingRestart, then
 *     poll /health every 2s up to 180s with progressive copy
 *   - on 'complete' with succeeded=false: back to Failed state
 */
class UpdatesViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null
    private var repo: UpdateRepository? = null
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _state = MutableStateFlow<UpdatesUiState>(UpdatesUiState.Loading)
    val state: StateFlow<UpdatesUiState> = _state.asStateFlow()

    private val _logLines = MutableStateFlow<List<LogLine>>(emptyList())
    val logLines: StateFlow<List<LogLine>> = _logLines.asStateFlow()

    private val _logModalOpen = MutableStateFlow(false)
    val logModalOpen: StateFlow<Boolean> = _logModalOpen.asStateFlow()

    private val _restartPollLabel = MutableStateFlow<String?>(null)
    val restartPollLabel: StateFlow<String?> = _restartPollLabel.asStateFlow()

    // Embeddings notification card (parity with the Portal card in
    // updates-manager.js). null = status unavailable or not yet fetched →
    // card absent; an embeddings failure can NEVER break the updates panel.
    private val _embeddings = MutableStateFlow<EmbeddingsStatus?>(null)
    val embeddings: StateFlow<EmbeddingsStatus?> = _embeddings.asStateFlow()

    private val _embeddingsUpdateInFlight = MutableStateFlow(false)
    val embeddingsUpdateInFlight: StateFlow<Boolean> = _embeddingsUpdateInFlight.asStateFlow()

    /** One-shot error message for the screen's snackbar; cleared after shown. */
    private val _embeddingsError = MutableStateFlow<String?>(null)
    val embeddingsError: StateFlow<String?> = _embeddingsError.asStateFlow()

    // Reranker selector card (M12, surface 3/3 — parity with the M11 Portal
    // card + M10.1 wizard selector). null = /rerank/status unavailable or not
    // yet fetched → card absent; a reranker failure can NEVER break the panel.
    private val _rerankStatus = MutableStateFlow<RerankStatus?>(null)
    val rerankStatus: StateFlow<RerankStatus?> = _rerankStatus.asStateFlow()

    private val _rerankBusy = MutableStateFlow(false)
    val rerankBusy: StateFlow<Boolean> = _rerankBusy.asStateFlow()

    /** One-shot error message for the reranker-select snackbar. */
    private val _rerankError = MutableStateFlow<String?>(null)
    val rerankError: StateFlow<String?> = _rerankError.asStateFlow()

    private var streamJob: Job? = null
    private var pollJob: Job? = null

    fun initialize(origin: String) {
        if (api == null) {
            api = BlackBoxApi(origin)
            repo = UpdateRepository(api!!)
        }
        refreshStatus(forceFresh = false)
    }

    fun refreshStatus(forceFresh: Boolean) {
        val r = repo ?: return
        _state.value = UpdatesUiState.Loading
        refreshEmbeddings()
        refreshRerank()
        viewModelScope.launch {
            try {
                val status = if (forceFresh) r.preflight() else r.getStatus()
                _state.value = mapStatusToState(status)
            } catch (e: Exception) {
                Log.e(TAG, "refreshStatus failed", e)
                _state.value = UpdatesUiState.Error(e.message ?: "unknown error")
            }
        }
    }

    // ── Embeddings notification card ────────────────────────────────────

    /**
     * Independent fetch of GET /embeddings/status. Failure → null → the card
     * simply doesn't render; it never touches the main updates state (the
     * Portal card has the same fail-quiet contract).
     */
    fun refreshEmbeddings() {
        val r = repo ?: return
        viewModelScope.launch {
            _embeddings.value = try {
                r.getEmbeddingsStatus()
            } catch (e: Exception) {
                Log.w(TAG, "embeddings status unavailable: ${e.message}")
                null
            }
        }
    }

    /**
     * [Update] on the superseded card: POST /embeddings/migrate, then refetch
     * status — 200 and 409 both mean a job is running now, so the refreshed
     * card shows live progress (mirrors _onEmbeddingsUpdateClick in the
     * Portal's updates-manager.js).
     */
    fun startEmbeddingsMigration(targetSlug: String) {
        val r = repo ?: return
        if (_embeddingsUpdateInFlight.value) return  // double-tap guard
        _embeddingsUpdateInFlight.value = true
        viewModelScope.launch {
            try {
                r.startEmbeddingsMigration(targetSlug)
                refreshEmbeddings()
            } catch (e: Exception) {
                Log.e(TAG, "embeddings migrate failed", e)
                _embeddingsError.value = "Could not start embedding update: ${e.message}"
            } finally {
                _embeddingsUpdateInFlight.value = false
            }
        }
    }

    fun clearEmbeddingsError() {
        _embeddingsError.value = null
    }

    // ── Reranker selector card (M12) ────────────────────────────────────

    /**
     * Independent fetch of GET /rerank/status. Failure → null → the card
     * simply doesn't render; it never touches the main updates state (same
     * fail-quiet contract as the embeddings card + the Portal's soft
     * /rerank/status probe in updates-manager.js).
     */
    fun refreshRerank() {
        val r = repo ?: return
        viewModelScope.launch {
            _rerankStatus.value = try {
                r.getRerankStatus()
            } catch (e: Exception) {
                Log.w(TAG, "rerank status unavailable: ${e.message}")
                null
            }
        }
    }

    /**
     * Select a reranker (or toggle it off). POSTs /rerank/select with
     * provider/model/enabled and — for an already-keyed provider — NO api_key;
     * only the paste-key path (an un-keyed Voyage/Cohere entry) passes [apiKey],
     * which the endpoint writes to .env + mirrors into os.environ. Mirrors the
     * Portal's _onRerankSelect refresh-from-server discipline: the echoed status
     * updates the card (key_present/preflight/enabled) without a second fetch.
     */
    fun selectRerank(provider: String, model: String, enabled: Boolean, apiKey: String? = null) {
        val r = repo ?: return
        if (_rerankBusy.value) return  // double-tap guard
        _rerankBusy.value = true
        viewModelScope.launch {
            try {
                _rerankStatus.value = r.selectRerank(provider, model, enabled, apiKey)
            } catch (e: Exception) {
                Log.e(TAG, "rerank select failed", e)
                _rerankError.value = "Could not change the reranker: ${e.message}"
                refreshRerank()  // fall back to the server's current truth
            } finally {
                _rerankBusy.value = false
            }
        }
    }

    fun clearRerankError() {
        _rerankError.value = null
    }

    private fun mapStatusToState(status: UpdateStatus): UpdatesUiState {
        if (!status.gitInitialized) {
            return UpdatesUiState.GitNotInitialized(status.message)
        }
        if (status.fetchError != null) {
            return UpdatesUiState.Error("fetch error: ${status.fetchError}")
        }
        if (status.inProgress) {
            // Re-poll in 3s; another runner is going. If we already have an
            // active stream the modal is showing those events; otherwise the
            // panel just waits.
            viewModelScope.launch {
                delay(3000)
                if (_state.value is UpdatesUiState.InProgress) {
                    refreshStatus(forceFresh = false)
                }
            }
            return UpdatesUiState.InProgress(status)
        }
        val lastState = status.lastState
        if (lastState != null && lastState.phase == "failed") {
            return UpdatesUiState.Failed(status, lastState)
        }
        if (lastState != null && !lastState.isTerminal && !status.inProgress) {
            return UpdatesUiState.Interrupted(status, lastState)
        }
        return when {
            status.commitsBehind > 0 -> UpdatesUiState.UpdatesAvailable(status)
            status.commitsAhead > 0 -> UpdatesUiState.LocalAhead(status)
            else -> UpdatesUiState.UpToDate(status)
        }
    }

    fun startUpdate() {
        val r = repo ?: return
        val current = _state.value
        val confirmSha = (current as? UpdatesUiState.UpdatesAvailable)?.status?.latestSha
        viewModelScope.launch {
            try {
                _logLines.value = emptyList()
                _logModalOpen.value = true
                _logLines.value = _logLines.value + LogLine.system("Starting update…")
                val response = r.start(confirmSha = confirmSha)
                streamLog(response.taskId)
            } catch (e: Exception) {
                Log.e(TAG, "startUpdate failed", e)
                _logLines.value = _logLines.value + LogLine.error("Failed to start: ${e.message}")
            }
        }
    }

    private fun streamLog(taskId: String) {
        val r = repo ?: return
        streamJob?.cancel()
        streamJob = viewModelScope.launch {
            try {
                r.streamLog(taskId).collect { event ->
                    val parsed = try { json.parseToJsonElement(event.data).jsonObject } catch (_: Exception) { null }
                    if (parsed != null) {
                        handleSseEvent(parsed)
                    }
                }
                Log.d(TAG, "SSE stream completed for task=$taskId")
            } catch (e: Exception) {
                Log.e(TAG, "SSE stream error", e)
                _logLines.value = _logLines.value + LogLine.system("(stream disconnected — service may be restarting)")
                // If we got a complete event already we'll be in AwaitingRestart anyway.
            }
        }
    }

    private fun handleSseEvent(obj: JsonObject) {
        val type = obj["type"]?.jsonPrimitive?.content ?: return
        when (type) {
            "heartbeat" -> { /* silent keepalive */ }
            "phase" -> {
                val phase = obj["phase"]?.jsonPrimitive?.content ?: "?"
                _logLines.value = _logLines.value + LogLine.phase(phase.uppercase())
            }
            "log" -> {
                val phase = obj["phase"]?.jsonPrimitive?.content
                val text = obj["text"]?.jsonPrimitive?.content ?: ""
                _logLines.value = _logLines.value + LogLine.line(text, phase)
            }
            "complete" -> {
                val ok = obj["succeeded"]?.jsonPrimitive?.content == "true"
                if (ok) {
                    val from = obj["sha_before"]?.jsonPrimitive?.content ?: ""
                    val to = obj["sha_after"]?.jsonPrimitive?.content ?: ""
                    _logLines.value = _logLines.value + LogLine.success("✓ COMPLETE: $from → $to")
                    awaitRestart()
                } else {
                    val err = obj["error"]?.jsonPrimitive?.content ?: "(no error msg)"
                    _logLines.value = _logLines.value + LogLine.error("✕ FAILED: $err")
                    // Refresh panel state so Failed branch surfaces with rollback button
                    viewModelScope.launch {
                        delay(1500)
                        refreshStatus(forceFresh = false)
                    }
                }
            }
        }
    }

    /**
     * After 'complete' event: poll /health every 2s up to 180s with progressive
     * copy (mirrors web Portal audit C5).
     */
    private fun awaitRestart() {
        val r = repo ?: return
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            val start = System.currentTimeMillis()
            val timeoutMs = 180_000L
            while (isActive && System.currentTimeMillis() - start < timeoutMs) {
                val elapsed = System.currentTimeMillis() - start
                val label = when {
                    elapsed < 30_000 -> "Restarting service…"
                    elapsed < 90_000 -> "Rebuilding snapshot index (60–90s typical)…"
                    else -> "Still warming. Check logs via journalctl if this hangs."
                }
                _restartPollLabel.value = label
                if (r.healthOk()) {
                    _logLines.value = _logLines.value + LogLine.success("✓ Service back online after ${elapsed / 1000}s.")
                    _restartPollLabel.value = null
                    delay(1500)
                    _logModalOpen.value = false
                    refreshStatus(forceFresh = true)
                    return@launch
                }
                delay(2000)
            }
            _restartPollLabel.value = null
            _logLines.value = _logLines.value + LogLine.error("⚠ Still down after 180s. Check the logs manually.")
        }
    }

    fun rollback() {
        val r = repo ?: return
        viewModelScope.launch {
            try {
                val response = r.rollback()
                _logLines.value = listOf(LogLine.system("Rolled back to ${response.revertedTo}. Restarting service…"))
                _logModalOpen.value = true
                awaitRestart()
            } catch (e: Exception) {
                Log.e(TAG, "rollback failed", e)
                _logLines.value = _logLines.value + LogLine.error("Rollback failed: ${e.message}")
            }
        }
    }

    fun closeLogModal() {
        _logModalOpen.value = false
    }

    override fun onCleared() {
        streamJob?.cancel()
        pollJob?.cancel()
        super.onCleared()
    }

    companion object { private const val TAG = "UpdatesViewModel" }
}

/** Sealed UI state — Compose collects this StateFlow and renders accordingly. */
sealed class UpdatesUiState {
    data object Loading : UpdatesUiState()
    data class Error(val message: String) : UpdatesUiState()
    data class GitNotInitialized(val message: String?) : UpdatesUiState()
    data class UpToDate(val status: UpdateStatus) : UpdatesUiState()
    data class LocalAhead(val status: UpdateStatus) : UpdatesUiState()
    data class UpdatesAvailable(val status: UpdateStatus) : UpdatesUiState()
    data class InProgress(val status: UpdateStatus) : UpdatesUiState()
    data class Failed(
        val status: UpdateStatus,
        val lastState: com.aiblackbox.portal.data.model.UpdateState,
    ) : UpdatesUiState()
    data class Interrupted(
        val status: UpdateStatus,
        val lastState: com.aiblackbox.portal.data.model.UpdateState,
    ) : UpdatesUiState()
}

/** Single log-modal entry. kind drives styling (success=green, error=red, etc.). */
data class LogLine(
    val text: String,
    val kind: Kind,
    val phase: String? = null,
) {
    enum class Kind { System, Phase, Line, Success, Error }
    companion object {
        fun system(t: String) = LogLine(t, Kind.System)
        fun phase(p: String) = LogLine("── $p ──", Kind.Phase, p)
        fun line(t: String, phase: String?) = LogLine("  $t", Kind.Line, phase)
        fun success(t: String) = LogLine(t, Kind.Success)
        fun error(t: String) = LogLine(t, Kind.Error)
    }
}
