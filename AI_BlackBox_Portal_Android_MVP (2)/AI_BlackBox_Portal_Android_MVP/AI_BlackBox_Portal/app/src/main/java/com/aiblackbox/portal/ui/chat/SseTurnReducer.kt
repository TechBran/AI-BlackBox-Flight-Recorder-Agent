package com.aiblackbox.portal.ui.chat

import android.util.Log
import com.aiblackbox.portal.data.api.SSEEvent
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.data.model.TokenCount
import com.aiblackbox.portal.data.wellness.WellnessChatRules
import com.aiblackbox.portal.data.wellness.WellnessUsed
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

private const val TAG = "ChatVM"

/**
 * "The Signal" HUD's per-turn state (presentation only, never persisted or minted): the
 * last tool named, whether the one-shot "generating ·" line has shown, the real model id
 * for it, and whether the line is kept dark for the whole turn (computer use, as on web).
 */
data class SignalState(
    val lastTool: String = "",
    val generating: Boolean = false,
    val modelLabel: String = "model",
    val suppressed: Boolean = false,
)

/**
 * Everything one /chat/stream turn has accumulated (the old sendViaSSE locals and turn
 * flags). [content] is the REAL reply; SSE "error" text accumulates apart in [errors], so
 * an error-only stream finalizes as a failure and error text is never minted.
 */
data class SseTurnState(
    val content: String = "",
    val reasoning: String = "",
    val errors: String = "",
    val mediaTasks: List<String> = emptyList(),
    val model: String? = null,
    val tokens: TokenCount? = null,
    val provenance: Provenance? = null,
    // cu_queued: the box queued this prompt behind a running computer-use task, whose
    // stream carries the reply (cu_queue_next), so this turn has no reply of its own.
    val queued: Boolean = false,
    // cu_queue_next: this stream now also carries the next queued prompt's reply,
    // appended after a separator, so "done" must not replace the accumulated content.
    val queueAppend: Boolean = false,
    val signal: SignalState = SignalState(),
    // M7: stream_start's `wellness_turn` (the turn key /chat/save echoes so the ledger copy
    // masks exactly this turn's health results), and what the turn's UI-only wellness_used
    // frames said (the bubble's footer, its wellness_ref). Neither is ever reply text.
    val wellnessTurn: String? = null,
    val wellnessUsed: WellnessUsed? = null,
)

/** A write to the Signal line: leave it, show [Show.label], or clear it. */
sealed interface SignalUpdate {
    data object Unchanged : SignalUpdate
    data class Show(val label: String) : SignalUpdate
    data object Clear : SignalUpdate
}

/** The CU state an event writes; null = unchanged. */
data class CuUpdate(
    val status: String? = null,
    val step: Int? = null,
    val stepTotal: Int? = null,
    val actionLabel: String? = null,
    val sessionId: String? = null,
    val deviceId: String? = null,
    val screenshotUrl: String? = null,
)

/** The robotics ER state an event writes; null = unchanged. */
data class ErUpdate(
    val missionActive: Boolean? = null,
    val status: String? = null,
    val cameraFrame: String? = null,
    val camera: String? = null,
    val reasoning: String? = null,
)

/** What one event leaves: the turn's new state and the UI state it writes. */
data class SseStep(
    val turn: SseTurnState,
    val chatState: ChatState? = null,
    val signal: SignalUpdate = SignalUpdate.Unchanged,
    val cu: CuUpdate = CuUpdate(),
    val er: ErUpdate = ErUpdate(),
)

/**
 * The chat's /chat/stream event handler as a PURE reducer (audit A04-chat-viewmodel-17/-18,
 * rank 76): events in, turn and UI state out, aligned with the Portal's chat-send.js
 * handler. It was a 35-branch switch inside ChatViewModel mutating ten flows, untestable
 * on the JVM; ChatViewModel now folds each event through [reduce] and writes the
 * [SseStep] it returns, and ChatSseGoldenTest pins that a turn leaves exactly what it did.
 *
 * [chatState] is the chat's state when the event arrives (some events only move THINKING
 * to STREAMING); [provider] the turn's provider; [baseUrl] the box (a relative CU
 * screenshot URL is joined onto it); [nowMs] the cache-buster that URL gets.
 */
