/**
 * Effect: METEOR FALL (id 'meteors').
 *
 * A quiet two-layer starfield that never stops, and — every 13 to 27 seconds —
 * ONE hairline streak that rips diagonally across a corner and is gone before
 * you are sure you saw it. The population is 3-8 elements, almost always zero
 * of them alive. The effect is the WAIT; the streak is the punctuation.
 *
 * Three things here are load-bearing and look like omissions if you skim:
 *
 *   THE SCHEDULE READS NOTHING. Spawn timing is a private dt-accumulated clock
 *   (`st.clock`) and env.rand — never env.active, env.surge, env.now or any
 *   other engine signal. A streak that fired when generation started would be
 *   read as a status indicator, and once a user believes a backdrop means
 *   something they watch it instead of the chat. rearm() therefore leaves the
 *   schedule ALONE; it is the one hook that correlates with activity.
 *
 *   THE EDGE BUDGET IS SOLVED AT SPAWN, NOT CLAMPED AT DRAW. Every streak is
 *   aimed OUTWARD from its corner, and the spawn x is capped so that the head
 *   (which only ever moves further out) plus the tail reaching back inward both
 *   start inside the outer `edgeLimit` band. So no drawn pixel can enter the
 *   centre 40% of the width where chat text is read — no per-frame test, no
 *   clipping, and it is provable from the spawn parameters alone.
 *
 *   NO createLinearGradient. The spec look is a linear-gradient stroke from the
 *   head back to head - velocity*k, but a CanvasGradient bakes its endpoints, so
 *   a moving one must be re-allocated every frame for every meteor. Instead the
 *   tail is `segments` short strokes whose width tapers to a point and whose
 *   colour/alpha come from a quantized rgba() string table built ONCE at import.
 *   Same picture, zero allocation in the hot path.
 *
 * Geometry comes from apparentScale(cssW, cssH) and NEVER from env.dpr — the
 * dpr-magnification bug this engine shipped for its first year.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

const TAU = Math.PI * 2;

const METEOR_CONFIG = {
    pool: 8,                    // hard ceiling. A sparse field, never a dense one.
    first: [9, 19],             // seconds of quiet before the FIRST streak of a session
    silence: [16, 34],          // …and between every event after that (measured mean gap ~19 s)
    showerChance: 0.12,         // occasionally 2-3 in quick succession, then a long lull
    showerExtra: 2,
    burstGap: [0.30, 1.35],     // spacing WITHIN a shower
    angle: [0.44, 0.92],        // radians off vertical (25°-53°) — diagonal, never a rain
    edgeLimit: 0.28,            // no drawn pixel crosses this fraction of the width
    centreBox: 0.30,            // the centre 40% (0.30..0.70 on both axes) is for reading
    centreDim: 0.42,            // star alpha multiplier inside that box
    life: [0.35, 1.60],         // seconds, clamped. BRIEF is the whole point.
    ageShift: 0.22,             // how far along the ramp a streak drifts as it ages
    segments: 9,                // tail resolution
    fireballChance: 0.26,
    kinds: [
        // 0 — hairline dust: fastest, thinnest, blue-white, a blink
        { speed: [900, 1420], tailK: [0.090, 0.135], width: [0.85, 1.40], alpha: [0.42, 0.60], ramp0: [0.02, 0.12], span: 0.72, glow: 4.6 },
        // 1 — fireball: slower, thicker, warmer, lingers one beat longer
        { speed: [620, 950], tailK: [0.150, 0.200], width: [1.50, 2.50], alpha: [0.62, 0.80], ramp0: [0.18, 0.28], span: 0.62, glow: 6.2 },
    ],
    stars: {
        count: 44, farFraction: 0.68, min: 18, max: 90,
        layers: [
            // far dust: barely there, drifts imperceptibly, slow twinkle
            { r: [0.35, 0.95], a: [0.10, 0.22], drift: 1.6, tw: [0.35, 0.85] },
            // near: brighter, faster drift, quicker twinkle
            { r: [0.95, 1.75], a: [0.20, 0.40], drift: 3.2, tw: [0.70, 1.60] },
        ],
    },
};

/** white-hot -> ice blue -> pale gold -> amber -> deep ember. Indexed by LIFE. */
const METEOR_RAMP = [[255, 255, 255], [214, 236, 255], [255, 236, 190], [255, 168, 86], [188, 58, 20]];
/** cool blue-white -> warm white. Indexed by each star's own tone + twinkle. */
const STAR_RAMP = [[176, 206, 255], [226, 236, 255], [255, 250, 236], [255, 232, 196]];

