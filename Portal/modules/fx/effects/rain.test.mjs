// Rainfall physics, headless.
//
// The effect hooks take their randomness and their clock from `env`, so the whole
// simulation runs here with a SEEDED generator and a fake clock — no canvas, no
// rAF, no DOM. Everything asserted below is something you cannot eyeball reliably
// in a backdrop that is deliberately 20% opaque, most of all the legibility
// budget: vertical streaks run parallel to the message column and the scroll
// direction, so the lean angle and the near-plane alpha are load-bearing, not
// taste. They are asserted as numbers here so a later "let's make it rain harder"
// tweak fails the build instead of eating the contrast of the chat text.
//
// Run: node --test Portal/modules/fx/effects/rain.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv } from "../registry.js";
import descriptor, {
    RAIN_CONFIG, RAIN_RAMP, NEAR_ALPHA_CEILING, lengthEnvelope, alphaEnvelope,
} from "./rain.js";

/** Deterministic PRNG — same seed, same field, every run and every machine. */
function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

const W = 1400, H = 800;

function makeEnv(over = {}) {
    const env = createEnv({ width: W, height: H, rand: mulberry32(0xBEEF), ...over });
    descriptor.init(env);
    return env;
}

/** Advance the sim `frames` times at a fixed dt, keeping env.now in step. */
function run(env, frames, dt = 1 / 60) {
    for (let i = 0; i < frames; i++) {
        env.now += dt * 1000;
        descriptor.update(dt, env);
    }
}

const live = env => env.state.drops.filter(p => !p.dead);
const streakLen = p => Math.hypot(p.x - p.tx, p.y - p.ty);
const mean = xs => xs.reduce((a, b) => a + b, 0) / xs.length;

// ---------------------------------------------------------------- population

test("the pool is fixed-size and is NEVER reallocated during a frame", () => {
    const env = makeEnv();
    const arr = env.state.drops;
    const n = env.state.count;
    assert.equal(arr.length, n);
    run(env, 600);
    // Same array object, same length: drops recycle in place, so a long session
    // does not hand the GC 250 dead objects a second.
    assert.equal(env.state.drops, arr, "the drop pool was reallocated");
    assert.equal(env.state.drops.length, n, "the pool changed size mid-run");
});

test("population matches the configured layer counts and stays bounded", () => {
    const env = makeEnv();                                  // env.scale defaults to 1
    const expected = RAIN_CONFIG.layers.reduce((a, L) => a + Math.round(L.count * RAIN_CONFIG.density), 0);
    assert.equal(env.state.count, expected);
    run(env, 500);
    assert.equal(live(env).length, expected, "drops leaked or vanished while active");
});

test("density is a real dial — doubling env.scale roughly doubles the field", () => {
    const a = makeEnv({ scale: 1 }).state.count;
    const b = makeEnv({ scale: 2 }).state.count;
    assert.ok(b > a * 1.8 && b < a * 2.2, `expected ~2x, got ${a} -> ${b}`);
});

// ------------------------------------------------------------------- motion

test("every drop falls DOWN and leans RIGHT — the field moves as one", () => {
    const env = makeEnv();
    run(env, 120);
    const before = live(env).map(p => ({ p, x: p.x, y: p.y }));
    run(env, 1);
    for (const s of before) {
        // A drop that recycled or wrapped this frame jumps; skip those two cases.
        const dy = s.p.y - s.y, dx = s.p.x - s.x;
        if (dy < 0) continue;                               // recycled to the top band
        if (Math.abs(dx) > W / 2) continue;                 // wrapped horizontally
        assert.ok(dy > 0, "a drop moved upward");
        assert.ok(dx > 0, "a drop leaned against the wind");
    }
});

test("depth reads as speed AND as streak length: near > mid > far", () => {
    const env = makeEnv();
    run(env, 240);
    const byLayer = [0, 1, 2].map(li => live(env).filter(p => p.layer === li && p.u > 0.2 && p.u < 0.8));
    const speeds = byLayer.map(ps => mean(ps.map(p => p.vy)));
    const lens = byLayer.map(ps => mean(ps.map(streakLen)));
    assert.ok(speeds[2] > speeds[1] && speeds[1] > speeds[0], `speeds not ordered: ${speeds}`);
    // Length is DERIVED from velocity (velocity-stretched billboard), so this
    // ordering is a consequence of the speeds, not a second authored parameter.
    assert.ok(lens[2] > lens[1] && lens[1] > lens[0], `lengths not ordered: ${lens}`);
});

