package com.aiblackbox.portal.data.remote

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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

    // ── Task 8: source/scope auth ──

    @Test fun off_tailnet_source_is_rejected() {
        assertEquals(403, authorize("POST", "/task", "192.168.1.50", "Brandon", "Brandon")?.status)
    }

    @Test fun tailnet_source_matching_operator_is_allowed() {
        assertNull(authorize("POST", "/task", "100.88.0.7", "Brandon", "Brandon"))
    }

    @Test fun tailnet_source_wrong_operator_is_rejected() {
        assertEquals(403, authorize("POST", "/task", "100.88.0.7", "Mallory", "Brandon")?.status)
    }

    @Test fun blank_bound_operator_fails_closed_on_task() {
        assertEquals(403, authorize("POST", "/task", "100.88.0.7", "Brandon", "")?.status)
    }

    @Test fun healthz_and_status_need_only_tailnet_not_operator() {
        assertNull(authorize("GET", "/healthz", "100.88.0.7", "", "Brandon"))
        assertNull(authorize("GET", "/status/abc", "100.88.0.7", "", "Brandon"))
    }

    @Test fun isTailnetSource_classification() {
        assertTrue(isTailnetSource("100.64.0.1"))
        assertTrue(isTailnetSource("100.127.255.255"))
        assertTrue(isTailnetSource("127.0.0.1"))             // loopback (same-device)
        assertTrue(isTailnetSource("fd7a:115c:a1e0::1"))     // Tailscale IPv6
        assertFalse(isTailnetSource("100.63.255.255"))       // just below CGNAT
        assertFalse(isTailnetSource("100.128.0.1"))          // just above CGNAT
        assertFalse(isTailnetSource("192.168.1.5"))          // LAN
        assertFalse(isTailnetSource("8.8.8.8"))              // public
        assertFalse(isTailnetSource(""))
    }

    @Test fun extractOperator_is_tolerant() {
        assertEquals("Brandon", extractOperator("""{"task":"x","operator":"Brandon"}"""))
        assertEquals("", extractOperator("not json"))
        assertEquals("", extractOperator("""{"task":"x"}"""))
    }
}
