package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.CINDER_ALPHA_FLAKE
import com.aiblackbox.portal.ui.components.CINDER_ALPHA_SPARK
import com.aiblackbox.portal.ui.components.CINDER_BUOYANCY
import com.aiblackbox.portal.ui.components.CINDER_CURL
import com.aiblackbox.portal.ui.components.CINDER_GUST_GAIN_FLAKE
import com.aiblackbox.portal.ui.components.CINDER_GUST_GAIN_SPARK
import com.aiblackbox.portal.ui.components.CINDER_HARD_CAP
import com.aiblackbox.portal.ui.components.CINDER_MAX_POP
import com.aiblackbox.portal.ui.components.CINDER_MIN_POP
import com.aiblackbox.portal.ui.components.CINDER_SAIL_FLAKE
import com.aiblackbox.portal.ui.components.CINDER_SAIL_SPARK
import com.aiblackbox.portal.ui.components.CINDER_SPARK_ODDS
import com.aiblackbox.portal.ui.components.CINDER_SPARK_SINK
import com.aiblackbox.portal.ui.components.CINDER_SWELL_MAX
import com.aiblackbox.portal.ui.components.CINDER_SWELL_MIN
import com.aiblackbox.portal.ui.components.Cinder
import com.aiblackbox.portal.ui.components.CinderWind
import com.aiblackbox.portal.ui.components.CindersSim
import com.aiblackbox.portal.ui.components.EMBER_BUOYANCY
import com.aiblackbox.portal.ui.components.EMBER_CURL
import com.aiblackbox.portal.ui.components.EMBER_SPARK_SINK
import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.cinderAlpha
import com.aiblackbox.portal.ui.components.cinderGeom
import com.aiblackbox.portal.ui.components.cinderGustAt
import com.aiblackbox.portal.ui.components.cinderRampIndex
import com.aiblackbox.portal.ui.components.cinderSizeOverLife
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.hypot

/**
 * Cinders on the Wind — headless physics, seeded, no Compose / Android graphics.
 *
 * The sim takes its randomness from an injected [FieldRandom], so the whole
 * simulation is drivable from the JVM with a deterministic PRNG. What is asserted
 * here is what would otherwise only be caught by staring at a phone: that the
 * field reads as a lateral CURRENT rather than a fire (the whole point of the
 * fork), that gusts are time-based events that shove and scatter without ever
 * blowing a mote backwards, that the LEGIBILITY CEILINGS this backdrop inherits
 * from the approved Embers field actually hold, and that DENSITY NEVER CHANGES
 * APPARENT GEOMETRY.
 *
 * Mirrors Portal/modules/fx/effects/cinders.test.mjs — keep them in step.
 * (Thresholds were calibrated by driving a transliteration of this sim over 40
 * seeds, so every one of them carries at least a 1.5× margin.)
 */
class CindersFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
    private val scale = 1f

    /** Pool size for [width] × [height] at [density]: one mote per 900 dp². */
    private val basePop = 300

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

    private fun sim(
        seed: Int = 42,
        countScale: Float = 1f,
        w: Float = width,
        h: Float = height,
        sc: Float = scale,
        d: Float = density,
    ): CindersSim = CindersSim(countScale, seeded(seed)).also { it.resize(w, h, sc, d) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: CindersSim, frames: Int, dt: Float = 1f / 60f, active: Boolean = true) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, active)
    }

    private fun live(s: CindersSim): List<Cinder> = s.motes.filter { it.alive }
    private fun mean(xs: List<Float>): Float = if (xs.isEmpty()) 0f else xs.sum() / xs.size

    /** Run [body] with the global weather overridden, then put it back — [CinderWind]
     *  is one field of weather for the whole app, exactly like the web's `WIND`. */
    private fun <T> withWind(
        speed: Float = CinderWind.speed,
        gust: Float = CinderWind.gust,
        scatter: Float = CinderWind.scatter,
        body: () -> T,
    ): T {
        val s = CinderWind.speed; val g = CinderWind.gust; val sc = CinderWind.scatter
        return try {
            CinderWind.speed = speed; CinderWind.gust = gust; CinderWind.scatter = scatter
            body()
        } finally {
            CinderWind.speed = s; CinderWind.gust = g; CinderWind.scatter = sc
        }
    }

    // ── 1. Population: the Embers budget, verbatim ──

    @Test fun `population is the Embers budget across the whole count dial`() {
        assertEquals("one mote per 900 dp² of screen", basePop, sim().motes.size)
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val n = sim(countScale = cs).motes.size
            assertEquals("countScale $cs", ParticleTuning.scaleCount(basePop, cs), n)
            assertTrue("countScale $cs produced $n motes", n in 1..CINDER_HARD_CAP)
        }
        // …and the dp-area rule is clamped to the Embers band on tiny + huge canvases.
        assertEquals("a small canvas floors at the Embers minimum", CINDER_MIN_POP,
            sim(seed = 7, w = 320f, h = 640f).motes.size)
        assertEquals("a huge canvas caps at the Embers maximum", CINDER_MAX_POP,
            sim(seed = 7, w = 3000f, h = 6000f).motes.size)
    }

    @Test fun `the fork's force budget is the web module's, not Embers'`() {
        // The whole effect IS these four numbers. If one of them drifts, the field
        // stops being weather and goes back to being a fire — and the two surfaces
        // stop being diffable.
        assertEquals("swirl gain", 1900f, CINDER_CURL, 0f)
        assertEquals("buoyancy", 0.9f, CINDER_BUOYANCY, 0f)
        assertEquals("spark sink", 18f, CINDER_SPARK_SINK, 0f)
        assertEquals("terminal lateral speed", 210f, CinderWind.speed, 0f)
        assertTrue("the swirl must stop BEING the motion", CINDER_CURL < 0.4f * EMBER_CURL)
        assertTrue("a spent cinder has no heat left to climb on",
            CINDER_BUOYANCY <= EMBER_BUOYANCY / 8f)
        assertTrue("heavy sparks still settle, but only a little",
            CINDER_SPARK_SINK < EMBER_SPARK_SINK / 3f)
        // Two sub-populations that differ where it shows: sail and gust gain.
        assertTrue("sparks carry more sail", CINDER_SAIL_SPARK > CINDER_SAIL_FLAKE)
        assertTrue("a gust throws sparks over twice as hard",
            CINDER_GUST_GAIN_SPARK > 2f * CINDER_GUST_GAIN_FLAKE)
        assertTrue("sparks stay the minority", CINDER_SPARK_ODDS in 0.05..0.25)
    }

    // ── 2. Motion: a lateral CURRENT, not a rise ──

    @Test fun `the field travels sideways - every mote is carried downwind`() {
        val s = sim(seed = 13)
        run(s, 120)                                  // 2 s: long past terminal velocity
        val l = live(s)
        val vx = mean(l.map { it.vx })
        val avy = mean(l.map { abs(it.vy) })
        assertTrue("the current must dominate the swirl; mean vx = $vx px/s",
            vx > 0.5f * CinderWind.speed)
        assertTrue("no mote may be travelling upwind — that is the curl gain overwhelming the current",
            l.all { it.vx > 0f })
        assertTrue("arcs must be SHALLOW: |vy| = $avy vs vx = $vx", vx > 4f * avy)
    }

    @Test fun `buoyancy is dropped toward zero - in still air the field barely drifts`() {
        // The honest test of "buoyancy → 0" is to take the wind away and see what
        // is left. Whatever vertical motion the field has downwind belongs to
        // WIND.tilt, not to buoyancy; with the wind off, all that remains is the
        // zero-mean curl swirl plus the sparks' small sink.
        val calm = withWind(speed = 0f) {
            val s = sim(seed = 21)
            run(s, 300)
            mean(live(s).map { it.vy })
        }
        // 12 px/s is under 7% of the 178 px/s the same field runs at downwind, and
        // 1.5× the worst still-air residual measured over 40 seeds.
        assertTrue("still air must leave almost no vertical drift; got $calm px/s", abs(calm) < 12f)
    }

    @Test fun `the horizontal wrap keeps the field inside the band at full width`() {
        val s = sim(seed = 9)
        run(s, 400)                                  // ~6.7 s: every mote has crossed at least once
        val l = live(s)
        val band = 20f * scale
        val quarters = IntArray(4)
        for (p in l) {
            assertTrue("mote escaped horizontally at x=${p.x}", p.x >= -band && p.x <= width + band)
            assertTrue("dead motes must be culled", p.life > 0f && p.life <= 1f)
            quarters[((p.x / width) * 4f).toInt().coerceIn(0, 3)]++
        }
        assertTrue("the wrap must keep all four quarters populated: ${quarters.toList()}",
            quarters.all { it > 0 })
    }

    // ── 3. WIND is a real seam (a future weather mode drives it) ──

    @Test fun `WIND speed drives the field - zeroing it stops the current`() {
        fun measure(): Float {
            val s = sim(seed = 31)
            run(s, 120)
            return mean(live(s).map { it.vx })
        }
        val blowing = measure()
        val calm = withWind(speed = 0f) { measure() }
        assertTrue("with no wind the field must have no lateral bias; got $calm vs $blowing",
            abs(calm) < 0.25f * blowing)
    }

    @Test fun `gusts are time-based events, bounded, and mostly absent`() {
        var maxG = 0f
        var calmFrames = 0
        val n = 6000                                  // 120 s at 50 Hz
        for (i in 0 until n) {
            val g = cinderGustAt(i * 0.02, 1.7f)
            assertTrue("gust envelope out of [0,1]: $g", g in 0f..1f)
            if (g > maxG) maxG = g
            if (g == 0f) calmFrames++
        }
        assertTrue("there must be real gusts, not a wobble ($maxG)", maxG > 0.3f)
        val calmFraction = calmFrames.toFloat() / n
        assertTrue("gusts must be EVENTS; air was calm only ${(calmFraction * 100).toInt()}% of the time",
            calmFraction > 0.4f)
        assertEquals("the envelope is a pure function of time",
            cinderGustAt(3.3, 0.5f), cinderGustAt(3.3, 0.5f), 0f)
        assertTrue("…and of the field's own phase, so two overlays never gust in unison",
            cinderGustAt(3.3, 0.5f) != cinderGustAt(3.3, 2.9f))
    }

    @Test fun `a gust both shoves and scatters the field`() {
        fun sample(): Pair<Float, Float> {
            val s = sim(seed = 77)
            s.gustPhase = 1.35f                       // a gust rides through this window
            run(s, 90)
            val l = live(s)
            return mean(l.map { it.vx }) to mean(l.map { abs(it.vy) })
        }
        val gusty = sample()
        val calm = withWind(gust = 0f, scatter = 0f) { sample() }
        assertTrue("a gust must shove: ${gusty.first} vs ${calm.first} px/s", gusty.first > calm.first)
        assertTrue("a gust must SCATTER (extra curl gain): |vy| ${gusty.second} vs ${calm.second}",
            gusty.second > calm.second)
    }

    @Test fun `even at a gust peak the current still wins - nothing is blown backwards`() {
        val s = sim(seed = 44)
        s.gustPhase = 1.35f
        run(s, 240)
        assertTrue("a gust may scatter the field but must never reverse it",
            live(s).all { it.vx > 0f })
    }

    // ── 4. THE LEGIBILITY BUDGET — asserted, not asserted-in-a-comment ──

    @Test fun `the per-mote alpha ceiling is Embers', to the float`() {
        assertEquals("spark ceiling", 0.85f, CINDER_ALPHA_SPARK, 0f)
        assertEquals("flake ceiling", 0.55f, CINDER_ALPHA_FLAKE, 0f)
        var peakSpark = 0f
        var peakFlake = 0f
        var life = 0f
        while (life <= 1f) {
            var breathe = 0f
            while (breathe <= 1f) {                  // breathe = 0.6 + 0.4·sin ∈ [0.2, 1]
                peakSpark = maxOf(peakSpark, cinderAlpha(true, life, breathe))
                peakFlake = maxOf(peakFlake, cinderAlpha(false, life, breathe))
                breathe += 0.02f
            }
            life += 0.005f
        }
        assertTrue("a spark went brighter than Embers' brightest ($peakSpark)",
            peakSpark <= CINDER_ALPHA_SPARK + 1e-6f)
        assertTrue("a flake went brighter than Embers' brightest ($peakFlake)",
            peakFlake <= CINDER_ALPHA_FLAKE + 1e-6f)
        assertEquals("…and the ceiling is actually reached", CINDER_ALPHA_SPARK,
            cinderAlpha(true, 1f, 1f), 1e-6f)
        assertEquals("a dying mote fades out, it does not pop", 0f, cinderAlpha(true, 0f, 1f), 1e-6f)
    }

    @Test fun `the drawn radius can never exceed the spawn radius, at any viewport we ship on`() {
        // THE ARITHMETIC THE WHOLE BUDGET RESTS ON: size-over-life peaks at 0.9385
        // and the top of the swell range is 1.06, so the envelope ceiling is 0.9948
        // — under 1.0 BY CONSTRUCTION.
        var peak = 0f
        var l = 0f
        while (l <= 1f) { peak = maxOf(peak, cinderSizeOverLife(l)); l += 0.0005f }
        assertTrue("size-over-life peaked at $peak", peak in 0.93f..0.94f)
        assertTrue("envelope ceiling ${peak * CINDER_SWELL_MAX} is not under 1.0",
            peak * CINDER_SWELL_MAX < 1f)

        // …and against the parent field: the biggest blob cinders can put over a
        // glyph, versus the biggest Embers puts there. Spawn-radius tables are
        // 2.9+6.7 (cinders) vs 2.5+6.0 (Embers); both bloom the wide pass ×4.2.
        val cindersMaxSpawn = 9.6f
        val embersMaxSpawn = 8.5f
        for (shortEdgeDp in floatArrayOf(348f, 412f, 599f, 712f, 800f)) {
            val g = cinderGeom(shortEdgeDp, shortEdgeDp * 2f)
            val ratio = cindersMaxSpawn * CINDER_SWELL_MAX * peak * g / embersMaxSpawn
            assertTrue("at a ${shortEdgeDp.toInt()} dp short edge cinders draws ${ratio}× Embers' largest sprite",
                ratio <= 1f)
        }
    }

    @Test fun `the geometry multiplier is a function of the dp box only`() {
        assertEquals("a phone-shaped box sits on the floor", 0.5f, cinderGeom(348f, 774f), 1e-6f)
        assertEquals("…and so does anything smaller", 0.5f, cinderGeom(103f, 206f), 1e-6f)
        assertEquals("a big tablet reaches the ceiling and stops", 1f, cinderGeom(968f, 1935f), 1e-6f)
        assertEquals("orientation cannot matter — it is the SHORT edge",
            cinderGeom(800f, 1280f), cinderGeom(1280f, 800f), 0f)
        assertTrue("the multiplier may only ever shrink the field below Embers",
            cinderGeom(4000f, 4000f) <= 1f)
    }

    @Test fun `the colour ramp is indexed by life and stays inside the 6-stop atlas`() {
        val top = 5
        for (hue0 in 0..2) {
            var l = 1f
            while (l > 0f) {
                for (spark in booleanArrayOf(true, false)) {
                    val i = cinderRampIndex(spark, hue0, l, top)
                    assertTrue("ramp index $i escaped the atlas", i in 0..top)
                }
                l -= 0.01f
            }
        }
        // White-hot at birth, deep ember red at burnout — and sparks start hotter
        // and cool over a shorter stretch than flakes.
        assertEquals("a newborn spark is white-hot", 0, cinderRampIndex(true, 0, 1f, top))
        assertTrue("…and cools as it burns down",
            cinderRampIndex(true, 0, 0.01f, top) > cinderRampIndex(true, 0, 1f, top))
        assertTrue("a flake traverses more of the ramp than a spark",
            cinderRampIndex(false, 0, 0.01f, top) > cinderRampIndex(true, 0, 0.01f, top))
        assertTrue("flakes are not all one temperature",
            cinderRampIndex(false, 2, 1f, top) > cinderRampIndex(false, 0, 1f, top))
    }

    // ── 5. Size over life, and the two sub-populations ──

    @Test fun `every mote has a size-over-life curve and its own envelope multiplier`() {
        val swells = live(sim(seed = 17)).map { it.swell }
        // Drawn per mote from a CONTINUOUS range — not one value per sub-population,
        // which is what "no two motes pump by the same amount" actually requires.
        assertTrue("the envelope multiplier is not per-mote (${swells.toSet().size} distinct of ${swells.size})",
            swells.toSet().size > swells.size * 9 / 10)
        assertTrue("swell out of band: ${swells.min()}..${swells.max()}",
            swells.min() >= CINDER_SWELL_MIN && swells.max() <= CINDER_SWELL_MAX)
        assertTrue("the envelope range is too narrow to see",
            swells.max() - swells.min() > 0.2f)

        // The drawn size must actually CHANGE over a mote's life: bloom in as it
        // catches the air, ride, then burn down.
        val radii = floatArrayOf(1f, 0.85f, 0.6f, 0.3f, 0.05f).map { cinderSizeOverLife(it) }
        assertEquals("size is constant over life: $radii", radii.size, radii.toSet().size)
        assertTrue("expected bloom-in", radii[1] > radii[0])
        assertTrue("expected burn-down", radii[4] < radii[2])
    }

    @Test fun `two sub-populations with genuinely different parameters`() {
        val s = sim(seed = 23)
        val sparks = live(s).filter { it.spark }
        val flakes = live(s).filter { !it.spark }
        assertTrue("both populations must exist: ${sparks.size}/${flakes.size}",
            sparks.size > 10 && flakes.size > 100)
        assertTrue("sparks are the small ones — by construction, not on average",
            sparks.maxOf { it.r } < flakes.minOf { it.r })
        assertTrue("sparks carry more sail", sparks.all { it.sail > flakes[0].sail })
        assertTrue("sparks are thrown harder by a gust", sparks.all { it.gain > flakes[0].gain })

        run(s, 120)
        val sv = mean(live(s).filter { it.spark }.map { it.vx })
        val fv = mean(live(s).filter { !it.spark }.map { it.vx })
        assertTrue("sparks must outrun the flakes downwind: $sv vs $fv", sv > fv)
    }

    // ── 6. Frame-rate independence and the DPI law ──

    @Test fun `60 Hz and 120 Hz travel the same path`() {
        // active = false freezes the population, so both runs carry the same motes.
        val a = sim(seed = 3).also { run(it, 120, 1f / 60f, active = false) }
        val b = sim(seed = 3).also { run(it, 240, 1f / 120f, active = false) }
        var worst = 0f
        var pairs = 0
        a.motes.forEachIndexed { i, p ->
            val q = b.motes[i]
            if (p.alive && q.alive) { worst = maxOf(worst, hypot(p.x - q.x, p.y - q.y)); pairs++ }
        }
        assertTrue("the two runs stopped carrying the same field ($pairs pairs)", pairs > 250)
        assertTrue("a 120 Hz screen drifted ${worst}px from a 60 Hz one in 2 s", worst < 12f)
        val va = mean(live(a).map { it.vx })
        val vb = mean(live(b).map { it.vx })
        assertTrue("terminal velocity is frame-rate dependent: $va vs $vb",
            abs(va - vb) < 0.03f * va)
    }

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1× and 2× density: every spatial
        // quantity must come out exactly 2× in pixels, i.e. identical in apparent
        // size. Population and the geometry multiplier are dp-derived, so neither
        // may move at all.
        val lo = sim(seed = 88, w = width, h = height, sc = 1f, d = 3.1f)
        val hi = sim(seed = 88, w = width * 2f, h = height * 2f, sc = 2f, d = 6.2f)
        assertEquals("population is dp-derived, not px-derived", lo.motes.size, hi.motes.size)
        run(lo, 300); run(hi, 300)
        lo.motes.forEachIndexed { i, p ->
            val q = hi.motes[i]
            assertEquals("a slot died on one screen and not the other", p.alive, q.alive)
            if (!p.alive) return@forEachIndexed
            assertEquals("x must be exactly 2× at 2× density", p.x * 2f, q.x, 0.5f)
            assertEquals("y must be exactly 2× at 2× density", p.y * 2f, q.y, 0.5f)
            assertEquals("stored velocity is density-free", p.vx, q.vx, 1e-3f)
            assertEquals("…and so is the vertical", p.vy, q.vy, 1e-3f)
            assertEquals("stored geometry is density-free", p.r, q.r, 1e-4f)
            assertEquals("…and so is the envelope", p.swell, q.swell, 1e-6f)
        }
    }

    // ── 7. Lifecycle, pooling, stability, determinism ──

    @Test fun `the field thins out when idle and the wind refills it on rearm`() {
        val s = sim(seed = 31)
        val n = s.motes.size
        run(s, 300, active = false)                  // 5 s of no generation
        assertTrue("an idle field must drain, not loop forever", live(s).size < n * 6 / 10)
        run(s, 300, active = false)                  // …and 5 s more outlasts the longest life
        assertEquals("nothing may outlive its own decay", 0, live(s).size)
        assertEquals("the pool is fixed-size: slots go dark, they are not removed", n, s.motes.size)
        s.rearm()
        assertEquals("a new turn brings fresh weather", n, live(s).size)
        assertTrue("a refilled field is born already travelling", live(s).all { it.vx > 0f })
    }

    @Test fun `the hot path reuses its slots for the whole session`() {
        val s = sim(seed = 45)
        val slots = s.motes.toList()
        run(s, 1000)
        assertTrue("a recycled mote must reuse its slot object",
            s.motes.withIndex().all { (i, p) -> p === slots[i] })
        s.rearm()
        assertTrue("…and a rearm must not reallocate the pool either",
            s.motes.withIndex().all { (i, p) -> p === slots[i] })
    }

    @Test fun `1000 frames - nothing escapes and nothing goes non-finite`() {
        val s = sim(seed = 101)
        for (f in 1..1000) {
            // a long stall every ~1.6 s, at the engine's dt clamp
            s.update(f * 16.7, if (f % 97 == 0) 0.25f else 1f / 60f, true)
        }
        val l = live(s)
        assertTrue("the field died out entirely", l.size > s.motes.size / 2)
        val band = 20f * scale
        for (p in l) {
            for (v in floatArrayOf(p.x, p.y, p.vx, p.vy, p.life, p.r, p.swell)) {
                assertTrue("a field went non-finite ($v)", v.isFinite())
            }
            assertTrue("escaped horizontally at x=${p.x}", p.x >= -band && p.x <= width + band)
            assertTrue("escaped vertically at y=${p.y}", p.y > -height * 0.1f && p.y < height * 1.2f)
            assertTrue("life out of range: ${p.life}", p.life > 0f && p.life <= 1f)
        }
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 99)
            run(s, 240)
            return live(s).joinToString { "${it.x},${it.y},${it.vx},${it.vy},${it.life},${it.r}" }
        }
        assertEquals(once(), once())
    }
}
