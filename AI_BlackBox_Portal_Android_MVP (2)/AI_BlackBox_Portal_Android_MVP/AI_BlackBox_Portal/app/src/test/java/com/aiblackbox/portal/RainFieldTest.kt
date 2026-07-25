package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.RAIN_BOT_BAND
import com.aiblackbox.portal.ui.components.RAIN_HEAVY
import com.aiblackbox.portal.ui.components.RAIN_LEAN
import com.aiblackbox.portal.ui.components.RAIN_LEAN_JITTER
import com.aiblackbox.portal.ui.components.RAIN_LEAN_SHEAR
import com.aiblackbox.portal.ui.components.RAIN_MAX_DROPS
import com.aiblackbox.portal.ui.components.RAIN_MAX_STROKE_DEVICE_PX
import com.aiblackbox.portal.ui.components.RAIN_MIN_DROPS
import com.aiblackbox.portal.ui.components.RAIN_MIN_STROKE_DEVICE_PX
import com.aiblackbox.portal.ui.components.RAIN_NEAR_ALPHA_CEILING
import com.aiblackbox.portal.ui.components.RAIN_PLANES
import com.aiblackbox.portal.ui.components.RAIN_RAMP
import com.aiblackbox.portal.ui.components.RAIN_RAMP_TOP
import com.aiblackbox.portal.ui.components.RAIN_SPAWN_MARGIN
import com.aiblackbox.portal.ui.components.RAIN_WEB_TO_REF_PX
import com.aiblackbox.portal.ui.components.RainDrop
import com.aiblackbox.portal.ui.components.RainSim
import com.aiblackbox.portal.ui.components.rainAlphaEnvelope
import com.aiblackbox.portal.ui.components.rainDrawnAlpha
import com.aiblackbox.portal.ui.components.rainLengthEnvelope
import com.aiblackbox.portal.ui.components.rainPlaneCount
import com.aiblackbox.portal.ui.components.rainPopulationScale
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * Rainfall physics — headless, seeded, no Compose / Android graphics.
 *
 * The sim takes its randomness from an injected [FieldRandom], so the whole
 * simulation is drivable from the JVM with a deterministic PRNG. Most of what is
 * asserted here cannot be eyeballed reliably in a backdrop that is deliberately
 * 20% opaque — above all the LEGIBILITY BUDGET. Vertical streaks run parallel to
 * the message column and the scroll direction, so the lean angle and the near
 * plane's alpha are load-bearing, not taste. They are pinned as numbers so a
 * later "let's make it rain harder" tweak fails the build instead of quietly
 * costing the chat text its contrast.
 *
 * Mirrors Portal/modules/fx/effects/rain.test.mjs — keep them in step.
 */
class RainFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
    private val scale = 1f

    /** Web CSS px → device px on this fixture — the geometry factor every stored
     *  length passes through, and the ONLY thing density is allowed to move. */
    private val g = scale * RAIN_WEB_TO_REF_PX

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

    private fun sim(seed: Int = 42, countScale: Float = 1f): RainSim =
        RainSim(countScale, seeded(seed)).also { it.resize(width, height, scale, density) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: RainSim, frames: Int, dt: Float = 1f / 60f, active: Boolean = true) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, active)
    }

    private fun live(s: RainSim): List<RainDrop> = s.drops.filter { !it.dead }

    /** The drawn geometry: head → tail, in device px. */
    private fun streakLen(p: RainDrop): Float = hypot(p.x - p.tx, p.y - p.ty)

    private fun expectedCounts(countScale: Float = 1f, w: Float = width, h: Float = height): IntArray {
        val d = rainPopulationScale(countScale, w, h, density)
        return IntArray(RAIN_PLANES.size) { rainPlaneCount(it, d) }
    }

    // ── 1. The pool ──

    @Test fun `the pool is fixed-size and is never reallocated during a run`() {
        val s = sim(seed = 5)
        val ids = s.drops.toList()
        run(s, 600)
        assertEquals("the pool changed size mid-run", ids.size, s.drops.size)
        assertTrue(
            "a recycled drop must reuse its slot object — a long session cannot hand the GC a field a second",
            s.drops.withIndex().all { (i, p) -> p === ids[i] },
        )
        assertTrue(
            "recycling must not reshuffle the depth planes",
            s.drops.withIndex().all { (i, p) -> p.plane == ids[i].plane },
        )
    }

    @Test fun `population matches the configured plane counts and never leaks`() {
        val s = sim()
        val want = expectedCounts()
        assertEquals(want.sum(), s.drops.size)
        for (li in RAIN_PLANES.indices) {
            assertEquals("plane $li", want[li], s.drops.count { it.plane == li })
        }
        assertTrue("the hard cap must hold", s.drops.size <= RAIN_MAX_DROPS)
        run(s, 500)
        assertEquals("drops leaked or vanished while active", s.drops.size, live(s).size)
    }

    @Test fun `population stays between its floor and its cap across the whole dial`() {
        val dials = floatArrayOf(
            ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX,
        )
        val boxes = arrayOf(
            floatArrayOf(320f, 640f),        // the smallest phone we ship to
            floatArrayOf(1080f, 2400f),      // the reference phone
            floatArrayOf(3000f, 6000f),      // an unfolded tablet at high density
        )
        for (cs in dials) for (box in boxes) {
            val s = RainSim(cs, seeded(7)).also { it.resize(box[0], box[1], scale, density) }
            val n = s.drops.size
            assertTrue(
                "dial $cs on ${box[0]}x${box[1]} produced $n drops",
                n in RAIN_MIN_DROPS..RAIN_MAX_DROPS,
            )
            // The cap must never be paid for by deleting a whole depth — that is
            // rain.js's layer-truncating bug, and it costs the NEAR plane first.
            for (li in RAIN_PLANES.indices) {
                assertTrue("plane $li emptied at dial $cs", s.drops.any { it.plane == li })
            }
        }
    }

    @Test fun `the count dial is real — doubling countScale doubles the field`() {
        val a = sim(countScale = 0.5f).drops.size
        val b = sim(countScale = 1f).drops.size
        assertTrue("expected ~2x, got $a -> $b", b > a * 1.8 && b < a * 2.2)
    }

    // ── 2. Motion ──

    @Test fun `every drop falls DOWN and leans RIGHT — the field moves as one`() {
        val s = sim(seed = 11)
        val dt = 1f / 60f
        run(s, 120, dt)
        val ps = live(s)
        val x0 = ps.map { it.x }
        val y0 = ps.map { it.y }
        s.update(121 * dt * 1000.0, dt, true)
        ps.forEachIndexed { i, p ->
            val dy = p.y - y0[i]
            val dx = p.x - x0[i]
            if (dy < 0f) return@forEachIndexed                  // recycled to the top band
            if (abs(dx) > width / 2f) return@forEachIndexed     // wrapped horizontally
            assertTrue("a drop moved upward", dy > 0f)
            assertTrue("a drop leaned against the wind", dx > 0f)
        }
    }

    @Test fun `depth reads as speed AND as streak length — near beats mid beats far`() {
        val s = sim(seed = 13)
        run(s, 240)
        val byPlane = RAIN_PLANES.indices.map { li ->
            live(s).filter { it.plane == li && it.u > 0.2f && it.u < 0.8f }
        }
        byPlane.forEachIndexed { li, ps -> assertTrue("plane $li had no mid-fall drops", ps.isNotEmpty()) }
        val speeds = byPlane.map { ps -> ps.map { it.vy }.average() }
        val lens = byPlane.map { ps -> ps.map { streakLen(it).toDouble() }.average() }
        assertTrue("speeds not ordered: $speeds", speeds[2] > speeds[1] && speeds[1] > speeds[0])
        // Length is DERIVED from velocity (velocity-stretched billboard), so this
        // ordering is a CONSEQUENCE of the speeds, not a second authored parameter.
        assertTrue("lengths not ordered: $lens", lens[2] > lens[1] && lens[1] > lens[0])
    }

    @Test fun `motion is delta-timed — 60 Hz and 120 Hz cover the same ground`() {
        val a = sim(seed = 3).also { run(it, 60, 1f / 60f) }
        val b = sim(seed = 3).also { run(it, 120, 1f / 120f) }
        assertEquals(a.drops.size, b.drops.size)
        // Per-slot, not centroid: a slot whose recycle straddled a frame boundary
        // differently consumes the shared PRNG out of step and lands anywhere, so
        // it is excluded. Everything that did NOT recycle is pure integration and
        // must agree to a pixel. A field running at 2x speed on a 120 Hz panel
        // agrees on nothing.
        val agree = a.drops.indices.count { abs(a.drops[it].y - b.drops[it].y) < 3f }
        assertTrue(
            "only $agree of ${a.drops.size} slots stayed in step over one second",
            agree > a.drops.size * 0.6,
        )
    }

    // ── 3. THE LEGIBILITY BUDGET ──

    @Test fun `the near plane is under the alpha ceiling BY CONSTRUCTION`() {
        val near = RAIN_PLANES.last()
        assertTrue(
            "the brightest possible drop is configured over the ceiling",
            near.alpha * RAIN_HEAVY.alpha < RAIN_NEAR_ALPHA_CEILING,
        )
        assertTrue(
            "planes must get brighter with proximity",
            RAIN_PLANES[0].alpha < RAIN_PLANES[1].alpha && RAIN_PLANES[1].alpha < near.alpha,
        )
        assertEquals("the far haze plane must never spawn a heavy drop", 0f, RAIN_PLANES[0].heavy, 0f)
    }

    @Test fun `no drop EVER exceeds the alpha ceiling, over 1000 frames`() {
        val s = sim(seed = 23)
        var peak = 0f
        for (f in 1..1000) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (p in s.drops) if (!p.dead && p.a > peak) peak = p.a
        }
        assertTrue("peak alpha $peak > $RAIN_NEAR_ALPHA_CEILING", peak <= RAIN_NEAR_ALPHA_CEILING)
        assertTrue("the field faded to nothing (peak $peak) — check the envelope", peak > 0.15f)
    }

    @Test fun `the lean stays inside 10-15 degrees — never parallel to the text column`() {
        // By construction first, so a config edit fails before the simulation does.
        assertTrue(RAIN_LEAN - RAIN_LEAN_SHEAR - RAIN_LEAN_JITTER >= 10f)
        assertTrue(RAIN_LEAN + RAIN_LEAN_SHEAR + RAIN_LEAN_JITTER <= 15f)
        val s = sim(seed = 29)
        var lo = 90f
        var hi = 0f
        for (f in 1..1000) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (p in s.drops) {
                if (p.dead) continue
                if (p.lean < lo) lo = p.lean
                if (p.lean > hi) hi = p.lean
            }
        }
        assertTrue("lean fell to $lo deg — too close to vertical", lo >= 10f)
        assertTrue("lean reached $hi deg — reads as a diagonal wipe", hi <= 15f)
        assertTrue("the wind never actually moved (lo=$lo hi=$hi)", hi - lo > 1.5f)
    }

    @Test fun `drops are NOT all parallel — the per-column shear is doing work`() {
        val s = sim(seed = 31)
        run(s, 200)
        val leans = live(s).map { it.lean }
        val spread = leans.max() - leans.min()
        assertTrue("only $spread deg of lean spread — reads as a hatch pattern", spread > 1.5f)
    }

    @Test fun `alpha-weighted ink over the viewport stays under the backdrop budget`() {
        // The real legibility number: how much light the field puts between the
        // page and the reader. sum(length × width × alpha) / viewport area.
        //
        // BOUNDS MOVED 2026-07-25 with the ANDROID PRESENCE BOOST (see the note
        // above RAIN_PLANES). They used to be 0.0001..0.0009 around a modelled
        // 0.033%, which was HALF the web's own 0.059% — and the web is the field
        // the 9.5–11.1:1 contrast against #C9C9C9 body text was measured on. Half
        // of a comfortably-legible field is not a safety margin, it is an
        // invisible field, which is what Brandon reported on the Fold. The
        // shipped tuning now models at ~0.050%: still UNDER the web's, i.e. still
        // inside the measured-safe envelope, with the ceiling left at ~2x it so a
        // tweak that doubles the ink from here still fails the build.
        //
        // This is a BUDGET, deliberately loose on both sides. The exact guard
        // against someone "restoring web parity" and re-thinning the strokes is
        // `every plane strokes wide enough to read as a line` below, which is
        // pure arithmetic on the constants and cannot drift with the seed.
        val s = sim(seed = 37)
        run(s, 300)
        var ink = 0.0
        for (p in live(s)) ink += streakLen(p).toDouble() * (p.wid * g) * p.a
        val coverage = ink / (width * height)
        assertTrue("ink coverage ${coverage * 100}% — too loud for a backdrop", coverage < 0.0011)
        assertTrue("ink coverage ${coverage * 100}% — the field is invisible", coverage > 0.0003)
    }

    @Test fun `every plane strokes wide enough to read as a line, and never as a bar`() {
        // THE PRESENCE FLOOR. Canvas coordinates are DEVICE px, and below roughly
        // 1.5 of them antialiasing spreads a stroke across two pixel columns at a
        // third coverage each — then the plane's alpha multiplies THAT. The far
        // plane used to be 0.55 web px = 0.85 device px, i.e. half the population
        // rendering as noise rather than as rain. Asserted as a PROPERTY (device
        // px at the reference density) rather than as the literal web numbers,
        // because the web numbers are exactly what does not survive × 0.5 × 3.1.
        for (li in RAIN_PLANES.indices) {
            val px = RAIN_PLANES[li].width * RAIN_WEB_TO_REF_PX
            assertTrue(
                "plane $li strokes at $px device px — under the $RAIN_MIN_STROKE_DEVICE_PX px " +
                    "floor, it will antialias into noise",
                px >= RAIN_MIN_STROKE_DEVICE_PX,
            )
        }
        // …and the other end: the widest stroke the config can produce is a near
        // plane heavy drop. Past this it stops being a streak and starts being a
        // bar sitting behind a glyph stem.
        val widest = RAIN_PLANES.last().width * RAIN_HEAVY.width * RAIN_WEB_TO_REF_PX
        assertTrue(
            "the heaviest near drop strokes at $widest device px — that is a bar, not rain",
            widest < RAIN_MAX_STROKE_DEVICE_PX,
        )
        // Width still reinforces depth. It is no longer the 1:1.64:2.64 spread the
        // web authors (lifting the far plane over the floor compressed it to
        // ~1:1.30:1.90) — depth is carried by SPEED → streak length here, and that
        // ordering is asserted from the simulation above. Width must still ORDER
        // with depth, it just no longer has to carry it alone.
        assertTrue(
            "widths must deepen with proximity",
            RAIN_PLANES[0].width < RAIN_PLANES[1].width &&
                RAIN_PLANES[1].width < RAIN_PLANES[2].width,
        )
    }

    @Test fun `the Intensity dial reaches the stroke alpha, and cannot blow past the text`() {
        // Rain is a LINE field: it bakes its own stroke paint and never calls
        // drawSpriteF, which is where every sprite-based effect picks up
        // FieldPaints.alphaScale for free. So the dial only reaches it through
        // rainDrawnAlpha, and if that ever stops being true the Intensity slider
        // silently goes back to adding drops of identical faintness — which is
        // exactly what Brandon hit ("scale all the way up and I just barely can
        // see the rain"). Both render tiers go through this one function.
        val near = RAIN_PLANES.last().alpha * RAIN_HEAVY.alpha

        // The default dial is a NO-OP: the boost is opt-in, not a silent re-tune.
        assertEquals(
            "the default dial must draw the stored alpha unchanged",
            near, rainDrawnAlpha(near, ParticleTuning.brightnessScale(1f)), 1e-6f,
        )

        val up = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MAX)
        val down = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MIN)
        assertTrue("turning Intensity up must actually brighten the rain", up > 1.3f)
        assertTrue(
            "the dial does not reach the stroke — the slider is count-only again",
            rainDrawnAlpha(near, up) > near * 1.3f,
        )
        assertTrue("turning Intensity down must dim it", rainDrawnAlpha(near, down) < near)

        // The ceiling the dial can reach. RAIN_NEAR_ALPHA_CEILING governs the
        // field at the DEFAULT dial (asserted on the simulation elsewhere); this
        // is the absolute brightest alpha that can ever hit the canvas, and it has
        // to stay far enough under opaque that slate-blue streaks behind #C9C9C9
        // body text are still a backdrop. rain.js applies the same term (`inten`).
        val brightest = rainDrawnAlpha(near, up)
        assertTrue("the dial can drive rain to $brightest alpha — that is not a backdrop", brightest < 0.40f)

        // Soundness: alpha is a fraction, and a corrupt dial must degrade to the
        // stored alpha rather than paint a NaN (which strokes as garbage).
        assertEquals(1f, rainDrawnAlpha(0.9f, 4f), 1e-6f)
        assertEquals(near, rainDrawnAlpha(near, Float.NaN), 1e-6f)
        assertEquals(0f, rainDrawnAlpha(0f, up), 0f)
    }

    // ── 4. The three premium rules ──

    @Test fun `(a) size over life is a CURVE with a per-drop envelope multiplier`() {
        assertTrue("no taper-in", rainLengthEnvelope(0f) < rainLengthEnvelope(0.3f))
        assertTrue("no taper-out", rainLengthEnvelope(1f) < rainLengthEnvelope(0.3f))
        assertEquals("the readable middle must hold full length", 1f, rainLengthEnvelope(0.5f), 1e-5f)
        assertEquals("drops must not pop IN at the top edge", 0f, rainAlphaEnvelope(0f), 0f)
        assertEquals("…nor OUT at the bottom", 0f, rainAlphaEnvelope(1f), 0f)
        assertEquals("…and must be at full alpha in between", 1f, rainAlphaEnvelope(0.5f), 1e-5f)
        // Every drop carries its OWN length envelope depth, drawn continuously.
        val muls = sim(seed = 41).drops.map { it.lenMul }
        assertEquals("length envelopes must be per-drop, not per-plane", muls.size, muls.toSet().size)
    }

    @Test fun `(b) colour is a ramp indexed by life, in bounds, and actually moves`() {
        val s = sim(seed = 43)
        run(s, 400)
        val seen = HashSet<Int>()
        for (p in live(s)) {
            assertTrue("ramp index ${p.ramp} out of range", p.ramp in 0..RAIN_RAMP_TOP)
            seen.add(p.ramp)
        }
        assertTrue("only ${seen.size} ramp stops in play — this is a flat colour", seen.size >= 4)
        // One drop must deepen as it falls: same tint, later in life, later stop.
        val p = live(s).first()
        val early = p.tint + (0.05f * 3f + 0.5f).toInt()
        val late = p.tint + (0.95f * 3f + 0.5f).toInt()
        assertTrue("the ramp does not track fall progress", late > early)
        // The ramp must actually be a ramp: 7 stops, pale blue-white deepening to
        // slate, every channel a legal byte. A drop's tint can add 3 and `u` a
        // further 3, so 7 is the shallowest table that never saturates on stop 0.
        assertEquals(7, RAIN_RAMP.size)
        for (c in RAIN_RAMP) {
            assertEquals("a ramp stop is not an RGB triple", 3, c.size)
            for (ch in c) assertTrue("ramp channel $ch out of byte range", ch in 0..255)
        }
        for (i in 1 until RAIN_RAMP.size) {
            assertTrue(
                "the ramp must deepen monotonically, stop $i does not",
                RAIN_RAMP[i].sum() < RAIN_RAMP[i - 1].sum(),
            )
        }
    }

    @Test fun `(c) three depth planes plus a heavy sub-population are all present`() {
        val s = sim(seed = 47)
        var everHeavy = 0
        var heavyInHazePlane = 0
        var peakHeavyShare = 0f
        for (f in 1..300) {
            s.update(f * 16.6667, 1f / 60f, true)
            val ps = live(s)
            val heavies = ps.count { it.heavy }
            if (heavies > 0) everHeavy++
            heavyInHazePlane += ps.count { it.heavy && it.plane == 0 }
            peakHeavyShare = max(peakHeavyShare, heavies.toFloat() / ps.size)
        }
        for (li in RAIN_PLANES.indices) {
            assertTrue("plane $li is empty", live(s).any { it.plane == li })
        }
        assertTrue("no heavy drops spawned in five seconds", everHeavy > 0)
        assertEquals("heavy drops leaked into the far haze plane", 0, heavyInHazePlane)
        assertTrue("heavy drops are supposed to be occasional ($peakHeavyShare)", peakHeavyShare < 0.25f)
        // …and the planes are genuinely different animals, not one plane resized.
        assertTrue("near must fall fastest", RAIN_PLANES[2].speedMin > RAIN_PLANES[1].speedMax)
        assertTrue("mid must outrun the haze", RAIN_PLANES[1].speedMin > RAIN_PLANES[0].speedMax)
        assertTrue(
            "a heavy drop is bigger and faster, not brighter",
            RAIN_HEAVY.width > 1.2f && RAIN_HEAVY.speed > 1f &&
                RAIN_PLANES[2].alpha * RAIN_HEAVY.alpha < RAIN_NEAR_ALPHA_CEILING,
        )
    }

    // ── 5. The DPI law and soundness ──

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1x and 2x density: every
        // spatial quantity must come out exactly 2x in pixels, i.e. identical in
        // apparent size. Population is dp-derived, so it must NOT change, and
        // nothing stored may carry a density at all.
        val lo = RainSim(1f, seeded(88)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = RainSim(1f, seeded(88)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("population is dp-derived, not px-derived", lo.drops.size, hi.drops.size)
        run(lo, 300); run(hi, 300)
        lo.drops.forEachIndexed { i, p ->
            val q = hi.drops[i]
            assertEquals("x must be exactly 2x at 2x density", p.x * 2f, q.x, 0.5f)
            assertEquals("y must be exactly 2x at 2x density", p.y * 2f, q.y, 0.5f)
            assertEquals("the streak tail scales with the head", p.tx * 2f, q.tx, 0.5f)
            assertEquals("stored speed is density-free", p.vy, q.vy, 1e-4f)
            assertEquals("…and so is the stroke width", p.wid, q.wid, 1e-4f)
            assertEquals("…and the fall progress behind every life curve", p.u, q.u, 1e-5f)
            assertEquals("…and the alpha", p.a, q.a, 1e-6f)
        }
    }

    @Test fun `nothing goes non-finite after 1000 frames`() {
        val s = sim(seed = 53)
        run(s, 1000)
        for (p in s.drops) {
            val fields = floatArrayOf(
                p.x, p.y, p.tx, p.ty, p.vx, p.vy, p.vt, p.u, p.wid, p.a, p.lean, p.lenMul,
            )
            for (v in fields) assertTrue("a field went non-finite ($v)", v.isFinite())
            assertTrue("fall progress out of range: ${p.u}", p.u in 0f..1f)
            assertTrue("ramp index out of range: ${p.ramp}", p.ramp in 0..RAIN_RAMP_TOP)
            assertTrue("alpha out of range: ${p.a}", p.a >= 0f && p.a <= RAIN_NEAR_ALPHA_CEILING)
        }
    }

    @Test fun `drops stay inside the viewport band — nothing escapes sideways forever`() {
        val s = sim(seed = 59)
        run(s, 1000)
        val m = RAIN_SPAWN_MARGIN * g
        for (p in live(s)) {
            assertTrue("drop escaped horizontally to ${p.x}", p.x >= -m - 1f && p.x <= width + m + 1f)
            assertTrue(
                "drop fell past the recycle line to ${p.y}",
                p.y <= height + RAIN_BOT_BAND * g + 1f,
            )
        }
    }

    // ── 6. Lifecycle, resize, determinism ──

    @Test fun `the field drains when idle and refills — staggered — on rearm`() {
        val s = sim(seed = 61)
        val n = s.drops.size
        run(s, 1800, active = false)                // 30 s: everything reaches the bottom
        assertEquals("rain kept falling over an idle chat", 0, live(s).size)
        assertEquals("the pool is fixed-size: slots go dark, they are not removed", n, s.drops.size)
        s.rearm()
        assertEquals("rearm did not restore the field", n, live(s).size)
        val ys = live(s).map { it.y }
        assertTrue("the field rearmed as a curtain", ys.max() - ys.min() > height * 0.5f)
    }

    @Test fun `a resize rebuilds in place and strands nobody`() {
        val s = sim(seed = 67)
        run(s, 100)
        val before = s.drops.toList()
        val wide = width * 1.8f
        s.resize(wide, height, scale, density)                  // unfolding a Fold
        assertTrue(
            "survivors keep their slot object (and their fall)",
            s.drops.take(min(before.size, s.drops.size)).withIndex().all { (i, p) -> p === before[i] },
        )
        val m = RAIN_SPAWN_MARGIN * g
        for (p in s.drops) {
            assertTrue("stranded outside the new width at x=${p.x}", p.x >= -m - 1f && p.x <= wide + m + 1f)
        }
        run(s, 100)
        for (p in live(s)) {
            assertTrue("escaped after a widening: ${p.x}", p.x >= -m - 1f && p.x <= wide + m + 1f)
        }
        s.resize(380f, 720f, scale, density)                    // then shrink hard
        run(s, 100)
        for (p in live(s)) {
            assertTrue("stranded off-screen after a shrink: ${p.x}", p.x >= -m - 1f && p.x <= 380f + m + 1f)
            assertTrue(
                "left below the new recycle line: ${p.y}",
                p.y <= 720f + RAIN_BOT_BAND * g + 1f,
            )
        }
        for (li in RAIN_PLANES.indices) {
            assertTrue("plane $li was lost on resize", s.drops.any { it.plane == li })
        }
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 71)
            run(s, 240)
            return s.drops.joinToString { "${it.x},${it.y},${it.vy},${it.u},${it.ramp}" }
        }
        assertEquals(once(), once())
    }

    @Test fun `the renderer's batch key stays inside its bucket table, losslessly`() {
        val s = sim(seed = 73)
        run(s, 600)
        val buckets = RAIN_PLANES.size * 2 * RAIN_RAMP.size
        for (p in s.drops) {
            assertTrue("plane ${p.plane} out of range", p.plane in RAIN_PLANES.indices)
            val key = ((p.plane shl 1) or (if (p.heavy) 1 else 0)) * RAIN_RAMP.size + p.ramp
            assertTrue("bucket key $key outside the $buckets-slot table", key in 0 until buckets)
        }
        // The batched tier is only LOSSLESS where both life envelopes are
        // saturated: there, a drop's alpha and width are exactly its plane's, so
        // one Paint can serve the whole bucket. If that ever stops being true the
        // batch silently starts drawing the wrong alpha.
        var flatSeen = 0
        for (p in s.drops) {
            if (p.dead || !p.flat) continue
            flatSeen++
            assertEquals("a batched drop's alpha must be exactly its plane's", p.alphaMul, p.a, 1e-6f)
            assertEquals("…and its width exactly its plane's", p.widMul, p.wid, 1e-6f)
        }
        assertTrue("nothing was batchable — the fast path is dead code", flatSeen > 0)
    }
}
