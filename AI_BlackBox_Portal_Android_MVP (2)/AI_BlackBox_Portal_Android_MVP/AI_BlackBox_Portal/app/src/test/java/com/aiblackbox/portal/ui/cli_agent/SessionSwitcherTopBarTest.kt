package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.model.ZellijSessionRow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.time.Instant

/**
 * T20 unit tests for the pure helper functions inside
 * [SessionSwitcherTopBar.kt]. The composable itself isn't exercised here —
 * Compose UI testing dependencies are wired only into `androidTest`
 * (instrumented) in app/build.gradle, and T23 owns on-device verification.
 *
 * Coverage:
 *   - [relativeTime] bucket boundaries: just now, Xs, Xm, Xh, Xd.
 *   - [relativeTime] null / blank / unparseable input → null.
 *   - [titleCaseProvider] single-word casing for known provider slugs.
 *   - [labelFor] / [sessionRowLabel] composition (provider + time + app).
 *   - [PROVIDER_SHORTCUTS] is the exact ordered list T20 specifies.
 */
class SessionSwitcherTopBarTest {

    private val NOW: Long = Instant.parse("2026-05-26T12:00:00Z").toEpochMilli()
    private fun isoOffset(secondsAgo: Long): String =
        Instant.ofEpochMilli(NOW - secondsAgo * 1000L).toString()

    // ── relativeTime ─────────────────────────────────────────────────────

    @Test
    fun `relativeTime returns just now under 30 seconds`() {
        assertEquals("just now", relativeTime(isoOffset(0), NOW))
        assertEquals("just now", relativeTime(isoOffset(15), NOW))
        assertEquals("just now", relativeTime(isoOffset(29), NOW))
    }

    @Test
    fun `relativeTime returns seconds between 30 and 60`() {
        assertEquals("30s ago", relativeTime(isoOffset(30), NOW))
        assertEquals("45s ago", relativeTime(isoOffset(45), NOW))
        assertEquals("59s ago", relativeTime(isoOffset(59), NOW))
    }

    @Test
    fun `relativeTime returns minutes between 1 minute and 1 hour`() {
        assertEquals("1m ago", relativeTime(isoOffset(60), NOW))
        assertEquals("2m ago", relativeTime(isoOffset(120), NOW))
        assertEquals("59m ago", relativeTime(isoOffset(59 * 60), NOW))
    }

    @Test
    fun `relativeTime returns hours between 1 hour and 1 day`() {
        assertEquals("1h ago", relativeTime(isoOffset(3600), NOW))
        assertEquals("2h ago", relativeTime(isoOffset(2 * 3600), NOW))
        assertEquals("23h ago", relativeTime(isoOffset(23 * 3600), NOW))
    }

    @Test
    fun `relativeTime returns days when older than 1 day`() {
        assertEquals("1d ago", relativeTime(isoOffset(86_400), NOW))
        assertEquals("3d ago", relativeTime(isoOffset(3 * 86_400), NOW))
        assertEquals("30d ago", relativeTime(isoOffset(30 * 86_400), NOW))
    }

    @Test
    fun `relativeTime returns null for null or blank input`() {
        assertNull(relativeTime(null, NOW))
        assertNull(relativeTime("", NOW))
        assertNull(relativeTime("   ", NOW))
    }

    @Test
    fun `relativeTime returns null for unparseable input`() {
        assertNull(relativeTime("not an iso", NOW))
        assertNull(relativeTime("2026-99-99T00:00:00Z", NOW))
    }

    // ── titleCaseProvider ────────────────────────────────────────────────

    @Test
    fun `titleCaseProvider capitalises first letter`() {
        assertEquals("Claude", titleCaseProvider("claude"))
        assertEquals("Gemini", titleCaseProvider("gemini"))
        assertEquals("Codex", titleCaseProvider("codex"))
        assertEquals("Antigravity", titleCaseProvider("antigravity"))
        assertEquals("Terminal", titleCaseProvider("terminal"))
    }

    @Test
    fun `titleCaseProvider leaves already-cased input alone`() {
        assertEquals("Claude", titleCaseProvider("Claude"))
        assertEquals("CLAUDE", titleCaseProvider("CLAUDE"))
    }

    @Test
    fun `titleCaseProvider returns fallback for blank slug`() {
        assertEquals("Session", titleCaseProvider(""))
        assertEquals("Session", titleCaseProvider("   "))
    }

    // ── labelFor (top-bar header) ────────────────────────────────────────

    @Test
    fun `labelFor returns provider plus relative time`() {
        val row = ZellijSessionRow(
            name = "Brandon__claude__root__1779750372",
            provider = "claude",
            createdAt = isoOffset(120),
        )
        assertEquals("Claude · 2m ago", labelFor(row, NOW))
    }

    @Test
    fun `labelFor drops time half when timestamp absent`() {
        val row = ZellijSessionRow(
            name = "x",
            provider = "terminal",
            createdAt = null,
        )
        assertEquals("Terminal", labelFor(row, NOW))
    }

    @Test
    fun `labelFor returns No session when session is null`() {
        assertEquals("No session", labelFor(null, NOW))
    }

    // ── sessionRowLabel (dropdown row) ──────────────────────────────────

    @Test
    fun `sessionRowLabel composes provider time and app`() {
        val row = ZellijSessionRow(
            name = "x",
            provider = "gemini",
            createdAt = isoOffset(3600),
            app = "blackbox-poc",
        )
        assertEquals("Gemini · 1h ago · blackbox-poc", sessionRowLabel(row, NOW))
    }

    @Test
    fun `sessionRowLabel omits app when null or blank`() {
        val row = ZellijSessionRow(
            name = "x",
            provider = "codex",
            createdAt = isoOffset(86_400),
            app = null,
        )
        assertEquals("Codex · 1d ago", sessionRowLabel(row, NOW))

        val rowBlank = row.copy(app = "")
        assertEquals("Codex · 1d ago", sessionRowLabel(rowBlank, NOW))
    }

    @Test
    fun `sessionRowLabel omits time when timestamp absent`() {
        val row = ZellijSessionRow(
            name = "x",
            provider = "antigravity",
            createdAt = null,
            app = "demo",
        )
        assertEquals("Antigravity · demo", sessionRowLabel(row, NOW))
    }

    // ── Shortcut list ordering ──────────────────────────────────────────

    @Test
    fun `PROVIDER_SHORTCUTS holds the exact T20 ordered list`() {
        assertEquals(
            listOf("claude", "gemini", "codex", "antigravity"),
            PROVIDER_SHORTCUTS,
        )
    }
}
