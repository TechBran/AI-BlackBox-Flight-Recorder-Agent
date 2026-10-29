/**
 * base-agent-handler.js
 * What the Claude Code and Gemini CLI agent handlers share (audit rank 75,
 * P03-live-agents-web-17): the per-turn WebSocket, the streaming buffers, the
 * bubble rendering and its history save, the banner, and the session
 * release/forget/end and new-session/dev-snapshot buttons. They were two
 * ~1,300-line copies that had drifted (a fix in one, never the other).
 *
 * A subclass returns its AGENT description (DOM ids, socket path, session
 * slot, names) from `get agent()` (a prototype getter, so it is there before
 * the constructor runs) and overrides the hooks where the agents differ:
 * the frames it sends, how a prompt adopts a session, what a socket close
 * means, and the messages, tools and panels only it has.
 */

import { $, toast, escapeHtml as sharedEscapeHtml } from './core-utils.js';
import { getHistoryData, setHistoryData, saveHistory, getOperator, getOperatorSession, saveOperatorSession, clearOperatorSession, OPERATOR_REQUIRED_MESSAGE } from './state-management.js';
import { addBubble } from './chat-bubbles.js';
import { markdownToSafeHtml } from './markdown-renderer.js';
import { sanitizeAssistantHtml } from './sanitize.js';
import { speakToBubble, setLastAssistantText } from './tts-stt.js';
import { makeSnapshotsClickable, applySyntaxHighlighting } from './timeline-browser.js';
import { scrollToBottomIfNeeded, setStreamingState } from './ui-setup.js';

/** Claude Code's tools (the Gemini handler adds its own on top) */
export const CODE_TOOL_ICONS = {
    'Bash': '⚡',
    'Read': '📄',
    'Edit': '✏️',
    'Write': '💾',
    'Grep': '🔍',
    'Glob': '🗂️',
    'Task': '🤖',
    'WebSearch': '🌐',
    'WebFetch': '🌍'
};

/** The banner elements already bound (once per page, whatever the instances). */
const _boundBannerEls = new WeakSet();

/**
 * Make an agent banner collapse and expand: the collapsed strip expands it,
 * the ▲ button collapses it (mouse and keyboard). Bound once per element, at
 * init, so it works before any handler exists (index.html used to carry an
 * inline onclick fallback for that, which the CSP now forbids).
 * @param {Object} agent - The agent description (ids.banner,
 *   ids.bannerCollapsed, ids.bannerCollapseBtn)
 */
export function bindBannerToggle(agent) {
    const { ids } = agent;
    const setCollapsed = (collapsed) => {
        const banner = $(ids.banner);
        if (!banner) return;
        banner.classList.toggle('collapsed', collapsed);
        if (window.updateHistoryPaddingForBanner) {
            window.updateHistoryPaddingForBanner();
        }
    };
    const expand = (e) => {
        if (e) e.preventDefault();
        setCollapsed(false);
    };
    const collapse = (e) => {
        if (e) {
            e.preventDefault();
            e.stopPropagation();
        }
        setCollapsed(true);
    };
    for (const [el, fn] of [[$(ids.bannerCollapsed), expand], [$(ids.bannerCollapseBtn), collapse]]) {
        if (!el || _boundBannerEls.has(el)) continue;
        _boundBannerEls.add(el);
        el.addEventListener('click', fn, { passive: false, capture: true });
        el.setAttribute('role', 'button');
        el.setAttribute('tabindex', '0');
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                fn(e);
            }
        });
    }
}

/**
 * One session probe at a time per operator: concurrent callers for the SAME
 * operator (boot + a re-select) share the probe in flight; another operator's
 * probe (an operator switch mid-probe) runs after it, so the last operator
 * picked is the one restored.
 * @param {function(string): Promise} run - The probe for one operator
 * @returns {function(): Promise} Probe for the current operator
 */
