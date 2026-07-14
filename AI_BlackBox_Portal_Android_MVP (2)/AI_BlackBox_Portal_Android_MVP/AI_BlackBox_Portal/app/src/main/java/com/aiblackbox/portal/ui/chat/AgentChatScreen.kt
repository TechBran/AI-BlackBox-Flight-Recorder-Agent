package com.aiblackbox.portal.ui.chat

import android.app.Application
import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.agent.AgentConnectionState
import com.aiblackbox.portal.data.agent.AgentEvent
import com.aiblackbox.portal.data.agent.ClaudeAgentClient
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import com.aiblackbox.portal.ui.components.ChatBubble
import com.aiblackbox.portal.ui.components.ClaudeAccent
import com.aiblackbox.portal.ui.components.SnapshotPeekSheet
import com.aiblackbox.portal.ui.components.GeminiAccent
import com.aiblackbox.portal.ui.components.LiveTextSection
import com.aiblackbox.portal.ui.components.ProviderBanner
import com.aiblackbox.portal.ui.components.StatusLine
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.SolidGreen
// Glass tokens from worktree Color.kt (no Glass.kt in worktree)
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

// =============================================================================
// Reasoning Phrases — mirrors Portal REASONING_PHRASES array
// =============================================================================
private val REASONING_PHRASES = listOf(
    "Reasoning",
    "Thinking deeply",
    "Analyzing",
    "Processing",
    "Contemplating",
    "Evaluating options",
    "Connecting ideas",
    "Synthesizing",
    "Deliberating",
    "Exploring paths",
    "Weighing factors",
    "Forming insights",
    "Building context",
    "Mapping patterns",
    "Refining thoughts",
    "Considering angles",
    "Crafting logic",
    "Structuring response",
    "Neural processing",
    "Deep analysis"
)

internal fun cliLiveStatusLabel(
    isThinking: Boolean,
    activeTool: String?,
    status: String,
): String? = when {
    isThinking -> "Thinking deeply"
    !activeTool.isNullOrBlank() -> "Using · $activeTool"
    status.isNotBlank() -> status
    else -> null
}

internal fun cliLiveStreamPhase(
    isStreaming: Boolean,
    isThinking: Boolean,
    activeTool: String?,
): LiveStreamPhase = when {
    !isStreaming -> LiveStreamPhase.IDLE
    isThinking -> LiveStreamPhase.THINKING
    !activeTool.isNullOrBlank() -> LiveStreamPhase.TOOL
    else -> LiveStreamPhase.ANSWERING
}

// =============================================================================
// Permission Modes — mirrors Portal cyclePermissionMode()
// =============================================================================
private val PERMISSION_MODES = listOf("Auto-Accept", "Normal", "Plan Mode")
private val PERMISSION_MODE_KEYS = listOf("auto", "normal", "plan")

// =============================================================================
// Tool Icons — mirrors Portal getToolIcon()
// =============================================================================
private val TOOL_ICONS = mapOf(
    "Bash" to "\u26A1",       // Lightning
    "Read" to "\uD83D\uDCC4", // Page
    "Edit" to "\u270F\uFE0F", // Pencil
    "Write" to "\uD83D\uDCBE", // Floppy
    "Grep" to "\uD83D\uDD0D", // Magnifier
    "Glob" to "\uD83D\uDDC2\uFE0F", // Folder
    "WebFetch" to "\uD83C\uDF10", // Globe
    "WebSearch" to "\uD83D\uDD0E", // Search
    "TodoWrite" to "\u2705",  // Checkmark
    "NotebookEdit" to "\uD83D\uDCD3" // Notebook
)

// =============================================================================
// AgentViewModel — mirrors Portal AgentChatHandler state management
//
// Portal state:
//   sessionId, outputBuffer, thinkingBuffer, isStreaming, hasActiveSession,
//   messageCount, lastTool, permissionMode, thinkingTokens, thinkingBudget,
//   reconnectAttempts, statusLine (model, context, cost, duration)
// =============================================================================
class AgentViewModel(application: Application) : AndroidViewModel(application) {

