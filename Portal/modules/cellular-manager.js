/**
 * cellular-manager.js
 * Cellular Internet Failover Manager — monitor, control, and debug cellular data connection
 */

import { $, toast, toastSuccess, toastError, escapeHtml, escapeAttr } from './core-utils.js';

// =============================================================================
// State
// =============================================================================

let _status = null;
let _pollInterval = null;
let _speedTestRunning = false;
let _atCommandHistory = [];

// =============================================================================
// API Functions
// =============================================================================

async function fetchStatus() {
    try {
        const res = await fetch('/internet/status');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        _status = await res.json();
        renderAll();
    } catch (e) {
        console.error('[Cellular] Status fetch failed:', e);
    }
}

async function doDeepReconnect() {
    try {
        toast('Deep reconnect — cycling radio...');
        const res = await fetch('/internet/deep-reconnect', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
            toastSuccess('Deep reconnect complete');
        } else {
            toastError(data.error || 'Deep reconnect failed');
        }
        await fetchStatus();
    } catch (e) {
        toastError('Deep reconnect failed: ' + e.message);
    }
}

async function runSpeedTest() {
    if (_speedTestRunning) return;
    _speedTestRunning = true;
    renderSpeedTest();

    try {
        // POST: the test spends cellular data, so it is never a GET.
        const res = await fetch('/internet/speed-test', { method: 'POST' });
        const data = await res.json();
        renderSpeedTestResults(data);
    } catch (e) {
        toastError('Speed test failed: ' + e.message);
    } finally {
        _speedTestRunning = false;
        renderSpeedTest();
    }
}

async function sendAtCommand(cmd) {
    if (!cmd) return;
    try {
        const res = await fetch('/internet/at-command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd })
        });
        const data = await res.json();
        _atCommandHistory.push({ cmd, response: data.response || data.error || 'No response' });
        renderAtConsole();
    } catch (e) {
        _atCommandHistory.push({ cmd, response: 'Error: ' + e.message });
        renderAtConsole();
    }
}

async function fetchHistory() {
    try {
        const res = await fetch('/internet/history?limit=50');
        const data = await res.json();
        renderEventLog(data.events || []);
    } catch (e) {
        console.error('[Cellular] History fetch failed:', e);
    }
}

// =============================================================================
// Render Functions
// =============================================================================

function renderAll() {
    if (!_status) return;
    renderStatusCard();
    renderBackoffIndicator();
    renderControls();
    renderApnSection();
    renderFailoverStatus();
    renderSpeedTest();
}

