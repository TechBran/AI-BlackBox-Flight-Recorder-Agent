package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraBins
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.exp

/**
 * The shared [WaveformFeed] data layer — the history ring, the time-based
 * decimation (60-vs-120 Hz independence), the empty/NaN safety, and the ONE
 * pinned definition of a speaker's overall level. Pure JVM, in the aurora
 * tradition: everything a renderer reads is a pure function of the inputs, so it
 * is asserted here without a device.
 */
class WaveformFeedTest {

    private fun human(vararg bands: Float) = AuroraVoice(AuroraSpeaker.HUMAN, bands)
    private fun ai(vararg bands: Float) = AuroraVoice(AuroraSpeaker.AI, bands)

    private fun bins(speaker: AuroraSpeaker, vararg v: Float) =
        AuroraBins(speaker, FloatArray(AuroraAnalyser.BINS) { v.getOrElse(it) { 0f } })

    // ------------------------------------------------------------ the ring

    @Test
    fun ring_is_newest_first_and_empty_safe() {
        val ring = EnvelopeRing(4)
        // Empty ring: size 0, every read is 0f, nothing throws.
        assertEquals(0, ring.size)
        assertEquals(0f, ring.newest(0), 0f)
        assertEquals(0f, ring.newest(5), 0f)
        assertEquals(0f, ring.newest(-1), 0f)

        ring.push(0.1f); ring.push(0.2f); ring.push(0.3f)
        assertEquals(3, ring.size)
        // newest-first: 0 is the last pushed.
        assertEquals(0.3f, ring.newest(0), 1e-6f)
        assertEquals(0.2f, ring.newest(1), 1e-6f)
        assertEquals(0.1f, ring.newest(2), 1e-6f)
        // past the filled tail → 0, not a throw.
        assertEquals(0f, ring.newest(3), 0f)
    }

    @Test
    fun ring_wraps_and_drops_oldest() {
        val ring = EnvelopeRing(3)
        // In-range distinct values so ordering (not clamping) is what is tested.
        ring.push(0.1f); ring.push(0.2f); ring.push(0.3f); ring.push(0.4f) // 0.1 falls off
        assertEquals(3, ring.size)
        assertEquals(0.4f, ring.newest(0), 1e-6f)
        assertEquals(0.3f, ring.newest(1), 1e-6f)
        assertEquals(0.2f, ring.newest(2), 1e-6f)
        assertEquals(0f, ring.newest(3), 0f) // the dropped 0.1 is gone, tail reads 0
    }

    @Test
    fun ring_clamps_and_sanitises_on_push() {
        val ring = EnvelopeRing(3)
        ring.push(Float.NaN)   // NaN → 0
        ring.push(5f)          // > 1 → 1
        ring.push(-2f)         // < 0 → 0
        assertEquals(0f, ring.newest(2), 0f)
        assertEquals(1f, ring.newest(1), 0f)
        assertEquals(0f, ring.newest(0), 0f)
    }

    @Test
    fun ring_clear_resets() {
        val ring = EnvelopeRing(3)
        ring.push(0.5f); ring.push(0.6f)
        ring.clear()
        assertEquals(0, ring.size)
        assertEquals(0f, ring.newest(0), 0f)
    }

    // -------------------------------------------------- level combination

    @Test
    fun level_is_the_pinned_weighted_max() {
        // A single lit band lights the level (weighted MAX, not sum/average).
        assertEquals(0.5f, WaveformFeed.levelOf(human(0.5f, 0f, 0f, 0f)), 1e-6f)
        // Upper bands are weighted DOWN: band 2 * 0.85, band 3 * 0.70.
        assertEquals(0.85f, WaveformFeed.levelOf(human(0f, 0f, 1f, 0f)), 1e-6f)
        assertEquals(0.70f, WaveformFeed.levelOf(human(0f, 0f, 0f, 1f)), 1e-6f)
        // Lower bands full weight; the max wins across bands.
        assertEquals(0.20f, WaveformFeed.levelOf(human(0.2f, 0.2f, 0.2f, 0.2f)), 1e-6f)
        assertEquals(0.9f, WaveformFeed.levelOf(human(0.9f, 0.1f, 1f, 0f)), 1e-6f) // max(0.9, 0.1, 0.85, 0)
        // Null / NaN are safe: null → 0, NaN band → 0 (via AuroraVoice.band).
        assertEquals(0f, WaveformFeed.levelOf(null), 0f)
        assertEquals(0f, WaveformFeed.levelOf(human(Float.NaN, Float.NaN, Float.NaN, Float.NaN)), 0f)
        assertTrue(WaveformFeed.levelOf(human(0.5f, 0f, 0f, 0f)) in 0f..1f)
    }

