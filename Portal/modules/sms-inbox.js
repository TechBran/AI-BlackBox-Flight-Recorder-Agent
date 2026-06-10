/**
 * sms-inbox.js
 * SMS Inbox — thread list, conversation view, compose, unread badges
 * Works in both web (two-panel) and Android/mobile (full-screen) contexts
 */
import { $, toast, toastSuccess, toastError } from './core-utils.js';
import { getOperator } from './state-management.js';

let _pollTimer = null;
let _currentPhone = null;
let _isOpen = false;
// Our sendable lines (enabled gateways' enabled ports with a phone number)
let _lines = [];
// Selected "from" line for outbound SMS; '' = use backend default gateway
let _selectedFromNumber = '';

// ============================================================================
// Helpers
// ============================================================================

function timeAgo(isoTimestamp) {
    if (!isoTimestamp) return '';
    const diff = (Date.now() - new Date(isoTimestamp).getTime()) / 1000;
    if (diff < 60) return 'now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
    return new Date(isoTimestamp).toLocaleDateString();
}

function formatTime(isoTimestamp) {
    if (!isoTimestamp) return '';
    const d = new Date(isoTimestamp);
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function dateLabel(isoTimestamp) {
    if (!isoTimestamp) return '';
    const d = new Date(isoTimestamp);
    const now = new Date();
    const diffDays = Math.floor((now - d) / 86400000);
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return d.toLocaleDateString([], { weekday: 'long' });
    return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
}

function truncate(str, len = 38) {
    if (!str) return '';
    return str.length > len ? str.slice(0, len) + '\u2026' : str;
}

function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

const SEND_ICON = `<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>`;

// ============================================================================
// API calls
// ============================================================================

async function fetchThreads() {
    const op = getOperator();
    const res = await fetch(`/sms/threads?operator=${encodeURIComponent(op)}`);
    if (!res.ok) throw new Error(`Failed to fetch threads: ${res.status}`);
    const data = await res.json();
    return data.threads || [];
}

async function fetchMessages(phone) {
    const op = getOperator();
    const res = await fetch(`/sms/messages?operator=${encodeURIComponent(op)}&phone=${encodeURIComponent(phone)}&limit=100`);
    if (!res.ok) throw new Error(`Failed to fetch messages: ${res.status}`);
    const data = await res.json();
    return data.messages || [];
}

async function sendMessage(to, message) {
    const op = getOperator();
    const body = { operator: op, to, message };
    // Only include from_number when a specific line is selected (omit → backend default)
    if (_selectedFromNumber) body.from_number = _selectedFromNumber;
    const res = await fetch('/sms/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
        throw new Error(data.error || data.detail || 'Send failed');
    }
    return data;
}

// Fetch our sendable lines: enabled gateways' enabled ports with a non-empty phone number
async function fetchLines() {
    try {
        const res = await fetch('/asterisk/gateways');
        if (!res.ok) return [];
        const data = await res.json();
        const gateways = data.gateways || [];
        const lines = [];
        for (const gw of gateways) {
            if (!gw.enabled) continue;
            const gwName = gw.name || gw.id || '';
            const ports = gw.ports || [];
            for (const port of ports) {
                if (!port.enabled) continue;
                const number = (port.phone_number || '').trim();
                if (!number) continue;
                lines.push({ number, label: `${gwName} · ${number}` });
            }
        }
        return lines;
    } catch (_) {
        return [];
    }
}

async function fetchUnreadCount() {
    try {
        const op = getOperator();
        const res = await fetch(`/sms/unread?operator=${encodeURIComponent(op)}`);
        if (!res.ok) return 0;
        const data = await res.json();
        return data.unread || 0;
    } catch (_) {
        return 0;
    }
}

async function fetchPreferences() {
    const op = getOperator();
    const res = await fetch(`/sms/preferences?operator=${encodeURIComponent(op)}`);
    if (!res.ok) return { sms_provider: 'anthropic', sms_model: 'claude-sonnet-4-5' };
    return await res.json();
}

async function savePreferences(provider, model) {
    const op = getOperator();
    await fetch('/sms/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operator: op, sms_provider: provider, sms_model: model })
    });
}

// ============================================================================
// Rendering
// ============================================================================

