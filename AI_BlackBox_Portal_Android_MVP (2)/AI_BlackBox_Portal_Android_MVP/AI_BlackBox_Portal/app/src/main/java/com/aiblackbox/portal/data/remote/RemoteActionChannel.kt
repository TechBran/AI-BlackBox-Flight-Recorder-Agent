package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.data.local.PhoneController
import com.aiblackbox.portal.data.local.ResidentTools
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.overlay.AndroidPhoneController
import com.aiblackbox.portal.overlay.DeviceCapabilities
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * (M1.3) The `action`↓ channel core: parse a `docs/schema/action.json` frame and route
 * it through the existing [PhoneController] ([com.aiblackbox.portal.overlay.AndroidPhoneController])
 * to the REAL actuators, returning a schema-conforming `action_result`.
 *
 * Split into a PURE parser ([parseAction]) + a dispatcher ([PhoneActionDispatcher]) over
 * the [PhoneController] interface, so the whole parse→dispatch routing is JVM-unit-tested
 * with a fake controller (no accessibility service, no device). The HTTP shell
 * (`RemoteControlServer.handleActionRequest`) only adds transport framing.
 *
 * ### Action variant → dispatch (closing the M0 gaps)
 * | `type` | dispatch name | actuator |
 * |---|---|---|
 * | `element_click` | `tap` | `Actuators.tap(NodeRef)` (resource_id preferred) |
 * | `element_set_text` | `type` | `Actuators.type(NodeRef, text)` (credential-handoff preserved) |
 * | `coordinate_tap` | `coordinate_tap` **(new)** | `Actuators.tap(x, y)` (exposed dispatchTap) |
 * | `coordinate_swipe` | `coordinate_swipe` **(new coord branch)** | `Actuators.swipe(x,y,x2,y2[,dur])` |
 * | `global_action` back / recents (recents is new) | `back` / `recents` | `performGlobalAction` (a11y) |
 * | `global_action` home | `home` | `IntentActuator.goHome()` — Application Context, NO a11y |
 * | `intent` | *(the intent name)* | `IntentActuator.perform(name, params)` — called generically |
 * | `open_app` | `open_app` | `IntentActuator.openApp(package)` — Application Context, NO a11y |
 * | `scroll` | `scroll` | `Actuators.scroll(direction)` |
 * | `press_key` enter/back/home/recents/tab/delete **(new M2)** | `press_key` | `Actuators.pressKey(key)` (enter→ACTION_IME_ENTER; back/home/recents→performGlobalAction) |
 */

/** A parsed action's category, used for capability-gating (coordinate actions on XR). */
enum class ActionKind { ELEMENT, COORDINATE, GLOBAL, INTENT, OPEN_APP, SCROLL, KEY }

/** (M2 / F1) The valid `press_key` keys — mirrors docs/schema/action.json press_key.key and
 *  the loop-side PRESS_KEYS. A key outside this set is rejected at parse (`invalid_argument`). */
private val PRESS_KEYS = setOf("enter", "back", "home", "recents", "tab", "delete")

/** The pure parse outcome of one action frame: a dispatch plan or a rejection. */
sealed interface ActionParse {
    /**
     * A validated dispatch: [dispatchName] + [args] are exactly what
     * `PhoneController.dispatch` expects, [kind] classifies it for capability-gating.
     */
    data class Plan(val dispatchName: String, val args: JsonObject, val kind: ActionKind) : ActionParse

    /**
     * The frame is malformed / unroutable. [error] is one of the `action_result.error`
     * enum values (`invalid_argument` / `unknown_action`); [detail] is a benign phrase.
     */
    data class Reject(val error: String, val detail: String) : ActionParse
}

/** Tolerant JSON reader for the action frame (extra transport keys — task_id/operator — ignored). */
internal val ACTION_JSON = Json { ignoreUnknownKeys = true }

/**
 * PURE: parse one `action.json` frame ([frame]) into a dispatch [ActionParse.Plan] or an
 * [ActionParse.Reject]. Keyed on the action-variant discriminator `type`. Element refs
 * (`resource_id` / `node_id`) are passed through verbatim so the actuator's own
 * `parseNodeRef` (resource_id-preferred, tolerant node_id) resolves them. No framework —
 * fully unit-testable.
 */
