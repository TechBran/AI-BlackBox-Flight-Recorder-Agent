/**
 * cli-agents-modal.js
 * CLI Agents launcher modal — picks app folder + provider, opens terminal session.
 * Per docs/plans/2026-05-20-portal-tools-section-alignment.md Track 3.
 *
 * Brandon-locked decisions (2026-05-20):
 *  - Simple <pre> terminal (no xterm.js dependency). First cut. xterm.js is
 *    a future enhancement if the simple form feels primitive in practice.
 *  - In-modal terminal panel (no separate browser tab).
 *  - Provider radio: Claude / Gemini / Codex (backend supports all 3 per
 *    Orchestrator/routes/cli_agent_routes.py:47 SUPPORTED_PROVIDERS).
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

// Session-name field separator (mirrors Orchestrator/cli_agent/session_manager.py _FIELD_SEP).
const FIELD_SEP = '__';
const APPS_ROOT_SLUG = '_root';

let activeSocket = null;       // WebSocket | null
let activeSessionId = null;    // string | null  (for kill on disconnect)

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
    const termEl = document.getElementById('cliAgentsTerminal');
    if (setupEl) setupEl.style.display = 'none';
    if (termEl) termEl.style.display = '';
    const infoEl = document.getElementById('cliAgentsSessionInfo');
    if (infoEl) infoEl.textContent = `${provider} · ${appSlug || 'Apps root'} · ${operator}`;
    const outEl = document.getElementById('cliAgentsOutput');
    if (outEl) outEl.textContent = '';

    // Build the WS URL — mirrors Android CliAgentWebSocket.buildUrl().
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams({
        op: operator,
        provider: provider,
        app: appSlug,
        cols: '80',
        rows: '24',
    });
    const wsUrl = `${proto}//${location.host}/cli-agent/ws/${encodeURIComponent(sessionId)}?${params.toString()}`;

    appendOutput(`[connecting to ${provider}…]\n`);

    try {
        activeSocket = new WebSocket(wsUrl);
        activeSocket.binaryType = 'arraybuffer';
    } catch (err) {
        toastError(`WS connect failed: ${err.message}`);
        appendOutput(`\n[connect failed: ${err.message}]\n`);
        return;
    }

    activeSocket.addEventListener('open', () => {
        appendOutput('[connected]\n');
    });

    // Decode raw PTY bytes (binary frames) or session_info / error JSON (text frames).
    const textDecoder = new TextDecoder('utf-8', { fatal: false });
    activeSocket.addEventListener('message', (ev) => {
        if (typeof ev.data === 'string') {
            // Text frame — JSON control message (session_info, error, etc.)
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === 'session_info') {
                    appendOutput(`[session ${msg.state || ''}]\n`);
                } else if (msg.type === 'error') {
                    appendOutput(`\n[error: ${msg.code || ''} ${msg.message || ''}]\n`);
                } else {
                    appendOutput(`\n[${msg.type || 'msg'}: ${ev.data}]\n`);
                }
            } catch {
                appendOutput(ev.data);
            }
        } else {
            // Binary frame — raw PTY bytes. Decode UTF-8 + strip terminal escapes
            // so the <pre> stays human-readable.
            const text = textDecoder.decode(new Uint8Array(ev.data));
            appendOutput(stripAnsi(text));
        }
    });

    activeSocket.addEventListener('close', (ev) => {
        appendOutput(`\n[disconnected: code=${ev.code}${ev.reason ? ' ' + ev.reason : ''}]\n`);
        activeSocket = null;
    });

    activeSocket.addEventListener('error', () => {
        // The 'close' event will fire right after with the real code/reason.
        appendOutput('\n[ws error]\n');
    });
}

// Best-effort ANSI/escape stripper so the simple <pre> stays readable.
// Drops:
//   - CSI escapes:   ESC [ <params> <command-letter>
//   - OSC sequences: ESC ] … BEL or ESC \
//   - Single-char escapes: ESC <one of a few well-known cmds>
//   - Bare control chars except newline + tab
// Terminal output will be readable but not visually faithful (no colors, no
// cursor positioning). That's the trade-off per the "simple <pre>" decision.
function stripAnsi(s) {
    if (!s) return '';
    // Strip CSI: ESC [ ... letter
    s = s.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
    // Strip OSC: ESC ] ... (BEL | ESC \)
    s = s.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '');
    // Strip simple ESC <one char> (e.g. ESC =, ESC >, ESC c)
    s = s.replace(/\x1b[=>cM78()]/g, '');
    // Drop other bare ESC
    s = s.replace(/\x1b/g, '');
    // Drop bell + carriage-return (keep \n, \t)
    s = s.replace(/[\x07\r]/g, '');
    return s;
}

function appendOutput(text) {
    const outEl = document.getElementById('cliAgentsOutput');
    if (!outEl) return;
    outEl.textContent += text;
    outEl.scrollTop = outEl.scrollHeight;  // auto-scroll
}

// =============================================================================
// Input — submit form sends raw bytes (binary frame) to WS
// =============================================================================

function setupInputForm() {
    const form = document.getElementById('cliAgentsInputForm');
    if (!form) return;
    form.addEventListener('submit', (e) => {
        e.preventDefault();
        const input = document.getElementById('cliAgentsInputField');
        const text = input?.value ?? '';
        if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN) {
            toastError('Not connected');
            return;
        }
        // Append newline so the line is committed by the remote shell / agent.
        const payload = new TextEncoder().encode(text + '\n');
        try {
            activeSocket.send(payload);
            // Echo locally — the PTY may also echo, in which case the user sees
            // their input twice. Acceptable for first cut; real terminal would
            // honor the remote echo flag.
            appendOutput(`> ${text}\n`);
            if (input) input.value = '';
        } catch (err) {
            toastError(`Send failed: ${err.message}`);
        }
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
        const termEl = document.getElementById('cliAgentsTerminal');
        if (setupEl) setupEl.style.display = '';
        if (termEl) termEl.style.display = 'none';
    });
}

// =============================================================================
// Modal open/close wiring
// =============================================================================

function openModal() {
    const modal = document.getElementById('cliAgentsModal');
    if (modal) modal.classList.remove('hide');
    refreshAppList();
}

function closeModal() {
    const modal = document.getElementById('cliAgentsModal');
    if (modal) modal.classList.add('hide');
    // If a session is open, leave it alone — the orchestrator's tmux session
    // survives detach so the user can reattach by re-launching with the same
    // operator + provider + app. The WebSocket stays open in the background;
    // we close it explicitly so we don't leak frames into the closed modal.
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
    setupInputForm();
    setupDisconnect();
}
