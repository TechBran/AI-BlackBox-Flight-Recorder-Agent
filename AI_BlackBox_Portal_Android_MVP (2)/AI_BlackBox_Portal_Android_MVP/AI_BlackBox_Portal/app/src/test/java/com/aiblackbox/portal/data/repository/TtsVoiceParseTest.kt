package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Test

class TtsVoiceParseTest {
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
}
