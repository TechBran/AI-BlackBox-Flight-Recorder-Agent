package com.aiblackbox.portal.overlay

import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.repository.LOST_CONNECTION_MESSAGE
import com.aiblackbox.portal.data.repository.TaskPollOutcome
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * The overlay panel's reply text for a finished POST /chat task (rank 76: the overlay's
 * own /tasks/{id} poll loop is replaced by the shared TaskPoller). The reply sits in
 * result_data under one of ui_reply, reply, text or response; a task with none of them
 * falls back to its result_url.
 */
internal object OverlayChatReply {

    private val REPLY_KEYS = listOf("ui_reply", "reply", "text", "response")

    fun replyOf(status: TaskStatus): String? {
        val data = status.resultData as? JsonObject
        for (key in REPLY_KEYS) {
            val v = data?.get(key) as? JsonPrimitive ?: continue
            if (v.isString) return v.content
        }
        return status.resultUrl
    }

    /**
     * What the panel shows when the poll ends: the reply, or an "Error: ..." line. The
     * failure text is the box's error_message (the loop this replaces read `error`, a key
     * the box does not send, so every failure said "Unknown error").
     */
    fun textFor(outcome: TaskPollOutcome): String = when (outcome) {
        is TaskPollOutcome.Completed -> replyOf(outcome.status) ?: "Task completed but no response"
        is TaskPollOutcome.Failed -> "Error: ${outcome.message}"
        TaskPollOutcome.Cancelled -> "Error: the task was cancelled"
        TaskPollOutcome.LostConnection -> "Error: $LOST_CONNECTION_MESSAGE"
    }

    /** How long the panel waits for a reply before saying the request timed out. */
    const val CEILING_MS = 120_000L
}
