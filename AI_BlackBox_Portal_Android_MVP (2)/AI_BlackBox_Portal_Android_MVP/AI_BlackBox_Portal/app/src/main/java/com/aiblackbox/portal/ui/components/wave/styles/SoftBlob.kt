package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraSpectrum
import com.aiblackbox.portal.ui.components.wave.WaveformFrame
import com.aiblackbox.portal.ui.components.wave.WaveformRenderer
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.exp
import kotlin.math.ln
import kotlin.math.sin
import kotlin.math.sqrt

// =============================================================================
// Soft Blob Wave — reference sheet concept #4 (tagged TTS OUTPUT).
//
// Organic mirrored MOUNDS: a handful of soft-edged filled lobes across the width,
// mirrored above AND below the horizontal centre axis into one metaball-ish
// silhouette. The silhouette is a SUM OF GAUSSIANS along x — each lobe is a smooth
// bump centred at its own position, and where two bumps overlap their heights add,
// so the outline swells and merges the way the reference sheet's blob does rather
// than reading as separate bars. The centre lobe is BROAD and driven by the lowest
// frequencies (the body of the voice); the side lobes are SMALLER and driven by
// progressively higher ones (brightness/sibilance) — so a booming vowel fattens
// the middle while a bright sibilant flicks the wings.
//
// EVERY LOBE READS ITS OWN SLICE OF THE SPECTRUM. That is the load-bearing change
// from the first cut of this style, which mapped five lobes onto four analyser
// bands and therefore drove the two OUTER lobes from the same band — they moved as
// one, and a mirrored pair that always agrees is exactly the "bland" the operator
// reported. A lobe now owns a frequency REGION in Hz ([SoftBlob.Slice]), and the
// five regions tile the spectrum map without gaps or overlap, so no two lobes can
// ever be handed the same number and no part of the audible range drives nothing.
//
// A SLICE IS RESOLVED FROM WHICHEVER CHANNEL IS LIVE, and the two are not
// interchangeable, which is why the code branches instead of averaging:
//   - SPECTRUM PRESENT (the [AuroraAnalyser.BINS] log-frequency channel): the bins
//     are RAW — amplitude-calibrated against a full-scale sine, with none of the
//     band channel's measured auto-gain, headroom or knee behind them. So they are
//     read for SHAPE only, as a ratio: each slice takes the loudest bin it owns,
//     the set is normalised by the loudest slice, and the whole thing is scaled by
//     the speaker's calibrated overall level. Ratios cancel the calibration this
//     channel does not have, so the style cannot invent one.
//   - NO SPECTRUM (every shipped call site today — the bins are produced but not
//     yet wired to a visualizer): the slice reads the four band drives at its
//     centre frequency, log-interpolated between the two bands whose Goertzel
//     centres bracket it. Four bands still cannot resolve five regions exactly, but
//     five distinct MIXTURES of them are five distinct numbers, which is all the
//     geometry needs to stop moving in lockstep.
//
// NO PRIVATE SMOOTHING STAGE. The first cut carried per-band attack/release
// envelopes across frames, which put a THIRD lag in series with the analyser's own
// auto-gain release and the renderer's — three stacked smoothers is what turned
// syllables into a slow heave. The envelopes are gone: a lobe's height is a pure
// function of this frame's drives, so the blob is as quick as the audio channel
// underneath it and the style is refresh-rate independent BY CONSTRUCTION rather
// than by an exponential that composes over dt. The style keeps exactly one motion
// of its own, the slow [SoftBlob.breatheFactor] pulse, which is a function of the
// shared wall clock and so is also identical at 60 and 120 Hz.
//
// THE ONE PERCEPTUAL CURVE. Drives reach geometry through [Aurora.displayCurve] and
// nothing else. The app has exactly one curve and it lives in Aurora; this style
// calls it rather than restating a square root, so a re-tune there moves this style
// with it (see the note on [Aurora.DISPLAY_GAMMA] — the duplicate curves it lists
// were deleted precisely so no consumer keeps a private copy).
//
// THE TWO-VOICE CONTRACT (plan's table: "red-tinted mounds + blue-tinted mounds,
// additive overlap"):
//   - one voice   → one mound set centred on the axis, glowing its identity colour
//     (red = human, blue = AI).
//   - both voices → the human's mound set is x-shifted LEFT and the AI's RIGHT by
//     [SoftBlob.INTERLEAVE_FRAC] so the two silhouettes INTERLEAVE rather than sit
//     exactly on top of each other; each carries its own colour glow and their
//     additive (BlendMode.Plus) glows sum where the lobes overlap. The misty white
//     fills stack too, which is the only place occlusion doubles — budgeted below.
//
// A few tiny drifting MOTES float around the silhouette (the reference's little
// spheres): deterministic orbits that are a pure function of a per-mote index and
// the shared wall-clock phase (so they too are refresh-rate independent), nudged
// brighter/bigger by the speaker's level and faded out as the voice goes quiet.
// Motes are OFF at the 40 dp player-bar height (see the degrade).
//
// VISUAL BUDGET (inherited from Aurora v33, a REQUIREMENT not a suggestion):
//   - OCCLUSION rides on the ONE SrcOver pass per speaker — the misty white fill.
//     Its alpha is capped at [SoftBlob.WHITE_ALPHA_MAX]; the worst case is the
//     two-voice overlap where both white fills stack, occluding
//     1 − (1 − a)² = 0.42 at a = 0.24, comfortably under the ≤55% ceiling. Every
//     other pass (the coloured glow, the core, the motes) is additive light, which
//     does not occlude at all.
//   - WHITE-OUT (additive) is bounded. The Plus passes are, per speaker, the glow
//     wash (≤[SoftBlob.GLOW_ALPHA_MAX]) and the central core (≤[SoftBlob.CORE_ALPHA_MAX]),
//     plus the motes (≤[SoftBlob.MOTE_ALPHA_MAX]). Two speakers fully overlapping
//     sum to 2·(GLOW+CORE)+MOTE = 1.54, under Aurora's 1.6 white-out ceiling.
//   - NO MESA at 40 dp. The silhouette height is a fraction of the AVAILABLE half
//     (half-height minus a rail inset) put through a SMOOTH saturating curve
//     ([SoftBlob.saturate]) capped at [SoftBlob.FILL_CEIL] < 1, so no matter how many
//     gaussians pile up the outline asymptotes toward — and never reaches — the rail.
//     The 40 dp player bar additionally degrades to 3 lobes with motes off so it
//     reads as a soft mound, not a shrunken copy of the 5-lobe strip.
//   - NaN-safe: every band read comes through the feed (already sanitised) and the
//     pure maths guards NaN explicitly where `coerceIn`/`sqrt` would pass it through
//     (JLS 15.20.1).
//   - IDLE-PARK friendly: the breathing and the motes are scaled by live energy, so
//     a present-but-silent voice settles to a static resting mound and then demands
//     no frames (the inherited [WaveformRenderer.needsFrames], which reads the
//     PASSED voices — never the feed, whose rings stop advancing once the loop parks
//     and would deadlock it shut).
//
// The maths lives in [SoftBlob] as pure functions so the geometry, the budgets and
// the degrade are JVM-testable without a device; this renderer is only a traversal
// of them. See SoftBlobTest.
// =============================================================================