    @Test
    fun weights_are_the_pinned_values() {
        // The weights themselves are a contract other styles rely on — pin them.
        assertEquals(4, WaveformFeed.LEVEL_WEIGHTS.size)
        assertEquals(1.0f, WaveformFeed.LEVEL_WEIGHTS[0], 0f)
        assertEquals(1.0f, WaveformFeed.LEVEL_WEIGHTS[1], 0f)
        assertEquals(0.85f, WaveformFeed.LEVEL_WEIGHTS[2], 0f)
        assertEquals(0.70f, WaveformFeed.LEVEL_WEIGHTS[3], 0f)
    }

    // ---------------------------------------------- instantaneous readers

    @Test
    fun instantaneous_readers_reflect_the_last_step() {
        val feed = WaveformFeed()
        // Nothing stepped yet: absent, zero.
        assertFalse(feed.present(AuroraSpeaker.HUMAN))
        assertEquals(0f, feed.level(AuroraSpeaker.HUMAN), 0f)
        assertEquals(0f, feed.band(AuroraSpeaker.AI, 0), 0f)

        feed.step(0.02f, listOf(human(0.4f, 0f, 0f, 0f)))
        assertTrue(feed.present(AuroraSpeaker.HUMAN))
        assertFalse(feed.present(AuroraSpeaker.AI))
        assertEquals(0.4f, feed.level(AuroraSpeaker.HUMAN), 1e-6f)
        assertEquals(0.4f, feed.band(AuroraSpeaker.HUMAN, 0), 1e-6f)
        // AI absent → 0 for band reads.
        assertEquals(0f, feed.band(AuroraSpeaker.AI, 0), 0f)
    }

    // --------------------------------------------- time-based decimation

    @Test
    fun decimation_fills_at_cadence_regardless_of_frame_rate() {
        // 2 seconds of a constant human voice, once at 60 Hz and once at 120 Hz.
        // The rings must fill to the SAME depth (refresh-rate independence) — the
        // whole reason the ring is time-decimated instead of per-frame.
        val voice = listOf(human(0.5f, 0f, 0f, 0f))

        val feed60 = WaveformFeed()
        repeat(120) { feed60.step(1f / 60f, voice) }   // 120 frames * (1/60) = 2.0 s

        val feed120 = WaveformFeed()
        repeat(240) { feed120.step(1f / 120f, voice) } // 240 frames * (1/120) = 2.0 s

        val ring60 = feed60.history(AuroraSpeaker.HUMAN)
        val ring120 = feed120.history(AuroraSpeaker.HUMAN)

        // ~50 Hz over 2 s ≈ 100 samples (well under the 150 capacity); allow a
        // single-sample float-accumulation slop between the two paths.
        assertTrue("expected ~100 samples, was ${ring60.size}", ring60.size in 98..101)
        assertTrue(
            "60 Hz (${ring60.size}) and 120 Hz (${ring120.size}) rings must match within 1",
            abs(ring60.size - ring120.size) <= 1,
        )
        // Constant level → every sample equal, and equal across the two paths.
        val overlap = minOf(ring60.size, ring120.size)
        for (i in 0 until overlap) {
            assertEquals(0.5f, ring60.newest(i), 1e-6f)
            assertEquals(ring60.newest(i), ring120.newest(i), 1e-6f)
        }
    }

    @Test
    fun a_dt_zero_frame_pushes_no_history() {
        val feed = WaveformFeed()
        val voice = listOf(human(0.5f, 0f, 0f, 0f))
        // dt 0 (first frame / resume / wake) updates the instantaneous readers but
        // must not advance the ring — mirror AuroraWaveform's dt-0 handling.
        feed.step(0f, voice)
        assertEquals(0, feed.history(AuroraSpeaker.HUMAN).size)
        assertEquals(0.5f, feed.level(AuroraSpeaker.HUMAN), 1e-6f) // instantaneous still set
    }

