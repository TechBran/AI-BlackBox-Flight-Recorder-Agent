package com.aiblackbox.portal.data.remote

import android.content.Context
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.ByteArrayInputStream

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

/**
 * Process-level seam between the model service (the PRODUCER of the Gemma-backed
 * [RemoteTaskHandler]) and the listener FGS (the single OWNER of the control-port
 * socket). [com.aiblackbox.portal.LocalModelService] PUBLISHES the live
 * [RemoteTaskRunner] here when a model loads and CLEARS it on stop;
 * [com.aiblackbox.portal.NotificationListenerFgs]'s [RemoteControlServer] reads it via
 * its `handlerProvider` PER REQUEST, falling back to [NoopRemoteTaskHandler] when
 * absent. This decouples the single socket binding (always the FGS) from the
 * model-dependent task runner — `/notify` + `/healthz` stay up model-free while
 * `/task` + `/status` light up exactly when Gemma is resident, with NO socket rebind.
 */
object RemoteTaskHandlerHolder {
    @Volatile
    private var handler: RemoteTaskHandler? = null

    /** Publish the Gemma-backed handler (model service, on warm/listener start). */
    fun set(h: RemoteTaskHandler) { handler = h }

    /** Clear it (model service stop/destroy) — `/task` falls back to the no-op. */
    fun clear() { handler = null }

    /** The live handler, or [NoopRemoteTaskHandler] when no model service is hosting. */
    fun current(): RemoteTaskHandler = handler ?: NoopRemoteTaskHandler
}

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

/**
 * The MESSAGE-KIND discriminator values (`msg`) for the M0 bidirectional streaming control
 * channel (research §5.5 decision #8). The frontier-driven loop carries three message kinds
 * on the wire: `observation` (device state UP: a11y tree + capability + optional screenshot),
 * `action` (frontier decision DOWN) and `action_result` (outcome UP).
 *
 * IMPORTANT (I2): `msg` (the MESSAGE kind) is a DIFFERENT key from an action's `type`. Inside
 * an `action` frame, `type` is the ACTION-VARIANT discriminator (element_click / element_set_text
 * / coordinate_tap / coordinate_swipe / global_action / intent / open_app / scroll — see
 * `docs/schema/action.json`). The two never collide: the message frame carries `msg:"action"`
 * while its nested action payload keeps its own `type`. These constants are the `msg` values and
 * are kept in lock-step with the `msg` const in the `docs/schema` JSON schemas.
 */
object WireMessageType {
    const val OBSERVATION = "observation"
    const val ACTION = "action"
    const val ACTION_RESULT = "action_result"
}

/**
 * (M0 scaffold) The `action` frame pushed DOWN over `POST /action` — the frontier model's
 * decision for a task. The MESSAGE-KIND discriminator is [msg] (const "action", I2), a
 * DIFFERENT key from the nested action payload's `type` (element_click / … — see
 * docs/schema/action.json), so the two never collide. `kind` is the M0 placeholder for that
 * nested action-variant `type`; the concrete params are intentionally left opaque at M0 (the
 * real action.json payload + actuator binding land in M1). `operator` scopes the action to
 * the device's bound operator via [authorize], mirroring `/task`; `task_id` correlates it to
 * a submitted task.
 */
@Serializable
data class ActionEnvelope(
    val msg: String = WireMessageType.ACTION,
    @SerialName("task_id") val taskId: String = "",
    val operator: String = "",
    val kind: String = "",
)

/**
 * The `action_result` frame returned UP for a dispatched action. Conforms to
 * docs/schema/action_result.json: `{msg:"action_result", success, error?, detail?, observation?}`.
 * [success]/[error]/[detail] carry the real actuator outcome (M1.3); the optional follow-on
 * [observation] rides here when the dispatcher embeds fresh screen state (else the loop pulls the
 * next observation over `/stream`). `error` is present only for a genuine failure (a user decline
 * / credential handoff is `success=false` with a benign [detail] and NO error). The listener that
 * has NO actuator dispatcher wired still returns `success=false, error="not_wired"` — the honest
 * handler-less state (M0 README decision 1). Serialized with the wire encoder
 * (explicitNulls=false) so null [error]/[detail]/[observation] are omitted and it stays conforming.
 */
