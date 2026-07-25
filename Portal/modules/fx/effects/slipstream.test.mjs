// Slipstream physics + legibility tests.
//
// The effect takes its randomness from env.rand and its clock from env.now, so
// the whole field — including the DRAWN output, because slipstream strokes
// geometry instead of blitting a sprite atlas — is reproducible headlessly with
// a seeded generator and a fake Canvas2D.
//
// The assertions that earn their keep here are the two legibility caps (1.5 px,
// 0.30 alpha) measured from the strokes the effect actually issues, the
// no-streak-across-the-viewport bound, and the refresh-rate invariance of both
// the motion and the scatter-reset rate.
//
// Run: node --test Portal/modules/fx/effects/slipstream.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
    createEnv, getEffect, parseClearPolicy, applyClearPolicy, applyBlend, resetBlend,
} from "../registry.js";
import descriptor, { SLIPSTREAM, BUCKETS } from "./slipstream.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG (same one registry.test.mjs uses) so runs are repeatable. */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/**
 * Canvas2D recorder that measures what actually got painted: the widest stroke,
 * the brightest stroke alpha, the longest segment, and whether any coordinate
 * went non-finite. Scalars only — 1,000 frames x ~2,000 segments would be 2M
 * points if we kept them.
 */
function fakeCtx() {
    const rec = {
        maxWidth: 0, maxAlpha: 0, maxSeg: 0, segments: 0, strokes: 0,
        strokesThisFrame: 0, maxStrokesPerFrame: 0, nonFinite: 0, fills: 0,
    };
    let mx = 0, my = 0, lw = 1, ss = "";
    const ctx = {
        rec,
        globalAlpha: 1, fillStyle: null, lineCap: null, lineJoin: null,
        clearRect() {},
        fillRect() { rec.fills++; },
        beginPath() {},
        moveTo(x, y) {
            if (!Number.isFinite(x) || !Number.isFinite(y)) rec.nonFinite++;
            mx = x; my = y;
        },
        lineTo(x, y) {
            if (!Number.isFinite(x) || !Number.isFinite(y)) rec.nonFinite++;
            const d = Math.hypot(x - mx, y - my);
            if (d > rec.maxSeg) rec.maxSeg = d;
            rec.segments++;
        },
        stroke() { rec.strokes++; rec.strokesThisFrame++; },
    };
    Object.defineProperty(ctx, "lineWidth", {
        get: () => lw,
        set: (v) => { lw = v; if (v > rec.maxWidth) rec.maxWidth = v; },
    });
    Object.defineProperty(ctx, "strokeStyle", {
        get: () => ss,
        set: (v) => {
            ss = v;
            const a = parseFloat(String(v).split(",").pop());
            if (Number.isFinite(a) && a > rec.maxAlpha) rec.maxAlpha = a;
        },
    });
    Object.defineProperty(ctx, "globalCompositeOperation", { get: () => "source-over", set: () => {} });
    return ctx;
}

function testEnv(over = {}) {
    return createEnv({ width: 1400, height: 800, scale: 1, rand: seeded(42), ...over });
}

/** One engine frame, exactly as ember-fx.js's loop() drives it. */
function frame(ctx, env, dt) {
    env.now += dt * 1000;
    env.dt = dt;
    ctx.rec.strokesThisFrame = 0;
    descriptor.update(dt, env);
    applyClearPolicy(ctx, descriptor, env);
    applyBlend(ctx, descriptor);
    descriptor.draw(ctx, env);
    resetBlend(ctx);
    if (ctx.rec.strokesThisFrame > ctx.rec.maxStrokesPerFrame) {
        ctx.rec.maxStrokesPerFrame = ctx.rec.strokesThisFrame;
    }
}

function run(env, ctx, frames, dt) {
    for (let i = 0; i < frames; i++) frame(ctx, env, dt);
}

/** curl of the potential at a point — recomputed here so the test is independent
 *  of the module's internals (it must not be able to agree with itself by luck). */
