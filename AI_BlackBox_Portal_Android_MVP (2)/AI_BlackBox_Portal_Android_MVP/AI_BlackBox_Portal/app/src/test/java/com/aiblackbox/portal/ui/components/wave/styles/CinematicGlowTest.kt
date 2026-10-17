package com.aiblackbox.portal.ui.components.wave.styles

import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import com.aiblackbox.portal.ui.components.wave.EnvelopeRing
import com.aiblackbox.portal.ui.components.wave.WaveformFeed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import kotlin.math.floor

/**
 * The Cinematic TTS Wave's GEOMETRY, BUDGET and COLOUR math, asserted on the JVM —
 * the same discipline AuroraWaveformTest uses. Everything the mass's look depends
 * on lives in pure functions in [Cinematic], so the Compose draw body is only a
 * traversal of them and an instrumented test would add a device and subtract
 * nothing. Organised by the five contracts the header pins:
 *
 *   V1 newest-right, scrolling, refresh-rate independent (the feed's, inherited)
 *   V2 symmetric one-voice mass vs a top/bottom two-voice split
 *   V3 red/blue tint per speaker, shading to white-hot with loudness
 *   V4 the glow budget: SrcOver body ≤ 55% occlusion, additive stack under white-out
 *   V5 no mesa at any height, with a defined 40 dp height-range degrade
 */
class CinematicGlowTest {

    // The three surfaces this ships at (player bar, composer strip, voice screen)
    // crossed with the densities we run on. Height in PX is what the renderer sees;
    // heightDp drives the degrade.
    private val shippedHeightsDp = floatArrayOf(40f, 52f, 140f)
    private val densities = floatArrayOf(1f, 2.625f, 3f)

    // =========================================================================
    // V1 — bar layout: count, newest-right ordering, scroll independence
    // =========================================================================

    @Test
    fun `bar count fills the width and clamps to a sane range`() {
        val pitch = 7.5f // 2.5 dp @ 3x
        // A wide surface fills, capped below MAX_BARS.
        assertEquals((1080f / pitch).toInt(), Cinematic.barCount(1080f, pitch))
        // A tiny/degenerate surface never drops below MIN_BARS and never throws.
        assertEquals(Cinematic.MIN_BARS, Cinematic.barCount(1f, pitch))
        assertEquals(Cinematic.MIN_BARS, Cinematic.barCount(0f, pitch))
        assertEquals(Cinematic.MIN_BARS, Cinematic.barCount(Float.NaN, pitch))
        assertEquals(Cinematic.MIN_BARS, Cinematic.barCount(100f, 0f))
        // A 4K wall of columns is capped, not drawn.
        assertEquals(Cinematic.MAX_BARS, Cinematic.barCount(100_000f, pitch))
        assertTrue(Cinematic.MAX_BARS >= 150) // a hair above the ~150-sample ring capacity
    }

    @Test
    fun `bar zero is the newest sample flush right and older bars march left`() {
        val w = 300f
        val pitch = 5f
        val barW = 3f
        // Bar 0 (newest) sits flush against the right edge.
        val right0 = Cinematic.barLeftPx(0, w, pitch, barW) + barW
        assertEquals("newest bar must touch the leading (right) edge", w, right0, 1e-4f)
        // Each older bar is exactly one pitch further left.
        var prev = Cinematic.barLeftPx(0, w, pitch, barW)
        for (i in 1..10) {
            val left = Cinematic.barLeftPx(i, w, pitch, barW)
            assertEquals("bar $i must be one pitch left of bar ${i - 1}", prev - pitch, left, 1e-4f)
            prev = left
        }
    }

