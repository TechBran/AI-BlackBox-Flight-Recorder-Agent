/**
 * device-manager.js
 * Device Manager — browse, pair, connect, and control mesh devices
 */

import { $, toast, toastSuccess, toastError } from './core-utils.js';

// =============================================================================
// State
// =============================================================================

let allDevices = [];
let adbConnected = [];
let currentSearch = '';
let currentTypeFilter = 'all';
let _pollInterval = null;

// =============================================================================
// API Functions
// =============================================================================

async function fetchDevices() {
    const res = await fetch('/devices/');
    if (!res.ok) throw new Error('Failed to fetch devices');
    const data = await res.json();
    return data.devices || [];
}

async function syncTailscale() {
    const res = await fetch('/devices/sync-tailscale', { method: 'POST' });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Sync failed');
    }
    return await res.json();
}

async function fetchAdbConnected() {
    const res = await fetch('/adb/devices');
    if (!res.ok) return [];
    const data = await res.json();
    return data.devices || [];
}

async function connectDevice(deviceId) {
    const res = await fetch(`/adb/connect/${deviceId}`, { method: 'POST' });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Connection failed');
    }
    return await res.json();
}

async function disconnectDevice(deviceId) {
    const res = await fetch(`/adb/disconnect/${deviceId}`, { method: 'POST' });
    if (!res.ok) throw new Error('Disconnect failed');
    return await res.json();
}

async function pairDevice(deviceId, pairingPort, pairingCode) {
    const res = await fetch('/adb/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            device_id: deviceId,
            pairing_port: parseInt(pairingPort),
            pairing_code: pairingCode
        })
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Pairing failed');
    }
    return await res.json();
}

async function smartPairDevice(deviceId, pairingPort, pairingCode) {
    const res = await fetch('/adb/smart-pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            device_id: deviceId,
            pairing_port: parseInt(pairingPort),
            pairing_code: pairingCode
        })
    });
    return await res.json();
}

async function submitFallbackPort(deviceId, connectionPort) {
    const res = await fetch('/adb/smart-pair/set-port', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            device_id: deviceId,
            connection_port: parseInt(connectionPort)
        })
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Connection failed');
    }
    return await res.json();
}

async function pairFromDevice(tailscaleIp, pairingPort, pairingCode) {
    const res = await fetch('/adb/pair-from-device', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            tailscale_ip: tailscaleIp,
            pairing_port: parseInt(pairingPort),
            pairing_code: pairingCode
        })
    });
    return await res.json();
}

