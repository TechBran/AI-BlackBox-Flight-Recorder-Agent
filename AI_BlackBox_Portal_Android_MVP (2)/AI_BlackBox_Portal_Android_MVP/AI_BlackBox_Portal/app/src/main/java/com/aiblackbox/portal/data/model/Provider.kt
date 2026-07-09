package com.aiblackbox.portal.data.model

enum class ChatProvider(val id: String, val displayName: String) {
    GEMINI("gemini", "Gemini"),
    ANTHROPIC("anthropic", "Anthropic"),
    OPENAI("openai", "OpenAI"),
    XAI("xai", "xAI"),
    AGENTS("agents", "Claude Code"),
    GEMINI_AGENTS("gemini-agents", "Gemini CLI"),
    REALTIME("realtime", "GPT Realtime"),
    GEMINI_LIVE("gemini-live", "Gemini Live"),
    GROK_LIVE("grok-live", "Grok Live"),
    ROBOTICS("robotics", "Robotics (ER)"),
    COMPUTER_USE("computer-use", "Computer Use"),
    LOCAL("local", "On-Device (Gemma)"),

    // User-registered OpenAI-compatible servers on the BOX (llama.cpp, Ollama,
    // vLLM… — Task 7.1). Deliberately NOT isLocal: "local" means the turn runs
    // on the PHONE; a custom turn goes over the normal cloud SSE path and the
    // box dispatches it to the registered server — so isStreaming must be true.
    CUSTOM("custom", "Custom (Local)");

    companion object {
        fun fromId(id: String): ChatProvider = entries.find { it.id == id } ?: GEMINI
    }

    val isAgent get() = this == AGENTS || this == GEMINI_AGENTS
    val isVoice get() = this == REALTIME || this == GEMINI_LIVE || this == GROK_LIVE
    val isRobotics get() = this == ROBOTICS

    /** On-device Gemma — its turn runs locally on the phone, not via the mesh. */
    val isLocal get() = this == LOCAL

    /**
     * A cloud streaming/SSE provider. LOCAL is deliberately EXCLUDED: it is
     * neither agent nor voice, but its turn is executed on-device (Phase 2),
     * NOT over the cloud SSE path — so the SSE/streaming branch must never treat
     * it as a streaming provider.
     */
    val isStreaming get() = !isAgent && !isVoice && !isLocal
}
