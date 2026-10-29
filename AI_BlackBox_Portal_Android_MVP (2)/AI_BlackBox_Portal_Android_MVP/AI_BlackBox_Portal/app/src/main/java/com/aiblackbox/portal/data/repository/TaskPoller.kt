package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskStatus
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import java.io.IOException
import kotlin.coroutines.cancellation.CancellationException

/**
 * The one poller for GET /tasks/status/{id} (audit A11-settings-generation-18, rank 76).
 * Every screen that waits on a box task uses it: the Image, Video and Music screens, Google
 * SSML, Gemini Pro TTS, the Settings voice preview, the chat's media pill and its placeholder
 * resolver, and the overlay's chat. They used to poll three, then five, ways, with different
 * terminal sets, error keys and URL handling, which is where the cancelled-spins-forever,
 * one-blip-crash and relative-URL bugs came from.
 *
 * It owns the terminal set ([isTerminalTaskStatus]), the transient-failure retry
 * ([PollRetryPolicy]), the outcome mapping ([TaskPollOutcome], never an exception except
 * cancellation) and making the result URL absolute against the box ([absolute]): the box
 * stores `/ui/uploads/...`, which an image loader or media player cannot open on its own.
 */
class TaskPoller(
    private val fetch: suspend (taskId: String) -> TaskStatus,
    private val baseUrl: String = "",
    private val retry: PollRetryPolicy = PollRetryPolicy(),
) {
    constructor(api: BlackBoxApi) : this(TaskRepository(api)::getTaskStatus, api.getBaseUrl())

    /**
     * Every status of [taskId] until a terminal one, [intervalMs] apart. Transient failures
     * (a network blip, the box restarting) are retried per the policy instead of ending the
     * flow; an exhausted budget ends it with [TaskPollLostException], any other failure (a
     * 404: the box does not know the task) with that exception.
     */
    fun poll(taskId: String, intervalMs: Long = DEFAULT_INTERVAL_MS): Flow<TaskStatus> =
        pollTaskStatus(taskId, intervalMs, retry) { fetch(taskId) }

    /**
     * Poll [taskId] to its end and say how it ended. Never throws except for cancellation,
     * so a caller's `viewModelScope.launch` cannot crash the app on a dropped connection.
     */
    suspend fun await(
        taskId: String,
        intervalMs: Long = DEFAULT_INTERVAL_MS,
        onStatus: (TaskStatus) -> Unit = {},
    ): TaskPollOutcome = awaitTaskOutcome(poll(taskId, intervalMs), ::absolute, onStatus)

    /** [url] made absolute against the box; null for a missing or blank one. */
    fun absolute(url: String?): String? = absoluteResultUrl(baseUrl, url)

    companion object {
        const val DEFAULT_INTERVAL_MS = 3_000L
    }
}

/**
 * [url] as a full URL: unchanged when it already is one (or no [baseUrl] is known), else
 * joined onto [baseUrl] with exactly one slash. Null for a missing or blank [url].
 */
fun absoluteResultUrl(baseUrl: String, url: String?): String? {
    val u = url?.trim()?.takeIf { it.isNotEmpty() } ?: return null
    if (u.startsWith("http://", ignoreCase = true) || u.startsWith("https://", ignoreCase = true)) return u
    if (baseUrl.isBlank()) return u
    return baseUrl.trimEnd('/') + (if (u.startsWith("/")) u else "/$u")
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
 * The poll loop behind [TaskPoller.poll], over an injectable [fetch] so it is testable
 * without HTTP. Emits every status until a terminal one; a transient [IOException] waits
 * [PollRetryPolicy.backoffMs] and asks again (the emit stays OUTSIDE the try, so a
 * collector's own exception is never mistaken for a poll failure).
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

/** How a task poll ended, as the screens render it. */
sealed interface TaskPollOutcome {
    /** [resultUrl] is [status]'s result URL made absolute against the box ([TaskPoller]). */
    data class Completed(val status: TaskStatus, val resultUrl: String? = status.resultUrl) : TaskPollOutcome
    data class Failed(val message: String) : TaskPollOutcome
    data object Cancelled : TaskPollOutcome
    /** The retry budget ran out — show [LOST_CONNECTION_MESSAGE], not a task failure. */
    data object LostConnection : TaskPollOutcome
}

/**
 * Collect [poll] to its end, handing every status to [onStatus], and map the last one
 * to a [TaskPollOutcome] ([absolutize] makes the completed result URL a full one). Every
 * failure becomes an outcome — the crash in A11-settings-generation-01 was this exception
 * escaping a bare launch — EXCEPT cancellation, which is rethrown so `reset()` cancelling
 * the poll job stays a cancel.
 */
suspend fun awaitTaskOutcome(
    poll: Flow<TaskStatus>,
    absolutize: (String?) -> String? = { it },
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
        end.status.equals("completed", true) -> TaskPollOutcome.Completed(end, absolutize(end.resultUrl))
        end.status.equals("cancelled", true) -> TaskPollOutcome.Cancelled
        end.status.equals("failed", true) -> TaskPollOutcome.Failed(end.error ?: "Generation failed")
        else -> TaskPollOutcome.Failed("Task ended as '${end.status}'")
    }
}
