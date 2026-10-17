package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import com.aiblackbox.portal.ui.components.wave.WaveformFrame
import com.aiblackbox.portal.ui.components.wave.WaveformRenderer
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.floor

// =============================================================================
// Segmented Equalizer — reference sheet concept #3, tagged BOTH.
//
// A classic LED-segment spectrum analyser: a width-derived number of columns
// across the surface, each a stack of small rounded-rect "LED" segments rising
// from a common baseline. The lit height of a column tracks a per-column drive; a
// dimmer "peak-hold" segment floats above it and falls slowly (~PEAK_FALL_SEC),
// the affordance every hi-fi EQ display has. Most columns are white/grey, a
// deterministic ~1-in-3 subset is the brand accent (red for the human, blue for
// the AI) so the eye has anchors without the whole field going one colour.
//
// EVERY COLUMN IS A MEASUREMENT. The style reads the log-frequency SPECTRUM
// channel ([AuroraAnalyser.BINS] bins, see AuroraSpectrum): each column owns a
// contiguous, non-overlapping range of REAL bins and reads the LOUDEST one in it.
// Nothing is interpolated between measurements and nothing is invented on top of
// them — the previous version lerped four band drives across sixteen columns and
// added a per-column sine wobble to decorrelate them, which is a cosmetic upsample
// wearing a spectrum analyser's clothes. Both the lerp and the wobble are DELETED;
// [SegmentedEqMath.columnDrive] takes no clock at all, so the jitter cannot come
// back by accident.
//
// MAX, not mean, over a column's bins: a narrow formant is the interesting part of
// speech and averaging it against its quiet neighbours is what makes an EQ look
// dead. The same reasoning WaveformFeed.levelOf gives for its weighted max.
//
// WHEN THERE IS NO SPECTRUM the columns fall back to the four band drives laid out
// on the SAME log-frequency axis (a quarter of the columns each — the analyser's
// probe pairs sit near the quarter points of the 80–7200 Hz map, so the fallback
// puts a sound in the same place the spectrum would, just coarser). Today no call
// site publishes bins, so this fallback IS what ships; the style becomes 24-point
// the moment a producer wires the second channel in, with no layout change.
//
// THE ONE CURVE IS APPLIED HERE, AND IT IS AURORA'S. A column's drive goes through
// Aurora.displayCurve and is lifted onto the rest floor by Aurora.restBlend, both
// CALLED rather than restated — a style that grows its OWN curve is exactly the
// drift WaveformPerceptualCurveTest exists to catch. What this style must not do
// either, and used to, is skip the curve: the drive reached the geometry as the
// raw measurement, and at a live mic's typical band levels (measured 0.07 / 0.03 /
// 0.01 on bands 1–3) a column lit one LED and stopped, so three quarters of the
// display were dead. The curve is where a small measurement becomes a column you
// can read. Calibrating the BIN channel's own gain is a separate job and belongs
// with the producer that wires it.
//
// COLUMN COUNT AND WIDTH COME FROM THE SURFACE. A fixed sixteen gave 14.9 dp bars
// on a 360 dp phone (too fat) and would give 29 dp on a Fold. The count is now
// width / COLUMN_PITCH_DP, floored at MIN_COLUMNS and CAPPED AT MAX_COLUMNS =
// AuroraAnalyser.BINS — the display never claims more columns than the measurement
// has bins. Past that width the pitch keeps growing but the bar does not: it is
// capped at MAX_BAR_W_DP and the extra room becomes gap, so a wide surface reads
// as a spectrum analyser rather than a picket fence.
//
// TWO-VOICE CONTRACT (the plan's table):
//   • one voice  → classic bottom-anchored EQ using the FULL height. The single
//                  speaker's colour identifies it (human red-accent + near-white,
//                  AI blue-accent + blue-grey). A voice hanging from the top would
//                  read as broken/upside-down, so a lone voice always rises from
//                  the bottom — direction is only pressed into service to SEPARATE
//                  two speakers.
//   • two voices → human columns rise from the BOTTOM half, AI columns hang from
//                  the TOP half, both meeting the centre gap at full drive. Same
//                  direction-is-identity idiom Aurora uses; no overlap, so the
//                  occlusion of the two halves never sums past one full field.
//
// BUDGET (inherited from Aurora v33, a REQUIREMENT not a nicety):
//   • Only the LIT segment cores are SrcOver (they must read as solid LEDs); their
//     worst-case coverage is COLUMN_FILL_FRACTION × (seg/(seg+gap)) ≈ 0.44 of the
//     surface — under the ~55% occlusion ceiling, so the particle field behind
//     always survives even at full drive with every column maxed. Pinned by test.
//     The bar-width cap can only NARROW a bar, so that worst case still bounds every
//     surface however wide, and the column count does not enter it at all.
//   • Glow and peak-hold are additive (BlendMode.Plus); their summed alpha is well
//     under white-out. Pinned by test.
//   • Every dimension is height-relative except the LED unit itself, which is a
//     fixed dp so the segments read as discrete LEDs at any size; a small-height
//     DEGRADE (SegmentedEqMath.resolveUnit) shrinks that unit only when a column
//     could not otherwise stack SEGMENT_MIN_ROWS rows, so the 40 dp player bar
//     (and its 20 dp half in two-voice mode) never collapses to one mesa block.
//   • The peak-hold fall is the style's ONLY motion of its own and it runs off
//     WaveformFrame.dtSec, so 60 and 120 Hz animate identically. Nothing reads
//     phaseSec any more — a column IS a measurement, and a measurement has no clock.
//     The fall is also the one thing that keeps changing after a voice goes silent,
//     so [needsFrames] is overridden to hold frames while any peak is still falling —
//     otherwise the idle park would freeze a half-fallen peak mid-air.
//
// All geometry, spectrum mapping, peak-decay and budget math is pure Kotlin in
// [SegmentedEqMath] / [SegmentedEqSpec] so it is JVM-testable exactly like the
// aurora math; the renderer below is a pure traversal of it.
//
// A later maintainer replaces ONLY this file's body; [segmentedEqRenderer]'s name
// and its registration lambda in WaveformRegistry never move.
// =============================================================================

