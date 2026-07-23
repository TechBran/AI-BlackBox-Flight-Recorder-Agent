package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

@Serializable
data class ChatMessage(
    val role: String,
    val content: JsonElement,
    val timestamp: Long = System.currentTimeMillis(),
    val model: String? = null,
    val provider: String? = null,
    val reasoning: String? = null
)

@Serializable
data class ChatRequest(
    val messages: List<ChatMessage>,
    val operator: String,
    val provider: String? = null,
    val model: String? = null
)

@Serializable
data class StreamRequest(
    val messages: List<ChatMessage>,
    val operator: String,
    val provider: String? = null,
    val model: String? = null,
    @SerialName("session_id") val sessionId: String? = null,
    @SerialName("device_id") val deviceId: String? = null,
    val camera: String? = null,
    // M3 (task 3.6): this phone's OWN tailnet identity (its 100.64/10 IPv4), so a
    // device-control tool triggered by THIS chat defaults to targeting THIS phone.
    // Null for surfaces that can't self-identify → backend falls back to the
    // operator's primary device (unchanged behavior).
    @SerialName("origin_device_id") val originDeviceId: String? = null
)

@Serializable
data class SaveRequest(
    val operator: String,
    @SerialName("user_message") val userMessage: String,
    @SerialName("assistant_response") val assistantResponse: String,
    val reasoning: String? = null,
    val model: String? = null,
    val tokens: TokenCount? = null,
    val provenance: Provenance? = null
)

@Serializable
data class TokenCount(val prompt: Int = 0, val completion: Int = 0)

@Serializable
data class Provenance(
    val recent: List<String> = emptyList(),
    val keyword: List<String> = emptyList(),
    val semantic: List<String> = emptyList(),
    val checkpoint: List<String> = emptyList()
) {
    fun isEmpty(): Boolean =
        recent.isEmpty() && keyword.isEmpty() && semantic.isEmpty() && checkpoint.isEmpty()
    fun totalCount(): Int =
        recent.size + keyword.size + semantic.size + checkpoint.size
}

@Serializable
data class TaskResponse(
    @SerialName("task_id") val taskId: String,
    val status: String,
    val message: String? = null
)

@Serializable
data class TaskStatus(
    @SerialName("task_id") val taskId: String,
    @SerialName("task_type") val taskType: String? = null,
    val status: String,
    val operator: String? = null,
    val progress: Int = 0,
    @SerialName("result_data") val resultData: kotlinx.serialization.json.JsonElement? = null,
    @SerialName("result_url") val resultUrl: String? = null,
    @SerialName("error_message") val error: String? = null,
    // G3-T13 (M3.3, additive): the live agent-step line shown under the pill.
    // Top-level on BOTH /tasks/list and /tasks/status/{id} (Orchestrator
    // task_routes.py :87 / :114), so kotlinx auto-parse fills it on the poll
    // path — but the /tasks/list DISCOVERY loop builds TaskStatus by hand
    // (ChatViewModel.startTaskDiscoveryLoop), so it is ALSO threaded there.
    @SerialName("progress_text") val progressText: String? = null,
    // CU reasoning-narration: the ACCUMULATING model-narration transcript for a
    // computer-use task ("[step 1] clicking…\n[step 2] typing…"). Bounded (~8000
    // char rolling tail), MAY contain newlines, and grows across polls (each poll
    // returns the latest cumulative value — the frontend does NOT accumulate).
    // null/absent for non-CU tasks. Top-level on /tasks/list; the DISCOVERY loop
    // (ChatViewModel.startTaskDiscoveryLoop) builds TaskStatus by hand and is the
    // SOLE panel feed, so this is threaded THERE too, not just via auto-parse.
    @SerialName("reasoning_text") val reasoningText: String? = null,
    // G3-T13: the CU target device for the "Live" button. Top-level ONLY on
    // /tasks/list; on /tasks/status/{id} it lives inside result_data. Prefer the
    // top-level field, else fall back to result_data — see effectiveDeviceId().
    @SerialName("device_id") val deviceId: String? = null,
    // M2 multi-desktop (2026-07-23): the CU session this task DRIVES. Top-level
    // on /tasks/list; inside result_data on /tasks/status/{id}. It routes the
    // "Live" button to the agent's OWN desktop (/cu/view/{session_id}) instead
    // of the first streamable session — see effectiveSessionId().
    @SerialName("session_id") val sessionId: String? = null
) {
    /**
     * Resolve the CU device for the "Live" button regardless of which poll path
     * built this object: top-level `device_id` (/tasks/list) first, else the
     * `device_id` nested in `result_data` (/tasks/status/{id}). Null/blank-safe.
     */
    fun effectiveDeviceId(): String? =
        deviceId?.takeIf { it.isNotBlank() }
            ?: runCatching {
                resultData?.jsonObject?.get("device_id")?.jsonPrimitive?.contentOrNull
            }.getOrNull()?.takeIf { it.isNotBlank() }

    /**
     * Resolve the CU session this task drives, either poll path: top-level
     * `session_id` (/tasks/list) first, else `session_id` in `result_data`
     * (/tasks/status/{id}). Null/blank-safe. The "Live" button navigates to
     * cu_live_view/{this} so it opens the RIGHT agent's desktop.
     */
    fun effectiveSessionId(): String? =
        sessionId?.takeIf { it.isNotBlank() }
            ?: runCatching {
                resultData?.jsonObject?.get("session_id")?.jsonPrimitive?.contentOrNull
            }.getOrNull()?.takeIf { it.isNotBlank() }

    /**
     * cli_agent product provider (claude|gemini|codex) from `result_data.provider`
     * — only /tasks/status/{id} carries it. Drives the specific product label
     * (Claude Code / Gemini CLI / Codex) in TaskUi.taskTypeMeta.
     */
    fun cliProvider(): String? =
        runCatching {
            resultData?.jsonObject?.get("provider")?.jsonPrimitive?.contentOrNull
        }.getOrNull()?.takeIf { it.isNotBlank() }
}

@Serializable
data class HealthResponse(
    val status: String,
    val detail: String? = null,
    @SerialName("snapshot_count") val snapshotCount: Int = 0,
    @SerialName("uptime_seconds") val uptimeSeconds: Long = 0
)

@Serializable
data class UploadResponse(val url: String)
