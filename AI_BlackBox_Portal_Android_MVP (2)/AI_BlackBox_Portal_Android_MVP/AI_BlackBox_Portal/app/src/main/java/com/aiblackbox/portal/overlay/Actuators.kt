package com.aiblackbox.portal.overlay

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo

/**
 * Gesture ACTUATORS for the on-device phone-control agent (Phase 4, Task 4.3).
 *
 * These are the raw mechanism by which the on-device Gemma agent performs
 * taps / typing / swipes / scrolls / app-launches / back / home **on the
 * owner's OWN phone, at their request**, through the consented
 * [BlackBoxA11yService]. This is a legitimate, user-enabled capability: the user
 * turns the accessibility service on from system settings and can disable it at
 * any time.
 *
 * ## Relationship to read_screen (4.2)
 * `read_screen` emits actionable nodes whose `node_id` is the *dense DFS index*
 * over actionable nodes. A `node_id` is NOT a durable handle. To act on one, an
 * actuator RE-WALKS the current tree with the identical filter + DFS order via
 * [UiTreeReader.findActionableNode] and takes the node at that index. The tree
 * may have shifted slightly since the read — that's acceptable; we act on the
 * best-effort positional match, or return a clear "node N not found" result.
 *
 * ## Result, not exceptions
 * Every method returns a small [ActuatorResult] so the agent loop (4.5) can feed
 * the outcome back to the model. Nothing throws: a missing/disabled service
 * (`service() == null`) yields `success=false, detail="accessibility service not
 * enabled"` and a missing node yields `success=false, detail="node N not found"`.
 *
 * ## Safety floors enforced HERE
 * - [type] never sets text on a password field. Instead of the bare 4.3 refusal,
 *   it now performs the CREDENTIAL HANDOFF (Task 4.7): the model's attempted text
 *   is DISCARDED and the USER is asked to type the secret directly into the field
 *   (see [type] / [credentialHandoff]). The password reaches the model in NEITHER
 *   direction — read_screen redacts it (4.2) and the model's text is never typed.
 * - The autonomy confirm-gate (Task 4.6) is enforced HERE for high-consequence
 *   `tap`/`type` actions: in [AutonomyMode.PERMISSION] the actuator asks [confirm]
 *   (the user) BEFORE firing a send/pay/delete/post/install tap or a sensitive
 *   type, and aborts with `"user declined"` if denied; in [AutonomyMode.YOLO]
 *   high-consequence actions run immediately. Benign actions never gate.
 *
 * ## Logging discipline (leak vector)
 * Logs emit ONLY `nodeId` / action name / coarse result detail. They MUST NEVER
 * emit the [type] `text` argument or any node's screen text/content. The gate
 * likewise NEVER passes a password node's text into the confirm message (the
 * label is null for a password target — see [tap]/[type]).
 *
 * ## Scope
 * Builds the gestures/intents and performs the actions, and enforces the autonomy
 * gate around the high-consequence ones. Does NOT capture screenshots (4.4) or
 * handle credential autofill (4.7).
 *
 * @param service seam to the connected service (prod: `{ BlackBoxA11yService.instance }`).
 * @param mode reads the current device autonomy posture each time it's needed
 *   (prod: a SharedPref-backed read; default `{ AutonomyMode.YOLO }` so existing
 *   call-sites/tests that don't wire a gate behave exactly as before — the SAFE
 *   PERMISSION default is supplied by the production wiring, not this constructor).
 * @param confirm the user-confirmation seam for high-consequence actions in
 *   Permission mode (prod: [OverlayConfirmUi]; default auto-approve no-op so
 *   un-wired call-sites are unaffected).
 * @param credentialHandoff the seam that asks the USER to type a password directly
 *   into the field when the model targets a password field (Task 4.7; prod:
 *   [OverlayCredentialHandoff]). Default [AutoDeclineCredentialHandoff] auto-declines,
 *   so an un-wired call-site fails SAFE (password entry never silently proceeds).
 * @param coordinateLabeler (C1, M4) recovers a redaction-safe LABEL for a COORDINATE
 *   tap by hit-testing `(x,y)` against the live a11y tree (prod default:
 *   [UiTreeReader.labelAtPoint] over [service]). Returns [CoordinateHit.None] when the
 *   coordinate resolves to no node — the FAIL-SAFE the coordinate gate treats as
 *   high-consequence (confirm-by-default in PERMISSION). Injected as a fake in unit
 *   tests to exercise the resolved-benign / resolved-dangerous / unresolved paths.
 */
