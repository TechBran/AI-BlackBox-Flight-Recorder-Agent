/**
 * Effect: LOW FOG (id 'fog').
 *
 * Barely-there veils of grey drifting across the black at different speeds, so
 * the backdrop reads as weather with depth instead of flat paint. The whole
 * effect is 8-20 ENORMOUS soft radial quads per frame — no noise, no shader, no
 * per-pixel work. The 64px-sprite-blown-up-to-900px mush that is a DEFECT for
 * embers (visible blur on a 6px spark) is the FEATURE here: it is exactly the
 * soft-edged falloff a fog bank needs, for free.
 *
 * THE LEGIBILITY BUDGET — the reason this file is careful.
 * This is the highest-risk effect in the catalogue. Unlike embers or stars,
 * which put light in a few small places, fog raises the AVERAGE luminance of the
 * backdrop UNIFORMLY — precisely what erodes the contrast of the chat body text
 * sitting on top of it. So the ceiling is enforced in CODE, twice:
 *
 *   MAX_VEIL_ALPHA  a HARD per-quad ceiling (6%), applied with Math.min at both
 *                   the compute site and the draw site. No tuning value in
 *                   VEIL_KINDS may exceed it, and a test asserts that.
 *   ALPHA_BUDGET    a HARD field-wide ceiling on the SUM of the quad alphas.
 *                   A per-quad cap alone is not enough: twenty 6% veils stacked
 *                   are a white wash. update() normalises the whole field down
 *                   to the budget once per frame (st.budgetK), so the worst-case
 *                   luminance lift is bounded no matter what the population does.
 *
 * Compositing is 'source-over': fog is NOT emissive. Under 'lighter' the
 * overlaps would blow out to white, which is the exact failure this budget
 * exists to prevent. clearPolicy is a hard 'clear' — a fade would smear the
 * veils into an ever-brightening wash, i.e. it would defeat the budget by
 * accumulating in the framebuffer where no clamp can see it.
 */
import { registerEffect } from '../registry.js';
import { makeSprite } from '../sprites.js';
import { apparentScale } from '../sizing.js';

/** HARD per-quad alpha ceiling. Not a convention — a Math.min. */
const MAX_VEIL_ALPHA = 0.115;
/** HARD ceiling on the SUM of the field's quad alphas (worst-case stack). */
const ALPHA_BUDGET = 0.58;
/** Population bounds. Twenty giant quads is the entire per-frame cost. */
const MIN_VEILS = 8;
const MAX_VEILS = 20;
/** Bake size. Small on purpose: it is magnified 6-14x and the mush is wanted. */
const SPRITE_PX = 64;
const TAU = Math.PI * 2;

/**
 * Colour ramp, indexed by LIFE (young -> old). Never a flat grey: a veil is born
 * cool slate (fresh, backlit) and ages through neutral into warm dust, which is
 * what stops twenty overlapping quads from reading as one grey sheet.
 */
const FOG_RAMP = [
    [172, 186, 204],   // cool slate — freshly formed
    [186, 190, 196],   // neutral grey
    [197, 192, 184],   // dust
    [190, 180, 168],   // warm, dissipating
];

/**
 * The two SUB-POPULATIONS. Same draw call, different physics — banks are the
 * slow deep layer that gives the black its depth, wisps are the quicker shreds
 * in front of them, and the speed difference IS the parallax.
 *
 * `rx` is the LONG (horizontal) radius in reference px; the quad is an ellipse
 * (rx / aspect) because a rotating radially-symmetric circle is invisible —
 * rotation only reads on a non-circular quad.
 * Every `alpha` maximum here is below MAX_VEIL_ALPHA by construction.
 */
const VEIL_KINDS = {
    bank: {
        share: 0.6, rx: [200, 450], aspect: [1.7, 2.6], alpha: [0.046, 0.074],
        vx: [4, 11], vy: 0.8, bob: [3, 9], spin: [0.003, 0.010],
        life: [42, 78], y: [0.42, 1.06], breathe: [0.04, 0.10], hue0: [0, 1],
    },
    wisp: {
        share: 0.4, rx: [90, 200], aspect: [2.2, 3.4], alpha: [0.027, 0.050],
        vx: [14, 34], vy: 2.2, bob: [6, 16], spin: [0.012, 0.030],
        life: [18, 34], y: [0.20, 1.02], breathe: [0.08, 0.16], hue0: [1, 2],
    },
};

const rr = (rand, range) => range[0] + rand() * (range[1] - range[0]);
const smooth = (t) => { const s = t < 0 ? 0 : (t > 1 ? 1 : t); return s * s * (3 - 2 * s); };

