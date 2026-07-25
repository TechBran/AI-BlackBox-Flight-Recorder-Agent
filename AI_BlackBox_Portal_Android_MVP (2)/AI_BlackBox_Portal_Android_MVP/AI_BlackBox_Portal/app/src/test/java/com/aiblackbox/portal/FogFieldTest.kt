package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.FOG_ALPHA_BUDGET
import com.aiblackbox.portal.ui.components.FOG_BANK
import com.aiblackbox.portal.ui.components.FOG_MAX_VEILS
import com.aiblackbox.portal.ui.components.FOG_MAX_VEIL_ALPHA
import com.aiblackbox.portal.ui.components.FOG_MIN_VEILS
import com.aiblackbox.portal.ui.components.FOG_RAMP
import com.aiblackbox.portal.ui.components.FOG_SPRITE_ALPHAS
import com.aiblackbox.portal.ui.components.FOG_SPRITE_STOPS
import com.aiblackbox.portal.ui.components.FOG_WEB_TO_REF_PX
import com.aiblackbox.portal.ui.components.FOG_WISP
import com.aiblackbox.portal.ui.components.FieldRandom
import com.aiblackbox.portal.ui.components.FogSim
import com.aiblackbox.portal.ui.components.FogVeil
import com.aiblackbox.portal.ui.components.FogVeilKind
import com.aiblackbox.portal.ui.components.ParticleTuning
import com.aiblackbox.portal.ui.components.fogEnvelope
import com.aiblackbox.portal.ui.components.fogFieldScale
import com.aiblackbox.portal.ui.components.fogQuadAlpha
import com.aiblackbox.portal.ui.components.fogSpriteMeanAlpha
import com.aiblackbox.portal.ui.components.fogSwell
import com.aiblackbox.portal.ui.components.fogVeilCount
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Low Fog physics + THE LEGIBILITY BUDGET — headless, seeded, no Compose /
 * Android graphics.
 *
 * Fog is the highest-risk effect in the catalogue: it is the one class that
 * raises the backdrop's AVERAGE luminance uniformly, which is exactly what
 * erodes body-text contrast for the chat sitting on top of it. The budget is
 * therefore asserted here as a measured property of the alphas the effect
 * actually draws — not trusted to the tuning table, and not trusted to a
 * comment. [fogQuadAlpha] is the very function FogSim.render calls, so these
 * assertions test the shipping draw path rather than a copy of it.
 *
 * TWO CEILINGS WERE RAISED on 2026-07-25 (per-quad 0.115 → 0.24, field-wide
 * 0.58 → 1.85) because at the old values the effect did not render at all on a
 * density-3.1 phone — see the ANDROID DIVERGENCE block in FogField.kt. The
 * assertions that used to pin those literals now pin the PROPERTY the literals
 * existed to protect (measured WCAG contrast against #C9C9C9 body text, over
 * the shipping sprite falloff and the shipping draw alphas), which is both
 * stricter and immune to the next tuning pass. A veil nobody can see has no
 * value and its budget is protecting nothing.
 *
 * Diverges from Portal/modules/fx/effects/fog.test.mjs by exactly the amount
 * FogField.kt diverges from fog.js, and for the reasons documented there.
 */
class FogFieldTest {

    private val width = 1080f
    private val height = 2400f
    private val density = 3.1f                       // FIELD_REFERENCE_DENSITY → scale 1
    private val scale = 1f

    /** Web CSS px → device px on the reference screen (scale 1). */
    private val g = scale * FOG_WEB_TO_REF_PX