/**
 * Build a Soft Blob Wave renderer. THE public surface the registry keeps stable:
 * WaveformStyles registers this style by calling [softBlobRenderer], so this
 * function's name and shape must not change even when the body does.
 */
internal fun softBlobRenderer(): WaveformRenderer = SoftBlobRenderer()

private class SoftBlobRenderer : WaveformRenderer {

    /** The two speakers, in a fixed order so HUMAN draws before AI every frame. */
    private val speakers = AuroraSpeaker.entries

    /**
     * Per-speaker scratch, owned so the draw path allocates NOTHING per frame: the
     * four band drives, the [AuroraAnalyser.BINS] spectrum (only filled when one is
     * published), and the per-SLICE lobe heights the geometry reads.
     */
    private val bandScratch = Array(AuroraSpeaker.entries.size) { FloatArray(AuroraAnalyser.BANDS) }
    private val binScratch = Array(AuroraSpeaker.entries.size) { FloatArray(AuroraAnalyser.BINS) }
    private val heights = Array(AuroraSpeaker.entries.size) { FloatArray(SoftBlob.SLICES.size) }
    private val present = BooleanArray(AuroraSpeaker.entries.size)
    private val liveLevel = FloatArray(AuroraSpeaker.entries.size)

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        val oneDp = 1.dp.toPx()
        val small = SoftBlob.isSmallHeight(h, SoftBlob.SMALL_HEIGHT_DP * oneDp)
        val lobes = if (small) SoftBlob.SMALL_LOBES else SoftBlob.FULL_LOBES

        val inset = SoftBlob.railInsetPx(h, SoftBlob.INSET_MIN_DP * oneDp)
        val availHalf = SoftBlob.availableHalfPx(h, inset)
        val centre = h / 2f

        // --- resolve this frame's per-slice lobe heights ----------------------
        // A quiet-but-PRESENT voice floors to restLevel so it keeps a soft resting
        // mound (a live mic breathes; the player bar rests at 0 and goes truly flat).
        for (si in speakers.indices) {
            val sp = speakers[si]
            val isPresent = frame.feed.present(sp)
            present[si] = isPresent
            liveLevel[si] = if (isPresent) frame.feed.level(sp) else 0f
            if (!isPresent) continue

            val bands = bandScratch[si]
            for (b in bands.indices) bands[b] = frame.feed.band(sp, b)
            val bins = if (frame.feed.hasSpectrum(sp)) {
                val row = binScratch[si]
                for (k in row.indices) row[k] = frame.feed.bin(sp, k)
                row
            } else {
                null
            }

            val row = heights[si]
            SoftBlob.sliceDrives(bands, bins, liveLevel[si], row)
            for (i in row.indices) row[i] = SoftBlob.lobeHeight(row[i], frame.restLevel)
        }

