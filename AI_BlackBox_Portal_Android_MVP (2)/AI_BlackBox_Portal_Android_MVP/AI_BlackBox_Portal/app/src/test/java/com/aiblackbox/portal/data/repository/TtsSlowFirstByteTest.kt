package com.aiblackbox.portal.data.repository

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task 7.9 — D10 slow-first-byte affordance for on-box (Qwen) TTS.
 *
 * Locks the two things the settings preview ([SettingsViewModel.previewVoice])
 * and the chat speak path ([NativeMainActivity] onSpeakWithId) BOTH depend on,
 * so the surfaces can't drift:
 *   1. the shared sentinel + threshold constants, and
 *   2. the watchdog state transition — flips ONLY after the threshold elapses
 *      while the request is still in flight; a warm/fast synthesis (completed or
 *      cancelled first) never trips it.
 *
 * Uses virtual time (advanceTimeBy + runCurrent) so it is hermetic and needs no
 * Android ViewModel / Application (the repo convention — SettingsViewModel is an
 * AndroidViewModel and can't be built on the plain JVM).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class TtsSlowFirstByteTest {

    @Test
    fun `sentinel and threshold are the shared constants both surfaces read`() {
        assertEquals("qwen", TtsRepository.ON_BOX_PROVIDER)
        assertEquals(1500L, TtsRepository.SLOW_FIRST_BYTE_MS)
    }

    @Test
    fun `flips slow only after the threshold while still in flight`() = runTest {
        var slow = false
        val job = launch {
            TtsRepository.awaitSlowFirstByte(stillInFlight = { true }, onSlow = { slow = true })
        }
        advanceTimeBy(TtsRepository.SLOW_FIRST_BYTE_MS - 1)
        runCurrent()
        assertFalse("must not trip before the threshold", slow)

        advanceTimeBy(1)
        runCurrent()
        assertTrue("must trip once the threshold elapses and the request is still in flight", slow)
        job.cancel()
    }

    @Test
    fun `does not flip when the request already completed at the threshold`() = runTest {
        var slow = false
        var inFlight = true
        val job = launch {
            TtsRepository.awaitSlowFirstByte(stillInFlight = { inFlight }, onSlow = { slow = true })
        }
        inFlight = false // first byte arrived before the timer fires
        advanceTimeBy(TtsRepository.SLOW_FIRST_BYTE_MS + 1)
        runCurrent()
        assertFalse("a warm/fast synthesis must never show the loading affordance", slow)
        job.cancel()
    }

    @Test
    fun `a watchdog cancelled before the threshold never fires`() = runTest {
        var slow = false
        val job = launch {
            TtsRepository.awaitSlowFirstByte(stillInFlight = { true }, onSlow = { slow = true })
        }
        advanceTimeBy(500)
        runCurrent()
        job.cancel() // caller cancels the instant the first byte returns
        advanceTimeBy(5000)
        runCurrent()
        assertFalse("cancelling before the threshold must suppress the affordance", slow)
    }
}
