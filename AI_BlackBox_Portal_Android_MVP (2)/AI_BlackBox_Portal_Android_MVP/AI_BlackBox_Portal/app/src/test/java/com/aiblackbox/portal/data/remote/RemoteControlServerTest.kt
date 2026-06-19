package com.aiblackbox.portal.data.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Records what was submitted + returns canned status/health, so [routeRequest] can
 *  be exercised purely (no NanoHTTPD, no sockets). */
private class FakeHandler(
    private val submitId: String = "task-1",
    private val status: RemoteStatus? = RemoteStatus(phase = "working"),
    private val health: Boolean = true,
) : RemoteTaskHandler {
    var lastTask: String? = null
    var lastOperator: String? = null
    override fun submitTask(task: String, operator: String): String {
        lastTask = task; lastOperator = operator; return submitId
    }
    override fun taskStatus(taskId: String): RemoteStatus? = status
    override fun healthz(): Boolean = health
}

class RemoteControlServerTest {

    @Test fun healthz_returns_ok() {
        val r = routeRequest("GET", "/healthz", "", FakeHandler(health = true))
        assertEquals(200, r.status)
        assertTrue(r.json, r.json.contains("\"ok\":true"))
    }

    @Test fun post_task_returns_task_id_and_passes_trimmed_fields() {
        val h = FakeHandler(submitId = "abc")
        val r = routeRequest("POST", "/task",
            """{"task":"  open maps  ","operator":"Brandon"}""", h)
        assertEquals(200, r.status)
        assertTrue(r.json, r.json.contains("\"task_id\":\"abc\""))
        assertEquals("open maps", h.lastTask)     // trimmed
        assertEquals("Brandon", h.lastOperator)
    }

    @Test fun post_task_blank_task_is_400() {
        val r = routeRequest("POST", "/task", """{"task":"   "}""", FakeHandler())
        assertEquals(400, r.status)
        assertTrue(r.json, r.json.contains("task required"))
    }

    @Test fun post_task_malformed_json_is_400() {
        val r = routeRequest("POST", "/task", "not json", FakeHandler())
        assertEquals(400, r.status)
        assertTrue(r.json, r.json.contains("invalid JSON"))
    }

    @Test fun post_task_empty_body_is_400() {
        val r = routeRequest("POST", "/task", "", FakeHandler())
        assertEquals(400, r.status)  // empty -> "{}" -> task blank -> 400
    }

    @Test fun get_status_known_returns_status_json() {
        val h = FakeHandler(status = RemoteStatus(phase = "done", result = "Opened Maps."))
        val r = routeRequest("GET", "/status/task-1", "", h)
        assertEquals(200, r.status)
        assertTrue(r.json, r.json.contains("\"phase\":\"done\""))
        assertTrue(r.json, r.json.contains("Opened Maps."))
    }

    @Test fun get_status_unknown_is_404() {
        val r = routeRequest("GET", "/status/nope", "", FakeHandler(status = null))
        assertEquals(404, r.status)
    }

    @Test fun get_status_empty_id_defers_to_handler() {
        // GET /status/ -> handler.taskStatus("") -> null in any real impl -> 404.
        val r = routeRequest("GET", "/status/", "", FakeHandler(status = null))
        assertEquals(404, r.status)
    }

    @Test fun wrong_method_on_known_path_is_405() {
        assertEquals(405, routeRequest("GET", "/task", "", FakeHandler()).status)
        assertEquals(405, routeRequest("POST", "/healthz", "", FakeHandler()).status)
        assertEquals(405, routeRequest("POST", "/status/x", "", FakeHandler()).status)
    }

    @Test fun unknown_path_is_404() {
        assertEquals(404, routeRequest("GET", "/nope", "", FakeHandler()).status)
    }

    @Test fun method_is_case_insensitive() {
        assertEquals(200, routeRequest("get", "/healthz", "", FakeHandler()).status)
    }
}
