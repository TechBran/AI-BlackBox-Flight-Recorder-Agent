/**
 * devices-section.js
 * System-Menu "Devices" section (M3 task 3.7 — frontier device-control build).
 *
 * Lists ALL Tailscale-net devices via GET /devices/mesh and lets an operator
 * claim a device (owner), mark one primary device per operator, and set a
 * device's default frontier provider. Consumes the M3 mesh-JOIN surface:
 *   GET  /devices/mesh?operator=X            → the whole tailnet + ownership
 *   POST /devices/{id}/operator {operator}   → assign/claim
 *   POST /devices/{id}/primary  {operator}   → set operator's primary
 *   POST /devices/{id}/default-provider {provider, operator?}
 *
 * This is DISTINCT from device-manager.js (the ADB pair/connect modal on the
 * older GET /devices/ CRUD) — it does not touch that surface.
 */

import { $, toastSuccess, toastError } from './core-utils.js';

// Provider choices mirror the backend enum {gemma,gemini,claude,openai|null}.
const PROVIDERS = [
    { value: '',       label: 'No default' },
    { value: 'gemma',  label: 'Gemma (on-device)' },
    { value: 'gemini', label: 'Gemini' },
    { value: 'claude', label: 'Claude' },
    { value: 'openai', label: 'OpenAI' },
];

const TYPE_ICON = { android: '📱', windows: '🖥️', macos: '💻', linux: '🐧' };

// -----------------------------------------------------------------------------
// State
// -----------------------------------------------------------------------------
let _operators = [];       // live operator roster (from /operators)
let _filter = '';          // '' = all operators; else filter mesh to one operator
let _loading = false;
let _lastMenuVisible = false;

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------
function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

// Tailscale status can list the same relay/ingress node multiple times; keep the
// first occurrence of each id so the management list shows one row per device.
function dedupById(devices) {
    const seen = new Set();
    const out = [];
    for (const d of devices) {
        if (!d || d.id == null || seen.has(d.id)) continue;
        seen.add(d.id);
        out.push(d);
    }
    return out;
}

// -----------------------------------------------------------------------------
// API
// -----------------------------------------------------------------------------
async function fetchOperators() {
    try {
        const res = await fetch('/operators');
        if (!res.ok) return [];
        const data = await res.json();
        return Array.isArray(data.operators) ? data.operators : [];
    } catch {
        return [];
    }
}

async function fetchMesh(operator) {
    const qs = operator ? `?operator=${encodeURIComponent(operator)}` : '';
    const res = await fetch(`/devices/mesh${qs}`);
    if (!res.ok) throw new Error(`mesh ${res.status}`);
    const data = await res.json();
    return Array.isArray(data.devices) ? data.devices : [];
}

