/**
 * ember-fx.js
 * Background particle FIELD for the chat backdrop — a three-mode field that
 * fades in WHILE the AI is generating (text streaming, non-streaming polling,
 * agent-CLI, OR image/video/music) and gracefully drains when generation ends.
 * The single red "Signal" telemetry line renders in its OWN layer in FRONT of
 * this field (see docs/plans/2026-07-13-system-telemetry-stream-design.md).
 *
 * Three selectable fields (ported from the approved prototype, Appendix A of the
 * design doc — the-signal-prototype.html):
 *   - 'stars'   Rising Stars  — DEFAULT. Parallax depth layers, power-skewed
 *                 sizes, de-synced twinkle (per-star phase+speed), glow sprite
 *                 only on the brightest ~10%, crisp cores (clears each frame,
 *                 additive over the black .chat — identical compositing to the
 *                 field this replaces, so the default look does not regress).
 *   - 'embers'  Embers        — curl-noise divergence-free swirl, blackbody
 *                 white-hot→deep-red ramp by particle life, pre-rendered sprite
 *                 atlas drawn twice (faint glow + bright core) under additive
 *                 blend, heat-persistence smear, buoyancy + drag + sparks.
 *   - 'matrix'  Matrix        — column rain, translucent trail fade, bright
 *                 leading glyph + dim green trail, per-column speed variance,
 *                 katakana + digits.
 *
 * The particle mode is ORTHOGONAL to the Off/While-generating/Always VISIBILITY
 * setting (that setting is unchanged; see `mode` / setMode / bb_ember_mode).
 * Particle mode persists in localStorage (bb_particle_mode) and is switched at
 * runtime via the exported setParticleMode(mode) (for a later settings UI).
 *
 * Design invariants preserved verbatim from the shipped generation-ember effect:
 *  - ONE <canvas id="emberCanvas"> mounted as the first child of
 *    <section class="chat">, behind the (raised, transparent) #history so the
 *    field shows through the gaps between bubbles while the bubbles stay
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

// ---- Field tuning -----------------------------------------------------------
// density/intensity are the two prototype tuning knobs; fixed to 1.0 here (no
// tuning panel in the Portal). DRAIN_MAX_MS bounds the post-generation drain so
// the rAF loop can never idle-spin (it must outlast the CSS opacity fade).
const FIELD = { density: 1.0, intensity: 1.0 };
const DRAIN_MAX_MS = 650;

// =============================================================================
// Module state
// =============================================================================
const REASONS = new Set();   // active generation reasons (dom, media, manual…)
let mode = 'always';         // VISIBILITY: 'off'|'generating'|'always' (bb_ember_mode)
let particleMode = 'stars';  // FIELD: 'stars'|'embers'|'matrix' (bb_particle_mode)
let host = null;             // <section class="chat">
let canvas = null;
let ctx = null;
let width = 0, height = 0, dpr = 1;
let rafId = 0;
let active = false;          // generation in progress (spawning enabled)
let running = false;         // RAF loop alive (stays true through the drain)
let resizeObs = null;
let mutationObs = null;
let recomputeQueued = false;
let inited = false;          // idempotency guard for initEmberFX()
let drainStartT = 0;         // rAF timestamp the current drain began (0 = not draining)
let lastT = 0;               // rAF timestamp of the previous frame (delta timing)
let surge = 0;               // load-reactivity (0 = calm baseline; no driver in this task)

const prefersReduced = typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// Coarse-pointer (touch) halves particle counts. Evaluated ONCE at module load —
// baseCount() runs every frame, so never create an MQL ~60×/s in the hot path.
const coarsePointer = typeof window.matchMedia === 'function' &&
    window.matchMedia('(pointer:coarse)').matches;

// =============================================================================
// Pre-rendered sprite atlas — drawImage() instead of per-particle gradients
// (the #1 perf win). Built lazily on first canvas init so a non-DOM import
// (unit test / SSR) never touches document.
// =============================================================================
let EMBER_SPR = null;   // blackbody ramp: white-hot core → deep ember red
let STAR_SPR = null;    // soft warm-white blob for the hero-star glow
// blackbody-ish ember ramp: white-hot core -> deep ember red
const RAMP = [[255, 255, 240], [255, 238, 150], [255, 182, 64], [255, 110, 22], [201, 44, 6], [92, 16, 5]];

function makeSprite(size, r, g, b) {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const x = c.getContext('2d');
    const gr = x.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gr.addColorStop(0, `rgba(${r},${g},${b},1)`);
    gr.addColorStop(0.35, `rgba(${r},${g},${b},.5)`);
    gr.addColorStop(1, `rgba(${r},${g},${b},0)`);
    x.fillStyle = gr;
    x.fillRect(0, 0, size, size);
    return c;
}
function ensureSprites() {
    if (EMBER_SPR) return;
    EMBER_SPR = RAMP.map(c => makeSprite(64, c[0], c[1], c[2]));
    STAR_SPR = makeSprite(48, 255, 252, 246);
}

// =============================================================================
// Shared field helpers
// =============================================================================
// responsive count by CSS AREA (logical px, not device px), clamped; halved on
// coarse pointers (Appendix A: ~1 particle / 8,000 px², floor 60, ceiling 1200)
function baseCount() {
    let n = (width * height) / 8000;
    if (coarsePointer) n *= 0.5;
    return Math.max(60, Math.min(n, 1200));
}
const emberScale = () => Math.max(0.4, Math.min(2.4, (width * height) / (1440 * 900))) * FIELD.density;

// cheap 2-octave sine curl-noise potential ψ → divergence-free swirl
function pot(x, y, t) {
    return Math.sin(x * 0.0065 + t * 0.22) * Math.cos(y * 0.0065 - t * 0.16)
        + 0.5 * Math.sin(x * 0.013 - t * 0.31) * Math.cos(y * 0.013 + t * 0.26);
}

// =============================================================================
// Field: EMBERS (curl-noise + blackbody ramp + additive sprites + smear)
// =============================================================================
let embers = [];
function spawnEmber(n) {
    for (let i = 0; i < n; i++) {
        const spark = Math.random() < 0.08, ml = 2.6 + Math.random() * 3.6; // long life → floats across
        embers.push({
            x: Math.random() * width, y: Math.random() * height,            // ANYWHERE on screen (not just the bottom)
            vx: (Math.random() - 0.5) * 16, vy: -(4 + Math.random() * 14),   // gentle float, not a bottom jet
            life: 1, decay: 1 / ml, r: spark ? (1.2 + Math.random() * 1.3) : (2.5 + Math.random() * 6),
            spark, fade: Math.random() * 6.28, hue0: Math.floor(Math.random() * 3) // 0..2 varied warm heat
        });
    }
}
function drawEmbers(now, dt, isActive) {
    const ts = now * 0.001;
    // heat-persistence smear (soft glowing trails). NO bottom ground-glow — real
    // embers float across the WHOLE screen, not a fireball at the base.
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = 'rgba(8,6,10,0.20)';
    ctx.fillRect(0, 0, width, height);
    // keep a full-screen population while generating (seeded full via initField)
    if (isActive) {
        const target = Math.round((150 + surge * 160) * emberScale());
        let acc = (drawEmbers._a || 0) + (30 + surge * 90) * emberScale() * dt;
        while (acc >= 1 && embers.length < target * 1.3) { spawnEmber(1); acc--; }
        drawEmbers._a = acc;
    }
    ctx.globalCompositeOperation = 'lighter';
    const eps = 3;
    for (let i = embers.length - 1; i >= 0; i--) {
        const p = embers[i];
        p.life -= p.decay * dt;
        if (p.life <= 0) { embers[i] = embers[embers.length - 1]; embers.pop(); continue; }
        // curl-noise swirl (gentle) + slow float — drifts everywhere, doesn't jet up
        const cvx = (pot(p.x, p.y + eps, ts) - pot(p.x, p.y - eps, ts));
        const cvy = -(pot(p.x + eps, p.y, ts) - pot(p.x - eps, p.y, ts));
        p.vx += cvx * 5200 * dt; p.vy += cvy * 5200 * dt;
        p.vy -= 9 * p.life * dt;                   // gentle buoyancy (slow float up)
        if (p.spark) p.vy += 60 * dt;              // sparks drift down a touch
        p.vx *= (1 - 0.9 * dt); p.vy *= (1 - 0.9 * dt); // drag
        p.x += p.vx * dt; p.y += p.vy * dt;
        // wrap horizontally so the field stays full across the whole width
        if (p.x < -20) p.x = width + 20; else if (p.x > width + 20) p.x = -20;
        const breathe = 0.6 + 0.4 * Math.sin(ts * 1.3 + p.fade);          // soft per-ember flicker
        const idx = p.spark ? 0 : Math.max(0, Math.min(RAMP.length - 1, p.hue0 + Math.round((1 - p.life) * 3)));
        const spr = EMBER_SPR[idx];                                       // varied warm heat, cooling as it ages
        const al = (p.spark ? 0.85 : 0.55) * Math.min(1, p.life * 1.4) * breathe * FIELD.intensity;
        // faint big glow + bright core (cheap bloom)
        const grr = p.r * (p.spark ? 3.5 : 4.2); ctx.globalAlpha = al * 0.22; ctx.drawImage(spr, p.x - grr, p.y - grr, grr * 2, grr * 2);
        const cr = p.r * (p.spark ? 1.5 : 1.9); ctx.globalAlpha = al; ctx.drawImage(spr, p.x - cr, p.y - cr, cr * 2, cr * 2);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    if (embers.length > 3000) embers.splice(0, embers.length - 3000);
}

// =============================================================================
// Field: RISING STARS — the ORIGINAL warm ember-rising field, restored verbatim
// from the pre-3-mode engine (d125f05^). This is the look Brandon confirmed he
// wants for "Rising Stars" ("I liked the way it looked before, it was perfect").
// UI-free StarField of StarParticles, rendered via soft radial-gradient glow.
// =============================================================================
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
function starPickColor() {
    const rand = Math.random(); let cumulative = 0;
    for (let i = 0; i < STAR_CONFIG.colorWeights.length; i++) {
        cumulative += STAR_CONFIG.colorWeights[i];
        if (rand < cumulative) return STAR_CONFIG.colors[i];
    }
    return STAR_CONFIG.colors[0];
}
class StarParticle {
    constructor(layer) { this.layer = layer; this.reset(true); }
    reset(initial) {
        const w = this.layer._w, h = this.layer._h;
        this.x = Math.random() * w;
        this.y = h + Math.random() * 100;
        this.size = this.layer.size[0] + Math.random() * (this.layer.size[1] - this.layer.size[0]);
        this.baseSize = this.size;
        this.color = starPickColor();
        this.vx = (Math.random() - 0.5) * 2 * this.layer.speed;
        this.vy = -(0.5 + Math.random() * 0.5) * STAR_CONFIG.riseSpeed * this.layer.speed;
        this.baseVy = this.vy;
        this.oscillationOffset = Math.random() * Math.PI * 2;
        this.oscillationSpeed = 0.005 + Math.random() * 0.008;
        this.oscillationAmplitude = 5 + Math.random() * 10;
        this.flickerOffset = Math.random() * Math.PI * 2;
        this.flickerSpeed = STAR_CONFIG.flickerSpeed * (0.8 + Math.random() * 0.4);
        this.opacity = this.layer.opacity; this.baseOpacity = this.layer.opacity;
        this.trail = []; this.life = 1; this.dead = false;
        if (initial) this.y = Math.random() * h * 1.5; // stagger the first fill
    }
    update(time, active) {
        const w = this.layer._w, h = this.layer._h;
        const turbX = Math.sin(time * 0.0003 + this.oscillationOffset) * STAR_CONFIG.turbulence * 0.3;
        const turbY = Math.cos(time * 0.0004 + this.oscillationOffset) * STAR_CONFIG.turbulence * 0.15;
        const oscillation = Math.sin(time * this.oscillationSpeed + this.oscillationOffset) * this.oscillationAmplitude * 0.002;
        this.vx += (turbX * 0.005 + oscillation - this.vx * 0.02);
        this.vy = this.baseVy + turbY * 0.005;
        this.x += this.vx; this.y += this.vy;
        if (STAR_CONFIG.trailLength > 0) {
            this.trail.unshift({ x: this.x, y: this.y, size: this.size, opacity: this.opacity });
            if (this.trail.length > STAR_CONFIG.trailLength) this.trail.pop();
        }
        const f1 = Math.sin(time * this.flickerSpeed + this.flickerOffset);
        const f2 = Math.sin(time * this.flickerSpeed * 0.7 + this.flickerOffset * 1.3);
        const flicker = (f1 + f2 * 0.5) / 1.5;
        this.opacity += (this.baseOpacity * (0.7 + flicker * 0.3) - this.opacity) * 0.05;
        this.size += (this.baseSize * (0.9 + flicker * 0.1) - this.size) * 0.05;
        if (this.y < h * 0.2) { this.life = this.y / (h * 0.2); this.opacity *= this.life; }
        if (this.y < -50 || this.x < -50 || this.x > w + 50) {
            if (active) this.reset(false); else this.dead = true;
        }
    }
}
class StarField {
    constructor() { this.width = 0; this.height = 0; this.particles = []; this._spawned = false; }
    resize(w, h) { this.width = w; this.height = h; this.particles.forEach(p => { p.layer._w = w; p.layer._h = h; }); }
    spawn() {
        this.particles = [];
        STAR_CONFIG.layers.forEach(base => {
            const layer = Object.assign({}, base, { _w: this.width, _h: this.height });
            for (let i = 0; i < base.count; i++) this.particles.push(new StarParticle(layer));
        });
        this._spawned = true;
    }
    update(time, active) {
        if (!this._spawned) this.spawn();
        for (let i = 0; i < this.particles.length; i++) if (!this.particles[i].dead) this.particles[i].update(time, active);
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
let starSim = null;
function initStars() {
    if (!starSim) starSim = new StarField();
    starSim.resize(width, height);
    starSim.spawn();
}
function drawStars(now, dt, isActive) {
    // Original compositing: full clear each frame + soft radial-gradient particles
    // (source-over); trails are drawn explicitly by drawStarParticle. Motion is the
    // original per-frame integration (not dt-scaled) to match the exact prior feel.
    ctx.globalCompositeOperation = 'source-over';
    ctx.clearRect(0, 0, width, height);
    if (!starSim) initStars();
    starSim.update(now, isActive !== false);
    const ps = starSim.particles;
    for (let i = 0; i < ps.length; i++) if (!ps[i].dead) drawStarParticle(ctx, ps[i]);
}

// =============================================================================
// Field: MATRIX (column rain)
// =============================================================================
let mCols = [], mSize = 16, mChars = [];
function initMatrix() {
    mSize = Math.max(13, Math.round(width / 78));
    const cols = Math.ceil(width / mSize);
    mCols = [];
    for (let i = 0; i < cols; i++) mCols.push({ y: Math.random() * -height, sp: mSize * (3 + Math.random() * 6), last: 0, glyph: null });
    if (!mChars.length) {
        for (let c = 0x30A0; c < 0x30FF; c++) mChars.push(String.fromCharCode(c)); // katakana
        '0123456789ABCDEF<>*+'.split('').forEach(c => mChars.push(c));              // digits/symbols
    }
}
function drawMatrix(now, dt) {
    // translucent trail fade (green-black smear) → glowing falling trails
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = 'rgba(4,7,5,0.085)';
    ctx.fillRect(0, 0, width, height);
    ctx.font = `${mSize}px ui-monospace,monospace`;
    ctx.textBaseline = 'top';
    const k = (1 + surge * 0.8) * (0.6 + 0.6 * Math.min(emberScale(), 2));
    // intensity is constant per frame → build both fillStyles ONCE, not per column.
    const leadStyle = `rgba(200,255,210,${0.9 * FIELD.intensity})`;   // bright leading glyph
    const trailStyle = `hsla(135,90%,55%,${0.5 * FIELD.intensity})`;  // dim green trail glyph
    for (let i = 0; i < mCols.length; i++) {
        const c = mCols[i], x = i * mSize;
        if (now - c.last > 36) { c.glyph = mChars[(Math.random() * mChars.length) | 0]; c.last = now; }
        ctx.fillStyle = leadStyle;
        ctx.fillText(c.glyph || mChars[0], x, c.y);
        ctx.fillStyle = trailStyle;
        ctx.fillText(mChars[(i * 7 + ((now / 90) | 0)) % mChars.length], x, c.y - mSize);
        c.y += c.sp * k * dt;
        if (c.y > height + Math.random() * height * 0.3) { c.y = Math.random() * -40; c.sp = mSize * (3 + Math.random() * 6); }
    }
    ctx.globalCompositeOperation = 'source-over';
}

// =============================================================================
// Field lifecycle
// =============================================================================
const DRAW = { stars: drawStars, embers: drawEmbers, matrix: drawMatrix };

// (re)prepare the arrays for a given field. Called on init, on activation and
// whenever the particle mode changes so switching re-inits the field cleanly.
function initField(m) {
    if (m === 'stars') initStars();
    else if (m === 'matrix') initMatrix();
    else if (m === 'embers') { embers = []; drawEmbers._a = 0; spawnEmber(Math.round(140 * emberScale())); }
}
// ensure the active field has state to draw (lazy — never resets a live field)
function ensureFieldReady(m) {
    if (m === 'stars') { if (!starSim || !starSim._spawned) initStars(); else starSim.rearm(); }
    else if (m === 'matrix' && mCols.length === 0) initMatrix();
    else if (m === 'embers' && embers.length === 0) spawnEmber(Math.round(140 * emberScale())); // seed full so it doesn't drip in
}

// =============================================================================
// Canvas plumbing
// =============================================================================
function ensureCanvas() {
    if (canvas) return true;
    host = document.querySelector('section.chat') || document.querySelector('.chat');
    if (!host) return false;
    ensureSprites();
    canvas = document.createElement('canvas');
    canvas.id = 'emberCanvas';
    canvas.setAttribute('aria-hidden', 'true');
    host.insertBefore(canvas, host.firstChild);
    ctx = canvas.getContext('2d');
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
    // Matrix column count is width-derived, so re-init it on resize — but ONLY when
    // matrix is the live field. Stars self-heal in drawStars via the count check;
    // embers self-fill from an empty array. Switching TO matrix re-inits via
    // setParticleMode → initField (and ensureFieldReady on activation).
    if (particleMode === 'matrix') initMatrix();
    else if (particleMode === 'stars' && starSim) starSim.resize(width, height);
}
function clearCanvas() {
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
function loop(now) {
    if (document.hidden) { rafId = 0; return; }  // browser pauses rAF; bail cleanly
    let dt = (now - lastT) / 1000; lastT = now;
    dt = Math.min(dt, 0.05);                      // clamp so motion is frame-rate independent
    (DRAW[particleMode] || drawStars)(now, dt, active);
    surge *= Math.exp(-2.8 * dt);                 // decay load-reactivity toward calm
    // Bound the drain: once generation ends, keep animating through the CSS opacity
    // fade, then force-stop so the loop can't idle-spin. 'always' mode never enters
    // this branch (active stays true), so it runs continuously by design.
    if (!active) {
        if (drainStartT === 0) drainStartT = now;
        else if (now - drainStartT > DRAIN_MAX_MS) {
            running = false; rafId = 0;
            clearCanvas();
            if (canvas) canvas.classList.remove('on');
            return;
        }
    }
    rafId = requestAnimationFrame(loop);
}
function startLoop() {
    lastT = (typeof performance !== 'undefined' ? performance.now() : Date.now());
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
        ensureFieldReady(particleMode);        // make sure the field has state
        if (particleMode === 'embers') { embers = []; drawEmbers._a = 0; } // fresh rise each turn
        drainStartT = 0;
        canvas.classList.add('on');            // fade in (CSS opacity)
        startLoop();
    } else if (canvas) {
        canvas.classList.remove('on');         // begin the opacity fade-out immediately
        drainStartT = 0;                       // loop stamps this + stops at the deadline
        startLoop();                           // ensure the loop runs to drain + clear
    }
}
function markGenerating(reason, on) {
    if (on) REASONS.add(reason); else REASONS.delete(reason);
    applyState();
}

// ---- VISIBILITY mode: off | generating | always (persisted, bb_ember_mode) --
function loadMode() {
    try {
        const m = localStorage.getItem('bb_ember_mode');
        if (m === 'off' || m === 'generating' || m === 'always') mode = m;
    } catch (e) { /* private mode / disabled storage */ }
}
function setMode(m) {
    if (m !== 'off' && m !== 'generating' && m !== 'always') return;
    mode = m;
    try { localStorage.setItem('bb_ember_mode', m); } catch (e) {}
    if (m === 'always') {
        markGenerating('always', true);          // permanent reason → always drifting
    } else {
        markGenerating('always', false);         // drop the permanent reason…
        recompute();                             // …and re-sync from the DOM (off → never)
    }
}

