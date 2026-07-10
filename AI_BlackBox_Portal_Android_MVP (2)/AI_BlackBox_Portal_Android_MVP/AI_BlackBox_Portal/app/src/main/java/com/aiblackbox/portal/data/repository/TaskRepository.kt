package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskStatus
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
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
                resultUrl = obj["result_url"]?.jsonPrimitive?.contentOrNull,
                error = obj["error_message"]?.jsonPrimitive?.contentOrNull
            )
        }
    }

    /**
     * List all tasks.
     */
    suspend fun listTasks(): List<TaskStatus> {
        val response = api.get("/tasks/list")
        val obj = api.json.parseToJsonElement(response).jsonObject
        val tasksArray = obj["tasks"]?.jsonArray ?: return emptyList()
        return tasksArray.map { api.json.decodeFromString(TaskStatus.serializer(), it.toString()) }
    }

    /**
     * Poll a task until completion.
     * Emits status updates at the specified interval.
     */
    fun pollTask(taskId: String, intervalMs: Long = 3000): Flow<TaskStatus> = flow {
        while (true) {
            val status = getTaskStatus(taskId)
            emit(status)
            // 'cancelled' is TERMINAL alongside completed/failed (G2-T8) — without
            // it this flow emits forever and the task never resolves in the UI.
            if (status.status.equals("completed", true)
                || status.status.equals("failed", true)
                || status.status.equals("cancelled", true)) break
            delay(intervalMs)
        }
    }

    /**
     * Cancel all pending tasks.
     */
    suspend fun cancelAll(): String {
        return api.post("/tasks/cancel-all", "{}")
    }
}
