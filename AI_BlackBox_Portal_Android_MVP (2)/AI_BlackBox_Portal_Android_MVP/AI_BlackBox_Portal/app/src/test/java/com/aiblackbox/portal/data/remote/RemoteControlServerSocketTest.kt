package com.aiblackbox.portal.data.remote

import org.junit.After
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.Socket

/**
 * Audit X02/X04 (rank 29), end to end over a real socket: the attack the audit describes is
 * another app on the phone (or a same-Wi-Fi peer) connecting to :8765 and sending a huge body.
 * The listener must answer from the request line + headers alone — a test client on loopback
 * plays that app. Before the fix the server called parseBody() first, so this request (which
 * declares 50 MB and sends none of it) hung until the socket read timeout instead of being
 * refused immediately.
 */
class RemoteControlServerSocketTest {

    private lateinit var server: RemoteControlServer

    @Before fun start() {
        server = RemoteControlServer(
            port = 0,
            pairingSecretProvider = { "7a".repeat(32) },
            ownAddressesProvider = { emptySet() },
        )
        server.startServer()
    }

    @After fun stop() {
        server.stopServer()
    }

    /** Send raw request bytes and return the status line, failing if none arrives quickly. */
    private fun statusLineFor(request: String): String {
        Socket("127.0.0.1", server.listeningPort).use { s ->
            s.soTimeout = 2_000
            s.getOutputStream().apply { write(request.toByteArray()); flush() }
            val line = BufferedReader(InputStreamReader(s.getInputStream())).readLine()
            return line ?: ""
        }
    }

    @Test fun a_huge_unread_body_from_this_device_is_refused_without_reading_it() {
        val line = statusLineFor(
            "POST /task HTTP/1.1\r\nHost: phone\r\nContent-Type: application/json\r\n" +
                "Content-Length: 52428800\r\n\r\n")
        assertTrue(line, line.startsWith("HTTP/1.1 403"))
    }

    @Test fun loopback_gets_no_service_even_for_healthz() {
        val line = statusLineFor("GET /healthz HTTP/1.1\r\nHost: phone\r\n\r\n")
        assertTrue(line, line.startsWith("HTTP/1.1 403"))
    }

    @Test fun loopback_with_the_right_secret_is_still_refused() {
        val line = statusLineFor(
            "POST /notify HTTP/1.1\r\nHost: phone\r\nAuthorization: Bearer ${"7a".repeat(32)}\r\n" +
                "Content-Length: 2\r\n\r\n{}")
        assertTrue(line, line.startsWith("HTTP/1.1 403"))
    }
}
