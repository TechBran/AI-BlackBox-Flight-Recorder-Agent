package com.aiblackbox.portal.ui.chat

import android.util.Log
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.data.repository.TaskPollOutcome
import com.aiblackbox.portal.data.repository.TaskPoller
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.cancellation.CancellationException

private const val TAG = "TaskDiscovery"

/**
 * The chat's box tasks, out of ChatViewModel (audit A04-chat-viewmodel-17, rank 76): the
 * 2 s /tasks/list discovery that feeds the TaskPanel pills, the per-task pill poll, and
 * the resolver that swaps a reply's media placeholders for the finished image, video or
 * music. Every poll goes through the one [TaskPoller].
 *
 * The chat's messages stay ChatViewModel's: a finished or dropped media task reaches them
 * through [onMediaResolved] / [onMediaDropped] (see [resolveMediaEntry] and
 * [dropMediaEntry] for what they do to the list). [listTasks] and [poller] answer null
 * until the box is known (ChatViewModel.initialize).
 */
class TaskDiscovery(
    private val scope: CoroutineScope,
    private val listTasks: suspend () -> List<TaskStatus>?,
    private val poller: () -> TaskPoller?,
    private val onMediaResolved: (taskId: String, taskType: String?, url: String) -> Unit,
    private val onMediaDropped: (taskId: String) -> Unit,
    private val discoveryIntervalMs: Long = DISCOVERY_INTERVAL_MS,
) {
    // Media task IDs for polling (image/video/music generation)
    private val _pendingMediaTasks = MutableStateFlow<List<String>>(emptyList())
    val pendingMediaTasks: StateFlow<List<String>> = _pendingMediaTasks.asStateFlow()

    // Active tasks being polled (for TaskPanel display)
    private val _activeTasks = MutableStateFlow<List<TaskStatus>>(emptyList())
    val activeTasks: StateFlow<List<TaskStatus>> = _activeTasks.asStateFlow()

    // Task completion events (for notifications)
    private val _taskCompletedEvent = MutableSharedFlow<TaskStatus>(extraBufferCapacity = 5)
    val taskCompletedEvent: SharedFlow<TaskStatus> = _taskCompletedEvent.asSharedFlow()

    /** Track which tasks are being actively polled by the resolver (prevent duplicates) */
    private val resolverPolling = mutableSetOf<String>()

    /** Poll /tasks/list every 2s to discover and update tasks — matches Portal TaskManager. */
    fun startDiscoveryLoop() {
        scope.launch {
            while (true) {
                try {
                    // No box yet, or no task list in its answer: try again next tick.
                    val serverTasks = listTasks() ?: run { delay(discoveryIntervalMs); continue }
                    val step = discoveryStep(_activeTasks.value, serverTasks)
                    _activeTasks.value = step.withDiscovered
                    if (step.discovered.isNotEmpty()) {
                        // startTaskPolling early-returns here BY DESIGN — the task was just
                        // added to _activeTasks on the line above, so its guard
                        // (`if (_activeTasks.value.any { it.taskId == taskId }) return`) is
                        // already true; the /tasks/status poll never runs and this discovery
                        // loop is the SOLE panel feed. Do NOT reorder this after the add, or
                        // drop that guard, without accepting a per-2s label flicker
                        // (e.g. "CLI Agent" <-> "Claude Code") from the poll object then ALSO
                        // writing _activeTasks with a differently-resolved TaskStatus. (G3-T13)
                        step.discovered.forEach { startTaskPolling(it.taskId, it.taskType) }
                        Log.d(TAG, "Discovered ${step.discovered.size} new active tasks")
                    }
                    _activeTasks.value = step.retained(_activeTasks.value)
                } catch (e: CancellationException) {
                    throw e
                } catch (_: Exception) {
                    // Silent — task discovery is best-effort
                }
                delay(discoveryIntervalMs)
            }
        }
    }

    /** Remove a completed media task from pending list */
    fun completeMediaTask(taskId: String) {
        _pendingMediaTasks.value = _pendingMediaTasks.value - taskId
        _activeTasks.value = _activeTasks.value.filter { it.taskId != taskId }
    }

    /**
     * Start polling a task. Called when media tasks arrive from SSE or generation screens.
     * Matches Portal's TaskManager 3-second polling loop.
     */
    fun startTaskPolling(taskId: String, taskType: String? = null) {
        val p = poller() ?: return
        // Skip if already tracking this task
        if (_activeTasks.value.any { it.taskId == taskId }) return
        // Add to pending
        if (taskId !in _pendingMediaTasks.value) {
            _pendingMediaTasks.value = _pendingMediaTasks.value + taskId
        }
        // Add initial status to active tasks
        _activeTasks.value = _activeTasks.value + TaskStatus(taskId = taskId, taskType = taskType, status = "pending")

        scope.launch {
            try {
                p.poll(taskId, intervalMs = PILL_INTERVAL_MS).collect { status ->
                    // Update active tasks list
                    _activeTasks.value = _activeTasks.value.map {
                        if (it.taskId == taskId) status else it
                    }

                    val isDone = status.status.equals("completed", true)
                    val isFailed = status.status.equals("failed", true)
                    // 'cancelled' is terminal (G2-T8): must end the poll and clean
                    // up the placeholder, or the flow collects forever.
                    val isCancelled = status.status.equals("cancelled", true)
                    if (isDone || isFailed || isCancelled) {
                        // Emit completion event for notifications — but NOT for a
                        // cancel: it is operator-initiated, so a push notification
                        // about the thing they just stopped is noise (and the
                        // consumer would render any non-completed status as
                        // "Failed"). Matches the backend suppress. G2-T8.
                        if (!isCancelled) _taskCompletedEvent.tryEmit(status)

                        // Replace placeholder with actual media in the message
                        val url = p.absolute(status.resultUrl)
                        if (isDone && url != null) {
                            onMediaResolved(taskId, taskType, url)
                        } else {
                            // Failed or cancelled — just remove the placeholder
                            onMediaDropped(taskId)
                        }

                        delay(2000)
                        completeMediaTask(taskId)
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "Task polling failed for $taskId: ${e.message}")
                completeMediaTask(taskId)
            }
        }
    }

    /**
     * Resolve every media placeholder in [messages] (prior sessions' and this one's):
     * each unresolved task is polled to its end through the shared [TaskPoller]. Completed
     * with a result swaps the placeholder for the media; failed, cancelled, unknown to the
     * box (404) or completed with nothing to show drops it. A lost connection (the poll's
     * retries ran out) or the 25-minute ceiling keeps it: the task may still finish, and
     * the next history load or turn resolves it. One network blip used to drop it.
     *
     * This is the single source of truth for media task resolution — it does NOT
     * delegate to startTaskPolling().
     */
    fun resolveUnresolvedMediaTasks(messages: List<UiMessage>) {
        val p = poller() ?: return
        val unresolved = unresolvedMediaTasks(messages, resolverPolling)
        if (unresolved.isEmpty()) return
        Log.d(TAG, "Found ${unresolved.size} unresolved media tasks — resolving")

        for ((taskId, taskType) in unresolved) {
            resolverPolling.add(taskId)
            scope.launch {
                try {
                    val outcome = withTimeoutOrNull(RESOLVER_CEILING_MS) {
                        p.await(taskId, intervalMs = RESOLVER_INTERVAL_MS)
                    }
                    when (outcome) {
                        is TaskPollOutcome.Completed -> {
                            val url = outcome.resultUrl
                            if (url != null) {
                                onMediaResolved(taskId, taskType, url)
                                Log.d(TAG, "Resolved media task $taskId → $url")
                            } else {
                                onMediaDropped(taskId)
                                Log.w(TAG, "Media task $taskId completed with no result — removed placeholder")
                            }
                        }
                        is TaskPollOutcome.Failed, TaskPollOutcome.Cancelled -> {
                            // 'cancelled' is terminal like failed (G2-T8): drop the
                            // placeholder, or a cancelled media task polls for the full 25 min.
                            onMediaDropped(taskId)
                            Log.w(TAG, "Media task $taskId ended ($outcome) — removed placeholder")
                        }
                        TaskPollOutcome.LostConnection -> Log.w(TAG, "Media task $taskId: connection lost, placeholder kept")
                        null -> Log.w(TAG, "Media task $taskId still running after the resolver's ceiling")
                    }
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    Log.e(TAG, "Resolver failed for $taskId: ${e.message}")
                } finally {
                    resolverPolling.remove(taskId)
                }
            }
        }
    }

    /** One discovery tick: what [existing] becomes from the box's [serverRows]. */
    internal class DiscoveryStep(val withDiscovered: List<TaskStatus>, val discovered: List<TaskStatus>, private val serverActiveIds: Set<String>) {
        /** Drop the tasks the box no longer reports as active (terminal ones show briefly). */
        fun retained(tasks: List<TaskStatus>): List<TaskStatus> = tasks.filter { task ->
            task.taskId in serverActiveIds ||
                task.status.equals("completed", true) ||
                task.status.equals("failed", true) ||
                task.status.equals("cancelled", true)   // terminal — show briefly (G2-T8)
        }
    }

    companion object {
        const val DISCOVERY_INTERVAL_MS = 2_000L
        const val PILL_INTERVAL_MS = 1_500L
        const val RESOLVER_INTERVAL_MS = 3_000L
        /** A video can take ~20 minutes; the resolver stops asking after 25 (500 polls of 3 s). */
        const val RESOLVER_CEILING_MS = 25 * 60 * 1_000L

        /**
         * The pure merge of one discovery tick: [existing] pills take the box's newer rows
         * for themselves, and active rows the panel does not show yet are [DiscoveryStep.discovered]
         * and appended. Only pending/processing rows count as active.
         */
        internal fun discoveryStep(existing: List<TaskStatus>, serverRows: List<TaskStatus>): DiscoveryStep {
            val existingIds = existing.map { it.taskId }.toSet()
            val active = serverRows.filter { it.status in listOf("pending", "processing") }
            // Update progress on existing active tasks
            val updated = existing.map { e -> active.find { it.taskId == e.taskId } ?: e }
            // Add newly discovered tasks
            val discovered = active.filter { it.taskId !in existingIds }
            return DiscoveryStep(updated + discovered, discovered, active.map { it.taskId }.toSet())
        }

        /**
         * The (task id, type) of every media placeholder in [messages] not already in
         * [polling]. A placeholder is "image:<id>", "video:<id>", "music:<id>" or a bare id.
         */
        internal fun unresolvedMediaTasks(messages: List<UiMessage>, polling: Set<String>): List<Pair<String, String?>> {
            val out = mutableListOf<Pair<String, String?>>()
            for (msg in messages) {
                for (entry in msg.mediaTasks) {
                    val colonIdx = entry.indexOf(':')
                    val taskType = if (colonIdx > 0) entry.substring(0, colonIdx) else null
                    val rawId = if (colonIdx > 0) entry.substring(colonIdx + 1) else entry
                    // Skip if already being polled by this resolver
                    if (rawId !in polling) out.add(rawId to taskType)
                }
            }
            return out
        }

        private val IMAGE_URL = Regex(".*\\.(png|jpg|jpeg|webp|gif)$", RegexOption.IGNORE_CASE)

        /**
         * [messages] with [taskId]'s placeholder replaced by its media at [url] (absolute):
         * an image joins the message's images (ChatBubble renders them), a video or audio
         * URL is appended to its content for the inline media extractor. Null when no
         * message holds the placeholder.
         */
        internal fun resolveMediaEntry(
            messages: List<UiMessage>,
            taskId: String,
            taskType: String?,
            url: String,
        ): List<UiMessage>? {
            // The task entry in mediaTasks is prefixed with type: "image:taskId"
            val prefixedEntries = listOf("image:$taskId", "video:$taskId", "music:$taskId", taskId)
            for (i in messages.indices) {
                val msg = messages[i]
                val matchingEntry = msg.mediaTasks.firstOrNull { entry ->
                    prefixedEntries.any { prefix -> entry == prefix || entry.endsWith(taskId) }
                } ?: continue
                // Determine media type from the prefix or taskType
                val isImage = matchingEntry.startsWith("image:") ||
                    taskType?.contains("image", true) == true ||
                    url.matches(IMAGE_URL)
                val remaining = msg.mediaTasks.filter { it != matchingEntry }
                val updated = if (isImage) {
                    msg.copy(images = msg.images + url, mediaTasks = remaining)
                } else {
                    // Video/audio — append URL to content so inline media extractor renders it
                    msg.copy(content = msg.content + "\n$url\n", mediaTasks = remaining)
                }
                return messages.toMutableList().also { it[i] = updated }
            }
            return null
        }

        /** [messages] without [taskId]'s placeholder; null when no message holds it. */
        internal fun dropMediaEntry(messages: List<UiMessage>, taskId: String): List<UiMessage>? {
            for (i in messages.indices) {
                val msg = messages[i]
                val matchingEntry = msg.mediaTasks.firstOrNull { it.endsWith(taskId) || it == taskId } ?: continue
                return messages.toMutableList().also {
                    it[i] = msg.copy(mediaTasks = msg.mediaTasks.filter { e -> e != matchingEntry })
                }
            }
            return null
        }
    }
}
