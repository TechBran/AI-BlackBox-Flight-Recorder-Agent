/**
 * cli-agents-zellij-launcher.js
 * Terminal-first launcher with shortcut-injection dropdown.
 *
 * Per docs/plans/2026-05-24-zellij-cli-agent-rewrite.md T11.5 (pivoted T15).
 *
 * Brandon's T15 redesign: drop the original 5-button per-provider row.
 * Replace with ONE primary "+ Terminal" button + ONE small "▾" shortcuts
 * trigger that opens a dropdown of binary aliases (Claude / Gemini /
 * Codex / Antigravity). Each dropdown click injects "<binary>\n" into
 * the currently-active terminal session via POST /cli-agent/zellij/inject.
 *
 * Why this is better than per-provider auto-launch:
 *   - Zellij's `pane command=X` in a layout file only spawns the binary
 *     when a client renders the pane. Our launch_session detaches its
 *     transient client after ~2s — the binary never actually spawns.
 *     T15 smoke confirmed: empty session backend, no children, browser
 *     attach shows default bash.
 *   - Injecting "claude\n" into a live bash pane sidesteps the entire
 *     "pane command needs a client" problem. Bash receives "claude" via
 *     its stdin, types it as if the user did, hits Enter, runs claude.
 *   - User can manually re-launch the binary at will (Ctrl+C → up → Enter).
 *
 * Exports:
 *   - mountLauncher(containerEl, options)
 *   - setOperator(op)
 *   - setActiveSession(sessionName | null)  ← drives shortcut-dropdown gate
 *   - unmountLauncher()
 *
 * Out of scope:
 *   - Iframe lifecycle (cli-agents-zellij-iframe.js).
 *   - Switcher rail (cli-agents-zellij-switcher.js).
 *   - Modal chrome wiring (cli-agents-modal.js).
 *   - CSS (Portal/styles/features/_cli_agents_modal.css).
 */

// Module-level singletons. One launcher per Portal page.
let currentContainerEl = null;
let currentRowEl = null;
let currentTerminalBtn = null;
let currentShortcutsBtn = null;
let currentDropdownEl = null;
let currentOperator = null;
let currentActiveSession = null;
let currentCallbacks = {};
let launchInFlight = false;
let dropdownOpen = false;
let documentClickHandler = null;

// Shortcut dropdown items: label → binary alias typed into the terminal.
// Antigravity's binary is `agy` (per the CLI Agent install convention).
const SHORTCUTS = [
    { id: 'claude',      label: 'Claude',      binary: 'claude' },
    { id: 'gemini',      label: 'Gemini',      binary: 'gemini' },
    { id: 'codex',       label: 'Codex',       binary: 'codex' },
    { id: 'antigravity', label: 'Antigravity', binary: 'agy' },
];

const LAUNCH_URL = '/cli-agent/zellij/launch';
const LOGIN_URL = '/app-proxy/9097/command/login';
const INJECT_URL = '/cli-agent/zellij/inject';
const SPAWN_URL  = '/cli-agent/zellij/spawn';

function fireCb(cb, payload, label) {
    if (typeof cb !== 'function') return;
    try { cb(payload); } catch (err) {
        console.error(`[ZELLIJ-LAUNCHER] ${label} threw:`, err);
    }
}

function refreshShortcutsState() {
    if (!currentShortcutsBtn) return;
    // Dropdown is meaningful any time there's a live session to inject into.
    // Deliberately NOT gated on launchInFlight: the caller's onLaunched fires
    // setActiveSession() BEFORE finishFlight() clears launchInFlight, which
    // would leave the button disabled-until-next-event if we also gated on
    // the in-flight flag. The inject endpoint is independent of /launch
    // anyway — they hit different code paths server-side.
    const canInject = !!currentActiveSession;
    currentShortcutsBtn.disabled = !canInject;
    currentShortcutsBtn.title = canInject
        ? 'Inject a CLI agent alias into the current terminal'
        : 'Launch a terminal first';
    if (!canInject && dropdownOpen) closeDropdown();
}

function setTerminalDisabled(disabled) {
    if (currentTerminalBtn) currentTerminalBtn.disabled = disabled;
}

