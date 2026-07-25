package com.aiblackbox.portal.ui.components

// =============================================================================
// Field: LOW FOG (id "fog") — the CHEAPEST field in the catalogue and the one
// with the highest legibility risk.
//
// A native port of the web module
//   Portal/modules/fx/effects/fog.js
// (its physics tests: Portal/modules/fx/effects/fog.test.mjs) — see the
// ANDROID DIVERGENCE block below for where and why the two now differ.
//
// Veils of grey ROLLING across the black at different speeds, so the backdrop
// reads as weather with depth instead of flat paint. The whole effect is 10–24
// enormous soft radial quads per frame — no noise, no shader, no per-pixel work.
// The sprite-blown-up-to-1200 px mush that is a DEFECT for embers (visible blur
// on a 6 px spark) is the FEATURE here: it is exactly the soft-edged falloff a
// fog bank needs, for free.
//
// ── ANDROID DIVERGENCE FROM fog.js (2026-07-25) — DO NOT "RESTORE PARITY" ────
//
// Brandon, Fold 6 (density 3.1, 120 Hz), testing all thirteen effects:
//   "Low fog — not really seeing anything here on the Android side. I don't see
//    any shade of fog rolling across the screen doing something here. Needs to
//    be more animated, we should probably look at how we can enhance that."
//
// Two independent defects, both of which the web numbers cause on this surface:
//
//  1. INVISIBLE. The web authors alpha in CSS px at an apparent scale near 1.0;
//     Android multiplies geometry by apparentScale 0.5 AND density 3.1, and the
//     web's 2.7–7.4% per-quad alphas then composite (SrcOver, on black, through
//     a radial falloff whose area-weighted mean is ~0.34 of its peak) to an
//     AVERAGE screen lift of ~1% — about TWO grey levels out of 255. That is
//     not "subtle", it is not rendering. Measured against #C9C9C9 body text the
//     old field sat at 12.6:1 where pure black is 12.7:1 — i.e. the budget was
//     protecting nothing, because there was nothing there.
//     Per-quad alphas are now ~2.7× the web's and the field-wide budget ~3.2×.
//     The cross-surface contract is the same APPARENT RESULT, not the same
//     numbers.
//
//  2. STATIC. The web's bob/breathe/spin "Hz" are RADIANS PER SECOND, not Hz —
//     sin(t · 0.05) has a period of 126 SECONDS, and spin at 0.003 rad/s is one
//     revolution per 35 minutes. Every oscillator was therefore a DC offset and
//     every quad a rigid blob sliding at 6–17 device px/s (a bank crossed the
//     Fold in 1–3 minutes). The rates below are 4–10× the web's and are now
//     asserted at the TABLE level: every veil completes at least one full bob
//     and one full breathe cycle, and turns at least ~30°, WITHIN ITS OWN
//     LIFETIME (FogFieldTest."the weather is actually ANIMATED"). Lateral drift
//     is ~6× — the slowest bank crosses a reference phone in under 30 s, the
//     fastest wisp in ~5 s, and that ratio IS the parallax.
//
//  Two consequences of the new speeds that the old numbers hid:
//   • WRAP MARGIN. Veils were far too slow to ever reach an edge; they now wrap
//     constantly. The margin is the veil's MAXIMUM DRAWN extent
//     (rx × sizeEnv × (1 + breatheAmp) + swayAmp — see [FogVeil.marginX]), not
//     its bare rx: swell × sizeEnv × breathe can reach 1.66×, so the old margin
//     would have teleported a two-thirds-visible bank across the screen.
//   • SWAY. A draw-time horizontal companion to the bob, on a rate incommensurate
//     with it, so a bank surges and eases instead of translating linearly. It is
//     draw-only (like the bob) — zero physics cost, zero effect on the dt tests.
//
// ── THE LEGIBILITY BUDGET — the reason this file is careful ──────────────────
//
// This is a BACKDROP behind chat body text. Unlike embers or stars, which put
// light in a few small places, fog raises the AVERAGE luminance of the backdrop
// UNIFORMLY — precisely what erodes the contrast of the text sitting on top of
// it. So the ceiling is enforced in CODE, twice:
//
//   FOG_MAX_VEIL_ALPHA   a HARD per-quad ceiling, applied with coerceAtMost at
//                        BOTH the compute site (update) and the draw site
//                        ([fogQuadAlpha], AFTER the Intensity dial). No tuning
//                        value in the kinds table may exceed it, and
//                        FogFieldTest asserts the ceiling as a CONTRAST property
//                        (a quad pinned at the ceiling still clears 7:1 against
//                        #C9C9C9) rather than as a frozen literal.
//   FOG_ALPHA_BUDGET     a HARD field-wide ceiling on the SUM of the quad
//                        alphas. A per-quad cap alone is not enough: twenty-four
//                        veils stacked are a white wash. update() normalises the
//                        whole field down to the budget once per frame
//                        ([FogSim.budgetK]), so the worst-case luminance lift is
//                        bounded no matter what the population does.
//
// The measured operating point at the default dial (reference Fold canvas,
// 1080×2400, 14 veils) is an average backdrop lift of ~6% alpha → ~11.8:1
// against #C9C9C9, inside the 9.5–11.1:1 band the web fields measured and well
// clear of WCAG AA's 4.5:1. FogFieldTest integrates that number from the
// SHIPPING sprite falloff and the SHIPPING draw alphas every run.
//
// COMPOSITING IS SrcOver — deliberately NOT the engine's additive FIELD_BLEND.
// Fog is not emissive; under Plus/'lighter' the overlaps would blow out to white,
// which is the exact failure the budget exists to prevent. That is why this
// effect draws through its OWN baked paint (see FOG_PAINT_KEY) instead of
// FieldPaints.sprite, which carries the additive blend every other field wants.
// It is the one and only reason this file does not use drawSpriteF — and it is
// why the Intensity dial's brightness channel (FieldPaints.alphaScale) has to be
// applied HERE, by hand, in [fogQuadAlpha]: drawSpriteF's multiply never runs.
//
// ── The three premium rules, as they land here ──────────────────────────────
//   1. SIZE OVER LIFE with a per-particle envelope: fogSwell(age) dilates a veil
//      0.62 → 1.0 monotonically as it ages, multiplied by the veil's OWN
//      sizeEnv (0.82–1.24) and its own breathe oscillation, so no two dilate to
//      the same size and none of them is a constant blob.
//   2. LIFE-INDEXED COLOUR RAMP: FOG_RAMP is walked by age (hue0 + round(age×2)),
//      so a veil is born cool slate (fresh, backlit) and ages through neutral
//      into warm dust. Never a flat grey — that is what stops two dozen
//      overlapping quads from reading as one sheet.
//   3. TWO SUB-POPULATIONS: banks (slow, deep, huge) and wisps (quick, small,
//      shredded). The speed difference IS the parallax.
//
// ── The two Android-specific contracts (get these wrong and it ships broken) ──
//
// DPI. Compose canvas coordinates are DEVICE PIXELS. Every spatial length here
// is authored at FIELD_REFERENCE_DENSITY and multiplied by
// `scale = density / FIELD_REFERENCE_DENSITY` at the moment it is used, so the
// field has the same APPARENT size on a 2.0 tablet and a 3.5 phone. Density
// reaches exactly two things: geometry (through [FogSim.g]) and the dp-derived
// population target — never an alpha, never a period, never a lifetime.
//
// dt. All motion is PIXELS PER SECOND multiplied by dtSec directly — no
// `dt * 60` normalizer. The bob/sway/breathe oscillators run off a PRIVATE clock
// advanced by dtSec, NOT off nowMs: a parked frame loop must resume the weather
// where it left it rather than teleporting every veil to a new common phase on
// resume.
// =============================================================================

