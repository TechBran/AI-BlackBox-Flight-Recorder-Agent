package com.aiblackbox.portal.ui.components.wave.styles

import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.EnvelopeRing
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Mirrored Stereo Pulse — the pure geometry, the visual budgets, the two-voice
 * split/mirror invariants, NaN/empty-feed safety, refresh-rate independence and
 * the 40 dp degrade, in the aurora tradition: everything the renderer draws is a
 * pure function of the ring + size, so it is asserted here without a device.
 */
class MirroredPulseTest {

    private fun human(vararg bands: Float) = AuroraVoice(AuroraSpeaker.HUMAN, bands)
    private fun ai(vararg bands: Float) = AuroraVoice(AuroraSpeaker.AI, bands)

    /** A ring holding exactly these values, NEWEST-FIRST (values[0] is the newest). */
    private fun ringOf(vararg newestFirst: Float): EnvelopeRing {
        val ring = EnvelopeRing(newestFirst.size.coerceAtLeast(1))
        // push oldest → newest so newest(0) is values[0].
        for (i in newestFirst.indices.reversed()) ring.push(newestFirst[i])
        return ring
    }

    // ----------------------------------------------------------- bar layout

    @Test
    fun column_count_fits_whole_bars_across_the_width() {
        // 360 px wide, 3.5 px pitch (density 1) → 102 whole cells.
        val pitch = MirroredPulse.pitchPx(oneDpPx = 1f, small = false)
        assertEquals(3.5f, pitch, 1e-6f)
        assertEquals(102, MirroredPulse.columnCount(360f, pitch))
        // Degenerate inputs never throw and never go negative.
        assertEquals(0, MirroredPulse.columnCount(0f, pitch))
        assertEquals(0, MirroredPulse.columnCount(360f, 0f))
    }

    @Test
    fun small_height_draws_fewer_but_still_thin_bars() {
        val big = MirroredPulse.pitchPx(oneDpPx = 1f, small = false)
        val small = MirroredPulse.pitchPx(oneDpPx = 1f, small = true)
        // Wider pitch at small height → FEWER bars for the same width...
        assertTrue("small-height pitch must be wider", small > big)
        assertTrue(
            "40 dp bar must be fewer bars",
            MirroredPulse.columnCount(360f, small) < MirroredPulse.columnCount(360f, big),
        )
        // ...but the BAR itself stays the same 2 dp thin (density-relative, not shrunk).
        assertEquals(2f, MirroredPulse.barWidthPx(1f), 1e-6f)
    }

    @Test
    fun small_height_threshold_selects_the_degrade() {
        // 40 dp player bar degrades; 52 dp composer and 140 dp voice do not.
        assertTrue(MirroredPulse.isSmallHeight(40f, MirroredPulse.SMALL_HEIGHT_DP * 1f))
        assertFalse(MirroredPulse.isSmallHeight(52f, MirroredPulse.SMALL_HEIGHT_DP * 1f))
        assertFalse(MirroredPulse.isSmallHeight(140f, MirroredPulse.SMALL_HEIGHT_DP * 1f))
    }

    @Test
    fun amplitude_degrade_shortens_bars_only_at_small_height() {
        assertEquals(1f, MirroredPulse.amplitudeDegrade(small = false), 0f)
        assertEquals(MirroredPulse.SMALL_AMP_SCALE, MirroredPulse.amplitudeDegrade(small = true), 0f)
        assertTrue("40 dp bars must be shorter", MirroredPulse.SMALL_AMP_SCALE < 1f)
    }

    // ------------------------------------------------------- no mesa / clip

