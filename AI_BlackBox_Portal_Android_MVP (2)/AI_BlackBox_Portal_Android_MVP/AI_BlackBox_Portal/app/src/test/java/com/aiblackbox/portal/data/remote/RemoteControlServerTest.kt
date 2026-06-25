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

    // /notify carries a top-level `operator` too — the shared tolerant decode reads it.
    @Test fun extractOperator_reads_notify_payload() {
        assertEquals("Sarah", extractOperator("""{"title":"Hi","operator":"Sarah","notif_id":"n1"}"""))
    }

    // ── MN.4: /notify route ──

    /** Captures the last notification dispatched so /notify routing is exercised purely. */
    private class FakeNotifier : Notifier {
        var calls = 0
        var lastTitle: String? = null
        var lastBody: String? = null
        var lastCategory: String? = null
        var lastOperator: String? = null
        var lastNotifId: String? = null
        override fun postNotification(title: String, body: String, category: String, operator: String, notifId: String) {
            calls++; lastTitle = title; lastBody = body; lastCategory = category
            lastOperator = operator; lastNotifId = notifId
        }
    }

    @Test fun post_notify_posts_and_trims_fields() {
        val n = FakeNotifier()
        val r = routeRequest("POST", "/notify",
            """{"title":"  Build done  ","body":"  green  ","category":"ci","operator":"Brandon","notif_id":" n7 "}""",
            FakeHandler(), n)
        assertEquals(200, r.status)
        assertTrue(r.json, r.json.contains("\"ok\":true"))
        assertEquals(1, n.calls)
        assertEquals("Build done", n.lastTitle)   // trimmed
        assertEquals("green", n.lastBody)
        assertEquals("ci", n.lastCategory)
        assertEquals("Brandon", n.lastOperator)
        assertEquals("n7", n.lastNotifId)
    }

    @Test fun post_notify_empty_body_is_allowed_metadata_only() {
        val n = FakeNotifier()
        val r = routeRequest("POST", "/notify",
            """{"title":"New message","body":"","category":"chat","operator":"Sarah","notif_id":"m1"}""",
            FakeHandler(), n)
        assertEquals(200, r.status)
        assertEquals(1, n.calls)
        assertEquals("New message", n.lastTitle)
        assertEquals("", n.lastBody)              // empty body accepted (title shown)
    }

    @Test fun post_notify_blank_title_and_body_is_400() {
        val n = FakeNotifier()
        val r = routeRequest("POST", "/notify", """{"title":"  ","body":"  ","category":"x"}""", FakeHandler(), n)
        assertEquals(400, r.status)
        assertEquals(0, n.calls)                  // nothing posted
    }

    @Test fun post_notify_malformed_json_is_400() {
        val r = routeRequest("POST", "/notify", "not json", FakeHandler(), FakeNotifier())
        assertEquals(400, r.status)
        assertTrue(r.json, r.json.contains("invalid JSON"))
    }

    @Test fun post_notify_without_notifier_is_503() {
        // notifier defaulted null (the old 4-arg call path) -> /notify is unavailable.
        val r = routeRequest("POST", "/notify", """{"title":"hi"}""", FakeHandler())
        assertEquals(503, r.status)
    }

    @Test fun wrong_method_on_notify_is_405() {
        assertEquals(405, routeRequest("GET", "/notify", "", FakeHandler(), FakeNotifier()).status)
    }

    @Test fun notify_works_model_free_with_noop_handler() {
        // A model-less device: no-op handler, but /notify still posts.
        val n = FakeNotifier()
        val r = routeRequest("POST", "/notify", """{"title":"x","body":"y"}""", NoopRemoteTaskHandler, n)
        assertEquals(200, r.status)
        assertEquals(1, n.calls)
        // And /healthz reports not-ready on a model-less device.
        assertTrue(routeRequest("GET", "/healthz", "", NoopRemoteTaskHandler).json.contains("\"ok\":false"))
    }

    // ── MN.4: /notify auth (subscription allow-list re-check) ──

    @Test fun notify_off_tailnet_is_rejected() {
        assertEquals(403, authorize("POST", "/notify", "8.8.8.8", "Brandon", "Brandon")?.status)
    }

    @Test fun notify_tailnet_default_predicate_accepts_all() {
        // Default isSubscribed = accept-all -> tailnet source alone authorizes.
        assertNull(authorize("POST", "/notify", "100.88.0.7", "Brandon", "Brandon"))
    }

    @Test fun notify_unsubscribed_operator_is_rejected() {
        assertEquals(403, authorize("POST", "/notify", "100.88.0.7", "Mallory", "Brandon",
            isSubscribed = { it == "Brandon" })?.status)
    }

    @Test fun notify_subscribed_operator_is_allowed() {
        assertNull(authorize("POST", "/notify", "100.88.0.7", "Brandon", "Brandon",
            isSubscribed = { it == "Brandon" }))
    }
}
