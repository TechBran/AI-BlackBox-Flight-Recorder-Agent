package com.aiblackbox.portal.ui.components.wave.styles

import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Circular Orb — the pure geometry, budget arithmetic and role→element decisions
 * in [OrbMath], plus the history-style idle-park invariant on the renderer's
 * [orbRenderer]. Pure JVM in the aurora tradition: everything the look depends on
 * is a function of its inputs, so it is pinned here without a device or a Canvas.
 *
 * What is pinned: ring/disc geometry and the field-survives occlusion budget at
 * both sizes; radial-tick length/extent NEVER clipping the box (incl. the 40 dp
 * degrade); the two-voice contract (ticks = human, ring = AI, trails split); the
 * dot-trail distance falloff and brightness (idle floor, loud lift); NaN/empty
 * safety everywhere; dt-independent (wall-clock) breathing; and that a departed
 * voice's trail keeps the frame loop alive until it drains.
 */
class CircularOrbTest {

    private val eps = 1e-4f

    // ------------------------------------------------------------ ring / disc geometry

    @Test
    fun ring_radius_is_70pct_diameter_and_grows_to_80pct_when_small() {
        val h = 200f
        assertEquals(h * 0.35f, OrbMath.ringRadiusPx(h, small = false), eps) // 0.70·h diameter
        assertEquals(h * 0.40f, OrbMath.ringRadiusPx(h, small = true), eps)  // 0.80·h diameter
        // The degrade GROWS the ring (ticks shrink out of its way).
        assertTrue(OrbMath.ringRadiusPx(h, small = true) > OrbMath.ringRadiusPx(h, small = false))
    }

    @Test
    fun disc_sits_strictly_inside_the_ring() {
        val h = 200f
        for (small in booleanArrayOf(false, true)) {
            val ring = OrbMath.ringRadiusPx(h, small)
            val disc = OrbMath.orbDiscRadiusPx(h, small)
            assertTrue("disc inside ring ($small)", disc < ring)
            assertEquals(ring * OrbMath.ORB_DISC_FRACTION_OF_RING, disc, eps)
        }
    }

    // ------------------------------------------------------------ occlusion budget (field survives)

    @Test
    fun occlusion_stays_under_55pct_even_in_a_square_box_both_sizes() {
        // Worst case: a perfectly SQUARE container (the large Settings preview).
        val s = 300f
        val normal = OrbMath.occlusionFraction(s, s, small = false)
        val small = OrbMath.occlusionFraction(s, s, small = true)
        assertTrue("normal $normal < 0.55", normal < 0.55f)
        assertTrue("small $small < 0.55", small < 0.55f)
        // Sanity on the exact numbers so a later radius tweak that busts budget is caught.
        assertEquals(0.3117f, normal, 1e-3f)
        assertEquals(0.4072f, small, 1e-3f)
    }

    @Test
    fun occlusion_is_tiny_on_a_wide_player_bar() {
        // A 600×40 bar: the disc is a small dot in a long strip.
        val f = OrbMath.occlusionFraction(600f, 40f, small = true)
        assertTrue("wide-bar occlusion $f is tiny", f < 0.05f)
    }

    @Test
    fun occlusion_is_zero_for_a_degenerate_box() {
        assertEquals(0f, OrbMath.occlusionFraction(0f, 100f, small = false), eps)
        assertEquals(0f, OrbMath.occlusionFraction(100f, 0f, small = false), eps)
    }

    // ------------------------------------------------------------ ticks: count / angle / band

    @Test
    fun tick_count_halves_on_a_small_surface() {
        assertEquals(OrbMath.TICK_COUNT, OrbMath.tickCount(small = false))
        assertEquals(OrbMath.TICK_COUNT_SMALL, OrbMath.tickCount(small = true))
        assertEquals(OrbMath.TICK_COUNT / 2, OrbMath.tickCount(small = true))
        // In the reference sheet's 40–60 band at full size.
        assertTrue(OrbMath.TICK_COUNT in 40..60)
    }

