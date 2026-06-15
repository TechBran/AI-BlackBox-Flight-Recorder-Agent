package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

/**
 * In-test [ToolCallingLlm] double for [FcLoop.runAgent]. Stands in for the deferred
 * LiteRtEngine (Task 2.6) so the tool loop is exercisable offline, on the JVM, with
 * no AI Edge SDK and no device.
 *
 * **Scripted by turn index:** [script] is a list of per-turn event lists. The Nth
 * call to [generateWithTools] emits `script[N]` as a cold flow. Scripts contain
 * ONLY [LlmEvent.TextDelta] / [LlmEvent.ToolCall] — the seam never emits
 * [LlmEvent.ToolOutcome] (the loop does).
 *
 * **Past the end of the script:** the LAST scripted event list is repeated. This
 * lets tests model a model that "always asks for a tool" (for the maxIterations
 * guard) with a one-element script, and means a finite script's terminal turn keeps
 * driving the loop the same way. (If [script] is empty, each turn emits nothing.)
 *
 * **Records** [toolsPerTurn] (the `tools` arg of each call, in order) and [prompts]
 * (the `prompt` arg of each call, in order) for assertions.
 */
class FakeToolCallingLlm(
    private val script: List<List<LlmEvent>>,
) : ToolCallingLlm {

    /** The `tools` list passed to each [generateWithTools] call, in order. */
    val toolsPerTurn: MutableList<List<ToolSchema>> = mutableListOf()

    /** The `prompt` passed to each [generateWithTools] call, in order. */
    val prompts: MutableList<String> = mutableListOf()

    private var turn = 0

    override fun generateWithTools(prompt: String, tools: List<ToolSchema>): Flow<LlmEvent> = flow {
        // Record at collection time (cold Flow): the turn is "used" only when the
        // stream is actually consumed, mirroring real generation.
        prompts.add(prompt)
        toolsPerTurn.add(tools)
        val events = when {
            script.isEmpty() -> emptyList()
            turn < script.size -> script[turn]
            else -> script.last() // repeat the terminal turn past the script's end
        }
        turn++
        for (event in events) emit(event)
    }
}
