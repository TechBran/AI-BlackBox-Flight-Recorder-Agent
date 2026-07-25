// Drift Snow physics — headless. The whole sim takes its randomness from
// env.rand and its clock from env.now, so a seeded generator + a fake ctx drives
// it deterministically with no DOM and no rAF.
//
// What is actually worth asserting here (everything else is decoration):
//   - the LEGIBILITY BUDGET: source-over, and never more than MAX_FLAKES flakes,
//     at any viewport density. This is a backdrop behind chat text.
//   - the DEPTH CORRELATION: size, speed and gust response rise together across
//     the three planes. Break it and the parallax collapses to a flat field.
//   - gusts are COHERENT (one number moves the whole population) and SETTLE.
//   - motion is delta-timed: 120 Hz and 60 Hz land in the same place.
//   - geometry is independent of devicePixelRatio (the shipped bug).
//   - no NaN after 1000 frames.
//
// Run: node --test Portal/modules/fx/effects/snow.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, getEffect, applyClearPolicy, applyBlend, resetBlend } from "../registry.js";
import descriptor, { MAX_FLAKES, RAMP_LUT, PLANES, sizeCurve, planeCounts } from "./snow.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG — same one the registry tests use. */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Canvas2D recorder that tracks every globalCompositeOperation WRITE. */
function fakeCtx() {
    const calls = [], blendWrites = [];
    const ctx = {
        calls, blendWrites, globalAlpha: 1, fillStyle: null,
        clearRect: (...a) => calls.push(["clearRect", ...a]),
        fillRect: (...a) => calls.push(["fillRect", ...a]),
        beginPath: () => calls.push(["beginPath"]),
        arc: (...a) => calls.push(["arc", ...a]),
        fill: () => calls.push(["fill"]),
        createRadialGradient: () => { throw new Error("per-frame gradient allocation"); },
    };
    let gco = "source-over";
    Object.defineProperty(ctx, "globalCompositeOperation", { get: () => gco, set: v => { gco = v; blendWrites.push(v); } });
    return ctx;
}

function makeEnv(over = {}) {
    return createEnv({ width: 1400, height: 800, scale: 1, rand: seeded(42), ...over });
}

/** Build a live field. */
function field(over = {}) {
    const env = makeEnv(over);
    descriptor.init(env);
    return env;
}

/** Advance the sim n frames of dt seconds, driving env.now like the engine does. */
function run(env, n, dt = 1 / 60) {
    for (let i = 0; i < n; i++) {
        env.now += dt * 1000;
        env.dt = dt;
        descriptor.update(dt, env);
    }
    return env;
}

const flakes = env => env.state.planes.flatMap(p => p.list);
const mean = xs => xs.reduce((a, b) => a + b, 0) / xs.length;

// ---------------------------------------------------------------------------
// 1. Legibility budget — the non-negotiable part
// ---------------------------------------------------------------------------

test("blend is source-over, NOT additive — white flakes must not blow out the text", () => {
    assert.equal(descriptor.blend, "source-over");
    assert.notEqual(descriptor.blend, "lighter");
    assert.equal(descriptor.clearPolicy, "clear", "a fade would smear flakes into streaks and grey the backdrop");
    assert.equal(getEffect("snow").id, "snow", "module registers itself on import");
});

test("population NEVER exceeds the flake budget, at any viewport density", () => {
    for (const scale of [0.1, 0.4, 1, 1.6, 2.4, 6, 100, 1000]) {
        const counts = planeCounts(scale);
        const total = counts.reduce((a, b) => a + b, 0);
        assert.ok(total <= MAX_FLAKES, `scale ${scale} asked for ${total} flakes (budget ${MAX_FLAKES})`);
        assert.ok(counts.every(c => c >= 1), `scale ${scale} emptied a depth plane: ${counts}`);
    }
    // …and the live field honours it too, not just the planner.
    const env = field({ scale: 2.4 });
    assert.ok(flakes(env).length <= MAX_FLAKES, `live field held ${flakes(env).length}`);
    assert.equal(flakes(field()).length, 244, "the shipped population at scale 1");
});

