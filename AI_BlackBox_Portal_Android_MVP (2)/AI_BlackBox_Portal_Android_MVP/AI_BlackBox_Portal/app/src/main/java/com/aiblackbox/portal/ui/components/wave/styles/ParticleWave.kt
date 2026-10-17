package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
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
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.PI
import kotlin.math.floor
import kotlin.math.ln
import kotlin.math.sin
import kotlin.math.sqrt

// =============================================================================
// Layered Particle Wave — reference sheet concept #7 (tagged BOTH).
//
// Wave SURFACES made of DOTS. Instead of stroking a ribbon, each voice is drawn
// as a stack of DEPTH LAYERS — 3 at full height, 2 on the 40 dp player bar — and
// every layer is a smooth travelling sine SURFACE whose shape is sampled by a
// scattered field of dots that sit ON the curve. As a layer's crest travels the
// dots bob up and down, so the surface shimmers across the width the way the
// reference sheet's dotted wave does, and the dots — not a line — ARE the wave.
//
// AERIAL PERSPECTIVE is the organising idea of the depth stack: the FRONT layer
// is the nearest and reads that way — bigger, brighter, denser dots, its crest
// travelling fastest and highest; each layer behind it is dimmer, smaller,
// sparser and slower, so the stack reads as receding into the black. The crest
// speeds are drawn from Aurora v33's device-calibrated "alive but not scrolling"
// band (0.25–0.37 widths/sec — see [ParticleWave.crestSpeedWidthsPerSec]); this
// style reuses that CALIBRATION, but assigns the FRONT layer the fastest speed
// (near = most parallax), the reverse of Aurora's own per-sheet shear, because
// the look here is depth-into-the-page rather than Aurora's woven ribbons.
//
// AMPLITUDE IS PER LAYER, FROM A SLICE OF THE SPECTRUM. Each depth owns a
// frequency REGION in Hz ([ParticleWave.Slice]) rather than one analyser band,
// and the three regions TILE the spectrum map: front = body, mid = the formants,
// back = presence and air. That tiling is the fix for the first cut's defect —
// it indexed the layers straight into the band array as {0, 1, 3} and therefore
// threw band 2 (1.6–2.6 kHz, the most intelligibility-carrying part of speech)
// on the floor, where it moved nothing at all. Now no part of the audible range
// drives nothing, and the mid surface is exactly the one that answers to it.
//
// A SLICE IS RESOLVED FROM WHICHEVER CHANNEL IS LIVE, and the two are not
// interchangeable, which is why the code branches instead of averaging:
//   - SPECTRUM PRESENT (the [AuroraAnalyser.BINS] log-frequency channel): the
//     bins are RAW — amplitude-calibrated against a full-scale sine, with none of
//     the band channel's measured auto-gain, headroom or knee behind them. So
//     they are read for SHAPE only, as a ratio: each slice takes the loudest bin
//     it owns, the set is normalised by the loudest slice, and the whole thing is
//     scaled by the speaker's calibrated overall level. Ratios cancel the
//     calibration this channel does not have, so the style cannot invent one.
//   - NO SPECTRUM (every shipped call site today — the bins are produced but not
//     yet wired to a visualizer): the slice reads the four band drives at its
//     centre frequency, log-interpolated between the two bands whose Goertzel
//     centres bracket it. Three distinct MIXTURES of four bands are three
//     distinct numbers, and every band contributes to at least one of them.
//
// NO PRIVATE SMOOTHING STAGE — the same decision Soft Blob records in its own
// header, applied here for the same measured reason. This style used to carry
// per-slice attack/release envelopes across frames, which put a THIRD lag in
// series with the analyser's auto-gain release and Aurora's release ladder. Three
// stacked smoothers do not merely delay motion, they DELETE range: measured over
// a whole utterance this envelope alone cost 9% of the style's drawn range and
// stretched its 90%-fall to 460 ms — two syllable periods, against the 219 ms of
// the chain without it. A surface that cannot get back down between one syllable
// and the next draws their envelope instead of them, which is precisely the
// operator's "movement isn't fluent and that much for how much audio is coming
// in" (2026-08-11, v35 on a Fold).
// A slice's amplitude is now a pure function of THIS FRAME's drives, so the dot
// field is as quick as the audio channel underneath it and is refresh-rate
// independent BY CONSTRUCTION rather than by an exponential that composes over
// dt. Everything the envelope also did for the idle park still happens without
// it: the wave motion and the twinkle both scale by live level, so a silent
// voice's field goes steady on its own and the loop parks on a faint static
// picture. A DEPARTED voice now stops being drawn on the frame it leaves rather
// than releasing over ~0.5 s, which is what every instantaneous style does.
//
// THE ONE PERCEPTUAL CURVE. Drives reach geometry through [Aurora.displayCurve]
// and nothing else, and they are lifted onto the rest floor by the one shared
// [Aurora.restBlend]. The app has exactly one of each and they live in Aurora;
// this style calls them rather than restating a square root or a `maxOf`, so a
// re-tune there moves this style with it (see the note on [Aurora.DISPLAY_GAMMA]
// — the duplicate curves it lists were deleted precisely so no consumer keeps a
// private copy).
//
// THE TWO-VOICE CONTRACT (plan's table: "two particle sheets, red/blue,
// depth-offset"):
//   - one voice   → its own depth stack about the centre line. HUMAN dots are the
//     red family (front red, mid white, back grey), the AI the blue family (front
//     blue, mid white, back grey). Colour naming by DEPTH, identity by the front
//     hue AND — the load-bearing separator, straight from Aurora C1 — by DIRECTION
//     OF TRAVEL: the human's surfaces crest left→right, the AI's right→left.
//   - both voices → the two stacks are DEPTH-INTERLEAVED: the draw order walks
//     back→front and, within each depth, paints the human then the AI, so the two
//     voices' layers alternate in the stack rather than one sitting wholly in
//     front of the other. Opposite travel + a π phase bias on the AI keep the two
//     dot fields from ever coinciding.
//
// VISUAL BUDGET (inherited from Aurora v33 — a REQUIREMENT, not a suggestion):
//   - OCCLUSION is ZERO. Every dot is additive light (BlendMode.Plus); there is
//     NOT ONE SrcOver pass, so the particle field behind is never occluded at all
//     — trivially under the ≤55% ceiling ([ParticleWave.OCCLUSION_COVERAGE] = 0).
//   - WHITE-OUT (additive) is bounded exactly as Aurora bounds its crossing
//     sheets. A dot draws a soft glow + a bright core, both Plus; the per-layer
//     peaks recede with depth so ONE voice's whole depth stack coinciding at a
//     pixel sums to [ParticleWave.worstVoiceStackAdditive] = 1.55 < Aurora's 1.6
//     ceiling, and the single brightest dot (front core+glow) is 0.66 < 1 — no
//     dot ever alone blows to white. (Two voices fully coinciding would need six
//     dots on one pixel, which the per-layer dot spacing makes effectively
//     impossible; the operative budget is one voice's stack, as Aurora's own
//     white-out test bounds one voice's four sheets, not two.)
//   - THE DOT COUNT IS HARD-CAPPED and documented, like ParticleTuning caps the
//     field: [ParticleWave.FULL_DOTS]/[ParticleWave.SMALL_DOTS] are fixed per
//     layer (front densest, back sparsest for perspective), and the total over
//     every layer and both voices is pinned under [ParticleWave.HARD_CAP_DOTS] so
//     the style stays cheap at 120 Hz however the depth stack is configured.
//   - NO MESA at 40 dp. A dot's furthest reach from the centre —
//     amplitude + vertical scatter + its own radius — is budgeted to stay strictly
//     inside the available half at EVERY layer and height ([ParticleWave.maxDotExtentFrac]
//     < 1), and the 40 dp bar degrades to 2 layers with fewer, smaller dots so it
//     reads as a shallow dotted wave rather than a clipped copy of the tall stack.
//   - NaN-safe: every band read comes through the feed (already sanitised) and the
//     pure maths guards NaN explicitly wherever `coerceIn`/`sqrt` would pass it
//     through (JLS 15.20.1).
//   - dt-INDEPENDENT: with the private envelope gone there is no state that could
//     depend on the frame rate at all — an amplitude is a pure function of this
//     frame's drives, and every dot's position, scatter and twinkle is a pure
//     function of wall-clock [WaveformFrame.phaseSec], so 60 Hz and 120 Hz render
//     the identical motion by construction rather than by an exponential.
//
// The maths lives in [ParticleWave] as pure functions so the geometry, the depth
// ladder, the budgets and the degrade are JVM-testable without a device; this
// renderer is only a traversal of them. See ParticleWaveTest.
// =============================================================================

