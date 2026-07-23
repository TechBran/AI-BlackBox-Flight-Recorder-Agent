/**
 * cu-interact.js
 * Interactive Computer Use viewer modal.
 * Opens as a fullscreen overlay when user clicks the CU Live View screenshot.
 * Captures click, keyboard, and scroll events and sends them to the backend.
 */

import { $ } from './core-utils.js';
import { getCUDeviceId, getCUSessionId } from './cu-drawer.js';
import {
    createFallbackViewport, fallbackTransformCss, mapViewToScreenshot,
} from './cu-fallback-zoom.js';

let modal = null;
let pollingInterval = null;
let isOpen = false;
let isFetching = false;  // Prevent overlapping fetches
const POLL_MS = 1500;
const DISPLAY_WIDTH = 1280;   // fallback default — live value fetched from /browser/status
const DISPLAY_HEIGHT = 720;   // fallback default — live value fetched from /browser/status
let displayW = DISPLAY_WIDTH;
let displayH = DISPLAY_HEIGHT;

// T12: device override passed by the top-bar "Live" button so the viewer polls
// and drives the RIGHT device (the task's device_id from /tasks/list) instead of
// the drawer's persisted selection. Null when opened without an override — then
// we fall back to the drawer selection, then 'blackbox'. Cleared on close so a
// later plain open() resumes the drawer selection.
let _activeDeviceId = null;

/** Get device_id for interactive action requests. */
function _getDeviceId() { return _activeDeviceId || getCUDeviceId() || 'blackbox'; }

// ── Pinch-zoom/pan state (desktop-first CU 2026-07-23, part C) ──
// View-only CSS transform on the screenshot <img>; the math is the streaming
// client's ViewportTransform (via cu-fallback-zoom.js). Clicks map through the
// CURRENT transform, so the fallback is never precise-tap-only.
let _vt = null;          // ViewportTransform over the img's layout box, or null
let _pinch = null;       // active two-finger gesture {dist, cx, cy}
let _panTouch = null;    // active one-finger pan while zoomed {x, y, moved}
let _suppressClick = false; // swallow the synthetic click after a pan/pinch

/** (Re)build the viewport when the img's LAYOUT size changes (offsetWidth is
 *  transform-independent, so an active zoom never rebuilds it). Keeps the
 *  current transform across screenshot polls of the same size. */
function _syncViewport() {
    const screen = modal && modal.querySelector('.cu-interact-screen');
    if (!screen) return;
    const w = screen.offsetWidth;
    const h = screen.offsetHeight;
    if (!(w > 0 && h > 0)) { return; }
    if (_vt && _vt.viewW === w && _vt.viewH === h) return;
    _vt = createFallbackViewport(w, h);
    _applyTransform();
}

function _applyTransform() {
    const screen = modal && modal.querySelector('.cu-interact-screen');
    if (!screen) return;
    screen.style.transformOrigin = '0 0';
    screen.style.transform = (_vt && _vt.scale !== 1) || (_vt && (_vt.tx || _vt.ty))
        ? fallbackTransformCss(_vt) : '';
}

function _resetViewport() {
    _vt = null;
    _pinch = null;
    _panTouch = null;
    _suppressClick = false;
    const screen = modal && modal.querySelector('.cu-interact-screen');
    if (screen) screen.style.transform = '';
}

function _wrapPoint(t) {
    const wrap = modal.querySelector('.cu-interact-screen-wrap');
    const r = wrap.getBoundingClientRect();
    return { x: t.clientX - r.left, y: t.clientY - r.top };
}

