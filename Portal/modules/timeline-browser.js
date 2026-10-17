/**
 * timeline-browser.js
 * Snapshot timeline browsing, filtering, and detail display
 *
 * Source: Portal/app.js lines 3429-3505, 4905-5195
 * Refactored: 2025-12-20
 */

import { $, toast, toastError, escapeHtml } from './core-utils.js';

// Re-export applySyntaxHighlighting from markdown-renderer for convenience
// (many modules import it from timeline-browser due to historical coupling)
export { applySyntaxHighlighting } from './markdown-renderer.js';

// =============================================================================
// State
// =============================================================================

/** All loaded timeline snapshots */
let timelineData = [];

/** Filtered timeline snapshots based on search/filter */
let filteredTimelineData = [];

/** Currently selected snapshot ID */
let currentSelectedSnapshotId = null;

// =============================================================================
// Snapshot ID Processing
// =============================================================================

/**
 * Make snapshot IDs in content clickable
 * @param {HTMLElement} container - Container to process
 * @param {Object} options - Options
 * @param {boolean} [options.skipPreTags=true] - Skip processing inside pre/code tags
 */
export function makeSnapshotsClickable(container, options = {}) {
    console.log('[SNAP-CLICK] makeSnapshotsClickable called on:', container?.tagName, container?.className);
    if (!container) return;

    const { skipPreTags = true } = options;

    // Find all text nodes in the container
    const walker = document.createTreeWalker(
        container,
        NodeFilter.SHOW_TEXT,
        null,
        false
    );

    const textNodes = [];
    let node;
    while (node = walker.nextNode()) {
        // Skip text nodes inside code blocks or pre tags
        let parent = node.parentElement;
        if (skipPreTags && parent && (parent.tagName === 'CODE' || parent.tagName === 'PRE' || parent.closest('pre'))) {
            continue;
        }
        // Skip if already processed
        if (parent && parent.classList && parent.classList.contains('clickable-snapshot')) {
            continue;
        }
        textNodes.push(node);
    }

    console.log('[SNAP-CLICK] Found', textNodes.length, 'text nodes to check for SNAP IDs');

    // Process each text node to find and wrap snapshot IDs
    let snapCount = 0;
    textNodes.forEach(textNode => {
        const text = textNode.textContent;
        const regex = /\b(SNAP-(?:\d{8}-)?\d+)\b/gi;

        if (regex.test(text)) {
            console.log('[SNAP-CLICK] Found SNAP pattern in text:', text.substring(0, 100));
            snapCount++;
            regex.lastIndex = 0;

            const fragment = document.createDocumentFragment();
            let lastIndex = 0;
            let match;

            while ((match = regex.exec(text)) !== null) {
                // Add text before match
                if (match.index > lastIndex) {
                    fragment.appendChild(document.createTextNode(text.substring(lastIndex, match.index)));
                }

                // Create clickable span for snapshot ID
                const span = document.createElement('span');
                span.setAttribute('data-highlight', 'snapshot');
                span.className = 'clickable-snapshot';
                span.setAttribute('data-snap-id', match[0]);
                span.textContent = match[0];
                fragment.appendChild(span);

                lastIndex = regex.lastIndex;
            }

            // Add remaining text
            if (lastIndex < text.length) {
                fragment.appendChild(document.createTextNode(text.substring(lastIndex)));
            }

            // Replace the text node with the fragment
            textNode.parentNode.replaceChild(fragment, textNode);
            console.log('[MAKE-CLICKABLE] Processed snapshot IDs in text node');
        }
    });

    console.log('[SNAP-CLICK] Total SNAP IDs found and processed:', snapCount);
}

// =============================================================================
// Data Loading
// =============================================================================

/**
 * Load timeline data from the volume
 * @returns {Promise<Array>} Timeline data array
 */
