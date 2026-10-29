/**
 * gemini-agent-handler.js
 * Gemini CLI Agent WebSocket handler
 * Manages agent sessions, streaming, and tool execution display
 * Mirrors the Claude Code CLI integration pattern
 */

import { $, toast } from './core-utils.js';
import { getOperator, getOperatorSession, saveOperatorSession } from './state-management.js';
import {
    BaseAgentHandler, CODE_TOOL_ICONS, bindBannerToggle, probeOncePerOperator, restoreForOperatorSwitch,
} from './base-agent-handler.js';

// =============================================================================
// GeminiAgentHandler Class
// =============================================================================

/** The operator-session slot of an operator's Gemini session (Claude's is the operator) */
const GEMINI_SESSION_KEY = (operator) => operator + '-gemini';

/** What sets the Gemini CLI agent apart (base-agent-handler.js) */
const GEMINI_AGENT = {
    logPrefix: '[GeminiAgent]',
    name: 'Gemini CLI',
    connectFailed: 'Failed to connect to Gemini CLI',
    wsPath: '/ws/gemini-agent/',
    sessionSuffix: '-gemini',
    historyFlag: 'isGeminiAgent',
    // Empty or whitespace-only content, thinking, results and raw output are skipped
    skipBlankDeltas: true,
    toolIcons: {
        ...CODE_TOOL_ICONS,
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
    },
    toasts: {
        sessionCleared: 'Gemini session cleared. Ready for new prompt.',
        sessionEnded: 'Gemini session ended. Start a new conversation.',
        endedByServer: 'Gemini session ended',
        error: 'Gemini agent error',
        connectionError: 'Gemini agent connection error - check console',
        continueFailed: 'Failed to continue Gemini session',
    },
    ids: {
        endSession: 'endGeminiSession',
        newSession: 'newGeminiSession',
        snapshot: 'geminiSnapshotBtn',
        confirmModal: 'confirmNewGeminiSessionModal',
        banner: 'geminiCodeBanner',
        bannerCollapsed: 'geminiBannerCollapsed',
        bannerCollapseBtn: 'geminiBannerCollapseBtn',
        status: 'geminiAgentStatus',
        statusCompact: 'geminiStatusCompact',
    },
};

/**
 * Handler for Gemini CLI agent WebSocket communication: the shared turn and
 * rendering machinery (BaseAgentHandler) plus the YOLO switch, the resume
 * flag, and tool calls and results written into the reply.
 */
export class GeminiAgentHandler extends BaseAgentHandler {
    /** The Gemini CLI agent's description (BaseAgentHandler) */
    get agent() {
        return GEMINI_AGENT;
    }