class Actuators(
    private val service: () -> BlackBoxA11yService?,
    private val mode: () -> AutonomyMode = { AutonomyMode.YOLO },
    private val confirm: ConfirmUi = AutoApproveConfirmUi,
    private val credentialHandoff: CredentialHandoff = AutoDeclineCredentialHandoff,
    private val coordinateLabeler: (Int, Int) -> CoordinateHit = defaultCoordinateLabeler(service),
) {

    /**
     * Tap the node identified by [ref] — a STABLE [NodeRef.ById] resource id
     * (preferred; doesn't drift when the screen changes) OR a positional
     * [NodeRef.ByIndex] `node_id` (fallback, for nodes without a resource id).
     *
     * Resolves the node via [resolve]. If found and
     * [AccessibilityNodeInfo.isClickable], performs a semantic
     * [AccessibilityNodeInfo.ACTION_CLICK] (more reliable than a coordinate tap).
     * Otherwise falls back to dispatching a touch gesture at the node's
     * on-screen bounds center. A null node → `success=false, "node N not found"`.
     *
     * **Autonomy gate (4.6):** once the node is resolved, computes whether this is
     * a high-consequence tap (send/pay/delete/post/install… by the node's label)
     * and, in [AutonomyMode.PERMISSION], asks [confirm] BEFORE acting — returning
     * `success=false, "user declined"` if the user denies. The label fed to the
     * gate is the RESOLVED node's NON-password text (null for a password node,
     * which a tap never targets in practice), so no secret can leak into the
     * confirm message. The gate/redaction operate on the resolved node regardless
     * of how it was addressed (resource id vs index).
     */
    suspend fun tap(ref: NodeRef): ActuatorResult {
        val svc = service() ?: return notEnabled()
        val node = resolve(svc, ref) ?: return nodeNotFound(ref)

        // Autonomy gate: never read a password node's text into the label. Use the
        // same text-or-contentDescription label read_screen uses, so an icon-only
        // high-consequence button (label lives in contentDescription) still gates.
        val isPasswordTarget = isPasswordField(node.isPassword, node.inputType)
        val label = if (isPasswordTarget) null else (node.text ?: node.contentDescription)?.toString()
        gate("tap", label, isPasswordTarget)?.let { return it }

        val target = ref.describe()
        return try {
            // Device finding (4.8): the model often targets a non-clickable leaf
            // (e.g. the "☰" TextView) whose CLICK handler lives on a parent
            // container. A coordinate-gesture at the leaf's center is unreliable
            // (hits nothing visible). So prefer a SEMANTIC click on the node itself
            // or its nearest clickable ancestor (ACTION_CLICK is reliable, no
            // coordinates); only fall back to a gesture when nothing in the
            // ancestor chain is clickable.
            val clickTarget = clickableSelfOrAncestor(node)
            if (clickTarget != null) {
                val ok = clickTarget.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                logAction("tap", target, ok)
                ActuatorResult(ok, if (ok) "tapped $target" else "click action rejected for $target")
            } else {
                // No clickable self-or-ancestor: last-resort touch gesture at center.
                val rect = android.graphics.Rect()
                node.getBoundsInScreen(rect)
                val cx = rect.centerX()
                val cy = rect.centerY()
                val ok = dispatchTap(svc, cx, cy)
                logAction("tap(gesture)", target, ok)
                ActuatorResult(ok, if (ok) "tapped $target at center" else "tap gesture dispatch failed for $target")
            }
        } catch (e: Exception) {
            // Never leak node content in the message — only the action + class.
            logActionError("tap", target, e)
            ActuatorResult(false, "tap failed for $target (${e.javaClass.simpleName})")
        }
    }

    /**
     * Positional-index convenience overload, kept so existing call-sites / tests
     * that tap by raw `node_id` are unaffected. Equivalent to
     * `tap(NodeRef.ByIndex(nodeId))`.
     */
    suspend fun tap(nodeId: Int): ActuatorResult = tap(NodeRef.ByIndex(nodeId))

    /**
     * (M1.3) COORDINATE tap at absolute screen pixel ([x], [y]) via [dispatchGesture] —
     * the public entry point for the frontier `coordinate_tap` action. It exposes the
     * previously-private [dispatchTap] (which existed only as the internal fallback inside
     * [tap] `(NodeRef)`).
     *
     * **Autonomy gate (C1, M4) — a coordinate tap CANNOT bypass the confirm-gate.** Unlike
     * an element tap there is no resolved node handed in, so first we RECOVER a label by
     * hit-testing `(x,y)` against the live a11y tree ([coordinateLabeler] →
     * [UiTreeReader.labelAtPoint]). The pure [isHighConsequenceCoordinateTap] then decides:
     * a coordinate that resolves to a clearly-benign labeled element may skip the confirm,
     * but an UNRESOLVED / unlabeled / dangerous coordinate is high-consequence and CONFIRMS
     * in [AutonomyMode.PERMISSION] (fires unattended in YOLO — the same [shouldConfirm] mode
     * rule as every gate). A DENIED tap returns a clean `success=false, "user declined"` and
     * never actuates.
     *
     * Coordinate capability itself is ALSO gated UPSTREAM (the frontier dispatcher skips
     * coordinate actions when `supportsCoordinateGesture=false`, e.g. XR) — that XR gate is
     * unchanged and independent of this autonomy gate. Never throws; a disabled service →
     * `accessibility service not enabled`.
     */
    suspend fun tap(x: Int, y: Int): ActuatorResult {
        // C1: recover a label + gate BEFORE the service check, so the confirm-gate is
        // consulted even for an unresolved/tree-blind coordinate (fail-safe high-consequence).
        coordinateGate(coordinateLabeler(x, y))?.let { return it }

        val svc = service() ?: return notEnabled()
        return try {
            val ok = dispatchTap(svc, x, y)
            logGesture("tap(coord)", ok)
            ActuatorResult(ok, if (ok) "tapped ($x,$y)" else "tap gesture dispatch failed at ($x,$y)")
        } catch (e: Exception) {
            Log.w(TAG, "coordinate tap failed (${e.javaClass.simpleName})")
            ActuatorResult(false, "tap failed (${e.javaClass.simpleName})")
        }
    }

    /**
     * Return [node] if it is itself clickable, else its nearest clickable ANCESTOR
     * (walking up `parent`, bounded by [maxDepth] hops so we never climb to the
     * whole window/root). Returns null if nothing in the chain is clickable.
     * Lets a tap on a non-clickable leaf (a label/icon) activate the real button.
     */
    private fun clickableSelfOrAncestor(
        node: AccessibilityNodeInfo,
        maxDepth: Int = 6,
    ): AccessibilityNodeInfo? {
        var cur: AccessibilityNodeInfo? = node
        var depth = 0
        while (cur != null && depth <= maxDepth) {
            if (cur.isClickable) return cur
            cur = cur.parent
            depth++
        }
        return null
    }

    /**
     * Set [text] on the editable node identified by [ref] — a STABLE
     * [NodeRef.ById] resource id (preferred) OR a positional [NodeRef.ByIndex]
     * `node_id` (fallback). Resolved via [resolve].
     *
     * **CREDENTIAL HANDOFF (Task 4.7) — HARD SAFETY FLOOR:** if the resolved node
     * is a password field ([UiTreeReader.isPasswordField] over
     * `isPassword`+`inputType`), this NEVER types the model's [text]. Per
     * [credentialDecision] (with `hasSavedCredential = false` — Credential Manager
     * autofill is DEFERRED) it takes the USER HANDOFF: the model's attempted [text]
     * is DISCARDED on the floor and [credentialHandoff] asks the USER to type their
     * password directly into the field. On success → `success=true, "user entered
     * their credential"` (the model continues, e.g. taps Sign In next); on
     * decline/cancel → `success=false, "user declined credential entry"`. The model
     * learns the password in NEITHER direction.
     *
     * The [text] argument is NEVER logged (leak vector), NEVER passed into the
     * autonomy confirm message (4.6), and — critically for 4.7 — NEVER passed into
     * the handoff prompt: the handoff is fed only the GENERIC
     * [CREDENTIAL_FIELD_DESCRIPTION], and for a password target [text] is discarded
     * before the handoff is even called.
     *
     * **Autonomy gate (4.6):** a password type never reaches the gate (it diverts to
     * the handoff above). A non-password type is benign and does not gate today; the
     * gate call is present so any FUTURE sensitive non-password type is covered by
     * the same Permission-mode confirm.
     */
    suspend fun type(ref: NodeRef, text: String): ActuatorResult {
        val svc = service() ?: return notEnabled()
        val node = resolve(svc, ref) ?: return nodeNotFound(ref)
        val target = ref.describe()

        val isPasswordTarget = isPasswordField(node.isPassword, node.inputType)
        when (credentialDecision(isPasswordTarget, hasSavedCredential = false)) {
            // SAFETY FLOOR (4.7): a password target NEVER types the model's text.
            // Discard it and hand entry back to the user. SYSTEM_AUTOFILL is
            // DEFERRED (Credential Manager picker) — for v1 it shares the handoff
            // path so logins still work, and is unreachable today because the
            // call-site passes hasSavedCredential = false.
            CredentialAction.USER_HANDOFF, CredentialAction.SYSTEM_AUTOFILL -> {
                // `text` is NEVER read, logged, or forwarded — it is discarded here.
                logAction("type(credential-handoff)", target, true)
                val entered = credentialHandoff.requestUserEntry(CREDENTIAL_FIELD_DESCRIPTION)
                return if (entered) {
                    ActuatorResult(true, "user entered their credential")
                } else {
                    ActuatorResult(false, "user declined credential entry")
                }
            }
            CredentialAction.TYPE_NORMAL -> { /* fall through to the normal type path */ }
        }

        return try {
            // Autonomy gate for a non-password type. Label is the field's text
            // (the FIELD name/placeholder), never the value being typed.
            gate("type", node.text?.toString(), isPasswordTarget = false)?.let { return it }

            val args = Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            }
            val ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            // NOTE: deliberately NOT logging `text` — only the target + result.
            logAction("type", target, ok)
            ActuatorResult(ok, if (ok) "set text on $target" else "set-text action rejected for $target")
        } catch (e: Exception) {
            logActionError("type", target, e)
            ActuatorResult(false, "type failed for $target (${e.javaClass.simpleName})")
        }
    }

    /**
     * Positional-index convenience overload, kept so existing call-sites / tests
     * that type by raw `node_id` are unaffected. Equivalent to
     * `type(NodeRef.ByIndex(nodeId), text)`.
     */
    suspend fun type(nodeId: Int, text: String): ActuatorResult = type(NodeRef.ByIndex(nodeId), text)

    /**
     * Resolve a [NodeRef] to a live [AccessibilityNodeInfo] on the current tree:
     *  - [NodeRef.ById]    → [UiTreeReader.findNodeByResourceId] (STABLE — keyed on
     *    the resource id, doesn't drift when the screen changes).
     *  - [NodeRef.ByIndex] → [UiTreeReader.findActionableNode] (positional,
     *    best-effort; the legacy path for nodes with no resource id).
     * Returns null when nothing matches (caller maps null to "not found").
     */
    private fun resolve(svc: BlackBoxA11yService, ref: NodeRef): AccessibilityNodeInfo? {
        val root = svc.rootInActiveWindow
        return when (ref) {
            is NodeRef.ById -> UiTreeReader.findNodeByResourceId(root, ref.resourceId)
            is NodeRef.ByIndex -> UiTreeReader.findActionableNode(root, ref.index)
        }
    }

    /**
     * Swipe in a cardinal [direction] ("up"/"down"/"left"/"right") — a centered
     * swipe across the current screen via [dispatchGesture]. Unknown direction →
     * `success=false`.
     */
    fun swipe(direction: String): ActuatorResult {
        val svc = service() ?: return notEnabled()
        val metrics = svc.resources.displayMetrics
        val coords = swipeCoords(direction, metrics.widthPixels, metrics.heightPixels)
            ?: return ActuatorResult(false, "unknown swipe direction: $direction")
        return swipe(coords[0], coords[1], coords[2], coords[3])
    }

    /**
     * Swipe along an explicit start→end segment (screen pixels) via
     * [dispatchGesture]. Coordinate overload used directly by [swipe] above and
     * available to the agent for precise drags.
     */
    fun swipe(startX: Int, startY: Int, endX: Int, endY: Int): ActuatorResult =
        swipe(startX, startY, endX, endY, SWIPE_DURATION_MS)

    /**
     * (M1.3) Swipe along an explicit start→end segment over [durationMs] — the coordinate
     * overload the frontier `coordinate_swipe` action drives (its `duration_ms` maps here;
     * the no-duration overload defaults to [SWIPE_DURATION_MS] = 250ms). Never throws.
     */
    fun swipe(startX: Int, startY: Int, endX: Int, endY: Int, durationMs: Long): ActuatorResult {
        val svc = service() ?: return notEnabled()
        return try {
            val ok = dispatchSwipe(svc, startX, startY, endX, endY, durationMs)
            logGesture("swipe", ok)
            ActuatorResult(ok, if (ok) "swiped" else "swipe gesture dispatch failed")
        } catch (e: Exception) {
            Log.w(TAG, "swipe failed (${e.javaClass.simpleName})")
            ActuatorResult(false, "swipe failed (${e.javaClass.simpleName})")
        }
    }

    /**
     * Scroll the screen in [direction] ("up"/"down"/"left"/"right"). Implemented
     * as a centered swipe in the OPPOSITE finger direction is unintuitive, so we
     * keep it simple and map a scroll directly onto the same centered-swipe
     * gesture as [swipe] (a swipe "up" scrolls content up). Unknown direction →
     * `success=false`.
     */
    fun scroll(direction: String): ActuatorResult {
        val svc = service() ?: return notEnabled()
        val metrics = svc.resources.displayMetrics
        val coords = swipeCoords(direction, metrics.widthPixels, metrics.heightPixels)
            ?: return ActuatorResult(false, "unknown scroll direction: $direction")
        return try {
            val ok = dispatchSwipe(svc, coords[0], coords[1], coords[2], coords[3], SWIPE_DURATION_MS)
            logGesture("scroll", ok)
            ActuatorResult(ok, if (ok) "scrolled $direction" else "scroll gesture dispatch failed")
        } catch (e: Exception) {
            Log.w(TAG, "scroll failed (${e.javaClass.simpleName})")
            ActuatorResult(false, "scroll failed (${e.javaClass.simpleName})")
        }
    }

    /**
     * Launch the app with the given [packageName] via its launch [Intent]
     * (NEW_TASK). If the package isn't installed (`getLaunchIntentForPackage`
     * returns null) → `success=false, "app not installed: <pkg>"`.
     */
    fun openApp(packageName: String): ActuatorResult {
        val svc = service() ?: return notEnabled()
        return try {
            val intent = svc.packageManager?.getLaunchIntentForPackage(packageName)
                ?: return ActuatorResult(false, "app not installed: $packageName")
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            svc.startActivity(intent)
            Log.i(TAG, "openApp pkg=$packageName")
            ActuatorResult(true, "launched $packageName")
        } catch (e: Exception) {
            Log.w(TAG, "openApp failed for $packageName (${e.javaClass.simpleName})")
            ActuatorResult(false, "open app failed: $packageName (${e.javaClass.simpleName})")
        }
    }

    /** Press the system Back button via [AccessibilityService.performGlobalAction]. */
    fun back(): ActuatorResult = globalAction("back")

    /** Go to the system Home screen via [AccessibilityService.performGlobalAction]. */
    fun home(): ActuatorResult = globalAction("home")

    /**
     * (M1.3) Open the Recents / overview screen via
     * [AccessibilityService.performGlobalAction] (`GLOBAL_ACTION_RECENTS`) — the third
     * `global_action` the frontier contract exposes (back/home already existed). Never
     * throws; a disabled service → `accessibility service not enabled`.
     */
    fun recents(): ActuatorResult = globalAction("recents")

    /**
     * (M2 / F1) Press a semantic KEY — the coordinate-free `press_key` action. Routed by
     * [pressKeyPlan]:
     *  - `enter` → submit the currently-focused editable field via
     *    [AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER] (API 30+). This is how a
     *    "type → submit" flow (a search box, a chat input) completes WITHOUT a coordinate.
     *    Graceful when there is no focused field (`no focused field to submit`) or the API is
     *    too old (`enter key not supported on this Android version`).
     *  - `back` / `home` / `recents` → [performGlobalAction] (reuses [globalActionFor]).
     *  - anything else (`tab` / `delete` / unknown) → best-effort `unsupported key: <k>` (M2).
     *
     * CAPABILITY-SAFE: uses NO coordinates, so it is meaningful on every form factor incl. XR
     * (the remote dispatcher never coordinate-gates it). Never throws; a disabled service →
     * `accessibility service not enabled`.
     */
    fun pressKey(key: String): ActuatorResult {
        val svc = service() ?: return notEnabled()
        return try {
            when (val plan = pressKeyPlan(key)) {
                is PressKeyPlan.Global -> {
                    val ok = svc.performGlobalAction(plan.action)
                    logGesture("press_key($key)", ok)
                    ActuatorResult(ok, if (ok) "pressed $key" else "$key key rejected")
                }
                PressKeyPlan.ImeEnter -> {
                    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
                        return ActuatorResult(false, "enter key not supported on this Android version")
                    }
                    val focus = svc.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                        ?: return ActuatorResult(false, "no focused field to submit")
                    val ok = focus.performAction(
                        AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
                    logGesture("press_key(enter)", ok)
                    ActuatorResult(ok, if (ok) "pressed enter" else "enter key rejected")
                }
                PressKeyPlan.Unsupported ->
                    ActuatorResult(false, "unsupported key: ${key.trim().lowercase()}")
            }
        } catch (e: Exception) {
            Log.w(TAG, "pressKey failed (${e.javaClass.simpleName})")
            ActuatorResult(false, "press_key failed (${e.javaClass.simpleName})")
        }
    }

    // ---- internals --------------------------------------------------------

    /**
     * The AUTONOMY GATE (4.6). Given a resolved-node [action] ("tap"/"type"), its
     * NON-password [label], and whether the target is a password field
     * ([isPasswordTarget]), decides via the pure [isHighConsequence] +
     * [shouldConfirm] whether to ask the user.
     *
     * Returns:
     *  - `null` → proceed (benign, or YOLO, or the user allowed it).
     *  - a non-null [ActuatorResult] (`success=false, "user declined"`) → ABORT;
     *    the caller must return it without actuating.
     *
     * SECURITY: [describeAction] is fed only the action + [label]; for a password
     * target [label] is null by construction, so the confirm message is the fixed
     * generic "Type into password field" — the typed text can never reach it.
     */
    private suspend fun gate(action: String, label: String?, isPasswordTarget: Boolean): ActuatorResult? {
        val hc = isHighConsequence(action, label, isPasswordTarget)
        if (!shouldConfirm(mode(), hc)) return null
        val allowed = confirm.confirm(describeAction(action, label))
        if (allowed) {
            // Log the DECISION only — never the label/text (leak discipline).
            Log.i(TAG, "autonomy: $action allowed by user")
            return null
        }
        Log.i(TAG, "autonomy: $action declined by user")
        return ActuatorResult(false, "user declined")
    }

    /**
     * (C1, M4) The COORDINATE-tap autonomy gate. Given the [hit] recovered from
     * hit-testing `(x,y)`, decides via the pure [isHighConsequenceCoordinateTap] +
     * [shouldConfirm] whether to ask the user.
     *
     * Returns:
     *  - `null` → proceed (benign labeled coordinate, or YOLO, or the user allowed it).
     *  - a non-null [ActuatorResult] (`success=false, "user declined"`) → ABORT; the caller
     *    returns it without actuating.
     *
     * The confirm message is [describeAction] `("tap", label)`: for an unresolved/unlabeled
     * coordinate the label is null → generic "Tap this control"; a recovered dangerous label
     * surfaces `Tap "<label>"`. A password node contributes a null label upstream
     * ([UiTreeReader.labelAtPoint]), so no secret can reach the message.
     */
    private suspend fun coordinateGate(hit: CoordinateHit): ActuatorResult? {
        val hc = isHighConsequenceCoordinateTap(hit)
        if (!shouldConfirm(mode(), hc)) return null
        val label = (hit as? CoordinateHit.Node)?.label
        val allowed = confirm.confirm(describeAction("tap", label))
        if (allowed) {
            Log.i(TAG, "autonomy: coordinate tap allowed by user")
            return null
        }
        Log.i(TAG, "autonomy: coordinate tap declined by user")
        return ActuatorResult(false, "user declined")
    }

    private fun globalAction(name: String): ActuatorResult {
        val svc = service() ?: return notEnabled()
        val action = globalActionFor(name) ?: return ActuatorResult(false, "unknown global action: $name")
        return try {
            val ok = svc.performGlobalAction(action)
            Log.i(TAG, "globalAction $name ok=$ok")
            ActuatorResult(ok, if (ok) name else "$name action rejected")
        } catch (e: Exception) {
            Log.w(TAG, "globalAction $name failed (${e.javaClass.simpleName})")
            ActuatorResult(false, "$name failed (${e.javaClass.simpleName})")
        }
    }

    /** Dispatch a single short tap (down→up at one point) as a gesture. */
    private fun dispatchTap(svc: AccessibilityService, x: Int, y: Int): Boolean {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, TAP_DURATION_MS)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return svc.dispatchGesture(gesture, null, null)
    }

    /** Dispatch a swipe stroke from start→end over [durationMs]. */
    private fun dispatchSwipe(
        svc: AccessibilityService,
        startX: Int,
        startY: Int,
        endX: Int,
        endY: Int,
        durationMs: Long,
    ): Boolean {
        val path = Path().apply {
            moveTo(startX.toFloat(), startY.toFloat())
            lineTo(endX.toFloat(), endY.toFloat())
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return svc.dispatchGesture(gesture, null, null)
    }

    private fun notEnabled() = ActuatorResult(false, "accessibility service not enabled")

    private fun nodeNotFound(ref: NodeRef): ActuatorResult {
        val target = ref.describe()
        Log.i(TAG, "action target $target not found")
        return ActuatorResult(false, "$target not found")
    }

    /**
     * Logs the action + a coarse node TARGET (node_id index, or resource id) +
     * result ONLY — never the typed text or node screen content. A resource id is
     * the dev-assigned view id, not user data, so it is safe to log; this keeps the
     * no-screen-text discipline intact.
     */
    private fun logAction(action: String, target: String, ok: Boolean) {
        Log.i(TAG, "$action $target ok=$ok")
    }

    private fun logActionError(action: String, target: String, e: Exception) {
        // Class name only — exception messages can carry node text on some
        // frameworks, so we deliberately omit e.message.
        Log.w(TAG, "$action $target failed (${e.javaClass.simpleName})")
    }

    private fun logGesture(action: String, ok: Boolean) {
        Log.i(TAG, "$action ok=$ok")
    }

    companion object {
        private const val TAG = "Actuators"

        /** Tap stroke duration — short press. */
        private const val TAP_DURATION_MS = 60L

        /** Swipe/scroll stroke duration. */
        private const val SWIPE_DURATION_MS = 250L

        /**
         * Production factory: actuates through the live connected
         * [BlackBoxA11yService] via the singleton seam.
         *
         * @param mode reads the device autonomy posture (prod wiring supplies a
         *   SharedPref-backed read defaulting to [AutonomyMode.PERMISSION] — the
         *   SAFE default). Defaults here to YOLO only so an un-wired call keeps
         *   the pre-4.6 behavior; the real wiring (ChatViewModel) passes the safe
         *   reader.
         * @param confirm the user-confirmation seam (prod: [OverlayConfirmUi]).
         * @param credentialHandoff the password-entry handoff seam (Task 4.7; prod:
         *   [OverlayCredentialHandoff]). Default auto-declines so an un-wired call
         *   fails SAFE.
         */
        fun fromService(
            mode: () -> AutonomyMode = { AutonomyMode.YOLO },
            confirm: ConfirmUi = AutoApproveConfirmUi,
            credentialHandoff: CredentialHandoff = AutoDeclineCredentialHandoff,
        ): Actuators = Actuators({ BlackBoxA11yService.instance }, mode, confirm, credentialHandoff)
    }
}

