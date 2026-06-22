package com.aiblackbox.portal

import com.aiblackbox.portal.ui.chat.ChatViewModel
import com.aiblackbox.portal.util.SpeakableText
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Phase 6b -- artifact download chips on Android.
 *
 * Two pure seams are proven here (no AndroidViewModel / Robolectric needed):
 *   (a) ChatViewModel.parseArtifacts -- tolerant extraction of the Phase 6a
 *       /chat/save response's artifacts[] into List<ArtifactRef>.
 *   (b) SpeakableText.stripArtifactBlocks -- the display strip that removes raw
 *       [ARTIFACT] blocks from the bubble once chips are rendered.
 *
 * parseArtifacts uses kotlinx (not org.json), so it is a REAL parser under
 * testOptions.unitTests.returnDefaultValues=true (org.json would be a no-op).
 */
class ArtifactRenderTest {

    // -- (a) parseArtifacts ---------------------------------------------------

    @Test fun `parses a chat-save-shaped artifacts array`() {
        val body = """
            {
              "modified_response": "Here is the report. [ARTIFACT:report.pdf:pdf]...[/ARTIFACT]",
              "artifacts": [
                {"filename": "report.pdf", "type": "pdf", "url": "/artifacts/abc123", "size_kb": 42.5},
                {"filename": "data.csv", "type": "csv", "url": "/artifacts/def456", "size_kb": 3.0}
              ]
            }
        """.trimIndent()

        val artifacts = ChatViewModel.parseArtifacts(body)
        assertEquals(2, artifacts.size)
        assertEquals("report.pdf", artifacts[0].filename)
        assertEquals("pdf", artifacts[0].type)
        assertEquals("/artifacts/abc123", artifacts[0].url)
        assertEquals(42.5, artifacts[0].sizeKb, 0.0001)
        assertEquals("data.csv", artifacts[1].filename)
        assertEquals("/artifacts/def456", artifacts[1].url)
    }

    @Test fun `empty artifacts array yields empty list`() {
        val body = """{"modified_response": "ok", "artifacts": []}"""
        assertTrue(ChatViewModel.parseArtifacts(body).isEmpty())
    }

    @Test fun `missing artifacts key yields empty list`() {
        val body = """{"modified_response": "ok"}"""
        assertTrue(ChatViewModel.parseArtifacts(body).isEmpty())
    }

    @Test fun `malformed json yields empty list and does not throw`() {
        assertTrue(ChatViewModel.parseArtifacts("not json at all {").isEmpty())
        assertTrue(ChatViewModel.parseArtifacts("").isEmpty())
        assertTrue(ChatViewModel.parseArtifacts(null).isEmpty())
    }

    @Test fun `missing size_kb defaults to zero and entries without url or filename are skipped`() {
        val body = """
            {
              "artifacts": [
                {"filename": "no-size.txt", "type": "txt", "url": "/artifacts/g1"},
                {"type": "txt", "url": "/artifacts/g2"},
                {"filename": "no-url.txt", "type": "txt"}
              ]
            }
        """.trimIndent()

        val artifacts = ChatViewModel.parseArtifacts(body)
        assertEquals(1, artifacts.size)
        assertEquals("no-size.txt", artifacts[0].filename)
        assertEquals(0.0, artifacts[0].sizeKb, 0.0001)
    }

    // -- (b) stripArtifactBlocks ----------------------------------------------

    @Test fun `strips a full artifact block`() {
        assertEquals(
            "Here is the report.  Enjoy.",
            SpeakableText.stripArtifactBlocks(
                "Here is the report. [ARTIFACT:report.pdf:pdf]base64stuff\nmore\n[/ARTIFACT] Enjoy."
            )
        )
    }

    @Test fun `strips a lone unclosed artifact opener`() {
        assertEquals(
            "Done  thanks",
            SpeakableText.stripArtifactBlocks("Done [ARTIFACT:report.pdf:pdf] thanks")
        )
    }

    @Test fun `leaves normal prose untouched`() {
        val prose = "Just normal text with no brackets, including a [link](http://x)."
        assertEquals(prose, SpeakableText.stripArtifactBlocks(prose))
    }

    @Test fun `null or empty returns empty string`() {
        assertEquals("", SpeakableText.stripArtifactBlocks(null))
        assertEquals("", SpeakableText.stripArtifactBlocks(""))
    }
}
