package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.EmberSimulation
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Physics invariants for the UI-free [EmberSimulation] (the ember-particle
 * system ported from Apps/landing-page/app.js:191-518). Positions use
 * Math.random, so these assert on invariants — particle count / layer split /
 * velocity sign / off-screen reset / drain — never on exact coordinates.
 */
class EmberSimulationTest {

    private val width = 1000f
    private val height = 2000f

    @Test fun `spawns 120 particles across 3 layers`() {
        val sim = EmberSimulation()
        sim.resize(width, height)

        assertEquals("total particle count", 240, sim.particles.size)

        val byLayer = sim.particles.groupingBy { it.layerIndex }.eachCount()
        assertEquals("far layer count", 80, byLayer[0])
        assertEquals("mid layer count", 100, byLayer[1])
        assertEquals("foreground layer count", 60, byLayer[2])
    }

    @Test fun `all particles rise (upward velocity)`() {
        val sim = EmberSimulation()
        sim.resize(width, height)

        sim.particles.forEach { p ->
            assertTrue("vy must be negative (upward), was ${p.vy}", p.vy < 0f)
        }
    }

    @Test fun `particle pushed off the top resets to the bottom while active`() {
        val sim = EmberSimulation()
        sim.resize(width, height)

        val p = sim.particles[0]
        p.y = -100f // shove it off the top of the field

        sim.update(timeNanos = 16_000_000L, active = true)

        // While active, an off-screen ember respawns at the bottom
        // (reset sets y = height + rand*100), so it ends up >= height.
        assertTrue("should reset to bottom, y was ${p.y}", p.y >= height)
        assertFalse("active reset must keep it alive", p.dead)
    }

    @Test fun `drains to a stop when inactive`() {
        val sim = EmberSimulation()
        sim.resize(width, height)

        assertFalse("freshly spawned field is not drained", sim.isDrained())

        var t = 0L
        var iterations = 0
        while (!sim.isDrained() && iterations < 100_000) {
            t += 16_000_000L
            sim.update(timeNanos = t, active = false)
            iterations++
        }

        assertTrue("all particles should have drained while inactive", sim.isDrained())
    }

    @Test fun `killAll marks the whole field drained`() {
        val sim = EmberSimulation()
        sim.resize(width, height)
        assertFalse("freshly spawned field is not drained", sim.isDrained())

        sim.killAll() // the drain-deadline cap force-culls every particle

        assertTrue("killAll must drain the field immediately", sim.isDrained())
    }

    @Test fun `scale multiplies size and velocity (DPI-correct sizing)`() {
        val base = EmberSimulation().apply { resize(width, height) }               // scale 1.0
        val scaled = EmberSimulation().apply { scale = 2f; resize(width, height) } // scale 2.0

        val baseSize = base.particles.map { it.baseSize }.average()
        val scaledSize = scaled.particles.map { it.baseSize }.average()
        assertTrue("2x scale should ~double avg size ($baseSize -> $scaledSize)", scaledSize > baseSize * 1.6)

        val baseSpeed = base.particles.map { -it.vy }.average()       // upward magnitude
        val scaledSpeed = scaled.particles.map { -it.vy }.average()
        assertTrue("2x scale should ~double rise speed ($baseSpeed -> $scaledSpeed)", scaledSpeed > baseSpeed * 1.6)
    }

    @Test fun `rearm re-staggers the field after a full drain`() {
        val sim = EmberSimulation()
        sim.resize(width, height)
        sim.killAll()
        assertTrue("precondition: fully drained", sim.isDrained())

        sim.rearm() // fresh activation after a drain

        assertFalse("rearm must revive the field", sim.isDrained())
        assertEquals("still 240 particles", 240, sim.particles.size)
        assertTrue("velocities still upward", sim.particles.all { it.vy < 0f })
        // Re-staggered across ~1.5x height rather than all bunched at the bottom.
        assertTrue(
            "some embers should sit above the bottom band after re-stagger",
            sim.particles.any { it.y < height * 0.75f }
        )
    }
}
