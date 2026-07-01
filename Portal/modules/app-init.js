/**
 * app-init.js
 * Application orchestrator - initializes all modules in correct order
 * Entry point for the modular Portal application
 *
 * Source: Portal/app.js DOMContentLoaded handlers
 * Refactored: 2025-12-20
 */

// =============================================================================
// Module Imports
// =============================================================================

// Phase 1: Foundation
import { $, toast, sleep } from './core-utils.js';
import {
    loadHistory,
    saveHistory,
    getHistoryData,
    setHistoryData,
    getOperator,
    setOperator,
    initOperatorSelector,
    fetchAvailableModels,
    updateModelDropdown
} from './state-management.js';
import { initNotificationSystem, showNotification } from './notifications.js';
import { SlashCommandDropdown } from './slash-commands.js';

// Phase 2: UI Components
import { configureMarked, renderMarkdown } from './markdown-renderer.js';
import {
    setupTimelineBrowser,
    makeSnapshotsClickable,
    applySyntaxHighlighting
} from './timeline-browser.js';
import {
    setupScrollButton,
    setupDynamicHistoryPadding,
    setupImageLightbox,
    setupKeyboardShortcuts,
    setupTaskMonitor,
    adjustClaudeBannerPosition,
    refreshHealth,
    initModalHandlers,
    scrollToBottomIfNeeded
} from './ui-setup.js';
import { initHelpHints, updateHintsVisibility } from './help-hints.js';
import { initGeminiAudioRecorder } from './gemini-recorder.js';

// Phase 3: Features
import {
    initVoiceSelector,
    syncVoiceDropdown,
    initSystemPromptSection,
    initAutoTTS,
    setupNativeSTTCallbacks,
    attachGeneratedAudioPlayer
} from './tts-stt.js';
import { loadAudioCache } from './state-management.js';
import {
    renderPreviews,
    setupNativeFileCallback,
    addAttachedFile
} from './file-upload.js';
import { taskManager } from './task-manager.js';

// Phase 4: Chat Components
import {
    appendBubble,
    addBubble,
    createAnimatedThinkingBubble,
    startThinkingAnimation,
    renderHistory
} from './chat-bubbles.js';
import {
    initGenerationModals,
    setupAudioAnalysisModal
} from './generation-modals.js';
import { initMediaManager } from './media-manager.js';
import { initCronManager } from './cron-manager.js';
import { initDeviceManager } from './device-manager.js';
import { initDevicesSection } from './devices-section.js';
import { initTelephonyManager } from './telephony-manager.js';
import { initTelephonyWizard } from './telephony-wizard.js';
import { initCellularManager } from './cellular-manager.js';
import { initSMSInbox, openSMSInbox, pollUnread as pollSMSUnread } from './sms-inbox.js';
import { initContactsManager } from './contacts-manager.js';
import { initCLIAgentsModal } from './cli-agents-modal.js';
import { initVoiceAgentsModal } from './voice-agents-modal.js';
import { initGmailModal } from './gmail-modal.js';
import {
    initAgentHandler,
    checkExistingAgentSession
} from './agent-handler.js';
import {
    initGeminiAgentHandler,
    checkExistingGeminiAgentSession
} from './gemini-agent-handler.js';

// Phase 5: Chat Send
import { initChatSend, send, createBubbleWithThinking, checkCUBackgroundStatus } from './chat-send.js';
import { initCUDrawer } from './cu-drawer.js';

// Generation ember backdrop (fades embers in while the AI is generating)
import { initEmberFX, initEmberModeControl } from './ember-fx.js';

// Phase 6: GPT-4o Realtime
import { initRealtimeUI, connect as realtimeConnect, disconnect as realtimeDisconnect, checkRealtimeAvailable } from './gpt-realtime.js';

// Phase 7: Gemini Live
import { initGeminiLiveUI, connect as geminiLiveConnect, disconnect as geminiLiveDisconnect, checkGeminiLiveAvailable } from './gemini-live.js';

