/**
 * cli-agents-extra-keys.js
 * Mobile extra-keys bar below the Zellij terminal iframe — parity with
 * Android's ExtraKeysBar (ui/cli_agent/ExtraKeysBar.kt) for the plan's
 * key set (Ctrl/Alt/mic intentionally omitted on web).
 *
 * Per docs/plans/2026-07-20 terminal-file-attach plan Task 12.
 *
 * Layout: [Esc | divider | scrollable strip]. Esc is PINNED outside the
 * horizontally-scrollable strip — the Android "dead Esc" regression proved
 * that a scroll container's gesture arbitration eats taps at the scroll
 * edge (a tap with the tiniest sideways drift gets claimed as a scroll and
 * the click never fires). Same hazard exists for touch browsers, so the
 * interrupt key lives in a fixed slot that never competes with the strip.
 *
 * Delivery channel: POST /cli-agent/zellij/send-key?op={op} with body
 * {"session": name, "bytes": [ints]} → `zellij action write` server-side.
 * We use send-key for ALL bar keys, not just Esc:
 *   - CRITICAL: zellij-web's terminal-WS input parser holds a BARE trailing
 *     ESC frame forever (no ESC timeout on the web path) — a lone 0x1b sent
 *     over the iframe's WebSocket hangs its parser, so the send-key relay is
 *     the ONLY correct Esc path (the fix Android shipped first).
 *   - Consistency: one sanctioned channel for every key; the 32-byte cap
 *     covers every chord in this bar (largest payload is 30 bytes).
 *
 * Focus rule (live-verified on the terminal bar): every button preventDefaults
 * pointerdown — click still fires, but the button never steals keyboard focus
 * from the terminal iframe (and never dismisses the mobile IME). NEVER
 * touchstart+preventDefault (that kills the click on touch devices). After a
 * send we also best-effort refocus zellij-web's xterm (same-origin via the
 * app-proxy exposes window.term on the iframe's contentWindow).
 *
 * Sticky Shift (Android parity):
 *   tap        → armed (one-shot; cleared after the next key — ANY key, even
 *                one with no shifted variant, consumes the arm)
 *   long-press → locked (~500ms hold) until tapped again
 * Shift only alters keys that define a shifted variant (arrows, PgUp/PgDn);
 * everything else always sends its bare bytes.
 *
 * Exports (singleton, mirrors the terminal-bar's mount/unmount discipline):
 *   - mountExtraKeys(containerEl, { operator, getSessionName, onError })
 *   - setActiveSession(sessionName | null) — enable/disable the keys
 *   - setExtraKeysVisible(bool) — the MODAL owns visibility state/persistence
 *   - unmountExtraKeys()
 */

// Module-level singletons. One extra-keys bar per Portal page is the design.
let currentContainerEl = null;
let currentBarEl = null;
let currentOperator = null;
let currentOpts = {};
let currentSessionName = null;
let keyButtons = [];        // every sendable key button (for enable/disable)
let shiftBtn = null;
let shiftState = 'off';     // 'off' | 'armed' | 'locked'
let shiftHoldTimer = null;
let shiftLongPressFired = false;
// Sequential send chain: each tap's POST dispatches only after the previous
// one settled, so rapid taps can never reorder at the network layer. Taps
// enqueue synchronously — the UI is never blocked.
let sendChain = Promise.resolve();
// Increments on every mount/unmount. In-flight fetch callbacks snapshot this
// and drop their work if it no longer matches (orphan guard, same idiom as
// the terminal bar's upload XHRs).
let mountSerial = 0;
// Send-failure feedback state (see reportSendFailure for the policy).
let failureTimes = [];
let lastErrorToastAt = 0;
let errorToastEl = null;
let errorToastTimer = null;

const SEND_KEY_URL = '/cli-agent/zellij/send-key';
const SHIFT_HOLD_MS = 500;          // long-press threshold (Android parity)
const FAILURE_REPEAT_WINDOW_MS = 10000;
const ERROR_TOAST_MIN_INTERVAL_MS = 8000;
const ERROR_TOAST_TTL_MS = 5000;

// ── Key byte tables (parity matrix — exact) ──────────────────────────────

const ESC_BYTES = [27];

// Bare PgUp/PgDn: zellij-web ignores ESC[5~/ESC[6~ for viewport scroll, but
// honors SGR mouse wheel reports. One send-key call carries 3× wheel-up
// ESC[<64;1;1M (or wheel-down ESC[<65;1;1M) = 30 bytes, inside the relay's
// 32-byte cap — one tap scrolls a visible chunk instead of a single line.
const WHEEL_UP_ONE = [27, 91, 60, 54, 52, 59, 49, 59, 49, 77];   // ESC [ < 6 4 ; 1 ; 1 M
const WHEEL_DOWN_ONE = [27, 91, 60, 54, 53, 59, 49, 59, 49, 77]; // ESC [ < 6 5 ; 1 ; 1 M
const PGUP_BARE = [...WHEEL_UP_ONE, ...WHEEL_UP_ONE, ...WHEEL_UP_ONE];
const PGDN_BARE = [...WHEEL_DOWN_ONE, ...WHEEL_DOWN_ONE, ...WHEEL_DOWN_ONE];

