/**
 * sms-inbox.js
 * SMS Inbox — thread list, conversation view, compose, unread badges
 * Works in both web (two-panel) and Android/mobile (full-screen) contexts
 */
import { $, toast, toastSuccess, toastError, escapeHtml as sharedEscapeHtml } from './core-utils.js';
import {
    getOperator, OPERATOR_REQUIRED_MESSAGE, fetchAvailableModels, getHydratedModels, getProviderName,
} from './state-management.js';
import { normalizePhone, parsePhoneInput } from './phone.js';

let _pollTimer = null;
// The open thread: its key (the box's phone_number, last 10 digits) and where a send
// from it goes (the number as typed for a new conversation, digits only).
let _currentPhone = null;
let _currentTo = null;
let _isOpen = false;
// Our sendable lines (enabled gateways' enabled ports with a phone number)
let _lines = [];
// Selected "from" line for outbound SMS; '' = use backend default gateway
let _selectedFromNumber = '';

// The open conversation as shown, oldest first: the newest page the box serves plus
// any older pages loaded with "Load earlier". _hasMore/_nextBeforeId describe what is
// older than its first message; _convSig is what was last rendered.
let _messages = [];
let _hasMore = false;
let _nextBeforeId = null;
let _convSig = '';
// Bumped per thread open, so a read that lands after another thread opened is dropped.
let _convSeq = 0;
let _modelSeq = 0;
// The model the box runs an "Auto" SMS reply on, for the provider it was asked for
// (GET /sms/preferences sms_model_resolved).
let _resolved = { provider: '', model: '' };

const PAGE_SIZE = 100;
// Within this many px of the bottom counts as "at the bottom": new messages follow.
const BOTTOM_SLACK_PX = 40;
// Providers an SMS reply can run on: the chat providers of the box's non-streaming
// /chat worker (tasks.process_chat_task). Each one's models come from its live catalog
// (/models/<provider>); custom is offered once a LAN server is set up.
const SMS_PROVIDERS = ['anthropic', 'google', 'openai', 'xai', 'custom'];

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
    return str.length > len ? str.slice(0, len) + '…' : str;
}

function escapeHtml(text) {
    // Shared helper: also escapes quotes (used in data-phone / value attributes)
    return sharedEscapeHtml(text);
}

/** One thread, however each side wrote the number. */
function samePhone(a, b) {
    return !!a && !!b && normalizePhone(a) === normalizePhone(b);
}

function isAtBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK_PX;
}

/** Jump without the message area's smooth scrolling (keeping a reader's place must
 *  not animate the view past what they were reading). */
function jumpScrollTop(el, top) {
    const prev = el.style.scrollBehavior;
    el.style.scrollBehavior = 'auto';
    el.scrollTop = top;
    el.style.scrollBehavior = prev;
}

const SEND_ICON = `<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>`;

/**
 * The SMS reply provider as the inbox's select names it. The box treats "google" and
 * "gemini" alike and the Android app used to save "gemini", which this select does not
 * list; "" (nothing stored) is the box's own default, Anthropic.
 */
function smsProviderId(raw) {
    const id = String(raw || '').trim();
    if (id === 'gemini') return 'google';
    return id || 'anthropic';
}

// ============================================================================
// API calls
// ============================================================================

// Reads with no operator resolved return nothing: there is nobody's inbox to read
// (and `operator=` would only 422). Writes refuse with OPERATOR_REQUIRED_MESSAGE.
async function fetchThreads() {
    const op = getOperator();
    if (!op) return [];
    const res = await fetch(`/sms/threads?operator=${encodeURIComponent(op)}`);
    if (!res.ok) throw new Error(`Failed to fetch threads: ${res.status}`);
    const data = await res.json();
    return data.threads || [];
}

/**
 * The newest page of a thread, or the page just before message [beforeId] (the box
 * serves the newest window; next_before_id is the cursor for the page above it).
 * @returns {Promise<{messages: Array, hasMore: boolean, nextBeforeId: (number|null)} | null>}
 *          null while no operator is resolved
 */
