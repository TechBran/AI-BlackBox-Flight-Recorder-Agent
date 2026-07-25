// Aurora Curtain physics — headless, seeded, no DOM.
//
// The risk this effect carries is not "does it look nice", it is "does a large
// bright REGION sit on top of the chat text". So the legibility budget is an
// ACTUAL ASSERTION here (measured additive alpha per column over 1000 frames),
// not a comment claiming the arithmetic works out.
//
// Run: node --test Portal/modules/fx/effects/aurora.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, getEffect } from "../registry.js";
import descriptor, {
    foldEnvelope, SHEETS, TINTS, GRAD_STOPS, BAND_FRAC, PEAK_ALPHA,
    MAX_STACK, STRIP_ALPHA, OVERLAP, WOBBLE_FRAC, stripCount,
} from "./aurora.js";

const TAU = Math.PI * 2;

// Seeded PRNG: the field must be reproducible from the seed alone, otherwise a
// budget failure is unreproducible and therefore unfixable.
function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
        a = (a + 0x6D2B79F5) | 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

function makeEnv(over = {}) {
    return createEnv({
        width: 1440, height: 900, scale: 1, intensity: 1, active: true,
        rand: mulberry32(0xA47), state: {}, ...over,
    });
}

/**
 * Stand-in 2D context that measures what actually reaches the canvas.
 * `cols` accumulates globalAlpha per CSS-pixel column for ONE frame — under
 * 'lighter' that sum IS the brightness the chat text has to compete with, and
 * the baked gradient's own alpha is <= 1, so this is an upper bound.
 */
class Probe {
    constructor(width) {
        this.w = Math.ceil(width);
        this.cols = new Float64Array(this.w);
        this.globalAlpha = 1;
        this.maxColumn = 0;
        this.maxBottom = -Infinity;
        this.calls = 0;
        this.bad = 0;
        this.minH = Infinity; this.maxH = -Infinity;
        this.xSum = 0;
        this.tints = new Set();
    }
    frame() { this.cols.fill(0); this.xSum = 0; }
    endFrame() {
        for (let i = 0; i < this.w; i++) if (this.cols[i] > this.maxColumn) this.maxColumn = this.cols[i];
    }
    drawImage(img, x, y, w, h) {
        this.calls++;
        const a = this.globalAlpha;
        if (!(Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(w)
            && Number.isFinite(h) && Number.isFinite(a))) { this.bad++; return; }
        if (y + h > this.maxBottom) this.maxBottom = y + h;
        if (h < this.minH) this.minH = h;
        if (h > this.maxH) this.maxH = h;
        this.xSum += x;
        if (img && img.tint !== undefined) this.tints.add(img.tint);
        const lo = Math.max(0, Math.floor(x));
        const hi = Math.min(this.w - 1, Math.ceil(x + w));
        for (let i = lo; i <= hi; i++) this.cols[i] += a;
    }
}

function run(env, probe, frames, dt) {
    for (let f = 0; f < frames; f++) {
        probe.frame();
        descriptor.update(dt, env);
        descriptor.draw(probe, env);
        probe.endFrame();
    }
}

// The colour-ramp index draw() computes. Mirrored so per-sheet ramp REGIONS can
// be asserted; the observed-tint assertion below guards the mirror from drifting.
function tintIndex(s, life) {
    let i = (s.hueLo + s.hueSpan * life + 0.5) | 0;
    return i < 0 ? 0 : (i > TINTS.length - 1 ? TINTS.length - 1 : i);
}

// -- contract -----------------------------------------------------------------

test("registers itself with the declared blend and clear policy", () => {
    const d = getEffect("aurora");
    assert.ok(d, "aurora must be in the registry after import");
    assert.equal(d.blend, "lighter", "curtains are emissive — overlaps add light");
    assert.equal(d.clearPolicy, "clear", "a fade would let the 8% budget accumulate frame over frame");
    assert.equal(d.label, "Aurora Curtain");
    for (const hook of ["init", "resize", "update", "draw", "rearm"]) {
        assert.equal(typeof d[hook], "function", `${hook} must be a function`);
    }
});

// -- population ---------------------------------------------------------------

