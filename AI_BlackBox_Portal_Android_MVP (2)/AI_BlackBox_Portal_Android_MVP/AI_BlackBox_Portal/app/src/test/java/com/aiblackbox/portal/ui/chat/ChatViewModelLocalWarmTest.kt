package com.aiblackbox.portal.ui.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task W1 - warm-while-app-open preload + readiness state.
 *
 * Same strategy as [ChatViewModelLocalRoutingTest] / [ChatViewModelLocalEngineTest]:
 * the AndroidViewModel can't be instantiated on the plain JVM (no Robolectric, no
 * Application, no main dispatcher), so [ChatViewModel.preloadLocalEngine] is a thin
 * wiring shim over PURE, testable cores - the readiness state machine
 * ([ChatViewModel.localEngineStateAfter]), the no-double-warm guard
 * ([ChatViewModel.shouldStartWarm]), and the pill-label mapping
 * ([providerPillLabel]) - which this exercises directly. The instance method
 * applies exactly these functions (WARM_STARTED on launch, WARM_SUCCEEDED on a
 * clean load, WARM_FAILED on a throw; shouldStartWarm before launching), so proving
 * the cores proves the readiness path. The lazy fallback in
 * [ChatViewModel.runLocalEngineTurn] is unchanged and covered by
 * [ChatViewModelLocalEngineTest].
 *
 * Coverage:
 *  1. happy path IDLE -> WARMING -> READY.
 *  2. failure path WARMING -> ERROR.
 *  3. no double-warm: shouldStartWarm is false only while WARMING.
 *  4. a late SUCCEEDED/FAILED that arrives after the warm was superseded does NOT
 *     clobber the newer state.
 *  5. STARTED always wins (a new warm overrides any prior terminal state).
 *  6. the provider pill shows the readiness suffix ONLY for the local provider.
 */
class ChatViewModelLocalWarmTest {

    // -- 1. Happy path: IDLE -> WARMING -> READY --

    @Test fun `warm happy path transitions IDLE to WARMING to READY`() {
        val warming = ChatViewModel.localEngineStateAfter(
            LocalEngineState.IDLE, LocalEngineEvent.WARM_STARTED,
        )
        assertEquals(LocalEngineState.WARMING, warming)

        val ready = ChatViewModel.localEngineStateAfter(
            warming, LocalEngineEvent.WARM_SUCCEEDED,
        )
        assertEquals(LocalEngineState.READY, ready)
    }

    // -- 2. Failure path: WARMING -> ERROR --

    @Test fun `warm failure transitions WARMING to ERROR`() {
        val warming = ChatViewModel.localEngineStateAfter(
            LocalEngineState.IDLE, LocalEngineEvent.WARM_STARTED,
        )
        val error = ChatViewModel.localEngineStateAfter(
            warming, LocalEngineEvent.WARM_FAILED,
        )
        assertEquals(LocalEngineState.ERROR, error)
    }

    // -- 3. No double-warm guard --

    @Test fun `shouldStartWarm blocks a second warm only while WARMING`() {
        assertTrue("IDLE permits a warm", ChatViewModel.shouldStartWarm(LocalEngineState.IDLE))
        assertFalse("WARMING blocks a second warm", ChatViewModel.shouldStartWarm(LocalEngineState.WARMING))
        // READY/ERROR permit a re-warm; preloadLocalEngine re-checks the concrete
        // model path and no-ops when already loaded for that exact model.
        assertTrue("READY permits a re-warm", ChatViewModel.shouldStartWarm(LocalEngineState.READY))
        assertTrue("ERROR permits a retry", ChatViewModel.shouldStartWarm(LocalEngineState.ERROR))
    }

    @Test fun `a second WARM_STARTED while WARMING stays WARMING (no churn)`() {
        // localEngineStateAfter(WARMING, WARM_STARTED) is still WARMING; combined with
        // shouldStartWarm gating the launch, a duplicate trigger is a no-op.
        assertEquals(
            LocalEngineState.WARMING,
            ChatViewModel.localEngineStateAfter(LocalEngineState.WARMING, LocalEngineEvent.WARM_STARTED),
        )
    }

    // -- 4. A superseded outcome does not clobber the newer state --

    @Test fun `a late WARM_SUCCEEDED after the warm was superseded is ignored`() {
        // The warm was replaced (state moved off WARMING by a NEWER trigger, or to
        // ERROR/IDLE): a stale SUCCEEDED from the old in-flight load must not flip
        // the newer state to READY.
        assertEquals(
            "a SUCCEEDED while not WARMING is ignored (IDLE stays IDLE)",
            LocalEngineState.IDLE,
            ChatViewModel.localEngineStateAfter(LocalEngineState.IDLE, LocalEngineEvent.WARM_SUCCEEDED),
        )
        assertEquals(
            "a SUCCEEDED while ERROR is ignored (ERROR stays ERROR)",
            LocalEngineState.ERROR,
            ChatViewModel.localEngineStateAfter(LocalEngineState.ERROR, LocalEngineEvent.WARM_SUCCEEDED),
        )
    }

    @Test fun `a late WARM_FAILED after the warm was superseded is ignored`() {
        assertEquals(
            "a FAILED while READY is ignored (READY stays READY)",
            LocalEngineState.READY,
            ChatViewModel.localEngineStateAfter(LocalEngineState.READY, LocalEngineEvent.WARM_FAILED),
        )
        assertEquals(
            "a FAILED while IDLE is ignored (IDLE stays IDLE)",
            LocalEngineState.IDLE,
            ChatViewModel.localEngineStateAfter(LocalEngineState.IDLE, LocalEngineEvent.WARM_FAILED),
        )
    }

    // -- 5. STARTED always wins --

    @Test fun `WARM_STARTED always wins, overriding any prior terminal state`() {
        for (prior in LocalEngineState.entries) {
            assertEquals(
                "WARM_STARTED from $prior must be WARMING",
                LocalEngineState.WARMING,
                ChatViewModel.localEngineStateAfter(prior, LocalEngineEvent.WARM_STARTED),
            )
        }
    }

    // -- 6. Provider pill readiness label --

    @Test fun `pill label shows readiness suffix only for the local provider`() {
        // Local provider: each readiness state maps to a distinct suffix.
        val warming = providerPillLabel("local", LocalEngineState.WARMING)
        assertTrue("WARMING shows a loading affordance", warming.contains("loading", ignoreCase = true))
        assertTrue("base label retained", warming.contains("On-Device"))

        val ready = providerPillLabel("local", LocalEngineState.READY)
        assertTrue("READY shows ready", ready.contains("ready", ignoreCase = true))

        val error = providerPillLabel("local", LocalEngineState.ERROR)
        assertTrue("ERROR shows the warning glyph", error.contains("⚠"))

        val idle = providerPillLabel("local", LocalEngineState.IDLE)
        assertEquals("IDLE shows no suffix (plain display name)", "On-Device (Gemma)", idle)
    }

    @Test fun `pill label ignores engine state for non-local providers`() {
        // Even a WARMING/READY engine state must not bleed onto a cloud pill.
        assertEquals("Gemini", providerPillLabel("gemini", LocalEngineState.READY))
        assertEquals("Anthropic", providerPillLabel("anthropic", LocalEngineState.WARMING))
        assertEquals("OpenAI", providerPillLabel("openai", LocalEngineState.ERROR))
    }
}