    private val store = BlackBoxStore(application)
    private var api: BlackBoxApi? = null
    private var agentClient: ClaudeAgentClient? = null

    // -- Messages --
    private val _messages = MutableStateFlow<List<UiMessage>>(emptyList())
    val messages: StateFlow<List<UiMessage>> = _messages.asStateFlow()

    // -- Connection --
    private val _connectionState = MutableStateFlow(AgentConnectionState.DISCONNECTED)
    val connectionState: StateFlow<AgentConnectionState> = _connectionState.asStateFlow()

    // -- Model --
    private val _currentModel = MutableStateFlow("sonnet")
    val currentModel: StateFlow<String> = _currentModel.asStateFlow()

    // -- Status — mirrors Portal banner status text
    private val _status = MutableStateFlow("Ready")
    val status: StateFlow<String> = _status.asStateFlow()

    // -- Permission mode — mirrors Portal cyclePermissionMode()
    private val _permissionModeIndex = MutableStateFlow(0) // 0=auto, 1=normal, 2=plan
    val permissionModeIndex: StateFlow<Int> = _permissionModeIndex.asStateFlow()

    // -- Permission request --
    private val _permissionRequest = MutableStateFlow<AgentEvent.PermissionRequest?>(null)
    val permissionRequest: StateFlow<AgentEvent.PermissionRequest?> = _permissionRequest.asStateFlow()

    // -- Active tool indicator — mirrors Portal updateToolIndicator()
    private val _activeTool = MutableStateFlow<ToolIndicatorData?>(null)
    val activeTool: StateFlow<ToolIndicatorData?> = _activeTool.asStateFlow()

    // -- Thinking state — mirrors Portal thinkingBuffer / thinkingTokens
    private val _isThinking = MutableStateFlow(false)
    val isThinking: StateFlow<Boolean> = _isThinking.asStateFlow()

    // -- Streaming state --
    private val _isStreaming = MutableStateFlow(false)
    val isStreaming: StateFlow<Boolean> = _isStreaming.asStateFlow()

    // -- Session metrics — mirrors Portal statusLine
    private val _contextPercent = MutableStateFlow(0)
    val contextPercent: StateFlow<Int> = _contextPercent.asStateFlow()

    private val _costDollars = MutableStateFlow("0.00")
    val costDollars: StateFlow<String> = _costDollars.asStateFlow()

    private val _duration = MutableStateFlow("0s")
    val duration: StateFlow<String> = _duration.asStateFlow()

    // -- Message count — mirrors Portal messageCount
    private val _messageCount = MutableStateFlow(0)
    val messageCount: StateFlow<Int> = _messageCount.asStateFlow()

    private var currentOperator = "Brandon"
    private var currentProvider = "agents"
    private var outputBuffer = StringBuilder()
    private var thinkingBuffer = StringBuilder()

    init {
        viewModelScope.launch {
            store.operator.collect { currentOperator = it }
        }
        viewModelScope.launch {
            store.claudeModel.collect { _currentModel.value = it }
        }
    }

    fun initialize(origin: String, provider: String = "agents") {
        if (origin.isBlank() || api != null) return
        currentProvider = provider
        api = BlackBoxApi(origin)
        val wsUrl = origin.replace("https://", "wss://").replace("http://", "ws://")
        val wsPath = if (provider == "gemini-agents") "/ws/gemini-agent" else "/ws/agent"
        agentClient = ClaudeAgentClient(api!!.getClient(), wsUrl, wsPath)

        // Set correct default model per provider
        if (provider == "gemini-agents") {
            _currentModel.value = "gemini-3.1-pro-preview"
        }

        // Observe connection state
        viewModelScope.launch {
            agentClient!!.connectionState.collect { state ->
                _connectionState.value = state
                when (state) {
                    AgentConnectionState.CONNECTING -> _status.value = "Connecting..."
                    AgentConnectionState.CONNECTED -> {
                        if (_messageCount.value > 0) {
                            _status.value = "Session Active (${_messageCount.value} msgs)"
                        } else {
                            _status.value = "Connected"
                        }
                    }
                    AgentConnectionState.DISCONNECTED -> {
                        if (_messageCount.value > 0) {
                            _status.value = "Disconnected"
                        } else {
                            _status.value = "Ready"
                        }
                    }
                    AgentConnectionState.ERROR -> _status.value = "Error"
                }
            }
        }

        // Observe events — mirrors Portal handleMessage() switch
        viewModelScope.launch {
            agentClient!!.events.collect { event ->
                handleEvent(event)
            }
        }
    }