fun parseAction(frame: JsonObject): ActionParse {
    // MINOR (a): if a `msg` message-kind discriminator is present, it MUST be "action" — a
    // frame stamped as an observation / action_result must never be parsed as an action.
    // (Absent `msg` is tolerated: the pure parser is also driven directly in unit tests and
    // the dispatcher's transport shell supplies `msg`.)
    val msg = strField(frame, "msg")
    if (msg != null && msg != WireMessageType.ACTION) {
        return ActionParse.Reject("invalid_argument", "not an action frame")
    }
    val type = strField(frame, "type")
        ?: return ActionParse.Reject("invalid_argument", "action type required")
    return when (type) {
        "element_click" -> {
            if (!hasElementRef(frame)) {
                return ActionParse.Reject("invalid_argument", "resource_id or node_id required")
            }
            ActionParse.Plan("tap", elementRefArgs(frame), ActionKind.ELEMENT)
        }

        "element_set_text" -> {
            if (!hasElementRef(frame)) {
                return ActionParse.Reject("invalid_argument", "resource_id or node_id required")
            }
            val text = strField(frame, "text")
                ?: return ActionParse.Reject("invalid_argument", "text required")
            val args = buildJsonObject {
                strField(frame, "resource_id")?.takeIf { it.isNotBlank() }?.let { put("resource_id", it) }
                intField(frame, "node_id")?.let { put("node_id", it) }
                put("text", text)
            }
            ActionParse.Plan("type", args, ActionKind.ELEMENT)
        }

        "coordinate_tap" -> {
            val x = intField(frame, "x") ?: return ActionParse.Reject("invalid_argument", "x required")
            val y = intField(frame, "y") ?: return ActionParse.Reject("invalid_argument", "y required")
            ActionParse.Plan(
                "coordinate_tap",
                buildJsonObject { put("x", x); put("y", y) },
                ActionKind.COORDINATE,
            )
        }

        "coordinate_swipe" -> {
            val x = intField(frame, "x") ?: return ActionParse.Reject("invalid_argument", "x required")
            val y = intField(frame, "y") ?: return ActionParse.Reject("invalid_argument", "y required")
            val x2 = intField(frame, "x2") ?: return ActionParse.Reject("invalid_argument", "x2 required")
            val y2 = intField(frame, "y2") ?: return ActionParse.Reject("invalid_argument", "y2 required")
            val duration = intField(frame, "duration_ms")
            ActionParse.Plan(
                "coordinate_swipe",
                buildJsonObject {
                    put("x", x); put("y", y); put("x2", x2); put("y2", y2)
                    duration?.let { put("duration_ms", it) }
                },
                ActionKind.COORDINATE,
            )
        }

        "global_action" -> {
            val action = strField(frame, "action")?.trim()?.lowercase()
                ?: return ActionParse.Reject("invalid_argument", "action required")
            when (action) {
                "back", "home", "recents" ->
                    ActionParse.Plan(action, EMPTY_ARGS, ActionKind.GLOBAL)
                else -> ActionParse.Reject("invalid_argument", "unknown global action: $action")
            }
        }

        "intent" -> {
            val name = strField(frame, "name")
                ?: return ActionParse.Reject("invalid_argument", "intent name required")
            // I1: the intent name MUST be a known INTENT_ACTIONS entry. Without this an
            // attacker/model could smuggle a GESTURE/GLOBAL/COORDINATE dispatch name
            // (read_screen, coordinate_tap, tap, back, …) through the `intent` branch —
            // type-confusion that would (a) route a coordinate gesture past the XR
            // capability-gate (which keys on the COORDINATE variant, not on `intent`), and
            // (b) leak screen text via read_screen-as-intent (violating the
            // action_result.detail "never screen text" contract). INTENT_ACTIONS is disjoint
            // from every gesture/global/coordinate dispatch name, so membership fully closes
            // the collision. The intent set is owned by ResidentTools; we never hardcode it.
            if (name !in ResidentTools.INTENT_ACTIONS) {
                return ActionParse.Reject("unknown_action", "unknown intent action: $name")
            }
            // params passed VERBATIM to IntentActuator.perform (via controller.dispatch).
            val params = frame["params"]?.let { it as? JsonObject } ?: EMPTY_ARGS
            ActionParse.Plan(name, params, ActionKind.INTENT)
        }

        "open_app" -> {
            // I2: the wire contract is EXACTLY `package` (action.json open_app,
            // additionalProperties:false). No `package_name` alias on the remote wire — the
            // parser normalizes to `package` and the schema forbids the extra key.
            val pkg = strField(frame, "package")
            if (pkg.isNullOrBlank()) {
                return ActionParse.Reject("invalid_argument", "package required")
            }
            ActionParse.Plan(
                "open_app",
                buildJsonObject { put("package", pkg) },
                ActionKind.OPEN_APP,
            )
        }

        "scroll" -> {
            val direction = strField(frame, "direction")
                ?: return ActionParse.Reject("invalid_argument", "direction required")
            ActionParse.Plan(
                "scroll",
                buildJsonObject { put("direction", direction) },
                ActionKind.SCROLL,
            )
        }

        "press_key" -> {
            // (M2 / F1) The coordinate-free key action. Validate `key` against the schema enum
            // (an unknown key is a caller-input error → invalid_argument, never actuated). KEY
            // kind is NEVER coordinate-gated — press_key uses no coordinates, so it is safe on
            // every form factor incl. XR (enter→IME / back/home/recents→performGlobalAction).
            val key = strField(frame, "key")?.trim()?.lowercase()
                ?: return ActionParse.Reject("invalid_argument", "key required")
            if (key !in PRESS_KEYS) {
                return ActionParse.Reject("invalid_argument", "unknown key: $key")
            }
            ActionParse.Plan("press_key", buildJsonObject { put("key", key) }, ActionKind.KEY)
        }

        else -> ActionParse.Reject("unknown_action", "unknown action type: $type")
    }
}

