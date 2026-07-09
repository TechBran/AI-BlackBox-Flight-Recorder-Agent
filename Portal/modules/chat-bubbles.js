/**
 * chat-bubbles.js
 * Chat bubble creation, rendering, and management
 * Includes thinking animation, bubble actions, and history display
 *
 * Source: Portal/app.js lines 3309-3373, 3853-3891, 3996-4282
 * Refactored: 2025-12-20
 */

import { $, toast } from './core-utils.js';
import { getHistoryData, setHistoryData, saveHistory } from './state-management.js';
import { renderMarkdown, addCopyButtonsToCodeBlocks, copyToClipboard, addTimestampToBubble } from './markdown-renderer.js';
import { makeSnapshotsClickable, applySyntaxHighlighting } from './timeline-browser.js';
import { speakToBubble, setLastAssistantText } from './tts-stt.js';
import { updateHintsVisibility } from './help-hints.js';
import { scrollToBottomIfNeeded } from './ui-setup.js';

// =============================================================================
// Constants
// =============================================================================

/** Thinking animation messages */
const THINKING_MESSAGES = [
    "Thinking",
    "Making magic happen",
    "Consulting the AI oracle",
    "Brewing some intelligence",
    "Contemplating the cosmos",
    "Channeling digital wisdom",
    "Computing brilliance",
    "Summoning insights",
    "Crafting your answer",
    "Pondering deeply",
    "Analyzing patterns",
    "Generating brilliance"
];

/** Maximum history items to keep */
const MAX_HISTORY_ITEMS = 100;

// =============================================================================
// State
// =============================================================================

/** Interval ID for thinking animation */
let thinkingInterval = null;

/** Current thinking message index */
let currentThinkingIndex = 0;

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Escape HTML characters
 * @param {string} text - Text to escape
 * @returns {string} Escaped text
 */
export function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// =============================================================================
// Download Button
// =============================================================================

/**
 * Add download button to an image element
 * @param {HTMLImageElement} imgElement - Image element
 * @param {HTMLElement} parentElement - Parent bubble element
 */
export function addDownloadButtonToImage(imgElement, parentElement) {
    if (!imgElement || !parentElement) return;

    const imgSrc = imgElement.getAttribute('src');
    if (!imgSrc) return;

    // Check if a download button already exists
    if (parentElement.querySelector(`.download-btn[href="${imgSrc}"]`)) {
        return;
    }

    const downloadLink = document.createElement('a');
    downloadLink.href = imgSrc;

    // Try to derive filename, fallback to generic name
    let filename = 'generated-image.png';
    try {
        const url = new URL(imgSrc, window.location.origin);
        const pathParts = url.pathname.split('/');
        if (pathParts.length > 0) {
            const potentialFilename = pathParts[pathParts.length - 1];
            if (potentialFilename && potentialFilename.includes('.')) {
                filename = potentialFilename;
            }
        }
    } catch (e) {
        console.warn("Error parsing image URL for filename:", e);
    }

    downloadLink.download = filename;
    downloadLink.textContent = '💾';
    downloadLink.title = 'Download Image';
    downloadLink.classList.add('btn', 'download-btn');

    if (imgElement.parentNode) {
        imgElement.parentNode.insertBefore(downloadLink, imgElement.nextSibling);
    }
}

// =============================================================================
// Thinking Bubble Animation
// =============================================================================

/**
 * Create an animated thinking bubble
 * @returns {HTMLElement} Thinking bubble element
 */
export function createAnimatedThinkingBubble() {
    const wrap = document.createElement("div");
    wrap.className = "bubble assistant thinking";

    const span = document.createElement("span");
    span.className = "bubble-text";

    const textSpan = document.createElement("span");
    textSpan.className = "thinking-message";
    textSpan.textContent = THINKING_MESSAGES[0];

    const dots = document.createElement("span");
    dots.className = "thinking-dots";
    dots.innerHTML = '<span></span><span></span><span></span>';

    span.appendChild(textSpan);
    span.appendChild(dots);
    wrap.appendChild(span);

    return wrap;
}