    /**
     * Handle agent events — mirrors Portal handleMessage() switch cases.
     */
    private fun handleEvent(event: AgentEvent) {
        when (event) {
            // Content streaming — Portal: appendContent()
            is AgentEvent.Content -> {
                _isThinking.value = false
                _isStreaming.value = true
                _activeTool.value = null
                _status.value = "Responding..."
                outputBuffer.append(event.text)
                updateLastAssistant(outputBuffer.toString())
            }

            // Thinking — Portal: appendThinking()
            is AgentEvent.Thinking -> {
                _isThinking.value = true
                _isStreaming.value = true
                _status.value = "Thinking..."
                thinkingBuffer.append(event.text)
                updateLastAssistantThinking(thinkingBuffer.toString())
            }

            // Tool use — Portal: appendToolUse(), updateToolIndicator()
            is AgentEvent.ToolUse -> {
                _isThinking.value = false
                _isStreaming.value = true
                _status.value = "Using tools..."
                val icon = TOOL_ICONS[event.name] ?: "\uD83D\uDD27" // Wrench default
                val detail = event.input["command"]?.take(50)
                    ?: event.input["file_path"]?.substringAfterLast('/')
                    ?: event.input["pattern"]?.take(35)
                    ?: ""
                _activeTool.value = ToolIndicatorData(
                    name = event.name,
                    icon = icon,
                    detail = detail
                )
            }

            // Tool result — Portal: appendToolResult()
            is AgentEvent.ToolResult -> {
                _isThinking.value = false
                _isStreaming.value = true
                _activeTool.value = null
                _status.value = "Processing..."
                if (event.text.isNotBlank()) {
                    val lines = event.text.count { it == '\n' } + 1
                    val summary = if (lines > 10) "[Tool output: $lines lines]" else event.text
                    outputBuffer.append("\n```\n$summary\n```\n")
                    updateLastAssistant(outputBuffer.toString())
                }
            }

            // File result — Portal: appendFileResult()
            is AgentEvent.FileResult -> {
                _isThinking.value = false
                _isStreaming.value = true
                _activeTool.value = null
                _status.value = "File operation..."
                val fileName = event.filePath.substringAfterLast('/')
                outputBuffer.append("\n[Read: $fileName, lines ${event.startLine}-${event.startLine + event.numLines - 1}]\n")
                updateLastAssistant(outputBuffer.toString())
            }

            // Edit diff — Portal: appendEditDiff()
            is AgentEvent.EditDiff -> {
                _isThinking.value = false
                _isStreaming.value = true
                _activeTool.value = null
                _status.value = "Edit complete"
                val fileName = event.filePath.substringAfterLast('/')
                outputBuffer.append("\n[Edited: $fileName]\n")
                if (event.diff.isNotBlank()) {
                    outputBuffer.append("```diff\n${event.diff}\n```\n")
                }
                updateLastAssistant(outputBuffer.toString())
            }

            // Raw output — Portal: appendRawOutput()
            is AgentEvent.Output -> {
                _isThinking.value = false
                _isStreaming.value = true
                _activeTool.value = null
                _status.value = "Streaming..."
                outputBuffer.append(event.text)
                updateLastAssistant(outputBuffer.toString())
            }

            // Completed — Portal: finalizeOutput(), hasActiveSession = true
            is AgentEvent.Completed -> {
                _isStreaming.value = false
                _isThinking.value = false
                _activeTool.value = null
                _messageCount.value = _messageCount.value + 1
                _status.value = "Session Active (${_messageCount.value} msgs)"
                updateLastAssistant(outputBuffer.toString(), streaming = false)
            }

            // Waiting — Portal: console.log('Claude waiting for input')
            is AgentEvent.Waiting -> {
                _status.value = "Waiting for input"
            }

            // Status update — Portal: statusLine.updateFromMessage()
            is AgentEvent.StatusUpdate -> {
                if (event.model.isNotBlank()) _currentModel.value = event.model
                _contextPercent.value = event.contextPercent
                _costDollars.value = event.costDollars
                _duration.value = event.duration
            }

            // Error — Portal: toast('Agent error:')
            is AgentEvent.Error -> {
                _status.value = "Error"
                _isStreaming.value = false
                outputBuffer.append("\n\u26A0\uFE0F ${event.message}")
                updateLastAssistant(outputBuffer.toString(), streaming = false)
            }

            // Info — Portal: toast(msg.data)
            is AgentEvent.Info -> {
                // Show as inline content if streaming, otherwise update status
                if (_isStreaming.value) {
                    _isThinking.value = false
                    _activeTool.value = null
                    outputBuffer.append("\n\u2139\uFE0F ${event.message}")
                    updateLastAssistant(outputBuffer.toString())
                } else {
                    _status.value = event.message
                }
            }

            // Permission request — Portal: showPermissionButtons()
            is AgentEvent.PermissionRequest -> {
                _status.value = "Permission Required"
                _permissionRequest.value = event
            }

            // Session ended — Portal: hasActiveSession = false
            is AgentEvent.SessionEnded -> {
                _isStreaming.value = false
                _isThinking.value = false
                _activeTool.value = null
                _messageCount.value = 0
                _status.value = "Ready"
                updateLastAssistant(outputBuffer.toString(), streaming = false)
            }

            is AgentEvent.Connected -> {
                _status.value = "Connected"
            }

            is AgentEvent.Disconnected -> {
                _isStreaming.value = false
                updateLastAssistant(outputBuffer.toString(), streaming = false)
            }

            // Provenance — Plan Task 10: parse-from-WS reaches the streaming bubble
            is AgentEvent.ProvenanceUpdate -> {
                updateLastAssistantProvenance(event.provenance)
            }
        }
    }