/**
 * Build a Segmented Equalizer renderer. THE public surface the registry calls —
 * its name/shape is stable even as the body evolves. Stateful (peak-hold rings),
 * so one instance is kept alive across frames by VoiceVisualizer and rebuilt on a
 * style switch, exactly the lifecycle the renderer contract promises.
 */
internal fun segmentedEqRenderer(): WaveformRenderer = SegmentedEqRenderer()

/**
 * Tunable constants, each with the reason it holds its value. Grouped so a
 * later on-device tweak pass (M4) edits numbers here, never logic below.
 */
internal object SegmentedEqSpec {

    /** Target width of one column's PITCH (bar + gap), in dp — what the column count
     *  is derived from. 12 dp asks a 360 dp phone for 30 columns, which the bin cap
     *  then trims to 24 (a 15 dp pitch, 9.9 dp bars); a narrower surface keeps the
     *  12 dp pitch and simply gets fewer columns instead of the same sixteen squeezed
     *  together. */
    const val COLUMN_PITCH_DP = 12f

    /** Fewest columns worth drawing. Below eight the display stops reading as a
     *  spectrum and becomes a handful of blocks, so a very narrow surface holds here
     *  and lets its bars get thin rather than losing the shape of the sound. */
    const val MIN_COLUMNS = 8

    /** Most columns the display may draw, and it is not a taste decision: one column
     *  per spectrum bin. Past this a column could only repeat its neighbour's
     *  measurement or invent a value between two — see the file header. */
    const val MAX_COLUMNS = AuroraAnalyser.BINS

    /** Fraction of a column's PITCH the lit bar fills; the rest is the inter-column
     *  gap. 0.66 keeps worst-case horizontal occlusion at 0.66 and, times the
     *  vertical fill below, lands the whole SrcOver budget at ≈0.44 < 0.55. */
    const val COLUMN_FILL_FRACTION = 0.66f

    /** Ceiling on a bar's WIDTH in dp, whatever the pitch. 10 dp is a hair above the
     *  9.9 dp a 360 dp phone lands on, so the phone is unaffected and only surfaces
     *  wide enough to have saturated [MAX_COLUMNS] are held back from fattening. The
     *  cap only ever NARROWS a bar, so the occlusion budget below stays the worst
     *  case. */
    const val MAX_BAR_W_DP = 10f