/**
 * Start the thinking animation
 * @param {HTMLElement} bubble - Thinking bubble element
 */
export function startThinkingAnimation(bubble) {
    if (!bubble) return;

    currentThinkingIndex = 0;
    const textSpan = bubble.querySelector(".thinking-message");

    thinkingInterval = setInterval(() => {
        currentThinkingIndex = (currentThinkingIndex + 1) % THINKING_MESSAGES.length;
        if (textSpan) {
            textSpan.style.opacity = '0';
            setTimeout(() => {
                textSpan.textContent = THINKING_MESSAGES[currentThinkingIndex];
                textSpan.style.opacity = '1';
            }, 150);
        }
    }, 2500);
}

/**
 * Stop the thinking animation
 */
export function stopThinkingAnimation() {
    if (thinkingInterval) {
        clearInterval(thinkingInterval);
        thinkingInterval = null;
    }
}

// =============================================================================
// Bubble Creation
// =============================================================================

/**
 * Append a bubble to the history without saving to localStorage
 * @param {string} role - Bubble role (user, assistant, system)
 * @param {string} content - Bubble content
 * @returns {HTMLElement} The created bubble element
 */
export function appendBubble(role, content) {
    const contentString = String(content ?? "");

    const wrap = document.createElement("div");
    wrap.className = "bubble " + role;

    const span = document.createElement("span");
    span.className = "bubble-text";

    let textContentForActions = contentString;
    const containsImage = role === 'assistant' && contentString.includes('<img');
    const containsAudio = role === 'assistant' && contentString.includes('<audio');
    const containsVideo = role === 'assistant' && contentString.includes('<video');
    const containsMedia = containsImage || containsAudio || containsVideo;

    const isAlreadyHtml = contentString.includes('<pre') || contentString.includes('<code') ||
                          contentString.includes('class="hljs') || contentString.includes('bubble-text');

    if (role === 'assistant' && containsMedia) {
        span.innerHTML = contentString;
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = contentString;
        textContentForActions = (tempDiv.textContent || tempDiv.innerText || "").trim();
    } else if (role === 'assistant' && isAlreadyHtml) {
        span.innerHTML = contentString;
        textContentForActions = span.textContent || span.innerText || "";
        setTimeout(() => {
            span.querySelectorAll('pre code').forEach(block => {
                if (window.hljs && !block.classList.contains('hljs')) {
                    hljs.highlightElement(block);
                }
            });
            addCopyButtonsToCodeBlocks(span);
            makeSnapshotsClickable(span);
            applySyntaxHighlighting(span);
        }, 0);
    } else if (role === 'assistant' && !containsMedia) {
        span.innerHTML = renderMarkdown(contentString);
        textContentForActions = span.textContent || span.innerText || "";
        setTimeout(() => addCopyButtonsToCodeBlocks(span), 0);
        setTimeout(() => makeSnapshotsClickable(span), 0);
        setTimeout(() => applySyntaxHighlighting(span), 0);
        setTimeout(() => {
            span.querySelectorAll('pre code').forEach(block => {
                if (window.hljs && !block.classList.contains('hljs')) {
                    hljs.highlightElement(block);
                }
            });
        }, 10);
    } else {
        span.textContent = contentString;
    }
    wrap.appendChild(span);

    // The RAW text a copy action must yield. Aligns appendBubble's assistant copy
    // with streaming bubbles (chat-send.js setupBubbleControls copies the raw
    // markdown string): for markdown-rendered assistant content copy the raw
    // content string we were given; for media/pre-rendered-HTML content fall back
    // to the plain-text extraction (raw markup on the clipboard would be worse).
    // User bubbles render via textContent, so their content string IS raw.
    const rawCopyText = (role === 'assistant' && (containsMedia || isAlreadyHtml))
        ? textContentForActions
        : contentString;
    // Long-press-to-copy (touch) reads this — see initLongPressCopy below.
    wrap._bbxCopyText = rawCopyText;

    // Controls bar: assistant bubbles get speak + copy; user bubbles get copy
    // (speak stays assistant-only).
    let controlsBar = null;
    if ((role === "assistant" || role === "user") && (role === "assistant" || rawCopyText)) {
        controlsBar = document.createElement("div");
        controlsBar.className = "bubble-controls";
        wrap.appendChild(controlsBar);
    }

    if (containsImage) {
        setTimeout(() => {
            span.querySelectorAll('img').forEach(img => addDownloadButtonToImage(img, wrap));
        }, 0);
    }

    if (controlsBar) {
        if (role === "assistant" && textContentForActions) {
            const spk = document.createElement("button");
            spk.className = "bubble-btn speak-btn";
            spk.title = "Generate audio playback";
            spk.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
            </svg>`;
            spk.onclick = () => speakToBubble(textContentForActions, wrap, spk);
            controlsBar.appendChild(spk);
        }

        if (rawCopyText) {
            const cpy = document.createElement("button");
            cpy.className = "bubble-btn copy-btn";
            cpy.title = "Copy text";
            cpy.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>`;
            cpy.onclick = () => copyToClipboard(rawCopyText);
            controlsBar.appendChild(cpy);
        }
    }

    addTimestampToBubble(wrap);

    const hist = $("history");
    if (hist) {
        hist.appendChild(wrap);
        scrollToBottomIfNeeded();
        updateHintsVisibility();
    }
    return wrap;
}