// ---- PARTICLE mode: stars | embers | matrix (persisted, bb_particle_mode) ----
function loadParticleMode() {
    try {
        const m = localStorage.getItem('bb_particle_mode');
        if (m === 'stars' || m === 'embers' || m === 'matrix') particleMode = m;
    } catch (e) { /* private mode / disabled storage */ }
}
// EXPORTED setter so a settings UI (a later task) can switch fields live.
function setParticleMode(m) {
    if (m !== 'stars' && m !== 'embers' && m !== 'matrix') return;
    if (m === particleMode) return;
    particleMode = m;
    try { localStorage.setItem('bb_particle_mode', m); } catch (e) {}
    // Re-init the newly selected field and clear any residue (smear/trail) from
    // the previous one so the switch is clean. If the loop is live it picks the
    // new DRAW routine on the next frame; if idle, the next activation re-inits.
    if (canvas) {
        initField(m);
        clearCanvas();
    }
}
function getParticleMode() { return particleMode; }

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
    // 'off' suppresses the generation-driven reason entirely; 'always' keeps its
    // own permanent reason so this only governs the 'generating' mode.
    markGenerating('dom', mode !== 'off' && isGeneratingNow());
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
    if (running && !rafId) { lastT = performance.now(); rafId = requestAnimationFrame(loop); } // resume render
}

