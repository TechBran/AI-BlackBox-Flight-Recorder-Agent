// Effect-registry tests — the seam that replaced a hard-coded 3-entry dispatch
// table and a mode whitelist that was written out THREE times (loadParticleMode,
// setParticleMode, window.EmberFX) and free to drift between the copies.
//
// These run headlessly: every effect takes its randomness and its clock from the
// injected `env`, never from Math.random()/performance.now(), so the physics can
// be driven with a seeded generator and a fake canvas context.
//
// Run: node --test Portal/modules/fx/registry.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
    EFFECTS, DEFAULT_EFFECT_ID, registerEffect, getEffect, hasEffect, effectIds,
    effectList, resolveEffectId, loadEffect, createEnv, parseClearPolicy,
    applyClearPolicy, applyBlend, resetBlend,
} from "./registry.js";
import "./effects/index.js";   // the catalogue: stubs only, no simulation code yet

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG so effect physics is reproducible across runs. */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Minimal Canvas2D recorder. Tracks every globalCompositeOperation WRITE,
 *  which is how we prove effects never set their own blend mode. */
function fakeCtx() {
    const calls = [];
    const blendWrites = [];
    const ctx = {
        calls, blendWrites,
        globalAlpha: 1, fillStyle: null, font: null, textBaseline: null,
        clearRect: (...a) => calls.push(["clearRect", ...a]),
        fillRect: (...a) => calls.push(["fillRect", ...a]),
        fillText: (...a) => calls.push(["fillText", ...a]),
        drawImage: (...a) => calls.push(["drawImage", ...a.slice(1)]),
        beginPath: () => calls.push(["beginPath"]),
        arc: (...a) => calls.push(["arc", ...a]),
        fill: () => calls.push(["fill"]),
        createRadialGradient: () => ({ addColorStop() {} }),
    };
    let gco = "source-over";
    Object.defineProperty(ctx, "globalCompositeOperation", {
        get: () => gco,
        set: (v) => { gco = v; blendWrites.push(v); },
    });
    return ctx;
}

/** One engine frame, exactly as ember-fx.js's loop() drives it. */
function frame(ctx, effect, env, dt) {
    env.dt = dt;
    effect.update(dt, env);
    applyClearPolicy(ctx, effect, env);
    applyBlend(ctx, effect);
    const before = ctx.blendWrites.length;
    effect.draw(ctx, env);
    const effectBlendWrites = ctx.blendWrites.length - before;
    resetBlend(ctx);
    return effectBlendWrites;
}

function testEnv(over = {}) {
    return createEnv({
        width: 1400, height: 800, scale: 1, rand: seeded(42),
        sprites: { ember: [0, 1, 2, 3, 4, 5].map(i => ({ i })), star: {} },
        ...over,
    });
}

// ---------------------------------------------------------------------------
// 1. Registration
// ---------------------------------------------------------------------------

test("registerEffect stores the descriptor and fills in defaults", () => {
    const d = registerEffect({ id: "__t_reg", label: "Test", update() {}, draw() {} });
    try {
        assert.equal(getEffect("__t_reg"), d);
        assert.equal(d.blend, "source-over", "blend defaults to source-over, never additive by accident");
        assert.equal(d.clearPolicy, "clear");
        assert.equal(typeof d.init, "function", "missing hooks are filled with no-ops so the engine never branches");
        assert.equal(typeof d.resize, "function");
        assert.equal(typeof d.rearm, "function");
    } finally { EFFECTS.delete("__t_reg"); }
});

test("registerEffect merges, so a lazy module can complete its own catalogue stub", () => {
    registerEffect({ id: "__t_merge", label: "Stub", load: () => Promise.resolve() });
    registerEffect({ id: "__t_merge", blend: "lighter", clearPolicy: "none", update() {} });
    try {
        const d = getEffect("__t_merge");
        assert.equal(d.label, "Stub", "metadata from the stub survives");
        assert.equal(d.blend, "lighter", "the module wins for the fields it declares");
        assert.equal(d.clearPolicy, "none");
    } finally { EFFECTS.delete("__t_merge"); }
});

