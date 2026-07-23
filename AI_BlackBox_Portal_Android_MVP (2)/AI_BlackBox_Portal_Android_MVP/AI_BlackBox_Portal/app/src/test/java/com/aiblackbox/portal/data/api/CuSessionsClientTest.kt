package com.aiblackbox.portal.data.api

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class CuSessionsClientTest {
    private lateinit var server: MockWebServer
    private lateinit var client: CuSessionsClient

    @Before fun setUp() {
        server = MockWebServer(); server.start()
        val baseUrl = server.url("").toString().trimEnd('/')
        client = CuSessionsClient(BlackBoxApi(baseUrl))
    }
    @After fun tearDown() = server.close()

    @Test fun `parses active sessions`() = runTest {
        server.enqueue(MockResponse.Builder().body(
            """{"active":true,"count":1,"sessions":[
               {"session_id":"s1","operator":"Brandon","backend":"anthropic",
                "width":1280,"height":720,"display":":100","live_view":true,
                "view_url":"/cu/view/s1","started_at":1.0}]}""").build())
        val state = client.sessions()
        assertTrue(state.active)
        assertEquals(1, state.sessions.size)
        assertEquals("Brandon", state.sessions[0].operator)
        assertEquals("/cu/view/s1", state.sessions[0].viewUrl)
    }

    @Test fun `empty when idle`() = runTest {
        server.enqueue(MockResponse.Builder().body(
            """{"active":false,"count":0,"sessions":[]}""").build())
        val state = client.sessions()
        assertFalse(state.active)
        assertTrue(state.sessions.isEmpty())
    }

    // ── pickLiveViewSession (pure) — CU live-view entry-point target choice ──

    @Test fun `picker prefers the first live_view-capable session`() {
        val noStream = CuSession(sessionId = "a", liveView = false)
        val live1 = CuSession(sessionId = "b", liveView = true)
        val live2 = CuSession(sessionId = "c", liveView = true)
        assertEquals("b", pickLiveViewSession(listOf(noStream, live1, live2))?.sessionId)
    }

    @Test fun `picker returns null when no session streams`() {
        val noStream = CuSession(sessionId = "a", liveView = false)
        assertEquals(null, pickLiveViewSession(listOf(noStream)))
        assertEquals(null, pickLiveViewSession(emptyList()))
    }

    // ── openSession — POST /cu/session/open (desktop-first CU, 2026-07-23) ──

    @Test fun `openSession POSTs operator and parses the opened session`() = runTest {
        server.enqueue(MockResponse.Builder().body(
            """{"session_id":"cu-virt-1","view_url":"/cu/view/cu-virt-1",
               "reused":false,"live_view":true}""").build())
        val opened = client.openSession("Brandon")
        assertEquals("cu-virt-1", opened.sessionId)
        assertEquals("/cu/view/cu-virt-1", opened.viewUrl)
        assertFalse(opened.reused)
        assertTrue(opened.liveView)

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/cu/session/open", request.target)
        val sent = Json.parseToJsonElement(request.body!!.utf8()).jsonObject
        assertEquals("Brandon", sent["operator"]?.jsonPrimitive?.content)
    }

    @Test fun `openSession omits operator when blank and reports reuse`() = runTest {
        server.enqueue(MockResponse.Builder().body(
            """{"session_id":"cu-virt-2","view_url":"/cu/view/cu-virt-2",
               "reused":true,"live_view":true}""").build())
        val opened = client.openSession(null)
        assertTrue(opened.reused)

        val sent = Json.parseToJsonElement(server.takeRequest().body!!.utf8()).jsonObject
        assertFalse("operator must be omitted, not null-valued", sent.containsKey("operator"))
    }

    @Test fun `openSession surfaces server errors`() = runTest {
        server.enqueue(MockResponse.Builder().code(503)
            .body("""{"detail":"CU display stack unavailable"}""").build())
        try {
            client.openSession("Brandon")
            fail("expected ApiHttpException")
        } catch (e: ApiHttpException) {
            assertEquals("CU display stack unavailable", e.message)
        }
    }

    // ── closeSession — POST /cu/session/{sid}/close ──

    @Test fun `closeSession POSTs to the session close route`() = runTest {
        server.enqueue(MockResponse.Builder().body("""{"closed":true}""").build())
        client.closeSession("cu-virt-1")
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/cu/session/cu-virt-1/close", request.target)
    }

    @Test fun `closeSession surfaces unknown-session 404`() = runTest {
        server.enqueue(MockResponse.Builder().code(404)
            .body("""{"detail":"unknown session"}""").build())
        try {
            client.closeSession("gone")
            fail("expected ApiHttpException")
        } catch (e: ApiHttpException) {
            assertEquals("unknown session", e.message)
        }
    }

    // ── chooseCuEntrySurface (pure) — desktop-first decision logic, Portal
    // chooseDrawerSurface parity (cu-viewer-route.js) ──

    @Test fun `no sessions on the local desktop offers open-desktop`() {
        val choice = chooseCuEntrySurface(emptyList(), deviceId = "blackbox")
        assertTrue(choice is CuEntrySurface.OpenDesktop)
        assertEquals("no-sessions", (choice as CuEntrySurface.OpenDesktop).reason)
    }

    @Test fun `null or local device ids behave like the local desktop`() {
        assertTrue(chooseCuEntrySurface(emptyList(), deviceId = null) is CuEntrySurface.OpenDesktop)
        assertTrue(chooseCuEntrySurface(emptyList(), deviceId = "local") is CuEntrySurface.OpenDesktop)
    }

    @Test fun `live session streams by default`() {
        val live = CuSession(sessionId = "s1", liveView = true, viewUrl = "/cu/view/s1")
        val choice = chooseCuEntrySurface(listOf(live), deviceId = "blackbox")
        assertTrue(choice is CuEntrySurface.Stream)
        assertEquals("s1", (choice as CuEntrySurface.Stream).session.sessionId)
    }

    @Test fun `first streamable session wins over non-streamable`() {
        val noStream = CuSession(sessionId = "a", liveView = false)
        val live = CuSession(sessionId = "b", liveView = true, viewUrl = "/cu/view/b")
        val choice = chooseCuEntrySurface(listOf(noStream, live))
        assertEquals("b", (choice as CuEntrySurface.Stream).session.sessionId)
    }

    @Test fun `sessions without a working stream fall back — never the open CTA`() {
        // live_view=false, and live_view=true with no view_url: both unstreamable
        val native = CuSession(sessionId = "a", liveView = false)
        val noUrl = CuSession(sessionId = "b", liveView = true, viewUrl = "")
        val choice = chooseCuEntrySurface(listOf(native, noUrl))
        assertTrue(choice is CuEntrySurface.Fallback)
        assertEquals("stream-unavailable", (choice as CuEntrySurface.Fallback).reason)
    }

    @Test fun `remote device targets never get the local-desktop CTA`() {
        // With no sessions AND with a streamable one — remote is always fallback
        val choiceEmpty = chooseCuEntrySurface(emptyList(), deviceId = "pixel-fold")
        assertTrue(choiceEmpty is CuEntrySurface.Fallback)
        assertEquals("remote-device", (choiceEmpty as CuEntrySurface.Fallback).reason)

        val live = CuSession(sessionId = "s1", liveView = true, viewUrl = "/cu/view/s1")
        val choiceLive = chooseCuEntrySurface(listOf(live), deviceId = "pixel-fold")
        assertTrue(choiceLive is CuEntrySurface.Fallback)
    }

    @Test fun `stream choice carries no open-desktop leakage`() {
        // Guard: OpenDesktop only ever appears for the empty-local case
        val live = CuSession(sessionId = "s1", liveView = true, viewUrl = "/cu/view/s1")
        assertNull(
            listOf(
                chooseCuEntrySurface(listOf(live)),
                chooseCuEntrySurface(listOf(CuSession(sessionId = "x", liveView = false))),
                chooseCuEntrySurface(emptyList(), deviceId = "remote-1"),
            ).firstOrNull { it is CuEntrySurface.OpenDesktop }
        )
    }
}