@Serializable
data class ActionResultEnvelope(
    val msg: String = WireMessageType.ACTION_RESULT,
    val success: Boolean = false,
    val error: String? = null,
    val detail: String? = null,
    val observation: Observation? = null,
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

/**
 * No-op [RemoteTaskHandler] so the listener is constructable MODEL-FREE (MN.4). With
 * no on-device Gemma installed there is nothing to run: `/healthz` reports not-ready,
 * a `/task` submit yields an immediately-`error` status, and `/status` is always
 * unknown (404). `/notify` is independent of this handler, so notifications still post
 * on a device that has never installed a model. [LocalModelService] swaps in the real
 * [RemoteTaskRunner] (via the shared handler holder) once a model loads.
 */
object NoopRemoteTaskHandler : RemoteTaskHandler {
    override fun submitTask(task: String, operator: String): String = "no-model"
    override fun taskStatus(taskId: String): RemoteStatus? =
        if (taskId == "no-model")
            RemoteStatus(phase = "error", error = "no on-device model is installed")
        else null
    override fun healthz(): Boolean = false
}

/**
 * What a `POST /notify` ultimately invokes — posting a REAL system notification with
 * NO model/LLM in the path (MN.4). Implemented by [NotificationListenerFgs] over
 * [com.aiblackbox.portal.BlackBoxNotificationManager.showTaskNotification]. Pure
 * interface so [routeRequest] stays JVM-unit-testable with a fake. The `notifId` is
 * mapped to a stable (tag, id) by the implementation so retries COLLAPSE instead of
 * stacking.
 */
fun interface Notifier {
    /** Post (or re-post, idempotently keyed on [notifId]) a system notification. */
    fun postNotification(title: String, body: String, category: String, operator: String, notifId: String)
}

/** A routed response: HTTP status + JSON body. A pure value (no NanoHTTPD types) so
 *  the routing logic is unit-testable on the JVM without binding a socket. */
data class RemoteResponse(val status: Int, val json: String)

// encodeDefaults left at false so null result/error/step are omitted from RemoteStatus
// on the wire (cleaner; the backend reads phase/result/error tolerantly).
private val JSON = Json { ignoreUnknownKeys = true }

// Wire-envelope encoder for the M0 streaming channel (observation/action/action_result):
// encodeDefaults=true so the `type` discriminator is ALWAYS emitted (it equals its
// default) — the discriminator is the point of the schema alignment; explicitNulls=false
// still drops an absent optional (e.g. a null `detail`).
private val WIRE_JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true; explicitNulls = false }

@Serializable private data class TaskRequest(val task: String = "", val operator: String = "")
@Serializable private data class TaskAccepted(@SerialName("task_id") val taskId: String)
@Serializable private data class ErrorBody(val error: String)
@Serializable private data class HealthBody(val ok: Boolean)
// No default on `ok` so it is always emitted (the JSON instance has encodeDefaults=false,
// which would otherwise drop a defaulted-true field from the wire).
@Serializable private data class OkBody(val ok: Boolean)

/** Inbound `/notify` payload from the backend notification bus. `body` may be EMPTY
 *  for a metadata-only cross-operator push (title + category only). `notif_id` is the
 *  bus's idempotency key — retries reuse it so the notification COLLAPSES. */
@Serializable private data class NotifyRequest(
    val title: String = "",
    val body: String = "",
    val category: String = "",
    val operator: String = "",
    @SerialName("notif_id") val notifId: String = "",
)

/**
 * PURE request router: (method, path, body) + handler -> [RemoteResponse]. No
 * sockets, no Android — directly unit-testable. Routes:
 *   GET  /healthz      -> {"ok": <handler.healthz()>}
 *   POST /task         -> {"task_id": ...}    (400 if task missing/blank/bad JSON)
 *   GET  /status/{id}  -> RemoteStatus json   (404 if unknown id)
 *   POST /notify       -> {"ok": true}        (MN.4; 400 bad JSON, 503 if no notifier)
 * Known path + wrong method -> 405; anything else -> 404.
 *
 * NOTE: POST /action (the frontier action↓ channel) is NOT routed here — it dispatches
 * through the live actuators (a suspend, controller-backed hop), so it is handled by the
 * suspend [handleActionRequest] and invoked from [RemoteControlServer.serve] (like the
 * GET /stream/{id} observation↑ SSE half). Neither can be a pure fixed-length
 * [RemoteResponse] from this synchronous router.
 *
 * [notifier] is the MODEL-FREE notification poster (MN.4). It is defaulted to null so
 * the existing 4-arg callers/tests are unaffected; a null notifier makes `/notify`
 * return 503 (the listener was constructed without a poster — should not happen in
 * production, where [NotificationListenerFgs] always supplies one).
 */
