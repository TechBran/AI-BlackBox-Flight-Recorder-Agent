package com.aiblackbox.portal.ui.components.wave.styles

import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * The Segmented Equalizer's GEOMETRY, signal interpolation, peak-hold decay and
 * budget math, asserted on the JVM in the aurora tradition. Every visible property
 * of the style is a pure function in [SegmentedEqMath] / [SegmentedEqSpec], so the
 * Compose renderer is only a traversal of these and an instrumented test would add
 * a device and subtract nothing.
 *
 * Organised by the guarantees the plan and the style contract require:
 *   • the WIDTH-DERIVED column count, capped at the measurement's own resolution
 *   • the per-column read off the REAL spectrum bins (and the band fallback)
 *   • LED stack geometry + the small-height DEGRADE (no mesa/clip at 40 dp)
 *   • lit-count / peak-row quantisation
 *   • peak-hold decay: fall rate AND 60-vs-120 Hz dt independence
 *   • the deterministic two-voice colour scheme (red human / blue AI)
 *   • the occlusion + white-out budgets, as NUMBERS
 */
class SegmentedEqTest {

    private val bins = AuroraAnalyser.BINS

    /** The saturated column count — what any surface wide enough to ask for it gets. */
    private val cols = SegmentedEqSpec.MAX_COLUMNS

    private fun spectrum(vararg pairs: Pair<Int, Float>): FloatArray =
        FloatArray(bins).also { for ((k, v) in pairs) it[k] = v }

    // =========================================================================
    // constants sanity
    // =========================================================================

    @Test
    fun columns_and_min_rows_are_the_pinned_values() {
        assertEquals("a column may never claim more than one real bin", bins, cols)
        assertTrue("min rows must be at least a readable meter", SegmentedEqSpec.SEGMENT_MIN_ROWS >= 3)
        assertEquals(4, AuroraAnalyser.BANDS) // the band FALLBACK assumes four source bands
    }

    // =========================================================================
    // the width-derived column count
    // =========================================================================

    @Test
    fun column_count_derives_from_the_surface_width() {
        val d = 3f
        fun countAt(widthDp: Float) = SegmentedEqMath.columnCount(widthDp * d, d)
        // A 360 dp phone asks for 30 at the target pitch and is capped at the bin count.
        assertEquals(cols, countAt(360f))
        // A narrow surface gets FEWER columns rather than the same sixteen squeezed in.
        assertEquals(10, countAt(120f))
        assertEquals(16, countAt(192f))
        // A tiny surface floors at MIN_COLUMNS rather than collapsing to one bar.
        assertEquals(SegmentedEqSpec.MIN_COLUMNS, countAt(30f))
        // Degenerate inputs are total, never a divide-by-zero.
        assertEquals(SegmentedEqSpec.MIN_COLUMNS, SegmentedEqMath.columnCount(0f, d))
        assertEquals(SegmentedEqSpec.MIN_COLUMNS, SegmentedEqMath.columnCount(1080f, 0f))
    }

    @Test
    fun column_count_never_claims_more_resolution_than_the_measurement_has() {
        // The cap is the WHOLE honesty claim: at most one real bin per column, so no
        // column is ever an invented value between two measurements.
        for (d in floatArrayOf(1f, 2.625f, 3f)) {
            for (widthDp in floatArrayOf(40f, 120f, 360f, 720f, 2000f)) {
                val n = SegmentedEqMath.columnCount(widthDp * d, d)
                assertTrue("count $n over $bins bins @${widthDp}dp/${d}x", n <= bins)
                assertTrue("count $n under the floor", n >= SegmentedEqSpec.MIN_COLUMNS)
            }
        }
    }

