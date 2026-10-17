package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Android takes its TTS default from GET /tts/catalog (audit rank 39,
 * P02-voice-web-08/-09 "Check Android TtsRepository for the same pinned value").
 *
 * Before: parseVoice pinned "tts-1-hd" on every OpenAI request (overriding the
 * box's [audio] model) and the store fell back to "openai:onyx" for an operator
 * with no saved voice, on a box with no OpenAI key too. Now the catalog's
 * `default` is the default, OpenAI/ElevenLabs send no model (the server picks),
 * and a blank voice parses to "nothing chosen" so the server speaks its default.
 */
class TtsCatalogDefaultTest {

    // -------------------------------------------------------------------------
    // parseVoice — no pinned OpenAI model; blank = the box default.
    // -------------------------------------------------------------------------

    @Test fun `openai voice leaves the model to the server`() {
        val c = TtsRepository.parseVoice("openai:nova")
        assertEquals("openai", c.provider)
        assertEquals("nova", c.voice)
        assertEquals("", c.model)
    }

    @Test fun `bare legacy voice is openai with the server's model`() {
        val c = TtsRepository.parseVoice("onyx")
        assertEquals(VoiceConfig("openai", "onyx", ""), c)
    }

    @Test fun `blank voice names nothing so the server speaks its default`() {
        assertEquals(VoiceConfig("", "", ""), TtsRepository.parseVoice(""))
        assertEquals(VoiceConfig("", "", ""), TtsRepository.parseVoice("  "))
    }

    @Test fun `a catalog group's model wins over the offline Gemini id`() {
        val groups = listOf(VoiceGroup("Gemini Flash TTS",
            listOf(VoiceOption("gemini-flash:Kore", "Kore", "")),
            id = "gemini-flash", model = "gemini-next-flash-tts"))
        assertEquals("gemini-next-flash-tts",
            TtsRepository.parseVoice("gemini-flash:Kore", groups).model)
    }

    // -------------------------------------------------------------------------
    // parseCatalog — additive id / available / model / default fields.
    // -------------------------------------------------------------------------

    private val catalogJson = """
        {"groups":[
          {"id":"openai","label":"OpenAI TTS HD","available":false,
           "unavailable_reason":"Needs OPENAI_API_KEY","model":"tts-1-hd","voices":[
             {"id":"openai:alloy","name":"Alloy","description":"Neutral"}]},
          {"id":"gemini-pro","label":"Gemini Pro TTS","available":true,
           "model":"gemini-2.5-pro-tts","voices":[
             {"id":"gemini-pro:Charon","name":"Charon","description":"Calm"}]}
        ],
        "default":{"id":"gemini-pro:Charon","provider":"gemini-pro","voice":"Charon",
                   "model":"gemini-2.5-pro-tts","available":true,"source":"fallback"}}
    """.trimIndent()

    @Test fun `parseCatalog reads the default and per-group availability`() {
        val cat = TtsRepository.parseCatalog(catalogJson)
        assertNotNull(cat)
        assertEquals("gemini-pro:Charon", cat!!.defaultVoiceId)
        val openai = cat.groups.first { it.id == "openai" }
        assertFalse(openai.available)
        assertEquals("tts-1-hd", openai.model)
        assertTrue(cat.groups.first { it.id == "gemini-pro" }.available)
    }

    @Test fun `a default that names no listed voice is ignored`() {
        val raw = catalogJson.replace("\"id\":\"gemini-pro:Charon\",\"provider\"",
                                      "\"id\":\"gone:voice\",\"provider\"")
        assertNull(TtsRepository.parseCatalog(raw)!!.defaultVoiceId)
    }

    @Test fun `an older server without the new fields still parses`() {
        val cat = TtsRepository.parseCatalog(
            """{"groups":[{"id":"openai","label":"OpenAI TTS HD","voices":[
                 {"id":"openai:alloy","name":"Alloy","description":"Neutral"}]}]}""")
        assertNotNull(cat)
        assertNull(cat!!.defaultVoiceId)
        assertTrue("missing available means usable", cat.groups.single().available)
        assertEquals("", cat.groups.single().model)
    }

    @Test fun `an empty catalog parses to null so callers keep the offline fallback`() {
        assertNull(TtsRepository.parseCatalog("""{"groups":[]}"""))
        assertNull(TtsRepository.parseCatalog("""{"nope":1}"""))
    }

    // -------------------------------------------------------------------------
    // VoicePicker — effective voice + availability label.
    // -------------------------------------------------------------------------

    @Test fun `a saved voice wins, else the box default, else nothing`() {
        assertEquals("openai:nova", VoicePicker.effectiveVoice("openai:nova", "gemini-pro:Charon"))
        assertEquals("gemini-pro:Charon", VoicePicker.effectiveVoice("", "gemini-pro:Charon"))
        assertEquals("", VoicePicker.effectiveVoice("", null))
    }

    @Test fun `a group the box has no key for is labelled not configured`() {
        val off = VoiceGroup("OpenAI TTS HD", emptyList(), id = "openai", available = false)
        val on = VoiceGroup("Gemini Pro TTS", emptyList(), id = "gemini-pro")
        assertEquals("OpenAI TTS HD (not configured)", VoicePicker.providerLabel(off))
        assertEquals("Gemini Pro TTS", VoicePicker.providerLabel(on))
    }

    @Test fun `the offline fallback carries group ids`() {
        assertEquals(listOf("openai", "gemini-flash", "gemini-pro"), TTS_VOICE_GROUPS.map { it.id })
    }
}
