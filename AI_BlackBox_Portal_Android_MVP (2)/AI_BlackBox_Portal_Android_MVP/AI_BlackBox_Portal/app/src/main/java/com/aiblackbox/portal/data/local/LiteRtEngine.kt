package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.Contents
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import com.google.ai.edge.litertlm.ExperimentalApi
import com.google.ai.edge.litertlm.ExperimentalFlags
import com.google.ai.edge.litertlm.Message
import com.google.ai.edge.litertlm.MessageCallback
import com.google.ai.edge.litertlm.OpenApiTool
import com.google.ai.edge.litertlm.SamplerConfig
import com.google.ai.edge.litertlm.tool
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.channels.trySendBlocking
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.File
import java.util.concurrent.atomic.AtomicInteger

/**
 * The concrete on-device LLM engine (Task 2.6a) — wraps LiteRT-LM
 * (`com.google.ai.edge.litertlm:litertlm-android:0.13.1`) and implements BOTH the
 * Phase-2 text seam ([LocalLlm]) and the Phase-3 tool-aware seam
 * ([ToolCallingLlm]) over ONE [Engine]. See `docs/litert-lm-kotlin-api-0.13.1.md`
 * (the javap-verified ground-truth API surface this is written against).
 *
 * **One engine, two seams.** [ChatViewModel.sendViaLocalEngine] capability-detects
 * the single provider instance: because LiteRtEngine `is ToolCallingLlm` it routes
 * to the agent loop ([FcLoop.runAgent]); the text path ([FcLoop.runTurn]) is still
 * available via [generate] for a text-only fallback. The engine is a SINGLETON
 * reused across turns (initialize() is ~10s — never rebuilt per turn).
 *
 * **STATELESS per call (no built-in tool loop).** [generateWithTools] sets
 * `automaticToolCalling = false`: LiteRT-LM's own auto tool loop registers a FIXED
 * tool set at conversation creation, but our tiered two-hop design needs DYNAMIC
 * tiering ([FcLoop] injects newly-discovered schemas each turn). So each call opens
 * a FRESH conversation, streams ONE model turn, and closes it; [FcLoop.runAgent]
 * owns the loop + dispatch + feed-back. The engine never emits
 * [LlmEvent.ToolOutcome] and never uses `Content.ToolResponse` / `Message.tool`.
 *
 * **Lifecycle.** [load] builds an [EngineConfig] + [Engine] and `initialize()`s it
 * off the main thread (idempotent for the same model). [isLoaded] mirrors
 * `engine.isInitialized()` (the artifact exposes this as a METHOD, not a Kotlin
 * property — the doc's `val` form was a transcription slip the compiler corrected).
 * [close] releases the native engine.
 *
 * @param modelFile the `.litertlm` bundle to run (see [LocalModelManager.installedModels]).
 * @param delegate compute backend: `"cpu"` (default) / `"gpu"` / `"npu"`.
 * @param cacheDir optional dir LiteRT-LM uses to speed up the 2nd load.
 * @param nativeLibraryDir required for the NPU backend
 *   (`context.applicationInfo.nativeLibraryDir`); ignored otherwise. When `"npu"`
 *   is requested without it, [backendFor] falls back to CPU.
 * @param maxTokens the context window (input+output tokens) to configure on the
 *   engine via [EngineConfig.maxNumTokens]. Defaults to [DEFAULT_MAX_TOKENS] —
 *   set explicitly because the litertlm default (null) is only 4096, far below
 *   what the on-device agent's per-turn prompt needs (see the constant's KDoc).
 * @param supportImage whether this model bundle accepts IMAGE input (Task W4).
 *   When true, [load] sets [EngineConfig.visionBackend] (see [visionBackendFor])
 *   so the engine can run image inference, and [generateWithImage] is usable;
 *   when false the engine stays text-only and [generateWithImage] gates itself
 *   off (throws), preserving the working CPU text path. Threaded per-model from
 *   [ModelConfig.supportImage] (W2) exactly like [maxTokens]/[sampler].
 */
/**
 * Per-model sampler overrides (Task W2), mirroring Edge Gallery's
 * topK/topP/temperature config. All fields OPTIONAL: a null field falls back to
 * [LiteRtEngine.DEFAULT_SAMPLER_TOP_K] / [DEFAULT_SAMPLER_TOP_P] /
 * [DEFAULT_SAMPLER_TEMPERATURE] when a `SamplerConfig` IS built. When ALL three
 * are null ([isUnset]) the engine omits `samplerConfig` entirely, preserving the
 * prior (litertlm-default) behavior.
 *
 * litertlm's `SamplerConfig(topK: Int, topP: Double, temperature: Double)` requires
 * all three together (verified against the 0.13.1 artifact: the synthetic
 * default-ctor defaults only `seed`, not the trio), so partial overrides are filled
 * with the constants above rather than passed through individually.
 */
data class SamplerSettings(
    val topK: Int? = null,
    val topP: Float? = null,
    val temperature: Float? = null,
) {
    /** True when no override is set -> the engine omits `samplerConfig`. */
    val isUnset: Boolean get() = topK == null && topP == null && temperature == null
}

