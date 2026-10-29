/**
 * agent-handler.js
 * Claude Code Agent WebSocket handler
 * Manages agent sessions, streaming, and tool execution display
 *
 * Source: Portal/app.js lines 8411-9959
 * Refactored: 2025-12-20
 */

import { $, toast, escapeHtml } from './core-utils.js';
import { getOperator, saveOperatorSession, getOperatorSession } from './state-management.js';
import { addBubble } from './chat-bubbles.js';
import { markdownToSafeHtml, markdownCodeSpan } from './markdown-renderer.js';
import { scrollToBottomIfNeeded } from './ui-setup.js';
import { todoTracker } from './todo-tracker.js';
import { statusLine } from './status-line.js';
import {
    BaseAgentHandler, bindBannerToggle, probeOncePerOperator, restoreForOperatorSwitch,
} from './base-agent-handler.js';
import {
    PERMISSION_MODE_INFO,
    nextPermissionMode,
    permissionPayload,
    loadPermissionMode,
    savePermissionMode,
} from './agent-permission-mode.js';

/** localStorage, or null where reading it throws (private window, blocked site data). */
function permissionStorage() {
    try {
        return window.localStorage;
    } catch (_) {
        return null;
    }
}

// =============================================================================
// AgentChatHandler Class
// =============================================================================

/** What sets the Claude Code agent apart (base-agent-handler.js) */
const CLAUDE_AGENT = {
    logPrefix: '[Agent]',
    name: 'Claude Code',
    connectFailed: 'Failed to connect to Claude Code CLI',
    wsPath: '/ws/agent/',
    sessionSuffix: '',
    historyFlag: 'isAgent',
    skipBlankDeltas: false,
    toasts: {
        sessionCleared: 'Session cleared. Ready for new prompt.',
        sessionEnded: 'Session ended. Start a new conversation.',
        endedByServer: 'Session ended',
        error: 'Agent error',
        connectionError: 'Agent connection error - check console',
        continueFailed: 'Failed to continue session',
    },
    ids: {
        endSession: 'endAgentSession',
        newSession: 'newAgentSession',
        snapshot: 'devSnapshotBtn',
        confirmModal: 'confirmNewSessionModal',
        banner: 'claudeCodeBanner',
        bannerCollapsed: 'claudeBannerCollapsed',
        bannerCollapseBtn: 'bannerCollapseBtn',
        status: 'claudeAgentStatus',
        statusCompact: 'claudeStatusCompact',
    },
};

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

/**
 * Handler for Claude Code agent WebSocket communication: the shared turn and
 * rendering machinery (BaseAgentHandler) plus reconnection, the permission
 * mode, the thinking budget and reasoning panel, todos and the status line,
 * file/edit results, permission prompts, quick actions and detected apps.
 */
export class AgentChatHandler extends BaseAgentHandler {
    /** The Claude Code agent's description (BaseAgentHandler) */
    get agent() {
        return CLAUDE_AGENT;
    }

    constructor() {
        super();
        this.permissionKeyboardHandler = null;
        this.activePermissionDialog = null;
        // Thinking budget tracking
        this.thinkingTokens = 0;
        this.thinkingBudget = 31999;  // Claude's max thinking budget
        this.permissionMode = loadPermissionMode(permissionStorage());  // 'yolo' | 'plan'
        // Reasoning animation
        this.reasoningCycleInterval = null;
        this.reasoningPhraseIndex = 0;
        // Reconnection state
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 15;
        this.reconnectTimer = null;
        // Through the base's signal, so dispose() removes them: a second
        // instance's copies cycled the permission mode twice per Shift+Tab
        // (audit P03-live-agents-web-04).
        const signal = this._listeners.signal;

        // Setup Permission Mode click handler
        const permissionIndicator = $('permissionModeIndicator');
        if (permissionIndicator) {
            permissionIndicator.addEventListener('click', () => this.cyclePermissionMode(), { signal });
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
        }, { signal });