test("a garbage scale falls back to 1 instead of emptying or flooding the field", () => {
    for (const bad of [undefined, NaN, 0, -3]) {
        const total = planeCounts(bad).reduce((a, b) => a + b, 0);
        assert.equal(total, 244);
    }
});

// ---------------------------------------------------------------------------
// 2. Three sub-populations, correlated into depth
// ---------------------------------------------------------------------------

test("three depth planes, each populated", () => {
    const env = field();
    assert.equal(env.state.planes.length, 3);
    assert.equal(PLANES.length, 3);
    env.state.planes.forEach((p, i) => assert.ok(p.list.length > 0, `plane ${i} is empty`));
});

test("size, fall speed, sway and gust response all rise together far -> near", () => {
    const env = field();
    const [far, mid, near] = env.state.planes.map(p => ({
        r: mean(p.list.map(f => f.r)),
        vy: mean(p.list.map(f => f.vy)),
        sway: mean(p.list.map(f => f.swayAmp)),
        gust: p.cfg.gust,
        soft: p.cfg.soft,
    }));
    for (const key of ["r", "vy", "sway", "gust", "soft"]) {
        assert.ok(far[key] < mid[key] && mid[key] < near[key],
            `${key} is not monotonic across the planes (${far[key]}, ${mid[key]}, ${near[key]}) — the depth illusion IS this correlation`);
    }
});

// ---------------------------------------------------------------------------
// 3. Motion: down, fluttering, gusting coherently
// ---------------------------------------------------------------------------

test("every flake falls DOWNWARD (vy inverted from the rising star field)", () => {
    const env = field();
    assert.ok(flakes(env).every(f => f.vy > 0), "a flake is rising");
    // Frame by frame: y may only decrease by RECYCLING, which lands the flake
    // above the top edge. Anything else is a flake drifting upward.
    let advanced = 0;
    for (let i = 0; i < 30; i++) {
        const before = flakes(env).map(f => f.y);
        run(env, 1);
        flakes(env).forEach((f, k) => {
            if (f.y > before[k]) { advanced++; return; }
            assert.ok(f.y < 0, `flake ${k} moved up without recycling (${before[k]} -> ${f.y})`);
        });
    }
    assert.ok(advanced > 7000, `too few downward steps to judge (${advanced})`);
});

test("flutter oscillates: a flake's horizontal drift reverses repeatedly", () => {
    const env = field();
    env.state.gustWait = 1e9;                       // isolate flutter from the gust
    const f = env.state.planes[2].list[0];
    let last = f.x, signs = 0, prev = 0;
    for (let i = 0; i < 480; i++) {
        run(env, 1);
        const s = Math.sign(f.x - last);
        if (s !== 0 && prev !== 0 && s !== prev) signs++;
        if (s !== 0) prev = s;
        last = f.x;
    }
    assert.ok(signs >= 2, `flutter never reversed (${signs} reversals) — that is a rail, not a sway`);
    assert.equal(env.state.gust, 0, "no gust should have fired in this window");
});

test("a gust pushes the WHOLE field the same way, then settles back to calm", () => {
    const env = field();
    let peak = 0, peakDx = null;
    for (let i = 0; i < 3600; i++) {                // 60 s: several gust cycles
        const before = flakes(env).map(f => f.x);
        run(env, 1);
        const g = env.state.gust;
        if (Math.abs(g) > Math.abs(peak)) {
            peak = g;
            const after = flakes(env).map(f => f.x);
            peakDx = mean(after.map((x, k) => x - before[k]).filter(d => Math.abs(d) < 100));
        }
    }
    assert.ok(Math.abs(peak) > 15, `no real gust in 60 s (peak ${peak})`);
    assert.ok(Math.sign(peakDx) === Math.sign(peak),
        `the field drifted against the gust (gust ${peak}, mean dx ${peakDx}) — coherence is the whole point`);
    // Let it settle: no new gust can start for at least GUST.wait[0] seconds.
    env.state.gustWait = 1e9; env.state.gustHold = 0; env.state.gustTarget = 0;
    run(env, 300);
    assert.ok(Math.abs(env.state.gust) < 1, `gust never settled (${env.state.gust})`);
});

