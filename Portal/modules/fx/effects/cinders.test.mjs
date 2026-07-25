// Cinders on the Wind — headless physics tests.
//
// This effect is a PARAMETER FORK of Embers, and its legibility budget is
// "identical to the shipped Embers field". That is not a comment here, it is an
// assertion: both fields are driven for 200 frames from the same seed and the
// additive ink cinders puts on the canvas is measured against embers'.
//
// Everything runs with an injected seeded rand and an injected clock, so the
// physics is reproducible and nothing reaches for Math.random()/performance.now().
//
// Run: node --test Portal/modules/fx/effects/cinders.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, applyClearPolicy, applyBlend, resetBlend } from "../registry.js";
import cinders, { WIND, gustAt } from "./cinders.js";
import embers from "./embers.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG (same generator the registry tests use). */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Canvas2D recorder that meters INK: sum of alpha x drawn area, in px². That is
 *  the quantity the legibility budget is actually about — how much light lands
 *  on the chat text — and it captures globalAlpha at call time, not after. */
function meterCtx() {
    let gco = "source-over";
    const ctx = {
        ink: 0, draws: 0, peakAlpha: 0, maxRadius: 0, blendWrites: [], calls: [],
        globalAlpha: 1, fillStyle: null,
        clearRect: () => { ctx.calls.push("clearRect"); },
        fillRect: () => { ctx.calls.push("fillRect"); },
        drawImage: (_img, _x, _y, w, h) => {
            ctx.ink += ctx.globalAlpha * w * h;
            ctx.draws++;
            if (ctx.globalAlpha > ctx.peakAlpha) ctx.peakAlpha = ctx.globalAlpha;
            if (w / 2 > ctx.maxRadius) ctx.maxRadius = w / 2;
            ctx.calls.push("drawImage");
        },
        beginPath() {}, arc() {}, fill() {},
        createRadialGradient: () => ({ addColorStop() {} }),
    };
    Object.defineProperty(ctx, "globalCompositeOperation", {
        get: () => gco, set: (v) => { gco = v; ctx.blendWrites.push(v); },
    });
    return ctx;
}

function testEnv(over = {}) {
    return createEnv({
        width: 1400, height: 800, scale: 1, rand: seeded(42),
        sprites: { ember: [0, 1, 2, 3, 4, 5].map(i => ({ i })), star: {} },
        ...over,
    });
}

/** One engine frame, exactly as ember-fx.js's loop() drives it. */
function frame(ctx, effect, env, dt) {
    env.dt = dt;
    effect.update(dt, env);
    applyClearPolicy(ctx, effect, env);
    applyBlend(ctx, effect);
    const before = ctx.blendWrites.length;
    effect.draw(ctx, env);
    const wrote = ctx.blendWrites.length - before;
    resetBlend(ctx);
    return wrote;
}

/** Drive an effect for `frames` frames at `dt`, returning the meter. */
function run(effect, env, frames, dt = 1 / 60, ctx = meterCtx()) {
    for (let f = 1; f <= frames; f++) { env.now = f * dt * 1000; frame(ctx, effect, env, dt); }
    return ctx;
}

const mean = (xs) => xs.reduce((a, b) => a + b, 0) / (xs.length || 1);

// ---------------------------------------------------------------------------
// 1. Descriptor contract
// ---------------------------------------------------------------------------

test("declares the emissive blend and the Embers heat smear, and sets neither itself", () => {
    assert.equal(cinders.id, "cinders");
    assert.equal(cinders.label, "Cinders on the Wind");
    assert.equal(cinders.blend, "lighter", "an emissive field must ADD light");
    assert.equal(cinders.clearPolicy, embers.clearPolicy, "the smear is Embers', unchanged");

    const env = testEnv();
    cinders.init(env);
    const ctx = meterCtx();
    let wrote = 0;
    for (let f = 1; f <= 3; f++) { env.now = f * 16.7; wrote += frame(ctx, cinders, env, 1 / 60); }
    assert.equal(wrote, 0, "draw() wrote globalCompositeOperation — that is the engine's job");
    assert.ok(ctx.draws > 0, "the field drew nothing");
});

