package com.aiblackbox.portal.data.model

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Decode-safety tests for the /local/turn prepare + complete DTOs.
 *
 * The app's wire Json is lenient (ignoreUnknownKeys + isLenient + encodeDefaults,
 * NO naming strategy), so we mirror it here. The non-negotiable invariant: every
 * DTO field is defaulted, so a missing or extra field can NEVER throw during decode
 * and fault an on-device turn.
 */
class LocalTurnTest {

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        encodeDefaults = true
    }

    @Test
    fun `prepareResponse decodes full payload`() {
        val payload = """
            {
              "success": true,
              "turn_id": "TURN-123",
              "system_prompt": "You are the BlackBox.",
              "tools": [
                {"name": "roll_dice", "description": "Roll a die", "parameters": {"type": "object"}}
              ],
              "provenance": {
                "semantic": ["SNAP-A", "SNAP-B"],
                "checkpoint": ["CKPT-1"]
              },
              "budget": {"package_chars": 1234, "cap_chars": 16000}
            }
        """.trimIndent()

        val resp = json.decodeFromString<PrepareResponse>(payload)

        assertTrue(resp.success)
        assertEquals("TURN-123", resp.turnId)
        assertEquals("You are the BlackBox.", resp.systemPrompt)
        assertEquals(1, resp.tools.size)
        assertEquals("roll_dice", resp.tools[0].name)
        assertEquals(listOf("SNAP-A", "SNAP-B"), resp.provenance.semantic)
        assertEquals(listOf("CKPT-1"), resp.provenance.checkpoint)
        assertEquals(1234, resp.budget.packageChars)
        assertEquals(16000, resp.budget.capChars)
    }

    @Test
    fun `prepareResponse decodes with MISSING fields (decode-safe)`() {
        val resp = json.decodeFromString<PrepareResponse>("{}")

        assertEquals(false, resp.success)
        assertEquals("", resp.turnId)
        assertEquals("", resp.systemPrompt)
        assertTrue(resp.tools.isEmpty())
        assertTrue(resp.provenance.semantic.isEmpty())
        assertTrue(resp.provenance.checkpoint.isEmpty())
        assertEquals(0, resp.budget.packageChars)
        assertEquals(16000, resp.budget.capChars)
    }

    @Test
    fun `completeRequest round-trips`() {
        val args = JsonObject(mapOf("sides" to JsonPrimitive(6)))
        val original = CompleteRequest(
            turnId = "TURN-123",
            operator = "Brandon",
            prompt = "roll a die",
            finalResponse = "You rolled a 4.",
            toolTranscript = listOf(
                ToolCallRecord(name = "roll_dice", args = args, result = "4"),
            ),
        )

        val encoded = json.encodeToString(original)
        val decoded = json.decodeFromString<CompleteRequest>(encoded)

        assertEquals(original, decoded)
        assertEquals("TURN-123", decoded.turnId)
        assertEquals("Brandon", decoded.operator)
        assertEquals("roll a die", decoded.prompt)
        assertEquals("You rolled a 4.", decoded.finalResponse)
        assertEquals(1, decoded.toolTranscript.size)
        assertEquals("roll_dice", decoded.toolTranscript[0].name)
        assertEquals(args, decoded.toolTranscript[0].args)
        assertEquals("4", decoded.toolTranscript[0].result)
    }

    @Test
    fun `completeRequest encodes snake_case wire keys`() {
        val encoded = json.encodeToString(
            CompleteRequest(turnId = "T", finalResponse = "done", toolTranscript = emptyList()),
        )
        assertTrue("turn_id key present", encoded.contains("\"turn_id\""))
        assertTrue("final_response key present", encoded.contains("\"final_response\""))
        assertTrue("tool_transcript key present", encoded.contains("\"tool_transcript\""))
    }

    @Test
    fun `completeResponse decodes`() {
        val resp = json.decodeFromString<CompleteResponse>(
            """{"success":true,"snap_id":"SNAP-X","checkpoint_triggered":true}""",
        )
        assertTrue(resp.success)
        assertEquals("SNAP-X", resp.snapId)
        assertTrue(resp.checkpointTriggered)

        val empty = json.decodeFromString<CompleteResponse>("{}")
        assertEquals(false, empty.success)
        assertEquals("", empty.snapId)
        assertEquals(false, empty.checkpointTriggered)
    }

    @Test
    fun `extra unknown field is ignored`() {
        val payload = """
            {
              "success": true,
              "turn_id": "TURN-9",
              "debug": 1
            }
        """.trimIndent()

        val resp = json.decodeFromString<PrepareResponse>(payload)
        assertEquals("TURN-9", resp.turnId)
        assertTrue(resp.success)
    }
}
