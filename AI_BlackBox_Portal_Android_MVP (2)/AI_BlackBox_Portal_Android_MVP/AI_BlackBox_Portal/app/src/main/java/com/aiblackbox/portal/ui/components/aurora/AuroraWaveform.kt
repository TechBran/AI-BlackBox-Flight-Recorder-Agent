package com.aiblackbox.portal.ui.components.aurora

import androidx.compose.foundation.layout.Spacer
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.WaveAmber
import com.aiblackbox.portal.ui.theme.WaveBlue
import com.aiblackbox.portal.ui.theme.WaveGreen
import kotlinx.coroutines.flow.first
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.sin
import kotlin.math.pow

/**
 * Aurora waveform — layered translucent wave SHEETS, one identity colour per speaker, drawn into
 * whatever box the caller sizes it to, with nothing behind them.
 *
 * Each sheet is a closed path between a crest curve and a half-cycle-shifted bottom curve, painted
 * three times: a translucent body, a wide low-alpha bloom stroke and a bright thin crest line. That
 * three-pass stack is where the depth comes from and it is the whole look — do not collapse it to
 * one pass.
 *
 * Ported from whisper-everywhere's `BarWaveformView` (2026-07-18), which despite its name drew no
 * bars. Every wave constant below is that view's, measured and tuned on device. The SEVEN departures
 * are deliberate, each is called out at the code it affects, and none of them should be "restored
 * to parity":
 *
 *   C1 One amplitude in, one ribbon out -> a LIST of voices. BlackBox shows the human's mic and
 *      the AI's playback in the SAME container at the SAME time, and they have to stay telling
 *      apart. They are told apart by DIRECTION OF TRAVEL- the human's ribbons run left to right
 *      and the AI's right to left, continuously and forever, which is unmistakable within a
 *      second of watching and only gets clearer the longer you watch (Brandon, 2026-07-27). See
 *      [AuroraSpeaker], [Aurora.travelDirection], [Aurora.phaseBias] and [Aurora.driftBias].
 *   C2 The six-stop red->magenta->purple->blue->navy gradient is DELETED, and hue carries the
 *      SPECTRUM rather than the speaker- sheet j is band j (C7), so band j owns a solid colour
 *      that BOTH speakers paint it in. Red voicing, blue vowel, green formant, amber sibilance;
 *      see [auroraBandColor]. Colour used to mean the speaker (red human, blue AI) and could not
 *      go on meaning it once C1 handed that job to the travel direction — one ribbon cannot be
 *      red-because-human and green-because-formant at the same time. Depth still comes from the
 *      alpha ladder, never from hue. (Brandon, 2026-07-26- "the gradient in the middle I never
 *      really liked"; 2026-07-27 for the per-band palette.)
 *   C3 Motion was per FRAME — `phase += 0.13f` and one attack/release blend per `onDraw`. On a
 *      120 Hz Fold that ran the entire animation at DOUBLE the tuned speed. Everything is per
 *      SECOND now, driven by measured elapsed time. See [Aurora.approach].
 *   C4 Geometry assumed a fixed 160x56dp pill and bounded the sheets with that pill's stadium
 *      caps. We render at 40dp, 52dp and 140dp, always full width, never a pill. The bounds come
 *      from the DrawScope size now. See [Aurora.railTop] and [Aurora.edgeTaper].
 *   C5 Nothing opaque, at all. The particle field renders behind this and must show through; the
 *      composable paints sheets and only sheets. (Guarded by a test — this project has shipped an
 *      opaque waveform container twice.)
 *   C6 The original ran a 60 fps ValueAnimator for as long as it was attached. This one stops
 *      asking for frames when every voice is silent and settled, always stops when it has no
 *      voices at all (nothing is drawn in that state, so no call site may opt out of it), and
 *      parks while backgrounded.
 *   C7 ONE BAND PER RIBBON, BIPOLAR ABOUT ONE SHARED CENTRE LINE, TRAVELLING VERY SLOWLY. This is
 *      the largest divergence, it is the shape Brandon specified on 2026-07-27 ("each ribbon
 *      should represent a different band of the audio spectrum and its amplitude is how high it
 *      goes... we want our ribbon to go vertical in the upwards direction and the bottom as well.
 *      Then we can introduce a small movement, you know, from left to right, like we had, so it's
 *      not stale anymore" — plus, asked to choose- "smooth wave, few humps" and "all share one
 *      centre line"), and it is the one most likely to be "restored" by somebody diffing against
 *      the reference. Four things differ from that reference and every one is deliberate:
 *      ONE- SPEED. What matters is not the raw phase multiplier but the speed the CREST travels
 *      along x, which is `phaseMultiplier * PHASE_RATE_PER_SEC / (PI * frequency)` widths/sec
 *      (hold the sine's argument constant and differentiate). The first tuning ran the crests at
 *      2.4..2.9% of the width per second- 34 to 42 seconds to cross the pill, which Brandon
 *      rejected on device on 2026-07-27 as "there's no motion". [Aurora.phaseMultiplier] is now
 *      0.805..1.460, chosen so the four crests travel at 0.25 / 0.29 / 0.33 / 0.37 widths/sec
 *      (a research-backed "alive but not scrolling" band), with a ~15% per-layer speed shear that
 *      reads as parallax depth. That is one crossing of a ~40dp bar every 3 to 4 seconds- plainly
 *      moving, nothing you would call a scroll. The reference's 1.0..2.3 FULL cycles per second
 *      ("moving from right to left across the screen", rejected 2026-07-26) is faster still. Do
 *      not retune toward EITHER extreme "for parity"; a test bounds the crest speed from both
 *      sides.
 *      TWO- HUMPS. `sin(progress * PI * freq)` draws freq/2 humps across the width, so the
 *      reference's freq 1.15..2.80 was 0.6..1.4 humps- ONE broad arc however tall it was drawn,
 *      which is what read as flat at a MEASURED 62% of the available half-height.
 *      [Aurora.frequency] is 8.0..9.8, i.e. 4.0..4.9 humps- Brandon asked on 2026-07-27 for MORE
 *      humps than the 2.1..3.0 the first pass shipped. A flat ribbon is fixed with more humps,
 *      never by raising [Aurora.AMP_FRACTION] or the analyser's gains.
 *      THREE- ONE BAND PER SHEET. Sheet j's WHOLE amplitude is band j's energy, so the four
 *      bands are four ribbons rising and falling independently through each other about the same
 *      centre line, and hue is free to name the band (C2). The reference drove every sheet from
 *      one global level.
 *      FOUR- DIRECTION IS IDENTITY. The travel is signed per speaker (C1), which the reference
 *      had no need of, having only one voice to draw.
 *      An intermediate design (2026-07-26, "C7/C8") is GONE- it built every sheet's crest from a
 *      STANDING lattice of eight band-driven mountains rising one-sided off a ground line at 55%
 *      of the box. PEAKS, PEAK_BANDS, ridge(), peakShift(), speakerPeakShift(), GROUND_FRACTION
 *      and groundY() went with it, as did the tests that pinned them. Do not restore any of it:
 *      the ribbons are one-band, bipolar and moving, by request. The composition problems the
 *      ground line existed to solve cannot recur either — a wave that is symmetric about the
 *      centre uses its box evenly by construction, so there is nothing left for a ground line to
 *      correct and reintroducing one would only push the ink off centre again.
 *
 * The maths lives in [Aurora] as pure functions so it is testable on the JVM without a device;
 * this file's composable is only a traversal of them. See AuroraWaveformTest.
 */

// =============================================================================
// Public surface
// =============================================================================

/** Who a ribbon belongs to. The colour and the weave offsets both key off this. */
enum class AuroraSpeaker { HUMAN, AI }

/**
 * One live voice- a speaker identity plus its current four band energies (0..1), as produced by
 * [AuroraAnalyser]. Sheet j follows band j.
 *
 * Deliberately NOT a data class. A `FloatArray` property in a data class compares by IDENTITY, so
 * two updates carrying equal band data would compare unequal and Compose would recompose every
 * audio chunk forever, while one scratch array reused in place would compare EQUAL after its
 * contents changed and the ribbon would freeze. Content equality plus a defensive copy is the only
 * combination that behaves — the copy is four floats and it also gets the bands safely across from
 * the audio thread that produced them to the frame thread that draws them.
 */
class AuroraVoice(val speaker: AuroraSpeaker, bands: FloatArray) {

    private val bands: FloatArray = bands.copyOf()