test("draws nothing rather than throwing when the sprite atlas is absent", () => {
    const env = testEnv({ sprites: null });
    cinders.init(env);
    const ctx = meterCtx();
    frame(ctx, cinders, env, 1 / 60);
    assert.deepEqual(ctx.calls, ["fillRect"], "only the engine's fade ran");
});

// ---------------------------------------------------------------------------
// 2. Population bounds — identical to the shipped Embers field
// ---------------------------------------------------------------------------

test("population: seeds at 140, settles near the target, drains when idle, rearms empty", () => {
    const env = testEnv({ rand: seeded(5) });
    cinders.init(env);
    assert.equal(env.state.list.length, 140, "seeded at 140 * scale, exactly as Embers");

    const ctx = run(cinders, env, 120);
    const live = env.state.list.length;
    assert.ok(live > 100 && live < 260, `population should settle near the target, got ${live}`);

    env.active = false;                       // generation ended: no more spawning
    run(cinders, env, 280, 1 / 60, ctx);
    assert.ok(env.state.list.length < live, "the field must drain once generation stops");

    cinders.rearm(env);
    assert.equal(env.state.list.length, 0, "each turn starts empty and the current refills it");
    assert.equal(env.state.acc, 0);
});

test("population is hard-capped even under a sustained surge", () => {
    const env = testEnv({ rand: seeded(6), scale: 2.4, surge: 1 });
    cinders.init(env);
    run(cinders, env, 600);
    assert.ok(env.state.list.length <= 3000, `hard cap breached: ${env.state.list.length}`);
    assert.ok(env.state.list.length <= Math.round((150 + 160) * 2.4 * 1.3) + 1,
        `population overshot its own target band: ${env.state.list.length}`);
});

// ---------------------------------------------------------------------------
// 3. Motion — a lateral CURRENT, not a rise
// ---------------------------------------------------------------------------

test("the field travels sideways: every mote is carried downwind, not upward", () => {
    const env = testEnv({ rand: seeded(13) });
    cinders.init(env);
    run(cinders, env, 120);                   // 2 s: long past terminal velocity

    const vx = env.state.list.map(p => p.vx);
    const vy = env.state.list.map(p => p.vy);
    assert.ok(mean(vx) > 0.5 * WIND.speed,
        `the current must dominate the swirl; mean vx = ${mean(vx).toFixed(1)} px/s`);
    assert.ok(env.state.list.every(p => p.vx > 0),
        "no mote may be travelling upwind — that is the curl gain overwhelming the current");
    assert.ok(mean(vx) > 4 * mean(vy.map(Math.abs)),
        `arcs must be SHALLOW: |vy| = ${mean(vy.map(Math.abs)).toFixed(1)} vs vx = ${mean(vx).toFixed(1)}`);
});

test("buoyancy is dropped toward zero: in STILL AIR the field barely drifts", () => {
    // The honest test of "buoyancy -> 0" is to take the wind away and see what is
    // left. Embers still climbs on its own heat; cinders has nothing to climb on,
    // so whatever vertical motion it has downwind belongs to WIND.tilt, not to
    // buoyancy. (Comparing the two fields WITH wind proves nothing — the tilt
    // would be doing the lifting.)
    const stillAir = (eff) => {
        const e = testEnv({ rand: seeded(21) });
        eff.init(e);
        run(eff, e, 180);
        return mean(e.state.list.map(p => p.vy));
    };
    const saved = WIND.speed;
    let calm;
    try { WIND.speed = 0; calm = stillAir(cinders); } finally { WIND.speed = saved; }
    const embersRise = stillAir(embers);
    assert.ok(Math.abs(calm) < 1.5,
        `still air must leave almost no vertical drift; got ${calm.toFixed(2)} px/s`);
    assert.ok(embersRise < -3 && Math.abs(calm) < Math.abs(embersRise) / 3,
        `Embers climbs on its own heat (${embersRise.toFixed(2)} px/s); cinders must not`);
});

