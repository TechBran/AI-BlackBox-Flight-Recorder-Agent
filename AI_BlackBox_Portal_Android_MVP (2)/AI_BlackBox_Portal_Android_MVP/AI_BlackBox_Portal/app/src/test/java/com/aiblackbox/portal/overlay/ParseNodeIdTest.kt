package com.aiblackbox.portal.overlay

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Unit tests for [parseNodeId] — the tolerant node_id arg parser.
 *
 * REGRESSION GUARD (Task 4.8 device finding): on a real device, the on-device
 * Gemma emitted `{"node_id": 6.0}` (a JSON FLOAT). The original strict
 * `intOrNull` parse returned null for "6.0", so every tap/type silently failed
 * with "node_id required". parseNodeId now accepts int / float / string forms.
 */
class ParseNodeIdTest {

    @Test fun `int form`() {
        assertEquals(6, parseNodeId(buildJsonObject { put("node_id", 6) }))
    }

    @Test fun `float form (the device bug) 6_0 - 6`() {
        // This is the exact shape Gemma emitted on-device.
        assertEquals(6, parseNodeId(buildJsonObject { put("node_id", 6.0) }))
        assertEquals(12, parseNodeId(buildJsonObject { put("node_id", 12.0) }))
    }

    @Test fun `string int and string float`() {
        assertEquals(6, parseNodeId(buildJsonObject { put("node_id", JsonPrimitive("6")) }))
        assertEquals(6, parseNodeId(buildJsonObject { put("node_id", JsonPrimitive("6.0")) }))
    }

    @Test fun `nodeId camelCase alias`() {
        assertEquals(3, parseNodeId(buildJsonObject { put("nodeId", 3.0) }))
    }

    @Test fun `absent or non-numeric - null`() {
        assertNull(parseNodeId(buildJsonObject { }))
        assertNull(parseNodeId(buildJsonObject { put("node_id", JsonPrimitive("abc")) }))
        assertNull(parseNodeId(buildJsonObject { put("other", 5) }))
    }
}
