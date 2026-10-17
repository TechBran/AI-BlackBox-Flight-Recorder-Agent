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
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Layered Particle Wave — the pure geometry, depth ladder, spectral slicing,
 * budgets and envelope logic in [ParticleWave], plus the audible branch of the
 * renderer's [needsFrames]. Pure JVM in the aurora tradition: everything the look
 * depends on is a function of its inputs, so it is pinned here without a device or
 * a Canvas.
 *
 * What is pinned: the depth stack (3 full / 2 small) and its ratio ladder, the
 * SLICE-per-layer mapping (front body … back air) and the requirement that NO
 * analyser band is discarded by it, the slice map (tiling, bin ownership,
 * log-interpolated band fallback), the shared M7 acceptance rule — two spectra at
 * equal overall LEVEL must draw differently — on both the band and the bin path,
 * the travelling surface geometry and its Aurora-calibrated crest speeds, the
 * hard-capped dot budget, the no-mesa dot-extent budget at ANY depth/height incl.
 * 40 dp, the ZERO-occlusion + bounded-additive white-out budgets with their exact
 * numbers, the two-voice invariants (opposite travel, π bias, depth-interleaved
 * tones), the asymmetric attack/release envelope draining to rest, twinkle
 * steadiness when silent, NaN/empty safety, and dt-independent motion.
 */
class ParticleWaveTest {

    private val eps = 1e-4f

    /** Geometric centre of an analyser band, from the real probe pair — never restated. */
    private fun bandCentreHz(band: Int): Float {
        val p = AuroraAnalyser.PROBES[band]
        return sqrt(p.first().toDouble() * p.last()).toFloat()
    }

    private fun drivesFor(bands: FloatArray, bins: FloatArray? = null, level: Float = 1f): FloatArray {
        val out = FloatArray(ParticleWave.SLICES.size)
        ParticleWave.sliceDrives(bands, bins, level, out)
        return out
    }

    // ------------------------------------------------------------ depth stack

    @Test
    fun full_stack_is_three_layers_small_is_two() {
        assertEquals(3, ParticleWave.layerCount(false))
        assertEquals(2, ParticleWave.layerCount(true))
        assertEquals(3, ParticleWave.FULL_LAYERS)
        assertEquals(2, ParticleWave.SMALL_LAYERS)
    }

    @Test
    fun depth_ratio_runs_front_zero_to_back_one_for_both_stacks() {
        assertEquals(0f, ParticleWave.depthT(0, 3), eps)
        assertEquals(0.5f, ParticleWave.depthT(1, 3), eps)
        assertEquals(1f, ParticleWave.depthT(2, 3), eps)
        assertEquals(0f, ParticleWave.depthT(0, 2), eps)
        assertEquals(1f, ParticleWave.depthT(1, 2), eps)
        // Degenerate counts do not divide by zero.
        assertEquals(0f, ParticleWave.depthT(0, 1), eps)
        assertEquals(0f, ParticleWave.depthT(5, 0), eps)
    }

    @Test
    fun small_height_degrade_engages_at_40dp_only() {
        assertTrue(ParticleWave.isSmallHeight(40f, ParticleWave.SMALL_HEIGHT_DP))
        assertFalse(ParticleWave.isSmallHeight(52f, ParticleWave.SMALL_HEIGHT_DP))
        assertFalse(ParticleWave.isSmallHeight(140f, ParticleWave.SMALL_HEIGHT_DP))
    }

    @Test
    fun available_half_is_never_negative() {
        assertEquals(0f, ParticleWave.availableHalfPx(2f, 10f), eps)
        assertTrue(ParticleWave.availableHalfPx(200f, ParticleWave.railInsetPx(200f, 1f)) > 0f)
    }

    // ------------------------------------------------------------ slice per layer