    /** One LED segment's height, in dp. ~3 dp reads as a discrete LED, not a bar. */
    const val SEGMENT_HEIGHT_DP = 3f

    /** Gap between stacked segments, in dp. Half the segment so the vertical fill is
     *  seg/(seg+gap) = 3/4.5 ≈ 0.667 — the second half of the occlusion budget. */
    const val SEGMENT_GAP_DP = 1.5f

    /** Corner radius of a segment, in dp. A hair of rounding = "rounded-rect LED". */
    const val SEGMENT_CORNER_DP = 1f

    /** The fewest rows a column must stack before the small-height DEGRADE kicks in
     *  and shrinks the LED unit. Below three rows a column can no longer read as a
     *  meter (it mesas into one or two blocks), so this is the floor the 40 dp bar —
     *  and its 20 dp half in two-voice mode — is guaranteed to clear. */
    const val SEGMENT_MIN_ROWS = 3

    /** Rows always lit for a PRESENT column, so a present-but-silent voice shows a
     *  baseline LED instead of an empty gap (the player bar's flat-but-there rest).
     *  One row is still visually flat, so restLevel=0 on the player bar stays honest. */
    const val MIN_LIT_WHEN_PRESENT = 1

    /** Seconds for a full-scale peak-hold to fall to zero. The classic slow drop; a
     *  fall RATE of 1/1.5 per second. */
    const val PEAK_FALL_SEC = 1.5f

    /** Below this remaining peak we treat the fall as done — the idle-park epsilon
     *  for [SegmentedEqRenderer.needsFrames]. Well under one row's worth of height. */
    const val PEAK_PARK_EPS = 0.01f

    /** Inset from the top/bottom edges, in dp, so the baseline sits NEAR (not on)
     *  the edge and a full column does not kiss the border. */
    const val EDGE_INSET_DP = 2f

    /** Gap left at the centre in two-voice mode, in dp, so the human's rising stack
     *  and the AI's hanging stack never touch even at simultaneous full drive. */
    const val CENTER_GAP_DP = 3f

    // ── the deterministic accent subset ──

    /** A column is an ACCENT column when index % this == the phase below. Modulo 3
     *  with phase 2 lights columns 2,5,8,11,14 — five of sixteen, ≈"every 3rd". */
    const val ACCENT_MODULO = 3
    const val ACCENT_PHASE = 2

    // ── alphas ──

    /** Additive halo behind each lit segment (BlendMode.Plus). */
    const val GLOW_ALPHA = 0.18f

    /** The lit LED core (SrcOver) — solid enough to read as an LED, not full 1.0 so
     *  even the occluding pass leaves the field a sliver of light. */
    const val CORE_ALPHA = 0.80f

    /** The floating peak-hold segment (BlendMode.Plus, dimmer than the core). */
    const val PEAK_ALPHA = 0.30f

    // ── palette (near-black bg, red/white/grey with a blue AI tint) ──

    /** Human non-accent columns: near-white. */
    val HUMAN_GREY = Color(0xFFE6E9F0)

    /** AI non-accent columns: a blue-grey — the "tint AI blue-ish" of the spec. */
    val AI_GREY = Color(0xFFB9CCE6)

    /** Human accent columns: the brand red. */
    val HUMAN_ACCENT = BbxAccent

    /** AI accent columns: the brand blue. */
    val AI_ACCENT = WaveBlue
}

/**
 * Pure geometry / signal math for the Segmented Equalizer. No Compose, no
 * android.* — every visible property of the style is a function here so the JVM
 * tests can pin it without a device, in the aurora tradition.
 */
internal object SegmentedEqMath {

    /** A resolved LED unit for one column stack: the (possibly degraded) segment and
     *  gap heights in px and how many rows fit. */
    class SegUnit(val segPx: Float, val gapPx: Float, val rows: Int)

    /** Sanitise one array read: out-of-range index or NaN → 0, else clamp 0..1. */
    private fun valueOf(values: FloatArray, index: Int): Float {
        if (index < 0 || index >= values.size) return 0f
        val v = values[index]
        return if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
    }