import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.drawscope.CanvasDrawScope
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.LayoutDirection
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin

// -----------------------------------------------------------------------------
// The legibility ceilings. NOT style, and not to be re-tuned by feel: both are
// asserted by FogFieldTest as CONTRAST properties measured over the shipping
// draw path, so raising either one fails the build the moment the backdrop would
// start eating body text.
// -----------------------------------------------------------------------------

/** HARD per-quad alpha ceiling. Not a convention — a coerceAtMost, twice, the
 *  second time AFTER the Intensity dial (see [fogQuadAlpha]).
 *
 *  Diverges from fog.js' 0.115 (which itself came up from 0.06 on 2026-07-24 for
 *  the same reason): at 0.115 the field measured ~1% average screen lift on a
 *  density-3.1 phone — two grey levels, i.e. invisible. A single quad pinned at
 *  0.24 composites to grey ~48 on black and still clears 8.1:1 against #C9C9C9
 *  body text, where AA needs 4.5:1. */
internal const val FOG_MAX_VEIL_ALPHA = 0.24f

/** HARD ceiling on the SUM of the field's quad alphas (worst-case stack), at the
 *  reference Intensity dial. Diverges from fog.js' 0.58 for the same reason and
 *  by roughly the same factor; the resulting AVERAGE backdrop lift is the number
 *  that is actually asserted (≥ 9:1 body-text contrast at the default dial,
 *  ≥ 8:1 at the maximum). */
internal const val FOG_ALPHA_BUDGET = 1.85f

/** Population bounds. Two dozen giant quads is the ENTIRE per-frame cost — and
 *  the fill cost is FLAT against the old 8–20 because the veils came down in
 *  size at the same time (see FOG_BANK.rxMax: 450 → 390 web px). More, smaller
 *  banks is also what lets them pass IN FRONT OF each other, which is the cue
 *  the eye actually reads as motion. */
internal const val FOG_MIN_VEILS = 10
internal const val FOG_MAX_VEILS = 24

