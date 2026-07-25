/**
 * Effect: DRIFT SNOW (id 'snow').
 *
 * Three depth planes of soft, out-of-focus flakes sifting DOWN, each flake
 * fluttering on its own sine, with an occasional field-wide GUST that shoves the
 * whole population sideways for about a second and then settles.
 *
 * The depth illusion is a CORRELATION, not a trick: within a plane, size, fall
 * speed, sway amplitude, blur radius and gust response all move together, so a
 * big near flake is fast and smeared while a small far one is slow and crisp.
 * Break the correlation (equal speeds, equal sharpness) and three planes read as
 * one flat field of dots.
 *
 * Three decisions that look like omissions and are not:
 *
 *   blend is 'source-over', NOT 'lighter'. This is a BACKDROP behind chat text.
 *     White-on-black at 250 flakes under additive blend blows the overlaps out to
 *     paper white and takes the text contrast with it. Non-emissive field =>
 *     occlusion, not accumulation. Same reason the population is capped
 *     (MAX_FLAKES) instead of scaling freely with the viewport.
 *
 *   clearPolicy is 'clear', NOT a fade. A translucent fade would smear every
 *     flake into a vertical streak — that is sleet, not snow — and the residue
 *     of 250 white sprites per frame is exactly the low-contrast grey veil the
 *     legibility budget exists to prevent.
 *
 *   `life` is FALL PROGRESS (0 at the top margin, 1 at the bottom), not an
 *     age counter. A flake's life in this sim genuinely IS how far it has
 *     fallen, and indexing the size/alpha envelope by it means every flake fades
 *     in above the top edge and out below the bottom one — so recycling is
 *     invisible, with no popping and no bookkeeping.
 *
 * Flakes are drawn as concentric arcs (widest first, core last) rather than a
 * baked sprite: a tinted snow atlas belongs in the shared sprites.js bake, and
 * the existing atlas is warm-white ember stock that cannot carry this cold
 * life-ramp. At <=250 flakes the arcs cost less than the per-particle radial
 * gradients stars.js already pays for 120.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

const TAU = Math.PI * 2;

/** LEGIBILITY BUDGET. A hard ceiling, not a target — see the header. */
export const MAX_FLAKES = 250;

/**
 * The three sub-populations. Every field is a range sampled per flake, so no two
 * flakes in a plane are identical; the PLANES differ from each other in the
 * correlated way that produces depth.
 *   size/fall/sway  in CSS px (scaled by apparentScale at spawn, never by dpr)
 *   swayW           flutter angular frequency, rad/s
 *   soft            blur spread: how far the halo rings reach past the core
 *   rings           arc passes: 1 = crisp dot, 3 = properly defocused
 *   gust            share of the global gust this plane rides (parallax)
 */
const PLANES = [
    // FAR — small, slow, crisp, faint. Barely notices the wind.
    { count: 112, size: [0.6, 1.15], fall: [11, 19], sway: [3, 8], swayW: [1.1, 2.0], soft: 0.30, rings: 1, alpha: 0.30, gust: 0.32 },
    // MID — the body of the field.
    { count: 88, size: [1.3, 2.3], fall: [23, 38], sway: [8, 16], swayW: [1.4, 2.6], soft: 0.85, rings: 2, alpha: 0.40, gust: 0.68 },
    // NEAR — big, fast, blurred. Rides the gust hardest: that is PARALLAX (screen
    // displacement scales with proximity), not aerodynamics.
    { count: 44, size: [2.6, 4.8], fall: [46, 78], sway: [13, 26], swayW: [1.8, 3.2], soft: 1.55, rings: 3, alpha: 0.34, gust: 1.15 },
];

/** Gust state machine, in SECONDS (accumulated from dt, never wall-clock —
 *  absolute timestamps go stale across a hidden tab and fire a gust on return). */
const GUST = { wait: [4.5, 11], hold: [0.7, 1.6], strength: [38, 96], ease: 2.4 };

/** Spawn/despawn band above and below the viewport, in CSS px at scale 1. */
const MARGIN = 22;