    @Test
    fun negative_and_nan_dt_are_ignored_for_history() {
        val feed = WaveformFeed()
        val voice = listOf(human(0.5f, 0f, 0f, 0f))
        feed.step(-1f, voice)
        feed.step(Float.NaN, voice)
        assertEquals(0, feed.history(AuroraSpeaker.HUMAN).size)
    }

    @Test
    fun history_capacity_is_bounded_by_the_window() {
        val feed = WaveformFeed()
        // Step far more than 3 s of wall clock; the ring must cap at its capacity,
        // never grow unbounded (150 = ceil(3.0 * 50)).
        repeat(2000) { feed.step(0.02f, listOf(human(0.3f, 0f, 0f, 0f))) }
        assertEquals(feed.capacity, feed.history(AuroraSpeaker.HUMAN).size)
        assertEquals(150, feed.capacity)
    }

    @Test
    fun two_voices_keep_independent_rings() {
        val feed = WaveformFeed()
        repeat(120) { feed.step(1f / 60f, listOf(human(0.6f, 0f, 0f, 0f), ai(0.2f, 0f, 0f, 0f))) }
        assertEquals(0.6f, feed.history(AuroraSpeaker.HUMAN).newest(0), 1e-6f)
        assertEquals(0.2f, feed.history(AuroraSpeaker.AI).newest(0), 1e-6f)
        assertTrue(feed.present(AuroraSpeaker.HUMAN))
        assertTrue(feed.present(AuroraSpeaker.AI))
    }

    // =========================================================================
    // THE ONE SMOOTHER (v35 device feedback: "twitching", "isn't fluent")
    //
    // Measured on the real tap: a band's frame-to-frame step on room tone is 44%
    // of its own median (0.0257 against p50 0.0578) and its direction reverses on
    // 65-72% of frames — i.i.d. coin-flip noise is 66.7%, so essentially every
    // frame the picture changes its mind. Eight of the ten styles apply no
    // temporal smoothing of their own, which is why the filter belongs HERE, once,
    // rather than nine times over.
    //
    // These tests MEASURE the jitter (reversal fraction, mean step) rather than
    // asserting the filter merely exists, and pin the responsiveness that a
    // too-heavy filter would quietly trade away.
    // =========================================================================

    /**
     * Fraction of samples where the direction of travel FLIPPED. 0.667 is i.i.d.
     * noise (the measured raw path); a coherent, flowing signal is far lower.
     * Zero-deltas are skipped rather than counted as a direction.
     */
    private fun reversalFraction(xs: List<Float>): Float {
        var reversals = 0
        var counted = 0
        for (i in 2 until xs.size) {
            val d1 = xs[i - 1] - xs[i - 2]
            val d2 = xs[i] - xs[i - 1]
            if (d1 == 0f || d2 == 0f) continue
            counted++
            if (d1 * d2 < 0f) reversals++
        }
        return if (counted == 0) 0f else reversals.toFloat() / counted
    }

    /** Mean |x[t] − x[t−1]| — how far the drawn value jumps between frames. */
    private fun meanStep(xs: List<Float>): Float {
        if (xs.size < 2) return 0f
        var sum = 0f
        for (i in 1 until xs.size) sum += abs(xs[i] - xs[i - 1])
        return sum / (xs.size - 1)
    }

    /**
     * The measured room-tone band: median 0.0578 with a ±0.0386 uniform jitter,
     * which reproduces the tap's measured 0.0257 mean step (44% of the median) and
     * its ~0.667 reversal fraction. Deterministic seed — this is a measurement,
     * and it has to give the same number on every run.
     */
    private fun roomToneBand(frames: Int, seed: Int = 7): List<Float> {
        val rnd = java.util.Random(seed.toLong())
        return List(frames) {
            (0.0578f + (rnd.nextFloat() * 2f - 1f) * 0.0386f).coerceIn(0f, 1f)
        }
    }

