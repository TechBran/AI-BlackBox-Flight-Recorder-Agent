package com.aiblackbox.portal.ui.sms

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.OperatorUnresolvedException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.net.URLEncoder

/**
 * The SMS inbox's requests, each for the operator current AT THAT REQUEST
 * (A14-ui-misc-07): [operatorOf] is read on every call, so switching operator in the top
 * bar while the inbox is open moves the very next read, poll or send to the new
 * operator's inbox. (The view model used to keep the operator it was first opened with.)
 *
 * Reads return null while no operator is resolved — there is nobody's inbox to read;
 * writes throw [OperatorUnresolvedException] so the caller can say why nothing was sent.
 * Bodies are built as JSON objects, so a quote or newline in a message stays text.
 */
class SmsInboxApi(
    private val api: BlackBoxApi,
    private val operatorOf: suspend () -> String,
) {
    private suspend fun operatorOrNull(): String? = operatorOf().trim().ifEmpty { null }

    private suspend fun operatorOrThrow(): String = operatorOrNull() ?: throw OperatorUnresolvedException()

    suspend fun threads(): String? {
        val op = operatorOrNull() ?: return null
        return api.get("/sms/threads?operator=${q(op)}")
    }

    suspend fun messages(phone: String): String? {
        val op = operatorOrNull() ?: return null
        return api.get("/sms/messages?operator=${q(op)}&phone=${q(phone)}&limit=100")
    }

    suspend fun unread(): String? {
        val op = operatorOrNull() ?: return null
        return api.get("/sms/unread?operator=${q(op)}")
    }

    suspend fun preferences(): String? {
        val op = operatorOrNull() ?: return null
        return api.get("/sms/preferences?operator=${q(op)}")
    }

    /** [model] "" is Auto: the box answers with its own default for [provider]. */
    suspend fun savePreferences(provider: String, model: String): String {
        val body = buildJsonObject {
            put("operator", operatorOrThrow())
            put("sms_provider", provider)
            put("sms_model", model)
        }
        return api.post("/sms/preferences", body.toString())
    }

    /** [fromNumber] null/blank = the box's default line. */
    suspend fun send(to: String, message: String, fromNumber: String?): String {
        val body = buildJsonObject {
            put("operator", operatorOrThrow())
            put("to", to)
            put("message", message)
            if (!fromNumber.isNullOrBlank()) put("from_number", fromNumber)
        }
        return api.post("/sms/send", body.toString())
    }

    private fun q(value: String): String = URLEncoder.encode(value, "UTF-8")
}

/**
 * The SMS reply model the inbox shows. An empty [model] is "Auto" — the box's configured
 * default for [provider] — and is what the inbox sends unless the operator names a model
 * (X05-architecture-08 client half, A14-ui-misc-14): never a pinned id that later retires.
 */
data class SmsPreference(val provider: String, val model: String) {
    companion object {
        /** The box's own default provider for SMS replies, with Auto for the model. */
        val DEFAULT = SmsPreference(provider = "anthropic", model = "")

        private val json = Json { ignoreUnknownKeys = true; isLenient = true }

        /**
         * GET /sms/preferences → preference; anything missing or unreadable is [DEFAULT].
         * The Portal stores Google as "google"; this app's provider menu calls it "gemini"
         * (the server treats both alike), so it shows as gemini here.
         */
        fun parse(raw: String?): SmsPreference = try {
            val obj = json.parseToJsonElement(raw ?: "") as? JsonObject
            fun field(name: String) = (obj?.get(name) as? JsonPrimitive)?.takeIf { it.isString }?.content?.trim()
            val provider = field("sms_provider")?.ifEmpty { null } ?: DEFAULT.provider
            SmsPreference(
                provider = if (provider == "google") "gemini" else provider,
                model = field("sms_model").orEmpty(),
            )
        } catch (_: Exception) {
            DEFAULT
        }
    }
}
