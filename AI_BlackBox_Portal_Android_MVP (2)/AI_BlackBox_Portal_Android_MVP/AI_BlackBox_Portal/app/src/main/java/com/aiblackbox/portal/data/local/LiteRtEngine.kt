package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolSchema
import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Content
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import com.google.ai.edge.litertlm.Message
import com.google.ai.edge.litertlm.OpenApiTool
import com.google.ai.edge.litertlm.tool
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.File

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
 */
class LiteRtEngine(
    private val modelFile: File,
    private val delegate: String = "cpu",
    private val cacheDir: String? = null,
    private val nativeLibraryDir: String? = null,
    private val maxTokens: Int = DEFAULT_MAX_TOKENS,
) : LocalLlm, ToolCallingLlm {

    @Volatile
    private var engine: Engine? = null
    // Absolute path of the model the current engine was initialized for, so a
    // repeat load() of the SAME bundle is a no-op (initialize() is ~10s).
    @Volatile
    private var loadedModelPath: String? = null

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

            val config = EngineConfig(
                modelPath = targetPath,
                backend = backendFor(delegate),
                cacheDir = cacheDir,
                // Explicit context window — the litertlm default (null) is only
                // 4096 tokens (device-confirmed: "Input token ids are too long:
                // 4292 >= 4096"), too small for the agent's per-turn prompt.
                maxNumTokens = maxTokens,
            )
            val built = Engine(config)
            built.initialize() // ~10s; we're on Dispatchers.IO.
            engine = built
            loadedModelPath = targetPath
        }
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
        val conversation = eng.createConversation() // no tools
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
        val config = ConversationConfig(
            tools = tools.map { tool(openApiToolFor(it)) },
            automaticToolCalling = false,
        )
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
        ): LiteRtEngine = LiteRtEngine(
            modelFile = modelFile,
            delegate = delegate,
            cacheDir = context.cacheDir.absolutePath,
            nativeLibraryDir = context.applicationInfo.nativeLibraryDir,
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
fun openApiToolFor(schema: ToolSchema): OpenApiTool {
    val descriptionString = toolDescriptionJson(schema.name, schema.description, schema.parameters)
    return object : OpenApiTool {
        override fun getToolDescriptionJsonString(): String = descriptionString
        override fun execute(paramsJsonString: String): String = bridgeDispatchedStub()
    }
}
