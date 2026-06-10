/**
 * telephony-manager.js
 * Telephony Gateway Manager — discover, add, configure, and monitor Yeastar TG200 GSM-to-SIP gateways
 */

import { $, toast, toastSuccess, toastError } from './core-utils.js';
import { getOperator } from './state-management.js';
import { openWizard } from './telephony-wizard.js';

// =============================================================================
// State
// =============================================================================

let allGateways = [];
let _pollInterval = null;
let _isDiscovering = false;
let _operators = [];

// Models and their GSM port counts (mirrors backend MODEL_PORTS).
const MODEL_PORTS = { TG100: 1, TG200: 2, TG400: 4, TG800: 8 };

async function fetchOperators() {
    try {
        const r = await fetch('/health');
        const j = await r.json();
        _operators = (j.users && Array.isArray(j.users.list)) ? j.users.list : [];
    } catch (e) {
        console.error('[Telephony] Failed to fetch operators:', e);
        _operators = [];
    }
}

// =============================================================================
// API Functions
// =============================================================================

async function fetchGateways() {
    try {
        const res = await fetch('/asterisk/gateways');
        if (!res.ok) throw new Error('Failed to fetch gateways');
        const data = await res.json();
        allGateways = data.gateways || [];
        renderGateways();
    } catch (e) {
        console.error('[Telephony] Failed to fetch gateways:', e);
        toastError('Failed to load gateways');
    }
}

async function discoverGateways() {
    if (_isDiscovering) return;
    _isDiscovering = true;

    const btn = $('btnDiscoverGateways');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="discover-spinner"></span> Scanning...';
    }

    try {
        const res = await fetch('/asterisk/gateways/discover', { method: 'POST' });
        if (!res.ok) throw new Error('Discovery failed');
        const data = await res.json();
        const discovered = data.discovered || data.gateways || [];

        if (discovered.length === 0) {
            toast('No new gateways found on the network');
        } else {
            toastSuccess(`Found ${discovered.length} gateway(s)`);
            renderDiscoveredGateways(discovered);
        }
    } catch (e) {
        console.error('[Telephony] Discovery failed:', e);
        toastError('Gateway discovery failed: ' + e.message);
    } finally {
        _isDiscovering = false;
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Auto-Discover';
        }
    }
}

async function addGateway(config) {
    try {
        const res = await fetch('/asterisk/gateways', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to add gateway');
        }
        toastSuccess('Gateway added');
        hideAddForm();
        await fetchGateways();
    } catch (e) {
        console.error('[Telephony] Add gateway failed:', e);
        toastError('Failed to add gateway: ' + e.message);
    }
}

