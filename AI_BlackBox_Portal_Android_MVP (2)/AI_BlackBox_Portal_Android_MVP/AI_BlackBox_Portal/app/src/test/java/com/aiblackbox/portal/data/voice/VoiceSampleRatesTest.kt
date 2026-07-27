package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin

/**
 * The voice screen runs TWO analysers at once and they do not share a rate- the mic's depends on
 * the backend (24 kHz only for GPT realtime, 16 kHz for the rest) while the AI's playback is
 * always 24 kHz. Naming the rates here is what lets the AudioRecord and its analyser read the same
 * number; before this they were two literals in two methods, and the analyser would have silently
 * probed a 16 kHz mic as though it were 24 kHz (a 1.5x frequency error in every band).
 */
class VoiceSampleRatesTest {

    @Test
    fun `only GPT realtime captures at 24 kHz`() {
        assertEquals(24000, VoiceSampleRates.mic(VoiceBackend.GPT_REALTIME))
        assertEquals(16000, VoiceSampleRates.mic(VoiceBackend.GEMINI_LIVE))
        assertEquals(16000, VoiceSampleRates.mic(VoiceBackend.GROK_LIVE))
    }

    @Test
    fun `the AI playback rate matches the AudioTrack the chunks are written to`() {
        assertEquals(24000, VoiceSampleRates.AI_OUTPUT)
    }

    @Test
    fun `every rate we capture at still resolves the sibilance band`() {
        // The analyser DROPS any probe at or above its Nyquist margin rather than aliasing into it,
        // so a rate chosen too low silently kills the top band and the ribbon loses its sparkle
        // with no error anywhere. 5 kHz is band 3's lower probe- assert it survives at every rate
        // this app actually captures or plays at.
        val rates = VoiceBackend.entries.map { VoiceSampleRates.mic(it) } + VoiceSampleRates.AI_OUTPUT
        for (rate in rates.distinct()) {
            val alive = AuroraAnalyser.analyzeOffline(tone(5000f, rate, 200), rate).first
            assertTrue("band 3 died at ${rate}Hz", alive.maxOf { it[3] } > 0.9f)
        }

        // The control that gives the assertion above its teeth- at 8 kHz (mu-law telephony, a rate
        // we deliberately never capture at) BOTH of band 3's probes sit past the guard, the band is
        // dropped rather than aliased, and it reports a flat nothing. That silent nothing is
        // exactly the failure a badly chosen capture rate would produce.
        val dead = AuroraAnalyser.analyzeOffline(tone(5000f, 8000, 200), 8000).first
        assertEquals("8kHz should not resolve band 3 at all", 0f, dead.maxOf { it[3] }, 0f)
    }

    /** [durationMs] of a full-scale sine at [freq], as PCM16 mono samples. */
    private fun tone(freq: Float, sampleRate: Int, durationMs: Int): ShortArray {
        val n = sampleRate * durationMs / 1000
        return ShortArray(n) { i ->
            (sin(2.0 * PI * freq * i / sampleRate) * 30000.0).toInt().toShort()
        }
    }
}