/**
 * Build a Layered Particle Wave renderer. THE public surface the registry keeps
 * stable: WaveformStyles registers this style by calling [particleWaveRenderer],
 * so this function's name and shape must not change even when the body does.
 */
internal fun particleWaveRenderer(): WaveformRenderer = ParticleWaveRenderer()

private class ParticleWaveRenderer : WaveformRenderer {

    /** The two speakers, in a fixed order so HUMAN draws before AI within a depth. */
    private val speakers = AuroraSpeaker.entries

    /**
     * Per-speaker scratch, owned so the draw path allocates NOTHING per frame: the
     * four band drives, the [AuroraAnalyser.BINS] spectrum (only filled when one is
     * published), this frame's per-SLICE drives, and the present/level pair.
     *
     * [drive] is indexed by SLICE, not by layer — a layer looks its amplitude up by
     * its slice ([ParticleWave.layerSlice]) — so the 3-layer and 2-layer stacks read
     * one array and a height change (3↔2 layers) cannot orphan an entry. It is pure
     * scratch: every element is rewritten from this frame's feed before it is read,
     * and NOTHING in this renderer survives a frame. That is the point of deleting
     * the envelope (see the header) and it is what makes the style stateless.
     */
    private val bandScratch = Array(AuroraSpeaker.entries.size) { FloatArray(AuroraAnalyser.BANDS) }
    private val binScratch = Array(AuroraSpeaker.entries.size) { FloatArray(AuroraAnalyser.BINS) }
    private val drive = Array(AuroraSpeaker.entries.size) { FloatArray(ParticleWave.SLICES.size) }
    private val present = BooleanArray(AuroraSpeaker.entries.size)
    private val liveLevel = FloatArray(AuroraSpeaker.entries.size)

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        val oneDp = 1.dp.toPx()
        val small = ParticleWave.isSmallHeight(h, ParticleWave.SMALL_HEIGHT_DP * oneDp)
        val layers = ParticleWave.layerCount(small)
        val inset = ParticleWave.railInsetPx(h, ParticleWave.INSET_MIN_DP * oneDp)
        val availHalf = ParticleWave.availableHalfPx(h, inset)
        val centre = h / 2f

