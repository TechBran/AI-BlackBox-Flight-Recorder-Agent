package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * On-Box (Qwen3-TTS) Voice Lab wire contracts (V2-android-voicelab-qwen):
 *   GET  /local-models/status   → {healthy, routing:{tts:{enabled,...}}, ...}
 *   GET  /qwen/voices           → {voices:[{slug, name, variant}]}
 *   POST /qwen/voices/design    → {previews:[{generated_voice_id, audio_b64, sample_rate}]}
 *   POST /qwen/voices/clone|design/save → {voice_id}
 * Same offline-fake-JSON style as XaiVoiceParsingTest.
 */
class QwenVoiceLabParsingTest {

    // ── /local-models/status gate ────────────────────────────────────────────

    @Test
    fun `healthy stack with tts enabled is available`() {
        val raw = """{"healthy": true, "installed": true,
            "routing": {"tts": {"enabled": true, "healthy": true, "decision": "on-box"}}}"""
        val st = parseQwenStatusResponse(raw)
        assertTrue(st.healthy)
        assertTrue(st.ttsEnabled)
        assertTrue(st.available)
    }

    @Test
    fun `unhealthy stack is unavailable even with tts enabled`() {
        val raw = """{"healthy": false,
            "routing": {"tts": {"enabled": true, "healthy": false, "decision": "unhealthy"}}}"""
        assertFalse(parseQwenStatusResponse(raw).available)
    }

    @Test
    fun `healthy stack with tts capability off is unavailable`() {
        val raw = """{"healthy": true,
            "routing": {"tts": {"enabled": false, "healthy": true, "decision": "off"}}}"""
        val st = parseQwenStatusResponse(raw)
        assertTrue(st.healthy)
        assertFalse(st.ttsEnabled)
        assertFalse(st.available)
    }

    @Test
    fun `missing routing block fails open on healthy alone`() {
        assertTrue(parseQwenStatusResponse("""{"healthy": true}""").available)
        assertFalse(parseQwenStatusResponse("""{"healthy": false}""").available)
    }

    @Test
    fun `stack-off shape without healthy field is unavailable`() {
        // A minimal / degraded body must render the clean cloud-only state.
        assertFalse(parseQwenStatusResponse("""{"installed": false}""").available)
    }

    // ── /qwen/voices list ────────────────────────────────────────────────────

    @Test
    fun `parses voices with slug name variant`() {
        val raw = """{"voices": [
            {"slug": "my-narrator", "name": "My Narrator", "variant": "clone", "operator": "Brandon"},
            {"slug": "warm-brit", "name": "Warm Brit", "variant": "design"}
        ]}"""
        val voices = parseQwenVoicesResponse(raw)
        assertEquals(2, voices.size)
        assertEquals(QwenVoice("my-narrator", "My Narrator", "clone"), voices[0])
        assertEquals(QwenVoice("warm-brit", "Warm Brit", "design"), voices[1])
    }

    @Test
    fun `slugless rows skipped and missing name falls back to slug`() {
        val raw = """{"voices": [
            {"name": "no-slug-row"},
            {"slug": "bare"}
        ]}"""
        val voices = parseQwenVoicesResponse(raw)
        assertEquals(1, voices.size)
        assertEquals(QwenVoice("bare", "bare", ""), voices[0])
    }

    @Test
    fun `empty and missing voices arrays yield empty list`() {
        assertTrue(parseQwenVoicesResponse("""{"voices": []}""").isEmpty())
        assertTrue(parseQwenVoicesResponse("""{}""").isEmpty())
    }

    // ── /qwen/voices/design previews ─────────────────────────────────────────

    @Test
    fun `parses design previews with audio_b64 and sample_rate`() {
        val raw = """{"previews": [
            {"generated_voice_id": "gen-1", "audio_b64": "UklGRg==", "sample_rate": 24000},
            {"generated_voice_id": "gen-2", "audio_b64": "QUJD", "sample_rate": 24000}
        ]}"""
        val previews = parseQwenDesignResponse(raw)
        assertEquals(2, previews.size)
        assertEquals("gen-1", previews[0].generatedVoiceId)
        assertEquals("UklGRg==", previews[0].audioB64)
        assertEquals(24000, previews[0].sampleRate)
        assertEquals("", previews[0].audioUrl)
    }

    @Test
    fun `preview without generated_voice_id is skipped and audio_url fallback kept`() {
        val raw = """{"previews": [
            {"audio_b64": "orphan"},
            {"generated_voice_id": "gen-3", "audio_url": "/ui/uploads/p.wav"}
        ]}"""
        val previews = parseQwenDesignResponse(raw)
        assertEquals(1, previews.size)
        assertEquals("gen-3", previews[0].generatedVoiceId)
        assertEquals("/ui/uploads/p.wav", previews[0].audioUrl)
        assertEquals("", previews[0].audioB64)
    }

    @Test
    fun `missing previews array yields empty list`() {
        assertTrue(parseQwenDesignResponse("""{"text": "hello"}""").isEmpty())
    }

    // ── clone / design-save voice_id ─────────────────────────────────────────

    @Test
    fun `parses voice_id from mutation responses`() {
        assertEquals("my-narrator", parseQwenVoiceIdResponse("""{"voice_id": "my-narrator", "ok": true}"""))
        assertEquals("", parseQwenVoiceIdResponse("""{"ok": true}"""))
    }
}
