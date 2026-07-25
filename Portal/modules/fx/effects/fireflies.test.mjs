// Fireflies physics tests — headless, seeded, no DOM.
//
// The effect takes its randomness and its clock from the injected `env`, so the
// whole simulation is drivable from node with a deterministic PRNG and a fake
// Canvas2D. What is asserted here is what would otherwise only be caught by
// staring at the screen: that the population stays in its bounds, that the
// pulses never synchronise, that the field has no upward bias, that the path
// curves instead of jittering, and — the one that protects the product rather
// than the effect — that the LEGIBILITY BUDGET holds, because this field lives
// behind chat text.
//
// Run: node --test Portal/modules/fx/effects/fireflies.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, applyClearPolicy, applyBlend, resetBlend, getEffect } from "../registry.js";
import fireflies, { KINDS, MIN_POP, MAX_POP, pulse, sizeOverLife } from "./fireflies.js";
import embers from "./embers.js";

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Deterministic PRNG (mulberry32) — same one registry.test.mjs uses. */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Canvas2D recorder. Captures globalAlpha AT THE MOMENT of each drawImage —
 *  that is the quantity the legibility budget is measured from. */
function fakeCtx() {
    const draws = [];         // {alpha, w, h}
    const blendWrites = [];
    const ctx = {
        draws, blendWrites,
        globalAlpha: 1, fillStyle: null,
        clearRect() {}, fillRect() {},
        drawImage: (_s, _x, _y, w, h) => draws.push({ alpha: ctx.globalAlpha, w, h }),
    };
    let gco = "source-over";
    Object.defineProperty(ctx, "globalCompositeOperation", {
        get: () => gco,
        set: (v) => { gco = v; blendWrites.push(v); },
    });
    return ctx;
}

const ATLAS = { ember: [0, 1, 2, 3, 4, 5].map(i => ({ i })), star: { star: true } };

function testEnv(over = {}) {
    return createEnv({ width: 1400, height: 800, scale: 1, rand: seeded(42), sprites: ATLAS, ...over });
}

/** One engine frame, exactly as ember-fx.js's loop() drives it. */
function frame(ctx, eff, env, dt) {
    env.dt = dt;
    eff.update(dt, env);
    applyClearPolicy(ctx, eff, env);
    applyBlend(ctx, eff);
    ctx.draws.length = 0;
    eff.draw(ctx, env);
    resetBlend(ctx);
}

function run(eff, env, frames, dt = 1 / 60, ctx = fakeCtx()) {
    eff.init(env);
    for (let f = 1; f <= frames; f++) { env.now = f * dt * 1000; frame(ctx, eff, env, dt); }
    return ctx;
}

// ---------------------------------------------------------------------------
// 1. Catalogue contract
// ---------------------------------------------------------------------------

/** Share of the population sitting in the busiest quarter of the pulse cycle.
 *  1.0 = the whole field breathes as one (the cheap-CSS-demo failure); 0.25 =
 *  perfectly spread. Robust to the seeded PRNG's stride correlations in a way a
 *  bucket-occupancy count is not. */
function worstQuarterShare(list) {
    const q = [0, 0, 0, 0];
    for (const p of list) q[Math.min(3, Math.floor((p.t / p.period) * 4))]++;
    return Math.max(...q) / list.length;
}

test("fireflies registers with the compositing the engine will apply", () => {
    // The registry stores a MERGED descriptor, so compare what the engine reads.
    const stored = getEffect("fireflies");
    assert.equal(stored.id, "fireflies");
    assert.equal(stored.blend, fireflies.blend);
    assert.equal(stored.clearPolicy, fireflies.clearPolicy);
    assert.equal(stored.update, fireflies.update);
    assert.equal(fireflies.label, "Fireflies");
    assert.equal(fireflies.blend, "lighter", "emissive: overlapping glows add");
    assert.equal(fireflies.clearPolicy, "clear", "a fade would smear paths and raise the floor behind the text");
});

// ---------------------------------------------------------------------------
// 2. Population bounds — the effect's entire premise
// ---------------------------------------------------------------------------

