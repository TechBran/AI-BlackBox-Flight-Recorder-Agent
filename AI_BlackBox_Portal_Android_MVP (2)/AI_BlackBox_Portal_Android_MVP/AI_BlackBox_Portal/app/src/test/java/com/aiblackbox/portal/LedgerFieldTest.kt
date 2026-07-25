package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.LEDGER_DEFAULT_IDS
import com.aiblackbox.portal.ui.components.LEDGER_KIND_LEDGER
import com.aiblackbox.portal.ui.components.LEDGER_KIND_NOISE
import com.aiblackbox.portal.ui.components.LEDGER_LEAD_A
import com.aiblackbox.portal.ui.components.LEDGER_LEAD_COLORS
import com.aiblackbox.portal.ui.components.LEDGER_LEAD_STEPS
import com.aiblackbox.portal.ui.components.LEDGER_MAX_COLS
import com.aiblackbox.portal.ui.components.LEDGER_MAX_TRAIL
import com.aiblackbox.portal.ui.components.LEDGER_NOISE_TRAIL
import com.aiblackbox.portal.ui.components.LEDGER_RAMP_STOPS
import com.aiblackbox.portal.ui.components.LEDGER_SIZE_BUCKETS
import com.aiblackbox.portal.ui.components.LEDGER_TRAIL
import com.aiblackbox.portal.ui.components.LEDGER_TRAIL_BUDGET
import com.aiblackbox.portal.ui.components.LEDGER_TRAIL_COLORS
import com.aiblackbox.portal.ui.components.LEDGER_TRAIL_STEPS
import com.aiblackbox.portal.ui.components.LEDGER_WSTEP
import com.aiblackbox.portal.ui.components.LedgerColumn
import com.aiblackbox.portal.ui.components.LedgerSim
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.ledgerAlphaOf
import com.aiblackbox.portal.ui.components.ledgerApparentScale
import com.aiblackbox.portal.ui.components.ledgerBucket
import com.aiblackbox.portal.ui.components.ledgerSizeCurve
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.floor
import kotlin.math.max

/**
 * Ledger Rain physics — headless, seeded, no Compose / Android graphics.
 *
 * The sim takes its randomness from an injected [FieldRandom] and its ledger from
 * a constructor-injected id list, so the whole simulation is drivable from the JVM
 * with a deterministic PRNG and nothing it does can reach for I/O.
 *
 * THE ASSERTION THAT MATTERS MOST is the LEGIBILITY BUDGET: this is a backdrop
 * behind chat text, and it is measured against the alpha table the RENDERER
 * indexes (`LEDGER_LEAD_COLORS` / `LEDGER_TRAIL_COLORS` at the column's own
 * `si`/`la`/WSTEP indices, with render()'s off-screen cull replicated) rather than
 * against the module's own ceiling constants — a refactor that quietly brightens
 * the field fails here instead of shipping.
 *
 * Mirrors Portal/modules/fx/effects/ledger.test.mjs — keep them in step.
 */
class LedgerFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
    private val scale = 1f

    /** The ink one shipped Matrix column spends per frame: lead 0.95 + trail 0.5.
     *  This effect is allowed to MATCH that profile and never to exceed it. */
    private val matrixInk = LEDGER_LEAD_A + LEDGER_TRAIL_BUDGET
    private val eps = 1e-4f

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
        ids: List<String> = LEDGER_DEFAULT_IDS,
    ): LedgerSim = LedgerSim(countScale, seeded(seed), ids)
        .also { it.resize(width, height, scale, density) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: LedgerSim, frames: Int, dt: Float = 1f / 60f, active: Boolean = true) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, active)
    }

    // ── the renderer's own arithmetic, replicated so the budget is measured on
    //    what would actually be PAINTED (culling included), not on constants ──

    private fun leadInk(s: LedgerSim, c: LedgerColumn): Float {
        val y0 = c.row * s.pitchDp
        if (y0 < -s.pitchDp || y0 > s.heightDp) return 0f
        return ledgerAlphaOf(LEDGER_LEAD_COLORS[c.si * LEDGER_LEAD_STEPS + c.la])
    }

    private fun trailInk(s: LedgerSim, c: LedgerColumn): Float {
        val y0 = c.row * s.pitchDp
        val steps = LEDGER_WSTEP[c.trail]
        var sum = 0f
        for (d in 1..c.trail) {
            val step = steps[d - 1]
            if (step < 0) continue
            val y = y0 - d * s.pitchDp
            if (y < -s.pitchDp || y > s.heightDp) continue
            sum += ledgerAlphaOf(LEDGER_TRAIL_COLORS[c.si * LEDGER_TRAIL_STEPS + step])
        }
        return sum
    }

    private fun glyphsDrawn(s: LedgerSim, c: LedgerColumn): Int {
        val y0 = c.row * s.pitchDp
        var n = if (y0 >= -s.pitchDp && y0 <= s.heightDp) 1 else 0
        val steps = LEDGER_WSTEP[c.trail]
        for (d in 1..c.trail) {
            if (steps[d - 1] < 0) continue
            val y = y0 - d * s.pitchDp
            if (y >= -s.pitchDp && y <= s.heightDp) n++
        }
        return n
    }

    /** Read a column top-to-bottom: oldest pushed row first, head last. */
    private fun columnText(s: LedgerSim, c: LedgerColumn, n: Int): String {
        val sb = StringBuilder(n)
        for (d in n - 1 downTo 0) sb.append(s.glyphs[c.buf[(c.hp - d).mod(LEDGER_MAX_TRAIL)]])
        return sb.toString()
    }

    // ── 1. The grid ──

    @Test fun `the column grid is bounded and tiles the viewport`() {
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val s = sim(countScale = cs)
            val n = s.columns.size
            assertTrue("countScale $cs produced $n columns", n in 1..LEDGER_MAX_COLS)
            assertEquals("columns must tile the full width", width, n * s.colWidthPx, 1e-3f)
        }
        for (w in floatArrayOf(320f, 1080f, 3000f)) {
            val s = LedgerSim(1f, seeded(7)).also { it.resize(w, w * 2f, scale, density) }
            assertTrue("a ${w}px canvas produced ${s.columns.size}", s.columns.size in 1..LEDGER_MAX_COLS)
            assertEquals(w, s.columns.size * s.colWidthPx, 1e-3f)
            for (c in s.columns) {
                assertEquals("the ring buffer is fixed-depth", LEDGER_MAX_TRAIL, c.buf.size)
                assertTrue(c.kind == LEDGER_KIND_LEDGER || c.kind == LEDGER_KIND_NOISE)
                assertTrue(c.trail == LEDGER_TRAIL || c.trail == LEDGER_NOISE_TRAIL)
                assertTrue("a column's rows must fit its ring buffer", 1 + c.trail <= LEDGER_MAX_TRAIL)
            }
        }
    }

    @Test fun `a glyph never has to be drawn wider than its own column`() {
        // The web does NOT get this free: ledger.js uses cols = ceil(width/size),
        // so its columns come out NARROWER than its glyphs (harmless on a canvas —
        // a monospace advance is ~0.6 em). Android floors the base grid instead and
        // caps the size by the column, because here the count dial can multiply the
        // column count until a fixed size really would overlap. See buildGrid().
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 1f, 2f, ParticleTuning.COUNT_SCALE_MAX)) {
            val s = sim(countScale = cs)
            val colWidthDp = s.widthDp / s.columns.size
            assertTrue("dial $cs: ${s.sizeDp} dp glyphs in $colWidthDp dp columns",
                s.sizeDp <= colWidthDp + 1e-3f)
            assertTrue("row pitch must never collapse to zero", s.pitchDp >= 1f)
        }
        // …and at the shipped dial the size is ledger.js's, unmodified.
        assertEquals("web parity at the default dial", 12f, sim().sizeDp, 1e-4f)
    }

    @Test fun `both sub-populations exist, with distinct parameters`() {
        val s = sim(seed = 5)
        val ledger = s.columns.filter { it.kind == LEDGER_KIND_LEDGER }
        val noise = s.columns.filter { it.kind == LEDGER_KIND_NOISE }
        assertTrue("ledger=${ledger.size} noise=${noise.size}", ledger.size > 3 && noise.size > 3)
        assertTrue("id columns fall slower than noise columns, ALWAYS — that is what makes " +
            "an id readable while the noise whips past",
            ledger.maxOf { it.sp } < noise.minOf { it.sp })
        assertTrue("size envelopes differ per sub-population",
            ledger.minOf { it.envSize } > noise.minOf { it.envSize })
        assertEquals("an id column carries a long trail so a whole id reads at once",
            LEDGER_TRAIL, ledger.first().trail)
        assertEquals(LEDGER_NOISE_TRAIL, noise.first().trail)
    }

    // ── 2. THE legibility budget (the rule that makes or breaks a backdrop) ──

    @Test fun `the ceilings themselves are Matrix parity`() {
        assertEquals("lead alpha ceiling was raised", 0.95f, LEDGER_LEAD_A, 0f)
        assertEquals("trail ink budget was raised", 0.5f, LEDGER_TRAIL_BUDGET, 0f)
        // Quantisation may only ever UNDERSHOOT: every drawn alpha is floor()ed.
        for (len in 0..LEDGER_TRAIL) {
            var sum = 0f
            for (step in LEDGER_WSTEP[len]) {
                if (step < 0) continue
                assertTrue("style step out of table", step < LEDGER_TRAIL_STEPS)
                sum += ledgerAlphaOf(LEDGER_TRAIL_COLORS[step])
            }
            assertTrue("trail length $len sums to $sum", sum <= LEDGER_TRAIL_BUDGET + eps)
        }
        for (a in LEDGER_LEAD_COLORS) {
            assertTrue("a lead colour paints above the ceiling", ledgerAlphaOf(a) <= LEDGER_LEAD_A + eps)
        }
    }

    @Test fun `per column per frame - lead under 0-95 and trail ink under 0-5`() {
        val s = sim(seed = 11)
        var worstColumn = 0f
        var worstLead = 0f
        var worstTrail = 0f
        var glyphs = 0
        for (f in 1..400) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (c in s.columns) {
                val lead = leadInk(s, c)
                val trail = trailInk(s, c)
                val n = glyphsDrawn(s, c)
                assertTrue("lead alpha $lead > $LEDGER_LEAD_A", lead <= LEDGER_LEAD_A + eps)
                assertTrue("trail ink $trail > $LEDGER_TRAIL_BUDGET", trail <= LEDGER_TRAIL_BUDGET + eps)
                assertTrue("column ink ${lead + trail} > matrix profile $matrixInk",
                    lead + trail <= matrixInk + eps)
                assertTrue("column drew $n glyphs", n <= 1 + LEDGER_TRAIL)
                worstColumn = max(worstColumn, lead + trail)
                worstLead = max(worstLead, lead)
                worstTrail = max(worstTrail, trail)
                glyphs += n
            }
        }
        assertTrue("the field is actually drawing", glyphs / 400 > 20)
        assertTrue("…and at a useful contrast, not invisibly (worst column $worstColumn)",
            worstColumn > 0.9f)
        assertTrue("the lead must actually reach its ceiling somewhere", worstLead > 0.9f)
        assertTrue("the trail must actually spend its budget", worstTrail > 0.3f)
    }

    @Test fun `a WARMED field stays quieter than the shipped Matrix, not just per column`() {
        val s = sim(seed = 12)
        run(s, 3000)                                   // fill the screen
        var total = 0f
        for (f in 1..200) {
            s.update((3000 + f) * 16.6667, 1f / 60f, true)
            for (c in s.columns) total += leadInk(s, c) + trailInk(s, c)
        }
        val ink = total / 200f
        val matrix = s.columns.size * matrixInk
        assertTrue("field ink $ink must stay under the Matrix profile $matrix", ink < matrix)
        assertTrue("but the field is populated, not a ghost town", ink > s.columns.size * 0.4f)
    }

    @Test fun `the draw is grouped into a bounded number of glyph sizes`() {
        // render() writes paint.textSize at most once per (role, bucket): that
        // bound is what keeps this field off MatrixSim's ~1,040-drawText profile.
        val s = sim(seed = 13)
        run(s, 900)
        for (c in s.columns) {
            assertTrue("lead bucket ${c.lb} out of table", c.lb in 0 until LEDGER_SIZE_BUCKETS)
            assertTrue("trail bucket ${c.tb} out of table", c.tb in 0 until LEDGER_SIZE_BUCKETS)
            assertTrue("trail must sit one size step behind the head", c.tb <= c.lb)
        }
        assertEquals(LEDGER_SIZE_BUCKETS, s.fontDp.size)
        // ledger.js's 8 dp font floor, capped so it can never inflate a glyph past
        // its own column on a dialled-up grid.
        for (f in s.fontDp) assertTrue("a font size collapsed to $f dp", f >= 8f)
        val dense = sim(seed = 13, countScale = ParticleTuning.COUNT_SCALE_MAX)
        for (f in dense.fontDp) {
            assertTrue("the font floor inflated a glyph past its column ($f dp)",
                f >= kotlin.math.min(8f, dense.sizeDp) - 1e-3f)
        }
    }

    // ── 3. THE THREE PREMIUM RULES ──

    @Test fun `SIZE is a curve over life with a per-column envelope, never constant`() {
        assertTrue("the head punches in as the column enters",
            ledgerSizeCurve(0.14f) > ledgerSizeCurve(0f))
        assertTrue("…and tapers as the column ages",
            ledgerSizeCurve(0.14f) > ledgerSizeCurve(1f))
        assertEquals("the curve is continuous at the punch-in knee",
            ledgerSizeCurve(0.1399f), ledgerSizeCurve(0.1401f), 2e-3f)
        assertEquals(0, ledgerBucket(-5f))
        assertEquals(LEDGER_SIZE_BUCKETS - 1, ledgerBucket(99f))
        // Every column carries its OWN envelope depth, over its kind's band.
        val s = sim(seed = 14)
        val envs = s.columns.map { it.envSize }
        assertEquals("the envelope must be per-column", envs.size, envs.toSet().size)
        assertTrue("envelope out of band: ${envs.min()}..${envs.max()}",
            envs.min() >= 0.76f && envs.max() <= 1.20f)
        // …and it actually moves the drawn size over a run.
        val seen = HashSet<Int>()
        for (f in 1..900) { s.update(f * 16.6667, 1f / 60f, true); for (c in s.columns) seen.add(c.lb) }
        assertTrue("the size never changed — the curve is not wired up ($seen)", seen.size >= 3)
    }

    @Test fun `COLOUR is indexed by fall progress, never a flat tint`() {
        val s = sim(seed = 16)
        val ledgerStops = HashSet<Int>()
        val noiseStops = HashSet<Int>()
        for (f in 1..1800) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (c in s.columns) {
                assertTrue("ramp index ${c.si} escaped its table",
                    c.si in 0 until 2 * LEDGER_RAMP_STOPS)
                val stop = c.si - c.kind * LEDGER_RAMP_STOPS
                assertTrue("a column read another kind's ramp", stop in 0 until LEDGER_RAMP_STOPS)
                if (c.kind == LEDGER_KIND_LEDGER) ledgerStops.add(stop) else noiseStops.add(stop)
            }
        }
        // Id columns fall slowly by design, so they traverse fewer stops per run —
        // both must still move through the ramp rather than sit on one tint.
        assertTrue("id columns sat on one colour ($ledgerStops)", ledgerStops.size >= 3)
        assertTrue("noise columns sat on one colour ($noiseStops)", noiseStops.size >= 4)
        // The two sub-populations must not share a ramp — the noise must never
        // compete with an id for attention.
        assertNotEquals(
            LEDGER_LEAD_COLORS[LEDGER_KIND_LEDGER * LEDGER_RAMP_STOPS * LEDGER_LEAD_STEPS],
            LEDGER_LEAD_COLORS[LEDGER_KIND_NOISE * LEDGER_RAMP_STOPS * LEDGER_LEAD_STEPS],
        )
    }

    // ── 4. Motion ──

    @Test fun `rain falls DOWN, and only stalls for a hold beat`() {
        val s = sim(seed = 17)
        val n = s.columns.size
        val py = FloatArray(n) { s.columns[it].y }
        val pr = IntArray(n) { s.columns[it].row }
        val ph = FloatArray(n) { s.columns[it].hold }
        var advanced = 0
        var held = 0
        for (f in 1..1800) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (i in 0 until n) {
                val c = s.columns[i]
                if (c.y < py[i]) {
                    // The only legal drop is a respawn, which always seeds ABOVE
                    // the viewport — a live column can never move upward.
                    assertTrue("column $i moved up without respawning", c.y < 0f)
                } else if (c.y > py[i]) {
                    advanced++
                    assertTrue("row index must track the head monotonically", c.row >= pr[i])
                } else {
                    assertTrue("a column only holds still while its id is being read",
                        ph[i] > 0f)
                    held++
                }
                assertEquals("row must stay the floor of y/pitch",
                    floor(c.y / s.pitchDp).toInt(), c.row)
                py[i] = c.y; pr[i] = c.row; ph[i] = c.hold
            }
        }
        assertTrue("columns are not falling ($advanced advancing column-frames)",
            advanced > n * 1500)
        assertTrue("no column ever held for a beat — the id is never readable", held > 0)
    }

    @Test fun `60 Hz and 120 Hz travel the same path`() {
        val a = sim(seed = 3).also { run(it, 120, 1f / 60f) }
        val b = sim(seed = 3).also { run(it, 240, 1f / 120f) }
        assertEquals(a.columns.size, b.columns.size)
        a.columns.forEachIndexed { i, c ->
            val d = b.columns[i]
            // 0.05 dp = under a sixth of a device pixel on the reference phone: the
            // only gap left is Float accumulation over 2x as many additions, not a
            // missing dt term (which would show up as a ~2x difference).
            assertTrue("column $i drifted: ${c.y} vs ${d.y}", abs(c.y - d.y) < 0.05f)
            assertTrue("column $i advanced a different number of rows: ${c.row} vs ${d.row}",
                abs(c.row - d.row) <= 1)
        }
    }

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1x and 2x density: the grid must
        // be identical in dp and exactly 2x in device px.
        val lo = LedgerSim(1f, seeded(88)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = LedgerSim(1f, seeded(88)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("the column count is dp-derived, not px-derived",
            lo.columns.size, hi.columns.size)
        assertEquals("glyph size is dp-derived", lo.sizeDp, hi.sizeDp, 1e-4f)
        assertEquals("row pitch is dp-derived", lo.pitchDp, hi.pitchDp, 1e-4f)
        assertEquals("the dp box is density-free", lo.widthDp, hi.widthDp, 1e-3f)
        for (b in 0 until LEDGER_SIZE_BUCKETS) assertEquals(lo.fontDp[b], hi.fontDp[b], 1e-4f)
        assertEquals("column width must be exactly 2x in device px",
            lo.colWidthPx * 2f, hi.colWidthPx, 1e-3f)
        run(lo, 300); run(hi, 300)
        lo.columns.forEachIndexed { i, c ->
            val d = hi.columns[i]
            assertEquals("stored geometry is density-free", c.y, d.y, 1e-3f)
            assertEquals("…and so is the fall speed", c.sp, d.sp, 1e-4f)
            assertEquals(c.row, d.row)
        }
        // apparentScale takes a dp box and NOTHING else — the shipped web bug was
        // geometry tracking devicePixelRatio, and its Android spelling is glyphs
        // getting bigger on a denser screen.
        assertEquals("the 900 dp short edge is the reference the look was tuned at",
            1f, ledgerApparentScale(1440f, 900f), 1e-6f)
        assertEquals("clamped below", 0.5f, ledgerApparentScale(10f, 10f), 0f)
        assertEquals("clamped above", 2.0f, ledgerApparentScale(9000f, 9000f), 0f)
    }

    // ── 5. The ledger itself ──

    @Test fun `ships plausible default ids and never fetches`() {
        assertTrue(LEDGER_DEFAULT_IDS.size >= 3)
        val slug = Regex("^SNAP-\\d{8}-\\d+$")
        for (id in LEDGER_DEFAULT_IDS) assertTrue("$id is not a plausible slug", slug.matches(id))
        // The ledger is CONSTRUCTOR-injected and compiled once: there is no code
        // path that can refill it, so a long run can never change its size.
        val s = sim(seed = 18)
        assertEquals(LEDGER_DEFAULT_IDS.size, s.idCount)
        run(s, 3000)
        assertEquals("the ledger changed mid-flight — something is fetching",
            LEDGER_DEFAULT_IDS.size, s.idCount)
    }

    @Test fun `a column resolves an INJECTED id, in order, then dissolves`() {
        val id = "SNAP-20260101-0007"
        val s = sim(seed = 3, ids = listOf(id))
        assertEquals("only the injected id is compiled", 1, s.idCount)
        var spelled = false
        var dissolved = false
        for (f in 1..6000) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (c in s.columns) {
                if (!spelled && columnText(s, c, id.length) == id) spelled = true
                else if (spelled && c.kind == LEDGER_KIND_LEDGER && c.word < 0 && c.gap > 0) {
                    dissolved = true
                }
            }
            if (dissolved) break
        }
        assertTrue("no ledger column ever spelled the injected id top-to-bottom", spelled)
        assertTrue("…and it never went back to noise before re-locking", dissolved)
    }

    @Test fun `garbage ids fall back instead of throwing`() {
        val garbage = listOf(
            emptyList<String>(), listOf(""), listOf("   ", "\t"), listOf("", "  "),
        )
        for (bad in garbage) {
            val s = sim(ids = bad)
            assertEquals("fell back to the shipped defaults", LEDGER_DEFAULT_IDS.size, s.idCount)
        }
        // An over-long id is truncated to the ring-buffer depth, not rejected.
        val overlong = "X".repeat(400)
        val s = sim(ids = listOf(overlong))
        assertEquals(1, s.idCount)
        run(s, 600)
        for (c in s.columns) {
            for (d in 0 until LEDGER_MAX_TRAIL) {
                assertTrue("buffer holds an invalid glyph index: ${c.buf[d]}",
                    c.buf[d] >= 0 && c.buf[d] < s.glyphs.size)
            }
        }
    }

    // ── 6. Durability, lifecycle, determinism ──

    @Test fun `no NaN anywhere after 1000 frames of hostile dt`() {
        val s = sim(seed = 23)
        val dts = floatArrayOf(1f / 60f, 1f / 120f, 1f / 144f, 0.5f, 1f / 30f, 0f)
        var now = 0.0
        for (f in 0 until 1000) {
            val dt = dts[f % dts.size]
            now += dt * 1000.0
            s.update(now, dt, true)
            for (c in s.columns) {
                for (v in floatArrayOf(c.y, c.sp, c.flare, c.hold, c.tail, c.envSize)) {
                    assertTrue("a column field went non-finite ($v)", v.isFinite())
                }
                assertTrue("ring head out of range: ${c.hp}", c.hp in 0 until LEDGER_MAX_TRAIL)
                assertTrue("flare out of range: ${c.flare}", c.flare in 0f..1f)
                assertTrue("hold went negative: ${c.hold}", c.hold >= 0f)
                assertTrue("nothing escapes below the viewport: ${c.y}",
                    c.y <= s.heightDp * 1.3f + 1f)
                assertTrue("the id cursor escaped its sequence", c.wp >= 0)
                for (d in 0 until LEDGER_MAX_TRAIL) {
                    assertTrue("buffer holds an invalid glyph index", c.buf[d] in s.glyphs.indices)
                }
            }
        }
    }

    @Test fun `the hot path reuses its slots for the whole session`() {
        val s = sim(seed = 45)
        val slots = s.columns.toList()
        run(s, 3600)
        assertEquals("the grid is fixed-size", slots.size, s.columns.size)
        assertTrue("a respawned column reuses its slot object",
            s.columns.withIndex().all { (i, c) -> c === slots[i] })
    }

    @Test fun `resize rebuilds the grid - rearm never clears a live field`() {
        val s = sim(seed = 27)
        run(s, 60)
        val wide = s.columns.size
        val before = s.columns.toList()
        s.resize(width, height, scale, density)
        assertTrue("a no-op resize must not re-scatter a live field — the Canvas " +
            "calls resize EVERY frame",
            s.columns.withIndex().all { (i, c) -> c === before[i] })
        assertEquals(wide, s.columns.size)
        s.resize(420f, 900f, scale, density)
        assertNotEquals(wide, s.columns.size)
        assertEquals("columns still tile the full width", 420f,
            s.columns.size * s.colWidthPx, 1e-3f)
        val live = s.columns.toList()
        s.rearm()
        assertTrue("rearm leaves a populated field alone",
            s.columns.withIndex().all { (i, c) -> c === live[i] })
        // …and on a field that was never sized it is a no-op, not a crash.
        val empty = LedgerSim(1f, seeded(1))
        empty.rearm()
        assertEquals(0, empty.columns.size)
        empty.update(16.0, 1f / 60f, true)
        empty.resize(width, height, scale, density)
        assertTrue("a sized field populates", empty.columns.isNotEmpty())
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 99)
            run(s, 240)
            return s.columns.joinToString {
                "${it.y},${it.row},${it.sp},${it.kind},${it.si},${it.lb},${it.hp},${it.buf[it.hp]}"
            }
        }
        assertEquals(once(), once())
    }
}