test("the horizontal wrap keeps the field inside the band at full width", () => {
    const env = testEnv({ rand: seeded(9) });
    cinders.init(env);
    run(cinders, env, 400);                   // ~6.7 s: every mote has crossed at least once
    for (const p of env.state.list) {
        assert.ok(p.x >= -20 && p.x <= env.width + 20, `mote escaped horizontally at x=${p.x}`);
        assert.ok(p.life > 0, "dead motes must be culled");
    }
    const buckets = [0, 0, 0, 0];
    for (const p of env.state.list) buckets[Math.max(0, Math.min(3, Math.floor((p.x / env.width) * 4)))]++;
    assert.ok(buckets.every(b => b > 0), `the wrap must keep all four quarters populated: ${buckets}`);
});

// ---------------------------------------------------------------------------
// 4. WIND is a real seam (a future weather mode drives it)
// ---------------------------------------------------------------------------

test("WIND.speed drives the field — zeroing it stops the current", () => {
    const measure = () => {
        const e = testEnv({ rand: seeded(31) });
        cinders.init(e);
        run(cinders, e, 120);
        return mean(e.state.list.map(p => p.vx));
    };
    const blowing = measure();
    const saved = WIND.speed;
    try {
        WIND.speed = 0;
        const calm = measure();
        assert.ok(Math.abs(calm) < 0.25 * blowing,
            `with no wind the field must have no lateral bias; got ${calm.toFixed(1)} vs ${blowing.toFixed(1)}`);
    } finally { WIND.speed = saved; }
});

test("gusts are time-based events, bounded, and mostly absent", () => {
    const samples = [];
    for (let i = 0; i < 6000; i++) samples.push(gustAt(i * 0.02, 1.7));   // 120 s at 50 Hz
    assert.ok(samples.every(g => g >= 0 && g <= 1), "gust envelope must stay in [0,1]");
    assert.ok(Math.max(...samples) > 0.3, "there must be real gusts, not a wobble");
    const calmFraction = samples.filter(g => g === 0).length / samples.length;
    assert.ok(calmFraction > 0.4, `gusts must be EVENTS; air was calm only ${(calmFraction * 100) | 0}% of the time`);
    assert.equal(gustAt(3.3, 0.5), gustAt(3.3, 0.5), "the envelope is a pure function of time");
});

test("a gust both shoves and scatters the field", () => {
    const sample = () => {
        const e = testEnv({ rand: seeded(77) });
        cinders.init(e);
        e.state.gustPhase = 1.35;              // a gust rides through this window
        run(cinders, e, 90);
        return { vx: mean(e.state.list.map(p => p.vx)), spread: mean(e.state.list.map(p => Math.abs(p.vy))) };
    };
    const gusty = sample();
    const g = WIND.gust, s = WIND.scatter;
    let calm;
    try { WIND.gust = 0; WIND.scatter = 0; calm = sample(); } finally { WIND.gust = g; WIND.scatter = s; }
    assert.ok(gusty.vx > calm.vx, `a gust must shove: ${gusty.vx.toFixed(1)} vs ${calm.vx.toFixed(1)} px/s`);
    assert.ok(gusty.spread > calm.spread,
        `a gust must SCATTER (extra curl gain): |vy| ${gusty.spread.toFixed(1)} vs ${calm.spread.toFixed(1)}`);
});

test("even at a gust peak the current still wins — nothing is blown backwards", () => {
    const e = testEnv({ rand: seeded(44) });
    cinders.init(e);
    e.state.gustPhase = 1.35;
    run(cinders, e, 240);
    assert.ok(e.state.list.every(p => p.vx > 0),
        "a gust may scatter the field but must never reverse it");
});

// ---------------------------------------------------------------------------
// 5. THE LEGIBILITY BUDGET — asserted, not asserted-in-a-comment
// ---------------------------------------------------------------------------