async function fetchMessages(phone, beforeId = null) {
    const op = getOperator();
    if (!op) return null;
    let url = `/sms/messages?operator=${encodeURIComponent(op)}&phone=${encodeURIComponent(phone)}&limit=${PAGE_SIZE}`;
    if (beforeId != null) url += `&before_id=${encodeURIComponent(beforeId)}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Failed to fetch messages: ${res.status}`);
    const data = await res.json();
    return {
        messages: data.messages || [],
        hasMore: !!data.has_more,
        nextBeforeId: data.next_before_id ?? null,
    };
}

// The thread is on screen: mark it read. GET /sms/messages is read-only, so
// this is explicit. Best-effort: a failure only leaves the badge up.
function markThreadRead(op, phone) {
    fetch('/sms/mark-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operator: op, phone })
    }).catch(() => {});
}

async function sendMessage(to, message) {
    const op = getOperator();
    // Never as "": the SMS would really go out, filed in no operator's inbox.
    if (!op) throw new Error(OPERATOR_REQUIRED_MESSAGE);
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
        // A 422 carries FastAPI's list of field errors, not a string.
        const detail = Array.isArray(data.detail) ? data.detail.map(d => d.msg).join('; ') : data.detail;
        throw new Error(data.error || detail || 'Send failed');
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
        if (!op) return 0;
        const res = await fetch(`/sms/unread?operator=${encodeURIComponent(op)}`);
        if (!res.ok) return 0;
        const data = await res.json();
        return data.unread || 0;
    } catch (_) {
        return 0;
    }
}

// An empty sms_model is "Auto": the box answers with its own configured default for
// the provider, so the inbox never pins a model id that later retires.
async function fetchPreferences() {
    const op = getOperator();
    if (!op) return { sms_provider: 'anthropic', sms_model: '' };
    const res = await fetch(`/sms/preferences?operator=${encodeURIComponent(op)}`);
    if (!res.ok) return { sms_provider: 'anthropic', sms_model: '' };
    return await res.json();
}

/** @returns {Promise<boolean>} false when nothing was saved (no operator, or the box refused) */
async function savePreferences(provider, model) {
    const op = getOperator();
    if (!op) {
        toastError(OPERATOR_REQUIRED_MESSAGE);
        return false;
    }
    try {
        const res = await fetch('/sms/preferences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operator: op, sms_provider: provider, sms_model: model })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
        toastError(`SMS model not saved: ${err.message}`);
        return false;
    }
    return true;
}

// ============================================================================
// Reply model: providers and models from the live catalogs (P06-comms-web-09)
// ============================================================================

function addOption(sel, value, label) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    sel.appendChild(opt);
}

/** List the SMS providers (custom once a LAN server is set up) and show [selected],
 *  adding it when the list lacks it, so a stored provider is never shown blank (and
 *  then saved back as another one). */
function renderProviderOptions(selected) {
    const sel = $('smsProviderSelect');
    if (!sel) return;
    const offered = SMS_PROVIDERS.filter(p => p !== 'custom' || getHydratedModels('custom').length > 0);
    if (selected && !offered.includes(selected)) offered.push(selected);
    if (Array.from(sel.options).map(o => o.value).join('\n') !== offered.join('\n')) {
        const keep = selected || sel.value;
        sel.textContent = '';
        for (const p of offered) addOption(sel, p, getProviderName(p));
        if (keep) sel.value = keep;
    }
    if (selected) sel.value = selected;
}

/** "(Auto - <the model the box runs>)" when the box said which; else the catalog's. */
function autoLabel(provider) {
    const models = getHydratedModels(provider);
    if (_resolved.provider === provider && _resolved.model) {
        const named = models.find(m => m.id === _resolved.model);
        return `(Auto - ${named ? named.name : _resolved.model})`;
    }
    const auto = models.find(m => m.id === '');
    return auto ? auto.name : '(Auto)';
}

/** Fill the model select from [provider]'s live catalog and show [selectedId]: a stored
 *  id the catalog does not list is shown as itself, never as Auto or another model. */
async function renderModelOptions(provider, selectedId) {
    const seq = ++_modelSeq;
    await fetchAvailableModels(provider);
    const sel = $('smsModelSelect');
    if (seq !== _modelSeq || !sel) return;  // a newer provider pick won
    sel.textContent = '';
    addOption(sel, '', autoLabel(provider));
    const models = getHydratedModels(provider).filter(m => m.id !== '');
    for (const m of models) addOption(sel, m.id, m.name);
    const want = selectedId || '';
    if (want && !models.some(m => m.id === want)) addOption(sel, want, want);
    sel.value = want;
}