    @Test
    fun bars_stop_widening_once_the_surface_outruns_the_spectrum() {
        val d = 3f
        // 360 dp: 24 columns → 15 dp pitch → 9.9 dp bars, against the old fixed
        // sixteen's 14.9 dp. The operator's complaint, as a number.
        val phonePitch = SegmentedEqMath.pitchPx(360f * d, SegmentedEqMath.columnCount(360f * d, d))
        val phoneBar = SegmentedEqMath.barWidthPx(phonePitch, d)
        assertEquals(15f, phonePitch / d, 1e-3f)
        assertEquals(9.9f, phoneBar / d, 1e-2f)
        assertTrue("bars must be narrower than the old 16-column look", phoneBar / d < 14.9f)

        // A Fold-width surface saturates the column count, so the PITCH keeps growing;
        // the bar must not grow with it or the wide surface gets fat blocks again.
        val widePitch = SegmentedEqMath.pitchPx(720f * d, SegmentedEqMath.columnCount(720f * d, d))
        val wideBar = SegmentedEqMath.barWidthPx(widePitch, d)
        assertTrue("pitch grows with width", widePitch > phonePitch)
        assertEquals("bar is capped", SegmentedEqSpec.MAX_BAR_W_DP, wideBar / d, 1e-3f)
        assertTrue("the cap only bites once the natural bar is wider", wideBar < widePitch)
    }

    // =========================================================================
    // the per-column read off the REAL bins
    // =========================================================================

    @Test
    fun columns_tile_the_spectrum_with_contiguous_non_empty_bin_ranges() {
        // Every bin belongs to exactly one column, and no column is empty — the
        // structural statement that this is a measurement and not an interpolation.
        for (n in intArrayOf(8, 10, 16, 24)) {
            assertEquals("first column starts at bin 0 (n=$n)", 0, SegmentedEqMath.binRangeStart(0, n))
            assertEquals("last column ends at the top bin (n=$n)", bins, SegmentedEqMath.binRangeEnd(n - 1, n))
            for (c in 0 until n) {
                val lo = SegmentedEqMath.binRangeStart(c, n)
                val hi = SegmentedEqMath.binRangeEnd(c, n)
                assertTrue("column $c of $n owns nothing", hi > lo)
                if (c < n - 1) {
                    assertEquals("gap/overlap after column $c of $n", hi, SegmentedEqMath.binRangeStart(c + 1, n))
                }
            }
        }
    }

    @Test
    fun a_column_reads_the_loudest_bin_it_covers() {
        // Eight columns over 24 bins: three bins each. A narrow peak inside one of
        // them must survive at full height (a MEAN would wash it out to a third).
        val n = 8
        val peak = spectrum(4 to 0.9f)
        assertEquals(0.9f, SegmentedEqMath.columnDrive(peak, FloatArray(4), 1, n), 1e-6f)
        for (c in 0 until n) {
            if (c == 1) continue
            assertEquals("column $c must stay dark", 0f, SegmentedEqMath.columnDrive(peak, FloatArray(4), c, n), 0f)
        }
    }

    @Test
    fun a_lone_bin_lights_exactly_its_own_column_with_no_wobble() {
        // At the saturated count each column IS one bin: the read is exact, there is
        // no bleed into neighbours, and nothing about it moves with time (the drive
        // takes no clock at all — the deleted jitter cannot come back by accident).
        val n = cols
        val bandsSilent = FloatArray(4)
        for (k in 0 until bins) {
            val one = spectrum(k to 1f)
            for (c in 0 until n) {
                val expected = if (c == k) 1f else 0f
                assertEquals("bin $k → column $c", expected, SegmentedEqMath.columnDrive(one, bandsSilent, c, n), 0f)
            }
        }
    }

