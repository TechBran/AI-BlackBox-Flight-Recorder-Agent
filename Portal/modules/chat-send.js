/**
 * chat-send.js
 * Chat message sending, streaming, and media polling
 * Handles all chat communication modes (streaming SSE, polling, agent WebSocket)
 *
 * Source: Portal/app.js lines 350-370, 1740-2330, 7145-8140
 * Refactored: 2025-12-20
 */

import { $, toast, escapeAttr, createPoller } from './core-utils.js';
import { parseSseStream } from './sse.js';
import { getHistoryData, setHistoryData, saveHistory, getOperator, OPERATOR_REQUIRED_MESSAGE } from './state-management.js';
import { renderMarkdown, addCopyButtonsToCodeBlocks } from './markdown-renderer.js';
import { makeSnapshotsClickable, applySyntaxHighlighting } from './timeline-browser.js';
import { addBubble, appendBubble, attachRetryChip, createAnimatedThinkingBubble, updateThinkingBubble, addDownloadButtonToImage, escapeHtml, decorateWellnessBubble, mergeWellnessUsed } from './chat-bubbles.js';
import { wellnessSigner, withOriginProof, lastSourceLabel } from './wellness-section.js';
import { updateHintsVisibility } from './help-hints.js';
import { speakToBubble, setLastAssistantText, speakThinkingContent } from './tts-stt.js';
import { handleFileUpload, handleSessionFileUpload, getAttachedFiles, clearAttachedFiles, renderPreviews } from './file-upload.js';
import { openVideoExtensionModal } from './generation-modals.js';
import { notifyNewMessage, notifyTaskComplete, showNotification } from './notifications.js';
import { refreshHealth, scrollToBottomIfNeeded, setStreamingState, snapToBottom } from './ui-setup.js';
import { AgentChatHandler, getAgentHandler, setAgentHandler } from './agent-handler.js';
import { GeminiAgentHandler, getGeminiAgentHandler, setGeminiAgentHandler } from './gemini-agent-handler.js';
import { todoTracker } from './todo-tracker.js';
import { open as openCUInteract, updateScreenshot as updateCUInteractScreenshot, isInteractOpen, saveCUState, clearCUState, restoreCUButton } from './cu-interact.js';
import { getCUSessionId, setCUSessionId, getCUDeviceId } from './cu-drawer.js';
import { SignalLine } from './signal-feed.js';

// =============================================================================
// Constants
// =============================================================================

/** Streaming mode enabled state */
let STREAMING_ENABLED = localStorage.getItem("bb_streaming_enabled") !== "false";

/** Computer Use step tracking for TodoTracker */
let _cuCurrentStep = 0;
let _cuTotalSteps = 0;
/** Dynamic task list — built from actual CU events, not pre-generated */
let _cuTasks = [];
/** CU queue append mode — when true, queued responses append to the original bubble */
let _cuQueueAppendMode = false;
/** True when an SSE stream is actively consuming CU events — blocks poller from adding duplicates */
let _cuStreamActive = false;

/**
 * The turn key /chat/stream's stream_start names for an operator with a health
 * store ("wt_" + 16 hex, Orchestrator/wellness/turn.py). /chat/save gets it
 * back and masks exactly that turn's health results; anything else is not
 * echoed, and the box then masks against every recent result (the safe way).
 */
const WELLNESS_TURN_KEY = /^[A-Za-z0-9_-]{1,64}$/;

// =============================================================================
// Helper Functions
// =============================================================================

/**
 * Unwrap a reply that is ENTIRELY one ```json (or bare ```) fence around a
 * JSON envelope, e.g. ```json {"reply": ...} ```. Anything else comes back
 * trimmed but otherwise untouched: the old version deleted every ``` line in
 * the reply, so code blocks in polled replies rendered as prose (audit
 * P01-chat-core-web-08).
 * @param {string} s - Text to strip
 * @returns {string} Stripped text
 */
function stripMdAndFences(s) {
    if (typeof s !== 'string') return s;
    const t = s.trim();
    const m = /^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n?```$/i.exec(t);
    if (m && /^\s*[{[]/.test(m[1]) && m[1].indexOf('```') === -1) return m[1].trim();
    return t;
}

/**
 * Pick reply text from various response formats
 * @param {*} x - Response object or string
 * @returns {string} Extracted reply text
 */
function pickReplyFromAny(x) {
    if (x && typeof x === "object") {
        const cand = x.ui_reply ?? x.reply ?? x.text ?? x.message ?? null;
        if (typeof cand === "string") return stripMdAndFences(cand);
        if (cand && typeof cand === "object") return pickReplyFromAny(cand);
        return stripMdAndFences(JSON.stringify(x));
    }
    if (typeof x === "string") {
        let t = stripMdAndFences(x);
        if (t.startsWith("{") && /"ui_reply"|"reply"|"text"/.test(t)) {
            try { return pickReplyFromAny(JSON.parse(t)); } catch {}
        }
        return t;
    }
    return String(x ?? "");
}

/**
 * Normalize reply from any response format
 * @param {*} x - Response object or string
 * @returns {string} Normalized reply text
 */
export function normalizeReplyFromAny(x) {
    try { return pickReplyFromAny(x); }
    catch(e) { console.warn("normalizeReplyFromAny error", e); return (typeof x === "string") ? x : JSON.stringify(x); }
}

/**
 * Strip snapshot format from history content
 * @param {string} text - Text to strip
 * @returns {string} Cleaned text
 */