        val twoVoice = present[SpeakerIndex.HUMAN] && present[SpeakerIndex.AI]

        if (present[SpeakerIndex.HUMAN]) {
            val shift = if (twoVoice) -SoftBlob.INTERLEAVE_FRAC else 0f
            drawMoundSet(
                w, h, centre, availHalf, lobes, heights[SpeakerIndex.HUMAN], shift,
                colour = BbxAccent, phaseSec = frame.phaseSec, level = liveLevel[SpeakerIndex.HUMAN],
                small = small, speakerOrdinal = AuroraSpeaker.HUMAN.ordinal,
            )
        }
        if (present[SpeakerIndex.AI]) {
            val shift = if (twoVoice) SoftBlob.INTERLEAVE_FRAC else 0f
            drawMoundSet(
                w, h, centre, availHalf, lobes, heights[SpeakerIndex.AI], shift,
                colour = WaveBlue, phaseSec = frame.phaseSec, level = liveLevel[SpeakerIndex.AI],
                small = small, speakerOrdinal = AuroraSpeaker.AI.ordinal,
            )
        }
    }

    /**
     * Paint one speaker's mound set: the misty white silhouette (the ONE occluding
     * pass), the identity-colour glow wash over it, the concentrated central core,
     * and — unless degraded — the drifting motes. All but the white fill are
     * additive (BlendMode.Plus) light.
     */
    private fun DrawScope.drawMoundSet(
        w: Float,
        h: Float,
        centre: Float,
        availHalf: Float,
        lobes: List<SoftBlob.Lobe>,
        heights: FloatArray,
        shiftFrac: Float,
        colour: Color,
        phaseSec: Float,
        level: Float,
        small: Boolean,
        speakerOrdinal: Int,
    ) {
        if (availHalf <= 0f) return

        // Breathing is scaled by live energy so a settled/silent mound is static
        // (idle-park correct); a speaking mound gently pulses.
        val breathe = SoftBlob.breatheFactor(phaseSec, level)

        // Sample the mirrored silhouette across the width into one closed path.
        val steps = SoftBlob.sampleSteps(w)
        val top = Path()
        val bottom = Path()
        for (s in 0..steps) {
            val xFrac = s.toFloat() / steps
            val x = xFrac * w
            val raw = SoftBlob.moundRaw(xFrac, lobes, heights, shiftFrac)
            val ext = SoftBlob.silhouettePx(raw, availHalf, breathe)
            if (s == 0) {
                top.moveTo(x, centre - ext)
                bottom.moveTo(x, centre + ext)
            } else {
                top.lineTo(x, centre - ext)
                bottom.lineTo(x, centre + ext)
            }
        }
        // Close top→bottom into a single filled blob (walk the bottom edge back).
        val blob = Path().apply {
            addPath(top)
            for (s in steps downTo 0) {
                val xFrac = s.toFloat() / steps
                val x = xFrac * w
                val raw = SoftBlob.moundRaw(xFrac, lobes, heights, shiftFrac)
                val ext = SoftBlob.silhouettePx(raw, availHalf, breathe)
                lineTo(x, centre + ext)
            }
            close()
        }

        // 1) Misty white fill — the ONLY SrcOver (occluding) pass. Alpha rises with
        //    level so a loud mound is denser mist; capped for the occlusion budget.
        drawPath(blob, BbxWhite, alpha = SoftBlob.whiteAlpha(level))

        // 2) Identity-colour glow wash over the whole silhouette — additive light.
        drawPath(blob, colour, alpha = SoftBlob.glowAlpha(level), blendMode = BlendMode.Plus)

        // 3) Concentrated core at the CENTRE lobe — a soft radial that falls to
        //    transparent, so the middle of the blob reads as a lit heart.
        val coreCentreX = (SoftBlob.centreLobeXFrac(lobes) + shiftFrac).coerceIn(0f, 1f) * w
        val coreRadius = (w * SoftBlob.CORE_RADIUS_FRAC).coerceAtMost(availHalf * SoftBlob.CORE_RADIUS_HALF_CAP)
        if (coreRadius > 0f) {
            val brush = Brush.radialGradient(
                colors = listOf(colour.copy(alpha = SoftBlob.coreAlpha(level)), Color.Transparent),
                center = Offset(coreCentreX, centre),
                radius = coreRadius,
            )
            drawCircle(brush, radius = coreRadius, center = Offset(coreCentreX, centre), blendMode = BlendMode.Plus)
        }

        // 4) Motes — off at the 40 dp degrade, faded by level so they vanish with
        //    the voice (keeping idle-park honest).
        if (!small) {
            val moteAlphaBase = SoftBlob.moteAlpha(level)
            if (moteAlphaBase > 0f) {
                val minDim = minOf(w, h)
                for (m in 0 until SoftBlob.MOTE_COUNT) {
                    val mx = SoftBlob.moteX(m, phaseSec, w, speakerOrdinal)
                    val my = SoftBlob.moteY(m, phaseSec, centre, availHalf, speakerOrdinal)
                    val r = SoftBlob.moteRadiusPx(m, level, minDim)
                    val a = moteAlphaBase * SoftBlob.moteTwinkle(m, phaseSec)
                    drawCircle(colour, radius = r, center = Offset(mx, my), alpha = a, blendMode = BlendMode.Plus)
                }
            }
        }
    }

    /** Fixed speaker slot indices — HUMAN then AI — matching [AuroraSpeaker.ordinal]. */
    private object SpeakerIndex {
        const val HUMAN = 0
        const val AI = 1
    }
}

