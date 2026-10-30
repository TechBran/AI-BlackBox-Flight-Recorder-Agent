/**
 * chat-bubbles.js
 * Chat bubble creation, rendering, and management
 * Includes thinking animation, bubble actions, and history display
 *
 * Source: Portal/app.js lines 3309-3373, 3853-3891, 3996-4282
 * Refactored: 2025-12-20
 */

import { $, toast } from './core-utils.js';
import { getHistoryData, setHistoryData, saveHistory, getOperator } from './state-management.js';
import { renderMarkdown, addCopyButtonsToCodeBlocks, copyToClipboard, addTimestampToBubble, highlightCodeBlocks } from './markdown-renderer.js';
import { sanitizeAssistantHtml } from './sanitize.js';
import { makeSnapshotsClickable, applySyntaxHighlighting } from './timeline-browser.js';
import { speakToBubble, setLastAssistantText } from './tts-stt.js';
import { updateHintsVisibility } from './help-hints.js';
import { scrollToBottomIfNeeded } from './ui-setup.js';
import {
    ORIGIN_HEADER, wellnessSigner, originProof, parseWellnessTime, providerLabel, withOperatorQuery,
    WELLNESS_SPEECH_OFF,
} from './wellness-section.js';

// =============================================================================
// Constants
// =============================================================================

/** Maximum history items to keep */
const MAX_HISTORY_ITEMS = 100;

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Escape HTML characters — re-export of the shared core-utils helper (kept here
 * because chat-send.js and others import it from this module).
 */