    /**
     * Energy driving sheet [index], clamped to 0..1, and TOTAL- every index and every float value,
     * including NaN, produces a usable number. This is read inside a draw loop and must never take
     * a frame down.
     *
     * A caller with FEWER bands than sheets drives the remaining sheets from its last band, which
     * is how a source that only has an RMS level (a single value) still animates the whole stack —
     * the original's global-level fallback, expressed per voice.
     *
     * NaN is checked EXPLICITLY because `coerceIn` does not catch it- every comparison against NaN
     * is false, so neither the `< min` nor the `> max` branch fires and it passes straight through
     * (JLS 15.20.1). One NaN reaching the envelope is unrecoverable rather than merely ugly: the
     * exponential approach is absorbing (`NaN + (target - NaN) * k` is NaN forever), so the sheet
     * would flatline permanently and, because `abs(NaN - floor) > eps` is false, report itself
     * SETTLED while doing it. A level computed as rms/peak with a zero peak is exactly this input.
     */
    fun band(index: Int): Float {
        if (bands.isEmpty()) return 0f
        val v = bands[index.coerceIn(0, bands.size - 1)]
        return if (v.isNaN()) 0f else v.coerceIn(0f, 1f)
    }

    /**
     * True while this voice is loud enough that the ribbon would visibly move.
     *
     * Goes through [band] rather than reading the raw array, so the frame loop and the envelope
     * agree about what a given input means. Reading raw, a NaN-only voice reported silence here
     * while the envelope was still being handed NaN — the loop parked mid-glitch.
     */
    internal fun hasEnergy(): Boolean {
        for (i in bands.indices) if (band(i) > Aurora.ENERGY_FLOOR) return true
        return false
    }

    override fun equals(other: Any?): Boolean =
        this === other || (other is AuroraVoice && speaker == other.speaker && bands.contentEquals(other.bands))

    override fun hashCode(): Int = 31 * speaker.hashCode() + bands.contentHashCode()

    override fun toString(): String = "AuroraVoice($speaker, ${bands.contentToString()})"
}

/**
 * Draws [voices] as overlapping aurora ribbons filling [modifier]'s bounds.
 *
 * The caller sizes it (`Modifier.fillMaxWidth().height(52.dp)`) — every dimension in here is
 * derived from that box, so the same composable serves the composer strip, the player bar and the
 * voice screen without a size parameter.
 *
 * Pass one voice for the ordinary case, or two to show the human and the AI at once. A voice that
 * disappears from the list retires smoothly to flat rather than vanishing.
 *
 * @param pauseWhenIdle stop requesting frames once nothing is moving. Same knob and same meaning
 *   as VoiceWaveform's, but defaulted the other way round- that one defaults to `false` purely to
 *   keep its existing call sites byte-for-byte identical, and this composable has no existing call
 *   sites to preserve. Pass `false` for a ribbon that must flow even in silence. It only defers the
 *   park for a PRESENT voice- an empty [voices] list parks either way, because there is then
 *   nothing on screen to keep flowing (see the frame loop).
 * @param restLevel where a PRESENT but silent voice settles, 0..1. [Aurora.BASELINE] keeps the
 *   ribbon breathing gently through the pauses between words, which is what a live mic wants — a
 *   mic that vanishes between syllables reads as broken. The player bar passes 0 instead, because
 *   its silence is the FILE's silence and resting flat on it is behaviour VoiceWaveform shipped
 *   (`idleLevel = 0f`) and M3 is required to preserve. A voice resting at 0 is still DRAWN — flat,
 *   not gone (see [Aurora.paintsSheet]); only an ABSENT voice retires off the screen, and it does
 *   so whatever this is set to. This is purely the floor a present one sits on.
 */
@Composable
fun AuroraWaveform(
    voices: List<AuroraVoice>,
    modifier: Modifier = Modifier,
    pauseWhenIdle: Boolean = true,
    restLevel: Float = Aurora.BASELINE,
) {
    // Keyed on restLevel- it is a per-call-site constant everywhere it is used, so rebuilding on a
    // change costs nothing real and beats an engine quietly holding a floor its caller abandoned.
    val engine = remember(restLevel) { AuroraEngine(restLevel) }
    // Hoisted out of the draw path (the original kept `sheetPath`/`crestPath` as fields for the
    // same reason)- two Paths rebuilt in place, never allocated per frame. Their native storage is
    // kept between frames too; see the rewind()-not-reset() note in the draw loop.
    val paths = remember { AuroraPaths() }
    // The loop's handle on the CURRENT voices. Without rememberUpdatedState the coroutine captures
    // whatever list existed when it launched and never sees another one.
    val liveVoices = rememberUpdatedState(voices)
    // The ONLY snapshot state the draw phase reads, and the sole reason it re-runs. Writing it per
    // frame invalidates draw and nothing else — the enclosing screen never recomposes for the
    // ribbon. Everything the draw phase actually needs (the phase, the levels) is read off the
    // engine as plain fields in the same frame.
    //
    // A monotonic TICK rather than a mirror of the engine's phase, because an EQUAL write is not a
    // write- SnapshotMutableFloatStateImpl compares the incoming value against the current record
    // and branches past the write entirely, so it invalidates nothing. The frame where dt is 0 (the
    // first frame of a fresh composition, a resume or a wake from the idle park) advances neither
    // the phase nor any envelope, so a value mirror would go quiet on precisely the frame that
    // flips a voice to PRESENT. On the player bar that is fatal rather than cosmetic- it rests at
    // `restLevel = 0f` on all-zero bands, so that same frame leaves the engine already settled and
    // the loop parks immediately afterwards, and the flat ribbon M3 requires (see [Aurora.paintsSheet])
    // would be authorised and never painted, leaving an empty 40dp box until something else
    // happened to recompose the bar. Incrementing unconditionally cannot have that failure mode.
    val tick = remember { mutableIntStateOf(0) }
    val lastNanos = remember { longArrayOf(0L) }

    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(engine, pauseWhenIdle, lifecycleOwner) {
        // withFrameNanos is Choreographer-driven and keeps firing while the activity is stopped,
        // so the lifecycle gate is pure battery: park the whole loop when we cannot be seen.
        lifecycleOwner.lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            lastNanos[0] = 0L
            while (true) {
                // Hoisted out of the frame callback so the park below can see it- a frame that
                // advanced no time cannot have finished anything, so it is not allowed to park.
                val steppedSec = withFrameNanos { t ->
                    // dt = 0 on the first frame of a resume or a wake, so no accumulated-time jump.
                    // The cap covers a stalled frame clock- a half-second dt would teleport the
                    // phase and snap every envelope to its target in one step.
                    val dt = if (lastNanos[0] == 0L) 0f else
                        ((t - lastNanos[0]).toDouble() / 1_000_000_000.0)
                            .coerceAtMost(MAX_FRAME_DT_SEC.toDouble()).toFloat()
                    lastNanos[0] = t
                    engine.step(dt, liveVoices.value)
                    tick.intValue++
                    dt
                }
                // An EMPTY list parks whatever `pauseWhenIdle` says. That flag's justification —
                // a frozen wave reads as a hung app — only holds while a voice is PRESENT; with
                // no voice at all there is no wave to freeze, and once both envelopes have
                // retired [Aurora.paintsSheet] is false for every sheet, so the loop would be
                // stepping the engine and invalidating the draw phase at up to 120 Hz to paint
                // literally nothing. The voice screen sits in exactly that state before the
                // session connects and after it disconnects. `needsFrames` still decides WHEN:
                // a departing voice keeps its frames until it has animated away.
                //
                // `steppedSec > 0f`- a dt-of-0 frame has decided nothing yet. The first frame of a
                // fresh composition, a resume or a wake steps every envelope by zero, so an engine
                // that arrives ALREADY settled (a present, silent, zero-floor voice — the idle
                // player bar) would report `needsFrames` false on the very frame that first made it
                // PRESENT, and park before one tick of real time had passed. One more frame costs
                // nothing and guarantees the picture the loop parks on is one it animated to.
                val current = liveVoices.value
                if ((pauseWhenIdle || current.isEmpty()) && !engine.needsFrames(current) && steppedSec > 0f) {
                    // C6- SUSPEND rather than spin. Nothing is moving, so the next frame would
                    // paint the identical picture. A live mic never actually reaches this state
                    // between words (room noise keeps the bands well above the baseline once the
                    // analyser's auto-gain has normalised it) — this is for a stream that has
                    // genuinely stopped, or a ribbon composed on a screen nobody is talking to.
                    snapshotFlow { liveVoices.value }.first { engine.needsFrames(it) }
                    lastNanos[0] = 0L
                }
            }
        }
    }

    // Spacer + drawWithCache is what Canvas() is, minus the trap: size-derived setup runs in the
    // cache block, which re-runs only when the size actually changes, leaving onDrawBehind a pure
    // read-and-draw on every one of 120 frames a second.
    Spacer(
        modifier = modifier.drawWithCache {
            val h = size.height
            val w = size.width
            // Stroke widths are FRACTIONS of the height, not the original's fixed 2dp/9dp. Those
            // were 3.6% and 16% of its 56dp pill; hard px values would have been a hairline on the
            // 140dp voice screen and a slab on the 40dp player bar. The dp minimums stop them
            // disappearing entirely at 1x density.
            val crestPx = Aurora.crestStrokePx(h, 1.dp.toPx())
            val bloomPx = Aurora.bloomStrokePx(h, 3.dp.toPx())
            val insetPx = Aurora.insetPx(h, crestPx)
            val steps = Aurora.sampleCount(w)
            val crestStroke = Stroke(width = crestPx, cap = StrokeCap.Round, join = StrokeJoin.Round)
            // The bloom is a 13%-alpha halo and is allowed to bleed past the path envelope- it is
            // drawn over a transparent surface with nothing to clip it, and pulling it inside
            // would replace a soft glow with a visible hard edge. Only the CREST line is bounded
            // (its half-width is inside insetPx), because that one reads as the ribbon's edge.
            val bloomStroke = Stroke(width = bloomPx, cap = StrokeCap.Round)

            onDrawBehind {
                // Reading the tick HERE is what subscribes the draw phase to the frame loop; the
                // value is deliberately never used, only the read. Everything below then comes off
                // the engine as plain fields, written by the same frame callback that bumped it.
                @Suppress("UNUSED_VARIABLE") val subscribe = tick.intValue
                val nowPhase = engine.phaseRad
                if (w <= 0f || h <= 0f) return@onDrawBehind

                // Interleaved by LAYER, not by voice- drawing one speaker's whole stack and then
                // the other's would put every one of the second speaker's crests on top of every
                // one of the first's, and the back ribbon would read as permanently behind.
                for (layer in 0 until Aurora.SHEETS) {
                    for (s in AURORA_SPEAKERS.indices) {
                        val speaker = AURORA_SPEAKERS[s]
                        val envelope = engine.envelope(speaker)
                        // This sheet's OWN band. A sheet is one band again (C7), so a quiet band
                        // beside a loud one has nothing on screen to keep alive.
                        val drive = envelope.levels[layer]
                        if (!Aurora.paintsSheet(envelope.present, drive)) continue

                        // Hue is the BAND (C2), so it does not vary with the speaker; what varies
                        // with the speaker is which way the wave travels and how it wanders.
                        val colour = auroraBandColor(layer)
                        val amp = Aurora.amplitude(h, drive, layer)
                        val drift = Aurora.drift(h, nowPhase, layer, Aurora.driftBias(speaker))
                        val travel = Aurora.travelDirection(speaker)
                        val bias = Aurora.phaseBias(speaker)

                        // rewind(), NOT reset(). reset() forwards to android.graphics.Path.reset(),
                        // which returns the path "to the same state it had when it was created" and
                        // frees the native point/verb storage; rewind() is the documented sibling
                        // that "keeps the internal data structure for faster reuse". Resetting would
                        // re-grow two native buffers of up to ~242 points here EIGHT times a frame
                        // (4 layers x 2 voices) at up to 120 Hz — the exact churn hoisting the Paths
                        // out of the draw path exists to avoid. A test pins the call.
                        paths.sheet.rewind()
                        paths.crest.rewind()
                        forEachSheetPoint(
                            width = w,
                            height = h,
                            steps = steps,
                            insetPx = insetPx,
                            amp = amp,
                            phase = nowPhase,
                            layer = layer,
                            travel = travel,
                            phaseBias = bias,
                            drift = drift,
                            onCrest = { x, y, first ->
                                if (first) {
                                    paths.sheet.moveTo(x, y)
                                    paths.crest.moveTo(x, y)
                                } else {
                                    paths.sheet.lineTo(x, y)
                                    paths.crest.lineTo(x, y)
                                }
                            },
                            // Closing the crest back along a second, half-cycle-shifted curve is what
                            // makes a rippling silk BODY whose thickness itself undulates, rather
                            // than a stroked line.
                            onBottom = { x, y -> paths.sheet.lineTo(x, y) },
                        )
                        paths.sheet.close()

                        // The FILL stays SrcOver (default): it is the translucent body and it is
                        // the only pass allowed to occlude the particle field, held to a per-sheet
                        // <=0.16 alpha and a 4-sheet fill+bloom coverage under 55% (see [Aurora.fillAlpha]).
                        // The BLOOM and CREST passes are BlendMode.Plus- additive LIGHT, so where
                        // sheets overlap they BRIGHTEN instead of stacking toward opaque, which is
                        // what lets the alpha ladder read as depth without dimming the field behind.
                        // The reduced crest/bloom alphas keep the summed brightness where all four
                        // sheets cross plausible rather than blown to white (asserted < 1.6).
                        drawPath(paths.sheet, colour, alpha = Aurora.fillAlpha(layer))
                        drawPath(paths.crest, colour, alpha = Aurora.bloomAlpha(layer), style = bloomStroke, blendMode = BlendMode.Plus)
                        drawPath(paths.crest, colour, alpha = Aurora.crestAlpha(layer), style = crestStroke, blendMode = BlendMode.Plus)
                    }
                }
            }
        },
    )
}

