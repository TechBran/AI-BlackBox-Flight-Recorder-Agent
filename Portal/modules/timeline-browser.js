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

/** Loaded timeline snapshots (pages of the index, newest first) */
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
//
// The list comes from the snapshot index a page at a time: GET
// /api/snapshots/recent (id, operator, time, preview; ?before=<id> pages back),
// and a snapshot's text from GET /fossil/snapshot/{id} only when it is opened.
// This used to download the whole ledger from /assert (58 MB on the dev box,
// growing with every snapshot) and regex it in the browser on every open and
// every SNAP-id click (audit P04-media-tasks-fx-web-06).

/** Snapshots per page of the list. */
export const TIMELINE_PAGE_SIZE = 100;

/** Results for an Enter in the search box (server-side hybrid search). */
const TIMELINE_SEARCH_LIMIT = 50;

/** Typing filters the loaded list after this pause. */
const FILTER_DEBOUNCE_MS = 200;

/** Operators named in the snapshot index, for the filter dropdown. */
let timelineOperators = [];

/** An older page exists (the list ends with a "Load older" button). */
let timelineHasMore = false;

/** Operator the loaded pages were fetched for ('' = all operators). */
let timelineOperator = '';

/** Query whose search results the list shows ('' = the recent pages). */
let timelineSearchQuery = '';

/** Detail requests in flight: only the newest one may fill the popup. */
let detailRequestSeq = 0;

let filterTimer = null;

/** A list row from /api/snapshots/recent or /fossil/hybrid -> a timeline item. */
function toTimelineItem(row) {
    return {
        snapId: String(row.snap_id || ''),
        operator: row.operator || 'Unknown',
        timestamp: row.timestamp || '',
        type: row.type || 'normal',
        preview: row.snippet || 'No preview available',
        content: null,  // fetched from /fossil/snapshot/{id} when opened
    };
}

async function fetchJson(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
}

async function fetchTimelinePage(operator, before) {
    const params = new URLSearchParams({ limit: String(TIMELINE_PAGE_SIZE) });
    if (operator) params.set('operator', operator);
    if (before) params.set('before', before);
    const data = await fetchJson(`/api/snapshots/recent?${params}`);
    return {
        items: (data.results || []).map(toTimelineItem),
        hasMore: Boolean(data.has_more),
        operators: Array.isArray(data.operators) ? data.operators : [],
    };
}

/**
 * Fetch one snapshot's full text. null when the box has no such snapshot.
 * @param {string} snapId - Full or short (SNAP-<n>) snapshot id
 * @returns {Promise<Object|null>} Timeline item with `content`
 */
async function fetchSnapshotDetail(snapId) {
    const response = await fetch(`/fossil/snapshot/${encodeURIComponent(snapId)}`);
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const meta = data.metadata || {};
    return {
        snapId: String(data.snap_id || snapId),
        operator: meta.operator || 'Unknown',
        timestamp: meta.timestamp || '',
        type: meta.type || 'normal',
        preview: '',
        content: data.content || '',
    };
}

/**
 * Load the newest page of the timeline (and reset paging and search).
 * @param {Object} [options]
 * @param {string} [options.operator] - Only this operator's snapshots ('' = all)
 * @returns {Promise<Array>} Timeline data array
 */
export async function loadTimelineData(options = {}) {
    const operator = options.operator ?? timelineOperator;
    try {
        console.log('[TIMELINE] Loading timeline page...', operator || '(all operators)');
        const page = await fetchTimelinePage(operator, '');
        timelineOperator = operator;
        timelineSearchQuery = '';
        timelineData = page.items;
        timelineHasMore = page.hasMore;
        if (page.operators.length) timelineOperators = page.operators;
        filteredTimelineData = applyLocalFilter(timelineData);
        console.log('[TIMELINE] Loaded', timelineData.length, 'snapshots; older pages:', timelineHasMore);
        return timelineData;
    } catch (e) {
        console.error('[TIMELINE] Failed to load timeline data:', e);
        toastError('Failed to load timeline data: ' + e.message);
        return [];
    }
}

/**
 * Append the next older page to the loaded timeline.
 * @returns {Promise<Array>} Timeline data array
 */