async function launchTerminal() {
    if (launchInFlight) {
        console.warn('[ZELLIJ-LAUNCHER] terminal launch already in flight — ignoring click');
        return;
    }
    if (!currentOperator) {
        console.error('[ZELLIJ-LAUNCHER] launchTerminal called without operator set');
        return;
    }

    launchInFlight = true;
    setTerminalDisabled(true);
    refreshShortcutsState();
    fireCb(currentCallbacks.onLaunching, { provider: 'terminal' }, 'onLaunching');

    // Row-ref snapshot — unmount-during-flight drops callbacks silently.
    const launchRowEl = currentRowEl;
    const isStillMounted = () => currentRowEl === launchRowEl;

    const finishFlight = () => {
        if (isStillMounted()) {
            setTerminalDisabled(false);
            refreshShortcutsState();
        }
        launchInFlight = false;
    };

    const emitError = (stage, status, message) => {
        if (isStillMounted()) {
            fireCb(currentCallbacks.onError,
                { provider: 'terminal', stage, status, message }, 'onError');
        }
        finishFlight();
    };

    let launchData;
    let launchStatus = 0;
    try {
        const resp = await fetch(
            `${LAUNCH_URL}?op=${encodeURIComponent(currentOperator)}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider: 'terminal' }),
            },
        );
        launchStatus = resp.status;
        if (!resp.ok) {
            const message = await resp.text().catch(() => '');
            return emitError('launch', resp.status, message);
        }
        try {
            launchData = await resp.json();
        } catch (e) {
            return emitError('launch', resp.status, `malformed launch response: ${e.message}`);
        }
    } catch (err) {
        return emitError('launch', 0, String(err));
    }

    const { session_name, session_url, token } = launchData || {};
    if (typeof session_name !== 'string' || !session_name ||
        typeof session_url !== 'string' || !session_url ||
        typeof token !== 'string' || !token) {
        return emitError('launch', launchStatus, 'launch response missing required fields (session_name, session_url, token)');
    }

    // Cookie bridge — Zellij auth on iframe load.
    try {
        const login = await fetch(LOGIN_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ auth_token: token, remember_me: false }),
        });
        if (!login.ok) {
            const message = await login.text().catch(() => '');
            return emitError('login', login.status, message);
        }
        let loginBody;
        try {
            loginBody = await login.json();
        } catch (e) {
            return emitError('login', login.status, `malformed login response: ${e.message}`);
        }
        if (loginBody?.success !== true) {
            return emitError('login', login.status, loginBody?.message || 'login refused');
        }
    } catch (err) {
        return emitError('login', 0, String(err));
    }

    if (isStillMounted()) {
        fireCb(currentCallbacks.onLaunched, {
            provider: 'terminal',
            sessionName: session_name,
            sessionUrl: session_url,
            token,
            expiresAt: launchData.expires_at,
        }, 'onLaunched');
    }
    finishFlight();
}

async function injectShortcut(shortcut) {
    closeDropdown();
    if (!currentActiveSession) {
        console.warn('[ZELLIJ-LAUNCHER] injectShortcut called with no active session');
        return;
    }
    // Use /zellij/spawn (new-tab) rather than /zellij/inject (write-chars).
    // T15 surfaced empirically that typing "claude\n" into a bash pane
    // doesn't actually run claude (bash never executes it — silent), while
    // `zellij action new-tab -- claude` works. To keep all four shortcuts
    // consistent, all of them spawn-in-new-tab.
    try {
        const resp = await fetch(
            `${SPAWN_URL}?op=${encodeURIComponent(currentOperator)}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    session_name: currentActiveSession,
                    binary: shortcut.binary,
                }),
            },
        );
        if (!resp.ok) {
            const message = await resp.text().catch(() => '');
            fireCb(currentCallbacks.onError, {
                provider: shortcut.id,
                stage: 'spawn',
                status: resp.status,
                message,
            }, 'onError');
            return;
        }
        fireCb(currentCallbacks.onInjected, {
            shortcut: shortcut.id,
            binary: shortcut.binary,
            sessionName: currentActiveSession,
        }, 'onInjected');
    } catch (err) {
        fireCb(currentCallbacks.onError, {
            provider: shortcut.id,
            stage: 'spawn',
            status: 0,
            message: String(err),
        }, 'onError');
    }
}

