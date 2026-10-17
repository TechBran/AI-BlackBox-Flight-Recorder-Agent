package com.aiblackbox.portal.ui.components.wave

import androidx.compose.ui.graphics.drawscope.DrawScope
import com.aiblackbox.portal.ui.components.aurora.AuroraBins
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice

// =============================================================================
// WaveformRenderer — the per-style DRAW contract.
//
// A "style" that is drawn frame-by-frame (Classic and the eight reference-sheet
// looks) implements this interface. VoiceVisualizer owns the shared machinery
// around it — it creates the [WaveformFeed], runs the single withFrameNanos loop
// (with the same time-based dt, idle-park and lifecycle discipline AuroraWaveform
// established), and calls [draw] inside a DrawScope every frame. A renderer is
// therefore ALL look and no plumbing: it reads the frame's inputs and paints.
//
// Aurora is the ONE exception. It is not a draw renderer at all — it is a whole
// composable that owns its own engine and frame loop (AuroraWaveform), and the
// registry wraps it as a self-contained module (see WaveformRegistry's
// WaveformContent.SelfContained) so it stays PIXEL-IDENTICAL to what ships today.
// That is why this contract is "for the styles that share VoiceVisualizer's
// loop", not "for every style".
//
// WHAT A STYLE FILE MAY TOUCH: the body of its renderer — the geometry, the
// colours, the passes it draws, and (if it scrolls) its [needsFrames] override.
// WHAT IT MUST NOT: the [WaveformFrame] shape, the interface signature, or its
// registration lambda in WaveformRegistry. New per-frame inputs are added as
// fields on [WaveformFrame] (additive, so existing styles keep compiling), never
// by changing [draw]'s signature.
//
// The visual budget inherited from Aurora v33 is a REQUIREMENT here, not a
// suggestion: additive glow via BlendMode.Plus where glow is wanted; the SrcOver
// (occluding) passes kept under ~55% coverage so the particle field behind
// survives; NaN-safe inputs (the feed guarantees them); time-based motion off
// [WaveformFrame.dtSec] so 60 and 120 Hz look identical; and no mesa/clip at the
// 40 dp player-bar height — height-relative geometry with a defined small-height
// degrade. Per-style JVM tests pin these exactly as AuroraWaveformTest does.
// =============================================================================

/**
 * Everything a [WaveformRenderer] receives for one frame, besides the DrawScope
 * receiver (which carries `size`, and the draw/measure API) that [draw] extends.
 *
 * A plain data holder rather than a widening parameter list precisely so later
 * inputs can be ADDED here without editing a single style's [draw] signature.
 *
 * @property dtSec seconds elapsed since the previous frame, capped upstream so a
 *   stalled frame clock cannot teleport an animation. 0 on the first frame of a
 *   composition/resume/wake. Drive ALL motion off this, never off a frame count,
 *   so a 60 Hz phone and a 120 Hz Fold animate at the same wall-clock speed.
 * @property phaseSec a monotonic accumulated-seconds clock (wrapped at a large
 *   period to preserve float precision) shared by the loop. A convenience for
 *   continuous, position-independent motion — multiply it by a style's own rate.
 *   Styles that need a private phase may of course keep their own instead.
 * @property voices the live speakers this frame — the same list AuroraWaveform
 *   gets. HUMAN and/or AI, each an [AuroraVoice] with NaN-safe band drives.
 * @property feed the shared data layer, including the per-speaker history rings.
 *   Instantaneous styles read [WaveformFeed.level]/[WaveformFeed.band];
 *   scrolling styles read [WaveformFeed.history].
 * @property restLevel the floor a PRESENT-but-silent voice settles at, 0..1 —
 *   the per-call-site knob (Aurora.BASELINE for a live mic that must keep
 *   breathing, 0f for the player bar that must go truly flat on the file's
 *   silence). A style honours it so the same call site behaves the same whatever
 *   look is selected.
 * @property spectra the live log-frequency SPECTRA this frame, one entry per
 *   speaker that publishes one — the second channel beside [voices], carrying
 *   [com.aiblackbox.portal.ui.components.aurora.AuroraAnalyser.BINS] bins where
 *   the voices carry four bands. Empty on a surface whose producer has none (the
 *   player bar's offline decode runs no transform), which is what makes this an
 *   ADDED FIELD rather than a new requirement: a style that never heard of a
 *   spectrum is unaffected.
 *
 *   THIS IS THE PRESENCE FLAG, NOT THE DATA. A style asks the FEED whether a
 *   speaker has a spectrum ([WaveformFeed.hasSpectrum]) and reads its levels from
 *   [WaveformFeed.bin] (or [WaveformFeed.binHistory] for its past), for the same
 *   reason it reads bands from the feed: this list is the carrier exactly as the
 *   producer published it, at the ~50 Hz analysis hop, so a renderer drawing at
 *   60-120 Hz that reads its numbers directly repeats a value on most frames and
 *   then jumps. The feed's one smoother is what turns that into motion.
 */
class WaveformFrame(
    val dtSec: Float,
    val phaseSec: Float,
    val voices: List<AuroraVoice>,
    val feed: WaveformFeed,
    val restLevel: Float,
    val spectra: List<AuroraBins> = emptyList(),
)

/**
 * One drawable waveform style. Created once (per selection) via the registry's
 * factory and remembered by VoiceVisualizer, so a renderer MAY hold per-frame
 * state that evolves off [WaveformFrame.dtSec] — the same lifecycle a [FieldSim]
 * gets. Kept alive across frames, rebuilt on a style switch.
 */
interface WaveformRenderer {

    /**
     * Paint this style for one frame into the receiver DrawScope (its `size` is
     * the surface the caller sized). Called on every animated frame by
     * VoiceVisualizer; must be a pure read-and-draw with no allocation it can
     * avoid, exactly like AuroraWaveform's onDrawBehind.
     */
    fun DrawScope.draw(frame: WaveformFrame)

    /**
     * Whether the next frame could look any different from this one — the idle
     * park's gate, the twin of AuroraEngine.needsFrames. When VoiceVisualizer is
     * asked to pause while idle and this returns false on a frame that advanced
     * real time, the loop SUSPENDS until it returns true again (a new voice, or
     * fresh energy), so an idle surface stops repainting at 120 Hz.
     *
     * The default keeps frames while ANY present voice is audible and parks
     * otherwise — correct for instantaneous styles (Classic, Glowing Ribbon, Soft
     * Blob, Particle Wave), which show nothing new once every voice is silent. A
     * SCROLLING style must OVERRIDE this to also return true while its history ring
     * still holds motion that has not yet scrolled off screen, or it would freeze a
     * half-finished scroll the instant the speaker stopped.
     */
    fun needsFrames(voices: List<AuroraVoice>, feed: WaveformFeed): Boolean {
        for (i in voices.indices) if (voices[i].hasEnergy()) return true
        return false
    }
}
