package com.aiblackbox.portal.ui.updates

import com.aiblackbox.portal.data.model.UpdateStateResponse
import com.aiblackbox.portal.data.model.UpdateStatus
import kotlinx.coroutines.delay

/**
 * Restart detection after Install, Rollback and Restart Service (audit rank 43,
 * A13-ui-ops-01). The backend restarts 2 s AFTER it reports success, so the
 * first /health 200 came from the OLD process and the app logged "back online
 * after 0s" ahead of a 60-90 s outage. /health now carries `boot_id`, which
 * changes with every process; the app reads it before the action and waits
 * for a different one. Same rules as waitForRestart in the Portal's
 * updates-manager.js. Pure: the clock, the sleep and the probe are injected.
 *
 * Probe values: a boot id, "" for a 200 from a backend without the field, or
 * null when /health did not answer 200.
 *
 * When the log stream drops mid-update (A13-ui-ops-06), GET /update/state says
 * what THIS task is doing ([taskVerdict]): the update lock is free ~2 s before
 * the restart fires while the old process still answers, so "nothing is in
 * progress" alone is not "no restart is coming".
 */
object RestartWait {
    /** Fallback for a backend without boot_id: accept a 200 only once the
     *  service was seen down, or this long after the wait began. */
    const val GRACE_MS = 10_000L
    const val TIMEOUT_MS = 180_000L
    const val INTERVAL_MS = 2_000L
    /** How long a still-running update can keep the wait going (see [await]). */
    const val MAX_MS = 3_600_000L

    /** What /update/state says about one update task. */
    enum class TaskVerdict {
        /** The update lock is held: it is still working. */
        Running,
        /** It finished and scheduled the restart (coming ~2 s after the lock is free). */
        Restarting,
        /** It failed, was refused or had nothing to do: no restart is coming. */
        Ended,
    }

    fun taskVerdict(state: UpdateStateResponse, taskId: String): TaskVerdict = when {
        state.inProgress -> TaskVerdict.Running
        state.state?.taskId == taskId &&
            state.state.phase in setOf("restart_pending", "complete", "rolled_back") -> TaskVerdict.Restarting
        else -> TaskVerdict.Ended
    }

    /** Has the service restarted since [priorBootId] was read? A null prior
     *  means it could not be read; "" means that backend had no boot_id. */
    fun restartObserved(
        priorBootId: String?,
        bootId: String,
        seenDown: Boolean,
        elapsedMs: Long,
        graceMs: Long = GRACE_MS,
    ): Boolean = when {
        !priorBootId.isNullOrEmpty() -> bootId != priorBootId // "" too: an older build answered
        priorBootId == "" && bootId.isNotEmpty() -> true      // a newer build answered
        else -> seenDown || elapsedMs >= graceMs              // nothing to compare
    }

    /** Does a complete event promise a restart? Older backends omit the flag;
     *  their only no-restart success is "Already up to date" (equal SHAs). */
    fun restartExpected(succeeded: Boolean, restartScheduled: Boolean?, shaBefore: String, shaAfter: String): Boolean =
        restartScheduled ?: (succeeded && shaBefore != shaAfter)

    sealed class Outcome {
        abstract val elapsedMs: Long
        data class Restarted(override val elapsedMs: Long) : Outcome()
        /** The old process kept answering for the whole wait: no restart happened. */
        data class NotRestarted(override val elapsedMs: Long) : Outcome()
        /** Nothing answered by the end of the wait. */
        data class StillDown(override val elapsedMs: Long) : Outcome()
        /** [await]'s taskState said the update ended without a restart. */
        data class GaveUp(override val elapsedMs: Long) : Outcome()
        /** The update was still running when the wait hit its cap. */
        data class StillRunning(override val elapsedMs: Long) : Outcome()
    }

