/**
 * cli-agents-zellij-launcher.js
 * Provider button row + launch-flow execution for the Zellij CLI Agent modal.
 *
 * Per docs/plans/2026-05-24-zellij-cli-agent-rewrite.md T11.5.
 *
 * Scope:
 *   - Renders 5 provider buttons (Claude / Gemini / Codex / Antigravity /
 *     + Terminal) inside a caller-supplied container.
 *   - On click: POSTs /cli-agent/zellij/launch?op={op} to mint a session,
 *     then POSTs /app-proxy/9097/command/login to set the same-origin
 *     session_token cookie that the iframe load needs (Zellij requires the
 *     cookie on the iframe request itself — we cannot inject Authorization
 *     headers on an iframe src navigation, so we bridge via cookie).
 *   - Hands the session URL/name back to the caller via onLaunched callback;
 *     does NOT touch the iframe (cli-agents-zellij-iframe.js owns that).
 *
 * Out of scope:
 *   - Iframe lifecycle (T11b / cli-agents-zellij-iframe.js).
 *   - Switcher rail (T11.6).
 *   - Modal chrome wiring (T12).
 *   - CSS (T13 owns Portal/styles/features/_cli_agents_modal.css).
 *
 * Provider list:
 *   The first 4 (claude/gemini/codex/antigravity) drive real CLI binaries
 *   inside the Zellij session. "terminal" is the AC10 BlackBox Terminal mode
 *   — backend launches Zellij with no startup command, dropping the user
 *   at a bare bash prompt for arbitrary shell work.
 */

// Module-level singletons. One launcher per Portal page is the design —
// the caller (T12 modal) instantiates once when the modal opens, swaps the
// container reference if the modal re-mounts.
let currentContainerEl = null;
let currentRowEl = null;
let currentOperator = null;
let currentCallbacks = {};
// Single in-flight slot, not a Set: only one provider can be launching at a
// time because all 5 buttons disable on click. The slot exists as defense in
// depth against rapid double-clicks slipping past the disabled state.
let inFlightProvider = null;

const PROVIDERS = [
    { id: 'claude', label: 'Claude', extraClass: null },
    { id: 'gemini', label: 'Gemini', extraClass: null },
    { id: 'codex', label: 'Codex', extraClass: null },
    { id: 'antigravity', label: 'Antigravity', extraClass: null },
    { id: 'terminal', label: '+ Terminal', extraClass: 'zellij-launcher-btn-terminal' },
];

function fireCb(cb, payload, label) {
    if (typeof cb !== 'function') return;
    try { cb(payload); } catch (err) { console.error(`[ZELLIJ-LAUNCHER] ${label} threw:`, err); }
}

function setButtonsDisabled(disabled) {
    if (!currentRowEl) return;
    const buttons = currentRowEl.querySelectorAll('button.zellij-launcher-btn');
    buttons.forEach((b) => { b.disabled = disabled; });
}

