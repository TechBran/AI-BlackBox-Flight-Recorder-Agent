package com.aiblackbox.portal.overlay

import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M1.3) Proves the NEW [AndroidPhoneController] dispatch branches — `coordinate_tap`,
 * `coordinate_swipe`, `recents` (the closed M0 gaps) — actually REACH the actuators
 * rather than falling through to "unknown phone action".
 *
 * The controller is built with a null-service [Actuators], so the reached actuator
 * short-circuits to `accessibility service not enabled` BEFORE touching the framework
 * (Path/GestureDescription/performGlobalAction are Stub! throws in the unit-test
 * android.jar). Distinguishing that graceful phrase from `unknown phone action` proves
 * the branch exists + routes correctly (the real gesture dispatch is device-verified).
 */
class AndroidPhoneControllerActionTest {

    private fun controller(): AndroidPhoneController = AndroidPhoneController(
        UiTreeReader(rootProvider = { null }),
        Actuators({ null }),        // null service -> every actuator returns "not enabled"
        IntentActuator({ null }),
    )

    private fun detail(r: ToolResult): String? = (r.result as? JsonPrimitive)?.content

    @Test fun `coordinate_tap reaches the actuator`() = runBlocking {
        val r = controller().dispatch("coordinate_tap", buildJsonObject { put("x", 100); put("y", 200) })
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", detail(r))
    }

    @Test fun `coordinate_tap missing coord is a clear argument error`() = runBlocking {
        val r = controller().dispatch("coordinate_tap", buildJsonObject { put("x", 100) })
        assertFalse(r.success)
        assertEquals("y required", detail(r))
    }

    @Test fun `coordinate_swipe reaches the actuator (with and without duration)`() = runBlocking {
        val c = controller()
        val a = c.dispatch("coordinate_swipe", buildJsonObject { put("x", 1); put("y", 2); put("x2", 3); put("y2", 4) })
        val b = c.dispatch("coordinate_swipe", buildJsonObject {
            put("x", 1); put("y", 2); put("x2", 3); put("y2", 4); put("duration_ms", 500)
        })
        assertEquals("accessibility service not enabled", detail(a))
        assertEquals("accessibility service not enabled", detail(b))
    }

    // ---- (M6.1) XR coordinate gating — defense in depth at the controller seam ----

    private val xrCap = DeviceCapabilities(
        FormFactor.XR_HEADSET, hasScreenshot = false, supportsCoordinateGesture = false, displayId = 0)
    private val phoneCap = DeviceCapabilities(
        FormFactor.PHONE, hasScreenshot = true, supportsCoordinateGesture = true, displayId = 0)

    /** A controller with a live capability provider (M6.1). Null service → reached actuators
     *  return "not enabled", which lets us distinguish "gated (never reached)" from "passed
     *  through (reached, then not enabled)". */
    private fun controllerWithCapability(cap: DeviceCapabilities?): AndroidPhoneController =
        AndroidPhoneController(
            UiTreeReader(rootProvider = { null }),
            Actuators({ null }),
            IntentActuator({ null }),
            capability = { cap },
        )

    @Test fun `coordinate_tap is skipped and reported on XR`() = runBlocking {
        val r = controllerWithCapability(xrCap)
            .dispatch("coordinate_tap", buildJsonObject { put("x", 100); put("y", 200) })
        assertFalse(r.success)
        // The gate fired BEFORE the actuator (would otherwise say "not enabled").
        assertEquals("coordinate gestures not supported on xr_headset", detail(r))
    }

    @Test fun `coordinate_swipe is skipped and reported on XR`() = runBlocking {
        val r = controllerWithCapability(xrCap).dispatch("coordinate_swipe", buildJsonObject {
            put("x", 1); put("y", 2); put("x2", 3); put("y2", 4)
        })
        assertFalse(r.success)
        assertEquals("coordinate gestures not supported on xr_headset", detail(r))
    }

    @Test fun `element click still passes through on XR`() = runBlocking {
        // node ACTION_CLICK is display-agnostic — it must NOT be gated on XR. It reaches the
        // (null-service) actuator → "not enabled", proving it was NOT skipped by the coord gate.
        val r = controllerWithCapability(xrCap)
            .dispatch("tap", buildJsonObject { put("resource_id", "ok") })
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", detail(r))
    }