function handleTouchStart(e) {
    _syncViewport();
    if (!_vt) return;
    if (e.touches.length === 2) {
        e.preventDefault();
        const a = _wrapPoint(e.touches[0]);
        const b = _wrapPoint(e.touches[1]);
        _pinch = {
            dist: Math.hypot(a.x - b.x, a.y - b.y),
            cx: (a.x + b.x) / 2, cy: (a.y + b.y) / 2,
        };
        _panTouch = null;
        _suppressClick = true;  // a two-finger gesture is never a click
    } else if (e.touches.length === 1 && _vt.zoomedIn) {
        const p = _wrapPoint(e.touches[0]);
        _panTouch = { x: p.x, y: p.y, moved: false };
    } else {
        _panTouch = null;
    }
}

function handleTouchMove(e) {
    if (!_vt) return;
    if (_pinch && e.touches.length === 2) {
        e.preventDefault();
        const a = _wrapPoint(e.touches[0]);
        const b = _wrapPoint(e.touches[1]);
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        const cx = (a.x + b.x) / 2;
        const cy = (a.y + b.y) / 2;
        if (_pinch.dist > 0 && d > 0) _vt.zoomAt(cx, cy, d / _pinch.dist);
        _vt.panBy(cx - _pinch.cx, cy - _pinch.cy);
        _pinch = { dist: d, cx, cy };
        _applyTransform();
    } else if (_panTouch && e.touches.length === 1) {
        const p = _wrapPoint(e.touches[0]);
        const dx = p.x - _panTouch.x;
        const dy = p.y - _panTouch.y;
        if (_panTouch.moved || Math.hypot(dx, dy) > 8) {
            e.preventDefault();
            _panTouch.moved = true;
            _suppressClick = true;  // this drag pans the view; it must not click
            _vt.panBy(dx, dy);
            _panTouch.x = p.x;
            _panTouch.y = p.y;
            _applyTransform();
        }
    }
}

function handleTouchEnd(e) {
    if (e.touches.length < 2) _pinch = null;
    if (e.touches.length === 0) _panTouch = null;
    // _suppressClick is consumed by handleScreenClick (the browser fires the
    // synthetic click AFTER touchend).
}

/**
 * Fetch the live CU display resolution from /browser/status (fire-and-forget).
 * Native mode reports `cu_resolution` ("1280x720"); non-native mode reports
 * `resolution`. On any failure (fetch error, display down, unparseable shape)
 * the 1280x720 defaults are kept silently.
 */
function _fetchDisplayResolution() {
    fetch('/browser/status')
        .then((res) => res.json())
        .then((data) => {
            const resStr = data && (data.cu_resolution || data.resolution);
            if (typeof resStr !== 'string') return;
            const m = resStr.match(/^(\d+)x(\d+)$/);
            if (!m) return;
            const w = parseInt(m[1], 10);
            const h = parseInt(m[2], 10);
            if (w > 0 && h > 0) {
                displayW = w;
                displayH = h;
            }
        })
        .catch(() => { /* keep defaults */ });
}

