// Low Fog physics + LEGIBILITY BUDGET tests.
//
// Fog is the highest-risk effect in the catalogue: it is the one class that
// raises the backdrop's AVERAGE luminance uniformly, which is exactly what
// erodes body-text contrast. The budget is therefore asserted here as a
// measured property of the frames the effect actually draws — not trusted to
// the tuning table, and not trusted to a comment.
//
// Runs headlessly: randomness comes from a seeded generator injected on env and
// time from env.now/env.dt, never from Math.random()/performance.now().
//
// Run: node --test Portal/modules/fx/effects/fog.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, applyClearPolicy, applyBlend, resetBlend } from "../registry.js";
import fog, {
    MAX_VEIL_ALPHA, ALPHA_BUDGET, MIN_VEILS, MAX_VEILS, VEIL_KINDS,
    veilCount, envelope, swell,
} from "./fog.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG (same one the registry suite uses). */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Canvas2D recorder. Records the alpha and the size of every quad, and every
 *  globalCompositeOperation WRITE (proving the effect never sets its own blend).
 *  `spin:false` drops the transform API to exercise the axis-aligned fallback. */
function fakeCtx({ spin = true } = {}) {
    const quads = [];       // {alpha, w, h, x, y}
    const blendWrites = [];
    let tx = 0, ty = 0, depth = 0;
    const ctx = {
        quads, blendWrites,
        globalAlpha: 1, fillStyle: null,
        clearRect() {},
        fillRect() {},
        drawImage(_s, x, y, w, h) { quads.push({ alpha: ctx.globalAlpha, x: x + tx, y: y + ty, w, h }); },
    };
    if (spin) {
        ctx.save = () => { depth++; };
        ctx.restore = () => { depth--; tx = 0; ty = 0; };
        ctx.translate = (x, y) => { tx = x; ty = y; };
        ctx.rotate = () => {};
        Object.defineProperty(ctx, "_depth", { get: () => depth });
    }
    let gco = "source-over";
    Object.defineProperty(ctx, "globalCompositeOperation", {
        get: () => gco,
        set: (v) => { gco = v; blendWrites.push(v); },
    });
    return ctx;
}

function testEnv(over = {}) {
    return createEnv({
        width: 1400, height: 800, scale: 1, rand: seeded(42),
        sprites: { ember: [0, 1, 2, 3, 4, 5].map(i => ({ i })), star: { star: true } },
        ...over,
    });
}

/** One engine frame, exactly as ember-fx.js's loop() drives it. */
function frame(ctx, env, dt, now) {
    env.now = now;
    env.dt = dt;
    fog.update(dt, env);
    applyClearPolicy(ctx, fog, env);
    applyBlend(ctx, fog);
    const before = ctx.blendWrites.length;
    ctx.quads.length = 0;                       // this frame's quads only
    fog.draw(ctx, env);
    const wrote = ctx.blendWrites.length - before;
    resetBlend(ctx);
    return wrote;
}

// ---------------------------------------------------------------------------
// 1. Contract
// ---------------------------------------------------------------------------

test("fog declares non-emissive compositing and a hard clear", () => {
    assert.equal(fog.id, "fog");
    assert.equal(fog.label, "Low Fog");
    // 'lighter' would blow twenty overlapping veils out to white; a fade would
    // let them accumulate in the framebuffer where the alpha budget can't see it.
    assert.equal(fog.blend, "source-over");
    assert.equal(fog.clearPolicy, "clear");
});

// ---------------------------------------------------------------------------
// 2. Population bounds + sub-populations
// ---------------------------------------------------------------------------

test("population stays inside [8, 20] across every viewport the engine can produce", () => {
    // env.scale is fieldScale(): clamped to [0.4, 2.4] by ember-fx.
    for (let s = 0.4; s <= 2.4001; s += 0.1) {
        const n = veilCount({ scale: s });
        assert.ok(n >= MIN_VEILS && n <= MAX_VEILS, `scale ${s.toFixed(1)} gave ${n} veils`);
    }
    assert.equal(veilCount({ scale: 1 }), 14);
    assert.equal(veilCount({ scale: 2.4 }), MAX_VEILS, "the biggest viewport is capped, not scaled");
});