fun routeRequest(method: String, path: String, body: String,
                 handler: RemoteTaskHandler, notifier: Notifier? = null,
                 killSwitch: RemoteKillSwitch? = null): RemoteResponse {
    val m = method.uppercase()
    return when {
        path == "/healthz" && m == "GET" ->
            RemoteResponse(200, JSON.encodeToString(HealthBody(handler.healthz())))

        // (M8.2) Global incident kill. Operator-scope is enforced UPSTREAM in [authorize]
        // (POST body `operator` must equal the device's bound operator), so these routes only
        // execute the kill. `/kill/{taskId}` halts ONE task; `/kill-all` halts every in-flight
        // task for the (authorized) operator. Both return {ok, killed_count}. A null [killSwitch]
        // (never in production — the FGS wires [BusKillSwitch]) → 503.
        path.startsWith("/kill/") && m == "POST" -> {
            val taskId = path.removePrefix("/kill/").trim()
            if (taskId.isBlank())
                RemoteResponse(400, JSON.encodeToString(ErrorBody("task_id required")))
            else if (killSwitch == null)
                RemoteResponse(503, JSON.encodeToString(ErrorBody("kill switch unavailable")))
            else
                RemoteResponse(200, JSON.encodeToString(
                    KillBody(ok = true, killedCount = killSwitch.kill(taskId))))
        }

        path == "/kill-all" && m == "POST" -> {
            if (killSwitch == null)
                RemoteResponse(503, JSON.encodeToString(ErrorBody("kill switch unavailable")))
            else
                // The operator is the auth-verified body operator (== bound operator, checked
                // in authorize) — kill only THIS operator's in-flight tasks.
                RemoteResponse(200, JSON.encodeToString(
                    KillBody(ok = true, killedCount = killSwitch.killAll(extractOperator(body)))))
        }

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

        path == "/notify" && m == "POST" -> {
            val req = try {
                JSON.decodeFromString<NotifyRequest>(body.ifBlank { "{}" })
            } catch (e: Exception) {
                return RemoteResponse(400, JSON.encodeToString(ErrorBody("invalid JSON body")))
            }
            // Need at least a title OR a body to show something useful. (A metadata-only
            // push carries title + category with an EMPTY body — that is valid and the
            // notifier renders title/category only.)
            if (req.title.isBlank() && req.body.isBlank())
                return RemoteResponse(400, JSON.encodeToString(ErrorBody("title or body required")))
            if (notifier == null)
                return RemoteResponse(503, JSON.encodeToString(ErrorBody("notifier unavailable")))
            notifier.postNotification(
                title = req.title.trim(),
                body = req.body.trim(),
                category = req.category.trim(),
                operator = req.operator.trim(),
                notifId = req.notifId.trim(),
            )
            RemoteResponse(200, JSON.encodeToString(OkBody(ok = true)))
        }

        path.startsWith("/status/") && m == "GET" -> {
            val st = handler.taskStatus(path.removePrefix("/status/"))
            if (st == null) RemoteResponse(404, JSON.encodeToString(ErrorBody("unknown task")))
            else RemoteResponse(200, JSON.encodeToString(st))
        }

        path == "/healthz" || path == "/task" || path == "/notify" ||
            path.startsWith("/status/") || path.startsWith("/kill/") || path == "/kill-all" ->
            RemoteResponse(405, JSON.encodeToString(ErrorBody("method not allowed")))

        else -> RemoteResponse(404, JSON.encodeToString(ErrorBody("not found")))
    }
}

/**
 * (M8.2) The kill seam `POST /kill/{taskId}` + `POST /kill-all` invoke. Abstracted so the
 * routes are JVM-unit-testable with a fake and the production impl ([BusKillSwitch]) is the
 * process-wide [RemoteSessionBus].
 */
interface RemoteKillSwitch {
    /** Kill ONE task. Returns how many IN-FLIGHT sessions were aborted (0 or 1); the task is
     *  recorded killed either way, so every subsequent /action + /stream frame for it is refused. */
    fun kill(taskId: String): Int

    /** Kill ALL in-flight tasks for [operator]. Returns the count aborted (0 or 1 — the bus
     *  tracks one active session). Blank operator kills nothing (fail-closed). */
    fun killAll(operator: String): Int
}

/** Production [RemoteKillSwitch] over the global [RemoteSessionBus]. */
object BusKillSwitch : RemoteKillSwitch {
    override fun kill(taskId: String): Int = if (RemoteSessionBus.stop(taskId) != null) 1 else 0
    override fun killAll(operator: String): Int = RemoteSessionBus.stopAll(operator)
}

