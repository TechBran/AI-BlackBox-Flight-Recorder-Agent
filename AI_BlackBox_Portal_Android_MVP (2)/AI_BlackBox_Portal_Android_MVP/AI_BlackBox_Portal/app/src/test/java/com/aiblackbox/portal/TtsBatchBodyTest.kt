package com.aiblackbox.portal

import com.aiblackbox.portal.data.repository.TtsRepository
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * Audit O07-tts-voice-06: the chat TTS paths (auto-TTS, speaker button, onSpeak)
 * sent parseVoice()'s pinned Gemini model ids on every POST /tts/batch, a third
 * copy of a provider fact the server already owns. /tts/batch now synthesises
 * each group with its own model, so the body names no model at all.
 */
class TtsBatchBodyTest {

    private fun body(voiceValue: String) = TtsRepository.parseVoice(voiceValue).let { cfg ->
        Json.parseToJsonElement(
            buildTtsBatchBody("Hello there.", cfg.voice, "mp3", cfg.provider, "op")
        ).jsonObject
    }

    @Test
    fun gemini_voice_sends_no_model() {
        for (voice in listOf("gemini-flash:Kore", "gemini-pro:Charon")) {
            val b = body(voice)
            assertFalse("$voice: no client model id", b.containsKey("model"))
            assertEquals(voice.substringBefore(":"), b["provider"]!!.jsonPrimitive.content)
            assertEquals(voice.substringAfter(":"), b["voice"]!!.jsonPrimitive.content)
        }
    }

    @Test
    fun nothing_chosen_sends_neither_voice_nor_provider_so_the_box_default_speaks() {
        val b = body("")
        assertEquals("", b["voice"]!!.jsonPrimitive.content)
        assertEquals("", b["provider"]!!.jsonPrimitive.content)
        assertFalse(b.containsKey("model"))
        assertEquals("mp3", b["format"]!!.jsonPrimitive.content)
        assertEquals("op", b["operator"]!!.jsonPrimitive.content)
    }
}
