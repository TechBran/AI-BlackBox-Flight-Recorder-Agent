package com.aiblackbox.portal.ui.components.wave.styles

import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser
import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.wave.WaveformFrame
import com.aiblackbox.portal.ui.components.wave.WaveformRenderer
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlin.math.PI
import kotlin.math.sin

// =============================================================================
// Classic — the pre-aurora "ribbon scope" (ui/voice/VoiceWaveform.kt), adapted
// as a draw module. It is the free-win tenth style: three layered translucent
// sine paths whose height tracks the speaker's level and whose phase flows
// continuously. Kept faithful to VoiceWaveform's LOOK (same three heightFraction/
// alpha/frequency/phase-offset triples, same 2.2 s phase period, same PI-taper
// ends), with three deliberate adaptations, each because the world around it
// changed:
//
//   A1 INPUT is the feed, not a single (amplitude, speaker). VoiceWaveform
//      predates AuroraVoice — every call site fed it one scalar amplitude and
//      one speaker, and the two-voice screen drew two separate VoiceWaveforms.
//      Here one renderer draws BOTH present speakers in one pass off the shared
//      feed's per-speaker level, which is what the two-voice contract requires
//      and what lets Classic sit behind the same VoiceVisualizer as every other
//      style. The per-speaker analyser gain VoiceWaveform applied by hand
//      (USER_GAIN/AI_GAIN) is gone: the v33 analyser already normalises each
//      speaker's dynamics to 0..1, which is exactly what those hand lifts were
//      compensating for (the same reason the live AuroraWaveform call sites
//      dropped their sensitivity knobs).
//
//   A2 COLOUR is red human / blue AI, the locked project convention, not
//      VoiceWaveform's original red-user / green-AI pair. That green predated
//      the red=human/blue=AI rule; every non-Aurora style now follows it, so
//      Classic does too. The old per-call-site overrideColors (the player bar
//      forced red) is unnecessary — identity is the speaker's, carried by the
//      colour, not the surface's.
//
//   A3 MOTION is time-based off WaveformFrame.phaseSec, not an
//      infiniteRepeatable/animateFloatAsState pair. VoiceVisualizer's shared loop
//      already gives every style a wall-clock phase and the idle park, so Classic
//      inherits 60-vs-120 Hz independence and battery discipline for free instead
//      of running its own animators.
//
//   A4 THE THREE RIBBONS ARE SPECTRAL. VoiceWaveform had one amplitude and drew
//      the same three curves scaled off it, so a booming vowel and a bright "s" of
//      equal loudness were the IDENTICAL picture. Here each ribbon owns a slice of
//      the spectrum — the slow tall one the body, the middle one presence, the fast
//      small one air — so the layers separate when the voice does. Loudness still
//      sets the overall size ([WaveformFeed.level]); the spectrum only decides how
//      that size is SHARED OUT (see [ClassicMath]). A flat spectrum therefore draws
//      exactly the pre-spectral ribbon, which is the case the test pins.
//
//   A5 THE LEVEL IS CURVED AND BLENDED, not raw and clamped. VoiceWaveform drew
//      whatever amplitude it was handed; Classic sends the feed's level through
//      the app's ONE perceptual curve (Aurora.displayCurve) and lifts it onto the
//      rest floor with the ONE floor rule (Aurora.restBlend). Reading raw was
//      measured as the reason so little of the ribbon moved on a real mic, and a
//      clamp made every level under the floor draw the same picture. Neither
//      function is restated here — the app has exactly one of each.
//
// A present-but-silent speaker rests at WaveformFrame.restLevel — 0f on the
// player bar draws a FLAT line across the bar (VoiceWaveform's idleLevel=0f
// behaviour, which the player bar depends on), Aurora.BASELINE on a live mic
// keeps it gently breathing. Height-relative throughout, so nothing mesas at the
// 40 dp player bar (the amplitude is a fraction of the box, exactly as the
// original's `size.height / 2f * heightFraction`).
// =============================================================================