export async function loadTimelineData() {
    try {
        console.log('[TIMELINE] Loading timeline data...');
        const response = await fetch('/assert');
        const data = await response.json();

        console.log('[TIMELINE] Volume size:', data.volume ? data.volume.length : 0, 'bytes');

        const volumeText = data.volume || "";

        if (!volumeText) {
            console.warn('[TIMELINE] Volume is empty');
            toastError('Volume is empty - no snapshots to display');
            return [];
        }

        // Match snapshot format: === START SNAPSHOT — UTC {utc} — {snap_id} (version) ===
        const snapshotRegex = /=== START SNAPSHOT — UTC ([^\s]+) — (SNAP-\d+-\d+) \([^)]+\) ===([\s\S]*?)=== END SNAPSHOT — \2 — UTC \1 ===/g;

        timelineData = [];
        let match;
        let matchCount = 0;

        console.log('[TIMELINE] Starting regex matching...');

        while ((match = snapshotRegex.exec(volumeText)) !== null) {
            matchCount++;
            const timestamp = match[1];
            const snapId = match[2];
            const content = match[3];

            // Extract operator from content
            const opMatch = content.match(/OPERATOR:\s*([^\n]+)/);
            const operator = opMatch ? opMatch[1].trim() : "Unknown";

            // Extract preview from user message
            const userMatch = content.match(/Raw Session Log[\s\S]*?operator=\S+\s+user:\s*([^\n]+)/);
            const preview = userMatch ? userMatch[1].substring(0, 100) : "No preview available";

            timelineData.push({
                snapId,
                operator,
                timestamp,
                content,
                preview
            });

            if (matchCount <= 3) {
                console.log('[TIMELINE] Matched snapshot:', { snapId, operator, timestamp, contentLength: content.length });
            }
        }

        console.log('[TIMELINE] Total snapshots matched:', matchCount);

        if (matchCount === 0) {
            console.warn('[TIMELINE] No snapshots matched! Checking volume format...');
            console.log('[TIMELINE] Volume start:', volumeText.substring(0, 500));
            toastError('No snapshots found - check volume format');
            return [];
        }

        // Reverse to show most recent first
        timelineData.reverse();
        filteredTimelineData = [...timelineData];

        console.log('[TIMELINE] Timeline data loaded successfully:', timelineData.length, 'snapshots');
        return timelineData;
    } catch (e) {
        console.error('[TIMELINE] Failed to load timeline data:', e);
        toastError('Failed to load timeline data: ' + e.message);
        return [];
    }
}

// =============================================================================
// Rendering
// =============================================================================

/**
 * Render the timeline list
 */
export function renderTimelineList() {
    const listContainer = $("timelineList");
    if (!listContainer) {
        console.error('[TIMELINE] List container not found');
        return;
    }

    console.log('[TIMELINE] Rendering', filteredTimelineData.length, 'snapshots');

    if (filteredTimelineData.length === 0) {
        listContainer.innerHTML = '<div class="timeline-loading">No snapshots found</div>';
        return;
    }

    listContainer.innerHTML = '';

    filteredTimelineData.forEach((snapshot, index) => {
        const item = document.createElement('div');
        item.className = 'timeline-item';
        item.dataset.snapId = snapshot.snapId;

        const date = new Date(snapshot.timestamp);
        const dateStr = date.toLocaleString();

        // operator + preview are parsed out of the ledger, which chat, MCP, cron
        // and inbound SMS all write to — escape every field (P04-media-tasks-fx-web-05).
        item.innerHTML = `
            <div class="timeline-item-header">
                <span class="timeline-item-id">${escapeHtml(snapshot.snapId)}</span>
                <span class="timeline-item-operator">${escapeHtml(snapshot.operator)}</span>
            </div>
            <div class="timeline-item-time">${escapeHtml(dateStr)}</div>
            <div class="timeline-item-preview">${escapeHtml(snapshot.preview)}</div>
        `;

        item.addEventListener('click', () => {
            console.log('[TIMELINE] Item clicked:', snapshot.snapId);
            showTimelineDetail(snapshot);
        });
        listContainer.appendChild(item);
    });

    console.log('[TIMELINE] List rendered with', filteredTimelineData.length, 'items');
}

/**
 * Show snapshot detail in popup
 * @param {Object} snapshot - Snapshot data
 */
export function showTimelineDetail(snapshot) {
    console.log('[TIMELINE] showTimelineDetail called with:', snapshot);

    const popup = $("snapshotPopup");
    const popupBody = $("snapshotPopupBody");

    if (!popup || !popupBody) {
        console.error('[TIMELINE] Snapshot popup elements not found');
        return;
    }

    console.log('[TIMELINE] Showing popup for snapshot:', snapshot.snapId);

    const date = new Date(snapshot.timestamp);
    const dateStr = date.toLocaleString();

    // Escape HTML in content
    const escapedContent = escapeHtml(snapshot.content);

    popupBody.innerHTML = `
        <div class="detail-meta">
            <div class="detail-row">
                <strong>Snapshot ID:</strong> <code>${escapeHtml(snapshot.snapId)}</code>
            </div>
            <div class="detail-row">
                <strong>Operator:</strong> ${escapeHtml(snapshot.operator)}
            </div>
            <div class="detail-row">
                <strong>Timestamp:</strong> ${escapeHtml(dateStr)}
            </div>
            <div class="detail-row">
                <strong>Size:</strong> ${(snapshot.content.length / 1024).toFixed(2)} KB
            </div>
        </div>
        <div class="detail-content-box">
            <pre>${escapedContent}</pre>
        </div>
    `;

    // Highlight the selected item
    document.querySelectorAll('.timeline-item').forEach(item => {
        item.classList.remove('selected');
    });
    const selectedItem = Array.from(document.querySelectorAll('.timeline-item'))
        .find(item => item.dataset.snapId === snapshot.snapId);
    if (selectedItem) {
        selectedItem.classList.add('selected');
        currentSelectedSnapshotId = snapshot.snapId;
    }

    // Show popup
    popup.classList.remove('hide');

    // Make snapshot IDs in the popup content clickable
    setTimeout(() => makeSnapshotsClickable(popupBody, { skipPreTags: false }), 0);

    console.log('[TIMELINE] Popup displayed');
}