function createModal() {
    if (modal) return modal;

    modal = document.createElement('div');
    modal.className = 'cu-interact-overlay';
    modal.innerHTML = `
        <div class="cu-interact-modal">
            <div class="cu-interact-header">
                <div class="cu-interact-header-left">
                    <span class="cu-interact-dot"></span>
                    <span class="cu-interact-title">Interactive Browser</span>
                    <span class="cu-interact-step"></span>
                </div>
                <div class="cu-interact-header-right">
                    <button class="cu-interact-btn cu-interact-refresh" title="Force refresh">↻</button>
                    <button class="cu-interact-btn cu-interact-close" title="Close (ESC)">✕</button>
                </div>
            </div>
            <div class="cu-interact-body">
                <div class="cu-interact-screen-wrap">
                    <img class="cu-interact-screen" draggable="false" />
                    <div class="cu-interact-click-indicator"></div>
                </div>
            </div>
            <div class="cu-interact-typing-bar">
                <input type="text" class="cu-interact-typing-input"
                       placeholder="Tap to type..."
                       autocomplete="off" autocorrect="off" autocapitalize="off"
                       inputmode="text" />
                <button class="cu-interact-key-btn" data-key="Return" title="Enter">↵</button>
                <button class="cu-interact-key-btn" data-key="Tab" title="Tab">⇥</button>
                <button class="cu-interact-key-btn" data-key="BackSpace" title="Backspace">⌫</button>
                <button class="cu-interact-key-btn" data-key="Escape" title="Escape">Esc</button>
            </div>
            <div class="cu-interact-status">
                <span class="cu-interact-status-text">Click to interact</span>
                <span class="cu-interact-hint">ESC to close</span>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    const closeBtn = modal.querySelector('.cu-interact-close');
    const refreshBtn = modal.querySelector('.cu-interact-refresh');
    const screen = modal.querySelector('.cu-interact-screen');

    closeBtn.addEventListener('click', close);
    refreshBtn.addEventListener('click', refreshScreenshot);
    screen.addEventListener('click', handleScreenClick);
    screen.addEventListener('wheel', handleScreenScroll, { passive: false });
    screen.addEventListener('load', _syncViewport);

    // Pinch-zoom/pan on the screenshot surface (view-only CSS transform).
    const screenWrap = modal.querySelector('.cu-interact-screen-wrap');
    screenWrap.addEventListener('touchstart', handleTouchStart, { passive: false });
    screenWrap.addEventListener('touchmove', handleTouchMove, { passive: false });
    screenWrap.addEventListener('touchend', handleTouchEnd, { passive: false });
    screenWrap.addEventListener('touchcancel', handleTouchEnd, { passive: false });
    modal.addEventListener('keydown', handleKeyDown);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) close();
    });

    // Mobile typing bar — handles soft keyboard input
    const typingInput = modal.querySelector('.cu-interact-typing-input');
    typingInput.addEventListener('input', handleTypingInput);
    typingInput.addEventListener('keydown', handleTypingKeyDown);
    typingInput.addEventListener('focus', _attachKeyboardOffset);
    typingInput.addEventListener('blur', _detachKeyboardOffset);

    // Quick key buttons (Enter, Tab, Backspace, Escape)
    modal.querySelectorAll('.cu-interact-key-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const key = btn.dataset.key;
            if (key === 'Escape') { close(); return; }
            fetch('/browser/key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key, device_id: _getDeviceId() })
            });
            setStatus(`Key: ${key}`);
            setTimeout(refreshScreenshot, 200);
            // NO auto re-focus (design 2026-07-23 §4.4): the soft keyboard
            // opens only when the user taps the typing input themselves —
            // quick keys must never pop the IME. Parity with the streaming
            // client's manual keyboard toggle.
        });
    });

    return modal;
}

export function open(initialScreenshotUrl, deviceId) {
    // Re-entry guard, symmetric with close()'s `if (!isOpen) return;`. Without it
    // a second open() before a close() would leak a second 1.5s screenshot
    // pollingInterval (the first is never cleared) and leave a stale
    // _activeDeviceId override in place. Idempotent open/close is the module's
    // discipline.
    if (isOpen) return;
    createModal();
    // T12: optional device override (backward compatible — existing open(url)
    // calls pass no deviceId and keep using the drawer selection).
    if (deviceId) _activeDeviceId = deviceId;
    modal.classList.add('open');
    isOpen = true;

    if (initialScreenshotUrl) {
        modal.querySelector('.cu-interact-screen').src = initialScreenshotUrl;
    }

    modal.setAttribute('tabindex', '-1');
    modal.focus();

    _fetchDisplayResolution();
    _syncViewport();
    refreshScreenshot();
    pollingInterval = setInterval(refreshScreenshot, POLL_MS);
    document.addEventListener('keydown', globalEscHandler);
    window.addEventListener('resize', _syncViewport);
}

export function close() {
    if (!isOpen) return;
    isOpen = false;
    _activeDeviceId = null;   // T12: drop the override so a later open() resumes the drawer device
    modal.classList.remove('open');

    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }

    _detachKeyboardOffset();
    document.removeEventListener('keydown', globalEscHandler);
    window.removeEventListener('resize', _syncViewport);
    _resetViewport();  // a later open() starts un-zoomed
}

// =============================================================================
// Soft Keyboard Offset (visualViewport)
// =============================================================================

/** Active visualViewport resize handler (null when detached). */
let vvResizeHandler = null;

/**
 * Keep the typing bar above the soft keyboard while the input is focused.
 * Uses the visualViewport API; no-op on browsers without it.
 */
function _attachKeyboardOffset() {
    if (!window.visualViewport || vvResizeHandler) return;
    const bar = modal.querySelector('.cu-interact-typing-bar');
    if (!bar) return;
    vvResizeHandler = () => {
        const vv = window.visualViewport;
        const kb = window.innerHeight - vv.height - vv.offsetTop;
        bar.style.transform = kb > 0 ? `translateY(-${kb}px)` : '';
    };
    window.visualViewport.addEventListener('resize', vvResizeHandler);
    vvResizeHandler();
}

/** Remove the keyboard offset listener and reset the bar position. */
function _detachKeyboardOffset() {
    if (vvResizeHandler && window.visualViewport) {
        window.visualViewport.removeEventListener('resize', vvResizeHandler);
    }
    vvResizeHandler = null;
    const bar = modal ? modal.querySelector('.cu-interact-typing-bar') : null;
    if (bar) bar.style.transform = '';
}

function globalEscHandler(e) {
    if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        close();
    }
}

async function refreshScreenshot() {
    if (!isOpen || isFetching) return;
    isFetching = true;
    try {
        const res = await fetch(`/browser/screenshot/live?device_id=${encodeURIComponent(_getDeviceId())}`);
        const data = await res.json();
        if (data.url) {
            const screen = modal.querySelector('.cu-interact-screen');
            // Wait for image to load before allowing next fetch
            const imgUrl = data.url + '?t=' + data.timestamp;
            await new Promise((resolve) => {
                screen.onload = resolve;
                screen.onerror = resolve;
                screen.src = imgUrl;
            });
        }
    } catch (e) {
        // Silently ignore polling errors
    } finally {
        isFetching = false;
    }
}

/**
 * Map a client-space pointer event to screenshot coords. Goes through the
 * pinch viewport when active (mapViewToScreenshot — the transform-aware pure
 * math tested in cu-fallback-zoom.test.mjs); falls back to the legacy
 * rect-ratio math when the viewport is not initialized. Both are equivalent
 * at identity — the helper is what keeps zoomed clicks correct.
 */
function _eventToScreenshotCoords(e) {
    if (_vt) {
        const wrap = modal.querySelector('.cu-interact-screen-wrap');
        const wr = wrap.getBoundingClientRect();
        return mapViewToScreenshot(_vt, e.clientX - wr.left, e.clientY - wr.top,
                                   displayW, displayH);
    }
    const screen = modal.querySelector('.cu-interact-screen');
    const rect = screen.getBoundingClientRect();
    return {
        x: Math.round(((e.clientX - rect.left) / rect.width) * displayW),
        y: Math.round(((e.clientY - rect.top) / rect.height) * displayH),
    };
}

function handleScreenClick(e) {
    e.preventDefault();
    // The synthetic click that trails a pan/pinch gesture is not a click.
    if (_suppressClick) { _suppressClick = false; return; }
    const screen = modal.querySelector('.cu-interact-screen');
    const rect = screen.getBoundingClientRect();

    const { x, y } = _eventToScreenshotCoords(e);

    showClickIndicator(e.clientX - rect.left, e.clientY - rect.top, rect);

    fetch('/browser/click', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ x, y, button: 'left', device_id: _getDeviceId() })
    });

    setStatus(`Clicked at ${x}, ${y}`);
    setTimeout(refreshScreenshot, 200);
}

function handleScreenScroll(e) {
    e.preventDefault();

    // Ctrl+wheel (incl. trackpad pinch, which browsers report as ctrl+wheel):
    // zoom the VIEW about the pointer — desktop parity with touch pinch.
    if (e.ctrlKey) {
        _syncViewport();
        if (_vt) {
            const wrap = modal.querySelector('.cu-interact-screen-wrap');
            const wr = wrap.getBoundingClientRect();
            _vt.zoomAt(e.clientX - wr.left, e.clientY - wr.top,
                       e.deltaY < 0 ? 1.1 : 1 / 1.1);
            _applyTransform();
        }
        return;
    }

    const { x, y } = _eventToScreenshotCoords(e);
    const direction = e.deltaY > 0 ? 'down' : 'up';
    const clicks = Math.min(Math.ceil(Math.abs(e.deltaY) / 50), 5);

    fetch('/browser/scroll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ x, y, direction, clicks, device_id: _getDeviceId() })
    });

    setStatus(`Scrolled ${direction}`);
    setTimeout(refreshScreenshot, 200);
}

function handleKeyDown(e) {
    if (e.key === 'Escape') return;

    e.preventDefault();
    e.stopPropagation();

    const keyMap = {
        'Enter': 'Return',
        'Backspace': 'BackSpace',
        'Tab': 'Tab',
        'ArrowUp': 'Up',
        'ArrowDown': 'Down',
        'ArrowLeft': 'Left',
        'ArrowRight': 'Right',
        'Delete': 'Delete',
        'Home': 'Home',
        'End': 'End',
        'PageUp': 'Prior',
        'PageDown': 'Next',
        ' ': 'space',
    };

    if (e.ctrlKey || e.metaKey) {
        const combo = 'ctrl+' + e.key.toLowerCase();
        fetch('/browser/key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: combo, device_id: _getDeviceId() })
        });
        setStatus(`Key: ${combo}`);
        setTimeout(refreshScreenshot, 200);
        return;
    }

    if (e.altKey) {
        const combo = 'alt+' + e.key;
        fetch('/browser/key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: combo, device_id: _getDeviceId() })
        });
        setStatus(`Key: ${combo}`);
        setTimeout(refreshScreenshot, 200);
        return;
    }

    if (keyMap[e.key]) {
        fetch('/browser/key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: keyMap[e.key], device_id: _getDeviceId() })
        });
        setStatus(`Key: ${e.key}`);
        setTimeout(refreshScreenshot, 200);
        return;
    }

    if (e.key.length === 1) {
        fetch('/browser/type', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: e.key, device_id: _getDeviceId() })
        });
        setStatus(`Typed: ${e.key}`);
        setTimeout(refreshScreenshot, 150);
    }
}

// =============================================================================
// Mobile Typing Bar Handlers
// =============================================================================

/**
 * Handle text input from the typing bar (mobile soft keyboard).
 * Fires on each character/composition from the Android keyboard.
 */
function handleTypingInput(e) {
    const input = e.target;
    const text = input.value;
    if (!text) return;

    // Send the typed text to the browser
    fetch('/browser/type', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, device_id: _getDeviceId() })
    });

    setStatus(`Typed: ${text}`);
    // Clear input after sending
    input.value = '';
    setTimeout(refreshScreenshot, 150);
}

/**
 * Handle special keys from the typing bar (Enter, Backspace on physical keyboard).
 * On Android soft keyboard, Enter triggers this before input event.
 */
function handleTypingKeyDown(e) {
    const keyMap = {
        'Enter': 'Return',
        'Backspace': 'BackSpace',
        'Tab': 'Tab',
    };

    if (keyMap[e.key]) {
        e.preventDefault();
        fetch('/browser/key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: keyMap[e.key], device_id: _getDeviceId() })
        });
        setStatus(`Key: ${e.key}`);
        setTimeout(refreshScreenshot, 200);
    }
}

function showClickIndicator(localX, localY, rect) {
    const indicator = modal.querySelector('.cu-interact-click-indicator');
    const wrap = modal.querySelector('.cu-interact-screen-wrap');
    const wrapRect = wrap.getBoundingClientRect();

    indicator.style.left = (localX + (rect.left - wrapRect.left)) + 'px';
    indicator.style.top = (localY + (rect.top - wrapRect.top)) + 'px';
    indicator.classList.remove('active');
    void indicator.offsetWidth;
    indicator.classList.add('active');
}

function setStatus(text) {
    const statusEl = modal.querySelector('.cu-interact-status-text');
    if (statusEl) statusEl.textContent = text;
}

export function updateScreenshot(url, step) {
    if (!isOpen || !modal) return;
    const screen = modal.querySelector('.cu-interact-screen');
    if (screen) screen.src = url + '?t=' + Date.now();
    const stepEl = modal.querySelector('.cu-interact-step');
    if (stepEl) stepEl.textContent = `Step ${step}`;
    const dot = modal.querySelector('.cu-interact-dot');
    if (dot) dot.classList.add('pulse');
}

export function isInteractOpen() {
    return isOpen;
}

// =============================================================================
// State Persistence (survives page refresh)
// =============================================================================

const CU_STATE_KEY = 'bb_cu_active_state';

/**
 * Save CU active state to sessionStorage so the interactive viewer
 * can be restored after a page refresh (e.g., Android back button).
 */
export function saveCUState(screenshotUrl, step) {
    try {
        sessionStorage.setItem(CU_STATE_KEY, JSON.stringify({
            active: true,
            lastScreenshotUrl: screenshotUrl || '',
            lastStep: step || 0,
            timestamp: Date.now()
        }));
    } catch (e) {
        // Ignore storage errors
    }
}

/**
 * Clear CU active state (call when CU session ends).
 */
export function clearCUState() {
    try {
        sessionStorage.removeItem(CU_STATE_KEY);
        _removeFloatingButton();
    } catch (e) {
        // Ignore
    }
}

/**
 * Check for saved CU state on page load and show a floating
 * "Live Browser" button if CU was active before refresh.
 * Called from app-init or chat-send during initialization.
 */
export function restoreCUButton() {
    try {
        const raw = sessionStorage.getItem(CU_STATE_KEY);
        if (!raw) return;
        const state = JSON.parse(raw);
        // Only restore if state is recent (within 30 minutes)
        if (!state.active || (Date.now() - state.timestamp) > 30 * 60 * 1000) {
            sessionStorage.removeItem(CU_STATE_KEY);
            return;
        }
        _showFloatingButton(state.lastScreenshotUrl);
    } catch (e) {
        // Ignore parse errors
    }
}

/** Floating "Live Browser" button for quick access after refresh */
let floatingBtn = null;

function _showFloatingButton(screenshotUrl) {
    if (floatingBtn) return;
    floatingBtn = document.createElement('button');
    floatingBtn.className = 'cu-floating-browser-btn';
    floatingBtn.innerHTML = '<span class="cu-floating-dot"></span> Live Browser';
    floatingBtn.title = 'Open interactive browser viewer';
    floatingBtn.addEventListener('click', () => {
        // Route stream-vs-fallback (M4): the restored session may have a live
        // virtual display — prefer the streaming client, fall back to this
        // modal. Dynamic import avoids a static cycle with cu-viewer-route.
        import('./cu-viewer-route.js')
            .then((m) => m.openCuViewer({
                sessionId: getCUSessionId() || undefined,
                deviceId: getCUDeviceId(),
                screenshotUrl
            }))
            .catch(() => open(screenshotUrl));
    });
    document.body.appendChild(floatingBtn);
}

function _removeFloatingButton() {
    if (floatingBtn) {
        floatingBtn.remove();
        floatingBtn = null;
    }
}
