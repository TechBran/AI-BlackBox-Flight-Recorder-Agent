package com.aiblackbox.portal.data.voice

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull

/**
 * The server → client frames of `/ws/stt`, parsed in ONE place (audit rank 52: W24,
 * A08-cli-agent-ui-07, O08-stt-realtime-04).
 *
 * The contract, pinned for the server, the Portal and this app by the shared fixture
 * tests/fixtures/stt_ws_frames.json:
 *   - `stt_error` is TERMINAL (the client stops capture and never reconnects) and carries,
 *     additively, `code` (quota_exceeded, auth_failed, …), `provider` and `switchable`: another
 *     provider can serve the dictation, so the UI offers the ones /stt/catalog lists as available
 *     and streaming. An older server's bare message reads as a non-switchable provider_error.
 *   - `stt_status` `provider_override` names the provider the mic is really using, and
 *     `loading_models` means the on-box models are warming until the next state.
 */
object SttFrames {
    const val STATE_PROVIDER_OVERRIDE = "provider_override"
    const val STATE_LOADING_MODELS = "loading_models"
    const val CODE_PROVIDER_ERROR = "provider_error"

    private fun JsonObject.str(key: String): String? =
        (this[key] as? JsonPrimitive)?.takeUnless { it is JsonNull }?.content

    /** A server frame as an [SttEvent]; null for `stt_done` (a marker) and for unknown types. */
    fun parse(obj: JsonObject): SttEvent? = when (obj.str("type")) {
        "stt_delta" -> SttEvent.Delta(obj.str("text") ?: "")
        "stt_final" -> SttEvent.Final(obj.str("text") ?: "")
        "stt_error" -> SttEvent.Error(
            message = obj.str("message") ?: "Transcription error",
            code = obj.str("code") ?: CODE_PROVIDER_ERROR,
            provider = obj.str("provider"),
            switchable = (obj["switchable"] as? JsonPrimitive)?.booleanOrNull == true,
        )
        "stt_status" -> SttEvent.Status(
            state = obj.str("state") ?: "",
            provider = obj.str("provider"),
            overriddenFrom = obj.str("overridden_from"),
        )
        else -> null
    }

    /**
     * Providers that can take over a dictation [failedProvider] could not serve: available, able
     * to STREAM (a file-only local server cannot), and not the one that failed, in catalog order.
     */
    fun switchAlternatives(catalog: JsonObject?, failedProvider: String?): List<SttProviderChoice> {
        val providers = catalog?.get("providers") as? JsonArray ?: return emptyList()
        return providers.mapNotNull { element ->
            val p = element as? JsonObject ?: return@mapNotNull null
            val id = p.str("id") ?: return@mapNotNull null
            val available = (p["available"] as? JsonPrimitive)?.booleanOrNull == true
            val streams = (p["models"] as? JsonObject)?.str("streaming") != null
            if (available && streams && id != failedProvider) SttProviderChoice(id, p.str("label") ?: id) else null
        }
    }

    /** id → label from a /stt/catalog response. */
    fun labels(catalog: JsonObject?): Map<String, String> {
        val providers = catalog?.get("providers") as? JsonArray ?: return emptyMap()
        return providers.mapNotNull { element ->
            val p = element as? JsonObject ?: return@mapNotNull null
            val id = p.str("id") ?: return@mapNotNull null
            id to (p.str("label") ?: id)
        }.toMap()
    }

    /** The status chip's text for [status], or null when no chip should show. */
    fun statusText(status: SttUiStatus, labels: Map<String, String>): String? {
        fun label(id: String?) = id?.let { labels[it] ?: it } ?: "the selected provider"
        return when {
            status.warming -> "Warming on-box speech-to-text…"
            status.overrideProvider != null ->
                "Mic is using ${label(status.overrideProvider)}: " +
                    "${label(status.overriddenFrom)} can't stream on this box"
            else -> null
        }
    }
}

/** A provider the switch prompt offers. */
data class SttProviderChoice(val id: String, val label: String)

/**
 * What one dictation session's `stt_status` frames say, for the status chip: the provider the mic
 * is really using when the server overrode the pick, and whether the on-box models are warming.
 */
data class SttUiStatus(
    val warming: Boolean = false,
    val overrideProvider: String? = null,
    val overriddenFrom: String? = null,
) {
    fun reduce(status: SttEvent.Status): SttUiStatus = when (status.state) {
        SttFrames.STATE_PROVIDER_OVERRIDE ->
            copy(overrideProvider = status.provider, overriddenFrom = status.overriddenFrom)
        SttFrames.STATE_LOADING_MODELS -> copy(warming = true)
        // listening / speech / processing: the models are up.
        else -> copy(warming = false)
    }
}