/**
 * (M8.3) The read seam `GET /telemetry/{taskId}` + `GET /telemetry/summary?operator=` query.
 * Abstracted so [buildTelemetryResponse] is JVM-unit-testable with a fake; production is
 * [BusTelemetryReader] over the process-wide [RemoteSessionTelemetry].
 */
interface TelemetryReader {
    fun stepsFor(taskId: String, operator: String): List<RemoteSessionTelemetry.Step>
    fun summary(operator: String): RemoteSessionTelemetry.Summary
}

/** Production [TelemetryReader] over the global [RemoteSessionTelemetry]. */
object BusTelemetryReader : TelemetryReader {
    override fun stepsFor(taskId: String, operator: String) =
        RemoteSessionTelemetry.stepsFor(taskId, operator)
    override fun summary(operator: String) = RemoteSessionTelemetry.summary(operator)
}

/** The `{ok, killed_count}` body for the kill routes. No defaults → both always emitted. */
@Serializable
private data class KillBody(val ok: Boolean, @SerialName("killed_count") val killedCount: Int)

/** The `GET /telemetry/{taskId}` body: the task's operator-scoped per-step records. */
@Serializable
data class TelemetryStepsBody(
    @SerialName("task_id") val taskId: String,
    val operator: String,
    val steps: List<RemoteSessionTelemetry.Step>,
)

// Telemetry encoder: encodeDefaults=true so every non-sensitive field is emitted.
private val TELEMETRY_JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true }

/**
 * (M8.3) PURE builder for the telemetry GETs. `/telemetry/summary` → the operator aggregate
 * (avg latency + success rate); `/telemetry/{taskId}` → that task's operator-scoped step list.
 * OPERATOR-SCOPED: [operator] is the auth-verified query operator (== bound operator, checked in
 * [authorize]); both reads filter by it, so one operator can never read another's telemetry. No
 * screen text / secrets are ever in the store, so the response is safe to serialize. Kept pure +
 * separate from the NanoHTTPD body so the shape is JVM-unit-testable.
 */
fun buildTelemetryResponse(path: String, operator: String, telemetry: TelemetryReader): RemoteResponse =
    when {
        path == "/telemetry/summary" ->
            RemoteResponse(200, TELEMETRY_JSON.encodeToString(telemetry.summary(operator)))
        path.startsWith("/telemetry/") -> {
            val taskId = path.removePrefix("/telemetry/").trim()
            if (taskId.isBlank())
                RemoteResponse(400, JSON.encodeToString(ErrorBody("task_id required")))
            else
                RemoteResponse(200, TELEMETRY_JSON.encodeToString(
                    TelemetryStepsBody(taskId, operator, telemetry.stepsFor(taskId, operator))))
        }
        else -> RemoteResponse(404, JSON.encodeToString(ErrorBody("not found")))
    }

/**
 * (M1.3) Handle POST /action: dispatch the frontier `action` frame in [body] through the
 * live actuators (via [dispatcher]) and return a schema-conforming `action_result`. This
 * is a SUSPEND, controller-backed hop (real dispatch → the accessibility service), so it
 * lives outside the pure synchronous [routeRequest] and is invoked (under `runBlocking`)
 * from [RemoteControlServer.serve].
 *
 * Transport framing (M0 semantics preserved):
 *  - non-POST → 405;
 *  - malformed JSON → 400;
 *  - blank/absent `task_id` → 400 (the correlation id the loop polls);
 *  - no [dispatcher] wired (a handler-less device) → 200 with `action_result{success:false,
 *    error:"not_wired"}` — the honest handler-less state (M0 README decision 1);
 *  - otherwise → 200 with the dispatched `action_result` (a malformed action VARIANT is a
 *    conforming `success:false` result, not a transport error — more useful to the loop).
 *
 * The `dispatcher` is JVM-unit-tested via [PhoneActionDispatcher] over a fake controller;
 * this shell adds only the framing above (tested via a fake [RemoteActionDispatcher]).
 * Operator-scope is enforced UPSTREAM in [authorize] (unchanged); this never re-checks it.
 */