    fun startSession() {
        val client = agentClient ?: return
        _status.value = "Starting..."
        val sessionId = client.generateSessionId()
        viewModelScope.launch { store.setString("agent_session_$currentOperator", sessionId) }
        client.connect(sessionId, viewModelScope)
    }

    fun sendPrompt(text: String) {
        val client = agentClient ?: return

        // Add user message to UI immediately
        _messages.value = _messages.value + UiMessage(role = "user", content = text)

        // Reset buffers
        outputBuffer = StringBuilder()
        thinkingBuffer = StringBuilder()
        _activeTool.value = null
        _isThinking.value = false
        _isStreaming.value = true

        // Add streaming assistant placeholder
        _messages.value = _messages.value + UiMessage(
            role = "assistant",
            content = "",
            isStreaming = true,
            isThinking = true,
            model = _currentModel.value
        )

        val permissionKey = PERMISSION_MODE_KEYS[_permissionModeIndex.value]

        if (_connectionState.value != AgentConnectionState.CONNECTED) {
            // Not connected — connect AND send in one step (prompt fires in onOpen)
            // Mirrors Portal: startSession() sends prompt inside ws.onopen handler
            _status.value = "Connecting..."
            val sessionId = client.generateSessionId()
            viewModelScope.launch { store.setString("agent_session_$currentOperator", sessionId) }
            client.connectAndSend(
                sessionId = sessionId,
                scope = viewModelScope,
                text = text,
                operator = currentOperator,
                model = _currentModel.value,
                permissionMode = permissionKey
            )
        } else {
            // Already connected — send directly
            _status.value = "Continuing..."
            client.sendPrompt(text, currentOperator, _currentModel.value, permissionKey)
        }
    }