/** A small outcome the agent loop (4.5) feeds back to the model. */
data class ActuatorResult(val success: Boolean, val detail: String)

/**
 * (C1, M4) The production coordinate LABELER: hit-tests `(x,y)` against the live a11y
 * tree via [UiTreeReader.labelAtPoint] to recover a redaction-safe label for the
 * coordinate-tap gate. A null [service] (accessibility off) → [CoordinateHit.None], which
 * the gate treats as high-consequence — so a coordinate tap with the service disabled still
 * fails SAFE (confirm-by-default in PERMISSION) rather than skipping the gate. Top-level +
 * private so the [Actuators] constructor's default can reference the `service` seam; unit
 * tests inject their own labeler instead.
 */
private fun defaultCoordinateLabeler(
    service: () -> BlackBoxA11yService?,
): (Int, Int) -> CoordinateHit = { x, y ->
    val svc = service()
    if (svc == null) CoordinateHit.None else UiTreeReader.labelAtPoint(svc.rootInActiveWindow, x, y)
}

/**
 * How a tap/type target is ADDRESSED. The model gives us either the stable
 * resource id (preferred — see [ById]) or the positional `node_id` ([ByIndex]);
 * [Actuators.resolve] turns either into a live node. Splitting the two as a
 * sealed type keeps the selection logic pure + unit-testable
 * ([parseNodeRef][com.aiblackbox.portal.overlay.parseNodeRef]) and the resolution
 * exhaustive.
 */
