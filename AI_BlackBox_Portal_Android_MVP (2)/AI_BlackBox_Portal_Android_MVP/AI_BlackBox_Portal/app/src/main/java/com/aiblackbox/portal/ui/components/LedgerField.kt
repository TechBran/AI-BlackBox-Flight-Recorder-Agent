package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: LEDGER RAIN (id "ledger") — Matrix rain forked into the BlackBox's own
// alphabet. A faithful native port of the web module
//   Portal/modules/fx/effects/ledger.js
// (its physics tests: Portal/modules/fx/effects/ledger.test.mjs).
//
// The falling glyphs are the characters a snapshot slug is made of, and a column
// periodically LOCKS onto a real snapshot id (SNAP-20260724-8551), spells it one
// character per row as it falls, holds a beat so it can be read, then dissolves
// back into noise and re-locks onto another. Tinted the accent red.
//
// IDs ARE INJECTED (constructor parameter, defaulting to plausible literals) and
// compiled ONCE at construction. This sim never fetches anything: it is a
// renderer, and a backdrop that can block on the network is a backdrop that
// stutters.
//
// ── THE LEGIBILITY BUDGET (non-negotiable — this is a BACKDROP, not a screensaver)
//
// The shipped Matrix field is the busiest thing in production and is only
// tolerable because it draws a lead glyph at alpha 0.95 and a trail at 0.5. This
// effect spends the IDENTICAL ink, redistributed so an id is readable:
//   • lead        <= LEDGER_LEAD_A       (0.95)  — Matrix parity. NEVER raise.
//   • trail SUM   <= LEDGER_TRAIL_BUDGET (0.5)   — Matrix parity. NEVER raise.
//   • per column, per frame, total ink <= 1.45   — the same number Matrix spends.
// LEDGER_WSTEP quantises every trail alpha DOWNWARDS (floor), and ledgerArgb
// truncates to 8 bits, so the budget can only ever be UNDERSHOT. The alpha tables
// the renderer indexes are the same ones LedgerFieldTest sums, so a refactor that
// quietly brightens the field fails a test rather than shipping.
//
// Unlike the web, intensity does NOT scale alpha here: the Android dial is a
// COUNT dial (ParticleTuning.countScale), so the ceilings above are constants and
// no dial setting can exceed them. That is strictly stricter than ledger.js.
//
// ── DRAW COST (the reason this file exists in the shape it does)
//
// The shipped MatrixSim issues ~1,040 drawText calls per frame (13 glyphs × up to
// 80 columns) and is the most expensive field on Android. This one draws at most
// 1 + LEDGER_TRAIL = 9 glyphs for an id column and 1 + LEDGER_NOISE_TRAIL = 4 for
// a noise column, off-screen rows culled, over ~29 columns on a reference phone —
// call it ~200/frame typical and ~540 worst case on a large tablet. It is roughly
// HALF of Matrix at the same dial and never more. Rows too faint to be worth a
// call are dropped by LEDGER_WSTEP (s < 0) before the call is made, and the draw
// is grouped by (role, size bucket) exactly as the web groups by font, so
// paint.textSize is written at most 2 × LEDGER_SIZE_BUCKETS times per frame
// instead of once per glyph — same glyph size means the same cached rasterisation
// in HWUI's font atlas.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. ledger.js authors every
// length in CSS px (== dp) and applies apparentScale() ITSELF, inside the 12..21
// clamp — so unlike FirefliesField's WEB_TO_REF_PX (= 0.5 × 3.1) the phone clamp
// must NOT be folded in a second time; only the dp → device-px leg remains, which
// is `density` (== scale × FIELD_REFERENCE_DENSITY, see EmberOverlay). EVERY
// stored spatial quantity below — y, speed, tail, glyph size, pitch, fonts — is
// therefore held in DP and multiplied at use, so a density change re-scales the
// live field and apparent size is identical on a 2.0 tablet and a 3.5 phone.
//
// dt. Speeds are DP PER SECOND and are multiplied by dtSec directly — no
// `dt * 60` normalizer (the fireflies/embers convention, which is what the web
// module already uses so the constants transfer verbatim).
// =============================================================================

import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt

// -----------------------------------------------------------------------------
// The ledger. Overwritten per-instance by the host; NEVER fetched.
// -----------------------------------------------------------------------------
/** Fallback ledger — plausible literals, so a fresh box with no snapshots still
 *  rains something that reads as a slug. The host passes real slugs instead. */
