/**
 * Effect: AURORA CURTAIN (id 'aurora').
 *
 * Slow vertical curtains of crimson-through-violet light hanging in the TOP half
 * of the canvas, brightest at their base, folding through each other like silk.
 *
 * How the volumetric look is faked, and why each half of it exists:
 *
 *   ONE baked vertical gradient per tint (transparent -> accent -> transparent),
 *   blitted 30-46 times as narrow overlapping strips. Baking is per RESIZE, not
 *   per frame — a per-strip createLinearGradient() would be ~46 gradient objects
 *   a frame, which is the exact cost stars.js is still paying and is the first
 *   thing to fix when a field feels heavy.
 *
 *   A 2-3 OCTAVE SINE SUM drives each strip's x-offset and height. The sum is
 *   deliberately NOT smoothed: adjacent strips carry a per-strip PHASE STEP, so
 *   the octaves fall out of alignment along x and the field breaks into the hard
 *   vertical bands that read as "curtain". A single smooth octave — or one shared
 *   offset for the whole sheet — reads as a drifting blob, which is the failure
 *   mode this effect is most likely to regress into.
 *
 * LEGIBILITY BUDGET (non-negotiable — this is a backdrop behind chat text):
 *
 *   Curtains are large bright REGIONS rather than sparse particles, so the usual
 *   "particles are small, it'll be fine" reasoning does not apply and the budget
 *   is enforced arithmetically instead of by eye:
 *
 *     - every strip's bottom edge is clamped to BAND_FRAC (top 55%) of the
 *       canvas, so nothing is ever drawn over the message area;
 *     - the baked gradient's LAST stop is alpha 0, so the light has already
 *       reached zero by the time it gets to that clamp line;
 *     - additive blending means overlapping strips SUM, so the per-strip alpha is
 *       PEAK_ALPHA / MAX_STACK, where MAX_STACK is the analytic worst case
 *       derived below. The total additive alpha in any column can therefore never
 *       exceed PEAK_ALPHA (8%). aurora.test.mjs asserts the measured column sum,
 *       not the arithmetic.
 *
 * The MAX_STACK derivation (keep this true if you retune OVERLAP or WOBBLE_FRAC):
 * strips sit one stripW apart and are drawn at most OVERLAP=1.7 stripW wide, so
 * tiling alone stacks at most 2 per sheet. Each strip's x-offset is bounded to
 * WOBBLE_FRAC=0.5 stripW, so two neighbours can close by at most 1.0 stripW —
 * one more overlap, giving 3 per sheet. Two sheets => 6.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

const TAU = Math.PI * 2;

/** Curtains live in the TOP fraction of the canvas. Nothing draws below it. */
const BAND_FRAC = 0.55;
/** Ceiling on the TOTAL additive alpha any single column may accumulate. */
const PEAK_ALPHA = 0.165;
/** Analytic worst-case simultaneous strips over one column (see header). */
const MAX_STACK = 6;
/** Per-strip alpha ceiling. The budget, divided by the worst case. */
const STRIP_ALPHA = PEAK_ALPHA / MAX_STACK;
/** Drawn width as a multiple of the slot pitch — strips cross-fade, no seams. */
const OVERLAP = 1.7;
/** Hard bound on |x-offset| as a fraction of stripW. MAX_STACK depends on it. */
const WOBBLE_FRAC = 0.5;
/** Vertical wobble of a curtain's base, in CSS px at apparentScale 1.0. */
const WOBBLE_PX = 9;
/** Below this a blit is invisible and costs a drawImage. Skip it. */
const MIN_ALPHA = 0.0006;
/** Baked strip width. The gradient is purely vertical, so this can be tiny. */
const BAKE_W = 8;

/**
 * Colour ramp, crimson (the Portal accent, rgb(255,74,74)) through magenta to
 * violet. Indexed by a strip's LIFE, so a curtain visibly cools as it folds
 * shut. The wrap from index-high back to index-low at life 1 -> 0 is invisible
 * because foldEnvelope() has already taken the alpha to zero at both ends.
 */
const TINTS = [
    [255, 74, 74],    // accent crimson
    [236, 62, 104],
    [198, 54, 146],   // magenta
    [150, 60, 190],
    [104, 68, 214],   // violet
];

/**
 * Alpha profile of the baked strip, top -> bottom, as [offset, alpha].
 * The peak sits at 0.80 because an aurora is brightest at its BASE, and the
 * final stop MUST stay 0 — that zero is what keeps the light off the chat text.
 */
