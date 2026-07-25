/**
 * ember-fx.js
 * Background particle FIELD for the chat backdrop — a selectable field that
 * fades in WHILE the AI is generating (text streaming, non-streaming polling,
 * agent-CLI, OR image/video/music) and gracefully drains when generation ends.
 * The single red "Signal" telemetry line renders in its OWN layer in FRONT of
 * this field (see docs/plans/2026-07-13-system-telemetry-stream-design.md).
 *
 * THIS FILE IS THE ENGINE, NOT THE EFFECTS. The fields themselves live in
 * Portal/modules/fx/effects/ and are registered in fx/effects/index.js; the
 * engine only ever talks to the registry (fx/registry.js). Adding a field is
 * one module + one line in that index — never an edit here.
 *
 * What the engine owns, and no effect may touch:
 *   - the canvas, its backing store and the DPR transform (fx/sizing.js)
 *   - clearPolicy: how the previous frame is disposed of (clear / fade / none)
 *   - blend: the compositing mode, which is PER-EFFECT (additive is right for
 *     emissive fields and wrong for snow/fog, which blow out to white)
 *   - the rAF loop, the generation-driven activation and the drain
 *
 * The particle field is ORTHOGONAL to the Off/While-generating/Always VISIBILITY
 * setting (that setting is unchanged; see `mode` / setMode / bb_ember_mode).
 * The selected field persists in localStorage (bb_particle_mode) and is switched
 * at runtime via the exported setParticleMode(id).
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

// Canvas sizing math lives in its own module because it is the one part of this
// engine that had a shipped, invisible-at-dpr=1 bug and no test. See sizing.js
// for why RESOLUTION (backingStore) and GEOMETRY (apparentScale) must stay apart.
import { backingStore } from './fx/sizing.js';
import { ensureSprites } from './fx/sprites.js';
import {
    DEFAULT_EFFECT_ID, createEnv, hasEffect, resolveEffectId, loadEffect, effectList,
    applyClearPolicy, applyBlend, resetBlend,
} from './fx/registry.js';
// Catalogue only — stubs with a lazy loader. Importing this costs nothing but
// the id/label table; no simulation code is fetched until a field is selected.
import './fx/effects/index.js';

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
let particleMode = DEFAULT_EFFECT_ID;   // FIELD: an id from the registry (bb_particle_mode)
let effect = null;           // the loaded descriptor for particleMode (null while importing)
let wantedEffectId = null;   // the id whose import is in flight (guards a fast double switch)
let pendingField = null;     // 'init' | 'rearm' — deferred until the module + canvas exist
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

// The environment handed to every effect hook. rand/clock are injected here (not
// reached for as globals inside effects) so effect physics can be driven from a
// headless unit test with a seeded generator.
const env = createEnv();

const prefersReduced = typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// Coarse-pointer (touch) halves particle counts. Evaluated ONCE at module load —
// baseCount() runs every frame, so never create an MQL ~60×/s in the hot path.
const coarsePointer = typeof window.matchMedia === 'function' &&
    window.matchMedia('(pointer:coarse)').matches;

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
// Viewport-area × density multiplier. Effects read it as env.scale rather than
// recomputing it, so there is one definition of "how busy is this viewport".
const fieldScale = () => Math.max(0.4, Math.min(2.4, (width * height) / (1440 * 900))) * FIELD.density;

// =============================================================================
// Effect selection + lifecycle (the only dispatch in this file)
// =============================================================================
function refreshEnv(now, dt) {
    env.width = width; env.height = height; env.dpr = dpr;
    env.now = now; env.dt = dt;
    env.active = active;
    env.scale = fieldScale();
    env.intensity = FIELD.intensity;
    env.surge = surge;
}

// Apply a deferred init/rearm once BOTH the effect module and the canvas exist.
// 'init' is a hard (re)build (mode switch); 'rearm' is the lazy revive used on
// activation — per-effect semantics live in the descriptor, not here.
function applyPendingField() {
    if (!effect || !canvas || !pendingField) return;
    refreshEnv(lastT, 0);
    if (pendingField === 'init') effect.init(env);
    else effect.rearm(env);
    pendingField = null;
}

// Lazily import the selected field. Returns a promise for callers that care;
// the engine itself is fire-and-forget (the loop simply draws nothing until the
// module lands, which is at most a frame or two behind the CSS fade-in).
function useEffect(id) {
    const wanted = resolveEffectId(id);
    if (effect && effect.id === wanted) return Promise.resolve(effect);
    wantedEffectId = wanted;
    effect = null;
    env.state = {};                       // each field owns its own scratch space
    return loadEffect(wanted).then((d) => {
        if (wantedEffectId !== wanted) return null;   // superseded by a newer selection
        effect = d;
        applyPendingField();
        return d;
    }).catch((err) => {
        console.warn('[EmberFX] failed to load field "' + wanted + '"', err);
        return null;
    });
}

// =============================================================================
// Canvas plumbing
// =============================================================================
function ensureCanvas() {
    if (canvas) return true;
    host = document.querySelector('section.chat') || document.querySelector('.chat');
    if (!host) return false;
    env.sprites = ensureSprites();
    canvas = document.createElement('canvas');
    canvas.id = 'emberCanvas';
    canvas.setAttribute('aria-hidden', 'true');
    host.insertBefore(canvas, host.firstChild);
    ctx = canvas.getContext('2d');
    resize();
    resizeObs = new ResizeObserver(resize);
    resizeObs.observe(host);
    watchDpr();
    return true;
}
// devicePixelRatio changes with no resize of the host box: dragging the window
// to a different-density monitor, changing OS display scale, or browser page
// zoom. resize() only ran from the host ResizeObserver, so the backing store
// went stale (blurry) until some unrelated layout change happened to fire it.
// A resolution media query is the documented way to observe this, and it must
// re-register after each change because the query pins the OLD value.
function watchDpr() {
    if (typeof window.matchMedia !== 'function') return;
    const mql = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    const onChange = () => { resize(); watchDpr(); };
    if (typeof mql.addEventListener === 'function') mql.addEventListener('change', onChange, { once: true });
    else if (typeof mql.addListener === 'function') mql.addListener(onChange);  // older Safari
}
function resize() {
    if (!canvas || !host) return;
    const w = host.clientWidth, h = host.clientHeight;
    if (w === 0 || h === 0) return;            // ignore transient 0-measure (e.g. display:none)
    const store = backingStore(w, h, window.devicePixelRatio);
    // No-op guard: ResizeObserver fires on every composer keystroke that changes
    // height. Reassigning canvas.width/height WIPES the buffer, which visibly
    // resets the ember heat-smear and the matrix rain, and reallocates ~18 MB.
    if (canvas.width === store.w && canvas.height === store.h) {
        width = w; height = h;
        return;
    }
    dpr = store.dpr;
    width = w; height = h;
    canvas.width = store.w;
    canvas.height = store.h;
    // Pin the CSS box explicitly. The stylesheet already does this (width/height
    // 100%), but a <canvas> is a replaced element that falls back to its
    // INTRINSIC attribute size the moment that rule is missing — which is
    // exactly the bug that shipped. Belt and braces: never trust the stylesheet
    // alone for the one property that silently magnifies the entire field.
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Let the live field react (column counts, re-scatter…). Effects that don't
    // care declare a no-op resize — the engine has no per-field knowledge.
    if (effect) { refreshEnv(lastT, 0); effect.resize(env); }
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
    if (effect) {
        refreshEnv(now, dt);
        effect.update(dt, env);
        // The engine owns compositing: dispose of the last frame per the field's
        // declared policy, select its blend, draw, then hand the context back
        // neutral. No effect ever sets globalCompositeOperation itself.
        applyClearPolicy(ctx, effect, env);
        applyBlend(ctx, effect);
        effect.draw(ctx, env);
        resetBlend(ctx);
    }
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
    lastT = env.clock();
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
        pendingField = 'rearm';                // make sure the field has state
        applyPendingField();                   // (deferred if the module is still loading)
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

// ---- PARTICLE field: any registered effect id (persisted, bb_particle_mode) --
// ONE whitelist, derived from the registry (hasEffect). It used to be written
// out three times — here, in setParticleMode and on the window.EmberFX surface —
// and the three copies were free to drift.
function loadParticleMode() {
    try {
        const m = localStorage.getItem('bb_particle_mode');
        if (hasEffect(m)) particleMode = m;
    } catch (e) { /* private mode / disabled storage */ }
}
function persistParticleMode(m) {
    try { localStorage.setItem('bb_particle_mode', m); } catch (e) { /* private mode */ }
}
// EXPORTED setter so the settings UI can switch fields live.
function setParticleMode(m) {
    if (!hasEffect(m)) return;
    if (m === particleMode) return;
    particleMode = m;
    persistParticleMode(m);
    // Re-init the newly selected field and clear any residue (smear/trail) from
    // the previous one so the switch is clean. The import is lazy, so the init
    // is deferred until the module lands; if the loop is live it picks the new
    // field on the next frame, and if idle the next activation rearms it.
    pendingField = 'init';
    useEffect(m);
    if (canvas) { applyPendingField(); clearCanvas(); }
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
    if (running && !rafId) { lastT = env.clock(); rafId = requestAnimationFrame(loop); } // resume render
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
            setParticleMode: (m) => { if (hasEffect(m)) { particleMode = m; persistParticleMode(m); } },
            getParticleMode,
            listParticleModes: effectList,
            isActive: () => false,
            _reduced: true
        };
        console.log('[EmberFX] prefers-reduced-motion — particle field disabled (field=' + particleMode + ')');
        return;
    }
    // Warm the selected field now (idle, off the critical path) so it is resident
    // before the first generation rather than one import behind it.
    useEffect(particleMode);
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
        listParticleModes: effectList,   // [{id,label}] for a registry-driven picker
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

// Reflect the persisted PARTICLE field into the settings radio group + drive
// setParticleMode on change. Sibling of initEmberModeControl (same markup/idiom):
// the ember-mode control governs VISIBILITY, this one governs the FIELD look.
// (M5 replaces the hand-written radios with options generated from
// window.EmberFX.listParticleModes(); this still works either way.)
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

// Exposed for the settings UI / external drivers.
export { setParticleMode, getParticleMode };
export default { initEmberFX, initEmberModeControl, initParticleModeControl, setParticleMode, getParticleMode };
