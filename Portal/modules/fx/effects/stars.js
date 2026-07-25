/**
 * Effect: RISING STARS (id 'stars') — the DEFAULT field.
 *
 * The ORIGINAL warm ember-rising field, restored verbatim from the pre-3-mode
 * engine (d125f05^). This is the look Brandon confirmed he wants ("I liked the
 * way it looked before, it was perfect"), so the physics and the draw call below
 * are a transcription, NOT a rewrite: at dpr=1 / 60 fps the output is unchanged
 * by the move into this module.
 *
 * Two deliberate non-changes, both of which look like bugs and are not:
 *   - blend is 'source-over', NOT 'lighter'. The engine has always drawn these
 *     particles under source-over; additive would brighten every overlap and is
 *     a re-art-direction, not a repair.
 *   - drawStarParticle still builds two radial gradients per particle per frame.
 *     Routing it through the baked sprite atlas is a real (and planned) perf win
 *     but it does not produce identical pixels, so it is NOT part of this move.
 */
import { registerEffect } from '../registry.js';

const STAR_CONFIG = {
    layers: [
        { count: 40, speed: 0.3, size: [0.5, 1], opacity: 0.25 }, // far / tiny
        { count: 50, speed: 0.5, size: [1, 2],   opacity: 0.4  }, // mid / small
        { count: 30, speed: 0.8, size: [1.5, 3], opacity: 0.7  }  // fore / medium
    ],
    colors: [
        { r: 255, g: 74, b: 74 }, { r: 255, g: 120, b: 50 }, { r: 255, g: 180, b: 50 },
        { r: 255, g: 220, b: 100 }, { r: 255, g: 250, b: 200 }
    ],
    colorWeights: [0.3, 0.3, 0.2, 0.15, 0.05],
    glowIntensity: 10, turbulence: 0.6, riseSpeed: 0.8, flickerSpeed: 0.015, trailLength: 2
};

function starPickColor(rand) {
    const r = rand(); let cumulative = 0;
    for (let i = 0; i < STAR_CONFIG.colorWeights.length; i++) {
        cumulative += STAR_CONFIG.colorWeights[i];
        if (r < cumulative) return STAR_CONFIG.colors[i];
    }
    return STAR_CONFIG.colors[0];
}

// The randomness source rides on the LAYER object (which already carries the
// live viewport as _w/_h) so every particle inherits the env's injected rand
// without threading an extra argument through reset() on every call site.
class StarParticle {
    constructor(layer) { this.layer = layer; this.reset(true); }
    reset(initial) {
        const w = this.layer._w, h = this.layer._h, rand = this.layer._rand;
        this.x = rand() * w;
        this.y = h + rand() * 100;
        this.size = this.layer.size[0] + rand() * (this.layer.size[1] - this.layer.size[0]);
        this.baseSize = this.size;
        this.color = starPickColor(rand);
        this.vx = (rand() - 0.5) * 2 * this.layer.speed;
        this.vy = -(0.5 + rand() * 0.5) * STAR_CONFIG.riseSpeed * this.layer.speed;
        this.baseVy = this.vy;
        this.oscillationOffset = rand() * Math.PI * 2;
        this.oscillationSpeed = 0.005 + rand() * 0.008;
        this.oscillationAmplitude = 5 + rand() * 10;
        this.flickerOffset = rand() * Math.PI * 2;
        this.flickerSpeed = STAR_CONFIG.flickerSpeed * (0.8 + rand() * 0.4);
        this.opacity = this.layer.opacity; this.baseOpacity = this.layer.opacity;
        this.trail = []; this.life = 1; this.dead = false;
        if (initial) this.y = rand() * h * 1.5; // stagger the first fill
    }
    // dt60 = elapsed frames at the 60 Hz reference (1.0 at 60 fps, 0.5 at 120 Hz).
    // The physics constants below are expressed as per-60Hz-tick deltas — the
    // original code integrated them once per FRAME, so the field ran ~2x fast on
    // a 120 Hz display and crawled on a throttled tab. Multiplying the
    // ACCUMULATING terms by dt60 is mathematically identical at 60 fps (dt60 = 1),
    // so the approved look is preserved exactly, and correct everywhere else.
    // `vy` is an assignment, not an accumulation, so it is deliberately unscaled.
    update(time, active, dt60 = 1) {
        const w = this.layer._w, h = this.layer._h;
        const turbX = Math.sin(time * 0.0003 + this.oscillationOffset) * STAR_CONFIG.turbulence * 0.3;
        const turbY = Math.cos(time * 0.0004 + this.oscillationOffset) * STAR_CONFIG.turbulence * 0.15;
        const oscillation = Math.sin(time * this.oscillationSpeed + this.oscillationOffset) * this.oscillationAmplitude * 0.002;
        this.vx += (turbX * 0.005 + oscillation - this.vx * 0.02) * dt60;
        this.vy = this.baseVy + turbY * 0.005;
        this.x += this.vx * dt60; this.y += this.vy * dt60;
        if (STAR_CONFIG.trailLength > 0) {
            this.trail.unshift({ x: this.x, y: this.y, size: this.size, opacity: this.opacity });
            if (this.trail.length > STAR_CONFIG.trailLength) this.trail.pop();
        }
        const f1 = Math.sin(time * this.flickerSpeed + this.flickerOffset);
        const f2 = Math.sin(time * this.flickerSpeed * 0.7 + this.flickerOffset * 1.3);
        const flicker = (f1 + f2 * 0.5) / 1.5;
        // Exponential approach — also a per-tick rate, so it scales with dt60 too
        // (clamped: a long stall must not overshoot past the target).
        const k = Math.min(1, 0.05 * dt60);
        this.opacity += (this.baseOpacity * (0.7 + flicker * 0.3) - this.opacity) * k;
        this.size += (this.baseSize * (0.9 + flicker * 0.1) - this.size) * k;
        if (this.y < h * 0.2) { this.life = this.y / (h * 0.2); this.opacity *= this.life; }
        if (this.y < -50 || this.x < -50 || this.x > w + 50) {
            if (active) this.reset(false); else this.dead = true;
        }
    }
}