export function probeOncePerOperator(run) {
    let inFlight = null;
    return function check() {
        const operator = getOperator();
        if (inFlight && inFlight.operator === operator) return inFlight.promise;
        const prior = inFlight ? inFlight.promise.catch(() => {}) : Promise.resolve();
        const entry = { operator, promise: null };
        entry.promise = prior
            .then(() => run(operator))
            .finally(() => { if (inFlight === entry) inFlight = null; });
        inFlight = entry;
        return entry.promise;
    };
}

/**
 * An operator switch restores THAT operator's session: the previous operator's
 * is let go (not ended — it stays theirs to resume) and the new one's is looked
 * up. A turn that is still streaming keeps its operator; the next send
 * switches instead.
 * @param {BaseAgentHandler|null} handler - The agent's handler, if any
 * @param {function(): Promise} check - The agent's session probe
 */
export async function restoreForOperatorSwitch(handler, check) {
    if (handler && handler.isStreaming) return;
    if (handler && handler.currentOperator && !handler.belongsTo(getOperator())) {
        handler._releaseSession();
        handler.updateBannerStatus('Ready');
    }
    await check();
}

export class BaseAgentHandler {
    /**
     * What tells the agents apart (a subclass overrides it):
     *   logPrefix, name (in the loader, toasts and errors), connectFailed,
     *   wsPath, sessionSuffix (the operator-session slot), historyFlag,
     *   skipBlankDeltas, toolIcons, toasts {sessionCleared, sessionEnded,
     *   endedByServer, error, connectionError, continueFailed} and ids
     *   {endSession, newSession, snapshot, confirmModal, banner,
     *   bannerCollapsed, bannerCollapseBtn, status, statusCompact}
     * @returns {Object}
     */
    get agent() {
        throw new Error('BaseAgentHandler subclass must define get agent()');
    }

    constructor() {
        const agent = this.agent;
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
        // Every listener this instance adds to shared DOM goes through this
        // signal, so dispose() removes them all. A second instance used to
        // stack its own copies (audit P03-live-agents-web-04).
        this._listeners = new AbortController();
        const signal = this._listeners.signal;
        const { ids } = agent;

        // Setup End Session button handler
        const endBtn = $(ids.endSession);
        if (endBtn) {
            endBtn.onclick = () => this.endSession();
        }

        // Setup New Session button handler
        const newSessionBtn = $(ids.newSession);
        if (newSessionBtn) {
            newSessionBtn.onclick = () => this.startNewSession();
        }

        // Setup Dev Snapshot button handler
        const snapshotBtn = $(ids.snapshot);
        if (snapshotBtn) {
            snapshotBtn.onclick = () => this.createDevSnapshot();
        }

        // Banner collapse/expand (normally bound at init already)
        bindBannerToggle(agent);

        // Save state when page is about to unload
        window.addEventListener('beforeunload', () => {
            if (this.isStreaming && this.currentBubble) {
                this._saveToHistory();
            }
        }, { signal });
    }

    /** Log with the agent's prefix */
    _log(...args) {
        console.log(this.agent.logPrefix, ...args);
    }

    /** This operator's session slot (Claude: the operator; Gemini: <operator>-gemini) */
    _sessionKey(operator) {
        return operator + this.agent.sessionSuffix;
    }

    /** The turn socket's URL */
    _wsUrl() {
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${wsProtocol}//${window.location.host}${this.agent.wsPath}${this.sessionId}`;
    }

    /**
     * Retire this instance: its DOM listeners go and its socket is let go
     * quietly (the session itself is not ended server-side).
     */
    dispose() {
        this._listeners.abort();
        this._releaseSession();
    }

