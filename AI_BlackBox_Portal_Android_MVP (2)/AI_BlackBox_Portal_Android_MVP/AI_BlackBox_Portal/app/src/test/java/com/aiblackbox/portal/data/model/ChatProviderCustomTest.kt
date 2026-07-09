package com.aiblackbox.portal.data.model

import com.aiblackbox.portal.ui.chat.ChatViewModel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task 7.1 — the `custom` provider (user-registered OpenAI-compatible servers
 * on the box) as a [ChatProvider] member. Mirrors [ChatProviderLocalTest].
 *
 * Locks the enum contract the picker + ChatViewModel hydration rely on:
 *   - `fromId("custom")` resolves to [ChatProvider.CUSTOM] (round-trips its
 *     id — the fromId fallback-to-GEMINI trap is dead for "custom").
 *   - CUSTOM.isStreaming is TRUE — its turn goes over the normal cloud SSE
 *     path (the BOX dispatches to the registered server); it is deliberately
 *     NOT isLocal (that means "runs on the phone"), nor agent/voice/robotics.
 *   - `mapProviderForApi("custom") == "custom"` — without the mapping the
 *     model-list hydration silently no-ops (unknown → null → Constants
 *     fallback forever).
 */
class ChatProviderCustomTest {

    @Test fun `fromId custom resolves to CUSTOM`() {
        assertEquals(ChatProvider.CUSTOM, ChatProvider.fromId("custom"))
        assertEquals("custom", ChatProvider.CUSTOM.id)
    }

    @Test fun `CUSTOM is not agent, voice, robotics, or local`() {
        assertFalse(ChatProvider.CUSTOM.isAgent)
        assertFalse(ChatProvider.CUSTOM.isVoice)
        assertFalse(ChatProvider.CUSTOM.isRobotics)
        assertFalse(
            "CUSTOM runs on the box's registered servers, not on the phone",
            ChatProvider.CUSTOM.isLocal,
        )
    }

    @Test fun `CUSTOM isStreaming is true (normal cloud SSE path to the box)`() {
        assertTrue(
            "CUSTOM must be a streaming/SSE provider — the box dispatches the turn",
            ChatProvider.CUSTOM.isStreaming,
        )
    }

    @Test fun `mapProviderForApi maps custom to custom (hydration is not a no-op)`() {
        assertEquals("custom", ChatViewModel.mapProviderForApi("custom"))
    }

    @Test fun `mapProviderForApi existing mappings are unchanged`() {
        assertEquals("google", ChatViewModel.mapProviderForApi("gemini"))
        assertEquals("computer-use", ChatViewModel.mapProviderForApi("computer-use"))
        assertNull(ChatViewModel.mapProviderForApi("gemini-live"))
    }
}
