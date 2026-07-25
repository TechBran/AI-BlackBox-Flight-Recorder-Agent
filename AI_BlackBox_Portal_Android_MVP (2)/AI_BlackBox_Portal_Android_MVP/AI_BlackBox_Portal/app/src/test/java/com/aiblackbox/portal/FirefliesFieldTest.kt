package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.FIREFLY_LAMP
import com.aiblackbox.portal.ui.components.FIREFLY_MAX_POP
import com.aiblackbox.portal.ui.components.FIREFLY_MIN_POP
import com.aiblackbox.portal.ui.components.FIREFLY_SPARK
import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.Firefly
import com.aiblackbox.portal.ui.components.FirefliesSim
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.fireflyLifeFade
import com.aiblackbox.portal.ui.components.fireflyPulse
import com.aiblackbox.portal.ui.components.fireflySizeOverLife
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * Fireflies physics — headless, seeded, no Compose / Android graphics.
 *
 * The sim takes its randomness from an injected [FieldRandom], so the whole
 * simulation is drivable from the JVM with a deterministic PRNG. What is asserted
 * here is what would otherwise only be caught by staring at a phone: that the
 * population stays in its bounds, that the PULSES NEVER SYNCHRONISE (the one rule
 * that makes or breaks the effect), that the field has no upward bias, that the
 * path curves instead of jittering, that nobody escapes, and that DENSITY NEVER
 * CHANGES APPARENT GEOMETRY.
 *
 * Mirrors Portal/modules/fx/effects/fireflies.test.mjs — keep them in step.
 */
class FirefliesFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
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

    private fun sim(seed: Int = 42, countScale: Float = 1f): FirefliesSim =
        FirefliesSim(countScale, seeded(seed)).also { it.resize(width, height, scale, density) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: FirefliesSim, frames: Int, dt: Float = 1f / 60f, active: Boolean = true) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, active)
    }

    /** Share of the population sitting in the busiest quarter of the pulse cycle.
     *  1.0 = the whole field breathes as one (the cheap-demo failure); 0.25 =
     *  perfectly spread. */
    private fun worstQuarterShare(list: List<Firefly>): Float {
        val q = IntArray(4)
        for (p in list) q[min(3, ((p.t / p.period) * 4f).toInt())]++
        return (q.max()).toFloat() / list.size
    }

    // ── 1. Population: the effect's entire premise ──

    @Test fun `population stays inside 15 to 40 across the whole count dial`() {
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val n = sim(countScale = cs).flies.size
            assertTrue("countScale $cs produced $n fireflies", n in FIREFLY_MIN_POP..FIREFLY_MAX_POP)
        }
        // …and on a very small and a very large canvas at the default dial.
        for (w in floatArrayOf(320f, 1080f, 3000f)) {
            val s = FirefliesSim(1f, seeded(7)).also { it.resize(w, w * 2f, scale, density) }
            assertTrue("a ${w}px canvas produced ${s.flies.size}", s.flies.size in FIREFLY_MIN_POP..FIREFLY_MAX_POP)
        }
    }

    @Test fun `both sub-populations exist even at the minimum population`() {
        val s = sim(seed = 8, countScale = ParticleTuning.COUNT_SCALE_MIN)   // clamps to MIN_POP
        assertEquals(FIREFLY_MIN_POP, s.flies.size)
        val sparks = s.flies.filter { it.spark }
        val lamps = s.flies.filter { !it.spark }
        assertEquals("every third slot is a spark — deterministic, not a coin flip", 5, sparks.size)
        assertEquals(10, lamps.size)
        // …and they are genuinely different animals, not one population resized.
        assertTrue("spark periods are all shorter", sparks.maxOf { it.period } < lamps.minOf { it.period })
        assertTrue("sparks blink, lamps breathe", sparks.maxOf { it.duty } < lamps.minOf { it.duty })
        assertTrue("sparks are faster", sparks.minOf { it.sp } > lamps.maxOf { it.sp })
        assertTrue("sparks are smaller on average", sparks.map { it.r0 }.average() < lamps.map { it.r0 }.average())
    }

    @Test fun `a resize grows the pool in place and strands nobody`() {
        val s = sim(seed = 4)
        val before = s.flies.toList()
        s.resize(width * 1.8f, height, scale, density)             // unfolding a Fold
        assertTrue("survivors keep their slot object (and their pulse)",
            s.flies.take(before.size).withIndex().all { (i, p) -> p === before[i] })
        s.resize(380f, 720f, scale, density)                       // then shrink hard
        for (p in s.flies) {
            assertTrue("a shrunk viewport strands nobody: ${p.x},${p.y}",
                p.x in 0f..380f && p.y in 0f..720f)
        }
    }

    // ── 2. THE rule: pulses desynchronised in BOTH period and phase ──

    @Test fun `every firefly gets its own period AND its own phase`() {
        val list = sim(seed = 33).flies
        assertEquals("a shared period beats back into sync",
            list.size, list.map { it.period }.toSet().size)
        assertEquals("a shared phase flashes in unison",
            list.size, list.map { it.t / it.period }.toSet().size)
        // Phases must be SPREAD, not merely distinct.
        val share = worstQuarterShare(list)
        assertTrue("${(share * 100).toInt()}% of the field starts in one quarter-cycle", share < 0.5f)
    }

    @Test fun `the field never flashes together — at any frame, over a full minute`() {
        val s = sim(seed = 19)
        var worst = 0f
        var worstAt = 0
        for (f in 1..3600) {                                        // 60 s at 60 Hz
            s.update(f * 16.6667, 1f / 60f, true)
            val hot = s.flies.count { fireflyPulse(it.t / it.period, it.duty) > 0.8f }
            val share = hot.toFloat() / s.flies.size
            if (share > worst) { worst = share; worstAt = f }
        }
        assertTrue("${(worst * 100).toInt()}% of the field peaked together at frame $worstAt", worst < 0.6f)
    }

    @Test fun `pulse is a flash with a real dark gap, and has no corners`() {
        assertEquals("past the duty cycle the spark is fully dark", 0f, fireflyPulse(0.5f, 0.3f), 0f)
        assertEquals(0f, fireflyPulse(0f, 0.5f), 0f)
        assertTrue("the attack reaches full brightness", fireflyPulse(0.28f * 0.5f, 0.5f) > 0.999f)
        var maxSlope = 0f
        var u = 0f
        while (u < 0.5f) {
            maxSlope = max(maxSlope, abs(fireflyPulse(u + 0.001f, 0.5f) - fireflyPulse(u, 0.5f)))
            u += 0.001f
        }
        assertTrue("pulse has a corner (max step $maxSlope)", maxSlope < 0.02f)
    }

    @Test fun `size and alpha are curves over life, never constant`() {
        assertEquals("a newborn firefly has no size yet", 0f, fireflySizeOverLife(1f), 1e-6f)
        assertTrue("mid-life sits at full size",
            fireflySizeOverLife(0.85f) > 0.95f && fireflySizeOverLife(0.5f) > 0.999f)
        assertTrue("an old firefly shrinks as it fades", fireflySizeOverLife(0.02f) < 0.7f)
        assertEquals("a dying slot fades out, it does not pop", 0f, fireflyLifeFade(0f), 1e-6f)
        assertEquals("…and it fades in at birth", 0f, fireflyLifeFade(1f), 1e-6f)
        assertTrue(fireflyLifeFade(0.5f) > 0.99f)
        // Every particle carries its OWN swell depth, over its kind's range.
        val swells = sim(seed = 52).flies.map { it.swell }
        assertEquals("swell must be per-particle", swells.size, swells.toSet().size)
        // Spans BOTH kinds' bands (lamp 0.18–0.42, spark 0.30–0.70) and nothing outside.
        assertTrue("swell out of band: ${swells.min()}..${swells.max()}",
            swells.min() >= min(FIREFLY_LAMP.swellMin, FIREFLY_SPARK.swellMin) &&
                swells.max() <= max(FIREFLY_LAMP.swellMax, FIREFLY_SPARK.swellMax))
    }

    // ── 3. Motion: no upward bias, noise steering (not a random walk) ──

    @Test fun `the field wanders with NO upward bias`() {
        val s = sim(seed = 21)
        val x0 = s.flies.map { it.x }
        val y0 = s.flies.map { it.y }
        run(s, 600)
        val moved = s.flies.indices.count { hypot(s.flies[it].x - x0[it], s.flies[it].y - y0[it]) > 2f }
        assertTrue("fireflies must drift; only $moved moved", moved > s.flies.size * 0.8)
        var dy = 0f
        var dist = 0f
        s.flies.forEachIndexed { i, p ->
            dy += p.y - y0[i]
            dist += hypot(p.x - x0[i], p.y - y0[i])
        }
        val bias = abs(dy) / dist
        assertTrue("net vertical drift is a bias, not a wander (|dy|/dist = $bias)", bias < 0.45f)
    }

    @Test fun `heading is STEERED by a low-frequency field, not re-rolled every frame`() {
        // A random walk decorrelates: its frame-to-frame heading change is
        // unbounded and its path length dwarfs its net displacement. Steering
        // bounds the turn to (turn rate × urgency × dt) and keeps the flight
        // curved rather than scribbled.
        val s = sim(seed = 15)
        val n = s.flies.size
        val prevH = FloatArray(n) { s.flies[it].h }
        val prevL = FloatArray(n) { s.flies[it].life }
        val px = FloatArray(n) { s.flies[it].x }
        val py = FloatArray(n) { s.flies[it].y }
        val sx = px.copyOf()
        val sy = py.copyOf()
        val path = FloatArray(n)
        var maxTurn = 0f
        var sumTurn = 0f
        var turns = 0
        var walls = 0
        for (f in 1..600) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (i in 0 until n) {
                val p = s.flies[i]
                if (p.life <= prevL[i]) {                       // skip the frame a slot recycled on
                    val d = abs(((p.h - prevH[i] + Math.PI.toFloat() * 3f) % (Math.PI.toFloat() * 2f)) - Math.PI.toFloat())
                    maxTurn = max(maxTurn, d); sumTurn += d; turns++
                    path[i] += hypot(p.x - px[i], p.y - py[i])
                } else {
                    sx[i] = p.x; sy[i] = p.y; path[i] = 0f
                }
                if (p.x == 0f || p.x == width || p.y == 0f || p.y == height) walls++
                prevH[i] = p.h; prevL[i] = p.life; px[i] = p.x; py[i] = p.y
            }
        }
        assertTrue("worst single-frame turn $maxTurn rad exceeds the steering law's bound", maxTurn < 0.35f)
        assertTrue("mean turn ${sumTurn / turns} rad/frame reads as jitter", sumTurn / turns < 0.01f)
        assertEquals("the containment backstop fired — a visible bounce off the edge", 0, walls)
        val straight = s.flies.mapIndexed { i, p ->
            if (path[i] > 20f) hypot(p.x - sx[i], p.y - sy[i]) / path[i] else 1f
        }.sorted()
        val median = straight[straight.size / 2]
        assertTrue("flight must curve, not scribble (median net/path = $median)", median > 0.3f)
    }

    @Test fun `nobody escapes the viewport and nothing goes non-finite`() {
        val s = sim(seed = 77)
        run(s, 1000)
        for (p in s.flies) {
            assertTrue("escaped horizontally at x=${p.x}", p.x in 0f..width)
            assertTrue("escaped vertically at y=${p.y}", p.y in 0f..height)
            for (v in floatArrayOf(p.x, p.y, p.h, p.sp, p.t, p.period, p.life, p.r0, p.swell)) {
                assertTrue("a field went non-finite ($v)", v.isFinite())
            }
            assertTrue("life out of range: ${p.life}", p.life > 0f && p.life <= 1f)
            assertTrue("pulse phase out of range: ${p.t}", p.t >= 0f && p.t < p.period)
        }
    }

    // ── 4. Frame-rate independence and the DPI law ──

    @Test fun `60 Hz and 120 Hz travel the same path`() {
        val a = sim(seed = 3).also { run(it, 120, 1f / 60f) }.flies.map { Triple(it.x, it.y, it.t) }
        val b = sim(seed = 3).also { run(it, 240, 1f / 120f) }.flies.map { Triple(it.x, it.y, it.t) }
        var worstPos = 0f
        var worstPhase = 0f
        a.forEachIndexed { i, p ->
            worstPos = max(worstPos, hypot(p.first - b[i].first, p.second - b[i].second))
            worstPhase = max(worstPhase, abs(p.third - b[i].third))
        }
        assertTrue("a 120 Hz screen drifted ${worstPos}px from a 60 Hz one in 2 s", worstPos < 1.5f)
        assertTrue("the pulse clock is not delta-timed ($worstPhase)", worstPhase < 1e-3f)
    }

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1x and 2x density: every
        // spatial quantity must come out exactly 2x in pixels, i.e. identical in
        // apparent size. Population is dp-derived, so it must NOT change.
        val lo = FirefliesSim(1f, seeded(88)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = FirefliesSim(1f, seeded(88)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("population is dp-derived, not px-derived", lo.flies.size, hi.flies.size)
        run(lo, 300); run(hi, 300)
        lo.flies.forEachIndexed { i, p ->
            val q = hi.flies[i]
            assertEquals("x must be exactly 2x at 2x density", p.x * 2f, q.x, 0.5f)
            assertEquals("y must be exactly 2x at 2x density", p.y * 2f, q.y, 0.5f)
            assertEquals("stored geometry is density-free", p.r0, q.r0, 1e-4f)
            assertEquals("…and so is the pulse", p.t, q.t, 1e-4f)
        }
    }

    // ── 5. Lifecycle + determinism ──

    @Test fun `the field thins out when idle and revives — unsynced — on rearm`() {
        val s = sim(seed = 31)
        val n = s.flies.size
        for (f in 1..3600) s.update(f * 16.6667, 1f / 60f, false)
        assertTrue("an idle field must stop recycling, not loop forever", s.flies.count { it.dead } > 0)
        assertEquals("the pool is fixed-size: slots go dark, they are not removed", n, s.flies.size)
        s.rearm()
        assertEquals("rearm revives every dark slot", 0, s.flies.count { it.dead })
        val phases = s.flies.map { it.t / it.period }
        assertEquals("a rearm must not hand the field a common rhythm", phases.size, phases.toSet().size)
        assertEquals("…nor a common period", phases.size, s.flies.map { it.period }.toSet().size)
        assertTrue("a revived field shares a quarter-cycle", worstQuarterShare(s.flies) < 0.5f)
    }

    @Test fun `the hot path reuses its slots for the whole session`() {
        val s = sim(seed = 45)
        val ids = s.flies.toList()
        run(s, 3600)
        assertTrue("a recycled firefly reuses its slot object",
            s.flies.withIndex().all { (i, p) -> p === ids[i] })
        assertTrue("recycling must not reshuffle the sub-populations",
            s.flies.withIndex().all { (i, p) -> p.spark == (i % 3 == 1) })
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 99)
            run(s, 240)
            return s.flies.joinToString { "${it.x},${it.y},${it.t},${it.life},${it.h}" }
        }
        assertEquals(once(), once())
    }

    @Test fun `the kinds differ on every visible axis`() {
        assertTrue("sparks blink faster", FIREFLY_SPARK.periodMax < FIREFLY_LAMP.periodMin)
        assertTrue("sparks have a real dark gap", FIREFLY_SPARK.floorMax < FIREFLY_LAMP.floorMin)
        assertTrue("lamps never go fully dark", FIREFLY_LAMP.floorMin > 0f)
        assertTrue("sparks turn harder", FIREFLY_SPARK.turn > FIREFLY_LAMP.turn)
        assertTrue("sparks read cooler/whiter", FIREFLY_SPARK.hue0 < FIREFLY_LAMP.hue0)
        for (k in listOf(FIREFLY_LAMP, FIREFLY_SPARK)) {
            assertTrue("ramp index must stay inside the 6-stop atlas", k.hue0 >= 0f && k.hue1 <= 5f)
            assertTrue("must traverse the ramp, not sit on one stop", k.hue1 - k.hue0 > 1.5f)
        }
    }
}
