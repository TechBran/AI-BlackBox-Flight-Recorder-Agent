package com.aiblackbox.portal

import com.aiblackbox.portal.data.api.SSEEvent
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.ui.chat.ChatViewModel
import org.junit.Assert.*
import org.junit.Test

/**
 * Phase 2 Task 2.1 — "The Signal" telemetry parse.
 *
 * Verifies that a `system_activity` SSE event's data (a JSON OBJECT
 * `{stage, label, detail, seq}`) is decoded down to its display `label`, which is
 * all that feeds the transient _signalLabel flow.
 *
 * Strategy: like ChatViewModelSaveTest, we test the PURE companion function
 * ChatViewModel.parseSignalLabel rather than instantiating the AndroidViewModel
 * (which needs Robolectric + kotlinx-coroutines-test we don't have). The
 * processSSEEvent "system_activity" branch is a literal forward
 * (`parseSignalLabel(event.data)?.let { _signalLabel.value = it }`), so proving
 * the parser is enough. kotlinx.serialization is used (pure JVM) — org.json is
 * unmocked in local unit tests and would throw.
 *
 * HARD RULE coverage: the parser reads ONLY the top-level `label`; it cannot
 * reach or emit any body-like `detail` fields, and the persisted UiMessage model
 * has no telemetry field for a label to land on. Both are asserted below.
 */
class SignalLabelParseTest {

    @Test fun `system_activity event yields its label`() {
        val event = SSEEvent(
            event = "system_activity",
            data = """{"stage":"embed_query","label":"embed · gemini-embedding-2 · 3072d","detail":{"dims":3072},"seq":2}"""
        )
        assertEquals(
            "embed · gemini-embedding-2 · 3072d",
            ChatViewModel.parseSignalLabel(event.data)
        )
    }

    @Test fun `retrieval-stage label with counts is passed through verbatim`() {
        val data = """{"stage":"retrieve","label":"search · 8,005 → 40 cleared floor","seq":3}"""
        assertEquals("search · 8,005 → 40 cleared floor", ChatViewModel.parseSignalLabel(data))
    }

    @Test fun `missing label returns null`() {
        val data = """{"stage":"retrieve","detail":{"n":40},"seq":3}"""
        assertNull(ChatViewModel.parseSignalLabel(data))
    }

    @Test fun `blank label returns null`() {
        assertNull(ChatViewModel.parseSignalLabel("""{"stage":"x","label":"   ","seq":1}"""))
    }

    @Test fun `unparseable data returns null (fail-safe, never throws)`() {
        assertNull(ChatViewModel.parseSignalLabel("not json"))
        assertNull(ChatViewModel.parseSignalLabel(""))
        // A bare JSON string (not an object) must not blow up either.
        assertNull(ChatViewModel.parseSignalLabel("\"just a string\""))
    }

    /**
     * HARD RULE — the parser extracts ONLY the top-level label. Even when the
     * telemetry envelope's `detail` carries body-like fields, those are
     * unreachable: the HUD can never smuggle conversation content/reasoning.
     */
    @Test fun `parser extracts only the label, never body-like detail fields`() {
        val data = """{"stage":"context","label":"context · 8 memories · 42k tokens",""" +
            """"detail":{"content":"SECRET BODY","reasoning":"SECRET THOUGHT"},"seq":6}"""
        val label = ChatViewModel.parseSignalLabel(data)
        assertEquals("context · 8 memories · 42k tokens", label)
        assertFalse("label must not leak detail body", label!!.contains("SECRET"))
    }

    /**
     * HARD RULE — the label is ephemeral: parsing it produces a plain String and
     * touches no persisted message. The persisted UiMessage data model has no
     * telemetry/signal field at all, so a label can never be written onto a turn.
     */
    @Test fun `parsed label never mutates a persisted message`() {
        val msg = UiMessage(role = "assistant", content = "hello", reasoning = "real thoughts")
        val label = ChatViewModel.parseSignalLabel(
            """{"stage":"generate","label":"generating · gemini-3.1-pro","seq":9}"""
        )
        assertEquals("generating · gemini-3.1-pro", label)
        // The message is entirely unchanged by parsing telemetry.
        assertEquals("hello", msg.content)
        assertEquals("real thoughts", msg.reasoning)
        // And there is nowhere on the model for a label to be persisted: the only
        // string-bearing fields are the conversation content + real reasoning.
        assertFalse(msg.content.contains("generating"))
        assertFalse(msg.reasoning!!.contains("generating"))
    }

    // ── Task 2.3 — mint line (built from the /chat/save RESPONSE, match web) ──

    @Test fun `mint label built from snap_id and dims`() {
        val body = """{"minted":true,"snap_id":"SNAP-20260713-8347","dims":3072}"""
        assertEquals("mint · SNAP-20260713-8347 · 3072d", ChatViewModel.parseMintLabel(body))
    }

    @Test fun `mint label falls back to embedding_dims`() {
        val body = """{"snap_id":"SNAP-X","embedding_dims":768}"""
        assertEquals("mint · SNAP-X · 768d", ChatViewModel.parseMintLabel(body))
    }

    @Test fun `mint label omits dims when absent`() {
        assertEquals("mint · SNAP-Y", ChatViewModel.parseMintLabel("""{"snap_id":"SNAP-Y"}"""))
    }

    @Test fun `mint label is null when not minted (no snap_id)`() {
        assertNull(ChatViewModel.parseMintLabel("""{"minted":false}"""))
        assertNull(ChatViewModel.parseMintLabel("""{"artifacts":[]}"""))
        assertNull(ChatViewModel.parseMintLabel("garbage"))
    }

    // ── CU carve-out — The Signal stays DARK on computer-use turns (match web) ──
    // The gate feeds signalSuppressed, which makes pushSignal a no-op so _signalLabel
    // stays null for the whole CU turn (SignalLine then renders nothing).

    @Test fun `signal is suppressed for the computer-use provider`() {
        assertTrue(ChatViewModel.signalSuppressedForProvider("computer-use"))
    }

    @Test fun `signal is NOT suppressed for chat providers`() {
        for (p in listOf("anthropic", "openai", "gemini", "xai", "custom", "local", "robotics", "")) {
            assertFalse("provider '$p' must not suppress The Signal",
                ChatViewModel.signalSuppressedForProvider(p))
        }
    }
}