test("legibility: cinders puts NO MORE additive ink on the canvas than shipped Embers", () => {
    // Chat-sized box, 200 frames, same seed, same driver. `ink` is
    // sum(globalAlpha x drawn area) — the light that lands on the chat text.
    const inkOf = (eff) => {
        const e = testEnv({ rand: seeded(1234) });
        eff.init(e);
        return run(eff, e, 200);
    };
    const c = inkOf(cinders), b = inkOf(embers);
    // ~0.56 of Embers as measured. A field that TRAVELS lays its light down at a
    // new place every frame, so under the shared 0.20 persistence veil it covers
    // more of the frame at lower peak brightness than a hovering field of the
    // same instantaneous ink — which is the direction legibility wants.
    assert.ok(c.ink <= b.ink,
        `budget breached: cinders ${(c.ink / 1e6).toFixed(1)}Mpx² vs embers ${(b.ink / 1e6).toFixed(1)}Mpx²`);
    assert.ok(c.ink > 0.4 * b.ink,
        `so far under budget the field would read as empty: ${(c.ink / b.ink).toFixed(2)} of Embers`);
    // Same alpha ceiling, to the float: both fields top out at the 0.85 spark
    // alpha (the sampled peaks differ only in how close a flicker got to 1.0).
    assert.ok(c.peakAlpha <= 0.85 + 1e-6 && Math.abs(c.peakAlpha - b.peakAlpha) < 1e-4,
        `per-particle alpha ceiling must be Embers': ${c.peakAlpha} vs ${b.peakAlpha}`);
    assert.ok(c.maxRadius <= b.maxRadius,
        `largest drawn sprite must not exceed Embers': ${c.maxRadius.toFixed(1)} vs ${b.maxRadius.toFixed(1)}`);
});

test("legibility holds on a phone-sized box too", () => {
    const inkOf = (eff) => {
        const e = testEnv({ rand: seeded(88), width: 412, height: 720, scale: 0.4 });
        eff.init(e);
        return run(eff, e, 200).ink;
    };
    // Well UNDER on a phone, and deliberately so: apparentScale shrinks the
    // geometry with the CSS box, which Embers never did — a 35 px glow is 8% of a
    // desktop chat pane and 17% of a phone's, i.e. Embers is oversized on a
    // phone. The budget is a ceiling, and here cinders sits far below it.
    assert.ok(inkOf(cinders) < inkOf(embers), "the budget is a ceiling at every viewport");
});

// ---------------------------------------------------------------------------
// 6. Geometry is a function of the CSS box ONLY (the shipped dpr bug)
// ---------------------------------------------------------------------------

test("devicePixelRatio does not touch particle geometry", () => {
    const inkAtDpr = (dpr) => {
        const e = testEnv({ rand: seeded(4), dpr });
        cinders.init(e);
        return run(cinders, e, 200).ink;
    };
    assert.equal(inkAtDpr(1), inkAtDpr(3),
        "geometry tracked devicePixelRatio — that is the bug sizing.js exists to prevent");
});

test("geometry follows the CSS box and is picked up without a resize() call", () => {
    const e = testEnv({ rand: seeded(4) });
    cinders.init(e);
    const big = run(cinders, e, 1).ink;
    e.width = 300; e.height = 300;             // engine resized but never called resize()
    const small = run(cinders, e, 1).ink;
    assert.ok(small < big, "a smaller CSS box must shrink the field");
    cinders.resize(e);
    assert.equal(e.state.geom, 0.5, "sizing.js floors apparentScale at 0.5 — the field never becomes dust");
    e.width = 4000; e.height = 3000;
    cinders.resize(e);
    assert.equal(e.state.geom, 1, "the geometry multiplier is a CEILING: a huge viewport never spends more ink than Embers");
});

// ---------------------------------------------------------------------------
// 7. Size-over-life and the two sub-populations
// ---------------------------------------------------------------------------

test("every mote has a size-over-life curve and its own envelope multiplier", () => {
    const e = testEnv({ rand: seeded(17) });
    cinders.init(e);
    const swells = e.state.list.map(p => p.swell);
    assert.ok(Math.max(...swells) - Math.min(...swells) > 0.2,
        "the per-particle envelope multiplier is not varying");
    assert.ok(swells.every(s => s >= 0.72 && s <= 1.12));

    // Drawn radius must actually change over a mote's life, and never exceed the
    // spawn radius — that ceiling is what the ink budget above rests on.
    const p = e.state.list[0];
    const radii = [];
    const ctx = meterCtx();
    for (const life of [1, 0.85, 0.6, 0.3, 0.05]) {
        p.life = life;
        e.state.list.length = 1;
        ctx.maxRadius = 0;
        cinders.draw(ctx, e);
        radii.push(ctx.maxRadius);
    }
    assert.ok(new Set(radii).size === radii.length, `size is constant over life: ${radii}`);
    assert.ok(radii[1] > radii[0] && radii[4] < radii[2], "expected bloom-in then burn-down");
});