export { escapeHtml } from './core-utils.js';

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

    // Check if a download button already exists (compared by attribute value,
    // not by interpolating the src into a CSS selector)
    if (Array.from(parentElement.querySelectorAll('.download-btn')).some(a => a.getAttribute('href') === imgSrc)) {
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
    textSpan.textContent = "Thinking";

    const dots = document.createElement("span");
    dots.className = "thinking-dots";
    dots.innerHTML = '<span></span><span></span><span></span>';

    span.appendChild(textSpan);
    span.appendChild(dots);
    wrap.appendChild(span);

    return wrap;
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

    // The media / already-HTML sniffing only picks the FORMAT (pre-rendered HTML
    // vs markdown). It is never a trust decision: assistant text is
    // attacker-steerable and every branch goes through the sanitizer, which
    // fails closed to escaped text (audit P01-chat-core-web-01).
    if (role === 'assistant' && containsMedia) {
        span.innerHTML = sanitizeAssistantHtml(contentString);
        textContentForActions = (span.textContent || "").trim();
    } else if (role === 'assistant' && isAlreadyHtml) {
        span.innerHTML = sanitizeAssistantHtml(contentString);
        textContentForActions = span.textContent || span.innerText || "";
        setTimeout(() => {
            highlightCodeBlocks(span);   // stored HTML: not highlighted at parse time
            addCopyButtonsToCodeBlocks(span);
            makeSnapshotsClickable(span);
            applySyntaxHighlighting(span);
        }, 0);
    } else if (role === 'assistant' && !containsMedia) {
        // Code is highlighted while parsing (final render), not on a timer.
        span.innerHTML = renderMarkdown(contentString);
        textContentForActions = span.textContent || span.innerText || "";
        setTimeout(() => addCopyButtonsToCodeBlocks(span), 0);
        setTimeout(() => makeSnapshotsClickable(span), 0);
        setTimeout(() => applySyntaxHighlighting(span), 0);
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
    if (role === "assistant" || (role === "user" && rawCopyText)) {
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
 * STRUCTURAL CHOICE (deliberate): the chip is appended INSIDE the .bubble
 * element, NOT as a sibling in #history. Several code paths assume one
 * #history child per historyData entry (e.g. updateThinkingBubble maps a
 * bubble's DOM index straight to its historyData index) — a sibling chip
 * element would shift every index after it and corrupt those mappings.
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
    btn.title = "Message failed to send — retry";
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
 * @param {{wellness?: ?{wellness_ref: string, wellness_used: ?Object}}} [opts] -
 *   wellness: the turn's health use (wellnessFromReply of a POST /chat
 *   result): its history entry keeps the ref and the frame, the bubble gets
 *   its footer (and Show numbers inside the app), and it is never read aloud
 */
export function updateThinkingBubble(thinkingBubble, newContent, { wellness = null } = {}) {
    if (!thinkingBubble) return;

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
        // Media replies are pre-rendered HTML; still sanitized (fails closed).
        if (containsMedia) {
            span.innerHTML = sanitizeAssistantHtml(newContentString);
            textContentForActions = (span.textContent || "").trim();
        } else {
            span.innerHTML = renderMarkdown(newContentString);   // highlights code while parsing
            textContentForActions = span.textContent || span.innerText || "";
            setTimeout(() => addCopyButtonsToCodeBlocks(span), 0);
            setTimeout(() => makeSnapshotsClickable(span), 0);
            setTimeout(() => applySyntaxHighlighting(span), 0);
        }
    }

    if (containsImage) {
        setTimeout(() => {
            span.querySelectorAll('img').forEach(img => addDownloadButtonToImage(img, thinkingBubble));
        }, 0);
    }

    // Raw-copy parity for polling-mode bubbles: the long-press path reads this
    // (same rule as appendBubble — raw string for markdown content, plain-text
    // extraction only for media content).
    thinkingBubble._bbxCopyText = containsMedia ? textContentForActions : newContentString;

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

    // A health turn's entry keeps its ref (the history scrub, Show numbers)
    // and its frame (the footer), as a streamed turn's does.
    const tagEntry = (entry) => {
        if (!wellness) return;
        entry.wellness_ref = wellness.wellness_ref;
        if (wellness.wellness_used) entry.wellness_used = wellness.wellness_used;
    };

    if (thinkingIndex > -1 && historyData[thinkingIndex]) {
        historyData[thinkingIndex].content = newContentString;
        delete historyData[thinkingIndex].pending;
        delete historyData[thinkingIndex].pendingTimestamp;
        tagEntry(historyData[thinkingIndex]);
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
                tagEntry(historyData[i]);
                if (textContentForActions) {
                    setLastAssistantText(textContentForActions);
                }
                setHistoryData(historyData);
                saveHistory();
                break;
            }
        }
    }

    // A POST /chat turn a wellness tool fed (its result_data names the refs)
    // gets the footer a streamed one gets; a reply the box masked live ("[·]")
    // is a health reply even without them. Neither is ever read aloud.
    const maskedHealth = newContentString.includes(WELLNESS_MASK);
    if (wellness) decorateWellnessBubble(thinkingBubble, { content: newContentString, ...wellness });
    if (maskedHealth) markWellnessReply(thinkingBubble);
    const healthReply = maskedHealth || !!wellness;

    // Trigger Auto-TTS if enabled
    if (window.triggerAutoTTS && textContentForActions && !healthReply) {
        window.triggerAutoTTS(textContentForActions, thinkingBubble);
    }
}

// =============================================================================
// Health (M7): the "Used your health data" footer and Show numbers
// =============================================================================
//
// A reply a wellness_* tool fed carries the stream's UI-only wellness_used
// frame ({ref, categories, as_of, provider, provider_label}; chat-send.js
// keeps it on the history entry with wellness_ref = its first ref). A POST
// /chat turn carries the same in its result_data (wellnessFromReply). Its
// bubble gets a footer naming the source, "as of" and who the data was
// shared with, never a value. A MASKED reply (the ledger's copy, "[·]" where
// a health value was) with a ref offers "Show numbers" inside the BlackBox
// Android app only: the app signs GET /wellness/ref/{ref}?operator=X (the
// delivered envelope) and the display strings show in a private in-page
// panel that is never saved and closes on navigation. A snapshot reloaded
// from the ledger (the Timeline popup) offers it too, from the refs its
// WELLNESS gauge names (wellnessGaugeRefs, snapshotNumbersFooter).
// Everything here is built with createElement / textContent.
//
// Such a reply is never read aloud from the Portal: its text (values
// included) would go to the voice service, a destination the health consent
// does not name (a cloud TTS provider; on-box voices write the audio under
// Portal/uploads), so a local-only category could reach the cloud (rules W26,
// W31). chat-send.js skips auto-TTS for it, and the speak buttons of its
// bubble (reply and reasoning) only say why.

