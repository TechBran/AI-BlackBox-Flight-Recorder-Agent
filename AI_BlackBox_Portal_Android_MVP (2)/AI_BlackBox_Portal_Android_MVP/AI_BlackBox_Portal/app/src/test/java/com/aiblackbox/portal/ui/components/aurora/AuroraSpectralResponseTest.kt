package com.aiblackbox.portal.ui.components.aurora

import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * Aurora's answer to M7's shared rule — EVERY style must respond to spectral CONTENT,
 * not merely to level — measured end to end through the real engine and the real
 * geometry, and deliberately WITHOUT changing a single Aurora constant.
 *
 * Aurora is the one style that was born spectrum-aware: it is four sheets and sheet j
 * follows band j, so two spectra at the same overall loudness already draw two
 * different pictures. That was never pinned as a REQUIREMENT though, only as a
 * description of the layout, and an unpinned property is one refactor away from
 * quietly becoming "all four sheets follow the loudest band". This file states it as
 * the requirement it is.
 *
 * WHY THERE IS NO AURORA PIXEL CHANGE IN THIS MILESTONE. Aurora is the pixel-identical
 * default; the milestone that was allowed to move it (the perceptual-curve pass) has
 * already run and its result is [Aurora.DISPLAY_GAMMA]. Everything M7 asks of a style
 * — spectrum in, different picture out — Aurora already does, so the honest deliverable
 * here is the measurement, not a tweak. Anything that DID want changing about Aurora
 * (the live band spread, the rectangular-window rate dependence) is upstream in the
 * analyser's calibration, out of a renderer's reach, and is recorded as still open.
 *
 * The comparison is made on the DRAWN geometry (the crest outline in pixels), not on
 * the envelope levels, so it cannot be satisfied by a number that never reaches the
 * screen.
 */
class AuroraSpectralResponseTest {

    private val width = 900f
    private val height = 140f

    private fun bands(vararg v: Float) = AuroraVoice(AuroraSpeaker.HUMAN, v)

    /** Settle the engine on one steady spectrum, the way a sustained passage would. */
    private fun settled(voice: AuroraVoice): AuroraEnvelope {
        val engine = AuroraEngine(Aurora.BASELINE)
        repeat(400) { engine.step(0.016f, listOf(voice)) }
        return engine.envelope(AuroraSpeaker.HUMAN)
    }

    /** The crest outline of one sheet, in pixels across the width — what the eye actually sees. */
    private fun crest(levels: FloatArray, layer: Int, phase: Float = 2.0f): FloatArray {
        val insetPx = Aurora.insetPx(height, Aurora.crestStrokePx(height, 1f))
        val amp = Aurora.amplitude(height, levels[layer], layer)
        val travel = Aurora.travelDirection(AuroraSpeaker.HUMAN)
        val phaseBias = Aurora.phaseBias(AuroraSpeaker.HUMAN)
        val driftBias = Aurora.driftBias(AuroraSpeaker.HUMAN)
        val drift = Aurora.drift(height, phase, layer, driftBias)
        return FloatArray(41) { i ->
            Aurora.crestY(i / 40f, height, insetPx, amp, phase, layer, travel, phaseBias, drift)
        }
    }

    private fun maxAbsDiff(a: FloatArray, b: FloatArray): Float {
        var m = 0f
        for (i in a.indices) m = maxOf(m, abs(a[i] - b[i]))
        return m
    }

    /** The gap between the tallest and the shortest sheet — how far the ribbon is FANNED. */
    private fun sheetSpread(levels: FloatArray): Float {
        var lo = levels[0]
        var hi = levels[0]
        for (v in levels) {
            if (v < lo) lo = v
            if (v > hi) hi = v
        }
        return hi - lo
    }

    @Test
    fun equal_level_spectra_with_different_content_draw_a_different_aurora() {
        // Two spectra at the SAME overall level by the shared definition
        // (WaveformFeed.levelOf, the weighted max every style agrees on) but with the
        // energy in opposite places: all body versus all sibilance.
        val body = bands(0.64f, 0.10f, 0.02f, 0.01f)
        val sibilance = bands(0.01f, 0.02f, 0.64f / 0.85f, 0.10f)
        val feed = WaveformFeed()
        feed.step(0.02f, listOf(body))
        val bodyLevel = feed.level(AuroraSpeaker.HUMAN)
        feed.step(0.02f, listOf(sibilance))
        assertEquals(
            "the two spectra must be at the same overall level for this to prove anything",
            bodyLevel,
            feed.level(AuroraSpeaker.HUMAN),
            1e-3f,
        )

        val a = settled(body).levels.copyOf()
        val b = settled(sibilance).levels.copyOf()
        // EVERY sheet is drawn somewhere else, and the picture as a whole differs by a
        // large fraction of the ribbon's height — a level-only style would draw the
        // two frames identically.
        var worst = 0f
        for (layer in 0 until Aurora.SHEETS) {
            val d = maxAbsDiff(crest(a, layer), crest(b, layer))
            assertTrue("sheet $layer moved only ${d}px between the two spectra", d > height * 0.02f)
            worst = maxOf(worst, d)
        }
        assertTrue("the ribbon as a whole barely moved (${worst}px)", worst > height * 0.15f)
    }

    @Test
    fun each_sheet_follows_its_own_band_and_no_other() {
        // The property that MAKES Aurora spectral: lifting one band lifts exactly one
        // sheet. If a refactor ever routed the overall level into every sheet, all
        // four would rise together and this fails.
        for (loud in 0 until Aurora.SHEETS) {
            val v = FloatArray(Aurora.SHEETS) { if (it == loud) 0.81f else 0f }
            val levels = settled(AuroraVoice(AuroraSpeaker.HUMAN, v)).levels
            for (layer in 0 until Aurora.SHEETS) {
                if (layer == loud) {
                    assertTrue(
                        "sheet $layer must follow band $loud",
                        levels[layer] > Aurora.displayCurve(0.5f),
                    )
                } else {
                    assertEquals(
                        "sheet $layer must ignore band $loud",
                        Aurora.BASELINE,
                        levels[layer],
                        1e-3f,
                    )
                }
            }
        }
    }

    @Test
    fun a_flat_spectrum_and_a_tilted_one_at_the_same_peak_are_told_apart() {
        // The weakest form of the requirement, and the one a level-only style fails:
        // identical PEAK band, different shape. The drawn sheet spread must differ.
        val flat = bands(0.5f, 0.5f, 0.5f, 0.5f)
        val tilted = bands(0.5f, 0.25f, 0.08f, 0.02f)
        val f = settled(flat).levels
        val t = settled(tilted).levels
        assertEquals("same peak band by construction", f[0], t[0], 1e-3f)
        assertTrue("a flat spectrum draws flat sheets", sheetSpread(f) < 1e-3f)
        assertTrue("a tilted spectrum must fan the sheets", sheetSpread(t) > 0.3f)
    }
}