function stripSnapshotFormat(text) {
    if (typeof text !== 'string') return text;
    // Remove complete snapshot blocks
    let cleaned = text.replace(/===\s*START SNAPSHOT[\s\S]*?===\s*END SNAPSHOT[^=]*===/g, '');
    // Remove orphan START/END markers
    cleaned = cleaned.replace(/===\s*START SNAPSHOT[^=]*===/g, '');
    cleaned = cleaned.replace(/===\s*END SNAPSHOT[^=]*===/g, '');
    // Remove common snapshot structure elements
    cleaned = cleaned.replace(/^(CROSS-FILE BEACON|VOLUME TRACKER|GAUGES|SNAPSHOT BODY|Kernel Index|Context Provenance|Raw Session Log|Release Notes|SESSION SUMMARY|FILES MODIFIED|CHANGES MADE|REASONING|TESTING).*$/gm, '');
    cleaned = cleaned.replace(/^(Tail-first sweep|COUNT=|UFL:|Result:|Mode:|MODEL:|OPERATOR:|CONTINUITY:|TOKENS \(since).*$/gm, '');
    cleaned = cleaned.replace(/^-\s*(Tail|Current|Next|Volume|Recent fossils|Relevant fossils|GM_EXCERPT):.*$/gm, '');

    // Remove media placeholder HTML
    cleaned = cleaned.replace(/<div\s+class="image-loading-placeholder"[^>]*>[\s\S]*?<\/div>/gi, '');
    cleaned = cleaned.replace(/<div\s+class="video-loading-placeholder"[^>]*>[\s\S]*?<\/div>/gi, '');
    cleaned = cleaned.replace(/<div\s+class="music-loading-placeholder"[^>]*>[\s\S]*?<\/div>/gi, '');
    cleaned = cleaned.replace(/<div\s+class="generated-image"[^>]*>[\s\S]*?<\/div>/gi, '');
    cleaned = cleaned.replace(/<div\s+class="generated-video"[^>]*>[\s\S]*?<\/div>/gi, '');
    cleaned = cleaned.replace(/<div\s+class="audio-result"[^>]*>[\s\S]*?<\/div>/gi, '');
    cleaned = cleaned.replace(/<img\s+[^>]*src="[^"]*\/ui\/uploads\/[^"]*"[^>]*>/gi, '');
    cleaned = cleaned.replace(/<video\s+[^>]*>[\s\S]*?<\/video>/gi, '');
    cleaned = cleaned.replace(/<audio\s+[^>]*>[\s\S]*?<\/audio>/gi, '');

    cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim();
    return cleaned;
}

// =============================================================================
// Streaming Bubble Functions
// =============================================================================

/**
 * Create a streaming thinking bubble
 * @returns {HTMLElement} Bubble element
 */
export function createStreamingThinkingBubble() {
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant streaming-bubble";

    // "The Signal": one red, morphing telemetry line hosted where the old fake
    // "Reasoning in progress" placeholder used to sit. It is a persistent HUD for
    // the whole turn (retrieval → generate → tool → mint) and is driven ONLY by
    // the streaming events below (system_activity / tool_start / tool_result) plus
    // the post-save mint line — presentation-only, never persisted.
    const signalHost = document.createElement("div");
    signalHost.className = "signal-host";

    // Create the floating panel (collapsed by default). No id: every turn has
    // its own panel, reached through bubble._floatingPanel (a fixed id made a
    // panel orphaned by a failed turn capture the next turn's reasoning,
    // audit P01-chat-core-web-09).
    const panel = document.createElement("div");
    panel.className = "streaming-thinking-panel reasoning-active floating collapsed";
    panel.innerHTML = `
        <div class="reasoning-header" data-bubble-action="thinking-toggle">
            <span class="reasoning-icon">\uD83E\uDDE0</span>
            <span class="reasoning-label">Reasoning</span>
            <span class="reasoning-stats">
                <span class="thinking-word-count">0</span> <span class="stats-label">words</span>
            </span>
            <div class="reasoning-actions">
                <button class="reasoning-btn tts-btn" title="Listen to reasoning" data-bubble-action="thinking-speak">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                        <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                    </svg>
                </button>
                <button class="reasoning-btn toggle-btn" data-bubble-action="thinking-toggle">
                    <span class="reasoning-toggle">\u25B6</span>
                </button>
            </div>
        </div>
        <div class="thinking-content"></div>
    `;

    // Add the Signal line to the bubble (top) and mount its controller
    bubble.appendChild(signalHost);
    const signalLine = new SignalLine();
    signalLine.mount(signalHost);

    // Add inline panel (hidden during streaming, shown when complete)
    bubble.appendChild(panel);

    // Create floating panel for body (clone and append)
    const floatingPanel = panel.cloneNode(true);
    document.body.appendChild(floatingPanel);

    // This turn's floating panel: every reasoning update and the removal go
    // through this reference, never a document-wide lookup.
    bubble._floatingPanel = floatingPanel;
    // The Signal controller — the SSE loop and the post-save mint push reach it here.
    bubble._signalLine = signalLine;

    // Hide the inline panel (we're using the floating one during streaming)
    panel.style.display = 'none';

    // Add response panel and controls
    const responsePanel = document.createElement('div');
    responsePanel.className = 'streaming-response-panel';
    responsePanel.innerHTML = '<div class="response-content"></div>';
    bubble.appendChild(responsePanel);

    const controls = document.createElement('div');
    controls.className = 'bubble-controls';
    controls.innerHTML = `
        <button class="bubble-btn speak-btn" title="Generate audio" aria-pressed="false">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
            </svg>
        </button>
        <button class="bubble-btn copy-btn" title="Copy text">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
        </button>
    `;
    bubble.appendChild(controls);

    return bubble;
}

// =============================================================================
// Bubble panel controls
// =============================================================================

/**
 * The bubbles' panel headers and buttons, routed by ONE delegated listener:
 * each control carries a data-bubble-action instead of an inline onclick
 * string, so the Portal's Content-Security-Policy can forbid inline script
 * (audit rank 75). Delegation also covers bubbles rebuilt from HTML strings
 * (restored history, re-rendered panels) without re-attaching listeners. Only
 * the innermost control acts, so a header's buttons never toggle the header.
 * @param {MouseEvent} e - A click anywhere in the page
 */
function onBubbleControlClick(e) {
    const control = e.target && e.target.closest && e.target.closest('[data-bubble-action]');
    if (!control) return;
    switch (control.dataset.bubbleAction) {
        case 'thinking-toggle':
            window.toggleThinkingPanel(control.closest('.reasoning-header'));
            break;
        case 'thinking-speak':
            speakThinkingContent(control);
            break;
        case 'cu-panel-toggle':
            window.toggleCUPanel(control);
            break;
        case 'cu-live-view-toggle':
            window.toggleCULiveView(control);
            break;
        case 'cu-bg-view':
            window.__openCUBrowserView();
            break;
        case 'cu-bg-reconnect':
            window.__reconnectCU();
            break;
    }
}

if (typeof document !== 'undefined') {
    document.addEventListener('click', onBubbleControlClick);
}

// Toggle a reasoning panel open/closed (also callable from the page)
window.toggleThinkingPanel = function(header) {
    const panel = header.closest('.streaming-thinking-panel');
    if (panel) {
        panel.classList.toggle('collapsed');
        const toggle = panel.querySelector('.reasoning-toggle');
        if (toggle) {
            toggle.textContent = panel.classList.contains('collapsed') ? '\u25B6' : '\u25BC';
        }
    }
};

// Legacy toggle function for old HTML
window.toggleReasoningPanel = window.toggleThinkingPanel;

// ─── Computer Use Activity Panel ───

/**
 * Toggle the CU activity panel open/closed
 */
window.toggleCUPanel = function(header) {
    const panel = header.closest('.cu-activity-panel');
    if (panel) {
        panel.classList.toggle('collapsed');
        const toggle = panel.querySelector('.cu-activity-toggle');
        if (toggle) {
            toggle.textContent = panel.classList.contains('collapsed') ? '\u25B6' : '\u25BC';
        }
    }
};

/**
 * Get or create the collapsible CU activity panel inside a bubble.
 * Panel is a sibling of .streaming-response-panel so innerHTML overwrites don't destroy it.
 * @param {HTMLElement} bubble - The chat bubble element
 * @returns {HTMLElement} The CU activity panel element
 */
function _ensureCUPanel(bubble) {
    let panel = bubble.querySelector('.cu-activity-panel');
    if (panel) return panel;

    panel = document.createElement('div');
    panel.className = 'cu-activity-panel';
    panel.innerHTML = `
        <div class="cu-activity-header" data-bubble-action="cu-panel-toggle">
            <span class="cu-live-dot pulse"></span>
            <span class="cu-activity-label">Computer Use</span>
            <span class="cu-step-counter"></span>
            <span class="cu-activity-toggle">\u25BC</span>
        </div>
        <div class="cu-activity-body"></div>
    `;

    // Insert before the response panel so activity shows above the text response
    const responsePanel = bubble.querySelector('.streaming-response-panel');
    if (responsePanel) {
        bubble.insertBefore(panel, responsePanel);
    } else {
        const controls = bubble.querySelector('.bubble-controls');
        if (controls) {
            bubble.insertBefore(panel, controls);
        } else {
            bubble.appendChild(panel);
        }
    }

    return panel;
}

/**
 * Start a new CU step in the todo panel.
 * Marks the previous step as completed and adds a new in_progress entry.
 * @param {number} stepNum - Step number from cu_step event
 */
function _cuTodoNewStep(stepNum) {
    // Mark previous step completed
    const prev = _cuTasks.find(t => t.status === 'in_progress');
    if (prev) { prev.status = 'completed'; prev.activeForm = null; }

    _cuTasks.push({
        id: `cu-step-${stepNum}`,
        content: `Step ${stepNum} — Analyzing screen...`,
        status: 'in_progress',
        activeForm: 'Analyzing screen...'
    });
    todoTracker.updateTodos([..._cuTasks]);
}

/**
 * Update the current in-progress CU task with a descriptive action.
 * Called by cu_action, cu_bash_output, cu_file_edit, cu_screenshot handlers.
 * @param {string} description - Human-readable action description
 * @param {string} [activeForm] - Optional shorter spinner text
 */
function _cuTodoDescribe(description, activeForm) {
    const current = _cuTasks.find(t => t.status === 'in_progress');
    if (current) {
        current.content = `Step ${_cuCurrentStep} — ${description}`;
        current.activeForm = activeForm || description;
    }
    todoTracker.updateTodos([..._cuTasks]);
}

/**
 * Mark all CU tasks completed and reset state.
 */
function _cuTodoComplete() {
    _cuTasks.forEach(t => { t.status = 'completed'; t.activeForm = null; });
    todoTracker.updateTodos([..._cuTasks]);
    _cuTasks = [];
    _cuCurrentStep = 0;
    _cuTotalSteps = 0;
}

/**
 * Build a human-readable description for a CU action
 * @param {string} action - Action type (left_click, type, key, scroll, etc.)
 * @param {string} params - Raw params string
 * @returns {string} Human-readable description
 */
function _cuActionLabel(action, params) {
    const p = params || '';
    switch (action) {
        case 'left_click': return `Click ${p}`;
        case 'right_click': return `Right-click ${p}`;
        case 'double_click': return `Double-click ${p}`;
        case 'triple_click': return `Triple-click ${p}`;
        case 'type': return `Type "${p.length > 50 ? p.slice(0, 50) + '...' : p}"`;
        case 'key': return `Press ${p}`;
        case 'scroll': return `Scroll ${p}`;
        case 'screenshot': return 'Take screenshot';
        case 'cursor_position': return `Move cursor ${p}`;
        case 'drag': return `Drag ${p}`;
        default: return `${action} ${p}`.trim();
    }
}

/**
 * Show/update the tool indicator as a fixed floating pill on document.body.
 * For CU provider, positions below the topbar instead of inside bubble.
 * @param {HTMLElement} bubble - Chat bubble element (unused, kept for API compat)
 * @param {string} toolName - Tool or action name
 * @param {string} detail - Additional detail text
 */
function _updateCUToolIndicator(bubble, toolName, detail) {
    // Skip the floating pill when the CU drawer is active — drawer shows the same info
    if (document.getElementById('cuDrawer')) {
        // Still forward to drawer callback
        if (window.__cuDrawerUpdateTool) window.__cuDrawerUpdateTool(toolName, detail?.length > 60 ? detail.substring(0, 60) + '...' : (detail || ''));
        return;
    }
    let indicator = document.getElementById('cuToolIndicator');
    if (!indicator) {
        indicator = document.createElement('div');
        indicator.id = 'cuToolIndicator';
        indicator.className = 'tool-indicator cu-fixed';
        document.body.appendChild(indicator);
    }
    const icons = {
        'computer': '\uD83D\uDDA5\uFE0F', 'screenshot': '\uD83D\uDCF8', 'bash': '\u26A1',
        'str_replace_based_edit_tool': '\u270F\uFE0F', 'text_editor': '\u270F\uFE0F',
        'left_click': '\uD83D\uDDB1\uFE0F', 'right_click': '\uD83D\uDDB1\uFE0F', 'double_click': '\uD83D\uDDB1\uFE0F',
        'type': '\u2328\uFE0F', 'key': '\u2328\uFE0F', 'scroll': '\u2195\uFE0F',
        'web_search': '\uD83C\uDF10', 'web_fetch': '\uD83C\uDF0D',
        'generate_image': '\uD83C\uDFA8', 'generate_video': '\uD83C\uDFAC',
        'processing': '\u2699\uFE0F'
    };
    const icon = icons[toolName] || '\uD83D\uDD27';
    const truncDetail = detail?.length > 60 ? detail.substring(0, 60) + '...' : (detail || '');
    indicator.innerHTML = `<span class="tool-icon">${icon}</span> <strong>${escapeHtml(toolName)}</strong>${truncDetail ? ` <span class="tool-detail">${escapeHtml(truncDetail)}</span>` : ''}`;
    indicator.classList.remove('hidden');
}

/**
 * Hide and remove the fixed CU tool indicator from document.body
 * @param {HTMLElement} bubble - Chat bubble element (unused, kept for API compat)
 */
function _hideCUToolIndicator(bubble) {
    const indicator = document.getElementById('cuToolIndicator');
    if (indicator) indicator.remove();
}

/**
 * Get or create the Live View panel inside a bubble.
 * Starts expanded so the latest screenshot is always visible.
 * Background-polls /browser/screenshot/live to stay fresh between SSE events.
 * @param {HTMLElement} bubble - The chat bubble element
 * @returns {HTMLElement} The Live View panel element
 */
let _cuLiveViewPollTimer = null;
let _cuLiveViewFetching = false;
const CU_LIVE_VIEW_POLL_MS = 3000; // 3 seconds between panel polls

function _ensureCULiveView(bubble) {
    let panel = bubble.querySelector('.cu-live-view-panel');
    if (panel) return panel;

    panel = document.createElement('div');
    panel.className = 'cu-live-view-panel'; // Start expanded (no 'collapsed')
    panel.innerHTML = `
        <div class="cu-live-view-header" data-bubble-action="cu-live-view-toggle">
            <span class="cu-live-dot pulse"></span>
            <span class="cu-live-view-label">Live View</span>
            <span class="cu-live-view-step">Step 0</span>
            <span class="cu-live-view-toggle">\u25BC</span>
        </div>
        <div class="cu-live-view-content">
            <img class="cu-live-view-img" />
        </div>
    `;

    // Insert before bubble-controls (after streaming-response-panel / cu-activity-panel)
    const controls = bubble.querySelector('.bubble-controls');
    if (controls) {
        bubble.insertBefore(panel, controls);
    } else {
        bubble.appendChild(panel);
    }

    // Start background polling to keep the screenshot fresh
    _startCULiveViewPolling(panel);

    return panel;
}

/**
 * Background-poll /browser/screenshot/live to keep the Live View panel image current.
 * Skips fetching when the interactive modal is open (it has its own faster poll).
 */
function _startCULiveViewPolling(panel) {
    // Clear any existing timer
    _stopCULiveViewPolling();

    // createPoller: paused while the page is hidden, never overlapping.
    _cuLiveViewPollTimer = createPoller(async () => {
        // Skip if interactive modal is open (it polls at 1.5s already)
        if (isInteractOpen()) return;
        // Skip if panel was marked complete (session ended)
        if (panel.classList.contains('cu-complete')) {
            _stopCULiveViewPolling();
            return;
        }
        // Skip if already fetching
        if (_cuLiveViewFetching) return;

        _cuLiveViewFetching = true;
        try {
            const res = await fetch('/browser/screenshot/live');
            if (!res.ok) return;
            const data = await res.json();
            if (data.url) {
                const img = panel.querySelector('.cu-live-view-img');
                if (img) {
                    img.src = data.url + '?t=' + data.timestamp;
                }
            }
        } catch (e) {
            // Silently ignore polling errors
        } finally {
            _cuLiveViewFetching = false;
        }
    }, { intervalMs: CU_LIVE_VIEW_POLL_MS, immediate: false });
    _cuLiveViewPollTimer.start();
}

function _stopCULiveViewPolling() {
    if (_cuLiveViewPollTimer) {
        _cuLiveViewPollTimer.stop();
        _cuLiveViewPollTimer = null;
    }
}

/**
 * Toggle the CU Live View panel open/closed
 */
window.toggleCULiveView = function(header) {
    const panel = header.closest('.cu-live-view-panel');
    if (!panel) return;
    panel.classList.toggle('collapsed');
    const toggle = panel.querySelector('.cu-live-view-toggle');
    if (toggle) {
        toggle.textContent = panel.classList.contains('collapsed') ? '\u25B6' : '\u25BC';
    }
};

/**
 * Create a bubble with thinking content for history restoration
 * Used when loading saved conversations that include AI reasoning
 * @param {string} content - Response content
 * @param {string} thinking - Thinking/reasoning content
 * @param {Array} [cuLog] - Optional Computer Use activity log for restoration
 * @returns {HTMLElement} Complete bubble element with thinking panel
 */
export function createBubbleWithThinking(content, thinking, cuLog) {
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant";

    // Calculate stats for thinking content
    const thinkingText = thinking || '';
    const wordCount = thinkingText.split(/\s+/).filter(w => w.length > 0).length;

    bubble.innerHTML = `
        <div class="streaming-thinking-panel reasoning-complete collapsed">
            <div class="reasoning-header" data-bubble-action="thinking-toggle">
                <span class="reasoning-icon">\u2713</span>
                <span class="reasoning-label">Response Complete</span>
                <span class="reasoning-stats">
                    <span class="thinking-word-count">${wordCount}</span> <span class="stats-label">words</span>
                </span>
                <div class="reasoning-actions">
                    <button class="reasoning-btn tts-btn" title="Listen to reasoning" data-bubble-action="thinking-speak">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                            <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                        </svg>
                    </button>
                    <button class="reasoning-btn toggle-btn" data-bubble-action="thinking-toggle">
                        <span class="reasoning-toggle">\u25B6</span>
                    </button>
                </div>
            </div>
            <div class="thinking-content"></div>
        </div>
        <div class="streaming-response-panel">
            <div class="response-content"></div>
        </div>
        <div class="bubble-controls">
            <button class="bubble-btn speak-btn" title="Generate audio" aria-pressed="false">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                    <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                    <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
                </svg>
            </button>
            <button class="bubble-btn copy-btn" title="Copy text">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                </svg>
            </button>
        </div>
    `;

    // Populate thinking content with markdown rendering (code is highlighted
    // while parsing: these are final renders)
    const thinkingContent = bubble.querySelector('.thinking-content');
    if (thinkingContent && thinking) {
        thinkingContent.innerHTML = renderMarkdown(thinking);
    }

    // Populate response content with markdown rendering
    const responseContent = bubble.querySelector('.response-content');
    if (responseContent && content) {
        responseContent.innerHTML = renderMarkdown(content);

        // Apply syntax highlighting and snapshot click handling
        setTimeout(() => {
            if (typeof makeSnapshotsClickable === 'function') {
                makeSnapshotsClickable(responseContent);
            }
            if (typeof applySyntaxHighlighting === 'function') {
                applySyntaxHighlighting(responseContent);
            }
        }, 0);
    }

    // Rebuild CU activity panel from saved log (if present)
    if (cuLog && cuLog.length > 0) {
        const cuPanel = document.createElement('div');
        cuPanel.className = 'cu-activity-panel collapsed';
        const lastStep = cuLog.filter(e => e.step).map(e => e.step).pop() || 0;
        cuPanel.innerHTML = `
            <div class="cu-activity-header" data-bubble-action="cu-panel-toggle">
                <span class="cu-live-dot">\u2713</span>
                <span class="cu-activity-label">Computer Use Complete</span>
                <span class="cu-step-counter">${lastStep ? 'Step ' + escapeHtml(lastStep) : ''}</span>
                <span class="cu-activity-toggle">\u25B6</span>
            </div>
            <div class="cu-activity-body"></div>
        `;
        const cuBody = cuPanel.querySelector('.cu-activity-body');
        // Find the last screenshot for the preview
        const lastSS = [...cuLog].reverse().find(e => e.type === 'screenshot');
        if (lastSS && cuBody) {
            const preview = document.createElement('img');
            preview.className = 'cu-live-preview';
            preview.src = lastSS.url;
            preview.alt = `Final screenshot`;
            preview.onclick = () => openCUInteract(lastSS.url);
            cuBody.appendChild(preview);
        }
        // Build a gallery of all screenshots
        const screenshots = cuLog.filter(e => e.type === 'screenshot');
        if (screenshots.length > 1 && cuBody) {
            const gallery = document.createElement('div');
            gallery.className = 'cu-gallery';
            screenshots.forEach(ss => {
                const thumb = document.createElement('img');
                thumb.className = 'cu-gallery-thumb';
                thumb.src = ss.url;
                thumb.alt = `Step ${ss.step}`;
                thumb.onclick = () => openCUInteract(ss.url);
                gallery.appendChild(thumb);
            });
            cuBody.appendChild(gallery);
        }
        // Rebuild action/bash/file_edit entries
        cuLog.forEach(entry => {
            if (!cuBody) return;
            if (entry.type === 'action') {
                const line = document.createElement('div');
                line.className = 'cu-action-line';
                line.innerHTML = `<strong>${escapeHtml(entry.action)}</strong> <span>${escapeHtml(entry.params || '')}</span>`;
                cuBody.appendChild(line);
            } else if (entry.type === 'bash') {
                const block = document.createElement('div');
                block.className = 'cu-bash-block';
                const cmd = entry.command || '';
                const out = entry.output || '';
                const code = entry.exit_code;
                block.innerHTML = `
                    <div class="cu-bash-header">
                        <span>$ ${escapeHtml(cmd.length > 120 ? cmd.slice(0, 120) + '...' : cmd)}</span>
                        <span class="cu-bash-exit ${code === 0 ? 'success' : 'error'}">exit ${escapeHtml(code)}</span>
                    </div>
                    <pre class="cu-bash-output">${escapeHtml(out.length > 1500 ? out.slice(0, 1500) + '\n... [truncated]' : out)}</pre>
                `;
                cuBody.appendChild(block);
            } else if (entry.type === 'file_edit') {
                const line = document.createElement('div');
                line.className = 'cu-action-line cu-file-edit';
                line.innerHTML = `<strong>${escapeHtml(entry.command)}</strong> <code>${escapeHtml(entry.path)}</code>`;
                cuBody.appendChild(line);
            }
        });
        // Insert CU panel before response panel
        const rp = bubble.querySelector('.streaming-response-panel');
        if (rp) {
            bubble.insertBefore(cuPanel, rp);
        } else {
            bubble.appendChild(cuPanel);
        }
    }

    // Setup button handlers
    setupBubbleControls(bubble, content);

    return bubble;
}

/**
 * Append thinking content to bubble
 * @param {HTMLElement} bubble - Bubble element
 * @param {string} text - Thinking text to append
 */
export function appendThinkingContent(bubble, text) {
    // Update floating panel content (a text node: linear, never re-parsed)
    const floatingPanel = bubble._floatingPanel;
    if (floatingPanel) {
        const floatingContent = floatingPanel.querySelector('.thinking-content');
        if (floatingContent) {
            floatingContent.appendChild(document.createTextNode(text));
            floatingContent.scrollTop = floatingContent.scrollHeight;
        }
    }

    // Also update inline panel for when it returns
    const thinkingContent = bubble.querySelector('.thinking-content');
    if (thinkingContent) {
        thinkingContent.appendChild(document.createTextNode(text));
        thinkingContent.scrollTop = thinkingContent.scrollHeight;
    }

    updateThinkingStats(bubble);
}

/**
 * Remove this turn's floating reasoning panel (no-op when already gone).
 * @param {HTMLElement} bubble - Bubble element
 * @param {boolean} [animate=false] - Fade out first (the normal completion path)
 */
function removeFloatingPanel(bubble, animate = false) {
    const floatingPanel = bubble && bubble._floatingPanel;
    if (!floatingPanel) return;
    bubble._floatingPanel = null;
    if (animate) {
        floatingPanel.classList.add('floating-exit');
        setTimeout(() => floatingPanel.remove(), 300);
    } else {
        floatingPanel.remove();
    }
}

/**
 * Update thinking stats display
 * @param {HTMLElement} bubble - Bubble element
 */
function updateThinkingStats(bubble) {
    // Get content from floating panel
    const floatingPanel = bubble._floatingPanel;
    const floatingContent = floatingPanel?.querySelector('.thinking-content');
    const text = floatingContent?.textContent || '';
    const wordCount = text.trim() ? text.trim().split(/\s+/).length : 0;

    // Update floating panel stats
    if (floatingPanel) {
        const wordCountEl = floatingPanel.querySelector('.thinking-word-count');
        if (wordCountEl) {
            wordCountEl.textContent = wordCount.toLocaleString();
        }
    }

    // Also update inline panel stats
    const inlinePanel = bubble.querySelector('.streaming-thinking-panel');
    if (inlinePanel) {
        const wordCountEl = inlinePanel.querySelector('.thinking-word-count');
        if (wordCountEl) {
            wordCountEl.textContent = wordCount.toLocaleString();
        }
    }
}

/**
 * Append response content to bubble during streaming
 * NOTE: Syntax highlighting is deferred to finalizeStreamingBubble for smooth
 * scrolling: the coalesced renders here are streaming renders, which never
 * run highlight.js
 * @param {HTMLElement} bubble - Bubble element
 * @param {string} text - Text to append
 * @param {string|null} fullResponse - Full response for markdown rendering
 */
export function appendResponseContent(bubble, text, fullResponse = null) {
    const responseContent = bubble.querySelector('.response-content');
    if (!responseContent) return;

    if (fullResponse !== null) {
        // Coalesce: at most one full markdown + sanitize render per animation
        // frame, of the latest text. Rendering on every token re-parsed the
        // whole reply each time and stalled phones on long answers (audit
        // P01-chat-core-web-11). finalizeStreamingBubble renders the final state.
        bubble._pendingRender = fullResponse;
        if (!bubble._renderFrame) {
            bubble._renderFrame = requestFrame(() => {
                bubble._renderFrame = null;
                const latest = bubble._pendingRender;
                bubble._pendingRender = null;
                if (latest === null || latest === undefined) return;
                responseContent.innerHTML = renderMarkdown(latest, { streaming: true });
                // Smooth scroll to bottom during streaming
                scrollToBottomIfNeeded();
            });
        }
        return;
    }

    responseContent.innerHTML += escapeHtml(text);

    // Smooth scroll to bottom during streaming
    scrollToBottomIfNeeded();
}

/**
 * requestAnimationFrame, or a ~16 ms timer where there is none.
 * @param {Function} fn - The frame callback
 * @returns {() => void} Cancels it, with the API that scheduled it (frame ids
 *   and timer ids are separate counters: clearTimeout(frameId) cancels some
 *   unrelated timer that shares the number)
 */
function requestFrame(fn) {
    if (typeof requestAnimationFrame === 'function') {
        const id = requestAnimationFrame(fn);
        return () => cancelAnimationFrame(id);
    }
    const id = setTimeout(fn, 16);
    return () => clearTimeout(id);
}

/**
 * Drop a coalesced render that has not run yet (the final render, or the
 * bubble's removal, supersedes it).
 * @param {HTMLElement} bubble - Bubble element
 */
function cancelPendingRender(bubble) {
    if (!bubble || !bubble._renderFrame) return;
    bubble._renderFrame();
    bubble._renderFrame = null;
    bubble._pendingRender = null;
}

/**
 * Mark thinking as complete
 * @param {HTMLElement} bubble - Bubble element
 */
export function markThinkingComplete(bubble) {
    // Get floating panel and copy its content to inline panel
    const floatingPanel = bubble._floatingPanel;
    const inlinePanel = bubble.querySelector('.streaming-thinking-panel');

    if (floatingPanel && inlinePanel) {
        // Copy content from floating to inline
        const floatingContent = floatingPanel.querySelector('.thinking-content');
        const inlineContent = inlinePanel.querySelector('.thinking-content');
        if (floatingContent && inlineContent) {
            inlineContent.innerHTML = floatingContent.innerHTML;
        }

        // Copy word count
        const floatingWordCount = floatingPanel.querySelector('.thinking-word-count');
        const inlineWordCount = inlinePanel.querySelector('.thinking-word-count');
        if (floatingWordCount && inlineWordCount) {
            inlineWordCount.textContent = floatingWordCount.textContent;
        }

        // Remove floating panel with fade animation
        removeFloatingPanel(bubble, true);
    }

    // Show the inline (collapsed) reasoning panel. Note: The Signal line is left
    // running here — thinking_end is mid-turn, and the line keeps narrating
    // generation / tools / mint until stream_end dissolves it.
    if (inlinePanel) {
        inlinePanel.style.display = '';
        inlinePanel.classList.remove('reasoning-active', 'floating');
        inlinePanel.classList.add('reasoning-complete', 'collapsed');
    }

    // Update icon to checkmark
    const icon = inlinePanel?.querySelector('.reasoning-icon');
    if (icon) {
        icon.textContent = '\u2713';
    }

    // Update label to "Response Complete"
    const label = inlinePanel?.querySelector('.reasoning-label');
    if (label) {
        label.textContent = 'Response Complete';
    }

    // Update toggle arrow
    const toggle = inlinePanel?.querySelector('.reasoning-toggle');
    if (toggle) {
        toggle.textContent = '\u25B6';
    }
}

/**
 * Finalize streaming bubble with full content
 * @param {HTMLElement} bubble - Bubble element
 * @param {string} fullResponse - Full response text
 * @param {string} fullThinking - Full thinking text
 * @param {{wellness?: boolean}} [opts] - wellness: a health-data tool fed this reply
 */
export function finalizeStreamingBubble(bubble, fullResponse, fullThinking, { wellness = false } = {}) {
    console.log('[FINALIZE] finalizeStreamingBubble called, response length:', fullResponse?.length);

    if (fullResponse && fullResponse.length > 0) {
        const responsePreview = fullResponse.substring(0, 50).replace(/\n/g, ' ');
        // A reply a wellness tool fed can carry health values, and inside the
        // app this is a native notification (title and body, lock screen
        // included): it says only that the reply is ready (rule W33).
        showNotification("\uD83D\uDCAC AI Response Ready", {
            body: wellness ? 'Your reply is ready.' : responsePreview + (fullResponse.length > 50 ? '...' : ''),
            tag: "chat-complete",
            operator: getOperator()
        });
    }

    // The final render below supersedes any coalesced one still pending.
    cancelPendingRender(bubble);

    const responseContent = bubble.querySelector('.response-content');
    console.log('[FINALIZE] Found responseContent:', !!responseContent, 'fullResponse:', !!fullResponse);

    if (responseContent && fullResponse) {
        // CU panel is a sibling of .streaming-response-panel, not inside .response-content,
        // so this innerHTML overwrite won't destroy it. The final render
        // highlights code while parsing.
        responseContent.innerHTML = renderMarkdown(fullResponse);
        console.log('[FINALIZE] Rendered markdown, innerHTML length:', responseContent.innerHTML.length);

        if (typeof makeSnapshotsClickable === 'function') {
            console.log('[FINALIZE] Calling makeSnapshotsClickable (first)...');
            makeSnapshotsClickable(responseContent);
        }

        if (typeof applySyntaxHighlighting === 'function') {
            console.log('[FINALIZE] Calling applySyntaxHighlighting (second)...');
            applySyntaxHighlighting(responseContent);
        }
    }

    const thinkingContent = bubble.querySelector('.thinking-content');
    if (thinkingContent && fullThinking) {
        thinkingContent.innerHTML = renderMarkdown(fullThinking);

        if (typeof makeSnapshotsClickable === 'function') {
            makeSnapshotsClickable(thinkingContent);
        }

        if (typeof applySyntaxHighlighting === 'function') {
            applySyntaxHighlighting(thinkingContent);
        }
    }

    if (!fullThinking) {
        const panel = bubble.querySelector('.streaming-thinking-panel');
        if (panel) panel.classList.add('hide');
    }

    // Mark CU activity panel as complete (if present)
    const cuPanel = bubble.querySelector('.cu-activity-panel');
    if (cuPanel) {
        const dot = cuPanel.querySelector('.cu-live-dot');
        if (dot) { dot.classList.remove('pulse'); dot.textContent = '\u2713'; }
        const label = cuPanel.querySelector('.cu-activity-label');
        if (label) label.textContent = 'Computer Use Complete';
        cuPanel.classList.add('collapsed');
        const toggle = cuPanel.querySelector('.cu-activity-toggle');
        if (toggle) toggle.textContent = '\u25B6';
    }

    // Mark Live View panel as complete (if present) and stop background polling
    const liveViewPanel = bubble.querySelector('.cu-live-view-panel');
    if (liveViewPanel) {
        liveViewPanel.classList.add('cu-complete');
        _stopCULiveViewPolling();
        const lvDot = liveViewPanel.querySelector('.cu-live-dot');
        if (lvDot) { lvDot.classList.remove('pulse'); }
        const lvLabel = liveViewPanel.querySelector('.cu-live-view-label');
        if (lvLabel) lvLabel.textContent = 'Live View Complete';
    }

    // Clear CU active state (no longer need refresh persistence)
    clearCUState();

    // Remove fixed tool indicator from body
    _hideCUToolIndicator(bubble);

    bubble.classList.remove('streaming-bubble');
    setupBubbleControls(bubble, fullResponse);

    // Snap to bottom now that streaming is complete
    snapToBottom();

    // Note: Auto-TTS is now triggered explicitly in the stream completion handler
    // (sendStreamingChat processChunk result.done) after this function returns.
    // This ensures the bubble is fully finalized before TTS generation starts.
}

/**
 * Setup bubble controls (copy/speak buttons)
 * @param {HTMLElement} bubble - Bubble element
 * @param {string} text - Text content for buttons
 */
function setupBubbleControls(bubble, text) {
    // Long-press-to-copy (chat-bubbles.js) reads this — keeps the raw markdown
    // available to the touch path exactly like the copy button below.
    bubble._bbxCopyText = text;

    const copyBtn = bubble.querySelector('.copy-btn');
    if (copyBtn) {
        copyBtn.onclick = () => {
            navigator.clipboard.writeText(text).then(() => toast('Copied!')).catch(() => toast('Copy failed'));
        };
    }

    const speakBtn = bubble.querySelector('.speak-btn');
    if (speakBtn) {
        speakBtn.onclick = () => speakToBubble(text, bubble, speakBtn);
    }
}

// =============================================================================
// Streaming Chat (SSE)
// =============================================================================

/**
 * Which failures of a /chat/stream turn may be re-sent through POST /chat.
 *
 * Only a turn the server never accepted: the request never got a response
 * (fetch rejected) or the server refused the stream with an HTTP error. Once
 * it answered 200 the turn is running server-side -- its provider call is
 * billed and its tools (image/video/music jobs, messages, device control) may
 * already have acted -- so re-sending it would run them twice (audit
 * P01-chat-core-web-18). Those failures drop the partial bubble and offer the
 * retry chip instead, which the operator chooses to press.
 * @param {*} err - What sendStreamingChat rejected with
 * @returns {boolean}
 */
export function shouldFallBackToStandard(err) {
    return !!err && (err.stage === 'request' || err.stage === 'http');
}

/** An Error tagged with the stage of the stream it failed in (see shouldFallBackToStandard). */
function streamFailure(stage, cause, status) {
    const message = stage === 'stalled'
        ? 'Stream stalled: no data received for 5 minutes'
        : (cause && cause.message ? cause.message : String(cause || 'stream failed'));
    const err = new Error(message);
    err.stage = stage;
    err.cause = cause;
    if (status !== undefined) err.status = status;
    return err;
}

/**
 * Tidy the UI after a failed stream: the Signal line, any pending render and
 * the floating reasoning panel go; the partial bubble is removed (a Computer
 * Use bubble stays, finalized, because the CU task keeps running server-side
 * and its live view is the only progress the operator has).
 * @param {HTMLElement} bubble - The turn's streaming bubble
 * @param {string} provider - The turn's provider
 * @param {Error} err - The tagged failure
 */
function cleanupFailedStream(bubble, provider, err) {
    if (bubble._signalLine) { bubble._signalLine.destroy(); bubble._signalLine = null; } // no leaked rAF
    cancelPendingRender(bubble);
    removeFloatingPanel(bubble);
    _hideCUToolIndicator(bubble);
    setStreamingState(false);
    if (provider === 'computer-use') {
        _cuStreamActive = false;
        _cuQueueAppendMode = false;
        if (!shouldFallBackToStandard(err) && bubble.parentNode) {
            bubble.classList.remove('streaming-bubble');
            _stopCULiveViewPolling();
            return;
        }
    }
    if (bubble.parentNode) bubble.parentNode.removeChild(bubble);
}

/**
 * Send chat message via streaming SSE
 * @param {Array} messages - Messages array
 * @param {string} provider - AI provider
 * @param {string} model - Model ID
 * @param {string} operator - Operator name
 * @returns {Promise<Object>} Response with thinking, content and usage. Rejects
 *   with an Error whose .stage says where it failed: 'request' (no response),
 *   'http' (the server refused the stream), 'stream' (died after the server
 *   answered) or 'stalled' (no bytes for 300 s). The UI is already tidied.
 */
export async function sendStreamingChat(messages, provider, model, operator) {
    const bubble = createStreamingThinkingBubble();
    const hist = $("history");
    if (hist) {
        hist.appendChild(bubble);
        scrollToBottomIfNeeded();
    }

    // CU doesn't need the reasoning panel or The Signal — the tool indicator,
    // todo panel, and Live View carry the activity narrative instead.
    if (provider === 'computer-use') {
        removeFloatingPanel(bubble);
        if (bubble._signalLine) { bubble._signalLine.destroy(); bubble._signalLine = null; }
        const inlinePanel = bubble.querySelector('.streaming-thinking-panel');
        if (inlinePanel) inlinePanel.style.display = 'none';
        // Initialize TodoTracker for CU session
        todoTracker.init('todoPanel', 'cu-' + Date.now());
    }

    // The Signal controller for this turn (null for CU, destroyed above).
    const signal = bubble._signalLine;
    // Tracks the most recent tool_start name so tool_result can echo it as done.
    let signalLastTool = "";
    // Push "generating · <model>" exactly once when the answer starts streaming.
    let signalGenerating = false;
    // Honest generating label = the REAL resolved model id. The `model` arg is
    // often "" (window.__model unset), so this is refined from the stream_start
    // frame's `model` field below; provider is only a last-ditch fallback.
    let signalModelLabel = model || provider || 'model';

    let fullThinking = "";
    let fullResponse = "";
    let thinkingStarted = false;
    let provenance = null;
    let cuLog = [];  // Computer Use session log for persistence
    // SSE 'error' event text — accumulated so send() can detect an ERROR-ONLY
    // stream (transport OK, no reply) and surface the failure + retry chip.
    let streamErrorText = "";
    // The turn's token usage: the LAST 'usage' event wins (Android parity,
    // ChatViewModel's onMeta), sent to /chat/save as {prompt, completion}.
    let usage = null;
    // M7 ledger rule (spec §3.7): the turn key stream_start names (echoed to
    // /chat/save) and the turn's UI-only wellness_used frames, merged (the
    // reply's wellness_ref and its "Used your health data" footer).
    let wellnessTurn = null;
    let wellnessUsed = null;

    // NOTE: We no longer use streaming TTS (real-time sentence-by-sentence).
    // Auto-TTS now waits for the complete response and generates a single TTS call.
    // This is more reliable and avoids audio gaps, cut-offs, and wasted API calls.
    // The triggerAutoTTS function in finalizeStreamingBubble handles this.

    const body = JSON.stringify({
        messages: messages,
        provider: provider,
        model: model,
        operator: operator,
        ...(provider === 'computer-use' && {
            session_id: getCUSessionId() || undefined,
            device_id: getCUDeviceId() || 'blackbox'
        })
    });

    // ── M7 stall watchdog (mirrors Android M7.1a's 300s stream read
    // timeout, = the server's provider-leg httpx timeout). The server
    // emits ": keepalive" SSE comment frames every ~10s during provider
    // prefill silence, so ANY healthy stream delivers bytes at least every
    // ~10s. If NOTHING arrives for 300s the stream is dead: the request is
    // aborted and the turn fails instead of leaving this bubble hanging.
    const STALL_TIMEOUT_MS = 300000;
    const controller = new AbortController();
    let stallTimer = null;
    let stalled = false;
    const armStallWatchdog = () => {
        if (stallTimer) clearTimeout(stallTimer);
        stallTimer = setTimeout(() => {
            stalled = true;
            console.error('[STREAM] Stall watchdog: no stream data for 300s — aborting');
            controller.abort();
        }, STALL_TIMEOUT_MS);
    };

    const handleEvent = (eventType, eventData) => {
        // Once a wellness tool answered, this turn's text can carry health
        // values, and the app's WebView console goes to logcat: the events are
        // logged without their payloads from then on.
        const quiet = wellnessUsed !== null;
        try {
            const data = JSON.parse(eventData);

            // Debug: Log ALL event types received
            console.log(`[STREAM DEBUG] Event type: ${eventType}`,
                quiet ? `(${String(eventData).length} chars)` : (typeof data === 'string' ? data.substring(0, 100) : data));

            switch (eventType) {
                case 'stream_start':
                    console.log('[STREAM] Started:', data);
                    setStreamingState(true); // Enable smooth scrolling
                    if (provider === 'computer-use') _cuStreamActive = true;
                    // The server resolves the real model id here — use it for
                    // the honest "generating · <model>" line (not the provider).
                    if (data.model) signalModelLabel = data.model;
                    if (data.provenance) {
                        provenance = data.provenance;
                        console.log('[STREAM] Provenance:', provenance);
                    }
                    if (typeof data.wellness_turn === 'string' && WELLNESS_TURN_KEY.test(data.wellness_turn)) {
                        wellnessTurn = data.wellness_turn;
                    }
                    break;

                case 'wellness_used': {
                    // UI-only, like The Signal (never a prompt, never the
                    // ledger): a wellness tool answered with health data. The
                    // reply being built keeps the ref (the history scrub, Show
                    // numbers) and gets the "Used your health data" footer.
                    const merged = mergeWellnessUsed(wellnessUsed, data);
                    if (merged) {
                        if (!merged.source_label) merged.source_label = lastSourceLabel() || null;
                        wellnessUsed = merged;
                        decorateWellnessBubble(bubble, { content: '', wellness_used: wellnessUsed });
                    }
                    break;
                }

                case 'thinking_start':
                    thinkingStarted = true;
                    // Panel stays collapsed - user can expand if they want to see reasoning
                    break;

                case 'thinking':
                    if (typeof data === 'string') {
                        fullThinking += data;
                        appendThinkingContent(bubble, data);
                    }
                    break;

                case 'thinking_end':
                    markThinkingComplete(bubble);
                    break;

                // ─── The Signal — presentation-only telemetry HUD ───
                // These three cases drive the morphing red line ONLY.
                // HARD RULE: they must NEVER append to fullThinking /
                // fullResponse or feed the /chat/save payload — this
                // telemetry is display-only and is never persisted.
                case 'system_activity':
                    // data = { stage, label, detail, seq } (see
                    // Orchestrator/telemetry_events.py). We render only the label.
                    if (signal && data && typeof data.label === 'string') {
                        signal.push(data.label);
                    }
                    break;

                case 'tool_start': {
                    // data = tool name (string) per chat_routes.py stream.
                    const toolName = (typeof data === 'string')
                        ? data
                        : (data && (data.name || data.tool)) || '';
                    signalLastTool = toolName;
                    if (signal && toolName) signal.push('tool · ' + toolName);
                    break;
                }

                case 'tool_result': {
                    // data = a result-description string. Echo the last
                    // tool name as done; fall back to a trimmed description.
                    if (signal) {
                        if (signalLastTool) {
                            signal.push('tool · ' + signalLastTool + ' · ok');
                        } else if (typeof data === 'string' && data) {
                            signal.push('tool · ' + (data.length > 40 ? data.slice(0, 40) : data));
                        }
                    }
                    break;
                }

                case 'content_start':
                    if (signal && !signalGenerating) {
                        signal.push('generating · ' + signalModelLabel);
                        signalGenerating = true;
                    }
                    break;

                case 'content':
                    if (typeof data === 'string') {
                        // First answer bytes → narrate generation once. On a
                        // silent turn (no system_activity) this is the only
                        // honest line the HUD shows before it dissolves.
                        if (signal && !signalGenerating) {
                            signal.push('generating · ' + signalModelLabel);
                            signalGenerating = true;
                        }
                        fullResponse += data;
                        appendResponseContent(bubble, data, fullResponse);
                        // Note: Auto-TTS is now triggered after complete response
                        // See processChunk result.done handler
                    }
                    break;

                case 'usage':
                    console.log('[STREAM] Usage:', data);
                    if (data && typeof data === 'object') usage = data;
                    break;

                case 'image_task': {
                    console.log('[STREAM] Image generation task:', data);

                    // Track with task manager - it will handle the placeholder
                    const imgTaskId = data.task_id;
                    const imgPrompt = data.prompt;
                    const imgCount = data.count;
                    import('./task-manager.js').then(({ taskManager }) => {
                        taskManager.addTask(imgTaskId, 'image_generation', imgPrompt, null, imgCount);
                        console.log(`[STREAM] Tracking image generation: ${imgTaskId}`);
                    });
                    break;
                }

                case 'video_task': {
                    console.log('[STREAM] Video generation task:', data);

                    // Track with task manager - it will handle the placeholder
                    const vidTaskId = data.task_id;
                    const vidPrompt = data.prompt;
                    const vidDuration = data.duration;
                    const vidResolution = data.resolution;
                    import('./task-manager.js').then(({ taskManager }) => {
                        taskManager.addTask(vidTaskId, 'video_generation', vidPrompt, { duration: vidDuration, resolution: vidResolution });
                        console.log(`[STREAM] Tracking video generation: ${vidTaskId}`);
                    });
                    break;
                }

                case 'music_task': {
                    console.log('[STREAM] Music generation task:', data);

                    // Track with task manager
                    const musTaskId = data.task_id;
                    const musPrompt = data.prompt;
                    const musSampleCount = data.sample_count;
                    import('./task-manager.js').then(({ taskManager }) => {
                        taskManager.addTask(musTaskId, 'lyria_music', musPrompt, null, musSampleCount || 1);
                        console.log(`[STREAM] Tracking music generation: ${musTaskId}`);
                    });
                    break;
                }

                // ─── Computer Use Events ───
                case 'cu_screenshot': {
                    const ssUrl = data.url;
                    const ssStep = data.step || 0;
                    console.log(`[CU] Screenshot step ${ssStep}:`, ssUrl);
                    cuLog.push({ type: 'screenshot', url: ssUrl, step: ssStep });

                    // Persist CU state for refresh recovery
                    saveCUState(ssUrl, ssStep);

                    // Update the Live View panel (single image replacement)
                    const liveView = _ensureCULiveView(bubble);
                    const liveImg = liveView.querySelector('.cu-live-view-img');
                    if (liveImg) {
                        liveImg.src = ssUrl;
                        liveImg.alt = `Screenshot step ${ssStep}`;
                        // M4 routing (design 2026-07-23 §7.1): open the
                        // streaming client when this session has a live
                        // virtual display; the screenshot-poll modal is
                        // the automatic fallback (native mode / remote
                        // device / no-websockify box).
                        liveImg.onclick = () => {
                            import('./cu-viewer-route.js')
                                .then((m) => m.openCuViewer({
                                    sessionId: getCUSessionId() || undefined,
                                    deviceId: getCUDeviceId(),
                                    screenshotUrl: ssUrl
                                }))
                                .catch(() => openCUInteract(ssUrl));
                        };
                    }
                    const stepLabel = liveView.querySelector('.cu-live-view-step');
                    if (stepLabel) stepLabel.textContent = `Step ${ssStep}`;

                    // Also update the interactive modal if it's open
                    if (isInteractOpen()) {
                        updateCUInteractScreenshot(ssUrl, ssStep);
                    }

                    // Pulse the live dot
                    const liveDot = liveView.querySelector('.cu-live-dot');
                    if (liveDot) {
                        liveDot.classList.remove('pulse');
                        void liveDot.offsetWidth; // force reflow
                        liveDot.classList.add('pulse');
                    }

                    // Also update the activity panel step counter (if it exists)
                    const actPanel = bubble.querySelector('.cu-activity-panel');
                    if (actPanel) {
                        const counter = actPanel.querySelector('.cu-step-counter');
                        if (counter) counter.textContent = `Step ${ssStep}`;
                    }

                    _updateCUToolIndicator(bubble, 'screenshot', 'Step ' + ssStep);
                    _cuTodoDescribe('Capturing screenshot', 'Capturing screenshot');
                    scrollToBottomIfNeeded();
                    break;
                }

                case 'cu_action': {
                    const actAction = data.action || '';
                    const actParams = data.params || '';
                    console.log(`[CU] Action: ${actAction}`, actParams);
                    cuLog.push({ type: 'action', action: actAction, params: actParams, step: data.step });
                    const panel2 = _ensureCUPanel(bubble);
                    const body2 = panel2.querySelector('.cu-activity-body');
                    if (body2) {
                        const line = document.createElement('div');
                        line.className = 'cu-action-line';
                        line.innerHTML = `<strong>${escapeHtml(actAction)}</strong> <span>${escapeHtml(actParams)}</span>`;
                        body2.appendChild(line);
                    }
                    _updateCUToolIndicator(bubble, actAction, actParams);
                    _cuTodoDescribe(_cuActionLabel(actAction, actParams));
                    break;
                }

                case 'cu_bash_output': {
                    const bashCmd = data.command || '';
                    const bashOut = data.output || '';
                    const bashCode = data.exit_code;
                    console.log(`[CU] Bash (exit ${bashCode}):`, bashCmd);
                    cuLog.push({ type: 'bash', command: bashCmd, output: bashOut, exit_code: bashCode, step: data.step });
                    const panel3 = _ensureCUPanel(bubble);
                    const body3 = panel3.querySelector('.cu-activity-body');
                    if (body3) {
                        const block = document.createElement('div');
                        block.className = 'cu-bash-block';
                        block.innerHTML = `
                            <div class="cu-bash-header">
                                <span>$ ${escapeHtml(bashCmd.length > 120 ? bashCmd.slice(0, 120) + '...' : bashCmd)}</span>
                                <span class="cu-bash-exit ${bashCode === 0 ? 'success' : 'error'}">exit ${escapeHtml(bashCode)}</span>
                            </div>
                            <pre class="cu-bash-output">${escapeHtml(bashOut.length > 1500 ? bashOut.slice(0, 1500) + '\n... [truncated]' : bashOut)}</pre>
                        `;
                        body3.appendChild(block);
                    }
                    _updateCUToolIndicator(bubble, 'bash', bashCmd?.substring(0, 60));
                    const shortCmd = bashCmd?.length > 50 ? bashCmd.slice(0, 50) + '...' : bashCmd;
                    _cuTodoDescribe(`Run: ${shortCmd}`, `bash: ${shortCmd}`);
                    break;
                }

                case 'cu_file_edit': {
                    const feCmd = data.command || '';
                    const fePath = data.path || '';
                    console.log(`[CU] File edit: ${feCmd} ${fePath}`);
                    cuLog.push({ type: 'file_edit', command: feCmd, path: fePath, step: data.step });
                    const panel4 = _ensureCUPanel(bubble);
                    const body4 = panel4.querySelector('.cu-activity-body');
                    if (body4) {
                        const line = document.createElement('div');
                        line.className = 'cu-action-line cu-file-edit';
                        line.innerHTML = `<strong>${escapeHtml(feCmd)}</strong> <code>${escapeHtml(fePath)}</code>`;
                        body4.appendChild(line);
                    }
                    _updateCUToolIndicator(bubble, feCmd, fePath);
                    _cuTodoDescribe(`Edit: ${fePath}`, `${feCmd} ${fePath}`);
                    break;
                }

                case 'cu_step': {
                    console.log(`[CU] Step ${data.step}/${data.total}`);
                    const panel5 = _ensureCUPanel(bubble);
                    const counter = panel5.querySelector('.cu-step-counter');
                    if (counter) counter.textContent = `Step ${data.step}/${data.total}`;
                    // Pulse the live indicator
                    const dot = panel5.querySelector('.cu-live-dot');
                    if (dot) { dot.classList.remove('pulse'); void dot.offsetWidth; dot.classList.add('pulse'); }
                    _cuCurrentStep = data.step;
                    _cuTotalSteps = data.total || _cuTotalSteps;
                    _updateCUToolIndicator(bubble, 'processing', 'Step ' + data.step + '/' + data.total);
                    if (window.__cuDrawerUpdateStep) window.__cuDrawerUpdateStep(data.step, data.total);
                    if (window.__cuDrawerUpdateStatus) window.__cuDrawerUpdateStatus('running');
                    _cuTodoNewStep(data.step);
                    break;
                }

                case 'heartbeat': {
                    // Background task keepalive — update step counter
                    console.log(`[CU] Heartbeat: step ${data.step}/${data.total}`);
                    if (bubble) {
                        const hbPanel = bubble.querySelector('.cu-activity-panel');
                        if (hbPanel) {
                            const hbCounter = hbPanel.querySelector('.cu-step-counter');
                            if (hbCounter) hbCounter.textContent = `Step ${data.step}/${data.total}`;
                            const hbDot = hbPanel.querySelector('.cu-live-dot');
                            if (hbDot) { hbDot.classList.remove('pulse'); void hbDot.offsetWidth; hbDot.classList.add('pulse'); }
                        }
                    }
                    _cuCurrentStep = data.step;
                    _cuTotalSteps = data.total || _cuTotalSteps;
                    _updateCUToolIndicator(bubble, 'processing', 'Step ' + data.step + '/' + data.total);
                    // Only add a new step if we haven't seen it yet
                    if (!_cuTasks.find(t => t.id === `cu-step-${data.step}`)) {
                        _cuTodoNewStep(data.step);
                    }
                    _cuTodoDescribe('Running in background...', 'Background task');
                    break;
                }

                // ─── CU Session Management Events ───
                case 'cu_session':
                    console.log('[CU] Session ID:', data.session_id, 'Device:', data.device_id);
                    setCUSessionId(data.session_id);
                    if (window.__cuDrawerSetSession) window.__cuDrawerSetSession(data.session_id);
                    if (data.device_id && window.__cuDrawerSetDevice) window.__cuDrawerSetDevice(data.device_id);
                    break;

                case 'cu_queued':
                    console.log(`[CU] Prompt queued #${data.position}`);
                    toast(`Prompt queued (#${data.position})`);
                    if (window.__cuDrawerUpdateQueue) window.__cuDrawerUpdateQueue(data.position);
                    // Remove the empty assistant bubble — queued response will
                    // appear in the original stream's bubble via cu_queue_next
                    if (bubble && bubble.parentNode) {
                        bubble.remove();
                    }
                    // Remove the user's queued message bubble too — its text
                    // will appear as the separator in the original bubble.
                    // This keeps the original assistant bubble as the last element
                    // so auto-scroll works correctly.
                    {
                        const histEl = $('history');
                        if (histEl && histEl.lastElementChild) {
                            const lastEl = histEl.lastElementChild;
                            if (lastEl.classList.contains('user') || (lastEl.querySelector && lastEl.querySelector('.bubble-text'))) {
                                lastEl.remove();
                            }
                        }
                        // Remove from history data too (last user entry)
                        const hd = getHistoryData();
                        if (hd.length > 0 && hd[hd.length - 1].role === 'user') {
                            hd.pop();
                            setHistoryData(hd);
                            saveHistory();
                        }
                    }
                    break;

                case 'cu_stopped':
                    console.log('[CU] Task stopped:', data.reason);
                    if (window.__cuDrawerUpdateStatus) window.__cuDrawerUpdateStatus('stopped');
                    toast('Computer Use task stopped');
                    break;

                case 'cu_queue_next':
                    console.log(`[CU] Auto-dequeuing next prompt (${data.remaining} remaining)`);
                    if (window.__cuDrawerUpdateQueue) window.__cuDrawerUpdateQueue(data.remaining);
                    // Enter append mode — subsequent content streams into same bubble
                    _cuQueueAppendMode = true;
                    // Re-enable streaming state so scroll tracking works for new content
                    setStreamingState(true);
                    // Add visual separator with the queued prompt text
                    fullResponse += '\n\n---\n\n**\u27A1 Next:** ' + (data.prompt || '') + '\n\n';
                    appendResponseContent(bubble, '', fullResponse);
                    // Force scroll to the bubble since it's now the last element
                    if (bubble && bubble.scrollIntoView) {
                        bubble.scrollIntoView({ block: 'end', behavior: 'smooth' });
                    }
                    break;

                case 'done':
                    console.log('[STREAM] Done:', quiet ? '(payload not logged: a health-data turn)' : data);
                    if (data.thinking) {
                        fullThinking = _cuQueueAppendMode
                            ? (fullThinking + '\n\n' + data.thinking)
                            : data.thinking;
                    }
                    if (data.content) {
                        if (_cuQueueAppendMode) {
                            // Append mode: content already streamed into fullResponse
                            // via content events — don't overwrite
                            console.log('[CU] Queue append mode — preserving accumulated response');
                        } else {
                            fullResponse = data.content;
                        }
                    }
                    if (provider === 'computer-use' && window.__cuDrawerUpdateStatus) {
                        window.__cuDrawerUpdateStatus('complete');
                    }
                    break;

                case 'error':
                    console.error('[STREAM] Error:', data);
                    toast('Stream error: ' + data);
                    // Accumulate — an error-only stream (no response)
                    // must reach send()'s failure/retry handling instead
                    // of leaving a dangling empty streaming bubble.
                    streamErrorText += (streamErrorText ? '\n\n' : '') +
                        (typeof data === 'string' ? data : JSON.stringify(data));
                    break;

                case 'stream_end':
                    console.log('[STREAM] Stream ended');
                    // The answer is finalized — dissolve The Signal. If a
                    // mint follows (after /chat/save resolves), the caller
                    // re-animates this same line with the mint id, then
                    // dissolves again.
                    if (signal) signal.dissolve();
                    // Don't kill streaming state if another CU stream is still active
                    // (the queued request's short SSE stream ends before the original)
                    if (!_cuQueueAppendMode) {
                        setStreamingState(false);
                    }
                    if (provider === 'computer-use' && window.__cuDrawerUpdateStatus && !_cuQueueAppendMode) {
                        window.__cuDrawerUpdateStatus('complete');
                    }
                    break;
            }
        } catch (e) {
            console.warn('[STREAM] Parse error:', e, quiet ? '' : eventData);
        }
    };

    // Inside the BlackBox Android app the request is signed by the phone over
    // the exact body bytes (spec §6.3), so the box knows the enrolled phone
    // asked; a fresh proof per turn. Anywhere else (or a null from the app)
    // the request is exactly what it always was.
    const streamHeaders = wellnessSigner()
        ? await withOriginProof({ "Content-Type": "application/json" }, 'POST', '/chat/stream', body)
        : { "Content-Type": "application/json" };

    try {
        armStallWatchdog(); // covers a server that never answers or never sends a first chunk
        let response;
        try {
            response = await fetch("/chat/stream", {
                method: "POST",
                headers: streamHeaders,
                body: body,
                signal: controller.signal
            });
        } catch (err) {
            throw streamFailure(stalled ? 'stalled' : 'request', err);
        }
        if (!response.ok) {
            throw streamFailure('http', new Error(`Stream failed: ${response.status}`), response.status);
        }
        try {
            await parseSseStream(response.body.getReader(),
                ({ event, data }) => handleEvent(event, data),
                { onChunk: armStallWatchdog, signal: controller.signal }); // any bytes (keepalives too) reset the stall clock
        } catch (err) {
            throw streamFailure(stalled ? 'stalled' : 'stream', err);
        }
    } catch (err) {
        if (stallTimer) clearTimeout(stallTimer); // watchdog dies with the stream
        if (err.stage === 'stalled') toast('Stream stalled — no data received for 5 minutes.');
        console.error(`[STREAM] Stream failed (${err.stage}):`, err.cause || err);
        cleanupFailedStream(bubble, provider, err);
        throw err;
    }
    if (stallTimer) clearTimeout(stallTimer);

    // If this was a queued request (bubble was removed), resolve silently
    if (!bubble.parentNode && provider === 'computer-use') {
        console.log('[CU] Queued request stream ended — no bubble to finalize');
        return { thinking: '', response: '', provenance: provenance, cuLog: cuLog, queued: true };
    }

    markThinkingComplete(bubble);

    // CU: mark all todo steps completed and hide tool indicator
    if (provider === 'computer-use') {
        _cuTodoComplete();
        _hideCUToolIndicator(bubble);
        _cuQueueAppendMode = false;
        _cuStreamActive = false;
    }

    // Finalize the bubble first
    finalizeStreamingBubble(bubble, fullResponse, fullThinking, { wellness: wellnessUsed !== null });

    // Trigger Auto-TTS after response is complete (if enabled)
    // This generates a single TTS call for the full response
    if (window.autoTTSEnabled && window.triggerAutoTTS && fullResponse) {
        console.log('[STREAM] Response complete, triggering Auto-TTS');
        window.triggerAutoTTS(fullResponse, bubble);
    }

    return {
        thinking: fullThinking,
        response: fullResponse,
        provenance: provenance,
        cuLog: cuLog,
        errorText: streamErrorText,
        usage: usage,
        wellnessTurn: wellnessTurn,   // echoed to /chat/save (null: not a health-store operator)
        wellnessUsed: wellnessUsed,   // the reply's wellness_ref / footer (null: no health data used)
        bubbleEl: bubble  // lets send() remove/convert an error-only stream's empty bubble
    };
}

// =============================================================================
// Streaming Mode Toggle
// =============================================================================

/**
 * Set streaming mode
 * @param {boolean} enabled - Whether streaming is enabled
 */
export function setStreamingMode(enabled) {
    STREAMING_ENABLED = enabled;
    localStorage.setItem("bb_streaming_enabled", enabled ? "true" : "false");
    toast(enabled ? "AI Reasoning mode enabled - see thinking in real-time" : "AI Reasoning mode disabled");
}

/**
 * Get streaming mode state
 * @returns {boolean} Whether streaming is enabled
 */
export function isStreamingEnabled() {
    return STREAMING_ENABLED;
}

/**
 * Initialize streaming toggle
 */
export function initStreamingToggle() {
    const toggle = $("streamingToggle");
    if (toggle) {
        toggle.checked = STREAMING_ENABLED;
        toggle.onchange = () => {
            setStreamingMode(toggle.checked);
        };
    }
}

// =============================================================================
// Media Polling
// =============================================================================

/**
 * Update history when media is generated
 * @param {HTMLElement} bubble - Bubble element
 */
function updateHistoryWithMedia(bubble) {
    if (!bubble) return;

    const contentDiv = bubble.querySelector('.response-content') ||
                       bubble.querySelector('.bubble-text') ||
                       bubble.querySelector('span');
    if (!contentDiv) return;

    const updatedContent = contentDiv.innerHTML;
    const hist = $("history");
    if (!hist) return;

    const allBubbles = Array.from(hist.children);
    const bubbleIndex = allBubbles.indexOf(bubble);

    let assistantCount = 0;
    for (let i = 0; i <= bubbleIndex && i < allBubbles.length; i++) {
        if (allBubbles[i].classList.contains('assistant')) {
            assistantCount++;
        }
    }

    const historyData = getHistoryData();
    let historyAssistantCount = 0;
    for (let i = 0; i < historyData.length; i++) {
        if (historyData[i].role === 'assistant') {
            historyAssistantCount++;
            if (historyAssistantCount === assistantCount) {
                historyData[i].content = updatedContent;
                setHistoryData(historyData);
                saveHistory();
                console.log('[HISTORY] Updated history entry', i, 'with generated media');
                return;
            }
        }
    }
}

/**
 * Poll for video generation tasks
 */
export async function pollVideoTasks() {
    const placeholders = document.querySelectorAll('.video-loading-placeholder');

    for (const placeholder of placeholders) {
        const taskId = placeholder.getAttribute('data-task-id');
        if (!taskId) continue;

        try {
            const response = await fetch(`/tasks/status/${taskId}`);
            if (!response.ok) continue;

            const data = await response.json();

            if (data.status === 'completed' && data.result_url) {
                const parentBubble = placeholder.closest('.bubble');
                const taskOperator = data.result_data?.operator;
                const currentOperator = getOperator();
                const isOwnTask = !taskOperator || !currentOperator || taskOperator === currentOperator;

                const videoHTML = `<video src="${escapeAttr(data.result_url)}" controls preload="metadata" class="generated-video" style="max-width: 100%; border-radius: 8px; margin: 8px 0;">Your browser does not support the video tag.</video>`;
                placeholder.outerHTML = videoHTML;
                console.log('[VIDEO] Video generation completed:', data.result_url);

                if (isOwnTask) {
                    notifyTaskComplete('video generation', true, taskOperator);
                }

                updateHistoryWithMedia(parentBubble);
            } else if (data.status === 'failed') {
                placeholder.innerHTML = `<span style="color: #ff6b6b;">\u274C Video generation failed: ${escapeHtml(data.error_message || 'Unknown error')}</span>`;
                placeholder.classList.remove('video-loading-placeholder');
            }
        } catch (error) {
            console.error('[VIDEO] Error polling task:', taskId, error);
        }
    }
}

/**
 * Poll for image generation tasks
 */
export async function pollImageTasks() {
    const placeholders = document.querySelectorAll('.image-loading-placeholder');
    console.log('[DEBUG] pollImageTasks - Found placeholders:', placeholders.length);

    for (const placeholder of placeholders) {
        const taskId = placeholder.getAttribute('data-task-id');
        console.log('[DEBUG] Checking image task:', taskId);
        if (!taskId) continue;

        try {
            const response = await fetch(`/tasks/status/${taskId}`);
            console.log('[DEBUG] Image task response status:', response.status);
            if (!response.ok) continue;

            const data = await response.json();
            console.log('[DEBUG] Image task data:', data.status, data.result_url);

            if (data.status === 'completed' && data.result_url) {
                const parentBubble = placeholder.closest('.bubble');
                const parentElement = placeholder.parentElement;
                const taskOperator = data.result_data?.operator;
                const currentOperator = getOperator();
                const isOwnTask = !taskOperator || !currentOperator || taskOperator === currentOperator;

                // Built with DOM APIs: the URL never becomes markup or a CSS selector.
                const newImage = document.createElement('img');
                newImage.setAttribute('src', data.result_url);
                newImage.alt = 'Generated image';
                newImage.className = 'generated-image';
                newImage.style.cssText = 'max-width: 100%; border-radius: 8px; margin: 8px 0;';
                placeholder.replaceWith(newImage);

                if (parentElement) {
                    addDownloadButtonToImage(newImage, parentElement);
                }

                console.log('[IMAGE] Image generation completed:', data.result_url);

                if (isOwnTask) {
                    notifyTaskComplete('image generation', true, taskOperator);
                }

                updateHistoryWithMedia(parentBubble);
            } else if (data.status === 'failed') {
                placeholder.innerHTML = `<span style="color: #ff6b6b;">\u274C Image generation failed: ${escapeHtml(data.error_message || 'Unknown error')}</span>`;
                placeholder.classList.remove('image-loading-placeholder');
            }
        } catch (error) {
            console.error('[IMAGE] Error polling task:', taskId, error);
        }
    }
}

/**
 * Poll for music generation tasks
 */
export async function pollMusicTasks() {
    const placeholders = document.querySelectorAll('.music-loading-placeholder');

    for (const placeholder of placeholders) {
        const taskId = placeholder.getAttribute('data-task-id');
        if (!taskId) continue;

        try {
            const response = await fetch(`/tasks/status/${taskId}`);
            if (!response.ok) continue;

            const data = await response.json();

            if (data.status === 'completed' && data.result_url) {
                const parentBubble = placeholder.closest('.bubble');
                const taskOperator = data.result_data?.operator;
                const currentOperator = getOperator();
                const isOwnTask = !taskOperator || !currentOperator || taskOperator === currentOperator;

                const audioHTML = `<div class="audio-result" style="margin: 8px 0;">
                    <span class="audio-label" style="font-size: 13px; font-weight: 600; color: #27d980;">\uD83C\uDFB5 Generated Music</span>
                    <audio controls src="${escapeAttr(data.result_url)}" style="width: 100%; margin-top: 8px;"></audio>
                </div>`;
                placeholder.outerHTML = audioHTML;
                console.log('[MUSIC] Music generation completed:', data.result_url);

                if (isOwnTask) {
                    notifyTaskComplete('music generation', true, taskOperator);
                }

                updateHistoryWithMedia(parentBubble);
            } else if (data.status === 'failed') {
                placeholder.innerHTML = `<span style="color: #ff6b6b;">\u274C Music generation failed: ${escapeHtml(data.error_message || 'Unknown error')}</span>`;
                placeholder.classList.remove('music-loading-placeholder');
            }
        } catch (error) {
            console.error('[MUSIC] Error polling task:', taskId, error);
        }
    }
}

// =============================================================================
// Video Extension
// =============================================================================

/**
 * Add extend button to generated videos
 * @param {HTMLVideoElement} videoElement - Video element
 */
function addExtendButtonToVideo(videoElement) {
    // Check for both button class names to avoid duplicates
    if (videoElement.parentElement.querySelector('.extend-video-btn') ||
        videoElement.parentElement.querySelector('.btn-extend-video')) {
        return;
    }

    let videoUrl = videoElement.src;
    try {
        const url = new URL(videoUrl);
        videoUrl = url.pathname;
    } catch (e) {
        // Use as-is
    }

    const extendBtn = document.createElement('button');
    extendBtn.className = 'extend-video-btn btn';
    extendBtn.textContent = '\u2197 Extend Video';
    extendBtn.style.cssText = 'margin-top: 8px; font-size: 12px; padding: 6px 12px; background: #4a9eff; color: #fff; border: none;';

    extendBtn.addEventListener('click', () => {
        openVideoExtensionModal(videoUrl);
    });

    videoElement.parentElement.insertBefore(extendBtn, videoElement.nextSibling);
}

// =============================================================================
// History Rendering
// =============================================================================

/**
 * Render the stored history into #history: the ONE renderer (page load,
 * a Computer Use task finished in the background). A recent pending reply
 * comes back as a thinking bubble and a stale one is dropped; a reply keeps
 * its reasoning and Computer Use panels; a failed user turn keeps its retry
 * chip. chat-bubbles had a second renderer without the pending, reasoning
 * and CU handling (audit P01-chat-core-web-20).
 */
export function renderHistory() {
    const hist = $("history");
    if (!hist) return;

    hist.innerHTML = "";
    const historyData = getHistoryData();

    for (let i = 0; i < historyData.length; i++) {
        const b = historyData[i];

        // Handle pending messages
        if (b.pending && b.role === 'assistant') {
            const now = Date.now();
            const pendingAge = now - (b.pendingTimestamp || 0);
            const fiveMinutes = 5 * 60 * 1000;

            if (b.pendingTimestamp && pendingAge < fiveMinutes) {
                const thinkingBubble = createAnimatedThinkingBubble();
                hist.appendChild(thinkingBubble);
                console.log('[History] Restored recent pending message as thinking bubble');
                continue;
            } else {
                console.log('[History] Found stale pending message, removing');
                historyData.splice(i, 1);
                setHistoryData(historyData);
                saveHistory();
                i--;
                continue;
            }
        }

        // Check if this assistant message has thinking/reasoning or CU content.
        // A reply a wellness tool fed gets its footer back (and, inside the
        // app, Show numbers when its text is the masked copy); an entry without
        // the fields renders exactly as before.
        if (b.role === 'assistant' && (b.thinking || b.cuLog)) {
            console.log('[History] Restoring message with thinking/CU content');
            const bubbleWithThinking = createBubbleWithThinking(b.content, b.thinking, b.cuLog);
            hist.appendChild(bubbleWithThinking);
            decorateWellnessBubble(bubbleWithThinking, b);
        } else {
            const bubble = appendBubble(b.role, b.content);
            if (b.role === 'assistant' && bubble) decorateWellnessBubble(bubble, b);
            // Restore the retry affordance on user turns whose send failed
            if (b.role === 'user' && b.failed && bubble) {
                if (b.sendKey) bubble.dataset.sendKey = b.sendKey;
                if (b.failedAt) bubble.dataset.failedAt = String(b.failedAt);
                attachRetryChip(bubble, b.content);
            }
        }
    }

    scrollToBottomIfNeeded();
    updateHintsVisibility();
}

// =============================================================================
// CLI Agents (Claude Code, Gemini CLI)
// =============================================================================

/**
 * The CLI agents the composer sends to over their WebSocket, by provider:
 * their handler slot, names and session-folder prefix. send() had one ~180-line
 * branch per agent, differing only in these (audit P01-chat-core-web-20).
 */
const CLI_AGENTS = {
    'agents': {
        label: 'Claude Code',
        logPrefix: '[Agent]',
        connecting: 'Connecting to Claude Code CLI...',
        errorToast: 'Agent error',
        folderPrefix: 'session-',
        getHandler: getAgentHandler,
        createHandler() {
            const handler = new AgentChatHandler();
            setAgentHandler(handler);
            return handler;
        },
    },
    'gemini-agents': {
        label: 'Gemini CLI',
        logPrefix: '[GeminiAgent]',
        connecting: 'Connecting to Gemini CLI...',
        errorToast: 'Gemini agent error',
        folderPrefix: 'gemini-session-',
        getHandler: getGeminiAgentHandler,
        createHandler() {
            const handler = new GeminiAgentHandler();
            setGeminiAgentHandler(handler);
            return handler;
        },
    },
};

/**
 * The block appended to an agent prompt naming the files uploaded for it
 * (the agent reads them with its tools).
 * @param {Array<{filename: string, path: string}>} uploaded - The uploaded files
 * @returns {string}
 */
function attachedFilesBlock(uploaded) {
    let block = '\n\n--- ATTACHED FILES ---\n';
    block += `Session folder: ${uploaded[0].path.replace(/\/[^/]+$/, '')}\n\n`;
    block += 'Files uploaded to your session folder:\n';
    for (const f of uploaded) {
        block += `- ${f.filename}: ${f.path}\n`;
    }
    block += '\nYou can read, analyze, or manipulate these files using your tools (Read, analyze_image, analyze_audio, analyze_video, etc.).\n';
    block += '---\n';
    return block;
}

/**
 * Upload the composer's attachments into an agent's session folder, the
 * composer showing the progress (amber while uploading, green once done).
 * @param {string} txt - The typed prompt
 * @param {File[]} files - The attachments
 * @param {string} folderId - The session folder
 * @param {Object} agent - A CLI_AGENTS entry
 * @param {boolean} startsSession - Whether the prompt starts a session (wording)
 * @returns {Promise<{prompt: string, uploaded: Array}>} The prompt with the
 *   ATTACHED FILES block (when any file made it) and the uploaded files
 */
async function uploadToAgentSession(txt, files, folderId, agent, startsSession) {
    const promptEl = $("prompt");
    const uploaded = [];
    console.log(`${agent.logPrefix} Uploading ${files.length} file(s) to session folder: ${folderId}`);
    toast(`Uploading ${files.length} file(s) to session folder...`);

    if (promptEl) {
        promptEl.style.backgroundColor = 'rgba(255, 159, 64, 0.2)';
        promptEl.style.borderColor = '#ff9f40';
        promptEl.disabled = true;
    }

    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        try {
            if (promptEl) {
                promptEl.value = txt + `\n\n⏳ Uploading ${file.name} (${i+1}/${files.length})...`;
            }
            const uploadResult = await handleSessionFileUpload(file, folderId);
            if (uploadResult && uploadResult.path) {
                uploaded.push({
                    filename: uploadResult.filename,
                    path: uploadResult.path,
                    url: uploadResult.url
                });
                console.log(`${agent.logPrefix} File uploaded to session: ${uploadResult.path}`);
            }
        } catch (err) {
            console.error(`${agent.logPrefix} Error uploading ${file.name}:`, err);
            toast(`Error uploading ${file.name}: ${err.message}`);
        }
    }

    let prompt = txt;
    if (uploaded.length > 0) {
        prompt = txt + attachedFilesBlock(uploaded);
        if (promptEl) {
            promptEl.style.backgroundColor = 'rgba(39, 217, 128, 0.2)';
            promptEl.style.borderColor = '#27d980';
            promptEl.value = prompt;
        }
        toast(`${uploaded.length} file(s) uploaded! ${startsSession ? 'Starting' : 'Sending to'} ${agent.label}...`);
        await new Promise(resolve => setTimeout(resolve, 500));
    } else {
        console.warn(`${agent.logPrefix} No files uploaded successfully`);
        toast('Warning: Could not upload attachments');
    }

    if (promptEl) {
        promptEl.style.backgroundColor = '';
        promptEl.style.borderColor = '';
        promptEl.disabled = false;
    }
    return { prompt, uploaded };
}

