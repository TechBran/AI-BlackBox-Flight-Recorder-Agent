/**
 * ember-fx.js
 * Generation Ember FX — a faithful port of the landing-page cinematic ember
 * system (Apps/landing-page/app.js → initCinematicParticles('embers'),
 * lines 191-518), adapted to the Portal so that WHILE the AI is generating
 * (text streaming, non-streaming polling, agent-CLI, OR image/video/music) the
 * chat backdrop fills with rising embers over the (already-black) background,
 * then gracefully drains when generation finishes.
 *
 * Design (see docs/plans/2026-06-28-generation-ember-backdrop.md):
 *  - ONE <canvas id="emberCanvas"> mounted as the first child of
 *    <section class="chat">, behind the (raised, transparent) #history so the
 *    embers show through the gaps between bubbles while the bubbles stay
 *    readable. Structural CSS lives in styles/features/_ember-fx.css.
 *  - Driven by a MutationObserver scoped to #history + #thinkingIndicator that
 *    reads the generation MARKERS the existing code already toggles — NO edits
 *    to chat-send.js / agent-handler.js / task-manager.js:
 *      .streaming-bubble                      streaming chat   (chat-send.js)
 *      .bubble.thinking                       polling          (chat-bubbles.js)
 *      .generating-image|video|music          media tasks      (task-manager.js)
 *      #thinkingIndicator:not(.hide)          agent CLI        (agent-handler.js)
 *  - window.EmberFX.markGenerating(reason, on) lets other code drive it too.
 *  - Respects prefers-reduced-motion (the effect IS motion → skip it).
 *  - The rAF loop runs ONLY while generating or draining; it stops + clears the
 *    canvas when idle (no battery-burning idle loop) and pauses when hidden.
 */

// ---- Palette / feel: identical to the website embers ------------------------
const CONFIG = {
    layers: [
        { count: 40, speed: 0.3, size: [0.5, 1], opacity: 0.25 }, // far / tiny
        { count: 50, speed: 0.5, size: [1, 2],   opacity: 0.4  }, // mid / small
        { count: 30, speed: 0.8, size: [1.5, 3], opacity: 0.7  }  // fore / medium
    ],
    colors: [
        { r: 255, g: 74,  b: 74  }, // red
        { r: 255, g: 120, b: 50  }, // orange
        { r: 255, g: 180, b: 50  }, // yellow-orange
        { r: 255, g: 220, b: 100 }, // yellow
        { r: 255, g: 250, b: 200 }  // white-hot
    ],
    colorWeights: [0.3, 0.3, 0.2, 0.15, 0.05],
    glowIntensity: 10,
    turbulence: 0.6,
    riseSpeed: 0.8,
    flickerSpeed: 0.015,
    trailLength: 2
};

// =============================================================================
// EmberSimulation — UI-free physics (no DOM/canvas), so it is unit-testable.
// =============================================================================
function pickColor() {
    const rand = Math.random();
    let cumulative = 0;
    for (let i = 0; i < CONFIG.colorWeights.length; i++) {
        cumulative += CONFIG.colorWeights[i];
        if (rand < cumulative) return CONFIG.colors[i];
    }
    return CONFIG.colors[0];
}

class Particle {
    constructor(layer, layerIndex) {
        this.layer = layer;
        this.layerIndex = layerIndex;
        this.reset(true);
    }
    reset(initial) {
        const w = this.layer._w, h = this.layer._h;
        this.x = Math.random() * w;
        this.y = h + Math.random() * 100;
        this.size = this.layer.size[0] + Math.random() * (this.layer.size[1] - this.layer.size[0]);
        this.baseSize = this.size;
        this.color = pickColor();
        this.vx = (Math.random() - 0.5) * 2 * this.layer.speed;
        this.vy = -(0.5 + Math.random() * 0.5) * CONFIG.riseSpeed * this.layer.speed;
        this.baseVy = this.vy;
        this.oscillationOffset = Math.random() * Math.PI * 2;
        this.oscillationSpeed = 0.005 + Math.random() * 0.008;
        this.oscillationAmplitude = 5 + Math.random() * 10;
        this.flickerOffset = Math.random() * Math.PI * 2;
        this.flickerSpeed = CONFIG.flickerSpeed * (0.8 + Math.random() * 0.4);
        this.opacity = this.layer.opacity;
        this.baseOpacity = this.layer.opacity;
        this.trail = [];
        this.life = 1;
        this.dead = false;
        if (initial) this.y = Math.random() * h * 1.5; // stagger the first fill
    }
    update(time, active) {
        const w = this.layer._w, h = this.layer._h;
        const turbX = Math.sin(time * 0.0003 + this.oscillationOffset) * CONFIG.turbulence * 0.3;
        const turbY = Math.cos(time * 0.0004 + this.oscillationOffset) * CONFIG.turbulence * 0.15;
        const oscillation = Math.sin(time * this.oscillationSpeed + this.oscillationOffset) * this.oscillationAmplitude * 0.002;
        this.vx += (turbX * 0.005 + oscillation - this.vx * 0.02);
        this.vy = this.baseVy + turbY * 0.005;
        this.x += this.vx;
        this.y += this.vy;
        if (CONFIG.trailLength > 0) {
            this.trail.unshift({ x: this.x, y: this.y, size: this.size, opacity: this.opacity });
            if (this.trail.length > CONFIG.trailLength) this.trail.pop();
        }
        const f1 = Math.sin(time * this.flickerSpeed + this.flickerOffset);
        const f2 = Math.sin(time * this.flickerSpeed * 0.7 + this.flickerOffset * 1.3);
        const flicker = (f1 + f2 * 0.5) / 1.5;
        const targetOpacity = this.baseOpacity * (0.7 + flicker * 0.3);
        this.opacity += (targetOpacity - this.opacity) * 0.05;
        const targetSize = this.baseSize * (0.9 + flicker * 0.1);
        this.size += (targetSize - this.size) * 0.05;
        if (this.y < h * 0.2) { this.life = this.y / (h * 0.2); this.opacity *= this.life; }
        if (this.y < -50 || this.x < -50 || this.x > w + 50) {
            if (active) this.reset(false); // generating: recycle
            else this.dead = true;         // draining: cull
        }
    }
}