// =============================================================================
// Retry-on-failed-send (chip under a failed user bubble)
// =============================================================================

/**
 * Attach a retry chip under a FAILED user bubble. Clicking clears the failed
 * state, removes the error assistant bubble, and re-fires the same text via
 * chat-send.js retryFailedTurn (dynamic import — chat-send already imports this
 * module, so a static import here would be circular).
 *
 * v1 limitation: attachments are NOT re-sent (File objects are gone after
 * clearAttachedFiles) — noted in the title attribute.
 *
 * XSS: chip content is set via textContent only.
 *
 * @param {HTMLElement} bubbleEl - The failed user bubble
 * @param {string} retryText - The raw prompt text to re-send
 */
export function attachRetryChip(bubbleEl, retryText) {
    if (!bubbleEl || !retryText) return;
    if (bubbleEl.querySelector('.bubble-retry')) return; // already attached

    const btn = document.createElement("button");
    btn.className = "bubble-retry";
    btn.type = "button";
    btn.textContent = "↻ Retry";
    btn.title = "Message failed to send — retry (attachments are not re-sent)";
    btn.onclick = async () => {
        btn.disabled = true;
        try {
            const { retryFailedTurn } = await import('./chat-send.js');
            await retryFailedTurn(bubbleEl, retryText);
        } catch (e) {
            console.error('[Retry] Failed to retry turn:', e);
            btn.disabled = false;
            toast('Retry failed: ' + (e?.message || String(e)));
        }
    };
    bubbleEl.appendChild(btn);
}

// =============================================================================
// Long-press-to-copy (touch parity with the copy button)
// =============================================================================

/**
 * Delegated long-press (~500ms) on any .bubble copies its raw text.
 * - Touch pointers ONLY — desktop mouse text selection is untouched.
 * - Cancelled by movement beyond a small threshold (scrolling) or pointerup.
 * - Skips presses that start on interactive children (buttons, links, media).
 * No user-select changes; native selection behavior is left as-is.
 */
const LONG_PRESS_MS = 500;
const LONG_PRESS_MOVE_PX = 10;
let _lpTimer = null;
let _lpStartX = 0;
let _lpStartY = 0;

function _lpCancel() {
    if (_lpTimer) {
        clearTimeout(_lpTimer);
        _lpTimer = null;
    }
}

