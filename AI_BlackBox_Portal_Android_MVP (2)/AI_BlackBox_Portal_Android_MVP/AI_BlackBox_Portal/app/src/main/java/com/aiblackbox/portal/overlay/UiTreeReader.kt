package com.aiblackbox.portal.overlay

import android.graphics.Rect
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/**
 * The `read_screen` UI-tree reader (Phase 4, Task 4.2).
 *
 * Turns the live [AccessibilityNodeInfo] tree (from the consented
 * [BlackBoxA11yService]) into a compact JSON list of *actionable* nodes for the
 * on-device Gemma agent to reason over — with **password fields redacted at the
 * boundary**.
 *
 * ## Security guarantee (the whole point of this task)
 * A password field's raw text MUST NEVER cross the device boundary into the
 * model / transcript / snapshot. The single gate that enforces this is
 * [nodeText]: for a password node it returns the fixed placeholder
 * [PASSWORD_PLACEHOLDER] and the raw [CharSequence] is dropped on the floor —
 * it is never copied into a [UiNode], a log line, or an exception message.
 *
 * ## Design: pure core + thin framework shell
 * Everything that decides *what text gets emitted* ([nodeText], [roleOf],
 * [boundsString], [nodesToJson]) is a pure, JVM-unit-testable top-level
 * function with no framework dependency — that's where the redaction guarantee
 * lives and is tested hard ([UiTreeReaderTest]). The [UiTreeReader] class is the
 * thin framework shell that DFS-walks the real tree and is device-verified.
 *
 * ## Scope (4.2 ONLY)
 * This produces the JSON. Wiring `read_screen` into the agent loop as a resident
 * function is Task 4.5. Gesture actuation is 4.3. Screenshots are 4.4.
 * Credentials are 4.7. None of those happen here.
 */
class UiTreeReader(private val rootProvider: () -> AccessibilityNodeInfo?) {

    /**
     * Read the current screen as a compact JSON array of actionable [UiNode]s.
     *
     * - If the accessibility service isn't connected (root is null) → returns
     *   `nodesToJson(emptyList())` i.e. `"[]"` (graceful, never throws).
     * - Otherwise DFS-walks the tree, emitting a [UiNode] only for *actionable*
     *   nodes (see [isActionable]), capping the result at [MAX_NODES] so a huge
     *   tree cannot blow up the prompt.
     *
     * Redaction happens inline via [nodeText]; the raw password text is never
     * materialized into the node list. No node text/content is logged — only
     * counts/roles, which are not a leak vector.
     */
    fun readScreen(): String {
        val root = rootProvider() ?: run {
            Log.i(TAG, "read_screen: service not connected (null root) -> []")
            return nodesToJson(emptyList())
        }

        val nodes = ArrayList<UiNode>(MAX_NODES)
        val counter = intArrayOf(0) // sequential DFS index → node_id
        try {
            // Shared walk: emit a UiNode for every actionable node, in the SAME
            // pre-order DFS + same filter + same MAX_NODES cap that
            // findActionableNode (the actuator resolver) replays. This is what
            // keeps node_ids stable between read_screen and the actuators (4.3).
            walkActionable(root, counter) { node, denseIndex ->
                val rect = Rect()
                node.getBoundsInScreen(rect)
                // Treat as password if EITHER the node flag OR the inputType says
                // so (a native field can set one without the other) — computed
                // once and used for BOTH the redaction gate and the emitted flag.
                val pw = isPasswordField(node.isPassword, node.inputType)
                nodes.add(
                    UiNode(
                        nodeId = denseIndex,
                        role = roleOf(node.className),
                        // REDACTION BOUNDARY: nodeText drops the raw text for a
                        // password node. Prefer text, fall back to contentDescription.
                        text = nodeText(node.text ?: node.contentDescription, pw),
                        bounds = boundsString(rect.left, rect.top, rect.right, rect.bottom),
                        clickable = node.isClickable,
                        editable = node.isEditable,
                        isPassword = pw,
                    ),
                )
                false // never short-circuit: collect every actionable node up to the cap
            }
        } catch (e: Exception) {
            // Defensive: never let a malformed tree crash the agent. The message
            // is deliberately generic — node text/content must never appear in
            // an exception/log line (leak vector).
            Log.w(TAG, "read_screen: tree walk aborted (${e.javaClass.simpleName}); returning partial")
        }
        Log.i(TAG, "read_screen: emitted ${nodes.size} actionable node(s)")
        return nodesToJson(nodes)
    }

