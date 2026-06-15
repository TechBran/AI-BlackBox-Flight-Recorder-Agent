package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.coroutines.flow.Flow

/**
 * The tool-aware generation seam for the Phase-3 on-device agent loop. Given a
 * prompt and the tool schemas currently callable, it streams ONE model turn as a
 * cold [Flow] of [LlmEvent]s — emitting [LlmEvent.TextDelta] and/or
 * [LlmEvent.ToolCall], and NEVER [LlmEvent.ToolOutcome] (the loop, not the model,
 * produces outcomes).
 *
 * **Deliberately SEPARATE from [LocalLlm.generate].** The text seam is frozen so
 * Phase 3 does not disturb it (see [LocalLlm]'s KDoc). The deferred concrete
 * LiteRtEngine (Task 2.6) will implement BOTH [LocalLlm] and [ToolCallingLlm]: the
 * AI Edge Function Calling SDK returns exactly this text-or-function-call shape, so
 * the two seams coexist on one engine. No concrete implementation lives here —
 * fakes in the test source set drive it.
 */
interface ToolCallingLlm {

    /**
     * Stream one model turn given [prompt] and the CURRENTLY-AVAILABLE [tools].
     *
     * A **cold** Flow: collecting it starts a generation. Emits
     * [LlmEvent.TextDelta] and/or [LlmEvent.ToolCall]; it must NEVER emit
     * [LlmEvent.ToolOutcome]. Completes when the turn finishes.
     */
    fun generateWithTools(prompt: String, tools: List<ToolSchema>): Flow<LlmEvent>
}