internal val LEDGER_DEFAULT_IDS: List<String> = listOf(
    "SNAP-20260724-8551",
    "SNAP-20260723-8532",
    "SNAP-20260720-8502",
    "SNAP-20260713-8342",
    "SNAP-20260709-8131",
)

// -----------------------------------------------------------------------------
// Glyph pool. Built once at class-init: pure data, no randomness, no Android
// types — the rotating NOISE buffer lists digits TWICE because a snapshot slug is
// mostly digits and the field should read as ledger noise, not as an alphabet
// soup that happens to contain "SNAP".
//
// Android draws a glyph as nativeCanvas.drawText(charArray, index, 1, …), so the
// pool IS the draw buffer: a glyph "index" indexes straight into it and the hot
// loop never builds a string or copies a char.
// -----------------------------------------------------------------------------
private const val LEDGER_NOISE_SRC = "01234567890123456789SNAP-ABCDEF"

/** The unique characters of [LEDGER_NOISE_SRC], in first-appearance order. Every
 *  sim starts from this pool, so [LEDGER_NOISE]'s indices are valid in all of
 *  them regardless of what characters the injected ids add on top. */
internal val LEDGER_BASE_GLYPHS: CharArray = LEDGER_NOISE_SRC.toSet().toCharArray()

/** The rotating noise buffer, as glyph indices (duplicates intact — that is the
 *  digit weighting). */
internal val LEDGER_NOISE: IntArray =
    IntArray(LEDGER_NOISE_SRC.length) { i -> LEDGER_BASE_GLYPHS.indexOf(LEDGER_NOISE_SRC[i]) }

// -----------------------------------------------------------------------------
// Tunables. Every one of these is byte-identical to ledger.js so the two surfaces
// can be diffed forever. Lengths are WEB CSS px == dp; times are seconds.
// -----------------------------------------------------------------------------
/** Ring-buffer depth; also the longest id we will spell. */
internal const val LEDGER_MAX_TRAIL = 24

/** Hot-path bound: an ultrawide must not uncap the loop. Unreachable on a phone
 *  (it would take a ~2,600 dp viewport at the 12 dp glyph floor). */
internal const val LEDGER_MAX_COLS = 220

/** Trail rows for an id column — the id reads over 1 + 8 = 9 rows. */
internal const val LEDGER_TRAIL = 8

/** Trail rows for a noise column. */
internal const val LEDGER_NOISE_TRAIL = 3

internal const val LEDGER_KIND_LEDGER = 0
internal const val LEDGER_KIND_NOISE = 1

/** Roll on RESPAWN, not the steady-state mix: id columns fall slower, so they
 *  survive longer per pass and the live field settles at roughly 55/45 rather
 *  than 38/62. That is the intended read (more ids on screen) and it costs
 *  nothing — the ink budget is enforced per column, whatever the mix. */
private const val LEDGER_SHARE = 0.38f

/** Legibility ceiling — Matrix parity. NEVER raise. */
internal const val LEDGER_LEAD_A = 0.95f

/** Total trail ink per column — Matrix parity. NEVER raise. */
internal const val LEDGER_TRAIL_BUDGET = 0.5f

/** Brightest a single trail glyph may ever be. */
private const val LEDGER_TRAIL_PEAK = 0.2f

/** Geometric falloff down the trail. */
private const val LEDGER_TRAIL_DECAY = 0.72f

// Alpha quantisation (style-table rows).
internal const val LEDGER_LEAD_STEPS = 8
internal const val LEDGER_TRAIL_STEPS = 16

/** Stops in each colour ramp — the LIFE-INDEXED colour axis (fall progress). */
internal const val LEDGER_RAMP_STOPS = 5

internal const val LEDGER_SIZE_BUCKETS = 8
private const val LEDGER_SIZE_MIN = 0.5f
private const val LEDGER_SIZE_MAX = 1.2f

/** Seconds for a freshly-resolved lead glyph to settle out of its flare. */
private const val LEDGER_FLARE_T = 0.16f

/** Flare for a ROUTINE noise character — a whisper. A full 1.0 here is what made
 *  the field strobe; the full value is reserved for an id locking on or
 *  completing, which are the only two moments worth drawing the eye. */
private const val LEDGER_NOISE_FLARE = 0.18f

/** Trail rows at/below this depth re-randomise (the tail dissolves to noise). */
private const val LEDGER_DISSOLVE_AT = 6

