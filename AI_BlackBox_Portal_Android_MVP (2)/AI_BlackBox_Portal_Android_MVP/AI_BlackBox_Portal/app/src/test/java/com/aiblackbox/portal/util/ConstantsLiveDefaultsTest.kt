package com.aiblackbox.portal.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ConstantsLiveDefaultsTest {
    @Test fun `gemini live default is 3_1 preview`() {
        assertEquals(
            "gemini-3.1-flash-live-preview",
            Constants.LIVE_MODEL_DEFAULTS["gemini-live"]
        )
    }

    @Test fun `gemini live default is thinking capable`() {
        val default = Constants.LIVE_MODEL_DEFAULTS["gemini-live"]
        assertTrue(default in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS)
    }

    @Test fun `gemini live default is present in model config list`() {
        val ids = Constants.MODEL_CONFIG["gemini-live"].orEmpty().map { it.first }
        assertTrue("gemini-3.1-flash-live-preview" in ids)
    }

    @Test fun `realtime default is gpt-realtime-2_1 and 2_1 family is listed`() {
        assertEquals("gpt-realtime-2.1", Constants.LIVE_MODEL_DEFAULTS["realtime"])
        val ids = Constants.MODEL_CONFIG["realtime"].orEmpty().map { it.first }
        assertTrue("gpt-realtime-2.1" in ids)
        assertTrue("gpt-realtime-2.1-mini" in ids)
    }

    @Test fun `grok live fallback models include latest alias and think-fast pin`() {
        val ids = Constants.MODEL_CONFIG["grok-live"].orEmpty().map { it.first }
        assertTrue("" in ids) // Auto — backend resolves grok-voice-latest
        assertTrue("grok-voice-latest" in ids)
        assertTrue("grok-voice-think-fast-1.0" in ids)
        assertEquals("", Constants.LIVE_MODEL_DEFAULTS["grok-live"])
    }

    @Test fun `grok live fallback voices exist and default is a member`() {
        assertTrue(Constants.VOICES_GROK_LIVE.isNotEmpty())
        assertTrue(Constants.DEFAULT_GROK_LIVE_VOICE in Constants.VOICES_GROK_LIVE)
    }

    @Test fun `grok reasoning efforts are high and none`() {
        assertEquals(listOf("high", "none"), Constants.GROK_LIVE_REASONING_EFFORTS)
    }
}
