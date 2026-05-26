package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.CliAgentApp
import com.aiblackbox.portal.data.model.CliAgentAppsResponse
import com.aiblackbox.portal.data.model.CliAgentKillResponse
import com.aiblackbox.portal.data.model.CliAgentSessionEntry
import com.aiblackbox.portal.data.model.CliAgentSessionsResponse
import com.aiblackbox.portal.data.model.ZELLIJ_PROVIDER_SLUGS
import com.aiblackbox.portal.data.model.ZellijBackendStatus
import com.aiblackbox.portal.data.model.ZellijLaunchResponse
import com.aiblackbox.portal.data.model.ZellijListResponse
import com.aiblackbox.portal.data.model.ZellijSession
import com.aiblackbox.portal.data.model.ZellijSessionRow
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.io.IOException
import java.net.URLEncoder

/**
 * Build the deterministic CLI Agent tmux session id.
 *
 * Field separator is "__" (double underscore) so hyphenated operators
 * like "Brandon-DEV" round-trip cleanly. Apps root pick uses slug "_root".
 *
 * MUST match Orchestrator/cli_agent/session_manager.py::session_name.
 */
fun cliAgentSessionId(operator: String, provider: String, appSlug: String): String {
    require("__" !in operator && "__" !in provider) {
        "Operator/provider names must not contain '__'"
    }
    val slug = if (appSlug.isEmpty()) "_root" else appSlug
    return "cli-agent-${operator}__${provider}__${slug}"
}

/**
 * Repository for CLI Agent picker data: apps + sessions list + kill.
 *
 * Apps are filtered to those under the BlackBox `Apps/` directory
 * (workspace allowlist per design Q3). Sessions are scoped to the
 * given operator. Kill is idempotent — returns false (with reason
 * "not-found") for a session that doesn't exist.
 */
class CliAgentSessionRepository(private val api: BlackBoxApi) {

    /** Apps available as CLI Agent workspaces (those under Apps/). */
    suspend fun listApps(): List<CliAgentApp> {
        val body = api.get("/agent/apps")
        val parsed = api.json.decodeFromString(CliAgentAppsResponse.serializer(), body)
        return parsed.apps.filter { "/Apps/" in it.directory }
    }

    /** Live CLI Agent sessions for [operator]. */
    suspend fun listSessions(operator: String): List<CliAgentSessionEntry> {
        // URL-encode the operator name defensively
        val encoded = URLEncoder.encode(operator, "UTF-8")
        val body = api.get("/cli-agent/sessions?op=$encoded")
        return api.json.decodeFromString(CliAgentSessionsResponse.serializer(), body).sessions
    }

    /** Kill a session by id. Idempotent — never throws on missing. */
    suspend fun killSession(sessionId: String): Boolean {
        return try {
            val body = api.delete("/cli-agent/sessions/$sessionId")
            api.json.decodeFromString(CliAgentKillResponse.serializer(), body).killed
        } catch (e: IOException) {
            // 404 path on backends that don't return idempotent 200
            false
        }
    }

    // ── T19: Zellij REST surface ─────────────────────────────────────────
    //
    // These call the new zellij-backed endpoints introduced in Phase 2:
    //   POST   /cli-agent/zellij/launch?op={op}
    //   GET    /cli-agent/zellij/sessions?op={op}
    //   DELETE /cli-agent/zellij/sessions/{name}?op={op}
    //   GET    /cli-agent/zellij/backend-status?op={op}
    //
    // Convention: these throw IOException on transport / non-2xx failures,
    // matching the existing tmux methods above and the rest of the
    // repository layer (see UpdateRepository). The operator-prefix gate
    // (audit I8) is enforced server-side — the resulting HTTP 403 surfaces
    // as an IOException with "403" in the message.
    //
    // Provider strings are pre-checked against ZELLIJ_PROVIDER_SLUGS with
    // `require` so callers get a precise IllegalArgumentException at the
    // call site instead of a generic HTTP 400 from the backend.

