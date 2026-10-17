package com.aiblackbox.portal.ui.components.wave.styles

import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.EnvelopeRing
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Sharp Reactive Line — the pure geometry and per-segment colour DECISIONS in
 * [SharpLineMath], plus the history-style idle-park invariant on the renderer's
 * [needsFrames]. Pure JVM in the aurora tradition: everything the look depends on
 * is a function of its inputs, so it is pinned here without a device or a Canvas.
 *
 * What is pinned: newest-at-right scroll geometry, amplitude never clipping the
 * box (incl. two-voice and the 40 dp degrade), the 70th-percentile-plus-floor red
 * gate (a silent window stays grey), red/cool brightness ramps, NaN/empty safety,
 * deterministic dt-independent noise, and that a departed voice's tail keeps the
 * frame loop alive until it drains.
 */
class SharpLineTest {

    private val eps = 1e-4f

    // ------------------------------------------------------------ geometry: scroll

    @Test
    fun columnX_newest_at_right_oldest_at_left() {
        val w = 800f
        val cols = 150
        // k = 0 is the newest → right edge; k = cols-1 is the oldest → left edge.
        assertEquals(w, SharpLineMath.columnX(0, cols, w), eps)
        assertEquals(0f, SharpLineMath.columnX(cols - 1, cols, w), eps)
        // Monotonic: a step older is a step left.
        assertTrue(SharpLineMath.columnX(1, cols, w) < SharpLineMath.columnX(0, cols, w))
        assertTrue(SharpLineMath.columnX(10, cols, w) < SharpLineMath.columnX(9, cols, w))
    }

    @Test
    fun columnX_single_column_is_finite_no_divide_by_zero() {
        val x = SharpLineMath.columnX(0, 1, 500f)
        assertTrue(x.isFinite())
        assertEquals(500f, x, eps) // pinned to the right edge
    }

    // ------------------------------------------------------------ geometry: amplitude / centre

    @Test
    fun ampPx_one_voice_leaves_a_margin_and_never_clips() {
        val h = 200f
        val amp = SharpLineMath.ampPx(h, twoVoice = false, small = false)
        // A full-loudness peak must stay strictly inside the half-box (no mesa).
        assertTrue(amp < h / 2f)
        assertEquals(h * SharpLineMath.AMP_FRACTION, amp, eps)
    }

    @Test
    fun ampPx_two_voice_and_small_are_progressively_smaller() {
        val h = 200f
        val one = SharpLineMath.ampPx(h, twoVoice = false, small = false)
        val two = SharpLineMath.ampPx(h, twoVoice = true, small = false)
        val twoSmall = SharpLineMath.ampPx(h, twoVoice = true, small = true)
        assertTrue(two < one)
        assertTrue(twoSmall < two)
    }

    @Test
    fun centreY_one_voice_is_dead_centre_two_voice_splits_human_above_ai_below() {
        val h = 200f
        assertEquals(h / 2f, SharpLineMath.centreY(AuroraSpeaker.HUMAN, h, twoVoice = false), eps)
        val human = SharpLineMath.centreY(AuroraSpeaker.HUMAN, h, twoVoice = true)
        val ai = SharpLineMath.centreY(AuroraSpeaker.AI, h, twoVoice = true)
        // Smaller y is higher on screen: human above, AI below.
        assertTrue(human < h / 2f)
        assertTrue(ai > h / 2f)
        assertEquals(h * SharpLineMath.OFFSET_FRACTION, (h / 2f) - human, eps)
        assertEquals(h * SharpLineMath.OFFSET_FRACTION, ai - (h / 2f), eps)
    }

    @Test
    fun two_voice_peaks_and_noise_stay_inside_the_box_at_every_extreme() {
        // The worst cases: a full-loudness peak with the noise pushing further out,
        // and a silent sample with the noise at its opposite extreme. Both voices,
        // both directions, must land inside [0, h] — the "never clips / never
        // mesas" budget, computed analytically from the same constants.
        val h = 200f
        val amp = SharpLineMath.ampPx(h, twoVoice = true, small = false)
        val noise = h * SharpLineMath.NOISE_FRACTION
        for (speaker in AuroraSpeaker.entries) {
            val centre = SharpLineMath.centreY(speaker, h, twoVoice = true)
            val highest = centre - 1f * amp - noise // loud peak + noise pushing up
            val lowest = centre - 0f * amp + noise  // silent + noise pushing down
            assertTrue("top edge for $speaker", highest >= 0f)
            assertTrue("bottom edge for $speaker", lowest <= h)
        }
    }