/**
 * Send a turn to a CLI agent: input to its open session when it has one for
 * this operator (files into that session's folder), else a new session (files
 * into a fresh folder).
 * @param {Object} agent - A CLI_AGENTS entry
 * @param {string} txt - The typed prompt
 * @param {File[]} attachedFiles - The composer's attachments
 */
async function sendToCliAgent(agent, txt, attachedFiles) {
    let handler = agent.getHandler();
    const promptEl = $("prompt");

    if (handler && handler.isActive() && handler.belongsTo(getOperator())) {
        console.log(`${agent.logPrefix} Active session detected, sending input to existing session`);
        let inputPrompt = txt;
        let uploaded = [];
        if (attachedFiles.length > 0 && handler.sessionId) {
            ({ prompt: inputPrompt, uploaded } = await uploadToAgentSession(
                txt, attachedFiles, handler.sessionId, agent, false));
            clearAttachedFiles();
            renderPreviews();
        }

        let bubbleText = txt;
        if (uploaded.length > 0) {
            bubbleText += `\n[Attached ${uploaded.length} file(s) to session folder]`;
        }
        addBubble("user", bubbleText);
        if (promptEl) promptEl.value = "";
        handler.sendInput(inputPrompt);
        return;
    }

    console.log(`${agent.logPrefix} No active session, starting a new one...`);
    let enhancedPrompt = txt;
    if (attachedFiles.length > 0) {
        const sessionFolderId = agent.folderPrefix + Date.now() + '-' + Math.random().toString(36).substr(2, 9);
        ({ prompt: enhancedPrompt } = await uploadToAgentSession(
            txt, attachedFiles, sessionFolderId, agent, true));
    }

    toast(agent.connecting);
    addBubble("user", enhancedPrompt);
    if (promptEl) promptEl.value = "";
    clearAttachedFiles();
    renderPreviews();

    if (!handler) {
        handler = agent.createHandler();
    }
    try {
        await handler.startSession(enhancedPrompt, getOperator());
    } catch (err) {
        console.error(`${agent.logPrefix} startSession error:`, err);
        toast(`${agent.errorToast}: ${err.message}`);
    }
}

