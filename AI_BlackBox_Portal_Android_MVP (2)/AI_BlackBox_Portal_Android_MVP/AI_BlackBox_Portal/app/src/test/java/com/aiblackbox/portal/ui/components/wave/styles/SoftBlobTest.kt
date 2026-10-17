package com.aiblackbox.portal.ui.components.wave.styles

import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraSpectrum
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Soft Blob Wave — the pure geometry, spectral slicing and budgets in [SoftBlob],
 * plus the audible branch of the renderer's inherited needsFrames. Pure JVM in the
 * aurora tradition: everything the look depends on is a function of its inputs, so
 * it is pinned here without a device or a Canvas.
 *
 * What is pinned: the lobe layouts (5 full / 3 small, broad low-frequency centre)
 * and the requirement that EVERY lobe reads a DIFFERENT slice of the spectrum, the
 * slice map (tiling, bin ownership, log-interpolated band fallback), the shared M7
 * acceptance rule — two spectra at equal overall LEVEL must draw differently — on
 * both the band and the bin path, the gaussian + summed-mound geometry, the
 * saturating silhouette that never clips or mesas at ANY height incl. 40 dp and
 * with two voices, the occlusion (≤55%) and additive white-out (<1.6) budgets with
 * their exact numbers, the two-voice interleave shift, and NaN/empty safety across
 * every entry point.
 */
class SoftBlobTest {

    private val eps = 1e-4f

    /** Geometric centre of an analyser band, from the real probe pair — never restated. */
    private fun bandCentreHz(band: Int): Float {
        val p = AuroraAnalyser.PROBES[band]
        return sqrt(p.first().toDouble() * p.last()).toFloat()
    }

    private fun drivesFor(bands: FloatArray, bins: FloatArray? = null, level: Float = 1f): FloatArray {
        val out = FloatArray(SoftBlob.SLICES.size)
        SoftBlob.sliceDrives(bands, bins, level, out)
        return out
    }

    private fun heightsFor(bands: FloatArray, bins: FloatArray? = null, level: Float = 1f): FloatArray {
        val out = drivesFor(bands, bins, level)
        for (i in out.indices) out[i] = SoftBlob.lobeHeight(out[i], 0f)
        return out
    }

    /** The biggest difference between two mound profiles, sampled across the width. */
    private fun profileDistance(a: FloatArray, b: FloatArray, lobes: List<SoftBlob.Lobe>): Float {
        var worst = 0f
        for (i in 0..100) {
            val x = i / 100f
            val d = abs(SoftBlob.moundRaw(x, lobes, a, 0f) - SoftBlob.moundRaw(x, lobes, b, 0f))
            if (d > worst) worst = d
        }
        return worst
    }

    // ------------------------------------------------------------ lobe layouts

    @Test
    fun full_layout_is_five_lobes_small_is_three() {
        assertEquals(5, SoftBlob.FULL_LOBES.size)
        assertEquals(3, SoftBlob.SMALL_LOBES.size)
    }

    @Test
    fun centre_lobe_is_the_broad_low_frequency_mound_in_both_layouts() {
        for (lobes in listOf(SoftBlob.FULL_LOBES, SoftBlob.SMALL_LOBES)) {
            // The tallest lobe sits dead centre, is driven by the LOWEST slice (0 —
            // the body of the voice), and is the BROADEST (largest sigma).
            val tallest = lobes.maxByOrNull { it.ampScale }!!
            assertEquals(0.5f, tallest.xFrac, eps)
            assertEquals(0, tallest.slice)
            assertEquals(tallest.sigmaFrac, lobes.maxOf { it.sigmaFrac }, eps)
            assertEquals(0.5f, SoftBlob.centreLobeXFrac(lobes), eps)
        }
    }

