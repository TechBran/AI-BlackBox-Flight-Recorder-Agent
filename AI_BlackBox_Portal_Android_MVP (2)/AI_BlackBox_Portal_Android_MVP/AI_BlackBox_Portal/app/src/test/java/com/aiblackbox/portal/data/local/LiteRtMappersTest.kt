package com.aiblackbox.portal.data.local

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.float
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
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
}
