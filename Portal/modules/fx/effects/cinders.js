/**
 * Effect: CINDERS ON THE WIND (id 'cinders').
 *
 * A PARAMETER FORK of effects/embers.js, not a second simulation. The curl-noise
 * potential, the blackbody sprite ramp, the two-pass additive bloom and the
 * heat-persistence smear are transcribed unchanged; what moves is the FORCE
 * BUDGET, so the same field reads as weather instead of a fire:
 *
 *   buoyancy   9 -> 0.9     embers float UP; a cinder that has stopped burning
 *                           has nothing left to lift it, so it goes where the
 *                           air goes. In still air the field barely drifts at all
 *                           (0.03 px/s vs Embers' 5.9 — asserted in the test).
 *   curl gain  5200 -> 1900 the swirl stops BEING the motion and becomes an
 *                           undulation ON a current. Above ~2500 the eddies win
 *                           and motes visibly travel upwind, which reads as
 *                           chaos rather than weather.
 *   + WIND                  one global vector every mote accelerates toward, with
 *                           gusts that briefly scatter the field
 *
 * The horizontal wrap embers already had is what keeps the field full-width once
 * everything is travelling: nothing is ever spawned off-screen-left and marched
 * across, motes simply reappear on the other edge.
 *
 * LEGIBILITY: this is a backdrop behind chat text, and the budget is the approved
 * Embers budget — same clearPolicy veil, same per-particle alpha ceiling, same
 * bloom multipliers, same population target and hard cap, same largest sprite —
 * with the geometry multiplier clamped so it may only ever shrink the field below
 * Embers, never grow it. Measured, it lands at ~0.56 of Embers' additive ink on a
 * chat-sized pane and well under that on a phone. cinders.test.mjs asserts every
 * one of those against the shipped field rather than describing them here.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

/**
 * THE WIND. Exported and deliberately mutable: a future weather mode drives the
 * field by writing these, and needs no other seam into the simulation.
 *
 *   speed    px/s. This is the drag-balanced TERMINAL lateral velocity of a
 *            sail=1 mote, not an acceleration — the wind term below is
 *            pre-multiplied by DRAG so the number means what it says.
 *   tilt     vertical share of the current (negative = a shallow upward rake).
 *   gust     gust peak as a fraction of `speed`, added on top of it.
 *   scatter  extra curl gain at gust peak — the "briefly scatter them" part.
 */
export const WIND = { speed: 210, tilt: -0.03, gust: 0.9, scatter: 0.9 };

const CURL = 1900;          // swirl gain (embers: 5200) — an undulation, not the motion
const DRAG = 0.9;           // per-second velocity decay, unchanged from embers
const BUOYANCY = 0.9;       // dropped toward zero (embers: 9)
const SPARK_SINK = 18;      // heavy sparks still settle a little (embers: 60)
const GUST_ONSET = 0.55;    // gusts are events, not a wobble: below this the air is calm
const HARD_CAP = 3000;      // identical to embers

// Per-particle envelope multiplier range for the size-over-life curve. The top
// of the range times the curve's peak (0.94) is 0.9964 — under 1.0, which is what
// guarantees a drawn radius never exceeds the spawn radius, and therefore that no
// mote is ever a bigger blob over a glyph than the approved Embers field's.
const SWELL = [0.78, 1.06];

// The two sub-populations. A CINDER is a spent flake: big, draggy, low sail, it
// rides the mean current. A SPARK is still hot: tiny, bright, high sail and a
// gust gain over twice the cinder's, so a gust throws the sparks across the
// frame while the cinders barely lean.
const SPARK_ODDS = 0.14;
const SAIL = { cinder: 0.85, spark: 1.25 };
const GUST_GAIN = { cinder: 0.7, spark: 1.6 };

// cheap 2-octave sine curl-noise potential ψ → divergence-free swirl (verbatim)
function pot(x, y, t) {
    return Math.sin(x * 0.0065 + t * 0.22) * Math.cos(y * 0.0065 - t * 0.16)
        + 0.5 * Math.sin(x * 0.013 - t * 0.31) * Math.cos(y * 0.013 + t * 0.26);
}

/**
 * Gust envelope in [0, 1] as a pure function of ABSOLUTE time — never of frame
 * count — so a gust lands at the same wall-clock moment at 60 Hz and 144 Hz, and
 * a headless test can ask for one directly.
 *
 * Two incommensurate sines so the cadence never reads as metronomic, thresholded
 * at GUST_ONSET (most of the time the air is simply calm) and squared, so the
 * onset is a shove rather than a ramp.
 * @param {number} ts seconds
 * @param {number} phase per-field random phase
 */