        // Initialize permission mode indicator
        this.updatePermissionModeIndicator();
    }

    /**
     * Retire this instance (BaseAgentHandler.dispose) and its reasoning animation.
     */
    dispose() {
        super.dispose();
        this.stopReasoningCycle();
    }

    /**
     * A failed turn also stops the reasoning animation, the thinking-budget
     * indicator and the status line's session (BaseAgentHandler._finalizeFailure).
     * @param {string} [message] - Shown where the loader was, if no reply came
     */
    _finalizeFailure(message) {
        this.stopReasoningCycle();
        $('thinkingIndicator')?.classList.add('hide'); // don't leave embers pinned on
        statusLine.endSession();
        super._finalizeFailure(message);
    }

    // =========================================================================
    // Turn hooks (BaseAgentHandler)
    // =========================================================================

    /** The model picked in the banner */
    _selectedModel() {
        const claudeModelSelect = $('claudeModelSelect');
        return claudeModelSelect ? claudeModelSelect.value : (window.__claudeModel || 'sonnet');
    }

    /** A new session clears the todo tracker and the status line */
    _onNewSession() {
        todoTracker.clear();
        statusLine.reset();
    }

    /** The todo tracker and status line follow the session (and cache their DOM) */
    _attachSessionUi() {
        todoTracker.init('todoPanel', this.sessionId);
        statusLine.init('agentStatusLine');
    }

    /** An open socket resets the reconnect backoff and starts the status line */
    _onTurnOpen() {
        this.reconnectAttempts = 0;
        statusLine.startSession(this.sessionId, this._selectedModel());
    }

    /** An abnormal close of a live session reconnects with backoff */
    _reconnectsAfter(e) {
        return e.code !== 1000 && !!this.sessionId;
    }

    _promptMessage(prompt) {
        return {
            ...super._promptMessage(prompt),
            model: this._selectedModel(),
            // The banner's mode switch: plan = read-only, yolo = every tool approved.
            ...permissionPayload(this.permissionMode)
        };
    }

    _followUpMessage(text) {
        return { ...super._followUpMessage(text), model: this._selectedModel(), ...permissionPayload(this.permissionMode) };
    }

    _inputMessage(text) {
        // A mode switched since the last message applies to this one.
        return { ...super._inputMessage(text), ...permissionPayload(this.permissionMode) };
    }

    /**
     * Let go of the session (BaseAgentHandler._releaseSession), and of a
     * pending reconnect, which would otherwise attach the old session again.
     */
    _releaseSession() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        super._releaseSession();
    }

    /**
     * Attempt to reconnect WebSocket with exponential backoff
     */
    attemptReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.log('[Agent] Max reconnect attempts reached');
            this._finalizeFailure('Lost the connection to Claude Code.');
            this.updateBannerStatus('Connection Lost');
            return;
        }

        this.reconnectAttempts++;
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts - 1), 30000);
        console.log(`[Agent] Reconnecting (${this.reconnectAttempts}/${this.maxReconnectAttempts}) in ${delay}ms...`);
        this.updateBannerStatus(`Reconnecting (${this.reconnectAttempts})...`);

        this.reconnectTimer = setTimeout(() => {
            if (!this.sessionId) return;

            try {
                this.ws = new WebSocket(this._wsUrl());

                this.ws.onopen = () => {
                    console.log('[Agent] Reconnected successfully');
                    this.reconnectAttempts = 0;
                    this.updateBannerStatus('Reconnected', true);

                    this.ws.send(JSON.stringify({
                        type: 'reconnect',
                        operator: this.currentOperator || getOperator()
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
                        this._finalizeFailure();
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

    // =========================================================================
    // Message Handling
    // =========================================================================

    /**
     * The messages only Claude Code sends (the rest: BaseAgentHandler.handleMessage)
     * @param {Object} msg - Message object with type and data
     * @returns {boolean} Whether it was handled
     */
    _handleAgentMessage(msg) {
        switch(msg.type) {
            case 'assistant_message':
                // The complete message: after streamed deltas it is their duplicate.
                if (!this.hasReceivedStreamingContent) {
                    this.updateBannerStatus('Responding...', true);
                    this.appendContent(msg.data);
                }
                return true;
            case 'file_result':
                this.updateBannerStatus('File operation...', true);
                this.appendFileResult(msg.data);
                return true;
            case 'edit_diff':
                this.updateBannerStatus('Edit complete', true);
                this.appendEditDiff(msg.data);
                return true;
            case 'waiting':
                this.updateBannerStatus('Waiting for input', true);
                console.log('[Agent] Claude waiting for input');
                return true;
            case 'permission_request':
                this.updateBannerStatus('Permission Required', true);
                this.showPermissionButtons(msg.data);
                if (navigator.vibrate) {
                    navigator.vibrate([200, 100, 200]);
                }
                return true;
            case 'app_detected':
                console.log('[Agent] App detected:', msg.data);
                this.addAppToPanel(msg.data);
                toast(`App detected on port ${msg.data.port}`, 3000);
                return true;
            case 'todo_write':
                // Handle TodoWrite tool - update task tracker
                this.updateBannerStatus('Working...', true);
                todoTracker.updateTodos(msg.data);
                return true;
            case 'status_update':
                // Handle status updates (context/cost/model)
                statusLine.updateFromMessage(msg.data);
                return true;
            case 'image_task': {
                console.log('[Agent] Image generation task:', msg.data);
                const imgData = msg.data;
                import('./task-manager.js').then(({ taskManager }) => {
                    taskManager.addTask(imgData.task_id, 'image_generation', imgData.prompt, null, imgData.count);
                    console.log(`[Agent] Tracking image generation: ${imgData.task_id}`);
                });
                return true;
            }
            case 'video_task': {
                console.log('[Agent] Video generation task:', msg.data);
                const vidData = msg.data;
                import('./task-manager.js').then(({ taskManager }) => {
                    taskManager.addTask(vidData.task_id, 'video_generation', vidData.prompt, { duration: vidData.duration, resolution: vidData.resolution });
                    console.log(`[Agent] Tracking video generation: ${vidData.task_id}`);
                });
                return true;
            }
            case 'music_task': {
                console.log('[Agent] Music generation task:', msg.data);
                const musData = msg.data;
                import('./task-manager.js').then(({ taskManager }) => {
                    taskManager.addTask(musData.task_id, 'lyria_music', musData.prompt, null, musData.sample_count || 1);
                    console.log(`[Agent] Tracking music generation: ${musData.task_id}`);
                });
                return true;
            }
            default:
                return false;
        }
    }

    // =========================================================================
    // Thinking
    // =========================================================================

    /**
     * Thinking counts toward the spoken text and the thinking budget
     * @param {string} text - Thinking text
     */
    _onThinking(text) {
        this.speakableBuffer += text;

        // Estimate tokens (rough: ~4 chars per token for English)
        this.thinkingTokens = Math.round(this.thinkingBuffer.length / 4);
        this.updateThinkingIndicator();
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
     * The thinking section: a reasoning panel with cycling words
     * @returns {string} HTML
     */
    _thinkingHtml() {
        const wordCount = this.thinkingBuffer.split(/\s+/).filter(w => w.length > 0).length;
        const isActive = this.isStreaming;
        const activeClass = isActive ? 'reasoning-active' : 'reasoning-complete';

        return `<details class="reasoning-panel ${activeClass}">
                <summary class="reasoning-header">
                    <span class="reasoning-icon">${isActive ? '🧠' : '✓'}</span>
                    <span class="reasoning-label">${isActive ? 'Reasoning' : 'Reasoning Complete'}</span>
                    <span class="reasoning-stats">${wordCount} words</span>
                    <span class="reasoning-toggle">▶</span>
                </summary>
                <div class="reasoning-content"><pre>${this.escapeHtml(this.thinkingBuffer)}</pre></div>
            </details>`;
    }

    /**
     * Start the cycling animation while reasoning is active
     * @param {HTMLElement} bubbleText - The re-rendered bubble text
     */
    _onRendered(bubbleText) {
        if (this.thinkingBuffer && this.isStreaming) {
            this.startReasoningCycle(bubbleText);
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

    // =========================================================================
    // Permission Mode
    // =========================================================================

    /**
     * Toggle the permission mode (YOLO ⇄ Plan). It applies from the next message sent.
     */
    cyclePermissionMode() {
        this.permissionMode = nextPermissionMode(this.permissionMode);
        savePermissionMode(permissionStorage(), this.permissionMode);
        this.updatePermissionModeIndicator();

        toast(`Permission mode: ${PERMISSION_MODE_INFO[this.permissionMode].description}`);

        console.log(`[Agent] Permission mode changed to: ${this.permissionMode}`);
    }

    /**
     * Update the permission mode indicator display
     */
    updatePermissionModeIndicator() {
        const indicator = $('permissionModeIndicator');
        if (!indicator) return;

        const mode = PERMISSION_MODE_INFO[this.permissionMode] || PERMISSION_MODE_INFO.plan;

        // Update text
        indicator.textContent = mode.label;
        indicator.title = mode.description;

        // Update classes
        indicator.className = `permission-mode ${mode.className}`;
    }

    // =========================================================================
    // Tool, File and Edit Results
    // =========================================================================

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
        const summary = `${icon} <strong>${this.escapeHtml(fileName)}</strong> <span class="file-line-range">${this.escapeHtml(lineRange)}</span>`;
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
        let summary = `${icon} <strong>Edited ${this.escapeHtml(fileName)}</strong>`;
        if (lineCount > 1) {
            summary += ` <span class="edit-line-count">(${lineCount} lines)</span>`;
        }

        let details = `\n<details class="edit-diff-collapsible" open><summary>${summary}</summary>\n\n\`\`\`diff\n${diff}\n\`\`\`\n`;

        if (oldString && newString && oldString.length < 100 && newString.length < 100) {
            // Markdown code spans: marked escapes their text, so it goes in raw
            // (markdownCodeSpan also keeps backticks/newlines from breaking out).
            const before = markdownCodeSpan(oldString.substring(0, 50) + (oldString.length > 50 ? '...' : ''));
            const after = markdownCodeSpan(newString.substring(0, 50) + (newString.length > 50 ? '...' : ''));
            details += `\n> Changed: ${before} \u2192 ${after}\n`;
        }

        details += '</details>\n';
        this.outputBuffer += details;

        this.renderOutput();
    }

    // =========================================================================
    // Finalizing
    // =========================================================================

    /**
     * Stop the reasoning animation and hide the thinking-budget indicator on
     * turn completion. updateThinkingIndicator only re-shows it while actively
     * thinking, but nothing re-hid it — so it (and the generation ember
     * backdrop, which keys off #thinkingIndicator:not(.hide)) would otherwise
     * linger for the rest of the session.
     */
    _onFinalizing() {
        this.stopReasoningCycle();
        $('thinkingIndicator')?.classList.add('hide');
    }

    /**
     * The reasoning panel shows complete and the file/edit/tool result sections
     * collapse
     */
    _onFinalRendered() {
        if (!this.currentBubble) return;
        const reasoningPanel = this.currentBubble.querySelector('.reasoning-panel');
        if (reasoningPanel) {
            reasoningPanel.classList.remove('reasoning-active');
            reasoningPanel.classList.add('reasoning-complete');
            const icon = reasoningPanel.querySelector('.reasoning-icon');
            const label = reasoningPanel.querySelector('.reasoning-label');
            if (icon) icon.textContent = '✓';
            if (label) label.textContent = 'Reasoning Complete';
        }

        const collapsibles = this.currentBubble.querySelectorAll(
            'details.file-result-collapsible, details.edit-diff-collapsible, details.tool-result-collapsible'
        );
        collapsibles.forEach(details => {
            details.removeAttribute('open');
        });
    }

    // =========================================================================
    // Quick Actions
    // =========================================================================

    /** The terminal output may be asking a question: offer its answers */
    _onRawOutput() {
        this.detectAndShowQuickActions();
    }

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

        menuAppsList.appendChild(buildAppMenuItem(app, () => this.openAppPreview(app.port, app.name)));
    }

    /**
     * Open app preview modal
     * @param {number} port - App port
     * @param {string} name - App name
     */
    openAppPreview(port, name) {
        openAppPreviewModal(port, name);
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
                bubbleText.innerHTML = markdownToSafeHtml(this.outputBuffer);
            }
        }

        if (sessionData.apps && sessionData.apps.length > 0) {
            sessionData.apps.forEach(app => this.addAppToPanel(app));
        }

        if (sessionData.status === 'running') {
            this.currentBubble.classList.add('agent-streaming');
            this.isStreaming = true;

            console.log('[Agent] Session still running, reconnecting WebSocket...');

            this.ws = new WebSocket(this._wsUrl());

            this.ws.onopen = () => {
                this.reconnectAttempts = 0;
                console.log('[Agent] Reconnected to running session');
                // Start status line on reconnect
                statusLine.startSession(this.sessionId, this._selectedModel());

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
                    this._finalizeFailure();
                }
            };
        } else {
            this.updateBannerStatus('Session Ready');
        }

        console.log('[Agent] Restored session:', this.sessionId, 'status:', sessionData.status);
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
    if (agentHandler && agentHandler !== handler) agentHandler.dispose();
    agentHandler = handler;
    window.agentHandler = handler;
}

/**
 * The handler a restore goes into: the existing instance, never a second
 * one (each constructor registers document-level listeners; two instances
 * doubled Shift+Tab, the toasts and, on reconnect, every delta).
 * @param {string} operator - Whose session is being restored
 * @returns {AgentChatHandler}
 */
function handlerForRestore(operator) {
    if (!agentHandler) setAgentHandler(new AgentChatHandler());
    if (agentHandler.currentOperator && agentHandler.currentOperator !== operator) {
        agentHandler._releaseSession();
    }
    agentHandler.currentOperator = operator;
    return agentHandler;
}

// =============================================================================
// Session Initialization
// =============================================================================

/**
 * Check for existing agent session on page load
 * Restores the last session ID per operator from localStorage. Runs once the
 * operator is resolved (app-init awaits initOperatorSelector); with no operator
 * there is nobody's session to look for, so it neither probes nor restores.
 */
export const checkExistingAgentSession = probeOncePerOperator(_checkExistingAgentSession);

/** Whether a restore for `operator` is still wanted (no switch since it began). */
const stillOperator = (operator) => getOperator() === operator;

async function _checkExistingAgentSession(operator) {
    if (!operator || !stillOperator(operator)) return;
    // A turn that is streaming keeps its session; nothing to restore over it.
    if (agentHandler && agentHandler.isStreaming) return;

    const persistedSessionId = getOperatorSession(operator);
    if (persistedSessionId) {
        console.log('[Agent] Found persisted session ID for operator:', operator, persistedSessionId);
    }

    try {
        const resp = await fetch(`/agent/session/${encodeURIComponent(operator)}`);
        if (resp.ok) {
            const data = await resp.json();
            if (!stillOperator(operator)) return; // switched away mid-probe
            if (data.session && data.session.status !== 'completed') {
                console.log('[Agent] Found active backend session:', data.session.session_id);
                const handler = handlerForRestore(operator);
                const ws = handler.ws;
                const alreadyAttached = handler.sessionId === data.session.session_id
                    && ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING);
                if (!alreadyAttached) {
                    if (ws) handler._releaseSession(); // one socket per session, never two
                    handler.currentOperator = operator;
                    await handler.reconnect({
                        ...data.session,
                        output_buffer: data.output_buffer || []
                    });
                }
                saveOperatorSession(operator, data.session.session_id);
                return;
            }
        }
    } catch (e) {
        console.log('[Agent] Backend session check failed:', e.message);
    }

    if (persistedSessionId && stillOperator(operator)) {
        const handler = handlerForRestore(operator);
        if (handler.sessionId === persistedSessionId && handler.hasActiveSession) return; // already restored
        console.log('[Agent] Pre-loading persisted session ID for operator:', operator);
        handler.sessionId = persistedSessionId;
        handler.hasActiveSession = true;
        handler.updateBannerStatus('Session Ready');
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

    menuAppsList.appendChild(buildAppMenuItem(app, () => openAppPreviewModal(app.port, app.name)));
}

/**
 * Ask a registered app's own server whether it is up, through the proxy the
 * Open button uses. The registry's status is always "running" (a default
 * nothing ever updates), so the Apps tab listed dead apps as running (W23).
 * The proxy answers 503/504 when nothing listens on the port; 403 means it
 * refuses the port, which the operator cannot open either.
 * @param {number|string} port - The app's port
 * @param {object} [opts]
 * @param {number} [opts.timeoutMs=4000]
 * @returns {Promise<boolean>} Whether the app answered
 */
export async function probeAppPort(port, { timeoutMs = 4000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
        const r = await fetch(`/app-proxy/${encodeURIComponent(port)}/`, { cache: 'no-store', signal: ctrl.signal });
        return r.status < 500 && r.status !== 403;
    } catch (_) {
        return false;
    } finally {
        clearTimeout(timer);
    }
}

/**
 * One Apps-tab row: name, port, a live status (probed, never the registry's
 * claim) and the Open button.
 * @param {Object} app - Registry entry ({name, port})
 * @param {Function} onOpen - Open button handler
 * @returns {HTMLElement}
 */
function buildAppMenuItem(app, onOpen) {
    const item = document.createElement('div');
    item.className = 'menu-app-item';
    item.dataset.port = app.port;
    item.innerHTML = `
        <span class="app-name">${escapeHtml(app.name)}</span>
        <span class="app-port">:${escapeHtml(app.port)}</span>
        <span class="app-status">checking…</span>
        <button class="btn btn-open-app">Open</button>
    `;
    item.querySelector('.btn-open-app').onclick = onOpen;

    const statusEl = item.querySelector('.app-status');
    probeAppPort(app.port).then((up) => {
        statusEl.textContent = up ? 'running' : 'not running';
        statusEl.classList.add(up ? 'is-up' : 'is-down');
        item.classList.toggle('app-down', !up);
    });
    return item;
}

/**
 * Open the app preview modal on an app (through the sandboxed app proxy)
 * @param {number} port - App port
 * @param {string} name - App name
 */
function openAppPreviewModal(port, name) {
    const modal = $('appPreviewModal');
    const frame = $('appPreviewFrame');
    const title = $('appPreviewTitle');

    if (!modal || !frame) return;

    title.textContent = `\uD83D\uDE80 ${name || 'App Preview'}`;
    frame.src = `/app-proxy/${port}/`;
    modal.classList.remove('hide');
}

/**
 * Initialize agent handler on DOM ready. The session restore is NOT started here:
 * app-init runs checkExistingAgentSession once the operator is resolved (a second,
 * earlier run probed before the operator was known and raced the first).
 */
export function initAgentHandler() {
    bindBannerToggle(CLAUDE_AGENT);
    initAppPreviewModal();
    loadRegisteredApps();
    window.addEventListener('bbx:operator-changed', restoreAgentSessionForOperatorSwitch);
}

/**
 * An operator switch restores THAT operator's session: the previous operator's is let
 * go (not ended — it stays theirs to resume) and the new one's is looked up. A turn
 * that is still streaming keeps its operator; the next send switches instead.
 */
export async function restoreAgentSessionForOperatorSwitch() {
    await restoreForOperatorSwitch(agentHandler, checkExistingAgentSession);
}