test("both sub-populations exist even at the minimum population", () => {
    for (const scale of [0.4, 1, 2.4]) {
        const env = testEnv({ scale, rand: seeded(9) });
        fog.init(env);
        const list = env.state.list;
        const banks = list.filter(p => p.kind === "bank").length;
        const wisps = list.filter(p => p.kind === "wisp").length;
        assert.equal(banks + wisps, veilCount(env));
        assert.ok(banks >= 1 && wisps >= 1, `scale ${scale}: ${banks} banks / ${wisps} wisps`);
        // The two populations must actually differ, or it is one population.
        const meanSpeed = k => list.filter(p => p.kind === k).reduce((a, p) => a + Math.abs(p.vx), 0)
            / list.filter(p => p.kind === k).length;
        assert.ok(meanSpeed("wisp") > meanSpeed("bank") * 1.5,
            "wisps must be visibly faster than banks — the speed difference IS the parallax");
        const meanR = k => list.filter(p => p.kind === k).reduce((a, p) => a + p.rx, 0)
            / list.filter(p => p.kind === k).length;
        assert.ok(meanR("bank") > meanR("wisp") * 1.5, "banks must be visibly larger than wisps");
    }
});

test("veils are enormous — the brief's 400-900px quads, not particles", () => {
    const env = testEnv({ rand: seeded(4) });
    fog.init(env);
    for (const p of env.state.list) {
        const dia = p.rx * 2;
        assert.ok(dia >= 180 && dia <= 900, `veil diameter ${dia.toFixed(0)}px is outside 180-900`);
        assert.ok(p.ry < p.rx, "the quad must be elliptical or the slow rotation is invisible");
    }
});

// ---------------------------------------------------------------------------
// 3. THE LEGIBILITY BUDGET (the assertion this file exists for)
// ---------------------------------------------------------------------------

test("no tuning value can exceed the hard 6% ceiling", () => {
    assert.equal(MAX_VEIL_ALPHA, 0.06);
    for (const [name, k] of Object.entries(VEIL_KINDS)) {
        assert.ok(k.alpha[1] <= MAX_VEIL_ALPHA, `${name} tops out at ${k.alpha[1]}, above the ceiling`);
    }
});

test("every drawn quad is under 6% alpha and the FIELD sum stays inside the budget — 1000 frames", () => {
    // Worst realistic case: the largest viewport, so the population is capped at 20.
    const env = testEnv({ width: 2560, height: 1440, scale: 2.4, rand: seeded(17) });
    fog.init(env);
    assert.equal(env.state.list.length, MAX_VEILS);
    const ctx = fakeCtx();
    let peakQuad = 0, peakSum = 0, frames = 0, quadsDrawn = 0;
    for (let f = 1; f <= 1000; f++) {
        frame(ctx, env, 1 / 60, f * (1000 / 60));
        assert.equal(ctx.quads.length, MAX_VEILS, `frame ${f} drew ${ctx.quads.length} quads`);
        let sum = 0;
        for (const q of ctx.quads) {
            assert.ok(q.alpha <= MAX_VEIL_ALPHA, `frame ${f}: quad alpha ${q.alpha} broke the 6% ceiling`);
            assert.ok(q.alpha >= 0, `frame ${f}: negative alpha ${q.alpha}`);
            sum += q.alpha;
            if (q.alpha > peakQuad) peakQuad = q.alpha;
            quadsDrawn++;
        }
        assert.ok(sum <= ALPHA_BUDGET + 1e-9, `frame ${f}: field alpha sum ${sum} broke the budget`);
        if (sum > peakSum) peakSum = sum;
        frames++;
    }
    assert.equal(frames, 1000);
    assert.equal(quadsDrawn, 20000, "20 quads x 1000 frames — the whole per-frame cost");
    // The budget must actually ENGAGE at full population, or it is decoration.
    assert.ok(peakSum > ALPHA_BUDGET * 0.95, `budget never bit: peak sum was only ${peakSum}`);
    assert.ok(peakQuad < MAX_VEIL_ALPHA, `operating point should sit under the ceiling, hit ${peakQuad}`);
});

test("the envelope and the size curve stay bounded across a whole life", () => {
    for (let life = 1; life >= -0.001; life -= 0.001) {
        const e = envelope(life);
        assert.ok(e >= 0 && e <= 1 && Number.isFinite(e), `envelope(${life}) = ${e}`);
        const s = swell(1 - life);
        assert.ok(s >= 0.62 && s <= 1.0000001, `swell at life ${life} = ${s}`);
    }
    assert.ok(envelope(1) < 0.01, "a newborn veil must fade IN, never pop");
    assert.ok(envelope(0) < 0.01, "a dying veil must fade OUT, never pop");
    assert.ok(envelope(0.6) > 0.99, "mid-life is full strength");
});

// ---------------------------------------------------------------------------
// 4. Motion
// ---------------------------------------------------------------------------

