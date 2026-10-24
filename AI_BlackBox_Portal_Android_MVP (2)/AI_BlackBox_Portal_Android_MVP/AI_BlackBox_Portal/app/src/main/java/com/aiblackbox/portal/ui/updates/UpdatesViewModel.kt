package com.aiblackbox.portal.ui.updates

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.EmbeddingsStatus
import com.aiblackbox.portal.data.model.RerankStatus
import com.aiblackbox.portal.data.model.UpdateStatus
import com.aiblackbox.portal.data.repository.UpdateRepository
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
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
 * Once the user clicks Install (or Rollback):
 *   - a confirm dialog names what happens, including local changes to tracked
 *     files that the backend will stash (audit rank 43, O16-01)
 *   - the log modal streams the task's SSE events
 *   - on 'complete' with a restart scheduled: poll /health every 2s up to 180s
 *     until its boot_id CHANGES (A13-ui-ops-01), with progressive copy
 *   - on 'complete' without one (failed, refused, up to date): refresh, so the
 *     Failed state surfaces
 *   - when the stream drops first: the same wait, asking /update/state what the
 *     task is doing (A13-ui-ops-06; see RestartWait.taskVerdict)
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

    // Install and Rollback ask first (A13-ui-ops-02): the screen renders this
    // as an AlertDialog and answers with confirmPending() / dismissConfirm().
    private val _confirm = MutableStateFlow<UpdateConfirm?>(null)
    val confirm: StateFlow<UpdateConfirm?> = _confirm.asStateFlow()

    // True from a confirmed Install/Rollback until its flow ends: the buttons
    // disable, so a second tap cannot send a second /update/start.
    private val _actionInFlight = MutableStateFlow(false)
    val actionInFlight: StateFlow<Boolean> = _actionInFlight.asStateFlow()

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

    // Reranker status line (read-only; the selector moved to the onboarding
    // wizard). null = /rerank/status unavailable or not yet fetched → line
    // absent; a reranker failure can NEVER break the updates panel.
    private val _rerankStatus = MutableStateFlow<RerankStatus?>(null)
    val rerankStatus: StateFlow<RerankStatus?> = _rerankStatus.asStateFlow()

    /**
     * B5: single "something to surface" signal for the activity-scoped top-bar
     * Updates badge. TRUE when any of:
     *   - an update is available (state is [UpdatesUiState.UpdatesAvailable]), OR
     *   - embedding health is "broken" or "superseded", OR
     *   - an embeddings migration job is running.
     * Derived from the same flows the Updates screen renders, so the badge and
     * the screen can never disagree. Started eagerly so it stays populated for
     * the badge even while the Updates screen is closed (this VM lives at
     * activity scope now).
     */
    val attention: StateFlow<Boolean> = combine(_state, _embeddings) { state, emb ->
        val updateAvailable = state is UpdatesUiState.UpdatesAvailable
        val embHealth = emb?.health?.state
        val embNeedsAttention = embHealth == "broken" || embHealth == "superseded"
        val embJobRunning = emb?.job?.state == "running"
        updateAvailable || embNeedsAttention || embJobRunning
    }.stateIn(viewModelScope, SharingStarted.Eagerly, false)

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

    // ── Reranker status line (read-only) ────────────────────────────────

    /**
     * Independent fetch of GET /rerank/status. Failure → null → the status
     * line simply doesn't render; it never touches the main updates state
     * (same fail-quiet contract as the embeddings card + the Portal's soft
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

    // ── Install / Rollback (audit rank 43) ──────────────────────────────

    /** Install asks first (A13-ui-ops-02); the screen renders [confirm]. */
    fun requestInstall() {
        val status = (_state.value as? UpdatesUiState.UpdatesAvailable)?.status ?: return
        if (_actionInFlight.value) return
        _confirm.value = UpdateConfirm.Install(
            text = UpdateConfirmText.install(status),
            confirmSha = status.latestSha,
            // The dialog names these files: agreeing to it is the consent the
            // backend needs to stash them (O16-01).
            discardLocalChanges = status.dirtyFiles.isNotEmpty(),
        )
    }

    /** Rollback asks first, naming any local changes it will stash. */
    fun requestRollback() {
        if (_actionInFlight.value) return
        val status = _state.value.statusOrNull()
        _confirm.value = UpdateConfirm.Rollback(
            text = UpdateConfirmText.rollback(status),
            discardLocalChanges = status?.dirtyFiles?.isNotEmpty() == true,
        )
    }

    fun confirmPending() {
        val pending = _confirm.value ?: return
        _confirm.value = null
        when (pending) {
            is UpdateConfirm.Install -> startUpdate(pending.confirmSha, pending.discardLocalChanges)
            is UpdateConfirm.Rollback -> rollback(pending.discardLocalChanges)
        }
    }

    fun dismissConfirm() {
        _confirm.value = null
    }

    private fun startUpdate(confirmSha: String?, discardLocalChanges: Boolean) {
        val r = repo ?: return
        if (_actionInFlight.value) return  // double-tap guard
        _actionInFlight.value = true
        viewModelScope.launch {
            _logLines.value = listOf(LogLine.system("Starting update…"))
            _logModalOpen.value = true
            // Read before the action: the old process's id is what must change.
            val priorBootId = r.bootId()
            try {
                val response = r.start(confirmSha = confirmSha, discardLocalChanges = discardLocalChanges)
                streamLog(response.taskId, priorBootId)
            } catch (e: Exception) {
                Log.e(TAG, "startUpdate failed", e)
                append(LogLine.error("Failed to start: ${e.message}"))
                _actionInFlight.value = false
                if ((e as? ApiHttpException)?.code == 409) {
                    // A fresh fetch: the 409 may say origin/main moved.
                    afterConflict(discardLocalChanges, refresh = { r.preflight() }) { fresh ->
                        UpdateConfirm.Install(
                            text = UpdateConfirmText.stashRetry(fresh.dirtyFiles, "Installing"),
                            confirmSha = fresh.latestSha,
                            discardLocalChanges = true,
                        )
                    }
                }
            }
        }
    }

    private fun rollback(discardLocalChanges: Boolean) {
        val r = repo ?: return
        if (_actionInFlight.value) return
        _actionInFlight.value = true
        viewModelScope.launch {
            _logLines.value = listOf(LogLine.system("Rolling back…"))
            _logModalOpen.value = true
            val priorBootId = r.bootId()
            try {
                val response = r.rollback(discardLocalChanges)
                if (response.taskId.isNotEmpty()) {
                    // Newer backends run the rollback as a task (venv restore +
                    // reset under the update lock): stream it like an install.
                    append(LogLine.system("Rolling back to ${response.revertedTo}…"))
                    streamLog(response.taskId, priorBootId)
                } else {
                    append(LogLine.system("Rolled back to ${response.revertedTo}. Restarting service…"))
                    awaitRestart(priorBootId)
                }
            } catch (e: Exception) {
                Log.e(TAG, "rollback failed", e)
                append(LogLine.error("Rollback failed: ${e.message}"))
                _actionInFlight.value = false
                if ((e as? ApiHttpException)?.code == 409) {
                    // Never preflight here: it deletes a failed state, and with
                    // it the tag this rollback needs.
                    afterConflict(discardLocalChanges, refresh = { r.getStatus() }) { fresh ->
                        UpdateConfirm.Rollback(
                            text = UpdateConfirmText.stashRetry(fresh.dirtyFiles, "Rolling back"),
                            discardLocalChanges = true,
                        )
                    }
                }
            }
        }
    }

    /**
     * After a 409: refresh the panel with [refresh] (A13-ui-ops-02), and when
     * the refusal was local changes the request had no consent for, ask again
     * with the backend's own list of files.
     */
    private suspend fun afterConflict(
        consentSent: Boolean,
        refresh: suspend () -> UpdateStatus,
        retry: (UpdateStatus) -> UpdateConfirm,
    ) {
        val fresh = try {
            refresh()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "status after 409 failed: ${e.message}")
            null
        } ?: return
        _state.value = mapStatusToState(fresh)
        if (!consentSent && fresh.dirtyFiles.isNotEmpty()) {
            _logModalOpen.value = false
            _confirm.value = retry(fresh)
        }
    }

    private fun streamLog(taskId: String, priorBootId: String?) {
        val r = repo ?: return
        streamJob?.cancel()
        streamJob = viewModelScope.launch {
            var completeSeen = false
            try {
                r.streamLog(taskId).collect { event ->
                    val parsed = try { json.parseToJsonElement(event.data).jsonObject } catch (_: Exception) { null }
                    if (parsed != null && handleSseEvent(parsed, priorBootId)) completeSeen = true
                }
                Log.d(TAG, "SSE stream completed for task=$taskId")
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "SSE stream error", e)
                append(LogLine.system("(stream disconnected — service may be restarting)"))
            }
            if (!completeSeen) {
                // A13-ui-ops-06: the runner keeps going without the stream (Doze,
                // a network blip). Wait for the restart, asking /update/state
                // about THIS task: keep waiting while it runs or once it
                // scheduled its restart, stop once it ended without one.
                awaitRestart(priorBootId, taskState = {
                    try {
                        RestartWait.taskVerdict(r.getState(), taskId)
                    } catch (e: CancellationException) {
                        throw e
                    } catch (_: Exception) {
                        null
                    }
                })
            }
        }
    }

    /** Applies one SSE event; true when it was the final 'complete' event. */
    private fun handleSseEvent(obj: JsonObject, priorBootId: String?): Boolean {
        val type = obj["type"]?.jsonPrimitive?.content ?: return false
        when (type) {
            "heartbeat" -> { /* silent keepalive */ }
            "phase" -> {
                val phase = obj["phase"]?.jsonPrimitive?.content ?: "?"
                append(LogLine.phase(phase.uppercase()))
            }
            "log" -> {
                val phase = obj["phase"]?.jsonPrimitive?.content
                val text = obj["text"]?.jsonPrimitive?.content ?: ""
                append(LogLine.line(text, phase))
            }
            "complete" -> {
                val ok = obj["succeeded"]?.jsonPrimitive?.booleanOrNull == true
                val from = obj["sha_before"]?.jsonPrimitive?.content ?: ""
                val to = obj["sha_after"]?.jsonPrimitive?.content ?: ""
                if (ok) {
                    append(LogLine.success("✓ COMPLETE: $from → $to"))
                } else {
                    val err = obj["error"]?.jsonPrimitive?.content ?: "(no error msg)"
                    append(LogLine.error("✕ FAILED: $err"))
                }
                val scheduled = obj["restart_scheduled"]?.jsonPrimitive?.booleanOrNull
                if (RestartWait.restartExpected(ok, scheduled, from, to)) {
                    awaitRestart(priorBootId)
                } else {
                    // Nothing restarts (already up to date, refused, failed):
                    // refresh so the Failed card and its Rollback button surface.
                    if (ok) append(LogLine.system("No restart needed."))
                    _actionInFlight.value = false
                    viewModelScope.launch {
                        delay(1500)
                        refreshStatus(forceFresh = false)
                    }
                }
                return true
            }
        }
        return false
    }

    /**
     * After a restart was requested: poll /health every 2s up to 180s with
     * progressive copy (mirrors web Portal audit C5) until its boot_id differs
     * from [priorBootId] (A13-ui-ops-01). The old process answers for ~2 s
     * after the complete event; that answer is not "back online".
     */
    private fun awaitRestart(
        priorBootId: String?,
        taskState: (suspend () -> RestartWait.TaskVerdict?)? = null,
    ) {
        val r = repo ?: return
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            val outcome = RestartWait.await(
                priorBootId = priorBootId,
                probe = { r.bootId() },
                onTick = { elapsed, verdict -> _restartPollLabel.value = RestartWait.stageLabel(elapsed, verdict) },
                taskState = taskState,
            )
            _restartPollLabel.value = null
            _actionInFlight.value = false
            when (outcome) {
                is RestartWait.Outcome.Restarted -> {
                    append(LogLine.success("✓ Service back online after ${outcome.elapsedMs / 1000}s."))
                    delay(1500)
                    _logModalOpen.value = false
                    refreshStatus(forceFresh = true)
                }
                is RestartWait.Outcome.GaveUp -> {
                    append(LogLine.system("The update ended and the service did not restart. See the Updates panel for what happened."))
                    refreshStatus(forceFresh = false)
                }
                is RestartWait.Outcome.StillRunning -> {
                    append(LogLine.system("The update is still running on the box after ${outcome.elapsedMs / 60_000} min. " +
                        "The Updates panel shows its progress; the service restarts when it finishes."))
                    refreshStatus(forceFresh = false)
                }
                is RestartWait.Outcome.NotRestarted -> append(LogLine.error(
                    "⚠ After ${outcome.elapsedMs / 1000}s the service is still the process that was running before: the restart did not happen. " +
                        "Restart it from Settings → System, or run `sudo systemctl restart blackbox.service` on the box."))
                is RestartWait.Outcome.StillDown -> append(LogLine.error("⚠ Still down after ${outcome.elapsedMs / 1000}s. Check the logs manually."))
            }
        }
    }

    private fun append(line: LogLine) {
        _logLines.value = _logLines.value + line
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

/** The status a UI state carries, or null for Loading/Error/GitNotInitialized. */
fun UpdatesUiState.statusOrNull(): UpdateStatus? = when (this) {
    is UpdatesUiState.UpToDate -> status
    is UpdatesUiState.LocalAhead -> status
    is UpdatesUiState.UpdatesAvailable -> status
    is UpdatesUiState.InProgress -> status
    is UpdatesUiState.Failed -> status
    is UpdatesUiState.Interrupted -> status
    else -> null
}

/** A destructive action waiting for the operator's yes (A13-ui-ops-02). */
sealed class UpdateConfirm(val title: String, val confirmLabel: String) {
    abstract val text: String

    data class Install(
        override val text: String,
        val confirmSha: String?,
        val discardLocalChanges: Boolean,
    ) : UpdateConfirm("Install update", "Install")

    data class Rollback(
        override val text: String,
        val discardLocalChanges: Boolean,
    ) : UpdateConfirm("Roll back", "Roll back")
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