function renderStatusCard() {
    const container = $('cellStatusCard');
    if (!container || !_status) return;

    const modem = _status.modem || {};
    const signal = _status.signal || {};
    const conn = _status.connection || {};
    const state = _status.state || 'unknown';

    // Signal strength (0-5 bars from signal_quality 0-100)
    const quality = parseInt(modem.signal_quality) || 0;
    const bars = quality > 80 ? 5 : quality > 60 ? 4 : quality > 40 ? 3 : quality > 20 ? 2 : quality > 0 ? 1 : 0;

    // Access tech — format nicely
    const techs = modem.access_technologies || [];
    const techMap = { 'lte': 'LTE', '5gnr': '5G NR', 'umts': '3G', 'gsm': '2G', 'lte-cat-m': 'Cat-M', 'lte-nb-iot': 'NB-IoT' };
    const techStr = techs.length ? techs.map(t => techMap[t] || t.toUpperCase()).join(' + ') : '--';

    // Tag shows access tech (bands from mmcli are "allowed" not "serving")
    const tagStr = techStr;

    // IP
    const ip = conn.ip || '--';

    // Uptime
    let uptimeStr = '--';
    if (_status.uptime_seconds) {
        const s = Math.floor(_status.uptime_seconds);
        if (s < 60) uptimeStr = `${s}s`;
        else if (s < 3600) uptimeStr = `${Math.floor(s/60)}m ${s%60}s`;
        else uptimeStr = `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
    }

    // LTE signal details (filter out bogus -32768 values from mmcli)
    const lte = signal.lte || {};
    const validSig = (v) => v && v !== '--' && parseFloat(v) > -32000;
    const signalDetail = validSig(lte.rsrp)
        ? `RSRP ${escapeHtml(lte.rsrp)} dBm | RSRQ ${validSig(lte.rsrq) ? escapeHtml(lte.rsrq) : '--'} dB | SNR ${validSig(lte.snr) ? escapeHtml(lte.snr) : '--'} dB`
        : `Quality: ${quality}%`;

    // Status label
    const isInitializing = _status.modem_initializing;
    const verified = _status.internet_verified;
    let statusLabel = state;
    if (isInitializing) statusLabel = 'initializing';
    else if (state === 'connected' && !verified) statusLabel = 'connected (unverified)';
    else if (state === 'connected' && verified) statusLabel = 'connected';

    const dotClass = isInitializing ? 'connecting' : state;

    // Every value below comes from the modem / network (a carrier name is
    // broadcast over the air by whatever cell the modem hears) — escape them all.
    container.innerHTML = `
        <div class="cell-signal-bars strength-${bars}">
            <div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div><div class="bar"></div>
        </div>
        <div class="cell-status-info">
            <div class="cell-status-row">
                <span class="cell-operator-name">${escapeHtml(modem.operator_name || 'No Carrier')}</span>
                <span class="cell-band-tag">${escapeHtml(tagStr)}</span>
                <span class="cell-access-tech" style="font-size:11px;color:var(--neutral-600)">${escapeHtml(statusLabel)}</span>
            </div>
            <div class="cell-status-detail">
                <code>${escapeHtml(ip)}</code> &middot; ${signalDetail} &middot; Up: ${uptimeStr}
            </div>
        </div>
        <div class="cell-status-dot ${escapeAttr(dotClass)}"></div>
    `;
}

function renderBackoffIndicator() {
    const container = $('cellBackoffBar');
    if (!container || !_status) return;

    const bo = _status.backoff || {};
    if (!bo.active && !bo.throttle_detected) {
        container.style.display = 'none';
        return;
    }
    container.style.display = 'flex';

    const isThrottle = bo.throttle_detected;
    container.className = 'cell-backoff-bar ' + (isThrottle ? 'danger' : 'warning');

    const remaining = Math.ceil(bo.remaining || 0);
    const minutes = Math.floor(remaining / 60);
    const seconds = remaining % 60;
    const timeStr = minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;

    container.innerHTML = `
        <span class="backoff-label">${isThrottle ? 'PDN THROTTLE' : 'BACKOFF'}</span>
        <span class="backoff-detail">
            ${isThrottle ? 'Carrier is throttling connections. Waiting before retry.' : `${escapeHtml(bo.consecutive_failures)} consecutive failures. Waiting before retry.`}
        </span>
        <span class="backoff-countdown">${bo.active ? timeStr : 'ready'}</span>
    `;
}

function renderControls() {
    const container = $('cellControls');
    if (!container || !_status) return;

    const state = _status.state || 'unknown';
    const isConnected = state === 'connected';
    const isConnecting = state === 'connecting';

    container.innerHTML = `
        <button class="btn" data-action="deep-reconnect" ${isConnecting ? 'disabled' : ''}>
            Deep Reconnect (Radio Cycle)
        </button>
        <button class="btn" data-action="speed-test">Speed Test</button>
    `;
}

function renderApnSection() {
    const container = $('cellApnSection');
    if (!container || !_status) return;

    const conn = _status.connection || {};
    const currentApn = conn.apn || '--';

    container.innerHTML = `
        <div class="cell-section-title">APN Configuration</div>
        <div class="cell-apn-row">
            <input type="text" id="cellApnInput" value="${escapeAttr(currentApn !== '--' ? currentApn : '')}" placeholder="e.g. vzwinternet">
            <button class="btn" data-action="set-apn">Set</button>
            <button class="btn" data-action="auto-apn">Auto-Detect</button>
        </div>
    `;
}

function renderFailoverStatus() {
    const container = $('cellFailoverSection');
    if (!container || !_status) return;

    const routing = _status.routing || {};
    const routes = routing.routes || [];
    const conn = _status.connection || {};

    let rowsHtml = '';
    for (const r of routes) {
        const isCellular = (r.interface || '').startsWith('wwan');
        const cls = isCellular ? 'cell-route-active' : '';
        rowsHtml += `<tr>
            <td class="${cls}">${escapeHtml(r.interface || '--')}</td>
            <td class="${cls}">${escapeHtml(r.gateway || '--')}</td>
            <td class="${cls}">${escapeHtml(r.metric || '--')}</td>
        </tr>`;
    }

    if (!rowsHtml) {
        rowsHtml = '<tr><td colspan="3" style="color:var(--neutral-500)">No default routes</td></tr>';
    }

    container.innerHTML = `
        <div class="cell-section-title">Failover Routing</div>
        <table class="cell-route-table">
            <tr><th>Interface</th><th>Gateway</th><th>Metric</th></tr>
            ${rowsHtml}
        </table>
        <div class="cell-status-detail" style="margin-top:8px">
            Primary: <strong>${escapeHtml(routing.primary_interface || '--')}</strong>
            &middot; Cellular metric: ${escapeHtml(conn.metric || '--')}
        </div>
    `;
}

function renderSpeedTest() {
    const container = $('cellSpeedSection');
    if (!container) return;

    const btn = container.querySelector('[data-action="speed-test"]');
    if (btn) {
        btn.disabled = _speedTestRunning;
        btn.innerHTML = _speedTestRunning ? '<span class="cell-loading-spinner"></span> Testing...' : 'Run Speed Test';
    }
}

function renderSpeedTestResults(data) {
    const container = $('cellSpeedResults');
    if (!container) return;

    container.innerHTML = `
        <div class="cell-speed-metric">
            <div class="cell-speed-value">${data.download_mbps != null ? escapeHtml(data.download_mbps) : '--'}</div>
            <div class="cell-speed-unit">Mbps Down</div>
        </div>
        <div class="cell-speed-metric">
            <div class="cell-speed-value">${data.upload_mbps != null ? escapeHtml(data.upload_mbps) : '--'}</div>
            <div class="cell-speed-unit">Mbps Up</div>
        </div>
        <div class="cell-speed-metric">
            <div class="cell-speed-value">${data.latency_ms != null ? escapeHtml(data.latency_ms) : '--'}</div>
            <div class="cell-speed-unit">ms Latency</div>
        </div>
    `;
}

function renderAtConsole() {
    const output = $('cellAtOutput');
    if (!output) return;

    output.textContent = _atCommandHistory.map(h => `> ${h.cmd}\n${h.response}`).join('\n\n');
    output.scrollTop = output.scrollHeight;
}

function renderEventLog(events) {
    const container = $('cellEventLog');
    if (!container) return;

    if (!events.length) {
        container.innerHTML = '<div class="cell-event-log-title">Event Log</div><div style="color:var(--neutral-500);font-size:12px">No events yet</div>';
        return;
    }

    let html = '<div class="cell-event-log-title">Event Log</div>';
    for (const ev of events) {
        const d = new Date(ev.time * 1000);
        const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const typeClass = String(ev.type || '').replace(/_/g, '-');
        html += `
            <div class="cell-event-item">
                <span class="cell-event-time">${escapeHtml(timeStr)}</span>
                <span class="cell-event-type ${escapeAttr(typeClass)}">${escapeHtml(ev.type)}</span>
                <span class="cell-event-detail">${escapeHtml(ev.detail || '')}</span>
            </div>`;
    }
    container.innerHTML = html;
}

// =============================================================================
// Action Handler (event delegation)
// =============================================================================

function handleActions(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;

    const action = btn.dataset.action;

    switch (action) {
        case 'deep-reconnect':
            doDeepReconnect();
            break;
        case 'speed-test':
            runSpeedTest();
            break;
        case 'send-at': {
            const input = $('cellAtInput');
            if (input && input.value.trim()) {
                sendAtCommand(input.value.trim());
                input.value = '';
            }
            break;
        }
    }
}

// =============================================================================
// Polling
// =============================================================================

function startPolling() {
    if (_pollInterval) return;
    fetchStatus();
    fetchHistory();
    _pollInterval = setInterval(() => {
        fetchStatus();
    }, 5000);
    // Refresh event log every 15s
    setInterval(() => {
        if (_pollInterval) fetchHistory();
    }, 15000);
}

function stopPolling() {
    if (_pollInterval) {
        clearInterval(_pollInterval);
        _pollInterval = null;
    }
}

// =============================================================================
// Init
// =============================================================================

export function initCellularManager() {
    // Open modal
    const btnOpen = $('btnCellularManager');
    if (btnOpen) {
        btnOpen.addEventListener('click', () => {
            const modal = $('cellularManagerModal');
            if (modal) modal.classList.remove('hide');
            startPolling();
        });
    }

    // Close modal
    const btnClose = $('btnCloseCellular');
    if (btnClose) {
        btnClose.addEventListener('click', () => {
            const modal = $('cellularManagerModal');
            if (modal) modal.classList.add('hide');
            stopPolling();
        });
    }

    // Event delegation on modal body
    const body = document.querySelector('.cellular-manager-body');
    if (body) {
        body.addEventListener('click', handleActions);
    }

    // AT console enter key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target.id === 'cellAtInput') {
            e.preventDefault();
            const cmd = e.target.value.trim();
            if (cmd) {
                sendAtCommand(cmd);
                e.target.value = '';
            }
        }
    });

    // Click outside modal to close
    const modal = $('cellularManagerModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                modal.classList.add('hide');
                stopPolling();
            }
        });
    }
}