    /**
     * Leave the streaming state after a failure: a backend 'error', or a
     * socket that closed or errored without reconnecting. The 'completed'
     * message used to be the ONLY way out, so after a failure isStreaming
     * stayed true, the bubble kept its "Starting ... session" loader and
     * ui-setup's page-fill scrolling stayed on for the rest of the page
     * (audit P03-live-agents-web-12).
     * @param {string} [message] - Shown where the loader was, if no reply came
     */
    _finalizeFailure(message) {
        this.isStreaming = false;
        setStreamingState(false);
        const bubble = this.currentBubble;
        if (!bubble) return;
        bubble.classList.remove('agent-streaming');
        const loading = bubble.querySelector('.agent-loading');
        if (loading) {
            const note = document.createElement('div');
            note.className = 'agent-error';
            note.textContent = message || `${this.agent.name} stopped before replying.`;
            loading.replaceWith(note);
        }
    }

    // =========================================================================
    // Session Management
    // =========================================================================

    /**
     * Start a new clean session (clears current and lets user send new prompt)
     */
    startNewSession() {
        const modal = $(this.agent.ids.confirmModal);
        if (modal) {
            modal.classList.remove('hide');
        } else {
            // No confirmation modal: confirm directly
            this.confirmNewSession();
        }
    }

    /**
     * Actually perform the session clear (called when user confirms)
     */
    confirmNewSession() {
        this._log('Starting new session');
        const operator = this.currentOperator || getOperator();
        if (operator) clearOperatorSession(this._sessionKey(operator));
        this._onNewSession();
        this.endSession();
        toast(this.agent.toasts.sessionCleared);

        const modal = $(this.agent.ids.confirmModal);
        if (modal) {
            modal.classList.add('hide');
        }
    }

    /** Hook: a new session was asked for (before the current one ends) */
    _onNewSession() {}

    /**
     * Cancel new session (user clicked Cancel)
     */
    cancelNewSession() {
        toast('Session kept. Continuing with current context.');
        const modal = $(this.agent.ids.confirmModal);
        if (modal) {
            modal.classList.add('hide');
        }
    }

    /**
     * Create a dev snapshot by triggering the /snapshot-dev slash command
     */
    createDevSnapshot() {
        this._log('Triggering /snapshot-dev command');

        if (!this.hasActiveSession || !this.sessionId) {
            toast('No active session. Start a conversation first.');
            return;
        }

        const promptEl = $('prompt');
        if (promptEl) {
            promptEl.value = '/snapshot-dev';
            $('btnSend')?.click();
            toast(`Snapshot command sent to ${this.agent.name}...`);
        }
    }

    /**
     * Update banner status display
     * @param {string} status - Status text
     * @param {boolean} isStreaming - Whether currently streaming
     */
    updateBannerStatus(status, isStreaming = false) {
        const { ids } = this.agent;
        const bannerEl = $(ids.banner);

        const isReady = status.toLowerCase().includes('ready') ||
                        status.toLowerCase().includes('active') ||
                        status.toLowerCase().includes('msgs)');

        for (const el of [$(ids.status), $(ids.statusCompact)]) {
            if (!el) continue;
            el.textContent = status;
            el.classList.toggle('streaming', isStreaming);
            el.classList.toggle('ready', !isStreaming && isReady);
        }

        if (bannerEl) {
            bannerEl.classList.toggle('working', isStreaming);
            bannerEl.classList.toggle('session-ready', !isStreaming && isReady);
        }

        const endBtn = $(ids.endSession);
        if (endBtn) {
            endBtn.classList.toggle('hide', !(this.hasActiveSession && !isStreaming));
        }
    }

    // =========================================================================
    // Turns
    // =========================================================================

    /**
     * Hook: pick the session a new prompt goes to, from this operator's
     * persisted one or a fresh one.
     * @param {string|null} persistedSessionId - The operator's saved session
     * @returns {boolean} Whether this prompt starts a new session
     */
    _adoptSession(persistedSessionId) {
        const isNewSession = !this.hasActiveSession || !this.sessionId;
        if (isNewSession) {
            if (persistedSessionId && !this.sessionId) {
                this.sessionId = persistedSessionId;
                this._log('Restoring persisted session:', this.sessionId);
            } else {
                this.sessionId = crypto.randomUUID();
                saveOperatorSession(this._sessionKey(this.currentOperator), this.sessionId);
            }
            this.messageCount = 0;
        }
        return isNewSession;
    }