    @Test
    fun tick_angles_span_the_full_circle_evenly() {
        val count = 48
        assertEquals(0f, OrbMath.tickAngle(0, count), eps)
        val two_pi = (2.0 * Math.PI).toFloat()
        assertEquals(two_pi * 0.5f, OrbMath.tickAngle(count / 2, count), eps) // halfway round
        // Monotonic, evenly spaced.
        val step = OrbMath.tickAngle(1, count) - OrbMath.tickAngle(0, count)
        assertEquals(two_pi / count, step, eps)
    }

    @Test
    fun tick_angle_single_count_is_finite_no_divide_by_zero() {
        assertTrue(OrbMath.tickAngle(0, 0).isFinite())
    }

    // ------------------------------------------------------------ ticks: the spectrum around the rim

    @Test
    fun the_forty_eight_spokes_carry_one_real_bin_each_mirrored_about_the_horizontal() {
        // The whole point of the milestone: 48 spokes used to repeat four band values
        // twelve times. At the shipped count each SIDE of the rim is exactly the 24-bin
        // spectrum, so the rim carries 24 distinct readings, mirrored top/bottom.
        val count = OrbMath.TICK_COUNT
        assertEquals(AuroraAnalyser.BINS, OrbMath.sideCount(count))
        val readings = HashSet<Int>()
        for (i in 0 until count) {
            val p = OrbMath.sidePosition(i, count)
            readings.add(p)
            // Mirror: the spoke reflected across the horizontal axis reads the same bin.
            assertEquals("spoke $i and its mirror must agree", p, OrbMath.sidePosition(count - 1 - i, count))
            assertEquals("one bin per spoke at the full count", 1, OrbMath.binRangeEnd(i, count) - OrbMath.binRangeStart(i, count))
        }
        assertEquals(AuroraAnalyser.BINS, readings.size)
    }

    @Test
    fun the_small_rim_still_covers_every_bin_rather_than_dropping_half_of_them() {
        // Halving the spokes must halve the RESOLUTION, not throw away the bins that
        // no longer have a spoke of their own — a peak must never become invisible
        // just because the surface is a 40 dp player bar.
        val count = OrbMath.TICK_COUNT_SMALL
        val covered = BooleanArray(AuroraAnalyser.BINS)
        for (i in 0 until count) {
            for (k in OrbMath.binRangeStart(i, count) until OrbMath.binRangeEnd(i, count)) covered[k] = true
        }
        for (k in covered.indices) assertTrue("bin $k is not read by any spoke", covered[k])
    }

    @Test
    fun spoke_bin_ranges_are_contiguous_and_tile_one_side_of_the_rim() {
        for (count in intArrayOf(OrbMath.TICK_COUNT, OrbMath.TICK_COUNT_SMALL, 12)) {
            val side = OrbMath.sideCount(count)
            assertEquals(0, OrbMath.binRangeStart(0, count))
            assertEquals(AuroraAnalyser.BINS, OrbMath.binRangeEnd(side - 1, count))
            for (i in 0 until side - 1) {
                assertTrue(OrbMath.binRangeEnd(i, count) > OrbMath.binRangeStart(i, count))
                assertEquals(OrbMath.binRangeEnd(i, count), OrbMath.binRangeStart(i + 1, count))
            }
        }
    }

    @Test
    fun spoke_maps_are_total_for_any_index_or_count() {
        for (count in intArrayOf(0, 1, 3, 24, 47, 48)) {
            for (i in intArrayOf(-5, 0, 1, 23, 47, 500)) {
                val p = OrbMath.sidePosition(i, count)
                assertTrue("side position $p out of range (i=$i, n=$count)", p in 0 until OrbMath.sideCount(count))
                val lo = OrbMath.binRangeStart(i, count)
                val hi = OrbMath.binRangeEnd(i, count)
                assertTrue("bin range $lo..$hi out of range (i=$i, n=$count)", lo in 0 until AuroraAnalyser.BINS && hi in (lo + 1)..AuroraAnalyser.BINS)
                assertTrue("band out of range (i=$i, n=$count)", OrbMath.bandForTick(i, count) in 0 until AuroraAnalyser.BANDS)
            }
        }
    }