export async function loadMoreTimeline() {
    if (!timelineHasMore || timelineSearchQuery || timelineData.length === 0) return timelineData;
    const oldest = timelineData[timelineData.length - 1].snapId;
    try {
        const page = await fetchTimelinePage(timelineOperator, oldest);
        const seen = new Set(timelineData.map(s => s.snapId));
        timelineData = timelineData.concat(page.items.filter(s => !seen.has(s.snapId)));
        timelineHasMore = page.hasMore;
        filteredTimelineData = applyLocalFilter(timelineData);
    } catch (e) {
        console.error('[TIMELINE] Failed to load older snapshots:', e);
        toastError('Failed to load older snapshots: ' + e.message);
    }
    return timelineData;
}

/**
 * Search every snapshot on the box (server-side hybrid search, the same one
 * the Android Timeline uses). An empty query goes back to the recent pages.
 * @param {string} query - Search text
 * @returns {Promise<Array>} Timeline data array
 */
export async function searchTimeline(query) {
    const q = String(query || '').trim();
    if (!q) return loadTimelineData();
    const params = new URLSearchParams({ q, limit: String(TIMELINE_SEARCH_LIMIT) });
    if (timelineOperator) params.set('operator', timelineOperator);
    try {
        const data = await fetchJson(`/fossil/hybrid?${params}`);
        timelineSearchQuery = q;
        timelineData = (data.results || []).map(toTimelineItem);
        timelineHasMore = false;
        filteredTimelineData = [...timelineData];
    } catch (e) {
        console.error('[TIMELINE] Search failed:', e);
        toastError('Search failed: ' + e.message);
    }
    return timelineData;
}

/**
 * Open a snapshot by id (SNAP-id links in chat). Short SNAP-<n> ids resolve
 * server-side to the newest snapshot with that number.
 * @param {string} snapId - Snapshot id
 * @returns {Promise<boolean>} Whether the snapshot was found and shown
 */