    @Test
    fun full_layout_side_lobes_are_narrower_and_driven_by_higher_slices() {
        val centre = SoftBlob.FULL_LOBES.maxByOrNull { it.ampScale }!!
        val outer = SoftBlob.FULL_LOBES.filter { it.xFrac < 0.2f || it.xFrac > 0.8f }
        assertTrue(outer.isNotEmpty())
        for (l in outer) {
            assertTrue("outer lobe narrower than centre", l.sigmaFrac < centre.sigmaFrac)
            assertTrue("outer lobe on a higher slice", l.slice > centre.slice)
        }
    }

    @Test
    fun every_lobe_reads_a_distinct_spectral_slice() {
        // The "bland" defect was two lobes sharing one drive: a mirrored pair that
        // always agrees carries no information. No layout may repeat a slice.
        for (lobes in listOf(SoftBlob.FULL_LOBES, SoftBlob.SMALL_LOBES)) {
            val used = lobes.map { it.slice }
            assertEquals("a layout repeats a slice: $used", used.size, used.toSet().size)
            for (l in lobes) assertTrue(l.slice in SoftBlob.SLICES.indices)
        }
    }

    // ------------------------------------------------------------ the slice map

    @Test
    fun slices_tile_the_spectrum_map_end_to_end() {
        val slices = SoftBlob.SLICES
        assertEquals(AuroraSpectrum.MIN_HZ, slices.first().loHz, eps)
        assertEquals(AuroraSpectrum.MAX_HZ, slices.last().hiHz, eps)
        for (i in slices.indices) {
            assertTrue("slice $i is empty", slices[i].hiHz > slices[i].loHz)
            if (i > 0) {
                // Contiguous: no gap (a frequency driving nothing) and no overlap
                // (two lobes sharing energy).
                assertEquals(slices[i - 1].hiHz, slices[i].loHz, eps)
                assertTrue("centres ascend", slices[i].centreHz > slices[i - 1].centreHz)
            }
        }
    }

    @Test
    fun every_slice_owns_at_least_one_spectrum_bin_and_together_they_own_them_all() {
        var total = 0
        for (i in SoftBlob.SLICES.indices) {
            val n = SoftBlob.sliceBinCount(i)
            assertTrue("slice $i owns no bin", n > 0)
            total += n
        }
        // The tiling is exact: every log bin belongs to exactly one slice.
        assertEquals(AuroraAnalyser.BINS, total)
    }