    /**
     * THE spectrum-awareness acceptance test: two spectra with the SAME overall level
     * (same peak, same total energy) at different frequencies must bristle differently.
     * The old `i % BANDS` rim could only ever show four lengths whatever the spectrum.
     */
    @Test
    fun two_spectra_with_equal_overall_level_drive_different_ticks() {
        val count = OrbMath.TICK_COUNT
        val low = FloatArray(AuroraAnalyser.BINS).also { it[2] = 1f; it[3] = 0.8f }
        val high = FloatArray(AuroraAnalyser.BINS).also { it[20] = 1f; it[19] = 0.8f }
        assertEquals(low.max(), high.max(), 0f)
        assertEquals(low.sum(), high.sum(), 1e-6f)

        var differing = 0
        val lengths = HashSet<Float>()
        for (i in 0 until count) {
            val a = OrbMath.tickDrive(low, FloatArray(AuroraAnalyser.BANDS), i, count)
            val b = OrbMath.tickDrive(high, FloatArray(AuroraAnalyser.BANDS), i, count)
            if (a != b) differing++
            lengths.add(OrbMath.tickLengthPx(a, 200f, small = false))
        }
        // Two spokes a side for each of the four lit bins.
        assertEquals(8, differing)
        // And the rim is no longer four repeated lengths: silence + two lit values.
        assertTrue("the rim must carry more than the four band lengths", lengths.size >= 3)
    }

    @Test
    fun a_spoke_reads_the_loudest_bin_it_covers_and_is_nan_safe() {
        val count = OrbMath.TICK_COUNT_SMALL // two bins a spoke — a peak must survive
        val peak = FloatArray(AuroraAnalyser.BINS).also { it[3] = 0.9f }
        val bands = FloatArray(AuroraAnalyser.BANDS)
        val hit = (0 until count).filter { OrbMath.tickDrive(peak, bands, it, count) > 0f }
        assertEquals("one spoke a side owns bin 3", 2, hit.size)
        // The spoke's DRIVE is the bin through the app's one perceptual curve — the rim
        // used to hand the raw measurement straight to the geometry, which is why it read
        // as a flush stub on a real signal (a raw bin's typical value is a few
        // ten-thousandths). Pinned against the shared function, never a literal.
        assertEquals(Aurora.displayCurve(0.9f), OrbMath.tickDrive(peak, bands, hit[0], count), 1e-6f)

        val dirty = FloatArray(AuroraAnalyser.BINS) { Float.NaN }
        for (i in 0 until count) {
            assertTrue(OrbMath.tickDrive(dirty, bands, i, count) in 0f..1f)
        }
    }

    @Test
    fun without_a_spectrum_the_rim_falls_back_to_the_four_bands_on_the_same_axis() {
        // Same frequency layout, coarser: bass at the trail ends, sibilance at the top
        // of the arc, mirrored — so turning the spectrum on changes RESOLUTION, not
        // where a sound appears on the rim.
        val count = OrbMath.TICK_COUNT
        val bands = floatArrayOf(0.1f, 0.4f, 0.7f, 1.0f)
        assertEquals(0, OrbMath.bandForTick(0, count))
        assertEquals(AuroraAnalyser.BANDS - 1, OrbMath.bandForTick(count / 2, count))
        for (i in 0 until count) {
            val expected = Aurora.displayCurve(bands[OrbMath.bandForTick(i, count)])
            assertEquals("spoke $i", expected, OrbMath.tickDrive(null, bands, i, count), 0f)
        }
        // Still spectral: two band sets with the same overall level bristle differently.
        val low = floatArrayOf(1f, 0.6f, 0f, 0f)
        val high = floatArrayOf(0f, 0f, 0.6f, 1f)
        var differing = 0
        for (i in 0 until count) {
            if (OrbMath.tickDrive(null, low, i, count) != OrbMath.tickDrive(null, high, i, count)) differing++
        }
        assertEquals(count, differing)
    }