/** Re-read which model an Auto reply runs on (it moves with the saved provider). */
async function refreshResolvedModel() {
    try {
        const prefs = await fetchPreferences();
        _resolved = { provider: smsProviderId(prefs.sms_provider), model: prefs.sms_model_resolved || '' };
        const sel = $('smsModelSelect');
        const provider = $('smsProviderSelect')?.value;
        if (sel && sel.options.length && provider) sel.options[0].textContent = autoLabel(provider);
    } catch (_) { /* the label stays as it was */ }
}

async function loadPreferences() {
    const prefs = await fetchPreferences();
    const provider = smsProviderId(prefs.sms_provider);
    _resolved = { provider, model: prefs.sms_model_resolved || '' };
    await fetchAvailableModels('custom');  // is a LAN server set up? (never cached)
    renderProviderOptions(provider);
    await renderModelOptions(provider, prefs.sms_model || '');
}

// ============================================================================
// Rendering
// ============================================================================

/**
 * The thread list's fixed part — "+ New Message", its number row and the reply-model
 * bar — rendered ONCE, above #smsThreadRows. A poll only diffs the rows: rebuilding the
 * bar reset the saved provider/model (and the next edit saved the wrong provider) and
 * wiped a number being typed (audit P06-comms-web-03).
 * @returns {HTMLElement|null} the rows container
 */
function ensureThreadListChrome() {
    const el = $('smsThreadList');
    if (!el) return null;
    const existing = $('smsThreadRows');
    if (existing && el.contains(existing)) return existing;

    el.innerHTML = `<div class="sms-new-msg-btn" id="smsNewMsgBtn">+ New Message</div>
        <div class="sms-new-msg" id="smsNewMsg" style="display:none;">
            <input type="text" id="smsNewPhone" placeholder="+1 (555) 123-4567" inputmode="tel" autocomplete="tel" />
            <button id="btnNewMsgStart" class="btn">Go</button>
        </div>
        <div class="sms-prefs-bar" id="smsPrefsBar">
            <label class="sms-prefs-label" for="smsProviderSelect">SMS Model:</label>
            <select id="smsProviderSelect" class="sms-prefs-select" title="Provider for SMS replies"></select>
            <select id="smsModelSelect" class="sms-prefs-select sms-prefs-input" title="Model for SMS replies"></select>
        </div>
        <div class="sms-thread-rows" id="smsThreadRows"></div>`;
    _wireThreadListChrome();
    renderProviderOptions();
    return $('smsThreadRows');
}

function threadRowHtml(t) {
    const name = escapeHtml(t.contact_name || t.phone_number);
    const phone = escapeHtml(t.phone_number);
    const dirIcon = t.direction === 'outbound' ? '↗ ' : '';
    const preview = escapeHtml(truncate(dirIcon + (t.last_message || '')));
    const time = t.last_timestamp ? timeAgo(t.last_timestamp) : '';
    const badge = t.unread_count > 0
        ? `<span class="sms-thread-badge">${escapeHtml(t.unread_count)}</span>`
        : '';
    const lineNumber = (t.line_number || '').trim();
    const lineLabel = lineNumber
        ? `<div class="sms-thread-line">via ${escapeHtml(lineNumber)}</div>`
        : '';
    return `<div class="sms-thread-name">${name}</div>
                <div class="sms-thread-phone">${phone}</div>
                ${lineLabel}
                <div class="sms-thread-preview">${preview}</div>
                <div class="sms-thread-meta">
                    <span class="sms-thread-time">${escapeHtml(time)}</span>
                    ${badge}
                </div>`;
}

/** What a row shows; the row is rebuilt only when this changes. */
function threadSignature(t) {
    return JSON.stringify([t.contact_name, t.phone_number, t.last_message, t.direction,
        t.unread_count, t.line_number, t.last_timestamp ? timeAgo(t.last_timestamp) : '']);
}