    /**
     * Launch a fresh Zellij session for [operator] running [provider]
     * (optionally pinned to [app]'s working directory). The returned
     * [ZellijSession] carries the iframe-ready `sessionUrl` + short-lived
     * `token` the WebSocket transport needs at connect time.
     *
     * Wraps POST /cli-agent/zellij/launch?op={operator}.
     *
     * @throws IllegalArgumentException if [provider] is not in [ZELLIJ_PROVIDER_SLUGS].
     * @throws IOException on transport failure or non-2xx response.
     */
    @Throws(IOException::class)
    suspend fun launchZellijSession(
        operator: String,
        provider: String,
        app: String? = null,
    ): ZellijSession {
        require(provider in ZELLIJ_PROVIDER_SLUGS) {
            "Unknown Zellij provider '$provider'; expected one of $ZELLIJ_PROVIDER_SLUGS"
        }
        // Build the request body without a `null` app field when omitted.
        val request = if (app == null) {
            buildJsonObject { put("provider", JsonPrimitive(provider)) }
        } else {
            buildJsonObject {
                put("provider", JsonPrimitive(provider))
                put("app", JsonPrimitive(app))
            }
        }
        val bodyStr = api.json.encodeToString(JsonObject.serializer(), request)
        val encodedOp = URLEncoder.encode(operator, "UTF-8")
        val response = api.post("/cli-agent/zellij/launch?op=$encodedOp", bodyStr)
        val parsed = api.json.decodeFromString(ZellijLaunchResponse.serializer(), response)
        return ZellijSession(
            name = parsed.sessionName,
            provider = provider,
            sessionUrl = parsed.sessionUrl,
            token = parsed.token,
            expiresAt = parsed.expiresAt,
            createdAt = null,
            app = app,
            lastActivity = null,
        )
    }

    /**
     * List this [operator]'s live Zellij sessions. Returns rows in the
     * exact shape the backend emits — `name, provider, app, createdAt,
     * expiresAt`. The launch-time `sessionUrl`/`token` are NOT included
     * because the GET endpoint deliberately omits them; consumers that
     * need to re-attach use the token they captured at launch.
     *
     * Wraps GET /cli-agent/zellij/sessions?op={operator}.
     *
     * @throws IOException on transport failure or non-2xx response.
     */
    @Throws(IOException::class)
    suspend fun listZellijSessions(operator: String): List<ZellijSessionRow> {
        val encodedOp = URLEncoder.encode(operator, "UTF-8")
        val body = api.get("/cli-agent/zellij/sessions?op=$encodedOp")
        return api.json.decodeFromString(ZellijListResponse.serializer(), body).sessions
    }

    /**
     * Kill a Zellij session by [name]. The orchestrator's operator-prefix
     * gate (audit I8) returns HTTP 403 if [name] doesn't start with
     * "${operator}__"; we surface that as an IOException rather than
     * silently masking it the way [killSession] does for the tmux path
     * — Zellij kills are explicit user actions, not idempotent sweeps.
     *
     * Wraps DELETE /cli-agent/zellij/sessions/{name}?op={operator}.
     * Backend returns HTTP 204 No Content on success.
     *
     * @throws IOException on transport failure or non-2xx response (incl. 403).
     */
    @Throws(IOException::class)
    suspend fun killZellijSession(operator: String, name: String) {
        val encodedOp = URLEncoder.encode(operator, "UTF-8")
        val encodedName = URLEncoder.encode(name, "UTF-8")
        // BlackBoxApi.delete throws IOException on non-2xx, treats
        // any 2xx as success. 204 has an empty body — we don't parse.
        api.delete("/cli-agent/zellij/sessions/$encodedName?op=$encodedOp")
    }

    /**
     * Lightweight backend health probe for the status indicator (audit
     * I9). Requires an [operator] because the backend reports `my_session_count`
     * relative to that operator's prefix.
     *
     * Wraps GET /cli-agent/zellij/backend-status?op={operator}.
     *
     * @throws IOException on transport failure or non-2xx response.
     */
    @Throws(IOException::class)
    suspend fun getZellijBackendStatus(operator: String): ZellijBackendStatus {
        val encodedOp = URLEncoder.encode(operator, "UTF-8")
        val body = api.get("/cli-agent/zellij/backend-status?op=$encodedOp")
        return api.json.decodeFromString(ZellijBackendStatus.serializer(), body)
    }
}