// =============================================================================
// Geometry + budgets — pure, no Compose draw calls, unit-testable on the JVM.
// =============================================================================

internal object SoftBlob {

    /**
     * One soft mound. [xFrac] is its centre along the width (0..1), [slice] the
     * SPECTRAL SLICE whose drive sets its height, [sigmaFrac] its half-spread as a
     * fraction of the width (broad centre, narrow wings), and [ampScale] its
     * relative peak height (the centre lobe is the tallest at 1.0).
     */
    data class Lobe(
        val xFrac: Float,
        val slice: Int,
        val sigmaFrac: Float,
        val ampScale: Float,
    )

    // ---------------------------------------------------------- spectral slices

    /**
     * A contiguous region of the audible range, in Hz, that ONE lobe is driven by.
     * The frequency band is the unit of meaning here rather than a channel index,
     * because the two channels that can answer for it are different widths
     * ([AuroraAnalyser.BANDS] = 4, [AuroraAnalyser.BINS] = 24) and only a frequency
     * means the same thing to both.
     */
    class Slice(val loHz: Float, val hiHz: Float) {
        /** Geometric centre — the frequency this slice is "about" on a log axis. */
        val centreHz: Float = sqrt(loHz.toDouble() * hiHz).toFloat()
    }

    /**
     * The five slices, low → high, TILING the spectrum map end to end: each one's
     * top edge is the next one's bottom, the first starts at the map's floor and the
     * last ends at its ceiling. Tiling is the property that matters, not the exact
     * edges — it is what guarantees no lobe shares a drive with another (the "two
     * outer lobes moved as one" defect) and that no part of the range drives nothing.
     *
     * The edges themselves are voice landmarks: fundamental/body, first formant,
     * upper formant (intelligibility), presence, air/sibilance.
     */
    val SLICES: List<Slice> = listOf(
        Slice(AuroraSpectrum.MIN_HZ, 400f),
        Slice(400f, 1000f),
        Slice(1000f, 2400f),
        Slice(2400f, 4200f),
        Slice(4200f, AuroraSpectrum.MAX_HZ),
    )

    /**
     * Geometric centre of each analyser band, DERIVED from its Goertzel probe pair
     * rather than written down, so the band map and this one cannot drift apart.
     */
    private val BAND_CENTRE_HZ = FloatArray(AuroraAnalyser.BANDS) { b ->
        val probes = AuroraAnalyser.PROBES[b]
        sqrt(probes.first().toDouble() * probes.last()).toFloat()
    }

    /** Half-open range of spectrum bins each slice owns, by bin CENTRE frequency. */
    private val SLICE_BIN_LO = IntArray(SLICES.size)
    private val SLICE_BIN_HI = IntArray(SLICES.size)

    init {
        for (i in SLICES.indices) {
            var lo = -1
            var hi = 0
            for (k in 0 until AuroraAnalyser.BINS) {
                val c = AuroraSpectrum.centreHz(k)
                if (c >= SLICES[i].loHz && c < SLICES[i].hiHz) {
                    if (lo < 0) lo = k
                    hi = k + 1
                }
            }
            SLICE_BIN_LO[i] = if (lo < 0) 0 else lo
            SLICE_BIN_HI[i] = if (lo < 0) 0 else hi
        }
    }

    /** How many spectrum bins slice [index] owns. Pinned by test to be non-empty. */
    fun sliceBinCount(index: Int): Int {
        val s = index.coerceIn(0, SLICES.size - 1)
        return SLICE_BIN_HI[s] - SLICE_BIN_LO[s]
    }

    /** NaN-safe, range-safe read of one band drive. */
    private fun bandAt(bands: FloatArray, index: Int): Float {
        if (index < 0 || index >= bands.size) return 0f
        val v = bands[index]
        return if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
    }

