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

    // ── Rising Stars ──
    @Test fun `stars spawn within the 400-800 cap and drift`() {
        val sim = StarSim()
        sim.resize(width, height, scale, density)
        assertTrue("count in cap, was ${sim.stars.size}", sim.stars.size in 400..800)
        val s = sim.stars.first()
        val y0 = s.y
        sim.update(nowMs = 16.0, dtSec = 0.016f, active = true)
        assertTrue("stars rise (y decreases), $y0 -> ${s.y}", s.y < y0)
    }

    @Test fun `a star pushed above the top wraps back to the bottom`() {
        val sim = StarSim()
        sim.resize(width, height, scale, density)
        val s = sim.stars.first()
        s.y = -1000f
        sim.update(nowMs = 16.0, dtSec = 0.016f, active = false)
        assertTrue("wrapped to bottom, y was ${s.y}", s.y >= height)
    }

    @Test fun `resize is a no-op at the same size (no re-scatter)`() {
        val sim = StarSim()
        sim.resize(width, height, scale, density)
        val count = sim.stars.size
        val firstX = sim.stars.first().x
        sim.resize(width, height, scale, density) // same size → must not rebuild
        assertEquals(count, sim.stars.size)
        assertEquals(firstX, sim.stars.first().x, 0f)
    }

    // ── Embers ──
    @Test fun `embers spawn while active and drain when inactive`() {
        val sim = EmberSim()
        sim.resize(width, height, scale, density)
        // Pool sized but empty until spawned.
        assertTrue("pool empty before spawning", sim.alivePool.none { it.alive })
        // A few active frames spawn embers (pool cap 200..400).
        var t = 0.0
        repeat(60) { t += 16.0; sim.update(t, 0.016f, active = true) }
        val liveWhileActive = sim.alivePool.count { it.alive }
        assertTrue("some embers alive while active, was $liveWhileActive", liveWhileActive > 0)
        assertTrue("respects the 200-400 cap, was $liveWhileActive", liveWhileActive <= 400)
        // Inactive: no new spawns, existing embers die off (life decays to 0).
        repeat(400) { t += 16.0; sim.update(t, 0.05f, active = false) }
        assertTrue("field drains when inactive", sim.alivePool.none { it.alive })
    }

    @Test fun `rearm clears the ember pool for a fresh rise`() {
        val sim = EmberSim()
        sim.resize(width, height, scale, density)
        var t = 0.0
        repeat(30) { t += 16.0; sim.update(t, 0.016f, active = true) }
        assertTrue("precondition: some alive", sim.alivePool.any { it.alive })
        sim.rearm()
        assertTrue("rearm empties the pool", sim.alivePool.none { it.alive })
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
