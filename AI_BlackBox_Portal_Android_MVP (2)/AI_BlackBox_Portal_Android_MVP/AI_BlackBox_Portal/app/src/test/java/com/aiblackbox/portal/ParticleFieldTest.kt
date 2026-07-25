package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.EmberSim
import com.aiblackbox.portal.ui.components.MatrixSim
import com.aiblackbox.portal.ui.components.ParticleMode
import com.aiblackbox.portal.ui.components.StarSim
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-logic invariants for the 3-mode particle FIELD (ParticleField.kt). The
 * fields are UI-free in resize/update, so these run on the JVM with no Compose /
 * Android graphics. Positions use Math.random, so we assert on invariants
 * (mode parsing, count caps, drift, spawn/drain) — never exact coordinates.
 */
class ParticleFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 2.75f
    private val scale = density / 3.1f

    // ── ParticleMode.parse — the persistence normalizer (the pinned logic) ──
    @Test fun `parse defaults unknown and null to stars`() {
        assertEquals(ParticleMode.STARS, ParticleMode.parse(null))
        assertEquals(ParticleMode.STARS, ParticleMode.parse(""))
        assertEquals(ParticleMode.STARS, ParticleMode.parse("bogus"))
        assertEquals(ParticleMode.STARS, ParticleMode.parse("stars"))
    }

    @Test fun `parse normalizes case and whitespace`() {
        assertEquals(ParticleMode.EMBERS, ParticleMode.parse("EMBERS"))
        assertEquals(ParticleMode.EMBERS, ParticleMode.parse("  Embers "))
        assertEquals(ParticleMode.MATRIX, ParticleMode.parse("Matrix"))
    }

    @Test fun `ALL lists the three modes in order with labels`() {
        assertEquals(listOf(ParticleMode.STARS, ParticleMode.EMBERS, ParticleMode.MATRIX), ParticleMode.ALL)
        assertEquals("Rising Stars", ParticleMode.label(ParticleMode.STARS))
        assertEquals("Embers", ParticleMode.label(ParticleMode.EMBERS))
        assertEquals("Matrix", ParticleMode.label(ParticleMode.MATRIX))
    }

    // ── Rising Stars (restored ORIGINAL warm rising-ember field) ──
    @Test fun `stars spawn the 240 warm particles and rise`() {
        val sim = StarSim()
        sim.resize(width, height, scale, density)
        assertEquals(240, sim.particles.size) // 80 + 100 + 60 (the original 3 layers)
        val p = sim.particles.first()
        val y0 = p.y
        repeat(5) { sim.update(nowMs = 16.0, dtSec = 0.016f, active = true) }
        assertTrue("stars rise (y decreases), $y0 -> ${p.y}", p.y < y0)
    }

    @Test fun `an off-screen star respawns at the bottom while active`() {
        val sim = StarSim()
        sim.resize(width, height, scale, density)
        val p = sim.particles.first()
        p.y = -10000f
        sim.update(nowMs = 16.0, dtSec = 0.016f, active = true)
        assertTrue("respawned near the bottom, y was ${p.y}", p.y > height)
    }

    @Test fun `resize is a no-op at the same size (no re-scatter)`() {
        val sim = StarSim()
        sim.resize(width, height, scale, density)
        val count = sim.particles.size
        val firstX = sim.particles.first().x
        sim.resize(width, height, scale, density) // same size → must not rebuild
        assertEquals(count, sim.particles.size)
        assertEquals(firstX, sim.particles.first().x, 0f)
    }

    @Test fun `a width growth resamples stars across the newly revealed strip`() {
        val sim = StarSim()
        sim.resize(width, height, scale, density)
        val count = sim.particles.size
        val wide = width * 1.8f                          // unfolding a Fold
        sim.resize(wide, height, scale, density)
        assertEquals("resample, never a rebuild", count, sim.particles.size)
        assertTrue(
            "stars reach the newly revealed strip (x > $width)",
            sim.particles.any { it.x > width },
        )
        assertTrue(
            "no star is pushed past the new width",
            sim.particles.none { it.x > wide + 1f },
        )
    }

    // ── Embers (full-screen floating) ──
    @Test fun `embers seed full on resize and drain when inactive`() {
        val sim = EmberSim()
        sim.resize(width, height, scale, density)
        // resize seeds the field full → embers across the WHOLE screen at rest.
        val seeded = sim.alivePool.count { it.alive }
        assertTrue("seeded full on resize, was $seeded", seeded > 0)
        assertTrue("respects the 200-400 cap, was $seeded", seeded <= 400)
        // Inactive: no new spawns; existing embers die off (life decays to 0).
        var t = 0.0
        repeat(700) { t += 16.0; sim.update(t, 0.05f, active = false) }
        assertTrue("field drains when inactive", sim.alivePool.none { it.alive })
    }

    // The dead-slot free list: a slot must be handed back exactly once when it
    // dies. A missed push starves spawning; a double push overflows the IntArray
    // (AIOOBE) and hands the same Emb out twice.
    @Test fun `ember free list hands every slot back exactly once`() {
        val sim = EmberSim()
        sim.resize(width, height, scale, density)
        val cap = sim.alivePool.size
        assertEquals("resize seeds every slot", cap, sim.alivePool.count { it.alive })
        var t = 0.0
        repeat(700) { t += 16.0; sim.update(t, 0.05f, active = false) }   // drain
        assertTrue("precondition: drained", sim.alivePool.none { it.alive })
        sim.rearm()
        assertEquals("every slot came back", cap, sim.alivePool.count { it.alive })
    }

    @Test fun `sustained ember activity keeps spawn and death balanced`() {
        val sim = EmberSim()
        sim.resize(width, height, scale, density)
        var t = 0.0
        repeat(2000) { t += 16.0; sim.update(t, 0.016f, active = true) }
        val alive = sim.alivePool.count { it.alive }
        assertTrue("pool stays populated while active, was $alive", alive > 0)
        assertTrue("never exceeds the pool", alive <= sim.alivePool.size)
    }

    @Test fun `rearm refills the ember pool for a full screen`() {
        val sim = EmberSim()
        sim.resize(width, height, scale, density)
        var t = 0.0
        repeat(700) { t += 16.0; sim.update(t, 0.05f, active = false) } // drain it
        assertTrue("precondition: drained", sim.alivePool.none { it.alive })
        sim.rearm()
        assertTrue("rearm refills the pool", sim.alivePool.any { it.alive })
    }

    // ── Matrix ──
    @Test fun `matrix builds columns within the 40-80 cap tiling the width`() {
        val sim = MatrixSim()
        sim.resize(width, height, scale, density)
        assertTrue("column count in cap, was ${sim.columns.size}", sim.columns.size in 40..80)
        assertTrue("font size positive", sim.fontSizePx > 0f)
        // columns tile the width exactly (fontSizePx = width / columns).
        assertEquals(width, sim.fontSizePx * sim.columns.size, 0.5f)
    }

    @Test fun `matrix columns fall and recycle to the top`() {
        val sim = MatrixSim()
        sim.resize(width, height, scale, density)
        val c = sim.columns.first()
        // Push past the max recycle threshold (bottom + 0.3×height) so recycle is
        // deterministic regardless of the random depth stagger.
        c.y = height * 1.4f
        sim.update(nowMs = 16.0, dtSec = 0.05f, active = true)
        assertTrue("column recycled to the top (y <= 0), was ${c.y}", c.y <= 0f)
    }
}
