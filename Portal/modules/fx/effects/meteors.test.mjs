// Meteor Fall physics tests — headless, seeded, no DOM.
//
// The invariants that actually matter for THIS effect, in order of how badly a
// regression would hurt:
//   1. the legibility budget — nothing is ever drawn in the centre 40% of the
//      width, and stars inside the reading box are dimmed. Asserted from the
//      RECORDED DRAW CALLS, not from the config, so a draw-side mistake is caught.
//   2. the schedule correlates with nothing — identical spawn times whether
//      env.active/env.surge say the machine is busy or idle, and rearm() (the one
//      hook that fires on activation) never pulls an event forward.
//   3. long silences — a low duty cycle with real multi-second gaps is the effect.
//   4. delta-timing, a fixed pool, and no NaN.
//
// Run: node --test Portal/modules/fx/effects/meteors.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, applyClearPolicy, applyBlend, resetBlend, getEffect } from "../registry.js";
import meteors, { METEOR_CONFIG, envelope } from "./meteors.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG — same one registry.test.mjs uses. */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Canvas2D recorder that keeps the geometry AND the paint of every mark. */
function fakeCtx() {
    const arcs = [];        // {x, y, r, alpha}
    const segs = [];        // {x1, y1, x2, y2, w, alpha}
    const blendWrites = [];
    let pending = null, cur = null;
    const ctx = {
        arcs, segs, blendWrites,
        globalAlpha: 1, fillStyle: null, strokeStyle: null, lineWidth: 1, lineCap: null,
        clearRect() {}, fillRect() {}, drawImage() {},
        beginPath() { pending = null; },
        arc(x, y, r) { cur = { x, y, r }; },
        moveTo(x, y) { pending = { x1: x, y1: y }; },
        lineTo(x, y) { if (pending) { pending.x2 = x; pending.y2 = y; } },
        fill() { if (cur) arcs.push({ ...cur, alpha: alphaOf(ctx.fillStyle) }); cur = null; },
        stroke() { if (pending) segs.push({ ...pending, w: ctx.lineWidth, alpha: alphaOf(ctx.strokeStyle) }); pending = null; },
    };
    let gco = "source-over";
    Object.defineProperty(ctx, "globalCompositeOperation", { get: () => gco, set: (v) => { gco = v; blendWrites.push(v); } });
    return ctx;
}

function alphaOf(style) {
    if (typeof style !== "string") return 0;
    const m = /rgba\([^)]*,([\d.]+)\)$/.exec(style);
    return m ? parseFloat(m[1]) : 1;
}

function testEnv(over = {}) {
    return createEnv({ width: 1400, height: 800, scale: 1, rand: seeded(42), ...over });
}

/** One engine frame, driven exactly as ember-fx.js's loop() drives it.
 *  @returns {number} globalCompositeOperation writes made by the EFFECT itself */
function frame(ctx, env, dt) {
    env.dt = dt;
    meteors.update(dt, env);
    if (!ctx) return 0;
    applyClearPolicy(ctx, meteors, env);
    applyBlend(ctx, meteors);
    const before = ctx.blendWrites.length;
    meteors.draw(ctx, env);
    const mine = ctx.blendWrites.length - before;
    resetBlend(ctx);
    return mine;
}

const alive = (env) => env.state.meteors.filter(m => m.alive);

/**
 * Run `secs` of simulation, tracking each streak across its life.
 *
 * Meteors are POOLED, so object identity is not streak identity — a slot is
 * reused by the next streak. Records are opened on the dead->alive edge and
 * closed on the way back, which is the only correct way to count them.
 */
function run(env, secs, dt = 1 / 60, ctx = null) {
    const streaks = [], open = new Map();
    let busyFrames = 0, frames = 0;
    for (let t = 0; t < secs; t += dt) {
        env.now += dt * 1000;
        frame(ctx, env, dt);
        frames++;
        let live = 0;
        for (const m of env.state.meteors) {
            if (m.alive) {
                live++;
                let rec = open.get(m);
                if (!rec) { rec = { kind: m.kind, speed: m.speed, tail: m.tail, born: env.state.clock, dur: 0 }; open.set(m, rec); streaks.push(rec); }
                rec.dur += dt;
            } else if (open.has(m)) open.delete(m);
        }
        if (live > 0) busyFrames++;
    }
    return { streaks, births: streaks.map(s => s.born), frames, busyFrames, duty: busyFrames / frames };
}

