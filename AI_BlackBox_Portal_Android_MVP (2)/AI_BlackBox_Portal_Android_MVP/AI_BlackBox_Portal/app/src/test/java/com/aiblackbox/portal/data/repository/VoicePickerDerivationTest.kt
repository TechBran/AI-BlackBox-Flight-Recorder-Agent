package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * D2 provider-first two-step voice picker — pure provider→voice derivation.
 *
 * VoicePicker resolves which provider group the picker shows for a persisted
 * `provider:voice` id and which voices populate the second dropdown. No Android,
 * no ViewModel — just the pure functions the SettingsSheet two-step picker uses.
 *
 * Derivation is exercised against a SYNTHETIC live catalog that appends the
 * dynamic on-box `qwen` group (as GET /tts/catalog does on a healthy stack),
 * NOT the compiled-in TTS_VOICE_GROUPS — which stays cloud-only by design
 * (qwen is dynamic-only; see QwenVoiceRoutingTest + the fail-open invariant).
 * The last block re-asserts that offline-fallback inertness.
 */
class VoicePickerDerivationTest {

    // A healthy-stack catalog: cloud groups first, on-box `qwen` appended — the
    // shape GET /tts/catalog returns when the on-box TTS stack is up.
    private val liveCatalog = TTS_VOICE_GROUPS + VoiceGroup(
        "Qwen3-TTS (On-Box)",
        listOf(
            VoiceOption("qwen:Vivian", "Vivian", "Warm, expressive"),
            VoiceOption("qwen:Serena", "Serena", "Calm, measured"),
            VoiceOption("qwen:Uncle_Fu", "Uncle Fu", "Deep, avuncular"),
        ),
    )

    // -------------------------------------------------------------------------
    // groupForVoice — exact-match then prefix-match.
    // -------------------------------------------------------------------------

    @Test fun `exact voice id resolves to its owning group`() {
        val g = VoicePicker.groupForVoice(liveCatalog, "openai:onyx")
        assertEquals("OpenAI TTS HD", g?.label)
    }

    @Test fun `gemini-pro voice resolves to the Gemini Pro group not Flash`() {
        val g = VoicePicker.groupForVoice(liveCatalog, "gemini-pro:Charon")
        assertEquals("Gemini Pro TTS", g?.label)
    }

    @Test fun `qwen preset resolves to the on-box group`() {
        val g = VoicePicker.groupForVoice(liveCatalog, "qwen:Vivian")
        assertEquals("Qwen3-TTS (On-Box)", g?.label)
    }

    @Test fun `unknown voice in a known provider still resolves by prefix`() {
        // A voice id NOT (yet) in the catalog but with a known provider prefix
        // still maps to that provider group (e.g. a freshly-cloned qwen slug).
        val g = VoicePicker.groupForVoice(liveCatalog, "qwen:MyClone")
        assertEquals("Qwen3-TTS (On-Box)", g?.label)
    }

    @Test fun `voice with an unknown provider prefix resolves to null`() {
        assertNull(VoicePicker.groupForVoice(liveCatalog, "acme:whoever"))
    }

    @Test fun `bare voice with no colon resolves to null`() {
        assertNull(VoicePicker.groupForVoice(liveCatalog, "onyx"))
    }

    // -------------------------------------------------------------------------
    // selectedGroup — owning group, else the first group; null only when empty.
    // -------------------------------------------------------------------------

    @Test fun `selectedGroup returns the owning group for a resolvable voice`() {
        assertEquals("Qwen3-TTS (On-Box)",
            VoicePicker.selectedGroup(liveCatalog, "qwen:Serena")?.label)
    }

    @Test fun `selectedGroup falls back to the first group for an unresolvable voice`() {
        // Legacy/unknown id → the two-step picker defaults to the first provider
        // (the persisted value still displays via the flat currentVoiceDisplay).
        assertEquals(liveCatalog.first().label,
            VoicePicker.selectedGroup(liveCatalog, "onyx")?.label)
    }

    @Test fun `selectedGroup is null for an empty catalog`() {
        assertNull(VoicePicker.selectedGroup(emptyList(), "openai:onyx"))
    }

    // -------------------------------------------------------------------------
    // voicesFor — second-dropdown contents scoped to the chosen provider only.
    // -------------------------------------------------------------------------

    @Test fun `voicesFor returns only the selected providers voices`() {
        val g = VoicePicker.selectedGroup(liveCatalog, "openai:onyx")
        val voices = VoicePicker.voicesFor(g)
        assertTrue("every second-dropdown voice belongs to the chosen provider",
            voices.all { it.id.substringBefore(':') == "openai" })
        assertTrue("openai:onyx is offered", voices.any { it.id == "openai:onyx" })
        assertTrue("no cross-provider leakage (no gemini voices)",
            voices.none { it.id.startsWith("gemini") })
    }

    @Test fun `voicesFor scopes the on-box provider to only qwen voices`() {
        val g = VoicePicker.selectedGroup(liveCatalog, "qwen:Vivian")
        val voices = VoicePicker.voicesFor(g)
        assertTrue(voices.isNotEmpty())
        assertTrue("only qwen voices in the on-box second dropdown",
            voices.all { it.id.startsWith("qwen:") })
    }

    @Test fun `voicesFor null group yields an empty list`() {
        assertTrue(VoicePicker.voicesFor(null).isEmpty())
    }

    // -------------------------------------------------------------------------
    // Fail-open / inert invariant — the COMPILED-IN offline fallback carries no
    // on-box surface (qwen dynamic-only, whisper STT-only). Mirrors the web D1
    // static fallback and QwenVoiceRoutingTest; on a stack-less box whose live
    // catalog is unreachable, nothing on-box is ever advertised.
    // -------------------------------------------------------------------------

    @Test fun `offline fallback is cloud-only in server order`() {
        assertEquals(
            listOf("OpenAI TTS HD", "Gemini Flash TTS", "Gemini Pro TTS"),
            TTS_VOICE_GROUPS.map { it.label },
        )
    }

    @Test fun `offline fallback advertises no on-box qwen or whisper voices`() {
        val v = TTS_VOICE_GROUPS.flatMap { it.voices }
        assertTrue("no qwen voices baked into the fallback",
            v.none { it.id.startsWith("qwen") })
        assertTrue("no whisper voices baked into the fallback (STT-only)",
            v.none { it.id.startsWith("whisper") })
        assertTrue("no on-box group label baked in",
            TTS_VOICE_GROUPS.none {
                it.label.contains("On-Box", ignoreCase = true) ||
                it.label.contains("Whisper", ignoreCase = true)
            })
    }
}