    @Test
    fun a_bar_never_reaches_a_rail_at_any_shipped_height() {
        // The exact defect the geometry is budgeted to avoid: a full-drive bar
        // that clips flat against a rail. Sweep the three shipped heights, both
        // densities, both degrade states, and every level 0..1.
        val heightsDp = floatArrayOf(40f, 52f, 140f)
        val densities = floatArrayOf(1f, 2.75f)
        for (density in densities) {
            for (hDp in heightsDp) {
                val h = hDp * density
                val small = MirroredPulse.isSmallHeight(h, MirroredPulse.SMALL_HEIGHT_DP * density)
                val inset = MirroredPulse.railInsetPx(h, MirroredPulse.INSET_MIN_DP * density)
                val availHalf = MirroredPulse.availableHalfPx(h, inset)
                val degrade = MirroredPulse.amplitudeDegrade(small)
                val centre = h / 2f
                var level = 0f
                while (level <= 1.0001f) {
                    val ext = MirroredPulse.barHalfExtentPx(availHalf, level, degrade)
                    // Fits strictly inside the available half → strictly inside the rails.
                    assertTrue(
                        "ext $ext must stay under availHalf $availHalf (h=$h)",
                        ext <= MirroredPulse.AMP_FRACTION * availHalf + 1e-4f,
                    )
                    // Up and down stay off both rails (railTop = inset, railBottom = h - inset).
                    assertTrue("bar top clipped at h=$h level=$level", centre - ext >= inset - 1e-3f)
                    assertTrue("bar bottom clipped at h=$h level=$level", centre + ext <= h - inset + 1e-3f)
                    level += 0.05f
                }
            }
        }
    }

    @Test
    fun full_drive_leaves_measurable_headroom() {
        // At 140 dp the tallest bar keeps AMP_FRACTION's slack below the rail.
        val h = 140f
        val inset = MirroredPulse.railInsetPx(h, MirroredPulse.INSET_MIN_DP)
        val availHalf = MirroredPulse.availableHalfPx(h, inset)
        val ext = MirroredPulse.barHalfExtentPx(availHalf, level = 1f, degrade = 1f)
        assertEquals(MirroredPulse.AMP_FRACTION * availHalf, ext, 1e-4f)
        assertTrue("headroom must be positive", availHalf - ext > 0f)
    }

    @Test
    fun core_never_exceeds_the_available_half() {
        // On a tiny canvas the core min could otherwise poke past the rails.
        val h = 6f
        val inset = MirroredPulse.railInsetPx(h, MirroredPulse.INSET_MIN_DP)
        val availHalf = MirroredPulse.availableHalfPx(h, inset)
        val core = MirroredPulse.coreHalfPx(h, MirroredPulse.CORE_MIN_DP, availHalf)
        assertTrue("core $core must fit availHalf $availHalf", core <= availHalf + 1e-6f)
    }

    // ------------------------------------------------------------- shaping

    /**
     * THE STYLE MUST NOT CURVE THE LEVEL TWICE. The perceptual lift happens exactly once
     * per column, in [MirroredPulse.columnLevel], through the app's one
     * [Aurora.displayCurve]; the geometry that consumes it is LINEAR, exactly as Aurora's
     * own `amplitude` is linear in its envelope's output.
     *
     * This replaces a `shaped(level) = sqrt(level)` that used to sit between the column
     * and the bar. It was the style's only curve while the column was read raw, and it
     * became a SECOND one the moment the column was curved — composing to an effective
     * exponent of 0.325, which is more compression than the ribbon had before any of this
     * work started. Deleting it is what keeps the one-curve rule true for this style.
     */
    @Test
    fun the_bar_geometry_is_linear_in_the_already_curved_column_level() {
        // A sanitiser, not a curve: the identity on a clean 0..1 input.
        for (v in listOf(0f, 0.04f, 0.25f, 0.5f, 1f)) {
            assertEquals(v, MirroredPulse.drawn(v), 1e-6f)
        }
        // NaN and out-of-range are absorbed, never propagated.
        assertEquals(0f, MirroredPulse.drawn(Float.NaN), 0f)
        assertEquals(1f, MirroredPulse.drawn(5f), 0f)
        assertEquals(0f, MirroredPulse.drawn(-1f), 0f)
        // ...and the extent is proportional, so doubling the drawn level doubles the bar.
        val half = MirroredPulse.barHalfExtentPx(100f, 0.4f, 1f)
        val full = MirroredPulse.barHalfExtentPx(100f, 0.8f, 1f)
        assertEquals("the bar must be linear in the column level", 2.0, (full / half).toDouble(), 1e-4)
    }