    constructor() {
        super();
        this.yoloMode = true;  // Default to YOLO mode (auto-approve tools)
        this._resumeSession = false;

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

    // =========================================================================
    // Turn hooks (BaseAgentHandler)
    // =========================================================================

    /**
     * A prompt resumes when there is an active session OR a persisted session
     * ID (the backend then runs the CLI with --resume latest); otherwise it
     * starts a new session.
     * @param {string|null} persistedSessionId - The operator's saved Gemini session
     * @returns {boolean} Whether this prompt starts a new session
     */
    _adoptSession(persistedSessionId) {
        const shouldResume = this.hasActiveSession || (persistedSessionId && persistedSessionId.length > 0);
        this._resumeSession = shouldResume;

        if (persistedSessionId && !this.sessionId) {
            // Restore persisted session
            this.sessionId = persistedSessionId;
            console.log('[GeminiAgent] Restoring persisted session:', this.sessionId);
        } else if (!this.sessionId) {
            // Create new session
            this.sessionId = crypto.randomUUID();
            saveOperatorSession(GEMINI_SESSION_KEY(this.currentOperator), this.sessionId);
            this.messageCount = 0;
        }
        return !shouldResume;
    }

    _promptMessage(prompt) {
        return {
            ...super._promptMessage(prompt),
            yolo_mode: this.yoloMode,
            resume_session: this._resumeSession  // Tell backend to use --resume latest
        };
    }

    _followUpMessage(text) {
        return { ...super._followUpMessage(text), yolo_mode: this.yoloMode };
    }

    // =========================================================================
    // Tool Calls and Results
    // =========================================================================

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
    if (geminiAgentHandler && geminiAgentHandler !== handler) geminiAgentHandler.dispose();
    geminiAgentHandler = handler;
    window.geminiAgentHandler = handler;
}

/**
 * The handler a restore goes into: the existing instance, never a second
 * one (each constructor registers document-level listeners).
 * @param {string} operator - Whose session is being restored
 * @returns {GeminiAgentHandler}
 */
function geminiHandlerForRestore(operator) {
    if (!geminiAgentHandler) setGeminiAgentHandler(new GeminiAgentHandler());
    if (geminiAgentHandler.currentOperator && geminiAgentHandler.currentOperator !== operator) {
        geminiAgentHandler._releaseSession();
    }
    geminiAgentHandler.currentOperator = operator;
    return geminiAgentHandler;
}

// =============================================================================
// Session Initialization
// =============================================================================

/**
 * Check for existing Gemini agent session on page load, once the operator is
 * resolved (app-init awaits initOperatorSelector). With no operator there is
 * nobody's session to look for, so it neither probes nor restores.
 */
export const checkExistingGeminiAgentSession = probeOncePerOperator(_checkExistingGeminiAgentSession);

/** Whether a restore for `operator` is still wanted (no switch since it began). */
const stillOperator = (operator) => getOperator() === operator;

async function _checkExistingGeminiAgentSession(operator) {
    if (!operator || !stillOperator(operator)) return;
    // A turn that is streaming keeps its session; nothing to restore over it.
    if (geminiAgentHandler && geminiAgentHandler.isStreaming) return;

    const persistedSessionId = getOperatorSession(GEMINI_SESSION_KEY(operator));
    if (persistedSessionId) {
        console.log('[GeminiAgent] Found persisted session ID for operator:', operator, persistedSessionId);
    }

    try {
        const resp = await fetch(`/gemini-agent/session/${encodeURIComponent(operator)}`);
        if (resp.ok) {
            const data = await resp.json();
            if (!stillOperator(operator)) return; // switched away mid-probe
            if (data.session && data.session.status !== 'completed') {
                console.log('[GeminiAgent] Found active backend session:', data.session.session_id);
                const handler = geminiHandlerForRestore(operator);
                handler.sessionId = data.session.session_id;
                handler.hasActiveSession = true;
                handler.yoloMode = data.session.yolo_mode !== false;
                handler.updateBannerStatus('Session Ready');
                handler.updateBannerYoloStatus();
                saveOperatorSession(GEMINI_SESSION_KEY(operator), data.session.session_id);
                return;
            }
        }
    } catch (e) {
        console.log('[GeminiAgent] Backend session check failed:', e.message);
    }

    if (persistedSessionId && stillOperator(operator)) {
        const handler = geminiHandlerForRestore(operator);
        if (handler.sessionId === persistedSessionId && handler.hasActiveSession) return; // already restored
        console.log('[GeminiAgent] Pre-loading persisted session ID for operator:', operator);
        handler.sessionId = persistedSessionId;
        handler.hasActiveSession = true;
        handler.updateBannerStatus('Session Ready');
        toast(`Restored Gemini session for ${operator}.`, 3000);
    }
}

/**
 * Initialize Gemini agent handler. The boot-time session restore runs from app-init
 * once the operator is resolved (checkExistingGeminiAgentSession); running it here
 * probed before the operator was known.
 */
export function initGeminiAgentHandler() {
    bindBannerToggle(GEMINI_AGENT);
    window.addEventListener('bbx:operator-changed', restoreGeminiSessionForOperatorSwitch);
}

/**
 * An operator switch restores THAT operator's Gemini session: the previous operator's
 * is let go (not ended) and the new one's is looked up. A streaming turn keeps its
 * operator; the next send switches instead.
 */
export async function restoreGeminiSessionForOperatorSwitch() {
    await restoreForOperatorSwitch(geminiAgentHandler, checkExistingGeminiAgentSession);
}
