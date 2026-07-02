package com.aiblackbox.portal.overlay

import android.view.Display
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
 *
 * ## Display addressing (M5.2)
 * [displayId] supplies the TARGET display for the COORDINATE gestures (`coordinate_tap` /
 * `coordinate_swipe`) — read per dispatch and passed to [Actuators.tap] / [Actuators.swipe] →
 * [GestureDescription.Builder.setDisplayId]. Defaults to [Display.DEFAULT_DISPLAY] (0), so a
 * single-display device is unchanged; a future DeX / external-display target routes its
 * coordinate gestures to the right display without touching the semantic node path (node
 * `ACTION_CLICK` is display-agnostic — it acts on the resolved node wherever it lives).
 *
 * ## XR coordinate gating — defense in depth (M6.1)
 * [capability] is an OPTIONAL live device-capability provider. When it reports
 * `supportsCoordinateGesture=false` (an XR headset — the per-panel 3D compositor has no flat
 * framebuffer), the two COORDINATE dispatch branches (`coordinate_tap` / `coordinate_swipe`)
 * are SKIPPED + logged and return a benign `success=false, "coordinate gestures not supported
 * on <formFactor>"` — element `ACTION_CLICK` + intents + global actions still pass through, so
 * the loop falls back to node+intent actuation. This is DEFENSE IN DEPTH: the frontier
 * `/action` path already gates coordinate actions one layer up in
 * [com.aiblackbox.portal.data.remote.PhoneActionDispatcher] (which returns before the
 * controller is ever called), so on that path this gate never fires; it exists so ANY OTHER
 * caller that reaches the controller directly (a future on-device planner, a test, a
 * refactor) can't dispatch a meaningless XR coordinate gesture either. The default provider
 * `{ null }` means "no gate — behave exactly as before", so phone/tablet/foldable and the
 * two direct-constructor call-sites are byte-for-byte unchanged.
 */
