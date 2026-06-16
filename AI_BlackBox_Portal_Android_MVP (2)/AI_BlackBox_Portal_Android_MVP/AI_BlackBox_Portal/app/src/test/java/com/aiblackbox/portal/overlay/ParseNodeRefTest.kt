package com.aiblackbox.portal.overlay

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [parseNodeRef] — the PURE selection of HOW a tap/type target was
 * addressed (stable resource_id vs positional node_id).
 *
 * This is the key testable logic of the 4.8 follow-up (stable resource_id node
 * handles): the model SHOULD prefer the stable `resource_id` handle from
 * read_screen, falling back to the positional `node_id` only when there is no
 * resource id. parseNodeRef encodes exactly that precedence. The actual
 * resolution (findNodeByResourceId / findActionableNode) is framework
 * (AccessibilityNodeInfo) and device-verified, not unit-tested.
 */
class ParseNodeRefTest {

    @Test fun `resource_id present - ById`() {
        val ref = parseNodeRef(buildJsonObject { put("resource_id", JsonPrimitive("com.android.settings:id/title")) })
        assertEquals(NodeRef.ById("com.android.settings:id/title"), ref)
    }

    @Test fun `node_id int form - ByIndex`() {
        assertEquals(NodeRef.ByIndex(6), parseNodeRef(buildJsonObject { put("node_id", 6) }))
    }

    @Test fun `node_id float form (the device bug) 6_0 - ByIndex 6`() {
        // The exact shape Gemma emitted on-device: a JSON float. parseNodeRef must
        // tolerate it (via parseNodeId) and yield ByIndex(6).
        assertEquals(NodeRef.ByIndex(6), parseNodeRef(buildJsonObject { put("node_id", 6.0) }))
    }

    @Test fun `node_id string form - ByIndex`() {
        assertEquals(NodeRef.ByIndex(6), parseNodeRef(buildJsonObject { put("node_id", JsonPrimitive("6")) }))
        assertEquals(NodeRef.ByIndex(6), parseNodeRef(buildJsonObject { put("node_id", JsonPrimitive("6.0")) }))
    }

    @Test fun `resource_id WINS when both present`() {
        // Stability beats position: a present resource_id is always chosen.
        val ref = parseNodeRef(
            buildJsonObject {
                put("resource_id", JsonPrimitive("com.app:id/send"))
                put("node_id", 6)
            },
        )
        assertEquals(NodeRef.ById("com.app:id/send"), ref)
    }

    @Test fun `blank resource_id falls back to node_id`() {
        // An empty/whitespace resource_id is NOT a usable handle — fall through.
        assertEquals(NodeRef.ByIndex(6), parseNodeRef(buildJsonObject { put("resource_id", JsonPrimitive("")); put("node_id", 6) }))
        assertEquals(NodeRef.ByIndex(6), parseNodeRef(buildJsonObject { put("resource_id", JsonPrimitive("   ")); put("node_id", 6) }))
    }

    @Test fun `blank resource_id with no node_id - null`() {
        // Blank handle and no fallback index → nothing to act on.
        assertNull(parseNodeRef(buildJsonObject { put("resource_id", JsonPrimitive("")) }))
    }

    @Test fun `both absent - null`() {
        assertNull(parseNodeRef(buildJsonObject { }))
        assertNull(parseNodeRef(buildJsonObject { put("other", 5) }))
    }

    @Test fun `non-numeric node_id with no resource_id - null`() {
        assertNull(parseNodeRef(buildJsonObject { put("node_id", JsonPrimitive("abc")) }))
    }

    @Test fun `describe is a coarse non-secret label`() {
        // The log/result label surfaces only the resource id (a dev-assigned view
        // id, not user data) or the index — never node screen text.
        assertTrue(NodeRef.ById("com.app:id/send").describe().contains("com.app:id/send"))
        assertEquals("node 6", NodeRef.ByIndex(6).describe())
    }
}
