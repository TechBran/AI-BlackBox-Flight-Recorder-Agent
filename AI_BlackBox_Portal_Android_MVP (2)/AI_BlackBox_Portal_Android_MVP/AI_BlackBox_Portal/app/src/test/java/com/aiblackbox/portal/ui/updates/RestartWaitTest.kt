package com.aiblackbox.portal.ui.updates

import com.aiblackbox.portal.data.model.UpdateState
import com.aiblackbox.portal.data.model.UpdateStateResponse
import com.aiblackbox.portal.data.model.UpdateStatus
import com.aiblackbox.portal.data.repository.UpdateRepository
import com.aiblackbox.portal.ui.updates.RestartWait.TaskVerdict
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Restart detection and confirm wording for the Updates screen (audit rank 43,
 * A13-ui-ops-01 / A13-ui-ops-02 / O16-01).
 *
 * awaitRestart() used to probe /health the moment 'complete' arrived; the
 * backend restarts 2 s later, so the OLD process answered and the log said
 * "Service back online after 0s" ahead of a 60-90 s outage. The wait now
 * compares /health's boot_id. Pure: a scripted probe and a fake clock.
 */
class RestartWaitTest {

    /** Scripted /health: a boot id, "" (no field) or null (down) per probe;
     *  the last entry repeats. sleep() advances the clock. */
    private class Script(vararg probes: String?) {
        private val queue = ArrayDeque(probes.toList())
        private val last = probes.lastOrNull()
        var t = 0L
        val seen = mutableListOf<String?>()
        fun probe(): String? = (if (queue.isEmpty()) last else queue.removeFirst()).also { seen += it }
    }

    private suspend fun Script.await(
        prior: String?,
        timeoutMs: Long = RestartWait.TIMEOUT_MS,
        taskState: (suspend () -> TaskVerdict?)? = null,
    ) = RestartWait.await(
        priorBootId = prior,
        probe = { probe() },
        now = { t },
        sleep = { t += it },
        timeoutMs = timeoutMs,
        taskState = taskState,
    )

    /** A clock-driven box: /health answers boot(t), /update/state task(t). */
    private class Timeline(val boot: (Long) -> String?, val task: (Long) -> TaskVerdict?) {
        var t = 0L
        suspend fun await(prior: String?, timeoutMs: Long = RestartWait.TIMEOUT_MS,
                          maxMs: Long = RestartWait.MAX_MS, probeTask: Boolean = true) = RestartWait.await(
            priorBootId = prior,
            probe = { boot(t) },
            now = { t },
            sleep = { t += it },
            timeoutMs = timeoutMs,
            maxMs = maxMs,
            taskState = if (probeTask) ({ task(t) }) else null,
        )
    }

    @Test
    fun `the old process answering is not a restart`() {
        assertFalse(RestartWait.restartObserved("A", "A", seenDown = true, elapsedMs = 60_000))
        assertTrue(RestartWait.restartObserved("A", "B", seenDown = false, elapsedMs = 0))
    }

    @Test
    fun `a build without boot_id answering after one with it is a restart`() {
        // Rollback to code that predates boot_id.
        assertTrue(RestartWait.restartObserved("A", "", seenDown = false, elapsedMs = 0))
    }

    @Test
    fun `an old backend falls back to seen-down or the grace period`() {
        assertFalse(RestartWait.restartObserved("", "", seenDown = false, elapsedMs = 0))
        assertTrue(RestartWait.restartObserved("", "", seenDown = true, elapsedMs = 0))
        assertTrue(RestartWait.restartObserved("", "", seenDown = false, elapsedMs = RestartWait.GRACE_MS))
        assertTrue("a build that has it is the new process",
            RestartWait.restartObserved("", "B", seenDown = false, elapsedMs = 0))
    }

    @Test
    fun `an unreadable prior id never trusts the first answer`() {
        assertFalse(RestartWait.restartObserved(null, "B", seenDown = false, elapsedMs = 0))
        assertTrue(RestartWait.restartObserved(null, "B", seenDown = true, elapsedMs = 0))
    }

    @Test
    fun `waits through the old process, the outage and the warm-up`() = runTest {
        val s = Script("A", "A", null, null, null, "B")
        assertEquals(RestartWait.Outcome.Restarted(10_000), s.await(prior = "A"))
        assertEquals(6, s.seen.size)
    }