private val EMPTY_ARGS = JsonObject(emptyMap())

/**
 * (I1) The coordinate-gesture dispatch names — which are EXACTLY the coordinate action
 * VARIANT `type`s. The capability gate keys on these (the variant), not only on the parsed
 * [ActionKind], so a coordinate gesture can never reach the actuators past the
 * `supportsCoordinateGesture=false` (XR) guard regardless of how the frame was shaped.
 */
private val COORDINATE_DISPATCH_NAMES = setOf("coordinate_tap", "coordinate_swipe")

/** True iff the frame carries a usable element handle (non-blank resource_id OR a node_id). */
private fun hasElementRef(frame: JsonObject): Boolean =
    (strField(frame, "resource_id")?.isNotBlank() == true) || intField(frame, "node_id") != null

/** Build the tap/type element args (resource_id preferred, node_id fallback) from the frame. */
private fun elementRefArgs(frame: JsonObject): JsonObject = buildJsonObject {
    strField(frame, "resource_id")?.takeIf { it.isNotBlank() }?.let { put("resource_id", it) }
    intField(frame, "node_id")?.let { put("node_id", it) }
}

/** Content of a string primitive [key], or null. */
private fun strField(frame: JsonObject, key: String): String? =
    frame[key]?.jsonPrimitive?.contentOrNull

/**
 * Tolerant int read of [key]: accepts an int (`5`), a float (`5.0` — some models emit
 * decimals for whole numbers), or a numeric string (`"5"` / `"5.0"`). Null when absent
 * or non-numeric.
 */
private fun intField(frame: JsonObject, key: String): Int? {
    val prim = frame[key]?.jsonPrimitive ?: return null
    prim.intOrNull?.let { return it }
    prim.doubleOrNull?.let { return it.toInt() }
    return prim.contentOrNull?.toDoubleOrNull()?.toInt()
}

/**
 * PURE: map an actuator's failure [detail] to an `action_result.error` enum value, or
 * null when the failure is a USER DECISION (a declined confirm-gate / credential handoff)
 * — the schema requires those carry NO error, just the benign detail. Only called when
 * the actuator reported `success=false`.
 */
fun classifyActuatorError(detail: String): String? {
    val d = detail.lowercase()
    return when {
        // (M8.1) intent-only degradation: the a11y service is off/OS-revoked, so a screen action
        // (tap/type/gesture/global) is unavailable but the INTENT path still works. Checked FIRST
        // — its detail mentions "accessibility" and would otherwise be misread as not_enabled. This
        // is NOT a hard terminal: the frontier loop keys on it to know the device is intent-only.
        d.contains("intent_only_mode") -> "intent_only_mode"
        // User-initiated (decline / handoff) — benign, not an error.
        d.contains("declined") -> null
        d.contains("not enabled") -> "not_enabled"
        d.contains("not found") -> "node_not_found"
        d.contains("unknown phone action") || d.contains("unknown intent action") -> "unknown_action"
        // MINOR (b): bad-ARGUMENT phrases → invalid_argument (not the generic dispatch_failed):
        // a missing required arg ("<key> required"), an unsafe/invalid URI ("invalid url" /
        // "invalid uri" / "unsafe or invalid uri"), and a bad cardinal direction ("unknown
        // swipe/scroll direction: <d>"). These are caller-input errors, not dispatch failures.
        d.contains("required") ||
            d.contains("invalid url") || d.contains("invalid uri") || d.contains("unsafe or invalid") ||
            d.contains("unknown swipe direction") || d.contains("unknown scroll direction") -> "invalid_argument"
        // Everything else that failed is a dispatch/launch failure (gesture rejected,
        // app not installed, intent launch failure, empty detail).
        else -> "dispatch_failed"
    }
}

