package com.aiblackbox.portal

import com.aiblackbox.portal.util.SpeakableText.stripNonSpeakable
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * §3.5 speakable-text sanitizer. Mirrors the Portal node verifier cases so both
 * surfaces are proven to apply IDENTICAL rules before /tts/batch.
 */
class SpeakableTextTest {

    @Test fun `artifact block removed`() {
        assertEquals(
            "Here is the report enjoy.",
            stripNonSpeakable("Here is the report [ARTIFACT:report.pdf:pdf]base64stuff\nmore\n[/ARTIFACT] enjoy.")
        )
    }

    @Test fun `unclosed artifact opener removed`() {
        assertEquals(
            "Done thanks",
            stripNonSpeakable("Done [ARTIFACT:report.pdf:pdf] thanks")
        )
    }

    @Test fun `fenced code becomes code block`() {
        assertEquals(
            "Run this: code block That works.",
            stripNonSpeakable("Run this:\n```js\nconsole.log(1)\n```\nThat works.")
        )
    }

    @Test fun `fenced json becomes code block`() {
        assertEquals(
            "See: code block done",
            stripNonSpeakable("See:\n```json\n{\"a\":1}\n```\ndone")
        )
    }

    @Test fun `relative media url removed`() {
        assertEquals(
            "Your image is at now.",
            stripNonSpeakable("Your image is at /ui/uploads/2026/x.png now.")
        )
    }

    @Test fun `absolute media url removed`() {
        assertEquals(
            "Open to view.",
            stripNonSpeakable("Open http://localhost:9091/ui/uploads/a/b.mp4 to view.")
        )
    }

    @Test fun `whole-string envelope unwrapped to ui_reply`() {
        assertEquals(
            "Hello there.",
            stripNonSpeakable("{\"ui_reply\":\"Hello there.\",\"snapshot_perspective\":\"x\"}")
        )
    }

    @Test fun `fenced whole-string envelope unwrapped`() {
        assertEquals(
            "Fenced reply.",
            stripNonSpeakable("```json\n{\"ui_reply\":\"Fenced reply.\",\"snapshot_perspective\":\"y\"}\n```")
        )
    }

    @Test fun `plain prose unchanged`() {
        assertEquals(
            "The quick brown fox jumps over the lazy dog.",
            stripNonSpeakable("The quick brown fox jumps over the lazy dog.")
        )
    }

    @Test fun `mid-prose envelope NOT unwrapped`() {
        assertEquals(
            "I returned {\"ui_reply\":\"x\"} as the payload.",
            stripNonSpeakable("I returned {\"ui_reply\":\"x\"} as the payload.")
        )
    }

    // --- Cross-surface parity (FIX I-1): JSON-validated envelope unwrap. ---
    // Portal (JSON.parse) and reply_envelope.py (json.loads) only unwrap a
    // LEADING object that ACTUALLY parses as JSON. These malformed-but-leading
    // inputs must be PRESERVED on Android too (the old raw-regex wrongly
    // extracted ui_reply, diverging from Portal).

    @Test fun `trailing-comma envelope preserved (parity with Portal)`() {
        // {"ui_reply":"hi",} is not valid JSON -> Portal preserves whole; Android must too.
        assertEquals(
            "{\"ui_reply\":\"hi\",}",
            stripNonSpeakable("{\"ui_reply\":\"hi\",}")
        )
    }

    @Test fun `fake ui_reply in non-JSON preserved (parity with Portal)`() {
        // {not json but "ui_reply":"trap" here} does not parse -> preserve whole.
        assertEquals(
            "{not json but \"ui_reply\":\"trap\" here}",
            stripNonSpeakable("{not json but \"ui_reply\":\"trap\" here}")
        )
    }

    @Test fun `JSON5 nested envelope preserved (parity with Portal)`() {
        // {data: {"ui_reply":"deep"}} is JSON5/invalid (unquoted key) -> preserve whole.
        assertEquals(
            "{data: {\"ui_reply\":\"deep\"}}",
            stripNonSpeakable("{data: {\"ui_reply\":\"deep\"}}")
        )
    }

    @Test fun `null and empty return empty`() {
        assertEquals("", stripNonSpeakable(null))
        assertEquals("", stripNonSpeakable(""))
    }
}