    /**
     * THE spectrum-awareness acceptance test: two spectra with the SAME overall level
     * (same peak, same total energy) placed at different frequencies must draw
     * differently. A level-driven style cannot pass this.
     */
    @Test
    fun two_spectra_with_equal_overall_level_draw_different_columns() {
        val n = cols
        val low = spectrum(2 to 1f, 3 to 0.8f)
        val high = spectrum(20 to 1f, 19 to 0.8f)
        // The premise, asserted rather than assumed: identical peak and identical sum.
        assertEquals(low.max(), high.max(), 0f)
        assertEquals(low.sum(), high.sum(), 1e-6f)

        var differing = 0
        for (c in 0 until n) {
            val a = SegmentedEqMath.columnDrive(low, FloatArray(4), c, n)
            val b = SegmentedEqMath.columnDrive(high, FloatArray(4), c, n)
            if (abs(a - b) > 1e-4f) differing++
        }
        assertEquals("every lit column must have moved", 4, differing)
    }

    @Test
    fun bin_reads_are_bounded_and_nan_safe() {
        val n = cols
        val dirty = FloatArray(bins) { Float.NaN }
        dirty[5] = -3f
        dirty[6] = 7f
        for (c in 0 until n) {
            val v = SegmentedEqMath.columnDrive(dirty, FloatArray(4), c, n)
            assertTrue("column $c drive $v out of 0..1", v in 0f..1f)
        }
        assertEquals("an over-range bin clamps, it does not blow up", 1f, SegmentedEqMath.columnDrive(dirty, FloatArray(4), 6, n), 0f)
        assertEquals("a negative bin reads silence", 0f, SegmentedEqMath.columnDrive(dirty, FloatArray(4), 5, n), 0f)
    }

    @Test
    fun column_reads_handle_a_degenerate_width() {
        // A zero/negative column count must not divide by zero; an out-of-range column
        // clamps, like every other read in the style.
        val one = spectrum(0 to 0.42f)
        assertTrue(SegmentedEqMath.columnDrive(one, FloatArray(4), 0, 0).isFinite())
        assertTrue(SegmentedEqMath.columnDrive(one, FloatArray(4), -1, cols) in 0f..1f)
        assertTrue(SegmentedEqMath.columnDrive(one, FloatArray(4), 999, cols) in 0f..1f)
    }

    // =========================================================================
    // the band fallback (no producer publishes a spectrum yet)
    // =========================================================================

    @Test
    fun without_a_spectrum_the_columns_read_the_four_bands_on_the_same_axis() {
        // Same log-frequency layout, at the resolution the band channel actually has:
        // four plateaus, no invented values between them.
        val n = cols
        val bands = floatArrayOf(0.1f, 0.4f, 0.7f, 1.0f)
        for (c in 0 until n) {
            val expected = bands[c * AuroraAnalyser.BANDS / n]
            assertEquals("column $c", expected, SegmentedEqMath.columnDrive(null, bands, c, n), 0f)
        }
        assertEquals(0, SegmentedEqMath.bandForColumn(0, n))
        assertEquals(AuroraAnalyser.BANDS - 1, SegmentedEqMath.bandForColumn(n - 1, n))
    }

    @Test
    fun the_band_fallback_answers_to_spectral_shape_not_level() {
        // Two band sets with the same overall (weighted-max) level, energy at opposite
        // ends: the columns must differ there too.
        val n = cols
        val low = floatArrayOf(1f, 0.6f, 0f, 0f)
        val high = floatArrayOf(0f, 0f, 0.6f, 1f)
        var differing = 0
        for (c in 0 until n) {
            val a = SegmentedEqMath.columnDrive(null, low, c, n)
            val b = SegmentedEqMath.columnDrive(null, high, c, n)
            if (abs(a - b) > 1e-4f) differing++
        }
        assertEquals("all four plateaus must differ", n, differing)
    }

    @Test
    fun band_fallback_is_nan_safe() {
        val nan = floatArrayOf(Float.NaN, Float.NaN, Float.NaN, Float.NaN)
        for (c in 0 until cols) {
            assertEquals("NaN bands → 0 drive", 0f, SegmentedEqMath.columnDrive(null, nan, c, cols), 0f)
        }
        assertEquals(0f, SegmentedEqMath.columnDrive(null, FloatArray(0), 0, cols), 0f)
    }