test("population stays inside 15..40 across the engine's whole scale range", () => {
    // fieldScale() is clamped to [0.4, 2.4] by the engine; check past both ends.
    for (const scale of [0.1, 0.4, 0.7, 1, 1.5, 2.4, 6]) {
        const env = testEnv({ scale, rand: seeded(3) });
        fireflies.init(env);
        const n = env.state.list.length;
        assert.ok(n >= MIN_POP && n <= MAX_POP, `scale ${scale} produced ${n} fireflies`);
    }
    assert.equal(testEnvPop(1), 24, "a 1440x900-ish viewport gets the tuned 24");
    function testEnvPop(scale) {
        const env = testEnv({ scale });
        fireflies.init(env);
        return env.state.list.length;
    }
});

test("both sub-populations exist even at the minimum population", () => {
    const env = testEnv({ scale: 0.1, rand: seeded(8) });     // clamps to MIN_POP
    fireflies.init(env);
    const sparks = env.state.list.filter(p => p.spark).length;
    const lamps = env.state.list.length - sparks;
    assert.equal(env.state.list.length, MIN_POP);
    assert.equal(sparks, 5, "every third slot is a spark — deterministic, not a coin flip");
    assert.equal(lamps, 10);
    // ...and they are genuinely different animals, not one population resized.
    const s = env.state.list.filter(p => p.spark), l = env.state.list.filter(p => !p.spark);
    assert.ok(Math.max(...s.map(p => p.period)) < Math.min(...l.map(p => p.period)), "spark periods are all shorter");
    assert.ok(Math.max(...s.map(p => p.duty)) < Math.min(...l.map(p => p.duty)), "sparks blink, lamps breathe");
    assert.ok(Math.max(...s.map(p => p.r0)) < Math.min(...l.map(p => p.r0)), "sparks are smaller");
    assert.ok(Math.min(...s.map(p => p.sp)) > Math.max(...l.map(p => p.sp)), "sparks are faster");
});

test("a resize re-scales geometry from the CSS box and rehomes strays", () => {
    const env = testEnv({ rand: seeded(4) });
    fireflies.init(env);
    assert.ok(Math.abs(env.state.g - 800 / 900) < 1e-9, "geometry = apparentScale(short edge / 900)");
    env.width = 380; env.height = 720; env.scale = 0.4;
    fireflies.resize(env);
    for (const p of env.state.list) {
        assert.ok(p.x >= 0 && p.x <= 380 && p.y >= 0 && p.y <= 720, "a shrunk viewport strands nobody");
    }
    assert.ok(env.state.g < 1, "a small phone box scales the geometry down, not up");
});

// ---------------------------------------------------------------------------
// 3. Motion: no upward bias, noise steering (not a random walk)
// ---------------------------------------------------------------------------

test("the field wanders with NO upward bias (this is the star sim with the rise removed)", () => {
    const env = testEnv({ rand: seeded(21) });
    fireflies.init(env);
    const x0 = env.state.list.map(p => p.x), y0 = env.state.list.map(p => p.y);
    const ctx = fakeCtx();
    for (let f = 1; f <= 600; f++) { env.now = f * 16.6667; frame(ctx, fireflies, env, 1 / 60); }

    const moved = env.state.list.filter((p, i) => Math.hypot(p.x - x0[i], p.y - y0[i]) > 2).length;
    assert.ok(moved > env.state.list.length * 0.8, `fireflies must drift; only ${moved} moved`);

    let dy = 0, dist = 0;
    env.state.list.forEach((p, i) => { dy += p.y - y0[i]; dist += Math.hypot(p.x - x0[i], p.y - y0[i]); });
    const bias = Math.abs(dy) / dist;
    assert.ok(bias < 0.45, `net vertical drift is a bias, not a wander (|dy|/dist = ${bias.toFixed(3)})`);
});