function initLongPressCopy() {
    document.addEventListener('pointerdown', (e) => {
        if (e.pointerType !== 'touch') return; // touch parity only — never mouse
        if (!e.target || !e.target.closest) return;
        if (e.target.closest('button, a, audio, video, input, textarea, select')) return;
        const bubble = e.target.closest('.bubble');
        if (!bubble) return;

        _lpStartX = e.clientX;
        _lpStartY = e.clientY;
        _lpCancel();
        _lpTimer = setTimeout(() => {
            _lpTimer = null;
            const text = bubble._bbxCopyText ||
                (bubble.querySelector('.bubble-text')?.textContent || '').trim();
            if (!text) return;
            copyToClipboard(text);
            toast('Copied');
            // Brief visual cue on the bubble itself
            bubble.classList.add('bubble-copied-flash');
            setTimeout(() => bubble.classList.remove('bubble-copied-flash'), 1200);
        }, LONG_PRESS_MS);
    }, { passive: true });

    document.addEventListener('pointermove', (e) => {
        if (!_lpTimer) return;
        const dx = e.clientX - _lpStartX;
        const dy = e.clientY - _lpStartY;
        if ((dx * dx + dy * dy) > LONG_PRESS_MOVE_PX * LONG_PRESS_MOVE_PX) _lpCancel();
    }, { passive: true });

    document.addEventListener('pointerup', _lpCancel, { passive: true });
    document.addEventListener('pointercancel', _lpCancel, { passive: true });
}

if (typeof document !== 'undefined') {
    initLongPressCopy();
}

/**
 * Add a bubble to history and persist to localStorage
 * @param {string} role - Bubble role
 * @param {string} content - Bubble content
 * @returns {HTMLElement} The created bubble element
 */
export function addBubble(role, content) {
    console.log(`[addBubble] Creating ${role} bubble, content length: ${content?.length || 0}`);
    const safeContent = (typeof content === 'string') ? content : JSON.stringify(content);

    let historyData = getHistoryData();
    historyData.push({ role, content: safeContent });
    console.log(`[addBubble] historyData now has ${historyData.length} items`);

    // Proactive trimming
    if (historyData.length > MAX_HISTORY_ITEMS) {
        const removedCount = historyData.length - MAX_HISTORY_ITEMS;
        historyData = historyData.slice(-MAX_HISTORY_ITEMS);
        setHistoryData(historyData);
        console.log(`[History] Proactive trim: removed ${removedCount} oldest items`);

        const hist = $("history");
        if (hist && hist.children.length > MAX_HISTORY_ITEMS) {
            const bubblesToRemove = hist.children.length - MAX_HISTORY_ITEMS;
            for (let i = 0; i < bubblesToRemove; i++) {
                if (hist.firstChild) hist.removeChild(hist.firstChild);
            }
        }
    } else {
        setHistoryData(historyData);
    }

    saveHistory();
    return appendBubble(role, safeContent);
}

/**
 * Remove the last assistant bubble from history and DOM
 * Used when a tool call is detected to remove the pre-tool response
 * so only the tool-enhanced response is shown
 */
export function removeLastAssistantBubble() {
    let historyData = getHistoryData();

    // Find and remove the last assistant entry
    for (let i = historyData.length - 1; i >= 0; i--) {
        if (historyData[i].role === 'assistant') {
            historyData.splice(i, 1);
            setHistoryData(historyData);
            saveHistory();
            console.log('[removeLastAssistantBubble] Removed last assistant entry from history');
            break;
        }
    }

    // Remove the last assistant bubble from DOM
    const hist = $("history");
    if (hist) {
        const bubbles = hist.querySelectorAll('.bubble.assistant');
        if (bubbles.length > 0) {
            const lastBubble = bubbles[bubbles.length - 1];
            lastBubble.remove();
            console.log('[removeLastAssistantBubble] Removed last assistant bubble from DOM');
        }
    }
}

/**
 * Update a thinking bubble with final content
 * @param {HTMLElement} thinkingBubble - The thinking bubble to update
 * @param {string} newContent - New content to display
 */