    companion object {
        private const val TAG = "UiTreeReader"

        /**
         * Cap on emitted nodes so a pathologically large tree can't blow up the
         * on-device model's prompt. DFS order means the first (typically more
         * relevant top-of-tree) nodes win.
         */
        const val MAX_NODES = 80

        /**
         * Production factory: reads the live root from the connected
         * [BlackBoxA11yService] via the singleton seam. The constructor takes a
         * lambda seam instead of the singleton directly so the walker doesn't
         * hard-depend on it (and so the shell stays mockable on-device).
         */
        fun fromService(): UiTreeReader =
            UiTreeReader { BlackBoxA11yService.instance?.rootInActiveWindow }

        /**
         * Resolve a `node_id` (the dense actionable index from `read_screen`)
         * back to a live [AccessibilityNodeInfo] for the Actuators (Task 4.3).
         *
         * A `node_id` is NOT a durable handle — it is purely the position of a
         * node in the dense actionable sequence produced by [readScreen]. To act
         * on one, the actuator must RE-WALK the current tree with the IDENTICAL
         * filter ([isActionable]) and the IDENTICAL pre-order DFS + [MAX_NODES]
         * cap that the reader used, then take the node at that index. Both paths
         * go through [walkActionable], so the ordering can never drift between
         * read and act.
         *
         * The tree may have changed slightly since the read; that's acceptable —
         * this is a best-effort positional match. Returns null if [targetIndex]
         * is out of range / the tree shrank / [root] is null (caller treats null
         * as "node not found", never an NPE).
         *
         * The returned node is the LIVE framework node and is intentionally NOT
         * recycled here — the caller performs an action on it. (Sibling/child
         * nodes traversed on the way are recycled inside [walkActionable].)
         */
        internal fun findActionableNode(
            root: AccessibilityNodeInfo?,
            targetIndex: Int,
        ): AccessibilityNodeInfo? {
            if (root == null || targetIndex < 0) return null
            var found: AccessibilityNodeInfo? = null
            val counter = intArrayOf(0)
            walkActionable(root, counter) { node, denseIndex ->
                if (denseIndex == targetIndex) {
                    found = node
                    true // short-circuit: stop the walk, keep this node un-recycled
                } else {
                    false
                }
            }
            return found
        }

        /**
         * THE one shared pre-order DFS over *actionable* nodes. Both
         * [readScreen] (collects every node) and [findActionableNode] (stops at
         * one) drive their behavior through this so the dense actionable index
         * (`node_id`) is computed identically on both paths.
         *
         * For each actionable node (see [isActionable]) it invokes [visit] with
         * the live node and its dense index, then increments the index. If
         * [visit] returns `true` the walk stops immediately AND the node passed
         * to that final [visit] is NOT recycled (the resolver wants to keep it);
         * every other traversed child IS recycled. Honors [MAX_NODES] so a
         * pathological tree can't run away.
         *
         * @return true if the walk was short-circuited by [visit].
         */
        private fun walkActionable(
            node: AccessibilityNodeInfo?,
            counter: IntArray,
            visit: (AccessibilityNodeInfo, Int) -> Boolean,
        ): Boolean {
            if (node == null || counter[0] >= MAX_NODES) return false

            if (isActionable(node)) {
                val stop = visit(node, counter[0])
                counter[0]++
                if (stop) return true
            }

            val childCount = node.childCount
            for (i in 0 until childCount) {
                if (counter[0] >= MAX_NODES) break
                val child = node.getChild(i) ?: continue
                val stopped = walkActionable(child, counter, visit)
                if (stopped) {
                    // Do NOT recycle: the short-circuited node may be `child`
                    // itself or live in its subtree (findActionableNode returns
                    // it to the caller to act on). Recycling would invalidate it.
                    return true
                }
                @Suppress("DEPRECATION")
                try {
                    child.recycle()
                } catch (_: Exception) {
                    // recycle() is a no-op / may throw IllegalStateException on
                    // some versions; never let cleanup crash the walk.
                }
            }
            return false
        }

        /**
         * A node is worth emitting/acting on if the agent could act on it or
         * read it: clickable, editable, or it carries visible, non-blank
         * text/description. This is THE filter both the reader and the actuator
         * resolver use — keep it the single definition so node_ids line up.
         */
        private fun isActionable(node: AccessibilityNodeInfo): Boolean {
            if (node.isClickable || node.isEditable) return true
            val hasText = !node.text.isNullOrBlank() || !node.contentDescription.isNullOrBlank()
            return hasText && node.isVisibleToUser
        }
    }
}