/** Ring geometry: radius multiplier and alpha multiplier, core first. */
const HALO_R = [1, 2.1, 3.6];
const HALO_A = [1, 0.26, 0.10];

// ---------------------------------------------------------------------------
// Colour ramp — moonlit steel-blue high, near-white mid-fall, dusty settle low
// ---------------------------------------------------------------------------
const SNOW_RAMP = [[150, 176, 210], [200, 220, 242], [238, 247, 255], [216, 232, 249], [178, 198, 224]];
const RAMP_STEPS = 48;

/**
 * Bake the ramp to a LUT of 'rgb(...)' strings ONCE at import. The draw loop
 * assigns fillStyle from this table and carries alpha on globalAlpha, so a frame
 * of 250 flakes allocates exactly nothing — a per-flake template string would be
 * 250 short-lived strings every frame, i.e. a GC saw-tooth behind the chat.
 */
function bakeRamp() {
    const lut = new Array(RAMP_STEPS);
    const last = SNOW_RAMP.length - 1;
    for (let i = 0; i < RAMP_STEPS; i++) {
        const u = (i / (RAMP_STEPS - 1)) * last;
        const a = Math.min(last, Math.floor(u)), b = Math.min(last, a + 1), f = u - a;
        const c0 = SNOW_RAMP[a], c1 = SNOW_RAMP[b];
        lut[i] = `rgb(${Math.round(c0[0] + (c1[0] - c0[0]) * f)},${Math.round(c0[1] + (c1[1] - c0[1]) * f)},${Math.round(c0[2] + (c1[2] - c0[2]) * f)})`;
    }
    return lut;
}
export const RAMP_LUT = bakeRamp();

/**
 * Size/alpha envelope over life: swell in over the first 18% of the fall, hold,
 * taper over the last 22%. Smoothstepped so the fade has no visible corner.
 * @param {number} life 0..1 fall progress
 * @returns {number} 0..1
 */
export function sizeCurve(life) {
    const s = Math.max(0, Math.min(Math.min(1, life / 0.18), Math.min(1, (1 - life) / 0.22)));
    return s * s * (3 - 2 * s);
}

/**
 * Per-plane populations for a viewport density, clamped to the legibility budget.
 * Density (how many) comes from env.scale; GEOMETRY (how big) comes from
 * apparentScale — conflating the two is the shipped dpr bug in a new costume.
 * @param {number} scale env.scale
 * @returns {number[]} one count per plane, summing to <= MAX_FLAKES
 */
export function planeCounts(scale) {
    const s = Number.isFinite(scale) && scale > 0 ? scale : 1;
    const out = new Array(PLANES.length);
    let total = 0;
    for (let i = 0; i < PLANES.length; i++) { out[i] = Math.max(3, Math.round(PLANES[i].count * s)); total += out[i]; }
    if (total > MAX_FLAKES) {
        const k = MAX_FLAKES / total;
        total = 0;
        for (let i = 0; i < out.length; i++) { out[i] = Math.max(1, Math.floor(out[i] * k)); total += out[i]; }
        // The per-plane floor of 1 can in principle push the sum back over budget.
        // The budget wins: trim the densest plane until it fits.
        while (total > MAX_FLAKES) {
            let big = 0;
            for (let i = 1; i < out.length; i++) if (out[i] > out[big]) big = i;
            out[big]--; total--;
        }
    }
    return out;
}

// ---------------------------------------------------------------------------
// Flake pool — every object below is allocated at init/resize, never in a frame
// ---------------------------------------------------------------------------
function newFlake() {
    return { x: 0, y: 0, r: 0, vy: 0, swayAmp: 0, swayW: 0, phase: 0, sizeEnv: 1, bias: 0, life: 0 };
}

/** Recycle a flake in place. `initial` scatters it through the whole column so
 *  the first frame is a field, not a curtain arriving from off-screen. */
