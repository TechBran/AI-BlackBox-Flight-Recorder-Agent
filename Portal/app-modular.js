/**
 * app-modular.js
 * Modular entry point for the Portal application
 *
 * This file serves as the ES6 module entry point that loads all modular components.
 * The original app.js remains untouched as the immutable source of truth.
 *
 * To use the modular version, update index.html to:
 *   <script type="module" src="app-modular.js"></script>
 *
 * Source: Refactored from Portal/app.js (10,632 lines)
 * Created: 2025-12-20
 *
 * Module Structure:
 * =================
 *
 * Phase 1 - Foundation (no dependencies):
 *   - core-utils.js       : Basic utilities ($, toast, sleep, isMobile, etc.)
 *   - state-management.js : History, operators, providers, localStorage
 *   - notifications.js    : Push notifications and toasts
 *   - slash-commands.js   : Claude Code slash command dropdown
 *
 * Phase 2 - UI Components (depends on Phase 1):
 *   - markdown-renderer.js : Marked.js configuration and rendering
 *   - timeline-browser.js  : Snapshot timeline and syntax highlighting
 *   - ui-setup.js          : UI polish (scroll button, lightbox, shortcuts)
 *   - help-hints.js        : Help hints and tips
 *   - gemini-recorder.js   : Audio recording for Gemini analysis
 *
 * Phase 3 - Features (depends on Phases 1-2):
 *   - tts-stt.js          : Text-to-speech and speech-to-text
 *   - file-upload.js      : File attachment and upload
 *   - task-manager.js     : Background task polling
 *
 * Phase 4 - Chat Components (depends on Phases 1-3):
 *   - chat-bubbles.js     : Chat bubble creation and management
 *   - generation-modals.js: Image/video/music generation modals
 *   - agent-handler.js    : Claude Code WebSocket handler
 *
 * Phase 5 - Integration (depends on all):
 *   - chat-send.js        : Main send function and streaming
 *   - app-init.js         : Orchestrator and initialization
 */

// Import the orchestrator which handles all initialization
import './modules/app-init.js';

// CU live-view panel + shared active-sessions badge (D14). Type=module scripts
// are deferred, so the DOM is parsed by the time this top-level code runs.
import { initCuLiveView } from './modules/cu-live-view.js';
initCuLiveView();

// Re-export commonly used functions for potential external use
export {
    toast,
    $
} from './modules/core-utils.js';

export {
    getOperator,
    getHistoryData
} from './modules/state-management.js';

export {
    addBubble
} from './modules/chat-bubbles.js';

export {
    send
} from './modules/chat-send.js';

console.log('[Portal] Modular app loaded - 17 modules, ~7000+ lines refactored');