// ---------------------------------------------------------------------------
// 1. Descriptor contract
// ---------------------------------------------------------------------------

test("registers itself with the declared blend and clear policy", () => {
    const stored = getEffect("meteors");                  // registerEffect merges into its OWN entry
    assert.ok(stored, "the module must register on import");
    assert.equal(stored.id, "meteors");
    assert.equal(stored.label, "Meteor Fall");
    assert.equal(stored.blend, "lighter", "streaks and stars must ADD light");
    assert.equal(stored.clearPolicy, "clear", "the tail is drawn explicitly; a smear would ghost it a second time");
    for (const hook of ["init", "resize", "update", "draw", "rearm"]) {
        assert.equal(stored[hook], meteors[hook], `the registry must dispatch to the module's ${hook}()`);
    }
});

test("draw() never sets its own globalCompositeOperation", () => {
    const env = testEnv(); meteors.init(env);
    const ctx = fakeCtx();
    let writes = 0;
    for (let i = 0; i < 600; i++) { env.now += 16.7; writes += frame(ctx, env, 1 / 60); }
    assert.equal(writes, 0, "compositing is the engine's job");
    assert.ok(ctx.arcs.length > 0, "the starfield must never leave the screen empty");
});

// ---------------------------------------------------------------------------
// 2. Population bounds — sparse, pooled, never growing
// ---------------------------------------------------------------------------

test("the meteor pool is fixed at 8 and the live count stays sparse", () => {
    const env = testEnv({ rand: seeded(11) });
    meteors.init(env);
    assert.equal(env.state.meteors.length, METEOR_CONFIG.pool);
    let peak = 0;
    for (let i = 0; i < 18000; i++) {                     // 300 s
        env.now += 16.7; frame(null, env, 1 / 60);
        assert.equal(env.state.meteors.length, METEOR_CONFIG.pool, "the pool must never grow at runtime");
        peak = Math.max(peak, alive(env).length);
    }
    assert.ok(peak >= 1, "no streak ever spawned in 300 s");
    assert.ok(peak <= METEOR_CONFIG.pool, `live count ${peak} exceeded the pool`);
    assert.ok(peak <= 4, `a meteor field must read as sparse; peaked at ${peak}`);
});

test("the starfield is a low, viewport-independent count with two layers", () => {
    const env = testEnv();
    meteors.init(env);
    assert.equal(env.state.stars.length, 44);
    const radii = env.state.stars.map(s => s.r).sort((a, b) => a - b);
    assert.ok(radii[0] < 0.95 && radii[radii.length - 1] > 0.95, "two sub-populations: far dust and near stars");
    const sparse = createEnv({ width: 1400, height: 800, scale: 0.2, rand: seeded(4) });
    meteors.init(sparse);
    assert.equal(sparse.state.stars.length, METEOR_CONFIG.stars.min, "density is clamped, never zero");
});

// ---------------------------------------------------------------------------
// 3. THE LEGIBILITY BUDGET (asserted against real draw calls)
// ---------------------------------------------------------------------------

test("no meteor pixel is ever drawn inside the centre 40% of the width", () => {
    const W = 1400, lo = 0.30 * W, hi = 0.70 * W;
    for (const seed of [1, 7, 42, 1234, 90210]) {
        const env = testEnv({ rand: seeded(seed) });
        meteors.init(env);
        const ctx = fakeCtx();
        for (let i = 0; i < 12000; i++) {                 // 200 s per seed
            env.now += 16.7;
            ctx.segs.length = 0;
            frame(ctx, env, 1 / 60);
            for (const s of ctx.segs) {
                for (const x of [s.x1 - s.w / 2, s.x1 + s.w / 2, s.x2 - s.w / 2, s.x2 + s.w / 2]) {
                    assert.ok(x <= lo || x >= hi, `seed ${seed}: streak pixel at x=${x.toFixed(1)} is in the reading zone`);
                }
            }
            for (const m of alive(env)) {
                const tailX = m.x - m.ux * m.tail;
                assert.ok(m.x <= lo || m.x >= hi, `head at x=${m.x.toFixed(1)} entered the reading zone`);
                assert.ok(tailX <= lo || tailX >= hi, `tail at x=${tailX.toFixed(1)} entered the reading zone`);
            }
        }
    }
});

