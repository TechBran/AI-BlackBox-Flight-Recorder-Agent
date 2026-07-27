package com.aiblackbox.portal.ui.components.aurora

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.sqrt

/**
 * The Aurora renderer's GEOMETRY and ENVELOPE math, asserted on the JVM. Everything the ribbon's
 * look depends on lives in pure functions precisely so it can be tested here — the Compose
 * composable is only a traversal of these, so an instrumented test would add a device and
 * subtract nothing.
 *
 * The tests are organised by the four changes this port makes to the BarWaveformView original
 * (whisper-everywhere, 2026-07-18) plus the two contracts the surface has to honour:
 *
 *   C1 one amplitude      -> a LIST of voices, both drawn at once and kept distinguishable
 *   C2 6-stop gradient    -> solid red per human, solid blue per AI
 *   C3 per-FRAME motion   -> per-SECOND motion (the original ran at double speed on a 120 Hz Fold)
 *   C4 fixed 160x56 pill  -> geometry derived from whatever size the caller gives
 *   C5 -                  -> nothing opaque, ever (the particle field renders behind this)
 *   C6 -                  -> no idle frame loop
 *   C7 travelling sine    -> a STANDING ridge of band-driven mountains at fixed x positions
 */
class AuroraWaveformTest {

    // The three sizes this ships at (composer strip, player bar, voice screen) crossed with the
    // densities we actually run on: 1x is the pathological small case, 2.625x is a Pixel, 3x is
    // the Fold 6. Height in PX is what the renderer sees.
    private val shippedHeightsDp = floatArrayOf(52f, 40f, 140f)
    private val densities = floatArrayOf(1f, 2.625f, 3f)
    private val widths = floatArrayOf(300f, 1080f)

    private fun loud(speaker: AuroraSpeaker) = AuroraVoice(speaker, floatArrayOf(1f, 1f, 1f, 1f))
    private fun silent(speaker: AuroraSpeaker) = AuroraVoice(speaker, floatArrayOf(0f, 0f, 0f, 0f))

    /**
     * Drive an engine for [seconds] at [hz], holding [voices] constant. The frame count is ROUNDED-
     * truncating turns "one time constant of 0.295 s at 1 kHz" into 294 steps and quietly moves
     * the analytic expectations the release tests below check against.
     */
    private fun drive(hz: Int, seconds: Double, voices: List<AuroraVoice>, into: AuroraEngine = AuroraEngine()): AuroraEngine {
        val dt = 1f / hz
        repeat(Math.round(seconds * hz).toInt()) { into.step(dt, voices) }
        return into
    }

    // =========================================================================
    // C3 — motion is measured in SECONDS, not frames
    // =========================================================================

