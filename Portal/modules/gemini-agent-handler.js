/**
 * gemini-agent-handler.js
 * Gemini CLI Agent WebSocket handler
 * Manages agent sessions, streaming, and tool execution display
 * Mirrors the Claude Code CLI integration pattern
 */

import { $, toast } from './core-utils.js';
import { getHistoryData, setHistoryData, saveHistory, getOperator, getOperatorSession, saveOperatorSession, clearOperatorSession } from './state-management.js';
import { addBubble } from './chat-bubbles.js';
import { speakToBubble, setLastAssistantText } from './tts-stt.js';
import { makeSnapshotsClickable, applySyntaxHighlighting } from './timeline-browser.js';
import { scrollToBottomIfNeeded, setStreamingState } from './ui-setup.js';

// =============================================================================
// GeminiAgentHandler Class
// =============================================================================

/**
 * Handler for Gemini CLI agent WebSocket communication
 * Manages streaming output, tool execution display, and session persistence
 */
export class GeminiAgentHandler {
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
        this.yoloMode = true;  // Default to YOLO mode (auto-approve tools)

        // Setup End Session button handler
        const endBtn = $('endGeminiSession');
        if (endBtn) {
            endBtn.onclick = () => this.endSession();
        }

        // Setup New Session button handler
        const newSessionBtn = $('newGeminiSession');
        if (newSessionBtn) {
            newSessionBtn.onclick = () => this.startNewSession();
        }

        // Setup Dev Snapshot button handler
        const snapshotBtn = $('geminiSnapshotBtn');
        if (snapshotBtn) {
            snapshotBtn.onclick = () => this.createDevSnapshot();
        }

        // Setup YOLO Mode toggle
        const yoloToggle = $('geminiYoloToggle');
        if (yoloToggle) {
            yoloToggle.checked = this.yoloMode;
            yoloToggle.onchange = (e) => {
                this.yoloMode = e.target.checked;
                console.log('[GeminiAgent] YOLO mode:', this.yoloMode ? 'ON' : 'OFF');
                this.updateBannerYoloStatus();
            };
        }

        // Setup Banner Collapse/Expand Toggle
        const bannerCollapsed = $('geminiBannerCollapsed');
        const bannerCollapseBtn = $('geminiBannerCollapseBtn');

        // Define toggle functions
        window.expandGeminiBanner = function(e) {
            if (e) e.preventDefault();
            const banner = $('geminiCodeBanner');
            if (banner) {
                banner.classList.remove('collapsed');
                if (window.updateHistoryPaddingForBanner) {
                    window.updateHistoryPaddingForBanner();
                }
            }
        };

        window.collapseGeminiBanner = function(e) {
            if (e) {
                e.preventDefault();
                e.stopPropagation();
            }
            const banner = $('geminiCodeBanner');
            if (banner) {
                banner.classList.add('collapsed');
                if (window.updateHistoryPaddingForBanner) {
                    window.updateHistoryPaddingForBanner();
                }
            }
        };

        if (bannerCollapsed) {
            bannerCollapsed.addEventListener('click', window.expandGeminiBanner, { passive: false, capture: true });
        }

