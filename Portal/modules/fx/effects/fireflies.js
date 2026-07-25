/**
 * Effect: FIREFLIES (id 'fireflies') — the QUIETEST field in the catalogue.
 *
 * Fifteen to forty warm points wandering slowly through the dark, each breathing
 * on its own private rhythm. This is a BACKDROP that sits behind chat text, not a
 * screensaver: the whole design brief is "never quite still, never busy", and the
 * population bounds below are the mechanism — a handful of small points means the
 * average screen luminance barely moves, so body text keeps its contrast even at
 * the brightest frame the field can produce (asserted, see fireflies.test.mjs).
 *
 * A fork of the Rising Stars sim with three deliberate divergences:
 *
 *   1. NO upward bias. Stars carry a `riseSpeed` and a `baseVy` that is always
 *      negative; a firefly's heading is free, and the field's mean vertical drift
 *      is ~0 by construction.
 *   2. Heading comes from a LOW-FREQUENCY FLOW FIELD, not a random walk. A random
 *      walk re-rolls the direction every frame and reads as jitter no matter how
 *      small the steps; sampling a smooth noise field and steering TOWARD it
 *      curves the path, and a curved path is what reads as alive.
 *   3. Brightness is a PRIVATE pulse, not a shared sine of the clock. The one
 *      rule that makes or breaks this effect: the pulses must be desynchronised
 *      in BOTH period and phase. Shared period + random phase re-converges into
 *      visible beating; shared phase + random period flashes the whole field in
 *      unison on the first cycle. Both are drawn per particle, from a continuous
 *      range, so the population can never return to a common phase.
 *
 * Compositing is declared, never set here: additive ('lighter') because the glow
 * of two overlapping fireflies adds, and a hard clear because a fade would let
 * the trails accumulate behind the text — the exact thing the legibility budget
 * exists to prevent.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

const TAU = Math.PI * 2;

/** Population bounds. On a 4K wall the field may not become busy; on a phone it
 *  may not empty out. Everything between is env.scale's business. */
const MIN_POP = 15;
const MAX_POP = 40;
const BASE_POP = 24;          // at fieldScale() === 1 (a 1440x900 viewport)

/** Soft turn-back band, in CSS px at apparentScale 1. Fireflies steer away from
 *  the edges rather than wrapping — a point that teleports across the screen is
 *  the single most obvious "these are particles" tell, and a hard bounce is the
 *  second. The band has to be wider than the distance a firefly covers while it
 *  turns around (speed / turn rate ≈ 15 px for a lamp, 18 for a spark), or the
 *  backstop in update() fires and you see the bounce. */
const EDGE = 26;
const bandOf = (g) => EDGE * 2.5 * g;

/** How much harder a firefly veers when it is inside the turn-back band. Steering
 *  alone at the base rate loses the race against its own speed. */
const EDGE_URGENCY = 2.5;

/** Big-glow alpha as a fraction of the particle's own gain (cheap bloom: one
 *  wide faint pass + one small bright core, the embers idiom). */
const GLOW_ALPHA = 0.18;

/**
 * THE TWO SUB-POPULATIONS.
 *
 *   lamp  — the ambient breathers: large, slow, long-lived, never fully dark
 *           (a floor of steady glow), a long lazy period and a wide duty cycle.
 *   spark — the signallers: small, quicker, shorter-lived, a hard blink with a
 *           real dark gap between flashes (floor ~0, narrow duty).
 *
 * They differ in every axis that is visible — size, speed, turn rate, lifetime,
 * pulse period, duty, floor, swell, colour band — because two populations that
 * differ only in size just look like one population at two distances.
 */
