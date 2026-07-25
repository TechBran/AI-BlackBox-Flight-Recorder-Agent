// Bokeh Depth physics — driven headlessly with a SEEDED rand and a fake clock,
// because every number that matters here (how big, how bright, how fast, how
// many) is a function of env, never of a global.
//
// What these tests actually protect:
//   1. the PARALLAX correlation — size/alpha/speed/sprite must move together
//      across the four planes, or the screen flattens into confetti
//   2. the LEGIBILITY BUDGET — near discs are enormous, so alpha is the whole
//      budget; asserted against the real --muted body-text colour, not a vibe
//   3. delta-timing — identical motion at 60 and 120 Hz
//   4. pooling — the same particle objects for the life of the field
//
// Run: node --test Portal/modules/fx/effects/bokeh.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, getEffect } from "../registry.js";
import descriptor, { PLANES, BOKEH_RAMP, LEGIBILITY, alphaOf, radiusOf, rampIndexOf } from "./bokeh.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG (same one registry.test.mjs uses) so runs are reproducible. */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

function fakeCtx() {
    const calls = [];
    const blendWrites = [];
    const ctx = {
        calls, blendWrites, globalAlpha: 1,
        drawImage: (...a) => calls.push(["drawImage", ...a.slice(1)]),
    };
    let gco = "source-over";
    Object.defineProperty(ctx, "globalCompositeOperation", {
        get: () => gco, set: (v) => { gco = v; blendWrites.push(v); },
    });
    return ctx;
}

function envFor(w = 1400, h = 800, scale = 0.86, seed = 42) {
    const env = createEnv({ width: w, height: h, scale, rand: seeded(seed), now: 0 });
    descriptor.init(env);
    return env;
}

/** One engine frame. The clock is advanced by the SAME dt the physics is given. */
function frame(env, dt) {
    env.dt = dt;
    env.now += dt * 1000;
    descriptor.update(dt, env);
}

function run(env, frames, dt) { for (let i = 0; i < frames; i++) frame(env, dt); }

const planeOf = (env, id) => env.state.list.filter(p => p.plane.id === id);
const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;