    // =========================================================================
    // LED stack geometry + the small-height degrade
    // =========================================================================

    @Test
    fun rows_fit_within_the_usable_height() {
        // n rows span n*seg + (n-1)*gap; that must never exceed the usable room.
        val seg = 9f
        val gap = 4.5f
        val usable = 100f
        val n = SegmentedEqMath.rows(usable, seg, gap)
        assertTrue(n > 0)
        assertTrue("stack overflows usable", n * seg + (n - 1) * gap <= usable + 1e-3f)
        // One more row would overflow.
        assertTrue((n + 1) * seg + n * gap > usable)
    }

    @Test
    fun rows_is_zero_for_no_room() {
        assertEquals(0, SegmentedEqMath.rows(0f, 9f, 4.5f))
        assertEquals(0, SegmentedEqMath.rows(-5f, 9f, 4.5f))
        assertEquals(0, SegmentedEqMath.rows(100f, 0f, 4.5f))
    }

    @Test
    fun degrade_guarantees_min_rows_and_still_fits() {
        // A height too short for MIN_ROWS at the natural LED size must shrink the LED
        // so exactly MIN_ROWS fit — never fewer (a mesa), never overflowing (a clip).
        val seg = 9f
        val gap = 4.5f
        val minRows = SegmentedEqSpec.SEGMENT_MIN_ROWS
        val usable = 20f // natural rows here would be < minRows
        assertTrue(SegmentedEqMath.rows(usable, seg, gap) < minRows) // precondition
        val unit = SegmentedEqMath.resolveUnit(usable, seg, gap, minRows)
        assertEquals("degrade must yield exactly the floor rows", minRows, unit.rows)
        assertTrue("degraded LED must be smaller", unit.segPx < seg)
        // Preserves the seg:gap ratio.
        assertEquals(gap / seg, unit.gapPx / unit.segPx, 1e-3f)
        // Fits without clipping.
        val span = unit.rows * unit.segPx + (unit.rows - 1) * unit.gapPx
        assertTrue("degraded stack $span overflows $usable", span <= usable + 1e-3f)
    }

    @Test
    fun natural_size_kept_when_it_already_clears_min_rows() {
        val seg = 9f
        val gap = 4.5f
        val unit = SegmentedEqMath.resolveUnit(200f, seg, gap, SegmentedEqSpec.SEGMENT_MIN_ROWS)
        assertEquals("no degrade when there is room", seg, unit.segPx, 0f)
        assertEquals(gap, unit.gapPx, 0f)
        assertTrue(unit.rows >= SegmentedEqSpec.SEGMENT_MIN_ROWS)
    }

    @Test
    fun the_forty_dp_player_bar_never_mesas_in_either_mode() {
        // 40 dp at three shipped densities, in BOTH one-voice (full height) and
        // two-voice (a ~20 dp half). Every case must stack at least MIN_ROWS and fit.
        val minRows = SegmentedEqSpec.SEGMENT_MIN_ROWS
        for (density in floatArrayOf(1f, 2.625f, 3f)) {
            val h = 40f * density
            val seg = SegmentedEqSpec.SEGMENT_HEIGHT_DP * density
            val gap = SegmentedEqSpec.SEGMENT_GAP_DP * density
            val inset = SegmentedEqSpec.EDGE_INSET_DP * density
            val centerGap = SegmentedEqSpec.CENTER_GAP_DP * density
            val inner = h - 2 * inset

            // one-voice
            val one = SegmentedEqMath.resolveUnit(inner, seg, gap, minRows)
            assertTrue("one-voice @${density}x rows ${one.rows}", one.rows >= minRows)
            assertTrue(one.rows * one.segPx + (one.rows - 1) * one.gapPx <= inner + 1e-3f)

            // two-voice half
            val half = ((inner - centerGap) / 2f).coerceAtLeast(0f)
            val two = SegmentedEqMath.resolveUnit(half, seg, gap, minRows)
            assertTrue("two-voice half @${density}x rows ${two.rows}", two.rows >= minRows)
            assertTrue(two.rows * two.segPx + (two.rows - 1) * two.gapPx <= half + 1e-3f)
        }
    }