function respawn(f, cfg, st, rand, initial) {
    const geo = st.geo;
    f.x = rand() * st.w;
    f.y = initial ? rand() * (st.h + st.margin * 2) - st.margin : -st.margin - rand() * st.h * 0.2;
    f.r = (cfg.size[0] + rand() * (cfg.size[1] - cfg.size[0])) * geo;
    f.vy = (cfg.fall[0] + rand() * (cfg.fall[1] - cfg.fall[0])) * geo;
    f.swayAmp = (cfg.sway[0] + rand() * (cfg.sway[1] - cfg.sway[0])) * geo;
    f.swayW = cfg.swayW[0] + rand() * (cfg.swayW[1] - cfg.swayW[0]);
    f.phase = rand() * TAU;
    f.sizeEnv = 0.78 + rand() * 0.50;   // per-particle envelope multiplier: the
    f.bias = (rand() - 0.5) * 0.28;     // curve is shared, the AMPLITUDE is not,
    f.life = 0;                         // and bias de-banks the shared colour ramp
}

function scheduleGust(st, rand) {
    st.gust = 0; st.gustTarget = 0; st.gustHold = 0;
    st.gustWait = GUST.wait[0] + rand() * (GUST.wait[1] - GUST.wait[0]);
}

/** Advance the global gust. One number for the WHOLE field — coherence is the
 *  entire point: 250 independently-gusting flakes is just noise. */
function stepGust(st, env, dt) {
    const rand = env.rand;
    if (st.gustHold > 0) {
        st.gustHold -= dt;
        if (st.gustHold <= 0) {
            st.gustTarget = 0;
            st.gustWait = GUST.wait[0] + rand() * (GUST.wait[1] - GUST.wait[0]);
        }
    } else {
        st.gustWait -= dt;
        if (st.gustWait <= 0) {
            const mag = (GUST.strength[0] + rand() * (GUST.strength[1] - GUST.strength[0]));
            st.gustTarget = (rand() < 0.5 ? -1 : 1) * mag * st.geo * (1 + env.surge * 0.6);
            st.gustHold = GUST.hold[0] + rand() * (GUST.hold[1] - GUST.hold[0]);
        }
    }
    // Exponential approach, clamped so a long stall cannot overshoot the target.
    st.gust += (st.gustTarget - st.gust) * Math.min(1, GUST.ease * dt);
}

function init(env) {
    const st = env.state;
    st.w = env.width; st.h = env.height;
    st.geo = apparentScale(env.width, env.height);   // GEOMETRY from the CSS box ONLY — env.dpr is never read here
    st.margin = MARGIN * st.geo;
    st.planes = [];
    const counts = planeCounts(env.scale);
    for (let i = 0; i < PLANES.length; i++) {
        const cfg = PLANES[i], list = new Array(counts[i]);
        for (let j = 0; j < counts[i]; j++) {
            const f = newFlake();
            respawn(f, cfg, st, env.rand, true);
            list[j] = f;
        }
        st.planes.push({ cfg, list });
    }
    scheduleGust(st, env.rand);
}

