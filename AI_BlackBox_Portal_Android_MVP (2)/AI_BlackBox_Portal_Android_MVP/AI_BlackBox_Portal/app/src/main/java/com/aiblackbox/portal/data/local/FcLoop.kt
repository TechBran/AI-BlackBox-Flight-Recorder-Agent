package com.aiblackbox.portal.data.local

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.fold

/**
 * The on-device agent loop. Given a persona, the prior conversation, and a new
 * user message, it produces the model's streamed reply by generating through a
 * [LocalLlm].
 *
 * **Phase 2 has NO tools.** This is plain text generation: assemble one prompt,
 * hand it to [LocalLlm.generate], and stream the resulting deltas straight back
 * to the caller. There is intentionally no tool-call parsing, no FC-SDK, no
 * dispatch loop here yet — that is Phase 3.
 *
 * **Phase-3 extensibility (no speculative code now).** The loop is deliberately
 * shaped as *build a prompt → stream a model turn*. Phase 3's on-device function
 * calling (the AI Edge Function Calling SDK) layers on top by wrapping
 * [runTurn]'s single generation in a multi-step loop: inspect the streamed turn
 * for tool calls, dispatch them, append the results to [history] as new [Turn]s,
 * and call the model again until it stops requesting tools. [Turn]/[Role] are the
 * seam that grows to carry a TOOL role / tool-result payloads when that lands.
 * Nothing tool-related is added now (YAGNI).
 */
class FcLoop(private val llm: LocalLlm) {

    /** Who authored a conversation turn. Phase 3 may add a TOOL role. */
    enum class Role { USER, ASSISTANT }

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
    internal fun buildPrompt(persona: String, history: List<Turn>, userMessage: String): String {
        // SECURITY (Phase 4): history/user text is interpolated as plain text, so content containing literal "User:"/"Assistant:" lines is not distinguishable from real turn boundaries. Harmless for a single-user, no-tools Phase 2 model, but once Phase 4 actuators + autonomy gate exist, a self-injected "Assistant:" turn could fabricate intent. Mitigation: Task 2.6's concrete engine should re-template into Gemma's real turn tokens (<start_of_turn>...) which structurally separate role from content.
        val sb = StringBuilder()
        sb.append(persona)
        sb.append("\n\n")
        for (turn in history) {
            sb.append(turn.role.marker).append(": ").append(turn.text).append("\n")
        }
        sb.append(Role.USER.marker).append(": ").append(userMessage).append("\n")
        sb.append(Role.ASSISTANT.marker).append(":")
        return sb.toString()
    }

    /** Role marker used in the assembled prompt ("User" / "Assistant"). */
    private val Role.marker: String
        get() = when (this) {
            Role.USER -> "User"
            Role.ASSISTANT -> "Assistant"
        }
}