test("heading is STEERED by a low-frequency field, not re-rolled every frame", () => {
    // A random walk decorrelates: its frame-to-frame heading change is unbounded
    // and its path length dwarfs its net displacement. Steering bounds the turn
    // to (turn rate x urgency x dt), which at 60 Hz is at most ~0.09 of a
    // half-turn, and keeps the flight curved rather than scribbled.
    const env = testEnv({ rand: seeded(15) });
    fireflies.init(env);
    const ctx = fakeCtx();
    const n = env.state.list.length;
    const prevH = env.state.list.map(p => p.h);
    const prevL = env.state.list.map(p => p.life);
    const px = env.state.list.map(p => p.x), py = env.state.list.map(p => p.y);
    const sx = px.slice(), sy = py.slice();
    const path = new Array(n).fill(0);
    let maxTurn = 0, sumTurn = 0, turns = 0, walls = 0;
    for (let f = 1; f <= 600; f++) {
        env.now = f * 16.6667;
        frame(ctx, fireflies, env, 1 / 60);
        for (let i = 0; i < n; i++) {
            const p = env.state.list[i];
            if (p.life <= prevL[i]) {                       // skip the frame a slot recycled on
                const d = Math.abs(((p.h - prevH[i] + Math.PI * 3) % (Math.PI * 2)) - Math.PI);
                maxTurn = Math.max(maxTurn, d); sumTurn += d; turns++;
                path[i] += Math.hypot(p.x - px[i], p.y - py[i]);
            } else { sx[i] = p.x; sy[i] = p.y; path[i] = 0; }
            if (p.x === 0 || p.x === env.width || p.y === 0 || p.y === env.height) walls++;
            prevH[i] = p.h; prevL[i] = p.life; px[i] = p.x; py[i] = p.y;
        }
    }
    assert.ok(maxTurn < 0.35, `worst single-frame turn ${maxTurn.toFixed(4)} rad exceeds the steering law's bound`);
    assert.ok(sumTurn / turns < 0.01, `mean turn ${(sumTurn / turns).toFixed(4)} rad/frame reads as jitter`);
    assert.equal(walls, 0, "the containment backstop fired — a visible bounce off the edge");

    const straight = env.state.list
        .map((p, i) => (path[i] > 20 ? Math.hypot(p.x - sx[i], p.y - sy[i]) / path[i] : 1))
        .sort((a, b) => a - b);
    const median = straight[straight.length >> 1];
    assert.ok(median > 0.3, `flight must curve, not scribble (median net/path = ${median.toFixed(3)})`);
});

test("nobody escapes the viewport, and nothing is NaN after 1000 frames", () => {
    const env = testEnv({ rand: seeded(77) });
    const ctx = run(fireflies, env, 1000);
    for (const p of env.state.list) {
        assert.ok(p.x >= 0 && p.x <= env.width, `escaped horizontally at x=${p.x}`);
        assert.ok(p.y >= 0 && p.y <= env.height, `escaped vertically at y=${p.y}`);
        for (const key of ["x", "y", "h", "sp", "t", "period", "life", "r0", "swell"]) {
            assert.ok(Number.isFinite(p[key]), `p.${key} went non-finite (${p[key]})`);
        }
        assert.ok(p.life > 0 && p.life <= 1, `life out of range: ${p.life}`);
        assert.ok(p.t >= 0 && p.t < p.period, `pulse phase out of range: ${p.t}`);
    }
    for (const d of ctx.draws) {
        assert.ok(Number.isFinite(d.alpha) && d.alpha >= 0 && d.alpha <= 1, `bad alpha ${d.alpha}`);
        assert.ok(Number.isFinite(d.w) && d.w > 0, `bad sprite size ${d.w}`);
    }
    assert.ok(ctx.draws.length > 0, "the field drew nothing at all");
});

// ---------------------------------------------------------------------------
// 4. THE rule: pulses desynchronised in BOTH period and phase
// ---------------------------------------------------------------------------

test("every firefly gets its own period AND its own phase", () => {
    const env = testEnv({ rand: seeded(33) });
    fireflies.init(env);
    const list = env.state.list;
    assert.equal(new Set(list.map(p => p.period)).size, list.length, "a shared period beats back into sync");
    assert.equal(new Set(list.map(p => p.t / p.period)).size, list.length, "a shared phase flashes in unison");
    // Phases must be SPREAD, not merely distinct.
    const share = worstQuarterShare(list);
    assert.ok(share < 0.5, `${(share * 100).toFixed(0)}% of the field starts in one quarter of the cycle`);
});

