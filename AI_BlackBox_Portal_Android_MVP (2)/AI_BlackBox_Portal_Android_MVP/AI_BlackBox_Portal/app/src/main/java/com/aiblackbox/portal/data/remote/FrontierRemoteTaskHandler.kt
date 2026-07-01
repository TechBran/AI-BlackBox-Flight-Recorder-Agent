package com.aiblackbox.portal.data.remote

import android.content.Context
import android.util.Log
import java.util.Collections
import java.util.UUID

/**
 * [RemoteTaskHandler] backed by the CLOUD (frontier) brain instead of the on-device
 * Gemma. Where [RemoteTaskRunner] WAKES Gemma and runs the ReAct loop ON the phone,
 * this handler makes the device a provider-agnostic "thin hands" endpoint (research
 * §5.5 decision #1, Path B): the phone streams observation UP over the existing
 * Tailscale 8765 channel and a server-side frontier loop (Gemini 3.5 Flash
 * `environment:'mobile'` first) is the brain, streaming actions back DOWN over the M0
 * streaming channel (SSE `/stream/{taskId}` + `POST /action`). No on-device inference.
 *
 * **Same seam as Gemma — no socket rebind.** It implements the identical, already-generic
 * [RemoteTaskHandler] interface, so it slots into the SAME [RemoteTaskHandlerHolder]:
 * whatever component owns provider selection (M7) publishes it via
 * [RemoteTaskHandlerHolder.set] (or registers a [remoteTaskHandlerFactory] producing it),
 * and [com.aiblackbox.portal.NotificationListenerFgs]'s single [RemoteControlServer] reads
 * it PER REQUEST via `RemoteTaskHandlerHolder.current()`. Swapping frontier↔Gemma needs
 * NO rebind of [REMOTE_CONTROL_PORT]. Its `(Context) -> RemoteTaskHandler` production
 * constructor matches the factory type exactly, mirroring [RemoteTaskRunner].
 *
 * **M0 SCAFFOLD.** The actual HTTP bridge to the Orchestrator cloud loop is M2 and the
 * actuator binding is M1; here the interface is implemented correctly, the structure is
 * in place (bounded per-task status store mirroring [RemoteTaskRunner]), and `submitTask`
 * records a well-formed "not yet wired" status so the holder can hold this handler and
 * every existing route keeps working. See the TODO(M1/M2) markers for the downstream
 * seams.
 */
class FrontierRemoteTaskHandler internal constructor(
    // TODO(M2): the tailnet base URL of the BlackBox Orchestrator hosting the frontier
    //   loop (run_frontier_loop / control_device). Injected here so the handler is
    //   JVM-unit-testable without Android once the bridge lands. Empty = unresolved.
    private val orchestratorBaseUrl: String,
    // TODO(M2): inject the streaming HTTP bridge (okhttp) to the Orchestrator loop +
    //   the /stream observation pump + /action relay to the actuators (M1) here.
) : RemoteTaskHandler {

    /**
     * Production wiring from a [Context] — the shape the [remoteTaskHandlerFactory] /
     * [RemoteTaskHandlerHolder] seam expects (identical to [RemoteTaskRunner]).
     * TODO(M2): resolve [orchestratorBaseUrl] + auth from the attestation registry /
     * config (the box's tailnet host) instead of the empty placeholder.
     */
    constructor(appContext: Context) : this(orchestratorBaseUrl = "")

    // Bounded LRU (access-order) of per-task status — mirrors RemoteTaskRunner: an
    // actively-polled task stays warm while old terminal entries evict past MAX_TASKS,
    // so a long-lived foreground service never grows unbounded. Synchronized for the
    // listener's request threads.
    private val tasks: MutableMap<String, RemoteStatus> = Collections.synchronizedMap(
        object : LinkedHashMap<String, RemoteStatus>(16, 0.75f, true) {
            override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, RemoteStatus>?): Boolean =
                size > MAX_TASKS
        },
    )

    /**
     * Liveness for the device list.
     * TODO(M2): probe the Orchestrator frontier loop's reachability (box tailnet host up
     * + a provider configured) instead of a constant. For the M0 scaffold the cloud
     * bridge is not wired, so this reports not-ready — honest for the device list while
     * the loop is unimplemented; [taskStatus]/[submitTask] still respond without error so
     * a holder swap frontier↔Gemma does not break any route.
     */
    override fun healthz(): Boolean = false

    override fun taskStatus(taskId: String): RemoteStatus? = tasks[taskId]

    override fun submitTask(task: String, operator: String): String {
        val id = UUID.randomUUID().toString()
        // TODO(M2): POST the task to the Orchestrator frontier loop
        //   (run_frontier_loop / the control_device tool) for THIS device and open the
        //   M0 streaming channel so the cloud brain drives it:
        //     observation↑  (UiTreeReader tree + DeviceCapabilities + optional silent
        //                     AccessibilityService.takeScreenshot()) over /stream, then
        //     action↓       (POST /action → AndroidPhoneController → Actuators /
        //                     IntentActuator, wired in M1),
        //   updating tasks[id] (waking → working → done/error) from the streamed
        //   action_result / loop-completion events. Reuse the RemoteTaskRunner phase
        //   vocabulary (waking | working | done | error) so the backend poller is
        //   unchanged.
        Log.i(TAG, "submitTask(op=$operator): frontier cloud loop not yet wired (M2 scaffold)")
        tasks[id] = RemoteStatus(
            phase = "error",
            error = "frontier cloud brain not yet wired (M2 scaffold)",
        )
        return id
    }

    companion object {
        private const val TAG = "FrontierRemoteTaskHandler"
        /** Max retained per-task status entries before LRU eviction (bounded memory). */
        internal const val MAX_TASKS = 64
    }
}