    fun respondPermission(approved: Boolean) {
        agentClient?.respondPermission(approved)
        _permissionRequest.value = null
        if (approved) {
            _status.value = "Continuing..."
        }
    }

    fun endSession() {
        agentClient?.endSession()
        agentClient?.disconnect()
        _isStreaming.value = false
        _isThinking.value = false
        _activeTool.value = null
        _status.value = "Ready"
    }

    fun setModel(model: String) {
        _currentModel.value = model
        viewModelScope.launch { store.setClaudeModel(model) }
    }

    /**
     * Start a new session — mirrors Portal confirmNewSession()
     */
    fun newSession() {
        endSession()
        _messages.value = emptyList()
        outputBuffer = StringBuilder()
        thinkingBuffer = StringBuilder()
        _messageCount.value = 0
        _contextPercent.value = 0
        _costDollars.value = "0.00"
        _duration.value = "0s"
        viewModelScope.launch {
            store.setString("agent_session_$currentOperator", "")
        }
        startSession()
    }

    /**
     * Cycle permission mode — mirrors Portal cyclePermissionMode()
     */
    fun cyclePermissionMode() {
        _permissionModeIndex.value = (_permissionModeIndex.value + 1) % PERMISSION_MODES.size
    }

    /**
     * Trigger snapshot — mirrors Portal createDevSnapshot()
     */
    fun triggerSnapshot() {
        val client = agentClient ?: return
        if (_connectionState.value == AgentConnectionState.CONNECTED) {
            client.sendPrompt("/snapshot-dev", currentOperator, _currentModel.value, "auto")
        }
    }

    private fun updateLastAssistant(content: String, streaming: Boolean = true) {
        val current = _messages.value.toMutableList()
        if (current.isNotEmpty() && current.last().role == "assistant") {
            current[current.lastIndex] = current.last().copy(
                content = content,
                isStreaming = streaming,
                isThinking = false
            )
            _messages.value = current
        }
    }

    /**
     * Plan Task 10: store typed Provenance on the streaming assistant bubble.
     * ChatBubble already renders message.provenance via ContextProvenance —
     * the bubble lights up as soon as the WS provenance event arrives.
     */
    private fun updateLastAssistantProvenance(provenance: Provenance) {
        val current = _messages.value.toMutableList()
        if (current.isNotEmpty() && current.last().role == "assistant") {
            current[current.lastIndex] = current.last().copy(provenance = provenance)
            _messages.value = current
        }
    }

    private fun updateLastAssistantThinking(thinking: String) {
        val current = _messages.value.toMutableList()
        if (current.isNotEmpty() && current.last().role == "assistant") {
            current[current.lastIndex] = current.last().copy(
                reasoning = thinking,
                isThinking = true,
                isStreaming = true
            )
            _messages.value = current
        }
    }
}

/** Data for the sticky tool indicator */
data class ToolIndicatorData(
    val name: String,
    val icon: String,
    val detail: String
)

// =============================================================================
// AgentChatScreen — aligned with Portal agent handler UI
//
// Portal layout:
//   .claude-code-banner (top, fixed)
//   .thinking-indicator (floating above todo/status when thinking)
//   #history (chat messages with .agent-streaming class)
//   .tool-indicator (sticky at bottom of current bubble)
//   .agent-status-line (floating, shows context/cost/duration)
//   Composer (bottom)
//
// This Composable renders:
//   1. ProviderBanner with all metrics/controls/permissionMode
//   2. Tool indicator bar (when active)
//   3. Chat messages with ChatBubble
//   4. Thinking animation overlay
//   5. Permission dialog
// =============================================================================