suspend fun handleActionRequest(
    method: String,
    body: String,
    dispatcher: RemoteActionDispatcher?,
): RemoteResponse {
    if (method.uppercase() != "POST") {
        return RemoteResponse(405, JSON.encodeToString(ErrorBody("method not allowed")))
    }
    // The transport envelope carries the correlation task_id (+ operator, already
    // auth-checked). The action VARIANT (type + fields) is parsed inside the dispatcher.
    val env = try {
        JSON.decodeFromString<ActionEnvelope>(body.ifBlank { "{}" })
    } catch (e: Exception) {
        return RemoteResponse(400, JSON.encodeToString(ErrorBody("invalid JSON body")))
    }
    if (env.taskId.isBlank()) {
        return RemoteResponse(400, JSON.encodeToString(ErrorBody("task_id required")))
    }
    if (dispatcher == null) {
        // No actuator seam registered → honest handler-less state (not a scaffold ack).
        return RemoteResponse(
            200,
            WIRE_JSON.encodeToString(
                ActionResultEnvelope(
                    success = false,
                    error = "not_wired",
                    detail = "no action dispatcher registered on this device",
                ),
            ),
        )
    }
    val result = dispatcher.dispatch(body, env.taskId, env.operator)
    return RemoteResponse(200, WIRE_JSON.encodeToString(result))
}

/**
 * Source/scope auth for the inbound listener (Task 8): blast-radius scoping ON TOP of
 * the Tailscale perimeter. Returns a 403 [RemoteResponse] to REJECT, or null to allow.
 * PURE → JVM-unit-testable.
 *  - Every route: the caller's source IP must be on the tailnet (Tailscale CGNAT
 *    100.64.0.0/10 or the Tailscale IPv6 ULA) or loopback (same-device). A LAN /
 *    external caller that reached the 0.0.0.0-bound socket is rejected here.
 *  - POST /task additionally: the request's `operator` must match the device's bound
 *    operator (a different operator's hub cannot drive this device). Fail-closed: a
 *    blank bound operator rejects.
 *  - POST /action additionally: same operator-scope as /task (it actuates the device).
 *  - GET /stream/{id} additionally (I1): the observation stream will carry SCREEN CONTENTS
 *    in M1, so it must be operator-scoped too. A GET has no body, so the operator arrives as
 *    the `?operator=` query param (extracted in [RemoteControlServer.serve]) and must equal
 *    the bound operator. Fail-closed: a blank bound operator OR a blank/mismatched query
 *    operator rejects, so the channel is never cross-operator readable.
 *  - POST /notify additionally (MN.4, defense in depth): the request's `operator` must
 *    be one THIS device subscribed to. The check is delegated to [isSubscribed]
 *    (device-local DataStore allow-list at the call site). Default [isSubscribed]
 *    accepts everything, so a caller that does not supply the predicate keeps the
 *    tailnet-only posture (and the existing tests are unaffected). Fail-open by design:
 *    until a subscription is recorded the allow-list is empty == accept-all, so a fresh
 *    box still receives tailnet-sourced notifications.
 */
fun authorize(method: String, path: String, remoteIp: String,
              requestOperator: String, boundOperator: String,
              isSubscribed: (operator: String) -> Boolean = { true }): RemoteResponse? {
    if (!isTailnetSource(remoteIp))
        return RemoteResponse(403, JSON.encodeToString(ErrorBody("source not on tailnet")))
    val m = method.uppercase()
    if (path == "/task" && m == "POST") {
        if (boundOperator.isBlank() || requestOperator != boundOperator)
            return RemoteResponse(403, JSON.encodeToString(ErrorBody("operator not authorized for this device")))
    }
    // (M0) The frontier action↓ channel is a control channel like /task — scope it to the
    // device's bound operator so a different operator's hub cannot actuate this device.
    if (path == "/action" && m == "POST") {
        if (boundOperator.isBlank() || requestOperator != boundOperator)
            return RemoteResponse(403, JSON.encodeToString(ErrorBody("operator not authorized for this device")))
    }
    // (M8.2) The incident-kill routes are control channels like /action — scope them to the
    // device's bound operator so a foreign operator cannot kill THIS device's sessions. The
    // operator arrives in the POST body (extracted in serve()). Fail-closed: blank bound
    // operator OR mismatch → 403.
    if ((path.startsWith("/kill/") || path == "/kill-all") && m == "POST") {
        if (boundOperator.isBlank() || requestOperator != boundOperator)
            return RemoteResponse(403, JSON.encodeToString(ErrorBody("operator not authorized for this device")))
    }
    // (M8.3) The telemetry GETs carry per-session step data — scope them to the bound operator
    // (the ?operator= query param, extracted in serve()) so one operator can't read another's
    // telemetry. Fail-closed: blank bound operator OR blank/mismatched query operator → 403.
    if (path.startsWith("/telemetry") && m == "GET") {
        if (boundOperator.isBlank() || requestOperator != boundOperator)
            return RemoteResponse(403, JSON.encodeToString(ErrorBody("operator not authorized for this device")))
    }
    // (I1) The observation↑ stream will carry SCREEN CONTENTS in M1 — scope GET /stream/{id}
    // to the bound operator so it is never cross-operator readable. The operator arrives as
    // the ?operator= query param (a GET has no body), extracted in serve() as requestOperator.
    // Fail-closed: blank bound operator OR blank/mismatched query operator → 403. (A non-GET
    // /stream is NOT operator-gated here so it can fall through to the 405 method gate.)
    if (path.startsWith("/stream/") && m == "GET") {
        if (boundOperator.isBlank() || requestOperator != boundOperator)
            return RemoteResponse(403, JSON.encodeToString(ErrorBody("operator not authorized for this device")))
    }
    if (path == "/notify" && m == "POST") {
        if (!isSubscribed(requestOperator))
            return RemoteResponse(403, JSON.encodeToString(ErrorBody("device not subscribed for this operator")))
    }
    return null
}

