package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The demo driver's two load-bearing contracts:
 *
 *  1. DETERMINISM — `voicesAt(seed, t)` is a pure function, so the same seed and
 *     time always produce byte-identical bands. This is what lets the settings
 *     preview be screenshot-compared frame-for-frame during on-device iteration;
 *     if it drifted, every comparison would be noise.
 *  2. TURN STRUCTURE — the timeline actually produces the human-only, overlap
 *     (two-voice), AI-only and pause phases every style must be exercised against,
 *     with HUMAN before AI, all bands NaN-free and in 0..1.
 *
 * Pure JVM — the driver has no Compose or android.* in it, exactly so this runs
 * as fast unit math like the aurora tests.
 */
class WaveformDemoDriverTest {

    private val driver = WaveformDemoDriver()
    private val timeline = Timeline.DEFAULT

    // ── A time inside each phase of the default timeline ──────────────────────
    // human window [0,2.5), ai window [2.0,4.5), overlap [2.0,2.5), pause [4.5,5.2)
    private val humanOnlyT = 1.0
    private val overlapT = 2.25
    private val aiOnlyT = 3.5
    private val pauseT = 4.9

    @Test
    fun same_seed_and_time_produce_identical_voices() {
        // AuroraVoice.equals is content equality (speaker + bands), so equal lists
        // means byte-identical bands — the determinism the preview relies on.
        assertEquals(driver.voicesAt(humanOnlyT), driver.voicesAt(humanOnlyT))
        assertEquals(driver.voicesAt(overlapT), driver.voicesAt(overlapT))
        assertEquals(driver.voicesAt(aiOnlyT), driver.voicesAt(aiOnlyT))
    }

    @Test
    fun two_drivers_with_the_same_seed_agree_exactly() {
        val a = WaveformDemoDriver(seed = 12345L)
        val b = WaveformDemoDriver(seed = 12345L)
        for (i in 0..104) {
            val t = i * (timeline.periodSec / 100.0)
            assertEquals("mismatch at t=$t", a.voicesAt(t), b.voicesAt(t))
        }
    }

    @Test
    fun a_different_seed_changes_the_signal() {
        // Different seed → different per-syllable loudness → different bands at a
        // voiced instant. (Structure is identical; only the shaping differs.)
        val other = WaveformDemoDriver(seed = 0xBEEFL)
        assertNotEquals(driver.voicesAt(humanOnlyT), other.voicesAt(humanOnlyT))
    }

    @Test
    fun the_phase_loops_so_a_full_period_later_is_identical() {
        assertEquals(driver.voicesAt(humanOnlyT), driver.voicesAt(humanOnlyT + timeline.periodSec))
        assertEquals(driver.voicesAt(overlapT), driver.voicesAt(overlapT + timeline.periodSec * 3))
    }

    @Test
    fun human_only_phase_has_just_the_human() {
        val v = driver.voicesAt(humanOnlyT)
        assertEquals(1, v.size)
        assertEquals(AuroraSpeaker.HUMAN, v[0].speaker)
    }

    @Test
    fun overlap_phase_has_both_voices_human_first() {
        // THE two-voice moment every style must render — proven to actually occur.
        val v = driver.voicesAt(overlapT)
        assertEquals(2, v.size)
        assertEquals(AuroraSpeaker.HUMAN, v[0].speaker)
        assertEquals(AuroraSpeaker.AI, v[1].speaker)
    }

    @Test
    fun ai_only_phase_has_just_the_ai() {
        val v = driver.voicesAt(aiOnlyT)
        assertEquals(1, v.size)
        assertEquals(AuroraSpeaker.AI, v[0].speaker)
    }

    @Test
    fun the_pause_phase_is_empty() {
        // An empty list (absence), not a zero-filled voice — this is what drives a
        // style's present→absent retire rather than parking a resting ribbon.
        assertTrue(driver.voicesAt(pauseT).isEmpty())
    }

    @Test
    fun every_band_is_in_range_and_never_nan_across_a_whole_period() {
        // Walk a dense grid over two periods; every band of every present voice must
        // be a usable 0..1 number — a NaN or an over-1 drive would poison a renderer.
        var samples = 0
        val steps = 400
        for (i in 0..steps) {
            val t = i * (timeline.periodSec * 2 / steps)
            for (voice in driver.voicesAt(t)) {
                for (b in 0 until AuroraAnalyser.BANDS) {
                    val v = voice.band(b)
                    assertFalse("NaN band at t=$t", v.isNaN())
                    assertTrue("band $v out of range at t=$t", v in 0f..1f)
                }
                samples++
            }
        }
        assertTrue("expected present voices across the period", samples > 0)
    }

    @Test
    fun a_voiced_instant_actually_drives_energy() {
        // The signal is not silent everywhere: at a voiced syllable within a turn at
        // least one band lifts clearly off the floor, so a style has something to draw.
        val v = driver.voicesAt(humanOnlyT)
        val peak = (0 until AuroraAnalyser.BANDS).maxOf { v[0].band(it) }
        assertTrue("expected an audible band, got $peak", peak > 0.2f)
    }

    @Test
    fun a_nan_time_is_handled_as_zero_not_a_crash() {
        // The preview clock could momentarily hand a garbage time; it must not throw.
        val v = driver.voicesAt(Double.NaN)
        assertEquals(driver.voicesAt(0.0), v)
    }

    @Test
    fun rand01_is_deterministic_and_in_unit_range() {
        for (a in 0..3) for (b in 0..50) {
            val x = WaveformDemoDriver.rand01(999L, a, b)
            assertEquals(x, WaveformDemoDriver.rand01(999L, a, b))
            assertTrue("rand01 out of [0,1): $x", x >= 0f && x < 1f)
        }
    }

    @Test
    fun timeline_rejects_a_non_overlapping_layout() {
        // The overlap window is a hard requirement (the two-voice moment); a layout
        // without one is a construction-time failure, not a silently one-voice demo.
        var threw = false
        try {
            Timeline(humanStart = 0.0, humanEnd = 2.0, aiStart = 2.0, aiEnd = 4.0, periodSec = 4.5)
        } catch (e: IllegalArgumentException) {
            threw = true
        }
        assertTrue("expected non-overlapping timeline to be rejected", threw)
    }
}