// =============================================================================
// User Content
// =============================================================================

/**
 * The user turn's content parts and bubble text: the text, then each
 * attachment uploaded ONCE per logical send (both modes build on it, so a
 * streaming->standard fallback never uploads the files a second time, audit
 * P01-chat-core-web-18), then the uploaded image URLs for the image tools. A
 * retry re-sends the failed attempt's own parts, its uploads included.
 * @param {string} txt - The typed text
 * @param {File[]} attachedFiles - The composer's attachments
 * @param {?{content: Array, bubble: ?string}} resend - The turn a retry re-sends
 * @returns {Promise<?{userContent: Array, bubbleContent: string}>} null when an
 *   upload failed (nothing is sent; the draft and the attachments stay put)
 */
export async function buildUserContent(txt, attachedFiles, resend = null) {
    let userContent = [];
    let bubbleContent = txt;
    if (resend) {
        // The parts the failed attempt sent, its uploads included.
        userContent = resend.content.map((part) => ({ ...part }));
        if (resend.bubble) bubbleContent = resend.bubble;
    } else if (txt) {
        userContent.push({ type: "text", text: txt });
    }

    if (attachedFiles.length > 0) {
        let imageUrls = [];  // Track image URLs for tool usage
        for (const file of attachedFiles) {
            const type = classifyAttachment(file);
            if (!type) {
                // Never dropped silently: the operator would think the model saw it.
                toast(`Not sent to the model (unsupported type): ${file.name}`);
                continue;
            }
            let uploadResult;
            try {
                uploadResult = await handleFileUpload(file);
            } catch (err) {
                // Nothing is sent: the draft and the attachments stay put for another try.
                console.error('[CHAT] Upload failed:', file.name, err);
                toast(`Upload failed: ${file.name} — ${err?.message || err}`);
                return null;
            }
            if (!uploadResult || !uploadResult.url) continue;
            userContent.push({ type, [type]: { url: uploadResult.url } });
            bubbleContent += `\n[Attached: ${file.name}]`;

            // Track image URLs so model can use them for tools
            if (type === "image_url") {
                imageUrls.push({ filename: file.name, url: uploadResult.url });
            }
        }

        // Add image URL info as text so model can use for generate_video(image_url) or generate_image(reference_images)
        if (imageUrls.length > 0) {
            let urlInfo = '\n\n[Uploaded Image URLs for tool use:';
            for (const img of imageUrls) {
                urlInfo += `\n- ${img.filename}: ${img.url}`;
            }
            urlInfo += '\nUse these URLs with generate_video(image_url=...) for image-to-video, or generate_image(reference_images=[...]) for image-to-image.]';
            userContent.push({ type: "text", text: urlInfo });
        }
    }
    return { userContent, bubbleContent };
}

