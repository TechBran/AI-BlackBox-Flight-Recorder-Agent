/**
 * ui-setup.js
 * UI component initialization - scroll, lightbox, keyboard shortcuts, health checks
 *
 * Source: Portal/app.js lines 4285-4900
 * Refactored: 2025-12-20
 */

import { $, toast, toastError } from './core-utils.js';
import { getOperator } from './state-management.js';
import { loadTimelineData, showTimelineDetail } from './timeline-browser.js';

// =============================================================================
// State
// =============================================================================

/** Current lightbox zoom level */
let currentLightboxZoom = 1;

/** Timeline data reference for snapshot click handling */
let timelineData = [];

// =============================================================================
// Health Check
// =============================================================================

/**
 * Refresh health status from backend
 */
export async function refreshHealth() {
    const dot = $("healthDot");
    const label = $("healthLabel");
    try {
        const r = await fetch("/health");
        const j = await r.json();
        const pre = $("manifest");
        if (pre) pre.textContent = JSON.stringify(j.latest_manifest || {}, null, 2);

        const manifest = j.latest_manifest || {};

        // Health indicator — connected
        if (dot) { dot.className = "health-dot connected"; }
        if (label) { label.textContent = "Connected"; label.style.color = "#34c759"; }

        // Snapshot count
        const snapshotCount = j.snapshot_count || 0;
        const snapshotsEl = $("statusSnapshots");
        if (snapshotsEl) snapshotsEl.textContent = snapshotCount;

        // Token usage and Checkpoint - per operator
        const currentOperator = getOperator();
        const operatorTurns = j.operator_turns || {};

        // Checkpoint countdown
        const checkpointEl = $("statusCheckpoint");
        if (checkpointEl) {
            const opData = operatorTurns[currentOperator];
            if (opData) {
                const turnsUntil = opData.turns_until_checkpoint;
                if (turnsUntil === 0) {
                    checkpointEl.textContent = 'Now!';
                    checkpointEl.style.color = '#4a9eff';
                    checkpointEl.style.fontWeight = 'bold';
                } else {
                    checkpointEl.textContent = `${turnsUntil} turns`;
                    checkpointEl.style.color = '';
                    checkpointEl.style.fontWeight = '';
                }
            } else {
                checkpointEl.textContent = '--';
            }
        }

        // Token usage
        const tokensEl = $("statusTokens");
        if (tokensEl) {
            const opData = operatorTurns[currentOperator];
            const totalTokens = opData ? opData.total_tokens || 0 : 0;
            if (totalTokens >= 1000000) {
                tokensEl.textContent = `${(totalTokens / 1000000).toFixed(1)}M`;
            } else if (totalTokens >= 1000) {
                tokensEl.textContent = `${(totalTokens / 1000).toFixed(1)}K`;
            } else {
                tokensEl.textContent = totalTokens;
            }
        }

        // Tailscale paired-server display in System Controls section
        // Per docs/plans/2026-05-20-hamburger-polish-and-tailscale-copy.md Track 4
        const pairing = j.pairing || {};
        const tsValueEl = $("tailscaleAddressValue");
        const tsCopyBtn = $("btnCopyTailscale");
        const tsOrigin = pairing.default_origin || "";
        if (tsValueEl) {
            if (tsOrigin) {
                tsValueEl.textContent = tsOrigin;
                tsValueEl.classList.remove("tailscale-empty");
                if (tsCopyBtn) tsCopyBtn.disabled = false;
            } else {
                tsValueEl.textContent = "Not paired";
                tsValueEl.classList.add("tailscale-empty");
                if (tsCopyBtn) tsCopyBtn.disabled = true;
            }
        }
        if (tsCopyBtn && tsOrigin && !tsCopyBtn.dataset.wired) {
            tsCopyBtn.dataset.wired = "1";  // avoid double-wiring on re-init
            tsCopyBtn.addEventListener("click", async () => {
                try {
                    await navigator.clipboard.writeText(tsOrigin);
                    toast(`Copied ${tsOrigin}`);
                } catch (err) {
                    console.error("[Tailscale] copy failed:", err);
                    // Visual fallback if clipboard API rejects
                    tsCopyBtn.textContent = "✓";
                    setTimeout(() => { tsCopyBtn.textContent = "📋"; }, 1500);
                }
            });
        }

    } catch (e) {
        // Health indicator — lost
        if (dot) { dot.className = "health-dot lost"; }
        if (label) { label.textContent = "Lost"; label.style.color = "#ff3b30"; }
        console.warn("Health refresh failed", e);
    }
}