    @Test
    fun noise_direction_reversals_drop_well_below_the_iid_fraction() {
        val feed = WaveformFeed()
        val noise = roomToneBand(4000)
        val raw = ArrayList<Float>(noise.size)
        val smoothed = ArrayList<Float>(noise.size)
        for (v in noise) {
            feed.step(0.02f, listOf(human(v, 0f, 0f, 0f)))
            raw.add(feed.rawBand(AuroraSpeaker.HUMAN, 0))
            smoothed.add(feed.band(AuroraSpeaker.HUMAN, 0))
        }

        val rawRev = reversalFraction(raw)
        val smoothRev = reversalFraction(smoothed)
        // The input really is the measured twitch: ~0.667 reversals, ~0.026 step.
        assertTrue("raw input must reproduce the measured twitch, was $rawRev", rawRev > 0.60f)
        assertEquals("raw mean step", 0.0257f, meanStep(raw), 0.004f)
        // THE FIX: direction reversals a quarter below the i.i.d. 0.667 (measured
        // 0.473-0.487 across seeds at the shipped 0.120 s release).
        assertTrue("smoothed reversal fraction $smoothRev must be well below 0.667", smoothRev < 0.52f)
        // And the frame-to-frame jump shrinks by more than 3x (measured 4.5x).
        val shrink = meanStep(raw) / meanStep(smoothed)
        assertTrue("mean step only shrank ${shrink}x", shrink > 3f)
        // A filter that also flattens the signal away would be a different bug: an
        // attack/release follower rides the peaks, so the mean rises slightly.
        val rawMean = raw.average().toFloat()
        val smoothMean = smoothed.average().toFloat()
        assertTrue("smoother crushed the signal ($smoothMean vs $rawMean)", smoothMean > rawMean * 0.5f)
        assertTrue("smoother inflated the signal ($smoothMean vs $rawMean)", smoothMean < rawMean * 1.5f)
    }

    @Test
    fun the_level_channel_is_smoothed_the_same_way_the_bands_are() {
        val feed = WaveformFeed()
        val noise = roomToneBand(2000, seed = 11)
        val raw = ArrayList<Float>(noise.size)
        val smoothed = ArrayList<Float>(noise.size)
        for (v in noise) {
            feed.step(0.02f, listOf(human(v, v * 0.5f, 0f, 0f)))
            raw.add(feed.rawLevel(AuroraSpeaker.HUMAN))
            smoothed.add(feed.level(AuroraSpeaker.HUMAN))
        }
        assertTrue(reversalFraction(raw) > 0.60f)
        assertTrue(reversalFraction(smoothed) < 0.52f)
        assertTrue(meanStep(raw) / meanStep(smoothed) > 3f)
    }

    @Test
    fun the_bin_channel_is_smoothed_the_same_way() {
        val feed = WaveformFeed()
        val noise = roomToneBand(2000, seed = 13)
        val raw = ArrayList<Float>(noise.size)
        val smoothed = ArrayList<Float>(noise.size)
        for (v in noise) {
            feed.step(0.02f, listOf(human(0.3f, 0f, 0f, 0f)), listOf(bins(AuroraSpeaker.HUMAN, v)))
            raw.add(feed.rawBin(AuroraSpeaker.HUMAN, 0))
            smoothed.add(feed.bin(AuroraSpeaker.HUMAN, 0))
        }
        assertTrue(reversalFraction(raw) > 0.60f)
        assertTrue(reversalFraction(smoothed) < 0.52f)
        assertTrue(meanStep(raw) / meanStep(smoothed) > 3f)
    }

    /**
     * The RATIO channels — MirroredPulse.bandShare, Cinematic.brightness,
     * SharpLineMath.brightness, ClassicMath.bandLayerShare, RibbonMath.centroid /
     * spread — are all a normalised combination of the four band drives, so a
     * steadied outline over four still-boiling bands would just move the twitch
     * into the colour. The share is computed here exactly as bandShare computes it
     * (b / Σb) so the coverage claim is measured, not assumed.
     */
    @Test
    fun the_ratio_channels_stop_boiling_because_the_bands_they_derive_from_are_smoothed() {
        val feed = WaveformFeed()
        val a = roomToneBand(2000, seed = 17)
        val b = roomToneBand(2000, seed = 23)
        val c = roomToneBand(2000, seed = 29)
        val d = roomToneBand(2000, seed = 31)
        val rawShare = ArrayList<Float>(a.size)
        val smoothShare = ArrayList<Float>(a.size)
        fun share(b0: Float, b1: Float, b2: Float, b3: Float): Float {
            val total = b0 + b1 + b2 + b3
            return if (total <= 1e-6f) 0.25f else b0 / total
        }
        for (i in a.indices) {
            feed.step(0.02f, listOf(human(a[i], b[i], c[i], d[i])))
            rawShare.add(
                share(
                    feed.rawBand(AuroraSpeaker.HUMAN, 0),
                    feed.rawBand(AuroraSpeaker.HUMAN, 1),
                    feed.rawBand(AuroraSpeaker.HUMAN, 2),
                    feed.rawBand(AuroraSpeaker.HUMAN, 3),
                ),
            )
            smoothShare.add(
                share(
                    feed.band(AuroraSpeaker.HUMAN, 0),
                    feed.band(AuroraSpeaker.HUMAN, 1),
                    feed.band(AuroraSpeaker.HUMAN, 2),
                    feed.band(AuroraSpeaker.HUMAN, 3),
                ),
            )
        }
        // A ratio of four independently-noisy channels is the WORST case for the
        // reversal count — numerator and denominator both wander, so it only falls
        // from 0.678 to 0.538 where a single channel falls to 0.48. What collapses
        // is the SIZE of each move: 5.9x smaller, and boil is amplitude × rate.
        assertTrue("the raw share really does boil", reversalFraction(rawShare) > 0.60f)
        val rev = reversalFraction(smoothShare)
        assertTrue("the smoothed share reverses as often as noise ($rev)", rev < 0.58f)
        assertTrue(rev < reversalFraction(rawShare))
        assertTrue(meanStep(rawShare) / meanStep(smoothShare) > 3f)
    }