class LiteRtEngine(
    private val modelFile: File,
    private val delegate: String = "cpu",
    private val cacheDir: String? = null,
    private val nativeLibraryDir: String? = null,
    private val maxTokens: Int = DEFAULT_MAX_TOKENS,
    private val sampler: SamplerSettings = SamplerSettings(),
    private val supportImage: Boolean = false,
) : LocalLlm, ToolCallingLlm, NativeToolCallingLlm, VisionLlm {

    @Volatile
    private var engine: Engine? = null
    // Absolute path of the model the current engine was initialized for, so a
    // repeat load() of the SAME bundle is a no-op (initialize() is ~10s).
    @Volatile
    private var loadedModelPath: String? = null

    // Graceful GPU-vision degrade (W4 follow-up — the Important fix). When a
    // [supportImage] bundle's GPU vision backend fails to initialize on a
    // GPU-less / limited device, [load] retries ONCE building the engine WITHOUT
    // a visionBackend (text-only) so the engine ALWAYS loads for text/native —
    // and sets this flag. [generateWithImage] then gates on it (alongside
    // [supportImage]) to fail GRACEFULLY ("vision unavailable on this device")
    // instead of crashing the TEXT turns the single shared engine also serves.
    // Reset on every (re)load so a model switch / a successful vision init clears it.
    @Volatile
    private var visionDegraded: Boolean = false

    // Serializes load() so two concurrent first-turns can't both pass the
    // idempotency check-then-act and double-initialize (leaking a native engine)
    // or close-while-building. The ~10s initialize() runs holding this lock, so a
    // second concurrent load() of the same bundle simply waits and then no-ops.
    private val loadLock = Mutex()

    /**
     * Build + initialize the native engine for [modelFile] on [delegate], off the
     * main thread ([Dispatchers.IO]). **Idempotent:** if an engine is already
     * initialized for the same model path, this is a no-op (no ~10s re-warm). A
     * different model path tears down the old engine and builds a new one.
     *
     * The [modelFile] / [delegate] args satisfy the [LocalLlm] contract but the
     * engine's OWN constructor fields are authoritative — production builds one
     * engine per installed bundle and re-`load()`s it. The passed [modelFile] is
     * used as the load target (it equals the ctor's `modelFile` in production).
     */
    override suspend fun load(modelFile: File, delegate: String) = withContext(Dispatchers.IO) {
        val targetPath = modelFile.absolutePath
        // Serialize the whole check-then-act under loadLock so concurrent first
        // turns can't both build an engine (leak) or close-while-building.
        loadLock.withLock {
            // Idempotent: same bundle already initialized → nothing to do.
            if (engine?.isInitialized() == true && loadedModelPath == targetPath) {
                return@withLock
            }
            // Switching models (or recovering from a half-built engine): drop the old one.
            engine?.close()
            engine = null
            loadedModelPath = null

            // Vision backend (Task W4): only set a visionBackend when this model
            // accepts image input ([supportImage]); otherwise omit it (the engine
            // default applies) so the working text-only CPU path is untouched. Edge
            // Gallery forces GPU for vision ("must be GPU for Gemma 3n"); we mirror
            // that ([visionBackendFor]) — whether E4B does image input on CPU on a
            // given device is unknown and verified separately on-device. The primary
            // (text) backend is ALWAYS [backendFor(delegate)] regardless.
            val visionBackend = visionBackendFor(supportImage)
            // Fresh load: clear any prior degrade flag (a different model / a now-
            // working GPU should start un-degraded).
            visionDegraded = false
            // Happy path: build with the configured (vision) backend and initialize.
            // If GPU vision init THROWS on a GPU-less/limited device AND a
            // visionBackend WAS set, retry ONCE WITHOUT it (text-only) so the
            // engine ALWAYS loads for text/native; mark vision degraded so
            // generateWithImage fails gracefully instead of crashing text turns.
            // Safety cascade so the GPU default (Edge Gallery parity, ~10x faster than
            // CPU) can't brick the model on a GPU-less/limited device:
            //   1. (delegate, vision) — happy path.
            //   2. GPU-vision init failure -> (delegate, text-only) [vision degraded].
            //   3. delegate init failure outright (no usable GPU) -> (CPU, text-only).
            //   4. CPU also fails -> rethrow (caller surfaces it).
            val built = try {
                buildAndInitialize(targetPath, delegate, visionBackend)
            } catch (e: Throwable) {
                if (shouldRetryWithoutVision(supportImage, visionBackend != null)) {
                    // Log CLASS NAME only (never a message — could carry device/path detail).
                    android.util.Log.w(
                        TAG,
                        "vision init failed (${e.javaClass.simpleName}); degrading to text-only",
                    )
                    visionDegraded = true
                    try {
                        buildAndInitialize(targetPath, delegate, visionBackend = null)
                    } catch (e2: Throwable) {
                        buildOnCpuOrThrow(targetPath, delegate, e2)
                    }
                } else {
                    buildOnCpuOrThrow(targetPath, delegate, e)
                }
            }
            engine = built
            loadedModelPath = targetPath
        }
    }

    /**
     * Build an [EngineConfig] for [targetPath] on [delegate] — with [visionBackend]
     * when non-null (the [supportImage] path) or text-only when null — then
     * construct + `initialize()` the [Engine] (~10s; called on Dispatchers.IO).
     * Factored so [load] can call it TWICE: once with the configured vision
     * backend, and (on a GPU-vision init failure) once more without it.
     */
    private fun buildAndInitialize(
        targetPath: String,
        delegate: String,
        visionBackend: Backend?,
    ): Engine {
        val config = if (visionBackend != null) {
            EngineConfig(
                modelPath = targetPath,
                backend = backendFor(delegate),
                visionBackend = visionBackend,
                cacheDir = cacheDir,
                // Explicit context window — the litertlm default (null) is only
                // 4096 tokens (device-confirmed: "Input token ids are too long:
                // 4292 >= 4096"), too small for the agent's per-turn prompt.
                maxNumTokens = maxTokens,
            )
        } else {
            EngineConfig(
                modelPath = targetPath,
                backend = backendFor(delegate),
                cacheDir = cacheDir,
                maxNumTokens = maxTokens,
            )
        }
        val built = Engine(config)
        built.initialize() // ~10s; we're on Dispatchers.IO.
        return built
    }

    /** Last resort in the load cascade: if a non-CPU [delegate] (e.g. "gpu") failed to
     *  initialize, retry text-only on CPU so the model still loads; if we were already
     *  on CPU, rethrow [cause] (nothing left to fall back to). */
    private fun buildOnCpuOrThrow(targetPath: String, delegate: String, cause: Throwable): Engine {
        if (delegate.lowercase() == "cpu") throw cause
        android.util.Log.w(
            TAG,
            "$delegate init failed (${cause.javaClass.simpleName}); falling back to CPU",
        )
        visionDegraded = true // CPU path is text-only here
        return buildAndInitialize(targetPath, "cpu", visionBackend = null)
    }

    /** True once a model is loaded and the native engine is initialized. */
    override val isLoaded: Boolean
        // M4 (review): capture the @Volatile engine into a local so close() can't
        // null it between the null-check and the native isInitialized() call.
        get() {
            val e = engine
            return e != null && e.isInitialized()
        }

    /** Release the native engine. After [close], a fresh [load] is required. */
    override fun close() {
        engine?.close()
        engine = null
        loadedModelPath = null
    }

    /**
     * Stream the model's reply to [prompt] as incremental text deltas (Phase-2
     * text path). A COLD Flow: collection opens a tool-less conversation, streams
     * the message, emits each chunk's text, and closes the conversation in a
     * `finally`. Requires [isLoaded] (throws [IllegalStateException] otherwise —
     * the caller loads first).
     */
    override fun generate(prompt: String): Flow<String> = flow {
        val eng = engine
        check(eng != null && eng.isInitialized()) {
            "LiteRtEngine.generate called before load(); call load(modelFile, delegate) first."
        }
        // Per-model sampler (Task W2): only build a ConversationConfig when an
        // override is set; otherwise keep the prior tool-less, default-sampler path.
        val samplerConfig = sampler.toSamplerConfig()
        val conversation = if (samplerConfig != null) {
            eng.createConversation(ConversationConfig(samplerConfig = samplerConfig))
        } else {
            eng.createConversation() // no tools, engine-default sampler
        }
        try {
            conversation.sendMessageAsync(prompt).collect { msg ->
                val text = msg.plainText()
                if (text.isNotEmpty()) emit(text)
            }
        } catch (c: kotlinx.coroutines.CancellationException) {
            // I2 (review): on cancellation (user stop / new send), abort the
            // in-flight native generation, not just close the conversation.
            // TODO(2.6b): confirm whether close() alone aborts the native compute;
            // cancelProcess() is added defensively and is a no-op if already done.
            runCatching { conversation.cancelProcess() }
            throw c
        } finally {
            runCatching { conversation.close() }
        }
    }

    /**
     * Stream the model's reply to [prompt] WITH one or more images as incremental
     * text deltas — the on-device VISION path (Task W4). A COLD Flow: collection
     * opens a tool-less conversation, sends a single multimodal message whose
     * [Contents] hold the [images] FIRST (each as a [Content.ImageBytes]) then the
     * [Content.Text] prompt (Edge Gallery's `runInference(images)` ordering —
     * images precede text), streams each chunk's text, and closes the conversation
     * in a `finally`.
     *
     * **Gated on [supportImage].** A text-only bundle ([supportImage] == false)
     * cannot accept image input, so this throws [IllegalStateException] rather than
     * silently dropping the image — the caller ([ChatViewModel]'s look-at-screen
     * path) checks the capability and falls back to a clear message. With an EMPTY
     * [images] list this also throws (use [generate] for a text-only turn) so a
     * vision turn never quietly degrades to text.
     *
     * **NOT the agentic native loop.** Because [OpenApiTool.execute] returns a
     * String, an image cannot be returned to the model as a tool RESULT inside
     * [generateWithToolsNative]; so vision is a DIRECT multimodal turn (this
     * method), not a `look_at_screen` tool the engine calls mid-loop. An autonomous
     * "model decides to look at the screen mid-loop" path is a future enhancement
     * (it needs a tool result that can carry image bytes back into the engine).
     *
     * The contents-ORDERING is the pure, testable [orderVisionContents]; this thin
     * method just maps the [ByteArray]s to litertlm [Content] and streams (the
     * litertlm-typed glue can't be unit-tested under JDK 17 — see the Mappers
     * header). Requires [isLoaded] (throws [IllegalStateException] otherwise).
     *
     * @param images one or more PNG-encoded frames (e.g. from [ScreenCapture]); the
     *   bytes are passed VERBATIM to [Content.ImageBytes] and are EPHEMERAL — this
     *   method never writes them anywhere; the caller must not persist them either.
     */
    override fun generateWithImage(prompt: String, images: List<ByteArray>): Flow<String> = flow {
        val eng = engine
        check(eng != null && eng.isInitialized()) {
            "LiteRtEngine.generateWithImage called before load(); call load(modelFile, delegate) first."
        }
        check(supportImage) {
            "LiteRtEngine.generateWithImage requires a vision-capable model (supportImage=true); this bundle is text-only."
        }
        // Graceful GPU-vision degrade (W4 follow-up): the engine loaded text-only
        // because GPU vision init failed on this device. Fail with a CLEAR message
        // rather than attempting a vision turn the engine can't serve (the text
        // path still works — the caller surfaces this as "vision unavailable").
        check(!visionDegraded) {
            "LiteRtEngine.generateWithImage: vision is unavailable on this device (GPU vision init failed; the engine is running text-only)."
        }
        check(images.isNotEmpty()) {
            "LiteRtEngine.generateWithImage requires at least one image; use generate() for a text-only turn."
        }
        // Images BEFORE text (Edge Gallery ordering), via the pure orderer.
        val contents = Contents.of(
            orderVisionContents<Content>(
                images = images.map { Content.ImageBytes(it) },
                textContent = Content.Text(prompt),
            ),
        )
        // Per-model sampler (Task W2): only build a ConversationConfig when an
        // override is set; otherwise keep the tool-less, default-sampler path.
        val samplerConfig = sampler.toSamplerConfig()
        val conversation = if (samplerConfig != null) {
            eng.createConversation(ConversationConfig(samplerConfig = samplerConfig))
        } else {
            eng.createConversation()
        }
        try {
            conversation.sendMessageAsync(contents).collect { msg ->
                val text = msg.plainText()
                if (text.isNotEmpty()) emit(text)
            }
        } catch (c: kotlinx.coroutines.CancellationException) {
            runCatching { conversation.cancelProcess() }
            throw c
        } finally {
            runCatching { conversation.close() }
        }
    }

    /**
     * Stream ONE tool-aware model turn (Phase-3 agent path) as a COLD Flow of
     * [LlmEvent]. Opens a fresh conversation with [tools] registered and
     * `automaticToolCalling = false` (we drive the loop ourselves), streams the
     * turn, and closes the conversation in a `finally`. Emits
     * [LlmEvent.TextDelta] for non-empty text and [LlmEvent.ToolCall] for each of
     * the message's tool calls — NEVER [LlmEvent.ToolOutcome] ([FcLoop] produces
     * outcomes). Requires [isLoaded] (throws [IllegalStateException] otherwise).
     */
    override fun generateWithTools(prompt: String, tools: List<ToolSchema>): Flow<LlmEvent> = flow {
        val eng = engine
        check(eng != null && eng.isInitialized()) {
            "LiteRtEngine.generateWithTools called before load(); call load(modelFile, delegate) first."
        }
        // Per-model sampler (Task W2): pass samplerConfig only when an override is
        // set; null leaves litertlm's default sampler in place (prior behavior).
        val samplerConfig = sampler.toSamplerConfig()
        val config = if (samplerConfig != null) {
            ConversationConfig(
                tools = tools.map { tool(openApiToolFor(it)) },
                samplerConfig = samplerConfig,
                automaticToolCalling = false,
            )
        } else {
            ConversationConfig(
                tools = tools.map { tool(openApiToolFor(it)) },
                automaticToolCalling = false,
            )
        }
        val conversation = eng.createConversation(config)
        try {
            conversation.sendMessageAsync(prompt).collect { msg ->
                val text = msg.plainText()
                if (text.isNotEmpty()) emit(LlmEvent.TextDelta(text))
                for (tc in msg.toolCalls) {
                    emit(LlmEvent.ToolCall(tc.name, argsToJsonObject(tc.arguments)))
                }
            }
        } catch (c: kotlinx.coroutines.CancellationException) {
            // I2 (review): abort the in-flight native generation on cancellation.
            // TODO(2.6b): confirm close() alone aborts; cancelProcess() defensive.
            runCatching { conversation.cancelProcess() }
            throw c
        } finally {
            runCatching { conversation.close() }
        }
    }

    /**
     * Stream ONE engine-driven (NATIVE) tool-aware turn (Task W3) as a COLD [Flow]
     * of [LlmEvent]. Unlike [generateWithTools] (manual path, `automaticToolCalling
     * = false`, [FcLoop] drives), this opens a conversation with
     * `automaticToolCalling = true` so the litertlm ENGINE runs the tool loop: it
     * calls each [NativeTool.execute] ITSELF, feeds the returned JSON back into the
     * model, loops until a final answer, then signals `onDone`. That clean
     * termination is the W3 fix for the manual-path loop-repeat with the small E4B
     * model.
     *
     * **Bridging engine -> Flow.** `execute()` is SYNCHRONOUS and is invoked on the
     * engine's callback thread, while the Flow is collected on the caller's
     * coroutine. We use [callbackFlow] backed by a Channel: each tool's `execute`
     * emits [LlmEvent.ToolCall] (before) and [LlmEvent.ToolOutcome] (after) into the
     * channel via [trySendBlocking], and the litertlm [MessageCallback] emits
     * [LlmEvent.TextDelta] on `onMessage`, closes the flow on `onDone`, and closes
     * it with the throwable on `onError`. [awaitClose] cancels the in-flight native
     * generation ([Conversation.cancelProcess]) and closes the conversation when the
     * collector goes away (cancellation / completion).
     *
     * **Constrained decoding.** [ExperimentalFlags.enableConversationConstrainedDecoding]
     * is toggled on around [Engine.createConversation] (and reset after, as Edge
     * Gallery does) to reduce malformed tool-call tokens from the small model. The
     * flag is verified present in the litertlm 0.13.1 artifact.
     *
     * Requires [isLoaded] (throws [IllegalStateException] otherwise).
     */
    @OptIn(ExperimentalApi::class)
    override fun generateWithToolsNative(prompt: String, tools: List<NativeTool>): Flow<LlmEvent> = callbackFlow {
        val eng = engine
        check(eng != null && eng.isInitialized()) {
            "LiteRtEngine.generateWithToolsNative called before load(); call load(modelFile, delegate) first."
        }
        // Per-model sampler (Task W2): pass samplerConfig only when an override is set.
        val samplerConfig = sampler.toSamplerConfig()
        // App-side defensive step cap (Task W3 hardening): a per-INVOCATION counter
        // shared by every tool wrapper below, so the total tool executions in THIS
        // native turn are bounded even if the engine's own recurring-tool-call guard
        // were ever weakened/absent. Fresh per generateWithToolsNative call (resets
        // each turn). See [MAX_NATIVE_TOOL_CALLS] / the pure [overCap] decision.
        val nativeCallCount = AtomicInteger(0)
        // App-side per-turn tool-RESULT budget (snapshot-ledger Task 9): cumulative
        // chars of the (trimmed) tool results fed back this turn. The window is ~16K
        // and each result re-enters it, so a turn with several big results can still
        // overflow even under the step cap. Fresh per generateWithToolsNative call
        // (resets each turn). See [MAX_TURN_TOOL_RESULT_CHARS] / the pure
        // [overTurnBudget] decision (an ADDITIONAL soft-stop gate OR'd with overCap).
        val nativeToolResultChars = AtomicInteger(0)
        // Build engine-driven OpenApiTools: each `execute` runs the NativeTool body
        // AND bridges ToolCall(before)/ToolOutcome(after) into this flow's channel.
        // The shared counter lets each wrapper refuse to run its side-effecting body
        // once the cap is exceeded (returns a terminal "step limit reached" result).
        val providers = tools.map {
            tool(nativeOpenApiToolFor(it, nativeCallCount, nativeToolResultChars) { event -> trySendBlocking(event) })
        }
        // Constrained decoding (Gallery pattern): enable around createConversation,
        // reset after. Reduces malformed tool-call tokens from the small model.
        val priorConstrained = ExperimentalFlags.enableConversationConstrainedDecoding
        val conversation = try {
            ExperimentalFlags.enableConversationConstrainedDecoding = true
            val config = if (samplerConfig != null) {
                ConversationConfig(
                    tools = providers,
                    samplerConfig = samplerConfig,
                    automaticToolCalling = true,
                )
            } else {
                ConversationConfig(
                    tools = providers,
                    automaticToolCalling = true,
                )
            }
            eng.createConversation(config)
        } finally {
            ExperimentalFlags.enableConversationConstrainedDecoding = priorConstrained
        }
        // Engine-driven streaming via MessageCallback: text deltas -> TextDelta,
        // onDone -> complete the flow, onError -> fail the flow. The engine calls
        // each tool's execute() itself between onMessage events (auto tool loop).
        conversation.sendMessageAsync(
            prompt,
            object : MessageCallback {
                override fun onMessage(message: Message) {
                    val text = message.plainText()
                    if (text.isNotEmpty()) trySendBlocking(LlmEvent.TextDelta(text))
                }
                override fun onDone() {
                    channel.close()
                }
                override fun onError(throwable: Throwable) {
                    channel.close(throwable)
                }
            },
            emptyMap(),
        )
        awaitClose {
            // Collector cancelled or flow completed: abort in-flight native compute
            // and release the conversation (mirrors generate()/generateWithTools()).
            runCatching { conversation.cancelProcess() }
            runCatching { conversation.close() }
        }
    }

    /**
     * Map a [delegate] string to the LiteRT-LM [Backend]:
     *  - `"gpu"` → [Backend.GPU]
     *  - `"npu"` → [Backend.NPU] (requires [nativeLibraryDir]; falls back to CPU if null/blank)
     *  - anything else (incl. `"cpu"`) → [Backend.CPU]
     *
     * Internal so a unit test can assert the mapping without a real engine.
     */
    internal fun backendFor(delegate: String): Backend = when (delegate.lowercase()) {
        "gpu" -> Backend.GPU()
        "npu" -> {
            val dir = nativeLibraryDir
            if (dir.isNullOrBlank()) Backend.CPU() else Backend.NPU(dir)
        }
        else -> Backend.CPU()
    }

    companion object {
        /** Log tag (class name only — never logs prompts/results/secrets). */
        private const val TAG = "LiteRtEngine"

        /**
         * Context window (input+output tokens) the engine is configured for.
         *
         * When [EngineConfig.maxNumTokens] is left null the litertlm default is
         * only **4096** tokens — far below Gemma-4 E2B/E4B's native capacity, and
         * too small for the on-device agent's per-turn prompt (persona + the ~24
         * resident phone/intent tool schemas + a `read_screen` dump + tool
         * results). Device testing overran it: `Input token ids are too long.
         * Exceeding the maximum number of tokens allowed: 4292 >= 4096`. We set it
         * explicitly for INTRA-turn headroom.
         *
         * The KV cache grows with this value, so it is bounded for on-device RAM;
         * 16384 is comfortable on an 8GB+ device (the target hardware). Note this
         * is the SINGLE-TURN budget: the BlackBox ledger — not a growing in-prompt
         * transcript — is the memory, so inter-turn history is NOT accumulated
         * (see ChatViewModel's `LOCAL_HISTORY_WINDOW_TURNS`).
         */
        const val DEFAULT_MAX_TOKENS: Int = 16384

        /**
         * Sampler defaults used to FILL a [SamplerSettings] field left null when a
         * `SamplerConfig` is built (litertlm requires the full topK/topP/temperature
         * trio; see [SamplerSettings]). These mirror Edge Gallery's Gemma defaults
         * (topK 64, topP 0.95, temperature 1.0). They apply ONLY when at least one
         * override is set; an all-null [SamplerSettings] omits `samplerConfig` and
         * lets litertlm use its own built-in default.
         */
        const val DEFAULT_SAMPLER_TOP_K: Int = 64
        const val DEFAULT_SAMPLER_TOP_P: Float = 0.95f
        const val DEFAULT_SAMPLER_TEMPERATURE: Float = 1.0f

        /**
         * App-side defensive cap on the number of tool executions in a SINGLE
         * [generateWithToolsNative] turn (Task W3 hardening, defense-in-depth).
         *
         * The litertlm engine's OWN internal recurring-tool-call guard is the
         * PRIMARY bound on the native auto-loop (it terminates via `onDone`). This
         * constant is a SOFT cap layered UNDERNEATH it: if that engine-side bound
         * were ever weakened or absent in a future litertlm release, a misbehaving
         * small model could otherwise loop unbounded executing REAL side-effecting
         * tools (repeated `run_blackbox_tool` / `generate_image` / phone intents).
         * Past this count, each further [OpenApiTool.execute] returns a terminal
         * failed result ("step limit reached") WITHOUT running the tool body, so the
         * model is pushed to give its final answer rather than loop forever.
         *
         * Chosen generous-but-safe: large enough for a realistic multi-step
         * phone+cloud task (e.g. read_screen -> a few intents -> a cloud lookup ->
         * answer), far below any runaway. It does NOT fight the engine -- on the happy
         * path the engine's own `onDone` ends the loop long before this is reached.
         */
        const val MAX_NATIVE_TOOL_CALLS: Int = 24

        /**
         * Convenience factory: build an engine for an installed [modelFile] using
         * the app's native-library dir (so the NPU backend can find vendor
         * delegates). The engine is NOT loaded — call [load] (off the main thread)
         * before generating.
         */
        @JvmStatic
        fun fromInstalled(
            context: android.content.Context,
            modelFile: File,
            delegate: String = "cpu",
            maxTokens: Int = DEFAULT_MAX_TOKENS,
            sampler: SamplerSettings = SamplerSettings(),
            supportImage: Boolean = false,
        ): LiteRtEngine = LiteRtEngine(
            modelFile = modelFile,
            delegate = delegate,
            cacheDir = context.cacheDir.absolutePath,
            nativeLibraryDir = context.applicationInfo.nativeLibraryDir,
            maxTokens = maxTokens,
            sampler = sampler,
            supportImage = supportImage,
        )
    }
}