    @Test
    fun small_height_degrade_engages_at_40dp_and_shrinks_amplitude() {
        // 40 dp bar at density 1: 40 px < 52 px threshold → small.
        assertTrue(SharpLineMath.isSmall(40f, SharpLineMath.SMALL_HEIGHT_DP))
        assertFalse(SharpLineMath.isSmall(120f, SharpLineMath.SMALL_HEIGHT_DP))
        assertFalse(SharpLineMath.isSmall(0f, SharpLineMath.SMALL_HEIGHT_DP))
        val h = 40f
        val normal = SharpLineMath.ampPx(h, twoVoice = false, small = false)
        val degraded = SharpLineMath.ampPx(h, twoVoice = false, small = true)
        assertTrue(degraded < normal)
        // Even the un-degraded 40 dp peak is inside the box (fraction-of-height).
        assertTrue(normal < h / 2f)
    }

    // ------------------------------------------------------------ red gate: percentile

    @Test
    fun percentile_is_the_70th_of_the_visible_window() {
        val ring = EnvelopeRing(8)
        // 0.0 .. 0.7 — sorted is the same; 70th percentile index = round(7*0.7)=5 → 0.5.
        for (v in 0..7) ring.push(v / 10f)
        assertEquals(0.5f, SharpLineMath.percentileThreshold(ring, SharpLineMath.HOT_PERCENTILE), eps)
    }

    @Test
    fun percentile_returns_one_when_window_too_short_or_empty() {
        val empty = EnvelopeRing(8)
        assertEquals(1f, SharpLineMath.percentileThreshold(empty, 0.7f), eps)
        val short = EnvelopeRing(8)
        // One below MIN_HOT_SAMPLES → still "nothing hot yet".
        repeat(SharpLineMath.MIN_HOT_SAMPLES - 1) { short.push(0.9f) }
        assertEquals(1f, SharpLineMath.percentileThreshold(short, 0.7f), eps)
    }

    // ------------------------------------------------------------ red gate: hot / intensity

    @Test
    fun isHot_requires_both_the_percentile_and_the_absolute_floor() {
        val threshold = 0.5f
        assertTrue(SharpLineMath.isHot(0.6f, threshold))  // clears both
        assertTrue(SharpLineMath.isHot(0.5f, threshold))  // exactly at threshold
        assertFalse(SharpLineMath.isHot(0.4f, threshold)) // below the window's top slice
    }

    @Test
    fun isHot_keeps_a_silent_window_grey() {
        // A quiet window: its own 70th percentile is tiny, but the absolute floor
        // blocks red so a silent line is never "all peaks".
        val threshold = 0.05f
        assertFalse(SharpLineMath.isHot(0.05f, threshold))
        assertFalse(SharpLineMath.isHot(0.10f, threshold))
        assertTrue(SharpLineMath.isHot(0.20f, threshold)) // a real transient still lights
    }

    @Test
    fun isHot_is_nan_safe() {
        assertFalse(SharpLineMath.isHot(Float.NaN, 0.5f))
    }

    @Test
    fun redIntensity_ramps_threshold_to_peak_and_clamps() {
        val threshold = 0.6f
        assertEquals(0f, SharpLineMath.redIntensity(0.6f, threshold), eps)      // at threshold
        assertEquals(1f, SharpLineMath.redIntensity(1.0f, threshold), eps)      // tallest peak
        assertEquals(0.5f, SharpLineMath.redIntensity(0.8f, threshold), eps)    // midway
        assertEquals(0f, SharpLineMath.redIntensity(0.5f, threshold), eps)      // below → clamped
    }

    @Test
    fun redIntensity_guards_threshold_of_one() {
        assertEquals(1f, SharpLineMath.redIntensity(1.0f, 1.0f), eps)
    }

    @Test
    fun coolIntensity_greys_when_quiet_brightens_toward_the_threshold() {
        val threshold = 0.5f
        assertEquals(0f, SharpLineMath.coolIntensity(0f, threshold), eps)
        assertEquals(0.5f, SharpLineMath.coolIntensity(0.25f, threshold), eps)
        assertEquals(1f, SharpLineMath.coolIntensity(0.5f, threshold), eps)
        assertEquals(1f, SharpLineMath.coolIntensity(0.9f, threshold), eps) // clamped
    }

    @Test
    fun coolIntensity_guards_threshold_of_zero() {
        assertEquals(0f, SharpLineMath.coolIntensity(0.3f, 0f), eps)
    }