    /**
     * The history rings — what MirroredPulse.bandShare, Cinematic.brightness and
     * SharpLineMath.brightness actually read (they take bandHistory, not the
     * instantaneous band) — carry the SMOOTHED stream, which is the only reason
     * smoothing at the feed reaches them at all.
     */
    @Test
    fun the_history_rings_carry_the_smoothed_stream() {
        val feed = WaveformFeed()
        // Arrive quiet, then jump: the ring must hold the filtered ramp, not the step.
        feed.step(0.02f, listOf(human(0f, 0f, 0f, 0f)), listOf(bins(AuroraSpeaker.HUMAN, 0f)))
        feed.step(0.02f, listOf(human(1f, 1f, 1f, 1f)), listOf(bins(AuroraSpeaker.HUMAN, 1f)))
        val expected = WaveformFeed.smoothStep(0f, 1f, 0.02f)
        assertEquals(expected, feed.history(AuroraSpeaker.HUMAN).newest(0), 1e-6f)
        assertEquals(expected, feed.bandHistory(AuroraSpeaker.HUMAN, 0).newest(0), 1e-6f)
        assertEquals(expected, feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(0), 1e-6f)
        // The published sample IS the live smoothed value at the instant it was
        // pushed, so lerp(published, live, subFrame) is continuous across a push.
        assertEquals(feed.level(AuroraSpeaker.HUMAN), feed.publishedLevel(AuroraSpeaker.HUMAN), 1e-6f)
        assertEquals(feed.band(AuroraSpeaker.HUMAN, 0), feed.publishedBand(AuroraSpeaker.HUMAN, 0), 1e-6f)
        assertEquals(feed.bin(AuroraSpeaker.HUMAN, 0), feed.publishedBin(AuroraSpeaker.HUMAN, 0), 1e-6f)
    }

    // ------------------------------------------------------- filter shape

    @Test
    fun the_filter_is_the_shared_exponential_approach_with_an_asymmetric_tau() {
        // The same idiom Aurora and ParticleWave use: current + (target − current)·(1 − e^(−dt/τ)).
        assertEquals(0.020f, WaveformFeed.ATTACK_TAU_SEC, 0f)
        assertEquals(0.120f, WaveformFeed.RELEASE_TAU_SEC, 0f)
        assertTrue(WaveformFeed.RELEASE_TAU_SEC > WaveformFeed.ATTACK_TAU_SEC)

        val feed = WaveformFeed()
        feed.step(0.02f, listOf(human(0f, 0f, 0f, 0f)))
        // RISE: one frame of attack over dt = τ ⇒ 63.2% of the way.
        feed.step(0.02f, listOf(human(1f, 0f, 0f, 0f)))
        val afterAttack = feed.band(AuroraSpeaker.HUMAN, 0)
        assertEquals(1f - exp(-1f), afterAttack, 1e-6f)
        // FALL: the same dt is only 1 − e^(−dt/τ_release) = 15% of the way down.
        feed.step(0.02f, listOf(human(0f, 0f, 0f, 0f)))
        assertEquals(
            afterAttack * exp(-0.02f / WaveformFeed.RELEASE_TAU_SEC),
            feed.band(AuroraSpeaker.HUMAN, 0),
            1e-6f,
        )
    }