    /**
     * How many columns a surface [widthPx] wide carries at [density] — width divided
     * by [SegmentedEqSpec.COLUMN_PITCH_DP], floored at MIN_COLUMNS and capped at
     * MAX_COLUMNS (one column per spectrum bin; see the file header). Total: a
     * degenerate width or density falls back to the floor rather than dividing by
     * zero or asking for a single mega-bar.
     */
    fun columnCount(widthPx: Float, density: Float): Int {
        if (widthPx.isNaN() || density.isNaN() || widthPx <= 0f || density <= 0f) {
            return SegmentedEqSpec.MIN_COLUMNS
        }
        val pitch = SegmentedEqSpec.COLUMN_PITCH_DP * density
        if (pitch <= 0f) return SegmentedEqSpec.MIN_COLUMNS
        return Math.round(widthPx / pitch)
            .coerceIn(SegmentedEqSpec.MIN_COLUMNS, SegmentedEqSpec.MAX_COLUMNS)
    }

    /** One column's pitch (bar + gap) in px: the surface split evenly [columns] ways. */
    fun pitchPx(widthPx: Float, columns: Int): Float =
        if (columns <= 0 || widthPx <= 0f) 0f else widthPx / columns

    /**
     * The lit bar's width in px inside a [pitchPx] column: the fill fraction, but never
     * wider than [SegmentedEqSpec.MAX_BAR_W_DP] — on a surface wide enough to have
     * saturated the column count the extra room becomes gap instead of fat bars.
     */
    fun barWidthPx(pitchPx: Float, density: Float): Float {
        if (pitchPx <= 0f) return 0f
        val natural = pitchPx * SegmentedEqSpec.COLUMN_FILL_FRACTION
        val cap = SegmentedEqSpec.MAX_BAR_W_DP * density.coerceAtLeast(0f)
        return minOf(natural, cap).coerceAtLeast(0f)
    }

    /**
     * First spectrum bin owned by [columnIndex] of [columnCount]. The columns PARTITION
     * the bins: `floor` on both edges makes one column's end its neighbour's start
     * exactly, so every bin belongs to one column and none is skipped.
     */
    fun binRangeStart(columnIndex: Int, columnCount: Int): Int {
        val n = columnCount.coerceAtLeast(1)
        val c = columnIndex.coerceIn(0, n - 1)
        return (c.toLong() * AuroraAnalyser.BINS / n).toInt()
            .coerceIn(0, AuroraAnalyser.BINS - 1)
    }

    /**
     * One past the last bin owned by [columnIndex] — at least one bin, so a column
     * count above [AuroraAnalyser.BINS] (which the cap prevents, but the function stays
     * total) repeats a real measurement rather than inventing one between two.
     */
    fun binRangeEnd(columnIndex: Int, columnCount: Int): Int {
        val n = columnCount.coerceAtLeast(1)
        val c = columnIndex.coerceIn(0, n - 1)
        val end = ((c + 1).toLong() * AuroraAnalyser.BINS / n).toInt()
        return end.coerceIn(binRangeStart(columnIndex, columnCount) + 1, AuroraAnalyser.BINS)
    }

    /**
     * Which band drives [columnIndex] when there is no spectrum — the same log-frequency
     * axis at the band channel's own resolution, a quarter of the columns each.
     */
    fun bandForColumn(columnIndex: Int, columnCount: Int): Int {
        val n = columnCount.coerceAtLeast(1)
        val c = columnIndex.coerceIn(0, n - 1)
        return (c.toLong() * AuroraAnalyser.BANDS / n).toInt()
            .coerceIn(0, AuroraAnalyser.BANDS - 1)
    }

    /**
     * The drive for [columnIndex] of [columnCount], 0..1 and NaN-safe on every input.
     *
     * With a spectrum ([bins] non-null) it is the LOUDEST real bin the column owns —
     * a measurement, not an interpolation, and no function of time. Without one it is
     * the band covering the same slice of the frequency axis. See the file header for
     * why there is no curve and no wobble.
     */
    fun columnDrive(
        bins: FloatArray?,
        bands: FloatArray,
        columnIndex: Int,
        columnCount: Int,
    ): Float {
        if (bins == null) return valueOf(bands, bandForColumn(columnIndex, columnCount))
        var m = 0f
        val lo = binRangeStart(columnIndex, columnCount)
        val hi = binRangeEnd(columnIndex, columnCount)
        for (k in lo until hi) {
            val v = valueOf(bins, k)
            if (v > m) m = v
        }
        return m
    }

