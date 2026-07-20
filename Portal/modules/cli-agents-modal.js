/**
 * cli-agents-modal.js
 * CLI Agents launcher modal — picks app folder + provider, opens terminal session.
 * Per docs/plans/2026-05-20-portal-tools-section-alignment.md Track 3 (xterm.js fix).
 *
 * Brandon-locked decisions (2026-05-20, post hardware test):
 *  - xterm.js (standard VT100/xterm emulator) — simple <pre> couldn't render
 *    Claude Code's TUI (cursor escapes, box-drawing, alternate screen buffer).
 *  - Loaded via jsDelivr CDN as UMD scripts; globals window.Terminal +
 *    window.FitAddon consumed from this ES module.
 *  - In-modal terminal panel (no separate browser tab).
 *  - Provider radio: Claude / Gemini / Codex / Antigravity (backend supports
 *    all 4 per Orchestrator/routes/cli_agent_routes.py:47 SUPPORTED_PROVIDERS).
 *    Antigravity (agy binary) was added as the 4th provider in Track 3 of
 *    docs/plans/2026-05-22-antigravity-cli-integration.md. buildSessionName()
 *    is provider-agnostic — no allowlist client-side; backend validates.
 *
 * Backend contract (cli_agent_routes.py):
 *  - WS path:  /cli-agent/ws/{session_id}
 *  - Query:    op, provider, app, cols, rows
 *  - session_id MUST equal session_name(op, provider, app), which is
 *    "cli-agent-{op}__{provider}__{slug or _root}". Anything else gets 4003.
 *  - PTY output:   raw bytes via binary WebSocket frames (terminal escapes).
 *  - PTY input:    raw bytes via binary WebSocket frames.
 *  - Control msgs: JSON via text frames — {type:"resize"|"paste"|"kill", ...}.
 *  - First frame:  text JSON {"type":"session_info","state":"created"|"attaching"}.
 */

import { toast, toastError } from './core-utils.js';
import { getOperator } from './state-management.js';

// ── Zellij backend trio (Phase 3 T11+T11.5+T11.6) ────────────────────────
// The modal branches at openModal time: tmux backend keeps the existing
// xterm.js flow untouched; Zellij backend composes these three modules into
// a three-region UI (launcher row top, switcher rail left, iframe fills
// rest). Coexistence is intentional — flipping CLI_AGENT_BACKEND in .env is
// the only required change to switch backends. T12 (this file's branching).
import {
    mountIframe, loadSession, unloadSession,
    getCurrentSessionName, unmountIframe,
} from './cli-agents-zellij-iframe.js';
import {
    mountLauncher, unmountLauncher,
    setOperator as setLauncherOperator,
    setActiveSession as setLauncherActiveSession,
} from './cli-agents-zellij-launcher.js';
import {
    mountSwitcher, unmountSwitcher,
    markSessionActive, refresh as refreshSwitcher,
    setOperator as setSwitcherOperator,
} from './cli-agents-zellij-switcher.js';

// Session-name field separator (mirrors Orchestrator/cli_agent/session_manager.py _FIELD_SEP).
const FIELD_SEP = '__';
const APPS_ROOT_SLUG = '_root';

// Zellij-mode singleton state (only populated when backend is Zellij).
let zellijMode = false;
let zellijShellEl = null;
// Monotonic open-counter used to drop stale post-await work. Every openModal
// increments it; every closeModal also increments it. Any async branch that
// captured the value at entry must compare to confirm "still my open" before
// touching DOM/modules. Fixes the re-entrant race + close-during-detect race
// flagged in T12 code-review.
let openSerial = 0;

// Defaults-to-tmux on ANY error: never break the existing path because of a
// transient orchestrator hiccup or new-endpoint deploy lag.
async function detectBackend() {
    try {
        const op = getOperator();
        const resp = await fetch(
            `/cli-agent/zellij/backend-status?op=${encodeURIComponent(op)}`,
        );
        if (!resp.ok) return 'tmux';
        const body = await resp.json();
        return body?.effective_backend === 'zellij' ? 'zellij' : 'tmux';
    } catch (e) {
        console.warn('[CLI-AGENTS-MODAL] backend detection failed; defaulting to tmux:', e);
        return 'tmux';
    }
}

