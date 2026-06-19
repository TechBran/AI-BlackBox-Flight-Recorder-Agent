package com.aiblackbox.portal.data.remote

import android.content.Context
import android.util.Log
import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.LocalEngineHolder
import com.aiblackbox.portal.data.local.NativeTool
import com.aiblackbox.portal.data.local.NativeToolCallingLlm
import com.aiblackbox.portal.data.local.PhoneController
import com.aiblackbox.portal.data.local.ResidentTools
import com.aiblackbox.portal.data.local.ensureWarmEngine
import com.aiblackbox.portal.data.local.toResultJsonString
import com.aiblackbox.portal.overlay.AndroidPhoneController
import com.aiblackbox.portal.overlay.AutonomyMode
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.util.Collections
import java.util.UUID

/**
 * Runs a remote-control task on the on-device Gemma (control_phone). Implements
 * [RemoteTaskHandler] so [RemoteControlServer] can submit tasks + poll status. The
 * task drives the owner's OWN phone, at the owner's (remote) request via a frontier
 * model.
 *
 * Flow: submitTask -> `waking` (acquire the warm engine) -> `working` (engine-driven
 * native tool loop over the REMOTE-ALLOWLISTED device tools) -> `done`(result) |
 * `error`. No user is present, so the actuator runs YOLO (auto-approve confirms)
 * with the credential handoff DECLINED (passwords never proceed); non-allowlisted
 * tools are refused before dispatch. Cloud tools are NOT exposed remotely
 * (device-only). Auth/scope (Task 8) bounds WHO may submit.
 *
 * The public [constructor] wires production seams from a [Context]; the internal
 * constructor injects them so the runner is JVM-unit-testable without Android.
 */