function openDropdown() {
    if (!currentRowEl || dropdownOpen) return;
    dropdownOpen = true;
    currentShortcutsBtn?.setAttribute('aria-expanded', 'true');

    currentDropdownEl = document.createElement('div');
    currentDropdownEl.className = 'zellij-launcher-shortcuts-menu';
    currentDropdownEl.setAttribute('role', 'menu');
    for (const s of SHORTCUTS) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'zellij-launcher-shortcut-item';
        item.dataset.provider = s.id;
        item.setAttribute('role', 'menuitem');
        item.textContent = s.label;
        item.addEventListener('click', () => injectShortcut(s));
        currentDropdownEl.appendChild(item);
    }
    currentRowEl.appendChild(currentDropdownEl);

    // Click-away to close. Listener attached on document (capture phase) and
    // cleaned up on closeDropdown/unmount so we don't leak handlers across
    // modal re-opens.
    documentClickHandler = (ev) => {
        if (!currentDropdownEl) return;
        if (currentDropdownEl.contains(ev.target)) return;
        if (currentShortcutsBtn?.contains(ev.target)) return;
        closeDropdown();
    };
    setTimeout(() => document.addEventListener('click', documentClickHandler, true), 0);
}

function closeDropdown() {
    if (!dropdownOpen) return;
    dropdownOpen = false;
    currentShortcutsBtn?.setAttribute('aria-expanded', 'false');
    if (currentDropdownEl?.parentNode) {
        currentDropdownEl.parentNode.removeChild(currentDropdownEl);
    }
    currentDropdownEl = null;
    if (documentClickHandler) {
        document.removeEventListener('click', documentClickHandler, true);
        documentClickHandler = null;
    }
}

function toggleDropdown() {
    if (dropdownOpen) closeDropdown(); else openDropdown();
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
    currentActiveSession = options.activeSession || null;
    currentCallbacks = {
        onLaunching: options.onLaunching,
        onLaunched: options.onLaunched,
        onError: options.onError,
        onInjected: options.onInjected,
    };

    const row = document.createElement('div');
    row.className = 'zellij-launcher-row';

    const terminalBtn = document.createElement('button');
    terminalBtn.type = 'button';
    terminalBtn.className = 'zellij-launcher-btn zellij-launcher-btn-terminal';
    terminalBtn.dataset.provider = 'terminal';
    terminalBtn.textContent = '+ Terminal';
    terminalBtn.addEventListener('click', launchTerminal);

    const shortcutsBtn = document.createElement('button');
    shortcutsBtn.type = 'button';
    shortcutsBtn.className = 'zellij-launcher-shortcuts-trigger';
    shortcutsBtn.setAttribute('aria-haspopup', 'menu');
    shortcutsBtn.setAttribute('aria-expanded', 'false');
    shortcutsBtn.textContent = 'Shortcuts ▾';
    shortcutsBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        toggleDropdown();
    });

    row.appendChild(terminalBtn);
    row.appendChild(shortcutsBtn);
    containerEl.appendChild(row);

    currentRowEl = row;
    currentTerminalBtn = terminalBtn;
    currentShortcutsBtn = shortcutsBtn;
    refreshShortcutsState();
    return row;
}

export function setOperator(op) {
    if (!currentRowEl) {
        console.warn('[ZELLIJ-LAUNCHER] setOperator called before mountLauncher — ignored');
        return;
    }
    if (typeof op !== 'string' || !op) {
        console.warn('[ZELLIJ-LAUNCHER] setOperator requires a non-empty string; got:', op);
        return;
    }
    currentOperator = op;
}

// Driven by the caller (T12 modal-branch) whenever the user-visible active
// session changes — both after onLaunched fires AND after the switcher rail
// reports an onSwitch / onDelete. Pass null to indicate no active session
// (which disables the shortcuts dropdown).
export function setActiveSession(sessionName) {
    if (!currentRowEl) {
        console.warn('[ZELLIJ-LAUNCHER] setActiveSession called before mountLauncher — ignored');
        return;
    }
    currentActiveSession = (typeof sessionName === 'string' && sessionName) ? sessionName : null;
    refreshShortcutsState();
}

export function unmountLauncher() {
    closeDropdown();
    if (currentRowEl?.parentNode) {
        currentRowEl.parentNode.removeChild(currentRowEl);
    }
    currentRowEl = null;
    currentContainerEl = null;
    currentTerminalBtn = null;
    currentShortcutsBtn = null;
    currentOperator = null;
    currentActiveSession = null;
    currentCallbacks = {};
    // Don't reset launchInFlight — pending fetch may still resolve; its
    // isStillMounted() check (row-ref snapshot) drops the callbacks.
}