    @Test fun `intents still pass through on XR`() = runBlocking {
        // an intent (deterministic OS intent) needs no coordinates → never gated on XR.
        val r = controllerWithCapability(xrCap)
            .dispatch("show_map", buildJsonObject { put("query", "coffee") })
        // reaches the (null-context) IntentActuator rather than being coordinate-skipped.
        assertFalse(detail(r) == "coordinate gestures not supported on xr_headset")
    }

    @Test fun `coordinate_tap reaches the actuator on a phone (not gated)`() = runBlocking {
        val r = controllerWithCapability(phoneCap)
            .dispatch("coordinate_tap", buildJsonObject { put("x", 100); put("y", 200) })
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", detail(r)) // passed the gate
    }

    @Test fun `no capability provider leaves coordinate paths unchanged (back-compat)`() = runBlocking {
        val r = controllerWithCapability(null)
            .dispatch("coordinate_tap", buildJsonObject { put("x", 100); put("y", 200) })
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", detail(r)) // default = no gate
    }

    // ---- (C1, M4) coordinate taps + degenerate swipes route through the GATE ----

    /** Records whether the confirm seam was consulted; returns a fixed answer. */
    private class RecordingConfirm(private val answer: Boolean) : ConfirmUi {
        var calls = 0
        override suspend fun confirm(description: String): Boolean { calls++; return answer }
    }

    /** A controller whose gesture actuators carry a PERMISSION posture + an injected hit-test. */
    private fun gatedController(
        confirm: ConfirmUi,
        hit: CoordinateHit,
        mode: AutonomyMode = AutonomyMode.PERMISSION,
    ) = AndroidPhoneController(
        UiTreeReader(rootProvider = { null }),
        Actuators(service = { null }, mode = { mode }, confirm = confirm, coordinateLabeler = { _, _ -> hit }),
        IntentActuator({ null }),
    )

    @Test fun `coordinate_tap in PERMISSION consults the confirm gate — deny refuses`() = runBlocking {
        val confirm = RecordingConfirm(answer = false)
        val r = gatedController(confirm, CoordinateHit.None)
            .dispatch("coordinate_tap", buildJsonObject { put("x", 100); put("y", 200) })
        assertFalse(r.success)
        assertEquals("user declined", detail(r))
        assertEquals("a coordinate_tap must be gated (no compose-then-send bypass)", 1, confirm.calls)
    }

    @Test fun `a degenerate coordinate_swipe routes through the gated coordinate tap`() = runBlocking {
        // start == end is a tap-equivalent, not a drag → it must go through the GATED tap.
        val confirm = RecordingConfirm(answer = false)
        val r = gatedController(confirm, CoordinateHit.None)
            .dispatch("coordinate_swipe", buildJsonObject { put("x", 5); put("y", 5); put("x2", 5); put("y2", 5) })
        assertFalse(r.success)
        assertEquals("user declined", detail(r))
        assertEquals("a degenerate swipe must not bypass the tap gate", 1, confirm.calls)
    }

    @Test fun `a genuine drag coordinate_swipe does NOT gate`() = runBlocking {
        // start != end → a real scroll/pan → low-risk → ungated (reaches the swipe, no service).
        val confirm = RecordingConfirm(answer = false)
        val r = gatedController(confirm, CoordinateHit.None)
            .dispatch("coordinate_swipe", buildJsonObject { put("x", 1); put("y", 2); put("x2", 9); put("y2", 9) })
        assertEquals("accessibility service not enabled", detail(r))
        assertEquals("a genuine drag must not gate", 0, confirm.calls)
    }

    @Test fun `YOLO coordinate_tap does not gate`() = runBlocking {
        val confirm = RecordingConfirm(answer = false) // would refuse IF consulted
        val r = gatedController(confirm, CoordinateHit.None, AutonomyMode.YOLO)
            .dispatch("coordinate_tap", buildJsonObject { put("x", 100); put("y", 200) })
        assertEquals("accessibility service not enabled", detail(r))
        assertEquals("YOLO must fire a coordinate tap unattended", 0, confirm.calls)
    }

    @Test fun `recents reaches the actuator`() = runBlocking {
        val r = controller().dispatch("recents", JsonObject(emptyMap()))
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", detail(r))
    }

