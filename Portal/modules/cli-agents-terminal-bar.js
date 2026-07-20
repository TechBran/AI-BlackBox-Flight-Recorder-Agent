/**
 * cli-agents-terminal-bar.js
 * Toolbar row above the Zellij terminal iframe: rail-collapse toggle (☰),
 * active-session label, file attach (📎), and modal maximize (⛶).
 *
 * Per docs/plans/2026-07-20 terminal-file-attach plan Task 9 (web parity
 * with the Android terminal toolbar).
 *
 * Scope:
 *   - We render the toolbar row + own the attach upload flow (hidden file
 *     input, sequential XHR uploads with progress chip, outcome toasts).
 *   - We do NOT own the collapse/maximize CLASSES — the modal (T12 owner of
 *     the shell + card DOM) flips those in the onToggleRail/onToggleMaximize
 *     callbacks and persists rail state. We only track button glyph/state.
 *   - Toasts are module-local overlays anchored to the iframe host: the
 *     Portal's global #toast sits at z-index 100, the modal at 5000, so
 *     core-utils toasts are invisible while the modal is open — and the
 *     "paste failed" outcome needs a copy-path button the global toast
 *     can't render anyway.
 *
 * Backend contract (cli_agent_routes.py POST /cli-agent/zellij/attach-file):
 *   multipart FormData {file, session_name} + ?op= → 200 JSON
 *   {url, path, filename, session_folder, provider, injected}.
 *   injected:true  → server already bracketed-pasted the path into the pane.
 *   injected:false → upload succeeded but the paste failed (session dead
 *                    etc.) — surface the path so the user can paste manually.
 *
 * Exports (singleton, mirrors the launcher/switcher module shape):
 *   - mountTerminalBar(containerEl, options)
 *   - setActiveSession(sessionName | null)
 *   - attachFiles(files) — public entry into the upload pipeline (📎 input
 *     change AND the modal's drag-and-drop path both route through it)
 *   - unmountTerminalBar()
 */

// Module-level singletons. One toolbar per Portal page is the design.
let currentContainerEl = null;
let currentBarEl = null;
let currentLabelEl = null;
let currentAttachBtn = null;
let currentFileInput = null;
let currentOperator = null;
let currentOpts = {};
let currentSessionName = null;
let maximized = false;
let railCollapsed = false;
// Upload machinery — sequential drain so per-file progress/toasts stay
// unambiguous (parallel uploads would fight over the single chip).
let uploadQueue = [];
let uploadRunning = false;
let currentXhr = null;
let currentChipEl = null;
let currentToastStackEl = null;
const toastTimers = new Set();
// Increments on every mount/unmount. In-flight XHR callbacks snapshot this
// and drop their UI work if it no longer matches (orphan guard, same idiom
// as the switcher's poll serial).
let mountSerial = 0;

const ATTACH_URL = '/cli-agent/zellij/attach-file';
const NO_SESSION_LABEL = 'no session';
const TOAST_SUCCESS_MS = 4000;
const TOAST_STICKY_MS = 8000;   // warn + error variants persist longer

function fireCb(cb, payload, label) {
    if (typeof cb !== 'function') return;
    try { cb(payload); } catch (err) {
        console.error(`[ZELLIJ-TERMINAL-BAR] ${label} threw:`, err);
    }
}

// Chip + toasts anchor to the iframe host (position: relative) so they
// overlay the terminal without shifting layout. Resolved lazily — the
// toolbar host and iframe host are siblings inside the terminal column.
function overlayHost() {
    return currentContainerEl?.parentElement?.querySelector('.cli-agents-zellij-iframe-host')
        || currentContainerEl;
}

// ── Progress chip ────────────────────────────────────────────────────────

function setChip(text) {
    const host = overlayHost();
    if (!host) return;
    if (!currentChipEl || !currentChipEl.isConnected) {
        currentChipEl = document.createElement('div');
        currentChipEl.className = 'zellij-terminal-chip';
        host.appendChild(currentChipEl);
    }
    currentChipEl.textContent = text;
}

function clearChip() {
    if (currentChipEl?.parentNode) currentChipEl.parentNode.removeChild(currentChipEl);
    currentChipEl = null;
}

// ── Toasts ───────────────────────────────────────────────────────────────

function ensureToastStack() {
    if (currentToastStackEl && currentToastStackEl.isConnected) return currentToastStackEl;
    const host = overlayHost();
    if (!host) return null;
    currentToastStackEl = document.createElement('div');
    currentToastStackEl.className = 'zellij-terminal-toast-stack';
    // The toasts are the attach flow's ONLY feedback channel — announce
    // them to screen readers (polite: outcomes, not interruptions).
    currentToastStackEl.setAttribute('role', 'status');
    currentToastStackEl.setAttribute('aria-live', 'polite');
    host.appendChild(currentToastStackEl);
    return currentToastStackEl;
}