async function updateGateway(id, config) {
    try {
        const res = await fetch(`/asterisk/gateways/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to update gateway');
        }
        toastSuccess('Gateway updated');
        await fetchGateways();
    } catch (e) {
        console.error('[Telephony] Update gateway failed:', e);
        toastError('Failed to update gateway: ' + e.message);
    }
}

async function removeGateway(id) {
    if (!confirm('Remove this gateway? This will disconnect all associated SIM channels.')) return;
    try {
        const res = await fetch(`/asterisk/gateways/${id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to remove gateway');
        toast('Gateway removed');
        await fetchGateways();
    } catch (e) {
        console.error('[Telephony] Remove gateway failed:', e);
        toastError('Failed to remove gateway: ' + e.message);
    }
}

async function testGateway(id) {
    try {
        toast('Testing gateway...');
        const res = await fetch(`/asterisk/gateways/${id}/test`, { method: 'POST' });
        if (!res.ok) throw new Error('Test failed');
        const data = await res.json();
        if (data.success) {
            toastSuccess(data.message || 'Gateway is reachable and responding');
        } else {
            toastError(data.message || 'Gateway test failed');
        }
    } catch (e) {
        console.error('[Telephony] Test gateway failed:', e);
        toastError('Gateway test failed: ' + e.message);
    }
}

// =============================================================================
// Rendering
// =============================================================================

function renderGateways() {
    const container = $('telephonyGatewayList');
    if (!container) return;

    if (allGateways.length === 0) {
        container.innerHTML = `
            <div class="telephony-empty">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--neutral-400)" stroke-width="1.5" style="margin-bottom: 12px;">
                    <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/>
                </svg>
                <p>No gateways configured</p>
                <p style="font-size: 12px; margin-top: 4px;">Click <strong>Auto-Discover</strong> to scan the network or <strong>+ Add Gateway</strong> to configure manually.</p>
            </div>`;
        return;
    }

    container.innerHTML = allGateways.map(gw => renderGatewayCard(gw)).join('');
}

function renderGatewayCard(gw) {
    const status = gw.status || {};
    const reachable = status.reachable !== false;
    const registered = status.sip_registered === true;

    let dotClass = 'offline';
    let statusLabel = 'Unreachable';
    if (reachable && registered) {
        dotClass = 'online';
        statusLabel = 'Online';
    } else if (reachable && !registered) {
        dotClass = 'warning';
        statusLabel = 'Not Registered';
    }

    const activeCalls = status.active_calls || 0;
    const capacity = status.capacity || gw.capacity || 2;

    // SIM slot rendering
    const simSlots = status.sim_slots || gw.sim_slots || [];
    let simHtml = '';
    if (simSlots.length > 0) {
        simHtml = `
            <div class="gateway-sim-slots">
                ${simSlots.map((sim, i) => {
                    const signal = sim.signal_strength || 0;
                    const bars = renderSignalBars(signal);
                    const carrier = escapeHtml(sim.carrier || 'Unknown');
                    const phone = escapeHtml(sim.phone_number || '--');
                    const simStatus = sim.status || 'unknown';
                    const simDot = simStatus === 'active' ? 'online' : (simStatus === 'no_signal' ? 'warning' : 'offline');
                    return `
                        <div class="sim-slot">
                            <div class="sim-slot-label">
                                <span class="gateway-status-dot ${simDot}"></span>
                                SIM ${i + 1}
                            </div>
                            <div class="sim-slot-carrier">${carrier}</div>
                            <div class="sim-slot-phone">${phone}</div>
                            <div class="sim-slot-signal">${bars} <span class="sim-signal-pct">${signal}%</span></div>
                        </div>`;
                }).join('')}
            </div>`;
    }

    // Build phone numbers from SIM slots (auto-detected, not user-editable)
    const simPhones = (status.sim_slots || [])
        .filter(s => s.phone_number)
        .map(s => s.phone_number);
    let phoneHtml = '';
    if (simPhones.length > 0) {
        phoneHtml = `<div class="gateway-phone-numbers">${simPhones.map(p => escapeHtml(p)).join(', ')}</div>`;
    } else {
        phoneHtml = `<div class="gateway-phone-numbers gateway-no-sims">No SIMs detected</div>`;
    }

    return `
        <div class="gateway-card" data-gateway-id="${gw.id}">
            <div class="gateway-header">
                <div class="gateway-info">
                    <span class="gateway-status-dot ${dotClass}"></span>
                    <span class="gateway-name">${escapeHtml(gw.name || 'Unnamed Gateway')}</span>
                    <a class="gateway-ip" href="http://${escapeHtml(gw.ip || '')}" target="_blank" title="Open TG200 web GUI">${escapeHtml(gw.ip || '')}</a>
                </div>
                <div class="gateway-stats">
                    <span class="gateway-stat" title="Active calls / Capacity">${activeCalls} / ${capacity} calls</span>
                    <span class="gateway-status-label gateway-status-${dotClass}">${statusLabel}</span>
                </div>
            </div>
            ${phoneHtml}
            ${simHtml}
            <div class="gateway-actions">
                <button class="btn gw-action-btn" data-action="wizard" data-gw-id="${gw.id}" data-gw-name="${escapeHtml(gw.name || '')}">Setup</button>
                <button class="btn gw-action-btn" data-action="test" data-gw-id="${gw.id}">Test</button>
                <button class="btn gw-action-btn" data-action="edit" data-gw-id="${gw.id}">Edit</button>
                <button class="btn gw-action-btn gw-action-remove" data-action="remove" data-gw-id="${gw.id}">Remove</button>
            </div>
        </div>`;
}

function renderDiscoveredGateways(discovered) {
    const container = $('telephonyGatewayList');
    if (!container) return;

    // Filter out already-configured gateways (backend discovery returns `ip`).
    const existingIPs = new Set(allGateways.map(g => g.ip));
    const newGateways = discovered.filter(d => !existingIPs.has(d.ip));

    if (newGateways.length === 0) {
        toast('All discovered gateways are already configured');
        return;
    }

    const discoveredHtml = newGateways.map(d => `
        <div class="gateway-card gateway-card-discovered" data-discovered-ip="${escapeHtml(d.ip)}">
            <div class="gateway-header">
                <div class="gateway-info">
                    <span class="gateway-status-dot warning"></span>
                    <span class="gateway-name">${escapeHtml(d.model || 'Yeastar TG200')}</span>
                    <span class="gateway-ip">${escapeHtml(d.ip)}</span>
                </div>
                <span class="gateway-discovered-badge">Discovered</span>
            </div>
            <div class="gateway-actions">
                <button class="btn btn-confirm gw-action-btn" data-action="add-discovered"
                    data-ip="${escapeHtml(d.ip)}"
                    data-model="${escapeHtml(d.model || 'TG200')}">+ Add This Gateway</button>
            </div>
        </div>
    `).join('');

    // Prepend discovered gateways before existing ones
    container.insertAdjacentHTML('afterbegin', discoveredHtml);
}

function renderSignalBars(percentage) {
    const totalBars = 5;
    const filledBars = Math.round((percentage / 100) * totalBars);
    let html = '<span class="signal-bars">';
    for (let i = 0; i < totalBars; i++) {
        const height = 4 + (i * 3); // 4px, 7px, 10px, 13px, 16px
        const filled = i < filledBars;
        html += `<span class="signal-bar ${filled ? 'signal-bar-filled' : ''}" style="height: ${height}px;"></span>`;
    }
    html += '</span>';
    return html;
}

// =============================================================================
// Add / Edit Form
// =============================================================================

function showAddForm(prefill = {}) {
    const form = $('telephonyAddForm');
    if (!form) return;

    form.style.display = 'block';
    form.innerHTML = buildFormHtml(prefill);

    // Focus first input
    const nameInput = form.querySelector('#gwFormName');
    if (nameInput) nameInput.focus();
}

function showEditForm(gwId) {
    const gw = allGateways.find(g => g.id === gwId);
    if (!gw) return;

    const form = $('telephonyAddForm');
    if (!form) return;

    form.style.display = 'block';
    form.innerHTML = buildFormHtml({
        id: gw.id,
        name: gw.name || '',
        ip: gw.ip || '',
        model: gw.model || 'TG200',
        sip_port: gw.sip_port || 5060,
        http_port: gw.http_port || 80,
        codec: gw.codec || 'g711',
        http_user: (gw.http && gw.http.user) || '',
        ami_user: (gw.ami && gw.ami.user) || '',
        // Secrets are NEVER pre-filled (GET only returns has_password/has_secret).
        ports: gw.ports || [],
        simSlots: (gw.status && gw.status.sim_slots) || [],
        isEdit: true
    });

    const nameInput = form.querySelector('#gwFormName');
    if (nameInput) nameInput.focus();
}

function hideAddForm() {
    const form = $('telephonyAddForm');
    if (form) {
        form.style.display = 'none';
        form.innerHTML = '';
    }
}

function buildFormHtml(prefill = {}) {
    const isEdit = prefill.isEdit || false;
    const title = isEdit ? 'Edit Gateway' : 'Add Gateway';
    const model = prefill.model || 'TG200';

    const modelOptions = Object.keys(MODEL_PORTS).map(m =>
        `<option value="${m}" ${model === m ? 'selected' : ''}>${m} (${MODEL_PORTS[m]} ${MODEL_PORTS[m] === 1 ? 'line' : 'lines'})</option>`
    ).join('');

    return `
        <h4 class="gateway-form-title">${title}</h4>
        ${prefill.id ? `<input type="hidden" id="gwFormId" value="${prefill.id}">` : ''}
        <div class="gateway-form-row">
            <div>
                <label for="gwFormName">Name</label>
                <input type="text" id="gwFormName" placeholder="e.g. Office TG200" value="${escapeHtml(prefill.name || '')}">
            </div>
            <div>
                <label for="gwFormIP">IP Address</label>
                <input type="text" id="gwFormIP" placeholder="e.g. 192.168.1.100" value="${escapeHtml(prefill.ip || '')}">
            </div>
        </div>
        <div class="gateway-form-row">
            <div>
                <label for="gwFormModel">Model</label>
                <select id="gwFormModel">${modelOptions}</select>
            </div>
            <div>
                <label for="gwFormPort">SIP Port</label>
                <input type="number" id="gwFormPort" placeholder="5060" value="${prefill.sip_port || 5060}" min="1" max="65535">
            </div>
        </div>
        <div class="gateway-form-row">
            <div>
                <label for="gwFormHttpPort">HTTP Port</label>
                <input type="number" id="gwFormHttpPort" placeholder="80" value="${prefill.http_port || 80}" min="1" max="65535">
            </div>
            <div>
                <label for="gwFormCodec">Codec Preference</label>
                <select id="gwFormCodec">
                    <option value="g722" ${prefill.codec === 'g722' ? 'selected' : ''}>G.722 HD</option>
                    <option value="g711" ${prefill.codec !== 'g722' ? 'selected' : ''}>G.711 (ulaw/alaw)</option>
                </select>
            </div>
        </div>
        <div class="gateway-form-section-label">TG web GUI (HTTP)</div>
        <div class="gateway-form-row">
            <div>
                <label for="gwFormHttpUser">HTTP Username</label>
                <input type="text" id="gwFormHttpUser" placeholder="admin" value="${escapeHtml(prefill.http_user || '')}">
            </div>
            <div>
                <label for="gwFormHttpPass">HTTP Password</label>
                <input type="password" id="gwFormHttpPass" placeholder="${isEdit ? '(unchanged)' : 'password'}" value="" autocomplete="new-password">
            </div>
        </div>
        <div class="gateway-form-section-label">Asterisk Manager Interface (AMI)</div>
        <div class="gateway-form-row">
            <div>
                <label for="gwFormAmiUser">AMI Username</label>
                <input type="text" id="gwFormAmiUser" placeholder="blackbox" value="${escapeHtml(prefill.ami_user || '')}">
            </div>
            <div>
                <label for="gwFormAmiSecret">AMI Secret</label>
                <input type="password" id="gwFormAmiSecret" placeholder="${isEdit ? '(unchanged)' : 'secret'}" value="" autocomplete="new-password">
            </div>
        </div>
        ${buildPortsTableHtml(prefill)}
        <div class="gateway-form-actions">
            <button class="btn" id="gwFormCancel">Cancel</button>
            <button class="btn btn-confirm" id="gwFormSave">${isEdit ? 'Update' : 'Add Gateway'}</button>
        </div>`;
}

function operatorOptionsHtml(selected) {
    const opts = ['<option value="">— none —</option>'];
    for (const op of _operators) {
        opts.push(`<option value="${escapeHtml(op)}" ${op === selected ? 'selected' : ''}>${escapeHtml(op)}</option>`);
    }
    return opts.join('');
}

function buildPortsTableHtml(prefill) {
    const ports = prefill.ports || [];
    // On add, ports[] is empty until saved (model port-count is authoritative
    // server-side); only render the per-line table when we have lines to show.
    if (ports.length === 0) return '';

    const simBySpan = {};
    (prefill.simSlots || []).forEach(s => { simBySpan[s.span] = s; });

    const rows = ports.map(p => {
        const sim = simBySpan[p.span] || {};
        const carrier = sim.carrier ? escapeHtml(sim.carrier) : '—';
        const signal = (sim.signal !== null && sim.signal !== undefined && sim.signal !== '')
            ? `${escapeHtml(String(sim.signal))}` : '—';
        const enabledChecked = p.enabled === false ? '' : 'checked';
        return `
            <tr data-port-span="${p.span}" data-port-slot="${p.slot != null ? p.slot : ''}">
                <td class="gw-port-span">Line ${(p.slot != null ? p.slot : (p.span - 2)) + 1}<span class="gw-port-span-sub">span ${p.span}</span></td>
                <td class="gw-port-carrier">${carrier}<span class="gw-port-signal">${signal}</span></td>
                <td><input type="text" class="gw-port-phone" placeholder="+1..." value="${escapeHtml(p.phone_number || '')}"></td>
                <td><select class="gw-port-operator">${operatorOptionsHtml(p.operator || '')}</select></td>
                <td class="gw-port-enabled"><input type="checkbox" class="gw-port-enabled-cb" ${enabledChecked}></td>
            </tr>`;
    }).join('');

    return `
        <div class="gateway-form-section-label">Lines</div>
        <table class="gateway-ports-table">
            <thead>
                <tr>
                    <th>Line</th>
                    <th>Carrier / Signal</th>
                    <th>Phone Number</th>
                    <th>Operator</th>
                    <th>On</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
}

function handleFormSave() {
    const id = document.getElementById('gwFormId')?.value;
    const name = document.getElementById('gwFormName')?.value?.trim();
    const ip = document.getElementById('gwFormIP')?.value?.trim();
    const model = document.getElementById('gwFormModel')?.value || 'TG200';
    const sipPort = parseInt(document.getElementById('gwFormPort')?.value) || 5060;
    const httpPort = parseInt(document.getElementById('gwFormHttpPort')?.value) || 80;
    const codec = document.getElementById('gwFormCodec')?.value || 'g711';
    const httpUser = document.getElementById('gwFormHttpUser')?.value?.trim();
    const httpPass = document.getElementById('gwFormHttpPass')?.value;
    const amiUser = document.getElementById('gwFormAmiUser')?.value?.trim();
    const amiSecret = document.getElementById('gwFormAmiSecret')?.value;

    if (!name) {
        toastError('Gateway name is required');
        document.getElementById('gwFormName')?.focus();
        return;
    }
    if (!ip) {
        toastError('IP address is required');
        document.getElementById('gwFormIP')?.focus();
        return;
    }

    const config = {
        name,
        ip,
        model,
        sip_port: sipPort,
        http_port: httpPort,
        codec,
        http_user: httpUser || null,
        ami_user: amiUser || null,
        operator: getOperator() || 'Brandon'
    };

    // Send-on-change: only include secrets when actually entered.
    if (httpPass) config.http_password = httpPass;
    if (amiSecret) config.ami_secret = amiSecret;

    // Collect per-line config from the table (present on edit / after save).
    const portRows = document.querySelectorAll('.gateway-ports-table tbody tr');
    if (portRows.length > 0) {
        const ports = [];
        portRows.forEach(tr => {
            const span = parseInt(tr.dataset.portSpan);
            const slotRaw = tr.dataset.portSlot;
            const row = {
                span: Number.isNaN(span) ? null : span,
                phone_number: tr.querySelector('.gw-port-phone')?.value?.trim() || '',
                operator: tr.querySelector('.gw-port-operator')?.value || '',
                enabled: tr.querySelector('.gw-port-enabled-cb')?.checked !== false,
            };
            if (slotRaw !== '' && slotRaw !== undefined) {
                const slot = parseInt(slotRaw);
                if (!Number.isNaN(slot)) row.slot = slot;
            }
            ports.push(row);
        });
        config.ports = ports;
    }

    if (id) {
        updateGateway(id, config);
    } else {
        addGateway(config);
    }
}

// =============================================================================
// Event Delegation
// =============================================================================

function handleGatewayAction(e) {
    const btn = e.target.closest('.gw-action-btn');
    if (!btn) return;

    const action = btn.dataset.action;
    const gwId = btn.dataset.gwId;

    switch (action) {
        case 'wizard':
            openWizard(gwId, btn.dataset.gwName || '');
            break;
        case 'test':
            testGateway(gwId);
            break;
        case 'edit':
            showEditForm(gwId);
            break;
        case 'remove':
            removeGateway(gwId);
            break;
        case 'add-discovered':
            showAddForm({
                name: btn.dataset.model || 'TG200',
                ip: btn.dataset.ip || '',
                model: btn.dataset.model || 'TG200'
            });
            break;
    }
}

function handleFormAction(e) {
    const target = e.target;
    if (target.id === 'gwFormSave') {
        handleFormSave();
    } else if (target.id === 'gwFormCancel') {
        hideAddForm();
    }
}

// =============================================================================
// Polling
// =============================================================================

function _startPolling() {
    _stopPolling();
    _pollInterval = setInterval(fetchGateways, 30000);
}

function _stopPolling() {
    if (_pollInterval) {
        clearInterval(_pollInterval);
        _pollInterval = null;
    }
}

// =============================================================================
// Utility
// =============================================================================

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// =============================================================================
// Init & Export
// =============================================================================

export function initTelephonyManager() {
    // Open modal
    const btnOpen = $('btnTelephonyManager');
    if (btnOpen) {
        btnOpen.addEventListener('click', async () => {
            const modal = $('telephonyManagerModal');
            if (modal) modal.classList.remove('hide');
            await Promise.all([fetchGateways(), fetchOperators()]);
            _startPolling();
        });
    }

    // Close modal
    const btnClose = $('btnCloseTelephony');
    if (btnClose) {
        btnClose.addEventListener('click', () => {
            const modal = $('telephonyManagerModal');
            if (modal) modal.classList.add('hide');
            _stopPolling();
            hideAddForm();
        });
    }

    // Auto-discover
    const btnDiscover = $('btnDiscoverGateways');
    if (btnDiscover) {
        btnDiscover.addEventListener('click', discoverGateways);
    }

    // Add gateway button
    const btnAdd = $('btnAddGateway');
    if (btnAdd) {
        btnAdd.addEventListener('click', () => showAddForm());
    }

    // Event delegation on gateway list (test, edit, remove, add-discovered)
    const gwList = $('telephonyGatewayList');
    if (gwList) {
        gwList.addEventListener('click', handleGatewayAction);
    }

    // Event delegation on add form (save, cancel)
    const addForm = $('telephonyAddForm');
    if (addForm) {
        addForm.addEventListener('click', handleFormAction);
    }

    // Click outside modal to close
    const modal = $('telephonyManagerModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                modal.classList.add('hide');
                _stopPolling();
                hideAddForm();
            }
        });
    }
}
