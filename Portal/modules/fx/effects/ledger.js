/**
 * Effect: LEDGER RAIN (id 'ledger').
 *
 * Matrix rain forked into the BlackBox's own alphabet: the falling glyphs are the
 * characters a snapshot slug is made of, and a column periodically LOCKS onto a
 * real snapshot id (SNAP-20260724-8551), spells it one character per row as it
 * falls, then dissolves back into noise and re-locks onto another.
 *
 * Forked from effects/matrix.js. What carries over verbatim, and why:
 *   - the translucent smear (clearPolicy fade at 0.085) — same persistence, so a
 *     glyph the column has already passed keeps decaying behind the head instead
 *     of being redrawn. That is where most of a Matrix field's apparent trail
 *     comes from, and it costs nothing.
 *   - the per-column speed variance and the width-derived column grid.
 *   - blend 'source-over'. Additive would let the smear accumulate to white
 *     behind the chat text — the whole reason the engine owns blend per effect.
 *
 * THE LEGIBILITY BUDGET (non-negotiable — this is a BACKDROP, not a screensaver).
 * The shipped Matrix field is the busiest thing in production and is only
 * tolerable because it draws exactly two glyphs per column: a lead at alpha 0.95
 * and one trail glyph at 0.5. This effect spends the IDENTICAL ink, redistributed
 * so an id is readable: lead <= 0.95 (LEAD_A) and a decaying trail whose alphas
 * SUM to <= 0.5 (TRAIL_BUDGET) no matter how many rows the column draws. Per
 * column, per frame, total ink <= 1.45 — the same number Matrix spends. The
 * quantisation below always rounds alpha DOWN, so the budget can only be
 * undershot, never exceeded, and ledger.test.mjs asserts it against the actual
 * fillText calls rather than against these constants.
 *
 * IDs are INJECTED (`descriptor.ids`, defaulting to plausible literals). This
 * module never fetches: it is a renderer, and a backdrop that can block on the
 * network is a backdrop that stutters.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

/** Fallback ledger. The host overwrites `descriptor.ids` with real slugs. */
const DEFAULT_IDS = [
    'SNAP-20260724-8551',
    'SNAP-20260723-8532',
    'SNAP-20260720-8502',
    'SNAP-20260713-8342',
    'SNAP-20260709-8131',
];

// --- glyph pool -------------------------------------------------------------
// Single-character STRINGS built once at import: fillText needs a string, and
// String.fromCharCode() in the draw loop would allocate one per glyph per frame.
// Pure data — no DOM, no randomness — so the module stays importable headlessly.
const GLYPHS = [];
const GLYPH_AT = new Map();
function glyphOf(ch) {
    let i = GLYPH_AT.get(ch);
    if (i === undefined) { i = GLYPHS.length; GLYPHS.push(ch); GLYPH_AT.set(ch, i); }
    return i;
}
// The rotating noise buffer: slug characters only, digits listed twice because a
// snapshot slug is mostly digits and the field should read as ledger noise, not
// as an alphabet soup that happens to contain 'SNAP'.
const NOISE = [];
'01234567890123456789SNAP-ABCDEF'.split('').forEach(ch => NOISE.push(glyphOf(ch)));

// --- tunables ---------------------------------------------------------------
const MAX_TRAIL = 24;        // ring-buffer depth; also the longest id we will spell
const MAX_COLS = 220;        // hot-path bound: an ultrawide must not uncap the loop
const LEDGER_TRAIL = 8;      // trail rows for an id column (id reads over 9 rows)
const NOISE_TRAIL = 3;       // trail rows for a noise column
const KIND_LEDGER = 0, KIND_NOISE = 1;
// Roll on RESPAWN, not the steady-state mix: id columns fall slower, so they
// survive longer per pass and the live field settles at roughly 55/45 rather
// than 38/62. That is the intended read (more ids on screen) and it costs
// nothing — the ink budget below is enforced per column, whatever the mix.
const LEDGER_SHARE = 0.38;
const LEAD_A = 0.95;         // legibility ceiling — Matrix parity. NEVER raise.
const TRAIL_BUDGET = 0.5;    // total trail ink per column — Matrix parity. NEVER raise.
const TRAIL_PEAK = 0.2;      // brightest a single trail glyph may ever be
const TRAIL_DECAY = 0.72;    // geometric falloff down the trail
const LEAD_STEPS = 8, TRAIL_STEPS = 16;   // alpha quantisation (style-table rows)
const RAMP_STOPS = 5;
const SIZE_BUCKETS = 8, SIZE_MIN = 0.5, SIZE_MAX = 1.2;
const FLARE_T = 0.16;        // seconds for a freshly-resolved lead glyph to settle
const DISSOLVE_AT = 6;       // trail rows at/below this depth re-randomise (dissolve)
const HOLD_MIN = 0.22, HOLD_SPAN = 0.24;  // beat where a completed id sits still