/** True iff [ip] is a Tailscale tailnet address (CGNAT 100.64.0.0/10 or the Tailscale
 *  IPv6 ULA fd7a:115c:a1e0::/48) or loopback (same-device). PURE. */
fun isTailnetSource(ip: String): Boolean {
    val a = ip.trim().lowercase()
    if (a.isEmpty()) return false
    if (a == "::1" || a.startsWith("127.")) return true        // loopback (same-device)
    if (a.startsWith("fd7a:115c:a1e0")) return true            // Tailscale IPv6 ULA
    val octets = a.split(".")                                  // IPv4 CGNAT 100.64.0.0/10
    if (octets.size == 4) {
        val o0 = octets[0].toIntOrNull()
        val o1 = octets[1].toIntOrNull()
        if (o0 == 100 && o1 != null && o1 in 64..127) return true
    }
    return false
}

/** Tolerant extract of the `operator` field from a JSON body ("" if absent/malformed).
 *  Both /task and /notify carry an `operator` field at the top level, so a single
 *  tolerant decode (TaskRequest reads `operator`, ignoring the other /notify keys via
 *  ignoreUnknownKeys) serves both. */
internal fun extractOperator(body: String): String =
    try { JSON.decodeFromString<TaskRequest>(body.ifBlank { "{}" }).operator } catch (e: Exception) { "" }

/**
 * (I1) PURE method gate for the observation stream (`/stream/{id}`). The stream is GET-only —
 * it carries an SSE `observation` feed UP — so any other method is 405. Kept pure + separate
 * from the NanoHTTPD SSE body so the gate is JVM-unit-testable. Returns a 405 [RemoteResponse]
 * to REJECT, or null to proceed to the SSE pump. (Operator-scope for the GET is enforced
 * upstream in [authorize]; this only rejects the wrong METHOD.)
 */
fun streamMethodGate(method: String): RemoteResponse? =
    if (method.uppercase() == "GET") null
    else RemoteResponse(405, JSON.encodeToString(ErrorBody("method not allowed")))

/**
 * (F2 / I3) PURE kill gate for the observation stream. A task the user STOPPED must NOT be
 * served a fresh `observation` frame — serving one would re-raise the consent banner AND keep
 * feeding the cloud loop screen state for a session the user ended. Returns a 409 [RemoteResponse]
 * to REFUSE a killed task, or null to proceed to the SSE pump. Kept pure + separate from the
 * NanoHTTPD SSE body so the gate is JVM-unit-testable (mirrors [streamMethodGate]); the "stopped
 * by user" body is the same stable phrase the killed `/action` result carries, so the loop keys
 * on one signal. Completes the I3 kill-switch: a STOP promptly ends BOTH the `/action` and
 * `/stream` halves of the server loop.
 */
fun streamKillGate(taskId: String, killed: Boolean,
                   detail: String = RemoteSessionBus.DETAIL_USER_STOP): RemoteResponse? =
    if (killed) RemoteResponse(409, JSON.encodeToString(ErrorBody(detail)))
    else null