/** The ledger's placeholder for a masked health value (Orchestrator/wellness/mask.py MASK). */
export const WELLNESS_MASK = '[·]';
// A delivered ref ("w-20261029-7f3a", Orchestrator/wellness/api.new_ref). It
// goes into the signed path of GET /wellness/ref/{ref}: only characters that
// need no percent-encoding, so the path the app signs is the one the box
// checks.
const WELLNESS_REF = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const WELLNESS_REFS_MAX = 8;
const WELLNESS_CATEGORIES = new Set(['sleep', 'heart', 'activity', 'blood_pressure']);
const WELLNESS_PART_MAX = 80;
const WELLNESS_LINE_MAX = 300;
const WELLNESS_LINES_MAX = 160;

const isWellnessRef = (ref) => typeof ref === 'string' && WELLNESS_REF.test(ref);

/** One line of text from the box, or '' (non-strings, blank, runaway lengths). */
function wellnessText(value, max = WELLNESS_PART_MAX) {
    return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim().slice(0, max) : '';
}

/** A box key ("sleep_duration") in words ("sleep duration"). */
function humanize(key) {
    return wellnessText(typeof key === 'string' ? key.replace(/_/g, ' ') : '');
}

const listOf = (v) => (Array.isArray(v) ? v : []);

/**
 * A wellness_used frame (or a stored copy) cut down to well-typed fields, or
 * null when it names no usable ref.
 * @param {*} data
 * @returns {?{ref: string, refs: string[], categories: string[], as_of: ?string,
 *   provider: ?string, provider_label: ?string, source_label: ?string}}
 */
export function normalizeWellnessUsed(data) {
    if (!data || typeof data !== 'object' || !isWellnessRef(data.ref)) return null;
    return {
        ref: data.ref,
        refs: [...new Set([data.ref, ...listOf(data.refs)].filter(isWellnessRef))].slice(0, WELLNESS_REFS_MAX),
        categories: [...new Set(listOf(data.categories).filter((c) => WELLNESS_CATEGORIES.has(c)))],
        as_of: typeof data.as_of === 'string' ? data.as_of.slice(0, 40) : null,
        provider: wellnessText(data.provider) || null,
        provider_label: wellnessText(data.provider_label) || null,
        source_label: wellnessText(data.source_label, 200) || null,
    };
}

/**
 * One turn's wellness_used frames as one: the FIRST ref stays the message's
 * wellness_ref, every ref is kept for Show numbers, categories are joined, the
 * newest as_of and provider win. A frame with no usable ref changes nothing.
 */
export function mergeWellnessUsed(prev, data) {
    const base = normalizeWellnessUsed(prev);
    const next = normalizeWellnessUsed(data);
    if (!base || !next) return base || next;
    return {
        ref: base.ref,
        refs: [...new Set([...base.refs, ...next.refs])].slice(0, WELLNESS_REFS_MAX),
        categories: [...new Set([...base.categories, ...next.categories])],
        as_of: next.as_of || base.as_of,
        provider: next.provider || base.provider,
        provider_label: next.provider_label || base.provider_label,
        source_label: base.source_label || next.source_label,
    };
}

