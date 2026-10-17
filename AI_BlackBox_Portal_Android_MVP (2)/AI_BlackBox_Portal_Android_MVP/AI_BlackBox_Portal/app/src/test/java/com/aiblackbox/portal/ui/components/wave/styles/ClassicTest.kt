package com.aiblackbox.portal.ui.components.wave.styles

import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Classic — the SPECTRAL layer drive in [ClassicMath].
 *
 * Classic's three ribbons used to be one scalar level scaled by three constants, so a booming vowel
 * and a bright sibilant of the same loudness drew the IDENTICAL picture. These pin the fix: each
 * ribbon is driven by its own slice of the spectrum, read relative to the loudest slice in the same
 * frame, and the pre-spectral behaviour survives exactly as the FLAT-spectrum special case.
 *
 * Pure JVM in the aurora tradition — every number the look depends on is a function of its inputs.
 */
class ClassicTest {

    private val eps = 1e-5f

    /**
     * A spectrum as the renderer reads it: a plain FloatArray filled from
     * [com.aiblackbox.portal.ui.components.wave.WaveformFeed.bin], which is the SMOOTHED channel
     * (never the raw AuroraBins carrier — see ClassicRenderer for why that distinction is the
     * difference between a flowing ribbon and a stepping one).
     */
    private fun binsOf(vararg values: Float): FloatArray {
        val full = FloatArray(AuroraAnalyser.BINS)
        for (i in values.indices) full[i.coerceIn(0, full.size - 1)] = values[i]
        return full
    }

    /** All BINS in one group set to [v], everything else silent. */
    private fun binsInGroup(group: Int, v: Float): FloatArray {
        val full = FloatArray(AuroraAnalyser.BINS)
        for (k in ClassicMath.binGroupStart(group) until ClassicMath.binGroupStart(group + 1)) {
            full[k] = v
        }
        return full
    }

    // ------------------------------------------------------- the identity case

    @Test
    fun a_flat_spectrum_reproduces_the_pre_spectral_ribbon_exactly() {
        // Every band equal → every layer's share is 1 → amplitude is the old
        // `level * LAYER_HEIGHT[i]`. The spectral drive is an ADDITION, and this is
        // the case that proves it did not move the look it was added to.
        val level = 0.6f
        for (layer in 0 until ClassicMath.LAYERS) {
            val share = ClassicMath.bandLayerShare(0.4f, 0.4f, 0.4f, 0.4f, layer)
            assertEquals("layer $layer share", 1f, share, eps)
            assertEquals(
                "layer $layer amplitude",
                level * ClassicMath.LAYER_HEIGHT[layer],
                ClassicMath.layerAmplitude(level, share, layer),
                eps,
            )
        }
    }

    @Test
    fun silence_is_neutral_and_never_nan() {
        // No energy anywhere: no slice can be "relatively louder", so the shares fall
        // back to neutral and the amplitude is carried entirely by the level (0 here).
        for (layer in 0 until ClassicMath.LAYERS) {
            val share = ClassicMath.bandLayerShare(0f, 0f, 0f, 0f, layer)
            assertEquals(1f, share, eps)
            assertEquals(0f, ClassicMath.layerAmplitude(0f, share, layer), 0f)
        }
    }

    // --------------------------------------------------- THE acceptance test

    @Test
    fun two_spectra_at_the_SAME_overall_level_draw_different_ribbons() {
        // Identical weighted-max level (0.8 in band 0 vs 0.8 in band 3, the feed's
        // LEVEL_WEIGHTS are 1.0 and 0.70 so both peak the same slice at 0.8/0.56 —
        // what matters here is that the LEVEL handed to the layers is the same and
        // the drawn amplitudes still differ).
        val level = 0.8f
        val dark = floatArrayOf(0.8f, 0.7f, 0.05f, 0.02f)   // body-heavy vowel
        val bright = floatArrayOf(0.05f, 0.1f, 0.6f, 0.8f)  // sibilant
        var anyDifferent = false
        for (layer in 0 until ClassicMath.LAYERS) {
            val a = ClassicMath.layerAmplitude(
                level, ClassicMath.bandLayerShare(dark[0], dark[1], dark[2], dark[3], layer), layer,
            )
            val b = ClassicMath.layerAmplitude(
                level, ClassicMath.bandLayerShare(bright[0], bright[1], bright[2], bright[3], layer), layer,
            )
            if (kotlin.math.abs(a - b) > 0.01f) anyDifferent = true
        }
        assertTrue("equal level must NOT imply an equal picture", anyDifferent)
    }

