package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The TTS playback path has no live tap- the whole file is decoded up front, so its bands come from
 * [com.aiblackbox.portal.ui.components.aurora.AuroraOfflineBands], fed sample by sample straight
 * out of the decode loop. The two pieces that makes safe are tested here; the MediaCodec loop that
 * drives them is not unit-testable and is deliberately kept to plumbing.
 *
 * [MonoDownmixer] exists because Goertzel probes a SINGLE stream- feeding it interleaved stereo
 * would read every frequency at double, folding the top band into nonsense. The RMS envelope beside
 * it does not care and is left alone.
 *
 * [AudioEnvelope.bandStream] exists so a container that lies about its sample rate costs the clip
 * its BANDS and not its whole envelope. Clip length is not a factor at all any more — the band pass
 * holds windows, never the clip (see AuroraAnalyserTest for the long-clip proof).
 */
class AudioEnvelopeBandsTest {

    private fun collect(channels: Int, samples: List<Int>): List<Int> {
        val out = ArrayList<Int>()
        val downmix = MonoDownmixer(channels) { out.add(it.toInt()) }
        for (s in samples) downmix.add(s)
        return out
    }

    // ------------------------------------------------------------- downmix

    @Test
    fun `mono audio passes through untouched`() {
        assertEquals(listOf(100, -100), collect(channels = 1, samples = listOf(100, -100)))
    }

    @Test
    fun `stereo frames are averaged into one mono sample`() {
        val mono = collect(
            channels = 2,
            samples = listOf(1000, -1000, 2000, 0),  // out of phase -> silence, then half
        )
        assertEquals(listOf(0, 1000), mono)
    }

    @Test
    fun `a frame split across two decoder buffers is still one sample`() {
        // MediaCodec hands back whole frames in practice, but the downmixer carries its partial
        // frame across calls rather than assuming that — one dropped sample would shift every
        // channel afterwards and turn the downmix into noise for the rest of the clip.
        val out = ArrayList<Int>()
        val downmix = MonoDownmixer(channels = 2) { out.add(it.toInt()) }
        downmix.add(400)
        assertEquals("a half frame is not a sample yet", emptyList<Int>(), out)
        downmix.add(600)
        assertEquals(listOf(500), out)
    }

    @Test
    fun `a nonsense channel count is treated as mono rather than dividing by zero`() {
        assertEquals(listOf(7), collect(channels = 0, samples = listOf(7)))
    }

    // -------------------------------------------------------- the rate guard

    @Test
    fun `a bogus sample rate skips the band pass instead of failing the decode`() {
        // AuroraOfflineBands `require`s a positive rate; letting that throw would propagate out of
        // decode() and cost the clip its RMS envelope too, i.e. a dead ribbon over audible audio.
        assertNull(AudioEnvelope.bandStream(0, windowMs = 20))
        assertNull(AudioEnvelope.bandStream(-44100, windowMs = 20))
    }

    @Test
    fun `an ordinary rate gets a band pass whatever the clip length`() {
        // No duration argument here and none anywhere else: the band pass is length-independent by
        // construction now. If a budget/cap parameter ever comes back, this test is the tripwire.
        assertNotNull(AudioEnvelope.bandStream(44100, windowMs = 20))
        assertNotNull(AudioEnvelope.bandStream(8000, windowMs = 20))
    }

    // ------------------------------------------ the DECODER's format, not the container's

    /*
     * [DecodedPcmPass] is the piece that lets decode() build its windowing, its downmix stride and
     * its probe frequencies from ONE format — the decoder's reported output format, which may not be
     * the container's (HE-AAC SBR+PS: the track says 22050 Hz mono, the decoder emits 44100 Hz
     * stereo). The MediaCodec loop that chooses which format to hand over is not unit-testable; that
     * the pass honours whatever it is given is.
     */

    @Test
    fun `the RMS window is sized from the format the pass is built with`() {
        // Interleaved: the decode loop counts every channel, so a stereo window is frames x 2.
        assertEquals(882 * 2, DecodedPcmPass(44100, 2, windowMs = 20).samplesPerWindow)
        assertEquals(441, DecodedPcmPass(22050, 1, windowMs = 20).samplesPerWindow)
    }

    @Test
    fun `stereo PCM is downmixed on the format's stride before the band pass sees it`() {
        // 8 kHz x 2ch at 20 ms = 160 MONO samples per window. Three windows' worth of interleaved
        // stereo must yield three windows, not six: de-interleaving on the wrong stride doubles the
        // window count, which is exactly how a wrong channel count detaches the band envelope from
        // the playback position for the whole clip.
        val pass = DecodedPcmPass(8000, 2, windowMs = 20)
        repeat(160 * 2 * 3) { pass.addSample(0) }
        assertEquals(3, pass.finish().size)
    }

    @Test
    fun `a bogus rate costs the pass its bands and not its window`() {
        val pass = DecodedPcmPass(0, 1, windowMs = 20)
        assertEquals("a 0 window would make every sample its own window", 1, pass.samplesPerWindow)
        assertEquals(0, pass.finish().size)
    }
}