test("two sub-populations with genuinely different parameters", () => {
    const e = testEnv({ rand: seeded(23) });
    cinders.init(e);
    const sparks = e.state.list.filter(p => p.spark);
    const flakes = e.state.list.filter(p => !p.spark);
    assert.ok(sparks.length > 5 && flakes.length > 50, `both populations must exist: ${sparks.length}/${flakes.length}`);
    assert.ok(mean(sparks.map(p => p.r)) < mean(flakes.map(p => p.r)), "sparks are the small ones");
    assert.ok(sparks.every(p => p.sail > flakes[0].sail), "sparks carry more sail");
    assert.ok(sparks.every(p => p.gain > flakes[0].gain), "sparks are thrown harder by a gust");

    run(cinders, e, 120);
    const sv = mean(e.state.list.filter(p => p.spark).map(p => p.vx));
    const fv = mean(e.state.list.filter(p => !p.spark).map(p => p.vx));
    assert.ok(sv > fv, `sparks must outrun the flakes downwind: ${sv.toFixed(1)} vs ${fv.toFixed(1)}`);
});

// ---------------------------------------------------------------------------
// 8. Frame-rate independence, pooling, and 1000-frame stability
// ---------------------------------------------------------------------------

test("60 Hz and 120 Hz travel the same distance in the same second", () => {
    const at = (dt, frames) => {
        const e = testEnv({ rand: seeded(3) });
        cinders.init(e);
        e.active = false;                      // freeze the population: same motes both runs
        const p = e.state.list[0];
        run(cinders, e, frames, dt);
        return { vx: p.vx, x: p.x };
    };
    const a = at(1 / 60, 60), b = at(1 / 120, 120);
    assert.ok(Math.abs(a.vx - b.vx) < 0.02 * Math.abs(a.vx),
        `terminal velocity is frame-rate dependent: ${a.vx.toFixed(1)} vs ${b.vx.toFixed(1)}`);
    assert.ok(Math.abs(a.x - b.x) < 4, `position diverged: ${a.x.toFixed(1)} vs ${b.x.toFixed(1)}`);
});

test("the hot path recycles particles instead of allocating them", () => {
    const e = testEnv({ rand: seeded(19) });
    cinders.init(e);
    run(cinders, e, 240);
    const seen = new Set(e.state.list);
    const pooled = e.state.pool.length;
    assert.ok(pooled > 0, "culled motes must go back to the pool, not to the GC");

    cinders.rearm(e);
    assert.equal(e.state.list.length, 0);
    run(cinders, e, 120);
    const reused = e.state.list.filter(p => seen.has(p)).length;
    assert.ok(reused > 0.8 * e.state.list.length,
        `refill allocated fresh objects instead of reusing the pool: ${reused}/${e.state.list.length}`);
    assert.ok(e.state.pool.length + e.state.list.length <= 3000 + 1, "the pool itself must stay bounded");
});

test("1000 frames: no NaN anywhere in the field", () => {
    const e = testEnv({ rand: seeded(101), surge: 0.5 });
    cinders.init(e);
    const ctx = meterCtx();
    for (let f = 1; f <= 1000; f++) {
        e.now = f * 16.7;
        e.active = f < 700;                    // build, then drain
        frame(ctx, cinders, e, f % 97 === 0 ? 0.25 : 1 / 60);   // a long stall every ~1.6 s
    }
    for (const p of e.state.list) {
        for (const k of ["x", "y", "vx", "vy", "life", "r", "swell"]) {
            assert.ok(Number.isFinite(p[k]), `p.${k} is ${p[k]} after 1000 frames`);
        }
    }
    assert.ok(Number.isFinite(ctx.ink) && ctx.ink > 0, "ink meter went non-finite");
    assert.ok(e.state.list.length > 0, "the field died out entirely");
});

test("state is reproducible from a seeded rand — no Math.random inside", () => {
    const once = () => {
        const e = testEnv({ rand: seeded(7) });
        cinders.init(e);
        run(cinders, e, 30);
        return JSON.stringify(e.state.list);
    };
    assert.equal(once(), once());
});
