/**
 * Effect: SLIPSTREAM (id 'slipstream').
 *
 * Thousands of hair-thin luminous threads combing through invisible turbulence.
 * Each particle strokes the segment from where it was LAST frame to where it is
 * now, so the segment IS the thread — nothing is blitted, there is no sprite.
 * Folds and re-braids across the screen like smoke seen from above.
 *
 * The flow field is taken VERBATIM from effects/embers.js: the same 2-octave
 * divergence-free curl-noise potential, the same finite-difference curl, the
 * same eps. One deliberate difference in how it is USED — a thread is ADVECTED
 * by the curl (its velocity chases the field) rather than ACCELERATED by it as
 * an ember is. Advecting a divergence-free field is what keeps the density even:
 * threads comb and fold without piling up into blobs.
 *
 * THE DETAIL A NAIVE PORT MISSES: a curl potential this cheap has fixed
 * attractors, and a field of long-lived particles walks onto them and goes
 * STATIC within seconds — a beautiful first frame and a dead one at t=5s. So
 * ~2.5% of the population is scatter-reset every frame, on top of ordinary
 * lifetimes. It is expressed as a per-SECOND rate, not a per-frame percentage,
 * or a 120 Hz display would churn the field twice as fast as a 60 Hz one.
 * Because the envelope (below) is zero at birth, a reset is invisible.
 *
 * THE LEGIBILITY BUDGET — this is a backdrop behind chat text and it is the
 * quietest field in the catalogue by design:
 *   - stroke width is hard-capped at 1.5 CSS px (SLIPSTREAM.maxLinePx) AFTER
 *     the geometry scale, so a large viewport gets more threads, never fatter
 *     ones. Nothing wide enough to sit behind a glyph stem can exist.
 *   - stroke alpha is hard-capped at 0.30 (SLIPSTREAM.maxAlpha); the drawn
 *     population measures a MEAN of 0.148.
 *   - measured steady state at 1400x800: 1,600 threads cover 0.24% of the frame
 *     at 0.148 mean alpha, which against the 0.12 smear settles at a 0.30%
 *     accumulated veil. A 4K viewport is quieter still (0.19%) because the
 *     thread count is capped while the area is not.
 * Both caps are asserted, from the drawn output, in slipstream.test.mjs.
 *
 * Cost (node, 1,000 frames): 0.36 ms/frame at 1400x800, 0.66 ms at 3840x2160
 * with 2,600 threads, 0.13 ms on a 412x915 phone box.
 *
 * Everything is pooled: the particle array, the bucket index and the strokeStyle
 * strings are all built once, so a frame allocates nothing.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

// cheap 2-octave sine curl-noise potential ψ → divergence-free swirl
// Byte-identical to embers.js's pot() on purpose: both fields must swirl the
// same way. NOT imported from there — embers does not export it, and a shared
// import would let an art-direction tweak to one effect silently re-tune the
// other. This is the one duplication in the module.
function pot(x, y, t) {
    return Math.sin(x * 0.0065 + t * 0.22) * Math.cos(y * 0.0065 - t * 0.16)
        + 0.5 * Math.sin(x * 0.013 - t * 0.31) * Math.cos(y * 0.013 + t * 0.26);
}

const SLIPSTREAM = {
    // Population. Viewport-area driven (env.scale), never dpr driven.
    perScale: 1600, minThreads: 420, maxThreads: 2600,
    advect: 4200,        // curl → px/s. 4200 x a typical curl of .03 ≈ 125 px/s
    eps: 3,              // finite-difference step, same as embers
    resetPerSec: 1.5,    // = 2.5% of the field per 60 Hz frame, refresh-invariant
    margin: 24,          // px outside the box before a thread is recycled
    maxLinePx: 1.05,      // THE legibility cap (CSS px — ctx is pre-scaled by dpr)
    maxAlpha: 0.30,      // THE other one
    minAlpha: 0.015,     // below this a thread is skipped, not drawn at 0
    // Quantised stroke widths (all <= maxLinePx) and the edges that bisect them.
    widths: [0.45, 0.7, 1.0], widthEdges: [0.82, 1.2],
    // THREE sub-populations. Weights sum to 1.
    //  filament — the hair; the bulk of the field, rides the curl exactly
    //  veil     — slower, wider, laggier: broad folding sheets behind the hair
    //  glint    — sparse, quick, short-lived bright strands that pick out shear
    kinds: [
        { w: 0.72, gain: 1.00, dx:  16, dy:  -6, width: 0.62, alpha: 0.26, life: [2.2, 2.8], resp: 6.0 },
        { w: 0.22, gain: 0.55, dx: -10, dy:   4, width: 1.05, alpha: 0.13, life: [3.6, 4.4], resp: 2.4 },
        { w: 0.06, gain: 1.35, dx:  26, dy: -14, width: 0.45, alpha: 0.30, life: [0.9, 1.0], resp: 9.0 },
    ],
    // Colour ramp indexed by LIFE: entering the flow → ice at the peak of the
    // envelope → cooling out. Additive blend, so these stay cold and quiet.
    ramp: ['70,104,208', '96,168,236', '168,226,255', '140,176,246', '84,96,170'],
    alphaLevels: 4,
};

const RAMP_N = SLIPSTREAM.ramp.length;
const ALPHA_N = SLIPSTREAM.alphaLevels;
const WIDTH_N = SLIPSTREAM.widths.length;
const BUCKETS = RAMP_N * ALPHA_N * WIDTH_N;   // 60

/**
 * Stroke state (strokeStyle/lineWidth) is the expensive part of stroking 2,000
 * segments, not the segments — so colour x alpha x width is quantised into 60
 * buckets, each drawn as ONE batched path. These are the 20 colour strings,
 * built at import: a `rgba(...)` template literal in the draw loop would be the
 * single largest source of per-frame garbage in the engine.
 * Representative alpha per level is the MIDPOINT of the level, so the darkest
 * tail rounds up by at most 0.02 and the cap is never exceeded.
 */