// Scrollable-strip keys, in Android's order. `shiftBytes` is the xterm
// modifier-2 (Shift) encoding — e.g. Shift+PgUp = ESC[5;2~, the sequence
// Claude Code's scroll binding actually fires on (bare ESC[5~ doesn't).
// { shift: true } marks the sticky Shift slot's position in the strip.
const STRIP_KEYS = [
    { label: 'Tab',  aria: 'Tab',         bytes: [9] },
    { label: '↵',    aria: 'Enter',       bytes: [13] },
    { label: '⌫',    aria: 'Backspace',   bytes: [127] },
    { label: '/',    aria: 'Slash',       bytes: [47] },
    { shift: true },
    { label: '←',    aria: 'Arrow left',  bytes: [27, 91, 68], shiftBytes: [27, 91, 49, 59, 50, 68] },
    { label: '↓',    aria: 'Arrow down',  bytes: [27, 91, 66], shiftBytes: [27, 91, 49, 59, 50, 66] },
    { label: '↑',    aria: 'Arrow up',    bytes: [27, 91, 65], shiftBytes: [27, 91, 49, 59, 50, 65] },
    { label: '→',    aria: 'Arrow right', bytes: [27, 91, 67], shiftBytes: [27, 91, 49, 59, 50, 67] },
    { label: 'PgUp', aria: 'Page up',     bytes: PGUP_BARE,    shiftBytes: [27, 91, 53, 59, 50, 126] },
    { label: 'PgDn', aria: 'Page down',   bytes: PGDN_BARE,    shiftBytes: [27, 91, 54, 59, 50, 126] },
    { label: '@',    aria: 'At sign',     bytes: [64] },
    { label: '-',    aria: 'Hyphen',      bytes: [45] },
    { label: 'Home', aria: 'Home',        bytes: [27, 91, 72] },
    { label: 'End',  aria: 'End',         bytes: [27, 91, 70] },
];

function fireCb(cb, payload, label) {
    if (typeof cb !== 'function') return;
    try { cb(payload); } catch (err) {
        console.error(`[ZELLIJ-EXTRA-KEYS] ${label} threw:`, err);
    }
}

// The iframe host is a sibling of the extra-keys host inside the terminal
// column (same overlay-host idiom as the terminal bar's chip/toasts).
function overlayHost() {
    return currentContainerEl?.parentElement?.querySelector('.cli-agents-zellij-iframe-host')
        || currentContainerEl;
}

// Best-effort refocus of zellij-web's xterm after a send — the app-proxy is
// same-origin so contentWindow.term is reachable; anything short of that
// (detached iframe, zellij-web internals change) must silently no-op.
function refocusTerminal() {
    try {
        overlayHost()?.querySelector('iframe')?.contentWindow?.term?.focus();
    } catch { /* best-effort only */ }
}

// ── Send-failure feedback ────────────────────────────────────────────────
// Deliberate policy: EVERY failed send logs one console.warn (full trail for
// debugging) and fires onError, but visible toasts are debounced hard —
// arrow-mashing against a dead session would otherwise bury the terminal
// under a toast per keystroke. A toast appears only when failures REPEAT
// (≥2 within 10s) and at most once per 8s.

function showErrorToast(message) {
    const host = overlayHost();
    if (!host) return;
    if (errorToastTimer) { clearTimeout(errorToastTimer); errorToastTimer = null; }
    if (!errorToastEl || !errorToastEl.isConnected) {
        errorToastEl = document.createElement('div');
        errorToastEl.className = 'zellij-extra-keys-error-toast';
        // Failure feedback for a visual-only control — announce politely.
        errorToastEl.setAttribute('aria-live', 'polite');
        host.appendChild(errorToastEl);
    }
    errorToastEl.textContent = message;
    errorToastTimer = setTimeout(() => {
        errorToastTimer = null;
        if (errorToastEl?.parentNode) errorToastEl.parentNode.removeChild(errorToastEl);
        errorToastEl = null;
    }, ERROR_TOAST_TTL_MS);
}

function reportSendFailure(status, message) {
    console.warn(`[ZELLIJ-EXTRA-KEYS] send-key failed (${status}): ${message}`);
    fireCb(currentOpts.onError, { stage: 'send-key', status, message }, 'onError');
    const now = Date.now();
    failureTimes = failureTimes.filter((t) => now - t < FAILURE_REPEAT_WINDOW_MS);
    failureTimes.push(now);
    if (failureTimes.length >= 2 && now - lastErrorToastAt >= ERROR_TOAST_MIN_INTERVAL_MS) {
        lastErrorToastAt = now;
        showErrorToast(`⌨ keys aren't reaching the terminal — ${message}`);
    }
}