    @Test
    fun `the regression - first 200 right after complete is not back online`() = runTest {
        // Exactly the field failure: 'complete' arrives, the old process still
        // serves /health for ~2 s, then the box goes down for the restart.
        val s = Script("A", null, null, "B")
        val out = s.await(prior = "A")
        assertTrue(out is RestartWait.Outcome.Restarted)
        assertTrue("must not stop at the first 200", out.elapsedMs > 0)
    }

    @Test
    fun `a restart that never happens is reported as not restarted`() = runTest {
        val s = Script("A")
        assertEquals(RestartWait.Outcome.NotRestarted(20_000 + RestartWait.INTERVAL_MS),
            s.await(prior = "A", timeoutMs = 20_000))
    }

    @Test
    fun `a box that never comes back is still down`() = runTest {
        val s = Script(null)
        assertTrue(s.await(prior = "A", timeoutMs = 20_000) is RestartWait.Outcome.StillDown)
    }

    // ── a dropped log stream (A13-ui-ops-06) ────────────────────────────

    @Test
    fun `taskVerdict - running under the lock, restarting once its task finished, else ended`() {
        fun st(inProgress: Boolean, taskId: String? = null, phase: String = "") = UpdateStateResponse(
            inProgress = inProgress, state = taskId?.let { UpdateState(taskId = it, phase = phase) })
        assertEquals(TaskVerdict.Running, RestartWait.taskVerdict(st(true), "update-1"))
        for (phase in listOf("restart_pending", "complete", "rolled_back")) {
            assertEquals(TaskVerdict.Restarting, RestartWait.taskVerdict(st(false, "update-1", phase), "update-1"))
        }
        assertEquals(TaskVerdict.Ended, RestartWait.taskVerdict(st(false, "update-1", "failed"), "update-1"))
        // An older update's "complete" says nothing about this one (refused, or up to date).
        assertEquals(TaskVerdict.Ended, RestartWait.taskVerdict(st(false, "update-0", "complete"), "update-1"))
        assertEquals(TaskVerdict.Ended, RestartWait.taskVerdict(st(false), "update-1"))
    }

    @Test
    fun `GET update-state decodes, including a box that never updated`() {
        val json = Json { ignoreUnknownKeys = true; isLenient = true }
        val busy = json.decodeFromString(UpdateStateResponse.serializer(),
            """{"has_state":true,"state":{"task_id":"update-1","phase":"pip_install"},"is_interrupted":false,"in_progress":true}""")
        assertTrue(busy.inProgress)
        assertEquals("update-1", busy.state?.taskId)
        val fresh = json.decodeFromString(UpdateStateResponse.serializer(),
            """{"has_state":false,"state":null,"is_interrupted":false,"in_progress":false}""")
        assertNull(fresh.state)
    }

    @Test
    fun `the review regression - the lock is free 2 s before the restart, keep waiting`() = runTest {
        // The update completes at 20 s and releases the lock, the restart fires
        // at 22 s, the new process answers at 80 s. Giving up on !inProgress
        // alone ended the wait at 20 s with "the service did not restart".
        val box = Timeline(
            boot = { t -> if (t < 22_000) "OLD" else if (t < 80_000) null else "NEW" },
            task = { t -> if (t < 20_000) TaskVerdict.Running else TaskVerdict.Restarting },
        )
        assertEquals(RestartWait.Outcome.Restarted(80_000), box.await(prior = "OLD"))
    }

    @Test
    fun `give up once the task ended without a restart`() = runTest {
        val box = Timeline(boot = { "A" }, task = { t -> if (t >= 12_000) TaskVerdict.Ended else TaskVerdict.Running })
        assertEquals(RestartWait.Outcome.GaveUp(12_000), box.await(prior = "A"))
    }

    @Test
    fun `a long pip phase keeps the wait going past the timeout`() = runTest {
        val box = Timeline(
            boot = { t -> if (t < 602_000) "A" else if (t < 650_000) null else "B" },
            task = { t -> if (t < 600_000) TaskVerdict.Running else TaskVerdict.Restarting },
        )
        assertEquals(RestartWait.Outcome.Restarted(650_000), box.await(prior = "A"))
    }

    @Test
    fun `an update that never finishes is reported as still running, at the cap`() = runTest {
        val box = Timeline(boot = { "A" }, task = { TaskVerdict.Running })
        val out = box.await(prior = "A", timeoutMs = 20_000, maxMs = 60_000)
        assertTrue(out.toString(), out is RestartWait.Outcome.StillRunning)
        assertTrue(out.elapsedMs in 60_001..62_000)
    }

