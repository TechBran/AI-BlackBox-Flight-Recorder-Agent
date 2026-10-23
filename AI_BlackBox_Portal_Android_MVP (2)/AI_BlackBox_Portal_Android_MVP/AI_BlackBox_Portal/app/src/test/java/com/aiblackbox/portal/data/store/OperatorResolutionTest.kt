package com.aiblackbox.portal.data.store

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A09-data-local-model-05 — the operator comes from the box, never from a seed name.
 * [OperatorResolver] is the ONE 0/1/many rule (the Portal's chooseOperator is its twin):
 * a stored pick the box still lists stands; otherwise the box's only person is used
 * (reserved overseers never count); several people → the operator picks; nobody →
 * guidance. Pure, so every case runs on the JVM.
 */
class OperatorResolutionTest {

    private fun roster(vararg ops: String, reserved: List<String> = emptyList(), default: String = "") =
        OperatorRoster(operators = ops.toList(), default = default, reserved = reserved)

    // ── GET /operators parsing ───────────────────────────────────────────────

    @Test fun `the roster parses operators, default and the reserved overseers`() {
        val r = OperatorResolver.parseRoster(
            """{"operators":["Flight Recorder","Ana","Ben"],"default":"Ana","reserved":["Flight Recorder"]}"""
        )!!
        assertEquals(listOf("Flight Recorder", "Ana", "Ben"), r.operators)
        assertEquals("Ana", r.default)
        assertEquals(listOf("Flight Recorder"), r.reserved)
        assertEquals(listOf("Ana", "Ben"), r.people)
    }

    @Test fun `an older server without reserved, blanks and junk entries still parse`() {
        val r = OperatorResolver.parseRoster("""{"operators":["Ana"," ",null,7,{"x":1}],"extra":true}""")!!
        assertEquals(listOf("Ana"), r.operators)
        assertEquals("", r.default)
        assertEquals(emptyList<String>(), r.reserved)
    }

    @Test fun `an empty roster is a box with nobody on it, not an unknown one`() {
        assertEquals(emptyList<String>(), OperatorResolver.parseRoster("""{"operators":[]}""")!!.operators)
    }

    @Test fun `a malformed reply is unknown, so nothing is changed on its account`() {
        for (raw in listOf("", "not json", "[]", """{"default":"Ana"}""", """{"operators":"Ana"}""")) {
            assertNull(raw, OperatorResolver.parseRoster(raw))
        }
    }

    // ── the 0/1/many rule ────────────────────────────────────────────────────

    @Test fun `a stored pick the box still lists is kept`() {
        val r = roster("Flight Recorder", "Ana", "Ben", reserved = listOf("Flight Recorder"))
        assertEquals(OperatorResolution.Resolved("Ben"), OperatorResolver.resolve("Ben", r))
        assertEquals(OperatorResolution.Resolved("Ben"), OperatorResolver.resolve("  Ben ", r))
        // An explicit pick of the overseer is still a pick.
        assertEquals(OperatorResolution.Resolved("Flight Recorder"), OperatorResolver.resolve("Flight Recorder", r))
    }

    @Test fun `one person on the box is picked automatically, never the overseer`() {
        val r = roster("Flight Recorder", "Ana", reserved = listOf("Flight Recorder"), default = "Flight Recorder")
        assertEquals(OperatorResolution.Resolved("Ana"), OperatorResolver.resolve("", r))
        // A stale pick (operator removed on the box, or another box re-paired) re-resolves.
        assertEquals(OperatorResolution.Resolved("Ana"), OperatorResolver.resolve("Gone", r))
    }

    @Test fun `several people and no valid pick open the picker, with no silent default`() {
        val r = roster("Flight Recorder", "Ana", "Ben", reserved = listOf("Flight Recorder"), default = "Ana")
        assertEquals(OperatorResolution.Choose(listOf("Flight Recorder", "Ana", "Ben")), OperatorResolver.resolve("", r))
        assertEquals(OperatorResolution.Choose(listOf("Flight Recorder", "Ana", "Ben")), OperatorResolver.resolve("Gone", r))
    }

    @Test fun `nobody on the box asks for one to be added`() {
        assertEquals(OperatorResolution.NoOperators, OperatorResolver.resolve("", roster()))
        assertEquals(OperatorResolution.NoOperators, OperatorResolver.resolve("Gone", roster()))
        assertEquals(
            OperatorResolution.NoOperators,
            OperatorResolver.resolve("", roster("Flight Recorder", reserved = listOf("Flight Recorder"))),
        )
    }

    // ── what the app does with it ────────────────────────────────────────────

    @Test fun `each resolution maps to one prompt for the top bar`() {
        assertEquals(OperatorPrompt.NONE, OperatorResolution.Resolved("Ana").prompt)
        assertEquals(OperatorPrompt.CHOOSE, OperatorResolution.Choose(listOf("Ana", "Ben")).prompt)
        assertEquals(OperatorPrompt.ADD_FIRST, OperatorResolution.NoOperators.prompt)
    }

    @Test fun `a send is blocked, with a message, exactly while no operator is resolved`() {
        assertNull(OperatorResolver.sendBlockedReason("Ana", OperatorPrompt.NONE))
        assertEquals(OperatorResolver.CHOOSE_MESSAGE, OperatorResolver.sendBlockedReason("", OperatorPrompt.CHOOSE))
        assertEquals(OperatorResolver.CHOOSE_MESSAGE, OperatorResolver.sendBlockedReason("  ", OperatorPrompt.NONE))
        assertEquals(OperatorResolver.NO_OPERATORS_MESSAGE, OperatorResolver.sendBlockedReason("", OperatorPrompt.ADD_FIRST))
        for (m in listOf(OperatorResolver.CHOOSE_MESSAGE, OperatorResolver.NO_OPERATORS_MESSAGE)) {
            assertTrue(m, m.contains("operator", ignoreCase = true))
        }
    }

    // ── the floating overlay ─────────────────────────────────────────────────

    @Test fun `the overlay spinner starts on the hand-over, else the same rule, else the box default`() {
        val r = roster("Flight Recorder", "Ana", "Ben", reserved = listOf("Flight Recorder"), default = "Ben")
        assertEquals("Ana", OperatorResolver.forSpinner("Ana", r))
        // Several people and no hand-over: the box default, shown in the spinner to change.
        assertEquals("Ben", OperatorResolver.forSpinner("", r))
        assertEquals("Ben", OperatorResolver.forSpinner("Gone", r))
        // A default the box no longer lists falls to the first person, never the overseer.
        assertEquals("Ana", OperatorResolver.forSpinner("", r.copy(default = "Gone")))
        assertEquals("Ana", OperatorResolver.forSpinner("", roster("Flight Recorder", "Ana", reserved = listOf("Flight Recorder"))))
        assertEquals("", OperatorResolver.forSpinner("", roster()))
    }

    @Test fun `the web Portal's operator is read back from evaluateJavascript's JSON result`() {
        assertEquals("Ana Lee", OperatorResolver.fromJsResult("\"Ana Lee\""))
        assertEquals("Ana \"A\"", OperatorResolver.fromJsResult("\"Ana \\\"A\\\"\""))
        for (raw in listOf(null, "", "null", "undefined", "\"\"", "\"  \"", "42", "{}", "garbage")) {
            assertEquals(raw.toString(), "", OperatorResolver.fromJsResult(raw))
        }
    }

    @Test fun `the stored operator is blank until something resolves it`() {
        assertEquals("", BlackBoxStore.operatorOf(androidx.datastore.preferences.core.emptyPreferences()))
        val prefs = androidx.datastore.preferences.core.mutablePreferencesOf(BlackBoxStore.KEY_OPERATOR to " Ana ")
        assertEquals("Ana", BlackBoxStore.operatorOf(prefs))
    }

    // ── a load that lands after an operator switch ──────────────────────────

    @Test fun `a load started before an operator switch is stale once the switch lands`() {
        val generation = OperatorGeneration()
        val beforeSwitch = generation.tag()      // the 10 s poll fires as Ana
        generation.advance()                     // the top bar switches to Ben
        val afterSwitch = generation.tag()       // the switch's own reload
        assertTrue(generation.isCurrent(afterSwitch))
        // Ana's reply arrives last: it must not replace Ben's list.
        assertEquals(false, generation.isCurrent(beforeSwitch))
        generation.advance()
        assertEquals(false, generation.isCurrent(afterSwitch))
    }
}
