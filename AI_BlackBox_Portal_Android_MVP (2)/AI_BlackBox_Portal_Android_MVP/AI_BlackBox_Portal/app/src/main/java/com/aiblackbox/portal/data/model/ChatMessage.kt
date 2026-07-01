package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

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
    @SerialName("error_message") val error: String? = null
)

@Serializable
data class HealthResponse(
    val status: String,
    val detail: String? = null,
    @SerialName("snapshot_count") val snapshotCount: Int = 0,
    @SerialName("uptime_seconds") val uptimeSeconds: Long = 0
)

@Serializable
data class UploadResponse(val url: String)
