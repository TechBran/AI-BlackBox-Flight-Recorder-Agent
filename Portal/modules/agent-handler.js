/**
 * agent-handler.js
 * Claude Code Agent WebSocket handler
 * Manages agent sessions, streaming, and tool execution display
 *
 * Source: Portal/app.js lines 8411-9959
 * Refactored: 2025-12-20
 */

import { $, toast } from './core-utils.js';
import { getHistoryData, setHistoryData, saveHistory, getOperator, getOperatorSession, saveOperatorSession, clearOperatorSession } from './state-management.js';
import { addBubble } from './chat-bubbles.js';
import { speakToBubble, setLastAssistantText } from './tts-stt.js';
import { makeSnapshotsClickable, applySyntaxHighlighting } from './timeline-browser.js';
import { scrollToBottomIfNeeded, setStreamingState } from './ui-setup.js';
import { todoTracker } from './todo-tracker.js';
import { statusLine } from './status-line.js';

// =============================================================================
// AgentChatHandler Class
// =============================================================================

/**
 * Handler for Claude Code agent WebSocket communication
 * Manages streaming output, tool execution display, and session persistence
 */
// Reasoning/Thinking cycling phrases
const REASONING_PHRASES = [
    "Reasoning",
    "Thinking deeply",
    "Analyzing",
    "Processing",
    "Contemplating",
    "Evaluating options",
    "Connecting ideas",
    "Synthesizing",
    "Deliberating",
    "Exploring paths",
    "Weighing factors",
    "Forming insights",
    "Building context",
    "Mapping patterns",
    "Refining thoughts",
    "Considering angles",
    "Crafting logic",
    "Structuring response",
    "Neural processing",
    "Deep analysis"
];

