package com.aiblackbox.portal.data.remote

import java.util.concurrent.CopyOnWriteArrayList

/**
 * (M1.4) Process-wide signal that a REMOTE-CONTROL session is active on this device,
 * plus the instant KILL switch. It is the seam between the action channel (the
 * PRODUCER — [RemoteControlServer]'s `/action` + `/stream` handlers drive
 * [start]/[stop]) and the consent surfaces (the CONSUMERS — [OverlayService]'s
 * "AI is controlling this device" banner and [NotificationListenerFgs]'s fail-safe
 * STOP notification observe it via [addListener]).
 *
 * ## Why a bus (not a direct call)
 * The socket owner ([NotificationListenerFgs]) and the overlay banner
 * ([OverlayService]) are independent components with independent lifecycles; either
 * may be absent (no overlay permission, overlay not running). A tiny shared bus lets
 * the handler signal "session active/ended" once and have EVERY present surface react,
 * with a fail-safe path (the notification) that works even when the overlay can't show.
 *
 * ## The kill switch has teeth
 * [stop] does two things: it clears the active session (every listener hides its
 * banner) AND records the taskId in a BOUNDED killed set (I3), so a stale/subsequent
 * `/action` frame for that same task is REFUSED by the dispatcher
 * ([PhoneActionDispatcher]) instead of resurrecting the session or actuating. It is a
 * SET, not a single slot: killing task B never forgets task A (a single slot would let a
 * later stale frame for A resurrect it). A brand-new taskId starts a fresh session
 * normally. NOTE: "abort" here = every SUBSEQUENT frame for the task is refused; there is
 * no in-flight coroutine to cancel — a `stop()` does not interrupt an actuator call that
 * is already running (the dispatcher also re-checks [isKilled] right after [start] to
 * close the stop-racing window, I3).
 *
 * ## Atomic state (I4)
 * [start] / [stop] / [isKilled] / [current] / [addListener] are `@Synchronized`, and each
 * transition notifies listeners off a CONSISTENT snapshot under the monitor — so the
 * consent banner can never be left visible after a stop under a worker/UI-thread race.
 *
 * ## Purity
 * Framework-free (only `java.util.concurrent`) so the session lifecycle + kill
 * semantics are JVM-unit-tested. The Android surfaces that react to it are the thin,
 * device-verified shells.
 */
object RemoteSessionBus {

    /** One active remote-control session. [startedAtMs] supports a future staleness UI. */
    data class Session(val taskId: String, val operator: String, val startedAtMs: Long)

    /**
     * (M8.2) WHY a task was killed — so the wire signal (and the frontier loop's terminal
     * classification) can tell a user STOP apart from an operator incident-kill.
     *  - [USER_STOP]: the on-device STOP button / notification kill switch (M1.4).
     *  - [OPERATOR_KILL]: the remote incident kill (`POST /kill/{taskId}` / `POST /kill-all`, M8.2).
     */
    enum class KillReason { USER_STOP, OPERATOR_KILL }

    /** Notified whenever the active session changes (starts → non-null; ends/killed → null). */
    fun interface Listener {
        fun onSessionChanged(session: Session?)
    }

    /** Cap on remembered killed task-ids (I3). Bounded so a long-lived process can't grow
     *  this without limit; oldest evicts first (a stale frame that old is not a real risk). */
    private const val MAX_KILLED_TASK_IDS = 64

    /** (M8.2) The stable wire-detail phrases the dispatcher + stream gate emit for a killed
     *  task, keyed on the [KillReason]. The frontier loop's terminal detection matches on these:
     *  "killed by operator" → error_kind=killed; "stopped by user" → error_kind=stopped. */
    const val DETAIL_USER_STOP = "remote control stopped by user"
    const val DETAIL_OPERATOR_KILL = "remote control killed by operator"

    // Guarded by `this` monitor (I4). All reads/writes go through @Synchronized methods so
    // session + killed set + listener notification move as ONE atomic step — the consent
    // banner can never be left visible after a stop under a worker/UI-thread race.
    private var session: Session? = null