const GRAD_STOPS = [
    [0.00, 0.00],
    [0.22, 0.30],
    [0.55, 0.62],
    [0.80, 1.00],
    [0.93, 0.34],
    [1.00, 0.00],
];

/**
 * The two SUB-POPULATIONS. Not two copies with jitter — they differ in count,
 * fold rate, band position, height, octave frequencies, ramp region and
 * brightness, so the two sheets pass through each other at different speeds
 * instead of moving as one slab.
 *
 * `oct` rows are [frequency(Hz), amplitude(fraction of stripW), phase-step-per-strip].
 * The x amplitudes of each sheet MUST sum to <= WOBBLE_FRAC or MAX_STACK lies.
 */
const SHEETS = [
    {
        key: 'crimson', base: 24, min: 18, max: 28,
        baseFrac: 0.52, heightFrac: 0.78, alphaMul: 1.00,
        // Rate ranges of the two sheets must not TOUCH (a test asserts it): a
        // violet strip folding at exactly crimson speed blurs the two-population
        // read that makes the field look layered rather than noisy.
        hue0: 0.0, hueSpan: 2.2, rate: [0.030, 0.050],
        oct: [[0.11, 0.30, 0.55], [0.27, 0.14, 1.30], [0.63, 0.06, 2.70]],
        hOct: [[0.09, 0.22, 0.40], [0.23, 0.11, 1.10]],
    },
    {
        key: 'violet', base: 15, min: 10, max: 18,
        baseFrac: 0.40, heightFrac: 0.62, alphaMul: 0.85,
        hue0: 2.0, hueSpan: 2.0, rate: [0.055, 0.095],
        oct: [[0.17, 0.26, 0.90], [0.41, 0.16, 2.10], [0.88, 0.08, 3.60]],
        hOct: [[0.14, 0.26, 0.75], [0.35, 0.13, 1.90]],
    },
];

/** Fast swell, long ebb. 0 at both ends, 1 at the crest. */
const FOLD_RISE = 0.28;

/**
 * SIZE-OVER-LIFE curve. A curtain opens quickly and closes slowly; the returned
 * 0..1 envelope drives height, width and alpha together so a fold fades out
 * instead of popping. Multiplied by a per-strip `env0` at the call site — the
 * curve is the shape, `env0` is that strip's personal amplitude.
 * @param {number} t life in [0,1)
 */
export function foldEnvelope(t) {
    if (!(t > 0) || t >= 1) return 0;
    if (t < FOLD_RISE) { const u = t / FOLD_RISE; return u * u * (3 - 2 * u); }
    const u = 1 - (t - FOLD_RISE) / (1 - FOLD_RISE);
    return u * u * (3 - 2 * u);
}

/**
 * Bake one vertical gradient per tint.
 *
 * Headless (unit test / SSR) has no `document`, so stand-in handles are returned
 * instead of null: draw() then still runs its full arithmetic and the alpha
 * budget stays measurable from a test, which is the whole point of the injected
 * env. `drawImage` is never actually reached without a real 2D context.
 */
function bakeBands(env) {
    const h = Math.max(1, Math.round(env.height * BAND_FRAC));
    if (typeof document === 'undefined') {
        return TINTS.map((c, i) => ({ headless: true, tint: i, width: BAKE_W, height: h }));
    }
    return TINTS.map((c) => {
        const cv = document.createElement('canvas');
        cv.width = BAKE_W; cv.height = h;
        const x = cv.getContext('2d');
        const g = x.createLinearGradient(0, 0, 0, h);
        for (let i = 0; i < GRAD_STOPS.length; i++) {
            g.addColorStop(GRAD_STOPS[i][0], `rgba(${c[0]},${c[1]},${c[2]},${GRAD_STOPS[i][1]})`);
        }
        x.fillStyle = g;
        x.fillRect(0, 0, BAKE_W, h);
        return cv;
    });
}

/** Strips per sheet. GEOMETRY-derived (never dpr), clamped so the curtain
 *  banding period stays readable on a phone and doesn't turn to fringe on 4K. */
function stripCount(sheet, geo) {
    return Math.max(sheet.min, Math.min(sheet.max, Math.round(sheet.base * (0.80 + 0.20 * geo))));
}

/**
 * One strip. Every field the hot loop touches is flattened onto this object at
 * build/resize time — draw() does arithmetic on numbers only, no nested lookups
 * into SHEETS and no allocation.
 */