// =============================================================================
// Scroll Button
// =============================================================================

/**
 * Setup scroll-to-bottom button
 */
export function setupScrollButton() {
    const scrollBtn = $("scrollToBottom");
    const historyEl = $("history");
    if (!scrollBtn || !historyEl) return;

    // Monitor scroll position - account for 50vh bottom padding
    historyEl.addEventListener('scroll', () => {
        const paddingAllowance = window.innerHeight * 0.5 + 150;
        const nearBottom = historyEl.scrollHeight - historyEl.scrollTop - historyEl.clientHeight < (200 + paddingAllowance);
        if (nearBottom) {
            scrollBtn.classList.add('hide');
        } else {
            scrollBtn.classList.remove('hide');
        }
    });

    // Click handler - scroll to last bubble, not absolute bottom
    scrollBtn.addEventListener('click', () => {
        const lastBubble = historyEl.lastElementChild;
        if (lastBubble) {
            lastBubble.scrollIntoView({ block: 'end', behavior: 'smooth' });
        }
        scrollBtn.classList.add('hide');
    });
}

// =============================================================================
// Dynamic History Padding
// =============================================================================

/**
 * Update history padding based on status bar position
 */
function updateHistoryPadding() {
    const historyEl = $("history");
    const statusBar = document.querySelector(".status-bar");
    if (!historyEl || !statusBar) return;

    const viewportHeight = window.innerHeight;
    const statusBarRect = statusBar.getBoundingClientRect();
    const statusBarFromBottom = viewportHeight - statusBarRect.top;

    // Add buffer for TTS buttons
    const paddingNeeded = statusBarFromBottom + 100;

    // Apply dynamic padding (minimum 200px)
    historyEl.style.paddingBottom = Math.max(paddingNeeded, 200) + 'px';
}

/**
 * Setup dynamic padding on load and resize
 */
export function setupDynamicHistoryPadding() {
    updateHistoryPadding();

    window.addEventListener('resize', updateHistoryPadding);

    window.addEventListener('orientationchange', () => {
        setTimeout(updateHistoryPadding, 100);
    });
}

// =============================================================================
// Image Lightbox
// =============================================================================

/**
 * Setup image lightbox viewer
 */