        // --- this frame's per-slice drives ------------------------------------
        // Read, curved and floored in one pass, with no memory of the last frame. A
        // quiet-but-PRESENT voice is lifted ONTO restLevel by [Aurora.restBlend] so
        // its surfaces keep a soft resting line and still move with the audio above
        // it (a live mic breathes; the player bar rests at 0, where the blend is the
        // identity and the file's silence goes truly flat).
        for (si in speakers.indices) {
            val sp = speakers[si]
            val isPresent = frame.feed.present(sp)
            present[si] = isPresent
            liveLevel[si] = if (isPresent) frame.feed.level(sp) else 0f
            val row = drive[si]

            if (isPresent) {
                val bands = bandScratch[si]
                for (b in bands.indices) bands[b] = frame.feed.band(sp, b)
                val bins = if (frame.feed.hasSpectrum(sp)) {
                    val spectrum = binScratch[si]
                    for (k in spectrum.indices) spectrum[k] = frame.feed.bin(sp, k)
                    spectrum
                } else {
                    null
                }
                ParticleWave.sliceDrives(bands, bins, liveLevel[si], row)
                val floorV = ParticleWave.restFloor(frame.restLevel)
                for (i in row.indices) row[i] = Aurora.restBlend(floorV, ParticleWave.shaped(row[i]))
            } else {
                row.fill(0f)
            }
        }

        if (availHalf > 0f) {
            // Interleave by DEPTH, not by voice: walk back→front, and within each
            // depth paint the human then the AI, so the two voices' layers alternate
            // in the stack (the plan's "depth-offset") rather than one voice sitting
            // wholly in front of the other.
            for (layer in (layers - 1) downTo 0) {
                for (si in speakers.indices) {
                    if (!present[si]) continue
                    drawLayer(
                        w = w,
                        centre = centre,
                        availHalf = availHalf,
                        oneDp = oneDp,
                        layer = layer,
                        layerCount = layers,
                        speaker = speakers[si],
                        sliceDrive = drive[si][ParticleWave.layerSlice(layer, layers)],
                        level = liveLevel[si],
                        phaseSec = frame.phaseSec,
                    )
                }
            }
        }
    }

    /**
     * Paint ONE depth layer of ONE speaker: a scattered field of dots sitting on
     * that layer's travelling surface. Each dot is a soft additive glow plus a
     * brighter additive core — light, never paint, so nothing occludes the field.
     */
    private fun DrawScope.drawLayer(
        w: Float,
        centre: Float,
        availHalf: Float,
        oneDp: Float,
        layer: Int,
        layerCount: Int,
        speaker: AuroraSpeaker,
        sliceDrive: Float,
        level: Float,
        phaseSec: Float,
    ) {
        val depthT = ParticleWave.depthT(layer, layerCount)
        val count = ParticleWave.dotsForLayer(layer, layerCount <= ParticleWave.SMALL_LAYERS)
        if (count <= 0) return

        val amp = availHalf * ParticleWave.ampFrac(depthT) * sliceDrive
        val travel = ParticleWave.travelDir(speaker)
        val bias = ParticleWave.phaseBias(speaker)
        val colour = layerColour(ParticleWave.layerTone(layer, layerCount), speaker)

        val radius = ParticleWave.dotRadiusPx(ParticleWave.radiusDp(depthT) * oneDp, availHalf)
        val glowRadius = radius * ParticleWave.GLOW_RADIUS_MULT
        val coreA = ParticleWave.coreAlpha(depthT, level)
        val glowA = ParticleWave.glowAlpha(depthT, level)
        val ord = speaker.ordinal

        for (d in 0 until count) {
            val xFrac = ParticleWave.dotBaseXFrac(d, count, layer, ord)
            val x = xFrac * w
            val wu = ParticleWave.waveUnit(xFrac, phaseSec, depthT, travel, bias)
            val scatter = availHalf * ParticleWave.SCATTER_FRAC * ParticleWave.dotScatterUnit(d, layer, ord)
            // Screen y decreases upward; a positive surface offset lifts the dot.
            val y = centre - (amp * wu + scatter)
            val tw = ParticleWave.twinkle(d, phaseSec, level)

            drawCircle(colour, radius = glowRadius, center = Offset(x, y), alpha = glowA * tw, blendMode = BlendMode.Plus)
            drawCircle(colour, radius = radius, center = Offset(x, y), alpha = coreA * tw, blendMode = BlendMode.Plus)
        }
    }

    /** Depth tone → the concrete brand Color, keyed by the speaker's identity hue. */
    private fun layerColour(tone: ParticleWave.Tone, speaker: AuroraSpeaker): Color = when (tone) {
        ParticleWave.Tone.IDENTITY -> if (speaker == AuroraSpeaker.HUMAN) BbxAccent else WaveBlue
        ParticleWave.Tone.WHITE -> BbxWhite
        ParticleWave.Tone.GREY -> Neutral700
    }

    // NO needsFrames OVERRIDE. The inherited [WaveformRenderer.needsFrames] — keep
    // frames while any PASSED voice is audible — is exactly right for a style with
    // no state: with the private envelope deleted there is no release tail left to
    // drain, so there is nothing a frame after the last audible one could reveal.
    // The override that used to be here returned `settling`, which existed solely to
    // finish that tail. Soft Blob, the other stateless style, inherits the default
    // for the same reason.
}