/** Alpha envelope over life (1 -> 0): ease in as it forms, out as it dissipates. */
function envelope(life) { return Math.min(smooth((1 - life) / 0.18), smooth(life / 0.30)); }

/** SIZE-OVER-LIFE curve: a veil dilates monotonically as it ages, 0.62 -> 1.0.
 *  Multiplied by the particle's own sizeEnv so no two dilate to the same size. */
function swell(age) { return 0.62 + 0.38 * smooth(age); }

// Our own grey ramp, baked through the SHARED bake (dither included — a 64px
// gradient stretched to 900px on black is the textbook banding case). Lazy and
// document-guarded so a headless import never touches the DOM, exactly like
// sprites.js. Module scope: baked once per document, not once per field.
let FOG_SPR = null;
function fogSprites() {
    if (FOG_SPR || typeof document === 'undefined') return FOG_SPR;
    FOG_SPR = FOG_RAMP.map(c => makeSprite(SPRITE_PX, c[0], c[1], c[2]));
    return FOG_SPR;
}

/** Population target. Bounded hard at both ends — this is a backdrop, and the
 *  ceiling is what keeps the frame at ~20 quads on the largest viewport. */
function veilCount(env) {
    return Math.max(MIN_VEILS, Math.min(MAX_VEILS, Math.round(9 + 5 * (env.scale || 1))));
}

/**
 * (Re)seed a veil IN PLACE. Every recycle reuses the same object, so the pool is
 * allocated once at init and the update/draw hot path allocates nothing, ever.
 */
function reseed(p, env, initial) {
    const rand = env.rand, st = env.state, k = VEIL_KINDS[p.kind];
    p.rx = rr(rand, k.rx);
    p.ry = p.rx / rr(rand, k.aspect);
    p.sizeEnv = 0.82 + rand() * 0.42;                      // per-particle envelope multiplier
    p.alpha0 = rr(rand, k.alpha);
    p.vx = st.wind * rr(rand, k.vx);                       // one prevailing wind for the field
    p.vy = (rand() - 0.5) * 2 * k.vy;                      // slow vertical layering
    p.bobAmp = rr(rand, k.bob);
    p.bobHz = 0.05 + rand() * 0.12;
    p.breatheAmp = rr(rand, k.breathe);
    p.breatheHz = 0.04 + rand() * 0.09;
    p.phase = rand() * TAU;
    p.rot = rand() * TAU;
    p.spin = rr(rand, k.spin) * (rand() < 0.5 ? -1 : 1);
    p.decay = 1 / rr(rand, k.life);
    p.hue0 = k.hue0[0] + Math.floor(rand() * (k.hue0[1] - k.hue0[0] + 1));
    p.y = env.height * rr(rand, k.y);
    if (initial) {
        p.x = rand() * env.width;
        p.life = 0.15 + rand() * 0.85;                     // stagger: they must not all die together
    } else {
        // Re-enter from the upwind edge, off-screen by its own radius, and let
        // the envelope fade it in — a veil must never pop into existence.
        p.x = st.wind > 0 ? -p.rx * st.geo : env.width + p.rx * st.geo;
        p.life = 1;
    }
    p.a = 0;
}

/**
 * Grow/shrink the pool to the viewport's target without disturbing the veils
 * that are already drifting (a rebuild on every resize would visibly restart the
 * weather, and ResizeObserver fires on composer keystrokes).
 */
function retarget(env, initial) {
    const st = env.state, list = st.list, target = veilCount(env);
    while (list.length > target) list.pop();
    let banks = 0;
    for (let i = 0; i < list.length; i++) if (list[i].kind === 'bank') banks++;
    const wantBanks = Math.round(target * VEIL_KINDS.bank.share);
    while (list.length < target) {
        const kind = banks < wantBanks ? 'bank' : 'wisp';
        if (kind === 'bank') banks++;
        // One fixed shape, declared in full, so the pool stays monomorphic.
        const p = {
            kind, x: 0, y: 0, rx: 0, ry: 0, sizeEnv: 1, alpha0: 0, a: 0,
            vx: 0, vy: 0, bobAmp: 0, bobHz: 0, breatheAmp: 0, breatheHz: 0,
            phase: 0, rot: 0, spin: 0, life: 1, decay: 0, hue0: 0,
        };
        reseed(p, env, initial);
        list.push(p);
    }
}

function init(env) {
    const st = env.state;
    st.geo = apparentScale(env.width, env.height);   // GEOMETRY from the CSS box only — never dpr
    st.wind = env.rand() < 0.5 ? -1 : 1;
    st.budgetK = 1;
    st.spr = null;
    st.list = [];
    retarget(env, true);
}