    /** Hook: a prompt opens (or re-opens) the session's socket */
    _attachSessionUi() {}

    /** Hook: the turn's socket opened */
    _onTurnOpen() {}

    /** Hook: whether a closed turn socket reconnects (else the turn fails) */
    _reconnectsAfter(_closeEvent) {
        return false;
    }

    /** The first prompt's frame */
    _promptMessage(prompt) {
        return { type: 'prompt', text: prompt, operator: this.currentOperator, working_dir: null };
    }

    /** A follow-up's frame when its socket had closed (the prompt is re-sent) */
    _followUpMessage(text) {
        return { type: 'prompt', text, operator: this.currentOperator || getOperator() };
    }

    /** A follow-up's frame on the open socket */
    _inputMessage(text) {
        return { type: 'input', text };
    }

    /**
     * Start one turn: fresh buffers, the streaming state, a bubble with its
     * loader. Every turn does all of it: a follow-up kept the previous turn's
     * "streaming content seen" flag, so a reply that came only as the complete
     * assistant message was dropped as a duplicate.
     * @param {string} loadingMsg - The loader text
     * @returns {HTMLElement|null} The bubble's text element
     */
    _startTurn(loadingMsg) {
        this.outputBuffer = '';
        this.thinkingBuffer = '';
        this.speakableBuffer = '';
        this.isStreaming = true;
        setStreamingState(true); // Enable smooth scrolling
        this.hasReceivedStreamingContent = false;
        this.messageCount++;

        this.currentBubble = addBubble('assistant', '');
        this.currentBubble.classList.add('agent-streaming');
        const bubbleText = this.currentBubble.querySelector('.bubble-text');
        if (bubbleText) {
            bubbleText.innerHTML = `<div class="agent-loading">${loadingMsg}</div>`;
        }
        return bubbleText;
    }

    /** Route a socket's frames to handleMessage */
    _onSocketMessage(e) {
        try {
            this.handleMessage(JSON.parse(e.data));
        } catch (err) {
            console.error(this.agent.logPrefix, 'Parse error:', err);
        }
    }

    /**
     * Start a new agent session
     * @param {string} prompt - User prompt
     * @param {string} operator - Operator name
     */
    async startSession(prompt, operator) {
        const op = operator || getOperator();
        if (!op) {
            toast(OPERATOR_REQUIRED_MESSAGE);
            return;
        }
        // An operator switch never continues the previous operator's session: let go of
        // it so this operator's own persisted session (or a fresh one) is used below.
        if (this.currentOperator && this.currentOperator !== op) this._releaseSession();
        this.currentOperator = op;

        const isNewSession = this._adoptSession(getOperatorSession(this._sessionKey(op)));
        this._attachSessionUi();

        const bubbleText = this._startTurn(
            isNewSession ? `Starting ${this.agent.name} session...` : 'Continuing conversation...');

        this._log('Session:', this.sessionId, 'Message #:', this.messageCount, 'New:', isNewSession);
        this._log('Prompt:', prompt);
        this._log('Operator:', this.currentOperator);

        this.updateBannerStatus(isNewSession ? 'Starting...' : 'Continuing...', true);

        const wsUrl = this._wsUrl();
        this._log('WebSocket URL:', wsUrl);

        try {
            this.ws = new WebSocket(wsUrl);
        } catch (err) {
            console.error(this.agent.logPrefix, 'WebSocket creation error:', err);
            this.updateBannerStatus('Connection Failed');
            if (bubbleText) {
                bubbleText.innerHTML = `<div class="agent-error">${this.agent.connectFailed}</div>`;
            }
            return;
        }

        this.ws.onopen = () => {
            this._onTurnOpen();
            this._log('WebSocket connected, sending prompt...');
            this.updateBannerStatus('Connected', true);

            const message = this._promptMessage(prompt);
            this._log('Sending:', message);
            this.ws.send(JSON.stringify(message));
        };

        this.ws.onmessage = (e) => this._onSocketMessage(e);

        this.ws.onclose = (e) => {
            this._log('WebSocket closed:', e.code, e.reason);
            if (this._reconnectsAfter(e)) {
                this.attemptReconnect();
            } else {
                this._finalizeFailure();
                this.updateBannerStatus('Disconnected');
            }
        };

        this.ws.onerror = (err) => {
            console.error(this.agent.logPrefix, 'WebSocket error:', err);
            this.updateBannerStatus('Error');
            toast(this.agent.toasts.connectionError);
            this._finalizeFailure(`Could not reach ${this.agent.name}.`);
        };
    }