// =============================================================================
// Geometry + depth ladder + budgets — pure, no Compose draw calls, JVM-testable.
// =============================================================================

internal object ParticleWave {

    /** How the three depth tones read; kept an enum so the choice is JVM-testable without a Color. */
    enum class Tone { IDENTITY, WHITE, GREY }

    private val TWO_PI = (2.0 * PI).toFloat()

    // ---------------------------------------------------------- depth stack size

    /** Full-height depth layers — near, mid, far. Three reads as a receding stack. */
    const val FULL_LAYERS = 3

    /** 40 dp player-bar layers — a near + a far only, so the shallow bar is not busy. */
    const val SMALL_LAYERS = 2

    /**
     * Below this height (dp) the small-height degrade engages (2 layers, fewer &
     * smaller dots). 48 dp so the 40 dp player bar degrades while the 52 dp composer
     * strip and the 140 dp voice screen keep the full 3-layer stack.
     */
    const val SMALL_HEIGHT_DP = 48f

    fun isSmallHeight(heightPx: Float, smallHeightPx: Float): Boolean = heightPx < smallHeightPx

    fun layerCount(small: Boolean): Int = if (small) SMALL_LAYERS else FULL_LAYERS

    /**
     * A layer's DEPTH as a 0..1 ratio: 0 is the front (nearest), 1 the back
     * (farthest). Everything that recedes with distance — amplitude, dot size,
     * brightness, crest speed — interpolates on this, so the SAME ladder serves the
     * 3-layer and 2-layer stacks (a 2-layer stack is just t = 0 and t = 1).
     */
    fun depthT(layer: Int, layerCount: Int): Float {
        if (layerCount <= 1) return 0f
        return (layer.coerceIn(0, layerCount - 1)).toFloat() / (layerCount - 1)
    }

    private fun lerp(front: Float, back: Float, t: Float): Float =
        front + (back - front) * t.coerceIn(0f, 1f)

    // -------------------------------------------------------------- rails

    /** Keep the surfaces clear of the very top/bottom edge, as a fraction of the height. */
    const val INSET_FRACTION = 0.06f

    /** Floor for the rail inset so it survives a tiny canvas (dp). */
    const val INSET_MIN_DP = 1.0f

    fun railInsetPx(heightPx: Float, minPx: Float): Float =
        maxOf(minPx, heightPx * INSET_FRACTION)

    /** The half-height a surface may grow into, once the rail inset is removed. Never negative. */
    fun availableHalfPx(heightPx: Float, insetPx: Float): Float =
        (heightPx / 2f - insetPx).coerceAtLeast(0f)

    // -------------------------------------------------- spectral slices per depth

    /**
     * A contiguous region of the audible range, in Hz, that ONE depth layer is driven
     * by. The frequency band is the unit of meaning here rather than a channel index,
     * because the two channels that can answer for it are different widths
     * ([AuroraAnalyser.BANDS] = 4, [AuroraAnalyser.BINS] = 24) and only a frequency
     * means the same thing to both.
     */
    class Slice(val loHz: Float, val hiHz: Float) {
        /** Geometric centre — the frequency this slice is "about" on a log axis. */
        val centreHz: Float = sqrt(loHz.toDouble() * hiHz).toFloat()
    }

