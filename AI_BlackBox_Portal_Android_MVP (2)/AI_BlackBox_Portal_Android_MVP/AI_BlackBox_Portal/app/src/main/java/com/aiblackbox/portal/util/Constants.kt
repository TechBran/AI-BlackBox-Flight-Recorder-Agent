package com.aiblackbox.portal.util

object Constants {
    const val PREFS_NAME = "bbx_prefs"
    const val KEY_ORIGIN = "origin"
    const val KEY_OPERATOR = "operator"
    const val KEY_USE_NATIVE = "use_native_ui"

    // DataStore keys
    const val DS_PROVIDER = "provider"
    const val DS_MODEL = "model"
    const val DS_STREAMING = "streaming_enabled"
    const val DS_CLAUDE_MODEL = "claude_model"
    const val DS_TTS_VOICE = "tts_voice"

    // API paths
    const val API_CHAT = "/chat"
    const val API_CHAT_STREAM = "/chat/stream"
    const val API_CHAT_SAVE = "/chat/save"
    const val API_UPLOAD = "/upload"
    const val API_HEALTH = "/health"
    const val API_MODELS = "/models"
    const val API_OPERATORS = "/operators"
    const val API_OPERATOR_PREFS = "/operator/preferences"
    const val API_TASKS_LIST = "/tasks/list"
    const val API_TASKS_STATUS = "/tasks/status"
    const val API_AGENT_SESSION = "/agent/session"
    const val API_AGENT_APPS = "/agent/apps"
    const val API_AGENT_COMMANDS = "/agent/commands"
    const val API_GENERATE_IMAGE = "/generate/image"
    const val API_GENERATE_VIDEO = "/generate/video"
    const val API_GENERATE_MUSIC = "/generate/lyria_music"
    const val API_GENERATE_GOOGLE_TTS = "/generate/google_ssml"
    const val API_GENERATE_GEMINI_TTS = "/generate/gemini_tts"
    const val API_TTS = "/tts"
    const val API_TTS_VOICES = "/tts/voices"
    const val API_STT = "/stt"
    const val API_TIMELINE = "/timeline"
    const val API_ASSERT = "/assert"
    const val API_MEDIA_LIST = "/api/media/list"
    const val API_DEVICES = "/devices/"
    const val API_CRON_JOBS = "/api/cron/jobs"
    const val API_ASTERISK = "/asterisk"
    const val API_INTERNET = "/internet"
    const val API_BROWSER_SCREENSHOT = "/browser/screenshot"
    const val API_FOSSIL_HYBRID = "/fossil/hybrid"

    // WebSocket paths
    const val WS_AGENT = "/ws/agent"
    const val WS_GEMINI_AGENT = "/ws/gemini-agent"
    const val WS_STT = "/ws/stt"

    // Default values
    const val DEFAULT_PROVIDER = "gemini"
    const val DEFAULT_OPERATOR = "Brandon"

    // Client identification header
    const val CLIENT_HEADER = "X-BlackBox-Client"
    const val CLIENT_ID = "native-android/1.0"

    /**
     * Model options per provider.
     * Each entry is provider-id -> list of (modelId, displayName).
     * Empty modelId ("") means "Auto - Latest" (server picks best).
     */
    val MODEL_CONFIG: Map<String, List<Pair<String, String>>> = mapOf(
        "gemini" to listOf(
            "" to "Auto - Latest",
            "gemini-3.1-pro-preview-customtools" to "Gemini 3.1 Pro Custom Tools",
            "gemini-3.1-pro-preview" to "Gemini 3.1 Pro",
            "gemini-2.5-pro" to "Gemini 2.5 Pro",
            "gemini-2.5-flash" to "Gemini 2.5 Flash",
            "gemini-2.5-flash-lite" to "Gemini 2.5 Flash-Lite"
        ),
        "openai" to listOf(
            "" to "Auto - Latest",
            "gpt-5.1" to "GPT-5.1",
            "gpt-5" to "GPT-5",
            "gpt-5-mini" to "GPT-5 Mini",
            "o3" to "o3 (Reasoning)",
            "o4-mini" to "o4-mini (Reasoning)",
            "gpt-4o" to "GPT-4o"
        ),
        "anthropic" to listOf(
            "" to "Auto - Latest",
            "claude-opus-4-7" to "Claude Opus 4.7 (1M ctx, adaptive thinking)",
            "claude-opus-4-6" to "Claude Opus 4.6",
            "claude-sonnet-4-6" to "Claude Sonnet 4.6",
            "claude-sonnet-4-5" to "Claude Sonnet 4.5",
            "claude-haiku-4-5" to "Claude Haiku 4.5",
            "claude-opus-4-1" to "Claude Opus 4.1",
            "claude-sonnet-4" to "Claude Sonnet 4"
        ),
        // T3 (2026-05-18): xai entries were MALFORMED — "grok-4", "grok-4.1-fast",
        // "grok-3-mini" don't exist in the xAI API and would silently fail at
        // chat-completion time. Refreshed to real API IDs matching current 2026
        // catalog. These are OFFLINE-BOOTSTRAP fallbacks only — ChatViewModel's
        // fetchLiveModels() hydrates the dropdown from /models/xai on init.
        "xai" to listOf(
            "" to "Auto - Latest",
            "grok-4.3" to "Grok 4.3",
            "grok-4.20-multi-agent-0309" to "Grok 4.20 Multi-Agent",
            "grok-3-mini-beta" to "Grok 3 Mini (legacy)"
        ),
        "agents" to listOf(
            "sonnet" to "Sonnet",
            "opus" to "Opus",
            "haiku" to "Haiku"
        ),
        "gemini-agents" to listOf(
            "gemini-3.1-pro-preview" to "Gemini 3.1 Pro"
        ),
        // Empirically WS-connection-verified 2026-05-19 against the GA endpoint
        // (no OpenAI-Beta header). gpt-realtime-2 is the newest GA default;
        // gpt-realtime-2025-08-28 remains REJECTED at the WS endpoint (close
        // code 4000) and is intentionally absent. See docs/plans/2026-05-19-live-api-ga-migration.md.
        "realtime" to listOf(
            "gpt-realtime-2" to "GPT Realtime 2 (Newest GA)",
            "gpt-realtime" to "GPT Realtime (GA alias)",
            "gpt-realtime-1.5" to "GPT Realtime 1.5 (pinned)",
            "gpt-realtime-mini" to "GPT Realtime Mini (cheap, alias)",
            "gpt-realtime-mini-2025-12-15" to "GPT Realtime Mini (Dec 2025 pin)"
        ),
        "gemini-live" to listOf(
            "gemini-3.1-flash-live-preview" to "Gemini 3.1 Flash Live (Preview, thinkingLevel)",
            "gemini-2.5-flash-native-audio-latest" to "Gemini 2.5 Flash Live (Latest GA-track)",
            "gemini-2.5-flash-native-audio-preview-12-2025" to "Gemini 2.5 Flash Live (Dec 2025 pin)"
        ),
        "grok-live" to listOf(
            "" to "Grok Live"
        ),
        // Offline fallback only — replaced by GET /models/computer-use hydration
        // (ChatViewModel.fetchLiveModels, CU production pass 2026-06). Mirrors the
        // Portal's fallback in Portal/modules/state-management.js. No backend field
        // is possible in this Pair structure — offline backend partitioning relies
        // on CuScreen.cuModelsForBackend's id-substring heuristic.
        "computer-use" to listOf(
            "" to "Auto - Latest",
            "claude-opus-4-6" to "Claude Opus 4.6",
            "gemini-2.5-computer-use-preview-10-2025" to "Gemini CU Preview",
            "gpt-5.5" to "GPT-5.5 (Computer Use)"
        ),
        "robotics" to listOf(
            "gemini-robotics-er-1.5-preview" to "Gemini Robotics-ER 1.5"
        )
    )