// Colour ramps indexed by fall progress (rule: never a flat colour). Lead is the
// accent red (#ff4a4a) hot at the top and cooling as the column ages; the trail
// is a deep red that sinks toward the smear.
const LEAD_RAMPS = [
    [[255, 196, 190], [255, 120, 112], [255, 74, 74], [236, 56, 62], [200, 40, 48]],  // ledger
    [[255, 120, 116], [236, 74, 78], [206, 52, 58], [172, 40, 48], [138, 30, 40]],    // noise
];
const TRAIL_RAMPS = [
    [[214, 60, 60], [186, 44, 48], [156, 32, 40], [126, 24, 32], [98, 18, 26]],       // ledger
    [[150, 30, 34], [126, 24, 30], [102, 20, 26], [80, 16, 22], [60, 12, 18]],        // noise
];

/**
 * Quantised trail alpha per trail length: WSTEP[len][i] is the style step for the
 * i-th trail row, or -1 for "too faint to be worth a fillText".
 *
 * Built at import because it depends only on constants — intensity is folded into
 * the style STRINGS instead, so this table never needs rebuilding. floor() means
 * every drawn alpha is <= its ideal weight, which is what keeps the sum under
 * TRAIL_BUDGET by construction.
 */
const WSTEP = [];
for (let len = 0; len <= LEDGER_TRAIL; len++) {
    const steps = new Int8Array(len);
    const norm = len ? (1 - Math.pow(TRAIL_DECAY, len)) / (1 - TRAIL_DECAY) : 1;
    for (let i = 0; i < len; i++) {
        const w = TRAIL_BUDGET * Math.pow(TRAIL_DECAY, i) / norm;
        const s = Math.floor(w / TRAIL_PEAK * TRAIL_STEPS) - 1;
        steps[i] = s < 0 ? -1 : Math.min(TRAIL_STEPS - 1, s);
    }
    WSTEP.push(steps);
}

/** Size over life: punches in as the column enters, tapers as it ages. */
function sizeCurve(p) {
    const inn = p < 0.14 ? p / 0.14 : 1;
    return (0.62 + 0.38 * inn) * (1 - 0.26 * p);
}

function bucket(mult) {
    const t = (mult - SIZE_MIN) / (SIZE_MAX - SIZE_MIN) * (SIZE_BUCKETS - 1);
    return t < 0 ? 0 : (t > SIZE_BUCKETS - 1 ? SIZE_BUCKETS - 1 : Math.round(t));
}

/** Compile the injected id array into glyph-index sequences. Identity-guarded by
 *  the caller, so a host may swap `descriptor.ids` at any time and the next frame
 *  picks it up without a reload. */
function compileIds(src) {
    const seqs = [];
    if (Array.isArray(src)) {
        for (const raw of src) {
            if (typeof raw !== 'string') continue;
            const s = raw.trim().slice(0, MAX_TRAIL);
            if (!s) continue;
            const seq = new Int16Array(s.length);
            for (let i = 0; i < s.length; i++) seq[i] = glyphOf(s[i]);
            seqs.push(seq);
        }
    }
    return seqs.length ? seqs : compileIds(DEFAULT_IDS);
}

function ensureIds(env) {
    const st = env.state;
    const src = (env.ids || descriptor.ids || DEFAULT_IDS);
    if (st._idsRef !== src) { st._idsRef = src; st.seqs = compileIds(src); }
}

/** Style tables: every rgba() string the field can draw, built once and rebuilt
 *  only when intensity changes. The draw loop indexes; it never concatenates. */
function makeStyles(st, intensity) {
    const lead = new Array(2 * RAMP_STOPS * LEAD_STEPS);
    const trail = new Array(2 * RAMP_STOPS * TRAIL_STEPS);
    for (let k = 0; k < 2; k++) {
        for (let r = 0; r < RAMP_STOPS; r++) {
            const lc = LEAD_RAMPS[k][r], tc = TRAIL_RAMPS[k][r];
            for (let s = 0; s < LEAD_STEPS; s++) {
                const a = LEAD_A * ((s + 1) / LEAD_STEPS) * intensity;
                lead[(k * RAMP_STOPS + r) * LEAD_STEPS + s] = `rgba(${lc[0]},${lc[1]},${lc[2]},${a.toFixed(4)})`;
            }
            for (let s = 0; s < TRAIL_STEPS; s++) {
                const a = TRAIL_PEAK * ((s + 1) / TRAIL_STEPS) * intensity;
                trail[(k * RAMP_STOPS + r) * TRAIL_STEPS + s] = `rgba(${tc[0]},${tc[1]},${tc[2]},${a.toFixed(4)})`;
            }
        }
    }
    st.leadStyles = lead; st.trailStyles = trail; st._styleI = intensity;
}