    @Test
    fun body_energy_favours_the_slow_layer_and_air_favours_the_fast_one() {
        val level = 0.7f
        // A body-only voice: the slow ribbon is at full share, the fast one at the floor.
        val bodySlow = ClassicMath.bandLayerShare(0.8f, 0.6f, 0f, 0f, 0)
        val bodyFast = ClassicMath.bandLayerShare(0.8f, 0.6f, 0f, 0f, 2)
        assertEquals(1f, bodySlow, eps)
        assertTrue("air ribbon must recede on a body-only voice", bodyFast < bodySlow)
        // An air-only voice flips it.
        val airSlow = ClassicMath.bandLayerShare(0f, 0f, 0f, 0.8f, 0)
        val airFast = ClassicMath.bandLayerShare(0f, 0f, 0f, 0.8f, 2)
        assertEquals(1f, airFast, eps)
        assertTrue("body ribbon must recede on an air-only voice", airSlow < airFast)
        // ...and the drawn amplitudes follow the shares.
        assertTrue(
            ClassicMath.layerAmplitude(level, airFast, 2) >
                ClassicMath.layerAmplitude(level, bodyFast, 2),
        )
    }

    @Test
    fun the_mid_layer_answers_the_presence_band() {
        // Band 2 (1600/2600 Hz) owns the middle ribbon: lifting it alone lifts that
        // layer's share above the other two.
        val mid = ClassicMath.bandLayerShare(0.1f, 0.1f, 0.9f, 0.1f, 1)
        val slow = ClassicMath.bandLayerShare(0.1f, 0.1f, 0.9f, 0.1f, 0)
        val fast = ClassicMath.bandLayerShare(0.1f, 0.1f, 0.9f, 0.1f, 2)
        assertEquals(1f, mid, eps)
        assertTrue(slow < mid)
        assertTrue(fast < mid)
    }

    // ------------------------------------------------------------ the budget

    @Test
    fun a_spectral_layer_never_exceeds_the_pre_spectral_ceiling() {
        // The share is 0..1 and multiplies the SAME `level * LAYER_HEIGHT` the old
        // renderer used, so no spectrum can make a ribbon taller than Classic's
        // shipped maximum — the no-mesa budget is inherited, not re-argued.
        val bands = floatArrayOf(1f, 0f, 0.5f, 0.25f)
        for (layer in 0 until ClassicMath.LAYERS) {
            var level = 0f
            while (level <= 1.0001f) {
                val amp = ClassicMath.layerAmplitude(
                    level,
                    ClassicMath.bandLayerShare(bands[0], bands[1], bands[2], bands[3], layer),
                    layer,
                )
                assertTrue(
                    "layer $layer at level $level exceeded its ceiling",
                    amp <= level * ClassicMath.LAYER_HEIGHT[layer] + eps,
                )
                assertTrue("layer $layer must stay positive-definite", amp >= 0f)
                level += 0.05f
            }
        }
    }

    @Test
    fun the_shape_floor_keeps_every_ribbon_alive() {
        // A slice with NO energy at all still draws at the floor share — Classic is
        // three layered ribbons, and a style that deleted one on a dark voice would
        // stop being Classic.
        val dead = ClassicMath.bandLayerShare(1f, 1f, 0f, 0f, 2)
        assertEquals(0f, dead, eps)
        val amp = ClassicMath.layerAmplitude(0.9f, dead, 2)
        assertTrue("a silent slice must still show the floor", amp > 0f)
        assertEquals(
            0.9f * ClassicMath.LAYER_HEIGHT[2] * ClassicMath.SHAPE_FLOOR,
            amp,
            eps,
        )
    }