test("the field never flashes together — at any frame, over a full minute", () => {
    // The cheap-CSS-demo failure mode. Sample the same envelope the draw path
    // uses and cap how much of the population may be near its peak at once.
    const env = testEnv({ rand: seeded(19) });
    fireflies.init(env);
    const ctx = fakeCtx();
    let worst = 0, worstAt = 0;
    for (let f = 1; f <= 3600; f++) {                     // 60 s at 60 Hz
        env.now = f * 16.6667;
        frame(ctx, fireflies, env, 1 / 60);
        let hot = 0;
        for (const p of env.state.list) if (pulse(p.t / p.period, p.duty) > 0.8) hot++;
        const share = hot / env.state.list.length;
        if (share > worst) { worst = share; worstAt = f; }
    }
    assert.ok(worst < 0.6, `${(worst * 100).toFixed(0)}% of the field peaked together at frame ${worstAt}`);
});

test("two fireflies drift apart instead of locking on", () => {
    const env = testEnv({ rand: seeded(64) });
    fireflies.init(env);
    const a = env.state.list.find(p => !p.spark), b = env.state.list.filter(p => !p.spark)[1];
    const ctx = fakeCtx();
    let sameCount = 0, n = 0;
    for (let f = 1; f <= 1800; f++) {
        env.now = f * 16.6667;
        frame(ctx, fireflies, env, 1 / 60);
        const ea = pulse(a.t / a.period, a.duty), eb = pulse(b.t / b.period, b.duty);
        if (Math.abs(ea - eb) < 0.05) sameCount++;
        n++;
    }
    assert.ok(sameCount / n < 0.5, `two lamps tracked each other ${(100 * sameCount / n).toFixed(0)}% of the time`);
});

test("pulse() is a flash with a real dark gap, and has no corners", () => {
    assert.equal(pulse(0.5, 0.3), 0, "past the duty cycle the spark is fully dark");
    assert.equal(pulse(0, 0.5), 0);
    const peak = pulse(0.28 * 0.5, 0.5);
    assert.ok(peak > 0.999, "the attack reaches full brightness");
    // Sampled derivative must stay bounded — a triangle wave reads as an LED.
    let maxSlope = 0;
    for (let u = 0; u < 0.5; u += 0.001) maxSlope = Math.max(maxSlope, Math.abs(pulse(u + 0.001, 0.5) - pulse(u, 0.5)));
    assert.ok(maxSlope < 0.02, `pulse has a corner (max step ${maxSlope.toFixed(4)})`);
});

// ---------------------------------------------------------------------------
// 5. Premium rules: size over life, colour ramp over life
// ---------------------------------------------------------------------------

test("size is never constant: a life curve times a per-particle envelope", () => {
    const env = testEnv({ rand: seeded(52) });
    fireflies.init(env);
    // Every particle carries its OWN swell depth, and the two kinds swell over
    // different ranges — a constant-size field would collapse both to one value.
    const swells = env.state.list.map(p => p.swell);
    assert.equal(new Set(swells).size, swells.length, "swell must be per-particle");
    assert.ok(Math.min(...swells) > 0.17 && Math.max(...swells) < 0.71);

    // Track one lamp's drawn radius across ten seconds, using the same
    // expression draw() uses, and require it to actually move.
    const p = env.state.list.find(x => !x.spark);
    const ctx = fakeCtx();
    let min = Infinity, max = 0;
    for (let f = 1; f <= 600; f++) {
        env.now = f * 16.6667;
        frame(ctx, fireflies, env, 1 / 60);
        const e = p.floor + (1 - p.floor) * pulse(p.t / p.period, p.duty);
        const r = p.r0 * env.state.g * sizeOverLife(p.life) * (1 + p.swell * e);
        min = Math.min(min, r); max = Math.max(max, r);
    }
    assert.ok(max / min > 1.1, `size must breathe (min ${min.toFixed(2)}, max ${max.toFixed(2)})`);

    // ...and the life curve is a curve: born small, full-size mid-life, tapered.
    assert.equal(sizeOverLife(1), 0, "a newborn firefly has no size yet");
    assert.ok(sizeOverLife(0.85) > 0.95 && sizeOverLife(0.5) === 1, "mid-life sits at full size");
    assert.ok(sizeOverLife(0.02) < 0.7, "an old firefly shrinks as it fades");
});