// ---- Public init ------------------------------------------------------------
export function initEmberFX() {
    if (inited) return;                        // idempotent: never double-attach observers
    inited = true;
    loadMode();
    loadParticleMode();
    if (prefersReduced) {
        // Still expose the setters so the settings controls persist the choices.
        window.EmberFX = {
            markGenerating: () => {},
            setMode: (m) => { if (m === 'off' || m === 'generating' || m === 'always') { mode = m; try { localStorage.setItem('bb_ember_mode', m); } catch (e) {} } },
            getMode: () => mode,
            setParticleMode: (m) => { if (m === 'stars' || m === 'embers' || m === 'matrix') { particleMode = m; try { localStorage.setItem('bb_particle_mode', m); } catch (e) {} } },
            getParticleMode: () => particleMode,
            isActive: () => false,
            _reduced: true
        };
        console.log('[EmberFX] prefers-reduced-motion — particle field disabled (mode=' + particleMode + ')');
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
        setMode,
        getMode: () => mode,
        setParticleMode,
        getParticleMode,
        isActive: () => active
    };
    // Apply the persisted VISIBILITY mode: 'always' forces-on; 'off'/'generating' sync from the DOM.
    if (mode === 'always') markGenerating('always', true);
    else recompute();
    console.log('[EmberFX] Particle field initialized (visibility=' + mode + ', field=' + particleMode + ')');
}