    @Test
    fun the_filter_composes_exactly_over_dt_so_60_and_120_hz_agree() {
        // Aurora C3: two half steps land where one whole step lands, so the same
        // wall clock of audio leaves the smoother in the same place at any refresh.
        val voices = listOf(human(0.8f, 0.4f, 0.2f, 0.1f))
        val start = listOf(human(0f, 0f, 0f, 0f))

        // Deliberately sampled MID-transient (50 ms into a 20 ms-τ attack, ~92% of
        // the way): saturated values would agree whatever the filter did.
        val feed60 = WaveformFeed()
        feed60.step(1f / 60f, start)
        repeat(3) { feed60.step(1f / 60f, voices) }

        val feed120 = WaveformFeed()
        feed120.step(1f / 120f, start)
        repeat(6) { feed120.step(1f / 120f, voices) }

        assertTrue(feed60.band(AuroraSpeaker.HUMAN, 0) < 0.8f) // still climbing
        for (b in 0 until 4) {
            assertEquals(
                "band $b must land in the same place at 60 and 120 Hz",
                feed60.band(AuroraSpeaker.HUMAN, b),
                feed120.band(AuroraSpeaker.HUMAN, b),
                1e-6f,
            )
        }
        assertEquals(
            feed60.level(AuroraSpeaker.HUMAN),
            feed120.level(AuroraSpeaker.HUMAN),
            1e-6f,
        )
    }

    @Test
    fun a_step_input_still_reaches_90_percent_within_three_attack_taus() {
        // THE RESPONSIVENESS GUARD. 90% of an exponential is ln(10) = 2.303 τ, i.e.
        // 46 ms at τ = 20 ms; the first whole frame past that is 60 ms. A filter
        // that misses this has traded the operator's "not much movement" complaint
        // for a worse one.
        for (fps in intArrayOf(60, 120)) {
            val dt = 1f / fps
            val feed = WaveformFeed()
            feed.step(dt, listOf(human(0f, 0f, 0f, 0f)))
            var elapsed = 0f
            while (elapsed < WaveformFeed.ATTACK_TAU_SEC * 3f) {
                feed.step(dt, listOf(human(1f, 1f, 1f, 1f)))
                elapsed += dt
            }
            assertTrue(
                "at $fps Hz a step only reached ${feed.band(AuroraSpeaker.HUMAN, 0)} in 60 ms",
                feed.band(AuroraSpeaker.HUMAN, 0) >= 0.90f,
            )
            assertTrue(feed.level(AuroraSpeaker.HUMAN) >= 0.90f)
        }
    }

    // ------------------------------------------------- arrival / departure

    @Test
    fun a_speaker_snaps_to_its_first_measurement_rather_than_ramping_in() {
        // Absent → present is the START of a stream, not a change within one: there
        // is nothing to filter against, and ramping would mean every style opened
        // with a swell the microphone never heard. This is also what keeps a
        // single-step read (every "step once and assert" test in the tree) exact.
        val feed = WaveformFeed()
        feed.step(0.02f, listOf(human(0.7f, 0.5f, 0.3f, 0.1f)), listOf(bins(AuroraSpeaker.HUMAN, 0.9f)))
        assertEquals(0.7f, feed.band(AuroraSpeaker.HUMAN, 0), 1e-6f)
        assertEquals(0.7f, feed.level(AuroraSpeaker.HUMAN), 1e-6f)
        assertEquals(0.9f, feed.bin(AuroraSpeaker.HUMAN, 0), 1e-6f)
        // A dt-0 first frame (composition/resume/wake) snaps too — the filter cannot
        // advance on dt 0, so without the snap the whole first frame would read 0.
        val fresh = WaveformFeed()
        fresh.step(0f, listOf(human(0.6f, 0f, 0f, 0f)))
        assertEquals(0.6f, fresh.level(AuroraSpeaker.HUMAN), 1e-6f)
    }