/**
 * One actionable UI node, redacted and ready to serialize for the on-device
 * model. [text] is ALREADY redacted (see [nodeText]) before construction — a
 * password node's raw text never reaches this type.
 */
@Serializable
data class UiNode(
    @SerialName("node_id") val nodeId: Int,
    val role: String, // short class-name-derived role, e.g. "Button","EditText"
    val text: String, // ALREADY redacted if password (see nodeText)
    val bounds: String, // "l,t,r,b"
    val clickable: Boolean,
    val editable: Boolean,
    @SerialName("is_password") val isPassword: Boolean,
)

/** The fixed placeholder emitted in place of any password field's text. */
const val PASSWORD_PLACEHOLDER: String = "·····"

private val readScreenJson = Json {
    encodeDefaults = true
    // Keep output compact (no pretty-print) — this goes straight into a prompt.
}

/**
 * THE REDACTION GATE. For a password node returns the fixed
 * [PASSWORD_PLACEHOLDER]; the raw text is dropped and never returned. For a
 * non-password node returns the raw text (or "" if null).
 *
 * This is the *only* place screen text becomes node text, so it is the single
 * choke point the security guarantee depends on.
 */
fun nodeText(rawText: CharSequence?, isPassword: Boolean): String =
    if (isPassword) PASSWORD_PLACEHOLDER else rawText?.toString().orEmpty()

/**
 * Whether a node must be treated as a password field for redaction. Gates on the
 * node's own [isPassword] flag AND the [inputType] password variations: a native
 * EditText can carry a password [inputType] while reporting `isPassword == false`
 * (the framework doesn't always set both), and its cleartext would otherwise flow
 * through the gate ungated. Checking both is defense-in-depth for the
 * native-input case.
 *
 * LIMITATION (tracked for the 4.8 security review): this CANNOT catch WebView
 * `<input type="password">` or Compose `VisualTransformation` fields, which
 * expose neither `isPassword` nor a password `inputType` — their masking is
 * purely visual. Those remain a known false-negative surface at this layer.
 */
fun isPasswordField(isPassword: Boolean, inputType: Int): Boolean {
    if (isPassword) return true
    val variation = inputType and android.text.InputType.TYPE_MASK_VARIATION
    val cls = inputType and android.text.InputType.TYPE_MASK_CLASS
    if (cls == android.text.InputType.TYPE_CLASS_TEXT) {
        return variation == android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD ||
            variation == android.text.InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD ||
            variation == android.text.InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD
    }
    if (cls == android.text.InputType.TYPE_CLASS_NUMBER) {
        return variation == android.text.InputType.TYPE_NUMBER_VARIATION_PASSWORD
    }
    return false
}

/**
 * Last `.`-segment of a class name (e.g. `android.widget.Button` → `Button`),
 * or `"View"` if null/blank.
 */
fun roleOf(className: CharSequence?): String {
    val s = className?.toString()?.trim().orEmpty()
    if (s.isEmpty()) return "View"
    return s.substringAfterLast('.')
}

/** Compact bounds string `"l,t,r,b"` from the four edge coordinates. */
fun boundsString(left: Int, top: Int, right: Int, bottom: Int): String =
    "$left,$top,$right,$bottom"

/** Serialize the node list to compact JSON — this is what `read_screen` returns. */
fun nodesToJson(nodes: List<UiNode>): String = readScreenJson.encodeToString(nodes)
