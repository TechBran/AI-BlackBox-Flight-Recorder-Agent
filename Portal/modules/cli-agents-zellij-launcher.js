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
    // New strategy (T15 final): each shortcut click LAUNCHES A NEW SESSION
    // with the provider as a KDL `pane command=` layout. When the iframe
    // attaches to the new session, Zellij spawns the binary natively. No
    // `write-chars`-into-bash (broken for claude) and no `new-pane -i`
    // focus-per-client tangle. Trade-off: each click creates a fresh
    // session that the user navigates between via the switcher rail.
    try {
        const launch = await fetch(
            `${LAUNCH_URL}?op=${encodeURIComponent(currentOperator)}`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider: shortcut.id }),
            },
        );
        if (!launch.ok) {
            const message = await launch.text().catch(() => '');
            fireCb(currentCallbacks.onError, {
                provider: shortcut.id,
                stage: 'launch',
                status: launch.status,
                message,
            }, 'onError');
            return;
        }
        const data = await launch.json();

        // Cookie-bridge auth — Zellij sets the session_token cookie on our
        // origin so the iframe load includes it automatically.
        const login = await fetch(LOGIN_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ auth_token: data.token, remember_me: false }),
        });
        if (!login.ok) {
            const message = await login.text().catch(() => '');
            fireCb(currentCallbacks.onError, {
                provider: shortcut.id,
                stage: 'login',
                status: login.status,
                message,
            }, 'onError');
            return;
        }

        // Fire onLaunched so the caller (T12 modal) swaps the iframe to
        // the new session URL. Identical shape to terminal launch.
        fireCb(currentCallbacks.onLaunched, {
            provider: shortcut.id,
            sessionName: data.session_name,
            sessionUrl: data.session_url,
            token: data.token,
            expiresAt: data.expires_at,
        }, 'onLaunched');

        // T15 critical workaround: Claude Code's startup sends a DA1
        // (`ESC[c`) terminal query and BLOCKS until a reply arrives. Zellij
        // 0.44.3's web client has a known regression (PR #5156 fixes it on
        // main) where `ForwardQueryToHost` messages — the path that
        // forwards DA1 to xterm.js for a reply — are dropped. xterm.js
        // never sees the query, never responds, claude waits forever.
        // Workaround per Anthropic claude-code issue #62220: inject the
        // DA1 reply (`ESC[?1;2c` = VT100 with advanced video) into the
        // pane ~750ms after launch (giving the iframe + claude time to
        // start). Fire 3x spaced 500ms apart in case the first arrives
        // before claude is ready to read. Other binaries that don't use
        // the DA1 sentinel are unaffected.
        if (shortcut.id === 'claude') {
            const da1Reply = '\x1b[?1;2c';
            for (const delayMs of [750, 1250, 1750]) {
                setTimeout(() => {
                    fetch(`${INJECT_URL}?op=${encodeURIComponent(currentOperator)}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            session_name: data.session_name,
                            text: da1Reply,
                        }),
                    }).catch(() => {});
                }, delayMs);
            }
        }
    } catch (err) {
        fireCb(currentCallbacks.onError, {
            provider: shortcut.id,
            stage: 'launch',
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
