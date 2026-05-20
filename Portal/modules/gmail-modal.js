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
 *   GET  /auth/gmail/authorize?operator={op}  — redirects to Google OAuth
 *   GET  /auth/gmail/callback                 — OAuth callback (server-side)
 *   GET  /gmail/status/{operator}             — { connected, email, operator }
 *   POST /gmail/disconnect/{operator}         — removes tokens
 */

import { toast, toastError } from './core-utils.js';
import { getOperator } from './state-management.js';

/**
 * Fetch + render Gmail connection status for the current operator.
 * Verified response shape (curl /gmail/status/Brandon, 2026-05-20):
 *   { "connected": true, "email": "bastrackstar@gmail.com", "operator": "Brandon" }
 */
async function refreshStatus() {
    const op = getOperator() || 'Brandon';
    const valueEl = document.getElementById('gmailStatusValue');
    const connectBtn = document.getElementById('gmailConnectBtn');
    const disconnectBtn = document.getElementById('gmailDisconnectBtn');
    if (!valueEl) return;

    valueEl.textContent = 'Checking…';
    valueEl.classList.remove('gmail-connected', 'gmail-disconnected');
    if (connectBtn) connectBtn.style.display = 'none';
    if (disconnectBtn) disconnectBtn.style.display = 'none';

    try {
        const res = await fetch(`/gmail/status/${encodeURIComponent(op)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        // Primary shape: { connected: bool, email: string, operator: string }
        // Fallbacks kept for forward-compat in case backend renames keys.
        const connected = !!(data.connected || data.is_connected || data.authenticated);
        const email = data.email || data.user_email || data.gmail_address || '';

        if (connected) {
            valueEl.textContent = email ? `Connected as ${email}` : 'Connected';
            valueEl.classList.add('gmail-connected');
            if (disconnectBtn) disconnectBtn.style.display = '';
        } else {
            valueEl.textContent = 'Not connected';
            valueEl.classList.add('gmail-disconnected');
            if (connectBtn) connectBtn.style.display = '';
        }
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
    const op = getOperator() || 'Brandon';
    const url = `/auth/gmail/authorize?operator=${encodeURIComponent(op)}`;
    window.open(url, '_blank', 'noopener,noreferrer');
    toast('OAuth opened in new tab. After granting access, click 🔄 Refresh.');
}

/**
 * POST /gmail/disconnect/{operator} — removes saved tokens.
 * Gated by a confirm() dialog to avoid accidental logout.
 */
async function disconnect() {
    const op = getOperator() || 'Brandon';
    if (!confirm(`Disconnect Gmail for operator "${op}"? You'll need to re-authorize via OAuth to reconnect.`)) {
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
        toast('Gmail disconnected');
        await refreshStatus();
    } catch (err) {
        toastError(`Disconnect failed: ${err.message}`);
    }
}

/**
 * Wire button handlers + lazy status fetch on each modal open.
 */
export function initGmailModal() {
    // Tools button opens modal
    document.getElementById('btnGmail')?.addEventListener('click', () => {
        const modal = document.getElementById('gmailModal');
        modal?.classList.remove('hide');
        // Refresh every open — user may have just completed an OAuth round-trip.
        refreshStatus();
    });

    // Modal internal buttons
    document.getElementById('gmailConnectBtn')?.addEventListener('click', startOAuthFlow);
    document.getElementById('gmailDisconnectBtn')?.addEventListener('click', disconnect);
    document.getElementById('gmailRefreshBtn')?.addEventListener('click', refreshStatus);
}
