package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceCatalogTest {
    @Test fun `parses object-shaped models plus voices default and presets`() {
        val raw = """
        {"status":"ok",
         "models":[{"id":"gpt-realtime-2.1","label":"GPT Realtime 2.1"},{"id":"gpt-realtime-2.1-mini"}],
         "model_default":"gpt-realtime-2.1",
         "voices":["ash","marin","cedar"],
         "presets":[{"id":"sales-agent","name":"Sales Agent"}]}
        """.trimIndent()
        val cat = VoiceCatalog.parse(raw)!!
        assertEquals(
            listOf(
                VoiceCatalogOption("gpt-realtime-2.1", "GPT Realtime 2.1"),
                VoiceCatalogOption("gpt-realtime-2.1-mini", "gpt-realtime-2.1-mini"),
            ),
            cat.models
        )
        assertEquals("gpt-realtime-2.1", cat.modelDefault)
        assertEquals(listOf("ash", "marin", "cedar"), cat.voices)
        assertEquals(listOf(VoiceCatalogOption("sales-agent", "Sales Agent")), cat.presets)
    }

    @Test fun `parses string-shaped models`() {
        val cat = VoiceCatalog.parse("""{"models":["grok-voice-latest","grok-voice-think-fast-1.0"]}""")!!
        assertEquals(2, cat.models.size)
        assertEquals("grok-voice-latest", cat.models[0].id)
        assertEquals("grok-voice-latest", cat.models[0].label)
    }

    @Test fun `missing fields yield empty catalog not null`() {
        val cat = VoiceCatalog.parse("""{"status":"ok","api_key_configured":true}""")!!
        assertTrue(cat.models.isEmpty())
        assertTrue(cat.voices.isEmpty())
        assertTrue(cat.presets.isEmpty())
        assertNull(cat.modelDefault)
    }

    @Test fun `garbage returns null`() {
        assertNull(VoiceCatalog.parse("not json"))
        assertNull(VoiceCatalog.parse("[1,2,3]"))
    }

    @Test fun `fallback helpers prefer non-empty catalog`() {
        val cat = VoiceCatalog(models = listOf(VoiceCatalogOption("m1", "M1")), voices = listOf("v1"))
        assertEquals(listOf("v1"), cat.voicesOrFallback(listOf("fb")))
        assertEquals(listOf("m1" to "M1"), cat.modelsOrFallback(listOf("fb" to "FB")))
        val absent: VoiceCatalog? = null
        assertEquals(listOf("fb"), absent.voicesOrFallback(listOf("fb")))
        assertEquals(listOf("fb" to "FB"), VoiceCatalog().modelsOrFallback(listOf("fb" to "FB")))
    }
}
