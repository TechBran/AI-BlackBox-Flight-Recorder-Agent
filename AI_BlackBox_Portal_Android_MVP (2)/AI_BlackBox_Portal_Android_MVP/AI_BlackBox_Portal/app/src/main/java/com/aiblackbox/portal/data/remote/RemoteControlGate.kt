package com.aiblackbox.portal.data.remote

import fi.iki.elonen.NanoHTTPD
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.InputStream
import java.net.NetworkInterface
import java.security.MessageDigest
import java.util.Collections
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.BlockingQueue
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadFactory
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * The control listener's PRE-BODY gate (audit X02/X04). [RemoteControlServer.serve] runs it on
 * the request line + headers alone, before a single body byte is read, so a caller that is off
 * the tailnet, on this phone itself, unpaired, or sending an oversized body costs the process
 * nothing beyond the 8 KB header buffer NanoHTTPD already holds:
 *
 *  1. SOURCE: the peer must be a tailnet address ([isTailnetSource]) — loopback is NOT one any
 *     more, because any other app on the phone can reach 127.0.0.1:8765.
 *  2. SELF: the peer must not be one of this phone's own interface addresses. A local app can
 *     also connect to the phone's own 100.x address, which passes check 1.
 *  3. PAIRING: every route except `GET /healthz` needs `Authorization: Bearer <secret>`, the
 *     per-install secret the phone registered with its box ([RemoteControlPairing]), compared
 *     in constant time. 100.64.0.0/10 is also carrier-NAT space, so check 1 alone is not
 *     enough; the secret is what actually identifies the box.
 *  4. SIZE: a body must declare its length (no chunked uploads) and stay within
 *     [MAX_REQUEST_BODY_BYTES]; the server then reads exactly that many bytes
 *     ([readBoundedBody]) instead of NanoHTTPD's spool-to-temp-file-then-copy parseBody().
 *
 * Operator scoping ([authorize]) still runs after the body is read; this gate is about who may
 * talk to the listener at all. PURE apart from [OwnAddresses] / [BoundedAsyncRunner] → JVM-tested.
 */

/** Largest request body the listener reads. The biggest legitimate one is a `/notify` or an
 *  `/action` frame — a few KB. */
const val MAX_REQUEST_BODY_BYTES: Int = 256 * 1024

/** The one route served without the pairing secret: liveness only, it reveals nothing. */
const val PAIRING_EXEMPT_PATH: String = "/healthz"

private const val BEARER_REALM = "Bearer realm=\"blackbox-remote-control\""

@Serializable private data class GateError(val error: String)

private val GATE_JSON = Json { encodeDefaults = true }

/** Outcome of [gateBeforeBody]. A [Reject] always closes the connection (the unread body must
 *  never be parsed as the next keep-alive request). */
sealed class PreBodyDecision {
    data class Reject(
        val response: RemoteResponse,
        val headers: Map<String, String> = emptyMap(),
    ) : PreBodyDecision()

    /** Proceed: read exactly [bodyLength] bytes (0..[MAX_REQUEST_BODY_BYTES]). */
    data class Accept(val bodyLength: Int) : PreBodyDecision()
}

private fun reject(status: Int, error: String, headers: Map<String, String> = emptyMap()) =
    PreBodyDecision.Reject(RemoteResponse(status, GATE_JSON.encodeToString(GateError(error))), headers)

/** Canonical text of a peer address: no brackets / zone id, IPv4-mapped IPv6 unwrapped. PURE. */
fun normalizeRemoteIp(ip: String): String {
    var a = ip.trim().lowercase().removePrefix("[").removeSuffix("]")
    val zone = a.indexOf('%')
    if (zone >= 0) a = a.substring(0, zone)
    if (a.startsWith("::ffff:") && a.count { it == '.' } == 3) a = a.removePrefix("::ffff:")
    return a
}

/**
 * True iff [authorization] is `Bearer <secret>` for this phone's [secret]. The comparison is
 * constant-time over SHA-256 digests (so neither the content nor the length of the secret leaks
 * through timing). A null/blank [secret] — the phone could not load one — matches nothing.
 */
fun bearerMatches(authorization: String?, secret: String?): Boolean {
    if (secret.isNullOrBlank() || authorization == null) return false
    val value = authorization.trim()
    if (value.length < 7 || !value.substring(0, 7).equals("Bearer ", ignoreCase = true)) return false
    val presented = value.substring(7).trim()
    if (presented.isEmpty()) return false
    val sha = { s: String -> MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8)) }
    return MessageDigest.isEqual(sha(presented), sha(secret))
}

/**
 * PURE pre-body decision. [headers] are NanoHTTPD's (lower-cased names); [ownAddresses] are
 * this phone's interface addresses, already normalized ([OwnAddresses.current]);
 * [pairingSecret] is the phone's listener secret, or null if it could not be loaded.
 */
