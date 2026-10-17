package com.aiblackbox.portal.data.remote

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException

/**
 * Audit X02/X04 (rank 29), end to end over a real socket. The attackers the audit describes are
 * another app on the phone (loopback, or the phone's own address) and a peer that can reach
 * :8765 without being the paired box (a same-Wi-Fi or carrier-NAT neighbour). None of them may
 * get a response, hold a worker, or make the listener read an unbounded body.
 *
 * Every test client connects from loopback. To play a tailnet peer the server is given a
 * [RemoteControlServer.peerPolicy] that admits a loopback ALIAS (127.0.0.2 / 127.0.0.3, which
 * Linux routes to lo), so "admitted" and "refused" peers can share one test host.
 */
class RemoteControlServerSocketTest {

    private val secret = "7a".repeat(32)
    private var server: RemoteControlServer? = null
    private val openSockets = mutableListOf<Socket>()

    private fun start(
        admitted: Set<String>? = null,
        headerDeadlineMs: Long = HEADER_DEADLINE_MS,
    ): RemoteControlServer {
        val s = if (admitted == null) {
            // The production peer policy: only tailnet sources, never this host itself.
            RemoteControlServer(port = 0, pairingSecretProvider = { secret },
                ownAddressesProvider = { emptySet() }, headerDeadlineMs = headerDeadlineMs)
        } else {
            RemoteControlServer(port = 0, pairingSecretProvider = { secret },
                ownAddressesProvider = { emptySet() }, headerDeadlineMs = headerDeadlineMs,
                peerPolicy = { ip -> if (ip in admitted) null else "source not on tailnet" })
        }
        s.startServer()
        server = s
        return s
    }

    @After fun stop() {
        openSockets.forEach { runCatching { it.close() } }
        server?.stopServer()
    }

    private fun connect(from: String = "127.0.0.1"): Socket {
        val s = Socket()
        s.bind(InetSocketAddress(InetAddress.getByName(from), 0))
        s.connect(InetSocketAddress("127.0.0.1", server!!.listeningPort), 2_000)
        s.soTimeout = 3_000
        return s
    }

    /** True when this host can source connections from a loopback alias (Linux can). */
    private fun loopbackAliasesWork(): Boolean = try {
        Socket().use { it.bind(InetSocketAddress(InetAddress.getByName("127.0.0.3"), 0)) }
        true
    } catch (e: IOException) {
        false
    }

    /**
     * Send raw request bytes and return the status line (trimmed: NanoHTTPD ends it with a
     * space), or null when the server closed the connection without answering. A read TIMEOUT
     * fails the test: the refusal must be prompt.
     */
    private fun exchange(request: String, from: String = "127.0.0.1"): String? {
        connect(from).use { s ->
            try {
                s.getOutputStream().apply { write(request.toByteArray()); flush() }
            } catch (e: SocketException) {
                return null  // already closed (reset) by the server
            }
            return try {
                BufferedReader(InputStreamReader(s.getInputStream())).readLine()?.trim()
            } catch (e: SocketTimeoutException) {
                throw AssertionError("no answer and no close within ${s.soTimeout} ms", e)
            } catch (e: SocketException) {
                null  // connection reset: closed without a response
            }
        }
    }

    private val healthz = "GET /healthz HTTP/1.1\r\nHost: phone\r\n\r\n"

    // ── refused peers get nothing: closed at accept, before a header byte is read ────

    @Test fun a_huge_unread_body_from_this_device_is_refused_without_reading_it() {
        start()
        assertNull(exchange(
            "POST /task HTTP/1.1\r\nHost: phone\r\nContent-Type: application/json\r\n" +
                "Content-Length: 52428800\r\n\r\n"))
    }

    @Test fun loopback_gets_no_service_even_for_healthz() {
        start()
        assertNull(exchange(healthz))
    }

    @Test fun loopback_with_the_right_secret_is_still_refused() {
        start()
        assertNull(exchange(
            "POST /notify HTTP/1.1\r\nHost: phone\r\nAuthorization: Bearer $secret\r\n" +
                "Content-Length: 2\r\n\r\n{}"))
    }

    // ── an admitted (tailnet) peer still passes the full pre-body gate ───────────────

