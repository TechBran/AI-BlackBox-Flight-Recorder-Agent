package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.EmberSim
import com.aiblackbox.portal.ui.components.MatrixSim
import com.aiblackbox.portal.ui.components.ParticleMode
import com.aiblackbox.portal.ui.components.ParticleQuality
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.StarSim
import com.aiblackbox.portal.ui.components.newFieldSim
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The particle COUNT dials (ParticleTuning / ParticleQuality) plus the density
 * multiplier where it actually lands — the three sims' pool sizes.
 *
 * Two contracts are pinned here because breaking either one ships a regression
 * a screenshot won't catch:
 *   1. DEFAULTS ARE THE IDENTITY. intensity 1.0 × quality High == 1.0, and
 *      scaleCount(n, 1.0) == n, so the field is bit-for-bit what it was before
 *      this setting existed.
 *   2. A CORRUPT OR ABSENT STORED VALUE FALLS BACK — never a crash, and never
 *      zero particles (an empty field is indistinguishable from a broken one).
 */
class ParticleTuningTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 2.75f
    private val scale = density / 3.1f

    // ── Contract 1: the defaults change nothing ──
    @Test fun `defaults resolve to exactly one — today's field`() {
        assertEquals(1.0f, ParticleTuning.INTENSITY_DEFAULT, 0f)
        assertEquals(ParticleQuality.HIGH, ParticleQuality.DEFAULT)
        assertEquals(1.0f, ParticleQuality.multiplier(ParticleQuality.DEFAULT), 0f)
        assertEquals(1.0f, ParticleTuning.countScale(ParticleTuning.INTENSITY_DEFAULT, ParticleQuality.DEFAULT), 0f)
        // …and an unconfigured install (both keys absent) lands on the same place.
        assertEquals(1.0f, ParticleTuning.countScale(null, null), 0f)
    }

    @Test fun `scaleCount at scale one is the identity for every baseline`() {
        for (base in intArrayOf(1, 40, 60, 80, 100, 200, 400)) {
            assertEquals(base, ParticleTuning.scaleCount(base, 1.0f))
        }
    }

    @Test fun `sims at the default scale keep their historical counts`() {
        val stars = StarSim(1f).also { it.resize(width, height, scale, density) }
        assertEquals(240, stars.particles.size)                    // 80 + 100 + 60

        val embers = EmberSim(1f).also { it.resize(width, height, scale, density) }
        assertTrue("cap unchanged, was ${embers.alivePool.size}", embers.alivePool.size in 200..400)

        val matrix = MatrixSim(1f).also { it.resize(width, height, scale, density) }
        assertTrue("cap unchanged, was ${matrix.columns.size}", matrix.columns.size in 40..80)
    }

    @Test fun `newFieldSim defaults to scale one`() {
        val sim = newFieldSim(ParticleMode.STARS) as StarSim
        sim.resize(width, height, scale, density)
        assertEquals(240, sim.particles.size)
    }

    // ── Intensity parsing / clamping ──
    @Test fun `intensity clamps into range`() {
        assertEquals(ParticleTuning.INTENSITY_MIN, ParticleTuning.parseIntensity(0f), 0f)
        assertEquals(ParticleTuning.INTENSITY_MIN, ParticleTuning.parseIntensity(-5f), 0f)
        assertEquals(ParticleTuning.INTENSITY_MAX, ParticleTuning.parseIntensity(99f), 0f)
        assertEquals(0.5f, ParticleTuning.parseIntensity(0.5f), 0f)
    }

    @Test fun `a corrupt or absent intensity falls back to the default`() {
        assertEquals(1.0f, ParticleTuning.parseIntensity(null as Float?), 0f)
        assertEquals(1.0f, ParticleTuning.parseIntensity(Float.NaN), 0f)
        assertEquals(1.0f, ParticleTuning.parseIntensity(Float.POSITIVE_INFINITY), 0f)
        assertEquals(1.0f, ParticleTuning.parseIntensity(Float.NEGATIVE_INFINITY), 0f)
        // …and the same value arriving as text (legacy / exported prefs).
        assertEquals(1.0f, ParticleTuning.parseIntensity(null as String?), 0f)
        assertEquals(1.0f, ParticleTuning.parseIntensity(""), 0f)
        assertEquals(1.0f, ParticleTuning.parseIntensity("   "), 0f)
        assertEquals(1.0f, ParticleTuning.parseIntensity("banana"), 0f)
        assertEquals(0.75f, ParticleTuning.parseIntensity(" 0.75 "), 0f)
    }

    // ── Quality parsing ──
    @Test fun `quality parse normalizes and defaults to high`() {
        assertEquals(ParticleQuality.HIGH, ParticleQuality.parse(null))
        assertEquals(ParticleQuality.HIGH, ParticleQuality.parse(""))
        assertEquals(ParticleQuality.HIGH, ParticleQuality.parse("bogus"))
        assertEquals(ParticleQuality.LOW, ParticleQuality.parse("  LOW "))
        assertEquals(ParticleQuality.ULTRA, ParticleQuality.parse("Ultra"))
    }

    @Test fun `quality ALL lists the three tiers in order with labels`() {
        assertEquals(
            listOf(ParticleQuality.LOW, ParticleQuality.HIGH, ParticleQuality.ULTRA),
            ParticleQuality.ALL,
        )
        assertEquals("Low", ParticleQuality.label(ParticleQuality.LOW))
        assertEquals("High", ParticleQuality.label(ParticleQuality.HIGH))
        assertEquals("Ultra", ParticleQuality.label(ParticleQuality.ULTRA))
        assertEquals("High", ParticleQuality.label("garbage"))
    }

    @Test fun `quality multipliers are ordered low lt high lt ultra`() {
        val low = ParticleQuality.multiplier(ParticleQuality.LOW)
        val high = ParticleQuality.multiplier(ParticleQuality.HIGH)
        val ultra = ParticleQuality.multiplier(ParticleQuality.ULTRA)
        assertTrue("$low < $high < $ultra", low < high && high < ultra)
    }

    // ── The combined multiplier ──
    @Test fun `countScale multiplies intensity by the quality tier`() {
        assertEquals(0.5f, ParticleTuning.countScale(1.0f, ParticleQuality.LOW), 1e-4f)
        assertEquals(1.6f, ParticleTuning.countScale(1.0f, ParticleQuality.ULTRA), 1e-4f)
        assertEquals(1.0f, ParticleTuning.countScale(2.0f, ParticleQuality.LOW), 1e-4f)
        // 0.25 × 0.5 = 0.125, quantized to 2 decimals → 0.13 (still well above the floor).
        assertEquals(0.13f, ParticleTuning.countScale(0.25f, ParticleQuality.LOW), 1e-4f)
    }

    @Test fun `countScale is quantized so a slider drag cannot rekey on float noise`() {
        val a = ParticleTuning.countScale(1.0f / 3f * 3f, ParticleQuality.ULTRA)
        val b = ParticleTuning.countScale(1.0f, ParticleQuality.ULTRA)
        assertEquals(b, a, 0f)
        // 2 decimals: no value carries more precision than that.
        val q = ParticleTuning.countScale(1.234567f, ParticleQuality.HIGH)
        assertEquals(q, Math.round(q * 100f) / 100f, 0f)
    }

    @Test fun `countScale stays inside its bounds for extreme input`() {
        val hot = ParticleTuning.countScale(Float.MAX_VALUE, ParticleQuality.ULTRA)
        assertTrue("$hot <= ${ParticleTuning.COUNT_SCALE_MAX}", hot <= ParticleTuning.COUNT_SCALE_MAX)
        val cold = ParticleTuning.countScale(-Float.MAX_VALUE, ParticleQuality.LOW)
        assertTrue("$cold >= ${ParticleTuning.COUNT_SCALE_MIN}", cold >= ParticleTuning.COUNT_SCALE_MIN)
    }

    @Test fun `a corrupt combined scale degrades to one, not to the floor`() {
        assertEquals(1.0f, ParticleTuning.sanitizeScale(Float.NaN), 0f)
        assertEquals(1.0f, ParticleTuning.sanitizeScale(Float.POSITIVE_INFINITY), 0f)
        assertEquals(1.0f, ParticleTuning.sanitizeScale(Float.NEGATIVE_INFINITY), 0f)
    }

    // ── Contract 2: never zero particles ──
    @Test fun `scaleCount never returns zero for a positive baseline`() {
        val scales = floatArrayOf(
            0f, -1f, 0.0001f, ParticleTuning.COUNT_SCALE_MIN, Float.NaN,
            Float.POSITIVE_INFINITY, Float.NEGATIVE_INFINITY, Float.MIN_VALUE,
        )
        for (s in scales) for (base in intArrayOf(1, 5, 60, 400)) {
            val n = ParticleTuning.scaleCount(base, s)
            assertTrue("scaleCount($base, $s) = $n must be >= 1", n >= 1)
        }
        // A zero baseline is the one legal zero (nothing to scale).
        assertEquals(0, ParticleTuning.scaleCount(0, 1f))
        assertEquals(0, ParticleTuning.scaleCount(-3, 1f))
    }

    @Test fun `every sim still has particles at the smallest possible scale`() {
        val tiny = ParticleTuning.COUNT_SCALE_MIN

        val stars = StarSim(tiny).also { it.resize(width, height, scale, density) }
        assertTrue("stars survive the floor, was ${stars.particles.size}", stars.particles.size >= 3)
        assertTrue("…and are thinner than default", stars.particles.size < 240)

        val embers = EmberSim(tiny).also { it.resize(width, height, scale, density) }
        assertTrue("embers survive the floor", embers.alivePool.isNotEmpty())
        assertTrue("…and are thinner than default", embers.alivePool.size < 200)

        val matrix = MatrixSim(tiny).also { it.resize(width, height, scale, density) }
        assertTrue("matrix keeps columns", matrix.columns.isNotEmpty())
        assertTrue("…glyphs still tile the width", matrix.fontSizePx > 0f)
        assertEquals(width, matrix.fontSizePx * matrix.columns.size, 0.5f)
    }

    @Test fun `a corrupt scale reaching a sim yields the default field, not an empty one`() {
        val stars = StarSim(Float.NaN).also { it.resize(width, height, scale, density) }
        assertEquals("NaN degrades to the default counts", 240, stars.particles.size)

        val matrix = MatrixSim(Float.NEGATIVE_INFINITY).also { it.resize(width, height, scale, density) }
        assertTrue("NaN/∞ degrades to the default columns", matrix.columns.size in 40..80)
    }

    // ── The dial actually moves the counts ──
    @Test fun `raising the scale raises every sim's budget`() {
        val stars = StarSim(2f).also { it.resize(width, height, scale, density) }
        assertTrue("2x doubles the stars, was ${stars.particles.size}", stars.particles.size == 480)

        val embers1 = EmberSim(1f).also { it.resize(width, height, scale, density) }
        val embers2 = EmberSim(2f).also { it.resize(width, height, scale, density) }
        assertTrue(
            "2x grows the ember pool (${embers1.alivePool.size} -> ${embers2.alivePool.size})",
            embers2.alivePool.size > embers1.alivePool.size,
        )

        val m1 = MatrixSim(1f).also { it.resize(width, height, scale, density) }
        val m2 = MatrixSim(2f).also { it.resize(width, height, scale, density) }
        assertTrue(
            "2x adds columns (${m1.columns.size} -> ${m2.columns.size})",
            m2.columns.size > m1.columns.size,
        )
        assertTrue("…and narrows the glyphs", m2.fontSizePx < m1.fontSizePx)
    }

    @Test fun `the ultra tier at max intensity is capped, not unbounded`() {
        val scale3 = ParticleTuning.countScale(ParticleTuning.INTENSITY_MAX, ParticleQuality.ULTRA)
        assertEquals(ParticleTuning.COUNT_SCALE_MAX, scale3, 1e-4f)   // 2.0 × 1.6 = 3.2 → capped at 3.0
        val matrix = MatrixSim(scale3).also { it.resize(width, height, scale, density) }
        assertTrue("columns stay bounded, was ${matrix.columns.size}", matrix.columns.size <= 200)
    }

    // ── The caption the settings sheet renders ──
    @Test fun `intensity label renders whole percent and survives garbage`() {
        assertEquals("100%", ParticleTuning.intensityLabel(1.0f))
        assertEquals("25%", ParticleTuning.intensityLabel(0.25f))
        assertEquals("200%", ParticleTuning.intensityLabel(2.0f))
        assertEquals("100%", ParticleTuning.intensityLabel(Float.NaN))
        assertEquals("100%", ParticleTuning.intensityLabel(null))
    }
}