async function updateDevicePort(deviceId, adbPort) {
    const res = await fetch(`/devices/${deviceId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adb_port: parseInt(adbPort) })
    });
    if (!res.ok) throw new Error('Failed to update device port');
    return await res.json();
}

async function removeDevice(deviceId) {
    const res = await fetch(`/devices/${deviceId}`, { method: 'DELETE' });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Remove failed');
    }
    return await res.json();
}

async function checkHealth(deviceId) {
    const res = await fetch(`/devices/${deviceId}/health`);
    if (!res.ok) throw new Error('Health check failed');
    return await res.json();
}

async function fetchScreenshot(deviceId) {
    const res = await fetch(`/adb/screenshot/${deviceId}`);
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Screenshot failed');
    }
    return await res.json();
}

async function fetchSubscriptions() {
    const res = await fetch('/notifications/subscriptions');
    if (!res.ok) throw new Error('Failed to fetch subscriptions');
    // Admin map: { device_id: { all, operators, tailnet_name, device_kind, display_name, updated_at } }
    return await res.json();
}

// =============================================================================
// Device List Rendering
// =============================================================================

async function refreshDeviceList() {
    try {
        const [devices, adbDevs] = await Promise.all([
            fetchDevices(),
            fetchAdbConnected()
        ]);
        allDevices = devices;
        adbConnected = adbDevs;
        renderFilteredDevices();
    } catch (e) {
        console.error('[DeviceManager] Refresh failed:', e);
    }
}

function renderFilteredDevices() {
    let devices = allDevices;

    // Filter by type
    if (currentTypeFilter !== 'all') {
        devices = devices.filter(d => d.device_type === currentTypeFilter);
    }

    // Filter by search
    if (currentSearch) {
        const q = currentSearch.toLowerCase();
        devices = devices.filter(d =>
            d.name.toLowerCase().includes(q) ||
            d.id.toLowerCase().includes(q) ||
            (d.description || '').toLowerCase().includes(q) ||
            (d.tailscale_ip || '').includes(q)
        );
    }

    renderDeviceList(devices);
}

function isAdbConnected(device) {
    const connStr = `${device.tailscale_ip}:${device.adb_port || 5555}`;
    return adbConnected.some(a => a.serial === connStr && a.state === 'device');
}

function getDeviceIcon(deviceType) {
    switch (deviceType) {
        case 'android': return '📱';
        case 'linux': return '🐧';
        case 'windows': return '🪟';
        case 'macos': return '🍎';
        default: return '💻';
    }
}

function renderDeviceList(devices) {
    const container = $('dmDeviceList');
    if (!container) return;

    if (devices.length === 0) {
        container.innerHTML = `
            <div class="dm-empty-state">
                <p>No devices found</p>
                <p class="dm-empty-hint">Click "Sync Tailscale" to discover devices on your mesh network</p>
            </div>`;
        return;
    }

    container.innerHTML = devices.map(device => {
        const connected = isAdbConnected(device);
        const status = device.status || 'unknown';
        const isAndroid = device.protocol === 'adb';

        // Determine card accent class
        let accentClass = `dm-status-${status}`;
        if (connected) accentClass = 'dm-status-adb-connected';

        // Build metadata items
        const metaItems = [];
        metaItems.push(`<span class="dm-meta-item"><strong>IP:</strong> ${escapeHtml(device.tailscale_ip)}</span>`);
        if (device.device_type) {
            metaItems.push(`<span class="dm-meta-item"><strong>Type:</strong> ${escapeHtml(device.device_type)}</span>`);
        }
        if (device.protocol) {
            metaItems.push(`<span class="dm-meta-item"><strong>Protocol:</strong> ${escapeHtml(device.protocol.toUpperCase())}</span>`);
        }
        if (isAndroid && device.adb_port) {
            metaItems.push(`<span class="dm-meta-item"><strong>ADB Port:</strong> ${device.adb_port}</span>`);
        }
        if (device.metadata) {
            if (device.metadata.model) {
                metaItems.push(`<span class="dm-meta-item"><strong>Model:</strong> ${escapeHtml(device.metadata.model)}</span>`);
            }
            if (device.metadata.android_version) {
                metaItems.push(`<span class="dm-meta-item"><strong>Android:</strong> ${escapeHtml(device.metadata.android_version)}</span>`);
            }
            if (device.metadata.screen_size) {
                metaItems.push(`<span class="dm-meta-item"><strong>Screen:</strong> ${escapeHtml(device.metadata.screen_size)}</span>`);
            }
        }

        // Build action buttons
        const actions = [];
        if (isAndroid) {
            if (connected) {
                actions.push(`<button class="dm-action-btn dm-btn-disconnect" data-action="disconnect" data-device-id="${device.id}">Disconnect</button>`);
                actions.push(`<button class="dm-action-btn" data-action="screenshot" data-device-id="${device.id}">Screenshot</button>`);
            } else {
                actions.push(`<button class="dm-action-btn dm-btn-connect" data-action="connect" data-device-id="${device.id}">Connect</button>`);
                actions.push(`<button class="dm-action-btn dm-btn-pair" data-action="pair" data-device-id="${device.id}">Pair</button>`);
            }
        }
        actions.push(`<button class="dm-action-btn" data-action="health" data-device-id="${device.id}">Health</button>`);
        actions.push(`<button class="dm-action-btn dm-btn-remove" data-action="remove" data-device-id="${device.id}">Remove</button>`);

        return `
        <div class="dm-device-card ${accentClass}" data-device-id="${device.id}">
            <div class="dm-card-header">
                <span class="dm-device-icon">${getDeviceIcon(device.device_type)}</span>
                <span class="dm-device-name">${escapeHtml(device.name)}</span>
                ${connected ? '<span class="dm-adb-badge">ADB</span>' : ''}
                <span class="dm-status-badge ${status}">${status}</span>
            </div>
            ${device.description ? `<div class="dm-device-desc">${escapeHtml(device.description)}</div>` : ''}
            <div class="dm-card-meta">${metaItems.join('')}</div>
            <div class="dm-card-actions">${actions.join('')}</div>
        </div>`;
    }).join('');

    // Bind action buttons
    container.querySelectorAll('.dm-action-btn').forEach(btn => {
        btn.addEventListener('click', handleDeviceAction);
    });
}

// =============================================================================
// Notification Subscriptions (read-only admin view)
// =============================================================================

async function refreshSubscriptions() {
    const container = $('dmSubsList');
    if (!container) return;
    try {
        const subs = await fetchSubscriptions();
        renderSubscriptions(subs);
    } catch (e) {
        console.error('[DeviceManager] Subscriptions refresh failed:', e);
        container.innerHTML = `
            <div class="dm-empty-state">
                <p>Could not load subscriptions</p>
                <p class="dm-empty-hint">${escapeHtml(e.message)}</p>
            </div>`;
    }
}

function formatUpdatedAt(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
}

function renderSubscriptions(subs) {
    const container = $('dmSubsList');
    if (!container) return;

    const entries = Object.entries(subs || {});
    if (entries.length === 0) {
        container.innerHTML = `
            <div class="dm-empty-state">
                <p>No devices subscribed yet</p>
                <p class="dm-empty-hint">Devices self-register for operator notifications</p>
            </div>`;
        return;
    }

    container.innerHTML = entries.map(([deviceId, sub]) => {
        sub = sub || {};
        const name = sub.display_name || deviceId;
        const kind = sub.device_kind ? ` (${escapeHtml(sub.device_kind)})` : '';

        // Operators: "All operators" when all=true; named list; "(none)" when empty
        let opsHtml;
        if (sub.all) {
            opsHtml = '<span class="dm-sub-ops-all">All operators</span>';
        } else {
            const ops = Array.isArray(sub.operators) ? sub.operators.filter(Boolean) : [];
            opsHtml = ops.length
                ? ops.map(o => `<span class="dm-sub-op-tag">${escapeHtml(String(o))}</span>`).join('')
                : '<span class="dm-sub-ops-none">\u2014 (none)</span>';
        }

        const meta = [];
        if (sub.tailnet_name) {
            meta.push(`<span class="dm-sub-meta-item">${escapeHtml(sub.tailnet_name)}</span>`);
        }
        const updated = formatUpdatedAt(sub.updated_at);
        if (updated) {
            meta.push(`<span class="dm-sub-meta-item">Updated ${escapeHtml(updated)}</span>`);
        }

        return `
        <div class="dm-sub-row">
            <div class="dm-sub-row-head">
                <span class="dm-sub-name">${escapeHtml(name)}${kind}</span>
                <span class="dm-sub-ops">${opsHtml}</span>
            </div>
            ${meta.length ? `<div class="dm-sub-meta">${meta.join('')}</div>` : ''}
        </div>`;
    }).join('');
}

// =============================================================================
// Action Handlers
// =============================================================================

async function handleDeviceAction(e) {
    const btn = e.currentTarget;
    const action = btn.dataset.action;
    const deviceId = btn.dataset.deviceId;

    try {
        switch (action) {
            case 'connect': {
                // Check if device still has default port 5555 — likely wrong for wireless debugging
                const dev = allDevices.find(d => d.id === deviceId);
                if (dev && dev.adb_port === 5555) {
                    const port = prompt(
                        'This device uses the default port 5555 which usually won\'t work for wireless debugging.\n\n' +
                        'Enter the port shown in Settings > Developer Options > Wireless Debugging on the device:',
                        '5555'
                    );
                    if (!port) break;
                    if (port !== '5555') {
                        await updateDevicePort(deviceId, port);
                    }
                }
                toast('Connecting...');
                await connectDevice(deviceId);
                toastSuccess('Connected');
                await refreshDeviceList();
                break;
            }

            case 'disconnect':
                await disconnectDevice(deviceId);
                toast('Disconnected');
                await refreshDeviceList();
                break;

            case 'pair':
                openPairModal(deviceId);
                break;

            case 'screenshot':
                openScreenshotModal(deviceId);
                break;

            case 'health': {
                toast('Checking health...');
                const result = await checkHealth(deviceId);
                toastSuccess(`Status: ${result.status}`);
                await refreshDeviceList();
                break;
            }

            case 'remove':
                if (confirm(`Remove device "${deviceId}" from the registry?`)) {
                    await removeDevice(deviceId);
                    toast('Device removed');
                    await refreshDeviceList();
                }
                break;
        }
    } catch (err) {
        console.error(`[DeviceManager] Action '${action}' failed:`, err);
        toastError(`Failed: ${err.message}`);
    }
}

// =============================================================================
// Sync Tailscale
// =============================================================================

async function handleSyncTailscale() {
    const btn = $('btnSyncTailscale');
    if (!btn || btn.classList.contains('syncing')) return;

    btn.classList.add('syncing');
    btn.textContent = 'Syncing...';

    try {
        const result = await syncTailscale();
        const added = Object.values(result.results || {}).filter(v => v === 'added').length;
        const updated = Object.values(result.results || {}).filter(v => v === 'updated').length;
        toastSuccess(`Synced: ${added} added, ${updated} updated (${result.total_devices} total)`);
        await refreshDeviceList();
    } catch (err) {
        toastError(`Sync failed: ${err.message}`);
    } finally {
        btn.classList.remove('syncing');
        btn.textContent = 'Sync Tailscale';
    }
}

// =============================================================================
// Pair Modal — Smart Pair
// =============================================================================

const STEP_MAP = {
    reachability: 'dmStepReachability',
    pairing: 'dmStepPairing',
    port_discovery: 'dmStepPortDiscovery',
    connect: 'dmStepConnect'
};

const STEP_LABELS = {
    reachability: 'Checking reachability...',
    pairing: 'Pairing with device...',
    port_discovery: 'Discovering connection port...',
    connect: 'Connecting...'
};

const STEP_ICONS = { ok: '\u2713', fail: '\u2717', active: '\u2026', skip: '!' };

function resetProgressSteps() {
    const progress = $('dmPairProgress');
    if (progress) progress.style.display = 'none';

    Object.entries(STEP_MAP).forEach(([step, id]) => {
        const el = $(id);
        if (!el) return;
        el.className = 'dm-pair-step';
        el.querySelector('.dm-step-icon').textContent = Object.keys(STEP_MAP).indexOf(step) + 1;
        el.querySelector('.dm-step-message').textContent = STEP_LABELS[step];
    });

    // Hide error + fallback
    const errEl = $('dmPairError');
    if (errEl) { errEl.classList.remove('visible'); errEl.textContent = ''; }
    const fallback = $('dmFallbackSection');
    if (fallback) fallback.classList.remove('visible');
}

function showProgress() {
    const progress = $('dmPairProgress');
    if (progress) progress.style.display = 'flex';
}

function updateProgressStep(stepName, status, message) {
    const elId = STEP_MAP[stepName];
    if (!elId) return;
    const el = $(elId);
    if (!el) return;

    // Clear old state classes
    el.className = 'dm-pair-step';
    if (status === 'ok') el.classList.add('dm-step-ok');
    else if (status === 'fail') el.classList.add('dm-step-fail');
    else if (status === 'active') el.classList.add('dm-step-active');
    else if (status === 'skip') el.classList.add('dm-step-skip');

    el.querySelector('.dm-step-icon').textContent = STEP_ICONS[status] || (Object.keys(STEP_MAP).indexOf(stepName) + 1);
    if (message) el.querySelector('.dm-step-message').textContent = message;
}

function setActiveStep(stepName) {
    updateProgressStep(stepName, 'active', STEP_LABELS[stepName]);
}

function showPairError(msg) {
    const el = $('dmPairError');
    if (el) {
        el.textContent = msg;
        el.classList.add('visible');
    }
}

function showFallbackSection() {
    const el = $('dmFallbackSection');
    if (el) el.classList.add('visible');
}

function openPairModal(deviceId) {
    const modal = $('dmPairModal');
    if (!modal) return;

    const device = allDevices.find(d => d.id === deviceId);
    if (!device) return;

    // Clear any stale quick pair state
    delete modal.dataset.quickPairIp;

    // Reset state
    $('dmPairDeviceId').value = deviceId;
    $('dmPairTitle').textContent = `Pair: ${device.name}`;
    $('dmPairDeviceInfo').innerHTML = `<strong>${escapeHtml(device.name)}</strong> &mdash; ${escapeHtml(device.tailscale_ip)}`;
    $('dmPairingPort').value = '';
    $('dmPairingCode').value = '';
    $('dmPairingPort').classList.remove('dm-input-error');
    $('dmPairingCode').classList.remove('dm-input-error');
    if ($('dmPairingPortHint')) $('dmPairingPortHint').textContent = '';
    if ($('dmPairingCodeHint')) $('dmPairingCodeHint').textContent = '';
    if ($('dmFallbackPort')) $('dmFallbackPort').value = '';

    resetProgressSteps();

    // Re-enable submit button
    const submitBtn = $('btnSubmitPair');
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Pair & Connect'; }

    modal.classList.remove('hide');
    $('dmPairingPort').focus();
}

function validatePairFields() {
    const port = $('dmPairingPort')?.value?.trim();
    const code = $('dmPairingCode')?.value?.trim();
    let valid = true;

    // Port validation
    const portNum = parseInt(port);
    if (!port || isNaN(portNum)) {
        $('dmPairingPort')?.classList.add('dm-input-error');
        if ($('dmPairingPortHint')) $('dmPairingPortHint').textContent = 'Required';
        valid = false;
    } else if (portNum < 1 || portNum > 65535) {
        $('dmPairingPort')?.classList.add('dm-input-error');
        if ($('dmPairingPortHint')) $('dmPairingPortHint').textContent = 'Must be 1-65535';
        valid = false;
    }

    // Code validation (6 digits)
    if (!code) {
        $('dmPairingCode')?.classList.add('dm-input-error');
        if ($('dmPairingCodeHint')) $('dmPairingCodeHint').textContent = 'Required';
        valid = false;
    } else if (!/^\d{6}$/.test(code)) {
        $('dmPairingCode')?.classList.add('dm-input-error');
        if ($('dmPairingCodeHint')) $('dmPairingCodeHint').textContent = 'Must be 6 digits';
        valid = false;
    }

    return valid;
}

async function handleSubmitPair() {
    const deviceId = $('dmPairDeviceId')?.value;
    const port = $('dmPairingPort')?.value?.trim();
    const code = $('dmPairingCode')?.value?.trim();
    const quickPairIp = $('dmPairModal')?.dataset.quickPairIp;

    // Clear errors
    $('dmPairingPort')?.classList.remove('dm-input-error');
    $('dmPairingCode')?.classList.remove('dm-input-error');
    if ($('dmPairingPortHint')) $('dmPairingPortHint').textContent = '';
    if ($('dmPairingCodeHint')) $('dmPairingCodeHint').textContent = '';

    if (!validatePairFields()) return;

    // Disable button, show progress
    const submitBtn = $('btnSubmitPair');
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Pairing...'; }
    resetProgressSteps();
    showProgress();

    // Animate steps as "active" sequentially
    setActiveStep('reachability');

    try {
        // Use pair-from-device for Quick Pair (no device_id, have IP)
        const result = (!deviceId && quickPairIp)
            ? await pairFromDevice(quickPairIp, port, code)
            : await smartPairDevice(deviceId, port, code);

        // Update progress steps from server response
        if (result.steps) {
            for (const step of result.steps) {
                updateProgressStep(step.step, step.status, step.message);
            }
            // Set remaining steps to their final state
            const stepNames = Object.keys(STEP_MAP);
            const completedSteps = result.steps.map(s => s.step);
            const lastStep = completedSteps[completedSteps.length - 1];
            const lastIdx = stepNames.indexOf(lastStep);

            // Mark steps after the last completed one as untouched (if not in result)
            for (let i = lastIdx + 1; i < stepNames.length; i++) {
                if (!completedSteps.includes(stepNames[i])) {
                    // Leave as default (numbered, gray)
                }
            }
        }

        if (result.success) {
            toastSuccess('Paired and connected!');
            setTimeout(() => {
                $('dmPairModal')?.classList.add('hide');
                refreshDeviceList();
            }, 1500);
        } else if (result.needs_connection_port) {
            // Pairing succeeded but port not found — show fallback
            showFallbackSection();
            if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Paired'; }
        } else {
            // Hard failure — show error
            const lastFail = (result.steps || []).filter(s => s.status === 'fail').pop();
            showPairError(lastFail?.message || 'Pairing failed');
            if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Retry'; }
        }
    } catch (err) {
        showPairError(`Request failed: ${err.message}`);
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Retry'; }
    }
}

async function handleSubmitFallbackPort() {
    const deviceId = $('dmPairDeviceId')?.value;
    const portStr = $('dmFallbackPort')?.value?.trim();
    const port = parseInt(portStr);

    if (!portStr || isNaN(port) || port < 1 || port > 65535) {
        toastError('Enter a valid port (1-65535)');
        return;
    }

    const btn = $('btnSubmitFallbackPort');
    if (btn) { btn.disabled = true; btn.textContent = 'Connecting...'; }

    try {
        const result = await submitFallbackPort(deviceId, port);
        if (result.success) {
            toastSuccess('Connected!');
            updateProgressStep('port_discovery', 'ok', `Port ${port} (manual)`);
            updateProgressStep('connect', 'ok', 'Connected');
            setTimeout(() => {
                $('dmPairModal')?.classList.add('hide');
                refreshDeviceList();
            }, 1500);
        } else {
            toastError(result.error || 'Connection failed');
            if (btn) { btn.disabled = false; btn.textContent = 'Connect'; }
        }
    } catch (err) {
        toastError(`Failed: ${err.message}`);
        if (btn) { btn.disabled = false; btn.textContent = 'Connect'; }
    }
}

// =============================================================================
// Android App Quick Pair
// =============================================================================

function isAndroidApp() {
    return !!(window.AndroidBridge && typeof window.AndroidBridge.getDeviceTailscaleIp === 'function');
}

async function handleQuickPair() {
    try {
        const ip = window.AndroidBridge.getDeviceTailscaleIp();
        if (!ip) {
            toastError('Could not detect Tailscale IP. Is Tailscale connected?');
            return;
        }

        // Find or set device in modal
        const device = allDevices.find(d => d.tailscale_ip === ip);
        const modal = $('dmPairModal');
        if (!modal) return;

        if (device) {
            $('dmPairDeviceId').value = device.id;
            $('dmPairTitle').textContent = `Quick Pair: ${device.name}`;
            $('dmPairDeviceInfo').innerHTML = `<strong>${escapeHtml(device.name)}</strong> &mdash; ${escapeHtml(ip)}`;
        } else {
            $('dmPairDeviceId').value = '';
            $('dmPairTitle').textContent = 'Quick Pair This Device';
            $('dmPairDeviceInfo').innerHTML = `<strong>This device</strong> &mdash; ${escapeHtml(ip)}`;
        }

        $('dmPairingPort').value = '';
        $('dmPairingCode').value = '';
        resetProgressSteps();

        const submitBtn = $('btnSubmitPair');
        if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Pair & Connect'; }

        // Store IP for pair-from-device flow
        modal.dataset.quickPairIp = ip;
        modal.classList.remove('hide');

        // Open wireless debugging settings on the device
        if (typeof window.AndroidBridge.openWirelessDebuggingSettings === 'function') {
            window.AndroidBridge.openWirelessDebuggingSettings();
        }

        $('dmPairingPort').focus();
    } catch (err) {
        toastError(`Quick pair error: ${err.message}`);
    }
}

// =============================================================================
// Screenshot Modal
// =============================================================================

async function openScreenshotModal(deviceId) {
    const modal = $('dmScreenshotModal');
    if (!modal) return;

    const device = allDevices.find(d => d.id === deviceId);
    $('dmScreenshotTitle').textContent = device ? `Screenshot: ${device.name}` : 'Screenshot';
    $('dmScreenshotContent').innerHTML = '<div class="dm-screenshot-loading">Capturing screenshot...</div>';
    modal.classList.remove('hide');

    try {
        const result = await fetchScreenshot(deviceId);
        const screenSize = result.screen_size ? `${result.screen_size[0]}x${result.screen_size[1]}` : '';
        $('dmScreenshotContent').innerHTML = `
            <img class="dm-screenshot-img" src="data:image/png;base64,${result.screenshot_base64}" alt="Device screenshot">
            ${screenSize ? `<div class="dm-screenshot-meta">${screenSize}</div>` : ''}
        `;
    } catch (err) {
        $('dmScreenshotContent').innerHTML = `<div class="dm-screenshot-loading">Failed: ${escapeHtml(err.message)}</div>`;
    }
}

// =============================================================================
// Utilities
// =============================================================================

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// =============================================================================
// Polling
// =============================================================================

function _startPolling() {
    _stopPolling();
    _pollInterval = setInterval(() => {
        refreshDeviceList();
        refreshSubscriptions();
    }, 10000);
}

function _stopPolling() {
    if (_pollInterval) {
        clearInterval(_pollInterval);
        _pollInterval = null;
    }
}

// =============================================================================
// Init & Export
// =============================================================================

export function initDeviceManager() {
    // Open manager modal
    const btnOpen = $('btnDeviceManager');
    if (btnOpen) {
        btnOpen.addEventListener('click', async () => {
            const modal = $('deviceManagerModal');
            if (modal) modal.classList.remove('hide');
            await refreshDeviceList();
            refreshSubscriptions();
            _startPolling();

            // Show quick pair button if in Android app
            const quickPairBtn = $('btnQuickPair');
            if (quickPairBtn && isAndroidApp()) {
                quickPairBtn.classList.add('visible');
            }
        });
    }

    // Close buttons
    const closeIds = [
        ['btnCloseDeviceManager', 'deviceManagerModal'],
        ['btnClosePairModal', 'dmPairModal'],
        ['btnCloseScreenshot', 'dmScreenshotModal'],
    ];
    closeIds.forEach(([btnId, modalId]) => {
        const btn = $(btnId);
        if (btn) {
            btn.addEventListener('click', () => {
                $(modalId)?.classList.add('hide');
                if (modalId === 'deviceManagerModal') _stopPolling();
            });
        }
    });

    // Sync Tailscale button
    const btnSync = $('btnSyncTailscale');
    if (btnSync) {
        btnSync.addEventListener('click', handleSyncTailscale);
    }

    // Pair modal actions
    const btnSubmitPair = $('btnSubmitPair');
    if (btnSubmitPair) {
        btnSubmitPair.addEventListener('click', handleSubmitPair);
    }
    const btnCancelPair = $('btnCancelPair');
    if (btnCancelPair) {
        btnCancelPair.addEventListener('click', () => $('dmPairModal')?.classList.add('hide'));
    }

    // Fallback port submit
    const btnFallback = $('btnSubmitFallbackPort');
    if (btnFallback) {
        btnFallback.addEventListener('click', handleSubmitFallbackPort);
    }

    // Quick pair button (Android app)
    const btnQuickPair = $('btnQuickPair');
    if (btnQuickPair) {
        btnQuickPair.addEventListener('click', handleQuickPair);
    }

    // Clear error highlighting on input
    ['dmPairingPort', 'dmPairingCode'].forEach(id => {
        const el = $(id);
        if (el) {
            el.addEventListener('input', () => {
                el.classList.remove('dm-input-error');
                const hintId = id + 'Hint';
                const hint = $(hintId);
                if (hint) hint.textContent = '';
            });
        }
    });

    // Search input (debounced)
    const searchInput = $('dmSearchInput');
    if (searchInput) {
        let searchTimeout;
        searchInput.addEventListener('input', () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                currentSearch = searchInput.value;
                renderFilteredDevices();
            }, 200);
        });
    }

    // Type filter
    const typeFilter = $('dmTypeFilter');
    if (typeFilter) {
        typeFilter.addEventListener('change', () => {
            currentTypeFilter = typeFilter.value;
            renderFilteredDevices();
        });
    }

    // Click outside modal to close
    ['deviceManagerModal', 'dmPairModal', 'dmScreenshotModal'].forEach(id => {
        const modal = $(id);
        if (modal) {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    modal.classList.add('hide');
                    if (id === 'deviceManagerModal') _stopPolling();
                }
            });
        }
    });
}