test("a typo'd clearPolicy or blend fails at REGISTRATION, not at 60fps", () => {
    assert.throws(() => registerEffect({ id: "__t_bad", clearPolicy: "fadey" }), /invalid clearPolicy/);
    assert.throws(() => registerEffect({ id: "__t_bad", clearPolicy: "fade:" }), /invalid clearPolicy/);
    assert.throws(() => registerEffect({ id: "__t_bad", blend: "lighten" }), /unsupported blend/);
    assert.throws(() => registerEffect({}), /non-empty string/);
    assert.equal(hasEffect("__t_bad"), false, "a rejected descriptor must not be half-registered");
});

// ---------------------------------------------------------------------------
// 2. Unknown-id fallback
// ---------------------------------------------------------------------------

test("an unknown id falls back to the shipped default", () => {
    // Ledger Rain is the default (Brandon 2026-07-25: "it fits the black box
    // aesthetic perfectly"). Asserted explicitly so a catalogue reorder cannot
    // silently change what a fresh install opens on — the default is a decision,
    // not whatever sits first in the list. Everything else here asserts the
    // FALLBACK RELATIONSHIP against DEFAULT_EFFECT_ID rather than a literal, so
    // changing the default again stays a one-line edit.
    assert.equal(DEFAULT_EFFECT_ID, "ledger");
    assert.equal(resolveEffectId("nope"), DEFAULT_EFFECT_ID);
    assert.equal(resolveEffectId(undefined), DEFAULT_EFFECT_ID);
    assert.equal(resolveEffectId(null), DEFAULT_EFFECT_ID);
    assert.equal(resolveEffectId("matrix"), "matrix", "a known id is passed through untouched");
    assert.equal(resolveEffectId("stars"), "stars", "a known id is passed through untouched");
});

test("loadEffect on an unknown id resolves the fallback, not a rejection", async () => {
    const d = await loadEffect("does-not-exist");
    assert.equal(d.id, DEFAULT_EFFECT_ID);
});

// ---------------------------------------------------------------------------
// 3. clearPolicy is applied BY THE ENGINE
// ---------------------------------------------------------------------------

test("parseClearPolicy understands the three forms", () => {
    assert.deepEqual(parseClearPolicy("clear"), { kind: "clear", color: null });
    assert.deepEqual(parseClearPolicy("none"), { kind: "none", color: null });
    assert.deepEqual(parseClearPolicy("fade:rgba(8,6,10,0.20)"), { kind: "fade", color: "rgba(8,6,10,0.20)" });
});

test("clearPolicy 'clear' clears the whole CSS box", () => {
    const ctx = fakeCtx();
    const env = testEnv();
    applyClearPolicy(ctx, { clearPolicy: "clear" }, env);
    assert.deepEqual(ctx.calls, [["clearRect", 0, 0, 1400, 800]]);
});

test("clearPolicy 'fade:<rgba>' paints the veil under source-over", () => {
    const ctx = fakeCtx();
    ctx.globalCompositeOperation = "lighter";           // leftover from a previous effect
    applyClearPolicy(ctx, { clearPolicy: "fade:rgba(4,7,5,0.085)" }, testEnv());
    assert.equal(ctx.fillStyle, "rgba(4,7,5,0.085)");
    assert.deepEqual(ctx.calls, [["fillRect", 0, 0, 1400, 800]]);
    assert.equal(ctx.globalCompositeOperation, "source-over",
        "a fade drawn under 'lighter' would ADD light instead of veiling it");
    assert.equal(ctx.globalAlpha, 1, "a stale globalAlpha would make the fade the wrong strength");
});

test("clearPolicy 'none' touches nothing", () => {
    const ctx = fakeCtx();
    applyClearPolicy(ctx, { clearPolicy: "none" }, testEnv());
    assert.deepEqual(ctx.calls, []);
    assert.deepEqual(ctx.blendWrites, []);
});

