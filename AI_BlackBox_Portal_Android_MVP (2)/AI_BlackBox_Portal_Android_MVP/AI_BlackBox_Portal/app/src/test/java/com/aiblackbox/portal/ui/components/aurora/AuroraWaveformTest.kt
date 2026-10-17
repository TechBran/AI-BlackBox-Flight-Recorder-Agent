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
 *   C1 one amplitude      -> a LIST of voices, both drawn at once and kept distinguishable, by
 *                            travelling in OPPOSITE directions (human left-to-right, AI the other)
 *   C2 6-stop gradient    -> one solid colour per BAND, shared by both speakers
 *   C3 per-FRAME motion   -> per-SECOND motion (the original ran at double speed on a 120 Hz Fold)
 *   C4 fixed 160x56 pill  -> geometry derived from whatever size the caller gives
 *   C5 -                  -> nothing opaque, ever (the particle field renders behind this)
 *   C6 -                  -> no idle frame loop
 *   C7 one global level, a fast travelling sine on a half-hump, bounded by a pill
 *                         -> ONE BAND PER SHEET, bipolar about one shared centre line, 4-5 humps,
 *                            travelling at a visible 0.25-0.37 widths/sec
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
     * 60 fps it was tuned on, or a port is a re-tune wearing a port's clothes. k = 1 - exp(-dt/tau).
     *
     * THE PER-BAND RELEASES ARE THE ONE DELIBERATE EXCEPTION, edited here on purpose (2026-08-11,
     * then again 2026-08-12) with the operator's approval to change aurora's default look. They
     * were [0.055, 0.085, 0.115, 0.16] per frame — the original's, reproduced faithfully — and they
     * were too slow to draw speech at all: a first-order release needs 2.3 time constants to lose
     * 90% of its height, so the front sheet took three syllable periods to fall and drew the
     * ENVELOPE of speech rather than speech. The first pass scaled the ladder by 0.322; the second
     * halved that again to 0.161 after the operator ran v35 on a Fold and said the movement "isn't
     * fluent and that much for how much audio is coming in" — a release ladder does not merely
     * delay motion, it deletes range, measured at 11.8% of the whole drawn range (see
     * [Aurora.BAND_RELEASE_TAUS_SEC] for the sweep that picked the scale).
     *
     * The four numbers below are still in the ORIGINAL'S PROPORTIONS — only the scale has moved,
     * both times, which is what keeps this a calibration rather than a redesign. Everything else in
     * this test is untouched, and that is the attribution: the port is still faithful, one
     * calibration inside it was deliberately replaced.
     */
    @Test
    fun `the per-second time constants reproduce the original per-frame blends at 60 fps`() {
        val dt = 1f / 60f
        assertEquals("attack", 0.65f, Aurora.blendAt(dt, Aurora.ATTACK_TAU_SEC), 0.002f)
        assertEquals("global release", 0.10f, Aurora.blendAt(dt, Aurora.RELEASE_TAU_SEC), 0.002f)
        // RE-TUNED TWICE: [0.055, 0.085, 0.115, 0.16] -> [0.161, 0.239, 0.315, 0.416] -> these.
        val expected = floatArrayOf(0.296f, 0.421f, 0.531f, 0.659f)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("band $j release", expected[j], Aurora.blendAt(dt, Aurora.BAND_RELEASE_TAUS_SEC[j]), 0.002f)
        }
        // The original advanced 0.13 rad per frame. That is the raw frame clock and it is still
        // tuned to this rate; what C7 slowed down is the per-layer multiplier the travelling wave
        // reads it through, never this.
        assertEquals("phase per frame", 0.13f, Aurora.PHASE_RATE_PER_SEC * dt, 0.002f)
    }

    /**
     * THE 120 Hz BUG. The original applied its blend once per frame and added 0.13 rad per frame,
     * so a Fold running at 120 Hz released twice as fast and flowed twice as fast as the tuned
     * look. Same elapsed time must mean the same picture at any refresh rate.
     */
    @Test
    fun `the same elapsed time gives the same levels at 60 Hz and at 120 Hz`() {
        // The release window is 80 ms, not the 500 ms it was: since the per-band releases were
        // re-scaled to fall inside one syllable, half a second of silence bottoms every sheet out
        // and the comparison below would agree at any frame rate for the wrong reason — which is
        // precisely what the vacuity guard at the end catches. 80 ms is still ~2.6 time constants
        // on the SLOWEST sheet, so all four are mid-release and plainly spread.
        fun run(hz: Int): FloatArray {
            val e = AuroraEngine()
            drive(hz, 0.5, listOf(loud(AuroraSpeaker.HUMAN)), e)    // saturate
            drive(hz, 0.08, listOf(silent(AuroraSpeaker.HUMAN)), e) // then release
            return e.envelope(AuroraSpeaker.HUMAN).levels.copyOf()
        }
        val at60 = run(60)
        val at120 = run(120)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("sheet $j", at60[j], at120[j], 1e-3f)
        }
        // Guard the guard: the sheets must still be spread out, otherwise they have all bottomed
        // out and would agree at any frame rate for the wrong reason.
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
        // Roughly one time constant of silence on sheet 0, and the expectation is scored against
        // the time the engine was ACTUALLY stepped for, not against the tau. [drive] rounds
        // seconds*hz to whole frames, so a tau that is not a whole number of milliseconds (0.0475 s
        // is 47.5 frames at 1 kHz) is silently truncated, and comparing that run against exp(-1)
        // fails by the rounding rather than by anything about the release. Deriving the elapsed
        // time here keeps this test exact for ANY future ladder.
        val hz = 1000
        val tau = Aurora.BAND_RELEASE_TAUS_SEC[0]
        val frames = Math.round(tau.toDouble() * hz).toInt()
        val elapsed = frames.toFloat() / hz
        drive(hz, frames.toDouble() / hz, listOf(silent(AuroraSpeaker.HUMAN)), e)
        val b = Aurora.BASELINE
        assertEquals(b + (start - b) * exp(-elapsed / tau), e.envelope(AuroraSpeaker.HUMAN).levels[0], 2e-3f)
    }

    /**
     * Phase accumulates forever, so it has to wrap or float precision rots the animation over a
     * long call. It may only wrap where EVERY term that consumes it is back in phase — a wrap that
     * is not a whole number of turns for one consumer jumps that consumer sideways once a minute.
     *
     * The consumers are the drift rate (0.35) and the four per-layer wave multipliers
     * (0.04..0.07). 2pi/0.01 is the period because 0.01 divides all five; C7 had to widen it from
     * the 2pi/0.05 the standing design could get away with, which 0.04, 0.06 and 0.07 do NOT
     * divide. Both TRAVEL DIRECTIONS are checked, and the phase is swept NEGATIVE as well as
     * positive- the AI's ribbons read the clock through a minus sign, and a wrap has to be a whole
     * number of turns backwards too.
     */
    @Test
    fun `wrapping the phase is invisible to every term that consumes it`() {
        val h = 156f
        for (layer in 0 until Aurora.SHEETS) {
            for (speaker in AuroraSpeaker.entries) {
                val driftBias = Aurora.driftBias(speaker)
                val travel = Aurora.travelDirection(speaker)
                val bias = Aurora.phaseBias(speaker)
                for (p in -20..20) {
                    val phase = p * 5f
                    assertEquals(
                        "sheet $layer drift at phase $phase",
                        Aurora.drift(h, phase, layer, driftBias),
                        Aurora.drift(h, phase + Aurora.PHASE_WRAP_RAD, layer, driftBias),
                        1e-3f,
                    )
                    for (i in 0..8) {
                        val progress = i / 8f
                        assertEquals(
                            "sheet $layer $speaker wave at phase $phase, progress $progress",
                            Aurora.wave(progress, phase, layer, travel, bias),
                            Aurora.wave(progress, phase + Aurora.PHASE_WRAP_RAD, layer, travel, bias),
                            1e-3f,
                        )
                    }
                }
            }
        }
    }

    /**
     * The wrap invisibility above rests on an ARITHMETIC invariant: [Aurora.PHASE_WRAP_RAD] is a
     * whole number of turns (2pi) for every rate that multiplies the phase. Asserted directly, so a
     * future retune of a multiplier onto a grid the wrap does not divide fails HERE with a clear
     * message rather than as a subtle sideways jump in the sweep test.
     *
     * The consumers are the four [Aurora.phaseMultiplier] and the private DRIFT_RATE of 0.35
     * (mirrored here as the drift test mirrors it). The grid is 0.005, so the wrap is 2pi/0.005 and
     * each rate's turn count is rate/0.005- 161, 201, 245, 292 for the multipliers and 70 for the
     * drift, all whole.
     */
    @Test
    fun `the phase wrap is a whole number of turns for every rate that consumes it`() {
        val twoPi = 2f * PI.toFloat()
        val driftRate = 0.35f   // mirrors Aurora's private DRIFT_RATE
        val rates = (0 until Aurora.SHEETS).map { Aurora.phaseMultiplier(it) } + driftRate
        for (rate in rates) {
            val turns = Aurora.PHASE_WRAP_RAD * rate / twoPi
            assertEquals(
                "the phase wrap is not a whole number of turns for rate $rate ($turns turns) - a " +
                    "wrap would jump this consumer sideways",
                Math.round(turns).toFloat(), turns, 1e-2f,
            )
        }
    }

    /**
     * ...and the clock reaches BOTH of them from the draw loop, which is what makes the wrap
     * argument above complete.
     *
     * The wave read is the one that matters and the one a source scan can see: the bottom edge
     * takes its vertical signal through the `drift` PARAMETER, so a reader who deleted the phase
     * from the geometry call would still see a moving ribbon and would only have made it stale.
     * The intermediate design forbade exactly this read; C7 requires it.
     */
    @Test
    fun `the frame clock reaches both the wave and the vertical drift`() {
        val src = stripComments(rendererSource())
        assertTrue(
            "the draw loop stopped feeding the phase to the vertical drift",
            Regex("""Aurora\.drift\(\s*h\s*,\s*nowPhase""").containsMatchIn(src),
        )
        assertTrue(
            "the draw loop stopped feeding the phase to the sheet geometry - the wave is stale " +
                "again, which is what C7 exists to fix",
            Regex("""phase\s*=\s*nowPhase""").containsMatchIn(src),
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
                    // 240 samples of the wrap period, which is 80 s of wall clock- the drift and
                    // the four wave multipliers scissor against each other over the whole of it,
                    // so the worst combination of a peak and an up-drift only turns up if the
                    // sweep is fine enough to land near it.
                    for (frame in 0..240) {
                        val phase = frame * (Aurora.PHASE_WRAP_RAD / 240f)
                        for (v in voices) {
                            val levels = engine.envelope(v.speaker).levels
                            for (layer in 0 until Aurora.SHEETS) {
                                val amp = Aurora.amplitude(h, levels[layer], layer)
                                val drift = Aurora.drift(h, phase, layer, Aurora.driftBias(v.speaker))
                                val travel = Aurora.travelDirection(v.speaker)
                                val bias = Aurora.phaseBias(v.speaker)
                                forEachSheetPoint(w, h, steps, inset, amp, phase, layer, travel, bias, drift,
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

        // The taper MULTIPLIES both excursions rather than clamping them, so both edges of the
        // sheet close onto the CENTRE LINE at both ends and the ribbon terminates in a point. An
        // earlier geometry squeezed the ends against a closing envelope instead, which crushed
        // them flat against a rail and left the ribbon ending in a wedge.
        for (layer in 0 until Aurora.SHEETS) {
            for (speaker in AuroraSpeaker.entries) {
                for (phase in floatArrayOf(0f, DRIFT_PERIOD_RAD / 3f, DRIFT_PERIOD_RAD * 2f / 3f)) {
                    val drift = Aurora.drift(h, phase, layer, Aurora.driftBias(speaker))
                    val centre = Aurora.centreY(h, inset, drift)
                    val travel = Aurora.travelDirection(speaker)
                    val bias = Aurora.phaseBias(speaker)
                    val amp = Aurora.amplitude(h, 1f, layer)
                    for (edge in floatArrayOf(0f, 1f)) {
                        assertEquals(
                            "sheet $layer $speaker crest does not close on the centre line at $edge",
                            centre,
                            Aurora.crestY(edge, h, inset, amp, phase, layer, travel, bias, drift),
                            1e-3f,
                        )
                        assertEquals(
                            "sheet $layer $speaker belly does not close on the centre line at $edge",
                            centre,
                            Aurora.bottomY(edge, h, inset, amp, phase, layer, travel, bias, drift),
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
                // CROSS- coerceIn throws on an inverted range, and the centre line is coerced
                // between them on every sample.
                assertTrue("rails inverted at h=$h", Aurora.railTop(h, inset) <= Aurora.railBottom(h, inset))
                // An absurd amplitude and an absurd drift, in both travel directions.
                for (travel in floatArrayOf(1f, -1f)) {
                    val y = Aurora.crestY(progress, h, inset, 999f, 500f, 0, travel, 0.4f, 500f)
                    val yb = Aurora.bottomY(progress, h, inset, 999f, 500f, 0, travel, 0.4f, 500f)
                    assertTrue("crest NaN", !y.isNaN())
                    assertTrue("bottom NaN", !yb.isNaN())
                    assertTrue("crest escaped h=$h", y >= -0.01f && y <= h + 0.01f)
                    assertTrue("bottom escaped h=$h", yb >= -0.01f && yb <= h + 0.01f)
                }
            }
        }
    }

    /**
     * The per-layer ladders are the look. There are three of them now (C7): the sheets are held
     * apart by how DEEP they are, by how many humps they carry and by how fast those humps travel.
     * Four sheets sharing all three would stack into one thick line whatever the audio did.
     */
    @Test
    fun `the per-layer amplitude, frequency and travel ladders hold the sheets apart`() {
        val h = 100f
        for (layer in 0 until Aurora.SHEETS) {
            assertEquals(
                "amplitude $layer",
                h * 0.35f * (1f - layer * 0.10f),
                Aurora.amplitude(h, 1f, layer),
                1e-4f,
            )
            // LINEAR IN THE DRIVE, which is the whole of C7's "its amplitude is how high it goes".
            assertEquals("half drive $layer", Aurora.amplitude(h, 1f, layer) / 2f, Aurora.amplitude(h, 0.5f, layer), 1e-4f)
            assertEquals("no drive $layer", 0f, Aurora.amplitude(h, 0f, layer), 1e-6f)
        }
        // Higher sheets are shallower — that ordering is what gives the stack depth.
        for (layer in 1 until Aurora.SHEETS) {
            assertTrue(Aurora.amplitude(h, 1f, layer) < Aurora.amplitude(h, 1f, layer - 1))
        }

        // ...and the amplitude ladder is LIVE- it IS the drawn height, not a knob something else
        // clamps. Measured on the geometry helpers' own container, whose height the crest helper
        // fixes at RIDGE_H.
        for (layer in 0 until Aurora.SHEETS) {
            val drift = Aurora.drift(RIDGE_H, 0f, layer, Aurora.driftBias(AuroraSpeaker.HUMAN))
            val centre = Aurora.centreY(RIDGE_H, ridgeInset(), drift)
            val amp = Aurora.amplitude(RIDGE_H, 1f, layer)
            var tallest = 0f
            for (i in 0..RIDGE_SAMPLES) {
                val p = i.toFloat() / RIDGE_SAMPLES
                tallest = maxOf(tallest, centre - crestAt(1f, layer, AuroraSpeaker.HUMAN, p))
            }
            assertEquals(
                "sheet $layer's peak is not its full amplitude - something is clamping it again",
                amp, tallest, 0.01f * RIDGE_H,
            )
        }

        // FREQUENCY- 4 to 5 humps per sheet (`sin(progress * PI * freq)` runs freq/2 cycles across
        // the width), and never the same count on two sheets, or they would ripple in lockstep.
        // Brandon asked for MORE humps than the first pass's 2-3 on 2026-07-27.
        val humps = (0 until Aurora.SHEETS).map { Aurora.frequency(it) / 2f }
        for (layer in 0 until Aurora.SHEETS) {
            assertTrue(
                "sheet $layer draws ${humps[layer]} humps - Brandon asked for a smooth wave of 4 to 5",
                humps[layer] in 4f..5f,
            )
        }
        assertEquals("two sheets carry the same hump count", Aurora.SHEETS, humps.toSet().size)

        // TRAVEL- what a viewer reads is the CREST SPEED across the width, not the raw multiplier:
        // `mult * PHASE_RATE_PER_SEC / (PI * frequency)` widths/sec. Every sheet moves at its own
        // speed and all four land in the research "alive but not scrolling" band 0.25..0.37. The
        // geometric version of the same bound is the travel test below.
        val speeds = (0 until Aurora.SHEETS).map {
            Aurora.PHASE_RATE_PER_SEC * Aurora.phaseMultiplier(it) / (PI.toFloat() * Aurora.frequency(it))
        }
        for (layer in 0 until Aurora.SHEETS) {
            assertTrue(
                "sheet $layer crest travels at ${speeds[layer]} widths/sec (a crossing every " +
                    "${1f / speeds[layer]} s)",
                speeds[layer] > 0.2f && speeds[layer] < 0.4f,
            )
        }
        assertEquals("two sheets travel at the same speed", Aurora.SHEETS, speeds.toSet().size)
    }

    /**
     * NO SHEET MAY RAIL. The regression test for a defect this geometry has now been rewritten
     * twice to remove: a crest whose peak is cut off flat against the top of the box, on the sheet
     * the eye goes to, at exactly the loud moments the user is looking at.
     *
     * It is a live risk again because C7's wave is bipolar and the drift is not- a sheet reaches
     * `AMP_FRACTION + its worst drift` from the centre and that sum has to fit inside the rails.
     * [Aurora.amplitude] is budgeted for it with 0.013 of the height in hand, so the honest
     * assertion is EXACTLY zero clamped samples at every drive up to and including a two-voice
     * shout — not the 1% float slack the previous, tighter geometry needed.
     *
     * Swept over the whole wrap period rather than one drift turn, because the wave's travel and
     * the drift beat against each other and the worst alignment is somewhere inside that beat.
     */
    @Test
    fun `no sheet rails against the box at any drive`() {
        val h = RIDGE_H
        val inset = ridgeInset()
        val top = Aurora.railTop(h, inset)
        val bottom = Aurora.railBottom(h, inset)

        // Fraction of the drawn width of BOTH edges sitting exactly on a rail.
        fun clamped(drive: Float, layer: Int): Float {
            var onRail = 0
            var total = 0
            for (speaker in AuroraSpeaker.entries) {
                for (frame in 0 until 120) {
                    val phase = frame * Aurora.PHASE_WRAP_RAD / 120f
                    // The WHOLE width, edges included.
                    for (i in 0..RIDGE_SAMPLES) {
                        val p = i.toFloat() / RIDGE_SAMPLES
                        for (y in floatArrayOf(
                            crestAt(drive, layer, speaker, p, phase),
                            bottomAt(drive, layer, speaker, p, phase),
                        )) {
                            total++
                            if (abs(y - top) < 1e-3f || abs(y - bottom) < 1e-3f) onRail++
                        }
                    }
                }
            }
            return onRail.toFloat() / total
        }

        // Rest, the analyser's measured steady-speech band (~0.58) through the envelope's sqrt
        // perceptual lift, and a full two-voice shout.
        for (drive in floatArrayOf(Aurora.BASELINE, sqrt(0.58f), 1f)) {
            for (layer in 0 until Aurora.SHEETS) {
                assertEquals(
                    "sheet $layer clips at drive $drive - the ribbon is railing again",
                    0f, clamped(drive, layer), 1e-6f,
                )
            }
        }

        // GUARD THE GUARD- a ribbon that had simply gone flat would pass everything above. The
        // front sheet's peak has to be a real fraction of the box at a real drive.
        var tallest = 0f
        for (frame in 0 until 120) {
            val phase = frame * Aurora.PHASE_WRAP_RAD / 120f
            val centre = Aurora.centreY(h, inset, Aurora.drift(h, phase, 0, Aurora.driftBias(AuroraSpeaker.HUMAN)))
            for (i in 0..RIDGE_SAMPLES) {
                tallest = maxOf(tallest, centre - crestAt(sqrt(0.58f), 0, AuroraSpeaker.HUMAN, i.toFloat() / RIDGE_SAMPLES, phase))
            }
        }
        assertTrue("the ribbon went flat (${tallest / h} of the box at speech)", tallest / h > 0.20f)
    }

    /**
     * BIPOLAR ABOUT ONE SHARED CENTRE LINE (C7). "We want our ribbon to go vertical in the upwards
     * direction and the bottom as well" — so at a given drive the ink reaches COMPARABLY far above
     * its centre line and below it, and it is the same centre line for all four sheets and both
     * speakers.
     *
     * This replaces the old ground-line composition test, and it subsumes it: a wave symmetric
     * about the box centre cannot leave a quarter of the box dead at one end without failing here,
     * and a clamped (railed) crest shows up as an asymmetry too.
     *
     * The tolerance is 25% of the larger excursion. The two are NOT identical by construction- the
     * crest reaches `amp` up while the belly reaches `BOTTOM_AMP_SCALE * amp + BOTTOM_OFFSET *
     * height` down, which measures 14.3% apart at worst (the front sheet at full drive, the gap
     * grown from 10.4% when AMP_FRACTION rose to 0.35) and 2% at best. 25% covers that spread and
     * nothing else — a one-sided geometry, a ground line anywhere but the centre, or a clamped edge
     * all blow straight through it.
     *
     * ASSERTED AT SPEAKING DRIVES ONLY, and the resting state is asserted separately below. At rest
     * the wave is a twentieth of its full excursion and `BOTTOM_OFFSET_FRACTION` — the constant
     * that keeps a silent ribbon a visible sliver rather than a zero-area path — is four times all
     * of it. The resting sliver therefore hangs BELOW the centre line by construction and always
     * did; what matters about it is that it is thin and close to the line, which is what is checked.
     */
    @Test
    fun `the wave reaches as far above its centre line as below it`() {
        for (h in floatArrayOf(40f, 52f * 2.625f, 140f * 3f)) {
            val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 1f))
            for (drive in floatArrayOf(sqrt(0.58f), 1f)) {
                for (layer in 0 until Aurora.SHEETS) {
                    for (speaker in AuroraSpeaker.entries) {
                        var above = 0f
                        var below = 0f
                        for (frame in 0 until 24) {
                            val phase = frame * Aurora.PHASE_WRAP_RAD / 24f
                            val drift = Aurora.drift(h, phase, layer, Aurora.driftBias(speaker))
                            val centre = Aurora.centreY(h, inset, drift)
                            val amp = Aurora.amplitude(h, drive, layer)
                            val travel = Aurora.travelDirection(speaker)
                            val bias = Aurora.phaseBias(speaker)
                            for (i in 0..RIDGE_SAMPLES) {
                                val p = i.toFloat() / RIDGE_SAMPLES
                                above = maxOf(above, centre - Aurora.crestY(p, h, inset, amp, phase, layer, travel, bias, drift))
                                below = maxOf(below, Aurora.bottomY(p, h, inset, amp, phase, layer, travel, bias, drift) - centre)
                            }
                        }
                        assertTrue(
                            "sheet $layer $speaker at drive $drive on a ${h}px box reaches " +
                                "${above / h} of the box up and ${below / h} down - the ribbon is " +
                                "no longer bipolar about its centre line",
                            abs(above - below) < 0.25f * maxOf(above, below),
                        )
                    }
                }
            }
        }

        // THE RESTING SLIVER- thin, and hugging the line rather than hanging off it. The whole of
        // it (crest above, belly below) fits in a tenth of the box, and nothing about it is a
        // resting ribbon "parked low in its box", which is what a ground line would produce.
        for (h in floatArrayOf(40f, 52f * 2.625f, 140f * 3f)) {
            val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 1f))
            for (layer in 0 until Aurora.SHEETS) {
                for (speaker in AuroraSpeaker.entries) {
                    val amp = Aurora.amplitude(h, Aurora.BASELINE, layer)
                    val travel = Aurora.travelDirection(speaker)
                    val bias = Aurora.phaseBias(speaker)
                    val drift = Aurora.drift(h, 0f, layer, Aurora.driftBias(speaker))
                    val centre = Aurora.centreY(h, inset, drift)
                    var above = 0f
                    var below = 0f
                    for (i in 0..RIDGE_SAMPLES) {
                        val p = i.toFloat() / RIDGE_SAMPLES
                        above = maxOf(above, centre - Aurora.crestY(p, h, inset, amp, 0f, layer, travel, bias, drift))
                        below = maxOf(below, Aurora.bottomY(p, h, inset, amp, 0f, layer, travel, bias, drift) - centre)
                    }
                    assertTrue("the resting sliver is ${(above + below) / h} of the box thick", above + below < 0.10f * h)
                    assertTrue("the resting sliver hangs off its centre line", below < 0.08f * h)
                }
            }
        }

        // ...and it really is ONE line for everybody, not four lanes. The drift ladder straddles
        // the box centre, so the STACK's mean centre is the box centre exactly, and every sheet's
        // ink crosses that centre at a speaking drive whatever the drift is doing to it. Four
        // sheets each given their own band of the box would fail the second half.
        val h = RIDGE_H
        val inset = ridgeInset()
        val n = 240
        var stack = 0.0
        var samples = 0
        for (layer in 0 until Aurora.SHEETS) {
            for (speaker in AuroraSpeaker.entries) {
                for (frame in 0 until n) {
                    val phase = frame * DRIFT_PERIOD_RAD / n
                    stack += Aurora.centreY(h, inset, Aurora.drift(h, phase, layer, Aurora.driftBias(speaker))).toDouble()
                    samples++
                }
                var above = false
                var below = false
                for (frame in 0 until 24) {
                    val phase = frame * Aurora.PHASE_WRAP_RAD / 24f
                    for (i in 0..RIDGE_SAMPLES) {
                        val p = i.toFloat() / RIDGE_SAMPLES
                        if (crestAt(1f, layer, speaker, p, phase) < h / 2f) above = true
                        if (bottomAt(1f, layer, speaker, p, phase) > h / 2f) below = true
                    }
                }
                assertTrue("sheet $layer $speaker never reaches above the box centre", above)
                assertTrue("sheet $layer $speaker never reaches below the box centre", below)
            }
        }
        assertEquals(
            "the stack of sheets is no longer centred on the box",
            (h / 2f).toDouble(), stack / samples, 0.01 * h,
        )
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
            // ribbons that never cross, which is the thing the drift exists to prevent. The floor
            // is 0.01*h rather than 0.02: DRIFT_FRACTION was halved (0.06 -> 0.03) for amplitude
            // headroom, so the inner layers' swing is now |layer-1.5| * DRIFT_FRACTION = 0.015 of
            // the height. Still plainly moving, just over a narrower band.
            for (layer in 0 until Aurora.SHEETS) {
                val spread = d[layer].max() - d[layer].min()
                assertTrue("layer $layer drift is frozen (spread $spread)", spread > 0.01f * h)
            }
        }

        // The AI's bias RE-TIMES the wander without relocating it- identical mean over a full turn,
        // so neither ribbon sits off centre, but a visibly different position at any given instant.
        for (layer in 0 until Aurora.SHEETS) {
            val human = sweep(layer, Aurora.driftBias(AuroraSpeaker.HUMAN))
            val ai = sweep(layer, Aurora.driftBias(AuroraSpeaker.AI))
            assertEquals("layer $layer mean drift moved", human.average(), ai.average(), 1e-3)
            val apart = (0 until n).maxOf { abs(human[it] - ai[it]) }
            // 0.008*h, not 0.02: the halved DRIFT_FRACTION shrinks the human/ai gap on the inner
            // layers to ~0.011 of the height (the outer layers stay ~0.034). Still visibly apart.
            assertTrue("layer $layer drift bias changed nothing", apart > 0.008f * h)
        }
    }

    // =========================================================================
    // C7 — one band per sheet, bipolar, 4-5 humps, travelling at a visible speed
    // =========================================================================

    /**
     * THE ONE BRANDON ASKED FOR, PART ONE- "each ribbon should represent a different band of the
     * audio spectrum and its amplitude is how high it goes".
     *
     * Sheet j is band j and NOTHING else. Asserted at both ends of the pipe: the envelope lifts
     * level j alone, and the geometry turns level j into sheet j's height alone. An intermediate
     * design built every sheet's crest out of all four bands at once, so "band 2 fired" was a
     * change everywhere on every sheet; this is the property that replaced it.
     */
    @Test
    fun `each sheet rises and falls with its own band and no other`() {
        val quiet = 0.04f
        val loudBand = 0.81f    // the curve + rest floor lift these to 0.18 and 0.88 — well apart
        // Scored through the shipped functions rather than a literal curve, so a re-tune of
        // [Aurora.DISPLAY_GAMMA] or of the rest-floor rule moves the expectation with the code and
        // this test keeps testing the WIRING (sheet j reads band j and nothing else) it is for.
        val drawnLoud = Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(loudBand))
        val drawnQuiet = Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(quiet))
        assertTrue("the two probe levels are no longer clear of each other", drawnLoud > drawnQuiet * 3f)
        for (band in 0 until Aurora.SHEETS) {
            val bands = FloatArray(Aurora.SHEETS) { if (it == band) loudBand else quiet }
            val e = AuroraEngine()
            drive(1000, 0.3, listOf(AuroraVoice(AuroraSpeaker.HUMAN, bands)), e)
            val levels = e.envelope(AuroraSpeaker.HUMAN).levels

            assertEquals("band $band did not lift its own sheet", drawnLoud, levels[band], 1e-3f)
            for (other in 0 until Aurora.SHEETS) {
                if (other == band) continue
                assertEquals("band $band lifted sheet $other", drawnQuiet, levels[other], 1e-3f)
            }

            // ...and on screen: sheet `band`'s wave is four and a half times the excursion of the
            // others', at the same layer depth it would otherwise share with them.
            for (layer in 0 until Aurora.SHEETS) {
                val amp = Aurora.amplitude(RIDGE_H, levels[layer], layer)
                val depth = 1f - layer * 0.10f
                assertEquals(
                    "sheet $layer is not drawing its own band's level",
                    RIDGE_H * 0.35f * levels[layer] * depth, amp, 1e-3f,
                )
                // Stated as a RATIO rather than as two absolute thresholds: the rest floor is a
                // blend now, so a quiet sheet sits a little higher than it used to in absolute
                // terms while the SEPARATION — the thing this test is about — is unchanged.
                if (layer == band) {
                    assertTrue("the loud band's sheet is not the tall one", amp / depth > 0.2f * RIDGE_H)
                } else {
                    assertTrue(
                        "a quiet band's sheet stood up anyway",
                        amp / depth < 0.35f * (RIDGE_H * 0.35f * drawnLoud),
                    )
                }
            }
        }

        // The wiring itself, pinned at the draw loop, since everything above would also pass if the
        // renderer handed every sheet the same level.
        val src = stripComments(rendererSource())
        assertTrue(
            "the draw loop stopped driving sheet j from band j",
            Regex("""envelope\.levels\[layer\]""").containsMatchIn(src),
        )
    }

    /**
     * THE ONE BRANDON ASKED FOR, PART TWO- "then we can introduce a small movement, you know, from
     * left to right, like we had, so it's not stale anymore" — and, on 2026-07-27, that the first
     * pass's motion was too slow to see ("there's no motion").
     *
     * Bounded from BOTH sides, because the two failure modes are opposites and each is the other's
     * "fix". STALE is the first pass's 2.4..2.9% of the width per second (34-42 s to cross a bar);
     * SCROLLING is the reference's 1.0 to 2.3 full cycles per second "moving from right to left".
     * The target is the research band 0.25..0.37 widths/sec- a crossing every 3-4 s.
     *
     * Measured as a real picture shift, not read back off the constants- the crest is sampled a
     * short interval apart over the untapered middle and the two sweeps are aligned by minimising
     * their mean squared difference, mean-subtracted first so the vertical drift (which does NOT
     * move the picture sideways) cannot be mistaken for travel. The interval is DELIBERATELY short
     * (0.15 s): at these speeds the wave has many humps and a full-second sample would slide the
     * picture by nearly a whole wave period, which aliases back onto itself and reads as no motion.
     * Over 0.15 s the crest moves well under half a hump, so the alignment is unambiguous; the
     * per-second speed is then that shift divided by the interval.
     */
    @Test
    fun `the wave travels across the width, at a visible speed`() {
        for (speaker in AuroraSpeaker.entries) {
            for (layer in 0 until Aurora.SHEETS) {
                val now = travelSweep(layer, speaker, 0f)
                val later = travelSweep(layer, speaker, TRAVEL_MEASURE_PHASE)

                // NOT STALE- the picture is a different picture, over and above the drift.
                val moved = centred(now).indices.maxOf { abs(centred(now)[it] - centred(later)[it]) }
                assertTrue(
                    "sheet $layer $speaker is stale (${moved / RIDGE_H} of the height of change " +
                        "in ${TRAVEL_MEASURE_SEC}s)",
                    moved > 0.02f * RIDGE_H,
                )

                // ...and it moved by SLIDING, at the research speed. Shift over the interval,
                // divided by the interval, is the widths/sec a viewer reads.
                val shiftFrac = bestAlignmentShift(now, later) * TRAVEL_SPAN / TRAVEL_SAMPLES
                val speed = abs(shiftFrac) / TRAVEL_MEASURE_SEC
                val expected = Aurora.PHASE_RATE_PER_SEC * Aurora.phaseMultiplier(layer) /
                    (PI.toFloat() * Aurora.frequency(layer))
                assertTrue(
                    "sheet $layer $speaker slid $speed widths/sec, outside the alive-not-scrolling band",
                    speed > 0.22f && speed < 0.40f,
                )
                assertEquals(
                    "sheet $layer $speaker measured travel does not match the analytic crest speed",
                    expected, speed, 0.03f,
                )
            }
        }
    }

    /**
     * MORE HUMPS (Brandon, 2026-07-27), and the regression test for "right now it seems pretty flat".
     *
     * The measured cause of flatness was never gain- steady speech already drove the front sheet to
     * 62% of the available half-height. It was that `sin(progress * PI * freq)` with the reference's
     * freq 1.15..2.80 puts 0.6 to 1.4 humps across the whole width, i.e. one broad arc however tall
     * it is drawn. The first pass raised it to 2.1..3.0 humps; Brandon asked for more still. So this
     * counts HUMPS on the drawn crest and fails from both directions- too few is the arc coming
     * back, too many is faceting. Do not "fix" a failure here by raising the amplitude.
     *
     * 4..6 rather than the exact 4.0..4.9 [Aurora.frequency] carries, because a COUNT of drawn humps
     * is not quite the cycle count: the wave travels, so at some phases a partial hump sits at each
     * edge, and where the edge taper is still ramping it turns one of those partials into a
     * countable local maximum of its own. The exact 4.0..4.9 cycles is asserted on the frequency
     * ladder above, where nothing is in the way of it; this is the end-to-end guard that the drawn
     * picture really does undulate that many times.
     */
    @Test
    fun `the crest draws four to five humps across the width`() {
        for (drive in floatArrayOf(sqrt(0.58f), 1f)) {
            for (speaker in AuroraSpeaker.entries) {
                for (layer in 0 until Aurora.SHEETS) {
                    // Several phases: the wave travels, so a hump can be halfway off the edge.
                    for (frame in 0 until 8) {
                        val phase = frame * Aurora.PHASE_WRAP_RAD / 8f
                        val humps = crestPeaks(drive, layer, speaker, phase)
                        assertTrue(
                            "sheet $layer ($speaker) shows ${humps.size} humps at drive $drive, " +
                                "phase $phase",
                            humps.size in 4..6,
                        )
                    }
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
     * properties are invisible- dropping the curve and collapsing all four sheets onto band 0 left
     * the whole suite green. This input sees both.
     *
     * SCORED THROUGH THE SHIPPED FUNCTIONS, not against a literal. The expectation is
     * `Aurora.restBlend(BASELINE, Aurora.displayCurve(band))` — what the envelope is specified to
     * compute — so a re-tune of [Aurora.DISPLAY_GAMMA] moves this test with the code, while the
     * property it is actually here for (sheet j reads band j, and reads it THROUGH the curve) is
     * still checked: the guards below require the lift to be a real lift and the silent sheets to
     * stay down.
     */
    @Test
    fun `sheet j follows band j through the perceptual curve`() {
        val e = AuroraEngine()
        // Bass only, at a quarter energy — well clear of the curve's fixed points at 0 and 1. 0.2 s
        // is ~12 attack time constants, so every sheet has fully arrived at its target.
        val band = 0.25f
        drive(1000, 0.2, listOf(AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(band, 0f, 0f, 0f))), e)
        val levels = e.envelope(AuroraSpeaker.HUMAN).levels

        // THE LIFT- the curve pulls a quarter-energy band up to 0.41 and the rest floor blends it
        // to 0.44, well over the raw 0.25. It is what lets a quiet-but-present band still animate;
        // feeding the band through raw would park this sheet at 0.25.
        val expected = Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(band))
        assertEquals("sheet 0 lost the perceptual lift", expected, levels[0], 1e-3f)
        assertTrue("the lift is not lifting — a curved band must draw well above its raw energy",
            levels[0] > band * 1.5f)

        // THE WIRING- bands 1..3 are silent, so their sheets rest at the baseline. A sheet reading
        // a shared band instead of its own would be sitting up at 0.5 alongside sheet 0.
        for (j in 1 until Aurora.SHEETS) {
            assertEquals("sheet $j is not following band $j", Aurora.BASELINE, levels[j], 1e-3f)
        }
        assertTrue("sheet 0 collapsed onto the silent sheets", levels[0] - levels[1] > 0.3f)
    }

    /**
     * THE LOAD-BEARING REQUIREMENT, and the one Brandon named the constraint for- when he and the
     * model are talking AT THE SAME TIME, fed byte-identical audio, the two sets of ribbons must
     * still be tellable apart.
     *
     * THE MECHANISM IS DIRECTION OF TRAVEL (C1): the human's waves run left to right and the AI's
     * right to left, forever. Colour cannot do this job any more — C2 gave hue to the BAND, so
     * both speakers paint sheet 2 the same green — and a static phase offset could not do it on a
     * stationary wave, which is what the intermediate design left behind.
     *
     * Asserted as the SIGN of the picture's motion, measured the same way the travel test measures
     * speed, because that is the property a viewer actually reads. Equal magnitudes, opposite
     * signs: neither identity is the odd one out, and neither is the fast one.
     */
    @Test
    fun `the two voices travel in opposite directions`() {
        // Same short interval the travel-speed test uses, and for the same reason- a full-second
        // sample aliases the many-hump wave back onto itself and could flip the measured sign.
        for (layer in 0 until Aurora.SHEETS) {
            val human = bestAlignmentShift(
                travelSweep(layer, AuroraSpeaker.HUMAN, 0f),
                travelSweep(layer, AuroraSpeaker.HUMAN, TRAVEL_MEASURE_PHASE),
            )
            val ai = bestAlignmentShift(
                travelSweep(layer, AuroraSpeaker.AI, 0f),
                travelSweep(layer, AuroraSpeaker.AI, TRAVEL_MEASURE_PHASE),
            )
            // Screen x grows rightward and so does the sweep index, so a POSITIVE alignment shift
            // is a picture that moved right.
            assertTrue("sheet $layer- the human's ribbon is not travelling left to right ($human)", human > 0)
            assertTrue("sheet $layer- the AI's ribbon is not travelling right to left ($ai)", ai < 0)
            assertEquals(
                "sheet $layer- one identity travels faster than the other, which reads as one of " +
                    "them being wrong rather than as two identities",
                abs(human).toFloat(), abs(ai).toFloat(), 1f,
            )
        }
        // The constant behind it, pinned so a "tidy-up" cannot collapse the pair to one sign.
        assertEquals(1f, Aurora.travelDirection(AuroraSpeaker.HUMAN), 1e-6f)
        assertEquals(-1f, Aurora.travelDirection(AuroraSpeaker.AI), 1e-6f)
    }

    /**
     * ...and the two are apart at the ONE instant direction cannot separate them: phase zero.
     *
     * Two waves travelling in opposite directions are identical wherever their clocks coincide, and
     * with no bias they would coincide at phase 0 — the first frame of every fresh composition,
     * every resume and every wake from the idle park, i.e. the most-observed instant this ribbon
     * has. [Aurora.phaseBias] is kept for exactly that: pi on the AI makes phase 0 the maximally
     * separated instant instead of the maximally mushed one. Making the pair EQUAL is the mush this
     * exists to prevent.
     *
     * Measured as mean vertical separation of the crest lines, scaled by the drive because the
     * separation is an ANTIPHASE one and therefore proportional to amplitude: two near-flat quiet
     * ribbons cannot be far apart in absolute pixels and do not need to be, there being nothing
     * there to mush together.
     */
    @Test
    fun `the two voices are furthest apart at the phase every wake starts from`() {
        val h = 156f
        val w = 1080f
        val steps = Aurora.sampleCount(w)
        val inset = Aurora.insetPx(h, Aurora.crestStrokePx(h, 3f))
        assertNotEquals(
            "the two phase biases collapsed onto each other",
            Aurora.phaseBias(AuroraSpeaker.HUMAN),
            Aurora.phaseBias(AuroraSpeaker.AI),
        )
        for (drive in floatArrayOf(0.35f, 0.7f, 1f)) {
            var worstLayer = Float.MAX_VALUE
            for (layer in 0 until Aurora.SHEETS) {
                var total = 0f
                var n = 0
                for (i in 0..steps) {
                    val p = i.toFloat() / steps
                    total += abs(
                        crestAt(drive, layer, AuroraSpeaker.HUMAN, p) -
                            crestAt(drive, layer, AuroraSpeaker.AI, p),
                    )
                    n++
                }
                worstLayer = minOf(worstLayer, total / n)
            }
            assertTrue(
                "voices overlap into mush at drive=$drive (mean separation ${worstLayer / h} of height)",
                worstLayer > 0.10f * h * drive,
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
                Aurora.paintsSheet(env.present, d),
            )

            // Parked, so the phase is frozen wherever the frame loop stopped- 0 will do.
            val phase = 0f
            val amp = Aurora.amplitude(h, d, layer)
            val drift = Aurora.drift(h, phase, layer, Aurora.driftBias(AuroraSpeaker.AI))
            val crestYs = FloatArray(steps + 1)
            val bottomYs = FloatArray(steps + 1)
            var ci = 0
            var bi = steps
            forEachSheetPoint(
                w, h, steps, inset, amp, phase, layer,
                Aurora.travelDirection(AuroraSpeaker.AI), Aurora.phaseBias(AuroraSpeaker.AI), drift,
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
     *
     * The rule MOVED (M8) rather than changed: what used to be an inline `when` here is now
     * [com.aiblackbox.portal.ui.components.playerRibbonBands], which hands back the SHARED silent
     * array for every state except PLAYING and is pinned state-by-state in PlayerBarStatusTest.
     * That same `when` also carried the synthetic breathing tween that stood in for an undecoded
     * envelope, and that is deliberately gone — the bar prefetches its envelope and playback waits
     * for it, so there is no longer a gap to animate over.
     */
    @Test
    fun `the player bar keeps its ribbon present while idle`() {
        val src = stripComments(playerBarSource())
        assertTrue(
            "the idle player bar stopped feeding bands - its ribbon retires to an empty box",
            src.contains(Regex("playerRibbonBands\\(status,\\s*outputBands\\)")),
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
     * THE OCCLUSION BUDGET. Measured on device, a single voice's four sheets covered 99.24% of the
     * particle field's centre band (fill+bloom alone 74%), i.e. the field the ribbon is supposed to
     * float over was all but painted out. The fix was two things at once: cut the alphas AND draw
     * the light passes (bloom, crest) additively (BlendMode.Plus), so overlaps BRIGHTEN instead of
     * stacking toward opaque.
     *
     * Three bounds, all recomputed here:
     *   COVERAGE- if the four fills AND the four blooms were all composited normally (SrcOver) at
     *   one shared point, the fraction of background they occlude. This is the conservative
     *   non-additive equivalent (the blooms are actually Plus and occlude nothing), and even so it
     *   must sit at or under 55%. It lands at 51.7%; fills alone (the only pass that truly occludes)
     *   are 38.7%, so the field reads through.
     *   PER-SHEET FILL- the body alpha the budget hangs on, held to <= 0.16.
     *   WHITE-OUT- the additive passes SUM their brightness where all four sheets cross, so
     *   `sum(crestAlpha + bloomAlpha)` must stay plausible. It lands at 1.55, under 1.6.
     */
    @Test
    fun `the fill and bloom passes stay within the occlusion and white-out budgets`() {
        // Non-additive-equivalent coverage of all fills + blooms stacked at a shared point.
        var transparency = 1f
        for (layer in 0 until Aurora.SHEETS) {
            transparency *= (1f - Aurora.fillAlpha(layer))
            transparency *= (1f - Aurora.bloomAlpha(layer))
        }
        val coverage = 1f - transparency
        assertTrue(
            "fill+bloom occlude ${coverage * 100}% of the field - the particle backdrop is hidden again",
            coverage <= 0.55f,
        )

        // The body alpha budget, per sheet.
        for (layer in 0 until Aurora.SHEETS) {
            assertTrue(
                "fill $layer alpha ${Aurora.fillAlpha(layer)} over the 0.16 per-sheet budget",
                Aurora.fillAlpha(layer) <= 0.16f,
            )
        }

        // Additive (BlendMode.Plus) white-out guard: four crossing crests + blooms sum.
        var additive = 0f
        for (layer in 0 until Aurora.SHEETS) {
            additive += Aurora.crestAlpha(layer) + Aurora.bloomAlpha(layer)
        }
        assertTrue(
            "additive crest+bloom sum $additive risks blowing the shared centre to white",
            additive < 1.6f,
        )

        // ...and the light passes really are additive at the draw site, or the story above is a lie.
        val src = stripComments(rendererSource())
        assertEquals(
            "expected exactly two BlendMode.Plus passes (bloom + crest) - additive compositing is " +
                "what lets them brighten instead of occlude",
            2,
            Regex("""blendMode\s*=\s*BlendMode\.Plus""").findAll(src).count(),
        )
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

    /**
     * ONE SOLID COLOUR PER BAND (C2), and the sheet index IS the band index (C7), so a reorder of
     * either would silently repaint the spectrum.
     *
     * Asserted on the CHANNELS rather than on the token names, so swapping two tokens in the theme
     * fails here rather than shipping. All four are fully opaque- the ALPHA is applied per pass.
     */
    @Test
    fun `each band owns one solid identity colour`() {
        val colours = (0 until Aurora.SHEETS).map { auroraBandColor(it) }
        assertEquals("two bands share a colour", Aurora.SHEETS, colours.toSet().size)
        for ((layer, c) in colours.withIndex()) {
            assertEquals("band $layer alpha", 1f, c.alpha, 1e-6f)
        }
        val (red, blue, green, amber) = colours
        assertTrue("band 0 (voicing) is not red", red.red > red.blue && red.red > red.green)
        assertTrue("band 1 (vowel) is not blue", blue.blue > blue.red && blue.blue > blue.green)
        assertTrue("band 2 (formant) is not green", green.green > green.red && green.green > green.blue)
        // Amber is a red-dominant yellow: red >= green, both well clear of blue. Distinguished
        // from band 0's red by having most of a green channel, which is what makes it read amber.
        assertTrue("band 3 (sibilance) is not amber", amber.red >= amber.green && amber.green > 0.5f && amber.blue < 0.2f)
        // ...and it does NOT depend on the speaker, which is the property that freed hue to carry
        // the spectrum in the first place. `auroraBandColor` takes no speaker to give it.
        val src = stripComments(rendererSource())
        assertFalse(
            "the renderer colours by speaker again - hue is the band's, identity is the travel " +
                "direction's (C1/C2)",
            src.contains("auroraSpeakerColor"),
        )
        assertTrue("the draw loop stopped colouring by layer", src.contains("auroraBandColor(layer)"))
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
     * THE PARK'S TWO CONDITIONS HAVE TO AGREE ABOUT WHAT "INAUDIBLE" MEANS, or the loop parks on a
     * picture the envelope has not finished drawing.
     *
     * [AuroraVoice.hasEnergy] answers it with [Aurora.ENERGY_FLOOR]; the envelope answers it by
     * sitting within [Aurora.SETTLE_EPS] of its floor. Those are only the same question if
     * ENERGY_FLOOR is exactly the band at which a sheet stops being distinguishable from its
     * resting sliver — which is why the constant is DERIVED from the curve and the floor rule
     * rather than typed. It was a literal (0.0038) calibrated against the old square root and the
     * old `maxOf` floor; under [Aurora.restBlend] any nonzero band lifts the sheet a little, so
     * that literal would now call a visibly-lifted sheet inaudible and freeze it there.
     *
     * Asserted as the PROPERTY, in both directions, so the derivation can be rewritten but not
     * quietly broken.
     */
    @Test
    fun `the audibility floor is exactly the band that cannot lift a sheet off its rest`() {
        val atFloor = Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(Aurora.ENERGY_FLOOR))
        assertTrue(
            "a band AT the floor must be indistinguishable from rest (drew $atFloor)",
            atFloor - Aurora.BASELINE <= Aurora.SETTLE_EPS + 1e-6f,
        )
        // ...and it must be the LARGEST such band: 2x the floor has to be distinguishable, or the
        // constant is simply too small and the loop never parks.
        val above = Aurora.restBlend(Aurora.BASELINE, Aurora.displayCurve(Aurora.ENERGY_FLOOR * 2f))
        assertTrue(
            "a band at 2x the floor must lift the sheet clear of rest (drew $above)",
            above - Aurora.BASELINE > Aurora.SETTLE_EPS,
        )
        // The two answers agree end to end: a voice at the floor is inaudible AND settles.
        val e = AuroraEngine()
        val atFloorVoice = listOf(AuroraVoice(AuroraSpeaker.HUMAN, FloatArray(Aurora.SHEETS) { Aurora.ENERGY_FLOOR }))
        drive(1000, 2.0, atFloorVoice, e)
        assertFalse("a voice at the audibility floor must let the loop park", e.needsFrames(atFloorVoice))
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
        assertTrue("a present voice must paint whatever its drive", Aurora.paintsSheet(env.present, env.levels[0]))
        // ...while changing nothing a value mirror could notice, which is the whole trap.
        assertEquals("a zero-dt frame moved the phase", 0f, e.phaseRad, 0f)
        for (j in 0 until Aurora.SHEETS) {
            assertEquals("a zero-dt frame moved an envelope", 0f, env.levels[j], 0f)
        }
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

    /** Far denser than the renderer's own ~242 samples, so no hump can hide between two. */
    private val RIDGE_SAMPLES = 600

    private fun ridgeInset() = Aurora.insetPx(RIDGE_H, Aurora.crestStrokePx(RIDGE_H, 3f))

    /** Crest y at one point across the width, for one sheet of one speaker at one drive. */
    private fun crestAt(
        drive: Float,
        layer: Int,
        speaker: AuroraSpeaker,
        progress: Float,
        phase: Float = 0f,
    ): Float = Aurora.crestY(
        progress,
        RIDGE_H,
        ridgeInset(),
        Aurora.amplitude(RIDGE_H, drive, layer),
        phase,
        layer,
        Aurora.travelDirection(speaker),
        Aurora.phaseBias(speaker),
        Aurora.drift(RIDGE_H, phase, layer, Aurora.driftBias(speaker)),
    )

    /** Bottom-edge y at one point across the width — [crestAt]'s sibling, same arguments. */
    private fun bottomAt(
        drive: Float,
        layer: Int,
        speaker: AuroraSpeaker,
        progress: Float,
        phase: Float = 0f,
    ): Float = Aurora.bottomY(
        progress,
        RIDGE_H,
        ridgeInset(),
        Aurora.amplitude(RIDGE_H, drive, layer),
        phase,
        layer,
        Aurora.travelDirection(speaker),
        Aurora.phaseBias(speaker),
        Aurora.drift(RIDGE_H, phase, layer, Aurora.driftBias(speaker)),
    )

    /** The whole crest across the width, exactly as the renderer walks it, only finer. */
    private fun crestSweep(
        drive: Float,
        layer: Int,
        speaker: AuroraSpeaker,
        phase: Float,
        from: Float = 0f,
        to: Float = 1f,
    ): FloatArray = FloatArray(RIDGE_SAMPLES + 1) { i ->
        crestAt(drive, layer, speaker, from + (to - from) * i / RIDGE_SAMPLES, phase)
    }

    // ------------------------------------------------------------- travel

    /**
     * The window the travel tests measure in, and the number of samples across it.
     *
     * Strictly INSIDE [Aurora.TAPER_FRACTION] (0.12) at both ends, so the taper is exactly 1
     * everywhere in it and the crest is a pure sinusoid plus a constant. Sampling into the taper
     * would anchor both ends of the sweep at the centre line and bias the alignment below toward
     * "nothing moved", which is precisely the failure it is there to detect.
     */
    private val TRAVEL_FROM = 0.14f
    private val TRAVEL_SPAN = 1f - 2f * TRAVEL_FROM
    private val TRAVEL_SAMPLES = 600

    /**
     * The interval the travel tests measure over- short ON PURPOSE. At the shipped crest speeds
     * (0.25..0.37 widths/sec) and hump counts (4.0..4.9), a full second slides the picture by
     * nearly a whole wave period, which aliases back onto itself; over 0.15 s the crest moves well
     * under half a hump, so the cross-correlation alignment is unambiguous. Expressed as the phase
     * advance [PHASE_RATE_PER_SEC] produces in that time.
     */
    private val TRAVEL_MEASURE_SEC = 0.15f
    private val TRAVEL_MEASURE_PHASE = Aurora.PHASE_RATE_PER_SEC * TRAVEL_MEASURE_SEC

    private fun travelSweep(layer: Int, speaker: AuroraSpeaker, phase: Float): FloatArray =
        FloatArray(TRAVEL_SAMPLES + 1) { i ->
            crestAt(drive = 1f, layer = layer, speaker = speaker, progress = TRAVEL_FROM + TRAVEL_SPAN * i / TRAVEL_SAMPLES, phase = phase)
        }

    /** Mean-subtracted copy- the vertical drift is a constant offset and is not sideways travel. */
    private fun centred(v: FloatArray): FloatArray {
        val mean = v.average().toFloat()
        return FloatArray(v.size) { v[it] - mean }
    }

    /**
     * How far [later] has slid relative to [now], in samples, positive meaning rightward.
     *
     * The shift that minimises the mean squared difference between the two mean-subtracted sweeps.
     * A real measurement of the picture rather than the constants read back- it would catch a wave
     * that travelled by some route this test does not know about, and it is blind to the vertical
     * drift, which moves the ribbon without moving it sideways.
     *
     * [maxShift] is bounded WELL INSIDE one wave period in samples (the fastest sheet's period is
     * ~170 samples over [TRAVEL_SPAN]; the closest full-period alias sits at ~124). 80 admits the
     * true small shift a short interval produces (~30-50 samples) and excludes the aliases, so the
     * many-hump wave cannot register a false match one period away.
     */
    private fun bestAlignmentShift(now: FloatArray, later: FloatArray, maxShift: Int = 80): Int {
        val a = centred(now)
        val b = centred(later)
        var best = 0
        var bestErr = Float.MAX_VALUE
        for (k in -maxShift..maxShift) {
            var err = 0f
            var n = 0
            for (i in a.indices) {
                val j = i + k
                if (j < 0 || j >= b.size) continue
                val d = a[i] - b[j]
                err += d * d
                n++
            }
            // Enough overlap that a near-empty window cannot win by having nothing to disagree on.
            if (n < a.size / 2) continue
            val mean = err / n
            if (mean < bestErr) {
                bestErr = mean
                best = k
            }
        }
        return best
    }

    // -------------------------------------------------------------- humps

    /**
     * Where the ribbon is open enough for a hump to be one. Inside this the taper has scaled the
     * excursion down to a fraction of itself, so a hump out here is a bump a viewer would not call
     * one — the taper pinching the ribbon shut is a fine thing to draw and a nonsense thing to
     * count.
     */
    private val RIDGE_TRIM = 0.06f

    /**
     * Sample indices of the crest's local maxima — the HUMPS a viewer would count.
     *
     * Screen y grows downward, so a crest hump is a MINIMUM of y; the sweep is negated and read as
     * a ridge line before being handed to [summits].
     */
    private fun crestPeaks(
        drive: Float,
        layer: Int,
        speaker: AuroraSpeaker,
        phase: Float,
        prominence: Float = 0.02f * RIDGE_H,
    ): List<Int> =
        summits(crestSweep(drive, layer, speaker, phase, RIDGE_TRIM, 1f - RIDGE_TRIM).map { -it }, prominence)

    /**
     * Sample indices of the local maxima of [rise], which is the crest's sweep oriented so that a
     * hump is a maximum.
     *
     * A hysteresis walk rather than a bare three-sample comparison, because float noise on a
     * nearly-flat stretch would otherwise report dozens of "humps" and make the count assertions
     * meaningless. [prominence] is what a hump has to rise above the trough beside it to count as
     * one: 2% of the container height, i.e. about 3 px on the voice screen, which is well under a
     * real hump (30-40 px) and well over the noise.
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