    /** The Intensity dial's brightness channel at its two extremes. */
    private val dialDefault = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_DEFAULT)
    private val dialMax = ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MAX)

    /** mulberry32 — the same deterministic PRNG the web test uses. */
    private fun seeded(seed: Int = 1): FieldRandom {
        var a = seed
        return FieldRandom {
            a += 0x6D2B79F5.toInt()
            var t = a
            t = (t xor (t ushr 15)) * (1 or t)
            t += (t xor (t ushr 7)) * (61 or t)
            ((t xor (t ushr 14)).toLong() and 0xFFFFFFFFL).toDouble() / 4294967296.0
        }
    }

    private fun sim(seed: Int = 42, countScale: Float = 1f): FogSim =
        FogSim(countScale, seeded(seed)).also { it.resize(width, height, scale, density) }

    /** The largest viewport the engine can hand us — the worst case for the
     *  budget, because the population is capped at [FOG_MAX_VEILS] there. */
    private fun maxSim(seed: Int = 17): FogSim =
        FogSim(3f, seeded(seed)).also { it.resize(2560f, 1600f, 2f / 3.1f, 2f) }

    /** One frame, exactly as EmberOverlay's loop drives it. */
    private fun run(s: FogSim, frames: Int, dt: Float = 1f / 60f, active: Boolean = true) {
        for (f in 1..frames) s.update(f * dt * 1000.0, dt, active)
    }

    private fun banks(s: FogSim): List<FogVeil> = s.veils.filter { !it.wisp }
    private fun wisps(s: FogSim): List<FogVeil> = s.veils.filter { it.wisp }

    // ── WCAG plumbing. The backdrop is pure black; the chat body text is
    //    #C9C9C9. A fog quad of colour C at alpha A composites (SrcOver) to
    //    A×C on black, and the ratio below is the number every legibility
    //    assertion in this file is written against. AA body text needs 4.5:1.
    private fun toLinear(c: Float): Float =
        if (c <= 0.04045f) c / 12.92f
        else Math.pow(((c + 0.055f) / 1.055f).toDouble(), 2.4).toFloat()

    private fun luminance(r: Float, gr: Float, b: Float): Float =
        0.2126f * toLinear(r) + 0.7152f * toLinear(gr) + 0.0722f * toLinear(b)

    /** #C9C9C9 — the chat body text this backdrop sits behind. */
    private val bodyTextL = luminance(201 / 255f, 201 / 255f, 201 / 255f)

    private fun ratio(l1: Float, l2: Float): Float =
        (max(l1, l2) + 0.05f) / (min(l1, l2) + 0.05f)

    /** Body-text contrast over a backdrop of [alpha] × [rgb] composited on black. */
    private fun contrastOver(alpha: Float, rgb: IntArray): Float =
        ratio(bodyTextL, luminance(alpha * rgb[0] / 255f, alpha * rgb[1] / 255f, alpha * rgb[2] / 255f))

    /** Worst case across the ramp: the stop that lifts luminance the most. */
    private fun worstContrastOver(alpha: Float): Float =
        FOG_RAMP.minOf { contrastOver(alpha, it) }

    /**
     * THE legibility measurement: the AVERAGE alpha this field lays over the
     * whole viewport in one frame.
     *
     * Per quad that is (drawn alpha) × (ellipse area) × (the sprite's
     * area-weighted mean alpha — [fogSpriteMeanAlpha], integrated from the
     * SHIPPING falloff), summed and divided by the viewport. Ellipse extents use
     * the veil's MAXIMUM drawn size (marginX/marginY less the sway/bob, i.e.
     * rx × swellMax), so the estimate is deliberately an over-count.
     *
     * This is the quantity the file header calls "fog raises the AVERAGE
     * luminance of the backdrop UNIFORMLY", which is the thing that actually
     * erodes text contrast — as opposed to the sum-of-peaks budget, which is the
     * MECHANISM that bounds it.
     */
    private fun meanBackdropAlpha(s: FogSim, gPx: Float, wPx: Float, hPx: Float, dial: Float): Float {
        val cover = fogSpriteMeanAlpha()
        var ink = 0.0
        for (p in s.veils) {
            val a = fogQuadAlpha(p.a, s.budgetK, dial)
            val rx = p.rx * p.swellMax * gPx
            val ry = p.ry * p.swellMax * gPx
            ink += a.toDouble() * PI * rx * ry * cover
        }
        return (ink / (wPx.toDouble() * hPx.toDouble())).toFloat()
    }

    /** The WORST frame of a ten-second run: what the backdrop looks like at its
     *  brightest, which is the frame legibility has to survive. */
    private fun peakBackdropLift(s: FogSim, gPx: Float, wPx: Float, hPx: Float): Float {
        var peak = 0f
        for (f in 1..600) {
            s.update(f * 16.6667, 1f / 60f, true)
            peak = max(peak, meanBackdropAlpha(s, gPx, wPx, hPx, dialDefault))
        }
        return peak
    }

    // ── 1. Population: 10–24 enormous quads is the ENTIRE per-frame cost ──

    @Test fun `population stays inside 10 to 24 across every viewport the engine can produce`() {
        var s = 0.12f
        while (s <= 4.0001f) {
            val n = fogVeilCount(s)
            assertTrue("field scale $s gave $n veils", n in FOG_MIN_VEILS..FOG_MAX_VEILS)
            s += 0.04f
        }
        assertEquals("the reference viewport", 18, fogVeilCount(1f))
        assertEquals("the biggest viewport is capped, not scaled", FOG_MAX_VEILS, fogVeilCount(2.4f))
        assertEquals("a phone-shaped box floors at 0.4", 14, fogVeilCount(0.4f))
    }

    @Test fun `every real canvas and every dial position stays inside the bounds`() {
        for (cs in floatArrayOf(ParticleTuning.COUNT_SCALE_MIN, 0.5f, 1f, 1.6f, ParticleTuning.COUNT_SCALE_MAX)) {
            val n = sim(countScale = cs).veils.size
            assertTrue("countScale $cs produced $n veils", n in FOG_MIN_VEILS..FOG_MAX_VEILS)
        }
        for (w in floatArrayOf(320f, 1080f, 3000f, 6000f)) {
            val n = FogSim(1f, seeded(7)).also { it.resize(w, w * 2f, scale, density) }.veils.size
            assertTrue("a ${w}px canvas produced $n veils", n in FOG_MIN_VEILS..FOG_MAX_VEILS)
        }
        assertEquals("the worst case really is the capped population", FOG_MAX_VEILS, maxSim().veils.size)
    }

    @Test fun `population is viewport AREA in dp, folded with the operator dial — never pixels`() {
        // The web's fieldScale(), verbatim: viewport clamp then dial, then the
        // engine's outer clamp. A dial can thin the weather but never empty it.
        assertEquals(0.4f, fogFieldScale(100f, 1f), 1e-6f)          // tiny box → floor
        assertEquals(2.4f, fogFieldScale(9e6f, 1f), 1e-6f)          // huge box → ceiling
        assertEquals(1f, fogFieldScale(1440f * 900f, 1f), 1e-6f)    // the reference box
        assertEquals(0.12f, fogFieldScale(1440f * 900f, 0.05f), 1e-6f)  // outer floor
        assertEquals(4.0f, fogFieldScale(9e6f, 3f), 1e-6f)          // outer ceiling
    }

    // ── 2. THE two sub-populations (premium rule 3) ──

    @Test fun `both sub-populations exist at every population, and they are different animals`() {
        for (s in listOf(
            sim(seed = 9, countScale = ParticleTuning.COUNT_SCALE_MIN),
            sim(seed = 9),
            sim(seed = 9, countScale = ParticleTuning.COUNT_SCALE_MAX),
            maxSim(seed = 9),
        )) {
            val b = banks(s)
            val w = wisps(s)
            assertEquals(s.veils.size, b.size + w.size)
            assertTrue("${b.size} banks / ${w.size} wisps — a field of one layer has no depth",
                b.isNotEmpty() && w.isNotEmpty())
            // The speed difference IS the parallax, and it is guaranteed by the
            // tuning table (wisp 68–140 vs bank 26–56), not by luck of the draw.
            assertTrue("wisps must be visibly faster than banks",
                w.minOf { abs(it.vx) } > b.maxOf { abs(it.vx) })
            assertTrue("banks must be visibly larger than wisps",
                b.minOf { it.rx } > w.maxOf { it.rx })
            // Exactly the web's split: round(target × 0.6) banks, the rest wisps.
            assertEquals((s.veils.size * FOG_BANK.share).roundToInt(), b.size)
        }
    }

    @Test fun `veils are ENORMOUS — the brief's soft quads, not particles`() {
        val s = sim(seed = 4)
        for (p in s.veils) {
            val dia = p.rx * 2f
            assertTrue("veil diameter ${dia}px is outside 180–900", dia in 180f..900f)
            assertTrue("the quad must be elliptical or the slow rotation is invisible", p.ry < p.rx)
        }
    }

    // ── 3. THE LEGIBILITY BUDGET (the assertion this file exists for) ──

    @Test fun `the per-quad ceiling is a CONTRAST guarantee, and it is a real clamp`() {
        // The ceiling used to be pinned as a literal (`<= 0.14`). A literal says
        // nothing about legibility and everything about the last person to tune
        // it, so it is pinned as the PROPERTY it exists to protect instead: a
        // quad pinned at the ceiling, in the brightest colour the ramp can
        // produce, must still leave body text comfortably above WCAG AA (4.5:1).
        // Measured 8.1:1 at 0.24. That is what makes raising it safe — and what
        // will fail the build when it is no longer safe.
        val worst = worstContrastOver(FOG_MAX_VEIL_ALPHA)
        assertTrue("a quad at the ceiling drops body text to ${worst}:1", worst >= 7.0f)
        // …and no tuning value may reach the ceiling on its own.
        assertTrue("bank tops out above the ceiling", FOG_BANK.alphaMax <= FOG_MAX_VEIL_ALPHA)
        assertTrue("wisp tops out above the ceiling", FOG_WISP.alphaMax <= FOG_MAX_VEIL_ALPHA)
        // …and the draw-site clamp is a real clamp, not a comment — at ANY dial.
        assertEquals(FOG_MAX_VEIL_ALPHA, fogQuadAlpha(1f, 1f, 1f), 0f)
        assertEquals(FOG_MAX_VEIL_ALPHA, fogQuadAlpha(FOG_MAX_VEIL_ALPHA, 1f, 1f), 0f)
        assertEquals(FOG_MAX_VEIL_ALPHA, fogQuadAlpha(FOG_MAX_VEIL_ALPHA, 1f, dialMax), 0f)
        assertEquals(0.02f, fogQuadAlpha(0.04f, 0.5f, 1f), 1e-6f)
        // A corrupt dial must degrade to 1.0, never to zero and never to NaN.
        assertEquals(0.04f, fogQuadAlpha(0.04f, 1f, Float.NaN), 1e-6f)
        assertEquals(0.04f, fogQuadAlpha(0.04f, 1f, 0f), 1e-6f)
    }

    @Test fun `every drawn quad is under the ceiling and the FIELD sum stays inside the budget — 1000 frames`() {
        val s = maxSim()
        assertEquals(FOG_MAX_VEILS, s.veils.size)
        var peakDefault = 0f
        var peakSum = 0f
        for (f in 1..1000) {
            s.update(f * 16.6667, 1f / 60f, true)
            var sum = 0f
            for (p in s.veils) {
                // The ceiling holds at the DEFAULT dial and at the MAXIMUM dial —
                // the dial is applied inside fogQuadAlpha, under the clamp.
                for (dial in floatArrayOf(dialDefault, dialMax)) {
                    val a = fogQuadAlpha(p.a, s.budgetK, dial)
                    assertTrue("frame $f: quad alpha $a broke the per-quad ceiling", a <= FOG_MAX_VEIL_ALPHA)
                    assertTrue("frame $f: negative alpha $a", a >= 0f)
                }
                val base = fogQuadAlpha(p.a, s.budgetK, dialDefault)
                peakDefault = max(peakDefault, base)
                sum += base
            }
            assertTrue("frame $f: field alpha sum $sum broke the budget", sum <= FOG_ALPHA_BUDGET + 1e-4f)
            peakSum = max(peakSum, sum)
        }
        // The budget must actually ENGAGE at full population, or it is decoration.
        assertTrue("budget never bit: peak sum was only $peakSum", peakSum > FOG_ALPHA_BUDGET * 0.95f)
        // The DEFAULT dial must leave headroom under the ceiling — otherwise the
        // Intensity dial has nothing to give and the clamp is doing the tuning.
        assertTrue("no headroom left for the Intensity dial, peak was $peakDefault",
            peakDefault < FOG_MAX_VEIL_ALPHA)
        assertTrue("budgetK must stay a normalizer", s.budgetK > 0f && s.budgetK <= 1f)
    }

    @Test fun `the AVERAGE backdrop lift keeps body text in the measured contrast band`() {
        // THE number the budget exists to bound (see meanBackdropAlpha). The web
        // fields measured 9.5–11.1:1 against #C9C9C9 where AA needs 4.5:1; this
        // asserts the Android field lands in the same band at the default dial
        // and stays clear of AA even with the Intensity dial at maximum.
        //
        // It also asserts the OTHER failure: an effect too dim to see. Before
        // 2026-07-25 this field measured ~1% average lift — 12.6:1, i.e.
        // indistinguishable from a black screen, which is exactly what Brandon
        // reported from the Fold.
        // The reference Fold canvas — the surface Brandon actually tested.
        val onPhone = peakBackdropLift(sim(seed = 88), g, width, height)
        // The largest viewport the engine can produce, where the population is
        // capped and the field-wide budget is doing the work.
        val onTablet = peakBackdropLift(maxSim(seed = 88), (2f / 3.1f) * FOG_WEB_TO_REF_PX, 2560f, 1600f)

        for ((where, lift) in listOf("Fold" to onPhone, "max viewport" to onTablet)) {
            val cDefault = worstContrastOver(lift)
            val cMax = worstContrastOver(lift * dialMax)
            assertTrue("$where: ${lift * 100f}% average lift → ${cDefault}:1 body text", cDefault >= 9.0f)
            assertTrue("$where at max Intensity: → ${cMax}:1 body text", cMax >= 8.0f)
        }
        // …and the other direction. The field must lay down enough light to BE
        // fog: ~1% average lift (what the web constants produced here) is two grey
        // levels out of 255 — that is not "subtle", it is not rendering.
        assertTrue("the field is too faint to see at all: ${onPhone * 100f}% average lift",
            onPhone >= 0.03f)
    }

    @Test fun `the sprite falloff is a soft puff, and its mean is what the budget is measured against`() {
        assertEquals("the falloff must be a full profile", FOG_SPRITE_STOPS.size, FOG_SPRITE_ALPHAS.size)
        assertEquals("a puff starts opaque at its core", 1f, FOG_SPRITE_ALPHAS.first(), 0f)
        assertEquals("…and must reach exactly zero at the rim, or the ellipse has an EDGE",
            0f, FOG_SPRITE_ALPHAS.last(), 0f)
        for (i in 1 until FOG_SPRITE_STOPS.size) {
            assertTrue("stops must ascend", FOG_SPRITE_STOPS[i] > FOG_SPRITE_STOPS[i - 1])
            assertTrue("alpha must fall monotonically", FOG_SPRITE_ALPHAS[i] < FOG_SPRITE_ALPHAS[i - 1])
        }
        assertEquals("the profile must span the whole sprite", 0f, FOG_SPRITE_STOPS.first(), 0f)
        assertEquals(1f, FOG_SPRITE_STOPS.last(), 0f)
        // The area-weighted mean sits between "a point light" and "a filled disc":
        // below ~0.2 the quad is a dot with a halo, above ~0.5 it is a hard blob
        // with a rim you can see.
        val mean = fogSpriteMeanAlpha()
        assertTrue("sprite mean alpha $mean is not a soft volumetric puff", mean in 0.20f..0.50f)
        // The outermost stop, at the hottest alpha the effect can draw, must land
        // within a few grey levels of black — that is what hides the ellipse. Any
        // higher and the last stop becomes a step you can see on an OLED, which
        // is the "these are stretched bitmaps" tell.
        val rim = FOG_SPRITE_ALPHAS[FOG_SPRITE_ALPHAS.size - 2] * FOG_MAX_VEIL_ALPHA * 204f
        assertTrue("the quad has a visible edge (${rim} grey levels at the rim)", rim < 5f)
    }

    @Test fun `the COUNT dial never reaches alpha — the BRIGHTNESS dial does, under the ceiling`() {
        // countScale is a POPULATION multiplier; if it could scale alpha the
        // contrast measurements above would mean nothing at 3x.
        val lo = sim(seed = 61, countScale = ParticleTuning.COUNT_SCALE_MIN)
        val hi = sim(seed = 61, countScale = ParticleTuning.COUNT_SCALE_MAX)
        run(lo, 120); run(hi, 120)
        for (p in lo.veils + hi.veils) {
            assertTrue("the count dial leaked into alpha: ${p.a}", p.a <= FOG_MAX_VEIL_ALPHA)
        }
        assertTrue("the count dial must still do something to the population", hi.veils.size > lo.veils.size)

        // The BRIGHTNESS channel is separate and deliberate: fog draws through
        // its own SrcOver paint, so it never passes through drawSpriteF's
        // `a * paints.alphaScale` and has to apply the dial itself. Before this
        // was wired, turning Intensity up added veils of identical faintness.
        val s = sim(seed = 61)
        run(s, 120)
        var brightened = 0
        var dimmed = 0
        for (p in s.veils) {
            val base = fogQuadAlpha(p.a, s.budgetK, dialDefault)
            if (base <= 0.003f) continue
            val up = fogQuadAlpha(p.a, s.budgetK, dialMax)
            val down = fogQuadAlpha(p.a, s.budgetK, ParticleTuning.brightnessScale(ParticleTuning.INTENSITY_MIN))
            if (up > base) brightened++
            if (down < base) dimmed++
            assertTrue("the dial escaped the ceiling: $up", up <= FOG_MAX_VEIL_ALPHA)
        }
        assertTrue("Intensity must BRIGHTEN fog, not just add veils", brightened > 0)
        assertTrue("…and must still be able to dim it", dimmed > 0)
        assertTrue("the dial must be a real multiplier at the top of its range", dialMax > 1.3f)
    }

    // ── 4. The curves: size-over-life × a per-particle envelope (premium rule 1) ──

    @Test fun `the envelope and the size curve stay bounded across a whole life`() {
        var life = 1f
        while (life >= -0.001f) {
            val e = fogEnvelope(life)
            assertTrue("envelope($life) = $e", e >= 0f && e <= 1f && e.isFinite())
            val sw = fogSwell(1f - life)
            assertTrue("swell at life $life = $sw", sw >= 0.62f && sw <= 1.0000001f)
            life -= 0.001f
        }
        assertTrue("a newborn veil must fade IN, never pop", fogEnvelope(1f) < 0.01f)
        assertTrue("a dying veil must fade OUT, never pop", fogEnvelope(0f) < 0.01f)
        assertTrue("mid-life is full strength", fogEnvelope(0.6f) > 0.99f)
        assertTrue("a veil dilates monotonically as it ages", fogSwell(1f) > fogSwell(0f))
    }

    @Test fun `size carries a PER-PARTICLE envelope, so no two veils dilate alike`() {
        val envs = sim(seed = 52).veils.map { it.sizeEnv }
        assertEquals("sizeEnv must be per-particle", envs.size, envs.toSet().size)
        assertTrue("sizeEnv out of band: ${envs.min()}..${envs.max()}",
            envs.min() >= 0.82f && envs.max() <= 1.24f)
        // …and the breathe oscillator is private in BOTH depth and rate, so the
        // field can never pulse in unison.
        val hz = sim(seed = 52).veils.map { it.breatheHz }
        assertEquals("a shared breathe rate flashes the field in unison", hz.size, hz.toSet().size)
        val phases = sim(seed = 52).veils.map { it.phase }
        assertEquals("a shared phase does the same", phases.size, phases.toSet().size)
        // The sway rides the bob's rate but never matches it: equal rates trace a
        // straight diagonal (a slide), an incommensurate pair traces a roll.
        for (p in sim(seed = 52).veils) {
            assertTrue("sway must not lock to the bob", abs(p.swayHz - p.bobHz) > 1e-4f)
            assertTrue("sway rate must be positive and slower than the bob",
                p.swayHz > 0f && p.swayHz < p.bobHz)
        }
    }

    @Test fun `colour is indexed by LIFE and stays inside the ramp`() {
        // The draw path's index, replicated: hue0 + round(age × 2), clamped.
        val top = FOG_RAMP.size - 1
        fun idx(hue0: Int, age: Float) = (hue0 + (age * 2f).roundToInt()).coerceIn(0, top)
        for (p in sim(seed = 12).veils) {
            assertTrue("base ramp stop out of range: ${p.hue0}", p.hue0 in 0..top)
            assertTrue("a veil must traverse the ramp, not sit on one stop",
                idx(p.hue0, 1f) > idx(p.hue0, 0f))
        }
        assertEquals("the ramp must be walked, never flat", 4, FOG_RAMP.size)
        assertTrue("fresh veils are cool slate, old ones warm dust",
            FOG_RAMP[0][2] > FOG_RAMP[0][0] && FOG_RAMP[top][0] > FOG_RAMP[top][2])
    }

    // ── 5. Motion — the half of Brandon's report that was not about brightness ──

    @Test fun `the weather is actually ANIMATED — every oscillator cycles within a life`() {
        // "I don't see any shade of fog rolling across the screen doing something
        //  here. Needs to be more animated."
        //
        // The web's rates are RADIANS PER SECOND despite being named *Hz: at
        // bob 0.05 rad/s a veil bobbed once every 126 SECONDS and spun once every
        // 35 MINUTES, so every oscillator was a DC offset and every quad a rigid
        // blob. This asserts the fix at the TABLE level (so it holds for every
        // possible draw, not for one lucky seed): the SLOWEST oscillator of each
        // kind completes a full cycle inside the SHORTEST life of that kind.
        val tau = (2.0 * PI).toFloat()
        for ((name, k) in listOf("bank" to FOG_BANK, "wisp" to FOG_WISP)) {
            assertTrue("$name never completes a bob (${k.lifeMin}s × ${k.bobHzMin}rad/s)",
                k.lifeMin * k.bobHzMin >= tau)
            assertTrue("$name never completes a breathe (${k.lifeMin}s × ${k.breatheHzMin}rad/s)",
                k.lifeMin * k.breatheHzMin >= tau)
            // Rotation is slower by design — it is a churn, not a spin — but it
            // must still turn far enough to READ on an ellipse (~30°).
            assertTrue("$name barely turns at all over its life",
                k.lifeMin * k.spinMin >= 0.5f)
            // And the amplitudes have to survive the density difference: a bob of
            // 3 web px is 4.6 device px on a Fold, which is nothing on a 700 px
            // tall bank. 10 web px = 15 device px reads.
            assertTrue("$name bob amplitude is lost at density 3.1",
                k.bobMin * FOG_WEB_TO_REF_PX >= 12f)
            assertTrue("$name breathe depth is imperceptible", k.breatheMin >= 0.08f)
        }
    }

    @Test fun `lateral drift crosses the screen in seconds, not minutes`() {
        // The other half of "more animated". At the web's 4–11 CSS px/s a bank
        // took 1–3 MINUTES to cross a Fold, which is indistinguishable from
        // stationary. Asserted against a reference phone width so the property is
        // "how long to cross the screen", not "what number is in the table".
        val refWidthPx = 1080f
        val slowest = FOG_BANK.vxMin * FOG_WEB_TO_REF_PX
        val fastest = FOG_WISP.vxMax * FOG_WEB_TO_REF_PX
        assertTrue("the slowest bank takes ${refWidthPx / slowest}s to cross — that reads as static",
            refWidthPx / slowest <= 30f)
        assertTrue("the fastest wisp crosses in ${refWidthPx / fastest}s — fog, not a smear",
            refWidthPx / fastest >= 3f)
        // The RATIO is the parallax, and it has to be big enough to see one layer
        // slide past the other. Measured band-to-band (midpoint speeds), because
        // the extremes of the two bands are allowed to approach each other — what
        // must not happen is the two POPULATIONS travelling at the same rate.
        val bankTypical = (FOG_BANK.vxMin + FOG_BANK.vxMax) * 0.5f
        val wispTypical = (FOG_WISP.vxMin + FOG_WISP.vxMax) * 0.5f
        assertTrue("wisps must outrun banks by at least 2x (${wispTypical / bankTypical}x)",
            wispTypical / bankTypical >= 2f)
    }

    @Test fun `every veil drifts with the ONE prevailing wind, at its own speed`() {
        val s = sim(seed = 23)
        assertTrue("wind must be a direction", s.wind == 1f || s.wind == -1f)
        val x0 = s.veils.map { it.x }
        run(s, 30)                                     // half a second: no wrap, no recycle
        s.veils.forEachIndexed { i, p ->
            assertEquals("a counter-drifting veil would break the weather illusion",
                s.wind, Math.signum(p.vx), 0f)
            assertEquals("veil $i did not move downwind", s.wind, Math.signum(p.x - x0[i]), 0f)
        }
        // Spread is asserted as a property of the FIELD, not of one lucky draw:
        // the population must span both speed bands, which is what makes the two
        // layers slide past each other.
        val speeds = s.veils.map { abs(it.vx) }
        assertTrue("speeds must span both bands, or there is no depth (the parallax IS the speed)",
            speeds.max() >= FOG_WISP.vxMin && speeds.min() <= FOG_BANK.vxMax)
    }

    @Test fun `60 Hz and 120 Hz travel the same path`() {
        val a = sim(seed = 31).also { run(it, 60, 1f / 60f) }.veils
            .map { floatArrayOf(it.x, it.y, it.rot, it.life) }
        val b = sim(seed = 31).also { run(it, 120, 1f / 120f) }.veils
            .map { floatArrayOf(it.x, it.y, it.rot, it.life) }
        assertEquals(a.size, b.size)
        a.forEachIndexed { i, p ->
            assertEquals("veil $i drifted apart over 1 s", p[0], b[i][0], 0.5f)
            assertEquals("veil $i y diverged", p[1], b[i][1], 0.5f)
            assertEquals("veil $i rotation diverged", p[2], b[i][2], 0.01f)
            assertEquals("veil $i aged at a different rate", p[3], b[i][3], 1e-5f)
        }
    }

    @Test fun `a veil never clips at an edge — it wraps by its own MAXIMUM DRAWN extent`() {
        // The wrap margin is marginX/marginY, NOT rx/ry: swell × sizeEnv × breathe
        // can reach 1.66×, and the draw-time sway/bob add more, so a veil that
        // wrapped on its bare radius would teleport with two thirds of itself
        // still on screen. At the old speeds veils almost never reached an edge,
        // which is why this never showed; they now wrap constantly.
        val s = sim(seed = 8)
        for (p in s.veils) {
            assertTrue("marginX must cover the largest drawn half-width",
                p.marginX >= p.rx * p.swellMax + p.swayAmp - 1e-3f)
            assertTrue("marginY must cover the largest drawn half-height",
                p.marginY >= p.ry * p.swellMax + p.bobAmp - 1e-3f)
            assertTrue("swellMax must be sizeEnv × the breathe peak",
                abs(p.swellMax - p.sizeEnv * (1f + p.breatheAmp)) < 1e-4f)
        }
        // …and the margin genuinely cannot be `rx`: the size envelope alone can
        // draw a veil half again as wide as its stored radius, which is the case
        // the old bare-radius wrap teleported mid-screen.
        for (k in listOf(FOG_BANK, FOG_WISP)) {
            assertTrue("a veil can be drawn wider than its rx, so rx is not a margin",
                1.24f * (1f + k.breatheMin) > 1.1f)
        }
        for (f in 1..2000) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (p in s.veils) {
                val mx = p.marginX * g
                val my = p.marginY * g
                assertTrue("frame $f: x ${p.x} escaped", p.x >= -mx - 1f && p.x <= width + mx + 1f)
                assertTrue("frame $f: y ${p.y} escaped", p.y >= -my - 1f && p.y <= height + my + 1f)
            }
        }
    }

    @Test fun `nothing goes non-finite after 1000 frames`() {
        val s = sim(seed = 77)
        for (f in 1..1000) {
            s.update(f * 16.6667, 1f / 60f, true)
            for (p in s.veils) {
                for (v in floatArrayOf(
                    p.x, p.y, p.rx, p.ry, p.rot, p.life, p.a, p.vx, p.vy,
                    p.sizeEnv, p.swellMax, p.marginX, p.marginY, p.swayAmp, p.swayHz,
                )) {
                    assertTrue("a field went non-finite ($v)", v.isFinite())
                }
                assertTrue("life out of range: ${p.life}", p.life > 0f && p.life <= 1f)
                assertTrue("rot must stay wrapped: ${p.rot}", abs(p.rot) <= 6.2831856f)
            }
            assertTrue("budgetK went bad: ${s.budgetK}",
                s.budgetK.isFinite() && s.budgetK > 0f && s.budgetK <= 1f)
        }
    }

    // ── 6. The DPI law ──

    @Test fun `density changes resolution, never apparent geometry`() {
        // THE DPI LAW. The same physical screen at 1x and 2x density: every
        // spatial quantity must come out exactly 2x in pixels, i.e. identical in
        // apparent size. Population is dp-derived, so it must NOT change.
        val lo = FogSim(1f, seeded(99)).also { it.resize(width, height, 1f, 3.1f) }
        val hi = FogSim(1f, seeded(99)).also { it.resize(width * 2f, height * 2f, 2f, 6.2f) }
        assertEquals("population is dp-derived, not px-derived", lo.veils.size, hi.veils.size)
        assertEquals("the wind is not a function of density", lo.wind, hi.wind, 0f)
        run(lo, 300); run(hi, 300)
        lo.veils.forEachIndexed { i, p ->
            val q = hi.veils[i]
            assertEquals("x must be exactly 2x at 2x density", p.x * 2f, q.x, 0.5f)
            assertEquals("y must be exactly 2x at 2x density", p.y * 2f, q.y, 0.5f)
            assertEquals("stored geometry is density-free", p.rx, q.rx, 1e-4f)
            assertEquals("…and so is the wrap margin", p.marginX, q.marginX, 1e-4f)
            assertEquals("…and so is the drift", p.vx, q.vx, 1e-4f)
            assertEquals("…and so is the rotation", p.rot, q.rot, 1e-4f)
            assertEquals("…and so are the oscillator rates", p.bobHz, q.bobHz, 1e-6f)
            assertEquals("…and so is the alpha", p.a, q.a, 1e-6f)
        }
    }

    // ── 7. Lifecycle, the hot path and determinism ──

    @Test fun `the pool is allocated once — veils are recycled in place, never re-created`() {
        val s = sim(seed = 55)
        val ids = s.veils.toList()
        val lives = FloatArray(ids.size) { ids[it].life }
        var recycled = 0
        for (f in 1..3000) {
            s.update(f * 16.6667, 1f / 60f, true)
            s.veils.forEachIndexed { i, p ->
                if (p.life > lives[i]) recycled++
                lives[i] = p.life
            }
        }
        assertEquals("the pool must not grow", ids.size, s.veils.size)
        assertTrue("a recycled veil reuses its slot object",
            s.veils.withIndex().all { (i, p) -> p === ids[i] })
        assertTrue("recycling must not reshuffle the sub-populations",
            s.veils.withIndex().all { (i, p) -> p.wisp == ids[i].wisp })
        assertTrue("50 s should have recycled at least one veil", recycled > 0)
    }

    @Test fun `resize retargets the population without restarting the weather`() {
        val s = FogSim(1f, seeded(21)).also { it.resize(400f, 800f, scale, density) }
        val small = s.veils.size
        val before = s.veils.take(3).toList()
        s.resize(6000f, 6000f, scale, density)
        assertTrue("a bigger viewport gets more veils", s.veils.size > small)
        assertEquals(FOG_MAX_VEILS, s.veils.size)
        assertTrue("existing veils must keep drifting",
            s.veils.take(3).withIndex().all { (i, p) -> p === before[i] })
        s.resize(400f, 800f, scale, density)
        assertEquals("…and shrink back down", small, s.veils.size)
        // A no-op resize (the Canvas calls it on every cache invalidation) must
        // never re-scatter a live field.
        val ids = s.veils.toList()
        s.resize(400f, 800f, scale, density)
        assertTrue(s.veils.withIndex().all { (i, p) -> p === ids[i] })
    }

    @Test fun `rearm keeps the weather running instead of re-seeding it`() {
        val s = sim(seed = 6)
        run(s, 120)
        val ids = s.veils.toList()
        val xs = s.veils.map { it.x }
        s.rearm()
        assertTrue("fog is continuous; rearm must not rebuild",
            s.veils.withIndex().all { (i, p) -> p === ids[i] })
        assertEquals("…nor teleport anything", xs, s.veils.map { it.x })
        // …but a rearm before any valid size must not crash, and must not spawn
        // a field into a zero-sized canvas.
        val empty = FogSim(1f, seeded(6))
        empty.rearm()
        assertTrue(empty.veils.isEmpty())
        empty.update(16.6667, 1f / 60f, true)          // a frame before any size: no-op, not a crash
        empty.resize(width, height, scale, density)
        assertTrue("the first valid size builds the field", empty.veils.isNotEmpty())
    }

    @Test fun `state is reproducible from a seeded rand — no Math random inside`() {
        fun once(): String {
            val s = sim(seed = 3)
            run(s, 240)
            return s.veils.joinToString { "${it.x},${it.y},${it.rot},${it.life},${it.a},${it.wisp}" } +
                "|${s.wind}|${s.budgetK}"
        }
        assertEquals(once(), once())
    }

    // ── 8. The tuning table itself ──

    @Test fun `the kinds differ on every visible axis`() {
        assertTrue("wisps must be faster — the speed difference IS the parallax",
            FOG_WISP.vxMin > FOG_BANK.vxMax)
        assertTrue("banks must be bigger", FOG_BANK.rxMin >= FOG_WISP.rxMax && FOG_BANK.rxMax > FOG_WISP.rxMax)
        assertTrue("wisps are thinner shreds", FOG_WISP.aspectMin > FOG_BANK.aspectMin)
        assertTrue("wisps are shorter-lived", FOG_WISP.lifeMax < FOG_BANK.lifeMin)
        assertTrue("wisps spin faster", FOG_WISP.spinMin > FOG_BANK.spinMax)
        assertTrue("wisps bob harder", FOG_WISP.bobMax > FOG_BANK.bobMax)
        assertTrue("wisps bob faster", FOG_WISP.bobHzMin > FOG_BANK.bobHzMax)
        assertTrue("wisps sway wider", FOG_WISP.swayMax > FOG_BANK.swayMax)
        assertTrue("wisps breathe harder", FOG_WISP.breatheMin > FOG_BANK.breatheMin)
        assertTrue("wisps breathe faster", FOG_WISP.breatheHzMin > FOG_BANK.breatheHzMax)
        assertTrue("wisps are fainter — they are in front, and they must not veil the text",
            FOG_WISP.alphaMax < FOG_BANK.alphaMax)
        assertTrue("wisps drift vertically faster", FOG_WISP.vy > FOG_BANK.vy)
        assertTrue("the two kinds start on different ramp stops", FOG_WISP.hue0Lo > FOG_BANK.hue0Lo)
        assertEquals("the shares must account for the whole field", 1f, FOG_BANK.share + FOG_WISP.share, 1e-6f)
        for (k in listOf<FogVeilKind>(FOG_BANK, FOG_WISP)) {
            assertTrue("aspect must exceed 1 or the quad is a circle", k.aspectMin > 1f)
            assertTrue("base ramp stop out of the atlas", k.hue0Lo >= 0 && k.hue0Lo <= FOG_RAMP.size - 1)
            assertTrue("alpha band must be a band, not a value", k.alphaMax > k.alphaMin)
            assertTrue("every band must be a band", k.rxMax > k.rxMin && k.vxMax > k.vxMin &&
                k.bobHzMax > k.bobHzMin && k.breatheHzMax > k.breatheHzMin &&
                k.swayMax > k.swayMin && k.spinMax > k.spinMin && k.lifeMax > k.lifeMin)
            // The sway must stay a nudge on top of the drift, never a rewrite of
            // it: it is a draw-time offset the wrap margin has to absorb.
            assertTrue("sway must stay well inside the veil", k.swayMax < k.rxMin * 0.5f)
        }
    }
}