    /**
     * The band channel read AT a frequency: log-interpolated between the two band
     * centres that bracket [hz], clamped to the end bands outside them. LOG, not
     * linear, because the bands are geometrically spaced and a linear blend would put
     * the midpoint of 632 Hz and 2040 Hz at 1336 Hz — a frequency neither band is
     * about. This is what lets four bands answer for five (or three) distinct slices:
     * five different mixtures are five different numbers.
     */
    fun bandAtHz(bands: FloatArray, hz: Float): Float {
        if (hz.isNaN()) return 0f
        val last = AuroraAnalyser.BANDS - 1
        if (hz <= BAND_CENTRE_HZ[0]) return bandAt(bands, 0)
        if (hz >= BAND_CENTRE_HZ[last]) return bandAt(bands, last)
        var i = 0
        while (i < last - 1 && BAND_CENTRE_HZ[i + 1] < hz) i++
        val lo = BAND_CENTRE_HZ[i]
        val hi = BAND_CENTRE_HZ[i + 1]
        val t = (ln(hz / lo) / ln(hi / lo)).coerceIn(0f, 1f)
        return bandAt(bands, i) + (bandAt(bands, i + 1) - bandAt(bands, i)) * t
    }

    /** The loudest bin slice [index] owns, 0 when it owns none. NaN-safe. */
    fun sliceBinMax(bins: FloatArray, index: Int): Float {
        val s = index.coerceIn(0, SLICES.size - 1)
        var m = 0f
        for (k in SLICE_BIN_LO[s] until minOf(SLICE_BIN_HI[s], bins.size)) {
            val v = bins[k]
            if (!v.isNaN() && v > m) m = v
        }
        return m
    }

    /**
     * Fill [out] with one 0..1 drive per slice — the whole spectral input to the
     * geometry, in one place.
     *
     * [bins] null (no spectrum published this frame) reads the band channel at each
     * slice's centre. A spectrum present reads the bins for SHAPE ONLY, as a ratio
     * normalised by the loudest slice and scaled by the calibrated [level]: the bin
     * channel is raw and amplitude-calibrated against a full-scale sine, with none of
     * the band channel's measured auto-gain or headroom behind it, so its absolute
     * value is not a drive and a ratio is the only honest thing to take from it.
     *
     * Writes nothing beyond `min(out.size, SLICES.size)`; the renderer sizes [out]
     * once and reuses it, so this never allocates.
     */
    fun sliceDrives(bands: FloatArray, bins: FloatArray?, level: Float, out: FloatArray) {
        val n = minOf(out.size, SLICES.size)
        if (bins == null) {
            for (i in 0 until n) out[i] = bandAtHz(bands, SLICES[i].centreHz)
            return
        }
        var peak = 0f
        for (i in 0 until n) {
            val v = sliceBinMax(bins, i)
            out[i] = v
            if (v > peak) peak = v
        }
        if (peak <= 0f) {
            for (i in 0 until n) out[i] = 0f
            return
        }
        val lv = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        for (i in 0 until n) out[i] = (out[i] / peak) * lv
    }

    /**
     * A lobe's drawn height from its slice drive: THE ONE perceptual curve, with the
     * present-but-silent rest floor under it. The curve is [Aurora.displayCurve] and
     * the floor rule is [Aurora.restBlend]; neither is restated here — the app has
     * exactly one of each and every consumer calls them.
     *
     * BLENDED onto the floor, not clamped to it. A `maxOf` drew the identical mound
     * for every slice drive under the floor, which on a live mic is the whole quiet
     * end of the range (measured: no change at all between two room levels 5 dB
     * apart). See [Aurora.restBlend].
     */
    fun lobeHeight(drive: Float, restLevel: Float): Float =
        Aurora.restBlend(restFloor(restLevel), shaped(drive))

    // -------------------------------------------------------------- lobe layouts

    /**
     * The full 5-lobe layout. Symmetric in POSITION about the centre but never in
     * DRIVE: a BROAD central mound on the body slice (0), a mid pair on the two
     * formant slices (1, 2), and a NARROW outer pair on presence (3) and air (4).
     * Every lobe owns a different slice, which is the fix for the mirrored outer pair
     * that used to share band 3 and therefore always agreed.
     */
    val FULL_LOBES: List<Lobe> = listOf(
        Lobe(xFrac = 0.12f, slice = 3, sigmaFrac = 0.05f, ampScale = 0.55f),
        Lobe(xFrac = 0.31f, slice = 1, sigmaFrac = 0.08f, ampScale = 0.80f),
        Lobe(xFrac = 0.50f, slice = 0, sigmaFrac = 0.13f, ampScale = 1.00f),
        Lobe(xFrac = 0.69f, slice = 2, sigmaFrac = 0.08f, ampScale = 0.80f),
        Lobe(xFrac = 0.88f, slice = 4, sigmaFrac = 0.05f, ampScale = 0.55f),
    )

    /**
     * The 3-lobe small-height layout (the 40 dp player bar). A broad central mound
     * (body) flanked by two wider mounds on the first-formant and presence slices —
     * fewer, softer bumps so the strip reads as a gentle blob rather than a shrunken
     * 5-lobe copy that would alias at that height, and still three DIFFERENT slices
     * so the bar keeps spectral movement. Motes are additionally suppressed at this
     * size (see the renderer).
     */
    val SMALL_LOBES: List<Lobe> = listOf(
        Lobe(xFrac = 0.22f, slice = 1, sigmaFrac = 0.10f, ampScale = 0.70f),
        Lobe(xFrac = 0.50f, slice = 0, sigmaFrac = 0.16f, ampScale = 1.00f),
        Lobe(xFrac = 0.78f, slice = 3, sigmaFrac = 0.10f, ampScale = 0.70f),
    )

