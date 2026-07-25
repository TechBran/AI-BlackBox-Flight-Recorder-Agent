/**
 * Effect: RAINFALL (id 'rain').
 *
 * Fine near-vertical streaks falling at three depths — the near ones long and
 * fast, the far ones short and slow — all leaning together in the wind.
 *
 * VELOCITY-STRETCHED BILLBOARDS. A drop is not a dot: it is a stroked line from
 * its head back to `head - velocity * k`, so streak length is DERIVED from speed
 * rather than authored. Everything that changes a drop's speed (depth layer,
 * per-drop variance, the acceleration toward terminal velocity after it enters
 * frame) shows up in the geometry for free. No sprite, no gradient, no blur —
 * the motion cue is the whole effect.
 *
 * PER-COLUMN WIND SHEAR. A single uniform lean angle is the cheap-version
 * giveaway: it reads as a hatch pattern, not as weather. The lean here is a
 * continuous standing wave across x that also breathes slowly in time, plus a
 * per-drop jitter, so no two drops are exactly parallel while the field still
 * leans as one.
 *
 * LEGIBILITY (non-negotiable — this is a BACKDROP behind chat text). Vertical
 * streaks run parallel to both the message column and the scroll direction, the
 * one geometry that competes directly with reading. Two mitigations, both
 * asserted in rain.test.mjs rather than left as good intentions:
 *   - the lean is held inside 10-15 degrees off vertical (base 12.5 +/- shear
 *     +/- jitter), enough to break the parallel with the text column;
 *   - the near plane never exceeds NEAR_ALPHA_CEILING (25%) alpha, heavy drops
 *     included.
 *
 * clearPolicy is 'clear', NOT a fade. The velocity stretch already draws the
 * trail; a translucent fade would smear a second, softer copy of every streak
 * AND leave a permanent tint sitting between the text and the page background.
 * Trails are for effects whose particles are points. These are not.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';

const TAU = Math.PI * 2;
const DEG = Math.PI / 180;

/**
 * The legibility budget, as a number other code can read. The near plane is the
 * only layer bright enough to matter; keep it under this and body text keeps its
 * contrast. `layers[2].alpha * heavy.alpha` must stay below it — asserted.
 */
export const NEAR_ALPHA_CEILING = 0.25;

/** Hard population ceiling, whatever the viewport asks for. */
const MAX_DROPS = 900;

/** Bands above/below the viewport where drops fade in and out, in CSS px. */
const TOP_BAND = 60;
const BOT_BAND = 60;

/**
 * Tunables. `lean` and `density` are deliberately exposed as the two dials a
 * future weather mode drives: swing `lean` toward 15 and push `density` past 1
 * and the same simulation is a squall; drop them and it is drizzle. Everything
 * else is art direction that should NOT move with the weather.
 */
export const RAIN_CONFIG = {
    lean: 12.5,          // degrees off vertical, the whole field's base angle
    leanShear: 2.0,      // +/- degrees of per-column shear on top of the base
    leanJitter: 0.35,    // +/- degrees of per-drop jitter, so a column is not a hatch
    shearHz: 0.055,      // how slowly the shear pattern breathes (cycles/sec)
    shearBands: 19,      // gust bands across the viewport width
    density: 1,          // population multiplier — the second weather dial
    streak: 0.095,       // SECONDS of travel a streak represents (length = |v| * this)
    // Three depths. Far reads as haze, near as rain. Speed drives length, so the
    // speed spread IS the depth cue; alpha and width only reinforce it.
    layers: [
        { count: 130, speed: [130, 190], width: 0.55, alpha: 0.085, len: 0.55, heavy: 0,    tint: 0 },
        { count: 80,  speed: [300, 390], width: 0.90, alpha: 0.140, len: 0.85, heavy: 0.05, tint: 1 },
        { count: 42,  speed: [560, 700], width: 1.45, alpha: 0.190, len: 1.15, heavy: 0.09, tint: 2 },
    ],
    // Sub-population: the occasional fat drop, in the two closer planes only.
    // alpha stays multiplied UNDER the ceiling — a heavy drop is bigger, not brighter.
    heavy: { speed: 1.18, width: 1.5, alpha: 1.15, len: 1.25 },
};

/**
 * Colour ramp, indexed by fall progress. Pale hazy blue-white at the top (rain
 * high in the frame is lit and far away) deepening to slate at the bottom.
 * A drop's layer biases its STARTING stop, so the far plane also sits at the
 * pale end for its whole life — aerial perspective and life-ramp on one axis.
 */
