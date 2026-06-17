package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import kotlinx.serialization.json.JsonObject

/**
 * The richer turn type for the Phase-3 on-device agent loop. Where Phase 2's
 * [LocalLlm.generate] streams only plain text deltas, a tool-aware model turn can
 * emit either text OR a request to call a function — and the loop ([FcLoop.runAgent])
 * then dispatches that call and feeds the result back. [LlmEvent] models all three
 * shapes that flow out of an agent turn.
 *
 * **Who emits what:**
 *  - The LLM seam ([ToolCallingLlm.generateWithTools]) emits ONLY [TextDelta] and
 *    [ToolCall] — exactly the text-or-function-call shape the AI Edge Function
 *    Calling SDK produces. It NEVER emits [ToolOutcome].
 *  - [FcLoop.runAgent] re-emits the seam's TextDelta/ToolCall AND additionally emits
 *    a [ToolOutcome] after it dispatches each [ToolCall] (via the [ToolBridge]).
 *
 * These are the events Task 3.3 will render in the chat UI; no UI is built here.
 */
sealed interface LlmEvent {

    /** Next piece of assistant TEXT (a delta, like [LocalLlm.generate]). */
    data class TextDelta(val text: String) : LlmEvent

    /**
     * The model is requesting a function call. [args] is the raw JSON-object
     * arguments the model supplied (passed through to the bridge verbatim).
     */
    data class ToolCall(val name: String, val args: JsonObject) : LlmEvent

    /**
     * The RESULT of dispatching a [ToolCall]. Emitted by the loop ([FcLoop]),
     * NEVER by the LLM seam.
     */
    data class ToolOutcome(val name: String, val result: ToolResult) : LlmEvent
}
