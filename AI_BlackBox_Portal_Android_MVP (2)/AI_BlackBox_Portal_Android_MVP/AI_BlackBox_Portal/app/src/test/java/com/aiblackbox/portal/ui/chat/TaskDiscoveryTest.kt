package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.data.repository.TaskPoller
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * The chat's box tasks, out of ChatViewModel (audit A04-chat-viewmodel-17, rank 76):
 * discovery's merge, the placeholder resolver and the message transforms, driven through
 * a fake /tasks/status and a fake /tasks/list under virtual time.
 */
class TaskDiscoveryTest {

    private fun task(id: String, status: String, url: String? = null, type: String? = "image", progress: Int = 0) =
        TaskStatus(taskId = id, taskType = type, status = status, resultUrl = url, progress = progress)

    /** A fake GET /tasks/status/{id}: each call runs the next scripted reply for that id. */
    private class FakeStatus(vararg script: Pair<String, () -> TaskStatus>) {
        private val queues = script.groupBy({ it.first }, { it.second }).mapValues { ArrayDeque(it.value) }
        val calls = mutableListOf<String>()
        suspend fun fetch(id: String): TaskStatus {
            calls += id
            val q = queues[id] ?: throw ApiHttpException("Task not found", 404)
            return (if (q.size > 1) q.removeFirst() else q.first())()
        }
    }

    private class Sinks {
        val resolved = mutableListOf<Triple<String, String?, String>>()
        val dropped = mutableListOf<String>()
    }

    /**
     * A discovery over [status]. Its coroutines run in the test's own scope by default, so
     * advanceUntilIdle runs them to the end; [background] (the discovery loop, a poll that
     * never ends) puts them in backgroundScope, which advanceUntilIdle does not wait for.
     */
    private fun TestScope.discovery(
        status: FakeStatus,
        sinks: Sinks,
        background: Boolean = false,
        list: suspend () -> List<TaskStatus>? = { emptyList() },
    ) = TaskDiscovery(
        scope = if (background) backgroundScope else this,
        listTasks = list,
        poller = { TaskPoller(status::fetch, baseUrl = "http://box:9091") },
        onMediaResolved = { id, type, url -> sinks.resolved += Triple(id, type, url) },
        onMediaDropped = { id -> sinks.dropped += id },
    )

    private fun reply(vararg entries: String) = UiMessage(role = "assistant", content = "here", mediaTasks = entries.toList())

    // ── the placeholder resolver ─────────────────────────────────────────────

    @Test fun `a finished image resolves with its absolute URL`() = runTest {
        val sinks = Sinks()
        val status = FakeStatus(
            "t1" to { task("t1", "processing") },
            "t1" to { task("t1", "completed", "/ui/uploads/a.png") },
        )
        discovery(status, sinks).resolveUnresolvedMediaTasks(listOf(reply("image:t1")))
        advanceUntilIdle()

        assertEquals(listOf(Triple("t1", "image", "http://box:9091/ui/uploads/a.png")), sinks.resolved)
        assertTrue(sinks.dropped.isEmpty())
    }

    @Test fun `one network blip no longer drops the placeholder`() = runTest {
        val sinks = Sinks()
        val status = FakeStatus(
            "t1" to { throw IOException("Software caused connection abort") },
            "t1" to { task("t1", "completed", "/ui/uploads/v.mp4", type = "video") },
        )
        discovery(status, sinks).resolveUnresolvedMediaTasks(listOf(reply("video:t1")))
        advanceUntilIdle()

        assertTrue("the blip used to remove it", sinks.dropped.isEmpty())
        assertEquals("http://box:9091/ui/uploads/v.mp4", sinks.resolved.single().third)
    }

    @Test fun `failed, cancelled and unknown tasks drop their placeholder`() = runTest {
        val sinks = Sinks()
        val status = FakeStatus("f" to { task("f", "failed") }, "c" to { task("c", "cancelled") })
        discovery(status, sinks).resolveUnresolvedMediaTasks(listOf(reply("image:f", "music:c", "image:gone")))
        advanceUntilIdle()

        assertEquals(setOf("f", "c", "gone"), sinks.dropped.toSet())
        assertTrue(sinks.resolved.isEmpty())
    }

    @Test fun `a lost connection keeps the placeholder, the task may still finish`() = runTest {
        val sinks = Sinks()
        val status = FakeStatus("t1" to { throw IOException("Network is unreachable") })
        discovery(status, sinks).resolveUnresolvedMediaTasks(listOf(reply("image:t1")))
        advanceUntilIdle()

        assertTrue(sinks.dropped.isEmpty())
        assertTrue(sinks.resolved.isEmpty())
    }

