/**
 * cli-agents-zellij-launcher.js
 * Terminal-first launcher with shortcut-injection dropdown.
 *
 * Per docs/plans/2026-05-24-zellij-cli-agent-rewrite.md T11.5 (pivoted T15).
 *
 * T15 redesign (operator-locked): drop the original 5-button per-provider row.
 * Replace with ONE primary "+ Terminal" button + ONE small "▾" shortcuts
 * trigger that opens a dropdown of provider entries (Claude / Gemini /
 * Codex / Antigravity). Each dropdown click LAUNCHES A NEW SESSION with the
 * provider as a KDL `pane command=` layout (T15 final — see injectShortcut).
 *
 * Mid-T15 rationale (HISTORICAL — write-chars injection was itself
 * superseded at T15 final because it broke for claude; kept for context):
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

// Auth note (Phase 5 master-token model, 2026-05-26): /cli-agent/zellij/launch
// returns token:null and the orchestrator's app-proxy injects the master
// session cookie on every upstream forward — the client NEVER holds or sends
// zellij tokens. The old POST /app-proxy/9097/command/login cookie bridge is
// gone (it 422'd against the tokenless response); launch goes straight to
// loadSession.
const LAUNCH_URL = '/cli-agent/zellij/launch';

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

    // token is deliberately NOT required or forwarded — the launch response
    // carries token:null under the master-token model (see auth note above).
    const { session_name, session_url } = launchData || {};
    if (typeof session_name !== 'string' || !session_name ||
        typeof session_url !== 'string' || !session_url) {
        return emitError('launch', launchStatus, 'launch response missing required fields (session_name, session_url)');
    }

    if (isStillMounted()) {
        fireCb(currentCallbacks.onLaunched, {
            provider: 'terminal',
            sessionName: session_name,
            sessionUrl: session_url,
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

        // No client-side auth step — the app-proxy injects the master
        // session cookie on every forward (see auth note above).
        // Fire onLaunched so the caller (T12 modal) swaps the iframe to
        // the new session URL. Identical shape to terminal launch.
        fireCb(currentCallbacks.onLaunched, {
            provider: shortcut.id,
            sessionName: data.session_name,
            sessionUrl: data.session_url,
            expiresAt: data.expires_at,
        }, 'onLaunched');

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