/**
 * The seam through which `RemoteControlServer.handleActionRequest` turns one action frame
 * into a schema-conforming `action_result`. Implemented by [PhoneActionDispatcher];
 * abstracted so the server route is testable with a fake dispatcher and the dispatcher is
 * testable with a fake controller.
 */
interface RemoteActionDispatcher {
    /** Parse [body], dispatch through the actuators, return the `action_result`. Never throws. */
    suspend fun dispatch(body: String, taskId: String, operator: String): ActionResultEnvelope
}

/**
 * The minimal remote-session signal the dispatcher needs: the kill-switch gate
 * ([isKilled]) + the "session is live" mark ([start], which raises the consent banner).
 * Defaults to the process-wide [RemoteSessionBus]; injected as a fake in tests.
 */
interface SessionSignal {
    fun isKilled(taskId: String): Boolean
    fun start(taskId: String, operator: String)
    /**
     * (M8.2) The wire DETAIL for a killed [taskId] — distinguishes an operator incident-kill
     * ("remote control killed by operator" → the loop's `killed` terminal) from a user STOP
     * ("remote control stopped by user" → `stopped`). Default = the user-stop phrase, so an
     * existing fake SessionSignal (isKilled/start only) keeps the pre-M8 behavior.
     */
    fun killDetail(taskId: String): String = RemoteSessionBus.DETAIL_USER_STOP
}

/** Production [SessionSignal] over the global [RemoteSessionBus]. */
object BusSessionSignal : SessionSignal {
    override fun isKilled(taskId: String): Boolean = RemoteSessionBus.isKilled(taskId)
    override fun start(taskId: String, operator: String) {
        RemoteSessionBus.start(taskId, operator)
    }
    override fun killDetail(taskId: String): String = RemoteSessionBus.killDetail(taskId)
}

/**
 * (M1.3) Dispatches parsed actions through the live [controller]
 * ([com.aiblackbox.portal.overlay.AndroidPhoneController]) — which already wraps every
 * actuator so a failure returns a graceful `ActuatorResult`, never a throw — and maps the
 * outcome to a schema-conforming [ActionResultEnvelope].
 *
 * Enforces two device-side rules the M0 contract requires:
 *  - **Kill switch:** a task the user STOPPED ([SessionSignal.isKilled]) is REFUSED here
 *    (checked both before parse AND after [SessionSignal.start] to close the stop-racing
 *    window, I3), never actuated. "Abort" = every SUBSEQUENT frame for that task is refused;
 *    there is no in-flight cancellation of an actuator call already running.
 *  - **Capability gate:** a coordinate action on a device that doesn't support coordinate
 *    gestures (XR, `supportsCoordinateGesture=false`) is SKIPPED + reported, never
 *    dispatched — the loop falls back to element/intent actuation.
 *
 * On the first actuated action it marks the session live ([SessionSignal.start]) so the
 * consent banner shows. Optionally embeds a fresh [Observation] (when
 * [observationProvider] is wired) so the loop can re-observe without a round-trip.
 *
 * @param capability the live device capability (M1.1) used for the coordinate gate.
 * @param sessionBus the kill-switch + banner signal (default: the process [RemoteSessionBus]).
 * @param observationProvider optional follow-on observation source; null = no embed
 *   (the loop pulls the next observation over `/stream` — the canonical path).
 */