test("stars inside the reading box are dimmed to a backdrop level", () => {
    const env = testEnv({ rand: seeded(5) });
    meteors.init(env);
    const ctx = fakeCtx();
    const W = 1400, H = 800;
    let insideSamples = 0, peakInside = 0, peakOutside = 0;
    for (let i = 0; i < 1200; i++) {
        env.now += 16.7;
        ctx.arcs.length = 0;
        frame(ctx, env, 1 / 60);
        for (const a of ctx.arcs) {
            const inBox = a.x > 0.30 * W && a.x < 0.70 * W && a.y > 0.30 * H && a.y < 0.70 * H;
            if (inBox) { insideSamples++; peakInside = Math.max(peakInside, a.alpha); }
            else peakOutside = Math.max(peakOutside, a.alpha);
        }
    }
    assert.ok(insideSamples > 0, "the starfield does cover the centre — it is just quiet there");
    assert.ok(peakInside <= 0.20, `centre-box star alpha peaked at ${peakInside}; the budget is 0.20`);
    assert.ok(peakOutside > peakInside, "stars outside the reading box are allowed to be brighter");
});

test("streaks are thin and brief — the two halves of 'you barely saw it'", () => {
    const env = testEnv({ rand: seeded(3) });
    meteors.init(env);
    const gs = env.state.gs;
    const ctx = fakeCtx();
    let widest = 0;
    const origStroke = ctx.stroke;
    ctx.stroke = function () { widest = Math.max(widest, ctx.lineWidth); origStroke.call(ctx); };
    const r = run(env, 300, 1 / 60, ctx);                 // 5 minutes
    assert.ok(widest <= 3.2 * gs, `head width ${widest.toFixed(2)} px is not a hairline (gs=${gs})`);
    const longest = Math.max(...r.streaks.map(s => s.dur));
    assert.ok(longest <= METEOR_CONFIG.life[1] + 0.05, `a streak lingered ${longest.toFixed(2)} s`);
    assert.ok(r.streaks.every(s => s.dur > 0.1), "a streak shorter than a blink is a flicker, not a meteor");
});

// ---------------------------------------------------------------------------
// 4. THE TIMING — long silences, and correlated with nothing
// ---------------------------------------------------------------------------

test("long silences dominate: low duty cycle with multi-second gaps", () => {
    const env = testEnv({ rand: seeded(21) });
    meteors.init(env);
    const r = run(env, 600);                              // 10 minutes
    assert.ok(r.births.length >= 15, `only ${r.births.length} streaks in 600 s`);
    assert.ok(r.duty < 0.15, `a streak was on screen ${(r.duty * 100).toFixed(1)}% of frames — that is a screensaver`);
    const gaps = r.births.slice(1).map((t, i) => t - r.births[i]);
    const mean = gaps.reduce((a, b) => a + b, 0) / gaps.length;
    assert.ok(Math.max(...gaps) >= 10, "there must be genuinely long silences");
    assert.ok(mean >= 8, `mean gap ${mean.toFixed(1)} s is too chatty`);
    assert.ok(gaps.some(g => g < 2), "showers exist: sometimes two arrive together");
    assert.ok(r.births[0] >= METEOR_CONFIG.first[0], "the field opens quiet, not with a streak");
});

test("the schedule reads NO engine signal — idle and busy fire identically", () => {
    const schedule = (over) => {
        const env = testEnv({ rand: seeded(77), ...over });
        meteors.init(env);
        return run(env, 300).births;
    };
    const idle = schedule({ active: false, surge: 0 });
    const busy = schedule({ active: true, surge: 1 });
    const loaded = schedule({ active: true, surge: 1, intensity: 0.4 });
    assert.ok(idle.length > 0);
    assert.deepEqual(busy, idle, "spawn timing correlated with env.active — it would read as a status light");
    assert.deepEqual(loaded, idle, "spawn timing correlated with env.surge/intensity");
});

test("rearm() — the one hook that fires on activation — never pulls a streak forward", () => {
    const env = testEnv({ rand: seeded(9) });
    meteors.init(env);
    run(env, 4);
    const before = { nextAt: env.state.nextAt, clock: env.state.clock, spawned: env.state.spawned };
    for (let i = 0; i < 50; i++) meteors.rearm(env);
    assert.equal(env.state.nextAt, before.nextAt, "rearm rescheduled the next streak");
    assert.equal(env.state.spawned, before.spawned, "rearm spawned a streak on activation");
    assert.equal(env.state.clock, before.clock);
    assert.equal(env.state.stars.length, 44, "the starfield survives rearm");
});