function curlAt(x, y, t) {
    const e = SLIPSTREAM.eps;
    const p = (px, py) => Math.sin(px * 0.0065 + t * 0.22) * Math.cos(py * 0.0065 - t * 0.16)
        + 0.5 * Math.sin(px * 0.013 - t * 0.31) * Math.cos(py * 0.013 + t * 0.26);
    return { cx: p(x, y + e) - p(x, y - e), cy: -(p(x + e, y) - p(x - e, y)) };
}

/** Analytic ceiling on |velocity|: the lerp only ever chases a target, so the
 *  largest target the field can produce bounds every segment ever drawn. */
function speedBound(apparent) {
    const e = SLIPSTREAM.eps;
    const maxCurl = 2 * Math.sin(0.0065 * e) + Math.sin(0.013 * e);
    const g = Math.max(...SLIPSTREAM.kinds.map(k => k.gain));
    const bx = maxCurl * SLIPSTREAM.advect * apparent * g + Math.max(...SLIPSTREAM.kinds.map(k => Math.abs(k.dx)));
    const by = maxCurl * SLIPSTREAM.advect * apparent * g + Math.max(...SLIPSTREAM.kinds.map(k => Math.abs(k.dy)));
    return Math.hypot(bx, by);
}

// ---------------------------------------------------------------------------
// Contract
// ---------------------------------------------------------------------------

test("registers itself with an additive blend and a persistence smear", () => {
    const reg = getEffect("slipstream");
    assert.ok(reg, "slipstream must be in the registry after import");
    assert.equal(reg.label, "Slipstream");
    assert.equal(descriptor.blend, "lighter", "crossing threads must ADD light");
    const cp = parseClearPolicy(descriptor.clearPolicy);
    assert.equal(cp.kind, "fade", "the trail IS the effect — a hard clear leaves 2px dashes");
    const fadeAlpha = parseFloat(cp.color.split(",").pop());
    assert.ok(fadeAlpha > 0.05 && fadeAlpha < 0.25, `smear alpha out of range: ${fadeAlpha}`);
});

test("every quantised stroke width is inside the legibility cap by construction", () => {
    for (const w of SLIPSTREAM.widths) {
        assert.ok(w <= SLIPSTREAM.maxLinePx, `quantised width ${w} exceeds the cap`);
    }
    assert.ok(SLIPSTREAM.maxLinePx <= 1.5, "the budget is 1.5px — this is the whole point of the effect");
});

// ---------------------------------------------------------------------------
// Population
// ---------------------------------------------------------------------------

test("population is viewport-driven, clamped, and never reallocated", () => {
    const env = testEnv();
    descriptor.init(env);
    const st = env.state;
    const pool = st.pool, order = st.order;
    assert.equal(pool.length, SLIPSTREAM.maxThreads, "the pool is fixed-size");
    assert.ok(st.count >= SLIPSTREAM.minThreads && st.count <= SLIPSTREAM.maxThreads);

    const ctx = fakeCtx();
    run(env, ctx, 1000, 1 / 60);
    assert.equal(st.pool, pool, "update/draw must never replace the pool");
    assert.equal(st.order, order, "the sort scratch must never be reallocated");
    assert.equal(st.pool.length, SLIPSTREAM.maxThreads, "the pool must never grow");
    assert.ok(st.count <= SLIPSTREAM.maxThreads);

    // A denser viewport asks for more threads; both ends stay clamped.
    const dense = testEnv({ scale: 2.4 });
    descriptor.init(dense);
    const sparse = testEnv({ scale: 0.4 });
    descriptor.init(sparse);
    assert.ok(dense.state.count > sparse.state.count, "count must track env.scale");
    assert.ok(dense.state.count <= SLIPSTREAM.maxThreads);
    assert.ok(sparse.state.count >= SLIPSTREAM.minThreads);
});

test("three sub-populations are all present in a settled field", () => {
    const env = testEnv();
    descriptor.init(env);
    run(env, fakeCtx(), 120, 1 / 60);
    const gains = new Set();
    for (let i = 0; i < env.state.count; i++) gains.add(env.state.pool[i].gain);
    assert.equal(gains.size, SLIPSTREAM.kinds.length,
        `expected all ${SLIPSTREAM.kinds.length} kinds alive, saw gains ${[...gains]}`);
});