/**
 * Build a Classic renderer. THE public surface a later maintainer keeps stable:
 * [com.aiblackbox.portal.ui.components.wave.WaveformStyles] registers Classic by
 * calling this, so the function name/shape must not change even if the body does.
 */
internal fun classicRenderer(): WaveformRenderer = ClassicRenderer()

private class ClassicRenderer : WaveformRenderer {

    /** One spectrum read per speaker per frame, into scratch the renderer owns — a draw allocates
     *  nothing. Safe to share between the two speakers: each is consumed before the next is read. */
    private val binScratch = FloatArray(AuroraAnalyser.BINS)

    override fun DrawScope.draw(frame: WaveformFrame) {
        val w = size.width
        val h = size.height
        if (w <= 0f || h <= 0f) return

        // VoiceWaveform's phase ran 0..2π over 2200 ms (its infiniteRepeatable).
        // Reproduce that angular speed off the shared seconds clock.
        val phase = frame.phaseSec * (2f * PI.toFloat() / PHASE_PERIOD_SEC)
        val stroke = Stroke(width = 4.dp.toPx())

        for (i in SPEAKERS.indices) {
            val speaker = SPEAKERS[i]
            if (!frame.feed.present(speaker)) continue

            // THE ONE PERCEPTUAL CURVE, then the ONE rest-floor rule: [Aurora.displayCurve]
            // followed by [Aurora.restBlend], both CALLED, never restated (A5). Classic used to
            // read the feed RAW, which is why so little of it moved — the live level's typical
            // frame is a few percent of full scale and a ribbon 3% of the box tall is not on
            // screen. Present-but-silent still settles at EXACTLY the caller's floor (flat on the
            // player bar, breathing on a live mic), because the blend maps level 0 onto it, and
            // full drive still reaches exactly 1, so the height ladder is untouched.
            val level = Aurora.restBlend(frame.restLevel, Aurora.displayCurve(frame.feed.level(speaker)))
            val (c1, c2) = if (speaker == AuroraSpeaker.HUMAN) HUMAN_COLORS else AI_COLORS
            // The original's edge-fading horizontal gradient: transparent → colour → transparent.
            val brush = Brush.horizontalGradient(
                listOf(c1.copy(alpha = 0f), c2, c1.copy(alpha = 0f)),
            )

            // Classic is INSTANTANEOUS — one spectral read per speaker per frame —
            // which is what lets it afford the fine BIN channel where the scrolling
            // styles cannot (they would need a read per bin per COLUMN). Bands are
            // the fallback, and are what the player bar's offline decode publishes.
            //
            // Read from the FEED, not from frame.spectra: the frame's list is the
            // carrier at the producer's ~50 Hz hop, and this draws at 60-120 Hz, so
            // reading it raw repeats the same value on 58% of drawn frames (measured
            // through the real tap at 120 Hz) and then jumps — the stepping half of
            // the v35 "isn't fluent" report. The feed's one smoother is what makes it
            // move, and it is already where the four bands below come from.
            val feed = frame.feed
            val bins = if (feed.hasSpectrum(speaker)) {
                for (k in 0 until AuroraAnalyser.BINS) binScratch[k] = feed.bin(speaker, k)
                binScratch
            } else {
                null
            }
            val b0 = feed.band(speaker, 0)
            val b1 = feed.band(speaker, 1)
            val b2 = feed.band(speaker, 2)
            val b3 = feed.band(speaker, 3)

            // The three layers, VoiceWaveform's exact (alpha, freq, phaseOffset)
            // triples and its height ladder, so a Classic ribbon reads as it always
            // did — with each layer's height now shared out by the spectrum (A4).
            for (layer in 0 until ClassicMath.LAYERS) {
                drawRibbon(
                    ClassicMath.layerAmplitude(
                        level,
                        ClassicMath.layerShare(bins, b0, b1, b2, b3, layer),
                        layer,
                    ),
                    ClassicMath.LAYER_ALPHA[layer],
                    ClassicMath.LAYER_FREQ[layer],
                    phase + ClassicMath.LAYER_PHASE[layer],
                    brush,
                    stroke,
                )
            }
        }
    }

