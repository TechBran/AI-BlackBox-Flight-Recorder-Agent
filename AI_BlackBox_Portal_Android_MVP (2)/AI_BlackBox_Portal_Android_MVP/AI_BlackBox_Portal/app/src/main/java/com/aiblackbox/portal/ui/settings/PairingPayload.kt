package com.aiblackbox.portal.ui.settings

import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Settings > Pair New Device: the QR another device scans (read by PairingActivity).
 * Like the web Portal's pairing dialog, the token is minted for the operator who opened
 * it and the QR names that operator; with none resolved it names nobody, and the new
 * device resolves its operator from the box's roster (OperatorResolver). A device that
 * already has an operator keeps it (BlackBoxStore.adoptPairingOperatorInto).
 */
object PairingPayload {

    /** POST /pair/start body: `{"operator": ...}`, or `{}` while no operator is resolved. */
    fun startBody(operator: String): String = buildJsonObject {
        operator.trim().takeIf { it.isNotEmpty() }?.let { put("operator", it) }
    }.toString()

    /** The QR payload; without a [token] (the box did not mint one) only the origin and operator. */
    fun qr(token: String?, exp: Long, origin: String, operator: String): String = buildJsonObject {
        if (token != null) {
            put("type", "pair")
            put("token", token)
            put("exp", exp)
        }
        put("origin", origin)
        operator.trim().takeIf { it.isNotEmpty() }?.let { put("operator", it) }
    }.toString()
}