class StarField {
    constructor() { this.width = 0; this.height = 0; this.particles = []; this._spawned = false; this._rand = Math.random; }
    resize(w, h) { this.width = w; this.height = h; this.particles.forEach(p => { p.layer._w = w; p.layer._h = h; }); }
    spawn(rand) {
        if (rand) this._rand = rand;
        this.particles = [];
        STAR_CONFIG.layers.forEach(base => {
            const layer = Object.assign({}, base, { _w: this.width, _h: this.height, _rand: this._rand });
            for (let i = 0; i < base.count; i++) this.particles.push(new StarParticle(layer));
        });
        this._spawned = true;
    }
    update(time, active, dt60 = 1) {
        if (!this._spawned) this.spawn();
        for (let i = 0; i < this.particles.length; i++) if (!this.particles[i].dead) this.particles[i].update(time, active, dt60);
    }
    rearm() { this.particles.forEach(p => { if (p.dead) p.reset(false); p.dead = false; }); }
}

function drawStarParticle(c, p) {
    if (STAR_CONFIG.trailLength > 0) {
        for (let i = 0; i < p.trail.length; i++) {
            const t = p.trail[i];
            const trailOpacity = t.opacity * (1 - i / STAR_CONFIG.trailLength) * 0.5;
            const trailSize = t.size * (1 - i / STAR_CONFIG.trailLength);
            c.beginPath(); c.arc(t.x, t.y, trailSize, 0, Math.PI * 2);
            c.fillStyle = `rgba(${p.color.r},${p.color.g},${p.color.b},${trailOpacity})`; c.fill();
        }
    }
    const g = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.size * STAR_CONFIG.glowIntensity);
    g.addColorStop(0, `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity * 0.8})`);
    g.addColorStop(0.1, `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity * 0.4})`);
    g.addColorStop(0.4, `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity * 0.1})`);
    g.addColorStop(1, `rgba(${p.color.r},${p.color.g},${p.color.b},0)`);
    c.beginPath(); c.arc(p.x, p.y, p.size * STAR_CONFIG.glowIntensity, 0, Math.PI * 2); c.fillStyle = g; c.fill();
    const cg = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.size);
    cg.addColorStop(0, `rgba(255,255,255,${p.opacity})`);
    cg.addColorStop(0.3, `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity})`);
    cg.addColorStop(1, `rgba(${p.color.r},${p.color.g},${p.color.b},0)`);
    c.beginPath(); c.arc(p.x, p.y, p.size, 0, Math.PI * 2); c.fillStyle = cg; c.fill();
}

function init(env) {
    const st = env.state;
    if (!st.sim) st.sim = new StarField();
    st.sim.resize(env.width, env.height);
    st.sim.spawn(env.rand);
}

export const descriptor = {
    id: 'stars',
    label: 'Rising Stars',
    blend: 'source-over',
    clearPolicy: 'clear',
    init,
    resize(env) { if (env.state.sim) env.state.sim.resize(env.width, env.height); },
    update(dt, env) {
        if (!env.state.sim) init(env);
        // Convert to 60 Hz reference ticks and clamp both ends, matching the shipped
        // Android port (ParticleField.kt: (dtSec*60).coerceIn(0.25, 4)) so the two
        // surfaces move at the same speed. Guards against a huge post-stall jump.
        const dt60 = Math.max(0.25, Math.min(4, dt * 60));
        env.state.sim.update(env.now, env.active !== false, dt60);
    },
    draw(ctx, env) {
        const sim = env.state.sim;
        if (!sim) return;
        const ps = sim.particles;
        for (let i = 0; i < ps.length; i++) if (!ps[i].dead) drawStarParticle(ctx, ps[i]);
    },
    rearm(env) {
        const st = env.state;
        if (!st.sim || !st.sim._spawned) init(env);
        else st.sim.rearm();
    },
};

registerEffect(descriptor);
export default descriptor;
export { STAR_CONFIG };
