package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

/**
 * W22 — the LIVE model list a provider switch checks before restoring that provider's
 * remembered pick ([ChatViewModel.fetchLiveModelIds], passed to
 * BlackBoxStore.setProviderAndModel by BOTH the composer pill and the Settings sheet).
 * It must hit the same /models/{apiProvider} endpoint the pill's dropdown hydrates
 * from, and answer null (= stay on Auto) whenever the list is not actually known.
 */
class ChatViewModelLiveModelIdsTest {
    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi

    @Before fun setUp() {
        server = MockWebServer(); server.start()
        api = BlackBoxApi(server.url("").toString().trimEnd('/'))
    }
    @After fun tearDown() = server.close()

    @Test fun `returns the ids of the provider's live catalog`() = runTest {
        server.enqueue(MockResponse.Builder().body(
            """{"models":[{"id":"gemini-3.1-pro-preview","name":"Gemini 3.1 Pro"},
               {"id":"gemini-2.5-flash"}],"source":"live"}""").build())

        val ids = ChatViewModel.fetchLiveModelIds(api, "gemini")

        assertEquals(listOf("gemini-3.1-pro-preview", "gemini-2.5-flash"), ids)
        // Same provider→endpoint mapping as the dropdown (gemini is "google" server-side).
        assertEquals("/models/google", server.takeRequest().target)
    }

    @Test fun `custom ids come back qualified`() = runTest {
        server.enqueue(MockResponse.Builder().body(
            """{"models":[{"id":"gemma-box::gemma-26b","status":"loaded"}],"default_id":"gemma-box::gemma-26b"}""").build())

        assertEquals(listOf("gemma-box::gemma-26b"), ChatViewModel.fetchLiveModelIds(api, "custom"))
        assertEquals("/models/custom", server.takeRequest().target)
    }

    @Test fun `an empty live catalog is an empty list, not unknown`() = runTest {
        server.enqueue(MockResponse.Builder().body("""{"models":[]}""").build())
        assertEquals(emptyList<String>(), ChatViewModel.fetchLiveModelIds(api, "anthropic"))
    }

    @Test fun `unknown when the request fails`() = runTest {
        server.enqueue(MockResponse.Builder().code(503).body("""{"detail":"down"}""").build())
        assertNull(ChatViewModel.fetchLiveModelIds(api, "anthropic"))
    }

    @Test fun `unknown when the body has no model list`() = runTest {
        server.enqueue(MockResponse.Builder().body("""{"error":"nope"}""").build())
        assertNull(ChatViewModel.fetchLiveModelIds(api, "openai"))
    }

    @Test fun `unknown without an api or a model endpoint, and nothing is requested`() = runTest {
        assertNull(ChatViewModel.fetchLiveModelIds(null, "anthropic"))
        for (p in listOf("local", "agents", "gemini-live", "robotics")) {
            assertNull(p, ChatViewModel.fetchLiveModelIds(api, p))
        }
        assertEquals(0, server.requestCount)
    }
}
