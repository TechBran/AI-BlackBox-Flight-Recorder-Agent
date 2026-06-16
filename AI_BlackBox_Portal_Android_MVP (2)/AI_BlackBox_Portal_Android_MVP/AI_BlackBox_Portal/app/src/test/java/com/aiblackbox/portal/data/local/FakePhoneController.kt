package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.serialization.json.JsonObject

/**
 * In-test [PhoneController] double for [FcLoop.runAgent]'s phone-routing branch
 * (Phase 4.5). Stands in for [com.aiblackbox.portal.overlay.AndroidPhoneController]
 * so the on-device phone-actuator ROUTING is exercisable offline, on the JVM, with
 * no accessibility service and no device.
 *
 * **Scriptable:** [resultFn] maps `(name, args) -> ToolResult`; defaults to a
 * success with a `"ok"` detail.
 *
 * **Records** [dispatched] (every `name to args`, in order) for assertions — this
 * is how a test proves a phone-actuator call reached the controller (and NOT the
 * [ToolBridge]).
 */
class FakePhoneController(
    private val resultFn: (name: String, args: JsonObject) -> ToolResult =
        { _, _ -> ToolResult(success = true, result = kotlinx.serialization.json.JsonPrimitive("ok")) },
) : PhoneController {

    /** Every (name, args) dispatched to this controller, in order. */
    val dispatched: MutableList<Pair<String, JsonObject>> = mutableListOf()

    override suspend fun dispatch(name: String, args: JsonObject): ToolResult {
        dispatched.add(name to args)
        return resultFn(name, args)
    }
}
