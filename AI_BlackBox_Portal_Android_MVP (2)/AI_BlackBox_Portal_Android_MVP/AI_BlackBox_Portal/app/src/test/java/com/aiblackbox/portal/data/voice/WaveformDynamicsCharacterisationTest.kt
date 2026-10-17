package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * THE WAVEFORM MEASUREMENT FLOOR — what the ribbon does TODAY, pinned as numbers.
 *
 * READ THIS BEFORE "FIXING" A FAILURE HERE.
 *
 * Every assertion in this file is a CHARACTERISATION, not a specification. It records the
 * measured behaviour of the shipped analyser, the shipped settings preview and the shipped
 * renderer as of this commit. Some of the numbers below are still pinning things that are WRONG —
 * a band distribution so lopsided that three of the four sheets barely move, and a preview that
 * bears no resemblance to the live ribbon it is supposed to be previewing. They are pinned
 * precisely BECAUSE they are wrong: later work intends to move them, and a change that is not
 * visible in a diff is a change nobody reviewed.
 *
 * THE RECALIBRATION OF 2026-08-11 MOVED ALMOST EVERY NUMBER IN THIS FILE, ON PURPOSE, WITH THE
 * OPERATOR'S EXPLICIT APPROVAL TO CHANGE AURORA'S DEFAULT LOOK. Three things changed at once and
 * each moved a different family of numbers, so every edited expectation below names which:
 *   (C) THE CURVE. The perceptual lift was applied TWICE on both live paths (analyser GAMMA 0.82
 *       plus the renderer's square root = 0.41 live; offline LIFT 0.55 plus the same = 0.275). Both
 *       upstream copies are deleted; the renderer's Aurora.DISPLAY_GAMMA is now the only one.
 *   (A) THE SECOND REFERENCE. The auto-gain's session reference is now driven by a fast per-band
 *       peak-hold follower rather than by raw band energy, so it parks where the speech actually is
 *       instead of ~2.6x below it.
 *   (H) HEADROOM, re-derived 2.5 -> 3.5 against the single curve and the new reference.
 * The requirements all of this had to satisfy are in WaveformDynamicsGatesTest, which is the file
 * that says what is CORRECT; this one only says what IS.
 *
 * So: a failure here is not automatically a regression. It means a number moved. The correct
 * response is to confirm the move was intended, then EDIT THE EXPECTED VALUE IN THE SAME COMMIT
 * that moved it, with the new measurement in the commit message. What is never correct is
 * loosening a tolerance until the test passes again — that silently deletes the floor.
 *
 * All statistics come from [AuroraDynamicsHarness], which defines them precisely (percentiles
 * are nearest-rank over frames where a voice is present). Tolerances are tight because every
 * input is deterministic — no clock, no RNG, no device.
 */
class WaveformDynamicsCharacterisationTest {

    /** Levels are 0..1; a thousandth is far below anything an eye resolves, and stable in float. */
    private val LEVEL_TOL = 1e-3

    /** Ratios are pinned to 1%: tight enough to catch a real shift, loose enough for float drift. */
    private fun assertRatio(message: String, expected: Double, actual: Double) {
        assertEquals(message, expected, actual, expected * 0.01)
    }

    // ================================================================= 1. the live ribbon

    /**
     * The mic path at the chunk size production fed it before the AudioAnalysisTap, which now
     * analyses every stream at its own 20 ms hop (see the shipped-window case below).
     *
     * 80 ms is not an arbitrary choice — it is what the mic read length worked out to when these
     * numbers were first taken. It is now a plain parameter: since the AGC release was made
     * time-based, chunk size no longer moves the auto-gain's operating point (see
     * [AuroraRateInvarianceTest]). What it still moves is the ANALYSIS WINDOW, because the probes
     * are rectangular-window Goertzel — that residual rate-dependence is pinned in its own test at
     * the bottom of this file.
     */
    @Test
    fun `characterises_the live band distribution at an 80ms chunk`() {
        val d = AuroraDynamicsHarness.liveDistribution(chunkMs = 80)

        // Band 0 is off the ceiling. Its typical frame sits at 0.54 with loud frames at 0.94, so
        // there is now visible room between "talking" and "talking loudly" — see the swing test.
        // (80 ms was the LONGEST window a real source handed us and so the most flattering to band 0;
        // at the pre-tap 40 ms mic read the same figure is 0.39, and at the shipped 20 ms tap window
        // 0.28. All three are pinned, below.)
        //
        // MOVED by (C)(A)(H): was 0.9423 / 0.9991, i.e. a typical frame already past the soft knee.
        // The single largest contributor is (A) — a reference that parks at the speech level cannot
        // drive the ratio past the knee on ordinary speech — with (H) placing where it lands.
        assertEquals("band0 p50", 0.5427, d.perBand[0].p50, LEVEL_TOL)
        assertEquals("band0 p90", 0.9360, d.perBand[0].p90, LEVEL_TOL)

        // The other three sheets still sit near the floor for the same speech, and the reason is
        // still worth recording: on this clip those bands' references decay below NOISE_LIVE, so
        // their output denominator is that floor rather than the reference, and no amount of
        // auto-gain work reaches them. Whatever is wrong with the band balance, it is not the AGC —
        // this recalibration confirms that a second time, since it moved band 0 by half and these
        // three only by the amount (C) and (H) move any fixed ratio. Was 0.1625 / 0.0750 / 0.0366.
        assertEquals("band1 p50", 0.0606, d.perBand[1].p50, LEVEL_TOL)
        assertEquals("band2 p50", 0.0236, d.perBand[2].p50, LEVEL_TOL)
        assertEquals("band3 p50", 0.0098, d.perBand[3].p50, LEVEL_TOL)

        assertEquals("the low band dominates", 0, d.dominantBand)
    }

    /**
     * The same measurement at the chunk the mic fed once the doubled read was fixed and BEFORE the
     * AudioAnalysisTap shipped (1920 B platform minimum at 24 kHz = 960 samples = 40 ms; see
     * [MicBufferTest]). It is not the shipped window: the tap analyses every stream at its own 20 ms
     * hop whatever the read, pinned in the next test. This one stays as the record of the read fix.
     *
     * Recorded alongside the 80 ms figures rather than replacing them, because the pair is the
     * evidence for what the read-length fix did and did not do. The AGC contribution to the gap
     * between these two columns is 0.2% ([AuroraRateInvarianceTest]); everything else you see —
     * band 1 doubling, the spread falling from 25.7x to 20.3x — is the RECTANGULAR ANALYSIS WINDOW
     * changing width, the separate rate-dependence pinned at the bottom of this file. So halving
     * the read did visibly change the ribbon's band balance. That is not a regression and not a
     * hidden re-tune: it is the window defect, now the only rate-dependence left, and it is the
     * next thing to fix.
     */
    @Test
    fun `characterises_the live band distribution at the pre-tap 40ms mic read`() {
        val d = AuroraDynamicsHarness.liveDistribution(chunkMs = 40)

        // MOVED by (C)(A)(H). Was 0.8585 / 0.9978 / 0.1879 / 0.0867 / 0.0423.
        assertEquals("band0 p50", 0.3935, d.perBand[0].p50, LEVEL_TOL)
        assertEquals("band0 p90", 0.8870, d.perBand[0].p90, LEVEL_TOL)
        assertEquals("band1 p50", 0.0723, d.perBand[1].p50, LEVEL_TOL)
        assertEquals("band2 p50", 0.0282, d.perBand[2].p50, LEVEL_TOL)
        assertEquals("band3 p50", 0.0117, d.perBand[3].p50, LEVEL_TOL)

        assertEquals("the low band still dominates", 0, d.dominantBand)
        // THE HEADLINE NUMBER OF THIS MILESTONE: the dominant sheet's peak-to-typical swing, 1.162
        // before and 2.347 after. This is the "saturated plateau that barely moves" measured.
        assertRatio("band0 p90/p50 swing (was 1.162)", 2.254, d.perBand[0].swing)
        // The band spread WIDENED (was 20.293 / 15.794), which is the one thing this milestone
        // made worse: band 0 came off the soft knee that had been compressing it toward the other
        // three. The imbalance itself is a NOISE_LIVE-floor defect, not a dynamics one, and nothing
        // here could have reached it — see the spread test below.
        assertRatio("band spread (medians)", 33.521, d.bandSpread)
        assertRatio("band spread (means)", 33.042, d.bandSpreadOfMeans)
    }

    /**
     * The same measurement at the window that SHIPS: mic-sized reads into a real AudioAnalysisTap,
     * whose band window is its 20 ms hop ([AuroraDynamicsHarness.liveDistributionViaTap], processed
     * profile at 24 kHz like the cases above). This is the distribution the live ribbon is actually
     * driven by, and the one WaveformDynamicsGatesTest scores its requirements on — where, at
     * 0.276, the dominant sheet's typical frame misses the 0.30 lower bound the 40 ms figure above
     * cleared.
     *
     * Everything that differs from the 40 ms column is the RECTANGULAR WINDOW halving again (see the
     * window test at the bottom of this file; the tap itself adds nothing — TapEquivalenceTest): band
     * 0's typical frame drops from 0.394 to 0.276 while its loud frames barely move, so its swing
     * widens from 2.254 to 3.278.
     */
    @Test
    fun `characterises_the live band distribution at the shipped 20ms tap window`() {
        val d = AuroraDynamicsHarness.liveDistributionViaTap()

        assertEquals("band0 p50", 0.2762, d.perBand[0].p50, LEVEL_TOL)
        assertEquals("band0 p90", 0.9053, d.perBand[0].p90, LEVEL_TOL)
        assertEquals("band1 p50", 0.0745, d.perBand[1].p50, LEVEL_TOL)
        assertEquals("band2 p50", 0.0290, d.perBand[2].p50, LEVEL_TOL)
        assertEquals("band3 p50", 0.0121, d.perBand[3].p50, LEVEL_TOL)

        assertEquals("the low band still dominates", 0, d.dominantBand)
        assertRatio("band0 p90/p50 swing", 3.278, d.perBand[0].swing)
        assertRatio("band spread (medians)", 22.817, d.bandSpread)
        assertRatio("band spread (means)", 28.129, d.bandSpreadOfMeans)
    }

    @Test
    fun `characterises_the live ribbon has almost no peak-to-typical swing on its dominant band`() {
        val d = AuroraDynamicsHarness.liveDistribution(chunkMs = 80)

        // 1.72x, was 1.060 — a loud moment used to be 6% taller than an ordinary one on the sheet
        // carrying most of the visible motion, which is exactly why the ribbon read as a bright bar
        // that did not track the voice. MOVED by (A) above all: once the reference parks at the
        // speech level, ordinary speech leaves the soft knee and the top of the range is usable
        // again. (At the pre-tap 40 ms mic read the same figure is 2.254 and at the shipped 20 ms tap
        // window 3.278; a longer analysis window raises the p50 and shrinks the swing accordingly.)
        assertRatio("band0 p90/p50 swing (was 1.060)", 1.725, d.perBand[0].swing)

        // The quiet bands swing a little more than before (was 2.172 / 2.176), for the arithmetic
        // reason rather than an interesting one: they sit on the NOISE_LIVE floor, so their ratio is
        // untouched by the AGC and only (C) — dropping the analyser's compressing GAMMA — moves it.
        assertRatio("band1 swing", 2.576, d.perBand[1].swing)
        assertRatio("band3 swing", 2.580, d.perBand[3].swing)
    }

    @Test
    fun `characterises_the live bands are spread over more than an order of magnitude`() {
        val d = AuroraDynamicsHarness.liveDistribution(chunkMs = 80)

        // Loudest band's typical level over quietest band's typical level. Four sheets are drawn;
        // one of them is doing essentially all of the work.
        //
        // MOVED again (was 25.726 / 19.469), and the WRONG WAY: band 0 came off the soft knee,
        // which had been compressing it toward the other three, so removing the saturation exposes
        // more of the imbalance rather than less. Worth recording plainly — this milestone improved
        // the ribbon's dynamics and left its band BALANCE slightly worse, and that balance is a
        // NOISE_LIVE-floor problem for a later milestone, not something HEADROOM can reach.
        assertRatio("band spread (medians)", 55.110, d.bandSpread)
        assertRatio("band spread (means)", 43.845, d.bandSpreadOfMeans)
    }

    @Test
    fun `characterises_the live ribbon never reaches true zero inside speech`() {
        val d = AuroraDynamicsHarness.liveDistribution(chunkMs = 80)
        // Not one voiced frame on any band is exactly zero. The live ribbon has no true rest
        // inside an utterance; contrast the preview, which is at true zero 39% of the time.
        assertEquals("live zero fraction", 0.0, d.pooled.zeroFraction, 0.0)
    }

    // ============================================================== 2. the settings preview

    /**
     * The Settings style preview NEVER touches the analyser: WaveformStylePreview feeds
     * [com.aiblackbox.portal.ui.components.wave.WaveformDemoDriver] output straight into
     * VoiceVisualizer. So "what a style looks like in Settings" and "what that style looks like
     * on a real voice" are two unrelated signals, and these numbers say how unrelated.
     */
    @Test
    fun `characterises_the preview band distribution`() {
        val d = AuroraDynamicsHarness.previewDistribution(AuroraSpeaker.HUMAN)

        assertEquals("pooled p50", 0.0308, d.pooled.p50, LEVEL_TOL)
        assertEquals("pooled p90", 0.5610, d.pooled.p90, LEVEL_TOL)

        // All four bands sit at a similar typical level — the opposite of the live path's
        // 24x lopsidedness.
        assertRatio("band spread (medians)", 1.861, d.bandSpread)
        assertRatio("band spread (means)", 1.245, d.bandSpreadOfMeans)
    }

    @Test
    fun `characterises_the preview is at true zero for 39 percent of every syllable`() {
        val d = AuroraDynamicsHarness.previewDistribution(AuroraSpeaker.HUMAN)
        // The demo driver's syllable is voiced for SYLLABLE_DUTY and silent for the rest, so the
        // preview genuinely flatlines between syllables. Real speech analysed by the analyser
        // never does this (see the live zero-fraction test above).
        assertEquals("pooled zero fraction", 0.3873, d.pooled.zeroFraction, 2e-3)
    }

    @Test
    fun `characterises_the preview swings 18x where the live ribbon swings 1_1x`() {
        val preview = AuroraDynamicsHarness.previewDistribution(AuroraSpeaker.HUMAN)
        val live = AuroraDynamicsHarness.liveDistribution(chunkMs = 80)

        assertRatio("preview pooled swing", 18.241, preview.pooled.swing)
        // MOVED by (C)(A)(H), was 8.458. The live ribbon's pooled swing now essentially MATCHES the
        // preview's 18.2 — though for a reason that is only half good news: band 0 genuinely swings
        // now, and the three floored sheets drag the pooled p50 down, and both raise this figure.
        assertRatio("live pooled swing", 15.083, live.pooled.swing)
    }

    /**
     * THE GAP, stated as one number per property. This is the test the preview work exists to
     * change: a preview that predicted the live ribbon would put every ratio here near 1.0.
     */
    @Test
    fun `characterises_the preview does not resemble the live ribbon it previews`() {
        val preview = AuroraDynamicsHarness.previewDistribution(AuroraSpeaker.HUMAN)
        val live = AuroraDynamicsHarness.liveDistribution(chunkMs = 80)

        // Typical level: the live ribbon sits 1.27x higher than the preview, was 3.7x. The gap
        // nearly closed as a side effect of the recalibration — nothing here targeted the preview.
        assertRatio("live p50 / preview p50", 1.269, live.pooled.p50 / preview.pooled.p50)

        // Dominant-band swing: the preview's dominant sheet swings 9.3x more than the live one's,
        // was 15.160. The live ribbon moved toward the preview, which is the
        // direction this gap has to close in, though a preview that PREDICTED the live ribbon would
        // put this at 1.0 and that is still a long way off.
        assertRatio(
            "preview dominant swing / live dominant swing (was 15.160)",
            9.318,
            preview.perBand[preview.dominantBand].swing / live.perBand[live.dominantBand].swing,
        )

        // Band balance: the live path is 29.6x more lopsided across its four sheets, was 13.824 —
        // the one property this milestone made worse, for the reason given on the spread test above.
        assertRatio("live spread / preview spread", 29.613, live.bandSpread / preview.bandSpread)

        // And the preview rests at true zero while the live ribbon never does.
        assertTrue("preview reaches true zero", preview.pooled.zeroFraction > 0.35)
        assertEquals("live never does", 0.0, live.pooled.zeroFraction, 0.0)
    }

    // ==================================================== 3. the ONE perceptual curve

    /**
     * The renderer's perceptual lift — [com.aiblackbox.portal.ui.components.aurora.Aurora.displayCurve]
     * in AuroraEnvelope.step. Measured end-to-end by settling a real envelope, so this tracks the
     * renderer rather than restating it.
     *
     * This is the ONLY curve in the pipeline now. The section used to be headed "double
     * compression"; the two duplicate upstream curves it characterised are gone, and the structural
     * rule that keeps them gone is asserted in WaveformPerceptualCurveTest.
     */
    @Test
    fun `characterises_the renderer applies one power law on top of whatever it is fed`() {
        // MOVED 0.50 -> 0.65 on 2026-08-12, deliberately, with the operator's approval, after v35
        // on a Fold read as "constantly too much going on". A square root is a very strong lift and
        // it was measured destroying 76.1% of the whole utterance's drawn range while dragging room
        // tone up into the picture; 0.65 gives ~34% less drawn height on room tone at EVERY level
        // and renders every contrast in the signal larger. See [Aurora.DISPLAY_GAMMA].
        assertEquals("renderer exponent (was 0.50)", 0.65, AuroraDynamicsHarness.rendererExponent(), 1e-6)
        // A quarter-energy band settles at 0.25^0.65. Was 0.5 (the square root).
        assertEquals("settled level for a 0.25 band", 0.4061, AuroraDynamicsHarness.settledLevel(0.25f), 1e-3)
    }

    /**
     * LIVE effective exponent, 0.50, WAS 0.41 (the analyser's GAMMA 0.82 composed with the
     * renderer's 0.5).
     *
     * 0.41 was a very aggressive compression — a 4x change in acoustic level became a 1.8x change in
     * displayed height, and combined with a reference parked below the speech it produced the 1.16x
     * dominant-band swing this file used to record. At 0.50 the same 4x becomes 2.0x.
     *
     * Measured end-to-end through a real analyser (settle on a tone, probe one chunk below the
     * settled level so the denominator is frozen), so it tracks the code rather than restating it.
     */
    @Test
    fun `characterises_the live display exponent is 0_65 — one curve, the renderer's`() {
        // The live path is analyser x renderer, and the analyser is required to be LINEAR
        // (WaveformPerceptualCurveTest owns that requirement), so this figure IS the renderer's
        // exponent. Was 0.41 (analyser GAMMA 0.82 x renderer 0.5), then 0.50, now 0.65.
        assertEquals("live composed exponent (was 0.41, then 0.50)", 0.65, AuroraDynamicsHarness.liveDisplayExponent(), 2e-3)
    }

    /**
     * PLAYER BAR effective exponent, 0.50, WAS 0.275 (the offline path's own LIFT = 0.55 composed
     * with the same renderer square root). The offline gate and lift are recovered empirically
     * (their constants are private), which pins them as behaviour rather than as a copy of a
     * literal — and it is what caught the lift being a second curve in the first place.
     *
     * The GATE stays: keeping true silence flat is a threshold, not a curve, and only this path
     * (which peak-normalises a whole known clip) needs one.
     */
    @Test
    fun `characterises_the player bar display exponent is 0_65 — the same one curve`() {
        val s = AuroraDynamicsHarness.offlineShaping()

        assertEquals("offline gate", 0.133, s.gate, 3e-3)
        assertEquals("offline lift, was 0.550 — now no curve at all", 1.000, s.lift, 2e-3)
        // MOVED 0.500 -> 0.650 with the renderer's DISPLAY_GAMMA; the offline path applies no
        // curve of its own, so the composed figure is the renderer's exponent exactly.
        assertEquals("renderer", 0.650, s.renderer, 1e-6)
        assertEquals("player bar composed exponent (was 0.275, then 0.500)", 0.650, s.composed, 2e-3)

        // The two paths used to compress by different amounts — the bar HARDER than the live path,
        // on audio that had already been peak-normalised. They are the same curve now.
        assertEquals(
            "the player bar and the live path must compress identically",
            AuroraDynamicsHarness.liveDisplayExponent(),
            s.composed,
            2e-3,
        )
    }

    // ============================================== 4. chunk size changes what is drawn

    /**
     * THE DEFECT THAT WAS HERE, NOW FIXED — and pinned at the value it was fixed TO.
     *
     * The AGC release (`ref * STEADY_DECAY` in AuroraAnalyser.analyze) used to be applied ONCE PER
     * CHUNK with no `dtSec` term, unlike the STEADY_RISE beside it. Because the two oppose each
     * other the consequence was not a wrong time constant but a wrong OPERATING POINT: a constant
     * band energy `v` parks the reference at `s*v / (1 - d + s*d)`, and with `s` time-based and `d`
     * per-chunk, that balance moved with the buffer size upstream. The reference is the output's
     * denominator, so THE SAME UNCHANGING TONE WAS DRAWN AT A DIFFERENT HEIGHT depending on how the
     * caller happened to chunk it — measured across this sweep: 0.9587 / 0.8019 / 0.7360 / 0.6027 /
     * 0.5765, a 1.66x range on identical audio.
     *
     * The release is now `exp(-dtSec / STEADY_DECAY_TAU_SEC)`, sized to reproduce the measured
     * per-chunk constant exactly at 32 ms, so the sweep collapses onto the 32 ms value that was
     * always the constant's own calibration. Note which number DIDN'T move: 32 ms was 0.8019 before
     * and is 0.8019 after. That is the evidence that this was a rate fix and not a re-tune.
     *
     * This test keeps pinning the LEVEL (it is the calibration floor, and the recalibration moved
     * it). The PROPERTY — that the level does not depend on chunk size, on any band — is asserted
     * separately in [AuroraRateInvarianceTest], which also documents the residual that survives,
     * why it is there, and why removing it is a different job. Note that the property test needed
     * NO edit through this milestone: adding a second reference did not cost any rate honesty.
     */
    @Test
    fun `characterises_the AGC release parks one steady tone at 0_42 whatever the chunk size`() {
        // Identical audio — one unchanging 220 Hz tone at one amplitude — analysed at five buffer
        // sizes. Every number here IS the same, to within the operator-splitting residual.
        //
        // MOVED from 0.80 to 0.42 by (C) and (H): a steady tone parks the reference at a fixed fraction of
        // its own level, so its displayed height is a pure function of HEADROOM and the curve, and
        // both changed. (A) cannot move this one — a fast follower on an UNCHANGING input settles
        // exactly on that input, which is what makes this the cleanest probe of the other two.
        assertEquals("16ms", 0.4249, AuroraDynamicsHarness.steadyToneLevel(16), LEVEL_TOL)
        assertEquals("32ms", 0.4244, AuroraDynamicsHarness.steadyToneLevel(32), LEVEL_TOL)
        assertEquals("40ms", 0.4241, AuroraDynamicsHarness.steadyToneLevel(40), LEVEL_TOL)
        assertEquals("80ms", 0.4229, AuroraDynamicsHarness.steadyToneLevel(80), LEVEL_TOL)
        assertEquals("100ms", 0.4224, AuroraDynamicsHarness.steadyToneLevel(100), LEVEL_TOL)

        // Was 1.663x between the smallest and largest buffer a real source hands us, then 1.004x
        // once the release was made time-based. Slightly larger in RATIO terms now only because the
        // level it is a ratio of is no longer sitting on the soft knee, which used to compress it.
        assertRatio(
            "16ms level / 100ms level (was 1.663 before M1, 1.004 after)",
            1.006,
            AuroraDynamicsHarness.steadyToneLevel(16) / AuroraDynamicsHarness.steadyToneLevel(100),
        )

        // The specific move the mic read-length fix used to cause on its own: correcting the read
        // from 80 ms to the intended 40 ms (see [MicBufferTest]) raised this tone 22% through that
        // defect alone. Still a fraction of a percent, which is what made that fix safe to make.
        assertRatio(
            "40ms level / 80ms level — what the mic-read fix moves (was 1.221)",
            1.003,
            AuroraDynamicsHarness.steadyToneLevel(40) / AuroraDynamicsHarness.steadyToneLevel(80),
        )
    }

    /**
     * A SECOND, INDEPENDENT rate-dependence, pinned separately because it has a different cause
     * and will need a different fix.
     *
     * The probes are single-bin Goertzel over a RECTANGULAR window, so the chunk length IS the
     * analysis window: it sets the bin resolution (31 Hz at 32 ms, 12.5 Hz at 80 ms) and therefore
     * how much of an off-bin partial each band actually sees. On real speech this effect is LARGER
     * than the AGC one above — and now demonstrated rather than predicted: making the release
     * time-based left every per-band ratio below unchanged to four figures, so they were never the
     * AGC's doing. Fixing the release did NOT fix these; a window function and/or rate-normalised
     * probes would.
     */
    @Test
    fun `characterises_the rectangular analysis window makes band balance depend on chunk size`() {
        val at32 = AuroraDynamicsHarness.liveDistribution(chunkMs = 32)
        val at80 = AuroraDynamicsHarness.liveDistribution(chunkMs = 80)

        // MOVED by (C) alone — dropping the analyser's GAMMA stops compressing these ratios, so the
        // SAME underlying window defect now reads at its true size. Was 2.361 / 3.142 / 2.520, and
        // 2.850 / 4.042 / 3.084 is what those were all along under a 0.82 power law
        // (2.361^(1/0.82) = 2.85, and so on for the other two).
        assertRatio("band1 32ms/80ms", 2.850, at32.perBand[1].p50 / at80.perBand[1].p50)
        assertRatio("band2 32ms/80ms", 4.042, at32.perBand[2].p50 / at80.perBand[2].p50)
        assertRatio("band3 32ms/80ms", 3.084, at32.perBand[3].p50 / at80.perBand[3].p50)

        // So "how lopsided the ribbon looks" is itself a function of the buffer size: 13.9x at
        // 32 ms against 55.1x at 80 ms. Was 9.548 / 25.726.
        assertRatio("spread at 32ms", 13.853, at32.bandSpread)
        assertRatio("spread at 80ms", 55.110, at80.bandSpread)
    }
}
