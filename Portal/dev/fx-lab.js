// fx-lab.js — the FX Lab gallery's script (was inline in fx-lab.html; the
// Portal's Content-Security-Policy runs no inline script). The page and its
// purpose are described in fx-lab.html.
import {
    effectList, loadEffect, createEnv,
    applyClearPolicy, applyBlend, resetBlend,
} from '../modules/fx/registry.js';
import { ensureSprites } from '../modules/fx/sprites.js';
import { backingStore } from '../modules/fx/sizing.js';
import '../modules/fx/effects/index.js';

const grid = document.getElementById('grid');
const errBox = document.getElementById('err');

// A transcript with a mix of long prose, a short line and code — the three
// things that actually get hard to read over a busy backdrop.
const TRANSCRIPT = [
    ['bot', 'The snapshot ledger is append-only, so every turn you take is recoverable — including the ones you would rather forget.'],
    ['user', 'what did we decide about the reranker?'],
    ['bot', 'Cohere by default, hardware-tiered, body-only. The local cross-encoder stays behind a flag until the GPU box is the primary.'],
];

function buildTranscript(el) {
    for (const [who, text] of TRANSCRIPT) {
        const d = document.createElement('div');
        d.className = 'msg ' + (who === 'user' ? 'user' : 'bot');
        d.textContent = text;
        el.appendChild(d);
    }
}

// One independent driver per card. This is deliberately a *reimplementation* of
// the engine's frame loop in miniature: if an effect needs something the
// descriptor does not expose, it will fail here, which is the point.
function mountCard(meta) {
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="card-head">
        <span class="name"></span><span class="id"></span>
        <span class="badge"></span>
      </div>
      <div class="stage"><canvas></canvas><div class="transcript"></div></div>
      <div class="controls">
        <label>Density</label><input type="range" min="0.2" max="2.5" step="0.05" value="1"><output>1.00</output>
        <label>Speed</label><input type="range" min="0.1" max="3" step="0.05" value="1"><output>1.00</output>
        <div class="row">
          <button data-act="play" aria-pressed="true">Pause</button>
          <button data-act="reseed">Reseed</button>
          <span class="fps">—</span>
        </div>
      </div>`;
    card.querySelector('.name').textContent = meta.label;
    card.querySelector('.id').textContent = meta.id;
    buildTranscript(card.querySelector('.transcript'));
    grid.appendChild(card);

    const canvas = card.querySelector('canvas');
    const badge = card.querySelector('.badge');
    const fpsEl = card.querySelector('.fps');
    const [densEl, speedEl] = card.querySelectorAll('input[type=range]');
    const [densOut, speedOut] = card.querySelectorAll('output');
    const playBtn = card.querySelector('[data-act=play]');
    const reseedBtn = card.querySelector('[data-act=reseed]');

    const ctx = canvas.getContext('2d');
    let descriptor = null, env = null, raf = 0, last = 0, playing = true;
    let frames = 0, fpsT = 0;

    function sizeCanvas() {
        const r = canvas.getBoundingClientRect();
        if (!r.width || !r.height) return false;
        const s = backingStore(r.width, r.height, window.devicePixelRatio);
        canvas.width = s.w; canvas.height = s.h;
        canvas.style.width = r.width + 'px';
        canvas.style.height = r.height + 'px';
        ctx.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
        if (env) { env.width = r.width; env.height = r.height; env.dpr = s.dpr; }
        return true;
    }

    // Seeded PRNG (mulberry32) so "Reseed" is reproducible and two cards of the
    // same effect are not accidentally identical.
    function makeRand(seed) {
        let a = seed >>> 0;
        return () => {
            a = (a + 0x6D2B79F5) >>> 0;
            let t = Math.imul(a ^ (a >>> 15), 1 | a);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    // The engine's own viewport-area density law. A lab card is far smaller than
    // the chat pane, so passing a raw 1.0 would spawn a full-screen population
    // into a thumbnail — which is what made Embers look like soup on first run.
    function areaScale() {
        const a = (env.width * env.height) / (1440 * 900);
        return Math.max(0.4, Math.min(2.4, a));
    }

    // Run the sim forward WITHOUT drawing so the card opens on the effect's
    // steady state. Several effects (Matrix, snow, rain) legitimately start
    // off-canvas and take seconds to fall into view; a gallery that shows their
    // empty entrance is showing the wrong thing.
    const WARMUP_FRAMES = 180;
    function warmUp() {
        for (let i = 0; i < WARMUP_FRAMES; i++) {
            env.now += 1000 / 60;
            env.dt = 1 / 60;
            env.scale = areaScale() * Number(densEl.value);
            descriptor.update(env.dt, env);
        }
    }

    function seed(n) {
        env = createEnv({ rand: makeRand(n), sprites: ensureSprites(), active: true });
        env.now = 1000;                       // non-zero: some effects gate on elapsed time
        sizeCanvas();
        if (descriptor.resize) descriptor.resize(env);
        descriptor.init(env);
        warmUp();
    }

    function frame(now) {
        raf = requestAnimationFrame(frame);
        if (!playing) { last = now; return; }
        const dt = Math.min((now - last) / 1000, 0.05) * Number(speedEl.value);
        last = now;
        env.now = now; env.dt = dt;
        env.scale = areaScale() * Number(densEl.value);
        applyClearPolicy(ctx, descriptor, env);
        applyBlend(ctx, descriptor);
        descriptor.update(dt, env);
        descriptor.draw(ctx, env);
        resetBlend(ctx);

        frames++;
        if (now - fpsT > 500) {
            fpsEl.textContent = Math.round(frames * 1000 / (now - fpsT)) + ' fps';
            frames = 0; fpsT = now;
        }
    }

    densEl.addEventListener('input', () => { densOut.value = Number(densEl.value).toFixed(2); });
    speedEl.addEventListener('input', () => { speedOut.value = Number(speedEl.value).toFixed(2); });
    playBtn.addEventListener('click', () => {
        playing = !playing;
        playBtn.textContent = playing ? 'Pause' : 'Play';
        playBtn.setAttribute('aria-pressed', String(playing));
    });
    reseedBtn.addEventListener('click', () => seed((Math.random() * 1e9) | 0));
    new ResizeObserver(() => { if (env) { sizeCanvas(); if (descriptor.resize) descriptor.resize(env); } })
        .observe(canvas);

    loadEffect(meta.id).then(d => {
        descriptor = d;
        badge.textContent = d.blend === 'lighter' ? 'additive' : d.blend;
        if (d.blend === 'lighter') badge.classList.add('additive');
        seed(1337);
        raf = requestAnimationFrame(frame);
    }).catch(e => {
        card.querySelector('.stage').innerHTML =
            `<div class="err">${meta.id}: ${String(e && e.message || e)}</div>`;
        console.error('[fx-lab]', meta.id, e);
    });
}

const list = effectList();
document.getElementById('countOut').textContent = list.length;
document.getElementById('dprOut').textContent = (window.devicePixelRatio || 1).toFixed(2);
if (!list.length) {
    errBox.hidden = false;
    errBox.textContent = 'No effects registered — check modules/fx/effects/index.js';
}
for (const meta of list) mountCard(meta);