    /**
     * THE RIM HONOURS THE REST FLOOR — the one surface in the app that used to ignore it.
     *
     * Every other style lifts a present-but-silent voice onto the caller's restLevel so a
     * live mic keeps a resting presence between words; [OrbMath.tickDrive] took no floor
     * at all, so the rim alone collapsed flush while the ring beside it kept breathing.
     * The floor is BLENDED (Aurora.restBlend), so a silent spoke sits exactly on it and
     * every drive above it still moves.
     */
    @Test
    fun a_spoke_rests_on_the_callers_floor_and_still_moves_above_it() {
        val count = OrbMath.TICK_COUNT
        val silent = FloatArray(AuroraAnalyser.BANDS)
        // Live mic: a silent spoke settles EXACTLY on the floor, not below it.
        assertEquals(0.06f, OrbMath.tickDrive(null, silent, 0, count, restFloor = 0.06f), 1e-6f)
        // Player bar: floor 0 is the identity, so file silence still draws a bare rim.
        assertEquals(0f, OrbMath.tickDrive(null, silent, 0, count, restFloor = 0f), 0f)
        // ...and two quiet bands BOTH under the floor still produce different spokes.
        fun spokeAt(v: Float): Float {
            val bands = FloatArray(AuroraAnalyser.BANDS) { v }
            return OrbMath.tickDrive(null, bands, 0, count, restFloor = 0.06f)
        }
        assertTrue("the rim is clamping — quiet spokes are frozen", spokeAt(0.02f) > spokeAt(0.002f) * 1.2f)
        // The default is no floor, which is what an absent speaker gets.
        assertEquals(OrbMath.tickDrive(null, silent, 0, count), OrbMath.tickDrive(null, silent, 0, count, 0f), 0f)
    }

    // ------------------------------------------------------------ ticks: length never clips

    @Test
    fun tick_length_is_flush_at_idle_and_reaches_at_full_drive() {
        val h = 200f
        val idle = OrbMath.tickLengthPx(0f, h, small = false)
        val full = OrbMath.tickLengthPx(1f, h, small = false)
        assertEquals(h * OrbMath.TICK_BASE_FRACTION, idle, eps) // nearly flush
        assertTrue(full > idle)
        assertEquals(h * (OrbMath.TICK_BASE_FRACTION + OrbMath.TICK_SPAN_FRACTION), full, eps)
    }

    @Test
    fun tick_length_is_nan_safe_and_clamps_drive() {
        val h = 200f
        val base = h * OrbMath.TICK_BASE_FRACTION
        assertEquals(base, OrbMath.tickLengthPx(Float.NaN, h, small = false), eps)   // NaN → idle
        assertEquals(base, OrbMath.tickLengthPx(-2f, h, small = false), eps)         // clamped low
        val full = h * (OrbMath.TICK_BASE_FRACTION + OrbMath.TICK_SPAN_FRACTION)
        assertEquals(full, OrbMath.tickLengthPx(5f, h, small = false), eps)          // clamped high
    }

    @Test
    fun tick_outer_extent_never_clips_the_box_at_full_drive_both_sizes() {
        // The no-mesa / no-clip budget: rim + longest tick stays inside height/2,
        // for the full surface AND the 40 dp degrade — computed analytically.
        val h = 200f
        for (small in booleanArrayOf(false, true)) {
            val extent = OrbMath.tickOuterExtentPx(1f, h, small)
            assertTrue("extent $extent < h/2 ($small)", extent < h / 2f)
            assertTrue("extent keeps a margin ($small)", extent <= h * 0.48f)
        }
    }

    @Test
    fun small_degrade_shrinks_tick_reach() {
        val h = 200f
        val normalFull = OrbMath.tickLengthPx(1f, h, small = false)
        val smallFull = OrbMath.tickLengthPx(1f, h, small = true)
        assertTrue(smallFull < normalFull)
    }

    // ------------------------------------------------------------ dot trails: count / falloff / brightness

