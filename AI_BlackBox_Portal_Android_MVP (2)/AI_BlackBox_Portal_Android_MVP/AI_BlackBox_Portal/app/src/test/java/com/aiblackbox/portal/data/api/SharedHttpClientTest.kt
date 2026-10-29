package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.PortalWebGuard
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * One OkHttpClient for the process (audit A10-data-remote-06, A02-overlay-service-10, rank 76).
 *
 * Each BlackBoxApi used to build three independent clients, and the app builds a BlackBoxApi
 * in about 27 places, so every screen paid its own TCP+TLS handshake to the same box and left
 * its own dispatcher threads and idle pool behind. Every client now derives from
 * [HttpClients.base] with newBuilder(), which shares the connection pool and the dispatcher
 * and changes only the timeouts.
 */
class SharedHttpClientTest {

    private fun assertShared(client: OkHttpClient) {
        assertSame(HttpClients.base.connectionPool, client.connectionPool)
        assertSame(HttpClients.base.dispatcher, client.dispatcher)
    }

    @Test
    fun `every BlackBoxApi client shares the process pool and dispatcher`() {
        val a = BlackBoxApi("http://localhost:9091")
        val b = BlackBoxApi("http://100.64.0.1:9091")
        for (client in listOf(a.getClient(), a.streamClient, a.getTtsClient(), b.getClient(), b.streamClient)) {
            assertShared(client)
        }
    }

    @Test
    fun `the derived clients keep their own timeouts`() {
        val api = BlackBoxApi("http://localhost:9091")
        // TTS keeps the 300s read window /tts/batch needs; plain calls keep 120s.
        assertEquals(300_000, api.getTtsClient().readTimeoutMillis)
        assertEquals(30_000, api.getTtsClient().connectTimeoutMillis)
        assertEquals(60_000, api.getTtsClient().writeTimeoutMillis)
        assertEquals(120_000, api.getClient().readTimeoutMillis)
    }

    @Test
    fun `the base starts from OkHttp's defaults, as each replaced Builder did`() {
        val fresh = OkHttpClient.Builder().build()
        assertEquals(fresh.connectTimeoutMillis, HttpClients.base.connectTimeoutMillis)
        assertEquals(fresh.readTimeoutMillis, HttpClients.base.readTimeoutMillis)
        assertEquals(fresh.writeTimeoutMillis, HttpClients.base.writeTimeoutMillis)
        assertEquals(fresh.callTimeoutMillis, HttpClients.base.callTimeoutMillis)
        assertEquals(fresh.pingIntervalMillis, HttpClients.base.pingIntervalMillis)
    }

    @Test
    fun `the WebView download client shares the pool and keeps its paired-origin guard`() {
        val c = PortalWebGuard.pairedOnlyDownloadClient("http://100.64.0.1:9091")
        assertShared(c)
        assertEquals(30_000, c.connectTimeoutMillis)
        assertEquals(60_000, c.readTimeoutMillis)
        assertEquals(1, c.networkInterceptors.size)
    }

    @Test
    fun `an api built over another client derives from that one`() {
        val other = OkHttpClient.Builder().build()
        val api = BlackBoxApi("http://localhost:9091", other)
        assertSame(other.connectionPool, api.getClient().connectionPool)
        assertSame(other.connectionPool, api.streamClient.connectionPool)
    }

    @Test
    fun `the shared dispatcher lets the whole app run as many calls to the box as each screen could alone`() {
        // OkHttp's default is 5 async calls per host. That was per screen while each screen built
        // its own clients; on the one shared dispatcher it would be 5 for the whole app.
        val d = HttpClients.base.dispatcher
        assertTrue("per-host limit ${d.maxRequestsPerHost}", d.maxRequestsPerHost >= 64)
        assertTrue("total limit ${d.maxRequests}", d.maxRequests >= 2 * d.maxRequestsPerHost)
    }

    @Test
    fun `one screen's quick call is not held back by other screens' open stream and slow calls`() = runBlocking {
        val server = MockWebServer()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse =
                when (request.target.substringBefore('?')) {
                    // A slow box answer (a cold model load, a stalled event loop): headers in 4 s.
                    "/slow" -> MockResponse.Builder().headersDelay(4, TimeUnit.SECONDS).body("{}").build()
                    // A chat turn's SSE stream, open for 4 s: SSEClient reads it inside onResponse,
                    // so it holds its dispatcher slot for the whole turn.
                    "/chat/stream" -> MockResponse.Builder()
                        .addHeader("Content-Type", "text/event-stream")
                        .body("event: content\ndata: hi\n\n")
                        .bodyDelay(4, TimeUnit.SECONDS)
                        .build()
                    else -> MockResponse.Builder().body("{\"ok\":true}").build()
                }
        }
        server.start()
        try {
            val origin = server.url("/").toString().trimEnd('/')
            val stream = launch(Dispatchers.IO) {
                runCatching { SSEClient(BlackBoxApi(origin)).stream("/chat/stream", "{}").collect { } }
            }
            // Twice the old per-host limit of slow calls, each from another screen's api.
            val slow = (1..10).map { async(Dispatchers.IO) { runCatching { BlackBoxApi(origin).get("/slow") } } }
            delay(500)
            // The CU STOP (or a send, or a catalog refresh) from yet another screen.
            val t0 = System.nanoTime()
            withTimeout(15_000) { BlackBoxApi(origin).post("/chat/cu-stop", "{}") }
            val ms = (System.nanoTime() - t0) / 1_000_000
            assertTrue("the quick call waited $ms ms behind other screens' calls", ms < 1_500)
            slow.forEach { it.cancelAndJoin() }
            stream.cancelAndJoin()
        } finally {
            server.close()
        }
    }
}