    private fun DrawScope.drawRibbon(
        heightFraction: Float,
        alpha: Float,
        freq: Float,
        phase: Float,
        brush: Brush,
        stroke: Stroke,
    ) {
        val midY = size.height / 2f
        val amp = midY * heightFraction
        val steps = 64
        val path = Path().apply {
            moveTo(0f, midY)
            for (i in 0..steps) {
                val x = size.width * i / steps
                val t = i.toFloat() / steps
                // PI-taper to the centre line at both ends, exactly as the original.
                val envelope = sin(PI * t).toFloat()
                val y = midY + sin(t * freq * 2f * PI.toFloat() + phase) * amp * envelope
                lineTo(x, y)
            }
        }
        drawPath(path = path, brush = brush, alpha = alpha, style = stroke)
    }

    companion object {
        private val SPEAKERS = AuroraSpeaker.entries

        /** VoiceWaveform's phase period (its 2200 ms infiniteRepeatable), in seconds. */
        private const val PHASE_PERIOD_SEC = 2.2f

        // Red = human, blue = AI (A2). Pairs feed the edge-fading gradient; both
        // opaque, alpha comes from the per-layer ladder above.
        private val HUMAN_COLORS: Pair<Color, Color> = BbxAccent to BbxRed
        private val AI_COLORS: Pair<Color, Color> = WaveBlue to WaveBlue
    }
}

/**
 * Classic's layer ladder and its SPECTRAL drive (A4), as pure functions — no Compose, no android.*,
 * so every number the look depends on is pinned by ClassicTest on the JVM.
 *
 * THE ONE NON-OBVIOUS RULE: a layer is driven by its slice's level RELATIVE TO THE LOUDEST SLICE IN
 * THE SAME FRAME, never by the slice's absolute level. Absolute band levels are not comparable
 * ACROSS bands on live audio — the measured live spread between the loudest and quietest band is
 * over an order of magnitude (bands 1-3 sit on their noise floor while band 0 carries the voice), so
 * a layer driven by an absolute band level would simply be switched off on a phone mic. A ratio is
 * immune to that: it asks "which part of this voice is loud right now", which is the question the
 * three ribbons exist to answer, and it keeps answering it correctly if the band calibration moves.
 */
internal object ClassicMath {

    /** VoiceWaveform's three ribbons. Not a knob — the layer tables below are all this wide. */
    const val LAYERS = 3

    /** Per-layer height ladder, alpha, sine frequency and phase offset — VoiceWaveform's originals. */
    val LAYER_HEIGHT = floatArrayOf(0.9f, 0.6f, 0.35f)
    val LAYER_ALPHA = floatArrayOf(0.9f, 0.5f, 0.3f)
    val LAYER_FREQ = floatArrayOf(1.6f, 2.4f, 3.3f)
    val LAYER_PHASE = floatArrayOf(0f, 1.1f, 2.3f)

    /**
     * How much of a layer's height survives when its slice of the spectrum is SILENT relative to the
     * loudest one. Classic is three layered ribbons; a style that deleted a ribbon on a dark voice
     * would stop being Classic, so the spectrum redistributes emphasis rather than switching layers
     * on and off. The remaining 1 - this is what the spectrum actually moves.
     */
    const val SHAPE_FLOOR = 0.35f

    /** Below this the frame carries no usable spectral shape and every layer reads neutral. */
    private const val SHAPE_EPS = 1e-4f