export function gustAt(ts, phase) {
    const raw = Math.sin(ts * 0.41 + phase) * 0.62 + Math.sin(ts * 0.17 + phase * 1.7) * 0.38;
    if (raw <= GUST_ONSET) return 0;
    const g = (raw - GUST_ONSET) / (1 - GUST_ONSET);
    return g * g;
}

/**
 * SIZE-OVER-LIFE. A cinder catches the air as it lights (bloom over the first
 * ~15% of life), rides at full size, then burns down — so a mote is never the
 * same size twice. Peak is 0.94 and, times the top of SWELL, the ceiling is 1.0
 * BY CONSTRUCTION: the drawn radius can never exceed the spawn radius. That is
 * the arithmetic the legibility budget rests on.
 */
function sizeOverLife(life) {
    const age = 1 - life;
    const bloom = age < 0.15 ? 0.62 + 0.38 * (age / 0.15) : 1;
    return bloom * (0.59 + 0.41 * life);
}

/** Geometry multiplier: a function of the CSS box ONLY — apparentScale never
 *  sees devicePixelRatio, and the min() keeps a large viewport from spending
 *  more ink than the approved Embers field (it may only ever shrink). */
function geomFor(env) {
    return Math.min(1, apparentScale(env.width, env.height));
}

/** Pooled particle: the update/draw hot path allocates nothing, so a dead mote
 *  is recycled rather than re-created 30x/second. */
function alloc(st) {
    const p = st.pool.pop();
    if (p) return p;
    return { x: 0, y: 0, vx: 0, vy: 0, life: 1, decay: 1, r: 1, spark: false, sail: 1, gain: 1, swell: 1, fade: 0, hue0: 0 };
}

function spawn(env, n) {
    const rand = env.rand, st = env.state, list = st.list;
    for (let i = 0; i < n; i++) {
        const spark = rand() < SPARK_ODDS, ml = 2.6 + rand() * 3.6;      // life range unchanged: same drain curve
        const p = alloc(st);
        p.x = rand() * env.width; p.y = rand() * env.height;             // ANYWHERE on screen — the wrap keeps it full
        p.vx = WIND.speed * (0.3 + rand() * 0.5);                        // born already travelling, not from rest
        p.vy = (rand() - 0.5) * 12;
        p.life = 1; p.decay = 1 / ml;
        p.r = spark ? (1.1 + rand() * 1.5) : (2.9 + rand() * 6.7);       // <= embers once swell/curve/geom are applied
        p.spark = spark;
        p.sail = spark ? SAIL.spark : SAIL.cinder;
        p.gain = spark ? GUST_GAIN.spark : GUST_GAIN.cinder;
        p.swell = SWELL[0] + rand() * (SWELL[1] - SWELL[0]);             // per-particle envelope multiplier
        p.fade = rand() * 6.28; p.hue0 = Math.floor(rand() * 3);         // 0..2 varied warm heat
        list.push(p);
    }
}

function recycle(st, i) {
    const p = st.list[i];
    st.list[i] = st.list[st.list.length - 1];
    st.list.pop();
    if (st.pool.length < HARD_CAP) st.pool.push(p);
}

