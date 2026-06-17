package com.aiblackbox.portal.ui.chat

import android.app.Application
import android.util.Log
import androidx.annotation.VisibleForTesting
import androidx.compose.ui.text.input.TextFieldValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.LocalModelApi
import com.aiblackbox.portal.data.api.SSEEvent
import com.aiblackbox.portal.data.local.FcLoop
import com.aiblackbox.portal.data.local.LiteRtEngine
import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.LocalLlm
import com.aiblackbox.portal.data.local.LocalModelManager
import com.aiblackbox.portal.data.local.LocalSnapshotQueue
import com.aiblackbox.portal.data.local.PersonaCache
import com.aiblackbox.portal.data.local.SamplerSettings
import com.aiblackbox.portal.data.local.ToolBridge
import com.aiblackbox.portal.data.local.ToolBridgeClient
import com.aiblackbox.portal.data.local.ToolCallingLlm
import com.aiblackbox.portal.overlay.AndroidPhoneController
import com.aiblackbox.portal.overlay.OverlayConfirmUi
import com.aiblackbox.portal.overlay.OverlayCredentialHandoff
import com.aiblackbox.portal.data.model.ChatMessage
import com.aiblackbox.portal.data.model.ChatProvider
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.model.TokenCount
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.data.repository.ChatRepository
import com.aiblackbox.portal.data.repository.TaskRepository
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.ChatHistoryStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.delay
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

// =============================================================================
// ChatViewModel — aligned with Portal chat-send.js
//
// Portal event handling (30+ SSE event types):
//   Stream lifecycle: stream_start, content_start, content, stream_end, done, error
//   Thinking: thinking_start, thinking, thinking_end
//   Media tasks: image_task, video_task, music_task
//   Computer Use: cu_screenshot, cu_action, cu_bash_output, cu_file_edit, cu_step
//   Metadata: usage, provenance, heartbeat
//
// Provider routing:
//   agents/gemini-agents → WebSocket (AgentChatHandler)
//   realtime/gemini-live/grok-live → Voice WebSocket
//   everything else → SSE streaming
// =============================================================================

private const val TAG = "ChatVM"

enum class ChatState { IDLE, STREAMING, THINKING, ERROR }

/**
 * Which delivery path [ChatViewModel.sendMessage] routes a turn to, selected by
 * [ChatViewModel.routeFor] from the provider's traits. Extracted so the routing
 * decision is unit-testable without instantiating the AndroidViewModel.
 *
 * - [AGENT]: forward to the agent screen (Claude Code / Gemini CLI WebSocket).
 * - [VOICE]: handled by the Voice screen, not chat.
 * - [ER_INJECT]: inject into a running robotics ER mission.
 * - [LOCAL_PLACEHOLDER]: on-device (Gemma) — safe placeholder until the Phase 2
 *   on-device engine lands; deliberately NOT the SSE path.
 * - [SSE]: cloud streaming via /chat SSE (the default).
 */
enum class ChatRoute { AGENT, VOICE, ER_INJECT, LOCAL_PLACEHOLDER, SSE }

private const val MAX_CHAT_MESSAGES = 100

/**
 * How many prior conversation turns to feed into the ON-DEVICE model's prompt.
 *
 * BlackBox architecture: the immutable snapshot ledger is the memory — NOT a
 * growing in-prompt transcript. Inter-turn recall will come from snapshot
 * semantic-search (the next stage); the on-device model only needs INTRA-turn
 * context, so each turn starts FRESH. `0` = no carried history.
 *
 * This keeps every on-device prompt bounded regardless of session length, and was
 * the fix for the 4096-token overrun caused by accumulating the whole transcript
 * each turn (device-confirmed: `Input token ids are too long: 4292 >= 4096`). A
 * small sliding window can be re-enabled here later (e.g. for immediate "it"
 * follow-ups) without touching [FcLoop]. Only the on-device path uses this; the
 * cloud SSE path is unaffected (the server owns its own context budget).
 */
private const val LOCAL_HISTORY_WINDOW_TURNS = 0

class ChatViewModel(application: Application) : AndroidViewModel(application) {

    private val appContext = application.applicationContext
    private val store = BlackBoxStore(application)
    private val historyStore = ChatHistoryStore(application)

    // Locally-persisted device autonomy posture (Task 4.6). The Task 1.5 toggle
    // writes it; the on-device phone-control gate reads it here. load() returns
    // PERMISSION (the SAFE, gating default) when unset/unreadable, so the agent
    // never silently runs in YOLO.
    private val autonomyStore by lazy {
        com.aiblackbox.portal.data.local.AutonomyStore.fromContext(appContext)
    }

    private var api: BlackBoxApi? = null
    private var repository: ChatRepository? = null
    private var taskRepository: TaskRepository? = null
    private var historyLoaded = false

    // ── On-device (local) provider gating (Task 1.6) ──
    // Built once the api is ready (initialize); gates LOCAL in the picker on a
    // disk-present, sha-verified model and fires a best-effort re-attest on open.
    private var providerPicker: ProviderPickerViewModel? = null

    // ── On-device (local) engine seam (Task 2.4) ──
    // The on-device generation engine, injected as a lazy provider so tests can
    // supply a FakeLocalLlm. NULL until an installed on-device model is found —
    // then it is lazily wired (Task 2.6a) to the SINGLETON [localEngine]
    // (initialize() is ~10s, so the engine is built once and reused across turns).
    // With no model installed it stays null and sendViaLocalEngine() falls back to
    // the 1.6 placeholder. Settable so a host/builder (or a test) can wire a fake.
    @VisibleForTesting
    var localLlmProvider: (() -> LocalLlm)? = null

    // The concrete on-device engine singleton (Task 2.6a). Built once the first
    // time a local turn runs with an installed model present (see
    // [localProviderOrWire]); reused across turns (its load() is idempotent — the
    // ~10s initialize() happens once). Closed in [onCleared]. Held here (not just
    // captured by the provider lambda) so onCleared can release the native engine.
    private var localEngine: LiteRtEngine? = null

    // The installed bundle file the [localEngine] runs, and its delegate. Resolved
    // from [LocalModelManager.installedModels] when the engine is wired.
    private var localEngineModelFile: java.io.File? = null
    private var localEngineDelegate: String = "cpu"

    // The persona / system-prompt cache (Task 2.3) feeding the on-device turn.
    // Built lazily off the application context the first time a local turn runs
    // (needs the api, which is set in initialize()); cached thereafter.
    private var personaCache: PersonaCache? = null

    // The two-hop on-device tool bridge (Task 3.1/3.3) — the dependency
    // FcLoop.runAgent needs to discover + execute BlackBox tools. Built lazily off
    // this VM's api (mirroring personaCacheOrBuild); cached. Settable so a test can
    // wire a fake. NULL until the api is set, in which case the local path stays on
    // the text-only runTurn (no agent loop without a bridge).
    @VisibleForTesting
    var toolBridge: ToolBridge? = null

    // The offline-resilient memory write-back queue (Task 2.5). persistLocalSave
    // routes completed on-device turns through this instead of a bare
    // saveConversation, so a turn finished without the mesh is queued on disk and
    // flushed (in FIFO order) when connectivity returns. Built lazily off the
    // application context + repository (both ready after initialize()); cached.
    private var snapshotQueue: LocalSnapshotQueue? = null

    // ── UI State ──
    private val _messages = MutableStateFlow<List<UiMessage>>(emptyList())
    val messages: StateFlow<List<UiMessage>> = _messages.asStateFlow()

    private val _chatState = MutableStateFlow(ChatState.IDLE)
    val chatState: StateFlow<ChatState> = _chatState.asStateFlow()

    private val _inputText = MutableStateFlow(TextFieldValue())
    val inputText: StateFlow<TextFieldValue> = _inputText.asStateFlow()

    private val _snapshotCount = MutableStateFlow(0)
    val snapshotCount: StateFlow<Int> = _snapshotCount.asStateFlow()

    private val _isHealthy = MutableStateFlow(true)
    val isHealthy: StateFlow<Boolean> = _isHealthy.asStateFlow()

    private val _checkpointTurns = MutableStateFlow(0)
    val checkpointTurns: StateFlow<Int> = _checkpointTurns.asStateFlow()

    private val _operators = MutableStateFlow(listOf("Brandon"))
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    // Media task IDs for polling (image/video/music generation)
    private val _pendingMediaTasks = MutableStateFlow<List<String>>(emptyList())
    val pendingMediaTasks: StateFlow<List<String>> = _pendingMediaTasks.asStateFlow()

    // Auto-TTS: emits text to speak when response completes and auto-TTS is on
    private val _autoTtsEvent = MutableSharedFlow<String>(extraBufferCapacity = 1)
    val autoTtsEvent: SharedFlow<String> = _autoTtsEvent.asSharedFlow()
    var autoTtsEnabled: Boolean = false

    // Active tasks being polled (for TaskPanel display)
    private val _activeTasks = MutableStateFlow<List<TaskStatus>>(emptyList())
    val activeTasks: StateFlow<List<TaskStatus>> = _activeTasks.asStateFlow()

    // Task completion events (for notifications)
    private val _taskCompletedEvent = MutableSharedFlow<TaskStatus>(extraBufferCapacity = 5)
    val taskCompletedEvent: SharedFlow<TaskStatus> = _taskCompletedEvent.asSharedFlow()

    // ── Agent prompt forwarding ──
    // When provider is an agent type, prompts are forwarded here instead of SSE
    private val _agentPromptEvent = MutableSharedFlow<String>(extraBufferCapacity = 1)
    val agentPromptEvent: SharedFlow<String> = _agentPromptEvent.asSharedFlow()

    // ── Dynamic model list (fetched from /models/{provider}) ──
    private val _liveModels = MutableStateFlow<List<Pair<String, String>>>(emptyList())
    val liveModels: StateFlow<List<Pair<String, String>>> = _liveModels.asStateFlow()

    // ── On-device (local) provider availability (Task 1.6) ──
    // True when the current operator has a disk-present, sha-verified on-device
    // model → the picker offers the LOCAL provider. Default false until loaded.
    private val _localAvailable = MutableStateFlow(false)
    val localAvailable: StateFlow<Boolean> = _localAvailable.asStateFlow()

    // ── CU model backends (id → "anthropic" | "google" | "openai") ──
    // Populated only when fetching /models/computer-use (CU production pass
    // 2026-06: the CU catalog entries carry a `backend` field). Includes the
    // "" (Auto) key mapped to the server default's backend. Empty for every
    // other provider — CuScreen falls back to its id-substring heuristic.
    private val _cuModelBackends = MutableStateFlow<Map<String, String>>(emptyMap())
    val cuModelBackends: StateFlow<Map<String, String>> = _cuModelBackends.asStateFlow()

    // ── Computer Use state ──
    // SSE-driven screenshot URL (from CU agent loop)
    private val _cuScreenshotUrl = MutableStateFlow<String?>(null)
    val cuScreenshotUrl: StateFlow<String?> = _cuScreenshotUrl.asStateFlow()

    // CU session ID (persists across turns)
    private val _cuSessionId = MutableStateFlow<String?>(null)
    val cuSessionId: StateFlow<String?> = _cuSessionId.asStateFlow()

    // CU device ID (selected target device)
    private val _cuDeviceId = MutableStateFlow("blackbox")
    val cuDeviceId: StateFlow<String> = _cuDeviceId.asStateFlow()

