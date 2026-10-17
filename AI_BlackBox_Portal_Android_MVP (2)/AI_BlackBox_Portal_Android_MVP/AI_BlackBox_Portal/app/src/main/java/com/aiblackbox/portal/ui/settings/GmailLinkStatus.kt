package com.aiblackbox.portal.ui.settings

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonObject
import java.net.URLEncoder

/**
 * One operator's Google Workspace link, decoded from GET /gmail/status/{operator}.
 *
 * The backend proves the stored grant with a live Google call: a revoked or
 * expired grant (invalid_grant) comes back as connected:false + state
 * "needs_reauth" + a root-relative reauth_url, so Settings can offer Reconnect
 * instead of showing "Connected" for a dead token (audit W15). A backend older
 * than that has no `state`; [parse] then falls back to the `connected` boolean,
 * which keeps the old connected flow unchanged.
 *
 * Pure JVM (kotlinx.serialization), so it is unit-tested without Robolectric.
 */
data class GmailLinkStatus(
    val state: State,
    val email: String = "",
    val error: String? = null,
    val reauthPath: String? = null,
) {
    enum class State { CONNECTED, UNVERIFIED, NEEDS_REAUTH, NOT_CONNECTED }

    /** Healthy or unverifiable: the existing connected UI (email + Disconnect/Reconnect). */
    val isConnected: Boolean get() = state == State.CONNECTED || state == State.UNVERIFIED

    /**
     * The sign-in URL Connect/Reconnect opens. The server's reauth path is used
     * only when it is the same-origin authorize path; anything else falls back
     * to the URL built here.
     */
    fun authorizeUrl(origin: String, operator: String): String {
        val path = reauthPath?.takeIf { it.startsWith(AUTHORIZE_PATH + "?") }
            ?: "$AUTHORIZE_PATH?operator=${encode(operator)}"
        return origin.trimEnd('/') + path
    }

    companion object {
        const val AUTHORIZE_PATH = "/auth/gmail/authorize"

        private val json = Json { ignoreUnknownKeys = true }

        fun parse(body: String): GmailLinkStatus {
            val obj = json.parseToJsonElement(body).jsonObject
            val email = obj.string("email") ?: ""
            val state = when (obj.string("state")) {
                "connected" -> State.CONNECTED
                "unverified" -> State.UNVERIFIED
                "needs_reauth" -> State.NEEDS_REAUTH
                "not_connected" -> State.NOT_CONNECTED
                else -> when {
                    obj.bool("needs_reauth") == true -> State.NEEDS_REAUTH
                    obj.bool("connected") == true -> State.CONNECTED
                    else -> State.NOT_CONNECTED
                }
            }
            return GmailLinkStatus(
                state = state,
                email = email,
                error = obj.string("error"),
                reauthPath = obj.string("reauth_url"),
            )
        }

        /** Sign-in URL for an operator with no status in hand (e.g. status fetch failed). */
        fun authorizeUrl(origin: String, operator: String): String =
            GmailLinkStatus(State.NOT_CONNECTED).authorizeUrl(origin, operator)

        // URLEncoder is form encoding (space -> '+'), which the backend's query
        // parser decodes back to a space.
        private fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")

        private fun JsonObject.string(key: String): String? =
            (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.content

        private fun JsonObject.bool(key: String): Boolean? =
            (this[key] as? JsonPrimitive)?.booleanOrNull
    }
}
