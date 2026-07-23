package com.aiblackbox.portal.ui.voicelab

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * resolveVoiceLabGate — the Voice Lab screen's top-level render decision after
 * the ElevenLabs (/elevenlabs/status) and on-box (/local-models/status) probes.
 * Contract: content as soon as EITHER provider is positive; NOT_CONFIGURED only
 * after BOTH probes answered negative (keyless + stack-off box → the existing
 * clean not-configured state, never a broken/empty UI); LOADING otherwise.
 */
class VoiceLabGateTest {

    @Test
    fun `nothing loaded yet is loading`() {
        assertEquals(
            VoiceLabGate.LOADING,
            resolveVoiceLabGate(elevenLoaded = false, qwenLoaded = false,
                elevenConfigured = false, qwenAvailable = false),
        )
    }

    @Test
    fun `elevenlabs positive renders content without waiting for qwen probe`() {
        assertEquals(
            VoiceLabGate.CONTENT,
            resolveVoiceLabGate(elevenLoaded = true, qwenLoaded = false,
                elevenConfigured = true, qwenAvailable = false),
        )
    }

    @Test
    fun `qwen positive renders content on a keyless box`() {
        assertEquals(
            VoiceLabGate.CONTENT,
            resolveVoiceLabGate(elevenLoaded = true, qwenLoaded = true,
                elevenConfigured = false, qwenAvailable = true),
        )
    }

    @Test
    fun `qwen positive renders content even before the elevenlabs probe lands`() {
        assertEquals(
            VoiceLabGate.CONTENT,
            resolveVoiceLabGate(elevenLoaded = false, qwenLoaded = true,
                elevenConfigured = false, qwenAvailable = true),
        )
    }

    @Test
    fun `both providers available is content`() {
        assertEquals(
            VoiceLabGate.CONTENT,
            resolveVoiceLabGate(elevenLoaded = true, qwenLoaded = true,
                elevenConfigured = true, qwenAvailable = true),
        )
    }

    @Test
    fun `keyless and stack-off box shows the not-configured state`() {
        assertEquals(
            VoiceLabGate.NOT_CONFIGURED,
            resolveVoiceLabGate(elevenLoaded = true, qwenLoaded = true,
                elevenConfigured = false, qwenAvailable = false),
        )
    }

    @Test
    fun `one negative probe keeps loading until the other answers`() {
        assertEquals(
            VoiceLabGate.LOADING,
            resolveVoiceLabGate(elevenLoaded = true, qwenLoaded = false,
                elevenConfigured = false, qwenAvailable = false),
        )
        assertEquals(
            VoiceLabGate.LOADING,
            resolveVoiceLabGate(elevenLoaded = false, qwenLoaded = true,
                elevenConfigured = false, qwenAvailable = false),
        )
    }
}