    /**
     * (I3) The task-ids the user/operator KILLED → the [KillReason], so their stale frames stay
     * refused — a BOUNDED insertion-ordered MAP (not a single slot). Killing task B must NOT
     * forget task A: with a single slot, a later stale frame for A would resurrect it. Capped at
     * [MAX_KILLED_TASK_IDS] (oldest-first eviction). A new legitimate task has a different id and
     * is unaffected. The value (M8.2) records WHY it was killed so the wire detail distinguishes a
     * user STOP from an operator incident-kill.
     */
    private val killedTaskIds = LinkedHashMap<String, KillReason>()

    private val listeners = CopyOnWriteArrayList<Listener>()

    /** The active session, or null when idle. */
    @Synchronized
    fun current(): Session? = session

    /** Whether a remote-control session is currently active. */
    @Synchronized
    fun isActive(): Boolean = session != null

    /**
     * Whether [taskId] was killed by the user and must not be actuated/resurrected.
     * Blank never matches (so a task-less probe is never treated as killed). Correct across
     * MULTIPLE kills (I3): membership in the bounded killed set, not equality to one slot.
     */
    @Synchronized
    fun isKilled(taskId: String): Boolean = killedContains(taskId)

    /** Caller MUST hold the `this` monitor. Blank never matches. */
    private fun killedContains(taskId: String): Boolean =
        taskId.isNotBlank() && killedTaskIds.containsKey(taskId)

    /**
     * (M8.2) Why [taskId] was killed, or null if it was not killed. Blank never matches. Lets the
     * dispatcher + stream gate emit a wire detail that distinguishes an operator incident-kill
     * ("remote control killed by operator") from a user STOP ("remote control stopped by user").
     */
    @Synchronized
    fun killReason(taskId: String): KillReason? =
        if (taskId.isBlank()) null else killedTaskIds[taskId]

    /**
     * (M8.2) The stable wire DETAIL phrase for a killed [taskId] — the signal the frontier loop's
     * terminal-state detection keys on. OPERATOR_KILL → "…killed by operator" (loop → `killed`);
     * USER_STOP / unknown → "…stopped by user" (loop → `stopped`). Both success=false, no error
     * code (a stop is a user/operator decision, not a failure).
     */
    @Synchronized
    fun killDetail(taskId: String): String = when (killedTaskIds[taskId]) {
        KillReason.OPERATOR_KILL -> DETAIL_OPERATOR_KILL
        else -> DETAIL_USER_STOP
    }

    /**
     * Mark a session active for [taskId] / [operator]. Returns true only on a genuine
     * transition (a new/changed session started), so callers can log the session start
     * once. Two guards keep it well-behaved:
     *  - IDEMPOTENT: re-signalling the SAME active task is a no-op (no listener churn), so
     *    a multi-action loop keeps ONE banner.
     *  - KILL-SAFE (defense in depth): a task the user STOPPED is NEVER resurrected here,
     *    even if a caller forgot to gate on [isKilled] first — the dispatcher checks
     *    [isKilled] too, but this makes the bus self-protecting.
     */
    @Synchronized
    fun start(taskId: String, operator: String, clock: () -> Long = { System.currentTimeMillis() }): Boolean {
        if (killedContains(taskId)) return false
        val existing = session
        if (existing != null && existing.taskId == taskId) return false
        val next = Session(taskId, operator, clock())
        session = next
        notifyListeners(next)   // consistent snapshot, under the monitor (I4)
        return true
    }

    /**
     * The instant KILL. Clears the active session (every surface hides its banner) and
     * records its taskId in the bounded killed set so SUBSEQUENT frames for it are refused
     * (I3). Returns the session that was aborted, or null if none was active. Safe to call
     * repeatedly. NOTE: this refuses future frames for the task — it does NOT cancel an
     * actuator call already in flight (there is no in-flight coroutine to cancel here).
     */
    @Synchronized
    fun stop(): Session? = stopInternal(reason = KillReason.USER_STOP)