    /**
     * The three slices, front → back, TILING the spectrum map end to end: body, the
     * formant region, then presence and air. Tiling is the property that matters —
     * it is what stopped band 2 driving nothing when the layers indexed bands {0,1,3}
     * directly. The MID slice deliberately spans both formant bands, since that whole
     * region is one perceptual object (intelligibility) and one surface should carry it.
     */
    val SLICES: List<Slice> = listOf(
        Slice(AuroraSpectrum.MIN_HZ, 600f),
        Slice(600f, 2400f),
        Slice(2400f, AuroraSpectrum.MAX_HZ),
    )

    /**
     * Which slice drives each layer's amplitude — FRONT = the body of the voice,
     * BACK = presence/air, the mid layer the formants between. So a booming vowel
     * lifts the near surface and a bright sibilant flickers the far one, exactly the
     * "front = low … back = high" the reference asks for.
     *
     * Two tables because the stacks are different heights. The 2-layer bar keeps the
     * outer pair (the widest spectral separation two surfaces can show); the formant
     * slice it drops still reaches the geometry, because the band channel's centres
     * are interpolated and the presence slice's mixture includes band 2.
     */
    private val LAYER_SLICES_FULL = intArrayOf(0, 1, 2)
    private val LAYER_SLICES_SMALL = intArrayOf(0, 2)

    fun layerSlice(layer: Int, layerCount: Int): Int {
        val table = if (layerCount <= SMALL_LAYERS) LAYER_SLICES_SMALL else LAYER_SLICES_FULL
        val s = table[layer.coerceIn(0, table.size - 1)]
        return s.coerceIn(0, SLICES.size - 1)
    }

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
     * about. This is what lets four bands answer for three distinct slices without any
     * of them being ignored.
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
     * depth stack, in one place.
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

    // -------------------------------------------------- the travelling surface

    /**
     * Full cycles of the surface across the width, per depth. The near surface has
     * MORE cycles (more detail, closer), the far FEWER (a broad distant swell) —
     * aerial perspective in the wave shape as well as the dots. Modest counts (a
     * couple of humps) so even the 40 dp bar reads as a wave, not a ripple.
     */
    private const val CYCLES_FRONT = 2.4f
    private const val CYCLES_BACK = 1.6f

    fun cycles(depthT: Float): Float = lerp(CYCLES_FRONT, CYCLES_BACK, depthT)

    /**
     * How fast a layer's crest travels along x, in WIDTHS PER SECOND — the observable
     * quantity, chosen exactly as Aurora v33 chose its own (hold the sine's argument
     * constant and differentiate: a crest advances [angularRate]/(2π·[cycles])
     * widths/sec). These sit inside Aurora's device-tuned "alive but not scrolling"
     * band of 0.25–0.37 widths/sec (Brandon, 2026-07-27: slower read as "no motion",
     * faster as a scroll). The FRONT layer is the fastest (near = most parallax) and
     * the back the slowest — the reverse of Aurora's per-sheet shear, because this
     * style's depth reads INTO the page rather than as Aurora's woven ribbons.
     */
    private const val CREST_SPEED_FRONT = 0.35f
    private const val CREST_SPEED_BACK = 0.25f

    fun crestSpeedWidthsPerSec(depthT: Float): Float = lerp(CREST_SPEED_FRONT, CREST_SPEED_BACK, depthT)

    /**
     * The angular rate (radians of phase per second) that makes a layer's crest
     * travel at [crestSpeedWidthsPerSec]. Derived so the SPEED, not the raw rate, is
     * what the constants above pin: speed = rate / (2π·cycles) ⇒ rate = speed·2π·cycles.
     */
    fun angularRate(depthT: Float): Float = crestSpeedWidthsPerSec(depthT) * TWO_PI * cycles(depthT)

    /**
     * How far each end taper occupies, as a fraction of the width — the surface eases
     * to the centre line at both ends so the dot field fades in/out rather than being
     * chopped at the edges (Aurora's [edgeTaper], expressed here for a full-width surface).
     */
    const val TAPER_FRACTION = 0.12f

    /** 1 through the middle, easing to 0 at both edges over [TAPER_FRACTION] of the width. */
    fun taper(xFrac: Float): Float {
        val p = xFrac.coerceIn(0f, 1f)
        val d = minOf(p, 1f - p)
        if (d >= TAPER_FRACTION) return 1f
        return 0.5f - 0.5f * kotlin.math.cos((PI * d / TAPER_FRACTION).toFloat())
    }

    /**
     * A per-speaker offset that keeps the two voices' surfaces from coinciding. Two
     * waves travelling opposite ways are identical whenever their phases meet, and
     * with no bias they meet at phase zero — the most-observed instant (first frame /
     * resume / wake). A π bias makes that instant the maximally separated one, at the
     * cost of nothing but a constant (Aurora's [phaseBias], same reasoning).
     */
    fun phaseBias(speaker: AuroraSpeaker): Float = when (speaker) {
        AuroraSpeaker.HUMAN -> 0f
        AuroraSpeaker.AI -> PI.toFloat()
    }

