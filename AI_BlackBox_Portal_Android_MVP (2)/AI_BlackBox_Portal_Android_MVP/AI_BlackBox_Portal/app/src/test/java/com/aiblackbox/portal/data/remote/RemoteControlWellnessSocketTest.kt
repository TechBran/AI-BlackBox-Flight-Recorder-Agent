package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.data.wellness.OriginSigner
import com.aiblackbox.portal.data.wellness.WellnessBinding
import com.aiblackbox.portal.data.wellness.WellnessBindingRecord
import com.aiblackbox.portal.data.wellness.WellnessBindRequest
import com.aiblackbox.portal.data.wellness.WellnessFiles
import com.aiblackbox.portal.data.wellness.WellnessFixtures
import com.aiblackbox.portal.data.wellness.WellnessPort
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket

/**
 * `/wellness/bind` end to end over a real socket: the pre-body gate (pairing bearer), the
 * operator taken from the POST body, authorize()'s wellness clause and the nonce check, the
 * way the box will call it. The peer plays a tailnet node from a loopback alias, as in
 * RemoteControlServerSocketTest.
 */
class RemoteControlWellnessSocketTest {

    private val secret = "5c".repeat(32)
    private val op = WellnessFixtures.OPERATOR
    private val key = OriginSigner.encodeKey(ByteArray(32) { 9 })
    private val binding = WellnessBinding(WellnessFiles(WellnessFixtures.tempDir("socket")))
    private var server: RemoteControlServer? = null

    private val port = object : WellnessPort {
        override fun bindOperator() = binding.bindOperator()
        override fun enrolledOperator() = binding.enrolledOperator()
        override fun acceptBind(request: WellnessBindRequest) = binding.acceptBind(request)
        override fun current() = binding.current()
        override fun requestSync() = Unit
        override fun clearAll() = binding.clearAll()
        override fun onBound(record: WellnessBindingRecord) = Unit
    }

    private fun start() {
        val s = RemoteControlServer(
            port = 0,
            // The chat-bound operator is someone else on purpose: health scope is the enrollment's.
            operatorProvider = { "someone-else" },
            pairingSecretProvider = { secret },
            wellnessPortProvider = { port },
            ownAddressesProvider = { emptySet() },
            peerPolicy = { ip -> if (ip == "127.0.0.2") null else "source not on tailnet" },
        )
        s.startServer()
        server = s
    }

    @After fun stop() {
        server?.stopServer()
    }

    private fun aliasesWork(): Boolean = try {
        Socket().use { it.bind(InetSocketAddress(InetAddress.getByName("127.0.0.2"), 0)) }
        true
    } catch (e: IOException) {
        false
    }

    private fun post(path: String, body: String, bearer: String? = secret): String? {
        Socket().use { s ->
            s.bind(InetSocketAddress(InetAddress.getByName("127.0.0.2"), 0))
            s.connect(InetSocketAddress("127.0.0.1", server!!.listeningPort), 2_000)
            s.soTimeout = 3_000
            val bytes = body.toByteArray(Charsets.UTF_8)
            val auth = if (bearer != null) "Authorization: Bearer $bearer\r\n" else ""
            s.getOutputStream().apply {
                write(("POST $path HTTP/1.1\r\nHost: phone\r\nContent-Type: application/json\r\n$auth" +
                    "Content-Length: ${bytes.size}\r\nConnection: close\r\n\r\n").toByteArray())
                write(bytes)
                flush()
            }
            return BufferedReader(InputStreamReader(s.getInputStream())).readLine()?.trim()
        }
    }

    private fun bindBody(nonce: String, operator: String = op) =
        """{"operator":"$operator","enrollment_id":"en_0badc0de","epoch":1,"nonce":"$nonce","k":"$key"}"""

    @Test fun bind_over_the_socket_needs_the_bearer_the_operator_and_the_live_nonce() {
        assumeTrue(aliasesWork())
        start()
        val intent = binding.beginIntent(op)
        assertEquals("HTTP/1.1 401 Unauthorized", post("/wellness/bind", bindBody(intent.nonce), bearer = null))
        assertEquals("HTTP/1.1 403 Forbidden", post("/wellness/bind", bindBody(intent.nonce, operator = "someone-else")))
        assertEquals("HTTP/1.1 403 Forbidden", post("/wellness/bind", bindBody("0".repeat(32))))
        assertNull(binding.current())
        assertEquals("HTTP/1.1 200 OK", post("/wellness/bind", bindBody(intent.nonce)))
        assertEquals("en_0badc0de", binding.current()?.enrollmentId)
        assertEquals("HTTP/1.1 202 Accepted",
            post("/wellness/sync", """{"operator":"$op","enrollment_id":"en_0badc0de"}"""))
        assertEquals("HTTP/1.1 200 OK",
            post("/wellness/unbind", """{"operator":"$op","enrollment_id":"en_0badc0de"}"""))
        assertNull(binding.current())
    }
}
