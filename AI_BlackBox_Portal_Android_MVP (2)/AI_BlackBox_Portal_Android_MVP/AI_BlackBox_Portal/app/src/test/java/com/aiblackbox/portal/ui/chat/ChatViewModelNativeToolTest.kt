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
import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task W3 (+ follow-up) — routing tests for the NATIVE (engine-driven) on-device
 * tool path ([ChatViewModel.streamLocalNativeAgentTurn]). Mirrors the FcLoop
 * phone-routing test style ([com.aiblackbox.portal.data.local.FcLoopTest]) but for
 * the native loop. It proves:
 *  - PHONE/INTENT tools become [NativeTool]s whose `execute` dispatches to the
 *    [com.aiblackbox.portal.data.local.PhoneController] (NEVER the cloud
 *    [com.aiblackbox.portal.data.local.ToolBridge]);
 *  - CLOUD tools (find_blackbox_tool / run_blackbox_tool, W3 follow-up) become
 *    [NativeTool]s whose `execute` reaches the [ToolBridge] (NEVER the phone),
 *    operator-scoped, carrying only the model's args;
 *  - the two families coexist in ONE native loop and render + persist exactly like
 *    the manual agent turn.
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
        // A recording cloud bridge that must stay UNTOUCHED by a phone call.
        val bridge = FakeToolBridge()

        var saved: SaveRequest? = null
        val acc = StringBuilder()
        val sink: (String, Boolean) -> Unit = { content, _ -> acc.setLength(0); acc.append(content) }
        val saveSink: (SaveRequest, String) -> Unit = { req, _ -> saved = req }

        val ok = ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            bridge = bridge,
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
    fun `native path offers phone, intent AND the cloud-vault tools (W3 follow-up)`() = runTest {
        val engine = FakeNativeToolCallingLlm(calls = emptyList(), finalText = "hi")
        val phone = FakePhoneController()
        val bridge = FakeToolBridge()

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            bridge = bridge,
            prompt = "p",
            operator = "system",
            model = null,
            text = "hello",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        val offered = engine.seenTools.map { it.schema.name }.toSet()
        assertEquals(
            "native path offers the phone actuators + intent actions + cloud-vault tools",
            ResidentTools.PHONE_ACTUATORS + ResidentTools.INTENT_ACTIONS + ResidentTools.CLOUD_TOOLS,
            offered,
        )
        // The manual-path discovery name (search_tools) is NOT used on the native path;
        // the native cloud entrypoint is find_blackbox_tool instead.
        assertTrue("manual search_tools is NOT offered on the native path",
            ResidentTools.SEARCH_TOOLS !in offered)
        assertTrue("find_blackbox_tool IS offered", ResidentTools.FIND_BLACKBOX_TOOL in offered)
        assertTrue("run_blackbox_tool IS offered", ResidentTools.RUN_BLACKBOX_TOOL in offered)
    }

    // ---- Task 10 (snapshot ledger): server-injected DIRECT tools in the native loop ----
    //
    // /local/turn/prepare returns the top-K relevant tools; streamLocalNativeAgentTurn
    // takes them via the new `injectedTools` param and offers them as DIRECTLY-callable
    // NativeTools (the model calls each by its real name, e.g. roll_dice) ALONGSIDE the
    // resident phone/intent tools and the find/run cloud fallback. Order: phone, then
    // injected direct tools, then the find/run fallback.

    @Test
    fun `injected tools are offered as direct native tools alongside phone + the find-run fallback`() = runTest {
        val engine = FakeNativeToolCallingLlm(calls = emptyList(), finalText = "hi")
        val phone = FakePhoneController()
        val bridge = FakeToolBridge()
        val injected = listOf(
            ToolSchema(name = "roll_dice", description = "Roll a die", parameters = buildJsonObject {}),
            ToolSchema(name = "generate_image", description = "Make an image", parameters = buildJsonObject {}),
        )

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            bridge = bridge,
            prompt = "p",
            injectedTools = injected,
            operator = "Brandon",
            model = null,
            text = "roll a dice",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        val offeredNames = engine.seenTools.map { it.schema.name }
        // The server-injected tools are offered DIRECTLY (by their real names)...
        assertTrue("injected roll_dice offered directly", "roll_dice" in offeredNames)
        assertTrue("injected generate_image offered directly", "generate_image" in offeredNames)
        // ...the resident phone/intent tools are still offered...
        assertTrue("phone actuators still offered",
            ResidentTools.PHONE_ACTUATORS.all { it in offeredNames })
        // ...and the find/run fallback is STILL present (long-tail reachability).
        assertTrue("find_blackbox_tool fallback retained", ResidentTools.FIND_BLACKBOX_TOOL in offeredNames)
        assertTrue("run_blackbox_tool fallback retained", ResidentTools.RUN_BLACKBOX_TOOL in offeredNames)
        // Order contract: phone first, then injected, then the find/run fallback.
        val rollIdx = offeredNames.indexOf("roll_dice")
        val findIdx = offeredNames.indexOf(ResidentTools.FIND_BLACKBOX_TOOL)
        val firstPhoneIdx = offeredNames.indexOf(ResidentTools.PHONE_ACTUATORS.first())
        assertTrue("phone tools precede injected", firstPhoneIdx in 0 until rollIdx)
        assertTrue("injected precede the find/run fallback", rollIdx in 0 until findIdx)
    }

    @Test
    fun `an injected tool dispatches directly to the bridge with its own name, not via find-run`() = runTest {
        // The engine "calls" the injected roll_dice by its REAL name (no find_blackbox_tool first).
        val engine = FakeNativeToolCallingLlm(
            calls = listOf("roll_dice" to """{"sides":6}"""),
            finalText = "rolled",
        )
        val phone = FakePhoneController() // must stay untouched
        val bridge = FakeToolBridge(
            executeFn = { _, _ -> ToolResult(success = true, result = JsonPrimitive("rolled-4")) },
        )
        val injected = listOf(
            ToolSchema(name = "roll_dice", description = "Roll a die", parameters = buildJsonObject {}),
        )

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            bridge = bridge,
            prompt = "p",
            injectedTools = injected,
            operator = "Brandon",
            model = null,
            text = "roll a dice",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        // Direct: the bridge ran roll_dice by name; no find_blackbox_tool indirection; phone untouched.
        assertEquals(listOf("roll_dice"), bridge.executeCalls.map { it.first })
        assertEquals("Brandon", bridge.executeOperators.single())
        assertTrue("a direct injected call must NOT search the catalog first", bridge.searchCalls.isEmpty())
        assertTrue("an injected tool must NOT reach the phone controller", phone.dispatched.isEmpty())
    }

    @Test
    fun `injectedTools defaults to empty so existing callers are unaffected`() = runTest {
        // The manual/offline callers omit injectedTools entirely; the offered set is
        // then exactly the legacy phone + find/run fallback (no extra direct tools).
        val engine = FakeNativeToolCallingLlm(calls = emptyList(), finalText = "hi")
        val phone = FakePhoneController()
        val bridge = FakeToolBridge()

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            bridge = bridge,
            prompt = "p",
            operator = "system",
            model = null,
            text = "hello",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        val offered = engine.seenTools.map { it.schema.name }.toSet()
        assertEquals(
            "no injectedTools -> exactly phone actuators + intent actions + cloud find/run",
            ResidentTools.PHONE_ACTUATORS + ResidentTools.INTENT_ACTIONS + ResidentTools.CLOUD_TOOLS,
            offered,
        )
    }

    @Test
    fun `with no bridge the native path is phone-only (cloud tools omitted)`() = runTest {
        val engine = FakeNativeToolCallingLlm(calls = emptyList(), finalText = "hi")
        val phone = FakePhoneController()

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            bridge = null,
            prompt = "p",
            operator = "system",
            model = null,
            text = "hello",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        val offered = engine.seenTools.map { it.schema.name }.toSet()
        assertEquals(
            "no bridge -> only phone actuators + intent actions",
            ResidentTools.PHONE_ACTUATORS + ResidentTools.INTENT_ACTIONS,
            offered,
        )
        assertTrue("no cloud tools without a bridge",
            ResidentTools.CLOUD_TOOLS.none { it in offered })
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
            bridge = bridge,
            prompt = "p",
            operator = "system",
            model = null,
            text = "coffee?",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        assertEquals(listOf("show_map"), phone.dispatched.map { it.first })
        assertTrue("intent actions must NOT reach the cloud bridge", bridge.executeCalls.isEmpty())
        assertTrue("intent actions must NOT search the cloud bridge", bridge.searchCalls.isEmpty())
    }

    // ---- Task W3 follow-up: cloud-vault NativeTools (engine-driven) -------------

    @Test
    fun `find_blackbox_tool execute calls bridge searchTools and returns formatted matches`() = runTest {
        val matches = listOf(
            ToolSchema(name = "generate_image", description = "Create an image", parameters = buildJsonObject {}),
        )
        val bridge = FakeToolBridge(searchMap = mapOf("make a picture" to matches))
        val cloudTools = ChatViewModel.buildCloudNativeTools(bridge, operator = "Brandon")
        val search = cloudTools.first { it.schema.name == ResidentTools.FIND_BLACKBOX_TOOL }

        val resultJson = search.execute("""{"query":"make a picture"}""")

        // The bridge was hit with the model's query, at the injection cap k.
        assertEquals(listOf("make a picture"), bridge.searchCalls)
        assertEquals(ResidentTools.MAX_INJECTED_SCHEMAS, bridge.searchKs.single())
        // It returned a SUCCEEDED Gallery-shaped result whose payload lists the match name.
        val (ok, payload) = parseResultJsonString(resultJson)
        assertTrue("succeeded", ok)
        assertTrue("payload carries the discovered tool name",
            (payload as? JsonPrimitive)?.contentOrNull?.contains("generate_image") == true)
    }

    @Test
    fun `find_blackbox_tool execute with no match returns a failed result`() = runTest {
        val bridge = FakeToolBridge() // empty search map -> emptyList()
        val search = ChatViewModel.buildCloudNativeTools(bridge, operator = "system")
            .first { it.schema.name == ResidentTools.FIND_BLACKBOX_TOOL }

        val (ok, _) = parseResultJsonString(search.execute("""{"query":"nothing here"}"""))
        assertFalse("empty matches -> failed result", ok)
    }

    @Test
    fun `run_blackbox_tool execute calls bridge execute with name, parsed args and operator`() = runTest {
        val bridge = FakeToolBridge(
            executeFn = { _, _ -> ToolResult(success = true, result = JsonPrimitive("image-url")) },
        )
        val call = ChatViewModel.buildCloudNativeTools(bridge, operator = "Brandon")
            .first { it.schema.name == ResidentTools.RUN_BLACKBOX_TOOL }

        val resultJson = call.execute("""{"name":"generate_image","args":{"prompt":"a cat"}}""")

        // The bridge.execute was called with the chosen tool name + parsed args + operator.
        assertEquals(listOf("generate_image"), bridge.executeCalls.map { it.first })
        assertEquals(
            "a cat",
            bridge.executeCalls.single().second["prompt"]?.let { (it as JsonPrimitive).contentOrNull },
        )
        assertEquals("Brandon", bridge.executeOperators.single())
        // It returned the bridge's result as the Gallery-shaped success JSON.
        val (ok, payload) = parseResultJsonString(resultJson)
        assertTrue("succeeded", ok)
        assertEquals("image-url", (payload as? JsonPrimitive)?.contentOrNull)
    }

    @Test
    fun `run_blackbox_tool accepts args supplied as a JSON-encoded string`() = runTest {
        val bridge = FakeToolBridge()
        val call = ChatViewModel.buildCloudNativeTools(bridge, operator = "system")
            .first { it.schema.name == ResidentTools.RUN_BLACKBOX_TOOL }

        // Some small models emit args as a STRING rather than a nested object.
        call.execute("""{"name":"search_snapshots","args":"{\"q\":\"hi\"}"}""")

        assertEquals(listOf("search_snapshots"), bridge.executeCalls.map { it.first })
        assertEquals(
            "hi",
            bridge.executeCalls.single().second["q"]?.let { (it as JsonPrimitive).contentOrNull },
        )
    }

    @Test
    fun `run_blackbox_tool with no name is a failed result and never hits the bridge`() = runTest {
        val bridge = FakeToolBridge()
        val call = ChatViewModel.buildCloudNativeTools(bridge, operator = "system")
            .first { it.schema.name == ResidentTools.RUN_BLACKBOX_TOOL }

        val (ok, _) = parseResultJsonString(call.execute("""{"args":{"x":1}}"""))
        assertFalse("missing name -> failed", ok)
        assertTrue("a nameless call must not reach the bridge", bridge.executeCalls.isEmpty())
    }

    @Test
    fun `routing - in a native turn a cloud call hits the bridge and NOT the phone`() = runTest {
        // The engine "calls" run_blackbox_tool; phone must stay UNTOUCHED, bridge must fire.
        val engine = FakeNativeToolCallingLlm(
            calls = listOf(
                ResidentTools.RUN_BLACKBOX_TOOL to """{"name":"generate_image","args":{"prompt":"x"}}""",
            ),
            finalText = "image made",
        )
        val phone = FakePhoneController() // must NOT be dispatched to
        val bridge = FakeToolBridge(
            executeFn = { _, _ -> ToolResult(success = true, result = JsonPrimitive("ok")) },
        )

        ChatViewModel.streamLocalNativeAgentTurn(
            engine = engine,
            phone = phone,
            phoneTools = phoneTools,
            bridge = bridge,
            prompt = "p",
            operator = "system",
            model = null,
            text = "make an image",
            sink = { _, _ -> },
            saveSink = { _, _ -> },
        )

        // SECURITY: a cloud tool reached the bridge, NEVER the PhoneController.
        assertEquals(listOf("generate_image"), bridge.executeCalls.map { it.first })
        assertTrue("a cloud tool must NOT reach the phone controller", phone.dispatched.isEmpty())
    }

    // -- Fix 2 (final-pass review): the native persona addendum must only name the
    //    cloud tools when a cloud bridge is actually wired. With no bridge the
    //    native turn registers no find_blackbox_tool / run_blackbox_tool, so the
    //    prompt must not advertise them. nativeAddendum(hasCloud) is the PURE
    //    decision the runLocalEngineTurn call site uses (hasCloud = bridge != null).

    @Test
    fun `nativeAddendum with cloud names the cloud tools and the phone actions`() {
        val s = ChatViewModel.nativeAddendum(hasCloud = true)
        // Phone steering is always present.
        assertTrue("phone action example present", s.contains("flashlight_on"))
        assertTrue("closing instruction present", s.contains("Call one tool at a time"))
        // Cloud sentence present iff hasCloud.
        assertTrue("find_blackbox_tool advertised when cloud present", s.contains("find_blackbox_tool"))
        assertTrue("run_blackbox_tool advertised when cloud present", s.contains("run_blackbox_tool"))
        // R2-B disambiguation: the addendum must steer the model AWAY from web_search
        // for its own tools (the name-collision bug that sent "roll a dice" to the browser).
        assertTrue("addendum tells the model NOT to use web_search for tools",
            s.contains("Do NOT use web_search to find your tools"))
    }

    @Test
    fun `nativeAddendum without cloud omits the cloud tools but keeps phone steering`() {
        val s = ChatViewModel.nativeAddendum(hasCloud = false)
        // Phone steering still present...
        assertTrue("phone action example present", s.contains("flashlight_on"))
        assertTrue("closing instruction present", s.contains("Call one tool at a time"))
        // ...but the offline native turn must NOT name tools that aren't registered.
        assertFalse("find_blackbox_tool must NOT be advertised offline", s.contains("find_blackbox_tool"))
        assertFalse("run_blackbox_tool must NOT be advertised offline", s.contains("run_blackbox_tool"))
        // The no-cloud variant is exactly the base phone addendum.
        assertEquals(ChatViewModel.NATIVE_PHONE_CONTROL_ADDENDUM, s)
    }

    @Test
    fun `nativeAddendum splices the cloud sentence cleanly (no double spaces, single insertion)`() {
        val s = ChatViewModel.nativeAddendum(hasCloud = true)
        assertFalse("no double space introduced by the splice", s.contains("  "))
        // The cloud sentence sits before the closing instruction, exactly once.
        val cloudIdx = s.indexOf("find_blackbox_tool")
        val closeIdx = s.indexOf("Call one tool at a time")
        assertTrue("cloud sentence precedes the closing instruction", cloudIdx in 0 until closeIdx)
        assertEquals("exactly one closing instruction", closeIdx, s.lastIndexOf("Call one tool at a time"))
    }
}
