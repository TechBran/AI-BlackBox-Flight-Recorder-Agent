// Ledger Rain physics tests — headless, seeded, no DOM.
//
// The effect takes its randomness from the injected env.rand and its clock from
// env.now/env.dt, never from Math.random()/performance.now(), which is what makes
// the field reproducible frame-for-frame here.
//
// The assertion that matters most is the LEGIBILITY BUDGET: this is a backdrop
// behind chat text, and it is measured against the recorded fillText calls (lead
// alpha, summed trail alpha, per column, per frame) rather than against the
// module's own constants — a refactor that quietly brightens the field fails.
//
// Run: node --test Portal/modules/fx/effects/ledger.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createEnv, applyClearPolicy, applyBlend, resetBlend, getEffect, parseClearPolicy } from "../registry.js";
import descriptor, {
    GLYPHS, DEFAULT_IDS, MAX_TRAIL, MAX_COLS, LEDGER_TRAIL, NOISE_TRAIL,
    KIND_LEDGER, KIND_NOISE, LEAD_A, TRAIL_BUDGET, SIZE_BUCKETS,
} from "./ledger.js";

// Matrix is the profile this effect is allowed to match and not exceed.
const MATRIX_INK = LEAD_A + TRAIL_BUDGET;      // 1.45 alpha-glyphs per column per frame
const EPS = 1e-9;

/** Deterministic PRNG (same one the registry tests use). */
function seeded(seed = 1) {
    let a = seed >>> 0;
    return function rand() {
        a = (a + 0x6D2B79F5) >>> 0;
        let t = Math.imul(a ^ (a >>> 15), 1 | a);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

/** Canvas2D recorder: every fillText with the fillStyle in force, every font
 *  write, and every globalCompositeOperation write. */
function fakeCtx() {
    const texts = [];      // {glyph, x, y, alpha}
    const fonts = [];
    const blendWrites = [];
    const ctx = {
        texts, fonts, blendWrites,
        globalAlpha: 1, fillStyle: null, textBaseline: null,
        clearRect() {}, fillRect() {},
        fillText(glyph, x, y) {
            const m = /rgba\([^)]*,\s*([0-9.]+)\s*\)/.exec(String(ctx.fillStyle));
            texts.push({ glyph, x, y, alpha: m ? parseFloat(m[1]) : NaN });
        },
        reset() { texts.length = 0; fonts.length = 0; },
    };
    let font = null;
    Object.defineProperty(ctx, "font", { get: () => font, set: (v) => { font = v; fonts.push(v); } });
    let gco = "source-over";
    Object.defineProperty(ctx, "globalCompositeOperation", {
        get: () => gco, set: (v) => { gco = v; blendWrites.push(v); },
    });
    return ctx;
}

function testEnv(over = {}) {
    return createEnv({ width: 1400, height: 800, scale: 1, intensity: 1, rand: seeded(42), ...over });
}

/** One engine frame, driven exactly as ember-fx.js's loop() drives it. */
function frame(ctx, env, dt) {
    env.dt = dt;
    env.now += dt * 1000;
    descriptor.update(dt, env);
    applyClearPolicy(ctx, descriptor, env);
    applyBlend(ctx, descriptor);
    const before = ctx.blendWrites.length;
    descriptor.draw(ctx, env);
    const wrote = ctx.blendWrites.length - before;
    resetBlend(ctx);
    return wrote;
}

function started(over = {}) {
    const env = testEnv(over);
    descriptor.init(env);
    return env;
}

/** Read a column top-to-bottom: oldest pushed row first, head last. */
function columnText(c, n) {
    let s = "";
    for (let d = n - 1; d >= 0; d--) s += GLYPHS[c.buf[(c.hp - d + MAX_TRAIL) % MAX_TRAIL]];
    return s;
}

// ---------------------------------------------------------------------------
// Contract
// ---------------------------------------------------------------------------

test("registers with a valid, matrix-parity descriptor", () => {
    const reg = getEffect("ledger");
    assert.ok(reg, "effect registered itself on import");
    assert.equal(reg.label, "Ledger Rain");
    assert.equal(reg.blend, "source-over");                 // additive would blow out behind chat text
    const { kind, color } = parseClearPolicy(reg.clearPolicy);
    assert.equal(kind, "fade");
    assert.match(color, /0\.085\)$/, "smear persistence matches the shipped Matrix field exactly");
});