// ── Send pipeline ────────────────────────────────────────────────────────

function sendBytes(bytes) {
    // Session captured at TAP time (not dispatch time): the key you pressed
    // goes to the session you were looking at. Sends settle in ~ms, so a
    // mid-queue session switch is a non-issue (unlike large file uploads).
    const session = typeof currentOpts.getSessionName === 'function'
        ? (currentOpts.getSessionName() || null)
        : null;
    if (!session) return; // disabled buttons make this unreachable; belt-and-braces
    const serial = mountSerial;
    const op = currentOperator;
    sendChain = sendChain.then(() => fetch(
        `${SEND_KEY_URL}?op=${encodeURIComponent(op)}`,
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session, bytes }),
        },
    ).then((resp) => {
        if (serial !== mountSerial) return; // unmounted mid-flight — drop
        if (!resp.ok) reportSendFailure(resp.status, `HTTP ${resp.status}`);
        refocusTerminal();
    }).catch((err) => {
        if (serial !== mountSerial) return;
        reportSendFailure(0, err?.message || 'network error');
    })).catch(() => {
        // Defensive tail: the handlers above are written to never throw,
        // but if one ever does the rejection must not poison sendChain —
        // every subsequent tap would silently skip its send.
    });
}

/** Resolve + send a key's bytes, applying (and consuming) the Shift arm. */
function fireKey(spec) {
    if (!currentSessionName) return;
    const useShifted = shiftState !== 'off' && Array.isArray(spec.shiftBytes);
    const bytes = useShifted ? spec.shiftBytes : spec.bytes;
    // One-shot arm is consumed by ANY key — even one without a shifted
    // variant (Android fireKey parity: Pending clears on every key).
    if (shiftState === 'armed') setShiftState('off');
    sendBytes(bytes);
}

// ── Sticky Shift ─────────────────────────────────────────────────────────

function setShiftState(next) {
    shiftState = next;
    if (!shiftBtn) return;
    shiftBtn.dataset.shiftState = next;
    // armed and locked are both "engaged" to assistive tech; the visual
    // three-state detail rides on data-shift-state.
    shiftBtn.setAttribute('aria-pressed', next === 'off' ? 'false' : 'true');
}

function clearShiftHoldTimer() {
    if (shiftHoldTimer) { clearTimeout(shiftHoldTimer); shiftHoldTimer = null; }
}

// ── Button construction ──────────────────────────────────────────────────

function makeKeyBtn(label, aria) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'zellij-extra-keys-btn';
    btn.textContent = label;
    btn.title = aria || label;
    btn.setAttribute('aria-label', aria || label);
    btn.disabled = true; // enabled by setActiveSession
    // Focus-preservation rule (live-verified): preventDefault on pointerdown
    // keeps focus (and the mobile IME) on the terminal iframe; click still
    // fires. NEVER touchstart+preventDefault (kills the click on touch).
    btn.addEventListener('pointerdown', (ev) => ev.preventDefault());
    return btn;
}

function makeShiftBtn() {
    const btn = makeKeyBtn('Shift', 'Shift');
    btn.classList.add('zellij-extra-keys-shift');
    btn.title = 'Shift — tap: next key only · hold: lock';
    btn.dataset.shiftState = 'off';
    btn.setAttribute('aria-pressed', 'false');
    // Touch long-press must lock Shift, not open a context menu.
    btn.addEventListener('contextmenu', (ev) => ev.preventDefault());
    btn.addEventListener('pointerdown', () => {
        shiftLongPressFired = false;
        clearShiftHoldTimer();
        shiftHoldTimer = setTimeout(() => {
            shiftHoldTimer = null;
            shiftLongPressFired = true; // swallow the click that follows the hold
            setShiftState(shiftState === 'locked' ? 'off' : 'locked');
        }, SHIFT_HOLD_MS);
    });
    btn.addEventListener('pointerup', clearShiftHoldTimer);
    btn.addEventListener('pointercancel', clearShiftHoldTimer);
    btn.addEventListener('pointerleave', clearShiftHoldTimer);
    btn.addEventListener('click', () => {
        if (shiftLongPressFired) { shiftLongPressFired = false; return; }
        // Tap: off → armed; armed/locked → off (Android toggleSticky parity).
        setShiftState(shiftState === 'off' ? 'armed' : 'off');
    });
    return btn;
}

// ── Public API ───────────────────────────────────────────────────────────