test("colour is a ramp indexed by life, per sub-population, not a flat hue", () => {
    // Two atlas stops are cross-faded per particle, so a young and an old
    // firefly of the same kind land on different sprites.
    for (const kind of ["lamp", "spark"]) {
        const k = KINDS[kind];
        const young = k.hue[0], old = k.hue[1];
        assert.ok(old - young > 1.5, `${kind} must traverse the ramp, not sit on one stop`);
        assert.ok(young >= 0 && old <= 5, `${kind} ramp index must stay inside the 6-stop atlas`);
    }
    assert.ok(KINDS.spark.hue[0] < KINDS.lamp.hue[0], "sparks read cooler/whiter than the lamps");
});

// ---------------------------------------------------------------------------
// 6. THE LEGIBILITY BUDGET — the assertion that protects the chat text
// ---------------------------------------------------------------------------

// A baked sprite is a radial gradient (1.0 at the centre, 0.5 at 35%, 0 at the
// rim), so its mean alpha over its bounding box is ~0.2; 0.25 is the generous
// round-up. Multiply by the alpha it was drawn at and its area, sum over the
// frame, divide by the screen: the fraction of a fully-white screen this field
// adds. Additive blending means these simply sum.
const SPRITE_FILL = 0.25;
function frameLight(ctx, env) {
    let lit = 0;
    for (const d of ctx.draws) lit += d.alpha * d.w * d.h * SPRITE_FILL;
    return lit / (env.width * env.height);
}

test("the field is the safest backdrop in the catalogue (measured, not asserted by vibes)", () => {
    const env = testEnv({ rand: seeded(6) });
    fireflies.init(env);
    const ctx = fakeCtx();
    let peak = 0, sum = 0, n = 0;
    for (let f = 1; f <= 1800; f++) {               // 30 s
        env.now = f * 16.6667;
        frame(ctx, fireflies, env, 1 / 60);
        const l = frameLight(ctx, env);
        peak = Math.max(peak, l); sum += l; n++;
    }
    const mean = sum / n;
    // THE BUDGET. Measured across six seeds the field peaks at ~0.040% and
    // averages ~0.021% of a white screen; the gates are set at roughly 2x that,
    // so any retune that doubles the light fails here instead of on Brandon's
    // screen. 0.08% of white is under one 8-bit step of background lift — body
    // text keeps its contrast at the brightest frame the field can produce.
    assert.ok(peak < 0.0008, `peak added luminance ${(peak * 100).toFixed(4)}% exceeds the 0.08% budget`);
    assert.ok(mean < 0.0004, `mean added luminance ${(mean * 100).toFixed(4)}% exceeds the 0.04% budget`);

    // ...and it must be dramatically quieter than the loudest shipped field.
    const eenv = testEnv({ rand: seeded(6) });
    const ectx = fakeCtx();
    embers.init(eenv);
    let epeak = 0;
    for (let f = 1; f <= 600; f++) {
        eenv.now = f * 16.6667;
        frame(ectx, embers, eenv, 1 / 60);
        epeak = Math.max(epeak, frameLight(ectx, eenv));
    }
    // Measured ratio is ~1:27 against embers' 1.14% peak.
    assert.ok(peak < epeak * 0.06,
        `fireflies (${(peak * 100).toFixed(3)}%) must be far under embers (${(epeak * 100).toFixed(3)}%)`);
});

test("intensity 0 means a black backdrop — the field can always be turned off", () => {
    const env = testEnv({ rand: seeded(2), intensity: 0 });
    const ctx = run(fireflies, env, 30);
    assert.equal(ctx.draws.length, 0, "zero intensity must emit no light at all");
});

// ---------------------------------------------------------------------------
// 7. Frame-rate independence and the dpr rule
// ---------------------------------------------------------------------------

test("60 Hz and 120 Hz travel the same path", () => {
    const at = (dt, frames) => {
        const env = testEnv({ rand: seeded(3) });
        run(fireflies, env, frames, dt);
        return env.state.list.map(p => ({ x: p.x, y: p.y, t: p.t }));
    };
    const a = at(1 / 60, 120), b = at(1 / 120, 240);      // 2 seconds either way
    let worstPos = 0, worstPhase = 0;
    a.forEach((p, i) => {
        worstPos = Math.max(worstPos, Math.hypot(p.x - b[i].x, p.y - b[i].y));
        worstPhase = Math.max(worstPhase, Math.abs(p.t - b[i].t));
    });
    assert.ok(worstPos < 1.0, `a 120 Hz screen drifted ${worstPos.toFixed(3)}px from a 60 Hz one in 2 s`);
    assert.ok(worstPhase < 1e-9, `the pulse clock is not delta-timed (${worstPhase})`);
});

