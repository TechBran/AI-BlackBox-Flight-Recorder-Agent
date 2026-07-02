package com.aiblackbox.portal.overlay

import android.view.accessibility.AccessibilityWindowInfo
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the PURE core of the read_screen UI-tree reader (Task 4.2).
 *
 * The redaction guarantee lives in these pure functions, so they are tested
 * hard here. The live tree walk in [UiTreeReader.readScreen] depends on the
 * Android framework ([android.view.accessibility.AccessibilityNodeInfo]) and is
 * device-verified by the phone controller — only the pure security gate and the
 * serialization are unit-tested.
 *
 * NOTE on bounds: [boundsString] takes four ints rather than an
 * [android.graphics.Rect] because Rect's constructor is a `Stub!` throw in the
 * unit-test android.jar (verified). The framework shell extracts the four edge
 * ints from the live Rect and passes them in; this keeps the pure core fully
 * JVM-unit-testable.
 */
class UiTreeReaderTest {

    // ---- isPasswordField: inputType-aware password gate (4.2 hardening) ----

    private val TEXT = android.text.InputType.TYPE_CLASS_TEXT
    private val NUMBER = android.text.InputType.TYPE_CLASS_NUMBER

    @Test
    fun `isPasswordField true when node flag is set regardless of inputType`() {
        assertTrue(isPasswordField(isPassword = true, inputType = 0))
        assertTrue(isPasswordField(isPassword = true, inputType = TEXT)) // plain text inputType
    }

    @Test
    fun `isPasswordField catches password inputType even when node flag is false`() {
        // The whole point of the hardening: isPassword=false but inputType says password.
        assertTrue(isPasswordField(false, TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD))
        assertTrue(isPasswordField(false, TEXT or android.text.InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD))
        assertTrue(isPasswordField(false, TEXT or android.text.InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD))
        assertTrue(isPasswordField(false, NUMBER or android.text.InputType.TYPE_NUMBER_VARIATION_PASSWORD))
    }

    @Test
    fun `isPasswordField false for normal inputs`() {
        assertFalse(isPasswordField(false, 0))
        assertFalse(isPasswordField(false, TEXT)) // plain text
        assertFalse(isPasswordField(false, TEXT or android.text.InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS))
        assertFalse(isPasswordField(false, NUMBER)) // plain number
    }

    @Test
    fun `redaction holds when only inputType marks a password (end-to-end through the gate)`() {
        val secret = "hunter2"
        val pw = isPasswordField(false, TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD)
        val node = UiNode(
            nodeId = 0, role = "EditText", text = nodeText(secret, pw),
            bounds = "0,0,1,1", clickable = true, editable = true, isPassword = pw,
        )
        val json = nodesToJson(listOf(node))
        assertFalse("inputType-only password must still be redacted", json.contains(secret))
        assertTrue(json.contains("·····"))
        assertTrue(json.contains("\"is_password\":true"))
    }

    // ---- nodeText: THE REDACTION GATE -------------------------------------

    @Test
    fun `nodeText redacts a real-looking password regardless of raw text`() {
        val secret = "hunter2"
        val out = nodeText(secret, isPassword = true)
        assertEquals("·····", out)
        // The raw password chars must NEVER survive into the output.
        assertFalse("redacted output must not contain the raw password", out.contains(secret))
        assertFalse(out.contains("hunter"))
    }

    @Test
    fun `nodeText redacts even a long complex password to the fixed placeholder`() {
        val out = nodeText("P@ssw0rd!Sup3rSecret#2026", isPassword = true)
        assertEquals("·····", out)
        assertEquals("placeholder is always exactly 5 middots", 5, out.length)
    }

    @Test
    fun `nodeText returns raw text when not a password`() {
        assertEquals("Sign in", nodeText("Sign in", isPassword = false))
    }

    @Test
    fun `nodeText returns empty string for null raw text when not a password`() {
        assertEquals("", nodeText(null, isPassword = false))
    }

    @Test
    fun `nodeText returns placeholder for null raw text when password`() {
        // A password field with no readable text still must serialize as the
        // placeholder, never as empty (so the model still sees it's a secret).
        assertEquals("·····", nodeText(null, isPassword = true))
    }

    // ---- roleOf -----------------------------------------------------------

