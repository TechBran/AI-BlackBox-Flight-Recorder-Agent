package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.SLIPSTREAM_ADVECT
import com.aiblackbox.portal.ui.components.SLIPSTREAM_EPS
import com.aiblackbox.portal.ui.components.SLIPSTREAM_FADE
import com.aiblackbox.portal.ui.components.SLIPSTREAM_KINDS
import com.aiblackbox.portal.ui.components.SLIPSTREAM_MAX_ALPHA
import com.aiblackbox.portal.ui.components.SLIPSTREAM_MAX_LINE_PX
import com.aiblackbox.portal.ui.components.SLIPSTREAM_MAX_THREADS
import com.aiblackbox.portal.ui.components.SLIPSTREAM_MIN_ALPHA
import com.aiblackbox.portal.ui.components.SLIPSTREAM_MIN_THREADS
import com.aiblackbox.portal.ui.components.SLIPSTREAM_RESET_PER_SEC
import com.aiblackbox.portal.ui.components.SLIPSTREAM_SMEAR
import com.aiblackbox.portal.ui.components.SLIPSTREAM_STYLES
import com.aiblackbox.portal.ui.components.SLIPSTREAM_TRAIL
import com.aiblackbox.portal.ui.components.SLIPSTREAM_WIDTHS
import com.aiblackbox.portal.ui.components.SLIP_ALPHA_N
import com.aiblackbox.portal.ui.components.SLIP_BUCKETS
import com.aiblackbox.portal.ui.components.SLIP_RAMP_N
import com.aiblackbox.portal.ui.components.SLIP_WIDTH_N
import com.aiblackbox.portal.ui.components.SlipstreamSim
import com.aiblackbox.portal.ui.components.slipArch
import com.aiblackbox.portal.ui.components.slipPot
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Slipstream physics + legibility — headless, seeded, no Compose / Android
 * graphics.
 *
 * Slipstream strokes GEOMETRY instead of blitting a sprite atlas, which means
 * the DRAWN OUTPUT itself is reproducible on the JVM: `buildSegments()` is the
 * exact bucketing `render()` issues its drawLines calls from, so the two
 * legibility caps (stroke width, stroke alpha) are asserted here from the
 * strokes the effect actually emits — not from the config that claims them.
 *
 * The rest is what would otherwise only be caught by staring at a phone: that
 * the population stays inside its clamp, that the field DOES NOT GO STATIC on
 * the curl potential's fixed attractors (the rule that makes or breaks this
 * effect), that no recycled thread strokes a bright bar across the chat text,
 * that motion and churn are both delta-timed, and that DENSITY NEVER CHANGES
 * APPARENT GEOMETRY.
 *
 * Mirrors Portal/modules/fx/effects/slipstream.test.mjs — keep them in step.
 */
class SlipstreamFieldTest {

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

