package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.model.ChatProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task 1.6 — selecting the `local` provider hits a SAFE placeholder branch in
 * [ChatViewModel.sendMessage] and never falls through to the cloud SSE path
 * (which would POST provider=local to the orchestrator and error). Phase 2
 * replaces this branch with the real on-device engine.
 *
 * Same strategy as [com.aiblackbox.portal.ChatViewModelSaveTest]: rather than
 * instantiate the AndroidViewModel (needs Robolectric + a coroutines-test dep
 * the offline gate lacks), we test the pure pieces the production routing
 * delegates to:
 *   - [ChatViewModel.routeFor] — the branch selector sendMessage() switches on.
 *     It must return [ChatRoute.LOCAL_PLACEHOLDER] for local and never SSE.
 *   - [ChatViewModel.buildLocalPlaceholder] — the assistant message the branch
 *     appends. It must NOT be streaming (no SSE handshake) and must carry the
 *     friendly "wiring lands next update" copy.
 */
class ChatViewModelLocalRoutingTest {

    @Test fun `routeFor local selects the placeholder branch, not SSE`() {
        assertEquals(ChatRoute.LOCAL_PLACEHOLDER, ChatViewModel.routeFor(ChatProvider.LOCAL, erMissionActive = false))
    }

    @Test fun `routeFor cloud providers selects SSE`() {
        assertEquals(ChatRoute.SSE, ChatViewModel.routeFor(ChatProvider.GEMINI, erMissionActive = false))
        assertEquals(ChatRoute.SSE, ChatViewModel.routeFor(ChatProvider.ANTHROPIC, erMissionActive = false))
    }

    @Test fun `routeFor agent and voice are unchanged`() {
        assertEquals(ChatRoute.AGENT, ChatViewModel.routeFor(ChatProvider.AGENTS, erMissionActive = false))
        assertEquals(ChatRoute.VOICE, ChatViewModel.routeFor(ChatProvider.REALTIME, erMissionActive = false))
    }

    @Test fun `LOCAL is not a streaming provider so it can never reach the SSE else branch`() {
        // Defense-in-depth with routeFor: even the trait the else branch keys on
        // excludes LOCAL.
        assertFalse(ChatProvider.LOCAL.isStreaming)
    }

    @Test fun `local placeholder message is not streaming and carries friendly copy`() {
        val msg = ChatViewModel.buildLocalPlaceholder(provider = "local", model = "gemma-4-e2b")
        assertEquals("assistant", msg.role)
        assertFalse("placeholder must not be a streaming message — no SSE", msg.isStreaming)
        assertFalse(msg.isThinking)
        assertTrue("mentions on-device", msg.content.contains("on-device", ignoreCase = true))
        assertEquals("local", msg.provider)
    }
}