    @Test
    fun front_layer_follows_the_low_slice_back_the_high_slice() {
        // Full stack: front → slice 0 (body), back → the top slice, each layer its own.
        assertEquals(0, ParticleWave.layerSlice(0, 3))
        assertEquals(ParticleWave.SLICES.size - 1, ParticleWave.layerSlice(2, 3))
        val full = (0 until 3).map { ParticleWave.layerSlice(it, 3) }
        assertEquals("a layer repeats another's slice: $full", full.size, full.toSet().size)
        assertTrue(full == full.sorted())
        // Small stack: front body, back air — the widest separation two surfaces can show.
        assertEquals(0, ParticleWave.layerSlice(0, 2))
        assertEquals(ParticleWave.SLICES.size - 1, ParticleWave.layerSlice(1, 2))
    }

    @Test
    fun every_layer_slice_is_in_range() {
        for (count in intArrayOf(2, 3)) {
            for (layer in 0 until count) {
                assertTrue(ParticleWave.layerSlice(layer, count) in ParticleWave.SLICES.indices)
            }
        }
        // Out-of-range layer indices are clamped, never crash.
        assertTrue(ParticleWave.layerSlice(99, 3) in ParticleWave.SLICES.indices)
    }

    // ------------------------------------------------------------ the slice map

    @Test
    fun slices_tile_the_spectrum_map_end_to_end() {
        val slices = ParticleWave.SLICES
        assertEquals(AuroraSpectrum.MIN_HZ, slices.first().loHz, eps)
        assertEquals(AuroraSpectrum.MAX_HZ, slices.last().hiHz, eps)
        for (i in slices.indices) {
            assertTrue("slice $i is empty", slices[i].hiHz > slices[i].loHz)
            if (i > 0) {
                // Contiguous: no gap (a frequency driving nothing) and no overlap
                // (two surfaces sharing energy).
                assertEquals(slices[i - 1].hiHz, slices[i].loHz, eps)
                assertTrue("centres ascend", slices[i].centreHz > slices[i - 1].centreHz)
            }
        }
    }

    @Test
    fun every_slice_owns_at_least_one_spectrum_bin_and_together_they_own_them_all() {
        var total = 0
        for (i in ParticleWave.SLICES.indices) {
            val n = ParticleWave.sliceBinCount(i)
            assertTrue("slice $i owns no bin", n > 0)
            total += n
        }
        // The tiling is exact: every log bin belongs to exactly one slice.
        assertEquals(AuroraAnalyser.BINS, total)
    }

    @Test
    fun no_analyser_band_is_discarded_by_the_layer_map() {
        // THE defect this style shipped with: the layers indexed bands {0,1,3}, so
        // band 2 — 1.6–2.6 kHz, where most of speech's intelligibility lives — moved
        // nothing at all. Every band must now lift some layer of the FULL stack.
        val fullSlices = (0 until ParticleWave.FULL_LAYERS).map { ParticleWave.layerSlice(it, ParticleWave.FULL_LAYERS) }
        for (b in 0 until AuroraAnalyser.BANDS) {
            val bands = FloatArray(AuroraAnalyser.BANDS).also { it[b] = 1f }
            val drives = drivesFor(bands)
            assertTrue("band $b lifts no layer", fullSlices.any { drives[it] > 0.01f })
        }
    }

