package com.aiblackbox.portal.data.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task 1.6 — the `local` on-device (Gemma) provider as a [ChatProvider] member.
 *
 * Locks the enum contract the picker gating + ChatViewModel routing rely on:
 *   - `fromId("local")` resolves to [ChatProvider.LOCAL] (round-trips its id).
 *   - LOCAL.isLocal is true; it is neither agent, voice, nor robotics.
 *   - LOCAL.isStreaming is FALSE — its turn runs on-device, NOT via the cloud
 *     SSE path, so the streaming/SSE branch must never treat it as a cloud
 *     streaming provider.
 */
class ChatProviderLocalTest {

    @Test fun `fromId local resolves to LOCAL`() {
        assertEquals(ChatProvider.LOCAL, ChatProvider.fromId("local"))
        assertEquals("local", ChatProvider.LOCAL.id)
    }

    @Test fun `LOCAL isLocal is true`() {
        assertTrue(ChatProvider.LOCAL.isLocal)
    }

    @Test fun `LOCAL is not agent, voice, or robotics`() {
        assertFalse(ChatProvider.LOCAL.isAgent)
        assertFalse(ChatProvider.LOCAL.isVoice)
        assertFalse(ChatProvider.LOCAL.isRobotics)
    }

    @Test fun `LOCAL isStreaming is false (on-device, not cloud SSE)`() {
        assertFalse(
            "LOCAL must NOT be a streaming/SSE provider — its turn runs on-device",
            ChatProvider.LOCAL.isStreaming,
        )
    }

    @Test fun `no other provider claims isLocal`() {
        ChatProvider.entries.filter { it != ChatProvider.LOCAL }.forEach {
            assertFalse("${it.id} must not be isLocal", it.isLocal)
        }
    }
}