// ===========================================================================
// Mappers.
//
// IMPORTANT — why the PURE cores take PRIMITIVES, not litertlm types:
// the litertlm-android 0.13.1 artifact is compiled to Java-21 bytecode (class
// major version 65), but this module's JVM unit tests run on JDK 17 — so merely
// CONSTRUCTING a litertlm class (Message/Content/ToolCall) in a unit test throws
// UnsupportedClassVersionError before any code runs (it's not a native-lib load —
// the class file itself won't verify under JDK 17). The Android app build is fine
// (D8/R8 desugar these), but the host test JVM cannot. So the testable cores
// ([plainTextOf], [argsToJsonObject], [toolDescriptionJson], [bridgeDispatchedStub])
// take PRIMITIVES and live in LiteRtMappersTest; the thin litertlm-typed adapters
// ([Message.plainText], [openApiToolFor]) just extract primitives from the
// litertlm types and delegate, and are covered indirectly by compileDebugKotlin +
// the 2.6b on-device smoke.
// ===========================================================================

/** Lenient JSON used by the mappers (tolerant of extra per-tool schema fields). */
private val mapperJson = Json { ignoreUnknownKeys = true }

// --- Pure cores (primitive-typed → JVM-unit-testable under JDK 17) -----------

/**
 * Concatenate streamed assistant TEXT: the [texts] (already extracted from the
 * message's [Content.Text] pieces, non-text content dropped) joined in order.
 */