export function setupImageLightbox() {
    const lightboxModal = $("lightboxModal");
    const lightboxImage = $("lightboxImage");
    const btnCloseLightbox = $("btnCloseLightbox");
    const btnZoomIn = $("btnZoomIn");
    const btnZoomOut = $("btnZoomOut");
    const btnResetZoom = $("btnResetZoom");

    if (!lightboxModal || !lightboxImage) return;

    // Function to open lightbox
    window.openLightbox = function(imgSrc) {
        if (lightboxImage) lightboxImage.src = imgSrc;
        currentLightboxZoom = 1;
        if (lightboxImage) lightboxImage.style.transform = `scale(${currentLightboxZoom})`;
        if (lightboxModal) lightboxModal.classList.remove('hide');
    };

    // Close lightbox
    if (btnCloseLightbox) {
        btnCloseLightbox.addEventListener('click', () => {
            if (lightboxModal) lightboxModal.classList.add('hide');
        });
    }

    // Click background to close
    if (lightboxModal) {
        lightboxModal.addEventListener('click', (e) => {
            if (e.target === lightboxModal) {
                lightboxModal.classList.add('hide');
            }
        });
    }

    // Zoom controls
    if (btnZoomIn) {
        btnZoomIn.addEventListener('click', () => {
            currentLightboxZoom = Math.min(currentLightboxZoom + 0.25, 3);
            if (lightboxImage) lightboxImage.style.transform = `scale(${currentLightboxZoom})`;
        });
    }

    if (btnZoomOut) {
        btnZoomOut.addEventListener('click', () => {
            currentLightboxZoom = Math.max(currentLightboxZoom - 0.25, 0.5);
            if (lightboxImage) lightboxImage.style.transform = `scale(${currentLightboxZoom})`;
        });
    }

    if (btnResetZoom) {
        btnResetZoom.addEventListener('click', () => {
            currentLightboxZoom = 1;
            if (lightboxImage) lightboxImage.style.transform = `scale(${currentLightboxZoom})`;
        });
    }

    // Add click handlers to all images in bubbles and snapshot IDs
    document.addEventListener('click', (e) => {
        if (e.target.tagName === 'IMG' && e.target.closest('.bubble')) {
            const imgSrc = e.target.src;
            if (imgSrc && window.openLightbox) {
                window.openLightbox(imgSrc);
            }
        }

        // Handle snapshot ID clicks
        const snapshotElement = e.target.closest('.clickable-snapshot');
        if (snapshotElement) {
            const snapId = snapshotElement.getAttribute('data-snap-id');
            console.log('[CLICK-HANDLER] Snapshot clicked, snapId:', snapId);
            if (snapId) {
                e.preventDefault();
                e.stopPropagation();
                handleSnapshotClick(snapId);
            } else {
                console.warn('[CLICK-HANDLER] No snapId found on element');
            }
        }
    });

    // Function to handle snapshot ID clicks
    async function handleSnapshotClick(snapId) {
        console.log('[SNAPSHOT-CLICK] Clicked on snapshot:', snapId);

        snapId = snapId.toUpperCase();
        toast('Loading snapshot ' + snapId + '...');

        try {
            // Load timeline data if needed
            const data = await loadTimelineData();
            timelineData = data;

            // Find the snapshot
            let snapshot = timelineData.find(s => s.snapId === snapId);

            // Try partial match for short format
            if (!snapshot && snapId.match(/^SNAP-\d+$/)) {
                const shortNum = snapId.replace('SNAP-', '');
                console.log('[SNAPSHOT-CLICK] Short format detected, searching for:', shortNum);

                const matches = timelineData.filter(s => s.snapId.endsWith('-' + shortNum));

                if (matches.length === 1) {
                    snapshot = matches[0];
                    console.log('[SNAPSHOT-CLICK] Found unique match:', snapshot.snapId);
                } else if (matches.length > 1) {
                    console.warn('[SNAPSHOT-CLICK] Multiple matches found:', matches.map(s => s.snapId));
                    snapshot = matches[0];
                    toast('Multiple matches found, showing most recent: ' + snapshot.snapId);
                }
            }

            if (snapshot) {
                console.log('[SNAPSHOT-CLICK] Snapshot found, displaying...');
                showTimelineDetail(snapshot);
            } else {
                console.error('[SNAPSHOT-CLICK] Snapshot not found:', snapId);
                toastError('Snapshot ' + snapId + ' not found in volume');
            }
        } catch (error) {
            console.error('[SNAPSHOT-CLICK] Error loading snapshot:', error);
            toastError('Failed to load snapshot: ' + error.message);
        }
    }
}

// =============================================================================
// Keyboard Shortcuts
// =============================================================================

/**
 * Setup global keyboard shortcuts
 */
export function setupKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
        // Ctrl+Enter or Cmd+Enter: Send message
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            const btnSend = $("btnSend");
            if (btnSend) btnSend.click();
            return;
        }

        // Ctrl+K or Cmd+K: Clear chat
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            const btnClear = $("ctlClear");
            if (btnClear) btnClear.click();
            return;
        }

        // Ctrl+M or Cmd+M: Mint snapshot
        if ((e.ctrlKey || e.metaKey) && e.key === 'm') {
            e.preventDefault();
            const btnMint = $("ctlMint");
            if (btnMint) btnMint.click();
            return;
        }

        // Escape: Close all modals
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal:not(.hide)').forEach(modal => {
                modal.classList.add('hide');
            });
            return;
        }

        // /: Focus prompt (if not already in an input)
        if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(e.target.tagName)) {
            e.preventDefault();
            const promptEl = $("prompt");
            if (promptEl) promptEl.focus();
            return;
        }
    });
}