function renderThreadList(threads) {
    const rows = ensureThreadListChrome();
    if (!rows) return;

    // The box lists one row per phone; never show two rows for one thread anyway.
    const seen = new Set();
    const list = threads.filter((t) => {
        const key = normalizePhone(t.phone_number) || String(t.phone_number || '');
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
    });

    if (!list.length) {
        if (!rows.querySelector(':scope > .sms-empty')) {
            rows.innerHTML = `<div class="sms-empty">
            <span class="sms-empty-icon">💬</span>
            No conversations yet
        </div>`;
        }
        return;
    }
    rows.querySelector(':scope > .sms-empty')?.remove();

    // Keyed diff: a row is rebuilt only when what it shows changed, and moved only
    // when the order changed, so an unchanged poll touches nothing.
    const byPhone = new Map();
    rows.querySelectorAll(':scope > .sms-thread').forEach(el => byPhone.set(el.dataset.phone, el));
    const wanted = list.map((t) => {
        const phone = String(t.phone_number);
        let item = byPhone.get(phone);
        byPhone.delete(phone);
        if (!item) {
            item = document.createElement('div');
            item.dataset.phone = phone;
        }
        const sig = threadSignature(t);
        if (item.dataset.sig !== sig) {
            item.className = `sms-thread${t.unread_count > 0 ? ' has-unread' : ''}`;
            item.innerHTML = threadRowHtml(t);
            item.dataset.sig = sig;
        }
        const active = samePhone(phone, _currentPhone);
        if (item.classList.contains('active') !== active) item.classList.toggle('active', active);
        return item;
    });
    for (const gone of byPhone.values()) gone.remove();
    let cursor = rows.firstElementChild;
    for (const item of wanted) {
        if (item === cursor) {
            cursor = cursor.nextElementSibling;
        } else {
            rows.insertBefore(item, cursor);
        }
    }
}

function _wireThreadListChrome() {
    // One delegated listener: rows come and go, the listener stays.
    const rows = $('smsThreadRows');
    if (rows) {
        rows.addEventListener('click', (e) => {
            const item = e.target.closest('.sms-thread');
            if (item && rows.contains(item) && item.dataset.phone) selectThread(item.dataset.phone);
        });
    }

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
    const modSel = $('smsModelSelect');
    if (provSel) {
        provSel.addEventListener('change', async () => {
            const provider = provSel.value;
            // A new provider starts on Auto: the previous provider's model id is
            // foreign to it (a Claude id sent to OpenAI fails every reply).
            const field = $('smsModelSelect');
            if (field) field.value = '';
            const saved = await savePreferences(provider, '');
            await renderModelOptions(provider, '');
            if (saved) {
                toastSuccess('SMS provider updated');
                refreshResolvedModel();
            }
        });
    }
    if (modSel) {
        modSel.addEventListener('change', async () => {
            // The provider the select shows (renderProviderOptions keeps a stored one
            // listed); never a fallback that would switch the reply provider behind the model.
            const prov = $('smsProviderSelect')?.value;
            if (!prov) return;
            if (await savePreferences(prov, modSel.value)) {
                toastSuccess('SMS model updated');
                refreshResolvedModel();
            }
        });
    }
}

function _startNewConversation() {
    const phoneInput = $('smsNewPhone');
    if (!phoneInput) return;
    const val = phoneInput.value.trim();
    if (!val) return;
    if (!getOperator()) {
        toastError(OPERATOR_REQUIRED_MESSAGE);
        return;
    }
    // The thread is filed under the box's key for the number, and the send goes to the
    // number without its punctuation (the gateway refuses spaces and dashes).
    const parsed = parsePhoneInput(val);
    if (!parsed) {
        toastError('Enter a phone number with at least 10 digits');
        phoneInput.focus();
        return;
    }
    selectThread(parsed.key, parsed.to);
    const newArea = $('smsNewMsg');
    if (newArea) newArea.style.display = 'none';
}

function renderHeader(phone, contactName) {
    const header = $('smsConvHeader');
    if (!header) return;
    const sig = JSON.stringify([phone, contactName || '']);
    if (header.dataset.sig === sig) return;
    header.dataset.sig = sig;

    const nameHtml = contactName
        ? `<div class="sms-conv-name">${escapeHtml(contactName)}</div><div class="sms-conv-phone">${escapeHtml(phone)}</div>`
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
            _currentTo = null;
            const conv = $('smsConversation');
            if (conv) conv.classList.remove('sms-conv-visible');
            refreshThreads();
        });
    }
}

function clearHeader() {
    const header = $('smsConvHeader');
    if (!header) return;
    header.innerHTML = '';
    delete header.dataset.sig;
}

