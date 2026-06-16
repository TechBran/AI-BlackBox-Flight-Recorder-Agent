package com.aiblackbox.portal.overlay

import com.aiblackbox.portal.data.local.PhoneController
import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * Production [PhoneController] (Phase 4, Task 4.5): maps a resident on-device
 * phone-actuator call to the live [UiTreeReader] / [Actuators] over the consented
 * [BlackBoxA11yService]. This is the seam through which the on-device Gemma agent
 * drives the owner's OWN phone — at the owner's request, through the accessibility
 * service the owner enabled and can disable at any time.
 *
 * ## Framework, device-verified (not unit-tested here)
 * This class is a thin framework shell over [UiTreeReader.readScreen] and the
 * [Actuators] gestures; both depend on the live accessibility service, so this
 * adapter is verified on a device in Task 4.8 (the [FcLoop] ROUTING that calls it
 * IS unit-tested, against a fake [PhoneController]). The actuators already return
 * a graceful `success=false, "accessibility service not enabled"` when the service
 * is off, and `readScreen()` returns `"[]"`, so it is safe to always wire this.
 *
 * ## Contract honored here
 * - [dispatch] NEVER throws: any unexpected error is caught and returned as a
 *   `success=false` [ToolResult] carrying ONLY the exception's class name.
 * - It NEVER leaks screen text or the typed `text` argument beyond what the
 *   underlying actuator already reports (the actuators themselves never echo typed
 *   text or node content; this adapter adds nothing). `read_screen`'s JSON is
 *   already redacted at the boundary (password fields → placeholder).
 *
 * ## Where the autonomy gate (4.6) wraps
 * 4.6 will wrap [PhoneController.dispatch] (a decorating controller or an in-loop
 * guard) to confirm high-consequence actions before they reach these actuators.
 * Not implemented here.
 *
 * @param reader the redacting UI-tree reader (prod: [UiTreeReader.fromService]).
 * @param actuators the gesture actuators (prod: [Actuators.fromService]).
 */
class AndroidPhoneController(
    private val reader: UiTreeReader,
    private val actuators: Actuators,
) : PhoneController {

    override suspend fun dispatch(name: String, args: JsonObject): ToolResult {
        return try {
            when (name) {
                "read_screen" ->
                    // The JSON string is handed to the model AS TEXT (a primitive),
                    // already password-redacted by UiTreeReader.
                    ToolResult(success = true, result = JsonPrimitive(reader.readScreen()))

                "tap" -> {
                    val nodeId = nodeId(args)
                        ?: return ToolResult(false, JsonPrimitive("node_id required"))
                    actuators.tap(nodeId).toToolResult()
                }

                "type" -> {
                    val nodeId = nodeId(args)
                        ?: return ToolResult(false, JsonPrimitive("node_id required"))
                    val text = args["text"]?.jsonPrimitive?.contentOrNull
                        ?: return ToolResult(false, JsonPrimitive("text required"))
                    // Actuators.type REFUSES password fields (hard safety floor) and
                    // never logs/echoes the typed text — we forward it verbatim and
                    // do NOT put it in the result detail.
                    actuators.type(nodeId, text).toToolResult()
                }

                "swipe" -> {
                    val direction = args["direction"]?.jsonPrimitive?.contentOrNull
                        ?: return ToolResult(false, JsonPrimitive("direction required"))
                    actuators.swipe(direction).toToolResult()
                }

                "scroll" -> {
                    val direction = args["direction"]?.jsonPrimitive?.contentOrNull
                        ?: return ToolResult(false, JsonPrimitive("direction required"))
                    actuators.scroll(direction).toToolResult()
                }

                "open_app" -> {
                    val pkg = (args["package"] ?: args["package_name"])
                        ?.jsonPrimitive?.contentOrNull
                        ?: return ToolResult(false, JsonPrimitive("package required"))
                    actuators.openApp(pkg).toToolResult()
                }

                "back" -> actuators.back().toToolResult()
                "home" -> actuators.home().toToolResult()

                else -> ToolResult(false, JsonPrimitive("unknown phone action: $name"))
            }
        } catch (e: Exception) {
            // NEVER throw, NEVER leak content: class name only (matches the
            // actuators' own logging discipline).
            ToolResult(false, JsonPrimitive("${name} failed (${e.javaClass.simpleName})"))
        }
    }

    /** Read `node_id` (accept "node_id" or "nodeId") as an int, or null if absent/non-int. */
    private fun nodeId(args: JsonObject): Int? =
        (args["node_id"] ?: args["nodeId"])?.jsonPrimitive?.intOrNull

    /** Map an [ActuatorResult] to a [ToolResult], carrying ONLY the actuator's own detail. */
    private fun ActuatorResult.toToolResult(): ToolResult =
        ToolResult(success = success, result = JsonPrimitive(detail))

    companion object {
        /**
         * Production factory: reads + actuates through the live connected
         * [BlackBoxA11yService] via the singleton seams. Safe to call even when the
         * service is disabled — the underlying reader/actuators degrade gracefully.
         */
        fun fromService(): AndroidPhoneController =
            AndroidPhoneController(UiTreeReader.fromService(), Actuators.fromService())
    }
}
