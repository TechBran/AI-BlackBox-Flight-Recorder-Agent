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
        client = IngestClient(server.url("").toString().removeSuffix("/"), nowSec = { 1_793_000_000L }, keyPinned = { true })
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
    fun `a drifted phone clock is corrected from the box's Date header`() = runBlocking {
        // The box's clock is 10 minutes ahead of this phone's.
        val httpDate = java.text.SimpleDateFormat("EEE, dd MMM yyyy HH:mm:ss 'GMT'", java.util.Locale.US)
            .apply { timeZone = java.util.TimeZone.getTimeZone("GMT") }
            .format(java.util.Date((1_793_000_000L + 600) * 1000))
        server.enqueue(MockResponse.Builder().code(200).setHeader("Date", httpDate).body("{}").build())
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        client.status(WellnessFixtures.OPERATOR, signer)
        taken()
        assertEquals(600L, client.clockSkewS)
        client.status(WellnessFixtures.OPERATOR, signer)
        val ts = taken().headers[OriginSigner.HEADER]!!.split(';').first { it.startsWith("ts=") }.removePrefix("ts=")
        assertEquals("1793000600", ts)
        assertEquals(0L, IngestClient.skewFor(100, 80))
        assertEquals(-45L, IngestClient.skewFor(100, 145))
    }

    @Test
    fun `a signed call to a box the key does not belong to is never sent, signed or not`() = runBlocking {
        val asked = mutableListOf<String>()
        val repointed = IngestClient(server.url("").toString().removeSuffix("/"), nowSec = { 1_793_000_000L },
            keyPinned = { url -> asked += url; false })
        val gz = WellnessOutbox.gzip("""{"schema":"bbx.wellness.ingest/1"}""")
        for (r in listOf(repointed.ingest(gz, signer), repointed.status(WellnessFixtures.OPERATOR, signer),
                repointed.capabilities("{}", signer), repointed.putSettings("{}", signer),
                repointed.action(IngestClient.ERASE, "{}", signer))) {
            assertEquals(-1, r.status)
            assertEquals(IngestClient.OTHER_BOX, r.error)
            assertEquals(WellnessRules.OutboxReaction.RETRY_LATER, WellnessRules.outboxReaction(r.status))
        }
        assertEquals("nothing reached the box", 0, server.requestCount)
        assertEquals(server.url("/wellness/ingest").toString(), asked.first())
        // the unsigned calls (enroll, a read with no key) are not the key's to hold back
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        assertEquals(200, repointed.enroll("""{"operator":"wellness-fixture"}""").status)
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        assertEquals(200, repointed.status(WellnessFixtures.OPERATOR, null).status)
        assertNull(taken().headers[OriginSigner.HEADER])
        assertNull(taken().headers[OriginSigner.HEADER])
        // and the default client signs nothing at all
        assertEquals(IngestClient.OTHER_BOX, IngestClient(server.url("").toString()).ingest(gz, signer).error)
        assertEquals(server.url("").toString().removeSuffix("/"), repointed.origin)
    }

    @Test
    fun `no answer is status -1, never an exception`() = runBlocking {
        val dead = IngestClient("http://127.0.0.1:1", keyPinned = { true })
        val r = dead.ingest(ByteArray(0), signer)
        assertEquals(-1, r.status)
        assertFalse(r.reached)
        assertEquals(WellnessRules.OutboxReaction.RETRY_LATER, WellnessRules.outboxReaction(r.status))
    }
}
