package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.METEOR_AGE_SHIFT
import com.aiblackbox.portal.ui.components.METEOR_CENTRE_BOX
import com.aiblackbox.portal.ui.components.METEOR_CENTRE_DIM
import com.aiblackbox.portal.ui.components.METEOR_DUST
import com.aiblackbox.portal.ui.components.METEOR_FIREBALL
import com.aiblackbox.portal.ui.components.METEOR_FIREBALL_CHANCE
import com.aiblackbox.portal.ui.components.METEOR_FIRST_MIN
import com.aiblackbox.portal.ui.components.METEOR_HEAD_R
import com.aiblackbox.portal.ui.components.METEOR_LIFE_MAX
import com.aiblackbox.portal.ui.components.METEOR_MIN_HEAD_PX
import com.aiblackbox.portal.ui.components.METEOR_POOL
import com.aiblackbox.portal.ui.components.METEOR_RAMP
import com.aiblackbox.portal.ui.components.METEOR_RAMP_STEPS
import com.aiblackbox.portal.ui.components.METEOR_STAR_COUNT
import com.aiblackbox.portal.ui.components.METEOR_STAR_FAR
import com.aiblackbox.portal.ui.components.METEOR_STAR_MAX
import com.aiblackbox.portal.ui.components.METEOR_STAR_MIN
import com.aiblackbox.portal.ui.components.METEOR_STAR_NEAR
import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.Meteor
import com.aiblackbox.portal.ui.components.MeteorsSim
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.meteorEnvelope
import com.aiblackbox.portal.ui.components.meteorRampIndex
import com.aiblackbox.portal.ui.components.meteorRampRgb
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Meteor Fall physics — headless, seeded, no Compose / Android graphics.
 *
 * The sim takes its randomness from an injected [FieldRandom], so the whole
 * simulation — including THE SCHEDULE — is drivable from the JVM with a
 * deterministic PRNG. The invariants below are in order of how badly a regression
 * would hurt:
 *
 *   1. THE LEGIBILITY BUDGET — nothing is ever drawn inside the centre 40% of the
 *      width, and stars inside the reading box are dimmed to ≤ 0.20 alpha. Both
 *      are asserted from the DRAWN geometry (position + envelope + width), not
 *      from the config, so a draw-side mistake is caught.
 *   2. THE SCHEDULE CORRELATES WITH NOTHING — identical spawn times whether the
 *      machine is idle or generating, and rearm() (the one hook that fires on
 *      activation) never pulls an event forward.
 *   3. THE RATE — a meteor lands every few seconds (so a phone glance sees one)
 *      and the gaps still dominate (so it is punctuation, not a curtain).
 *   4. delta-timing, the DPI law, a fixed pool, and no NaN.
 *
 * Mirrors Portal/modules/fx/effects/meteors.test.mjs, EXCEPT where the Fold 6
 * device pass (2026-07-25) deliberately diverged — the schedule and the stroke
 * width. Those two are re-expressed as properties rather than as the web's
 * literals, each with the reason inline; everything else stays in step.
 */
class MeteorsFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
    private val scale = 1f

    /** web CSS px → device px at the reference density (0.5 × 3.1) — the factor
     *  MeteorsField.kt authors its whole tuning table in. */
    private val gs = 1.55f

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

    private fun sim(seed: Int = 42, countScale: Float = 1f): MeteorsSim =
        MeteorsSim(countScale, seeded(seed)).also { it.resize(width, height, scale, density) }

    // -------------------------------------------------------------------------
    // Streak tracking. Meteors are POOLED, so object identity is NOT streak
    // identity — a slot is reused by the next streak. Records are opened on the
    // dead→alive edge AND on a life reset inside a still-"alive" slot (a spawn can
    // land on the frame after a death), which is the only correct way to count them.
    // -------------------------------------------------------------------------
    private class Streak(
        val kind: Int,
        val speed: Float,
        val tail: Float,
        val envMul: Float,
        val born: Double,
    ) {
        var dur = 0f
        var widest = 0f
    }

    private class RunResult(val streaks: List<Streak>, val frames: Int, val busyFrames: Int) {
        val duty: Float get() = busyFrames.toFloat() / frames
        val births: List<Double> get() = streaks.map { it.born }
    }

    /** Run `secs` of simulation, exactly as EmberOverlay's loop drives it. */
    private fun run(s: MeteorsSim, secs: Float, dt: Float = 1f / 60f, active: Boolean = true): RunResult {
        val pool = s.meteors                                  // stable view: the array is never re-allocated
        val streaks = ArrayList<Streak>()
        val open = HashMap<Meteor, Streak>()
        val prevLife = FloatArray(pool.size)
        var busy = 0
        var frames = 0
        val total = (secs / dt).toInt()
        for (f in 1..total) {
            s.update(0.0, dt, active)
            frames++
            var live = 0
            for (i in pool.indices) {
                val m = pool[i]
                if (!m.alive) { open.remove(m); prevLife[i] = 0f; continue }
                live++
                val existing = open[m]
                val rec: Streak
                if (existing == null || m.life > prevLife[i]) {   // fresh streak in a reused slot
                    rec = Streak(m.kind, m.speed, m.tail, m.envMul, s.clockSec)
                    open[m] = rec
                    streaks.add(rec)
                } else {
                    rec = existing
                }
                rec.dur += dt
                rec.widest = max(rec.widest, m.width * meteorEnvelope(1f - m.life) * m.envMul)
                prevLife[i] = m.life
            }
            if (live > 0) busy++
        }
        return RunResult(streaks, frames, busy)
    }

    /** Everything the sim owns, as one string — for the determinism tests. */
    private fun dump(s: MeteorsSim): String = buildString {
        append(s.clockSec).append('|').append(s.nextAtSec).append('|').append(s.spawned).append('|')
        for (m in s.meteors) append("${m.alive},${m.x},${m.y},${m.life},${m.speed},${m.tail};")
        for (st in s.sky) append("${st.x},${st.y},${st.k},${st.r};")
    }

    private fun assertFinite(s: MeteorsSim, where: String) {
        assertTrue("$where: clock", s.clockSec.isFinite())
        assertTrue("$where: nextAt", s.nextAtSec.isFinite())
        for (m in s.meteors) {
            for (v in floatArrayOf(m.x, m.y, m.ux, m.uy, m.speed, m.tail, m.life, m.decay, m.envMul, m.width, m.alpha, m.ramp0)) {
                assertTrue("$where: a meteor field went non-finite ($v)", v.isFinite())
            }
        }
        for (st in s.sky) {
            for (v in floatArrayOf(st.x, st.y, st.r, st.a, st.vx, st.tw, st.tws, st.envMul, st.tone, st.k)) {
                assertTrue("$where: a star field went non-finite ($v)", v.isFinite())
            }
        }
    }

    // ── 1. Population: sparse, pooled, never growing ──

    @Test fun `the meteor pool is fixed at 8 and the live count stays sparse`() {
        val s = sim(seed = 11)
        assertEquals(METEOR_POOL, s.meteors.size)
        val pool = s.meteors
        var peak = 0
        for (f in 1..18000) {                                 // 300 s
            s.update(0.0, 1f / 60f, true)
            assertEquals("the pool must never grow at runtime", METEOR_POOL, s.meteors.size)
            peak = max(peak, pool.count { it.alive })
        }
        assertTrue("no streak ever spawned in 300 s", peak >= 1)
        // WAS: peak <= 5, at the web's ~19 s mean gap. The gap is now ~3.4 s
        // (2026-07-25 Fold pass — see `a meteor lands every few seconds…`), and a
        // shower still fires 3-4 streaks back to back at 0.35-1.20 s spacing, so a
        // handful can legitimately overlap. The PROPERTY is unchanged and is what
        // this bound protects: the field must never SATURATE its 8-slot pool into a
        // continuous curtain. Emptiness is asserted directly by the duty cycle test.
        assertTrue("a meteor field must never saturate into a curtain; peaked at $peak", peak <= 6)
    }

    @Test fun `the starfield is a low, viewport-independent count with two layers`() {
        val s = sim()
        assertEquals(METEOR_STAR_COUNT, s.sky.size)
        // A sky, not a field: a 3x bigger canvas gets the SAME number of stars.
        val big = MeteorsSim(1f, seeded(4)).also { it.resize(3000f, 3000f, scale, density) }
        assertEquals("star count must not track viewport area", METEOR_STAR_COUNT, big.sky.size)
        // Only the intensity × quality dial moves it, clamped hard at both ends.
        assertEquals(METEOR_STAR_MIN, sim(countScale = ParticleTuning.COUNT_SCALE_MIN).sky.size)
        assertEquals(METEOR_STAR_MAX, sim(countScale = ParticleTuning.COUNT_SCALE_MAX).sky.size)
        // Two sub-populations, index-derived so both exist at ANY count.
        assertEquals("far dust is the bulk of the sky", 30, s.sky.count { it.far })
        assertEquals(14, s.sky.count { !it.far })
        assertTrue("the layers must not overlap in size",
            s.sky.filter { it.far }.maxOf { it.r } <= METEOR_STAR_FAR.rMax &&
                s.sky.filter { !it.far }.minOf { it.r } >= METEOR_STAR_NEAR.rMin)
        val minimal = sim(seed = 8, countScale = ParticleTuning.COUNT_SCALE_MIN)
        assertTrue("both layers must survive the minimum sky",
            minimal.sky.any { it.far } && minimal.sky.any { !it.far })
    }

    // ── 2. THE LEGIBILITY BUDGET (asserted against the drawn geometry) ──

    @Test fun `no streak is ever drawn inside the centre 40 percent of the width`() {
        val lo = METEOR_CENTRE_BOX * width
        val hi = width - lo
        var checked = 0
        for (seed in intArrayOf(1, 7, 42, 1234, 90210)) {
            val s = MeteorsSim(1f, seeded(seed)).also { it.resize(width, height, scale, density) }
            val pool = s.meteors
            for (f in 1..12000) {                             // 200 s per seed
                s.update(0.0, 1f / 60f, true)
                for (m in pool) {
                    if (!m.alive) continue
                    // Exactly what render() draws: half-width from the size-over-life
                    // envelope, the head core disc, and the tail reaching back inward.
                    // The head constants come from the effect, not from literals, so
                    // the Fold-6 width uplift (2026-07-25) is measured here rather
                    // than assumed away — the streaks are now ~2x thicker and this is
                    // the assertion that proves the extra light still lands outside
                    // the reading zone (spawnMeteor subtracts a head guard from the
                    // x cap for exactly this reason).
                    val hw = m.width * meteorEnvelope(1f - m.life) * m.envMul
                    val headR = max(METEOR_MIN_HEAD_PX, hw * METEOR_HEAD_R)
                    val tailX = m.x - m.ux * m.tail
                    for (x in floatArrayOf(m.x - headR, m.x + headR, tailX - hw / 2f, tailX + hw / 2f)) {
                        assertTrue("seed $seed: streak pixel at x=$x is in the reading zone", x <= lo || x >= hi)
                    }
                    checked++
                }
            }
        }
        assertTrue("only $checked streak samples — the assertion was near-vacuous", checked > 200)
    }

    @Test fun `stars inside the reading box are dimmed to a backdrop level`() {
        val s = sim(seed = 5)
        val sky = s.sky
        val bx0 = METEOR_CENTRE_BOX * width
        val by0 = METEOR_CENTRE_BOX * height
        var inside = 0
        var peakInside = 0f
        var peakOutside = 0f
        for (f in 1..1200) {
            s.update(0.0, 1f / 60f, true)
            for (st in sky) {
                val inBox = st.x > bx0 && st.x < width - bx0 && st.y > by0 && st.y < height - by0
                val al = st.a * (0.55f + 0.45f * st.k) * (if (inBox) METEOR_CENTRE_DIM else 1f)
                if (inBox) { inside++; peakInside = max(peakInside, al) } else peakOutside = max(peakOutside, al)
            }
        }
        assertTrue("the starfield does cover the centre — it is just quiet there", inside > 0)
        assertTrue("centre-box star alpha peaked at $peakInside; the budget is 0.20", peakInside <= 0.20f)
        assertTrue("stars outside the reading box are allowed to be brighter", peakOutside > peakInside)
    }

    @Test fun `streaks are thin and brief — the two halves of 'you barely saw it'`() {
        val r = run(sim(seed = 3), 300f)                      // 5 minutes
        assertTrue("only ${r.streaks.size} streaks sampled", r.streaks.size >= 8)
        // WAS: widest <= 3.2 × gs, mirroring the web's 3.2-CSS-px pin. That literal
        // was raised on 2026-07-25 because it was pinning a stroke that had already
        // lost: web widths × apparentScale 0.5 × density 3.1 put the taper under one
        // DEVICE pixel, where an antialiased line dissolves into grey noise before
        // alpha is even applied ("I can't really see anything happening there").
        // So assert the PROPERTY the literal was protecting instead of the literal:
        // the head is still a HAIRLINE IN PHYSICAL TERMS — under 3 dp of stroke on
        // any screen — which is a bright thread, never a bar of light. (Same result
        // as the web, different number: the cross-surface contract is the apparent
        // look, not the constant.)
        val widestDp = r.streaks.maxOf { it.widest } / density
        assertTrue("head width $widestDp dp is not a hairline", widestDp <= 3.0f)
        val longest = r.streaks.maxOf { it.dur }
        assertTrue("a streak lingered $longest s", longest <= METEOR_LIFE_MAX + 0.05f)
        // The web asserts 0.1 s on its 1400-CSS-px box; a phone is narrower in web
        // px, so a fast dust streak clears the edge sooner. Still not a flicker.
        assertTrue("a streak shorter than a blink is a flicker, not a meteor",
            r.streaks.all { it.dur > 0.08f })
    }

    // ── 3. THE TIMING — long silences, correlated with nothing ──

    @Test fun `a meteor lands every few seconds, and the gaps still dominate`() {
        val long = run(sim(seed = 21), 900f)                  // 15 minutes
        assertTrue("only ${long.births.size} streaks in 900 s", long.births.size >= 20)
        // WAS: duty < 0.15, mean gap >= 8 s, longest gap >= 10 s — the web schedule
        // ([16, 34] s, ~19 s mean). Re-tuned on 2026-07-25 after the Fold 6 pass:
        // "I can't really see anything happening there… make that one more
        // animated." At a 19 s mean gap the MEDIAN PHONE GLANCE CONTAINS NO METEOR,
        // so the effect reads as broken rather than as sparse. The bounds below now
        // assert the two halves of the character that actually matter, and the rate
        // sits between them:
        //   • it is an EVENT, not a stream  — the screen is empty most of the time,
        //     and every gap is many times a streak's own ~0.4 s life;
        //   • it is VISIBLE ON A GLANCE     — several land in the first minute.
        assertTrue("a streak was on screen ${(long.duty * 100).toInt()}% of frames — that is a stream, not an event",
            long.duty < 0.30f)
        assertTrue("the field opens quiet, not with a streak", long.births[0] >= METEOR_FIRST_MIN)
        // BRANDON'S ASK, PINNED. A phone is glanced at for a few seconds; the first
        // minute must contain several streaks or the effect is invisible in practice.
        val minute = run(sim(seed = 21), 60f).births
        assertTrue("only ${minute.size} streaks in the first minute — a glance would see none",
            minute.size >= 8)
        // Pool the gaps across three seeds: showers are a 16% event, so a single
        // 15-minute sample is a thin basis for "showers exist".
        val gaps = ArrayList<Double>()
        fun collect(r: RunResult) {
            val b = r.births
            for (i in 1 until b.size) gaps.add(b[i] - b[i - 1])
        }
        collect(long); collect(run(sim(seed = 5), 300f)); collect(run(sim(seed = 8), 300f))
        assertTrue("there must be genuinely long silences, ${gaps.max()} s is not one", gaps.max() >= 5.0)
        assertTrue("mean gap ${gaps.average()} s is a stream, not punctuation", gaps.average() >= 2.0)
        assertTrue("mean gap ${gaps.average()} s is back to 'nothing is happening'", gaps.average() <= 6.0)
        assertTrue("showers exist: sometimes two arrive together", gaps.any { it < 2.0 })
    }

    @Test fun `the schedule reads NO engine signal — idle and busy fire identically`() {
        val idle = run(sim(seed = 77), 300f, active = false).births
        val busy = run(sim(seed = 77), 300f, active = true).births
        assertTrue("no streak fired at all", idle.isNotEmpty())
        assertEquals("spawn timing correlated with `active` — it would read as a status light",
            idle, busy)
    }

    @Test fun `the schedule ignores nowMs — the clock is private and delta-driven`() {
        // A parked frame loop resumes with a huge timestamp jump. If anything read
        // nowMs, the whole schedule would teleport.
        val a = sim(seed = 66)
        val b = sim(seed = 66)
        for (f in 1..3600) {
            a.update(0.0, 1f / 60f, true)
            b.update(f * 987654.0, 1f / 60f, true)
        }
        assertEquals(dump(a), dump(b))
    }

    @Test fun `rearm — the one hook that fires on activation — never pulls a streak forward`() {
        val s = sim(seed = 9)
        run(s, 4f)
        val nextAt = s.nextAtSec
        val clock = s.clockSec
        val spawned = s.spawned
        val sky = s.sky.size
        repeat(50) { s.rearm() }
        assertEquals("rearm rescheduled the next streak", nextAt, s.nextAtSec, 0.0)
        assertEquals("rearm moved the clock", clock, s.clockSec, 0.0)
        assertEquals("rearm spawned a streak on activation", spawned, s.spawned)
        assertEquals("the starfield survives rearm", sky, s.sky.size)
    }

    // ── 4. Motion ──

    @Test fun `streaks fall diagonally and always away from the centre`() {
        val s = sim(seed = 31)
        val pool = s.meteors
        val cx = width * 0.5f
        val px = FloatArray(pool.size)
        val py = FloatArray(pool.size)
        val pl = FloatArray(pool.size)
        val was = BooleanArray(pool.size)
        var checked = 0
        // 400 s, where meteors.test.mjs samples 200. Meteor Fall is still SPARSE —
        // an 8-slot pool and ~9 frames in 10 with nothing alive — and a phone is far
        // narrower in web px than the web test's 1400-CSS-px box, so a streak
        // reaches the edge cull in ~0.26 s instead of ~0.38 s and contributes ~35%
        // fewer per-frame samples. Same threshold as the web (> 200 samples),
        // collected over a window sized for this geometry. (The window is kept as-is
        // after the 2026-07-25 rate uplift: it now oversamples, which only makes the
        // assertion stronger.)
        for (f in 1..24000) {
            for (i in pool.indices) {
                was[i] = pool[i].alive; px[i] = pool[i].x; py[i] = pool[i].y; pl[i] = pool[i].life
            }
            s.update(0.0, 1f / 60f, true)
            for (i in pool.indices) {
                val m = pool[i]
                if (!was[i] || !m.alive || m.life > pl[i]) continue   // skip births/recycles
                assertTrue("a meteor must fall", m.y > py[i])
                assertTrue("a meteor must travel outward, not into the text",
                    abs(m.x - cx) > abs(px[i] - cx))
                assertTrue("diagonal, not vertical rain", abs(m.x - px[i]) > 1e-6f)
                checked++
            }
        }
        assertTrue("only $checked motion samples", checked > 200)
    }

    @Test fun `tail length encodes speed, and both sub-populations occur`() {
        val r = run(sim(seed = 63), 900f)                     // 15 minutes
        assertTrue("only ${r.streaks.size} distinct streaks sampled", r.streaks.size >= 20)
        for (st in r.streaks) {
            assertTrue("a streak with no tail is a dot", st.tail > 0f)
            val k = st.tail / st.speed                        // tail = v × k, capped by the edge budget
            val kind = if (st.kind == 1) METEOR_FIREBALL else METEOR_DUST
            assertTrue("tail/speed $k out of band for kind ${st.kind}", k <= kind.tailKMax + 1e-6f)
        }
        val dust = r.streaks.filter { it.kind == 0 }
        val fire = r.streaks.filter { it.kind == 1 }
        assertTrue("both sub-populations must occur", dust.isNotEmpty() && fire.isNotEmpty())
        assertTrue("fireballs are the rare one", fire.size < dust.size)
        assertTrue("hairline dust is the faster population",
            dust.map { it.speed.toDouble() }.average() > fire.map { it.speed.toDouble() }.average())
    }

    @Test fun `the size-over-life envelope rises, peaks mid-flight and closes at zero`() {
        assertEquals("no pop-in at birth", 0f, meteorEnvelope(0f), 0f)
        assertEquals("no cut-off at death", 0f, meteorEnvelope(1f), 0f)
        var peak = 0f
        var peakT = 0f
        var t = 0f
        while (t <= 1f) {
            val e = meteorEnvelope(t)
            if (e > peak) { peak = e; peakT = t }
            t += 0.001f
        }
        assertTrue("envelope peaks at t=$peakT — it should attack then taper", peakT > 0.05f && peakT < 0.4f)
        assertTrue("no pop-in at birth", meteorEnvelope(0.02f) < peak * 0.35f)
        assertTrue("no cut-off at death", meteorEnvelope(0.95f) < peak * 0.15f)
        // …and every streak carries its OWN depth (0.82..1.24) over that curve, so
        // no two streaks are the same brightness ramp and none is a constant bar.
        // 60 s, not 300: at the 2026-07-25 rate that is already ~17 streaks, and a
        // strict all-distinct assertion on a 24-bit float gets birthday-flaky once
        // the sample runs into the hundreds. Same property, tighter sample.
        val muls = run(sim(seed = 52), 60f).streaks.map { it.envMul }
        assertTrue("only ${muls.size} streaks to judge", muls.size >= 8)
        assertEquals("the envelope depth must be per-particle", muls.size, muls.toSet().size)
        assertTrue("envelope depth out of band: ${muls.min()}..${muls.max()}",
            muls.min() >= 0.82f && muls.max() <= 1.24f)
    }

    @Test fun `the colour ramp is life-indexed and stays inside the LUT`() {
        assertEquals(0, meteorRampIndex(-1f))
        assertEquals(METEOR_RAMP_STEPS - 1, meteorRampIndex(2f))
        assertEquals("white-hot at the head", 0xFFFFFF, meteorRampRgb(METEOR_RAMP, 0f))
        assertEquals("deep ember at the far end", (188 shl 16) or (58 shl 8) or 20,
            meteorRampRgb(METEOR_RAMP, 1f))
        for (k in listOf(METEOR_DUST, METEOR_FIREBALL)) {
            assertTrue("a streak must MOVE along the ramp as it ages",
                meteorRampIndex(k.ramp0Min + METEOR_AGE_SHIFT) > meteorRampIndex(k.ramp0Min))
            assertTrue("the tail must traverse the ramp, not sit on one stop", k.span > 0.5f)
            assertTrue("the ramp start must stay inside the LUT", k.ramp0Min >= 0f && k.ramp0Max <= 1f)
        }
    }

    // ── 5. Frame-rate independence and the DPI law ──

    @Test fun `motion is delta-timed exactly - displacement equals velocity times dt`() {
        val s = sim(seed = 12)
        val pool = s.meteors
        val px = FloatArray(pool.size)
        val py = FloatArray(pool.size)
        val pl = FloatArray(pool.size)
        val was = BooleanArray(pool.size)
        var checks = 0
        var f = 0
        while (f < 12000 && checks < 40) {
            f++
            val dt = if (f % 2 == 0) 1f / 120f else 1f / 60f   // alternate refresh rates mid-flight
            for (i in pool.indices) {
                was[i] = pool[i].alive; px[i] = pool[i].x; py[i] = pool[i].y; pl[i] = pool[i].life
            }
            s.update(0.0, dt, true)
            for (i in pool.indices) {
                val m = pool[i]
                if (!was[i] || !m.alive || m.life > pl[i]) continue
                assertEquals("x is not dt-scaled", m.ux * m.speed * dt, m.x - px[i], 0.02f)
                assertEquals("y is not dt-scaled", m.uy * m.speed * dt, m.y - py[i], 0.02f)
                assertEquals("life is not dt-scaled", m.decay * dt, pl[i] - m.life, 1e-4f)
                checks++
            }
        }
        assertTrue("only $checks dt samples", checks >= 40)
    }

    @Test fun `60 Hz and 120 Hz reach the same state after the same wall time`() {
        val a = sim(seed = 55)
        val b = sim(seed = 55)
        repeat(2400) { a.update(0.0, 1f / 60f, true) }         // 40 s
        repeat(4800) { b.update(0.0, 1f / 120f, true) }        // …the same 40 s
        assertEquals("the private clock drifted between refresh rates", a.clockSec, b.clockSec, 1e-3)
        assertEquals("a 120 Hz display must not see more meteors", a.spawned, b.spawned)
        val span = width + 4f * gs                             // the star wrap period
        a.sky.forEachIndexed { i, st ->
            val q = b.sky[i]
            val d = abs(st.x - q.x) % span
            assertTrue("star $i drifted ${min(d, span - d)} px faster at 120 Hz", min(d, span - d) < 0.5f)
            assertEquals("twinkle is frame-rate dependent", st.k, q.k, 1e-3f)
        }
    }

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1x and 2x density: every spatial
        // quantity must come out exactly 2x in pixels, i.e. identical in apparent
        // size. Counts and the schedule are density-free, so they must NOT change.
        val lo = MeteorsSim(1f, seeded(88)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = MeteorsSim(1f, seeded(88)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("the sky is dial-derived, not px-derived", lo.sky.size, hi.sky.size)
        var checked = 0
        for (f in 1..1800) {                                   // 30 s — past the opening silence
            lo.update(0.0, 1f / 60f, true)
            hi.update(0.0, 1f / 60f, true)
            lo.meteors.forEachIndexed { i, m ->
                val q = hi.meteors[i]
                assertEquals("a streak must exist on both screens", m.alive, q.alive)
                if (!m.alive) return@forEachIndexed
                assertEquals("x must be exactly 2x at 2x density", m.x * 2f, q.x, 0.5f)
                assertEquals("y must be exactly 2x at 2x density", m.y * 2f, q.y, 0.5f)
                assertEquals("speed must be exactly 2x", m.speed * 2f, q.speed, 0.5f)
                assertEquals("the tail must be exactly 2x", m.tail * 2f, q.tail, 0.5f)
                assertEquals("the stroke must be exactly 2x", m.width * 2f, q.width, 1e-3f)
                assertEquals("lifetime is density-free", m.life, q.life, 1e-4f)
                checked++
            }
        }
        assertTrue("no streak flew in 30 s — the meteor half was vacuous", checked > 0)
        assertEquals("the schedule is density-free", lo.spawned, hi.spawned)
        lo.sky.forEachIndexed { i, st ->
            val q = hi.sky[i]
            assertEquals("star x must be exactly 2x", st.x * 2f, q.x, 0.5f)
            assertEquals("star y must be exactly 2x", st.y * 2f, q.y, 0.5f)
            assertEquals("stored geometry is density-free", st.r, q.r, 1e-6f)
            assertEquals("…and so is the drift", st.vx, q.vx, 1e-6f)
            assertEquals("…and so is the twinkle", st.k, q.k, 1e-6f)
        }
    }

    // ── 6. Lifecycle, robustness, determinism ──

    @Test fun `resize keeps the starfield and retires in-flight streaks`() {
        val s = sim(seed = 64)
        val pool = s.meteors
        var guard = 0
        while (pool.none { it.alive } && guard++ < 6000) s.update(0.0, 1f / 60f, true)
        assertTrue("no streak flew in 100 s", pool.any { it.alive })
        val nextAt = s.nextAtSec
        s.resize(700f, 1200f, scale, density)
        assertEquals("a streak aimed at the old width must not keep flying", 0, s.meteors.count { it.alive })
        assertEquals("the sky does not blink out on resize", METEOR_STAR_COUNT, s.sky.size)
        assertEquals("a resize is not an event — the schedule is untouched", nextAt, s.nextAtSec, 0.0)
        for (st in s.sky) {
            assertTrue("star x=${st.x} was not rescaled into the new box", st.x >= -10f && st.x <= 710f)
            assertTrue("star y=${st.y} was not rescaled into the new box", st.y >= -10f && st.y <= 1210f)
        }
    }

    @Test fun `nothing goes non-finite - at any viewport, at any frame time`() {
        val s = sim(seed = 101)
        for (f in 1..1000) { s.update(0.0, 1f / 60f, true); assertFinite(s, "frame $f") }

        // Hostile: a zero-area canvas, then a recovery, with the engine's dt clamp.
        val rough = MeteorsSim(1f, seeded(102))
        rough.resize(0f, 0f, scale, density)
        for (f in 1..600) rough.update(0.0, if (f % 97 == 0) 0.05f else 1f / 60f, true)
        rough.resize(width, height, scale, density)
        for (f in 1..3000) { rough.update(0.0, 1f / 60f, true); assertFinite(rough, "recovered $f") }
        assertTrue("the field recovers once the canvas has area", rough.spawned > 0)

        // A 4-second stall: the catch-up loop must TERMINATE (this test hangs if it
        // does not) and must not swallow the events it stepped over.
        val stalled = sim(seed = 103)
        for (f in 1..200) stalled.update(0.0, 4f, true)        // 800 s in 200 frames
        assertFinite(stalled, "stalled")
        assertTrue("a long stall must not swallow every event", stalled.spawned > 10)
    }

    @Test fun `the hot path reuses its slots for the whole session`() {
        val s = sim(seed = 45)
        val ids = s.meteors.toList()
        val sky = s.sky.toList()
        run(s, 300f)
        assertEquals("the pool is fixed-size", METEOR_POOL, s.meteors.size)
        assertTrue("a recycled streak reuses its slot object",
            s.meteors.withIndex().all { (i, p) -> p === ids[i] })
        assertTrue("the sky is never re-allocated",
            s.sky.withIndex().all { (i, p) -> p === sky[i] })
        assertTrue("recycling must not reshuffle the star layers",
            s.sky.withIndex().all { (i, p) -> p.far == (i < 30) })
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 99)
            run(s, 120f)
            return dump(s)
        }
        assertEquals(once(), once())
    }

    // ── 7. The sub-populations differ on every visible axis ──

    @Test fun `the two meteor kinds differ on every visible axis`() {
        // Speed is the ONE axis on which the two bands legitimately overlap: dust
        // runs 900..1420 and a fireball 620..950 (meteors.js, verbatim — a 50 px/s
        // sliver). "Dust is the faster population" is therefore a claim about the
        // BAND, not about strict separation: faster at both ends, and faster on
        // average, which is exactly what meteors.test.mjs asserts empirically
        // (mean(dust) > mean(fire) — mirrored above in `tail length encodes
        // speed…`, on real spawned streaks). Asserting disjoint bands here would
        // force a re-tune of the shared tuning table and break web parity.
        assertTrue("hairline dust is the faster population",
            METEOR_DUST.speedMin > METEOR_FIREBALL.speedMin &&
                METEOR_DUST.speedMax > METEOR_FIREBALL.speedMax)
        assertTrue("a fireball drags a longer tail per unit speed",
            METEOR_FIREBALL.tailKMin > METEOR_DUST.tailKMax)
        assertTrue("a fireball is thicker", METEOR_FIREBALL.widthMin > METEOR_DUST.widthMax)
        assertTrue("…and brighter", METEOR_FIREBALL.alphaMin > METEOR_DUST.alphaMax)
        assertTrue("…and is born warmer on the ramp", METEOR_FIREBALL.ramp0Min > METEOR_DUST.ramp0Max)
        assertTrue("…and blooms harder", METEOR_FIREBALL.glow > METEOR_DUST.glow)
        assertTrue("the fireball must be the RARE one", METEOR_FIREBALL_CHANCE < 0.5f)
    }

    @Test fun `the two star layers differ on every visible axis`() {
        assertTrue("near stars are bigger", METEOR_STAR_NEAR.rMin >= METEOR_STAR_FAR.rMax)
        assertTrue("…and brighter", METEOR_STAR_NEAR.aMax > METEOR_STAR_FAR.aMax)
        assertTrue("…and drift faster", METEOR_STAR_NEAR.drift > METEOR_STAR_FAR.drift)
        assertTrue("…and twinkle quicker", METEOR_STAR_NEAR.twMin > METEOR_STAR_FAR.twMin)
        assertTrue("far dust must stay under the reading-box budget on its own",
            METEOR_STAR_FAR.aMax * METEOR_CENTRE_DIM < 0.2f)
        // Per-star envelope depth and twinkle: distinct rate AND distinct phase, so
        // the sky can never breathe in unison.
        val sky = sim(seed = 52).sky
        assertEquals("envelope depth must be per-star", sky.size, sky.map { it.envMul }.toSet().size)
        assertEquals("a shared twinkle rate beats back into sync", sky.size, sky.map { it.tws }.toSet().size)
        assertEquals("a shared phase flashes in unison", sky.size, sky.map { it.tw }.toSet().size)
    }
}