// =============================================================================
// Main Send Function
// =============================================================================

/**
 * Main send function - handles all chat modes
 */
export async function send() {
    console.log('[v101] send() called - provider:', window.__provider, 'attachedFiles:', getAttachedFiles().length);

    // A retry (retryFailedTurn) re-sends the failed turn's own text and
    // already-uploaded parts; the composer's attachments are not part of it.
    const resend = _resendTurn;
    _resendTurn = null;
    const txt = resend ? resend.text : ($("prompt") ? $("prompt").value : "").trim();
    const attachedFiles = resend ? [] : getAttachedFiles();

    if (!txt && attachedFiles.length === 0 && !(resend && resend.content.length)) {
        toast("Type something or attach a file.");
        return;
    }

    // Every turn is the resolved operator's: with none picked yet (several on the box,
    // or none), nothing is sent and the draft stays put.
    if (!getOperator()) {
        toast(OPERATOR_REQUIRED_MESSAGE);
        return;
    }

    // The CLI agents (Claude Code, Gemini CLI) take the turn over their WebSocket.
    const cliAgent = CLI_AGENTS[window.__provider];
    if (cliAgent) {
        await sendToCliAgent(cliAgent, txt, attachedFiles);
        return;
    }

    // ATTACHMENTS: uploaded ONCE per logical send (buildUserContent).
    const built = await buildUserContent(txt, attachedFiles, resend);
    if (!built) return;  // an upload failed: the draft and the attachments stay put
    const { userContent, bubbleContent } = built;

    // The user turn's bubble for this logical send, appended exactly ONCE
    // (the standard-mode fallback reuses it), and the anchor for the retry
    // chip when the FINAL outcome is failure. tagUserTurn stamps a per-send
    // key onto the history entry + bubble so failure marking / retry removal
    // target exactly THIS send even when sends overlap.
    const userTurnBubble = addBubble("user", bubbleContent);
    tagUserTurn(userTurnBubble);
    if ($("prompt")) $("prompt").value = "";
    if (!resend) { // a retry leaves files attached since the failure where they are
        clearAttachedFiles();
        renderPreviews();
    }

    // =========================================================================
    // STREAMING MODE: Use SSE with thinking/reasoning display
    // =========================================================================
    if (STREAMING_ENABLED && window.__provider !== 'agents' && window.__provider !== 'gemini-agents') {
        const provider = window.__provider || "gemini";
        let result;
        try {
            // IMPORTANT: We do NOT send localStorage history to the AI model.
            // Context comes from BlackBox snapshots (recent, relevant, checkpoints) which are:
            // - Synthesized and reasoned (not raw conversation)
            // - Semantically searched for relevance
            // - Properly sized and managed
            // localStorage history is ONLY for UI persistence (so bubbles remain on refresh)
            const messages = [];
            messages.push({ role: "user", content: userContent.length === 1 && userContent[0].type === "text" ? txt : userContent });
            console.log('[CHAT] Sending only current message - context comes from BlackBox snapshots');

            result = await sendStreamingChat(
                messages,
                provider,
                window.__model || "",
                getOperator()
            );
        } catch (streamError) {
            if (!shouldFallBackToStandard(streamError)) {
                // The server accepted the turn and it died mid-stream. Re-sending
                // would run it (and its tools) twice, so the partial bubble is
                // already gone and the operator gets the retry chip instead.
                if (provider === 'computer-use') {
                    // The CU task keeps running server-side; the background
                    // banner picks it up and adds its result when it finishes.
                    toast('Lost the Computer Use stream — the task keeps running in the background');
                    checkCUBackgroundStatus();
                } else {
                    addBubble("assistant", "Chat error: " + (streamError?.message || String(streamError)));
                    markSendFailed(userTurnBubble, txt, userContent);
                }
                return;
            }
            console.warn("[STREAM] Server did not accept the stream, falling back to polling:", streamError);
            toast("Streaming unavailable, using standard mode...");
        }

        if (result) {
            // ERROR-ONLY stream: the transport completed but the server delivered
            // only SSE 'error' events — no reply. Previously this left a dangling
            // empty streaming bubble and a user turn with no reply and NO retry
            // chip. Convert it into the standard failure outcome. Guarded on
            // !result.response so partial/CU-queue flows keep today's behavior.
            if (!result.queued && !result.response && result.errorText) {
                if (result.bubbleEl) {
                    if (result.bubbleEl._signalLine) result.bubbleEl._signalLine.destroy(); // no leaked rAF
                    if (result.bubbleEl.parentNode) {
                        result.bubbleEl.remove(); // drop the empty streaming bubble (not in historyData)
                    }
                }
                addBubble("assistant", "Chat error: " + result.errorText);
                markSendFailed(userTurnBubble, txt, userContent);
                return;
            }

            if (result.queued) {
                console.log('[STREAM] Queued request — response will appear in original bubble');
                // Don't save to history — the original stream handles the combined response
            } else if (result.response) {
                await saveStreamedTurn(txt, result);
            }
            return;
        }
    }

    // =========================================================================
    // STANDARD MODE: Use polling-based chat (fallback)
    // =========================================================================
    let thinking;

    try {
        const payload = {
            operator: getOperator(),
            provider: window.__provider,
            model: window.__model,
            messages: []
        };

        payload.messages.push({ role: "user", content: userContent });

        thinking = createAnimatedThinkingBubble();
        const hist = $("history");
        if (hist) {
            hist.appendChild(thinking);
            scrollToBottomIfNeeded();
        }

        localStorage.setItem("bb_pending_response", JSON.stringify({
            timestamp: Date.now(),
            operator: getOperator()
        }));

        const historyData = getHistoryData();
        historyData.push({role: "assistant", content: "...", pending: true, pendingTimestamp: Date.now()});
        setHistoryData(historyData);
        saveHistory();

        let requestBody;
        let headers = {};

        const hasMedia = userContent.some(p => p.type === 'image_url' || p.type === 'video_url' || p.type === 'audio_url' || p.type === 'document_url');

        if (hasMedia) {
            const formData = new FormData();
            formData.append('messages', JSON.stringify(payload.messages));
            formData.append('operator', payload.operator);
            formData.append('provider', payload.provider);
            formData.append('model', payload.model);
            requestBody = formData;
        } else {
            headers['Content-Type'] = 'application/json';
            requestBody = JSON.stringify(payload);
            // Inside the app: signed over the exact body, as /chat/stream is. A
            // multipart body (attachments) cannot be hashed before the browser
            // builds it, so that turn goes unsigned.
            if (wellnessSigner()) headers = await withOriginProof(headers, 'POST', '/chat', requestBody);
        }

        const r = await fetch("/chat", {
            method: "POST",
            headers: headers,
            body: requestBody
        });

        const rText = await r.text();
        let taskResponse;
        try {
            taskResponse = JSON.parse(rText);
        } catch(e) {
            throw new Error(`Server returned non-JSON: ${rText}`);
        }

        if (!r.ok) {
            throw new Error(taskResponse.detail || taskResponse.error || rText);
        }

        const taskId = taskResponse.task_id;
        if (!taskId) {
            throw new Error("No task ID returned from server");
        }

        console.log(`[ASYNC-CHAT] Task created: ${taskId}, polling for completion...`);

        localStorage.setItem("bb_pending_task", JSON.stringify({
            taskId: taskId,
            timestamp: Date.now(),
            operator: getOperator()
        }));

        let reply = null;
        let taskFailure = null;
        let pollAttempts = 0;
        const maxPollAttempts = 180;

        while (pollAttempts < maxPollAttempts) {
            await new Promise(resolve => setTimeout(resolve, 1000));
            pollAttempts++;

            let taskStatus;
            try {
                const statusResponse = await fetch(`/tasks/status/${taskId}`);
                taskStatus = await statusResponse.json();
            } catch (pollError) {
                // A transient fetch error: keep polling the same task.
                console.error(`[ASYNC-CHAT] Polling error:`, pollError);
                continue;
            }

            console.log(`[ASYNC-CHAT] Poll attempt ${pollAttempts}: status=${taskStatus.status}, progress=${taskStatus.progress}`);

            if (taskStatus.status === "completed") {
                console.log(`[ASYNC-CHAT] Task completed after ${pollAttempts} polls`);
                reply = taskStatus.result_data;
                break;
            } else if (taskStatus.status === "failed" || taskStatus.status === "cancelled") {
                // Terminal: stop polling now and surface the task's own error
                // (it used to be swallowed by the poll's catch, and the turn
                // reported a 180 s timeout instead; audit P01-chat-core-web-07).
                taskFailure = taskStatus.error_message || (taskStatus.status === "cancelled" ? "Task cancelled" : "Unknown error");
                break;
            }
        }

        if (taskFailure !== null) {
            localStorage.removeItem("bb_pending_task");
            throw new Error(`Task failed: ${taskFailure}`);
        }

        if (!reply) {
            throw new Error(`Task timed out after ${pollAttempts} attempts. Check Timeline for response.`);
        }

        if (reply.checkpoint_triggered) {
            toast('Auto-checkpoint minting in progress...', { duration: 5000 });
        }

        localStorage.removeItem("bb_pending_response");
        localStorage.removeItem("bb_pending_task");

        let replyText = normalizeReplyFromAny(reply);

        localStorage.setItem("bb_last_response", JSON.stringify({
            response: replyText,
            timestamp: Date.now()
        }));

        if (reply.inline_media && Array.isArray(reply.inline_media) && reply.inline_media.length > 0) {
            const mediaHTML = reply.inline_media.map(media => {
                const url = escapeAttr(media.url);
                const type = media.type;

                if (type === 'image') {
                    return `<img src="${url}" alt="Generated by Gemini 3" class="inline-media-image" style="max-width: 100%; border-radius: 8px; margin: 8px 0;">`;
                } else if (type === 'audio') {
                    return `<audio src="${url}" controls class="inline-media-audio" style="width: 100%; margin: 8px 0;"></audio>`;
                } else if (type === 'video') {
                    return `<video src="${url}" controls preload="metadata" class="inline-media-video" style="max-width: 100%; border-radius: 8px; margin: 8px 0;"></video>`;
                }
                return '';
            }).join('\n');

            replyText = replyText + '\n\n' + mediaHTML;
            console.log('[GEMINI3] Rendered ' + reply.inline_media.length + ' inline media items');
        }

        updateThinkingBubble(thinking, replyText);
        const previewText = replyText.replace(/<[^>]+>/g, '').substring(0, 100);
        notifyNewMessage(previewText);

        if (!replyText.includes('<img') && !replyText.includes('<video') && !replyText.includes('<audio')) {
            setLastAssistantText(replyText);
        }

        // Server-built loading placeholders (image/video/music jobs the turn
        // started) wake the media pollers, which stop again once none are left.
        if (/(?:video|image|music)-loading-placeholder/.test(replyText)) {
            ensureMediaPolling();
        }

        await refreshHealth();

    } catch(e) {
        console.error("Chat error:", e);
        if (thinking) {
            updateThinkingBubble(thinking, "Chat error: " + (e?.message || String(e)));
        } else {
            addBubble("assistant", "Chat error: " + (e?.message || String(e)));
        }
        // FINAL outcome is failure (streaming already fell back here, or standard
        // mode failed directly): flag the user turn + offer retry under its bubble.
        markSendFailed(userTurnBubble, txt, userContent);
    }
}

