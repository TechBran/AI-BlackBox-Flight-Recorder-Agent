package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.BOKEH_APPARENT
import com.aiblackbox.portal.ui.components.BOKEH_INK_BUDGET
import com.aiblackbox.portal.ui.components.BOKEH_LARGE_DISC_PX
import com.aiblackbox.portal.ui.components.BOKEH_NEAR_ALPHA_CAP
import com.aiblackbox.portal.ui.components.BOKEH_PLANES
import com.aiblackbox.portal.ui.components.BOKEH_RAMP
import com.aiblackbox.portal.ui.components.BokehDisc
import com.aiblackbox.portal.ui.components.BokehSim
import com.aiblackbox.portal.ui.components.FIELD_REFERENCE_DENSITY
import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.bokehAlpha
import com.aiblackbox.portal.ui.components.bokehRadius
import com.aiblackbox.portal.ui.components.bokehRampIndex
import com.aiblackbox.portal.ui.components.bokehSizeCurve
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Bokeh Depth physics — headless, seeded, no Compose / Android graphics.
 *
 * The sim takes its randomness from an injected [FieldRandom], so the whole
 * simulation is drivable from the JVM with a deterministic PRNG. What is asserted
 * here is what would otherwise only be caught by staring at a phone:
 *
 *   1. the PARALLAX CORRELATION — size/alpha/speed/sprite-form must move together
 *      across the four planes, or the screen flattens into confetti;
 *   2. the LEGIBILITY BUDGET — the near discs are enormous, so alpha is the whole
 *      budget; measured against the real --muted body-text colour (BbxDim,
 *      #C9C9C9), not a vibe;
 *   3. delta timing — identical motion at 60 and 120 Hz;
 *   4. THE DPI LAW — density changes resolution, never apparent geometry;
 *   5. pooling — the same particle objects for the life of the field.
 *
 * Mirrors Portal/modules/fx/effects/bokeh.test.mjs — keep them in step.
 */
class BokehFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = FIELD_REFERENCE_DENSITY    // → scale 1
    private val scale = 1f

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

    private fun sim(seed: Int = 42, countScale: Float = 1f, w: Float = width, h: Float = height): BokehSim =
        BokehSim(countScale, seeded(seed)).also { it.resize(w, h, scale, density) }

    /** A sim on one of [viewports] — (width, height, DPI scale), with the density
     *  that scale implies, so geometry and population are both that device's. */
    private fun simFor(v: FloatArray, seed: Int): BokehSim =
        BokehSim(1f, seeded(seed)).also { it.resize(v[0], v[1], v[2], v[2] * FIELD_REFERENCE_DENSITY) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: BokehSim, frames: Int, dt: Float = 1f / 60f) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, true)
    }

    /** Device px per web CSS px, for a given DPI scale (the sim's private `g`). */
    private fun geo(s: Float = scale): Float = s * BOKEH_APPARENT * FIELD_REFERENCE_DENSITY

    private fun plane(s: BokehSim, i: Int): List<BokehDisc> =
        s.discs.filter { it.plane === BOKEH_PLANES[i] }

    private fun mean(xs: List<Float>): Float = xs.sum() / xs.size

    /**
     * The published population formula, restated so a drift in either copy fails:
     * a dp-AREA term clamped to [0.4, 2.4] × the operator's dial, clamped to
     * [0.12, 4], applied per plane and floored at 1.
     */
    private fun expectedCounts(w: Float, h: Float, d: Float, dial: Float): IntArray {
        val areaDp = (w / d) * (h / d)
        val s = ((areaDp / (1440f * 900f)).coerceIn(0.4f, 2.4f) * dial).coerceIn(0.12f, 4f)
        return IntArray(BOKEH_PLANES.size) { ParticleTuning.scaleCount(BOKEH_PLANES[it].count, s) }
    }

    // --muted / BbxDim (0xFFC9C9C9). Discs are drawn additively over near-black,
    // so a disc's composite is alpha × its ramp colour.
    private fun lin(v: Float): Double {
        val c = (v / 255f).toDouble()
        return if (c <= 0.04045) c / 12.92 else Math.pow((c + 0.055) / 1.055, 2.4)
    }

    private fun luminance(r: Float, g: Float, b: Float): Double =
        0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    private val mutedL = luminance(201f, 201f, 201f)

    // ── 1. Four planes, populated, ordered ──

    @Test fun `four planes, each populated, counts from dp area x the dial`() {
        assertEquals("the depth IS the four planes", 4, BOKEH_PLANES.size)
        for (dial in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val s = sim(seed = 5, countScale = dial)
            val want = expectedCounts(width, height, density, dial)
            assertEquals("total population at dial $dial", want.sum(), s.discs.size)
            for (i in BOKEH_PLANES.indices) {
                assertEquals("${BOKEH_PLANES[i].id} @ $dial", want[i], plane(s, i).size)
                assertTrue("a plane vanished at dial $dial — the depth needs all four", want[i] >= 1)
            }
        }
        // …and on a small phone and a big tablet at the default dial.
        for (w in floatArrayOf(320f, 1080f, 2560f)) {
            val s = sim(seed = 7, w = w, h = w * 2f)
            val want = expectedCounts(w, w * 2f, density, 1f)
            assertEquals("a ${w}px canvas", want.sum(), s.discs.size)
        }
    }

    @Test fun `the pool is grouped FAR to NEAR — the painter's order`() {
        val s = sim(seed = 11)
        var seen = 0
        for (p in s.discs) {
            val idx = BOKEH_PLANES.indexOfFirst { it === p.plane }
            assertTrue("planes must be contiguous blocks, far first", idx >= seen)
            seen = idx
        }
    }

    @Test fun `two sprite forms — the near planes are annuli, the far ones solid blobs`() {
        assertEquals(listOf("inner", "near"), BOKEH_PLANES.filter { it.donut }.map { it.id })
        assertEquals(listOf("dust", "grain"), BOKEH_PLANES.filter { !it.donut }.map { it.id })
    }

    // ── 2. Parallax correlation — the whole effect ──

    @Test fun `size, alpha and speed are CORRELATED across the planes, far to near`() {
        for (i in 1 until BOKEH_PLANES.size) {
            val far = BOKEH_PLANES[i - 1]
            val near = BOKEH_PLANES[i]
            assertTrue("${near.id} must be strictly bigger than ${far.id}", near.rMin > far.rMax)
            assertTrue("${near.id} must be dimmer than ${far.id}", near.alpha < far.alpha)
            assertTrue("${near.id} must be strictly slower than ${far.id}", near.speedMax < far.speedMin)
            // Lifetimes overlap between neighbours on purpose (nothing should die on
            // a schedule), so this one is a mean, not a strict separation.
            assertTrue("${near.id} must outlive ${far.id} on average",
                near.lifeMin + near.lifeMax > far.lifeMin + far.lifeMax)
        }
    }

    @Test fun `the LIVE field reproduces that correlation, not just the config`() {
        // A fat dial so every plane has enough discs for its MEAN to mean something.
        val s = sim(seed = 13, countScale = 2f)
        run(s, 120)
        val gg = geo()
        for (i in 1 until BOKEH_PLANES.size) {
            val far = plane(s, i - 1)
            val near = plane(s, i)
            assertTrue("${BOKEH_PLANES[i].id} discs must render bigger",
                mean(near.map { bokehRadius(it, gg) }) > mean(far.map { bokehRadius(it, gg) }))
            assertTrue("${BOKEH_PLANES[i].id} must creep slower",
                mean(near.map { abs(it.vx) }) < mean(far.map { abs(it.vx) }))
        }
    }

    @Test fun `every disc drifts LEFTWARD and keeps drifting`() {
        val s = sim(seed = 17)
        assertTrue("a plane with mixed drift directions has no parallax", s.discs.all { it.vx < 0f })
        val before = s.discs.map { Triple(it, it.x, it.decay) }
        run(s, 20)                                    // 0.33 s — shorter than any possible lifetime
        var moved = 0
        for ((p, x0, decay0) in before) {
            if (p.decay != decay0) continue           // skip anything that recycled
            assertTrue("${p.plane.id} did not move left", p.x < x0)
            moved++
        }
        assertTrue("too few survivors to judge: $moved", moved > 20)
    }

    // ── 3. Delta timing ──

    @Test fun `60 Hz and 120 Hz travel the same path`() {
        // Half a second of wall clock, driven at both rates. A per-frame (rather
        // than per-second) constant anywhere in update() doubles the distance and
        // trips this immediately. A disc that walked off the left edge in ONE run
        // and not (yet) the other is skipped — reset() re-rolls decay, which is the
        // flag for it — exactly as the web test does.
        val a = sim(seed = 3)
        val snapshot = a.discs.map { it.decay }
        val b = sim(seed = 3)
        run(a, 30, 1f / 60f)
        run(b, 60, 1f / 120f)
        assertEquals(a.discs.size, b.discs.size)
        var worstPos = 0f
        var worstLife = 0f
        var compared = 0
        a.discs.forEachIndexed { i, p ->
            val q = b.discs[i]
            if (p.decay != snapshot[i] || q.decay != snapshot[i]) return@forEachIndexed
            worstPos = max(worstPos, max(abs(p.x - q.x), abs(p.y - q.y)))
            worstLife = max(worstLife, abs(p.life - q.life))
            compared++
        }
        assertTrue("too few comparable discs: $compared", compared > 20)
        assertTrue("a 120 Hz screen drifted ${worstPos}px from a 60 Hz one", worstPos < 0.5f)
        assertTrue("life is not delta-timed ($worstLife)", worstLife < 1e-4f)
    }

    @Test fun `a parked frame loop is clamped, not integrated`() {
        val s = sim(seed = 23)
        val p = plane(s, 3)[0]                        // the slowest, biggest plane
        val x0 = p.x
        val life0 = p.life
        s.update(4000.0, 4f, true)                    // four seconds in one frame
        assertTrue("a 4 s jump must not teleport the field",
            abs(p.x - x0) < abs(p.vx * geo()) * 0.11f)
        assertTrue("life must be clamped with the motion", life0 - p.life <= p.decay * 0.1f + 1e-6f)
    }

    // ── 4. The three premium rules ──

    @Test fun `(a) size follows a curve over life, with a per-particle envelope`() {
        assertTrue("a disc must swell as it defocuses",
            bokehSizeCurve(1f) < bokehSizeCurve(0.5f) && bokehSizeCurve(0.5f) < bokehSizeCurve(0.02f))
        assertTrue("the swell must be visible, not a rounding error",
            bokehSizeCurve(0f) / bokehSizeCurve(1f) > 1.5f)
        val s = sim(seed = 29)
        val envs = s.discs.map { it.sizeEnv }
        assertTrue("per-particle size envelope has no spread", envs.max() - envs.min() > 0.3f)
        assertTrue("size envelope out of band: ${envs.min()}..${envs.max()}",
            envs.min() >= 0.82f && envs.max() <= 0.82f + 0.42f)
    }

    @Test fun `(b) colour is a ramp indexed by life, never flat`() {
        assertTrue(BOKEH_RAMP.size >= 5)
        val s = sim(seed = 31)
        val p = plane(s, 2)[0]
        val life = p.life
        p.life = 1f
        val young = bokehRampIndex(p)
        p.life = 0f
        val aged = bokehRampIndex(p)
        p.life = life
        assertTrue("a disc must warm as it ages ($young → $aged)", aged > young)
        val seen = HashSet<Int>()
        for (f in 1..1000) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (q in s.discs) {
                val i = bokehRampIndex(q)
                assertTrue("ramp index out of range: $i", i >= 0 && i < BOKEH_RAMP.size)
                seen.add(i)
            }
        }
        assertTrue("the field is using only ${seen.size} ramp stops", seen.size >= 3)
    }

    @Test fun `(c) the sub-populations really are different populations`() {
        val s = sim(seed = 37, countScale = 2f)
        val gg = geo()
        val far = mean(plane(s, 0).map { bokehRadius(it, gg) })
        val near = mean(plane(s, 3).map { bokehRadius(it, gg) })
        assertTrue("near/far size ratio is only ${near / far}x — that is not depth", near / far > 20f)
    }

    // ── 5. The legibility budget — this is a backdrop behind chat text ──

    @Test fun `a LARGE disc is never brighter than the near-plane alpha cap`() {
        var largeSeen = 0
        for (v in viewports()) {
            val s = simFor(v, 41)
            for (f in 1..1000) {
                val now = f * 16.6667
                s.update(now, 1f / 60f, true)
                for (p in s.discs) {
                    if (bokehRadius(p, BOKEH_APPARENT) < BOKEH_LARGE_DISC_PX) continue
                    largeSeen++
                    val a = bokehAlpha(p, now)
                    assertTrue("${p.plane.id} disc at alpha $a on ${v[0]}x${v[1]}", a < BOKEH_NEAR_ALPHA_CAP)
                }
            }
        }
        assertTrue("nothing large was drawn — the assertion proved nothing", largeSeen > 0)
    }

    @Test fun `no disc — nor a three-deep stack — outshines the muted body text`() {
        val s = sim(seed = 43)
        var worst = 0.0
        var worstStack = 0.0
        for (f in 1..600) {
            val now = f * 16.6667
            s.update(now, 1f / 60f, true)
            for (p in s.discs) {
                if (bokehRadius(p, BOKEH_APPARENT) < BOKEH_LARGE_DISC_PX) continue
                val a = bokehAlpha(p, now)
                val c = BOKEH_RAMP[bokehRampIndex(p)]
                worst = max(worst, luminance(a * c[0], a * c[1], a * c[2]))
                val k = min(1f, a * 3f)               // three overlapping discs, additively
                worstStack = max(worstStack, luminance(k * c[0], k * c[1], k * c[2]))
            }
        }
        assertTrue("nothing large was drawn — the assertion proved nothing", worst > 0.0)
        assertTrue("a single disc (L=$worst) outshines --muted (L=$mutedL)", worst <= mutedL)
        assertTrue("a 3-stack (L=$worstStack) outshines --muted (L=$mutedL)", worstStack <= mutedL)
    }

    @Test fun `the whole field lays down less ink than the published budget`() {
        // Σ alpha × π r² as a fraction of the viewport. Deliberately conservative:
        // every sprite falls to zero well inside its nominal radius.
        var peak = 0.0
        for (v in viewports()) {
            val s = simFor(v, 47)
            val gg = geo(v[2])
            for (f in 1..1000) {
                val now = f * 16.6667
                s.update(now, 1f / 60f, true)
                var ink = 0.0
                for (p in s.discs) {
                    val r = bokehRadius(p, gg).toDouble()
                    ink += bokehAlpha(p, now) * Math.PI * r * r
                }
                peak = max(peak, ink / (v[0] * v[1]).toDouble())
            }
        }
        assertTrue("the field is invisible — the budget proved nothing", peak > 0.0005)
        assertTrue("peak ink ${peak * 100}% > budget ${BOKEH_INK_BUDGET * 100}%", peak <= BOKEH_INK_BUDGET)
    }

    @Test fun `alpha can never be pushed over the plane cap by envelope, breathe or gain`() {
        val s = sim(seed = 53)
        for (f in 1..400) {
            val now = f * 16.6667
            s.update(now, 1f / 60f, true)
            for (p in s.discs) {
                // An operator cranking every knob at once must not break the budget.
                val a = bokehAlpha(p, now, 4f)
                assertTrue("${p.plane.id} alpha $a > cap ${p.plane.alpha}", a >= 0f && a <= p.plane.alpha)
            }
        }
    }

    @Test fun `a disc fades in from zero and out to zero — it never pops`() {
        val s = sim(seed = 59)
        val p = s.discs[0]
        val life = p.life
        p.life = 1f
        assertEquals("born invisible", 0f, bokehAlpha(p, 0.0), 0f)
        p.life = 0.0001f
        assertTrue("dies invisible", bokehAlpha(p, 0.0) < p.plane.alpha * 0.01f)
        p.life = 0.6f
        assertTrue("visible in between", bokehAlpha(p, 0.0) > p.plane.alpha * 0.5f)
        p.life = life
    }

    // ── 6. Frame-rate independence's twin: the DPI law ──

    @Test fun `density changes resolution, never apparent geometry`() {
        // The same physical screen at 1x and 2x density: every spatial quantity must
        // come out exactly 2x in pixels, i.e. identical in apparent size. Population
        // is dp-derived, so it must NOT change.
        val lo = BokehSim(1f, seeded(61)).also { it.resize(width, height, 1f, FIELD_REFERENCE_DENSITY) }
        val hi = BokehSim(1f, seeded(61)).also { it.resize(width * 2f, height * 2f, 2f, FIELD_REFERENCE_DENSITY * 2f) }
        assertEquals("population is dp-derived, not px-derived", lo.discs.size, hi.discs.size)
        run(lo, 300); run(hi, 300)
        lo.discs.forEachIndexed { i, p ->
            val q = hi.discs[i]
            assertEquals("x must be exactly 2x at 2x density", p.x * 2f, q.x, 0.5f)
            assertEquals("y must be exactly 2x at 2x density", p.y * 2f, q.y, 0.5f)
            assertEquals("stored geometry is density-free", p.baseR, q.baseR, 1e-4f)
            assertEquals("…and so is the drift", p.vx, q.vx, 1e-4f)
            assertEquals("…and so is the life clock", p.life, q.life, 1e-4f)
        }
    }

    // ── 7. Containment, pooling, lifecycle, determinism ──

    @Test fun `nobody escapes the box and nothing goes non-finite`() {
        val s = sim(seed = 67)
        run(s, 1000)
        // A disc is recycled once it clears the left edge by its own drawn radius
        // × 1.35 + 8 px, and re-enters at the mirror of that on the right.
        val pad = 96f * geo() * 1.35f + 8f * geo() + 4f
        // The vertical loll spans at most 2 × amp / rate from where it spawned
        // (∫ amp·sin(rate·t + φ) dt), worst case the dust plane at max amplitude
        // and min rate — plus a little slack for the frame-wise integration.
        val bob = 2f * (11f * 1.4f) * geo() / 0.09f * 1.05f + 4f
        for (p in s.discs) {
            assertTrue("escaped horizontally at x=${p.x}", p.x > -pad && p.x < width + pad)
            assertTrue("wandered off vertically at y=${p.y}", p.y > -bob && p.y < height + bob)
            for (v in floatArrayOf(p.x, p.y, p.vx, p.life, p.baseR, p.sizeEnv, p.decay, p.bobAmp)) {
                assertTrue("a field went non-finite ($v)", v.isFinite())
            }
            assertTrue("life out of range: ${p.life}", p.life > 0f && p.life <= 1f)
        }
    }

    @Test fun `no NaN after 1000 frames of mixed frame times`() {
        val s = sim(seed = 71)
        val dts = floatArrayOf(1f / 60f, 1f / 120f, 1f / 30f, 2f, 1f / 144f, 0f)
        for (f in 1..1000) s.update(f * 16.6667, dts[f % dts.size], true)
        for (p in s.discs) {
            for (v in floatArrayOf(p.x, p.y, p.vx, p.life, p.baseR, p.sizeEnv, p.decay, p.bobAmp)) {
                assertTrue("${p.plane.id} went non-finite ($v)", v.isFinite())
            }
            assertTrue("life out of range: ${p.life}", p.life > 0f && p.life <= 1f)
            assertTrue(bokehAlpha(p, 1234.0).isFinite() && bokehRadius(p, geo()).isFinite())
        }
    }

    @Test fun `the hot path reuses its slots for the whole session`() {
        val s = sim(seed = 73)
        val ids = java.util.IdentityHashMap<BokehDisc, Boolean>()
        val original = s.discs.toList()
        for (p in original) ids[p] = true
        run(s, 1000)
        assertEquals("population must not drift", original.size, s.discs.size)
        assertTrue("a disc was allocated in the hot path", s.discs.all { ids.containsKey(it) })
        assertTrue("a recycled disc keeps its slot AND its plane",
            s.discs.withIndex().all { (i, p) -> p === original[i] })
    }

    @Test fun `a keystroke-frequency resize does not re-scatter the field, and rearm never blinks`() {
        val s = sim(seed = 79)
        run(s, 60)
        val before = s.discs.map { it.x }
        val slots = s.discs.toList()
        s.resize(width, height - 60f, scale, density)     // composer grew a line; same count bucket
        assertEquals("the field jumped on a resize", before, s.discs.map { it.x })
        assertEquals("the pool was rebuilt on a resize", slots, s.discs.toList())
        s.rearm()
        assertEquals("rearm blinked the field", before, s.discs.map { it.x })
        assertEquals(slots, s.discs.toList())
    }

    @Test fun `a viewport change resamples the field instead of stranding it`() {
        val s = sim(seed = 83)
        run(s, 120)
        s.resize(380f, 720f, scale, density)              // hard shrink (rotate)
        val pad = 96f * geo() * 1.35f + 8f * geo() + 4f
        for (p in s.discs) {
            assertTrue("a shrunk viewport stranded ${p.plane.id} at ${p.x},${p.y}",
                p.x > -pad && p.x < 380f + pad && p.y > -720f && p.y < 720f * 2f)
        }
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 89)
            run(s, 240)
            return s.discs.joinToString { "${it.x},${it.y},${it.life},${it.baseR},${it.vx}" }
        }
        assertEquals(once(), once())
    }

    /** (width, height, DPI scale) — a small phone, this reference phone, a tablet. */
    private fun viewports(): List<FloatArray> = listOf(
        floatArrayOf(1277f, 2836f, 1f),
        floatArrayOf(1080f, 2400f, 1f),
        floatArrayOf(2560f, 1600f, 2.0f / FIELD_REFERENCE_DENSITY),
    )
}