// =============================================================================
// Claude Banner Position
// =============================================================================

/**
 * Adjust Claude Code banner position to avoid overlap with header
 */
export function adjustClaudeBannerPosition() {
    const header = document.querySelector('.topbar');
    const claudeBanner = $('claudeCodeBanner');

    if (!header || !claudeBanner) return;

    function updatePosition() {
        const headerHeight = header.offsetHeight;
        const bannerTop = headerHeight + 10;
        claudeBanner.style.top = bannerTop + 'px';
        console.log(`[Layout] Header height: ${headerHeight}px, Banner top: ${bannerTop}px`);
    }

    updatePosition();

    let resizeTimer;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(updatePosition, 150);
    });
}

// =============================================================================
// Task Monitor
// =============================================================================

/** Interval ID for task polling */
let taskMonitorInterval = null;

/** Track previous active task count for notifications */
let previousActiveTaskCount = 0;

/**
 * Set up task monitor UI and start polling
 */
export function setupTaskMonitor() {
    const taskMonitor = $("taskMonitor");
    const taskIndicator = $("taskIndicator");
    const taskPanel = $("taskPanel");
    const taskPanelClose = $("taskPanelClose");

    if (!taskMonitor || !taskIndicator) return;

    // Toggle panel on indicator click
    if (taskIndicator) {
        taskIndicator.addEventListener('click', () => {
            if (taskPanel) {
                taskPanel.classList.toggle('hide');
            }
        });
    }

    // Close panel button
    if (taskPanelClose) {
        taskPanelClose.addEventListener('click', () => {
            if (taskPanel) {
                taskPanel.classList.add('hide');
            }
        });
    }

    // Start polling for tasks
    startTaskPolling();
}

/**
 * Start polling for background tasks
 */
function startTaskPolling() {
    console.log('[DEBUG] Task polling started');

    // Poll every 2 seconds
    taskMonitorInterval = setInterval(async () => {
        try {
            const response = await fetch('/tasks/list');
            if (!response.ok) {
                console.log('[DEBUG] Task list fetch not OK:', response.status);
                return;
            }

            const data = await response.json();
            const tasks = data.tasks || data;
            console.log('[DEBUG] Fetched tasks:', tasks.length, 'total');
            updateTaskMonitor(tasks);
        } catch (error) {
            console.error('[TaskMonitor] Error fetching tasks:', error);
        }
    }, 2000);

    // Also run immediately on startup
    setTimeout(async () => {
        try {
            const response = await fetch('/tasks/list');
            if (!response.ok) return;

            const data = await response.json();
            const tasks = data.tasks || data;
            updateTaskMonitor(tasks);
        } catch (error) {
            console.error('[TaskMonitor] Error on initial fetch:', error);
        }
    }, 1000);
}

/**
 * Update task monitor UI with current tasks
 * @param {Array} tasks - Task list from server
 */
function updateTaskMonitor(tasks) {
    const taskMonitor = $("taskMonitor");
    const taskCount = $("taskCount");
    const taskPanelBody = $("taskPanelBody");

    if (!taskMonitor || !taskCount || !taskPanelBody) return;

    // Filter to only show active tasks (pending or processing)
    const activeTasks = tasks.filter(t =>
        t.status === 'pending' || t.status === 'processing'
    );

    const currentCount = activeTasks.length;

    // Notify when active task count changes (tasks complete)
    if (previousActiveTaskCount > 0 && currentCount < previousActiveTaskCount) {
        const completedCount = previousActiveTaskCount - currentCount;
        // Import showNotification dynamically to avoid circular dependency
        import('./notifications.js').then(({ showNotification }) => {
            showNotification("🎯 Tasks Completed", {
                body: `${completedCount} task${completedCount > 1 ? 's' : ''} finished! ${currentCount} remaining.`,
                tag: "task-progress",
                operator: window.__operator
            });
        });
    }

    previousActiveTaskCount = currentCount;

    // Update task count
    taskCount.textContent = activeTasks.length;

    // Show/hide monitor based on active tasks
    if (activeTasks.length > 0) {
        taskMonitor.classList.remove('hide');
        // Render active tasks in panel
        taskPanelBody.innerHTML = activeTasks.map(task => renderTaskItem(task)).join('');
    } else {
        // Clear panel body to remove any stale tasks
        taskPanelBody.innerHTML = '';
        // Hide monitor and close panel
        taskMonitor.classList.add('hide');
        const taskPanel = $("taskPanel");
        if (taskPanel) {
            taskPanel.classList.add('hide');
        }
    }
}

