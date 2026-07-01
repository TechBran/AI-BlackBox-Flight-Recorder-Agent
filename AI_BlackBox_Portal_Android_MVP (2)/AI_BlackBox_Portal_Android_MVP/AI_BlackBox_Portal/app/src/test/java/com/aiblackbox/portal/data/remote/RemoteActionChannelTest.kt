package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.data.local.FakePhoneController
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.overlay.DeviceCapabilities
import com.aiblackbox.portal.overlay.FormFactor
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M1.3) Tests for the action↓ channel core: the PURE [parseAction] (each `action.json`
 * variant → the right dispatch plan, incl. the newly-closed coordinate_tap /
 * coordinate_swipe / recents gaps) and the [PhoneActionDispatcher] routing through a fake
 * [com.aiblackbox.portal.data.local.PhoneController] (each variant → the correct
 * controller call), the capability gate (coordinate skipped on XR), the kill-switch gate,
 * and action_result conformance.
 */
class RemoteActionChannelTest {

    private fun frame(json: String): JsonObject = Json.parseToJsonElement(json).jsonObject

    private val phoneCap = DeviceCapabilities(FormFactor.PHONE, hasScreenshot = true, supportsCoordinateGesture = true, displayId = 0)
    private val xrCap = DeviceCapabilities(FormFactor.XR_HEADSET, hasScreenshot = false, supportsCoordinateGesture = false, displayId = 0)

    /** Fake session signal: controls the kill gate + records start() calls. [killOnStart]
     *  models a stop() racing in RIGHT AFTER the top isKilled() check but during start() —
     *  the dispatcher's post-start re-check (I3) must still refuse THIS frame. */
    private class FakeSession(var killed: Boolean = false, private val killOnStart: Boolean = false) : SessionSignal {
        val started = mutableListOf<Pair<String, String>>()
        override fun isKilled(taskId: String): Boolean = killed
        override fun start(taskId: String, operator: String) {
            started.add(taskId to operator)
            if (killOnStart) killed = true
        }
    }

    // ======================= parseAction (pure) =======================

