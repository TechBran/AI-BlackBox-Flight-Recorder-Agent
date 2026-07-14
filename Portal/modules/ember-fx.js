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
        const spark = Math.random() < 0.12, ml = 0.8 + Math.random() * 0.7;
        embers.push({
            x: width * (0.08 + Math.random() * 0.84), y: height + 10 + Math.random() * 20,
            vx: (Math.random() - 0.5) * 60, vy: -(60 + Math.random() * 130),
            life: 1, decay: 1 / ml, r: spark ? (1.6 + Math.random() * 1.4) : (6 + Math.random() * 11), spark
        });
    }
}
function drawEmbers(now, dt, isActive) {
    const ts = now * 0.001;
    // heat-persistence smear: translucent dark fill instead of a hard clear
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = 'rgba(8,6,10,0.15)';
    ctx.fillRect(0, 0, width, height);
    // ground heat-glow (subtle; grows a touch with surge)
    const gh = height * (0.32 + 0.1 * Math.min(surge, 1)), a = (0.13 + 0.14 * Math.min(surge, 1.2)) * FIELD.intensity;
    const g = ctx.createLinearGradient(0, height, 0, height - gh);
    g.addColorStop(0, `rgba(226,60,10,${a})`);
    g.addColorStop(0.5, `rgba(170,28,6,${a * 0.5})`);
    g.addColorStop(1, 'rgba(120,20,5,0)');
    ctx.globalCompositeOperation = 'lighter';
    ctx.fillStyle = g;
    ctx.fillRect(0, height - gh, width, gh);
    // spawn (only while generating; during drain we stop feeding so it thins out)
    if (isActive) {
        const perSec = (240 + surge * 840) * emberScale();
        let acc = (drawEmbers._a || 0) + perSec * dt;
        while (acc >= 1) { spawnEmber(1); acc--; }
        drawEmbers._a = acc;
    }
    const eps = 3;
    for (let i = embers.length - 1; i >= 0; i--) {
        const p = embers[i];
        p.life -= p.decay * dt;
        if (p.life <= 0) { embers[i] = embers[embers.length - 1]; embers.pop(); continue; }
        // curl-noise swirl: v = curl(ψ) via central differences
        const cvx = (pot(p.x, p.y + eps, ts) - pot(p.x, p.y - eps, ts));
        const cvy = -(pot(p.x + eps, p.y, ts) - pot(p.x - eps, p.y, ts));
        p.vx += cvx * 7200 * dt; p.vy += cvy * 7200 * dt;
        p.vy -= 52 * p.life * dt;                 // buoyancy ∝ heat
        if (p.spark) p.vy += 120 * dt;            // sparks arc under gravity
        p.vx *= (1 - 1.3 * dt); p.vy *= (1 - 0.9 * dt); // drag
        p.x += p.vx * dt; p.y += p.vy * dt;
        const idx = Math.max(0, Math.min(RAMP.length - 1, Math.round((1 - p.life) * (RAMP.length - 1))));
        const spr = p.spark ? EMBER_SPR[0] : EMBER_SPR[idx];
        const al = (p.spark ? 0.75 : 0.4) * p.life * FIELD.intensity;
        // faint big glow + bright core (cheap bloom)
        const grr = p.r * (p.spark ? 4 : 4.2); ctx.globalAlpha = al * 0.22; ctx.drawImage(spr, p.x - grr, p.y - grr, grr * 2, grr * 2);
        const cr = p.r * (p.spark ? 1.6 : 1.9); ctx.globalAlpha = al; ctx.drawImage(spr, p.x - cr, p.y - cr, cr * 2, cr * 2);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    if (embers.length > 2600) embers.splice(0, embers.length - 2600);
}

// =============================================================================
// Field: RISING STARS (parallax + de-synced twinkle) — DEFAULT
// =============================================================================
let stars = [];
const LAYERS = [
    { p: 0.6, s: [0.5, 1.0], vy: 0.4, a: [0.4, 0.6], glow: false }, // far / tiny
    { p: 0.3, s: [1.0, 1.8], vy: 0.7, a: [0.6, 0.8], glow: false }, // mid / small
    { p: 0.1, s: [1.8, 2.6], vy: 1.0, a: [0.8, 1.0], glow: true }   // fore / hero (glow)
];
function makeStar() {
    const roll = Math.random(); let L = LAYERS[0];
    if (roll > 0.9) L = LAYERS[2]; else if (roll > 0.6) L = LAYERS[1];
    const sk = Math.pow(Math.random(), 2.2); // power-skewed: mostly tiny, few bright
    return {
        x: Math.random() * width, y: Math.random() * height,
        size: L.s[0] + (L.s[1] - L.s[0]) * sk, vy: -(8 + Math.random() * 17) * L.vy, vx: (Math.random() - 0.5) * 4,
        base: L.a[0] + (L.a[1] - L.a[0]) * Math.random(), amp: 0.25 + Math.random() * 0.2,
        tw: 0.8 + Math.random() * 1.7, seed: Math.random() * 6.28,
        hue: Math.random() < 0.7 ? 255 : (Math.random() < 0.5 ? 250 : 34), glow: L.glow
    };
}
function initStars() {
    stars = [];
    const N = Math.round(baseCount() * FIELD.density);
    for (let i = 0; i < N; i++) stars.push(makeStar());
}
// Grow/trim the star array IN PLACE toward `want` so existing stars keep their
// positions — a full initStars() re-scatter on a resize count-drift causes a
// visible "pop" as every star jumps.
function resizeStars(want) {
    while (stars.length < want) stars.push(makeStar());
    if (stars.length > want) stars.length = want;
}
function drawStars(now, dt) {
    // Crisp points: CLEAR to transparent each frame (no smear) and draw additively
    // over the black .chat — the exact compositing model of the field this replaces.
    ctx.globalCompositeOperation = 'source-over';
    ctx.clearRect(0, 0, width, height);
    const want = Math.round(baseCount() * FIELD.density);
    if (Math.abs(want - stars.length) > 40) resizeStars(want);
    ctx.globalCompositeOperation = 'lighter';
    const ts = now * 0.001;
    for (const s of stars) {
        s.y += s.vy * dt; s.x += s.vx * dt;
        if (s.y < -6) { s.y = height + 6; s.x = Math.random() * width; }
        const al = Math.max(0, Math.min(1, (s.base + Math.sin(ts * s.tw + s.seed) * s.amp))) * FIELD.intensity;
        const col = s.hue === 34 ? '255,205,110' : (s.hue === 250 ? '200,215,255' : '248,247,255');
        if (s.glow) { const gr = s.size * 7; ctx.globalAlpha = al * 0.5; ctx.drawImage(STAR_SPR, s.x - gr, s.y - gr, gr * 2, gr * 2); }
        ctx.globalAlpha = al; ctx.fillStyle = `rgb(${col})`;
        ctx.beginPath(); ctx.arc(s.x, s.y, s.size, 0, 6.2832); ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
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
    else if (m === 'embers') { embers = []; drawEmbers._a = 0; }
}
// ensure the active field has state to draw (lazy — never resets a live field)
function ensureFieldReady(m) {
    if (m === 'stars' && stars.length === 0) initStars();
    else if (m === 'matrix' && mCols.length === 0) initMatrix();
    // embers self-fills from an empty array
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

// Exposed for a later settings UI / external drivers.
export { setParticleMode, getParticleMode };
export default { initEmberFX, initEmberModeControl, setParticleMode, getParticleMode };