/**
 * The BAND's identity colour (C2), keyed by sheet index because sheet j is band j (C7).
 *
 * A pure function of the layer and of NOTHING else- both speakers paint band 2 the same green, so
 * a listener learns "green is the formant band" once rather than per voice. Who is speaking is
 * carried by [Aurora.travelDirection] (C1).
 *
 * The order is the analyser's own band order (see AuroraAnalyser's header), lowest frequency
 * first, so the front/brightest sheet is the voicing band a human ear weights most. Full alpha-
 * transparency is the per-pass alpha ladder's job and baking it in here would multiply twice.
 */
internal fun auroraBandColor(layer: Int): Color = when (layer.coerceIn(0, Aurora.SHEETS - 1)) {
    0 -> BbxAccent      // voicing / bass body    ~120-220 Hz
    1 -> WaveBlue       // vowel energy           ~500-800 Hz
    2 -> WaveGreen      // formants / consonants  ~1.6-2.6 kHz
    else -> WaveAmber   // sibilance              ~5-6.8 kHz
}

/** Cached once- `values()` clones its array on every call and this is read per frame. */
private val AURORA_SPEAKERS = AuroraSpeaker.entries

/** One stalled frame must not teleport the animation. */
private const val MAX_FRAME_DT_SEC = 0.05f

/** Reusable per-frame path scratch. Mutable and therefore never shared between two ribbons. */
private class AuroraPaths {
    val sheet = Path()
    val crest = Path()
}

// =============================================================================
// Geometry + envelope maths — pure, no Android, no Compose, unit-testable
// =============================================================================

internal object Aurora {

    const val SHEETS = 4

    /**
     * DEFAULT resting height of the ribbon while a voice is present but silent — the floor is a
     * per-call-site choice ([AuroraWaveform]'s `restLevel`), and the player bar sets it to 0.
     */
    const val BASELINE = 0.06f

    // ---------------------------------------------------------------- timing

    /**
     * C3- the original's per-frame constants converted to time constants, so the animation runs at
     * the speed it was tuned to on any refresh rate. Each was solved from the 60 fps blend it
     * replaces- k = 1 - exp(-dt/tau), dt = 1/60. A test asserts the round trip.
     *
     * Fast attack, slow release, was and remains the point- a syllable kicks the waves inside one
     * chunk and then drifts back down.
     */
    const val ATTACK_TAU_SEC = 0.0159f          // was ATTACK  = 0.65 per frame
    const val RELEASE_TAU_SEC = 0.158f          // was RELEASE = 0.10 per frame