function conversationSignature() {
    return JSON.stringify([_hasMore, _messages.map(m => [m.id, m.status, m.body, m.timestamp])]);
}

/**
 * Render the open conversation. [mode]: 'bottom' shows its newest message (a thread
 * opened, our own send); 'follow' (a poll) keeps the reader where they are unless they
 * were already at the bottom (audit P06-comms-web-12); 'prepend' (older page loaded)
 * keeps the message they were reading in place.
 */
function renderMessages(mode) {
    const container = $('smsMessages');
    if (!container) return;
    const wasAtBottom = isAtBottom(container);
    const prevHeight = container.scrollHeight;
    const prevTop = container.scrollTop;

    if (!_messages.length) {
        container.innerHTML = `<div class="sms-empty">
            <span class="sms-empty-icon">✉️</span>
            No messages yet. Send the first one!
        </div>`;
        return;
    }

    let html = _hasMore
        ? `<div class="sms-load-earlier"><button type="button" id="smsLoadEarlier" class="btn">Load earlier messages</button></div>`
        : '';
    let lastDate = '';
    for (const m of _messages) {
        // Insert date separator when day changes
        const msgDate = dateLabel(m.timestamp);
        if (msgDate !== lastDate) {
            lastDate = msgDate;
            html += `<div class="sms-date-sep"><span>${escapeHtml(msgDate)}</span></div>`;
        }

        const cls = m.direction === 'inbound' ? 'sms-bubble-in' : 'sms-bubble-out';
        const body = escapeHtml(m.body || '');
        const time = escapeHtml(formatTime(m.timestamp));
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

    if (mode === 'bottom' || (mode === 'follow' && wasAtBottom)) {
        container.scrollTop = container.scrollHeight;
    } else if (mode === 'prepend') {
        jumpScrollTop(container, container.scrollHeight - prevHeight + prevTop);
    } else {
        jumpScrollTop(container, prevTop);
    }
}

/**
 * Fold the box's newest page into what is shown: messages older than the page (pages
 * loaded with "Load earlier", or ones that slid out of the newest window) stay; the
 * page replaces the rest. A page that does not overlap what is shown starts afresh.
 * @returns {boolean} true when the page started afresh
 */
function mergeNewestPage(page) {
    if (!_messages.length || !page.length) {
        _messages = page.slice();
        return true;
    }
    const at = _messages.findIndex(m => m.id === page[0].id);
    if (at < 0) {
        _messages = page.slice();
        return true;
    }
    _messages = _messages.slice(0, at).concat(page);
    return false;
}

/** Read the open thread's newest page and show it if anything changed. */
async function loadConversation(mode) {
    const phone = _currentPhone;
    if (!phone) return;
    const seq = _convSeq;
    let page;
    try {
        page = await fetchMessages(phone);
    } catch (err) {
        if (mode === 'bottom') toastError('Failed to load messages');
        console.error('[SMS] fetchMessages error:', err);
        return;
    }
    if (!page || seq !== _convSeq || phone !== _currentPhone) return;

    const shownIds = new Set(_messages.map(m => m.id));
    if (mergeNewestPage(page.messages)) {
        _hasMore = page.hasMore;
        _nextBeforeId = page.nextBeforeId;
    }
    const sig = conversationSignature();
    if (sig === _convSig) return;
    _convSig = sig;
    renderMessages(mode);

    const contactName = _messages.find(m => m.contact_name)?.contact_name || '';
    if (contactName) renderHeader(phone, contactName);
    // On screen now: the thread was opened, or a new inbound text arrived in it.
    const op = getOperator();
    const newInbound = _messages.some(m => m.direction === 'inbound' && !shownIds.has(m.id));
    if (op && (mode === 'bottom' || newInbound)) markThreadRead(op, phone);
}

/** "Load earlier": the page just above the oldest message shown (the box's cursor). */
async function loadEarlier() {
    const phone = _currentPhone;
    if (!phone || !_hasMore || _nextBeforeId == null) return;
    const seq = _convSeq;
    const btn = $('smsLoadEarlier');
    if (btn) btn.disabled = true;
    let page;
    try {
        page = await fetchMessages(phone, _nextBeforeId);
    } catch (err) {
        toastError('Failed to load earlier messages');
        console.error('[SMS] loadEarlier error:', err);
        if (btn) btn.disabled = false;
        return;
    }
    if (!page || seq !== _convSeq || phone !== _currentPhone) return;
    const shownIds = new Set(_messages.map(m => m.id));
    _messages = page.messages.filter(m => !shownIds.has(m.id)).concat(_messages);
    _hasMore = page.hasMore;
    _nextBeforeId = page.nextBeforeId;
    _convSig = conversationSignature();
    renderMessages('prepend');
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

/**
 * Open the thread filed under [phone] (the box's key for the number); sends from it go
 * to [to] (default: the key itself, which is what an existing thread replies to).
 */
async function selectThread(phone, to = phone) {
    _currentPhone = phone;
    _currentTo = to;
    _convSeq++;
    _messages = [];
    _hasMore = false;
    _nextBeforeId = null;
    _convSig = '';

    // Highlight in list
    let row = null;
    const threadList = $('smsThreadList');
    if (threadList) {
        threadList.querySelectorAll('.sms-thread').forEach(el => {
            const active = samePhone(el.getAttribute('data-phone'), phone);
            if (active) row = el;
            if (el.classList.contains('active') !== active) el.classList.toggle('active', active);
        });
    }

    // On mobile, show conversation panel
    const conv = $('smsConversation');
    if (conv) conv.classList.add('sms-conv-visible');

    // The row names the contact (its name line shows the number when there is none).
    const named = row?.querySelector('.sms-thread-name')?.textContent || '';
    renderHeader(phone, named && named !== row.dataset.phone ? named : '');

    // Show loading state
    const container = $('smsMessages');
    if (container) container.innerHTML = `<div class="sms-empty">Loading...</div>`;

    // Show compose area (with from-number selector if we have sendable lines)
    const compose = $('smsCompose');
    if (compose) compose.style.display = 'flex';
    renderFromSelector();

    await loadConversation('bottom');

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
        await sendMessage(_currentTo || _currentPhone, message);
        await loadConversation('bottom');
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
        // Nobody is looking: skip the tick (the next visible one catches up).
        if (!_isOpen || document.hidden) return;
        await refreshThreads();
        if (_currentPhone) await loadConversation('follow');
    }, 10000);
}