    @Test
    fun `roleOf takes the last dot-segment of the class name`() {
        assertEquals("Button", roleOf("android.widget.Button"))
        assertEquals("EditText", roleOf("android.widget.EditText"))
        assertEquals("TextView", roleOf("android.widget.TextView"))
    }

    @Test
    fun `roleOf returns View for null class name`() {
        assertEquals("View", roleOf(null))
    }

    @Test
    fun `roleOf returns View for blank class name`() {
        assertEquals("View", roleOf(""))
        assertEquals("View", roleOf("   "))
    }

    @Test
    fun `roleOf returns the whole string when there is no dot`() {
        assertEquals("MyCustomView", roleOf("MyCustomView"))
    }

    // ---- boundsString -----------------------------------------------------

    @Test
    fun `boundsString formats edges as l,t,r,b`() {
        assertEquals("10,20,300,80", boundsString(10, 20, 300, 80))
    }

    @Test
    fun `boundsString handles zero and negative offsets`() {
        assertEquals("0,0,0,0", boundsString(0, 0, 0, 0))
        assertEquals("-5,-10,15,20", boundsString(-5, -10, 15, 20))
    }

    // ---- nodesToJson ------------------------------------------------------

    @Test
    fun `nodesToJson serializes a button and a redacted password field`() {
        val nodes = listOf(
            UiNode(
                nodeId = 0,
                role = "Button",
                text = "Sign in",
                bounds = "0,0,100,40",
                clickable = true,
                editable = false,
                isPassword = false,
            ),
            UiNode(
                nodeId = 1,
                role = "EditText",
                // Already redacted by the reader via nodeText before reaching here.
                text = "·····",
                bounds = "0,50,200,90",
                clickable = true,
                editable = true,
                isPassword = true,
            ),
        )
        val json = nodesToJson(nodes)

        // Visible (non-secret) content is present.
        assertTrue("button text should be present", json.contains("Sign in"))
        // The redaction placeholder is present.
        assertTrue("placeholder should be present", json.contains("·····"))
        // snake_case field names per the @SerialName contract.
        assertTrue(json.contains("node_id"))
        assertTrue(json.contains("role"))
        assertTrue(json.contains("text"))
        assertTrue(json.contains("bounds"))
        assertTrue(json.contains("clickable"))
        assertTrue(json.contains("editable"))
        assertTrue(json.contains("is_password"))
    }

    @Test
    fun `nodesToJson of empty list is a valid empty JSON array`() {
        assertEquals("[]", nodesToJson(emptyList()))
    }

    // ---- resource_id: the stable handle (4.8 follow-up) --------------------

    @Test
    fun `UiNode serializes with a resource_id under the snake_case name`() {
        val node = UiNode(
            nodeId = 0,
            role = "TextView",
            text = "Display",
            resourceId = "com.android.settings:id/title",
            bounds = "0,0,100,40",
            clickable = true,
            editable = false,
            isPassword = false,
        )
        val json = nodesToJson(listOf(node))
        assertTrue("resource_id field name present", json.contains("resource_id"))
        assertTrue("resource id value present", json.contains("com.android.settings:id/title"))
    }

    @Test
    fun `UiNode resource_id defaults to empty string when omitted`() {
        // Nodes with no view id (Compose / custom / WebView) still serialize
        // cleanly with an empty resource_id; the model falls back to node_id.
        val node = UiNode(
            nodeId = 0,
            role = "View",
            text = "x",
            bounds = "0,0,1,1",
            clickable = true,
            editable = false,
            isPassword = false,
        )
        assertEquals("", node.resourceId)
        val json = nodesToJson(listOf(node))
        assertTrue(json.contains("\"resource_id\":\"\""))
    }

    @Test
    fun `resource_id does not affect password redaction`() {
        // resource_id is a dev-assigned view id, not screen text — adding it never
        // changes the redaction guarantee: a password field's raw text is still
        // dropped even though it carries a (non-secret) resource_id.
        val secret = "hunter2"
        val node = UiNode(
            nodeId = 0,
            role = "EditText",
            text = nodeText(secret, isPassword = true),
            resourceId = "com.app:id/password",
            bounds = "0,0,200,60",
            clickable = true,
            editable = true,
            isPassword = true,
        )
        val json = nodesToJson(listOf(node))
        assertFalse("raw password must NEVER reach the JSON even with a resource_id", json.contains(secret))
        assertTrue(json.contains("·····"))
        // The (non-secret) resource id is still present.
        assertTrue(json.contains("com.app:id/password"))
    }