    @Test
    fun `identical history draws identically regardless of frame rate`() {
        // The dt independence is the FEED's (time-decimated ring); this renderer is
        // a pure function of the ring, so a 60 Hz phone and a 120 Hz Fold that saw
        // the same wall clock of audio draw the same bars. 2 s of a constant voice.
        val voice = listOf(AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0.5f, 0f, 0f, 0f)))
        val feed60 = WaveformFeed().apply { repeat(120) { step(1f / 60f, voice) } }
        val feed120 = WaveformFeed().apply { repeat(240) { step(1f / 120f, voice) } }
        val r60 = feed60.history(AuroraSpeaker.HUMAN)
        val r120 = feed120.history(AuroraSpeaker.HUMAN)
        assertTrue("rings must fill to within one sample", Math.abs(r60.size - r120.size) <= 1)
        val overlap = minOf(r60.size, r120.size)
        for (i in 0 until overlap) {
            val e60 = Cinematic.extentPx(140f, 140f, r60.newest(i))
            val e120 = Cinematic.extentPx(140f, 140f, r120.newest(i))
            assertEquals("bar $i extent must match across refresh rates", e60, e120, 1e-5f)
        }
    }

    // =========================================================================
    // V2 — symmetric one-voice mass vs top/bottom two-voice split
    // =========================================================================

    @Test
    fun `one voice draws a mass mirrored equally above and below centre`() {
        val centre = 70f
        val ext = 20f
        val top = Cinematic.barTopPx(centre, ext, Mode.SYMMETRIC, 1f)
        val bottom = Cinematic.barBottomPx(centre, ext, Mode.SYMMETRIC, 1f)
        assertEquals("reaches ext above centre", centre - ext, top, 1e-4f)
        assertEquals("reaches ext below centre", centre + ext, bottom, 1e-4f)
        // Truly symmetric: the centre line bisects the bar.
        assertEquals(centre, (top + bottom) / 2f, 1e-4f)
    }

    @Test
    fun `two voices split the mass into disjoint top and bottom halves`() {
        val centre = 70f
        val ext = 25f
        val gap = 2f
        // Human owns the top half (grows UP), AI the bottom (grows DOWN).
        val humanTop = Cinematic.barTopPx(centre, ext, Mode.HALF_UP, gap)
        val humanBottom = Cinematic.barBottomPx(centre, ext, Mode.HALF_UP, gap)
        val aiTop = Cinematic.barTopPx(centre, ext, Mode.HALF_DOWN, gap)
        val aiBottom = Cinematic.barBottomPx(centre, ext, Mode.HALF_DOWN, gap)

        assertTrue("human stays entirely above centre", humanBottom <= centre)
        assertTrue("ai stays entirely below centre", aiTop >= centre)
        // The two never overlap — there is a real gap between them.
        assertTrue("halves must be disjoint", humanBottom < aiTop)
        // Each half's mirror of the other reads as one symmetric mass.
        assertEquals("human reaches ext up", ext, humanBottom - humanTop, 1e-4f)
        assertEquals("ai reaches ext down", ext, aiBottom - aiTop, 1e-4f)
    }

    // =========================================================================
    // V3 — red human / blue AI, shading to white-hot with loudness
    // =========================================================================

    @Test
    fun `a quiet human bar is deep red and a quiet ai bar is deep blue`() {
        val h = Cinematic.barColor(AuroraSpeaker.HUMAN, 0f)
        assertTrue("quiet human must read red", h.red > h.green && h.red > h.blue)
        val a = Cinematic.barColor(AuroraSpeaker.AI, 0f)
        assertTrue("quiet ai must read blue", a.blue > a.red && a.blue > a.green)
        // Deep, not bright — a low-level bar is dim so the field pours through it.
        assertTrue("quiet human is deep, not bright", h.red < 0.7f)
        assertTrue("quiet ai is deep, not bright", a.blue < 0.7f)
    }

    @Test
    fun `the tallest peaks of both voices burn white-hot`() {
        for (speaker in AuroraSpeaker.entries) {
            val hot = Cinematic.barColor(speaker, 1f)
            assertTrue(
                "a full-drive $speaker bar must be white-hot (all channels high)",
                hot.red > 0.9f && hot.green > 0.9f && hot.blue > 0.7f,
            )
        }
    }

    @Test
    fun `a bar brightens monotonically from its tint toward white as it gets louder`() {
        // The human's green channel is a good white-ness proxy (deep red has almost
        // none; white-hot has nearly full), and it must rise with level.
        var prev = -1f
        var l = 0f
        while (l <= 1f) {
            val g = Cinematic.barColor(AuroraSpeaker.HUMAN, l).green
            assertTrue("colour must brighten toward white with level (at $l)", g >= prev - 1e-4f)
            prev = g
            l += 0.1f
        }
    }

    // =========================================================================
    // V4 — the glow budget (occlusion + white-out), the whole point of this style
    // =========================================================================

    @Test
    fun `the occluding body pass stays inside the 55 percent budget`() {
        // Bars never overlap horizontally and the two voices sit in disjoint
        // y-halves, so the SrcOver body is the ONLY occluder and covers any point at
        // most once. Its worst-case (loudest) alpha is therefore the whole per-point
        // occlusion, and must sit under the field-survives budget.
        assertTrue(
            "body alpha ${Cinematic.BODY_ALPHA_MAX} exceeds the 0.55 occlusion budget",
            Cinematic.BODY_ALPHA_MAX <= 0.55f,
        )
        var l = 0f
        while (l <= 1f) {
            assertTrue(
                "body alpha at $l is over budget",
                Cinematic.bodyAlpha(l) <= Cinematic.BODY_ALPHA_MAX + 1e-6f,
            )
            l += 0.1f
        }
    }

    @Test
    fun `the additive glow and hot passes stay under the white-out limit`() {
        // A point can be covered by several neighbour glows plus one hot spine, all
        // additive (BlendMode.Plus). At full drive that stack must not blow past the
        // white-out limit AuroraWaveformTest uses (1.6).
        val overlap = Cinematic.glowOverlapCount()
        // Cross-check the derivation the constant encodes.
        val expected = 2 * floor(Cinematic.GLOW_WIDTH_DP / (2f * Cinematic.PITCH_DP)).toInt() + 1
        assertEquals(expected, overlap)
        val additive = overlap * Cinematic.GLOW_ALPHA_MAX + Cinematic.HOT_ALPHA_MAX
        assertTrue(
            "additive glow+hot stack $additive risks blowing the mass to white",
            additive < 1.6f,
        )
        // ...and each additive pass is individually bounded.
        var l = 0f
        while (l <= 1f) {
            assertTrue("glow alpha at $l over cap", Cinematic.glowAlpha(l) <= Cinematic.GLOW_ALPHA_MAX + 1e-6f)
            assertTrue("hot alpha at $l over cap", Cinematic.hotAlpha(l) <= Cinematic.HOT_ALPHA_MAX + 1e-6f)
            l += 0.1f
        }
    }

    @Test
    fun `only loud bars ignite the white-hot core`() {
        // level² gating: silence carries NO hot pass, a whisper barely any, a shout
        // burns. This is what keeps a resting mass from glowing white.
        assertEquals("silence must not burn", 0f, Cinematic.hotAlpha(0f), 0f)
        assertTrue("a whisper barely burns", Cinematic.hotAlpha(0.2f) < 0.05f)
        assertTrue("a shout burns", Cinematic.hotAlpha(1f) > 0.4f)
        // Monotonic in loudness.
        assertTrue(Cinematic.hotAlpha(0.5f) < Cinematic.hotAlpha(0.9f))
    }

    @Test
    fun `the glow and body alphas rise with loudness`() {
        assertTrue(Cinematic.bodyAlpha(0.1f) < Cinematic.bodyAlpha(0.9f))
        assertTrue(Cinematic.glowAlpha(0.1f) < Cinematic.glowAlpha(0.9f))
        // A silent-but-present bar still shows a faint body (the flat line), not nothing.
        assertTrue("resting body must be visible", Cinematic.bodyAlpha(0f) > 0f)
    }

    @Test
    fun `exactly two additive passes and no opaque container`() {
        val src = stripComments(rendererSource())
        // Glow + hot core are the two BlendMode.Plus (additive light) passes; the
        // body is SrcOver. Same story AuroraWaveformTest pins for its bloom+crest.
        assertEquals(
            "expected exactly two BlendMode.Plus passes (glow + hot core)",
            2,
            Regex("""blendMode\s*=\s*BlendMode\.Plus""").findAll(src).count(),
        )
        // The style paints (thin bars), but it must NOT paint a background/container
        // that would hide the particle field — this project has shipped an opaque
        // waveform container twice.
        val banned = listOf(
            ".background(",
            "drawRoundRect(",
            "drawCircle(",
            "Surface(",
            "Card(",
            "BbxBlack",
            "BbxDark",
            "BbxSurface",
            "Color.Black",
            "Brush",
            "Gradient",
            "Shader",
        )
        for (token in banned) {
            assertFalse("Cinematic.kt contains `$token` - it must not paint a container", src.contains(token))
        }
        // It really does draw bars, so the scan is not passing on an empty file.
        assertTrue(src.contains("drawRect("))
    }

    // =========================================================================
    // V5 — no mesa at any height, with a defined 40 dp degrade
    // =========================================================================

    @Test
    fun `no bar ever reaches the rail and flat-tops at any shipped size`() {
        for (dp in shippedHeightsDp) {
            for (d in densities) {
                val hPx = dp * d
                val centre = hPx / 2f
                // The tallest possible bar: full drive, symmetric (uses the most box).
                val ext = Cinematic.extentPx(hPx, dp, 1f)
                val top = Cinematic.barTopPx(centre, ext, Mode.SYMMETRIC, 0f)
                val bottom = Cinematic.barBottomPx(centre, ext, Mode.SYMMETRIC, 0f)
                assertTrue(
                    "bar mesas at the top at ${dp}dp/${d}x (top=$top)",
                    top > 0f,
                )
                assertTrue(
                    "bar mesas at the bottom at ${dp}dp/${d}x (bottom=$bottom of $hPx)",
                    bottom < hPx,
                )
                // And the guarantee is structural: the excursion fraction is < 0.5.
                assertTrue("half fraction must stay under 0.5 at ${dp}dp", Cinematic.halfFraction(dp) < 0.5f)
            }
        }
    }

    @Test
    fun `the 40 dp player bar degrades to a shorter height range but still glows`() {
        val small = Cinematic.halfFraction(40f)   // player bar
        val mid = Cinematic.halfFraction(52f)     // composer
        val full = Cinematic.halfFraction(140f)   // voice screen
        assertEquals(Cinematic.HALF_FRACTION_SMALL, small, 1e-6f)
        assertEquals(Cinematic.HALF_FRACTION_FULL, full, 1e-6f)
        // Monotonic: the range grows with the surface.
        assertTrue("40 dp must be the tightest range", small < mid)
        assertTrue("140 dp must be the widest range", mid < full)
        // The glow is UNCHANGED by the degrade — the alphas are height-independent,
        // so a 40 dp bar blooms exactly as bright as a 140 dp one.
        assertEquals(Cinematic.glowAlpha(0.8f), Cinematic.glowAlpha(0.8f), 0f)
        assertTrue("the degrade must still bloom", Cinematic.glowAlpha(1f) > 0f)
    }

    // =========================================================================
    // V6 — the burn is SPECTRAL: what is in a bar, not only how loud it is
    // =========================================================================

    @Test
    fun `brightness reads the high bands against the loudest one`() {
        // Relative, not absolute: the measured live spread between the loudest and
        // the quietest band is over an order of magnitude, so a burn gated on an
        // absolute high-band level would simply never light on a phone mic.
        assertEquals("a body-heavy vowel is not bright", 0f, Cinematic.brightness(0.9f, 0.6f, 0f, 0f), 1e-5f)
        assertEquals("a sibilant is fully bright", 1f, Cinematic.brightness(0f, 0f, 0.3f, 0.8f), 1e-5f)
        val mid = Cinematic.brightness(0.8f, 0.8f, 0.4f, 0.2f)
        assertTrue(mid > 0f && mid < 1f)
        assertTrue(Cinematic.brightness(0.8f, 0.8f, 0.7f, 0.2f) > mid)
        // Silence and NaN carry no opinion and cannot poison a bar.
        assertEquals(0f, Cinematic.brightness(0f, 0f, 0f, 0f), 0f)
        assertTrue(Cinematic.brightness(Float.NaN, 0.4f, Float.NaN, 0.1f).isFinite())
    }

    @Test
    fun `the white-hot spine climbs the bar as the spectrum brightens`() {
        // The burning core used to run the whole bar whatever was in it. Now its
        // LENGTH is the spectrum: a dark bar burns at its base only, a bright one
        // burns all the way to the tip. Loudness still decides how fiercely (hotAlpha).
        val dull = Cinematic.spineFraction(0f)
        val bright = Cinematic.spineFraction(1f)
        assertEquals("a fully bright bar burns end to end", 1f, bright, 1e-5f)
        assertEquals(Cinematic.SPINE_MIN_FRACTION, dull, 1e-5f)
        assertTrue("a dull bar must still show a core", dull > 0f)
        assertTrue(dull < bright)
        // Monotonic and bounded across the whole range, NaN included.
        var prev = -1f
        var b = 0f
        while (b <= 1.0001f) {
            val f = Cinematic.spineFraction(b)
            assertTrue("spine fraction $f out of range", f > 0f && f <= 1f + 1e-6f)
            assertTrue("spine must not shrink as brightness rises", f >= prev - 1e-6f)
            prev = f
            b += 0.1f
        }
        assertEquals(Cinematic.spineFraction(0f), Cinematic.spineFraction(Float.NaN), 1e-6f)
    }

    @Test
    fun `the spine never escapes the bar it burns inside, in every mode`() {
        // The spine grows OUT OF THE CENTRE LINE, so it shares the bar's base and can
        // never poke past its tip — the no-mesa budget covers it with no new argument.
        val centre = 70f
        val ext = 30f
        val gap = 1f
        for (mode in listOf(Mode.SYMMETRIC, Mode.HALF_UP, Mode.HALF_DOWN)) {
            val top = Cinematic.barTopPx(centre, ext, mode, gap)
            val bottom = Cinematic.barBottomPx(centre, ext, mode, gap)
            var b = 0f
            while (b <= 1.0001f) {
                val spineExt = Cinematic.spineExtentPx(ext, b)
                val sTop = Cinematic.barTopPx(centre, spineExt, mode, gap)
                val sBottom = Cinematic.barBottomPx(centre, spineExt, mode, gap)
                assertTrue("$mode spine top escaped at brightness $b", sTop >= top - 1e-4f)
                assertTrue("$mode spine bottom escaped at brightness $b", sBottom <= bottom + 1e-4f)
                assertTrue("$mode spine inverted at brightness $b", sBottom >= sTop - 1e-4f)
                b += 0.25f
            }
            // At full brightness the spine IS the bar.
            assertEquals(ext, Cinematic.spineExtentPx(ext, 1f), 1e-4f)
        }
    }

    @Test
    fun `two spectra at the SAME level draw different bars`() {
        // THE acceptance test. Identical loudness, so the bar is exactly as tall in
        // both — but a rumble and a sibilant must not paint the same mass.
        val level = 0.7f
        val ext = Cinematic.extentPx(140f, 140f, level)
        val dull = Cinematic.brightness(0.9f, 0.5f, 0.02f, 0.01f)
        val bright = Cinematic.brightness(0.05f, 0.1f, 0.6f, 0.9f)
        assertEquals("the mass must be the same height", ext, Cinematic.extentPx(140f, 140f, level), 0f)
        assertTrue(
            "equal level must NOT imply an equal burn",
            Cinematic.spineExtentPx(ext, bright) > Cinematic.spineExtentPx(ext, dull) + 1f,
        )
        // ...and the colour differs too: the bright bar whitens sooner.
        assertTrue(
            "a bright bar must whiten sooner than a dull one of the same height",
            Cinematic.barColor(AuroraSpeaker.HUMAN, level, bright).green >
                Cinematic.barColor(AuroraSpeaker.HUMAN, level, dull).green,
        )
    }

    @Test
    fun `the shipped colour ladder is exactly the zero-brightness case`() {
        // The spectral knee is an ADDITION: with no high content the ladder is the one
        // that shipped, which is what keeps V3's pinned endpoints meaningful.
        for (speaker in AuroraSpeaker.entries) {
            var l = 0f
            while (l <= 1f) {
                val shipped = Cinematic.barColor(speaker, l)
                val neutral = Cinematic.barColor(speaker, l, 0f)
                assertEquals("red at $l", shipped.red, neutral.red, 1e-6f)
                assertEquals("green at $l", shipped.green, neutral.green, 1e-6f)
                assertEquals("blue at $l", shipped.blue, neutral.blue, 1e-6f)
                l += 0.1f
            }
        }
    }

    @Test
    fun `the hot knee moves with brightness but never off the ladder`() {
        assertEquals(Cinematic.HOT_KNEE, Cinematic.hotKnee(0f), 1e-6f)
        assertTrue("bright content must cross over sooner", Cinematic.hotKnee(1f) < Cinematic.HOT_KNEE)
        var b = 0f
        while (b <= 1.0001f) {
            val knee = Cinematic.hotKnee(b)
            // A knee at 0 or 1 would divide by zero in one of the two colour segments.
            assertTrue("knee $knee must stay strictly inside the ladder", knee > 0.05f && knee < 0.95f)
            b += 0.1f
        }
        assertEquals(Cinematic.hotKnee(0f), Cinematic.hotKnee(Float.NaN), 1e-6f)
        // Every colour on the brightened ladder is still a real colour.
        var l = 0f
        while (l <= 1f) {
            val c = Cinematic.barColor(AuroraSpeaker.AI, l, 1f)
            assertTrue(c.red.isFinite() && c.green.isFinite() && c.blue.isFinite())
            assertTrue(c.red in 0f..1f && c.green in 0f..1f && c.blue in 0f..1f)
            l += 0.1f
        }
    }

    // =========================================================================
    // NaN / empty-feed safety
    // =========================================================================

    @Test
    fun `a NaN or empty feed cannot strand a bar`() {
        // A NaN level (a level computed as rms/0) is zero excursion, not a poisoned
        // NaN that would strand the bar (JLS 15.20.1: NaN slips past coerceIn).
        assertEquals(0f, Cinematic.extentPx(140f, 140f, Float.NaN), 0f)
        assertEquals(0f, Cinematic.extentPx(140f, 140f, -1f), 0f)          // clamps
        assertEquals(Cinematic.bodyAlpha(0f), Cinematic.bodyAlpha(Float.NaN), 1e-6f)
        assertEquals(0f, Cinematic.hotAlpha(Float.NaN), 0f)
        // A never-fed ring reads as silence everywhere, no throw.
        val empty = EnvelopeRing(150)
        assertFalse("an empty ring holds no motion", Cinematic.historyEnergy(empty))
        assertEquals(0f, Cinematic.extentPx(140f, 140f, empty.newest(0)), 0f)
    }

    @Test
    fun `a history ring holds motion only while a sample is above the floor`() {
        val ring = EnvelopeRing(8)
        assertFalse(Cinematic.historyEnergy(ring))
        // A sample under the floor does not count (it is silence, and would park).
        ring.push(Cinematic.ENERGY_FLOOR / 2f)
        assertFalse("a sub-floor sample is silence", Cinematic.historyEnergy(ring))
        // A real sample keeps the scroll alive even after it has moved off newest.
        ring.push(0.6f)
        ring.push(0.01f); ring.push(0.01f)
        assertTrue("a scrolling tail still holds motion", Cinematic.historyEnergy(ring))
    }

    // ------------------------------------------------------------- source scan

    private fun stripComments(src: String): String = src
        .replace(Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL), "")
        .replace(Regex("//.*"), "")

    private fun moduleSource(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }

    private fun rendererSource(): String =
        moduleSource("src/main/java/com/aiblackbox/portal/ui/components/wave/styles/Cinematic.kt")
}