export const descriptor = {
    id: 'snow',
    label: 'Drift Snow',
    // Non-emissive white field behind text: additive would blow out. See header.
    blend: 'source-over',
    // Hard clear: a fade smears flakes into streaks and greys out the backdrop.
    clearPolicy: 'clear',

    init,

    // Survivors keep their positions. The engine resizes on every composer-height
    // change, and rebuilding would re-scatter the whole field on each keystroke —
    // geometry is rescaled in place instead, and only the count delta is spawned.
    resize(env) {
        const st = env.state;
        if (!st.planes) { init(env); return; }
        const geo = apparentScale(env.width, env.height);
        const k = geo / st.geo;                       // apparentScale floors at 0.5, so st.geo is never 0
        st.w = env.width; st.h = env.height; st.geo = geo; st.margin = MARGIN * geo;
        const counts = planeCounts(env.scale);
        for (let i = 0; i < st.planes.length; i++) {
            const pl = st.planes[i], list = pl.list;
            for (let j = 0; j < list.length; j++) {
                const f = list[j];
                f.r *= k; f.vy *= k; f.swayAmp *= k;
                if (f.x > st.w) f.x = env.rand() * st.w;   // a narrower viewport strands flakes off the right edge
            }
            while (list.length > counts[i]) list.pop();
            while (list.length < counts[i]) {
                const f = newFlake();
                respawn(f, pl.cfg, st, env.rand, true);
                list.push(f);
            }
        }
    },

    // env.active is deliberately ignored: the pool is fixed and nothing spawns on
    // demand, so there is no generation-gated behaviour to gate. A backdrop that
    // froze mid-fall between turns would read as a hung page.
    update(dt, env) {
        const st = env.state;
        if (!st.planes) init(env);
        if (env.width !== st.w || env.height !== st.h) descriptor.resize(env);
        stepGust(st, env, dt);
        const t = env.now * 0.001;
        const span = Math.max(1, st.h + st.margin * 2);
        const lean = Math.abs(st.gust) * 0.12;        // wind drives the field down a touch harder
        const wrap = st.w + st.margin * 2;
        for (let i = 0; i < st.planes.length; i++) {
            const pl = st.planes[i], list = pl.list, gust = st.gust * pl.cfg.gust;
            for (let j = 0; j < list.length; j++) {
                const f = list[j];
                // Flutter is a VELOCITY, not a position offset: integrating the sine
                // gives the lazy S-path a flake actually falls in. A position offset
                // would make it a rigid metronome sliding down a rail.
                f.x += (Math.sin(t * f.swayW + f.phase) * f.swayAmp + gust) * dt;
                f.y += (f.vy + lean * pl.cfg.gust) * dt;
                f.life = Math.max(0, Math.min(1, (f.y + st.margin) / span));
                if (f.y > st.h + st.margin) respawn(f, pl.cfg, st, env.rand, false);
                else if (f.x < -st.margin) f.x += wrap;
                else if (f.x > st.w + st.margin) f.x -= wrap;
            }
        }
    },

    draw(ctx, env) {
        const st = env.state;
        if (!st.planes) return;
        const intensity = env.intensity, top = RAMP_STEPS - 1;
        for (let i = 0; i < st.planes.length; i++) {
            const pl = st.planes[i], cfg = pl.cfg, list = pl.list;
            // Per-plane constants hoisted out of the flake loop.
            const rings = cfg.rings, spread = 1 + cfg.soft;
            // The blurrier the plane, the DIMMER its core relative to its halo —
            // that redistribution is what reads as out of focus rather than small.
            const core = 1 - 0.45 * Math.min(1, cfg.soft);
            for (let j = 0; j < list.length; j++) {
                const f = list[j];
                const curve = sizeCurve(f.life);
                const r = f.r * f.sizeEnv * (0.62 + 0.38 * curve);          // SIZE OVER LIFE
                const a = cfg.alpha * (0.10 + 0.90 * curve) * intensity;
                if (a <= 0.002 || r <= 0.05) continue;                      // sub-pixel/invisible: skip the arcs
                let idx = Math.round((f.life + f.bias) * top);              // COLOUR RAMP BY LIFE
                idx = idx < 0 ? 0 : (idx > top ? top : idx);
                ctx.fillStyle = RAMP_LUT[idx];
                for (let k = rings - 1; k >= 0; k--) {                      // widest ring first, core lands on top
                    ctx.globalAlpha = a * HALO_A[k] * (k === 0 ? core : 1);
                    ctx.beginPath();
                    ctx.arc(f.x, f.y, r * (1 + (HALO_R[k] - 1) * spread), 0, TAU);
                    ctx.fill();
                }
            }
        }
    },

    // Nothing to revive — the field never empties. Just make sure it exists, and
    // reset the gust clock so a tab that was hidden for an hour does not come
    // back to a gust that was "due" the whole time.
    rearm(env) {
        const st = env.state;
        if (!st.planes) init(env);
        else scheduleGust(st, env.rand);
    },
};

registerEffect(descriptor);
export default descriptor;
export { PLANES, GUST };
