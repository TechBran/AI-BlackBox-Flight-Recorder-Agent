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
import com.aiblackbox.portal.LocalModelService
import com.aiblackbox.portal.data.local.LiteRtEngine
import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.EngineSource
import com.aiblackbox.portal.data.local.LocalEngineHolder
import com.aiblackbox.portal.data.local.engineSourceFor
import com.aiblackbox.portal.data.local.LocalLlm
import com.aiblackbox.portal.data.local.LocalModelManager
import com.aiblackbox.portal.data.local.LocalSnapshotQueue
import com.aiblackbox.portal.data.local.NativeTool
import com.aiblackbox.portal.data.local.NativeToolCallingLlm
import com.aiblackbox.portal.data.local.PersonaCache
import com.aiblackbox.portal.data.local.PhoneController
import com.aiblackbox.portal.data.local.ResidentTools
import com.aiblackbox.portal.data.local.SamplerSettings
import com.aiblackbox.portal.data.local.ToolBridge
import com.aiblackbox.portal.data.local.ToolBridgeClient
import com.aiblackbox.portal.data.local.ToolCallingLlm
import com.aiblackbox.portal.data.local.TurnClient
import com.aiblackbox.portal.data.local.VisionLlm
import com.aiblackbox.portal.data.local.formatCloudToolMatches
import com.aiblackbox.portal.data.local.toResultJsonString
import com.aiblackbox.portal.overlay.AndroidPhoneController
import com.aiblackbox.portal.overlay.OverlayConfirmUi
import com.aiblackbox.portal.overlay.OverlayCredentialHandoff
import com.aiblackbox.portal.data.model.ArtifactRef
import com.aiblackbox.portal.data.model.ChatMessage
import com.aiblackbox.portal.data.model.ChatProvider
import com.aiblackbox.portal.data.model.CompleteRequest
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.model.TokenCount
import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.data.repository.ChatRepository
import com.aiblackbox.portal.data.repository.TaskRepository
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.ChatHistoryStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.runBlocking
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
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
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

/**
 * Readiness of the on-device (`local`) engine, surfaced to the UI so the provider
 * pill can show a "loading…/ready" affordance instead of the user discovering the
 * ~10-75s cold model load only on their first send (Task W1 — warm-while-app-open).
 *
 * - [IDLE]: no local engine warm has started (no model installed, or the local
 *   provider is not the active one — we don't load a multi-GB model for a
 *   cloud-only session).
 * - [WARMING]: a preload is in flight ([ChatViewModel.preloadLocalEngine] is
 *   running `engine.load(...)` off the main thread).
 * - [READY]: the engine is loaded and the first send will be instant.
 * - [ERROR]: the warm failed (the lazy fallback in [ChatViewModel.runLocalEngineTurn]
 *   will retry on the next send — load() is idempotent — so this is informational,
 *   not terminal).
 *
 * The transition logic lives in the pure [ChatViewModel.localEngineStateAfter] /
 * [ChatViewModel.shouldStartWarm] so it is unit-testable without the ViewModel.
 */
enum class LocalEngineState { IDLE, WARMING, READY, ERROR }

/**
 * The events that drive the [LocalEngineState] machine (see
 * [ChatViewModel.localEngineStateAfter]). Pure data so the transition table is
 * exercised in a plain JVM unit test.
 */
enum class LocalEngineEvent { WARM_STARTED, WARM_SUCCEEDED, WARM_FAILED }

/**
 * What to do when the active on-device model SELECTION changes (Task W5):
 *  - [NONE]   no-op (unchanged, not local, or the first replayed emission).
 *  - [NOW]    invalidate the cached engine + re-warm the new bundle immediately.
 *  - [DEFER]  a turn is in flight -> apply at turn completion (I1: never close()
 *             the native engine mid-generation).
 * Decided by the pure [ChatViewModel.localReWarmAction].
 */
enum class LocalReWarmAction { NONE, NOW, DEFER }

private const val MAX_CHAT_MESSAGES = 100

/**
 * Char budget for the rolling conversation history carried into the ON-DEVICE
 * model's prompt (SL-2). The on-device session is now STATEFUL: recent turns are
 * carried so the model remembers the conversation â but bounded, because the
 * GPU context window is only ~6144 tokens.
 *
 * [budgetHistory] keeps the NEWEST turns and drops the OLDEST first until the sum
 * of carried turn-text chars is under this budget (turns are atomic â never split).
 * "Start lean, build context up; a buffer that drops the oldest entries first."
 *
 * 4000 chars ≈ ~1.6K tokens at the real on-device density (~2.5 chars/token,
 * not 4); device-tuned to fit the 6144 window with headroom for the
 * persona, any injected tool schemas, the current user message, the model's
 * output, AND the intra-turn tool-result growth the native loop adds (see
 * [com.aiblackbox.portal.data.local.MAX_TURN_TOOL_RESULT_CHARS]). Replaces the
 * earlier stateless count cap (which carried NO history to dodge a 4096-token
 * overrun: `Input token ids are too long: 4292 >= 4096`); the char budget bounds
 * the prompt regardless of session length while restoring recall. Device-tunable.
 * Only the on-device path uses this; the cloud SSE path is unaffected (the server
 * owns its own context budget).
 */
