package com.aiblackbox.portal.ui.voicelab

import kotlinx.serialization.SerializationException
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * friendlyVoiceLabError — raw parser exceptions must never reach the user.
 * A fetch that races a server restart gets a transient non-JSON body; kotlinx
 * then throws SerializationException("Unexpected JSON token at offset ...")
 * which previously leaked verbatim into the Voice Lab UI. Pure function
 * (logging stays at the VM call sites), offline test like VoiceLabGateTest.
 */
class VoiceLabErrorMappingTest {

    @Test
    fun `kotlinx serialization exceptions map to the friendly restart hint`() {
        val e = SerializationException(
            "Unexpected JSON token at offset 0: Expected start of the object '{', but had 'I' instead"
        )
        assertEquals(VOICE_LAB_UNEXPECTED_REPLY, friendlyVoiceLabError(e, "Clone failed"))
    }

    @Test
    fun `serialization exception maps even without the token phrase in its message`() {
        assertEquals(
            VOICE_LAB_UNEXPECTED_REPLY,
            friendlyVoiceLabError(SerializationException("boom"), "Design failed"),
        )
    }

    @Test
    fun `unexpected json token message maps when carried by a plain exception`() {
        val e = RuntimeException("unexpected JSON token at offset 42: garbage body")
        assertEquals(VOICE_LAB_UNEXPECTED_REPLY, friendlyVoiceLabError(e, "Clone failed"))
    }

    @Test
    fun `ordinary errors keep their message verbatim`() {
        assertEquals("HTTP 503", friendlyVoiceLabError(RuntimeException("HTTP 503"), "Clone failed"))
        assertEquals(
            "Consent is required.",
            friendlyVoiceLabError(IllegalStateException("Consent is required."), "Clone failed"),
        )
    }

    @Test
    fun `null or blank messages fall back to the caller default`() {
        assertEquals("Clone failed", friendlyVoiceLabError(RuntimeException(), "Clone failed"))
        assertEquals("Save failed", friendlyVoiceLabError(RuntimeException("   "), "Save failed"))
    }
}