    @Test
    fun dotCount_fills_to_the_edge_capped_and_never_negative() {
        val spacing = 4f
        // Room = w/2 - start. 300 - 20 = 280 → 70 dots, but capped at DOT_MAX_COUNT.
        val n = OrbMath.dotCount(600f, 20f, spacing)
        assertEquals(OrbMath.DOT_MAX_COUNT, n)
        // No room (start beyond half width) → zero, not negative.
        assertEquals(0, OrbMath.dotCount(100f, 200f, spacing))
        // Zero spacing → zero (no divide-by-zero).
        assertEquals(0, OrbMath.dotCount(600f, 20f, 0f))
    }

    @Test
    fun dot_distance_falloff_is_1_nearest_and_decreases_outward() {
        val count = 40
        assertEquals(1f, OrbMath.dotDistanceFalloff(0, count), eps) // newest, nearest the orb
        var prev = OrbMath.dotDistanceFalloff(0, count)
        for (i in 1 until count) {
            val f = OrbMath.dotDistanceFalloff(i, count)
            assertTrue("monotonic decrease at $i", f <= prev)
            assertTrue("bounded 0..1", f in 0f..1f)
            prev = f
        }
        assertEquals(1f, OrbMath.dotDistanceFalloff(0, 1), eps) // single-dot guard
    }

    @Test
    fun dot_brightness_has_an_idle_floor_and_lifts_with_loudness() {
        val count = 40
        // Silent newest dot: floor only, but still visible (a faint dotted line).
        val silent = OrbMath.dotBrightness(0f, 0, count)
        assertEquals(OrbMath.DOT_FLOOR, silent, eps)
        assertTrue("idle dot is visible", silent >= OrbMath.DOT_MIN_VISIBLE)
        // Loud lifts it well above the floor.
        val loud = OrbMath.dotBrightness(1f, 0, count)
        assertTrue(loud > silent)
        assertEquals(1f, loud, eps) // newest + full loudness = full brightness
        // Bounded and NaN-safe.
        assertTrue(OrbMath.dotBrightness(5f, 3, count) in 0f..1f)
        assertTrue(OrbMath.dotBrightness(Float.NaN, 3, count).isFinite())
    }

    @Test
    fun dot_radius_shrinks_with_distance_and_is_bounded() {
        val base = 3f
        val count = 40
        val near = OrbMath.dotRadiusPx(base, 1f, 0, count)
        val far = OrbMath.dotRadiusPx(base, 1f, count - 1, count)
        assertTrue("near dot bigger than far", near > far)
        assertTrue("radius never negative", OrbMath.dotRadiusPx(base, Float.NaN, 5, count) >= 0f)
    }

    // ------------------------------------------------------------ two-voice contract

    @Test
    fun ticks_are_human_ring_is_ai_when_both_voices_are_live() {
        // The locked plan mapping: HUMAN = outer ticks, AI = ring pulse + inner ring.
        assertEquals(AuroraSpeaker.HUMAN, OrbMath.tickSpeaker(twoVoice = true, solo = AuroraSpeaker.HUMAN))
        assertEquals(AuroraSpeaker.AI, OrbMath.ringSpeaker(twoVoice = true, solo = AuroraSpeaker.HUMAN))
        assertTrue(OrbMath.innerRingVisible(twoVoice = true))
    }

    @Test
    fun single_voice_owns_the_whole_instrument_in_its_own_colour() {
        // Solo AI: ticks AND ring are AI; no inner ring.
        assertEquals(AuroraSpeaker.AI, OrbMath.tickSpeaker(twoVoice = false, solo = AuroraSpeaker.AI))
        assertEquals(AuroraSpeaker.AI, OrbMath.ringSpeaker(twoVoice = false, solo = AuroraSpeaker.AI))
        assertFalse(OrbMath.innerRingVisible(twoVoice = false))
        // Solo human likewise.
        assertEquals(AuroraSpeaker.HUMAN, OrbMath.ringSpeaker(twoVoice = false, solo = AuroraSpeaker.HUMAN))
    }

