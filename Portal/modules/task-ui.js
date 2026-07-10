/**
 * task-ui.js
 * Pure, framework-free helpers shared by the TWO Portal task surfaces:
 *   - the top-bar Task Monitor        (ui-setup.js,   polls GET /tasks/list)
 *   - the in-chat placeholder pills    (task-manager.js, polls GET /tasks/status/{id})
 *
 * Single source of truth for:
 *   - task_type -> { icon, label }               (taskTypeMeta / TASK_TYPE_META)
 *   - the CU / CLI-agent predicates that gate the clickable-expand, the STOP
 *     button and the "Live" button                (isCUTaskType / isCLITaskType /
 *                                                   isAgentTaskType / canShowLiveView)
 *   - the progress-line / prompt truncation        (truncateText)
 *
 * NO DOM and NO imports on purpose: these are trivially unit-testable under node
 * and are the spec the Android / T13 surface mirrors.
 *
 * G3-T12 (M3.2).
 */

// task_type (Orchestrator/models.py :: TaskType) -> { icon, label }.
// Includes the canonical enum values, the legacy short keys the top-bar used
// before, and — new in T12 — real icons+labels for Computer Use and the CLI
// coding agents (previously fell through to a generic gear + the raw type).
export const TASK_TYPE_META = {
    // --- Media / chat (canonical TaskType enum values) ---
    image_generation: { icon: '🎨', label: 'Image Generation' },
    video_generation: { icon: '🎬', label: 'Video Generation' },
    audio_analysis:   { icon: '🎧', label: 'Audio Analysis' },
    google_tts:       { icon: '🔊', label: 'Text-to-Speech' },
    gemini_tts:       { icon: '🔊', label: 'Text-to-Speech' },
    lyria_music:      { icon: '🎵', label: 'Music Generation' },
    elevenlabs_music: { icon: '🎵', label: 'Music Generation' },
    chat:             { icon: '💬', label: 'Chat Response' },
    agent_chat:       { icon: '💬', label: 'Agent Chat' },
    checkpoint:       { icon: '💾', label: 'Checkpoint' },

    // --- Computer Use (T12: real icon + "Computer Use", never raw 'use_computer') ---
    use_computer: { icon: '💻', label: 'Computer Use' },
    browser_use:  { icon: '💻', label: 'Computer Use' },
    gemini_cu:    { icon: '💻', label: 'Computer Use' },

    // --- CLI coding agents ---
    // The task_type on the wire is always 'cli_agent'; the *_task keys are the
    // ToolVault tool names, mapped here too for forward-compat and so a provider
    // hint can resolve to the product name (see CLI_PROVIDER_LABELS).
    cli_agent:        { icon: '⌨️', label: 'CLI Agent' },
    claude_code_task: { icon: '⌨️', label: 'Claude Code' },
    gemini_cli_task:  { icon: '⌨️', label: 'Gemini CLI' },
    codex_cli_task:   { icon: '⌨️', label: 'Codex' },

    // --- Legacy short keys (kept so any older caller still resolves cleanly) ---
    image: { icon: '🎨', label: 'Image Generation' },
    video: { icon: '🎬', label: 'Video Generation' },
    audio: { icon: '🎧', label: 'Audio Analysis' },
    tts:   { icon: '🔊', label: 'Text-to-Speech' },
    ssml:  { icon: '🎙️', label: 'SSML Generation' },
};

/** Fallback for an unknown task_type — a generic gear, label = the raw type. */
const GENERIC_ICON = '⚙️';

/** result_data.provider (cli_agent) -> product label. */
const CLI_PROVIDER_LABELS = { claude: 'Claude Code', gemini: 'Gemini CLI', codex: 'Codex' };

/**
 * Resolve { icon, label } for a task_type. Never returns undefined fields.
 * @param {string} taskType
 * @param {string} [provider] - optional cli_agent provider (claude|gemini|codex)
 *                              from result_data.provider (only /tasks/status has it)
 * @returns {{icon: string, label: string}}
 */
export function taskTypeMeta(taskType, provider) {
    const base = TASK_TYPE_META[taskType];
    // A cli_agent with a known provider gets the specific product name but keeps
    // the terminal icon.
    if (taskType === 'cli_agent' && provider && CLI_PROVIDER_LABELS[provider]) {
        return { icon: (base && base.icon) || '⌨️', label: CLI_PROVIDER_LABELS[provider] };
    }
    if (base) return { icon: base.icon, label: base.label };
    return { icon: GENERIC_ICON, label: taskType ? String(taskType) : 'Task' };
}

const CU_TASK_TYPES = new Set(['use_computer', 'browser_use', 'gemini_cu']);
const CLI_TASK_TYPES = new Set(['cli_agent', 'claude_code_task', 'gemini_cli_task', 'codex_cli_task']);

/** True for Computer-Use task types (get a "Live" button when a device is known). */
export function isCUTaskType(taskType) { return CU_TASK_TYPES.has(taskType); }

/** True for CLI coding-agent task types. */
export function isCLITaskType(taskType) { return CLI_TASK_TYPES.has(taskType); }

/**
 * "Agent" tasks = CU + CLI. Their progress_text is a live narration worth
 * expanding; media/chat tasks are not expandable.
 */
export function isAgentTaskType(taskType) {
    return isCUTaskType(taskType) || isCLITaskType(taskType);
}

/**
 * Live-view button predicate: only CU tasks, and only when we actually have a
 * device to address. The backend defaults device_id to 'blackbox', so in
 * practice this is "device_id present & non-empty".
 * @param {{task_type?: string, device_id?: string}} task
 * @returns {boolean}
 */
export function canShowLiveView(task) {
    return !!task
        && isCUTaskType(task.task_type)
        && typeof task.device_id === 'string'
        && task.device_id.length > 0;
}

/**
 * Truncate a progress line / prompt for a pill. Collapses runs of whitespace
 * (agent stdout can contain newlines) so it stays a single wrap-safe line, and
 * returns '' for null/empty/whitespace-only so callers can render nothing.
 * @param {*} text
 * @param {number} [max=140]
 * @returns {string}
 */
export function truncateText(text, max = 140) {
    if (text == null) return '';
    const s = String(text).replace(/\s+/g, ' ').trim();
    if (!s) return '';
    if (max > 0 && s.length > max) return s.slice(0, Math.max(0, max - 1)).trimEnd() + '…';
    return s;
}