/** Local date and time of a box ISO time: "27 Oct, 22:40" (null when unreadable). */
function formatWhen(iso, { locale, timeZone } = {}) {
    const t = parseWellnessTime(iso);
    if (t === null) return null;
    try {
        return new Date(t).toLocaleString(locale, { ...(timeZone ? { timeZone } : {}),
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch {
        return null;                            // an unknown locale or zone
    }
}

/**
 * "as of" text: the local time when it is today, else the date and time.
 * @param {*} iso - The frame's as_of (ISO 8601 with offset)
 * @param {{now?: number, locale?: string, timeZone?: string}} [opts] - Injectable for tests
 * @returns {?string} null when missing or unreadable (never "Jan 1")
 */
export function formatAsOf(iso, { now = Date.now(), locale, timeZone } = {}) {
    const t = parseWellnessTime(iso);
    if (t === null) return null;
    const zone = timeZone ? { timeZone } : {};
    try {
        const day = (ms) => new Date(ms).toLocaleDateString(locale, { ...zone, year: 'numeric', month: '2-digit', day: '2-digit' });
        if (day(t) === day(now)) {
            return new Date(t).toLocaleTimeString(locale, { ...zone, hour: '2-digit', minute: '2-digit' });
        }
    } catch {
        return null;
    }
    return formatWhen(iso, { locale, timeZone });
}

/**
 * The footer under a reply a wellness tool fed:
 * "Used your health data · <source> · as of <time> · <provider>". The source is
 * the label the turn recorded (chat-send.js: the Health card's last status for
 * the turn's operator), else `sourceLabel`, else "your phone". It never
 * carries a health value.
 * @param {*} used - The turn's wellness_used
 * @param {{sourceLabel?: ?string, now?: number, locale?: string, timeZone?: string}} [opts]
 * @returns {string} '' for a frame with no usable ref
 */
export function formatWellnessFooter(used, { sourceLabel = null, now = Date.now(), locale, timeZone } = {}) {
    const u = normalizeWellnessUsed(used);
    if (!u) return '';
    const parts = ['Used your health data', u.source_label || wellnessText(sourceLabel, 200) || 'your phone'];
    const asOf = formatAsOf(u.as_of, { now, locale, timeZone });
    if (asOf) parts.push(`as of ${asOf}`);
    const who = u.provider_label || providerLabel(u.provider);
    if (who) parts.push(who);
    return parts.join(' · ');
}

/** A reply whose stored text is the ledger's masked copy, with a ref to fetch its numbers by. */
export function isMaskedHealthTurn(text, ref) {
    return typeof text === 'string' && text.includes(WELLNESS_MASK) && isWellnessRef(ref);
}

/** Every delivered ref of a history entry: its wellness_ref first, then the frame's. */
export function wellnessRefsOf(entry) {
    if (!entry || typeof entry !== 'object') return [];
    const used = normalizeWellnessUsed(entry.wellness_used);
    return [...new Set([entry.wellness_ref, ...(used ? used.refs : [])].filter(isWellnessRef))]
        .slice(0, WELLNESS_REFS_MAX);
}

// The box's WELLNESS gauge line (Orchestrator/wellness/redact.py _GAUGE_RE)
// and a ref in it (api.new_ref: "w-<day>-" and 4, or after collisions 8, hex).
const GAUGE_LINE = /^WELLNESS: ([^\n]+?) · values not recorded[ \t]*$/m;
const GAUGE_REF = /^w-\d{8}-(?:[0-9a-f]{4}|[0-9a-f]{8})$/;

/** A snapshot's header GAUGES block ("GAUGES" up to the blank line), or ''. */
function gaugesBlock(text) {
    const log = text.indexOf('\nRaw Session Log\n');
    const head = log < 0 ? text : text.slice(0, log + 1);
    let start;
    if (head.startsWith('GAUGES\n')) {
        start = 'GAUGES\n'.length;
    } else {
        const i = head.indexOf('\nGAUGES\n');
        if (i < 0) return '';
        start = i + '\nGAUGES\n'.length;
    }
    const end = head.indexOf('\n\n', start);
    return end < 0 ? head.slice(start) : head.slice(start, end + 1);
}

/**
 * The refs a minted snapshot's WELLNESS gauge names ("WELLNESS: sleep,heart ·
 * night of 2026-10-28 · ref w-20261029-7f3a, w-20261029-a1b2 · values not
 * recorded"), in its order, at most 8. Read from the header's GAUGES block
 * only, as the box reads it (redact.parse_tag): the session log below it is
 * anyone's text. A hidden turn's gauge ("WELLNESS: [wellness conversation ·
 * not shared]") and an untagged snapshot name none.
 * @param {*} text - The snapshot text (GET /fossil/snapshot/{id} content)
 * @returns {string[]}
 */
export function wellnessGaugeRefs(text) {
    if (typeof text !== 'string' || !text.includes('WELLNESS: ')) return [];
    const m = GAUGE_LINE.exec(gaugesBlock(text.replace(/\r\n/g, '\n')));
    if (!m) return [];
    let refs = [];
    for (const part of m[1].split(' · ').slice(1)) {
        if (part.startsWith('ref ')) refs = part.slice(4).split(',').map((r) => r.trim());
    }
    return [...new Set(refs.filter((r) => GAUGE_REF.test(r)))].slice(0, WELLNESS_REFS_MAX);
}

/**
 * The health use of a POST /chat turn, from its result_data: wellness_used
 * (the turn's newest delivery, the frame the stream's wellness_used carries),
 * wellness_refs (every ref of the turn, oldest first) and wellness_ref (the
 * first). As for a streamed turn, the FIRST ref is the message's and every
 * ref is kept for Show numbers. null when the turn used none (the box then
 * sends none of the keys).
 * @param {*} data - The task's result_data
 * @returns {?{wellness_ref: string, wellness_used: ?Object}}
 */
export function wellnessFromReply(data) {
    if (!data || typeof data !== 'object') return null;
    const frame = normalizeWellnessUsed(data.wellness_used);
    const refs = [...new Set([data.wellness_ref, ...listOf(data.wellness_refs), ...(frame ? frame.refs : [])]
        .filter(isWellnessRef))].slice(0, WELLNESS_REFS_MAX);
    if (!refs.length) return null;
    return { wellness_ref: refs[0], wellness_used: frame ? { ...frame, ref: refs[0], refs } : null };
}

function metricLine(m) {
    if (!m || typeof m !== 'object') return '';
    const parts = [`${wellnessText(m.label) || humanize(m.metric)}: ${wellnessText(m.display) || 'not recorded'}`];
    if (wellnessText(m.total_display)) parts.push(`total ${wellnessText(m.total_display)}`);
    if (wellnessText(m.range_display)) parts.push(`range ${wellnessText(m.range_display)}`);
    const vs = m.vs_usual && typeof m.vs_usual === 'object' ? m.vs_usual : {};
    if (wellnessText(vs.label)) {
        parts.push(wellnessText(vs.delta_display) ? `${wellnessText(vs.label)}, ${wellnessText(vs.delta_display)}` : wellnessText(vs.label));
    }
    const cover = m.coverage && typeof m.coverage === 'object' ? wellnessText(m.coverage.label) : '';
    if (cover) parts.push(cover);
    return parts.join(' · ');
}

function sessionLine(s, opts) {
    if (!s || typeof s !== 'object') return '';
    const kind = wellnessText(s.type) && s.type !== s.kind ? humanize(s.type) : humanize(s.kind);
    const when = [formatWhen(s.start, opts), formatWhen(s.end, opts)].filter(Boolean).join(' to ');
    const parts = [[kind, when].filter(Boolean).join(', ')];
    for (const v of [s.duration, s.distance]) {
        if (wellnessText(v)) parts.push(wellnessText(v));
    }
    if (wellnessText(s.avg_hr_display)) parts.push(`average heart rate ${wellnessText(s.avg_hr_display)}`);
    if (wellnessText(s.calories)) parts.push(wellnessText(s.calories));
    if (Number.isFinite(s.hr_coverage_pct)) parts.push(`heart rate coverage ${s.hr_coverage_pct}%`);
    return parts.filter(Boolean).join(' · ');
}

function pointLine(p, opts) {
    if (!p || typeof p !== 'object') return '';
    const when = formatWhen(p.t, opts) || wellnessText(String(p.t ?? ''), 20);
    const cover = Number.isFinite(p.coverage_pct) ? ` (${p.coverage_pct}% covered)` : '';
    return `${when}: ${wellnessText(p.display) || 'not recorded'}${cover}`;
}

// A history cell is a stored value with its unit (the table has no display strings).
function historyLine(env, row) {
    const cols = listOf(env.columns);
    if (!Array.isArray(row) || cols.length < 2) return '';
    const units = env.units && typeof env.units === 'object' ? env.units : {};
    const cells = [];
    for (let i = 1; i < cols.length && i < row.length; i++) {
        const col = cols[i];
        const v = row[i];
        if (col === 'night_worn') {
            if (v === 1) cells.push('worn at night');
            else if (v === 0) cells.push('not worn at night');
        } else if (col === 'day_wear_h') {
            if (Number.isFinite(v)) cells.push(`worn ${v} h by day`);
        } else if (v === null || v === undefined) {
            cells.push(`${humanize(col)} not recorded`);
        } else if (typeof v === 'number' || typeof v === 'string') {
            const unit = wellnessText(units[col], 20);
            cells.push(`${humanize(col)} ${wellnessText(String(v), 40)}${unit && unit !== 'clock' ? ` ${unit}` : ''}`);
        }
    }
    return `${wellnessText(String(row[0] ?? ''), 20)}: ${cells.join(', ')}`;
}

function gapLine(g) {
    if (!g || typeof g !== 'object') return '';
    const day = wellnessText(g.day, 20);
    const reason = wellnessText(g.reason);
    return `Not recorded: ${humanize(g.metric) || 'a metric'}${day ? ` on ${day}` : ''}${reason ? ` (${reason})` : ''}`;
}

/**
 * What the Show numbers panel lists for one delivered envelope (GET
 * /wellness/ref/{ref}: {ref, ts, tool, categories, payload}): a title, the
 * "as of" line, then the display strings with their coverage, and the gaps.
 * @param {*} delivery
 * @param {{now?: number, locale?: string, timeZone?: string}} [opts]
 * @returns {{title: string, lines: string[]}} empty for an error envelope or junk
 */
export function refPanel(delivery, opts = {}) {
    const env = delivery && typeof delivery === 'object' && delivery.payload && typeof delivery.payload === 'object'
        ? delivery.payload : null;
    if (!env || env.ok !== true) return { title: '', lines: [] };
    const asOf = formatAsOf(env.as_of, opts);
    const lines = [asOf ? `As of ${asOf}${env.stale ? ' (not synced recently)' : ''}` : 'As of: not synced yet'];
    const add = (text) => {
        const line = wellnessText(text, WELLNESS_LINE_MAX);
        if (line && lines.length < WELLNESS_LINES_MAX) lines.push(line);
    };
    let title = '';
    switch (env.tool) {
        case 'wellness_summary':
            title = wellnessText(env.period && env.period.label);
            listOf(env.metrics).forEach((m) => add(metricLine(m)));
            listOf(env.sessions).forEach((s) => add(sessionLine(s, opts)));
            break;
        case 'wellness_series':
            title = [humanize(env.metric), wellnessText(env.window)].filter(Boolean).join(' · ');
            listOf(env.points).forEach((p) => add(pointLine(p, opts)));
            break;
        case 'wellness_sessions':
            title = [env.kind ? `${humanize(env.kind)} sessions` : '', wellnessText(env.window)].filter(Boolean).join(' · ');
            listOf(env.sessions).forEach((s) => add(sessionLine(s, opts)));
            break;
        case 'wellness_history':
            title = env.start && env.end ? `${wellnessText(env.start, 20)} to ${wellnessText(env.end, 20)}` : '';
            listOf(env.rows).forEach((row) => add(historyLine(env, row)));
            break;
        default:
            break;
    }
    listOf(env.gaps).forEach((g) => add(gapLine(g)));
    return { title: title || 'Your health data', lines };
}

/**
 * One delivered envelope for `operator` (the Portal's), signed by the app:
 * {state: 'ok', delivery} | {state: 'unavailable'} (no proof, 401/403/404, the
 * box's kill switch, a reply for another ref) | {state: 'error'} (network, 5xx).
 * The path names the operator: the app signs a GET only when it does.
 */
async function loadWellnessRef(ref, operator) {
    if (!operator) return { state: 'unavailable' };
    const path = withOperatorQuery(`/wellness/ref/${encodeURIComponent(ref)}`, operator);
    const proof = await originProof('GET', path);
    if (!proof) return { state: 'unavailable' };
    let res;
    try {
        res = await fetch(path, { cache: 'no-store', headers: { [ORIGIN_HEADER]: proof } });
    } catch {
        return { state: 'error' };
    }
    if (!res.ok) return { state: (res.status >= 400 && res.status < 500) || res.status === 503 ? 'unavailable' : 'error' };
    let body = null;
    try {
        body = await res.json();
    } catch {
        body = null;
    }
    if (!body || typeof body !== 'object' || body.ref !== ref) return { state: 'unavailable' };
    return { state: 'ok', delivery: body };
}

// Every open numbers panel's close(), so navigation closes them all.
const _openNumbers = new Set();
let _numbersClosersBound = false;

/** Close every open Show numbers panel (navigation, an operator switch, the page hidden). */
export function closeWellnessNumbers() {
    for (const close of [..._openNumbers]) close();
}

/** Close the Show numbers panels under `root` (a popup closing, or drawn again). */
export function closeWellnessNumbersIn(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    for (const footer of root.querySelectorAll('.wellness-footer')) {
        if (footer._closeNumbers) footer._closeNumbers();
    }
}

function bindNumbersClosers() {
    if (_numbersClosersBound || typeof window === 'undefined') return;
    _numbersClosersBound = true;
    for (const type of ['pagehide', 'popstate', 'hashchange', 'bbx:operator-changed']) {
        window.addEventListener(type, closeWellnessNumbers);
    }
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) closeWellnessNumbers();
    });
}