    @Test
    fun trails_split_left_human_right_ai_in_two_voice_and_mirror_the_solo_otherwise() {
        assertEquals(AuroraSpeaker.HUMAN, OrbMath.trailSpeaker(isLeft = true, twoVoice = true, solo = AuroraSpeaker.HUMAN))
        assertEquals(AuroraSpeaker.AI, OrbMath.trailSpeaker(isLeft = false, twoVoice = true, solo = AuroraSpeaker.HUMAN))
        // One voice: BOTH sides carry the solo speaker (symmetric).
        assertEquals(AuroraSpeaker.AI, OrbMath.trailSpeaker(isLeft = true, twoVoice = false, solo = AuroraSpeaker.AI))
        assertEquals(AuroraSpeaker.AI, OrbMath.trailSpeaker(isLeft = false, twoVoice = false, solo = AuroraSpeaker.AI))
    }

    // ------------------------------------------------------------ ring glow / whiteness budgets

    @Test
    fun ring_glow_alpha_is_bounded_and_rises_with_loudness() {
        val quiet = OrbMath.ringGlowAlpha(0f, breathe = 0.5f)
        val loud = OrbMath.ringGlowAlpha(1f, breathe = 0.5f)
        assertTrue(loud > quiet)
        for (l in listOf(-1f, 0f, 0.5f, 1f, 5f, Float.NaN)) {
            for (b in listOf(0f, 0.5f, 1f)) {
                val a = OrbMath.ringGlowAlpha(l, b)
                assertTrue("glow alpha $a bounded", a in OrbMath.RING_GLOW_ALPHA_MIN..OrbMath.RING_GLOW_ALPHA_MAX)
            }
        }
    }

    @Test
    fun ring_glow_width_is_bounded_and_smaller_when_small() {
        val normal = OrbMath.ringGlowWidthMult(1f, breathe = 0.5f, small = false)
        val small = OrbMath.ringGlowWidthMult(1f, breathe = 0.5f, small = true)
        assertTrue("small halo is trimmed", small < normal)
        assertTrue(normal <= OrbMath.RING_GLOW_MULT_MAX + eps)
        assertTrue(OrbMath.ringGlowWidthMult(0f, 0.5f, false) >= OrbMath.RING_GLOW_MULT_MIN - eps)
    }

    @Test
    fun additive_pass_alphas_never_white_out_on_their_own() {
        // Every additive (Plus) pass must be ≤ 1 alpha individually, so no single
        // pass whites out the surface; the bright RIM is deliberately the sum of the
        // thin ring core + halo (a white-hot line), never a broad fill.
        assertTrue(OrbMath.RING_CORE_ALPHA <= 1f)
        assertTrue(OrbMath.INNER_RING_CORE_ALPHA <= 1f)
        assertTrue(OrbMath.RING_GLOW_ALPHA_MAX <= 1f)
        assertTrue(OrbMath.TICK_CORE_ALPHA <= 1f)
        assertTrue(OrbMath.TICK_GLOW_ALPHA <= 1f)
        assertTrue(OrbMath.DOT_ALPHA <= 1f)
    }

    @Test
    fun ring_whiteness_is_bounded() {
        assertEquals(0f, OrbMath.ringWhiteness(0f), eps)
        assertTrue(OrbMath.ringWhiteness(1f) in 0f..1f)
        assertTrue(OrbMath.ringWhiteness(Float.NaN).isFinite())
    }

    // ------------------------------------------------------------ breathe: dt-free, bounded

    @Test
    fun breathe_is_bounded_deterministic_and_dt_independent() {
        for (p in listOf(0f, 0.3f, 1f, 7.5f, 1000f)) {
            assertTrue(OrbMath.breathe(p) in 0f..1f)
        }
        // Same wall-clock phase → same value no matter how many frames drew it.
        assertEquals(OrbMath.breathe(2.5f), OrbMath.breathe(2.5f), 0f)
        // A different phase generally moves it (the ring breathes).
        assertNotEquals(OrbMath.breathe(0.1f), OrbMath.breathe(2.0f))
        // NaN phase → finite.
        assertTrue(OrbMath.breathe(Float.NaN).isFinite())
    }

    // ------------------------------------------------------------ ring level / rest floor