@Composable
fun AgentChatScreen(
    origin: String,
    operator: String,
    provider: String = "agents",
    onSend: (String) -> Unit = {},
    chatViewModel: ChatViewModel? = null,
    modifier: Modifier = Modifier,
    viewModel: AgentViewModel = viewModel()
) {
    val messages by viewModel.messages.collectAsState()
    val connectionState by viewModel.connectionState.collectAsState()
    val currentModel by viewModel.currentModel.collectAsState()
    val status by viewModel.status.collectAsState()
    val permissionReq by viewModel.permissionRequest.collectAsState()
    val permissionModeIdx by viewModel.permissionModeIndex.collectAsState()
    val activeTool by viewModel.activeTool.collectAsState()
    val isThinking by viewModel.isThinking.collectAsState()
    val isStreaming by viewModel.isStreaming.collectAsState()
    val contextPercent by viewModel.contextPercent.collectAsState()
    val costDollars by viewModel.costDollars.collectAsState()
    val duration by viewModel.duration.collectAsState()
    val messageCount by viewModel.messageCount.collectAsState()
    val listState = rememberLazyListState()
    val view = LocalView.current

    var peekSnapId by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(origin, provider) {
        viewModel.initialize(origin, provider)
        com.aiblackbox.portal.ui.components.setChatBaseUrl(origin)
    }

    // Listen for prompts forwarded from Composer via ChatViewModel
    LaunchedEffect(chatViewModel) {
        chatViewModel?.agentPromptEvent?.collect { prompt ->
            viewModel.sendPrompt(prompt)
        }
    }

    val isGeminiAgent = provider == "gemini-agents"
    val bannerName = if (isGeminiAgent) "Gemini CLI" else "Claude Code"
    val bannerAccent = if (isGeminiAgent) GeminiAccent else ClaudeAccent

    Column(modifier = modifier.fillMaxSize().padding(top = 100.dp)) {
        // ====================================================================
        // 1. Provider Banner — full Portal alignment
        // ====================================================================
        ProviderBanner(
            providerName = bannerName,
            isConnected = connectionState == AgentConnectionState.CONNECTED,
            model = currentModel,
            status = status,
            contextPercent = contextPercent,
            costDollars = costDollars,
            duration = duration,
            permissionMode = PERMISSION_MODES[permissionModeIdx],
            onModelChange = {
                viewModel.setModel(it)
            },
            onNewSession = {
                viewModel.newSession()
            },
            onSnapshot = {
                viewModel.triggerSnapshot()
            },
            onEndSession = {
                viewModel.endSession()
            },
            onPermissionModeChange = {
                viewModel.cyclePermissionMode()
            },
            accentColor = bannerAccent,
            extraContent = {
                // StatusLine only shown when connected with metrics
                if (connectionState == AgentConnectionState.CONNECTED) {
                    StatusLine(
                        contextPercent = contextPercent.toFloat(),
                        cost = if (costDollars != "0.00") "$$costDollars" else "",
                        model = currentModel,
                        duration = duration
                    )
                }
            }
        )

        // ====================================================================
        // 2. Chat messages and the single fixed live-status rail
        // ====================================================================
        AgentLiveMessageContent(
            messages = messages,
            provider = provider,
            status = status,
            activeTool = activeTool,
            isThinking = isThinking,
            isStreaming = isStreaming,
            listState = listState,
            modifier = Modifier.fillMaxWidth().weight(1f),
            onSnapshotClick = { peekSnapId = it },
        )
    }

    // ========================================================================
    // 5. Permission Dialog — mirrors Portal .permission-dialog
    // ========================================================================
    permissionReq?.let { req ->
        view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
        PermissionDialog(
            request = req,
            onApprove = {
                viewModel.respondPermission(true)
            },
            onDeny = {
                viewModel.respondPermission(false)
            }
        )
    }

    peekSnapId?.let { snapId ->
        SnapshotPeekSheet(
            snapId = snapId,
            origin = origin,
            onDismiss = { peekSnapId = null }
        )
    }
}