// ── OAuth URL banner ─────────────────────────────────────────────────────
// Backend extracts OAuth URLs from the PTY byte stream and pushes them as
// {type:"auth_url_detected"} sidechannel messages. We render a sticky banner
// inside the modal with a click handler that triggers window.open under a
// user gesture (popup blockers reject auto-triggered window.open). Avoids
// the copy-paste-from-terminal flow that Antigravity's long auth URLs broke
// when xterm.js line-wrapped them.
function showAuthUrlBanner(url) {
    if (!url) return;
    const modal = document.getElementById('cliAgentsModal');
    if (!modal) return;
    // Reuse existing banner if present (dedup repeat detections)
    let banner = modal.querySelector('.cli-agents-auth-banner');
    if (banner && banner.dataset.url === url) return;  // same URL, already shown
    if (!banner) {
        banner = document.createElement('div');
        banner.className = 'cli-agents-auth-banner';
        // Insert at top of the terminal pane (above the xterm container)
        const termWrap = document.getElementById('cliAgentsTerminalPane') || modal.querySelector('.modal-body');
        termWrap?.insertBefore(banner, termWrap.firstChild);
    }
    banner.dataset.url = url;
    banner.innerHTML = '';
    const label = document.createElement('span');
    label.className = 'cli-agents-auth-banner-label';
    label.textContent = '🔗 OAuth sign-in detected — ';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cli-agents-auth-banner-btn';
    btn.textContent = 'Open in browser';
    btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = 'Opening…';
        // PRIMARY PATH: backend /onboarding/open-url uses the hardened 3-step
        // chain (xdg-desktop-portal → gio launch firefox snap → direct firefox
        // subprocess) to spawn a browser on the BackBlackBox host's desktop.
        // Bypasses client-side popup blockers and works on machines where
        // xdg-open routing is misconfigured. Reuses the same endpoint the
        // onboarding wizard uses for external links (proven on MSO2).
        let ok = false;
        try {
            const res = await fetch('/onboarding/open-url', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            });
            if (res.ok) {
                const data = await res.json().catch(() => ({}));
                ok = data.ok !== false;  // treat missing ok=true as success
            }
        } catch (err) {
            console.warn('[CLI-AGENTS] /onboarding/open-url failed:', err);
        }
        // FALLBACK: window.open if backend couldn't open (e.g., headless
        // BlackBox install with no display, or endpoint unreachable). User
        // gesture is preserved — click is still in the synchronous portion
        // of this handler conceptually, but await may break that on some
        // browsers; fallback is best-effort.
        if (!ok) {
            const opened = window.open(url, '_blank', 'noopener,noreferrer');
            ok = !!(opened && !opened.closed);
        }
        if (ok) {
            banner.classList.add('cli-agents-auth-banner-opened');
            btn.textContent = '✓ Opened';
        } else {
            btn.disabled = false;
            btn.textContent = 'Open in browser';
            // Show the URL inline so user can copy it manually as a last resort
            const fallback = document.createElement('div');
            fallback.className = 'cli-agents-auth-banner-fallback';
            fallback.textContent = `Couldn't auto-open. Copy this URL into a browser: ${url}`;
            banner.appendChild(fallback);
        }
    });
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'cli-agents-auth-banner-dismiss';
    dismiss.textContent = '✕';
    dismiss.title = 'Dismiss';
    dismiss.addEventListener('click', () => banner.remove());
    banner.appendChild(label);
    banner.appendChild(btn);
    banner.appendChild(dismiss);
}

let activeSocket = null;       // WebSocket | null
let activeSessionId = null;    // string | null  (for kill on disconnect)

// xterm.js singleton — created lazily on first launch, reused on subsequent ones.
let terminal = null;
let fitAddon = null;
let resizeListenerAttached = false;

// =============================================================================
// App list refresh — fills #cliAgentsAppSelect from GET /agent/apps
// =============================================================================

async function refreshAppList() {
    const sel = document.getElementById('cliAgentsAppSelect');
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = '<option value="">-- Loading apps... --</option>';
    try {
        const res = await fetch('/agent/apps');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const apps = Array.isArray(data) ? data : (data.apps || []);
        sel.innerHTML = '';

        // Always offer the Apps/ root as the first option — mirrors Android's
        // AppFolderPicker "+ New app workspace" row which also passes "" as slug.
        const rootOpt = document.createElement('option');
        rootOpt.value = '';                    // "" → backend resolves to Apps/ root
        rootOpt.textContent = 'Apps/ root (no specific app)';
        sel.appendChild(rootOpt);

        apps.forEach(app => {
            const slug = slugFromDirectory(app.directory) || app.name;
            // Skip apps with no directory (defensive — shouldn't happen).
            if (!slug) return;
            const opt = document.createElement('option');
            opt.value = slug;
            const portStr = app.port ? ` (port ${app.port})` : '';
            opt.textContent = `${app.name}${portStr}`;
            sel.appendChild(opt);
        });
        // Restore previous selection if still present
        if (prev) {
            const match = Array.from(sel.options).find(o => o.value === prev);
            if (match) sel.value = prev;
        }
    } catch (err) {
        sel.innerHTML = '<option value="">-- Failed to load apps --</option>';
        console.error('[CLI-AGENTS] App list fetch failed:', err);
    }
}

