package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskStatus
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.IOException
import kotlin.coroutines.cancellation.CancellationException

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
     * Emits status updates at the specified interval. Transient failures (a network
     * blip, the box restarting) are retried per [retry] instead of ending the flow;
     * an exhausted budget ends it with [TaskPollLostException]. See [pollTaskStatus].
     */
    fun pollTask(
        taskId: String,
        intervalMs: Long = 3000,
        retry: PollRetryPolicy = PollRetryPolicy(),
    ): Flow<TaskStatus> = pollTaskStatus(taskId, intervalMs, retry) { getTaskStatus(taskId) }

    /**
     * Poll [taskId] to its end and say how it ended ([awaitTaskOutcome]) — the one
     * poller the generation screens share. Never throws except for cancellation, so
     * a caller's `viewModelScope.launch` can't crash the app on a dropped connection.
     */
    suspend fun awaitTask(
        taskId: String,
        intervalMs: Long = 3000,
        onStatus: (TaskStatus) -> Unit = {},
    ): TaskPollOutcome = awaitTaskOutcome(pollTask(taskId, intervalMs), onStatus)

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

/** What the generation screens say when a poll gives up: the task itself is fine. */
const val LOST_CONNECTION_MESSAGE = "Lost connection; the task keeps running on the server"

/**
 * Bounded retry budget for a task poll's TRANSIENT failures (A11-settings-generation-01).
 * One IOException from GET /tasks/status used to end the poll — and, in a bare
 * `viewModelScope.launch`, the app. The budget counts CONSECUTIVE failures only and
 * resets on every good reply, so a 20-minute video survives any number of separate
 * blips. Default: 6 retries waiting 2s 4s 8s 16s 30s 30s (~90s) — long enough for
 * the box's own service restart to answer again.
 */
data class PollRetryPolicy(
    val maxConsecutiveRetries: Int = 6,
    val initialBackoffMs: Long = 2_000,
    val maxBackoffMs: Long = 30_000,
) {
    /** The wait before retry number [attempt] (0-based): doubling, capped at [maxBackoffMs]. */
    fun backoffMs(attempt: Int): Long =
        (initialBackoffMs shl attempt.coerceIn(0, 20)).coerceAtMost(maxBackoffMs)
}

/**
 * The poll gave up after [PollRetryPolicy.maxConsecutiveRetries] transient failures in a
 * row. Only the CONNECTION was lost: the task is not known to have failed and keeps
 * running on the server.
 */
class TaskPollLostException(val taskId: String, cause: IOException) :
    IOException(LOST_CONNECTION_MESSAGE, cause)

/**
 * True for a failure worth retrying: transport errors (timeouts, resets, no route) and
 * the server's 5xx / 408 / 429. Any other 4xx is an ANSWER — 404 means the box does not
 * know the task — so retrying it would only delay the terminal state.
 */
internal fun isTransientPollFailure(e: IOException): Boolean = when (e) {
    is ApiHttpException -> e.code == 0 || e.code == 408 || e.code == 429 || e.code >= 500
    else -> true
}

/** 'cancelled' is TERMINAL alongside completed/failed (G2-T8) — without it a poll emits
 *  forever and the task never resolves in the UI. */
internal fun isTerminalTaskStatus(status: String): Boolean =
    status.equals("completed", true) ||
        status.equals("failed", true) ||
        status.equals("cancelled", true)

/**
 * The poll loop behind [TaskRepository.pollTask], over an injectable [fetch] so it is
 * testable without HTTP. Emits every status until a terminal one; a transient
 * [IOException] waits [PollRetryPolicy.backoffMs] and asks again (the emit stays OUTSIDE
 * the try, so a collector's own exception is never mistaken for a poll failure).
 */
internal fun pollTaskStatus(
    taskId: String,
    intervalMs: Long,
    retry: PollRetryPolicy,
    fetch: suspend () -> TaskStatus,
): Flow<TaskStatus> = flow {
    var failures = 0
    while (true) {
        val status = try {
            fetch()
        } catch (e: IOException) {
            if (!isTransientPollFailure(e)) throw e
            if (failures >= retry.maxConsecutiveRetries) throw TaskPollLostException(taskId, e)
            android.util.Log.w("TaskRepo", "Poll $taskId failed (${e.message}); retry ${failures + 1}/${retry.maxConsecutiveRetries}")
            delay(retry.backoffMs(failures))
            failures++
            continue
        }
        failures = 0
        emit(status)
        if (isTerminalTaskStatus(status.status)) break
        delay(intervalMs)
    }
}

/** How a task poll ended, as the generation screens render it. */
sealed interface TaskPollOutcome {
    data class Completed(val status: TaskStatus) : TaskPollOutcome
    data class Failed(val message: String) : TaskPollOutcome
    data object Cancelled : TaskPollOutcome
    /** The retry budget ran out — show [LOST_CONNECTION_MESSAGE], not a task failure. */
    data object LostConnection : TaskPollOutcome
}

/**
 * Collect [poll] to its end, handing every status to [onStatus], and map the last one
 * to a [TaskPollOutcome]. Every failure becomes an outcome — the crash in
 * A11-settings-generation-01 was this exception escaping a bare launch — EXCEPT
 * cancellation, which is rethrown so `reset()` cancelling the poll job stays a cancel.
 */
suspend fun awaitTaskOutcome(
    poll: Flow<TaskStatus>,
    onStatus: (TaskStatus) -> Unit = {},
): TaskPollOutcome {
    var last: TaskStatus? = null
    try {
        poll.collect { status ->
            last = status
            onStatus(status)
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: TaskPollLostException) {
        return TaskPollOutcome.LostConnection
    } catch (e: Exception) {
        return TaskPollOutcome.Failed(e.message ?: "Task status unavailable")
    }
    val end = last ?: return TaskPollOutcome.Failed("Task status unavailable")
    return when {
        end.status.equals("completed", true) -> TaskPollOutcome.Completed(end)
        end.status.equals("cancelled", true) -> TaskPollOutcome.Cancelled
        end.status.equals("failed", true) -> TaskPollOutcome.Failed(end.error ?: "Generation failed")
        else -> TaskPollOutcome.Failed("Task ended as '${end.status}'")
    }
}
