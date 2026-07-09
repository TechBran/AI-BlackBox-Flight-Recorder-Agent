package com.aiblackbox.portal.ui.cli_agent

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Terminal-paste decision for the CLI mic's stop exits (2026-07-09 stuck-spinner
 * fix): SessionEnded / local timeout / tap-escape all paste via
 * [shouldPasteOnTerminal]. The user's words must be pasted best-effort on a user
 * stop with content, and NEVER pasted after a long-press cancel or when there is
 * nothing accumulated.
 */
class CliMicTerminalDecisionTest {

    @Test
    fun stop_with_accumulated_transcript_pastes() {
        assertTrue(shouldPasteOnTerminal(stopping = true, cancelRequested = false, committed = "hello world"))
    }

    @Test
    fun cancel_never_pastes_even_with_content() {
        assertFalse(shouldPasteOnTerminal(stopping = true, cancelRequested = true, committed = "hello world"))
    }

    @Test
    fun nothing_accumulated_pastes_nothing() {
        assertFalse(shouldPasteOnTerminal(stopping = true, cancelRequested = false, committed = ""))
        assertFalse(shouldPasteOnTerminal(stopping = true, cancelRequested = false, committed = "   "))
    }

    @Test
    fun midstream_terminal_without_user_stop_pastes_nothing() {
        // e.g. a late SessionEnded after a tap-escape already reset stopping.
        assertFalse(shouldPasteOnTerminal(stopping = false, cancelRequested = false, committed = "hello world"))
    }

    @Test
    fun ui_timeout_is_longer_than_the_client_stop_backstop() {
        // SttStreamClient waits up to 10s for the server's stt_done; the UI's
        // defensive timeout must fire strictly AFTER that so the client's own
        // fallback-final + SessionEnded path always gets to run first.
        assertTrue(TRANSCRIBING_TIMEOUT_MS > 10_000L)
    }
}
