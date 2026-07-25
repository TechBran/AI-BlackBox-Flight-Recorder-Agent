/**
 * Effect: EMBERS (id 'embers').
 *
 * Curl-noise divergence-free swirl, blackbody white-hot -> deep-red ramp indexed
 * by particle life, the pre-rendered sprite atlas drawn twice (faint glow +
 * bright core) under ADDITIVE blend, heat-persistence smear, buoyancy + drag +
 * a spark sub-population.
 *
 * Transcribed from ember-fx.js's drawEmbers() with two mechanical changes and no
 * physics changes:
 *   - the frame's smear (`fade:rgba(8,6,10,0.20)`) and the 'lighter' blend are
 *     now declared, not drawn/set here — the engine applies both.
 *   - integrate/cull moved into update(), drawing into draw(). Order within a
 *     frame is unchanged and additive compositing is commutative, so the frame
 *     is identical.
 */
import { registerEffect } from '../registry.js';

// cheap 2-octave sine curl-noise potential ψ → divergence-free swirl
function pot(x, y, t) {
    return Math.sin(x * 0.0065 + t * 0.22) * Math.cos(y * 0.0065 - t * 0.16)
        + 0.5 * Math.sin(x * 0.013 - t * 0.31) * Math.cos(y * 0.013 + t * 0.26);
}

function spawn(env, n) {
    const rand = env.rand, list = env.state.list;
    for (let i = 0; i < n; i++) {
        const spark = rand() < 0.08, ml = 2.6 + rand() * 3.6;               // long life → floats across
        list.push({
            x: rand() * env.width, y: rand() * env.height,                  // ANYWHERE on screen (not just the bottom)
            vx: (rand() - 0.5) * 16, vy: -(4 + rand() * 14),                // gentle float, not a bottom jet
            life: 1, decay: 1 / ml, r: spark ? (1.2 + rand() * 1.3) : (2.5 + rand() * 6),
            spark, fade: rand() * 6.28, hue0: Math.floor(rand() * 3)        // 0..2 varied warm heat
        });
    }
}

export const descriptor = {
    id: 'embers',
    label: 'Embers',
    // Emissive field: overlapping embers must ADD light, not occlude each other.
    blend: 'lighter',
    // Heat-persistence smear (soft glowing trails). NO bottom ground-glow — real
    // embers float across the WHOLE screen, not a fireball at the base.
    clearPolicy: 'fade:rgba(8,6,10,0.20)',

    init(env) {
        env.state.list = [];
        env.state.acc = 0;
        spawn(env, Math.round(140 * env.scale));
    },

    resize() { /* population is viewport-independent; embers wrap and re-spawn */ },

    update(dt, env) {
        const st = env.state;
        if (!st.list) { st.list = []; st.acc = 0; }
        const ts = env.now * 0.001;
        // keep a full-screen population while generating
        if (env.active) {
            const target = Math.round((150 + env.surge * 160) * env.scale);
            let acc = st.acc + (30 + env.surge * 90) * env.scale * dt;
            while (acc >= 1 && st.list.length < target * 1.3) { spawn(env, 1); acc--; }
            st.acc = acc;
        }
        const eps = 3;
        for (let i = st.list.length - 1; i >= 0; i--) {
            const p = st.list[i];
            p.life -= p.decay * dt;
            if (p.life <= 0) { st.list[i] = st.list[st.list.length - 1]; st.list.pop(); continue; }
            // curl-noise swirl (gentle) + slow float — drifts everywhere, doesn't jet up
            const cvx = (pot(p.x, p.y + eps, ts) - pot(p.x, p.y - eps, ts));
            const cvy = -(pot(p.x + eps, p.y, ts) - pot(p.x - eps, p.y, ts));
            p.vx += cvx * 5200 * dt; p.vy += cvy * 5200 * dt;
            p.vy -= 9 * p.life * dt;                   // gentle buoyancy (slow float up)
            if (p.spark) p.vy += 60 * dt;              // sparks drift down a touch
            p.vx *= (1 - 0.9 * dt); p.vy *= (1 - 0.9 * dt); // drag
            p.x += p.vx * dt; p.y += p.vy * dt;
            // wrap horizontally so the field stays full across the whole width
            if (p.x < -20) p.x = env.width + 20; else if (p.x > env.width + 20) p.x = -20;
        }
        if (st.list.length > 3000) st.list.splice(0, st.list.length - 3000);
    },

    draw(ctx, env) {
        const st = env.state;
        const spr = env.sprites && env.sprites.ember;
        if (!spr || !st.list) return;                  // atlas not baked (headless test) → nothing to draw
        const ts = env.now * 0.001;
        const top = spr.length - 1;
        for (let i = 0; i < st.list.length; i++) {
            const p = st.list[i];
            const breathe = 0.6 + 0.4 * Math.sin(ts * 1.3 + p.fade);      // soft per-ember flicker
            const idx = p.spark ? 0 : Math.max(0, Math.min(top, p.hue0 + Math.round((1 - p.life) * 3)));
            const s = spr[idx];                                           // varied warm heat, cooling as it ages
            const al = (p.spark ? 0.85 : 0.55) * Math.min(1, p.life * 1.4) * breathe * env.intensity;
            // faint big glow + bright core (cheap bloom)
            const grr = p.r * (p.spark ? 3.5 : 4.2); ctx.globalAlpha = al * 0.22; ctx.drawImage(s, p.x - grr, p.y - grr, grr * 2, grr * 2);
            const cr = p.r * (p.spark ? 1.5 : 1.9); ctx.globalAlpha = al; ctx.drawImage(s, p.x - cr, p.y - cr, cr * 2, cr * 2);
        }
    },

    // Fresh rise each turn: the shipped engine seeded the field on activation and
    // then immediately emptied it (applyState wiped `embers` right after
    // ensureFieldReady), so the net shipped behaviour is "start empty and build".
    rearm(env) { env.state.list = []; env.state.acc = 0; },
};

registerEffect(descriptor);
export default descriptor;