/**
 * WEB CSS px → reference-density DEVICE px.
 *
 * The web module authors every length in CSS px and multiplies by
 * apparentScale(cssBox), which clamps to 0.5 on a phone-shaped viewport. A CSS
 * px is a dp, and FIELD_REFERENCE_DENSITY turns a dp into 3.1 device px:
 *     0.5 × 3.1 = 1.55
 * Keeping this factor explicit lets every LENGTH in the kinds table below stay
 * comparable to fog.js, so the two surfaces can still be diffed.
 *
 * Note it is applied to VELOCITY as well as size. The web applies `st.geo` to
 * radii but not to `vx`; on a phone-shaped box geo IS 0.5, so scaling both by
 * the same factor reproduces the web-on-a-phone rendering exactly — and it is
 * the only spelling that satisfies the Android DPI law (a velocity that skipped
 * `scale` would make the fog cross a 3.5×-density screen slower).
 */
internal const val FOG_WEB_TO_REF_PX = 1.55f

/** dp² of the viewport the web's `fieldScale()` normalises against (1440×900).
 *  Population is viewport-AREA derived, in logical (dp) units — never pixels. */
private const val FOG_REF_AREA_DP = 1440f * 900f

private const val FOG_TAU = 6.2831855f
private const val FOG_RAD_TO_DEG = 57.29578f

/** Memo keys for this effect's private FieldResources assets. Prefixed with the
 *  effect id so two effects can never collide in the shared `extras` map. */
private const val FOG_SPRITE_KEY = "fog.ramp"
private const val FOG_PAINT_KEY = "fog.paint"

/**
 * Colour ramp, indexed by LIFE (young → old). Never a flat grey: a veil is born
 * cool slate (fresh, backlit) and ages through neutral into warm dust, which is
 * what stops two dozen overlapping quads from reading as one grey sheet.
 * Byte-identical to fog.js — the alpha does the work, not the colour.
 */
internal val FOG_RAMP = arrayOf(
    intArrayOf(172, 186, 204),   // cool slate — freshly formed
    intArrayOf(186, 190, 196),   // neutral grey
    intArrayOf(197, 192, 184),   // dust
    intArrayOf(190, 180, 168),   // warm, dissipating
)

// -----------------------------------------------------------------------------
// The sprite: one soft radial puff per ramp stop.
//
// Baked at 160 px rather than the shared atlas' 64 px because a fog quad is
// magnified 7–15× (a bank is ~1200 device px across): a 64 px gradient stretched
// that far BANDS on an OLED, and banding is the one artefact that says "this is
// a stretched bitmap" instead of "this is fog". 4 × 160² × 4 B ≈ 410 KB, baked
// once per (density, effect) and freed with the effect.
//
// The falloff is kept as DATA rather than a literal inside the bake so
// FogFieldTest can INTEGRATE it: the number that decides whether a backdrop is
// legible is the AREA-WEIGHTED MEAN alpha of a quad (a paragraph of text spans
// the whole quad), not its sub-pixel core — see [fogSpriteMeanAlpha]. Two
// parallel arrays, not an Array<Pair>, so the constant costs no boxed pairs at
// class-init.
//
// The profile is fuller than the shared bake's (mean 0.339 vs 0.266) — a fog
// bank is a volume, not a point light — while the outer 25% still eases to zero,
// so at the ceiling alpha the outermost visible ring is ~1.6% and there is no
// ellipse edge to see.
// -----------------------------------------------------------------------------
private const val FOG_SPRITE_PX = 160

internal val FOG_SPRITE_STOPS = floatArrayOf(0.00f, 0.20f, 0.38f, 0.56f, 0.74f, 0.88f, 1.00f)
internal val FOG_SPRITE_ALPHAS = floatArrayOf(1.00f, 0.90f, 0.71f, 0.46f, 0.22f, 0.08f, 0.00f)

/**
 * AREA-WEIGHTED MEAN alpha of the sprite: ∫₀¹ 2r·α(r) dr over the piecewise-
 * linear falloff above, evaluated exactly (α is linear on each segment, so the
 * integral is closed-form — no sampling, no drift between the bake and the test).
 *
 * This is THE conversion between "peak quad alpha" and "how much this quad
 * actually lifts the screen", and it is what FogFieldTest multiplies the drawn
 * alphas by to measure the backdrop's average luminance. Pure maths, no state.
 */
internal fun fogSpriteMeanAlpha(): Float {
    var acc = 0f
    for (i in 0 until FOG_SPRITE_STOPS.size - 1) {
        val r0 = FOG_SPRITE_STOPS[i]
        val r1 = FOG_SPRITE_STOPS[i + 1]
        val dr = r1 - r0
        if (dr <= 0f) continue
        val a0 = FOG_SPRITE_ALPHAS[i]
        val m = (FOG_SPRITE_ALPHAS[i + 1] - a0) / dr
        val d2 = r1 * r1 - r0 * r0
        val d3 = r1 * r1 * r1 - r0 * r0 * r0
        acc += a0 * d2 + 2f * m * (d3 / 3f - r0 * d2 / 2f)
    }
    return acc
}

