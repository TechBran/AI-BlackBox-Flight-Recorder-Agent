/**
 * core-utils.js
 * Foundation utilities used across all modules
 *
 * Source: Portal/app.js lines 3146-3175, 3706-3714
 * Refactored: 2025-12-20
 */

// =============================================================================
// DOM Helpers
// =============================================================================

/**
 * DOM element selector helper - shorthand for getElementById
 * @param {string} id - Element ID to find
 * @returns {HTMLElement|null} The found element or null
 */
export const $ = (id) => document.getElementById(id);

// =============================================================================
// HTML Escaping (the shared helpers — use these at every innerHTML sink)
// =============================================================================

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/**
 * Escape a value for use as HTML element content.
 *
 * Pure string work (no DOM), so it also escapes both quote characters — the
 * old per-module `div.textContent = x; return div.innerHTML` copies did NOT,
 * which made them unsafe inside attribute values. null/undefined -> ''.
 * @param {*} value - Any value; non-strings are stringified
 * @returns {string} Escaped text, safe between tags
 */
export function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

/**
 * Escape a value for use inside a QUOTED attribute value (`attr="${...}"`).
 *
 * Same escaping as escapeHtml (both quotes included); the separate name marks
 * the attribute context at the call site. Never use it for unquoted attributes
 * or inside inline JS handlers (onclick="...") — build those with
 * addEventListener instead.
 * @param {*} value - Any value; non-strings are stringified
 * @returns {string} Escaped text, safe inside "..." or '...'
 */
export function escapeAttr(value) {
    return escapeHtml(value);
}

// =============================================================================
// Toast Notifications
// =============================================================================

/**
 * Display a toast notification message
 * @param {string} msg - Message to display
 */
export function toast(msg) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hide");
    setTimeout(() => t.classList.add("hide"), 2000);
}

/**
 * Display a success toast notification (green styling)
 * @param {string} msg - Success message to display
 */
export function toastSuccess(msg) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hide", "error");
    t.classList.add("success");
    setTimeout(() => {
        t.classList.add("hide");
        t.classList.remove("success");
    }, 3000);
}

/**
 * Display an error toast notification (red styling)
 * @param {string} msg - Error message to display
 */
export function toastError(msg) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.remove("hide", "success");
    t.classList.add("error");
    setTimeout(() => {
        t.classList.add("hide");
        t.classList.remove("error");
    }, 4000);
}

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Generate a simple hash for string caching purposes
 * @param {string} str - String to hash
 * @returns {string} Base36 hash string
 */
export function simpleHash(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        const char = str.charCodeAt(i);
        hash = ((hash << 5) - hash) + char;
        hash = hash & hash; // Convert to 32bit integer
    }
    return hash.toString(36);
}

// =============================================================================
// Async Utilities
// =============================================================================

/**
 * Sleep/delay helper for async operations
 * @param {number} ms - Milliseconds to wait
 * @returns {Promise<void>} Promise that resolves after delay
 */
export const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

/**
 * The one poller the Portal uses (audit P01-chat-core-web-21, rank 67).
 *
 * Bare setInterval pollers kept a backgrounded tab or the Android WebView
 * hitting the box every few seconds for the life of the page, started a
 * second request while a slow one was still in flight, and kept their full
 * cadence through a 60-90 s service restart. A createPoller poller:
 *  - waits intervalMs from the END of one tick to the start of the next, so a
 *    tick never overlaps itself (poke() while one is in flight is a no-op);
 *  - pauses while the page is hidden (no timer is even armed) and runs once
 *    as soon as it is visible again. A poller whose tick raises a
 *    notification (the task pollers) passes hiddenIntervalMs instead: it keeps
 *    polling while hidden at that slower pace, since a paused poller only
 *    learns a job finished once the operator is already back on the page;
 *  - backs off exponentially while tick() throws or rejects, up to
 *    maxIntervalMs, and returns to intervalMs on the next success.
 *
 * A tick may call stop() on its own poller (e.g. when nothing is left to
 * watch); start() revives it.
 *
 * @param {() => (Promise<*>|*)} tick - One poll. A throw/rejection is a failure.
 * @param {object} opts
 * @param {number} opts.intervalMs - Delay between ticks
 * @param {number} [opts.maxIntervalMs] - Backoff ceiling (default 8x intervalMs)
 * @param {boolean} [opts.immediate=true] - First tick on start() (else after one interval)
 * @param {boolean} [opts.pauseWhenHidden=true] - Pause while document.hidden
 * @param {?number} [opts.hiddenIntervalMs=null] - Keep polling while hidden,
 *   this far apart (pauseWhenHidden is then moot)
 * @param {(err: *, failures: number) => void} [opts.onError] - After each failed tick
 * @param {() => void} [opts.onRecover] - After the first success that follows a failure
 * @param {{setTimeout: Function, clearTimeout: Function}} [opts.timers] - Injectable for tests
 * @returns {{start: () => void, stop: () => void, poke: () => void, readonly running: boolean, readonly failures: number}}
 */
