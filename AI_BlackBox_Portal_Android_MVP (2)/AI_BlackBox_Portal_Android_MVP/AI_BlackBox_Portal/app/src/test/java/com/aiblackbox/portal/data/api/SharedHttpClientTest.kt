package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.PortalWebGuard
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

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
}
