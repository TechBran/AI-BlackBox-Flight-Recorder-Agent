package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.local.FakeToolBridge
import com.aiblackbox.portal.data.local.parseResultJsonString
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task 8 (on-device snapshot ledger plan) — unit tests for
 * [ChatViewModel.buildInjectedNativeTools]: the top-K tool schemas returned by
 * `/local/turn/prepare` become DIRECTLY-callable [com.aiblackbox.portal.data.local.NativeTool]s
 * (the model calls each by its real name, e.g. `roll_dice`, with no
 * find_blackbox_tool/run_blackbox_tool indirection).
 *
 * Sibling to [ChatViewModelNativeToolTest] (which covers buildCloudNativeTools); it
 * reuses the same [FakeToolBridge]. The SECURITY-relevant guarantee mirrored here:
 * an injected/cloud tool routes to [com.aiblackbox.portal.data.local.ToolBridge.execute]
 * ONLY, never the phone PhoneController.
 */
class ChatViewModelInjectedToolTest {

    @Test
    fun `maps each schema to a native tool with matching name`() {
        val schemas = listOf(
            ToolSchema(name = "roll_dice", description = "Roll a die", parameters = buildJsonObject {}),
            ToolSchema(name = "generate_image", description = "Make an image", parameters = buildJsonObject {}),
        )
        val bridge = FakeToolBridge()

        val tools = ChatViewModel.buildInjectedNativeTools(schemas, bridge, operator = "Brandon")

        assertEquals(2, tools.size)
        assertEquals(listOf("roll_dice", "generate_image"), tools.map { it.schema.name })
    }

    @Test
    fun `execute routes directly to the bridge with the tool's own name + args`() = runTest {
        val bridge = FakeToolBridge(
            executeFn = { _, _ -> ToolResult(success = true, result = JsonPrimitive("rolled-4")) },
        )
        val schemas = listOf(
            ToolSchema(name = "roll_dice", description = "Roll a die", parameters = buildJsonObject {}),
        )

        val tools = ChatViewModel.buildInjectedNativeTools(schemas, bridge, operator = "system")
        // The tool's args ARE the payload (no nested "args"): the model calls roll_dice directly.
        val resultJson = tools[0].execute("""{"sides":6}""")

        // The bridge was hit with the tool's OWN fixed name (schema.name), parsed args, + operator.
        assertEquals(listOf("roll_dice"), bridge.executeCalls.map { it.first })
        assertEquals(
            "args are the whole payload",
            "6",
            bridge.executeCalls.single().second["sides"]?.let { (it as JsonPrimitive).contentOrNull },
        )
        assertEquals("system", bridge.executeOperators.single())
        // The returned String is the toResultJsonString() of the fake's ToolResult.
        val (ok, payload) = parseResultJsonString(resultJson)
        assertTrue("succeeded", ok)
        assertEquals("rolled-4", (payload as? JsonPrimitive)?.contentOrNull)
    }

    @Test
    fun `empty list to empty`() {
        val bridge = FakeToolBridge()
        assertTrue(ChatViewModel.buildInjectedNativeTools(emptyList(), bridge, "Brandon").isEmpty())
    }
}
