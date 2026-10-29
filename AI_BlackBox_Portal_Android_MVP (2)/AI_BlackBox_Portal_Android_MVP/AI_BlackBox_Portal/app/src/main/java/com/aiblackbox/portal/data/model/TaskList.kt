package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/**
 * GET /tasks/list: `{"tasks": [...], "total_tasks": N, "filtered_count": M}`
 * (Orchestrator/routes/task_routes.py). Each row has the [TaskStatus] fields the pill
 * renders, top-level: progress_text, reasoning_text, device_id and session_id included;
 * result_data and error_message are on /tasks/status/{id} only. [tasks] stays raw so one
 * odd row is skipped instead of failing the list ([parseRows]).
 */
@Serializable
data class TaskListResponse(
    val tasks: List<JsonElement>? = null,
    @SerialName("total_tasks") val totalTasks: Int? = null,
    @SerialName("filtered_count") val filteredCount: Int? = null,
) {
    companion object {
        private val json = Json { ignoreUnknownKeys = true; isLenient = true }

        /**
         * The rows of a /tasks/list body, typed: null when the body has no task list (not
         * JSON, an error body), so a poll tick can tell "no answer" from "no tasks"; a row
         * that is not a task (no id, no status, another shape) is skipped.
         *
         * This replaces ChatViewModel's hand-built rows, which also turned a JSON null into
         * the string "null" (`jsonPrimitive.content` of JsonNull) for task_type and
         * result_url.
         */
        fun parseRows(raw: String): List<TaskStatus>? {
            val rows = try {
                val root = json.parseToJsonElement(raw) as? JsonObject ?: return null
                json.decodeFromJsonElement(serializer(), root).tasks ?: return null
            } catch (_: Exception) {
                return null
            }
            return rows.mapNotNull { row ->
                try {
                    json.decodeFromJsonElement(TaskStatus.serializer(), row).takeIf { it.taskId.isNotBlank() }
                } catch (_: Exception) {
                    null
                }
            }
        }
    }
}