    /**
     * Per-band release- bass lingers, sibilance vanishes.
     *
     * RE-SCALED, was [0.295, 0.188, 0.137, 0.096]. Those were the ported original's per-frame
     * blends converted honestly, and they were too slow to draw speech: a first-order release needs
     * ln(10) = 2.3 time constants to lose 90% of its height, so those four took 0.68 / 0.43 / 0.32 /
     * 0.22 s to fall, against the ~230 ms period of connected speech at ~4.3 syllables/sec. Three
     * of the four physically could not return to the floor between one syllable and the next, and
     * the FRONT sheet — layer 0, the one the eye follows — was the worst at three syllable periods.
     * Whatever dynamics the analyser recovered upstream, that sheet drew their envelope and not
     * them: the "saturated plateau that barely moves" seen in the time domain rather than in level.
     *
     * The ladder is the original's, scaled UNIFORMLY by 0.161 so band 0 falls 90% in 0.109 s, less
     * than half a syllable. Uniform on purpose: the relative ladder (bass lingering ~3x longer than
     * sibilance) is measured character worth keeping, and it was only ever the absolute scale that
     * was wrong. WaveformDynamicsGatesTest asserts BOTH halves — every sheet inside a syllable, and
     * the ladder still monotone.
     *
     * HALVED AGAIN (0.322 -> 0.161) after the operator ran v35 and said the movement "isn't fluent
     * and that much for how much audio is coming in". A release ladder is a low-pass on the drawn
     * height, so it does not just delay motion, it DELETES range — the sheet never gets down to
     * where the audio went before the next syllable pushes it back up. Measured on a whole utterance
     * through the real analyser, the ladder was costing 11.8% of the drawn range, and the sweep that
     * chose this scale is unambiguous:
     *
     *   tau x1.00 (0.0950 s, t90 219 ms): p90/p10 =  9.24, motion/frame 0.02228
     *   tau x0.70 (0.0665 s, t90 153 ms): p90/p10 = 12.53, motion/frame 0.02413
     *   tau x0.50 (0.0475 s, t90 109 ms): p90/p10 = 14.45, motion/frame 0.02560   <- here
     *   tau x0.35 (0.0333 s, t90  77 ms): p90/p10 = 14.90, motion/frame 0.02677
     *   tau x0.25 (0.0237 s, t90  55 ms): p90/p10 = 14.82, motion/frame 0.02804
     *
     * x0.50 takes 56% of the available range gain; the curve flattens hard after it (x0.35 buys 3%
     * more range for 30% shorter taus) while the twitch the shorter taus let through keeps rising —
     * frame-to-frame jitter is the OTHER half of the operator's report and a release ladder is the
     * only thing on this path damping it. This is the knee, not the extreme, and it is the reason
     * not to keep halving.
     */
    val BAND_RELEASE_TAUS_SEC = floatArrayOf(0.0475f, 0.0305f, 0.022f, 0.0155f)

    /**
     * Was `phase += 0.13f` per frame at 60 fps, and it is LEFT at that rate: this is the raw frame
     * clock, and the vertical [drift] it feeds was tuned against it on device.
     *
     * It is NOT the speed anything appears to move at. Every consumer scales it down- the drift by
     * [DRIFT_RATE], the travelling wave by [phaseMultiplier], which is where C7's slow left-right
     * motion is set. Retuning THIS constant would retune both at once.
     */
    const val PHASE_RATE_PER_SEC = 7.8f

    /**
     * Phase accumulates for as long as a screen is open, so it has to wrap or it loses float
     * precision. It may only wrap where every consumer of it is back in phase, i.e. at a whole
     * number of turns of every rate it is multiplied by. Those rates are [DRIFT_RATE] = 0.35 and
     * the four [PHASE_MULTIPLIERS] = 0.805, 1.005, 1.225, 1.460, all of them whole multiples of
     * 0.005 (161, 201, 245, 292 turns; 0.35 is 70), so the period is 2pi/0.005 — about 161 s of
     * wall clock at [PHASE_RATE_PER_SEC], and a whole number of turns for each.
     *
     * Re-derived when the faster crest speeds landed: the grid was 0.01 (2pi/0.01) while the
     * multipliers were on a 0.01 grid, and the new 1.225 and 1.460 both land on a 0.005 grid the
     * 0.01 step does NOT divide, so the wrap would have jumped two of the four sheets sideways.
     * gcd of the five rates is exactly 0.005 — see [PHASE_MULTIPLIERS].
     *
     * The wrap is invisible for a NEGATIVE multiple of the phase too (the AI's ribbons travel the
     * other way, [travelDirection]) — sin is 2pi-periodic in both directions, so a whole number of
     * turns forward is a whole number of turns back. A test pins both signs, and a second pins the
     * whole-turns invariant against every rate directly.
     */
    val PHASE_WRAP_RAD = (2.0 * PI / 0.005).toFloat()

    /**
     * THE PERCEPTUAL CURVE — the ONE place in the whole pipeline where acoustic energy is bent on
     * its way to a drawn height, and the only one allowed to be.
     *
     * WHY IT LIVES HERE. This curve describes the EYE, not the microphone: the same bend applies
     * whether the audio came from a mic, a decoded TTS file or the Settings preview's demo driver.
     * So it belongs at the single stage every one of those paths passes through, which is also the
     * only stage a new producer cannot forget to go through. Everything that IS a property of the
     * source — band gains, floors, HEADROOM, the soft knee, the AGC time constants — lives in
     * [AuroraCalibration], which is per-source by construction. That is the line: per-eye here,
     * per-source there.
     *
     * WHAT IT REPLACES. The lift used to be applied TWICE on both live paths. The analyser applied
     * GAMMA = 0.82 and then this stage applied its square root on top (effective exponent 0.41);
     * the offline pass applied LIFT = 0.55 and then the same square root (effective 0.275). Two
     * paths, two different total curves, neither written down as a decision anywhere. Both upstream
     * curves are DELETED — do not reintroduce one, in the analyser, in the offline pass, or in a
     * producer. WaveformPerceptualCurveTest measures both paths end-to-end and fails if either
     * bends the level again.
     *
     * WHAT IT APPLIES TO. A 0..1 energy on its way to a DRAWN EXTENT — a height, a bar length, a
     * tick length, a lobe radius, a strand amplitude. NOT to a colour, an alpha, a heat ramp or a
     * threshold comparison: those are not heights, they have their own ladders (Aurora's alpha
     * stack is not curved either), and bending a percentile gate with a monotone function only
     * moves the gate. Every style call site below obeys that split.
     *
     * 0.65, RAISED from the ported original's square root after the operator ran v35 on a Fold and
     * reported the ribbon "constantly has too much going on". A square root is a very strong lift:
     * it is what dragged the room-tone floor up into the picture, and it cost most of the range
     * between a quiet frame and a loud one. Measured over a whole utterance through the real
     * analyser, 0.5 destroyed 76.1% of the 1.825 decades between the analyser's p10 and p90 — more
     * than the rest-floor clamp and the release ladder put together.
     *
     * 0.65 was chosen against three measurements at once, and it is the only exponent that satisfies
     * all three:
     *   RANGE. Whole-utterance drawn p90/p10 rises ~8%, and the voiced peak-to-typical swing ~8%,
     *   because a ratio r in energy draws as r^0.65 instead of r^0.5 — every contrast in the signal
     *   is rendered LARGER, which is what "movement for how much audio is coming in" means.
     *   NOISE. Room tone drops ~34% in drawn height at EVERY noise level simultaneously (a fixed
     *   power of a small number falls faster the smaller it is), which is the "too much going on"
     *   complaint. No gate, no threshold, no per-level tuning — the same one exponent.
     *   WHISPER. A whisper measures 0.0898 linear on the shipped path and still draws at 0.209 of
     *   full height, clear of the 0.20 visibility gate WaveformDynamicsGatesTest holds. That gate is
     *   the ceiling on this constant: much past 0.68 and a whisper stops being visible, so this is
     *   not a knob to keep turning.
     */
    const val DISPLAY_GAMMA = 0.65f

    /** [DISPLAY_GAMMA] as the function the envelope applies. Fixed points at 0 and 1. */
    fun displayCurve(energy: Float): Float = energy.coerceAtLeast(0f).pow(DISPLAY_GAMMA)

    /**
     * THE ONE REST-FLOOR RULE — how a present-but-silent voice's floor and its live level combine.
     *
     * A BLEND, `floor + (1 - floor) * level`, and never `maxOf(floor, level)`. The clamp it replaces
     * was a measured defect rather than a stylistic choice: below about -35 dBFS every level a style
     * was handed came out under the floor, so four styles drew the IDENTICAL picture across the
     * whole quiet end of the range — the measured step between two noise levels 5 dB apart was
     * exactly 0.0000. A ribbon frozen solid is worse than a small one, and the clamp cost 12.1% of
     * the whole drawn range on an ordinary utterance.
     *
     * The blend is an affine remap of 0..1 onto floor..1, so it keeps everything the clamp was there
     * for and loses the freeze:
     *   - a silent present voice sits at EXACTLY [floor] (level 0 -> floor), which is what the idle
     *     park's settle test compares against, so nothing about parking changes;
     *   - full drive still reaches exactly 1, so no amplitude budget, rail or no-mesa guarantee is
     *     touched;
     *   - it is strictly increasing, so no two different inputs can ever draw the same height;
     *   - a call site resting at 0 — the player bar, whose silence is the FILE's silence — gets
     *     `0 + 1 * level`, i.e. byte-for-byte what it drew before. The change is live-mic only.
     *
     * Total, like every other function on this path: NaN in either argument is absorbed, never
     * propagated (`coerceIn` passes NaN straight through, JLS 15.20.1, and one NaN in an envelope
     * is unrecoverable — see [AuroraVoice.band]).
     */
    fun restBlend(floor: Float, level: Float): Float {
        val f = if (floor.isNaN()) 0f else floor.coerceIn(0f, 1f)
        val l = if (level.isNaN()) 0f else level.coerceIn(0f, 1f)
        return f + (1f - f) * l
    }

