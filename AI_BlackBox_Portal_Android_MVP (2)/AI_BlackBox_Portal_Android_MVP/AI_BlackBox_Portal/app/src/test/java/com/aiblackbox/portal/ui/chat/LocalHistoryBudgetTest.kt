package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.local.FcLoop
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * SL-2 — token-budgeted rolling history for the ON-DEVICE (`local`) provider.
 *
 * Exercises the PURE companion function [ChatViewModel.budgetHistory] directly
 * (no AndroidViewModel, no Robolectric — same strategy as
 * [ChatViewModelLocalRoutingTest] / [com.aiblackbox.portal.ChatViewModelSaveTest]).
 *
 * Contract under test:
 *  - keeps the NEWEST turns, drops the OLDEST first, until the SUM of turn text
 *    chars is <= maxChars
 *  - a turn is atomic: never split (whole include or whole exclude)
 *  - order preserved oldest->newest in the returned list
 *  - empty input -> empty
 *  - everything fits -> passthrough (unchanged)
 *
 * EDGE-CASE DECISION (single newest turn alone exceeds maxChars):
 *  We RETURN THE NEWEST TURN ALONE even when it exceeds maxChars, rather than
 *  returning empty. Rationale: the user's most recent context must never be
 *  silently dropped, and the per-turn [com.aiblackbox.portal.data.local.overTurnBudget]
 *  / [trimToolResult] soft-stop is the backstop for an over-budget prompt. So
 *  budgetHistory NEVER returns empty for a non-empty input.
 */
class LocalHistoryBudgetTest {

    private fun user(text: String) = FcLoop.Turn(FcLoop.Role.USER, text)
    private fun assistant(text: String) = FcLoop.Turn(FcLoop.Role.ASSISTANT, text)

    @Test
    fun `empty input returns empty`() {
        assertEquals(emptyList<FcLoop.Turn>(), ChatViewModel.budgetHistory(emptyList(), 8000))
    }

    @Test
    fun `within budget is passthrough preserving oldest to newest order`() {
        val turns = listOf(user("a"), assistant("b"), user("c"), assistant("d"))
        // total = 4 chars, well under budget -> unchanged, same order
        assertEquals(turns, ChatViewModel.budgetHistory(turns, 8000))
    }

    @Test
    fun `drops oldest first until under budget keeping newest`() {
        // 5 turns of 10 chars each = 50 total; budget 25 -> keep newest 2 (=20), drop oldest 3
        val turns = listOf(
            user("1".repeat(10)),
            assistant("2".repeat(10)),
            user("3".repeat(10)),
            assistant("4".repeat(10)),
            user("5".repeat(10)),
        )
        val kept = ChatViewModel.budgetHistory(turns, 25)
        assertEquals(listOf(turns[3], turns[4]), kept)
    }

    @Test
    fun `exact budget boundary keeps the turn that lands on the limit`() {
        // 3 turns of 10 each = 30; budget exactly 20 -> keep newest 2 (sum == 20, <= budget)
        val turns = listOf(
            user("1".repeat(10)),
            assistant("2".repeat(10)),
            user("3".repeat(10)),
        )
        val kept = ChatViewModel.budgetHistory(turns, 20)
        assertEquals(listOf(turns[1], turns[2]), kept)
    }

    @Test
    fun `turns are atomic and never split`() {
        // budget 15 cannot fit two 10-char turns (20) -> only the newest 10-char turn,
        // returned whole (length 10) -- never a 15-char slice.
        val turns = listOf(
            user("1".repeat(10)),
            assistant("2".repeat(10)),
        )
        val kept = ChatViewModel.budgetHistory(turns, 15)
        assertEquals(listOf(turns[1]), kept)
        assertEquals(10, kept.single().text.length)
    }

    @Test
    fun `single newest turn exceeding budget is still returned alone never empty`() {
        // The newest turn alone is over budget; we return it (don't drop the latest
        // context) rather than returning empty.
        val turns = listOf(
            user("old".repeat(5)),
            assistant("z".repeat(100)),
        )
        val kept = ChatViewModel.budgetHistory(turns, 10)
        assertEquals(listOf(turns[1]), kept)
    }

    @Test
    fun `multiple turns all over budget collapses to the single newest`() {
        val turns = listOf(
            user("a".repeat(50)),
            assistant("b".repeat(50)),
            user("c".repeat(50)),
        )
        val kept = ChatViewModel.budgetHistory(turns, 10)
        assertEquals(listOf(turns[2]), kept)
    }
}
