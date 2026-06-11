/**
 * cu-interact.js
 * Interactive Computer Use viewer modal.
 * Opens as a fullscreen overlay when user clicks the CU Live View screenshot.
 * Captures click, keyboard, and scroll events and sends them to the backend.
 */

import { $ } from './core-utils.js';
import { getCUDeviceId } from './cu-drawer.js';

let modal = null;
let pollingInterval = null;
let isOpen = false;
let isFetching = false;  // Prevent overlapping fetches
const POLL_MS = 1500;
const DISPLAY_WIDTH = 1280;   // fallback default — live value fetched from /browser/status
const DISPLAY_HEIGHT = 720;   // fallback default — live value fetched from /browser/status
let displayW = DISPLAY_WIDTH;
let displayH = DISPLAY_HEIGHT;

/** Get device_id for interactive action requests. */
function _getDeviceId() { return getCUDeviceId() || 'blackbox'; }

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
            // Re-focus input so keyboard stays open
            typingInput.focus();
        });
    });

    return modal;
}

export function open(initialScreenshotUrl) {
    createModal();
    modal.classList.add('open');
    isOpen = true;

    if (initialScreenshotUrl) {
        modal.querySelector('.cu-interact-screen').src = initialScreenshotUrl;
    }

    modal.setAttribute('tabindex', '-1');
    modal.focus();

    _fetchDisplayResolution();
    refreshScreenshot();
    pollingInterval = setInterval(refreshScreenshot, POLL_MS);
    document.addEventListener('keydown', globalEscHandler);
}

export function close() {
    if (!isOpen) return;
    isOpen = false;
    modal.classList.remove('open');

    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }

    _detachKeyboardOffset();
    document.removeEventListener('keydown', globalEscHandler);
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

function handleScreenClick(e) {
    e.preventDefault();
    const screen = modal.querySelector('.cu-interact-screen');
    const rect = screen.getBoundingClientRect();

    const x = Math.round(((e.clientX - rect.left) / rect.width) * displayW);
    const y = Math.round(((e.clientY - rect.top) / rect.height) * displayH);

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
    const screen = modal.querySelector('.cu-interact-screen');
    const rect = screen.getBoundingClientRect();

    const x = Math.round(((e.clientX - rect.left) / rect.width) * displayW);
    const y = Math.round(((e.clientY - rect.top) / rect.height) * displayH);
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
        open(screenshotUrl);
    });
    document.body.appendChild(floatingBtn);
}

function _removeFloatingButton() {
    if (floatingBtn) {
        floatingBtn.remove();
        floatingBtn = null;
    }
}
