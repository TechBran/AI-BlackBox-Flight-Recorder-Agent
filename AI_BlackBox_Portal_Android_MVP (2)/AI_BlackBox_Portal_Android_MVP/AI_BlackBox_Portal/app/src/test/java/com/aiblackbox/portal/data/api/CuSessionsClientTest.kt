package com.aiblackbox.portal.data.api

import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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
}