class AndroidPhoneController(
    private val reader: UiTreeReader,
    private val actuators: Actuators,
    private val intentActuator: IntentActuator,
    private val displayId: () -> Int = { Display.DEFAULT_DISPLAY },
    private val capability: () -> DeviceCapabilities? = { null },
    // (M8.1) live a11y-enabled probe for the intent-fallback gate. Default `{ true }` (no gate)
    // so the existing direct-constructor call-sites + unit tests behave exactly as before; the
    // production [fromService] wires `{ BlackBoxA11yService.isConnected() }`, so a disabled /
    // OS-revoked (Advanced Protection) service degrades screen actions to intent_only_mode.
    private val a11yEnabled: () -> Boolean = { true },
) : PhoneController {

    /**
     * (M4) Memoized device capability for the XR coordinate gate. The prod [capability] provider
     * is [DeviceCapabilities.detect], which reads config + the [FoldingFeatureMonitor] + the
     * [android.content.pm.PackageManager] — too expensive to re-run on every coordinate action. The
     * FORM FACTOR (XR vs handheld → `supportsCoordinateGesture`) is stable for the life of a control
     * session, so we probe ONCE per controller instance. (Foldable POSTURE, which DOES change
     * mid-session, is handled separately by the observation path + [FoldingFeatureMonitor]; this
     * gate only needs the posture-stable form factor.) `by lazy` is thread-safe (SYNCHRONIZED),
     * which matters because [dispatch] is a suspend fn that may run off the main thread.
     */
    private val cachedCapability: DeviceCapabilities? by lazy { capability() }

    override suspend fun dispatch(name: String, args: JsonObject): ToolResult {
        return try {
            // (M8.1) a11y-revocation → intent fallback. If the AccessibilityService is disabled or
            // OS-revoked (e.g. Android Advanced Protection), the SCREEN actuators can't run — return
            // a clear intent_only_mode result listing the still-available intent actions instead of
            // failing opaquely/crashing. The INTENT actions (handled in the `else` branch via the
            // Application-Context IntentActuator — no a11y) STILL fire. Re-enabling a11y resumes the
            // tree/gesture path on the very next call (this probes live each dispatch).
            if (name in A11Y_DEPENDENT_ACTIONS && !a11yEnabled()) {
                return intentOnlyModeResult()
            }
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

                // (M2 / F1) press_key — the coordinate-free key action. `enter` submits the
                // focused field (ACTION_IME_ENTER) enabling a "type → submit" flow;
                // back/home/recents reuse performGlobalAction. Key is validated against the
                // schema enum by the remote parser; pressKey degrades gracefully regardless.
                "press_key" -> {
                    val key = args["key"]?.jsonPrimitive?.contentOrNull
                        ?: return ToolResult(false, JsonPrimitive("key required"))
                    actuators.pressKey(key).toToolResult()
                }

                // (M1.3) coordinate_tap — expose the coordinate tap path (the frontier
                // `coordinate_tap` action). Coordinates are absolute screen pixels; the
                // frontier dispatcher already gated coordinate support (skips on XR).
                // (C1, M4) actuators.tap(x,y) is now the GATED coordinate tap: it recovers a
                // label by hit-testing (x,y) and confirms-by-default on an unlabeled/tree-
                // blind/dangerous coordinate in PERMISSION — no compose-then-send bypass.
                "coordinate_tap" -> {
                    // (M6.1) defense-in-depth: skip+report a coordinate gesture on a
                    // coordinate-less device (XR). No-op on phone/tablet (capability null/true).
                    coordinateUnsupported()?.let { return it }
                    val x = intArg(args, "x")
                        ?: return ToolResult(false, JsonPrimitive("x required"))
                    val y = intArg(args, "y")
                        ?: return ToolResult(false, JsonPrimitive("y required"))
                    // (M5.2) route to the target display (default 0) via setDisplayId.
                    actuators.tap(x, y, displayId()).toToolResult()
                }

                // (M1.3) coordinate_swipe — read the explicit segment (x,y)->(x2,y2)
                // (the M0 "swipe" branch read only a `direction`). Optional duration_ms
                // maps to the stroke duration; absent → the 250ms default overload.
                "coordinate_swipe" -> {
                    // (M6.1) defense-in-depth: skip+report on a coordinate-less device (XR).
                    coordinateUnsupported()?.let { return it }
                    val x = intArg(args, "x")
                        ?: return ToolResult(false, JsonPrimitive("x required"))
                    val y = intArg(args, "y")
                        ?: return ToolResult(false, JsonPrimitive("y required"))
                    val x2 = intArg(args, "x2")
                        ?: return ToolResult(false, JsonPrimitive("x2 required"))
                    val y2 = intArg(args, "y2")
                        ?: return ToolResult(false, JsonPrimitive("y2 required"))
                    // (C1, M4) A DEGENERATE swipe (start == end) is a coordinate TAP-equivalent,
                    // not a drag — route it through the GATED coordinate tap so it can't be used
                    // to fire an unconfirmed high-consequence tap disguised as a swipe. A genuine
                    // drag (start != end) is a low-risk scroll/pan and stays ungated.
                    // (M5.2) coordinate gestures are display-addressed (default display 0).
                    val display = displayId()
                    if (x == x2 && y == y2) {
                        actuators.tap(x, y, display).toToolResult()
                    } else {
                        val duration = intArg(args, "duration_ms")?.toLong() ?: Actuators.SWIPE_DURATION_MS
                        actuators.swipe(x, y, x2, y2, duration, display).toToolResult()
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

    /**
     * (M8.1) The graceful a11y-off result: `success=false` with the machine-detectable
     * [INTENT_ONLY_MODE_DETAIL] (starts with the `intent_only_mode` token the /action classifier
     * + frontier loop key on, then lists the still-available intent actions). Never leaks content.
     */
    private fun intentOnlyModeResult(): ToolResult =
        ToolResult(false, JsonPrimitive(INTENT_ONLY_MODE_DETAIL))

    /**
     * (M6.1) The XR coordinate gate. Returns a benign skip [ToolResult] when the memoized
     * [cachedCapability] reports `supportsCoordinateGesture=false` (an XR headset); `null` (proceed)
     * on any device that supports coordinate gestures OR when no capability provider is wired
     * (the default `{ null }` → phone/existing behavior unchanged). The message mirrors the
     * remote dispatcher's wording so the frontier model reads the same signal on either path.
     */
    private fun coordinateUnsupported(): ToolResult? {
        val cap = cachedCapability ?: return null
        if (cap.supportsCoordinateGesture) return null
        val ff = cap.formFactor.name.lowercase()
        android.util.Log.i("AndroidPhoneController", "coordinate action skipped on $ff (no coordinate gesture)")
        return ToolResult(false, JsonPrimitive("coordinate gestures not supported on $ff"))
    }

    companion object {

        /**
         * (M8.1) The on-device actions that REQUIRE the [BlackBoxA11yService] (screen-tree reads +
         * gesture actuation). When a11y is disabled / OS-revoked these degrade to intent_only_mode;
         * the [ResidentTools.INTENT_ACTIONS] (Application-Context intents) are NOT in this set and
         * still fire. `open_app` is here because [Actuators.openApp] launches via the a11y service.
         */
        val A11Y_DEPENDENT_ACTIONS: Set<String> = setOf(
            "read_screen", "tap", "type", "swipe", "scroll", "open_app", "back", "home",
            "recents", "press_key", "coordinate_tap", "coordinate_swipe",
        )

        /**
         * (M8.1) The intent_only_mode detail. Starts with the `intent_only_mode` token the
         * `/action` error classifier ([com.aiblackbox.portal.data.remote.classifyActuatorError]) and
         * the server-side frontier loop key on, then lists the still-available intent actions (from
         * [ResidentTools.INTENT_ACTIONS], never hardcoded) so the driver knows what remains. Carries
         * NO screen text.
         */
        val INTENT_ONLY_MODE_DETAIL: String =
            "intent_only_mode: on-device accessibility is off (or OS-revoked); screen reading, " +
                "taps, typing, and gestures are unavailable. Intent actions still work: " +
                ResidentTools.INTENT_ACTIONS.sorted().joinToString(", ") +
                ". Re-enable BlackBox accessibility to resume screen control."

        /**
         * (M1 / C1 — SUPERSEDED by M4) The fail-safe autonomy posture the boot-survivable REMOTE
         * `/action` path used as a STOPGAP before the real gates were wired: PERMISSION mode +
         * [FailSafeDenyConfirmUi] (every high-consequence confirmation resolves to DENY).
         *
         * As of **M4**, [com.aiblackbox.portal.NotificationListenerFgs] no longer uses these —
         * it wires the REAL [OverlayConfirmUi] (on-device Allow/Deny, fail-safe DENY) + the
         * per-device [com.aiblackbox.portal.data.local.AutonomyStore] reader + [OverlayCredentialHandoff],
         * so high-consequence actions now surface a real confirm instead of blanket-denying.
         * These constants are RETAINED as the named fail-safe primitives (still the correct SAFE
         * fallback for any surface without a real confirm UI) and are guarded by
         * `RemotePhoneControllerAutonomyTest`.
         */
        val M1_REMOTE_AUTONOMY_MODE: () -> AutonomyMode = { AutonomyMode.PERMISSION }

        /** (M1 / C1 — SUPERSEDED by M4) See [M1_REMOTE_AUTONOMY_MODE] — the fail-safe-deny confirm primitive. */
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
         * @param displayId (M5.2) the target display for coordinate gestures. Default
         *   [Display.DEFAULT_DISPLAY] (0). A future multi-display/DeX caller supplies the
         *   attested device's target display here.
         * @param capability (M6.1) the live device-capability provider used by the
         *   defense-in-depth XR coordinate gate. Default = an HONEST [DeviceCapabilities.detect]
         *   over [appContext], probed ONCE per controller instance (M4: memoized into
         *   [cachedCapability] — the form factor is session-stable, so a coordinate tap doesn't
         *   re-run detect each time), so EVERY production controller self-gates coordinate gestures
         *   on an XR headset (`supportsCoordinateGesture=false`) while phone/tablet/foldable are
         *   unaffected. `detect` never throws (degrades to the phone profile), so wiring it is
         *   always safe.
         */
        fun fromService(
            appContext: android.content.Context,
            mode: () -> AutonomyMode = { AutonomyMode.YOLO },
            confirm: ConfirmUi = AutoApproveConfirmUi,
            credentialHandoff: CredentialHandoff = AutoDeclineCredentialHandoff,
            displayId: () -> Int = { Display.DEFAULT_DISPLAY },
            capability: () -> DeviceCapabilities? = { DeviceCapabilities.detect(appContext) },
            // (M8.1) live a11y-enabled probe — the intent-fallback gate. isConnected() ==
            // (instance != null); a disabled / OS-revoked service clears the instance in
            // onUnbind/onDestroy, so screen actions degrade to intent_only_mode and resume
            // the instant the user re-enables it.
            a11yEnabled: () -> Boolean = { BlackBoxA11yService.isConnected() },
        ): AndroidPhoneController =
            AndroidPhoneController(
                UiTreeReader.fromService(),
                Actuators.fromService(mode, confirm, credentialHandoff),
                // Task IA-3 / W0: the intent actions share the SAME autonomy posture +
                // confirm seam as the gestures (its internal gate covers send_*), but
                // fire through the Application context — NO accessibility required.
                IntentActuator.fromAppContext(appContext, mode, confirm),
                displayId = displayId,
                capability = capability,
                a11yEnabled = a11yEnabled,
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