fun gateBeforeBody(
    method: String,
    path: String,
    remoteIp: String,
    headers: Map<String, String>,
    ownAddresses: Set<String>,
    pairingSecret: String?,
    maxBodyBytes: Int = MAX_REQUEST_BODY_BYTES,
): PreBodyDecision {
    val ip = normalizeRemoteIp(remoteIp)
    if (!isTailnetSource(ip)) return reject(403, "source not on tailnet")
    if (ip in ownAddresses) return reject(403, "requests from this device itself are not accepted")

    val exempt = path == PAIRING_EXEMPT_PATH && method.equals("GET", ignoreCase = true)
    if (!exempt && !bearerMatches(headers["authorization"], pairingSecret)) {
        return reject(401, "valid pairing secret required", mapOf("WWW-Authenticate" to BEARER_REALM))
    }

    val transferEncoding = headers["transfer-encoding"]?.trim()
    if (!transferEncoding.isNullOrEmpty() && !transferEncoding.equals("identity", ignoreCase = true)) {
        return reject(411, "Content-Length required (chunked bodies are not accepted)")
    }
    val declared = headers["content-length"]?.trim()
    val length = if (declared.isNullOrEmpty()) 0L
        else declared.toLongOrNull()?.takeIf { it >= 0 } ?: return reject(400, "invalid Content-Length")
    if (length > maxBodyBytes) return reject(413, "request body too large (max $maxBodyBytes bytes)")
    return PreBodyDecision.Accept(length.toInt())
}

/**
 * Read exactly [length] bytes of body from [input] — never more, and never more than
 * [MAX_REQUEST_BODY_BYTES] (the gate has already refused anything larger). Null when the peer
 * closed the stream before sending what it declared.
 */
fun readBoundedBody(input: InputStream, length: Int): ByteArray? {
    require(length in 0..MAX_REQUEST_BODY_BYTES) { "body length $length outside 0..$MAX_REQUEST_BODY_BYTES" }
    val buf = ByteArray(length)
    var off = 0
    while (off < length) {
        val n = input.read(buf, off, length - off)
        if (n < 0) return null
        off += n
    }
    return buf
}

/**
 * This phone's own interface addresses (normalized), re-enumerated at most every 30 s.
 * Best-effort: an enumeration failure yields an empty set, which leaves the tailnet + pairing
 * checks in force.
 */
object OwnAddresses {
    private const val TTL_MS = 30_000L

    @Volatile private var cached: Set<String> = emptySet()
    @Volatile private var cachedAt: Long = 0L

    fun current(nowMs: Long = System.currentTimeMillis()): Set<String> {
        if (nowMs - cachedAt < TTL_MS && cachedAt != 0L) return cached
        val fresh = try {
            NetworkInterface.getNetworkInterfaces()?.asSequence().orEmpty()
                .flatMap { it.inetAddresses.asSequence() }
                .mapNotNull { it.hostAddress?.let(::normalizeRemoteIp) }
                .toSet()
        } catch (e: Exception) {
            emptySet()
        }
        cached = fresh
        cachedAt = nowMs
        return fresh
    }
}

/**
 * A fixed-size worker pool with a bounded queue. [tryExecute] returns false instead of growing
 * when it is saturated — NanoHTTPD's default runner spawns an unbounded thread per connection.
 * Idle workers time out, so a stopped server leaves no threads behind.
 */
class BoundedWorkerPool(maxWorkers: Int, queueCapacity: Int) {
    private val threadIds = AtomicInteger()
    private val queue: BlockingQueue<Runnable> =
        if (queueCapacity > 0) ArrayBlockingQueue(queueCapacity) else SynchronousQueue()
    private val executor = ThreadPoolExecutor(
        maxWorkers, maxWorkers, 30L, TimeUnit.SECONDS, queue,
        ThreadFactory { r ->
            Thread(r, "RemoteControl-worker-${threadIds.incrementAndGet()}").apply { isDaemon = true }
        },
    ).apply { allowCoreThreadTimeOut(true) }

    fun tryExecute(task: Runnable): Boolean = try {
        executor.execute(task)
        true
    } catch (e: RejectedExecutionException) {
        false
    }

    fun shutdown() {
        executor.shutdownNow()
    }
}

/** NanoHTTPD [NanoHTTPD.AsyncRunner] over a [BoundedWorkerPool]: a connection that finds the
 *  pool saturated is closed at once rather than given a new thread. */
class BoundedAsyncRunner(
    maxWorkers: Int = MAX_WORKERS,
    queueCapacity: Int = QUEUE_CAPACITY,
) : NanoHTTPD.AsyncRunner {
    private val pool = BoundedWorkerPool(maxWorkers, queueCapacity)
    private val running = Collections.synchronizedList(mutableListOf<NanoHTTPD.ClientHandler>())

    override fun exec(code: NanoHTTPD.ClientHandler) {
        running.add(code)
        if (!pool.tryExecute(code)) {
            running.remove(code)
            code.close()
        }
    }

    override fun closed(clientHandler: NanoHTTPD.ClientHandler) {
        running.remove(clientHandler)
    }

    override fun closeAll() {
        synchronized(running) { running.toList() }.forEach { it.close() }
    }

    companion object {
        /** Concurrent connections served. The box drives one session at a time (/stream then
         *  /action, plus the odd /notify or /kill), so 8 is generous. */
        const val MAX_WORKERS = 8
        const val QUEUE_CAPACITY = 16
    }
}