    /**
     * How far, as a fraction of the width, each speaker's whole mound set is shifted
     * off the centre axis when BOTH voices are present — the human LEFT (−), the AI
     * RIGHT (+). Small: enough that the two silhouettes interleave and their glows
     * add where they overlap, not so much that either set slides off its half. One
     * voice alone is not shifted (0), so the single-voice look is dead-centred.
     */
    const val INTERLEAVE_FRAC = 0.06f

    /** The x-fraction of the tallest (central) lobe — where the core glow concentrates. */
    fun centreLobeXFrac(lobes: List<Lobe>): Float {
        var best = 0.5f
        var bestAmp = -1f
        for (l in lobes) if (l.ampScale > bestAmp) {
            bestAmp = l.ampScale
            best = l.xFrac
        }
        return best
    }

    // ----------------------------------------------------------- small-height / rails

    /**
     * Below this height (dp) the small-height degrade engages (3 lobes, no motes).
     * 48 dp so the 40 dp player bar degrades while the 52 dp composer strip and the
     * 140 dp voice screen keep the full 5-lobe language.
     */
    const val SMALL_HEIGHT_DP = 48f

    /** Keep the mounds clear of the very top/bottom edge, as a fraction of the height. */
    const val INSET_FRACTION = 0.06f

    /** Floor for the rail inset so it survives a tiny canvas (dp). */
    const val INSET_MIN_DP = 1.0f

    fun isSmallHeight(heightPx: Float, smallHeightPx: Float): Boolean = heightPx < smallHeightPx

    fun railInsetPx(heightPx: Float, minPx: Float): Float =
        maxOf(minPx, heightPx * INSET_FRACTION)

    /** The half-height a mound may grow into, once the rail inset is removed. Never negative. */
    fun availableHalfPx(heightPx: Float, insetPx: Float): Float =
        (heightPx / 2f - insetPx).coerceAtLeast(0f)

    // ---------------------------------------------------------------- shaping

    /**
     * Where a PRESENT-but-silent voice's mounds rest, 0..1 — the caller's restLevel,
     * NaN-guarded. The player bar passes 0 (file silence goes truly flat); a live mic
     * passes a small baseline so its blob keeps a soft resting presence.
     */
    fun restFloor(restLevel: Float): Float =
        if (restLevel.isNaN()) 0f else restLevel.coerceIn(0f, 1f)

    /**
     * THE perceptual curve on a 0..1 drive — [Aurora.displayCurve], called rather than
     * restated, so this style can never become a second copy of it. Guards NaN
     * EXPLICITLY: `coerceIn` passes NaN straight through (every comparison is false,
     * JLS 15.20.1) and a NaN would poison the whole silhouette.
     */
    fun shaped(level: Float): Float {
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        return Aurora.displayCurve(l)
    }

    // ------------------------------------------------------------- the silhouette

    /** Path samples across the width. Enough for a smooth curve, clamped for tiny/huge surfaces. */
    fun sampleSteps(widthPx: Float): Int = (widthPx / 6f).toInt().coerceIn(24, 96)

    /**
     * A single gaussian bump: 1 at the centre, falling smoothly to 0 as |dx| grows,
     * with half-spread [sigmaFrac]. A degenerate sigma yields a spike at dx==0 only.
     * NaN-guarded on dx.
     */
    fun gaussian(dxFrac: Float, sigmaFrac: Float): Float {
        if (dxFrac.isNaN()) return 0f
        if (sigmaFrac <= 0f) return if (dxFrac == 0f) 1f else 0f
        val z = dxFrac / sigmaFrac
        return exp(-0.5f * z * z)
    }

    /**
     * The RAW mound height at width-fraction [xFrac]: the sum over all lobes of
     * (its peak · its slice height · its gaussian at this x), with the whole set
     * shifted by [shiftFrac] (the two-voice interleave). ≥ 0; may exceed 1 where
     * lobes pile up — the caller saturates it, so this stays a truthful sum.
     */
    fun moundRaw(xFrac: Float, lobes: List<Lobe>, heights: FloatArray, shiftFrac: Float): Float {
        var sum = 0f
        for (l in lobes) {
            val hgt = heights[l.slice.coerceIn(0, heights.size - 1)]
            // NaN guard is explicit: `hgt <= 0f` is false for NaN (JLS 15.20.1), so
            // a NaN height would otherwise multiply straight into the sum and poison
            // the whole silhouette. Drives are already sanitised upstream; this is
            // belt-and-braces so the pure function is total on any input.
            if (hgt.isNaN() || hgt <= 0f) continue
            sum += l.ampScale * hgt * gaussian(xFrac - (l.xFrac + shiftFrac), l.sigmaFrac)
        }
        return sum
    }