    // ------------------------------------------------------- spectral sharpness

    @Test
    fun brightness_reads_the_high_bands_against_the_loudest_one() {
        // Relative, not absolute: what makes a line sharp is that the HIGH content is
        // dominant right now, not that it cleared some fixed level. (Absolute band
        // levels are not comparable across bands on live audio — the measured spread
        // is over an order of magnitude — so an absolute gate would never fire.)
        assertEquals("a body-only vowel is not bright", 0f, SharpLineMath.brightness(0.9f, 0.6f, 0f, 0f), eps)
        assertEquals("a sibilant is fully bright", 1f, SharpLineMath.brightness(0f, 0f, 0.2f, 0.8f), eps)
        // Half-and-half sits in between and is monotonic in the high bands.
        val mid = SharpLineMath.brightness(0.8f, 0.8f, 0.4f, 0.2f)
        assertTrue(mid > 0f && mid < 1f)
        assertTrue(SharpLineMath.brightness(0.8f, 0.8f, 0.6f, 0.2f) > mid)
    }

    @Test
    fun brightness_is_zero_on_silence_and_nan_safe() {
        assertEquals(0f, SharpLineMath.brightness(0f, 0f, 0f, 0f), 0f)
        val n = SharpLineMath.brightness(Float.NaN, 0.5f, Float.NaN, 0.1f)
        assertTrue(n.isFinite())
        assertTrue(n in 0f..1f)
    }

    @Test
    fun a_dull_line_is_EXACTLY_the_shipped_line() {
        // The spectral jag is an ADDITION. With no high content there is no jag, and
        // every column draws the level it always drew — the case that proves the new
        // behaviour did not move the old picture.
        for (k in 0 until 16) {
            assertEquals(0.62f, SharpLineMath.jaggedLevel(0.62f, 0f, k), eps)
        }
    }

    @Test
    fun a_bright_line_alternates_into_real_spikes() {
        // Sharpness IS the style. Bright content pulls alternate columns DOWN, so the
        // polyline zig-zags between neighbours instead of drawing a smooth envelope —
        // and because the jag only ever subtracts, the peak is never taller than the
        // level, which is what keeps the pinned no-clip budget valid untouched.
        val level = 0.8f
        val even = SharpLineMath.jaggedLevel(level, 1f, 0)
        val odd = SharpLineMath.jaggedLevel(level, 1f, 1)
        assertEquals("even columns keep the full level", level, even, eps)
        assertTrue("odd columns must dip", odd < even)
        assertEquals(level * (1f - SharpLineMath.JAG_DEPTH), odd, eps)
        // Deeper brightness → deeper zig.
        assertTrue(SharpLineMath.jaggedLevel(level, 0.3f, 1) > SharpLineMath.jaggedLevel(level, 0.9f, 1))
    }

    @Test
    fun the_jag_never_exceeds_the_level_at_any_input() {
        var level = 0f
        while (level <= 1.0001f) {
            var b = 0f
            while (b <= 1.0001f) {
                for (k in 0 until 8) {
                    val v = SharpLineMath.jaggedLevel(level, b, k)
                    assertTrue("jag must never lift a peak", v <= level + eps)
                    assertTrue("jag must never go negative", v >= -eps)
                }
                b += 0.25f
            }
            level += 0.25f
        }
        // NaN in either input collapses to something drawable, never a NaN vertex.
        assertTrue(SharpLineMath.jaggedLevel(Float.NaN, 1f, 3).isFinite())
        assertTrue(SharpLineMath.jaggedLevel(0.5f, Float.NaN, 3).isFinite())
    }

    @Test
    fun two_spectra_at_the_SAME_level_draw_different_lines() {
        // THE acceptance test. Identical loudness in both windows; only the spectrum
        // differs. The drawn column series must not be identical.
        val level = 0.7f
        val dull = FloatArray(8) { SharpLineMath.jaggedLevel(level, SharpLineMath.brightness(0.9f, 0.5f, 0.02f, 0.01f), it) }
        val bright = FloatArray(8) { SharpLineMath.jaggedLevel(level, SharpLineMath.brightness(0.05f, 0.1f, 0.6f, 0.9f), it) }
        assertFalse("equal level must NOT imply an equal line", dull.contentEquals(bright))
    }