/**
 * Render a single task item for the task panel
 * @param {Object} task - Task object
 * @returns {string} HTML string
 */
function renderTaskItem(task) {
    const typeIcons = {
        'image': '🎨',
        'video': '🎬',
        'audio': '🎵',
        'chat': '💬',
        'checkpoint': '💾',
        'tts': '🔊',
        'ssml': '🎙️'
    };

    const typeNames = {
        'image': 'Image Generation',
        'video': 'Video Generation',
        'audio': 'Audio Analysis',
        'chat': 'Chat Response',
        'checkpoint': 'Checkpoint',
        'tts': 'Text-to-Speech',
        'ssml': 'SSML Generation'
    };

    const icon = typeIcons[task.task_type] || '⚙️';
    const typeName = typeNames[task.task_type] || task.task_type;
    const progress = task.progress || 0;

    // Calculate elapsed time
    const createdAt = new Date(task.created_at);
    const now = new Date();
    const elapsed = Math.floor((now - createdAt) / 1000);
    const timeStr = elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`;

    // Truncate prompt for display
    const description = task.prompt
        ? task.prompt.substring(0, 60) + (task.prompt.length > 60 ? '...' : '')
        : 'Processing...';

    return `
        <div class="task-item" data-task-id="${task.task_id}">
            <div class="task-item-header">
                <div class="task-type">
                    <span class="task-type-icon">${icon}</span>
                    <span>${typeName}</span>
                </div>
                <span class="task-status ${task.status}">${task.status}</span>
            </div>
            <div class="task-description">${description}</div>
            ${progress > 0 ? `
                <div class="task-progress">
                    <div class="task-progress-bar" style="width: ${progress}%"></div>
                </div>
            ` : ''}
            <div class="task-time">Started ${timeStr} ago</div>
        </div>
    `;
}

// Stop polling when page unloads
if (typeof window !== 'undefined') {
    window.addEventListener('beforeunload', () => {
        if (taskMonitorInterval) {
            clearInterval(taskMonitorInterval);
        }
    });
}

// =============================================================================
// Utility Functions
// =============================================================================

// Scroll state for smooth streaming scroll
let _scrollRAF = null;
let _lastScrollTime = 0;
let _isStreaming = false;

/**
 * Set streaming state - enables smoother auto-scroll during streaming
 * @param {boolean} streaming - Whether content is currently streaming
 */
export function setStreamingState(streaming) {
    const wasStreaming = _isStreaming;
    _isStreaming = streaming;

    // When streaming ends, snap to bottom
    if (wasStreaming && !streaming) {
        snapToBottom();
    }
}

/**
 * Scroll history to last bubble with smooth animation
 */
export function scrollToBottom() {
    const historyEl = $("history");
    if (historyEl) {
        const lastBubble = historyEl.lastElementChild;
        if (lastBubble) {
            lastBubble.scrollIntoView({ block: 'end', behavior: 'smooth' });
        }
    }
}

/**
 * Check if user is near the bottom of history (viewing recent content)
 * Accounts for large bottom padding (50vh) by using viewport-relative threshold
 * @param {number} threshold - Additional pixels beyond viewport padding (default 200)
 * @returns {boolean}
 */
export function isNearBottom(threshold = 200) {
    const historyEl = $("history");
    if (!historyEl) return true;
    // Account for 50vh bottom padding plus threshold
    const paddingAllowance = window.innerHeight * 0.5 + 150;
    return historyEl.scrollHeight - historyEl.scrollTop - historyEl.clientHeight < (threshold + paddingAllowance);
}

/**
 * Smooth scroll to bottom during streaming
 * During streaming: Gentle scroll at reduced frequency for smooth experience
 * Not streaming: Only scroll if near bottom (don't interrupt manual scrolling)
 * @param {number} threshold - Pixels from bottom to consider "near" (default 300)
 */
// Track if we're in the middle of a page scroll animation
let _pageScrolling = false;

export function scrollToBottomIfNeeded(threshold = 300) {
    const historyEl = $("history");
    if (!historyEl) return;

    // During streaming, use "page-fill" approach:
    // Let text fill the visible area, then smooth scroll when it overflows
    if (_isStreaming) {
        // Don't interrupt an ongoing page scroll
        if (_pageScrolling) return;

        // Cancel any pending frame
        if (_scrollRAF) {
            cancelAnimationFrame(_scrollRAF);
        }

        // Schedule check on next frame
        _scrollRAF = requestAnimationFrame(() => {
            // Find streaming bubble - works for both regular chat and agents
            const streamingBubble = historyEl.querySelector('.streaming-bubble') ||
                                    historyEl.querySelector('.agent-streaming');
            if (!streamingBubble) return;

            // Get the content element (different for chat vs agents)
            const contentEl = streamingBubble.querySelector('.response-content') ||
                              streamingBubble.querySelector('.agent-terminal') ||
                              streamingBubble.querySelector('.bubble-text');
            if (!contentEl) return;

            // Get positions relative to viewport
            const contentRect = contentEl.getBoundingClientRect();

            // Visible area: below floating panel (~140px from top), above composer (~160px from bottom)
            const visibleTop = 140;
            const visibleBottom = window.innerHeight - 160;

            // If content bottom is below visible area, do a page scroll
            if (contentRect.bottom > visibleBottom) {
                _pageScrolling = true;

                // Calculate scroll to bring current content bottom to near the top
                // Leave some context visible (20% of visible height)
                const visibleHeight = visibleBottom - visibleTop;
                const contextKeep = visibleHeight * 0.2;
                const scrollAmount = contentRect.bottom - visibleTop - contextKeep;

                historyEl.scrollTo({
                    top: historyEl.scrollTop + scrollAmount,
                    behavior: 'smooth'
                });

                // Allow next scroll after animation completes (~400ms for smooth scroll)
                setTimeout(() => {
                    _pageScrolling = false;
                }, 400);
            }
        });
    } else {
        // Not streaming - only scroll if user is near bottom
        if (isNearBottom(threshold)) {
            const lastBubble = historyEl.lastElementChild;
            if (lastBubble) {
                lastBubble.scrollIntoView({ block: 'end', behavior: 'instant' });
            }
        }
    }
}

/**
 * Snap to last bubble - use when streaming completes
 */
export function snapToBottom() {
    const historyEl = $("history");
    if (historyEl) {
        const lastBubble = historyEl.lastElementChild;
        if (lastBubble) {
            // Scroll last bubble to bottom of visible area
            lastBubble.scrollIntoView({ block: 'end', behavior: 'smooth' });
        }
    }
}

/**
 * Check if we're on mobile or remote (Tailscale)
 * @returns {boolean}
 */
export function isMobileOrRemote() {
    const hostname = window.location.hostname;
    return hostname !== 'localhost' && hostname !== '127.0.0.1';
}

// =============================================================================
// Modal and Button Handlers
// =============================================================================

/**
 * Safely attach click handler to element by ID
 * @param {string} id - Element ID
 * @param {Function} handler - Click handler
 */
function safeSetOnClick(id, handler) {
    const element = $(id);
    if (element) {
        element.onclick = handler;
    } else {
        console.warn(`UI element not found, cannot attach click handler: #${id}`);
    }
}