/** The beat where a completed id sits still so it can actually be read. */
private const val LEDGER_HOLD_MIN = 0.22f
private const val LEDGER_HOLD_SPAN = 0.24f

/** Reference short edge for apparentScale, in dp (Portal/modules/fx/sizing.js —
 *  a viewport this size renders at 1.0; it is the size the look was tuned at). */
private const val LEDGER_REF_SHORT_EDGE_DP = 900f
private const val LEDGER_APPARENT_MIN = 0.5f
private const val LEDGER_APPARENT_MAX = 2.0f

// -----------------------------------------------------------------------------
// COLOUR: two ramps × two sub-populations, indexed by FALL PROGRESS (the rain's
// life axis). Rule: never a flat colour. Lead is the accent red (#ff4a4a), hot at
// the top of the screen and cooling as the column ages down it; the trail is a
// deep red that sinks toward the smear.
// -----------------------------------------------------------------------------
private val LEDGER_LEAD_RAMPS = arrayOf(
    arrayOf(   // ledger
        intArrayOf(255, 196, 190), intArrayOf(255, 120, 112), intArrayOf(255, 74, 74),
        intArrayOf(236, 56, 62), intArrayOf(200, 40, 48),
    ),
    arrayOf(   // noise
        intArrayOf(255, 120, 116), intArrayOf(236, 74, 78), intArrayOf(206, 52, 58),
        intArrayOf(172, 40, 48), intArrayOf(138, 30, 40),
    ),
)
private val LEDGER_TRAIL_RAMPS = arrayOf(
    arrayOf(   // ledger
        intArrayOf(214, 60, 60), intArrayOf(186, 44, 48), intArrayOf(156, 32, 40),
        intArrayOf(126, 24, 32), intArrayOf(98, 18, 26),
    ),
    arrayOf(   // noise
        intArrayOf(150, 30, 34), intArrayOf(126, 24, 30), intArrayOf(102, 20, 26),
        intArrayOf(80, 16, 22), intArrayOf(60, 12, 18),
    ),
)

/** Pack an ARGB colour. Alpha TRUNCATES (never rounds up) so a quantised alpha is
 *  always <= its ideal weight — the same "floor, never ceil" discipline that
 *  keeps the trail sum under [LEDGER_TRAIL_BUDGET] by construction. */
private fun ledgerArgb(a: Float, r: Int, g: Int, b: Int): Int =
    ((a.coerceIn(0f, 1f) * 255f).toInt() shl 24) or (r shl 16) or (g shl 8) or b

/** The alpha a packed colour will actually paint with, in [0,1]. The test's
 *  stand-in for parsing the alpha back out of a canvas fillStyle. */
internal fun ledgerAlphaOf(argb: Int): Float = ((argb ushr 24) and 0xFF) / 255f

/**
 * Every colour the field can draw, built ONCE. The draw loop indexes; it never
 * builds a colour. Layout: (kind × RAMP_STOPS + stop) × STEPS + step, so the
 * per-column `si` (kind + fall progress) is a plain integer the update phase can
 * precompute and the draw phase only reads.
 *
 * Android folds the user dial into COUNT, not alpha, so these are constants —
 * ledger.js multiplies by env.intensity here and can exceed the ceilings at
 * intensity > 1; this port cannot.
 */
internal val LEDGER_LEAD_COLORS: IntArray =
    IntArray(2 * LEDGER_RAMP_STOPS * LEDGER_LEAD_STEPS).also { out ->
        for (k in 0 until 2) for (r in 0 until LEDGER_RAMP_STOPS) {
            val c = LEDGER_LEAD_RAMPS[k][r]
            for (s in 0 until LEDGER_LEAD_STEPS) {
                val a = LEDGER_LEAD_A * ((s + 1).toFloat() / LEDGER_LEAD_STEPS)
                out[(k * LEDGER_RAMP_STOPS + r) * LEDGER_LEAD_STEPS + s] =
                    ledgerArgb(a, c[0], c[1], c[2])
            }
        }
    }

internal val LEDGER_TRAIL_COLORS: IntArray =
    IntArray(2 * LEDGER_RAMP_STOPS * LEDGER_TRAIL_STEPS).also { out ->
        for (k in 0 until 2) for (r in 0 until LEDGER_RAMP_STOPS) {
            val c = LEDGER_TRAIL_RAMPS[k][r]
            for (s in 0 until LEDGER_TRAIL_STEPS) {
                val a = LEDGER_TRAIL_PEAK * ((s + 1).toFloat() / LEDGER_TRAIL_STEPS)
                out[(k * LEDGER_RAMP_STOPS + r) * LEDGER_TRAIL_STEPS + s] =
                    ledgerArgb(a, c[0], c[1], c[2])
            }
        }
    }

