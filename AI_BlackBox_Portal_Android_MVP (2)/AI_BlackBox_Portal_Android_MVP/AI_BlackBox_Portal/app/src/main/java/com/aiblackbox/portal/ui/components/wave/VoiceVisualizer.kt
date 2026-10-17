package com.aiblackbox.portal.ui.components.wave

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
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import com.aiblackbox.portal.ui.components.aurora.Aurora
import com.aiblackbox.portal.ui.components.aurora.AuroraBins
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import kotlinx.coroutines.flow.first

// =============================================================================
// VoiceVisualizer — THE composable every call site uses to draw voices.
//
// It replaces the three direct `AuroraWaveform(...)` call sites (the composer
// mic strip, the audio player bar, the voice screen). Given the selected style
// id, it resolves the module from [WaveformStyles] and dispatches:
//
//   - Aurora (the default, and any future SelfContained style) is a whole
//     composable that owns its own engine/frame loop — VoiceVisualizer just
//     forwards the same four arguments the old call sites passed AuroraWaveform,
//     so the default stays PIXEL-IDENTICAL to today.
//
//   - Every Draw style shares ONE frame loop, feed and canvas, owned here in
//     [WaveformDrawHost]. That loop is the same shape AuroraWaveform's is —
//     time-based dt, a monotonic tick that invalidates only the draw phase, an
//     idle park gated on the renderer's needsFrames, and a lifecycle gate — so
//     every Draw style, Classic included, inherits the battery discipline for free.
//
// An unknown/absent style id resolves to the first catalogue entry (aurora), so
// a stale DataStore value or a build that has never heard of a newer id draws
// the default rather than crashing.
// =============================================================================

/**
 * Draw [voices] in the currently-selected waveform [styleId], filling [modifier]'s
 * bounds.
 *
 * The signature intentionally mirrors [com.aiblackbox.portal.ui.components.aurora.AuroraWaveform]'s
 * so the three call sites change by name and one added `styleId` argument only —
 * the meaning of every other parameter is unchanged and is forwarded to the
 * Aurora module verbatim:
 *
 * @param styleId the persisted style id (from `LocalWaveformStyle` / the store).
 *   Unknown/absent → the default (aurora).
 * @param voices the live speakers, exactly as [auroraVoices] builds them today.
 * @param pauseWhenIdle stop asking for frames once nothing is moving (the player
 *   bar wants this; the always-flowing mic/voice ribbons pass false). Honoured by
 *   both the Aurora module and the shared Draw loop.
 * @param restLevel where a PRESENT-but-silent voice settles, 0..1 — Aurora.BASELINE
 *   to keep a live mic breathing, 0f for the player bar's flat-on-silence rest.
 * @param spectra the SECOND channel: log-frequency bins for the speakers whose
 *   producer publishes them (see [com.aiblackbox.portal.ui.components.aurora.auroraBins]).
 *   The two live-microphone surfaces (the composer strip, the voice screen) and the
 *   settings preview all pass it; the player bar does not and cannot, because its
 *   producer is the OFFLINE decode and no spectrum of that audio exists — which is
 *   the case the styles' four-band fallback is for. Still defaulted to empty so a new
 *   surface without a spectrum producer is a valid call, and IGNORED by the
 *   self-contained Aurora module by construction — the default look consumes bands
 *   and only bands, whatever a call site hands the visualizer.
 *
 *   A style reads this list only to find out WHETHER a speaker has a spectrum; the
 *   VALUES it draws come from [WaveformFeed.bin], which is the same list smoothed to
 *   the frame rate. Reading the list's numbers directly re-creates the ~50 Hz
 *   staircase the feed exists to remove — see GlowingRibbon.fillLevels.
 */
@Composable
fun VoiceVisualizer(
    styleId: String,
    voices: List<AuroraVoice>,
    modifier: Modifier = Modifier,
    pauseWhenIdle: Boolean = true,
    restLevel: Float = Aurora.BASELINE,
    spectra: List<AuroraBins> = emptyList(),
) {
    val entry = WaveformStyles.resolve(styleId)
    when (val content = entry.content) {
        is WaveformContent.SelfContained ->
            content.content(voices, modifier, pauseWhenIdle, restLevel)

        is WaveformContent.Draw ->
            WaveformDrawHost(
                // Keyed on the RESOLVED id, not the raw styleId, so an unknown id
                // and the default it falls back to share one host instead of
                // rebuilding when the garbage value changes shape.
                styleKey = entry.id,
                create = content.create,
                voices = voices,
                modifier = modifier,
                pauseWhenIdle = pauseWhenIdle,
                restLevel = restLevel,
                spectra = spectra,
            )
    }
}

/**
 * The shared host for every [WaveformContent.Draw] style: it owns the single
 * [WaveformFeed], the withFrameNanos loop, and the canvas, and calls the
 * renderer's [WaveformRenderer.draw] once per frame.
 *
 * This is AuroraWaveform's frame loop, generalised: the engine-specific parts
 * (its bands, its phase) are replaced by the feed + a plain accumulated-seconds
 * clock, and the per-sheet draw is replaced by the one renderer call. Every
 * comment on AuroraWaveform's loop about WHY it is shaped this way applies here
 * unchanged, so they are not repeated in full — see AuroraWaveform.
 */