    /**
     * The per-second time constants must reproduce the original's per-frame blend factors at the
     * 60 fps it was tuned on, or this is a re-tune wearing a port's clothes. k = 1 - exp(-dt/tau).
     */
    @Test
    fun `the per-second time constants reproduce the original per-frame blends at 60 fps`() {
        val dt = 1f / 60f
        assertEquals("attack", 0.65f, Aurora.blendAt(dt, Aurora.ATTACK_TAU_SEC), 0.002f)
        assertEquals("global release", 0.10f, Aurora.blendAt(dt, Aurora.RELEASE_TAU_SEC), 0.002f)
        val expected = floatArrayOf(0.055f, 0.085f, 0.115f, 0.16f)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("band $j release", expected[j], Aurora.blendAt(dt, Aurora.BAND_RELEASE_TAUS_SEC[j]), 0.002f)
        }
        // The original advanced 0.13 rad per frame. Since C7 that clock drives ONLY the vertical
        // drift, but it still has to advance at the rate the wander was tuned at.
        assertEquals("phase per frame", 0.13f, Aurora.PHASE_RATE_PER_SEC * dt, 0.002f)
    }

    /**
     * THE 120 Hz BUG. The original applied its blend once per frame and added 0.13 rad per frame,
     * so a Fold running at 120 Hz released twice as fast and flowed twice as fast as the tuned
     * look. Same elapsed time must mean the same picture at any refresh rate.
     */
    @Test
    fun `the same elapsed time gives the same levels at 60 Hz and at 120 Hz`() {
        fun run(hz: Int): FloatArray {
            val e = AuroraEngine()
            drive(hz, 0.5, listOf(loud(AuroraSpeaker.HUMAN)), e)   // saturate
            drive(hz, 0.5, listOf(silent(AuroraSpeaker.HUMAN)), e) // then release
            return e.envelope(AuroraSpeaker.HUMAN).levels.copyOf()
        }
        val at60 = run(60)
        val at120 = run(120)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("sheet $j", at60[j], at120[j], 1e-3f)
        }
        // Guard the guard: after 500 ms of release the sheets must still be spread out, otherwise
        // they have all bottomed out and would agree at any frame rate for the wrong reason.
        assertTrue("sheets collapsed - test is vacuous", at60[0] - at60[3] > 0.05f)
    }

    @Test
    fun `the same elapsed time gives the same phase at 60 Hz and at 120 Hz`() {
        val v = listOf(loud(AuroraSpeaker.HUMAN))
        assertEquals(drive(60, 1.0, v).phaseRad, drive(120, 1.0, v).phaseRad, 1e-3f)
        // ...and it is the tuned speed, not merely a consistent one.
        assertEquals(Aurora.PHASE_RATE_PER_SEC, drive(60, 1.0, v).phaseRad, 1e-3f)
    }

    /** Pin the semantics- release is a true exponential decay toward the resting baseline. */
    @Test
    fun `release follows the analytic exponential toward the baseline`() {
        val e = AuroraEngine()
        drive(1000, 0.5, listOf(loud(AuroraSpeaker.HUMAN)), e)
        assertTrue("did not saturate", e.envelope(AuroraSpeaker.HUMAN).levels[0] > 0.99f)
        val start = e.envelope(AuroraSpeaker.HUMAN).levels[0]
        // Exactly one time constant of silence on sheet 0.
        drive(1000, Aurora.BAND_RELEASE_TAUS_SEC[0].toDouble(), listOf(silent(AuroraSpeaker.HUMAN)), e)
        val b = Aurora.BASELINE
        assertEquals(b + (start - b) * exp(-1f), e.envelope(AuroraSpeaker.HUMAN).levels[0], 2e-3f)
    }

    /**
     * Phase accumulates forever, so it has to wrap or float precision rots the animation over a
     * long call. It may only wrap where EVERY term that consumes it is back in phase. Since C7 the
     * one consumer is the drift rate (0.35), and 2pi/0.05 is kept as the period because 0.35 is
     * exactly 7 x 0.05 — a whole number of drift turns, so the wrap stays invisible and stays safe
     * if another consumer on the same 0.05 grid is added back. Wrapping at a naive 2pi would jump.
     */
    @Test
    fun `wrapping the phase is invisible to every term that consumes it`() {
        val h = 156f
        for (layer in 0 until Aurora.SHEETS) {
            for (speaker in AuroraSpeaker.entries) {
                val drift = Aurora.driftBias(speaker)
                for (p in 0..20) {
                    val phase = p * 5f
                    assertEquals(
                        "sheet $layer drift",
                        Aurora.drift(h, phase, layer, drift),
                        Aurora.drift(h, phase + Aurora.PHASE_WRAP_RAD, layer, drift),
                        1e-3f,
                    )
                }
            }
        }
    }

    /**
     * ...and the drift really is the ONLY thing the clock reaches, which is what makes the wrap
     * argument above complete AND is the structural half of C7. A time term anywhere in an
     * x-dependent expression is a travelling wave by another name, and the shape test below can
     * only catch the ones that survive into [Aurora.ridge] — this catches one wired straight into
     * the draw loop.
     */
    @Test
    fun `the frame clock reaches nothing but the vertical drift`() {
        val src = stripComments(rendererSource())
        val uses = Regex("""nowPhase""").findAll(src).count()
        assertTrue("the draw loop stopped reading the phase - the ribbon is frozen", uses >= 2)
        // Every read is either the local binding or the drift call. Any third consumer is a
        // horizontal traveller unless proven otherwise, so it has to be justified HERE.
        val intoDrift = Regex("""Aurora\.drift\(\s*h\s*,\s*nowPhase""").findAll(src).count()
        val binding = Regex("""val\s+nowPhase\s*=""").findAll(src).count()
        assertEquals(
            "the frame clock feeds something other than Aurora.drift - C7 forbids a time term in " +
                "any x-dependent expression",
            uses,
            intoDrift + binding,
        )
    }

    // =========================================================================
    // C4 — geometry comes from the given size
    // =========================================================================

    /**
     * THE ONE THAT MATTERS. At every shipped height, at every density, with BOTH voices at full
     * drive, across a whole phase cycle- no point of any sheet (and no half of the crest stroke
     * drawn on it) may leave the box the caller gave us.
     */
    @Test
    fun `no drawn point escapes the canvas at any shipped height with two voices at full drive`() {
        val voices = listOf(loud(AuroraSpeaker.HUMAN), loud(AuroraSpeaker.AI))
        for (density in densities) {
            val minCrest = 1f * density   // 1.dp in px
            for (dp in shippedHeightsDp) {
                val h = dp * density
                val crest = Aurora.crestStrokePx(h, minCrest)
                val inset = Aurora.insetPx(h, crest)
                val halfStroke = crest / 2f
                for (w in widths) {
                    val steps = Aurora.sampleCount(w)
                    val engine = AuroraEngine()
                    drive(240, 1.0, voices, engine)  // saturate every band
                    for (frame in 0..60) {
                        val phase = frame * (Aurora.PHASE_WRAP_RAD / 60f)
                        for (v in voices) {
                            val levels = engine.envelope(v.speaker).levels
                            for (layer in 0 until Aurora.SHEETS) {
                                val amp = Aurora.amplitude(h, layer)
                                val drift = Aurora.drift(h, phase, layer, Aurora.driftBias(v.speaker))
                                val shift = Aurora.peakShift(v.speaker, layer)
                                forEachSheetPoint(w, h, steps, inset, amp, levels, layer, shift, drift,
                                    onCrest = { x, y, _ -> assertInside("crest ${dp}dp@${density}x layer $layer", x, y, w, h, halfStroke) },
                                    onBottom = { x, y -> assertInside("bottom ${dp}dp@${density}x layer $layer", x, y, w, h, halfStroke) },
                                )
                            }
                        }
                    }
                }
            }
        }
    }

    private fun assertInside(what: String, x: Float, y: Float, w: Float, h: Float, halfStroke: Float) {
        if (x < -0.01f || x > w + 0.01f) fail("$what x=$x outside 0..$w")
        if (y - halfStroke < -0.01f || y + halfStroke > h + 0.01f) {
            fail("$what y=$y (+/-$halfStroke stroke) outside 0..$h")
        }
    }

    /**
     * The original's stadium envelope pinched on the PILL's cap radius (= half its height). Ours
     * is width-relative, because a 400dp-wide 40dp-tall bar would otherwise taper over 5% of its
     * width and read as a hard cut at both ends.
     */
    @Test
    fun `the envelope tapers to nothing at both edges and is full through the middle`() {
        val h = 156f
        val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 3f))
        assertEquals(0f, Aurora.edgeTaper(0f), 1e-4f)
        assertEquals(0f, Aurora.edgeTaper(1f), 1e-4f)
        assertEquals(1f, Aurora.edgeTaper(0.5f), 1e-4f)
        // Monotone rise through the taper, so the ribbon opens smoothly instead of stepping.
        var prev = -1f
        var p = 0f
        while (p <= Aurora.TAPER_FRACTION) {
            val v = Aurora.edgeTaper(p)
            assertTrue("taper not monotone at $p", v >= prev)
            prev = v
            p += Aurora.TAPER_FRACTION / 20f
        }
        // Symmetric — a ribbon that opens faster on the left looks broken.
        for (q in 1..9) {
            val f = q / 10f * Aurora.TAPER_FRACTION
            assertEquals(Aurora.edgeTaper(f), Aurora.edgeTaper(1f - f), 1e-4f)
        }

        // C8 — the taper MULTIPLIES both excursions rather than clamping them, so both edges of the
        // sheet close onto the GROUND LINE at both ends. Before, they were squeezed against an
        // envelope closing on the box centre, which crushed the ends flat against a rail (measured:
        // a third of the front sheet's clamped width lived out here at speech drive) and left the
        // ribbon terminating in a wedge instead of a point.
        for (layer in 0 until Aurora.SHEETS) {
            for (speaker in AuroraSpeaker.entries) {
                for (phase in floatArrayOf(0f, DRIFT_PERIOD_RAD / 3f, DRIFT_PERIOD_RAD * 2f / 3f)) {
                    val drift = Aurora.drift(h, phase, layer, Aurora.driftBias(speaker))
                    val ground = Aurora.groundY(h, inset, drift)
                    val shift = Aurora.peakShift(speaker, layer)
                    val amp = Aurora.amplitude(h, layer)
                    for (edge in floatArrayOf(0f, 1f)) {
                        assertEquals(
                            "sheet $layer $speaker crest does not close on the ground line at $edge",
                            ground,
                            Aurora.crestY(edge, h, inset, amp, HELD_BANDS, layer, shift, drift),
                            1e-3f,
                        )
                        assertEquals(
                            "sheet $layer $speaker belly does not close on the ground line at $edge",
                            ground,
                            Aurora.bottomY(edge, h, inset, amp, HELD_BANDS, layer, shift, drift),
                            1e-3f,
                        )
                    }
                }
            }
        }
    }

    /**
     * A 0-height canvas happens for one layout pass. The original clamped with coerceIn against a
     * bound that could invert at the cap apexes and it explicitly warned that coerceIn THROWS on
     * an empty range — the same trap exists here whenever the inset exceeds the half-height.
     */
    @Test
    fun `degenerate canvas sizes clamp instead of throwing`() {
        for (h in floatArrayOf(0f, 0.5f, 1f, 4f, 12f)) {
            val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 3f))
            for (p in 0..10) {
                val progress = p / 10f
                // The rails may meet on a canvas smaller than two insets, but they may never
                // CROSS- coerceIn throws on an inverted range, and the ground line is coerced
                // between them on every sample.
                assertTrue("rails inverted at h=$h", Aurora.railTop(h, inset) <= Aurora.railBottom(h, inset))
                val loudBands = FloatArray(Aurora.SHEETS) { 1f }
                val y = Aurora.crestY(progress, h, inset, 999f, loudBands, 0, 0.4f, 500f)
                val yb = Aurora.bottomY(progress, h, inset, 999f, loudBands, 0, 0.4f, 500f)
                assertTrue("crest NaN", !y.isNaN())
                assertTrue("bottom NaN", !yb.isNaN())
                assertTrue("crest escaped h=$h", y >= -0.01f && y <= h + 0.01f)
                assertTrue("bottom escaped h=$h", yb >= -0.01f && yb <= h + 0.01f)
            }
        }
    }

    /**
     * The per-layer ladders are the look. C7 retired the frequency and phase-multiplier ladders
     * with the travelling sine and put a LATTICE ladder in their place: the sheets are held apart
     * by where their mountains sit and by which band each mountain carries, since four sheets
     * drawing the same eight mountains from the same four bands would stack into one thick line.
     */
    @Test
    fun `the per-layer amplitude and lattice ladders hold the sheets apart`() {
        val h = 100f
        for (layer in 0 until Aurora.SHEETS) {
            assertEquals("amplitude $layer", h * 0.46f * (1f - layer * 0.10f), Aurora.amplitude(h, layer), 1e-4f)
        }
        // Higher sheets are shallower — that ordering is what gives the stack depth.
        for (layer in 1 until Aurora.SHEETS) {
            assertTrue(Aurora.amplitude(h, layer) < Aurora.amplitude(h, layer - 1))
        }
        // ...and the ladder is LIVE, which C8 changed. It used to be inert on the front sheet:
        // `minOf(amp, halfHeight)` clamped sheet 0's amplitude against an envelope it exceeded at
        // every progress, so raising AMP_FRACTION moved nothing there and the shout was tuned by a
        // knob that was not connected to it. The amplitude IS the drawn height now, so a change to
        // it has to show up in the picture, at every layer.
        val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 3f))
        val full = FloatArray(Aurora.SHEETS) { 1f }
        for (layer in 0 until Aurora.SHEETS) {
            val drift = Aurora.drift(h, 0f, layer, Aurora.driftBias(AuroraSpeaker.HUMAN))
            val ground = Aurora.groundY(h, inset, drift)
            val shift = Aurora.peakShift(AuroraSpeaker.HUMAN, layer)
            // Mid-ribbon, on this sheet's own mountain, so neither the taper nor a saddle is what
            // is being measured.
            val p = (0 until Aurora.PEAKS).map { Aurora.peakProgress(it, shift) }
                .first { it > 0.4f && it < 0.6f }
            val amp = Aurora.amplitude(h, layer)
            assertEquals(
                "sheet $layer's summit is not its full amplitude - something is clamping it again",
                ground - amp,
                Aurora.crestY(p, h, inset, amp, full, layer, shift, drift),
                1e-3f,
            )
        }

        // LATTICE- every sheet is skewed off every other sheet, by a visible fraction of the peak
        // spacing but never by the half step the two SPEAKERS are separated with.
        val spacing = 1f / Aurora.PEAKS
        for (layer in 1 until Aurora.SHEETS) {
            val apart = Aurora.peakProgress(0, Aurora.peakShift(AuroraSpeaker.HUMAN, layer)) -
                Aurora.peakProgress(0, Aurora.peakShift(AuroraSpeaker.HUMAN, layer - 1))
            assertTrue("sheets $layer and ${layer - 1} share a lattice", apart > 0.08f * spacing)
            assertTrue("sheet $layer skewed a whole half step", apart < 0.45f * spacing)
        }

        // BANDS- every band owns exactly two mountains in every sheet (the mirrored sequence), and
        // no two sheets carry the same band in the same place.
        for (layer in 0 until Aurora.SHEETS) {
            val owned = IntArray(Aurora.SHEETS)
            for (peak in 0 until Aurora.PEAKS) owned[Aurora.peakBand(layer, peak)]++
            for (band in 0 until Aurora.SHEETS) {
                assertEquals("band $band on sheet $layer", 2, owned[band])
            }
            for (other in 0 until layer) {
                val same = (0 until Aurora.PEAKS).count {
                    Aurora.peakBand(layer, it) == Aurora.peakBand(other, it)
                }
                assertTrue("sheets $layer and $other carry the same bands", same < Aurora.PEAKS)
            }
        }
    }

    /**
     * THE FRONT SHEET MAY NOT RAIL (C8). The regression test for the defect C8 exists to remove.
     *
     * The brightest sheet used to SATURATE against the top of its box: `crestY` clamped the crest
     * against an envelope that closed on the box CENTRE, and 18.64% of layer 0's width was a flat
     * clamped mesa at the analyser's steady-speech drive, 44.85% on a two-voice shout (measured by
     * exactly this method against the pre-C8 geometry). Mountains with their summits cut off flat,
     * on the sheet the eye goes to, at exactly the loud moments the user is looking at.
     *
     * Nothing about the amplitude could fix that — the clamp was the binding constraint, not the
     * gain — so C8 moved the ground line down, made the taper a multiplier and inverted the drift
     * ladder. Measured after: 0.00% at every drive, on every sheet.
     *
     * The threshold at full drive is 1% rather than 0 purely as float slack (one sample of 121 at a
     * single drift phase would be 0.03%); any real mesa is an order of magnitude wider than that,
     * since a summit is quadratic at its apex and even 1 px of clipping flattens ~17% of the width.
     * Speech is asserted at EXACTLY zero, which is what "nothing clips at realistic drive" means.
     */
    @Test
    fun `the front sheet keeps its summits instead of railing`() {
        val h = RIDGE_H
        val inset = ridgeInset()

        // Fraction of the drawn width sitting exactly on the top rail, over a whole drift turn.
        fun clamped(levels: FloatArray, layer: Int): Float {
            var onRail = 0
            var total = 0
            for (speaker in AuroraSpeaker.entries) {
                for (frame in 0 until 24) {
                    val drift = Aurora.drift(h, frame * DRIFT_PERIOD_RAD / 24f, layer, Aurora.driftBias(speaker))
                    val amp = Aurora.amplitude(h, layer)
                    val shift = Aurora.peakShift(speaker, layer)
                    // The WHOLE width, edges included. The old geometry crushed the ends against
                    // the closing envelope, so a mid-ribbon-only window would have hidden a third
                    // of the defect.
                    for (i in 0..RIDGE_SAMPLES) {
                        val p = i.toFloat() / RIDGE_SAMPLES
                        val y = Aurora.crestY(p, h, inset, amp, levels, layer, shift, drift)
                        total++
                        if (abs(y - Aurora.railTop(h, inset)) < 1e-3f) onRail++
                    }
                }
            }
            return onRail.toFloat() / total
        }

        // SPEECH- the analyser's measured steady-speech band (~0.58) through the envelope's sqrt
        // perceptual lift, which is the level the renderer actually sees.
        val speech = FloatArray(Aurora.SHEETS) { sqrt(0.58f) }
        for (layer in 0 until Aurora.SHEETS) {
            assertEquals(
                "sheet $layer clips at ordinary speech - the ribbon is railing again",
                0f, clamped(speech, layer), 1e-6f,
            )
        }
        // FULL TWO-VOICE DRIVE- rare and narrow, not half the width.
        for (layer in 0 until Aurora.SHEETS) {
            val c = clamped(FloatArray(Aurora.SHEETS) { 1f }, layer)
            assertTrue("sheet $layer is ${c * 100}% flat mesa at full drive", c < 0.01f)
        }

        // GUARD THE GUARD- a ribbon that had simply gone flat would pass everything above. The
        // summits have to be TALLER than the railed ones they replace: measured on the pre-C8
        // geometry, layer 0's tallest summit was 38.30% of the container at full drive with its top
        // sliced off, and 32.60% at speech. It draws 46.00% and 35.03% now.
        fun summit(levels: FloatArray): Float {
            var tallest = 0f
            for (frame in 0 until 24) {
                val drift = Aurora.drift(h, frame * DRIFT_PERIOD_RAD / 24f, 0, Aurora.driftBias(AuroraSpeaker.HUMAN))
                val ground = Aurora.groundY(h, inset, drift)
                for (i in 0..RIDGE_SAMPLES) {
                    val p = i.toFloat() / RIDGE_SAMPLES
                    val y = Aurora.crestY(p, h, inset, Aurora.amplitude(h, 0), levels, 0, Aurora.peakShift(AuroraSpeaker.HUMAN, 0), drift)
                    tallest = maxOf(tallest, ground - y)
                }
            }
            return tallest / h
        }
        assertTrue("the shout got SHORTER (${summit(FloatArray(Aurora.SHEETS) { 1f })} of the box)",
            summit(FloatArray(Aurora.SHEETS) { 1f }) > 0.383f)
        assertTrue("speech got SHORTER (${summit(speech)} of the box)", summit(speech) > 0.326f)
    }

    /**
     * THE COMPOSITION (C8). Where the ink actually SITS in the box it was given.
     *
     * The ribbon used to ride high and leave the bottom empty: measured over a whole drift turn
     * across all four sheets and both speakers, it spanned 33.93%..69.68% of the container at rest,
     * 7.20%..77.58% at the steady-speech drive asserted below and 7.20%..80.27% on a shout — 19.7%
     * to 30.3% of the box permanently dead at the BOTTOM against 7.2% to 11.7% of headroom at the
     * top, a 2:1 to 2.7:1 asymmetry, with the optical centre riding up to 42.4% of the height while
     * speaking (~34dp of empty box under a speaking ribbon on the 140dp voice screen).
     *
     * Pinned here as an exact band, not as a direction, so nobody can drift it back up into the
     * top rail (which is also the shape of the clipping defect) or re-open the dead space below.
     * The numbers are pure fractions of the container — the inset only reaches the rails and the
     * rails never bind — so the same assertions hold on the 40dp player bar, the 52dp composer
     * strip and the 140dp voice screen at any density, which the loop below checks directly.
     */
    @Test
    fun `the ink is composed in its box at every drive`() {
        // levels, expected top, expected bottom (fractions of the container).
        val expected = listOf(
            Triple(FloatArray(Aurora.SHEETS) { Aurora.BASELINE }, 0.3957f, 0.7497f),
            Triple(FloatArray(Aurora.SHEETS) { sqrt(0.58f) }, 0.1698f, 0.8626f),
            Triple(FloatArray(Aurora.SHEETS) { 1f }, 0.0931f, 0.9010f),
        )
        for (h in floatArrayOf(40f, 52f * 2.625f, 140f * 3f)) {
            val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 1f))
            for ((levels, wantTop, wantBottom) in expected) {
                var top = Float.MAX_VALUE
                var bottom = -Float.MAX_VALUE
                for (layer in 0 until Aurora.SHEETS) {
                    for (speaker in AuroraSpeaker.entries) {
                        val amp = Aurora.amplitude(h, layer)
                        val shift = Aurora.peakShift(speaker, layer)
                        for (frame in 0 until 24) {
                            val drift = Aurora.drift(h, frame * DRIFT_PERIOD_RAD / 24f, layer, Aurora.driftBias(speaker))
                            for (i in 0..RIDGE_SAMPLES) {
                                val p = i.toFloat() / RIDGE_SAMPLES
                                top = minOf(top, Aurora.crestY(p, h, inset, amp, levels, layer, shift, drift))
                                bottom = maxOf(bottom, Aurora.bottomY(p, h, inset, amp, levels, layer, shift, drift))
                            }
                        }
                    }
                }
                val drive = levels[0]
                assertEquals("ink top at drive $drive on a ${h}px box", wantTop, top / h, 0.02f)
                assertEquals("ink bottom at drive $drive on a ${h}px box", wantBottom, bottom / h, 0.02f)

                val headroom = top / h
                val dead = 1f - bottom / h
                // NEITHER end may be starved. 1.6:1 covers the resting sliver, which is a band of
                // four thin sheets spread by the drift and cannot be centred at the same ground
                // line that centres a full-height range (that trade is the ground line's own doc).
                // The LOUD states, which is where the defect was, come in at 1.24:1 and 1.06:1.
                assertTrue(
                    "the ink is lopsided at drive $drive- ${headroom * 100}% headroom above, " +
                        "${dead * 100}% dead below",
                    maxOf(headroom, dead) < 1.6f * minOf(headroom, dead),
                )
                // ...and specifically the defect: no more than a seventh of the box may be empty
                // under a SPEAKING ribbon (it was a quarter).
                if (drive > 0.5f) {
                    assertTrue("still ${dead * 100}% of dead box under a speaking ribbon", dead < 0.14f)
                }
                // The optical centre stays near the middle of the box at every drive; it used to
                // ride to 43.6% while speaking and now sits at 51.6%.
                val centre = (top / h + bottom / h) / 2f
                assertTrue("the ribbon's optical centre sits at ${centre * 100}% of the box", abs(centre - 0.5f) < 0.08f)
            }
        }
    }

    /**
     * THE WEAVE. The drift is what stops the four sheets travelling as one slab- each layer wanders
     * vertically at its own offset and on its own timing, so they pass THROUGH each other instead
     * of sliding as a stack of parallel ribbons. It is half of why the depth stack reads as depth.
     *
     * Nothing else in this file notices if it stops. The phase-wrap test compares the drift to
     * ITSELF, which a constant satisfies; the two-voice separation test is carried by the phase
     * bias; and the bounds test only gets easier with the drift removed. Stubbing the whole
     * function to 0f passed every other test here, so the weave is asserted directly.
     */
    @Test
    fun `the drift pushes the layers apart and keeps them moving`() {
        val h = 156f
        // One full turn of the drift's own sine. 0.35 mirrors Aurora's private DRIFT_RATE — if that
        // is ever retuned this must follow it, because the mean assertion below relies on sampling
        // EXACTLY one period (a uniform sweep of a whole sine period sums to zero, a partial one
        // does not). Sweeping PHASE_WRAP_RAD instead would cover this period many times over and
        // show nothing extra, the drift being far slower than the phase.
        val period = (2.0 * PI / 0.35).toFloat()
        val n = 240
        fun sweep(layer: Int, bias: Float) = FloatArray(n) { Aurora.drift(h, it * period / n, layer, bias) }

        for (bias in floatArrayOf(Aurora.driftBias(AuroraSpeaker.HUMAN), Aurora.driftBias(AuroraSpeaker.AI))) {
            val d = Array(Aurora.SHEETS) { sweep(it, bias) }
            for (i in 0 until n) {
                // The outer sheets are held clear of the centre line, and on OPPOSITE sides of it —
                // they straddle the (SHEETS-1)/2 centre, which is what opens the stack up.
                assertTrue("layer 0 drift vanished", abs(d[0][i]) > 0.02f * h)
                assertTrue("layer 3 drift vanished", abs(d[3][i]) > 0.02f * h)
                assertTrue("outer layers drifted to the same side", d[0][i] * d[3][i] < 0f)
                // ...and they always reach further out than the inner pair, at every phase. The
                // ordering may never invert, or the stack would visibly turn inside out mid-flow.
                for (outer in intArrayOf(0, 3)) {
                    for (inner in intArrayOf(1, 2)) {
                        assertTrue(
                            "layer $outer stopped out-drifting layer $inner at phase index $i",
                            abs(d[outer][i]) > abs(d[inner][i]),
                        )
                    }
                }
            }
            // MOVING, not merely displaced. A fixed per-layer offset would be four parallel
            // ribbons that never cross, which is the thing the drift exists to prevent.
            for (layer in 0 until Aurora.SHEETS) {
                val spread = d[layer].max() - d[layer].min()
                assertTrue("layer $layer drift is frozen (spread $spread)", spread > 0.02f * h)
            }
        }

        // The AI's bias RE-TIMES the wander without relocating it- identical mean over a full turn,
        // so neither ribbon sits off centre, but a visibly different position at any given instant.
        for (layer in 0 until Aurora.SHEETS) {
            val human = sweep(layer, Aurora.driftBias(AuroraSpeaker.HUMAN))
            val ai = sweep(layer, Aurora.driftBias(AuroraSpeaker.AI))
            assertEquals("layer $layer mean drift moved", human.average(), ai.average(), 1e-3)
            val apart = (0 until n).maxOf { abs(human[it] - ai[it]) }
            assertTrue("layer $layer drift bias changed nothing", apart > 0.02f * h)
        }
    }

    // =========================================================================
    // C7 — a STANDING ridge of band-driven mountains
    // =========================================================================

    /**
     * THE INTERPOLATION CONTRACT. The ridge has to pass exactly through every control point (or a
     * band does not really own its mountain), stay inside 0..max (a negative excursion is a notch
     * cut through the centre line, which against the soft bloom pass reads as a tear), and be
     * MONOTONE between neighbours (which is what rules Catmull-Rom out — it overshoots).
     */
    @Test
    fun `the ridge passes through its control points without overshooting between them`() {
        val levels = floatArrayOf(0.9f, 0.1f, 0.6f, 0.35f)
        for (layer in 0 until Aurora.SHEETS) {
            for (speaker in AuroraSpeaker.entries) {
                val shift = Aurora.peakShift(speaker, layer)
                for (peak in 0 until Aurora.PEAKS) {
                    val p = Aurora.peakProgress(peak, shift)
                    if (p < 0f || p > 1f) continue
                    assertEquals(
                        "sheet $layer $speaker mountain $peak is not standing on its own band",
                        levels[Aurora.peakBand(layer, peak)],
                        Aurora.ridge(p, levels, layer, shift),
                        1e-5f,
                    )
                }
                // Sampled far finer than the renderer walks it, so an overshoot cannot hide
                // between two samples.
                var prev = Aurora.ridge(0f, levels, layer, shift)
                var turns = 0
                for (i in 1..4000) {
                    val v = Aurora.ridge(i / 4000f, levels, layer, shift)
                    assertTrue("sheet $layer ridge went negative ($v)", v >= -1e-5f)
                    assertTrue("sheet $layer ridge exceeded its loudest band ($v)", v <= 0.9f + 1e-5f)
                    if ((v - prev) != 0f) turns++
                    prev = v
                }
                assertTrue("sheet $layer ridge is flat", turns > 0)
            }
        }
    }

    /**
     * THE ONE BRANDON ASKED FOR, PART ONE- "it seems like the ribbons are moving from right to left
     * across the screen, which we don't necessarily need".
     *
     * The old crest was `sin(progress * PI * freq + phase)`, a textbook travelling wave running at
     * 1.0 to 2.3 FULL cycles per second in -x. Half a second of the frame clock therefore slid the
     * entire ribbon past itself at least once. With band energies HELD CONSTANT the shape must not
     * move along x at all — the drift still moves it vertically, which is why this compares peak
     * POSITIONS rather than raw y.
     */
    @Test
    fun `the crest stands still while the bands are held`() {
        val levels = HELD_BANDS
        val laterPhases = STANDING_PHASES
        for (speaker in AuroraSpeaker.entries) {
            for (layer in 0 until Aurora.SHEETS) {
                val now = crestPeaks(levels, layer, speaker, phase = 0f)
                for (phase in laterPhases) {
                    val later = crestPeaks(levels, layer, speaker, phase)
                    assertEquals(
                        "sheet $layer $speaker changed shape at phase $phase on held audio",
                        now.size, later.size,
                    )
                    for (i in now.indices) {
                        // One sample of RIDGE_SAMPLES is 0.17% of the width. The travelling wave
                        // this replaces moved 100%+ of it in half a second.
                        assertTrue(
                            "sheet $layer $speaker mountain $i travelled from ${now[i]} to ${later[i]}",
                            abs(now[i] - later[i]) <= 1,
                        )
                    }
                }
                // Guard the guard- the ribbon is still ALIVE, it just moves VERTICALLY. A stubbed
                // drift satisfies every assertion above. Measured across the drift's own turn
                // rather than at one arbitrary instant, because half a second of the phase clock
                // is only a twentieth of that turn and lands near a stationary point on some
                // sheets — which is what a slow wander looks like, not a frozen one.
                val a = crestSweep(levels, layer, speaker, 0f)
                val moved = laterPhases.maxOf { phase ->
                    val b = crestSweep(levels, layer, speaker, phase)
                    a.indices.maxOf { abs(a[it] - b[it]) }
                }
                assertTrue("sheet $layer $speaker is frozen, not standing", moved > 0.01f * RIDGE_H)
            }
        }
    }

    /**
     * ...AND SO DOES THE BOTTOM EDGE, which is the half of the sheet nothing else here watches.
     *
     * The crest test above walks [Aurora.crestY] only, and the structural scan cannot cover the gap
     * either- the bottom edge takes its time signal as the `drift` PARAMETER, so a term smuggled in
     * through that argument is invisible to a source scan counting `nowPhase`. The reference
     * implementation's bottom edge was `sin(... + phase * mult2)`, a second travelling wave on the
     * other side of the body, so this is the exact line a later reader diffing against the source
     * would "restore". Mutation-checked- putting `drift * 0.05f` back into the bottom's peak shift
     * (about 4% of the width of sideways slosh at the drift's 0.43 Hz, far too little to notice by
     * eye) fails this test and passes every other one in the file.
     *
     * Same four later phases as the crest, same one-sample tolerance, and the same guard against
     * proving it by freezing the ribbon.
     */
    @Test
    fun `the bottom edge stands still while the bands are held`() {
        val levels = HELD_BANDS
        for (speaker in AuroraSpeaker.entries) {
            for (layer in 0 until Aurora.SHEETS) {
                val now = bottomPeaks(levels, layer, speaker, phase = 0f)
                // Not vacuous- there is a shape down there to hold still in the first place.
                assertTrue(
                    "sheet $layer $speaker bottom edge shows only ${now.size} mountains to test",
                    now.size >= 4,
                )
                for (phase in STANDING_PHASES) {
                    val later = bottomPeaks(levels, layer, speaker, phase)
                    assertEquals(
                        "sheet $layer $speaker bottom edge changed shape at phase $phase on held audio",
                        now.size, later.size,
                    )
                    for (i in now.indices) {
                        assertTrue(
                            "sheet $layer $speaker bottom mountain $i travelled from ${now[i]} to ${later[i]}",
                            abs(now[i] - later[i]) <= 1,
                        )
                    }
                }
                // The body still BREATHES vertically- the bottom edge rides the same drift the
                // crest does, so a stubbed drift must not be able to satisfy the assertions above.
                val a = bottomSweep(levels, layer, speaker, 0f, BOTTOM_TRIM, 1f - BOTTOM_TRIM)
                val moved = STANDING_PHASES.maxOf { phase ->
                    val b = bottomSweep(levels, layer, speaker, phase, BOTTOM_TRIM, 1f - BOTTOM_TRIM)
                    a.indices.maxOf { abs(a[it] - b[it]) }
                }
                assertTrue("sheet $layer $speaker bottom edge is frozen, not standing", moved > 0.01f * RIDGE_H)
            }
        }
    }

    /**
     * THE ONE BRANDON ASKED FOR, PART TWO- "all of the different hertz levels to trigger different
     * mountains".
     *
     * Each band owns its own mountains at a stable x, so raising ONE band lifts the crest there and
     * NOWHERE else — and, in the second half, a band left QUIET beside a roaring one still KEEPS a
     * mountain of its own.
     *
     * That second half is what pins the saddle rule to `min` of the two neighbouring mountains
     * rather than their mean, and it has to be asserted as a strict LOCAL MAXIMUM because nothing
     * else here can see the difference: the raised cosine is a partition of unity and passes
     * exactly through every control point, so the rise measured AT a peak is identically zero under
     * either rule and the cross-talk bound below is blind to it. So is the peak COUNT, because a
     * swallowed mountain simply drops under the prominence floor. A mean saddle between a 1.0 and a
     * 0.06 mountain sits at 0.1855 and buries the quiet one — measured, it turns 91 of the peaks
     * checked below into local MINIMA.
     */
    @Test
    fun `each band drives its own mountains and leaves the others alone`() {
        val quiet = 0.25f
        for (band in 0 until Aurora.SHEETS) {
            val base = FloatArray(Aurora.SHEETS) { quiet }
            val lifted = FloatArray(Aurora.SHEETS) { if (it == band) 0.75f else quiet }
            for (speaker in AuroraSpeaker.entries) {
                for (layer in 0 until Aurora.SHEETS) {
                    var best = 0f
                    for (peak in 0 until Aurora.PEAKS) {
                        val p = Aurora.peakProgress(peak, Aurora.peakShift(speaker, layer))
                        if (p < 0f || p > 1f) continue
                        val rise = crestAt(base, layer, speaker, p) - crestAt(lifted, layer, speaker, p)
                        if (Aurora.peakBand(layer, peak) == band) {
                            best = maxOf(best, rise)
                        } else {
                            assertTrue(
                                "band $band leaked into band ${Aurora.peakBand(layer, peak)}'s " +
                                    "mountain on sheet $layer ($speaker): $rise px",
                                abs(rise) < 0.01f * RIDGE_H,
                            )
                        }
                    }
                    // At least one of this band's two mountains has to stand up properly. 7% of the
                    // container, not more, because the sibilance band deliberately owns the
                    // OUTERMOST pair (see PEAK_BANDS) and on the front sheet both of those sit
                    // under the edge taper — a half-scale mountain there is the taper working, and
                    // 12 px on a 156 px ribbon is still plainly a mountain appearing.
                    assertTrue(
                        "band $band moves nothing on sheet $layer ($speaker): best rise $best px",
                        best > 0.07f * RIDGE_H,
                    )
                }
            }
        }

        // NO BAND MAY BE SWALLOWED. Maximally lopsided — one band at full drive, every other at
        // the resting baseline — and every mountain still on screen must be a strict local maximum
        // of the ridge, taller than the saddle on BOTH sides of it. Asserted on the ridge itself
        // rather than through crestY, because the saddles either side of an outermost mountain can
        // fall outside 0..1 where the taper and the clamp would mask the shape.
        for (band in 0 until Aurora.SHEETS) {
            val lopsided = FloatArray(Aurora.SHEETS) { if (it == band) 1f else Aurora.BASELINE }
            for (speaker in AuroraSpeaker.entries) {
                for (layer in 0 until Aurora.SHEETS) {
                    val shift = Aurora.peakShift(speaker, layer)
                    for (peak in 0 until Aurora.PEAKS) {
                        val p = Aurora.peakProgress(peak, shift)
                        if (p < 0f || p > 1f) continue
                        val top = Aurora.ridge(p, lopsided, layer, shift)
                        for (side in floatArrayOf(-HALF_STEP, HALF_STEP)) {
                            val saddle = Aurora.ridge(p + side, lopsided, layer, shift)
                            assertTrue(
                                "band $band swallowed band ${Aurora.peakBand(layer, peak)}'s mountain " +
                                    "$peak on sheet $layer ($speaker): peak $top, saddle $saddle - " +
                                    "the saddle rule is no longer min() of the two neighbours",
                                top > saddle + 1e-3f,
                            )
                        }
                    }
                }
            }
        }
    }

    /**
     * THE DIRECT REGRESSION TEST FOR "RIGHT NOW IT SEEMS PRETTY FLAT".
     *
     * The measured cause was never gain- steady speech already drove sheet 0 to 62% of the
     * available half-height. It was that `sin(progress * PI * freq)` with freq 1.15..2.80 puts
     * barely more than ONE broad arc across the whole width, and a 62%-tall single arc on a 1000 px
     * bar is a gentle bulge. So this counts MOUNTAINS, and it fails if anyone reduces the ridge
     * back toward one hump. Do not "fix" a failure here by raising the amplitude.
     */
    @Test
    fun `the ribbon reads as a range of mountains and not as one arc`() {
        // A steady-speech drive (the analyser's measured ~0.58) and a lopsided one.
        for (levels in listOf(
            FloatArray(Aurora.SHEETS) { 0.58f },
            floatArrayOf(0.62f, 0.3f, 0.5f, 0.2f),
        )) {
            for (speaker in AuroraSpeaker.entries) {
                for (layer in 0 until Aurora.SHEETS) {
                    val peaks = crestPeaks(levels, layer, speaker, phase = 0f)
                    // 6..9 rather than exactly PEAKS: a lattice shifted by the speaker offset or
                    // the per-layer skew lands one of its mountains under the edge taper, which
                    // flattens it out of existence. That is the taper doing its job, not a
                    // regression — what would be a regression is a couple of broad humps.
                    assertTrue(
                        "sheet $layer ($speaker) shows ${peaks.size} mountains across the width",
                        peaks.size in 6..9,
                    )
                }
            }
        }
    }

    // =========================================================================
    // C1 — two voices in one container
    // =========================================================================

    /**
     * A FloatArray in a data class compares by IDENTITY, so two voices carrying equal band data
     * would compare unequal and Compose would recompose on every audio chunk forever. Content
     * equality is deliberate.
     */
    @Test
    fun `voice equality is content based`() {
        val a = AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.1f, 0.2f, 0.3f, 0.4f))
        val b = AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.1f, 0.2f, 0.3f, 0.4f))
        assertEquals(a, b)
        assertEquals(a.hashCode(), b.hashCode())
        assertNotEquals(a, AuroraVoice(AuroraSpeaker.AI, floatArrayOf(0.1f, 0.2f, 0.3f, 0.4f)))
        assertNotEquals(a, AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.1f, 0.2f, 0.3f, 0.9f)))
    }

    /**
     * The audio thread produces bands, the frame thread reads them. A caller reusing one scratch
     * buffer must not be able to mutate a voice we have already compared equal and skipped.
     */
    @Test
    fun `a voice copies the caller band array`() {
        val scratch = floatArrayOf(0.5f, 0.5f, 0.5f, 0.5f)
        val v = AuroraVoice(AuroraSpeaker.HUMAN, scratch)
        val before = AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.5f, 0.5f, 0.5f, 0.5f))
        scratch[0] = 1f
        assertEquals(0.5f, v.band(0), 1e-6f)
        assertEquals(before, v)
    }

    /**
     * Sheet j follows band j. A caller with fewer bands than sheets (an RMS-only source) drives
     * the remaining sheets from its last band rather than leaving them dead — that is the
     * original's global-level fallback, expressed per voice.
     */
    @Test
    fun `a short band array drives the remaining sheets from its last band`() {
        val rms = AuroraVoice(AuroraSpeaker.AI, floatArrayOf(0.7f))
        for (j in 0 until Aurora.SHEETS) assertEquals(0.7f, rms.band(j), 1e-6f)
        val empty = AuroraVoice(AuroraSpeaker.AI, FloatArray(0))
        for (j in 0 until Aurora.SHEETS) assertEquals(0f, empty.band(j), 1e-6f)
        // Out-of-range indices clamp rather than crash a frame.
        assertEquals(0.7f, rms.band(-1), 1e-6f)
        assertEquals(0.7f, rms.band(99), 1e-6f)
        // Values are clamped into 0..1 whatever a hand-rolled caller supplies.
        val wild = AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(-3f, 5f, 0.5f, 0f))
        assertEquals(0f, wild.band(0), 1e-6f)
        assertEquals(1f, wild.band(1), 1e-6f)
        // NaN is the ONE value coerceIn does not catch- every comparison against NaN is false, so
        // neither the `< min` nor the `> max` branch fires and it sails through. It is also the
        // likeliest bad input from a real call site: any level computed as rms/peak with a zero
        // peak, or an envelope divided by a silent clip's maximum, is exactly this. Reads as
        // silence; the neighbouring bands are unaffected.
        val nan = AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(Float.NaN, 0.5f, Float.NaN, 0f))
        assertEquals(0f, nan.band(0), 1e-6f)
        assertEquals(0.5f, nan.band(1), 1e-6f)
        assertEquals(0f, nan.band(2), 1e-6f)
    }

    /**
     * A NaN band must not poison the ribbon PERMANENTLY, and must not silently park the loop.
     *
     * The exponential approach is absorbing- `NaN + (target - NaN) * k` is NaN on every subsequent
     * step, so one bad chunk would flatline the sheet forever even after clean audio resumed. Worse,
     * the failure hides: `abs(NaN - floor) > SETTLE_EPS` is false so the envelope reports itself
     * SETTLED, and a `hasEnergy` reading the raw bands reports silence, so the frame loop parks —
     * having just handed Skia a path built entirely from NaN points (NaN also fails the
     * MIN_VISIBLE_DRIVE skip, being neither above nor below it).
     */
    @Test
    fun `a NaN band cannot poison the envelope or strand the frame loop`() {
        val e = AuroraEngine()
        drive(1000, 0.2, listOf(loud(AuroraSpeaker.HUMAN)), e)
        val poison = listOf(AuroraVoice(AuroraSpeaker.HUMAN, FloatArray(Aurora.SHEETS) { Float.NaN }))
        drive(1000, 0.05, poison, e)
        for (j in 0 until Aurora.SHEETS) {
            assertTrue("sheet $j went NaN", e.envelope(AuroraSpeaker.HUMAN).levels[j].isFinite())
        }

        // RECOVERY- clean bands after the glitch bring the ribbon straight back.
        drive(1000, 0.2, listOf(loud(AuroraSpeaker.HUMAN)), e)
        for (j in 0 until Aurora.SHEETS) {
            val level = e.envelope(AuroraSpeaker.HUMAN).levels[j]
            assertTrue("sheet $j never recovered ($level)", level > 0.9f)
        }

        // AGREEMENT- a NaN band reads as silence in BOTH places. `needsFrames` consults the bands
        // directly while the envelope consults them through band(); the two reading the same value
        // is what stops the loop parking on a picture the envelope has not finished drawing.
        val nanOnly = listOf(AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(Float.NaN)))
        assertTrue("a NaN voice still has to animate DOWN", e.needsFrames(nanOnly))
        // 3 s, not the 2 s the from-zero baseline test uses- these sheets start SATURATED, and the
        // slowest band release (0.295 s) still leaves them ~0.001 above the baseline at 2 s.
        drive(1000, 3.0, nanOnly, e)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("sheet $j", Aurora.BASELINE, e.envelope(AuroraSpeaker.HUMAN).levels[j], 1e-3f)
        }
        assertFalse("a settled NaN voice must stop asking for frames", e.needsFrames(nanOnly))
    }

    /**
     * The band WIRING and the sqrt perceptual lift, pinned where either can actually be observed-
     * a NON-uniform band vector whose one live band is neither 0 nor 1.
     *
     * The test above only exercises the [AuroraVoice.band] accessor, and every other engine test
     * here drives [1,1,1,1] or [0,0,0,0]. 0 and 1 are exactly the two fixed points of sqrt, and a
     * uniform vector reads the same whichever band a sheet picks up, so on those inputs BOTH
     * properties are invisible- dropping the sqrt and collapsing all four sheets onto band 0 left
     * the whole suite green. This input sees both.
     */
    @Test
    fun `sheet j follows band j through the perceptual sqrt lift`() {
        val e = AuroraEngine()
        // Bass only, at a quarter energy — well clear of sqrt's fixed points. 0.2 s is ~12 attack
        // time constants, so every sheet has fully arrived at its target.
        drive(1000, 0.2, listOf(AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.25f, 0f, 0f, 0f))), e)
        val levels = e.envelope(AuroraSpeaker.HUMAN).levels

        // THE LIFT- sqrt(0.25) is 0.5, DOUBLE the raw band. It is what lets a quiet-but-present
        // band still animate; feeding the band through raw would park this sheet at 0.25.
        assertEquals("sheet 0 lost the sqrt perceptual lift", sqrt(0.25f), levels[0], 1e-3f)

        // THE WIRING- bands 1..3 are silent, so their sheets rest at the baseline. A sheet reading
        // a shared band instead of its own would be sitting up at 0.5 alongside sheet 0.
        for (j in 1 until Aurora.SHEETS) {
            assertEquals("sheet $j is not following band $j", Aurora.BASELINE, levels[j], 1e-3f)
        }
        assertTrue("sheet 0 collapsed onto the silent sheets", levels[0] - levels[1] > 0.3f)
    }

    /**
     * THE LOAD-BEARING REQUIREMENT. Fed byte-identical audio at the same instant, the human and
     * the AI ribbons must still be visibly apart — colour alone cannot separate two sheets that
     * sit on top of each other. Measured as mean vertical separation of the crest lines.
     */
    @Test
    fun `the two voices stay visibly separated when fed identical audio`() {
        val h = 156f
        val w = 1080f
        val steps = Aurora.sampleCount(w)
        val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 3f))
        for (drive in floatArrayOf(0.35f, 0.7f, 1f)) {
            val levels = FloatArray(Aurora.SHEETS) { drive }
            var worstLayer = Float.MAX_VALUE
            for (layer in 0 until Aurora.SHEETS) {
                var total = 0f
                var n = 0
                for (frame in 0..24) {
                    val phase = frame * (Aurora.PHASE_WRAP_RAD / 24f)
                    val amp = Aurora.amplitude(h, layer)
                    for (i in 0..steps) {
                        val p = i.toFloat() / steps
                        val yh = Aurora.crestY(p, h, inset, amp, levels, layer,
                            Aurora.peakShift(AuroraSpeaker.HUMAN, layer),
                            Aurora.drift(h, phase, layer, Aurora.driftBias(AuroraSpeaker.HUMAN)))
                        val ya = Aurora.crestY(p, h, inset, amp, levels, layer,
                            Aurora.peakShift(AuroraSpeaker.AI, layer),
                            Aurora.drift(h, phase, layer, Aurora.driftBias(AuroraSpeaker.AI)))
                        total += abs(yh - ya)
                        n++
                    }
                }
                worstLayer = minOf(worstLayer, total / n)
            }
            // 10% of the container height PER UNIT OF DRIVE, averaged over the whole ribbon and a
            // full phase cycle. Scaled by the drive because C7 made the separation SPATIAL: two
            // ribbons offset by half a peak spacing are pulled apart by the RELIEF between a
            // mountain and its saddle, and that relief is the audio. A pair of near-flat quiet
            // ribbons cannot be far apart in absolute pixels and does not need to be — there is
            // nothing there to mush together. The travelling wave's flat 8% was measured against a
            // pi phase bias, which separated a full amplitude regardless of how quiet it got.
            assertTrue(
                "voices overlap into mush at drive=$drive (mean separation ${worstLayer / h} of height)",
                worstLayer > 0.10f * h * drive,
            )
        }
    }

    /**
     * ...and the MECHANISM, since C7 changed it. The travelling wave was separated by a pi phase
     * bias, which on a standing ridge would shift precisely nothing; the separation is spatial now,
     * so what has to hold is that the AI's mountains land in the human's saddles.
     *
     * Asserted on the peak POSITIONS rather than on a y separation, so it survives the two voices
     * happening to be at the same drive, at the same drift, at the same instant — which is exactly
     * the moment the mean-separation test above is weakest.
     */
    @Test
    fun `the AI mountains stand in the human saddles`() {
        val spacing = 1f / Aurora.PEAKS
        for (layer in 0 until Aurora.SHEETS) {
            val human = Aurora.peakShift(AuroraSpeaker.HUMAN, layer)
            val ai = Aurora.peakShift(AuroraSpeaker.AI, layer)
            assertNotEquals("the two lattices collapsed onto each other", human, ai)
            for (peak in 0 until Aurora.PEAKS) {
                val hp = Aurora.peakProgress(peak, human)
                // The nearest AI mountain to this human one, wherever the lattice has wrapped it.
                val nearest = (-1..Aurora.PEAKS).minOf { abs(Aurora.peakProgress(it, ai) - hp) }
                assertEquals(
                    "sheet $layer peak $peak- the AI mountain is not sitting in the human saddle",
                    spacing / 2f, nearest, 1e-4f,
                )
            }
        }
        // A LONE voice still looks centred. The front sheet's lattice covers the container
        // symmetrically for BOTH identities — the AI's half-step shift moves a mountain onto each
        // edge rather than sliding the whole range sideways — so neither reads as shoved to one
        // side when it is on its own. (The per-layer skew deliberately breaks this behind the
        // front sheet; that is the ladder asserted above, and it is invisible as a bias because
        // the two directions of the stack cancel.)
        for (speaker in AuroraSpeaker.entries) {
            val shift = Aurora.peakShift(speaker, 0)
            val onScreen = (-1..Aurora.PEAKS)
                .map { Aurora.peakProgress(it, shift) }
                .filter { it >= -1e-4f && it <= 1f + 1e-4f }
            assertTrue("$speaker has no mountains on screen at all", onScreen.size >= 7)
            assertEquals(
                "$speaker sits off centre on its own",
                0.5f, onScreen.average().toFloat(), 1e-4f,
            )
        }
    }

    /** Envelopes are keyed by SPEAKER. Keyed by list index, a joining AI would inherit the human's. */
    @Test
    fun `each speaker keeps its own envelope across list churn`() {
        val e = AuroraEngine()
        val human = loud(AuroraSpeaker.HUMAN)
        drive(1000, 0.3, listOf(human), e)
        assertTrue(e.envelope(AuroraSpeaker.HUMAN).levels[0] > 0.9f)
        assertEquals(0f, e.envelope(AuroraSpeaker.AI).levels[0], 1e-4f)
        // The AI joins at the FRONT of the list, holding silence.
        drive(1000, 0.01, listOf(silent(AuroraSpeaker.AI), human), e)
        assertTrue("AI inherited the human envelope", e.envelope(AuroraSpeaker.AI).levels[0] < 0.2f)
        assertTrue("human lost its envelope", e.envelope(AuroraSpeaker.HUMAN).levels[0] > 0.9f)
    }

    /**
     * Present-but-silent breathes at the baseline; GONE goes flat. A stream that ends has to
     * retire the ribbon completely, not park a permanent stub on screen.
     */
    @Test
    fun `a silent voice breathes at the baseline and an absent one retires to flat`() {
        val e = AuroraEngine()
        drive(1000, 2.0, listOf(silent(AuroraSpeaker.HUMAN)), e)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("sheet $j", Aurora.BASELINE, e.envelope(AuroraSpeaker.HUMAN).levels[j], 1e-3f)
        }
        drive(1000, 2.0, emptyList(), e)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("sheet $j", 0f, e.envelope(AuroraSpeaker.HUMAN).levels[j], 1e-3f)
        }
    }

    /**
     * ...and a call site may move that floor to ZERO, which is what the player bar does.
     *
     * A silent passage inside a PLAYING clip keeps its voice present (the bands are simply all
     * zero — the decoded envelope gates true silence to exactly 0), so "flat on silence" cannot
     * come from dropping the voice; it has to come from the floor. At the default baseline the
     * same input parks every sheet ~1.1dp up on a 40dp bar, which is not what VoiceWaveform's
     * `idleLevel = 0f` did there and not what M3 is allowed to ship.
     */
    @Test
    fun `a zero rest level keeps a present but silent voice flat`() {
        val e = AuroraEngine(restLevel = 0f)
        // Loud FIRST, so the sheets have somewhere to fall from- a fresh engine already sits at 0
        // and would pass this without the floor ever being consulted.
        drive(1000, 0.5, listOf(loud(AuroraSpeaker.AI)), e)
        drive(1000, 3.0, listOf(silent(AuroraSpeaker.AI)), e)
        val levels = e.envelope(AuroraSpeaker.AI).levels
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("sheet $j rested above flat", 0f, levels[j], 1e-3f)
        }
        // ...and it still parks. A ribbon that is flat AND settled but keeps asking for frames is
        // a battery leak on a screen that can hold dozens of these.
        assertFalse(
            "a flat, settled voice kept asking for frames",
            e.needsFrames(listOf(silent(AuroraSpeaker.AI))),
        )
    }

    /**
     * FLAT MUST MEAN A FLAT RIBBON, NOT A BLANK BOX.
     *
     * The test above asserts the LEVELS reach 0 and would pass just as happily if the renderer then
     * painted nothing at all — which is exactly what a `drive > MIN_VISIBLE_DRIVE` cull does to a
     * voice resting at a zero floor, since 0 is below the threshold. That is a regression against
     * the behaviour M3 has to preserve: VoiceWaveform at amplitude 0 still stroked its centre line
     * across the bar, so an idle bar in a message list showed a flat ribbon, not an empty 40dp box,
     * and a silent passage mid-clip did not blink the ribbon out and back.
     *
     * So: a PRESENT voice paints whatever its drive, an ABSENT one still retires, and at drive 0
     * the geometry really does yield ink — a flat crest with the body sliver hanging under it.
     */
    @Test
    fun `a present but settled zero-drive voice still paints a flat ribbon`() {
        val e = AuroraEngine(restLevel = 0f)
        drive(1000, 0.5, listOf(loud(AuroraSpeaker.AI)), e)          // somewhere to fall from
        drive(1000, 3.0, listOf(silent(AuroraSpeaker.AI)), e)        // ...down to the zero floor
        val env = e.envelope(AuroraSpeaker.AI)

        // The player bar's own geometry: 40dp at Fold density, full width.
        val h = 40f * 3f
        val w = 1080f
        val crestPx = Aurora.crestStrokePx(h, 3f)
        val inset = Aurora.insetPx(h, crestPx)
        val steps = Aurora.sampleCount(w)

        for (layer in 0 until Aurora.SHEETS) {
            val d = env.levels[layer]
            // Guard the guard- if the sheets were NOT below the cull threshold this test would be
            // passing on drive the renderer never had to make an exception for.
            assertTrue("sheet $layer is not resting under the cull threshold ($d)", d <= Aurora.MIN_VISIBLE_DRIVE)
            assertTrue(
                "sheet $layer was culled - the idle player bar paints an empty box",
                Aurora.paintsSheet(env.present, env.loudest),
            )

            // Parked, so the phase is frozen wherever the frame loop stopped- 0 will do.
            val phase = 0f
            val amp = Aurora.amplitude(h, layer)
            val drift = Aurora.drift(h, phase, layer, Aurora.driftBias(AuroraSpeaker.AI))
            val crestYs = FloatArray(steps + 1)
            val bottomYs = FloatArray(steps + 1)
            var ci = 0
            var bi = steps
            forEachSheetPoint(
                w, h, steps, inset, amp, env.levels, layer,
                Aurora.peakShift(AuroraSpeaker.AI, layer), drift,
                onCrest = { x, y, _ ->
                    assertInside("flat crest layer $layer", x, y, w, h, crestPx / 2f)
                    crestYs[ci++] = y
                },
                onBottom = { x, y ->
                    assertInside("flat bottom layer $layer", x, y, w, h, crestPx / 2f)
                    bottomYs[bi--] = y
                },
            )
            assertEquals("crest was not walked", steps + 1, ci)
            assertEquals("bottom was not walked", -1, bi)

            // INK- the body between the two edges is what stands in for the old flat line. A sheet
            // whose crest and bottom coincide would be drawn and still be invisible.
            val gap = (0..steps).maxOf { bottomYs[it] - crestYs[it] }
            assertTrue("sheet $layer paints no visible body (gap $gap of $h)", gap > 0.02f * h)

            // FLAT- through the middle, where the taper is not clamping, the crest must not swing.
            // Measured in PIXELS and not required to be exactly constant: an exponential release
            // approaches its floor without ever landing on it, so ~1e-5 of drive survives forever
            // and leaves a trace of the ripple in the geometry. A twentieth of a pixel of residual
            // swing is flat — the line it replaces was a single row of pixels itself.
            var lo = Float.MAX_VALUE
            var hi = -Float.MAX_VALUE
            for (i in steps / 4..steps * 3 / 4) {
                lo = minOf(lo, crestYs[i])
                hi = maxOf(hi, crestYs[i])
            }
            assertTrue("sheet $layer visibly swings at rest (${hi - lo} px)", hi - lo < 0.05f)
        }

        // ...and the threshold still retires an ABSENT voice, which is the case it exists for.
        drive(1000, 3.0, emptyList(), e)
        val gone = e.envelope(AuroraSpeaker.AI)
        for (layer in 0 until Aurora.SHEETS) {
            assertFalse(
                "a departed voice must leave the surface, not park a flat stub on it",
                Aurora.paintsSheet(gone.present, gone.levels[layer]),
            )
        }
        // A ribbon nobody has ever fed costs nothing either.
        val fresh = AuroraEngine().envelope(AuroraSpeaker.HUMAN)
        for (layer in 0 until Aurora.SHEETS) {
            assertFalse("an unused ribbon painted", Aurora.paintsSheet(fresh.present, fresh.levels[layer]))
        }
    }

    /**
     * The engine test above passes whether or not the player bar actually ASKS for a flat rest, so
     * pin the call site too. This is the one surface whose silence is a file's silence rather than
     * a live mic's, and it is the surface the requirement names.
     */
    @Test
    fun `the player bar asks for a flat rest level`() {
        val src = stripComments(playerBarSource())
        assertTrue(
            "AudioPlayerBar stopped passing restLevel = 0f - a silent passage breathes again",
            src.contains(Regex("restLevel\\s*=\\s*0f")),
        )
    }

    /**
     * ...and it must keep its voice PRESENT while idle, or the flat rest is never asked for at all.
     *
     * The renderer only exempts a present voice from the visibility cull (a departed one has to be
     * able to leave), so handing it `null` when nothing is playing retires the ribbon and every
     * idle bar in a message list becomes an empty box with a progress track. VoiceWaveform painted
     * its line whether or not the clip was playing; this is the same contract expressed as
     * presence.
     */
    @Test
    fun `the player bar keeps its ribbon present while idle`() {
        val src = stripComments(playerBarSource())
        assertTrue(
            "the idle player bar stopped feeding bands - its ribbon retires to an empty box",
            src.contains(Regex("!thisPlaying\\s*->\\s*AURORA_SILENT_BANDS")),
        )
        assertFalse(
            "a nullable ribbon can go ABSENT - an idle bar must rest flat, not retire",
            src.contains("FloatArray?"),
        )
    }

    /**
     * A departing voice retires on the GLOBAL release constant rather than the four per-band
     * ones, so the ribbon leaves as one object instead of unravelling sheet by sheet.
     */
    @Test
    fun `a departing voice retires on the global release constant`() {
        val e = AuroraEngine()
        drive(1000, 0.5, listOf(loud(AuroraSpeaker.HUMAN)), e)
        val start = e.envelope(AuroraSpeaker.HUMAN).levels[0]
        drive(1000, Aurora.RELEASE_TAU_SEC.toDouble(), emptyList(), e)
        val levels = e.envelope(AuroraSpeaker.HUMAN).levels
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("sheet $j left at its own rate", start * exp(-1f), levels[j], 5e-3f)
        }
    }

    // =========================================================================
    // C2 + C5 — solid colour, nothing opaque
    // =========================================================================

    /**
     * Depth comes from the ALPHA ladder, not from hue (C2 deleted the gradient). Every one of the
     * three passes on every sheet must therefore be translucent — and visible.
     */
    @Test
    fun `every draw pass is translucent`() {
        for (layer in 0 until Aurora.SHEETS) {
            for ((name, a) in listOf(
                "fill" to Aurora.fillAlpha(layer),
                "bloom" to Aurora.bloomAlpha(layer),
                "crest" to Aurora.crestAlpha(layer),
            )) {
                assertTrue("$name $layer alpha $a not in 0..1 exclusive of opaque", a > 0f && a < 1f)
            }
            // The ladder recedes with depth, which is the whole trick.
            if (layer > 0) {
                assertTrue(Aurora.fillAlpha(layer) < Aurora.fillAlpha(layer - 1))
                assertTrue(Aurora.bloomAlpha(layer) < Aurora.bloomAlpha(layer - 1))
                assertTrue(Aurora.crestAlpha(layer) < Aurora.crestAlpha(layer - 1))
            }
            // Body dimmer than its own crest line, bloom dimmest — the source's depth stack.
            assertTrue(Aurora.bloomAlpha(layer) < Aurora.fillAlpha(layer))
            assertTrue(Aurora.fillAlpha(layer) < Aurora.crestAlpha(layer))
        }
    }

    /**
     * This project has TWICE shipped a waveform inside an opaque container and hidden the
     * particle field behind it. The renderer is a ribbon and NOTHING else, so the source may not
     * contain a background, a filled rect, a Surface or a Card — nor any of the gradient
     * machinery C2 deleted. Comments are stripped first so this file's own prose cannot trip it.
     */
    @Test
    fun `the renderer paints no opaque container and no gradient`() {
        val src = stripComments(rendererSource())
        val banned = listOf(
            ".background(" to "an opaque container would hide the particle field",
            "drawRect(" to "a filled rect is a background by another name",
            "drawRoundRect(" to "a filled rect is a background by another name",
            "drawCircle(" to "the blob is gone - ribbon only",
            "Surface(" to "a Surface paints its container colour",
            "Card(" to "a Card paints its container colour",
            "BbxBlack" to "no opaque brand surface behind the ribbon",
            "BbxDark" to "no opaque brand surface behind the ribbon",
            "BbxSurface" to "no opaque brand surface behind the ribbon",
            "Color.Black" to "no opaque fill",
            "Brush" to "C2 removed the gradient - solid red and solid blue only",
            "Gradient" to "C2 removed the gradient - solid red and solid blue only",
            "Shader" to "C2 removed the gradient - solid red and solid blue only",
        )
        for ((token, why) in banned) {
            assertFalse("AuroraWaveform.kt contains `$token` - $why", src.contains(token))
        }
        // ...and it really does paint, so the scan is not passing on an empty file.
        assertTrue(src.contains("drawPath("))
    }

    /** Both speaker colours are fully opaque brand tokens- the ALPHA is applied per pass. */
    @Test
    fun `each speaker owns one solid identity colour`() {
        val human = auroraSpeakerColor(AuroraSpeaker.HUMAN)
        val ai = auroraSpeakerColor(AuroraSpeaker.AI)
        assertNotEquals("red and blue must differ", human, ai)
        assertEquals("human alpha", 1f, human.alpha, 1e-6f)
        assertEquals("ai alpha", 1f, ai.alpha, 1e-6f)
        // Red is the human, blue is the AI — asserted on the channels, not the token name, so
        // swapping the two tokens fails here.
        assertTrue("human is not red", human.red > human.blue && human.red > human.green)
        assertTrue("ai is not blue", ai.blue > ai.red && ai.blue > ai.green)
    }

    // =========================================================================
    // C6 — no idle frame loop
    // =========================================================================

    @Test
    fun `the engine stops asking for frames once every voice is silent and settled`() {
        val e = AuroraEngine()
        // Nothing has ever been fed- a composed but unused ribbon costs zero frames.
        assertFalse("a fresh engine asked for frames", e.needsFrames(emptyList()))

        val quiet = listOf(silent(AuroraSpeaker.HUMAN))
        assertTrue("a voice arriving must wake the loop", e.needsFrames(quiet))
        drive(1000, 2.0, quiet, e)
        assertFalse("settled at the baseline but still asking for frames", e.needsFrames(quiet))

        // Audio arrives -> frames again.
        assertTrue(e.needsFrames(listOf(loud(AuroraSpeaker.HUMAN))))
    }

    /**
     * The wake-up condition cannot be "someone is audible" alone- a stream that ENDS while parked
     * would leave its ribbon frozen on screen forever with nothing to animate it away.
     */
    @Test
    fun `a voice leaving keeps frames running until it has retired`() {
        val e = AuroraEngine()
        drive(1000, 2.0, listOf(silent(AuroraSpeaker.HUMAN)), e)
        assertFalse(e.needsFrames(listOf(silent(AuroraSpeaker.HUMAN))))
        assertTrue("departure ignored", e.needsFrames(emptyList()))
        drive(1000, 2.0, emptyList(), e)
        assertFalse("still spinning after retiring", e.needsFrames(emptyList()))
    }

    /**
     * `pauseWhenIdle = false` is an escape hatch for a PRESENT voice that has to keep flowing
     * through a silence. It may NOT hold the loop open on an EMPTY list, where the renderer draws
     * no pixels at all and the loop would be stepping the engine and invalidating the draw phase at
     * up to 120 Hz forever. The voice screen is the one surface that passes false, and it sits in
     * exactly that state before its session connects and after it disconnects.
     */
    @Test
    fun `the frame loop parks on an empty voice list whatever pauseWhenIdle says`() {
        val src = stripComments(rendererSource())
        assertTrue(
            "the idle guard stopped widening on an empty list - `pauseWhenIdle = false` now spins forever",
            Regex("""if\s*\(\s*\(\s*pauseWhenIdle\s*\|\|\s*\w+\.isEmpty\(\)\s*\)\s*&&\s*!\s*\w+\.needsFrames""")
                .containsMatchIn(src),
        )
        // The two properties that make parking there safe rather than a frozen ribbon- a retired
        // voice reports nothing left to animate, and nothing of it is painted.
        val e = AuroraEngine()
        drive(1000, 2.0, listOf(loud(AuroraSpeaker.AI)), e)
        drive(1000, 2.0, emptyList(), e)
        assertFalse("a retired ribbon still asks for frames", e.needsFrames(emptyList()))
        for (layer in 0 until Aurora.SHEETS) {
            assertFalse(
                "a retired sheet is still painted, so parking would freeze something visible",
                Aurora.paintsSheet(present = false, drive = e.envelope(AuroraSpeaker.AI).levels[layer]),
            )
        }
    }

    /**
     * THE FIRST FRAME HAS TO PAINT, AND IT IS THE ONE FRAME THAT ADVANCES NOTHING.
     *
     * `dt` is 0 on the first `withFrameNanos` of a fresh composition, a resume or a wake — by
     * design, so a resume cannot teleport the animation. That frame still does the one thing the
     * idle player bar's whole rest state hangs on: it flips the voice to PRESENT, which is what
     * exempts a zero-drive sheet from [Aurora.paintsSheet]'s cull. Nothing else about the engine
     * moves — `phaseRad + 7.8 * 0` is the value it already held, and [Aurora.approach] returns
     * `current` untouched at dt <= 0.
     *
     * So a draw phase subscribed to a VALUE mirrored off the engine sees no write at all
     * (SnapshotMutableFloatStateImpl compares old against new and branches past the record), and
     * the loop then parks, because a present, silent, zero-floor voice is settled and audible in no
     * sense. The flat ribbon M3 requires is authorised and never painted: an empty 40dp box on an
     * idle AudioPlayerBar, healing only when something else happens to recompose the bar — which in
     * a scrolling list makes it intermittent rather than reproducible, i.e. the worst kind.
     *
     * Two independent guarantees, both pinned: the draw phase is subscribed to a monotonic TICK
     * that cannot be written equal, and the loop refuses to park on a frame that advanced no time.
     */
    @Test
    fun `the first zero-dt frame paints the flat ribbon instead of parking on it`() {
        // AudioPlayerBar's idle state exactly- present, all-zero bands, resting on a zero floor.
        val idle = listOf(silent(AuroraSpeaker.AI))
        val e = AuroraEngine(restLevel = 0f)
        e.step(0f, idle)
        val env = e.envelope(AuroraSpeaker.AI)

        // The frame that AUTHORISES the ribbon...
        assertTrue("a present voice must paint whatever its drive", Aurora.paintsSheet(env.present, env.loudest))
        // ...while changing nothing a value mirror could notice, which is the whole trap.
        assertEquals("a zero-dt frame moved the phase", 0f, e.phaseRad, 0f)
        assertEquals("a zero-dt frame moved an envelope", 0f, env.loudest, 0f)
        assertFalse("the engine has nothing left to animate after this frame", e.needsFrames(idle))

        val src = stripComments(rendererSource())
        // (1) The subscription is a TICK, bumped unconditionally on every stepped frame.
        assertTrue(
            "the frame loop stopped bumping a monotonic tick - an equal write invalidates nothing",
            Regex("""tick\.intValue\s*\+\+""").containsMatchIn(src),
        )
        assertTrue(
            "the draw phase stopped reading the tick - it is no longer subscribed to the frame loop",
            Regex("""onDrawBehind\s*\{[^}]*tick\.intValue""", RegexOption.DOT_MATCHES_ALL).containsMatchIn(src),
        )
        assertFalse(
            "the draw phase subscribes to a mirrored float again - a zero-dt frame writes it equal " +
                "and invalidates nothing",
            src.contains("mutableFloatStateOf"),
        )
        // (2) ...and the park refuses a frame that has not advanced the clock, so the loop can
        // never settle on a state it has not actually animated to.
        assertTrue(
            "the idle park stopped requiring elapsed time - the first frame can park before it has " +
                "animated anything",
            Regex("""needsFrames\([^)]*\)\s*&&\s*\w+\s*>\s*0f""").containsMatchIn(src),
        )
    }

    /**
     * The two hoisted Paths are rebuilt in place every frame, so they must be REWOUND, not RESET.
     * `Path.reset()` forwards to android.graphics.Path.reset(), which returns the path "to the same
     * state it had when it was created" — it drops the native point/verb storage. `rewind()` is the
     * documented sibling that "keeps the internal data structure for faster reuse". With reset the
     * draw path re-grows two native buffers of up to ~242 points EIGHT times per frame (4 layers x
     * 2 voices) at up to 120 Hz, which is exactly the churn hoisting them was supposed to remove.
     */
    @Test
    fun `the hoisted paths are rewound rather than reset`() {
        val src = stripComments(rendererSource())
        assertTrue("the draw path stopped rewinding its hoisted Paths", src.contains(".rewind()"))
        assertFalse("`.reset()` frees the native path storage - rewind() keeps it", src.contains(".reset()"))
    }

    @Test
    fun `a loud voice always asks for frames`() {
        val e = AuroraEngine()
        val v = listOf(loud(AuroraSpeaker.HUMAN), loud(AuroraSpeaker.AI))
        drive(1000, 2.0, v, e)
        assertTrue(e.needsFrames(v))
    }

    // =========================================================================
    // helpers
    // =========================================================================

    /** The voice screen at Fold density- the tallest surface, where the shape is most visible. */
    private val RIDGE_H = 156f

    /** One full turn of the drift's own sine. Mirrors Aurora's private DRIFT_RATE of 0.35. */
    private val DRIFT_PERIOD_RAD = (2.0 * PI / 0.35).toFloat()

    /**
     * Mountain-to-saddle spacing in progress units, mirroring Aurora's private HALF_STEP. Derived
     * from [Aurora.PEAKS] rather than written out, so it follows a change to the peak count instead
     * of quietly measuring the wrong offset.
     */
    private val HALF_STEP = 0.5f / Aurora.PEAKS

    /** Far denser than the renderer's own ~242 samples, so no mountain can hide between two. */
    private val RIDGE_SAMPLES = 600

    /**
     * The audio both STANDING tests hold constant while the clock runs. Lopsided on purpose- four
     * equal bands make a ridge whose mountains are interchangeable, so a lattice that had slid by
     * exactly one spacing would look untouched.
     */
    private val HELD_BANDS = floatArrayOf(0.6f, 0.25f, 0.45f, 0.35f)

    /**
     * When the STANDING tests look again: half a second of the frame clock (the travelling wave
     * this replaces slid the whole ribbon past itself at least once in that time), then three
     * points around a full turn of the drift, which between them cover the fast part of the
     * vertical wander and the slow part.
     */
    private val STANDING_PHASES = floatArrayOf(
        Aurora.PHASE_RATE_PER_SEC * 0.5f,
        DRIFT_PERIOD_RAD / 4f,
        DRIFT_PERIOD_RAD / 2f,
        DRIFT_PERIOD_RAD * 3f / 4f,
    )

    private fun ridgeInset() = Aurora.insetPx(RIDGE_H, Aurora.crestStrokePx(RIDGE_H, 3f))

    /** Crest y at one point across the width, for one sheet of one speaker. */
    private fun crestAt(
        levels: FloatArray,
        layer: Int,
        speaker: AuroraSpeaker,
        progress: Float,
        phase: Float = 0f,
    ): Float = Aurora.crestY(
        progress,
        RIDGE_H,
        ridgeInset(),
        Aurora.amplitude(RIDGE_H, layer),
        levels,
        layer,
        Aurora.peakShift(speaker, layer),
        Aurora.drift(RIDGE_H, phase, layer, Aurora.driftBias(speaker)),
    )

    /** The whole crest across the width, exactly as the renderer walks it, only finer. */
    private fun crestSweep(
        levels: FloatArray,
        layer: Int,
        speaker: AuroraSpeaker,
        phase: Float,
        from: Float = 0f,
        to: Float = 1f,
    ): FloatArray = FloatArray(RIDGE_SAMPLES + 1) { i ->
        crestAt(levels, layer, speaker, from + (to - from) * i / RIDGE_SAMPLES, phase)
    }

    /** Bottom-edge y at one point across the width — [crestAt]'s sibling, same arguments. */
    private fun bottomAt(
        levels: FloatArray,
        layer: Int,
        speaker: AuroraSpeaker,
        progress: Float,
        phase: Float = 0f,
    ): Float = Aurora.bottomY(
        progress,
        RIDGE_H,
        ridgeInset(),
        Aurora.amplitude(RIDGE_H, layer),
        levels,
        layer,
        Aurora.peakShift(speaker, layer),
        Aurora.drift(RIDGE_H, phase, layer, Aurora.driftBias(speaker)),
    )

    /** The whole bottom edge across the width — [crestSweep]'s sibling. */
    private fun bottomSweep(
        levels: FloatArray,
        layer: Int,
        speaker: AuroraSpeaker,
        phase: Float,
        from: Float = 0f,
        to: Float = 1f,
    ): FloatArray = FloatArray(RIDGE_SAMPLES + 1) { i ->
        bottomAt(levels, layer, speaker, from + (to - from) * i / RIDGE_SAMPLES, phase)
    }

    /**
     * Sample indices of the bottom edge's mountains. NOT negated- the bottom ridge is mirrored
     * downward, so its mountains are MAXIMA of screen y where the crest's are minima.
     */
    private fun bottomPeaks(
        levels: FloatArray,
        layer: Int,
        speaker: AuroraSpeaker,
        phase: Float,
        prominence: Float = 0.02f * RIDGE_H,
    ): List<Int> =
        summits(bottomSweep(levels, layer, speaker, phase, BOTTOM_TRIM, 1f - BOTTOM_TRIM).asList(), prominence)

    /**
     * Where the ribbon is open enough for a mountain to be one. Inside this the taper has scaled
     * the excursion down to a fraction of itself, so a mountain out here is a bump a viewer would
     * not call a summit — the taper pinching the ribbon shut is a fine thing to draw and a nonsense
     * thing to count.
     *
     * It used to be about the CLAMP rather than the taper- before C8 the envelope closed on the box
     * centre and the ends rode the rail, which made the tally wobble with the drift. It does not
     * any more (the taper is a multiplier and the ends are a pure vertical translate at every
     * phase), so these two trims are now only about what counts as a mountain.
     */
    private val RIDGE_TRIM = 0.06f

    /**
     * The bottom edge needs a WIDER trim than the crest- its excursion is a third of the crest's
     * (BOTTOM_AMP_SCALE), so the taper takes it under the prominence floor sooner. Trimming to just
     * inside [Aurora.TAPER_FRACTION] (0.12) keeps every counted sample in the full-height middle,
     * where the shape is a pure translate of itself at any phase.
     */
    private val BOTTOM_TRIM = 0.14f

    /**
     * Sample indices of the crest's local maxima — the MOUNTAINS a viewer would count.
     *
     * Screen y grows downward, so a crest mountain is a MINIMUM of y; the sweep is negated and read
     * as a ridge line before being handed to [summits].
     */
    private fun crestPeaks(
        levels: FloatArray,
        layer: Int,
        speaker: AuroraSpeaker,
        phase: Float,
        prominence: Float = 0.02f * RIDGE_H,
    ): List<Int> =
        summits(crestSweep(levels, layer, speaker, phase, RIDGE_TRIM, 1f - RIDGE_TRIM).map { -it }, prominence)

    /**
     * Sample indices of the local maxima of [rise], which is whichever edge's sweep oriented so
     * that a mountain is a maximum.
     *
     * A hysteresis walk rather than a bare three-sample comparison, because float noise on a
     * nearly-flat stretch would otherwise report dozens of "mountains" and make the count
     * assertions meaningless. [prominence] is what a mountain has to rise above the saddle beside
     * it to count as one: 2% of the container height, i.e. about 3 px on the voice screen, which is
     * well under a real mountain (20-40 px) and well over the noise.
     */
    private fun summits(rise: List<Float>, prominence: Float): List<Int> {
        val peaks = ArrayList<Int>()
        var maxV = -Float.MAX_VALUE
        var minV = Float.MAX_VALUE
        var maxAt = 0
        var climbing = true
        for (i in rise.indices) {
            val v = rise[i]
            if (v > maxV) { maxV = v; maxAt = i }
            if (v < minV) minV = v
            if (climbing) {
                if (v < maxV - prominence) { peaks.add(maxAt); minV = v; climbing = false }
            } else if (v > minV + prominence) {
                maxV = v; maxAt = i; climbing = true
            }
        }
        return peaks
    }

    private fun stripComments(src: String): String = src
        .replace(Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL), "")
        .replace(Regex("//.*"), "")

    /** Walk up from the test working directory until [rel] (module-relative) turns up. */
    private fun moduleSource(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }

    private fun rendererSource(): String =
        moduleSource("src/main/java/com/aiblackbox/portal/ui/components/aurora/AuroraWaveform.kt")

    private fun playerBarSource(): String =
        moduleSource("src/main/java/com/aiblackbox/portal/ui/components/AudioPlayerBar.kt")
}
