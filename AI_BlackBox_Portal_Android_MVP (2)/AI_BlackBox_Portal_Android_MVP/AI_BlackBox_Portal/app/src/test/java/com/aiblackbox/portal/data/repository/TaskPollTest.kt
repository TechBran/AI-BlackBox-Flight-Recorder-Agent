package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskStatus
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.IOException
import java.net.SocketTimeoutException

/**
 * A11-settings-generation-01 / -02 — media polling.
 *
 * A single IOException from GET /tasks/status (the phone leaving Wi-Fi or the
 * tailnet for a second during a 5-20 minute video) used to escape the poll flow
 * and, in GenerationViewModel's bare `viewModelScope.launch`, kill the process.
 * The poll now retries transient failures with a bounded backoff and resets the
 * budget after every good reply; only an exhausted budget ends it, as a
 * [TaskPollLostException] the UIs render as "the task keeps running on the server".
 */
class TaskPollTest {
    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi

    @Before fun setUp() {
        server = MockWebServer(); server.start()
        api = BlackBoxApi(server.url("").toString().trimEnd('/'))
    }
    @After fun tearDown() = server.close()

    private fun status(s: String, url: String? = null, err: String? = null) =
        TaskStatus(taskId = "t1", status = s, resultUrl = url, error = err)

    private fun statusJson(s: String) = """{"task_id":"t1","task_type":"video","status":"$s","progress":0}"""

    /** A fake /tasks/status: each call runs the next scripted reply. */
    private class ScriptedFetch(vararg replies: () -> TaskStatus) {
        private val queue = ArrayDeque(replies.toList())
        var calls = 0
        suspend fun next(): TaskStatus { calls++; return queue.removeFirst()() }
    }

    private fun blip(): TaskStatus = throw IOException("Software caused connection abort")

    // ── Through the real repository + HTTP client ──

    @Test fun `TaskRepository pollTask rides out a 503 from the box`() = runTest {
        server.enqueue(MockResponse.Builder().code(503).body("""{"detail":"warming up"}""").build())
        server.enqueue(MockResponse.Builder().body(statusJson("completed")).build())

        val seen = TaskRepository(api).pollTask("t1", intervalMs = 10).toList()

        assertEquals(listOf("completed"), seen.map { it.status })
        assertEquals(2, server.requestCount)
    }

    @Test fun `a 404 is an answer, not a blip - it ends the poll without retrying`() = runTest {
        server.enqueue(MockResponse.Builder().code(404).body("""{"detail":"Task not found"}""").build())

        try {
            TaskRepository(api).pollTask("t1", intervalMs = 10).toList()
            fail("expected the 404 to end the poll")
        } catch (e: ApiHttpException) {
            assertEquals(404, e.code)
        }
        assertEquals(1, server.requestCount)
    }

    // ── The poll loop against a fake fetch ──

    @Test fun `one transport blip mid-poll is retried, not thrown`() = runTest {
        val fetch = ScriptedFetch({ status("processing") }, { blip() }, { status("completed", "/ui/uploads/a.png") })

        val seen = pollTaskStatus("t1", intervalMs = 1000, retry = PollRetryPolicy()) { fetch.next() }.toList()

        assertEquals(listOf("processing", "completed"), seen.map { it.status })
        assertEquals(3, fetch.calls)
    }

    @Test fun `an exhausted retry budget ends as TaskPollLostException after bounded backoff`() = runTest {
        val policy = PollRetryPolicy()
        var calls = 0
        try {
            pollTaskStatus("t1", intervalMs = 1000, retry = policy) {
                calls++
                throw SocketTimeoutException("timeout")
            }.toList()
            fail("expected the poll to give up")
        } catch (e: TaskPollLostException) {
            assertEquals("t1", e.taskId)
            assertTrue(e.cause is SocketTimeoutException)
        }
        // The first try plus every retry the budget allows — then it stops.
        assertEquals(policy.maxConsecutiveRetries + 1, calls)
        // Bounded, growing waits: 2s 4s 8s 16s 30s 30s by default (~90s, long enough
        // for the box's service restart to come back).
        val expectedWait = (0 until policy.maxConsecutiveRetries).sumOf { policy.backoffMs(it) }
        assertEquals(90_000L, expectedWait)
        assertEquals(expectedWait, testScheduler.currentTime)
    }