    @Test fun `element_click by resource_id routes to tap`() {
        val p = parseAction(frame("""{"type":"element_click","resource_id":"foo"}""")) as ActionParse.Plan
        assertEquals("tap", p.dispatchName)
        assertEquals(ActionKind.ELEMENT, p.kind)
        assertEquals("foo", p.args["resource_id"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `element_click by node_id routes to tap`() {
        val p = parseAction(frame("""{"type":"element_click","node_id":6}""")) as ActionParse.Plan
        assertEquals("tap", p.dispatchName)
        assertEquals(6, p.args["node_id"]?.jsonPrimitive?.int)
    }

    @Test fun `element_click with no ref is rejected`() {
        val r = parseAction(frame("""{"type":"element_click"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `element_set_text routes to type with text`() {
        val p = parseAction(frame("""{"type":"element_set_text","resource_id":"e","text":"hello"}""")) as ActionParse.Plan
        assertEquals("type", p.dispatchName)
        assertEquals("hello", p.args["text"]?.jsonPrimitive?.contentOrNull)
        assertEquals("e", p.args["resource_id"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `element_set_text without text is rejected`() {
        val r = parseAction(frame("""{"type":"element_set_text","resource_id":"e"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `coordinate_tap routes to coordinate_tap with x y`() {
        val p = parseAction(frame("""{"type":"coordinate_tap","x":100,"y":200}""")) as ActionParse.Plan
        assertEquals("coordinate_tap", p.dispatchName)
        assertEquals(ActionKind.COORDINATE, p.kind)
        assertEquals(100, p.args["x"]?.jsonPrimitive?.int)
        assertEquals(200, p.args["y"]?.jsonPrimitive?.int)
    }

    @Test fun `coordinate_tap tolerates float coords`() {
        val p = parseAction(frame("""{"type":"coordinate_tap","x":100.0,"y":200.0}""")) as ActionParse.Plan
        assertEquals(100, p.args["x"]?.jsonPrimitive?.int)
    }

    @Test fun `coordinate_tap missing y is rejected`() {
        val r = parseAction(frame("""{"type":"coordinate_tap","x":100}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `coordinate_swipe routes with the full segment and optional duration`() {
        val p = parseAction(frame("""{"type":"coordinate_swipe","x":1,"y":2,"x2":3,"y2":4,"duration_ms":500}""")) as ActionParse.Plan
        assertEquals("coordinate_swipe", p.dispatchName)
        assertEquals(ActionKind.COORDINATE, p.kind)
        assertEquals(1, p.args["x"]?.jsonPrimitive?.int)
        assertEquals(4, p.args["y2"]?.jsonPrimitive?.int)
        assertEquals(500, p.args["duration_ms"]?.jsonPrimitive?.int)
    }

    @Test fun `coordinate_swipe without duration omits it`() {
        val p = parseAction(frame("""{"type":"coordinate_swipe","x":1,"y":2,"x2":3,"y2":4}""")) as ActionParse.Plan
        assertNull(p.args["duration_ms"])
    }

    @Test fun `global_action back home recents route to their names`() {
        for (a in listOf("back", "home", "recents")) {
            val p = parseAction(frame("""{"type":"global_action","action":"$a"}""")) as ActionParse.Plan
            assertEquals(a, p.dispatchName)
            assertEquals(ActionKind.GLOBAL, p.kind)
        }
    }

    @Test fun `global_action unknown is rejected`() {
        val r = parseAction(frame("""{"type":"global_action","action":"overview"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `intent routes to its name with params verbatim`() {
        val p = parseAction(frame("""{"type":"intent","name":"send_sms","params":{"number":"555","body":"hi"}}""")) as ActionParse.Plan
        assertEquals("send_sms", p.dispatchName)   // not hardcoded — the name passes through
        assertEquals(ActionKind.INTENT, p.kind)
        assertEquals("555", p.args["number"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `intent without params is empty args`() {
        val p = parseAction(frame("""{"type":"intent","name":"flashlight_on"}""")) as ActionParse.Plan
        assertEquals("flashlight_on", p.dispatchName)
        assertTrue(p.args.isEmpty())
    }

    @Test fun `intent accepts the decision-9 comprehensive names`() {
        for (name in listOf("navigate", "open_settings", "send_intent", "capture_video", "pick_file")) {
            val p = parseAction(frame("""{"type":"intent","name":"$name"}""")) as ActionParse.Plan
            assertEquals(name, p.dispatchName)
            assertEquals(ActionKind.INTENT, p.kind)
        }
    }

    // ---- I1: a gesture/coordinate name can NOT be smuggled as an intent ----

    @Test fun `intent named coordinate_tap is rejected as unknown_action (I1)`() {
        val r = parseAction(frame("""{"type":"intent","name":"coordinate_tap","params":{"x":1,"y":2}}""")) as ActionParse.Reject
        assertEquals("unknown_action", r.error)
    }

    @Test fun `intent named read_screen is rejected as unknown_action (I1)`() {
        // read_screen-as-intent would leak screen text into action_result.detail — rejected.
        val r = parseAction(frame("""{"type":"intent","name":"read_screen"}""")) as ActionParse.Reject
        assertEquals("unknown_action", r.error)
    }

    @Test fun `no gesture global or coordinate dispatch name collides with an intent name (I1)`() {
        // The INTENT_ACTIONS-membership gate is only sound if the intent set is disjoint from
        // every gesture/global/coordinate dispatch name — verify that invariant here.
        val gestureGlobalCoord = com.aiblackbox.portal.data.local.ResidentTools.PHONE_ACTUATORS +
            setOf("coordinate_tap", "coordinate_swipe", "recents", "press_key")
        val overlap = com.aiblackbox.portal.data.local.ResidentTools.INTENT_ACTIONS.intersect(gestureGlobalCoord)
        assertTrue("intent names collide with dispatch names: $overlap", overlap.isEmpty())
    }

    // ---- MINOR (a): a non-action msg is rejected ----

    @Test fun `a frame stamped with a non-action msg is rejected`() {
        val r = parseAction(frame("""{"msg":"observation","type":"element_click","resource_id":"x"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `an explicit action msg still parses`() {
        val p = parseAction(frame("""{"msg":"action","type":"scroll","direction":"up"}""")) as ActionParse.Plan
        assertEquals("scroll", p.dispatchName)
    }

    @Test fun `open_app routes to open_app on the exact package wire key`() {
        val p1 = parseAction(frame("""{"type":"open_app","package":"com.x"}""")) as ActionParse.Plan
        assertEquals("open_app", p1.dispatchName)
        assertEquals("com.x", p1.args["package"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `open_app package_name alias is not on the wire (I2)`() {
        // I2: the wire key is EXACTLY `package` (action.json additionalProperties:false); the
        // legacy `package_name` alias was dropped from the parser.
        val r = parseAction(frame("""{"type":"open_app","package_name":"com.y"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `scroll routes to scroll with direction`() {
        val p = parseAction(frame("""{"type":"scroll","direction":"down"}""")) as ActionParse.Plan
        assertEquals("scroll", p.dispatchName)
        assertEquals("down", p.args["direction"]?.jsonPrimitive?.contentOrNull)
    }

    // ---- (M2 / F1) press_key parse: valid enum → Plan(KEY); unknown/missing → invalid_argument

    @Test fun `press_key valid keys route to press_key with a normalized key`() {
        for (k in listOf("enter", "back", "home", "recents", "tab", "delete")) {
            val p = parseAction(frame("""{"type":"press_key","key":"$k"}""")) as ActionParse.Plan
            assertEquals("press_key", p.dispatchName)
            assertEquals(ActionKind.KEY, p.kind)          // never coordinate-gated
            assertEquals(k, p.args["key"]?.jsonPrimitive?.contentOrNull)
        }
    }

    @Test fun `press_key normalizes case and trims`() {
        val p = parseAction(frame("""{"type":"press_key","key":" Enter "}""")) as ActionParse.Plan
        assertEquals("enter", p.args["key"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `press_key unknown key is invalid_argument`() {
        val r = parseAction(frame("""{"type":"press_key","key":"f13"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `press_key missing key is invalid_argument`() {
        val r = parseAction(frame("""{"type":"press_key"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    @Test fun `unknown type is unknown_action`() {
        val r = parseAction(frame("""{"type":"levitate"}""")) as ActionParse.Reject
        assertEquals("unknown_action", r.error)
    }

    @Test fun `missing type is invalid_argument`() {
        val r = parseAction(frame("""{"resource_id":"x"}""")) as ActionParse.Reject
        assertEquals("invalid_argument", r.error)
    }

    // ======================= PhoneActionDispatcher (routing) =======================

    private fun dispatcher(
        controller: FakePhoneController,
        cap: DeviceCapabilities = phoneCap,
        session: FakeSession = FakeSession(),
    ) = PhoneActionDispatcher(controller, capability = { cap }, sessionBus = session)

    private fun body(json: String) = """{"msg":"action","task_id":"t1","operator":"Brandon",${json.trim().removePrefix("{")}"""

    @Test fun `dispatch element_click reaches controller tap`() = runBlocking {
        val c = FakePhoneController()
        dispatcher(c).dispatch(body("""{"type":"element_click","resource_id":"foo"}"""), "t1", "Brandon")
        assertEquals(1, c.dispatched.size)
        assertEquals("tap", c.dispatched[0].first)
        assertEquals("foo", c.dispatched[0].second["resource_id"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `dispatch coordinate_tap reaches controller coordinate_tap`() = runBlocking {
        val c = FakePhoneController()
        dispatcher(c).dispatch(body("""{"type":"coordinate_tap","x":100,"y":200}"""), "t1", "Brandon")
        assertEquals("coordinate_tap", c.dispatched[0].first)
        assertEquals(100, c.dispatched[0].second["x"]?.jsonPrimitive?.int)
    }

    @Test fun `dispatch coordinate_swipe reaches controller coordinate_swipe`() = runBlocking {
        val c = FakePhoneController()
        dispatcher(c).dispatch(body("""{"type":"coordinate_swipe","x":1,"y":2,"x2":3,"y2":4}"""), "t1", "Brandon")
        assertEquals("coordinate_swipe", c.dispatched[0].first)
    }

    @Test fun `dispatch global_action recents reaches controller recents`() = runBlocking {
        val c = FakePhoneController()
        dispatcher(c).dispatch(body("""{"type":"global_action","action":"recents"}"""), "t1", "Brandon")
        assertEquals("recents", c.dispatched[0].first)
    }

    @Test fun `dispatch press_key reaches controller press_key with the key`() = runBlocking {
        val c = FakePhoneController()
        dispatcher(c).dispatch(body("""{"type":"press_key","key":"enter"}"""), "t1", "Brandon")
        assertEquals("press_key", c.dispatched[0].first)
        assertEquals("enter", c.dispatched[0].second["key"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `press_key is not coordinate-gated on XR (coordinate-free)`() = runBlocking {
        // press_key uses no coordinates → it must pass through on a coordinate-less device (XR),
        // unlike coordinate_tap/swipe. enter→IME / back/home/recents→performGlobalAction all work.
        val c = FakePhoneController()
        dispatcher(c, cap = xrCap).dispatch(body("""{"type":"press_key","key":"enter"}"""), "t1", "Brandon")
        assertEquals("press_key", c.dispatched[0].first)   // reached the controller on XR
    }

    @Test fun `dispatch intent reaches controller by intent name`() = runBlocking {
        val c = FakePhoneController()
        dispatcher(c).dispatch(body("""{"type":"intent","name":"send_sms","params":{"number":"555"}}"""), "t1", "Brandon")
        assertEquals("send_sms", c.dispatched[0].first)
        assertEquals("555", c.dispatched[0].second["number"]?.jsonPrimitive?.contentOrNull)
    }

    @Test fun `dispatch open_app and scroll reach the controller`() = runBlocking {
        val c = FakePhoneController()
        val d = dispatcher(c)
        d.dispatch(body("""{"type":"open_app","package":"com.x"}"""), "t1", "Brandon")
        d.dispatch(body("""{"type":"scroll","direction":"up"}"""), "t1", "Brandon")
        assertEquals("open_app", c.dispatched[0].first)
        assertEquals("scroll", c.dispatched[1].first)
    }

    // ---- capability gate: coordinate skipped on XR ----

    @Test fun `coordinate action is skipped and reported on XR`() = runBlocking {
        val c = FakePhoneController()
        val res = dispatcher(c, cap = xrCap)
            .dispatch(body("""{"type":"coordinate_tap","x":10,"y":20}"""), "t1", "Brandon")
        assertTrue(c.dispatched.isEmpty())          // never dispatched
        assertFalse(res.success)
        assertEquals("invalid_argument", res.error)
        assertTrue(res.detail!!, res.detail!!.contains("not supported"))
    }

    @Test fun `element action still works on XR`() = runBlocking {
        val c = FakePhoneController()
        dispatcher(c, cap = xrCap)
            .dispatch(body("""{"type":"element_click","resource_id":"ok"}"""), "t1", "Brandon")
        assertEquals("tap", c.dispatched[0].first)   // node click passes through on XR
    }

    // ---- kill switch ----

    @Test fun `killed task is refused and never actuated`() = runBlocking {
        val c = FakePhoneController()
        val res = dispatcher(c, session = FakeSession(killed = true))
            .dispatch(body("""{"type":"element_click","resource_id":"foo"}"""), "t1", "Brandon")
        assertTrue(c.dispatched.isEmpty())
        assertFalse(res.success)
        assertNull(res.error)                        // user-initiated stop -> no error code
        assertTrue(res.detail!!, res.detail!!.contains("stopped by user"))
    }

    @Test fun `post-start kill refuses the current frame and never actuates (I3)`() = runBlocking {
        // A stop() that races in during start() (killOnStart) is caught by the post-start
        // isKilled re-check — the frame is refused and nothing reaches the controller.
        val c = FakePhoneController()
        val res = dispatcher(c, session = FakeSession(killed = false, killOnStart = true))
            .dispatch(body("""{"type":"element_click","resource_id":"foo"}"""), "t1", "Brandon")
        assertTrue(c.dispatched.isEmpty())
        assertFalse(res.success)
        assertNull(res.error)                         // user-initiated stop -> no error code
        assertTrue(res.detail!!, res.detail!!.contains("stopped by user"))
    }

    @Test fun `a coordinate gesture smuggled as an intent cannot reach a coordinate dispatch on XR (I1)`() = runBlocking {
        // Even on XR (coordinate-gate on), a {type:intent, name:coordinate_tap} is rejected at
        // PARSE (unknown_action) — it never becomes a coordinate dispatch that could slip past
        // the capability gate, and the controller is never called.
        val c = FakePhoneController()
        val res = dispatcher(c, cap = xrCap)
            .dispatch(body("""{"type":"intent","name":"coordinate_tap","params":{"x":10,"y":20}}"""), "t1", "Brandon")
        assertTrue(c.dispatched.isEmpty())
        assertFalse(res.success)
        assertEquals("unknown_action", res.error)
    }

    @Test fun `dispatching an action marks the session active`() = runBlocking {
        val c = FakePhoneController()
        val s = FakeSession()
        PhoneActionDispatcher(c, capability = { phoneCap }, sessionBus = s)
            .dispatch(body("""{"type":"element_click","resource_id":"foo"}"""), "t1", "Brandon")
        assertEquals(listOf("t1" to "Brandon"), s.started)   // banner raised
    }

    // ---- action_result conformance ----

    @Test fun `success maps to a conforming action_result`() = runBlocking {
        val c = FakePhoneController { _, _ -> ToolResult(true, JsonPrimitive("tapped node[foo]")) }
        val res = dispatcher(c).dispatch(body("""{"type":"element_click","resource_id":"foo"}"""), "t1", "Brandon")
        assertEquals(WireMessageType.ACTION_RESULT, res.msg)
        assertTrue(res.success)
        assertEquals("tapped node[foo]", res.detail)
        assertNull(res.error)
    }

    @Test fun `node-not-found maps to node_not_found error`() = runBlocking {
        val c = FakePhoneController { _, _ -> ToolResult(false, JsonPrimitive("node 5 not found")) }
        val res = dispatcher(c).dispatch(body("""{"type":"element_click","node_id":5}"""), "t1", "Brandon")
        assertFalse(res.success)
        assertEquals("node_not_found", res.error)
    }

    @Test fun `service-disabled maps to not_enabled`() = runBlocking {
        val c = FakePhoneController { _, _ -> ToolResult(false, JsonPrimitive("accessibility service not enabled")) }
        val res = dispatcher(c).dispatch(body("""{"type":"scroll","direction":"up"}"""), "t1", "Brandon")
        assertEquals("not_enabled", res.error)
    }

    @Test fun `malformed action frame gives invalid_argument result`() = runBlocking {
        val c = FakePhoneController()
        val res = dispatcher(c).dispatch("not json", "t1", "Brandon")
        assertFalse(res.success)
        assertEquals("invalid_argument", res.error)
        assertTrue(c.dispatched.isEmpty())
    }

    @Test fun `follow-on observation is embedded when a provider is wired`() = runBlocking {
        val c = FakePhoneController()
        val obs = Observation(uiTree = emptyList(), deviceCapability = phoneCap, timestamp = 9L)
        val d = PhoneActionDispatcher(
            c, capability = { phoneCap }, sessionBus = FakeSession(),
            observationProvider = { obs },
        )
        val res = d.dispatch(body("""{"type":"element_click","resource_id":"foo"}"""), "t1", "Brandon")
        assertTrue(res.success)
        assertEquals(obs, res.observation)   // fresh screen state rides on the result
    }

    @Test fun `no observation is embedded without a provider (stream is canonical)`() = runBlocking {
        val c = FakePhoneController()
        val res = dispatcher(c).dispatch(body("""{"type":"element_click","resource_id":"foo"}"""), "t1", "Brandon")
        assertNull(res.observation)
    }

    @Test fun `reject variant does not actuate`() = runBlocking {
        val c = FakePhoneController()
        val res = dispatcher(c).dispatch(body("""{"type":"element_click"}"""), "t1", "Brandon")
        assertFalse(res.success)
        assertEquals("invalid_argument", res.error)
        assertTrue(c.dispatched.isEmpty())
    }

    // ---- classifyActuatorError (pure) ----

    @Test fun `classifyActuatorError maps the actuator phrases`() {
        assertEquals("not_enabled", classifyActuatorError("accessibility service not enabled"))
        assertEquals("node_not_found", classifyActuatorError("node 3 not found"))
        assertEquals("unknown_action", classifyActuatorError("unknown phone action: foo"))
        assertEquals("invalid_argument", classifyActuatorError("text required"))
        // MINOR (b): bad-argument phrases → invalid_argument (not dispatch_failed).
        assertEquals("invalid_argument", classifyActuatorError("unknown scroll direction: sideways"))
        assertEquals("invalid_argument", classifyActuatorError("unknown swipe direction: diag"))
        assertEquals("invalid_argument", classifyActuatorError("unsafe or invalid uri"))
        assertEquals("invalid_argument", classifyActuatorError("invalid uri"))
        assertEquals("dispatch_failed", classifyActuatorError("app not installed: com.x"))
        assertEquals("dispatch_failed", classifyActuatorError("swipe gesture dispatch failed"))
        assertEquals("dispatch_failed", classifyActuatorError("scroll gesture dispatch failed"))
        // User decisions are NOT errors.
        assertNull(classifyActuatorError("user declined"))
        assertNull(classifyActuatorError("user declined credential entry"))
    }
}