    @Test
    fun a_bright_peak_burns_hotter_than_a_dull_one_of_the_same_height() {
        // Colour answers the spectrum too: the red gate still decides WHICH segments
        // are peaks (loudness), but how hot a peak burns also depends on what is in it.
        val heat = 0.4f
        assertEquals("dull is exactly the loudness heat", heat, SharpLineMath.spectralHeat(heat, 0f), eps)
        assertTrue(SharpLineMath.spectralHeat(heat, 1f) > heat)
        // Bounded and monotonic in both arguments.
        var h = 0f
        while (h <= 1.0001f) {
            var b = 0f
            while (b <= 1.0001f) {
                val v = SharpLineMath.spectralHeat(h, b)
                assertTrue("heat $v out of range", v in 0f..1f)
                b += 0.25f
            }
            assertTrue(SharpLineMath.spectralHeat(h, 0.5f) >= SharpLineMath.spectralHeat(h, 0f) - eps)
            h += 0.25f
        }
        assertTrue(SharpLineMath.spectralHeat(1f, 1f) <= 1f + eps)
        assertTrue(SharpLineMath.spectralHeat(Float.NaN, Float.NaN).isFinite())
    }

    // ------------------------------------------------------------ noise: deterministic, bounded, dt-free

    @Test
    fun noiseOffset_is_bounded_finite_and_nan_safe() {
        for (k in 0 until 200) {
            val n = SharpLineMath.noiseOffset(k, 1.234f)
            assertTrue(n.isFinite())
            assertTrue(n in -1f..1f)
        }
        assertTrue(SharpLineMath.noiseOffset(3, Float.NaN).isFinite()) // NaN phase → finite
    }

    @Test
    fun noiseOffset_is_deterministic_and_dt_independent() {
        // Pure function of (column, wall-clock phase): the SAME phase yields the
        // SAME texture no matter how many frames drew it — 60 Hz and 120 Hz agree.
        assertEquals(
            SharpLineMath.noiseOffset(17, 2.5f),
            SharpLineMath.noiseOffset(17, 2.5f),
            0f,
        )
        // A different wall-clock phase generally moves it (the line shimmers).
        assertTrue(
            SharpLineMath.noiseOffset(17, 2.5f) != SharpLineMath.noiseOffset(17, 9.0f),
        )
    }

    @Test
    fun noiseOffset_varies_by_column_so_the_quiet_line_is_never_dead_flat() {
        val values = (0 until 32).map { SharpLineMath.noiseOffset(it, 0.5f) }.toSet()
        // Many distinct values across columns — a textured line, not a flat rule.
        assertTrue(values.size > 8)
    }

    // ------------------------------------------------------------ ring energy / idle park

    @Test
    fun ringHasEnergy_true_while_a_sample_is_above_rest_false_when_drained() {
        val ring = EnvelopeRing(4)
        assertFalse(SharpLineMath.ringHasEnergy(ring)) // empty
        ring.push(0.5f)
        assertTrue(SharpLineMath.ringHasEnergy(ring))
        // Drain: push zeros until the loud sample scrolls off the 4-slot ring.
        repeat(4) { ring.push(0f) }
        assertFalse(SharpLineMath.ringHasEnergy(ring))
    }

    @Test
    fun needsFrames_false_when_silent_and_empty() {
        val feed = WaveformFeed()
        assertFalse(sharpLineRenderer().needsFrames(emptyList(), feed))
    }

    @Test
    fun needsFrames_true_while_a_voice_is_audible() {
        val feed = WaveformFeed()
        val voices = listOf(human(0.8f, 0.8f, 0.8f, 0.8f))
        feed.step(0.02f, voices)
        assertTrue(sharpLineRenderer().needsFrames(voices, feed))
    }

    @Test
    fun needsFrames_keeps_the_scroll_alive_after_the_voice_leaves_then_parks() {
        // A departed voice's tail must keep the loop running until it has scrolled
        // off — the history-style override, not the default audible-only park.
        val feed = WaveformFeed(historySec = 0.1f, cadenceHz = 50f) // small ring, drains fast
        val renderer = sharpLineRenderer()
        val loud = listOf(human(0.9f, 0.9f, 0.9f, 0.9f))
        // Fill the ring with energy.
        repeat(10) { feed.step(0.02f, loud) }
        // The voice leaves the list, but its tail is still on screen.
        feed.step(0.02f, emptyList())
        assertTrue("tail still scrolling", renderer.needsFrames(emptyList(), feed))
        // Keep stepping silence until the ring drains — then it parks.
        repeat(20) { feed.step(0.02f, emptyList()) }
        assertFalse("drained → parked", renderer.needsFrames(emptyList(), feed))
    }

    private fun human(vararg bands: Float) = AuroraVoice(AuroraSpeaker.HUMAN, bands)
}