// Extract app slug from a directory path. Mirrors Android's appSlugFor():
//   "/home/.../Apps/grocery-store" → "grocery-store"
function slugFromDirectory(dir) {
    if (!dir) return '';
    const trimmed = dir.replace(/\/+$/, '');
    if (!trimmed) return '';
    const idx = trimmed.lastIndexOf('/');
    return idx >= 0 ? trimmed.slice(idx + 1) : trimmed;
}

// Expose for the Tools click handler to refresh on modal open.
if (typeof window !== 'undefined') {
    window.refreshCLIAgentsAppList = refreshAppList;
}

// =============================================================================
// Session-name builder — must match session_manager.session_name() exactly
// =============================================================================

function buildSessionName(operator, provider, appSlug) {
    if (operator.includes(FIELD_SEP) || provider.includes(FIELD_SEP)) {
        throw new Error(`Operator/provider must not contain "${FIELD_SEP}"`);
    }
    const slug = appSlug || APPS_ROOT_SLUG;
    return `cli-agent-${operator}${FIELD_SEP}${provider}${FIELD_SEP}${slug}`;
}

// =============================================================================
// xterm.js lifecycle — lazy init, reused across launches
// =============================================================================

function ensureTerminal() {
    if (terminal) return terminal;
    const container = document.getElementById('cliAgentsTerminal');
    if (!container) return null;
    if (!window.Terminal || !window.FitAddon) {
        console.error('[CLI-AGENTS] xterm.js UMD globals not available — CDN load failed?');
        return null;
    }
    // FitAddon ships as window.FitAddon.FitAddon (UMD namespace wrapper)
    const FitAddonCtor = window.FitAddon.FitAddon || window.FitAddon;
    terminal = new window.Terminal({
        cursorBlink: true,
        fontFamily: 'ui-monospace, "SF Mono", Menlo, Monaco, "Courier New", monospace',
        fontSize: 13,
        theme: {
            background: '#0a0a0a',
            foreground: '#e0e0e0',
        },
        convertEol: false,  // PTY emits proper CRLF; let xterm.js handle it natively
        scrollback: 5000,
    });
    fitAddon = new FitAddonCtor();
    terminal.loadAddon(fitAddon);
    terminal.open(container);
    try { fitAddon.fit(); } catch (e) { /* container may not be visible yet */ }

    // Keystroke → WS binary frame. xterm.js calls onData for every keypress
    // (including arrow keys, ctrl-chords, escape sequences) — no Enter buffering.
    terminal.onData((data) => {
        if (activeSocket && activeSocket.readyState === WebSocket.OPEN) {
            try {
                activeSocket.send(new TextEncoder().encode(data));
            } catch (err) {
                console.warn('[CLI-AGENTS] send failed:', err);
            }
        }
    });

    // Refit on window resize, and let the backend know about the new dims.
    if (!resizeListenerAttached) {
        window.addEventListener('resize', sendResize);
        resizeListenerAttached = true;
    }

    return terminal;
}

function sendResize() {
    if (!terminal || !fitAddon) return;
    try { fitAddon.fit(); } catch { return; }
    if (activeSocket && activeSocket.readyState === WebSocket.OPEN) {
        try {
            activeSocket.send(JSON.stringify({
                type: 'resize',
                cols: terminal.cols,
                rows: terminal.rows,
            }));
        } catch (err) {
            console.warn('[CLI-AGENTS] resize send failed:', err);
        }
    }
}

// =============================================================================
// Launch — opens WS to /cli-agent/ws/<session_id>?op=X&provider=Y&app=Z
// =============================================================================