export async function openSnapshotById(snapId) {
    const id = String(snapId || '').trim().toUpperCase();
    if (!id) return false;
    const loaded = timelineData.find(s => s.snapId === id && s.content !== null);
    if (loaded) {
        await showTimelineDetail(loaded);
        return true;
    }
    try {
        const detail = await fetchSnapshotDetail(id);
        if (!detail) {
            toastError('Snapshot ' + id + ' not found in volume');
            return false;
        }
        await showTimelineDetail(detail);
        return true;
    } catch (e) {
        console.error('[TIMELINE] Failed to load snapshot', id, e);
        toastError('Failed to load snapshot: ' + e.message);
        return false;
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

    listContainer.innerHTML = '';

    if (timelineSearchQuery) {
        const note = document.createElement('div');
        note.className = 'timeline-loading';
        note.textContent = `Search results for "${timelineSearchQuery}" (clear the box to go back)`;
        listContainer.appendChild(note);
    }

    if (filteredTimelineData.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'timeline-loading';
        empty.textContent = 'No snapshots found';
        listContainer.appendChild(empty);
    }

    filteredTimelineData.forEach((snapshot) => {
        const item = document.createElement('div');
        item.className = 'timeline-item';
        item.dataset.snapId = snapshot.snapId;
        if (snapshot.snapId === currentSelectedSnapshotId) item.classList.add('selected');

        const date = new Date(snapshot.timestamp);
        const dateStr = date.toLocaleString();

        // operator + preview come from the ledger, which chat, MCP, cron and
        // inbound SMS all write to — escape every field (P04-media-tasks-fx-web-05).
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

    if (timelineHasMore && !timelineSearchQuery) {
        const more = document.createElement('div');
        more.className = 'timeline-loading';
        const btn = document.createElement('button');
        btn.className = 'btn timeline-more';
        btn.textContent = 'Load older snapshots';
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            btn.textContent = 'Loading...';
            await loadMoreTimeline();
            renderTimelineList();
        });
        more.appendChild(btn);
        listContainer.appendChild(more);
    }

    console.log('[TIMELINE] List rendered with', filteredTimelineData.length, 'items');
}

/**
 * Fill the popup body with one snapshot's metadata and text.
 * @param {HTMLElement} popupBody - Popup body element
 * @param {Object} snapshot - Timeline item with `content`
 */
function renderTimelineDetail(popupBody, snapshot) {
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

    // Make snapshot IDs in the popup content clickable
    setTimeout(() => makeSnapshotsClickable(popupBody, { skipPreTags: false }), 0);
}

/**
 * Show snapshot detail in popup. The text is fetched from the per-id route
 * the first time a listed snapshot is opened, then kept on the item.
 * @param {Object} snapshot - Snapshot data
 */
export async function showTimelineDetail(snapshot) {
    console.log('[TIMELINE] showTimelineDetail called with:', snapshot && snapshot.snapId);

    const popup = $("snapshotPopup");
    const popupBody = $("snapshotPopupBody");

    if (!popup || !popupBody || !snapshot) {
        console.error('[TIMELINE] Snapshot popup elements not found');
        return;
    }

    // Highlight the selected item
    document.querySelectorAll('.timeline-item').forEach(item => {
        item.classList.toggle('selected', item.dataset.snapId === snapshot.snapId);
    });
    currentSelectedSnapshotId = snapshot.snapId;

    // Show popup
    popup.classList.remove('hide');

    const request = ++detailRequestSeq;
    if (snapshot.content === null || snapshot.content === undefined) {
        popupBody.innerHTML = '<div class="timeline-loading">Loading snapshot...</div>';
        let detail = null;
        try {
            detail = await fetchSnapshotDetail(snapshot.snapId);
        } catch (e) {
            console.error('[TIMELINE] Failed to load snapshot', snapshot.snapId, e);
        }
        if (request !== detailRequestSeq) return;  // another snapshot was opened meanwhile
        if (!detail) {
            popupBody.innerHTML = '<div class="timeline-loading">This snapshot could not be loaded</div>';
            toastError('Snapshot ' + snapshot.snapId + ' could not be loaded');
            return;
        }
        snapshot.content = detail.content;
    }

    renderTimelineDetail(popupBody, snapshot);
    console.log('[TIMELINE] Popup displayed');
}

// =============================================================================
// Filtering
// =============================================================================

/** The loaded items whose id, operator or preview contain the search box text. */
function applyLocalFilter(items) {
    const searchInput = $("timelineSearch");
    const term = searchInput ? searchInput.value.trim().toLowerCase() : '';
    if (!term) return [...items];
    return items.filter(snapshot =>
        snapshot.snapId.toLowerCase().includes(term) ||
        snapshot.operator.toLowerCase().includes(term) ||
        snapshot.preview.toLowerCase().includes(term));
}

/**
 * Filter the loaded timeline by the search box (id, operator, preview).
 * Bodies are not loaded, so they are not searched here; Enter searches every
 * snapshot on the box (searchTimeline).
 */
export function filterTimeline() {
    const searchInput = $("timelineSearch");
    const term = searchInput ? searchInput.value.trim() : '';

    if (timelineSearchQuery) {
        // Search results stay as returned until the box is cleared (back to
        // the recent pages) or Enter searches again.
        if (!term) loadTimelineData().then(() => renderTimelineList());
        return;
    }

    filteredTimelineData = applyLocalFilter(timelineData);
    renderTimelineList();
}

/**
 * Fill the operator dropdown from the snapshot index, keeping the selection.
 * @param {HTMLSelectElement} operatorFilter - The dropdown
 */
function populateOperatorFilter(operatorFilter) {
    if (!operatorFilter) return;
    const selected = operatorFilter.value;
    const operators = timelineOperators.length
        ? timelineOperators
        : [...new Set(timelineData.map(s => s.operator))];
    operatorFilter.innerHTML = '';
    const all = document.createElement('option');
    all.value = '';
    all.textContent = 'All Operators';
    operatorFilter.appendChild(all);
    operators.forEach(op => {
        const option = document.createElement('option');
        option.value = op;
        option.textContent = op;
        operatorFilter.appendChild(option);
    });
    operatorFilter.value = operators.includes(selected) ? selected : '';
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
            if (timelineSearch) timelineSearch.value = '';
            const listContainer = $("timelineList");
            if (listContainer) listContainer.innerHTML = '<div class="timeline-loading">Loading snapshots...</div>';

            await loadTimelineData({ operator: operatorFilter ? operatorFilter.value : '' });
            populateOperatorFilter(operatorFilter);
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

    // Search: typing filters the loaded list after a pause; Enter searches
    // every snapshot on the box.
    if (timelineSearch) {
        timelineSearch.placeholder = 'Filter loaded snapshots (Enter searches all)';
        timelineSearch.addEventListener('input', () => {
            clearTimeout(filterTimer);
            filterTimer = setTimeout(filterTimeline, FILTER_DEBOUNCE_MS);
        });
        timelineSearch.addEventListener('keydown', async (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            clearTimeout(filterTimer);
            await searchTimeline(timelineSearch.value);
            renderTimelineList();
        });
    }

    // Operator filter: the server pages that operator's snapshots
    if (operatorFilter) {
        operatorFilter.addEventListener('change', async () => {
            const query = timelineSearchQuery;
            await loadTimelineData({ operator: operatorFilter.value });
            if (query) await searchTimeline(query);
            renderTimelineList();
        });
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