// ---------------------------------------------------------------------------
// Shading: the size-over-life curve and the colour ramp
// ---------------------------------------------------------------------------

// bin = (ci * alphaLevels + ai) * widths.length + wi
const W_N = SLIPSTREAM.widths.length;
const A_N = SLIPSTREAM.alphaLevels;
const decode = (bin) => ({
    wi: bin % W_N,
    ai: ((bin / W_N) | 0) % A_N,
    ci: (bin / (W_N * A_N)) | 0,
});

test("stroke width rides an arch over life, with a per-particle multiplier", () => {
    // Premium rule (a): size is never constant. A thread opens from nothing,
    // holds a plateau, and closes again — which is also what makes a scatter
    // reset invisible. Measured on a 4K box, where the quantised widths spread
    // across all three buckets instead of bottoming out.
    const env = testEnv({ width: 3840, height: 2160, scale: 2.4 });
    descriptor.init(env);
    run(env, fakeCtx(), 300, 1 / 60);
    const st = env.state;
    let peak = 0, peakN = 0, tail = 0, tailN = 0;
    const envelopes = new Set();
    for (let i = 0; i < st.count; i++) {
        const p = st.pool[i];
        envelopes.add(p.wEnv.toFixed(4));
        if (p.bin < 0) continue;
        const u = 1 - p.life, { wi } = decode(p.bin);
        if (u > 0.42 && u < 0.58) { peak += wi; peakN++; }
        else if (u < 0.12 || u > 0.88) { tail += wi; tailN++; }
    }
    assert.ok(peakN > 20 && tailN > 20, `not enough samples: peak=${peakN} tail=${tailN}`);
    const pm = peak / peakN, tm = tail / tailN;
    assert.ok(pm > tm * 1.25,
        `size does not track life: mid-life mean width bucket ${pm.toFixed(2)} vs tails ${tm.toFixed(2)}`);
    assert.ok(envelopes.size > 50,
        `the envelope multiplier is not per-particle (${envelopes.size} distinct values)`);
});

test("alpha opens and closes on the same arch, so a reset never pops in", () => {
    // This is what buys the 2.5%-per-frame churn: a thread re-seated somewhere
    // else is at u=0, where the envelope is zero, so it is not drawn at all
    // that frame and fades up from nothing instead of blinking into existence.
    const env = testEnv();
    descriptor.init(env);
    const ctx = fakeCtx();
    let peak = 0, peakN = 0, tail = 0, tailN = 0, born = 0;
    // Sampled every frame, not once at the end: only a handful of threads sit at
    // u===0 in any given frame, so a single-frame sample is a coin toss.
    for (let f = 0; f < 300; f++) {
        frame(ctx, env, 1 / 60);
        const st = env.state;
        for (let i = 0; i < st.count; i++) {
            const p = st.pool[i];
            const u = 1 - p.life;
            if (u === 0) { born++; assert.ok(p.bin < 0, `a just-reset thread was drawn (bin ${p.bin})`); }
            if (p.bin < 0 || f < 200) continue;              // stats from a settled field
            const { ai } = decode(p.bin);
            if (u > 0.42 && u < 0.58) { peak += ai; peakN++; }
            else if (u < 0.12 || u > 0.88) { tail += ai; tailN++; }
        }
    }
    assert.ok(born > 50, `sanity: only ${born} re-seated threads observed in 300 frames`);
    assert.ok(peakN > 20 && tailN > 20, `not enough samples: peak=${peakN} tail=${tailN}`);
    const pm = peak / peakN, tm = tail / tailN;
    assert.ok(pm > tm * 1.3,
        `alpha does not track life: mid-life mean bucket ${pm.toFixed(2)} vs tails ${tm.toFixed(2)}`);
});