    // ---- SECURITY: end-to-end through the reader's own path ----------------

    // ---- (M5.3) window topology: WindowInfo serialization + system-bar gate --

    private val topoJson = Json { encodeDefaults = true }

    @Test
    fun `WindowInfo serializes with the camelCase schema keys`() {
        val w = WindowInfo(displayId = 2, appPackage = "com.app", bounds = "0,0,1080,2400", isSystemBar = false)
        val json = topoJson.encodeToString(listOf(w))
        assertTrue("displayId key present", json.contains("\"displayId\":2"))
        assertTrue("appPackage key present", json.contains("\"appPackage\":\"com.app\""))
        assertTrue("bounds key present", json.contains("\"bounds\":\"0,0,1080,2400\""))
        assertTrue("isSystemBar key present", json.contains("\"isSystemBar\":false"))
    }

    @Test
    fun `WindowInfo carries no screen text — only package geometry type`() {
        // A window entry is package + geometry + a bool; there is no free-text field to leak.
        val json = topoJson.encodeToString(listOf(WindowInfo(0, "com.android.systemui", "0,0,1080,96", true)))
        assertTrue(json.contains("\"isSystemBar\":true"))
        assertFalse(json.contains("\"text\""))
    }

    @Test
    fun `isSystemBarWindow is true only for a TYPE_SYSTEM window`() {
        assertTrue(isSystemBarWindow(AccessibilityWindowInfo.TYPE_SYSTEM))
        assertFalse(isSystemBarWindow(AccessibilityWindowInfo.TYPE_APPLICATION))
        assertFalse(isSystemBarWindow(AccessibilityWindowInfo.TYPE_INPUT_METHOD))
        assertFalse(isSystemBarWindow(AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER))
    }

    @Test
    fun `topology entries carry distinct per-display displayIds (multi-display)`() {
        // (I2) Simulate the flattened getWindowsOnAllDisplays() output: an app window on the default
        // display (0), a second app window on a DeX / external display (2), and a system bar on 0.
        // The pure assembler must PRESERVE each window's displayId — the I2 fix: getWindows() (default
        // display only) collapsed every displayId to 0; getWindowsOnAllDisplays() carries the real id.
        val topo = listOf(
            windowInfoOf(0, "com.app.main", 0, 0, 1080, 2400, AccessibilityWindowInfo.TYPE_APPLICATION),
            windowInfoOf(2, "com.app.dex", 0, 0, 1920, 1080, AccessibilityWindowInfo.TYPE_APPLICATION),
            windowInfoOf(0, "com.android.systemui", 0, 0, 1080, 96, AccessibilityWindowInfo.TYPE_SYSTEM),
        )
        assertEquals(listOf(0, 2, 0), topo.map { it.displayId })
        assertEquals("two distinct displays represented", setOf(0, 2), topo.map { it.displayId }.toSet())
        // The extracted facts flow through: package, bounds, and the system-bar gate.
        assertEquals("com.app.dex", topo[1].appPackage)
        assertEquals("0,0,1920,1080", topo[1].bounds)
        assertTrue("the TYPE_SYSTEM window is a system bar", topo[2].isSystemBar)
        assertFalse("an app window is not a system bar", topo[0].isSystemBar)
    }

    @Test
    fun `serialized JSON for a password field never contains the raw secret`() {
        val rawSecret = "hunter2"
        // Build the UiNode for a password field via THE SAME path the reader
        // uses: nodeText(raw, isPassword=true). The raw secret must never be
        // copied into the node, and therefore never into the JSON.
        val node = UiNode(
            nodeId = 0,
            role = roleOf("android.widget.EditText"),
            text = nodeText(rawSecret, isPassword = true),
            bounds = boundsString(0, 0, 200, 60),
            clickable = true,
            editable = true,
            isPassword = true,
        )
        val json = nodesToJson(listOf(node))

        // The single most important assertion in this file.
        assertFalse("raw password must NEVER reach the serialized JSON", json.contains(rawSecret))
        // And the placeholder must be what's there instead.
        assertTrue(json.contains("·····"))
        assertTrue(json.contains("\"is_password\":true"))
    }
}
