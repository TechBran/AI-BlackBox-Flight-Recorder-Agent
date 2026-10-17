/**
 * gmail-modal.js
 * Gmail launcher modal — OAuth connect/disconnect + status display.
 * Per docs/plans/2026-05-20-portal-tools-section-alignment.md Track 5.
 *
 * Matches Android's minimal "Connect Gmail" scope (SettingsSheet.kt:391) —
 * no inbox UI. Inbox/search interaction is via the gmail_search / gmail_read /
 * gmail_reply / gmail_labels MCP tools, available to voice + chat agents.
 *
 * Backend endpoints (Orchestrator/routes/gmail_routes.py):
 *   GET  /auth/gmail/authorize?operator={op}  — redirects to Google OAuth (operator required)
 *   GET  /auth/gmail/callback                 — OAuth callback (server-side)
 *   GET  /gmail/status/{operator}             — live-probed status (shape below)
 *   POST /gmail/disconnect/{operator}         — removes tokens
 */

import { toast, toastError } from './core-utils.js';
import { getOperator } from './state-management.js';

/**
 * Root-relative sign-in URL for an operator. Same-origin, so it works on every
 * origin the Portal is served from (LAN, localhost, tailnet).
 */
export function authorizeUrl(op) {
    return `/auth/gmail/authorize?operator=${encodeURIComponent(op)}`;
}

/**
 * Map a /gmail/status response to what the modal shows. PURE (no DOM) so it
 * is unit-tested in gmail-modal.test.mjs.
 *
 * Response shape (gmail_routes.gmail_status):
 *   { connected, email, operator,
 *     state: "connected" | "needs_reauth" | "unverified" | "not_connected",
 *     needs_reauth, error, error_message, reauth_url }
 * `connected` is proven with a live Google call: a revoked/expired grant comes
 * back as connected:false + needs_reauth + reauth_url (it used to read
 * "Connected" for as long as a dead token file existed). A server without
 * `state` falls back to the boolean.
 */
export function gmailStatusView(data, op) {
    const d = data || {};
    const email = d.email || d.user_email || d.gmail_address || '';
    const state = d.state
        || (d.needs_reauth ? 'needs_reauth'
            : ((d.connected || d.is_connected || d.authenticated) ? 'connected' : 'not_connected'));

    if (state === 'needs_reauth') {
        // Only follow a same-origin sign-in path from the server; anything else
        // falls back to the URL we build ourselves.
        const reauth = typeof d.reauth_url === 'string' && d.reauth_url.startsWith('/auth/gmail/authorize?')
            ? d.reauth_url : authorizeUrl(op);
        const why = d.error ? ` (${d.error})` : '';
        return {
            text: `Reconnect needed — Google rejected the saved sign-in${why}`,
            cls: 'gmail-needs-reauth',
            showConnect: true,
            connectLabel: 'Reconnect Google Workspace',
            connectUrl: reauth,
            showDisconnect: true,
        };
    }
    if (state === 'connected' || state === 'unverified') {
        const base = email ? `Connected as ${email}` : 'Connected';
        return {
            text: state === 'unverified' ? `${base} (could not reach Google to verify)` : base,
            cls: 'gmail-connected',
            showConnect: false,
            connectLabel: 'Connect Google Workspace',
            connectUrl: authorizeUrl(op),
            showDisconnect: true,
        };
    }
    return {
        text: 'Not connected',
        cls: 'gmail-disconnected',
        showConnect: true,
        connectLabel: 'Connect Google Workspace',
        connectUrl: authorizeUrl(op),
        showDisconnect: false,
    };
}

// The sign-in URL the Connect/Reconnect button opens (set by refreshStatus).
let connectUrl = '';

/**
 * Fetch + render Gmail connection status for the current operator.
 */