    private fun sim(seed: Int = 42, countScale: Float = 1f): SlipstreamSim =
        SlipstreamSim(countScale, seeded(seed)).also { it.resize(width, height, scale, density) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: SlipstreamSim, frames: Int, dt: Float = 1f / 60f, active: Boolean = true) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, active)
    }

    // ── bucket decode: bin = (ci * ALPHA_N + ai) * WIDTH_N + wi ──
    private fun wi(b: Int) = b % SLIP_WIDTH_N
    private fun ai(b: Int) = (b / SLIP_WIDTH_N) % SLIP_ALPHA_N
    private fun ci(b: Int) = b / (SLIP_WIDTH_N * SLIP_ALPHA_N)

    /** The alpha the paint is actually set to for bucket [b] (from the packed
     *  ARGB the renderer hands to Paint.setColor — the real drawn value). */
    private fun bucketAlpha(b: Int): Float =
        ((SLIPSTREAM_STYLES[b / SLIP_WIDTH_N] ushr 24) and 0xFF) / 255f

    /** Longest single stroke, in device px, of the segments currently built. */
    private fun longestSegment(s: SlipstreamSim): Float {
        val pts = s.segmentPoints
        var worst = 0f
        val end = s.bucketStart(SLIP_BUCKETS) * 4
        var i = 0
        while (i < end) {
            worst = max(worst, hypot(pts[i + 2] - pts[i], pts[i + 3] - pts[i + 1]))
            i += 4
        }
        return worst
    }

    /**
     * Analytic ceiling on |velocity| in device px/s: the chase only ever lerps
     * toward a target, so the largest target the field can produce bounds every
     * segment ever drawn. Recomputed here from the potential so the test cannot
     * agree with the module by luck.
     */
    private fun speedBoundPx(scale: Float): Float {
        val gp = scale * 3.1f                                   // device px per box CSS px
        val g = gp * 0.5f                                       // …per pre-apparent CSS px
        val maxCurl = 2f * sin(0.0065f * SLIPSTREAM_EPS.toFloat()) + sin(0.013f * SLIPSTREAM_EPS.toFloat())
        val gain = SLIPSTREAM_KINDS.maxOf { it.gain }
        val bx = maxCurl * SLIPSTREAM_ADVECT * g * gain + SLIPSTREAM_KINDS.maxOf { abs(it.dx) } * gp
        val by = maxCurl * SLIPSTREAM_ADVECT * g * gain + SLIPSTREAM_KINDS.maxOf { abs(it.dy) } * gp
        return hypot(bx, by)
    }

    // ── 1. The legibility budget — the whole point of the effect ──

    @Test fun `every quantised stroke width is inside the cap by construction`() {
        for (w in SLIPSTREAM_WIDTHS) {
            assertTrue("quantised width $w exceeds the cap", w <= SLIPSTREAM_MAX_LINE_PX)
        }
        assertTrue("the budget is 1.5 CSS px — this is the whole point of the effect",
            SLIPSTREAM_MAX_LINE_PX <= 1.5f)
        assertTrue("alpha ceiling moved", SLIPSTREAM_MAX_ALPHA <= 0.30f)
        // The brightest level the renderer can ever set (level midpoints).
        val brightest = (0 until SLIP_BUCKETS).maxOf { bucketAlpha(it) }
        assertTrue("a quantised level busts the 0.30 alpha budget: $brightest",
            brightest <= SLIPSTREAM_MAX_ALPHA)
    }

    @Test fun `1000 frames never stroke wider than the cap or brighter than 0_30`() {
        val s = sim(seed = 11)
        var maxWidth = 0f
        var maxAlpha = 0f
        var segments = 0L
        for (f in 1..1000) {
            s.update(f * 16.6667, 1f / 60f, true)
            val n = s.buildSegments()
            segments += n
            for (b in 0 until SLIP_BUCKETS) {
                if (s.bucketStart(b) == s.bucketStart(b + 1)) continue
                maxWidth = max(maxWidth, s.strokeWidthPx(b))
                maxAlpha = max(maxAlpha, bucketAlpha(b))
            }
        }
        assertTrue("field looks dead: only $segments segments in 1000 frames", segments > 100_000)
        assertTrue("stroke width ${maxWidth}px busts the ${s.maxStrokeWidthPx}px budget",
            maxWidth <= s.maxStrokeWidthPx)
        assertTrue("stroke alpha $maxAlpha busts the 0.30 budget", maxAlpha <= SLIPSTREAM_MAX_ALPHA)
        // Hair-thin in absolute terms too: ~0.5 dp at the reference density.
        assertTrue("threads are not hair-thin any more: ${maxWidth}px", maxWidth <= 1.7f)
    }

    @Test fun `no thread ever strokes a streak across the viewport`() {
        // A recycled particle whose history was left stale draws a ~1000px bar
        // straight through the chat text. The bound is analytic: the largest
        // velocity the field can produce, times the frame time.
        val s = sim(seed = 5)
        val dt = 1f / 60f
        var worst = 0f
        for (f in 1..1000) {
            s.update(f * dt * 1000.0, dt, true)
            s.buildSegments()
            worst = max(worst, longestSegment(s))
        }
        val limit = speedBoundPx(scale) * dt * 1.02f
        assertTrue("longest segment $worst px exceeds the $limit px bound", worst <= limit)
        assertTrue("the bound itself is too loose to be meaningful: $limit", limit < 40f)
    }

    @Test fun `a re-seated thread carries no tail from where it used to be`() {
        val s = sim(seed = 23)
        for (p in s.threads) assertEquals("init left a tail", 0, p.segments)
        run(s, 200)
        assertTrue("sanity: a settled field should be in motion",
            s.threads.count { it.segments > 0 } > s.threads.size * 0.9)
        s.rearm()
        for (p in s.threads) assertEquals("rearm left a tail", 0, p.segments)
        // …and a thread can never stroke more history than the trail holds.
        run(s, 400)
        assertTrue("the tail outgrew its ring buffer",
            s.threads.all { it.segments <= SLIPSTREAM_TRAIL })
    }

    @Test fun `the explicit trail reproduces the web smear and dies out`() {
        // Android has no clearPolicy fade, so the smear is redrawn per frame.
        assertEquals("age 0 is the head segment, undimmed", 1f, SLIPSTREAM_FADE[0], 1e-6f)
        for (i in 1 until SLIPSTREAM_TRAIL) {
            assertEquals("the fade is not the web's 12% per frame",
                SLIPSTREAM_FADE[i - 1] * (1f - SLIPSTREAM_SMEAR), SLIPSTREAM_FADE[i], 1e-6f)
        }
        // The accumulated veil must stay UNDER the web's steady state (1/0.12).
        val veil = SLIPSTREAM_FADE.sum()
        assertTrue("the explicit trail veils more than the web's smear: $veil",
            veil < 1f / SLIPSTREAM_SMEAR)
        assertTrue("…and it is long enough to read as a thread: $veil", veil > 5f)
    }

    // ── 2. Population ──

    @Test fun `population is viewport-driven and clamped at both ends`() {
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val n = sim(countScale = cs).threads.size
            assertTrue("countScale $cs produced $n threads", n in SLIPSTREAM_MIN_THREADS..SLIPSTREAM_MAX_THREADS)
        }
        val phone = SlipstreamSim(1f, seeded(7)).also { it.resize(1080f, 2400f, 1f, 3.1f) }
        val tablet = SlipstreamSim(1f, seeded(7)).also { it.resize(2400f, 3800f, 1f, 3.1f) }
        assertTrue("count must track viewport AREA", tablet.threads.size > phone.threads.size)
        for (w in floatArrayOf(240f, 1080f, 4000f)) {
            val s = SlipstreamSim(1f, seeded(9)).also { it.resize(w, w * 2f, scale, density) }
            assertTrue("a ${w}px canvas produced ${s.threads.size}",
                s.threads.size in SLIPSTREAM_MIN_THREADS..SLIPSTREAM_MAX_THREADS)
        }
    }

    @Test fun `three sub-populations are all present in a settled field`() {
        val s = sim(seed = 13)
        run(s, 120)
        assertEquals("expected all ${SLIPSTREAM_KINDS.size} kinds alive",
            SLIPSTREAM_KINDS.size, s.threads.map { it.gain }.toSet().size)
    }

    @Test fun `the kinds differ on every visible axis`() {
        val weights = SLIPSTREAM_KINDS.map { it.w }.sum()
        assertEquals("sub-population weights must sum to 1", 1f, weights, 1e-5f)
        assertEquals("gain must differ per kind", SLIPSTREAM_KINDS.size, SLIPSTREAM_KINDS.map { it.gain }.toSet().size)
        assertEquals("width must differ per kind", SLIPSTREAM_KINDS.size, SLIPSTREAM_KINDS.map { it.width }.toSet().size)
        assertEquals("alpha must differ per kind", SLIPSTREAM_KINDS.size, SLIPSTREAM_KINDS.map { it.alpha }.toSet().size)
        assertEquals("response must differ per kind", SLIPSTREAM_KINDS.size, SLIPSTREAM_KINDS.map { it.resp }.toSet().size)
        for (k in SLIPSTREAM_KINDS) {
            assertTrue("a kind busts the width cap: ${k.width}", k.width <= SLIPSTREAM_MAX_LINE_PX)
            assertTrue("a kind busts the alpha cap: ${k.alpha}", k.alpha <= SLIPSTREAM_MAX_ALPHA)
            assertTrue("every kind must be visible at its peak", k.alpha > SLIPSTREAM_MIN_ALPHA)
        }
        // The glint is the sparse quick one; the veil is the slow wide one.
        val glint = SLIPSTREAM_KINDS.last()
        val veil = SLIPSTREAM_KINDS[1]
        assertTrue("the glint must be the sparsest", glint.w < veil.w)
        assertTrue("the glint must react hardest", glint.resp > veil.resp)
        assertTrue("the veil must be the widest", veil.width > glint.width)
    }

    // ── 3. THE premium rules: an arch over life, a life-indexed ramp ──

    @Test fun `size rides an arch over life, with a per-particle multiplier`() {
        assertEquals("a newborn thread has no size yet", 0f, slipArch(0f), 1e-6f)
        assertEquals("…nor does a dying one", 0f, slipArch(1f), 1e-6f)
        assertEquals("and it holds a plateau in the middle", 1f, slipArch(0.5f), 1e-6f)
        assertTrue("the arch opens fast", slipArch(0.12f) > 0.6f)
        val s = sim(seed = 17)
        run(s, 300)
        var peak = 0.0; var peakN = 0
        var tail = 0.0; var tailN = 0
        val envelopes = HashSet<Float>()
        for (f in 1..300) {
            s.update((300 + f) * 16.6667, 1f / 60f, true)
            for (p in s.threads) {
                envelopes.add(p.wEnv)
                if (p.wi < 0) continue
                val u = 1f - p.life
                if (u > 0.42f && u < 0.58f) { peak += p.wi; peakN++ }
                else if (u < 0.12f || u > 0.88f) { tail += p.wi; tailN++ }
            }
        }
        assertTrue("not enough samples: peak=$peakN tail=$tailN", peakN > 20 && tailN > 20)
        val pm = peak / peakN
        val tm = tail / tailN
        assertTrue("size does not track life: mid-life width bucket $pm vs tails $tm", pm > tm * 1.25)
        assertTrue("the envelope multiplier is not per-particle (${envelopes.size} distinct)",
            envelopes.size > 50)
        assertTrue("the envelope must span its whole band",
            envelopes.min() < 0.85f && envelopes.max() > 1.25f)
        // …and the arch must survive quantisation in the DRAWN output: more than
        // one width bucket has to be in use. (Not three — the top bucket 1.0 is
        // unreachable by construction, cap 1.05 < upper edge 1.2, exactly as in
        // slipstream.js. This assertion is the guard that the OTHER two do not
        // collapse into one, which is what happens if the apparent scale is
        // applied before the quantisation on a phone-pinned 0.5.)
        val buckets = HashSet<Int>()
        s.buildSegments()
        for (b in 0 until SLIP_BUCKETS) if (s.bucketStart(b) != s.bucketStart(b + 1)) buckets.add(wi(b))
        assertTrue("the size curve quantised away — only $buckets drawn", buckets.size >= 2)
    }

    @Test fun `alpha opens and closes on the same arch, so a reset never pops in`() {
        val s = sim(seed = 29)
        var peak = 0.0; var peakN = 0
        var tail = 0.0; var tailN = 0
        var reseated = 0
        for (f in 1..400) {
            s.update(f * 16.6667, 1f / 60f, true)
            s.buildSegments()
            for (p in s.threads) {
                // THIS is what buys the 2.5%-per-frame churn: a thread re-seated
                // somewhere else emits NO segment on the frame it jumped, so it
                // cannot stroke a bar from where it used to be, and its envelope
                // fades it up from nothing instead of blinking it into existence.
                if (p.segments == 0) reseated++
                if (p.life >= 1f) {
                    assertTrue("a thread that died and re-seated was drawn", p.wi < 0)
                    assertEquals("…and it must contribute no segment", 0, p.segments)
                }
                if (p.wi < 0 || f < 200) continue
                val u = 1f - p.life
                if (u > 0.42f && u < 0.58f) { peak += p.alpha; peakN++ }
                else if (u < 0.12f || u > 0.88f) { tail += p.alpha; tailN++ }
            }
        }
        assertTrue("sanity: only $reseated re-seated threads observed", reseated > 50)
        assertTrue("not enough samples: peak=$peakN tail=$tailN", peakN > 20 && tailN > 20)
        assertTrue("alpha does not track life: $peakN/$tailN", peak / peakN > (tail / tailN) * 1.3)
    }

    @Test fun `colour is a ramp indexed by life, not a flat tint`() {
        val s = sim(seed = 37)
        run(s, 300)
        var young = 0.0; var youngN = 0
        var old = 0.0; var oldN = 0
        val seen = HashSet<Int>()
        for (f in 1..200) {
            s.update((300 + f) * 16.6667, 1f / 60f, true)
            for (p in s.threads) {
                if (p.wi < 0) continue
                seen.add(p.ci)
                val u = 1f - p.life
                if (u < 0.25f) { young += p.ci; youngN++ } else if (u > 0.7f) { old += p.ci; oldN++ }
            }
        }
        assertTrue("not enough samples: young=$youngN old=$oldN", youngN > 20 && oldN > 20)
        assertTrue("colour does not track life: young ${young / youngN} vs old ${old / oldN}",
            old / oldN > young / youngN + 1.5)
        assertTrue("only ${seen.size} ramp stops ever used", seen.size >= 4)
        assertTrue("the ramp index must stay inside the atlas", seen.all { it in 0 until SLIP_RAMP_N })
    }

    // ── 4. Motion: advected by the curl, delta-timed, never static ──

    @Test fun `threads are advected BY the curl field, not drifting through it`() {
        val s = sim(seed = 41)
        val frames = 120
        run(s, frames)
        val ts = frames * (1f / 60f) * 1000.0 * 0.001
        val gp = scale * 3.1f
        val g = gp * 0.5f
        var sum = 0.0
        var n = 0
        for (p in s.threads) {
            val lx = (p.x / gp).toDouble()
            val ly = (p.y / gp).toDouble()
            val cx = slipPot(lx, ly + SLIPSTREAM_EPS, ts) - slipPot(lx, ly - SLIPSTREAM_EPS, ts)
            val cy = -(slipPot(lx + SLIPSTREAM_EPS, ly, ts) - slipPot(lx - SLIPSTREAM_EPS, ly, ts))
            val tx = cx * SLIPSTREAM_ADVECT * g * p.gain + p.dx * gp
            val ty = cy * SLIPSTREAM_ADVECT * g * p.gain + p.dy * gp
            val mag = hypot(p.vx.toDouble(), p.vy.toDouble()) * hypot(tx, ty)
            if (mag > 0) { sum += (p.vx * tx + p.vy * ty) / mag; n++ }
        }
        assertTrue("mean cosine to the local field is only ${sum / n}", sum / n > 0.8)
    }

    @Test fun `motion is delta-timed — 120 Hz travels the same distance per second`() {
        fun measure(dt: Float, frames: Int): Float {
            val s = sim(seed = 3)
            var travelled = 0f
            var samples = 0
            var i = 0
            while (i < frames) {
                i++
                s.update(i * dt * 1000.0, dt, true)
                s.buildSegments()
                val pts = s.segmentPoints
                var j = 0
                val end = s.bucketStart(SLIP_BUCKETS) * 4
                while (j < end) {                        // stride: a fair sample
                    travelled += hypot(pts[j + 2] - pts[j], pts[j + 3] - pts[j + 1])
                    samples++
                    j += 4 * 7
                }
            }
            return travelled / samples / dt               // device px per second
        }
        val at60 = measure(1f / 60f, 600)
        val at120 = measure(1f / 120f, 1200)
        val drift = abs(at120 - at60) / at60
        assertTrue("speed is frame-rate dependent: $at60 px/s at 60 Hz vs $at120 at 120 Hz",
            drift < 0.12f)
    }

    @Test fun `the scatter-reset churns 2-3 percent per 60Hz frame — as a RATE`() {
        // Without it the field walks onto the potential's fixed attractors and
        // freezes. A naive port hard-codes "2.5% per frame", which doubles on a
        // 120 Hz panel; the per-SECOND rate must be identical on both.
        fun churn(dt: Float, frames: Int): Float {
            val s = sim(seed = 61)
            run(s, frames, dt)
            return s.resets / (frames * dt) / s.threads.size
        }
        val r60 = churn(1f / 60f, 600)
        val r120 = churn(1f / 120f, 1200)
        assertTrue("per-60Hz-frame churn is ${r60 / 60f * 100f}%, want 2-3%",
            r60 / 60f > 0.02f && r60 / 60f < 0.03f)
        assertTrue("churn is frame-rate dependent: $r60/s at 60 Hz vs $r120/s at 120 Hz",
            abs(r120 - r60) / r60 < 0.05f)
        assertEquals("…and it is the module's declared rate", SLIPSTREAM_RESET_PER_SEC, r60, 0.15f)
    }

    @Test fun `the field stays spread out after 20 seconds — it does not go static`() {
        val s = sim(seed = 67)
        run(s, 1200)
        val n = s.threads.size
        var mx = 0.0; var my = 0.0
        for (p in s.threads) { mx += p.x; my += p.y }
        mx /= n; my /= n
        var vx = 0.0; var vy = 0.0
        for (p in s.threads) { vx += (p.x - mx) * (p.x - mx); vy += (p.y - my) * (p.y - my) }
        val sdx = sqrt(vx / n)
        val sdy = sqrt(vy / n)
        // Uniform over 1080x2400 would be ~312 / ~693. Collapsed onto the
        // potential's attractors is a small fraction of that.
        assertTrue("x collapsed onto attractors: sd=$sdx", sdx > width * 0.22)
        assertTrue("y collapsed onto attractors: sd=$sdy", sdy > height * 0.22)
    }

    // ── 5. The DPI law ──

    @Test fun `density changes resolution, never apparent geometry`() {
        // The same physical screen at 1x and 2x density: every spatial quantity
        // must come out exactly 2x in pixels — i.e. identical in apparent size.
        // Population is dp-derived, so it must NOT change; nor may a width
        // bucket, which is a look, not a resolution.
        val lo = SlipstreamSim(1f, seeded(88)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = SlipstreamSim(1f, seeded(88)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("population is dp-derived, not px-derived", lo.threads.size, hi.threads.size)
        run(lo, 300); run(hi, 300)
        lo.threads.forEachIndexed { i, p ->
            val q = hi.threads[i]
            assertEquals("x must be exactly 2x at 2x density", p.x * 2f, q.x, 0.5f)
            assertEquals("y must be exactly 2x at 2x density", p.y * 2f, q.y, 0.5f)
            assertEquals("velocity must be exactly 2x", p.vx * 2f, q.vx, 1f)
            assertEquals("the life curve is density-free", p.life, q.life, 1e-4f)
            assertEquals("…and so is the width bucket", p.wi.toFloat(), q.wi.toFloat(), 0f)
            assertEquals("…and the colour stop", p.ci.toFloat(), q.ci.toFloat(), 0f)
        }
        // Apparent stroke width is constant: 2x the device px at 2x the density.
        assertEquals("a 2x screen must draw the same APPARENT width",
            lo.strokeWidthPx(0) * 2f, hi.strokeWidthPx(0), 1e-4f)
    }

    // ── 6. Lifecycle, pooling, robustness, determinism ──

    @Test fun `resize and rearm re-seat the field without reallocating slots`() {
        val s = sim(seed = 71)
        val before = s.threads.toList()
        run(s, 200)
        s.resize(width * 1.8f, height, scale, density)             // unfolding a Fold
        assertTrue("survivors must keep their slot object",
            s.threads.take(before.size).withIndex().all { (i, p) -> p === before[i] })
        s.resize(380f, 720f, scale, density)                       // then shrink hard
        for (p in s.threads) {
            assertTrue("a stray was left outside the new box: ${p.x},${p.y}",
                p.x <= 380f && p.y <= 720f)
        }
        s.rearm()
        val lives = s.threads.map { (it.life * 100f).toInt() }.toSet()
        assertTrue("rearm did not stagger the life curve (${lives.size} distinct)", lives.size > 20)
        run(s, 200)
    }

    @Test fun `the hot path reuses its slots for the whole session`() {
        val s = sim(seed = 45)
        val ids = s.threads.toList()
        val pts = s.segmentPoints
        run(s, 1200)
        s.buildSegments()
        assertTrue("a recycled thread must reuse its slot object",
            s.threads.withIndex().all { (i, p) -> p === ids[i] })
        assertTrue("the draw scratch must never be reallocated", pts === s.segmentPoints)
    }

    @Test fun `nothing goes non-finite over 1000 frames, including a 50ms stall`() {
        val s = sim(seed = 77)
        var now = 0.0
        for (i in 1..1000) {
            val dt = if (i % 97 == 0) 0.05f else 1f / 60f      // the post-stall frame
            now += dt * 1000.0
            s.update(now, dt, true)
        }
        s.buildSegments()
        for (p in s.threads) {
            for (v in floatArrayOf(p.x, p.y, p.vx, p.vy, p.life, p.alpha, p.wEnv)) {
                assertTrue("a field went non-finite ($v)", v.isFinite())
            }
            assertTrue("life out of range: ${p.life}", p.life > 0f && p.life <= 1f)
            assertTrue("width bucket out of range: ${p.wi}", p.wi >= -1 && p.wi < SLIP_WIDTH_N)
            assertTrue("colour stop out of range: ${p.ci}", p.ci in 0 until SLIP_RAMP_N)
            assertTrue("a drawn thread is under the visibility floor: ${p.alpha}",
                p.wi < 0 || p.alpha >= SLIPSTREAM_MIN_ALPHA)
        }
        val pts = s.segmentPoints
        val end = s.bucketStart(SLIP_BUCKETS) * 4
        for (i in 0 until end) assertTrue("a non-finite coordinate reached the canvas", pts[i].isFinite())
        assertTrue("segments must never outgrow the scratch", end <= pts.size)
    }

    @Test fun `an idle field keeps combing and the population never empties`() {
        // Deliberately unlike Fireflies and like Matrix: the population is FIXED,
        // so there is no spawn ramp to gate on `active`. The engine's bounded
        // drain plus the alpha fade is what ends the field.
        val s = sim(seed = 83)
        val n = s.threads.size
        for (f in 1..600) s.update(f * 16.6667, 1f / 60f, false)
        assertEquals("the pool is fixed-size", n, s.threads.size)
        assertTrue("an idle field must keep flowing", s.buildSegments() > 0)
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 99)
            run(s, 240)
            return s.threads.joinToString { "${it.x},${it.y},${it.life},${it.vx},${it.ci},${it.wi}" }
        }
        assertEquals(once(), once())
    }

    @Test fun `bucket indices stay inside the style tables`() {
        val s = sim(seed = 91)
        run(s, 240)
        val total = s.buildSegments()
        assertTrue("a settled field draws something", total > 0)
        var counted = 0
        for (b in 0 until SLIP_BUCKETS) {
            val from = s.bucketStart(b)
            val to = s.bucketStart(b + 1)
            assertTrue("bucket $b is inverted", to >= from)
            counted += to - from
            if (from == to) continue
            assertTrue("style index out of range", b / SLIP_WIDTH_N < SLIPSTREAM_STYLES.size)
            assertTrue("width index out of range", wi(b) < SLIPSTREAM_WIDTHS.size)
            assertTrue("alpha level out of range", ai(b) in 0 until SLIP_ALPHA_N)
            assertTrue("colour stop out of range", ci(b) in 0 until SLIP_RAMP_N)
        }
        assertEquals("the counting sort lost segments", total, counted)
        assertTrue("a thread cannot emit more than its trail",
            total <= s.threads.size * SLIPSTREAM_TRAIL)
        assertTrue("the pool must stay inside the population cap",
            s.threads.size <= SLIPSTREAM_MAX_THREADS)
        // The oldest tail segment of the brightest thread must still clear the
        // visibility floor, or the trail would be truncated before its fade ends
        // and the thread would stop with a hard edge instead of dissolving.
        assertTrue("the trail is culled before it fades out",
            SLIPSTREAM_MAX_ALPHA * SLIPSTREAM_FADE[SLIPSTREAM_TRAIL - 1] > SLIPSTREAM_MIN_ALPHA)
    }
}