test("strip population stays in the 24-48 curtain band on every viewport", () => {
    // Too few and the banding reads as fenceposts; too many and it reads as
    // fringe. Both ends must hold from a phone to a 4K desktop.
    for (const [w, h] of [[360, 640], [412, 915], [900, 900], [1440, 900], [2560, 1440], [3840, 2160]]) {
        const env = makeEnv({ width: w, height: h });
        descriptor.init(env);
        const n = env.state.strips.length;
        assert.ok(n >= 24 && n <= 48, `${w}x${h} produced ${n} strips (want 24..48)`);
        assert.equal(n, env.state.counts[0] + env.state.counts[1]);
    }
});

test("two sub-populations, genuinely different — not one sheet twice", () => {
    const env = makeEnv();
    descriptor.init(env);
    const sheets = new Set(env.state.strips.map(s => s.sheet));
    assert.equal(sheets.size, 2, "exactly two sheets must be alive");
    const [a, b] = SHEETS;
    assert.notEqual(a.baseFrac, b.baseFrac, "sheets must hang at different heights");
    assert.notEqual(a.heightFrac, b.heightFrac);
    assert.notEqual(a.alphaMul, b.alphaMul);
    assert.ok(b.rate[0] > a.rate[1], "the violet sheet must fold strictly faster than the crimson one");
    assert.notEqual(a.oct[0][0], b.oct[0][0], "sheets must ripple at different frequencies");
    // ...and the two populations must actually differ in the field, not just in config.
    const rate = i => env.state.strips.filter(s => s.sheet === i).map(s => s.rate);
    assert.ok(Math.min(...rate(1)) > Math.max(...rate(0)), "no rate overlap between sheets");
});

// -- THE LEGIBILITY BUDGET ----------------------------------------------------

test("total additive alpha in any column never exceeds the 8% budget", () => {
    // 1000 frames at 60 Hz = ~16 s, several full fold cycles of both sheets.
    const env = makeEnv();
    const probe = new Probe(env.width);
    descriptor.init(env);
    run(env, probe, 1000, 1 / 60);
    assert.ok(probe.calls > 10000, `expected a busy field, got ${probe.calls} blits`);
    assert.ok(probe.maxColumn <= PEAK_ALPHA + 1e-9,
        `peak column alpha ${probe.maxColumn.toFixed(5)} busts the ${PEAK_ALPHA} budget`);
    assert.ok(probe.maxColumn > PEAK_ALPHA * 0.2,
        `budget is barely used (${probe.maxColumn.toFixed(5)}) — the field would be invisible`);
});

test("nothing is ever drawn below the top 55% of the canvas", () => {
    const env = makeEnv();
    const probe = new Probe(env.width);
    descriptor.init(env);
    run(env, probe, 1000, 1 / 60);
    const limit = env.height * BAND_FRAC;
    assert.ok(probe.maxBottom <= limit + 1e-6,
        `a curtain reached y=${probe.maxBottom.toFixed(2)}, past the ${limit} clamp into the message area`);
});

test("the baked gradient's alpha reaches ZERO at the clamp line, and peaks at the base", () => {
    assert.equal(GRAD_STOPS[0][1], 0, "top edge must fade in from nothing");
    assert.equal(GRAD_STOPS[GRAD_STOPS.length - 1][1], 0,
        "bottom edge MUST be alpha 0 — this is what keeps light off the chat text");
    let peakAt = 0, peak = 0;
    for (const [off, a] of GRAD_STOPS) if (a > peak) { peak = a; peakAt = off; }
    assert.ok(peakAt >= 0.7, `brightest stop is at ${peakAt}; an aurora is brightest at its BASE`);
    assert.ok(peak <= 1, "a gradient stop above alpha 1 is meaningless and breaks the budget maths");
    for (let i = 1; i < GRAD_STOPS.length; i++) {
        assert.ok(GRAD_STOPS[i][0] > GRAD_STOPS[i - 1][0], "stop offsets must be strictly increasing");
    }
});

test("the per-strip alpha ceiling is exactly the budget divided by the worst-case stack", () => {
    // If someone retunes OVERLAP or WOBBLE_FRAC without redoing MAX_STACK, the
    // measured budget test above starts failing — this pins the derivation itself.
    assert.equal(STRIP_ALPHA, PEAK_ALPHA / MAX_STACK);
    const perSheet = Math.ceil(OVERLAP) + Math.ceil(2 * WOBBLE_FRAC / 1) - 1;   // tiling + closure
    assert.ok(MAX_STACK >= perSheet * SHEETS.length,
        `MAX_STACK ${MAX_STACK} is below the derived worst case ${perSheet * SHEETS.length}`);
    for (const sh of SHEETS) {
        assert.ok(sh.alphaMul <= 1, "a sheet may dim itself, never brighten past the ceiling");
    }
});