    /**
     * ...and the ONE curve is still there, upstream, where it belongs: a column read off
     * the ring IS its sample through [Aurora.displayCurve]. Together with the test above
     * this pins the whole chain — curved exactly once, then drawn linearly.
     */
    @Test
    fun the_column_carries_the_apps_one_perceptual_curve_and_only_one() {
        val ring = ringOf(0.25f)
        val column = MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 0, count = 1)
        assertEquals(Aurora.displayCurve(0.25f), column, 1e-6f)
        // Composed through the geometry, the total exponent from ring sample to drawn
        // extent is the app's one exponent and nothing more.
        val lo = MirroredPulse.barHalfExtentPx(
            100f,
            MirroredPulse.columnLevel(ringOf(0.1f), present = false, restLevel = 0f, column = 0, count = 1),
            1f,
        )
        val hi = MirroredPulse.barHalfExtentPx(
            100f,
            MirroredPulse.columnLevel(ringOf(0.4f), present = false, restLevel = 0f, column = 0, count = 1),
            1f,
        )
        val measuredExponent = Math.log((hi / lo).toDouble()) / Math.log(4.0)
        assertEquals(
            "ring sample -> drawn extent must compose to exactly one curve",
            Aurora.DISPLAY_GAMMA.toDouble(),
            measuredExponent,
            1e-4,
        )
    }

    // ----------------------------------------------------- brightness budget

    @Test
    fun body_alpha_rises_with_level_within_bounds() {
        assertEquals(MirroredPulse.BODY_ALPHA_MIN, MirroredPulse.bodyAlpha(0f), 1e-6f)
        assertEquals(MirroredPulse.BODY_ALPHA_MAX, MirroredPulse.bodyAlpha(1f), 1e-6f)
        assertTrue("louder must be brighter", MirroredPulse.bodyAlpha(0.9f) > MirroredPulse.bodyAlpha(0.2f))
        // Every value stays in the declared band.
        var l = 0f
        while (l <= 1f) {
            val a = MirroredPulse.bodyAlpha(l)
            assertTrue(a in MirroredPulse.BODY_ALPHA_MIN..MirroredPulse.BODY_ALPHA_MAX)
            l += 0.1f
        }
    }

    @Test
    fun white_out_sum_stays_under_auroras_ceiling() {
        // Bars are disjoint columns, so the worst additive stack at any pixel is
        // ONE body plus its own core. Keep it under the 1.6 ceiling — occlusion is
        // ZERO (every pass is additive), so this is the only budget that can bind.
        val worst = MirroredPulse.BODY_ALPHA_MAX + MirroredPulse.CORE_ALPHA
        assertTrue("worst additive stack $worst must be < ceiling", worst < MirroredPulse.WHITE_OUT_CEILING)
        assertEquals(1.52f, worst, 1e-6f)
    }

    // ------------------------------------------------ history → columns map

    /**
     * Every expectation in this section goes through [Aurora.displayCurve] and
     * [Aurora.restBlend] rather than restating a number, because the column IS those two
     * functions applied to a ring sample. Written as literals they would pin the app's
     * one perceptual curve inside a style test, where a re-tune of it reads as a
     * MirroredPulse regression; written this way each test keeps checking the thing it
     * is actually for — which sample a column reads, and how the floor is applied.
     */
    @Test
    fun column_level_is_newest_first_flush_right() {
        // Newest at the rightmost column, oldest at the left.
        val ring = ringOf(0.9f, 0.6f, 0.3f) // newest .. oldest
        val count = 3
        fun drawn(v: Float) = Aurora.displayCurve(v)
        // rightmost column (count-1) → newest.
        assertEquals(drawn(0.9f), MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 2, count), 1e-6f)
        assertEquals(drawn(0.6f), MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 1, count), 1e-6f)
        assertEquals(drawn(0.3f), MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 0, count), 1e-6f)
        // ...and the curve is a LIFT, not a rescale: a quiet column draws well above its
        // raw sample, which is the whole reason this style stopped reading the ring raw.
        assertTrue(drawn(0.3f) > 0.3f * 1.5f)
    }

    @Test
    fun column_level_reads_empty_tail_as_silence() {
        val ring = ringOf(0.5f, 0.4f) // only 2 samples
        val count = 5 // 5 columns over a 2-sample ring
        // The 2 newest (rightmost) columns carry data...
        assertEquals(Aurora.displayCurve(0.5f), MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 4, count), 1e-6f)
        assertEquals(Aurora.displayCurve(0.4f), MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 3, count), 1e-6f)
        // ...the older, not-yet-filled columns read silence, no throw.
        assertEquals(0f, MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 2, count), 0f)
        assertEquals(0f, MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 0, count), 0f)
        // Degenerate count is safe.
        assertEquals(0f, MirroredPulse.columnLevel(ring, present = false, restLevel = 0f, column = 0, count = 0), 0f)
    }

    @Test
    fun present_live_mic_floors_columns_to_stubble_player_bar_stays_flat() {
        val ring = ringOf(0f, 0f, 0f) // a silent window
        val count = 3
        // Live mic: restLevel 0.06 lifts every silent column into a low stubble.
        for (c in 0 until count) {
            assertEquals(
                0.06f,
                MirroredPulse.columnLevel(ring, present = true, restLevel = 0.06f, column = c, count),
                1e-6f,
            )
        }
        // Player bar: restLevel 0 is NOT floored → true flat silence (draws nothing).
        for (c in 0 until count) {
            assertEquals(
                0f,
                MirroredPulse.columnLevel(ring, present = true, restLevel = 0f, column = c, count),
                0f,
            )
        }
        // A loud sample rides well above the floor — BLENDED onto it, not clamped, so
        // every sample between silence and full drive still gets its own column height
        // (a `maxOf` drew the identical stubble for everything under 0.06).
        val loud = ringOf(0.8f)
        assertEquals(
            Aurora.restBlend(0.06f, Aurora.displayCurve(0.8f)),
            MirroredPulse.columnLevel(loud, present = true, restLevel = 0.06f, column = 0, count = 1),
            1e-6f,
        )
        // THE BLEND, stated as the property it exists for: two quiet samples that both sit
        // UNDER the floor must still draw at different heights.
        val quietA = MirroredPulse.columnLevel(ringOf(0.002f), present = true, restLevel = 0.06f, column = 0, count = 1)
        val quietB = MirroredPulse.columnLevel(ringOf(0.02f), present = true, restLevel = 0.06f, column = 0, count = 1)
        assertTrue("the floor is clamping again — quiet columns are frozen solid", quietB > quietA * 1.2f)
    }

    @Test
    fun departed_voice_is_not_floored_so_its_tail_drains() {
        // present = false (voice gone): its remaining tail reads its true history,
        // never a floor, so it fades to nothing as it scrolls off.
        val ring = ringOf(0.7f, 0.2f, 0f)
        assertEquals(0f, MirroredPulse.columnLevel(ring, present = false, restLevel = 0.06f, column = 0, count = 3), 0f)
        assertEquals(
            Aurora.displayCurve(0.7f),
            MirroredPulse.columnLevel(ring, present = false, restLevel = 0.06f, column = 2, count = 3),
            1e-6f,
        )
    }

    @Test
    fun column_level_survives_a_nan_rest_level() {
        val ring = ringOf(0.3f)
        // A garbage NaN floor must not poison the column (falls back to the sample).
        assertEquals(
            Aurora.displayCurve(0.3f),
            MirroredPulse.columnLevel(ring, present = true, restLevel = Float.NaN, column = 0, count = 1),
            1e-6f,
        )
    }

    // ---------------------------------------------------- two-voice split

    @Test
    fun split_reads_each_speaker_ring_independently() {
        // Both present at DIFFERENT levels → human (up) and AI (down) read their
        // OWN history, never each other's. This is the two-voice contract.
        val feed = WaveformFeed()
        val voices = listOf(human(0.6f, 0f, 0f, 0f), ai(0.25f, 0f, 0f, 0f))
        repeat(120) { feed.step(1f / 60f, voices) } // ~2 s
        val count = 40
        val ringH = feed.history(AuroraSpeaker.HUMAN)
        val ringA = feed.history(AuroraSpeaker.AI)
        val lh = MirroredPulse.columnLevel(ringH, present = true, restLevel = 0f, column = count - 1, count)
        val la = MirroredPulse.columnLevel(ringA, present = true, restLevel = 0f, column = count - 1, count)
        assertEquals(Aurora.displayCurve(0.6f), lh, 1e-6f)
        assertEquals(Aurora.displayCurve(0.25f), la, 1e-6f)
        // The two halves' extents differ accordingly (louder human → taller up bar).
        val availHalf = 60f
        assertTrue(
            MirroredPulse.barHalfExtentPx(availHalf, lh, 1f) > MirroredPulse.barHalfExtentPx(availHalf, la, 1f),
        )
    }

    @Test
    fun single_voice_mirror_is_symmetric_about_the_centre() {
        // Full mirror: the same column level drives the UP and DOWN extents, so the
        // bar is exactly symmetric about the centre line.
        val centre = 70f
        val availHalf = 60f
        val level = 0.5f
        val ext = MirroredPulse.barHalfExtentPx(availHalf, level, 1f)
        val topDistance = centre - (centre - ext)   // up reach
        val bottomDistance = (centre + ext) - centre // down reach
        assertEquals(topDistance, bottomDistance, 1e-4f)
    }

    // ------------------------------------------------ idle park / scrolling

    @Test
    fun ring_has_motion_detects_a_scrolling_tail() {
        assertFalse("empty ring is still", MirroredPulse.ringHasMotion(EnvelopeRing(4)))
        assertFalse("sub-eps ring is still", MirroredPulse.ringHasMotion(ringOf(0.005f, 0.002f)))
        assertTrue("a loud tail still needs frames", MirroredPulse.ringHasMotion(ringOf(0f, 0.4f, 0f)))
    }

    @Test
    fun speaker_active_covers_present_and_the_draining_tail() {
        val empty = EnvelopeRing(4)
        val tail = ringOf(0.5f, 0.3f)
        // Present always draws, even mid-silence.
        assertTrue(MirroredPulse.speakerActive(present = true, empty))
        // Departed but ring still scrolling → keep drawing the tail.
        assertTrue(MirroredPulse.speakerActive(present = false, tail))
        // Departed and drained → nothing to draw.
        assertFalse(MirroredPulse.speakerActive(present = false, empty))
    }

    @Test
    fun renderer_needs_frames_while_a_tail_is_still_scrolling() {
        val renderer = mirroredPulseRenderer()
        val feed = WaveformFeed()
        // Speak, then go silent (empty voices) — the ring still holds the tail.
        repeat(60) { feed.step(1f / 60f, listOf(human(0.6f, 0f, 0f, 0f))) }
        // No present voice this frame, but the loud tail must keep frames alive.
        assertTrue(renderer.needsFrames(emptyList(), feed))
        // Drain the ring with silence until it falls below the motion floor.
        repeat(400) { feed.step(1f / 60f, emptyList()) }
        assertFalse("a drained strip must park", renderer.needsFrames(emptyList(), feed))
    }

    // --------------------------------------------- refresh-rate independence

    @Test
    fun columns_are_identical_at_60_and_120_hz() {
        // The renderer adds no phase of its own — its picture is a pure function of
        // the (time-decimated) ring, so the SAME wall clock of audio yields the
        // SAME columns at 60 and 120 Hz.
        val voice = listOf(human(0.5f, 0f, 0f, 0f))
        val feed60 = WaveformFeed()
        repeat(120) { feed60.step(1f / 60f, voice) }
        val feed120 = WaveformFeed()
        repeat(240) { feed120.step(1f / 120f, voice) }

        val count = 40
        val ring60 = feed60.history(AuroraSpeaker.HUMAN)
        val ring120 = feed120.history(AuroraSpeaker.HUMAN)
        for (c in 0 until count) {
            val l60 = MirroredPulse.columnLevel(ring60, present = true, restLevel = 0f, column = c, count)
            val l120 = MirroredPulse.columnLevel(ring120, present = true, restLevel = 0f, column = c, count)
            assertEquals("column $c must match across refresh rates", l60, l120, 1e-6f)
        }
    }

    // ------------------------------------------- spectral segmentation (VU)

    @Test
    fun the_segments_tile_the_whole_bar_edge_to_edge() {
        // The bar's SILHOUETTE is the level's, unchanged — the spectrum only decides
        // where inside it one segment ends and the next begins. So the segments must
        // tile [0, ext] exactly: no gap (a dark seam across every bar) and no overlap
        // (two bodies compositing, which would break the white-out budget).
        val ext = 40f
        val (b0, b1, b2, b3) = listOf(0.6f, 0.3f, 0.15f, 0.05f)
        assertEquals(0f, MirroredPulse.segmentStartPx(ext, b0, b1, b2, b3, 0), 1e-5f)
        assertEquals(ext, MirroredPulse.segmentEndPx(ext, b0, b1, b2, b3, MirroredPulse.SEGMENTS - 1), 1e-4f)
        for (band in 0 until MirroredPulse.SEGMENTS - 1) {
            assertEquals(
                "segment $band must hand over to ${band + 1} with no seam",
                MirroredPulse.segmentEndPx(ext, b0, b1, b2, b3, band),
                MirroredPulse.segmentStartPx(ext, b0, b1, b2, b3, band + 1),
                1e-5f,
            )
        }
    }

    @Test
    fun segment_bounds_are_ordered_and_inside_the_bar() {
        val ext = 25f
        for (band in 0 until MirroredPulse.SEGMENTS) {
            val s = MirroredPulse.segmentStartPx(ext, 0.2f, 0.9f, 0f, 0.4f, band)
            val e = MirroredPulse.segmentEndPx(ext, 0.2f, 0.9f, 0f, 0.4f, band)
            assertTrue("segment $band start $s", s >= -1e-5f && s <= ext + 1e-5f)
            assertTrue("segment $band end $e", e >= -1e-5f && e <= ext + 1e-5f)
            assertTrue("segment $band must not invert", e >= s - 1e-5f)
        }
    }

    @Test
    fun a_flat_spectrum_splits_the_bar_evenly() {
        // Four equal bands carry no spectral opinion, so the bar divides into four
        // equal stripes — the neutral case the shipped single-body bar decays to.
        val ext = 40f
        for (band in 0 until MirroredPulse.SEGMENTS) {
            assertEquals(
                ext * band / MirroredPulse.SEGMENTS,
                MirroredPulse.segmentStartPx(ext, 0.5f, 0.5f, 0.5f, 0.5f, band),
                1e-4f,
            )
        }
    }

    @Test
    fun a_silent_column_splits_evenly_and_never_nan() {
        // No energy at all (a rest-level stubble column, whose BANDS are genuinely
        // zero even though its level is floored) must divide evenly rather than
        // divide by zero.
        val ext = 12f
        for (band in 0 until MirroredPulse.SEGMENTS) {
            val s = MirroredPulse.segmentStartPx(ext, 0f, 0f, 0f, 0f, band)
            val e = MirroredPulse.segmentEndPx(ext, 0f, 0f, 0f, 0f, band)
            assertTrue(s.isFinite() && e.isFinite())
            assertEquals(ext / MirroredPulse.SEGMENTS, e - s, 1e-4f)
        }
        // NaN bands are absorbed, exactly like every other input on this style.
        val nanShare = MirroredPulse.bandShare(Float.NaN, 0.4f, Float.NaN, 0.2f, 1)
        assertTrue(nanShare.isFinite())
        assertTrue(nanShare in 0f..1f)
    }

    @Test
    fun two_spectra_at_the_SAME_column_level_segment_the_bar_differently() {
        // THE acceptance test. Both columns read level 0.8 (the feed's weighted max
        // of the bands), so the bar is exactly as TALL in both — but a body-heavy
        // vowel and a presence-heavy one must not draw the same bar.
        val ext = 50f
        val body = floatArrayOf(0.8f, 0.1f, 0.05f, 0.02f)
        val presence = floatArrayOf(0.1f, 0.8f, 0.05f, 0.02f)
        var different = false
        for (band in 0 until MirroredPulse.SEGMENTS) {
            val a = MirroredPulse.segmentEndPx(ext, body[0], body[1], body[2], body[3], band)
            val b = MirroredPulse.segmentEndPx(ext, presence[0], presence[1], presence[2], presence[3], band)
            if (kotlin.math.abs(a - b) > 0.5f) different = true
        }
        assertTrue("equal level must NOT imply an equal bar", different)
    }

    @Test
    fun low_energy_hugs_the_centre_and_air_reaches_the_tip() {
        // The stack runs low→high from the centre line outward, so a rumble fills the
        // bar from the middle and a sibilant pushes its light out to the tip. That is
        // the whole reading: the bar's HEIGHT is still time-and-level, its CONTENT is
        // the spectrum.
        val ext = 40f
        val lowHeavy = MirroredPulse.segmentEndPx(ext, 0.9f, 0.05f, 0.02f, 0.02f, 0)
        val airHeavy = MirroredPulse.segmentEndPx(ext, 0.02f, 0.02f, 0.05f, 0.9f, 0)
        assertTrue("a low-heavy column fills from the centre", lowHeavy > ext * 0.7f)
        assertTrue("an air-heavy column barely lights the centre", airHeavy < ext * 0.2f)
        // ...and the top segment owns the tip in the air-heavy case.
        val airTop = ext - MirroredPulse.segmentStartPx(ext, 0.02f, 0.02f, 0.05f, 0.9f, 3)
        assertTrue("the air segment must own the tip", airTop > ext * 0.7f)
    }

    @Test
    fun segment_brightness_follows_that_bands_own_loudness() {
        // Each stripe is lit by ITS band, not by the column's overall level — that is
        // what makes a quiet high band a dim tip above a bright body rather than a
        // uniformly bright bar.
        val loud = MirroredPulse.segmentAlpha(0.9f)
        val quiet = MirroredPulse.segmentAlpha(0.05f)
        assertTrue("a loud band must be brighter", loud > quiet)
        // The pinned budget is unchanged: the stripes are vertically disjoint, so the
        // worst additive stack at a pixel is still ONE body alpha plus the core.
        assertTrue(loud <= MirroredPulse.BODY_ALPHA_MAX + 1e-6f)
        assertTrue(quiet >= MirroredPulse.BODY_ALPHA_MIN - 1e-6f)
        assertEquals(MirroredPulse.bodyAlpha(0.4f), MirroredPulse.segmentAlpha(0.4f), 1e-6f)
    }

    @Test
    fun column_band_reads_newest_first_like_the_level_ring() {
        // The band rings are pushed in the SAME feed loop as the level ring, so
        // column c indexes both identically — the stripe and the height it divides
        // describe the same instant of audio.
        val ring = ringOf(0.9f, 0.6f, 0.3f)
        assertEquals(0.9f, MirroredPulse.columnBand(ring, column = 2, count = 3), 1e-6f)
        assertEquals(0.3f, MirroredPulse.columnBand(ring, column = 0, count = 3), 1e-6f)
        // Empty tail and degenerate counts read as silence, never a throw.
        assertEquals(0f, MirroredPulse.columnBand(ring, column = 0, count = 8), 0f)
        assertEquals(0f, MirroredPulse.columnBand(ring, column = 0, count = 0), 0f)
    }

    @Test
    fun the_bar_height_is_still_the_level_not_the_spectrum() {
        // The identity guard: Mirrored Pulse's X axis is TIME and its bar height is
        // LOUDNESS. Whatever the spectrum does inside the bar, it must never change
        // how tall that bar is, or the style would have become a spectrum analyser.
        val availHalf = 60f
        val level = 0.5f
        val ext = MirroredPulse.barHalfExtentPx(availHalf, level, 1f)
        for (band in 0 until MirroredPulse.SEGMENTS) {
            val end = MirroredPulse.segmentEndPx(ext, 0.9f, 0.1f, 0f, 0f, band)
            assertTrue("no stripe may exceed the level-driven bar", end <= ext + 1e-4f)
        }
        assertEquals(
            "the outermost stripe ends exactly at the level-driven tip",
            ext,
            MirroredPulse.segmentEndPx(ext, 0.9f, 0.1f, 0f, 0f, MirroredPulse.SEGMENTS - 1),
            1e-4f,
        )
    }

    // --------------------------------------------------- empty-feed safety

    @Test
    fun a_fresh_feed_draws_nothing_and_never_throws() {
        val renderer = mirroredPulseRenderer()
        val feed = WaveformFeed()
        // Nothing stepped: neither speaker active, no frames wanted, columns silent.
        assertFalse(renderer.needsFrames(emptyList(), feed))
        assertFalse(MirroredPulse.speakerActive(feed.present(AuroraSpeaker.HUMAN), feed.history(AuroraSpeaker.HUMAN)))
        assertEquals(
            0f,
            MirroredPulse.columnLevel(feed.history(AuroraSpeaker.AI), present = false, restLevel = 0f, column = 0, count = 10),
            0f,
        )
    }
}
