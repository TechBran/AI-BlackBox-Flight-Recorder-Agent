package com.aiblackbox.portal.data.agent

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Audit P03/O06 (rank 30): the Claude agent's mode switch is real. Plan sends the read-only
 * `permission_mode:"plan"` with `skip_permissions:false`; only YOLO sends
 * `skip_permissions:true`; the old "Normal" (prompt-for-approval) mode no longer exists, and a
 * stale or unknown value never escalates.
 */
class ClaudeAgentPermissionModeTest {

    private fun parse(s: String): JsonObject = Json.parseToJsonElement(s).jsonObject

    @Test fun exactly_two_modes_and_normal_is_gone() {
        assertEquals(listOf("yolo", "plan"), AgentPermissionMode.ALL)
        assertEquals("YOLO", AgentPermissionMode.label(AgentPermissionMode.YOLO))
        assertEquals("Plan Mode", AgentPermissionMode.label(AgentPermissionMode.PLAN))
        assertFalse(AgentPermissionMode.ALL.contains("normal"))
    }

    @Test fun plan_prompt_is_read_only_and_never_skips_permissions() {
        val body = parse(buildPromptMessage("look", "Alice", "sonnet", AgentPermissionMode.PLAN))
        assertEquals("prompt", body["type"]!!.jsonPrimitive.content)
        assertEquals("plan", body["permission_mode"]!!.jsonPrimitive.content)
        assertFalse(body["skip_permissions"]!!.jsonPrimitive.boolean)
        assertEquals("look", body["text"]!!.jsonPrimitive.content)
        assertEquals("Alice", body["operator"]!!.jsonPrimitive.content)
        assertEquals("sonnet", body["model"]!!.jsonPrimitive.content)
    }

    @Test fun only_yolo_sends_skip_permissions_true() {
        val yolo = parse(buildPromptMessage("go", "Alice", "sonnet", AgentPermissionMode.YOLO))
        assertEquals("yolo", yolo["permission_mode"]!!.jsonPrimitive.content)
        assertTrue(yolo["skip_permissions"]!!.jsonPrimitive.boolean)
        for (mode in listOf("plan", "normal", "ask", "garbage")) {
            val b = parse(buildPromptMessage("go", "Alice", "sonnet", mode))
            assertFalse(mode, b["skip_permissions"]!!.jsonPrimitive.boolean)
            assertEquals(mode, "plan", b["permission_mode"]!!.jsonPrimitive.content)
        }
    }

    @Test fun legacy_auto_key_maps_to_yolo_and_blank_to_the_default() {
        assertEquals("yolo", AgentPermissionMode.normalize("auto"))
        assertEquals("yolo", AgentPermissionMode.normalize(" YOLO "))
        assertEquals("yolo", AgentPermissionMode.normalize(null))
        assertEquals("yolo", AgentPermissionMode.normalize(""))
        assertEquals("plan", AgentPermissionMode.normalize("normal"))
    }

    @Test fun a_follow_up_input_carries_the_current_mode() {
        val body = parse(buildInputMessage("next", AgentPermissionMode.PLAN))
        assertEquals("input", body["type"]!!.jsonPrimitive.content)
        assertEquals("plan", body["permission_mode"]!!.jsonPrimitive.content)
        assertFalse(body["skip_permissions"]!!.jsonPrimitive.boolean)
        // No mode given: the session keeps the one it started with.
        assertNull(parse(buildInputMessage("next", null))["permission_mode"])
    }
}