test("colour is a ramp indexed by life, not a flat tint", () => {
    // Premium rule (b). Young threads sit at the cold end of the ramp, dying
    // ones at the dim end; a flat colour collapses the two means together.
    const env = testEnv();
    descriptor.init(env);
    run(env, fakeCtx(), 300, 1 / 60);
    const st = env.state;
    let young = 0, youngN = 0, old = 0, oldN = 0;
    const seen = new Set();
    for (let i = 0; i < st.count; i++) {
        const p = st.pool[i];
        if (p.bin < 0) continue;
        const u = 1 - p.life, { ci } = decode(p.bin);
        seen.add(ci);
        if (u < 0.25) { young += ci; youngN++; } else if (u > 0.7) { old += ci; oldN++; }
    }
    assert.ok(youngN > 20 && oldN > 20, `not enough samples: young=${youngN} old=${oldN}`);
    const ym = young / youngN, om = old / oldN;
    assert.ok(om > ym + 1.5, `colour does not track life: young ${ym.toFixed(2)} vs old ${om.toFixed(2)}`);
    assert.ok(seen.size >= 4, `only ${seen.size} ramp stops ever used`);
});

// ---------------------------------------------------------------------------
// THE legibility budget — measured from what was drawn, not from the config
// ---------------------------------------------------------------------------

test("a re-seated thread carries no tail from where it used to be", () => {
    // The segment IS (px,py)->(x,y), so a recycled particle that keeps its old
    // previous position strokes a bright bar across the viewport. The envelope
    // hides it for one frame, which is exactly why this needs asserting
    // directly rather than being left to the max-segment bound.
    const env = testEnv();
    descriptor.init(env);
    for (let i = 0; i < env.state.count; i++) {
        const p = env.state.pool[i];
        assert.equal(p.px, p.x, `init left a tail on pool[${i}]`);
        assert.equal(p.py, p.y);
    }
    run(env, fakeCtx(), 200, 1 / 60);
    let moving = 0;
    for (let i = 0; i < env.state.count; i++) if (env.state.pool[i].px !== env.state.pool[i].x) moving++;
    assert.ok(moving > env.state.count * 0.9, "sanity: a settled field should be in motion");
    descriptor.rearm(env);
    for (let i = 0; i < env.state.count; i++) {
        const p = env.state.pool[i];
        assert.equal(p.px, p.x, `rearm left a tail on pool[${i}]`);
        assert.equal(p.py, p.y);
    }
});

test("1000 frames never stroke wider than 1.5px or brighter than 0.30 alpha", () => {
    const env = testEnv();
    descriptor.init(env);
    const ctx = fakeCtx();
    run(env, ctx, 1000, 1 / 60);
    assert.ok(ctx.rec.segments > 100000, `field looks dead: only ${ctx.rec.segments} segments`);
    assert.ok(ctx.rec.maxWidth <= SLIPSTREAM.maxLinePx,
        `stroke width ${ctx.rec.maxWidth} busts the 1.5px budget`);
    assert.ok(ctx.rec.maxAlpha <= SLIPSTREAM.maxAlpha,
        `stroke alpha ${ctx.rec.maxAlpha} busts the 0.30 budget`);
});

test("a 4K viewport gets MORE threads, never fatter ones", () => {
    // apparentScale() clamps at 2.0, so an unclamped width would be ~2.7px here.
    const env = testEnv({ width: 3840, height: 2160, scale: 2.4 });
    descriptor.init(env);
    const ctx = fakeCtx();
    run(env, ctx, 400, 1 / 60);
    assert.ok(ctx.rec.maxWidth <= SLIPSTREAM.maxLinePx,
        `geometry scale leaked past the cap: ${ctx.rec.maxWidth}px`);
    assert.equal(env.state.count, SLIPSTREAM.maxThreads);
});

test("geometry ignores devicePixelRatio — the shipped bug must not come back", () => {
    // sizing.js exists because particle geometry once tracked dpr and the whole
    // field rendered dpr x magnified. Same seed, same CSS box, dpr 1 vs 3: the
    // simulation and every stroke must be bit-identical.
    const replay = (dpr) => {
        const env = testEnv({ dpr });
        descriptor.init(env);
        const ctx = fakeCtx();
        run(env, ctx, 200, 1 / 60);
        let sum = 0;
        for (let i = 0; i < env.state.count; i++) sum += env.state.pool[i].x + env.state.pool[i].y;
        return { sum, apparent: env.state.apparent, w: ctx.rec.maxWidth, seg: ctx.rec.maxSeg, n: ctx.rec.segments };
    };
    assert.deepEqual(replay(3), replay(1), "devicePixelRatio leaked into the geometry");
});