fun plainTextOf(texts: List<String>): String = texts.joinToString("")

/**
 * Resolve a [SamplerSettings] to the EFFECTIVE (topK, topP, temperature) trio that
 * would be passed to litertlm's `SamplerConfig`, OR null when no override is set.
 *
 * PURE (primitives only) so it is JVM-unit-testable under JDK 17 (constructing a
 * litertlm `SamplerConfig` here would throw UnsupportedClassVersionError on the host
 * test JVM; see the Mappers header). Any null field is filled with the matching
 * [LiteRtEngine] default constant; an all-null input returns null (-> the engine
 * omits `samplerConfig`). Returned topP/temperature are Double (litertlm's type).
 */
fun resolveSampler(
    topK: Int?,
    topP: Float?,
    temperature: Float?,
): Triple<Int, Double, Double>? {
    if (topK == null && topP == null && temperature == null) return null
    return Triple(
        topK ?: LiteRtEngine.DEFAULT_SAMPLER_TOP_K,
        (topP ?: LiteRtEngine.DEFAULT_SAMPLER_TOP_P).toDouble(),
        (temperature ?: LiteRtEngine.DEFAULT_SAMPLER_TEMPERATURE).toDouble(),
    )
}

/**
 * The app-side native-loop STEP-CAP decision (Task W3 hardening, defense-in-depth):
 * given how many tool calls a single [LiteRtEngine.generateWithToolsNative] turn has
 * ALREADY executed ([callCount], 1-based for the call being evaluated) and the cap
 * [max] ([LiteRtEngine.MAX_NATIVE_TOOL_CALLS]), is this call OVER the cap and thus
 * to be refused (return a terminal "step limit reached" result WITHOUT running the
 * tool body)? True when `callCount > max` -- i.e. the first [max] calls run normally
 * and the (max+1)-th onward are refused, pushing the model to its final answer.
 *
 * This is a SOFT cap UNDER the litertlm engine's own recurring-tool-call guard (the
 * primary bound); it only matters if that engine-side bound were ever weakened or
 * absent. PURE (primitives only) so the decision is JVM-unit-testable under JDK 17;
 * the enforcement inside [nativeOpenApiToolFor]'s `execute` is device/compile-verified
 * (see the Mappers header).
 */