// ---------------------------------------------------------------------------
// 5. Motion
// ---------------------------------------------------------------------------

test("streaks fall diagonally and always away from the centre", () => {
    const env = testEnv({ rand: seeded(31) });
    meteors.init(env);
    const cx = env.width / 2;
    let checked = 0;
    for (let i = 0; i < 12000; i++) {
        env.now += 16.7;
        const before = alive(env).map(m => ({ m, x: m.x, y: m.y }));
        frame(null, env, 1 / 60);
        for (const s of before) {
            if (!s.m.alive) continue;
            assert.ok(s.m.y > s.y, "a meteor must fall");
            assert.ok(Math.abs(s.m.x - cx) > Math.abs(s.x - cx), "a meteor must travel outward, not into the text");
            assert.ok(Math.abs(s.m.x - s.x) > 1e-6, "diagonal, not vertical rain");
            checked++;
        }
    }
    assert.ok(checked > 200, `only ${checked} motion samples`);
});

test("tail length encodes speed, and both sub-populations occur", () => {
    const env = testEnv({ rand: seeded(63) });
    meteors.init(env);
    const { streaks } = run(env, 900);                    // 15 minutes
    assert.ok(streaks.length >= 20, `only ${streaks.length} distinct streaks sampled`);
    for (const s of streaks) {
        const k = s.tail / s.speed;                       // tail = v * k, capped by the edge budget
        assert.ok(s.tail > 0);
        assert.ok(k <= METEOR_CONFIG.kinds[s.kind].tailK[1] + 1e-9, `tail/speed ${k.toFixed(3)} out of band for kind ${s.kind}`);
    }
    const dust = streaks.filter(s => s.kind === 0), fire = streaks.filter(s => s.kind === 1);
    assert.ok(dust.length > 0 && fire.length > 0, "both sub-populations must occur");
    assert.ok(fire.length < dust.length, "fireballs are the rare one");
    const mean = (a) => a.reduce((x, s) => x + s.speed, 0) / a.length;
    assert.ok(mean(dust) > mean(fire), "hairline dust is the faster population");
});

test("the size-over-life envelope rises, peaks mid-flight and closes at zero", () => {
    assert.equal(envelope(0), 0);
    assert.equal(envelope(1), 0);
    let peakT = 0, peak = 0;
    for (let t = 0; t <= 1; t += 0.001) if (envelope(t) > peak) { peak = envelope(t); peakT = t; }
    assert.ok(peakT > 0.05 && peakT < 0.4, `envelope peaks at t=${peakT.toFixed(2)} — it should attack then taper`);
    assert.ok(envelope(0.02) < peak * 0.35, "no pop-in at birth");
    assert.ok(envelope(0.95) < peak * 0.15, "no cut-off at death");
});

// ---------------------------------------------------------------------------
// 6. Delta timing, dpr independence, NaN
// ---------------------------------------------------------------------------

test("motion is delta-timed exactly: displacement == velocity * dt", () => {
    const env = testEnv({ rand: seeded(12) });
    meteors.init(env);
    let checks = 0;
    for (let i = 0; i < 12000 && checks < 40; i++) {
        env.now += 16.7;
        const dt = i % 2 ? 1 / 120 : 1 / 60;              // alternate refresh rates mid-flight
        const before = alive(env).map(m => ({ m, x: m.x, y: m.y, life: m.life }));
        frame(null, env, dt);
        for (const s of before) {
            if (!s.m.alive) continue;
            assert.ok(Math.abs((s.m.x - s.x) - s.m.ux * s.m.speed * dt) < 1e-9, "x is not dt-scaled");
            assert.ok(Math.abs((s.m.y - s.y) - s.m.uy * s.m.speed * dt) < 1e-9, "y is not dt-scaled");
            assert.ok(Math.abs((s.life - s.m.life) - s.m.decay * dt) < 1e-12, "life is not dt-scaled");
            checks++;
        }
    }
    assert.ok(checks >= 40, `only ${checks} dt samples`);
});