    /** Within this of its resting value an envelope counts as settled (drives the idle park). */
    const val SETTLE_EPS = 0.002f

    /**
     * A band below this cannot lift a sheet clear of the DEFAULT resting baseline, so it does not
     * justify a frame.
     *
     * DERIVED, not typed, and derived because the two constants it depends on both moved: it is the
     * exact energy at which `restBlend(BASELINE, displayCurve(e))` lands SETTLE_EPS above BASELINE,
     * i.e. the largest band a sheet can carry while still being indistinguishable from its resting
     * sliver. Solving `(1 - BASELINE) * e^DISPLAY_GAMMA = SETTLE_EPS` gives the expression below.
     * Written as a literal it was 0.0038, calibrated against the OLD square root and the OLD
     * `maxOf` floor; under the blend ([restBlend]) any nonzero band lifts the sheet a little, so a
     * literal left over from the clamp would have declared a visibly-lifted sheet unworthy of a
     * frame and frozen it there. Deriving it means the next re-tune of either constant cannot
     * silently invalidate this one.
     *
     * The value is now ~7.7e-5, ~50x smaller, and that is the honest threshold rather than a
     * regression: nothing on any shipped path lands between the two. The live analyser's room tone
     * sits orders of magnitude above it, the offline player-bar path gates true silence to exactly
     * 0, and the demo driver's between-syllable rest is exactly 0 too — all three still park.
     *
     * A call site resting LOWER than the baseline is still parked correctly, just by the other two
     * conditions- a sheet does not count as `resting` until it is within SETTLE_EPS of ITS OWN
     * floor, so the loop keeps frames while a sub-floor band is still lifting it.
     */
    val ENERGY_FLOOR: Float = (SETTLE_EPS / (1f - BASELINE)).pow(1f / DISPLAY_GAMMA)

    /** Below this a DEPARTED sheet has retired- skip building its path at all. See [paintsSheet]. */
    const val MIN_VISIBLE_DRIVE = 0.002f

    /**
     * Whether a sheet at [drive] is worth building a path for.
     *
     * A PRESENT voice ALWAYS paints, however flat it has settled — the threshold gates the ABSENT
     * case only. That exemption is not an optimisation dodged, it is the player bar's entire rest
     * state- that surface passes `restLevel = 0f`, so on the file's silence every sheet settles at
     * exactly 0, and culling on the drive alone would paint NOTHING there. VoiceWaveform drew a
     * line in that state (`drawRibbon` at heightFraction 0 strokes the centre line across the bar),
     * every not-playing bar in a message list IS that state, and a silent passage mid-clip would
     * otherwise blink the ribbon out and back. At drive 0 the geometry still yields the flat
     * crest+bottom sliver that stands in for the old line. Pinned by a test.
     *
     * The threshold keeps doing its real job on an absent voice- a departed stream retires to
     * nothing rather than parking a stub on screen, and a ribbon nobody has ever fed costs nothing.
     */
    fun paintsSheet(present: Boolean, drive: Float): Boolean = present || drive > MIN_VISIBLE_DRIVE

    // -------------------------------------------------------------- geometry

    /**
     * The bright crest line's width, as a fraction of the height (1dp floor in [crestStrokePx]).
     *
     * Was 0.036 (2dp on the reference's 56dp pill). THINNED to 0.022 (~1.2dp-equivalent) as part
     * of the occlusion pass- the crest is the heaviest ink on the sheet, and at 0.036 the four
     * crests plus their bloom haloes covered 99% of the field's centre band. A thinner bright line
     * reads just as sharp now that it is composited additively (see the draw loop), and it costs
     * LESS of the amplitude budget: half the stroke folds into [insetPx], so 0.036 -> 0.022 hands
     * [AMP_FRACTION] back 0.007 of the height (0.018 -> 0.011 of half-width reserved at each rail).
     */
    const val CREST_STROKE_FRACTION = 0.022f

    /**
     * The soft halo's width, as a fraction of the height (3dp floor in [bloomStrokePx]).
     *
     * Was 0.16 (9dp on the 56dp pill). NARROWED to 0.10- the bloom is now an additive (BlendMode.Plus)
     * light pass, so a narrower, lower-alpha halo brightens the crest without smearing a wide
     * translucent band across the particle field behind. Part of the same alpha-budget pass that
     * dropped [bloomAlpha]'s numerator.
     */
    const val BLOOM_STROKE_FRACTION = 0.10f

    /** Was a flat 3dp inset inside the pill's stadium interior. */
    const val EDGE_INSET_FRACTION = 0.054f

    /**
     * How much of the WIDTH each end taper occupies (C4).
     *
     * The original pinched its sheets on the pill's cap radius, which was half its height. Ours are
     * full-width rectangles up to 50x as wide as they are tall, so a height-derived taper would
     * span 5% of the width and read as a hard cut at both ends. Width-relative keeps the ribbon
     * terminating in a point at any aspect ratio, and matches what VoiceWaveform already does with
     * its `sin(PI * t)` end taper.
     */
    const val TAPER_FRACTION = 0.12f

    /**
     * Full-scale crest excursion EITHER SIDE of the centre line, as a fraction of the height.
     *
     * RAISED to 0.35 for reactivity- Brandon wanted a taller, more responsive ribbon, and the
     * headroom to give it came from halving [DRIFT_FRACTION] (0.06 -> 0.03) and thinning the crest
     * stroke ([CREST_STROKE_FRACTION] 0.036 -> 0.022). The budget, exactly:
     *
     *   a sheet reaches `AMP_FRACTION + its worst drift` from the centre, and that sum has to fit
     *   inside `0.5 - EDGE_INSET_FRACTION - CREST_STROKE_FRACTION/2` = 0.5 - 0.054 - 0.011 = 0.435
     *   or the clamp in [crestY] turns a summit into a flat mesa. The worst drift is the outer
     *   sheets' 2.25 x [DRIFT_FRACTION] = 2.25 x 0.03 = 0.0675 (see [drift]), so the budget for the
     *   amplitude is 0.435 - 0.0675 = 0.3675, and 0.35 leaves 0.0175 of the height in hand.
     *
     * The binding case is sheet 0 at full drive: it carries the full amplitude AND, being the
     * furthest-out layer on the up side, the full 0.0675 up-drift, so `0.5 - 0.0675 - 0.35 = 0.0825`
     * of the height still separates its highest possible peak from the top rail at 0.065. NOTHING
     * clips at full drive on either edge, at any layer, at any drift phase — pinned by a test to
     * EXACTLY zero clamped samples, because a mesa is the exact defect this geometry was rewritten
     * twice to remove.
     *
     * A bipolar wave spends BOTH sides of the centre, so the extra amplitude buys reach up AND down
     * about the shared centre line without pushing the optical centre off it. If it ever reads as
     * flat the lever is [frequency], never this.
     */
    private const val AMP_FRACTION = 0.35f

    /**
     * RESTORED to the reference's 0.7 (C7). The bottom edge is a sine about the same centre line
     * as the crest again, so — as in the reference — roughly half of this excursion sits on the
     * crest's side of the line and the body's mean weight is about half of what the number looks
     * like. The intermediate one-sided design had to halve it to 0.35 to pay for silhouettes
     * filled all the way from a ridge down to a belly; that bill is gone with the ridge.
     *
     * It is also what makes the sheet SYMMETRIC- the crest reaches [AMP_FRACTION] up and the belly
     * 0.7 of that plus [BOTTOM_OFFSET_FRACTION] down, which at every layer lands within ~14% of
     * equal (the gap widened slightly when [AMP_FRACTION] rose to 0.35, the fixed offset now a
     * smaller share of a taller wave). Lowering it thins the body back toward a stroked line and tips the ink upward off the
     * centre; raising it past ~0.85 makes the body a constant-width band (see [BOTTOM_PHASE_SHIFT])
     * and doubles the accumulated fill alpha the particle field has to show through.
     */
    private const val BOTTOM_AMP_SCALE = 0.7f
    private const val BOTTOM_OFFSET_FRACTION = 0.055f