    @Test
    fun bands_are_read_at_a_frequency_by_log_interpolation() {
        val bands = floatArrayOf(0.1f, 0.9f, 0.2f, 0.5f)
        // At a band's own centre the reading IS that band.
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertEquals(bands[b], SoftBlob.bandAtHz(bands, bandCentreHz(b)), 1e-3f)
        }
        // Between two centres it sits between the two values, and moves monotonically.
        val lo = bandCentreHz(1)
        val hi = bandCentreHz(2)
        val mid = SoftBlob.bandAtHz(bands, sqrt(lo * hi))
        assertTrue(mid < bands[1] && mid > bands[2])
        // Outside the probe range it clamps to the end bands rather than extrapolating.
        assertEquals(bands[0], SoftBlob.bandAtHz(bands, 20f), eps)
        assertEquals(bands[3], SoftBlob.bandAtHz(bands, 20000f), eps)
        // Total on garbage.
        assertEquals(0f, SoftBlob.bandAtHz(bands, Float.NaN), eps)
        assertEquals(0f, SoftBlob.bandAtHz(floatArrayOf(Float.NaN, Float.NaN, Float.NaN, Float.NaN), 1000f), eps)
        assertEquals(0f, SoftBlob.bandAtHz(FloatArray(0), 1000f), eps)
    }

    @Test
    fun no_analyser_band_is_ignored_by_the_slice_map() {
        // Every band must reach the geometry through some slice; a band that drives
        // nothing is spectral information thrown away.
        for (b in 0 until AuroraAnalyser.BANDS) {
            val bands = FloatArray(AuroraAnalyser.BANDS).also { it[b] = 1f }
            val drives = drivesFor(bands)
            assertTrue("band $b drives no slice", drives.any { it > 0.01f })
        }
    }

    // ------------------------------------------ M7: spectrum, not just level

    @Test
    fun the_two_outer_lobes_no_longer_move_as_one() {
        // They used to share band 3. A spectrum tilted anywhere in the top half must
        // now separate presence (slice 3) from air (slice 4).
        val bands = floatArrayOf(0.2f, 0.2f, 0.9f, 0.2f)
        val drives = drivesFor(bands)
        val left = SoftBlob.FULL_LOBES.first { it.xFrac < 0.2f }
        val right = SoftBlob.FULL_LOBES.first { it.xFrac > 0.8f }
        assertNotEquals("the mirrored outer lobes still share a drive", drives[left.slice], drives[right.slice])
    }

    @Test
    fun two_spectra_at_equal_level_draw_different_mounds() {
        // The shared M7 acceptance rule. Both vectors carry the SAME overall level by
        // the feed's own definition (the weighted max every style shares), so the only
        // thing that can separate them is their spectral shape.
        val low = floatArrayOf(0.6f, 0.1f, 0.1f, 0.1f)
        val high = floatArrayOf(0.1f, 0.1f, 0.1f, 0.6f / WaveformFeed.LEVEL_WEIGHTS[3])
        assertEquals(
            WaveformFeed.levelOf(AuroraVoice(AuroraSpeaker.HUMAN, low)),
            WaveformFeed.levelOf(AuroraVoice(AuroraSpeaker.HUMAN, high)),
            1e-3f,
        )
        val d = profileDistance(heightsFor(low), heightsFor(high), SoftBlob.FULL_LOBES)
        assertTrue("equal-level spectra drew the same mound (d=$d)", d > 0.1f)
    }

    @Test
    fun with_a_spectrum_published_the_shape_comes_from_the_bins() {
        // Same bands, same level, two different SPECTRA: the bins must reshape the
        // blob. Anything less means the 24-bin channel is being ignored.
        val bands = floatArrayOf(0.5f, 0.5f, 0.5f, 0.5f)
        val lowTilt = FloatArray(AuroraAnalyser.BINS) { if (it < AuroraAnalyser.BINS / 2) 0.8f else 0.05f }
        val highTilt = FloatArray(AuroraAnalyser.BINS) { if (it < AuroraAnalyser.BINS / 2) 0.05f else 0.8f }
        val a = drivesFor(bands, lowTilt, 0.7f)
        val b = drivesFor(bands, highTilt, 0.7f)
        var worst = 0f
        for (i in a.indices) worst = maxOf(worst, abs(a[i] - b[i]))
        assertTrue("the two spectra produced the same drives", worst > 0.2f)
        // And the tilt points the right way round: low-tilted lights the body slice
        // hardest, high-tilted the air slice.
        assertTrue(a[0] > a[SoftBlob.SLICES.size - 1])
        assertTrue(b[SoftBlob.SLICES.size - 1] > b[0])
    }

    @Test
    fun the_bin_path_takes_shape_from_the_bins_and_magnitude_from_the_level() {
        // The bins are RAW (no auto-gain, no headroom): only their RATIO is usable, so
        // the loudest slice must land exactly on the calibrated level and no slice may
        // exceed it — whatever absolute size the bins happen to have.
        val bands = floatArrayOf(0.4f, 0.4f, 0.4f, 0.4f)
        for (scale in listOf(0.01f, 0.2f, 1f)) {
            val bins = FloatArray(AuroraAnalyser.BINS) { scale * (if (it < 4) 1f else 0.25f) }
            val drives = drivesFor(bands, bins, 0.6f)
            assertEquals(0.6f, drives.max(), 1e-3f)
            for (d in drives) assertTrue(d <= 0.6f + eps)
        }
        // An all-silent spectrum is silent, not a divide by zero.
        val silent = drivesFor(bands, FloatArray(AuroraAnalyser.BINS), 0.6f)
        for (d in silent) assertEquals(0f, d, eps)
    }

    @Test
    fun slice_drives_are_total_on_garbage_input() {
        val bands = floatArrayOf(Float.NaN, 0.5f, Float.NaN, 2f)
        for (d in drivesFor(bands)) assertTrue("NaN leaked into a drive", d.isFinite() && d >= 0f)
        val bins = FloatArray(AuroraAnalyser.BINS) { if (it % 3 == 0) Float.NaN else 0.4f }
        for (d in drivesFor(bands, bins, Float.NaN)) assertTrue(d.isFinite() && d >= 0f)
        // A short input (a producer with fewer bands/bins than we expect) reads 0, not
        // an index out of bounds, because this runs inside a draw loop.
        for (d in drivesFor(FloatArray(1), FloatArray(2), 0.5f)) assertTrue(d.isFinite())
        // An undersized out array is filled as far as it goes and never overruns.
        val short = FloatArray(2)
        SoftBlob.sliceDrives(floatArrayOf(1f, 1f, 1f, 1f), null, 1f, short)
        for (d in short) assertTrue(d > 0f)
    }

    // ------------------------------------------------------------ the one curve

    @Test
    fun the_style_applies_the_apps_one_perceptual_curve_and_never_its_own() {
        // Not "sqrt" restated — the app has exactly one curve and this style calls it,
        // so a re-tune of Aurora.DISPLAY_GAMMA moves the blob with it.
        for (v in listOf(0f, 0.05f, 0.25f, 0.5f, 1f)) {
            assertEquals(Aurora.displayCurve(v), SoftBlob.shaped(v), 1e-6f)
        }
        assertEquals(1f, SoftBlob.shaped(4f), eps)      // clamped
        assertEquals(0f, SoftBlob.shaped(Float.NaN), eps)
    }

    @Test
    fun lobe_height_curves_the_drive_and_blends_it_onto_the_rest_floor() {
        // Floor 0 (the player bar) is the identity: the lobe IS the curved drive.
        assertEquals(SoftBlob.shaped(0.4f), SoftBlob.lobeHeight(0.4f, 0f), eps)
        // A present-but-silent voice keeps the caller's resting mound, exactly.
        assertEquals(0.06f, SoftBlob.lobeHeight(0f, 0.06f), eps)
        // A live drive rides above it.
        assertTrue(SoftBlob.lobeHeight(0.5f, 0.06f) > 0.06f)
        assertEquals(0f, SoftBlob.lobeHeight(Float.NaN, Float.NaN), eps)
        // Full drive still reaches exactly 1, so the FILL_CEIL / no-mesa budget is untouched.
        assertEquals(1f, SoftBlob.lobeHeight(1f, 0.06f), eps)
        // THE BLEND, not a clamp: two drives BOTH under the floor must still draw different
        // mounds. Under the `maxOf` this replaces they drew the identical silhouette, which
        // froze the blob solid across the whole quiet end of a live mic's range.
        assertTrue(
            "the rest floor is clamping again — quiet mounds are frozen",
            SoftBlob.lobeHeight(0.02f, 0.06f) > SoftBlob.lobeHeight(0.002f, 0.06f) * 1.2f,
        )
    }

    @Test
    fun rest_floor_clamps_and_is_nan_safe() {
        assertEquals(0f, SoftBlob.restFloor(0f), eps)
        assertEquals(0.06f, SoftBlob.restFloor(0.06f), eps)
        assertEquals(1f, SoftBlob.restFloor(9f), eps)
        assertEquals(0f, SoftBlob.restFloor(Float.NaN), eps)
    }

    // ------------------------------------------------------------ gaussian

    @Test
    fun gaussian_is_one_at_centre_symmetric_and_falls_off() {
        assertEquals(1f, SoftBlob.gaussian(0f, 0.1f), eps)
        assertEquals(SoftBlob.gaussian(0.05f, 0.1f), SoftBlob.gaussian(-0.05f, 0.1f), eps)
        assertTrue(SoftBlob.gaussian(0.2f, 0.1f) < SoftBlob.gaussian(0.1f, 0.1f))
        assertTrue(SoftBlob.gaussian(0.1f, 0.1f) < 1f)
    }

    @Test
    fun gaussian_degenerate_sigma_is_a_spike_and_nan_safe() {
        assertEquals(1f, SoftBlob.gaussian(0f, 0f), eps)
        assertEquals(0f, SoftBlob.gaussian(0.01f, 0f), eps)
        assertEquals(0f, SoftBlob.gaussian(Float.NaN, 0.1f), eps)
    }

    // ------------------------------------------------------------ mound sum

    @Test
    fun moundRaw_is_zero_for_silent_heights() {
        val silent = FloatArray(SoftBlob.SLICES.size)
        assertEquals(0f, SoftBlob.moundRaw(0.5f, SoftBlob.FULL_LOBES, silent, 0f), eps)
    }

    @Test
    fun moundRaw_peaks_under_the_lit_lobe() {
        // Light only the body slice → the central mound is where the height is greatest.
        val h = FloatArray(SoftBlob.SLICES.size)
        h[0] = 1f
        val atCentre = SoftBlob.moundRaw(0.5f, SoftBlob.FULL_LOBES, h, 0f)
        val offCentre = SoftBlob.moundRaw(0.15f, SoftBlob.FULL_LOBES, h, 0f)
        assertTrue(atCentre > offCentre)
        // The central peak with a full body slice is ~its amp scale (gaussian≈1 there).
        assertTrue(atCentre > 0.9f)
    }

    @Test
    fun moundRaw_overlapping_lobes_add() {
        // Two neighbouring slices lit → the sum between their lobes exceeds either
        // lobe alone at that same x (the metaball merge).
        val one = FloatArray(SoftBlob.SLICES.size).also { it[1] = 1f }
        val both = FloatArray(SoftBlob.SLICES.size).also { it[1] = 1f; it[0] = 1f }
        val x = 0.4f
        assertTrue(SoftBlob.moundRaw(x, SoftBlob.FULL_LOBES, both, 0f) >
            SoftBlob.moundRaw(x, SoftBlob.FULL_LOBES, one, 0f))
    }

    @Test
    fun moundRaw_is_nan_safe_on_a_poisoned_height() {
        val h = FloatArray(SoftBlob.SLICES.size).also { it[0] = Float.NaN; it[1] = 1f }
        val raw = SoftBlob.moundRaw(0.31f, SoftBlob.FULL_LOBES, h, 0f)
        assertTrue(raw.isFinite())
        assertTrue(raw > 0f) // slice 1 still contributes; the NaN slice is skipped
    }

    // ------------------------------------------------------------ saturation / silhouette budget

    @Test
    fun saturate_is_bounded_monotone_and_nan_safe() {
        assertEquals(0f, SoftBlob.saturate(0f), eps)
        assertTrue(SoftBlob.saturate(1f) < 1f)
        assertTrue(SoftBlob.saturate(100f) < 1f)
        assertTrue(SoftBlob.saturate(2f) > SoftBlob.saturate(1f))
        assertEquals(0f, SoftBlob.saturate(Float.NaN), eps)
        assertEquals(0f, SoftBlob.saturate(-5f), eps)
    }

    @Test
    fun fill_ceiling_leaves_headroom_for_breathing() {
        // The core no-mesa invariant: even breathing at its peak cannot reach the rail.
        assertTrue(SoftBlob.FILL_CEIL * (1f + SoftBlob.BREATHE_AMP) < 1f)
    }

    @Test
    fun silhouette_never_reaches_the_rail_at_any_raw_or_breathe() {
        val availHalf = 90f
        val maxBreathe = 1f + SoftBlob.BREATHE_AMP
        for (raw in listOf(0f, 0.5f, 1f, 5f, 50f, 1000f)) {
            val ext = SoftBlob.silhouettePx(raw, availHalf, maxBreathe)
            assertTrue("raw=$raw must stay inside the half", ext < availHalf)
            assertTrue(ext >= 0f)
        }
    }

    @Test
    fun silhouette_is_monotone_in_raw() {
        val availHalf = 90f
        assertTrue(SoftBlob.silhouettePx(0.2f, availHalf, 1f) < SoftBlob.silhouettePx(2f, availHalf, 1f))
        assertEquals(0f, SoftBlob.silhouettePx(0f, availHalf, 1f), eps)
    }

    @Test
    fun silhouette_at_40dp_stays_inside_the_box() {
        // 40 dp @ density 1 → small; the available half is tiny, yet the outline
        // must still sit strictly inside it (no mesa on the player bar).
        val h = 40f
        val inset = SoftBlob.railInsetPx(h, SoftBlob.INSET_MIN_DP)
        val availHalf = SoftBlob.availableHalfPx(h, inset)
        assertTrue(availHalf > 0f)
        val ext = SoftBlob.silhouettePx(1000f, availHalf, 1f + SoftBlob.BREATHE_AMP)
        assertTrue(ext < availHalf)
    }

    @Test
    fun silhouette_is_nan_safe() {
        assertEquals(0f, SoftBlob.silhouettePx(Float.NaN, 90f, 1f), eps)
    }

    // ------------------------------------------------------------ small-height / rails

    @Test
    fun small_height_degrade_engages_at_40dp_only() {
        assertTrue(SoftBlob.isSmallHeight(40f, SoftBlob.SMALL_HEIGHT_DP))
        assertFalse(SoftBlob.isSmallHeight(52f, SoftBlob.SMALL_HEIGHT_DP))
        assertFalse(SoftBlob.isSmallHeight(140f, SoftBlob.SMALL_HEIGHT_DP))
    }

    @Test
    fun available_half_is_never_negative() {
        assertEquals(0f, SoftBlob.availableHalfPx(2f, 10f), eps)
        assertTrue(SoftBlob.availableHalfPx(200f, SoftBlob.railInsetPx(200f, 1f)) > 0f)
    }

    // ------------------------------------------------------------ occlusion budget (SrcOver white)

    @Test
    fun white_alpha_rides_between_its_bounds() {
        assertEquals(SoftBlob.WHITE_ALPHA_MIN, SoftBlob.whiteAlpha(0f), eps)
        assertEquals(SoftBlob.WHITE_ALPHA_MAX, SoftBlob.whiteAlpha(1f), eps)
        val mid = SoftBlob.whiteAlpha(0.5f)
        assertTrue(mid > SoftBlob.WHITE_ALPHA_MIN && mid < SoftBlob.WHITE_ALPHA_MAX)
    }

    @Test
    fun stacked_white_occlusion_stays_under_the_ceiling_even_two_voice() {
        // One fill occludes exactly its alpha; two stacked (the two-voice overlap)
        // occlude 1-(1-a)^2. Both must clear the ≤55% budget.
        assertEquals(SoftBlob.WHITE_ALPHA_MAX, SoftBlob.stackedOcclusion(1), eps)
        val two = SoftBlob.stackedOcclusion(2)
        assertEquals(1f - (1f - SoftBlob.WHITE_ALPHA_MAX) * (1f - SoftBlob.WHITE_ALPHA_MAX), two, eps)
        assertTrue("two-voice occlusion under budget", two <= SoftBlob.OCCLUSION_CEILING)
    }

    // ------------------------------------------------------------ white-out budget (additive)

    @Test
    fun glow_and_core_alphas_ride_between_their_bounds() {
        assertEquals(SoftBlob.GLOW_ALPHA_MIN, SoftBlob.glowAlpha(0f), eps)
        assertEquals(SoftBlob.GLOW_ALPHA_MAX, SoftBlob.glowAlpha(1f), eps)
        assertEquals(SoftBlob.CORE_ALPHA_MIN, SoftBlob.coreAlpha(0f), eps)
        assertEquals(SoftBlob.CORE_ALPHA_MAX, SoftBlob.coreAlpha(1f), eps)
    }

    @Test
    fun worst_additive_sum_two_voice_stays_under_the_whiteout_ceiling() {
        val worst = SoftBlob.worstAdditiveSum(2)
        assertEquals(2f * (SoftBlob.GLOW_ALPHA_MAX + SoftBlob.CORE_ALPHA_MAX) + SoftBlob.MOTE_ALPHA_MAX, worst, eps)
        assertTrue("two-voice additive under white-out ceiling", worst < SoftBlob.WHITE_OUT_CEILING)
    }

    // ------------------------------------------------------------ two-voice interleave

    @Test
    fun interleave_shift_moves_the_two_mound_sets_apart() {
        // Human shifts LEFT (−), AI shifts RIGHT (+): the body-slice central peak
        // therefore lands left of centre for the human and right for the AI.
        val h = FloatArray(SoftBlob.SLICES.size).also { it[0] = 1f }
        val humanShift = -SoftBlob.INTERLEAVE_FRAC
        val aiShift = SoftBlob.INTERLEAVE_FRAC
        // The human's mound is taller left of centre than the AI's there, and vice versa.
        val xLeft = 0.5f - SoftBlob.INTERLEAVE_FRAC
        val xRight = 0.5f + SoftBlob.INTERLEAVE_FRAC
        assertTrue(SoftBlob.moundRaw(xLeft, SoftBlob.FULL_LOBES, h, humanShift) >
            SoftBlob.moundRaw(xLeft, SoftBlob.FULL_LOBES, h, aiShift))
        assertTrue(SoftBlob.moundRaw(xRight, SoftBlob.FULL_LOBES, h, aiShift) >
            SoftBlob.moundRaw(xRight, SoftBlob.FULL_LOBES, h, humanShift))
    }

    @Test
    fun interleave_fraction_is_positive_but_small() {
        assertTrue(SoftBlob.INTERLEAVE_FRAC > 0f)
        assertTrue(SoftBlob.INTERLEAVE_FRAC < 0.2f)
    }

    // ------------------------------------------------------------ breathing

    @Test
    fun breathe_is_flat_when_silent_and_bounded_when_loud() {
        // Silent → exactly 1 (a static mound, so the loop can park).
        for (p in listOf(0f, 1f, 3.3f, 7.7f)) {
            assertEquals(1f, SoftBlob.breatheFactor(p, 0f), eps)
        }
        // Loud → oscillates but stays inside [1-AMP, 1+AMP] (silhouette budget holds).
        for (p in 0..200) {
            val f = SoftBlob.breatheFactor(p * 0.05f, 1f)
            assertTrue(f in (1f - SoftBlob.BREATHE_AMP - eps)..(1f + SoftBlob.BREATHE_AMP + eps))
        }
        assertTrue(SoftBlob.breatheFactor(Float.NaN, 1f).isFinite())
    }

    // ------------------------------------------------------------ motes: bounded, deterministic

    @Test
    fun motes_stay_inside_the_box_for_every_index_time_and_speaker() {
        val w = 600f
        val h = 200f
        val centre = h / 2f
        val availHalf = SoftBlob.availableHalfPx(h, SoftBlob.railInsetPx(h, 1f))
        for (sp in AuroraSpeaker.entries) {
            for (m in 0 until SoftBlob.MOTE_COUNT) {
                for (t in 0..120) {
                    val phase = t * 0.1f
                    val x = SoftBlob.moteX(m, phase, w, sp.ordinal)
                    val y = SoftBlob.moteY(m, phase, centre, availHalf, sp.ordinal)
                    assertTrue("x in box", x in 0f..w)
                    assertTrue("y in box", y in 0f..h)
                }
            }
        }
    }

    @Test
    fun mote_paths_are_deterministic_and_dt_independent() {
        // Pure function of (index, wall-clock phase): the same phase → the same
        // position no matter how many frames drew it (60 Hz == 120 Hz).
        assertEquals(SoftBlob.moteX(3, 2.5f, 600f, 0), SoftBlob.moteX(3, 2.5f, 600f, 0), 0f)
        assertEquals(SoftBlob.moteY(3, 2.5f, 100f, 80f, 0), SoftBlob.moteY(3, 2.5f, 100f, 80f, 0), 0f)
        // A different wall-clock phase generally moves it (the mote drifts).
        assertTrue(SoftBlob.moteX(3, 2.5f, 600f, 0) != SoftBlob.moteX(3, 9.0f, 600f, 0))
    }

    @Test
    fun mote_positions_are_nan_safe_on_a_bad_phase() {
        assertTrue(SoftBlob.moteX(1, Float.NaN, 600f, 0).isFinite())
        assertTrue(SoftBlob.moteY(1, Float.NaN, 100f, 80f, 0).isFinite())
    }

    @Test
    fun red_and_blue_motes_do_not_coincide() {
        // The per-speaker phase offset keeps the two mote fields apart.
        val x0 = SoftBlob.moteX(2, 1.0f, 600f, AuroraSpeaker.HUMAN.ordinal)
        val x1 = SoftBlob.moteX(2, 1.0f, 600f, AuroraSpeaker.AI.ordinal)
        assertTrue(x0 != x1)
    }

    @Test
    fun mote_alpha_fades_out_below_the_gate_and_grows_with_level() {
        assertEquals(0f, SoftBlob.moteAlpha(0f), eps)
        assertEquals(0f, SoftBlob.moteAlpha(SoftBlob.MOTE_LEVEL_GATE - 0.001f), eps)
        assertTrue(SoftBlob.moteAlpha(0.5f) > 0f)
        assertTrue(SoftBlob.moteAlpha(1f) <= SoftBlob.MOTE_ALPHA_MAX + eps)
        assertTrue(SoftBlob.moteAlpha(1f) > SoftBlob.moteAlpha(0.3f))
    }

    @Test
    fun mote_radius_grows_with_level_and_is_bounded() {
        val minDim = 200f
        val quiet = SoftBlob.moteRadiusPx(0, 0.05f, minDim)
        val loud = SoftBlob.moteRadiusPx(0, 1f, minDim)
        assertTrue(loud > quiet)
        assertTrue(loud <= minDim * (SoftBlob.MOTE_RADIUS_MIN_FRAC + SoftBlob.MOTE_RADIUS_LEVEL_FRAC) + eps)
        assertTrue(quiet > 0f)
    }

    @Test
    fun mote_twinkle_stays_between_its_floor_and_one() {
        for (m in 0 until SoftBlob.MOTE_COUNT) {
            for (t in 0..80) {
                val v = SoftBlob.moteTwinkle(m, t * 0.1f)
                assertTrue(v in (SoftBlob.MOTE_TWINKLE_FLOOR - eps)..(1f + eps))
            }
        }
    }

    // ------------------------------------------------------------ idle park

    @Test
    fun needsFrames_false_when_silent_and_empty() {
        val feed = WaveformFeed()
        assertFalse(softBlobRenderer().needsFrames(emptyList(), feed))
    }

    @Test
    fun needsFrames_reads_the_passed_voices_never_the_feed() {
        // The park-deadlock invariant: the feed's rings stop advancing once the loop
        // parks, so a style that asked THE FEED whether it was audible could never
        // wake up. This feed is deliberately never stepped.
        val feed = WaveformFeed()
        val voices = listOf(AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.8f, 0.8f, 0.8f, 0.8f)))
        assertEquals(0f, feed.level(AuroraSpeaker.HUMAN), eps)
        assertTrue(softBlobRenderer().needsFrames(voices, feed))
    }

    // ------------------------------------------------------------ sample steps

    @Test
    fun sample_steps_are_clamped_for_tiny_and_huge_surfaces() {
        assertEquals(24, SoftBlob.sampleSteps(1f))
        assertEquals(96, SoftBlob.sampleSteps(100000f))
        assertTrue(SoftBlob.sampleSteps(360f) in 24..96)
    }
}