const KINDS = {
    lamp: {
        r: [2.2, 3.9], speed: [5, 13], turn: 0.85, life: [22, 40],
        period: [2.6, 5.4], duty: [0.55, 0.82], floor: [0.18, 0.34],
        swell: [0.18, 0.42], hue: [1.35, 3.60], glow: 4.5, core: 1.45, gain: 0.62,
    },
    spark: {
        r: [1.3, 2.3], speed: [14, 30], turn: 1.70, life: [12, 24],
        period: [1.1, 2.4], duty: [0.16, 0.34], floor: [0.00, 0.06],
        swell: [0.30, 0.70], hue: [0.55, 2.50], glow: 3.4, core: 1.15, gain: 0.80,
    },
};

/** Every third slot is a spark. Deterministic rather than a coin flip so the mix
 *  is exactly 1:2 at ANY population — a 15-particle phone field still gets both
 *  sub-populations, which a per-particle rand() cannot promise. */
const isSpark = (i) => (i % 3) === 1;

const pick = (rand, range) => range[0] + rand() * (range[1] - range[0]);
const smooth = (t) => t * t * (3 - 2 * t);                       // smoothstep, no corners

/**
 * One flash of the private pulse. `u` is the particle's own phase in [0,1) and
 * `duty` the lit fraction of its cycle; the rest of the cycle is dark (or, for a
 * lamp, its floor). Fast attack / slow decay, smoothstepped at both ends so the
 * peak has no visible corner — a triangle wave reads as a blinking LED.
 */
const ATTACK = 0.28;
function pulse(u, duty) {
    if (u >= duty) return 0;
    const k = u / duty;
    const t = k < ATTACK ? k / ATTACK : 1 - (k - ATTACK) / (1 - ATTACK);
    return smooth(t);
}

/** SIZE over life: swells in over the first 12%, tapers to 55% over the last
 *  25%. Multiplied per particle by (1 + swell * breath) at draw time, so no two
 *  fireflies pump by the same amount and none of them is a constant dot. */
function sizeOverLife(life) {
    return smooth(Math.min(1, (1 - life) * 8)) * (0.55 + 0.45 * smooth(Math.min(1, life * 4)));
}

/** ALPHA over life: fade in at birth, fade out at death, so a recycled slot
 *  never pops. Never negative — `life` is culled at <= 0 before this runs. */
function lifeFade(life) {
    return Math.min(1, (1 - life) * 8) * Math.min(1, life * 4);
}

/**
 * Low-frequency flow field -> a heading in radians. Two octaves of sine at
 * ~1/700 px and ~1/1100 px, drifting at ~1/20 Hz: slow enough in BOTH space and
 * time that neighbours share a lazy common current while each still curves its
 * own way. `k` is the particle's private offset into the field so that two
 * fireflies in the same place never fly in lockstep.
 */
function flow(x, y, t, k) {
    return (Math.sin(x * 0.0013 + t * 0.055 + k)
        + Math.cos(y * 0.0017 - t * 0.041 + k * 1.7)
        + 0.5 * Math.sin((x - y) * 0.0009 + t * 0.029)) * 1.25;
}

/** Shortest signed turn from angle `from` to angle `to`, in (-PI, PI]. */
function turnTo(from, to) {
    let d = (to - from) % TAU;
    if (d > Math.PI) d -= TAU; else if (d < -Math.PI) d += TAU;
    return d;
}

/** 0 inside the soft band, ramping to 1 at (and past) the edge. */
function edgePush(p, w, h, m) {
    const d = Math.min(p.x, p.y, w - p.x, h - p.y);
    if (d >= m) return 0;
    return m > 0 ? Math.min(1, 1 - d / m) : 1;
}

/**
 * (Re)seed one firefly IN PLACE. Called at init and whenever a slot's life runs
 * out; the slot object itself is never reallocated, so the hot path allocates
 * nothing at all. `spark` is a property of the slot, not of the draw — a firefly
 * does not change species when it is recycled.
 */