test("every veil drifts with the one prevailing wind, at its own speed", () => {
    const env = testEnv({ rand: seeded(23) });
    fog.init(env);
    const wind = env.state.wind;
    assert.ok(wind === 1 || wind === -1);
    const x0 = env.state.list.map(p => p.x);
    const ctx = fakeCtx();
    for (let f = 1; f <= 30; f++) frame(ctx, env, 1 / 60, f * (1000 / 60));
    env.state.list.forEach((p, i) => {
        assert.equal(Math.sign(p.vx), wind, "a counter-drifting veil would break the weather illusion");
        assert.ok(Math.sign(p.x - x0[i]) === wind, `veil ${i} did not move downwind`);
    });
    const speeds = env.state.list.map(p => Math.abs(p.vx));
    assert.ok(Math.max(...speeds) > Math.min(...speeds) * 2, "speeds must spread, or there is no depth");
});

test("motion is delta-timed: 120 Hz lands where 60 Hz lands", () => {
    const run = (hz) => {
        const env = testEnv({ rand: seeded(31) });
        fog.init(env);
        const ctx = fakeCtx();
        for (let f = 1; f <= hz; f++) frame(ctx, env, 1 / hz, f * (1000 / hz));
        return env.state.list.map(p => ({ x: p.x, y: p.y, rot: p.rot, life: p.life }));
    };
    const a = run(60), b = run(120);
    assert.equal(a.length, b.length);
    a.forEach((p, i) => {
        assert.ok(Math.abs(p.x - b[i].x) < 0.5, `veil ${i} drifted ${Math.abs(p.x - b[i].x).toFixed(2)}px apart over 1s`);
        assert.ok(Math.abs(p.y - b[i].y) < 0.5, `veil ${i} y diverged`);
        assert.ok(Math.abs(p.rot - b[i].rot) < 0.01, `veil ${i} rotation diverged`);
        assert.ok(Math.abs(p.life - b[i].life) < 1e-6, `veil ${i} aged at a different rate`);
    });
});

test("a veil never clips at an edge: it wraps by its OWN radius", () => {
    const env = testEnv({ rand: seeded(8) });
    fog.init(env);
    const ctx = fakeCtx();
    for (let f = 1; f <= 2000; f++) {
        frame(ctx, env, 1 / 60, f * (1000 / 60));
        for (const p of env.state.list) {
            const mx = p.rx * env.state.geo, my = p.ry * env.state.geo;
            assert.ok(p.x >= -mx - 1 && p.x <= env.width + mx + 1, `frame ${f}: x ${p.x} escaped`);
            assert.ok(p.y >= -my - 1 && p.y <= env.height + my + 1, `frame ${f}: y ${p.y} escaped`);
        }
    }
});

// ---------------------------------------------------------------------------
// 5. Numerical health + the hot path
// ---------------------------------------------------------------------------

test("no NaN anywhere after 1000 frames, including the drawn geometry", () => {
    const env = testEnv({ rand: seeded(77) });
    fog.init(env);
    const ctx = fakeCtx();
    for (let f = 1; f <= 1000; f++) {
        frame(ctx, env, 1 / 60, f * (1000 / 60));
        for (const p of env.state.list) {
            for (const key of ["x", "y", "rx", "ry", "rot", "life", "a", "vx", "vy", "hue0"]) {
                assert.ok(Number.isFinite(p[key]), `frame ${f}: ${p.kind}.${key} = ${p[key]}`);
            }
        }
        for (const q of ctx.quads) {
            assert.ok(Number.isFinite(q.w) && q.w > 0, `frame ${f}: quad width ${q.w}`);
            assert.ok(Number.isFinite(q.h) && q.h > 0, `frame ${f}: quad height ${q.h}`);
            assert.ok(Number.isFinite(q.alpha), `frame ${f}: quad alpha ${q.alpha}`);
        }
    }
    assert.ok(Number.isFinite(env.state.budgetK) && env.state.budgetK > 0 && env.state.budgetK <= 1);
});

test("the pool is allocated once — veils are recycled in place, never re-created", () => {
    const env = testEnv({ rand: seeded(55) });
    fog.init(env);
    const identities = env.state.list.slice();
    const ctx = fakeCtx();
    let recycled = 0;
    const lives = identities.map(p => p.life);
    for (let f = 1; f <= 1000; f++) {
        frame(ctx, env, 1 / 60, f * (1000 / 60));
        env.state.list.forEach((p, i) => { if (p.life > lives[i]) recycled++; lives[i] = p.life; });
    }
    assert.equal(env.state.list.length, identities.length, "the pool must not grow");
    env.state.list.forEach((p, i) => {
        assert.equal(p, identities[i], `veil ${i} was re-allocated instead of reseeded`);
    });
    assert.ok(recycled > 0, "1000 frames should have recycled at least one veil");
});