object SseTurnReducer {

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private fun obj(data: String): JsonObject = json.parseToJsonElement(data).jsonObject

    /** `jsonPrimitive.content` of [key] (a JSON null reads as "null", as it always has); null when absent. */
    private fun JsonObject.str(key: String): String? = this[key]?.jsonPrimitive?.content

    private fun JsonObject.int(key: String): Int? = str(key)?.toIntOrNull()

    /** The turn's first state: the Signal labelled with the sent model until stream_start names the real one. */
    fun start(provider: String, model: String): SseTurnState = SseTurnState(
        signal = SignalState(
            modelLabel = model.ifBlank { provider }.ifBlank { "model" },
            suppressed = ChatViewModel.signalSuppressedForProvider(provider),
        ),
    )

    fun reduce(
        state: SseTurnState,
        event: SseEventIn,
        chatState: ChatState,
        provider: String,
        baseUrl: String,
        nowMs: Long,
    ): SseStep = Reduction(state, chatState, provider, baseUrl, nowMs).apply(event)

    /** [reduce] over an [SSEEvent] as SSEClient parses it. */
    fun reduce(
        state: SseTurnState,
        event: SSEEvent,
        chatState: ChatState,
        provider: String,
        baseUrl: String,
        nowMs: Long,
    ): SseStep = reduce(state, SseEventIn(event.event, event.data), chatState, provider, baseUrl, nowMs)

    /** One event's name and decoded data. */
    data class SseEventIn(val event: String, val data: String)

