package com.aiblackbox.portal.overlay

import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
        UiTreeReader { null },
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

    @Test fun `recents reaches the actuator`() = runBlocking {
        val r = controller().dispatch("recents", JsonObject(emptyMap()))
        assertFalse(r.success)
        assertEquals("accessibility service not enabled", detail(r))
    }

    @Test fun `existing back and home still route`() = runBlocking {
        assertEquals("accessibility service not enabled", detail(controller().dispatch("back", JsonObject(emptyMap()))))
        assertEquals("accessibility service not enabled", detail(controller().dispatch("home", JsonObject(emptyMap()))))
    }

    @Test fun `a truly unknown action is still rejected as unknown`() = runBlocking {
        val r = controller().dispatch("levitate", JsonObject(emptyMap()))
        assertFalse(r.success)
        assertTrue(detail(r)!!, detail(r)!!.contains("unknown phone action"))
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