test("an out-of-range intensity cannot brighten the field past the budget", () => {
    const env = makeEnv({ intensity: 12 });
    const probe = new Probe(env.width);
    descriptor.init(env);
    run(env, probe, 240, 1 / 60);
    assert.ok(probe.maxColumn <= PEAK_ALPHA + 1e-9,
        `intensity=12 leaked ${probe.maxColumn.toFixed(5)} past the budget`);
});

// -- premium rule (a): size over life -----------------------------------------

test("the size-over-life curve opens fast, closes slow, and is zero at both ends", () => {
    assert.equal(foldEnvelope(0), 0);
    assert.equal(foldEnvelope(1), 0);
    let max = 0, maxAt = 0;
    for (let i = 0; i <= 1000; i++) {
        const t = i / 1000, v = foldEnvelope(t);
        assert.ok(v >= 0 && v <= 1, `envelope out of range at ${t}: ${v}`);
        if (v > max) { max = v; maxAt = t; }
    }
    assert.ok(max > 0.999, `curve never reaches full size (peak ${max})`);
    assert.ok(maxAt < 0.4, `crest at ${maxAt} — the swell must be faster than the ebb`);
});

test("every strip carries its OWN envelope multiplier and drawn size is never constant", () => {
    const env = makeEnv();
    const probe = new Probe(env.width);
    descriptor.init(env);
    const env0 = env.state.strips.map(s => s.env0);
    assert.equal(new Set(env0).size, env0.length, "envelope multipliers must be per-strip, not shared");
    assert.ok(Math.max(...env0) / Math.min(...env0) > 1.2, "multipliers must actually spread");
    run(env, probe, 1000, 1 / 60);
    assert.ok(probe.maxH / probe.minH > 2,
        `drawn heights spanned only ${(probe.maxH / probe.minH).toFixed(2)}x — size is near-constant`);
});

// -- premium rule (b): colour ramp indexed by life -----------------------------

test("tint is indexed by life, and the two sheets occupy different ramp regions", () => {
    const env = makeEnv();
    descriptor.init(env);
    for (const s of env.state.strips) {
        const seen = new Set();
        for (let i = 0; i <= 100; i++) seen.add(tintIndex(s, i / 100));
        assert.ok(seen.size >= 2, "a strip must traverse the ramp as it folds, not sit on one colour");
        const lo = Math.min(...seen), hi = Math.max(...seen);
        if (s.sheet === 0) assert.ok(hi <= 2, `crimson sheet reached violet index ${hi}`);
        else assert.ok(lo >= 2, `violet sheet reached crimson index ${lo}`);
    }
});

test("the ramp is genuinely exercised on screen (>=3 tints blitted)", () => {
    const env = makeEnv();
    const probe = new Probe(env.width);
    descriptor.init(env);
    run(env, probe, 1000, 1 / 60);
    assert.ok(probe.tints.size >= 3,
        `only ${probe.tints.size} tints reached the canvas — that reads as a flat colour`);
    assert.equal(TINTS.length, 5);
    for (const c of TINTS) assert.equal(c.length, 3);
});

// -- the multi-octave curtain -------------------------------------------------

test("x motion is a 3-octave sum with distinct frequencies and a per-strip phase step", () => {
    for (const sh of SHEETS) {
        assert.equal(sh.oct.length, 3, "a single octave reads as a drifting blob, not a curtain");
        assert.equal(sh.hOct.length, 2, "height needs its own sum or the base line is flat");
        const freqs = sh.oct.map(o => o[0]);
        assert.equal(new Set(freqs).size, 3, "octaves must not share a frequency");
        for (const o of sh.oct) {
            assert.ok(o[1] > 0, "every octave must contribute amplitude");
            assert.ok(o[2] > 0, "phase step per strip is what makes the wave TRAVEL along x");
        }
        const sum = sh.oct.reduce((a, o) => a + o[1], 0);
        assert.ok(sum <= WOBBLE_FRAC + 1e-9,
            `${sh.key} x amplitude sums to ${sum} stripW; MAX_STACK assumes <= ${WOBBLE_FRAC}`);
    }
});