    @Test fun `back stays a11y while home now routes to the intent layer`() = runBlocking {
        // back has no reliable intent equivalent → stays a11y (null service → not enabled).
        assertEquals("accessibility service not enabled", detail(controller().dispatch("back", JsonObject(emptyMap()))))
        // home now fires via ACTION_MAIN+CATEGORY_HOME through the Application Context (no a11y):
        // it reaches the null-context IntentActuator ("app context unavailable"), NOT the a11y path.
        assertEquals("app context unavailable", detail(controller().dispatch("home", JsonObject(emptyMap()))))
    }

    // ---- (M2 / F1) press_key: enter (IME submit) + back/home/recents (global) reach the actuator

    @Test fun `press_key enter reaches the actuator`() = runBlocking {
        // enter → Actuators.pressKey("enter") → ImeEnter path; null service short-circuits to the
        // graceful not-enabled BEFORE touching findFocus/ACTION_IME_ENTER (Stub! throws otherwise),
        // proving the dispatch branch exists + routes (the real IME submit is device-verified).
        val r = controller().dispatch("press_key", buildJsonObject { put("key", "enter") })
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", detail(r))
    }

    @Test fun `press_key back reaches the actuator (global path)`() = runBlocking {
        val r = controller().dispatch("press_key", buildJsonObject { put("key", "back") })
        assertEquals("accessibility service not enabled", detail(r))
    }

    @Test fun `press_key missing key is a clear argument error`() = runBlocking {
        val r = controller().dispatch("press_key", JsonObject(emptyMap()))
        assertFalse(r.success)
        assertEquals("key required", detail(r))
    }

    @Test fun `a truly unknown action is still rejected as unknown`() = runBlocking {
        val r = controller().dispatch("levitate", JsonObject(emptyMap()))
        assertFalse(r.success)
        assertTrue(detail(r)!!, detail(r)!!.contains("unknown phone action"))
    }

    // ---- (M8.1) a11y-revocation → intent fallback ----

    /** A controller whose live a11y probe reports the service DISABLED / OS-revoked. */
    private fun controllerA11yOff(): AndroidPhoneController = AndroidPhoneController(
        UiTreeReader(rootProvider = { null }),
        Actuators({ null }),
        IntentActuator({ null }),
        a11yEnabled = { false },
    )

    @Test fun `read_screen degrades to intent_only_mode when a11y is off`() = runBlocking {
        val r = controllerA11yOff().dispatch("read_screen", JsonObject(emptyMap()))
        assertFalse(r.success)
        assertTrue(detail(r)!!, detail(r)!!.startsWith("intent_only_mode"))
        // it must list the still-available intent actions so the driver knows what remains.
        assertTrue(detail(r)!!, detail(r)!!.contains("show_map"))
    }

    @Test fun `a11y-dependent actions degrade to intent_only_mode when a11y is off`() = runBlocking {
        // open_app + home are NO LONGER here — they route through the Application-Context
        // IntentActuator (no a11y) and are covered by their own tests below. Everything left
        // genuinely needs the a11y service (screen inspection / fine-grained UI / back+recents).
        val c = controllerA11yOff()
        for (name in listOf("tap", "type", "swipe", "scroll", "back",
                "recents", "press_key", "coordinate_tap", "coordinate_swipe")) {
            val r = c.dispatch(name, buildJsonObject {
                put("resource_id", "x"); put("text", "y"); put("direction", "down")
                put("package", "com.x"); put("key", "enter"); put("x", 1); put("y", 2)
                put("x2", 3); put("y2", 4)
            })
            assertFalse(name, r.success)
            assertTrue("$name -> ${detail(r)}", detail(r)!!.startsWith("intent_only_mode"))
        }
    }

    @Test fun `the INTENT path still fires when a11y is off (not gated to intent_only_mode)`() = runBlocking {
        // show_map is an Application-Context intent (no a11y) — it must NOT be intent-only-gated;
        // it reaches the (null-context) IntentActuator instead.
        val r = controllerA11yOff().dispatch("show_map", buildJsonObject { put("query", "coffee") })
        assertFalse("intent action must not be gated as intent_only_mode",
            detail(r)!!.startsWith("intent_only_mode"))
    }