/**
 * Initialize all modal and button handlers
 */
export function initModalHandlers() {
    // Import required functions dynamically to avoid circular deps
    import('./state-management.js').then(({
        getOperator, setOperator, clearAudioCache, addCustomOperator,
        getHistoryData, setHistoryData, saveHistory
    }) => {
        import('./help-hints.js').then(({ updateHintsVisibility }) => {
            import('./tts-stt.js').then(({ startSTT, stopSTT, isMicOn, isTranscribing }) => {
                setupAllHandlers(getOperator, setOperator, clearAudioCache, addCustomOperator,
                    getHistoryData, setHistoryData, saveHistory, updateHintsVisibility,
                    startSTT, stopSTT, isMicOn, isTranscribing);
            });
        });
    });
}

/**
 * Set up all modal and button handlers
 */
function setupAllHandlers(getOperator, setOperator, clearAudioCache, addCustomOperator,
    getHistoryData, setHistoryData, saveHistory, updateHintsVisibility,
    startSTT, stopSTT, isMicOn, isTranscribing) {

    // Modal references
    const menuModal = $("menuModal");
    const manifestModal = $("manifestModal");
    const pairModal = $("pairModal");
    const confirmClearModal = $("confirmClearModal");
    const addOperatorModal = $("addOperatorModal");

    // Menu modal
    safeSetOnClick("btnMenu", () => {
        if (!menuModal) return;
        menuModal.classList.remove("hide");
        // T8 — Updates panel fetches /update/status on every menu open.
        // No cache busting; the backend's own 60s cache (audit M7) keeps
        // GitHub from being hammered when the user toggles the menu rapidly.
        import("./updates-manager.js")
            .then((mod) => mod.initUpdatesPanel())
            .catch((e) => console.warn("[updates] init failed:", e));
    });
    safeSetOnClick("btnCloseMenu", () => menuModal && menuModal.classList.add("hide"));

    // Collapsible section helper — generalizes the existing .advanced-section
    // toggle pattern to Generation/Tools/Apps/Reasoning sections. Per
    // docs/plans/2026-05-20-hamburger-polish-and-tailscale-copy.md Track 3.
    // In-memory state only (no localStorage); refresh resets to default.
    function wireCollapsibleSection(toggleBtnId, contentDivId, initiallyExpanded = false) {
        const btn = $(toggleBtnId);
        const content = $(contentDivId);
        if (!btn || !content) return;
        if (initiallyExpanded) {
            content.classList.remove("collapsed");
            btn.classList.add("active");
        } else {
            content.classList.add("collapsed");
            btn.classList.remove("active");
        }
        btn.addEventListener("click", () => {
            const isCollapsed = content.classList.toggle("collapsed");
            btn.classList.toggle("active", !isCollapsed);
        });
    }

    // Existing Advanced Settings Accordion (preserved behavior — markup uses
    // .advanced-content not .section-content, but the collapsed class is the
    // same so the helper works identically).
    wireCollapsibleSection("btnToggleAdvanced", "advancedSettings");

    // 4 new collapsibles introduced in Track 3 — all default to collapsed.
    wireCollapsibleSection("btnToggleGeneration", "generationContent");
    wireCollapsibleSection("btnToggleTools", "toolsContent");
    wireCollapsibleSection("btnToggleApps", "appsContent");
    wireCollapsibleSection("btnToggleReasoning", "reasoningContent");
    wireCollapsibleSection("btnToggleSystemPrompt", "systemPromptContent");

    // Manifest modal
    safeSetOnClick("btnManifest", async () => {
        await refreshHealth();
        manifestModal && manifestModal.classList.remove("hide");
    });
    safeSetOnClick("btnCloseManifest", () => manifestModal && manifestModal.classList.add("hide"));

    // Pairing modal
    safeSetOnClick("ctlPair", async () => {
        try {
            const r = await fetch("/pair/start", { method: "POST" });
            const j = await r.json();
            const pairing = (await (await fetch("/health")).json()).pairing || {};
            const origin = pairing.default_origin || (window.location.origin + "/ui");
            const operator = pairing.default_operator || getOperator() || "";
            // Display payload for reference; QR is rendered server-side from the same token
            // so phone scans encode whatever URL the Portal was loaded from (Tailscale, LAN, localhost).
            const payload = { type: "pair", exp: j.exp, origin, operator, token: j.token };
            if ($("pairQR")) $("pairQR").src = "/pair/qr/" + encodeURIComponent(j.token);
            if ($("pairJSON")) $("pairJSON").textContent = JSON.stringify(payload, null, 2);
            if (pairModal) pairModal.classList.remove("hide");
        } catch (e) {
            import('./core-utils.js').then(({ toast }) => toast("Pairing error: " + e));
        }
    });
    safeSetOnClick("btnClosePair", () => pairModal && pairModal.classList.add("hide"));

    // Manage Setup → re-enter onboarding wizard in manage mode
    safeSetOnClick("btnManageSetup", () => {
        // Wizard is the single source of truth for credential management.
        // ?mode=manage suppresses the auto-redirect-to-/ui in onboarding.js:198.
        location.href = "/onboarding/?mode=manage";
    });

    // Clear history modal
    safeSetOnClick("ctlClear", () => confirmClearModal && confirmClearModal.classList.remove("hide"));
    safeSetOnClick("btnCancelClear", () => confirmClearModal && confirmClearModal.classList.add("hide"));
    safeSetOnClick("btnConfirmClear", () => {
        console.log('[HINTS] Clear history clicked');
        setHistoryData([]);
        saveHistory();
        clearAudioCache();
        const hist = $("history");
        if (hist) {
            const bubbles = hist.querySelectorAll('.bubble');
            console.log('[HINTS] Removing', bubbles.length, 'bubbles');
            bubbles.forEach(bubble => bubble.remove());
        }
        if (confirmClearModal) confirmClearModal.classList.add("hide");
        import('./core-utils.js').then(({ toast }) => toast("History cleared"));
        console.log('[HINTS] Calling updateHintsVisibility after clear');
        updateHintsVisibility();
    });

    // Add Operator modal
    safeSetOnClick("btnCloseAddOperator", () => {
        if (addOperatorModal) addOperatorModal.classList.add("hide");
        const input = $("newOperatorName");
        if (input) input.value = "";
    });
    safeSetOnClick("btnCancelAddOperator", () => {
        if (addOperatorModal) addOperatorModal.classList.add("hide");
        const input = $("newOperatorName");
        if (input) input.value = "";
    });
    safeSetOnClick("btnConfirmAddOperator", async () => {
        const input = $("newOperatorName");
        if (!input) return;
        const name = input.value.trim();
        if (!name) {
            import('./core-utils.js').then(({ toast }) => toast("Please enter an operator name"));
            return;
        }
        const result = await addCustomOperator(name);
        if (result) {
            import('./core-utils.js').then(({ toast }) => toast(`Operator "${name}" added`));
            setOperator(name);
            import('./state-management.js').then(({ initOperatorSelector }) => initOperatorSelector());
            if (addOperatorModal) addOperatorModal.classList.add("hide");
            input.value = "";
        } else {
            import('./core-utils.js').then(({ toast }) => toast(`Operator "${name}" already exists or failed to add`));
        }
    });

    // Enter key support for add operator input
    const newOperatorInput = $("newOperatorName");
    if (newOperatorInput) {
        newOperatorInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                const btn = $("btnConfirmAddOperator");
                if (btn) btn.click();
            }
        });
    }

    // Mic/STT button
    safeSetOnClick("ctlMic", () => {
        if (isTranscribing()) return; // Don't interrupt while Whisper is processing
        if (!isMicOn()) {
            startSTT();
        } else {
            stopSTT();
        }
    });

    // Cancel stuck tasks
    safeSetOnClick("btnCancelTasks", async () => {
        try {
            const r = await fetch("/tasks/cancel-all", { method: "POST" });
            const j = await r.json();
            toast(`Cancelled ${j.cancelled} stuck task(s)`);
        } catch (e) {
            toastError("Failed to cancel tasks: " + e);
        }
    });

    console.log('[UI] Modal handlers initialized');
}