// ---------------------------------------------------------------------------
// 4. The two premium curves
// ---------------------------------------------------------------------------

test("size-over-life is a curve, not a constant, and closes at both ends", () => {
    assert.equal(sizeCurve(0), 0);
    assert.equal(sizeCurve(1), 0);
    assert.equal(sizeCurve(0.5), 1, "full size through the middle of the fall");
    for (let l = 0; l < 0.18; l += 0.01) assert.ok(sizeCurve(l) < sizeCurve(l + 0.01), "swell-in must be monotonic");
    for (let l = 0.79; l < 0.99; l += 0.01) assert.ok(sizeCurve(l) > sizeCurve(l + 0.01), "taper-out must be monotonic");
    assert.ok(sizeCurve(-5) === 0 && sizeCurve(9) === 0, "clamped outside 0..1");
});

test("each flake carries its own envelope multiplier — the curve is shared, the amplitude is not", () => {
    const env = field();
    const envs = new Set(flakes(env).map(f => f.sizeEnv.toFixed(6)));
    assert.ok(envs.size > 200, `only ${envs.size} distinct envelopes across ${flakes(env).length} flakes`);
    assert.ok(flakes(env).every(f => f.sizeEnv >= 0.78 && f.sizeEnv <= 1.28));
});

test("colour is a life-indexed ramp, not a flat white", () => {
    assert.equal(RAMP_LUT.length, 48);
    assert.ok(RAMP_LUT.every(c => /^rgb\(\d{1,3},\d{1,3},\d{1,3}\)$/.test(c)), "LUT holds pre-baked css colours (zero per-frame string allocation)");
    assert.equal(new Set(RAMP_LUT).size > 20, true, "ramp is not flat");
    assert.notEqual(RAMP_LUT[0], RAMP_LUT[24]);
    assert.notEqual(RAMP_LUT[24], RAMP_LUT[47]);
    // Per-flake bias de-banks the ramp so flakes at the same height differ.
    const biases = new Set(flakes(field()).map(f => f.bias.toFixed(6)));
    assert.ok(biases.size > 200, `only ${biases.size} distinct colour biases`);
});

// ---------------------------------------------------------------------------
// 5. Frame-rate, dpr and numerical safety
// ---------------------------------------------------------------------------

test("motion is delta-timed: 120 Hz lands where 60 Hz lands", () => {
    // A FRESH generator per field — sharing one instance would desync the two
    // initial scatters and this test would be measuring nothing.
    const tall = () => ({ width: 1400, height: 4000, rand: seeded(42) });
    const a = run(field(tall()), 60, 1 / 60);
    const b = run(field(tall()), 120, 1 / 120);
    const fa = flakes(a), fb = flakes(b);
    let compared = 0;
    for (let i = 0; i < fa.length; i++) {
        // Skip anything that recycled: respawning draws from env.rand, so the two
        // runs legitimately diverge for that flake. Everything still falling must
        // be in the same place — same elapsed time, same distance.
        if (fa[i].y < 0 || fb[i].y < 0) continue;
        assert.ok(Math.abs(fa[i].y - fb[i].y) < 0.5, `flake ${i}: ${fa[i].y} vs ${fb[i].y}`);
        assert.ok(Math.abs(fa[i].x - fb[i].x) < 1.0, `flake ${i} x: ${fa[i].x} vs ${fb[i].x}`);
        compared++;
    }
    assert.ok(compared > 200, `only compared ${compared} flakes`);
});

test("geometry is INDEPENDENT of devicePixelRatio — the shipped bug", () => {
    const one = run(field({ dpr: 1, rand: seeded(7) }), 100);
    const four = run(field({ dpr: 4, rand: seeded(7) }), 100);
    const a = flakes(one), b = flakes(four);
    assert.equal(a.length, b.length);
    for (let i = 0; i < a.length; i++) {
        assert.equal(a[i].r, b[i].r, `flake ${i} radius moved with dpr`);
        assert.equal(a[i].y, b[i].y, `flake ${i} position moved with dpr`);
    }
});