function renderThreadList(threads) {
    const el = $('smsThreadList');
    if (!el) return;

    let html = `<div class="sms-new-msg-btn" id="smsNewMsgBtn">+ New Message</div>`;

    html += `<div class="sms-new-msg" id="smsNewMsg" style="display:none;">
        <input type="text" id="smsNewPhone" placeholder="+1 (555) 123-4567" inputmode="tel" />
        <button id="btnNewMsgStart" class="btn">Go</button>
    </div>`;

    html += `<div class="sms-prefs-bar" id="smsPrefsBar">
        <label class="sms-prefs-label">SMS Model:</label>
        <select id="smsProviderSelect" class="sms-prefs-select">
            <option value="anthropic">Anthropic</option>
            <option value="google">Google</option>
            <option value="openai">OpenAI</option>
            <option value="xai">xAI</option>
        </select>
        <input type="text" id="smsModelInput" class="sms-prefs-input" placeholder="Model name" />
    </div>`;

    if (!threads.length) {
        html += `<div class="sms-empty">
            <span class="sms-empty-icon">💬</span>
            No conversations yet
        </div>`;
    } else {
        for (const t of threads) {
            const active = _currentPhone === t.phone_number ? ' active' : '';
            const unread = t.unread_count > 0 ? ' has-unread' : '';
            const name = escapeHtml(t.contact_name || t.phone_number);
            const phone = escapeHtml(t.phone_number);
            const dirIcon = t.direction === 'outbound' ? '↗ ' : '';
            const preview = escapeHtml(truncate(dirIcon + (t.last_message || '')));
            const time = t.last_timestamp ? timeAgo(t.last_timestamp) : '';
            const badge = t.unread_count > 0
                ? `<span class="sms-thread-badge">${t.unread_count}</span>`
                : '';
            const lineNumber = (t.line_number || '').trim();
            const lineLabel = lineNumber
                ? `<div class="sms-thread-line">via ${escapeHtml(lineNumber)}</div>`
                : '';

            html += `<div class="sms-thread${active}${unread}" data-phone="${escapeHtml(t.phone_number)}">
                <div class="sms-thread-name">${name}</div>
                <div class="sms-thread-phone">${phone}</div>
                ${lineLabel}
                <div class="sms-thread-preview">${preview}</div>
                <div class="sms-thread-meta">
                    <span class="sms-thread-time">${time}</span>
                    ${badge}
                </div>
            </div>`;
        }
    }

    el.innerHTML = html;
    _wireThreadListEvents(el);
}

function _wireThreadListEvents(el) {
    el.querySelectorAll('.sms-thread').forEach(item => {
        item.addEventListener('click', () => {
            const phone = item.getAttribute('data-phone');
            if (phone) selectThread(phone);
        });
    });

    const newBtn = $('smsNewMsgBtn');
    if (newBtn) newBtn.addEventListener('click', handleNewMessage);

    const goBtn = $('btnNewMsgStart');
    if (goBtn) {
        goBtn.addEventListener('click', () => _startNewConversation());
    }

    const phoneInput = $('smsNewPhone');
    if (phoneInput) {
        phoneInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); _startNewConversation(); }
        });
    }

    const provSel = $('smsProviderSelect');
    const modInput = $('smsModelInput');
    if (provSel) {
        provSel.addEventListener('change', async () => {
            const model = $('smsModelInput')?.value || '';
            await savePreferences(provSel.value, model);
            toastSuccess('SMS provider updated');
        });
    }
    if (modInput) {
        modInput.addEventListener('change', async () => {
            const prov = $('smsProviderSelect')?.value || 'anthropic';
            await savePreferences(prov, modInput.value);
            toastSuccess('SMS model updated');
        });
    }
}

function _startNewConversation() {
    const phoneInput = $('smsNewPhone');
    if (!phoneInput) return;
    const val = phoneInput.value.trim();
    if (!val) return;
    selectThread(val);
    const newArea = $('smsNewMsg');
    if (newArea) newArea.style.display = 'none';
}