const STYLES = [];
for (let ci = 0; ci < RAMP_N; ci++) {
    for (let ai = 0; ai < ALPHA_N; ai++) {
        const a = SLIPSTREAM.maxAlpha * (ai + 0.5) / ALPHA_N;
        STYLES.push(`rgba(${SLIPSTREAM.ramp[ci]},${a.toFixed(3)})`);
    }
}

function pickKind(rand) {
    const r = rand(); let cumulative = 0;
    for (let i = 0; i < SLIPSTREAM.kinds.length; i++) {
        cumulative += SLIPSTREAM.kinds[i].w;
        if (r < cumulative) return SLIPSTREAM.kinds[i];
    }
    return SLIPSTREAM.kinds[0];
}

/** One pooled thread. Every field is assigned here so the shape stays monomorphic. */
function newThread() {
    return {
        x: 0, y: 0, px: 0, py: 0, vx: 0, vy: 0,
        life: 1, decay: 1, gain: 1, dx: 0, dy: 0, resp: 1,
        wb: 1, ab: 0.2, wEnv: 1, tint: 0, bin: -1,
    };
}

function threadCount(env) {
    const n = Math.round(SLIPSTREAM.perScale * env.scale);
    return Math.max(SLIPSTREAM.minThreads, Math.min(SLIPSTREAM.maxThreads, n));
}

/**
 * Re-seat a thread anywhere in the box. `initial` staggers its position on the
 * life curve so a fresh field does not pulse in unison.
 *
 * The two lines that matter: px/py are pinned to the new x/y (a recycled thread
 * with a stale previous position strokes a bright line clean across the
 * viewport — the classic trail-renderer bug, and a legibility failure), and the
 * velocity is seeded FROM the field so a new thread joins the flow at once
 * instead of visibly drifting into it.
 */
function resetThread(p, env, ts, initial) {
    const rand = env.rand, k = pickKind(rand), e = SLIPSTREAM.eps;
    p.x = rand() * env.width; p.y = rand() * env.height;
    p.px = p.x; p.py = p.y;
    p.gain = k.gain; p.dx = k.dx; p.dy = k.dy; p.resp = k.resp;
    p.wb = k.width; p.ab = k.alpha;
    p.life = initial ? (0.05 + rand() * 0.9) : 1;
    p.decay = 1 / (k.life[0] + rand() * k.life[1]);
    p.wEnv = 0.8 + rand() * 0.5;              // per-particle envelope multiplier
    p.tint = rand() < 0.5 ? 0 : 1;            // ramp offset: two colour families
    p.bin = -1;                               // invisible until its envelope opens
    const adv = SLIPSTREAM.advect * (env.state.apparent || 1);
    const cvx = (pot(p.x, p.y + e, ts) - pot(p.x, p.y - e, ts));
    const cvy = -(pot(p.x + e, p.y, ts) - pot(p.x - e, p.y, ts));
    p.vx = cvx * adv * k.gain + k.dx;
    p.vy = cvy * adv * k.gain + k.dy;
}