    @Test
    fun a_departed_speaker_drops_to_silence_at_once_and_re_arrives_snapped() {
        // Absence is the END of the stream, not a quiet measurement: releasing over
        // it would hold a departed voice's tail alive in every history ring and
        // deadlock the idle-park gates that watch those rings drain.
        val feed = WaveformFeed()
        val loud = listOf(human(0.9f, 0.9f, 0.9f, 0.9f))
        val spectra = listOf(bins(AuroraSpeaker.HUMAN, 0.8f))
        repeat(20) { feed.step(0.02f, loud, spectra) }
        feed.step(0.02f, emptyList())
        assertEquals(0f, feed.level(AuroraSpeaker.HUMAN), 0f)
        assertEquals(0f, feed.band(AuroraSpeaker.HUMAN, 0), 0f)
        assertEquals(0f, feed.bin(AuroraSpeaker.HUMAN, 0), 0f)
        assertEquals(0f, feed.history(AuroraSpeaker.HUMAN).newest(0), 0f)
        assertEquals(0f, feed.bandHistory(AuroraSpeaker.HUMAN, 0).newest(0), 0f)
        assertEquals(0f, feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(0), 0f)
        // Coming back is an arrival: it snaps rather than ramping from the silence.
        feed.step(0.02f, listOf(human(0.4f, 0f, 0f, 0f)))
        assertEquals(0.4f, feed.level(AuroraSpeaker.HUMAN), 1e-6f)
    }

    @Test
    fun raw_accessors_report_the_measurement_the_smoothed_ones_filter() {
        val feed = WaveformFeed()
        feed.step(0.02f, listOf(human(0f, 0f, 0f, 0f)), listOf(bins(AuroraSpeaker.HUMAN, 0f)))
        feed.step(0.02f, listOf(human(1f, 0.5f, 0f, 0f)), listOf(bins(AuroraSpeaker.HUMAN, 1f)))
        // Raw is the measurement, untouched, for anything that genuinely wants it.
        assertEquals(1f, feed.rawBand(AuroraSpeaker.HUMAN, 0), 0f)
        assertEquals(0.5f, feed.rawBand(AuroraSpeaker.HUMAN, 1), 0f)
        assertEquals(1f, feed.rawLevel(AuroraSpeaker.HUMAN), 0f)
        assertEquals(1f, feed.rawBin(AuroraSpeaker.HUMAN, 0), 0f)
        // Smoothed lags it, by exactly one attack step.
        assertTrue(feed.band(AuroraSpeaker.HUMAN, 0) < feed.rawBand(AuroraSpeaker.HUMAN, 0))
        assertEquals(WaveformFeed.smoothStep(0f, 1f, 0.02f), feed.band(AuroraSpeaker.HUMAN, 0), 1e-6f)
        // Out-of-range reads stay total on both paths: bands clamp (a short voice
        // drives the remaining sheets), bins read 0 (never invent sibilance).
        assertEquals(feed.band(AuroraSpeaker.HUMAN, 3), feed.band(AuroraSpeaker.HUMAN, 99), 0f)
        assertEquals(feed.rawBand(AuroraSpeaker.HUMAN, 3), feed.rawBand(AuroraSpeaker.HUMAN, 99), 0f)
        assertEquals(0f, feed.bin(AuroraSpeaker.HUMAN, 999), 0f)
        assertEquals(0f, feed.rawBin(AuroraSpeaker.HUMAN, -1), 0f)
        // An absent speaker is zero on both.
        assertEquals(0f, feed.rawLevel(AuroraSpeaker.AI), 0f)
        assertEquals(0f, feed.rawBand(AuroraSpeaker.AI, 0), 0f)
    }

    @Test
    fun the_smoother_is_nan_safe() {
        val feed = WaveformFeed()
        feed.step(0.02f, listOf(human(0.5f, 0.5f, 0.5f, 0.5f)))
        feed.step(Float.NaN, listOf(human(Float.NaN, Float.NaN, Float.NaN, Float.NaN)))
        assertTrue(feed.level(AuroraSpeaker.HUMAN).isFinite())
        assertTrue(feed.band(AuroraSpeaker.HUMAN, 0).isFinite())
        assertTrue(feed.band(AuroraSpeaker.HUMAN, 0) in 0f..1f)
        // A NaN dt cannot advance the filter (it is not time), and a NaN band reads
        // as 0 — so this is a release toward 0 that has not moved yet.
        assertEquals(0.5f, feed.band(AuroraSpeaker.HUMAN, 0), 1e-6f)
        feed.step(0.02f, listOf(human(Float.NaN, 0f, 0f, 0f)))
        assertTrue(feed.band(AuroraSpeaker.HUMAN, 0) < 0.5f)
        assertTrue(feed.band(AuroraSpeaker.HUMAN, 0) > 0f)
    }