function renderConversation(messages, phone) {
    // Header with back button for mobile
    const header = $('smsConvHeader');
    if (header) {
        const contactName = messages.length > 0 && messages[0].contact_name
            ? escapeHtml(messages[0].contact_name) : '';

        const nameHtml = contactName
            ? `<div class="sms-conv-name">${contactName}</div><div class="sms-conv-phone">${escapeHtml(phone)}</div>`
            : `<div class="sms-conv-name">${escapeHtml(phone)}</div>`;

        header.innerHTML = `
            <span class="sms-conv-back" id="smsBackBtn" title="Back">&larr;</span>
            <div class="sms-conv-header-info">${nameHtml}</div>
            <div class="sms-conv-status" title="Connected"></div>
        `;

        // Wire back button
        const backBtn = $('smsBackBtn');
        if (backBtn) {
            backBtn.addEventListener('click', () => {
                _currentPhone = null;
                const conv = $('smsConversation');
                if (conv) conv.classList.remove('sms-conv-visible');
                refreshThreads();
            });
        }
    }

    // Messages with date separators
    const container = $('smsMessages');
    if (!container) return;

    if (!messages.length) {
        container.innerHTML = `<div class="sms-empty">
            <span class="sms-empty-icon">✉️</span>
            No messages yet. Send the first one!
        </div>`;
    } else {
        let html = '';
        let lastDate = '';

        for (const m of messages) {
            // Insert date separator when day changes
            const msgDate = dateLabel(m.timestamp);
            if (msgDate !== lastDate) {
                lastDate = msgDate;
                html += `<div class="sms-date-sep"><span>${escapeHtml(msgDate)}</span></div>`;
            }

            const cls = m.direction === 'inbound' ? 'sms-bubble-in' : 'sms-bubble-out';
            const body = escapeHtml(m.body || '');
            const time = formatTime(m.timestamp);
            const statusAttr = m.status ? ` data-status="${escapeHtml(m.status)}"` : '';
            const status = m.direction === 'outbound' && m.status
                ? `<span class="sms-bubble-status"${statusAttr}>${escapeHtml(m.status)}</span>`
                : '';

            html += `<div class="${cls}">
                <div class="sms-bubble-body">${body}</div>
                <div class="sms-bubble-footer">
                    <span class="sms-bubble-time">${time}</span>
                    ${status}
                </div>
            </div>`;
        }
        container.innerHTML = html;
    }

    // Scroll to bottom
    requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight;
    });

    // Show compose area (with from-number selector if we have sendable lines)
    const compose = $('smsCompose');
    if (compose) compose.style.display = 'flex';
    renderFromSelector();
}

function renderFromSelector() {
    const host = $('smsFromSelectorRow');
    if (!host) return;
    if (!_lines.length) {
        host.style.display = 'none';
        host.innerHTML = '';
        return;
    }
    let html = `<label class="sms-from-label" for="smsFromSelect">From:</label>
        <select id="smsFromSelect" class="sms-from-select">
            <option value="">Default line</option>`;
    for (const line of _lines) {
        const sel = line.number === _selectedFromNumber ? ' selected' : '';
        html += `<option value="${escapeHtml(line.number)}"${sel}>${escapeHtml(line.label)}</option>`;
    }
    html += `</select>`;
    host.innerHTML = html;
    host.style.display = 'flex';

    const sel = $('smsFromSelect');
    if (sel) {
        sel.addEventListener('change', () => {
            _selectedFromNumber = sel.value || '';
        });
    }
}

function updateUnreadBadge(count) {
    const badge = $('smsBadge');
    if (!badge) return;
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.style.display = 'inline-flex';
    } else {
        badge.style.display = 'none';
    }
}

// ============================================================================
// Event Handlers
// ============================================================================

async function selectThread(phone) {
    _currentPhone = phone;

    // Highlight in list
    const threadList = $('smsThreadList');
    if (threadList) {
        threadList.querySelectorAll('.sms-thread').forEach(el => {
            el.classList.toggle('active', el.getAttribute('data-phone') === phone);
        });
    }

    // On mobile, show conversation panel
    const conv = $('smsConversation');
    if (conv) conv.classList.add('sms-conv-visible');

    // Show loading state
    const container = $('smsMessages');
    if (container) container.innerHTML = `<div class="sms-empty">Loading...</div>`;

    try {
        const messages = await fetchMessages(phone);
        renderConversation(messages, phone);
    } catch (err) {
        toastError('Failed to load messages');
        console.error('[SMS] fetchMessages error:', err);
    }

    const input = $('smsInput');
    if (input) input.focus();
}

