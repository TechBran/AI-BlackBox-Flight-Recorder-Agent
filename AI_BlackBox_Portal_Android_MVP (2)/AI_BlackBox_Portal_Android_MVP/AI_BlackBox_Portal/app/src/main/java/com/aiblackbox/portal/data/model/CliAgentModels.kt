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

// --- T19: Zellij REST surface DTOs ─────────────────────────────────────
// Mirrors POST /cli-agent/zellij/launch, GET /cli-agent/zellij/sessions,
// DELETE /cli-agent/zellij/sessions/{name}, GET /cli-agent/zellij/backend-status.
//
// Provider slugs accepted by the orchestrator (see _ZELLIJ_PROVIDER_BINARIES
// in cli_agent_routes.py): "claude", "gemini", "codex", "agy",
// "antigravity", "terminal". Antigravity has two aliases ("agy" and
// "antigravity"); we send the long form.

/** Allowed provider slugs for Zellij launch. */
val ZELLIJ_PROVIDER_SLUGS: Set<String> =
    setOf("claude", "gemini", "codex", "antigravity", "terminal")

/**
 * Live Zellij session metadata, as returned from POST /launch.
 *
 * Carries the iframe-ready [sessionUrl] + short-lived [token] that the
 * WebSocket transport needs at connect time. For lightweight listing
 * (GET /sessions), use [ZellijSessionRow] — it intentionally omits
 * token/sessionUrl since the backend doesn't re-issue them per call.
 *
 * **Master-token model (Phase 5, 2026-05-26):** the [token] field is
 * kept for backward compatibility with the launch-response wire format
 * but is no longer used by the Android client — the orchestrator handles
 * all zellij auth via a master token injected on the upstream proxy.
 * The field may be `null` or an empty string; consumers MUST NOT depend
 * on a non-empty value. See SNAP-20260526-6798 +
 * docs/plans/2026-05-24-zellij-cli-agent-rewrite.md Phase 4 RESULTS.
 */
@Serializable
data class ZellijSession(
    val name: String,
    val provider: String,
    @SerialName("session_url") val sessionUrl: String = "",
    val token: String = "",
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    val app: String? = null,
    // The orchestrator's GET /sessions response does NOT currently include
    // last_activity (see cli_agent_routes.py::zellij_list_sessions). The
    // field is reserved here for forward-compat with audit follow-ups
    // that may add server-side activity tracking.
    @SerialName("last_activity") val lastActivity: String? = null,
    // Phase 2-Android (2026-06-22, session persistence): true when the
    // launch ATTACHED an existing deterministic-name session rather than
    // creating a fresh one. Mirrors ZellijLaunchResponse.resumed; the
    // repository copies it through so the screen can surface a brief
    // "Resumed session" vs "Started new session" signal. Default false
    // keeps every existing synthesised-ZellijSession call site (which omit
    // it) byte-compatible.
    val resumed: Boolean = false,
)

@Serializable
data class ZellijLaunchResponse(
    @SerialName("session_name") val sessionName: String,
    @SerialName("session_url") val sessionUrl: String,
    // Phase 5 (2026-05-26, master-token model): orchestrator may emit null
    // or empty token; field retained for wire compatibility but unused.
    val token: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    // Phase 2-Android (2026-06-22): backend resume contract. true = the
    // launch reattached an existing deterministic-name session; false =
    // created fresh (or forked). Older backends that predate the field
    // simply omit it → defaults false (no resume signal), which is safe.
    val resumed: Boolean = false,
)

@Serializable
data class ZellijListResponse(
    val sessions: List<ZellijSessionRow> = emptyList(),
)

/**
 * Row as actually returned by GET /cli-agent/zellij/sessions. Distinct
 * from [ZellijSession] because the GET shape never includes session_url
 * or token — those are only meaningful immediately after launch. T20+
 * UI consumers reach for [ZellijSessionRow] when rendering the list and
 * [ZellijSession] when handling the just-launched session.
 */
@Serializable
data class ZellijSessionRow(
    val name: String,
    val provider: String,
    val app: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
)

/**
 * Backend health for the Portal status indicator. The orchestrator
 * returns five fields; [configuredBackend] and [effectiveBackend] let
 * the UI distinguish "configured zellij but fell back to tmux" from
 * "intentionally on tmux" (audit I9). T19 carries them through even
 * though the brief only enumerated three — they're cheap to expose
 * and a UI need will land in T22 (hamburger menu).
 */
@Serializable
data class ZellijBackendStatus(
    @SerialName("web_daemon_running") val webDaemonRunning: Boolean = false,
    @SerialName("session_count_total") val sessionCountTotal: Int = 0,
    @SerialName("my_session_count") val mySessionCount: Int = 0,
    @SerialName("configured_backend") val configuredBackend: String? = null,
    @SerialName("effective_backend") val effectiveBackend: String? = null,
)
