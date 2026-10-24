package com.aiblackbox.portal.ui.updates

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
 */
object RestartWait {
    /** Fallback for a backend without boot_id: accept a 200 only once the
     *  service was seen down, or this long after the wait began. */
    const val GRACE_MS = 10_000L
    const val TIMEOUT_MS = 180_000L
    const val INTERVAL_MS = 2_000L

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
        /** [await]'s giveUp said no restart is coming (the update ended without one). */
        data class GaveUp(override val elapsedMs: Long) : Outcome()
    }

    /**
     * Poll [probe] every [intervalMs] until the service has restarted or
     * [timeoutMs] passes. [giveUp] is asked, after the grace period, each time
     * the OLD process answers; true ends the wait with [Outcome.GaveUp].
     */
    suspend fun await(
        priorBootId: String?,
        probe: suspend () -> String?,
        now: () -> Long = System::currentTimeMillis,
        sleep: suspend (Long) -> Unit = { delay(it) },
        initiallySeenDown: Boolean = false,
        timeoutMs: Long = TIMEOUT_MS,
        intervalMs: Long = INTERVAL_MS,
        graceMs: Long = GRACE_MS,
        onTick: (Long) -> Unit = {},
        giveUp: (suspend () -> Boolean)? = null,
    ): Outcome {
        val start = now()
        var seenDown = initiallySeenDown
        var lastUp = false
        while (true) {
            val elapsed = now() - start
            if (elapsed > timeoutMs) {
                return if (lastUp) Outcome.NotRestarted(elapsed) else Outcome.StillDown(elapsed)
            }
            onTick(elapsed)
            val bootId = probe()
            lastUp = bootId != null
            if (bootId == null) {
                seenDown = true
            } else if (restartObserved(priorBootId, bootId, seenDown, elapsed, graceMs)) {
                return Outcome.Restarted(elapsed)
            } else if (giveUp != null && elapsed >= graceMs && giveUp()) {
                return Outcome.GaveUp(elapsed)
            }
            sleep(intervalMs)
        }
    }

    /** Progressive copy while waiting (parity with the Portal's audit C5 stages). */
    fun stageLabel(elapsedMs: Long): String = when {
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