@Composable
private fun WaveformDrawHost(
    styleKey: String,
    create: () -> WaveformRenderer,
    voices: List<AuroraVoice>,
    modifier: Modifier,
    pauseWhenIdle: Boolean,
    restLevel: Float,
    spectra: List<AuroraBins>,
) {
    // Rebuilt only on a genuine style switch (the id changes). A renderer may hold
    // per-frame state, so it must be as long-lived as the selection — like a FieldSim.
    val renderer = remember(styleKey) { create() }
    // One feed per host, stepped by the loop below and read by the renderer.
    val feed = remember { WaveformFeed() }
    // The loop's handle on the CURRENT voices; without this the coroutine captures
    // the list that existed when it launched and never sees another.
    val liveVoices = rememberUpdatedState(voices)
    // The spectrum channel needs the identical treatment for the identical reason —
    // it is read from inside the same long-lived frame loop.
    val liveSpectra = rememberUpdatedState(spectra)
    // The ONLY snapshot state the draw phase reads: a monotonic tick, bumped every
    // frame, whose read inside onDrawBehind subscribes the draw to the loop. A
    // monotonic int (not a value mirror) so an EQUAL write can never be skipped —
    // the dt-0 frame that first flips a voice PRESENT must still repaint. (Same
    // reasoning as AuroraWaveform's tick.)
    val tick = remember { mutableIntStateOf(0) }
    val lastNanos = remember { longArrayOf(0L) }
    // Accumulated wrapped seconds, handed to the renderer as WaveformFrame.phaseSec.
    val phaseSec = remember { floatArrayOf(0f) }
    // The dt of the most recent stepped frame, shared from the frame callback to
    // the draw phase — a per-host one-element cell (never module-global, or two
    // visualizers would clobber each other's dt). The draw phase is already
    // subscribed via the tick, so no snapshot-state round trip is needed.
    val lastFrameDt = remember { floatArrayOf(0f) }

    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(renderer, feed, pauseWhenIdle, lifecycleOwner) {
        lifecycleOwner.lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            lastNanos[0] = 0L
            while (true) {
                val steppedSec = withFrameNanos { t ->
                    val dt = if (lastNanos[0] == 0L) 0f else
                        ((t - lastNanos[0]).toDouble() / 1_000_000_000.0)
                            .coerceAtMost(MAX_FRAME_DT_SEC.toDouble()).toFloat()
                    lastNanos[0] = t
                    // Wrap the phase clock so long sessions keep float precision; the
                    // wrap point is large and every renderer scales it by its own rate.
                    phaseSec[0] = (phaseSec[0] + dt) % PHASE_WRAP_SEC
                    lastFrameDt[0] = dt
                    feed.step(dt, liveVoices.value, liveSpectra.value)
                    tick.intValue++
                    dt
                }
                val current = liveVoices.value
                // Park exactly as AuroraWaveform does: an empty list parks whatever
                // pauseWhenIdle says (no wave to freeze); otherwise only when the
                // renderer reports no possible change AND real time has advanced.
                if ((pauseWhenIdle || current.isEmpty()) &&
                    !renderer.needsFrames(current, feed) &&
                    steppedSec > 0f
                ) {
                    snapshotFlow { liveVoices.value }.first { renderer.needsFrames(it, feed) }
                    lastNanos[0] = 0L
                }
            }
        }
    }

    Spacer(
        modifier = modifier.drawWithCache {
            onDrawBehind {
                @Suppress("UNUSED_VARIABLE") val subscribe = tick.intValue
                if (size.width <= 0f || size.height <= 0f) return@onDrawBehind
                val frame = WaveformFrame(
                    // The SAME dt the feed saw this frame (stashed by the loop), so
                    // a renderer's own per-frame state advances in lock-step with the
                    // feed rather than guessing from the phase delta.
                    dtSec = lastFrameDt[0],
                    phaseSec = phaseSec[0],
                    voices = liveVoices.value,
                    feed = feed,
                    restLevel = restLevel,
                    spectra = liveSpectra.value,
                )
                with(renderer) { draw(frame) }
            }
        },
    )
}

/** One stalled frame must not teleport an animation (AuroraWaveform's MAX_FRAME_DT_SEC). */
private const val MAX_FRAME_DT_SEC = 0.05f

/**
 * Where [WaveformFrame.phaseSec] wraps. Large enough that a renderer's continuous
 * motion is smooth for a whole session, small enough that a 32-bit float keeps
 * millisecond resolution at the top of the range (a phase of ~10000 s still
 * resolves ~1 ms). Renderers scale it by their own rate, so the exact value is
 * not visible as long as it wraps at a whole number of seconds.
 */
private const val PHASE_WRAP_SEC = 10_000f