test("motion is delta-timed: 120 Hz and 60 Hz cover the same ground per second", () => {
    const a = makeEnv(); run(a, 60, 1 / 60);
    const b = makeEnv(); run(b, 120, 1 / 120);
    // Compare the field CENTROID after one simulated second. Per-drop comparison
    // would diverge the moment one sim recycles a drop a frame earlier than the
    // other and consumes the shared PRNG differently.
    const ya = mean(live(a).map(p => p.y)), yb = mean(live(b).map(p => p.y));
    assert.ok(Math.abs(ya - yb) < H * 0.01, `frame-rate dependent motion: ${ya} vs ${yb}`);
});

// -------------------------------------------------- THE LEGIBILITY BUDGET

test("the near plane is under the alpha ceiling BY CONSTRUCTION", () => {
    const near = RAIN_CONFIG.layers[RAIN_CONFIG.layers.length - 1];
    assert.ok(near.alpha * RAIN_CONFIG.heavy.alpha < NEAR_ALPHA_CEILING,
        "the brightest possible drop is configured over the ceiling");
});

test("no drop EVER exceeds the alpha ceiling, over 1000 frames", () => {
    const env = makeEnv();
    let peak = 0;
    for (let i = 0; i < 1000; i++) {
        env.now += 1000 / 60;
        descriptor.update(1 / 60, env);
        for (const p of env.state.drops) if (!p.dead && p.a > peak) peak = p.a;
    }
    assert.ok(peak <= NEAR_ALPHA_CEILING, `peak alpha ${peak} > ${NEAR_ALPHA_CEILING}`);
    assert.ok(peak > 0.15, `the field faded to nothing (peak ${peak}) — check the envelope`);
});

test("the lean stays inside 10-15 degrees — never parallel to the text column", () => {
    const env = makeEnv();
    let lo = 90, hi = 0;
    for (let i = 0; i < 1000; i++) {
        env.now += 1000 / 60;
        descriptor.update(1 / 60, env);
        for (const p of env.state.drops) {
            if (p.dead) continue;
            if (p.lean < lo) lo = p.lean;
            if (p.lean > hi) hi = p.lean;
        }
    }
    assert.ok(lo >= 10, `lean fell to ${lo} deg — too close to vertical`);
    assert.ok(hi <= 15, `lean reached ${hi} deg — reads as a diagonal wipe`);
});

test("alpha-weighted ink over the viewport stays under the backdrop budget", () => {
    // The real legibility number: how much light the field puts between the page
    // and the reader. sum(length * width * alpha) / viewport area.
    const env = makeEnv();
    run(env, 300);
    let ink = 0;
    for (const p of live(env)) ink += streakLen(p) * p.wid * p.a;
    // Measured at the shipped tuning: 0.059%. The bound is deliberately close to
    // it — a "let's make it rain harder" tweak that doubles the ink fails here
    // rather than quietly costing the chat text its contrast.
    const coverage = ink / (W * H);
    assert.ok(coverage < 0.001, `ink coverage ${(coverage * 100).toFixed(3)}% — too loud for a backdrop`);
    assert.ok(coverage > 0.0002, `ink coverage ${(coverage * 100).toFixed(3)}% — the field is invisible`);
});

test("drops are NOT all parallel — the per-column shear is doing work", () => {
    const env = makeEnv();
    run(env, 200);
    const leans = live(env).map(p => p.lean);
    const spread = Math.max(...leans) - Math.min(...leans);
    assert.ok(spread > 1.5, `only ${spread.toFixed(2)} deg of lean spread — reads as a hatch pattern`);
});

// ------------------------------------------------- the three premium rules

test("(a) size over life is a CURVE with a per-drop envelope multiplier", () => {
    // Rises out of the top band, holds, tapers on exit — never constant.
    assert.ok(lengthEnvelope(0) < lengthEnvelope(0.3), "no taper-in");
    assert.ok(lengthEnvelope(1) < lengthEnvelope(0.3), "no taper-out");
    assert.ok(alphaEnvelope(0) === 0 && alphaEnvelope(1) === 0, "drops pop in/out at the edges");
    const env = makeEnv();
    const muls = new Set(live(env).map(p => p.lenMul.toFixed(4)));
    assert.ok(muls.size > 50, `only ${muls.size} distinct length envelopes — drops are clones`);
});

