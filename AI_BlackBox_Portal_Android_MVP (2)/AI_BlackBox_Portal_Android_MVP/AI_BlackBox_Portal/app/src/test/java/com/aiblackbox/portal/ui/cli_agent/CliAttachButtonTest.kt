package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.api.ApiHttpException
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.IOException

/**
 * Pure-logic seams of CliAttachButton (Compose behavior is device QA):
 *
 *  - [attachRequestPath] must URL-encode the operator into the `op` query
 *    parameter (the established /cli-agent convention — hyphenated and
 *    spaced operator names must survive the trip).
 *  - [attachOutcomeMessage] is the chip-vs-toast decision: injected:true is
 *    the ONLY success-chip path; every other outcome (paste failed, HTTP
 *    error, transport error) must surface as a Toast — the forbidden
 *    silent-drop rule pinned as a test.
 *  - [uploadingChipText] shows (i/n) progress only for multi-file batches.
 */
class CliAttachButtonTest {

    // ── attachRequestPath — operator encoding ────────────────────────────────

    @Test
    fun `plain operator rides unencoded`() {
        assertEquals(
            "/cli-agent/zellij/attach-file?op=Brandon",
            attachRequestPath("Brandon"),
        )
    }

    @Test
    fun `hyphenated operator is preserved`() {
        assertEquals(
            "/cli-agent/zellij/attach-file?op=Brandon-DEV",
            attachRequestPath("Brandon-DEV"),
        )
    }

    @Test
    fun `spaces and reserved characters are URL-encoded`() {
        // URLEncoder form-encoding: space → '+', '/' → %2F, '&' → %26.
        assertEquals(
            "/cli-agent/zellij/attach-file?op=Op+Name",
            attachRequestPath("Op Name"),
        )
        assertEquals(
            "/cli-agent/zellij/attach-file?op=a%2Fb%26c",
            attachRequestPath("a/b&c"),
        )
    }

    // ── attachOutcomeMessage — chip vs toast selection ───────────────────────

    @Test
    fun `injected true flashes the success chip`() {
        assertEquals(
            AttachOutcome.Chip("📎 report.pdf attached"),
            attachOutcomeMessage("report.pdf", injected = true, serverPath = "/x/report.pdf", error = null),
        )
    }

    @Test
    fun `injected false toasts the stored path so the user can paste it`() {
        assertEquals(
            AttachOutcome.Notice("Uploaded — paste failed. Path: /uploads/t/s/report.pdf"),
            attachOutcomeMessage("report.pdf", injected = false, serverPath = "/uploads/t/s/report.pdf", error = null),
        )
    }

    @Test
    fun `injected false with no parseable path still toasts`() {
        assertEquals(
            AttachOutcome.Notice("Uploaded — paste failed. Path: (unknown)"),
            attachOutcomeMessage("report.pdf", injected = false, serverPath = null, error = null),
        )
    }

    @Test
    fun `ApiHttpException surfaces the backend detail verbatim`() {
        val e = ApiHttpException("Cannot attach to a session belonging to another operator")
        assertEquals(
            AttachOutcome.Notice("Cannot attach to a session belonging to another operator"),
            attachOutcomeMessage("report.pdf", injected = false, serverPath = null, error = e),
        )
    }

    @Test
    fun `transport IOException is wrapped in friendly copy`() {
        assertEquals(
            AttachOutcome.Notice("Upload failed: timeout"),
            attachOutcomeMessage("report.pdf", injected = false, serverPath = null, error = IOException("timeout")),
        )
    }

    @Test
    fun `an error always wins over a claimed injection`() {
        // A thrown upload can never be reported as an attached success.
        val out = attachOutcomeMessage(
            "report.pdf",
            injected = true,
            serverPath = "/x",
            error = IOException("connection reset"),
        )
        assertEquals(AttachOutcome.Notice("Upload failed: connection reset"), out)
    }

    // ── uploadingChipText — (i or n) progress ────────────────────────────────

    @Test
    fun `single file omits the progress counter`() {
        assertEquals("Uploading photo.jpg…", uploadingChipText("photo.jpg", index = 0, total = 1))
    }

    @Test
    fun `multi-file batch shows one-based i of n`() {
        assertEquals("Uploading a.txt… (1/3)", uploadingChipText("a.txt", index = 0, total = 3))
        assertEquals("Uploading c.txt… (3/3)", uploadingChipText("c.txt", index = 2, total = 3))
    }
}