@Composable
internal fun AgentLiveMessageContent(
    messages: List<UiMessage>,
    provider: String,
    status: String,
    activeTool: ToolIndicatorData?,
    isThinking: Boolean,
    isStreaming: Boolean,
    modifier: Modifier = Modifier,
    listState: LazyListState = rememberLazyListState(),
    onSnapshotClick: (String) -> Unit = {},
) {
    // AgentViewModel creates and mutates real `assistant` placeholders. Selecting
    // by that model value also remains correct if a user message follows history.
    val liveMessage = messages.lastOrNull { it.role == "assistant" }
    val phase = cliLiveStreamPhase(isStreaming, isThinking, activeTool?.name)
    val label = cliLiveStatusLabel(isThinking, activeTool?.name, status)
    val snapshot = LiveStreamSnapshot(
        messageId = liveMessage?.id,
        reasoningLength = liveMessage?.reasoning?.length ?: 0,
        answerLength = liveMessage?.content?.length ?: 0,
        phase = phase,
        statusLabel = label,
    )
    val followState = rememberLiveStreamFollowState(listState, snapshot)
    val expectedSection = when (phase) {
        LiveStreamPhase.THINKING -> LiveTextSection.REASONING
        LiveStreamPhase.ANSWERING -> LiveTextSection.ANSWER
        else -> null
    }

    Box(modifier = modifier.testTag("agent-messages-$provider")) {
        LazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .liveStreamUserInput(followState)
                .testTag("messages"),
            contentPadding = PaddingValues(top = 8.dp, bottom = 180.dp),
        ) {
            items(items = messages, key = { it.id }) { message ->
                val isLiveTurn = message.id == snapshot.messageId
                ChatBubble(
                    message = message,
                    onSnapshotClick = onSnapshotClick,
                    onLiveEdgePositioned = if (isLiveTurn && expectedSection != null) {
                        { section, y -> if (section == expectedSection) followState.reportEdge(y) }
                    } else null,
                )
            }
        }
        if (snapshot.isActive) {
            LiveStreamFocalRail(label, followState)
        }
    }
}

// =============================================================================
// ToolIndicatorBar — mirrors Portal .tool-indicator
//
// Portal: sticky at bottom of bubble, shows icon + tool name + detail
// Android: compact bar below banner, glass surface
// =============================================================================

@Composable
private fun ToolIndicatorBar(tool: ToolIndicatorData) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Neutral100)
            .border(
                width = 1.dp,
                color = GlassBorder,
                shape = RoundedCornerShape(0.dp)
            )
            .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        // Spinning indicator
        val infiniteTransition = rememberInfiniteTransition(label = "toolSpin")
        val alpha by infiniteTransition.animateFloat(
            initialValue = 0.4f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(
                animation = tween(800),
                repeatMode = RepeatMode.Reverse
            ),
            label = "toolAlpha"
        )

        // Tool icon
        Text(
            text = tool.icon,
            fontSize = 14.sp,
            modifier = Modifier.alpha(alpha)
        )

        // Tool name
        Text(
            text = tool.name,
            style = MaterialTheme.typography.labelMedium.copy(
                fontWeight = FontWeight.SemiBold
            ),
            color = ClaudeAccent
        )

        // Detail (file name, command, pattern)
        if (tool.detail.isNotBlank()) {
            Text(
                text = tool.detail,
                style = MaterialTheme.typography.labelSmall.copy(
                    fontFamily = FontFamily.Monospace
                ),
                color = Neutral500,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f, fill = false)
            )
        }
    }
}

// =============================================================================
// ThinkingBar — mirrors Portal .thinking-indicator with cycling phrases
//
// Portal: fixed position, purple accent, cycling labels every 2s
// Android: inline bar above chat, purple theme
// =============================================================================