export const RAIN_RAMP = [
    [226, 240, 250], [198, 222, 242], [168, 202, 232], [138, 180, 220],
    [110, 156, 202], [86, 130, 180], [66, 106, 152],
];
// Baked ONCE at import: draw() indexes this array, it never builds a colour
// string. Pure data, no DOM, so the module stays importable headlessly.
const RAIN_CSS = RAIN_RAMP.map(c => `rgb(${c[0]},${c[1]},${c[2]})`);
const RAMP_TOP = RAIN_RAMP.length - 1;

/**
 * Size-over-life curve for the streak. Rises fast out of the dark band above the
 * viewport, holds through the readable middle, tapers as the drop leaves the
 * bottom. Multiplied by a PER-DROP envelope (`lenMul`) so two drops at the same
 * speed and the same height are still different lengths.
 * @param {number} u fall progress, 0 at the top band, 1 at the bottom band
 */
export function lengthEnvelope(u) {
    const arrive = u < 0.16 ? u / 0.16 : 1;
    const leave = u > 0.88 ? (1 - u) / 0.12 : 1;
    return (0.5 + 0.5 * arrive * (2 - arrive)) * (0.78 + 0.22 * leave);
}

/**
 * Alpha over life. Zero at both ends — a drop must never pop into or out of
 * existence at the edge of the viewport, which is the tell that a field is
 * recycling rather than falling.
 */
export function alphaEnvelope(u) {
    const inK = u < 0.08 ? u / 0.08 : 1;
    const outK = u > 0.82 ? (1 - u) / 0.18 : 1;
    return inK * outK;
}

/** Two multiplied waves → a shear field with no repeating period you can read. */
function shearWave(bx, t) {
    return Math.sin(bx + t * TAU * RAIN_CONFIG.shearHz)
        * Math.cos(bx * 0.61 - t * TAU * RAIN_CONFIG.shearHz * 0.61);
}

/** One fully-shaped drop. Built once per pool slot; never reallocated. */
function newDrop() {
    return {
        layer: 0, x: 0, y: 0, tx: 0, ty: 0, vx: 0, vy: 0, vt: 0,
        u: 0, wid: 0, a: 0, ramp: 0, lean: 0,
        lenMul: 1, widMul: 1, alphaMul: 1, tint: 0, jit: 0, heavy: false, dead: false,
    };
}

/**
 * Recycle a drop back to the top band. `initial` staggers it anywhere down the
 * fall instead, so the first frame is already a full field rather than a curtain
 * dropping in from above.
 */
function reset(p, env, initial) {
    const rand = env.rand, g = env.state.g;
    const L = RAIN_CONFIG.layers[p.layer];
    const H = RAIN_CONFIG.heavy;
    const heavy = rand() < L.heavy;
    p.heavy = heavy;
    // Terminal velocity in CSS px/s. `g` is apparentScale — GEOMETRY only, and it
    // never sees devicePixelRatio (see sizing.js; that was a shipped bug).
    p.vt = (L.speed[0] + rand() * (L.speed[1] - L.speed[0])) * (heavy ? H.speed : 1) * g;
    p.vy = p.vt * 0.72;                                    // enters slower → the streak GROWS as it falls
    p.lean = RAIN_CONFIG.lean;
    p.vx = p.vy * Math.tan(RAIN_CONFIG.lean * DEG);
    p.lenMul = L.len * (heavy ? H.len : 1) * (0.8 + rand() * 0.45);
    p.widMul = L.width * (heavy ? H.width : 1) * (0.85 + rand() * 0.3) * g;
    p.alphaMul = L.alpha * (heavy ? H.alpha : 1);
    p.tint = L.tint + ((rand() * 1.999) | 0);              // layer-biased ramp entry point
    p.jit = (rand() - 0.5) * 2 * RAIN_CONFIG.leanJitter;
    p.x = rand() * (env.width + 120) - 60;
    p.y = initial
        ? rand() * (env.height + TOP_BAND + BOT_BAND) - TOP_BAND
        : -TOP_BAND - rand() * (env.height * 0.25 + 40);
    p.u = 0; p.wid = 0; p.a = 0; p.ramp = p.tint;
    p.tx = p.x; p.ty = p.y;
    p.dead = false;
}

/**
 * (Re)build the pool for the current viewport. Runs at init and on resize ONLY —
 * never per frame. Slots are reused in place so a resize does not churn the heap.
 */
