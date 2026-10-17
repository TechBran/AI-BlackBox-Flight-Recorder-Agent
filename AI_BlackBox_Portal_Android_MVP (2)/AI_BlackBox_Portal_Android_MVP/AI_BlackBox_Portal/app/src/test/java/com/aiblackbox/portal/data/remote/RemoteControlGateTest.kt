package com.aiblackbox.portal.data.remote

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.net.SocketTimeoutException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Audit X02/X04 (rank 29): the control listener decides source, pairing secret and body size
 * from the request line + headers ALONE, before a single body byte is read. These are the
 * hostile inputs: another app on the phone (loopback or the phone's own tailnet address), a
 * carrier-NAT neighbour in 100.64/10 without the secret, and an oversized or unbounded body.
 */
class RemoteControlGateTest {

    private val secret = "5e".repeat(32)
    private val box = "100.101.102.103"
    private val phone = "100.88.0.7"
    private val own = setOf(phone, "192.168.1.40", "127.0.0.1", "::1")
    private val auth = mapOf("authorization" to "Bearer $secret")

    private fun gate(
        method: String = "POST",
        path: String = "/task",
        ip: String = box,
        headers: Map<String, String> = auth + ("content-length" to "20"),
        pairingSecret: String? = secret,
    ) = gateBeforeBody(method, path, ip, headers, own, pairingSecret)

    private fun status(d: PreBodyDecision): Int = (d as PreBodyDecision.Reject).response.status

    // ── source ─────────────────────────────────────────────────────────────────────

    @Test fun paired_box_on_the_tailnet_is_accepted_with_its_body_length() {
        val d = gate()
        assertTrue(d is PreBodyDecision.Accept)
        assertEquals(20, (d as PreBodyDecision.Accept).bodyLength)
    }

    @Test fun loopback_is_never_trusted_even_with_the_secret() {
        // Any other app on the phone can reach 127.0.0.1:8765.
        assertEquals(403, status(gate(ip = "127.0.0.1")))
        assertEquals(403, status(gate(ip = "::1")))
    }

    @Test fun the_phones_own_tailnet_address_is_refused() {
        // A local app can also connect to the phone's own 100.x address, which passes the
        // tailnet-range check; the own-address check is what stops it.
        assertEquals(403, status(gate(ip = phone)))
        assertEquals(403, status(gate(ip = "::ffff:$phone")))
    }

    @Test fun lan_and_public_sources_are_refused() {
        assertEquals(403, status(gate(ip = "192.168.1.50")))
        assertEquals(403, status(gate(ip = "8.8.8.8")))
        assertEquals(403, status(gate(ip = "")))
    }

    // ── pairing secret ─────────────────────────────────────────────────────────────

    @Test fun every_route_but_healthz_requires_the_secret() {
        val routes = listOf(
            "POST" to "/task", "GET" to "/status/t1", "POST" to "/notify", "POST" to "/action",
            "GET" to "/stream/t1", "POST" to "/kill-all", "POST" to "/kill/t1",
            "GET" to "/telemetry/summary", "GET" to "/telemetry/t1", "GET" to "/nope",
        )
        for ((m, p) in routes) {
            assertEquals("$m $p", 401, status(gate(method = m, path = p, headers = emptyMap())))
        }
    }

    @Test fun healthz_get_needs_no_secret_but_still_needs_a_tailnet_source() {
        assertTrue(gate(method = "GET", path = "/healthz", headers = emptyMap()) is PreBodyDecision.Accept)
        assertEquals(403, status(gate(method = "GET", path = "/healthz", ip = "127.0.0.1", headers = emptyMap())))
        // Only GET is exempt.
        assertEquals(401, status(gate(method = "POST", path = "/healthz", headers = emptyMap())))
    }

    @Test fun a_carrier_nat_neighbour_without_the_secret_is_refused() {
        assertEquals(401, status(gate(ip = "100.64.12.34", headers = mapOf("content-length" to "2"))))
    }

    @Test fun a_wrong_or_malformed_secret_is_refused() {
        val wrong = "Bearer " + "6f".repeat(32)
        assertEquals(401, status(gate(headers = mapOf("authorization" to wrong))))
        assertEquals(401, status(gate(headers = mapOf("authorization" to secret))))           // no scheme
        assertEquals(401, status(gate(headers = mapOf("authorization" to "Basic $secret"))))
        assertEquals(401, status(gate(headers = mapOf("authorization" to "Bearer "))))
        assertEquals(401, status(gate(headers = mapOf("authorization" to "Bearer ${secret}x"))))
    }

    @Test fun an_unpaired_phone_fails_closed() {
        // No secret could be loaded on the phone: nothing but /healthz is served.
        assertEquals(401, status(gate(pairingSecret = null)))
        assertEquals(401, status(gate(pairingSecret = "", headers = mapOf("authorization" to "Bearer "))))
        assertTrue(gate(method = "GET", path = "/healthz", headers = emptyMap(), pairingSecret = null)
            is PreBodyDecision.Accept)
    }

    @Test fun the_401_names_the_bearer_scheme() {
        val r = gate(headers = emptyMap()) as PreBodyDecision.Reject
        assertTrue(r.headers["WWW-Authenticate"]!!.startsWith("Bearer"))
        assertFalse(r.response.json.contains(secret))
    }

    @Test fun bearer_scheme_is_case_insensitive_and_whitespace_tolerant() {
        assertTrue(bearerMatches("bearer $secret", secret))
        assertTrue(bearerMatches("  Bearer   $secret  ", secret))
        assertFalse(bearerMatches(null, secret))
        assertFalse(bearerMatches("Bearer $secret", null))
        assertFalse(bearerMatches("Bearer ", ""))
    }

    // ── body size, decided before reading ──────────────────────────────────────────

    @Test fun a_body_over_256_kb_is_refused_before_it_is_read() {
        val over = (MAX_REQUEST_BODY_BYTES + 1).toString()
        assertEquals(413, status(gate(headers = auth + ("content-length" to over))))
        assertEquals(413, status(gate(headers = auth + ("content-length" to "52428800"))))
    }

    @Test fun a_body_of_exactly_the_cap_is_accepted() {
        val d = gate(headers = auth + ("content-length" to MAX_REQUEST_BODY_BYTES.toString()))
        assertEquals(MAX_REQUEST_BODY_BYTES, (d as PreBodyDecision.Accept).bodyLength)
    }

    @Test fun a_chunked_or_unknown_length_body_is_refused() {
        assertEquals(411, status(gate(headers = auth + ("transfer-encoding" to "chunked"))))
    }

    @Test fun a_malformed_content_length_is_refused() {
        assertEquals(400, status(gate(headers = auth + ("content-length" to "abc"))))
        assertEquals(400, status(gate(headers = auth + ("content-length" to "-5"))))
    }

    @Test fun no_content_length_means_an_empty_body() {
        assertEquals(0, (gate(headers = auth) as PreBodyDecision.Accept).bodyLength)
    }

    @Test fun an_unauthenticated_caller_learns_nothing_about_size_handling() {
        // The secret is checked first, so a huge unauthenticated body is a plain 401.
        assertEquals(401, status(gate(headers = mapOf("content-length" to "52428800"))))
    }

    // ── address normalisation ──────────────────────────────────────────────────────

    @Test fun remote_ip_normalisation() {
        assertEquals("100.88.0.7", normalizeRemoteIp(" ::FFFF:100.88.0.7 "))
        assertEquals("fe80::1", normalizeRemoteIp("fe80::1%wlan0"))
        assertEquals("fd7a:115c:a1e0::1", normalizeRemoteIp("[FD7A:115C:A1E0::1]"))
        assertEquals("100.88.0.7", normalizeRemoteIp("100.88.0.7"))
    }

    // ── bounded body read ──────────────────────────────────────────────────────────

    @Test fun reads_exactly_the_declared_length_and_no_more() {
        val input = ByteArrayInputStream("{\"a\":1}NEXT".toByteArray())
        assertArrayEquals("{\"a\":1}".toByteArray(), readBoundedBody(input, 7))
        assertEquals('N'.code, input.read())        // the rest stays unread
    }

    @Test fun a_short_body_is_reported_incomplete() {
        assertNull(readBoundedBody(ByteArrayInputStream("abc".toByteArray()), 10))
    }

    @Test fun a_zero_length_body_is_empty() {
        assertEquals(0, readBoundedBody(ByteArrayInputStream(ByteArray(0)), 0)!!.size)
    }

    @Test(expected = IllegalArgumentException::class)
    fun a_read_beyond_the_cap_is_refused() {
        readBoundedBody(ByteArrayInputStream(ByteArray(0)), MAX_REQUEST_BODY_BYTES + 1)
    }

    // ── peer policy (also applied at ACCEPT time, before a worker slot is taken) ──────

    @Test fun peer_rejection_admits_only_a_tailnet_peer_that_is_not_this_phone() {
        assertNull(peerRejection(box, own))
        assertNull(peerRejection("fd7a:115c:a1e0::53", own))
        assertEquals("source not on tailnet", peerRejection("127.0.0.1", own))
        assertEquals("source not on tailnet", peerRejection("::1", own))
        assertEquals("source not on tailnet", peerRejection("192.168.1.50", own))
        assertEquals("source not on tailnet", peerRejection("", own))
        assertTrue(peerRejection(phone, own)!!.contains("this device"))
        assertTrue(peerRejection("::ffff:$phone", own)!!.contains("this device"))
    }

    @Test fun the_gate_honours_a_peer_decision_made_elsewhere() {
        // The server decides the peer once (its injectable peerPolicy) and hands it in.
        val refused = gateRequest("GET", "/healthz", "source not on tailnet", emptyMap(), secret)
        assertEquals(403, status(refused))
        assertTrue(gateRequest("GET", "/healthz", null, emptyMap(), secret) is PreBodyDecision.Accept)
        assertEquals(401, status(gateRequest("POST", "/task", null, emptyMap(), secret)))
    }

    // ── header deadline ────────────────────────────────────────────────────────────

    /** Yields one byte per read, like a peer trickling its request line. */
    private class Trickle : InputStream() {
        override fun read(): Int = 'a'.code
        override fun read(b: ByteArray, off: Int, len: Int): Int { b[off] = 'a'.code.toByte(); return 1 }
    }

    @Test fun headers_that_take_longer_than_the_deadline_are_cut_off() {
        var now = 1_000L
        val input = HeaderDeadlineInputStream(Trickle(), deadlineMs = 500, clock = { now })
        val buf = ByteArray(8)
        input.read(buf, 0, 8)          // first byte of the request: the clock starts
        now += 499
        input.read(buf, 0, 8)          // still inside the deadline
        now += 1
        try {
            input.read(buf, 0, 8)
            throw AssertionError("read past the header deadline must fail")
        } catch (e: SocketTimeoutException) {
            // NanoHTTPD closes the connection on a SocketTimeoutException
        }
        try {
            input.read()
            throw AssertionError("the single-byte read must fail too")
        } catch (e: SocketTimeoutException) {
        }
    }

    @Test fun idle_time_before_a_request_does_not_count() {
        var now = 0L
        val input = HeaderDeadlineInputStream(Trickle(), deadlineMs = 500, clock = { now })
        now += 60_000                 // a keep-alive connection waiting for its next request
        val buf = ByteArray(4)
        input.read(buf, 0, 4)         // the request's first byte starts the clock
        now += 400
        assertEquals(1, input.read(buf, 0, 4))
    }

    @Test fun the_body_is_never_timed_and_the_next_request_gets_a_fresh_clock() {
        var now = 0L
        val input = HeaderDeadlineInputStream(Trickle(), deadlineMs = 500, clock = { now })
        val buf = ByteArray(4)
        input.read(buf, 0, 4)
        input.disarm()                // headers parsed: serve() runs, the body is length-bounded
        now += 10_000
        assertEquals(1, input.read(buf, 0, 4))
        input.rearm()                 // response sent: the next request on this connection
        now += 10_000
        input.read(buf, 0, 4)         // starts its own clock
        now += 400
        assertEquals(1, input.read(buf, 0, 4))
        now += 100
        try {
            input.read(buf, 0, 4)
            throw AssertionError("the re-armed deadline must apply")
        } catch (e: SocketTimeoutException) {
        }
    }

    // ── per-peer connection slots ──────────────────────────────────────────────────

    @Test fun one_peer_gets_at_most_its_share_of_connections() {
        val slots = PeerSlots(maxPerPeer = 2)
        assertTrue(slots.tryAcquire("100.70.0.1"))
        assertTrue(slots.tryAcquire("100.70.0.1"))
        assertFalse(slots.tryAcquire("100.70.0.1"))      // a third from the same peer: refused
        assertTrue(slots.tryAcquire(box))                // another peer is unaffected
        slots.release("100.70.0.1")
        assertTrue(slots.tryAcquire("100.70.0.1"))       // a freed slot can be reused
        assertEquals(2, slots.inUse("100.70.0.1"))
    }

    @Test fun releasing_more_than_was_acquired_never_goes_negative() {
        val slots = PeerSlots(maxPerPeer = 1)
        slots.release(box)
        assertEquals(0, slots.inUse(box))
        assertTrue(slots.tryAcquire(box))
        assertFalse(slots.tryAcquire(box))
    }

    // ── bounded worker pool ────────────────────────────────────────────────────────

    @Test fun the_worker_pool_refuses_work_beyond_its_bound() {
        val pool = BoundedWorkerPool(maxWorkers = 2, queueCapacity = 1)
        val release = CountDownLatch(1)
        val started = CountDownLatch(2)
        val blocker = Runnable { started.countDown(); release.await(5, TimeUnit.SECONDS) }
        try {
            assertTrue(pool.tryExecute(blocker))
            assertTrue(pool.tryExecute(blocker))
            assertTrue(started.await(5, TimeUnit.SECONDS))
            assertTrue(pool.tryExecute(Runnable { }))       // fills the one queue slot
            assertFalse(pool.tryExecute(Runnable { }))      // saturated: refused, not a new thread
        } finally {
            release.countDown()
            pool.shutdown()
        }
    }

    @Test fun the_worker_pool_accepts_again_once_workers_free_up() {
        val pool = BoundedWorkerPool(maxWorkers = 1, queueCapacity = 0)
        try {
            val done = CountDownLatch(1)
            assertTrue(pool.tryExecute(Runnable { done.countDown() }))
            assertTrue(done.await(5, TimeUnit.SECONDS))
            var accepted = false
            for (i in 0 until 50) {
                if (pool.tryExecute(Runnable { })) { accepted = true; break }
                Thread.sleep(20)
            }
            assertTrue(accepted)
        } finally {
            pool.shutdown()
        }
    }
}
