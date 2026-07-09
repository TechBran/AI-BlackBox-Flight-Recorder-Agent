package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.voice.SttStreamClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Terminal-paste decision for the CLI mic's stop exits (2026-07-09 stuck-spinner
 * fix): SessionEnded / local timeout / tap-escape all paste via
 * [shouldPasteOnTerminal]. The user's words must be pasted best-effort on a user
 * stop with content, and NEVER pasted after a long-press cancel or when there is
 * nothing to paste. The timeout/tap-escape paths pass the JOINED preview text
 * (committed + current partial) because no fallback final has folded the partial
 * into `committed` on those paths.
 */
class CliMicTerminalDecisionTest {

    @Test
    fun stop_with_accumulated_transcript_pastes() {
        assertTrue(shouldPasteOnTerminal(stopping = true, cancelRequested = false, text = "hello world"))
    }

    @Test
    fun cancel_never_pastes_even_with_content() {
        assertFalse(shouldPasteOnTerminal(stopping = true, cancelRequested = true, text = "hello world"))
    }

    @Test
    fun nothing_accumulated_pastes_nothing() {
        assertFalse(shouldPasteOnTerminal(stopping = true, cancelRequested = false, text = ""))
        assertFalse(shouldPasteOnTerminal(stopping = true, cancelRequested = false, text = "   "))
    }

    @Test
    fun midstream_terminal_without_user_stop_pastes_nothing() {
        // e.g. a late SessionEnded after a tap-escape already reset stopping.
        assertFalse(shouldPasteOnTerminal(stopping = false, cancelRequested = false, text = "hello world"))
    }

    @Test
    fun single_utterance_with_empty_committed_pastes_the_joined_preview_text() {
        // The case that motivated joined-text pasting: one utterance, no final
        // ever arrived → committed is empty but the partial holds the words.
        // Tap-escape/timeout paste joinTranscript(committed, partial), which
        // must be non-empty and thus pasteable.
        val joined = joinTranscript("", "hello from a single utterance")
        assertEquals("hello from a single utterance", joined)
        assertTrue(shouldPasteOnTerminal(stopping = true, cancelRequested = false, text = joined))
        // committed-only would have (wrongly) pasted nothing here:
        assertFalse(shouldPasteOnTerminal(stopping = true, cancelRequested = false, text = ""))
    }

    @Test
    fun joined_preview_text_includes_both_committed_and_partial() {
        assertEquals(
            "first utterance and the tail",
            joinTranscript("first utterance", "and the tail"),
        )
    }

    @Test
    fun ui_timeout_is_longer_than_the_client_stop_backstop() {
        // SttStreamClient waits up to STOP_BACKSTOP_MS for the server's stt_done;
        // the UI's defensive timeout must fire strictly AFTER that so the
        // client's own fallback-final + SessionEnded path always runs first.
        // Asserted against the REAL constant so the layers can't silently drift.
        assertTrue(TRANSCRIBING_TIMEOUT_MS > SttStreamClient.STOP_BACKSTOP_MS)
    }
}
