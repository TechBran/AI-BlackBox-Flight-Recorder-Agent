package com.aiblackbox.portal.ui.generation

import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.repository.LOST_CONNECTION_MESSAGE
import com.aiblackbox.portal.data.repository.PollRetryPolicy
import com.aiblackbox.portal.data.repository.TaskPollOutcome
import com.aiblackbox.portal.data.repository.pollTaskStatus
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.IOException

/**
 * A11-settings-generation-01 / -02 — what the Image / Video / Music screens show
 * when a poll ends. The AndroidViewModel can't be built on the plain JVM, so the
 * poll-to-UI-state step is the pure [GenerationViewModel.pollToResult] /
 * [GenerationViewModel.genResultFor] core the instance poller runs.
 */
class GenerationPollTest {

    private fun status(s: String, url: String? = null, err: String? = null) =
        TaskStatus(taskId = "t1", status = s, resultUrl = url, error = err)

    @Test fun `a fake repository that keeps throwing IOException ends as lost connection, not a crash`() = runTest {
        var calls = 0
        val seen = mutableListOf<String>()
        val poll = pollTaskStatus("t1", intervalMs = 1000, retry = PollRetryPolicy()) {
            calls++
            if (calls == 1) status("processing") else throw IOException("Network is unreachable")
        }

        val result = GenerationViewModel.pollToResult(poll) { seen += it.status }

        // LOST, not FAILED: the task is still running on the box (review of
        // A11-settings-generation-01 — FAILED put "Retry" on the button, which
        // submits a NEW paid generation).
        assertEquals(GenState.LOST, result.state)
        assertEquals(LOST_CONNECTION_MESSAGE, result.error)
        assertNull(result.resultUrl)
        assertEquals(listOf("processing"), seen)
    }

    @Test fun `a blip during the poll still reaches the result`() = runTest {
        var calls = 0
        val poll = pollTaskStatus("t1", intervalMs = 1000, retry = PollRetryPolicy()) {
            calls++
            when (calls) {
                1 -> status("processing")
                2 -> throw IOException("Software caused connection abort")
                else -> status("completed", "/ui/uploads/img.png")
            }
        }

        val result = GenerationViewModel.pollToResult(poll)

        assertEquals(GenState.COMPLETED, result.state)
        assertEquals("/ui/uploads/img.png", result.resultUrl)
        assertNull(result.error)
    }

    @Test fun `completed, failed, cancelled and lost each map to their own state`() {
        assertEquals(GenerationViewModel.GenResult(GenState.COMPLETED, "/ui/uploads/a.wav", null),
            GenerationViewModel.genResultFor(TaskPollOutcome.Completed(status("completed", "/ui/uploads/a.wav"))))
        assertEquals(GenerationViewModel.GenResult(GenState.FAILED, null, "quota exhausted"),
            GenerationViewModel.genResultFor(TaskPollOutcome.Failed("quota exhausted")))
        // 'cancelled' (Settings > Cancel Tasks, or a per-task cancel) is terminal: the
        // spinner stops and Generate comes back instead of "Generating... N%" forever.
        assertEquals(GenerationViewModel.GenResult(GenState.CANCELLED, null, null),
            GenerationViewModel.genResultFor(TaskPollOutcome.Cancelled))
        assertEquals(GenerationViewModel.GenResult(GenState.LOST, null, LOST_CONNECTION_MESSAGE),
            GenerationViewModel.genResultFor(TaskPollOutcome.LostConnection))
    }

    @Test fun `only a lost poll is resumed, and it resumes the same task`() {
        // "Check again" re-polls the task the box is still running; every other state's
        // button submits (or is disabled), so it never resumes anything.
        assertEquals("t-video-1", GenerationViewModel.resumableTaskId(GenState.LOST, "t-video-1"))
        for (s in GenState.values().filter { it != GenState.LOST }) {
            assertNull(s.name, GenerationViewModel.resumableTaskId(s, "t-video-1"))
        }
        assertNull(GenerationViewModel.resumableTaskId(GenState.LOST, null))
    }
}