function wellnessNode(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
}

function renderNumbers(panel, results) {
    const shown = results.filter((r) => r.state === 'ok');
    const body = [];
    if (!shown.length) {
        body.push(wellnessNode('p', 'wellness-numbers-note', results.some((r) => r.state === 'error')
            ? "Couldn't load the numbers right now. Try again in a moment." : 'Not available'));
    }
    for (const r of shown) {
        const { title, lines } = refPanel(r.delivery);
        if (!lines.length) continue;
        body.push(wellnessNode('p', 'wellness-numbers-title', title));
        const list = wellnessNode('ul', 'wellness-numbers-lines');
        list.append(...lines.map((line) => wellnessNode('li', '', line)));
        body.push(list);
    }
    if (shown.length && body.length === 0) body.push(wellnessNode('p', 'wellness-numbers-note', 'Not available'));
    if (shown.length && shown.length < results.length) body.push(wellnessNode('p', 'wellness-numbers-note', 'Some of it is not available.'));
    body.push(wellnessNode('p', 'wellness-numbers-note', 'Shown on this screen only. Not saved.'));
    panel.replaceChildren(...body);
}

/** Open (or close) the numbers panel under a masked bubble's footer. */
async function toggleNumbers(footer, refs, button) {
    if (footer._closeNumbers) {
        footer._closeNumbers();
        return;
    }
    bindNumbersClosers();
    const panel = wellnessNode('div', 'wellness-numbers');
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-label', 'Your health numbers');
    panel.append(wellnessNode('p', 'wellness-numbers-note', 'Loading…'));
    footer.after(panel);
    const close = () => {
        panel.remove();
        _openNumbers.delete(close);
        footer._closeNumbers = null;
        button.textContent = 'Show numbers';
        button.setAttribute('aria-expanded', 'false');
    };
    footer._closeNumbers = close;
    _openNumbers.add(close);
    button.textContent = 'Hide numbers';
    button.setAttribute('aria-expanded', 'true');

    const operator = getOperator();
    const results = [];
    for (const ref of refs) results.push(await loadWellnessRef(ref, operator));
    if (!_openNumbers.has(close)) return;      // closed while it loaded
    if (operator !== getOperator()) {
        close();
        return;
    }
    renderNumbers(panel, results);
}