fun overCap(callCount: Int, max: Int): Boolean = callCount > max

/**
 * The vision GATING decision (Task W4): does this model accept image input, i.e.
 * should the engine configure a `visionBackend` and should [LiteRtEngine.generateWithImage]
 * be allowed to run? Purely [supportImage] today (a one-liner now, but kept as a
 * named pure function so the gate is one testable place and a future device/
 * accelerator nuance can land here without touching the engine glue).
 *
 * PURE (primitives only) so it is JVM-unit-testable under JDK 17 (constructing a
 * litertlm `Backend` here would throw UnsupportedClassVersionError on the host test
 * JVM; see the Mappers header). The thin litertlm-typed adapter is [visionBackendFor].
 */
fun visionEnabled(supportImage: Boolean): Boolean = supportImage

/**
 * The graceful GPU-vision degrade DECISION (W4 follow-up — the Important fix):
 * after the engine's first [com.google.ai.edge.litertlm.Engine.initialize] THROWS,
 * should [LiteRtEngine.load] retry ONCE building the engine WITHOUT a
 * `visionBackend` (text-only)? Yes IFF this is a vision bundle ([supportImage])
 * AND a `visionBackend` was actually set on the failed config ([visionWasSet]) —
 * i.e. the failure could plausibly be the GPU vision backend on a GPU-less /
 * limited device, and there is a text-only fallback to fall back TO. A text-only
 * bundle (no visionBackend) has nothing to retry, so its init failure is real.
 *
 * The result: a vision-capable model on a device whose GPU can't init vision
 * still loads for TEXT/native (it just can't do image turns) instead of the
 * whole engine — and thus every text turn — failing. PURE (primitives only) so
 * the retry decision is JVM-unit-testable under JDK 17 (the initialize/retry
 * itself is framework/device-verified; see the Mappers header).
 */