/**
 * Embedded HTTP listener (NanoHTTPD). Binds to all interfaces, but [authorize] gates
 * every request to tailnet-source callers (Tailscale/WireGuard encrypts the
 * transport), scopes POST /task to the device's bound operator, and scopes POST
 * /notify to the device's subscription allow-list. The socket binding is
 * device/compile-verified; the routing + auth it delegates to are unit-tested via
 * [routeRequest] / [authorize].
 *
 * **Single owner on the control port (MN.4).** Exactly ONE of these binds
 * [REMOTE_CONTROL_PORT]; it is owned by [com.aiblackbox.portal.NotificationListenerFgs]
 * (model-free, boot-survivable), NOT by the Gemma service. The Gemma task handler is
 * INJECTED via [handlerProvider]: it returns [NoopRemoteTaskHandler] until
 * [com.aiblackbox.portal.LocalModelService] publishes the real [RemoteTaskRunner] (when
 * a model loads), so `/task` + `/status` work when Gemma is up while `/healthz` +
 * `/notify` always work model-free.
 *
 * @param handlerProvider read PER REQUEST so an injected Gemma handler appears/vanishes
 *   with the model service without rebinding the socket. Defaults to the no-op handler.
 * @param notifier the model-free notification poster for `/notify`.
 * @param subscriptionPredicate device-local allow-list re-check for `/notify` (true =
 *   accept). Defaults to accept-all (tailnet-only posture).
 * @param actionDispatcherProvider (M1.3) read PER REQUEST — the actuator seam POST /action
 *   dispatches through ([PhoneActionDispatcher] over the live [com.aiblackbox.portal.overlay.AndroidPhoneController]).
 *   Defaults to null (no dispatcher → `/action` returns the honest `not_wired` state).
 * @param observationProvider (M1.2) optional device-side `buildObservation()` source. When
 *   wired, GET /stream/{id} emits one real `observation` frame and the `/action` follow-on
 *   can embed fresh screen state. Null keeps the M0 scaffold stream comment.
 */