test("each strip's horizontal offset stays inside the bound MAX_STACK depends on", () => {
    const env = makeEnv();
    descriptor.init(env);
    for (const s of env.state.strips) {
        const amp = s.ax0 + s.ax1 + s.ax2;
        assert.ok(amp <= WOBBLE_FRAC * s.stripW + 1e-9,
            `strip amplitude ${amp} exceeds ${WOBBLE_FRAC} * stripW ${s.stripW}`);
    }
});

test("the curtain ripples ACROSS: neighbours are out of phase and the field moves", () => {
    const env = makeEnv();
    const probe = new Probe(env.width);
    descriptor.init(env);
    const xo = (s, t) => s.ax0 * Math.sin(t * s.f0 + s.p0)
        + s.ax1 * Math.sin(t * s.f1 + s.p1)
        + s.ax2 * Math.sin(t * s.f2 + s.p2);
    // Spatial: at one instant, adjacent slots are displaced differently — a rigid
    // slab translation (all offsets equal) is the blob failure mode.
    const sheet0 = env.state.strips.filter(s => s.sheet === 0);
    const atT = sheet0.map(s => xo(s, 3.7));
    assert.ok(new Set(atT.map(v => v.toFixed(4))).size > sheet0.length * 0.9,
        "strips are moving together — that is a slab, not a curtain");
    // Temporal: each strip oscillates about its slot in BOTH directions.
    for (const s of sheet0) {
        let lo = Infinity, hi = -Infinity;
        for (let i = 0; i < 2000; i++) { const v = xo(s, i * 0.05); if (v < lo) lo = v; if (v > hi) hi = v; }
        assert.ok(lo < 0 && hi > 0, "a strip must fold to both sides of its slot");
        assert.ok(hi - lo > s.stripW * 0.2, "fold travel is too small to read as motion");
    }
    // ...and the drawn field is genuinely not static.
    probe.frame(); descriptor.update(1 / 60, env); descriptor.draw(probe, env); const first = probe.xSum;
    run(env, probe, 900, 1 / 60);
    probe.frame(); descriptor.update(1 / 60, env); descriptor.draw(probe, env); const later = probe.xSum;
    assert.ok(Math.abs(later - first) > 1, "the field did not move over 900 frames");
});

// -- frame-rate independence ---------------------------------------------------

test("60 Hz and 120 Hz reach the same field after the same elapsed time", () => {
    const mk = () => { const e = makeEnv({ rand: mulberry32(0x51D) }); descriptor.init(e); return e; };
    const a = mk(), b = mk();
    for (let i = 0; i < 600; i++) descriptor.update(1 / 60, a);
    for (let i = 0; i < 1200; i++) descriptor.update(1 / 120, b);
    assert.ok(Math.abs(a.state.t - b.state.t) < 1e-6, `clock drift ${a.state.t - b.state.t}`);
    assert.equal(a.state.strips.length, b.state.strips.length);
    for (let i = 0; i < a.state.strips.length; i++) {
        const d = Math.abs(a.state.strips[i].life - b.state.strips[i].life);
        assert.ok(Math.min(d, 1 - d) < 1e-6, `strip ${i} life drifted by ${d} between refresh rates`);
    }
});

// -- hot path ------------------------------------------------------------------

test("update/draw allocate nothing and draw no randomness from the hot path", () => {
    let calls = 0;
    const env = makeEnv({ rand: () => { calls++; return mulberry32(0xC0FFEE + calls)(); } });
    descriptor.init(env);
    const afterInit = calls;
    const strips = env.state.strips, bands = env.state.bands;
    const probe = new Probe(env.width);
    run(env, probe, 1000, 1 / 60);
    assert.equal(calls, afterInit, "rand() in the frame loop makes the physics unreproducible");
    assert.equal(env.state.strips, strips, "the strip pool must be reused, never rebuilt per frame");
    assert.equal(env.state.strips.length, strips.length, "strips must not be pushed/popped per frame");
    assert.equal(env.state.bands, bands, "the gradient bake must survive the frame loop");
});

