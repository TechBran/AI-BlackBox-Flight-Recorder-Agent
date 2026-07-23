package com.aiblackbox.portal.data.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

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
 * Polls GET /cu/sessions for the D14 active-sessions badge (derived from
 * [CuSessionsState.sessions].size) + live-view targets. Parsing goes through
 * the shared kotlinx.serialization [BlackBoxApi.json]
 * (pure-JVM, unit-testable) — org.json is a non-mockable stub under this
 * module's `unitTests.returnDefaultValues = true`.
 */
/**
 * POST /cu/session/open response (desktop-first CU, 2026-07-23): the
 * ensure-or-create result for the operator's manual live desktop session.
 * `reused=true` means an existing live session was attached, not spawned.
 */
@Serializable
data class CuOpenedSession(
    @SerialName("session_id") val sessionId: String,
    @SerialName("view_url") val viewUrl: String = "",
    val reused: Boolean = false,
    @SerialName("live_view") val liveView: Boolean = false,
)

class CuSessionsClient(private val api: BlackBoxApi) {
    suspend fun sessions(): CuSessionsState {
        val body = api.get("/cu/sessions")
        return api.json.decodeFromString(CuSessionsState.serializer(), body)
    }

    /**
     * Ensure-or-create the operator's live virtual desktop session (display
     * quartet + live-view pipeline, NO agent loop). Blank/null operator is
     * OMITTED from the payload (server resolves its default) — never sent as
     * `"operator": null`. Non-2xx throws [ApiHttpException].
     */
    suspend fun openSession(operator: String? = null): CuOpenedSession {
        val payload = buildJsonObject {
            if (!operator.isNullOrBlank()) put("operator", operator)
        }
        val body = api.post("/cu/session/open",
            api.json.encodeToString(JsonObject.serializer(), payload))
        return api.json.decodeFromString(CuOpenedSession.serializer(), body)
    }

    /**
     * Explicitly end a live session via POST /cu/session/{sid}/close.
     * 404 (unknown/already-reaped session) surfaces as [ApiHttpException] —
     * callers treat it as "already gone" and refresh.
     */
    suspend fun closeSession(sessionId: String) {
        api.post("/cu/session/$sessionId/close", "{}")
    }
}

/**
 * Which session should the live-view entry point open? First session whose
 * quartet actually streams (`live_view=true` — websockify+noVNC present);
 * null when nothing is watchable (badge hidden, fallback viewer only).
 * Pure — unit-tested in CuSessionsClientTest. The served /cu/view page owns
 * session *switching*; this only picks the landing session.
 */
fun pickLiveViewSession(sessions: List<CuSession>): CuSession? =
    sessions.firstOrNull { it.liveView }

/**
 * Desktop-first CU entry decision (2026-07-23) — Kotlin mirror of the Portal's
 * pure `chooseDrawerSurface` (cu-viewer-route.js):
 *  - remote device target        → [CuEntrySurface.Fallback] "remote-device"
 *    (there is no LOCAL virtual desktop to open for it — never show the CTA)
 *  - no sessions on local target → [CuEntrySurface.OpenDesktop] (the
 *    "Open live desktop" CTA → POST /cu/session/open)
 *  - a streamable session        → [CuEntrySurface.Stream] — the live view is
 *    the DEFAULT surface; streamable = live_view AND a non-blank view_url
 *  - sessions but none stream    → [CuEntrySurface.Fallback]
 *    "stream-unavailable" (screenshot-poll viewer stays the surface)
 *
 * PURE — unit-tested in CuSessionsClientTest against fake /cu/sessions data.
 */
sealed interface CuEntrySurface {
    data class OpenDesktop(val reason: String) : CuEntrySurface
    data class Stream(val session: CuSession) : CuEntrySurface
    data class Fallback(val reason: String) : CuEntrySurface
}

fun chooseCuEntrySurface(
    sessions: List<CuSession>,
    deviceId: String? = null,
): CuEntrySurface {
    if (!deviceId.isNullOrBlank() && deviceId != "blackbox" && deviceId != "local") {
        return CuEntrySurface.Fallback("remote-device")
    }
    if (sessions.isEmpty()) return CuEntrySurface.OpenDesktop("no-sessions")
    val streamable = sessions.firstOrNull { it.liveView && it.viewUrl.isNotBlank() }
    return if (streamable != null) CuEntrySurface.Stream(streamable)
    else CuEntrySurface.Fallback("stream-unavailable")
}