export function createPoller(tick, {
    intervalMs,
    maxIntervalMs = intervalMs * 8,
    immediate = true,
    pauseWhenHidden = true,
    hiddenIntervalMs = null,
    onError = null,
    onRecover = null,
    timers = null,
} = {}) {
    const setT = timers ? timers.setTimeout.bind(timers) : (fn, ms) => setTimeout(fn, ms);
    const clearT = timers ? timers.clearTimeout.bind(timers) : (id) => clearTimeout(id);
    const doc = typeof document !== 'undefined' ? document : null;
    let running = false;
    let inFlight = false;
    let timer = null;
    let failures = 0;

    const slowWhenHidden = typeof hiddenIntervalMs === 'number' && hiddenIntervalMs > 0;
    const watchVisibility = !!doc && (pauseWhenHidden || slowWhenHidden);
    const pageHidden = () => !!doc && doc.hidden === true;
    // Paused: hidden, and no hidden pace to keep.
    const hidden = () => pauseWhenHidden && !slowWhenHidden && pageHidden();

    const clearTimer = () => {
        if (timer !== null) {
            clearT(timer);
            timer = null;
        }
    };

    const schedule = (delay) => {
        clearTimer();
        if (!running || hidden()) return;
        timer = setT(run, delay);
    };

    const nextDelay = () => {
        const base = slowWhenHidden && pageHidden() ? hiddenIntervalMs : intervalMs;
        return failures
            ? Math.min(base * Math.pow(2, failures), Math.max(maxIntervalMs, base))
            : base;
    };

    async function run() {
        timer = null;
        if (!running || inFlight || hidden()) return;
        inFlight = true;
        try {
            await tick();
            if (failures) {
                failures = 0;
                if (onRecover) onRecover();
            }
        } catch (err) {
            failures += 1;
            if (onError) {
                try { onError(err, failures); } catch (_) { /* a reporter never kills the poller */ }
            }
        } finally {
            inFlight = false;
            schedule(nextDelay());
        }
    }

    const onVisibility = () => {
        if (!running) return;
        if (pageHidden()) {
            // A slow-paced poller lets its armed timer fire; the next delay
            // is the hidden one.
            if (hidden()) clearTimer();
        } else if (!inFlight) {
            clearTimer(); // the hidden-pace timer, if one is armed
            run();
        }
    };

    return {
        start() {
            if (running) return;
            running = true;
            if (watchVisibility) doc.addEventListener('visibilitychange', onVisibility);
            if (immediate) run();
            else schedule(intervalMs);
        },
        stop() {
            running = false;
            clearTimer();
            if (watchVisibility) doc.removeEventListener('visibilitychange', onVisibility);
        },
        poke() {
            if (!running || inFlight) return;
            clearTimer();
            run();
        },
        get running() { return running; },
        get failures() { return failures; },
    };
}

// =============================================================================
// Debug Utilities
// =============================================================================

/**
 * Log with module prefix for debugging
 * @param {string} module - Module name
 * @param  {...any} args - Arguments to log
 */
export function debugLog(module, ...args) {
    console.log(`[${module}]`, ...args);
}