/**
 * The type a chat attachment is sent to the model as, or null when the model
 * cannot take it. ONE table for both modes, matching the file picker's accept
 * list (the streaming branch used to drop .doc/.docx silently; audit
 * P01-chat-core-web-16).
 * @param {File} file - An attached file
 * @returns {'image_url'|'video_url'|'audio_url'|'document_url'|null}
 */
export function classifyAttachment(file) {
    const mime = (file && file.type) || '';
    if (mime.startsWith("image/")) return "image_url";
    if (mime.startsWith("video/")) return "video_url";
    if (mime.startsWith("audio/")) return "audio_url";
    if (DOCUMENT_MIME_TYPES.has(mime)) return "document_url";
    // The picker accepts .pdf/.txt/.doc/.docx by EXTENSION; a browser without
    // a MIME entry for one reports "" or application/octet-stream.
    if (!mime || mime === "application/octet-stream") {
        const ext = String((file && file.name) || '').toLowerCase().split('.').pop();
        if (DOCUMENT_EXTENSIONS.has(ext)) return "document_url";
    }
    return null;
}

const DOCUMENT_MIME_TYPES = new Set([
    "application/pdf",
    "text/plain",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]);

const DOCUMENT_EXTENSIONS = new Set(["pdf", "txt", "doc", "docx"]);

