package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskListResponse
import com.aiblackbox.portal.data.model.TaskStatus
import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

/**
 * The one task poller (audit A11-settings-generation-18, rank 76): the screens used to
 * absolutize a result URL their own ways, or not at all, so the Image screen handed the
 * box's `/ui/uploads/...` straight to the image loader and Google SSML to its player.
 */
class TaskPollerTest {
    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi
    private lateinit var base: String

    @Before fun setUp() {
        server = MockWebServer(); server.start()
        base = server.url("").toString().trimEnd('/')
        api = BlackBoxApi(base)
    }
    @After fun tearDown() = server.close()

    private fun respond(body: String, code: Int = 200) =
        server.enqueue(MockResponse.Builder().code(code).body(body).build())

    @Test fun `a completed task's relative result URL comes back absolute against the box`() = runTest {
        respond("""{"task_id":"t1","task_type":"image","status":"processing","progress":40}""")
        respond("""{"task_id":"t1","task_type":"image","status":"completed","progress":100,"result_url":"/ui/uploads/a.png"}""")

        val seen = mutableListOf<Int>()
        val outcome = TaskPoller(api).await("t1", intervalMs = 10) { seen += it.progress }

        assertEquals(TaskPollOutcome.Completed::class, outcome::class)
        outcome as TaskPollOutcome.Completed
        assertEquals("$base/ui/uploads/a.png", outcome.resultUrl)
        // The status itself keeps what the box said.
        assertEquals("/ui/uploads/a.png", outcome.status.resultUrl)
        assertEquals(listOf(40, 100), seen)
        assertEquals("/tasks/status/t1", server.takeRequest().target)
    }

    @Test fun `failed carries the box's error_message, a 404 ends as a failure`() = runTest {
        respond("""{"task_id":"t1","status":"failed","error_message":"quota exhausted"}""")
        assertEquals(TaskPollOutcome.Failed("quota exhausted"), TaskPoller(api).await("t1", intervalMs = 10))

        respond("""{"detail":"Task not found"}""", code = 404)
        assertEquals(TaskPollOutcome.Failed("Task not found"), TaskPoller(api).await("t2", intervalMs = 10))
    }

    @Test fun `absolute URLs, blank ones and a missing base are handled`() {
        assertEquals("http://box:9091/ui/uploads/v.mp4", absoluteResultUrl("http://box:9091", "/ui/uploads/v.mp4"))
        assertEquals("http://box:9091/ui/uploads/v.mp4", absoluteResultUrl("http://box:9091/", "ui/uploads/v.mp4"))
        assertEquals("https://cdn.example/x.png", absoluteResultUrl("http://box:9091", "https://cdn.example/x.png"))
        assertEquals("/ui/uploads/v.mp4", absoluteResultUrl("", "/ui/uploads/v.mp4"))
        assertNull(absoluteResultUrl("http://box:9091", null))
        assertNull(absoluteResultUrl("http://box:9091", "  "))
    }

    @Test fun `an outcome built without a base keeps the status's URL`() {
        val s = TaskStatus(taskId = "t", status = "completed", resultUrl = "/ui/uploads/a.wav")
        assertEquals("/ui/uploads/a.wav", TaskPollOutcome.Completed(s).resultUrl)
    }

    @Test fun `task list rows are typed - a JSON null stays null, odd rows are skipped`() {
        val rows = TaskListResponse.parseRows(
            """{"tasks":[
                {"task_id":"a","task_type":null,"status":"pending","progress":0,"result_url":null,
                 "progress_text":"step 1/3","device_id":"blackbox","session_id":"s1","reasoning_text":null},
                {"task_id":"b","task_type":"video","status":"processing","progress":55,"operator":"op"},
                {"status":"pending"},
                "garbage"
            ],"total_tasks":9,"filtered_count":4}"""
        )!!
        assertEquals(listOf("a", "b"), rows.map { it.taskId })
        // The hand-built rows this replaces read JSON null as the string "null".
        assertNull(rows[0].taskType)
        assertNull(rows[0].resultUrl)
        assertEquals("step 1/3", rows[0].progressText)
        assertEquals("blackbox", rows[0].effectiveDeviceId())
        assertEquals("s1", rows[0].effectiveSessionId())
        assertEquals(55, rows[1].progress)

        assertEquals(emptyList<TaskStatus>(), TaskListResponse.parseRows("""{"tasks":[]}"""))
        assertNull("no task list is no answer", TaskListResponse.parseRows("""{"detail":"down"}"""))
        assertNull(TaskListResponse.parseRows("<html>"))
    }
}
