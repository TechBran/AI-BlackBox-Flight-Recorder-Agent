/**
 * cli-agents-zellij-switcher.js
 * Left-rail list of the operator's active Zellij sessions, with click-to-
 * switch and per-row delete (×). Per docs/plans/2026-05-24-zellij-cli-
 * agent-rewrite.md T11.6.
 *
 * Polls GET /cli-agent/zellij/sessions?op={op}, hands a constructed session
 * URL back to the caller via onSwitch (caller wires it to the iframe
 * module), and DELETE /cli-agent/zellij/sessions/{name}?op={op} on × click.
 *
 * Auth model:
 *   sessionUrl is /app-proxy/9097/{encoded-name} with NO ?token=… query
 *   string. We rely on the same-origin session_token cookie that the
 *   launcher (T11.5) set via /command/login at launch time, which the
 *   browser auto-includes on the iframe navigation. The switcher itself
 *   never mints or sees tokens.
 *
 * Out of scope: launching (T11.5), iframe (T11b), modal wiring (T12),
 * CSS (T13).
 */

// Module-level singletons. One switcher per Portal page is the design.
let currentContainerEl = null;
let currentRailEl = null;
let currentOperator = null;
let currentCallbacks = {};
let currentActiveName = null;
let currentSessions = [];
let currentPollTimeoutId = null;
let currentPollIntervalMs = 5000;
// Increments on every mount/unmount. In-flight fetches snapshot this at
// fire time and drop their result if it no longer matches — guards
// against orphan callbacks landing after unmount or operator-switch reset.
let mountSerial = 0;

const SESSIONS_URL = '/cli-agent/zellij/sessions';
const PROXY_BASE = '/app-proxy/9097/';
const MIN_POLL_MS = 1000;
const MAX_POLL_MS = 60000;

function fireCb(cb, payload, label) {
    if (typeof cb !== 'function') return;
    try { cb(payload); } catch (err) { console.error(`[ZELLIJ-SWITCHER] ${label} threw:`, err); }
}
const isStillMounted = (serial) => serial === mountSerial;
const sessionUrlFor = (name) => PROXY_BASE + encodeURIComponent(name);

function clampPollInterval(ms) {
    if (typeof ms !== 'number' || !Number.isFinite(ms)) return 5000;
    return Math.min(MAX_POLL_MS, Math.max(MIN_POLL_MS, ms));
}

function renderEmpty() {
    currentRailEl.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'zellij-switcher-empty';
    empty.textContent = 'No active sessions';
    currentRailEl.appendChild(empty);
}

function makeItem(name, provider, app) {
    const item = document.createElement('div');
    item.className = 'zellij-switcher-item';
    if (name === currentActiveName) item.classList.add('zellij-switcher-item-active');
    item.dataset.sessionName = name;
    item.dataset.provider = provider;

    const label = document.createElement('div');
    label.className = 'zellij-switcher-item-label';
    const badge = document.createElement('span');
    badge.className = 'zellij-switcher-provider-badge';
    badge.dataset.provider = provider;
    badge.textContent = provider;
    const nameEl = document.createElement('span');
    nameEl.className = 'zellij-switcher-name';
    nameEl.textContent = name;
    label.append(badge, nameEl);
    item.appendChild(label);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'zellij-switcher-delete';
    del.setAttribute('aria-label', 'Delete session');
    del.textContent = '×';
    // stopPropagation keeps the row-level click from firing onSwitch on
    // the very session we're deleting.
    del.addEventListener('click', (ev) => { ev.stopPropagation(); deleteSession(name); });
    item.appendChild(del);

    item.addEventListener('click', () => {
        fireCb(currentCallbacks.onSwitch, { name, provider, app, sessionUrl: sessionUrlFor(name) }, 'onSwitch');
    });
    return item;
}

function renderSessions() {
    if (!currentRailEl) return;
    if (!currentSessions.length) { renderEmpty(); return; }
    currentRailEl.replaceChildren();
    for (const s of currentSessions) {
        currentRailEl.appendChild(makeItem(s.name, s.provider || 'terminal', s.app || null));
    }
}

async function deleteSession(name) {
    if (!currentOperator) return;
    const serial = mountSerial;
    const url = `${SESSIONS_URL}/${encodeURIComponent(name)}?op=${encodeURIComponent(currentOperator)}`;
    try {
        const resp = await fetch(url, { method: 'DELETE' });
        if (!isStillMounted(serial)) return;
        // 204/200/404 all count as "gone" — DELETE is idempotent server-side
        // and 404 means it was already cleaned up; reflect that either way.
        if (resp.ok || resp.status === 404) {
            fireCb(currentCallbacks.onDelete, { name }, 'onDelete');
        } else {
            const message = await resp.text().catch(() => '');
            fireCb(currentCallbacks.onError, { stage: 'delete', status: resp.status, message }, 'onError');
        }
    } catch (err) {
        if (!isStillMounted(serial)) return;
        fireCb(currentCallbacks.onError, { stage: 'delete', status: 0, message: String(err) }, 'onError');
    }
    // Always reconcile after a delete attempt — even on error the server
    // may or may not have removed the row; refresh() is the cheapest probe.
    if (isStillMounted(serial)) refresh();
}