function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ============================================================================
// Init & Export
// ============================================================================

function closeInbox() {
    const modal = $('smsInboxModal');
    if (modal) modal.classList.add('hide');
    _isOpen = false;
    _currentPhone = null;
    _currentTo = null;
    _convSeq++;
    stopPolling();
}

/** "Load earlier" is re-rendered with the messages; one delegated listener serves it. */
function ensureConversationWiring() {
    const container = $('smsMessages');
    if (!container || container.dataset.wired) return;
    container.dataset.wired = '1';
    container.addEventListener('click', (e) => {
        if (e.target.closest('#smsLoadEarlier')) loadEarlier();
    });
}

export function initSMSInbox() {
    const btnClose = $('btnCloseSMS');
    if (btnClose) btnClose.addEventListener('click', closeInbox);

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
            if (e.target === modal) closeInbox();
        });
    }

    // Hide compose until thread selected
    const compose = $('smsCompose');
    if (compose) compose.style.display = 'none';

    ensureThreadListChrome();
    ensureConversationWiring();
}

export async function openSMSInbox() {
    const modal = $('smsInboxModal');
    if (!modal) return;

    modal.classList.remove('hide');
    _isOpen = true;
    _currentPhone = null;
    _currentTo = null;
    _selectedFromNumber = '';
    _convSeq++;
    _messages = [];
    _convSig = '';

    ensureThreadListChrome();
    ensureConversationWiring();
    const newArea = $('smsNewMsg');
    if (newArea) newArea.style.display = 'none';

    // Load our sendable lines (enabled gateways' enabled ports) for the from-number selector
    try {
        _lines = await fetchLines();
    } catch (_) {
        _lines = [];
    }

    // Clear conversation panel
    clearHeader();
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
    if (!getOperator()) toastError(OPERATOR_REQUIRED_MESSAGE);

    // Load and display SMS preferences (once per open: a poll never touches them)
    try {
        await loadPreferences();
    } catch (_) {}

    startPolling();
}

export async function pollUnread() {
    const count = await fetchUnreadCount();
    updateUnreadBadge(count);
    return count;
}