async function handleSend() {
    const input = $('smsInput');
    if (!input || !_currentPhone) return;

    const message = input.value.trim();
    if (!message) return;

    const sendBtn = $('btnSMSSend');

    input.value = '';
    input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    // Add sending class for animation
    const compose = $('smsCompose');
    if (compose) compose.classList.add('sms-sending');

    try {
        await sendMessage(_currentPhone, message);
        const messages = await fetchMessages(_currentPhone);
        renderConversation(messages, _currentPhone);
        // Also refresh thread list to update preview
        refreshThreads();
    } catch (err) {
        toastError(`Send failed: ${err.message}`);
        input.value = message;
    } finally {
        input.disabled = false;
        if (sendBtn) sendBtn.disabled = false;
        if (compose) compose.classList.remove('sms-sending');
        input.focus();
    }
}

function handleNewMessage() {
    const newArea = $('smsNewMsg');
    if (!newArea) return;
    const visible = newArea.style.display !== 'none';
    newArea.style.display = visible ? 'none' : 'flex';
    if (!visible) {
        const phoneInput = $('smsNewPhone');
        if (phoneInput) { phoneInput.value = ''; phoneInput.focus(); }
    }
}

async function refreshThreads() {
    try {
        const threads = await fetchThreads();
        renderThreadList(threads);
    } catch (err) {
        console.error('[SMS] refreshThreads error:', err);
    }
}

function startPolling() {
    stopPolling();
    _pollTimer = setInterval(async () => {
        if (!_isOpen) return;
        await refreshThreads();
        if (_currentPhone) {
            try {
                const messages = await fetchMessages(_currentPhone);
                renderConversation(messages, _currentPhone);
            } catch (_) { /* ignore */ }
        }
    }, 10000);
}

function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ============================================================================
// Init & Export
// ============================================================================

export function initSMSInbox() {
    const btnClose = $('btnCloseSMS');
    if (btnClose) {
        btnClose.addEventListener('click', () => {
            const modal = $('smsInboxModal');
            if (modal) modal.classList.add('hide');
            _isOpen = false;
            _currentPhone = null;
            stopPolling();
        });
    }

    const btnSend = $('btnSMSSend');
    if (btnSend) btnSend.addEventListener('click', handleSend);

    const input = $('smsInput');
    if (input) {
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
        });
    }

    // Click outside modal to close
    const modal = $('smsInboxModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                modal.classList.add('hide');
                _isOpen = false;
                _currentPhone = null;
                stopPolling();
            }
        });
    }

    // Hide compose until thread selected
    const compose = $('smsCompose');
    if (compose) compose.style.display = 'none';
}

export async function openSMSInbox() {
    const modal = $('smsInboxModal');
    if (!modal) return;

    modal.classList.remove('hide');
    _isOpen = true;
    _currentPhone = null;
    _selectedFromNumber = '';

    // Load our sendable lines (enabled gateways' enabled ports) for the from-number selector
    try {
        _lines = await fetchLines();
    } catch (_) {
        _lines = [];
    }

    // Clear conversation panel
    const header = $('smsConvHeader');
    if (header) header.innerHTML = '';
    const messages = $('smsMessages');
    if (messages) messages.innerHTML = `<div class="sms-empty">
        <span class="sms-empty-icon">💬</span>
        Select a conversation
    </div>`;
    const compose = $('smsCompose');
    if (compose) compose.style.display = 'none';

    // Remove mobile visible state
    const conv = $('smsConversation');
    if (conv) conv.classList.remove('sms-conv-visible');

    await refreshThreads();

    // Load and display SMS preferences
    try {
        const prefs = await fetchPreferences();
        const provSel = $('smsProviderSelect');
        const modInput = $('smsModelInput');
        if (provSel) provSel.value = prefs.sms_provider || 'anthropic';
        if (modInput) modInput.value = prefs.sms_model || 'claude-sonnet-4-5';
    } catch (_) {}

    startPolling();
}

export async function pollUnread() {
    const count = await fetchUnreadCount();
    updateUnreadBadge(count);
    return count;
}