    // ------------------------------------------------------------- NaN safety

    @Test
    fun nan_inputs_are_absorbed() {
        for (layer in 0 until ClassicMath.LAYERS) {
            val share = ClassicMath.bandLayerShare(Float.NaN, 0.5f, Float.NaN, 0.2f, layer)
            assertTrue("share must be finite", share.isFinite())
            assertTrue(share in 0f..1f)
            val amp = ClassicMath.layerAmplitude(Float.NaN, share, layer)
            assertEquals(0f, amp, 0f)
        }
        // An out-of-range layer index clamps rather than throwing inside a draw loop.
        assertTrue(ClassicMath.bandLayerShare(0.5f, 0.5f, 0.5f, 0.5f, -3).isFinite())
        assertTrue(ClassicMath.bandLayerShare(0.5f, 0.5f, 0.5f, 0.5f, 99).isFinite())
        assertTrue(ClassicMath.layerAmplitude(0.5f, 0.5f, 99).isFinite())
    }

    // ----------------------------------------------------- the BIN channel

    @Test
    fun bin_groups_tile_the_whole_spectrum_without_a_gap() {
        assertEquals(0, ClassicMath.binGroupStart(0))
        assertEquals(AuroraAnalyser.BINS, ClassicMath.binGroupStart(ClassicMath.LAYERS))
        for (g in 0 until ClassicMath.LAYERS) {
            assertTrue(
                "group $g must be non-empty and ordered",
                ClassicMath.binGroupStart(g) < ClassicMath.binGroupStart(g + 1),
            )
        }
    }

    @Test
    fun a_published_spectrum_drives_the_layers_bin_by_bin() {
        // The finer channel: energy confined to the TOP bin group lights the fast
        // ribbon and starves the slow one, exactly as the coarse band path does.
        val air = binsInGroup(2, 0.8f)
        assertEquals(1f, ClassicMath.layerShare(air, 0f, 0f, 0f, 0f, 2), eps)
        assertTrue(ClassicMath.layerShare(air, 0f, 0f, 0f, 0f, 0) < 1f)
        val body = binsInGroup(0, 0.8f)
        assertEquals(1f, ClassicMath.layerShare(body, 0f, 0f, 0f, 0f, 0), eps)
        assertTrue(ClassicMath.layerShare(body, 0f, 0f, 0f, 0f, 2) < 1f)
    }

    @Test
    fun bins_are_preferred_over_bands_when_a_producer_publishes_them() {
        // Bands say "all body", bins say "all air". A published spectrum is the finer
        // reading of the SAME audio, so it wins — and with no spectrum the bands do.
        val air = binsInGroup(2, 0.9f)
        assertTrue(
            "the published spectrum must decide",
            ClassicMath.layerShare(air, 0.9f, 0.9f, 0f, 0f, 2) >
                ClassicMath.layerShare(null, 0.9f, 0.9f, 0f, 0f, 2),
        )
        assertEquals(
            1f,
            ClassicMath.layerShare(null, 0.9f, 0.9f, 0f, 0f, 0),
            eps,
        )
    }

    @Test
    fun a_silent_published_spectrum_is_neutral_not_dark() {
        // A present-but-silent producer publishes an all-zero spectrum every tick.
        // That must read as "no spectral opinion" (neutral shares), never as a
        // spectrum that switches two of the three ribbons off.
        val silent = binsOf()
        for (layer in 0 until ClassicMath.LAYERS) {
            assertEquals(1f, ClassicMath.layerShare(silent, 0f, 0f, 0f, 0f, layer), eps)
        }
    }
}
