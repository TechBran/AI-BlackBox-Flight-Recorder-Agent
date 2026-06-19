package com.aiblackbox.portal.data.remote

import android.content.Context
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/**
 * Inbound remote-control listener for the `control_phone` feature (the BlackBox →
 * phone inversion). The phone hosts a tiny HTTP server on the tailnet so a frontier
 * model (via the BlackBox) can POST a task, then poll its status. Hosted by
 * [com.aiblackbox.portal.LocalModelService] for the lifetime of the foreground
 * service.
 *
 * Task 5 covers the listener + the PURE [routeRequest] routing (unit-tested with a
 * fake handler). The real work — waking Gemma and running an ALLOWLISTED task — is
 * delegated to a [RemoteTaskHandler] supplied by [remoteTaskHandlerFactory] (wired
 * in Task 6). Auth/scope (paired hub + operator) is Task 8.
 */

/** Default port the listener binds to. MUST match the backend control_phone
 *  executor's REMOTE_CONTROL_PORT. */
const val REMOTE_CONTROL_PORT = 8765

/**
 * Set by the remote-control feature (Task 6) to supply the task handler that
 * actually wakes Gemma + runs an allowlisted task. NULL = the inbound listener
 * stays OFF (there is nothing safe to run), which is the safe default until the
 * allowlist-enforcing runner is deliberately registered. Decouples the listener
 * (Task 5) from the runner (Task 6).
 */
@Volatile
var remoteTaskHandlerFactory: ((Context) -> RemoteTaskHandler)? = null

/** Status of one remote task, surfaced by GET /status/{id}. Field names match what
 *  the backend control_phone executor reads (phase / result / error). Terminal
 *  phases are EXACTLY `done` / `error` (the backend treats any other phase as
 *  non-terminal and keeps polling). */
@Serializable
data class RemoteStatus(
    val phase: String,            // waking | working | done | error
    val result: String? = null,   // present when phase == done
    val error: String? = null,    // present when phase == error
    val step: Int? = null,        // optional progress hint while working
)

/** What the listener delegates real work to — implemented by the runner (Task 6). */
interface RemoteTaskHandler {
    /** Accept a task; return an opaque task id to poll. Must be non-blocking. */
    fun submitTask(task: String, operator: String): String
    /** Status for a task id, or null if unknown (-> 404). */
    fun taskStatus(taskId: String): RemoteStatus?
    /** Liveness for the device list. */
    fun healthz(): Boolean
}

/** A routed response: HTTP status + JSON body. A pure value (no NanoHTTPD types) so
 *  the routing logic is unit-testable on the JVM without binding a socket. */
data class RemoteResponse(val status: Int, val json: String)

// encodeDefaults left at false so null result/error/step are omitted from RemoteStatus
// on the wire (cleaner; the backend reads phase/result/error tolerantly).
private val JSON = Json { ignoreUnknownKeys = true }

@Serializable private data class TaskRequest(val task: String = "", val operator: String = "")
@Serializable private data class TaskAccepted(@SerialName("task_id") val taskId: String)
@Serializable private data class ErrorBody(val error: String)
@Serializable private data class HealthBody(val ok: Boolean)

/**
 * PURE request router: (method, path, body) + handler -> [RemoteResponse]. No
 * sockets, no Android — directly unit-testable. Routes:
 *   GET  /healthz      -> {"ok": <handler.healthz()>}
 *   POST /task         -> {"task_id": ...}    (400 if task missing/blank/bad JSON)
 *   GET  /status/{id}  -> RemoteStatus json   (404 if unknown id)
 * Known path + wrong method -> 405; anything else -> 404.
 */
fun routeRequest(method: String, path: String, body: String,
                 handler: RemoteTaskHandler): RemoteResponse {
    val m = method.uppercase()
    return when {
        path == "/healthz" && m == "GET" ->
            RemoteResponse(200, JSON.encodeToString(HealthBody(handler.healthz())))

        path == "/task" && m == "POST" -> {
            val req = try {
                JSON.decodeFromString<TaskRequest>(body.ifBlank { "{}" })
            } catch (e: Exception) {
                return RemoteResponse(400, JSON.encodeToString(ErrorBody("invalid JSON body")))
            }
            if (req.task.isBlank())
                RemoteResponse(400, JSON.encodeToString(ErrorBody("task required")))
            else
                RemoteResponse(200, JSON.encodeToString(
                    TaskAccepted(handler.submitTask(req.task.trim(), req.operator.trim()))))
        }

        path.startsWith("/status/") && m == "GET" -> {
            val st = handler.taskStatus(path.removePrefix("/status/"))
            if (st == null) RemoteResponse(404, JSON.encodeToString(ErrorBody("unknown task")))
            else RemoteResponse(200, JSON.encodeToString(st))
        }

        path == "/healthz" || path == "/task" || path.startsWith("/status/") ->
            RemoteResponse(405, JSON.encodeToString(ErrorBody("method not allowed")))

        else -> RemoteResponse(404, JSON.encodeToString(ErrorBody("not found")))
    }
}

/**
 * Embedded HTTP listener (NanoHTTPD). Binds to all interfaces; Tailscale (WireGuard)
 * encrypts the transport, and Task 8 adds the paired-hub + operator auth that is the
 * real blast-radius guard. The socket binding is device/compile-verified; the routing
 * it delegates to is unit-tested via [routeRequest].
 */
class RemoteControlServer(
    port: Int,
    private val handler: RemoteTaskHandler,
) : NanoHTTPD(port) {

    fun startServer() = start(SOCKET_READ_TIMEOUT, false)
    fun stopServer() = stop()

    override fun serve(session: IHTTPSession): Response {
        val method = session.method?.name ?: "GET"
        val body = if (method.equals("POST", ignoreCase = true)) readBody(session) else ""
        val routed = routeRequest(method, session.uri ?: "/", body, handler)
        return newFixedLengthResponse(statusOf(routed.status), "application/json", routed.json)
    }

    private fun readBody(session: IHTTPSession): String {
        val files = HashMap<String, String>()
        return try {
            session.parseBody(files)
            files["postData"] ?: ""
        } catch (e: Exception) {
            Log.w(TAG, "failed to read request body (${e.javaClass.simpleName})")
            ""
        }
    }

    private fun statusOf(code: Int): Response.IStatus =
        Response.Status.values().firstOrNull { it.requestStatus == code }
            ?: object : Response.IStatus {
                override fun getRequestStatus() = code
                override fun getDescription() = code.toString()
            }

    companion object {
        private const val TAG = "RemoteControlServer"
    }
}