    // Step progress
    private val _cuStep = MutableStateFlow(0)
    val cuStep: StateFlow<Int> = _cuStep.asStateFlow()

    private val _cuStepTotal = MutableStateFlow(0)
    val cuStepTotal: StateFlow<Int> = _cuStepTotal.asStateFlow()

    // CU agent status: idle, running, stopped, complete
    private val _cuStatus = MutableStateFlow("idle")
    val cuStatus: StateFlow<String> = _cuStatus.asStateFlow()

    // Latest CU action description (for activity display)
    private val _cuActionLabel = MutableStateFlow("")
    val cuActionLabel: StateFlow<String> = _cuActionLabel.asStateFlow()

    // ── Robotics ER state ──
    // Whether an ER mission is actively running (matches Portal window.__erMissionActive)
    private val _erMissionActive = MutableStateFlow(false)
    val erMissionActive: StateFlow<Boolean> = _erMissionActive.asStateFlow()

    // Base64 camera frame from er_frame event
    private val _erCameraFrame = MutableStateFlow<String?>(null)
    val erCameraFrame: StateFlow<String?> = _erCameraFrame.asStateFlow()

    // Which camera is active — OAK-D is primary (stationary, has depth + YOLO distances)
    private val _erCamera = MutableStateFlow("yolo_oakd")
    val erCamera: StateFlow<String> = _erCamera.asStateFlow()

    // Robot status: offline, connecting, capturing, reasoning, connected
    private val _erStatus = MutableStateFlow("offline")
    val erStatus: StateFlow<String> = _erStatus.asStateFlow()

    // ER reasoning text (accumulated from er_reasoning events)
    private val _erReasoning = MutableStateFlow("")
    val erReasoning: StateFlow<String> = _erReasoning.asStateFlow()

    private var streamJob: Job? = null

    // ── Cached values ──
    private var currentOperator = "Brandon"
    private var currentProvider = "gemini"
    private var currentModel = ""

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    init {
        viewModelScope.launch {
            launch {
                store.operator.collect { op ->
                    val oldOp = currentOperator
                    currentOperator = op
                    // Load history when operator changes (or on first launch)
                    if (!historyLoaded || op != oldOp) {
                        loadHistory(op)
                    }
                }
            }
            launch {
                store.provider.collect {
                    currentProvider = it
                    fetchLiveModels(it)
                }
            }
            launch { store.model.collect { currentModel = it } }
        }
    }

    private fun loadHistory(operator: String) {
        viewModelScope.launch {
            val saved = historyStore.load(operator)
            if (saved.isNotEmpty()) {
                // Cap at MAX to keep UI responsive on load
                // Clear ALL stale transient flags from persisted state:
                // - ttsGenerating: no TTS generation active after restart
                // - isStreaming: no stream active after restart (stuck true = buttons won't render)
                // - isThinking: no thinking active after restart
                val cleaned = saved.takeLast(MAX_CHAT_MESSAGES).map { msg ->
                    if (msg.ttsGenerating || msg.isStreaming || msg.isThinking) {
                        msg.copy(ttsGenerating = false, isStreaming = false, isThinking = false)
                    } else msg
                }
                _messages.value = cleaned
                Log.d(TAG, "Restored ${saved.size} messages for $operator (capped at $MAX_CHAT_MESSAGES)")
            }
            historyLoaded = true
            // Resolve any unresolved media placeholders from prior sessions
            resolveUnresolvedMediaTasks()
        }
    }

    /** Track which tasks are being actively polled by the resolver (prevent duplicates) */
    private val _resolverPolling = mutableSetOf<String>()

    /**
     * Scan all messages for unresolved mediaTasks and resolve them.
     * Handles both prior session placeholders and current session tasks.
     *
     * For completed tasks: resolves immediately (replaces placeholder with media).
     * For pending tasks: starts its OWN polling loop (3s interval, self-contained).
     * For failed/missing tasks: removes the stale placeholder.
     *
     * This is the single source of truth for media task resolution — it does NOT
     * delegate to startTaskPolling() which has a separate, unreliable code path.
     */
    private fun resolveUnresolvedMediaTasks() {
        val repo = taskRepository ?: return
        val messages = _messages.value
        val unresolvedTasks = mutableListOf<Pair<String, String?>>() // (rawId, taskType)

        for (msg in messages) {
            for (entry in msg.mediaTasks) {
                val colonIdx = entry.indexOf(':')
                val taskType = if (colonIdx > 0) entry.substring(0, colonIdx) else null
                val rawId = if (colonIdx > 0) entry.substring(colonIdx + 1) else entry
                // Skip if already being polled by this resolver
                if (rawId !in _resolverPolling) {
                    unresolvedTasks.add(rawId to taskType)
                }
            }
        }

        if (unresolvedTasks.isEmpty()) return
        Log.d(TAG, "Found ${unresolvedTasks.size} unresolved media tasks — resolving")

        for ((taskId, taskType) in unresolvedTasks) {
            _resolverPolling.add(taskId)

            viewModelScope.launch {
                try {
                    // Poll until completed, failed, or not found (max ~25 min for videos)
                    var attempts = 0
                    val maxAttempts = 500  // 500 * 3s = 25 minutes max
                    while (attempts < maxAttempts) {
                        try {
                            val status = repo.getTaskStatus(taskId)

                            when {
                                status.status.equals("completed", true) && status.resultUrl != null -> {
                                    resolveMediaTaskInMessage(taskId, taskType, status.resultUrl!!)
                                    Log.d(TAG, "Resolved media task $taskId → ${status.resultUrl}")
                                    _resolverPolling.remove(taskId)
                                    return@launch
                                }
                                status.status.equals("failed", true) -> {
                                    removeMediaTaskFromMessage(taskId)
                                    Log.w(TAG, "Media task $taskId failed — removed placeholder")
                                    _resolverPolling.remove(taskId)
                                    return@launch
                                }
                                else -> {
                                    // Still pending/processing — wait and retry
                                    if (attempts % 10 == 0) {
                                        Log.d(TAG, "Media task $taskId: ${status.status} (attempt $attempts)")
                                    }
                                }
                            }
                        } catch (e: Exception) {
                            // Task not found on server — remove stale placeholder
                            removeMediaTaskFromMessage(taskId)
                            Log.w(TAG, "Media task $taskId not found — removed: ${e.message}")
                            _resolverPolling.remove(taskId)
                            return@launch
                        }

                        attempts++
                        delay(3000)  // Poll every 3 seconds
                    }

                    // Timed out after max attempts
                    Log.w(TAG, "Media task $taskId timed out after $maxAttempts attempts")
                    _resolverPolling.remove(taskId)
                } catch (e: Exception) {
                    Log.e(TAG, "Resolver failed for $taskId: ${e.message}")
                    _resolverPolling.remove(taskId)
                }
            }
        }
    }

    private fun persistHistory() {
        viewModelScope.launch {
            historyStore.save(currentOperator, _messages.value)
        }
    }

    fun initialize(origin: String) {
        if (origin.isNotBlank() && api == null) {
            api = BlackBoxApi(origin)
            repository = ChatRepository(api!!)
            taskRepository = TaskRepository(api!!)
            Log.d(TAG, "Initialized for $origin")
            startHealthLoop()
            startTaskDiscoveryLoop()

            // Build the on-device provider gating (Task 1.6) now the api is ready.
            // Disk reads run off the main thread (Dispatchers.IO).
            val picker = ProviderPickerViewModel.fromContext(
                context = appContext,
                api = api!!,
                operatorProvider = { currentOperator },
                ioDispatcher = Dispatchers.IO,
            )
            providerPicker = picker
            viewModelScope.launch {
                picker.localAvailable.collect { _localAvailable.value = it }
            }
            refreshLocalAvailability()

            // App-open flush (Task 2.5): drain any on-device turns queued offline
            // in a prior session now that the hub origin is set. Best-effort,
            // fire-and-forget; a still-offline flush leaves the items for later.
            snapshotQueueOrBuild()?.let { queue ->
                viewModelScope.launch {
                    try {
                        queue.flush()
                    } catch (e: Exception) {
                        Log.w(TAG, "local snapshot app-open flush failed (non-critical): ${e.message}")
                    }
                }
            }
        }
    }

    /**
     * Recompute on-device (LOCAL) provider availability and fire the best-effort
     * re-attest. Call when the picker opens so the gate reflects a just-installed
     * (or just-deleted) model and the BlackBox's binding record stays current.
     * Safe to call before [initialize] (no-op until the picker exists).
     */
    fun refreshLocalAvailability() {
        providerPicker?.refresh()
    }

    override fun onCleared() {
        // Cancel the picker's own CoroutineScope (separate from viewModelScope,
        // which the framework cancels automatically) so it doesn't leak.
        providerPicker?.dispose()
        // Release the on-device engine's native runtime (Task 2.6a). Safe even if
        // never loaded; close() is idempotent. Guarded so a native-layer throw on
        // teardown can't crash the VM disposal.
        runCatching { localEngine?.close() }
        localEngine = null
        super.onCleared()
    }

    private fun startHealthLoop() {
        viewModelScope.launch {
            while (true) {
                checkHealth()
                delay(60_000)
            }
        }
    }