async function launchSession() {
    const appSlug = document.getElementById('cliAgentsAppSelect')?.value ?? '';
    const provider = document.querySelector('input[name="cliAgentsProvider"]:checked')?.value || 'claude';
    const operator = getOperator() || 'Brandon';

    let sessionId;
    try {
        sessionId = buildSessionName(operator, provider, appSlug);
    } catch (err) {
        toastError(err.message);
        return;
    }
    activeSessionId = sessionId;

    // Swap setup pane → terminal pane
    const setupEl = document.getElementById('cliAgentsSetup');
    const paneEl = document.getElementById('cliAgentsTerminalPane');
    if (setupEl) setupEl.style.display = 'none';
    if (paneEl) paneEl.style.display = '';
    const infoEl = document.getElementById('cliAgentsSessionInfo');
    if (infoEl) infoEl.textContent = `${provider} · ${appSlug || 'Apps root'} · ${operator}`;

    // Boot xterm.js now that the container is visible. Run fit() twice with
    // a small delay to handle modal animation lag.
    const term = ensureTerminal();
    if (term) {
        term.clear();                                  // wipe leftovers from prior launch
        try { fitAddon?.fit(); } catch {}
        setTimeout(() => { try { fitAddon?.fit(); sendResize(); } catch {} }, 50);
    } else {
        toastError('Terminal failed to initialize (xterm.js not loaded)');
        return;
    }

    // Read fitted size for the initial WS handshake. Falls back to 80x24
    // if fit() hasn't completed (e.g. container measured 0×0 mid-animation).
    const cols = term.cols && term.cols > 0 ? term.cols : 80;
    const rows = term.rows && term.rows > 0 ? term.rows : 24;

    // Build the WS URL — mirrors Android CliAgentWebSocket.buildUrl().
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams({
        op: operator,
        provider: provider,
        app: appSlug,
        cols: String(cols),
        rows: String(rows),
    });
    const wsUrl = `${proto}//${location.host}/cli-agent/ws/${encodeURIComponent(sessionId)}?${params.toString()}`;

    term.write(`\x1b[2m[connecting to ${provider}...]\x1b[0m\r\n`);

    try {
        activeSocket = new WebSocket(wsUrl);
        // MUST set binaryType before listeners so message events deliver
        // ArrayBuffer (not Blob) — avoids the async-decode dance.
        activeSocket.binaryType = 'arraybuffer';
    } catch (err) {
        toastError(`WS connect failed: ${err.message}`);
        term.write(`\r\n\x1b[31m[connect failed: ${err.message}]\x1b[0m\r\n`);
        return;
    }

    activeSocket.addEventListener('open', () => {
        term.write('\x1b[2m[connected]\x1b[0m\r\n');
        // Send an authoritative resize once the socket is open — the URL-param
        // dims are a first guess; this is the real one.
        sendResize();
    });

    activeSocket.addEventListener('message', (ev) => {
        if (ev.data instanceof ArrayBuffer) {
            // PTY bytes — raw write. xterm.js owns ANSI/cursor/box-drawing.
            terminal?.write(new Uint8Array(ev.data));
        } else if (typeof ev.data === 'string') {
            // JSON control event — session_info, error, auth_url_detected, etc.
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === 'session_info') {
                    terminal?.write(`\x1b[2m[session ${msg.state || ''}]\x1b[0m\r\n`);
                } else if (msg.type === 'error') {
                    terminal?.write(`\r\n\x1b[31m[error: ${msg.code || ''} ${msg.message || ''}]\x1b[0m\r\n`);
                } else if (msg.type === 'auth_url_detected') {
                    // Backend scraped an OAuth URL from PTY output.
                    // Surface a clickable banner — popup blockers reject
                    // unrequested window.open, so we need a user gesture
                    // (the button click below) to actually open the browser.
                    showAuthUrlBanner(msg.url);
                } else {
                    terminal?.write(`\r\n\x1b[2m[${msg.type || 'msg'}: ${ev.data}]\x1b[0m\r\n`);
                }
            } catch {
                terminal?.write(ev.data);
            }
        }
    });

    activeSocket.addEventListener('close', (ev) => {
        terminal?.write(`\r\n\x1b[33m[disconnected: code=${ev.code}${ev.reason ? ' ' + ev.reason : ''}]\x1b[0m\r\n`);
        activeSocket = null;
    });

    activeSocket.addEventListener('error', () => {
        // The 'close' event will fire right after with the real code/reason.
        terminal?.write('\r\n\x1b[31m[ws error]\x1b[0m\r\n');
    });
}