/**
 * The "Show numbers" button of a footer: opens (and closes) the panel of
 * `refs` under it.
 */
function numbersButton(footer, refs) {
    const button = wellnessNode('button', 'wellness-show-numbers', 'Show numbers');
    button.type = 'button';
    button.setAttribute('aria-expanded', 'false');
    button.addEventListener('click', () => {
        toggleNumbers(footer, refs, button).catch((err) => console.warn('[Health] Show numbers failed', err && err.name));
    });
    return button;
}

/**
 * Show numbers for a snapshot opened from the ledger (the Timeline popup, a
 * SNAP-id link). A signed GET /fossil/snapshot/{id} gives the owner's
 * enrolled phone the turn as minted, masked, with its WELLNESS gauge; the
 * refs come from that gauge. A footer with the button, or null: outside the
 * app, for a turn that is not masked, or one whose gauge names no ref (a
 * hidden turn's never does). The panel is the chat's: never saved, closed on
 * navigation, and by the caller when the popup closes (closeWellnessNumbersIn).
 * @param {*} text - The snapshot text
 * @returns {?HTMLElement}
 */
export function snapshotNumbersFooter(text) {
    const refs = wellnessGaugeRefs(text);
    if (!refs.length || !isMaskedHealthTurn(text, refs[0]) || !wellnessSigner()) return null;
    const footer = wellnessNode('div', 'wellness-footer wellness-snapshot-footer');
    footer.append(wellnessNode('span', 'wellness-footer-text', 'Used your health data · values not kept in the ledger'),
        numbersButton(footer, refs));
    return footer;
}

