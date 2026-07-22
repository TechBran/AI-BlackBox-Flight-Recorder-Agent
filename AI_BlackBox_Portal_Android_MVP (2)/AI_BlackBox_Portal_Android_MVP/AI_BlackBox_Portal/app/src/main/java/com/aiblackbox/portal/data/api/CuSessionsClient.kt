package com.aiblackbox.portal.data.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One active Computer-Use session, as advertised by the Orchestrator's
 * GET /cu/sessions endpoint. `view_url` points at the per-session noVNC
 * live-view proxy (`/cu/view/{session_id}`). Extra/unknown fields (e.g.
 * `display`, `started_at`) are tolerated by the lenient Json config.
 */
@Serializable
data class CuSession(
    @SerialName("session_id") val sessionId: String,
    val operator: String = "",
    val backend: String = "",
    val width: Int = 0,
    val height: Int = 0,
    @SerialName("live_view") val liveView: Boolean = false,
    @SerialName("view_url") val viewUrl: String = "",
)

@Serializable
data class CuSessionsState(
    val active: Boolean = false,
    val sessions: List<CuSession> = emptyList(),
)

/**
 * Polls GET /cu/sessions for the D14 active-sessions badge (count) + live-view
 * targets. Parsing goes through the shared kotlinx.serialization [BlackBoxApi.json]
 * (pure-JVM, unit-testable) — org.json is a non-mockable stub under this
 * module's `unitTests.returnDefaultValues = true`.
 */
class CuSessionsClient(private val api: BlackBoxApi) {
    suspend fun sessions(): CuSessionsState {
        val body = api.get("/cu/sessions")
        return api.json.decodeFromString(CuSessionsState.serializer(), body)
    }
}