@Composable
private fun ThinkingBar() {
    var phraseIndex by remember { mutableIntStateOf(0) }

    // Cycle through phrases — mirrors Portal startReasoningCycle()
    LaunchedEffect(Unit) {
        while (true) {
            delay(2000)
            phraseIndex = (phraseIndex + 1) % REASONING_PHRASES.size
        }
    }

    val infiniteTransition = rememberInfiniteTransition(label = "thinkPulse")
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 0.6f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(1500),
            repeatMode = RepeatMode.Reverse
        ),
        label = "thinkAlpha"
    )

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Color(0x339370DB)) // rgba(147, 112, 219, 0.2)
            .border(
                width = 1.dp,
                color = Color(0x669370DB), // rgba(147, 112, 219, 0.4)
                shape = RoundedCornerShape(0.dp)
            )
            .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        // Brain icon with pulse — mirrors Portal .thinking-icon
        Text(
            text = "\uD83E\uDDE0",
            fontSize = 16.sp,
            modifier = Modifier.alpha(pulseAlpha)
        )

        // Cycling label — mirrors Portal .thinking-label
        Text(
            text = REASONING_PHRASES[phraseIndex],
            style = MaterialTheme.typography.labelMedium.copy(
                fontWeight = FontWeight.SemiBold
            ),
            color = Color(0xFFB19CD9) // Portal: #b19cd9
        )
    }
}

// =============================================================================
// PermissionDialog — mirrors Portal .permission-dialog
//
// Portal structure:
//   .permission-header (icon + title + tool badge)
//   .permission-prompt (monospace code block)
//   .permission-file (blue file path)
//   .permission-buttons (Allow + Deny, both touch-friendly 48px min)
//   .permission-hint (keyboard shortcut hint)
//
// Android: AlertDialog with Portal styling
// =============================================================================

@Composable
private fun PermissionDialog(
    request: AgentEvent.PermissionRequest,
    onApprove: () -> Unit,
    onDeny: () -> Unit
) {
    val feedback = rememberPressFeedback()
    AlertDialog(
        onDismissRequest = onDeny,
        containerColor = Neutral100,
        title = {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                // Permission icon
                Text("\uD83D\uDD10", fontSize = 20.sp) // Lock icon
                Text(
                    "Permission Request",
                    color = ClaudeAccent,
                    style = MaterialTheme.typography.titleMedium.copy(
                        fontWeight = FontWeight.SemiBold
                    )
                )
                // Tool badge — matches Portal .permission-tool
                if (request.tool.isNotBlank()) {
                    Spacer(Modifier.weight(1f))
                    Box(
                        modifier = Modifier
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(ClaudeAccent.copy(alpha = 0.2f))
                            .padding(horizontal = 8.dp, vertical = 2.dp)
                    ) {
                        Text(
                            text = request.tool,
                            style = MaterialTheme.typography.labelSmall.copy(
                                fontFamily = FontFamily.Monospace
                            ),
                            color = Color(0xFF7AB3FF) // Portal: #7ab3ff
                        )
                    }
                }
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                // Command/prompt — matches Portal .permission-prompt
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(Color.Black)
                        .padding(12.dp)
                ) {
                    Text(
                        text = request.prompt,
                        style = MaterialTheme.typography.bodySmall.copy(
                            fontFamily = FontFamily.Monospace,
                            lineHeight = 18.sp
                        ),
                        color = BbxWhite
                    )
                }

                // File path — matches Portal .permission-file
                if (request.filePath.isNotBlank()) {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(Neutral200)
                            .border(1.dp, Neutral300, RoundedCornerShape(RadiusMd))
                            .padding(horizontal = 12.dp, vertical = 8.dp)
                    ) {
                        Text(
                            text = request.filePath,
                            style = MaterialTheme.typography.labelSmall.copy(
                                fontFamily = FontFamily.Monospace
                            ),
                            color = ClaudeAccent
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { feedback(); onApprove() }) {
                Text(
                    "Allow",
                    color = SolidGreen,
                    style = MaterialTheme.typography.labelLarge.copy(
                        fontWeight = FontWeight.SemiBold
                    )
                )
            }
        },
        dismissButton = {
            TextButton(onClick = { feedback(); onDeny() }) {
                Text(
                    "Deny",
                    color = BbxAccent,
                    style = MaterialTheme.typography.labelLarge.copy(
                        fontWeight = FontWeight.SemiBold
                    )
                )
            }
        }
    )
}
