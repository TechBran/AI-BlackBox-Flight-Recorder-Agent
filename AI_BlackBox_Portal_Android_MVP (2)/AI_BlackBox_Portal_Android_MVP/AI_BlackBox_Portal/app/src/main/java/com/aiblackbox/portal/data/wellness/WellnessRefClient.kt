package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlin.coroutines.cancellation.CancellationException

/**
 * Show numbers' one network step: `GET /wellness/ref/{ref}?operator=<op>` for each ref of a
 * masked turn, through [api] (its client derives from HttpClients.base, whose
 * [WellnessSigningInterceptor] signs the call when this phone is [operator]'s enrolled source;
 * unsigned, the box answers 401 and the sheet says "Not available").
 *
 * The answers go straight into [ShowNumbersResult] and nowhere else: nothing is logged,
 * cached or persisted.
 */
object WellnessRefClient {

    suspend fun fetch(api: BlackBoxApi, refs: List<String>, operator: String): ShowNumbersResult {
        val wanted = refs.filter { WellnessChatRules.REF_RE.matches(it) }.distinct().take(WellnessChatRules.MAX_REFS)
        if (wanted.isEmpty() || operator.isBlank()) return ShowNumbersResult.NotAvailable
        val replies = wanted.map { ref ->
            try {
                200 to api.get(WellnessChatRules.refPath(ref, operator))
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiHttpException) {
                e.code to ""
            } catch (_: Exception) {
                -1 to ""
            }
        }
        return ShowNumbersResult.of(replies)
    }
}