    @Test
    fun ring_level_floors_a_present_but_silent_voice_and_is_zero_when_absent() {
        val feed = WaveformFeed()
        // Present but silent (all-zero bands): floored by restLevel.
        feed.step(0.02f, listOf(human(0f, 0f, 0f, 0f)))
        assertEquals(0.3f, OrbMath.ringLevel(feed, AuroraSpeaker.HUMAN, restLevel = 0.3f), eps)
        // Absent AI: no floor → 0.
        assertEquals(0f, OrbMath.ringLevel(feed, AuroraSpeaker.AI, restLevel = 0.3f), eps)
        // Loud present voice reads its own level, above the floor.
        feed.step(0.02f, listOf(human(0.9f, 0.9f, 0.9f, 0.9f)))
        assertTrue(OrbMath.ringLevel(feed, AuroraSpeaker.HUMAN, 0.1f) > 0.5f)
    }

    // ------------------------------------------------------------ small degrade gate

    @Test
    fun small_degrade_engages_at_40dp_and_not_on_a_tall_surface() {
        assertTrue(OrbMath.isSmall(40f, OrbMath.SMALL_HEIGHT_DP))   // 40 < 52 → small
        assertFalse(OrbMath.isSmall(120f, OrbMath.SMALL_HEIGHT_DP))
        assertFalse(OrbMath.isSmall(0f, OrbMath.SMALL_HEIGHT_DP))   // degenerate, not "small"
    }

    // ------------------------------------------------------------ activity / solo

    @Test
    fun active_count_and_solo_track_present_and_draining_voices() {
        val feed = WaveformFeed()
        assertEquals(0, OrbMath.activeCount(feed)) // silent, empty
        feed.step(0.02f, listOf(human(0.8f, 0.8f, 0.8f, 0.8f)))
        assertEquals(1, OrbMath.activeCount(feed))
        assertEquals(AuroraSpeaker.HUMAN, OrbMath.soloSpeaker(feed))
        // Solo AI resolves to AI.
        val feed2 = WaveformFeed()
        feed2.step(0.02f, listOf(ai(0.8f, 0.8f, 0.8f, 0.8f)))
        assertEquals(AuroraSpeaker.AI, OrbMath.soloSpeaker(feed2))
        // Both live → two-voice.
        val feed3 = WaveformFeed()
        feed3.step(0.02f, listOf(human(0.7f, 0f, 0f, 0f), ai(0.7f, 0f, 0f, 0f)))
        assertEquals(2, OrbMath.activeCount(feed3))
    }

    // ------------------------------------------------------------ idle park (history style)

    @Test
    fun needsFrames_false_when_silent_and_empty() {
        assertFalse(orbRenderer().needsFrames(emptyList(), WaveformFeed()))
    }

    @Test
    fun needsFrames_true_while_a_voice_is_audible() {
        val feed = WaveformFeed()
        val voices = listOf(human(0.8f, 0.8f, 0.8f, 0.8f))
        feed.step(0.02f, voices)
        assertTrue(orbRenderer().needsFrames(voices, feed))
    }

    @Test
    fun needsFrames_keeps_the_trail_flowing_after_the_voice_leaves_then_parks() {
        // A departed voice's dot trail must keep flowing outward until it drains —
        // the history-style override, not the default audible-only park.
        val feed = WaveformFeed(historySec = 0.1f, cadenceHz = 50f) // small ring, drains fast
        val renderer = orbRenderer()
        val loud = listOf(human(0.9f, 0.9f, 0.9f, 0.9f))
        repeat(10) { feed.step(0.02f, loud) }
        feed.step(0.02f, emptyList()) // voice leaves; tail still on screen
        assertTrue("tail still flowing", renderer.needsFrames(emptyList(), feed))
        repeat(20) { feed.step(0.02f, emptyList()) }
        assertFalse("drained → parked", renderer.needsFrames(emptyList(), feed))
    }

    private fun human(vararg bands: Float) = AuroraVoice(AuroraSpeaker.HUMAN, bands)
    private fun ai(vararg bands: Float) = AuroraVoice(AuroraSpeaker.AI, bands)
}
