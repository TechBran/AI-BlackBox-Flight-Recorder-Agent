/**
 * media-manager.js
 * Media Manager modal functionality - browse, preview, and manage uploaded media files
 * Redesigned: List view with inline expandable players and file type sorting
 *
 * Created: 2025-12-27
 * Redesigned: 2025-01-25
 */

import { $, toast, toastError, escapeHtml, escapeAttr } from './core-utils.js';

// =============================================================================
// State
// =============================================================================

/** Current page in media list */
let currentPage = 1;

/** Total pages available */
let totalPages = 1;

/** Items per page */
const perPage = 50;

/** All media files (cached for client-side filtering) */
let allMediaFiles = [];

/** Filtered files after search/type filter applied */
let filteredFiles = [];

/** Track the currently expanded item */
let expandedItemElement = null;

// =============================================================================
// Media Manager Modal
// =============================================================================

/**
 * Open the media manager modal and load media files
 */
export async function openMediaManager() {
    const modal = $('mediaManagerModal');
    if (!modal) {
        console.error('[MediaManager] Modal element not found');
        return;
    }

    modal.classList.remove('hide');
    currentPage = 1;
    expandedItemElement = null;

    // Reset filters
    const searchInput = $('mediaSearch');
    const typeFilter = $('mediaTypeFilter');
    const sortBy = $('mediaSortBy');
    if (searchInput) searchInput.value = '';
    if (typeFilter) typeFilter.value = '';
    if (sortBy) sortBy.value = 'newest';

    await loadMediaFiles();
}

/**
 * Close the media manager modal
 */
export function closeMediaManager() {
    const modal = $('mediaManagerModal');
    if (modal) {
        modal.classList.add('hide');
    }

    // Stop any playing media
    stopAllPlayback();
    expandedItemElement = null;
}

/**
 * Load media files from the API
 */
async function loadMediaFiles() {
    const list = $('mediaList');
    const countEl = $('mediaCount');

    if (!list) return;

    // Show loading state
    list.innerHTML = '<div class="media-loading">Loading media files...</div>';

    try {
        // Fetch all files (we'll do client-side pagination for filtering)
        const response = await fetch(`/api/media/list?page=1&per_page=1000`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();

        // Cache all files
        allMediaFiles = data.files || [];

        // Update stats
        if (countEl) {
            countEl.textContent = `${allMediaFiles.length} files`;
        }

        // Apply filters and render
        applyFilters();

    } catch (err) {
        console.error('[MediaManager] Error loading files:', err);
        list.innerHTML = `<div class="media-error">Failed to load media: ${escapeHtml(err.message)}</div>`;
    }
}

/**
 * Apply search, type filter, and sorting to the file list
 */
function applyFilters() {
    const searchTerm = ($('mediaSearch')?.value || '').toLowerCase().trim();
    const typeFilter = $('mediaTypeFilter')?.value || '';
    const sortBy = $('mediaSortBy')?.value || 'newest';

    // Filter
    filteredFiles = allMediaFiles.filter(file => {
        const matchesSearch = !searchTerm || file.filename.toLowerCase().includes(searchTerm);
        const matchesType = !typeFilter || file.type === typeFilter;
        return matchesSearch && matchesType;
    });

    // Sort
    filteredFiles.sort((a, b) => {
        switch (sortBy) {
            case 'newest':
                return (b.modified || 0) - (a.modified || 0);
            case 'oldest':
                return (a.modified || 0) - (b.modified || 0);
            case 'name':
                return a.filename.localeCompare(b.filename);
            case 'size':
                return (b.size || 0) - (a.size || 0);
            default:
                return 0;
        }
    });

    // Update count
    const countEl = $('mediaCount');
    if (countEl) {
        const filterText = (searchTerm || typeFilter) ? ' (filtered)' : '';
        countEl.textContent = `${filteredFiles.length} files${filterText}`;
    }

    // Reset to page 1 when filters change
    currentPage = 1;
    renderList();
}

/**
 * Render the media list with pagination
 */
function renderList() {
    const list = $('mediaList');
    const pageInfo = $('pageInfo');
    const prevBtn = $('btnPrevPage');
    const nextBtn = $('btnNextPage');

    if (!list) return;

    // Close any expanded item
    expandedItemElement = null;

    // Calculate pagination
    totalPages = Math.max(1, Math.ceil(filteredFiles.length / perPage));
    const startIdx = (currentPage - 1) * perPage;
    const endIdx = Math.min(startIdx + perPage, filteredFiles.length);
    const pageFiles = filteredFiles.slice(startIdx, endIdx);

    // Update pagination controls
    if (pageInfo) {
        pageInfo.textContent = `Page ${currentPage} of ${totalPages}`;
    }
    if (prevBtn) {
        prevBtn.disabled = currentPage <= 1;
    }
    if (nextBtn) {
        nextBtn.disabled = currentPage >= totalPages;
    }

    // Render list
    if (pageFiles.length === 0) {
        list.innerHTML = '<div class="media-empty">No media files found</div>';
        return;
    }

    list.innerHTML = '';
    pageFiles.forEach((file, index) => {
        const item = createMediaListItem(file, startIdx + index);
        list.appendChild(item);
    });
}

/**
 * Create a media list item element
 * @param {Object} file - File info from API
 * @param {number} index - Index in filtered array
 * @returns {HTMLElement} List item element
 */
function createMediaListItem(file, index) {
    const item = document.createElement('div');
    item.className = 'media-list-item';
    item.dataset.index = index;

    const icon = getFileIcon(file.type);
    const size = formatFileSize(file.size);
    const date = formatDate(file.modified);
    const typeClass = `type-${escapeAttr(file.type)}`;

    item.innerHTML = `
        <div class="media-list-header">
            <span class="media-list-icon">${icon}</span>
            <div class="media-list-info">
                <div class="media-list-name" title="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</div>
                <div class="media-list-meta">${size} • ${date}</div>
            </div>
            <span class="media-list-type ${typeClass}">${escapeHtml(file.type)}</span>
            <span class="media-list-expand">▼</span>
        </div>
        <div class="media-list-content">
            <!-- Populated on expand -->
        </div>
    `;

    // Click header to toggle expand
    const header = item.querySelector('.media-list-header');
    header.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleExpand(file, item);
    });

    return item;
}