function build(env) {
    const st = env.state;
    st.g = apparentScale(env.width, env.height);
    st.uk = 1 / Math.max(1, env.height + TOP_BAND + BOT_BAND);
    const d = RAIN_CONFIG.density * Math.max(0.35, Math.min(2.4, env.scale || 1));
    const drops = st.drops || (st.drops = []);
    let n = 0;
    for (let li = 0; li < RAIN_CONFIG.layers.length; li++) {
        const want = Math.max(1, Math.round(RAIN_CONFIG.layers[li].count * d));
        for (let i = 0; i < want && n < MAX_DROPS; i++, n++) {
            const p = drops[n] || (drops[n] = newDrop());
            p.layer = li;
            reset(p, env, true);
        }
    }
    if (drops.length > n) drops.length = n;
    st.count = n;
    st.live = n;
}

export const descriptor = {
    id: 'rain',
    label: 'Rainfall',
    // NOT additive: rain is not emissive. Under 'lighter' every crossing streak
    // would sum toward white and eat the contrast of the chat text on top.
    blend: 'source-over',
    // See the header: the velocity stretch IS the trail. A fade would double it
    // and tint the page.
    clearPolicy: 'clear',

    init: build,
    // Population, geometry scale and the fall-progress denominator are all
    // viewport-derived, so a resize has to rebuild.
    resize: build,

    update(dt, env) {
        const st = env.state;
        if (!st.drops) build(env);
        const drops = st.drops, n = st.count;
        const t = env.now * 0.001;
        const bandK = TAU * RAIN_CONFIG.shearBands / Math.max(1, env.width);
        const recycleY = env.height + BOT_BAND;
        const active = env.active !== false;
        const k = RAIN_CONFIG.streak;
        const inten = env.intensity;
        // Exponential approach to terminal velocity, clamped so a long stall
        // cannot overshoot past the target.
        const accelK = Math.min(1, 2.2 * dt);
        let live = 0;

        for (let i = 0; i < n; i++) {
            const p = drops[i];
            if (p.dead) continue;

            // Wind: base lean + continuous per-column shear + per-drop jitter.
            const shear = shearWave(p.x * bandK, t);
            const lean = RAIN_CONFIG.lean + RAIN_CONFIG.leanShear * shear + p.jit;
            p.lean = lean;
            p.vy += (p.vt - p.vy) * accelK;
            p.vx = p.vy * Math.tan(lean * DEG);
            p.x += p.vx * dt;
            p.y += p.vy * dt;

            if (p.y > recycleY) {
                // Idle: let the field drain instead of raining over a quiet chat.
                if (!active) { p.dead = true; p.a = 0; continue; }
                reset(p, env, false);
            } else if (p.x < -60) {
                p.x += env.width + 120;                    // wrap BEFORE the tail is derived,
            } else if (p.x > env.width + 60) {             // or the streak spans the viewport for a frame
                p.x -= env.width + 120;
            }
            live++;

            // Fall progress drives every per-life curve. Spatial, not a timer, so
            // the fade-out lands exactly on the recycle line — no pop.
            let u = (p.y + TOP_BAND) * st.uk;
            if (u < 0) u = 0; else if (u > 1) u = 1;
            p.u = u;
            const le = lengthEnvelope(u);
            const kEff = k * p.lenMul * le;                 // seconds of travel this streak shows
            p.tx = p.x - p.vx * kEff;
            p.ty = p.y - p.vy * kEff;
            p.wid = p.widMul * (0.65 + 0.35 * le);
            p.a = p.alphaMul * alphaEnvelope(u) * inten;
            const idx = p.tint + ((u * 3 + 0.5) | 0);
            p.ramp = idx > RAMP_TOP ? RAMP_TOP : idx;
        }
        st.live = live;
    },

    draw(ctx, env) {
        const st = env.state;
        if (!st.drops) return;
        const drops = st.drops, n = st.count;
        ctx.lineCap = 'round';
        for (let i = 0; i < n; i++) {
            const p = drops[i];
            if (p.dead || p.a <= 0.002) continue;          // skip the invisible ends of the fade
            ctx.globalAlpha = p.a;
            ctx.strokeStyle = RAIN_CSS[p.ramp];
            ctx.lineWidth = p.wid;
            ctx.beginPath();
            ctx.moveTo(p.tx, p.ty);
            ctx.lineTo(p.x, p.y);
            ctx.stroke();
        }
    },

    // Rain resumes as a full field, not a drought: revive whatever drained while
    // the chat was idle, staggered down the fall so it does not arrive as a wall.
    rearm(env) {
        const st = env.state;
        if (!st.drops) { build(env); return; }
        const drops = st.drops, n = st.count;
        for (let i = 0; i < n; i++) if (drops[i].dead) reset(drops[i], env, true);
        st.live = n;
    },
};

registerEffect(descriptor);
export default descriptor;