// =============================================================================
// Filtering
// =============================================================================

/**
 * Filter timeline based on search and operator
 */
export function filterTimeline() {
    const searchInput = $("timelineSearch");
    const operatorFilter = $("timelineOperatorFilter");

    const searchTerm = searchInput ? searchInput.value.toLowerCase() : '';
    const selectedOperator = operatorFilter ? operatorFilter.value : '';

    filteredTimelineData = timelineData.filter(snapshot => {
        const matchesSearch = !searchTerm ||
            snapshot.snapId.toLowerCase().includes(searchTerm) ||
            snapshot.preview.toLowerCase().includes(searchTerm) ||
            snapshot.content.toLowerCase().includes(searchTerm);

        const matchesOperator = !selectedOperator || snapshot.operator === selectedOperator;

        return matchesSearch && matchesOperator;
    });

    renderTimelineList();
}

// =============================================================================
// Setup
// =============================================================================

/**
 * Setup timeline browser UI and event handlers
 */
export function setupTimelineBrowser() {
    const timelineModal = $("timelineModal");
    const btnOpenTimeline = $("openTimelineBtn");
    const btnCloseTimeline = $("btnCloseTimeline");
    const btnBackToList = $("btnBackToList");
    const timelineSearch = $("timelineSearch");
    const operatorFilter = $("timelineOperatorFilter");

    if (!timelineModal) return;

    // Open timeline
    if (btnOpenTimeline) {
        btnOpenTimeline.addEventListener('click', async () => {
            if (timelineModal) timelineModal.classList.remove('hide');

            await loadTimelineData();

            // Populate operator filter
            if (operatorFilter) {
                const operators = [...new Set(timelineData.map(s => s.operator))];
                operatorFilter.innerHTML = '<option value="">All Operators</option>';
                operators.forEach(op => {
                    const option = document.createElement('option');
                    option.value = op;
                    option.textContent = op;
                    operatorFilter.appendChild(option);
                });
            }

            renderTimelineList();
        });
    }

    // Close timeline
    if (btnCloseTimeline) {
        btnCloseTimeline.addEventListener('click', () => {
            if (timelineModal) timelineModal.classList.add('hide');
            const listContainer = $("timelineList");
            const detailContainer = $("timelineDetail");
            if (listContainer) listContainer.classList.remove('hide');
            if (detailContainer) detailContainer.classList.add('hide');
        });
    }

    // Close snapshot popup
    const btnCloseSnapshotPopup = $("btnCloseSnapshotPopup");
    const snapshotPopup = $("snapshotPopup");

    if (btnCloseSnapshotPopup) {
        btnCloseSnapshotPopup.addEventListener('click', () => {
            if (snapshotPopup) snapshotPopup.classList.add('hide');
        });
    }

    // Close popup when clicking outside
    if (snapshotPopup) {
        snapshotPopup.addEventListener('click', (e) => {
            if (e.target === snapshotPopup) {
                snapshotPopup.classList.add('hide');
            }
        });
    }

    // Back to list button
    if (btnBackToList) {
        btnBackToList.addEventListener('click', () => {
            const listContainer = $("timelineList");
            const detailContainer = $("timelineDetail");
            if (listContainer) listContainer.classList.remove('hide');
            if (detailContainer) detailContainer.classList.add('hide');
        });
    }

    // Search and filter
    if (timelineSearch) {
        timelineSearch.addEventListener('input', filterTimeline);
    }

    if (operatorFilter) {
        operatorFilter.addEventListener('change', filterTimeline);
    }
}

// =============================================================================
// Exports for state access
// =============================================================================

/**
 * Get current timeline data
 * @returns {Array} Timeline data
 */
export function getTimelineData() {
    return timelineData;
}

/**
 * Get filtered timeline data
 * @returns {Array} Filtered timeline data
 */
export function getFilteredTimelineData() {
    return filteredTimelineData;
}

/**
 * Get currently selected snapshot ID
 * @returns {string|null} Selected snapshot ID
 */
export function getCurrentSelectedSnapshotId() {
    return currentSelectedSnapshotId;
}
