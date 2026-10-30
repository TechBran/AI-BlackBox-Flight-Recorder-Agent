package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi

/**
 * The Timeline's and the SNAP peek sheet's ledger read, `GET /fossil/snapshot/<id>` (spec §6.1
 * "Chat": Show numbers on a turn reloaded from the ledger), opted in to
 * [WellnessSigningInterceptor] for the operator on the app's screen. On the enrolled phone, with
 * its own operator on screen, the read is signed and the box reads the snapshot as that person's
 * own screen: a health turn of theirs comes back as minted, masked ("[·]") with its WELLNESS
 * gauge, so [WellnessChatRules.offersShowNumbers] finds its refs. Anywhere else it is unsigned
 * and exactly what it was before Health (a tagged turn reads "[wellness conversation · not shared]").
 *
 * A proof the box refuses (401 / 409 / 410: a drifted clock, an owner change, a revoked key) is
 * answered with the refusal, never with an unsigned read, so the read asks ONCE more unsigned:
 * the Timeline never shows less than before. Nothing here logs the id or the text.
 */
object WellnessLedgerRead {

    suspend fun fetch(api: BlackBoxApi, snapId: String, screenOperator: String?): String {
        // An id the allow-list does not take (a lower-case "snap-1" link) is read as it always was.
        val path = WellnessChatRules.ledgerPath(snapId) ?: return api.get(WellnessChatRules.LEDGER_PREFIX + snapId)
        if (screenOperator.isNullOrBlank()) return api.get(path)
        return try {
            api.get(path, signHealth = true, signFor = screenOperator)
        } catch (e: ApiHttpException) {
            if (!WellnessChatRules.proofRefused(e.code)) throw e
            api.get(path)
        }
    }
}
