/**
 * cu-drawer.js
 * Computer Use control drawer — extends the operator bubble with
 * session management, tool tracking, E-stop, and prompt queue.
 *
 * Shows as a third row inside .operator-menu-bubble only when
 * the computer-use provider is selected.
 *
 * Session IDs are stored per-operator in localStorage AND synced
 * to server-side operator preferences for cross-device persistence.
 */

import { stopCUTask, newCUSession } from './chat-send.js';
import { toast } from './core-utils.js';
import { getOperator } from './state-management.js';

// =============================================================================
// Per-Operator Session Storage
// =============================================================================

/**
 * Get the localStorage key for a specific operator's CU session ID.
 * @param {string} [operator] - Operator name (defaults to current)
 * @returns {string} localStorage key
 */
function _sessionKey(operator) {
    const op = operator || getOperator() || 'default';
    return `bb_cu_session_id_${op}`;
}

/**
 * Get the stored CU session ID for the current operator.
 * @returns {string|null}
 */
export function getCUSessionId() {
    return localStorage.getItem(_sessionKey()) || null;
}

/**
 * Store a CU session ID for the current operator.
 * Also syncs to server-side operator preferences (fire-and-forget).
 * @param {string|null} sessionId
 */
export function setCUSessionId(sessionId) {
    const op = getOperator();
    const key = _sessionKey(op);
    if (sessionId) {
        localStorage.setItem(key, sessionId);
    } else {
        localStorage.removeItem(key);
    }
    // Sync to server preferences for cross-device persistence
    if (op) {
        fetch(`/operator/preferences/${encodeURIComponent(op)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cu_session_id: sessionId || null })
        }).catch(err => console.warn('[CU-PREFS] Failed to sync session_id to server:', err));
    }
}

/**
 * Load CU session ID from server preferences (for cross-device sync).
 * Updates localStorage if server has a value.
 * @returns {Promise<string|null>}
 */
async function _fetchSessionIdFromServer() {
    const op = getOperator();
    if (!op) return null;
    try {
        const res = await fetch(`/operator/preferences/${encodeURIComponent(op)}`);
        if (res.ok) {
            const data = await res.json();
            const serverSessionId = data.preferences?.cu_session_id;
            if (serverSessionId) {
                localStorage.setItem(_sessionKey(op), serverSessionId);
                return serverSessionId;
            }
        }
    } catch (err) {
        console.warn('[CU-PREFS] Failed to fetch session_id from server:', err);
    }
    return getCUSessionId();
}

// =============================================================================
// Per-Operator Device Storage
// =============================================================================

function _deviceKey(operator) {
    const op = operator || getOperator() || 'default';
    return `bb_cu_device_id_${op}`;
}

/**
 * Get the stored CU device ID for the current operator.
 * @returns {string} Device ID (defaults to 'blackbox')
 */
export function getCUDeviceId() {
    return localStorage.getItem(_deviceKey()) || 'blackbox';
}

/**
 * Store a CU device ID for the current operator.
 * @param {string} deviceId
 */
export function setCUDeviceId(deviceId) {
    localStorage.setItem(_deviceKey(), deviceId || 'blackbox');
}

// =============================================================================
// State
// =============================================================================

let _drawer = null;
let _currentSessionId = null;
let _currentStatus = 'idle'; // idle | running | stopped | complete
let _queueCount = 0;
let _deviceRefreshInterval = null;

// =============================================================================
// DOM Creation
// =============================================================================

function _createDrawer() {
    const drawer = document.createElement('div');
    drawer.className = 'cu-drawer';
    drawer.id = 'cuDrawer';
    drawer.innerHTML = `
        <div class="cu-drawer-row cu-drawer-tool-row">
            <span class="cu-drawer-dot"></span>
            <span class="cu-drawer-tool-label">Idle</span>
            <span class="cu-drawer-step"></span>
        </div>
        <div class="cu-drawer-row cu-drawer-device-row">
            <label class="cu-drawer-device-label">Device:</label>
            <select class="cu-drawer-device-select" id="cuDeviceSelect">
                <option value="blackbox">BlackBox (Local)</option>
            </select>
        </div>
        <div class="cu-drawer-row cu-drawer-controls">
            <button class="cu-drawer-estop" title="Emergency stop">STOP</button>
            <span class="cu-drawer-queue-badge hide"></span>
            <span class="cu-drawer-session-id"></span>
            <button class="cu-drawer-live" title="Open live view">Live</button>
            <button class="cu-drawer-new" title="New session">New</button>
        </div>
    `;
    return drawer;
}

// =============================================================================
// Device Population
// =============================================================================

/**
 * Resolve the CU backend for the currently selected model.
 * window.__cuModelBackends is populated by state-management's
 * updateModelDropdown (model id -> backend, including the "" Auto
 * entry mapped to the server default's backend). Defaults to
 * 'anthropic' when the lookup or the model id is unknown.
 */
function _modelBackend() {
    return (window.__cuModelBackends || {})[window.__model || ''] || 'anthropic';
}

/**
 * Fetch Tailscale devices and populate the device dropdown.
 * - google (Gemini CU): all devices including ADB (Android + computers)
 * - anthropic / openai: LOCAL + VNC only (computers)
 */
async function _populateDevices() {
    const select = document.getElementById('cuDeviceSelect');
    if (!select) return;

    const backend = _modelBackend();

    try {
        const res = await fetch('/devices/');
        if (!res.ok) return;
        const data = await res.json();
        const devices = data.devices || data || [];

        // Preserve current selection
        const saved = getCUDeviceId();
        const prevValue = select.value;

        // Clear and rebuild
        select.innerHTML = '';

        for (const d of devices) {
            // Filter by backend capability:
            // google (Gemini CU): all protocols (computers + Android via ADB)
            // anthropic / openai: only LOCAL + VNC (computers)
            if (backend !== 'google' && d.protocol !== 'local' && d.protocol !== 'vnc') continue;
            const opt = document.createElement('option');
            opt.value = d.id;
            // Local device is always online; show status for remote devices only
            const isLocal = d.id === 'blackbox' || d.protocol === 'local';
            const status = (isLocal || d.status === 'online') ? '' : ' (offline)';
            const typeTag = d.protocol === 'adb' ? ' [Android]' : '';
            opt.textContent = `${d.name || d.id}${typeTag}${status}`;
            if (d.status !== 'online' && !isLocal) opt.disabled = true;
            select.appendChild(opt);
        }

        // Ensure blackbox always exists as fallback
        if (!select.querySelector('option[value="blackbox"]')) {
            const fallback = document.createElement('option');
            fallback.value = 'blackbox';
            fallback.textContent = 'BlackBox (Local)';
            select.prepend(fallback);
        }

        // Restore saved selection (or previous if saved is gone)
        if (saved && select.querySelector(`option[value="${saved}"]`)) {
            select.value = saved;
        } else if (prevValue && select.querySelector(`option[value="${prevValue}"]`)) {
            select.value = prevValue;
        } else {
            select.value = 'blackbox';
        }
    } catch (err) {
        console.warn('[CU-Drawer] Failed to populate devices:', err);
    }
}

// =============================================================================
// Preflight Banner
// =============================================================================

/**
 * Fetch /cu/preflight and show a banner inside the drawer when any check
 * is non-ok. Fetched once per attach (the drawer detaches/reattaches on
 * provider switch). Fetch failures are silent — offline boxes or backends
 * without the endpoint (404) must not break the drawer.
 * @param {HTMLElement} drawer
 */
async function _fetchPreflight(drawer) {
    try {
        const res = await fetch('/cu/preflight?skip_screenshot=true');
        if (!res.ok) return;
        const data = await res.json();
        if (!data || data.status === 'ok' || !Array.isArray(data.checks)) return;
        _renderPreflightBanner(drawer, data);
    } catch (err) {
        console.warn('[CU-Drawer] Preflight check unavailable:', err);
    }
}

/**
 * Render the preflight banner above the controls row.
 * @param {HTMLElement} drawer
 * @param {{status: string, checks: Array<{id: string, status: string, detail: string, remediation?: string}>}} data
 */
function _renderPreflightBanner(drawer, data) {
    // Drawer may have detached while the fetch was in flight
    if (!drawer || !drawer.isConnected || drawer.querySelector('.cu-drawer-banner')) return;

    const banner = document.createElement('div');
    banner.className = `cu-drawer-banner ${data.status === 'fail' ? 'fail' : 'warn'}`;

    for (const check of data.checks) {
        if (check.status === 'ok') continue;
        const line = document.createElement('span');
        line.className = 'cu-banner-check';
        line.textContent = `${check.id}: ${check.detail}`;
        banner.appendChild(line);
        if (check.remediation) {
            const rem = document.createElement('span');
            rem.className = 'cu-banner-remediation';
            rem.textContent = check.remediation;
            banner.appendChild(rem);
        }
    }

    const close = document.createElement('button');
    close.className = 'cu-banner-close';
    close.title = 'Dismiss';
    close.textContent = '×';
    close.addEventListener('click', (e) => {
        e.stopPropagation();
        banner.remove();
        drawer.classList.remove('has-banner');
    });
    banner.appendChild(close);

    // Insert before the controls row so STOP/New stay at the bottom
    const controls = drawer.querySelector('.cu-drawer-controls');
    drawer.insertBefore(banner, controls || null);
    drawer.classList.add('has-banner');
}

// =============================================================================
// Attach / Detach
// =============================================================================

function _attachDrawer() {
    const bubble = document.querySelector('.operator-menu-bubble');
    if (!bubble || document.getElementById('cuDrawer')) return;

    _drawer = _createDrawer();
    bubble.appendChild(_drawer);

    // Wire up buttons
    const stopBtn = _drawer.querySelector('.cu-drawer-estop');
    const newBtn = _drawer.querySelector('.cu-drawer-new');

    stopBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        stopBtn.disabled = true;
        stopBtn.textContent = '...';
        try {
            const result = await stopCUTask();
            if (result.success) {
                toast('Stop requested');
            } else {
                toast(result.error || 'No running task');
            }
        } catch (err) {
            toast('Stop failed: ' + err.message);
        }
        setTimeout(() => {
            stopBtn.disabled = false;
            stopBtn.textContent = 'STOP';
        }, 1500);
    });

    newBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (_currentStatus === 'running') {
            if (!confirm('A task is running. Stop it and start a new session?')) return;
        }
        newBtn.disabled = true;
        try {
            await newCUSession();
            toast('Session cleared');
            _updateStatus('idle');
            _setSession(null);
            _updateQueue(0);
        } catch (err) {
            toast('New session failed: ' + err.message);
        }
        newBtn.disabled = false;
    });

    // Live view — route stream-vs-fallback (M4, design 2026-07-23 §7.1):
    // streaming client when this session is a live virtual session,
    // screenshot-poll modal automatically otherwise (native mode / remote
    // device / no-websockify box). Dynamic import keeps the drawer light.
    const liveBtn = _drawer.querySelector('.cu-drawer-live');
    if (liveBtn) {
        liveBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
                const route = await import('./cu-viewer-route.js');
                await route.openCuViewer({
                    sessionId: _currentSessionId || getCUSessionId() || undefined,
                    deviceId: getCUDeviceId()
                });
            } catch (err) {
                console.error('[CU-Drawer] Failed to open live view:', err);
            }
        });
    }

    // Wire device selector
    const deviceSelect = _drawer.querySelector('.cu-drawer-device-select');
    if (deviceSelect) {
        deviceSelect.addEventListener('change', (e) => {
            e.stopPropagation();
            setCUDeviceId(e.target.value);
            console.log('[CU-Drawer] Device changed to:', e.target.value);
        });
    }

    // Populate device list from API + sync Tailscale (fire-and-forget on first load)
    fetch('/devices/sync-tailscale', { method: 'POST' }).catch(() => {});
    _populateDevices();

    // Auto-refresh device list every 30 seconds for live updates
    if (_deviceRefreshInterval) clearInterval(_deviceRefreshInterval);
    _deviceRefreshInterval = setInterval(_populateDevices, 30000);

    // Preflight environment check (async, non-blocking, once per attach)
    _fetchPreflight(_drawer);

    // Re-populate when model changes (google backend shows Android, others don't)
    const modelEl = document.getElementById('modelSelect');
    if (modelEl) {
        modelEl.addEventListener('change', () => {
            setTimeout(_populateDevices, 100);  // slight delay for window.__model to update
        });
    }

    // Restore session from per-operator localStorage
    const savedId = getCUSessionId();
    if (savedId) _setSession(savedId);

    // Also try server preferences (async, updates if different)
    _fetchSessionIdFromServer().then(serverId => {
        if (serverId && serverId !== savedId) {
            _setSession(serverId);
        }
    });

    // Animate in
    requestAnimationFrame(() => {
        _drawer.classList.add('visible');
    });
}

function _detachDrawer() {
    // Stop auto-refresh when drawer is removed
    if (_deviceRefreshInterval) {
        clearInterval(_deviceRefreshInterval);
        _deviceRefreshInterval = null;
    }
    const drawer = document.getElementById('cuDrawer');
    if (drawer) {
        drawer.classList.remove('visible');
        setTimeout(() => drawer.remove(), 200);
    }
    _drawer = null;
}

// =============================================================================
// Update Functions (exposed as window callbacks)
// =============================================================================

function _updateTool(name, detail) {
    if (!_drawer) return;
    const label = _drawer.querySelector('.cu-drawer-tool-label');
    if (label) {
        const icons = {
            'computer': '\uD83D\uDDA5\uFE0F', 'screenshot': '\uD83D\uDCF8', 'bash': '\u26A1',
            'str_replace_based_edit_tool': '\u270F\uFE0F', 'text_editor': '\u270F\uFE0F',
            'left_click': '\uD83D\uDDB1\uFE0F', 'right_click': '\uD83D\uDDB1\uFE0F',
            'type': '\u2328\uFE0F', 'key': '\u2328\uFE0F', 'scroll': '\u2195\uFE0F',
            'web_search': '\uD83C\uDF10', 'processing': '\u2699\uFE0F'
        };
        const icon = icons[name] || '\uD83D\uDD27';
        const shortDetail = detail && detail.length > 30 ? detail.substring(0, 30) + '...' : (detail || '');
        label.textContent = `${icon} ${name}${shortDetail ? ' \u2014 ' + shortDetail : ''}`;
    }
    // Pulse dot
    const dot = _drawer.querySelector('.cu-drawer-dot');
    if (dot) {
        dot.classList.remove('pulse');
        void dot.offsetWidth;
        dot.classList.add('pulse');
    }
}

function _updateStep(step, total) {
    if (!_drawer) return;
    const stepEl = _drawer.querySelector('.cu-drawer-step');
    if (stepEl) stepEl.textContent = `${step}/${total}`;
}

function _updateStatus(status) {
    if (!_drawer) return;
    _currentStatus = status;
    const dot = _drawer.querySelector('.cu-drawer-dot');
    const label = _drawer.querySelector('.cu-drawer-tool-label');
    const stepEl = _drawer.querySelector('.cu-drawer-step');
    const stopBtn = _drawer.querySelector('.cu-drawer-estop');

    if (dot) {
        dot.className = 'cu-drawer-dot';
        switch (status) {
            case 'running':
                dot.classList.add('running', 'pulse');
                break;
            case 'stopped':
                dot.classList.add('stopped');
                if (label) label.textContent = '\u26D4 Stopped';
                break;
            case 'complete': {
                dot.classList.add('complete');
                const stepText = stepEl ? stepEl.textContent : '';
                if (label) label.textContent = `\u2705 Complete${stepText ? ' \u2014 ' + stepText + ' steps' : ''}`;
                break;
            }
            default:
                if (label) label.textContent = 'Idle';
        }
    }

    // Disable stop button when not running
    if (stopBtn) {
        stopBtn.disabled = status !== 'running';
    }
}

function _updateQueue(count) {
    if (!_drawer) return;
    _queueCount = count;
    const badge = _drawer.querySelector('.cu-drawer-queue-badge');
    if (badge) {
        if (count > 0) {
            badge.textContent = `Q:${count}`;
            badge.classList.remove('hide');
        } else {
            badge.classList.add('hide');
        }
    }
}

function _setSession(id) {
    _currentSessionId = id;
    // Persist per-operator + sync to server
    setCUSessionId(id);
    if (!_drawer) return;
    const sessionEl = _drawer.querySelector('.cu-drawer-session-id');
    if (sessionEl) {
        if (id) {
            sessionEl.textContent = id.substring(0, 8);
            sessionEl.title = id;
        } else {
            sessionEl.textContent = '';
            sessionEl.title = '';
        }
    }
}

function _setDevice(deviceId) {
    const select = document.getElementById('cuDeviceSelect');
    if (select && deviceId) {
        select.value = deviceId;
        setCUDeviceId(deviceId);
    }
}

// =============================================================================
// Provider Change Listener
// =============================================================================

function _onProviderChange() {
    const provider = window.__provider;
    if (provider === 'computer-use') {
        _attachDrawer();
    } else {
        _detachDrawer();
    }
}

// =============================================================================
// Init
// =============================================================================

export function initCUDrawer() {
    // One-time cleanup: remove old non-per-operator session key
    localStorage.removeItem('bb_cu_session_id');

    // Register global callbacks for SSE event handlers in chat-send.js
    window.__cuDrawerUpdateTool = _updateTool;
    window.__cuDrawerUpdateStep = _updateStep;
    window.__cuDrawerUpdateStatus = _updateStatus;
    window.__cuDrawerUpdateQueue = _updateQueue;
    window.__cuDrawerSetSession = _setSession;
    window.__cuDrawerSetDevice = _setDevice;

    // Show drawer if CU is already the active provider
    if (window.__provider === 'computer-use') {
        _attachDrawer();
    }

    // Listen for provider changes via the select element
    const providerEl = document.getElementById('providerSelect');
    if (providerEl) {
        providerEl.addEventListener('change', _onProviderChange);
    }

    // Also set up a MutationObserver on __provider in case it's set programmatically
    let lastProvider = window.__provider;
    setInterval(() => {
        if (window.__provider !== lastProvider) {
            lastProvider = window.__provider;
            _onProviderChange();
        }
    }, 500);

    console.log('[CU-Drawer] Initialized');
}
