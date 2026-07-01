package com.aiblackbox.portal.overlay

import com.aiblackbox.portal.data.local.PhoneController
import com.aiblackbox.portal.data.local.ResidentTools
import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
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
 * ## Autonomy gate (4.6)
 * The YOLO-vs-Permission confirm-gate is enforced INSIDE [Actuators] (it needs the
 * resolved node's label + isPassword, which only the actuator has), so this
 * adapter just forwards — the [Actuators] handed in via [fromService] already
 * carry the autonomy [mode][AutonomyMode] reader + the [ConfirmUi]. A
 * high-consequence action the user declines comes back as a normal
 * `success=false, "user declined"` [ToolResult] the model can react to.
 *
 * ## Credential handoff (4.7)
 * Likewise enforced INSIDE [Actuators.type]: a `type` into a password field never
 * sets the model's text — the text is discarded and the user is asked to type the
 * secret directly via [CredentialHandoff]. The model sees only a generic
 * `"user entered their credential"` / `"user declined credential entry"` result;
 * the password reaches it in neither direction.
 *
 * @param reader the redacting UI-tree reader (prod: [UiTreeReader.fromService]).
 * @param actuators the gesture actuators (prod: [Actuators.fromService], wired
 *   with the autonomy mode + overlay confirm + credential handoff).
 * @param intentActuator the intent-action actuator (Task IA-3 / W0; prod:
 *   [IntentActuator.fromAppContext], which fires the OS intents through the
 *   Application [Context] and so needs NO accessibility — only the gesture
 *   [Actuators] do (Gallery parity). Wired with the same autonomy mode + confirm.
 *   A call whose name is in [ResidentTools.INTENT_ACTIONS] is forwarded here.
 */
class AndroidPhoneController(
    private val reader: UiTreeReader,
    private val actuators: Actuators,
    private val intentActuator: IntentActuator,
) : PhoneController {

    override suspend fun dispatch(name: String, args: JsonObject): ToolResult {
        return try {
            when (name) {
                "read_screen" ->
                    // The JSON string is handed to the model AS TEXT (a primitive),
                    // already password-redacted by UiTreeReader.
                    ToolResult(success = true, result = JsonPrimitive(reader.readScreen()))

                "tap" -> {
                    val ref = parseNodeRef(args)
                        ?: return ToolResult(false, JsonPrimitive("node_id or resource_id required"))
                    actuators.tap(ref).toToolResult()
                }

                "type" -> {
                    val ref = parseNodeRef(args)
                        ?: return ToolResult(false, JsonPrimitive("node_id or resource_id required"))
                    val text = args["text"]?.jsonPrimitive?.contentOrNull
                        ?: return ToolResult(false, JsonPrimitive("text required"))
                    // Actuators.type performs the CREDENTIAL HANDOFF (4.7) on a
                    // password field: it DISCARDS this `text` and asks the user to
                    // type the secret themselves. For a non-password field it sets
                    // the text normally. Either way the typed text is never logged or
                    // echoed into the result detail — we forward it verbatim.
                    actuators.type(ref, text).toToolResult()
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

                // (M1.3) recents — the third global action (back/home already existed);
                // routes to the new Actuators.recents() → GLOBAL_ACTION_RECENTS.
                "recents" -> actuators.recents().toToolResult()

                // (M1.3) coordinate_tap — expose the coordinate tap path (the frontier
                // `coordinate_tap` action). Coordinates are absolute screen pixels; the
                // frontier dispatcher already gated coordinate support (skips on XR).
                "coordinate_tap" -> {
                    val x = intArg(args, "x")
                        ?: return ToolResult(false, JsonPrimitive("x required"))
                    val y = intArg(args, "y")
                        ?: return ToolResult(false, JsonPrimitive("y required"))
                    actuators.tap(x, y).toToolResult()
                }

                // (M1.3) coordinate_swipe — read the explicit segment (x,y)->(x2,y2)
                // (the M0 "swipe" branch read only a `direction`). Optional duration_ms
                // maps to the stroke duration; absent → the 250ms default overload.
                "coordinate_swipe" -> {
                    val x = intArg(args, "x")
                        ?: return ToolResult(false, JsonPrimitive("x required"))
                    val y = intArg(args, "y")
                        ?: return ToolResult(false, JsonPrimitive("y required"))
                    val x2 = intArg(args, "x2")
                        ?: return ToolResult(false, JsonPrimitive("x2 required"))
                    val y2 = intArg(args, "y2")
                        ?: return ToolResult(false, JsonPrimitive("y2 required"))
                    val duration = intArg(args, "duration_ms")
                    if (duration != null) {
                        actuators.swipe(x, y, x2, y2, duration.toLong()).toToolResult()
                    } else {
                        actuators.swipe(x, y, x2, y2).toToolResult()
                    }
                }

                // Task IA-3: an intent action (e.g. show_map, dial, set_alarm) is a
                // deterministic single-shot OS intent — forward it to the
                // IntentActuator, which builds + fires the stock Android intent and
                // applies the send_* autonomy gate internally. Anything else is unknown.
                else -> if (name in ResidentTools.INTENT_ACTIONS) {
                    intentActuator.perform(name, args).toToolResult()
                } else {
                    ToolResult(false, JsonPrimitive("unknown phone action: $name"))
                }
            }
        } catch (e: Exception) {
            // NEVER throw, NEVER leak content: class name only (matches the
            // actuators' own logging discipline).
            ToolResult(false, JsonPrimitive("${name} failed (${e.javaClass.simpleName})"))
        }
    }

    /** Map an [ActuatorResult] to a [ToolResult], carrying ONLY the actuator's own detail. */
    private fun ActuatorResult.toToolResult(): ToolResult =
        ToolResult(success = success, result = JsonPrimitive(detail))

    companion object {

        /**
         * (M1 / C1) The FAIL-SAFE autonomy posture for the boot-survivable REMOTE `/action`
         * path ([com.aiblackbox.portal.data.remote.PhoneActionDispatcher], wired in
         * [com.aiblackbox.portal.NotificationListenerFgs]). It is the SAFE default the remote
         * dispatcher MUST construct its controller with — NOT the un-wired [fromService]
         * defaults (`{ YOLO }` + [AutoApproveConfirmUi]), which would fire high-consequence
         * actions with no confirmation.
         *
         * [M1_REMOTE_AUTONOMY_MODE] is [AutonomyMode.PERMISSION] (so high-consequence actions
         * gate) and [M1_REMOTE_CONFIRM] is [FailSafeDenyConfirmUi] (so every gated confirmation
         * resolves to DENY). Net effect: safe navigation/typing/open_app/scroll/read + benign
         * intents WORK (the M2 loop can drive), while send_email/send_sms/send_intent and
         * send/pay/delete/post/confirm taps are REFUSED. Credential handoff is separately
         * fail-safe ([AutoDeclineCredentialHandoff], the [fromService] default).
         *
         * TODO(M4): replace both with the real [OverlayConfirmUi] + an AutonomyStore-backed
         * per-device mode reader (real per-device autonomy + on-screen confirm).
         */
        val M1_REMOTE_AUTONOMY_MODE: () -> AutonomyMode = { AutonomyMode.PERMISSION }

        /** (M1 / C1) See [M1_REMOTE_AUTONOMY_MODE] — the fail-safe-deny confirm for the remote path. */
        val M1_REMOTE_CONFIRM: ConfirmUi = FailSafeDenyConfirmUi

        /**
         * Production factory: reads + actuates the GESTURE layer through the live
         * connected [BlackBoxA11yService] via the singleton seams (safe even when
         * the service is disabled — the reader/actuators degrade gracefully), while
         * the INTENT layer fires through [appContext]'s Application [Context] and
         * needs NO accessibility at all (Gallery parity, Task W0).
         *
         * @param appContext any [Context]; its application context is the long-lived
         *   launch Context handed to [IntentActuator.fromAppContext].
         * @param mode reads the device autonomy posture for the actuator's gate
         *   (prod wiring passes a SharedPref-backed read defaulting to
         *   [AutonomyMode.PERMISSION] — the SAFE default). Defaults to YOLO here
         *   ONLY so an un-wired call keeps pre-4.6 behavior; ChatViewModel supplies
         *   the safe reader.
         * @param confirm the user-confirmation seam (prod: [OverlayConfirmUi]).
         * @param credentialHandoff the password-entry handoff seam (Task 4.7; prod:
         *   [OverlayCredentialHandoff]). Default auto-declines so an un-wired call
         *   fails SAFE — a password entry never silently proceeds.
         */
        fun fromService(
            appContext: android.content.Context,
            mode: () -> AutonomyMode = { AutonomyMode.YOLO },
            confirm: ConfirmUi = AutoApproveConfirmUi,
            credentialHandoff: CredentialHandoff = AutoDeclineCredentialHandoff,
        ): AndroidPhoneController =
            AndroidPhoneController(
                UiTreeReader.fromService(),
                Actuators.fromService(mode, confirm, credentialHandoff),
                // Task IA-3 / W0: the intent actions share the SAME autonomy posture +
                // confirm seam as the gestures (its internal gate covers send_*), but
                // fire through the Application context — NO accessibility required.
                IntentActuator.fromAppContext(appContext, mode, confirm),
            )
    }
}

/**
 * PURE, tolerant parse of the `node_id` arg (accepts "node_id" or "nodeId").
 *
 * On-device device verification (4.8) surfaced that Gemma emits JSON numbers with
 * a decimal point — e.g. `{"node_id": 6.0}` — so a strict `intOrNull` (which does
 * `"6.0".toIntOrNull()` → null) silently dropped every tap/type. We therefore
 * accept an int (`6`), a float (`6.0` → 6 via [doubleOrNull]), and a string form
 * (`"6"`/`"6.0"`). Returns null only when truly absent / non-numeric. Top-level so
 * it is JVM-unit-testable without the framework actuators.
 */
internal fun parseNodeId(args: JsonObject): Int? {
    val prim = (args["node_id"] ?: args["nodeId"])?.jsonPrimitive ?: return null
    prim.intOrNull?.let { return it }
    prim.doubleOrNull?.let { return it.toInt() }
    return prim.contentOrNull?.toDoubleOrNull()?.toInt()
}

/**
 * PURE, tolerant read of an integer arg [key] (used by the coordinate actions for
 * x/y/x2/y2/duration_ms). Mirrors [parseNodeId]'s tolerance: accepts an int (`6`), a
 * float (`6.0`, since some models emit decimals for whole numbers), or a numeric string
 * (`"6"` / `"6.0"`). Returns null when absent / non-numeric. Top-level so it is
 * JVM-unit-testable without the framework actuators.
 */
internal fun intArg(args: JsonObject, key: String): Int? {
    val prim = args[key]?.jsonPrimitive ?: return null
    prim.intOrNull?.let { return it }
    prim.doubleOrNull?.let { return it.toInt() }
    return prim.contentOrNull?.toDoubleOrNull()?.toInt()
}

/**
 * PURE selection of HOW a tap/type target was addressed, for a `tap`/`type` call:
 *
 *  1. PREFER `resource_id` — the STABLE dev-assigned `viewIdResourceName` handle
 *     from read_screen (e.g. `com.android.settings:id/title`). Unlike `node_id` (a
 *     positional DFS index that DRIFTS when the screen changes between read_screen
 *     and the tap), a resource id doesn't move with insertions, so the tap can't
 *     miss. If present and non-blank → [NodeRef.ById].
 *  2. Otherwise fall back to `node_id` via the tolerant [parseNodeId] (int / float
 *     `6.0` / string forms) → [NodeRef.ByIndex]. This covers nodes with no resource
 *     id (Compose / custom / WebView).
 *  3. Neither present → null (caller returns "node_id or resource_id required").
 *
 * resource_id WINS when both are supplied. Top-level + pure so the selection logic
 * is JVM-unit-testable without the framework actuators.
 */
internal fun parseNodeRef(args: JsonObject): NodeRef? {
    val resourceId = args["resource_id"]?.jsonPrimitive?.contentOrNull?.takeIf { it.isNotBlank() }
    if (resourceId != null) return NodeRef.ById(resourceId)
    return parseNodeId(args)?.let { NodeRef.ByIndex(it) }
}
