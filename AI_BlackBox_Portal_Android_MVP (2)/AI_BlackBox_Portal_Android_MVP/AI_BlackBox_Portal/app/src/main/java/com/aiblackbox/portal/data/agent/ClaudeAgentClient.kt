package com.aiblackbox.portal.data.agent

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.WebSocketClient
import com.aiblackbox.portal.data.api.WsMessage
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.ui.chat.ChatViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import kotlin.math.min
import kotlin.math.pow

// =============================================================================
// Connection State — mirrors Portal agent handler's connection lifecycle
// =============================================================================
enum class AgentConnectionState { DISCONNECTED, CONNECTING, CONNECTED, ERROR }

// =============================================================================
// AgentEvent — mirrors Portal handleMessage() switch cases
//
// Portal message types (from agent-handler.js handleMessage):
//   content, assistant_message, thinking, tool_use, tool_result,
//   file_result, edit_diff, raw, output, waiting, completed,
//   session_ended, permission_request, info, error,
//   todo_write, status_update, image_task, video_task, music_task
// =============================================================================
sealed class AgentEvent {
    /** Streaming text content (type: "content" or "assistant_message") */
    data class Content(val text: String) : AgentEvent()

    /** Extended thinking content (type: "thinking") */
    data class Thinking(val text: String) : AgentEvent()

    /** Tool execution started (type: "tool_use") */
    data class ToolUse(
        val name: String,
        val input: Map<String, String> = emptyMap()
    ) : AgentEvent()

    /** Tool execution result (type: "tool_result") */
    data class ToolResult(val text: String) : AgentEvent()

    /** File read result (type: "file_result") */
    data class FileResult(
        val filePath: String,
        val content: String,
        val numLines: Int,
        val startLine: Int
    ) : AgentEvent()

    /** Edit diff result (type: "edit_diff") */
    data class EditDiff(
        val filePath: String,
        val diff: String,
        val oldString: String,
        val newString: String
    ) : AgentEvent()

    /** Raw/output text (type: "raw" or "output") */
    data class Output(val text: String) : AgentEvent()

    /** Agent response complete (type: "completed") */
    data class Completed(val messageCount: Int) : AgentEvent()

    /** Status update with metrics (type: "status_update") */
    data class StatusUpdate(
        val model: String = "",
        val contextPercent: Int = 0,
        val costDollars: String = "0.00",
        val duration: String = "0s"
    ) : AgentEvent()

    /** Error message (type: "error") */
    data class Error(val message: String) : AgentEvent()

    /** Info/toast message (type: "info") */
    data class Info(val message: String) : AgentEvent()

    /** Permission request (type: "permission_request") */
    data class PermissionRequest(
        val prompt: String,
        val tool: String = "",
        val filePath: String = "",
        val allowed: List<String> = emptyList()
    ) : AgentEvent()

    /** Session ended (type: "session_ended") */
    data class SessionEnded(val message: String = "") : AgentEvent()

    /** Retrieval provenance (type: "provenance") — recent/keyword/semantic/checkpoint SNAP-IDs */
    data class ProvenanceUpdate(val provenance: Provenance) : AgentEvent()

    /** Waiting for input (type: "waiting") */
    data object Waiting : AgentEvent()

    /** WebSocket connected */
    data object Connected : AgentEvent()

    /** WebSocket disconnected */
    data object Disconnected : AgentEvent()
}

// =============================================================================
// Permission modes — mirrors Portal agent-permission-mode.js (audit P03/O06)
//
// Exactly two modes, each a Claude CLI flag that works in print mode (agent_routes.py):
//   yolo — every tool approved (--dangerously-skip-permissions); the default
//   plan — read-only (--permission-mode plan, with only read-only tools and no MCP servers)
// There is no "Normal" (approve each tool) mode: print mode has no approval round-trip, so
// it hung or auto-denied behind a fake dialog. It returns only with a real approval protocol.
// =============================================================================
object AgentPermissionMode {
    const val YOLO = "yolo"
    const val PLAN = "plan"
    val ALL: List<String> = listOf(YOLO, PLAN)

    /** Blank -> the default (YOLO); the old "auto" key -> YOLO; anything else unrecognised
     *  (a stale "normal", garbage) -> PLAN, so a bad value never escalates. */
    fun normalize(mode: String?): String {
        val m = mode?.trim()?.lowercase().orEmpty()
        return when {
            m.isEmpty() -> YOLO
            m == YOLO || m == "auto" -> YOLO
            else -> PLAN
        }
    }

    fun label(mode: String): String = if (normalize(mode) == YOLO) "YOLO" else "Plan Mode"
}

/** The `prompt` frame. `skip_permissions` is true ONLY for YOLO — it keeps an older box
 *  (which reads only that flag) in the mode the user chose. */