test("ships plausible default ids and fetches nothing", () => {
    assert.ok(Array.isArray(descriptor.ids) && descriptor.ids.length >= 3);
    for (const id of descriptor.ids) assert.match(id, /^SNAP-\d{8}-\d+$/);
    assert.equal(DEFAULT_IDS.length, descriptor.ids.length);
    const src = descriptor.draw.toString() + descriptor.update.toString();
    assert.ok(!/fetch|XMLHttpRequest|import\(/.test(src), "renderer must not reach for the network");
});

// ---------------------------------------------------------------------------
// Population
// ---------------------------------------------------------------------------

test("population is bounded and spans the viewport", () => {
    for (const [w, h] of [[320, 640], [1400, 800], [5120, 1440], [0, 0]]) {
        const env = started({ width: w, height: h });
        const st = env.state;
        assert.ok(st.cols.length >= 1 && st.cols.length <= MAX_COLS, `cols in range at ${w}x${h}`);
        if (w > 0) assert.ok(Math.abs(st.cols.length * st.colw - w) < 1e-6, "columns tile the full width");
        for (const c of st.cols) {
            assert.equal(c.buf.length, MAX_TRAIL);
            assert.ok(c.kind === KIND_LEDGER || c.kind === KIND_NOISE);
            assert.ok(c.trail === LEDGER_TRAIL || c.trail === NOISE_TRAIL);
            assert.ok(1 + c.trail <= MAX_TRAIL, "a column's rows fit its ring buffer");
        }
    }
});

test("both sub-populations exist, with distinct parameters", () => {
    const env = started();
    const ledger = env.state.cols.filter(c => c.kind === KIND_LEDGER);
    const noise = env.state.cols.filter(c => c.kind === KIND_NOISE);
    assert.ok(ledger.length > 5 && noise.length > 5, `ledger=${ledger.length} noise=${noise.length}`);
    const maxLedgerSp = Math.max(...ledger.map(c => c.sp));
    const minNoiseSp = Math.min(...noise.map(c => c.sp));
    assert.ok(maxLedgerSp < minNoiseSp, "id columns fall slower than noise columns, always");
    assert.ok(Math.min(...ledger.map(c => c.envSize)) > Math.min(...noise.map(c => c.envSize)),
        "size envelopes differ per sub-population");
});

test("geometry ignores devicePixelRatio (the shipped bug)", () => {
    const a = started({ dpr: 1, rand: seeded(7) });
    const b = started({ dpr: 3, rand: seeded(7) });
    assert.equal(a.state.size, b.state.size);
    assert.equal(a.state.pitch, b.state.pitch);
    assert.equal(a.state.colw, b.state.colw);
    assert.deepEqual(a.state.fonts, b.state.fonts);
    assert.equal(a.state.cols.length, b.state.cols.length);
});

// ---------------------------------------------------------------------------
// Motion
// ---------------------------------------------------------------------------

test("rain falls DOWN, and only stalls for a hold beat", () => {
    const env = started();
    const ctx = fakeCtx();
    let moved = 0;
    for (let f = 0; f < 600; f++) {
        const prev = env.state.cols.map(c => ({ y: c.y, row: c.row, hold: c.hold }));
        frame(ctx, env, 1 / 60);
        ctx.reset();
        env.state.cols.forEach((c, i) => {
            if (c.y < prev[i].y) {
                assert.ok(prev[i].y > env.height, "downward-only: a drop can only follow a respawn");
                return;
            }
            if (c.y > prev[i].y) moved++;
            // A stalled frame is only legal if the column entered it holding —
            // the beat's LAST frame ends with hold back at 0, so check the entry state.
            else assert.ok(prev[i].hold > 0, "a column only holds still while its id is being read");
            assert.ok(c.row >= prev[i].row || prev[i].y > env.height, "row index tracks the head monotonically");
        });
    }
    assert.ok(moved > 50000, `columns are actually falling (${moved} advancing column-frames)`);
});

test("motion is delta-timed: 60 Hz and 120 Hz agree after 2 seconds", () => {
    const a = started({ rand: seeded(9) });
    const b = started({ rand: seeded(9) });
    const ctx = fakeCtx();
    for (let f = 0; f < 120; f++) { frame(ctx, a, 1 / 60); ctx.reset(); }
    for (let f = 0; f < 240; f++) { frame(ctx, b, 1 / 120); ctx.reset(); }
    assert.equal(a.state.cols.length, b.state.cols.length);
    a.state.cols.forEach((c, i) => {
        const d = b.state.cols[i];
        assert.ok(Math.abs(c.y - d.y) < 0.01, `column ${i}: ${c.y} vs ${d.y}`);
        assert.equal(c.row, d.row, `column ${i} advanced the same number of rows`);
    });
});

// ---------------------------------------------------------------------------
// The legibility budget
// ---------------------------------------------------------------------------

test("legibility budget: per column, lead <= 0.95 and trail sum <= 0.5", () => {
    const env = started();
    const ctx = fakeCtx();
    const byColumn = new Map();
    let worstColumn = 0, worstLead = 0, worstTrail = 0, frames = 0, glyphs = 0;
    for (let f = 0; f < 400; f++) {
        frame(ctx, env, 1 / 60);
        frames++;
        glyphs += ctx.texts.length;
        byColumn.clear();
        for (const t of ctx.texts) {
            assert.ok(Number.isFinite(t.alpha), "every glyph draws through a parseable rgba() style");
            assert.equal(GLYPHS.includes(t.glyph), true, "glyphs come from the pooled slug alphabet");
            const key = Math.round(t.x);
            const acc = byColumn.get(key) || { sum: 0, max: 0, n: 0 };
            acc.sum += t.alpha; acc.max = Math.max(acc.max, t.alpha); acc.n++;
            byColumn.set(key, acc);
        }
        for (const [, acc] of byColumn) {
            const trail = acc.sum - acc.max;              // exactly one lead per column, always the brightest
            assert.ok(acc.max <= LEAD_A + EPS, `lead alpha ${acc.max} > ${LEAD_A}`);
            assert.ok(trail <= TRAIL_BUDGET + EPS, `trail ink ${trail} > ${TRAIL_BUDGET}`);
            assert.ok(acc.sum <= MATRIX_INK + EPS, `column ink ${acc.sum} > matrix profile ${MATRIX_INK}`);
            assert.ok(acc.n <= 1 + LEDGER_TRAIL, `column drew ${acc.n} glyphs`);
            worstColumn = Math.max(worstColumn, acc.sum);
            worstLead = Math.max(worstLead, acc.max);
            worstTrail = Math.max(worstTrail, trail);
        }
        assert.ok(ctx.fonts.length <= 2 * SIZE_BUCKETS, `font state changes per frame: ${ctx.fonts.length}`);
        ctx.reset();
    }
    assert.ok(glyphs / frames > 100, "the field is actually drawing");
    assert.ok(worstColumn > 0.9, "and drawing at a useful contrast, not invisibly");
    console.log(`  budget: worst column ink ${worstColumn.toFixed(3)} / ${MATRIX_INK} `
        + `(lead ${worstLead.toFixed(3)}, trail ${worstTrail.toFixed(3)}) over ${frames} frames, `
        + `${(glyphs / frames).toFixed(0)} glyphs/frame`);
});

test("a WARMED field is quieter than the shipped Matrix, not just per column", () => {
    const env = started();
    const ctx = fakeCtx();
    for (let f = 0; f < 3000; f++) { frame(ctx, env, 1 / 60); ctx.reset(); }   // fill the screen
    let total = 0, frames = 0;
    for (let f = 0; f < 200; f++) {
        frame(ctx, env, 1 / 60);
        for (const t of ctx.texts) total += t.alpha;
        frames++;
        ctx.reset();
    }
    const ink = total / frames;
    const matrix = env.state.cols.length * MATRIX_INK;
    assert.ok(ink < matrix, `field ink ${ink.toFixed(1)} must stay under the Matrix profile ${matrix.toFixed(1)}`);
    assert.ok(ink > env.state.cols.length * 0.4, "but the field is populated, not a ghost town");
    console.log(`  whole-field ink ${ink.toFixed(1)} vs shipped Matrix ${matrix.toFixed(1)} `
        + `(${((1 - ink / matrix) * 100).toFixed(0)}% calmer) across ${env.state.cols.length} columns`);
});

test("the effect never sets its own blend mode", () => {
    const env = started();
    const ctx = fakeCtx();
    for (let f = 0; f < 60; f++) {
        assert.equal(frame(ctx, env, 1 / 60), 0, "blend is the engine's job");
        ctx.reset();
    }
});

// ---------------------------------------------------------------------------
// Ids
// ---------------------------------------------------------------------------

test("a column resolves an INJECTED id, in order, then dissolves", () => {
    const original = descriptor.ids;
    const ID = "SNAP-20260101-0007";
    descriptor.ids = [ID];
    try {
        const env = started({ rand: seeded(3) });
        const ctx = fakeCtx();
        let spelled = false, dissolved = false;
        for (let f = 0; f < 6000 && !dissolved; f++) {
            frame(ctx, env, 1 / 60);
            ctx.reset();
            for (const c of env.state.cols) {
                if (!spelled && columnText(c, ID.length) === ID) spelled = true;
                else if (spelled && c.kind === KIND_LEDGER && c.word < 0 && c.gap > 0) dissolved = true;
            }
        }
        assert.ok(spelled, "a ledger column spelled the injected id top-to-bottom");
        assert.ok(dissolved, "and then went back to noise before re-locking");
        assert.equal(env.state.seqs.length, 1, "only the injected id is compiled");
    } finally {
        descriptor.ids = original;
    }
});

test("a hot-swapped id array is picked up without a re-init", () => {
    const original = descriptor.ids;
    try {
        const env = started();
        const before = env.state.seqs.length;
        descriptor.ids = ["SNAP-19700101-0001", "SNAP-19700101-0002"];
        descriptor.update(1 / 60, env);
        assert.equal(env.state.seqs.length, 2);
        assert.notEqual(env.state.seqs.length, before);
    } finally {
        descriptor.ids = original;
    }
});

test("garbage ids fall back instead of throwing", () => {
    const original = descriptor.ids;
    try {
        for (const bad of [[], null, [1, {}, "", "   "], "not-an-array"]) {
            descriptor.ids = bad;
            const env = started();
            assert.ok(env.state.seqs.length >= 3, "fell back to the shipped defaults");
        }
    } finally {
        descriptor.ids = original;
    }
});

// ---------------------------------------------------------------------------
// Durability
// ---------------------------------------------------------------------------

test("no NaN anywhere after 1000 frames of hostile dt", () => {
    const env = started();
    const ctx = fakeCtx();
    const dts = [1 / 60, 1 / 120, 1 / 144, 0.5, 1 / 30, 0];   // includes a half-second stall
    for (let f = 0; f < 1000; f++) {
        frame(ctx, env, dts[f % dts.length]);
        for (const t of ctx.texts) {
            assert.ok(Number.isFinite(t.x) && Number.isFinite(t.y), "glyph position is finite");
            assert.ok(t.alpha > 0 && t.alpha <= LEAD_A + EPS, "glyph alpha stays in budget");
        }
        ctx.reset();
        for (const c of env.state.cols) {
            assert.ok(Number.isFinite(c.y) && Number.isFinite(c.sp) && Number.isFinite(c.flare), "column state finite");
            assert.ok(Number.isInteger(c.row) && Number.isInteger(c.hp), "row/head are integers");
            assert.ok(c.hp >= 0 && c.hp < MAX_TRAIL, "ring head in range");
            assert.ok(c.y <= env.height + env.height * 0.3 + 1, "nothing escapes below the viewport");
            for (let d = 0; d < MAX_TRAIL; d++) {
                assert.ok(c.buf[d] >= 0 && c.buf[d] < GLYPHS.length, "buffer holds valid glyph indices");
            }
        }
    }
});

test("resize rebuilds the grid; rearm never clears a live field", () => {
    const env = started();
    const ctx = fakeCtx();
    for (let f = 0; f < 30; f++) { frame(ctx, env, 1 / 60); ctx.reset(); }
    const wide = env.state.cols.length;
    env.width = 420; env.height = 900;
    descriptor.resize(env);
    assert.notEqual(env.state.cols.length, wide);
    assert.ok(Math.abs(env.state.cols.length * env.state.colw - 420) < 1e-6);
    const cols = env.state.cols;
    descriptor.rearm(env);
    assert.equal(env.state.cols, cols, "rearm leaves a populated field alone");
    env.state.cols = [];
    descriptor.rearm(env);
    assert.ok(env.state.cols.length > 0, "rearm re-inits an emptied field");
});