private fun bakeFogSprite(r: Int, g: Int, b: Int, density: Density): android.graphics.Bitmap {
    val bitmap = ImageBitmap(FOG_SPRITE_PX, FOG_SPRITE_PX)
    // Fully-qualified bitmap-backed Canvas factory (distinct from the @Composable Canvas).
    val canvas = androidx.compose.ui.graphics.Canvas(bitmap)
    val drawScope = CanvasDrawScope()
    val radius = FOG_SPRITE_PX / 2f
    val center = Offset(radius, radius)
    val base = Color(r / 255f, g / 255f, b / 255f, 1f)
    drawScope.draw(
        density,
        LayoutDirection.Ltr,
        canvas,
        Size(FOG_SPRITE_PX.toFloat(), FOG_SPRITE_PX.toFloat()),
    ) {
        drawCircle(
            brush = Brush.radialGradient(
                // Built from the arrays above so the bake and the legibility
                // integral can never drift apart. Allocates — and may: this runs
                // four times per density, never in a frame.
                colorStops = Array(FOG_SPRITE_STOPS.size) { i ->
                    FOG_SPRITE_STOPS[i] to base.copy(alpha = FOG_SPRITE_ALPHAS[i])
                },
                center = center,
                radius = radius,
            ),
            radius = radius,
            center = center,
        )
    }
    return bitmap.asAndroidBitmap()
}

/**
 * Fog's PRIVATE bitmap paint. Deliberately NOT [FieldPaints.sprite]: that one
 * carries [FIELD_BLEND] (additive from API 28 up), and additive is exactly what
 * two dozen overlapping veils behind body text must not be. Default Paint
 * blending is SrcOver, which is what we want, so there is nothing to set beyond
 * filtering.
 */
private fun newFogPaint(): android.graphics.Paint =
    android.graphics.Paint(android.graphics.Paint.FILTER_BITMAP_FLAG).apply { isAntiAlias = true }

/**
 * THE TWO SUB-POPULATIONS.
 *
 *   bank — the slow deep layer that gives the black its depth: huge, ponderous,
 *          long-lived, turning slowly, sitting low in the frame.
 *   wisp — the quicker shreds in front of them: smaller, thinner, shorter-lived,
 *          bobbing/swaying harder and spinning ~3× faster.
 *
 * The SPEED difference IS the parallax, and the SIZE difference is what makes it
 * read as two layers rather than one population at two distances.
 *
 * [rxMin]/[rxMax] is the LONG (horizontal) radius in WEB CSS px (see
 * [FOG_WEB_TO_REF_PX]); the quad is an ellipse (rx / aspect) because a rotating
 * radially-symmetric circle is invisible — rotation only reads on a
 * non-circular quad. Times are seconds; [spinMin]/[spinMax], [bobHzMin] and
 * [breatheHzMin] are RADIANS PER SECOND (the web's field names say "Hz" and are
 * wrong by 2π — a rate of 0.05 there is one cycle every two minutes, which is
 * how the effect came to be static); the rest is dimensionless.
 *
 * Every [alphaMax] here is below [FOG_MAX_VEIL_ALPHA] by construction, every
 * oscillator completes a full cycle inside [lifeMin], and FogFieldTest asserts
 * both at the table level so a future tuning pass cannot quietly undo either.
 */
internal class FogVeilKind(
    val share: Float,
    val rxMin: Float, val rxMax: Float,
    val aspectMin: Float, val aspectMax: Float,
    val alphaMin: Float, val alphaMax: Float,
    val vxMin: Float, val vxMax: Float,
    val vy: Float,
    val bobMin: Float, val bobMax: Float,
    val bobHzMin: Float, val bobHzMax: Float,
    val swayMin: Float, val swayMax: Float,
    val spinMin: Float, val spinMax: Float,
    val lifeMin: Float, val lifeMax: Float,
    val yMin: Float, val yMax: Float,
    val breatheMin: Float, val breatheMax: Float,
    val breatheHzMin: Float, val breatheHzMax: Float,
    val hue0Lo: Int, val hue0Hi: Int,
)

internal val FOG_BANK = FogVeilKind(
    share = 0.6f,
    rxMin = 180f, rxMax = 390f, aspectMin = 1.7f, aspectMax = 2.6f,
    alphaMin = 0.135f, alphaMax = 0.200f,
    vxMin = 26f, vxMax = 56f, vy = 3.6f,
    bobMin = 10f, bobMax = 26f, bobHzMin = 0.30f, bobHzMax = 0.62f,
    swayMin = 8f, swayMax = 20f,
    spinMin = 0.020f, spinMax = 0.046f,
    lifeMin = 26f, lifeMax = 48f, yMin = 0.34f, yMax = 1.08f,
    breatheMin = 0.10f, breatheMax = 0.20f, breatheHzMin = 0.30f, breatheHzMax = 0.55f,
    hue0Lo = 0, hue0Hi = 1,
)