    /**
     * Send follow-up input to continue the conversation
     * @param {string} text - Input text
     */
    async sendInput(text) {
        this._log(`sendInput called with: "${text}"`);
        this._log(`WebSocket state: ${this.ws ? this.ws.readyState : 'null'}, hasActiveSession=${this.hasActiveSession}`);
        const unreachable = `Could not reach ${this.agent.name}.`;

        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            if (!this.hasActiveSession) {
                console.error(this.agent.logPrefix, 'No active session to continue');
                toast('No active session - start a new conversation');
                return;
            }
            // The socket closed after the last turn: re-open it and re-prompt.
            this._attachSessionUi();
            this._startTurn('Continuing conversation...');
            this.updateBannerStatus('Continuing...', true);

            try {
                const ws = new WebSocket(this._wsUrl());
                this.ws = ws;
                this.ws.onopen = () => {
                    this._onTurnOpen();
                    this._log('Reconnected, sending follow-up prompt');
                    this.ws.send(JSON.stringify(this._followUpMessage(text)));
                };
                this.ws.onmessage = (e) => this._onSocketMessage(e);
                this.ws.onclose = (e) => {
                    this._log('WebSocket closed after follow-up:', e.code);
                    if (this.ws !== ws) return; // replaced by a later turn's socket: not this turn's end
                    if (this._reconnectsAfter(e)) {
                        this.attemptReconnect();
                    } else {
                        this._finalizeFailure();
                    }
                };
                this.ws.onerror = (err) => {
                    console.error(this.agent.logPrefix, 'WebSocket error:', err);
                    if (this.ws !== ws) return;
                    this._finalizeFailure(unreachable);
                };
            } catch (err) {
                console.error(this.agent.logPrefix, 'Reconnect failed:', err);
                toast(this.agent.toasts.continueFailed);
                this._finalizeFailure(unreachable);
            }
            return;
        }

        this._startTurn('Continuing conversation...');