    // =========================================================================
    // lit count + peak row quantisation
    // =========================================================================

    @Test
    fun lit_count_rounds_and_respects_the_present_floor() {
        val rows = 10
        assertEquals(0, SegmentedEqMath.litCount(0f, rows, 0))
        assertEquals(5, SegmentedEqMath.litCount(0.5f, rows, 0))
        assertEquals(10, SegmentedEqMath.litCount(1f, rows, 0))
        assertEquals(10, SegmentedEqMath.litCount(5f, rows, 0)) // clamps over-range
        // The present floor keeps a baseline LED lit even at silence...
        assertEquals(1, SegmentedEqMath.litCount(0f, rows, 1))
        // ...but never invents light a loud column would not already have.
        assertEquals(5, SegmentedEqMath.litCount(0.5f, rows, 1))
        // NaN → 0 (then floored).
        assertEquals(0, SegmentedEqMath.litCount(Float.NaN, rows, 0))
        assertEquals(0, SegmentedEqMath.litCount(0.5f, 0, 1)) // no rows → nothing
    }

    @Test
    fun peak_row_sits_on_a_real_led_and_vanishes_at_zero() {
        val rows = 10
        assertEquals("no peak for silence", -1, SegmentedEqMath.peakRow(0f, rows))
        assertEquals(9, SegmentedEqMath.peakRow(1f, rows)) // top row index
        assertEquals(4, SegmentedEqMath.peakRow(0.5f, rows))
        assertTrue(SegmentedEqMath.peakRow(0.05f, rows) in 0..(rows - 1))
        assertEquals(-1, SegmentedEqMath.peakRow(0.5f, 0))
        assertEquals(-1, SegmentedEqMath.peakRow(Float.NaN, rows))
    }

    // =========================================================================
    // peak-hold decay
    // =========================================================================

    @Test
    fun peak_snaps_up_and_never_dives_below_the_live_level() {
        val fall = 1f / SegmentedEqSpec.PEAK_FALL_SEC
        // A louder level snaps the peak straight up.
        assertEquals(0.8f, SegmentedEqMath.decayPeak(0.3f, 0.8f, 0.016f, fall), 1e-6f)
        // A quieter level lets it fall, but not below the current level.
        val held = SegmentedEqMath.decayPeak(0.9f, 0.4f, 0.016f, fall)
        assertTrue(held < 0.9f && held >= 0.4f)
        // Deep fall clamps at the live level, never under it.
        assertEquals(0.4f, SegmentedEqMath.decayPeak(0.41f, 0.4f, 10f, fall), 1e-6f)
    }

    @Test
    fun peak_fall_is_dt_independent_between_60_and_120_hz() {
        // Peak parked at 1.0, live level 0, driven for 0.75 s once at 60 Hz and once
        // at 120 Hz. Both must land at the same height (a linear fall of 1/1.5 per
        // second → ~0.5 after 0.75 s) — the 60-vs-120 Hz guarantee the contract wants.
        val fall = 1f / SegmentedEqSpec.PEAK_FALL_SEC
        fun run(hz: Int): Float {
            var p = 1f
            val dt = 1f / hz
            repeat((0.75f * hz).toInt()) { p = SegmentedEqMath.decayPeak(p, 0f, dt, fall) }
            return p
        }
        val p60 = run(60)
        val p120 = run(120)
        assertEquals("60 and 120 Hz must fall the same", p60, p120, 1e-3f)
        assertEquals("~half-fallen at 0.75 s", 0.5f, p60, 2e-2f)
    }

