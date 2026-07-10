package com.aiblackbox.portal.ui.components

/**
 * TaskUi — Kotlin mirror of `Portal/modules/task-ui.js` (G3-T12 / T13).
 *
 * Single source of truth SHARED with the web so the two task surfaces (Portal
 * pills + Android [TaskPanel]) cannot drift:
 *   - task_type -> { icon, label }             (TASK_TYPE_META / taskTypeMeta)
 *   - the CU / CLI-agent predicates that gate the clickable-expand, the STOP
 *     button and the "Live" button            (isCUTaskType / isCLITaskType /
 *                                               isAgentTaskType / canShowLiveView)
 *   - the progress-line / prompt truncation    (truncateText)
 *
 * PURE: no Android / Compose / serialization imports on purpose — every function
 * is trivially unit-testable (see TaskUiTest) and is a byte-for-byte behavioural
 * mirror of the JS spec. Icons are UTF-16 escapes (matching the codebase style)
 * but are the SAME strings as the JS emoji literals; the trailing comment shows
 * the glyph.
 */
object TaskUi {

    data class TypeMeta(val icon: String, val label: String)

    /** Fallback for an unknown task_type — a generic gear, label = the raw type. */
    private const val GENERIC_ICON = "⚙️" // ⚙️

    /**
     * task_type (Orchestrator/models.py :: TaskType) -> { icon, label }. Mirrors
     * TASK_TYPE_META exactly: canonical enum values, the legacy short keys, and
     * (T12) real icons+labels for Computer Use and the CLI coding agents.
     * Case-sensitive exact lookup — matches the JS map (backend enum is lowercase).
     */
    val TASK_TYPE_META: Map<String, TypeMeta> = mapOf(
        // --- Media / chat (canonical TaskType enum values) ---
        "image_generation" to TypeMeta("🎨", "Image Generation"), // 🎨
        "video_generation" to TypeMeta("🎬", "Video Generation"), // 🎬
        "audio_analysis" to TypeMeta("🎧", "Audio Analysis"),      // 🎧
        "google_tts" to TypeMeta("🔊", "Text-to-Speech"),          // 🔊
        "gemini_tts" to TypeMeta("🔊", "Text-to-Speech"),          // 🔊
        "lyria_music" to TypeMeta("🎵", "Music Generation"),       // 🎵
        "elevenlabs_music" to TypeMeta("🎵", "Music Generation"),  // 🎵
        "chat" to TypeMeta("💬", "Chat Response"),                 // 💬
        "agent_chat" to TypeMeta("💬", "Agent Chat"),              // 💬
        "checkpoint" to TypeMeta("💾", "Checkpoint"),              // 💾

        // --- Computer Use (T12: real icon + "Computer Use", never raw type) ---
        "use_computer" to TypeMeta("💻", "Computer Use"),          // 💻
        "browser_use" to TypeMeta("💻", "Computer Use"),           // 💻
        "gemini_cu" to TypeMeta("💻", "Computer Use"),             // 💻

        // --- CLI coding agents ---
        "cli_agent" to TypeMeta("⌨️", "CLI Agent"),                // ⌨️
        "claude_code_task" to TypeMeta("⌨️", "Claude Code"),       // ⌨️
        "gemini_cli_task" to TypeMeta("⌨️", "Gemini CLI"),         // ⌨️
        "codex_cli_task" to TypeMeta("⌨️", "Codex"),               // ⌨️

        // --- Legacy short keys (kept so any older caller still resolves cleanly) ---
        "image" to TypeMeta("🎨", "Image Generation"),             // 🎨
        "video" to TypeMeta("🎬", "Video Generation"),             // 🎬
        "audio" to TypeMeta("🎧", "Audio Analysis"),               // 🎧
        "tts" to TypeMeta("🔊", "Text-to-Speech"),                 // 🔊
        "ssml" to TypeMeta("🎙️", "SSML Generation")          // 🎙️
    )

    /** result_data.provider (cli_agent) -> product label. */
    private val CLI_PROVIDER_LABELS = mapOf(
        "claude" to "Claude Code",
        "gemini" to "Gemini CLI",
        "codex" to "Codex"
    )

    /**
     * Resolve { icon, label } for a task_type. Never returns blank fields. A
     * cli_agent with a known provider gets the specific product name but keeps
     * the terminal icon. Mirrors taskTypeMeta().
     */
    fun taskTypeMeta(taskType: String?, provider: String? = null): TypeMeta {
        val base = taskType?.let { TASK_TYPE_META[it] }
        if (taskType == "cli_agent" && provider != null && CLI_PROVIDER_LABELS.containsKey(provider)) {
            return TypeMeta(base?.icon ?: "⌨️", CLI_PROVIDER_LABELS.getValue(provider))
        }
        if (base != null) return base
        return TypeMeta(GENERIC_ICON, if (!taskType.isNullOrEmpty()) taskType else "Task")
    }

    private val CU_TASK_TYPES = setOf("use_computer", "browser_use", "gemini_cu")
    private val CLI_TASK_TYPES =
        setOf("cli_agent", "claude_code_task", "gemini_cli_task", "codex_cli_task")

    /** True for Computer-Use task types (get a "Live" button when a device is known). */
    fun isCUTaskType(taskType: String?): Boolean = taskType != null && taskType in CU_TASK_TYPES

    /** True for CLI coding-agent task types. */
    fun isCLITaskType(taskType: String?): Boolean = taskType != null && taskType in CLI_TASK_TYPES

    /**
     * "Agent" tasks = CU + CLI. Their progress_text is a live narration worth
     * expanding; media/chat tasks are not expandable.
     */
    fun isAgentTaskType(taskType: String?): Boolean =
        isCUTaskType(taskType) || isCLITaskType(taskType)

    /**
     * Live-view button predicate: only CU tasks, and only when we actually have a
     * device to address. Mirrors canShowLiveView() — the backend defaults
     * device_id to "blackbox", so in practice this is "CU type AND device_id
     * present & non-empty".
     */
    fun canShowLiveView(taskType: String?, deviceId: String?): Boolean =
        isCUTaskType(taskType) && deviceId != null && deviceId.isNotEmpty()

    /**
     * Truncate a progress line / prompt for a pill. Collapses runs of whitespace
     * (agent stdout can contain newlines) so it stays a single wrap-safe line, and
     * returns "" for null/empty/whitespace-only so callers can render nothing.
     * Mirrors truncateText(text, max=140).
     */
    fun truncateText(text: String?, max: Int = 140): String {
        if (text == null) return ""
        val s = text.replace(Regex("\\s+"), " ").trim()
        if (s.isEmpty()) return ""
        if (max > 0 && s.length > max) {
            return s.substring(0, maxOf(0, max - 1)).trimEnd() + "…" // …
        }
        return s
    }
}
