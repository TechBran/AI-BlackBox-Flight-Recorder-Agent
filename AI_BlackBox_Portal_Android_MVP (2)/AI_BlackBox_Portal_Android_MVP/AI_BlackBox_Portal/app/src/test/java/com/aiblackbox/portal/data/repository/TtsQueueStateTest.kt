package com.aiblackbox.portal.data.repository

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * B3 (2026-07-22): unit tests for the on-box TTS queue client's PURE core —
 * GET /tts/task/{id} payload parsing ([TtsQueue.parseState]), the bubble
 * status strings ([TtsQueue.statusLine]), the error-backoff poll delay, and
 * the submit->poll state machine ([TtsQueue.pollLoop]) driven entirely by
 * fake fetch responses. Mirrors the web-side tts-stt.queue.test.mjs so the
 * two surfaces cannot drift on semantics.
 */
class TtsQueueStateTest {

    // =========================================================================
    // parseState — typed states from raw /tts/task JSON
    // =========================================================================

    @Test
    fun parse_queued_positionThree_meansTwoAhead() {
        val st = TtsQueue.parseState(
            """{"task_id":"ttsq-1","status":"queued","queue_position":3,"eta_s":12.0}"""
        )
        assertEquals(TtsQueueState.Queued(ahead = 2), st)
    }

    @Test
    fun parse_queued_positionOne_meansZeroAhead() {
        val st = TtsQueue.parseState("""{"status":"queued","queue_position":1}""")
        assertEquals(TtsQueueState.Queued(ahead = 0), st)
    }

    @Test
    fun parse_queued_missingPosition_defaultsToZeroAhead() {
        val st = TtsQueue.parseState("""{"status":"queued"}""")
        assertEquals(TtsQueueState.Queued(ahead = 0), st)
    }

    @Test
    fun parse_generating_carriesProgressFields() {
        val st = TtsQueue.parseState(
            """{"status":"generating","subbatch":2,"subbatches_total":5,
                "elapsed_s":75.4,"eta_s":30.2}"""
        )
        assertEquals(
            TtsQueueState.Generating(subbatch = 2, total = 5, elapsedS = 75.4, etaS = 30.2),
            st
        )
    }

    @Test
    fun parse_generating_missingFields_defaultsToZeros() {
        val st = TtsQueue.parseState("""{"status":"generating"}""")
        assertEquals(
            TtsQueueState.Generating(subbatch = 0, total = 0, elapsedS = 0.0, etaS = 0.0),
            st
        )
    }

    @Test
    fun parse_done_carriesAudioUrl() {
        val st = TtsQueue.parseState(
            """{"status":"done","audio_url":"/ui/uploads/ttsq-abc.wav","seconds":42.5}"""
        )
        assertEquals(TtsQueueState.Done("/ui/uploads/ttsq-abc.wav"), st)
    }

    @Test
    fun parse_done_withoutAudioUrl_isNonRetryableFailure() {
        val st = TtsQueue.parseState("""{"status":"done"}""")
        assertTrue(st is TtsQueueState.Failed)
        assertFalse((st as TtsQueueState.Failed).retryable)
    }

    @Test
    fun parse_failed_carriesErrorAndRetryable() {
        val st = TtsQueue.parseState(
            """{"status":"failed","error":"Qwen TTS failed (HTTP 500)","retryable":true}"""
        )
        assertEquals(
            TtsQueueState.Failed(error = "Qwen TTS failed (HTTP 500)", retryable = true),
            st
        )
    }

    @Test
    fun parse_failed_missingFields_defaults() {
        val st = TtsQueue.parseState("""{"status":"failed"}""")
        assertTrue(st is TtsQueueState.Failed)
        assertFalse((st as TtsQueueState.Failed).retryable)
        assertTrue(st.error.isNotBlank())
    }

    @Test
    fun parse_cancelled_foldsIntoNonRetryableFailure() {
        val st = TtsQueue.parseState("""{"status":"cancelled"}""")
        assertTrue(st is TtsQueueState.Failed)
        assertFalse((st as TtsQueueState.Failed).retryable)
    }

    @Test
    fun parse_unknownStatus_isNull() {
        assertNull(TtsQueue.parseState("""{"status":"warp-drive"}"""))
    }