// Phase 8: Grok Live
import { initGrokLiveUI, connect as grokLiveConnect, disconnect as grokLiveDisconnect, checkGrokLiveAvailable } from './grok-live.js';

// =============================================================================
// Global State
// =============================================================================

/** Slash command dropdown instance */
let slashCommandDropdown = null;

// Note: taskManager is imported as singleton from task-manager.js

// =============================================================================
// Provider/Model Initialization
// =============================================================================

/**
 * Initialize provider and model selectors
 */
async function initProviderModelSelectors() {
    const providerEl = $("providerSelect");
    const modelEl = $("modelSelect");
    const savedProvider = localStorage.getItem("bb_provider") || "google";

    if (providerEl) {
        providerEl.value = savedProvider;
        window.__provider = providerEl.value;

        await fetchAvailableModels(window.__provider);
        updateModelDropdown(window.__provider);
        updateClaudeCodeBanner(window.__provider);

        providerEl.addEventListener("change", async () => {
            window.__provider = providerEl.value;
            localStorage.setItem("bb_provider", window.__provider);

            await fetchAvailableModels(window.__provider);
            updateModelDropdown(window.__provider);
            updateClaudeCodeBanner(window.__provider);
        });
    } else {
        window.__provider = savedProvider;
    }

    if (modelEl) {
        modelEl.addEventListener("change", () => {
            window.__model = modelEl.value || "";
            localStorage.setItem(`bb_model_${window.__provider}`, window.__model);
            console.log(`[ModelSelect] Saved model preference for ${window.__provider}: ${window.__model || "(auto)"}`);
        });
    } else {
        const savedModel = localStorage.getItem(`bb_model_${window.__provider}`);
        window.__model = savedModel || "";
    }
}

/**
 * Update Claude Code / Gemini CLI banner visibility based on provider.
 * Track 4 (2026-05-20): realtime / gemini-live / grok-live arms removed —
 * those banners are deleted; voice agents are launched from the Tools modal.
 */
function updateClaudeCodeBanner(provider) {
    const banner = $('claudeCodeBanner');
    const geminiCodeBanner = $('geminiCodeBanner');
    const history = $('history');
    const baseHeaderPadding = 60;

    // Hide all CLI agent banners first
    if (banner) banner.classList.add('hide');
    if (geminiCodeBanner) geminiCodeBanner.classList.add('hide');

    if (provider === 'agents') {
        if (banner) {
            banner.classList.remove('hide');
            if (history) {
                const bannerHeight = banner.classList.contains('collapsed') ? 55 : 135;
                history.style.paddingTop = `${baseHeaderPadding + bannerHeight}px`;
            }
        }
    } else if (provider === 'gemini-agents') {
        if (geminiCodeBanner) {
            geminiCodeBanner.classList.remove('hide');
            if (history) {
                const bannerHeight = geminiCodeBanner.classList.contains('collapsed') ? 55 : 135;
                history.style.paddingTop = `${baseHeaderPadding + bannerHeight}px`;
            }
        }
    } else {
        if (history) {
            history.style.paddingTop = `${baseHeaderPadding}px`;
        }
    }
}
window.updateClaudeCodeBanner = updateClaudeCodeBanner;

/**
 * Update history padding based on banner state
 */
function updateHistoryPaddingForBanner() {
    const banner = $('claudeCodeBanner');
    const history = $('history');
    const baseHeaderPadding = 60;

    if (banner && history && !banner.classList.contains('hide')) {
        const bannerHeight = banner.classList.contains('collapsed') ? 55 : 135;
        history.style.paddingTop = `${baseHeaderPadding + bannerHeight}px`;
    }
}
window.updateHistoryPaddingForBanner = updateHistoryPaddingForBanner;

/**
 * Initialize Claude model selector
 */
