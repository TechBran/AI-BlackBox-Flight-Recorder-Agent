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
    fun readScreen(): String = nodesToJson(readNodes())

    /**
     * (M1.2) The same DFS walk as [readScreen] but returning the TYPED, already-redacted
     * [UiNode] list instead of its JSON string. This is the single source both the
     * `read_screen` tool text ([readScreen], which just serializes this) and the frontier
     * `observation` (`ui_tree`, via `ObservationBuilder`) draw from — so the
     * password-redaction gate ([nodeText]) is applied in exactly ONE place regardless of
     * consumer. Returns an empty list when the service isn't connected (null root);
     * never throws (a malformed tree yields the partial list collected so far).
     */
    fun readNodes(): List<UiNode> {
        val root = rootProvider() ?: run {
            Log.i(TAG, "read_screen: service not connected (null root) -> []")
            return emptyList()
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
                        // STABLE HANDLE (4.8 follow-up): the dev-assigned resource id
                        // (e.g. "com.android.settings:id/title"). Unlike node_id (a
                        // positional DFS index that DRIFTS when the screen changes),
                        // this does NOT move with insertions, so a tap/type keyed on
                        // it can't miss. "" when the view has no id (common for
                        // Compose / custom / WebView nodes). Not a secret — but kept
                        // out of the redaction path entirely (it is never node text).
                        resourceId = node.viewIdResourceName ?: "",
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
        return nodes
    }

    /**
     * Is the currently INPUT-FOCUSED node a password field? (Task W4.2 — the
     * screenshot redaction gate.) Used to REFUSE a MediaProjection screen capture
     * whenever the user is on a password entry, so a credential never reaches the
     * model via an image (the accessibility-text path already redacts password node
     * TEXT via [nodeText]; a screenshot would bypass that, hence this gate).
     *
     * Reads the live tree's input focus ([AccessibilityNodeInfo.findFocus] with
     * [AccessibilityNodeInfo.FOCUS_INPUT]) and runs the SAME pure
     * [isPasswordField] gate the reader uses (node flag OR password [inputType]).
     * Returns false when the service isn't connected (null root) or nothing is
     * focused — the caller treats "can't tell" as "not a password" (capture
     * proceeds); the explicit-refusal value is true ONLY when a password field is
     * confirmed focused. Never throws (a malformed tree degrades to false).
     *
     * LIMITATION (shared with [isPasswordField]): a WebView `<input type=password>`
     * or a Compose `VisualTransformation` field exposes neither `isPassword` nor a
     * password `inputType`, so this can't detect those — they remain a known
     * false-negative surface, tracked at the redaction layer.
     */
    fun isPasswordFieldFocused(): Boolean {
        // FAIL-OPEN: "can't tell" (no tree / nothing focused / query throws below)
        // returns false so the capture PROCEEDS. Safe ONLY because capture is
        // user-invoked today; a FUTURE autonomous capture path MUST flip this to
        // fail-CLOSED (treat "can't tell" as password-present → refuse the capture).
        val root = rootProvider() ?: return false
        return try {
            val focused = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return false
            val pw = isPasswordField(focused.isPassword, focused.inputType)
            @Suppress("DEPRECATION")
            try {
                focused.recycle()
            } catch (_: Exception) {
                // recycle() may be a no-op / throw on some versions; never crash here.
            }
            pw
        } catch (e: Exception) {
            // Defensive: a focus query on a malformed tree must not crash the gate.
            // Fail OPEN (not a password) — the caller's other guards still apply; the
            // password case is the one we positively detect, never assume.
            Log.w(TAG, "isPasswordFieldFocused: focus query failed (${e.javaClass.simpleName}) -> false")
            false
        }
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
         * Resolve a STABLE [resourceId] (the dev-assigned `viewIdResourceName`,
         * e.g. `com.android.settings:id/title`) back to a live
         * [AccessibilityNodeInfo] for the Actuators — the reliable path.
         *
         * Unlike [findActionableNode], this does NOT key on position, so it does
         * not drift when the screen changes between `read_screen` and the tap: a
         * node keeps its resource id even as siblings are inserted/removed. This is
         * the fix for the device bug where a positional `node_id` pointed at the
         * wrong (or no) node by the time the tap fired.
         *
         * DFS-walks via the SAME [walkActionable] (same actionable filter +
         * [MAX_NODES] cap) and returns the FIRST node whose `viewIdResourceName`
         * equals [resourceId] exactly. Returns null when [root] is null,
         * [resourceId] is blank, or no actionable node within the cap matches.
         *
         * LIMITATION: if several nodes share a resource id (e.g. identical list
         * rows), the FIRST match in DFS order wins. That is acceptable for v1; a
         * caller that needs a specific row can still fall back to `node_id`.
         *
         * The returned node is the LIVE framework node and is intentionally NOT
         * recycled here (the caller acts on it); other traversed children ARE
         * recycled inside [walkActionable], exactly like [findActionableNode].
         */
        internal fun findNodeByResourceId(
            root: AccessibilityNodeInfo?,
            resourceId: String,
        ): AccessibilityNodeInfo? {
            if (root == null || resourceId.isBlank()) return null
            var found: AccessibilityNodeInfo? = null
            val counter = intArrayOf(0)
            walkActionable(root, counter) { node, _ ->
                if (node.viewIdResourceName == resourceId) {
                    found = node
                    true // short-circuit: stop, keep this node un-recycled
                } else {
                    false
                }
            }
            return found
        }

        /**
         * (C1, M4) Hit-test a COORDINATE `(x,y)` against the live tree and recover a
         * redaction-safe LABEL for the autonomy gate — the label-recovery half of the
         * coordinate-tap fail-safe ([com.aiblackbox.portal.overlay.Actuators.tap] `(x,y)`).
         *
         * DFS-walks the SAME actionable filter + [MAX_NODES] cap the reader/resolvers use,
         * and returns the SMALLEST-area actionable node whose on-screen bounds CONTAIN the
         * point (the most specific control under the finger — a "Delete" button nested in a
         * clickable row wins over the row). The recovered label is the node's
         * text-or-contentDescription, EXCEPT a password node contributes a `null` label (its
         * text is never read — mirrors the redaction the reader/actuator enforce elsewhere).
         *
         * Returns [CoordinateHit.None] when [root] is null OR no actionable node within the
         * cap contains the point (tree-blind / unlabeled space / beyond the cap) — the
         * fail-safe the gate treats as high-consequence. Never throws (a malformed tree
         * degrades to whatever was resolved so far, else None). The LABEL is copied out
         * during the walk (never a live node reference), so [walkActionable]'s recycling of
         * traversed nodes is safe.
         */
        internal fun labelAtPoint(root: AccessibilityNodeInfo?, x: Int, y: Int): CoordinateHit {
            if (root == null) return CoordinateHit.None
            var found = false
            var bestArea = Long.MAX_VALUE
            var bestLabel: String? = null
            val counter = intArrayOf(0)
            try {
                walkActionable(root, counter) { node, _ ->
                    val rect = Rect()
                    node.getBoundsInScreen(rect)
                    if (rect.contains(x, y)) {
                        val area = rect.width().toLong() * rect.height().toLong()
                        if (!found || area < bestArea) {
                            found = true
                            bestArea = area
                            // Redaction: never read a password node's text into the label.
                            val pw = isPasswordField(node.isPassword, node.inputType)
                            bestLabel = if (pw) null else (node.text ?: node.contentDescription)?.toString()
                        }
                    }
                    false // scan every actionable node; do not short-circuit
                }
            } catch (e: Exception) {
                Log.w(TAG, "labelAtPoint: hit-test aborted (${e.javaClass.simpleName})")
            }
            return if (found) CoordinateHit.Node(bestLabel) else CoordinateHit.None
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
    // STABLE handle: the dev-assigned resource id (viewIdResourceName), e.g.
    // "com.android.settings:id/title". Preferred over node_id for tap/type because
    // it doesn't drift when the screen changes. "" when the view has no id
    // (Compose / custom / WebView). Defaults to "" so existing construction sites
    // (e.g. tests building a node without it) keep compiling.
    @SerialName("resource_id") val resourceId: String = "",
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