    @Test
    fun parse_garbage_isNull() {
        assertNull(TtsQueue.parseState("<html>502 bad gateway</html>"))
        assertNull(TtsQueue.parseState(""))
        assertNull(TtsQueue.parseState("""["not","an","object"]"""))
    }

    // =========================================================================
    // isTerminal
    // =========================================================================

    @Test
    fun terminal_doneAndFailedOnly() {
        assertTrue(TtsQueue.isTerminal(TtsQueueState.Done("/x.wav")))
        assertTrue(TtsQueue.isTerminal(TtsQueueState.Failed("e", true)))
        assertFalse(TtsQueue.isTerminal(TtsQueueState.Queued(0)))
        assertFalse(TtsQueue.isTerminal(TtsQueueState.Generating(1, 2, 0.0, 0.0)))
    }

    // =========================================================================
    // statusLine — the bubble chip strings
    // =========================================================================

    @Test
    fun statusLine_queuedWithJobsAhead() {
        assertEquals("Queued — 2 ahead", TtsQueue.statusLine(TtsQueueState.Queued(2)))
    }

    @Test
    fun statusLine_queuedNextUp() {
        assertEquals("Queued — starting next", TtsQueue.statusLine(TtsQueueState.Queued(0)))
    }

    @Test
    fun statusLine_generatingWithProgress() {
        val line = TtsQueue.statusLine(
            TtsQueueState.Generating(subbatch = 2, total = 5, elapsedS = 75.0, etaS = 30.4)
        )
        assertEquals("Generating 2/5… 1:15, ~30s left", line)
    }

    @Test
    fun statusLine_generatingNoTotals_showsClockOnly() {
        val line = TtsQueue.statusLine(
            TtsQueueState.Generating(subbatch = 0, total = 0, elapsedS = 5.0, etaS = 0.0)
        )
        assertEquals("Generating… 0:05", line)
    }

    @Test
    fun statusLine_generatingClampsSubbatchIntoRange() {
        // Server reports subbatch 0 while total is known (first tick) — show 1/K.
        val line = TtsQueue.statusLine(
            TtsQueueState.Generating(subbatch = 0, total = 3, elapsedS = 0.0, etaS = 12.0)
        )
        assertEquals("Generating 1/3… 0:00, ~12s left", line)
    }

    @Test
    fun statusLine_terminalStates() {
        assertEquals("Audio ready", TtsQueue.statusLine(TtsQueueState.Done("/x.wav")))
        assertEquals(
            "Audio failed: boom",
            TtsQueue.statusLine(TtsQueueState.Failed("boom", true))
        )
    }

    @Test
    fun formatClock_minutesAndZeroPaddedSeconds() {
        assertEquals("0:00", TtsQueue.formatClock(0.0))
        assertEquals("0:09", TtsQueue.formatClock(9.9))
        assertEquals("1:15", TtsQueue.formatClock(75.0))
        assertEquals("12:03", TtsQueue.formatClock(723.0))
        assertEquals("0:00", TtsQueue.formatClock(-5.0))
    }

    // =========================================================================
    // pollDelayMs — exponential backoff on consecutive fetch errors only
    // =========================================================================

    @Test
    fun pollDelay_healthyLoopStaysAtBase() {
        assertEquals(1500L, TtsQueue.pollDelayMs(0))
    }

    @Test
    fun pollDelay_backsOffAndCaps() {
        assertEquals(3000L, TtsQueue.pollDelayMs(1))
        assertEquals(6000L, TtsQueue.pollDelayMs(2))
        assertEquals(12000L, TtsQueue.pollDelayMs(3))
        assertEquals(12000L, TtsQueue.pollDelayMs(4))
        assertEquals(12000L, TtsQueue.pollDelayMs(99))
    }

    @Test
    fun pollDelay_negativeClampsToBase() {
        assertEquals(1500L, TtsQueue.pollDelayMs(-3))
    }

    // =========================================================================
    // pollLoop — the state machine, driven by fake fetch responses
    // =========================================================================