async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch { /* clipboard API denied — fall through to legacy path */ }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        return ok;
    } catch {
        return false;
    }
}

/**
 * kind: 'success' | 'warn' | 'error'. copyText renders a mono path line +
 * copy-to-clipboard button (the injected:false outcome).
 */
function showToast(kind, message, { copyText } = {}) {
    const stack = ensureToastStack();
    if (!stack) {
        // Never silent — worst case the outcome lands in the console.
        console.warn('[ZELLIJ-TERMINAL-BAR] toast host missing:', message, copyText || '');
        return;
    }
    const el = document.createElement('div');
    el.className = `zellij-terminal-toast zellij-terminal-toast-${kind}`;
    const msg = document.createElement('div');
    msg.className = 'zellij-terminal-toast-msg';
    msg.textContent = message;
    el.appendChild(msg);
    if (copyText) {
        const pathEl = document.createElement('div');
        pathEl.className = 'zellij-terminal-toast-path';
        pathEl.textContent = copyText;
        el.appendChild(pathEl);
        const copyBtn = document.createElement('button');
        copyBtn.type = 'button';
        copyBtn.className = 'zellij-terminal-toast-copy';
        copyBtn.textContent = 'Copy path';
        copyBtn.addEventListener('pointerdown', (ev) => ev.preventDefault());
        copyBtn.addEventListener('click', async () => {
            const ok = await copyToClipboard(copyText);
            copyBtn.textContent = ok ? '✓ Copied' : 'Copy failed — select the path above';
        });
        el.appendChild(copyBtn);
    }
    stack.appendChild(el);
    const ttl = kind === 'success' ? TOAST_SUCCESS_MS : TOAST_STICKY_MS;
    const timer = setTimeout(() => {
        toastTimers.delete(timer);
        if (el.parentNode) el.parentNode.removeChild(el);
    }, ttl);
    toastTimers.add(timer);
}

// ── Attach upload flow ───────────────────────────────────────────────────

function enqueueUploads(files) {
    for (const f of files) uploadQueue.push(f);
    if (!uploadRunning) drainUploads();
}

/**
 * Public entry into the upload pipeline — the ONE path every attach source
 * uses (📎 file-input change, modal drag-and-drop, future callers). Accepts
 * a FileList or File array. Validates mount + active session up front so
 * gesture-driven callers (drop) get one loud toast instead of a per-file
 * spray; uploadOne still re-checks the session per file (it can die
 * mid-queue). Returns true when files were queued.
 */
export function attachFiles(files) {
    if (!currentBarEl) {
        console.warn('[ZELLIJ-TERMINAL-BAR] attachFiles called before mountTerminalBar — ignored');
        return false;
    }
    const list = Array.from(files || []).filter((f) => f instanceof File);
    if (!list.length) return false;
    const sessionName = typeof currentOpts.getSessionName === 'function'
        ? (currentOpts.getSessionName() || null)
        : null;
    if (!sessionName) {
        showToast('error', '📎 No active terminal session — launch or select a terminal first');
        fireCb(currentOpts.onError,
            { stage: 'attach', status: 0, message: 'no active session' }, 'onError');
        return false;
    }
    enqueueUploads(list);
    return true;
}

async function drainUploads() {
    const serial = mountSerial;
    uploadRunning = true;
    while (uploadQueue.length && serial === mountSerial) {
        const file = uploadQueue.shift();
        await uploadOne(file, serial);
    }
    if (serial !== mountSerial) return; // unmounted mid-drain — state already reset
    uploadRunning = false;
    clearChip();
}

