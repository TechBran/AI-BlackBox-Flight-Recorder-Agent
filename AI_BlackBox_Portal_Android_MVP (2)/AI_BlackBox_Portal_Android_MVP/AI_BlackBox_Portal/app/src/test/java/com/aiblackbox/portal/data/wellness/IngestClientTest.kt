package com.aiblackbox.portal.data.wellness

import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * The real HTTP path to a stand-in box: the ingest body goes out as the exact outbox gzip
 * bytes with `Content-Encoding: gzip`, the X-BlackBox-Origin signature verifies over those
 * bytes (and the path without its query), enrollment carries no signature, and every answer,
 * including no answer, comes back as a status the outbox table understands.
 */
class IngestClientTest {

    private val server = MockWebServer()
    private val key = ByteArray(32) { (it + 1).toByte() }
    private val signer = OriginSigner("en_0badc0de", OriginSigner.encodeKey(key))
    private lateinit var client: IngestClient

    @Before fun setUp() {
        server.start()
        client = IngestClient(server.url("").toString().removeSuffix("/"), nowSec = { 1_793_000_000L })
    }

    @After fun tearDown() = server.close()

    private fun taken(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS)!!

    private fun verify(req: RecordedRequest, body: ByteArray) {
        val header = req.headers[OriginSigner.HEADER]!!
        val parts = header.split(';').associate { p -> p.substringBefore('=') to p.substringAfter('=') }
        assertEquals("en_0badc0de", parts["enr"])
        assertEquals("1793000000", parts["ts"])
        val expected = OriginSigner.signature(key, "en_0badc0de", 1_793_000_000L, parts.getValue("nonce"),
            req.method!!, req.url.encodedPath, body)
        assertEquals(expected, parts["sig"])
    }

    @Test
    fun `ingest sends the gzip bytes as-is, signed, with Content-Encoding gzip`() = runBlocking {
        server.enqueue(MockResponse.Builder().code(200).body("""{"accepted":3}""").build())
        val gz = WellnessOutbox.gzip("""{"schema":"bbx.wellness.ingest/1"}""")
        val r = client.ingest(gz, signer)
        assertEquals(200, r.status)
        assertTrue(r.ok)
        val req = taken()
        assertEquals("POST", req.method)
        assertEquals("/wellness/ingest", req.url.encodedPath)
        assertEquals("gzip", req.headers["Content-Encoding"])
        val sent = req.body!!.toByteArray()
        assertTrue(sent.contentEquals(gz))
        verify(req, sent)
    }

    @Test
    fun `a GET with a query signs the path alone and the empty body`() = runBlocking {
        server.enqueue(MockResponse.Builder().code(200).body("""{"state":"active"}""").build())
        client.status(WellnessFixtures.OPERATOR, signer)
        val req = taken()
        assertEquals("/wellness/status", req.url.encodedPath)
        assertEquals(WellnessFixtures.OPERATOR, req.url.queryParameter("operator"))
        verify(req, ByteArray(0))
    }

    @Test
    fun `enroll is unsigned`() = runBlocking {
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        client.enroll("""{"operator":"wellness-fixture"}""")
        assertNull(taken().headers[OriginSigner.HEADER])
    }

    @Test
    fun `Retry-After and the policy version header are read`() = runBlocking {
        server.enqueue(MockResponse.Builder().code(429).setHeader("Retry-After", "120").body("").build())
        assertEquals(120L, client.ingest(ByteArray(0), signer).retryAfterS)
        server.enqueue(MockResponse.Builder().code(200).setHeader("X-Policy-Version", "2026-10-29").body("# policy").build())
        assertEquals("2026-10-29", client.get(IngestClient.PRIVACY_POLICY, null).policyVersion)
    }

    @Test
    fun `no answer is status -1, never an exception`() = runBlocking {
        val dead = IngestClient("http://127.0.0.1:1")
        val r = dead.ingest(ByteArray(0), signer)
        assertEquals(-1, r.status)
        assertFalse(r.reached)
        assertEquals(WellnessRules.OutboxReaction.RETRY_LATER, WellnessRules.outboxReaction(r.status))
    }
}
