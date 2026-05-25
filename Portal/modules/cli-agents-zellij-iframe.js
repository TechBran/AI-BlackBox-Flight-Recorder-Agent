/**
 * cli-agents-zellij-iframe.js
 * Lifecycle of a single Zellij terminal iframe inside the CLI Agents modal.
 *
 * Per docs/plans/2026-05-24-zellij-cli-agent-rewrite.md T11b.
 *
 * Scope:
 *   - We render + swap + tear down ONE iframe at a time.
 *   - We do NOT mint sessions (launcher T11.5 does), drive the switcher rail
 *     (T11.6), or call /cli-agent/zellij/* (T12 wires those). Pure renderer.
 *   - We own the iframe singleton because Zellij's web UI keeps a live
 *     WebSocket per loaded session; juggling multiple iframes would multiply
 *     sockets without giving the user multiple visible panes anyway. The
 *     switcher's job is to swap src on this one iframe, not stack iframes.
 *   - We own the load-timeout because only the renderer knows when the swap
 *     started; the launcher hands off and forgets.
 */

// Module-level singletons. One iframe per Portal page is the design.
let currentSessionName = null;
let currentIframe = null;
let currentLoadingEl = null;
let currentErrorEl = null;
let currentContainerEl = null;
let currentLoadTimeoutId = null;
let mountOptions = {};

// Zellij's WebSocket handshake + xterm.js boot typically finishes well under
// 2s on localhost; 15s is the upper bound before we declare the proxy or the
// daemon dead. Switcher swaps cancel this timer mid-flight.
const LOAD_TIMEOUT_MS = 15000;

function fireCb(cb, payload, label) {
    if (typeof cb !== 'function') return;
    try { cb(payload); } catch (err) { console.error(`[ZELLIJ-IFRAME] ${label} threw:`, err); }
}

export function mountIframe(containerEl, options = {}) {
    if (!containerEl) {
        console.error('[ZELLIJ-IFRAME] mountIframe called without container element');
        return null;
    }
    if (currentContainerEl === containerEl && currentIframe) {
        console.warn('[ZELLIJ-IFRAME] mountIframe is idempotent — second call on same container ignored');
        return { iframe: currentIframe, loadingEl: currentLoadingEl, errorEl: currentErrorEl };
    }

    mountOptions = options || {};

    const iframe = document.createElement('iframe');
    iframe.className = 'zellij-iframe';
    iframe.src = 'about:blank';
    iframe.setAttribute('frameborder', '0');
    // clipboard-read/write enables copy/paste between the terminal and the
    // host page — primary UX win over the old xterm.js+WS bridge.
    iframe.setAttribute('allow', 'clipboard-read; clipboard-write');
    // sandbox rationale:
    //   allow-scripts:                    Zellij's xterm.js renderer
    //   allow-same-origin:                required so the iframe can open a
    //                                     WebSocket back through our
    //                                     /app-proxy/9097 on the same origin
    //   allow-forms / allow-popups:       Zellij's UI uses both for token
    //                                     entry + auth-URL pop-outs
    //   allow-popups-to-escape-sandbox:   OAuth flows need a real top-level
    //                                     window, not a sandboxed one
    iframe.setAttribute(
        'sandbox',
        'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox',
    );

    const loadingEl = document.createElement('div');
    loadingEl.className = 'zellij-iframe-loading';
    loadingEl.textContent = 'Connecting to terminal…';
    loadingEl.hidden = true;

    const errorEl = document.createElement('div');
    errorEl.className = 'zellij-iframe-error';
    errorEl.setAttribute('role', 'alert');
    errorEl.hidden = true;

    containerEl.appendChild(iframe);
    containerEl.appendChild(loadingEl);
    containerEl.appendChild(errorEl);

    currentContainerEl = containerEl;
    currentIframe = iframe;
    currentLoadingEl = loadingEl;
    currentErrorEl = errorEl;
    currentSessionName = null;

    return { iframe, loadingEl, errorEl };
}

export function loadSession({ sessionUrl, sessionName, onLoad, onError } = {}) {
    if (!currentIframe) {
        console.error('[ZELLIJ-IFRAME] loadSession called before mountIframe');
        return;
    }
    if (!sessionUrl || !sessionName) {
        console.error('[ZELLIJ-IFRAME] loadSession requires sessionUrl + sessionName');
        return;
    }
    if (sessionName === currentSessionName) {
        console.warn(`[ZELLIJ-IFRAME] session "${sessionName}" already loaded — skipping reload`);
        return;
    }

    // Cancel any pending timeout from an in-flight previous swap. The
    // switcher can fire loadSession rapidly when the user clicks between
    // tabs; we don't want a stale timer to fire an error on the new session.
    if (currentLoadTimeoutId !== null) {
        clearTimeout(currentLoadTimeoutId);
        currentLoadTimeoutId = null;
    }

    currentSessionName = sessionName;
    currentLoadingEl.hidden = false;
    currentErrorEl.hidden = true;
    currentErrorEl.textContent = '';

    const handleLoad = () => {
        currentIframe.removeEventListener('load', handleLoad);
        if (currentLoadTimeoutId !== null) {
            clearTimeout(currentLoadTimeoutId);
            currentLoadTimeoutId = null;
        }
        currentLoadingEl.hidden = true;
        const payload = { sessionName, sessionUrl };
        fireCb(onLoad, payload, 'onLoad');
        fireCb(mountOptions.onSessionLoad, payload, 'onSessionLoad');
    };

    currentIframe.addEventListener('load', handleLoad);
    currentIframe.src = sessionUrl;

    currentLoadTimeoutId = setTimeout(() => {
        currentLoadTimeoutId = null;
        currentIframe.removeEventListener('load', handleLoad);
        currentLoadingEl.hidden = true;
        currentErrorEl.hidden = false;
        currentErrorEl.textContent = 'Terminal failed to load. Tap to retry.';
        console.error(`[ZELLIJ-IFRAME] session "${sessionName}" load timed out after ${LOAD_TIMEOUT_MS}ms`);
        const payload = { sessionName, sessionUrl, reason: 'timeout' };
        fireCb(onError, payload, 'onError');
        fireCb(mountOptions.onSessionError, payload, 'onSessionError');
    }, LOAD_TIMEOUT_MS);
}

export function unloadSession() {
    if (!currentIframe) return;
    if (currentLoadTimeoutId !== null) {
        clearTimeout(currentLoadTimeoutId);
        currentLoadTimeoutId = null;
    }
    currentIframe.src = 'about:blank';
    currentLoadingEl.hidden = true;
    currentErrorEl.hidden = true;
    currentErrorEl.textContent = '';
    currentSessionName = null;
}

export function getCurrentSessionName() {
    return currentSessionName;
}

export function unmountIframe() {
    if (currentLoadTimeoutId !== null) {
        clearTimeout(currentLoadTimeoutId);
        currentLoadTimeoutId = null;
    }
    if (currentIframe && currentIframe.parentNode) currentIframe.parentNode.removeChild(currentIframe);
    if (currentLoadingEl && currentLoadingEl.parentNode) currentLoadingEl.parentNode.removeChild(currentLoadingEl);
    if (currentErrorEl && currentErrorEl.parentNode) currentErrorEl.parentNode.removeChild(currentErrorEl);
    currentIframe = null;
    currentLoadingEl = null;
    currentErrorEl = null;
    currentContainerEl = null;
    currentSessionName = null;
    mountOptions = {};
}