export class AgentChatHandler {
    constructor() {
        this.ws = null;
        this.sessionId = null;
        this.currentBubble = null;
        this.outputBuffer = '';
        this.thinkingBuffer = '';
        this.speakableBuffer = '';  // Text content only (no tool use) for TTS
        this.isStreaming = false;
        this.hasActiveSession = false;  // Track if we have an ongoing conversation
        this.messageCount = 0;
        this.currentOperator = null;
        this.lastTool = null;
        this.hasReceivedStreamingContent = false;
        this._saveStateTimer = null;
        this.permissionKeyboardHandler = null;
        this.activePermissionDialog = null;
        // Thinking budget tracking
        this.thinkingTokens = 0;
        this.thinkingBudget = 31999;  // Claude's max thinking budget
        this.permissionMode = 'auto';  // 'normal', 'auto', 'plan'
        // Reasoning animation
        this.reasoningCycleInterval = null;
        this.reasoningPhraseIndex = 0;
        // Reconnection state
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 15;
        this.reconnectTimer = null;

        // Setup End Session button handler
        const endBtn = $('endAgentSession');
        if (endBtn) {
            endBtn.onclick = () => this.endSession();
        }

        // Setup New Session button handler
        const newSessionBtn = $('newAgentSession');
        if (newSessionBtn) {
            newSessionBtn.onclick = () => this.startNewSession();
        }

        // Setup Dev Snapshot button handler
        const snapshotBtn = $('devSnapshotBtn');
        if (snapshotBtn) {
            snapshotBtn.onclick = () => this.createDevSnapshot();
        }

        // Setup Banner Collapse/Expand Toggle
        const bannerCollapsed = $('claudeBannerCollapsed');
        const bannerCollapseBtn = $('bannerCollapseBtn');

        // Define toggle functions that get banner fresh each time
        window.expandAgentBanner = function(e) {
            if (e) e.preventDefault();
            const banner = $('claudeCodeBanner');
            if (banner) {
                banner.classList.remove('collapsed');
                if (window.updateHistoryPaddingForBanner) {
                    window.updateHistoryPaddingForBanner();
                }
            }
        };

        window.collapseAgentBanner = function(e) {
            if (e) {
                e.preventDefault();
                e.stopPropagation();
            }
            const banner = $('claudeCodeBanner');
            if (banner) {
                banner.classList.add('collapsed');
                if (window.updateHistoryPaddingForBanner) {
                    window.updateHistoryPaddingForBanner();
                }
            }
        };

        if (bannerCollapsed) {
            bannerCollapsed.addEventListener('click', window.expandAgentBanner, { passive: false, capture: true });
            bannerCollapsed.setAttribute('role', 'button');
            bannerCollapsed.setAttribute('tabindex', '0');
            bannerCollapsed.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    window.expandAgentBanner(e);
                }
            });
        }

        if (bannerCollapseBtn) {
            bannerCollapseBtn.addEventListener('click', window.collapseAgentBanner, { passive: false, capture: true });
            bannerCollapseBtn.setAttribute('role', 'button');
            bannerCollapseBtn.setAttribute('tabindex', '0');
            bannerCollapseBtn.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    window.collapseAgentBanner(e);
                }
            });
        }

        // Setup Permission Mode click handler
        const permissionIndicator = $('permissionModeIndicator');
        if (permissionIndicator) {
            permissionIndicator.addEventListener('click', () => this.cyclePermissionMode());
        }

        // Setup Shift+Tab keyboard handler for permission mode cycling
        document.addEventListener('keydown', (e) => {
            if (e.shiftKey && e.key === 'Tab') {
                // Only handle if agents banner is visible
                const banner = $('claudeCodeBanner');
                if (banner && !banner.classList.contains('hide')) {
                    e.preventDefault();
                    this.cyclePermissionMode();
                }
            }
        });

        // Initialize permission mode indicator
        this.updatePermissionModeIndicator();

        // Save state when page is about to unload
        window.addEventListener('beforeunload', () => {
            if (this.isStreaming && this.currentBubble) {
                this._saveToHistory();
            }
        });
    }

    // =========================================================================
    // Session Management
    // =========================================================================

    /**
     * Start a new clean session (clears current and lets user send new prompt)
     */
    startNewSession() {
        const modal = $('confirmNewSessionModal');
        if (modal) {
            modal.classList.remove('hide');
        }
    }

    /**
     * Actually perform the session clear (called when user confirms)
     */
    confirmNewSession() {
        console.log('[Agent] Starting new session');
        const operator = this.currentOperator || window.__operator || 'Brandon';
        clearOperatorSession(operator);
        todoTracker.clear();  // Clear todo tracker for new session
        statusLine.reset();   // Reset status line for new session
        this.endSession();
        toast('Session cleared. Ready for new prompt.');

        const modal = $('confirmNewSessionModal');
        if (modal) {
            modal.classList.add('hide');
        }
    }

    /**
     * Cancel new session (user clicked Cancel)
     */
    cancelNewSession() {
        toast('Session kept. Continuing with current context.');
        const modal = $('confirmNewSessionModal');
        if (modal) {
            modal.classList.add('hide');
        }
    }

    /**
     * Create a dev snapshot by triggering the /snapshot-dev slash command
     */
    createDevSnapshot() {
        console.log('[Agent] Triggering /snapshot-dev command');

        if (!this.hasActiveSession || !this.sessionId) {
            toast('No active session. Start a conversation first.');
            return;
        }

        const promptEl = $('prompt');
        if (promptEl) {
            promptEl.value = '/snapshot-dev';
            $('btnSend')?.click();
            toast('Snapshot command sent to Claude Code...');
        }
    }

    /**
     * Update banner status display
     * @param {string} status - Status text
     * @param {boolean} isStreaming - Whether currently streaming
     */
    updateBannerStatus(status, isStreaming = false) {
        const statusEl = $('claudeAgentStatus');
        const statusCompactEl = $('claudeStatusCompact');
        const bannerEl = $('claudeCodeBanner');

        const isReady = status.toLowerCase().includes('ready') ||
                        status.toLowerCase().includes('active') ||
                        status.toLowerCase().includes('msgs)');

        if (statusEl) {
            statusEl.textContent = status;
            if (isStreaming) {
                statusEl.classList.add('streaming');
                statusEl.classList.remove('ready');
            } else {
                statusEl.classList.remove('streaming');
                if (isReady) {
                    statusEl.classList.add('ready');
                } else {
                    statusEl.classList.remove('ready');
                }
            }
        }

        if (statusCompactEl) {
            statusCompactEl.textContent = status;
            if (isStreaming) {
                statusCompactEl.classList.add('streaming');
                statusCompactEl.classList.remove('ready');
            } else {
                statusCompactEl.classList.remove('streaming');
                if (isReady) {
                    statusCompactEl.classList.add('ready');
                } else {
                    statusCompactEl.classList.remove('ready');
                }
            }
        }

        if (bannerEl) {
            if (isStreaming) {
                bannerEl.classList.add('working');
                bannerEl.classList.remove('session-ready');
            } else {
                bannerEl.classList.remove('working');
                if (isReady) {
                    bannerEl.classList.add('session-ready');
                } else {
                    bannerEl.classList.remove('session-ready');
                }
            }
        }

        const endBtn = $('endAgentSession');
        if (endBtn) {
            if (this.hasActiveSession && !isStreaming) {
                endBtn.classList.remove('hide');
            } else {
                endBtn.classList.add('hide');
            }
        }
    }

    /**
     * Attempt to reconnect WebSocket with exponential backoff
     */
    attemptReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.log('[Agent] Max reconnect attempts reached');
            this.updateBannerStatus('Connection Lost');
            return;
        }

        this.reconnectAttempts++;
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts - 1), 30000);
        console.log(`[Agent] Reconnecting (${this.reconnectAttempts}/${this.maxReconnectAttempts}) in ${delay}ms...`);
        this.updateBannerStatus(`Reconnecting (${this.reconnectAttempts})...`);

        this.reconnectTimer = setTimeout(() => {
            if (!this.sessionId) return;

            const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${wsProtocol}//${window.location.host}/ws/agent/${this.sessionId}`;

            try {
                this.ws = new WebSocket(wsUrl);

                this.ws.onopen = () => {
                    console.log('[Agent] Reconnected successfully');
                    this.reconnectAttempts = 0;
                    this.updateBannerStatus('Reconnected', true);

                    this.ws.send(JSON.stringify({
                        type: 'reconnect',
                        operator: this.currentOperator || window.__operator || 'Brandon'
                    }));
                };

                this.ws.onmessage = (e) => {
                    try {
                        const msg = JSON.parse(e.data);
                        this.handleMessage(msg);
                    } catch (err) {
                        console.error('[Agent] Parse error on reconnect:', err);
                    }
                };

                this.ws.onclose = (e) => {
                    if (e.code !== 1000 && this.sessionId) {
                        this.attemptReconnect();
                    } else {
                        this.isStreaming = false;
                        this.updateBannerStatus('Session Complete');
                    }
                };

                this.ws.onerror = () => {};
            } catch (err) {
                console.error('[Agent] Reconnect failed:', err);
                this.attemptReconnect();
            }
        }, delay);
    }

    /**
     * Start a new agent session
     * @param {string} prompt - User prompt
     * @param {string} operator - Operator name
     */
    async startSession(prompt, operator) {
        this.currentOperator = operator || window.__operator || 'Brandon';

        const persistedSessionId = getOperatorSession(this.currentOperator);
        const isNewSession = !this.hasActiveSession || !this.sessionId;

        if (isNewSession) {
            if (persistedSessionId && !this.sessionId) {
                this.sessionId = persistedSessionId;
                console.log('[Agent] Restoring persisted session:', this.sessionId);
            } else {
                this.sessionId = crypto.randomUUID();
                saveOperatorSession(this.currentOperator, this.sessionId);
            }
            this.messageCount = 0;
        }

        // Always initialize todo tracker and status line (ensures DOM elements are cached)
        todoTracker.init('todoPanel', this.sessionId);
        statusLine.init('agentStatusLine');

        this.outputBuffer = '';
        this.thinkingBuffer = '';
        this.speakableBuffer = '';
        this.isStreaming = true;
        setStreamingState(true); // Enable smooth scrolling
        this.hasReceivedStreamingContent = false;
        this.messageCount++;

        console.log('[Agent] Session:', this.sessionId, 'Message #:', this.messageCount, 'New:', isNewSession);
        console.log('[Agent] Prompt:', prompt);
        console.log('[Agent] Operator:', this.currentOperator);

        this.updateBannerStatus(isNewSession ? 'Starting...' : 'Continuing...', true);

        this.currentBubble = addBubble('assistant', '');
        this.currentBubble.classList.add('agent-streaming');

        const bubbleText = this.currentBubble.querySelector('.bubble-text');
        if (bubbleText) {
            const loadingMsg = isNewSession ? 'Starting Claude Code session...' : 'Continuing conversation...';
            bubbleText.innerHTML = `<div class="agent-loading">${loadingMsg}</div>`;
        }

        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProtocol}//${window.location.host}/ws/agent/${this.sessionId}`;

        console.log('[Agent] WebSocket URL:', wsUrl);

        try {
            this.ws = new WebSocket(wsUrl);
        } catch (err) {
            console.error('[Agent] WebSocket creation error:', err);
            this.updateBannerStatus('Connection Failed');
            if (bubbleText) {
                bubbleText.innerHTML = '<div class="agent-error">Failed to connect to Claude Code CLI</div>';
            }
            return;
        }

        this.ws.onopen = () => {
            this.reconnectAttempts = 0;
            console.log('[Agent] WebSocket connected, sending prompt...');
            this.updateBannerStatus('Connected', true);

            const claudeModelSelect = $('claudeModelSelect');
            const selectedModel = claudeModelSelect ? claudeModelSelect.value : (window.__claudeModel || 'sonnet');
            console.log(`[Agent] Model selector value: ${claudeModelSelect?.value}, window.__claudeModel: ${window.__claudeModel}, using: ${selectedModel}`);

            // Start status line session
            statusLine.startSession(this.sessionId, selectedModel);

            const skipPermissions = true;

            const message = {
                type: 'prompt',
                text: prompt,
                operator: operator || window.__operator || 'Brandon',
                working_dir: null,
                model: selectedModel,
                skip_permissions: skipPermissions
            };
            console.log('[Agent] Sending:', message);
            this.ws.send(JSON.stringify(message));
        };

        this.ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                this.handleMessage(msg);
            } catch (err) {
                console.error('[Agent] Parse error:', err);
            }
        };

        this.ws.onclose = (e) => {
            console.log('[Agent] WebSocket closed:', e.code, e.reason);
            if (e.code !== 1000 && this.sessionId) {
                this.attemptReconnect();
            } else {
                this.isStreaming = false;
                $('thinkingIndicator')?.classList.add('hide'); // don't leave embers pinned on
                this.updateBannerStatus('Disconnected');
                if (this.currentBubble) {
                    this.currentBubble.classList.remove('agent-streaming');
                }
                statusLine.endSession();
            }
        };

        this.ws.onerror = (err) => {
            console.error('[Agent] WebSocket error:', err);
            this.updateBannerStatus('Error');
            toast('Agent connection error - check console');
            this.isStreaming = false;
            $('thinkingIndicator')?.classList.add('hide'); // don't leave embers pinned on
            statusLine.endSession();
        };
    }

    // =========================================================================
    // Message Handling
    // =========================================================================

    /**
     * Handle incoming WebSocket message
     * @param {Object} msg - Message object with type and data
     */
    handleMessage(msg) {
        console.log('[Agent] Received message:', msg.type);
        switch(msg.type) {
            case 'content':
                this.updateBannerStatus('Responding...', true);
                this.hasReceivedStreamingContent = true;
                this.appendContent(msg.data);
                break;
            case 'assistant_message':
                if (!this.hasReceivedStreamingContent) {
                    this.updateBannerStatus('Responding...', true);
                    this.appendContent(msg.data);
                }
                break;
            case 'thinking':
                this.updateBannerStatus('Thinking...', true);
                this.appendThinking(msg.data);
                break;
            case 'tool_use':
                this.updateBannerStatus('Using tools...', true);
                this.appendToolUse(msg.data);
                break;
            case 'tool_result':
                this.updateBannerStatus('Processing...', true);
                this.appendToolResult(msg.data);
                break;
            case 'file_result':
                this.updateBannerStatus('File operation...', true);
                this.appendFileResult(msg.data);
                break;
            case 'edit_diff':
                this.updateBannerStatus('Edit complete', true);
                this.appendEditDiff(msg.data);
                break;
            case 'raw':
                this.updateBannerStatus('Active', true);
                this.appendRawOutput(msg.data);
                break;
            case 'output':
                this.updateBannerStatus('Streaming...', true);
                this.appendRawOutput(msg.data);
                break;
            case 'waiting':
                this.updateBannerStatus('Waiting for input', true);
                console.log('[Agent] Claude waiting for input');
                break;
            case 'completed':
                this.hasActiveSession = true;
                this.updateBannerStatus(`Session Active (${this.messageCount} msgs)`);
                this.isStreaming = false;
                setStreamingState(false); // Disable smooth scrolling
                this.currentBubble?.classList.remove('agent-streaming');
                this.finalizeOutput();
                break;
            case 'session_ended':
                this.hasActiveSession = false;
                this.sessionId = null;
                this.messageCount = 0;
                this.updateBannerStatus('Ready');
                toast(msg.data || 'Session ended');
                break;
            case 'permission_request':
                this.updateBannerStatus('Permission Required', true);
                this.showPermissionButtons(msg.data);
                if (navigator.vibrate) {
                    navigator.vibrate([200, 100, 200]);
                }
                break;
            case 'info':
                console.log('[Agent] Info:', msg.data);
                toast(msg.data);
                break;
            case 'error':
                this.updateBannerStatus('Error');
                console.error('[Agent] Error:', msg.data);
                toast(`Agent error: ${msg.data}`);
                break;
            case 'app_detected':
                console.log('[Agent] App detected:', msg.data);
                this.addAppToPanel(msg.data);
                toast(`App detected on port ${msg.data.port}`, 3000);
                break;
            case 'pong':
                break;
            case 'todo_write':
                // Handle TodoWrite tool - update task tracker
                this.updateBannerStatus('Working...', true);
                todoTracker.updateTodos(msg.data);
                break;
            case 'status_update':
                // Handle status updates (context/cost/model)
                statusLine.updateFromMessage(msg.data);
                break;
            case 'image_task': {
                console.log('[Agent] Image generation task:', msg.data);
                const imgData = msg.data;
                import('./task-manager.js').then(({ taskManager }) => {
                    taskManager.addTask(imgData.task_id, 'image_generation', imgData.prompt, null, imgData.count);
                    console.log(`[Agent] Tracking image generation: ${imgData.task_id}`);
                });
                break;
            }
            case 'video_task': {
                console.log('[Agent] Video generation task:', msg.data);
                const vidData = msg.data;
                import('./task-manager.js').then(({ taskManager }) => {
                    taskManager.addTask(vidData.task_id, 'video_generation', vidData.prompt, { duration: vidData.duration, resolution: vidData.resolution });
                    console.log(`[Agent] Tracking video generation: ${vidData.task_id}`);
                });
                break;
            }
            case 'music_task': {
                console.log('[Agent] Music generation task:', msg.data);
                const musData = msg.data;
                import('./task-manager.js').then(({ taskManager }) => {
                    taskManager.addTask(musData.task_id, 'lyria_music', musData.prompt, null, musData.sample_count || 1);
                    console.log(`[Agent] Tracking music generation: ${musData.task_id}`);
                });
                break;
            }
            default:
                console.warn('[Agent] Unhandled message type:', msg.type, 'data:', msg.data);
                if (typeof msg.data === 'string' && msg.data.length < 10000) {
                    this.appendContent(msg.data);
                }
                break;
        }
    }

    // =========================================================================
    // Content Appending
    // =========================================================================

    /**
     * Append content to output buffer
     * @param {string} text - Content to append
     */
    appendContent(text) {
        text = this.enhanceContent(text);
        this.outputBuffer += text;
        this.speakableBuffer += text;
        this.renderOutput();
    }

    /**
     * Enhance content with file path highlighting and diff markers
     * @param {string} text - Text to enhance
     * @returns {string} Enhanced text
     */
    enhanceContent(text) {
        // Detect file path references
        text = text.replace(/([\/\w\-\.]+\.(js|py|html|css|json|ts|tsx|jsx|md|txt|yaml|yml|xml|sh)):(\d+)/g,
            '<code class="file-ref">$1:<span class="line-num">$3</span></code>');

        // Detect diff markers
        if (text.includes('---') && text.includes('+++')) {
            text = text.replace(/^(---\s+.*)$/gm, '<span class="diff-file-old">$1</span>');
            text = text.replace(/^(\+\+\+\s+.*)$/gm, '<span class="diff-file-new">$1</span>');
        }
        text = text.replace(/^(\+.*)$/gm, '<span class="diff-add">$1</span>');
        text = text.replace(/^(-.*)$/gm, '<span class="diff-remove">$1</span>');
        text = text.replace(/^(@@.*)$/gm, '<span class="diff-hunk">$1</span>');

        return text;
    }

    /**
     * Append thinking content
     * @param {string} text - Thinking text
     */
    appendThinking(text) {
        if (!this.thinkingBuffer) this.thinkingBuffer = '';
        this.thinkingBuffer += text;
        this.speakableBuffer += text;

        // Estimate tokens (rough: ~4 chars per token for English)
        this.thinkingTokens = Math.round(this.thinkingBuffer.length / 4);
        this.updateThinkingIndicator();

        this.renderOutput();
    }

    /**
     * Update the thinking indicator display
     */
    updateThinkingIndicator() {
        const indicator = $('thinkingIndicator');
        if (!indicator) return;

        const percent = Math.min(100, Math.round((this.thinkingTokens / this.thinkingBudget) * 100));
        const fill = indicator.querySelector('.thinking-budget-fill');
        const text = indicator.querySelector('.thinking-budget-text');

        if (fill) {
            fill.style.width = `${percent}%`;
            // Warning colors when over 80%
            if (percent > 80) {
                fill.classList.add('warning');
            } else {
                fill.classList.remove('warning');
            }
        }

        if (text) {
            const tokensK = (this.thinkingTokens / 1000).toFixed(1);
            const budgetK = (this.thinkingBudget / 1000).toFixed(0);
            text.textContent = `${tokensK}k / ${budgetK}k`;
        }

        // Show indicator when thinking
        if (this.thinkingBuffer && this.thinkingBuffer.length > 0) {
            indicator.classList.remove('hide');
        }
    }

    /**
     * Reset thinking for new message
     */
    resetThinking() {
        this.thinkingBuffer = '';
        this.thinkingTokens = 0;
        this.stopReasoningCycle();
        const indicator = $('thinkingIndicator');
        if (indicator) {
            indicator.classList.add('hide');
        }
    }

    /**
     * Start the reasoning label cycling animation
     * @param {HTMLElement} container - Container with reasoning panel
     */
    startReasoningCycle(container) {
        // Don't start if already running
        if (this.reasoningCycleInterval) return;

        const label = container?.querySelector('.reasoning-label');
        if (!label) return;

        this.reasoningPhraseIndex = 0;

        this.reasoningCycleInterval = setInterval(() => {
            if (!this.isStreaming) {
                this.stopReasoningCycle();
                return;
            }

            this.reasoningPhraseIndex = (this.reasoningPhraseIndex + 1) % REASONING_PHRASES.length;

            // Fade out, change, fade in
            label.style.opacity = '0';
            setTimeout(() => {
                label.textContent = REASONING_PHRASES[this.reasoningPhraseIndex];
                label.style.opacity = '1';
            }, 150);
        }, 2000);
    }

    /**
     * Stop the reasoning label cycling animation
     */
    stopReasoningCycle() {
        if (this.reasoningCycleInterval) {
            clearInterval(this.reasoningCycleInterval);
            this.reasoningCycleInterval = null;
        }
    }

    /**
     * Cycle through permission modes (Normal → Auto → Plan → Normal)
     */
    cyclePermissionMode() {
        const modes = ['normal', 'auto', 'plan'];
        const currentIndex = modes.indexOf(this.permissionMode);
        const nextIndex = (currentIndex + 1) % modes.length;
        this.permissionMode = modes[nextIndex];
        this.updatePermissionModeIndicator();

        // Show toast with new mode
        const modeNames = {
            'normal': 'Normal (prompts for permissions)',
            'auto': 'Auto-Accept (all tools approved)',
            'plan': 'Plan Mode (read-only)'
        };
        toast(`Permission mode: ${modeNames[this.permissionMode]}`);

        console.log(`[Agent] Permission mode changed to: ${this.permissionMode}`);
    }

    /**
     * Update the permission mode indicator display
     */
    updatePermissionModeIndicator() {
        const indicator = $('permissionModeIndicator');
        if (!indicator) return;

        const modes = {
            'normal': { label: 'Normal', class: 'mode-normal' },
            'auto': { label: 'Auto-Accept', class: 'mode-auto' },
            'plan': { label: 'Plan Mode', class: 'mode-plan' }
        };

        const mode = modes[this.permissionMode] || modes['auto'];

        // Update text
        indicator.textContent = mode.label;

        // Update classes
        indicator.className = `permission-mode ${mode.class}`;
    }

    /**
     * Append tool use indicator
     * @param {Object} data - Tool use data
     */
    appendToolUse(data) {
        const toolName = data.name || 'Unknown';
        const toolInput = data.input || {};

        this.lastTool = {
            name: toolName,
            input: toolInput,
            timestamp: Date.now()
        };

        this.updateToolIndicator();
    }

    /**
     * Append tool result
     * @param {string} text - Tool result text
     */
    appendToolResult(text) {
        const isDiff = text.includes('---') || text.includes('+++') ||
                      /^[\+\-@].*$/m.test(text);

        const lineCount = (text.match(/\n/g) || []).length + 1;
        const lang = isDiff ? 'diff' : '';

        // Use collapsible for large outputs (>10 lines)
        if (lineCount > 10) {
            const summary = `Tool output (${lineCount} lines)`;
            this.outputBuffer += `\n<details class="tool-result-collapsible" open><summary>${summary}</summary>\n\n\`\`\`${lang}\n${text}\n\`\`\`\n</details>\n`;
        } else {
            this.outputBuffer += '\n```' + lang + '\n' + text + '\n```\n';
        }

        this.renderOutput();
    }

    /**
     * Append file result
     * @param {Object} data - File result data
     */
    appendFileResult(data) {
        const filePath = data.filePath || 'unknown';
        const content = data.content || '';
        const numLines = data.numLines || 0;
        const startLine = data.startLine || 1;

        const shortPath = filePath.split('/').slice(-2).join('/');
        const fileName = filePath.split('/').pop();
        const lineRange = `lines ${startLine}-${startLine + numLines - 1}`;

        const ext = filePath.split('.').pop().toLowerCase();
        const langMap = {
            'js': 'javascript', 'ts': 'typescript', 'py': 'python',
            'html': 'html', 'css': 'css', 'json': 'json', 'md': 'markdown',
            'sh': 'bash', 'yaml': 'yaml', 'yml': 'yaml'
        };
        const lang = langMap[ext] || '';

        // Always use collapsible for file reads
        const icon = '\uD83D\uDCC4'; // 📄
        const summary = `${icon} <strong>${fileName}</strong> <span class="file-line-range">${lineRange}</span>`;
        this.outputBuffer += `\n<details class="file-result-collapsible" open><summary>${summary}</summary>\n\n\`\`\`${lang}\n${content}\n\`\`\`\n</details>\n`;

        this.renderOutput();
    }

    /**
     * Append edit diff result
     * @param {Object} data - Edit diff data
     */
    appendEditDiff(data) {
        const filePath = data.filePath || 'unknown';
        const diff = data.diff || '';
        const oldString = data.oldString || '';
        const newString = data.newString || '';

        const fileName = filePath.split('/').pop();
        const lineCount = (diff.match(/\n/g) || []).length + 1;

        // Always use collapsible for edits
        const icon = '\u270F\uFE0F'; // ✏️
        let summary = `${icon} <strong>Edited ${fileName}</strong>`;
        if (lineCount > 1) {
            summary += ` <span class="edit-line-count">(${lineCount} lines)</span>`;
        }

        let details = `\n<details class="edit-diff-collapsible" open><summary>${summary}</summary>\n\n\`\`\`diff\n${diff}\n\`\`\`\n`;

        if (oldString && newString && oldString.length < 100 && newString.length < 100) {
            details += `\n> Changed: \`${this.escapeHtml(oldString.substring(0, 50))}${oldString.length > 50 ? '...' : ''}\` \u2192 \`${this.escapeHtml(newString.substring(0, 50))}${newString.length > 50 ? '...' : ''}\`\n`;
        }

        details += '</details>\n';
        this.outputBuffer += details;

        this.renderOutput();
    }

    /**
     * Append raw output
     * @param {string} html - Raw HTML to append
     */
    appendRawOutput(html) {
        this.outputBuffer += html;

        let terminal = this.currentBubble?.querySelector('.agent-terminal');
        if (!terminal) {
            const bubbleText = this.currentBubble?.querySelector('.bubble-text');
            if (bubbleText) {
                bubbleText.innerHTML = '<div class="agent-terminal"></div>';
                terminal = bubbleText.querySelector('.agent-terminal');
            }
        }

        if (terminal) {
            terminal.innerHTML = this.outputBuffer;
            this.detectAndShowQuickActions();

            scrollToBottomIfNeeded();
        }
    }

    // =========================================================================
    // Tool Indicator
    // =========================================================================

    /**
     * Update the tool indicator display (sticky at bottom of bubble)
     */
    updateToolIndicator() {
        if (!this.currentBubble || !this.lastTool) return;

        const tool = this.lastTool;
        const icon = this.getToolIcon(tool.name);

        let content = `<span class="tool-icon">${icon}</span> <strong>${tool.name}</strong>`;

        if (tool.input.command) {
            content += ` <span class="tool-detail">${this.escapeHtml(tool.input.command.substring(0, 50))}${tool.input.command.length > 50 ? '...' : ''}</span>`;
        } else if (tool.input.file_path) {
            const fileName = tool.input.file_path.split('/').pop();
            content += ` <span class="tool-detail">${this.escapeHtml(fileName)}</span>`;
        } else if (tool.input.pattern) {
            content += ` <span class="tool-detail">${this.escapeHtml(tool.input.pattern.substring(0, 35))}${tool.input.pattern.length > 35 ? '...' : ''}</span>`;
        }

        // Update sticky tool indicator in bubble
        let indicator = this.currentBubble.querySelector('.tool-indicator');
        if (!indicator) {
            indicator = document.createElement('div');
            indicator.className = 'tool-indicator';
            // Append to end of bubble so it's sticky at bottom
            this.currentBubble.appendChild(indicator);
        }
        indicator.innerHTML = content;
        indicator.classList.remove('hidden');
    }

    /**
     * Hide the tool indicator when done
     */
    hideToolIndicator() {
        if (this.currentBubble) {
            const indicator = this.currentBubble.querySelector('.tool-indicator');
            if (indicator) {
                indicator.classList.add('hidden');
            }
        }
    }

    /**
     * Get icon for tool type
     * @param {string} toolName - Tool name
     * @returns {string} Icon emoji
     */
    getToolIcon(toolName) {
        const icons = {
            'Bash': '\u26A1',
            'Read': '\uD83D\uDCC4',
            'Edit': '\u270F\uFE0F',
            'Write': '\uD83D\uDCBE',
            'Grep': '\uD83D\uDD0D',
            'Glob': '\uD83D\uDDC2\uFE0F',
            'Task': '\uD83E\uDD16',
            'WebSearch': '\uD83C\uDF10',
            'WebFetch': '\uD83C\uDF0D'
        };
        return icons[toolName] || '\uD83D\uDD27';
    }

    // =========================================================================
    // Rendering
    // =========================================================================

    /**
     * Highlight diff content
     * @param {HTMLElement} codeBlock - Code block element
     */
    highlightDiff(codeBlock) {
        const text = codeBlock.textContent;
        const lines = text.split('\n');

        const highlightedLines = lines.map(line => {
            if (line.startsWith('+++')) {
                return `<span style="color: #bd93f9; font-weight: bold;">${this.escapeHtml(line)}</span>`;
            } else if (line.startsWith('---')) {
                return `<span style="color: #ff79c6; font-weight: bold;">${this.escapeHtml(line)}</span>`;
            } else if (line.startsWith('+')) {
                return `<span style="color: #50fa7b; background: rgba(80, 250, 123, 0.1); display: inline-block; width: 100%;">${this.escapeHtml(line)}</span>`;
            } else if (line.startsWith('-')) {
                return `<span style="color: #ff5555; background: rgba(255, 85, 85, 0.1); display: inline-block; width: 100%;">${this.escapeHtml(line)}</span>`;
            } else if (line.startsWith('@@')) {
                return `<span style="color: #8be9fd; font-weight: bold;">${this.escapeHtml(line)}</span>`;
            } else {
                return this.escapeHtml(line);
            }
        });

        codeBlock.innerHTML = highlightedLines.join('\n');
    }

    /**
     * Render output buffer to bubble
     */
    renderOutput() {
        const bubbleText = this.currentBubble?.querySelector('.bubble-text');
        if (!bubbleText) return;

        let html = '';

        // Show thinking in enhanced collapsible section with cycling words
        if (this.thinkingBuffer) {
            const wordCount = this.thinkingBuffer.split(/\s+/).filter(w => w.length > 0).length;
            const isActive = this.isStreaming;
            const activeClass = isActive ? 'reasoning-active' : 'reasoning-complete';

            html += `<details class="reasoning-panel ${activeClass}">
                <summary class="reasoning-header">
                    <span class="reasoning-icon">${isActive ? '🧠' : '✓'}</span>
                    <span class="reasoning-label">${isActive ? 'Reasoning' : 'Reasoning Complete'}</span>
                    <span class="reasoning-stats">${wordCount} words</span>
                    <span class="reasoning-toggle">▶</span>
                </summary>
                <div class="reasoning-content"><pre>${this.escapeHtml(this.thinkingBuffer)}</pre></div>
            </details>`;
        }

        // Render main content as markdown
        if (this.outputBuffer) {
            const parsed = marked.parse(this.outputBuffer);
            html += typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(parsed) : parsed;
        }

        bubbleText.innerHTML = html;

        // Start cycling animation if reasoning is active
        if (this.thinkingBuffer && this.isStreaming) {
            this.startReasoningCycle(bubbleText);
        }

        // Only apply syntax highlighting when NOT streaming (prevents jitter)
        if (!this.isStreaming) {
            // Apply syntax highlighting
            bubbleText.querySelectorAll('pre code').forEach((block) => {
                if (block.classList.contains('language-diff')) {
                    this.highlightDiff(block);
                } else if (window.hljs) {
                    hljs.highlightElement(block);
                }
            });

            // Apply custom text highlighting
            if (typeof applySyntaxHighlighting === 'function') {
                applySyntaxHighlighting(bubbleText);
            }

            // Make snapshot IDs clickable
            if (typeof makeSnapshotsClickable === 'function') {
                makeSnapshotsClickable(bubbleText);
            }
        }

        // Scroll to bottom only if user is near bottom
        scrollToBottomIfNeeded();

        // Save current state
        this.saveStreamingState();
    }

    /**
     * Debounced save of streaming state to history
     */
    saveStreamingState() {
        if (this._saveStateTimer) {
            clearTimeout(this._saveStateTimer);
        }

        this._saveStateTimer = setTimeout(() => {
            this._saveToHistory();
        }, 500);
    }

    /**
     * Immediately save current state to history
     */
    _saveToHistory() {
        if (!this.currentBubble) return;

        const bubbleText = this.currentBubble.querySelector('.bubble-text');
        if (!bubbleText) return;

        const historyData = getHistoryData();
        for (let i = historyData.length - 1; i >= 0; i--) {
            if (historyData[i].role === 'assistant') {
                const content = historyData[i].content || '';
                if (!content || content.includes('agent-loading') || historyData[i].isAgent || historyData[i].isStreaming) {
                    historyData[i].content = bubbleText.innerHTML;
                    historyData[i].isAgent = true;
                    historyData[i].isStreaming = this.isStreaming;
                    historyData[i].speakableText = this.speakableBuffer || '';
                    setHistoryData(historyData);
                    saveHistory();
                    break;
                }
            }
        }
    }

    /**
     * Finalize output after completion
     */
    finalizeOutput() {
        // Stop reasoning animation
        this.stopReasoningCycle();

        // Hide the thinking-budget indicator on turn completion. updateThinkingIndicator
        // only re-shows it while actively thinking, but nothing re-hid it — so it (and
        // the generation ember backdrop, which keys off #thinkingIndicator:not(.hide))
        // would otherwise linger for the rest of the session.
        $('thinkingIndicator')?.classList.add('hide');

        // Hide tool indicator
        this.hideToolIndicator();

        this.renderOutput();

        // Update reasoning panel to complete state
        if (this.currentBubble) {
            const reasoningPanel = this.currentBubble.querySelector('.reasoning-panel');
            if (reasoningPanel) {
                reasoningPanel.classList.remove('reasoning-active');
                reasoningPanel.classList.add('reasoning-complete');
                const icon = reasoningPanel.querySelector('.reasoning-icon');
                const label = reasoningPanel.querySelector('.reasoning-label');
                if (icon) icon.textContent = '✓';
                if (label) label.textContent = 'Reasoning Complete';
            }
        }

        // Auto-collapse all file/edit/tool result sections
        if (this.currentBubble) {
            const collapsibles = this.currentBubble.querySelectorAll(
                'details.file-result-collapsible, details.edit-diff-collapsible, details.tool-result-collapsible'
            );
            collapsibles.forEach(details => {
                details.removeAttribute('open');
            });
        }

        if (this.currentBubble && this.speakableBuffer) {
            const existingControls = this.currentBubble.querySelector('.bubble-controls');
            if (existingControls) existingControls.remove();

            const controlsBar = document.createElement('div');
            controlsBar.className = 'bubble-controls';

            const speakableText = this.speakableBuffer;
            const bubble = this.currentBubble;

            // TTS play button
            const playBtn = document.createElement('button');
            playBtn.className = 'bubble-btn speak-btn';
            playBtn.title = 'Listen';
            playBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
            </svg>`;
            playBtn.onclick = () => speakToBubble(speakableText, bubble, playBtn);
            controlsBar.appendChild(playBtn);

            // Copy button
            const copyBtn = document.createElement('button');
            copyBtn.className = 'bubble-btn copy-btn';
            copyBtn.title = 'Copy text';
            copyBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>`;
            copyBtn.onclick = () => {
                navigator.clipboard.writeText(speakableText).then(() => {
                    toast('Copied to clipboard');
                    copyBtn.classList.add('copied');
                    setTimeout(() => copyBtn.classList.remove('copied'), 2000);
                });
            };
            controlsBar.appendChild(copyBtn);

            this.currentBubble.appendChild(controlsBar);

            // Update lastAssistantText for main speaker button
            setLastAssistantText(speakableText);

            // Trigger Auto-TTS if enabled
            if (window.triggerAutoTTS && speakableText) {
                window.triggerAutoTTS(speakableText, this.currentBubble);
            }

            // Save to history
            const bubbleText = this.currentBubble.querySelector('.bubble-text');
            if (bubbleText) {
                const historyData = getHistoryData();
                for (let i = historyData.length - 1; i >= 0; i--) {
                    if (historyData[i].role === 'assistant') {
                        const content = historyData[i].content || '';
                        if (!content || content.includes('agent-loading') || historyData[i].isAgent || historyData[i].isStreaming) {
                            historyData[i].content = bubbleText.innerHTML;
                            historyData[i].isAgent = true;
                            historyData[i].isStreaming = false;
                            historyData[i].speakableText = speakableText;
                            break;
                        }
                    }
                }
                setHistoryData(historyData);
                saveHistory();
            }
        }

        if (this._saveStateTimer) {
            clearTimeout(this._saveStateTimer);
            this._saveStateTimer = null;
        }

        this.thinkingBuffer = '';
    }

    // =========================================================================
    // Quick Actions
    // =========================================================================

    /**
     * Detect and show quick action buttons
     */
    detectAndShowQuickActions() {
        const existingActions = this.currentBubble?.querySelector('.agent-quick-actions');
        if (existingActions) existingActions.remove();

        const text = this.outputBuffer.toLowerCase();

        if (text.includes('trust') || text.includes('yes, proceed') || text.includes('no, exit')) {
            this.showQuickActions([
                { label: '1 - Yes, proceed', value: '1' },
                { label: '2 - No, exit', value: '2' },
                { label: '\u21B5 Enter', value: '' }
            ]);
            return;
        }

        if (text.includes('(y/n)') || text.includes('[y/n]')) {
            this.showQuickActions([
                { label: 'Y - Yes', value: 'y' },
                { label: 'N - No', value: 'n' }
            ]);
            return;
        }

        if (text.includes('allow') && (text.includes('edit') || text.includes('write') || text.includes('bash'))) {
            this.showQuickActions([
                { label: '\u2713 Allow', value: 'y' },
                { label: '\u2715 Deny', value: 'n' }
            ]);
            return;
        }
    }

    /**
     * Show quick action buttons
     * @param {Array} actions - Array of {label, value} objects
     */
    showQuickActions(actions) {
        if (!this.currentBubble) return;

        const div = document.createElement('div');
        div.className = 'agent-quick-actions';

        actions.forEach(action => {
            const btn = document.createElement('button');
            btn.className = 'agent-quick-btn';
            btn.textContent = action.label;
            btn.onclick = () => {
                console.log('[Agent] Quick action clicked:', action.value);
                this.sendInput(action.value);
                div.classList.add('action-sent');
                div.querySelectorAll('button').forEach(b => b.disabled = true);
            };
            div.appendChild(btn);
        });

        this.currentBubble.appendChild(div);

        scrollToBottomIfNeeded();
    }

    // =========================================================================
    // Permission Handling
    // =========================================================================

    /**
     * Show permission request buttons
     * @param {Object} perm - Permission request data
     */
    showPermissionButtons(perm) {
        this.activePermissionDialog = null;

        const div = document.createElement('div');
        div.className = 'permission-dialog';
        div.tabIndex = 0;

        const icons = {
            'bash': '\u26A1',
            'edit': '\u270F\uFE0F',
            'write': '\uD83D\uDCDD',
            'read': '\uD83D\uDCD6',
            'default': '\uD83D\uDD27'
        };
        const icon = icons[perm.action_type] || icons['default'];

        let detailsHtml = '';
        if (perm.input) {
            if (perm.input.command) {
                const cmd = perm.input.command;
                const truncated = cmd.length > 100 ? cmd.substring(0, 100) + '...' : cmd;
                detailsHtml = `<pre class="permission-details">${this.escapeHtml(truncated)}</pre>`;
            } else if (perm.input.file_path) {
                detailsHtml = `<span class="permission-file">${this.escapeHtml(perm.input.file_path)}</span>`;
            }
        }

        div.innerHTML = `
            <div class="permission-header">
                <span class="permission-icon">${icon}</span>
                <span class="permission-title">Permission Required</span>
                <span class="permission-tool">${this.escapeHtml(perm.tool || '')}</span>
            </div>
            <p class="permission-prompt">${this.escapeHtml(perm.prompt_text)}</p>
            ${detailsHtml}
            <div class="permission-buttons">
                <button class="btn btn-allow" data-key="1">
                    <span class="btn-key">1</span>
                    <span class="btn-icon">\u2713</span>
                    <span class="btn-text">Allow</span>
                </button>
                <button class="btn btn-deny" data-key="2">
                    <span class="btn-key">2</span>
                    <span class="btn-icon">\u2715</span>
                    <span class="btn-text">Deny</span>
                </button>
            </div>
            <p class="permission-hint">Press <kbd>1</kbd>/<kbd>y</kbd> to allow, <kbd>2</kbd>/<kbd>n</kbd> to deny</p>
        `;

        const allowBtn = div.querySelector('.btn-allow');
        const denyBtn = div.querySelector('.btn-deny');

        const handleAllow = () => {
            if (!div.parentNode) return;
            this.sendPermissionResponse(true);
            div.classList.add('permission-approved');
            this.removePermissionKeyboardHandler();
            setTimeout(() => div.remove(), 300);
        };

        const handleDeny = () => {
            if (!div.parentNode) return;
            this.sendPermissionResponse(false);
            div.classList.add('permission-denied');
            this.removePermissionKeyboardHandler();
            setTimeout(() => div.remove(), 300);
        };

        allowBtn.onclick = handleAllow;
        denyBtn.onclick = handleDeny;

        this.permissionKeyboardHandler = (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            if (e.key === '1' || e.key === 'y' || e.key === 'Y') {
                e.preventDefault();
                handleAllow();
            } else if (e.key === '2' || e.key === 'n' || e.key === 'N') {
                e.preventDefault();
                handleDeny();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (document.activeElement === allowBtn) {
                    handleAllow();
                } else if (document.activeElement === denyBtn) {
                    handleDeny();
                } else {
                    handleAllow();
                }
            } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                if (document.activeElement === allowBtn) {
                    denyBtn.focus();
                } else {
                    allowBtn.focus();
                }
            }
        };

        document.addEventListener('keydown', this.permissionKeyboardHandler);
        this.activePermissionDialog = div;

        if (this.currentBubble) {
            this.currentBubble.appendChild(div);
            setTimeout(() => allowBtn.focus(), 50);
            scrollToBottomIfNeeded();
        }
    }

    /**
     * Remove permission keyboard handler
     */
    removePermissionKeyboardHandler() {
        if (this.permissionKeyboardHandler) {
            document.removeEventListener('keydown', this.permissionKeyboardHandler);
            this.permissionKeyboardHandler = null;
        }
        this.activePermissionDialog = null;
    }

    /**
     * Send permission response
     * @param {boolean} approved - Whether permission was approved
     */
    sendPermissionResponse(approved) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({
                type: 'permission_response',
                approved: approved
            }));
            console.log(`[Agent] Permission ${approved ? 'approved' : 'denied'}`);
        }
    }

    // =========================================================================
    // Input Sending
    // =========================================================================

    /**
     * Send follow-up input to continue the conversation
     * @param {string} text - Input text
     */
    async sendInput(text) {
        console.log(`[Agent] sendInput called with: "${text}"`);
        console.log(`[Agent] WebSocket state: ${this.ws ? this.ws.readyState : 'null'}, hasActiveSession=${this.hasActiveSession}`);

        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            if (this.hasActiveSession) {
                // Initialize todo tracker and status line (same as startSession)
                todoTracker.init('todoPanel', this.sessionId);
                statusLine.init('agentStatusLine');

                this.outputBuffer = '';
                this.thinkingBuffer = '';
                this.speakableBuffer = '';
                this.isStreaming = true;
                this.messageCount++;

                this.currentBubble = addBubble('assistant', '');
                this.currentBubble.classList.add('agent-streaming');
                const bubbleText = this.currentBubble.querySelector('.bubble-text');
                if (bubbleText) {
                    bubbleText.innerHTML = '<div class="agent-loading">Continuing conversation...</div>';
                }

                this.updateBannerStatus('Continuing...', true);

                const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${wsProtocol}//${window.location.host}/ws/agent/${this.sessionId}`;

                try {
                    this.ws = new WebSocket(wsUrl);
                    this.ws.onopen = () => {
                        this.reconnectAttempts = 0;
                        console.log('[Agent] Reconnected, sending follow-up prompt');
                        const claudeModelSelect = $('claudeModelSelect');
                        const selectedModel = claudeModelSelect ? claudeModelSelect.value : (window.__claudeModel || 'sonnet');

                        // Start status line session on reconnect
                        statusLine.startSession(this.sessionId, selectedModel);

                        const skipPerms = true;
                        this.ws.send(JSON.stringify({
                            type: 'prompt',
                            text: text,
                            operator: window.__operator || 'Brandon',
                            model: selectedModel,
                            skip_permissions: skipPerms
                        }));
                    };
                    this.ws.onmessage = (e) => {
                        try {
                            this.handleMessage(JSON.parse(e.data));
                        } catch (err) {
                            console.error('[Agent] Parse error:', err);
                        }
                    };
                    this.ws.onclose = (e) => {
                        console.log('[Agent] WebSocket closed after follow-up:', e.code);
                        if (e.code !== 1000 && this.sessionId) {
                            this.attemptReconnect();
                        } else {
                            this.isStreaming = false;
                            statusLine.endSession();
                        }
                    };
                    this.ws.onerror = (err) => {
                        console.error('[Agent] WebSocket error:', err);
                        this.isStreaming = false;
                        statusLine.endSession();
                    };
                } catch (err) {
                    console.error('[Agent] Reconnect failed:', err);
                    toast('Failed to continue session');
                }
                return;
            } else {
                console.error('[Agent] No active session to continue');
                toast('No active session - start a new conversation');
                return;
            }
        }

        this.outputBuffer = '';
        this.thinkingBuffer = '';
        this.speakableBuffer = '';
        this.isStreaming = true;
        this.messageCount++;

        this.currentBubble = addBubble('assistant', '');
        this.currentBubble.classList.add('agent-streaming');
        const bubbleText = this.currentBubble.querySelector('.bubble-text');
        if (bubbleText) {
            bubbleText.innerHTML = '<div class="agent-loading">Continuing conversation...</div>';
        }

        const message = {
            type: 'input',
            text: text
        };
        console.log('[Agent] Sending input message:', message);
        this.ws.send(JSON.stringify(message));
        this.updateBannerStatus('Continuing...', true);
    }

    // =========================================================================
    // Session State
    // =========================================================================

    /**
     * Check if agent session is active
     * @returns {boolean} Whether session is active
     */
    isActive() {
        const wsOpen = this.ws && this.ws.readyState === WebSocket.OPEN;
        const active = wsOpen || this.hasActiveSession;
        console.log(`[Agent] isActive() = ${active}, hasActiveSession=${this.hasActiveSession}, wsOpen=${wsOpen}`);
        return active;
    }

    /**
     * End the current session
     */
    endSession() {
        console.log('[Agent] Ending session:', this.sessionId);

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'end_session' }));
        }

        this.hasActiveSession = false;
        this.sessionId = null;
        this.messageCount = 0;
        this.updateBannerStatus('Ready');
        toast('Session ended. Start a new conversation.');
    }

    /**
     * Disconnect WebSocket
     */
    disconnect() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.isStreaming = false;
    }

    // =========================================================================
    // App Panel
    // =========================================================================

    /**
     * Add app to running apps panel
     * @param {Object} app - App data
     */
    addAppToPanel(app) {
        const menuAppsList = $('menuRunningApps');

        if (!menuAppsList) return;

        const noAppsMsg = menuAppsList.querySelector('.no-apps-message');
        if (noAppsMsg) noAppsMsg.remove();

        if (menuAppsList.querySelector(`[data-port="${app.port}"]`)) return;

        const item = document.createElement('div');
        item.className = 'menu-app-item';
        item.dataset.port = app.port;
        item.innerHTML = `
            <span class="app-name">${this.escapeHtml(app.name)}</span>
            <span class="app-port">:${app.port}</span>
            <button class="btn btn-open-app">Open</button>
        `;

        item.querySelector('.btn-open-app').onclick = () => {
            this.openAppPreview(app.port, app.name);
        };

        menuAppsList.appendChild(item);
    }

    /**
     * Open app preview modal
     * @param {number} port - App port
     * @param {string} name - App name
     */
    openAppPreview(port, name) {
        const modal = $('appPreviewModal');
        const frame = $('appPreviewFrame');
        const title = $('appPreviewTitle');

        if (!modal || !frame) return;

        title.textContent = `\uD83D\uDE80 ${name || 'App Preview'}`;
        frame.src = `/app-proxy/${port}/`;
        modal.classList.remove('hide');
    }

    /**
     * Handle completed message
     */
    handleCompleted() {
        this.isStreaming = false;
        if (this.currentBubble) {
            this.currentBubble.classList.remove('agent-streaming');
            const controls = this.currentBubble.querySelector('.bubble-controls');
            if (!controls) {
                const newControls = document.createElement('div');
                newControls.className = 'bubble-controls';

                const playBtn = document.createElement('button');
                playBtn.className = 'bubble-btn speak-btn';
                playBtn.title = 'Listen';
                playBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                    <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                    <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
                </svg>`;
                playBtn.onclick = () => speakToBubble(this.outputBuffer, this.currentBubble, playBtn);
                newControls.appendChild(playBtn);

                const copyBtn = document.createElement('button');
                copyBtn.className = 'bubble-btn copy-btn';
                copyBtn.title = 'Copy text';
                copyBtn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                </svg>`;
                copyBtn.onclick = () => {
                    navigator.clipboard.writeText(this.outputBuffer).then(() => {
                        toast('Copied to clipboard');
                        copyBtn.classList.add('copied');
                        setTimeout(() => copyBtn.classList.remove('copied'), 2000);
                    });
                };
                newControls.appendChild(copyBtn);

                this.currentBubble.appendChild(newControls);
            }
        }
    }

    /**
     * Reconnect to existing session
     * @param {Object} sessionData - Session data from server
     */
    async reconnect(sessionData) {
        this.sessionId = sessionData.session_id;
        this.hasActiveSession = true;

        // Initialize todo tracker and status line for reconnection
        todoTracker.init('todoPanel', this.sessionId);
        statusLine.init('agentStatusLine');

        const rawLines = sessionData.output_buffer || [];
        let reconstructedText = '';
        for (const line of rawLines) {
            try {
                const event = JSON.parse(line);
                if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
                    reconstructedText += event.delta.text || '';
                } else if (event.type === 'assistant' && event.message?.content) {
                    for (const block of event.message.content) {
                        if (block.type === 'text') {
                            reconstructedText += block.text || '';
                        }
                    }
                }
            } catch (e) {
                // Not JSON, might be raw output
            }
        }
        this.outputBuffer = reconstructedText;
        this.speakableBuffer = reconstructedText;

        this.currentBubble = addBubble('assistant', '');
        if (this.outputBuffer) {
            const bubbleText = this.currentBubble.querySelector('.bubble-text');
            if (bubbleText) {
                const parsed = marked.parse(this.outputBuffer);
                bubbleText.innerHTML = typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(parsed) : parsed;
            }
        }

        if (sessionData.apps && sessionData.apps.length > 0) {
            sessionData.apps.forEach(app => this.addAppToPanel(app));
        }

        if (sessionData.status === 'running') {
            this.currentBubble.classList.add('agent-streaming');
            this.isStreaming = true;

            console.log('[Agent] Session still running, reconnecting WebSocket...');

            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${location.host}/ws/agent/${this.sessionId}`;

            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.reconnectAttempts = 0;
                console.log('[Agent] Reconnected to running session');
                // Start status line on reconnect
                const claudeModelSelect = $('claudeModelSelect');
                const selectedModel = claudeModelSelect ? claudeModelSelect.value : (window.__claudeModel || 'sonnet');
                statusLine.startSession(this.sessionId, selectedModel);

                this.ws.send(JSON.stringify({
                    type: 'reconnect',
                    operator: this.currentOperator
                }));
                this.updateBannerStatus('Reconnected - Streaming');
                toast('Reconnected to running agent session', 2000);
            };

            this.ws.onmessage = (event) => {
                this.handleMessage(JSON.parse(event.data));
            };

            this.ws.onerror = (err) => {
                console.error('[Agent] Reconnect WebSocket error:', err);
            };

            this.ws.onclose = (e) => {
                console.log('[Agent] Reconnect WebSocket closed:', e.code);
                if (e.code !== 1000 && this.sessionId) {
                    this.attemptReconnect();
                } else {
                    this.isStreaming = false;
                    this.currentBubble?.classList.remove('agent-streaming');
                    statusLine.endSession();
                }
            };
        } else {
            this.updateBannerStatus('Session Ready');
        }

        console.log('[Agent] Restored session:', this.sessionId, 'status:', sessionData.status);
    }

    /**
     * Legacy method for backwards compatibility
     * @param {string} text - Output text
     */
    appendOutput(text) {
        this.appendRawOutput(text);
    }

    /**
     * Escape HTML characters
     * @param {string} text - Text to escape
     * @returns {string} Escaped text
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// =============================================================================
// Global Instance
// =============================================================================

/** Global agent handler instance */
let agentHandler = null;

/**
 * Get the agent handler instance
 * @returns {AgentChatHandler|null} Agent handler
 */
export function getAgentHandler() {
    return agentHandler;
}

/**
 * Set the agent handler instance
 * @param {AgentChatHandler} handler - Handler to set
 */
export function setAgentHandler(handler) {
    agentHandler = handler;
    window.agentHandler = handler;
}

// =============================================================================
// Session Initialization
// =============================================================================

/**
 * Check for existing agent session on page load
 * Restores the last session ID per operator from localStorage
 */
export async function checkExistingAgentSession() {
    const operator = window.__operator || 'Brandon';

    const persistedSessionId = getOperatorSession(operator);
    if (persistedSessionId) {
        console.log('[Agent] Found persisted session ID for operator:', operator, persistedSessionId);
    }

    try {
        const resp = await fetch(`/agent/session/${operator}`);
        if (resp.ok) {
            const data = await resp.json();
            if (data.session && data.session.status !== 'completed') {
                console.log('[Agent] Found active backend session:', data.session.session_id);
                agentHandler = new AgentChatHandler();
                window.agentHandler = agentHandler;
                agentHandler.currentOperator = operator;
                await agentHandler.reconnect({
                    ...data.session,
                    output_buffer: data.output_buffer || []
                });
                saveOperatorSession(operator, data.session.session_id);
                return;
            }
        }
    } catch (e) {
        console.log('[Agent] Backend session check failed:', e.message);
    }

    if (persistedSessionId) {
        console.log('[Agent] Pre-loading persisted session ID for operator:', operator);
        agentHandler = new AgentChatHandler();
        window.agentHandler = agentHandler;
        agentHandler.currentOperator = operator;
        agentHandler.sessionId = persistedSessionId;
        agentHandler.hasActiveSession = true;
        agentHandler.updateBannerStatus('Session Ready');
        toast(`Restored session for ${operator}. Your context is preserved.`, 3000);
    }
}

// =============================================================================
// App Preview Modal Initialization
// =============================================================================

/**
 * Initialize app preview modal close button
 */
export function initAppPreviewModal() {
    const closeBtn = $('btnCloseAppPreview');
    if (closeBtn) {
        closeBtn.onclick = () => {
            $('appPreviewModal')?.classList.add('hide');
            const frame = $('appPreviewFrame');
            if (frame) frame.src = '';
        };
    }
}

/**
 * Load registered apps from backend
 */
export async function loadRegisteredApps() {
    try {
        const res = await fetch('/agent/apps');
        const data = await res.json();
        if (data.apps && data.apps.length > 0) {
            console.log(`[Apps] Loading ${data.apps.length} registered apps`);
            const currentOperator = getOperator();
            const operatorApps = currentOperator
                ? data.apps.filter(app => app.operator === currentOperator || app.operator === 'system')
                : data.apps;

            operatorApps.forEach(app => {
                addAppToMenuPanel(app);
            });
            if (operatorApps.length > 0) {
                console.log(`[Apps] Loaded ${operatorApps.length} apps for ${currentOperator || 'all operators'}`);
            }
        }
    } catch (err) {
        console.error('[Apps] Failed to load registered apps:', err);
    }
}

/**
 * Add app to menu panel (works without agentHandler)
 * @param {Object} app - App data
 */
function addAppToMenuPanel(app) {
    const menuAppsList = $('menuRunningApps');
    if (!menuAppsList) return;

    const noAppsMsg = menuAppsList.querySelector('.no-apps-message');
    if (noAppsMsg) noAppsMsg.remove();

    if (menuAppsList.querySelector(`[data-port="${app.port}"]`)) return;

    const item = document.createElement('div');
    item.className = 'menu-app-item';
    item.dataset.port = app.port;
    item.innerHTML = `
        <span class="app-name">${escapeHtmlSimple(app.name)}</span>
        <span class="app-port">:${app.port}</span>
        <button class="btn btn-open-app">Open</button>
    `;

    item.querySelector('.btn-open-app').onclick = () => {
        openAppPreviewSimple(app.port, app.name);
    };

    menuAppsList.appendChild(item);
}

/**
 * Simple HTML escape
 * @param {string} text - Text to escape
 * @returns {string} Escaped text
 */
function escapeHtmlSimple(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Simple app preview opener
 * @param {number} port - App port
 * @param {string} name - App name
 */
function openAppPreviewSimple(port, name) {
    const modal = $('appPreviewModal');
    const frame = $('appPreviewFrame');
    const title = $('appPreviewTitle');

    if (!modal || !frame) return;

    title.textContent = `\uD83D\uDE80 ${name || 'App Preview'}`;
    frame.src = `/app-proxy/${port}/`;
    modal.classList.remove('hide');
}

/**
 * Initialize agent handler on DOM ready
 */
export function initAgentHandler() {
    initAppPreviewModal();
    loadRegisteredApps();
    checkExistingAgentSession();
}