const RAMP_STEPS = 20;
const ALPHA_STEPS = 28;
const RGB = { r: 0, g: 0, b: 0 };   // module scratch: rampAt() writes here, never allocates

/** Sample a ramp at 0..1 into `out`. */
function rampAt(ramp, t, out) {
    const f = (t <= 0 ? 0 : t >= 1 ? 1 : t) * (ramp.length - 1);
    const i = Math.min(ramp.length - 2, f | 0);
    const k = f - i, a = ramp[i], b = ramp[i + 1];
    out.r = Math.round(a[0] + (b[0] - a[0]) * k);
    out.g = Math.round(a[1] + (b[1] - a[1]) * k);
    out.b = Math.round(a[2] + (b[2] - a[2]) * k);
    return out;
}

/**
 * The allocation-free stand-in for a per-frame gradient: every rgba() string the
 * effect can ever need, quantized to RAMP_STEPS x ALPHA_STEPS and built once at
 * import. Pure data, no DOM, no randomness — safe to import headlessly.
 */
function buildStyles(ramp) {
    const out = new Array(RAMP_STEPS * ALPHA_STEPS);
    for (let i = 0; i < RAMP_STEPS; i++) {
        rampAt(ramp, i / (RAMP_STEPS - 1), RGB);
        for (let j = 0; j < ALPHA_STEPS; j++) {
            out[i * ALPHA_STEPS + j] = `rgba(${RGB.r},${RGB.g},${RGB.b},${(j / (ALPHA_STEPS - 1)).toFixed(3)})`;
        }
    }
    return out;
}
const METEOR_STYLES = buildStyles(METEOR_RAMP);
const STAR_STYLES = buildStyles(STAR_RAMP);

function styleAt(lut, t, a) {
    const ti = t <= 0 ? 0 : t >= 1 ? RAMP_STEPS - 1 : (t * (RAMP_STEPS - 1) + 0.5) | 0;
    const ai = a <= 0 ? 0 : a >= 1 ? ALPHA_STEPS - 1 : (a * (ALPHA_STEPS - 1) + 0.5) | 0;
    return lut[ti * ALPHA_STEPS + ai];
}

const pick = (rand, r) => r[0] + rand() * (r[1] - r[0]);

/**
 * SIZE-OVER-LIFE, shared by width, head radius and alpha. Smoothstep attack so a
 * streak fades IN over its first ~10 px instead of popping into existence, then a
 * long power taper so it thins to nothing rather than being culled mid-stroke.
 * Multiplied per particle by `envMul`, so no two streaks share an envelope.
 * @param {number} t age, 0 at birth -> 1 at death
 */
const ATTACK = 0.14;
function envelope(t) {
    if (t <= 0 || t >= 1) return 0;
    const a = t < ATTACK ? t / ATTACK : 1;
    return a * a * (3 - 2 * a) * Math.pow(1 - t, 1.35);
}

// ---------------------------------------------------------------------------
// Pools. Both are allocated once and reused forever; nothing here runs per frame.
// ---------------------------------------------------------------------------

