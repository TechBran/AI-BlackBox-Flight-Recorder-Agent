package com.aiblackbox.portal.ui.contacts

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.OperatorUnresolvedException
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.net.URLEncoder

/**
 * The contact book's requests, each in the book of the operator current AT THAT REQUEST
 * (A14-ui-misc-07): [operatorOf] is read on every call. The view model used to keep the
 * operator the screen was first opened with, so after a switch it saved and deleted in
 * the previous operator's book while the dialog already named the new one — and could
 * set is_operator_self in the wrong book.
 *
 * [list] returns null while no operator is resolved; writes throw
 * [OperatorUnresolvedException]. Bodies are built as JSON objects (a quote in a note or a
 * name stays text).
 */
class ContactsBookApi(
    private val api: BlackBoxApi,
    private val operatorOf: suspend () -> String,
) {
    private suspend fun operatorOrNull(): String? = operatorOf().trim().ifEmpty { null }

    private suspend fun operatorOrThrow(): String = operatorOrNull() ?: throw OperatorUnresolvedException()

    suspend fun list(): String? {
        val op = operatorOrNull() ?: return null
        return api.get("/contacts?operator=${q(op)}")
    }

    /** Upsert by name (the backend matches case-insensitively). [tags] is comma-separated. */
    suspend fun save(
        name: String,
        phone: String,
        email: String,
        relationship: String,
        notes: String,
        tags: String,
        inboundAllowed: Boolean,
        isOperatorSelf: Boolean,
    ): String {
        val body = buildJsonObject {
            put("operator", operatorOrThrow())
            put("name", name)
            put("phone", phone)
            put("email", email)
            put("relationship", relationship)
            put("notes", notes)
            putJsonArray("tags") {
                tags.split(",").map { it.trim() }.filter { it.isNotBlank() }.forEach { add(it) }
            }
            put("inbound_allowed", inboundAllowed)
            put("is_operator_self", isOperatorSelf)
        }
        return api.post("/contacts", body.toString())
    }

    suspend fun delete(id: String): String {
        val op = operatorOrThrow()
        return api.delete("/contacts/$id?operator=${q(op)}")
    }

    private fun q(value: String): String = URLEncoder.encode(value, "UTF-8")
}