export const descriptor = {
    id: 'cinders',
    label: 'Cinders on the Wind',
    // Emissive field: overlapping motes must ADD light, not occlude each other.
    blend: 'lighter',
    // The Embers heat-persistence smear, unchanged — at these speeds it is also
    // what draws the streak behind a travelling mote.
    clearPolicy: 'fade:rgba(8,6,10,0.20)',

    init(env) {
        const st = env.state;
        st.list = [];
        st.pool = [];
        st.acc = 0;
        st.gustPhase = env.rand() * 6.28;   // each field gets its own weather
        st.geom = geomFor(env); st.geomW = env.width; st.geomH = env.height;
        spawn(env, Math.round(140 * env.scale));
    },

    resize(env) {
        // Population is viewport-independent (motes wrap and re-spawn); only the
        // geometry multiplier tracks the CSS box.
        const st = env.state;
        if (!st.list) return;
        st.geom = geomFor(env); st.geomW = env.width; st.geomH = env.height;
    },

    update(dt, env) {
        const st = env.state;
        if (!st.list) { st.list = []; st.pool = []; st.acc = 0; st.gustPhase = env.rand() * 6.28; }
        const ts = env.now * 0.001;
        // keep a full-screen population while generating (Embers' target verbatim)
        if (env.active) {
            const target = Math.round((150 + env.surge * 160) * env.scale);
            let acc = st.acc + (30 + env.surge * 90) * env.scale * dt;
            while (acc >= 1 && st.list.length < target * 1.3) { spawn(env, 1); acc--; }
            st.acc = acc;
        }
        // ONE gust sample per frame, not per particle: the whole field is in the
        // same air. Everything below is per-second and multiplied by dt, so the
        // field travels the same distance at 60 Hz and 120 Hz.
        const g = gustAt(ts, st.gustPhase || 0);
        const curl = CURL * (1 + WIND.scatter * g);
        const base = WIND.speed * DRAG, gustMul = WIND.gust * g;
        const eps = 3;
        for (let i = st.list.length - 1; i >= 0; i--) {
            const p = st.list[i];
            p.life -= p.decay * dt;
            if (p.life <= 0) { recycle(st, i); continue; }
            // curl-noise swirl — same field, lower gain, so it bends the current
            // into long shallow arcs instead of overwhelming it
            const cvx = (pot(p.x, p.y + eps, ts) - pot(p.x, p.y - eps, ts));
            const cvy = -(pot(p.x + eps, p.y, ts) - pot(p.x - eps, p.y, ts));
            p.vx += cvx * curl * dt; p.vy += cvy * curl * dt;
            // the current: pre-multiplied by DRAG, so the drag-balanced terminal
            // vx is exactly WIND.speed * sail * (1 + gust). Sparks carry BOTH the
            // higher sail and the higher gust gain, so a gust throws them across
            // the frame while the cinders merely lean into it.
            const s = base * p.sail * (1 + gustMul * p.gain);
            p.vx += s * dt; p.vy += s * WIND.tilt * dt;
            p.vy -= BUOYANCY * p.life * dt;            // buoyancy dropped toward zero
            if (p.spark) p.vy += SPARK_SINK * dt;      // sparks settle a touch
            p.vx *= (1 - DRAG * dt); p.vy *= (1 - DRAG * dt); // drag
            p.x += p.vx * dt; p.y += p.vy * dt;
            // wrap horizontally so the field stays full across the whole width —
            // with a steady current this is the ONLY thing keeping it populated
            if (p.x < -20) p.x = env.width + 20; else if (p.x > env.width + 20) p.x = -20;
        }
        while (st.list.length > HARD_CAP) recycle(st, st.list.length - 1);
    },

    draw(ctx, env) {
        const st = env.state;
        const spr = env.sprites && env.sprites.ember;
        if (!spr || !st.list) return;                  // atlas not baked (headless test) → nothing to draw
        const ts = env.now * 0.001;
        const top = spr.length - 1;
        // Recompute geometry only when the CSS box actually changed: two compares
        // per frame, and it keeps the field correct even if resize() never fires.
        if (st.geomW !== env.width || st.geomH !== env.height) {
            st.geom = geomFor(env); st.geomW = env.width; st.geomH = env.height;
        }
        const geom = st.geom;
        for (let i = 0; i < st.list.length; i++) {
            const p = st.list[i];
            const breathe = 0.6 + 0.4 * Math.sin(ts * 1.3 + p.fade);      // soft per-mote flicker
            // blackbody ramp indexed by LIFE: white-hot at birth, deep ember red
            // at burnout. Sparks start hotter and cool over a shorter stretch.
            const idx = p.spark
                ? Math.max(0, Math.min(top, Math.round((1 - p.life) * 1.5)))
                : Math.max(0, Math.min(top, p.hue0 + Math.round((1 - p.life) * 3)));
            const s = spr[idx];
            const al = (p.spark ? 0.85 : 0.55) * Math.min(1, p.life * 1.4) * breathe * env.intensity;
            const r = p.r * p.swell * sizeOverLife(p.life) * geom;        // size-over-life x per-mote envelope
            // faint big glow + bright core (cheap bloom) — Embers' multipliers
            const grr = r * (p.spark ? 3.5 : 4.2); ctx.globalAlpha = al * 0.22; ctx.drawImage(s, p.x - grr, p.y - grr, grr * 2, grr * 2);
            const cr = r * (p.spark ? 1.5 : 1.9); ctx.globalAlpha = al; ctx.drawImage(s, p.x - cr, p.y - cr, cr * 2, cr * 2);
        }
    },

    // Fresh weather each turn: start empty and let the current fill the frame.
    // Particles go back to the pool rather than to the GC.
    rearm(env) {
        const st = env.state;
        if (!st.list) return;
        while (st.list.length) recycle(st, st.list.length - 1);
        st.acc = 0;
    },
};

registerEffect(descriptor);
export default descriptor;