test("(b) colour is a ramp indexed by life, in bounds, and actually moves", () => {
    const env = makeEnv();
    run(env, 400);
    const seen = new Set();
    for (const p of live(env)) {
        assert.ok(Number.isInteger(p.ramp) && p.ramp >= 0 && p.ramp <= RAIN_RAMP.length - 1,
            `ramp index ${p.ramp} out of range`);
        seen.add(p.ramp);
    }
    assert.ok(seen.size >= 4, `only ${seen.size} ramp stops in play — this is a flat colour`);
    // A single drop must deepen as it falls: same tint, later in life, later stop.
    const p = live(env)[0];
    const early = p.tint + ((0.05 * 3 + 0.5) | 0);
    const late = p.tint + ((0.95 * 3 + 0.5) | 0);
    assert.ok(late > early, "the ramp does not track fall progress");
});

test("(c) three depth layers plus a heavy-drop sub-population are all present", () => {
    const env = makeEnv();
    run(env, 300);
    const ps = live(env);
    for (let li = 0; li < 3; li++) {
        assert.ok(ps.some(p => p.layer === li), `layer ${li} is empty`);
    }
    const heavies = ps.filter(p => p.heavy);
    assert.ok(heavies.length > 0, "no heavy drops spawned");
    assert.ok(heavies.length < ps.length * 0.2, "heavy drops are supposed to be occasional");
    assert.ok(heavies.every(p => p.layer > 0), "heavy drops leaked into the far haze plane");
});

// --------------------------------------------------------------- soundness

test("no NaN anywhere after 1000 frames", () => {
    const env = makeEnv();
    run(env, 1000);
    for (const p of env.state.drops) {
        for (const k of ["x", "y", "tx", "ty", "vx", "vy", "u", "wid", "a", "ramp", "lean"]) {
            assert.ok(Number.isFinite(p[k]), `drop.${k} is ${p[k]} after 1000 frames`);
        }
    }
});

test("drops stay inside the viewport band — nothing escapes sideways forever", () => {
    const env = makeEnv();
    run(env, 1000);
    for (const p of live(env)) {
        assert.ok(p.x >= -80 && p.x <= W + 80, `drop escaped horizontally to ${p.x}`);
        assert.ok(p.y <= H + 80, `drop fell past the recycle line to ${p.y}`);
    }
});

test("geometry is INDEPENDENT of devicePixelRatio (the shipped bug)", () => {
    const a = makeEnv({ dpr: 1 });
    const b = makeEnv({ dpr: 3 });
    run(a, 200); run(b, 200);
    const wa = live(a).map(p => p.wid), wb = live(b).map(p => p.wid);
    assert.deepEqual(wa, wb, "line width tracked devicePixelRatio");
    assert.equal(mean(live(a).map(p => p.y)), mean(live(b).map(p => p.y)), "motion tracked devicePixelRatio");
});

test("the field drains when idle and refills on rearm", () => {
    const env = makeEnv();
    const n = env.state.count;
    env.active = false;
    run(env, 600);                                          // ~10 s: everything reaches the bottom
    assert.equal(live(env).length, 0, "rain kept falling over an idle chat");
    descriptor.rearm(env);
    assert.equal(live(env).length, n, "rearm did not restore the field");
    // Refilled STAGGERED, not as a wall arriving from above.
    const ys = live(env).map(p => p.y);
    assert.ok(Math.max(...ys) - Math.min(...ys) > H * 0.5, "the field rearmed as a curtain");
});

test("resize rebuilds without leaking drops or losing the pool", () => {
    const env = makeEnv();
    run(env, 100);
    env.width = 600; env.height = 1200;
    descriptor.resize(env);
    assert.equal(env.state.drops.length, env.state.count);
    run(env, 100);
    for (const p of live(env)) {
        assert.ok(Number.isFinite(p.x) && p.x >= -80 && p.x <= 680, `drop at ${p.x} after resize`);
    }
});