    /**
     * How far the slow vertical [drift] carries a sheet's centre line, as a fraction of the height,
     * before its per-layer ladder factor (up to 2.25x, see [drift]).
     *
     * HALVED from 0.06 to 0.03 to buy amplitude headroom. The drift eats into the same rail budget
     * the crest does- a sheet's worst-case peak is `AMP_FRACTION + 2.25 * DRIFT_FRACTION` from the
     * centre. At 0.06 the drift alone claimed 0.135 of the height and capped [AMP_FRACTION] at 0.28;
     * at 0.03 it claims 0.0675 and lets the amplitude rise to 0.35 (see [AMP_FRACTION]'s budget).
     * The weave is still plainly visible at half the swing- the sheets still cross through each
     * other, just over a slightly narrower vertical band, which a two-voice screen never reads as
     * less alive. The drift TEST measures relative magnitudes and signs, both unchanged by scaling.
     */
    private const val DRIFT_FRACTION = 0.03f
    private const val DRIFT_RATE = 0.35f

    // ------------------------------------------------------- the wave (C7)

    /**
     * Humps across the width, per sheet.
     *
     * `sin(progress * PI * freq)` advances freq/2 CYCLES over `progress` 0..1, so these draw 4.0,
     * 4.3, 4.6 and 4.9 humps, each sheet on its own count so the four do not travel as one slab.
     * RAISED from 4.2..6.0 (2.1..3.0 humps): Brandon asked for MORE humps on 2026-07-27, and a
     * ribbon that reads as flat is fixed HERE, not at [AMP_FRACTION] or the analyser's gains.
     *
     * This sits right at the honest caution the first pass logged- past ~4 humps a 40dp player
     * bar's ribbon starts reading more as ripple than as a single wave. Brandon asked for it anyway
     * with eyes open, and [sampleCount] is NOT the limit (measured: faceting only sets in past
     * ~freq 60, an order of magnitude above this). If the ripple ever reads as too busy on the
     * smallest bar the lever is here; nothing downstream clamps it.
     */
    fun frequency(layer: Int): Float = 8.0f + layer.coerceIn(0, SHEETS - 1) * 0.6f

    /**
     * How fast the wave travels along x, per sheet, as a multiple of the [PHASE_RATE_PER_SEC]
     * frame clock. THE constant the left-right motion turns on.
     *
     * These are chosen for the observable quantity- the CREST TRAVEL SPEED, not the raw multiplier.
     * A crest sits where `progress * PI * frequency - travel * phase * mult` is constant, so holding
     * that argument fixed the crest advances `mult * PHASE_RATE_PER_SEC / (PI * frequency)` widths
     * per second. Solving that for the research-backed target speeds 0.25 / 0.29 / 0.33 / 0.37
     * widths/sec at the [frequency] ladder 8.0 / 8.6 / 9.2 / 9.8:
     *
     *   mult = target * PI * frequency / PHASE_RATE_PER_SEC
     *        = 0.25*PI*8.0/7.8, 0.29*PI*8.6/7.8, 0.33*PI*9.2/7.8, 0.37*PI*9.8/7.8
     *        = 0.8055, 1.0045, 1.2228, 1.4604
     *
     * snapped to the 0.005 grid (see below) to 0.805, 1.005, 1.225, 1.460, which travel at 0.250,
     * 0.290, 0.331, 0.370 widths/sec- a crossing of a bar every ~3-4 s, with a ~15% per-layer speed
     * shear that reads as parallax. The first pass ran these at 2.4..2.9% of the width per second
     * (34-42 s per crossing), which Brandon rejected on device as "there's no motion".
     *
     * THE GRID. [PHASE_WRAP_RAD] may only wrap where every consumer of the phase is a whole number
     * of turns in, so all of these AND [DRIFT_RATE] (0.35) must be integer multiples of one grid
     * step. gcd(0.805, 1.005, 1.225, 1.460, 0.35) = 0.005, so the grid is 0.005 and the wrap is
     * 2pi/0.005 (161, 201, 245, 292 and 70 turns respectively).
     *
     * Bounded from BOTH sides by a test on the crest speed, because the two failure modes (stale,
     * scrolling) are opposite and a fix for one is the other.
     */
    private val PHASE_MULTIPLIERS = floatArrayOf(0.805f, 1.005f, 1.225f, 1.460f)

    fun phaseMultiplier(layer: Int): Float = PHASE_MULTIPLIERS[layer.coerceIn(0, SHEETS - 1)]

    /**
     * How far the bottom edge's wave lags the crest's- HALF A CYCLE.
     *
     * With this shift the body is THICKEST exactly where the crest is highest and pinches where it
     * is lowest (thickness works out at `BOTTOM_OFFSET_FRACTION * height + (1 - BOTTOM_AMP_SCALE) *
     * amp * wave`), which is the undulating silk the reference got from running its bottom edge at
     * a different frequency AND phase. At a shift of zero the two edges would swing in opposition
     * and the sheet would flap open to 1.7 x its amplitude and shut again; the ribbon has to read
     * as ONE object with a rippling thickness, not as two lines with weather between them.
     *
     * The two edges do cross where `0.3 * amp` (the `1 - BOTTOM_AMP_SCALE` term) exceeds the offset
     * — at the taller [AMP_FRACTION] = 0.35, by 5.0% of the height at the very bottom of a full-drive
     * trough, 2.5% at speech. That is the ribbon pinching to a twist, a handful of pixels on any
     * shipped size, and the alternative (an offset big enough to prevent it) is a fat resting sliver
     * on the idle player bar.
     */
    private val BOTTOM_PHASE_SHIFT = PI.toFloat()

    /**
     * The travelling wave, -1..1, at [progress] across the width. The ONE place where position,
     * time and speaker identity meet.
     *
     * [travel] is +1 for a wave running left to right and -1 for right to left (C1), and it enters
     * as the SIGN of the time term because that is what a direction of travel is: holding the sine's
     * argument constant, `progress` has to move with `travel * phase` to stay on the same crest.
     */
    fun wave(progress: Float, phase: Float, layer: Int, travel: Float, phaseBias: Float): Float =
        crestShape(
            sin(progress * PI.toFloat() * frequency(layer) - travel * phase * phaseMultiplier(layer) + phaseBias)
        )

    /**
     * THE WAVESHAPER — what turns a row of domes into a row of MOUNTAINS.
     *
     * A sine is the wrong shape for a peak and it is worth being precise about why: it spends 41% of
     * every cycle inside the top fifth of its range, so whatever amplitude it is drawn at, each hump
     * reads as a broad rounded dome and the crest has no point to it. Brandon asked for sharper
     * mountains (2026-08-11) and this is where that lives — the sheet's HEIGHT is the band's energy
     * and is not ours to touch here, so sharpness has to come from the shape at a given height.
     *
     * `sign(s) * |s|^1.8` narrows the crest without moving anything the surrounding geometry is
     * built on: 0 and +/-1 are exact fixed points, so the wave still reaches full amplitude at the
     * sine's peak and still crosses the centre line in exactly the same places, and the humps do not
     * move. It is odd, so the sheet still swings as far up as down about the shared centre line. It
     * never exceeds the sine, so the amplitude budget (rails, inset, edge taper) is untouched — the
     * ribbon gets sharper, not bigger. The measured effect is 31% of the cycle in the top fifth
     * instead of 41%, and the valleys correspondingly wider and flatter.
     *
     * Applied inside [wave] rather than at the crest call site, so the BOTTOM edge is shaped by the
     * same curve half a cycle later and the sheet stays one object with a rippling thickness. Shape
     * only one edge and the body flaps open and shut instead.
     *
     * 1.8 is bounded from both sides by test: below it the crest is not measurably sharper than the
     * sine, and it may never push a sample past the sine it shapes.
     */
    const val CREST_SHARPNESS = 1.8f

    fun crestShape(s: Float): Float {
        val shaped = abs(s).pow(CREST_SHARPNESS)
        return if (s < 0f) -shaped else shaped
    }

    /**
     * Samples along one sheet edge. The original stepped a fixed 4 px, which on its 160dp pill was
     * about 120 samples; on a 1080 px-wide bar the same rule would be 270 per edge, per sheet, per
     * voice, every frame. Capped, therefore — and floored so a narrow ribbon stays smooth.
     */
    fun sampleCount(width: Float): Int = (width / 4f).toInt().coerceIn(24, 120)

    fun crestStrokePx(height: Float, minPx: Float): Float =
        maxOf(minPx, height * CREST_STROKE_FRACTION)

    fun bloomStrokePx(height: Float, minPx: Float): Float =
        maxOf(minPx, height * BLOOM_STROKE_FRACTION)

    /** Half the crest stroke is folded in so the BRIGHT line, not just the path, stays inside. */
    fun insetPx(height: Float, crestStrokePx: Float): Float =
        height * EDGE_INSET_FRACTION + crestStrokePx / 2f

    /** 1 through the middle, easing to 0 at both edges over [TAPER_FRACTION] of the width. */
    fun edgeTaper(progress: Float): Float {
        val p = progress.coerceIn(0f, 1f)
        val d = minOf(p, 1f - p)
        if (d >= TAPER_FRACTION) return 1f
        return 0.5f - 0.5f * cos((PI * d / TAPER_FRACTION).toFloat())
    }