test("no thread ever strokes a streak across the viewport", () => {
    // A recycled particle whose previous position was left stale draws a ~1000px
    // line — a bright bar straight through the chat text. The bound is analytic:
    // the largest velocity the field can produce, times the frame time.
    const env = testEnv();
    descriptor.init(env);
    const ctx = fakeCtx();
    const dt = 1 / 60;
    run(env, ctx, 1000, dt);
    const limit = speedBound(env.state.apparent) * dt * 1.02;
    assert.ok(ctx.rec.maxSeg <= limit,
        `longest segment ${ctx.rec.maxSeg.toFixed(1)}px exceeds the ${limit.toFixed(1)}px bound`);
    assert.ok(limit < 30, `the bound itself is too loose to be meaningful: ${limit}`);
});

test("draw batches: at most one state change per bucket, whatever the population", () => {
    const env = testEnv({ scale: 2.4 });
    descriptor.init(env);
    const ctx = fakeCtx();
    run(env, ctx, 200, 1 / 60);
    assert.ok(ctx.rec.maxStrokesPerFrame <= BUCKETS,
        `${ctx.rec.maxStrokesPerFrame} strokes in one frame — batching is broken`);
    assert.ok(ctx.rec.maxStrokesPerFrame > 1, "buckets should actually be in use");
});

// ---------------------------------------------------------------------------
// Motion
// ---------------------------------------------------------------------------

test("threads are advected BY the curl field, not drifting through it", () => {
    const env = testEnv();
    descriptor.init(env);
    run(env, fakeCtx(), 120, 1 / 60);
    const st = env.state, ts = env.now * 0.001;
    const adv = SLIPSTREAM.advect * st.apparent;
    let sum = 0;
    for (let i = 0; i < st.count; i++) {
        const p = st.pool[i];
        const { cx, cy } = curlAt(p.x, p.y, ts);
        const tx = cx * adv * p.gain + p.dx, ty = cy * adv * p.gain + p.dy;
        const dot = p.vx * tx + p.vy * ty;
        const mag = Math.hypot(p.vx, p.vy) * Math.hypot(tx, ty);
        if (mag > 0) sum += dot / mag;
    }
    const mean = sum / st.count;
    assert.ok(mean > 0.8, `mean cosine to the local field is only ${mean.toFixed(3)}`);
});

test("motion is delta-timed: 120Hz moves the same distance per SECOND as 60Hz", () => {
    const measure = (dt, frames) => {
        const env = testEnv();
        descriptor.init(env);
        const ctx = fakeCtx();
        let travelled = 0, samples = 0;
        for (let i = 0; i < frames; i++) {
            frame(ctx, env, dt);
            const st = env.state;
            for (let j = 0; j < st.count; j += 7) {          // stride: a fair sample
                const p = st.pool[j];
                travelled += Math.hypot(p.x - p.px, p.y - p.py);
                samples++;
            }
        }
        return travelled / samples / dt;                      // px per second
    };
    const at60 = measure(1 / 60, 600);
    const at120 = measure(1 / 120, 1200);
    const drift = Math.abs(at120 - at60) / at60;
    assert.ok(drift < 0.12,
        `speed is frame-rate dependent: ${at60.toFixed(1)} px/s at 60Hz vs ${at120.toFixed(1)} at 120Hz`);
});

test("the scatter-reset churns 2-3% of the field per 60Hz frame — as a RATE", () => {
    // Without this the field walks onto the potential's fixed attractors and
    // freezes. Naive ports hard-code "2.5% per frame", which doubles on a 120Hz
    // display; the per-second rate must be identical on both.
    const churn = (dt, frames) => {
        const env = testEnv();
        descriptor.init(env);
        run(env, fakeCtx(), frames, dt);
        return env.state.resets / (frames * dt) / env.state.count;   // fraction/sec
    };
    const r60 = churn(1 / 60, 600);
    const r120 = churn(1 / 120, 1200);
    assert.ok(r60 / 60 > 0.02 && r60 / 60 < 0.03,
        `per-60Hz-frame churn is ${(r60 / 60 * 100).toFixed(2)}%, want 2-3%`);
    assert.ok(Math.abs(r120 - r60) / r60 < 0.05,
        `churn is frame-rate dependent: ${r60.toFixed(2)}/s at 60Hz vs ${r120.toFixed(2)}/s at 120Hz`);
});