/**
 * Toggle expansion of a media item (accordion behavior)
 * @param {Object} file - File info
 * @param {HTMLElement} item - List item element
 */
function toggleExpand(file, item) {
    const wasExpanded = item.classList.contains('expanded');

    // Close any currently expanded item
    if (expandedItemElement && expandedItemElement !== item) {
        expandedItemElement.classList.remove('expanded');
        stopPlayback(expandedItemElement);
    }

    // If clicking the same item, just close it
    if (wasExpanded) {
        item.classList.remove('expanded');
        stopPlayback(item);
        expandedItemElement = null;
        return;
    }

    // Expand clicked item
    item.classList.add('expanded');
    expandedItemElement = item;

    const content = item.querySelector('.media-list-content');
    renderMediaContent(file, content);

    // Scroll item into view if needed
    setTimeout(() => {
        item.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 100);
}

/**
 * Render media content inside expanded item
 * @param {Object} file - File info
 * @param {HTMLElement} container - Content container
 */
function renderMediaContent(file, container) {
    container.innerHTML = '';

    if (file.type === 'image') {
        container.innerHTML = `
            <img src="${escapeAttr(file.url)}" alt="${escapeAttr(file.filename)}" loading="lazy" />
        `;
    } else if (file.type === 'video') {
        container.innerHTML = `
            <video controls autoplay src="${escapeAttr(file.url)}"></video>
        `;
    } else if (file.type === 'audio') {
        container.innerHTML = `
            <audio controls autoplay src="${escapeAttr(file.url)}"></audio>
        `;
    } else {
        container.innerHTML = `
            <div class="media-document-preview">
                <span class="file-icon">📄</span>
                <span>${escapeHtml(file.filename)}</span>
            </div>
        `;
    }

    // Download button wired with a closure: the old inline onclick put the
    // filename inside a JS string, where HTML escaping is the wrong context and
    // a name like x');alert(1)// ran script (P04-media-tasks-fx-web-13).
    const downloadBtn = document.createElement('button');
    downloadBtn.className = 'media-download-btn';
    downloadBtn.textContent = '⬇️ Download';
    downloadBtn.addEventListener('click', () => downloadFile(file.url, file.filename));
    container.appendChild(downloadBtn);
}

/**
 * Stop playback of media in an item
 * @param {HTMLElement} item - List item element
 */
function stopPlayback(item) {
    if (!item) return;

    const content = item.querySelector('.media-list-content');
    if (!content) return;

    const video = content.querySelector('video');
    const audio = content.querySelector('audio');
    if (video) {
        video.pause();
        video.currentTime = 0;
    }
    if (audio) {
        audio.pause();
        audio.currentTime = 0;
    }
}

/**
 * Stop all media playback
 */
function stopAllPlayback() {
    const list = $('mediaList');
    if (!list) return;

    const videos = list.querySelectorAll('video');
    const audios = list.querySelectorAll('audio');
    videos.forEach(v => { v.pause(); v.currentTime = 0; });
    audios.forEach(a => { a.pause(); a.currentTime = 0; });
}

/**
 * Download a file
 * @param {string} url - File URL
 * @param {string} filename - Filename for download
 */
function downloadFile(url, filename) {
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    toast(`Downloading ${filename}`);
}

// =============================================================================
// Utility Functions
// =============================================================================

/**
 * Get emoji icon for file type
 * @param {string} type - File type
 * @returns {string} Emoji icon
 */
function getFileIcon(type) {
    switch (type) {
        case 'image': return '🖼️';
        case 'video': return '🎬';
        case 'audio': return '🎵';
        default: return '📄';
    }
}

/**
 * Format file size in human readable format
 * @param {number} bytes - File size in bytes
 * @returns {string} Formatted size
 */
function formatFileSize(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
}

/**
 * Format timestamp as readable date
 * @param {number} timestamp - Unix timestamp in seconds
 * @returns {string} Formatted date
 */
function formatDate(timestamp) {
    if (!timestamp) return 'Unknown date';

    const date = new Date(timestamp * 1000);
    const now = new Date();
    const diffDays = Math.floor((now - date) / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
        return 'Today';
    } else if (diffDays === 1) {
        return 'Yesterday';
    } else if (diffDays < 7) {
        return `${diffDays} days ago`;
    } else {
        return date.toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric',
            year: date.getFullYear() !== now.getFullYear() ? 'numeric' : undefined
        });
    }
}