    /** Rows of (segment+gap) that fit in [usablePx]. n rows span n*seg+(n-1)*gap. */
    fun rows(usablePx: Float, segPx: Float, gapPx: Float): Int {
        if (usablePx <= 0f || segPx <= 0f) return 0
        return floor((usablePx + gapPx) / (segPx + gapPx)).toInt().coerceAtLeast(0)
    }

    /**
     * Resolve the LED unit for [usablePx] of vertical room, shrinking the fixed
     * [baseSegPx]/[baseGapPx] ONLY when the column could not otherwise stack
     * [minRows] rows (the small-height degrade). The seg:gap ratio is preserved so
     * a degraded LED still looks like the same LED, just smaller — never a mesa.
     */
    fun resolveUnit(usablePx: Float, baseSegPx: Float, baseGapPx: Float, minRows: Int): SegUnit {
        if (usablePx <= 0f || baseSegPx <= 0f) return SegUnit(0f, 0f, 0)
        val natural = rows(usablePx, baseSegPx, baseGapPx)
        if (natural >= minRows) return SegUnit(baseSegPx, baseGapPx, natural)
        // Too short for minRows at the natural size: fit EXACTLY minRows by scaling
        // seg (and gap, keeping the ratio) down. minRows*seg + (minRows-1)*gap = usable.
        val ratio = if (baseSegPx > 0f) baseGapPx / baseSegPx else 0f
        val denom = minRows + (minRows - 1) * ratio
        if (denom <= 0f) return SegUnit(0f, 0f, 0)
        val seg = (usablePx / denom).coerceAtLeast(0f)
        if (seg <= 0f) return SegUnit(0f, 0f, 0)
        return SegUnit(seg, seg * ratio, minRows)
    }

    /**
     * How many rows a [level] (0..1) lights out of [rows], never fewer than
     * [minWhenPresent] (a present column keeps its baseline LED). NaN → 0.
     */
    fun litCount(level: Float, rows: Int, minWhenPresent: Int): Int {
        if (rows <= 0) return 0
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        val n = Math.round(l * rows)
        return n.coerceIn(minWhenPresent.coerceIn(0, rows), rows)
    }

    /**
     * The row index (0 = baseline) where the floating peak-hold sits for a [peak]
     * fraction, or -1 if there is no peak to draw. Uses the same quantisation as
     * [litCount] so the peak lands on a real LED row.
     */
    fun peakRow(peak: Float, rows: Int): Int {
        if (rows <= 0) return -1
        val p = if (peak.isNaN()) 0f else peak.coerceIn(0f, 1f)
        if (p <= 0f) return -1
        return (Math.round(p * rows) - 1).coerceIn(0, rows - 1)
    }

    /**
     * Advance one peak-hold: snap UP to a new [level], else fall by [fallPerSec]·[dt]
     * but never below the current level (the peak caps the live column, it does not
     * dive under it). NaN-safe; a non-positive/NaN dt holds the peak still.
     */
    fun decayPeak(prevPeak: Float, level: Float, dt: Float, fallPerSec: Float): Float {
        val prev = if (prevPeak.isNaN()) 0f else prevPeak.coerceIn(0f, 1f)
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        if (l >= prev) return l
        val d = if (dt.isNaN() || dt <= 0f) 0f else dt
        return (prev - fallPerSec * d).coerceAtLeast(l)
    }

    /** Whether [columnIndex] is one of the deterministic accent (red/blue) columns. */
    fun isAccentColumn(columnIndex: Int): Boolean =
        Math.floorMod(columnIndex, SegmentedEqSpec.ACCENT_MODULO) == SegmentedEqSpec.ACCENT_PHASE

    /** The base colour for a column of [speaker] at [columnIndex] — accent vs grey,
     *  human (red/white) vs AI (blue/blue-grey). Pure, so a test can pin the scheme. */
    fun columnColor(speaker: AuroraSpeaker, columnIndex: Int): Color {
        val accent = isAccentColumn(columnIndex)
        return if (speaker == AuroraSpeaker.HUMAN) {
            if (accent) SegmentedEqSpec.HUMAN_ACCENT else SegmentedEqSpec.HUMAN_GREY
        } else {
            if (accent) SegmentedEqSpec.AI_ACCENT else SegmentedEqSpec.AI_GREY
        }
    }