/**
 * Persist a completed streamed turn: local history, then /chat/save (which
 * mints the snapshot) with the turn's real token usage, then the Signal's
 * mint flourish and any server-modified response.
 * @param {string} txt - The user's prompt text
 * @param {Object} result - What sendStreamingChat resolved with
 */
async function saveStreamedTurn(txt, result) {
    const updatedHistoryData = getHistoryData();
    updatedHistoryData.push({ role: "assistant", content: result.response });
    if (result.thinking) {
        updatedHistoryData[updatedHistoryData.length - 1].thinking = result.thinking;
    }
    if (result.cuLog && result.cuLog.length > 0) {
        updatedHistoryData[updatedHistoryData.length - 1].cuLog = result.cuLog;
    }
    // A reply a wellness tool fed: its ref (the delivered row behind it) and
    // the frame for its footer. The Portal never sends earlier turns to the
    // model, so neither ever leaves this browser in a /chat/stream request.
    if (result.wellnessUsed) {
        updatedHistoryData[updatedHistoryData.length - 1].wellness_ref = result.wellnessUsed.ref;
        updatedHistoryData[updatedHistoryData.length - 1].wellness_used = result.wellnessUsed;
    }
    setHistoryData(updatedHistoryData);
    saveHistory();
    setLastAssistantText(result.response);

    try {
        // CU provider saves internally via background task — skip /chat/save
        if (window.__provider === 'computer-use') {
            console.log('[STREAM] Skipping /chat/save for computer-use (background task saves internally)');
            return;
        }
        const saveResponse = await fetch("/chat/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                operator: getOperator(),
                user_message: txt,
                assistant_response: result.response,
                reasoning: result.thinking || "",
                provenance: result.provenance || {},
                model: window.__model || "",
                tokens: saveTokens(result.usage),
                // The turn key stream_start named: the box masks exactly this
                // turn's health results in the ledger. Absent, the body is
                // what it always was.
                ...(result.wellnessTurn ? { wellness_turn: result.wellnessTurn } : {})
            })
        });
        const saveResult = await saveResponse.json();
        if (saveResult.minted) {
            console.log('[STREAM] Snapshot minted:', saveResult.snap_id);
        }

        // The Signal: narrate the mint on the turn's HUD line as a brief
        // final flourish, then dissolve. The .signal-host survives
        // finalizeStreamingBubble, so the line is still mountable here.
        // Degrades gracefully — dims omitted if the save response lacks it.
        // Presentation-only: reads saveResult, never writes the payload.
        const _sig = result.bubbleEl && result.bubbleEl._signalLine;
        if (_sig && saveResult && saveResult.snap_id) {
            const _dims = saveResult.dims || saveResult.embedding_dims;
            _sig.push('mint · ' + saveResult.snap_id + (_dims ? ' · ' + _dims + 'd' : ''));
            // Let the mint line fully morph in and linger before dissolving.
            // Dissolving synchronously would call push("") on the same tick
            // and cancel the just-scheduled mint-in animation (never visible).
            setTimeout(() => _sig.dissolve(), 1600);
        }

        if (saveResult.modified_response) {
            console.log('[STREAM] Modified response received, updating bubble...');
            const hist = $("history");
            if (hist) {
                const lastChild = hist.lastElementChild;
                if (lastChild) {
                    let contentDiv = lastChild.querySelector('.response-content') ||
                                     lastChild.querySelector('.bubble-text') ||
                                     lastChild.querySelector('span');

                    if (contentDiv && saveResult.modified_response) {
                        const parsedContent = renderMarkdown(saveResult.modified_response);
                        contentDiv.innerHTML = parsedContent;

                        if (typeof makeSnapshotsClickable === 'function') {
                            makeSnapshotsClickable(contentDiv);
                        }

                        if (typeof applySyntaxHighlighting === 'function') {
                            applySyntaxHighlighting(contentDiv);
                        }

                        const finalHistoryData = getHistoryData();
                        const lastAssistantIndex = finalHistoryData.length - 1;
                        if (lastAssistantIndex >= 0 && finalHistoryData[lastAssistantIndex].role === 'assistant') {
                            finalHistoryData[lastAssistantIndex].content = saveResult.modified_response;
                            setHistoryData(finalHistoryData);
                            saveHistory();
                            // A wellness turn's modified text is the masked copy:
                            // inside the app it now offers Show numbers.
                            decorateWellnessBubble(lastChild, finalHistoryData[lastAssistantIndex]);
                        }
                    }
                }
            }
        }
    } catch (saveErr) {
        console.error('[STREAM] Failed to save conversation:', saveErr);
    }
}

/**
 * The stream's usage event as /chat/save's tokens shape ({prompt, completion},
 * what the server adds to the operator's counters). {} when there was none.
 * @param {?Object} usage - {prompt_tokens, completion_tokens} from the stream
 * @returns {{prompt?: number, completion?: number}}
 */
function saveTokens(usage) {
    if (!usage || typeof usage !== 'object') return {};
    const prompt = Number(usage.prompt_tokens ?? usage.input_tokens ?? 0) || 0;
    const completion = Number(usage.completion_tokens ?? usage.output_tokens ?? 0) || 0;
    return { prompt, completion };
}

// =============================================================================
// Retry-on-failed-send
// =============================================================================

/**
 * Stamp a unique per-send key onto the JUST-APPENDED user history entry (the
 * last entry — addBubble pushed it synchronously) and the DOM bubble. The key
 * decouples failure marking / retry removal from "last user entry" heuristics,
 * which overlapping sends could race onto the WRONG entry. Persisted with the
 * entry (saveHistory), restored onto the bubble by the history-restore paths.
 *
 * @param {HTMLElement|null} userBubbleEl - The bubble addBubble just returned
 * @returns {string|null} The key, or null when there was nothing to stamp
 */
function tagUserTurn(userBubbleEl) {
    const hd = getHistoryData();
    const entry = hd[hd.length - 1];
    if (!entry || entry.role !== 'user') return null;
    const key = 'send-' + Date.now() + '-' + Math.random().toString(36).slice(2, 9);
    entry.sendKey = key;
    saveHistory();
    if (userBubbleEl) userBubbleEl.dataset.sendKey = key;
    return key;
}

/**
 * Pure: index of the user history entry carrying sendKey (searched from the
 * end); -1 when the key is falsy or absent. Extracted so the keying logic is
 * testable and shared by markSendFailed + retryFailedTurn.
 *
 * @param {Array<{role: string, sendKey?: string}>} historyData
 * @param {string|undefined} sendKey
 * @returns {number}
 */
export function findUserTurnIndex(historyData, sendKey) {
    if (!sendKey) return -1;
    for (let i = historyData.length - 1; i >= 0; i--) {
        if (historyData[i].role === 'user' && historyData[i].sendKey === sendKey) return i;
    }
    return -1;
}

/**
 * The turn send() re-sends next (set by retryFailedTurn, read and cleared at
 * send()'s entry): {text, content, bubble}.
 * @type {?{text: string, content: Array<Object>, bubble: ?string}}
 */
let _resendTurn = null;

/**
 * Mark the just-sent user turn as FAILED: flag its historyData entry
 * ({failed: true, failedAt}) so the state survives reload (state-management owns
 * historyData/saveHistory), stamp the DOM bubble so retry can find the exact
 * entry, and attach the retry chip under the bubble. The entry is targeted by
 * the per-send key stamped at addBubble time (tagUserTurn); the last-user-entry
 * fallback only covers an untagged bubble (shouldn't happen in practice).
 *
 * What a retry re-sends is kept with the entry ({retry: {text, content}}):
 * the prompt and the parts the attempt sent, whose files are already
 * uploaded. A retry used to re-send the text alone, so the model answered
 * without the files, and a files-only turn had no retry at all.
 *
 * @param {HTMLElement|null} userBubbleEl - The user turn's bubble (may be null
 *   if the send failed before the bubble was appended — then there is nothing
 *   to retry from and this is a no-op).
 * @param {string} retryText - The raw prompt text to re-send
 * @param {Array<Object>} [content] - The user message parts the attempt sent
 *   (text and uploaded-file URLs). With no text and no parts → no retry.
 */