sealed interface NodeRef {
    /**
     * The STABLE handle: a dev-assigned `viewIdResourceName` (e.g.
     * `com.android.settings:id/title`). Resolved via
     * [UiTreeReader.findNodeByResourceId] — does NOT drift when the screen changes
     * between read_screen and the tap. Prefer this whenever the node has one.
     */
    data class ById(val resourceId: String) : NodeRef

    /**
     * The positional fallback: the dense actionable DFS index (`node_id`) from
     * read_screen. Resolved via [UiTreeReader.findActionableNode]. Used only for
     * nodes that have no resource id (Compose / custom / WebView). Best-effort —
     * can drift if the tree changed since the read.
     */
    data class ByIndex(val index: Int) : NodeRef

    /**
     * A coarse, NON-secret label for logs / result messages. A resource id is the
     * dev-assigned view id (not user data), so it is safe to surface; node screen
     * text is never used here.
     */
    fun describe(): String = when (this) {
        is ById -> "node[$resourceId]"
        is ByIndex -> "node $index"
    }
}

/**
 * The default [ConfirmUi] for un-wired [Actuators] (existing call-sites / tests):
 * auto-approves everything. This is ONLY ever reached together with the default
 * `mode = { YOLO }`, where [shouldConfirm] is already false and [ConfirmUi] is
 * never consulted — so it is a safe inert default, not a way to silently bypass a
 * Permission-mode gate. The production wiring supplies the real overlay + the safe
 * PERMISSION-default mode.
 */
