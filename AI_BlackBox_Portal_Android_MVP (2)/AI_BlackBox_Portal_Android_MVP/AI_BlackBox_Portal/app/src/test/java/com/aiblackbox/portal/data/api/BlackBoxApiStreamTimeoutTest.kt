package com.aiblackbox.portal.data.api

import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * M7.1a — SSE transport hardening pins (retrieval-upgrade plan, Task 7.1).
 *
 * Background: the 2026-04-25 Opus stall was OkHttp's 10s DEFAULT read timeout
 * killing `/chat/stream` mid-TTFB (30-60s first-token silence at ~210k-char
 * prefill; the M3 provider-window audit re-measured 14.0s cold at 210k and
 * mandates hardening for the historical band). The interim fix — readTimeout(0),
 * infinite — traded that for an eternal hang on silently dead sockets. M7.1a
 * lands a BOUNDED 300s read window on the SSE client only.
 *
 * These tests pin the exact timeout values so a refactor cannot silently
 * reintroduce either failure mode, and pin heartbeat/comment-frame tolerance in
 * the SSE parser. Device validation (the 210k repro per provider through this
 * client) happens at the M7 gate on the Fold — see the M7 checklist in the
 * provider-window audit doc.
 */
class BlackBoxApiStreamTimeoutTest {

    // -------------------------------------------------------------------------
    // Timeout configuration pins
    // -------------------------------------------------------------------------

    @Test
    fun `streamClient pins SSE transport timeouts - bounded read, tight connect, no call timeout`() {
        val api = BlackBoxApi("http://localhost:9091")

        // Read: 300s — tolerates the historical 30-60s Opus TTFB stall band with
        // 5-10x margin (and the measured 14.0s cold 210k TTFB with 21x), while
        // remaining BOUNDED so a silently dead TCP path surfaces as an error
        // instead of an infinite STREAMING hang. Aligned with the orchestrator's
        // own provider-leg read timeout (httpx timeout=300).
        assertEquals(300_000, api.streamClient.readTimeoutMillis)

        // Connect: tight — bounds only the TCP+TLS handshake; an unreachable
        // host must fail fast, not inherit the wide streaming window.
        assertEquals(10_000, api.streamClient.connectTimeoutMillis)

        // Write: request bodies are small JSON; unchanged.
        assertEquals(60_000, api.streamClient.writeTimeoutMillis)

        // Call timeout MUST stay unset (0): it would bound the total stream
        // duration, which is unbounded by design for long generations.
        assertEquals(0, api.streamClient.callTimeoutMillis)

        // Guard the guard: the constants the client is built from match what we
        // pinned above (they are public API for derived clients).
        assertEquals(300L, BlackBoxApi.STREAM_READ_TIMEOUT_SECONDS)
        assertEquals(10L, BlackBoxApi.STREAM_CONNECT_TIMEOUT_SECONDS)
        assertEquals(60L, BlackBoxApi.STREAM_WRITE_TIMEOUT_SECONDS)
    }

    @Test
    fun `plain request-response client keeps tight timeouts - SSE window is NOT global`() {
        val api = BlackBoxApi("http://localhost:9091")

        // The long-TTFB window is scoped to streamClient ONLY. Every plain API
        // call keeps the tight 120s read / 30s connect — a regression here would
        // give every request a 5-minute hang window.
        assertEquals(120_000, api.getClient().readTimeoutMillis)
        assertEquals(30_000, api.getClient().connectTimeoutMillis)
        assertEquals(60_000, api.getClient().writeTimeoutMillis)
        assertEquals(0, api.getClient().callTimeoutMillis)
    }

    // -------------------------------------------------------------------------
    // Heartbeat / comment-frame tolerance (functional, against MockWebServer)
    // -------------------------------------------------------------------------

    private lateinit var server: MockWebServer

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After fun tearDown() {
        server.close()
    }

    @Test
    fun `SSE parser tolerates comment keepalives and heartbeat events without corrupting content`() = runTest {
        // Wire format mirrors /chat/stream: `event:`/`data:` frames with
        // json.dumps-wrapped data, plus (a) an SSE comment line (": keepalive" —
        // the standard server-side keepalive frame M7 may add) and (b) an
        // explicit `event: heartbeat` (already emitted by the CU + gemini tool
        // loops). Neither may leak into content or break framing.
        val sseBody = buildString {
            append(": keepalive\n\n")
            append("event: heartbeat\ndata: \"\"\n\n")
            append("event: content\ndata: \"Hello\"\n\n")
            append(": keepalive\n\n")
            append("event: content\ndata: \" world\"\n\n")
            append("event: stream_end\ndata: {\"status\":\"complete\"}\n\n")
        }
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "text/event-stream"))
                .body(sseBody)
                .build()
        )

        val baseUrl = server.url("").toString().trimEnd('/')
        val api = BlackBoxApi(baseUrl)
        val events = SSEClient(api).stream("/chat/stream", "{}").toList()

        // Comment frames produce NO events; heartbeat arrives as its own typed
        // event (ChatViewModel ignores it); content is intact and ordered.
        assertEquals(
            listOf("heartbeat", "content", "content", "stream_end"),
            events.map { it.event }
        )
        assertEquals("", events[0].data)
        assertEquals("Hello", events[1].data)
        assertEquals(" world", events[2].data)
        assertTrue(events[3].data.contains("complete"))
    }

    @Test
    fun `SSE GET parser tolerates comment keepalives too`() = runTest {
        // streamGet() (used by /update/log/stream) shares framing via
        // sseCallback — pin the comment-tolerance branch there as well.
        val sseBody = ": keepalive\n\nevent: log\ndata: \"line1\"\n\n"
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "text/event-stream"))
                .body(sseBody)
                .build()
        )

        val baseUrl = server.url("").toString().trimEnd('/')
        val api = BlackBoxApi(baseUrl)
        val events = SSEClient(api).streamGet("/update/log/stream").toList()

        assertEquals(listOf("log"), events.map { it.event })
        assertEquals("line1", events[0].data)
    }
}