    /**
     * Worst-case fraction of the surface the SrcOver (occluding) cores cover: every
     * column full, horizontal fill × vertical fill. The occlusion-budget invariant
     * (must stay ≤ ~0.55) is pinned against THIS in the test — one number, one truth.
     */
    fun maxCoreOcclusion(): Float {
        val vFill = SegmentedEqSpec.SEGMENT_HEIGHT_DP /
            (SegmentedEqSpec.SEGMENT_HEIGHT_DP + SegmentedEqSpec.SEGMENT_GAP_DP)
        return SegmentedEqSpec.COLUMN_FILL_FRACTION * vFill
    }
}

private class SegmentedEqRenderer : WaveformRenderer {

    private val speakers = AuroraSpeaker.entries

    /** Per-speaker peak-hold fractions, kept alive across frames (one per column).
     *  Sized for the MAXIMUM column count so a surface resize never reallocates on the
     *  frame thread; the tail beyond the live count is driven to zero every frame so a
     *  stale peak up there can never hold [needsFrames] open and defeat the idle park. */
    private val peaks: Array<FloatArray> =
        Array(speakers.size) { FloatArray(SegmentedEqSpec.MAX_COLUMNS) }

    /** Reused per-frame scratch so the draw allocates nothing it can avoid. */
    private val bandScratch = FloatArray(AuroraAnalyser.BANDS)
    private val binScratch = FloatArray(AuroraAnalyser.BINS)
    private val levelScratch = FloatArray(SegmentedEqSpec.MAX_COLUMNS)

    /** The largest peak still standing after the last frame — the idle-park gate. */
    private var maxStandingPeak = 0f

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        val feed = frame.feed
        val humanPresent = feed.present(AuroraSpeaker.HUMAN)
        val aiPresent = feed.present(AuroraSpeaker.AI)
        val bothPresent = humanPresent && aiPresent

        val columns = SegmentedEqMath.columnCount(w, density)
        val segBase = SegmentedEqSpec.SEGMENT_HEIGHT_DP.dp.toPx()
        val gapBase = SegmentedEqSpec.SEGMENT_GAP_DP.dp.toPx()
        val corner = CornerRadius(SegmentedEqSpec.SEGMENT_CORNER_DP.dp.toPx())
        val inset = SegmentedEqSpec.EDGE_INSET_DP.dp.toPx()
        val centerGap = SegmentedEqSpec.CENTER_GAP_DP.dp.toPx()

        // Vertical layout. One-voice: full-height, bottom-anchored, rising up. Two-
        // voice: split at the centre — human bottom-half up, AI top-half down.
        val innerTop = inset
        val innerBottom = h - inset
        val innerHeight = (innerBottom - innerTop).coerceAtLeast(0f)

        // Advance peaks for BOTH speakers every frame (an absent speaker's peaks fall
        // to zero so the idle park can eventually engage), but draw only the present.
        var maxPeak = 0f
        for (si in speakers.indices) {
            val speaker = speakers[si]
            val present = if (speaker == AuroraSpeaker.HUMAN) humanPresent else aiPresent

            // Fill this speaker's channels, then the live column drives. The spectrum is
            // the source when the speaker publishes one; the bands are the fallback.
            for (b in bandScratch.indices) bandScratch[b] = feed.band(speaker, b)
            val bins = if (feed.hasSpectrum(speaker)) {
                for (k in binScratch.indices) binScratch[k] = feed.bin(speaker, k)
                binScratch
            } else {
                null
            }
            val floor = if (present) frame.restLevel else 0f
            for (c in levelScratch.indices) {
                // Columns past the live count are driven to ZERO, not left stale — see
                // the note on [peaks].
                val drive = if (c < columns) {
                    Aurora.restBlend(
                        floor,
                        Aurora.displayCurve(SegmentedEqMath.columnDrive(bins, bandScratch, c, columns)),
                    )
                } else {
                    0f
                }
                levelScratch[c] = drive.coerceIn(0f, 1f)
            }
            // Peak-hold update (absent → drives are 0, so peaks decay to 0).
            val ring = peaks[si]
            for (c in ring.indices) {
                ring[c] = SegmentedEqMath.decayPeak(
                    ring[c], levelScratch[c], frame.dtSec, 1f / SegmentedEqSpec.PEAK_FALL_SEC,
                )
                if (ring[c] > maxPeak) maxPeak = ring[c]
            }

            if (!present) continue

            // Where this speaker's stack lives and which way it grows.
            val usable: Float
            val baselineY: Float
            val dir: Float // -1 grows up, +1 grows down
            if (!bothPresent) {
                usable = innerHeight
                baselineY = innerBottom
                dir = -1f
            } else {
                usable = ((innerHeight - centerGap) / 2f).coerceAtLeast(0f)
                if (speaker == AuroraSpeaker.HUMAN) {
                    baselineY = innerBottom
                    dir = -1f
                } else {
                    baselineY = innerTop
                    dir = +1f
                }
            }

            drawColumns(speaker, columns, baselineY, dir, usable, segBase, gapBase, corner, ring)
        }

