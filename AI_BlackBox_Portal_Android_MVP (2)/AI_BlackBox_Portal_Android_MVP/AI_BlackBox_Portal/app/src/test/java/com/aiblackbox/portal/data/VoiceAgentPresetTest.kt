package com.aiblackbox.portal.data

import com.aiblackbox.portal.data.voice.VoiceAgentPresets
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceAgentPresetTest {

    @Test
    fun `parses agents array with unknown fields ignored`() {
        val body = """{"agents":[
            {"id":"va-1","name":"Pizza","provider":"grok-live","voice":"Rex","extra":123},
            {"id":"va-2","name":"Calm","provider":"realtime"}
        ]}"""
        val presets = VoiceAgentPresets.parse(body)
        assertEquals(2, presets.size)
        assertEquals("Pizza", presets[0].name)
        assertEquals("grok-live", presets[0].provider)
    }

    @Test
    fun `empty registry parses to empty list`() {
        assertTrue(VoiceAgentPresets.parse("""{"agents":[]}""").isEmpty())
    }

    @Test
    fun `malformed body degrades to empty list not crash`() {
        assertTrue(VoiceAgentPresets.parse("{oops").isEmpty())
        assertTrue(VoiceAgentPresets.parse("").isEmpty())
        assertTrue(VoiceAgentPresets.parse("""{"agents":"nope"}""").isEmpty())
    }
}