function makeStrip(sheetIdx, sh, slot, rand) {
    return {
        sheet: sheetIdx, slot,
        life: rand(),                                            // staggered: sheets never pulse in unison
        rate: sh.rate[0] + rand() * (sh.rate[1] - sh.rate[0]),
        env0: 0.78 + rand() * 0.44,                              // per-strip envelope multiplier
        // x octaves: frequency (rad/s), phase (random + per-strip step => travelling wave)
        f0: sh.oct[0][0] * TAU, p0: rand() * TAU + slot * sh.oct[0][2],
        f1: sh.oct[1][0] * TAU, p1: rand() * TAU + slot * sh.oct[1][2],
        f2: sh.oct[2][0] * TAU, p2: rand() * TAU + slot * sh.oct[2][2],
        // height octaves
        g0: sh.hOct[0][0] * TAU, q0: rand() * TAU + slot * sh.hOct[0][2], ah0: sh.hOct[0][1],
        g1: sh.hOct[1][0] * TAU, q1: rand() * TAU + slot * sh.hOct[1][2], ah1: sh.hOct[1][1],
        // base wobble + shimmer
        fy: (0.07 + rand() * 0.05) * TAU, py: rand() * TAU,
        fs: (0.13 + rand() * 0.10) * TAU, ps: rand() * TAU,
        hueLo: sh.hue0 + (rand() - 0.5) * 0.4, hueSpan: sh.hueSpan,
        alphaMax: STRIP_ALPHA * sh.alphaMul,
        // filled by layout()
        stripW: 0, slotX: 0, drawW: 0, hMax: 0, baseY0: 0, ay: 0,
        ax0: 0, ax1: 0, ax2: 0,
    };
}

/** Resolve every viewport-dependent quantity. Called on init and on resize. */
function layout(env) {
    const st = env.state;
    // GEOMETRY only. apparentScale takes the CSS box and never devicePixelRatio —
    // letting dpr in here is the shipped bug sizing.js exists to prevent.
    const geo = apparentScale(env.width, env.height);
    st.bandH = Math.max(1, env.height * BAND_FRAC);
    st.bandBottom = env.height * BAND_FRAC;
    const strips = st.strips;
    for (let i = 0; i < strips.length; i++) {
        const s = strips[i];
        const sh = SHEETS[s.sheet];
        const sw = env.width / st.counts[s.sheet];
        s.stripW = sw;
        s.slotX = (s.slot + 0.5) * sw;
        s.drawW = sw * OVERLAP;
        s.hMax = st.bandH * sh.heightFrac;
        s.baseY0 = env.height * sh.baseFrac;
        s.ay = WOBBLE_PX * geo;
        s.ax0 = sh.oct[0][1] * sw;
        s.ax1 = sh.oct[1][1] * sw;
        s.ax2 = sh.oct[2][1] * sw;
    }
}

function build(env) {
    const st = env.state, rand = env.rand;
    const geo = apparentScale(env.width, env.height);
    st.t = 0;
    st.counts = SHEETS.map(sh => stripCount(sh, geo));
    st.strips = [];
    for (let si = 0; si < SHEETS.length; si++) {
        for (let i = 0; i < st.counts[si]; i++) st.strips.push(makeStrip(si, SHEETS[si], i, rand));
    }
    layout(env);
    st.bands = bakeBands(env);
}

/**
 * Resize WITHOUT rebuilding: the composer's ResizeObserver fires on every
 * keystroke that changes its height, and a full rebuild there would restart
 * every curtain mid-fold — a visible blink once a second while typing. Surviving
 * strips keep their phase; only the count delta is built or dropped.
 * (New strips append out of slot order; additive blending is commutative, so
 * draw order is irrelevant.)
 */
function reconcile(env) {
    const st = env.state, rand = env.rand;
    const geo = apparentScale(env.width, env.height);
    const want = SHEETS.map(sh => stripCount(sh, geo));
    let changed = false;
    for (let i = 0; i < want.length; i++) if (want[i] !== st.counts[i]) changed = true;
    if (changed) {
        let w = 0;
        for (let i = 0; i < st.strips.length; i++) {
            const s = st.strips[i];
            if (s.slot < want[s.sheet]) st.strips[w++] = s;
        }
        st.strips.length = w;
        for (let si = 0; si < SHEETS.length; si++) {
            for (let i = st.counts[si]; i < want[si]; i++) st.strips.push(makeStrip(si, SHEETS[si], i, rand));
        }
        st.counts = want;
    }
    layout(env);
    st.bands = bakeBands(env);      // band height is viewport-derived — always rebake
}