    /**
     * The highest y any ink may reach- the box interior, inset so the CREST stroke's own half-width
     * stays inside too.
     *
     * Capped at the half-height so it can never cross [railBottom]. An inset larger than half the
     * box (a 4 px-tall canvas mid-layout, where the 1dp crest minimum dominates) would otherwise
     * hand `coerceIn` an inverted range, and `coerceIn` THROWS on one — the original carried the
     * same warning about its cap apexes.
     */
    fun railTop(height: Float, insetPx: Float): Float = insetPx.coerceIn(0f, height / 2f)

    /** ...and the lowest, mirrored, so the pair is always a valid range. */
    fun railBottom(height: Float, insetPx: Float): Float = height - railTop(height, insetPx)

    /**
     * The line this sheet's wave is symmetric about- the BOX CENTRE, plus this sheet's own slow
     * vertical [drift] (C7).
     *
     * All four sheets and both speakers share this one line, which is what Brandon chose over
     * stacking them in lanes ("all share one centre line") — they overlap and weave through each
     * other, and the alpha ladder is what keeps the stack readable through the overlap.
     *
     * The centre, not a ground line at 55% of the box: a wave that goes as far up as it goes down
     * uses its box evenly BY CONSTRUCTION. A ground line existed to stop a one-sided ridge wasting
     * the bottom half of the box; against a bipolar wave it would only push the ink off centre.
     *
     * Clamped into the rails so a degenerate canvas cannot put the line outside its own box; at any
     * shipped size the clamp is inert (0.5 +/- the drift's 0.0675 sits well inside 0.065..0.935).
     */
    fun centreY(height: Float, insetPx: Float, drift: Float): Float =
        (height / 2f + drift).coerceIn(railTop(height, insetPx), railBottom(height, insetPx))

    /**
     * This sheet's excursion either side of its centre line, in px- ITS OWN BAND'S energy times a
     * per-layer depth taper, so band j's loudness IS how high ribbon j goes (C7).
     *
     * Higher sheets are shallower, which is what reads as distance. Takes the [drive] again after
     * the intermediate design took it away: back then a crest was shared by eight mountains and all
     * four bands, so no single band could scale it. One band owns the whole sheet now.
     */
    fun amplitude(height: Float, drive: Float, layer: Int): Float =
        height * AMP_FRACTION * drive * (1f - layer * 0.10f)

    /**
     * Slow vertical wander, out of step per layer, so the sheets weave THROUGH each other rather
     * than sliding as a stack of parallel ribbons.
     *
     * The reference's ladder direction, `layer - (SHEETS-1)/2`: the near sheet wanders UP and the
     * far ones down. The intermediate design inverted it to keep its one-sided mountains off the
     * ceiling, and that reason is gone — a bipolar wave reaches equally far up and down, so a sheet
     * pays the same either way. Everything the drift test measures is relative magnitudes and
     * signs, so both directions satisfy it; this one is simply the reference's.
     *
     * It does decide which END of the box is the tight one, because the tallest sheet is layer 0.
     * This way up the top is the tight edge- sheet 0 carries the full [AMP_FRACTION] AND the full
     * up-drift, so its worst-case peak lands `0.5 - 0.0675 - 0.35 = 0.0825` of the box above the
     * middle, 0.0175 clear of the 0.065 top rail; the bottom, where the shallower sheets 2 and 3
     * live, keeps more spare than that. Inverting the ladder would swap which edge is tight and buy
     * a little more amplitude, but the amplitude is not what makes this ribbon read (see [frequency]),
     * so it stays the reference's way up.
     */
    fun drift(height: Float, phase: Float, layer: Int, driftBias: Float): Float =
        (layer - (SHEETS - 1) / 2f) * height * DRIFT_FRACTION *
            (1f + 0.5f * sin(phase * DRIFT_RATE + layer + driftBias))

    /**
     * The crest- a smooth [wave] of 4-5 humps rising ABOVE the centre line and falling equally far
     * BELOW it (screen y decreases upward), scaled by this sheet's [amp], which is its band's
     * energy. "Vertical in the upwards direction and the bottom as well" (Brandon, 2026-07-27).
     *
     * NOTHING CLIPS HERE. The excursion is `wave * amp * taper`, linear in the band energy all the
     * way to full drive, and the `coerceIn` is a box GUARANTEE rather than a shaper- [AMP_FRACTION]
     * is budgeted so a full-drive peak at the worst drift phase still lands inside [railTop] on
     * every layer. A summit cut off flat is the defect this geometry was rewritten twice to remove;
     * a test measures the fraction of the width sitting exactly on a rail and requires zero.
     *
     * The [edgeTaper] is a MULTIPLIER on the excursion, so both ends converge smoothly ONTO the
     * centre line and the ribbon terminates in a point rather than being squeezed flat against a
     * rail.
     */
    fun crestY(
        progress: Float,
        height: Float,
        insetPx: Float,
        amp: Float,
        phase: Float,
        layer: Int,
        travel: Float,
        phaseBias: Float,
        drift: Float,
    ): Float {
        val centre = centreY(height, insetPx, drift)
        val y = centre - edgeTaper(progress) * amp * wave(progress, phase, layer, travel, phaseBias)
        return y.coerceIn(railTop(height, insetPx), railBottom(height, insetPx))
    }

    /**
     * The bottom edge- the same wave half a cycle behind ([BOTTOM_PHASE_SHIFT]) at
     * [BOTTOM_AMP_SCALE] of the amplitude, hung under the centre line by a small constant offset.
     * That shift is what makes the body's thickness UNDULATE with the wave instead of tracking the
     * crest at a constant gap, and the offset is what keeps a flat, silent ribbon a visible sliver
     * rather than a degenerate zero-area path.
     *
     * Tapered as ONE quantity, offset included, so the belly meets the crest exactly on the centre
     * line at both ends and the sheet closes to a point. Tapering only the wave term would leave
     * the sliver open at the edges — a blunt end, and a hairline of body outside the taper.
     */
    fun bottomY(
        progress: Float,
        height: Float,
        insetPx: Float,
        amp: Float,
        phase: Float,
        layer: Int,
        travel: Float,
        phaseBias: Float,
        drift: Float,
    ): Float {
        val centre = centreY(height, insetPx, drift)
        val belly = height * BOTTOM_OFFSET_FRACTION +
            amp * BOTTOM_AMP_SCALE * wave(progress, phase, layer, travel, phaseBias + BOTTOM_PHASE_SHIFT)
        val y = centre + edgeTaper(progress) * belly
        return y.coerceIn(railTop(height, insetPx), railBottom(height, insetPx))
    }

    // ----------------------------------------------------------------- alpha

    /**
     * The depth stack (Compose takes 0..1; the port set 0..255 ints on a Paint). Body translucent,
     * bloom faintest, crest brightest, all three receding with depth. This ladder is the ONLY
     * source of depth now that C2 has removed the gradient — flattening it flattens the ribbon.
     *
     * ALL THREE NUMERATORS WERE CUT for the occlusion pass (measured on device: a single voice's
     * four sheets covered 99.24% of the field's centre band, fill+bloom alone 74%). The fix was two
     * things at once- lower alphas AND additive compositing on the light passes:
     *
     *   FILL 70 -> 40. The body is the only pass that OCCLUDES (it stays SrcOver), so it carries
     *   the coverage budget: per-sheet alpha 40/255 = 0.157 <= 0.16, and the four fills plus the
     *   four blooms, composited as if all were normal SrcOver at one shared point, occlude 51.7% of
     *   the field (a test bounds it <= 55%). Fills-only occlusion is 38.7%- the field reads through.
     *
     *   BLOOM 34 -> 19 and CREST 200 -> 120, BOTH now drawn with BlendMode.Plus (see the draw
     *   loop). Additive light BRIGHTENS where sheets overlap instead of stacking toward opaque, so
     *   these passes never dim the field- they add to it. The catch is white-out: four additive
     *   crests crossing sum their brightness, so the numerators are held low enough that
     *   `sum over sheets of (crestAlpha + bloomAlpha)` = 1.55 stays under 1.6 (pinned by a test).
     *   Crest fell further than the "~140" first estimate precisely to keep that sum plausible;
     *   additive passes read bright at a lower alpha, so nothing is lost by it.
     */
    fun fillAlpha(layer: Int): Float = 40f * (1f - layer * 0.18f) / 255f

    fun bloomAlpha(layer: Int): Float = 19f * (1f - layer * 0.15f) / 255f

    fun crestAlpha(layer: Int): Float = 120f * (1f - layer * 0.2f) / 255f