        maxStandingPeak = maxPeak
    }

    private fun DrawScope.drawColumns(
        speaker: AuroraSpeaker,
        columns: Int,
        baselineY: Float,
        dir: Float,
        usablePx: Float,
        segBase: Float,
        gapBase: Float,
        corner: CornerRadius,
        peakRing: FloatArray,
    ) {
        val w = size.width
        val unit = SegmentedEqMath.resolveUnit(
            usablePx, segBase, gapBase, SegmentedEqSpec.SEGMENT_MIN_ROWS,
        )
        if (unit.rows <= 0 || unit.segPx <= 0f) return

        val pitch = SegmentedEqMath.pitchPx(w, columns)
        val barW = SegmentedEqMath.barWidthPx(pitch, density)
        val barInset = (pitch - barW) / 2f
        val step = unit.segPx + unit.gapPx

        for (c in 0 until columns) {
            val x = c * pitch + barInset
            val color = SegmentedEqMath.columnColor(speaker, c)
            val lit = SegmentedEqMath.litCount(
                levelScratch[c], unit.rows, SegmentedEqSpec.MIN_LIT_WHEN_PRESENT,
            )

            // Lit LED stack: additive halo (never dims the field) + a solid-ish core.
            for (i in 0 until lit) {
                val near = baselineY + dir * (i * step)
                val far = near + dir * unit.segPx
                val top = minOf(near, far)
                drawSegment(x, top, barW, unit.segPx, corner, color)
            }

            // Floating peak-hold: one dim additive LED above the stack.
            val pr = SegmentedEqMath.peakRow(peakRing[c], unit.rows)
            if (pr >= 0) {
                val near = baselineY + dir * (pr * step)
                val far = near + dir * unit.segPx
                val top = minOf(near, far)
                drawRoundRect(
                    color = color,
                    topLeft = Offset(x, top),
                    size = Size(barW, unit.segPx),
                    cornerRadius = corner,
                    alpha = SegmentedEqSpec.PEAK_ALPHA,
                    style = Fill,
                    blendMode = BlendMode.Plus,
                )
            }
        }
    }

    /** One lit LED: an additive halo (Plus) then a brighter SrcOver core — the halo is
     *  "light, not paint", so the field behind is never erased. */
    private fun DrawScope.drawSegment(
        x: Float,
        top: Float,
        barW: Float,
        segPx: Float,
        corner: CornerRadius,
        color: Color,
    ) {
        drawRoundRect(
            color = color,
            topLeft = Offset(x, top),
            size = Size(barW, segPx),
            cornerRadius = corner,
            alpha = SegmentedEqSpec.GLOW_ALPHA,
            style = Fill,
            blendMode = BlendMode.Plus,
        )
        drawRoundRect(
            color = color,
            topLeft = Offset(x, top),
            size = Size(barW, segPx),
            cornerRadius = corner,
            alpha = SegmentedEqSpec.CORE_ALPHA,
            style = Fill,
            blendMode = BlendMode.SrcOver,
        )
    }

    /**
     * Hold frames while any voice is audible OR any peak-hold is still falling. The
     * default gate parks the instant every voice goes silent; here a peak that has
     * not yet fallen would freeze mid-air, so we keep painting until the last peak
     * has crossed [SegmentedEqSpec.PEAK_PARK_EPS].
     */
    override fun needsFrames(voices: List<AuroraVoice>, feed: WaveformFeed): Boolean {
        for (i in voices.indices) if (voices[i].hasEnergy()) return true
        return maxStandingPeak > SegmentedEqSpec.PEAK_PARK_EPS
    }
}