    /**
     * (M8.2) Targeted incident kill of a SPECIFIC [taskId] (`POST /kill/{taskId}`). Records the
     * task killed with [reason] (default OPERATOR_KILL) so every subsequent `/action` + `/stream`
     * frame for it is refused REGARDLESS of whether it is the active session — a stale or
     * still-arriving frame for that id can never actuate or resurrect it. If it IS the active
     * session, also clears it + notifies listeners (banner drops) and returns the aborted
     * [Session]; otherwise returns null (still recorded killed). Safe to call repeatedly.
     */
    @Synchronized
    fun stop(taskId: String, reason: KillReason = KillReason.OPERATOR_KILL): Session? {
        if (taskId.isBlank()) return null
        recordKilled(taskId, reason)
        val active = session
        if (active != null && active.taskId == taskId) {
            session = null
            notifyListeners(null)   // consistent snapshot, under the monitor (I4)
            return active
        }
        return null   // recorded killed, but this task was not the active session
    }

    /**
     * (M8.2) Kill ALL in-flight device-control for [operator] (`POST /kill-all`) — the operator
     * incident stop. Returns the count of in-flight sessions aborted. The bus tracks a single
     * active session, so this aborts it iff it belongs to [operator] (returning 1), else 0; a
     * blank [operator] never matches (fail-closed). The aborted task is recorded killed so its
     * stale frames stay refused. Operator-scope is ALSO enforced at the HTTP boundary (authorize).
     */
    @Synchronized
    fun stopAll(operator: String, reason: KillReason = KillReason.OPERATOR_KILL): Int {
        if (operator.isBlank()) return 0
        val active = session
        if (active != null && active.operator == operator) {
            recordKilled(active.taskId, reason)
            session = null
            notifyListeners(null)   // consistent snapshot, under the monitor (I4)
            return 1
        }
        return 0
    }

    /** Caller MUST hold the `this` monitor. Clears + records-killed the active session with
     *  [reason], notifying listeners off a consistent snapshot (I4). Shared by the no-arg
     *  [stop] (USER_STOP). */
    private fun stopInternal(reason: KillReason): Session? {
        val aborted = session
        if (aborted != null) recordKilled(aborted.taskId, reason)
        session = null
        notifyListeners(null)   // consistent snapshot, under the monitor (I4)
        return aborted
    }

    /** Caller MUST hold the `this` monitor. Add to the bounded killed map (I3) with its
     *  [reason], evicting the oldest id once past [MAX_KILLED_TASK_IDS]. Insertion order is
     *  preserved (re-adding an existing id keeps its position but updates the reason), so
     *  eviction is oldest-killed-first. */
    private fun recordKilled(taskId: String, reason: KillReason) {
        killedTaskIds[taskId] = reason
        while (killedTaskIds.size > MAX_KILLED_TASK_IDS) {
            val oldest = killedTaskIds.keys.iterator().next()
            killedTaskIds.remove(oldest)
        }
    }

    /**
     * Register [listener] and immediately deliver the CURRENT state (so a surface that
     * attaches mid-session shows its banner right away). Idempotent per instance.
     */
    @Synchronized
    fun addListener(listener: Listener) {
        if (!listeners.contains(listener)) listeners.add(listener)
        // Deliver the CURRENT state on registration, atomically w.r.t. start/stop (I4), so a
        // surface attaching mid-transition can't miss or double-see the banner. Fail-safe:
        // never let a listener throw.
        runCatching { listener.onSessionChanged(session) }
    }

    @Synchronized
    fun removeListener(listener: Listener) {
        listeners.remove(listener)
    }

    private fun notifyListeners(state: Session?) {
        // Never let one misbehaving surface break the signal to the others.
        for (l in listeners) runCatching { l.onSessionChanged(state) }
    }

    /**
     * TEST-ONLY reset of the global seam so unit tests don't leak session/kill state
     * into one another. Not used in production.
     */
    @Synchronized
    internal fun resetForTest() {
        session = null
        killedTaskIds.clear()
        listeners.clear()
    }
}
