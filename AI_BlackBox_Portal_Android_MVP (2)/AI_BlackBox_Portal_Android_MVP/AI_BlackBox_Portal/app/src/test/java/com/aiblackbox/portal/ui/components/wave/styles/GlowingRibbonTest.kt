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
import kotlin.math.PI
import kotlin.math.abs

/**
 * Glowing Ribbon — the braid's pure geometry, its crest-speed calibration, the strand
 * fan/weave offsets, the SPECTRAL shape terms, the drive shaping and idle floor, the
 * visual budgets (occlusion is zero by construction; white-out bounded) and the 40 dp
 * degrade, in the aurora tradition: everything the renderer draws is a pure function
 * of [RibbonMath], so it is pinned here without a device or a Canvas.
 *
 * What is pinned: the 7/4 strand counts, crest crossings inside 0.6–1.0 s with a
 * per-strand shear, the ω-from-speed round trip, the phase fan, the spectral
 * descriptors (volume-invariant centroid and spread) and everything they bend, sqrt
 * drive shaping + idle-floor/player-bar-zero, amplitude that never clips the box
 * (incl. two-voice and the 40 dp degrade), the tapered-to-a-floor ends, the additive
 * white-out ceiling and the glow-weighted brightness split, NaN/empty safety, dt
 * independence, and the direction-is-identity two-voice invariant.
 *
 * THE M7 ACCEPTANCE TEST is `equal_level_spectra_with_different_content_draw
 * _different_shapes`: two spectra at the SAME overall level must not merely differ in
 * height, they must differ in SHAPE once the amplitude is divided out.
 */
class GlowingRibbonTest {

    private val eps = 1e-4f

    private fun human(vararg bands: Float) = AuroraVoice(AuroraSpeaker.HUMAN, bands)
    private fun ai(vararg bands: Float) = AuroraVoice(AuroraSpeaker.AI, bands)

    /** The normalised (amplitude divided out) strand outline — the SHAPE, sampled across the width. */
    private fun shape(
        centroid: Float,
        spread: Float,
        drive: Float,
        j: Int = 0,
        dir: Float = 1f,
        phaseSec: Float = 1.3f,
    ): FloatArray = FloatArray(33) { i ->
        RibbonMath.wave(i / 32f, phaseSec, dir, j, centroid, spread, drive)
    }

    private fun maxAbsDiff(a: FloatArray, b: FloatArray): Float {
        var m = 0f
        for (i in a.indices) m = maxOf(m, abs(a[i] - b[i]))
        return m
    }

    // ------------------------------------------------------------ strand count / degrade

    @Test
    fun full_bundle_is_seven_strands_small_is_four() {
        assertEquals(7, RibbonMath.strandCount(small = false))
        assertEquals(4, RibbonMath.strandCount(small = true))
        assertTrue(RibbonMath.strandCount(small = true) < RibbonMath.strandCount(small = false))
    }

    @Test
    fun small_height_degrade_engages_at_40dp_only() {
        assertTrue(RibbonMath.isSmall(40f, RibbonMath.SMALL_HEIGHT_DP))
        assertFalse(RibbonMath.isSmall(52f, RibbonMath.SMALL_HEIGHT_DP))
        assertFalse(RibbonMath.isSmall(140f, RibbonMath.SMALL_HEIGHT_DP))
        assertFalse(RibbonMath.isSmall(0f, RibbonMath.SMALL_HEIGHT_DP))
    }

    // ------------------------------------------------------------ crest speed (M7 recalibration)

