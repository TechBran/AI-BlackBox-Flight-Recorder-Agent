package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * "Land the decode before the audio is audible" needs a deadline, because the decode can also fail
 * or take forever (a remote clip whose bytes are still arriving). This is the whole policy: hold
 * the first sample until the envelope is in hand, but never for longer than
 * [PlaybackStartGate.MAX_WAIT_MS] — audio that will not play is a worse bug than a ribbon that
 * starts flat.
 *
 * In practice the wait is zero: the bar prefetches on composition, so by the time the operator taps
 * play the envelope is usually already cached.
 */
class PlaybackStartGateTest {

    @Test
    fun `a ready envelope starts immediately`() {
        assertTrue(PlaybackStartGate.shouldStart(envelopeReady = true, waitedMs = 0))
    }

    @Test
    fun `an undecoded envelope holds the first sample`() {
        assertFalse(PlaybackStartGate.shouldStart(envelopeReady = false, waitedMs = 0))
        assertFalse(
            PlaybackStartGate.shouldStart(
                envelopeReady = false,
                waitedMs = PlaybackStartGate.MAX_WAIT_MS - 1,
            )
        )
    }

    @Test
    fun `the wait is bounded, so a failed decode can never mute the app`() {
        assertTrue(
            PlaybackStartGate.shouldStart(
                envelopeReady = false,
                waitedMs = PlaybackStartGate.MAX_WAIT_MS,
            )
        )
        assertTrue(PlaybackStartGate.shouldStart(envelopeReady = false, waitedMs = 10_000))
    }

    @Test
    fun `the bound stays under a quarter second of perceptible lag`() {
        // A hold long enough to read as a broken button is not a fix. Pinned so a later
        // "just wait a bit longer" cannot quietly trade the tap-to-sound feel away.
        assertTrue(PlaybackStartGate.MAX_WAIT_MS in 1..400)
    }
}
