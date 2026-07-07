package com.aiblackbox.portal.data.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M3 task 3.8: pure state logic for the System-Menu "Devices" (tailnet mesh) view.
 * Verifies the `GET /devices/mesh` response maps onto [MeshDevice] with the exact
 * backend field names, and that the derived ownership flag / malformed-input handling
 * behave as the UI relies on. No Android dependencies.
 */
class MeshDeviceParseTest {

    // A representative /devices/mesh body: one claimed+primary device, one un-claimed
    // tailnet node, plus an unknown top-level + per-row key the parser must ignore.
    private val meshJson = """
        {
          "operator": null,
          "extra_top_level": 1,
          "devices": [
            {
              "id": "brandon-fold6",
              "name": "Brandon Fold6",
              "tailnet": "brandon-fold6.tail401fb3.ts.net",
              "type": "android",
              "online": true,
              "owner": "Brandon",
              "is_primary": true,
              "default_provider": "gemma",
              "future_field": "ignored"
            },
            {
              "id": "device-100-64-9-9",
              "name": "spare-laptop",
              "tailnet": "100.64.9.9",
              "type": "linux",
              "online": false,
              "owner": null,
              "is_primary": false,
              "default_provider": null
            }
          ]
        }
    """.trimIndent()

    @Test fun parses_all_rows_with_backend_field_names() {
        val devices = parseMeshDevices(meshJson)
        assertEquals(2, devices.size)

        val primary = devices[0]
        assertEquals("brandon-fold6", primary.id)
        assertEquals("Brandon Fold6", primary.name)
        assertEquals("brandon-fold6.tail401fb3.ts.net", primary.tailnet)
        assertEquals("android", primary.type)
        assertTrue(primary.online)
        assertEquals("Brandon", primary.owner)
        assertTrue("is_primary must map to isPrimary", primary.isPrimary)
        assertEquals("gemma", primary.defaultProvider)
    }

    @Test fun unclaimed_node_has_no_owner_or_provider() {
        val spare = parseMeshDevices(meshJson)[1]
        assertNull(spare.owner)
        assertNull(spare.defaultProvider)
        assertFalse(spare.online)
        assertFalse(spare.isPrimary)
    }

    @Test fun isClaimed_reflects_owner_presence() {
        val devices = parseMeshDevices(meshJson)
        assertTrue("owned device is claimed", devices[0].isClaimed)
        assertFalse("unowned device is not claimed", devices[1].isClaimed)
        // Blank owner (not just null) is also treated as unclaimed.
        assertFalse(MeshDevice(id = "x", owner = "").isClaimed)
    }

    @Test fun malformed_or_empty_input_degrades_to_empty_list() {
        assertTrue(parseMeshDevices("not json at all").isEmpty())
        assertTrue(parseMeshDevices("").isEmpty())
        assertTrue(parseMeshDevices("""{"devices": []}""").isEmpty())
    }

    @Test fun provider_choices_match_backend_contract() {
        assertEquals(listOf("gemma", "gemini", "claude", "openai"), MESH_PROVIDER_CHOICES)
    }

    // --- parseOperators (M4.1: the owner dropdown was permanently empty) ----------
    // `GET /operators` returns a STRING array, `{"operators":["Brandon","Anna"]}`.
    // The old code read `it.jsonObject["operator"]`, which throws on a JsonPrimitive,
    // so the roster silently parsed to []. These pin the real shape + the degrade path.

    @Test fun parses_operators_string_array() {
        val ops = parseOperators("""{"operators":["Brandon","Anna"]}""")
        assertEquals(listOf("Brandon", "Anna"), ops)
    }

    @Test fun parse_operators_drops_blank_entries() {
        val ops = parseOperators("""{"operators":["Brandon","","  ","Anna"]}""")
        assertEquals(listOf("Brandon", "Anna"), ops)
    }

    @Test fun parse_operators_wrong_shape_degrades_to_empty() {
        // The OLD (incorrect) object-array shape must NOT throw — it degrades to empty.
        assertTrue(parseOperators("""{"operators":[{"operator":"Brandon"}]}""").isEmpty())
    }

    @Test fun parse_operators_malformed_or_missing_key_degrades_to_empty() {
        assertTrue(parseOperators("not json at all").isEmpty())
        assertTrue(parseOperators("").isEmpty())
        assertTrue(parseOperators("{}").isEmpty())
        assertTrue(parseOperators("""{"operators":[]}""").isEmpty())
    }
}