// =============================================================================
// Disconnect — closes WS, returns to setup pane
// =============================================================================

function setupDisconnect() {
    const btn = document.getElementById('cliAgentsDisconnect');
    if (!btn) return;
    btn.addEventListener('click', () => {
        if (activeSocket) {
            try { activeSocket.close(1000, 'user disconnect'); } catch {}
            activeSocket = null;
        }
        activeSessionId = null;
        const setupEl = document.getElementById('cliAgentsSetup');
        const paneEl = document.getElementById('cliAgentsTerminalPane');
        if (setupEl) setupEl.style.display = '';
        if (paneEl) paneEl.style.display = 'none';
        // Keep the xterm.js instance alive — clear() on next launch wipes it.
    });
}

// =============================================================================
// Modal open/close wiring
// =============================================================================

async function openModal() {
    const modal = document.getElementById('cliAgentsModal');
    if (modal) modal.classList.remove('hide');

    // Increment for this open; capture locally. If the user clicks Close, or
    // Open again, before detectBackend resolves, our captured value will be
    // stale and we'll bail before touching anything. Operator-change-while-
    // open is NOT forwarded into the launcher/switcher (modal-open re-mount
    // handles it naturally); documented as a known limitation.
    const mySerial = ++openSerial;
    const backend = await detectBackend();
    if (mySerial !== openSerial) return;  // stale; another open/close happened

    if (backend === 'zellij') {
        enterZellijMode();
    } else {
        enterTmuxMode();
        refreshAppList();
        applyPreselectProvider();
    }
}

// Build the three-region Zellij shell inside .cli-agents-body and compose
// the launcher + switcher + iframe modules with cross-wired callbacks.
function enterZellijMode() {
    zellijMode = true;
    // Hide the tmux-mode panes (do NOT modify HTML; just toggle display).
    const setup = document.getElementById('cliAgentsSetup');
    const term = document.getElementById('cliAgentsTerminalPane');
    if (setup) setup.style.display = 'none';
    if (term) term.style.display = 'none';

    // Idempotent: if a prior open already built the shell and close didn't
    // tear it down for some reason, blow away the trio's state FIRST (so
    // their singletons release refs to the old DOM children) THEN remove
    // the stale shell. Calling unmount* on never-mounted state is safe.
    unmountLauncher();
    unmountSwitcher();
    unmountIframe();
    if (zellijShellEl?.parentNode) zellijShellEl.parentNode.removeChild(zellijShellEl);
    zellijShellEl = null;

    const body = document.querySelector('#cliAgentsModal .cli-agents-body');
    if (!body) {
        console.error('[CLI-AGENTS-MODAL] cannot build Zellij shell: .cli-agents-body missing');
        return;
    }

    zellijShellEl = document.createElement('div');
    zellijShellEl.className = 'cli-agents-zellij-shell';
    zellijShellEl.id = 'cliAgentsZellijShell';
    const launcherHost = document.createElement('div');
    launcherHost.className = 'cli-agents-zellij-launcher-host';
    const mainRow = document.createElement('div');
    mainRow.className = 'cli-agents-zellij-main';
    const switcherHost = document.createElement('div');
    switcherHost.className = 'cli-agents-zellij-switcher-host';
    const iframeHost = document.createElement('div');
    iframeHost.className = 'cli-agents-zellij-iframe-host';
    mainRow.appendChild(switcherHost);
    mainRow.appendChild(iframeHost);
    zellijShellEl.appendChild(launcherHost);
    zellijShellEl.appendChild(mainRow);
    body.appendChild(zellijShellEl);

    // Defensive operator default — matches the tmux-path fallback elsewhere
    // in this file. mountLauncher would throw with a missing operator
    // otherwise, which would leave the modal in a half-mounted state.
    const op = getOperator() || 'Brandon';

    mountIframe(iframeHost, {
        onSessionError: ({ sessionName, reason }) => {
            toastError(`Terminal failed for ${sessionName}: ${reason || 'unknown'}`);
        },
        // Zellij's own session-manager can switch sessions from INSIDE the
        // terminal (the web client navigates the iframe itself). Mirror the
        // switcher's onSwitch wiring so the rail highlight + launcher gate
        // follow the truly-loaded session. Torn down with unmountIframe in
        // closeModal — no modal-side state to clean up.
        onSessionChanged: (sessionName) => {
            markSessionActive(sessionName);
            setLauncherActiveSession(sessionName);
        },
    });

    mountLauncher(launcherHost, {
        operator: op,
        onLaunched: ({ sessionName, sessionUrl }) => {
            loadSession({ sessionUrl, sessionName });
            markSessionActive(sessionName);
            setLauncherActiveSession(sessionName);
            refreshSwitcher();
        },
        onError: ({ provider, stage, status, message }) => {
            toastError(`${provider} ${stage} failed (${status}): ${message || ''}`);
        },
        onInjected: ({ binary, sessionName }) => {
            // Silent success is fine; surface only when something interesting
            // happens. Could add a debug toast here if Brandon wants feedback.
        },
    });

    mountSwitcher(switcherHost, {
        operator: op,
        onSwitch: ({ name, sessionUrl }) => {
            loadSession({ sessionUrl, sessionName: name });
            markSessionActive(name);
            setLauncherActiveSession(name);
        },
        onDelete: ({ name }) => {
            if (getCurrentSessionName() === name) {
                unloadSession();
                markSessionActive(null);
                setLauncherActiveSession(null);
            }
        },
        onError: ({ stage, status, message }) => {
            toastError(`Switcher ${stage} failed (${status}): ${message || ''}`);
        },
    });
}