export const descriptor = {
    id: 'aurora',
    label: 'Aurora Curtain',
    // Emissive: overlapping curtains must ADD light so folds brighten where they
    // cross. That is also why the alpha budget above is a SUM, not a max.
    blend: 'lighter',
    // Hard clear. A fade would let 8%-per-frame curtains accumulate into a
    // permanent bright wash across the top of the chat — the budget only holds
    // if each frame starts from nothing.
    clearPolicy: 'clear',

    init(env) { build(env); },

    resize(env) { if (env.state.strips) reconcile(env); else build(env); },

    update(dt, env) {
        const st = env.state;
        if (!st.strips) { build(env); return; }
        // Own dt-accumulated clock rather than env.now: every sine below is then a
        // function of INTEGRATED time, so 600 frames at 60 Hz and 1200 at 120 Hz
        // land on the same field. No rand() in here — the hot path is pure
        // arithmetic and the physics is reproducible from the seed alone.
        st.t += dt;
        const strips = st.strips;
        for (let i = 0; i < strips.length; i++) {
            const s = strips[i];
            s.life += s.rate * dt;
            if (s.life >= 1) s.life -= Math.floor(s.life);   // wrap; survives a long stall
        }
    },

    draw(ctx, env) {
        const st = env.state;
        const bands = st.bands;
        if (!bands || !st.strips) return;
        // Clamped to <=1: the alpha budget is only sound if nothing can scale a
        // strip ABOVE its declared ceiling.
        const gain = env.intensity > 1 ? 1 : (env.intensity > 0 ? env.intensity : 0);
        if (gain <= 0) return;
        const t = st.t;
        const bandBottom = st.bandBottom;
        const topIdx = bands.length - 1;
        const strips = st.strips;
        for (let i = 0; i < strips.length; i++) {
            const s = strips[i];
            const fold = foldEnvelope(s.life);
            if (fold <= 0) continue;
            // 3-octave sine sum -> the horizontal fold. Bounded by construction to
            // WOBBLE_FRAC * stripW (see the SHEETS amplitude rule).
            const xo = s.ax0 * Math.sin(t * s.f0 + s.p0)
                + s.ax1 * Math.sin(t * s.f1 + s.p1)
                + s.ax2 * Math.sin(t * s.f2 + s.p2);
            // 2-octave sine sum -> the height ripple that makes the base uneven.
            const hw = s.ah0 * Math.sin(t * s.g0 + s.q0)
                + s.ah1 * Math.sin(t * s.g1 + s.q1);
            // size-over-life curve x per-strip envelope multiplier x ripple
            const h = s.hMax * (0.42 + 0.58 * fold) * s.env0 * (1 + hw);
            if (!(h > 0.5)) continue;
            const w = s.drawW * (0.76 + 0.24 * fold);
            let baseY = s.baseY0 + s.ay * Math.sin(t * s.fy + s.py);
            if (baseY > bandBottom) baseY = bandBottom;      // THE legibility clamp
            const a = s.alphaMax * gain * (0.30 + 0.70 * fold)
                * (0.78 + 0.22 * Math.sin(t * s.fs + s.ps));
            if (a < MIN_ALPHA) continue;
            // COLOUR RAMP indexed by life: crimson at the fold's open, violet at
            // its close. (+0.5 | 0 is round(); hueLo is always >= 0 after clamp.)
            let idx = (s.hueLo + s.hueSpan * s.life + 0.5) | 0;
            if (idx < 0) idx = 0; else if (idx > topIdx) idx = topIdx;
            ctx.globalAlpha = a;
            ctx.drawImage(bands[idx], s.slotX + xo - w * 0.5, baseY - h, w, h);
        }
    },

    // A curtain does not restart when a turn does — the aurora is ambient, and
    // re-staggering every life on activation would blink the whole field. Rearm
    // therefore only guarantees the field EXISTS (first activation, or after a
    // mode switch wiped env.state).
    rearm(env) { if (!env.state.strips) build(env); },
};

registerEffect(descriptor);
export default descriptor;
export {
    SHEETS, TINTS, GRAD_STOPS, BAND_FRAC, PEAK_ALPHA, MAX_STACK,
    STRIP_ALPHA, OVERLAP, WOBBLE_FRAC, stripCount,
};