test("the field stays spread out after 20 simulated seconds (it does not go static)", () => {
    const env = testEnv();
    descriptor.init(env);
    run(env, fakeCtx(), 1200, 1 / 60);
    const st = env.state;
    let mx = 0, my = 0;
    for (let i = 0; i < st.count; i++) { mx += st.pool[i].x; my += st.pool[i].y; }
    mx /= st.count; my /= st.count;
    let vx = 0, vy = 0;
    for (let i = 0; i < st.count; i++) {
        vx += (st.pool[i].x - mx) ** 2; vy += (st.pool[i].y - my) ** 2;
    }
    const sdx = Math.sqrt(vx / st.count), sdy = Math.sqrt(vy / st.count);
    // Uniform over 1400x800 would be ~404 / ~231. Collapsed onto attractors is
    // a small fraction of that.
    assert.ok(sdx > 300, `x collapsed onto attractors: sd=${sdx.toFixed(0)}`);
    assert.ok(sdy > 170, `y collapsed onto attractors: sd=${sdy.toFixed(0)}`);
});

// ---------------------------------------------------------------------------
// Robustness
// ---------------------------------------------------------------------------

test("nothing goes NaN over 1000 frames, including a post-stall 50ms frame", () => {
    const env = testEnv();
    descriptor.init(env);
    const ctx = fakeCtx();
    for (let i = 0; i < 1000; i++) frame(ctx, env, i % 97 === 0 ? 0.05 : 1 / 60);
    const st = env.state;
    for (let i = 0; i < st.count; i++) {
        const p = st.pool[i];
        for (const key of ["x", "y", "px", "py", "vx", "vy", "life"]) {
            assert.ok(Number.isFinite(p[key]), `pool[${i}].${key} is ${p[key]}`);
        }
        assert.ok(p.life > 0 && p.life <= 1, `pool[${i}].life out of range: ${p.life}`);
        assert.ok(p.bin >= -1 && p.bin < BUCKETS, `pool[${i}].bin out of range: ${p.bin}`);
    }
    assert.equal(ctx.rec.nonFinite, 0, "a non-finite coordinate reached the canvas");
});

test("resize and rearm re-seat the field without reallocating or leaving strays", () => {
    const env = testEnv();
    descriptor.init(env);
    const pool = env.state.pool;
    run(env, fakeCtx(), 200, 1 / 60);

    env.width = 500; env.height = 420; env.scale = 0.5;
    descriptor.resize(env);
    assert.equal(env.state.pool, pool, "resize must not reallocate the pool");
    const m = SLIPSTREAM.margin;
    for (let i = 0; i < env.state.count; i++) {
        const p = env.state.pool[i];
        assert.ok(p.x <= env.width + m && p.y <= env.height + m,
            `stray thread left outside the new box at ${p.x.toFixed(0)},${p.y.toFixed(0)}`);
    }

    descriptor.rearm(env);
    assert.equal(env.state.pool, pool, "rearm must not reallocate the pool");
    // Staggered lives: a rearm that seeds every thread at life=1 makes the whole
    // field pulse in unison.
    const lives = new Set();
    for (let i = 0; i < env.state.count; i++) lives.add(env.state.pool[i].life.toFixed(2));
    assert.ok(lives.size > 20, `rearm did not stagger the life curve (${lives.size} distinct)`);
    run(env, fakeCtx(), 200, 1 / 60);
});

test("update survives being called before init (engine cold start)", () => {
    const env = testEnv();
    const ctx = fakeCtx();
    frame(ctx, env, 1 / 60);
    assert.ok(env.state.pool, "update must self-initialise");
    assert.ok(env.state.count > 0);
});