    @Test fun an_admitted_peer_is_served_through_the_pre_body_gate() {
        assumeTrue(loopbackAliasesWork())
        start(admitted = setOf("127.0.0.2"))
        assertEquals("HTTP/1.1 200 OK", exchange(healthz, from = "127.0.0.2"))
        val noSecret = exchange("POST /notify HTTP/1.1\r\nHost: phone\r\nContent-Length: 2\r\n\r\n{}",
            from = "127.0.0.2")
        assertTrue(noSecret, noSecret!!.startsWith("HTTP/1.1 401"))
        val huge = exchange("POST /task HTTP/1.1\r\nHost: phone\r\nAuthorization: Bearer $secret\r\n" +
            "Content-Length: 52428800\r\n\r\n", from = "127.0.0.2")
        assertTrue(huge, huge!!.startsWith("HTTP/1.1 413"))
    }

    // ── the bounded pool cannot be held by peers that never get past the gate ────────

    @Test fun refused_peers_cannot_exhaust_the_worker_pool() {
        assumeTrue(loopbackAliasesWork())
        start(admitted = setOf("127.0.0.2"))
        // Far more idle connections than workers + queue slots, from a refused source. Before
        // the accept-time check each one took a slot and sat in the 5 s header read.
        repeat(3 * (BoundedAsyncRunner.MAX_WORKERS + BoundedAsyncRunner.QUEUE_CAPACITY)) {
            openSockets += connect("127.0.0.1")
        }
        assertEquals("HTTP/1.1 200 OK", exchange(healthz, from = "127.0.0.2"))
    }

    @Test fun one_admitted_peer_cannot_hold_every_worker() {
        assumeTrue(loopbackAliasesWork())
        start(admitted = setOf("127.0.0.2", "127.0.0.3"))
        // A carrier-NAT neighbour passes the source check; it still gets at most its share.
        repeat(BoundedAsyncRunner.MAX_WORKERS + BoundedAsyncRunner.QUEUE_CAPACITY + 6) {
            openSockets += connect("127.0.0.2")
        }
        assertEquals("HTTP/1.1 200 OK", exchange(healthz, from = "127.0.0.3"))
    }

    // ── a request's headers must arrive within the deadline ──────────────────────────

    @Test fun a_peer_that_trickles_its_headers_is_cut_off_at_the_deadline() {
        assumeTrue(loopbackAliasesWork())
        start(admitted = setOf("127.0.0.2"), headerDeadlineMs = 300)
        val s = connect("127.0.0.2").also { openSockets += it }
        val out = s.getOutputStream()
        out.write("GET /healthz HTTP/1.1\r\nX-Pad: ".toByteArray()); out.flush()
        // One byte every 100 ms: well inside the 5 s per-read timeout, so only the overall
        // header deadline can end it. Stops by itself after 4 s.
        val trickle = Thread {
            try {
                repeat(40) { out.write('a'.code); out.flush(); Thread.sleep(100) }
            } catch (e: Exception) {
                // the server closed the connection: expected
            }
        }.apply { isDaemon = true; start() }
        val started = System.nanoTime()
        val closed = try {
            s.getInputStream().read() == -1
        } catch (e: SocketTimeoutException) {
            false
        } catch (e: SocketException) {
            true  // reset
        }
        val elapsedMs = (System.nanoTime() - started) / 1_000_000
        trickle.interrupt()
        assertTrue("connection still open after ${elapsedMs} ms", closed)
        assertTrue("closed only after ${elapsedMs} ms", elapsedMs < 2_500)
    }

    @Test fun keep_alive_requests_still_work_under_the_header_deadline() {
        assumeTrue(loopbackAliasesWork())
        start(admitted = setOf("127.0.0.2"), headerDeadlineMs = 300)
        connect("127.0.0.2").use { s ->
            val out = s.getOutputStream()
            val reader = BufferedReader(InputStreamReader(s.getInputStream()))
            fun roundTrip(): String {
                out.write(healthz.toByteArray()); out.flush()
                val status = reader.readLine()?.trim().orEmpty()
                // Drain the headers and the small fixed-length body.
                var length = 0
                while (true) {
                    val line = reader.readLine() ?: break
                    if (line.isEmpty()) break
                    if (line.lowercase().startsWith("content-length:")) {
                        length = line.substringAfter(':').trim().toInt()
                    }
                }
                repeat(length) { reader.read() }
                return status
            }
            assertEquals("HTTP/1.1 200 OK", roundTrip())
            Thread.sleep(500)  // idle between requests counts for nothing: the clock starts per request
            assertEquals("HTTP/1.1 200 OK", roundTrip())
        }
    }
}