    // ----------------------------------------------------- two-voice weaving

    /**
     * C1, and the load-bearing one- what keeps two ribbons in one container tellable apart when
     * Brandon and the model are talking at the SAME TIME.
     *
     * WHICH WAY THEY TRAVEL. The human's waves run left to right and the AI's right to left, +1 and
     * -1 into [wave]'s time term, forever. Colour cannot do this job any more (C2 gave hue to the
     * band), and direction is a better answer than colour ever was: it needs no legend, it survives
     * both voices being at the same drive at the same instant on the same centre line, and it gets
     * CLEARER the longer you look instead of relying on one glance.
     *
     * Note it costs the two voices nothing individually- a lone ribbon of either identity is the
     * same wave at the same speed, just aimed the other way, so neither reads as the odd one out
     * and neither jumps when the other joins or leaves.
     */
    fun travelDirection(speaker: AuroraSpeaker): Float = when (speaker) {
        AuroraSpeaker.HUMAN -> 1f
        AuroraSpeaker.AI -> -1f
    }

    /**
     * KEPT, not dropped, though [travelDirection] is now the primary separator.
     *
     * Two waves travelling in opposite directions are identical whenever their phases coincide, and
     * with no bias they coincide at phase ZERO — the first frame of every fresh composition, every
     * resume and every wake from the idle park, i.e. the single most-observed instant this ribbon
     * has. A pi bias makes that instant the maximally separated one instead, at the cost of nothing
     * but a constant. They still pass through each other periodically as the two clocks scissor,
     * which is the weave, and [driftBias] holds them apart vertically when they do.
     */
    fun phaseBias(speaker: AuroraSpeaker): Float = when (speaker) {
        AuroraSpeaker.HUMAN -> 0f
        AuroraSpeaker.AI -> PI.toFloat()
    }

    /** Offsets only the drift's timing, never its mean, so neither voice sits off centre. */
    fun driftBias(speaker: AuroraSpeaker): Float = when (speaker) {
        AuroraSpeaker.HUMAN -> 0f
        AuroraSpeaker.AI -> 1.7f
    }

    // -------------------------------------------------------------- envelope

    /** Blend factor an exponential approach with time constant [tauSec] applies over [dtSec]. */
    fun blendAt(dtSec: Float, tauSec: Float): Float = 1f - exp(-dtSec / tauSec)

    /**
     * One step of an exponential approach toward [target] (C3).
     *
     * Composes exactly- stepping twice over dt/2 lands where one step over dt lands, which is
     * precisely the property the per-frame original did not have.
     */
    fun approach(current: Float, target: Float, dtSec: Float, tauSec: Float): Float {
        if (dtSec <= 0f) return current
        return current + (target - current) * blendAt(dtSec, tauSec)
    }
}

/**
 * Walk one sheet- forward along the crest, then back along the bottom edge, exactly as the path is
 * built. The renderer feeds the points into a Path; the tests assert none of them leaves the
 * canvas. Sharing the one traversal is what stops those two drifting apart.
 *
 * `inline` so the renderer pays nothing for the two lambdas.
 */
internal inline fun forEachSheetPoint(
    width: Float,
    height: Float,
    steps: Int,
    insetPx: Float,
    amp: Float,
    phase: Float,
    layer: Int,
    travel: Float,
    phaseBias: Float,
    drift: Float,
    onCrest: (x: Float, y: Float, first: Boolean) -> Unit,
    onBottom: (x: Float, y: Float) -> Unit,
) {
    val n = steps.coerceAtLeast(1)
    for (i in 0..n) {
        val progress = i.toFloat() / n
        onCrest(
            progress * width,
            Aurora.crestY(progress, height, insetPx, amp, phase, layer, travel, phaseBias, drift),
            i == 0,
        )
    }
    for (i in n downTo 0) {
        val progress = i.toFloat() / n
        onBottom(
            progress * width,
            Aurora.bottomY(progress, height, insetPx, amp, phase, layer, travel, phaseBias, drift),
        )
    }
}

/**
 * One voice's four sheet levels and their asymmetric envelopes.
 *
 * Levels start at ZERO, not at the baseline- a ribbon that has never been fed costs no frames and
 * shows nothing, and the first voice to arrive fades it in instead of popping it on.
 */
internal class AuroraEnvelope(restLevel: Float = Aurora.BASELINE) {

    /**
     * Where a PRESENT voice settles (see [AuroraWaveform]'s `restLevel`). Coerced once, here, so no
     * call site can hand the sheets a negative floor- a sheet approaching one would report itself
     * settled while sitting BELOW the centre line, i.e. an inverted ribbon that never corrects.
     */
    private val restLevel = restLevel.coerceIn(0f, 1f)

    val levels = FloatArray(Aurora.SHEETS)

    /** Whether a voice was supplied at the last step (an arrival or a departure needs frames). */
    var present: Boolean = false
        private set

    /** Whether every sheet has reached what it is resting at, so nothing more will change. */
    var resting: Boolean = true
        private set

    fun step(dtSec: Float, voice: AuroraVoice?) {
        present = voice != null
        // Present but silent rests at [restLevel]; gone goes FLAT regardless. A stream that ends
        // has to clear the screen, not park a permanent stub on it — which is a DIFFERENT thing
        // from a silent passage inside a stream that is still running, and the reason the floor is
        // a parameter rather than a constant.
        val floor = if (voice == null) 0f else restLevel
        var settled = true
        for (j in levels.indices) {
            // THE perceptual lift — quiet-but-present bands still animate. This is the one and only
            // stage allowed to bend energy into height ([Aurora.DISPLAY_GAMMA]); everything that
            // feeds a voice in here hands over LINEAR normalised energy. The floor goes UNDER it as
            // a blend rather than over it as a clamp ([Aurora.restBlend]) — a clamp made every band
            // below the floor draw the identical sheet.
            val target = if (voice == null) 0f else Aurora.restBlend(restLevel, Aurora.displayCurve(voice.band(j)))
            val tau = when {
                target > levels[j] -> Aurora.ATTACK_TAU_SEC
                // A DEPARTING voice retires on the single global constant instead of the four
                // per-band ones, so the ribbon leaves as one object rather than unravelling sheet
                // by sheet over a third of a second.
                voice == null -> Aurora.RELEASE_TAU_SEC
                else -> Aurora.BAND_RELEASE_TAUS_SEC[j]
            }
            levels[j] = Aurora.approach(levels[j], target, dtSec, tau)
            if (abs(levels[j] - floor) > Aurora.SETTLE_EPS) settled = false
        }
        resting = settled
    }
}

/**
 * The animation state behind one [AuroraWaveform]- the shared phase plus one envelope per speaker.
 *
 * Envelopes are keyed by SPEAKER, never by position in the voices list. Keyed by index, an AI
 * joining ahead of an already-speaking human would inherit the human's saturated envelope and the
 * two ribbons would swap levels mid-sentence.
 */
internal class AuroraEngine(restLevel: Float = Aurora.BASELINE) {

    var phaseRad: Float = 0f
        private set

    private val envelopes = Array(AuroraSpeaker.entries.size) { AuroraEnvelope(restLevel) }

    fun envelope(speaker: AuroraSpeaker): AuroraEnvelope = envelopes[speaker.ordinal]

    fun step(dtSec: Float, voices: List<AuroraVoice>) {
        phaseRad = (phaseRad + Aurora.PHASE_RATE_PER_SEC * dtSec) % Aurora.PHASE_WRAP_RAD
        val speakers = AuroraSpeaker.entries
        for (i in speakers.indices) {
            val speaker = speakers[i]
            envelopes[speaker.ordinal].step(dtSec, find(voices, speaker))
        }
    }

    /**
     * Whether the next frame could look any different from this one (C6).
     *
     * Three independent reasons, and all three are load bearing- "somebody is audible" alone would
     * leave a departing ribbon frozen on screen forever, because the thing that has to animate is
     * precisely the absence of a voice.
     */
    fun needsFrames(voices: List<AuroraVoice>): Boolean {
        val speakers = AuroraSpeaker.entries
        for (i in speakers.indices) {
            val speaker = speakers[i]
            val voice = find(voices, speaker)
            val envelope = envelopes[speaker.ordinal]
            if ((voice != null) != envelope.present) return true    // arriving or departing
            if (!envelope.resting) return true                      // still moving
            if (voice != null && voice.hasEnergy()) return true      // audible
        }
        return false
    }

    /** Indexed loop- `firstOrNull { }` would allocate a capturing lambda on every frame. */
    private fun find(voices: List<AuroraVoice>, speaker: AuroraSpeaker): AuroraVoice? {
        for (i in voices.indices) if (voices[i].speaker == speaker) return voices[i]
        return null
    }
}