test("60 Hz and 120 Hz reach the same state after the same wall time", () => {
    const sim = (dt, frames) => {
        const env = testEnv({ rand: seeded(55) });
        meteors.init(env);
        for (let i = 0; i < frames; i++) { env.now += dt * 1000; frame(null, env, dt); }
        return env;
    };
    const a = sim(1 / 60, 3600), b = sim(1 / 120, 7200);   // 60 s each
    assert.ok(Math.abs(a.state.clock - b.state.clock) < 1e-6, "the private clock drifted between refresh rates");
    assert.equal(a.state.spawned, b.state.spawned, "a 120 Hz display must not see more meteors");
    const span = a.width + 4;
    for (let i = 0; i < a.state.stars.length; i++) {
        const d = Math.abs(a.state.stars[i].x - b.state.stars[i].x) % span;
        assert.ok(Math.min(d, span - d) < 0.5, `star ${i} drifted ${d.toFixed(2)} px faster at 120 Hz`);
        assert.ok(Math.abs(a.state.stars[i].k - b.state.stars[i].k) < 1e-6, "twinkle is frame-rate dependent");
    }
});

test("devicePixelRatio can never touch particle geometry", () => {
    const sim = (dpr) => {
        const env = testEnv({ rand: seeded(88), dpr });
        meteors.init(env);
        for (let i = 0; i < 6000; i++) { env.now += 16.7; frame(null, env, 1 / 60); }
        return JSON.stringify(env.state);
    };
    assert.equal(sim(1), sim(3), "geometry tracked dpr — the exact bug sizing.js exists to prevent");
    // …and it DOES track the CSS box.
    const small = testEnv({ width: 420, height: 900, rand: seeded(2) });
    const big = testEnv({ width: 2400, height: 1600, rand: seeded(2) });
    meteors.init(small); meteors.init(big);
    assert.ok(big.state.gs > small.state.gs, "apparentScale must still scale with the CSS box");
});

test("no NaN anywhere after 1000 frames, at any viewport, at any frame time", () => {
    const finite = (env, where) => {
        for (const m of env.state.meteors) for (const k of Object.keys(m)) {
            if (typeof m[k] === "number") assert.ok(Number.isFinite(m[k]), `${where}: meteor.${k} = ${m[k]}`);
        }
        for (const s of env.state.stars) for (const k of Object.keys(s)) {
            if (typeof s[k] === "number") assert.ok(Number.isFinite(s[k]), `${where}: star.${k} = ${s[k]}`);
        }
        for (const k of ["clock", "nextAt", "gs"]) assert.ok(Number.isFinite(env.state[k]), `${where}: state.${k}`);
    };

    const env = testEnv({ rand: seeded(101) });
    meteors.init(env);
    const ctx = fakeCtx();
    for (let i = 0; i < 1000; i++) { env.now += 16.7; frame(ctx, env, 1 / 60); finite(env, `frame ${i}`); }

    // hostile: a zero-area canvas, a 4 s stall, a resize mid-flight
    const rough = testEnv({ width: 0, height: 0, rand: seeded(102) });
    meteors.init(rough);
    for (let i = 0; i < 600; i++) { frame(null, rough, i % 97 === 0 ? 4 : 1 / 60); finite(rough, `degenerate ${i}`); }
    rough.width = 1400; rough.height = 800;
    meteors.resize(rough);
    for (let i = 0; i < 3000; i++) { frame(ctx, rough, 1 / 60); finite(rough, `recovered ${i}`); }
    assert.ok(rough.state.spawned > 0, "the field recovers once the canvas has area");
});

test("resize keeps the starfield and retires in-flight streaks", () => {
    const env = testEnv({ rand: seeded(64) });
    meteors.init(env);
    while (alive(env).length === 0) { env.now += 16.7; frame(null, env, 1 / 60); }
    const nextAt = env.state.nextAt;
    env.width = 700; env.height = 1200;
    meteors.resize(env);
    assert.equal(alive(env).length, 0, "a streak aimed at the old width must not keep flying");
    assert.equal(env.state.stars.length, 44, "the sky does not blink out on resize");
    assert.equal(env.state.nextAt, nextAt, "a resize is not an event — the schedule is untouched");
    for (const s of env.state.stars) {
        assert.ok(s.x >= -5 && s.x <= 705, `star x=${s.x} was not rescaled into the new box`);
        assert.ok(s.y >= -5 && s.y <= 1205);
    }
});
