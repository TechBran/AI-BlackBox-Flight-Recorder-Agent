package com.aiblackbox.portal.overlay

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
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
 * ## Safety floors enforced HERE (the rest is wrapped later)
 * - [type] REFUSES to type into a password field (see [type]). The proper
 *   credential handoff is Task 4.7; 4.3 must never set text on a secret field.
 * - The autonomy confirm-gate (4.6) and credential handoff (4.7) wrap these
 *   later for the high-consequence cases — they are NOT implemented here.
 *
 * ## Logging discipline (leak vector)
 * Logs emit ONLY `nodeId` / action name / coarse result detail. They MUST NEVER
 * emit the [type] `text` argument or any node's screen text/content.
 *
 * ## Scope (4.3 ONLY)
 * Builds the gestures/intents and performs the actions. Does NOT register these
 * as resident on-device functions (4.5), implement the autonomy gate (4.6),
 * handle credentials (4.7), or capture screenshots (4.4).
 *
 * @param service seam to the connected service (prod: `{ BlackBoxA11yService.instance }`).
 */
class Actuators(private val service: () -> BlackBoxA11yService?) {

    /**
     * Tap the node with the given `node_id`.
     *
     * Resolves the node positionally via [UiTreeReader.findActionableNode]. If
     * found and [AccessibilityNodeInfo.isClickable], performs a semantic
     * [AccessibilityNodeInfo.ACTION_CLICK] (more reliable than a coordinate tap).
     * Otherwise falls back to dispatching a touch gesture at the node's
     * on-screen bounds center. A null node → `success=false, "node N not found"`.
     */
    fun tap(nodeId: Int): ActuatorResult {
        val svc = service() ?: return notEnabled()
        val node = UiTreeReader.findActionableNode(svc.rootInActiveWindow, nodeId)
            ?: return nodeNotFound(nodeId)

        return try {
            if (node.isClickable) {
                val ok = node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                logAction("tap", nodeId, ok)
                ActuatorResult(ok, if (ok) "tapped node $nodeId" else "click action rejected for node $nodeId")
            } else {
                // Not clickable: tap the bounds center as a touch gesture.
                val rect = android.graphics.Rect()
                node.getBoundsInScreen(rect)
                val cx = rect.centerX()
                val cy = rect.centerY()
                val ok = dispatchTap(svc, cx, cy)
                logAction("tap(gesture)", nodeId, ok)
                ActuatorResult(ok, if (ok) "tapped node $nodeId at center" else "tap gesture dispatch failed for node $nodeId")
            }
        } catch (e: Exception) {
            // Never leak node content in the message — only the action + class.
            logActionError("tap", nodeId, e)
            ActuatorResult(false, "tap failed for node $nodeId (${e.javaClass.simpleName})")
        }
    }

    /**
     * Set [text] on the editable node with the given `node_id`.
     *
     * **HARD SAFETY FLOOR:** if the resolved node is a password field
     * ([UiTreeReader.isPasswordField] over `isPassword`+`inputType`), this
     * REFUSES — it returns `success=false, detail="refused: password field — use
     * credential handoff"` and does NOT set any text. The proper path for
     * secrets is the credential handoff (Task 4.7); 4.3 never types into a
     * password field.
     *
     * The [text] argument is NEVER logged (leak vector) — only the nodeId,
     * action, and result.
     */
    fun type(nodeId: Int, text: String): ActuatorResult {
        val svc = service() ?: return notEnabled()
        val node = UiTreeReader.findActionableNode(svc.rootInActiveWindow, nodeId)
            ?: return nodeNotFound(nodeId)

        return try {
            // SAFETY: refuse password fields outright. Same gate the reader uses.
            if (isPasswordField(node.isPassword, node.inputType)) {
                logAction("type(REFUSED:password)", nodeId, false)
                return ActuatorResult(false, "refused: password field — use credential handoff")
            }
            val args = Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            }
            val ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            // NOTE: deliberately NOT logging `text` — only the nodeId + result.
            logAction("type", nodeId, ok)
            ActuatorResult(ok, if (ok) "set text on node $nodeId" else "set-text action rejected for node $nodeId")
        } catch (e: Exception) {
            logActionError("type", nodeId, e)
            ActuatorResult(false, "type failed for node $nodeId (${e.javaClass.simpleName})")
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
    fun swipe(startX: Int, startY: Int, endX: Int, endY: Int): ActuatorResult {
        val svc = service() ?: return notEnabled()
        return try {
            val ok = dispatchSwipe(svc, startX, startY, endX, endY, SWIPE_DURATION_MS)
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

    // ---- internals --------------------------------------------------------

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

    private fun nodeNotFound(nodeId: Int): ActuatorResult {
        Log.i(TAG, "action target node $nodeId not found")
        return ActuatorResult(false, "node $nodeId not found")
    }

    /** Logs nodeId + action + result ONLY — never the typed text or node content. */
    private fun logAction(action: String, nodeId: Int, ok: Boolean) {
        Log.i(TAG, "$action node=$nodeId ok=$ok")
    }

    private fun logActionError(action: String, nodeId: Int, e: Exception) {
        // Class name only — exception messages can carry node text on some
        // frameworks, so we deliberately omit e.message.
        Log.w(TAG, "$action node=$nodeId failed (${e.javaClass.simpleName})")
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
         */
        fun fromService(): Actuators = Actuators { BlackBoxA11yService.instance }
    }
}

/** A small outcome the agent loop (4.5) feeds back to the model. */
data class ActuatorResult(val success: Boolean, val detail: String)

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
    else -> null
}