private const val LOCAL_HISTORY_BUDGET_CHARS = 4000

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

    // Warm-loop guard: a disk-persisted "warm in-flight" flag set BEFORE every
    // on-device engine warm and cleared on its success / graceful failure. A
    // SIGKILL mid-warm (device OOM) runs no cleanup, so the flag survives still-set
    // into the next process start -- the AUTO-warm in [preloadLocalEngine] reads it
    // and SKIPS, turning a crash/restart/auto-warm loop into a single failure
    // (a deliberate send is the manual retry). See [WarmInflightStore].
    private val warmInflightStore by lazy {
        com.aiblackbox.portal.data.local.WarmInflightStore.fromContext(appContext)
    }

    // Auto-warm-on-open SETTING (user preference, persisted): when DISABLED the
    // [preloadLocalEngine] auto path skips the warm and the model loads lazily on the
    // first send. Default TRUE (instant first send). Distinct from the warm-loop
    // GUARD above, which is an automatic OOM-crash safety, not a user choice.
    // See [LocalWarmPrefs].
    private val localWarmPrefs by lazy {
        com.aiblackbox.portal.data.local.LocalWarmPrefs.fromContext(appContext)
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
    private var localEngineDelegate: String = "gpu"

    // R2-C: true when [localEngine] is the PROCESS-held warm engine borrowed
    // from [LocalEngineHolder] (owned by [LocalModelService]), false when it is a
    // VM-built fallback this ViewModel owns. onCleared / invalidateLocalEngine
    // must close ONLY a VM-built engine -- closing the service-owned warm engine
    // would break the next turn / the model-as-a-tool path. Defaults false
    // (the pre-R2-C fallback owns its engine).
    private var localEngineFromHolder: Boolean = false

    // The slug of the active on-device model the user picked in the Model Manager
    // (Task W5), persisted under "model_local". [localProviderOrWire] prefers the
    // INSTALLED bundle whose slug matches this, so picking a different installed
    // model among several actually changes which one is warmed/used. Null/blank ->
    // fall back to the alphabetically-first installed bundle (the prior behavior).
    @Volatile
    private var currentLocalModelSlug: String = ""

    // I1 (W5 review): a model-selection change that arrives WHILE a local turn is
    // streaming must NOT close the live native engine mid-generation (close() is
    // not serialized against generate() -> native teardown race). When that
    // happens we set this flag and defer the invalidate+re-warm until the turn
    // completes ([processPendingLocalReWarm]).
    @Volatile
    private var pendingLocalReWarm: Boolean = false

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

    // The server-bracketed on-device turn client (Task 10) — POST /local/turn/prepare
    // (assemble the per-turn package) + POST /local/turn/complete (mint the finished
    // turn). Built lazily off this VM's api (mirroring toolBridge/toolBridgeOrBuild);
    // cached. Settable so a test can wire a fake. NULL until the api is set, in which
    // case the native path falls back to the local persona-cache turn (offline mode).
    @VisibleForTesting
    var turnClient: TurnClient? = null

    // ── On-device screen-capture seam (Task W4.2 — vision) ──
    // The single-frame screen capture used by the direct "look at my screen" path
    // ([lookAtScreen]). Production reuses the running overlay's MediaProjection +
    // the live accessibility tree's password gate ([OverlayScreenCapture]); built
    // lazily on first use. Settable so a test can inject a fake (no Service / no
    // projection needed). Captured frames are EPHEMERAL — never persisted.
    @VisibleForTesting
    var screenCapture: com.aiblackbox.portal.overlay.ScreenCapture? = null

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

    // ── On-device (local) engine readiness (Task W1) ──
    // Mirrors the warm-while-app-open preload so the provider pill can show
    // "loading…/ready". IDLE until a warm starts (we only warm when the local
    // provider is the active one — see [preloadLocalEngine]); the lazy load in
    // [runLocalEngineTurn] remains the fallback if the user sends before READY.
    private val _localEngineState = MutableStateFlow(LocalEngineState.IDLE)
    val localEngineState: StateFlow<LocalEngineState> = _localEngineState.asStateFlow()

    // True while a [preloadLocalEngine] warm is in flight, so a duplicate trigger
    // (e.g. re-selecting the local provider) does not launch a second concurrent
    // warm. Paired with the model path so a DIFFERENT model can still re-warm.
    @Volatile
    private var warmingModelPath: String? = null

    // ── CU model backends (id → "anthropic" | "google" | "openai") ──
    // Populated only when fetching /models/computer-use (CU production pass
    // 2026-06: the CU catalog entries carry a `backend` field). Includes the
    // "" (Auto) key mapped to the server default's backend. Empty for every
    // other provider — CuScreen falls back to its id-substring heuristic.
    private val _cuModelBackends = MutableStateFlow<Map<String, String>>(emptyMap())
    val cuModelBackends: StateFlow<Map<String, String>> = _cuModelBackends.asStateFlow()

    // ── Custom model load status (id → "loaded" | "unloaded") ──
    // Populated only when fetching /models/custom (Task 7.1: the custom catalog
    // entries carry an additive `status` field — the registry's last probe of
    // whether the server has that model warm in RAM). Parsed tolerantly: an
    // absent/null status simply isn't in the map. Cleared on leaving custom AND
    // whenever the custom catalog comes back empty/failed — mirrors the
    // _cuModelBackends multi-origin pattern above, minus the cache (the custom
    // roster/status is never cached; see [fetchLiveModels]).
    private val _customModelStatus = MutableStateFlow<Map<String, String>>(emptyMap())
    val customModelStatus: StateFlow<Map<String, String>> = _customModelStatus.asStateFlow()

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
                    // Task W1: warm-while-app-open. The moment the local provider
                    // becomes (or is restored as) the active one, proactively load
                    // the on-device model so the first send is instant. No-op for
                    // every other provider, and idempotent if already WARMING/READY
                    // (see [preloadLocalEngine]). This also covers app open, because
                    // the persisted provider replays through this collector on init.
                    preloadLocalEngine()
                }
            }
            launch { store.model.collect { currentModel = it } }
            // Task W5: track the active on-device model selection. When the user
            // picks a different installed model via the Model Manager ("Use"), the
            // chosen slug is persisted under "model_local"; honor it so
            // [localProviderOrWire] warms/uses THAT model, and re-warm if the
            // selection changes while the local provider is the active one.
            launch {
                store.getString("model_local").collect { slug ->
                    val previous = currentLocalModelSlug
                    currentLocalModelSlug = slug
                    // Thin shim over the PURE [localReWarmAction] decision (M1
                    // first-emission no-op + I1 mid-stream defer), so the branching
                    // is unit-tested without the ViewModel.
                    when (
                        localReWarmAction(
                            previousSlug = previous,
                            newSlug = slug,
                            isLocalProvider = ChatProvider.fromId(currentProvider).isLocal,
                            turnInFlight = isLocalTurnInFlight(),
                        )
                    ) {
                        LocalReWarmAction.NOW -> {
                            invalidateLocalEngine()
                            preloadLocalEngine()
                        }
                        // I1: tearing down now would close() the native runtime
                        // mid-generation -> apply at turn completion instead.
                        LocalReWarmAction.DEFER -> pendingLocalReWarm = true
                        LocalReWarmAction.NONE -> Unit
                    }
                }
            }
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
                                status.status.equals("failed", true)
                                    || status.status.equals("cancelled", true) -> {
                                    // 'cancelled' is terminal like failed (G2-T8):
                                    // stop polling and drop the placeholder, or a
                                    // cancelled media task polls for the full 25 min.
                                    removeMediaTaskFromMessage(taskId)
                                    Log.w(TAG, "Media task $taskId ${status.status} — removed placeholder")
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

            // Task W1: app-open warm. If the persisted/active provider is already
            // LOCAL, the provider collector may have fired in init BEFORE the api
            // was ready (so localProviderOrWire() returned null → IDLE). Now the api
            // is wired, kick the warm again; it no-ops for non-local providers and
            // is idempotent when already WARMING/READY.
            preloadLocalEngine()
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
        //
        // R2-C: close ONLY a VM-OWNED engine. When [localEngineFromHolder] the
        // engine is the PROCESS-held warm engine owned by [LocalModelService];
        // closing it here would tear down the native runtime the service is
        // keeping resident (and break the next turn / the model-as-a-tool path).
        // The service releases it on its own stop/destroy via the holder.
        if (!localEngineFromHolder) {
            runCatching { localEngine?.close() }
        }
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
                                resultUrl = t["result_url"]?.jsonPrimitive?.content,
                                // G3-T13 (M3.3): this MANUAL builder is the primary feed for
                                // TaskPanel — it overwrites the auto-parsed poll object every
                                // ~2s, so the new pill fields MUST be threaded here or they
                                // never surface. /tasks/list carries both top-level.
                                progressText = t["progress_text"]?.jsonPrimitive?.contentOrNull,
                                deviceId = t["device_id"]?.jsonPrimitive?.contentOrNull
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
                        task.status.equals("failed", true) ||
                        task.status.equals("cancelled", true)   // terminal — show briefly (G2-T8)
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
     * Retry a FAILED user turn (the retry chip under a `sendFailed` user bubble).
     *
     * REPLACES the failed turn — never duplicates it and never double-mints:
     *  1. the pure [retryRemoval] drops the error assistant bubble that followed
     *     the failed user turn AND the failed user message itself;
     *  2. the turn is re-fired through the SAME route sendMessage uses, with the
     *     SAME text + image URLs (both retained on the original UiMessage), which
     *     re-appends a fresh user turn exactly once (sendFailed defaults false).
     * saveConversation/mint only runs on stream SUCCESS, so replace-not-append
     * guarantees at most one mint for the logical turn.
     *
     * Guarded by [shouldBlockRetry] — STREAMING *and* THINKING block (the same
     * in-flight pair ChatScreen's send affordance uses): a stale chip tapped
     * while a newer turn is still THINKING must not fire a concurrent stream
     * (both would race updateLastMessage and could double-mint). ERROR does not
     * block — a failed turn is exactly the state retry exists for.
     */
    fun retryMessage(messageId: String) {
        if (shouldBlockRetry(_chatState.value)) return
        val (remaining, userMsg) = retryRemoval(_messages.value, messageId)
        if (userMsg == null) return
        _messages.value = remaining
        fireSend(userMsg.content, userMsg.images)
    }

    /**
     * Route an EXPLICIT text through the same branch logic as [sendMessage]
     * without reading (or clearing) `_inputText` — the retry path. Mirrors
     * sendMessage's when-block exactly; the SSE arm passes clearInput=false so a
     * draft typed after the failure survives the retry.
     */
    private fun fireSend(text: String, imageUrls: List<String>) {
        if (text.isBlank()) return
        val repo = repository ?: run {
            Log.e(TAG, "Repository not initialized")
            return
        }
        when (routeFor(ChatProvider.fromId(currentProvider), _erMissionActive.value)) {
            ChatRoute.AGENT -> _agentPromptEvent.tryEmit(text)
            ChatRoute.VOICE -> Log.w(TAG, "Voice provider $currentProvider — use Voice screen")
            ChatRoute.ER_INJECT -> injectErPrompt(text)
            ChatRoute.LOCAL_PLACEHOLDER -> sendViaLocalEngine(text, clearInput = false)
            ChatRoute.SSE -> sendViaSSE(text, imageUrls, repo, clearInput = false)
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
    private fun sendViaLocalEngine(text: String, clearInput: Boolean = true) {
        // VISION TRIGGER (W4 follow-up, v1): if the user is asking the model to LOOK
        // AT THE SCREEN, route to the direct multimodal vision path instead of the
        // normal text/agent turn. Conservative so it never hijacks a normal message
        // (see [isLookAtScreenRequest]). lookAtScreen owns its own streamJob
        // cancel + launch, so we return here without touching streamJob ourselves.
        if (isLookAtScreenRequest(text)) {
            lookAtScreen(text)
            return
        }
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
                sendViaLocalPlaceholder(text, clearInput)
                return@launch
            }
            runLocalEngineTurn(text, provider, clearInput)
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
    private suspend fun runLocalEngineTurn(text: String, provider: () -> LocalLlm, clearInput: Boolean = true) {
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
        // clearInput=false on the retry path — don't wipe a draft typed since the failure.
        if (clearInput) _inputText.value = TextFieldValue()

        // 2. History for the prompt (SL-2). The on-device session is STATEFUL:
        //    carry recent turns so the model remembers the conversation, but
        //    SIZE-BOUNDED â [budgetHistory] keeps the NEWEST turns and drops the
        //    OLDEST first until under [LOCAL_HISTORY_BUDGET_CHARS], so the per-turn
        //    prompt stays under the ~6144-token window regardless of session length
        //    (the accumulated transcript previously overran it). The full visible
        //    conversation still lives in _messages (UI) and is minted to the ledger.
        val history = budgetHistory(toFcHistory(_messages.value), LOCAL_HISTORY_BUDGET_CHARS)

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
                    // Warm-loop guard: a SEND is the deliberate (manual-retry) warm, so
                    // it is NOT blocked by the in-flight flag — but it still set/clears
                    // it around load(): if THIS load OOM-kills the process, the next
                    // launch's auto-warm sees the still-set flag and skips (no loop).
                    warmInflightStore.setInflight(true)
                    try {
                        withContext(Dispatchers.IO) {
                            engineToLoad.load(modelFile, localEngineDelegate)
                        }
                    } finally {
                        // Cleared on success AND on a (caught) load throw — both are
                        // graceful (the catch below surfaces the friendly error). Only
                        // an actual process kill leaves it set.
                        warmInflightStore.setInflight(false)
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

                // Capability-detect on the SINGLE provider() instance, picking the
                // best path the engine supports (W3):
                //   1. NATIVE (engine-driven) agent loop  - provider is
                //      NativeToolCallingLlm: the litertlm engine runs the tool loop
                //      itself (automaticToolCalling = true) and terminates cleanly
                //      (onDone), fixing the manual-loop repeat with the small E4B
                //      model. ONE native loop offers BOTH the resident phone/intent
                //      tools (-> PhoneController) AND the cloud vault via
                //      find_blackbox_tool / run_blackbox_tool (-> bridge); W3 follow-up.
                //   2. MANUAL agent loop - provider is ToolCallingLlm + a bridge:
                //      FcLoop drives the tiered two-hop loop (search + cloud + phone).
                //      Kept intact + selectable so nothing regresses (the test fakes
                //      land here).
                //   3. TEXT only - a plain LocalLlm (e.g. a text-only FakeLocalLlm).
                val llm = provider()
                val bridge = toolBridgeOrBuild()
                // The on-device phone controller, wired ONCE and shared by whichever
                // tool path runs. It reads the LIVE accessibility service via the
                // singleton seam; if the service isn't enabled the actuators degrade
                // gracefully ("not enabled") and read_screen returns "[]", so it is
                // safe to always build. The autonomy gate (Phase 4.6) + credential
                // handoff (Phase 4.7) live INSIDE the actuator (downstream of
                // dispatch), so they fire on BOTH the native and the manual path.
                val phoneController = AndroidPhoneController.fromService(
                    appContext,
                    mode = { autonomyStore.load() },
                    confirm = OverlayConfirmUi(appContext),
                    credentialHandoff = OverlayCredentialHandoff(appContext),
                )
                val ok = if (llm is NativeToolCallingLlm) {
                    // NATIVE path: ONE engine-driven loop handles BOTH phone/intent
                    // actions (dispatched LOCALLY through the controller, NEVER the cloud
                    // bridge) AND cloud capabilities (find_blackbox_tool / run_blackbox_tool,
                    // dispatched through the bridge ONLY). The two are SEPARATE NativeTool
                    // execute lambdas, so a phone tool structurally cannot reach the bridge
                    // (the W3 separation guarantee).
                    //
                    // SERVER-BRACKETED turn (Task 10): ask the hub to assemble this turn's
                    // package (POST /local/turn/prepare). ONLINE -> the model runs on the
                    // server-assembled system_prompt (fresh per-operator memory) + the
                    // server's top-K relevant tools as DIRECT native calls, and the turn is
                    // MINTED back via /local/turn/complete. OFFLINE (prepare == null,
                    // unreachable) -> the EXISTING local persona-cache turn + persistLocalSave
                    // queue (Task 11 formalizes degraded mode). The two paths are mutually
                    // exclusive on the save sink, so a turn is minted EXACTLY ONCE (online via
                    // complete, offline via persistLocalSave) -- NEVER both.
                    val tc = turnClientOrBuild()
                    val prep = if (tc != null) withContext(Dispatchers.IO) { tc.prepare(text, op) } else null
                    if (prep != null) {
                        // ONLINE: server system_prompt + injected direct tools; mint via complete.
                        // tc is non-null here (prep came from tc.prepare); capture it as a
                        // non-null val so the completeSink closure can call complete without !!.
                        val onlineClient = tc!!
                        val nativePrompt = prep.systemPrompt + nativeAddendum(hasCloud = bridge != null)
                        val onlinePrompt = FcLoop(llm).buildPrompt(nativePrompt, history, text)
                        // The save sink for the ONLINE path mints through /local/turn/complete
                        // (NOT persistLocalSave) -- the no-double-mint guarantee. v1 carries the
                        // provenance INLINE in finalResponse (already rendered into the assistant
                        // text); a structured tool_transcript is a later enhancement.
                        val completeSink: (SaveRequest, String) -> Unit = { req, _ ->
                            viewModelScope.launch {
                                val res = onlineClient.complete(
                                    CompleteRequest(
                                        turnId = prep.turnId,
                                        operator = op,
                                        prompt = text,
                                        finalResponse = req.assistantResponse,
                                        toolTranscript = emptyList(),
                                    ),
                                )
                                // Resilience (Task 11): prepare succeeded (online) but complete()
                                // came back null (mesh dropped mid-turn). Fall back to the durable
                                // queue so the turn is minted on reconnect -- a completed turn is
                                // NEVER silently lost.
                                if (res == null) persistLocalSave(req)
                            }
                        }
                        streamLocalNativeAgentTurn(
                            engine = llm,
                            phone = phoneController,
                            phoneTools = ResidentTools.phoneActuators() + ResidentTools.intentActions(),
                            bridge = bridge,
                            prompt = onlinePrompt,
                            injectedTools = prep.tools,
                            operator = op,
                            model = model,
                            text = text,
                            sink = sink,
                            saveSink = completeSink,
                        )
                    } else {
                        // OFFLINE / degraded (Task 11 formalizes): local persona cache prompt +
                        // persistLocalSave queue, no fresh memory / injected tools.
                        val nativePersona = persona + nativeAddendum(hasCloud = bridge != null)
                        val prompt = FcLoop(llm).buildPrompt(nativePersona, history, text)
                        streamLocalNativeAgentTurn(
                            engine = llm,
                            phone = phoneController,
                            phoneTools = ResidentTools.phoneActuators() + ResidentTools.intentActions(),
                            bridge = bridge,
                            prompt = prompt,
                            injectedTools = emptyList(),
                            operator = op,
                            model = model,
                            text = text,
                            sink = sink,
                            saveSink = saveSink,
                        )
                    }
                } else if (llm is ToolCallingLlm && bridge != null) {
                    // MANUAL agent loop (fallback / non-native engines + the fakes):
                    // FcLoop drives the tiered two-hop loop. When the phone controller
                    // is wired it advertises the resident phone actuators and routes
                    // those calls locally - never to the cloud bridge.
                    streamLocalAgentTurn(
                        fcLoop = FcLoop(
                            llm,
                            toolLlm = llm,
                            bridge = bridge,
                            operator = op,
                            phone = phoneController,
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
            if (faulted) {
                // Retry affordance (parity with the SSE catch): when nothing usable
                // arrived — the assistant bubble holds only the friendly error text,
                // no partial content — flag the user turn so the UI offers retry.
                val lastAssistant = _messages.value.lastOrNull()?.takeIf { it.role == "assistant" }
                val usable = (lastAssistant?.content ?: "")
                    .replace(LOCAL_ENGINE_ERROR_TEXT, "").isNotBlank()
                _messages.value = markSendFailedOnUserTurn(
                    _messages.value,
                    userMsg.id,
                    usableContentArrived = usable,
                )
            }
            stopBackgroundService()
            persistHistory()
            // I1 (W5): the turn is done -> apply any model switch that arrived
            // mid-stream now that no native generation is in flight.
            processPendingLocalReWarm()
        }
    }

    /**
     * The DIRECT on-device VISION path (Task W4.3): capture ONE screen frame and
     * ask the on-device model to look at it alongside [userPrompt] — for screens the
     * accessibility tree can't read (Compose / WebView / games). This is a DIRECT
     * multimodal turn, deliberately SEPARATE from the agentic native loop
     * ([runLocalEngineTurn]); see [VisionLlm] for why an image can't be a tool
     * result inside the litertlm native loop (and the deferred autonomous-vision
     * enhancement).
     *
     * Flow (each step degrades GRACEFULLY with a clear message, never a crash):
     *  1. Resolve the on-device provider; with none, fall back to the placeholder.
     *  2. Capture a frame via [screenCapture] (reusing the overlay's MediaProjection
     *     + the password redaction gate). A REFUSED capture (password focused) shows
     *     [LOCAL_VISION_PASSWORD_REFUSED_TEXT]; an UNAVAILABLE capture shows its
     *     customer-facing reason. The bytes are EPHEMERAL.
     *  3. If the model isn't image-capable (`!is VisionLlm`), show
     *     [LOCAL_VISION_UNSUPPORTED_TEXT] (the text path still works).
     *  4. Otherwise load the engine + stream [streamLocalVisionTurn] into the same
     *     assistant bubble, persisting the TEXT turn (never the screenshot).
     *
     * Mirrors [sendViaLocalEngine]'s single-[streamJob] discipline so a prior
     * in-flight turn is cancelled and clear/stop can cancel this one.
     */
    fun lookAtScreen(userPrompt: String) {
        streamJob?.cancel()
        streamJob = viewModelScope.launch {
            val provider = localProviderOrWire()
            if (provider == null) {
                // No installed model → reuse the placeholder path with a vision-flavored ask.
                sendViaLocalPlaceholder(userPrompt)
                return@launch
            }

            // Append the user message + a streaming assistant placeholder (parity with
            // runLocalEngineTurn). The user text is shown verbatim; the screenshot is
            // never added to the transcript.
            val userMsg = UiMessage(
                role = "user",
                content = userPrompt,
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
            _chatState.value = ChatState.STREAMING
            startBackgroundService("Looking at your screen...")

            val sink: (String, Boolean) -> Unit = { content, streaming ->
                updateLastMessage(content = content, isStreaming = streaming, isThinking = false)
            }
            val op = currentOperator
            val model = currentModel.ifBlank { null }

            var faulted = false
            try {
                // 1. Capture the frame (gate runs inside capture()).
                val capture = (screenCapture ?: com.aiblackbox.portal.overlay.OverlayScreenCapture())
                    .also { screenCapture = it }
                    .capture()
                when (capture) {
                    is com.aiblackbox.portal.overlay.ScreenCaptureResult.RefusedPassword -> {
                        sink(LOCAL_VISION_PASSWORD_REFUSED_TEXT, false)
                    }
                    is com.aiblackbox.portal.overlay.ScreenCaptureResult.Unavailable -> {
                        sink(capture.reason, false)
                    }
                    is com.aiblackbox.portal.overlay.ScreenCaptureResult.Success -> {
                        val llm = provider()
                        if (llm !is VisionLlm) {
                            // Model can't see images — text path still works; say so.
                            sink(LOCAL_VISION_UNSUPPORTED_TEXT, false)
                        } else {
                            val cache = personaCacheOrBuild()
                            // Load engine + fetch persona on IO (load() is idempotent).
                            val engineToLoad = localEngine
                            val modelFile = localEngineModelFile
                            if (engineToLoad != null && modelFile != null) {
                                // Warm-loop guard (send/vision = deliberate warm, not
                                // gated): set/clear the disk flag around load() so an
                                // OOM-kill here still leaves the flag set for the next
                                // launch's auto-warm to skip on.
                                warmInflightStore.setInflight(true)
                                try {
                                    withContext(Dispatchers.IO) {
                                        engineToLoad.load(modelFile, localEngineDelegate)
                                    }
                                } finally {
                                    warmInflightStore.setInflight(false)
                                }
                            }
                            val persona = withContext(Dispatchers.IO) { cache?.get(op) ?: "" }
                            // A vision turn is fresh (no transcript history needed for a
                            // single screen Q&A); buildPrompt with empty history.
                            val prompt = FcLoop(llm).buildPrompt(persona, emptyList(), userPrompt)
                            val saveSink: (SaveRequest, String) -> Unit = { req, _ -> persistLocalSave(req) }
                            val ok = streamLocalVisionTurn(
                                engine = llm,
                                prompt = prompt,
                                imageBytes = listOf(capture.pngBytes),
                                userMessage = userPrompt,
                                operator = op,
                                model = model,
                                sink = sink,
                                saveSink = saveSink,
                            )
                            faulted = !ok
                        }
                    }
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "look-at-screen error: ${e.message}", e)
                val partial = _messages.value.lastOrNull()?.content ?: ""
                updateLastMessage(
                    content = partial + LOCAL_ENGINE_ERROR_TEXT,
                    isStreaming = false,
                    isThinking = false,
                )
                faulted = true
            }
            _chatState.value = stateAfterLocalTurn(faulted, _chatState.value)
            stopBackgroundService()
            persistHistory()
            // I1 (W5): the turn is done -> apply any model switch that arrived
            // mid-stream now that no native generation is in flight.
            processPendingLocalReWarm()
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
     * Build (once) or return the server-bracketed turn client wired to this VM's api
     * (Task 10). Returns null when the api is not yet initialized (api?:return
     * convention, mirroring [toolBridgeOrBuild]) — no `api!!`. With no client the
     * native path falls back to the local persona-cache turn (the offline branch).
     */
    private fun turnClientOrBuild(): TurnClient? {
        turnClient?.let { return it }
        val client = api ?: return null
        val built = TurnClient(client)
        turnClient = built
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
        // Task W5: honor the user's ACTIVE on-device model selection (persisted under
        // "model_local" -> [currentLocalModelSlug]). Pick the installed bundle whose
        // slug matches it; if unset or no longer installed (e.g. just deleted), fall
        // back to installedModels()'s alphabetically-first entry (gemma-4-e2b, the
        // LIGHTER model) -- a safe RAM default and the prior behavior.
        val bundle = installed.firstOrNull { it.slug == currentLocalModelSlug }
            ?: installed.firstOrNull()
            ?: return null // no model -> placeholder path

        val delegate = "gpu" // Edge Gallery parity: GPU ~10x faster; load() falls back to CPU on GPU-init failure
        val targetPath = bundle.file.absolutePath

        // M3: SERIALIZE with the foreground-service warm. The service is the PRIMARY
        // warmer ([preloadLocalEngine] kicks it via LocalModelService.start). If it is
        // cold-loading THIS bundle, WAIT for it to pin the engine in the holder rather
        // than building a SECOND engine concurrently — two parallel 3.66GB GPU loads
        // OOM the device ("the model could not finish" / crash on launch). We give the
        // async service a brief head start to publish its [LocalEngineHolder.warmingPath]
        // marker, then keep waiting only while it is actively warming this exact bundle,
        // capped by a grace window. If it never claims the warm (service not running /
        // refused) we fall straight through to the unchanged USE_HOLDER/BUILD_OWN
        // decision below — the graceful fallback is preserved.
        run {
            val start = android.os.SystemClock.elapsedRealtime()
            while (LocalEngineHolder.getOrNull() == null &&
                android.os.SystemClock.elapsedRealtime() - start < SERVICE_WARM_GRACE_MS) {
                val serviceWarmingThis = LocalEngineHolder.warmingPath == targetPath
                val withinHeadStart =
                    android.os.SystemClock.elapsedRealtime() - start < SERVICE_WARM_HEADSTART_MS
                if (!serviceWarmingThis && !withinHeadStart) break
                delay(SERVICE_WARM_POLL_MS)
            }
        }

        // R2-C: PREFER the warm, PROCESS-resident engine pinned by
        // [LocalModelService] when it matches the active bundle -- it is already
        // loaded (no ~10-75s cold load) and survives VM/process recycles. The pure
        // [engineSourceFor] is the decision: USE_HOLDER iff the holder is non-empty
        // AND built for THIS bundle path; otherwise BUILD_OWN (the pre-R2-C path,
        // also taken when the service never started -- the graceful fallback).
        val held = LocalEngineHolder.getOrNull()
        if (engineSourceFor(held != null, LocalEngineHolder.modelPath, targetPath) == EngineSource.USE_HOLDER) {
            // Borrow the service-owned warm engine. We do NOT own it, so onCleared /
            // invalidateLocalEngine must not close() it (localEngineFromHolder = true).
            val warm = held!!
            localEngine = warm
            localEngineModelFile = bundle.file
            localEngineDelegate = delegate
            localEngineFromHolder = true
            val provider: () -> LocalLlm = { warm }
            localLlmProvider = provider
            return provider
        }

        // BUILD_OWN: no warm engine to borrow (service not running / different
        // model). Build the VM-owned engine for the installed bundle (default CPU
        // delegate), threading the PER-MODEL config (Task W2): maxTokens (fallback
        // to the engine default when the descriptor leaves it null) + sampler trio.
        // This is exactly the pre-R2-C path -- the guaranteed fallback.
        val cfg = bundle.config
        val engine = LiteRtEngine.fromInstalled(
            appContext,
            bundle.file,
            delegate = delegate,
            maxTokens = cfg.maxTokens ?: LiteRtEngine.DEFAULT_MAX_TOKENS,
            sampler = SamplerSettings(
                topK = cfg.topK,
                topP = cfg.topP,
                temperature = cfg.temperature,
            ),
            // Task W4: thread the per-model vision capability so the engine sets a
            // visionBackend + allows generateWithImage ONLY for image-capable bundles
            // (text-only models keep the working CPU text path untouched).
            supportImage = cfg.supportImage,
        )
        localEngine = engine
        localEngineModelFile = bundle.file
        localEngineDelegate = delegate
        localEngineFromHolder = false
        val provider: () -> LocalLlm = { engine }
        localLlmProvider = provider
        return provider
    }

    /**
     * Drop the cached on-device engine wiring (Task W5) so the next
     * [localProviderOrWire] / [preloadLocalEngine] rebuilds it -- used when the
     * ACTIVE local model selection changes, so a different installed bundle is
     * actually loaded instead of the previously-wired one. Closes the old native
     * engine (idempotent, guarded) and clears the warm marker so a re-warm runs.
     */
    private fun invalidateLocalEngine() {
        // R2-C: close ONLY a VM-OWNED engine; never the service-owned warm engine
        // borrowed from [LocalEngineHolder] (the service owns its lifecycle). We
        // just drop our reference + re-wire; if the active model changed, the
        // service's own re-warm (or the BUILD_OWN fallback) handles the new bundle.
        if (!localEngineFromHolder) {
            runCatching { localEngine?.close() }
        }
        localEngine = null
        localEngineModelFile = null
        localEngineFromHolder = false
        localLlmProvider = null
        warmingModelPath = null
        _localEngineState.value = LocalEngineState.IDLE
    }

    /**
     * I1 (W5 review): true while an on-device turn is actively running, so a
     * model-selection change defers its engine teardown until the turn finishes
     * rather than close()-ing the native runtime mid-generation. STREAMING and
     * THINKING are the in-flight states for a local turn (see [runLocalEngineTurn]
     * / the vision turn); IDLE/ERROR are terminal.
     */
    private fun isLocalTurnInFlight(): Boolean =
        _chatState.value == ChatState.STREAMING || _chatState.value == ChatState.THINKING

    /**
     * I1 (W5 review): apply a model-selection change that arrived mid-turn. Called
     * at every local-turn settle point -- normal turn completion
     * ([runLocalEngineTurn]) AND, per the final-pass review, on STOP
     * ([cancelStream]) and CLEAR ([clearHistory]), since those cancel the stream
     * and so skip the completion path. A no-op unless [pendingLocalReWarm] was set
     * (the consume decision is the pure [consumePendingReWarm]). Now that no
     * generation is in flight, it is safe to close() the old engine and re-warm
     * the newly-selected bundle.
     */
    private fun processPendingLocalReWarm() {
        // Consume the pending flag exactly once (pure decision, unit-tested).
        if (!consumePendingReWarm(pendingLocalReWarm)) return
        pendingLocalReWarm = false
        if (!ChatProvider.fromId(currentProvider).isLocal) return
        invalidateLocalEngine()
        preloadLocalEngine()
    }

    /**
     * Task W1 — warm the on-device engine WHILE the app/chat is open, so the first
     * send is instant instead of paying the ~10-75s cold model load on the first
     * turn (matching Edge Gallery's "initialize once, keep warm" pattern).
     *
     * Gated + idempotent:
     *  - Only warms when the LOCAL provider is the ACTIVE/selected one — we do NOT
     *    pull a multi-GB model into RAM for a cloud-only session. DESIGN NOTE: an
     *    always-warm-on-open variant (for a future "another model calls the local
     *    engine as a tool" path) is a one-line change — drop the [currentProvider]
     *    `isLocal` gate below.
     *  - Resolves the production engine via [localProviderOrWire] (the SAME wiring
     *    the send path uses) on [Dispatchers.IO]; with no installed model it stays
     *    [LocalEngineState.IDLE] and does nothing.
     *  - Guards against duplicate/concurrent warms ([shouldStartWarm] + the
     *    [warmingModelPath] in-flight marker): a repeat trigger while WARMING, or
     *    while already READY for the same model, returns WITHOUT launching a second
     *    `load()` (which would just await the in-flight Mutex anyway) — no redundant
     *    coroutine, no state churn.
     *  - NEVER blocks Main and NEVER crashes: a load fault sets
     *    [LocalEngineState.ERROR] (the lazy fallback in [runLocalEngineTurn] still
     *    works — `load()` is idempotent — so the user can still send and the next
     *    trigger re-warms).
     *
     * Safe to call before [initialize] (no api → [localProviderOrWire] returns null
     * → IDLE) and to call repeatedly (the guards make repeats cheap no-ops).
     */
    fun preloadLocalEngine() {
        // Provider gate: only warm for the active local provider (see DESIGN NOTE).
        if (!ChatProvider.fromId(currentProvider).isLocal) return
        // Auto-warm SETTING gate (user preference): if the user opted OUT of
        // auto-warm-on-open, do NOT warm here -- leave the engine IDLE so the model
        // loads LAZILY on the first send (runLocalEngineTurn's load() is idempotent).
        // This is a user choice, checked BEFORE the OOM-crash guard; the send path
        // never consults it, so a deliberate send always warms.
        if (!localWarmPrefs.autoWarmEnabled()) {
            _localEngineState.value = LocalEngineState.IDLE
            Log.i(TAG, "skipping auto-warm: disabled by user setting; will warm lazily on first send")
            return
        }
        // Warm-loop guard (defense-in-depth): if the PREVIOUS warm is still marked
        // in-flight, the process was SIGKILLed mid-load (device OOM) and never ran
        // its success/failure cleanup. Auto-warming again would just OOM and crash
        // again -- an unbounded crash/restart loop. SKIP the auto-warm, RE-ARM the
        // flag (so a deliberate send can retry), and leave the engine IDLE (the UI
        // pill shows no spinner; a user send lazily warms). The send path does NOT
        // consult this flag, so the manual retry is always available.
        if (!com.aiblackbox.portal.data.local.WarmInflightStore.shouldAutoWarm(warmInflightStore.isInflight())) {
            warmInflightStore.setInflight(false)
            _localEngineState.value = LocalEngineState.IDLE
            Log.w(TAG, "skipping auto-warm: prior warm did not complete (likely OOM-killed); send to retry")
            return
        }
        // R2-C: the FOREGROUND SERVICE is now the PRIMARY warmer -- it pins the
        // engine in the PROCESS-level [LocalEngineHolder] so it loads ONCE and
        // survives VM/process recycles. Best-effort + idempotent (a second start
        // while a warm is in flight is ignored; LiteRtEngine.load() is itself
        // Mutex-idempotent). NEVER throws into us (LocalModelService.start swallows
        // a platform refusal), so the VM-side warm below stays the graceful
        // fallback: if the service no-ops, [localProviderOrWire] BUILDs its own
        // engine and this same coroutine load()s it, exactly as before R2-C.
        LocalModelService.start(appContext)
        // Cheap pre-launch guard: a warm is already in flight → nothing to do.
        if (!shouldStartWarm(_localEngineState.value)) return

        viewModelScope.launch {
            // Resolve (and lazily wire) the production engine off the main thread.
            val provider = withContext(Dispatchers.IO) { localProviderOrWire() }
            if (provider == null) {
                // No installed model (or api not ready) → nothing to warm.
                _localEngineState.value = LocalEngineState.IDLE
                return@launch
            }
            val engine = localEngine
            val modelFile = localEngineModelFile
            // A FakeLocalLlm (tests) / fake-wired provider has no model file — there
            // is nothing to preload; reflect READY iff it reports already loaded.
            if (engine == null || modelFile == null) {
                _localEngineState.value =
                    if (provider().isLoaded) LocalEngineState.READY else LocalEngineState.IDLE
                return@launch
            }
            val targetPath = modelFile.absolutePath
            // Idempotency (model now known): already READY+loaded for this exact model
            // → keep the in-flight marker pinned and skip the redundant warm.
            if (_localEngineState.value == LocalEngineState.READY &&
                warmingModelPath == targetPath && engine.isLoaded) {
                return@launch
            }
            // Concurrency: another warm for this model is already mid-flight.
            if (warmingModelPath == targetPath && _localEngineState.value == LocalEngineState.WARMING) {
                return@launch
            }
            warmingModelPath = targetPath
            _localEngineState.value =
                localEngineStateAfter(_localEngineState.value, LocalEngineEvent.WARM_STARTED)
            // Warm-loop guard: persist "in-flight" to disk BEFORE load() so that, if
            // load() OOM-SIGKILLs the process, the flag survives still-set and the
            // next launch's preloadLocalEngine SKIPS the auto-warm (no crash loop).
            warmInflightStore.setInflight(true)
            try {
                // load() is idempotent + Mutex-serialized (instant if already loaded);
                // a concurrent first-send simply awaits this same in-flight load.
                withContext(Dispatchers.IO) { engine.load(modelFile, localEngineDelegate) }
                // Warm completed cleanly -> clear the in-flight flag (re-arm).
                warmInflightStore.setInflight(false)
                _localEngineState.value =
                    localEngineStateAfter(_localEngineState.value, LocalEngineEvent.WARM_SUCCEEDED)
                Log.d(TAG, "on-device engine warmed (READY) for $targetPath")
            } catch (e: kotlinx.coroutines.CancellationException) {
                // Cancellation (VM cleared / new warm) is NOT a fault — clear the
                // in-flight marker so a re-trigger can warm again, then unwind. This
                // is a GRACEFUL stop, so clear the disk flag too (no crash occurred).
                warmInflightStore.setInflight(false)
                if (warmingModelPath == targetPath) warmingModelPath = null
                throw e
            } catch (e: Exception) {
                // GRACEFUL (caught) failure -> clear the disk flag: this was not a
                // process kill, so the next launch may auto-warm again.
                warmInflightStore.setInflight(false)
                _localEngineState.value =
                    localEngineStateAfter(_localEngineState.value, LocalEngineEvent.WARM_FAILED)
                // Clear the in-flight marker so the next trigger retries the warm.
                if (warmingModelPath == targetPath) warmingModelPath = null
                Log.w(TAG, "on-device engine warm failed (lazy fallback still works): ${e.message}")
            }
        }
    }

    /**
     * The persisted AUTO-WARM-on-open setting (default true). Read by the on-device
     * model settings screen to pre-fill its auto-warm Switch. A thin passthrough to
     * [LocalWarmPrefs] -- the single store the [preloadLocalEngine] auto path reads.
     */
    fun autoWarmEnabled(): Boolean = localWarmPrefs.autoWarmEnabled()

    /**
     * Persist the AUTO-WARM-on-open setting from the settings screen Switch. When
     * false the [preloadLocalEngine] auto path skips the warm and the model loads
     * lazily on the first send. Thin passthrough to [LocalWarmPrefs].
     */
    fun setAutoWarmEnabled(enabled: Boolean) = localWarmPrefs.setAutoWarmEnabled(enabled)

    /**
     * The ACTIVE on-device model's persisted [ModelConfig] (maxTokens + sampler
     * trio), used by the settings screen to PRE-FILL its window slider + sampler
     * fields with the user's current per-model choice. Reads the sidecar for
     * [currentLocalModelSlug] (falling back to the first installed model, matching
     * [localProviderOrWire]'s selection) off the main thread. Returns an all-null
     * [ModelConfig] (engine defaults) when no model is installed or the api is not
     * ready, so callers can resolve each axis to its DEFAULT_* constant.
     */
    suspend fun currentLocalModelConfig(): com.aiblackbox.portal.data.local.ModelConfig {
        val client = api ?: return com.aiblackbox.portal.data.local.ModelConfig()
        return withContext(Dispatchers.IO) {
            val manager = LocalModelManager.fromContext(
                appContext, LocalModelApi(client), deviceId = "android-device",
            )
            val installed = runCatching { manager.installedModels() }.getOrDefault(emptyList())
            val bundle = installed.firstOrNull { it.slug == currentLocalModelSlug }
                ?: installed.firstOrNull()
            bundle?.config ?: com.aiblackbox.portal.data.local.ModelConfig()
        }
    }

    /**
     * Apply a USER-changed on-device context window + sampler, then RE-WARM the
     * engine so the new values take effect (on-device settings apply layer; the
     * settings SCREEN is a later task -- this is the headless helper it calls).
     *
     * The on-device engine's `maxNumTokens`/`samplerConfig` are FIXED at the native
     * `initialize()`, so a live edit requires three steps, run off the main thread on
     * [Dispatchers.IO] (disk write + native teardown):
     *
     *  1. PERSIST via [LocalModelManager.updateModelConfig] for the ACTIVE
     *     [currentLocalModelSlug] -- rewrites the `<slug>.json` sidecar so the next
     *     warm reads the new config (no-op + early-return if no model is installed,
     *     or the slug has no sidecar).
     *  2. DROP the stale-window engine: [invalidateLocalEngine] clears the VM
     *     wiring (closing a VM-OWNED engine; never the borrowed holder one), AND
     *     [LocalEngineHolder.clearAndClose] releases the PROCESS-held warm engine if
     *     the service pinned one -- otherwise the service-owned engine, still
     *     initialized at the OLD window, would be re-borrowed and the change lost.
     *  3. RE-WARM via [preloadLocalEngine], which (via [LocalModelService.start] +
     *     [localProviderOrWire]) rebuilds + loads the engine at the NEW config and
     *     flips [localEngineState] to [LocalEngineState.WARMING] -> READY, so the UI
     *     can show a "reloading on-device model" state through the existing pill.
     *
     * Provider-gated like the warm path: a no-op when the active provider is not the
     * local one (we never pull a multi-GB model into RAM for a cloud-only session).
     * Pass null for any axis to leave it unchanged (see [mergedConfig]).
     *
     * @param maxTokens new context window, or null to keep the current value.
     * @param topK / topP / temperature sampler overrides, null to keep current.
     */
    fun applyLocalModelSettings(
        maxTokens: Int?,
        topK: Int?,
        topP: Float?,
        temperature: Float?,
    ) {
        // Provider gate: only re-warm for the active local provider (mirrors
        // [preloadLocalEngine]); a cloud-only session has no engine to reconfigure.
        if (!ChatProvider.fromId(currentProvider).isLocal) return
        val slug = currentLocalModelSlug
        if (slug.isBlank()) {
            Log.w(TAG, "applyLocalModelSettings: no active on-device model slug; nothing to persist")
            return
        }
        val client = api ?: return
        val sampler = SamplerSettings(topK = topK, topP = topP, temperature = temperature)
        viewModelScope.launch {
            // (1) Persist the new config off the main thread (disk write).
            val persisted = withContext(Dispatchers.IO) {
                val manager = LocalModelManager.fromContext(
                    appContext, LocalModelApi(client), deviceId = "android-device",
                )
                runCatching { manager.updateModelConfig(slug, maxTokens, sampler) }
                    .getOrDefault(false)
            }
            if (!persisted) {
                // Slug not installed / unreadable sidecar -> nothing to re-warm.
                Log.w(TAG, "applyLocalModelSettings: no sidecar updated for '$slug'; skipping re-warm")
                return@launch
            }
            // (2) Drop BOTH the VM wiring AND the process-held warm engine so the
            // stale-window engine is not reused: invalidate clears VM state (and
            // closes a VM-owned engine), clearAndClose releases the service-pinned
            // engine if present (idempotent + guarded when nothing is held).
            invalidateLocalEngine()
            LocalEngineHolder.clearAndClose()
            // (3) Re-warm at the NEW config; preload sets WARMING -> READY so the UI
            // surfaces a "reloading on-device model" state via the existing pill.
            preloadLocalEngine()
        }
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
    private fun sendViaLocalPlaceholder(text: String, clearInput: Boolean = true) {
        val userMsg = UiMessage(
            role = "user",
            content = text,
            provider = currentProvider,
            model = currentModel,
        )
        val placeholder = buildLocalPlaceholder(currentProvider, currentModel)
        _messages.value = (_messages.value + userMsg + placeholder).takeLast(MAX_CHAT_MESSAGES)
        // clearInput=false on the retry path — don't wipe a draft typed since the failure.
        if (clearInput) _inputText.value = TextFieldValue()
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
    private fun sendViaSSE(
        text: String,
        imageUrls: List<String>,
        repo: ChatRepository,
        clearInput: Boolean = true,
    ) {
        // Append user message
        val userMsg = UiMessage(
            role = "user",
            content = text,
            images = imageUrls,
            provider = currentProvider,
            model = currentModel
        )
        // Captured for the failure path: when the stream dies with NOTHING usable
        // arrived, THIS user turn gets sendFailed=true so the UI offers a retry chip.
        val userMsgId = userMsg.id
        _messages.value = _messages.value + userMsg
        // clearInput=false on the retry path (retryMessage) — a retried turn must
        // not wipe whatever draft the user has typed since the failure.
        if (clearInput) _inputText.value = TextFieldValue()

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
            // SSE "error" events accumulate here, NOT in content — see the "error"
            // case in processSSEEvent and the failure branch in the finalize below.
            val errors = StringBuilder()
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
                // M3 (task 3.6): stamp this phone's OWN tailnet identity on the request so a
                // device-control tool this turn triggers defaults to targeting THIS device.
                // Interface enumeration runs off the main thread; null when not on the tailnet
                // (backend then falls back to the operator's primary device).
                val originDeviceId = withContext(Dispatchers.IO) {
                    com.aiblackbox.portal.data.remote.TailnetAddress.localTailnetIpv4()
                }
                val flow = if (imageUrls.isEmpty()) {
                    repo.sendStream(text, history, currentOperator, currentProvider, currentModel.ifBlank { null },
                        sessionId = cuSessionId, deviceId = cuDeviceId, camera = erCamera,
                        originDeviceId = originDeviceId)
                } else {
                    repo.sendStreamMultimodal(text, imageUrls, history, currentOperator, currentProvider, currentModel.ifBlank { null },
                        sessionId = cuSessionId, deviceId = cuDeviceId, camera = erCamera,
                        originDeviceId = originDeviceId)
                }

                flow.collect { event ->
                    processSSEEvent(
                        event, content, reasoning, mediaTasks, errors
                    ) { model, tokens, prov ->
                        if (model != null) streamModel = model
                        if (tokens != null) tokenCount = tokens
                        if (prov != null) provenance = prov
                    }

                    // Update the streaming assistant message — the user still sees
                    // error-event text as it arrives (rendered after real content).
                    updateLastMessage(
                        content = combineContentAndErrors(content.toString(), errors.toString()),
                        reasoning = reasoning.toString().ifBlank { null },
                        isStreaming = true,
                        isThinking = _chatState.value == ChatState.THINKING,
                        model = streamModel,
                        mediaTasks = mediaTasks.toList()
                    )
                }

                // Stream complete — finalize. An ERROR-ONLY stream (no real content,
                // only SSE "error" events) is a FAILURE outcome: the ERROR state set
                // by the error event must NOT be stomped to IDLE, the raw error text
                // must NOT be minted into the ledger via saveConversation, and the
                // user turn gets the retry affordance. Real-content-also-arrived
                // keeps the previous behavior (protects mid-stream transient errors).
                val realContent = content.toString()
                val errorText = errors.toString()
                val display = combineContentAndErrors(realContent, errorText)
                val failedOutcome = streamOutcomeIsFailure(realContent, errorText)

                if (!failedOutcome) {
                    _chatState.value = ChatState.IDLE
                    if (isCuProvider && _cuStatus.value == "running") {
                        _cuStatus.value = "complete"
                    }
                }
                stopBackgroundService()
                updateLastMessage(
                    content = display,
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

                if (failedOutcome) {
                    // Error-only stream: flag the user turn for retry, persist so the
                    // error bubble + chip survive a reload. NO saveConversation — raw
                    // error text must never mint a ledger snapshot. State stays ERROR
                    // (set by the error event; never IDLE here).
                    _messages.value = markSendFailedOnUserTurn(
                        _messages.value,
                        userMsgId,
                        usableContentArrived = false,
                    )
                    persistHistory()
                } else {
                    // Save conversation for snapshot (matches Portal /chat/save).
                    // provenance is forwarded so backend auto-mint records context lineage.
                    // assistantMessageId pins the artifacts[] from the /chat/save response
                    // (Phase 6a) onto THIS streamed assistant turn (the last message).
                    saveConversation(text, display, reasoning.toString(), streamModel, tokenCount, provenance,
                        assistantMessageId = _messages.value.lastOrNull()?.id)

                    // Persist to local storage
                    persistHistory()

                    // Auto-TTS: speak the response if enabled
                    // Matches Portal window.triggerAutoTTS() called after full response
                    if (autoTtsEnabled && content.isNotBlank()) {
                        _autoTtsEvent.tryEmit(content.toString())
                    }
                }

            } catch (e: Exception) {
                Log.e(TAG, "SSE error: ${e.message}", e)
                _chatState.value = ChatState.ERROR
                stopBackgroundService()
                updateLastMessage(
                    content = combineContentAndErrors(content.toString(), errors.toString())
                        .ifBlank { "Error: ${e.message}" },
                    isStreaming = false,
                    isThinking = false
                )
                // Retry affordance: when NOTHING usable arrived (REAL content blank —
                // accumulated error-event text does not count as usable, so an
                // error-then-exception stream still gets the retry chip), flag the
                // user turn so ChatBubble renders it. A user-initiated STOP
                // (streamJob.cancel → CancellationException) is NOT a send failure.
                if (e !is kotlinx.coroutines.CancellationException) {
                    _messages.value = markSendFailedOnUserTurn(
                        _messages.value,
                        userMsgId,
                        usableContentArrived = content.isNotBlank(),
                    )
                    // Persist so the error bubble + retry affordance survive a reload.
                    persistHistory()
                }
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
        errors: StringBuilder,
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
                // Accumulated SEPARATELY from real content: the bubble still renders
                // content+errors (combineContentAndErrors), but an error-only stream
                // must finalize as a FAILURE — never stomped to IDLE, never minted
                // into the ledger via saveConversation, and it must not defeat the
                // usableContentArrived check that drives the retry affordance.
                if (errors.isNotEmpty()) errors.append("\n\n")
                errors.append(event.data)
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
        provenance: Provenance? = null,
        assistantMessageId: String? = null
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
                // /chat/save returns the raw JSON body, including the Phase 6a
                // "artifacts" array. api.post -> ChatRepository.saveConversation
                // already forwards the body verbatim, so just parse it here and
                // attach the typed refs to the just-saved assistant message
                // (mirrors how resolveMediaTaskInMessage attaches media). Tolerant:
                // missing/[]/malformed -> no-op (parseArtifacts never throws).
                val body = repo.saveConversation(request)
                val artifacts = parseArtifacts(body)
                if (artifacts.isNotEmpty()) {
                    attachArtifactsToMessage(assistantMessageId, artifacts)
                    // Persist so the chips survive a history reload.
                    persistHistory()
                }
            } catch (e: Exception) {
                Log.w(TAG, "saveConversation failed (non-critical): ${e.message}")
            }
        }
    }

    /**
     * Attach [artifacts] to the just-saved assistant message and refresh the flow.
     * Targets [assistantMessageId] when known; otherwise falls back to the last
     * assistant message (the turn that just streamed). Replaces the message's
     * artifacts list, mirroring how resolveMediaTaskInMessage updates a message.
     */
    private fun attachArtifactsToMessage(assistantMessageId: String?, artifacts: List<ArtifactRef>) {
        val current = _messages.value.toMutableList()
        if (current.isEmpty()) return
        val idx = if (assistantMessageId != null) {
            current.indexOfLast { it.id == assistantMessageId }
        } else {
            current.indexOfLast { it.role == "assistant" }
        }
        if (idx < 0) return
        current[idx] = current[idx].copy(artifacts = artifacts)
        _messages.value = current
        Log.d(TAG, "Attached ${artifacts.size} artifact(s) to message ${current[idx].id}")
    }

    // =========================================================================
    // Public actions
    // =========================================================================
    fun clearHistory() {
        streamJob?.cancel()
        // I1 (final-pass review): a model switch DEFERred mid-turn must not be
        // dropped when the turn is cancelled. The normal turn-completion path
        // (runLocalEngineTurn) is SKIPPED on cancel, so apply it here. Guarded
        // no-op when nothing is pending.
        processPendingLocalReWarm()
        _messages.value = emptyList()
        _chatState.value = ChatState.IDLE
        viewModelScope.launch { historyStore.clear(currentOperator) }
    }

    /**
     * Clear the CURRENT operator's conversation (the on-device-settings reset).
     *
     * The on-device model accretes its per-turn statefulness inside its 6144-token
     * window and PULLS memory on demand; this hard-resets that accumulated session
     * context. Empties the in-memory `_messages` flow AND persists an EMPTY list for
     * [currentOperator] through [ChatHistoryStore.save] (the same seam history is
     * saved through), so the cleared session does not resurrect on the next operator
     * load or app restart. A mid-turn DEFERred model re-warm is applied first (same
     * guard as [clearHistory] -- the turn-completion path is skipped on cancel).
     *
     * The actual two-step effect (emit empty -> persist empty) is the pure,
     * unit-tested [performClearLocalConversation]; this just binds it to this VM's
     * `_messages` flow + `historyStore`.
     */
    fun clearLocalConversation() {
        streamJob?.cancel()
        processPendingLocalReWarm()
        _chatState.value = ChatState.IDLE
        val op = currentOperator
        viewModelScope.launch {
            performClearLocalConversation(
                operator = op,
                emit = { _messages.value = it },
                save = { operator, messages -> historyStore.save(operator, messages) },
            )
        }
    }

    fun cancelStream() {
        streamJob?.cancel()
        // I1 (final-pass review): see clearHistory -- a mid-turn model switch
        // (localReWarmAction -> DEFER) is applied at turn completion, which a
        // STOP skips; apply the pending re-warm here. Guarded no-op otherwise.
        processPendingLocalReWarm()
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
     * G3-T13 (M3.3): STOP a single active task from the TaskPanel. Fires the REAL
     * per-task cancel (POST /tasks/{id}/cancel — G2-T8), which signals the concrete
     * work and marks the row CANCELLED. No optimistic local flip: the pill reflects
     * `cancelled` on the next discovery/poll tick, keeping the server the source of
     * truth (mirrors the Portal STOP button).
     */
    fun cancelTask(taskId: String) {
        val repo = taskRepository ?: return
        viewModelScope.launch {
            try {
                repo.cancel(taskId)
            } catch (e: Exception) {
                Log.w(TAG, "cancelTask($taskId) failed: ${e.message}")
            }
        }
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
                    // 'cancelled' is terminal (G2-T8): must end the poll and clean
                    // up the placeholder, or the flow collects forever.
                    val isCancelled = status.status.equals("cancelled", true)
                    if (isDone || isFailed || isCancelled) {
                        // Emit completion event for notifications — but NOT for a
                        // cancel: it is operator-initiated, so a push notification
                        // about the thing they just stopped is noise (and the
                        // consumer would render any non-completed status as
                        // "Failed"). Matches the backend suppress. G2-T8.
                        if (!isCancelled) _taskCompletedEvent.tryEmit(status)

                        // Replace placeholder with actual media in the message
                        if (isDone && status.resultUrl != null) {
                            resolveMediaTaskInMessage(taskId, taskType, status.resultUrl!!)
                        } else {
                            // Failed or cancelled — just remove the placeholder
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
        if (provider != "custom") {
            // Same hygiene for the custom load-status map (Task 7.1).
            _customModelStatus.value = emptyMap()
        }
        if (apiProvider == null) {
            // No live models for this provider — clear so Constants fallback is used
            _liveModels.value = emptyList()
            return
        }

        // Custom never touches the 5-min cache — read OR write: servers are
        // added/removed in the wizard and models load/unload between switches,
        // so the roster+status must stay live (mirrors the Portal's skipCache
        // and CronViewModel.selectProvider, bypass-all-caches rule).
        val skipCache = apiProvider == "custom"

        // Cache hit — instant population, no network
        val cached = modelsCache[apiProvider]
        val now = System.currentTimeMillis()
        if (!skipCache && cached != null && now - cached.first < MODELS_CACHE_TTL_MS) {
            _liveModels.value = cached.second
            if (apiProvider == "computer-use") {
                _cuModelBackends.value = cuBackendsCache[apiProvider] ?: emptyMap()
            }
            Log.d(TAG, "Models cache hit for $provider (age ${(now - cached.first) / 1000}s)")
            return
        }

        if (skipCache) {
            // Empty-catalog staleness fix (Portal parity, commit 4240459):
            // _liveModels still holds the PREVIOUS provider's list here, and —
            // unlike the other providers, where keeping it during the fetch is
            // the intended fallback — those ids are meaningless as custom
            // models. Clear eagerly so the pill falls back to Constants' Auto
            // entry until the live roster arrives, and STAYS clear if the
            // registry is empty or the box is unreachable (before this fix an
            // empty/dead registry kept the previous provider's models
            // selectable as "custom" models forever).
            _liveModels.value = emptyList()
            _customModelStatus.value = emptyMap()
        }

        viewModelScope.launch {
            try {
                val response = currentApi.get("/models/$apiProvider")
                // Stale-response guard (CronViewModel's providerForCurrentList
                // is the in-repo precedent): a late-arriving 200 from a previous
                // provider's in-flight fetch must not clobber the list a newer
                // switch owns — and with custom never cache-hitting, every
                // visit-then-leave of custom opens that window. Both
                // currentProvider writes (store.provider collector) and this
                // coroutine run on Main, so the check is race-free.
                if (provider != currentProvider) return@launch // stale response; a newer switch owns the UI
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
                if (apiProvider == "custom") {
                    // Custom hydrates UNCONDITIONALLY on a 200 (Portal parity,
                    // 4240459): an EMPTY models list is real state (all servers
                    // removed in the wizard) and must clear the roster. Zero
                    // models → no Auto entry either (nothing for the backend to
                    // resolve "" to); the Composer then shows Constants' seed.
                    val statuses = modelsArr.mapNotNull { el ->
                        try {
                            val m = el.jsonObject
                            val id = m["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                            // Tolerant: absent OR null status → not in the map
                            // (contentOrNull turns JsonNull into null).
                            val status = m["status"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null
                            id to status
                        } catch (_: Exception) { null }
                    }.toMap()
                    val withAuto = if (models.isEmpty()) emptyList() else {
                        // Auto ("") resolves server-side to the registry default —
                        // label it with the default model's name when known
                        // (mirrors CronViewModel.selectProvider + the CU branch).
                        val defaultId = obj["default_id"]?.jsonPrimitive?.contentOrNull
                        val defaultName = models.firstOrNull { it.first == defaultId }?.second
                        listOf("" to (defaultName?.let { "Auto - $it" } ?: "Auto - Latest")) + models
                    }
                    // Adjacent writes, no suspension point between them — same
                    // atomic-w.r.t.-composition rationale as the CU branch below.
                    _liveModels.value = withAuto
                    _customModelStatus.value = statuses
                    // Never cached — roster/status must stay live (see skipCache).
                    Log.d(TAG, "Fetched ${models.size} live custom models (${statuses.count { it.value == "loaded" }} loaded)")
                } else if (models.isNotEmpty()) {
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
                // Keep whatever's in liveModels (could be from Constants fallback).
                // For custom that is the EMPTY list published eagerly above —
                // never a stale roster; the Composer shows Constants' Auto seed.
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
        /** Map provider IDs to the backend's expected provider key.
         *  Note: Android uses "gemini" as provider key but backend uses "google".
         *  T3 (2026-05-18) added xai mapping — previously a gap that left
         *  the xai dropdown stuck on the malformed Constants.MODEL_CONFIG
         *  fallback (which had IDs like "grok-4.1-fast" that don't exist
         *  in the xAI API). Pure (companion) so the mapping is unit-testable
         *  without the ViewModel — the same unknown→null trap would otherwise
         *  silently no-op a new provider's hydration (it did for custom). */
        @VisibleForTesting
        internal fun mapProviderForApi(provider: String): String? = when (provider) {
            "gemini" -> "google"
            "anthropic" -> "anthropic"
            "openai" -> "openai"
            "xai" -> "xai"
            // CU production pass 2026-06: the backend exposes GET /models/computer-use
            // (merged live Anthropic/Google/OpenAI CU catalogs with `backend` field).
            "computer-use" -> "computer-use"
            // Task 7.1: user-registered OpenAI-compatible servers — the backend
            // exposes GET /models/custom (registry roster + `status` field).
            "custom" -> "custom"
            else -> null // Voice/agent providers don't have model endpoints
        }

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
         * The retry-specific in-flight guard: STREAMING *and* THINKING both block
         * (the same pair ChatScreen's send affordance treats as in-flight). A new
         * send flips THINKING→STREAMING almost immediately, but a stale retry chip
         * tapped inside that window would otherwise fire a CONCURRENT stream —
         * both racing updateLastMessage and potentially double-minting. Pure so
         * the race guard is unit-testable without the ViewModel.
         */
        fun shouldBlockRetry(state: ChatState): Boolean =
            state == ChatState.STREAMING || state == ChatState.THINKING

        /**
         * Did a COMPLETED stream end in failure? True when NO real content arrived
         * and at least one SSE "error" event did — the bubble is a pure error
         * bubble. Drives the finalize branch in [sendViaSSE]: failure keeps
         * ChatState.ERROR (no IDLE stomp), SKIPS saveConversation (raw error text
         * must never mint a ledger snapshot), and flags the user turn for retry.
         * Real-content-also-arrived is NOT a failure (mid-stream transient errors
         * keep the delivered reply). Pure for unit tests.
         */
        fun streamOutcomeIsFailure(realContent: String, errorText: String): Boolean =
            realContent.isBlank() && errorText.isNotBlank()

        /**
         * The display string for a bubble that may carry both real streamed
         * content and separately-accumulated SSE error-event text — the user
         * always still sees the error message, it just never pollutes the REAL
         * content used for save/usable-content decisions. Pure for unit tests.
         */
        fun combineContentAndErrors(realContent: String, errorText: String): String =
            when {
                errorText.isBlank() -> realContent
                realContent.isBlank() -> errorText
                else -> "$realContent\n\n$errorText"
            }

        /**
         * Flag the user turn [userMessageId] as sendFailed when a send died with
         * NOTHING usable arrived ([usableContentArrived] false). Pure so the
         * failure→retry-affordance rule is unit-testable without the ViewModel
         * (same strategy as [routeFor]/[shouldBlockSend]). Partial-content
         * failures keep the turn unflagged — the user got something readable.
         */
        fun markSendFailedOnUserTurn(
            messages: List<UiMessage>,
            userMessageId: String,
            usableContentArrived: Boolean,
        ): List<UiMessage> =
            if (usableContentArrived) messages
            else messages.map {
                if (it.id == userMessageId && it.role == "user") it.copy(sendFailed = true) else it
            }

        /**
         * The pure REPLACE step of [retryMessage]: given the failed user turn's
         * [userMessageId], drop the error assistant bubble that immediately
         * follows it AND the failed user message itself, returning the remaining
         * list plus the removed user message (text + images retained, ready to
         * re-fire). Unknown id → (unchanged list, null). The error bubble is
         * always the immediate next message because both send paths append the
         * assistant placeholder right after the user turn.
         */
        fun retryRemoval(
            messages: List<UiMessage>,
            userMessageId: String,
        ): Pair<List<UiMessage>, UiMessage?> {
            val idx = messages.indexOfLast { it.id == userMessageId && it.role == "user" }
            if (idx < 0) return messages to null
            val userMsg = messages[idx]
            val out = messages.toMutableList()
            if (idx + 1 <= out.lastIndex && out[idx + 1].role == "assistant") out.removeAt(idx + 1)
            out.removeAt(idx)
            return out to userMsg
        }

        /**
         * The v1 VISION TRIGGER (W4 follow-up): does this on-device message ask the
         * model to LOOK AT THE SCREEN? When true, [sendViaLocalEngine] routes the
         * turn to [lookAtScreen] (capture a frame + multimodal turn) instead of the
         * normal text/agent turn — making the built-but-unreachable vision path
         * actually usable on-device. (A dedicated UI affordance and an autonomous
         * mid-loop "the model decides to look" path are FUTURE enhancements; this
         * conservative phrase match is the v1 trigger.)
         *
         * Deliberately CONSERVATIVE so it never hijacks a normal message: it matches
         * only explicit screen-looking intents ("look at my screen", "what's on my
         * screen", "what do you see", "read the screen for me", "describe my
         * screen", "can you see my screen"…), case-insensitively, and requires the
         * phrase to actually reference the SCREEN (or a direct "what do you see")
         * rather than firing on the word "screen" alone (e.g. "my screen is
         * cracked" must NOT trigger a capture). Pure so it is unit-testable without
         * the ViewModel.
         */
        fun isLookAtScreenRequest(text: String): Boolean {
            val t = text.lowercase().trim()
            if (t.isEmpty()) return false
            // The word "screen" must appear as a WHOLE WORD that refers to THIS
            // device's live display — not as part of "screenshot"/"screensaver" and
            // not immediately followed by a settings/feature qualifier ("screen
            // reader", "screen time", "screen of …", …). Without this anchor the
            // classifier hijacks ordinary chat ("the screen reader settings",
            // "the screenshot I described") into an on-device capture.
            //   \bscreen\b      → "screen" bounded by non-word chars (excludes
            //                       screenshot/screensaver/touchscreen).
            //   (?!\s*<qualifier>) → not a screen-setting/feature/"screen of" use.
            val screenAnchor = Regex(
                "\\bscreen\\b(?!\\s*(reader|time|saver|saving|brightness|" +
                    "rotation|resolution|lock|protector|mirroring|cast(?:ing)?|" +
                    "share|sharing|record(?:ing|er)?|of)\\b)"
            )
            if (!screenAnchor.containsMatchIn(t)) return false
            // A look/see/read/describe/check/view verb that targets the anchored
            // screen. Kept tight so a statement like "my screen is cracked" /
            // "share my screen" doesn't fire. Each phrase re-asserts the anchor so a
            // qualifier ("screen reader") can't satisfy a bare "screen" inside it.
            val verbRe = "(look at|see|read|describe|check|view|what'?s? on|what is on)"
            val targeted = Regex(
                "$verbRe\\s+(on\\s+)?(my |the |this )?$screenAnchor"
            )
            return targeted.containsMatchIn(t)
        }

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

        /**
         * Whether a NEW on-device warm should be launched from [current] (Task W1).
         * Pure so the no-double-warm guard is unit-testable: a warm is launched from
         * any state EXCEPT [LocalEngineState.WARMING] (a warm is already in flight).
         * IDLE/READY/ERROR all permit a (re)warm — READY/ERROR re-evaluate against
         * the concrete model path inside [preloadLocalEngine] and no-op when already
         * loaded for that exact model.
         */
        fun shouldStartWarm(current: LocalEngineState): Boolean =
            current != LocalEngineState.WARMING

        /**
         * What to do when the persisted active on-device model slug emits (Task
         * W5). PURE so the selection->warm branching (incl. the I1 mid-stream
         * defer + the M1 first-emission no-op) is unit-testable without the
         * ViewModel:
         *  - [previousSlug] blank  -> [LocalReWarmAction.NONE] (first replayed
         *    emission of the persisted value; the provider collector owns the
         *    initial warm, so don't invalidate + re-warm it).
         *  - slug unchanged        -> NONE.
         *  - not the local provider -> NONE (no on-device engine to re-wire).
         *  - a real change while a turn is in flight -> [LocalReWarmAction.DEFER]
         *    (close()-ing the native engine mid-generation is unsafe; apply it at
         *    turn completion).
         *  - a real change, idle    -> [LocalReWarmAction.NOW] (invalidate + warm).
         */
        fun localReWarmAction(
            previousSlug: String,
            newSlug: String,
            isLocalProvider: Boolean,
            turnInFlight: Boolean,
        ): LocalReWarmAction = when {
            previousSlug.isBlank() -> LocalReWarmAction.NONE
            newSlug == previousSlug -> LocalReWarmAction.NONE
            !isLocalProvider -> LocalReWarmAction.NONE
            turnInFlight -> LocalReWarmAction.DEFER
            else -> LocalReWarmAction.NOW
        }

        /**
         * PURE: whether [processPendingLocalReWarm] should apply (and thereby
         * consume) a DEFERred model re-warm. Trivial today (it just IS the pending
         * flag), but extracting it makes the final-pass fix testable without the
         * AndroidViewModel: a re-warm DEFERred mid-turn ([localReWarmAction] ->
         * DEFER, which sets pendingLocalReWarm) must be applied at the next settle
         * point -- including a STOP ([cancelStream]) or CLEAR ([clearHistory]),
         * which previously dropped it -- and must be a guarded no-op when nothing
         * is pending (so calling it on every cancel/clear is safe).
         */
        fun consumePendingReWarm(pending: Boolean): Boolean = pending

        /**
         * PURE core of [clearLocalConversation] (Task: on-device settings screen).
         * Mirrors the [localEngineStateAfter] / [consumePendingReWarm] convention so
         * the clear path is unit-testable WITHOUT the AndroidViewModel (no Application,
         * no main dispatcher). Two effects, in order:
         *  1. [emit] the EMPTY message list (clears the in-memory `_messages` flow);
         *  2. [save] the EMPTY list for [operator] through the SAME ChatHistoryStore
         *     seam history is persisted through elsewhere (so the cleared session does
         *     not resurrect on the next operator load / app restart).
         * The instance method just binds [emit] to `_messages.value =` and [save] to
         * `historyStore.save`.
         */
        suspend fun performClearLocalConversation(
            operator: String,
            emit: (List<UiMessage>) -> Unit,
            save: suspend (String, List<UiMessage>) -> Unit,
        ) {
            emit(emptyList())
            save(operator, emptyList())
        }

        /**
         * The on-device engine readiness state machine (Task W1) — pure so the
         * IDLE→WARMING→READY happy path and the →ERROR failure path are unit-tested
         * without the ViewModel. Transitions:
         *  - [LocalEngineEvent.WARM_STARTED]   → [LocalEngineState.WARMING]
         *  - [LocalEngineEvent.WARM_SUCCEEDED] → [LocalEngineState.READY]
         *  - [LocalEngineEvent.WARM_FAILED]    → [LocalEngineState.ERROR]
         *
         * A late SUCCEEDED/FAILED that arrives after the warm was superseded (state
         * no longer WARMING) is ignored — the in-flight outcome must not clobber a
         * newer state (e.g. a fresh WARMING the next trigger started). STARTED always
         * wins (a new warm overrides any prior terminal state).
         */
        fun localEngineStateAfter(
            current: LocalEngineState,
            event: LocalEngineEvent,
        ): LocalEngineState = when (event) {
            LocalEngineEvent.WARM_STARTED -> LocalEngineState.WARMING
            LocalEngineEvent.WARM_SUCCEEDED ->
                if (current == LocalEngineState.WARMING) LocalEngineState.READY else current
            LocalEngineEvent.WARM_FAILED ->
                if (current == LocalEngineState.WARMING) LocalEngineState.ERROR else current
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

        // M3 serialization ([localProviderOrWire] waits for the foreground service's
        // SINGLE warm rather than racing it with a 2nd concurrent engine build — the
        // double-warm that OOM'd the device on launch). HEADSTART covers the async gap
        // between LocalModelService.start() and the service publishing its warming
        // marker; GRACE caps the wait for a slow GPU cold load; POLL is the check tick.
        private const val SERVICE_WARM_HEADSTART_MS = 2_500L
        private const val SERVICE_WARM_GRACE_MS = 90_000L
        private const val SERVICE_WARM_POLL_MS = 200L

        /**
         * Friendly text appended to the (possibly partial) reply when the on-device
         * engine faults mid-generation. Mirrors the SSE error path: surface a
         * non-crashing, human message rather than a stack trace.
         */
        const val LOCAL_ENGINE_ERROR_TEXT =
            "\n\n[on-device error — the local model could not finish this reply]"

        /**
         * Shown (in place of a model reply) when the on-device "look at my screen"
         * vision path can't run because the active model has no image input
         * (Task W4). The text path still works — only vision is unavailable.
         */
        const val LOCAL_VISION_UNSUPPORTED_TEXT =
            "This on-device model can't see images. Switch to an image-capable model to have it look at your screen."

        /**
         * Shown when a screen capture is REFUSED because a password field is focused
         * (Task W4.2 redaction gate) — the model is never shown a screenshot of a
         * credential entry. Customer-facing + non-alarming.
         */
        const val LOCAL_VISION_PASSWORD_REFUSED_TEXT =
            "I won't capture the screen while a password field is focused. Close it and ask again."

        /**
         * Concise phone-control steering (the BASE, always-present part) appended to
         * the persona ONLY on the NATIVE engine-driven turn (Task W3 follow-up).
         * Mirrors Edge Gallery's prescriptive ordered-prompt style, kept short for
         * the small E4B model: act on the phone via the matching action directly;
         * one tool at a time; reply briefly when done. Not added to the manual/text
         * paths. The cloud-vault sentence ([NATIVE_CLOUD_CAPABILITY_SENTENCE]) is
         * spliced in by [nativeAddendum] ONLY when a cloud bridge is wired, so an
         * offline native turn never advertises find_blackbox_tool / run_blackbox_tool.
         */
        const val NATIVE_PHONE_CONTROL_ADDENDUM =
            "\n\nTo act on the phone, call the matching action directly (e.g. " +
            "flashlight_on, show_map, open_app). " +
            "Call one tool at a time; when the task is done, reply briefly. " +
            "You retain this conversation's recent turns."

        /**
         * The cloud-vault steering sentence, appended to
         * [NATIVE_PHONE_CONTROL_ADDENDUM] ONLY when the cloud tool bridge is wired
         * (Fix 2, final-pass review). Without a bridge the native turn registers no
         * find_blackbox_tool / run_blackbox_tool, so the prompt must not name them.
         */
        const val NATIVE_CLOUD_CAPABILITY_SENTENCE =
            " For a BlackBox capability you don't have a direct action for (roll " +
            "dice, generate an image, search memory, send something), call " +
            "find_blackbox_tool(query) FIRST, then run_blackbox_tool with the name " +
            "it returns. Do NOT use web_search to find your tools. " +
            "Your long-term memory is NOT pre-loaded: call search_snapshots (or " +
            "find_blackbox_tool) to recall older or deeper context when you need it."

        /**
         * PURE: the persona addendum for a NATIVE engine-driven turn. The phone
         * sentence is always present; the cloud sentence is present IFF [hasCloud]
         * (the bridge / cloud tools are wired). Inserts the cloud sentence after the
         * phone sentence and before the "one tool at a time" closing so the
         * prompt never advertises tools that are not registered.
         */
        fun nativeAddendum(hasCloud: Boolean): String =
            if (!hasCloud) NATIVE_PHONE_CONTROL_ADDENDUM
            else NATIVE_PHONE_CONTROL_ADDENDUM.replace(
                " Call one tool at a time;",
                NATIVE_CLOUD_CAPABILITY_SENTENCE + " Call one tool at a time;",
            )

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
        /**
         * SL-2 â size-bounded rolling history for the ON-DEVICE (`local`) prompt.
         *
         * Keep the NEWEST [turns] and drop the OLDEST first until the SUM of carried
         * turn-text chars is <= [maxChars]; a turn is ATOMIC (whole include/exclude,
         * never split). Order is preserved oldestânewest, matching what
         * [FcLoop.buildAgentPrompt] expects.
         *
         * Empty input â empty. Everything fits â passthrough (unchanged). If even the
         * SINGLE newest turn alone exceeds [maxChars] we still return THAT turn (never
         * empty for a non-empty input): the user's latest context must not be silently
         * dropped, and the per-turn soft-stop
         * ([com.aiblackbox.portal.data.local.overTurnBudget] / trim) is the backstop
         * against an over-budget prompt.
         *
         * PURE (Strings only) so it is JVM-unit-testable without the AndroidViewModel;
         * counts chars the same way [com.aiblackbox.portal.data.local.TurnBudget] does
         * (String.length).
         */
        fun budgetHistory(turns: List<FcLoop.Turn>, maxChars: Int): List<FcLoop.Turn> {
            if (turns.isEmpty()) return emptyList()
            var used = 0
            var start = turns.size
            // Walk newestâoldest, including a turn only while it fits whole.
            for (i in turns.indices.reversed()) {
                val cost = turns[i].text.length
                if (used + cost > maxChars && start != turns.size) break
                used += cost
                start = i
            }
            // start == turns.size only when the newest turn alone is over budget; keep it.
            if (start == turns.size) start = turns.size - 1
            return turns.subList(start, turns.size)
        }


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
         * PURE core of the DIRECT VISION turn (Task W4.3) — the [streamLocalTurn]
         * sibling that hands the model a captured screen frame ALONGSIDE the prompt.
         * It is a DIRECT multimodal generation, NOT a tool inside the native agent
         * loop: the litertlm [com.google.ai.edge.litertlm.OpenApiTool.execute] returns
         * a `String`, so an image can't be fed back as a tool RESULT — see [VisionLlm]
         * for the full rationale + the deferred autonomous-vision enhancement.
         *
         * Collects [VisionLlm.generateWithImage]'s text-delta Flow (same shape as
         * [streamLocalTurn]'s [FcLoop.runTurn] Flow) into the SAME streaming assistant
         * bubble, then persists via [saveSink]. The [imageBytes] are EPHEMERAL: they
         * are passed to the engine ONLY to build the prompt and are NEVER written to
         * the save request (the persisted transcript carries the user TEXT + the
         * assistant reply, never the screenshot).
         *
         * Contract (mirrors [streamLocalTurn]):
         *  - Streams `sink(runningText, isStreaming=true)` per delta.
         *  - Normal completion: `sink(fullText, false)` then
         *    `saveSink(SaveRequest, provider="local")`; returns `true`.
         *  - Mid-stream throw (generateWithImage's Flow can fault — e.g. the engine
         *    can't run vision on this device): caught via `.catch`, appends
         *    [LOCAL_ENGINE_ERROR_TEXT], `sink(partial+error, false)`, DOES NOT save,
         *    returns `false`. Never rethrows.
         *
         * Threading mirrors [streamLocalTurn]: `.flowOn(Dispatchers.IO)` moves the
         * generation onto IO; `.collect`/[sink] stay on the caller's dispatcher.
         *
         * @return `true` if the turn completed and was saved, `false` if it faulted.
         */
        suspend fun streamLocalVisionTurn(
            engine: VisionLlm,
            prompt: String,
            imageBytes: List<ByteArray>,
            userMessage: String,
            operator: String,
            model: String?,
            sink: (content: String, isStreaming: Boolean) -> Unit,
            saveSink: (request: SaveRequest, provider: String) -> Unit,
        ): Boolean {
            val acc = StringBuilder()
            var faulted = false
            engine.generateWithImage(prompt, imageBytes)
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
            val full = acc.toString()
            sink(full, false)
            // EPHEMERALITY: the save request carries the user TEXT + the reply only —
            // never the screenshot bytes (they exist solely to build the prompt above).
            val request = buildSaveRequest(
                operator = operator,
                userMessage = userMessage,
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
         * PURE core of the NATIVE (engine-driven) on-device tool turn (Task W3) - the
         * [streamLocalAgentTurn] sibling for the litertlm built-in auto tool loop. Where
         * [streamLocalAgentTurn] collects [FcLoop.runAgent] (manual loop,
         * `automaticToolCalling = false`), this collects
         * [NativeToolCallingLlm.generateWithToolsNative] (engine loop,
         * `automaticToolCalling = true`) so the ENGINE drives the tool loop and
         * terminates cleanly (`onDone`) - fixing the manual-path loop-repeat with the
         * small E4B model. Renders the streamed [LlmEvent]s INLINE via the SAME
         * [renderToolCall] / [renderToolOutcome] convention and persists via [saveSink],
         * so it is rendering-identical to the manual agent turn.
         *
         * **Unified scope (W3 follow-up): phone/intent + cloud, ONE native loop.** The
         * engine drives BOTH families with `automaticToolCalling = true`:
         *  - PHONE/INTENT: each [phoneTools] schema becomes a [NativeTool] whose
         *    `execute` dispatches LOCALLY through [phone] ([PhoneController.dispatch]) --
         *    bridging the suspend dispatch into the engine's SYNCHRONOUS execute with
         *    `runBlocking(Dispatchers.IO)` (Edge Gallery's `AgentTools.execute` pattern).
         *    These NEVER touch the cloud [bridge]; the autonomy gate + credential handoff
         *    stay INSIDE the actuator (downstream of [phone].dispatch), so they still fire.
         *  - CLOUD: when [bridge] is non-null, [buildCloudNativeTools] adds two NativeTools
         *    -- find_blackbox_tool (-> [ToolBridge.searchTools]) + run_blackbox_tool
         *    (-> [ToolBridge.execute], operator-scoped) -- whose `execute` reaches the
         *    [bridge] ONLY (structurally NEVER the [phone]) and carry only the model's
         *    args + the [operator] (no screen/phone content). Offline / no api -> the
         *    cloud tools are omitted and the turn is phone-only.
         * The phone and cloud execute lambdas are SEPARATE, so a phone tool cannot reach
         * the bridge and a cloud tool cannot reach the phone (the W3 separation guarantee).
         * Each dispatch result is serialized to the Gallery-shaped JSON string
         * ([toResultJsonString]) the engine feeds back to the model.
         *
         * Contract mirrors [streamLocalAgentTurn]: TextDelta -> append text; ToolCall ->
         * [renderToolCall]; ToolOutcome -> [renderToolOutcome]; `sink(runningText, true)`
         * per event; normal completion -> `sink(full, false)` + `saveSink`; a mid-stream
         * THROW (engine `onError`) is caught via `.catch`, appends
         * [LOCAL_ENGINE_ERROR_TEXT], DOES NOT save, returns `false`.
         *
         * @return `true` if the turn completed and was saved, `false` if it faulted.
         */
        suspend fun streamLocalNativeAgentTurn(
            engine: NativeToolCallingLlm,
            phone: PhoneController,
            phoneTools: List<ToolSchema>,
            bridge: ToolBridge?,
            prompt: String,
            operator: String,
            model: String?,
            text: String,
            sink: (content: String, isStreaming: Boolean) -> Unit,
            saveSink: (request: SaveRequest, provider: String) -> Unit,
            injectedTools: List<ToolSchema> = emptyList(),
        ): Boolean {
            // PHONE/INTENT NativeTools: each phone/intent schema dispatches LOCALLY
            // through the controller. runBlocking bridges the suspend dispatch into the
            // engine's synchronous execute (Gallery pattern); the autonomy gate lives
            // INSIDE dispatch, so it still fires. These execute bodies reach the
            // PhoneController ONLY -- structurally NEVER the cloud bridge (W3 guarantee).
            val phoneNativeTools = phoneTools.map { schema ->
                NativeTool(
                    schema = schema,
                    execute = { argsJson ->
                        runBlocking(Dispatchers.IO) {
                            phone.dispatch(schema.name, parseNativeArgs(argsJson))
                        }.toResultJsonString()
                    },
                )
            }
            // CLOUD NativeTools (Task W3 follow-up): expose the cloud vault to the SAME
            // native loop as two tools the engine drives like phone actions. Their
            // execute bodies reach the cloud [bridge] ONLY -- structurally NEVER the
            // PhoneController, and they carry only the model's args + the operator (no
            // screen/phone content). find_blackbox_tool discovers; run_blackbox_tool runs
            // the chosen tool (operator-scoped, exactly as the manual path always did).
            // Only wired when a [bridge] is present (offline/no-api -> phone-only).
            // SERVER-INJECTED DIRECT tools (Task 10): the top-K relevant tools that
            // /local/turn/prepare picked become DIRECTLY-callable NativeTools (the model
            // calls each by its real name, e.g. roll_dice, routed straight to the cloud
            // [bridge]); find_blackbox_tool/run_blackbox_tool stay below ONLY as the
            // long-tail fallback. Only wired with a [bridge] (offline -> none).
            val injected = if (bridge != null) buildInjectedNativeTools(injectedTools, bridge, operator) else emptyList()
            val cloudNativeTools = if (bridge != null) buildCloudNativeTools(bridge, operator) else emptyList()
            // Order: phone actuators, then the injected DIRECT tools, then the find/run fallback.
            // distinctBy{name} (phone > injected > cloud precedence) so a name appearing in
            // more than one source — e.g. a server-injected tool that also exists as a cloud
            // tool — is offered to the engine only ONCE (duplicate function declarations can
            // fault litertlm's constrained decoding). Mirrors FcLoop's de-dup.
            val nativeTools = (phoneNativeTools + injected + cloudNativeTools).distinctBy { it.schema.name }
            val acc = StringBuilder()
            var faulted = false
            // Runaway bounds on this native loop: the litertlm engine's OWN internal
            // recurring-tool-call guard is the PRIMARY bound (it owns termination via
            // onDone on this path by design), AND -- defense-in-depth (Task W3 hardening) --
            // [LiteRtEngine.MAX_NATIVE_TOOL_CALLS] is an app-side SOFT cap underneath it:
            // past that many tool executions in a single turn, each further tool refuses
            // to run its side-effecting body and returns a terminal 'step limit reached'
            // result, so a misbehaving model can't loop unbounded with real side effects
            // even if the engine-side guard were ever weakened/absent. This differs from
            // the manual FcLoop's explicit maxIterations only in WHERE the cap lives.
            engine.generateWithToolsNative(prompt, nativeTools)
                .flowOn(Dispatchers.IO)
                .catch { e ->
                    // Telemetry-before-fixes: the engine's onError was previously invisible.
                    Log.w(TAG, "native turn faulted", e)
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
         * Build the cloud-vault [NativeTool]s (Task W3 follow-up) the native engine
         * loop drives ALONGSIDE the phone/intent tools. Every execute body reaches the
         * cloud [bridge] ONLY -- structurally NEVER the [PhoneController] (the W3
         * separation guarantee) -- and passes only the model's args + the [operator] (no
         * screen/phone content; the bridge is operator-scoped, as the manual path was):
         *
         *  - find_blackbox_tool: runBlocking the suspend [ToolBridge.searchTools]
         *    (capped at [ResidentTools.MAX_INJECTED_SCHEMAS]) and return the matches
         *    (name + description) as a Gallery-shaped success string the model reads;
         *    an empty result is a failed "no match (possibly offline)" string.
         *  - run_blackbox_tool: parse name + args, runBlocking [ToolBridge.execute]
         *    (operator-scoped), and return its [ToolResult] as the Gallery-shaped JSON.
         *    A missing/blank name is a failed result (the engine loop continues).
         *  - web_search: HEADLESS direct call -- runBlocking [ToolBridge.execute]
         *    ("web_search") with the model's args so the search RESULTS come BACK into
         *    the turn (NEVER an Android browser intent that would background the app +
         *    evict the on-device model). Mirrors [buildInjectedNativeTools].
         *
         * Internal so it is unit-testable against a fake [ToolBridge].
         */
        internal fun buildCloudNativeTools(bridge: ToolBridge, operator: String): List<NativeTool> =
            ResidentTools.cloudTools().map { schema ->
                when (schema.name) {
                    ResidentTools.WEB_SEARCH -> NativeTool(
                        schema = schema,
                        execute = { argsJson ->
                            runBlocking(Dispatchers.IO) {
                                bridge.execute(schema.name, parseNativeArgs(argsJson), operator)
                            }.toResultJsonString()
                        },
                    )
                    ResidentTools.FIND_BLACKBOX_TOOL -> NativeTool(
                        schema = schema,
                        execute = { argsJson ->
                            val query = (parseNativeArgs(argsJson)["query"] as? JsonPrimitive)
                                ?.contentOrNull?.takeIf { it.isNotBlank() }
                            if (query == null) {
                                toResultJsonString(false, JsonPrimitive("query required"))
                            } else {
                                val found = runBlocking(Dispatchers.IO) {
                                    bridge.searchTools(query, k = ResidentTools.MAX_INJECTED_SCHEMAS)
                                }
                                if (found.isEmpty()) {
                                    toResultJsonString(
                                        false,
                                        JsonPrimitive("no matching tools available (the tool catalog may be unreachable)"),
                                    )
                                } else {
                                    // Format matches as a compact JSON string the model reads,
                                    // carried VERBATIM as the success payload.
                                    toResultJsonString(
                                        true,
                                        JsonPrimitive(formatCloudToolMatches(found)),
                                    )
                                }
                            }
                        },
                    )
                    else -> NativeTool( // RUN_BLACKBOX_TOOL
                        schema = schema,
                        execute = { argsJson ->
                            val args = parseNativeArgs(argsJson)
                            val name = (args["name"] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() }
                            if (name == null) {
                                toResultJsonString(false, JsonPrimitive("tool name required"))
                            } else {
                                val callArgs = parseCloudCallArgs(args["args"])
                                runBlocking(Dispatchers.IO) {
                                    bridge.execute(name, callArgs, operator)
                                }.toResultJsonString()
                            }
                        },
                    )
                }
            }

        /**
         * Turn the top-K tool schemas returned by /local/turn/prepare into DIRECTLY-callable
         * native tools: the model calls each by its real name (e.g. roll_dice) and the engine
         * routes the call straight to the cloud [ToolBridge] (NEVER the phone PhoneController) —
         * no find_blackbox_tool/run_blackbox_tool indirection. find_blackbox_tool remains
         * (via buildCloudNativeTools) ONLY as the long-tail fallback.
         */
        internal fun buildInjectedNativeTools(
            tools: List<ToolSchema>,
            bridge: ToolBridge,
            operator: String,
        ): List<NativeTool> = tools.map { schema ->
            NativeTool(
                schema = schema,
                execute = { argsJson ->
                    val callArgs = parseNativeArgs(argsJson) // the tool's args ARE the payload (no nested "args")
                    runBlocking(Dispatchers.IO) {
                        bridge.execute(schema.name, callArgs, operator)
                    }.toResultJsonString()
                },
            )
        }

        /**
         * Coerce the model-supplied `args` element of a run_blackbox_tool call into the
         * [JsonObject] passed to [ToolBridge.execute]. The small model may send `args`
         * as a JSON OBJECT (preferred) or as a JSON-encoded STRING; either is accepted,
         * anything else (null/absent/malformed) becomes an empty object. NEVER throws.
         * Internal so it is unit-testable.
         */
        internal fun parseCloudCallArgs(element: kotlinx.serialization.json.JsonElement?): JsonObject = when (element) {
            is JsonObject -> element
            is JsonPrimitive -> runCatching { Json.parseToJsonElement(element.content) as? JsonObject }
                .getOrNull() ?: JsonObject(emptyMap())
            else -> JsonObject(emptyMap())
        }

        /**
         * Parse the model-supplied tool-call argument JSON (what the litertlm engine
         * passes to a [NativeTool.execute]) into a [JsonObject] for
         * [PhoneController.dispatch]. A small model can emit a blank, non-object, or
         * malformed payload; this NEVER throws - it returns an empty object so dispatch
         * sees no args (the actuator then reports a normal "missing arg" failure rather
         * than crashing the native turn). Internal so it is unit-testable.
         *
         * NOTE: the engine seam ([nativeOpenApiToolFor]) also parses the same argsJson
         * once to emit the [LlmEvent.ToolCall]; this is a SECOND, independent parse in a
         * different layer (dispatch, not rendering). De-duping would mean threading the
         * parsed object through [NativeTool.execute]'s signature - not worth the coupling.
         */
        internal fun parseNativeArgs(argsJson: String): JsonObject =
            runCatching { Json.parseToJsonElement(argsJson) as? JsonObject }.getOrNull()
                ?: JsonObject(emptyMap())

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
         * Parse the Phase 6a artifacts[] array out of a raw /chat/save response body.
         * Each element is {filename, type, url, size_kb}. TOLERANT by contract: a
         * missing key, an absent/empty/non-array "artifacts", or unparseable JSON all
         * return an empty list — this NEVER throws (the save path must not fail a turn
         * over artifact rendering). Elements missing filename or url are skipped;
         * size_kb defaults to 0.0; type defaults to "".
         */
        @VisibleForTesting
        fun parseArtifacts(rawBody: String?): List<ArtifactRef> {
            if (rawBody.isNullOrBlank()) return emptyList()
            return try {
                val root = provJson.parseToJsonElement(rawBody)
                val arr = (root as? JsonObject)?.get("artifacts")?.let { it as? JsonArray }
                    ?: return emptyList()
                arr.mapNotNull { el ->
                    val obj = el as? JsonObject ?: return@mapNotNull null
                    val filename = obj["filename"]?.jsonPrimitive?.contentOrNull
                    val url = obj["url"]?.jsonPrimitive?.contentOrNull
                    if (filename.isNullOrBlank() || url.isNullOrBlank()) return@mapNotNull null
                    ArtifactRef(
                        filename = filename,
                        type = obj["type"]?.jsonPrimitive?.contentOrNull ?: "",
                        url = url,
                        sizeKb = obj["size_kb"]?.jsonPrimitive?.doubleOrNull ?: 0.0
                    )
                }
            } catch (_: Exception) {
                emptyList()
            }
        }

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