async function postJson(url, body) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Request failed (${res.status})`);
    }
    return res.json();
}

const assignOperator = (id, operator) =>
    postJson(`/devices/${encodeURIComponent(id)}/operator`, { operator });

const setPrimary = (id, operator) =>
    postJson(`/devices/${encodeURIComponent(id)}/primary`, { operator });

const setProvider = (id, provider, operator) => {
    const body = { provider: provider || null };
    if (operator) body.operator = operator;
    return postJson(`/devices/${encodeURIComponent(id)}/default-provider`, body);
};

// -----------------------------------------------------------------------------
// Rendering
// -----------------------------------------------------------------------------
function buildCard(d) {
    const owned = !!d.owner;
    const online = !!d.online;
    const icon = TYPE_ICON[(d.type || '').toLowerCase()] || '🖥️';

    const card = document.createElement('div');
    card.className = 'device-card' + (online ? '' : ' offline');

    const ownerOptions = [
        owned ? '' : '<option value="" selected disabled>— Unassigned —</option>',
        ..._operators.map((op) =>
            `<option value="${escapeHtml(op)}"${owned && op === d.owner ? ' selected' : ''}>${escapeHtml(op)}</option>`),
    ].join('');

    const providerOptions = PROVIDERS.map((p) =>
        `<option value="${p.value}"${(d.default_provider || '') === p.value ? ' selected' : ''}>${p.label}</option>`
    ).join('');

    // Primary is per-owner and requires ownership; there is no "unset" route, so
    // an already-primary device shows a locked badge (clearing happens implicitly
    // when another device is made primary for that owner).
    let primaryCtl = '';
    if (owned) {
        primaryCtl = d.is_primary
            ? `<button class="device-primary is-primary" type="button" disabled title="${escapeHtml(d.owner)}'s primary device">★ Primary</button>`
            : `<button class="device-primary" type="button" title="Make this ${escapeHtml(d.owner)}'s primary device">☆ Make primary</button>`;
    }

    card.innerHTML = `
        <div class="device-card-head">
            <span class="device-dot ${online ? 'online' : 'offline'}" title="${online ? 'Online' : 'Offline'}"></span>
            <span class="device-name">${escapeHtml(d.name || d.id)}</span>
            <span class="device-type">${icon} ${escapeHtml(d.type || '')}</span>
            ${primaryCtl}
        </div>
        <div class="device-tailnet" title="${escapeHtml(d.tailnet || '')}">${escapeHtml(d.tailnet || '—')}</div>
        <div class="device-card-controls">
            <label class="device-ctl">
                <span class="device-ctl-label">Owner</span>
                <select class="device-owner-select">${ownerOptions}</select>
            </label>
            <label class="device-ctl">
                <span class="device-ctl-label">Provider</span>
                <select class="device-provider-select"${owned ? '' : ' disabled title="Assign an owner first"'}>${providerOptions}</select>
            </label>
        </div>`;

    // Owner assignment (claim / reassign). Assigning auto-registers a tailnet node.
    const ownerSel = card.querySelector('.device-owner-select');
    ownerSel.addEventListener('change', async () => {
        const val = ownerSel.value;
        if (!val) return;
        ownerSel.disabled = true;
        try {
            await assignOperator(d.id, val);
            toastSuccess(`${d.name || d.id} → ${val}`);
            await refresh();
        } catch (e) {
            toastError(e.message || 'Failed to assign owner');
            ownerSel.disabled = false;
        }
    });

    // Default provider.
    const provSel = card.querySelector('.device-provider-select');
    if (provSel && owned) {
        provSel.addEventListener('change', async () => {
            const val = provSel.value;
            provSel.disabled = true;
            try {
                await setProvider(d.id, val, d.owner);
                toastSuccess(val ? `Default provider: ${val}` : 'Default provider cleared');
                await refresh();
            } catch (e) {
                toastError(e.message || 'Failed to set provider');
                provSel.disabled = false;
            }
        });
    }

    // Primary toggle (owned, not-yet-primary only).
    const primBtn = card.querySelector('button.device-primary:not([disabled])');
    if (primBtn) {
        primBtn.addEventListener('click', async () => {
            primBtn.disabled = true;
            try {
                await setPrimary(d.id, d.owner);
                toastSuccess(`${d.name || d.id} is now ${d.owner}'s primary`);
                await refresh();
            } catch (e) {
                toastError(e.message || 'Failed to set primary');
                primBtn.disabled = false;
            }
        });
    }

    return card;
}

function render(devices) {
    const list = $('devicesList');
    if (!list) return;
    if (!devices.length) {
        list.innerHTML = `<p class="devices-empty">No devices found on your Tailscale network${_filter ? ` for ${escapeHtml(_filter)}` : ''}.</p>`;
        return;
    }
    const frag = document.createDocumentFragment();
    devices.forEach((d) => frag.appendChild(buildCard(d)));
    list.innerHTML = '';
    list.appendChild(frag);
}

function syncOperatorFilter() {
    const sel = $('devicesOperatorFilter');
    if (!sel) return;
    sel.innerHTML = '<option value="">All operators</option>' +
        _operators.map((op) => `<option value="${escapeHtml(op)}">${escapeHtml(op)}</option>`).join('');
    sel.value = _filter || '';
}

async function refresh() {
    if (_loading) return;
    _loading = true;
    const list = $('devicesList');
    try {
        const [ops, devices] = await Promise.all([
            _operators.length ? Promise.resolve(_operators) : fetchOperators(),
            fetchMesh(_filter),
        ]);
        _operators = ops;
        syncOperatorFilter();
        render(dedupById(devices));
    } catch (e) {
        if (list) {
            list.innerHTML = `<p class="devices-empty devices-error">Could not load devices (${escapeHtml(e.message || 'error')}). Is Tailscale running?</p>`;
        }
    } finally {
        _loading = false;
    }
}

// -----------------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------------
export function initDevicesSection() {
    const section = document.querySelector('.devices-section');
    if (!section) return;

    const filterSel = $('devicesOperatorFilter');
    if (filterSel) {
        filterSel.addEventListener('change', () => {
            _filter = filterSel.value;
            refresh();
        });
    }

    const btnRefresh = $('btnRefreshDevices');
    if (btnRefresh) btnRefresh.addEventListener('click', () => refresh());

    // Refresh whenever the System Menu opens (mirrors the Updates panel's
    // fetch-on-open behavior) without editing ui-setup.js — observe #menuModal.
    const menu = document.getElementById('menuModal');
    if (menu) {
        const obs = new MutationObserver(() => {
            const visible = !menu.classList.contains('hide');
            if (visible && !_lastMenuVisible) refresh();
            _lastMenuVisible = visible;
        });
        obs.observe(menu, { attributes: true, attributeFilter: ['class'] });
    }

    console.log('[Devices] System-Menu devices section initialized');
}

export default { initDevicesSection };