    /**
     * WHICH WAY the surface travels — the load-bearing two-voice separator (Aurora
     * C1): the human's crests run left→right (+1), the AI's right→left (−1), forever,
     * which needs no legend and only gets clearer the longer you watch.
     */
    fun travelDir(speaker: AuroraSpeaker): Float = when (speaker) {
        AuroraSpeaker.HUMAN -> 1f
        AuroraSpeaker.AI -> -1f
    }

    /**
     * The travelling surface, −1..1, at width-fraction [xFrac]: a tapered sine of
     * [cycles] humps whose crest slides along x at [angularRate] in the [travel]
     * direction. NaN-safe on the phase. This is the shape the dots sit on; the
     * caller multiplies it by the layer's px amplitude.
     */
    fun waveUnit(xFrac: Float, phaseSec: Float, depthT: Float, travel: Float, phaseBias: Float): Float {
        val ph = if (phaseSec.isNaN()) 0f else phaseSec
        val arg = xFrac * TWO_PI * cycles(depthT) - travel * ph * angularRate(depthT) + phaseBias
        return taper(xFrac) * sin(arg)
    }

    // ------------------------------------------------------ amplitude / scatter

    /**
     * A layer's crest amplitude as a fraction of the available half — the FRONT
     * surface is the tallest (nearest), the BACK the shallowest (distance). Times
     * the layer's slice envelope (0..1) in the renderer, so a region's loudness is how
     * high its surface rises. Budgeted with [SCATTER_FRAC] and the dot radius so the
     * furthest dot can never touch the rail (see [maxDotExtentFrac]).
     */
    private const val AMP_FRAC_FRONT = 0.68f
    private const val AMP_FRAC_BACK = 0.48f

    fun ampFrac(depthT: Float): Float = lerp(AMP_FRAC_FRONT, AMP_FRAC_BACK, depthT)

    /**
     * How far a dot is scattered off its surface, as a fraction of the available
     * half — a small STATIC per-dot offset (deterministic, does not animate) that
     * gives the dotted surface a little thickness instead of a hairline of dots on
     * an exact curve. Static so it demands no frames and the idle park stays honest.
     */
    const val SCATTER_FRAC = 0.10f

    // ---------------------------------------------------------------- dots

    /**
     * Dots per layer, FRONT→BACK. Front densest (~near, most detail), back sparsest
     * (aerial perspective, and it keeps the total cheap). Fixed, hard-capped counts
     * — the ParticleTuning discipline: the budget is a documented constant, never an
     * unbounded per-frame number. ~60–80 dots on the near layers, thinning behind.
     */
    val FULL_DOTS = intArrayOf(78, 60, 46)

    /** 40 dp bar: 2 layers, ~40 dots each (the plan's degrade target). */
    val SMALL_DOTS = intArrayOf(42, 34)

    /**
     * The documented hard ceiling on TOTAL dots drawn (every layer, BOTH voices), the
     * ParticleTuning-style cap that keeps the style cheap at 120 Hz no matter how the
     * stack is configured. The full stack sums to 2·(78+60+46) = 368; the cap leaves
     * headroom without ever being an unbounded budget. Pinned by a test.
     */
    const val HARD_CAP_DOTS = 400

    fun dotsForLayer(layer: Int, small: Boolean): Int {
        val table = if (small) SMALL_DOTS else FULL_DOTS
        return table[layer.coerceIn(0, table.size - 1)]
    }

    /** Total dots one voice draws across its whole stack (for the budget test). */
    fun dotsPerVoice(small: Boolean): Int {
        val table = if (small) SMALL_DOTS else FULL_DOTS
        var sum = 0
        for (n in table) sum += n
        return sum
    }

    /** Total dots BOTH voices draw — what [HARD_CAP_DOTS] bounds. */
    fun totalDots(small: Boolean): Int = dotsPerVoice(small) * AuroraSpeaker.entries.size

    /**
     * A dot's base position along the width (0..1): an even spacing across the layer
     * plus a small DETERMINISTIC jitter (a pure hash of the dot, its layer and its
     * speaker) so the field does not read as a rigid grid. Clamped into [0,1]. The
     * jitter is at most [DOT_JITTER_FRAC] of one slot, so dots stay in order and do
     * not pile up. Pure in its inputs → identical at 60 and 120 Hz.
     */
    const val DOT_JITTER_FRAC = 0.45f

    fun dotBaseXFrac(index: Int, count: Int, layer: Int, speakerOrdinal: Int): Float {
        val n = count.coerceAtLeast(1)
        val slot = 1f / n
        val base = (index + 0.5f) * slot
        val jitter = (hashUnit(index, layer, speakerOrdinal, JITTER_SALT) * 2f - 1f) * DOT_JITTER_FRAC * slot
        return (base + jitter).coerceIn(0f, 1f)
    }

    /**
     * A dot's STATIC vertical scatter, −1..1 (a pure hash of the dot). Multiplied by
     * [SCATTER_FRAC]·availableHalf in the renderer. Deterministic and time-independent,
     * so it is a fixed thickness of the dotted surface, not animated noise.
     */
    fun dotScatterUnit(index: Int, layer: Int, speakerOrdinal: Int): Float =
        hashUnit(index, layer, speakerOrdinal, SCATTER_SALT) * 2f - 1f

