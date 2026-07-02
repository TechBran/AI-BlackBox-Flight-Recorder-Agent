package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.overlay.DeviceCapabilities
import com.aiblackbox.portal.overlay.UiNode
import com.aiblackbox.portal.overlay.WindowInfo
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * (M1.2) The device→brain `observation`: one snapshot of the target device's screen
 * state, sent UP over the 8765 channel for the cloud frontier loop to reason over.
 * Conforms to the M0 contract `docs/schema/observation.json`:
 * `{msg:"observation", schema_version, ui_tree[], device_capability, screenshot?, timestamp}`.
 *
 * ## Tree-first cadence (the point of M1.2)
 * [uiTree] (the password-redacted [UiNode] list from `UiTreeReader.readScreen`) is
 * ALWAYS present. [screenshot] rides along ONLY when the device can supply one
 * ([DeviceCapabilities.hasScreenshot]) AND the step needs vision — the tree is
 * blind/sparse or the model explicitly asked. See [shouldCaptureScreenshot] /
 * [treeIsBlind] (both pure + unit-tested) and [ObservationBuilder], which applies them.
 *
 * ## Password-redaction invariant
 * [uiTree] carries [UiNode]s already redacted at the device boundary
 * (`UiTreeReader.nodeText`): an `is_password` node's `text` is the placeholder
 * `·····`, never the raw credential. The screenshot path keeps the same refusal gate,
 * but scoped: it suppresses capture only while a password field is FOCUSED. This is NOT
 * complete pixel redaction — a password field that is VISIBLE but UNFOCUSED (or already-
 * revealed secret text) can still enter a screenshot frame. That gap is the plan's
 * documented open question (deferred); do not treat screenshot redaction as complete.
 *
 * Serialized with the `WIRE_JSON` encoder (encodeDefaults=true → the required
 * `device_capability.displayId` / `ui_node.resource_id` defaults are emitted;
 * explicitNulls=false → an absent [screenshot] is omitted, conforming to the schema).
 */
@Serializable
data class Observation(
    val msg: String = WireMessageType.OBSERVATION,
    @SerialName("schema_version") val schemaVersion: String = SCHEMA_VERSION,
    @SerialName("ui_tree") val uiTree: List<UiNode>,
    @SerialName("device_capability") val deviceCapability: DeviceCapabilities,
    // Base64-encoded PNG from the silent AccessibilityService.takeScreenshot(). Null
    // (omitted on the wire) under the tree-first cadence or when hasScreenshot=false.
    val screenshot: String? = null,
    // (M5.3) The window TOPOLOGY: which app owns which on-screen rectangle, on which display,
    // and where the system bars / split-screen divider sit (node bounds are display-relative).
    // Additive + optional; always emitted (may be []) so a multi-window loop always has it.
    @SerialName("window_topology") val windowTopology: List<WindowInfo> = emptyList(),
    // (M5.5) True when the foldable POSTURE changed since the previous observation — the loop
    // must treat prior screen coordinates as invalid and re-observe before its next coordinate
    // action (the current posture rides in device_capability.posture). Read-and-cleared source:
    // FoldingFeatureMonitor.consumePostureChanged(). Additive; defaults false.
    @SerialName("posture_changed") val postureChanged: Boolean = false,
    // Epoch milliseconds (System.currentTimeMillis) — the native Android form; the
    // schema also accepts an RFC-3339 string, but the device emits the integer.
    val timestamp: Long = System.currentTimeMillis(),
) {
    companion object {
        /** The wire-contract version negotiated on the observation (schema `const "1.3"`).
         *  1.1 = intent.name enum 15 → 26; 1.2 = the press_key action variant; 1.3 (M5, additive) =
         *  the observation `window_topology` + `posture_changed` fields + `device_capability.posture`
         *  (foldable/large-screen/DeX display-addressing). All minor, back-compatible bumps. */
        const val SCHEMA_VERSION = "1.3"
    }
}

/**
 * PURE: is the current [uiTree] "blind" — i.e. too sparse for the model to ground on,
 * so a screenshot is warranted this step? True when there are no actionable nodes at
 * all, OR none of them carry any visible text/description (a canvas / WebView / game
 * surface the accessibility tree can't see into). This is the tree-first trigger: a
 * rich tree needs no picture, a blind one does.
 */
fun treeIsBlind(uiTree: List<UiNode>): Boolean {
    if (uiTree.isEmpty()) return true
    return uiTree.none { it.text.isNotBlank() }
}

/**
 * PURE: the tree-first screenshot decision. Capture ONLY when the device advertises
 * [DeviceCapabilities.hasScreenshot] AND (the model explicitly [requested] one OR the
 * tree is blind per [treeIsBlind]). An `hasScreenshot=false` device (e.g. XR) NEVER
 * captures — the loop runs tree-only, and the wire schema forbids a screenshot there.
 */
fun shouldCaptureScreenshot(
    capability: DeviceCapabilities,
    uiTree: List<UiNode>,
    requested: Boolean,
): Boolean {
    if (!capability.hasScreenshot) return false
    return requested || treeIsBlind(uiTree)
}