async function refreshStatus() {
    const op = getOperator();
    const valueEl = document.getElementById('gmailStatusValue');
    const connectBtn = document.getElementById('gmailConnectBtn');
    const disconnectBtn = document.getElementById('gmailDisconnectBtn');
    if (!valueEl) return;

    valueEl.classList.remove('gmail-connected', 'gmail-disconnected', 'gmail-needs-reauth');
    if (connectBtn) connectBtn.style.display = 'none';
    if (disconnectBtn) disconnectBtn.style.display = 'none';

    if (!op) {
        // Tokens are per operator and the server requires one — never guess.
        valueEl.textContent = 'Select an operator first';
        valueEl.classList.add('gmail-disconnected');
        return;
    }

    valueEl.textContent = 'Checking…';
    connectUrl = authorizeUrl(op);

    try {
        const res = await fetch(`/gmail/status/${encodeURIComponent(op)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const view = gmailStatusView(await res.json(), op);

        valueEl.textContent = view.text;
        valueEl.classList.add(view.cls);
        connectUrl = view.connectUrl;
        if (connectBtn) {
            connectBtn.textContent = view.connectLabel;
            connectBtn.style.display = view.showConnect ? '' : 'none';
        }
        if (disconnectBtn) disconnectBtn.style.display = view.showDisconnect ? '' : 'none';
    } catch (err) {
        valueEl.textContent = `Error: ${err.message}`;
        // Show both buttons so user can attempt either action when status is unknown.
        if (connectBtn) connectBtn.style.display = '';
        if (disconnectBtn) disconnectBtn.style.display = '';
    }
}

/**
 * Open Google OAuth consent in a new tab.
 * OAuth typically blocks iframe embedding (X-Frame-Options: DENY on accounts.google.com),
 * so a new tab is the only reliable path. The server-side callback closes the loop;
 * user then returns to Portal and hits 🔄 Refresh to confirm the new status.
 */
function startOAuthFlow() {
    const op = getOperator();
    if (!op) {
        toastError('Select an operator first');
        return;
    }
    window.open(connectUrl || authorizeUrl(op), '_blank', 'noopener,noreferrer');
    toast('OAuth opened in new tab. After granting access, click 🔄 Refresh.');
}

/**
 * POST /gmail/disconnect/{operator} — removes saved tokens.
 * Gated by a confirm() dialog to avoid accidental logout.
 */
async function disconnect() {
    const op = getOperator();
    if (!op) {
        toastError('Select an operator first');
        return;
    }
    if (!confirm(`Disconnect Google Workspace for operator "${op}"? You'll need to re-authorize via OAuth to reconnect.`)) {
        return;
    }
    try {
        const res = await fetch(`/gmail/disconnect/${encodeURIComponent(op)}`, {
            method: 'POST',
        });
        if (!res.ok) {
            const err = await res.text();
            toastError(`Disconnect failed: ${err.substring(0, 100)}`);
            return;
        }
        toast('Google Workspace disconnected');
        await refreshStatus();
    } catch (err) {
        toastError(`Disconnect failed: ${err.message}`);
    }
}

/**
 * Wire button handlers + lazy status fetch on each modal open.
 */
export function initGmailModal() {
    const modal = document.getElementById('gmailModal');

    // Tools button opens modal
    document.getElementById('btnGmail')?.addEventListener('click', () => {
        modal?.classList.remove('hide');
        // Refresh every open — user may have just completed an OAuth round-trip.
        refreshStatus();
    });

    // Close button (the X in the modal head) — matches the wiring pattern used
    // by voice-agents-modal.js. No central .modal-close auto-binding exists;
    // each modal wires its own.
    modal?.querySelector('.modal-close')?.addEventListener('click', () => {
        modal.classList.add('hide');
    });

    // Modal internal buttons
    document.getElementById('gmailConnectBtn')?.addEventListener('click', startOAuthFlow);
    document.getElementById('gmailDisconnectBtn')?.addEventListener('click', disconnect);
    document.getElementById('gmailRefreshBtn')?.addEventListener('click', refreshStatus);
}
