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
import com.aiblackbox.portal.ui.theme.WaveBlue
import kotlinx.coroutines.flow.first
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.floor
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Aurora waveform — layered translucent wave SHEETS, one identity colour per speaker, drawn into
 * whatever box the caller sizes it to, with nothing behind them.
 *
 * Each sheet is a closed path between a crest ridge and a half-step-shifted bottom ridge, painted
 * three times: a translucent body, a wide low-alpha bloom stroke and a bright thin crest line. That
 * three-pass stack is where the depth comes from and it is the whole look — do not collapse it to
 * one pass.
 *
 * Ported from whisper-everywhere's `BarWaveformView` (2026-07-18), which despite its name drew no
 * bars. Every wave constant below is that view's, measured and tuned on device. The EIGHT departures
 * are deliberate, each is called out at the code it affects, and none of them should be "restored
 * to parity":
 *
 *   C1 One amplitude in, one ribbon out -> a LIST of voices. BlackBox shows the human's mic and
 *      the AI's playback in the SAME container at the SAME time, and they have to stay telling
 *      apart. See [AuroraSpeaker], [Aurora.speakerPeakShift] and [Aurora.driftBias].
 *   C2 The six-stop red->magenta->purple->blue->navy gradient is DELETED. Red is the human, blue
 *      is the AI, solid. Depth comes from the alpha ladder that was always there, not from hue.
 *      (Brandon, 2026-07-26- "the gradient in the middle I never really liked".)
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
 *   C7 The crest is a STANDING RIDGE OF BAND-DRIVEN MOUNTAINS, not a travelling sine. This is the
 *      largest divergence and the one most likely to be "restored" by somebody diffing against the
 *      reference, so: the original crest was `sin(progress * PI * freq + phase)` with freq 1.15..2.8
 *      and a phase advancing at 7.8 rad/s. That is TWO things Brandon rejected on device
 *      (2026-07-26- "the ribbons are moving from right to left across the screen, which we don't
 *      necessarily need... right now it seems pretty flat"). ONE- 1.15..2.8 half-humps across a
 *      whole 1000 px bar is a single broad arc however tall it is, so the ribbon read as flat at a
 *      MEASURED 62% of the available half-height; the fix is more PEAKS, not more gain, and nothing
 *      here should ever be "fixed" by raising [Aurora.AMP_FRACTION] or the analyser's gains. TWO-
 *      the phase term inside the spatial sine made it a travelling wave at 1..2.3 cycles/sec in -x,
 *      which is the scroll he saw. So the crest is now [Aurora.ridge]- [Aurora.PEAKS] mountains at
 *      FIXED x positions whose heights are the four band energies, interpolated with a
 *      partition-of-unity raised cosine. Time enters the geometry through the slow vertical
 *      [Aurora.drift] and NOWHERE else. Do not reintroduce a time term into any x-dependent
 *      expression; a test samples the ridge half a second apart and fails if the peaks move.
 *   C8 The range stands on a GROUND LINE at 55% of the box and is bounded by the box, where C7
 *      left it standing on the box CENTRE and bounded by a clamp band that closed on that centre.
 *      Both of C7's vertical defects came out of that one arrangement, and this is the fix for
 *      both. ONE- a one-sided ridge on the centre line has half a box to grow into and wastes the
 *      other half, so the ink sat 7-12% from the top and 20-30% clear of the bottom (measured;
 *      the optical centre rode to 43.6% of the height while speaking, which is ~34dp of dead box
 *      under a speaking ribbon on the voice screen). TWO- with only half a box above it the front
 *      sheet SATURATED: `crestY` clamped against `cy - halfHeight`, and 18.6% of that sheet's
 *      width was a flat mesa at the analyser's steady-speech drive, 44.9% on a shout (and 5.4-7.8%
 *      of the belly was crushed against the closing envelope at EVERY drive). Three changes, and
 *      the ribbon is measured after each in AuroraWaveformTest- the ground line drops to
 *      [Aurora.GROUND_FRACTION]; [Aurora.edgeTaper] becomes a MULTIPLIER on both excursions rather
 *      than a closing clamp band, so the ends taper onto the ground line instead of being crushed
 *      against a rail; and the per-layer [Aurora.drift] ladder is INVERTED so the tall near sheet
 *      wanders down and the short far sheets up. Nothing clips at any drive now, the mountains are
 *      TALLER than they drew before (the front sheet's summit is 46.0% of the height at full drive
 *      against a railed 38.3%, and 35.0% at steady speech against 32.6%), and
 *      the ink spans 40..75% of the box at rest, 17..86% at steady speech and 9..90% on a shout.
 *      The lever that is NOT connected to any of this is the analyser's gain — as in C7, a flat
 *      ribbon is never fixed by turning the audio up.
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
                        // The LOUDEST band, not this sheet's own- every sheet's ridge is built
                        // from all four bands now (C7), so culling on band `layer` alone would
                        // retire a sheet whose mountains are in fact standing up on the bass.
                        if (!Aurora.paintsSheet(envelope.present, envelope.loudest)) continue

                        val colour = auroraSpeakerColor(speaker)
                        val amp = Aurora.amplitude(h, layer)
                        val drift = Aurora.drift(h, nowPhase, layer, Aurora.driftBias(speaker))
                        // Constant in time (C7)- the ONLY thing `nowPhase` may reach from here is
                        // the vertical drift above.
                        val peakShift = Aurora.peakShift(speaker, layer)

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
                            levels = envelope.levels,
                            layer = layer,
                            peakShift = peakShift,
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
                            // Closing the crest back along a second, half-step-shifted ridge is what
                            // makes a rippling silk BODY whose thickness itself undulates, rather
                            // than a stroked line.
                            onBottom = { x, y -> paths.sheet.lineTo(x, y) },
                        )
                        paths.sheet.close()

                        drawPath(paths.sheet, colour, alpha = Aurora.fillAlpha(layer))
                        drawPath(paths.crest, colour, alpha = Aurora.bloomAlpha(layer), style = bloomStroke)
                        drawPath(paths.crest, colour, alpha = Aurora.crestAlpha(layer), style = crestStroke)
                    }
                }
            }
        },
    )
}