function initClaudeModelSelector() {
    const claudeModelSelect = $('claudeModelSelect');
    if (claudeModelSelect) {
        const savedClaudeModel = localStorage.getItem('bb_claude_model') || 'sonnet';
        claudeModelSelect.value = savedClaudeModel;
        window.__claudeModel = savedClaudeModel;
        console.log(`[ClaudeModel] Initialized from localStorage: ${savedClaudeModel}`);

        claudeModelSelect.addEventListener('change', () => {
            window.__claudeModel = claudeModelSelect.value;
            localStorage.setItem('bb_claude_model', window.__claudeModel);
            console.log(`[ClaudeModel] Changed to: ${window.__claudeModel}`);
        });
    } else {
        window.__claudeModel = localStorage.getItem('bb_claude_model') || 'sonnet';
        console.log(`[ClaudeModel] Fallback to localStorage: ${window.__claudeModel}`);
    }
}

// =============================================================================
// Slash Commands
// =============================================================================

/**
 * Initialize slash command dropdown
 */
function initSlashCommands() {
    const promptEl = $("prompt");
    if (!promptEl) return;

    slashCommandDropdown = new SlashCommandDropdown();
    slashCommandDropdown.fetchCommands();
    console.log('[SlashCommands] Dropdown initialized, fetching commands...');

    promptEl.addEventListener("keydown", (e) => {
        if (slashCommandDropdown && slashCommandDropdown.handleKeydown(e)) {
            return;
        }

        if (e.key === '/' && window.__provider === 'agents') {
            const value = promptEl.value;
            const selStart = promptEl.selectionStart;
            if (selStart === 0 || (value.length > 0 && /\s/.test(value[selStart - 1]))) {
                setTimeout(() => {
                    if (promptEl.value.startsWith('/')) {
                        slashCommandDropdown.show(promptEl);
                    }
                }, 0);
            }
        }
    });

    promptEl.addEventListener("input", (e) => {
        if (slashCommandDropdown) {
            slashCommandDropdown.handleInput(e, window.__provider);
        }
    });
}

// =============================================================================
// History Initialization
// =============================================================================

/**
 * Initialize and render chat history
 */
function initHistory() {
    loadHistory();
    loadAudioCache();

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
                startThinkingAnimation(thinkingBubble);
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

        // Check if this assistant message has thinking/reasoning or CU content
        if (b.role === 'assistant' && (b.thinking || b.cuLog)) {
            console.log('[History] Restoring message with thinking/CU content');
            const bubbleWithThinking = createBubbleWithThinking(b.content, b.thinking, b.cuLog);
            hist.appendChild(bubbleWithThinking);
        } else {
            appendBubble(b.role, b.content);
        }
    }

    scrollToBottomIfNeeded();
    updateHintsVisibility();
}

// =============================================================================
// New Session Modal
// =============================================================================

/**
 * Initialize new session confirmation modal
 */
function initNewSessionModal() {
    const confirmBtn = $("btnConfirmNewSession");
    const cancelBtn = $("btnCancelNewSession");
    const modal = $("confirmNewSessionModal");

    if (confirmBtn) {
        confirmBtn.addEventListener('click', () => {
            if (window.agentHandler) {
                window.agentHandler.confirmNewSession();
            }
            if (modal) modal.classList.add('hide');
        });
    }

    if (cancelBtn) {
        cancelBtn.addEventListener('click', () => {
            if (window.agentHandler) {
                window.agentHandler.cancelNewSession();
            }
            if (modal) modal.classList.add('hide');
        });
    }
}

// =============================================================================
// Drag and Drop
// =============================================================================

/**
 * Setup drag and drop for file attachments
 */
function setupDragAndDrop() {
    const dropZone = $("history");
    if (!dropZone) return;

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');

        const files = e.dataTransfer.files;
        if (files.length > 0) {
            for (const file of files) {
                // Import dynamically to avoid circular dependency
                import('./file-upload.js').then(module => {
                    module.addAttachedFile(file);
                    toast(`Attached ${file.name}`);
                });
            }
        }
    });
}

