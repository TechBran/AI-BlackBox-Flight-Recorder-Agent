package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.local.FakePhoneController
import com.aiblackbox.portal.data.local.FakeToolBridge
import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.NativeTool
import com.aiblackbox.portal.data.local.NativeToolCallingLlm
import com.aiblackbox.portal.data.local.ResidentTools
import com.aiblackbox.portal.data.local.parseResultJsonString
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task W3 — routing test for the NATIVE (engine-driven) on-device tool path
 * ([ChatViewModel.streamLocalNativeAgentTurn]). Mirrors the FcLoop phone-routing
 * test style ([com.aiblackbox.portal.data.local.FcLoopTest]) but for the native
 * loop: it proves the path builds [NativeTool]s whose `execute` dispatches to the
 * [com.aiblackbox.portal.data.local.PhoneController] (never the cloud
 * [com.aiblackbox.portal.data.local.ToolBridge]), and that the engine-emitted
 * [LlmEvent]s render + persist exactly like the manual agent turn.
 *
 * The real LiteRtEngine native loop is framework/device-verified; here a
 * [FakeNativeToolCallingLlm] stands in for the engine: it INVOKES the scripted
 * NativeTool's `execute` itself (as the litertlm engine would when
 * automaticToolCalling=true) and emits the ToolCall/ToolOutcome/TextDelta events.
 */
class ChatViewModelNativeToolTest {

    /**
     * In-test [NativeToolCallingLlm] double. Drives the engine-side auto tool loop:
     * for each scripted (toolName, argsJson) it locates the matching [NativeTool],
     * calls its `execute` (which dispatches + returns the Gallery result JSON),
     * emits a [LlmEvent.ToolCall] and a parsed [LlmEvent.ToolOutcome], then emits a
     * final [LlmEvent.TextDelta]. Records the [tools] it was given.
     */
    private class FakeNativeToolCallingLlm(
        private val calls: List<Pair<String, String>>,
        private val finalText: String = "done",
    ) : NativeToolCallingLlm {
        var seenTools: List<NativeTool> = emptyList()
        override fun generateWithToolsNative(prompt: String, tools: List<NativeTool>): Flow<LlmEvent> = flow {
            seenTools = tools
            for ((name, argsJson) in calls) {
                val tool = tools.first { it.schema.name == name }
                val argsObj = (kotlinx.serialization.json.Json.parseToJsonElement(argsJson) as? JsonObject)
                    ?: JsonObject(emptyMap())
                emit(LlmEvent.ToolCall(name, argsObj))
                val resultJson = tool.execute(argsJson) // ENGINE drives execute -> dispatch.
                val (ok, payload) = parseResultJsonString(resultJson)
                emit(LlmEvent.ToolOutcome(name, ToolResult(success = ok, result = payload)))
            }
            emit(LlmEvent.TextDelta(finalText))
        }
    }

    private val phoneTools = ResidentTools.phoneActuators() + ResidentTools.intentActions()

    @Test
    fun `native path invokes NativeTool execute to phone dispatch and never the cloud bridge`() = runTest {
        // The engine "calls" open_app, with the model's args.
        val openArgs = """{"package":"com.android.settings"}"""
        val engine = FakeNativeToolCallingLlm(
            calls = listOf("open_app" to openArgs),
            finalText = "Settings opened",
        )
        val phone = FakePhoneController { name, _ ->
            when (name) {
                "open_app" -> ToolResult(success = true, result = JsonPrimitive("opened"))
                else -> ToolResult(success = false, result = JsonPrimitive("?"))
            }
        }
        // A recording cloud bridge that must stay UNTOUCHED — the native path has no
        // access to it (streamLocalNativeAgentTurn takes no bridge), this documents it.
        val bridge = FakeToolBridge()

        var saved: SaveRequest? = null
        val acc = StringBuilder()
        val sink: (String, Boolean) -> Unit = { content, _ -> acc.setLength(0); acc.append(content) }
        val saveSink: (SaveRequest, String) -> Unit = { req, _ -> saved = req }

        val ok = ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            prompt = "persona\n\nUser: open settings\nAssistant:",
            operator = "system",
            model = "gemma-4-e4b",
            text = "open settings",
            sink = sink,
            saveSink = saveSink,
        )

        assertTrue("native turn completes + saves", ok)
        // The NativeTool.execute ran and dispatched to the PHONE controller, with args.
        assertEquals(listOf("open_app"), phone.dispatched.map { it.first })
        assertEquals(
            "open_app dispatched with the model's args",
            "com.android.settings",
            phone.dispatched[0].second["package"]?.let { (it as JsonPrimitive).contentOrNull },
        )
        // ...and NEVER the cloud bridge (the security-relevant assertion).
        assertTrue("native phone tools must NOT reach bridge.execute", bridge.executeCalls.isEmpty())
        assertTrue("native phone tools must NOT reach bridge.searchTools", bridge.searchCalls.isEmpty())
        // The engine's events rendered inline + the final text streamed + persisted.
        assertTrue("tool call rendered inline", acc.contains("`[open_app]`"))
        assertTrue("final text streamed", acc.contains("Settings opened"))
        assertTrue("the persisted turn carries the assistant text",
            saved?.assistantResponse?.contains("Settings opened") == true)
    }

    @Test
    fun `native path only offers phone and intent tools, no search_tools or cloud tool`() = runTest {
        val engine = FakeNativeToolCallingLlm(calls = emptyList(), finalText = "hi")
        val phone = FakePhoneController()

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            prompt = "p",
            operator = "system",
            model = null,
            text = "hello",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        val offered = engine.seenTools.map { it.schema.name }.toSet()
        assertEquals(
            "native path offers exactly the phone actuators + intent actions",
            ResidentTools.PHONE_ACTUATORS + ResidentTools.INTENT_ACTIONS,
            offered,
        )
        assertTrue("search_tools is NOT offered on the native path (W3 scope)",
            ResidentTools.SEARCH_TOOLS !in offered)
    }

    @Test
    fun `native path routes an intent action through the phone controller too`() = runTest {
        val engine = FakeNativeToolCallingLlm(
            calls = listOf("show_map" to """{"query":"coffee"}"""),
            finalText = "map opened",
        )
        val phone = FakePhoneController { _, _ -> ToolResult(success = true, result = JsonPrimitive("opened maps")) }
        val bridge = FakeToolBridge()

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            prompt = "p",
            operator = "system",
            model = null,
            text = "coffee?",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        assertEquals(listOf("show_map"), phone.dispatched.map { it.first })
        assertTrue("intent actions must NOT reach the cloud bridge", bridge.executeCalls.isEmpty())
    }
}