internal fun buildPromptMessage(text: String, operator: String, model: String, permissionMode: String): String {
    val mode = AgentPermissionMode.normalize(permissionMode)
    return buildJsonObject {
        put("type", "prompt")
        put("text", text)
        put("operator", operator)
        put("model", model)
        put("permission_mode", mode)
        put("skip_permissions", mode == AgentPermissionMode.YOLO)
    }.toString()
}

/** The follow-up `input` frame; a non-null [permissionMode] switches the session's mode. */
internal fun buildInputMessage(text: String, permissionMode: String?): String = buildJsonObject {
    put("type", "input")
    put("text", text)
    if (permissionMode != null) {
        val mode = AgentPermissionMode.normalize(permissionMode)
        put("permission_mode", mode)
        put("skip_permissions", mode == AgentPermissionMode.YOLO)
    }
}.toString()

// =============================================================================
// ClaudeAgentClient — mirrors Portal AgentChatHandler
//
// Features aligned with Portal:
//   - Session persistence per operator
//   - Reconnect with exponential backoff (Portal: attemptReconnect())
//   - Permission mode (yolo/plan — see AgentPermissionMode)
//   - Full message type parsing
//   - permission_mode (+ skip_permissions for YOLO) in prompt message
// =============================================================================
class ClaudeAgentClient(
    private val client: OkHttpClient,
    private val baseWsUrl: String,
    private val wsPath: String = "/ws/agent"
) {
    private val wsClient = WebSocketClient(client)
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _connectionState = MutableStateFlow(AgentConnectionState.DISCONNECTED)
    val connectionState: StateFlow<AgentConnectionState> = _connectionState.asStateFlow()

    private val _events = MutableSharedFlow<AgentEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<AgentEvent> = _events.asSharedFlow()

    private val _sessionId = MutableStateFlow<String?>(null)
    val sessionId: StateFlow<String?> = _sessionId.asStateFlow()

    private var connectionJob: Job? = null

    // Reconnect state — mirrors Portal reconnectAttempts / maxReconnectAttempts
    private var reconnectAttempts = 0
    private val maxReconnectAttempts = 15
    private var reconnectScope: CoroutineScope? = null

    // Pending prompt to send immediately on WebSocket open (mirrors Portal onopen handler)
    private var pendingPrompt: String? = null

    /**
     * Connect to a Claude Code agent session.
     * Mirrors Portal: ws = new WebSocket(wsUrl) in startSession()
     */
    fun connect(sessionId: String, scope: CoroutineScope) {
        _sessionId.value = sessionId
        _connectionState.value = AgentConnectionState.CONNECTING
        reconnectScope = scope
        reconnectAttempts = 0

        connectionJob?.cancel()
        connectionJob = scope.launch {
            val url = "$baseWsUrl$wsPath/$sessionId"
            android.util.Log.d("AgentClient", "Connecting to: $url")
            wsClient.connect(url).collect { msg ->
                when (msg) {
                    is WsMessage.Connected -> {
                        _connectionState.value = AgentConnectionState.CONNECTED
                        reconnectAttempts = 0
                        _events.emit(AgentEvent.Connected)
                        // Send pending prompt immediately on connect (like Portal onopen)
                        pendingPrompt?.let { prompt ->
                            pendingPrompt = null
                            wsClient.send(prompt)
                            android.util.Log.d("AgentClient", "Sent pending prompt on connect")
                        }
                    }
                    is WsMessage.Text -> parseMessage(msg.text)
                    is WsMessage.Closing -> {
                        _connectionState.value = AgentConnectionState.DISCONNECTED
                        _events.emit(AgentEvent.Error("Session closing: ${msg.reason}"))
                    }
                    is WsMessage.Error -> {
                        _connectionState.value = AgentConnectionState.ERROR
                        _events.emit(AgentEvent.Error(msg.error.message ?: "Connection error"))
                    }
                    is WsMessage.Disconnected -> {
                        _connectionState.value = AgentConnectionState.DISCONNECTED
                        _events.emit(AgentEvent.Disconnected)
                        if (_sessionId.value != null) {
                            attemptReconnect()
                        }
                    }
                }
            }
        }
    }

    /**
     * Connect AND send prompt in one step — mirrors Portal's startSession() pattern:
     * 1. Open WebSocket
     * 2. In onopen handler, immediately send prompt message
     * This eliminates the race condition of connect-then-delay-then-send.
     */
    fun connectAndSend(
        sessionId: String,
        scope: CoroutineScope,
        text: String,
        operator: String,
        model: String = "sonnet",
        permissionMode: String = AgentPermissionMode.YOLO
    ) {
        val promptMsg = buildPromptMessage(text, operator, model, permissionMode)

        // Queue the prompt — it fires in the Connected handler above
        pendingPrompt = promptMsg
        connect(sessionId, scope)
    }

    /**
     * Reconnect with exponential backoff.
     * Mirrors Portal: attemptReconnect() with Math.min(1000 * 2^n, 30000)
     */
    private fun attemptReconnect() {
        val scope = reconnectScope ?: return
        val sid = _sessionId.value ?: return

        if (reconnectAttempts >= maxReconnectAttempts) {
            android.util.Log.w("AgentClient", "Max reconnect attempts reached")
            _connectionState.value = AgentConnectionState.ERROR
            return
        }

        reconnectAttempts++
        val delayMs = min(1000L * 2.0.pow(reconnectAttempts - 1).toLong(), 30000L)
        android.util.Log.d("AgentClient", "Reconnecting ($reconnectAttempts/$maxReconnectAttempts) in ${delayMs}ms")
        _connectionState.value = AgentConnectionState.CONNECTING

        scope.launch {
            delay(delayMs)
            if (_sessionId.value != null) {
                connect(sid, scope)
            }
        }
    }

    /**
     * Send a prompt to start a new agent task.
     * Mirrors Portal: ws.send with type='prompt', includes permission_mode
     */
    fun sendPrompt(
        text: String,
        operator: String,
        model: String = "sonnet",
        permissionMode: String = AgentPermissionMode.YOLO
    ) {
        wsClient.send(buildPromptMessage(text, operator, model, permissionMode))
    }

    /**
     * Send follow-up input to the agent. A non-null [permissionMode] switches the session's
     * mode for this and later turns.
     */
    fun sendInput(text: String, permissionMode: String? = null) {
        wsClient.send(buildInputMessage(text, permissionMode))
    }

    /**
     * Respond to a permission request.
     * Mirrors Portal: type='permission_response' with approved boolean
     */
    fun respondPermission(approved: Boolean) {
        val msg = buildJsonObject {
            put("type", "permission_response")
            put("approved", approved)
        }
        wsClient.send(msg.toString())
    }

    /**
     * End the current session.
     */
    fun endSession() {
        val msg = buildJsonObject {
            put("type", "end_session")
        }
        wsClient.send(msg.toString())
    }

    /**
     * Disconnect the WebSocket.
     */
    fun disconnect() {
        connectionJob?.cancel()
        wsClient.close()
        _connectionState.value = AgentConnectionState.DISCONNECTED
        _sessionId.value = null
        reconnectAttempts = maxReconnectAttempts // Prevent reconnect
    }

    /**
     * Generate a new session ID — mirrors Portal: crypto.randomUUID()
     * Session IDs are client-generated UUIDs, NOT fetched from the server.
     */
    fun generateSessionId(): String = java.util.UUID.randomUUID().toString()

    // =========================================================================
    // Message Parsing — mirrors Portal handleMessage() switch
    // =========================================================================

    private suspend fun parseMessage(raw: String) {
        try {
            val obj = json.parseToJsonElement(raw).jsonObject
            val type = obj["type"]?.jsonPrimitive?.content ?: return
            val data = obj["data"]

            when (type) {
                // Content streaming — Portal: appendContent(msg.data)
                "content", "assistant_message" -> {
                    val text = extractText(data)
                    if (text.isNotEmpty()) _events.emit(AgentEvent.Content(text))
                }

                // Extended thinking — Portal: appendThinking(msg.data)
                "thinking" -> {
                    val text = extractText(data)
                    if (text.isNotEmpty()) _events.emit(AgentEvent.Thinking(text))
                }

                // Tool use — Portal: appendToolUse(msg.data)
                "tool_use" -> {
                    val dataObj = data?.jsonObject
                    val name = dataObj?.get("name")?.jsonPrimitive?.content ?: "Unknown"
                    val input = mutableMapOf<String, String>()
                    dataObj?.get("input")?.jsonObject?.forEach { (k, v) ->
                        try { input[k] = v.jsonPrimitive.content } catch (_: Exception) {}
                    }
                    _events.emit(AgentEvent.ToolUse(name, input))
                }

                // Tool result — Portal: appendToolResult(msg.data)
                "tool_result" -> {
                    val text = extractText(data)
                    _events.emit(AgentEvent.ToolResult(text))
                }

                // File result — Portal: appendFileResult(msg.data)
                "file_result" -> {
                    val dataObj = data?.jsonObject
                    _events.emit(AgentEvent.FileResult(
                        filePath = dataObj?.get("filePath")?.jsonPrimitive?.content ?: "",
                        content = dataObj?.get("content")?.jsonPrimitive?.content ?: "",
                        numLines = dataObj?.get("numLines")?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                        startLine = dataObj?.get("startLine")?.jsonPrimitive?.content?.toIntOrNull() ?: 1
                    ))
                }

                // Edit diff — Portal: appendEditDiff(msg.data)
                "edit_diff" -> {
                    val dataObj = data?.jsonObject
                    _events.emit(AgentEvent.EditDiff(
                        filePath = dataObj?.get("filePath")?.jsonPrimitive?.content ?: "",
                        diff = dataObj?.get("diff")?.jsonPrimitive?.content ?: "",
                        oldString = dataObj?.get("oldString")?.jsonPrimitive?.content ?: "",
                        newString = dataObj?.get("newString")?.jsonPrimitive?.content ?: ""
                    ))
                }

                // Raw/output — Portal: appendRawOutput(msg.data)
                "raw", "output" -> {
                    val text = extractText(data)
                    if (text.isNotEmpty()) _events.emit(AgentEvent.Output(text))
                }

                // Completed — Portal: finalizeOutput(), hasActiveSession = true
                "completed" -> {
                    _events.emit(AgentEvent.Completed(0))
                }

                // Waiting for input — Portal: console.log('Claude waiting for input')
                "waiting" -> {
                    _events.emit(AgentEvent.Waiting)
                }

                // Status update (context/cost/model) — Portal: statusLine.updateFromMessage()
                "status_update" -> {
                    val dataObj = data?.jsonObject
                    _events.emit(AgentEvent.StatusUpdate(
                        model = dataObj?.get("model")?.jsonPrimitive?.content ?: "",
                        contextPercent = dataObj?.get("context_percent")?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                        costDollars = dataObj?.get("cost")?.jsonPrimitive?.content ?: "0.00",
                        duration = dataObj?.get("duration")?.jsonPrimitive?.content ?: "0s"
                    ))
                }

                // Session ended — Portal: hasActiveSession = false, sessionId = null
                "session_ended" -> {
                    val message = extractText(data)
                    _events.emit(AgentEvent.SessionEnded(message))
                    _connectionState.value = AgentConnectionState.DISCONNECTED
                }

                // Permission request — Portal: showPermissionButtons(msg.data)
                "permission_request" -> {
                    val dataObj = data?.jsonObject
                    val prompt = dataObj?.get("prompt")?.jsonPrimitive?.content ?: ""
                    val tool = dataObj?.get("tool")?.jsonPrimitive?.content ?: ""
                    val filePath = dataObj?.get("file_path")?.jsonPrimitive?.content ?: ""
                    val allowed = dataObj?.get("allowed")?.let { arr ->
                        try {
                            json.decodeFromString<List<String>>(arr.toString())
                        } catch (_: Exception) { emptyList() }
                    } ?: emptyList()
                    _events.emit(AgentEvent.PermissionRequest(prompt, tool, filePath, allowed))
                }

                // Info message — Portal: toast(msg.data)
                "info" -> {
                    val text = extractText(data)
                    _events.emit(AgentEvent.Info(text))
                }

                // Error — Portal: toast('Agent error: ' + msg.data)
                "error" -> {
                    val text = extractText(data)
                    _events.emit(AgentEvent.Error(text.ifEmpty { "Unknown error" }))
                }

                // Provenance — Plan Task 10: backend emits {"type":"provenance","data":{recent,keyword,semantic,checkpoint}}
                // The "data" field is a nested JSON object — re-stringify and reuse ChatViewModel.parseProvenance.
                "provenance" -> {
                    val raw = try { data?.jsonObject?.toString() } catch (_: Exception) { null }
                    if (raw != null) {
                        val parsed = ChatViewModel.parseProvenance(raw)
                        if (parsed != null) {
                            _events.emit(AgentEvent.ProvenanceUpdate(parsed))
                            android.util.Log.d(
                                "AgentClient",
                                "provenance: recent=${parsed.recent.size} keyword=${parsed.keyword.size} " +
                                    "semantic=${parsed.semantic.size} checkpoint=${parsed.checkpoint.size}"
                            )
                        } else {
                            android.util.Log.w("AgentClient", "provenance unparseable: ${raw.take(200)}")
                        }
                    }
                }

                // Pong — heartbeat, ignore
                "pong" -> { /* noop */ }

                else -> {
                    android.util.Log.w("AgentClient", "Unhandled message type: $type")
                    // Portal: if string data < 10000 chars, appendContent
                    val text = extractText(data)
                    if (text.isNotEmpty() && text.length < 10000) {
                        _events.emit(AgentEvent.Content(text))
                    }
                }
            }
        } catch (e: Exception) {
            android.util.Log.e("AgentClient", "Parse error: ${e.message}")
            _events.emit(AgentEvent.Error("Parse error: ${e.message}"))
        }
    }

    /** Extract text from a JsonElement that could be a primitive string or object */
    private fun extractText(data: JsonElement?): String {
        if (data == null) return ""
        return try {
            data.jsonPrimitive.content
        } catch (_: Exception) {
            data.toString()
        }
    }
}