/**
 * Setup file attachment button and hidden file input
 */
function setupFileAttachment() {
    const btnAttach = $("btnAttach");
    const fileInput = $("fileInput");

    if (!btnAttach) {
        console.warn('[FileAttachment] btnAttach button not found');
        return;
    }

    // Check if running in native Android app with file picker
    const hasAndroidFilePicker = typeof AndroidFilePicker !== 'undefined' &&
                                  typeof AndroidFilePicker.openFileChooser === 'function';

    btnAttach.addEventListener('click', () => {
        console.log('[FileAttachment] Attach button clicked');

        if (hasAndroidFilePicker) {
            // Use native Android file picker
            // Accept types: images, videos, audio, PDFs, text, docs
            const acceptTypes = 'image/*,video/*,audio/*,application/pdf,text/plain,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document';
            console.log('[FileAttachment] Using native Android file picker with types:', acceptTypes);
            try {
                AndroidFilePicker.openFileChooser(acceptTypes);
            } catch (e) {
                console.error('[FileAttachment] Native file picker error:', e);
                toast('Failed to open file picker');
            }
        } else if (fileInput) {
            // Use standard HTML file input
            console.log('[FileAttachment] Using HTML file input');
            fileInput.click();
        } else {
            console.error('[FileAttachment] No file input element found');
            toast('File attachment not available');
        }
    });

    // Handle file selection from HTML input
    if (fileInput) {
        fileInput.addEventListener('change', (e) => {
            const files = e.target.files;
            console.log('[FileAttachment] Files selected:', files.length);

            if (files.length > 0) {
                for (const file of files) {
                    addAttachedFile(file);
                    toast(`Attached ${file.name}`);
                }
            }

            // Reset input so same file can be selected again
            fileInput.value = '';
        });
    }

    console.log('[FileAttachment] Setup complete, native Android file picker:', hasAndroidFilePicker);
}

/**
 * Setup Android Overlay feature
 * Shows overlay section and handles toggle button when running in Android app
 */
function setupAndroidOverlay() {
    // Check if running in native Android app
    const isAndroid = typeof AndroidBridge !== 'undefined' || window.isAndroidWebWrapper === true;

    const overlaySection = $('androidOverlaySection');
    const toggleBtn = $('menuOverlayToggle');
    const statusText = $('overlayStatusText');

    if (!isAndroid) {
        console.log('[AndroidOverlay] Not running in Android app, skipping overlay setup');
        return;
    }

    console.log('[AndroidOverlay] Android app detected, enabling overlay feature');

    // Show the overlay section
    if (overlaySection) {
        overlaySection.style.display = 'block';
    }

    // Update button/status UI based on running state
    function setOverlayUIState(isRunning) {
        if (toggleBtn) {
            toggleBtn.textContent = isRunning ? 'Stop Overlay' : 'Start Overlay';
            if (isRunning) {
                toggleBtn.classList.add('active');
            } else {
                toggleBtn.classList.remove('active');
            }
        }
        if (statusText) {
            statusText.textContent = isRunning ? 'Running' : 'Not running';
            if (isRunning) {
                statusText.classList.add('running');
            } else {
                statusText.classList.remove('running');
            }
        }
    }

    // Check actual status from Android and update UI
    function updateOverlayStatus() {
        if (typeof AndroidBridge !== 'undefined' && typeof AndroidBridge.isOverlayRunning === 'function') {
            try {
                const isRunning = AndroidBridge.isOverlayRunning();
                setOverlayUIState(isRunning);
                return isRunning;
            } catch (e) {
                console.error('[AndroidOverlay] Error checking overlay status:', e);
            }
        }
        return false;
    }

    // Handle toggle button click
    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            if (typeof AndroidBridge === 'undefined') {
                toast('AndroidBridge not available');
                return;
            }

            try {
                const isRunning = typeof AndroidBridge.isOverlayRunning === 'function' && AndroidBridge.isOverlayRunning();

                if (isRunning) {
                    // Stop the overlay - immediately update UI
                    if (typeof AndroidBridge.stopOverlay === 'function') {
                        AndroidBridge.stopOverlay();
                        setOverlayUIState(false);  // Immediate feedback
                        toast('Overlay stopped');
                    }
                } else {
                    // Start the overlay - immediately update UI to show it's starting
                    if (typeof AndroidBridge.startOverlay === 'function') {
                        AndroidBridge.startOverlay();
                        setOverlayUIState(true);  // Immediate feedback - lock red
                        toast('Starting overlay...');
                    }
                }

                // Verify status after delays (overlay may take time to start due to permissions)
                setTimeout(updateOverlayStatus, 500);
                setTimeout(updateOverlayStatus, 1500);
                setTimeout(updateOverlayStatus, 3000);
            } catch (e) {
                console.error('[AndroidOverlay] Error toggling overlay:', e);
                toast('Error: ' + e.message);
                updateOverlayStatus();  // Reset to actual state on error
            }
        });
    }

    // Initial status check (with delay to ensure AndroidBridge is ready)
    setTimeout(updateOverlayStatus, 100);
    setTimeout(updateOverlayStatus, 1000);

    // Update status when menu is shown
    const menuBtn = $('menuBtn');
    if (menuBtn) {
        menuBtn.addEventListener('click', () => {
            setTimeout(updateOverlayStatus, 100);
        });
    }

    // Periodic status check (every 1 second when menu is visible)
    setInterval(() => {
        const menuPanel = $('menuPanel');
        if (menuPanel && !menuPanel.classList.contains('hide')) {
            updateOverlayStatus();
        }
    }, 1000);

    console.log('[AndroidOverlay] Setup complete');
}