    @Test
    fun bands_are_read_at_a_frequency_by_log_interpolation() {
        val bands = floatArrayOf(0.1f, 0.9f, 0.2f, 0.5f)
        // At a band's own centre the reading IS that band.
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertEquals(bands[b], ParticleWave.bandAtHz(bands, bandCentreHz(b)), 1e-3f)
        }
        // Between two centres it sits between the two values.
        val mid = ParticleWave.bandAtHz(bands, sqrt(bandCentreHz(1) * bandCentreHz(2)))
        assertTrue(mid < bands[1] && mid > bands[2])
        // Outside the probe range it clamps to the end bands rather than extrapolating.
        assertEquals(bands[0], ParticleWave.bandAtHz(bands, 20f), eps)
        assertEquals(bands[3], ParticleWave.bandAtHz(bands, 20000f), eps)
        // Total on garbage.
        assertEquals(0f, ParticleWave.bandAtHz(bands, Float.NaN), eps)
        assertEquals(0f, ParticleWave.bandAtHz(FloatArray(0), 1000f), eps)
    }

    // ------------------------------------------ M7: spectrum, not just level

    @Test
    fun two_spectra_at_equal_level_drive_the_stack_differently() {
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
        val a = drivesFor(low)
        val b = drivesFor(high)
        // Not merely different: the two tilts must reverse which end of the stack rises.
        assertTrue("low-tilted spectrum did not favour the near surface", a[0] > a[a.size - 1])
        assertTrue("high-tilted spectrum did not favour the far surface", b[b.size - 1] > b[0])
    }

    @Test
    fun with_a_spectrum_published_the_shape_comes_from_the_bins() {
        // Same bands, same level, two different SPECTRA: the bins must reshape the
        // depth stack. Anything less means the 24-bin channel is being ignored.
        val bands = floatArrayOf(0.5f, 0.5f, 0.5f, 0.5f)
        val lowTilt = FloatArray(AuroraAnalyser.BINS) { if (it < AuroraAnalyser.BINS / 2) 0.8f else 0.05f }
        val highTilt = FloatArray(AuroraAnalyser.BINS) { if (it < AuroraAnalyser.BINS / 2) 0.05f else 0.8f }
        val a = drivesFor(bands, lowTilt, 0.7f)
        val b = drivesFor(bands, highTilt, 0.7f)
        var worst = 0f
        for (i in a.indices) worst = maxOf(worst, abs(a[i] - b[i]))
        assertTrue("the two spectra produced the same drives", worst > 0.2f)
        assertTrue(a[0] > a[a.size - 1])
        assertTrue(b[b.size - 1] > b[0])
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
        for (d in drivesFor(bands, FloatArray(AuroraAnalyser.BINS), 0.6f)) assertEquals(0f, d, eps)
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
        ParticleWave.sliceDrives(floatArrayOf(1f, 1f, 1f, 1f), null, 1f, short)
        for (d in short) assertTrue(d > 0f)
    }

    // ------------------------------------------------------ aerial perspective ladder

    @Test
    fun front_layer_is_taller_bigger_brighter_and_faster_than_the_back() {
        val front = 0f
        val back = 1f
        assertTrue("amp recedes", ParticleWave.ampFrac(front) > ParticleWave.ampFrac(back))
        assertTrue("dots shrink", ParticleWave.radiusDp(front) > ParticleWave.radiusDp(back))
        assertTrue("core dims", ParticleWave.coreAlpha(front, 1f) > ParticleWave.coreAlpha(back, 1f))
        assertTrue("glow dims", ParticleWave.glowAlpha(front, 1f) > ParticleWave.glowAlpha(back, 1f))
        assertTrue("crest slows", ParticleWave.crestSpeedWidthsPerSec(front) > ParticleWave.crestSpeedWidthsPerSec(back))
        assertTrue("more cycles near", ParticleWave.cycles(front) > ParticleWave.cycles(back))
    }

    @Test
    fun radius_dp_sits_in_the_reference_one_and_a_half_to_two_and_a_half() {
        for (t in listOf(0f, 0.5f, 1f)) {
            val r = ParticleWave.radiusDp(t)
            assertTrue(r in 1.5f..2.5f)
        }
    }

    // ------------------------------------------------------ the travelling surface

    @Test
    fun crest_speed_matches_the_aurora_calibrated_band() {
        // Every layer's crest speed sits inside Aurora v33's device-tuned
        // 0.25..0.37 widths/sec "alive but not scrolling" band.
        for (t in listOf(0f, 0.25f, 0.5f, 0.75f, 1f)) {
            val s = ParticleWave.crestSpeedWidthsPerSec(t)
            assertTrue("speed $s in band", s in 0.25f..0.37f)
        }
    }

    @Test
    fun angular_rate_realises_the_intended_crest_speed() {
        // rate / (2π·cycles) must equal the target crest speed at every depth — the
        // derivation the constants pin (hold the sine argument constant, differentiate).
        for (t in listOf(0f, 0.4f, 1f)) {
            val recovered = ParticleWave.angularRate(t) / ((2.0 * PI).toFloat() * ParticleWave.cycles(t))
            assertEquals(ParticleWave.crestSpeedWidthsPerSec(t), recovered, 1e-4f)
        }
    }

    @Test
    fun wave_unit_is_bounded_and_closes_to_zero_at_both_ends() {
        // |waveUnit| ≤ 1 everywhere (taper ≤ 1, sin ≤ 1), and the ends taper to 0 so
        // the dot field fades in/out rather than being chopped.
        for (i in 0..100) {
            val x = i / 100f
            val v = ParticleWave.waveUnit(x, 3.3f, 0.5f, 1f, 0f)
            assertTrue(v in -1f..1f)
        }
        assertEquals(0f, ParticleWave.waveUnit(0f, 3.3f, 0f, 1f, 0f), eps)
        assertEquals(0f, ParticleWave.waveUnit(1f, 3.3f, 0f, 1f, 0f), eps)
    }

    @Test
    fun taper_is_one_in_the_middle_and_zero_at_the_edges() {
        assertEquals(1f, ParticleWave.taper(0.5f), eps)
        assertEquals(0f, ParticleWave.taper(0f), eps)
        assertEquals(0f, ParticleWave.taper(1f), eps)
        assertTrue(ParticleWave.taper(0.05f) < 1f)
    }

    @Test
    fun wave_unit_moves_with_time_and_is_dt_independent() {
        // Pure in wall-clock phase: same phase → same value (60 Hz == 120 Hz), and a
        // different phase generally moves the surface (it travels).
        val a = ParticleWave.waveUnit(0.4f, 2.5f, 0f, 1f, 0f)
        val b = ParticleWave.waveUnit(0.4f, 2.5f, 0f, 1f, 0f)
        assertEquals(a, b, 0f)
        assertNotEquals(a, ParticleWave.waveUnit(0.4f, 6.0f, 0f, 1f, 0f))
    }

    @Test
    fun wave_unit_is_nan_safe_on_a_bad_phase() {
        assertTrue(ParticleWave.waveUnit(0.4f, Float.NaN, 0.5f, 1f, 0f).isFinite())
    }

    // ------------------------------------------------------------ two-voice contract

    @Test
    fun the_two_voices_travel_in_opposite_directions() {
        assertEquals(1f, ParticleWave.travelDir(AuroraSpeaker.HUMAN), eps)
        assertEquals(-1f, ParticleWave.travelDir(AuroraSpeaker.AI), eps)
    }

    @Test
    fun the_ai_carries_a_pi_phase_bias_so_the_fields_never_coincide() {
        assertEquals(0f, ParticleWave.phaseBias(AuroraSpeaker.HUMAN), eps)
        assertEquals(PI.toFloat(), ParticleWave.phaseBias(AuroraSpeaker.AI), eps)
        // At the most-observed instant (phase 0) the two surfaces differ, so the two
        // voices are visibly apart from the first frame.
        val human = ParticleWave.waveUnit(0.4f, 0f, 0.5f, ParticleWave.travelDir(AuroraSpeaker.HUMAN), ParticleWave.phaseBias(AuroraSpeaker.HUMAN))
        val ai = ParticleWave.waveUnit(0.4f, 0f, 0.5f, ParticleWave.travelDir(AuroraSpeaker.AI), ParticleWave.phaseBias(AuroraSpeaker.AI))
        assertNotEquals(human, ai)
    }

    @Test
    fun depth_tones_read_identity_front_grey_back_white_between() {
        // Full stack: front = identity hue, mid = white, back = grey.
        assertEquals(ParticleWave.Tone.IDENTITY, ParticleWave.layerTone(0, 3))
        assertEquals(ParticleWave.Tone.WHITE, ParticleWave.layerTone(1, 3))
        assertEquals(ParticleWave.Tone.GREY, ParticleWave.layerTone(2, 3))
        // Small stack: front identity, back grey (no white middle).
        assertEquals(ParticleWave.Tone.IDENTITY, ParticleWave.layerTone(0, 2))
        assertEquals(ParticleWave.Tone.GREY, ParticleWave.layerTone(1, 2))
    }

    // ------------------------------------------------------------ dot budget (hard cap)

    @Test
    fun dot_counts_are_hard_capped_and_recede_with_depth() {
        // Front densest, back sparsest — aerial perspective in the dot density too.
        assertTrue(ParticleWave.FULL_DOTS[0] > ParticleWave.FULL_DOTS[2])
        assertTrue(ParticleWave.SMALL_DOTS[0] > ParticleWave.SMALL_DOTS[1])
        // The documented ceiling holds for the whole stack, both voices.
        assertTrue("full total under cap", ParticleWave.totalDots(false) <= ParticleWave.HARD_CAP_DOTS)
        assertTrue("small total under cap", ParticleWave.totalDots(true) <= ParticleWave.HARD_CAP_DOTS)
        assertEquals(2 * (78 + 60 + 46), ParticleWave.totalDots(false))
        assertEquals(2 * (42 + 34), ParticleWave.totalDots(true))
    }

    @Test
    fun small_stack_dots_are_about_forty_per_layer() {
        for (n in ParticleWave.SMALL_DOTS) assertTrue(n in 30..50)
    }

    @Test
    fun dots_for_layer_clamps_a_stray_index() {
        assertEquals(ParticleWave.FULL_DOTS[2], ParticleWave.dotsForLayer(99, false))
        assertEquals(ParticleWave.SMALL_DOTS[1], ParticleWave.dotsForLayer(99, true))
    }

    // ------------------------------------------------------------ dot placement

    @Test
    fun dot_base_x_is_inside_the_width_and_deterministic() {
        for (sp in AuroraSpeaker.entries) {
            for (layer in 0 until 3) {
                val count = ParticleWave.dotsForLayer(layer, false)
                for (d in 0 until count) {
                    val x = ParticleWave.dotBaseXFrac(d, count, layer, sp.ordinal)
                    assertTrue("x in [0,1]", x in 0f..1f)
                    // Deterministic: same inputs → same position, frame to frame.
                    assertEquals(x, ParticleWave.dotBaseXFrac(d, count, layer, sp.ordinal), 0f)
                }
            }
        }
    }

    @Test
    fun red_and_blue_dot_fields_do_not_coincide() {
        // The per-speaker salt in the jitter keeps the two fields from overlapping exactly.
        val h = ParticleWave.dotBaseXFrac(5, 60, 1, AuroraSpeaker.HUMAN.ordinal)
        val a = ParticleWave.dotBaseXFrac(5, 60, 1, AuroraSpeaker.AI.ordinal)
        assertNotEquals(h, a)
    }

    @Test
    fun dot_scatter_is_a_static_unit_offset() {
        for (sp in AuroraSpeaker.entries) {
            for (d in 0 until 40) {
                val s = ParticleWave.dotScatterUnit(d, 0, sp.ordinal)
                assertTrue(s in -1f..1f)
                // Static (time-independent) → a fixed thickness, not animated noise.
                assertEquals(s, ParticleWave.dotScatterUnit(d, 0, sp.ordinal), 0f)
            }
        }
    }

    // ------------------------------------------------------------ dot size / no-mesa

    @Test
    fun dot_radius_is_capped_to_a_share_of_the_half() {
        // On a big surface the dp radius wins; on a tiny one the cap protects the rail.
        val bigHalf = 200f
        val tinyHalf = 6f
        val dp = ParticleWave.radiusDp(0f) // ~2.5 dp @ density 1
        assertTrue(ParticleWave.dotRadiusPx(dp, bigHalf) <= bigHalf * ParticleWave.DOT_RADIUS_HALF_CAP + eps)
        assertEquals(dp, ParticleWave.dotRadiusPx(dp, bigHalf), eps) // uncapped on big
        assertEquals(tinyHalf * ParticleWave.DOT_RADIUS_HALF_CAP, ParticleWave.dotRadiusPx(dp, tinyHalf), eps) // capped on tiny
    }

    @Test
    fun dot_radius_is_nan_and_negative_safe() {
        assertEquals(0f, ParticleWave.dotRadiusPx(Float.NaN, 100f), eps)
        assertEquals(0f, ParticleWave.dotRadiusPx(-3f, 100f), eps)
        assertEquals(0f, ParticleWave.dotRadiusPx(5f, -100f), eps)
    }

    @Test
    fun no_dot_ever_reaches_the_rail_at_any_depth() {
        // The core no-mesa invariant: amplitude + scatter + radius < the available
        // half at every layer, so nothing clips or mesas — the binding case is the
        // front (tallest amp, biggest dot).
        for (t in listOf(0f, 0.5f, 1f)) {
            assertTrue("depth $t extent < 1", ParticleWave.maxDotExtentFrac(t) < 1f)
        }
        // Front is the binding layer and still leaves headroom.
        assertTrue(ParticleWave.maxDotExtentFrac(0f) >= ParticleWave.maxDotExtentFrac(1f))
    }

    @Test
    fun no_mesa_holds_concretely_at_40dp() {
        // Rebuild the 40 dp budget in px: the furthest dot must sit strictly inside
        // the available half at the smallest shipped height.
        val h = 40f
        val inset = ParticleWave.railInsetPx(h, ParticleWave.INSET_MIN_DP)
        val availHalf = ParticleWave.availableHalfPx(h, inset)
        assertTrue(availHalf > 0f)
        // Front layer at full drive: amp + scatter + capped radius.
        val amp = availHalf * ParticleWave.ampFrac(0f) * 1f
        val scatter = availHalf * ParticleWave.SCATTER_FRAC
        val radius = ParticleWave.dotRadiusPx(ParticleWave.radiusDp(0f), availHalf)
        assertTrue(amp + scatter + radius < availHalf)
    }

    // ------------------------------------------------------------ occlusion budget

    @Test
    fun the_style_never_occludes_the_field() {
        // Every pass is additive light: zero SrcOver coverage, trivially under ≤55%.
        assertEquals(0f, ParticleWave.OCCLUSION_COVERAGE, eps)
        assertTrue(ParticleWave.OCCLUSION_COVERAGE <= ParticleWave.OCCLUSION_CEILING)
    }

    // ------------------------------------------------------------ white-out budget

    @Test
    fun a_single_dot_never_blows_to_white() {
        assertTrue(ParticleWave.brightestDotAdditive() < 1f)
        // Front core+glow at full level is the brightest single dot.
        assertEquals(ParticleWave.coreAlpha(0f, 1f) + ParticleWave.glowAlpha(0f, 1f), ParticleWave.brightestDotAdditive(), eps)
    }

    @Test
    fun one_voices_whole_stack_stays_under_the_whiteout_ceiling() {
        val worst = ParticleWave.worstVoiceStackAdditive(3)
        assertTrue("stack under ceiling", worst < ParticleWave.WHITE_OUT_CEILING)
        // The small 2-layer stack is lighter still.
        assertTrue(ParticleWave.worstVoiceStackAdditive(2) < worst)
    }

    @Test
    fun alpha_rides_up_from_a_faint_floor_to_its_depth_max() {
        for (t in listOf(0f, 0.5f, 1f)) {
            val quiet = ParticleWave.coreAlpha(t, 0f)
            val loud = ParticleWave.coreAlpha(t, 1f)
            assertTrue(loud > quiet)
            assertTrue(quiet > 0f) // even silent-but-present keeps a faint dot
        }
    }

    // ------------------------------------------------------------ twinkle

    @Test
    fun twinkle_is_steady_when_silent_so_the_field_can_park() {
        // Level 0 → exactly 1 at every phase: a quiet field does not shimmer, so the
        // idle park can suspend the loop.
        for (p in listOf(0f, 1f, 3.3f, 7.7f)) {
            assertEquals(1f, ParticleWave.twinkle(3, p, 0f), eps)
        }
    }

    @Test
    fun twinkle_only_dims_and_stays_bounded_when_loud() {
        for (d in 0 until 20) {
            for (t in 0..120) {
                val v = ParticleWave.twinkle(d, t * 0.1f, 1f)
                assertTrue(v in (1f - ParticleWave.TWINKLE_DEPTH - eps)..(1f + eps))
            }
        }
    }

    @Test
    fun twinkle_is_deterministic_dt_independent_and_nan_safe() {
        assertEquals(ParticleWave.twinkle(4, 2.5f, 1f), ParticleWave.twinkle(4, 2.5f, 1f), 0f)
        assertTrue(ParticleWave.twinkle(4, Float.NaN, 1f).isFinite())
        assertEquals(1f, ParticleWave.twinkle(4, 2.5f, Float.NaN), eps) // NaN level → steady
    }

    // ------------------------------------------------------------ the one curve

    @Test
    fun the_style_applies_the_apps_one_perceptual_curve_and_never_its_own() {
        // Not "sqrt" restated — the app has exactly one curve and this style calls it,
        // so a re-tune of Aurora.DISPLAY_GAMMA moves the dot field with it.
        for (v in listOf(0f, 0.05f, 0.25f, 0.5f, 1f)) {
            assertEquals(Aurora.displayCurve(v), ParticleWave.shaped(v), 1e-6f)
        }
        assertEquals(1f, ParticleWave.shaped(4f), eps)      // clamped
        assertEquals(0f, ParticleWave.shaped(Float.NaN), eps)
    }

    @Test
    fun rest_floor_clamps_and_is_nan_safe() {
        assertEquals(0f, ParticleWave.restFloor(0f), eps)
        assertEquals(0.06f, ParticleWave.restFloor(0.06f), eps)
        assertEquals(1f, ParticleWave.restFloor(9f), eps)
        assertEquals(0f, ParticleWave.restFloor(Float.NaN), eps)
    }

    // ------------------------------------------------ NO private envelope

    /**
     * THE ENVELOPE IS GONE, and this test is what stops it coming back.
     *
     * The style used to carry per-slice attack/release state across frames — a THIRD
     * smoother behind the analyser's auto-gain release and Aurora's release ladder.
     * Measured on a whole utterance it cost 9% of this style's drawn range and pushed
     * its 90%-fall from 219 ms to 460 ms, i.e. two syllable periods, which is the
     * operator's "movement isn't fluent" in the time domain. Soft Blob deleted the
     * identical stage for the identical reason.
     *
     * Asserted STRUCTURALLY rather than by behaviour, because "there is no hidden
     * state" is not something a single draw can show: the surface that made the
     * envelope possible is checked to be absent, so re-adding one cannot compile past
     * this file without someone deleting the test.
     */
    @Test
    fun the_style_has_no_private_smoothing_stage_left_to_stack() {
        val gone = listOf("ATTACK_TAU_SEC", "RELEASE_TAU_SEC", "SETTLE_EPS", "INK_EPS")
        val fields = ParticleWave.javaClass.declaredFields.map { it.name }
        for (name in gone) {
            assertFalse(
                "$name is back — this style must not smooth on top of Aurora's release ladder",
                fields.contains(name),
            )
        }
        val methods = ParticleWave.javaClass.declaredMethods.map { it.name }
        for (name in listOf("approach", "rowHasInk")) {
            assertFalse("$name is back — see the note on the deleted envelope", methods.contains(name))
        }
    }

    /**
     * ...and the drive a surface is handed is a pure function of THIS frame. Two
     * renderers fed the identical single frame draw the identical picture whatever
     * dt says, which an attack/release stage could not satisfy (its output would
     * depend on how much wall clock had passed).
     */
    @Test
    fun a_slice_drive_is_a_pure_function_of_this_frame() {
        val floor = ParticleWave.restFloor(Aurora.BASELINE)
        for (v in listOf(0f, 0.01f, 0.2f, 0.75f, 1f)) {
            val once = Aurora.restBlend(floor, ParticleWave.shaped(v))
            val again = Aurora.restBlend(floor, ParticleWave.shaped(v))
            assertEquals(once, again, 0f)
        }
    }

    // ------------------------------------------------------------ idle park

    @Test
    fun needsFrames_false_when_silent_and_empty() {
        val feed = WaveformFeed()
        assertFalse(particleWaveRenderer().needsFrames(emptyList(), feed))
    }

    @Test
    fun needsFrames_reads_the_passed_voices_never_the_feed() {
        // The park-deadlock invariant: the feed's rings stop advancing once the loop
        // parks, so a style that asked THE FEED whether it was audible could never
        // wake up. This feed is deliberately never stepped.
        val feed = WaveformFeed()
        val voices = listOf(AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.8f, 0.8f, 0.8f, 0.8f)))
        assertEquals(0f, feed.level(AuroraSpeaker.HUMAN), eps)
        assertTrue(particleWaveRenderer().needsFrames(voices, feed))
    }
}