fun shouldRetryWithoutVision(supportImage: Boolean, visionWasSet: Boolean): Boolean =
    supportImage && visionWasSet

/**
 * Order a multimodal turn's [Content]s with the IMAGES FIRST, then the TEXT —
 * mirroring Edge Gallery's `runInference(images)` (it `contents.add` every image,
 * then adds the text AFTER). Returns `images + textContent` in that order.
 *
 * Generic over the content type [T] so it is PURE + JVM-unit-testable under JDK 17
 * with plain Strings (constructing a litertlm `Content` in a test would throw
 * UnsupportedClassVersionError; see the Mappers header). [LiteRtEngine.generateWithImage]
 * calls it with `T = Content`. The ORDER is the contract being tested; the litertlm
 * `Contents.of(...)` wrap around it is covered by compileDebugKotlin + the device smoke.
 */
fun <T> orderVisionContents(images: List<T>, textContent: T): List<T> = images + textContent

/**
 * Convert a tool-call `arguments` map (`Map<String, Any?>`, values may be
 * String / Number / Boolean / null / nested Map / List) into a kotlinx
 * [JsonObject], preserving scalar types (numbers/booleans are NOT quoted) and
 * recursing into nested maps/lists. This is the `args` carried verbatim through
 * [LlmEvent.ToolCall] → [FcLoop] → the BlackBox bridge.
 */
fun argsToJsonObject(args: Map<String, Any?>): JsonObject = buildJsonObject {
    for ((key, value) in args) {
        put(key, value.toJsonElement())
    }
}

private fun Any?.toJsonElement(): kotlinx.serialization.json.JsonElement = when (this) {
    null -> JsonNull
    is String -> JsonPrimitive(this)
    is Boolean -> JsonPrimitive(this)
    is Number -> JsonPrimitive(this)
    is Map<*, *> -> buildJsonObject {
        for ((k, v) in this@toJsonElement) {
            put(k.toString(), v.toJsonElement())
        }
    }
    is Iterable<*> -> buildJsonArray {
        for (item in this@toJsonElement) add(item.toJsonElement())
    }
    is Array<*> -> buildJsonArray {
        for (item in this@toJsonElement) add(item.toJsonElement())
    }
    // Unknown type: stringify so we never throw on an unexpected value shape.
    else -> JsonPrimitive(this.toString())
}

/**
 * Serialize the OpenAI/OpenAPI function-declaration JSON
 * `{"name", "description", "parameters": <json-schema>}` the model sees for a
 * tool, from its [name] / [description] / [parameters].
 *
 * TODO(2.6b verify): confirm the exact JSON shape `getToolDescriptionJsonString()`
 * expects against a real model on-device (the OpenAPI function-declaration format
 * is the documented convention; lock it during the device smoke).
 */