/** Re-roll a column: sub-population, speed, size envelope, and a fresh buffer. */
function respawn(st, c, env, initial) {
    const rand = env.rand;
    const ledger = rand() < LEDGER_SHARE;
    c.kind = ledger ? KIND_LEDGER : KIND_NOISE;
    c.trail = ledger ? LEDGER_TRAIL : NOISE_TRAIL;
    // Per-column envelope multiplier — the size curve is shaped, this makes it
    // shaped DIFFERENTLY for every column. Ledger columns run a touch larger.
    c.envSize = ledger ? (0.94 + rand() * 0.26) : (0.76 + rand() * 0.26);
    // Rows per second. Ledger columns fall slowly enough to be read; noise
    // columns whip past at roughly the shipped Matrix rate.
    c.sp = st.pitch * (ledger ? (1.6 + rand() * 1.4) : (3.4 + rand() * 4.2));
    c.tail = rand() * env.height * 0.3;
    c.y = initial ? rand() * -env.height : -(1 + c.trail) * st.pitch - rand() * 60;
    c.row = Math.floor(c.y / st.pitch);
    c.word = -1; c.wp = 0; c.gap = 1 + ((rand() * 5) | 0);
    c.flare = 0; c.hold = 0; c.hp = 0;
    c.lb = SIZE_BUCKETS >> 1; c.tb = Math.max(0, c.lb - 1); c.la = LEAD_STEPS - 1; c.si = c.kind * RAMP_STOPS;
    for (let d = 0; d < MAX_TRAIL; d++) c.buf[d] = NOISE[(rand() * NOISE.length) | 0];
}

/** Advance the column by one row: the next id character, or noise. */
function pushGlyph(st, c, rand) {
    let g;
    if (c.word >= 0) {
        const seq = st.seqs[c.word];
        g = seq[c.wp++];
        if (c.wp >= seq.length) {                       // id complete → dissolve back into noise
            c.word = -1; c.wp = 0;
            c.gap = 2 + ((rand() * 9) | 0);
            if (rand() < 0.35) c.hold = HOLD_MIN + rand() * HOLD_SPAN;   // beat: let it be read
        }
    } else {
        g = NOISE[(rand() * NOISE.length) | 0];
        if (c.kind === KIND_LEDGER && --c.gap <= 0) { c.word = (rand() * st.seqs.length) | 0; c.wp = 0; }
    }
    c.hp = (c.hp + 1) % MAX_TRAIL;
    c.buf[c.hp] = g;
    c.flare = 1;                                        // fresh character resolves bright, then settles
}

function init(env) {
    const st = env.state, rand = env.rand;
    ensureIds(env);
    // GEOMETRY from the CSS box only. apparentScale() takes no dpr argument on
    // purpose — glyphs tracking devicePixelRatio is the shipped bug this engine
    // spent a release removing.
    st.size = Math.max(12, Math.min(21, Math.round(14 * apparentScale(env.width, env.height))));
    st.pitch = Math.round(st.size * 1.12);
    const cols = Math.max(1, Math.min(MAX_COLS, Math.ceil(env.width / st.size)));
    st.colw = env.width > 0 ? env.width / cols : st.size;
    st.fonts = new Array(SIZE_BUCKETS);
    for (let b = 0; b < SIZE_BUCKETS; b++) {
        const mult = SIZE_MIN + (SIZE_MAX - SIZE_MIN) * (b / (SIZE_BUCKETS - 1));
        st.fonts[b] = `${Math.max(8, Math.round(st.size * mult))}px ui-monospace,monospace`;
    }
    st.cols = [];
    for (let i = 0; i < cols; i++) {
        const c = { buf: new Int16Array(MAX_TRAIL) };
        respawn(st, c, env, true);
        st.cols.push(c);
    }
    makeStyles(st, env.intensity);
}