    /**
     * Poll [probe] every [intervalMs] until the service has restarted or
     * [timeoutMs] passes.
     *
     * [taskState], when given (the log stream dropped), is asked each time the
     * OLD process answers. [TaskVerdict.Ended] ends the wait with
     * [Outcome.GaveUp]. [TaskVerdict.Running] keeps it going until [timeoutMs]
     * after the task was last seen running (a pip phase alone can take 10
     * minutes), at most [maxMs]; reaching that cap while the update still runs
     * is [Outcome.StillRunning]. null (no answer) changes nothing. [onTick] hears
     * the elapsed time and the last verdict (null without a probe).
     */
    suspend fun await(
        priorBootId: String?,
        probe: suspend () -> String?,
        now: () -> Long = System::currentTimeMillis,
        sleep: suspend (Long) -> Unit = { delay(it) },
        initiallySeenDown: Boolean = false,
        timeoutMs: Long = TIMEOUT_MS,
        maxMs: Long = MAX_MS,
        intervalMs: Long = INTERVAL_MS,
        graceMs: Long = GRACE_MS,
        onTick: (Long, TaskVerdict?) -> Unit = { _, _ -> },
        taskState: (suspend () -> TaskVerdict?)? = null,
    ): Outcome {
        val start = now()
        var seenDown = initiallySeenDown
        var lastUp = false
        var verdict: TaskVerdict? = null
        var runningAt: Long? = null // elapsed when the task was last seen running
        while (true) {
            val elapsed = now() - start
            val deadline = runningAt?.let { maxOf(timeoutMs, minOf(maxMs, it + timeoutMs)) } ?: timeoutMs
            if (elapsed > deadline) {
                return when {
                    lastUp && verdict == TaskVerdict.Running -> Outcome.StillRunning(elapsed)
                    lastUp -> Outcome.NotRestarted(elapsed)
                    else -> Outcome.StillDown(elapsed)
                }
            }
            onTick(elapsed, verdict)
            val bootId = probe()
            lastUp = bootId != null
            if (bootId == null) {
                seenDown = true
            } else if (restartObserved(priorBootId, bootId, seenDown, elapsed, graceMs)) {
                return Outcome.Restarted(elapsed)
            } else if (taskState != null) {
                verdict = taskState()
                if (verdict == TaskVerdict.Ended) return Outcome.GaveUp(elapsed)
                if (verdict == TaskVerdict.Running) runningAt = elapsed
            }
            sleep(intervalMs)
        }
    }

    /** Progressive copy while waiting (parity with the Portal's audit C5 stages),
     *  unless the update behind a dropped stream is still running. */
    fun stageLabel(elapsedMs: Long, verdict: TaskVerdict? = null): String = when {
        verdict == TaskVerdict.Running -> "Update still running on the box…"
        elapsedMs < 30_000 -> "Restarting service…"
        elapsedMs < 90_000 -> "Rebuilding snapshot index (60–90s typical)…"
        else -> "Still warming. Check logs via journalctl if this hangs."
    }
}

/** Confirm-dialog wording, shared with the Portal's updates-manager.js. */
object UpdateConfirmText {
    private fun dirtyLine(dirty: List<String>, action: String): String {
        val shown = dirty.take(5).joinToString(", ") +
            if (dirty.size > 5) " and ${dirty.size - 5} more" else ""
        return "${dirty.size} tracked file(s) on this box have local changes ($shown). " +
            "$action stashes them first, so they can be restored later with git stash apply."
    }

    fun install(status: UpdateStatus): String = buildString {
        if (status.activeCliSessions > 0) {
            append("WARNING: ${status.activeCliSessions} active CLI agent session(s) will be disconnected.\n\n")
        }
        append("Install ${status.commitsBehind} update(s)?\n\n")
        append("This will restart the BlackBox service. Expect 60–90 seconds of downtime.")
        if (status.dirtyFiles.isNotEmpty()) append("\n\n").append(dirtyLine(status.dirtyFiles, "Installing"))
    }

    fun rollback(status: UpdateStatus?): String = buildString {
        append("Roll back to the pre-update tag and restart the service?\n\n")
        append("User data is unaffected — only application code (and the Python packages that update changed) reverts.")
        val dirty = status?.dirtyFiles.orEmpty()
        if (dirty.isNotEmpty()) append("\n\n").append(dirtyLine(dirty, "Rolling back"))
    }

    fun stashRetry(dirty: List<String>, action: String): String =
        "$action was refused: ${dirtyLine(dirty, action)}\n\nStash them and continue?"

    const val RESTART = "Restart the BlackBox service now?\n\n" +
        "Every connected client is cut off for 60–90 seconds while it comes back."

    const val CANCEL_TASKS = "Cancel every pending and running task, for every operator?\n\n" +
        "Long renders (video, music) and running CLI agents stop too."
}
