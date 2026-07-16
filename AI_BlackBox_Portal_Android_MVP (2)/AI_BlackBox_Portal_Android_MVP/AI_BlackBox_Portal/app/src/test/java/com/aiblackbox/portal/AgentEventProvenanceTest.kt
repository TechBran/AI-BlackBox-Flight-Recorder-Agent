package com.aiblackbox.portal

import com.aiblackbox.portal.data.agent.AgentEvent
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.ui.chat.cliLiveStatusLabel
import com.aiblackbox.portal.ui.chat.cliLiveStreamPhase
import com.aiblackbox.portal.ui.chat.LiveStreamPhase
import com.aiblackbox.portal.ui.chat.cliLiveEdgeSection
import com.aiblackbox.portal.ui.chat.reduceActiveTool
import com.aiblackbox.portal.ui.chat.ToolIndicatorData
import com.aiblackbox.portal.ui.components.LiveTextSection
import org.junit.Assert.*
import org.junit.Test

/**
 * Plan Task 10 — guards the agent WS event surface that AgentChatScreen
 * pattern-matches against. ProvenanceUpdate must carry a typed Provenance
 * (not a raw string) so the screen's handleEvent branch can store it on
 * UiMessage.provenance directly without re-parsing.
 */
class AgentEventProvenanceTest {

    @Test fun `CLI live status maps thinking tool and status to one label`() {
        // Signal vocabulary: honest STATE words ("thinking"), and the same
        // "tool · <name>" shape the main-chat Signal renders — never a canned
        // phrase from the old fake REASONING_PHRASES roster.
        assertEquals("thinking", cliLiveStatusLabel(true, null, "Thinking..."))
        assertEquals("tool · Read", cliLiveStatusLabel(false, "Read", "Running"))
        assertEquals("Running", cliLiveStatusLabel(false, null, "Running"))
        assertNull(cliLiveStatusLabel(false, null, ""))
    }

    @Test fun `CLI live phase switches from tool activity to answer tracking`() {
        assertEquals(
            LiveStreamPhase.TOOL,
            cliLiveStreamPhase(isStreaming = true, isThinking = false, activeTool = "Read"),
        )
        assertEquals(
            LiveStreamPhase.ANSWERING,
            cliLiveStreamPhase(isStreaming = true, isThinking = false, activeTool = null),
        )
    }

    @Test fun `CLI phase selects exactly one callback anchor with tool fallback precedence`() {
        assertEquals(LiveTextSection.REASONING, cliLiveEdgeSection(LiveStreamPhase.THINKING))
        assertEquals(LiveTextSection.ANSWER, cliLiveEdgeSection(LiveStreamPhase.ANSWERING))
        assertEquals(LiveTextSection.TOOL_FALLBACK, cliLiveEdgeSection(LiveStreamPhase.TOOL))
        assertNull(cliLiveEdgeSection(LiveStreamPhase.IDLE))
    }

    @Test fun `content result error disconnect and completion clear active tool`() {
        val active = ToolIndicatorData("Read", "", "file.kt")
        val terminalOrProseEvents = listOf(
            AgentEvent.Content("answer"),
            AgentEvent.ToolResult("ok"),
            AgentEvent.Error("failed"),
            AgentEvent.Disconnected,
            AgentEvent.Completed(1),
        )
        terminalOrProseEvents.forEach { event ->
            assertNull("$event must clear the tool anchor", reduceActiveTool(active, event))
        }
        assertEquals(active, reduceActiveTool(active, AgentEvent.StatusUpdate()))
    }

    @Test fun `AgentEvent ProvenanceUpdate carries a typed Provenance`() {
        val prov = Provenance(
            recent = listOf("SNAP-AR"),
            keyword = listOf("SNAP-AK"),
            semantic = listOf("SNAP-AS"),
            checkpoint = listOf("SNAP-AC"),
        )
        val event: AgentEvent = AgentEvent.ProvenanceUpdate(prov)
        assertTrue(event is AgentEvent.ProvenanceUpdate)
        val payload = (event as AgentEvent.ProvenanceUpdate).provenance
        assertEquals(prov, payload)
        assertEquals(4, payload.totalCount())
    }

    @Test fun `AgentEvent ProvenanceUpdate equality works for state diffing`() {
        val a = AgentEvent.ProvenanceUpdate(Provenance(recent = listOf("X")))
        val b = AgentEvent.ProvenanceUpdate(Provenance(recent = listOf("X")))
        val c = AgentEvent.ProvenanceUpdate(Provenance(recent = listOf("Y")))
        assertEquals(a, b)
        assertNotEquals(a, c)
    }
}