    /** Poll /tasks/list every 2s to discover and update tasks — matches Portal TaskManager. */
    private fun startTaskDiscoveryLoop() {
        viewModelScope.launch {
            while (true) {
                try {
                    val response = api?.get("/tasks/list") ?: run { delay(2_000); continue }
                    val obj = json.parseToJsonElement(response).jsonObject
                    val tasksArr = obj["tasks"]?.jsonArray ?: run { delay(2_000); continue }
                    val serverTasks = tasksArr.mapNotNull { el ->
                        try {
                            val t = el.jsonObject
                            val status = t["status"]?.jsonPrimitive?.content ?: ""
                            TaskStatus(
                                taskId = t["task_id"]?.jsonPrimitive?.content ?: return@mapNotNull null,
                                taskType = t["task_type"]?.jsonPrimitive?.content,
                                status = status,
                                progress = t["progress"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                                operator = t["operator"]?.jsonPrimitive?.content,
                                resultUrl = t["result_url"]?.jsonPrimitive?.content
                            )
                        } catch (_: Exception) { null }
                    }

                    val existingIds = _activeTasks.value.map { it.taskId }.toSet()

                    // Update progress on existing active tasks
                    val activeServerTasks = serverTasks.filter { it.status in listOf("pending", "processing") }
                    if (_activeTasks.value.isNotEmpty()) {
                        _activeTasks.value = _activeTasks.value.map { existing ->
                            activeServerTasks.find { it.taskId == existing.taskId } ?: existing
                        }
                    }

                    // Add newly discovered tasks
                    val newTasks = activeServerTasks.filter { it.taskId !in existingIds }
                    if (newTasks.isNotEmpty()) {
                        _activeTasks.value = _activeTasks.value + newTasks
                        newTasks.forEach { startTaskPolling(it.taskId, it.taskType) }
                        Log.d(TAG, "Discovered ${newTasks.size} new active tasks")
                    }

                    // Remove completed/failed tasks that server no longer reports as active
                    val serverActiveIds = activeServerTasks.map { it.taskId }.toSet()
                    _activeTasks.value = _activeTasks.value.filter { task ->
                        task.taskId in serverActiveIds ||
                        task.status.equals("completed", true) ||
                        task.status.equals("failed", true)
                    }
                } catch (_: Exception) {
                    // Silent — task discovery is best-effort
                }
                delay(2_000)
            }
        }
    }

    fun onInputChange(value: TextFieldValue) {
        _inputText.value = value
    }

    // =========================================================================
    // Send Message — routes by provider type
    // Matches Portal's three-path branching:
    //   agents → WebSocket, voice → WebSocket, else → SSE
    // =========================================================================
    fun sendMessage(imageUrls: List<String> = emptyList()) {
        val text = _inputText.value.text.trim()
        if (text.isBlank()) return

        val repo = repository ?: run {
            Log.e(TAG, "Repository not initialized")
            return
        }

        val provider = ChatProvider.fromId(currentProvider)

        // Route by provider type (matches Portal sendChatMessage branching).
        // The branch SELECTION is factored into routeFor() so it unit-tests
        // without instantiating this AndroidViewModel.
        when (routeFor(provider, _erMissionActive.value)) {
            ChatRoute.AGENT -> {
                // Forward to AgentChatScreen via shared event
                _agentPromptEvent.tryEmit(text)
                _inputText.value = TextFieldValue()
            }
            ChatRoute.VOICE -> {
                // Voice providers handled by VoiceScreen, not chat
                Log.w(TAG, "Voice provider $currentProvider — use Voice screen")
            }
            // Robotics: mission running → inject prompt instead of new stream
            // Matches Portal chat-send.js window.__erMissionActive check
            ChatRoute.ER_INJECT -> injectErPrompt(text)
            // On-device (Gemma): run the turn on-device via the local engine.
            // Must NOT fall through to SSE (which would POST provider=local to the
            // cloud and error). With no engine wired yet (production default until
            // Task 2.6), sendViaLocalEngine() falls back to the 1.6 placeholder.
            // No cloud network call on this path either way.
            ChatRoute.LOCAL_PLACEHOLDER -> {
                // Block duplicate sends BEFORE routing — guards BOTH the engine
                // path and the placeholder fallback (mirrors the SSE arm).
                if (shouldBlockSend(_chatState.value)) return
                sendViaLocalEngine(text)
            }
            ChatRoute.SSE -> {
                // Block duplicate sends for non-robotics streaming (robotics handled above)
                if (shouldBlockSend(_chatState.value)) return
                sendViaSSE(text, imageUrls, repo)
            }
        }
    }

    /**
     * Run a turn on the on-device (`local`) engine — the Phase 2 replacement for
     * [sendViaLocalPlaceholder]. NEVER touches the cloud SSE path.
     *
     * Engine seam: [localLlmProvider] is NULL in production (the concrete
     * LiteRtEngine is Task 2.6), so with no engine wired we fall straight back to
     * the unchanged 1.6 [sendViaLocalPlaceholder]. With an engine present we:
     *   1. append the user message + a STREAMING assistant placeholder (the SAME
     *      shape the SSE path uses), so the UI shows the identical streaming
     *      affordance;
     *   2. snapshot the conversation-so-far into [FcLoop.Turn]s, EXCLUDING the
     *      just-appended user turn (it is passed as `text`) and the in-flight
     *      assistant placeholder;
     *   3. on [viewModelScope] fetch the persona on [Dispatchers.IO], run
     *      [FcLoop.runTurn] with its generation moved to IO via `.flowOn`, and
     *      stream the deltas into the SAME sink the SSE path uses
     *      ([updateLastMessage]) on the VM (Main) collector — exactly mirroring
     *      [sendViaSSE], which never mutates UI state from an IO thread;
     *   4. on completion mark streaming done and persist the turn via the existing
     *      save path ([saveConversation], tagged provider=local); on fault set
     *      [ChatState.ERROR] (parity with the SSE error path). The OFFLINE QUEUE
     *      is Task 2.5 — a direct save is used now, structured so 2.5 can swap the
     *      queue in at the [ChatViewModel.streamLocalTurn] save sink.
     *
     * The streaming + error handling lives in the pure [streamLocalTurn] so it is
     * unit-testable without the AndroidViewModel; this method is the wiring shim.
     */
    private fun sendViaLocalEngine(text: String) {
        // SINGLE job per send: cancel any prior in-flight turn, then run resolution
        // + (placeholder | engine turn) inside ONE viewModelScope.launch so
        // streamJob always points at the actual running turn (cancelStream/clear can
        // reliably cancel it) and two sends can't run concurrent generations racing
        // on _messages. (The LOCAL_PLACEHOLDER double-send guard in sendMessage
        // blocks the common case; this cancel covers the non-STREAMING windows.)
        streamJob?.cancel()
        streamJob = viewModelScope.launch {
            // Resolve the provider: an already-wired one (test fake or a
            // previously-wired production engine) is returned as-is; otherwise wire
            // the production LiteRtEngine from an INSTALLED model (suspend disk read).
            val provider = localProviderOrWire()
            if (provider == null) {
                // No installed model (or api not ready) → 1.6 placeholder, unchanged.
                sendViaLocalPlaceholder(text)
                return@launch
            }
            runLocalEngineTurn(text, provider)
        }
    }

    /**
     * Run one on-device turn through an already-resolved [provider]. A SUSPEND
     * function (NOT a launcher): it runs INSIDE the single [streamJob] coroutine
     * owned by [sendViaLocalEngine], so there is exactly one job per send (no nested
     * sibling launch, no orphaned/uncancellable job). For the production
     * [LiteRtEngine] this also LOADS the engine (idempotent ~10s on the first turn,
     * instant after) on [Dispatchers.IO] before streaming.
     */
    private suspend fun runLocalEngineTurn(text: String, provider: () -> LocalLlm) {
        // The double-send guard is hoisted to sendMessage's LOCAL_PLACEHOLDER arm
        // (mirrors the SSE arm), so this path is already guarded by the time we get here.

        // 1. Append the user message + a streaming assistant placeholder, exactly
        //    like sendViaSSE — same UiMessage shape, same _messages flow.
        val userMsg = UiMessage(
            role = "user",
            content = text,
            provider = currentProvider,
            model = currentModel,
        )
        val assistantMsg = UiMessage(
            role = "assistant",
            content = "",
            isStreaming = true,
            provider = currentProvider,
            model = currentModel,
        )
        _messages.value = (_messages.value + userMsg + assistantMsg).takeLast(MAX_CHAT_MESSAGES)
        _inputText.value = TextFieldValue()

        // 2. History for the prompt. BlackBox architecture: the ledger is the
        //    memory, so we DON'T carry the inter-turn transcript into the on-device
        //    prompt — we window it to the last [LOCAL_HISTORY_WINDOW_TURNS] turns
        //    (0 = fresh each turn). This keeps the per-turn prompt bounded (the
        //    accumulated transcript previously overran the engine's token window).
        //    The full visible conversation still lives in _messages (UI) and is
        //    minted to the ledger; recall will come from snapshot search later.
        val history = toFcHistory(_messages.value).takeLast(LOCAL_HISTORY_WINDOW_TURNS)

        _chatState.value = ChatState.STREAMING
        startBackgroundService("Generating on-device response...")

        val op = currentOperator
        val model = currentModel.ifBlank { null }
        // api?:return convention (mirrors persistLocalSave's repository?:return) —
        // no api!!. personaCacheOrBuild needs the api to wire the LocalModelApi.
        val cache = personaCacheOrBuild() ?: return

        run {
            // Threading (mirrors sendViaSSE): the ONLY IO-dispatched work is the
            // persona fetch + (inside streamLocalTurn) the generation Flow via
            // .flowOn(Dispatchers.IO). The collect/sink runs HERE on the VM's
            // dispatcher (Main.immediate in production) — _messages/_chatState
            // read-modify-write is NOT touched from an IO thread.
            //
            // Defense (M4): PersonaCache.get only catches IOException, so a non-IO
            // throw (e.g. serialization) could escape before streamLocalTurn's
            // .catch scope. Wrap persona fetch + collection so ANY throw surfaces
            // the same friendly local-engine error + ChatState.ERROR (no crash).
            var faulted: Boolean
            try {
                // Ensure the on-device engine is loaded BEFORE streaming. load() is
                // idempotent (~10s the first turn, instant after) and runs on IO so
                // it never blocks the main thread; the assistant bubble already shows
                // the streaming affordance during the gap. The persona fetch is on IO
                // too — load first (sequentially) so a load fault surfaces the same
                // friendly error. A FakeLocalLlm (tests) has no model file → skip load.
                val engineToLoad = localEngine
                val modelFile = localEngineModelFile
                if (engineToLoad != null && modelFile != null) {
                    withContext(Dispatchers.IO) {
                        engineToLoad.load(modelFile, localEngineDelegate)
                    }
                }
                val persona = withContext(Dispatchers.IO) { cache.get(op) }
                // 3. The SAME sink sendViaSSE uses — append each delta to the
                //    in-flight assistant message (identical rendering). Runs on the
                //    collector's dispatcher (Main), NOT Dispatchers.IO.
                val sink: (String, Boolean) -> Unit = { content, streaming ->
                    updateLastMessage(content = content, isStreaming = streaming, isThinking = false)
                }
                // 4. The SAME save path (direct now; Task 2.5 swaps the queue).
                val saveSink: (SaveRequest, String) -> Unit = { req, _ -> persistLocalSave(req) }

                // Capability-detect on the SINGLE provider() instance: the concrete
                // 2.6 engine implements BOTH LocalLlm and ToolCallingLlm. When it
                // does AND a bridge is available, run the tool-aware agent loop so
                // tool calls/results render inline (Task 3.3); otherwise fall back to
                // the unchanged text path (e.g. a text-only FakeLocalLlm → Task 2.4).
                val llm = provider()
                val bridge = toolBridgeOrBuild()
                val ok = if (llm is ToolCallingLlm && bridge != null) {
                    streamLocalAgentTurn(
                        // Phase 4.5: always wire the on-device phone controller. It
                        // reads the LIVE accessibility service via the singleton seam;
                        // if the service isn't enabled the actuators degrade
                        // gracefully ("not enabled") and read_screen returns "[]", so
                        // it is safe to always pass. When wired, FcLoop advertises the
                        // resident phone actuators and routes those calls locally —
                        // never to the cloud bridge.
                        //
                        // Phase 4.6 (autonomy gate): supply the REAL autonomy posture
                        // (read from the locally-persisted AutonomyStore, defaulting
                        // to PERMISSION — the SAFE, gating default — when never set or
                        // unreadable) and the system-overlay confirm UI. In Permission
                        // mode a high-consequence tap/type asks the user via the
                        // overlay before firing; in YOLO it runs immediately; benign
                        // actions never gate.
                        //
                        // Phase 4.7 (credential handoff): supply the system-overlay
                        // OverlayCredentialHandoff. When the model targets a password
                        // field, Actuators.type DISCARDS the model's attempted text and
                        // this overlay asks the USER to type the password directly into
                        // the field — the model never sees it in either direction.
                        fcLoop = FcLoop(
                            llm,
                            toolLlm = llm,
                            bridge = bridge,
                            operator = op,
                            phone = AndroidPhoneController.fromService(
                                appContext,
                                mode = { autonomyStore.load() },
                                confirm = OverlayConfirmUi(appContext),
                                credentialHandoff = OverlayCredentialHandoff(appContext),
                            ),
                        ),
                        persona = persona,
                        history = history,
                        text = text,
                        operator = op,
                        model = model,
                        sink = sink,
                        saveSink = saveSink,
                    )
                } else {
                    streamLocalTurn(
                        fcLoop = FcLoop(llm),
                        persona = persona,
                        history = history,
                        text = text,
                        operator = op,
                        model = model,
                        sink = sink,
                        saveSink = saveSink,
                    )
                }
                faulted = !ok
            } catch (e: kotlinx.coroutines.CancellationException) {
                // I1 (review): cancellation is NOT a fault. Stopping the turn or
                // starting a new one calls streamJob?.cancel(), which throws
                // CancellationException here — it IS an Exception, so the generic
                // catch below would paint a spurious "[on-device error]", flip state
                // to ERROR, and stopBackgroundService() (killing the NEW turn's
                // service). Rethrow so the launch ends cancelled and the trailing
                // state/service/persist cleanup is SKIPPED; the canceller
                // (cancelStream/clearHistory) owns its own cleanup.
                throw e
            } catch (e: Exception) {
                // A throw OUTSIDE streamLocalTurn's Flow scope (e.g. persona fetch).
                Log.e(TAG, "local engine error before stream: ${e.message}", e)
                val partial = _messages.value.lastOrNull()?.content ?: ""
                updateLastMessage(
                    content = partial + LOCAL_ENGINE_ERROR_TEXT,
                    isStreaming = false,
                    isThinking = false,
                )
                faulted = true
            }
            // Mirror the SSE paths: fault → ERROR, success → IDLE (don't clobber a
            // terminal state already set by the stream). Mapping is the pure
            // stateAfterLocalTurn so it is unit-testable.
            _chatState.value = stateAfterLocalTurn(faulted, _chatState.value)
            stopBackgroundService()
            persistHistory()
        }
    }

    /**
     * Build (once) or return the persona cache wired to this VM's api + context.
     * Returns null when the api is not yet initialized (api?:return convention,
     * mirroring persistLocalSave's `repository ?: return`) — no `api!!`.
     */
    private fun personaCacheOrBuild(): PersonaCache? {
        personaCache?.let { return it }
        val client = api ?: return null
        // PersonaSource is the LocalModelApi slice (mirrors ProviderPicker wiring).
        val built = PersonaCache.fromContext(appContext, LocalModelApi(client))
        personaCache = built
        return built
    }

    /**
     * Build (once) or return the two-hop tool bridge wired to this VM's api.
     * Returns null when the api is not yet initialized (api?:return convention,
     * mirroring [personaCacheOrBuild]) — no `api!!`. With no bridge, the local path
     * stays on the text-only [streamLocalTurn] (you cannot run the agent loop
     * without a bridge), so this degrades to the Task 2.4 behaviour rather than
     * crashing.
     */
    private fun toolBridgeOrBuild(): ToolBridge? {
        toolBridge?.let { return it }
        val client = api ?: return null
        val built = ToolBridgeClient(client)
        toolBridge = built
        return built
    }

    /**
     * Resolve the on-device [localLlmProvider] (Task 2.6a), lazily wiring the
     * SINGLETON production [LiteRtEngine] the first time an INSTALLED model is
     * present.
     *
     *  - If [localLlmProvider] is already set (a test fake, or a previously-wired
     *    engine) it is returned unchanged — the test seam is never disturbed.
     *  - Otherwise this reads the installed bundles via [LocalModelManager]
     *    ([installedModels] is device-scoped + runs on IO), picks the first
     *    attested/installed bundle, builds the engine singleton ([LiteRtEngine.fromInstalled],
     *    delegate "cpu"), records its model file + delegate for [load], sets
     *    [localLlmProvider] to return that singleton, and returns it.
     *  - If NO model is installed (or the api is not ready) it returns null and
     *    [localLlmProvider] stays null — the caller falls back to the placeholder.
     *
     * The engine is built ONCE (its ~10s initialize() happens in [load], also
     * once) and reused across turns. Suspends only for the disk read.
     */
    private suspend fun localProviderOrWire(): (() -> LocalLlm)? {
        localLlmProvider?.let { return it }
        val client = api ?: return null

        // Device-scoped model discovery (deviceId is irrelevant to installedModels()
        // — it only matters for attest/setAutonomy — so a stable constant is fine
        // here; the picker/Model-Manager own the real attest flow).
        val manager = LocalModelManager.fromContext(appContext, LocalModelApi(client), deviceId = "android-device")
        val installed = runCatching { manager.installedModels() }.getOrDefault(emptyList())
        // M1 (review): firstOrNull → installedModels() is sorted, so this picks the
        // alphabetically-first slug (gemma-4-e2b, the LIGHTER model) — a safe RAM
        // default. Only one model is installed at a time today; TODO if multiple can
        // coexist, prefer LocalModelManager.recommendForDevice() among the installed.
        val bundle = installed.firstOrNull() ?: return null // no model → placeholder path

        // Build the singleton engine for the installed bundle (default CPU delegate),
        // threading the PER-MODEL config (Task W2): maxTokens (fallback to the
        // engine default when the descriptor leaves it null) + the sampler trio.
        val cfg = bundle.config
        val engine = LiteRtEngine.fromInstalled(
            appContext,
            bundle.file,
            delegate = "cpu",
            maxTokens = cfg.maxTokens ?: LiteRtEngine.DEFAULT_MAX_TOKENS,
            sampler = SamplerSettings(
                topK = cfg.topK,
                topP = cfg.topP,
                temperature = cfg.temperature,
            ),
        )
        localEngine = engine
        localEngineModelFile = bundle.file
        localEngineDelegate = "cpu"
        val provider: () -> LocalLlm = { engine }
        localLlmProvider = provider
        return provider
    }

    /**
     * Persist a completed on-device turn through the offline-resilient snapshot
     * queue (Task 2.5). The on-device engine works OFFLINE, so the turn is queued
     * to disk first (survives process death) and flushed in FIFO order when
     * connectivity returns — the memory ledger never drops or reorders a turn.
     * Tagged provider=local for traceability (same as Task 2.4's save sink);
     * SaveRequest itself carries no provider field, matching the cloud save shape.
     *
     * Best-effort + non-blocking: enqueue persists-then-flushes on viewModelScope;
     * a failed flush leaves the item queued for the next attempt (app-open flush
     * in [initialize], or the next turn's enqueue). Non-IO errors propagate inside
     * the queue without dropping the item.
     */
    private fun persistLocalSave(request: SaveRequest) {
        val queue = snapshotQueueOrBuild() ?: return
        viewModelScope.launch {
            try {
                queue.enqueue(request)
            } catch (e: Exception) {
                // enqueue persists the item before flushing, so a throw here (a
                // non-IO flush error) still leaves the turn queued for next time.
                Log.w(TAG, "local snapshot enqueue/flush failed (non-critical): ${e.message}")
            }
        }
    }

    /**
     * Build (once) or return the offline snapshot queue wired to this VM's
     * repository + context. Returns null when the repository is not yet ready
     * (repository ?: return convention, matching [persistLocalSave]'s prior shape).
     */
    private fun snapshotQueueOrBuild(): LocalSnapshotQueue? {
        snapshotQueue?.let { return it }
        val repo = repository ?: return null
        val built = LocalSnapshotQueue.fromContext(appContext, repo)
        snapshotQueue = built
        return built
    }

    /**
     * SAFE placeholder for the on-device `local` provider. Appends the user's
     * message plus a friendly assistant note and returns WITHOUT any network
     * call — selecting LOCAL must never reach the cloud SSE path. Used as the
     * fallback by [sendViaLocalEngine] until the concrete engine lands (Task 2.6).
     */
    private fun sendViaLocalPlaceholder(text: String) {
        val userMsg = UiMessage(
            role = "user",
            content = text,
            provider = currentProvider,
            model = currentModel,
        )
        val placeholder = buildLocalPlaceholder(currentProvider, currentModel)
        _messages.value = (_messages.value + userMsg + placeholder).takeLast(MAX_CHAT_MESSAGES)
        _inputText.value = TextFieldValue()
        persistHistory()
    }

    /**
     * Inject a prompt into a running ER mission via POST /robotics/mission/prompt.
     * Matches Portal chat-send.js lines 2281-2302.
     */
    private fun injectErPrompt(text: String) {
        val apiClient = api ?: return

        // Show the user message in chat immediately
        val userMsg = UiMessage(
            role = "user",
            content = text,
            provider = currentProvider,
            model = currentModel
        )
        _messages.value = _messages.value + userMsg
        _inputText.value = TextFieldValue()

        viewModelScope.launch {
            try {
                val body = buildJsonObject {
                    put("operator", currentOperator)
                    put("text", text)
                }.toString()
                val response = apiClient.post("/robotics/mission/prompt", body)
                val obj = json.parseToJsonElement(response).jsonObject
                val queued = obj["queued"]?.jsonPrimitive?.content?.toBoolean() == true
                val position = obj["position"]?.jsonPrimitive?.content?.toIntOrNull() ?: 1
                if (queued) {
                    Log.d(TAG, "ER prompt queued at position $position")
                } else {
                    val error = obj["error"]?.jsonPrimitive?.content ?: "No active mission"
                    Log.w(TAG, "ER prompt not queued: $error")
                    // Mission may have ended — clear flag and fall through to normal send
                    _erMissionActive.value = false
                }
            } catch (e: Exception) {
                Log.e(TAG, "ER prompt injection failed: ${e.message}")
                _erMissionActive.value = false
            }
            persistHistory()
        }
    }

    // =========================================================================
    // SSE Streaming — handles all 30+ event types from Portal chat-send.js
    // =========================================================================
    private fun sendViaSSE(text: String, imageUrls: List<String>, repo: ChatRepository) {
        // Append user message
        val userMsg = UiMessage(
            role = "user",
            content = text,
            images = imageUrls,
            provider = currentProvider,
            model = currentModel
        )
        _messages.value = _messages.value + userMsg
        _inputText.value = TextFieldValue()

        // Create placeholder assistant message
        val assistantMsg = UiMessage(
            role = "assistant",
            content = "",
            isStreaming = true,
            provider = currentProvider,
            model = currentModel
        )
        _messages.value = _messages.value + assistantMsg

        // Trim oldest messages to keep UI responsive (matches Portal MAX_HISTORY_ITEMS=100)
        if (_messages.value.size > MAX_CHAT_MESSAGES) {
            _messages.value = _messages.value.takeLast(MAX_CHAT_MESSAGES)
        }

        _chatState.value = ChatState.STREAMING
        startBackgroundService("Generating response...")

        val history = buildApiHistory()

        // CU: set status to running when sending a computer-use request
        val isCuProvider = currentProvider == "computer-use"
        if (isCuProvider) {
            _cuStatus.value = "running"
            _cuStep.value = 0
            _cuStepTotal.value = 0
            _cuActionLabel.value = ""
        }

        streamJob = viewModelScope.launch {
            val content = StringBuilder()
            val reasoning = StringBuilder()
            var tokenCount: TokenCount? = null
            var streamModel: String? = null
            var provenance: Provenance? = null
            val mediaTasks = mutableListOf<String>()

            // CU params (only when provider is computer-use)
            val cuSessionId = if (isCuProvider) _cuSessionId.value else null
            val cuDeviceId = if (isCuProvider) _cuDeviceId.value else null

            // Robotics ER camera (only when provider is robotics)
            val erCamera = if (currentProvider == "robotics") _erCamera.value else null

            try {
                val flow = if (imageUrls.isEmpty()) {
                    repo.sendStream(text, history, currentOperator, currentProvider, currentModel.ifBlank { null },
                        sessionId = cuSessionId, deviceId = cuDeviceId, camera = erCamera)
                } else {
                    repo.sendStreamMultimodal(text, imageUrls, history, currentOperator, currentProvider, currentModel.ifBlank { null },
                        sessionId = cuSessionId, deviceId = cuDeviceId, camera = erCamera)
                }

                flow.collect { event ->
                    processSSEEvent(
                        event, content, reasoning, mediaTasks
                    ) { model, tokens, prov ->
                        if (model != null) streamModel = model
                        if (tokens != null) tokenCount = tokens
                        if (prov != null) provenance = prov
                    }

                    // Update the streaming assistant message
                    updateLastMessage(
                        content = content.toString(),
                        reasoning = reasoning.toString().ifBlank { null },
                        isStreaming = true,
                        isThinking = _chatState.value == ChatState.THINKING,
                        model = streamModel,
                        mediaTasks = mediaTasks.toList()
                    )
                }

                // Stream complete — finalize
                _chatState.value = ChatState.IDLE
                stopBackgroundService()
                if (isCuProvider && _cuStatus.value == "running") {
                    _cuStatus.value = "complete"
                }
                updateLastMessage(
                    content = content.toString(),
                    reasoning = reasoning.toString().ifBlank { null },
                    isStreaming = false,
                    isThinking = false,
                    model = streamModel,
                    tokens = tokenCount,
                    provenance = provenance,
                    mediaTasks = mediaTasks.toList()
                )

                // Resolve media tasks — uses the robust resolver that handles
                // completed, pending, and failed tasks in one sweep
                resolveUnresolvedMediaTasks()

                // Save conversation for snapshot (matches Portal /chat/save).
                // provenance is forwarded so backend auto-mint records context lineage.
                saveConversation(text, content.toString(), reasoning.toString(), streamModel, tokenCount, provenance)

                // Persist to local storage
                persistHistory()

                // Auto-TTS: speak the response if enabled
                // Matches Portal window.triggerAutoTTS() called after full response
                if (autoTtsEnabled && content.isNotBlank()) {
                    _autoTtsEvent.tryEmit(content.toString())
                }

            } catch (e: Exception) {
                Log.e(TAG, "SSE error: ${e.message}", e)
                _chatState.value = ChatState.ERROR
                stopBackgroundService()
                updateLastMessage(
                    content = content.toString().ifBlank { "Error: ${e.message}" },
                    isStreaming = false,
                    isThinking = false
                )
            }
        }
    }

    // =========================================================================
    // Process SSE Event — aligned with Portal's 30+ event type handler
    // =========================================================================
    private fun processSSEEvent(
        event: SSEEvent,
        content: StringBuilder,
        reasoning: StringBuilder,
        mediaTasks: MutableList<String>,
        onMeta: (model: String?, tokens: TokenCount?, provenance: Provenance?) -> Unit
    ) {
        when (event.event) {
            // ── Stream lifecycle ──
            "stream_start" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val model = obj["model"]?.jsonPrimitive?.content
                    // Provenance is embedded in stream_start.data.provenance — backend
                    // does NOT send a standalone "provenance" SSE event for /chat/stream
                    // (the dead branch below is a defensive fallback for WS-bridged paths).
                    val parsedProv = extractProvenanceFromStreamStart(event.data)
                    onMeta(model, null, parsedProv)
                    if (parsedProv != null) {
                        Log.d(TAG, "stream_start provenance: " +
                              "recent=${parsedProv.recent.size} " +
                              "keyword=${parsedProv.keyword.size} " +
                              "semantic=${parsedProv.semantic.size} " +
                              "checkpoint=${parsedProv.checkpoint.size}")
                    }
                } catch (_: Exception) {}
            }
            "content_start" -> {
                // Transition from thinking to content (Portal uses this as marker)
                if (_chatState.value == ChatState.THINKING) {
                    _chatState.value = ChatState.STREAMING
                }
            }
            "content" -> {
                content.append(event.data)
                if (_chatState.value == ChatState.THINKING) {
                    _chatState.value = ChatState.STREAMING
                }
            }
            "stream_end" -> {
                _chatState.value = ChatState.IDLE
            }
            "done" -> {
                // "done" carries the full accumulated response as fallback.
                // Gemini: {"thinking": "...", "content": "..."}
                // Anthropic/OpenAI: {"reasoning": "...", "content": "..."}
                // If streaming missed content, this ensures we have the full response.
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val doneContent = obj["content"]?.jsonPrimitive?.content
                    val doneThinking = obj["thinking"]?.jsonPrimitive?.content
                        ?: obj["reasoning"]?.jsonPrimitive?.content
                    // Use done content if our streaming buffer is empty or shorter
                    if (!doneContent.isNullOrBlank() && doneContent.length > content.length) {
                        content.clear()
                        content.append(doneContent)
                    }
                    if (!doneThinking.isNullOrBlank() && doneThinking.length > reasoning.length) {
                        reasoning.clear()
                        reasoning.append(doneThinking)
                    }
                } catch (_: Exception) {
                    // done data might be a plain string — ignore parse failure
                }
                _chatState.value = ChatState.IDLE
                // Clear ER mission state when stream completes
                if (currentProvider == "robotics") {
                    _erMissionActive.value = false
                }
            }

