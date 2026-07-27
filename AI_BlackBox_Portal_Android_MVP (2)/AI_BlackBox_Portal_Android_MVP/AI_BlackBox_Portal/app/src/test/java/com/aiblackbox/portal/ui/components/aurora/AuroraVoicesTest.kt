package com.aiblackbox.portal.ui.components.aurora

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * [auroraVoices] is the seam all four audio sources pass through on their way to the ribbon:
 *
 *   composer mic    -> human only
 *   voice-screen    -> human AND ai, simultaneously (the load-bearing case)
 *   TTS player bar  -> ai only
 *
 * The tests below pin the two properties every one of those call sites depends on- a speaker
 * whose bands are `null` is ABSENT (its ribbon retires to flat), and two live speakers stay two
 * separate voices with their own band data rather than being merged or overwriting each other.
 */
class AuroraVoicesTest {

    @Test
    fun `human and AI live at once stay two separate voices`() {
        val voices = auroraVoices(
            human = floatArrayOf(0.9f, 0.8f, 0.7f, 0.6f),
            ai = floatArrayOf(0.1f, 0.2f, 0.3f, 0.4f),
        )

        assertEquals(2, voices.size)
        assertEquals(
            setOf(AuroraSpeaker.HUMAN, AuroraSpeaker.AI),
            voices.map { it.speaker }.toSet(),
        )
        // The bands must not cross over: this is exactly the bug a shared analyser or a
        // position-keyed envelope would produce, and it is invisible on screen until the two
        // ribbons swap levels mid-sentence.
        val human = voices.first { it.speaker == AuroraSpeaker.HUMAN }
        val ai = voices.first { it.speaker == AuroraSpeaker.AI }
        assertEquals(0.9f, human.band(0), 1e-6f)
        assertEquals(0.1f, ai.band(0), 1e-6f)
    }

    @Test
    fun `two live speakers draw in different colours`() {
        // Brandon's locked decision- RED is the human, BLUE is the AI. If these ever collapse to
        // one colour the simultaneous case above becomes unreadable no matter how the phases weave.
        assertNotEquals(
            auroraSpeakerColor(AuroraSpeaker.HUMAN),
            auroraSpeakerColor(AuroraSpeaker.AI),
        )
    }

    @Test
    fun `a source with no audio is absent rather than silent`() {
        // The player bar rests FLAT when nothing is playing (it passed idleLevel = 0f to the old
        // renderer). A PRESENT voice breathes at Aurora.BASELINE instead, so "nothing playing"
        // has to mean an empty list, not a zero-filled one.
        assertTrue(auroraVoices().isEmpty())
        assertTrue(auroraVoices(human = null, ai = null).isEmpty())
    }

    @Test
    fun `a present but silent source still yields a voice`() {
        // The composer ribbon is composed only while recording and must stay on screen through
        // the pauses between words — silence is not absence.
        val voices = auroraVoices(human = FloatArray(AuroraAnalyser.BANDS))

        assertEquals(1, voices.size)
        assertEquals(AuroraSpeaker.HUMAN, voices[0].speaker)
        assertEquals(0f, voices[0].band(0), 0f)
    }

    @Test
    fun `only the AI is present for a playback-only container`() {
        val voices = auroraVoices(ai = floatArrayOf(0.5f))

        assertEquals(1, voices.size)
        assertEquals(AuroraSpeaker.AI, voices[0].speaker)
        // One value drives all four sheets — the documented fallback for a source that only has
        // an RMS level (a clip whose band pass could not run at all, or the raw-audio recorder).
        assertEquals(0.5f, voices[0].band(3), 1e-6f)
    }

    @Test
    fun `the human is listed first so two-voice containers have a stable order`() {
        val voices = auroraVoices(human = floatArrayOf(0.4f), ai = floatArrayOf(0.4f))

        assertEquals(AuroraSpeaker.HUMAN, voices[0].speaker)
        assertEquals(AuroraSpeaker.AI, voices[1].speaker)
    }

    @Test
    fun `the shared silent bands are one instance so repeated silence conflates`() {
        // Producers publish AURORA_SILENT_BANDS through a StateFlow. StateFlow drops a value that
        // `equals` the current one and FloatArray compares by IDENTITY — so handing out the SAME
        // instance is what stops an idle 30 Hz "still silent" tick from recomposing the screen
        // thirty times a second. A fresh FloatArray(4) each time would emit every one of them.
        assertSame(AURORA_SILENT_BANDS, AURORA_SILENT_BANDS)
        assertEquals(AuroraAnalyser.BANDS, AURORA_SILENT_BANDS.size)
        assertTrue(AURORA_SILENT_BANDS.all { it == 0f })
    }
}
