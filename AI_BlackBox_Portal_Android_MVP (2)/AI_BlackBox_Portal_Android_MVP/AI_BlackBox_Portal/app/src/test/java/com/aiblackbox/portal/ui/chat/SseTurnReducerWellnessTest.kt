package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.api.SseFrameParser
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.data.wellness.WellnessUsed
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The health half of the /chat/stream reducer (spec §3.7, §6.1 "Chat"): stream_start's
 * `wellness_turn` is kept for /chat/save, and the UI-only `wellness_used` frames land on the
 * turn being built (text or no text yet), never in the reply, the reasoning or the Signal.
 */
class SseTurnReducerWellnessTest {

    private fun fold(stream: String, provider: String = "anthropic"): SseTurnState {
        var turn = SseTurnReducer.start(provider, "")
        var chat = ChatState.STREAMING
        for (e in SseFrameParser.parse(stream)) {
            val step = SseTurnReducer.reduce(turn, e, chat, provider, "http://box:9091", nowMs = 42)
            turn = step.turn
            step.chatState?.let { chat = it }
        }
        return turn
    }

    private val wellnessTurn = """
        event: stream_start
        data: {"provider": "anthropic", "model": "claude-test", "provenance": {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}, "wellness_turn": "wt_0123456789abcdef"}

        event: tool_start
        data: "wellness_summary"

        event: tool_result
        data: "wellness_summary: health data read"

        event: wellness_used
        data: {"ref": "w-20261028-ab12", "categories": ["sleep", "heart"], "as_of": "2026-10-28T07:10-04:00", "provider": "anthropic", "provider_label": "Anthropic", "future_field": [1, 2]}

        event: content_start
        data: ""

        event: content
        data: "You slept a little less than usual."

        event: done
        data: {"content": "You slept a little less than usual.", "reasoning": ""}

        event: stream_end
        data: ""

    """.trimIndent()

    @Test
    fun `stream_start's turn key and the frame's use land on the turn, not in the reply`() {
        val t = fold(wellnessTurn)
        assertEquals("wt_0123456789abcdef", t.wellnessTurn)
        assertEquals(WellnessUsed("w-20261028-ab12", listOf("w-20261028-ab12"), listOf("sleep", "heart"),
            "2026-10-28T07:10-04:00", "Anthropic"), t.wellnessUsed)
        assertEquals("You slept a little less than usual.", t.content)
        assertEquals("", t.reasoning)
        assertFalse(t.content.contains("w-20261028"))
        assertEquals("claude-test", t.model)
    }

    @Test
    fun `a frame before any assistant text still belongs to the turn`() {
        val t = fold("""
            event: wellness_used
            data: {"ref": "w-20261028-ab12", "categories": ["sleep"]}

        """.trimIndent())
        assertEquals("w-20261028-ab12", t.wellnessUsed?.ref)
        assertEquals("", t.content)
        assertNull(t.wellnessTurn)
    }

    @Test
    fun `two health tools in one turn merge, the first ref tags the bubble`() {
        val t = fold("""
            event: wellness_used
            data: {"ref": "w-20261028-aaaa", "categories": ["sleep"], "as_of": "2026-10-28T07:10-04:00", "provider_label": "Anthropic"}

            event: wellness_used
            data: {"ref": "w-20261028-bbbb", "categories": ["activity"], "as_of": "2026-10-28T11:55-04:00"}

        """.trimIndent())
        val used = t.wellnessUsed!!
        assertEquals("w-20261028-aaaa", used.ref)
        assertEquals(listOf("w-20261028-aaaa", "w-20261028-bbbb"), used.refs)
        assertEquals(listOf("sleep", "activity"), used.categories)
        assertEquals("2026-10-28T11:55-04:00", used.asOf)
        assertEquals("Anthropic", used.providerLabel)
    }

    @Test
    fun `a stream without health, or with a malformed frame, leaves no trace`() {
        val plain = fold("""
            event: stream_start
            data: {"provider": "anthropic", "model": "claude-test"}

            event: content
            data: "Hello."

        """.trimIndent())
        assertNull(plain.wellnessTurn)
        assertNull(plain.wellnessUsed)
        val junk = fold("""
            event: stream_start
            data: {"provider": "anthropic", "wellness_turn": null}

            event: wellness_used
            data: {"categories": ["sleep"]}

            event: wellness_used
            data: not json

            event: wellness_used
            data: {"ref": "w-bad", "categories": ["sleep"]}

        """.trimIndent())
        assertNull(junk.wellnessTurn)
        assertNull(junk.wellnessUsed)
        assertEquals("", junk.content)
    }

    @Test
    fun `the bubble carries the use, its first ref as wellnessRef, and keeps it on later updates`() {
        val used = WellnessUsed("w-20261028-ab12", categories = listOf("sleep"))
        val m = ChatViewModel.withWellnessUsed(UiMessage(role = "assistant", content = "x"), used)
        assertEquals("w-20261028-ab12", m.wellnessRef)
        assertEquals(used, m.wellnessUsed)
        // an update without a frame leaves what the bubble has
        assertEquals(m, ChatViewModel.withWellnessUsed(m, null))
    }
}