function makeMeteor() {
    return {
        alive: false, kind: 0, x: 0, y: 0, ux: 0, uy: 0, speed: 0, tail: 0,
        life: 1, decay: 1, envMul: 1, width: 1, alpha: 0, ramp0: 0, span: 0.7, glow: 5,
    };
}

function makeStar() {
    return { x: 0, y: 0, r: 1, a: 0.2, vx: 0, tw: 0, tws: 1, envMul: 1, tone: 0.5, k: 0.8 };
}

function resetStar(s, env, st, layer) {
    const rand = env.rand;
    s.x = rand() * env.width;
    s.y = rand() * env.height;
    s.r = pick(rand, layer.r) * st.gs;
    s.a = pick(rand, layer.a);
    s.vx = (rand() - 0.5) * 2 * layer.drift * st.gs;
    s.tw = rand() * TAU;
    s.tws = pick(rand, layer.tw);
    s.envMul = 0.85 + rand() * 0.30;     // per-star envelope multiplier
    s.tone = rand();                     // where this star sits on the colour ramp
    s.k = 0.8;
}

function buildStars(env, st) {
    const cfg = METEOR_CONFIG.stars;
    const want = Math.max(cfg.min, Math.min(cfg.max, Math.round(cfg.count * (env.scale || 1))));
    if (!st.stars) st.stars = [];
    while (st.stars.length < want) st.stars.push(makeStar());
    if (st.stars.length > want) st.stars.length = want;
    const split = Math.round(want * cfg.farFraction);
    for (let i = 0; i < st.stars.length; i++) resetStar(st.stars[i], env, st, cfg.layers[i < split ? 0 : 1]);
}

/**
 * Aim one streak. Everything the legibility budget depends on is decided HERE:
 * the corner, the outward heading, and the x cap that keeps head AND tail inside
 * the outer band for the whole flight.
 * @returns {boolean} false if the pool is saturated or the canvas has no area
 */
function spawnMeteor(env, st) {
    const C = METEOR_CONFIG, rand = env.rand, W = env.width, H = env.height;
    if (!(W > 0) || !(H > 0)) return false;
    let p = null;
    for (let i = 0; i < st.meteors.length; i++) if (!st.meteors[i].alive) { p = st.meteors[i]; break; }
    if (!p) return false;                                    // saturated: skip, never grow the pool

    const kind = rand() < C.fireballChance ? 1 : 0;
    const K = C.kinds[kind];
    const side = rand() < 0.5 ? -1 : 1;                      // which corner
    const ang = pick(rand, C.angle);
    const sinA = Math.sin(ang), cosA = Math.cos(ang);
    const speed = pick(rand, K.speed) * st.gs;

    // Tail length ENCODES SPEED (v * k), then is capped twice: by the viewport, and
    // by how far it may reach back toward the centre before it violates the budget.
    const room = (C.edgeLimit - 0.015) * W / Math.max(0.25, sinA);
    const tail = Math.min(speed * pick(rand, K.tailK), 0.34 * Math.min(W, H), room);

    // head x is capped so head + (tail reaching inward) stays in the outer band
    const xMin = 0.015 * W;
    const xMax = C.edgeLimit * W - sinA * tail;
    const x0 = xMin + rand() * Math.max(0, xMax - xMin);

    p.x = side < 0 ? x0 : W - x0;
    p.y = (-0.10 + rand() * 0.30) * H;                       // top edge band
    p.ux = side * sinA;                                      // OUTWARD: |x - W/2| only grows
    p.uy = cosA;
    p.speed = speed;
    p.tail = tail;
    p.kind = kind;

    // Die just before leaving frame so the envelope closes instead of being cut off.
    const tExitX = (x0 + tail) / (speed * sinA);
    const tExitY = (H + tail - p.y) / (speed * cosA);
    const span = Math.min(tExitX, tExitY) * (0.85 + rand() * 0.20);
    p.life = 1;
    p.decay = 1 / Math.max(C.life[0], Math.min(C.life[1], span));
    p.envMul = 0.82 + rand() * 0.42;
    p.width = pick(rand, K.width) * st.gs;
    p.alpha = pick(rand, K.alpha);
    p.ramp0 = pick(rand, K.ramp0);
    p.span = K.span;
    p.glow = K.glow;
    p.alive = true;
    st.spawned++;
    return true;
}