    /** The working copy one event is folded into. */
    private class Reduction(
        var turn: SseTurnState,
        val chatState: ChatState,
        val provider: String,
        val baseUrl: String,
        val nowMs: Long,
    ) {
        var newChatState: ChatState? = null
        var signal: SignalUpdate = SignalUpdate.Unchanged
        var cu = CuUpdate()
        var er = ErUpdate()

        /** The chat state as the handler sees it, with this event's own writes so far. */
        val currentChatState: ChatState get() = newChatState ?: chatState

        fun append(text: String) {
            turn = turn.copy(content = turn.content + text)
        }

        /** The ONLY path that shows a label: a no-op while the turn's Signal is dark. */
        fun pushSignal(label: String) {
            if (!turn.signal.suppressed) signal = SignalUpdate.Show(label)
        }

        /**
         * The "generating · <model>" line, exactly once per turn (content_start or the
         * first content bytes); the latch is set even on a dark turn.
         */
        fun signalOnGenerating() {
            if (!turn.signal.generating) {
                turn = turn.copy(signal = turn.signal.copy(generating = true))
                pushSignal("generating · ${turn.signal.modelLabel}")
            }
        }

        fun meta(model: String? = null, tokens: TokenCount? = null, provenance: Provenance? = null) {
            if (model != null) turn = turn.copy(model = model)
            if (tokens != null) turn = turn.copy(tokens = tokens)
            if (provenance != null) turn = turn.copy(provenance = provenance)
        }

        fun apply(event: SseEventIn): SseStep {
            val data = event.data
            when (event.event) {
                // ── Stream lifecycle ──
                "stream_start" -> try {
                    val o = obj(data)
                    val model = o.str("model")
                    // The Signal: remember the REAL model id for the "generating ·" line.
                    if (!model.isNullOrBlank()) turn = turn.copy(signal = turn.signal.copy(modelLabel = model))
                    // Provenance is embedded in stream_start.data.provenance — the box sends no
                    // standalone "provenance" event for /chat/stream (that branch is defensive).
                    val prov = ChatViewModel.extractProvenanceFromStreamStart(data)
                    meta(model = model, provenance = prov)
                    // M7: present only for an operator with a health store (else byte-identical).
                    (o["wellness_turn"] as? JsonPrimitive)?.takeIf { it.isString && it.content.isNotBlank() }
                        ?.let { turn = turn.copy(wellnessTurn = it.content) }
                    if (prov != null) {
                        Log.d(TAG, "stream_start provenance: recent=${prov.recent.size} " +
                            "keyword=${prov.keyword.size} semantic=${prov.semantic.size} " +
                            "checkpoint=${prov.checkpoint.size}")
                    }
                } catch (_: Exception) {}
                "content_start" -> {
                    // Transition from thinking to content (Portal uses this as marker)
                    if (currentChatState == ChatState.THINKING) newChatState = ChatState.STREAMING
                    signalOnGenerating()
                }
                "content" -> {
                    signalOnGenerating() // first answer bytes → narrate generation (once)
                    append(data)
                    if (currentChatState == ChatState.THINKING) newChatState = ChatState.STREAMING
                }
                "stream_end" -> {
                    newChatState = ChatState.IDLE
                    signal = SignalUpdate.Clear // dissolve The Signal (ephemeral)
                }
                "done" -> {
                    // "done" carries the full accumulated response as fallback.
                    // Gemini: {"thinking": "...", "content": "..."}
                    // Anthropic/OpenAI: {"reasoning": "...", "content": "..."}
                    try {
                        val o = obj(data)
                        val doneContent = o.str("content")
                        val doneThinking = o.str("thinking") ?: o.str("reasoning")
                        // Use done content if our streaming buffer is empty or shorter —
                        // except after cu_queue_next, when the buffer holds two replies.
                        if (!turn.queueAppend && !doneContent.isNullOrBlank() && doneContent.length > turn.content.length) {
                            turn = turn.copy(content = doneContent)
                        }
                        if (!doneThinking.isNullOrBlank() && doneThinking.length > turn.reasoning.length) {
                            turn = turn.copy(reasoning = doneThinking)
                        }
                    } catch (_: Exception) {
                        // done data might be a plain string — ignore parse failure
                    }
                    newChatState = ChatState.IDLE
                    signal = SignalUpdate.Clear // dissolve The Signal (ephemeral)
                    // Clear ER mission state when stream completes
                    if (provider == "robotics") er = er.copy(missionActive = false)
                }

                // ── Thinking/reasoning ──
                "thinking_start" -> newChatState = ChatState.THINKING
                "thinking" -> {
                    turn = turn.copy(reasoning = turn.reasoning + data)
                    if (currentChatState != ChatState.THINKING) newChatState = ChatState.THINKING
                }
                "thinking_end" -> {
                    // Thinking phase complete — content will follow; STREAMING on the next content.
                }

                // ── Usage & metadata ──
                "usage" -> try {
                    val o = obj(data)
                    meta(tokens = TokenCount(o.int("prompt_tokens") ?: 0, o.int("completion_tokens") ?: 0))
                } catch (_: Exception) {}
                "provenance" -> {
                    val parsed = ChatViewModel.parseProvenance(data)
                    if (parsed != null) {
                        meta(provenance = parsed)
                        Log.d(TAG, "provenance: recent=${parsed.recent.size} keyword=${parsed.keyword.size} " +
                            "semantic=${parsed.semantic.size} checkpoint=${parsed.checkpoint.size}")
                    } else {
                        Log.w(TAG, "provenance event unparseable: ${data.take(200)}")
                    }
                }

                // ── Media generation tasks ──
                "image_task", "video_task", "music_task" -> {
                    // Prefix with type so the placeholder knows which animation to show
                    val typePrefix = event.event.removeSuffix("_task") + ":"
                    try {
                        val taskId = obj(data).str("task_id")
                        if (taskId != null) {
                            turn = turn.copy(mediaTasks = turn.mediaTasks + "$typePrefix$taskId")
                            Log.d(TAG, "${event.event}: $taskId")
                        }
                    } catch (_: Exception) {
                        // task_id might be sent as plain string
                        if (data.isNotBlank()) turn = turn.copy(mediaTasks = turn.mediaTasks + "$typePrefix${data.trim()}")
                    }
                }

                // ── Computer Use events — matches Portal chat-send.js ──
                "cu_screenshot" -> {
                    try {
                        val o = obj(data)
                        val url = o.str("url")
                        if (url != null) {
                            val fullUrl = if (url.startsWith("http")) url else "$baseUrl$url"
                            cu = cu.copy(screenshotUrl = "$fullUrl?t=$nowMs")
                        }
                        o.int("step")?.let { cu = cu.copy(step = it) }
                    } catch (_: Exception) {}
                    cu = cu.copy(status = "running")
                    Log.d(TAG, "CU screenshot update")
                }
                "cu_action" -> {
                    try {
                        val o = obj(data)
                        val action = o.str("action") ?: ""
                        val step = o.int("step")
                        cu = cu.copy(actionLabel = action)
                        if (step != null) cu = cu.copy(step = step)
                        // Append action summary to content for chat history
                        append("\n`[$action]` ")
                    } catch (_: Exception) {}
                    cu = cu.copy(status = "running")
                }
                "cu_bash_output" -> try {
                    val o = obj(data)
                    append("\n```\n$ ${o.str("command") ?: ""}\n${o.str("output") ?: ""}\n```\n")
                } catch (_: Exception) {}
                "cu_file_edit" -> try {
                    val o = obj(data)
                    append("\n`[${o.str("command") ?: "edit"} ${o.str("path") ?: ""}]` ")
                } catch (_: Exception) {}
                "cu_step" -> {
                    try {
                        val o = obj(data)
                        cu = cu.copy(step = o.int("step") ?: 0)
                        cu = cu.copy(stepTotal = o.int("total") ?: 0)
                    } catch (_: Exception) {}
                    cu = cu.copy(status = "running")
                }
                "cu_session" -> try {
                    val o = obj(data)
                    o.str("session_id")?.let { cu = cu.copy(sessionId = it) }
                    o.str("device_id")?.let { cu = cu.copy(deviceId = it) }
                } catch (_: Exception) {}
                "cu_stopped", "cu_task_stopped" -> cu = cu.copy(status = "stopped")
                // ── CU prompt queue — matches Portal chat-send.js ──
                "cu_queued" -> {
                    // The box queued this prompt behind the running task; that task's
                    // stream carries the reply (cu_queue_next), so this turn ends empty
                    // and the finalize drops its placeholder instead of saving it.
                    turn = turn.copy(queued = true)
                    val position = runCatching { obj(data).str("position") }.getOrNull()
                    cu = cu.copy(actionLabel = if (position.isNullOrBlank()) "Prompt queued" else "Prompt queued (#$position)")
                }
                "cu_queue_next" -> {
                    // The running task picked up the next queued prompt: its reply
                    // streams into THIS bubble after a separator.
                    turn = turn.copy(queueAppend = true)
                    val prompt = runCatching { obj(data).str("prompt") }.getOrNull().orEmpty()
                    append(ChatViewModel.cuQueueSeparator(prompt))
                    newChatState = ChatState.STREAMING
                    cu = cu.copy(status = "running")
                }

                // ── Robotics ER events — matches Portal chat-send.js ──
                "er_frame" -> {
                    try {
                        val o = obj(data)
                        o.str("image")?.let { er = er.copy(cameraFrame = it) }
                        o.str("camera")?.let { er = er.copy(camera = it) }
                    } catch (_: Exception) {}
                    er = er.copy(status = "connected")
                    Log.d(TAG, "ER camera frame received")
                }
                "er_reasoning" -> {
                    try {
                        val text = obj(data).str("text") ?: ""
                        er = er.copy(reasoning = text)
                        Log.d(TAG, "ER reasoning: ${text.take(80)}...")
                    } catch (_: Exception) {}
                    er = er.copy(status = "connected")
                }
                "er_action" -> {
                    try {
                        val o = obj(data)
                        val tool = o.str("tool") ?: ""
                        val args = o["args"]?.toString() ?: ""
                        val status = o.str("status") ?: "executing"
                        append("\n`[$tool]` $args → $status ")
                    } catch (_: Exception) {}
                    er = er.copy(status = "connected")
                }
                "er_status" -> try {
                    er = er.copy(status = obj(data).str("state") ?: "idle")
                } catch (_: Exception) {}
                "er_error" -> try {
                    val error = obj(data).str("error") ?: "Unknown robot error"
                    append("\n\n**Robot Error:** $error")
                    er = er.copy(status = "offline")
                } catch (_: Exception) {}
                "er_mission_start" -> {
                    er = er.copy(missionActive = true, status = "mission_active")
                    Log.d(TAG, "ER mission started")
                }
                "er_stopped" -> {
                    er = er.copy(missionActive = false, status = "stopped")
                    Log.d(TAG, "ER mission stopped")
                }
                "er_timeout" -> {
                    er = er.copy(missionActive = false, status = "timeout")
                    Log.d(TAG, "ER mission timed out")
                }
                "er_queued" -> try {
                    Log.d(TAG, "ER prompt queued at position ${obj(data).int("position") ?: 1}")
                } catch (_: Exception) {}
                "er_prompt_injected" -> try {
                    Log.d(TAG, "ER prompt injected: ${(obj(data).str("text") ?: "").take(80)}")
                } catch (_: Exception) {}
                "er_nav_status" -> try {
                    Log.d(TAG, "ER nav: ${obj(data).str("goal_status") ?: ""}")
                    er = er.copy(status = "navigating")
                } catch (_: Exception) {}
                "er_session" -> try {
                    Log.d(TAG, "ER session: ${obj(data).str("session_id")}")
                } catch (_: Exception) {}

                // ── Keep-alive ──
                "heartbeat" -> {
                    // Ignore — just prevents connection timeout
                }

                // ── Error ──
                "error" -> {
                    // Accumulated SEPARATELY from real content: the bubble still renders
                    // content+errors (combineContentAndErrors), but an error-only stream
                    // must finalize as a FAILURE, never be minted, and must not defeat the
                    // usable-content check that drives the retry affordance.
                    turn = turn.copy(errors = if (turn.errors.isNotEmpty()) "${turn.errors}\n\n$data" else data)
                    newChatState = ChatState.ERROR
                }

                // ── "The Signal" — presentation-only telemetry HUD (Phase 2) ──
                // These three branches drive the morphing red line ONLY. HARD RULE: they
                // never append to content/reasoning, feed the /chat/save payload, or reach
                // the ledger.
                "system_activity" -> {
                    // data = { stage, label, detail, seq } (telemetry_events.py); only the
                    // label is rendered. pushSignal no-ops on computer-use turns.
                    ChatViewModel.parseSignalLabel(data)?.let { pushSignal(it) }
                }
                "tool_start" -> {
                    // data = the tool name (a JSON string, already unquoted). Web-parity
                    // tolerance: an OBJECT payload ({name}/{tool}, the planned structured
                    // tool events) names the tool instead of splashing JSON onto the HUD.
                    val raw = data.trim()
                    val toolName = if (raw.startsWith("{")) toolNameOf(raw) else raw
                    if (toolName.isNotBlank()) {
                        turn = turn.copy(signal = turn.signal.copy(lastTool = toolName))
                        pushSignal("tool · $toolName")
                    }
                }
                "tool_result" -> {
                    // Echo the last tool name as done; else fall back to a trimmed blurb.
                    val last = turn.signal.lastTool
                    if (last.isNotBlank()) {
                        pushSignal("tool · $last · ok")
                    } else {
                        val d = data.trim()
                        if (d.isNotBlank()) pushSignal("tool · " + (if (d.length > 40) d.substring(0, 40) else d))
                    }
                }

                // ── M7: a wellness tool answered (UI-only, like the Signal frames) ──
                // data = {ref, categories, as_of, provider, provider_label}: no values. It
                // lands on the turn (the assistant bubble being built, text or no text yet),
                // never in content/reasoning, the /chat/save payload or the ledger.
                "wellness_used" -> WellnessChatRules.parseUsedFrame(data)?.let {
                    turn = turn.copy(wellnessUsed = WellnessChatRules.merge(turn.wellnessUsed, it))
                }

                // ── Unknown events — log but don't crash ──
                else -> Log.d(TAG, "Unhandled SSE event: ${event.event}")
            }
            return SseStep(turn, newChatState, signal, cu, er)
        }

        /**
         * An object tool_start's tool: its `name`, else its `tool` ("" when neither),
         * read the way org.json's optString read it (a JSON null reads as "null").
         */
        private fun toolNameOf(raw: String): String = try {
            val o = obj(raw)
            fun field(key: String): String = (o[key] as? JsonPrimitive)?.content.orEmpty()
            field("name").ifBlank { field("tool") }
        } catch (_: Exception) {
            ""
        }
    }
}
