/**
 * Effect: BOKEH DEPTH (id 'bokeh').
 *
 * Huge, softly defocused discs loll in the foreground while razor-sharp pinpricks
 * of dust creep past far behind them. The depth is not faked with a blur filter —
 * it is FOUR correlated parallax planes (the star field has three) where size,
 * alpha, speed, lifetime and SPRITE FORM all move together:
 *
 *     near  → huge, dim, slow, long-lived, ANNULUS
 *     far   → tiny, bright, quick, short-lived, solid blob
 *
 * The annulus is the whole trick. A defocused point light does not image as a
 * glowing dot, it images as the lens aperture: a disc with a bright RIM and a
 * flat, dim interior. Drawing the same solid radial blob at 190 px just looks
 * like a smudge; the rim is what reads as "out of focus" instead of "big glow".
 * So this module bakes ONE new sprite form (bakeDonut) and reuses the existing
 * solid-blob bake (sprites.js makeSprite) for the far planes — both across the
 * same five-stop ramp so all four planes share one palette.
 *
 * LEGIBILITY (see LEGIBILITY below): this is a backdrop behind chat text, and the
 * near discs are enormous — a 190 px disc at a "modest-looking" 15% alpha would
 * put more light on the screen than the body text does. Alpha IS the entire
 * budget here: every large disc is hard-capped under 6%, and alphaOf() clamps to
 * the plane cap so no envelope, breathe or intensity gain can ever lift it.
 *
 * Geometry comes from apparentScale(CSS box) and NEVER from devicePixelRatio —
 * see sizing.js for the shipped bug that rule exists to prevent.
 */
import { registerEffect } from '../registry.js';
import { apparentScale } from '../sizing.js';
import { makeSprite } from '../sprites.js';

/**
 * Colour ramp, indexed by LIFE (premium rule: never a flat colour). A disc is
 * born cool blue-white — the far side of focus — and warms as it ages, ending
 * near the Portal accent. Real lenses do exactly this at the edges of the frame
 * (longitudinal chromatic aberration), so the drift reads as optics, not as a
 * colour cycle.
 */
export const BOKEH_RAMP = [
    [188, 212, 255],   // cool blue-white
    [214, 226, 255],   // periwinkle
    [240, 236, 228],   // neutral warm-white
    [255, 214, 168],   // champagne
    [255, 172, 142],   // warm rose
];

/**
 * The four planes, declared FAR → NEAR so the array order is also the painter's
 * order (dust behind, big discs in front). Compositing is additive and therefore
 * commutative, so this is for the reader, not for the pixels.
 *
 * `r` and `speed` are in REFERENCE px (the 900 px short edge sizing.js is tuned
 * at); both are multiplied by apparentScale at use, so the field looks and moves
 * the same on a phone and on a 4K monitor.
 *
 * Note the parallax is deliberately INVERTED from a camera pan (where near moves
 * fastest): the art direction is a lazy defocused foreground with dust hurrying
 * past behind it. Slow-and-huge in front is what sells "lens", not "landscape".
 */
export const PLANES = [
    // id       count  r (ref px)    alpha   speed (ref px/s)  life (s)   bob   donut
    { id: 'dust',  count: 46, r: [0.9, 2.0], alpha: 0.42,  speed: [34, 48], life: [7, 15],  bob: 11, donut: false },
    { id: 'grain', count: 18, r: [4.0, 9.0], alpha: 0.13,  speed: [20, 29], life: [12, 22], bob: 9,  donut: false },
    { id: 'inner', count: 9,  r: [22, 44],   alpha: 0.052, speed: [11, 17], life: [22, 38], bob: 7,  donut: true },
    { id: 'near',  count: 5,  r: [48, 96],   alpha: 0.042, speed: [6, 10],  life: [30, 52], bob: 5,  donut: true },
];

/**
 * The published legibility budget. Exported so the test can assert it rather
 * than restate it — a budget that lives only in a comment is not a budget.
 *
 *   largeDiscPx   at or above this DRAWN radius a disc covers text, not gaps
 *   nearAlphaCap  ...so its alpha must stay under this. Both near planes are
 *                 configured below it and alphaOf() clamps, so it cannot drift.
 *   inkBudget     Σ alpha·πr² over the whole field, as a fraction of the
 *                 viewport. A conservative over-estimate (every sprite falls off
 *                 to zero long before its nominal radius), so the real wash is
 *                 lower than this measures. Set just above the measured worst
 *                 case (0.97% over six seeds x four viewports) so that adding a
 *                 plane or lifting an alpha TRIPS the test instead of shipping.
 */
export const LEGIBILITY = { largeDiscPx: 24, nearAlphaCap: 0.06, inkBudget: 0.0125 };

const FADE_IN = 0.12;    // fraction of life spent fading up
const FADE_OUT = 0.28;   // ...and down. Long, because a big dim disc popping is very visible.

const SPAWN_STAGGER = 0; // first fill / re-target: anywhere on screen, MID-life
const SPAWN_DRIFT = 1;   // life expired: anywhere on screen, but fresh, so it fades in from 0
const SPAWN_EDGE = 2;    // walked off the left: re-enter from the right, fresh