internal val FOG_WISP = FogVeilKind(
    share = 0.4f,
    rxMin = 95f, rxMax = 180f, aspectMin = 2.2f, aspectMax = 3.4f,
    alphaMin = 0.085f, alphaMax = 0.135f,
    vxMin = 68f, vxMax = 140f, vy = 9.0f,
    bobMin = 18f, bobMax = 40f, bobHzMin = 0.70f, bobHzMax = 1.25f,
    swayMin = 16f, swayMax = 38f,
    spinMin = 0.052f, spinMax = 0.115f,
    lifeMin = 11f, lifeMax = 20f, yMin = 0.14f, yMax = 1.02f,
    breatheMin = 0.20f, breatheMax = 0.36f, breatheHzMin = 0.65f, breatheHzMax = 1.15f,
    hue0Lo = 1, hue0Hi = 2,
)

// -----------------------------------------------------------------------------
// Pure maths — no state, no allocation, unit-testable on its own.
// -----------------------------------------------------------------------------

/** smoothstep with the input CLAMPED (the web spelling — envelope() feeds it
 *  values well outside [0,1] and relies on the clamp). */
private fun fogSmooth(t: Float): Float {
    val s = if (t < 0f) 0f else if (t > 1f) 1f else t
    return s * s * (3f - 2f * s)
}

/** ALPHA envelope over life (1 → 0): eases in as the veil forms, out as it
 *  dissipates, so a recycled slot never pops into or out of existence. */
internal fun fogEnvelope(life: Float): Float =
    min(fogSmooth((1f - life) / 0.18f), fogSmooth(life / 0.30f))

/** SIZE-OVER-LIFE curve: a veil dilates monotonically as it ages, 0.62 → 1.0.
 *  Multiplied by the particle's own sizeEnv (and its own breathe) at draw time,
 *  so no two veils dilate to the same size. [age] is 1 - life. */
internal fun fogSwell(age: Float): Float = 0.62f + 0.38f * fogSmooth(age)

/**
 * The web's `fieldScale()`, in Android units: "how busy is this viewport",
 * folded with the operator's count dial.
 *
 * [areaDp] is the viewport area in LOGICAL (dp) units — never device pixels, so
 * a 2× density screen gets the same population, not four times as much fog.
 * Both clamps are the engine's, verbatim.
 */
internal fun fogFieldScale(areaDp: Float, countScale: Float): Float =
    ((areaDp / FOG_REF_AREA_DP).coerceIn(0.4f, 2.4f) * countScale).coerceIn(0.12f, 4.0f)

/** Population target. Bounded HARD at both ends — this is a backdrop, and the
 *  ceiling is what keeps the frame at ~24 quads on the largest viewport. A
 *  phone-shaped box floors at fieldScale 0.4 and gets 14. */
internal fun fogVeilCount(fieldScale: Float): Int =
    (12f + 6f * fieldScale).roundToInt().coerceIn(FOG_MIN_VEILS, FOG_MAX_VEILS)

/**
 * THE DRAW-SITE CEILING, and the ONLY place the Intensity dial reaches fog.
 *
 * [budgetK] is always ≤ 1, so on its own the clamp would be a backstop — but
 * [alphaScale] (FieldPaints.alphaScale, i.e. ParticleTuning.brightnessScale,
 * 0.55–1.6×) can push a quad past the ceiling, and this coerceAtMost is what
 * stops it. Fog draws through its own SrcOver paint and therefore never passes
 * through drawSpriteF, so if this multiply is deleted the dial silently stops
 * brightening the effect, and if the clamp is deleted the legibility budget
 * silently stops holding. Shared with FogFieldTest so the assertions test the
 * shipping code, not a copy of it.
 */
internal fun fogQuadAlpha(a: Float, budgetK: Float, alphaScale: Float): Float {
    val dial = if (alphaScale.isFinite() && alphaScale > 0f) alphaScale else 1f
    return (a * budgetK * dial).coerceAtMost(FOG_MAX_VEIL_ALPHA).coerceAtLeast(0f)
}

