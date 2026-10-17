package com.aiblackbox.portal.ui.components.aurora

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * The AI playback ribbon's LEAD, and the bound on how much of it can be fixed.
 *
 * THE DEFECT. The playback drain hands a chunk to `AudioTrack.write` and analyses it once the write
 * returns — but a MODE_STREAM write returns as soon as the bytes have been COPIED into the track's
 * own buffer, not when they are heard. That buffer holds a sixth of a second at the 16 KB floor and
 * a third at twice that, so the blue ribbon has been drawing audio the speaker had not played yet.
 * It is the same measurement error the retirement tick already documents at the other end of the
 * utterance, and this is the front end of it.
 *
 * THE FIX AND ITS BOUND. Frames wait here until the track's own playback position reaches the audio
 * they describe. `AudioTrack.getTimestamp` is the position, and it is documented BEST-EFFORT: it can
 * refuse (returning false), and right after `play()` it is stale by construction. So a frame's wait
 * is bounded by arithmetic that needs no timestamp at all — the track cannot be holding more than
 * its own buffer, so audio written more than one buffer ago IS audible. A device whose timestamp
 * never works therefore lands on today's behaviour offset by exactly one buffer, never on a ribbon
 * frozen behind a clock that stopped.
 */
class PlaybackDelayLineTest {

    /** 16 KB mono PCM16 — the buffer floor `initAudioPlayback` builds the AudioTrack to. */
    private val bufferSamples = 8192

    private val rate = 24000

    /** One hop of the 24 kHz playback tap, in samples. */
    private val hop = 480L

    private fun frame(offset: Long) = AnalysisFrame(
        sampleOffset = offset,
        atNs = 0L,
        bands = FloatArray(AuroraAnalyser.BANDS),
        bins = FloatArray(AuroraAnalyser.BINS),
    )

    /**
     * One chunk through the drain's real sequence: the track accepts bytes, then the tap turns the
     * same bytes into a frame. Returns the frame, so a test can assert on identity.
     */
    private fun PlaybackDelayLine.write(endOffset: Long): AnalysisFrame {
        accepted(hop.toInt() * 2)
        val f = frame(endOffset)
        offer(f)
        return f
    }

    @Test
    fun `a frame is held until the audio it describes has actually been rendered`() {
        val line = PlaybackDelayLine(bufferSamples)
        val first = line.write(hop)

        // The write has returned and the tap has produced its frame, but the track has rendered
        // nothing yet: this is precisely the instant the shipped code publishes, and it is wrong.
        assertNull(line.release(rendered = 0L))

        assertSame(first, line.release(rendered = hop))
    }

    @Test
    fun `nothing is delayed by more than the track's own buffer, timestamp or no timestamp`() {
        val line = PlaybackDelayLine(bufferSamples)
        val first = line.write(hop)

        // A device whose getTimestamp always refuses. While less than a buffer has been written the
        // track really could still be holding all of it, so holding is correct...
        while (line.acceptedSamples < bufferSamples) line.write(line.acceptedSamples + hop)
        assertNull(line.release(rendered = null))

        // ...but one hop past a full buffer, the first hop cannot still be unplayed.
        line.write(line.acceptedSamples + hop)
        assertSame(first, line.release(rendered = null))
    }

    @Test
    fun `a timestamp that is stale at zero cannot hold the ribbon behind a stopped clock`() {
        val line = PlaybackDelayLine(bufferSamples)
        val first = line.write(hop)

        // getTimestamp answering, but pinned at the position it had when play() was called.
        while (line.acceptedSamples < bufferSamples) line.write(line.acceptedSamples + hop)
        assertNull(line.release(rendered = 0L))

        line.write(line.acceptedSamples + hop)
        assertSame(first, line.release(rendered = 0L))
    }

    @Test
    fun `a working timestamp releases on the real position, not the worst case`() {
        val line = PlaybackDelayLine(bufferSamples)
        val first = line.write(hop)

        // Only one hop written, so the clamp alone would hold this for a whole buffer. The device
        // says it has already played it — that is the whole reason to ask.
        assertSame(first, line.release(rendered = hop))
    }

    @Test
    fun `only the newest reached frame is published and the ones it overtook are dropped`() {
        val line = PlaybackDelayLine(bufferSamples)
        repeat(5) { line.write((it + 1) * hop) }
        val newest = line.write(6 * hop)

        // The consumer is a conflating StateFlow read by a frame loop: publishing the five frames
        // the position swept past would repaint five times and leave the sixth on screen anyway.
        assertSame(newest, line.release(rendered = 6 * hop))
        assertNull(line.release(rendered = 6 * hop))
    }

    @Test
    fun `a frame the position has not reached waits for the next poll`() {
        val line = PlaybackDelayLine(bufferSamples)
        val first = line.write(hop)
        val second = line.write(2 * hop)

        assertSame(first, line.release(rendered = hop))
        assertSame(second, line.release(rendered = 2 * hop))
    }

    @Test
    fun `reset rewinds the accepted counter with the frames, because flush rewinds the track`() {
        val line = PlaybackDelayLine(bufferSamples)
        repeat(50) { line.write((it + 1) * hop) }
        line.release(rendered = 50 * hop)

        // Barge-in: AudioTrack.flush() discards what the track holds AND zeroes its playback
        // position. Dropping the pending frames alone is not enough — a counter left at the old
        // utterance's end puts the no-timestamp floor tens of thousands of samples in the FUTURE,
        // and the next utterance's first frame would publish before a sample of it had played.
        line.reset()

        val next = line.write(hop)
        assertNull(line.release(rendered = 0L))
        assertSame(next, line.release(rendered = hop))
    }

    @Test
    fun `a track with no measurable buffer publishes immediately, exactly as before`() {
        // The degenerate case must degrade to the shipped behaviour rather than to a stall.
        val line = PlaybackDelayLine(0)
        val first = line.write(hop)

        assertSame(first, line.release(rendered = null))
    }

    @Test
    fun `the rendered position is extrapolated from the timestamp's own instant`() {
        // AudioTimestamp is a (position, when) PAIR and the `when` can be hundreds of milliseconds
        // old — reading framePosition as though it were current is how a best-effort clock turns
        // into a half-buffer of extra delay.
        val ts = 5_000_000_000L
        val rendered = PlaybackDelayLine.renderedAt(
            framePosition = 1_000L, timestampNs = ts, nowNs = ts + 500_000_000L, sampleRate = rate,
        )

        assertEquals(1_000L + 12_000L, rendered)
    }

    @Test
    fun `the playback head position is reinterpreted as unsigned, never as a rewind`() {
        // `AudioTrack.getPlaybackHeadPosition` is an Int that the platform documents as UNSIGNED:
        // the frame after 0x7FFFFFFF is (int) 0x80000000. Read as a signed Kotlin Int it flips to
        // about minus two billion, which would look like the speaker rewinding a whole day and
        // would hold every subsequent frame until the buffer clamp bailed it out.
        assertEquals(0x8000_0000L, PlaybackDelayLine.headPositionFrames(Int.MIN_VALUE))
        assertEquals(0xFFFF_FFFFL, PlaybackDelayLine.headPositionFrames(-1))
        assertEquals(24_000L, PlaybackDelayLine.headPositionFrames(24_000))
    }

    @Test
    fun `a timestamp stamped in the future never rewinds the rendered position`() {
        val ts = 5_000_000_000L
        val rendered = PlaybackDelayLine.renderedAt(
            framePosition = 1_000L, timestampNs = ts, nowNs = ts - 1_000_000L, sampleRate = rate,
        )

        assertEquals(1_000L, rendered)
    }
}
