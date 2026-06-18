package com.aiblackbox.portal.data.local

import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-helper tests for the per-turn tool-result trim + cumulative budget
 * (snapshot-ledger Task 9). These mirror the [overCap] boundary tests in
 * [LiteRtMappersTest]: [trimToolResult] / [overTurnBudget] are PURE so the
 * decisions are JVM-unit-testable under JDK 17; the enforcement inside
 * [nativeOpenApiToolFor]'s `execute` is device/compile-verified.
 */
class TurnBudgetTest {

    // -------------------------------------------------------------------------
    // trimToolResult -- a single oversized tool result can't blow the 16K window
    // on its own. Short inputs pass through untouched; long inputs are truncated
    // to maxChars and a clear marker is appended.
    // -------------------------------------------------------------------------

    @Test
    fun `trimToolResult returns short input unchanged`() {
        val short = "small tool result"
        assertEquals("under the cap -> identical content", short, trimToolResult(short))
    }

    @Test
    fun `trimToolResult returns input of exactly maxChars unchanged`() {
        // length == maxChars is NOT over -> pass through (idempotent boundary).
        val exact = "x".repeat(MAX_TOOL_RESULT_CHARS)
        assertEquals(exact, trimToolResult(exact))
    }

    @Test
    fun `trimToolResult truncates a long input and appends the marker`() {
        val long = "y".repeat(MAX_TOOL_RESULT_CHARS * 3)
        val trimmed = trimToolResult(long)
        val marker = "\n[…tool result truncated for context budget]"
        assertTrue("oversized input must be truncated", trimmed.length < long.length)
        assertTrue("marker must be present", trimmed.endsWith(marker))
        // Kept body == maxChars, total == maxChars + marker length.
        assertEquals(MAX_TOOL_RESULT_CHARS + marker.length, trimmed.length)
        assertTrue(
            "result length <= maxChars + marker length",
            trimmed.length <= MAX_TOOL_RESULT_CHARS + marker.length,
        )
    }

    @Test
    fun `trimToolResult honors a custom maxChars`() {
        val long = "z".repeat(50)
        val trimmed = trimToolResult(long, maxChars = 10)
        assertTrue("body kept == custom maxChars prefix", trimmed.startsWith("z".repeat(10)))
        assertTrue("marker present", trimmed.contains("truncated for context budget"))
    }

    @Test
    fun `trimToolResult does not throw on empty input`() {
        assertEquals("", trimToolResult(""))
    }

    // -------------------------------------------------------------------------
    // overTurnBudget -- PURE boundary decision the native loop consults BEFORE
    // running the next tool: have the (trimmed) tool results fed back this turn
    // exceeded the per-turn budget? Same shape as overCap: false at the boundary,
    // true strictly above it.
    // -------------------------------------------------------------------------

    @Test
    fun `overTurnBudget is false below the cap`() {
        assertFalse("0 chars -> within budget", overTurnBudget(usedChars = 0, maxChars = 40000))
        assertFalse("just under cap -> within budget", overTurnBudget(usedChars = 39999, maxChars = 40000))
    }

    @Test
    fun `overTurnBudget is false at exactly the cap`() {
        // usedChars == maxChars is the LAST allowed state (mirrors overCap == max).
        assertFalse("exactly at cap -> still within budget", overTurnBudget(usedChars = 40000, maxChars = 40000))
    }

    @Test
    fun `overTurnBudget is true above the cap`() {
        assertTrue("one over cap -> soft-stop", overTurnBudget(usedChars = 40001, maxChars = 40000))
        assertTrue("far over cap -> soft-stop", overTurnBudget(usedChars = 1_000_000, maxChars = 40000))
    }

    @Test
    fun `overTurnBudget honors the real MAX_TURN_TOOL_RESULT_CHARS constant`() {
        val max = MAX_TURN_TOOL_RESULT_CHARS
        assertFalse("at the constant cap -> within budget", overTurnBudget(usedChars = max, maxChars = max))
        assertTrue("one past the constant cap -> soft-stop", overTurnBudget(usedChars = max + 1, maxChars = max))
    }

    // -------------------------------------------------------------------------
    // Accumulation: trimmed results summed across N calls flip the budget at the
    // expected call (each trimmed call contributes at most MAX_TOOL_RESULT_CHARS).
    // -------------------------------------------------------------------------

    @Test
    fun `accumulated trimmed results flip overTurnBudget at the expected call`() {
        // Each tool returns an oversized raw result; trimmed it contributes
        // MAX_TOOL_RESULT_CHARS chars (marker excluded here for a clean count).
        val perCall = MAX_TOOL_RESULT_CHARS
        var used = 0
        var flippedAt = -1
        // With default 40000 budget and 4000-char trimmed contributions, the 11th
        // call (used = 44000) is the first to exceed it; calls 1..10 stay within.
        for (call in 1..20) {
            val trimmed = trimToolResult("a".repeat(perCall * 2)).take(perCall)
            used += trimmed.length
            if (overTurnBudget(used) && flippedAt == -1) {
                flippedAt = call
            }
        }
        val expected = (MAX_TURN_TOOL_RESULT_CHARS / perCall) + 1 // = 11
        assertEquals("budget flips on the first call that pushes used past the cap", expected, flippedAt)
    }

    // -------------------------------------------------------------------------
    // trimResultPayload -- trim the INNER result VALUE, keeping the Gallery JSON
    // wrapper VALID. Regression: the native loop used to trim the whole
    // toResultJsonString string, which sliced big results mid-JSON → invalid →
    // parsed as "failed" (search_snapshots' ~23K-char result broke; roll_dice's
    // ~53 chars slipped under the cap and worked).
    // -------------------------------------------------------------------------

    @Test
    fun `big-result tool stays valid JSON with success preserved (search_snapshots regression)`() {
        val big = "x".repeat(23786) // ~ a real search_snapshots payload
        val rawJson = toResultJsonString(true, JsonPrimitive(big))

        // OLD BUG: trimming the WHOLE wrapper cuts it mid-JSON → unparseable → failed.
        val naive = trimToolResult(rawJson, MAX_TOOL_RESULT_CHARS)
        val (okBuggy, _) = parseResultJsonString(naive)
        assertFalse("wrapper-trim yields invalid JSON parsed as failure (the bug)", okBuggy)

        // FIX: parse → trim the inner payload → re-wrap → valid JSON, success kept.
        val (ok, rawPayload) = parseResultJsonString(rawJson)
        val payload = trimResultPayload(rawPayload, MAX_TOOL_RESULT_CHARS)
        val resultJson = toResultJsonString(ok, payload)
        val (ok2, payload2) = parseResultJsonString(resultJson)
        assertTrue("re-wrapped result is valid JSON parsed as SUCCESS", ok2)
        assertTrue("payload is a string", payload2 is JsonPrimitive)
        assertTrue(
            "payload bounded to maxChars + marker",
            (payload2 as JsonPrimitive).content.length <= MAX_TOOL_RESULT_CHARS + 64,
        )
    }

    @Test
    fun `trimResultPayload passes short and non-string payloads through unchanged`() {
        val short = JsonPrimitive("Rolled 1d6: [4]")
        assertEquals(short, trimResultPayload(short))
        val num = JsonPrimitive(42)
        assertEquals(num, trimResultPayload(num))
        assertEquals(null, trimResultPayload(null))
    }
}
