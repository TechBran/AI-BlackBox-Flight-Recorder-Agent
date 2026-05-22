package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class CliAgentApp(
    val id: String = "",
    val name: String = "",
    val directory: String = "",
    val port: Int? = null,
)

@Serializable
data class CliAgentAppsResponse(
    val apps: List<CliAgentApp> = emptyList(),
)

@Serializable
data class CliAgentSessionEntry(
    @SerialName("session_id") val sessionId: String = "",
    val cwd: String = "",
)

@Serializable
data class CliAgentSessionsResponse(
    val sessions: List<CliAgentSessionEntry> = emptyList(),
)

@Serializable
data class CliAgentKillResponse(
    val killed: Boolean = false,
    val reason: String? = null,
)

// --- WebSocket control frames (Task 5.2) ---
// Outbound and inbound JSON envelopes for the /cli-agent/ws/{session_id} endpoint.
// Binary frames carry raw PTY bytes; text frames carry these JSON envelopes.

@Serializable
data class CliAgentResizeFrame(
    val type: String = "resize",
    val cols: Int,
    val rows: Int,
)

@Serializable
data class CliAgentPasteFrame(
    val type: String = "paste",
    val text: String,
)

@Serializable
data class CliAgentKillFrame(
    val type: String = "kill",
)

@Serializable
data class CliAgentSessionInfoFrame(
    val type: String = "session_info",
    val state: String = "",
)

@Serializable
data class CliAgentErrorFrame(
    val type: String = "error",
    val code: String = "",
    val message: String = "",
)

/**
 * CLI Agent providers the orchestrator can launch behind the PTY bridge.
 * Slug values must match the keys of `PROVIDER_BIN` in
 * Orchestrator/routes/cli_agent_routes.py — the orchestrator rejects any
 * unknown provider with WebSocket close code 4003.
 */
enum class CliAgentProvider(val slug: String, val display: String) {
    CLAUDE("claude", "Claude"),
    GEMINI("gemini", "Gemini"),
    CODEX("codex", "Codex"),
    ANTIGRAVITY("antigravity", "Antigravity"),
    ;

    companion object {
        fun fromSlug(slug: String): CliAgentProvider =
            values().firstOrNull { it.slug == slug } ?: CLAUDE
    }
}
