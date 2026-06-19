package com.aiblackbox.portal.data.remote

import android.content.Context
import android.util.Log
import com.aiblackbox.portal.data.local.LlmEvent
import com.aiblackbox.portal.data.local.LocalEngineHolder
import com.aiblackbox.portal.data.local.NativeTool
import com.aiblackbox.portal.data.local.NativeToolCallingLlm
import com.aiblackbox.portal.data.local.PhoneController
import com.aiblackbox.portal.data.local.ResidentTools
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
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

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
) : RemoteTaskHandler {

    /** Production wiring: warm engine from the process holder; phone controller in the
     *  remote posture (YOLO; default confirm = auto-approve, credential handoff =
     *  auto-decline so passwords never proceed). */
    constructor(appContext: Context) : this(
        scope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
        engineProvider = { LocalEngineHolder.getOrNull() as? NativeToolCallingLlm },
        phoneProvider = { AndroidPhoneController.fromService(appContext, mode = { AutonomyMode.YOLO }) },
    )

    private val tasks = ConcurrentHashMap<String, RemoteStatus>()

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
        val engine = engineProvider()
        if (engine == null) {
            tasks[id] = RemoteStatus(
                phase = "error",
                error = "on-device model is not loaded yet; try again shortly",
            )
            return
        }
        val phone = try {
            phoneProvider()
        } catch (e: Exception) {
            tasks[id] = RemoteStatus(
                phase = "error",
                error = "phone control unavailable (${e.javaClass.simpleName})",
            )
            return
        }
        tasks[id] = RemoteStatus(phase = "working")
        val acc = StringBuilder()
        try {
            engine.generateWithToolsNative(remotePrompt(task), buildRemoteDeviceTools(phone))
                .flowOn(ioDispatcher)
                .catch { e -> throw e }   // surface to the catch below; never swallow
                .collect { event ->
                    if (event is LlmEvent.TextDelta) acc.append(event.text)
                }
            tasks[id] = RemoteStatus(phase = "done", result = acc.toString().ifBlank { "Done." })
        } catch (e: Exception) {
            Log.w(TAG, "remote task failed (${e.javaClass.simpleName})")
            tasks[id] = RemoteStatus(phase = "error", error = "task failed (${e.javaClass.simpleName})")
        }
    }

    companion object {
        private const val TAG = "RemoteTaskRunner"
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