/** Draw the next wait. Long silences, occasionally broken by a tight shower. */
function scheduleNext(env, st) {
    const C = METEOR_CONFIG, rand = env.rand;
    let gap;
    if (st.burst > 0) { st.burst--; gap = pick(rand, C.burstGap); }
    else if (rand() < C.showerChance) { st.burst = 1 + ((rand() * C.showerExtra) | 0); gap = pick(rand, C.burstGap); }
    else gap = pick(rand, C.silence);
    st.nextAt = st.clock + gap;                              // gap is always > 0 → the while() in update terminates
}

function init(env) {
    const st = env.state;
    st.gs = apparentScale(env.width, env.height);            // GEOMETRY from the CSS box only — never env.dpr
    st.w = env.width; st.h = env.height;
    st.clock = 0; st.burst = 0; st.spawned = 0;
    if (!st.meteors) {
        st.meteors = new Array(METEOR_CONFIG.pool);
        for (let i = 0; i < st.meteors.length; i++) st.meteors[i] = makeMeteor();
    } else {
        for (let i = 0; i < st.meteors.length; i++) st.meteors[i].alive = false;
    }
    buildStars(env, st);
    st.nextAt = pick(env.rand, METEOR_CONFIG.first);
}

export const descriptor = {
    id: 'meteors',
    label: 'Meteor Fall',
    // Emissive: a streak crossing a star must ADD light, not punch a hole in it.
    blend: 'lighter',
    // Hard clear, NOT a fade. The tail is drawn explicitly every frame, so a smear
    // would only add a second, slower ghost trail behind the real one — and under
    // additive blend a stale bright pixel takes many frames to decay to black.
    clearPolicy: 'clear',

    init,

    resize(env) {
        const st = env.state;
        if (!st.meteors) { init(env); return; }
        const sx = st.w > 0 ? env.width / st.w : 1;
        const sy = st.h > 0 ? env.height / st.h : 1;
        st.w = env.width; st.h = env.height;
        st.gs = apparentScale(env.width, env.height);
        for (let i = 0; i < st.stars.length; i++) { st.stars[i].x *= sx; st.stars[i].y *= sy; }
        // In-flight streaks are RETIRED: their edge budget was solved against the old
        // width, and a resize is rare enough that losing a sub-second streak is free.
        for (let i = 0; i < st.meteors.length; i++) st.meteors[i].alive = false;
    },

    update(dt, env) {
        const st = env.state;
        if (!st.meteors) init(env);
        st.clock += dt;                                      // private clock: dt-driven, immune to timestamp jumps

        // Scheduling deliberately ignores env.active / env.surge / env.now.
        // while(), not if(): one long stall must not swallow several events.
        while (st.clock >= st.nextAt) { spawnMeteor(env, st); scheduleNext(env, st); }

        const W = env.width, H = env.height;
        for (let i = 0; i < st.meteors.length; i++) {
            const p = st.meteors[i];
            if (!p.alive) continue;
            p.life -= p.decay * dt;
            p.x += p.ux * p.speed * dt;
            p.y += p.uy * p.speed * dt;
            const m = p.tail + 8;
            if (p.life <= 0 || p.y - m > H || p.x + m < 0 || p.x - m > W) { p.alive = false; p.life = 0; }
        }

        for (let i = 0; i < st.stars.length; i++) {
            const s = st.stars[i];
            s.x += s.vx * dt;
            if (s.x < -2) s.x += W + 4; else if (s.x > W + 2) s.x -= W + 4;
            s.k = 0.5 + 0.5 * Math.sin(st.clock * s.tws + s.tw);   // 0..1 twinkle
        }
    },

    draw(ctx, env) {
        const st = env.state;
        if (!st.stars) return;
        const C = METEOR_CONFIG;
        const spr = env.sprites && env.sprites.star;
        const inten = env.intensity;
        // The reading box, recomputed once per frame (never per particle).
        const bx0 = C.centreBox * env.width, bx1 = env.width - bx0;
        const by0 = C.centreBox * env.height, by1 = env.height - by0;

        // --- starfield: soft halo pass, then core pass (batched to avoid alpha thrash)
        if (spr) {
            for (let i = 0; i < st.stars.length; i++) {
                const s = st.stars[i];
                const dim = (s.x > bx0 && s.x < bx1 && s.y > by0 && s.y < by1) ? C.centreDim : 1;
                const g = s.r * (0.82 + 0.30 * s.k) * s.envMul * 4.5;
                ctx.globalAlpha = s.a * (0.55 + 0.45 * s.k) * dim * inten * 0.35;
                ctx.drawImage(spr, s.x - g, s.y - g, g * 2, g * 2);
            }
        }
        ctx.globalAlpha = 1;
        for (let i = 0; i < st.stars.length; i++) {
            const s = st.stars[i];
            const dim = (s.x > bx0 && s.x < bx1 && s.y > by0 && s.y < by1) ? C.centreDim : 1;
            const rad = s.r * (0.82 + 0.30 * s.k) * s.envMul;            // size-over-twinkle, per-star envelope
            const al = s.a * (0.55 + 0.45 * s.k) * dim * inten;
            ctx.beginPath();
            ctx.arc(s.x, s.y, Math.max(0.35, rad), 0, TAU);
            ctx.fillStyle = styleAt(STAR_STYLES, s.tone * 0.8 + 0.2 * (1 - s.k), al);
            ctx.fill();
        }

        // --- meteors: glow, then the tapering tail, then the head core
        ctx.lineCap = 'round';
        const segs = C.segments;
        for (let i = 0; i < st.meteors.length; i++) {
            const p = st.meteors[i];
            if (!p.alive) continue;
            const t = 1 - p.life;
            const e = envelope(t) * p.envMul;
            if (e <= 0) continue;
            const hw = p.width * e;
            const al = p.alpha * e * inten;
            const head = p.ramp0 + C.ageShift * t;                       // COLOUR RAMP indexed by life
            const tx = p.ux * p.tail, ty = p.uy * p.tail;                // head - velocity*k

            if (spr) {
                const g = hw * p.glow;
                ctx.globalAlpha = al * 0.30;
                ctx.drawImage(spr, p.x - g, p.y - g, g * 2, g * 2);
                ctx.globalAlpha = 1;
            }
            for (let j = 0; j < segs; j++) {
                const a = j / segs, b = (j + 1) / segs;
                ctx.lineWidth = Math.max(0.35, hw * Math.pow(1 - b, 1.25));
                ctx.strokeStyle = styleAt(METEOR_STYLES, head + p.span * a, al * Math.pow(1 - a, 1.9));
                ctx.beginPath();
                ctx.moveTo(p.x - tx * a, p.y - ty * a);
                ctx.lineTo(p.x - tx * b, p.y - ty * b);
                ctx.stroke();
            }
            ctx.beginPath();
            ctx.arc(p.x, p.y, Math.max(0.4, hw * 1.15), 0, TAU);
            ctx.fillStyle = styleAt(METEOR_STYLES, head, al);
            ctx.fill();
        }
    },

    // Activation must NOT reach the schedule. Firing a streak here would make the
    // backdrop a status indicator, which is the one thing it may never be.
    rearm(env) { if (!env.state.meteors) init(env); },
};

registerEffect(descriptor);
export default descriptor;
export { METEOR_CONFIG, METEOR_RAMP, envelope, rampAt };