function uploadOne(file, serial) {
    return new Promise((resolve) => {
        // Session name is re-read AT UPLOAD TIME (not captured at click):
        // the user can switch sessions from inside the terminal while a
        // large upload queue drains, and the file must land in the folder
        // of whatever session is CURRENTLY loaded.
        const sessionName = typeof currentOpts.getSessionName === 'function'
            ? (currentOpts.getSessionName() || null)
            : null;
        if (!sessionName) {
            showToast('error', `📎 ${file.name} — no active terminal session`);
            fireCb(currentOpts.onError,
                { stage: 'attach', status: 0, message: 'no active session' }, 'onError');
            resolve();
            return;
        }

        setChip(`Uploading ${file.name}… 0%`);

        // XHR (not fetch): upload.progress is the whole point of the chip.
        const xhr = new XMLHttpRequest();
        currentXhr = xhr;
        xhr.open('POST', `${ATTACH_URL}?op=${encodeURIComponent(currentOperator)}`);

        xhr.upload.addEventListener('progress', (ev) => {
            if (serial !== mountSerial) return;
            if (ev.lengthComputable && ev.total > 0) {
                const pct = Math.min(100, Math.round((ev.loaded / ev.total) * 100));
                setChip(`Uploading ${file.name}… ${pct}%`);
            } else {
                setChip(`Uploading ${file.name}…`);
            }
        });

        const settle = () => {
            if (currentXhr === xhr) currentXhr = null;
            resolve();
        };

        xhr.addEventListener('load', () => {
            if (serial !== mountSerial) return settle();
            let body = null;
            try { body = JSON.parse(xhr.responseText); } catch { /* non-JSON error body */ }
            if (xhr.status >= 200 && xhr.status < 300 && body) {
                if (body.injected === true) {
                    showToast('success', `📎 ${body.filename || file.name} — path pasted into terminal`);
                } else {
                    // Upload succeeded, paste didn't (session dead, zellij
                    // hiccup). The file is on disk — hand over the path.
                    showToast('warn',
                        `📎 ${body.filename || file.name} uploaded — paste failed. Copy the path into the terminal:`,
                        { copyText: body.path || '' });
                }
            } else {
                // A 2xx that landed here means the body didn't parse — the
                // upload may well have succeeded, so don't toast the
                // self-contradictory "failed — HTTP 200".
                const is2xxUnparsable = xhr.status >= 200 && xhr.status < 300;
                const rawDetail = body && (body.detail ?? body.message);
                const detail = typeof rawDetail === 'string' && rawDetail
                    ? rawDetail
                    : (rawDetail ? JSON.stringify(rawDetail)
                        : (is2xxUnparsable ? 'unexpected server response'
                            : `HTTP ${xhr.status}`));
                showToast('error', `📎 ${file.name} failed — ${detail}`);
                fireCb(currentOpts.onError,
                    { stage: 'attach', status: xhr.status, message: detail }, 'onError');
            }
            settle();
        });

        xhr.addEventListener('error', () => {
            if (serial === mountSerial) {
                showToast('error', `📎 ${file.name} failed — network error`);
                fireCb(currentOpts.onError,
                    { stage: 'attach', status: 0, message: 'network error' }, 'onError');
            }
            settle();
        });

        xhr.addEventListener('abort', settle);

        const fd = new FormData();
        fd.append('file', file, file.name);
        fd.append('session_name', sessionName);
        xhr.send(fd);
    });
}

// ── Toolbar construction ─────────────────────────────────────────────────

function makeBarButton(extraClass, glyph, title) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `zellij-terminal-bar-btn ${extraClass}`;
    btn.textContent = glyph;
    btn.title = title;
    // Glyph-only buttons: the accessible name comes from aria-label (the
    // emoji/box-drawing textContent is meaningless to screen readers).
    btn.setAttribute('aria-label', title);
    // Focus-preservation rule (live-verified): preventDefault on pointerdown
    // stops the button from stealing keyboard focus from the terminal
    // iframe; click still fires normally. NEVER touchstart+preventDefault
    // (that kills the click on touch devices).
    btn.addEventListener('pointerdown', (ev) => ev.preventDefault());
    return btn;
}

