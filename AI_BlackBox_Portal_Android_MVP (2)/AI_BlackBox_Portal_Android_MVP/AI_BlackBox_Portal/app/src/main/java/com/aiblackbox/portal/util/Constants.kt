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

    // Default values. There is no default operator: it is resolved from the box
    // (GET /operators via data.store.OperatorResolver), never a seed name.
    const val DEFAULT_PROVIDER = "gemini"

    // Client identification header
    const val CLIENT_HEADER = "X-BlackBox-Client"
    const val CLIENT_ID = "native-android/1.0"

    /** The cold-start seed of a provider whose catalog the box serves: Auto alone. */
    val AUTO_ONLY: List<Pair<String, String>> = listOf("" to "Auto - Latest")

    /**
     * Model options per provider: provider-id -> list of (modelId, displayName). An empty
     * modelId ("") is "Auto - Latest": the box picks its configured default.
     *
     * COLD-START PLACEHOLDER ONLY for the providers the box serves a catalog for (gemini,
     * openai, anthropic, xai, computer-use, custom). Every picker of theirs (the composer
     * pill, the Settings sheet, the cron editor, the SMS reply picker, the CU screen)
     * hydrates from GET /models/{provider} through data.repository.ModelCatalogRepository,
     * which also keeps the last catalog the box served on disk, so these Auto-only seeds
     * show only on a phone that has never reached its box (audit A01-activities-root-10,
     * rank 76). The hand-kept id lists they replace went stale (retired Claude ids 404'd
     * until they were hand-patched, 2026-10-07) and drifted from the Portal's.
     *
     * The other providers have no /models endpoint and keep their lists here: the agents'
     * CLI aliases, the voice providers' offline fallbacks (hydrated by the voice screen),
     * and robotics' one model.
     */
    val MODEL_CONFIG: Map<String, List<Pair<String, String>>> = mapOf(
        "gemini" to AUTO_ONLY,
        "openai" to AUTO_ONLY,
        "anthropic" to AUTO_ONLY,
        "xai" to AUTO_ONLY,
        "agents" to listOf(
            "sonnet" to "Sonnet",
            "opus" to "Opus",
            "haiku" to "Haiku"
        ),
        "gemini-agents" to listOf(
            "gemini-3.1-pro-preview" to "Gemini 3.1 Pro"
        ),
        // OFFLINE FALLBACKS ONLY (P3.11) — the live rosters hydrate from
        // GET /realtime/status | /grok-live/status at voice-screen open
        // (VoiceViewModel catalog fetch; provider-API-as-SoT). 2.1 family per
        // design 2026-07-11 (GA 2026-07-06); backend P0/P2 probes gate the catalog.
        "realtime" to listOf(
            "gpt-realtime-2.1" to "GPT Realtime 2.1 (Newest GA)",
            "gpt-realtime-2.1-mini" to "GPT Realtime 2.1 Mini",
            "gpt-realtime-2" to "GPT Realtime 2",
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
            "" to "Auto (grok-voice-latest)",
            "grok-voice-latest" to "Grok Voice (latest alias)",
            "grok-voice-think-fast-1.0" to "Grok Voice Think Fast 1.0 (pinned)"
        ),
        "computer-use" to AUTO_ONLY,
        "custom" to AUTO_ONLY,
        "robotics" to listOf(
            "gemini-robotics-er-1.5-preview" to "Gemini Robotics-ER 1.5"
        )
    )

    // ─── Live Models Upgrade (T10, plan 2026-05-19) ──────────────────────────
    // Catalogs + allowlists for OpenAI Realtime + Gemini Live. Constants.kt is
    // the SoT — VoiceScreen.kt and VoiceClient.kt must consume from here.

    /** Default model id per live provider — offline fallback; catalog model_default overrides. */
    val LIVE_MODEL_DEFAULTS: Map<String, String> = mapOf(
        "realtime" to "gpt-realtime-2.1",
        "gemini-live" to "gemini-3.1-flash-live-preview",
        "grok-live" to "",  // Auto — backend resolves grok-voice-latest
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

    /** Grok Live voices — offline fallback (hydrated from GET /grok-live/status). */
    val VOICES_GROK_LIVE: List<String> = listOf("Ara", "Rex", "Sal", "Eve", "Leo")
    const val DEFAULT_GROK_LIVE_VOICE = "Ara"

    /** Grok Live reasoning.effort values (grok-voice-think-fast-1.0 background reasoning). */
    val GROK_LIVE_REASONING_EFFORTS: List<String> = listOf("high", "none")

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

    /** Model ids supporting affective dialog + proactive audio (2.5 native-audio family,
     *  v1alpha, DEPRECATED line). Mirrors backend GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS. */
    val GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS: Set<String> = setOf(
        "gemini-2.5-flash-native-audio-latest",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    )

    /** P6a — translation target languages: top-20 BCP-47 + free-text "Other"
     *  handled in VoiceScreen. Mirrors Portal/modules/voice-agents-modal.js. */
    val TRANSLATE_LANGUAGES: List<Pair<String, String>> = listOf(
        "en" to "English", "es" to "Spanish", "fr" to "French", "de" to "German",
        "it" to "Italian", "pt-BR" to "Portuguese (Brazil)", "ja" to "Japanese",
        "ko" to "Korean", "zh-CN" to "Chinese (Simplified)", "zh-TW" to "Chinese (Traditional)",
        "ar" to "Arabic", "hi" to "Hindi", "ru" to "Russian", "nl" to "Dutch",
        "pl" to "Polish", "tr" to "Turkish", "vi" to "Vietnamese", "th" to "Thai",
        "id" to "Indonesian", "uk" to "Ukrainian",
    )
}
