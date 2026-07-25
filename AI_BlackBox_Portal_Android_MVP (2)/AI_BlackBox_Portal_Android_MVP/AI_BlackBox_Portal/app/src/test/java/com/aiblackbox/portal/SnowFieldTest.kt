package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.SNOW_ALPHA_CAP
import com.aiblackbox.portal.ui.components.SNOW_FAR
import com.aiblackbox.portal.ui.components.SNOW_MARGIN
import com.aiblackbox.portal.ui.components.SNOW_MAX_FLAKES
import com.aiblackbox.portal.ui.components.SNOW_MID
import com.aiblackbox.portal.ui.components.SNOW_NEAR
import com.aiblackbox.portal.ui.components.SNOW_PLANES
import com.aiblackbox.portal.ui.components.SNOW_RAMP_STEPS
import com.aiblackbox.portal.ui.components.SNOW_SPRITE_ALPHAS
import com.aiblackbox.portal.ui.components.SNOW_SPRITE_STOPS
import com.aiblackbox.portal.ui.components.SnowFlake
import com.aiblackbox.portal.ui.components.SnowGust
import com.aiblackbox.portal.ui.components.SnowSim
import com.aiblackbox.portal.ui.components.snowAlphaOverLife
import com.aiblackbox.portal.ui.components.snowDrawAlpha
import com.aiblackbox.portal.ui.components.snowFieldScale
import com.aiblackbox.portal.ui.components.snowPlaneCounts
import com.aiblackbox.portal.ui.components.snowRampStop
import com.aiblackbox.portal.ui.components.snowSizeCurve
import com.aiblackbox.portal.ui.components.snowSizeOverLife
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.sign

/**
 * Drift Snow physics — headless, seeded, no Compose / Android graphics.
 *
 * The sim takes its randomness from an injected [FieldRandom] and its clock from
 * accumulated dt, so the whole simulation is drivable from the JVM with a
 * deterministic PRNG. What is asserted here is what would otherwise only be
 * caught by staring at a phone:
 *
 *   - the LEGIBILITY BUDGET: never more than [SNOW_MAX_FLAKES] flakes at any
 *     viewport or dial setting, and a flake never dims body text past WCAG AA.
 *     This is a backdrop behind chat text. NOTE the ceilings are no longer pinned
 *     to the web's literals — the 2026-07-25 brightness pass lifted them for
 *     density (see the header of SnowField.kt), so what is pinned here is the
 *     PROPERTY (bounded lift, web ordering, measured contrast) instead.
 *   - the DEPTH CORRELATION: size, speed, sway, blur and gust response rise
 *     together across the three planes. Break it and parallax collapses to a flat
 *     field of dots.
 *   - gusts are COHERENT (one number moves the whole population) and SETTLE.
 *   - motion is delta-timed: 120 Hz and 60 Hz land in the same place.
 *   - DENSITY CHANGES RESOLUTION, NEVER APPARENT GEOMETRY.
 *   - no NaN after 1000 frames, and nothing escapes the field.
 *
 * Mirrors Portal/modules/fx/effects/snow.test.mjs — keep them in step.
 */
class SnowFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
    private val scale = 1f

    /** Device px per WEB CSS px on the reference device (0.5 apparentScale × 3.1). */
    private val g = 1.55f
    private val margin = SNOW_MARGIN * g

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

    private fun sim(seed: Int = 42, countScale: Float = 1f): SnowSim =
        SnowSim(countScale, seeded(seed)).also { it.resize(width, height, scale, density) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: SnowSim, frames: Int, dt: Float = 1f / 60f, active: Boolean = true) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, active)
    }

    private fun mean(xs: List<Float>): Float = xs.sum() / xs.size

    // ── 1. The legibility budget — the non-negotiable part ──

    @Test fun `population NEVER exceeds the flake budget, at any viewport density`() {
        for (s in floatArrayOf(0.1f, 0.4f, 1f, 1.6f, 2.4f, 6f, 100f, 1000f)) {
            val counts = snowPlaneCounts(s)
            val total = counts.sum()
            assertTrue("scale $s asked for $total flakes (budget $SNOW_MAX_FLAKES)", total <= SNOW_MAX_FLAKES)
            assertTrue("scale $s emptied a depth plane: ${counts.toList()}", counts.all { it >= 1 })
        }
        assertEquals("the shipped population at scale 1", 244, snowPlaneCounts(1f).sum())
        // …and the LIVE field honours it too, not just the planner — across the
        // whole count dial and on a tiny + a huge canvas.
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val n = sim(countScale = cs).flakes.size
            assertTrue("countScale $cs produced $n flakes", n in 3..SNOW_MAX_FLAKES)
        }
        for (w in floatArrayOf(320f, 1080f, 3000f)) {
            val s = SnowSim(ParticleTuning.COUNT_SCALE_MAX, seeded(7))
                .also { it.resize(w, w * 2f, scale, density) }
            assertTrue("a ${w}px canvas produced ${s.flakes.size}", s.flakes.size <= SNOW_MAX_FLAKES)
            assertTrue("a ${w}px canvas emptied the field", s.flakes.size >= 3)
        }
    }

    @Test fun `a garbage scale falls back to 1 instead of emptying or flooding the field`() {
        for (bad in floatArrayOf(Float.NaN, 0f, -3f, Float.NEGATIVE_INFINITY)) {
            assertEquals("scale $bad", 244, snowPlaneCounts(bad).sum())
        }
        // fieldScale itself is total too: a degenerate canvas must not produce NaN.
        assertEquals(1f, snowFieldScale(Float.NaN, 1f), 0f)
        assertEquals(1f, snowFieldScale(0f, 1f), 0f)
        assertTrue(snowFieldScale(1e12f, 3f).isFinite())
    }

    // The WEB's per-plane ceilings. Kept as the REFERENCE the Android numbers are
    // measured against, NOT as the values themselves: web lengths are CSS px at an
    // apparent scale near 1, ours are multiplied by 0.5 x 3.1 and the outer ramp
    // antialiases toward invisibility before alpha is ever applied, so equal
    // literals do not produce an equal apparent result. Brandon, Fold 6, Intensity
    // at maximum (2026-07-25): "while I can see the snow it is just not so bright."
    private val webAlpha = mapOf("far" to 0.30f, "mid" to 0.40f, "near" to 0.34f)

    /** WCAG 2.x relative luminance of an 8-bit sRGB triple. */
    private fun luminance(r: Int, g: Int, b: Int): Double {
        fun ch(v: Int): Double {
            val s = v / 255.0
            return if (s <= 0.03928) s / 12.92 else Math.pow((s + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)
    }

    private fun contrast(a: Double, b: Double): Double =
        (maxOf(a, b) + 0.05) / (minOf(a, b) + 0.05)

    /** #C9C9C9 — the body-text grey this backdrop sits behind. */
    private val bodyTextLuminance = luminance(0xC9, 0xC9, 0xC9)

    /**
     * Area-weighted mean alpha of the flake sprite: ∫2u·α(u)du over the unit disc,
     * α piecewise-linear between the stops. THIS, not the core, is what a glyph
     * sits on — a stroke spans many device px while the sprite core is a single
     * sub-pixel — so it is the number the contrast assertion below uses.
     */
    private fun spriteMeanAlpha(): Double {
        assertEquals("stop/alpha arrays must stay parallel", SNOW_SPRITE_STOPS.size, SNOW_SPRITE_ALPHAS.size)
        var acc = 0.0
        for (i in 0 until SNOW_SPRITE_STOPS.size - 1) {
            val u0 = SNOW_SPRITE_STOPS[i].toDouble()
            val u1 = SNOW_SPRITE_STOPS[i + 1].toDouble()
            val a0 = SNOW_SPRITE_ALPHAS[i].toDouble()
            val a1 = SNOW_SPRITE_ALPHAS[i + 1].toDouble()
            val m = (a1 - a0) / (u1 - u0)
            val c = a0 - m * u0                                     // α(u) = m·u + c
            acc += (2.0 * m / 3.0) * (u1 * u1 * u1 - u0 * u0 * u0) + c * (u1 * u1 - u0 * u0)
        }
        return acc
    }

    @Test fun `the alpha ceilings are a BOUNDED lift over the web, in the web's order`() {
        // A PROPERTY, not three literals. The ceilings moved on 2026-07-25; what may
        // not move is (a) their ordering and (b) how far they are allowed to travel.
        assertTrue(
            "far must stay the faintest and near must stay under mid — near is the big" +
                " blurred plane and is the real contrast risk (${SNOW_FAR.alpha}," +
                " ${SNOW_MID.alpha}, ${SNOW_NEAR.alpha})",
            SNOW_FAR.alpha < SNOW_NEAR.alpha && SNOW_NEAR.alpha < SNOW_MID.alpha,
        )
        for ((name, cfg) in listOf("far" to SNOW_FAR, "mid" to SNOW_MID, "near" to SNOW_NEAR)) {
            val k = cfg.alpha / webAlpha.getValue(name)
            assertTrue(
                "the $name plane (${cfg.alpha}) is DIMMER than the web — the density" +
                    " argument only ever justified going UP",
                k >= 1f,
            )
            assertTrue(
                "the $name plane is ${k}x the web ceiling: that is a redesign, not \"a" +
                    " shade brighter or two\"",
                k <= 1.35f,
            )
            assertTrue("the $name ceiling ${cfg.alpha} is past half-opaque", cfg.alpha <= 0.5f)
        }
        assertEquals(250, SNOW_MAX_FLAKES)
    }

    @Test fun `alpha is a curve over life, and the Intensity dial actually reaches it`() {
        // The per-flake multiplier band is unchanged: a flake fades, it never pops.
        var life = -0.5f
        while (life <= 1.5f) {
            val a = snowAlphaOverLife(life)
            assertTrue("alpha multiplier $a out of 0.10..1.0 at life $life", a >= 0.10f && a <= 1.0f)
            life += 0.01f
        }
        // THE REGRESSION THIS TEST EXISTS FOR. Snow bakes its own SrcOver paint and
        // so bypasses drawSpriteF, which is where every other sprite field picks up
        // FieldPaints.alphaScale — so before 2026-07-25 the Intensity dial only ever
        // added MORE flakes of identical faintness. Brightness must be monotonic in
        // the dial, and the default dial must still be the identity.
        val dim = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MIN)
        val nominal = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_DEFAULT)
        val hot = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MAX)
        assertTrue(
            "the Intensity dial does not brighten snow — alphaScale is not wired into render",
            snowDrawAlpha(SNOW_MID.alpha, 0.5f, dim) < snowDrawAlpha(SNOW_MID.alpha, 0.5f, nominal) &&
                snowDrawAlpha(SNOW_MID.alpha, 0.5f, nominal) < snowDrawAlpha(SNOW_MID.alpha, 0.5f, hot),
        )
        assertEquals(
            "at the default dial the drawn alpha is exactly the plane ceiling",
            SNOW_MID.alpha, snowDrawAlpha(SNOW_MID.alpha, 0.5f, nominal), 1e-6f,
        )
        // The life curve still bounds it from below: 10% of the ceiling, never 0.
        assertEquals(
            SNOW_MID.alpha * 0.10f, snowDrawAlpha(SNOW_MID.alpha, 0f, nominal), 1e-6f,
        )
    }

    @Test fun `no plane, life or dial setting can cross the alpha hard cap`() {
        assertTrue("the cap itself drifted past the contrast argument", SNOW_ALPHA_CAP <= 0.72f)
        val dials = floatArrayOf(
            ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MIN),
            1f,
            ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MAX),
            1.6f,   // brightnessScale's own clamp ceiling, in case INTENSITY_MAX ever rises
            99f,    // and a garbage value: the cap is a guard rail, not an assumption
        )
        for (cfg in SNOW_PLANES) {
            var life = 0f
            while (life <= 1f) {
                for (d in dials) {
                    val a = snowDrawAlpha(cfg.alpha, life, d)
                    assertTrue("drawn alpha $a crossed the cap at life $life, dial $d", a <= SNOW_ALPHA_CAP + 1e-6f)
                }
                life += 0.01f
            }
        }
        // At the SHIPPED ceilings the cap must still be SLACK: it is a guard rail for
        // future edits, not something the current look leans on.
        assertTrue(
            "the shipped field is already sitting on the cap — the lift went too far",
            snowDrawAlpha(SNOW_MID.alpha, 0.5f, ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MAX)) <
                SNOW_ALPHA_CAP,
        )
    }

    @Test fun `the brightest flake still clears WCAG AA behind body text`() {
        // This replaces the old literal ("the brightest flake is still under 0.40
        // alpha"), which was the web's number and not the property that matters.
        // Measured the way the web's 9.5-11.1:1 figures were: over the flake's AREA.
        val mean = spriteMeanAlpha()
        assertTrue("the sprite falloff integrates to $mean — outside any plausible band", mean in 0.15..0.55)
        val hot = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MAX)
        val brightest = SNOW_PLANES.maxOf { it.alpha }
        // Brightest plane, mid-fall (peak of the life curve), dial pinned to maximum.
        val worst = snowDrawAlpha(brightest, 0.5f, hot) * mean
        // SrcOver over black: the composited level IS colour x alpha.
        val stop = snowRampStop(SNOW_RAMP_STEPS / 2)
        fun composite(a: Double) = luminance(
            ((((stop shr 16) and 0xFF)) * a).roundToInt(),
            ((((stop shr 8) and 0xFF)) * a).roundToInt(),
            ((stop and 0xFF) * a).roundToInt(),
        )
        val ratio = contrast(bodyTextLuminance, composite(worst))
        assertTrue("body text drops to $ratio:1 over a flake (AA needs 4.5)", ratio >= 4.5)
        // …and even the single-pixel CORE, at the hard cap, never out-shines the
        // text: a backdrop may never become the brightest thing on screen.
        assertTrue(
            "a flake core is brighter than the body text it sits behind",
            composite(SNOW_ALPHA_CAP.toDouble()) < bodyTextLuminance,
        )
    }

    @Test fun `a phone-shaped viewport sits on the web's viewport floor`() {
        // The web clamps the viewport term to 0.4 (a 1440x900 desktop is 1.0), so
        // a phone ships ~0.4x the desktop population. That IS the shipped look.
        assertEquals(0.4f, snowFieldScale(412f * 915f, 1f), 1e-6f)
        assertEquals(1.0f, snowFieldScale(1440f * 900f, 1f), 1e-6f)
        assertEquals("the viewport term is clamped at both ends", 2.4f, snowFieldScale(4000f * 3000f, 1f), 1e-6f)
        // …and the combined product is clamped again, so no dial can hand the
        // planner a scale it was never tuned against.
        assertTrue(snowFieldScale(412f * 915f, ParticleTuning.COUNT_SCALE_MIN) >= 0.12f)
        assertTrue(snowFieldScale(4000f * 3000f, ParticleTuning.COUNT_SCALE_MAX) <= 4.0f)
    }

    // ── 2. Three sub-populations, correlated into depth ──

    @Test fun `three depth planes, each populated`() {
        val s = sim()
        assertEquals(3, s.planes.size)
        assertEquals(3, SNOW_PLANES.size)
        s.planes.forEachIndexed { i, p -> assertTrue("plane $i is empty", p.list.isNotEmpty()) }
    }

    @Test fun `size, fall speed, sway and gust response all rise together far to near`() {
        val s = sim(seed = 11)
        val r = s.planes.map { mean(it.list.map { f -> f.r }) }
        val vy = s.planes.map { mean(it.list.map { f -> f.vy }) }
        val sway = s.planes.map { mean(it.list.map { f -> f.swayAmp }) }
        val gust = s.planes.map { it.cfg.gust }
        val soft = s.planes.map { it.cfg.soft }
        for ((name, v) in listOf("r" to r, "vy" to vy, "sway" to sway, "gust" to gust, "soft" to soft)) {
            assertTrue(
                "$name is not monotonic across the planes ($v) — the depth illusion IS this correlation",
                v[0] < v[1] && v[1] < v[2],
            )
        }
    }

    // ── 3. Motion: down, fluttering, gusting coherently ──

    @Test fun `every flake falls DOWNWARD and only ever moves up by recycling`() {
        val s = sim(seed = 3)
        assertTrue("a flake is rising", s.flakes.all { it.vy > 0f })
        var advanced = 0
        for (i in 1..30) {
            val before = s.flakes.map { it.y }
            run(s, 1)
            s.flakes.forEachIndexed { k, f ->
                if (f.y > before[k]) advanced++
                else assertTrue("flake $k moved up without recycling (${before[k]} -> ${f.y})", f.y < 0f)
            }
        }
        assertTrue("too few downward steps to judge ($advanced)", advanced > 2500)
    }

    @Test fun `flutter oscillates — a flake's horizontal drift reverses repeatedly`() {
        val s = sim(seed = 6)
        s.gustWait = 1e9f                              // isolate flutter from the gust
        val f = s.planes[2].list[0]
        var last = f.x
        var signs = 0
        var prev = 0f
        for (i in 1..480) {                            // 8 s
            run(s, 1)
            val d = sign(f.x - last)
            if (d != 0f && prev != 0f && d != prev) signs++
            if (d != 0f) prev = d
            last = f.x
        }
        assertTrue("flutter never reversed ($signs reversals) — that is a rail, not a sway", signs >= 2)
        assertEquals("no gust should have fired in this window", 0f, s.gust, 0f)
    }

    @Test fun `a gust pushes the WHOLE field the same way, then settles back to calm`() {
        val s = sim(seed = 23)
        var peak = 0f
        var peakDx = 0f
        for (i in 1..3600) {                           // 60 s: several gust cycles
            val before = s.flakes.map { it.x }
            run(s, 1)
            if (abs(s.gust) > abs(peak)) {
                peak = s.gust
                val after = s.flakes.map { it.x }
                // Filter recycles/wraps: those teleport x and are not drift.
                val d = after.mapIndexed { k, x -> x - before[k] }.filter { abs(it) < 100f }
                peakDx = mean(d)
            }
        }
        assertTrue("no real gust in 60 s (peak $peak)", abs(peak) > 15f)
        assertEquals(
            "the field drifted against the gust (gust $peak, mean dx $peakDx) — coherence is the whole point",
            sign(peak), sign(peakDx), 0f,
        )
        // Let it settle: no new gust can start for a very long time.
        s.gustWait = 1e9f; s.gustHold = 0f; s.gustTarget = 0f
        run(s, 300)
        assertTrue("gust never settled (${s.gust})", abs(s.gust) < 1f)
    }

    // ── 4. The two premium curves ──

    @Test fun `size-over-life is a curve, not a constant, and closes at both ends`() {
        assertEquals(0f, snowSizeCurve(0f), 0f)
        assertEquals(0f, snowSizeCurve(1f), 0f)
        assertEquals("full size through the middle of the fall", 1f, snowSizeCurve(0.5f), 1e-6f)
        var l = 0f
        while (l < 0.17f) {
            assertTrue("swell-in must be monotonic", snowSizeCurve(l) < snowSizeCurve(l + 0.01f))
            l += 0.01f
        }
        l = 0.79f
        while (l < 0.98f) {
            assertTrue("taper-out must be monotonic", snowSizeCurve(l) > snowSizeCurve(l + 0.01f))
            l += 0.01f
        }
        assertEquals("clamped outside 0..1", 0f, snowSizeCurve(-5f), 0f)
        assertEquals("clamped outside 0..1", 0f, snowSizeCurve(9f), 0f)
        // The drawn radius never collapses to zero and never exceeds the flake's own.
        assertEquals(0.62f, snowSizeOverLife(0f), 1e-6f)
        assertEquals(1.0f, snowSizeOverLife(0.5f), 1e-6f)
    }

    @Test fun `each flake carries its own envelope multiplier and its own colour bias`() {
        val s = sim(seed = 17)
        val list = s.flakes
        val envs = list.map { it.sizeEnv }
        val biases = list.map { it.bias }
        assertTrue(
            "the curve is shared, the AMPLITUDE is not: only ${envs.toSet().size} distinct of ${list.size}",
            envs.toSet().size > list.size * 0.9,
        )
        assertTrue("envelope out of band: ${envs.min()}..${envs.max()}", envs.all { it >= 0.78f && it <= 1.28f })
        assertTrue(
            "the ramp must be de-banked per flake: only ${biases.toSet().size} distinct biases",
            biases.toSet().size > list.size * 0.9,
        )
        assertTrue("bias out of band: ${biases.min()}..${biases.max()}", biases.all { abs(it) <= 0.14f })
    }

    @Test fun `colour is a life-indexed ramp, not a flat white`() {
        assertEquals(48, SNOW_RAMP_STEPS)
        val lut = (0 until SNOW_RAMP_STEPS).map { snowRampStop(it) }
        assertTrue("ramp is flat (${lut.toSet().size} distinct stops)", lut.toSet().size > 20)
        assertTrue("the ramp does not travel", lut[0] != lut[24] && lut[24] != lut[47])
        // Moonlit steel-blue high → near-white mid-fall → dusty settle low: every
        // stop is a real 8-bit colour and blue never falls below red (it is snow,
        // not an ember).
        for (i in lut.indices) {
            val r = (lut[i] shr 16) and 0xFF
            val gc = (lut[i] shr 8) and 0xFF
            val b = lut[i] and 0xFF
            assertTrue("stop $i out of 8-bit range", r in 0..255 && gc in 0..255 && b in 0..255)
            assertTrue("stop $i is warm — snow must read cold", b >= r)
            // The COLD ENDS were lifted toward white on 2026-07-25 (the web's
            // rgb(150,176,210) head composited to about rgb(38,44,53) at the quarter
            // alpha a fading-in flake carries — grey dust, not snow). No stop may
            // fall back into that band: "the white particles could be a shade
            // brighter or two" — Brandon, Fold 6.
            assertTrue("stop $i (${r},${gc},${b}) is dark enough to read as dust, not snow", gc >= 190)
        }
        // Out-of-range indices clamp instead of throwing.
        assertEquals(lut[0], snowRampStop(-4))
        assertEquals(lut[47], snowRampStop(999))
    }

    // ── 5. Frame-rate, DPI and numerical safety ──

    @Test fun `motion is delta-timed — 120 Hz lands where 60 Hz lands`() {
        // A FRESH generator per field — sharing one instance would desync the two
        // initial scatters and this test would be measuring nothing. A tall canvas
        // keeps most flakes falling (a recycle legitimately diverges the runs).
        fun tall(seed: Int) = SnowSim(1f, seeded(seed)).also { it.resize(1080f, 4000f, scale, density) }
        val a = tall(42).also { run(it, 60, 1f / 60f) }.flakes
        val b = tall(42).also { run(it, 120, 1f / 120f) }.flakes
        assertEquals(a.size, b.size)
        var compared = 0
        for (i in a.indices) {
            // Skip anything that recycled: respawning draws from rand, so the two
            // runs legitimately diverge for that flake (a recycled flake is above
            // the top edge, i.e. y < 0).
            if (a[i].y < 0f || b[i].y < 0f) continue
            assertEquals("flake $i y", a[i].y, b[i].y, 0.5f)
            // x carries the sway integral, whose rectangle-rule error scales with
            // dt; the web's 1.0 px bound × our 3.1x larger geometry factor.
            assertEquals("flake $i x", a[i].x, b[i].x, 3.5f)
            compared++
        }
        assertTrue("only compared $compared flakes", compared > 60)
    }

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1x and 2x density: every spatial
        // quantity must come out exactly 2x in pixels, i.e. identical in apparent
        // size. Population is dp-derived, so it must NOT change.
        val lo = SnowSim(1f, seeded(88)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = SnowSim(1f, seeded(88)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("population is dp-derived, not px-derived", lo.flakes.size, hi.flakes.size)
        run(lo, 300); run(hi, 300)
        assertEquals("the gust is a density-free web-px velocity", lo.gust, hi.gust, 1e-4f)
        val la = lo.flakes
        val ha = hi.flakes
        la.forEachIndexed { i, p ->
            val q = ha[i]
            assertEquals("x must be exactly 2x at 2x density", p.x * 2f, q.x, 0.5f)
            assertEquals("y must be exactly 2x at 2x density", p.y * 2f, q.y, 0.5f)
            assertEquals("stored radius is density-free", p.r, q.r, 1e-4f)
            assertEquals("stored fall speed is density-free", p.vy, q.vy, 1e-4f)
            assertEquals("stored sway is density-free", p.swayAmp, q.swayAmp, 1e-4f)
            assertEquals("…and so is the flutter", p.phase, q.phase, 1e-4f)
            assertEquals("…and the life index", p.life, q.life, 1e-4f)
        }
    }

    @Test fun `1000 frames of mixed frame times produce no NaN and nothing escapes`() {
        val s = sim(seed = 77)
        val dts = floatArrayOf(1f / 60f, 1f / 120f, 1f / 30f, 0.05f, 1f / 144f)
        for (i in 0 until 1000) run(s, 1, dts[i % dts.size])
        for (f in s.flakes) {
            for (v in floatArrayOf(f.x, f.y, f.r, f.vy, f.swayAmp, f.swayW, f.phase, f.sizeEnv, f.bias, f.life)) {
                assertTrue("a field went non-finite ($v)", v.isFinite())
            }
            assertTrue("life out of range: ${f.life}", f.life >= 0f && f.life <= 1f)
            assertTrue("x escaped: ${f.x}", f.x >= -margin - 1f && f.x <= width + margin + 1f)
            assertTrue(
                "y escaped: ${f.y}",
                f.y >= -margin - height * 0.2f - 1f && f.y <= height + margin + 1f,
            )
        }
        assertTrue(s.gust.isFinite())
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 5)
            run(s, 200)
            return s.flakes.joinToString { "${it.x},${it.y},${it.life}" }
        }
        assertEquals(once(), once())
    }

    // ── 6. Lifecycle ──

    @Test fun `the hot path reuses its slots for the whole session`() {
        val s = sim(seed = 45)
        val ids = s.flakes
        run(s, 3600)
        assertTrue(
            "a recycled flake reuses its slot object",
            s.flakes.withIndex().all { (i, f) -> f === ids[i] },
        )
    }

    @Test fun `an idle field keeps falling — snow is not generation-gated`() {
        // Deliberate divergence from fireflies/stars: the pool is fixed and nothing
        // spawns on demand, so there is nothing to gate. A backdrop that froze
        // mid-fall between turns would read as a hung screen.
        val s = sim(seed = 31)
        val n = s.flakes.size
        val y0 = s.flakes.map { it.y }
        for (f in 1..600) s.update(f * 16.6667, 1f / 60f, false)
        assertEquals("the pool is fixed-size and never thins out", n, s.flakes.size)
        assertTrue(
            "an idle field must keep falling",
            s.flakes.withIndex().count { (i, f) -> f.y != y0[i] } > n * 0.9,
        )
    }

    @Test fun `rearm re-arms the gust clock instead of firing one the instant the app returns`() {
        val s = sim(seed = 9)
        run(s, 600)
        val slots = s.flakes
        s.gustWait = 0.01f
        s.rearm()
        assertTrue("gust was due immediately (${s.gustWait})", s.gustWait >= SnowGust.WAIT_MIN)
        assertTrue(s.gustWait <= SnowGust.WAIT_MAX)
        assertEquals(0f, s.gust, 0f)
        assertEquals(0f, s.gustTarget, 0f)
        // rearm must not disturb the field itself — there is nothing to revive.
        assertEquals(slots.size, s.flakes.size)
        assertTrue(s.flakes.withIndex().all { (i, f) -> f === slots[i] })
    }

    @Test fun `resize keeps the field rather than re-scattering it on every composer keystroke`() {
        val s = sim(seed = 12)
        run(s, 60)
        val slots: List<SnowFlake> = s.flakes
        val before = slots.map { it.y }
        s.resize(width, height - 80f, scale, density)      // composer grew by one line
        val after = s.flakes
        assertEquals("population changed on a trivial resize", before.size, after.size)
        assertTrue("slots were reallocated", after.withIndex().all { (i, f) -> f === slots[i] })
        after.forEachIndexed { i, f -> assertEquals("flakes teleported on resize", before[i], f.y, 0f) }
        // A no-op resize (the every-frame case) must be a pure early return.
        s.resize(width, height - 80f, scale, density)
        assertTrue(s.flakes.withIndex().all { (i, f) -> f === slots[i] })
        // A narrower viewport must pull stranded flakes back inside.
        s.resize(500f, height - 80f, scale, density)
        assertTrue("a flake was stranded off the right edge", s.flakes.all { it.x <= 500f })
    }

    @Test fun `the planes differ on every visible axis`() {
        assertTrue("near flakes are bigger", SNOW_FAR.sizeMax < SNOW_MID.sizeMin && SNOW_MID.sizeMax < SNOW_NEAR.sizeMin)
        assertTrue("near flakes fall faster", SNOW_FAR.fallMax < SNOW_MID.fallMin && SNOW_MID.fallMax < SNOW_NEAR.fallMin)
        assertTrue("near flakes are blurrier", SNOW_FAR.soft < SNOW_MID.soft && SNOW_MID.soft < SNOW_NEAR.soft)
        assertTrue("near flakes ride the gust hardest", SNOW_FAR.gust < SNOW_MID.gust && SNOW_MID.gust < SNOW_NEAR.gust)
        assertTrue("the far plane is the most populous", SNOW_FAR.count > SNOW_MID.count && SNOW_MID.count > SNOW_NEAR.count)
    }
}