    private const val JITTER_SALT = 1
    private const val SCATTER_SALT = 2

    /**
     * A cheap deterministic hash → [0,1). The classic fract(sin·k) sprite hash: total
     * on any input (a NaN cannot arise — the inputs are ints), stable across builds,
     * and needs no allocation. Used for the dot jitter and scatter so the field is
     * reproducible frame to frame and testable on the JVM.
     */
    private fun hashUnit(index: Int, layer: Int, speakerOrdinal: Int, salt: Int): Float {
        val s = index * 12.9898 + layer * 78.233 + speakerOrdinal * 37.719 + salt * 4.137
        val v = sin(s) * 43758.5453
        return (v - floor(v)).toFloat()
    }

    // ---------------------------------------------------------------- dot size

    /**
     * Dot radius in dp by depth — FRONT ~2.5 dp (near), BACK ~1.5 dp (far), the
     * "~1.5–2.5 dp by depth" the reference gives. Converted to px and capped in the
     * renderer; the pure cap below keeps even the near dot inside a tiny bar.
     */
    private const val RADIUS_DP_FRONT = 2.5f
    private const val RADIUS_DP_BACK = 1.5f

    fun radiusDp(depthT: Float): Float = lerp(RADIUS_DP_FRONT, RADIUS_DP_BACK, depthT)

    /**
     * Cap on the core dot radius as a fraction of the available half, so on the 40 dp
     * bar (where a 2.5 dp radius would be a large share of a tiny half) the dot cannot
     * fatten past the rail. Part of the no-mesa budget ([maxDotExtentFrac]).
     */
    const val DOT_RADIUS_HALF_CAP = 0.12f

    /** How much wider the soft additive glow is than the bright core. */
    const val GLOW_RADIUS_MULT = 2.4f

    /** The drawn core radius: the dp-derived size, capped so it never overruns a short bar. */
    fun dotRadiusPx(dpRadiusPx: Float, availableHalfPx: Float): Float {
        val cap = availableHalfPx * DOT_RADIUS_HALF_CAP
        val r = if (dpRadiusPx.isNaN() || dpRadiusPx < 0f) 0f else dpRadiusPx
        return minOf(r, cap.coerceAtLeast(0f))
    }

    // ------------------------------------------------------- no-mesa budget

    /**
     * A dot's furthest reach from the centre line, as a fraction of the available
     * half, at full drive: its layer amplitude + the scatter + its own radius. The
     * binding case is the FRONT layer (tallest amp, biggest dot). GUARANTEED < 1 —
     * pinned by a test — so no dot at any layer, height or drive touches the rail.
     */
    fun maxDotExtentFrac(depthT: Float): Float =
        ampFrac(depthT) + SCATTER_FRAC + DOT_RADIUS_HALF_CAP

    // ------------------------------------------------------ depth colour tones

    /**
     * A layer's TONE by depth — FRONT is the speaker's identity hue (red / blue),
     * the BACK is grey, anything between is white. So the stack reads red→white→grey
     * (human) / blue→white→grey (AI) front-to-back, the reference's dot palette. Kept
     * as an enum (not a Color) so the depth→tone choice is JVM-testable.
     */
    fun layerTone(layer: Int, layerCount: Int): Tone {
        val last = (layerCount - 1).coerceAtLeast(0)
        return when {
            layer <= 0 -> Tone.IDENTITY
            layer >= last -> Tone.GREY
            else -> Tone.WHITE
        }
    }

    // ------------------------------------------------------------- twinkle

    /** How deeply a dot can dim at full level — subtle sparkle, not a strobe. */
    const val TWINKLE_DEPTH = 0.45f

    /** Twinkle angular rate (radians/second). Gentle. */
    private const val TWINKLE_RATE = 2.1f

    /** Golden-angle-ish per-dot phase so dots twinkle out of step (no visible beat). */
    private const val TWINKLE_PHASE_STEP = 2.399963f

    /**
     * A dot's twinkle multiplier on its alpha, in [1 − TWINKLE_DEPTH·level, 1]. It
     * only ever DIMS (never brightens past the base alpha), so the white-out budget
     * is bounded by the base alphas. Crucially it is scaled by live [level]: a SILENT
     * dot returns exactly 1 (steady), so a quiet field does not shimmer and the loop
     * can park — the same idle-park discipline Aurora established. Pure in
     * (index, wall-clock phase, level) → dt-independent.
     */
    fun twinkle(index: Int, phaseSec: Float, level: Float): Float {
        val lv = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        val ph = if (phaseSec.isNaN()) 0f else phaseSec
        // (1 − sin)/2 ∈ [0,1]: how far this dot is into its dim right now.
        val dim = 0.5f * (1f - sin(ph * TWINKLE_RATE + index * TWINKLE_PHASE_STEP))
        return 1f - TWINKLE_DEPTH * lv * dim
    }