function markSendFailed(userBubbleEl, retryText, content = []) {
    const parts = Array.isArray(content) ? content : [];
    if (!userBubbleEl || (!retryText && !parts.length)) return;

    const failedAt = Date.now();
    const hd = getHistoryData();
    let idx = findUserTurnIndex(hd, userBubbleEl.dataset.sendKey);
    if (idx === -1) {
        for (let i = hd.length - 1; i >= 0; i--) {
            if (hd[i].role === 'user') { idx = i; break; }
        }
    }
    const retry = { text: retryText || '', content: parts };
    if (idx !== -1) {
        hd[idx].failed = true;
        hd[idx].failedAt = failedAt;
        hd[idx].retry = retry;
    }
    setHistoryData(hd);
    saveHistory();

    userBubbleEl.dataset.failedAt = String(failedAt);
    userBubbleEl._retry = retry;
    // The chip needs some text to carry; a files-only turn carries its bubble's.
    attachRetryChip(userBubbleEl, retryText || (idx !== -1 && hd[idx].content) || ' ');
}

/**
 * Retry a failed user turn (the .bubble-retry chip's action — invoked via
 * dynamic import from chat-bubbles.js attachRetryChip).
 *
 * REPLACES the failed turn — never duplicates it:
 *  1. remove the error assistant bubble that follows the failed user bubble
 *     (DOM) and its historyData entry;
 *  2. remove the failed user bubble + its historyData entry;
 *  3. re-fire the SAME turn through send() (the one true send path), which
 *     appends the user turn exactly once: the text and already-uploaded parts
 *     markSendFailed kept, or (an entry saved before those were kept) the
 *     text alone. /chat/save only runs on success, so replace-not-append also
 *     guarantees no double-mint.
 *
 * Re-fire mechanism: rather than extracting a core from the ~800-line send()
 * (4 provider branches all reading shared UI state — high regression risk),
 * the retry sets $("prompt").value = retryText and calls send(); send() reads
 * the prompt synchronously at entry, and any draft the user typed since the
 * failure is restored right after (send clears the prompt in its synchronous
 * prefix when no attachments are pending — a retry uploads nothing).
 *
 * @param {HTMLElement} userBubbleEl - The failed user bubble
 * @param {string} retryText - The raw prompt text to re-send (used when the
 *   entry kept no retry record)
 */
export async function retryFailedTurn(userBubbleEl, retryText) {
    // 1. Error assistant bubble(s) (DOM): everything the failed turn produced
    //    after the user bubble — the error bubble itself plus any stale partial
    //    streaming bubble left by a mid-stream death before the fallback ran.
    //    Stops at the next user bubble (never touches other turns).
    let next = userBubbleEl.nextElementSibling;
    while (next && next.classList.contains('bubble') && next.classList.contains('assistant')) {
        const toRemove = next;
        next = next.nextElementSibling;
        toRemove.remove();
    }

    // 2. History: drop the failed user entry + its trailing assistant error
    //    entry. Keyed by the per-send sendKey stamped at addBubble time
    //    (findUserTurnIndex); legacy fallbacks (failedAt stamp, then last
    //    failed user entry) cover entries persisted before keying existed.
    const stamp = Number(userBubbleEl.dataset.failedAt || 0);
    const hd = getHistoryData();
    let idx = findUserTurnIndex(hd, userBubbleEl.dataset.sendKey);
    if (idx === -1) {
        for (let i = hd.length - 1; i >= 0; i--) {
            if (hd[i].role === 'user' && hd[i].failed && (!stamp || hd[i].failedAt === stamp)) {
                idx = i;
                break;
            }
        }
    }
    if (idx === -1) {
        for (let i = hd.length - 1; i >= 0; i--) {
            if (hd[i].role === 'user' && hd[i].failed) { idx = i; break; }
        }
    }
    const retry = (idx !== -1 && hd[idx].retry) || userBubbleEl._retry || null;
    const bubbleText = idx !== -1 ? hd[idx].content : null;
    if (idx !== -1) {
        if (idx + 1 < hd.length && hd[idx + 1].role === 'assistant') {
            hd.splice(idx + 1, 1);
        }
        hd.splice(idx, 1);
        setHistoryData(hd);
        saveHistory();
    }

    // 3. Remove the failed user bubble and re-fire the same turn.
    userBubbleEl.remove();

    const text = retry ? String(retry.text || '') : retryText;
    if (retry && Array.isArray(retry.content) && retry.content.length) {
        _resendTurn = { text, content: retry.content, bubble: bubbleText };
    }
    const promptEl = $("prompt");
    const draft = promptEl ? promptEl.value : "";
    if (promptEl) promptEl.value = text;
    const sending = send(); // reads the prompt synchronously at entry
    if (promptEl && draft && !promptEl.value) promptEl.value = draft; // restore draft typed since the failure
    await sending;
}

// =============================================================================
// Video Observer
// =============================================================================

let videoObserver = null;

/**
 * Setup video observer for adding extend buttons
 */
export function setupVideoObserver() {
    videoObserver = new MutationObserver((mutations) => {
        let placeholderAdded = false;
        mutations.forEach((mutation) => {
            mutation.addedNodes.forEach((node) => {
                if (node.nodeType === 1) {
                    if (node.tagName === 'VIDEO' && node.classList.contains('generated-video')) {
                        addExtendButtonToVideo(node);
                    }
                    const videos = node.querySelectorAll ? node.querySelectorAll('video.generated-video') : [];
                    videos.forEach(video => addExtendButtonToVideo(video));
                    if (!placeholderAdded && (node.matches(MEDIA_PLACEHOLDER_SELECTOR)
                        || node.querySelector(MEDIA_PLACEHOLDER_SELECTOR))) {
                        placeholderAdded = true;
                    }
                }
            });
        });
        // A media job's placeholder appeared (a polled reply, a restored
        // history bubble): wake the media pollers.
        if (placeholderAdded) ensureMediaPolling();
    });

    const historyEl = $("history");
    if (historyEl) {
        videoObserver.observe(historyEl, { childList: true, subtree: true });

        setTimeout(() => {
            const existingVideos = historyEl.querySelectorAll('video.generated-video');
            existingVideos.forEach(video => addExtendButtonToVideo(video));
        }, 1000);
    }
}

// =============================================================================
// Polling Intervals
// =============================================================================

/** Server-built loading placeholders of image/video/music jobs (tasks.py). */
const MEDIA_PLACEHOLDER_SELECTOR = '.video-loading-placeholder, .image-loading-placeholder, .music-loading-placeholder';

/** The three media pollers (created on first use). */
const _mediaPollers = [];

/**
 * A createPoller poller that sweeps one kind of placeholder and stops itself
 * as soon as none is left on the page (the old setIntervals ran a
 * querySelectorAll sweep every 3 s for the life of the page, hidden or not;
 * audit P01-chat-core-web-21).
 */
function mediaPoller(selector, sweep, intervalMs) {
    const poller = createPoller(async () => {
        if (!document.querySelector(selector)) {
            poller.stop();
            return;
        }
        await sweep();
    }, { intervalMs, immediate: false });
    return poller;
}

/**
 * Wake the media pollers (no-op for one already running). Called when a
 * placeholder appears; each stops again once its kind is all resolved.
 */
export function ensureMediaPolling() {
    if (!_mediaPollers.length) {
        _mediaPollers.push(
            mediaPoller('.video-loading-placeholder', pollVideoTasks, 10000),
            mediaPoller('.image-loading-placeholder', pollImageTasks, 3000),
            mediaPoller('.music-loading-placeholder', pollMusicTasks, 3000),
        );
    }
    _mediaPollers.forEach(p => p.start());
}

/**
 * Start media polling (placeholders already on the page get swept; later ones
 * wake the pollers through the history observer).
 */
export function startMediaPolling() {
    ensureMediaPolling();
}

// =============================================================================
// Initialization
// =============================================================================

/**
 * Initialize chat send functionality
 */
export function initChatSend() {
    const btnSend = $("btnSend");
    if (btnSend) btnSend.addEventListener("click", send);

    const promptEl = $("prompt");
    if (promptEl) {
        promptEl.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
            }
        });
    }

    initStreamingToggle();
    setupVideoObserver();
    startMediaPolling();

    // Restore floating "Live Browser" button if CU was active before refresh
    restoreCUButton();

    // Restore todo tracker if session data exists from before refresh
    _restoreTodoTracker();
}

/**
 * Restore todo tracker from sessionStorage on page load.
 * Looks for any saved todo session and re-initializes the tracker.
 */
function _restoreTodoTracker() {
    try {
        // Check sessionStorage for any saved todo data
        for (let i = 0; i < sessionStorage.length; i++) {
            const key = sessionStorage.key(i);
            if (key && key.startsWith('bb_todos_')) {
                const sessionId = key.replace('bb_todos_', '');
                const raw = sessionStorage.getItem(key);
                if (raw) {
                    const data = JSON.parse(raw);
                    if (data.todos && data.todos.length > 0) {
                        console.log('[TodoRestore] Found saved todos for session:', sessionId);
                        todoTracker.init('todoPanel', sessionId);
                        return;
                    }
                }
            }
        }
    } catch (e) {
        console.warn('[TodoRestore] Error restoring todos:', e);
    }
}


// =============================================================================
// Computer Use Background Status
// =============================================================================

let _cuStatusPollTimer = null;

/**
 * Check if a Computer Use task is running in the background.
 * Shows a sticky banner if so, and adds completed results to history.
 */
export async function checkCUBackgroundStatus() {
    try {
        // Skip if an SSE stream is already consuming CU events — it handles everything
        if (_cuStreamActive) return;

        const operator = getOperator();
        const res = await fetch(`/chat/cu-status?operator=${encodeURIComponent(operator)}`);
        if (!res.ok) return;
        const status = await res.json();

        if (status.status === 'running') {
            _showCUBackgroundBanner(status);
            // Initialize TodoTracker once per background CU task
            if (!_cuStatusPollTimer) {
                todoTracker.init('todoPanel', 'cu-bg-session');
                _cuCurrentStep = status.step || 1;
                _cuTotalSteps = status.total || 15;
            }
            if (!_cuTasks.find(t => t.id === `cu-step-${status.step}`)) {
                _cuTodoNewStep(status.step || 1);
            }
            _cuTodoDescribe('Running in background...', 'Background task');
            // Poll every 3s while running
            if (!_cuStatusPollTimer) {
                _cuStatusPollTimer = createPoller(_pollCUStatus, { intervalMs: 3000, immediate: false });
                _cuStatusPollTimer.start();
            }
        } else if (status.status === 'complete' && status.final_response) {
            // Check if this result is already in local history
            const hist = getHistoryData();
            const lastAssistant = hist.length > 0 ? hist[hist.length - 1] : null;
            if (!lastAssistant || lastAssistant.content !== status.final_response) {
                // Add the completed background result to local history
                if (status.user_message) {
                    hist.push({ role: 'user', content: status.user_message });
                }
                hist.push({
                    role: 'assistant',
                    content: status.final_response,
                    cuLog: status.cu_log || [],
                    backgroundComplete: true
                });
                setHistoryData(hist);
                saveHistory();
                // Re-render history to show the new result, its CU panel included
                renderHistory();
                toast('Computer Use task completed in background');
            }
            _removeCUBackgroundBanner();
        } else {
            _removeCUBackgroundBanner();
        }
    } catch (e) {
        console.warn('[CU-BG] Status check failed:', e);
    }
}

async function _pollCUStatus() {
    try {
        const operator = getOperator();
        const res = await fetch(`/chat/cu-status?operator=${encodeURIComponent(operator)}`);
        if (!res.ok) return;
        const status = await res.json();

        if (status.status === 'running') {
            _updateCUBackgroundBanner(status);
        } else {
            // Task finished — stop polling and update UI
            if (_cuStatusPollTimer) {
                _cuStatusPollTimer.stop();
                _cuStatusPollTimer = null;
            }
            if (status.status === 'complete') {
                await checkCUBackgroundStatus(); // Will add result to history
            } else if (status.status === 'error') {
                _removeCUBackgroundBanner();
                toast('Computer Use background task failed: ' + (status.error_message || 'unknown error'));
            } else {
                _removeCUBackgroundBanner();
            }
        }
    } catch (e) {
        console.warn('[CU-BG] Poll failed:', e);
    }
}

function _showCUBackgroundBanner(status) {
    if (document.getElementById('cu-bg-banner')) {
        _updateCUBackgroundBanner(status);
        return;
    }
    const banner = document.createElement('div');
    banner.id = 'cu-bg-banner';
    banner.className = 'cu-bg-banner';
    banner.innerHTML = `
        <span class="cu-bg-dot"></span>
        <span class="cu-bg-label">Computer Use running in background</span>
        <span class="cu-bg-step">Step ${escapeHtml(status.step)}/${escapeHtml(status.total)}</span>
        <button class="cu-bg-view" data-bubble-action="cu-bg-view">Live Browser</button>
        <button class="cu-bg-reconnect" data-bubble-action="cu-bg-reconnect">Reconnect</button>
    `;
    document.body.appendChild(banner);
}

function _updateCUBackgroundBanner(status) {
    const stepEl = document.querySelector('#cu-bg-banner .cu-bg-step');
    if (stepEl) stepEl.textContent = `Step ${status.step}/${status.total}`;
    // Keep TodoTracker in sync with background progress
    _cuCurrentStep = status.step || _cuCurrentStep;
    _cuTotalSteps = status.total || _cuTotalSteps;
    if (!_cuTasks.find(t => t.id === `cu-step-${_cuCurrentStep}`)) {
        _cuTodoNewStep(_cuCurrentStep);
    }
    _cuTodoDescribe('Running in background...', 'Background task');
}

function _removeCUBackgroundBanner() {
    const banner = document.getElementById('cu-bg-banner');
    if (banner) banner.remove();
    if (_cuStatusPollTimer) {
        _cuStatusPollTimer.stop();
        _cuStatusPollTimer = null;
    }
    todoTracker.clear();
}

/**
 * Reconnect to a running CU background task by sending a synthetic
 * message that triggers the SSE reconnection path.
 */
window.__reconnectCU = function() {
    const operator = getOperator();
    console.log('[CU-BG] Reconnecting to background task...');
    _removeCUBackgroundBanner();

    // Use sendStreamingChat with a reconnect message — the backend
    // will detect the running task and resume the event stream
    const messages = [{ role: 'user', content: '[reconnect]' }];
    // Empty model id lets the server resolve CU_MODEL_DEFAULT
    sendStreamingChat(messages, 'computer-use', window.__model || '', operator);
};

/**
 * Open the CU viewer from the CU background banner — streaming client when
 * the running session has a live virtual display, screenshot-poll modal as
 * the automatic fallback (M4 routing, design 2026-07-23 §7.1).
 */
window.__openCUBrowserView = function() {
    import('./cu-viewer-route.js')
        .then((m) => m.openCuViewer({
            sessionId: getCUSessionId() || undefined,
            deviceId: getCUDeviceId()
        }))
        .catch(() => openCUInteract());
};

// =============================================================================
// CU Session Control Exports (used by cu-drawer.js)
// =============================================================================

/**
 * Request emergency stop of the running CU task.
 * @returns {Promise<Object>} Response from the server
 */
export async function stopCUTask() {
    const operator = getOperator();
    const sessionId = getCUSessionId();
    const model = window.__model || '';
    const res = await fetch('/chat/cu-stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operator, session_id: sessionId, model })
    });
    return res.json();
}

/**
 * Clear the current CU session and start fresh.
 * @returns {Promise<Object>} Response from the server
 */
export async function newCUSession() {
    const operator = getOperator();
    const model = window.__model || '';
    const res = await fetch('/chat/cu-new-session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operator, model })
    });
    setCUSessionId(null);
    if (window.__cuDrawerSetSession) window.__cuDrawerSetSession(null);
    if (window.__cuDrawerUpdateStatus) window.__cuDrawerUpdateStatus('idle');
    return res.json();
}
