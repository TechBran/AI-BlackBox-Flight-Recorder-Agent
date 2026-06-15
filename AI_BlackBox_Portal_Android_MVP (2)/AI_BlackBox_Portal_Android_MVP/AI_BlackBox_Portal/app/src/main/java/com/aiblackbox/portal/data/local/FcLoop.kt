package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.fold
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * The on-device agent loop. Given a persona, the prior conversation, and a new
 * user message, it produces the model's streamed reply by generating through a
 * [LocalLlm] (text path, [runTurn]) or — in Phase 3 — by running a tiered,
 * two-hop tool loop through a [ToolCallingLlm] + [ToolBridge] ([runAgent]).
 *
 * **Phase 2 text path ([runTurn]) has NO tools.** Plain text generation: assemble
 * one prompt, hand it to [LocalLlm.generate], stream the deltas back. No tool-call
 * parsing, no dispatch — that lives in [runAgent].
 *
 * **Phase 3 tool path ([runAgent]).** The small on-device model never sees the
 * whole tool vault. Instead one always-resident [ResidentTools.SEARCH_TOOLS]
 * function lets it DISCOVER tools; the schemas search returns are injected
 * (capped at [ResidentTools.MAX_INJECTED_SCHEMAS]) as callable functions for the
 * NEXT model turn; the model then calls a discovered tool, which executes via the
 * [bridge], and the result is fed back as a [Role.TOOL] turn. [Turn]/[Role] are
 * the seam that grew to carry the TOOL role; [LlmEvent] is the richer turn type.
 * [runAgent] needs the [toolLlm]/[bridge] dependencies — the text-only
 * `FcLoop(llm)` constructor cannot run agents.
 */