class PhoneActionDispatcher(
    private val controller: PhoneController,
    private val capability: () -> DeviceCapabilities,
    private val sessionBus: SessionSignal = BusSessionSignal,
    private val observationProvider: (suspend () -> Observation?)? = null,
    // (M8.3) records one NON-SENSITIVE step per actuated action (action name + outcome + latency
    // + capture kind). Default = the process-wide store; a fake sink in tests.
    private val telemetry: TelemetrySink = RemoteSessionTelemetry,
    // (M8.3) wall clock seam for the actuation latency measurement (injected in tests).
    private val clockMs: () -> Long = { System.currentTimeMillis() },
) : RemoteActionDispatcher {

    override suspend fun dispatch(body: String, taskId: String, operator: String): ActionResultEnvelope {
        // KILL SWITCH: a stopped task never actuates or resurrects the session. The detail
        // distinguishes an operator incident-kill from a user STOP (M8.2) so the loop can too.
        if (sessionBus.isKilled(taskId)) {
            return ActionResultEnvelope(success = false, detail = sessionBus.killDetail(taskId))
        }

        val frame = try {
            ACTION_JSON.parseToJsonElement(body.ifBlank { "{}" }).jsonObject
        } catch (e: Exception) {
            return ActionResultEnvelope(success = false, error = "invalid_argument", detail = "invalid action frame")
        }

        return when (val parsed = parseAction(frame)) {
            is ActionParse.Reject ->
                ActionResultEnvelope(success = false, error = parsed.error, detail = parsed.detail)

            is ActionParse.Plan -> {
                // Mark the session live (idempotent) so the consent banner shows.
                sessionBus.start(taskId, operator)

                // I3 (kill-switch race): re-check isKilled AFTER start(). A stop() that raced
                // in between the top isKilled() check and start() still refuses THIS frame —
                // start() is itself kill-safe (it won't resurrect a killed task), and this
                // post-start re-check bails the dispatch so nothing actuates. "Abort" means we
                // refuse subsequent frames for the task; there is no in-flight cancellation of
                // an already-running actuator call.
                if (sessionBus.isKilled(taskId)) {
                    return ActionResultEnvelope(success = false, detail = sessionBus.killDetail(taskId))
                }

                val cap = capability()
                // I1(b): gate on the coordinate ACTION VARIANT — dispatchName is the variant
                // `type` for coordinate actions (coordinate_tap / coordinate_swipe) — not only
                // on the parsed kind, so no frame can reach a coordinate gesture past the
                // supportsCoordinateGesture=false (XR) guard.
                val isCoordinate = parsed.kind == ActionKind.COORDINATE ||
                    parsed.dispatchName in COORDINATE_DISPATCH_NAMES
                if (isCoordinate && !cap.supportsCoordinateGesture) {
                    val obs = observationProvider?.invoke()
                    // (M2) a11y OFF also forces supportsCoordinateGesture=false, but the honest
                    // signal then is NOT "this form factor can't do coordinates" (the XR case) — it's
                    // that the WHOLE screen path (tree + taps/typing/gestures/coordinates) is gone
                    // and only the on-device INTENT path remains. Emit the `intent_only_mode` detail
                    // so the loop short-circuits to its `intent_only` terminal (the cloud driver
                    // can't issue intents) instead of misreading it as an XR coordinate rejection.
                    // The XR case (a11y ON, supportsCoordinateGesture=false) keeps its message.
                    val (gateError, gateDetail) = if (!cap.accessibilityEnabled) {
                        "intent_only_mode" to AndroidPhoneController.INTENT_ONLY_MODE_DETAIL
                    } else {
                        "invalid_argument" to
                            "coordinate gestures not supported on ${cap.formFactor.name.lowercase()}"
                    }
                    // (M8.3) the skip is still a step — record it (no coordinates, just the name).
                    telemetry.record(taskId, operator, parsed.dispatchName, false, 0L, captureType(obs))
                    return ActionResultEnvelope(
                        success = false,
                        error = gateError,
                        detail = gateDetail,
                        observation = obs,
                    )
                }

                // Dispatch through the real actuators (AndroidPhoneController never throws).
                val t0 = clockMs()
                val tr = controller.dispatch(parsed.dispatchName, parsed.args)
                val latencyMs = clockMs() - t0
                val detail = toolDetail(tr)
                val obs = observationProvider?.invoke()
                // (M8.3) per-step telemetry: the action NAME, its outcome, the actuation latency,
                // and how the follow-on screen was observed. NO screen/typed text, node content,
                // coordinates, or args — the sink signature can't carry them.
                telemetry.record(taskId, operator, parsed.dispatchName, tr.success, latencyMs, captureType(obs))
                ActionResultEnvelope(
                    success = tr.success,
                    error = if (tr.success) null else classifyActuatorError(detail),
                    detail = detail,
                    observation = obs,
                )
            }
        }
    }

    /** (M8.3) The non-sensitive capture kind for a follow-on [observation]: `screenshot` when it
     *  carried one, `tree_only` when it was tree-only, `none` when no observation was embedded. */
    private fun captureType(observation: Observation?): String = when {
        observation == null -> "none"
        observation.screenshot != null -> "screenshot"
        else -> "tree_only"
    }

    /** Pull the non-sensitive detail phrase the actuator reported (never node/typed text). */
    private fun toolDetail(tr: ToolResult): String =
        (tr.result as? kotlinx.serialization.json.JsonPrimitive)?.contentOrNull ?: ""
}