    /**
     * Half-open BIN ranges backing each layer when a producer publishes a spectrum: body, presence,
     * air. Three equal slices of the log-frequency map, which (with [AuroraAnalyser.BINS] = 24 over
     * 80..7200 Hz) puts the split points near 350 Hz and 1.6 kHz — the same places the four Goertzel
     * bands divide, so the two channels tell one story and a style cannot look different for having
     * been handed the finer one.
     */
    private val BIN_GROUP_START = intArrayOf(0, 8, 16, AuroraAnalyser.BINS)

    /** First bin of group [group]; `group == LAYERS` is the end of the map. Clamps, never throws. */
    fun binGroupStart(group: Int): Int = BIN_GROUP_START[group.coerceIn(0, LAYERS)]

    /**
     * Layer [layer]'s share of the frame's spectrum, 0..1 — from the published [bins] when there are
     * any, else from the four bands. The spectrum is the finer reading of the SAME audio, so it
     * wins; an all-zero spectrum (a present-but-silent producer) carries no opinion and reads
     * neutral, exactly like silent bands.
     *
     * [bins] is a plain array rather than an AuroraBins because that is what the renderer reads
     * out of [com.aiblackbox.portal.ui.components.wave.WaveformFeed.bin] — the SMOOTHED channel
     * every other spectral style takes, and the same type their math takes. Null is "no spectrum on
     * this surface", which on the player bar is the literal truth.
     */
    fun layerShare(bins: FloatArray?, b0: Float, b1: Float, b2: Float, b3: Float, layer: Int): Float =
        if (bins != null) binLayerShare(bins, layer) else bandLayerShare(b0, b1, b2, b3, layer)

    /**
     * The band-channel share. Bands 0 and 1 (120/220 and 500/800 Hz) are ONE slice — the body of the
     * voice — so the slow tall ribbon answers voicing and vowels together; band 2 is presence and
     * band 3 air. Grouped by MAX, matching the feed's own max-not-sum reading of "how loud is this".
     */
    fun bandLayerShare(b0: Float, b1: Float, b2: Float, b3: Float, layer: Int): Float =
        share(maxOf(sanit(b0), sanit(b1)), sanit(b2), sanit(b3), layer)

    /** The bin-channel share — the same three slices, read over [BIN_GROUP_START] instead. */
    fun binLayerShare(bins: FloatArray, layer: Int): Float =
        share(groupPeak(bins, 0), groupPeak(bins, 1), groupPeak(bins, 2), layer)

    /**
     * A ribbon's height fraction: the loudness sets the size, the share decides how much of it this
     * layer gets. Can never exceed `level * LAYER_HEIGHT[layer]`, which is exactly the pre-spectral
     * amplitude — so Classic's no-mesa budget is inherited rather than re-argued.
     */
    fun layerAmplitude(level: Float, share: Float, layer: Int): Float =
        sanit(level) * LAYER_HEIGHT[layer.coerceIn(0, LAYERS - 1)] *
            (SHAPE_FLOOR + (1f - SHAPE_FLOOR) * sanit(share))

    /** Sanitised like every other array read here: out of range or NaN is 0, else clamped 0..1. */
    private fun groupPeak(bins: FloatArray, group: Int): Float {
        var m = 0f
        for (k in binGroupStart(group) until binGroupStart(group + 1)) {
            val v = if (k < bins.size) sanit(bins[k]) else 0f
            if (v > m) m = v
        }
        return m
    }

    private fun share(e0: Float, e1: Float, e2: Float, layer: Int): Float {
        val peak = maxOf(e0, maxOf(e1, e2))
        if (peak <= SHAPE_EPS) return 1f
        val e = when (layer.coerceIn(0, LAYERS - 1)) {
            0 -> e0
            1 -> e1
            else -> e2
        }
        return (e / peak).coerceIn(0f, 1f)
    }

    /** NaN is caught explicitly — `coerceIn` passes it straight through (JLS 15.20.1). */
    private fun sanit(v: Float): Float = if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
}