    @Test fun `the retry budget resets after every good reply`() = runTest {
        val policy = PollRetryPolicy(maxConsecutiveRetries = 2, initialBackoffMs = 100, maxBackoffMs = 100)
        val fetch = ScriptedFetch(
            { blip() }, { blip() }, { status("processing") },
            { blip() }, { blip() }, { status("processing") },
            { blip() }, { blip() }, { status("completed") },
        )

        val seen = pollTaskStatus("t1", intervalMs = 10, retry = policy) { fetch.next() }.toList()

        assertEquals(listOf("processing", "processing", "completed"), seen.map { it.status })
    }

    @Test fun `cancelled is terminal - the poll stops asking`() = runTest {
        val fetch = ScriptedFetch({ status("processing") }, { status("cancelled") }, { fail("polled past cancelled"); status("x") })

        val seen = pollTaskStatus("t1", intervalMs = 10, retry = PollRetryPolicy()) { fetch.next() }.toList()

        assertEquals(listOf("processing", "cancelled"), seen.map { it.status })
        assertEquals(2, fetch.calls)
    }

    @Test fun `transient means transport errors and 5xx 408 429, never other 4xx`() {
        assertTrue(isTransientPollFailure(IOException("reset")))
        assertTrue(isTransientPollFailure(SocketTimeoutException("timeout")))
        assertTrue(isTransientPollFailure(ApiHttpException("HTTP 502", 502)))
        assertTrue(isTransientPollFailure(ApiHttpException("HTTP 408", 408)))
        assertTrue(isTransientPollFailure(ApiHttpException("HTTP 429", 429)))
        assertTrue(!isTransientPollFailure(ApiHttpException("Task not found", 404)))
        assertTrue(!isTransientPollFailure(ApiHttpException("bad", 400)))
    }

    // ── How a poll ended, as the generation screens render it ──

    @Test fun `outcome - completed carries the final status`() = runTest {
        val outcome = awaitTaskOutcome(flow { emit(status("processing")); emit(status("completed", "/ui/uploads/v.mp4")) })
        assertTrue(outcome is TaskPollOutcome.Completed)
        assertEquals("/ui/uploads/v.mp4", (outcome as TaskPollOutcome.Completed).status.resultUrl)
    }

    @Test fun `outcome - failed carries the server's error, with a fallback`() = runTest {
        assertEquals(TaskPollOutcome.Failed("quota exhausted"),
            awaitTaskOutcome(flow { emit(status("failed", err = "quota exhausted")) }))
        assertEquals(TaskPollOutcome.Failed("Generation failed"),
            awaitTaskOutcome(flow { emit(status("FAILED")) }))
    }

    @Test fun `outcome - cancelled is its own terminal state`() = runTest {
        assertEquals(TaskPollOutcome.Cancelled, awaitTaskOutcome(flow { emit(status("cancelled")) }))
    }

    @Test fun `outcome - an exhausted budget is LostConnection, not a crash`() = runTest {
        val outcome = awaitTaskOutcome(
            pollTaskStatus("t1", intervalMs = 10, retry = PollRetryPolicy()) { throw IOException("Network is unreachable") })
        assertEquals(TaskPollOutcome.LostConnection, outcome)
        assertEquals("Lost connection; the task keeps running on the server", LOST_CONNECTION_MESSAGE)
    }

    @Test fun `outcome - a non-transient error is a failure, not a crash`() = runTest {
        val outcome = awaitTaskOutcome(flow<TaskStatus> { throw ApiHttpException("Task not found", 404) })
        assertEquals(TaskPollOutcome.Failed("Task not found"), outcome)
    }

    @Test fun `outcome - every status reaches the progress callback`() = runTest {
        val seen = mutableListOf<String>()
        awaitTaskOutcome(flow { emit(status("pending")); emit(status("processing")); emit(status("completed")) }) {
            seen += it.status
        }
        assertEquals(listOf("pending", "processing", "completed"), seen)
    }

    @Test fun `cancelling the poll job cancels - it is never reported as an outcome`() = runTest {
        var outcome: TaskPollOutcome? = null
        val job = launch { outcome = awaitTaskOutcome(flow { emit(status("processing")); awaitCancellation() }) }
        advanceUntilIdle()
        job.cancel()
        job.join()
        assertTrue(job.isCancelled)
        assertNull(outcome)
    }
}