test("no NaN anywhere after 1000 frames", () => {
    const env = makeEnv();
    const probe = new Probe(env.width);
    descriptor.init(env);
    run(env, probe, 1000, 1 / 60);
    assert.equal(probe.bad, 0, "a non-finite value reached drawImage");
    assert.ok(Number.isFinite(env.state.t));
    for (const s of env.state.strips) {
        for (const k of Object.keys(s)) {
            assert.ok(Number.isFinite(s[k]), `strip.${k} went non-finite: ${s[k]}`);
        }
        assert.ok(s.life >= 0 && s.life < 1, `life escaped [0,1): ${s.life}`);
    }
});

test("a long stall does not throw life out of range", () => {
    const env = makeEnv();
    descriptor.init(env);
    descriptor.update(45, env);            // 45 s in one tick (tab restored)
    for (const s of env.state.strips) assert.ok(s.life >= 0 && s.life < 1, `life ${s.life} after stall`);
});

// -- lifecycle -----------------------------------------------------------------

test("resize keeps folds running instead of restarting the field", () => {
    // The composer's ResizeObserver fires on every keystroke that changes its
    // height. Rebuilding there would blink the whole curtain once a second.
    const env = makeEnv();
    descriptor.init(env);
    const probe = new Probe(env.width);
    run(env, probe, 120, 1 / 60);
    const before = env.state.strips.map(s => s.life);
    const bands = env.state.bands;
    env.height = 860;
    descriptor.resize(env);
    const kept = env.state.strips.filter((s, i) => s.life === before[i]).length;
    assert.ok(kept > before.length * 0.8, `resize restarted ${before.length - kept} strips`);
    assert.notEqual(env.state.bands, bands, "band height is viewport-derived — it must be rebaked");
    assert.equal(env.state.bands.length, TINTS.length);
});

test("resize across a count change stays inside the population bounds", () => {
    const env = makeEnv({ width: 360, height: 640 });
    descriptor.init(env);
    for (const [w, h] of [[3840, 2160], [900, 900], [412, 915], [2560, 1440]]) {
        env.width = w; env.height = h;
        descriptor.resize(env);
        const n = env.state.strips.length;
        assert.ok(n >= 24 && n <= 48, `${w}x${h} after resize gave ${n} strips`);
        assert.equal(n, env.state.counts[0] + env.state.counts[1]);
        const slots = env.state.strips.filter(s => s.sheet === 0).map(s => s.slot).sort((x, y) => x - y);
        assert.deepEqual(slots, slots.map((_, i) => i), "slots must stay a dense 0..n-1 range");
    }
    // ...and the budget still holds after all that churn.
    const probe = new Probe(env.width);
    run(env, probe, 300, 1 / 60);
    assert.ok(probe.maxColumn <= PEAK_ALPHA + 1e-9, `post-resize budget ${probe.maxColumn}`);
    assert.ok(probe.maxBottom <= env.height * BAND_FRAC + 1e-6, "post-resize clamp broke");
});

test("rearm keeps an ambient field running but rebuilds a wiped one", () => {
    const env = makeEnv();
    descriptor.init(env);
    descriptor.update(2, env);
    const lives = env.state.strips.map(s => s.life);
    descriptor.rearm(env);
    assert.deepEqual(env.state.strips.map(s => s.life), lives, "an aurora does not restart per turn");
    env.state = {};                                   // mode switch wipes the scratch space
    descriptor.rearm(env);
    assert.ok(env.state.strips && env.state.strips.length >= 24, "rearm must rebuild a wiped field");
});

test("update before init self-heals instead of throwing", () => {
    const env = makeEnv();
    const probe = new Probe(env.width);
    descriptor.update(1 / 60, env);
    descriptor.draw(probe, env);
    assert.ok(env.state.strips.length >= 24);
});

test("a zero-size viewport produces no NaN", () => {
    const env = makeEnv({ width: 0, height: 0 });
    const probe = new Probe(1);
    descriptor.init(env);
    run(env, probe, 60, 1 / 60);
    assert.equal(probe.bad, 0);
    for (const s of env.state.strips) assert.ok(Number.isFinite(s.slotX));
});

test("stripCount is clamped at both ends of the geometry range", () => {
    for (const sh of SHEETS) {
        assert.equal(stripCount(sh, 0.5), Math.max(sh.min, Math.min(sh.max, Math.round(sh.base * 0.9))));
        assert.ok(stripCount(sh, 0.1) >= sh.min);
        assert.ok(stripCount(sh, 9) <= sh.max);
    }
});
