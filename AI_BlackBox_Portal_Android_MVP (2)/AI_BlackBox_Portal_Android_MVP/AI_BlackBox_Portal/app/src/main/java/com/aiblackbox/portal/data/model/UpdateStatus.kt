package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Mirrors the backend's /update/status response shape.
 * See Orchestrator/routes/update_routes.py:update_status().
 */
@Serializable
data class UpdateStatus(
    @SerialName("git_initialized") val gitInitialized: Boolean = false,
    @SerialName("current_sha") val currentSha: String? = null,
    @SerialName("current_short") val currentShort: String? = null,
    @SerialName("latest_sha") val latestSha: String? = null,
    @SerialName("latest_short") val latestShort: String? = null,
    @SerialName("commits_behind") val commitsBehind: Int = 0,
    @SerialName("commits_ahead") val commitsAhead: Int = 0,
    val commits: List<UpdateCommit> = emptyList(),
    @SerialName("changed_files") val changedFiles: List<String> = emptyList(),
    val categories: UpdateCategories = UpdateCategories(),
    @SerialName("in_progress") val inProgress: Boolean = false,
    @SerialName("last_state") val lastState: UpdateState? = null,
    @SerialName("last_fetch_age_s") val lastFetchAgeS: Int? = null,
    @SerialName("active_cli_sessions") val activeCliSessions: Int = 0,
    @SerialName("fetch_error") val fetchError: String? = null,
    val message: String? = null,
    // Tracked files with local changes that Install/Rollback would overwrite
    // (audit O16-01). The backend refuses both unless the request consents to
    // stashing them; the confirm dialog names them first.
    @SerialName("dirty_files") val dirtyFiles: List<String> = emptyList(),
)

@Serializable
data class UpdateCommit(
    val sha: String = "",
    val short: String = "",
    val subject: String = "",
    val author: String = "",
    @SerialName("date_iso") val dateIso: String = "",
)

@Serializable
data class UpdateCategories(
    val apt: Boolean = false,
    val pip: Boolean = false,
    @SerialName("mcp_pip") val mcpPip: Boolean = false,
    val sudoers: Boolean = false,
    val helpers: Boolean = false,
    val systemd: Boolean = false,
    @SerialName("code_only") val codeOnly: Boolean = false,
) {
    /** Returns the list of "active" bucket names (excluding code_only sentinel). */
    fun activeBuckets(): List<String> = buildList {
        if (apt) add("apt")
        if (pip) add("pip")
        if (mcpPip) add("mcp_pip")
        if (sudoers) add("sudoers")
        if (helpers) add("helpers")
        if (systemd) add("systemd")
    }
}

/**
 * Persisted state from Manifest/update_state.json. Phase strings match
 * the PHASE_* constants in Orchestrator/update/manager.py.
 */
@Serializable
data class UpdateState(
    @SerialName("task_id") val taskId: String = "",
    val phase: String = "",
    @SerialName("target_sha") val targetSha: String = "",
    @SerialName("from_sha") val fromSha: String = "",
    @SerialName("pre_update_tag") val preUpdateTag: String = "",
    @SerialName("updated_iso") val updatedIso: String = "",
    @SerialName("failed_phase") val failedPhase: String? = null,
    val error: String? = null,
) {
    val isTerminal: Boolean
        get() = phase in setOf("complete", "failed", "rolled_back")
}

/** Body for POST /update/start. */
@Serializable
data class UpdateStartRequest(
    @SerialName("confirm_sha") val confirmSha: String? = null,
    /** Consent to stash local changes to tracked files (shown in the confirm). */
    @SerialName("discard_local_changes") val discardLocalChanges: Boolean = false,
)

/** Body for POST /update/rollback. */
@Serializable
data class UpdateRollbackRequest(
    @SerialName("discard_local_changes") val discardLocalChanges: Boolean = false,
)

/** Response from POST /update/start. */
@Serializable
data class UpdateStartResponse(
    @SerialName("task_id") val taskId: String = "",
    val status: String = "",
)

/** Response from POST /update/rollback. task_id (newer backends) names the
 *  rollback task to stream from /update/log/stream, like an install. */
@Serializable
data class UpdateRollbackResponse(
    @SerialName("task_id") val taskId: String = "",
    @SerialName("reverted_to") val revertedTo: String = "",
    @SerialName("current_sha") val currentSha: String = "",
    @SerialName("restart_scheduled") val restartScheduled: Boolean = false,
)