export function updateThinkingBubble(thinkingBubble, newContent) {
    if (!thinkingBubble) return;

    stopThinkingAnimation();
    thinkingBubble.classList.remove("thinking");

    const newContentString = String(newContent ?? "");

    thinkingBubble.querySelectorAll('.playbtn, .copybtn, .download-btn, .thinking-dots, .bubble-controls').forEach(el => el.remove());

    const span = thinkingBubble.querySelector(".bubble-text");
    const historyData = getHistoryData();
    const thinkingIndex = Array.from($("history").children).indexOf(thinkingBubble);

    let textContentForActions = newContentString;
    const containsImage = newContentString.includes('<img');
    const containsAudio = newContentString.includes('<audio');
    const containsVideo = newContentString.includes('<video');
    const containsMedia = containsImage || containsAudio || containsVideo;

    if (span) {
        if (containsMedia) {
            span.innerHTML = newContentString;
            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = newContentString;
            textContentForActions = (tempDiv.textContent || tempDiv.innerText || "").trim();
        } else {
            span.innerHTML = renderMarkdown(newContentString);
            textContentForActions = span.textContent || span.innerText || "";
            setTimeout(() => addCopyButtonsToCodeBlocks(span), 0);
            setTimeout(() => makeSnapshotsClickable(span), 0);
            setTimeout(() => applySyntaxHighlighting(span), 0);
            setTimeout(() => {
                span.querySelectorAll('pre code').forEach(block => {
                    if (window.hljs && !block.classList.contains('hljs')) {
                        hljs.highlightElement(block);
                    }
                });
            }, 10);
        }
    }

    if (containsImage) {
        setTimeout(() => {
            span.querySelectorAll('img').forEach(img => addDownloadButtonToImage(img, thinkingBubble));
        }, 0);
    }

    const controlsBar = document.createElement("div");
    controlsBar.className = "bubble-controls";
    thinkingBubble.appendChild(controlsBar);

    if (textContentForActions) {
        const spk = document.createElement("button");
        spk.className = "bubble-btn speak-btn";
        spk.title = "Listen";
        spk.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
            <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
            <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
        </svg>`;
        spk.onclick = () => speakToBubble(textContentForActions, thinkingBubble, spk);
        controlsBar.appendChild(spk);
    }

    if (textContentForActions) {
        const cpy = document.createElement("button");
        cpy.className = "bubble-btn copy-btn";
        cpy.title = "Copy text";
        cpy.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
        </svg>`;
        cpy.onclick = () => copyToClipboard(textContentForActions);
        controlsBar.appendChild(cpy);
    }

    addTimestampToBubble(thinkingBubble);

    if (thinkingIndex > -1 && historyData[thinkingIndex]) {
        historyData[thinkingIndex].content = newContentString;
        delete historyData[thinkingIndex].pending;
        delete historyData[thinkingIndex].pendingTimestamp;
        if (textContentForActions) {
            setLastAssistantText(textContentForActions);
        }
        setHistoryData(historyData);
        saveHistory();
    } else {
        console.warn('[updateThinkingBubble] Index mismatch, searching for pending messages');
        for (let i = historyData.length - 1; i >= 0; i--) {
            if (historyData[i].role === 'assistant' && historyData[i].pending) {
                console.log('[updateThinkingBubble] Found pending message at index', i, 'updating content');
                historyData[i].content = newContentString;
                delete historyData[i].pending;
                delete historyData[i].pendingTimestamp;
                if (textContentForActions) {
                    setLastAssistantText(textContentForActions);
                }
                setHistoryData(historyData);
                saveHistory();
                break;
            }
        }
    }

    // Trigger Auto-TTS if enabled
    if (window.triggerAutoTTS && textContentForActions) {
        window.triggerAutoTTS(textContentForActions, thinkingBubble);
    }
}

/**
 * Render all history items to the DOM
 */
export function renderHistory() {
    const hist = $("history");
    if (!hist) return;

    // Clear existing bubbles
    hist.innerHTML = '';

    const historyData = getHistoryData();
    historyData.forEach(item => {
        const bubble = appendBubble(item.role, item.content);
        // Restore the retry affordance on user turns whose send failed
        if (item.role === 'user' && item.failed && bubble) {
            if (item.failedAt) bubble.dataset.failedAt = String(item.failedAt);
            attachRetryChip(bubble, item.content);
        }
    });

    // Scroll to bottom after restoring history
    scrollToBottomIfNeeded();
}

/**
 * Clear all chat history
 */
export function clearHistory() {
    setHistoryData([]);
    saveHistory();

    const hist = $("history");
    if (hist) {
        hist.innerHTML = '';
        updateHintsVisibility();
    }

    toast("Chat cleared");
}