export function mountExtraKeys(containerEl, options = {}) {
    if (!containerEl) {
        console.error('[ZELLIJ-EXTRA-KEYS] mountExtraKeys called without container element');
        return null;
    }
    // Validate BEFORE the auto-unmount below — a bad call must reject
    // without destroying an already-working bar.
    if (!options.operator || typeof options.operator !== 'string') {
        console.error('[ZELLIJ-EXTRA-KEYS] mountExtraKeys requires options.operator (non-empty string)');
        return null;
    }
    if (currentBarEl && currentContainerEl === containerEl) {
        console.warn('[ZELLIJ-EXTRA-KEYS] mountExtraKeys is idempotent — second call on same container ignored');
        return currentBarEl;
    }
    if (currentBarEl && currentContainerEl !== containerEl) {
        console.warn('[ZELLIJ-EXTRA-KEYS] mountExtraKeys called with new container before unmount — auto-cleaning prior mount');
        unmountExtraKeys();
    }

    mountSerial += 1;
    currentContainerEl = containerEl;
    currentOperator = options.operator;
    currentOpts = {
        getSessionName: options.getSessionName,
        onError: options.onError,
    };
    currentSessionName = null;
    shiftState = 'off';
    shiftLongPressFired = false;
    sendChain = Promise.resolve();
    failureTimes = [];
    lastErrorToastAt = 0;
    keyButtons = [];

    const bar = document.createElement('div');
    bar.className = 'zellij-extra-keys';
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', 'Terminal quick keys');
    // The bar mounts with no session (currentSessionName null, every key
    // disabled) — start with the disabled class so the dimmed styling
    // matches from the first paint; setActiveSession toggles it from here.
    bar.classList.add('zellij-extra-keys-disabled');

    // Esc — pinned OUTSIDE the scroll strip (see module header).
    const escBtn = makeKeyBtn('Esc', 'Escape');
    escBtn.classList.add('zellij-extra-keys-esc');
    escBtn.addEventListener('click', () => fireKey({ bytes: ESC_BYTES }));
    keyButtons.push(escBtn);

    const divider = document.createElement('div');
    divider.className = 'zellij-extra-keys-divider';
    divider.setAttribute('aria-hidden', 'true');

    const strip = document.createElement('div');
    strip.className = 'zellij-extra-keys-strip';

    for (const spec of STRIP_KEYS) {
        if (spec.shift) {
            shiftBtn = makeShiftBtn();
            keyButtons.push(shiftBtn);
            strip.appendChild(shiftBtn);
            continue;
        }
        const btn = makeKeyBtn(spec.label, spec.aria);
        btn.addEventListener('click', () => fireKey(spec));
        keyButtons.push(btn);
        strip.appendChild(btn);
    }

    bar.appendChild(escBtn);
    bar.appendChild(divider);
    bar.appendChild(strip);
    containerEl.appendChild(bar);

    currentBarEl = bar;
    return bar;
}

// Driven by the modal from the same session-change sites as the terminal
// bar: launch success, rail onSwitch, rail onDelete, and the iframe's
// onSessionChanged. Pass null when no session is loaded (disables the keys —
// a tap on a disabled key must produce ZERO network traffic).
export function setActiveSession(sessionName) {
    if (!currentBarEl) {
        console.warn('[ZELLIJ-EXTRA-KEYS] setActiveSession called before mountExtraKeys — ignored');
        return;
    }
    currentSessionName = (typeof sessionName === 'string' && sessionName) ? sessionName : null;
    const disabled = !currentSessionName;
    for (const btn of keyButtons) btn.disabled = disabled;
    // A sticky arm/lock can't outlive its session.
    if (disabled) {
        clearShiftHoldTimer();
        shiftLongPressFired = false;
        setShiftState('off');
    }
    currentBarEl.classList.toggle('zellij-extra-keys-disabled', disabled);
}

// Visibility is OWNED by the modal (default pointer:coarse, persisted via
// localStorage, toggled by the toolbar's ⌨ button) — we only apply it.
export function setExtraKeysVisible(visible) {
    if (!currentBarEl) return;
    currentBarEl.hidden = !visible;
}

export function unmountExtraKeys() {
    clearShiftHoldTimer();
    if (errorToastTimer) { clearTimeout(errorToastTimer); errorToastTimer = null; }
    if (errorToastEl?.parentNode) errorToastEl.parentNode.removeChild(errorToastEl);
    errorToastEl = null;
    if (currentBarEl?.parentNode) currentBarEl.parentNode.removeChild(currentBarEl);
    currentBarEl = null;
    currentContainerEl = null;
    currentOperator = null;
    currentOpts = {};
    currentSessionName = null;
    keyButtons = [];
    shiftBtn = null;
    shiftState = 'off';
    shiftLongPressFired = false;
    sendChain = Promise.resolve();
    failureTimes = [];
    lastErrorToastAt = 0;
    // Bump serial so in-flight sends drop their callbacks (orphan guard).
    mountSerial += 1;
}