    @Test
    fun a_crest_crosses_the_width_in_well_under_a_second() {
        // THE operator-directed recalibration. The old band (0.25–0.37 widths/sec)
        // took a crest 2.7–4.0 SECONDS to cross, which read as a still picture with a
        // slow drift. Every strand now crosses inside 0.6–1.0 s.
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            val cross = RibbonMath.crossSeconds(j)
            assertTrue("strand $j crosses in ${cross}s — too slow", cross <= RibbonMath.CROSS_SEC_MAX + eps)
            assertTrue("strand $j crosses in ${cross}s — too fast", cross >= RibbonMath.CROSS_SEC_MIN - eps)
        }
        assertEquals(RibbonMath.CROSS_SEC_MAX, RibbonMath.crossSeconds(0), 1e-3f)
        assertEquals(
            RibbonMath.CROSS_SEC_MIN,
            RibbonMath.crossSeconds(RibbonMath.STRANDS_FULL - 1),
            0.01f,
        )
    }

    @Test
    fun crest_speed_band_matches_the_crossing_times_it_is_derived_from() {
        // speed and crossing time are one calibration expressed two ways; the
        // constants must not be able to drift apart.
        assertEquals(RibbonMath.SPEED_MIN, 1f / RibbonMath.CROSS_SEC_MAX, 1e-3f)
        assertEquals(RibbonMath.SPEED_MAX, 1f / RibbonMath.CROSS_SEC_MIN, 0.03f)
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            val s = RibbonMath.crestSpeed(j)
            assertTrue("strand $j speed $s below band", s >= RibbonMath.SPEED_MIN - eps)
            assertTrue("strand $j speed $s above band", s <= RibbonMath.SPEED_MAX + eps)
        }
        assertEquals(RibbonMath.SPEED_MIN, RibbonMath.crestSpeed(0), eps)
        assertEquals(RibbonMath.SPEED_MAX, RibbonMath.crestSpeed(RibbonMath.STRANDS_FULL - 1), eps)
    }

    @Test
    fun crest_speed_has_a_per_strand_shear() {
        // Monotonically increasing across the bundle — the parallax shear.
        for (j in 1 until RibbonMath.STRANDS_FULL) {
            assertTrue("shear must increase at $j", RibbonMath.crestSpeed(j) > RibbonMath.crestSpeed(j - 1))
        }
    }

    @Test
    fun omega_is_solved_back_from_the_observable_crest_speed_at_any_spectrum() {
        // ω = speed·π·freq, so the observable crest speed = ω / (π·freq) round-trips
        // exactly — the calibration is on the SPEED, and bending the frequency with
        // the spectrum must therefore not change how fast a crest travels.
        for (c in floatArrayOf(0f, 0.25f, 0.5f, 0.8f, 1f)) {
            for (j in 0 until RibbonMath.STRANDS_FULL) {
                val speed = RibbonMath.omega(j, c) / (PI.toFloat() * RibbonMath.frequency(j, c))
                assertEquals("strand $j at centroid $c", RibbonMath.crestSpeed(j), speed, eps)
            }
        }
    }

    // ------------------------------------------------------------ spectral descriptors

    @Test
    fun centroid_reads_where_the_energy_sits_and_is_blind_to_volume() {
        val low = floatArrayOf(1f, 0f, 0f, 0f)
        val high = floatArrayOf(0f, 0f, 0f, 1f)
        val flat = floatArrayOf(0.5f, 0.5f, 0.5f, 0.5f)
        assertEquals(0f, RibbonMath.centroid(low, 4), eps)
        assertEquals(1f, RibbonMath.centroid(high, 4), eps)
        assertEquals(0.5f, RibbonMath.centroid(flat, 4), eps)
        // VOLUME-INVARIANT: the descriptor is pure shape, so a quieter version of the
        // same voice bends the ribbon identically. This is what "responds to spectral
        // content, not to level" means as an equation.
        val loud = floatArrayOf(0.8f, 0.4f, 0.2f, 0.1f)
        val quiet = FloatArray(4) { loud[it] * 0.25f }
        assertEquals(RibbonMath.centroid(loud, 4), RibbonMath.centroid(quiet, 4), eps)
    }

    @Test
    fun centroid_is_neutral_on_silence_and_nan_safe() {
        assertEquals(RibbonMath.NEUTRAL_CENTROID, RibbonMath.centroid(FloatArray(4), 4), eps)
        assertEquals(RibbonMath.NEUTRAL_CENTROID, RibbonMath.centroid(FloatArray(24), 0), eps)
        val nasty = floatArrayOf(Float.NaN, 1f, Float.NaN, 0f)
        assertTrue(RibbonMath.centroid(nasty, 4).isFinite())
        assertTrue(RibbonMath.centroid(nasty, 4) in 0f..1f)
    }

    @Test
    fun spread_is_zero_for_one_tone_and_rises_as_the_energy_splits() {
        val tone = floatArrayOf(0f, 1f, 0f, 0f)
        val split = floatArrayOf(1f, 0f, 0f, 1f)
        val flat = floatArrayOf(0.4f, 0.4f, 0.4f, 0.4f)
        assertEquals(0f, RibbonMath.spread(tone, 4), eps)
        assertEquals(1f, RibbonMath.spread(split, 4), eps) // the widest a spectrum can be
        assertTrue(RibbonMath.spread(flat, 4) > 0f)
        assertTrue(RibbonMath.spread(flat, 4) < RibbonMath.spread(split, 4))
        // Volume-blind, exactly like the centroid.
        val quietSplit = FloatArray(4) { split[it] * 0.1f }
        assertEquals(RibbonMath.spread(split, 4), RibbonMath.spread(quietSplit, 4), eps)
        assertEquals(0f, RibbonMath.spread(FloatArray(4), 4), eps)
    }

    @Test
    fun strand_levels_slice_a_real_spectrum_and_round_robin_four_bands() {
        // 24 bins over 7 strands: each strand owns a CONTIGUOUS slice of the log map,
        // so the braid is spread across the spectrum rather than four bands repeated.
        val bins = FloatArray(AuroraAnalyser.BINS) { it / (AuroraAnalyser.BINS - 1f) }
        var prev = -1f
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            val v = RibbonMath.strandLevel(bins, AuroraAnalyser.BINS, j, RibbonMath.STRANDS_FULL)
            assertTrue("slice $j must climb with the ramp", v > prev)
            prev = v
        }
        // Fewer levels than strands (the four-band fallback every shipped call site
        // still feeds today) keeps the round robin the style shipped with.
        val bands = floatArrayOf(0.1f, 0.2f, 0.3f, 0.4f)
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            assertEquals(bands[j % 4], RibbonMath.strandLevel(bands, 4, j, RibbonMath.STRANDS_FULL), eps)
        }
        // Degenerate inputs are absorbed, never thrown from a draw loop.
        assertEquals(0f, RibbonMath.strandLevel(FloatArray(0), 0, 0, 7), 0f)
        assertTrue(RibbonMath.strandLevel(bins, AuroraAnalyser.BINS, 99, 7).isFinite())
    }

    // ------------------------------------------------------------ THE M7 acceptance test

    @Test
    fun equal_level_spectra_with_different_content_draw_different_shapes() {
        // Two spectra whose OVERALL LEVEL is identical by the shared definition
        // (WaveformFeed.levelOf, the weighted max) but whose content is opposite:
        // one is all body, the other all sibilance. The ribbon must not merely change
        // height between them — it must change SHAPE, which is what "the ribbon
        // follows the voice" means and what a pure sine of the wall clock cannot do.
        val body = floatArrayOf(0.64f, 0.10f, 0.02f, 0.01f)
        val sibilance = floatArrayOf(0.01f, 0.02f, 0.64f / 0.85f, 0.10f)
        val feed = WaveformFeed()
        feed.step(0.02f, listOf(human(*body), ai(*sibilance)))
        assertEquals(
            "the two spectra must be at the same overall level for this to prove anything",
            feed.level(AuroraSpeaker.HUMAN),
            feed.level(AuroraSpeaker.AI),
            1e-3f,
        )

        // Amplitude divided out on BOTH sides: only the shape is compared.
        val a = shape(
            RibbonMath.centroid(body, 4),
            RibbonMath.spread(body, 4),
            drive = 0.7f,
        )
        val b = shape(
            RibbonMath.centroid(sibilance, 4),
            RibbonMath.spread(sibilance, 4),
            drive = 0.7f,
        )
        assertTrue(
            "equal-level spectra drew the SAME shape — the ribbon is spectrum-blind",
            maxAbsDiff(a, b) > 0.2f,
        )
    }

    @Test
    fun the_same_spectrum_at_a_different_volume_keeps_its_shape() {
        // The control for the test above: turning the SAME voice down changes the
        // height (drive) but not the spectral descriptors, so the shape terms they
        // drive are untouched. Volume is amplitude; content is shape.
        val loud = floatArrayOf(0.9f, 0.5f, 0.2f, 0.1f)
        val quiet = FloatArray(4) { loud[it] * 0.2f }
        val a = shape(RibbonMath.centroid(loud, 4), RibbonMath.spread(loud, 4), drive = 0.5f)
        val b = shape(RibbonMath.centroid(quiet, 4), RibbonMath.spread(quiet, 4), drive = 0.5f)
        assertEquals(0f, maxAbsDiff(a, b), eps)
    }

    @Test
    fun a_brighter_voice_draws_more_humps_across_the_width() {
        // The centroid bends the FREQUENCY term: a dark voice draws long broad arcs,
        // a bright one a tighter ripple. Monotone across the whole sweep, and bounded
        // so a strand can never collapse to a straight line or a comb.
        var prev = 0f
        for (c in floatArrayOf(0f, 0.25f, 0.5f, 0.75f, 1f)) {
            val f = RibbonMath.frequency(0, c)
            assertTrue("frequency must climb with the centroid at $c", f > prev)
            prev = f
        }
        assertEquals(RibbonMath.BASE_FREQ, RibbonMath.frequency(0, RibbonMath.NEUTRAL_CENTROID), eps)
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            for (c in floatArrayOf(0f, 0.5f, 1f)) {
                assertTrue("strand $j at $c must stay a wave", RibbonMath.frequency(j, c) > 1f)
            }
            // The per-strand ladder survives the bend at every spectrum.
            if (j > 0) {
                for (c in floatArrayOf(0f, 0.5f, 1f)) {
                    assertTrue(RibbonMath.frequency(j, c) > RibbonMath.frequency(j - 1, c))
                }
            }
        }
        assertEquals(RibbonMath.BASE_FREQ + RibbonMath.FREQ_STEP, RibbonMath.frequency(1, 0.5f), eps)
    }

    @Test
    fun spectral_spread_swirls_the_strand_and_a_pure_tone_leaves_it_a_clean_sine() {
        // The spread drives a travelling PHASE modulation — the "writhe" that makes a
        // broadband, consonant-heavy passage look different from a sustained vowel.
        assertEquals(0f, RibbonMath.swirlDepth(spread = 0f, drive = 1f), eps)
        assertEquals(0f, RibbonMath.swirlDepth(spread = 1f, drive = 0f), eps)
        assertEquals(RibbonMath.SWIRL_DEPTH_MAX, RibbonMath.swirlDepth(1f, 1f), eps)
        assertEquals(0f, RibbonMath.swirlDepth(Float.NaN, Float.NaN), eps)
        val clean = shape(centroid = 0.5f, spread = 0f, drive = 0.8f)
        val swirled = shape(centroid = 0.5f, spread = 1f, drive = 0.8f)
        assertTrue("a broad spectrum must reshape the strand", maxAbsDiff(clean, swirled) > 0.2f)
    }

    @Test
    fun a_louder_strand_leads_its_quieter_neighbours() {
        // Drive enters the PHASE, not just the amplitude, so the bundle SHEARS with
        // the spectrum: the strands on the loud bands run ahead of the quiet ones.
        val quiet = shape(centroid = 0.5f, spread = 0f, drive = 0.05f)
        val loud = shape(centroid = 0.5f, spread = 0f, drive = 1f)
        assertTrue("drive must move the strand along, not only stretch it", maxAbsDiff(quiet, loud) > 0.2f)
        assertEquals(RibbonMath.DRIVE_LEAD_RAD, RibbonMath.driveLead(1f), eps)
        assertEquals(0f, RibbonMath.driveLead(0f), eps)
        assertEquals(0f, RibbonMath.driveLead(Float.NaN), eps)
    }

    @Test
    fun the_swirl_can_never_fold_the_ribbon_back_on_itself() {
        // A phase modulation deep enough to reverse the total phase would draw a
        // strand that doubles back over itself — a knot, not a braid. The design
        // bound is `SWIRL_DEPTH_MAX · SWIRL_FREQ < the lowest frequency the centroid
        // tilt can produce`, checked as an inequality AND swept numerically.
        val lowestFreq = RibbonMath.frequency(0, 0f)
        assertTrue(
            "swirl ${RibbonMath.SWIRL_DEPTH_MAX} x ${RibbonMath.SWIRL_FREQ} must stay under $lowestFreq",
            RibbonMath.SWIRL_DEPTH_MAX * RibbonMath.SWIRL_FREQ < lowestFreq,
        )
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            for (c in floatArrayOf(0f, 0.5f, 1f)) {
                for (dir in floatArrayOf(1f, -1f)) {
                    var prev = RibbonMath.totalPhase(0f, 2.1f, dir, j, c, 1f, 1f)
                    var p = 0.01f
                    while (p <= 1f) {
                        val cur = RibbonMath.totalPhase(p, 2.1f, dir, j, c, 1f, 1f)
                        assertTrue("phase reversed at p=$p j=$j c=$c dir=$dir", cur > prev)
                        prev = cur
                        p += 0.01f
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------ strand fan / weave

    @Test
    fun phase_offsets_fan_the_strands_across_the_spread() {
        // Distinct per strand, monotonic, inside [0, PHASE_SPREAD) — this fans the
        // bundle into vertical thickness instead of one overdrawn line.
        val offs = (0 until RibbonMath.STRANDS_FULL).map { RibbonMath.phaseOffset(it) }
        assertEquals(0f, offs.first(), eps)
        for (j in 1 until offs.size) assertTrue(offs[j] > offs[j - 1])
        assertTrue(offs.last() < RibbonMath.PHASE_SPREAD)
        assertEquals(RibbonMath.STRANDS_FULL, offs.toSet().size) // all distinct
    }

    @Test
    fun strand_depth_only_ever_reduces_amplitude() {
        // Depth taper varies the strand heights (body) but stays ≤ 1, so it can
        // never break the no-clip budget by lifting a strand past its peak.
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            val d = RibbonMath.strandDepth(j)
            assertTrue("strand $j depth $d must be in (0,1]", d in 0.5f..1.0f)
        }
        assertEquals(1f, RibbonMath.strandDepth(0), eps) // j%3==0 is full depth
    }

    // ------------------------------------------------------------ drive shaping / idle floor

    @Test
    fun shaped_is_the_apps_one_perceptual_curve_and_nan_safe() {
        // Not a private sqrt — this used to BE `sqrt(...)`, a second copy of the app's one
        // curve, which meant a re-tune of Aurora.DISPLAY_GAMMA left the braid behind on
        // the old exponent while every other style moved. Pinned against the shared
        // function so the two can never diverge again.
        assertEquals(0f, RibbonMath.shaped(0f), 0f)
        assertEquals(1f, RibbonMath.shaped(1f), eps)
        for (v in listOf(0.04f, 0.25f, 0.5f, 0.81f)) {
            assertEquals(Aurora.displayCurve(v), RibbonMath.shaped(v), 1e-6f)
        }
        assertTrue("quiet lifts", RibbonMath.shaped(0.04f) > 0.04f)
        assertEquals(0f, RibbonMath.shaped(Float.NaN), 0f)
        assertEquals(1f, RibbonMath.shaped(5f), 0f)
        assertEquals(0f, RibbonMath.shaped(-1f), 0f)
    }

    @Test
    fun idle_weight_leaves_only_a_few_strands_breathing() {
        // Even strands hold the full floor, odd strands a quarter — a sparse idle
        // braid, not an even comb (the plan's "few low-amplitude idle strands").
        assertEquals(1.0f, RibbonMath.idleWeight(0), eps)
        assertEquals(0.25f, RibbonMath.idleWeight(1), eps)
        assertEquals(1.0f, RibbonMath.idleWeight(2), eps)
    }

    @Test
    fun strand_drive_floors_a_live_mic_but_a_player_bar_stays_flat() {
        // Live mic (restFloor 0.06): an even idle strand sits at the floor when its
        // band is silent; the player bar (restFloor 0) leaves it at exactly 0 → not
        // drawn (below MIN_VISIBLE_DRIVE).
        assertEquals(0.06f, RibbonMath.strandDrive(bandDrive = 0f, restFloor = 0.06f, j = 0), eps)
        assertEquals(0f, RibbonMath.strandDrive(bandDrive = 0f, restFloor = 0f, j = 0), 0f)
        assertTrue(RibbonMath.strandDrive(bandDrive = 0f, restFloor = 0f, j = 0) <= RibbonMath.MIN_VISIBLE_DRIVE)
        // A loud band rides well above the idle floor, BLENDED onto it rather than
        // clamped to it (Aurora.restBlend).
        assertEquals(
            Aurora.restBlend(0.06f, Aurora.displayCurve(0.64f)),
            RibbonMath.strandDrive(bandDrive = 0.64f, restFloor = 0.06f, j = 0),
            eps,
        )
        // THE BLEND, as the property it exists for: two band drives that both sit UNDER a
        // strand's idle floor must still move the strand. Under the `max` this replaces
        // they drew the identical idle strand, which froze the even strands solid across
        // the whole quiet end of a live mic's range.
        val quietA = RibbonMath.strandDrive(bandDrive = 0.0005f, restFloor = 0.06f, j = 0)
        val quietB = RibbonMath.strandDrive(bandDrive = 0.005f, restFloor = 0.06f, j = 0)
        assertTrue("the idle floor is clamping again — quiet strands are frozen", quietB > quietA * 1.2f)
    }

    @Test
    fun sanitize_rest_absorbs_a_nan_or_out_of_range_floor() {
        assertEquals(0f, RibbonMath.sanitizeRest(Float.NaN), 0f)
        assertEquals(0f, RibbonMath.sanitizeRest(-1f), 0f)
        assertEquals(1f, RibbonMath.sanitizeRest(5f), 0f)
        assertEquals(0.06f, RibbonMath.sanitizeRest(0.06f), eps)
    }

    // ------------------------------------------------------------ amplitude: no mesa / no clip

    @Test
    fun two_voice_amplitude_is_smaller_than_one_voice_and_small_smaller_still() {
        val one = RibbonMath.ampFraction(twoVoice = false, small = false)
        val two = RibbonMath.ampFraction(twoVoice = true, small = false)
        val twoSmall = RibbonMath.ampFraction(twoVoice = true, small = true)
        assertTrue(two < one)
        assertTrue(twoSmall < two)
        assertEquals(RibbonMath.AMP_FRACTION, one, eps)
    }

    @Test
    fun no_strand_ever_clips_the_box_at_any_shipped_size_mode_or_drive() {
        // The exact defect the geometry is budgeted to avoid: a full-drive strand
        // clipping flat against a rail. Sweep every shipped height, both densities,
        // both voice modes, both speakers, every strand and every drive/position —
        // the UN-clamped excursion must land strictly inside [0, h].
        val heightsDp = floatArrayOf(40f, 52f, 140f)
        val densities = floatArrayOf(1f, 2.75f)
        for (density in densities) {
            for (hDp in heightsDp) {
                val h = hDp * density
                val small = RibbonMath.isSmall(h, RibbonMath.SMALL_HEIGHT_DP * density)
                for (twoVoice in booleanArrayOf(false, true)) {
                    for (speaker in AuroraSpeaker.entries) {
                        val centre = RibbonMath.centreY(speaker, h, twoVoice)
                        val strands = RibbonMath.strandCount(small)
                        for (j in 0 until strands) {
                            val amp = RibbonMath.ampPx(h, drive = 1f, j = j, twoVoice = twoVoice, small = small)
                            // taper ≤ 1 and |wave| ≤ 1, so the true excursion never
                            // exceeds amp: centre ± amp must be inside the box.
                            val highest = centre - amp
                            val lowest = centre + amp
                            assertTrue("clip top h=$h two=$twoVoice $speaker j=$j", highest >= 0f)
                            assertTrue("clip bottom h=$h two=$twoVoice $speaker j=$j", lowest <= h)
                        }
                    }
                }
            }
        }
    }

    @Test
    fun full_drive_single_voice_keeps_measurable_headroom() {
        val h = 140f
        val amp = RibbonMath.ampPx(h, drive = 1f, j = 0, twoVoice = false, small = false)
        val centre = RibbonMath.centreY(AuroraSpeaker.HUMAN, h, twoVoice = false)
        assertEquals(RibbonMath.AMP_FRACTION * h, amp, eps)
        assertTrue("headroom above", centre - amp > 0f)
        assertTrue("headroom below", centre + amp < h)
    }

    @Test
    fun centre_splits_human_above_ai_below_in_two_voice_mode() {
        val h = 200f
        assertEquals(h / 2f, RibbonMath.centreY(AuroraSpeaker.HUMAN, h, twoVoice = false), eps)
        val human = RibbonMath.centreY(AuroraSpeaker.HUMAN, h, twoVoice = true)
        val aiC = RibbonMath.centreY(AuroraSpeaker.AI, h, twoVoice = true)
        assertTrue("human rides above centre", human < h / 2f)
        assertTrue("ai rides below centre", aiC > h / 2f)
        assertEquals(h * RibbonMath.OFFSET_FRACTION, h / 2f - human, eps)
        assertEquals(h * RibbonMath.OFFSET_FRACTION, aiC - h / 2f, eps)
    }

    // ------------------------------------------------------------ taper (fanned ends)

    @Test
    fun taper_is_one_in_the_middle_and_eases_to_a_nonzero_floor_at_the_ends() {
        // A non-zero end floor is deliberate: it keeps the strands FANNED at the
        // ends (by their phase offsets) instead of pinching every strand onto one
        // shared tip where they would pile up additively.
        assertEquals(1f, RibbonMath.taper(0.5f), eps)
        assertEquals(RibbonMath.TAPER_END_FLOOR, RibbonMath.taper(0f), eps)
        assertEquals(RibbonMath.TAPER_END_FLOOR, RibbonMath.taper(1f), eps)
        assertTrue("floor must be > 0 to fan the ends", RibbonMath.TAPER_END_FLOOR > 0f)
        // Bounded and monotone in from the edge.
        var p = 0f
        while (p <= 1f) {
            val t = RibbonMath.taper(p)
            assertTrue("taper $t in range at p=$p", t in RibbonMath.TAPER_END_FLOOR..1f)
            p += 0.02f
        }
    }

    // ------------------------------------------------------------ the wave

    @Test
    fun wave_is_bounded_finite_and_nan_safe() {
        for (j in 0 until RibbonMath.STRANDS_FULL) {
            var p = 0f
            while (p <= 1f) {
                val v = RibbonMath.wave(p, 3.2f, 1f, j, centroid = 0.7f, spread = 0.6f, drive = 0.8f)
                assertTrue(v in -1f..1f)
                assertTrue(v.isFinite())
                p += 0.05f
            }
        }
        // Garbage in any channel must not take a frame down — it freezes, finite.
        assertTrue(RibbonMath.wave(0.3f, Float.NaN, 1f, 0, 0.5f, 0.5f, 0.5f).isFinite())
        assertTrue(RibbonMath.wave(0.3f, 1f, 1f, 0, Float.NaN, Float.NaN, Float.NaN).isFinite())
    }

    @Test
    fun strand_y_is_finite_and_inside_the_box_even_with_a_nan_phase() {
        val h = 140f
        val centre = RibbonMath.centreY(AuroraSpeaker.HUMAN, h, twoVoice = false)
        val amp = RibbonMath.ampPx(h, drive = 1f, j = 0, twoVoice = false, small = false)
        var p = 0f
        while (p <= 1f) {
            val y = RibbonMath.strandY(p, h, centre, amp, Float.NaN, 1f, 0, 0.5f, 0.5f, 1f)
            assertTrue(y.isFinite())
            assertTrue(y in 0f..h)
            p += 0.05f
        }
    }

    // ------------------------------------------------------------ dt / refresh-rate independence

    @Test
    fun motion_is_a_pure_function_of_wall_clock_phase() {
        // Same wall-clock phase → identical geometry, no matter how many frames
        // drew it: 60 Hz and 120 Hz agree by construction (no frame count anywhere).
        val a = RibbonMath.wave(0.4f, 2.5f, 1f, 3, 0.4f, 0.5f, 0.6f)
        val b = RibbonMath.wave(0.4f, 2.5f, 1f, 3, 0.4f, 0.5f, 0.6f)
        assertEquals(a, b, 0f)
        // A later wall-clock instant generally moves the crest (it travels).
        assertNotEquals(a, RibbonMath.wave(0.4f, 4.0f, 1f, 3, 0.4f, 0.5f, 0.6f))
    }

    // ------------------------------------------------------------ direction is identity

    @Test
    fun travel_direction_is_opposite_per_speaker() {
        assertEquals(1f, RibbonMath.travelDirection(AuroraSpeaker.HUMAN), eps)
        assertEquals(-1f, RibbonMath.travelDirection(AuroraSpeaker.AI), eps)
    }

    @Test
    fun the_two_bundles_travel_opposite_ways_at_the_same_speed() {
        // Identity costs the two voices nothing individually — same wave, same
        // speed, aimed the other way. At a non-zero phase the human and AI crests
        // sit at mirror-image places, which is what reads as "braiding through".
        val hg = RibbonMath.wave(0.3f, 1.7f, RibbonMath.travelDirection(AuroraSpeaker.HUMAN), 2, 0.5f, 0.5f, 0.7f)
        val ag = RibbonMath.wave(0.3f, 1.7f, RibbonMath.travelDirection(AuroraSpeaker.AI), 2, 0.5f, 0.5f, 0.7f)
        assertTrue(hg.isFinite() && ag.isFinite())
        assertNotEquals(hg, ag)
    }

    // ------------------------------------------------------------ visual budget

    @Test
    fun occlusion_budget_is_met_by_construction_every_pass_is_additive() {
        // There are no SrcOver passes — glow and core are both BlendMode.Plus — so
        // the ≤55% coverage ceiling is met at 0%. The only budget that can bind is
        // white-out, pinned below.
        val worst = RibbonMath.worstAdditiveStack()
        assertTrue("worst stack $worst must stay under the ceiling", worst < RibbonMath.WHITE_OUT_CEILING)
        // 4 coincident strands × (core + glow) = 1.52, echoing Mirrored Pulse. The M7
        // glow rebalance moved the SPLIT between the two, never their sum, so the
        // per-pixel white-out budget is byte-identical to what shipped.
        assertEquals(1.52f, worst, eps)
    }

    @Test
    fun the_glow_carries_the_majority_of_the_light() {
        // The operator's note: "the glow is great but underused". Weighted by the
        // width each pass actually covers (the halo is GLOW_MULT× the core), the glow
        // used to contribute 0.24 against the core's 0.30 — a minority. It is now the
        // dominant term, which is the whole point of the rebalance.
        assertTrue("glow must dominate the light", RibbonMath.glowLightShare() > 0.5f)
        assertTrue("core must still define the strand", RibbonMath.glowLightShare() < 0.8f)
        assertTrue(RibbonMath.GLOW_ALPHA_MAX > 0.12f)
        assertTrue(RibbonMath.CORE_ALPHA_MAX > RibbonMath.GLOW_ALPHA_MAX)
    }

    @Test
    fun brightness_ramps_from_a_floor_up_with_heat_within_bounds() {
        // A present idle strand still glows (the floor); loud drives toward white.
        assertEquals(RibbonMath.CORE_ALPHA_MAX * RibbonMath.BRIGHT_FLOOR, RibbonMath.coreAlpha(0f), eps)
        assertEquals(RibbonMath.CORE_ALPHA_MAX, RibbonMath.coreAlpha(1f), eps)
        assertEquals(RibbonMath.GLOW_ALPHA_MAX * RibbonMath.BRIGHT_FLOOR, RibbonMath.glowAlpha(0f), eps)
        assertEquals(RibbonMath.GLOW_ALPHA_MAX, RibbonMath.glowAlpha(1f), eps)
        assertTrue("louder is brighter", RibbonMath.coreAlpha(0.9f) > RibbonMath.coreAlpha(0.1f))
        var hh = 0f
        while (hh <= 1f) {
            assertTrue(RibbonMath.coreAlpha(hh) in 0f..RibbonMath.CORE_ALPHA_MAX)
            assertTrue(RibbonMath.glowAlpha(hh) in 0f..RibbonMath.GLOW_ALPHA_MAX)
            hh += 0.1f
        }
    }

    @Test
    fun brightness_is_nan_safe() {
        assertEquals(RibbonMath.CORE_ALPHA_MAX * RibbonMath.BRIGHT_FLOOR, RibbonMath.coreAlpha(Float.NaN), eps)
        assertEquals(RibbonMath.GLOW_ALPHA_MAX * RibbonMath.BRIGHT_FLOOR, RibbonMath.glowAlpha(Float.NaN), eps)
    }

    // ------------------------------------------------------------ sampling

    @Test
    fun sample_count_is_capped_floored_and_smooth_at_the_busiest_spectrum() {
        assertEquals(RibbonMath.SAMPLE_MIN, RibbonMath.sampleCount(1f))
        assertEquals(RibbonMath.SAMPLE_MAX, RibbonMath.sampleCount(100000f))
        val mid = RibbonMath.sampleCount(600f)
        assertTrue(mid in RibbonMath.SAMPLE_MIN..RibbonMath.SAMPLE_MAX)
        // The centroid can push the top strand to its busiest, so the FLOOR on the
        // sample count has to be read against that frequency, not the resting one, or
        // a bright voice on a narrow surface draws a polygon instead of a wave.
        val busiestHumps = RibbonMath.frequency(RibbonMath.STRANDS_FULL - 1, 1f) / 2f
        assertTrue(
            "only ${RibbonMath.SAMPLE_MIN / busiestHumps} vertices per hump at the busiest spectrum",
            RibbonMath.SAMPLE_MIN / busiestHumps >= 7f,
        )
    }

    // ------------------------------------------------------------ two-voice via the real feed

    @Test
    fun both_speakers_present_reads_each_band_independently() {
        // The two-voice contract end to end: feed both, and each bundle's strand j
        // is driven by its OWN speaker's spectrum, never the other's.
        val feed = WaveformFeed()
        val voices = listOf(human(0.81f, 0f, 0f, 0f), ai(0f, 0.49f, 0f, 0f))
        feed.step(0.02f, voices)
        assertTrue(feed.present(AuroraSpeaker.HUMAN) && feed.present(AuroraSpeaker.AI))
        val hBands = FloatArray(4) { feed.band(AuroraSpeaker.HUMAN, it) }
        val aBands = FloatArray(4) { feed.band(AuroraSpeaker.AI, it) }
        val n = AuroraAnalyser.BANDS
        val s = RibbonMath.STRANDS_FULL
        assertEquals(
            Aurora.displayCurve(0.81f),
            RibbonMath.strandDrive(RibbonMath.strandLevel(hBands, n, 0, s), 0f, 0),
            eps,
        )
        assertEquals(0f, RibbonMath.strandDrive(RibbonMath.strandLevel(aBands, n, 0, s), 0f, 0), 0f)
        assertEquals(
            Aurora.displayCurve(0.49f),
            RibbonMath.strandDrive(RibbonMath.strandLevel(aBands, n, 1, s), 0f, 1),
            eps,
        )
    }

    @Test
    fun a_fresh_feed_draws_nothing_and_the_renderer_parks() {
        val feed = WaveformFeed()
        val renderer = ribbonRenderer()
        // No voice present → both strand drives are 0, and the audible-only default
        // park is engaged (nothing new to paint). The park reads the PASSED voices,
        // never feed.level() — reading the feed there dead-locks the loop.
        assertFalse(feed.present(AuroraSpeaker.HUMAN))
        assertFalse(renderer.needsFrames(emptyList(), feed))
        assertEquals(0f, RibbonMath.strandDrive(feed.band(AuroraSpeaker.HUMAN, 0), 0f, 0), 0f)
    }

    @Test
    fun renderer_needs_frames_while_a_voice_is_audible() {
        val feed = WaveformFeed()
        val voices = listOf(human(0.7f, 0.7f, 0.7f, 0.7f))
        feed.step(0.02f, voices)
        assertTrue(ribbonRenderer().needsFrames(voices, feed))
        // I2: audibility comes from the PASSED voices, so a loud voice the feed has
        // not been stepped with still keeps frames coming.
        assertTrue(ribbonRenderer().needsFrames(voices, WaveformFeed()))
    }
}