    private class FakeFetcher(vararg responses: TtsQueueFetch) {
        private val queue = ArrayDeque(responses.toList())
        var calls = 0
        suspend fun fetch(): TtsQueueFetch {
            calls += 1
            return queue.removeFirstOrNull()
                ?: throw AssertionError("pollLoop fetched more than the fixture provides")
        }
    }

    private fun body(json: String) = TtsQueueFetch.Body(json)

    @Test
    fun pollLoop_happyPath_queuedGeneratingDone() = runTest {
        val fetcher = FakeFetcher(
            body("""{"status":"queued","queue_position":2}"""),
            body("""{"status":"generating","subbatch":1,"subbatches_total":2,"elapsed_s":3.0,"eta_s":9.0}"""),
            body("""{"status":"done","audio_url":"/ui/uploads/t.wav"}"""),
        )
        val seen = mutableListOf<TtsQueueState>()
        val delays = mutableListOf<Long>()
        val terminal = TtsQueue.pollLoop(
            fetch = fetcher::fetch,
            onStatus = { seen.add(it) },
            delayMs = { delays.add(it) },
        )
        assertEquals(TtsQueueState.Done("/ui/uploads/t.wav"), terminal)
        assertEquals(
            listOf(
                TtsQueueState.Queued(1),
                TtsQueueState.Generating(1, 2, 3.0, 9.0),
            ),
            seen
        )
        // Healthy loop: base cadence between the two non-terminal polls.
        assertEquals(listOf(1500L, 1500L), delays)
        assertEquals(3, fetcher.calls)
    }

    @Test
    fun pollLoop_terminalFailure_isReturnedNotRetriedLocally() = runTest {
        val fetcher = FakeFetcher(
            body("""{"status":"failed","error":"GPU on fire","retryable":true}"""),
        )
        val terminal = TtsQueue.pollLoop(fetch = fetcher::fetch, delayMs = { })
        assertEquals(TtsQueueState.Failed("GPU on fire", retryable = true), terminal)
        assertEquals(1, fetcher.calls)
    }

    @Test
    fun pollLoop_transientErrorsBackOff_thenRecover() = runTest {
        val fetcher = FakeFetcher(
            TtsQueueFetch.TransportError,
            TtsQueueFetch.TransportError,
            body("""{"status":"generating","subbatch":1,"subbatches_total":1,"elapsed_s":1.0,"eta_s":1.0}"""),
            body("""{"status":"done","audio_url":"/u.wav"}"""),
        )
        val delays = mutableListOf<Long>()
        val terminal = TtsQueue.pollLoop(fetch = fetcher::fetch, delayMs = { delays.add(it) })
        assertEquals(TtsQueueState.Done("/u.wav"), terminal)
        // err1 -> 3000, err2 -> 6000, healthy body -> reset to 1500.
        assertEquals(listOf(3000L, 6000L, 1500L), delays)
    }

    @Test
    fun pollLoop_eightConsecutiveErrors_givesUpRetryable() = runTest {
        val fetcher = FakeFetcher(
            *Array(8) { TtsQueueFetch.TransportError }
        )
        val terminal = TtsQueue.pollLoop(fetch = fetcher::fetch, delayMs = { })
        assertTrue(terminal is TtsQueueState.Failed)
        assertTrue((terminal as TtsQueueState.Failed).retryable)
        assertEquals(8, fetcher.calls)
    }

    @Test
    fun pollLoop_notFound_meansServiceRestartDroppedQueue() = runTest {
        val fetcher = FakeFetcher(TtsQueueFetch.NotFound)
        val terminal = TtsQueue.pollLoop(fetch = fetcher::fetch, delayMs = { })
        assertEquals(
            TtsQueueState.Failed("queue task lost (service restarted)", retryable = false),
            terminal
        )
    }

    @Test
    fun pollLoop_unparseableBody_countsAsTransientError() = runTest {
        val fetcher = FakeFetcher(
            body("<html>proxy error</html>"),
            body("""{"status":"done","audio_url":"/u.wav"}"""),
        )
        val delays = mutableListOf<Long>()
        val terminal = TtsQueue.pollLoop(fetch = fetcher::fetch, delayMs = { delays.add(it) })
        assertEquals(TtsQueueState.Done("/u.wav"), terminal)
        assertEquals(listOf(3000L), delays) // one error -> one backed-off delay
    }
}
