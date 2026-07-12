package com.aiblackbox.portal.voice

import com.aiblackbox.portal.data.voice.VoiceSessionConfig
import com.aiblackbox.portal.util.Constants
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** P6 — affective dialog + proactive audio gate parity with the backend
 *  GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS frozenset (2.5-native-audio family only). */
class GeminiAffectiveTest {

    @Test
    fun affectiveCapableSetMatchesBackendAllowlist() {
        assertEquals(
            setOf(
                "gemini-2.5-flash-native-audio-latest",
                "gemini-2.5-flash-native-audio-preview-12-2025",
            ),
            Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS,
        )
    }

    @Test
    fun threeOneIsNotAffectiveCapable() {
        assertFalse(
            "gemini-3.1-flash-live-preview" in Constants.GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
        )
    }

    @Test
    fun sessionConfigDefaultsOmitAffectiveFlags() {
        val cfg = VoiceSessionConfig()
        assertNull(cfg.affective)
        assertNull(cfg.proactive)
        assertTrue(VoiceSessionConfig(affective = true, proactive = true).affective == true)
    }
}