            // ── Thinking/reasoning ──
            "thinking_start" -> {
                _chatState.value = ChatState.THINKING
            }
            "thinking" -> {
                reasoning.append(event.data)
                if (_chatState.value != ChatState.THINKING) {
                    _chatState.value = ChatState.THINKING
                }
            }
            "thinking_end" -> {
                // Thinking phase complete — content will follow
                // State transitions to STREAMING on next content event
            }

            // ── Usage & metadata ──
            "usage" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val prompt = obj["prompt_tokens"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                    val completion = obj["completion_tokens"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                    onMeta(null, TokenCount(prompt, completion), null)
                } catch (_: Exception) {}
            }
            "provenance" -> {
                val parsed = parseProvenance(event.data)
                if (parsed != null) {
                    onMeta(null, null, parsed)
                    Log.d(TAG, "provenance: recent=${parsed.recent.size} " +
                          "keyword=${parsed.keyword.size} semantic=${parsed.semantic.size} " +
                          "checkpoint=${parsed.checkpoint.size}")
                } else {
                    Log.w(TAG, "provenance event unparseable: ${event.data.take(200)}")
                }
            }

            // ── Media generation tasks ──
            "image_task", "video_task", "music_task" -> {
                // Backend returns task_id for async generation
                // Prefix with type so placeholder knows which animation to show
                val typePrefix = when (event.event) {
                    "image_task" -> "image:"
                    "video_task" -> "video:"
                    "music_task" -> "music:"
                    else -> ""
                }
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val taskId = obj["task_id"]?.jsonPrimitive?.content
                    if (taskId != null) {
                        mediaTasks.add("$typePrefix$taskId")
                        Log.d(TAG, "${event.event}: $taskId")
                    }
                } catch (_: Exception) {
                    // task_id might be sent as plain string
                    if (event.data.isNotBlank()) {
                        mediaTasks.add("$typePrefix${event.data.trim()}")
                    }
                }
            }

            // ── Computer Use events — matches Portal chat-send.js ──
            "cu_screenshot" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val url = obj["url"]?.jsonPrimitive?.content
                    if (url != null) {
                        val baseUrl = api?.getBaseUrl() ?: ""
                        val fullUrl = if (url.startsWith("http")) url else "$baseUrl$url"
                        _cuScreenshotUrl.value = "$fullUrl?t=${System.currentTimeMillis()}"
                    }
                    val step = obj["step"]?.jsonPrimitive?.content?.toIntOrNull()
                    if (step != null) _cuStep.value = step
                } catch (_: Exception) {}
                _cuStatus.value = "running"
                Log.d(TAG, "CU screenshot update")
            }
            "cu_action" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val action = obj["action"]?.jsonPrimitive?.content ?: ""
                    val step = obj["step"]?.jsonPrimitive?.content?.toIntOrNull()
                    _cuActionLabel.value = action
                    if (step != null) _cuStep.value = step
                    // Append action summary to content for chat history
                    content.append("\n`[$action]` ")
                } catch (_: Exception) {}
                _cuStatus.value = "running"
            }
            "cu_bash_output" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val cmd = obj["command"]?.jsonPrimitive?.content ?: ""
                    val output = obj["output"]?.jsonPrimitive?.content ?: ""
                    content.append("\n```\n$ $cmd\n$output\n```\n")
                } catch (_: Exception) {}
            }
            "cu_file_edit" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val cmd = obj["command"]?.jsonPrimitive?.content ?: "edit"
                    val path = obj["path"]?.jsonPrimitive?.content ?: ""
                    content.append("\n`[$cmd $path]` ")
                } catch (_: Exception) {}
            }
            "cu_step" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    _cuStep.value = obj["step"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                    _cuStepTotal.value = obj["total"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0
                } catch (_: Exception) {}
                _cuStatus.value = "running"
            }
            "cu_session" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val sessionId = obj["session_id"]?.jsonPrimitive?.content
                    if (sessionId != null) _cuSessionId.value = sessionId
                    val deviceId = obj["device_id"]?.jsonPrimitive?.content
                    if (deviceId != null) _cuDeviceId.value = deviceId
                } catch (_: Exception) {}
            }
            "cu_stopped", "cu_task_stopped" -> {
                _cuStatus.value = "stopped"
            }

            // ── Robotics ER events — matches Portal chat-send.js ──
            "er_frame" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val image = obj["image"]?.jsonPrimitive?.content
                    val camera = obj["camera"]?.jsonPrimitive?.content
                    if (image != null) _erCameraFrame.value = image
                    if (camera != null) _erCamera.value = camera
                } catch (_: Exception) {}
                _erStatus.value = "connected"
                Log.d(TAG, "ER camera frame received")
            }
            "er_reasoning" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val text = obj["text"]?.jsonPrimitive?.content ?: ""
                    _erReasoning.value = text
                    Log.d(TAG, "ER reasoning: ${text.take(80)}...")
                } catch (_: Exception) {}
                _erStatus.value = "connected"
            }
            "er_action" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val tool = obj["tool"]?.jsonPrimitive?.content ?: ""
                    val args = obj["args"]?.toString() ?: ""
                    val status = obj["status"]?.jsonPrimitive?.content ?: "executing"
                    content.append("\n`[$tool]` $args → $status ")
                } catch (_: Exception) {}
                _erStatus.value = "connected"
            }
            "er_status" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val state = obj["state"]?.jsonPrimitive?.content ?: "idle"
                    _erStatus.value = state
                } catch (_: Exception) {}
            }
            "er_error" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val error = obj["error"]?.jsonPrimitive?.content ?: "Unknown robot error"
                    content.append("\n\n**Robot Error:** $error")
                    _erStatus.value = "offline"
                } catch (_: Exception) {}
            }
            "er_mission_start" -> {
                _erMissionActive.value = true
                _erStatus.value = "mission_active"
                Log.d(TAG, "ER mission started")
            }
            "er_stopped" -> {
                _erMissionActive.value = false
                _erStatus.value = "stopped"
                Log.d(TAG, "ER mission stopped")
            }
            "er_timeout" -> {
                _erMissionActive.value = false
                _erStatus.value = "timeout"
                Log.d(TAG, "ER mission timed out")
            }
            "er_queued" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val pos = obj["position"]?.jsonPrimitive?.content?.toIntOrNull() ?: 1
                    Log.d(TAG, "ER prompt queued at position $pos")
                } catch (_: Exception) {}
            }
            "er_prompt_injected" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val injText = obj["text"]?.jsonPrimitive?.content ?: ""
                    Log.d(TAG, "ER prompt injected: ${injText.take(80)}")
                } catch (_: Exception) {}
            }
            "er_nav_status" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val goalStatus = obj["goal_status"]?.jsonPrimitive?.content ?: ""
                    Log.d(TAG, "ER nav: $goalStatus")
                    _erStatus.value = "navigating"
                } catch (_: Exception) {}
            }
            "er_session" -> {
                try {
                    val obj = json.parseToJsonElement(event.data).jsonObject
                    val sessionId = obj["session_id"]?.jsonPrimitive?.content
                    Log.d(TAG, "ER session: $sessionId")
                } catch (_: Exception) {}
            }

            // ── Keep-alive ──
            "heartbeat" -> {
                // Ignore — just prevents connection timeout
            }

            // ── Error ──
            "error" -> {
                content.append("\n\n${event.data}")
                _chatState.value = ChatState.ERROR
            }

            // ── Unknown events — log but don't crash ──
            else -> {
                Log.d(TAG, "Unhandled SSE event: ${event.event}")
            }
        }
    }

    // =========================================================================
    // Update streaming message
    // =========================================================================
    private fun updateLastMessage(
        content: String,
        reasoning: String? = null,
        isStreaming: Boolean = true,
        isThinking: Boolean = false,
        model: String? = null,
        tokens: TokenCount? = null,
        provenance: Provenance? = null,
        mediaTasks: List<String> = emptyList()
    ) {
        val current = _messages.value.toMutableList()
        if (current.isNotEmpty()) {
            val last = current.last()
            current[current.lastIndex] = last.copy(
                content = content,
                reasoning = reasoning ?: last.reasoning,
                isStreaming = isStreaming,
                isThinking = isThinking,
                model = model ?: last.model,
                tokens = tokens ?: last.tokens,
                provenance = provenance ?: last.provenance,
                mediaTasks = mediaTasks.ifEmpty { last.mediaTasks }
            )
            _messages.value = current
        }
    }

    // =========================================================================
    // Build API history
    // =========================================================================
    /**
     * No chat history sent — the backend's build_streaming_context() provides all context
     * via snapshot/fossil retrieval (recent, keyword, semantic, checkpoint).
     * Sending history is redundant and causes issues (Anthropic empty-content errors, token waste).
     */
    private fun buildApiHistory(): List<ChatMessage> = emptyList()

    // =========================================================================
    // Save conversation for snapshot
    // =========================================================================
    private fun saveConversation(
        userMessage: String,
        assistantResponse: String,
        reasoning: String,
        model: String?,
        tokens: TokenCount?,
        provenance: Provenance? = null
    ) {
        val repo = repository ?: return
        val request = buildSaveRequest(
            operator = currentOperator,
            userMessage = userMessage,
            assistantResponse = assistantResponse,
            reasoning = reasoning,
            model = model ?: currentModel,
            tokens = tokens,
            provenance = provenance,
        )
        viewModelScope.launch {
            try {
                repo.saveConversation(request)
            } catch (e: Exception) {
                Log.w(TAG, "saveConversation failed (non-critical): ${e.message}")
            }
        }
    }

    // =========================================================================
    // Public actions
    // =========================================================================
    fun clearHistory() {
        streamJob?.cancel()
        _messages.value = emptyList()
        _chatState.value = ChatState.IDLE
        viewModelScope.launch { historyStore.clear(currentOperator) }
    }

    fun cancelStream() {
        streamJob?.cancel()
        _chatState.value = ChatState.IDLE
        updateLastMessage(
            content = _messages.value.lastOrNull()?.content ?: "",
            isStreaming = false,
            isThinking = false
        )
    }

    fun checkHealth() {
        val currentApi = api ?: return
        viewModelScope.launch {
            try {
                val response = currentApi.get("/health")
                val obj = json.parseToJsonElement(response).jsonObject
                _isHealthy.value = obj["status"]?.jsonPrimitive?.content == "ok"
                _snapshotCount.value = obj["snapshot_count"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0

                obj["operator_turns"]?.jsonObject?.get(currentOperator)?.jsonObject
                    ?.get("turns_until_checkpoint")?.jsonPrimitive?.content?.toIntOrNull()?.let {
                        _checkpointTurns.value = it
                    }

                obj["users"]?.jsonObject?.get("list")?.jsonArray?.let { arr ->
                    val ops = arr.mapNotNull {
                        try { it.jsonPrimitive.content } catch (_: Exception) { null }
                    }
                    if (ops.isNotEmpty()) _operators.value = ops
                }
            } catch (e: Exception) {
                _isHealthy.value = false
            }
        }
    }

    /** Remove a completed media task from pending list */
    fun completeMediaTask(taskId: String) {
        _pendingMediaTasks.value = _pendingMediaTasks.value - taskId
        _activeTasks.value = _activeTasks.value.filter { it.taskId != taskId }
    }

    /**
     * Replace a media task placeholder with actual media content in the message.
     * Finds the message containing this taskId, adds the result URL to images/content,
     * and removes the task from mediaTasks so the placeholder disappears.
     */
    private fun resolveMediaTaskInMessage(taskId: String, taskType: String?, resultUrl: String) {
        val current = _messages.value.toMutableList()
        // The task entry in mediaTasks is prefixed with type: "image:taskId"
        val prefixedEntries = listOf("image:$taskId", "video:$taskId", "music:$taskId", taskId)

        for (i in current.indices) {
            val msg = current[i]
            val matchingEntry = msg.mediaTasks.firstOrNull { entry ->
                prefixedEntries.any { prefix -> entry == prefix || entry.endsWith(taskId) }
            }
            if (matchingEntry != null) {
                // Determine media type from the prefix or taskType
                val isImage = matchingEntry.startsWith("image:") ||
                    taskType?.contains("image", true) == true ||
                    resultUrl.matches(Regex(".*\\.(png|jpg|jpeg|webp|gif)$", RegexOption.IGNORE_CASE))
                val isAudio = matchingEntry.startsWith("music:") ||
                    taskType?.contains("music", true) == true ||
                    resultUrl.matches(Regex(".*\\.(wav|mp3|m4a|ogg|flac)$", RegexOption.IGNORE_CASE))

                // Build full URL if relative
                val fullUrl = if (resultUrl.startsWith("http")) resultUrl
                    else "${api?.getBaseUrl() ?: ""}$resultUrl"

                val updatedMsg = if (isImage) {
                    // Add to images list — ChatBubble renders these with AsyncImage
                    msg.copy(
                        images = msg.images + fullUrl,
                        mediaTasks = msg.mediaTasks.filter { it != matchingEntry }
                    )
                } else {
                    // Video/audio — append URL to content so inline media extractor renders it
                    val urlLine = "\n$fullUrl\n"
                    msg.copy(
                        content = msg.content + urlLine,
                        mediaTasks = msg.mediaTasks.filter { it != matchingEntry }
                    )
                }
                current[i] = updatedMsg
                _messages.value = current
                persistHistory()
                Log.d(TAG, "Resolved media task $taskId → $fullUrl (${if (isImage) "image" else "media"})")
                return
            }
        }
        Log.w(TAG, "No message found for task $taskId — result URL: $resultUrl")
    }

    /** Remove a failed task's placeholder from the message. */
    private fun removeMediaTaskFromMessage(taskId: String) {
        val current = _messages.value.toMutableList()
        for (i in current.indices) {
            val msg = current[i]
            val matchingEntry = msg.mediaTasks.firstOrNull { it.endsWith(taskId) || it == taskId }
            if (matchingEntry != null) {
                current[i] = msg.copy(
                    mediaTasks = msg.mediaTasks.filter { it != matchingEntry }
                )
                _messages.value = current
                return
            }
        }
    }

    /**
     * Start polling a task. Called when media tasks arrive from SSE or generation screens.
     * Matches Portal's TaskManager 3-second polling loop.
     */
    fun startTaskPolling(taskId: String, taskType: String? = null) {
        val repo = taskRepository ?: return
        // Skip if already tracking this task
        if (_activeTasks.value.any { it.taskId == taskId }) return
        // Add to pending
        if (taskId !in _pendingMediaTasks.value) {
            _pendingMediaTasks.value = _pendingMediaTasks.value + taskId
        }
        // Add initial status to active tasks
        val initial = TaskStatus(
            taskId = taskId,
            taskType = taskType,
            status = "pending"
        )
        _activeTasks.value = _activeTasks.value + initial

        // Start polling flow
        viewModelScope.launch {
            try {
                repo.pollTask(taskId, intervalMs = 1500).collect { status ->
                    // Update active tasks list
                    _activeTasks.value = _activeTasks.value.map {
                        if (it.taskId == taskId) status else it
                    }

                    val isDone = status.status.equals("completed", true)
                    val isFailed = status.status.equals("failed", true)
                    if (isDone || isFailed) {
                        // Emit completion event for notifications
                        _taskCompletedEvent.tryEmit(status)

                        // Replace placeholder with actual media in the message
                        if (isDone && status.resultUrl != null) {
                            resolveMediaTaskInMessage(taskId, taskType, status.resultUrl!!)
                        } else {
                            // Failed — just remove the placeholder
                            removeMediaTaskFromMessage(taskId)
                        }

                        delay(2000)
                        completeMediaTask(taskId)
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Task polling failed for $taskId: ${e.message}")
                completeMediaTask(taskId)
            }
        }
    }

    /** Set the TTS audio URL on a specific message by ID, triggering the inline player */
    fun setMessageTtsAudioUrl(messageId: String, audioUrl: String) {
        val current = _messages.value.toMutableList()
        val idx = current.indexOfFirst { it.id == messageId }
        if (idx >= 0) {
            current[idx] = current[idx].copy(ttsAudioUrl = audioUrl, ttsGenerating = false)
            _messages.value = current
        }
    }

    fun setMessageTtsGenerating(messageId: String, generating: Boolean) {
        val current = _messages.value.toMutableList()
        val idx = current.indexOfFirst { it.id == messageId }
        if (idx >= 0) {
            current[idx] = current[idx].copy(ttsGenerating = generating)
            _messages.value = current
        }
    }

    fun getApi(): BlackBoxApi? = api

    /** Start foreground service to keep tasks alive when app is backgrounded */
    private fun startBackgroundService(label: String) {
        try {
            val intent = android.content.Intent(appContext, com.aiblackbox.portal.BackgroundTaskService::class.java).apply {
                action = com.aiblackbox.portal.BackgroundTaskService.ACTION_START
                putExtra(com.aiblackbox.portal.BackgroundTaskService.EXTRA_TASK_LABEL, label)
            }
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                appContext.startForegroundService(intent)
            } else {
                appContext.startService(intent)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not start background service: ${e.message}")
        }
    }

    private fun stopBackgroundService() {
        try {
            val intent = android.content.Intent(appContext, com.aiblackbox.portal.BackgroundTaskService::class.java).apply {
                action = com.aiblackbox.portal.BackgroundTaskService.ACTION_STOP
            }
            appContext.startService(intent)
        } catch (_: Exception) {}
    }

    fun getProviderLabel(): String {
        val provider = ChatProvider.fromId(currentProvider)
        return if (currentModel.isNotBlank()) {
            "${provider.displayName} \u00B7 $currentModel"
        } else {
            provider.displayName
        }
    }

    // =========================================================================
    // Dynamic model fetching — matches Portal fetchAvailableModels()
    // =========================================================================

    /** Map provider IDs to the backend's expected provider key.
     *  Note: Android uses "gemini" as provider key but backend uses "google".
     *  T3 (2026-05-18) added xai mapping — previously a gap that left
     *  the xai dropdown stuck on the malformed Constants.MODEL_CONFIG
     *  fallback (which had IDs like "grok-4.1-fast" that don't exist
     *  in the xAI API). */
    private fun mapProviderForApi(provider: String): String? = when (provider) {
        "gemini" -> "google"
        "anthropic" -> "anthropic"
        "openai" -> "openai"
        "xai" -> "xai"
        // CU production pass 2026-06: the backend exposes GET /models/computer-use
        // (merged live Anthropic/Google/OpenAI CU catalogs with `backend` field).
        "computer-use" -> "computer-use"
        else -> null // Voice/agent providers don't have model endpoints
    }

    // In-memory cache for fetched model lists (5min TTL — same as web sessionStorage).
    // Survives provider-switching within a session but NOT app restart (intentional —
    // restart implies operator may have switched API keys).
    private val modelsCache = mutableMapOf<String, Pair<Long, List<Pair<String, String>>>>()
    private val MODELS_CACHE_TTL_MS = 5 * 60 * 1_000L

    // CU backends map cached alongside modelsCache (same key + lifetime).
    // Caching choice: a parallel map (vs widening modelsCache's value type)
    // keeps the existing 4-provider cache code path byte-identical; the raw
    // JSON isn't retained so the map can't be re-derived on a cache hit.
    private val cuBackendsCache = mutableMapOf<String, Map<String, String>>()

    private fun fetchLiveModels(provider: String) {
        val currentApi = api ?: return
        val apiProvider = mapProviderForApi(provider)
        if (provider != "computer-use") {
            // Leaving CU (or never on it): stale id→backend pairs must not
            // survive into another provider's model list.
            _cuModelBackends.value = emptyMap()
        }
        if (apiProvider == null) {
            // No live models for this provider — clear so Constants fallback is used
            _liveModels.value = emptyList()
            return
        }

        // Cache hit — instant population, no network
        val cached = modelsCache[apiProvider]
        val now = System.currentTimeMillis()
        if (cached != null && now - cached.first < MODELS_CACHE_TTL_MS) {
            _liveModels.value = cached.second
            if (apiProvider == "computer-use") {
                _cuModelBackends.value = cuBackendsCache[apiProvider] ?: emptyMap()
            }
            Log.d(TAG, "Models cache hit for $provider (age ${(now - cached.first) / 1000}s)")
            return
        }

        viewModelScope.launch {
            try {
                val response = currentApi.get("/models/$apiProvider")
                val obj = json.parseToJsonElement(response).jsonObject
                val modelsArr = obj["models"]?.jsonArray ?: return@launch
                val models = modelsArr.mapNotNull { el ->
                    try {
                        val m = el.jsonObject
                        val id = m["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                        val name = m["name"]?.jsonPrimitive?.content ?: id
                        id to name
                    } catch (_: Exception) { null }
                }
                if (models.isNotEmpty()) {
                    if (apiProvider == "computer-use") {
                        // CU: backend partition map + default-aware Auto label
                        // (mirrors Portal buildHydratedModels, state-management.js)
                        val backends = modelsArr.mapNotNull { el ->
                            try {
                                val m = el.jsonObject
                                val id = m["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                                val backend = m["backend"]?.jsonPrimitive?.content ?: return@mapNotNull null
                                id to backend
                            } catch (_: Exception) { null }
                        }.toMap().toMutableMap()
                        val defaultId = obj["default_id"]?.jsonPrimitive?.content
                        val defaultName = models.firstOrNull { it.first == defaultId }?.second
                        // Auto ("") resolves server-side to the default model →
                        // it lives in the default model's backend group.
                        backends[""] = defaultId?.let { backends[it] } ?: "anthropic"
                        val autoLabel = if (defaultName != null) "Auto - $defaultName" else "Auto - Latest"
                        val withAuto = listOf("" to autoLabel) + models
                        // The two writes below have no suspension point between
                        // them and run on the main thread, so composition never
                        // observes the new list paired with a stale backends map
                        // (the writes are atomic w.r.t. composition).
                        _liveModels.value = withAuto
                        _cuModelBackends.value = backends
                        modelsCache[apiProvider] = System.currentTimeMillis() to withAuto
                        cuBackendsCache[apiProvider] = backends
                    } else {
                        // Prepend "Auto" option
                        val withAuto = listOf("" to "Auto - Latest") + models
                        _liveModels.value = withAuto
                        modelsCache[apiProvider] = System.currentTimeMillis() to withAuto
                    }
                    Log.d(TAG, "Fetched ${models.size} live models for $provider (source=${obj["source"]?.jsonPrimitive?.content ?: "?"})")
                }
            } catch (e: Exception) {
                Log.d(TAG, "Model fetch failed for $provider, using defaults: ${e.message}")
                // Keep whatever's in liveModels (could be from Constants fallback)
            }
        }
    }

    // =========================================================================
    // Computer Use — public API
    // =========================================================================

    fun setCuDeviceId(deviceId: String) {
        _cuDeviceId.value = deviceId
    }

    fun setCuSessionId(sessionId: String?) {
        _cuSessionId.value = sessionId
    }

    /** Reset CU state for a new session */
    fun resetCuSession() {
        _cuSessionId.value = null
        _cuStep.value = 0
        _cuStepTotal.value = 0
        _cuStatus.value = "idle"
        _cuActionLabel.value = ""
        _cuScreenshotUrl.value = null
    }

    // =========================================================================
    // Robotics ER — public API
    // =========================================================================

    fun setErCamera(camera: String) { _erCamera.value = camera }

    fun resetErState() {
        _erCameraFrame.value = null
        _erReasoning.value = ""
        _erStatus.value = "offline"
    }

    /** Request e-stop on the current CU session */
    fun stopCuTask() {
        val sessionId = _cuSessionId.value ?: return
        viewModelScope.launch {
            try {
                api?.post("/chat/cu-stop", buildCuStopBody(sessionId))
                _cuStatus.value = "stopped"
            } catch (e: Exception) {
                Log.e(TAG, "CU stop failed: ${e.message}")
            }
        }
    }

    private fun buildCuStopBody(sessionId: String): String {
        return kotlinx.serialization.json.buildJsonObject {
            put("session_id", sessionId)
            put("model", currentModel)
        }.toString()
    }

    companion object {
        /**
         * The on-device placeholder copy shown until the Phase 2 engine lands.
         * Centralised so the UI test can assert it.
         */
        const val LOCAL_PLACEHOLDER_TEXT =
            "On-device model selected — the on-device engine wiring lands in the next update."

        /**
         * Select the delivery path for [provider]. Pure (no ViewModel state) so
         * the routing decision is unit-testable. Mirrors the original when-block:
         *   agent → AGENT, voice → VOICE, robotics+mission → ER_INJECT,
         *   local → LOCAL_PLACEHOLDER, everything else → SSE.
         *
         * Order matters: LOCAL is checked before the SSE default precisely
         * because LOCAL is NOT a streaming provider — the SSE fall-through would
         * otherwise POST provider=local to the cloud and error.
         */
        fun routeFor(provider: ChatProvider, erMissionActive: Boolean): ChatRoute = when {
            provider.isAgent -> ChatRoute.AGENT
            provider.isVoice -> ChatRoute.VOICE
            provider.isRobotics && erMissionActive -> ChatRoute.ER_INJECT
            provider.isLocal -> ChatRoute.LOCAL_PLACEHOLDER
            else -> ChatRoute.SSE
        }

        /**
         * The pure double-send predicate both the SSE and LOCAL_PLACEHOLDER arms
         * of [sendMessage] consult BEFORE routing: a send is dropped while a turn
         * is already streaming. Extracted so the guard (hoisted in review I2 to
         * cover the placeholder path too) is unit-testable without the ViewModel.
         */
        fun shouldBlockSend(state: ChatState): Boolean = state == ChatState.STREAMING

        /**
         * The terminal [ChatState] a finished local turn leaves behind, given the
         * [streamLocalTurn] outcome and the state observed when it returned. Pure
         * so the M1/M2 "fault → ERROR, success → IDLE" mapping is unit-testable
         * (the instance method applies it after the collect):
         *  - faulted → [ChatState.ERROR] (parity with sendViaSSE's catch).
         *  - ok and still [STREAMING] → [ChatState.IDLE].
         *  - ok but already moved off STREAMING → keep it (don't clobber a
         *    terminal state the stream set).
         */
        fun stateAfterLocalTurn(faulted: Boolean, current: ChatState): ChatState = when {
            faulted -> ChatState.ERROR
            current == ChatState.STREAMING -> ChatState.IDLE
            else -> current
        }

        /** The non-streaming assistant placeholder message for the LOCAL branch. */
        fun buildLocalPlaceholder(provider: String, model: String): UiMessage = UiMessage(
            role = "assistant",
            content = LOCAL_PLACEHOLDER_TEXT,
            isStreaming = false,
            provider = provider,
            model = model,
        )

        /** Provider tag for on-device turns (logging / save traceability). */
        const val LOCAL_PROVIDER_ID = "local"

        /**
         * Friendly text appended to the (possibly partial) reply when the on-device
         * engine faults mid-generation. Mirrors the SSE error path: surface a
         * non-crashing, human message rather than a stack trace.
         */
        const val LOCAL_ENGINE_ERROR_TEXT =
            "\n\n[on-device error — the local model could not finish this reply]"

        /**
         * Max length of the one-line result snippet shown after a successful tool
         * outcome ([renderToolOutcome]) — keep tool activity inline-readable; never
         * dump a large JSON blob into the chat bubble.
         */
        const val TOOL_RESULT_SNIPPET_MAX = 80

        /**
         * Map the current UI conversation to [FcLoop.Turn]s for prompt assembly.
         *
         * Excludes anything that is NOT a settled user/assistant turn:
         *  - the in-flight streaming assistant placeholder (still being filled);
         *  - any empty-content turns (e.g. the freshly-appended placeholder).
         *
         * The just-appended user message is also dropped here because the caller
         * passes it separately as the turn's `text`; it is the LAST user message
         * and is excluded by trimming the final user turn. Roles other than
         * "user"/"assistant" (system/tool placeholders) are ignored.
         */
        fun toFcHistory(messages: List<UiMessage>): List<FcLoop.Turn> {
            // Drop the in-flight assistant placeholder (last, streaming/empty) and
            // the just-appended user message (the current turn's text) from the
            // tail before mapping.
            var end = messages.size
            // 1. Trailing in-flight assistant placeholder.
            if (end > 0) {
                val last = messages[end - 1]
                if (last.role == "assistant" && (last.isStreaming || last.content.isEmpty())) {
                    end--
                }
            }
            // 2. The current turn's user message (now the trailing entry).
            if (end > 0 && messages[end - 1].role == "user") {
                end--
            }
            return messages.subList(0, end).mapNotNull { m ->
                when (m.role) {
                    "user" -> FcLoop.Turn(FcLoop.Role.USER, m.content)
                    "assistant" -> if (m.content.isEmpty() || m.isStreaming) null
                        else FcLoop.Turn(FcLoop.Role.ASSISTANT, m.content)
                    else -> null
                }
            }
        }

        /**
         * PURE core of [sendViaLocalEngine]: run one on-device turn and stream its
         * deltas into [sink], then persist via [saveSink]. Extracted (like
         * [routeFor] / [buildSaveRequest]) so it is unit-testable without the
         * AndroidViewModel — production wires the real `updateLastMessage` /
         * `saveConversation` into [sink] / [saveSink].
         *
         * Contract:
         *  - Collects [FcLoop.runTurn]'s delta Flow, accumulating into a running
         *    buffer and calling `sink(runningText, isStreaming=true)` per delta —
         *    EXACTLY the SSE token path.
         *  - On normal completion: `sink(fullText, isStreaming=false)` then
         *    `saveSink(SaveRequest, provider="local")`; returns `true`.
         *  - On a mid-stream throw (runTurn's Flow can fault): caught via `.catch`,
         *    appends [LOCAL_ENGINE_ERROR_TEXT] to whatever streamed so far,
         *    `sink(partial+error, isStreaming=false)`, DOES NOT save (mirrors the
         *    SSE error path), and returns `false`. Never rethrows — the turn does
         *    not crash; the caller maps the `false` to [ChatState.ERROR].
         *  - Never reaches SSE.
         *
         * Threading: `.flowOn(Dispatchers.IO)` moves GENERATION (the upstream
         * [FcLoop.runTurn] Flow) onto IO, while `.collect` / [sink] run on the
         * collector's dispatcher (the VM's Main in production) — mirroring
         * [sendViaSSE], which collects on viewModelScope and never wraps the sink
         * in withContext(IO). flowOn is a no-op under the serial test dispatcher,
         * so the unit tests below stay deterministic.
         *
         * @return `true` if the turn completed and was saved, `false` if it faulted.
         */
        suspend fun streamLocalTurn(
            fcLoop: FcLoop,
            persona: String,
            history: List<FcLoop.Turn>,
            text: String,
            operator: String,
            model: String?,
            sink: (content: String, isStreaming: Boolean) -> Unit,
            saveSink: (request: SaveRequest, provider: String) -> Unit,
        ): Boolean {
            val acc = StringBuilder()
            var faulted = false
            fcLoop.runTurn(persona, history, text)
                // Generation runs on IO; the collector/sink stay on the caller's
                // dispatcher (Main in production) — parity with sendViaSSE.
                .flowOn(Dispatchers.IO)
                .catch { e ->
                    faulted = true
                    acc.append(LOCAL_ENGINE_ERROR_TEXT)
                    sink(acc.toString(), false)
                }
                .collect { delta ->
                    acc.append(delta)
                    sink(acc.toString(), true)
                }
            if (faulted) return false
            // Normal completion: finalize the stream + persist the turn.
            val full = acc.toString()
            sink(full, false)
            val request = buildSaveRequest(
                operator = operator,
                userMessage = text,
                assistantResponse = full,
                reasoning = "",
                model = model,
                tokens = null,
                provenance = null,
            )
            saveSink(request, LOCAL_PROVIDER_ID)
            return true
        }

        /**
         * PURE core of the TOOL-AWARE on-device turn — the [streamLocalTurn] sibling
         * for a model that can call BlackBox tools. Collects [FcLoop.runAgent]'s
         * [LlmEvent] Flow (instead of [FcLoop.runTurn]'s plain text Flow) and renders
         * each event INLINE into the SAME streaming assistant bubble the text streams
         * into, then persists via [saveSink]. Extracted (like [streamLocalTurn]) so it
         * is unit-testable without the AndroidViewModel.
         *
         * RENDERING PARITY (deliberate design choice): the Android MVP has NO typed
         * tool-call/tool-result UI message types — [UiMessage] carries none, and the
         * ONLY existing tool-rendering convention is the cloud ER `er_action` SSE path
         * (`content.append("\n`[$tool]` $args → $status ")`). So this turn adopts that
         * SAME inline-markdown convention via [renderToolCall] / [renderToolOutcome]:
         * tool name in backticks, the args, an arrow, a status — appended to the
         * streaming assistant `content`, so tool activity shows in the same bubble.
         * Each rendered line is SELF-CONTAINED + name-labeled because [FcLoop.runAgent]
         * emits ALL of a turn's [LlmEvent.ToolCall]s BEFORE any [LlmEvent.ToolOutcome]
         * (a call is NOT immediately followed by its outcome on a multi-call turn).
         *
         * Contract (mirrors [streamLocalTurn] exactly):
         *  - TextDelta → append text; ToolCall → append [renderToolCall];
         *    ToolOutcome → append [renderToolOutcome]; `sink(runningText, true)` per
         *    event.
         *  - MULTI-CALL ORDERING: on a turn with several tool calls [FcLoop.runAgent]
         *    emits ALL its [LlmEvent.ToolCall]s first, THEN all [LlmEvent.ToolOutcome]s,
         *    so the inline lines render as the calls batched, then the outcomes batched
         *    (not interleaved call→outcome); the `[name]` labels disambiguate which
         *    outcome belongs to which call.
         *  - On normal completion: `sink(fullText, false)` then
         *    `saveSink(SaveRequest, provider="local")`; returns `true`.
         *  - A TOOL-LEVEL failure (a [ToolResult] with `success=false`) is NOT a
         *    stream fault: it renders "failed" and the turn still completes + saves.
         *  - A mid-stream THROW propagating out of runAgent is caught via `.catch`,
         *    appends [LOCAL_ENGINE_ERROR_TEXT], `sink(partial+error, false)`, DOES
         *    NOT save, returns `false`. Note (Task 3.4 landed): an OFFLINE tool
         *    failure no longer reaches here — the bridge degrades to a
         *    `success=false` [ToolResult] (rendered "failed") so the turn continues;
         *    only a genuine non-IO fault (e.g. a SerializationException, or an
         *    engine fault) still surfaces as the local-engine error.
         *
         * Threading mirrors [streamLocalTurn]: `.flowOn(Dispatchers.IO)` moves the
         * agent loop onto IO; `.collect` / [sink] stay on the caller's dispatcher.
         *
         * @return `true` if the turn completed and was saved, `false` if it faulted.
         */
        suspend fun streamLocalAgentTurn(
            fcLoop: FcLoop,
            persona: String,
            history: List<FcLoop.Turn>,
            text: String,
            operator: String,
            model: String?,
            sink: (content: String, isStreaming: Boolean) -> Unit,
            saveSink: (request: SaveRequest, provider: String) -> Unit,
        ): Boolean {
            val acc = StringBuilder()
            var faulted = false
            fcLoop.runAgent(persona, history, text)
                .flowOn(Dispatchers.IO)
                .catch { e ->
                    faulted = true
                    acc.append(LOCAL_ENGINE_ERROR_TEXT)
                    sink(acc.toString(), false)
                }
                .collect { event ->
                    when (event) {
                        is LlmEvent.TextDelta -> acc.append(event.text)
                        is LlmEvent.ToolCall -> acc.append(renderToolCall(event.name, event.args))
                        is LlmEvent.ToolOutcome -> acc.append(renderToolOutcome(event.name, event.result))
                    }
                    sink(acc.toString(), true)
                }
            if (faulted) return false
            val full = acc.toString()
            sink(full, false)
            val request = buildSaveRequest(
                operator = operator,
                userMessage = text,
                assistantResponse = full,
                reasoning = "",
                model = model,
                tokens = null,
                provenance = null,
            )
            saveSink(request, LOCAL_PROVIDER_ID)
            return true
        }

        /**
         * Collapse to a single line and bound length so a large model-supplied arg or
         * tool result can't flood the chat bubble or the saved snapshot. Strips BOTH
         * \n and \r (a CR-laden tool result must render on one line) and truncates
         * with an ellipsis past [max] (default [TOOL_RESULT_SNIPPET_MAX]).
         */
        private fun inlineCap(s: String, max: Int = TOOL_RESULT_SNIPPET_MAX): String {
            val oneLine = s.replace('\n', ' ').replace('\r', ' ')
            return if (oneLine.length > max) oneLine.take(max) + "…" else oneLine
        }

        /**
         * Inline-markdown for an on-device TOOL CALL. Parity format with the cloud ER
         * `er_action` path: a backtick-wrapped, name-labeled line on its own row
         * carrying the (capped) args. `args.toString()` is compact JSON; it is routed
         * through [inlineCap] so a model inlining a large blob (e.g. a base64 image) as
         * an arg can't flood the bubble or snapshot — the SAME cap a large tool RESULT
         * gets in [renderToolOutcome]. Self-contained so it reads correctly even when a
         * turn batches several calls before their outcomes.
         */
        @VisibleForTesting
        internal fun renderToolCall(name: String, args: JsonObject): String =
            "\n`[$name]` ${inlineCap(args.toString())}"

        /**
         * Inline-markdown for an on-device TOOL OUTCOME. Parity format with the cloud
         * ER `er_action` path: a backtick-wrapped, name-labeled line, an arrow, then a
         * one-word status (a tool-level failure renders "failed", NOT a stream fault).
         * On success a SHORT one-line result snippet is appended (prefer the unquoted
         * string content for the common string case); large JSON is NOT dumped.
         */
        @VisibleForTesting
        internal fun renderToolOutcome(name: String, result: ToolResult): String {
            if (!result.success) return "\n`[$name]` → failed"
            val snippet = (result.result as? JsonPrimitive)?.contentOrNull
                ?: result.result?.toString()
            val shown = snippet?.let { inlineCap(it) }
            return if (shown.isNullOrBlank()) "\n`[$name]` → done" else "\n`[$name]` → done · $shown"
        }

        private val provJson = Json { ignoreUnknownKeys = true; isLenient = true }
        fun parseProvenance(raw: String): Provenance? = try {
            provJson.decodeFromString(Provenance.serializer(), raw.trim())
        } catch (_: Exception) { null }

        /**
         * Extract the provenance object from inside an SSE stream_start event's
         * data payload. Backend (chat_routes.py:5953/6035) emits provenance as a
         * field of stream_start, NOT as a standalone "provenance" SSE event —
         * matching Portal/modules/chat-send.js:1117-1122. Returns null if the
         * stream_start has no provenance field or fails to parse.
         */
        fun extractProvenanceFromStreamStart(streamStartData: String): Provenance? = try {
            val obj = provJson.parseToJsonElement(streamStartData).jsonObject
            obj["provenance"]?.let {
                provJson.decodeFromJsonElement(Provenance.serializer(), it)
            }
        } catch (_: Exception) { null }

        /**
         * Builds the SaveRequest sent to /chat/save. Extracted from
         * saveConversation() so unit tests can verify provenance threads
         * through without instantiating an AndroidViewModel.
         *
         * Behaviour preserved verbatim from the inline construction:
         *   - reasoning is normalized to null when blank
         *   - model passes through as-is (caller substitutes currentModel)
         *   - provenance is passed through, may be null
         */
        @VisibleForTesting
        internal fun buildSaveRequest(
            operator: String,
            userMessage: String,
            assistantResponse: String,
            reasoning: String,
            model: String?,
            tokens: TokenCount?,
            provenance: Provenance?
        ): SaveRequest = SaveRequest(
            operator = operator,
            userMessage = userMessage,
            assistantResponse = assistantResponse,
            reasoning = reasoning.ifBlank { null },
            model = model,
            tokens = tokens,
            provenance = provenance,
        )
    }
}