async function launchProvider(provider) {
    if (inFlightProvider) {
        console.warn(`[ZELLIJ-LAUNCHER] launch already in flight for "${inFlightProvider}" — ignoring click on "${provider}"`);
        return;
    }
    if (!currentOperator) {
        console.error('[ZELLIJ-LAUNCHER] launchProvider called without operator set');
        return;
    }

    inFlightProvider = provider;
    setButtonsDisabled(true);
    fireCb(currentCallbacks.onLaunching, { provider }, 'onLaunching');

    // Capture the row reference at launch time. If it changes (unmount, or
    // remount onto a different container) before our fetches resolve, we
    // treat the launch as orphaned and drop callbacks silently — the caller
    // who wired onLaunched/onError is gone.
    const launchRowEl = currentRowEl;
    const isStillMounted = () => currentRowEl === launchRowEl;

    const finishFlight = () => {
        if (isStillMounted()) setButtonsDisabled(false);
        if (inFlightProvider === provider) inFlightProvider = null;
    };

    const emitError = (stage, status, message) => {
        if (isStillMounted()) {
            fireCb(currentCallbacks.onError, { provider, stage, status, message }, 'onError');
        }
        finishFlight();
    };

    let launchData;
    try {
        const resp = await fetch(
            `/cli-agent/zellij/launch?op=${encodeURIComponent(currentOperator)}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider }),
            },
        );
        if (!resp.ok) {
            const message = await resp.text().catch(() => '');
            return emitError('launch', resp.status, message);
        }
        launchData = await resp.json();
    } catch (err) {
        return emitError('launch', 0, String(err));
    }

    // Cookie bridge: POST /command/login with credentials:"include" so the
    // Set-Cookie session_token from upstream Zellij lands on our origin.
    // The iframe's subsequent navigation auto-includes the cookie (same
    // origin, Path=/, SameSite=Strict permits same-site requests).
    try {
        const login = await fetch('/app-proxy/9097/command/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ auth_token: launchData.token, remember_me: false }),
        });
        if (!login.ok) {
            const message = await login.text().catch(() => '');
            return emitError('login', login.status, message);
        }
    } catch (err) {
        return emitError('login', 0, String(err));
    }

    // session_url is path-shape (e.g. /app-proxy/9097/Brandon__terminal?token=UUID)
    // — Zellij's JS reads the session name from location.pathname.split('/').pop(),
    // so the URL MUST include the pre-minted name in its path. We pass it
    // through untouched.
    if (isStillMounted()) {
        fireCb(
            currentCallbacks.onLaunched,
            {
                provider,
                sessionName: launchData.session_name,
                sessionUrl: launchData.session_url,
                token: launchData.token,
                expiresAt: launchData.expires_at,
            },
            'onLaunched',
        );
    }
    finishFlight();
}

export function mountLauncher(containerEl, options = {}) {
    if (!containerEl) {
        console.error('[ZELLIJ-LAUNCHER] mountLauncher called without container element');
        return null;
    }
    if (currentRowEl && currentContainerEl === containerEl) {
        console.warn('[ZELLIJ-LAUNCHER] mountLauncher is idempotent — second call on same container ignored');
        return currentRowEl;
    }
    if (currentRowEl && currentContainerEl !== containerEl) {
        console.warn('[ZELLIJ-LAUNCHER] mountLauncher called with new container before unmount — auto-cleaning prior mount');
        unmountLauncher();
    }
    if (!options.operator) {
        console.error('[ZELLIJ-LAUNCHER] mountLauncher requires options.operator');
        return null;
    }

    currentContainerEl = containerEl;
    currentOperator = options.operator;
    currentCallbacks = {
        onLaunching: options.onLaunching,
        onLaunched: options.onLaunched,
        onError: options.onError,
    };

    const row = document.createElement('div');
    row.className = 'zellij-launcher-row';

    for (const p of PROVIDERS) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = p.extraClass
            ? `zellij-launcher-btn ${p.extraClass}`
            : 'zellij-launcher-btn';
        btn.dataset.provider = p.id;
        btn.textContent = p.label;
        btn.addEventListener('click', () => launchProvider(p.id));
        row.appendChild(btn);
    }

    containerEl.appendChild(row);
    currentRowEl = row;
    return row;
}

export function setOperator(op) {
    if (!currentRowEl) {
        console.warn('[ZELLIJ-LAUNCHER] setOperator called before mountLauncher — ignored');
        return;
    }
    currentOperator = op;
}

export function unmountLauncher() {
    if (currentRowEl && currentRowEl.parentNode) {
        currentRowEl.parentNode.removeChild(currentRowEl);
    }
    currentRowEl = null;
    currentContainerEl = null;
    currentOperator = null;
    currentCallbacks = {};
    // Deliberately do NOT clear inFlightProvider — a pending fetch may still
    // resolve. Its isStillMounted() check uses a row-ref snapshot taken at
    // launch time; since we just nulled currentRowEl, it returns false and
    // the orphan launch drops its callbacks + DOM touches silently.
}