class EmberSimulation {
    constructor() {
        this.width = 0;
        this.height = 0;
        this.particles = [];
        this._spawned = false;
    }
    resize(width, height) {
        this.width = width;
        this.height = height;
        // propagate dimensions to each particle's layer view
        this.particles.forEach(p => { p.layer._w = width; p.layer._h = height; });
    }
    spawn() {
        this.particles = [];
        CONFIG.layers.forEach((base, li) => {
            const layer = Object.assign({}, base, { _w: this.width, _h: this.height });
            for (let i = 0; i < base.count; i++) this.particles.push(new Particle(layer, li));
        });
        this._spawned = true;
    }
    update(time, active) {
        if (!this._spawned) this.spawn();
        for (let i = 0; i < this.particles.length; i++) {
            if (!this.particles[i].dead) this.particles[i].update(time, active);
        }
    }
    isDrained() {
        for (let i = 0; i < this.particles.length; i++) if (!this.particles[i].dead) return false;
        return true;
    }
    rearm() { this.particles.forEach(p => { if (p.dead) p.reset(false); p.dead = false; }); }
}

// =============================================================================
// Canvas renderer + DOM-driven controller (browser-only).
// =============================================================================
const REASONS = new Set();   // active generation reasons (dom, media, manual…)
let host = null;             // <section class="chat">
let canvas = null;
let ctx = null;
let width = 0, height = 0, dpr = 1;
let sim = null;
let rafId = 0;
let active = false;          // generation in progress (spawning enabled)
let running = false;         // RAF loop alive (stays true through the drain)
let resizeObs = null;
let mutationObs = null;
let recomputeQueued = false;
let inited = false;          // idempotency guard for initEmberFX()
let drainStartT = 0;         // rAF timestamp the current drain began (0 = not draining)
const DRAIN_MAX_MS = 650;    // cap the post-generation drain so the loop can't run for minutes

const prefersReduced = typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function drawParticle(c, p) {
    if (CONFIG.trailLength > 0) {
        for (let i = 0; i < p.trail.length; i++) {
            const t = p.trail[i];
            const trailOpacity = t.opacity * (1 - i / CONFIG.trailLength) * 0.5;
            const trailSize = t.size * (1 - i / CONFIG.trailLength);
            c.beginPath();
            c.arc(t.x, t.y, trailSize, 0, Math.PI * 2);
            c.fillStyle = `rgba(${p.color.r},${p.color.g},${p.color.b},${trailOpacity})`;
            c.fill();
        }
    }
    const g = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.size * CONFIG.glowIntensity);
    g.addColorStop(0,   `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity * 0.8})`);
    g.addColorStop(0.1, `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity * 0.4})`);
    g.addColorStop(0.4, `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity * 0.1})`);
    g.addColorStop(1,   `rgba(${p.color.r},${p.color.g},${p.color.b},0)`);
    c.beginPath();
    c.arc(p.x, p.y, p.size * CONFIG.glowIntensity, 0, Math.PI * 2);
    c.fillStyle = g; c.fill();
    const cg = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.size);
    cg.addColorStop(0,   `rgba(255,255,255,${p.opacity})`);
    cg.addColorStop(0.3, `rgba(${p.color.r},${p.color.g},${p.color.b},${p.opacity})`);
    cg.addColorStop(1,   `rgba(${p.color.r},${p.color.g},${p.color.b},0)`);
    c.beginPath();
    c.arc(p.x, p.y, p.size, 0, Math.PI * 2);
    c.fillStyle = cg; c.fill();
}

