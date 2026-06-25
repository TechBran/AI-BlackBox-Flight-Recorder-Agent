package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class CronJob(
    val id: String = "",
    val name: String = "",
    val prompt: String = "",
    val schedule: String = "",
    @SerialName("frequency_hint") val frequencyHint: String? = null,
    // M4.4: canonical catalog provider key (google/anthropic/openai/xai/computer-use).
    // Nullable + defaulted so old jobs (written before the backend provider column)
    // round-trip cleanly; legacy rows are backfilled by the backend or derived from
    // `model` on edit. `model` holds the SPECIFIC model id (or "" for Auto).
    val provider: String? = null,
    val model: String = "gemini",
    val delivery: String = "snapshot",
    @SerialName("delivery_target") val deliveryTarget: String? = null,
    val operator: String = "",
    val status: String = "active",
    @SerialName("one_shot") val oneShot: Boolean = false,
    @SerialName("run_count") val runCount: Int = 0,
    @SerialName("next_run_at") val nextRunAt: String? = null,
    @SerialName("last_run_at") val lastRunAt: String? = null,

    // Kept for backward compat with any existing code paths
    val enabled: Boolean = true,
    val expression: String = "",
    val description: String = "",
    @SerialName("next_run") val nextRun: String? = null,
    @SerialName("last_run") val lastRun: String? = null,
    @SerialName("last_status") val lastStatus: String? = null
)

@Serializable
data class CronJobsResponse(val jobs: List<CronJob> = emptyList())

@Serializable
data class CronJobResponse(val job: CronJob? = null)

@Serializable
data class CronHistoryEntry(
    @SerialName("run_at") val runAt: String = "",
    val model: String = "",
    @SerialName("duration_ms") val durationMs: Long = 0,
    @SerialName("delivery_status") val deliveryStatus: String? = null,
    val result: String? = null,
    val error: String? = null
)

@Serializable
data class CronHistoryResponse(val history: List<CronHistoryEntry> = emptyList())

@Serializable
data class CronJobCreateRequest(
    val name: String,
    val prompt: String,
    val schedule: String,
    @SerialName("frequency_hint") val frequencyHint: String? = null,
    // M4.4: canonical catalog provider key sent alongside the specific model id
    // (identical to chat + Portal). Null = let the backend derive it from `model`.
    val provider: String? = null,
    val model: String = "gemini",
    val delivery: String = "snapshot",
    @SerialName("delivery_target") val deliveryTarget: String? = null,
    val operator: String = "",
    @SerialName("one_shot") val oneShot: Boolean = false
)
