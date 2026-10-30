package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.remote.RemoteResponse
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/**
 * The box -> phone wellness routes on the `:8765` listener (wire contract):
 *
 *   POST /wellness/bind    {operator, enrollment_id, epoch, nonce, k}  -> 200 | 400 | 403
 *   POST /wellness/sync    {operator, enrollment_id}                   -> 202 (expedited sync)
 *   POST /wellness/unbind  {operator, enrollment_id}                   -> 200 (state cleared)
 *
 * They run behind the listener's pre-body gate (tailnet peer, not this phone, the pairing
 * bearer, bounded body) and after `authorize()`, which already required the request operator
 * to equal [WellnessPort.bindOperator] (bind) or [WellnessPort.enrolledOperator] (sync,
 * unbind). This router re-checks nothing about the source; it validates the payload, and
 * for bind the live intent nonce. PURE over [WellnessPort] -> JVM-tested.
 */
interface WellnessPort {
    /** The operator the live intent names, "" when none (bind is refused). */
    fun bindOperator(): String

    /** The enrolled operator, "" when this phone is not a health source. */
    fun enrolledOperator(): String

    fun acceptBind(request: WellnessBindRequest): BindDecision

    fun current(): WellnessBindingRecord?

    /** Run an expedited sync soon (the worker debounces nothing for a box nudge). */
    fun requestSync()

    /** Forget everything (unbind). */
    fun clearAll()

    /** A bind just landed: schedule the backfill and the periodic worker. */
    fun onBound(record: WellnessBindingRecord)
}

const val WELLNESS_ROUTE_PREFIX = "/wellness/"
const val WELLNESS_BIND = "/wellness/bind"
const val WELLNESS_SYNC = "/wellness/sync"
const val WELLNESS_UNBIND = "/wellness/unbind"

private val ROUTE_JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true }

@Serializable private data class WellnessError(val error: String)
@Serializable private data class WellnessOk(val ok: Boolean, val state: String)
@Serializable private data class WellnessTarget(
    val operator: String = "",
    @SerialName("enrollment_id") val enrollmentId: String = "",
)

private fun err(status: Int, error: String) = RemoteResponse(status, ROUTE_JSON.encodeToString(WellnessError(error)))

/** Which operator `authorize()` must see on a wellness route, given the phone's state. */
fun wellnessOperatorFor(path: String, port: WellnessPort?): String = when {
    port == null -> ""
    path == WELLNESS_BIND -> port.bindOperator()
    path.startsWith(WELLNESS_ROUTE_PREFIX) -> port.enrolledOperator()
    else -> ""
}

fun routeWellness(method: String, path: String, body: String, port: WellnessPort?): RemoteResponse {
    if (port == null) return err(503, "wellness_unavailable")
    if (path != WELLNESS_BIND && path != WELLNESS_SYNC && path != WELLNESS_UNBIND) return err(404, "not found")
    if (!method.equals("POST", ignoreCase = true)) return err(405, "method not allowed")
    return when (path) {
        WELLNESS_BIND -> {
            val request = try {
                ROUTE_JSON.decodeFromString<WellnessBindRequest>(body.ifBlank { "{}" })
            } catch (e: Exception) {
                return err(400, "invalid JSON body")
            }
            when (val decision = port.acceptBind(request)) {
                is BindDecision.Rejected -> err(decision.status, decision.error)
                is BindDecision.Accepted -> {
                    runCatching { port.onBound(decision.record) }
                    RemoteResponse(200, ROUTE_JSON.encodeToString(WellnessOk(ok = true, state = "active")))
                }
            }
        }
        else -> {
            val target = try {
                ROUTE_JSON.decodeFromString<WellnessTarget>(body.ifBlank { "{}" })
            } catch (e: Exception) {
                return err(400, "invalid JSON body")
            }
            val current = port.current()
            if (path == WELLNESS_UNBIND && current == null)
                return RemoteResponse(200, ROUTE_JSON.encodeToString(WellnessOk(ok = true, state = "not_set_up")))
            if (current == null) return err(409, "not_enrolled")
            // The box names the enrollment it means; a stale or missing id never clears or
            // nudges the current one.
            if (target.enrollmentId != current.enrollmentId) return err(409, "enrollment_mismatch")
            if (path == WELLNESS_SYNC) {
                port.requestSync()
                RemoteResponse(202, ROUTE_JSON.encodeToString(WellnessOk(ok = true, state = "syncing")))
            } else {
                port.clearAll()
                RemoteResponse(200, ROUTE_JSON.encodeToString(WellnessOk(ok = true, state = "not_set_up")))
            }
        }
    }
}