    // ─── Live Models Upgrade (T10, plan 2026-05-19) ──────────────────────────
    // Catalogs + allowlists for OpenAI Realtime + Gemini Live. Constants.kt is
    // the SoT — VoiceScreen.kt and VoiceClient.kt must consume from here.

    /** Default model id per live provider — first item the dropdown picks if no user pref. */
    val LIVE_MODEL_DEFAULTS: Map<String, String> = mapOf(
        "realtime" to "gpt-realtime-2",
        "gemini-live" to "gemini-3.1-flash-live-preview",
    )

    /** OpenAI Realtime voices (10 GA voices, 2026-05-19 verified). */
    val VOICES_GPT_REALTIME: List<String> = listOf(
        "alloy", "ash", "ballad", "coral", "echo",
        "sage", "shimmer", "verse", "marin", "cedar"
    )
    const val DEFAULT_GPT_REALTIME_VOICE = "ash"

    /** Gemini Live voices (30 voices, full catalog). */
    val VOICES_GEMINI_LIVE: List<String> = listOf(
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
        "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus",
        "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
        "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
        "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat"
    )
    const val DEFAULT_GEMINI_LIVE_VOICE = "Orus"

    /** Gemini voice character descriptors (1:1 with VOICES_GEMINI_LIVE). */
    val GEMINI_VOICE_DESCRIPTORS: Map<String, String> = mapOf(
        "Zephyr" to "Bright",       "Puck" to "Upbeat",          "Charon" to "Informative",
        "Kore" to "Firm",           "Fenrir" to "Excitable",     "Leda" to "Youthful",
        "Orus" to "Firm",           "Aoede" to "Breezy",         "Callirrhoe" to "Easy-going",
        "Autonoe" to "Bright",      "Enceladus" to "Breathy",    "Iapetus" to "Clear",
        "Umbriel" to "Easy-going",  "Algieba" to "Smooth",       "Despina" to "Smooth",
        "Erinome" to "Clear",       "Algenib" to "Gravelly",     "Rasalgethi" to "Informative",
        "Laomedeia" to "Upbeat",    "Achernar" to "Soft",        "Alnilam" to "Firm",
        "Schedar" to "Even",        "Gacrux" to "Mature",        "Pulcherrima" to "Forward",
        "Achird" to "Friendly",     "Zubenelgenubi" to "Casual", "Vindemiatrix" to "Gentle",
        "Sadachbia" to "Lively",    "Sadaltager" to "Knowledgeable", "Sulafat" to "Warm"
    )

    /** Allowed VAD types for OpenAI Realtime. */
    val OPENAI_REALTIME_VAD_TYPES: List<String> = listOf("server_vad", "semantic_vad")

    /** Allowed eagerness for OpenAI Realtime semantic_vad. */
    val OPENAI_REALTIME_VAD_EAGERNESS: List<String> = listOf("auto", "low", "medium", "high")

    /** Allowed VAD sensitivities for Gemini Live. UPPERCASE per Gemini API. */
    val GEMINI_LIVE_VAD_SENSITIVITIES: List<String> = listOf("LOW", "MEDIUM", "HIGH")

    /** Allowed thinking levels for Gemini 3.1. LOWERCASE per google-genai SDK. */
    val GEMINI_LIVE_THINKING_LEVELS: List<String> = listOf("minimal", "low", "medium", "high")

    /** Model ids that support thinkingLevel (3.1-only currently). */
    val GEMINI_LIVE_THINKING_CAPABLE_MODELS: Set<String> = setOf("gemini-3.1-flash-live-preview")
}