function ensureCanvas() {
    if (canvas) return true;
    host = document.querySelector('section.chat') || document.querySelector('.chat');
    if (!host) return false;
    canvas = document.createElement('canvas');
    canvas.id = 'emberCanvas';
    canvas.setAttribute('aria-hidden', 'true');
    host.insertBefore(canvas, host.firstChild);
    ctx = canvas.getContext('2d');
    sim = new EmberSimulation();
    resize();
    resizeObs = new ResizeObserver(resize);
    resizeObs.observe(host);
    return true;
}
function resize() {
    if (!canvas || !host) return;
    const w = host.clientWidth, h = host.clientHeight;
    if (w === 0 || h === 0) return;            // ignore transient 0-measure (e.g. display:none)
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = w; height = h;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (sim) sim.resize(w, h);
}
function loop(time) {
    if (document.hidden) { rafId = 0; return; } // browser pauses rAF; bail cleanly
    ctx.clearRect(0, 0, width, height);
    ctx.globalCompositeOperation = 'lighter'; // additive glow over the black bg
    sim.update(time, active);
    // Bound the drain: once generation ends let particles rise/fade briefly, then
    // force-cull so the loop stops within ~DRAIN_MAX_MS instead of the ~1-2 min a
    // slow bottom particle would otherwise take to clear the top on its own.
    if (!active) {
        if (drainStartT === 0) drainStartT = time;
        else if (time - drainStartT > DRAIN_MAX_MS) {
            for (let i = 0; i < sim.particles.length; i++) sim.particles[i].dead = true;
        }
    }
    // particles are spawned in layer order and never reorder → no per-frame sort needed
    for (let i = 0; i < sim.particles.length; i++) {
        const p = sim.particles[i];
        if (!p.dead) drawParticle(ctx, p);
    }
    ctx.globalCompositeOperation = 'source-over';
    if (active || !sim.isDrained()) {
        rafId = requestAnimationFrame(loop);
    } else {
        running = false; rafId = 0;
        ctx.clearRect(0, 0, width, height);
        if (canvas) canvas.classList.remove('on');
    }
}
function startLoop() {
    if (!running) { running = true; rafId = requestAnimationFrame(loop); }
    else if (!rafId) { rafId = requestAnimationFrame(loop); }
}

// ---- Start / stop driven by REASONS -----------------------------------------
function applyState() {
    const shouldBeActive = REASONS.size > 0;
    if (shouldBeActive === active) return;
    active = shouldBeActive;
    if (active) {
        if (prefersReduced) return;            // honor reduced-motion: no-op
        if (!ensureCanvas()) { active = false; return; }
        resize();
        sim.rearm();                           // revive any culled particles
        drainStartT = 0;
        canvas.classList.add('on');            // fade in (CSS opacity)
        startLoop();
    } else if (canvas) {
        canvas.classList.remove('on');         // begin the opacity fade-out immediately
        drainStartT = 0;                       // loop stamps this + culls at the deadline
        startLoop();                           // ensure the loop runs to drain + clear
    }
}
function markGenerating(reason, on) {
    if (on) REASONS.add(reason); else REASONS.delete(reason);
    applyState();
}

// ---- Generation detection from the DOM --------------------------------------
const GEN_SELECTOR = '.streaming-bubble, .bubble.thinking, .generating-image, .generating-video, .generating-music';
function isGeneratingNow() {
    if (document.querySelector(GEN_SELECTOR)) return true;
    const ti = document.getElementById('thinkingIndicator');
    if (ti && !ti.classList.contains('hide')) return true;
    return false;
}
function recompute() {
    recomputeQueued = false;
    markGenerating('dom', isGeneratingNow());
}
function queueRecompute() {
    if (recomputeQueued) return;
    recomputeQueued = true;
    // setTimeout (not rAF) so generation DETECTION keeps working while the tab is
    // backgrounded — rAF is paused when hidden; only the RENDER loop should pause.
    setTimeout(recompute, 0);
}
function onVisibility() {
    if (document.hidden) return;
    queueRecompute();                         // re-sync detection after returning
    if (running && !rafId) rafId = requestAnimationFrame(loop); // resume render
}

// ---- Public init ------------------------------------------------------------
export function initEmberFX() {
    if (inited) return;                        // idempotent: never double-attach observers
    inited = true;
    if (prefersReduced) {
        window.EmberFX = { markGenerating: () => {}, isActive: () => false, _reduced: true };
        console.log('[EmberFX] prefers-reduced-motion — ember effect disabled');
        return;
    }
    const history = document.getElementById('history');
    const thinking = document.getElementById('thinkingIndicator');
    mutationObs = new MutationObserver(queueRecompute);
    if (history) {
        mutationObs.observe(history, {
            childList: true, subtree: true,
            attributes: true, attributeFilter: ['class']
        });
    }
    if (thinking) {
        mutationObs.observe(thinking, { attributes: true, attributeFilter: ['class'] });
    }
    document.addEventListener('visibilitychange', onVisibility);
    window.EmberFX = {
        markGenerating,
        isActive: () => active,
        _config: CONFIG,
        _sim: () => sim
    };
    recompute(); // catch a generation already in flight (e.g. restored pending)
    console.log('[EmberFX] Generation ember effect initialized (website-matched palette)');
}

// Exposed for unit tests / external drivers.
export { EmberSimulation, CONFIG };
export default { initEmberFX, EmberSimulation };
