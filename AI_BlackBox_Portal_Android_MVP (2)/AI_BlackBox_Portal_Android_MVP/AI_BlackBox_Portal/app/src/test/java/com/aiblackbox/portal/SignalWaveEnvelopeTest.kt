package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.signalWaveEnvelope
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Readability fix (mirrors web signal-feed.js @ 6e5102a): the SignalLine wave is a
 * per-line ENTRANCE that eases out to FLAT and then holds still. This pins the pure
 * amplitude envelope: peak at arrival, ease-out (k²) to 0 over 700ms, flat 0 after.
 */
class SignalWaveEnvelopeTest {

    private val peak = 2f // px (matches the composable's halved peak)

    @Test fun `full amplitude at arrival`() {
        assertEquals(peak, signalWaveEnvelope(0.0, peak), 1e-4f)
    }

    @Test fun `ease-out k squared at the midpoint`() {
        // elapsed = 350ms → k = 0.5 → amp = peak * 0.25
        assertEquals(peak * 0.25f, signalWaveEnvelope(350.0, peak), 1e-4f)
    }

    @Test fun `flat exactly at the decay deadline`() {
        assertEquals(0f, signalWaveEnvelope(700.0, peak), 1e-6f)
    }

    @Test fun `stays flat (0) forever after the deadline — the readable hold`() {
        assertEquals(0f, signalWaveEnvelope(701.0, peak), 1e-6f)
        assertEquals(0f, signalWaveEnvelope(5000.0, peak), 1e-6f)
    }

    @Test fun `monotonically decreasing across the decay`() {
        var prev = signalWaveEnvelope(0.0, peak)
        for (t in longArrayOf(100, 200, 350, 500, 650, 700)) {
            val cur = signalWaveEnvelope(t.toDouble(), peak)
            assertTrue("amp must not increase at t=$t ($cur > $prev)", cur <= prev + 1e-5f)
            prev = cur
        }
    }

    @Test fun `reduced motion (peak 0) is always flat`() {
        assertEquals(0f, signalWaveEnvelope(0.0, 0f), 1e-6f)
        assertEquals(0f, signalWaveEnvelope(300.0, 0f), 1e-6f)
    }
}
