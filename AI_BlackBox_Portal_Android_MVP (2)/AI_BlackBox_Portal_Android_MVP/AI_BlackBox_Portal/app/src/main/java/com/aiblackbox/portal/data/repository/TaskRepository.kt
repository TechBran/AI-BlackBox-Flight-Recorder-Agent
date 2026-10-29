package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskListResponse
import com.aiblackbox.portal.data.model.TaskStatus
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

class TaskRepository(private val api: BlackBoxApi) {

    /**
     * Get status of a single task.
     */
    suspend fun getTaskStatus(taskId: String): TaskStatus {
        val response = api.get("/tasks/status/$taskId")
        return try {
            api.json.decodeFromString(TaskStatus.serializer(), response)
        } catch (e: Exception) {
            // Fallback: parse manually if kotlinx.serialization fails on edge cases
            android.util.Log.w("TaskRepo", "Deserialize fallback for $taskId: ${e.message}")
            val obj = api.json.parseToJsonElement(response).jsonObject
            TaskStatus(
                taskId = obj["task_id"]?.jsonPrimitive?.content ?: taskId,
                taskType = obj["task_type"]?.jsonPrimitive?.contentOrNull,
                status = obj["status"]?.jsonPrimitive?.content ?: "pending",
                progress = obj["progress"]?.jsonPrimitive?.contentOrNull?.toIntOrNull() ?: 0,
                // Carry result_data so TaskStatus.effectiveDeviceId()/cliProvider()
                // still resolve on the manual fallback path (G3-T13 parity).
                resultData = obj["result_data"],
                resultUrl = obj["result_url"]?.jsonPrimitive?.contentOrNull,
                error = obj["error_message"]?.jsonPrimitive?.contentOrNull,
                // G3-T13 (M3.3): live pill line — top-level on /tasks/status/{id}.
                progressText = obj["progress_text"]?.jsonPrimitive?.contentOrNull,
                // CU reasoning-narration transcript — top-level (parity with auto-parse).
                reasoningText = obj["reasoning_text"]?.jsonPrimitive?.contentOrNull
            )
        }
    }

    /**
     * The box's task rows (GET /tasks/list: active plus recently ended), typed; null when
     * the box answered without a task list. One odd row is skipped, not the list.
     */
    suspend fun listTasks(): List<TaskStatus>? = TaskListResponse.parseRows(api.get("/tasks/list"))

    /**
     * Cancel all pending tasks.
     */
    suspend fun cancelAll(): String {
        return api.post("/tasks/cancel-all", "{}")
    }

    /**
     * Cancel ONE task (G3-T13). REAL per-task cancellation (G2-T8): the backend
     * signals the concrete work (process-group kill for a CLI agent / cu-stop for
     * Computer Use / cooperative flag for media) and marks the row CANCELLED.
     * Idempotent. The status flips to `cancelled` on the next poll.
     */
    suspend fun cancel(taskId: String): String {
        return api.post("/tasks/$taskId/cancel", "{}")
    }
}

// Polling a task to its end is TaskPoller's (TaskPoller.kt): the one poller every screen uses.