fun toolDescriptionJson(name: String, description: String, parameters: JsonObject): String {
    val descriptionJson = buildJsonObject {
        put("name", name)
        put("description", description)
        put("parameters", parameters)
    }
    return mapperJson.encodeToString(JsonObject.serializer(), descriptionJson)
}

/**
 * The `OpenApiTool.execute` stub. NEVER invoked when `automaticToolCalling =
 * false` — the model's tool calls surface as [LlmEvent.ToolCall] and dispatch via
 * the BlackBox bridge ([FcLoop]), not LiteRT-LM's auto loop. Always throws.
 */
fun bridgeDispatchedStub(): Nothing =
    throw UnsupportedOperationException("dispatched via BlackBox bridge")

// --- Native tool-calling path (Task W3) --------------------------------------
//
// The NATIVE path (`generateWithToolsNative`, `automaticToolCalling = true`) lets
// the litertlm ENGINE drive the tool loop: it calls each [OpenApiTool.execute]
// itself, feeds the (String) result back, loops until a final answer, and signals
// `onDone`. This fixes the manual-path loop-repeat (we fed tool results as plain
// text + re-advertised tools, so the small model never saw a "done"). The result
// JSON the engine consumes (and feeds back into the model's context) uses Edge
// Gallery's shape: `{"status":"succeeded","result":<...>}` on success,
// `{"status":"failed","error":<...>}` on failure. These pure cores convert a
// dispatched [ToolResult] to that string and parse it back (for inline rendering),
// and are JVM-unit-testable under JDK 17 (primitives only; see the Mappers header).

/** The succeeded/failed status literals in the Gallery tool-result JSON shape. */
const val TOOL_STATUS_SUCCEEDED = "succeeded"
const val TOOL_STATUS_FAILED = "failed"

/**
 * Serialize a dispatched tool result to the JSON string the litertlm engine feeds
 * back to the model (Edge Gallery's shape):
 *  - [success] true  -> `{"status":"succeeded","result":<result|null>}`
 *  - [success] false -> `{"status":"failed","error":<result|null>}`
 *
 * [result] is the [ToolResult.result] [JsonElement] (carried VERBATIM - a string,
 * object, list, number, or null). On failure it is the error detail (our bridge /
 * actuator put the message there, e.g. "needs connection"). PURE (primitives only)
 * so it is JDK17-unit-testable; the thin adapter is [ToolResult.toResultJsonString].
 */
fun toResultJsonString(success: Boolean, result: kotlinx.serialization.json.JsonElement?): String {
    val obj = buildJsonObject {
        if (success) {
            put("status", TOOL_STATUS_SUCCEEDED)
            put("result", result ?: JsonNull)
        } else {
            put("status", TOOL_STATUS_FAILED)
            put("error", result ?: JsonNull)
        }
    }
    return mapperJson.encodeToString(JsonObject.serializer(), obj)
}

/**
 * Parse a Gallery-shaped tool-result JSON string ([toResultJsonString]'s output)
 * back into the [success] flag and `result`/`error` [JsonElement] payload, so the
 * native engine path can emit a faithful [LlmEvent.ToolOutcome] for inline
 * rendering. `success` is `status == "succeeded"`; the payload is `result` on
 * success or `error` on failure (null when absent). MALFORMED / non-object input
 * (a model or engine that returns a bare string) does NOT throw - it is surfaced
 * as `success=false` with the raw text as the payload, so rendering never crashes.
 * PURE so it is JDK17-unit-testable. Returns `(success, payload)`.
 */
fun parseResultJsonString(json: String): Pair<Boolean, kotlinx.serialization.json.JsonElement?> {
    val obj = runCatching { mapperJson.parseToJsonElement(json) as? JsonObject }.getOrNull()
        ?: return false to JsonPrimitive(json) // not an object -> treat as a failure detail
    val success = (obj["status"] as? JsonPrimitive)?.contentOrNull == TOOL_STATUS_SUCCEEDED
    val payload = if (success) obj["result"] else (obj["error"] ?: obj["result"])
    return success to payload
}

// --- Thin litertlm-typed adapters (delegate to the pure cores) ---------------

/**
 * Concatenate the assistant TEXT from a streamed [Message]: all [Content.Text]
 * pieces joined in order, non-text content (images/audio/tool-responses) ignored.
 * Matches the helper in the API doc; delegates to the testable [plainTextOf].
 */
fun Message.plainText(): String =
    plainTextOf(contents.contents.filterIsInstance<Content.Text>().map { it.text })

/**
 * Build an [OpenApiTool] for a discovered [ToolSchema]: its
 * [OpenApiTool.getToolDescriptionJsonString] returns [toolDescriptionJson] for the
 * schema and its [OpenApiTool.execute] is the [bridgeDispatchedStub] (never called
 * when `automaticToolCalling = false`).
 */
/**
 * Build a litertlm [SamplerConfig] from per-model [SamplerSettings], or null when
 * no override is set (the engine then omits `samplerConfig`). Thin adapter over the
 * testable [resolveSampler]; covered indirectly by compileDebugKotlin + the device
 * smoke (the host test JVM can't construct litertlm 0.13.1 types).
 */
fun SamplerSettings.toSamplerConfig(): SamplerConfig? {
    val (k, p, t) = resolveSampler(topK, topP, temperature) ?: return null
    return SamplerConfig(topK = k, topP = p, temperature = t)
}

/**
 * The litertlm-typed vision-backend selector (Task W4): the [Backend] to set on
 * [EngineConfig.visionBackend], or null to OMIT it (text-only — leaves the working
 * CPU text path untouched). Returns [Backend.GPU] when [visionEnabled] is true,
 * mirroring Edge Gallery, which forces `EngineConfig(visionBackend = GPU)` ("must
 * be GPU for Gemma 3n"); whether Gemma-4 E4B can do image input on CPU on a given
 * device is unknown and is verified on-device separately, so we follow Gallery's
 * proven GPU choice rather than guessing CPU.
 *
 * Thin adapter over the testable [visionEnabled]; covered indirectly by
 * compileDebugKotlin + the device smoke (the host test JVM can't construct a
 * litertlm 0.13.1 `Backend` — see the Mappers header).
 */