test("the three shipped effects declare the clear policies they used to hand-roll", async () => {
    const [stars, embers, matrix] = await Promise.all(["stars", "embers", "matrix"].map(loadEffect));
    assert.equal(stars.clearPolicy, "clear");
    assert.equal(embers.clearPolicy, "fade:rgba(8,6,10,0.20)");   // heat-persistence smear
    assert.equal(matrix.clearPolicy, "fade:rgba(4,7,5,0.085)");   // green-black trail smear
});

// ---------------------------------------------------------------------------
// 4. blend is PER-EFFECT
// ---------------------------------------------------------------------------

test("blend is per-effect and applied by the engine", async () => {
    const [stars, embers, matrix] = await Promise.all(["stars", "embers", "matrix"].map(loadEffect));
    // Additive is correct for the emissive field and WRONG for the others —
    // snow/fog/bokeh would blow out to white and destroy the chat text contrast.
    assert.equal(embers.blend, "lighter");
    assert.equal(matrix.blend, "source-over");
    assert.equal(stars.blend, "source-over", "the approved Rising Stars look composites source-over");

    const ctx = fakeCtx();
    applyBlend(ctx, embers);
    assert.equal(ctx.globalCompositeOperation, "lighter");
    applyBlend(ctx, matrix);
    assert.equal(ctx.globalCompositeOperation, "source-over");
    applyBlend(ctx, {});
    assert.equal(ctx.globalCompositeOperation, "source-over", "a blend-less descriptor is never additive");
});

test("no effect sets its own globalCompositeOperation", async () => {
    for (const id of ["stars", "embers", "matrix"]) {
        const eff = await loadEffect(id);
        const env = testEnv();
        env.now = 0;
        eff.init(env);
        const ctx = fakeCtx();
        let writes = 0;
        for (let f = 1; f <= 3; f++) { env.now = f * 16.7; writes += frame(ctx, eff, env, 1 / 60); }
        assert.equal(writes, 0, `${id}.draw() wrote globalCompositeOperation — that is the engine's job`);
        assert.ok(ctx.calls.length > 0, `${id} drew nothing`);
    }
});

test("resetBlend hands the context back neutral", () => {
    const ctx = fakeCtx();
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = 0.2;
    resetBlend(ctx);
    assert.equal(ctx.globalCompositeOperation, "source-over");
    assert.equal(ctx.globalAlpha, 1);
});

// ---------------------------------------------------------------------------
// 5. The whitelist derives from the registry
// ---------------------------------------------------------------------------

test("the catalogue is complete and synchronous BEFORE any module is imported", () => {
    // Persistence and the settings picker both need the id/label list at page
    // load; the simulations must not be fetched until one is selected.
    //
    // Asserts the INVARIANT, not a frozen list. Pinning the exact ids here would
    // mean "adding an effect is one line" is a lie — every new effect would also
    // have to edit this test, which is the coupling the registry exists to
    // delete. What must hold is: the three shipped fields are present, the
    // default resolves, every entry is well-formed, and nothing is a duplicate.
    const ids = effectIds();
    for (const shipped of ["stars", "embers", "matrix"]) {
        assert.ok(ids.includes(shipped), `shipped field ${shipped} missing from the catalogue`);
    }
    assert.ok(ids.includes(DEFAULT_EFFECT_ID), "the default effect must be registered");
    assert.equal(new Set(ids).size, ids.length, "duplicate effect ids in the catalogue");

    for (const entry of effectList()) {
        assert.equal(typeof entry.id, "string");
        assert.ok(entry.id.length, "every catalogue entry needs an id");
        assert.equal(typeof entry.label, "string");
        assert.ok(entry.label.length, `effect ${entry.id} has no label for the picker`);
        // The label must be available WITHOUT loading the module — that is the
        // whole point of the stub.
        assert.ok(getEffect(entry.id).label, `effect ${entry.id} label is not synchronous`);
    }
});