    @Test fun `re-enabling a11y resumes the screen path (gate no longer fires)`() = runBlocking {
        // Same construction but a11y back ON → the a11y action reaches the (null-service) actuator
        // ("not enabled"), proving the gate is live per-dispatch and resumes when a11y returns.
        val on = AndroidPhoneController(
            UiTreeReader(rootProvider = { null }), Actuators({ null }), IntentActuator({ null }),
            a11yEnabled = { true },
        )
        val r = on.dispatch("tap", buildJsonObject { put("resource_id", "x") })
        assertEquals("accessibility service not enabled", detail(r))
    }

    @Test fun `default a11yEnabled is true — existing callers unaffected (no gate)`() = runBlocking {
        // The default direct-constructor controller (no a11yEnabled wired) behaves as a11y-on.
        val r = controller().dispatch("tap", buildJsonObject { put("resource_id", "x") })
        assertEquals("accessibility service not enabled", detail(r))
    }

    // ---- (intent-layer, XR) open_app + home fire via the Application Context, NO a11y ----
    //
    // open_app + home are removed from A11Y_DEPENDENT_ACTIONS: they route to the (Application-
    // Context) IntentActuator regardless of a11y state, NOT the intent_only_mode fallback. With a
    // null-context IntentActuator the routed call reaches "app context unavailable" — the point is
    // it is NOT intent_only_mode (it dispatched to the intent layer). The real launch is
    // device-verified (M6 on-device: intents fired with ZERO a11y on Samsung Galaxy XR).

    @Test fun `open_app launches via the intent layer with a11y OFF (not intent_only_mode)`() = runBlocking {
        val r = controllerA11yOff().dispatch("open_app", buildJsonObject { put("package", "com.android.settings") })
        assertFalse(r.success)
        assertFalse("open_app must NOT be gated to intent_only_mode when a11y is off",
            detail(r)!!.startsWith("intent_only_mode"))
        assertEquals("reached the Application-Context IntentActuator", "app context unavailable", detail(r))
    }

    @Test fun `open_app launches via the intent layer with a11y ON (uniform behavior)`() = runBlocking {
        val r = controller().dispatch("open_app", buildJsonObject { put("package", "com.android.settings") })
        assertFalse(detail(r)!!.startsWith("intent_only_mode"))
        assertEquals("app context unavailable", detail(r))   // same intent path whether a11y on/off
    }

    @Test fun `home goes home via the intent layer with a11y OFF (not intent_only_mode)`() = runBlocking {
        val r = controllerA11yOff().dispatch("home", JsonObject(emptyMap()))
        assertFalse(r.success)
        assertFalse("home must NOT be gated to intent_only_mode when a11y is off",
            detail(r)!!.startsWith("intent_only_mode"))
        assertEquals("app context unavailable", detail(r))
    }

    @Test fun `home goes home via the intent layer with a11y ON (uniform behavior)`() = runBlocking {
        val r = controller().dispatch("home", JsonObject(emptyMap()))
        assertFalse(detail(r)!!.startsWith("intent_only_mode"))
        assertEquals("app context unavailable", detail(r))
    }

    @Test fun `open_app on a missing package is a clear not-found error (not intent_only_mode)`() {
        // PURE decision: a package with no launcher intent → a concrete, actionable error, never a
        // crash and never the generic intent_only_mode. (The launch itself is device-verified.)
        val r = openAppNotFound(launchable = false, packageName = "com.does.not.exist")!!
        assertFalse(r.success)
        assertEquals("app not found or not launchable: com.does.not.exist", r.detail)
        assertFalse("a genuinely-missing app is NOT intent_only_mode",
            r.detail.startsWith("intent_only_mode"))
        // a launchable package proceeds (null → no rejection → the launch runs).
        assertNull(openAppNotFound(launchable = true, packageName = "com.android.settings"))
    }

    // ---- the tolerant intArg helper (pure) ----

    @Test fun `intArg tolerates int float and string forms`() {
        assertEquals(6, intArg(buildJsonObject { put("x", 6) }, "x"))
        assertEquals(6, intArg(buildJsonObject { put("x", 6.0) }, "x"))
        assertEquals(6, intArg(buildJsonObject { put("x", "6") }, "x"))
        assertEquals(null, intArg(buildJsonObject { put("x", "nope") }, "x"))
        assertEquals(null, intArg(JsonObject(emptyMap()), "x"))
    }
}