class RemoteControlServer(
    port: Int,
    private val handlerProvider: () -> RemoteTaskHandler = { NoopRemoteTaskHandler },
    private val notifier: Notifier? = null,
    private val operatorProvider: () -> String = { "" },
    private val subscriptionPredicate: (operator: String) -> Boolean = { true },
    private val actionDispatcherProvider: () -> RemoteActionDispatcher? = { null },
    private val observationProvider: (suspend () -> Observation?)? = null,
    // (M8.2) the incident-kill seam for POST /kill/{taskId} + /kill-all. Defaults to the
    // process-wide [BusKillSwitch] so the FGS's single server has kill enabled with no extra
    // wiring; a fake is injected in tests.
    private val killSwitchProvider: () -> RemoteKillSwitch? = { BusKillSwitch },
    // (M8.3) the telemetry read seam for GET /telemetry/{taskId} + /telemetry/summary. Defaults
    // to the process-wide [BusTelemetryReader].
    private val telemetryReaderProvider: () -> TelemetryReader = { BusTelemetryReader },
) : NanoHTTPD(port) {

    fun startServer() = start(SOCKET_READ_TIMEOUT, false)
    fun stopServer() = stop()

    override fun serve(session: IHTTPSession): Response {
        val method = session.method?.name ?: "GET"
        val path = session.uri ?: "/"
        val body = if (method.equals("POST", ignoreCase = true)) readBody(session) else ""
        val remoteIp = session.remoteIpAddress ?: ""
        // /task, /notify, (M0) /action and (M8.2) /kill* carry a top-level `operator` in the
        // POST body; (I1) GET /stream/{id} and (M8.3) GET /telemetry* — body-less GETs — carry it
        // as the ?operator= query param.
        val requestOperator = when {
            path == "/task" || path == "/notify" || path == "/action" ||
                path == "/kill-all" || path.startsWith("/kill/") -> extractOperator(body)
            path.startsWith("/stream/") || path.startsWith("/telemetry") ->
                session.parameters?.get("operator")?.firstOrNull()?.trim() ?: ""
            else -> ""
        }
        authorize(method, path, remoteIp, requestOperator, operatorProvider(), subscriptionPredicate)?.let { denied ->
            return newFixedLengthResponse(statusOf(denied.status), "application/json", denied.json)
        }
        // (M8.3) The telemetry GETs return a fixed-length JSON body (operator-scoped above).
        if (path.startsWith("/telemetry")) {
            val routed = if (method.equals("GET", ignoreCase = true))
                buildTelemetryResponse(path, requestOperator, telemetryReaderProvider())
            else RemoteResponse(405, JSON.encodeToString(ErrorBody("method not allowed")))
            return newFixedLengthResponse(statusOf(routed.status), "application/json", routed.json)
        }
        // (M0 scaffold) The observation↑ half of the frontier streaming control channel
        // (decision #8): a chunked SSE response, so it is served here rather than through
        // the pure [routeRequest] (which only yields fixed-length RemoteResponse values).
        // The action↓ half is POST /action, routed below. /task+/status+/healthz+/notify
        // stay untouched for Gemma back-compat.
        if (path.startsWith("/stream/")) {
            return serveObservationStream(method, path.removePrefix("/stream/"), requestOperator)
        }
        // (M1.3) The action↓ half dispatches through the live actuators — a suspend hop, so
        // it is served here (under runBlocking on the NanoHTTPD worker thread) rather than
        // via the pure synchronous routeRequest. Operator-scope was already enforced above.
        if (path == "/action") {
            val routed = kotlinx.coroutines.runBlocking {
                handleActionRequest(method, body, actionDispatcherProvider())
            }
            return newFixedLengthResponse(statusOf(routed.status), "application/json", routed.json)
        }
        val routed = routeRequest(method, path, body, handlerProvider(), notifier, killSwitchProvider())
        return newFixedLengthResponse(statusOf(routed.status), "application/json", routed.json)
    }

    /**
     * (M0 scaffold) The observation↑ half of the bidirectional frontier control channel
     * (research §5.5 decision #8). This NanoHTTPD build depends only on the core
     * `org.nanohttpd:nanohttpd` artifact — the WebSocket module (`nanohttpd-websocket`,
     * which supplies `NanoWSD`) is NOT on the classpath — so the streaming transport is
     * **SSE (chunked `text/event-stream`) + a companion `POST /action`** for the action↓
     * half, exactly the fallback the plan specifies (M0.3). One `observation` frame is a
     * `data:`-prefixed JSON line per SSE framing.
     *
     * (M1.2) When an [observationProvider] is wired it emits ONE real `observation` frame
     * (`data: <json>`) — the device's redacted `ui_tree` + `DeviceCapabilities` + optional
     * silent screenshot — then closes. The CONTINUOUS pump driven by the cloud brain is M2;
     * opening the stream also marks the remote-control session active ([RemoteSessionBus])
     * so the consent banner shows. With no provider wired it falls back to the M0 scaffold
     * comment frame so the endpoint stays reachable.
     */
    private fun serveObservationStream(method: String, taskId: String, operator: String): Response {
        streamMethodGate(method)?.let { denied ->
            return newFixedLengthResponse(statusOf(denied.status), "application/json", denied.json)
        }
        // (F2 / I3) REFUSE a stopped task: never serve it a fresh observation frame (which would
        // resurrect the consent banner + keep the loop driving a session the user ended). This
        // completes the I3 kill-switch on the /stream half — a STOP promptly ends the server loop.
        // (M8.2) carry the kill REASON detail so an operator incident-kill reads "killed by
        // operator" (loop → `killed`) vs a user STOP "stopped by user" (loop → `stopped`).
        streamKillGate(taskId, RemoteSessionBus.isKilled(taskId),
            RemoteSessionBus.killDetail(taskId))?.let { denied ->
            return newFixedLengthResponse(statusOf(denied.status), "application/json", denied.json)
        }
        // Opening the observation stream is the start of a control session → banner on. (The kill
        // gate above already refused a stopped task, so start() here only runs for a live task.)
        RemoteSessionBus.start(taskId, operator)
        val provider = observationProvider
        val frame: String = if (provider != null) {
            // One real observation frame under the tree-first cadence, then close (M1).
            try {
                val obs = kotlinx.coroutines.runBlocking { provider() }
                if (obs != null) {
                    "data: ${WIRE_JSON.encodeToString(obs)}\n\n"
                } else {
                    ": observation unavailable for task '$taskId' (no device state)\n\n"
                }
            } catch (e: Exception) {
                Log.w(TAG, "observation build failed (${e.javaClass.simpleName})")
                ": observation build failed (${e.javaClass.simpleName})\n\n"
            }
        } else {
            // No observation source wired → M0 scaffold comment (lines starting `:` are
            // comments the SSE client ignores).
            ": observation stream scaffold for task '$taskId' — no observation source wired " +
                "(types: ${WireMessageType.OBSERVATION}/${WireMessageType.ACTION_RESULT})\n\n"
        }
        val body = ByteArrayInputStream(frame.toByteArray(Charsets.UTF_8))
        return newChunkedResponse(Response.Status.OK, "text/event-stream", body).apply {
            addHeader("Cache-Control", "no-cache")
            addHeader("Connection", "keep-alive")
        }
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
