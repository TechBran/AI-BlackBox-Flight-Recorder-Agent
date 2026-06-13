package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class TtsVoiceParseTest {

    // -------------------------------------------------------------------------
    // parseVoice("provider:voice") — pure string → VoiceConfig mapping.
    // -------------------------------------------------------------------------

    @Test fun `openai voice maps to openai provider`() {
        val c = TtsRepository.parseVoice("openai:nova")
        assertEquals("openai", c.provider); assertEquals("nova", c.voice)
    }
    @Test fun `gemini-flash maps to flash tts model`() {
        val c = TtsRepository.parseVoice("gemini-flash:Zephyr")
        assertEquals("gemini-flash", c.provider)
        assertEquals("Zephyr", c.voice)
        assertEquals("gemini-2.5-flash-tts", c.model)
    }
    @Test fun `gemini-pro maps to pro tts model`() {
        val c = TtsRepository.parseVoice("gemini-pro:Charon")
        assertEquals("gemini-pro", c.provider)
        assertEquals("gemini-2.5-pro-tts", c.model)
    }
    @Test fun `bare legacy voice falls back to openai`() {
        val c = TtsRepository.parseVoice("onyx")
        assertEquals("openai", c.provider); assertEquals("onyx", c.voice)
    }

    // -------------------------------------------------------------------------
    // fetchCatalog() — GET /tts/catalog parse tolerance (ElevenLabs Task 20).
    //
    // Uses MockWebServer (mockwebserver3) + a real BlackBoxApi so the test
    // exercises the ACTUAL OkHttp + parse path in fetchCatalog(), matching
    // CliAgentSessionRepositoryTest's convention. The catalog parse walks the
    // JSON tree by hand (parseToJsonElement → groups[].label/voices[].{id,name,
    // description}); the goal here is to prove that the NEW backend shape — a
    // 4th "elevenlabs" group carrying a group-level `"dynamic": true` flag and
    // per-voice `preview_url`/`category` extras — parses fine and the extra
    // fields are harmlessly ignored (TtsRepository's Json{ignoreUnknownKeys=
    // true} plus the by-key tree walk that only reads the fields it wants).
    // -------------------------------------------------------------------------

    private lateinit var server: MockWebServer
    private lateinit var repo: TtsRepository

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        // BlackBoxApi expects a baseUrl WITHOUT a trailing slash so path
        // concatenation ("$baseUrl$path") keeps the leading slash.
        val baseUrl = server.url("").toString().trimEnd('/')
        repo = TtsRepository(BlackBoxApi(baseUrl))
    }

    @After fun tearDown() {
        server.close()
    }

    /** The Task-20 backend fixture: 4 groups, elevenlabs is dynamic + has extra
     *  per-voice fields, and the first My-Voices entry is ⭐-prefixed. */
    private val fourGroupCatalog = """
        {"groups":[
          {"id":"openai","label":"OpenAI TTS HD","voices":[
             {"id":"openai:alloy","name":"Alloy","description":"Neutral"}
          ]},
          {"id":"gemini-flash","label":"Gemini Flash TTS","voices":[
             {"id":"gemini-flash:Zephyr","name":"Zephyr","description":"Bright, cheerful"}
          ]},
          {"id":"gemini-pro","label":"Gemini Pro TTS","voices":[
             {"id":"gemini-pro:Charon","name":"Charon","description":"Calm, informative"}
          ]},
          {"id":"elevenlabs","label":"ElevenLabs","dynamic":true,"voices":[
             {"id":"elevenlabs:abc","name":"⭐ My Clone","description":"cloned","preview_url":"https://x/p.mp3","category":"cloned"},
             {"id":"elevenlabs:def","name":"David","description":"british, male","preview_url":"https://x/d.mp3","category":"premade"}
          ]}
        ]}
    """.trimIndent()

    private fun enqueueCatalog(body: String) {
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body(body)
                .build()
        )
    }

    @Test
    fun `fetchCatalog parses all four groups including the dynamic ElevenLabs group`() = runTest {
        enqueueCatalog(fourGroupCatalog)

        val groups = repo.fetchCatalog()

        // All four backend groups round-trip (NOT the 3-group offline fallback).
        assertEquals(4, groups.size)
        assertEquals(
            listOf("OpenAI TTS HD", "Gemini Flash TTS", "Gemini Pro TTS", "ElevenLabs"),
            groups.map { it.label },
        )

        // The new ElevenLabs group is present and labelled correctly.
        val el = groups.find { it.label == "ElevenLabs" }
        assertNotNull("ElevenLabs group must parse and be present", el)
        assertEquals(2, el!!.voices.size)

        // Hit the real GET /tts/catalog endpoint.
        assertEquals("/tts/catalog", server.takeRequest().target)
    }

    @Test
    fun `fetchCatalog parses ElevenLabs voices despite preview_url and category extras`() = runTest {
        enqueueCatalog(fourGroupCatalog)

        val el = repo.fetchCatalog().first { it.label == "ElevenLabs" }

        // id / name / description map correctly; the unknown preview_url +
        // category fields neither crash the parse nor leak into VoiceOption
        // (VoiceOption has no slots for them — they're simply dropped).
        val clone = el.voices[0]
        assertEquals("elevenlabs:abc", clone.id)
        assertEquals("⭐ My Clone", clone.name)   // ⭐-prefixed My-Voices name round-trips intact
        assertEquals("cloned", clone.description)

        val david = el.voices[1]
        assertEquals("elevenlabs:def", david.id)
        assertEquals("David", david.name)
        assertEquals("british, male", david.description)
    }

    @Test
    fun `fetchCatalog ignores group-level dynamic flag without crashing`() = runTest {
        // A bare elevenlabs-only payload that carries ONLY the unknown extras
        // (dynamic at the group level, preview_url/category on the voice).
        // If any of those unknown keys were fatal, this parse would throw and
        // fetchCatalog() would silently swap in the 3-group offline fallback —
        // so a single clean ElevenLabs group proves they're tolerated.
        enqueueCatalog(
            """
            {"groups":[
              {"id":"elevenlabs","label":"ElevenLabs","dynamic":true,"voices":[
                 {"id":"elevenlabs:abc","name":"⭐ My Clone","description":"cloned","preview_url":"https://x/p.mp3","category":"cloned"}
              ]}
            ]}
            """.trimIndent()
        )

        val groups = repo.fetchCatalog()
        assertEquals("dynamic + extras must not trip the fallback", 1, groups.size)
        assertEquals("ElevenLabs", groups[0].label)
        assertEquals("⭐ My Clone", groups[0].voices.single().name)
    }

    @Test
    fun `fetchCatalog tolerates voices that omit the optional description`() = runTest {
        // description is read with a null-safe default ("") in fetchCatalog, so
        // an ElevenLabs voice without one must still parse (not throw → not
        // fall back). id/name remain required.
        enqueueCatalog(
            """
            {"groups":[
              {"id":"elevenlabs","label":"ElevenLabs","dynamic":true,"voices":[
                 {"id":"elevenlabs:abc","name":"⭐ My Clone","preview_url":"https://x/p.mp3","category":"cloned"}
              ]}
            ]}
            """.trimIndent()
        )

        val el = repo.fetchCatalog().first { it.label == "ElevenLabs" }
        val v = el.voices.single()
        assertEquals("elevenlabs:abc", v.id)
        assertEquals("⭐ My Clone", v.name)
        assertTrue("missing description defaults to empty string", v.description.isEmpty())
    }
}
