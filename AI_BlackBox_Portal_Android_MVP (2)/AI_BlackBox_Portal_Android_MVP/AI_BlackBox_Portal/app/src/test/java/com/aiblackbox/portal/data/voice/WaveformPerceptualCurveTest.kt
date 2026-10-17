package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.Aurora
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * ONE PERCEPTUAL CURVE, ONE OWNER — the structural rule the ribbon's dynamics rest on.
 *
 * THE RULE. Exactly one stage in the chain may bend the relationship between acoustic energy and
 * drawn height, and that stage is the RENDERER ([Aurora.DISPLAY_GAMMA], applied in
 * `AuroraEnvelope.step`). Everything upstream — the live analyser, the offline/player-bar pass, the
 * demo driver behind the Settings preview — hands the renderer LINEAR normalised energy.
 *
 * WHY THE RENDERER AND NOT THE ANALYSER. The curve describes the EYE, not the microphone. It is the
 * same curve whether the audio came from a mic, a decoded TTS file or a synthetic demo, so it
 * belongs at the one stage every path passes through — which is also the only stage that cannot be
 * bypassed by adding a new producer. Everything that IS per-source (band gains, floors, HEADROOM,
 * the knee, the AGC time constants) stays in [com.aiblackbox.portal.ui.components.aurora.AuroraCalibration],
 * which is per-source by construction. That split is the reason `gamma` is not in the profile.
 *
 * WHAT THIS REPLACES. Until this milestone the lift was applied TWICE on both live paths: the
 * analyser applied GAMMA = 0.82 and then the renderer applied its square root again (effective
 * exponent 0.41), while the offline path applied LIFT = 0.55 and then the same square root
 * (effective 0.275). Two paths, two different total curves, neither one written down anywhere as a
 * decision. Both are now exactly [Aurora.DISPLAY_GAMMA].
 *
 * Every exponent below is MEASURED end-to-end through the real code (see [AuroraDynamicsHarness]),
 * never restated from a literal — a harness that copies a constant cannot notice the constant
 * moving.
 */
class WaveformPerceptualCurveTest {

    /** Exponents are fitted from two log-log points; 1% is well inside the fit's own noise. */
    private val EXPONENT_TOL = 0.01

    @Test
    fun `the analyser hands the renderer LINEAR energy — it applies no curve of its own`() {
        assertEquals(
            "the analyser is bending the level before the renderer sees it",
            1.0,
            AuroraDynamicsHarness.analyserExponent(),
            EXPONENT_TOL,
        )
    }

    @Test
    fun `the offline pass hands the renderer LINEAR energy — it applies no curve of its own`() {
        // The offline GATE stays: keeping true silence flat is a different job from bending the
        // level, and the gate is measured here only to fit the lift clear of it.
        assertEquals(
            "the offline pass is bending the level before the renderer sees it",
            1.0,
            AuroraDynamicsHarness.offlineShaping().lift,
            EXPONENT_TOL,
        )
    }

    /**
     * The THIRD place a curve can hide, and the one that was nearly missed: the RMS envelope the
     * player bar falls back to when a clip's band pass cannot run. It reaches the renderer through
     * the same AuroraVoice as everything else, so a `pow(0.55)` there meant the same clip drew on a
     * different curve depending on whether its container header parsed.
     */
    @Test
    fun `the RMS fallback envelope hands the renderer LINEAR energy too`() {
        assertEquals(
            "AudioEnvelope is bending the level before the renderer sees it",
            1.0,
            AuroraDynamicsHarness.rmsShapingExponent(),
            EXPONENT_TOL,
        )
    }

    @Test
    fun `the renderer is the owner — its measured exponent IS the declared constant`() {
        assertEquals(
            "AuroraEnvelope.step no longer applies Aurora.DISPLAY_GAMMA",
            Aurora.DISPLAY_GAMMA.toDouble(),
            AuroraDynamicsHarness.rendererExponent(),
            1e-6,
        )
    }

    @Test
    fun `both display paths compose to exactly one curve, and it is the same curve`() {
        val live = AuroraDynamicsHarness.liveDisplayExponent()
        val playerBar = AuroraDynamicsHarness.offlineShaping().composed
        val declared = Aurora.DISPLAY_GAMMA.toDouble()

        assertEquals("live path (was 0.41: GAMMA 0.82 x renderer 0.5)", declared, live, EXPONENT_TOL)
        assertEquals("player bar (was 0.275: LIFT 0.55 x renderer 0.5)", declared, playerBar, EXPONENT_TOL)
        assertEquals("the two paths must agree", live, playerBar, EXPONENT_TOL)
    }
}
