package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.float
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM unit tests for the PURE, PRIMITIVE-TYPED mapper cores backing
 * [LiteRtEngine] (Task 2.6a): [plainTextOf], [argsToJsonObject],
 * [toolDescriptionJson], [bridgeDispatchedStub].
 *
 * **Why primitive-typed (the documented fallback).** The litertlm-android 0.13.1
 * artifact is compiled to Java-21 bytecode (class major version 65), but this
 * module's unit tests run on JDK 17 — so merely constructing a litertlm class
 * (Message/Content/ToolCall) in a test throws `UnsupportedClassVersionError`
 * (the class file won't verify under JDK 17; it is NOT a native-lib load). The
 * Android app build is fine (D8/R8 desugar these), but the host test JVM cannot
 * load them. So the mapper cores take primitives and are tested directly here;
 * the thin litertlm-typed adapters ([Message.plainText] / [openApiToolFor]) only
 * extract primitives and delegate, and are covered by `compileDebugKotlin` + the
 * 2.6b on-device smoke.
 *
 * Coverage:
 *   1. [plainTextOf] concatenates text pieces in order (callers pre-filter Text).
 *   2. [argsToJsonObject] maps a string, numbers, a boolean, a nested map, a list.
 *   3. [toolDescriptionJson] emits JSON carrying name/description/parameters.
 *   4. [bridgeDispatchedStub] throws (the OpenApiTool.execute stub is never called
 *      when automaticToolCalling=false).
 */
class LiteRtMappersTest {

    @Test
    fun `plainTextOf concatenates text pieces in order`() {
        assertEquals("Hello, world", plainTextOf(listOf("Hello", ", ", "world")))
    }

    @Test
    fun `plainTextOf of an empty list is empty`() {
        // A message with only non-text content yields an empty list of text pieces.
        assertEquals("", plainTextOf(emptyList()))
    }

    @Test
    fun `argsToJsonObject maps a string arg`() {
        val obj = argsToJsonObject(mapOf("city" to "Boston"))
        assertEquals("Boston", obj["city"]?.jsonPrimitive?.contentOrNull)
    }

    @Test
    fun `argsToJsonObject maps numeric and boolean args with preserved types`() {
        val obj = argsToJsonObject(
            mapOf(
                "count" to 7,
                "ratio" to 1.5,
                "enabled" to true,
            ),
        )
        assertEquals(7, obj["count"]?.jsonPrimitive?.int)
        assertEquals(1.5f, obj["ratio"]?.jsonPrimitive?.float)
        assertEquals(true, obj["enabled"]?.jsonPrimitive?.boolean)
        // Numbers/booleans must NOT be quoted strings.
        assertTrue((obj["count"] as JsonPrimitive).isString.not())
        assertTrue((obj["enabled"] as JsonPrimitive).isString.not())
    }

    @Test
    fun `argsToJsonObject maps a nested map`() {
        val obj = argsToJsonObject(
            mapOf("filter" to mapOf("status" to "open", "limit" to 3)),
        )
        val nested = obj["filter"]?.jsonObject
        assertNotNull("nested map should serialize to a JsonObject", nested)
        assertEquals("open", nested!!["status"]?.jsonPrimitive?.contentOrNull)
        assertEquals(3, nested["limit"]?.jsonPrimitive?.int)
    }

    @Test
    fun `argsToJsonObject maps a list arg`() {
        val obj = argsToJsonObject(mapOf("tags" to listOf("a", "b", 2)))
        val arr = obj["tags"]
        assertNotNull(arr)
        assertEquals("[\"a\",\"b\",2]", arr.toString())
    }

    @Test
    fun `argsToJsonObject tolerates a null value`() {
        val obj = argsToJsonObject(mapOf("maybe" to null))
        assertEquals("null", obj["maybe"].toString())
    }

    @Test
    fun `toolDescriptionJson emits JSON with name, description and parameters`() {
        val params: JsonObject = buildJsonObject {
            put("type", "object")
            put("properties", buildJsonObject { put("q", buildJsonObject { put("type", "string") }) })
        }

        val descJson = Json.parseToJsonElement(
            toolDescriptionJson("search_tools", "Find tools by intent", params),
        ).jsonObject

        assertEquals("search_tools", descJson["name"]?.jsonPrimitive?.contentOrNull)
        assertEquals("Find tools by intent", descJson["description"]?.jsonPrimitive?.contentOrNull)
        // The parameters object is carried through verbatim.
        assertEquals(params, descJson["parameters"]?.jsonObject)
    }

    @Test
    fun `bridgeDispatchedStub throws UnsupportedOperationException`() {
        // The OpenApiTool.execute stub must never be reachable when
        // automaticToolCalling=false; if it ever is, it fails loudly.
        val thrown = runCatching { bridgeDispatchedStub() }.exceptionOrNull()
        assertTrue(
            "bridgeDispatchedStub() must throw UnsupportedOperationException, got $thrown",
            thrown is UnsupportedOperationException,
        )
    }

    // -------------------------------------------------------------------------
    // resolveSampler (Task W2) — the PURE core behind SamplerSettings.toSamplerConfig.
    // -------------------------------------------------------------------------

    @Test
    fun `resolveSampler returns null when all overrides are null`() {
        // All-null SamplerSettings -> engine omits samplerConfig (prior behavior).
        assertEquals(null, resolveSampler(topK = null, topP = null, temperature = null))
    }

    @Test
    fun `resolveSampler passes through a full override trio as Double`() {
        val trio = resolveSampler(topK = 10, topP = 0.5f, temperature = 0.2f)
        assertNotNull(trio)
        assertEquals(10, trio!!.first)
        assertEquals(0.5, trio.second, 1e-6)
        assertEquals(0.2, trio.third, 1e-6)
    }

    @Test
    fun `resolveSampler fills missing fields with engine defaults when any is set`() {
        // Only topK set -> topP/temperature fall back to the LiteRtEngine defaults.
        val trio = resolveSampler(topK = 99, topP = null, temperature = null)
        assertNotNull(trio)
        assertEquals(99, trio!!.first)
        assertEquals(LiteRtEngine.DEFAULT_SAMPLER_TOP_P.toDouble(), trio.second, 1e-6)
        assertEquals(LiteRtEngine.DEFAULT_SAMPLER_TEMPERATURE.toDouble(), trio.third, 1e-6)

        // Only temperature set -> topK/topP fall back to defaults.
        val trio2 = resolveSampler(topK = null, topP = null, temperature = 0.9f)
        assertNotNull(trio2)
        assertEquals(LiteRtEngine.DEFAULT_SAMPLER_TOP_K, trio2!!.first)
        assertEquals(LiteRtEngine.DEFAULT_SAMPLER_TOP_P.toDouble(), trio2.second, 1e-6)
        assertEquals(0.9, trio2.third, 1e-6)
    }

    @Test
    fun `SamplerSettings isUnset reflects whether any override is set`() {
        assertTrue(SamplerSettings().isUnset)
        assertTrue(SamplerSettings(topK = null, topP = null, temperature = null).isUnset)
        assertTrue(!SamplerSettings(topK = 1).isUnset)
        assertTrue(!SamplerSettings(topP = 0.1f).isUnset)
        assertTrue(!SamplerSettings(temperature = 0.1f).isUnset)
    }

    // -------------------------------------------------------------------------
    // Native tool-calling path (Task W3) — toResultJsonString / parseResultJsonString
    // / ToolResult.toResultJsonString, the pure cores the engine-driven loop maps
    // a dispatched ToolResult through (Edge Gallery's
    // {"status":"succeeded"|"failed","result"/"error":...} shape).
    // -------------------------------------------------------------------------

    @Test
    fun `toResultJsonString emits the succeeded shape with the result payload`() {
        val json = Json.parseToJsonElement(
            toResultJsonString(success = true, result = JsonPrimitive("opened")),
        ).jsonObject
        assertEquals("succeeded", json["status"]?.jsonPrimitive?.contentOrNull)
        assertEquals("opened", json["result"]?.jsonPrimitive?.contentOrNull)
        // A succeeded result carries no "error" key.
        assertTrue("succeeded shape has no error key", json["error"] == null)
    }

    @Test
    fun `toResultJsonString emits the failed shape with the error payload`() {
        val json = Json.parseToJsonElement(
            toResultJsonString(success = false, result = JsonPrimitive("needs connection")),
        ).jsonObject
        assertEquals("failed", json["status"]?.jsonPrimitive?.contentOrNull)
        // The failure detail goes under "error", not "result".
        assertEquals("needs connection", json["error"]?.jsonPrimitive?.contentOrNull)
        assertTrue("failed shape has no result key", json["result"] == null)
    }

    @Test
    fun `toResultJsonString serializes a null payload as JSON null`() {
        val json = Json.parseToJsonElement(
            toResultJsonString(success = true, result = null),
        ).jsonObject
        assertEquals("succeeded", json["status"]?.jsonPrimitive?.contentOrNull)
        assertEquals(JsonNull, json["result"])
    }

    @Test
    fun `parseResultJsonString round-trips a succeeded result`() {
        val (ok, payload) = parseResultJsonString(
            toResultJsonString(success = true, result = JsonPrimitive("tapped node 3")),
        )
        assertTrue("status succeeded -> success=true", ok)
        assertEquals("tapped node 3", (payload as JsonPrimitive).contentOrNull)
    }

    @Test
    fun `parseResultJsonString round-trips a failed result and reads the error payload`() {
        val (ok, payload) = parseResultJsonString(
            toResultJsonString(success = false, result = JsonPrimitive("missing arg")),
        )
        assertFalse("status failed -> success=false", ok)
        assertEquals("missing arg", (payload as JsonPrimitive).contentOrNull)
    }

    @Test
    fun `parseResultJsonString surfaces malformed non-object input as a failure without throwing`() {
        // A model/engine that returns a bare string instead of the JSON shape must
        // NOT crash the native turn — it becomes a failed outcome carrying the text.
        val (ok, payload) = parseResultJsonString("not json at all")
        assertFalse("malformed input -> success=false", ok)
        assertEquals("not json at all", (payload as JsonPrimitive).contentOrNull)
    }

    @Test
    fun `ToolResult toResultJsonString adapter matches the pure core`() {
        val res = ToolResult(success = true, result = JsonPrimitive("done"))
        assertEquals(
            toResultJsonString(res.success, res.result),
            res.toResultJsonString(),
        )
    }

    // ---- Task W3 follow-up: formatCloudToolMatches (find_blackbox_tool payload) ----

    @Test
    fun `formatCloudToolMatches emits a name+description JSON array`() {
        val matches = listOf(
            ToolSchema(name = "generate_image", description = "Create an image", parameters = buildJsonObject {}),
            ToolSchema(name = "search_snapshots", description = "Search memory", parameters = buildJsonObject {}),
        )
        val arr = Json.parseToJsonElement(formatCloudToolMatches(matches)).jsonArray
        assertEquals(2, arr.size)
        assertEquals("generate_image", arr[0].jsonObject["name"]?.jsonPrimitive?.contentOrNull)
        assertEquals("Create an image", arr[0].jsonObject["description"]?.jsonPrimitive?.contentOrNull)
        assertEquals("search_snapshots", arr[1].jsonObject["name"]?.jsonPrimitive?.contentOrNull)
        // The verbose per-tool parameters schema is intentionally omitted from the payload.
        assertTrue("payload omits the parameters schema", arr[0].jsonObject["parameters"] == null)
    }

    @Test
    fun `formatCloudToolMatches of an empty list is an empty JSON array`() {
        assertEquals(0, Json.parseToJsonElement(formatCloudToolMatches(emptyList())).jsonArray.size)
    }

    // -------------------------------------------------------------------------
    // Vision path (Task W4) — visionEnabled (the supportImage gating decision) +
    // orderVisionContents (images-before-text ordering). PURE cores; the litertlm-
    // typed glue (visionBackendFor / generateWithImage / Content.ImageBytes) can't
    // be constructed on the JDK-17 host test JVM (see the Mappers header) and is
    // covered by compileDebugKotlin + the on-device smoke.
    // -------------------------------------------------------------------------

    @Test
    fun `visionEnabled is true only for an image-capable model`() {
        assertTrue("supportImage=true -> vision on", visionEnabled(true))
        assertFalse("supportImage=false -> vision off (text path untouched)", visionEnabled(false))
    }

    @Test
    fun `orderVisionContents puts images before the text`() {
        // Generic over the content type so it tests with plain Strings.
        val ordered = orderVisionContents(images = listOf("img1", "img2"), textContent = "prompt")
        assertEquals(listOf("img1", "img2", "prompt"), ordered)
        // The text is LAST (Edge Gallery's runInference(images) ordering).
        assertEquals("prompt", ordered.last())
    }

    @Test
    fun `orderVisionContents with a single image yields image then text`() {
        assertEquals(listOf("frame", "describe"), orderVisionContents(listOf("frame"), "describe"))
    }

    @Test
    fun `orderVisionContents with no images is just the text`() {
        // Defensive: the engine guards images.isNotEmpty() before calling, but the
        // pure orderer is well-defined for the empty case (text only).
        assertEquals(listOf("only-text"), orderVisionContents(emptyList<String>(), "only-text"))
    }

    // -------------------------------------------------------------------------
    // Graceful GPU-vision degrade (W4 follow-up) — shouldRetryWithoutVision is the
    // PURE decision LiteRtEngine.load() consults when the engine's first
    // initialize() throws: retry ONCE text-only IFF this was a vision bundle whose
    // visionBackend was set (so the GPU vision backend could be the culprit and a
    // text-only fallback exists). The initialize()/retry itself is device-verified.
    // -------------------------------------------------------------------------

    @Test
    fun `shouldRetryWithoutVision retries only for a vision bundle whose vision backend was set`() {
        // The ONLY true case: a supportImage bundle that DID set a visionBackend.
        // GPU vision init could be the failure → retry text-only so text still loads.
        assertTrue(
            "vision bundle + visionBackend set -> retry text-only",
            shouldRetryWithoutVision(supportImage = true, visionWasSet = true),
        )
    }

    @Test
    fun `shouldRetryWithoutVision does not retry a text-only bundle (no vision to drop)`() {
        // Text-only bundle: no visionBackend was ever set, so an init failure is a
        // genuine failure with nothing to retry — the caller rethrows.
        assertFalse(
            "text-only bundle, no visionBackend -> no retry",
            shouldRetryWithoutVision(supportImage = false, visionWasSet = false),
        )
    }

    @Test
    fun `shouldRetryWithoutVision does not retry when no vision backend was set even for a vision bundle`() {
        // Defensive: a vision bundle that somehow did NOT set a visionBackend has
        // no GPU-vision backend to blame and nothing to fall back FROM → no retry.
        assertFalse(
            "supportImage but visionBackend not set -> no retry",
            shouldRetryWithoutVision(supportImage = true, visionWasSet = false),
        )
        // And a non-vision bundle that (impossibly) had a backend set is still no.
        assertFalse(
            "not a vision bundle -> no retry regardless",
            shouldRetryWithoutVision(supportImage = false, visionWasSet = true),
        )
    }

    // -------------------------------------------------------------------------
    // App-side native-loop step cap (Task W3 hardening) -- overCap is the PURE
    // decision nativeOpenApiToolFor's execute consults: increment a shared per-turn
    // counter, and if overCap(count, MAX_NATIVE_TOOL_CALLS) refuse to run the tool
    // body and return a terminal "step limit reached" result. Defense-in-depth UNDER
    // the litertlm engine's own recurring-tool-call guard. The enforcement inside
    // execute is device/compile-verified; here we pin the boundary arithmetic.
    // -------------------------------------------------------------------------

    @Test
    fun `overCap is false below the cap`() {
        // The first call(s) of a turn (count well under max) run normally.
        assertFalse("count 1 of 24 -> run", overCap(callCount = 1, max = 24))
        assertFalse("count 23 of 24 -> run", overCap(callCount = 23, max = 24))
    }

    @Test
    fun `overCap is false at the cap boundary (the cap-th call still runs)`() {
        // count == max is the LAST allowed call (1-based): exactly max executions run.
        assertFalse("count 24 of 24 -> still runs", overCap(callCount = 24, max = 24))
    }

    @Test
    fun `overCap is true above the cap (the next call is refused)`() {
        // count > max: refuse the body, return the terminal step-limit result.
        assertTrue("count 25 of 24 -> refuse", overCap(callCount = 25, max = 24))
        assertTrue("far over the cap -> refuse", overCap(callCount = 100, max = 24))
    }

    @Test
    fun `overCap honors the real MAX_NATIVE_TOOL_CALLS constant`() {
        // Pin the wired-in cap: the constant-th call runs, the next is refused.
        val max = LiteRtEngine.MAX_NATIVE_TOOL_CALLS
        assertFalse("at the constant cap -> runs", overCap(callCount = max, max = max))
        assertTrue("one past the constant cap -> refused", overCap(callCount = max + 1, max = max))
    }
}
