package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

/**
 * DTOs for the server-bracketed on-device turn:
 *   1. POST /local/turn/prepare   ({@link PrepareRequest})  → {@link PrepareResponse}
 *      — the hub assembles a ready package (system prompt + relevant tools +
 *        memory provenance + budget) BEFORE the on-device model runs.
 *   2. (phone runs the local Gemma model)
 *   3. POST /local/turn/complete  ({@link CompleteRequest}) → {@link CompleteResponse}
 *      — the phone reports the final answer + tool transcript; the hub mints the
 *        immutable snapshot and may trigger a checkpoint.
 *
 * Backend shape: Orchestrator/routes/local_routes.py (local_turn_prepare /
 * local_turn_complete).
 *
 * DECODE-SAFETY INVARIANT: EVERY field is defaulted. The app's lenient Json
 * (ignoreUnknownKeys + isLenient) means a missing OR extra wire field can never
 * throw during decode and fault a turn. Multi-word wire keys are snake_case and
 * carried via @SerialName on a camelCase Kotlin property, matching the house
 * convention in LocalBundle.kt. The `tools` field REUSES {@link ToolSchema}
 * (ToolBridge.kt) — its {name, description, parameters} shape is exactly the
 * prepare response's tool items.
 */

/** Body for POST /local/turn/prepare: `{"prompt": str, "operator": str}`. */
@Serializable
data class PrepareRequest(
    val prompt: String = "",
    val operator: String = "",
)

/**
 * The `provenance` block of a prepare response: which prior snapshots seeded the
 * package, split by retrieval source.
 *   `semantic`   — snapshot ids surfaced by embedding similarity.
 *   `checkpoint` — snapshot ids pulled from the latest checkpoint bundle.
 */
@Serializable
data class TurnProvenance(
    val semantic: List<String> = emptyList(),
    val checkpoint: List<String> = emptyList(),
)

/**
 * The `budget` block of a prepare response: how big the assembled package is
 * versus the cap. `capChars` defaults to 16000 (the on-device context budget) so
 * a missing field never reports an unbounded/zero cap.
 */
@Serializable
data class TurnBudget(
    @SerialName("package_chars") val packageChars: Int = 0,
    @SerialName("cap_chars") val capChars: Int = 16000,
)

/**
 * POST /local/turn/prepare →
 * `{"success", "turn_id", "system_prompt", "tools": [...], "provenance": {...},
 *   "budget": {...}}`.
 */
@Serializable
data class PrepareResponse(
    val success: Boolean = false,
    @SerialName("turn_id") val turnId: String = "",
    @SerialName("system_prompt") val systemPrompt: String = "",
    val tools: List<ToolSchema> = emptyList(),
    val provenance: TurnProvenance = TurnProvenance(),
    val budget: TurnBudget = TurnBudget(),
)

/**
 * One executed tool call in the on-device turn's transcript, as sent inside the
 * complete request's `tool_transcript`: `{"name", "args": {...}, "result": str}`.
 *
 * `args` is the (possibly empty) argument object the model passed, carried as a
 * raw [JsonObject] so any tool's argument shape passes through untyped.
 */
@Serializable
data class ToolCallRecord(
    val name: String = "",
    val args: JsonObject = JsonObject(emptyMap()),
    val result: String = "",
)

/**
 * Body for POST /local/turn/complete:
 * `{"turn_id", "operator", "prompt", "final_response", "tool_transcript": [...]}`.
 */
@Serializable
data class CompleteRequest(
    @SerialName("turn_id") val turnId: String = "",
    val operator: String = "",
    val prompt: String = "",
    @SerialName("final_response") val finalResponse: String = "",
    @SerialName("tool_transcript") val toolTranscript: List<ToolCallRecord> = emptyList(),
)

/**
 * POST /local/turn/complete →
 * `{"success", "snap_id", "checkpoint_triggered"}`.
 */
@Serializable
data class CompleteResponse(
    val success: Boolean = false,
    @SerialName("snap_id") val snapId: String = "",
    @SerialName("checkpoint_triggered") val checkpointTriggered: Boolean = false,
)