internal object AutoApproveConfirmUi : ConfirmUi {
    override suspend fun confirm(description: String): Boolean = true
}

/**
 * PURE: compute `[startX, startY, endX, endY]` for a centered swipe in the given
 * cardinal [direction] within a [width]×[height] screen, or null for an unknown
 * direction.
 *
 * The swipe spans the middle ~60% of the relevant axis (insets at 20%/80%) and
 * stays centered on the other axis, so it never touches the very edges (where
 * system gestures live) and is always in bounds — even on tiny/degenerate
 * screens (coords are coerced into `0..width` / `0..height`).
 *
 * Direction semantics (finger drag direction == content scroll direction):
 *  - "up"    : finger from lower-center → upper-center (startY > endY)
 *  - "down"  : finger from upper-center → lower-center (startY < endY)
 *  - "left"  : finger from right-center → left-center  (startX > endX)
 *  - "right" : finger from left-center  → right-center (startX < endX)
 *
 * Matching is case-insensitive and trims surrounding whitespace.
 */
fun swipeCoords(direction: String, width: Int, height: Int): IntArray? {
    val midX = width / 2
    val midY = height / 2
    // 20% / 80% insets, coerced in-bounds so a 1x1 screen still yields valid pts.
    val lowY = (height * 0.2f).toInt().coerceIn(0, height)
    val highY = (height * 0.8f).toInt().coerceIn(0, height)
    val lowX = (width * 0.2f).toInt().coerceIn(0, width)
    val highX = (width * 0.8f).toInt().coerceIn(0, width)

    return when (direction.trim().lowercase()) {
        // Swipe up: drag finger from lower (highY) to upper (lowY).
        "up" -> intArrayOf(midX, highY, midX, lowY)
        // Swipe down: drag finger from upper (lowY) to lower (highY).
        "down" -> intArrayOf(midX, lowY, midX, highY)
        // Swipe left: drag finger from right (highX) to left (lowX).
        "left" -> intArrayOf(highX, midY, lowX, midY)
        // Swipe right: drag finger from left (lowX) to right (highX).
        "right" -> intArrayOf(lowX, midY, highX, midY)
        else -> null
    }
}