// --muted in Portal/styles/_variables.css. The near discs are drawn additively
// over #000, so a disc's composite is alpha x its ramp colour.
const MUTED = [201, 201, 201];
function luminance(r, g, b) {
    const f = (v) => { const c = v / 255; return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
const MUTED_L = luminance(MUTED[0], MUTED[1], MUTED[2]);

// ---------------------------------------------------------------------------
// 1. Registration + population
// ---------------------------------------------------------------------------

test("registers with the additive blend and a HARD clear (no smear on slow discs)", () => {
    // registerEffect MERGES into its own stored entry, so the registry's copy is
    // not reference-equal to the exported descriptor — assert the contract, not
    // the identity.
    const d = getEffect("bokeh");
    assert.ok(d, "bokeh must register itself on import");
    assert.equal(d.update, descriptor.update);
    assert.equal(d.blend, "lighter");
    assert.equal(d.clearPolicy, "clear");
    assert.equal(d.label, "Bokeh Depth");
});

test("four planes, each populated, counts scaled by env.scale", () => {
    assert.equal(PLANES.length, 4, "the depth IS the four planes");
    for (const scale of [0.4, 1, 2.4]) {
        const env = envFor(1400, 800, scale);
        const want = PLANES.map(pl => Math.max(1, Math.round(pl.count * scale)));
        assert.equal(env.state.list.length, want.reduce((a, b) => a + b, 0), `scale ${scale}`);
        PLANES.forEach((pl, i) => assert.equal(planeOf(env, pl.id).length, want[i], `${pl.id} @ ${scale}`));
    }
});

test("two sprite forms: the near planes are annuli, the far planes solid blobs", () => {
    const donut = PLANES.filter(p => p.donut).map(p => p.id);
    const solid = PLANES.filter(p => !p.donut).map(p => p.id);
    assert.deepEqual(donut, ["inner", "near"], "the BIG discs are the defocused ones");
    assert.deepEqual(solid, ["dust", "grain"]);
});

// ---------------------------------------------------------------------------
// 2. Parallax correlation — the whole effect
// ---------------------------------------------------------------------------

test("size, alpha and speed are CORRELATED across the planes, far -> near", () => {
    // Declared far-first. Near = bigger, dimmer, slower, longer-lived.
    for (let i = 1; i < PLANES.length; i++) {
        const far = PLANES[i - 1], near = PLANES[i];
        assert.ok(near.r[0] > far.r[1], `${near.id} must be strictly bigger than ${far.id}`);
        assert.ok(near.alpha < far.alpha, `${near.id} must be dimmer than ${far.id}`);
        assert.ok(near.speed[1] < far.speed[0], `${near.id} must be strictly slower than ${far.id}`);
        // Lifetimes overlap between neighbouring planes on purpose (nothing should
        // die on a schedule), so this one is a mean, not a strict separation.
        assert.ok(near.life[0] + near.life[1] > far.life[0] + far.life[1],
            `${near.id} must outlive ${far.id} on average`);
    }
});

test("the LIVE field reproduces that correlation (not just the config)", () => {
    const env = envFor();
    run(env, 120, 1 / 60);
    const geo = env.state.geo;
    for (let i = 1; i < PLANES.length; i++) {
        const far = planeOf(env, PLANES[i - 1].id), near = planeOf(env, PLANES[i].id);
        assert.ok(mean(near.map(p => radiusOf(p, geo))) > mean(far.map(p => radiusOf(p, geo))),
            `${PLANES[i].id} discs must render bigger than ${PLANES[i - 1].id}`);
        assert.ok(mean(near.map(p => Math.abs(p.vx))) < mean(far.map(p => Math.abs(p.vx))),
            `${PLANES[i].id} must creep slower than ${PLANES[i - 1].id}`);
    }
});

test("every particle drifts LEFTWARD and keeps drifting", () => {
    const env = envFor();
    assert.ok(env.state.list.every(p => p.vx < 0), "a plane with mixed drift directions has no parallax");
    const before = env.state.list.map(p => ({ p, x: p.x, decay: p.decay }));
    run(env, 30, 1 / 60);
    const moved = before.filter(s => s.p.decay === s.decay);   // skip anything that recycled
    assert.ok(moved.length > 40, `too few survivors to judge: ${moved.length}`);
    for (const s of moved) assert.ok(s.p.x < s.x, `${s.p.plane.id} did not move left`);
});

// ---------------------------------------------------------------------------
// 3. Delta timing
// ---------------------------------------------------------------------------

test("120 Hz and 60 Hz produce the SAME motion over the same wall-clock second", () => {
    const a = envFor(1400, 800, 0.86, 7), b = envFor(1400, 800, 0.86, 7);
    const snap = a.state.list.map(p => ({ decay: p.decay }));
    run(a, 30, 1 / 60);
    run(b, 60, 1 / 120);
    let compared = 0;
    for (let i = 0; i < a.state.list.length; i++) {
        const pa = a.state.list[i], pb = b.state.list[i];
        if (pa.decay !== snap[i].decay || pb.decay !== snap[i].decay) continue;   // recycled in one run
        assert.ok(Math.abs(pa.x - pb.x) < 1e-6, `${pa.plane.id} x drift: ${pa.x} vs ${pb.x}`);
        assert.ok(Math.abs(pa.y - pb.y) < 0.5, `${pa.plane.id} y drift: ${pa.y} vs ${pb.y}`);
        assert.ok(Math.abs(pa.life - pb.life) < 1e-9);
        compared++;
    }
    assert.ok(compared > 40, `too few comparable particles: ${compared}`);
});

test("a backgrounded-tab stall is clamped, not integrated", () => {
    const env = envFor();
    const p = planeOf(env, "near")[0];
    const x0 = p.x, life0 = p.life;
    frame(env, 4);                                    // 4 seconds in one frame
    assert.ok(Math.abs(p.x - x0) < Math.abs(p.vx) * 0.11, "a 4 s jump must not teleport the field");
    assert.ok(life0 - p.life <= p.decay * 0.1 + 1e-9, "life must be clamped with the motion");
});

// ---------------------------------------------------------------------------
// 4. The three premium rules
// ---------------------------------------------------------------------------

test("(a) size follows a curve over life, with a per-particle envelope", () => {
    const env = envFor();
    const p = planeOf(env, "near")[0];
    const life = p.life;
    p.life = 1; const born = radiusOf(p, 1);
    p.life = 0.5; const mid = radiusOf(p, 1);
    p.life = 0.02; const old = radiusOf(p, 1);
    p.life = life;
    assert.ok(born < mid && mid < old, `disc must swell as it defocuses: ${born} ${mid} ${old}`);
    assert.ok(old / born > 1.5, "the swell must be visible, not a rounding error");
    const envs = env.state.list.map(q => q.sizeEnv);
    assert.ok(Math.max(...envs) - Math.min(...envs) > 0.3, "per-particle size envelope has no spread");
});

test("(b) colour is a ramp indexed by life, never flat", () => {
    const env = envFor();
    assert.ok(BOKEH_RAMP.length >= 5);
    const p = planeOf(env, "inner")[0];
    const life = p.life;
    p.life = 1; const young = rampIndexOf(p);
    p.life = 0; const aged = rampIndexOf(p);
    p.life = life;
    assert.ok(aged > young, "a disc must warm as it ages");
    const seen = new Set();
    run(env, 1000, 1 / 60);
    for (const q of env.state.list) {
        const i = rampIndexOf(q);
        assert.ok(Number.isInteger(i) && i >= 0 && i < BOKEH_RAMP.length, `ramp index out of range: ${i}`);
        seen.add(i);
    }
    assert.ok(seen.size >= 3, `field is using only ${seen.size} ramp stops`);
});

test("(c) the sub-populations really are different populations", () => {
    const env = envFor();
    const r = PLANES.map(pl => mean(planeOf(env, pl.id).map(p => radiusOf(p, env.state.geo))));
    assert.ok(r[3] / r[0] > 20, `near/far size ratio is only ${(r[3] / r[0]).toFixed(1)}x — that is not depth`);
});

// ---------------------------------------------------------------------------
// 5. The legibility budget — this is a backdrop behind chat text
// ---------------------------------------------------------------------------

test("a LARGE disc is never brighter than 6% alpha, over 1000 frames", () => {
    for (const [w, h, s] of [[412, 915, 0.4], [1400, 800, 0.86], [2560, 1440, 2.4]]) {
        const env = envFor(w, h, s);
        for (let i = 0; i < 1000; i++) {
            frame(env, 1 / 60);
            for (const p of env.state.list) {
                if (radiusOf(p, env.state.geo) < LEGIBILITY.largeDiscPx) continue;
                const a = alphaOf(p, env);
                assert.ok(a < LEGIBILITY.nearAlphaCap, `${p.plane.id} disc at alpha ${a} on ${w}x${h}`);
            }
        }
    }
});

test("no disc — nor a three-deep stack of them — outshines the muted body text", () => {
    const env = envFor(1400, 800, 2.4);
    let worst = 0, worstStack = 0;
    for (let i = 0; i < 600; i++) {
        frame(env, 1 / 60);
        for (const p of env.state.list) {
            if (radiusOf(p, env.state.geo) < LEGIBILITY.largeDiscPx) continue;
            const a = alphaOf(p, env), c = BOKEH_RAMP[rampIndexOf(p)];
            worst = Math.max(worst, luminance(a * c[0], a * c[1], a * c[2]));
            const k = Math.min(1, a * 3);                        // three overlapping discs, additively
            worstStack = Math.max(worstStack, luminance(k * c[0], k * c[1], k * c[2]));
        }
    }
    assert.ok(worst > 0, "nothing large was drawn — the assertion proved nothing");
    assert.ok(worst <= MUTED_L, `a single disc (L=${worst}) outshines --muted (L=${MUTED_L})`);
    assert.ok(worstStack <= MUTED_L, `a 3-stack (L=${worstStack}) outshines --muted (L=${MUTED_L})`);
});

test("the whole field lays down less ink than the published budget", () => {
    // Sum of alpha x pi r^2 as a fraction of the viewport. Deliberately
    // conservative: every sprite falls to zero well inside its nominal radius.
    let peak = 0;
    for (const [w, h, s] of [[412, 915, 0.4], [1400, 800, 0.86], [2560, 1440, 2.4]]) {
        const env = envFor(w, h, s);
        for (let i = 0; i < 1000; i++) {
            frame(env, 1 / 60);
            let ink = 0;
            for (const p of env.state.list) {
                const r = radiusOf(p, env.state.geo);
                ink += alphaOf(p, env) * Math.PI * r * r;
            }
            peak = Math.max(peak, ink / (w * h));
        }
    }
    assert.ok(peak > 0.0005, "the field is invisible — the budget proved nothing");
    assert.ok(peak <= LEGIBILITY.inkBudget, `peak ink ${(peak * 100).toFixed(2)}% > budget ${LEGIBILITY.inkBudget * 100}%`);
});

test("alpha can never be pushed over the plane cap by envelope, breathe or gain", () => {
    const env = envFor(1400, 800, 1);
    env.intensity = 4;                                  // an operator cranking the knob must not break the budget
    for (let i = 0; i < 400; i++) {
        frame(env, 1 / 60);
        for (const p of env.state.list) {
            const a = alphaOf(p, env);
            assert.ok(a >= 0 && a <= p.plane.alpha, `${p.plane.id} alpha ${a} > cap ${p.plane.alpha}`);
        }
    }
});

test("a disc fades in from zero and out to zero — it never pops", () => {
    const env = envFor();
    const p = env.state.list[0], life = p.life;
    p.life = 1; assert.equal(alphaOf(p, env), 0, "born invisible");
    p.life = 0.0001; assert.ok(alphaOf(p, env) < p.plane.alpha * 0.01, "dies invisible");
    p.life = 0.6; assert.ok(alphaOf(p, env) > p.plane.alpha * 0.5, "visible in between");
    p.life = life;
});

// ---------------------------------------------------------------------------
// 6. Engine contract: pooling, geometry, dpr, headless draw
// ---------------------------------------------------------------------------

test("pooled: the same particle objects survive 1000 frames of recycling", () => {
    const env = envFor();
    const list0 = env.state.list;
    const ids = new Set(list0);
    const n = list0.length;
    run(env, 1000, 1 / 60);
    assert.equal(env.state.list, list0, "the array itself must not be reallocated");
    assert.equal(env.state.list.length, n, "population must not drift");
    for (const p of env.state.list) assert.ok(ids.has(p), "a particle was allocated in the hot path");
});

test("no NaN anywhere after 1000 frames of mixed frame times", () => {
    const env = envFor();
    const dts = [1 / 60, 1 / 120, 1 / 30, 2, 1 / 144, 0];
    for (let i = 0; i < 1000; i++) frame(env, dts[i % dts.length]);
    for (const p of env.state.list) {
        for (const k of ["x", "y", "vx", "life", "baseR", "sizeEnv", "decay", "bobAmp"]) {
            assert.ok(Number.isFinite(p[k]), `${p.plane.id}.${k} = ${p[k]}`);
        }
        assert.ok(p.life > 0 && p.life <= 1, `life out of range: ${p.life}`);
        assert.ok(Number.isFinite(alphaOf(p, env)) && Number.isFinite(radiusOf(p, env.state.geo)));
    }
});

test("geometry ignores devicePixelRatio (the bug sizing.js exists to prevent)", () => {
    const a = envFor(1400, 800, 0.86, 3); a.dpr = 1;
    const b = envFor(1400, 800, 0.86, 3); b.dpr = 3;
    run(a, 60, 1 / 60); run(b, 60, 1 / 60);
    assert.equal(a.state.geo, b.state.geo);
    for (let i = 0; i < a.state.list.length; i++) {
        assert.equal(radiusOf(a.state.list[i], a.state.geo), radiusOf(b.state.list[i], b.state.geo));
    }
});

test("a keystroke-frequency resize does NOT re-scatter the field", () => {
    const env = envFor();
    run(env, 60, 1 / 60);
    const before = env.state.list.map(p => p.x);
    env.height = 780;                                   // composer grew a line; same density bucket
    descriptor.resize(env);
    assert.deepEqual(env.state.list.map(p => p.x), before, "the field jumped on a resize");
    assert.equal(env.state.geo, 780 / 900, "but geometry must track the new box");
});

test("rearm tops the field up without blinking it", () => {
    const env = envFor();
    run(env, 60, 1 / 60);
    const before = env.state.list.map(p => p.x);
    descriptor.rearm(env);
    assert.deepEqual(env.state.list.map(p => p.x), before);
});

test("draw() is a no-op headlessly and never touches the blend mode", () => {
    const env = envFor();
    const ctx = fakeCtx();
    run(env, 10, 1 / 60);
    descriptor.draw(ctx, env);                          // no document => no atlas => nothing drawn
    assert.equal(ctx.calls.length, 0);
    assert.equal(ctx.blendWrites.length, 0, "effects never set their own compositing");
});
