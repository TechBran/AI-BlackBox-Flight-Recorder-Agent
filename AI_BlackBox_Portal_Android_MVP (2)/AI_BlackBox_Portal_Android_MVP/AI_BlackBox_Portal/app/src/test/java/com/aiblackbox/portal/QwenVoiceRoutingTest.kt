package com.aiblackbox.portal

import com.aiblackbox.portal.data.repository.TtsRepository
import com.aiblackbox.portal.data.repository.TTS_VOICE_GROUPS
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * M7 Task 7.8 — Qwen voice routing key + offline-fallback convention.
 *   1. parseVoice("qwen:Vivian") yields provider=qwen / voice=Vivian, which is
 *      the routing key buildTtsBatchBody consumes (POST /tts/batch provider=qwen).
 *   2. Underscored preset tokens survive the split intact (voice=Uncle_Fu).
 *   3. Qwen is NOT in the compiled-in offline fallback (dynamic-only, like
 *      ElevenLabs/local) — it must come only from the live /tts/catalog.
 */
class QwenVoiceRoutingTest {

    @Test
    fun parseVoice_qwenPreset_splitsProviderAndVoice() {
        val cfg = TtsRepository.parseVoice("qwen:Vivian")
        assertEquals("qwen", cfg.provider)
        assertEquals("Vivian", cfg.voice)
    }

    @Test
    fun parseVoice_qwenUnderscoreToken_preserved() {
        val cfg = TtsRepository.parseVoice("qwen:Uncle_Fu")
        assertEquals("qwen", cfg.provider)
        assertEquals("Uncle_Fu", cfg.voice)
    }

    @Test
    fun offlineFallback_hasNoQwenGroup() {
        val labels = TTS_VOICE_GROUPS.map { it.label }
        assertFalse(labels.any { it.contains("Qwen", ignoreCase = true) })
    }
}