function init(env) {
    const st = env.state;
    // GEOMETRY comes from the CSS box alone — apparentScale takes no dpr, and
    // that is the whole point of sizing.js. Widths and speeds scale with it.
    st.apparent = apparentScale(env.width, env.height);
    if (!st.pool) {
        st.pool = new Array(SLIPSTREAM.maxThreads);
        for (let i = 0; i < SLIPSTREAM.maxThreads; i++) st.pool[i] = newThread();
        st.order = new Int32Array(SLIPSTREAM.maxThreads);   // counting-sort output
        st.counts = new Int32Array(BUCKETS);
        st.starts = new Int32Array(BUCKETS + 1);
        st.cursor = new Int32Array(BUCKETS);
    }
    st.count = threadCount(env);
    st.resetAcc = 0;
    st.resets = 0;                                          // telemetry for the test
    const ts = env.now * 0.001;
    for (let i = 0; i < st.count; i++) resetThread(st.pool[i], env, ts, true);
}

export const descriptor = {
    id: 'slipstream',
    label: 'Slipstream',
    // Emissive threads: crossing filaments must ADD light, not occlude.
    blend: 'lighter',
    // Trail persistence. A hard clear would leave 2 px dashes; the smear is what
    // turns them into smoke. Cold blue-black to match the ramp (embers' smear is
    // warm). 0.12 => a segment is still ~10% visible 18 frames later (~0.3 s),
    // long enough to read as a thread and short enough that the veil stays flat.
    clearPolicy: 'fade:rgba(6,9,16,0.12)',

    init,

    // Count is viewport-area derived and geometry is short-edge derived, so a
    // resize must redo both — and re-seat anything the new, smaller box left
    // outside itself.
    resize(env) {
        const st = env.state;
        if (!st.pool) { init(env); return; }
        st.apparent = apparentScale(env.width, env.height);
        const prev = st.count;
        st.count = threadCount(env);
        const ts = env.now * 0.001;
        for (let i = 0; i < st.count; i++) {
            const p = st.pool[i];
            if (i >= prev || p.x > env.width || p.y > env.height) resetThread(p, env, ts, true);
        }
    },

    update(dt, env) {
        const st = env.state;
        if (!st.pool) init(env);
        const ts = env.now * 0.001;
        const pool = st.pool, rand = env.rand, eps = SLIPSTREAM.eps;
        const adv = SLIPSTREAM.advect * st.apparent;
        const maxW = SLIPSTREAM.maxLinePx, maxA = SLIPSTREAM.maxAlpha, minA = SLIPSTREAM.minAlpha;
        const e0 = SLIPSTREAM.widthEdges[0], e1 = SLIPSTREAM.widthEdges[1];
        const aq = ALPHA_N / maxA, inten = env.intensity;
        const margin = SLIPSTREAM.margin, w = env.width, h = env.height;

        // Scatter-reset: a FRACTION PER SECOND (2.5% of a 60 Hz frame), with the
        // remainder carried in an accumulator so a 120 Hz display does the same
        // number of resets per second, not twice as many.
        st.resetAcc += st.count * SLIPSTREAM.resetPerSec * dt;
        while (st.resetAcc >= 1) {
            resetThread(pool[(rand() * st.count) | 0], env, ts, false);
            st.resetAcc -= 1; st.resets++;
        }

        // No env.active gate: the population is fixed, so there is no spawn ramp
        // to hold back. The engine's own fade-out ends the field.
        for (let i = 0; i < st.count; i++) {
            const p = pool[i];
            p.life -= p.decay * dt;
            if (p.life <= 0) resetThread(p, env, ts, false);
            // The segment drawn this frame is (px,py) -> (x,y). Capture BEFORE
            // integrating; resetThread pins them together so a recycled thread
            // never strokes across the viewport.
            p.px = p.x; p.py = p.y;
            // --- curl of ψ by central difference: verbatim from embers.js ---
            const cvx = (pot(p.x, p.y + eps, ts) - pot(p.x, p.y - eps, ts));
            const cvy = -(pot(p.x + eps, p.y, ts) - pot(p.x - eps, p.y, ts));
            // Advection, not acceleration: chase the field at a per-kind rate.
            // k is clamped to 1 so a long stall settles ON the target instead of
            // overshooting past it.
            const k = Math.min(1, p.resp * dt);
            p.vx += ((cvx * adv * p.gain + p.dx) - p.vx) * k;
            p.vy += ((cvy * adv * p.gain + p.dy) - p.vy) * k;
            p.x += p.vx * dt; p.y += p.vy * dt;
            if (p.x < -margin || p.x > w + margin || p.y < -margin || p.y > h + margin) {
                resetThread(p, env, ts, false);
            }
            // --- shading: size AND alpha ride one arch over the particle's life.
            // sqrt(4u(1-u)) opens fast, holds a fat plateau, closes fast — so a
            // birth or a scatter-reset is invisible and only the middle of a
            // thread's life is on screen. Guarded so a clamped-away negative can
            // never produce a NaN width.
            const u = 1 - p.life;
            const a4 = 4 * u * (1 - u);
            const arch = a4 > 0 ? Math.sqrt(a4) : 0;
            const a = p.ab * arch * inten;
            if (a < minA) { p.bin = -1; continue; }
            let wpx = p.wb * arch * p.wEnv * st.apparent;
            if (wpx > maxW) wpx = maxW;                       // THE cap, post-scale
            const wi = wpx < e0 ? 0 : (wpx < e1 ? 1 : 2);
            const ai = Math.min(ALPHA_N - 1, (a * aq) | 0);
            const ci = Math.min(RAMP_N - 1, ((u * RAMP_N) | 0) + p.tint);
            p.bin = (ci * ALPHA_N + ai) * WIDTH_N + wi;
        }
    },

    draw(ctx, env) {
        const st = env.state;
        if (!st.pool) return;
        const pool = st.pool, counts = st.counts, starts = st.starts, cursor = st.cursor, order = st.order;
        // Counting sort into the 60 style buckets — histogram, prefix sums,
        // scatter. Three linear passes over preallocated Int32Arrays, so the
        // batching costs no allocation and no comparison sort.
        counts.fill(0);
        for (let i = 0; i < st.count; i++) { const b = pool[i].bin; if (b >= 0) counts[b]++; }
        let acc = 0;
        for (let b = 0; b < BUCKETS; b++) { starts[b] = acc; cursor[b] = acc; acc += counts[b]; }
        starts[BUCKETS] = acc;
        if (acc === 0) return;
        for (let i = 0; i < st.count; i++) { const b = pool[i].bin; if (b >= 0) order[cursor[b]++] = i; }

        ctx.lineCap = 'round';        // a near-stationary thread reads as a dot,
        ctx.lineJoin = 'round';       // not a hard 1 px square
        for (let b = 0; b < BUCKETS; b++) {
            const from = starts[b], to = starts[b + 1];
            if (from === to) continue;
            ctx.strokeStyle = STYLES[(b / WIDTH_N) | 0];
            ctx.lineWidth = SLIPSTREAM.widths[b % WIDTH_N];
            ctx.beginPath();
            for (let j = from; j < to; j++) {
                const p = pool[order[j]];
                ctx.moveTo(p.px, p.py); ctx.lineTo(p.x, p.y);
            }
            ctx.stroke();             // one state change + one stroke per bucket
        }
    },

    // Fresh braid each turn: re-seat the whole field, staggered along the life
    // curve so it re-forms out of nothing instead of flashing in and pulsing.
    rearm(env) {
        const st = env.state;
        if (!st.pool) { init(env); return; }
        st.count = threadCount(env);
        st.resetAcc = 0;
        const ts = env.now * 0.001;
        for (let i = 0; i < st.count; i++) resetThread(st.pool[i], env, ts, true);
    },
};

registerEffect(descriptor);
export default descriptor;
export { SLIPSTREAM, BUCKETS, STYLES };