// =============================================================================
// Pagination
// =============================================================================

/**
 * Go to previous page
 */
function prevPage() {
    if (currentPage > 1) {
        currentPage--;
        renderList();
        // Scroll to top of list
        $('mediaList')?.scrollTo({ top: 0, behavior: 'smooth' });
    }
}

/**
 * Go to next page
 */
function nextPage() {
    if (currentPage < totalPages) {
        currentPage++;
        renderList();
        // Scroll to top of list
        $('mediaList')?.scrollTo({ top: 0, behavior: 'smooth' });
    }
}

// =============================================================================
// Initialization
// =============================================================================

/**
 * Initialize media manager UI handlers
 */
export function initMediaManager() {
    // Open button
    const btnOpen = $('btnMediaManager');
    if (btnOpen) {
        btnOpen.addEventListener('click', openMediaManager);
    }

    // Close button
    const btnClose = $('btnCloseMediaManager');
    if (btnClose) {
        btnClose.addEventListener('click', closeMediaManager);
    }

    // Close on backdrop click
    const modal = $('mediaManagerModal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                closeMediaManager();
            }
        });
    }

    // Filter controls
    const searchInput = $('mediaSearch');
    const typeFilter = $('mediaTypeFilter');
    const sortBy = $('mediaSortBy');

    if (searchInput) {
        // Debounce search input
        let searchTimeout;
        searchInput.addEventListener('input', () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(applyFilters, 200);
        });
    }

    if (typeFilter) {
        typeFilter.addEventListener('change', applyFilters);
    }

    if (sortBy) {
        sortBy.addEventListener('change', applyFilters);
    }

    // Pagination
    const btnPrev = $('btnPrevPage');
    const btnNext = $('btnNextPage');
    if (btnPrev) {
        btnPrev.addEventListener('click', prevPage);
    }
    if (btnNext) {
        btnNext.addEventListener('click', nextPage);
    }

    console.log('[MediaManager] Initialized with list view');
}