test("1000 frames of mixed frame times produce no NaN and nothing escapes the field", () => {
    const env = field();
    const dts = [1 / 60, 1 / 120, 1 / 30, 0.05, 1 / 144];
    for (let i = 0; i < 1000; i++) run(env, 1, dts[i % dts.length]);
    const m = env.state.margin, w = env.width, h = env.height;
    for (const f of flakes(env)) {
        for (const k of ["x", "y", "r", "vy", "life", "swayAmp", "phase", "sizeEnv"]) {
            assert.ok(Number.isFinite(f[k]), `${k} is ${f[k]}`);
        }
        assert.ok(f.life >= 0 && f.life <= 1, `life out of range: ${f.life}`);
        assert.ok(f.x >= -m - 1 && f.x <= w + m + 1, `x escaped: ${f.x}`);
        assert.ok(f.y >= -m - h * 0.2 - 1 && f.y <= h + m + 1, `y escaped: ${f.y}`);
    }
    assert.ok(Number.isFinite(env.state.gust));
});

test("identical seeds replay identically — no Math.random anywhere in the sim", () => {
    const a = flakes(run(field({ rand: seeded(5) }), 200)).map(f => `${f.x.toFixed(4)},${f.y.toFixed(4)}`).join("|");
    const b = flakes(run(field({ rand: seeded(5) }), 200)).map(f => `${f.x.toFixed(4)},${f.y.toFixed(4)}`).join("|");
    assert.equal(a, b);
});

// ---------------------------------------------------------------------------
// 6. Draw contract
// ---------------------------------------------------------------------------

test("draw never sets its own blend and never allocates a gradient", () => {
    const env = field();
    run(env, 120);
    const ctx = fakeCtx();
    applyClearPolicy(ctx, descriptor, env);
    applyBlend(ctx, descriptor);
    const before = ctx.blendWrites.length;
    descriptor.draw(ctx, env);                       // throws if it reaches for createRadialGradient
    assert.equal(ctx.blendWrites.length - before, 0, "the effect set globalCompositeOperation itself");
    resetBlend(ctx);
    const arcs = ctx.calls.filter(c => c[0] === "arc");
    assert.ok(arcs.length > 200, `only ${arcs.length} flake arcs drawn`);
    assert.ok(arcs.every(c => Number.isFinite(c[3]) && c[3] > 0), "an arc had a non-finite or zero radius");
});

test("draw is a no-op before init and needs no sprite atlas", () => {
    const bare = makeEnv();
    assert.doesNotThrow(() => descriptor.draw(fakeCtx(), bare));
    assert.equal(bare.sprites, null, "snow draws from arcs, so a missing atlas is not a blank field");
});

test("resize keeps the field rather than re-scattering it on every composer keystroke", () => {
    const env = field();
    run(env, 60);
    const before = flakes(env).map(f => f.y);
    env.height = 760;                                 // composer grew by one line
    descriptor.resize(env);
    const after = flakes(env).map(f => f.y);
    assert.equal(after.length, before.length, "population changed on a trivial resize");
    for (let i = 0; i < before.length; i++) assert.equal(after[i], before[i], "flakes teleported on resize");
    // A narrower viewport must pull stranded flakes back inside.
    env.width = 500;
    descriptor.resize(env);
    assert.ok(flakes(env).every(f => f.x <= 500 + env.state.margin), "a flake was stranded off the right edge");
});

test("rearm re-arms the gust clock instead of firing a gust the instant a hidden tab returns", () => {
    const env = field();
    env.state.gustWait = 0.01;
    descriptor.rearm(env);
    assert.ok(env.state.gustWait >= 4.5, `gust was due immediately (${env.state.gustWait})`);
    assert.equal(env.state.gust, 0);
});