/**
 * Quantised trail alpha per trail length: `LEDGER_WSTEP[len][i]` is the style step
 * for the i-th trail row, or -1 for "too faint to be worth a drawText".
 *
 * Built at class-init because it depends only on constants. floor() means every
 * drawn alpha is <= its ideal weight, which is what keeps the sum under
 * [LEDGER_TRAIL_BUDGET] BY CONSTRUCTION rather than by hoping.
 */
internal val LEDGER_WSTEP: Array<IntArray> = Array(LEDGER_TRAIL + 1) { len ->
    val steps = IntArray(len)
    val norm =
        if (len > 0) (1f - LEDGER_TRAIL_DECAY.pow(len)) / (1f - LEDGER_TRAIL_DECAY) else 1f
    for (i in 0 until len) {
        val w = LEDGER_TRAIL_BUDGET * LEDGER_TRAIL_DECAY.pow(i) / norm
        val s = floor(w / LEDGER_TRAIL_PEAK * LEDGER_TRAIL_STEPS).toInt() - 1
        steps[i] = if (s < 0) -1 else min(LEDGER_TRAIL_STEPS - 1, s)
    }
    steps
}

// -----------------------------------------------------------------------------
// Pure geometry maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------
/**
 * GEOMETRY multiplier from the dp box alone (Portal/modules/fx/sizing.js).
 *
 * Deliberately takes no density argument: particle geometry tracking device
 * pixel ratio is the exact bug the web engine spent a release removing, and its
 * Android spelling — glyphs getting bigger on a denser screen — is the same bug.
 */
internal fun ledgerApparentScale(widthDp: Float, heightDp: Float): Float =
    (min(widthDp, heightDp) / LEDGER_REF_SHORT_EDGE_DP)
        .coerceIn(LEDGER_APPARENT_MIN, LEDGER_APPARENT_MAX)

/** SIZE over life: punches in as the column enters the top of the screen, then
 *  tapers as it ages down it. Multiplied per column by its own `envSize`, so no
 *  two columns run the same envelope and none of them is a constant size. */
internal fun ledgerSizeCurve(p: Float): Float {
    val inn = if (p < 0.14f) p / 0.14f else 1f
    return (0.62f + 0.38f * inn) * (1f - 0.26f * p)
}

/** Quantise a size multiplier onto one of [LEDGER_SIZE_BUCKETS] font sizes — the
 *  draw phase groups by bucket, so this is what bounds the per-frame textSize
 *  writes to 2 × 8 instead of one per glyph. */
internal fun ledgerBucket(mult: Float): Int {
    val top = LEDGER_SIZE_BUCKETS - 1
    val t = (mult - LEDGER_SIZE_MIN) / (LEDGER_SIZE_MAX - LEDGER_SIZE_MIN) * top
    if (t < 0f) return 0
    if (t > top.toFloat()) return top
    return t.roundToInt()
}

// -----------------------------------------------------------------------------
// One column. A POOLED slot: respawned in place, never reallocated, so the hot
// path allocates nothing at all. All spatial state is in DP (density-free) and is
// converted to device px at use — see the DPI note in the file header.
// -----------------------------------------------------------------------------
class LedgerColumn internal constructor() {
    var kind = LEDGER_KIND_NOISE                  // sub-population ── axis 1
    var trail = LEDGER_NOISE_TRAIL                // rows drawn behind the head
    var envSize = 1f                              // per-column size-envelope depth
    var sp = 0f                                   // fall speed, dp/s
    var tail = 0f                                 // extra dp below the viewport before respawn
    var y = 0f                                    // head position, dp
    var row = 0                                   // head row index (integer, monotonic)
    var word = -1                                 // index into the compiled ids, or -1 = noise
    var wp = 0                                    // position within that id
    var gap = 1                                   // noise rows before the next lock-on
    var flare = 0f                                // 1 → 0 as a fresh character settles
    var hold = 0f                                 // seconds of stillness left on a completed id
    var hp = 0                                    // ring-buffer head
    var lb = 0                                    // lead size bucket   ── per-frame draw params,
    var tb = 0                                    // trail size bucket     written by update,
    var la = 0                                    // lead alpha step       read by render
    var si = 0                                    // ramp index = kind × RAMP_STOPS + fall stop
    val buf = IntArray(LEDGER_MAX_TRAIL)          // glyph indices, oldest → newest around hp
}