// -----------------------------------------------------------------------------
// One veil. A POOLED slot: recycled in place, never reallocated, so the hot path
// allocates nothing at all. `wisp` is a property of the SLOT, not of the draw —
// a bank does not become a wisp when it is recycled.
//
// Lengths (rx, ry, vx, vy, bobAmp, swayAmp, marginX/Y) are WEB CSS px and are
// multiplied by FogSim.g at USE, never baked in, so a density or size change
// re-scales the live field instead of stranding it.
// -----------------------------------------------------------------------------
class FogVeil internal constructor(val wisp: Boolean) {
    var x = 0f; var y = 0f
    var rx = 0f                 // long (horizontal) radius, web CSS px
    var ry = 0f                 // short radius — always < rx, or rotation is invisible
    var sizeEnv = 1f            // per-particle size-envelope multiplier ── premium rule 1
    /** The largest SIZE multiplier this veil can ever be drawn at: its own
     *  sizeEnv × its own breathe peak (fogSwell tops out at exactly 1). */
    var swellMax = 1f
    /** Half-width of the veil's maximum DRAWN footprint including the draw-time
     *  sway, in web CSS px. This — not `rx` — is the wrap margin and the upwind
     *  spawn offset: a veil that wrapped on its bare radius would teleport with
     *  up to two thirds of itself still on screen (swellMax reaches 1.66). */
    var marginX = 0f
    /** Same, vertically, including the draw-time bob. */
    var marginY = 0f
    var alpha0 = 0f             // this veil's own alpha, before envelope + budget + dial
    var a = 0f                  // CURRENT per-quad alpha (already ceiling-clamped)
    var vx = 0f                 // downwind drift, web CSS px/s
    var vy = 0f                 // slow vertical layering, web CSS px/s
    var bobAmp = 0f             // draw-time vertical bob, web CSS px
    var bobHz = 0f              // rad/s
    var swayAmp = 0f            // draw-time horizontal sway, web CSS px
    var swayHz = 0f             // rad/s, incommensurate with bobHz → a lissajous roll
    var breatheAmp = 0f         // draw-time size oscillation, dimensionless
    var breatheHz = 0f          // rad/s
    var phase = 0f              // private offset shared by bob + sway + breathe
    var rot = 0f                // radians
    var spin = 0f               // rad/s, signed
    var life = 1f               // 1 → 0
    var decay = 0f              // 1 / lifetime-in-seconds
    var hue0 = 0                // base index into FOG_RAMP ── premium rule 2
}

/**
 * FogSim — the effect. Registered in FieldRegistry.kt as ("fog", "Low Fog");
 * nothing else in the codebase names it.
 *
 * [countScale] is the intensity × quality budget multiplier (ParticleTuning) —
 * it reaches the POPULATION only, exactly as the web folds its dials into
 * env.scale. It must never reach an alpha; the Intensity dial's BRIGHTNESS
 * channel is a separate, draw-phase-only input (FieldPaints.alphaScale, applied
 * in [fogQuadAlpha] under the hard per-quad ceiling).
 *
 * [rand] is the injectable randomness seam, so FogFieldTest can drive the whole
 * simulation from a seeded generator.
 */