    @Test fun `a task already being resolved is not polled twice`() = runTest {
        val sinks = Sinks()
        val status = FakeStatus("t1" to { task("t1", "processing") })
        val d = discovery(status, sinks, background = true)
        d.resolveUnresolvedMediaTasks(listOf(reply("image:t1")))
        runCurrent()
        d.resolveUnresolvedMediaTasks(listOf(reply("image:t1")))
        advanceTimeBy(10_000)
        // One poll loop: a poll every 3 s since the first look at t=0.
        assertEquals(4, status.calls.size)
    }

    // ── discovery ────────────────────────────────────────────────────────────

    @Test fun `a discovery tick updates the pills, appends new active tasks and drops ended ones`() {
        val existing = listOf(task("a", "processing", progress = 10), task("gone", "processing"))
        val rows = listOf(
            task("a", "processing", progress = 60),
            task("new", "pending"),
            task("old", "completed"),
        )
        val step = TaskDiscovery.discoveryStep(existing, rows)

        assertEquals(listOf("new"), step.discovered.map { it.taskId })
        assertEquals(listOf("a", "gone", "new"), step.withDiscovered.map { it.taskId })
        assertEquals(60, step.withDiscovered.first().progress)
        // "gone" is no longer active on the box; only active rows count, so "old" is not added.
        assertEquals(listOf("a", "new"), step.retained(step.withDiscovered).map { it.taskId })
        // A pill the box last reported as ended still shows briefly.
        assertEquals(listOf("x"), step.retained(listOf(task("x", "cancelled"))).map { it.taskId })
    }

    @Test fun `the loop feeds the panel from the box's rows and skips a tick with no answer`() = runTest {
        val sinks = Sinks()
        val answers = ArrayDeque(listOf<List<TaskStatus>?>(
            listOf(task("a", "processing", progress = 5)),
            null,
            listOf(task("a", "processing", progress = 50)),
            emptyList(),
        ))
        val d = discovery(FakeStatus(), sinks, background = true) {
            if (answers.isEmpty()) emptyList() else answers.removeFirst()
        }
        d.startDiscoveryLoop()

        runCurrent()
        assertEquals(listOf(5), d.activeTasks.value.map { it.progress })
        advanceTimeBy(2_000); runCurrent()
        assertEquals("a tick without a task list changes nothing", listOf(5), d.activeTasks.value.map { it.progress })
        advanceTimeBy(2_000); runCurrent()
        assertEquals(listOf(50), d.activeTasks.value.map { it.progress })
        advanceTimeBy(2_000); runCurrent()
        assertTrue(d.activeTasks.value.isEmpty())
        // Discovery is the panel's only feed: its pills never start a /tasks/status poll.
        assertTrue(d.pendingMediaTasks.value.isEmpty())
    }

    // ── message transforms ───────────────────────────────────────────────────

    @Test fun `an image joins the images, video and audio are appended to the content`() {
        val msgs = listOf(UiMessage(role = "user", content = "make it"), reply("image:t1", "video:t2", "t3"))

        val withImage = TaskDiscovery.resolveMediaEntry(msgs, "t1", "image", "http://box/ui/uploads/a.png")!!
        assertEquals(listOf("http://box/ui/uploads/a.png"), withImage[1].images)
        assertEquals(listOf("video:t2", "t3"), withImage[1].mediaTasks)

        val withVideo = TaskDiscovery.resolveMediaEntry(withImage, "t2", "video", "http://box/ui/uploads/v.mp4")!!
        assertEquals("here\nhttp://box/ui/uploads/v.mp4\n", withVideo[1].content)
        assertEquals(listOf("t3"), withVideo[1].mediaTasks)

        // A bare id whose URL looks like an image is still an image.
        val bare = TaskDiscovery.resolveMediaEntry(withVideo, "t3", null, "http://box/ui/uploads/b.webp")!!
        assertEquals(2, bare[1].images.size)
        assertNull(TaskDiscovery.resolveMediaEntry(bare, "nope", null, "http://x"))
    }

    @Test fun `dropping removes only that placeholder`() {
        val msgs = listOf(reply("image:t1", "music:t2"))
        assertEquals(listOf("music:t2"), TaskDiscovery.dropMediaEntry(msgs, "t1")!![0].mediaTasks)
        assertNull(TaskDiscovery.dropMediaEntry(msgs, "zzz"))
    }

    @Test fun `unresolved placeholders are read with their type, skipping those in flight`() {
        val msgs = listOf(reply("image:t1", "t2"), reply("music:t3"))
        assertEquals(
            listOf("t1" to "image", "t2" to null),
            TaskDiscovery.unresolvedMediaTasks(msgs, setOf("t3")),
        )
    }
}
