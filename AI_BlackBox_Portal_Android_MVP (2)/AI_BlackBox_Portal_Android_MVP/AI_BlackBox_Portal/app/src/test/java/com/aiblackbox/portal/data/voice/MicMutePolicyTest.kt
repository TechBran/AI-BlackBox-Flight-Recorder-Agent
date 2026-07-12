package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MicMutePolicyTest {
    @Test fun `grok holds mic while AI speaks and during post-speech window`() {
        assertTrue(shouldHoldMic(VoiceBackend.GROK_LIVE, isAiSpeaking = true, msSinceAiStopped = 999_999))
        assertTrue(shouldHoldMic(VoiceBackend.GROK_LIVE, isAiSpeaking = false, msSinceAiStopped = 500))
        assertFalse(shouldHoldMic(VoiceBackend.GROK_LIVE, isAiSpeaking = false, msSinceAiStopped = 1300))
    }

    @Test fun `openai and gemini keep the mic open even while AI speaks`() {
        assertFalse(shouldHoldMic(VoiceBackend.GPT_REALTIME, isAiSpeaking = true, msSinceAiStopped = 0))
        assertFalse(shouldHoldMic(VoiceBackend.GEMINI_LIVE, isAiSpeaking = true, msSinceAiStopped = 0))
        assertFalse(shouldHoldMic(VoiceBackend.GEMINI_LIVE, isAiSpeaking = false, msSinceAiStopped = 100))
    }
}