class FcLoop(
    private val llm: LocalLlm,
    private val toolLlm: ToolCallingLlm? = null,
    private val bridge: ToolBridge? = null,
    private val operator: String = "system",
    private val resident: List<ToolSchema> = ResidentTools.resident(),
    private val maxIterations: Int = 8,
) {

    /** Who authored a conversation turn. TOOL carries a tool result fed back to the model. */
    enum class Role { USER, ASSISTANT, TOOL }

    /** One prior conversation turn fed into prompt assembly. Kept tiny and local
     * so FcLoop is not coupled to the UI message models (UiMessage/ChatMessage);
     * callers map their own messages into this shape. */
    data class Turn(val role: Role, val text: String)

    /**
     * Run a single conversation turn: assemble the prompt and stream the model's
     * reply deltas. The returned Flow is the [LocalLlm.generate] cold delta Flow
     * verbatim — collecting it starts generation; each emission is the next piece
     * of text (collectors concatenate). Streaming these deltas IS the Phase-2 turn
     * output.
     *
     * **Errors are NOT swallowed.** [LocalLlm.generate] may throw mid-stream
     * (e.g. a native engine fault or a not-loaded engine). FcLoop returns the
     * generate Flow without a masking `.catch`, so any such error propagates to
     * the collector and the caller (the ViewModel, Task 2.4) decides how to
     * surface it. The caller is responsible for handling collection errors.
     */
    fun runTurn(persona: String, history: List<Turn>, userMessage: String): Flow<String> =
        llm.generate(buildPrompt(persona, history, userMessage))

    /**
     * Convenience for tests and non-streaming callers: collect [runTurn] into the
     * full concatenated reply. Errors propagate the same way (this just folds the
     * deltas; it adds no `.catch`).
     */
    suspend fun complete(persona: String, history: List<Turn>, userMessage: String): String =
        runTurn(persona, history, userMessage).fold(StringBuilder()) { acc, delta ->
            acc.append(delta)
        }.toString()

    /**
     * Assemble the provider-neutral prompt. Format (deliberately SIMPLE and
     * plain-text):
     *
     * ```
     * <persona>
     *
     * User: <history[0].text>
     * Assistant: <history[1].text>
     * ...
     * User: <userMessage>
     * Assistant:
     * ```
     *
     * Persona leads, then each history [Turn] on its own line with a `User:` /
     * `Assistant:` role marker in order, then the new user message, then a
     * trailing `Assistant:` cue inviting the model to continue.
     *
     * This is a *textual* prompt only. The concrete LiteRT-LM engine (Task 2.6)
     * may re-wrap this content in Gemma's actual chat template / turn tokens
     * (`<start_of_turn>` etc.); FcLoop stays template-agnostic so it does not
     * over-fit one engine's tokenization.
     *
     * Internal so [FcLoopTest] can assert the structure directly.
     */
    internal fun buildPrompt(persona: String, history: List<Turn>, userMessage: String): String =
        buildAgentPrompt(persona, history + Turn(Role.USER, userMessage))

    /**
     * Assemble the provider-neutral prompt from the FULL turn list (already
     * including the new user turn). This is the shared prompt-assembly primitive:
     * [buildPrompt] is just `buildAgentPrompt(persona, history + userTurn)`, and
     * [runAgent] calls this each iteration with its growing working-turn list
     * (which may include [Role.TOOL] turns carrying tool results).
     *
     * Format (byte-for-byte identical to the original Phase-2 contract):
     * `persona + "\n\n"`, then each turn as `"<marker>: <text>\n"`, then a trailing
     * `"Assistant:"` cue.
     *
     * Internal so [FcLoopTest] can assert the structure directly.
     */
    internal fun buildAgentPrompt(persona: String, turns: List<Turn>): String {
        // SECURITY (Phase 4): history/user text is interpolated as plain text, so content containing literal "User:"/"Assistant:" lines is not distinguishable from real turn boundaries. Harmless for a single-user Phase 2 model, but once Phase 4 actuators + autonomy gate exist, a self-injected "Assistant:" turn could fabricate intent. Mitigation: Task 2.6's concrete engine should re-template into Gemma's real turn tokens (<start_of_turn>...) which structurally separate role from content.
        val sb = StringBuilder()
        sb.append(persona)
        sb.append("\n\n")
        for (turn in turns) {
            sb.append(turn.role.marker).append(": ").append(turn.text).append("\n")
        }
        sb.append(Role.ASSISTANT.marker).append(":")
        return sb.toString()
    }

    /**
     * Run the tiered, two-hop on-device tool loop, emitting a COLD [Flow] of
     * [LlmEvent]s. Requires [toolLlm] and [bridge] — the text-only `FcLoop(llm)`
     * constructor cannot run agents (an [IllegalArgumentException] fires on collect).
     *
     * Each iteration (up to [maxIterations]):
     *  1. Build the prompt from the working turns; offer `resident + injected`
     *     (deduped) tools — bounded because `injected` is already capped at
     *     [ResidentTools.MAX_INJECTED_SCHEMAS].
     *  2. Stream one model turn ([toolLlm]); re-emit its [LlmEvent.TextDelta] /
     *     [LlmEvent.ToolCall] events (a defensively-emitted ToolOutcome from the
     *     seam is ignored — the seam shouldn't produce one).
     *  3. No tool calls → the model gave its final answer; record it and complete.
     *  4. Otherwise dispatch each call in order: [ResidentTools.SEARCH_TOOLS]
     *     discovers + injects (capped) schemas for the NEXT turn; any other call
     *     executes via the [bridge]. Each dispatch emits a [LlmEvent.ToolOutcome]
     *     and appends a [Role.TOOL] turn so the model sees the result next turn.
     *
     * If [maxIterations] is exhausted, the flow completes gracefully (no hang, no
     * throw); the events already emitted are the output.
     *
     * **Errors are NOT swallowed.** Exceptions from [toolLlm] or [bridge]
     * propagate to the collector (graceful offline handling is a SEPARATE later
     * task; not built here).
     */
    fun runAgent(persona: String, history: List<Turn>, userMessage: String): Flow<LlmEvent> = flow {
        val tools = requireNotNull(toolLlm) {
            "FcLoop.runAgent requires a ToolCallingLlm; construct FcLoop with toolLlm + bridge (the text-only FcLoop(llm) cannot run agents)."
        }
        val toolBridge = requireNotNull(bridge) {
            "FcLoop.runAgent requires a ToolBridge; construct FcLoop with toolLlm + bridge (the text-only FcLoop(llm) cannot run agents)."
        }

        var working = history + Turn(Role.USER, userMessage)
        var injected = emptyList<ToolSchema>()

        repeat(maxIterations) {
            val prompt = buildAgentPrompt(persona, working)
            val available = (resident + injected).distinctBy { it.name } // bounded: injected already <= MAX

            val assistantText = StringBuilder()
            val pendingCalls = mutableListOf<LlmEvent.ToolCall>()

            tools.generateWithTools(prompt, available).collect { event ->
                when (event) {
                    is LlmEvent.TextDelta -> {
                        emit(event)
                        assistantText.append(event.text)
                    }
                    is LlmEvent.ToolCall -> {
                        emit(event)
                        pendingCalls.add(event)
                    }
                    // Defensive: the seam shouldn't emit ToolOutcome — ignore if it does.
                    is LlmEvent.ToolOutcome -> Unit
                }
            }

            if (pendingCalls.isEmpty()) {
                // Final answer: record for prompt coherence, then complete the flow.
                working = working + Turn(Role.ASSISTANT, assistantText.toString())
                return@flow
            }

            for (call in pendingCalls) {
                // Keep the textual prompt coherent: note that this call happened.
                working = working + Turn(Role.ASSISTANT, "[called ${call.name}]")

                if (call.name == ResidentTools.SEARCH_TOOLS) {
                    // A small on-device model can misfire this arg (omit it, send it
                    // blank, or send a JSON object/array). That's a MODEL error, not an
                    // offline error — a blank query 400s the real backend (see
                    // ToolBridgeClient.searchTools), and `.jsonPrimitive` would THROW on
                    // a non-primitive. Guard both: emit a failure outcome, feed it back as
                    // a TOOL turn, and let the model retry next turn — exactly like the
                    // execute-failure path. (Do NOT abort the whole run.)
                    val query = (call.args["query"] as? JsonPrimitive)?.contentOrNull
                        ?.takeIf { it.isNotBlank() }
                    if (query == null) {
                        emit(
                            LlmEvent.ToolOutcome(
                                ResidentTools.SEARCH_TOOLS,
                                ToolResult(success = false, result = JsonPrimitive("query required")),
                            ),
                        )
                        working = working + Turn(Role.TOOL, "search_tools error: query required")
                        continue // skip dispatching THIS call; the loop proceeds
                    }
                    // k is tied to the injection cap so the two can't silently drift.
                    val found = toolBridge.searchTools(query, k = ResidentTools.MAX_INJECTED_SCHEMAS)
                    // TIERING — never dump the whole result set into the next turn.
                    injected = (injected + found)
                        .distinctBy { it.name }
                        .take(ResidentTools.MAX_INJECTED_SCHEMAS)
                    val foundNames = JsonArray(found.map { JsonPrimitive(it.name) })
                    emit(
                        LlmEvent.ToolOutcome(
                            ResidentTools.SEARCH_TOOLS,
                            ToolResult(success = true, result = foundNames),
                        ),
                    )
                    working = working + Turn(
                        Role.TOOL,
                        "search_tools found: ${found.joinToString { it.name }}",
                    )
                } else {
                    val res = toolBridge.execute(call.name, call.args, operator)
                    emit(LlmEvent.ToolOutcome(call.name, res))
                    // Prefer the unquoted string content for the common string case so a
                    // JsonPrimitive("hello") feeds back as `hello`, not `"hello"`, into the
                    // prompt. (Emitted events are unchanged — this only affects prompt text.)
                    val resultText = (res.result as? JsonPrimitive)?.contentOrNull
                        ?: res.result?.toString() ?: "ok"
                    working = working + Turn(Role.TOOL, "${call.name} → $resultText")
                }
            }
            // Loop again: the model sees the appended tool results next turn.
        }
        // maxIterations exhausted — complete gracefully (do not hang, do not throw).
        // TODO(3.3): the collector gets no terminal signal here; surface a "stopped at
        // maxIterations" event/marker once tool-call/result rendering lands.
    }

    /** Role marker used in the assembled prompt ("User" / "Assistant" / "Tool"). */
    private val Role.marker: String
        get() = when (this) {
            Role.USER -> "User"
            Role.ASSISTANT -> "Assistant"
            Role.TOOL -> "Tool"
        }
}
