package com.aiblackbox.portal.data.remote

import kotlinx.coroutines.runBlocking
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

    // ── M1.3: frontier action↓ channel (POST /action) — handleActionRequest ──
    //
    // /action now dispatches through the live actuators (a suspend, controller-backed
    // hop), so it is served by handleActionRequest, not the pure routeRequest.

    /** Records the dispatched frame + returns a canned action_result. */
    private class FakeActionDispatcher(
        private val result: ActionResultEnvelope = ActionResultEnvelope(success = true, detail = "ok"),
    ) : RemoteActionDispatcher {
        var lastBody: String? = null
        var lastTaskId: String? = null
        var lastOperator: String? = null
        override suspend fun dispatch(body: String, taskId: String, operator: String): ActionResultEnvelope {
            lastBody = body; lastTaskId = taskId; lastOperator = operator; return result
        }
    }

    @Test fun handle_action_requires_task_id() = runBlocking {
        // Blank/absent task_id -> 400 (mirrors /task's required-field gate).
        assertEquals(400, handleActionRequest("POST", """{"operator":"Brandon"}""", FakeActionDispatcher()).status)
        assertEquals(400, handleActionRequest("POST", "", FakeActionDispatcher()).status)  // "" -> "{}" -> blank
    }

    @Test fun handle_action_with_no_dispatcher_is_honest_not_wired() = runBlocking {
        // No actuator seam wired -> the honest handler-less state (M0 README decision 1).
        val r = handleActionRequest("POST",
            """{"msg":"action","task_id":"t1","operator":"Brandon","type":"element_click","resource_id":"x"}""",
            dispatcher = null)
        assertEquals(200, r.status)
        assertTrue(r.json, r.json.contains("\"msg\":\"action_result\""))
        assertTrue(r.json, r.json.contains("\"success\":false"))
        assertTrue(r.json, r.json.contains("\"error\":\"not_wired\""))
        assertFalse(r.json, r.json.contains("\"status\""))  // no non-conforming field leaks
    }

    @Test fun handle_action_dispatches_and_returns_result() = runBlocking {
        val d = FakeActionDispatcher(ActionResultEnvelope(success = true, detail = "tapped node[foo]"))
        val body = """{"msg":"action","task_id":"t1","operator":"Brandon","type":"element_click","resource_id":"foo"}"""
        val r = handleActionRequest("POST", body, d)
        assertEquals(200, r.status)
        assertEquals("t1", d.lastTaskId)          // transport task_id threaded through
        assertEquals("Brandon", d.lastOperator)   // operator threaded through
        assertEquals(body, d.lastBody)            // full frame handed to the dispatcher
        assertTrue(r.json, r.json.contains("\"success\":true"))
        assertTrue(r.json, r.json.contains("tapped node[foo]"))
    }

    @Test fun handle_action_malformed_json_is_400() = runBlocking {
        val r = handleActionRequest("POST", "not json", FakeActionDispatcher())
        assertEquals(400, r.status)
        assertTrue(r.json, r.json.contains("invalid JSON"))
    }

    @Test fun wrong_method_on_action_is_405() = runBlocking {
        assertEquals(405, handleActionRequest("GET", "", FakeActionDispatcher()).status)
        assertEquals(405, handleActionRequest("PUT", "", FakeActionDispatcher()).status)
    }

    @Test fun action_is_operator_scoped_like_task() {
        // Foreign operator -> 403; matching -> allow; blank bound operator fails closed.
        assertEquals(403, authorize("POST", "/action", "100.88.0.7", "Mallory", "Brandon")?.status)
        assertNull(authorize("POST", "/action", "100.88.0.7", "Brandon", "Brandon"))
        assertEquals(403, authorize("POST", "/action", "100.88.0.7", "Brandon", "")?.status)
        // Off-tailnet is rejected regardless of operator.
        assertEquals(403, authorize("POST", "/action", "8.8.8.8", "Brandon", "Brandon")?.status)
    }

    // ── I1: observation↑ stream (GET /stream/{id}) is operator-scoped; only GET ──

    @Test fun get_stream_blank_operator_is_rejected() {
        // A GET carries the operator as the ?operator= query param (extracted in serve());
        // blank/absent operator -> 403 (fail closed — the stream carries screen contents in M1).
        assertEquals(403, authorize("GET", "/stream/t1", "100.88.0.7", "", "Brandon")?.status)
    }

    @Test fun get_stream_foreign_operator_is_rejected() {
        assertEquals(403, authorize("GET", "/stream/t1", "100.88.0.7", "Mallory", "Brandon")?.status)
    }

    @Test fun get_stream_matching_operator_is_allowed() {
        assertNull(authorize("GET", "/stream/t1", "100.88.0.7", "Brandon", "Brandon"))
    }

    @Test fun get_stream_blank_bound_operator_fails_closed() {
        assertEquals(403, authorize("GET", "/stream/t1", "100.88.0.7", "Brandon", "")?.status)
    }

    @Test fun get_stream_off_tailnet_is_rejected() {
        assertEquals(403, authorize("GET", "/stream/t1", "192.168.1.5", "Brandon", "Brandon")?.status)
    }

    @Test fun stream_is_get_only_post_is_405() {
        // The pure method gate: GET proceeds (null), anything else -> 405.
        assertNull(streamMethodGate("GET"))
        assertEquals(405, streamMethodGate("POST")?.status)
        assertEquals(405, streamMethodGate("PUT")?.status)
    }

    // ── F2 / I3: the observation stream REFUSES a killed task (completes the kill-switch) ──

    @Test fun stream_kill_gate_refuses_a_killed_task() {
        // A live task proceeds to the SSE pump (null); a STOPPED task is refused (409) so the
        // server never serves it a fresh observation frame (which would resurrect the banner +
        // keep the loop driving a session the user ended).
        assertNull(streamKillGate("t1", killed = false))
        val denied = streamKillGate("t1", killed = true)
        assertEquals(409, denied?.status)
        // Same stable phrase the killed /action result carries → the loop keys on one signal.
        assertTrue(denied!!.json, denied.json.contains("stopped by user"))
    }

    // ── F2: the killed /action result carries a machine-detectable "stopped" signal ──

    @Test fun killed_action_result_carries_a_stable_stopped_signal_the_loop_keys_on() = runBlocking {
        // The dispatcher refuses a stopped task with a stable detail (no error code — it is a
        // user-initiated stop, not a failure). The M2 loop's terminal-state detection keys on the
        // "stopped by user" phrase to short-circuit with error_kind=stopped.
        val killed = ActionResultEnvelope(success = false, detail = "remote control stopped by user")
        val d = object : RemoteActionDispatcher {
            override suspend fun dispatch(body: String, taskId: String, operator: String) = killed
        }
        val r = handleActionRequest("POST",
            """{"msg":"action","task_id":"t1","operator":"Brandon","type":"element_click","resource_id":"x"}""",
            d)
        assertEquals(200, r.status)
        assertTrue(r.json, r.json.contains("stopped by user"))   // stable, machine-detectable
        assertFalse(r.json, r.json.contains("\"error\""))        // no error code for a user stop
    }

    // ── M0: RemoteTaskHandlerHolder seam — frontier ↔ Gemma swap (no socket rebind) ──

    @Test fun handler_holder_swaps_frontier_and_fake_last_set_wins() {
        try {
            // Default (nothing published) -> the model-free no-op handler.
            RemoteTaskHandlerHolder.clear()
            assertTrue(RemoteTaskHandlerHolder.current() === NoopRemoteTaskHandler)

            // A fake (stand-in for the Gemma RemoteTaskRunner) is settable + read back.
            val fake = FakeHandler()
            RemoteTaskHandlerHolder.set(fake)
            assertTrue(RemoteTaskHandlerHolder.current() === fake)

            // The frontier handler swaps in over the SAME seam — last set wins, no rebind.
            val frontier = FrontierRemoteTaskHandler("")  // JVM-safe: String ctor, no Android
            RemoteTaskHandlerHolder.set(frontier)
            assertTrue(RemoteTaskHandlerHolder.current() === frontier)

            // Swap back to the fake (Gemma) — again the last set wins.
            RemoteTaskHandlerHolder.set(fake)
            assertTrue(RemoteTaskHandlerHolder.current() === fake)
        } finally {
            RemoteTaskHandlerHolder.clear()  // restore global seam for other tests
        }
    }
}