        const message = this._inputMessage(text);
        this._log('Sending input message:', message);
        this.ws.send(JSON.stringify(message));
        this.updateBannerStatus('Continuing...', true);
    }

    // =========================================================================
    // Message Handling
    // =========================================================================

    /**
     * Hook: the messages only this agent has
     * @param {Object} msg - Message object with type and data
     * @returns {boolean} Whether it was handled
     */
    _handleAgentMessage(_msg) {
        return false;
    }

    /**
     * Handle incoming WebSocket message
     * @param {Object} msg - Message object with type and data
     */
    handleMessage(msg) {
        this._log('Received message:', msg.type);
        if (this._handleAgentMessage(msg)) return;
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
                toast(msg.data || this.agent.toasts.endedByServer);
                break;
            case 'info':
                this._log('Info:', msg.data);
                toast(msg.data);
                break;
            case 'error':
                this.updateBannerStatus('Error');
                console.error(this.agent.logPrefix, 'Error:', msg.data);
                toast(`${this.agent.toasts.error}: ${msg.data}`);
                // The backend no longer has this session (e.g. after a service
                // restart): forget it, or every next prompt goes to sendInput
                // and fails the same way until "New Session".
                if (/no (active )?session/i.test(String(msg.data || ''))) this._forgetSession();
                this._finalizeFailure(String(msg.data || ''));
                break;
            case 'pong':
                break;
            default:
                console.warn(this.agent.logPrefix, 'Unhandled message type:', msg.type, 'data:', msg.data);
                if (typeof msg.data === 'string' && msg.data.length < 10000) {
                    this.appendContent(msg.data);
                }
                break;
        }
    }

    // =========================================================================
    // Content Appending
    // =========================================================================

    /** Whether a delta is dropped as blank (the Gemini CLI's are) */
    _skipsBlank(text) {
        return this.agent.skipBlankDeltas && (!text || !text.trim());
    }

    /**
     * Append content to output buffer
     * @param {string} text - Content to append
     */
    appendContent(text) {
        if (this._skipsBlank(text)) return;
        text = this.enhanceContent(text);
        this.outputBuffer += text;
        this.speakableBuffer += text;
        this.renderOutput();
    }

    /**
     * Enhance content with file path highlighting.
     *
     * No diff decoration here: this runs on every streamed DELTA, so the old
     * ^-/^+ passes wrapped every markdown bullet, every '---' rule and any
     * delta that merely began with '-' in red/green diff spans. Real diffs
     * arrive fenced (```diff), and highlightDiff colors those at render time
     * (audit P03-live-agents-web-01).
     * @param {string} text - Text to enhance
     * @returns {string} Enhanced text
     */
    enhanceContent(text) {
        // Detect file path references
        return text.replace(/([\/\w\-\.]+\.(js|py|html|css|json|ts|tsx|jsx|md|txt|yaml|yml|xml|sh)):(\d+)/g,
            '<code class="file-ref">$1:<span class="line-num">$3</span></code>');
    }

    /**
     * Append thinking content
     * @param {string} text - Thinking text
     */
    appendThinking(text) {
        if (this._skipsBlank(text)) return;
        if (!this.thinkingBuffer) this.thinkingBuffer = '';
        this.thinkingBuffer += text;
        this._onThinking(text);
        this.renderOutput();
    }

    /** Hook: thinking text arrived (after it is buffered, before the render) */
    _onThinking(_text) {}

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
     * Append raw output
     * @param {string} html - Raw HTML to append
     */
    appendRawOutput(html) {
        if (this._skipsBlank(html)) return;
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
            // Agent stdout is untrusted (ansi_to_html wraps it in spans but does
            // not escape it): sanitize, keeping the ansi-* class spans.
            terminal.innerHTML = sanitizeAssistantHtml(this.outputBuffer);
            this._onRawOutput();

            scrollToBottomIfNeeded();
        }
    }

    /** Hook: the terminal output was re-rendered */
    _onRawOutput() {}

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

        let content = `<span class="tool-icon">${icon}</span> <strong>${this.escapeHtml(tool.name)}</strong>`;

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
        const icons = this.agent.toolIcons || CODE_TOOL_ICONS;
        return icons[toolName] || '🔧';
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
     * Hook: the thinking section of the bubble
     * @returns {string} HTML
     */
    _thinkingHtml() {
        return `<details class="agent-thinking"><summary>💭 Thinking...</summary><pre>${this.escapeHtml(this.thinkingBuffer)}</pre></details>`;
    }

    /** Hook: the bubble was just re-rendered */
    _onRendered(_bubbleText) {}

    /**
     * Render output buffer to bubble
     */
    renderOutput() {
        const bubbleText = this.currentBubble?.querySelector('.bubble-text');
        if (!bubbleText) return;

        let html = '';

        // Show thinking in collapsible section
        if (this.thinkingBuffer) {
            html += this._thinkingHtml();
        }

        // Render main content as markdown (sanitized; fails closed to escaped
        // text). Code is highlighted while parsing only when NOT streaming
        // (prevents jitter, and the per-token re-render never runs highlight.js).
        if (this.outputBuffer) {
            html += markdownToSafeHtml(this.outputBuffer, { streaming: this.isStreaming });
        }

        bubbleText.innerHTML = html;
        this._onRendered(bubbleText);

        if (!this.isStreaming) {
            // Diff blocks get the add/remove line colouring
            bubbleText.querySelectorAll('pre code.language-diff').forEach((block) => this.highlightDiff(block));

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
     * Immediately save the bubble to its history entry: the latest assistant
     * entry that is still a placeholder, streaming, or this agent's.
     * @param {boolean} [streaming] - Whether the entry is still streaming
     */
    _saveToHistory(streaming = this.isStreaming) {
        if (!this.currentBubble) return;

        const bubbleText = this.currentBubble.querySelector('.bubble-text');
        if (!bubbleText) return;

        const flag = this.agent.historyFlag;
        const historyData = getHistoryData();
        for (let i = historyData.length - 1; i >= 0; i--) {
            if (historyData[i].role === 'assistant') {
                const content = historyData[i].content || '';
                if (!content || content.includes('agent-loading') || historyData[i][flag] || historyData[i].isStreaming) {
                    historyData[i].content = bubbleText.innerHTML;
                    historyData[i][flag] = true;
                    historyData[i].isStreaming = streaming;
                    historyData[i].speakableText = this.speakableBuffer || '';
                    setHistoryData(historyData);
                    saveHistory();
                    break;
                }
            }
        }
    }

    /** Hook: the turn completed, before its final render */
    _onFinalizing() {}

    /** Hook: the turn's final render is in the bubble */
    _onFinalRendered() {}

    /**
     * Finalize output after completion
     */
    finalizeOutput() {
        this._onFinalizing();

        // Hide tool indicator
        this.hideToolIndicator();

        this.renderOutput();
        this._onFinalRendered();

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
            this._saveToHistory(false);
        }

        if (this._saveStateTimer) {
            clearTimeout(this._saveStateTimer);
            this._saveStateTimer = null;
        }

        this.thinkingBuffer = '';
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
        return wsOpen || this.hasActiveSession;
    }

    /**
     * Whether this session may take [operator]'s next prompt: it has no owner yet or is
     * theirs. Another operator's prompt starts (or restores) their own session instead.
     * @param {string} operator - Operator about to send
     * @returns {boolean}
     */
    belongsTo(operator) {
        return !this.currentOperator || this.currentOperator === operator;
    }

    /**
     * Let go of the current session WITHOUT ending it server-side (it stays the previous
     * operator's to resume): the socket is closed quietly so no handler of the old
     * session (a reconnect loop among them) runs against the one that replaces it.
     */
    _releaseSession() {
        if (this.ws) {
            this.ws.onopen = this.ws.onmessage = this.ws.onclose = this.ws.onerror = null;
            try { this.ws.close(1000); } catch (_) { /* already closed */ }
            this.ws = null;
        }
        this.sessionId = null;
        this.hasActiveSession = false;
        this.isStreaming = false;
        this.messageCount = 0;
    }

    /**
     * Forget a session the backend reports it no longer has: let go of the
     * socket and drop the persisted id, so the next prompt starts a fresh one.
     */
    _forgetSession() {
        const operator = this.currentOperator || getOperator();
        this._releaseSession();
        if (operator) clearOperatorSession(this._sessionKey(operator));
        this.updateBannerStatus('Ready');
    }

    /**
     * End the current session
     */
    endSession() {
        this._log('Ending session:', this.sessionId);

        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'end_session' }));
        }

        this.hasActiveSession = false;
        this.sessionId = null;
        this.messageCount = 0;
        this.updateBannerStatus('Ready');
        toast(this.agent.toasts.sessionEnded);
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
     * Escape HTML characters (shared core-utils helper; also escapes quotes)
     * @param {string} text - Text to escape
     * @returns {string} Escaped text
     */
    escapeHtml(text) {
        return sharedEscapeHtml(text);
    }
}