    /**
     * Fraction of the available half a full, single dominant lobe should reach. The
     * saturating curve maps raw≈1 (one lobe at full drive) to ~0.8 of this ceiling.
     */
    const val FILL_GAIN = 4.0f

    /**
     * Ceiling for the silhouette as a fraction of the available half, BEFORE
     * breathing. Chosen with the breathing headroom so
     * FILL_CEIL·(1 + BREATHE_AMP) < 1 — the outline can never touch the rail no
     * matter how many gaussians stack (the no-mesa guarantee).
     */
    const val FILL_CEIL = 0.82f

    /**
     * Smooth, bounded saturation on [0, ∞) → [0, 1): monotone, no flat top. Used so
     * an unbounded sum of gaussians is squashed into the fill ceiling without ever
     * producing a mesa (a hard `min` would). Cheap — one divide, no `exp`.
     */
    fun saturate(x: Float): Float {
        val v = if (x.isNaN() || x < 0f) 0f else x
        return v / (1f + v)
    }

    /**
     * The mirrored half-extent of the silhouette in px at one x: the saturated,
     * gain-lifted raw sum against the fill ceiling and the breathing factor.
     * GUARANTEED strictly less than [availableHalfPx] (saturate < 1, and
     * FILL_CEIL·maxBreathe < 1), which is the no-clip/no-mesa budget, pinned by test.
     */
    fun silhouettePx(raw: Float, availableHalfPx: Float, breathe: Float): Float {
        val b = breathe.coerceIn(0f, 1f + BREATHE_AMP)
        return availableHalfPx * FILL_CEIL * saturate(raw * FILL_GAIN) * b
    }

    // ---------------------------------------------------------------- breathing

    /** Peak breathing swing as a fraction of the silhouette, at full energy. Subtle. */
    const val BREATHE_AMP = 0.05f

    /** Breathing angular rate (radians/second). Slow — a breath, not a flutter. */
    const val BREATHE_RATE = 0.9f

    /**
     * The breathing multiplier at wall-clock [phaseSec], scaled by live [level] so a
     * SILENT mound does not breathe (stays static → the loop can park). Returns
     * ~1 when silent and 1 ± (BREATHE_AMP·level) when speaking. Always in
     * [1 − BREATHE_AMP, 1 + BREATHE_AMP], so the silhouette budget holds.
     */
    fun breatheFactor(phaseSec: Float, level: Float): Float {
        val lv = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        val p = if (phaseSec.isNaN()) 0f else phaseSec
        return 1f + BREATHE_AMP * lv * sin(p * BREATHE_RATE)
    }

    // ------------------------------------------------------------ occlusion (white)

    /** A quiet mound's misty fill is faint. */
    const val WHITE_ALPHA_MIN = 0.10f

    /**
     * A loud mound's misty fill is denser. Held so the two-voice overlap
     * (both white fills stacking) occludes 1 − (1 − a)² = 0.42 < the ≤55% ceiling.
     */
    const val WHITE_ALPHA_MAX = 0.24f

    /** The occlusion ceiling (fraction) the stacked SrcOver whites must stay under. */
    const val OCCLUSION_CEILING = 0.55f

    /** The misty white fill alpha for a given level, MIN..MAX on the shaped level. */
    fun whiteAlpha(level: Float): Float =
        WHITE_ALPHA_MIN + (WHITE_ALPHA_MAX - WHITE_ALPHA_MIN) * shaped(level)

    /** Worst-case stacked occlusion of N white fills each at [WHITE_ALPHA_MAX]. */
    fun stackedOcclusion(passes: Int): Float {
        var transmit = 1f
        repeat(passes.coerceAtLeast(0)) { transmit *= (1f - WHITE_ALPHA_MAX) }
        return 1f - transmit
    }

    // -------------------------------------------------------- white-out (additive)

    /** A quiet mound's colour glow wash is faint. */
    const val GLOW_ALPHA_MIN = 0.06f

    /** A loud mound's colour glow wash. Additive; budgeted with the core below. */
    const val GLOW_ALPHA_MAX = 0.30f

    /** A quiet mound's core is faint. */
    const val CORE_ALPHA_MIN = 0.05f

    /** A loud mound's concentrated core. See [WHITE_OUT_CEILING] for the budget. */
    const val CORE_ALPHA_MAX = 0.36f

    /** A mote's peak additive alpha. Small — motes are sparkle, not mass. */
    const val MOTE_ALPHA_MAX = 0.22f

    /** Aurora's additive white-out ceiling; the summed additive brightness stays under it. */
    const val WHITE_OUT_CEILING = 1.6f

    fun glowAlpha(level: Float): Float =
        GLOW_ALPHA_MIN + (GLOW_ALPHA_MAX - GLOW_ALPHA_MIN) * shaped(level)

    fun coreAlpha(level: Float): Float =
        CORE_ALPHA_MIN + (CORE_ALPHA_MAX - CORE_ALPHA_MIN) * shaped(level)