async function pollOnce() {
    if (!currentOperator || !currentRailEl) return;
    const serial = mountSerial;
    const url = `${SESSIONS_URL}?op=${encodeURIComponent(currentOperator)}`;
    try {
        const resp = await fetch(url, { method: 'GET' });
        if (!isStillMounted(serial)) return;
        if (!resp.ok) {
            const message = await resp.text().catch(() => '');
            fireCb(currentCallbacks.onError, { stage: 'list', status: resp.status, message }, 'onError');
            return;
        }
        let data;
        try { data = await resp.json(); }
        catch (e) {
            fireCb(currentCallbacks.onError, { stage: 'list', status: resp.status, message: `malformed list response: ${e.message}` }, 'onError');
            return;
        }
        if (!isStillMounted(serial)) return;
        const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
        // Defensive shape filter — drop any row missing a string name.
        currentSessions = sessions.filter((s) => s && typeof s.name === 'string' && s.name);
        renderSessions();
    } catch (err) {
        if (!isStillMounted(serial)) return;
        fireCb(currentCallbacks.onError, { stage: 'list', status: 0, message: String(err) }, 'onError');
    }
}

// WHY setTimeout-not-setInterval: setInterval fires on a fixed cadence
// regardless of in-flight requests, which under a slow network can stack
// overlapping polls. Chained setTimeout keeps exactly one in-flight.
function pollAndReschedule(immediate) {
    if (currentPollTimeoutId !== null) { clearTimeout(currentPollTimeoutId); currentPollTimeoutId = null; }
    const serial = mountSerial;
    const tick = async () => {
        currentPollTimeoutId = null;
        if (!isStillMounted(serial)) return;
        await pollOnce();
        if (!isStillMounted(serial)) return;
        currentPollTimeoutId = setTimeout(tick, currentPollIntervalMs);
    };
    if (immediate) tick();
    else currentPollTimeoutId = setTimeout(tick, currentPollIntervalMs);
}

export function mountSwitcher(containerEl, options = {}) {
    if (!containerEl) {
        console.error('[ZELLIJ-SWITCHER] mountSwitcher called without container element');
        return null;
    }
    if (currentRailEl && currentContainerEl === containerEl) {
        console.warn('[ZELLIJ-SWITCHER] mountSwitcher is idempotent — second call on same container ignored');
        return currentRailEl;
    }
    if (currentRailEl && currentContainerEl !== containerEl) {
        console.warn('[ZELLIJ-SWITCHER] mountSwitcher called with new container before unmount — auto-cleaning prior mount');
        unmountSwitcher();
    }
    if (!options.operator || typeof options.operator !== 'string') {
        console.error('[ZELLIJ-SWITCHER] mountSwitcher requires options.operator (non-empty string)');
        return null;
    }

    mountSerial += 1;
    currentContainerEl = containerEl;
    currentOperator = options.operator;
    currentCallbacks = {
        onSwitch: options.onSwitch,
        onDelete: options.onDelete,
        onError: options.onError,
    };
    currentPollIntervalMs = clampPollInterval(options.pollIntervalMs);
    currentActiveName = null;
    currentSessions = [];

    const rail = document.createElement('div');
    rail.className = 'zellij-switcher-rail';
    containerEl.appendChild(rail);
    currentRailEl = rail;

    renderEmpty();
    pollAndReschedule(true);
    return rail;
}

export function setOperator(op) {
    if (typeof op !== 'string' || !op) {
        console.warn('[ZELLIJ-SWITCHER] setOperator requires a non-empty string; got:', op);
        return;
    }
    if (!currentRailEl) {
        console.warn('[ZELLIJ-SWITCHER] setOperator called before mountSwitcher — ignored');
        return;
    }
    currentOperator = op;
    // Different operator => different session set; current rows are stale.
    currentSessions = [];
    currentActiveName = null;
    renderEmpty();
    refresh();
}

export function markSessionActive(sessionName) {
    currentActiveName = sessionName || null;
    if (!currentRailEl) return;
    currentRailEl.querySelectorAll('.zellij-switcher-item').forEach((el) => {
        const match = currentActiveName && el.dataset.sessionName === currentActiveName;
        el.classList.toggle('zellij-switcher-item-active', !!match);
    });
}

export function refresh() {
    if (!currentRailEl) {
        console.warn('[ZELLIJ-SWITCHER] refresh called before mountSwitcher — ignored');
        return;
    }
    pollAndReschedule(true);
}

export function unmountSwitcher() {
    if (currentPollTimeoutId !== null) { clearTimeout(currentPollTimeoutId); currentPollTimeoutId = null; }
    if (currentRailEl && currentRailEl.parentNode) currentRailEl.parentNode.removeChild(currentRailEl);
    currentRailEl = null; currentContainerEl = null; currentOperator = null;
    currentCallbacks = {}; currentActiveName = null; currentSessions = [];
    // Bump serial so any in-flight pollOnce/deleteSession callbacks drop
    // their results on resolution (mountSerial mismatch = orphan).
    mountSerial += 1;
}