/**
 * The speaker's identity colour (C2). Full alpha- transparency is the per-pass alpha ladder's job,
 * and baking it in here would multiply twice.
 */
internal fun auroraSpeakerColor(speaker: AuroraSpeaker): Color = when (speaker) {
    AuroraSpeaker.HUMAN -> BbxAccent
    AuroraSpeaker.AI -> WaveBlue
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

    /** Per-band release- bass lingers, sibilance vanishes. Was [0.055, 0.085, 0.115, 0.16]/frame. */
    val BAND_RELEASE_TAUS_SEC = floatArrayOf(0.295f, 0.188f, 0.137f, 0.096f)

    /**
     * Was `phase += 0.13f` per frame at 60 fps. Since C7 the phase has exactly ONE consumer, the
     * vertical [drift]- it is the clock that keeps a ribbon under steady audio alive without
     * anything sliding sideways, and it is kept at the original rate so the wander is the wander
     * that was tuned on device.
     */
    const val PHASE_RATE_PER_SEC = 7.8f

    /**
     * Phase accumulates for as long as a screen is open, so it has to wrap or it loses float
     * precision. It may only wrap where every consumer of it is back in phase. That used to mean
     * the sheet multipliers AND the drift rate; C7 deleted the multipliers, so the surviving
     * consumer is [DRIFT_RATE] = 0.35 alone. The period stays 2pi/0.05 (about 16 s) rather than
     * tightening to 2pi/0.35, because 0.35 is exactly 7 x 0.05 and this is therefore still a whole
     * number of drift turns — the wrap remains invisible, and it stays safe if a second consumer on
     * the same 0.05 grid is ever added back. A test pins it.
     */
    val PHASE_WRAP_RAD = (2.0 * PI / 0.05).toFloat()

    /** Within this of its resting value an envelope counts as settled (drives the idle park). */
    const val SETTLE_EPS = 0.002f

    /**
     * A band below this cannot lift a sheet clear of the DEFAULT resting baseline, so it does not
     * justify a frame- `sqrt(ENERGY_FLOOR)` sits just under BASELINE + SETTLE_EPS.
     *
     * A call site resting LOWER than the baseline is still parked correctly, just by the other two
     * conditions- a sheet does not count as `resting` until it is within SETTLE_EPS of ITS OWN
     * floor, so the loop keeps frames while a sub-floor band is still lifting it.
     */
    const val ENERGY_FLOOR = 0.0038f

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

    /** Was 2dp on a 56dp pill. */
    const val CREST_STROKE_FRACTION = 0.036f

    /** Was 9dp on a 56dp pill. */
    const val BLOOM_STROKE_FRACTION = 0.16f

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
     * Full-scale crest excursion above the ground line, as a fraction of the height.
     *
     * LIVE since C8 and no longer the inert knob the old comment here warned about- the crest is
     * bounded by the box now, not by a clamp band it always exceeded, so this constant is exactly
     * how tall a full-drive mountain draws. Raising it eats the headroom C8 bought (the budget is
     * `GROUND_FRACTION - EDGE_INSET_FRACTION - CREST_STROKE_FRACTION/2 - the far sheet's worst
     * up-drift`, and the ladder's TOP rung is not the binding one — see [drift]), so a change here
     * has to be re-measured against the no-clip test rather than eyeballed.
     */
    private const val AMP_FRACTION = 0.46f

    /**
     * Where the range STANDS, as a fraction of the height, before this sheet's [drift] (C8).
     *
     * A one-sided ridge grows upward, so a ground line on the box's centre gives the crest half a
     * box to grow into and wastes the other half: measured over a whole drift turn across all four
     * sheets and both speakers, the ink used to span 33.9%..69.7% of the box at rest, 11.7%..75.5%
     * at speech and 7.2%..80.3% on a shout — 20-30% of the box permanently empty underneath
     * against 7-12% of headroom above, and the front sheet railed against the top for 18.6% of its
     * width at speech and 44.9% at a shout. Dropping the line to 55% pays the headroom back out of
     * the dead space: 39.6%..75.0%, 17.0%..86.3% and 9.3%..90.1%, nothing clipped at any drive.
     *
     * NOT lower, though the top rail would take it- the RESTING ribbon is a flat sliver sitting on
     * this line, so every centimetre the line drops is a centimetre the idle player bar's ribbon
     * drops with it. 0.55 is the most the loud states could get before the quiet one starts to
     * read as hanging off the bottom of its box (measured: the flat 40dp bar's band centres 2.9dp
     * below its old centre line, against 6dp at 0.64). A test pins both ends of that trade.
     */
    private const val GROUND_FRACTION = 0.55f

    /**
     * HALVED from the original's 0.7 (C7). Its bottom edge was a SINE about the centre line, so
     * half of that 0.7 sat on the crest's side and the mean downward displacement was ~0.35 of the
     * amplitude; the ridge is one-sided, so keeping 0.7 would have doubled the body's weight and
     * with it the accumulated fill alpha the particle field has to show through.
     *
     * It is also half of what keeps the ink CENTRED now that C8 has moved the ground line- the
     * crest reaches [AMP_FRACTION] up and the belly this much plus [BOTTOM_OFFSET_FRACTION] down,
     * and [GROUND_FRACTION] is placed so that the two, plus the drift, land the loud states within
     * a point or two of the box centre. Raising it swells the ribbon downward (re-check
     * [fillAlpha]'s accumulated coverage, which is the bill halving it paid) and lowering it lifts
     * the optical centre back up; either way the ground line has to move with it.
     */
    private const val BOTTOM_AMP_SCALE = 0.35f
    private const val BOTTOM_OFFSET_FRACTION = 0.055f
    private const val DRIFT_FRACTION = 0.06f
    private const val DRIFT_RATE = 0.35f

    // ------------------------------------------------------- the ridge (C7)

    /**
     * Mountains across the width. EIGHT, because four bands cannot make a range on their own- four
     * humps across a 1000 px bar is still close to the single broad arc C7 is replacing, and the
     * brief was "enough peaks to read as a RANGE". Mirroring the band sequence ([PEAK_BANDS])
     * doubles the count without inventing data the analyser does not have.
     */
    const val PEAKS = 8

    /**
     * Which band drives each mountain, left to right, before the per-layer rotation.
     *
     * Palindromic, so every band owns a matched PAIR of mountains — far easier to see fire than one
     * peak somewhere off-centre — and so the front sheet reads as composed rather than accidental.
     *
     * HIGH at the edges and LOW in the middle, not the other way round, because [edgeTaper] crushes
     * the outermost mountains to nothing: the band with the most energy and the most perceptual
     * weight has to sit where the ribbon is tallest, and sibilance (brief, quiet, and absent
     * entirely on an 8 kHz stream where the analyser drops the probe) is the band that can afford
     * to live under the taper.
     */
    private val PEAK_BANDS = intArrayOf(3, 2, 1, 0, 0, 1, 2, 3)

    /**
     * How deep the saddle between two mountains sits, as a fraction of the SMALLER of the two.
     *
     * `min`, never the mean- a tall neighbour must not lift the saddle above a quiet band's own
     * mountain, or that band stops owning a peak at all the moment something louder sits next to
     * it, which is precisely the thing this redesign exists to guarantee. Being a fraction of a
     * neighbour rather than a constant is also what keeps a silent ribbon flat instead of corrugated.
     */
    private const val VALLEY_FLOOR = 0.35f

    /**
     * Per-layer lattice skew, in HALF-steps of the peak spacing.
     *
     * Without it, four sheets carrying similar band energies draw the same mountains at the same x
     * and the depth stack collapses into one thick line — the failure the layer ladders exist to
     * prevent. Deliberately NOT 0.5 (that is the two-voice separation's step, see
     * [speakerPeakShift]) and deliberately small- 0.28 half-steps puts sheet 3 about 42% of a
     * spacing off sheet 0, enough to see the sheets as separate ridges and not so much that they
     * stop reading as the same range.
     */
    private const val LAYER_PEAK_SKEW = 0.28f

    /**
     * The bottom edge's lattice runs one HALF-step behind the crest's, so its mountains sit under
     * the crest's saddles. That is what makes the body's THICKNESS undulate — the original got the
     * same effect from a bottom sine at a different frequency and phase, and without it the sheet
     * is a constant-width band that reads as a stroked line rather than silk.
     */
    private const val BOTTOM_PEAK_SHIFT = 1f

    /** Spacing between a mountain and the saddle beside it, in progress units. */
    private const val HALF_STEP = 0.5f / PEAKS

    /**
     * Band driving mountain [peak] of sheet [layer]. Rotating the SEQUENCE (not the band values)
     * keeps every band on exactly two mountains in every sheet while breaking the mirror symmetry
     * for the sheets behind the front one.
     */
    fun peakBand(layer: Int, peak: Int): Int =
        PEAK_BANDS[wrapPeak(peak + layer)]

    /** Where mountain [peak] sits across the width, 0..1, for a lattice shifted by [peakShift]. */
    fun peakProgress(peak: Int, peakShift: Float): Float =
        (2 * peak + 1 + peakShift) * HALF_STEP

    /**
     * Total lattice shift for one sheet of one speaker, in half-steps- the speaker's separation
     * plus the per-layer skew. Constant in TIME (C7): every term here is a compile-time ladder, so
     * a mountain owns its x position for as long as the ribbon is on screen.
     */
    fun peakShift(speaker: AuroraSpeaker, layer: Int): Float =
        speakerPeakShift(speaker) + layer * LAYER_PEAK_SKEW

    /**
     * The normalised crest shape at [progress], 0..[levels] max — a range of [PEAKS] mountains at
     * fixed positions whose heights ARE the band energies, with a saddle between each pair.
     *
     * Interpolation is a raised cosine between the two bracketing control points, which is a
     * PARTITION OF UNITY (`w(t) + w(1-t) == 1`): the curve passes exactly through every control
     * point, is flat-tangent at each one, and is MONOTONE in between. That last property is why it
     * is not Catmull-Rom — a spline overshoots below a saddle and would cut a visible notch through
     * the centre line between two loud mountains, which against the soft bloom pass reads as a
     * tear. Nothing here allocates; it is called ~242 times per sheet per frame.
     */
    fun ridge(progress: Float, levels: FloatArray, layer: Int, peakShift: Float): Float {
        // Control points alternate mountain (odd k) / saddle (even k) along the half-step lattice.
        // The lattice is INFINITE and wrapped by [wrapPeak] rather than clipped to 0..1, so a
        // fractional shift slides it without opening a gap at either edge.
        val u = progress / HALF_STEP - peakShift
        val k0 = floor(u)
        val t = u - k0
        val i0 = k0.toInt()
        val lo = controlPoint(i0, levels, layer)
        val hi = controlPoint(i0 + 1, levels, layer)
        val w = 0.5f - 0.5f * cos((PI * t).toFloat())
        return lo + (hi - lo) * w
    }

    /** Height of lattice control point [k]: a band energy on the odd ones, a saddle on the even. */
    private fun controlPoint(k: Int, levels: FloatArray, layer: Int): Float {
        if (k and 1 != 0) return peakHeight(k, levels, layer)
        // Saddles are relative to the SHORTER neighbour so a quiet band keeps its mountain.
        return VALLEY_FLOOR * minOf(
            peakHeight(k - 1, levels, layer),
            peakHeight(k + 1, levels, layer),
        )
    }

    /** Height of the mountain at ODD lattice index [k]. */
    private fun peakHeight(k: Int, levels: FloatArray, layer: Int): Float {
        // Arithmetic shift, i.e. FLOOR division- plain `/ 2` truncates toward zero and would make
        // the lattice skip an ordinal at the origin, which a fractional [peakShift] walks straight
        // into at the left edge.
        val peak = (k - 1) shr 1
        val band = peakBand(layer, peak)
        return if (band < levels.size) levels[band] else 0f
    }

    private fun wrapPeak(peak: Int): Int {
        val m = peak % PEAKS
        return if (m < 0) m + PEAKS else m
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
     * Where this sheet's range STANDS at the moment- [GROUND_FRACTION] of the box, plus this
     * sheet's slow vertical [drift] (C8).
     *
     * The whole sheet is built off this: the crest rises from it, the belly hangs under it, and
     * both ends taper back ONTO it. Clamped into the rails so a degenerate canvas cannot put the
     * line outside its own box; at any shipped size the clamp is inert (0.55 +/- the drift's 0.135
     * sits well inside 0.072..0.928).
     */
    fun groundY(height: Float, insetPx: Float, drift: Float): Float =
        (height * GROUND_FRACTION + drift).coerceIn(railTop(height, insetPx), railBottom(height, insetPx))

    /**
     * FULL-SCALE excursion for a sheet- higher sheets are shallower, which is what reads as
     * distance. The per-mountain 0..1 comes from [ridge] now, so this no longer takes a drive
     * (C7): one band cannot scale a crest that eight mountains and four bands share.
     */
    fun amplitude(height: Float, layer: Int): Float =
        height * AMP_FRACTION * (1f - layer * 0.10f)

    /**
     * Slow vertical wander, out of step per layer, so the sheets weave THROUGH each other.
     *
     * The ladder RUNS THE OTHER WAY since C8- `(SHEETS-1)/2 - layer` rather than the reference's
     * `layer - (SHEETS-1)/2`, so the NEAR sheet wanders down and the FAR ones up. Same excursions,
     * same weave, same everything the drift test measures (it is all magnitudes and signs relative
     * to each other), but it decides which sheet spends the ribbon's headroom. The near sheet
     * carries the tallest mountains AND used to take the biggest push toward the ceiling, which is
     * the pair that produced the clipped mesas; sending it downward instead hands the headroom to
     * the far sheet, whose crest is 30% shorter and needs it least. It also reads better- a distant
     * range sits HIGHER and fainter in a landscape and a near one lower and brighter, so the depth
     * stack and the aerial perspective now agree instead of fighting.
     */
    fun drift(height: Float, phase: Float, layer: Int, driftBias: Float): Float =
        ((SHEETS - 1) / 2f - layer) * height * DRIFT_FRACTION *
            (1f + 0.5f * sin(phase * DRIFT_RATE + layer + driftBias))

    /**
     * The crest- mountains rise UPWARD off the ground line (screen y decreases), by the ridge
     * height at [progress] scaled by this sheet's full-scale [amp].
     *
     * One-sided on purpose. The old sine straddled the centre and spent half its excursion pushing
     * the crest DOWN through the body, which on a range of eight mountains would read as the
     * ribbon punching holes in itself; the bottom edge mirrors downward instead, so the sheet
     * SWELLS at a mountain and pinches at a saddle. The sheet is therefore asymmetric about its
     * ground line, which is why that line is NOT the box centre — see [GROUND_FRACTION].
     *
     * NOTHING CLIPS HERE ANY MORE (C8). The height a mountain draws is `ridge * amp * taper`,
     * linear in the band energy all the way to full drive, and the `coerceIn` is a box guarantee
     * rather than a shaper- the geometry is budgeted so a full-drive summit on the worst drift
     * phase still lands inside [railTop]. It used to bind constantly: the excursion was capped at
     * `minOf(amp, halfHeight)` against an envelope that closed on the box CENTRE, and layer 0's
     * drift pushed a loud mountain straight through it, so 18.6% of the front sheet's width sat on
     * the rail as a flat mesa at the analyser's steady-speech drive and 44.9% on a shout. A test
     * fails if any of that comes back.
     *
     * The [edgeTaper] is a MULTIPLIER on the excursion now, not a closing clamp band. Both ends
     * therefore converge smoothly ONTO the ground line instead of being squeezed against a rail
     * that ran out before they did — which is where a third of the old mesas actually were, and
     * why the ribbon used to end in a flattened wedge rather than a point.
     */
    fun crestY(
        progress: Float,
        height: Float,
        insetPx: Float,
        amp: Float,
        levels: FloatArray,
        layer: Int,
        peakShift: Float,
        drift: Float,
    ): Float {
        val ground = groundY(height, insetPx, drift)
        val y = ground - edgeTaper(progress) * ridge(progress, levels, layer, peakShift) * amp
        return y.coerceIn(railTop(height, insetPx), railBottom(height, insetPx))
    }

    /**
     * The bottom edge- the same ridge a half-step behind ([BOTTOM_PEAK_SHIFT]) and mirrored
     * downward, so the body's thickness undulates rather than tracking the crest at a constant gap.
     * The small constant offset is what keeps a flat, silent ribbon a visible sliver instead of a
     * degenerate zero-area path.
     *
     * Tapered as ONE quantity, offset included (C8), so the belly meets the crest exactly on the
     * ground line at both ends and the sheet closes to a point. Tapering only the ridge term would
     * leave the sliver open at the edges — a blunt end, and a hairline of body outside the taper.
     */
    fun bottomY(
        progress: Float,
        height: Float,
        insetPx: Float,
        amp: Float,
        levels: FloatArray,
        layer: Int,
        peakShift: Float,
        drift: Float,
    ): Float {
        val ground = groundY(height, insetPx, drift)
        val belly = height * BOTTOM_OFFSET_FRACTION +
            ridge(progress, levels, layer, peakShift + BOTTOM_PEAK_SHIFT) * amp * BOTTOM_AMP_SCALE
        val y = ground + edgeTaper(progress) * belly
        return y.coerceIn(railTop(height, insetPx), railBottom(height, insetPx))
    }

    // ----------------------------------------------------------------- alpha

    /**
     * The depth stack, unchanged from the original except for the units (it set 0..255 ints on a
     * Paint; Compose takes 0..1). Body translucent, bloom faintest, crest brightest, all three
     * receding with depth. This ladder is the ONLY source of depth now that C2 has removed the
     * gradient — flattening it flattens the ribbon.
     *
     * Note for device review- with TWO voices live the accumulated coverage roughly doubles where
     * their bodies overlap, and C7's one-sided crest makes the bodies BIGGER than the port's
     * bipolar sine did (a mountain silhouette is filled from its ridge down to the belly, where a
     * sine spent half its excursion on the other side of the centre line). [BOTTOM_AMP_SCALE] is
     * halved to pay most of that back and no single pass is anywhere near opaque, but this is the
     * thing to look at first if the field behind reads as dimmed on a loud two-voice moment.
     */
    fun fillAlpha(layer: Int): Float = 70f * (1f - layer * 0.18f) / 255f

    fun bloomAlpha(layer: Int): Float = 34f * (1f - layer * 0.15f) / 255f

    fun crestAlpha(layer: Int): Float = 200f * (1f - layer * 0.2f) / 255f

    // ----------------------------------------------------- two-voice weaving

    /**
     * C1- what keeps two ribbons in one container legible.
     *
     * Colour alone does not separate two sheets sitting on top of each other, so the AI's mountains
     * sit in the human's SADDLES- half a peak spacing, expressed in the ridge's own half-step units.
     * Where one voice crests the other is at its lowest, and they interleave instead of merging.
     *
     * SPATIAL, because C7 left nothing else available: this used to be a pi PHASE bias, which
     * separated two travelling waves by sliding one along in time — on a crest that no longer moves
     * a phase bias shifts nothing at all. A lattice offset is also better behaved for the ORDINARY
     * one-voice case, since both lattices stay centred on the container: a lone AI ribbon still
     * looks centred and identical in character to a lone human one, and neither jumps when the
     * other joins or leaves.
     *
     * LOOK DECISION- a half step (the maximum available separation) and 1.7 are awaiting Brandon's
     * review on device. They are safe to retune; making either pair EQUAL is what is not safe,
     * since that is the mush this exists to prevent.
     */
    fun speakerPeakShift(speaker: AuroraSpeaker): Float = when (speaker) {
        AuroraSpeaker.HUMAN -> 0f
        AuroraSpeaker.AI -> 1f
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
    levels: FloatArray,
    layer: Int,
    peakShift: Float,
    drift: Float,
    onCrest: (x: Float, y: Float, first: Boolean) -> Unit,
    onBottom: (x: Float, y: Float) -> Unit,
) {
    val n = steps.coerceAtLeast(1)
    for (i in 0..n) {
        val progress = i.toFloat() / n
        onCrest(
            progress * width,
            Aurora.crestY(progress, height, insetPx, amp, levels, layer, peakShift, drift),
            i == 0,
        )
    }
    for (i in n downTo 0) {
        val progress = i.toFloat() / n
        onBottom(
            progress * width,
            Aurora.bottomY(progress, height, insetPx, amp, levels, layer, peakShift, drift),
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

    /**
     * The loudest of the four levels — what the renderer's visibility cull consults, since every
     * sheet's ridge is built from all four bands (C7) and a sheet is therefore visible whenever
     * ANY band is up, not only its own.
     */
    var loudest: Float = 0f
        private set

    fun step(dtSec: Float, voice: AuroraVoice?) {
        present = voice != null
        // Present but silent rests at [restLevel]; gone goes FLAT regardless. A stream that ends
        // has to clear the screen, not park a permanent stub on it — which is a DIFFERENT thing
        // from a silent passage inside a stream that is still running, and the reason the floor is
        // a parameter rather than a constant.
        val floor = if (voice == null) 0f else restLevel
        var settled = true
        var peak = 0f
        for (j in levels.indices) {
            // sqrt is a perceptual lift- quiet-but-present bands still animate. Straight from the
            // original, where it was applied on the way in rather than here.
            val target = if (voice == null) 0f else maxOf(restLevel, sqrt(voice.band(j)))
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
            if (levels[j] > peak) peak = levels[j]
        }
        resting = settled
        loudest = peak
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