    @Test
    fun reset_clears_the_smoother_and_the_sub_frame_phase() {
        val feed = WaveformFeed()
        repeat(40) { feed.step(0.015f, listOf(human(0.8f, 0.8f, 0.8f, 0.8f))) }
        feed.reset()
        assertEquals(0f, feed.level(AuroraSpeaker.HUMAN), 0f)
        assertEquals(0f, feed.band(AuroraSpeaker.HUMAN, 0), 0f)
        assertEquals(0f, feed.rawLevel(AuroraSpeaker.HUMAN), 0f)
        assertEquals(0f, feed.subFrame, 0f)
        assertFalse(feed.present(AuroraSpeaker.HUMAN))
        // And the next arrival snaps, exactly as a first arrival does.
        feed.step(0.02f, listOf(human(0.3f, 0f, 0f, 0f)))
        assertEquals(0.3f, feed.level(AuroraSpeaker.HUMAN), 1e-6f)
    }

    // ------------------------------------------------------------ subFrame

    @Test
    fun sub_frame_reports_how_far_the_publish_interval_has_run() {
        // The renderer draws at 60-120 Hz off a ring that only advances at 50 Hz.
        // Without this fraction a scrolling style can only jump a whole column at a
        // time, which is the "stepping" half of the fluidity complaint.
        val feed = WaveformFeed()
        assertEquals(0f, feed.subFrame, 0f)
        val voice = listOf(human(0.5f, 0f, 0f, 0f))
        feed.step(0.01f, voice) // half of the 20 ms interval
        assertEquals(0.5f, feed.subFrame, 1e-4f)
        feed.step(0.01f, voice) // the interval completes → a push, and the phase resets
        assertEquals(1, feed.history(AuroraSpeaker.HUMAN).size)
        assertEquals(0f, feed.subFrame, 1e-4f)
        feed.step(0.005f, voice)
        assertEquals(0.25f, feed.subFrame, 1e-4f)
        // Always a usable 0..1 fraction, whatever the frame clock does.
        feed.step(Float.NaN, voice)
        assertTrue(feed.subFrame in 0f..1f)
        feed.step(10f, voice)
        assertTrue(feed.subFrame in 0f..1f)
    }

    @Test
    fun published_channels_are_the_last_pushed_sample_and_are_empty_safe() {
        val feed = WaveformFeed()
        // Nothing published yet: every published read is 0, nothing throws.
        assertEquals(0f, feed.publishedLevel(AuroraSpeaker.HUMAN), 0f)
        assertEquals(0f, feed.publishedBand(AuroraSpeaker.HUMAN, 0), 0f)
        assertEquals(0f, feed.publishedBin(AuroraSpeaker.HUMAN, 0), 0f)

        val voice = listOf(human(0.6f, 0.4f, 0.2f, 0.1f))
        val spectra = listOf(bins(AuroraSpeaker.HUMAN, 0.7f))
        repeat(50) { feed.step(0.02f, voice, spectra) }
        assertEquals(
            feed.history(AuroraSpeaker.HUMAN).newest(0),
            feed.publishedLevel(AuroraSpeaker.HUMAN),
            0f,
        )
        assertEquals(
            feed.bandHistory(AuroraSpeaker.HUMAN, 1).newest(0),
            feed.publishedBand(AuroraSpeaker.HUMAN, 1),
            0f,
        )
        assertEquals(
            feed.binHistory(AuroraSpeaker.HUMAN, 0).newest(0),
            feed.publishedBin(AuroraSpeaker.HUMAN, 0),
            0f,
        )
    }

    @Test
    fun smooth_step_is_the_public_shape_the_feed_filters_with() {
        // Exposed so a style can predict/replicate the feed's own step instead of
        // inventing a second curve. One τ of attack is 63.2% of the way there.
        assertEquals(0.632f, WaveformFeed.smoothStep(0f, 1f, WaveformFeed.ATTACK_TAU_SEC), 1e-3f)
        // dt 0 (and a NaN dt) advance nothing.
        assertEquals(0.25f, WaveformFeed.smoothStep(0.25f, 1f, 0f), 0f)
        assertEquals(0.25f, WaveformFeed.smoothStep(0.25f, 1f, Float.NaN), 0f)
    }
}
