/**
 * signal-feed.js — "The Signal"
 *
 * A single RED, monospace HUD line that WAVES and MORPHS through the REAL system
 * telemetry of a chat turn (embed / search / rank / generate / tool / mint). It
 * replaces the old fake cycling "thinking" phrases (removed in Task 1.5).
 *
 * PRESENTATION-ONLY. This controller renders whatever labels it is `push()`ed —
 * it never reads, mutates, or persists conversation content. The caller feeds it
 * from the `system_activity` SSE event and the already-emitted `tool_start` /
 * `tool_result` events plus a mint line; that telemetry is NEVER written into the
 * saved message, the context, or any request payload.
 *
 * Framework-free. The morph + wave algorithm is ported verbatim from the approved
 * prototype (the-signal-prototype.html): ONE line; a continuous per-character sine
 * wave; morphing by diffing per position so ONLY changed characters animate (shared
 * characters hold); a brief scramble/decode tick on changed chars. No bubble, no
 * outline — just the glowing red line. Dissolves to nothing at the end.
 *
 * Public API (see class SignalLine):
 *   mount(container)  — build the line element inside `container`.
 *   push(label)       — morph the line to `label` (a short string).
 *   dissolve()        — morph to empty, fade out, and stop the wave loop.
 *   destroy()         — hard teardown (used on error paths to avoid a leaked rAF).
 */

// Wave animation is the only thing suppressed for reduced-motion users; the
// morph itself is replaced with an instant set (no scramble/decode churn).
const REDUCE_MOTION = typeof matchMedia !== 'undefined'
    && matchMedia('(prefers-reduced-motion:reduce)').matches;

// Charset used for the brief scramble/decode tick on changed characters.
const SCRAMBLE = "ABCDEF0123456789·→λ#@%$".split("");

let _stylesInjected = false;
function _injectStyles() {
    if (_stylesInjected || typeof document === 'undefined') return;
    _stylesInjected = true;
    const style = document.createElement('style');
    style.id = 'signal-line-styles';
    // Self-contained so the module drops straight in — red monospace, no chrome.
    style.textContent = `
.signal-line{position:fixed;left:0;right:0;bottom:80px;z-index:60;
  display:flex;justify-content:center;white-space:pre;pointer-events:none;
  font-family:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,"Liberation Mono",monospace;
  font-weight:500;font-size:14px;letter-spacing:.02em;line-height:1.6;
  color:hsl(2 100% 62%);text-shadow:0 0 12px hsl(2 100% 55% / .55)}
.signal-line .cell{display:inline-block;will-change:transform}
.signal-line .cell .g{display:inline-block;transition:opacity .16s ease, transform .16s ease, color .16s ease, text-shadow .16s ease;will-change:opacity,transform}
.signal-line .cell .g.swap{opacity:0;transform:translateY(-.28em) scale(.7)}
.signal-line .cell .g.hot{color:#fff;text-shadow:0 0 20px hsl(2 100% 70% / .95)}
.signal-line.dissolving{opacity:0;transition:opacity .5s ease}
`;
    (document.head || document.documentElement).appendChild(style);
}

export class SignalLine {
    constructor() {
        this.root = null;         // the flex line element (one row of .cell spans)
        this.cells = [];          // [{ cell, g }]
        this.curText = "";        // current rendered string (diffed against on push)
        this.pending = [];        // scheduled per-char animation timeouts
        this._raf = null;         // wave rAF handle
        this._idleStop = null;    // dissolve teardown timeout
        this._alive = false;
        // A CONTINUOUS horizontal highlight SWEEP travels across the characters so
        // the line reads as live/streaming (Brandon: the flat-hold looked stagnant).
        // A bright crest scans left→right, lifting + brightening each char as it
        // passes; the text stays in place and readable. Off for reduced-motion.
        this._sweepEnabled = !REDUCE_MOTION;
        this._sweepSpeed = 0.85;      // cycles/sec the highlight crest travels
        this._sweepWavelength = 13;   // characters between crests
    }

    /**
     * Build the line element inside `container`. Idempotent-safe: a second mount
     * is ignored. Returns `this` for chaining.
     * @param {HTMLElement} container
     */
    mount(container) {
        if (!container || this.root || typeof document === 'undefined') return this;
        _injectStyles();
        this.root = document.createElement('div');
        this.root.className = 'signal-line';
        this.root.setAttribute('aria-hidden', 'true'); // decorative telemetry HUD
        container.appendChild(this.root);
        this._alive = true;
        return this;
    }

    // Grow/shrink the row to exactly `n` cells (ported: ensureCells).
    _ensureCells(n) {
        const cells = this.cells;
        while (cells.length < n) {
            const cell = document.createElement('span');
            cell.className = 'cell';
            const g = document.createElement('span');
            g.className = 'g';
            g.textContent = ' ';
            cell.appendChild(g);
            this.root.appendChild(cell);
            cells.push({ cell, g });
        }
        while (cells.length > n) {
            cells.pop().cell.remove();
        }
    }

