package com.aiblackbox.portal.ui.components.aurora

/**
 * Holds PLAYBACK [AnalysisFrame]s until the audio they describe has actually left the speaker.
 *
 * WHY ONLY PLAYBACK. A microphone frame is already in the past when it is measured — the sound
 * happened, then the hardware handed it over. Playback runs the other way: the drain hands a chunk
 * to `AudioTrack.write`, which on a MODE_STREAM track returns as soon as the bytes are COPIED into
 * the track's own buffer, and the ribbon has been drawn from that instant. The track holds up to a
 * whole buffer — a sixth of a second at the 16 KB floor, a third at twice that — so the blue ribbon
 * has been leading the sound by up to that much. This is the front end of the same measurement
 * error the retirement tick already documents at the back end of an utterance.
 *
 * WHAT IT MEASURES AGAINST, AND WHY THE COUNTER LIVES HERE. [AudioAnalysisTap] offsets count the
 * samples fed to the tap, and the producer feeds it exactly the bytes the track accepted, so a frame
 * at offset N is audible once the track has rendered N samples. That makes [acceptedSamples] and the
 * offsets two views of ONE position, and they have to be rewound together: `AudioTrack.flush()`
 * discards what the track holds AND zeroes its playback position, so a counter left at the last
 * utterance's end would put the fallback floor tens of thousands of samples in the future and
 * publish the next utterance before a sample of it had played. Keeping the counter inside the object
 * whose [reset] zeroes it makes that structural instead of a rule someone has to remember.
 *
 * THE BOUND, WHICH IS WHY A BEST-EFFORT CLOCK IS SAFE HERE. `AudioTrack.getTimestamp` is documented
 * best-effort: it can refuse outright, and right after `play()` it is stale by construction. Trust
 * alone would mean a device with a broken timestamp gets a ribbon frozen behind a clock that
 * stopped. So the audible position is the LATER of what the device claims and what arithmetic
 * guarantees — the track cannot be holding more than [bufferSamples], so audio accepted longer ago
 * than that IS audible. A refusing device lands on today's behaviour delayed by exactly one buffer;
 * a working one gets the real position. Neither can stall.
 *
 * Pure Kotlin (no android.*) — the AudioTimestamp pair is reduced to two longs by [renderedAt] at
 * the call site, so every rule above is asserted on the JVM in PlaybackDelayLineTest.
 *
 * NOT thread-safe, for the same reason as the tap that feeds it (D1): one line per playback stream,
 * driven from that stream's drain thread.
 *
 * @param bufferSamples the AudioTrack's own buffer, in frames — the worst case a write can be
 *   ahead of the speaker, and therefore the longest anything here may be held.
 */
class PlaybackDelayLine(private val bufferSamples: Int) {

    private val pending = ArrayDeque<AnalysisFrame>()

    /**
     * Frames the track has accepted since the last [reset] — the same position the tap's offsets
     * count in, and the only input the no-timestamp floor needs.
     */
    var acceptedSamples: Long = 0L
        private set

    /**
     * Record audio the track took. PCM16 mono, in BYTES, so this takes the same number the caller
     * hands [AudioAnalysisTap.offer] for the same chunk — the two counters cannot drift while they
     * are fed the one value.
     */
    fun accepted(lenBytes: Int) {
        if (lenBytes > 0) acceptedSamples += lenBytes / 2
    }

    /** Take a frame the tap produced from audio just accepted by the track. Offsets ascend. */
    fun offer(frame: AnalysisFrame) {
        pending.addLast(frame)
    }

    /**
     * The newest held frame whose audio has now been rendered, or null if none has.
     *
     * Frames the position swept PAST are dropped rather than returned: the consumer is a conflating
     * `StateFlow` read by a frame loop, so publishing all of them would repaint several times and
     * leave the newest on screen anyway. What this returns is "the frame at the speaker right now",
     * which is the one thing worth drawing.
     *
     * @param rendered the track's own playback position in frames, or null when it will not say.
     */
    fun release(rendered: Long?): AnalysisFrame? {
        val audible = maxOf(rendered ?: Long.MIN_VALUE, acceptedSamples - bufferSamples)
        var newest: AnalysisFrame? = null
        while (pending.isNotEmpty() && pending.first().sampleOffset <= audible) {
            newest = pending.removeFirst()
        }
        return newest
    }

    /**
     * Rewind to an empty stream at position zero: the frames a flushed or released track will never
     * play, and the counter they were measured against. See the counters note above for why both.
     */
    fun reset() {
        pending.clear()
        acceptedSamples = 0L
    }

    companion object {

        /**
         * Where the playback position has reached NOW, from an `AudioTimestamp`'s (position, when).
         *
         * The pair is a reading with a timestamp, not a current value — the `when` can be hundreds
         * of milliseconds old on a real device, and reading `framePosition` as though it were
         * current would turn a best-effort clock into half a buffer of extra delay. Extrapolating
         * can only ever release EARLIER, and [release]'s floor already stops a wild reading from
         * being the reason a frame waits, so the error this can introduce is bounded by the same
         * arithmetic that bounds the missing-clock case.
         *
         * A `now` BEFORE the stamp (a reading taken across the two clock reads) extrapolates by
         * zero rather than backwards.
         */
        fun renderedAt(framePosition: Long, timestampNs: Long, nowNs: Long, sampleRate: Int): Long {
            val elapsedNs = maxOf(0L, nowNs - timestampNs)
            return framePosition + elapsedNs * sampleRate / 1_000_000_000L
        }

        /**
         * `AudioTrack.getPlaybackHeadPosition`'s Int, widened the way the platform documents it:
         * UNSIGNED, so the frame after 0x7FFFFFFF is (int) 0x80000000. It is the always-available
         * rung below the best-effort timestamp — coarser (it is not corrected for the device's own
         * output latency) but never absent, which matters because the buffer clamp alone can never
         * release the LAST buffer of an utterance: nothing more is accepted after it, so the floor
         * stops advancing and the model's closing word would never be drawn.
         *
         * Read as a signed Int it would flip to about minus two billion mid-session and look like
         * the speaker rewinding a day.
         */
        fun headPositionFrames(rawHeadPosition: Int): Long = rawHeadPosition.toLong() and 0xFFFF_FFFFL
    }
}