class FogSim(
    countScale: Float = 1f,
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f
    private var height = 0f
    /** device px per reference-density px — the ONE place DPI is allowed to land. */
    private var scale = 1f
    /** Screen density, kept only to derive the dp-area population target.
     *  NOT named `density`: render() is a DrawScope member extension and
     *  DrawScope.density would shadow it. */
    private var deviceDensity = FIELD_REFERENCE_DENSITY
    private val countScale = ParticleTuning.sanitizeScale(countScale)

    private var pool = emptyArray<FogVeil>()

    /** ONE prevailing wind for the whole field (-1 or +1), drawn once at init.
     *  A counter-drifting veil would break the weather illusion instantly. */
    var wind = 1f
        private set

    /** Field-wide alpha normalizer, recomputed once per frame in [update].
     *  Always in (0, 1]: 1 when the field is under budget, ALPHA_BUDGET/sum when
     *  it is not. This is the ceiling a per-quad cap alone cannot give you. */
    var budgetK = 1f
        private set

    /** Private phase clock for the bob/sway/breathe oscillators, advanced by
     *  dtSec. Double because a long session would run a Float out of precision. */
    private var clock = 0.0

    /** Test/inspection view of the pool. Never called from the hot path. */
    val veils: List<FogVeil> get() = pool.asList()

    /** Web CSS px → device px for THIS screen. Applied at use, never stored. */
    private val g: Float get() = scale * FOG_WEB_TO_REF_PX

    private fun rnd(): Float = rand.next().toFloat()
    private fun pick(lo: Float, hi: Float): Float = lo + rnd() * (hi - lo)

    private fun targetCount(): Int =
        fogVeilCount(fogFieldScale((width / deviceDensity) * (height / deviceDensity), countScale))

    override fun resize(width: Float, height: Float, scale: Float, density: Float) {
        if (width <= 0f || height <= 0f) return
        val first = pool.isEmpty()
        val sameSize = !first && width == this.width && height == this.height
        this.width = width; this.height = height
        this.scale = scale; this.deviceDensity = density
        if (first) { initField(); return }
        if (sameSize) return
        // Population is viewport-derived; GEOMETRY is not, so only the count and
        // the apparent scale move. Drifting veils are left exactly where they are
        // — a rebuild on every resize would visibly restart the weather, and this
        // runs on every composer-driven size change. Nothing can be stranded: the
        // wrap in update() is computed against the CURRENT width/height, so a veil
        // outside a shrunken viewport re-enters on the very next frame.
        retarget(initial = false)
    }

    /** First valid size (or a rearm on an empty field): draw the wind, fill the
     *  pool with a STAGGERED set of lifetimes so the field cannot die as a cohort. */
    private fun initField() {
        wind = if (rnd() < 0.5f) -1f else 1f
        budgetK = 1f
        retarget(initial = true)
    }

    /**
     * Grow/trim the pool to the viewport's target IN PLACE. Survivors keep their
     * slot object — and therefore their drift, their phase and their age.
     *
     * Kinds are assigned the way the web assigns them: fill banks up to
     * round(target × bank share), then wisps. That makes the mix exact at ANY
     * population (14 veils → 8 banks / 6 wisps; 24 → 14 / 10), which a per-veil
     * coin flip could not promise — a field of two dozen wisps and no banks has
     * no depth at all.
     */
    private fun retarget(initial: Boolean) {
        val want = targetCount()
        if (pool.size == want) return
        val old = pool
        val keep = min(old.size, want)
        var banks = 0
        for (i in 0 until keep) if (!old[i].wisp) banks++
        val wantBanks = (want * FOG_BANK.share).roundToInt()
        // Decide the new slots' species FIRST (a plain loop, not a captured var
        // inside the Array initialiser) — this runs on resize only, never in a frame.
        val newIsWisp = BooleanArray(want)
        for (i in keep until want) {
            val wisp = banks >= wantBanks
            if (!wisp) banks++
            newIsWisp[i] = wisp
        }
        pool = Array(want) { i ->
            if (i < keep) old[i] else FogVeil(newIsWisp[i]).also { reseed(it, initial) }
        }
    }

    /**
     * (Re)seed one veil IN PLACE. Every recycle reuses the same object, so the
     * pool is allocated once and update/render allocate nothing, ever.
     *
     * [initial] staggers the first fill across the screen and across the life
     * cycle; a recycled veil instead re-enters from the UPWIND edge, off-screen
     * by its full DRAWN margin, and lets the envelope fade it in — a veil must
     * never pop into existence in the middle of the frame.
     */
    private fun reseed(p: FogVeil, initial: Boolean) {
        val k = if (p.wisp) FOG_WISP else FOG_BANK
        p.rx = pick(k.rxMin, k.rxMax)
        p.ry = p.rx / pick(k.aspectMin, k.aspectMax)
        p.sizeEnv = 0.82f + rnd() * 0.42f
        p.alpha0 = pick(k.alphaMin, k.alphaMax)
        p.vx = wind * pick(k.vxMin, k.vxMax)        // one prevailing wind for the field
        p.vy = (rnd() - 0.5f) * 2f * k.vy           // slow vertical layering
        p.bobAmp = pick(k.bobMin, k.bobMax)
        p.bobHz = pick(k.bobHzMin, k.bobHzMax)
        p.swayAmp = pick(k.swayMin, k.swayMax)
        // Incommensurate with the bob on purpose: equal rates would trace a
        // straight diagonal, which reads as a slide. A ratio near — but never at
        // — 2/3 traces an open lissajous, which reads as a roll.
        p.swayHz = p.bobHz * (0.52f + rnd() * 0.36f)
        p.breatheAmp = pick(k.breatheMin, k.breatheMax)
        p.breatheHz = pick(k.breatheHzMin, k.breatheHzMax)
        p.phase = rnd() * FOG_TAU
        p.rot = rnd() * FOG_TAU
        p.spin = pick(k.spinMin, k.spinMax) * (if (rnd() < 0.5f) -1f else 1f)
        p.decay = 1f / pick(k.lifeMin, k.lifeMax)
        p.hue0 = k.hue0Lo + (rnd() * (k.hue0Hi - k.hue0Lo + 1)).toInt()
        p.y = height * pick(k.yMin, k.yMax)
        // DERIVED, once per life: the veil's maximum drawn footprint. Recomputed
        // here rather than per frame because every input is fixed for this life.
        p.swellMax = p.sizeEnv * (1f + p.breatheAmp)
        p.marginX = p.rx * p.swellMax + p.swayAmp
        p.marginY = p.ry * p.swellMax + p.bobAmp
        if (initial) {
            p.x = rnd() * width
            p.life = 0.15f + rnd() * 0.85f          // stagger: they must not all die together
        } else {
            p.x = if (wind > 0f) -p.marginX * g else width + p.marginX * g
            p.life = 1f
        }
        p.a = 0f
    }

    /**
     * [active] is deliberately ignored, as it is on the web: fog is WEATHER, and
     * weather is continuous. There is nothing to drain and nothing to thin out —
     * EmberOverlay fades the whole canvas and parks the loop at DRAIN_MAX_MS,
     * which is the drain. [nowMs] is ignored too; see [clock].
     */
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) {
        if (pool.isEmpty() || width <= 0f || height <= 0f) return
        clock += dtSec.toDouble()
        val gg = g
        var sum = 0f
        for (p in pool) {
            p.life -= p.decay * dtSec
            if (p.life <= 0f) reseed(p, initial = false)   // recycle in place; the pool never grows
            // All motion is delta-timed: identical distance per second at 60 and 120 Hz.
            p.x += p.vx * gg * dtSec
            p.y += p.vy * gg * dtSec
            p.rot += p.spin * dtSec
            if (p.rot > FOG_TAU) p.rot -= FOG_TAU else if (p.rot < -FOG_TAU) p.rot += FOG_TAU
            // Wrap by the veil's own MAXIMUM DRAWN extent (radius × swellMax plus
            // its sway/bob amplitude) so a 1200 px bank never clips at an edge —
            // a hard-edged fog bank is the most obvious "these are quads" tell
            // there is, and at these speeds veils wrap constantly.
            val mx = p.marginX * gg
            val my = p.marginY * gg
            if (p.x < -mx) p.x = width + mx else if (p.x > width + mx) p.x = -mx
            if (p.y < -my) p.y = height + my else if (p.y > height + my) p.y = -my
            // Per-quad ceiling #1 (compute site).
            p.a = (p.alpha0 * fogEnvelope(p.life)).coerceAtMost(FOG_MAX_VEIL_ALPHA)
            sum += p.a
        }
        // Field-wide ceiling: scale the WHOLE field down when the stack would
        // exceed the budget. One divide per frame buys a bounded luminance lift,
        // which is the only thing standing between this effect and unreadable
        // body text.
        budgetK = if (sum > FOG_ALPHA_BUDGET) FOG_ALPHA_BUDGET / sum else 1f
    }

    /** Weather is continuous: unlike embers there is nothing to drain and nothing
     *  to re-seed, so a rearm only has to guarantee the field EXISTS. Touching a
     *  live veil here would restart the weather on every generation. */
    override fun rearm() {
        if (pool.isNotEmpty() || width <= 0f || height <= 0f) return
        initField()
    }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
        if (pool.isEmpty()) return
        // ── hoisted: one lookup per FRAME, never per particle ──
        // Our own grey ramp, baked large (see FOG_SPRITE_PX) and memoized under
        // our own key. Fog never touches res.atlas, so the warm ember atlas is
        // never baked while this field is selected.
        val ramp: Array<android.graphics.Bitmap> = res.bake(FOG_SPRITE_KEY) { d ->
            Array(FOG_RAMP.size) { i ->
                val c = FOG_RAMP[i]
                bakeFogSprite(c[0], c[1], c[2], d)
            }
        }
        // Our own SrcOver paint. FieldPaints.sprite carries FIELD_BLEND (additive
        // ≥ API 28) which every OTHER field wants and this one must not have —
        // see the header. Baked once per (density, effect) alongside the sprites,
        // so this is still zero allocation per frame; FieldResources is
        // remembered PER OVERLAY and only ever touched from the draw phase, so a
        // mutable paint living in it is safe by the same rule FieldPaints is.
        val paint: android.graphics.Paint = res.bake(FOG_PAINT_KEY) { _ -> newFogPaint() }
        val top = ramp.size - 1
        val ts = clock
        val gg = g
        val k = budgetK
        // The Intensity dial's BRIGHTNESS channel. drawSpriteF applies this for
        // every sprite-based field; fog draws through its own paint, so it has to
        // do it by hand — see fogQuadAlpha, which also holds the hard ceiling.
        val dial = paints.alphaScale
        val canvas = drawContext.canvas.nativeCanvas
        val dst = paints.dst                       // per-overlay scratch; never ours to keep
        for (p in pool) {
            // Per-quad ceiling #2 (draw site). THE line that must never be deleted.
            val a = fogQuadAlpha(p.a, k, dial)
            if (a <= 0.003f) continue              // same cull threshold as drawSpriteF
            val age = 1f - p.life
            // Breathe: a private size oscillation, on this veil's own rate and
            // phase, completing at least a full cycle within its life so the quad
            // visibly swells and settles instead of sitting at a fixed size.
            val breathe = 1f + p.breatheAmp * sin(ts * p.breatheHz + p.phase).toFloat()
            // SIZE = life curve × per-particle envelope × breathe. Never constant.
            val sw = fogSwell(age) * p.sizeEnv * breathe
            val rx = p.rx * gg * sw
            val ry = p.ry * gg * sw
            if (rx <= 0.1f || ry <= 0.1f) continue
            // COLOUR RAMP indexed by life: cool slate when fresh, warm dust when
            // dissipating, each sub-population starting on its own stop.
            val idx = (p.hue0 + (age * 2f).roundToInt()).coerceIn(0, top)
            // Bob + sway: a draw-time lissajous on top of the linear drift, which
            // is what turns "sliding rectangle of grey" into "rolling bank". Both
            // are inside marginX/marginY, so neither can expose an edge at a wrap.
            val x = p.x + sin(ts * p.swayHz + p.phase * 1.7f).toFloat() * p.swayAmp * gg
            val y = p.y + sin(ts * p.bobHz + p.phase).toFloat() * p.bobAmp * gg
            paint.alpha = (a * 255f).roundToInt()
            // Rotate about the veil's own centre. The quad is an ELLIPSE, which is
            // the only reason the (slow) spin is visible at all.
            dst.set(-rx, -ry, rx, ry)
            val restore = canvas.save()
            canvas.translate(x, y)
            canvas.rotate(p.rot * FOG_RAD_TO_DEG)
            canvas.drawBitmap(ramp[idx], null, dst, paint)
            canvas.restoreToCount(restore)
        }
    }
}