    @Test
    fun peak_decay_is_nan_safe() {
        val fall = 1f / SegmentedEqSpec.PEAK_FALL_SEC
        assertEquals(0f, SegmentedEqMath.decayPeak(Float.NaN, 0f, 0.016f, fall), 0f)
        // A NaN dt holds the peak (no fall), never poisons it.
        assertEquals(0.6f, SegmentedEqMath.decayPeak(0.6f, 0f, Float.NaN, fall), 1e-6f)
        // A zero/negative dt likewise holds.
        assertEquals(0.6f, SegmentedEqMath.decayPeak(0.6f, 0f, 0f, fall), 1e-6f)
    }

    // =========================================================================
    // the two-voice colour scheme (red human / blue AI)
    // =========================================================================

    @Test
    fun accent_columns_are_a_deterministic_third_ish_subset() {
        val accents = (0 until cols).count { SegmentedEqMath.isAccentColumn(it) }
        // Every third column, whatever the width-derived count is: 8 of 24 at the
        // saturated count (2,5,8,11,14,17,20,23).
        assertEquals(cols / SegmentedEqSpec.ACCENT_MODULO, accents)
        assertTrue(SegmentedEqMath.isAccentColumn(2))
        assertTrue(SegmentedEqMath.isAccentColumn(5))
        assertTrue("column 0 is a grey column", !SegmentedEqMath.isAccentColumn(0))
    }

    @Test
    fun human_reads_red_over_white_and_ai_reads_blue_over_blue_grey() {
        // Accent columns: brand red for the human, brand blue for the AI.
        val humanAccent = SegmentedEqMath.columnColor(AuroraSpeaker.HUMAN, 2)
        val aiAccent = SegmentedEqMath.columnColor(AuroraSpeaker.AI, 2)
        assertEquals(SegmentedEqSpec.HUMAN_ACCENT, humanAccent)
        assertEquals(SegmentedEqSpec.AI_ACCENT, aiAccent)
        assertNotEquals("human and AI must never share a colour", humanAccent, aiAccent)

        // Grey columns: near-white for the human, blue-grey for the AI (the tint).
        val humanGrey = SegmentedEqMath.columnColor(AuroraSpeaker.HUMAN, 0)
        val aiGrey = SegmentedEqMath.columnColor(AuroraSpeaker.AI, 0)
        assertEquals(SegmentedEqSpec.HUMAN_GREY, humanGrey)
        assertEquals(SegmentedEqSpec.AI_GREY, aiGrey)
        assertNotEquals(humanGrey, aiGrey)
    }

    // =========================================================================
    // budgets — the numbers the particle field behind depends on
    // =========================================================================

    @Test
    fun occluding_cores_stay_under_the_field_survival_ceiling() {
        // Only the lit SrcOver cores occlude. Worst case (every column, every row lit)
        // must stay under ~55% so the particle field behind always survives — the
        // inherited Aurora v33 requirement, as one pinned number.
        val occ = SegmentedEqMath.maxCoreOcclusion()
        assertTrue("segments must actually be visible", occ > 0.2f)
        assertTrue("occlusion $occ exceeds the 0.55 field-survival ceiling", occ <= 0.55f)
        // The two-voice halves do not overlap, so the combined ceiling is the same
        // single-field number, not double it.
    }

    @Test
    fun additive_passes_are_bounded_well_under_white_out() {
        // Glow + peak are the only additive (Plus) passes; even summed at a shared
        // pixel they must stay well under 1.0 or the field blows to white.
        val additiveSum = SegmentedEqSpec.GLOW_ALPHA + SegmentedEqSpec.PEAK_ALPHA
        assertTrue("additive sum $additiveSum risks white-out", additiveSum < 0.9f)
        // The SrcOver core is not full-opaque, so even the occluding pass leaves light.
        assertTrue(SegmentedEqSpec.CORE_ALPHA < 1f)
    }
}
