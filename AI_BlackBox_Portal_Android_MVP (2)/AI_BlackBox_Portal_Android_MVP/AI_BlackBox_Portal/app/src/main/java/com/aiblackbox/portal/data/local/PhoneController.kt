package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.serialization.json.JsonObject

/**
 * The seam through which [FcLoop.runAgent] dispatches a RESIDENT on-device
 * phone-actuator call (Phase 4, Task 4.5).
 *
 * The on-device Gemma agent drives the owner's OWN phone — at the owner's
 * request, through the consented accessibility service — by emitting tool calls
 * whose names are in [ResidentTools.PHONE_ACTUATORS] (`read_screen`, `tap`,
 * `type`, `swipe`, `scroll`, `open_app`, `back`, `home`). These calls are LOCAL:
 * they actuate the device directly via the accessibility service and must NEVER
 * be routed to the cloud [ToolBridge] (see [FcLoop.runAgent]'s phone branch).
 *
 * This is the seam the loop is tested against with a fake; the production
 * implementation is [com.aiblackbox.portal.overlay.AndroidPhoneController],
 * which reads the live screen ([com.aiblackbox.portal.overlay.UiTreeReader]) and
 * performs gestures ([com.aiblackbox.portal.overlay.Actuators]).
 *
 * ## Where the autonomy gate (Task 4.6) lives
 * The confirm-gate (YOLO vs Permission, high-consequence actions) landed INSIDE
 * the actuator layer ([com.aiblackbox.portal.overlay.Actuators] tap/type), not as
 * a decorator on this seam — because the gate needs the RESOLVED node's label +
 * isPassword, which only the actuator has. So a phone-action call passing through
 * [dispatch] is already gated downstream. The credential handoff (4.7) layers on
 * after.
 *
 * ## Contract
 * - [dispatch] NEVER throws — every outcome is returned as a [ToolResult] the
 *   loop feeds back to the model.
 * - It MUST NOT leak screen text or the typed `text` argument beyond whatever the
 *   underlying actuator already reports in its result detail.
 */
interface PhoneController {

    /**
     * Dispatch a resident phone-actuator call. [name] is one of
     * [ResidentTools.PHONE_ACTUATORS]; [args] is the model-supplied argument
     * object. Returns a [ToolResult] (never throws).
     */
    suspend fun dispatch(name: String, args: JsonObject): ToolResult
}