/**
 * LedgerSim — the effect. Registered in FieldRegistry.kt; nothing else in the
 * codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning);
 * [rand] is the injectable randomness seam, so LedgerFieldTest can drive the whole
 * simulation from a seeded generator; [ids] is the ledger to rain — INJECTED and
 * compiled once at construction, because a backdrop must never reach for I/O.
 */
class LedgerSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
    ids: List<String> = LEDGER_DEFAULT_IDS,
) : FieldSim {

    private val countScale = ParticleTuning.sanitizeScale(countScale)

    /** ledger.js's `k`, with surge = 0 (Android has no surge driver) and the count
     *  dial standing in for env.scale — the web's own speed law, so a denser field
     *  falls faster there and here alike. At the default dial k == 1.2. */
    private val speedK = 0.6f + 0.6f * min(this.countScale, 2f)

    // ── glyph pool: grown at CONSTRUCTION only (ids may add characters the noise
    //    alphabet does not have), then frozen into a flat CharArray to draw from.
    private var glyphPool = LEDGER_BASE_GLYPHS.copyOf(LEDGER_BASE_GLYPHS.size + 8)
    private var glyphCount = LEDGER_BASE_GLYPHS.size

    /** The compiled ledger: each id as a sequence of glyph indices. Fixed for the
     *  sim's whole life — there is no code path that refills this. */
    private val seqs: Array<IntArray> = compileIds(ids)

    /** The draw buffer: glyph index → character, handed straight to drawText. */
    internal val glyphs: CharArray = glyphPool.copyOf(glyphCount)

    var width = 0f; private set                   // device px
    var height = 0f; private set
    var widthDp = 0f; private set                 // the web's CSS-px box
    var heightDp = 0f; private set
    var sizeDp = 0f; private set                  // base glyph size, dp
    var pitchDp = 0f; private set                 // row spacing, dp
    var colWidthPx = 0f; private set              // device px; columns tile the width exactly
    /** Per-bucket font size in DP. Multiplied by [g] at use, never before. */
    val fontDp = FloatArray(LEDGER_SIZE_BUCKETS)

    private var grid = emptyArray<LedgerColumn>()

    /** Test/inspection view of the grid. Never called from the hot path. */
    val columns: List<LedgerColumn> get() = grid.asList()

    /** How many ids this sim compiled (a garbage list falls back to the defaults). */
    internal val idCount: Int get() = seqs.size

    /**
     * dp → device px for THIS screen. Equal to the `density` the engine passes,
     * which is `scale × FIELD_REFERENCE_DENSITY` by construction (EmberOverlay).
     * Applied at USE and never baked into stored state, so a density change
     * re-scales the live field instead of stranding it.
     */
    private var g = FIELD_REFERENCE_DENSITY

    private fun rnd(): Float = rand.next().toFloat()

    /** Uniform index in [0, n). Clamped rather than trusted: a generator that ever
     *  returns exactly 1.0 must not be able to corrupt a buffer index. */
    private fun pickIndex(n: Int): Int = (rnd() * n).toInt().coerceIn(0, n - 1)

    /** Intern a character into the pool. CONSTRUCTION ONLY — it may reallocate. */
    private fun glyphOf(ch: Char): Int {
        for (i in 0 until glyphCount) if (glyphPool[i] == ch) return i
        if (glyphCount == glyphPool.size) glyphPool = glyphPool.copyOf(glyphCount * 2)
        glyphPool[glyphCount] = ch
        return glyphCount++
    }

    /** Compile the injected ids into glyph-index sequences. Empty/blank entries are
     *  dropped and an empty result falls back to the shipped defaults, so no host
     *  can hand this effect a field with nothing to spell. */
    private fun compileIds(src: List<String>): Array<IntArray> {
        val out = ArrayList<IntArray>(src.size)
        for (raw in src) {
            val t = raw.trim()
            val s = if (t.length > LEDGER_MAX_TRAIL) t.substring(0, LEDGER_MAX_TRAIL) else t
            if (s.isEmpty()) continue
            out.add(IntArray(s.length) { i -> glyphOf(s[i]) })
        }
        return if (out.isEmpty()) compileIds(LEDGER_DEFAULT_IDS) else out.toTypedArray()
    }

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val dp = if (density > 0f) density else scale * FIELD_REFERENCE_DENSITY
        val same = width == this.width && height == this.height && dp == g && grid.isNotEmpty()
        this.width = width; this.height = height; this.g = dp
        if (same) return
        // The column COUNT is width-derived, so a resize rebuilds the grid — the
        // web module's `resize: init`. A rain field has no per-particle history
        // worth preserving across a rebuild the way a firefly's pulse does.
        widthDp = width / dp
        heightDp = height / dp
        buildGrid()
    }

    /**
     * Lay out the glyph grid. GEOMETRY FROM THE DP BOX ONLY: apparentScale takes
     * no density, exactly as sizing.js takes no devicePixelRatio.
     */
    private fun buildGrid() {
        val apparent = ledgerApparentScale(widthDp, heightDp)
        val baseSizeDp = (14f * apparent).roundToInt().coerceIn(12, 21)
        // THE ONE DIVERGENCE from ledger.js, and the reason it has to exist.
        //
        // ledger.js lays its base grid out with `cols = ceil(width / size)`, which
        // makes a column NARROWER than the glyph it carries: ceil() gives
        // colw = width/cols <= size, ALWAYS (reference phone: size 12 dp, 30
        // columns, 11.61 dp each). On a canvas that is harmless — a monospace
        // advance is ~0.6 em, so nothing actually overlaps — but it means the web
        // grid carries no "a glyph fits its column" invariant, and Android needs
        // one: here the count dial multiplies the column count, and past a point a
        // fixed glyph size really would draw one column over the next.
        //
        // So the base grid FLOORS — a base column is never narrower than the base
        // glyph — and the size is then capped by the column at EVERY dial, exactly
        // the way the shipped MatrixSim derives `fontSizePx = width / n`. Cost
        // against the web: at most one column (29 vs 30 on a reference phone). The
        // glyph size, the row pitch, and therefore every fall speed derived from
        // the pitch stay byte-identical to ledger.js at the shipped dial.
        val baseCols = floor(widthDp / baseSizeDp).toInt().coerceIn(1, LEDGER_MAX_COLS)
        // The Android count dial lands on COLUMNS (the MatrixSim idiom) — the web
        // has no count dial, it spends intensity on alpha instead, which this port
        // deliberately refuses to do (see the legibility note in the header).
        val n = ParticleTuning.scaleCount(baseCols, countScale).coerceIn(1, LEDGER_MAX_COLS)
        colWidthPx = width / n
        sizeDp = min(baseSizeDp.toFloat(), widthDp / n)
        pitchDp = max(1f, (sizeDp * 1.12f).roundToInt().toFloat())   // >= 1: row math divides by it
        // ledger.js's 8 CSS-px font floor exists to stop sub-pixel text; it must
        // never be able to inflate a glyph PAST its own column, which it could do
        // once the dial has shrunk the grid below 8 dp. Above 8 dp — i.e. every
        // realistic viewport at the shipped dial — this is the web's floor exactly.
        val floorDp = min(8f, sizeDp)
        for (b in 0 until LEDGER_SIZE_BUCKETS) {
            val mult = LEDGER_SIZE_MIN + (LEDGER_SIZE_MAX - LEDGER_SIZE_MIN) *
                (b.toFloat() / (LEDGER_SIZE_BUCKETS - 1))
            fontDp[b] = max(floorDp, (sizeDp * mult).roundToInt().toFloat())
        }
        grid = Array(n) { LedgerColumn() }
        for (c in grid) respawn(c, initial = true)
    }

    /**
     * Re-roll a column: SUB-POPULATION, speed, size envelope, and a fresh buffer.
     *
     * The two sub-populations differ on every axis that is visible — trail length,
     * fall speed, size envelope, and (via `si`) their own colour ramp:
     *   ledger — the signal: falls slowly enough to be READ, a long 8-row trail so
     *            a whole id is legible at once, and it runs a touch larger.
     *   noise  — the texture: whips past at roughly the shipped Matrix rate with a
     *            3-row trail, smaller, on a darker ramp so it never competes with
     *            an id for attention.
     * Speeds never overlap (ledger tops out at 3.0 × pitch, noise starts at 3.4),
     * which is what keeps the two readable as two things rather than one thing at
     * two distances.
     *
     * [initial] scatters the first fill up the screen so the field does not fall as
     * one cohort.
     */
    private fun respawn(c: LedgerColumn, initial: Boolean) {
        val ledger = rnd() < LEDGER_SHARE
        c.kind = if (ledger) LEDGER_KIND_LEDGER else LEDGER_KIND_NOISE
        c.trail = if (ledger) LEDGER_TRAIL else LEDGER_NOISE_TRAIL
        c.envSize = if (ledger) 0.94f + rnd() * 0.26f else 0.76f + rnd() * 0.26f
        c.sp = pitchDp * (if (ledger) 1.6f + rnd() * 1.4f else 3.4f + rnd() * 4.2f)
        c.tail = rnd() * heightDp * 0.3f
        c.y = if (initial) rnd() * -heightDp else -(1f + c.trail) * pitchDp - rnd() * 60f
        c.row = floor(c.y / pitchDp).toInt()
        c.word = -1; c.wp = 0; c.gap = 1 + (rnd() * 5f).toInt()
        c.flare = 0f; c.hold = 0f; c.hp = 0
        c.lb = LEDGER_SIZE_BUCKETS shr 1
        c.tb = max(0, c.lb - 1)
        c.la = LEDGER_LEAD_STEPS - 1
        c.si = c.kind * LEDGER_RAMP_STOPS
        for (d in 0 until LEDGER_MAX_TRAIL) c.buf[d] = LEDGER_NOISE[pickIndex(LEDGER_NOISE.size)]
    }

    /** Advance the column by one row: the next id character, or noise. */
    private fun pushGlyph(c: LedgerColumn) {
        val glyph: Int
        var flareStrength = LEDGER_NOISE_FLARE
        if (c.word >= 0) {
            val seq = seqs[c.word]
            glyph = seq[c.wp++]
            if (c.wp >= seq.size) {                       // id complete → dissolve back into noise
                c.word = -1; c.wp = 0
                flareStrength = 1f                        // a finished id is worth a glow
                c.gap = 2 + (rnd() * 9f).toInt()
                // …and sometimes hold a beat, so a finished id can actually be read
                // before it slides off the bottom.
                if (rnd() < 0.35f) c.hold = LEDGER_HOLD_MIN + rnd() * LEDGER_HOLD_SPAN
            }
        } else {
            glyph = LEDGER_NOISE[pickIndex(LEDGER_NOISE.size)]
            if (c.kind == LEDGER_KIND_LEDGER && --c.gap <= 0) {
                c.word = pickIndex(seqs.size); c.wp = 0   // lock on
                flareStrength = 1f                        // ...and so is one starting
            }
        }
        c.hp = (c.hp + 1) % LEDGER_MAX_TRAIL
        c.buf[c.hp] = glyph
        // Routine noise barely glints; an id resolving or completing glows.
        c.flare = flareStrength
    }

    /**
     * [active] is deliberately ignored: rain is continuous, exactly as ledger.js
     * and the shipped MatrixSim have it. The engine's OFF/GENERATING/ALWAYS gate
     * (EmberMode) already fades the whole overlay, and a rain field that thinned
     * out mid-fall would leave half-spelled ids stranded on screen.
     */
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (grid.isEmpty() || pitchDp <= 0f) return
        val decay = dtSec / LEDGER_FLARE_T
        for (c in grid) {
            c.flare = if (c.flare > decay) c.flare - decay else 0f
            if (c.hold > 0f) {
                c.hold -= dtSec                           // a completed id sits still for a beat
                if (c.hold < 0f) c.hold = 0f              // expire cleanly — never leave a negative timer
            } else {
                c.y += c.sp * speedK * dtSec              // delta-timed: same speed at 60 and 120 Hz
            }
            val row = floor(c.y / pitchDp).toInt()
            // Post-stall guard: a parked frame loop must not make the next frame
            // push a thousand glyphs to catch up. The ring buffer is MAX_TRAIL
            // deep, so anything older than that is unobservable anyway.
            if (row - c.row > LEDGER_MAX_TRAIL) c.row = row - LEDGER_MAX_TRAIL
            while (c.row < row) { c.row++; pushGlyph(c) }
            // Per-frame draw parameters live on the column so render() only reads.
            val p = if (heightDp > 0f) (if (c.y > 0f) min(1f, c.y / heightDp) else 0f) else 0f
            // COLOUR RAMP indexed by fall progress — never a flat colour.
            c.si = c.kind * LEDGER_RAMP_STOPS +
                (if (p >= 1f) LEDGER_RAMP_STOPS - 1 else (p * LEDGER_RAMP_STOPS).toInt())
            // SIZE = the shared curve × this column's OWN envelope depth.
            c.lb = ledgerBucket(ledgerSizeCurve(p) * c.envSize)
            c.tb = if (c.lb > 0) c.lb - 1 else 0          // trail one size step behind the head
            // Brandon on the Fold (2026-07-25): "it's like it's flickering at me".
            // The lead glyph used to swing 0.62 -> 1.00 on EVERY character advance,
            // a 38% brightness jump several times a second per column. That is a
            // strobe, not a glint. The swing is now 18%, and a FULL flare is
            // reserved for the moments that actually mean something (an id locking
            // on or completing) — see advance(). Ledger Rain is the shipped default
            // field, so it has to be restful to sit behind text all day.
            val la = floor((0.82f + 0.18f * c.flare) * LEDGER_LEAD_STEPS).toInt() - 1
            c.la = la.coerceIn(0, LEDGER_LEAD_STEPS - 1)
            if (c.y > heightDp + c.tail) respawn(c, initial = false)
        }
    }

    /** The web's `rearm`: re-init an EMPTIED field, never disturb a live one. */
    override fun rearm() {
        if (grid.isEmpty() && widthDp > 0f && heightDp > 0f) buildGrid()
    }

    /**
     * Grouped by (role, size bucket) exactly as the web groups by font: setting a
     * text size is the one genuinely expensive paint-state change in a text field,
     * so this writes it at most 2 × LEDGER_SIZE_BUCKETS times per frame instead of
     * once per column. The scan is integer compares over a pooled array — it
     * allocates nothing, and `paint.ascent()` is the allocation-free spelling of
     * `paint.fontMetrics.ascent` (which mints a FontMetrics per call).
     *
     * NOT additive: this field is drawn with the plain source-over text paint, the
     * web module's declared blend. Additive would let the red accumulate to white
     * behind the chat text, which is the whole reason blend is per-effect.
     */
    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (grid.isEmpty() || pitchDp <= 0f) return
        val paint = paints.matrix                     // hoisted: monospace, source-over
        val nc = drawContext.canvas.nativeCanvas
        val pool = glyphs
        val noise = LEDGER_NOISE
        val gg = g
        val pitch = pitchDp * gg
        val h = this@LedgerSim.height
        val rot = (nowMs / 70.0).toInt()              // rotates the dissolve; no per-frame state
        for (role in 0..1) {
            for (b in 0 until LEDGER_SIZE_BUCKETS) {
                var fontSet = false
                var topToBaseline = 0f                // web textBaseline='top' → Android baseline
                for (i in grid.indices) {
                    val c = grid[i]
                    if ((if (role == 0) c.lb else c.tb) != b) continue
                    val x = i * colWidthPx
                    val y0 = c.row * pitch
                    if (role == 0) {
                        if (y0 < -pitch || y0 > h) continue
                        if (!fontSet) {
                            paint.textSize = fontDp[b] * gg
                            topToBaseline = -paint.ascent()
                            fontSet = true
                        }
                        paint.color = LEDGER_LEAD_COLORS[c.si * LEDGER_LEAD_STEPS + c.la]
                        nc.drawText(pool, c.buf[c.hp], 1, x, y0 + topToBaseline, paint)
                    } else {
                        val steps = LEDGER_WSTEP[c.trail]
                        for (d in 1..c.trail) {
                            val s = steps[d - 1]
                            if (s < 0) continue           // too faint to be worth a drawText
                            val y = y0 - d * pitch
                            if (y < -pitch || y > h) continue
                            if (!fontSet) {
                                paint.textSize = fontDp[b] * gg
                                topToBaseline = -paint.ascent()
                                fontSet = true
                            }
                            val gi = if (d >= LEDGER_DISSOLVE_AT) {
                                noise[(rot + i * 7 + d * 13).mod(noise.size)]   // tail dissolves to noise
                            } else {
                                c.buf[(c.hp - d).mod(LEDGER_MAX_TRAIL)]
                            }
                            paint.color = LEDGER_TRAIL_COLORS[c.si * LEDGER_TRAIL_STEPS + s]
                            nc.drawText(pool, gi, 1, x, y + topToBaseline, paint)
                        }
                    }
                }
            }
        }
    }
}
