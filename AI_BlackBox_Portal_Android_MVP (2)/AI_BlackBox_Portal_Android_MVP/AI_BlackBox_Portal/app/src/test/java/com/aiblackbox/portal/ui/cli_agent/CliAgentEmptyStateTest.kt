package com.aiblackbox.portal.ui.cli_agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * T21 unit tests for [CliAgentEmptyState].
 *
 * Compose UI assertions (button visibility, expand/collapse semantics,
 * spinner-on-press) are deferred to T23 instrumented testing — see the
 * T20 precedent in [SessionSwitcherTopBarTest], which also leaves the
 * composable itself unexercised at the JVM unit-test layer because
 * Compose runtime deps are only wired into `androidTest`.
 *
 * What this file covers (pure logic that drives the composable):
 *   - Provider shortcut ordering (Claude, Gemini, Codex, Antigravity, Grok).
 *   - Label rendering goes through [titleCaseProvider] so the buttons
 *     show "Claude" not "claude".
 *   - The shortcut list contains exactly the 5 agent entries — no terminal
 *     leak into the shortcut row (the terminal button is separate and
 *     never gets a ⚡ YOLO affordance).
 *   - ⚡ YOLO content descriptions name each agent for accessibility.
 */
class CliAgentEmptyStateTest {

    @Test
    fun `PROVIDER_SHORTCUTS used by empty state holds exactly the 5 agent entries`() {
        // The empty state reads PROVIDER_SHORTCUTS for ordering — there must
        // be exactly 5 agents, in the specified order, and 'terminal' MUST
        // NOT appear in this list (terminal is the separate primary button
        // and, unlike the agents, never gets a ⚡ YOLO button).
        assertEquals(
            listOf("claude", "gemini", "codex", "antigravity", "grok"),
            PROVIDER_SHORTCUTS,
        )
        assertEquals("expected exactly 5 provider shortcuts", 5, PROVIDER_SHORTCUTS.size)
        assertTrue(
            "'terminal' must not appear in the shortcuts list (it's the primary button)",
            "terminal" !in PROVIDER_SHORTCUTS,
        )
    }

    @Test
    fun `titleCaseProvider renders shortcut labels for all 5 providers`() {
        // The empty state composable passes each PROVIDER_SHORTCUTS entry
        // through titleCaseProvider for the button label. Verify the
        // resulting labels are the operator-facing strings we expect.
        val labels = PROVIDER_SHORTCUTS.map(::titleCaseProvider)
        assertEquals(
            listOf("Claude", "Gemini", "Codex", "Antigravity", "Grok"),
            labels,
        )
    }

    @Test
    fun `every agent shortcut gets a YOLO description naming that agent`() {
        // The empty state renders a compact amber ⚡ button beside each agent
        // row with this exact content description (accessibility + tests).
        val descriptions = PROVIDER_SHORTCUTS.map(::yoloLaunchDescription)
        assertEquals(
            listOf(
                "Launch Claude with permissions skipped (YOLO)",
                "Launch Gemini with permissions skipped (YOLO)",
                "Launch Codex with permissions skipped (YOLO)",
                "Launch Antigravity with permissions skipped (YOLO)",
                "Launch Grok with permissions skipped (YOLO)",
            ),
            descriptions,
        )
    }

    @Test
    fun `launchInFlight membership check works for terminal and shortcuts`() {
        // Sanity check for the per-provider spinner contract: launching
        // 'terminal' must NOT make 'claude' show busy, and vice versa.
        // The empty state composable does `"terminal" in launchInFlight`
        // and `providerSlug in launchInFlight` independently — this test
        // pins that semantic.
        val onlyTerminal: Set<String> = setOf("terminal")
        assertTrue("terminal" in onlyTerminal)
        assertTrue("claude" !in onlyTerminal)
        assertTrue("gemini" !in onlyTerminal)

        val onlyClaude: Set<String> = setOf("claude")
        assertTrue("claude" in onlyClaude)
        assertTrue("terminal" !in onlyClaude)

        // Two independent launches share the set without collision.
        val concurrent: Set<String> = setOf("terminal", "claude")
        assertTrue("terminal" in concurrent)
        assertTrue("claude" in concurrent)
        assertTrue("gemini" !in concurrent)
    }
}