    // ------------------------------------------------ additive alpha (white-out)

    // The depth alpha ladder — front brightest, back dimmest (aerial perspective).
    // Every value is an ADDITIVE (BlendMode.Plus) alpha; there is no SrcOver pass, so
    // OCCLUSION is zero. The peaks are held so one voice's whole stack coinciding at a
    // pixel sums under Aurora's 1.6 white-out ceiling (see [worstVoiceStackAdditive]).
    private const val CORE_MAX_FRONT = 0.50f
    private const val CORE_MAX_BACK = 0.28f
    private const val CORE_MIN_FRONT = 0.10f
    private const val CORE_MIN_BACK = 0.06f
    private const val GLOW_MAX_FRONT = 0.16f
    private const val GLOW_MAX_BACK = 0.09f
    private const val GLOW_MIN_FRONT = 0.03f
    private const val GLOW_MIN_BACK = 0.02f

    /** Aurora's additive white-out ceiling; one voice's coinciding stack stays under it. */
    const val WHITE_OUT_CEILING = 1.6f

    /** The SrcOver occlusion this style contributes: NONE — every pass is additive light. */
    const val OCCLUSION_COVERAGE = 0f

    /** The occlusion ceiling the field-survival budget must stay under (Aurora ≤55%). */
    const val OCCLUSION_CEILING = 0.55f

    private fun coreMax(depthT: Float): Float = lerp(CORE_MAX_FRONT, CORE_MAX_BACK, depthT)
    private fun coreMin(depthT: Float): Float = lerp(CORE_MIN_FRONT, CORE_MIN_BACK, depthT)
    private fun glowMax(depthT: Float): Float = lerp(GLOW_MAX_FRONT, GLOW_MAX_BACK, depthT)
    private fun glowMin(depthT: Float): Float = lerp(GLOW_MIN_FRONT, GLOW_MIN_BACK, depthT)

    /** The bright core's additive alpha at a depth and level: MIN..MAX on the shaped level. */
    fun coreAlpha(depthT: Float, level: Float): Float =
        coreMin(depthT) + (coreMax(depthT) - coreMin(depthT)) * shaped(level)

    /** The soft glow's additive alpha at a depth and level: MIN..MAX on the shaped level. */
    fun glowAlpha(depthT: Float, level: Float): Float =
        glowMin(depthT) + (glowMax(depthT) - glowMin(depthT)) * shaped(level)

    /** The single brightest dot's additive contribution (front core + glow, at full level). */
    fun brightestDotAdditive(): Float = coreMax(0f) + glowMax(0f)

    /**
     * The worst-case additive sum at one pixel with ONE voice's whole [layerCount]-deep
     * stack coinciding (one dot per layer, each at full level, core + glow). The value
     * the [WHITE_OUT_CEILING] budget is checked against — bounded like Aurora bounds
     * its four crossing sheets.
     */
    fun worstVoiceStackAdditive(layerCount: Int): Float {
        var sum = 0f
        for (layer in 0 until layerCount.coerceAtLeast(1)) {
            val t = depthT(layer, layerCount)
            sum += coreMax(t) + glowMax(t)
        }
        return sum
    }

    // ------------------------------------------------------------ shaping
    //
    // ATTACK_TAU_SEC, RELEASE_TAU_SEC, SETTLE_EPS, INK_EPS, approach() and
    // rowHasInk() ARE DELETED — the whole private envelope. Do not reintroduce one
    // here: this style is the THIRD smoother in a chain that already has the
    // analyser's auto-gain release and Aurora's per-band release ladder, and the
    // measured cost of that third stage was 9% of the style's drawn range and a
    // 90%-fall stretched from 219 ms to 460 ms. Soft Blob deleted the identical
    // stage for the identical reason; the header records both. If a surface ever
    // needs to be calmer, the lever is Aurora's shared ladder, where it applies to
    // every style at once and can be measured against the syllable period.

    /**
     * THE perceptual curve on a 0..1 drive — [Aurora.displayCurve], called rather than
     * restated, so this style can never become a second copy of it. Guards NaN
     * EXPLICITLY: `coerceIn` passes NaN straight through (JLS 15.20.1) and a NaN would
     * poison every surface reading it.
     */
    fun shaped(level: Float): Float {
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        return Aurora.displayCurve(l)
    }

    /**
     * Where a PRESENT-but-silent voice's surfaces rest, 0..1 — the caller's restLevel,
     * NaN-guarded. The player bar passes 0 (file silence goes truly flat); a live mic
     * passes a small baseline so its dot field keeps a soft resting line.
     *
     * The floor goes UNDER the drive as [Aurora.restBlend], never over it as a
     * `maxOf`: a clamp drew the identical surface for every drive beneath it, which
     * on a live mic is the whole quiet end of the range.
     */
    fun restFloor(restLevel: Float): Float =
        if (restLevel.isNaN()) 0f else restLevel.coerceIn(0f, 1f)
}