/**
 * Size over life (premium rule: never a constant size). A disc swells as it ages
 * — the imaginary lens racking further out of focus — easing out so the growth
 * is fastest while it is still faint. p.sizeEnv then scatters the whole curve
 * per particle, so two discs with the same base radius never track each other.
 */
function sizeCurve(life) {
    const a = 1 - life;                    // age, 0 → 1
    return 0.74 + 0.46 * a * (2 - a);      // 0.74 at birth → 1.20 at death
}

/** Drawn radius in CSS px. `geo` is apparentScale of the CSS box — never dpr. */
export function radiusOf(p, geo) {
    return p.baseR * (geo || 1) * p.sizeEnv * sizeCurve(p.life);
}

/**
 * Drawn alpha. The plane's alpha is a CEILING, not a target: fade envelope,
 * breathe and env.intensity may only ever take light away. That clamp is what
 * makes the legibility budget an invariant instead of a hope.
 */
export function alphaOf(p, env) {
    const cap = p.plane.alpha;
    const a = 1 - p.life;
    const fadeIn = a < FADE_IN ? a / FADE_IN : 1;
    const fadeOut = p.life < FADE_OUT ? p.life / FADE_OUT : 1;
    const breathe = 0.82 + 0.18 * Math.sin(env.now * 0.0006 + p.fade);   // ~10 s, lazy
    const gain = Number.isFinite(env.intensity) ? env.intensity : 1;
    const raw = cap * fadeIn * fadeOut * breathe * gain;
    return raw > 0 ? (raw < cap ? raw : cap) : 0;
}

/** Ramp stop for this particle right now: per-particle start + a warm drift with age. */
export function rampIndexOf(p) {
    const i = p.hue0 + Math.round((1 - p.life) * 2);
    return i < 0 ? 0 : (i > BOKEH_RAMP.length - 1 ? BOKEH_RAMP.length - 1 : i);
}

// ---------------------------------------------------------------------------
// Sprites — baked once per document, lazily, so a headless import never touches
// `document` (draw() simply no-ops until an atlas exists, exactly like embers).
// ---------------------------------------------------------------------------
let ATLAS = null;

/**
 * The ANNULUS. Centre alpha is 0.10 rather than 0 — a truly hollow centre reads
 * as a soap bubble, whereas a dim, flat interior with a hot rim at 0.93 reads as
 * a defocused aperture. Baked at 128 px because the near plane magnifies it to
 * ~190 px and a 64 px source visibly softens the one edge that carries the whole
 * illusion.
 */
function bakeDonut(size, r, g, b) {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const x = c.getContext('2d');
    const gr = x.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gr.addColorStop(0, `rgba(${r},${g},${b},0.10)`);
    gr.addColorStop(0.55, `rgba(${r},${g},${b},0.16)`);
    gr.addColorStop(0.82, `rgba(${r},${g},${b},0.55)`);
    gr.addColorStop(0.93, `rgba(${r},${g},${b},1)`);     // the rim
    gr.addColorStop(0.985, `rgba(${r},${g},${b},0.35)`);
    gr.addColorStop(1, `rgba(${r},${g},${b},0)`);
    x.fillStyle = gr;
    x.fillRect(0, 0, size, size);
    return c;
}

function ensureAtlas() {
    if (ATLAS) return ATLAS;
    if (typeof document === 'undefined') return null;
    ATLAS = {
        donut: BOKEH_RAMP.map(c => bakeDonut(128, c[0], c[1], c[2])),
        // The far planes want the EXISTING solid blob — one shared bake, one
        // palette. Small (48 px) and drawn tiny, which is what keeps the dust
        // reading as a pinprick rather than a smudge.
        solid: BOKEH_RAMP.map(c => makeSprite(48, c[0], c[1], c[2])),
    };
    return ATLAS;
}

// ---------------------------------------------------------------------------
// Field
// ---------------------------------------------------------------------------

/** Re-roll a particle IN PLACE. Nothing here allocates — this is the recycler. */
function resetParticle(p, env, mode) {
    const rand = env.rand, pl = p.plane, geo = env.state.geo || 1;
    p.baseR = pl.r[0] + rand() * (pl.r[1] - pl.r[0]);
    p.sizeEnv = 0.82 + rand() * 0.42;                                    // per-particle size envelope
    p.vx = -(pl.speed[0] + rand() * (pl.speed[1] - pl.speed[0])) * geo;  // ALWAYS leftward: the plane drift is the parallax cue
    p.bobAmp = pl.bob * (0.6 + rand() * 0.8);
    p.bobRate = 0.09 + rand() * 0.16;                                    // rad/s — a slow lolling, not a wobble
    p.bobPhase = rand() * 6.283;
    p.fade = rand() * 6.283;
    p.hue0 = (rand() * 3) | 0;                                           // 0..2 — where on the ramp this one starts
    p.decay = 1 / (pl.life[0] + rand() * (pl.life[1] - pl.life[0]));
    // Only the FIRST fill starts mid-life. A life-expiry respawn must start at 1
    // so the fade-in envelope hides it; otherwise discs pop into existence at
    // full brightness in the middle of the screen.
    p.life = mode === SPAWN_STAGGER ? 0.06 + rand() * 0.94 : 1;
    p.x = mode === SPAWN_EDGE ? env.width + p.baseR * geo * 1.35 + 8 : rand() * env.width;
    p.y = rand() * env.height;
    return p;
}