test("devicePixelRatio must NEVER reach particle geometry", () => {
    // The bug that shipped for months: geometry tracking dpr made the whole
    // field dpr x too big on every HiDPI screen.
    const draw = (dpr) => {
        const env = testEnv({ rand: seeded(88), dpr });
        const ctx = run(fireflies, env, 90);
        return JSON.stringify(ctx.draws);
    };
    assert.equal(draw(1), draw(3), "a 3x screen must produce byte-identical geometry");
    assert.equal(/dpr/.test(fireflies.update.toString() + fireflies.draw.toString()), false,
        "no hook may even read env.dpr");
});

// ---------------------------------------------------------------------------
// 8. Lifecycle + engine contract
// ---------------------------------------------------------------------------

test("the field thins out when idle and revives — unsynced — on rearm", () => {
    const env = testEnv({ rand: seeded(31) });
    fireflies.init(env);
    const n = env.state.list.length;
    const ctx = fakeCtx();
    env.active = false;
    for (let f = 1; f <= 3600; f++) { env.now = f * 16.6667; frame(ctx, fireflies, env, 1 / 60); }
    const dead = env.state.list.filter(p => p.dead).length;
    assert.ok(dead > 0, "an idle field must stop recycling, not loop forever");
    assert.equal(env.state.list.length, n, "the pool is fixed-size: slots go dark, they are not removed");

    env.active = true;
    fireflies.rearm(env);
    assert.equal(env.state.list.filter(p => p.dead).length, 0, "rearm revives every dark slot");
    const phases = env.state.list.map(p => p.t / p.period);
    assert.equal(new Set(phases).size, phases.length, "a rearm must not hand the field a common rhythm");
    assert.equal(new Set(env.state.list.map(p => p.period)).size, phases.length, "...nor a common period");
    const share = worstQuarterShare(env.state.list);
    assert.ok(share < 0.5, `${(share * 100).toFixed(0)}% of the revived field shares a quarter-cycle`);
});

test("draw is a no-op before the sprite atlas is baked, and never sets its own blend", () => {
    const env = testEnv({ sprites: null });
    const ctx = run(fireflies, env, 5);
    assert.equal(ctx.draws.length, 0, "no atlas → draw nothing, don't throw");

    const env2 = testEnv({ rand: seeded(12) });
    const ctx2 = fakeCtx();
    fireflies.init(env2);
    for (let f = 1; f <= 5; f++) {
        env2.now = f * 16.6667;
        env2.dt = 1 / 60;
        fireflies.update(1 / 60, env2);
        const before = ctx2.blendWrites.length;
        fireflies.draw(ctx2, env2);
        assert.equal(ctx2.blendWrites.length, before, "compositing is the engine's job, never the effect's");
    }
});

test("the hot path allocates nothing: the pool is reused for the whole session", () => {
    const env = testEnv({ rand: seeded(45) });
    fireflies.init(env);
    const list = env.state.list;
    const ids = list.slice();
    const ctx = fakeCtx();
    for (let f = 1; f <= 3600; f++) { env.now = f * 16.6667; frame(ctx, fireflies, env, 1 / 60); }
    assert.equal(env.state.list, list, "the array itself must survive the session");
    assert.ok(list.every((p, i) => p === ids[i]), "a recycled firefly reuses its slot object, it is not reallocated");
    // A recycled slot keeps its species — sparks stay sparks forever.
    assert.ok(list.every((p, i) => p.spark === (i % 3 === 1)), "recycling must not reshuffle the sub-populations");
});

test("state is reproducible from a seeded rand — no Math.random() anywhere inside", () => {
    const once = () => {
        const env = testEnv({ rand: seeded(99) });
        run(fireflies, env, 240);
        return JSON.stringify(env.state.list);
    };
    assert.equal(once(), once());
});