export function mountTerminalBar(containerEl, options = {}) {
    if (!containerEl) {
        console.error('[ZELLIJ-TERMINAL-BAR] mountTerminalBar called without container element');
        return null;
    }
    // Validate BEFORE the auto-unmount below — a bad call (missing operator)
    // must reject without destroying an already-working toolbar.
    if (!options.operator || typeof options.operator !== 'string') {
        console.error('[ZELLIJ-TERMINAL-BAR] mountTerminalBar requires options.operator (non-empty string)');
        return null;
    }
    if (currentBarEl && currentContainerEl === containerEl) {
        console.warn('[ZELLIJ-TERMINAL-BAR] mountTerminalBar is idempotent — second call on same container ignored');
        return currentBarEl;
    }
    if (currentBarEl && currentContainerEl !== containerEl) {
        console.warn('[ZELLIJ-TERMINAL-BAR] mountTerminalBar called with new container before unmount — auto-cleaning prior mount');
        unmountTerminalBar();
    }

    mountSerial += 1;
    currentContainerEl = containerEl;
    currentOperator = options.operator;
    currentOpts = {
        getSessionName: options.getSessionName,
        onToggleRail: options.onToggleRail,
        onToggleMaximize: options.onToggleMaximize,
        onError: options.onError,
    };
    currentSessionName = null;
    maximized = false; // always fresh — maximize never survives a remount
    railCollapsed = !!options.railCollapsed; // caller passes persisted state

    const bar = document.createElement('div');
    bar.className = 'zellij-terminal-bar';

    const railBtn = makeBarButton('zellij-terminal-bar-rail', '☰', 'Toggle session list');
    railBtn.setAttribute('aria-expanded', String(!railCollapsed));
    railBtn.addEventListener('click', () => {
        railCollapsed = !railCollapsed;
        railBtn.setAttribute('aria-expanded', String(!railCollapsed));
        // The modal owns the shell element + localStorage persistence.
        fireCb(currentOpts.onToggleRail, { collapsed: railCollapsed }, 'onToggleRail');
    });

    const label = document.createElement('span');
    label.className = 'zellij-terminal-bar-label zellij-terminal-bar-label-empty';
    label.textContent = NO_SESSION_LABEL;
    label.title = 'No terminal session loaded';

    const attachBtn = makeBarButton('zellij-terminal-bar-attach', '📎', 'Launch a terminal first');
    attachBtn.disabled = true;
    attachBtn.addEventListener('click', () => {
        if (!currentSessionName || !currentFileInput) return;
        currentFileInput.click();
    });

    const maxBtn = makeBarButton('zellij-terminal-bar-max', '⛶', 'Maximize terminal');
    maxBtn.setAttribute('aria-pressed', 'false');
    maxBtn.addEventListener('click', () => {
        maximized = !maximized;
        maxBtn.textContent = maximized ? '🗗' : '⛶';
        maxBtn.title = maximized ? 'Restore window size' : 'Maximize terminal';
        maxBtn.setAttribute('aria-label', maxBtn.title);
        maxBtn.setAttribute('aria-pressed', String(maximized));
        // The modal owns the card element and flips .cli-agents-maximized.
        fireCb(currentOpts.onToggleMaximize, { maximized }, 'onToggleMaximize');
    });

    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.style.display = 'none';
    input.addEventListener('change', () => {
        const files = Array.from(input.files || []);
        input.value = ''; // allow re-attaching the same file later
        if (files.length) attachFiles(files); // one pipeline with drag-and-drop
    });

    bar.appendChild(railBtn);
    bar.appendChild(label);
    bar.appendChild(attachBtn);
    bar.appendChild(maxBtn);
    bar.appendChild(input);
    containerEl.appendChild(bar);

    currentBarEl = bar;
    currentLabelEl = label;
    currentAttachBtn = attachBtn;
    currentFileInput = input;
    return bar;
}

// Driven by the caller (modal) from all three session-change paths: launch
// success, rail onSwitch, and the iframe's onSessionChanged (in-terminal
// switches). Pass null when no session is loaded (disables attach).
export function setActiveSession(sessionName) {
    if (!currentBarEl) {
        console.warn('[ZELLIJ-TERMINAL-BAR] setActiveSession called before mountTerminalBar — ignored');
        return;
    }
    currentSessionName = (typeof sessionName === 'string' && sessionName) ? sessionName : null;
    if (currentLabelEl) {
        currentLabelEl.textContent = currentSessionName || NO_SESSION_LABEL;
        currentLabelEl.title = currentSessionName || 'No terminal session loaded';
        currentLabelEl.classList.toggle('zellij-terminal-bar-label-empty', !currentSessionName);
    }
    if (currentAttachBtn) {
        currentAttachBtn.disabled = !currentSessionName;
        currentAttachBtn.title = currentSessionName
            ? 'Attach a file — uploads and pastes its path into the terminal'
            : 'Launch a terminal first';
        currentAttachBtn.setAttribute('aria-label', currentAttachBtn.title);
    }
}

export function unmountTerminalBar() {
    if (currentXhr) {
        try { currentXhr.abort(); } catch { /* already settled */ }
        currentXhr = null;
    }
    uploadQueue = [];
    uploadRunning = false;
    for (const t of toastTimers) clearTimeout(t);
    toastTimers.clear();
    clearChip();
    if (currentToastStackEl?.parentNode) {
        currentToastStackEl.parentNode.removeChild(currentToastStackEl);
    }
    currentToastStackEl = null;
    if (currentBarEl?.parentNode) currentBarEl.parentNode.removeChild(currentBarEl);
    currentBarEl = null;
    currentContainerEl = null;
    currentLabelEl = null;
    currentAttachBtn = null;
    currentFileInput = null;
    currentOperator = null;
    currentOpts = {};
    currentSessionName = null;
    maximized = false;
    railCollapsed = false;
    // Bump serial so in-flight XHR callbacks drop their UI work on
    // resolution (mountSerial mismatch = orphan).
    mountSerial += 1;
}