// =============================================================================
// Main Initialization
// =============================================================================

/**
 * Main application initialization
 */
async function initApp() {
    console.log('[Portal] Starting modular app initialization...');

    // Phase 1: Foundation
    console.log('[Portal] Phase 1: Foundation...');
    await initProviderModelSelectors();
    initClaudeModelSelector();
    initOperatorSelector();

    // Phase 2: UI Components
    console.log('[Portal] Phase 2: UI Components...');
    configureMarked();
    setupScrollButton();
    setupDynamicHistoryPadding();
    setupImageLightbox();
    setupKeyboardShortcuts();
    setupTimelineBrowser();
    setupTaskMonitor();
    adjustClaudeBannerPosition();
    initNotificationSystem();
    initHelpHints();
    initModalHandlers();

    // Phase 3: Features
    console.log('[Portal] Phase 3: Features...');
    initVoiceSelector();
    initSystemPromptSection();
    initAutoTTS();
    setupNativeSTTCallbacks();  // Setup native Android mic callbacks
    setupNativeFileCallback();
    // Initialize the singleton taskManager with dependencies
    taskManager.init(addBubble, attachGeneratedAudioPlayer);
    window.taskManager = taskManager;

    // Phase 4: Chat Components
    console.log('[Portal] Phase 4: Chat Components...');
    initGenerationModals();
    initCronManager();
    initDeviceManager();
    initDevicesSection();   // M3 3.7 — System-Menu tailnet device management
    initTelephonyManager();
    initTelephonyWizard();
    initCellularManager();
    initSMSInbox();
    initContactsManager();
    // SMS Inbox button
    const btnSMS = $('btnSMSInbox');
    if (btnSMS) btnSMS.addEventListener('click', () => openSMSInbox());
    // Poll unread SMS every 30s
    pollSMSUnread();
    setInterval(pollSMSUnread, 30000);

    // Computer Use Tools button — shortcut that switches chat provider to
    // computer-use, which auto-attaches the existing cu-drawer to the
    // operator-menu-bubble. Per docs/plans/2026-05-20-portal-tools-section-alignment.md
    // Track 2 — Brandon decision: keep existing cu-drawer flow exactly as-is.
    const btnComputerUse = $('btnComputerUse');
    if (btnComputerUse) {
        btnComputerUse.addEventListener('click', () => {
            const providerSelect = document.getElementById('providerSelect');
            if (providerSelect) {
                providerSelect.value = 'computer-use';
                providerSelect.dispatchEvent(new Event('change'));
            }
        });
    }

    // CLI Agents Tools button — opens a modal that picks app folder + provider
    // (Claude / Gemini / Codex) and streams a tmux PTY session into a simple
    // <pre> output panel. Per docs/plans/2026-05-20-portal-tools-section-alignment.md
    // Track 3. The init function wires the button click itself.
    initCLIAgentsModal();

    // Voice Agents Tools button — opens a modal with provider tabs (GPT
    // Realtime / Gemini Live / Grok Live). The voice-agents-modal handles
    // lazy init of the three underlying provider modules on first open;
    // closing the modal while connected disconnects all active providers.
    // Per docs/plans/2026-05-20-portal-tools-section-alignment.md Track 4.
    initVoiceAgentsModal();

    // Gmail Tools button — opens a minimal Connect/Disconnect modal that
    // calls the existing /auth/gmail/* + /gmail/status|disconnect endpoints.
    // Matches Android's "📧 Connect Gmail" scope (SettingsSheet.kt:391) — no
    // inbox UI. Per docs/plans/2026-05-20-portal-tools-section-alignment.md
    // Track 5. Inbox interactions are agent-driven via gmail_* MCP tools.
    initGmailModal();
    initMediaManager();
    initAgentHandler();
    initGeminiAgentHandler();
    initNewSessionModal();
    setupDragAndDrop();
    setupFileAttachment();
    setupAndroidOverlay();  // Setup Android floating overlay feature

    // Phase 5: Chat Send
    console.log('[Portal] Phase 5: Chat Send...');
    initChatSend();
    initCUDrawer();
    initSlashCommands();

    // Phase 6-8: GPT-4o Realtime / Gemini Live / Grok Live
    // Per Track 4 (2026-05-20), voice provider modules init LAZILY from
    // voice-agents-modal.js on first modal open — the inline banners they
    // used to drive are gone, so eager init is unnecessary work. Disconnect
    // helpers remain exported below for any back-compat consumers.

    // Initialize history last (after all components ready)
    console.log('[Portal] Initializing history...');
    initHistory();

    // Generation ember backdrop — fades the website's ember particles in while
    // the AI is generating (text or media), then drains them on completion.
    // Reads generation state from existing DOM markers; no edits to send paths.
    initEmberFX();
    initEmberModeControl();   // wire the Off / While generating / Always-on settings control

    // Check for existing agent session
    await checkExistingAgentSession();

    // Check for running Computer Use background task
    await checkCUBackgroundStatus();

    // Sync voice preference
    const operator = getOperator();
    if (operator) {
        syncVoiceDropdown();
    }

    // Refresh health status
    await refreshHealth();

    // Periodic health refresh (every 60 seconds)
    setInterval(refreshHealth, 60000);

    console.log('[Portal] App initialization complete!');
}

// =============================================================================
// DOMContentLoaded Handler
// =============================================================================

document.addEventListener("DOMContentLoaded", async function() {
    try {
        await initApp();
    } catch (err) {
        console.error('[Portal] Initialization error:', err);
        toast('App initialization failed: ' + err.message);
    }
});

// =============================================================================
// Gemini Recorder (separate handler)
// =============================================================================

document.addEventListener('DOMContentLoaded', initGeminiAudioRecorder);

// =============================================================================
// Exports
// =============================================================================

export {
    initApp,
    slashCommandDropdown,
    taskManager,
    updateClaudeCodeBanner,
    updateHistoryPaddingForBanner,
    realtimeConnect,
    realtimeDisconnect,
    geminiLiveConnect,
    geminiLiveDisconnect
};