        if (bannerCollapseBtn) {
            bannerCollapseBtn.addEventListener('click', window.collapseGeminiBanner, { passive: false, capture: true });
        }

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
        const modal = $('confirmNewGeminiSessionModal');
        if (modal) {
            modal.classList.remove('hide');
        } else {
            // Fallback: confirm directly
            this.confirmNewSession();
        }
    }

    /**
     * Actually perform the session clear (called when user confirms)
     */
    confirmNewSession() {
        console.log('[GeminiAgent] Starting new session');
        const operator = this.currentOperator || window.__operator || 'Brandon';
        clearOperatorSession(operator + '-gemini');
        this.endSession();
        toast('Gemini session cleared. Ready for new prompt.');

        const modal = $('confirmNewGeminiSessionModal');
        if (modal) {
            modal.classList.add('hide');
        }
    }

    /**
     * Cancel new session (user clicked Cancel)
     */
    cancelNewSession() {
        toast('Session kept. Continuing with current context.');
        const modal = $('confirmNewGeminiSessionModal');
        if (modal) {
            modal.classList.add('hide');
        }
    }

    /**
     * Create a dev snapshot by triggering the /snapshot-dev slash command
     */
    createDevSnapshot() {
        console.log('[GeminiAgent] Triggering /snapshot-dev command');

        if (!this.hasActiveSession || !this.sessionId) {
            toast('No active session. Start a conversation first.');
            return;
        }

        const promptEl = $('prompt');
        if (promptEl) {
            promptEl.value = '/snapshot-dev';
            $('btnSend')?.click();
            toast('Snapshot command sent to Gemini CLI...');
        }
    }

    /**
     * Update YOLO mode indicator in banner
     */
    updateBannerYoloStatus() {
        const yoloIndicator = $('geminiYoloIndicator');
        if (yoloIndicator) {
            yoloIndicator.textContent = this.yoloMode ? 'YOLO: ON' : 'YOLO: OFF';
            yoloIndicator.className = 'yolo-indicator ' + (this.yoloMode ? 'yolo-on' : 'yolo-off');
        }
    }

    /**
     * Update banner status display
     * @param {string} status - Status text
     * @param {boolean} isStreaming - Whether currently streaming
     */
    updateBannerStatus(status, isStreaming = false) {
        const statusEl = $('geminiAgentStatus');
        const statusCompactEl = $('geminiStatusCompact');
        const bannerEl = $('geminiCodeBanner');

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

        const endBtn = $('endGeminiSession');
        if (endBtn) {
            if (this.hasActiveSession && !isStreaming) {
                endBtn.classList.remove('hide');
            } else {
                endBtn.classList.add('hide');
            }
        }
    }

    /**
     * Start a new agent session
     * @param {string} prompt - User prompt
     * @param {string} operator - Operator name
     */
    async startSession(prompt, operator) {
        this.currentOperator = operator || window.__operator || 'Brandon';

        const persistedSessionId = getOperatorSession(this.currentOperator + '-gemini');

        // Determine if this should continue a previous session
        // Resume if: we have an active session OR we have a persisted session ID
        const shouldResume = this.hasActiveSession || (persistedSessionId && persistedSessionId.length > 0);
        const isNewSession = !shouldResume;

        if (persistedSessionId && !this.sessionId) {
            // Restore persisted session
            this.sessionId = persistedSessionId;
            console.log('[GeminiAgent] Restoring persisted session:', this.sessionId);
        } else if (!this.sessionId) {
            // Create new session
            this.sessionId = crypto.randomUUID();
            saveOperatorSession(this.currentOperator + '-gemini', this.sessionId);
            this.messageCount = 0;
        }

        this.outputBuffer = '';
        this.thinkingBuffer = '';
        this.speakableBuffer = '';
        this.isStreaming = true;
        setStreamingState(true); // Enable smooth scrolling
        this.hasReceivedStreamingContent = false;
        this.messageCount++;

        console.log('[GeminiAgent] Session:', this.sessionId, 'Message #:', this.messageCount, 'New:', isNewSession);
        console.log('[GeminiAgent] Prompt:', prompt);
        console.log('[GeminiAgent] Operator:', this.currentOperator);
        console.log('[GeminiAgent] YOLO mode:', this.yoloMode);

        this.updateBannerStatus(isNewSession ? 'Starting...' : 'Continuing...', true);

        this.currentBubble = addBubble('assistant', '');
        this.currentBubble.classList.add('agent-streaming');

        const bubbleText = this.currentBubble.querySelector('.bubble-text');
        if (bubbleText) {
            const loadingMsg = isNewSession ? 'Starting Gemini CLI session...' : 'Continuing conversation...';
            bubbleText.innerHTML = `<div class="agent-loading">${loadingMsg}</div>`;
        }

        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProtocol}//${window.location.host}/ws/gemini-agent/${this.sessionId}`;

        console.log('[GeminiAgent] WebSocket URL:', wsUrl);

        try {
            this.ws = new WebSocket(wsUrl);
        } catch (err) {
            console.error('[GeminiAgent] WebSocket creation error:', err);
            this.updateBannerStatus('Connection Failed');
            if (bubbleText) {
                bubbleText.innerHTML = '<div class="agent-error">Failed to connect to Gemini CLI</div>';
            }
            return;
        }

        this.ws.onopen = () => {
            console.log('[GeminiAgent] WebSocket connected, sending prompt...');
            this.updateBannerStatus('Connected', true);

            const message = {
                type: 'prompt',
                text: prompt,
                operator: operator || window.__operator || 'Brandon',
                working_dir: null,
                yolo_mode: this.yoloMode,
                resume_session: shouldResume  // Tell backend to use --resume latest
            };
            console.log('[GeminiAgent] Sending:', message);
            this.ws.send(JSON.stringify(message));
        };

        this.ws.onmessage = (e) => {
            console.log('[GeminiAgent] Received:', e.data);
            try {
                const msg = JSON.parse(e.data);
                this.handleMessage(msg);
            } catch (err) {
                console.error('[GeminiAgent] Parse error:', err);
            }
        };

        this.ws.onclose = (e) => {
            console.log('[GeminiAgent] WebSocket closed:', e.code, e.reason);
            this.isStreaming = false;
            this.updateBannerStatus('Disconnected');
            if (this.currentBubble) {
                this.currentBubble.classList.remove('agent-streaming');
            }
        };

        this.ws.onerror = (err) => {
            console.error('[GeminiAgent] WebSocket error:', err);
            this.updateBannerStatus('Error');
            toast('Gemini agent connection error - check console');
            this.isStreaming = false;
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
        console.log('[GeminiAgent] Received message:', msg.type);
        switch(msg.type) {
            case 'content':
                this.updateBannerStatus('Responding...', true);
                this.hasReceivedStreamingContent = true;
                this.appendContent(msg.data);
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
            case 'raw':
                this.updateBannerStatus('Active', true);
                this.appendRawOutput(msg.data);
                break;
            case 'output':
                this.updateBannerStatus('Streaming...', true);
                this.appendRawOutput(msg.data);
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
                toast(msg.data || 'Gemini session ended');
                break;
            case 'info':
                console.log('[GeminiAgent] Info:', msg.data);
                toast(msg.data);
                break;
            case 'error':
                this.updateBannerStatus('Error');
                console.error('[GeminiAgent] Error:', msg.data);
                toast(`Gemini agent error: ${msg.data}`);
                break;
            case 'pong':
                break;
            default:
                console.warn('[GeminiAgent] Unhandled message type:', msg.type, 'data:', msg.data);
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
        // Skip empty or whitespace-only content
        if (!text || !text.trim()) {
            return;
        }
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
        // Skip empty or whitespace-only thinking
        if (!text || !text.trim()) {
            return;
        }
        if (!this.thinkingBuffer) this.thinkingBuffer = '';
        this.thinkingBuffer += text;
        // Note: Don't add thinking to speakableBuffer - TTS should only read final response
        this.renderOutput();
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

        // Add tool use to output buffer for visibility
        const icon = this.getToolIcon(toolName);
        let toolDisplay = `\n\n**${icon} ${toolName}**`;

        // Show relevant parameters
        if (toolInput.file_path) {
            toolDisplay += `\n\`${toolInput.file_path}\``;
        } else if (toolInput.dir_path) {
            toolDisplay += `\n\`${toolInput.dir_path}\``;
        } else if (toolInput.command) {
            toolDisplay += `\n\`\`\`bash\n${toolInput.command}\n\`\`\``;
        } else if (toolInput.query) {
            toolDisplay += `\n> ${toolInput.query}`;
        } else if (toolInput.instruction) {
            // For replace/edit tools, show a summary
            toolDisplay += `\n> ${toolInput.instruction.substring(0, 100)}${toolInput.instruction.length > 100 ? '...' : ''}`;
        }

        // For replace tool, generate a diff view from old_string and new_string
        if (toolName === 'replace' && toolInput.old_string && toolInput.new_string) {
            const diff = this.generateDiff(toolInput.old_string, toolInput.new_string);
            toolDisplay += `\n\n\`\`\`diff\n${diff}\n\`\`\``;
        }

        this.outputBuffer += toolDisplay;
        this.renderOutput();

        this.updateToolIndicator();
    }

    /**
     * Generate a unified diff from old and new strings
     * @param {string} oldStr - Original string
     * @param {string} newStr - New string
     * @returns {string} Diff in unified format
     */
    generateDiff(oldStr, newStr) {
        const oldLines = oldStr.split('\n');
        const newLines = newStr.split('\n');

        let diff = '';

        // Simple line-by-line diff
        // Find common prefix
        let commonStart = 0;
        while (commonStart < oldLines.length && commonStart < newLines.length &&
               oldLines[commonStart] === newLines[commonStart]) {
            commonStart++;
        }

        // Find common suffix
        let commonEnd = 0;
        while (commonEnd < oldLines.length - commonStart &&
               commonEnd < newLines.length - commonStart &&
               oldLines[oldLines.length - 1 - commonEnd] === newLines[newLines.length - 1 - commonEnd]) {
            commonEnd++;
        }

        // Show context (up to 3 lines before changes)
        const contextStart = Math.max(0, commonStart - 3);
        for (let i = contextStart; i < commonStart; i++) {
            diff += ` ${oldLines[i]}\n`;
        }

        // Show removed lines (from old)
        const oldEnd = oldLines.length - commonEnd;
        for (let i = commonStart; i < oldEnd; i++) {
            diff += `-${oldLines[i]}\n`;
        }

        // Show added lines (from new)
        const newEnd = newLines.length - commonEnd;
        for (let i = commonStart; i < newEnd; i++) {
            diff += `+${newLines[i]}\n`;
        }

        // Show context (up to 3 lines after changes)
        const contextEnd = Math.min(oldLines.length, oldEnd + 3);
        for (let i = oldEnd; i < contextEnd; i++) {
            diff += ` ${oldLines[i]}\n`;
        }

        return diff.trim() || '(no changes)';
    }

    /**
     * Append tool result
     * @param {string} text - Tool result text
     */
    appendToolResult(text) {
        // Skip empty or whitespace-only tool results
        if (!text || !text.trim()) {
            return;
        }

        // Truncate very long results
        const maxLen = 2000;
        let displayText = text;
        if (text.length > maxLen) {
            displayText = text.substring(0, maxLen) + `\n... (${text.length - maxLen} more characters)`;
        }

        const isDiff = displayText.includes('---') || displayText.includes('+++') ||
                      /^[\+\-@].*$/m.test(displayText);

        // Add result header
        this.outputBuffer += '\n<details><summary>Result</summary>\n\n';

        if (isDiff) {
            this.outputBuffer += '```diff\n' + displayText + '\n```';
        } else {
            this.outputBuffer += '```\n' + displayText + '\n```';
        }

        this.outputBuffer += '\n</details>\n';

        this.renderOutput();
    }

    /**
     * Append raw output
     * @param {string} html - Raw HTML to append
     */
    appendRawOutput(html) {
        // Skip empty or whitespace-only raw output
        if (!html || !html.trim()) {
            return;
        }

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
            // Claude tools
            'Bash': '\u26A1',
            'Read': '\uD83D\uDCC4',
            'Edit': '\u270F\uFE0F',
            'Write': '\uD83D\uDCBE',
            'Grep': '\uD83D\uDD0D',
            'Glob': '\uD83D\uDDC2\uFE0F',
            'Task': '\uD83E\uDD16',
            'WebSearch': '\uD83C\uDF10',
            'WebFetch': '\uD83C\uDF0D',
            // Gemini CLI tools
            'replace': '\u270F\uFE0F',
            'read_file': '\uD83D\uDCC4',
            'write_file': '\uD83D\uDCBE',
            'list_directory': '\uD83D\uDCC1',
            'shell': '\u26A1',
            'run_command': '\u26A1',
            'search': '\uD83D\uDD0D',
            'find_files': '\uD83D\uDDC2\uFE0F',
            // BlackBox MCP tools
            'search_snapshots': '\uD83D\uDD0D',
            'mint_snapshot': '\uD83D\uDCBE',
            'list_recent_snapshots': '\uD83D\uDCDA',
            'get_snapshot': '\uD83D\uDCCB',
            'get_context': '\uD83E\uDDE0',
            'generate_image': '\uD83C\uDFA8',
            'generate_video': '\uD83C\uDFAC',
            'lyria_music': '\uD83C\uDFB5',
            'analyze_image': '\uD83D\uDC41\uFE0F',
            'analyze_video': '\uD83C\uDFAC',
            'analyze_audio': '\uD83C\uDFA7',
            'text_to_speech': '\uD83D\uDD0A',
            'speech_to_text': '\uD83C\uDFA4'
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

        // Show thinking in collapsible section
        if (this.thinkingBuffer) {
            html += `<details class="agent-thinking"><summary>\uD83D\uDCAD Thinking...</summary><pre>${this.escapeHtml(this.thinkingBuffer)}</pre></details>`;
        }

        // Render main content as markdown
        if (this.outputBuffer) {
            const parsed = marked.parse(this.outputBuffer);
            html += typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(parsed) : parsed;
        }

        bubbleText.innerHTML = html;

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

        // Scroll to bottom
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
                if (!content || content.includes('agent-loading') || historyData[i].isGeminiAgent || historyData[i].isStreaming) {
                    historyData[i].content = bubbleText.innerHTML;
                    historyData[i].isGeminiAgent = true;
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
        // Hide tool indicator
        this.hideToolIndicator();

        this.renderOutput();

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
                        if (!content || content.includes('agent-loading') || historyData[i].isGeminiAgent || historyData[i].isStreaming) {
                            historyData[i].content = bubbleText.innerHTML;
                            historyData[i].isGeminiAgent = true;
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
    // Input Sending
    // =========================================================================

    /**
     * Send follow-up input to continue the conversation
     * @param {string} text - Input text
     */
    async sendInput(text) {
        console.log(`[GeminiAgent] sendInput called with: "${text}"`);

        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            if (this.hasActiveSession) {
                console.log('[GeminiAgent] WebSocket closed, reconnecting for follow-up');
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
                const wsUrl = `${wsProtocol}//${window.location.host}/ws/gemini-agent/${this.sessionId}`;

                try {
                    this.ws = new WebSocket(wsUrl);
                    this.ws.onopen = () => {
                        console.log('[GeminiAgent] Reconnected, sending follow-up prompt');
                        this.ws.send(JSON.stringify({
                            type: 'prompt',
                            text: text,
                            operator: window.__operator || 'Brandon',
                            yolo_mode: this.yoloMode
                        }));
                    };
                    this.ws.onmessage = (e) => {
                        try {
                            this.handleMessage(JSON.parse(e.data));
                        } catch (err) {
                            console.error('[GeminiAgent] Parse error:', err);
                        }
                    };
                    this.ws.onclose = () => {
                        console.log('[GeminiAgent] WebSocket closed after follow-up');
                        this.isStreaming = false;
                    };
                    this.ws.onerror = (err) => {
                        console.error('[GeminiAgent] WebSocket error:', err);
                        this.isStreaming = false;
                    };
                } catch (err) {
                    console.error('[GeminiAgent] Reconnect failed:', err);
                    toast('Failed to continue Gemini session');
                }
                return;
            } else {
                console.error('[GeminiAgent] No active session to continue');
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
        console.log('[GeminiAgent] Sending input message:', message);
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
        return active;
    }

    /**
     * End the current session
     */
    endSession() {
        console.log('[GeminiAgent] Ending session:', this.sessionId);

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'end_session' }));
        }

        this.hasActiveSession = false;
        this.sessionId = null;
        this.messageCount = 0;
        this.updateBannerStatus('Ready');
        toast('Gemini session ended. Start a new conversation.');
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

/** Global Gemini agent handler instance */
let geminiAgentHandler = null;

/**
 * Get the Gemini agent handler instance
 * @returns {GeminiAgentHandler|null} Gemini agent handler
 */
export function getGeminiAgentHandler() {
    return geminiAgentHandler;
}

/**
 * Set the Gemini agent handler instance
 * @param {GeminiAgentHandler} handler - Handler to set
 */
export function setGeminiAgentHandler(handler) {
    geminiAgentHandler = handler;
    window.geminiAgentHandler = handler;
}

// =============================================================================
// Session Initialization
// =============================================================================

/**
 * Check for existing Gemini agent session on page load
 */
export async function checkExistingGeminiAgentSession() {
    const operator = window.__operator || 'Brandon';

    const persistedSessionId = getOperatorSession(operator + '-gemini');
    if (persistedSessionId) {
        console.log('[GeminiAgent] Found persisted session ID for operator:', operator, persistedSessionId);
    }

    try {
        const resp = await fetch(`/gemini-agent/session/${operator}`);
        if (resp.ok) {
            const data = await resp.json();
            if (data.session && data.session.status !== 'completed') {
                console.log('[GeminiAgent] Found active backend session:', data.session.session_id);
                geminiAgentHandler = new GeminiAgentHandler();
                window.geminiAgentHandler = geminiAgentHandler;
                geminiAgentHandler.currentOperator = operator;
                geminiAgentHandler.sessionId = data.session.session_id;
                geminiAgentHandler.hasActiveSession = true;
                geminiAgentHandler.yoloMode = data.session.yolo_mode !== false;
                geminiAgentHandler.updateBannerStatus('Session Ready');
                geminiAgentHandler.updateBannerYoloStatus();
                saveOperatorSession(operator + '-gemini', data.session.session_id);
                return;
            }
        }
    } catch (e) {
        console.log('[GeminiAgent] Backend session check failed:', e.message);
    }

    if (persistedSessionId) {
        console.log('[GeminiAgent] Pre-loading persisted session ID for operator:', operator);
        geminiAgentHandler = new GeminiAgentHandler();
        window.geminiAgentHandler = geminiAgentHandler;
        geminiAgentHandler.currentOperator = operator;
        geminiAgentHandler.sessionId = persistedSessionId;
        geminiAgentHandler.hasActiveSession = true;
        geminiAgentHandler.updateBannerStatus('Session Ready');
        toast(`Restored Gemini session for ${operator}.`, 3000);
    }
}

/**
 * Initialize Gemini agent handler
 */
export function initGeminiAgentHandler() {
    checkExistingGeminiAgentSession();
}