/** What a speak button of a health reply says instead of speaking (wellness-section.js). */
export { WELLNESS_SPEECH_OFF };

// A health reply's bubble (and its streaming reasoning panel) carries this.
const WELLNESS_MARK = 'wellness';
let _speechGuardBound = false;

/** True for an element inside a health reply's bubble or reasoning panel. */
export function isWellnessReply(el) {
    return !!(el && typeof el.closest === 'function' && el.closest('[data-wellness="1"]'));
}

// One capture listener for the page: it runs before every speak handler
// (the buttons' own onclick, chat-send's delegated reasoning control) and
// stops the click when the control belongs to a health reply.
function guardWellnessSpeech(e) {
    const target = e.target;
    const control = target && typeof target.closest === 'function'
        && target.closest('.speak-btn, [data-bubble-action="thinking-speak"]');
    if (!control || !isWellnessReply(control)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    toast(WELLNESS_SPEECH_OFF);
}

function bindSpeechGuard() {
    if (_speechGuardBound || typeof document === 'undefined') return;
    _speechGuardBound = true;
    document.addEventListener('click', guardWellnessSpeech, true);
}

/** Mark a health reply's bubble (and its floating reasoning panel) so it is never spoken. */
function markWellnessReply(bubble) {
    bindSpeechGuard();
    bubble.dataset[WELLNESS_MARK] = '1';
    if (bubble._floatingPanel && bubble._floatingPanel.dataset) bubble._floatingPanel.dataset[WELLNESS_MARK] = '1';
}

/**
 * Give an assistant bubble its health footer (a turn with wellness_used) and,
 * inside the app, Show numbers (a masked turn with a ref). Idempotent; an
 * entry without the fields (every reply before M7, every other turn) is left
 * exactly as it was. A reply with either field is marked as a health reply
 * (data-wellness="1"): its speak buttons do not speak.
 * @param {HTMLElement} bubble - The assistant bubble
 * @param {Object} entry - The history entry: {content, wellness_ref?, wellness_used?}
 * @param {{sourceLabel?: ?string}} [opts] - The source label when the turn
 *   recorded none (default: none, so "your phone"; the chat history is one
 *   store for every operator, so the label of whoever is selected now is
 *   never borrowed)
 * @returns {?HTMLElement} The footer, or null when there is none
 */
export function decorateWellnessBubble(bubble, entry, opts = {}) {
    if (!bubble || !entry || typeof entry !== 'object') return null;
    for (const old of [...bubble.children]) {
        if (old.classList.contains('wellness-footer') || old.classList.contains('wellness-numbers')) {
            if (old._closeNumbers) old._closeNumbers();
            old.remove();
        }
    }
    const used = normalizeWellnessUsed(entry.wellness_used);
    const refs = wellnessRefsOf(entry);
    if (used || refs.length > 0) markWellnessReply(bubble);
    const offerNumbers = refs.length > 0 && isMaskedHealthTurn(entry.content, refs[0]) && !!wellnessSigner();
    if (!used && !offerNumbers) return null;

    const footer = wellnessNode('div', 'wellness-footer');
    if (used) {
        const sourceLabel = opts.sourceLabel !== undefined ? opts.sourceLabel : null;
        footer.append(wellnessNode('span', 'wellness-footer-text', formatWellnessFooter(used, { sourceLabel })));
    }
    if (offerNumbers) footer.append(numbersButton(footer, refs));
    const anchor = [...bubble.children].find((c) => c.classList.contains('streaming-response-panel'))
        || [...bubble.children].find((c) => c.classList.contains('bubble-text'));
    if (anchor) anchor.after(footer);
    else bubble.appendChild(footer);
    return footer;
}

// The history renderer is chat-send.js renderHistory (it restores pending
// turns and the reasoning and Computer Use panels, which this module's copy
// did not; audit P01-chat-core-web-20). Clearing is ui-setup's Clear handler.
