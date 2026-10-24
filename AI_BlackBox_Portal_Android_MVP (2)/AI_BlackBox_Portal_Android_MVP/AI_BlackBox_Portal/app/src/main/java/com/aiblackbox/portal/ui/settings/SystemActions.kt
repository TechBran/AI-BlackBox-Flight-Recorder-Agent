package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.ui.updates.RestartWait
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Result wording for Settings → System's Restart Service and Cancel Tasks
 * (audit A11-settings-generation-13). Both used to fire on one tap and drop
 * the response, so a restart sudo refused looked like one that worked. They
 * now confirm first (SettingsSheet) and report what the backend said. Pure.
 */
object SystemActions {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** Toast text from POST /tasks/cancel-all's {"cancelled": N}. */
    fun cancelledMessage(body: String): String {
        val n = try {
            json.parseToJsonElement(body).jsonObject["cancelled"]?.jsonPrimitive?.intOrNull
        } catch (_: Exception) {
            null
        }
        return when (n) {
            null -> "Cancel request sent."
            0 -> "No pending or running tasks to cancel."
            1 -> "Cancelled 1 task."
            else -> "Cancelled $n tasks."
        }
    }

    /** The failure POST /restart reported in a 200 body ({"status": "error"},
     *  e.g. sudo refused), or null when it did not report one. */
    fun restartRefusal(body: String): String? {
        val obj = try {
            json.parseToJsonElement(body).jsonObject
        } catch (_: Exception) {
            return null
        }
        if (obj["status"]?.jsonPrimitive?.contentOrNull != "error") return null
        val why = obj["message"]?.jsonPrimitive?.contentOrNull
            ?: obj["error"]?.jsonPrimitive?.contentOrNull
            ?: "unknown error"
        return if (why.startsWith("Restart failed")) why else "Restart failed: $why"
    }

    fun restartOutcomeMessage(outcome: RestartWait.Outcome): String = when (outcome) {
        is RestartWait.Outcome.Restarted ->
            "BlackBox is back online (restarted in ${outcome.elapsedMs / 1000}s)."
        is RestartWait.Outcome.NotRestarted ->
            "BlackBox did not restart: it is still the process that was running before."
        is RestartWait.Outcome.StillDown ->
            "BlackBox is still down after ${outcome.elapsedMs / 1000}s. Check journalctl -u blackbox.service on the box."
        is RestartWait.Outcome.GaveUp -> "BlackBox did not restart."
        is RestartWait.Outcome.StillRunning ->
            "An update is still running on BlackBox; it restarts the service when it finishes."
    }
}