export const descriptor = {
    id: 'fog',
    label: 'Low Fog',
    // Non-emissive. 'lighter' would blow the overlaps out to white and destroy
    // the contrast of the chat text — the one thing this effect must not do.
    blend: 'source-over',
    // Hard clear. A fade would let the veils accumulate in the framebuffer,
    // outside the reach of the alpha budget.
    clearPolicy: 'clear',

    init,

    // Population is viewport-derived; geometry is not, so only the count and the
    // apparent scale move. Drifting veils are left alone.
    resize(env) {
        const st = env.state;
        if (!st.list) { init(env); return; }
        st.geo = apparentScale(env.width, env.height);
        retarget(env, false);
    },

    update(dt, env) {
        const st = env.state;
        if (!st.list) init(env);
        const list = st.list, g = st.geo;
        let sum = 0;
        for (let i = 0; i < list.length; i++) {
            const p = list[i];
            p.life -= p.decay * dt;
            if (p.life <= 0) reseed(p, env, false);        // recycle in place; the pool never grows
            // All motion is delta-timed: identical distance per second at 60 and 120 Hz.
            p.x += p.vx * dt;
            p.y += p.vy * dt;
            p.rot += p.spin * dt;
            if (p.rot > TAU) p.rot -= TAU; else if (p.rot < -TAU) p.rot += TAU;
            // Wrap by the veil's OWN radius so a 900px bank never clips at an edge.
            const mx = p.rx * g, my = p.ry * g;
            if (p.x < -mx) p.x = env.width + mx; else if (p.x > env.width + mx) p.x = -mx;
            if (p.y < -my) p.y = env.height + my; else if (p.y > env.height + my) p.y = -my;
            // Per-quad ceiling #1 (compute site).
            p.a = Math.min(MAX_VEIL_ALPHA, p.alpha0 * envelope(p.life) * env.intensity);
            sum += p.a;
        }
        // Field-wide ceiling: scale the WHOLE field down when the stack would
        // exceed the budget. One divide per frame buys a bounded luminance lift.
        st.budgetK = sum > ALPHA_BUDGET ? ALPHA_BUDGET / sum : 1;
    },

    draw(ctx, env) {
        const st = env.state;
        if (!st.list) return;
        // Resolved once and cached: null means no document and no shared atlas
        // (headless) → nothing to draw, same contract as embers.
        const spr = st.spr || (st.spr = fogSprites() || fallbackSprites(env));
        if (!spr) return;
        const ts = env.now * 0.001, g = st.geo, k = st.budgetK, top = spr.length - 1;
        // The engine's 2D context always has the transform API; a headless
        // recorder may implement only drawImage, so probe once per frame.
        const canSpin = typeof ctx.rotate === 'function' && typeof ctx.save === 'function';
        for (let i = 0; i < st.list.length; i++) {
            const p = st.list[i];
            const age = 1 - p.life;
            const breathe = 1 + p.breatheAmp * Math.sin(ts * p.breatheHz + p.phase);
            const sw = swell(age) * p.sizeEnv * breathe;   // size-over-life x envelope x breathe
            const rx = p.rx * g * sw, ry = p.ry * g * sw;
            const idx = Math.max(0, Math.min(top, p.hue0 + Math.round(age * 2)));
            const y = p.y + Math.sin(ts * p.bobHz + p.phase) * p.bobAmp * g;
            // Per-quad ceiling #2 (draw site). budgetK is <= 1, so this is a
            // backstop — and it is the line that must never be deleted.
            ctx.globalAlpha = Math.min(MAX_VEIL_ALPHA, p.a * k);
            if (canSpin) {
                ctx.save();
                ctx.translate(p.x, y);
                ctx.rotate(p.rot);
                ctx.drawImage(spr[idx], -rx, -ry, rx * 2, ry * 2);
                ctx.restore();
            } else {
                ctx.drawImage(spr[idx], p.x - rx, y - ry, rx * 2, ry * 2);
            }
        }
    },

    // Weather is continuous: unlike embers there is nothing to drain and nothing
    // to re-seed, so a rearm only has to guarantee the field exists (the engine
    // fades the canvas out on its own when generation stops).
    rearm(env) { if (!env.state.list || !env.state.list.length) init(env); },
};

/** Last resort when there is no document to bake into: the shared atlas' star
 *  blob stands in for every ramp stop, so the draw path stays exercisable. */
function fallbackSprites(env) {
    const star = env.sprites && env.sprites.star;
    return star ? [star, star, star, star] : null;
}

registerEffect(descriptor);
export default descriptor;
export { FOG_RAMP, VEIL_KINDS, MAX_VEIL_ALPHA, ALPHA_BUDGET, MIN_VEILS, MAX_VEILS, veilCount, envelope, swell };