class RemoteTaskRunner internal constructor(
    private val scope: CoroutineScope,
    private val engineProvider: () -> NativeToolCallingLlm?,
    private val phoneProvider: () -> PhoneController,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    // WAKE: load the engine on demand when the holder is cold (the "waking Gemma" work).
    // Defaults to the sync provider (no load) for tests that don't exercise warming.
    private val engineWarmer: suspend () -> NativeToolCallingLlm? = { engineProvider() },
) : RemoteTaskHandler {

    /** Production wiring: warm engine from the process holder; WAKE it on demand via
     *  [ensureWarmEngine] when cold; phone controller in the remote posture (YOLO;
     *  default confirm = auto-approve, credential handoff = auto-decline so passwords
     *  never proceed). */
    constructor(appContext: Context) : this(
        scope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
        engineProvider = { LocalEngineHolder.getOrNull() as? NativeToolCallingLlm },
        phoneProvider = { AndroidPhoneController.fromService(appContext, mode = { AutonomyMode.YOLO }) },
        engineWarmer = { ensureWarmEngine(appContext) },
    )

    // Bounded LRU (access-order): an actively-polled task stays warm while old
    // terminal entries are evicted past MAX_TASKS — no unbounded growth on a
    // long-lived foreground service. Synchronized for the listener's threads.
    private val tasks: MutableMap<String, RemoteStatus> = Collections.synchronizedMap(
        object : LinkedHashMap<String, RemoteStatus>(16, 0.75f, true) {
            override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, RemoteStatus>?): Boolean =
                size > MAX_TASKS
        },
    )

    // Serialize remote turns so two overlapping remote tasks don't race the one warm
    // engine. NOTE (v1): the foreground chat uses the SAME engine and does NOT share
    // this lock, so a remote task concurrent with active on-device chat could still
    // race native state — unlikely (the owner is remote when delegating) and a
    // device-validation watch-item; a shared engine lock is the follow-up.
    private val turnMutex = Mutex()

    /** Liveness for the device list: ready only when a warm engine is available. */
    override fun healthz(): Boolean = engineProvider() != null

    override fun taskStatus(taskId: String): RemoteStatus? = tasks[taskId]

    override fun submitTask(task: String, operator: String): String {
        val id = UUID.randomUUID().toString()
        tasks[id] = RemoteStatus(phase = "waking")
        scope.launch { runTask(id, task) }
        return id
    }

    private suspend fun runTask(id: String, task: String) {
        val acc = StringBuilder()
        var step = 0
        var faulted = false
        var started = false
        // One remote turn at a time (serialize concurrent remote submissions). The task
        // stays `waking` while the lock is held AND while the engine cold-loads (the WAKE),
        // then flips to `working` once it drives the engine. The `.catch` operator (mirroring
        // the chat loop) handles an engine fault WITHIN the flow — reliable under flowOn,
        // unlike a try/catch around collect — and preserves cancellation (catch ignores
        // CancellationException). Only a clean completion reaches `done`.
        try {
            turnMutex.withLock {
                val engine = engineWarmer()   // wake Gemma on demand if the holder is cold
                if (engine == null) {
                    tasks[id] = RemoteStatus(
                        phase = "error",
                        error = "no on-device model is installed, or it failed to load",
                    )
                    return@withLock
                }
                val phone = phoneProvider()
                started = true
                tasks[id] = RemoteStatus(phase = "working")
                engine.generateWithToolsNative(remotePrompt(task), buildRemoteDeviceTools(phone))
                    .catch { e ->
                        faulted = true
                        Log.w(TAG, "remote task failed (${e.javaClass.simpleName})")
                        tasks[id] = RemoteStatus(phase = "error", error = "task failed (${e.javaClass.simpleName})")
                    }
                    .flowOn(ioDispatcher)
                    .collect { event ->
                        when (event) {
                            is LlmEvent.TextDelta -> acc.append(event.text)
                            is LlmEvent.ToolCall -> {
                                step++
                                tasks[id] = RemoteStatus(phase = "working", step = step)
                            }
                            is LlmEvent.ToolOutcome -> { /* fed back to the model by the engine */ }
                        }
                    }
            }
            if (started && !faulted) {
                tasks[id] = RemoteStatus(phase = "done", result = acc.toString().ifBlank { "Done." })
            }
        } catch (e: Exception) {
            Log.w(TAG, "remote task failed (${e.javaClass.simpleName})")
            tasks[id] = RemoteStatus(phase = "error", error = "task failed (${e.javaClass.simpleName})")
        }
    }

    companion object {
        private const val TAG = "RemoteTaskRunner"
        /** Max retained per-task status entries before LRU eviction (bounded memory). */
        internal const val MAX_TASKS = 64
        private val ARGS_JSON = Json { ignoreUnknownKeys = true }

        private fun remotePrompt(task: String): String =
            "You are running a hands-free device task on the owner's phone, requested " +
            "remotely. Only safe device actions are available; high-consequence actions " +
            "are blocked. Use the tools to accomplish the task, then briefly report what " +
            "you did.\n\nTask: $task"

        /**
         * The REMOTE device tool set: every phone/intent action as a [NativeTool] whose
         * execute is GATED by [RemoteAllowlist] — allowlisted names dispatch through the
         * controller (the suspend dispatch bridged via runBlocking, Gallery pattern),
         * non-allowlisted names REFUSE before any dispatch with a clear result. Cloud
         * tools are deliberately omitted (device-only remote control). The execute
         * bodies reach the PhoneController ONLY. JVM-unit-testable with a fake controller.
         */
        fun buildRemoteDeviceTools(phone: PhoneController): List<NativeTool> =
            (ResidentTools.phoneActuators() + ResidentTools.intentActions()).map { schema ->
                NativeTool(
                    schema = schema,
                    execute = { argsJson ->
                        if (RemoteAllowlist.isAllowedRemote(schema.name)) {
                            runBlocking(Dispatchers.IO) {
                                phone.dispatch(schema.name, parseArgs(argsJson))
                            }.toResultJsonString()
                        } else {
                            toResultJsonString(
                                false,
                                JsonPrimitive("refused: '${schema.name}' is not allowed for remote control"),
                            )
                        }
                    },
                )
            }

        /** Tolerant parse of the engine's tool-args JSON string -> JsonObject ({} on failure). */
        internal fun parseArgs(argsJson: String): JsonObject =
            try {
                ARGS_JSON.decodeFromString(JsonObject.serializer(), argsJson.ifBlank { "{}" })
            } catch (e: Exception) {
                JsonObject(emptyMap())
            }
    }
}