function recycle(p, env, initial) {
    const rand = env.rand;
    const k = p.spark ? KINDS.spark : KINDS.lamp;
    const m = bandOf(env.state.g);            // never born already fighting an edge
    p.x = m + rand() * Math.max(1, env.width - m * 2);
    p.y = m + rand() * Math.max(1, env.height - m * 2);
    p.h = rand() * TAU;                       // heading (radians); no upward bias
    p.sp = pick(rand, k.speed);               // px/s along that heading
    p.r0 = pick(rand, k.r);
    p.swell = pick(rand, k.swell);            // per-particle size-envelope depth
    p.nk = rand() * TAU;                      // private offset into the flow field
    p.decay = 1 / pick(rand, k.life);
    // --- desynchronisation: period AND phase, both continuous, both private ---
    p.period = pick(rand, k.period);
    p.t = rand() * p.period;                  // phase, carried in seconds
    p.duty = pick(rand, k.duty);
    p.floor = pick(rand, k.floor);
    // Stagger the first fill so the population does not die (and re-light) as a
    // cohort — the same trick stars.reset(initial) plays with y.
    p.life = initial ? 0.15 + rand() * 0.85 : 1;
    p.dead = false;
}

/**
 * Size the pool to the viewport and refresh the geometry scale.
 *
 * st.g is apparentScale(CSS box) and NOTHING ELSE. devicePixelRatio must never
 * reach particle geometry — that is the bug that shipped from the engine's first
 * commit until 2026-07-24 and made the whole field dpr x too big on every HiDPI
 * screen. env.dpr is not read anywhere in this module, by design.
 */
function ensurePool(env) {
    const st = env.state;
    st.g = apparentScale(env.width, env.height);
    const target = Math.max(MIN_POP, Math.min(MAX_POP, Math.round(BASE_POP * (env.scale || 1))));
    if (!st.list) st.list = [];
    const list = st.list;
    while (list.length > target) list.pop();
    while (list.length < target) {
        const p = {
            spark: isSpark(list.length),
            x: 0, y: 0, h: 0, sp: 0, r0: 0, swell: 0, nk: 0,
            decay: 0, period: 1, t: 0, duty: 0, floor: 0, life: 1, dead: false,
        };
        recycle(p, env, true);
        list.push(p);
    }
    // A shrunken viewport must not leave anyone stranded off-screen.
    for (let i = 0; i < list.length; i++) {
        const p = list[i];
        if (p.x > env.width) p.x = env.width;
        if (p.y > env.height) p.y = env.height;
    }
}