/**
 * PURE: map a global-action [name] to its [AccessibilityService] GLOBAL_ACTION_*
 * constant, or null for an unknown name. Matching is case-insensitive + trimmed.
 */
fun globalActionFor(name: String): Int? = when (name.trim().lowercase()) {
    "back" -> AccessibilityService.GLOBAL_ACTION_BACK
    "home" -> AccessibilityService.GLOBAL_ACTION_HOME
    // (M1.3) recents → the overview screen; the third global action the frontier
    // contract exposes (was absent — globalActionFor mapped only back/home).
    "recents" -> AccessibilityService.GLOBAL_ACTION_RECENTS
    else -> null
}

/**
 * (M2 / F1) How a `press_key` key actuates — the PURE decision behind [Actuators.pressKey],
 * split out so the routing (enter→IME, back/home/recents→global, else→unsupported) is
 * JVM-unit-testable without the framework.
 */
sealed interface PressKeyPlan {
    /** `enter` → submit the focused editable field via `ACTION_IME_ENTER` (API 30+). */
    object ImeEnter : PressKeyPlan

    /** `back`/`home`/`recents` → `performGlobalAction([action])` (reuses [globalActionFor]). */
    data class Global(val action: Int) : PressKeyPlan

    /** `tab`/`delete`/unknown → no reliable equivalent → best-effort unsupported (M2). */
    object Unsupported : PressKeyPlan
}

/**
 * PURE: map a `press_key` [key] to its [PressKeyPlan]. `enter` → [PressKeyPlan.ImeEnter];
 * `back`/`home`/`recents` → [PressKeyPlan.Global] (reusing [globalActionFor], so the same
 * `performGlobalAction` path serves both the `global_action` and `press_key` variants);
 * everything else (incl. `tab`/`delete`) → [PressKeyPlan.Unsupported]. Case-insensitive +
 * trimmed. The remote wire-parser ([com.aiblackbox.portal.data.remote.parseAction]) already
 * rejects a key outside the schema enum, so a device-side unknown is only reachable off the
 * on-device path — handled here as a graceful unsupported rather than a throw.
 */
fun pressKeyPlan(key: String): PressKeyPlan {
    val k = key.trim().lowercase()
    if (k == "enter") return PressKeyPlan.ImeEnter
    globalActionFor(k)?.let { return PressKeyPlan.Global(it) }   // back / home / recents
    return PressKeyPlan.Unsupported                              // tab / delete / unknown
}