    /**
     * Morph the line to `label`. Only positions whose character changed animate;
     * shared characters hold in place (ported: setLine). A no-op if not mounted.
     * @param {string} label
     */
    push(label) {
        if (!this.root) return;
        const text = (label == null ? "" : String(label));

        // Cancel any in-flight per-char timers from the previous morph.
        this.pending.forEach(id => clearTimeout(id));
        this.pending = [];
        // A fresh push cancels a pending dissolve fade-out.
        this.root.classList.remove('dissolving');
        if (this._idleStop) { clearTimeout(this._idleStop); this._idleStop = null; }

        const L = Math.max(text.length, this.curText.length);
        this._ensureCells(L);

        // Reduced motion: set the row instantly, no scramble/decode, no wave.
        if (REDUCE_MOTION) {
            for (let i = 0; i < L; i++) {
                const target = text[i] !== undefined ? text[i] : "";
                const { cell, g } = this.cells[i];
                g.textContent = target === "" ? " " : target;
                g.classList.remove('swap', 'hot');
                cell.style.transform = 'none';
            }
            this.curText = text;
            return;
        }

        const stagger = Math.max(11, Math.min(34, Math.round(520 / Math.max(L, 1))));
        for (let i = 0; i < L; i++) {
            const target = text[i] !== undefined ? text[i] : "";
            const from = this.curText[i] !== undefined ? this.curText[i] : "";
            if (target === from) {
                // Shared char holds — no animation. But clear any leftover
                // swap/hot from a prior morph: this push already cleared the
                // pending timers that would have removed them, so a coincidentally
                // shared char could otherwise stay stuck invisible (opacity:0).
                this.cells[i].g.classList.remove('swap', 'hot');
                continue;
            }
            const g = this.cells[i].g;
            const d = i * stagger;
            this.pending.push(setTimeout(() => {
                g.classList.add('swap', 'hot');
                this.pending.push(setTimeout(() => {
                    g.textContent = target === "" ? " " : SCRAMBLE[(Math.random() * SCRAMBLE.length) | 0];
                }, 70));
                this.pending.push(setTimeout(() => {
                    g.textContent = target === "" ? " " : target;
                    g.classList.remove('swap');
                }, 150));
                this.pending.push(setTimeout(() => {
                    g.classList.remove('hot');
                }, 420));
            }, d));
        }
        this.curText = text;
        this._startWave(); // ensure the continuous highlight sweep is running
    }

    // Continuous highlight SWEEP: a bright crest travels across the line, lifting +
    // brightening each character as it passes (a "scanning" streaming feel). The
    // text stays in place; only brightness + a tiny lift move. Runs continuously
    // while the line has content; returns false only when empty so the loop stops.
    _waveFrame(now) {
        const cells = this.cells;
        if (cells.length === 0) return false;
        const t = now * 0.001 * this._sweepSpeed;
        const wl = this._sweepWavelength;
        for (let i = 0; i < cells.length; i++) {
            const s = Math.sin((i / wl - t) * Math.PI * 2);
            const h = s > 0 ? s * s : 0;   // 0..1 sharp crest (only the leading half lights)
            const st = cells[i].cell.style;
            if (h > 0.02) {
                st.transform = `translateY(${(-h * 2.2).toFixed(2)}px)`;
                st.filter = `brightness(${(1 + h * 1.7).toFixed(2)}) saturate(${(1 - h * 0.55).toFixed(2)})`;
            } else {
                st.transform = 'none';
                st.filter = '';
            }
        }
        return true;
    }

    _startWave() {
        if (this._raf != null || !this._alive || !this._sweepEnabled) return;
        const loop = (now) => {
            // Only reschedule while the envelope is still above ~0; once flat we
            // stop entirely so the line holds perfectly still until the next push.
            this._raf = this._waveFrame(now) ? requestAnimationFrame(loop) : null;
        };
        this._raf = requestAnimationFrame(loop);
    }

    _stopWave() {
        if (this._raf != null) {
            cancelAnimationFrame(this._raf);
            this._raf = null;
        }
    }

    /**
     * Morph to nothing, fade out, then stop the wave loop and blank the row.
     * The element itself is kept (empty, zero-height) so a later push() — e.g. the
     * mint line after /chat/save resolves — can re-animate the same line.
     */
    dissolve() {
        if (!this.root) return;
        this.push("");                       // scramble the visible chars out to blanks
        this.root.classList.add('dissolving'); // opacity fade
        if (this._idleStop) clearTimeout(this._idleStop);
        this._idleStop = setTimeout(() => {
            this._stopWave();
            this._ensureCells(0);
            this.curText = "";
            if (this.root) this.root.classList.remove('dissolving');
            this._idleStop = null;
        }, 700);
    }

    /** Hard teardown — cancels all timers/rAF and removes the element. */
    destroy() {
        this.pending.forEach(id => clearTimeout(id));
        this.pending = [];
        if (this._idleStop) { clearTimeout(this._idleStop); this._idleStop = null; }
        this._stopWave();
        this._alive = false;
        if (this.root) { this.root.remove(); this.root = null; }
        this.cells = [];
        this.curText = "";
    }
}