    /**
     * Worst-case additive sum at one pixel with [voiceCount] speakers fully
     * overlapping (each contributing glow + core at max) plus one mote at max. The
     * value the [WHITE_OUT_CEILING] budget is checked against.
     */
    fun worstAdditiveSum(voiceCount: Int): Float =
        voiceCount.coerceAtLeast(0) * (GLOW_ALPHA_MAX + CORE_ALPHA_MAX) + MOTE_ALPHA_MAX

    // ------------------------------------------------------------------- the core

    /** Core radial radius as a fraction of the width — a soft lit heart at the centre lobe. */
    const val CORE_RADIUS_FRAC = 0.18f

    /** Cap on the core radius as a multiple of the available half, so it never overruns a short bar. */
    const val CORE_RADIUS_HALF_CAP = 1.4f

    // ------------------------------------------------------------------- motes

    /** How many motes drift around a speaker's silhouette (off at small height). */
    const val MOTE_COUNT = 8

    /** Horizontal drift spread as a fraction of the width (keeps motes inside the box). */
    const val MOTE_SPREAD_X = 0.42f

    /** Vertical drift spread as a fraction of the available half. < 1 so motes stay in range. */
    const val MOTE_SPREAD_Y = 0.9f

    /** Mote base radius as a fraction of the surface's smaller dimension. */
    const val MOTE_RADIUS_MIN_FRAC = 0.006f

    /** Extra mote radius at full level, as a fraction of the smaller dimension. */
    const val MOTE_RADIUS_LEVEL_FRAC = 0.010f

    /** Below this level motes are invisible (they fade out with the voice → idle-park honest). */
    const val MOTE_LEVEL_GATE = 0.02f

    /** Golden-angle-ish per-mote phase spacing (radians) so motes never clump or beat. */
    private const val MOTE_PHASE_STEP = 2.399963f

    /** Horizontal drift angular rate (radians/second). Slow. */
    private const val MOTE_RATE_X = 0.37f

    /** Vertical drift angular rate (radians/second), incommensurate with X for lissajous paths. */
    private const val MOTE_RATE_Y = 0.53f

    /** Twinkle angular rate (radians/second) — a gentle per-mote brightness shimmer. */
    private const val MOTE_TWINKLE_RATE = 1.7f

    private fun safePhase(phaseSec: Float): Float = if (phaseSec.isNaN()) 0f else phaseSec

    /**
     * A mote's x position: a slow horizontal drift about the centre, deterministic
     * in ([index], wall-clock [phaseSec]) so it is refresh-rate independent, offset
     * per speaker so red and blue motes do not coincide. Always within
     * [ (0.5 − SPREAD)·w , (0.5 + SPREAD)·w ] → inside the box.
     */
    fun moteX(index: Int, phaseSec: Float, widthPx: Float, speakerOrdinal: Int): Float {
        val ph = safePhase(phaseSec) * MOTE_RATE_X + index * MOTE_PHASE_STEP + speakerOrdinal * 1.13f
        return widthPx * (0.5f + MOTE_SPREAD_X * sin(ph))
    }

    /**
     * A mote's y position: a slow vertical drift about the centre line, within
     * [ centre − SPREAD·availHalf , centre + SPREAD·availHalf ] → inside the box.
     */
    fun moteY(index: Int, phaseSec: Float, centrePx: Float, availableHalfPx: Float, speakerOrdinal: Int): Float {
        val ph = safePhase(phaseSec) * MOTE_RATE_Y + index * (MOTE_PHASE_STEP * 0.5f) + speakerOrdinal * 0.71f
        return centrePx + availableHalfPx * MOTE_SPREAD_Y * sin(ph)
    }

    /** A mote's radius in px: a base plus a level-driven swell, both as fractions of the smaller dim. */
    fun moteRadiusPx(index: Int, level: Float, minDimPx: Float): Float {
        val lv = shaped(level)
        return minDimPx * (MOTE_RADIUS_MIN_FRAC + MOTE_RADIUS_LEVEL_FRAC * lv)
    }

    /** Per-mote twinkle multiplier in [MOTE_TWINKLE_FLOOR, 1] — a gentle brightness shimmer. */
    const val MOTE_TWINKLE_FLOOR = 0.55f

    fun moteTwinkle(index: Int, phaseSec: Float): Float {
        val ph = safePhase(phaseSec) * MOTE_TWINKLE_RATE + index * MOTE_PHASE_STEP
        // sin → [-1,1] mapped into [FLOOR, 1].
        val unit = 0.5f * (1f + sin(ph)) // [0,1]
        return MOTE_TWINKLE_FLOOR + (1f - MOTE_TWINKLE_FLOOR) * unit
    }

    /** The mote group's base alpha for a given level: faded out below the gate, up to the max. */
    fun moteAlpha(level: Float): Float {
        val lv = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        if (lv < MOTE_LEVEL_GATE) return 0f
        return MOTE_ALPHA_MAX * shaped(lv)
    }
}
