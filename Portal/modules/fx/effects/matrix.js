/**
 * Effect: MATRIX (id 'matrix').
 *
 * Column rain: translucent trail fade, bright leading glyph + dim green trail,
 * per-column speed variance, katakana + digits.
 *
 * Transcribed from ember-fx.js's drawMatrix(). The only mechanical change is the
 * update/draw split — the column advance now happens in update() (before the
 * frame is drawn) instead of after drawing. Same motion, same speed; the field
 * is one frame (~16 ms) further along than it used to be at the same timestamp,
 * which is not observable.
 */
import { registerEffect } from '../registry.js';

// Glyph set: katakana + digits/symbols. Pure data, built once at import — no DOM,
// no randomness, so this module stays importable headlessly.
const CHARS = [];
for (let c = 0x30A0; c < 0x30FF; c++) CHARS.push(String.fromCharCode(c));
'0123456789ABCDEF<>*+'.split('').forEach(c => CHARS.push(c));

function init(env) {
    const st = env.state, rand = env.rand;
    // Clamp BOTH ends. Only the floor existed, so on a wide monitor (width/78)
    // ran away and produced giant coarse glyphs — the same "everything is huge"
    // symptom as the canvas bug, from a different cause.
    st.size = Math.max(13, Math.min(22, Math.round(env.width / 78)));
    const cols = Math.ceil(env.width / st.size);
    st.cols = [];
    for (let i = 0; i < cols; i++) {
        st.cols.push({ y: rand() * -env.height, sp: st.size * (3 + rand() * 6), last: 0, glyph: null });
    }
}

export const descriptor = {
    id: 'matrix',
    label: 'Matrix',
    // NOT additive: the green rain is drawn over its own smear, and 'lighter'
    // would let the trail accumulate to white behind the chat text.
    blend: 'source-over',
    // translucent trail fade (green-black smear) → glowing falling trails
    clearPolicy: 'fade:rgba(4,7,5,0.085)',

    init,
    // Column count is width-derived, so a resize must rebuild the columns.
    resize: init,

    update(dt, env) {
        const st = env.state;
        if (!st.cols || !st.cols.length) init(env);
        const rand = env.rand;
        const k = (1 + env.surge * 0.8) * (0.6 + 0.6 * Math.min(env.scale, 2));
        for (let i = 0; i < st.cols.length; i++) {
            const c = st.cols[i];
            if (env.now - c.last > 36) { c.glyph = CHARS[(rand() * CHARS.length) | 0]; c.last = env.now; }
            c.y += c.sp * k * dt;
            if (c.y > env.height + rand() * env.height * 0.3) { c.y = rand() * -40; c.sp = st.size * (3 + rand() * 6); }
        }
    },

    draw(ctx, env) {
        const st = env.state;
        if (!st.cols) return;
        ctx.font = `${st.size}px ui-monospace,monospace`;
        ctx.textBaseline = 'top';
        // intensity is constant per frame → build both fillStyles ONCE, not per column.
        const leadStyle = `rgba(200,255,210,${0.9 * env.intensity})`;   // bright leading glyph
        const trailStyle = `hsla(135,90%,55%,${0.5 * env.intensity})`;  // dim green trail glyph
        const drift = (env.now / 90) | 0;
        for (let i = 0; i < st.cols.length; i++) {
            const c = st.cols[i], x = i * st.size;
            ctx.fillStyle = leadStyle;
            ctx.fillText(c.glyph || CHARS[0], x, c.y);
            ctx.fillStyle = trailStyle;
            ctx.fillText(CHARS[(i * 7 + drift) % CHARS.length], x, c.y - st.size);
        }
    },

    rearm(env) { if (!env.state.cols || !env.state.cols.length) init(env); },
};

registerEffect(descriptor);
export default descriptor;
export { CHARS };