export const descriptor = {
    id: 'fireflies',
    label: 'Fireflies',
    // Emissive: two overlapping glows ADD, they do not occlude.
    blend: 'lighter',
    // Hard clear. A fade would smear every path into a ribbon and quietly raise
    // the floor luminance of the whole canvas — exactly what a text backdrop
    // cannot afford. Fireflies leave no trail in the dark either.
    clearPolicy: 'clear',

    init(env) {
        env.state.list = null;
        ensurePool(env);
    },

    // Population is viewport-derived and geometry is CSS-box-derived, so a resize
    // must redo both. Existing fireflies keep their pulse — a window drag is not
    // a reason for the field to blink.
    resize(env) { ensurePool(env); },

    update(dt, env) {
        const st = env.state;
        if (!st.list) ensurePool(env);
        const list = st.list;
        const ts = env.now * 0.001;
        const active = env.active !== false;
        const w = env.width, h = env.height;
        const cx = w * 0.5, cy = h * 0.5;
        const band = bandOf(st.g);
        for (let i = 0; i < list.length; i++) {
            const p = list[i];
            if (p.dead) continue;
            p.life -= p.decay * dt;
            if (p.life <= 0) {
                // While generating, a new firefly takes the slot; once idle the
                // field thins out instead of looping forever (stars' semantics).
                if (active) recycle(p, env, false);
                else { p.life = 0; p.dead = true; }
                continue;
            }
            // Private pulse clock. Advanced by dt (not read off env.now) so a
            // stalled tab resumes the breath where it left it rather than
            // teleporting the whole field to a new phase together.
            p.t = (p.t + dt) % p.period;
            // Steer toward the flow field, with an inward bias near the edges.
            let target = flow(p.x, p.y, ts, p.nk);
            const push = edgePush(p, w, h, band);
            if (push > 0) target += turnTo(target, Math.atan2(cy - p.y, cx - p.x)) * push;
            const k = p.spark ? KINDS.spark : KINDS.lamp;
            // Clamped exponential approach — a rate per second, so it is
            // delta-timed, and clamped so a long stall cannot overshoot the
            // target heading (the same guard stars puts on its opacity chase).
            p.h += turnTo(p.h, target) * Math.min(1, k.turn * (1 + EDGE_URGENCY * push) * dt);
            p.x += Math.cos(p.h) * p.sp * dt;
            p.y += Math.sin(p.h) * p.sp * dt;
            // Backstop: steering alone can lose a race against a viewport that
            // just shrank, so reflect off the wall rather than escape it.
            if (p.x < 0) { p.x = 0; p.h = Math.PI - p.h; }
            else if (p.x > w) { p.x = w; p.h = Math.PI - p.h; }
            if (p.y < 0) { p.y = 0; p.h = -p.h; }
            else if (p.y > h) { p.y = h; p.h = -p.h; }
        }
    },

    draw(ctx, env) {
        const st = env.state;
        const spr = env.sprites && env.sprites.ember;
        if (!spr || !st.list) return;              // atlas not baked (headless) → draw nothing
        const star = env.sprites.star || spr[0];
        const top = spr.length - 1;
        const list = st.list, g = st.g;
        for (let i = 0; i < list.length; i++) {
            const p = list[i];
            if (p.dead) continue;
            const k = p.spark ? KINDS.spark : KINDS.lamp;
            // The breath: floor + its own flash, on its own period and phase.
            const e = p.floor + (1 - p.floor) * pulse(p.t / p.period, p.duty);
            // SIZE = life curve x per-particle envelope depth. Never constant.
            const r = p.r0 * g * sizeOverLife(p.life) * (1 + p.swell * e);
            const al = k.gain * e * lifeFade(p.life) * env.intensity;
            if (al <= 0) continue;                 // a spark in its dark gap costs nothing
            // COLOUR RAMP indexed by life: young fireflies sit at the pale-gold
            // end of the shared blackbody atlas and cool toward deep amber as
            // they age, each sub-population over its own band. Cross-faded
            // between adjacent stops so the ramp is continuous rather than six
            // visible steps.
            const idx = Math.max(0, Math.min(top, k.hue[0] + (k.hue[1] - k.hue[0]) * (1 - p.life)));
            const i0 = idx | 0;
            const f = idx - i0;
            const gr = r * k.glow, ga = al * GLOW_ALPHA, gd = gr * 2;
            ctx.globalAlpha = ga * (1 - f);
            ctx.drawImage(spr[i0], p.x - gr, p.y - gr, gd, gd);
            if (f > 0) {
                ctx.globalAlpha = ga * f;
                ctx.drawImage(spr[Math.min(top, i0 + 1)], p.x - gr, p.y - gr, gd, gd);
            }
            // Hot core, weighted toward the top of the flash: the glow breathes,
            // the core blinks. Without this a flash just reads as a slow fade.
            const cr = r * k.core;
            ctx.globalAlpha = al * (0.30 + 0.70 * e * e);
            ctx.drawImage(star, p.x - cr, p.y - cr, cr * 2, cr * 2);
        }
    },

    // A new turn revives the slots that went dark while idle, WITHOUT touching
    // the survivors — and every revived slot draws a fresh period and phase, so
    // a rearm can never hand the field a common rhythm.
    rearm(env) {
        const st = env.state;
        if (!st.list || !st.list.length) { ensurePool(env); return; }
        for (let i = 0; i < st.list.length; i++) {
            const p = st.list[i];
            if (p.dead) recycle(p, env, false);
        }
    },
};

registerEffect(descriptor);
export default descriptor;
export { KINDS, MIN_POP, MAX_POP, pulse, sizeOverLife };