export const descriptor = {
    id: 'ledger',
    label: 'Ledger Rain',
    // NOT additive: the rain is drawn over its own smear and 'lighter' would let
    // the trail accumulate to white behind the chat text.
    blend: 'source-over',
    // Deep-red-black smear at Matrix's exact 0.085 — same persistence, so the
    // rows an id has already passed keep decaying instead of being redrawn.
    clearPolicy: 'fade:rgba(9,4,5,0.085)',

    /** Injected by the host. Never fetched here. */
    ids: DEFAULT_IDS,

    init,
    // Column count is width-derived, so a resize must rebuild the grid.
    resize: init,

    update(dt, env) {
        const st = env.state;
        if (!st.cols || !st.cols.length) init(env);
        ensureIds(env);
        const rand = env.rand, pitch = st.pitch, h = env.height;
        const k = (1 + env.surge * 0.8) * (0.6 + 0.6 * Math.min(env.scale, 2));
        const decay = dt / FLARE_T;
        for (let i = 0; i < st.cols.length; i++) {
            const c = st.cols[i];
            c.flare = c.flare > decay ? c.flare - decay : 0;
            if (c.hold > 0) {
                c.hold -= dt;                            // a completed id sits still for a beat
                if (c.hold < 0) c.hold = 0;              // expire cleanly — never leave a negative timer behind
            } else {
                c.y += c.sp * k * dt;                    // delta-timed: same speed at 60 and 120 Hz
            }
            const row = Math.floor(c.y / pitch);
            if (row - c.row > MAX_TRAIL) c.row = row - MAX_TRAIL;   // post-stall guard: bound the catch-up
            while (c.row < row) { c.row++; pushGlyph(st, c, rand); }
            // Per-frame draw parameters live on the column so draw() only reads.
            const p = h > 0 ? (c.y > 0 ? Math.min(1, c.y / h) : 0) : 0;
            c.si = c.kind * RAMP_STOPS + (p >= 1 ? RAMP_STOPS - 1 : (p * RAMP_STOPS) | 0);
            c.lb = bucket(sizeCurve(p) * c.envSize);
            c.tb = c.lb > 0 ? c.lb - 1 : 0;              // trail one size step behind the head
            const la = Math.floor((0.62 + 0.38 * c.flare) * LEAD_STEPS) - 1;
            c.la = la < 0 ? 0 : (la > LEAD_STEPS - 1 ? LEAD_STEPS - 1 : la);
            if (c.y > h + c.tail) respawn(st, c, env, false);
        }
    },

    draw(ctx, env) {
        const st = env.state;
        if (!st.cols) return;
        if (st._styleI !== env.intensity) makeStyles(st, env.intensity);
        ctx.textBaseline = 'top';
        const cols = st.cols, pitch = st.pitch, h = env.height;
        const rot = (env.now / 70) | 0;                  // rotates the dissolve, no per-frame state
        // Grouped by font: `ctx.font =` is the one genuinely expensive canvas
        // state change in a text field, so the passes below set it at most
        // 2 x SIZE_BUCKETS times per frame instead of once per column. The scan
        // is integer compares over a pooled array — it allocates nothing.
        for (let role = 0; role < 2; role++) {
            for (let b = 0; b < SIZE_BUCKETS; b++) {
                let fontSet = false;
                for (let i = 0; i < cols.length; i++) {
                    const c = cols[i];
                    if ((role === 0 ? c.lb : c.tb) !== b) continue;
                    const x = i * st.colw, y0 = c.row * pitch;
                    if (role === 0) {
                        if (y0 < -pitch || y0 > h) continue;
                        if (!fontSet) { ctx.font = st.fonts[b]; fontSet = true; }
                        ctx.fillStyle = st.leadStyles[c.si * LEAD_STEPS + c.la];
                        ctx.fillText(GLYPHS[c.buf[c.hp]], x, y0);
                    } else {
                        const steps = WSTEP[c.trail];
                        for (let d = 1; d <= c.trail; d++) {
                            const s = steps[d - 1];
                            if (s < 0) continue;
                            const y = y0 - d * pitch;
                            if (y < -pitch || y > h) continue;
                            if (!fontSet) { ctx.font = st.fonts[b]; fontSet = true; }
                            const gi = d >= DISSOLVE_AT
                                ? NOISE[(rot + i * 7 + d * 13) % NOISE.length]   // tail dissolves back to noise
                                : c.buf[(c.hp - d + MAX_TRAIL) % MAX_TRAIL];
                            ctx.fillStyle = st.trailStyles[c.si * TRAIL_STEPS + s];
                            ctx.fillText(GLYPHS[gi], x, y);
                        }
                    }
                }
            }
        }
    },

    rearm(env) { if (!env.state.cols || !env.state.cols.length) init(env); },
};

registerEffect(descriptor);
export default descriptor;
// Exported for the headless physics test (ledger.test.mjs).
export {
    GLYPHS, NOISE, DEFAULT_IDS, WSTEP, MAX_TRAIL, MAX_COLS, LEDGER_TRAIL, NOISE_TRAIL,
    KIND_LEDGER, KIND_NOISE, LEAD_A, TRAIL_BUDGET, SIZE_BUCKETS,
};