fun visionBackendFor(supportImage: Boolean): Backend? =
    if (visionEnabled(supportImage)) Backend.GPU() else null

fun openApiToolFor(schema: ToolSchema): OpenApiTool {
    val descriptionString = toolDescriptionJson(schema.name, schema.description, schema.parameters)
    return object : OpenApiTool {
        override fun getToolDescriptionJsonString(): String = descriptionString
        override fun execute(paramsJsonString: String): String = bridgeDispatchedStub()
    }
}

/**
 * Build an [OpenApiTool] for a NATIVE (engine-driven) tool (Task W3). Its
 * description JSON is the SAME [toolDescriptionJson] the model sees on the manual
 * path, but its [OpenApiTool.execute] is REAL (not the [bridgeDispatchedStub]): the
 * litertlm engine calls it with the model's argument JSON, and it:
 *  1. ENFORCES the app-side step cap: it increments the shared per-turn
 *     [callCount] and, if [overCap] (count > [LiteRtEngine.MAX_NATIVE_TOOL_CALLS]),
 *     emits a single ToolOutcome marker and returns a terminal failed result
 *     ("step limit reached") WITHOUT running the side-effecting tool body -- so a
 *     misbehaving model is pushed to its final answer instead of looping forever.
 *  2. emits [LlmEvent.ToolCall] (the args parsed to a [JsonObject]) via [emit],
 *  3. runs the [NativeTool.execute] body (which returns the Gallery-shaped result
 *     JSON string the engine feeds back to the model),
 *  4. parses that string ([parseResultJsonString]) and emits [LlmEvent.ToolOutcome]
 *     for inline rendering, then
 *  5. returns the result string to the engine.
 *
 * [callCount] is the shared per-INVOCATION counter from [generateWithToolsNative]
 * (one [AtomicInteger] across all of a turn's tool wrappers; resets each turn). The
 * cap is a SOFT bound UNDER litertlm's own recurring-tool-call guard (defense-in-
 * depth): on the happy path the engine's `onDone` ends the loop long before it.
 *
 * [emit] is the channel sink supplied by [generateWithToolsNative]'s [callbackFlow]
 * (the events bridge from the engine's synchronous execute thread to the Flow).
 * NEVER throws out of `execute`: any failure to PARSE args/result is reported as a
 * failed ToolOutcome and a failed result string, so the engine's loop continues
 * rather than crashing the native turn.
 */
internal fun nativeOpenApiToolFor(
    nativeTool: NativeTool,
    callCount: AtomicInteger,
    toolResultChars: AtomicInteger,
    emit: (LlmEvent) -> Unit,
): OpenApiTool {
    val schema = nativeTool.schema
    val descriptionString = toolDescriptionJson(schema.name, schema.description, schema.parameters)
    return object : OpenApiTool {
        override fun getToolDescriptionJsonString(): String = descriptionString
        override fun execute(paramsJsonString: String): String {
            // 1. App-side soft gates (defense-in-depth), checked BEFORE running the
            //    side-effecting body. EITHER triggers the SAME terminal "stop and
            //    answer" path so the model wraps up with what it has -- never errors:
            //      a) STEP cap: count THIS call (1-based); over MAX_NATIVE_TOOL_CALLS?
            //      b) per-turn tool-RESULT budget (snapshot-ledger Task 9): have the
            //         (trimmed) results fed back this turn exceeded the char budget?
            //    Both are layered UNDER the engine's own recurring-tool-call guard (we
            //    don't fight the engine; we just stop executing tool bodies past either
            //    bound). The result-budget gate reads the shared accumulator updated in
            //    step 5 below; it gates the NEXT call once the budget is spent.
            if (overCap(callCount.incrementAndGet(), LiteRtEngine.MAX_NATIVE_TOOL_CALLS) ||
                overTurnBudget(toolResultChars.get(), MAX_TURN_TOOL_RESULT_CHARS)) {
                emit(LlmEvent.ToolOutcome(schema.name, ToolResult(success = false, result = STEP_LIMIT_PAYLOAD)))
                return STEP_LIMIT_RESULT_JSON
            }
            // 2. ToolCall (best-effort arg parse; malformed args -> empty object).
            val argsObj = runCatching { mapperJson.parseToJsonElement(paramsJsonString) as? JsonObject }
                .getOrNull() ?: JsonObject(emptyMap())
            emit(LlmEvent.ToolCall(schema.name, argsObj))
            // 3. Run the dispatch body (returns the Gallery-shaped result JSON).
            val rawResultJson = nativeTool.execute(paramsJsonString)
            // 4. TRIM the INNER result VALUE, not the JSON wrapper (snapshot-ledger
            //    Task 9 + fix): trimming the whole `{"status":...,"result":"..."}`
            //    string cuts it mid-JSON → invalid JSON → parseResultJsonString reads
            //    it as "failed" (this broke every big-result tool, e.g. search_snapshots
            //    at ~23K chars, while tiny ones like roll_dice slipped under the cap).
            //    So parse the VALID raw JSON, trim the payload string, re-wrap → the
            //    engine always receives valid JSON. Accumulate the trimmed length for
            //    the per-turn budget gate (step 1b) read on the NEXT call.
            val (ok, rawPayload) = parseResultJsonString(rawResultJson)
            val payload = trimResultPayload(rawPayload, MAX_TOOL_RESULT_CHARS)
            val resultJson = toResultJsonString(ok, payload)
            toolResultChars.addAndGet(resultJson.length)
            // 5. ToolOutcome for inline rendering (trimmed payload + faithful success).
            emit(LlmEvent.ToolOutcome(schema.name, ToolResult(success = ok, result = payload)))
            // 6. Return the re-wrapped (valid, trimmed) result string to the engine.
            return resultJson
        }
    }
}

/** The "stop and answer" message returned/emitted when the native step cap is exceeded. */
private const val STEP_LIMIT_MESSAGE = "step limit reached -- stop and give your final answer"

/** The terminal failed-result JSON the engine receives for any tool call past the cap. */
private val STEP_LIMIT_RESULT_JSON: String = toResultJsonString(false, JsonPrimitive(STEP_LIMIT_MESSAGE))

/** The ToolOutcome payload emitted (once per over-cap call) for inline visibility of the cap. */
private val STEP_LIMIT_PAYLOAD = JsonPrimitive(STEP_LIMIT_MESSAGE)