test("draw never sets its own globalCompositeOperation", () => {
    const env = testEnv({ rand: seeded(12) });
    fog.init(env);
    const ctx = fakeCtx();
    let writes = 0;
    for (let f = 1; f <= 5; f++) writes += frame(ctx, env, 1 / 60, f * (1000 / 60));
    assert.equal(writes, 0, "compositing is the engine's job");
    assert.ok(ctx.quads.length > 0, "fog drew nothing");
});

test("save/restore is balanced, and the axis-aligned fallback still draws", () => {
    const env = testEnv({ rand: seeded(13) });
    fog.init(env);
    const spun = fakeCtx();
    frame(spun, env, 1 / 60, 16.7);
    assert.equal(spun._depth, 0, "unbalanced save/restore would leak the engine's transform");
    const flat = fakeCtx({ spin: false });          // recorder without the transform API
    frame(flat, env, 1 / 60, 33.4);
    assert.equal(flat.quads.length, env.state.list.length, "the fallback path must still blit every veil");
});

// ---------------------------------------------------------------------------
// 6. The shipped bug: devicePixelRatio must never touch geometry
// ---------------------------------------------------------------------------

test("quad geometry is identical at dpr 1 and dpr 3 for the same CSS box", () => {
    const run = (dpr) => {
        const env = testEnv({ dpr, rand: seeded(99) });
        fog.init(env);
        const ctx = fakeCtx();
        for (let f = 1; f <= 60; f++) frame(ctx, env, 1 / 60, f * (1000 / 60));
        return ctx.quads.map(q => `${q.w.toFixed(4)}x${q.h.toFixed(4)}@${q.alpha.toFixed(6)}`).join("|");
    };
    assert.equal(run(1), run(3), "devicePixelRatio leaked into particle geometry — the shipped bug");
});

test("apparent size tracks the CSS short edge, and the state is reproducible from a seed", () => {
    const small = testEnv({ width: 420, height: 900, rand: seeded(5) });
    fog.init(small);
    const big = testEnv({ width: 2200, height: 1400, rand: seeded(5) });
    fog.init(big);
    assert.ok(big.state.geo > small.state.geo, "a larger CSS box scales the veils up");

    const snap = (seed) => {
        const env = testEnv({ rand: seeded(seed) });
        fog.init(env);
        const ctx = fakeCtx();
        for (let f = 1; f <= 10; f++) frame(ctx, env, 1 / 60, f * (1000 / 60));
        return JSON.stringify(env.state.list);
    };
    assert.equal(snap(3), snap(3), "fog is not reproducible from a seeded rand — Math.random leaked in");
});

// ---------------------------------------------------------------------------
// 7. Lifecycle
// ---------------------------------------------------------------------------

test("resize retargets the population without restarting the weather", () => {
    const env = testEnv({ scale: 0.4, rand: seeded(21) });
    fog.init(env);
    const before = env.state.list.slice(0, 3);
    const n0 = env.state.list.length;
    env.width = 2560; env.height = 1440; env.scale = 2.4;
    fog.resize(env);
    assert.ok(env.state.list.length > n0, "a bigger viewport gets more veils");
    assert.equal(env.state.list.length, MAX_VEILS);
    before.forEach((p, i) => assert.equal(env.state.list[i], p, "existing veils must keep drifting"));
    env.width = 420; env.height = 900; env.scale = 0.4;
    fog.resize(env);
    assert.equal(env.state.list.length, veilCount(env));
});

test("rearm keeps the weather running instead of re-seeding it", () => {
    const env = testEnv({ rand: seeded(6) });
    fog.init(env);
    const identities = env.state.list.slice();
    fog.rearm(env);
    env.state.list.forEach((p, i) => assert.equal(p, identities[i], "fog is continuous; rearm must not rebuild"));
    const fresh = testEnv({ rand: seeded(6) });
    fog.rearm(fresh);                                  // never initialised → rearm must build it
    assert.equal(fresh.state.list.length, veilCount(fresh));
});

test("update before init self-heals instead of throwing", () => {
    const env = testEnv({ rand: seeded(2) });
    const ctx = fakeCtx();
    frame(ctx, env, 1 / 60, 16.7);
    assert.equal(env.state.list.length, veilCount(env));
});
