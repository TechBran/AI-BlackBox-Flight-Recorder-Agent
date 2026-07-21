package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

/**
 * generateWithVoice must pass the PARSED provider through to /tts/batch instead
 * of hardcoding "openai" — otherwise on-box voices (local:/qwen:) are mislabeled
 * and 400. openai:/bare-legacy voices keep provider "openai" (regression guard).
 */
class TtsProviderRoutingTest {

    private lateinit var server: MockWebServer
    private lateinit var repo: TtsRepository

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        // BlackBoxApi expects a baseUrl WITHOUT a trailing slash (matches
        // TtsVoiceParseTest): "$baseUrl$path" keeps the leading slash.
        val baseUrl = server.url("").toString().trimEnd('/')
        repo = TtsRepository(BlackBoxApi(baseUrl))
    }

    @After fun tearDown() {
        server.close()
    }

    private fun enqueueOk() {
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"status":"ok","audio_url":"http://x/a.mp3"}""")
                .build()
        )
    }

    /** Drive generateWithVoice once and return (target, provider) actually sent. */
    private suspend fun routeVoice(voiceValue: String): Pair<String, String> {
        enqueueOk()
        repo.generateWithVoice(text = "hello", voiceValue = voiceValue)
        val rec = server.takeRequest()
        val provider = Json.parseToJsonElement(rec.body!!.utf8())
            .jsonObject["provider"]!!.jsonPrimitive.content
        return rec.target!! to provider
    }

    @Test fun `qwen voice routes to tts batch with provider qwen not openai`() = runTest {
        val (target, provider) = routeVoice("qwen:Vivian")
        assertEquals("/tts/batch", target)
        assertEquals("qwen", provider)
    }

    @Test fun `local voice routes with provider local`() = runTest {
        val (target, provider) = routeVoice("local:af_heart")
        assertEquals("/tts/batch", target)
        assertEquals("local", provider)
    }

    @Test fun `openai voice still routes with provider openai`() = runTest {
        val (_, provider) = routeVoice("openai:nova")
        assertEquals("openai", provider)
    }

    @Test fun `bare legacy voice still routes with provider openai`() = runTest {
        val (_, provider) = routeVoice("onyx")
        assertEquals("openai", provider)
    }
}