    @Test
    fun `the label says the update still runs while it does`() {
        assertEquals("Update still running on the box…", RestartWait.stageLabel(200_000, TaskVerdict.Running))
        assertEquals("Restarting service…", RestartWait.stageLabel(5_000, TaskVerdict.Restarting))
        assertEquals("Restarting service…", RestartWait.stageLabel(5_000))
    }

    @Test
    fun `onTick hears the task's verdict`() = runTest {
        val box = Timeline(
            boot = { t -> if (t < 8_000) "A" else "B" },
            task = { t -> if (t < 4_000) TaskVerdict.Running else TaskVerdict.Restarting },
        )
        val ticks = mutableListOf<Pair<Long, TaskVerdict?>>()
        RestartWait.await(
            priorBootId = "A", probe = { box.boot(box.t) }, now = { box.t }, sleep = { box.t += it },
            onTick = { elapsed, verdict -> ticks += elapsed to verdict },
            taskState = { box.task(box.t) },
        )
        assertEquals(listOf(0L to null, 2_000L to TaskVerdict.Running, 4_000L to TaskVerdict.Running,
            6_000L to TaskVerdict.Restarting, 8_000L to TaskVerdict.Restarting), ticks)
    }

    @Test
    fun `without a task probe the timeout is not extended`() = runTest {
        val box = Timeline(boot = { "A" }, task = { TaskVerdict.Running })
        assertTrue(box.await(prior = "A", timeoutMs = 20_000, probeTask = false) is RestartWait.Outcome.NotRestarted)
    }

    @Test
    fun `restartExpected follows the flag and old backends' equal-SHA no-op`() {
        assertFalse(RestartWait.restartExpected(true, false, "a", "b"))
        assertTrue(RestartWait.restartExpected(true, true, "a", "b"))
        assertFalse(RestartWait.restartExpected(false, null, "aaaaaaa", ""))
        val sha = "a".repeat(40)
        assertFalse(RestartWait.restartExpected(true, null, sha, sha))
        assertTrue(RestartWait.restartExpected(true, null, "aaaaaaa", "bbbbbbb"))
    }

    @Test
    fun `parseBootId reads the id, '' without the field, null for non-JSON`() {
        val json = Json { ignoreUnknownKeys = true; isLenient = true }
        assertEquals("k:1:2", UpdateRepository.parseBootId(json, """{"status":"ok","boot_id":"k:1:2"}"""))
        assertEquals("", UpdateRepository.parseBootId(json, """{"status":"ok"}"""))
        assertNull(UpdateRepository.parseBootId(json, "<html>captive portal</html>"))
    }

    @Test
    fun `status decodes dirty_files and older backends default to none`() {
        val json = Json { ignoreUnknownKeys = true; isLenient = true }
        val dirty = json.decodeFromString(UpdateStatus.serializer(),
            """{"git_initialized":true,"commits_behind":1,"dirty_files":["app.py"]}""")
        assertEquals(listOf("app.py"), dirty.dirtyFiles)
        val old = json.decodeFromString(UpdateStatus.serializer(), """{"git_initialized":true}""")
        assertEquals(emptyList<String>(), old.dirtyFiles)
    }

    @Test
    fun `the Install confirm names CLI sessions and the files it will stash`() {
        val clean = UpdateConfirmText.install(UpdateStatus(gitInitialized = true, commitsBehind = 3))
        assertTrue(clean.contains("Install 3 update(s)?"))
        assertFalse(clean.contains("local changes"))
        val dirty = UpdateConfirmText.install(UpdateStatus(
            gitInitialized = true, commitsBehind = 1, activeCliSessions = 2,
            dirtyFiles = listOf("app.py", "a b.txt")))
        assertTrue(dirty.startsWith("WARNING: 2 active CLI agent session(s)"))
        assertTrue(dirty.contains("2 tracked file(s) on this box have local changes (app.py, a b.txt)"))
        assertTrue(dirty.contains("Installing stashes them first"))
    }

    @Test
    fun `the Rollback confirm and the stash retry say what happens`() {
        assertTrue(UpdateConfirmText.rollback(null).startsWith("Roll back to the pre-update tag"))
        val r = UpdateConfirmText.rollback(UpdateStatus(dirtyFiles = listOf("x.py")))
        assertTrue(r.contains("(x.py)") && r.contains("Rolling back stashes them first"))
        val retry = UpdateConfirmText.stashRetry(List(7) { "f$it" }, "Installing")
        assertTrue(retry.contains("f0, f1, f2, f3, f4 and 2 more"))
        assertTrue(retry.endsWith("Stash them and continue?"))
    }
}