// Reflect the persisted VISIBILITY mode into the settings radio group + drive setMode on change.
export function initEmberModeControl() {
    const radios = document.querySelectorAll('input[name="emberMode"]');
    if (!radios.length) return;
    const cur = (window.EmberFX && window.EmberFX.getMode) ? window.EmberFX.getMode() : mode;
    const sync = () => radios.forEach((r) => {
        const label = r.closest('.ember-mode-opt');
        if (label) label.classList.toggle('selected', r.checked);
    });
    radios.forEach((r) => {
        r.checked = (r.value === cur);
        r.addEventListener('change', () => {
            sync();
            if (r.checked && window.EmberFX && window.EmberFX.setMode) window.EmberFX.setMode(r.value);
        });
    });
    sync(); // reflect the initial selection (works even without :has() support)
}

// Reflect the persisted PARTICLE style into the settings radio group + drive
// setParticleMode on change. Sibling of initEmberModeControl (same markup/idiom):
// the ember-mode control governs VISIBILITY, this one governs the FIELD look.
export function initParticleModeControl() {
    const radios = document.querySelectorAll('input[name="particleMode"]');
    if (!radios.length) return;
    const cur = (window.EmberFX && window.EmberFX.getParticleMode) ? window.EmberFX.getParticleMode() : particleMode;
    const sync = () => radios.forEach((r) => {
        const label = r.closest('.ember-mode-opt');
        if (label) label.classList.toggle('selected', r.checked);
    });
    radios.forEach((r) => {
        r.checked = (r.value === cur);
        r.addEventListener('change', () => {
            sync();
            if (r.checked && window.EmberFX && window.EmberFX.setParticleMode) window.EmberFX.setParticleMode(r.value);
        });
    });
    sync(); // reflect the initial selection (works even without :has() support)
}

// Exposed for a later settings UI / external drivers.
export { setParticleMode, getParticleMode };
export default { initEmberFX, initEmberModeControl, initParticleModeControl, setParticleMode, getParticleMode };
