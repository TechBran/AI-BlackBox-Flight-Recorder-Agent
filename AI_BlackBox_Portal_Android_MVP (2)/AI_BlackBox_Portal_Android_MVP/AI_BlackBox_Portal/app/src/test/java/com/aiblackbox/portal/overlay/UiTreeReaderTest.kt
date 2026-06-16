package com.aiblackbox.portal.overlay

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

    // ---- SECURITY: end-to-end through the reader's own path ----------------

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