function makeParticle(plane, env, mode) {
    return resetParticle({
        plane, x: 0, y: 0, vx: 0, baseR: 1, sizeEnv: 1,
        bobAmp: 0, bobRate: 0, bobPhase: 0, fade: 0, hue0: 0, decay: 0, life: 1,
    }, env, mode);
}

/**
 * Resolve geometry + population for the current viewport.
 *
 * The change guard matters: resize() fires on every composer keystroke that
 * changes the chat height. Rebuilding the pool there would re-scatter the field
 * mid-sentence. So unless a per-plane COUNT actually moved, this only refreshes
 * the geometry scale and returns, leaving every particle exactly where it was.
 */
function retarget(env, mode) {
    const st = env.state;
    st.geo = apparentScale(env.width, env.height);
    if (!st.counts) st.counts = PLANES.map(() => -1);
    const density = Number.isFinite(env.scale) && env.scale > 0 ? env.scale : 1;
    let changed = !st.list;
    for (let i = 0; i < PLANES.length; i++) {
        const want = Math.max(1, Math.round(PLANES[i].count * density));
        if (st.counts[i] !== want) { st.counts[i] = want; changed = true; }
    }
    if (!changed) return;
    const old = st.list || [];
    const next = [];                                    // resize-only allocation; never per frame
    for (let i = 0; i < PLANES.length; i++) {
        const plane = PLANES[i];
        let kept = 0;
        for (let j = 0; j < old.length && kept < st.counts[i]; j++) {
            if (old[j].plane === plane) { next.push(old[j]); kept++; }   // survivors keep their state
        }
        while (kept < st.counts[i]) { next.push(makeParticle(plane, env, mode)); kept++; }
    }
    st.list = next;
}

function init(env) {
    const st = env.state;
    st.list = null;
    st.counts = null;
    retarget(env, SPAWN_STAGGER);
}

export const descriptor = {
    id: 'bokeh',
    label: 'Bokeh Depth',
    // Out-of-focus highlights are LIGHT: two discs overlapping are brighter than
    // one. At these alphas additive is also the safe choice — the whole field
    // sums to a fraction of the body-text luminance (see LEGIBILITY).
    blend: 'lighter',
    // Hard clear. A fade would smear the discs into comet trails, which is the
    // opposite of a still, defocused plate — and the near discs move so slowly
    // that any persistence reads as ghosting.
    clearPolicy: 'clear',

    init,
    // Geometry always refreshes; the population only rebuilds if a count moved.
    resize(env) { if (env.state.list) retarget(env, SPAWN_STAGGER); else init(env); },

    update(dt, env) {
        const st = env.state;
        if (!st.list) init(env);
        // Clamp the step. A backgrounded tab hands back one enormous dt, and at
        // these speeds a 4 s jump would teleport the entire field sideways.
        const d = dt > 0 ? (dt < 0.1 ? dt : 0.1) : 0;
        const t = env.now * 0.001;
        const geo = st.geo || 1;
        const list = st.list;
        for (let i = 0; i < list.length; i++) {
            const p = list[i];
            p.life -= p.decay * d;
            if (p.life <= 0) { resetParticle(p, env, SPAWN_DRIFT); continue; }
            p.x += p.vx * d;                                                  // delta-timed: same speed at 60 and 120 Hz
            p.y += Math.sin(t * p.bobRate + p.bobPhase) * p.bobAmp * d;       // vertical loll, zero net drift
            // Recycle on the DRAWN radius: a 190 px disc must be fully clear of
            // the edge, or it visibly winks out with a third of it on screen.
            if (p.x < -(p.baseR * geo * 1.35 + 8)) resetParticle(p, env, SPAWN_EDGE);
        }
    },

    draw(ctx, env) {
        const st = env.state;
        const atlas = ensureAtlas();
        if (!atlas || !st.list) return;                  // no document (headless test) → nothing to draw
        const geo = st.geo || 1;
        const list = st.list;
        for (let i = 0; i < list.length; i++) {
            const p = list[i];
            const al = alphaOf(p, env);
            if (al < 0.0008) continue;                   // below the 8-bit floor: pure cost, no pixels
            const r = radiusOf(p, geo);
            const s = (p.plane.donut ? atlas.donut : atlas.solid)[rampIndexOf(p)];
            ctx.globalAlpha = al;
            ctx.drawImage(s, p.x - r, p.y - r, r * 2, r * 2);
        }
    },

    // A backdrop must not blink when a turn starts: top the population back up
    // and otherwise leave the field mid-flight, exactly where the user left it.
    rearm(env) { if (env.state.list) retarget(env, SPAWN_DRIFT); else init(env); },
};

registerEffect(descriptor);
export default descriptor;