test("registering an effect is the ONLY step needed to join the whitelist", () => {
    assert.equal(hasEffect("__t_new"), false);
    registerEffect({ id: "__t_new", label: "New", update() {}, draw() {} });
    try {
        assert.equal(hasEffect("__t_new"), true);
        assert.ok(effectIds().includes("__t_new"));
        assert.ok(effectList().some(e => e.id === "__t_new" && e.label === "New"));
        assert.equal(resolveEffectId("__t_new"), "__t_new");
    } finally { EFFECTS.delete("__t_new"); }
});

test("ember-fx.js hard-codes no mode list of its own", () => {
    // The triplication this refactor deleted: loadParticleMode, setParticleMode
    // and the window.EmberFX surface each carried their own copy of
    // ('stars'|'embers'|'matrix'). Any quoted effect id reappearing in the
    // ENGINE means a fourth copy is being born — including inside a comment,
    // which is why this check is deliberately blunt.
    const src = readFileSync(fileURLToPath(new URL("../ember-fx.js", import.meta.url)), "utf8");
    const stray = src.match(/'(stars|embers|matrix)'/g);
    assert.equal(stray, null, `ember-fx.js must ask the registry, found: ${stray}`);
    assert.ok(/hasEffect\(/.test(src), "the whitelist must go through registry.hasEffect()");
    assert.ok(/loadEffect\(/.test(src), "effects must be lazily imported through the registry");
    assert.equal(/import\(/.test(src), false, "ember-fx.js must not import effect modules directly");
});

test("each effect module completes its own stub with a matching label", async () => {
    for (const { id, label } of effectList()) {
        const d = await loadEffect(id);
        assert.equal(d.id, id);
        assert.equal(d.label, label, `catalogue label for '${id}' disagrees with the module's`);
        assert.equal(typeof d.load, "function", "the stub loader stays for a later cold reload");
    }
});

test("loadEffect memoizes — a repeated selection does not re-import", async () => {
    let hits = 0;
    registerEffect({ id: "__t_lazy", label: "Lazy", load: () => { hits++; return Promise.resolve(); } });
    try {
        await Promise.all([loadEffect("__t_lazy"), loadEffect("__t_lazy")]);
        await loadEffect("__t_lazy");
        assert.equal(hits, 1);
    } finally { EFFECTS.delete("__t_lazy"); }
});

// ---------------------------------------------------------------------------
// 6. The injected env is a real test seam (effect physics, headless)
// ---------------------------------------------------------------------------

test("createEnv injects rand and a clock instead of reaching for globals", () => {
    const env = createEnv();
    assert.equal(typeof env.rand, "function");
    assert.equal(typeof env.clock, "function");
    assert.ok(env.clock() > 0);
    const mine = createEnv({ rand: () => 0.5, clock: () => 1234 });
    assert.equal(mine.rand(), 0.5);
    assert.equal(mine.clock(), 1234);
    assert.deepEqual(mine.state, {}, "each effect gets its own scratch space");
});

test("effect state is reproducible from a seeded rand — no Math.random inside", async () => {
    const run = async (id) => {
        const eff = await loadEffect(id);
        const env = testEnv({ rand: seeded(7) });
        eff.init(env);
        const ctx = fakeCtx();
        for (let f = 1; f <= 5; f++) { env.now = f * 16.7; frame(ctx, eff, env, 1 / 60); }
        return JSON.stringify(env.state);
    };
    for (const id of ["stars", "matrix", "embers"]) {
        assert.equal(await run(id), await run(id), `${id} is not reproducible from a seeded rand`);
    }
});

test("stars: the field rises and the 60Hz normalizer is dt-independent", async () => {
    const stars = await loadEffect("stars");
    const env = testEnv({ rand: seeded(3) });
    stars.init(env);
    assert.equal(env.state.sim.particles.length, 120, "40 far + 50 mid + 30 fore, the approved layer counts");

    const y0 = env.state.sim.particles.map(p => p.y);
    const ctx = fakeCtx();
    for (let f = 1; f <= 60; f++) { env.now = f * 16.7; frame(ctx, stars, env, 1 / 60); }
    const risen = env.state.sim.particles.filter((p, i) => p.y < y0[i]).length;
    assert.ok(risen > 100, `stars must rise; only ${risen}/120 moved up`);

    // 120 Hz for twice as many frames must land in the same place as 60 Hz —
    // that is the whole point of dt60, and the reason the approved look is
    // preserved exactly at 60 fps (dt60 === 1).
    const at = (dt, frames) => {
        const e = testEnv({ rand: seeded(3) });
        stars.init(e);
        const c = fakeCtx();
        for (let f = 1; f <= frames; f++) { e.now = f * dt * 1000; frame(c, stars, e, dt); }
        return e.state.sim.particles[0].y;
    };
    assert.ok(Math.abs(at(1 / 60, 60) - at(1 / 120, 120)) < 1.0,
        "60 Hz and 120 Hz must travel the same distance in one second");
});

test("matrix: columns fall, wrap and follow the width", async () => {
    const matrix = await loadEffect("matrix");
    const env = testEnv({ rand: seeded(11) });
    matrix.init(env);
    assert.equal(env.state.size, 18, "glyph size is width/78 clamped to [13,22]");
    assert.equal(env.state.cols.length, Math.ceil(1400 / 18));
    const y0 = env.state.cols.map(c => c.y);
    const ctx = fakeCtx();
    for (let f = 1; f <= 30; f++) { env.now = f * 16.7; frame(ctx, matrix, env, 1 / 60); }
    assert.ok(env.state.cols.every((c, i) => c.y > y0[i]), "every column must fall");

    env.width = 400;                       // a narrow phone: the clamp floors the glyph
    matrix.resize(env);
    assert.equal(env.state.size, 13);
    assert.equal(env.state.cols.length, Math.ceil(400 / 13));
});

test("embers: population builds while active, stops when idle, drains on rearm", async () => {
    const embers = await loadEffect("embers");
    const env = testEnv({ rand: seeded(5) });
    embers.init(env);
    assert.equal(env.state.list.length, 140, "seeded at 140 * scale");

    const ctx = fakeCtx();
    for (let f = 1; f <= 120; f++) { env.now = f * 16.7; frame(ctx, embers, env, 1 / 60); }
    const live = env.state.list.length;
    assert.ok(live > 100 && live < 260, `population should settle near the target, got ${live}`);

    env.active = false;                    // generation ended: no more spawning
    for (let f = 121; f <= 400; f++) { env.now = f * 16.7; frame(ctx, embers, env, 1 / 60); }
    assert.ok(env.state.list.length < live, "the field must drain once generation stops");

    // Activation semantics carried over verbatim from the shipped engine: the
    // field starts EMPTY on each new turn and builds back up.
    embers.rearm(env);
    assert.equal(env.state.list.length, 0);
    assert.equal(env.state.acc, 0);
});

test("embers stays inside the horizontal wrap band", async () => {
    const embers = await loadEffect("embers");
    const env = testEnv({ rand: seeded(9) });
    embers.init(env);
    const ctx = fakeCtx();
    for (let f = 1; f <= 200; f++) { env.now = f * 16.7; frame(ctx, embers, env, 1 / 60); }
    for (const p of env.state.list) {
        assert.ok(p.x >= -20 && p.x <= env.width + 20, `ember escaped horizontally at x=${p.x}`);
        assert.ok(p.life > 0, "dead embers must be culled");
    }
});

test("an effect draws nothing rather than throwing when the sprite atlas is absent", async () => {
    // Headless / pre-bake: env.sprites is null. The engine must not need a
    // try/catch around draw().
    const embers = await loadEffect("embers");
    const env = testEnv({ sprites: null });
    embers.init(env);
    const ctx = fakeCtx();
    frame(ctx, embers, env, 1 / 60);
    assert.deepEqual(ctx.calls, [["fillRect", 0, 0, 1400, 800]], "only the engine's fade ran");
});