// Restore the tmux-mode DOM in case a prior open was Zellij.
function enterTmuxMode() {
    zellijMode = false;
    const setup = document.getElementById('cliAgentsSetup');
    const term = document.getElementById('cliAgentsTerminalPane');
    if (setup) setup.style.display = '';
    if (term) term.style.display = '';
    if (zellijShellEl?.parentNode) {
        zellijShellEl.parentNode.removeChild(zellijShellEl);
        zellijShellEl = null;
    }
}

function applyPreselectProvider() {
    let preselect = null;
    try { preselect = sessionStorage.getItem('cliAgentsPreselectProvider'); } catch {}
    if (!preselect) return;
    try { sessionStorage.removeItem('cliAgentsPreselectProvider'); } catch {}
    const radio = document.querySelector(`input[name="cliAgentsProvider"][value="${preselect}"]`);
    if (radio) radio.checked = true;
}

// Auto-open on page-load if the wizard set the flag and navigated to the
// Portal root. The wizard's launch button drops a second flag specifically
// for the "modal should be open when you arrive" case (vs the Tools-button
// flow, which arrives at the Portal already loaded and dispatches the click).
function autoOpenFromWizard() {
    let shouldOpen = null;
    try { shouldOpen = sessionStorage.getItem('cliAgentsAutoOpen'); } catch {}
    if (shouldOpen !== '1') return;
    try { sessionStorage.removeItem('cliAgentsAutoOpen'); } catch {}
    openModal();
}

function closeModal() {
    const modal = document.getElementById('cliAgentsModal');
    if (modal) modal.classList.add('hide');

    // Invalidate any in-flight detectBackend from a prior open — the bumped
    // serial makes the awaited branch bail before touching DOM/modules.
    openSerial++;

    // Zellij teardown first (idempotent + safe pre-mount): stops the switcher
    // poll, removes the iframe + launcher DOM, clears module state. The
    // backend Zellij sessions PERSIST across modal close — re-open will
    // re-mount and the switcher's poll will surface them again.
    if (zellijMode) {
        unmountLauncher();
        unmountSwitcher();
        unmountIframe();
        if (zellijShellEl?.parentNode) {
            zellijShellEl.parentNode.removeChild(zellijShellEl);
        }
        zellijShellEl = null;
        zellijMode = false;
    }

    // If a tmux session is open, leave the tmux session alone on the backend
    // — it survives detach so the user can reattach by re-launching with the
    // same operator + provider + app. We close the WS explicitly so we don't
    // leak frames into the closed modal.
    if (activeSocket) {
        try { activeSocket.close(1000, 'modal closed'); } catch {}
        activeSocket = null;
    }
    activeSessionId = null;
}

// =============================================================================
// Init — call from app-init.js
// =============================================================================

export function initCLIAgentsModal() {
    document.getElementById('btnCLIAgents')?.addEventListener('click', openModal);
    document.getElementById('btnCloseCLIAgents')?.addEventListener('click', closeModal);
    document.getElementById('cliAgentsLaunch')?.addEventListener('click', launchSession);
    setupDisconnect();
    // If the onboarding wizard navigated us here with a "please open the
    // CLI Agents modal" flag, honor it. Runs after init so all DOM is wired.
    autoOpenFromWizard();
}
