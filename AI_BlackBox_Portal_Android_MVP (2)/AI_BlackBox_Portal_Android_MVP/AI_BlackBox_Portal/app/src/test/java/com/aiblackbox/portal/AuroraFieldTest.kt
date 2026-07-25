package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.AURORA_BAND_FRAC
import com.aiblackbox.portal.ui.components.AURORA_BASE_WOBBLE_FRAC
import com.aiblackbox.portal.ui.components.AURORA_GRAD_STOPS
import com.aiblackbox.portal.ui.components.AURORA_MAX_STACK
import com.aiblackbox.portal.ui.components.AURORA_OVERLAP
import com.aiblackbox.portal.ui.components.AURORA_PEAK_ALPHA
import com.aiblackbox.portal.ui.components.AURORA_SHEETS
import com.aiblackbox.portal.ui.components.AURORA_STRIP_ALPHA
import com.aiblackbox.portal.ui.components.AURORA_TINTS
import com.aiblackbox.portal.ui.components.AURORA_WOBBLE_FRAC
import com.aiblackbox.portal.ui.components.AuroraSim
import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.auroraFoldEnvelope
import com.aiblackbox.portal.ui.components.auroraStripCount
import com.aiblackbox.portal.ui.components.auroraXWindow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Aurora Curtain physics — headless, seeded, no Compose / Android graphics.
 *
 * This effect carries TWO risks, and after the 2026-07-25 Fold session we know
 * both of them bite:
 *
 *   1. TOO BRIGHT — a large bright REGION sitting on top of the chat text. The
 *      legibility budget is an ACTUAL ASSERTION here: measured additive alpha per
 *      pixel column over 1000 frames, not a comment claiming the arithmetic works
 *      out.
 *   2. TOO DIM — which is what actually shipped. Brandon on the device: "Aurora
 *      curtain — I don't see anything happening here at all." The geometry was
 *      fine; the alphas quantized to 3/255 through android.graphics.Paint's 8-bit
 *      alpha and the field was literally black. An effect nobody can see has no
 *      value and its budget is protecting nothing, so the budget now has a FLOOR
 *      as well as a ceiling and both are measured the same way.
 *
 * The probe drives [AuroraSim.blit], which is the SAME call the renderer draws
 * from (it resolves one strip's destination rect, alpha and tint and mutates
 * nothing), and weights by [auroraXWindow], which is baked into the sprite the
 * renderer blits — so what is measured here is exactly what reaches the screen.
 *
 * Mirrors Portal/modules/fx/effects/aurora.test.mjs, EXCEPT where AuroraField.kt
 * documents a deliberate divergence (alpha ceiling, horizontal window, base
 * wobble, ragged base, per-ray brightness). Those are device-measured; do not
 * "restore parity" on them.
 */
class AuroraFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
    private val scale = 1f

    private companion object {
        /**
         * Float-rounding headroom on the budget assertions, NOT slack in the
         * guarantee. The runtime normalizer divides in Float, so a column that is
         * clamped to exactly the ceiling can land one ulp above it. 1e-6 of a 0.32
         * ceiling is 0.0003% — invisible, and four orders of magnitude below the
         * ~1-6% the measured peak actually sits under the ceiling in practice.
         */
        const val BUDGET_EPS = 1e-6
    }

    /** mulberry32 — the same deterministic PRNG the web test uses. */
    private fun seeded(seed: Int = 1): FieldRandom {
        var a = seed
        return FieldRandom {
            a += 0x6D2B79F5.toInt()
            var t = a
            t = (t xor (t ushr 15)) * (1 or t)
            t += (t xor (t ushr 7)) * (61 or t)
            ((t xor (t ushr 14)).toLong() and 0xFFFFFFFFL).toDouble() / 4294967296.0
        }
    }

    private fun sim(seed: Int = 42, countScale: Float = 1f): AuroraSim =
        AuroraSim(countScale, seeded(seed)).also { it.resize(width, height, scale, density) }

    /**
     * Stand-in for the canvas that measures what actually gets blitted.
     *
     * [cols] accumulates alpha per device-pixel column for ONE frame — under
     * additive blending that sum IS the brightness the chat text has to compete
     * with. Two deliberate properties:
     *   • it weights by [auroraXWindow], because that window is baked into the
     *     sprite: a ray is only at its full alpha along its centre line;
     *   • it ignores the vertical ramp (whose alpha is ≤ 1) and ignores y
     *     entirely, so the number is an UPPER BOUND on any real pixel.
     * AuroraSim.measureBudget bounds the same quantity with a per-bucket maximum
     * of the same window, so this per-pixel probe can never exceed what the sim
     * measured — the ceiling below is a guarantee, not a lucky sample.
     */
    private class Probe(widthPx: Float) {
        val w = ceil(widthPx).toInt().coerceAtLeast(1)
        val cols = DoubleArray(w)
        var maxColumn = 0.0
        var maxBottom = -Float.MAX_VALUE
        var calls = 0
        var bad = 0
        var minH = Float.MAX_VALUE
        var maxH = 0f
        var xSum = 0.0
        val tints = HashSet<Int>()
        /** Every per-strip alpha handed to paint.alpha — the 8-bit floor probe. */
        val alphas = ArrayList<Float>()
        var minGain = Float.MAX_VALUE
        var maxGain = -Float.MAX_VALUE

        fun frame() { cols.fill(0.0); xSum = 0.0 }
        fun endFrame() { for (v in cols) if (v > maxColumn) maxColumn = v }

        /** [o] is (left, top, right, bottom, alpha, tint) — AuroraSim.blit's contract. */
        fun add(o: FloatArray) {
            calls++
            for (v in o) if (!v.isFinite()) { bad++; return }
            val h = o[3] - o[1]
            if (o[3] > maxBottom) maxBottom = o[3]
            if (h < minH) minH = h
            if (h > maxH) maxH = h
            xSum += o[0].toDouble()
            tints.add(o[5].toInt())
            alphas.add(o[4])
            val wid = o[2] - o[0]
            if (wid <= 0f) return
            val lo = max(0, floor(o[0]).toInt())
            val hi = min(w - 1, ceil(o[2]).toInt())
            for (i in lo..hi) {
                cols[i] += (o[4] * auroraXWindow(((i + 0.5f) - o[0]) / wid)).toDouble()
            }
        }

        /** Median of the alphas that reached paint. Sorting a copy — test-only. */
        fun medianAlpha(): Float = alphas.sorted()[alphas.size / 2]
    }

    /** One frame, exactly as EmberOverlay's loop drives it (plus the probe). */
    private fun run(s: AuroraSim, probe: Probe?, frames: Int, dt: Float = 1f / 60f) {
        val out = FloatArray(6)
        for (f in 1..frames) {
            s.update(f * dt * 1000.0, dt, true)
            if (probe == null) continue
            if (s.budgetGain < probe.minGain) probe.minGain = s.budgetGain
            if (s.budgetGain > probe.maxGain) probe.maxGain = s.budgetGain
            probe.frame()
            for (i in s.strips.indices) if (s.blit(i, out)) probe.add(out)
            probe.endFrame()
        }
    }

    // ── 1. Population ──

    @Test fun `strip population stays in the 24 to 48 curtain band on every viewport`() {
        // Too few and the banding reads as fenceposts; too many and it reads as
        // fringe. Both ends must hold from a small phone to a 4K panel.
        val boxes = arrayOf(
            floatArrayOf(320f, 640f, 3.1f),        // tiny phone
            floatArrayOf(720f, 1280f, 2.0f),       // 360x640 dp
            floatArrayOf(1080f, 2400f, 3.1f),      // reference phone
            floatArrayOf(1812f, 2176f, 3.1f),      // Fold, unfolded
            floatArrayOf(2560f, 1600f, 2.0f),      // tablet, landscape
            floatArrayOf(3840f, 2160f, 1.0f),      // 4K at 1x
        )
        for (b in boxes) {
            val s = AuroraSim(1f, seeded(7)).also { it.resize(b[0], b[1], b[2] / 3.1f, b[2]) }
            val n = s.strips.size
            assertTrue("${b[0]}x${b[1]} @${b[2]} produced $n strips (want 24..48)", n in 24..48)
        }
    }

    @Test fun `the count dial cannot push the population out of its band`() {
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val n = sim(countScale = cs).strips.size
            assertTrue("countScale $cs produced $n strips", n in 24..48)
        }
    }

    @Test fun `two sub-populations, genuinely different — not one sheet twice`() {
        val s = sim()
        assertEquals("exactly two sheets must be alive", 2, s.strips.map { it.sheet }.toSet().size)
        val a = AURORA_SHEETS[0]
        val b = AURORA_SHEETS[1]
        assertTrue("sheets must hang at different heights", a.baseFrac != b.baseFrac)
        assertTrue(a.heightFrac != b.heightFrac)
        assertTrue(a.alphaMul != b.alphaMul)
        assertTrue("the violet sheet must fold strictly faster than the crimson one", b.rateMin > a.rateMax)
        assertTrue("sheets must ripple at different frequencies", a.of0 != b.of0)
        // …and the two populations must actually differ in the field, not just in config.
        val r0 = s.strips.filter { it.sheet == 0 }.map { it.rate }
        val r1 = s.strips.filter { it.sheet == 1 }.map { it.rate }
        assertTrue("no rate overlap between sheets", r1.min() > r0.max())
    }

    // ── 2. THE LEGIBILITY BUDGET — a ceiling AND a floor ──

    @Test fun `total additive alpha in any column never exceeds the budget`() {
        // 1000 frames at 60 Hz ≈ 16 s — several full fold cycles of both sheets.
        val s = sim(seed = 0xA47)
        val probe = Probe(width)
        run(s, probe, 1000)
        assertTrue("expected a busy field, got ${probe.calls} blits", probe.calls > 10000)
        assertTrue("peak column alpha ${probe.maxColumn} busts the $AURORA_PEAK_ALPHA budget",
            probe.maxColumn <= AURORA_PEAK_ALPHA + BUDGET_EPS)
    }

    @Test fun `the field is actually BRIGHT ENOUGH TO SEE — the regression Brandon hit`() {
        // THE FLOOR. This is the assertion the shipped version failed on the Fold:
        // it drew, it moved, and it was invisible. Both halves are asserted as
        // PROPERTIES of what reaches the canvas, not as literals, so a retune that
        // keeps the effect visible is free to move the constants.
        val s = sim(seed = 0xA47)
        val probe = Probe(width)
        run(s, probe, 1000)

        // (a) The field must USE the legibility budget it was granted. The shipped
        //     version peaked at 44% of its own budget and averaged 9% of it,
        //     because per-strip alpha was PEAK_ALPHA / MAX_STACK — an analytic
        //     worst case a pixel essentially never sees.
        assertTrue("peak column alpha ${probe.maxColumn} uses under 70% of the " +
            "$AURORA_PEAK_ALPHA budget — the field is leaving light on the table",
            probe.maxColumn > AURORA_PEAK_ALPHA * 0.7)

        // (b) SURVIVES 8-BIT. android.graphics.Paint takes an integer alpha, so an
        //     alpha under 1/510 is floored to nothing and the baked ramp (also
        //     8-bit, premultiplied) drops most of a strip well before that. The
        //     shipped version handed paint a median of 3/255 and a max of 7/255.
        val med = (probe.medianAlpha() * 255f).roundToInt()
        val top = (probe.alphas.max() * 255f).roundToInt()
        assertTrue("median strip alpha quantizes to $med/255 — at that level most of " +
            "the baked ramp rounds to zero and the curtain is black", med >= 8)
        assertTrue("brightest strip alpha quantizes to $top/255 — no curtain ever " +
            "reads as light", top >= 20)
    }

    @Test fun `the runtime normalizer can only ever dim, and never crushes the field`() {
        // The ceiling is enforced at RUNTIME (AuroraSim.measureBudget) instead of
        // by dividing the per-strip alpha by a worst case that never happens. Two
        // invariants make that safe: the gain is a gain (≤ 1, so it cannot brighten
        // past the budget) and it is not a sledgehammer (a frame is never dimmed to
        // the point of re-creating the invisible-field bug).
        for (cs in floatArrayOf(1f, ParticleTuning.COUNT_SCALE_MAX)) {
            val s = sim(seed = 0xB33, countScale = cs)
            val probe = Probe(width)
            run(s, probe, 400)
            assertTrue("countScale $cs leaked ${probe.maxColumn} past the budget",
                probe.maxColumn <= AURORA_PEAK_ALPHA + BUDGET_EPS)
            assertTrue("gain ${probe.maxGain} at countScale $cs would BRIGHTEN past the budget",
                probe.maxGain <= 1f + 1e-6f)
            assertTrue("gain ${probe.minGain} at countScale $cs dims the field into nothing",
                probe.minGain > 0.3f)
        }
    }

    @Test fun `nothing is ever drawn below the top 55 percent of the canvas`() {
        val s = sim(seed = 0xA47)
        val probe = Probe(width)
        run(s, probe, 1000)
        val limit = height * AURORA_BAND_FRAC
        assertTrue("a curtain reached y=${probe.maxBottom}, past the $limit clamp into the message area",
            probe.maxBottom <= limit + 1e-3f)
    }

    @Test fun `the baked gradient reaches ZERO at the clamp line and peaks at the base`() {
        assertEquals("top edge must fade in from nothing", 0f, AURORA_GRAD_STOPS.first()[1], 0f)
        assertEquals("bottom edge MUST be alpha 0 — this is what keeps light off the chat text",
            0f, AURORA_GRAD_STOPS.last()[1], 0f)
        var peak = 0f
        var peakAt = 0f
        for (st in AURORA_GRAD_STOPS) if (st[1] > peak) { peak = st[1]; peakAt = st[0] }
        assertTrue("brightest stop is at $peakAt; an aurora is brightest at its BASE", peakAt >= 0.7f)
        assertTrue("a stop above alpha 1 breaks the budget maths", peak <= 1f)
        for (i in 1 until AURORA_GRAD_STOPS.size) {
            assertTrue("stop offsets must be strictly increasing",
                AURORA_GRAD_STOPS[i][0] > AURORA_GRAD_STOPS[i - 1][0])
        }
        // …and the ramp must not spend its body under Android's 8-bit alpha floor,
        // which is what made the top of every curtain quantize to black. At the
        // median strip alpha the ramp's own alpha has to clear ~1/255 over most of
        // the strip, so the interior stops need real height.
        val interior = AURORA_GRAD_STOPS.drop(1).dropLast(1)
        assertTrue("the ramp is too thin to survive 8-bit quantization",
            interior.all { it[1] >= 0.3f })
    }

    @Test fun `the per-strip alpha ceiling is tuned, not derived from the worst-case stack`() {
        // DELIBERATE DIVERGENCE from aurora.js, and the whole fix: STRIP_ALPHA used
        // to be PEAK_ALPHA / MAX_STACK. MAX_STACK = 6 is a correct GEOMETRIC bound
        // and is still pinned below, but paying it on every strip made the typical
        // pixel ~11x darker than the legibility budget allowed. The ceiling now
        // lives at runtime (previous test); this one guards the derivation the
        // geometry still owes, and that no strip exceeds its own declared ceiling.
        val naive = AURORA_PEAK_ALPHA / AURORA_MAX_STACK
        assertTrue("STRIP_ALPHA $AURORA_STRIP_ALPHA is back to the naive worst-case " +
            "quotient $naive — that is the bug that made this effect invisible",
            AURORA_STRIP_ALPHA > naive * 2f)
        val perSheet = ceil(AURORA_OVERLAP).toInt() + ceil(2f * AURORA_WOBBLE_FRAC).toInt() - 1
        assertTrue("MAX_STACK $AURORA_MAX_STACK is below the derived worst case",
            AURORA_MAX_STACK >= perSheet * AURORA_SHEETS.size)
        for (sh in AURORA_SHEETS) {
            assertTrue("a sheet may dim itself, never brighten past the ceiling", sh.alphaMul <= 1f)
        }
        assertTrue("no strip may exceed its own ceiling",
            sim().strips.all { it.alphaMax <= AURORA_STRIP_ALPHA + 1e-9f })
    }

    // ── 3. The horizontal window — soft-edged rays, and an exact budget bound ──

    @Test fun `the ray window is a unimodal soft-edged bell that keeps most of its light`() {
        assertEquals("a ray must fade to nothing at its own edge, or rays abut in a " +
            "picket fence", 0f, auroraXWindow(0f), 0f)
        assertEquals(0f, auroraXWindow(1f), 0f)
        assertEquals("full alpha along the ray's centre line", 1f, auroraXWindow(0.5f), 1e-6f)
        assertEquals("a NaN must not leak light", 0f, auroraXWindow(Float.NaN), 0f)
        assertEquals(0f, auroraXWindow(-0.2f), 0f)
        assertEquals(0f, auroraXWindow(1.4f), 0f)
        var prev = -1f
        var area = 0.0
        for (i in 0..1000) {
            val u = i / 1000f
            val v = auroraXWindow(u)
            assertTrue("window out of range at $u: $v", v in 0f..1f)
            assertEquals("the window must be symmetric about the ray's axis",
                v, auroraXWindow(1f - u), 1e-5f)
            // UNIMODAL is what makes AuroraSim.measureBudget's per-bucket maximum an
            // exact bound rather than a sample: monotone up to the crest is half of it.
            if (u <= 0.5f) {
                assertTrue("the window must rise monotonically to its crest", v >= prev - 1e-6f)
                prev = v
            }
            area += v / 1001.0
        }
        assertTrue("mean window $area — a ray that soft loses too much light to read",
            area > 0.6)
    }

    // ── 4. Premium rule (a): size over life, with a per-strip envelope ──

    @Test fun `the size-over-life curve opens fast, closes slow, and is zero at both ends`() {
        assertEquals(0f, auroraFoldEnvelope(0f), 0f)
        assertEquals(0f, auroraFoldEnvelope(1f), 0f)
        var maxV = 0f
        var maxAt = 0f
        for (i in 0..1000) {
            val t = i / 1000f
            val v = auroraFoldEnvelope(t)
            assertTrue("envelope out of range at $t: $v", v >= 0f && v <= 1f)
            if (v > maxV) { maxV = v; maxAt = t }
        }
        assertTrue("curve never reaches full size (peak $maxV)", maxV > 0.999f)
        assertTrue("crest at $maxAt — the swell must be faster than the ebb", maxAt < 0.4f)
    }

    @Test fun `every strip carries its OWN envelope multiplier and drawn size is never constant`() {
        val s = sim(seed = 0xA47)
        val env0 = s.strips.map { it.env0 }
        assertEquals("envelope multipliers must be per-strip, not shared", env0.size, env0.toSet().size)
        assertTrue("multipliers must actually spread", env0.max() / env0.min() > 1.2f)
        val probe = Probe(width)
        run(s, probe, 1000)
        assertTrue("drawn heights spanned only ${probe.maxH / probe.minH}x — size is near-constant",
            probe.maxH / probe.minH > 2f)
    }

    // ── 5. Banding, not a slab: per-ray tone and a ragged base line ──

    @Test fun `neighbouring rays differ in BRIGHTNESS, not just in phase`() {
        // A sheet whose rays are all equally bright reads as one translucent slab
        // however well its geometry ripples. (Divergence from aurora.js, which
        // gives every strip in a sheet the same alpha ceiling.)
        val s = sim(seed = 0xA47)
        val b = s.strips.map { it.bright }
        assertEquals("brightness must be per-strip, not shared", b.size, b.toSet().size)
        // Asserted as a SPREAD, not a ratio of extremes: with ~36 draws the range of
        // a uniform population is the stable statistic (its ratio is not).
        assertTrue("ray brightness spans only ${b.max() - b.min()} — that is a slab",
            b.max() - b.min() > 0.2f)
        assertTrue("a ray may dim itself, never brighten past its sheet's ceiling",
            b.all { it in 0.01f..1f })
    }

    @Test fun `the base line is RAGGED and still never crosses the clamp`() {
        val s = sim(seed = 0xA47)
        for (sheet in 0..1) {
            val sh = AURORA_SHEETS[sheet]
            val bases = s.strips.filter { it.sheet == sheet }.map { it.baseY0 }
            assertTrue("${sh.key} hangs from one ruled line — that is a slab edge",
                bases.toSet().size > bases.size * 0.9)
            val nominal = height * sh.baseFrac
            assertTrue("a base may only ever be lifted ABOVE the sheet's nominal line",
                bases.all { it <= nominal + 1e-3f && it >= nominal - height * sh.baseJit - 1e-3f })
        }
        // The undulation must be big enough to read: the web's 9 CSS px landed at
        // 0.6% of a 2400 px screen, which is no undulation at all.
        assertTrue("base wobble must be a real fraction of the band",
            AURORA_BASE_WOBBLE_FRAC >= 0.02f)
        for (p in s.strips) {
            assertEquals("wobble amplitude is a band fraction, not a CSS px length",
                height * AURORA_BAND_FRAC * AURORA_BASE_WOBBLE_FRAC, p.ay, 1e-2f)
        }
    }

    // ── 6. Premium rule (b): colour ramp indexed by life ──

    @Test fun `tint is indexed by life, and the two sheets occupy different ramp regions`() {
        val top = AURORA_TINTS.size - 1
        for (p in sim(seed = 0xA47).strips) {
            val seen = HashSet<Int>()
            for (i in 0..100) {
                seen.add((p.hueLo + p.hueSpan * (i / 100f) + 0.5f).toInt().coerceIn(0, top))
            }
            assertTrue("a strip must traverse the ramp as it folds, not sit on one colour", seen.size >= 2)
            if (p.sheet == 0) assertTrue("crimson sheet reached violet index ${seen.max()}", seen.max() <= 2)
            else assertTrue("violet sheet reached crimson index ${seen.min()}", seen.min() >= 2)
        }
    }

    @Test fun `the ramp is genuinely exercised on screen`() {
        val s = sim(seed = 0xA47)
        val probe = Probe(width)
        run(s, probe, 1000)
        assertTrue("only ${probe.tints.size} tints reached the canvas — that reads as a flat colour",
            probe.tints.size >= 3)
        assertEquals(5, AURORA_TINTS.size)
        for (c in AURORA_TINTS) assertEquals(3, c.size)
    }

    // ── 7. The multi-octave curtain (the thing that makes it a curtain, not a blob) ──

    @Test fun `x motion is a 3-octave sum with distinct frequencies and a per-strip phase step`() {
        for (sh in AURORA_SHEETS) {
            val freqs = setOf(sh.of0, sh.of1, sh.of2)
            assertEquals("octaves must not share a frequency", 3, freqs.size)
            for (amp in floatArrayOf(sh.ox0, sh.ox1, sh.ox2)) {
                assertTrue("every octave must contribute amplitude", amp > 0f)
            }
            for (step in floatArrayOf(sh.os0, sh.os1, sh.os2)) {
                assertTrue("phase step per strip is what makes the wave TRAVEL along x", step > 0f)
            }
            assertTrue("height needs its own sum or the base line is flat", sh.hf0 != sh.hf1)
            val sum = sh.ox0 + sh.ox1 + sh.ox2
            assertTrue("${sh.key} x amplitude sums to $sum stripW; MAX_STACK assumes ≤ $AURORA_WOBBLE_FRAC",
                sum <= AURORA_WOBBLE_FRAC + 1e-6f)
        }
    }

    @Test fun `each strip's horizontal offset stays inside the bound MAX_STACK depends on`() {
        for (p in sim().strips) {
            val amp = p.ax0 + p.ax1 + p.ax2
            assertTrue("strip amplitude $amp exceeds $AURORA_WOBBLE_FRAC * stripW ${p.stripW}",
                amp <= AURORA_WOBBLE_FRAC * p.stripW + 1e-3f)
        }
    }

    @Test fun `the curtain ripples ACROSS — neighbours are out of phase and the field moves`() {
        val s = sim(seed = 0xA47)
        // Spatial: at one instant, adjacent slots are displaced differently. A rigid
        // slab translation (all offsets equal) is THE blob failure mode.
        val sheet0 = s.strips.indices.filter { s.strips[it].sheet == 0 }
        val atT = sheet0.map { String.format("%.4f", s.foldOffset(it, 3.7)) }
        assertTrue("strips are moving together — that is a slab, not a curtain",
            atT.toSet().size > sheet0.size * 0.9)
        // Temporal: each strip oscillates about its slot in BOTH directions.
        for (i in sheet0) {
            var lo = Float.MAX_VALUE
            var hi = -Float.MAX_VALUE
            for (k in 0 until 2000) {
                val v = s.foldOffset(i, k * 0.05)
                if (v < lo) lo = v
                if (v > hi) hi = v
            }
            assertTrue("a strip must fold to both sides of its slot", lo < 0f && hi > 0f)
            assertTrue("fold travel is too small to read as motion", hi - lo > s.strips[i].stripW * 0.2f)
        }
        // …and the drawn field is genuinely not static.
        val probe = Probe(width)
        run(s, probe, 1)
        val first = probe.xSum
        run(s, probe, 900)
        val later = probe.xSum
        assertTrue("the field did not move over 900 frames", abs(later - first) > 1.0)
    }

    // ── 8. Frame-rate independence and the DPI law ──

    @Test fun `60 Hz and 120 Hz reach the same field after the same elapsed time`() {
        val a = sim(seed = 0x51D)
        val b = sim(seed = 0x51D)
        run(a, null, 600, 1f / 60f)
        run(b, null, 1200, 1f / 120f)
        assertEquals("clock drift", a.clock, b.clock, 1e-6)
        assertEquals(a.strips.size, b.strips.size)
        a.strips.forEachIndexed { i, p ->
            val d = abs(p.life - b.strips[i].life)
            // 1e-4 is Float accumulation noise over 1200 adds, not a dt bug: a real
            // one (constants per 60 Hz tick) would drift by ~0.5 of a whole life.
            assertTrue("strip $i life drifted by $d between refresh rates", min(d, 1f - d) < 1e-4f)
        }
    }

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1x and 2x density: every spatial
        // quantity must come out exactly 2x in pixels, i.e. identical in apparent
        // size. Population is dp-box derived, so it must NOT change — and neither
        // may the budget gain, which is measured from ratios only.
        val lo = AuroraSim(1f, seeded(88)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = AuroraSim(1f, seeded(88)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("population is dp-derived, not px-derived", lo.strips.size, hi.strips.size)
        run(lo, null, 300)
        run(hi, null, 300)
        assertEquals("the legibility gain is a ratio — density may not touch it",
            lo.budgetGain, hi.budgetGain, 1e-5f)
        val a = FloatArray(6)
        val b = FloatArray(6)
        var checked = 0
        for (i in lo.strips.indices) {
            val p = lo.strips[i]
            val q = hi.strips[i]
            assertEquals("stored fold state is density-free", p.life, q.life, 1e-6f)
            assertEquals("…and so is the per-strip envelope", p.env0, q.env0, 1e-6f)
            assertEquals("…and the per-ray brightness", p.bright, q.bright, 1e-6f)
            assertEquals("x amplitude must be exactly 2x", p.ax0 * 2f, q.ax0, 1e-3f)
            assertEquals("the base wobble is a band fraction — 2x in px", p.ay * 2f, q.ay, 1e-3f)
            val drewLo = lo.blit(i, a)
            val drewHi = hi.blit(i, b)
            assertEquals("the same strips must draw at both densities", drewLo, drewHi)
            if (!drewLo) continue
            checked++
            for (k in 0..3) {
                assertEquals("blit coord $k must be exactly 2x at 2x density", a[k] * 2f, b[k], 1e-2f)
            }
            assertEquals("alpha is dimensionless — density may not touch it", a[4], b[4], 1e-6f)
            assertEquals("nor the tint", a[5], b[5], 0f)
        }
        assertTrue("nothing was drawn — the comparison proved nothing", checked > 20)
    }

    // ── 9. Hot path, lifecycle, determinism ──

    @Test fun `the frame loop draws no randomness and never rebuilds the pool`() {
        var calls = 0
        val base = seeded(0xC0FFEE)
        val counting = FieldRandom { calls++; base.next() }
        val s = AuroraSim(1f, counting).also { it.resize(width, height, scale, density) }
        val afterBuild = calls
        val slots = s.strips.toList()
        run(s, Probe(width), 1000)
        assertEquals("rand() in the frame loop makes the physics unreproducible", afterBuild, calls)
        assertEquals("the strip pool must be reused, never rebuilt per frame", slots.size, s.strips.size)
        assertTrue("a strip must keep its slot object for the whole session",
            s.strips.withIndex().all { (i, p) -> p === slots[i] })
    }

    @Test fun `no NaN anywhere after 1000 frames`() {
        val s = sim(seed = 0xA47)
        val probe = Probe(width)
        run(s, probe, 1000)
        assertEquals("a non-finite value reached the canvas", 0, probe.bad)
        assertTrue(s.clock.isFinite())
        assertTrue("the budget gain went non-finite", s.budgetGain.isFinite())
        for (p in s.strips) {
            for (v in floatArrayOf(p.life, p.rate, p.env0, p.bright, p.baseJit, p.slotX, p.drawW,
                    p.hMax, p.baseY0, p.ay, p.ax0, p.ax1, p.ax2, p.alphaMax, p.hueLo)) {
                assertTrue("a strip field went non-finite ($v)", v.isFinite())
            }
            assertTrue("life escaped [0,1): ${p.life}", p.life >= 0f && p.life < 1f)
        }
    }

    @Test fun `a long stall does not throw life out of range`() {
        val s = sim()
        s.update(45_000.0, 45f, true)                 // 45 s in one tick (device resumed)
        for (p in s.strips) assertTrue("life ${p.life} after stall", p.life >= 0f && p.life < 1f)
    }

    @Test fun `resize keeps folds running instead of restarting the field`() {
        // The composer's height changes on every keystroke; rebuilding there would
        // blink the whole curtain once a second while typing.
        val s = sim(seed = 12)
        run(s, null, 120)
        val before = s.strips.map { it.life }
        val slots = s.strips.toList()
        val shorter = height - 340f
        s.resize(width, shorter, scale, density)                // keyboard opened
        assertEquals("a height change must not re-scatter the field", before, s.strips.map { it.life })
        assertTrue("survivors keep their slot object", s.strips.withIndex().all { (i, p) -> p === slots[i] })
        // …but the LAYOUT must follow the new viewport, or the clamp line is stale.
        // (The base line is per-strip now — its ragged offset is stored on the strip
        // and re-applied to the NEW height, which is what keeps it density-free.)
        for (p in s.strips) {
            assertEquals("base line must track the new height",
                shorter * (AURORA_SHEETS[p.sheet].baseFrac - p.baseJit), p.baseY0, 1e-3f)
        }
    }

    @Test fun `resize across a count change stays in bounds with dense slots`() {
        val s = AuroraSim(1f, seeded(5)).also { it.resize(360f, 640f, 1f, 3.1f) }
        val boxes = arrayOf(
            floatArrayOf(3840f, 2160f, 1.0f),
            floatArrayOf(900f, 900f, 1.0f),
            floatArrayOf(1080f, 2400f, 3.1f),
            floatArrayOf(2560f, 1440f, 2.0f),
        )
        for (b in boxes) {
            s.resize(b[0], b[1], b[2] / 3.1f, b[2])
            val n = s.strips.size
            assertTrue("${b[0]}x${b[1]} after resize gave $n strips", n in 24..48)
            for (sheet in 0..1) {
                val slots = s.strips.filter { it.sheet == sheet }.map { it.slot }
                assertEquals("slots must stay a dense 0..n-1 range", slots.indices.toList(), slots)
            }
        }
        // …and the budget still holds after all that churn.
        val probe = Probe(2560f)
        run(s, probe, 300)
        assertTrue("post-resize budget ${probe.maxColumn}", probe.maxColumn <= AURORA_PEAK_ALPHA + BUDGET_EPS)
        assertTrue("post-resize clamp broke", probe.maxBottom <= 1440f * AURORA_BAND_FRAC + 1e-3f)
    }

    @Test fun `rearm keeps the ambient field running`() {
        val s = sim(seed = 6)
        run(s, null, 120)
        val lives = s.strips.map { it.life }
        s.rearm()
        assertEquals("an aurora does not restart per turn", lives, s.strips.map { it.life })
    }

    @Test fun `a zero-size viewport produces nothing and self-heals`() {
        val s = AuroraSim(1f, seeded(3))
        s.resize(0f, 0f, scale, density)
        assertTrue("a zero box must not build a field", s.strips.isEmpty())
        run(s, null, 60)                                        // must not throw
        s.resize(width, height, scale, density)
        assertTrue("a real size must build one", s.strips.size in 24..48)
        val out = FloatArray(6)
        run(s, null, 60)
        for (i in s.strips.indices) {
            if (!s.blit(i, out)) continue
            for (v in out) assertTrue("a non-finite value survived the zero-size box", v.isFinite())
        }
    }

    @Test fun `stripCount is clamped at both ends of the geometry range`() {
        for (sh in AURORA_SHEETS) {
            assertEquals((sh.base * 0.9).roundToInt().coerceIn(sh.min, sh.max),
                auroraStripCount(sh, 0.5f, 1f))
            assertTrue(auroraStripCount(sh, 0.1f, 1f) >= sh.min)
            assertTrue(auroraStripCount(sh, 9f, 1f) <= sh.max)
            assertTrue(auroraStripCount(sh, 1f, ParticleTuning.COUNT_SCALE_MIN) >= sh.min)
            assertTrue(auroraStripCount(sh, 1f, ParticleTuning.COUNT_SCALE_MAX) <= sh.max)
        }
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 99)
            run(s, null, 240)
            val out = FloatArray(6)
            return s.strips.indices.joinToString { i ->
                if (s.blit(i, out)) out.joinToString(",") else "-"
            }
        }
        assertEquals(once(), once())
    }

    @Test fun `the fold offset is the sum the renderer actually draws`() {
        // Guards the probe's mirror: foldOffset must be the same arithmetic blit
        // uses, so a retune can never make the test measure a stale field.
        val s = sim(seed = 77)
        run(s, null, 90)
        val p = s.strips[0]
        val expected = (p.ax0 * sin(s.clock * p.f0 + p.p0) +
            p.ax1 * sin(s.clock * p.f1 + p.p1) +
            p.ax2 * sin(s.clock * p.f2 + p.p2)).toFloat()
        assertEquals(expected, s.foldOffset(0, s.clock), 1e-4f)
    }
}
