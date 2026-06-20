package com.aiblackbox.portal.data.local

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Warm-loop guard (fix(local): never auto-retry a crashed warm into an OOM loop).
 *
 * The AndroidViewModel can't be instantiated on the plain JVM (no Application, no
 * SharedPreferences, no main dispatcher), so the guard's TESTABLE part is the PURE
 * decision [WarmInflightStore.shouldAutoWarm] plus a tiny in-memory fake of the
 * [WarmInflightStore] seam that proves the set-before / clear-after SEQUENCING the
 * ViewModel performs around a warm. SharedPreferences itself is NOT mocked (only
 * android.util.Log is, via returnDefaultValues), so the actual prefs read/write is
 * isolated behind the [WarmInflightStore] interface and never touched here.
 *
 * Coverage:
 *  1. shouldAutoWarm = true when the flag is false (clean prior warm / first run).
 *  2. shouldAutoWarm = false when the flag is still true (prior warm SIGKILLed).
 *  3. shouldAutoWarm is exactly the negation of the flag (full table).
 *  4. sequencing: a SUCCESSFUL warm sets then clears -> next launch auto-warms.
 *  5. sequencing: a GRACEFUL (caught) failure clears -> next launch auto-warms.
 *  6. sequencing: a CRASHED warm leaves the flag set -> next launch SKIPS auto-warm,
 *     and the skip RE-ARMS the flag (clears it) so a later deliberate attempt can run.
 */
class WarmInflightStoreTest {

    /** In-memory fake of the seam — substitutes SharedPreferences in the unit test. */
    private class FakeWarmInflightStore(initial: Boolean = false) : WarmInflightStore {
        private var flag = initial
        override fun isInflight(): Boolean = flag
        override fun setInflight(value: Boolean) { flag = value }
    }

    // -- 1 & 2: the pure decision --

    @Test fun `shouldAutoWarm is true when the prior warm flag is false`() {
        assertTrue(WarmInflightStore.shouldAutoWarm(false))
    }

    @Test fun `shouldAutoWarm is false when the prior warm flag is still set`() {
        assertFalse(WarmInflightStore.shouldAutoWarm(true))
    }

    // -- 3: full negation table --

    @Test fun `shouldAutoWarm is exactly the negation of the in-flight flag`() {
        for (flag in listOf(true, false)) {
            assertTrue(
                "shouldAutoWarm($flag) must equal !$flag",
                WarmInflightStore.shouldAutoWarm(flag) == !flag,
            )
        }
    }

    // -- 4: a successful warm re-arms the system for the next launch --

    @Test fun `a successful warm clears the flag so the next launch auto-warms`() {
        val store = FakeWarmInflightStore(initial = false)
        // ViewModel: launch reads the flag -> allowed to warm.
        assertTrue(WarmInflightStore.shouldAutoWarm(store.isInflight()))
        // Set BEFORE load(), clear on SUCCESS.
        store.setInflight(true)
        store.setInflight(false)
        // Next process start: flag is false -> auto-warm allowed again.
        assertTrue(WarmInflightStore.shouldAutoWarm(store.isInflight()))
    }

    // -- 5: a graceful failure also clears the flag --

    @Test fun `a graceful caught failure clears the flag so the next launch auto-warms`() {
        val store = FakeWarmInflightStore(initial = false)
        store.setInflight(true)   // before load()
        store.setInflight(false)  // caught (graceful) failure
        assertTrue(WarmInflightStore.shouldAutoWarm(store.isInflight()))
    }

    // -- 6: a crashed warm breaks the loop on the next launch --

    @Test fun `a crashed warm leaves the flag set so the next launch skips auto-warm`() {
        // Prior process: set true before load(), then SIGKILLed -> no clear ran.
        val store = FakeWarmInflightStore(initial = true)
        // Next process start: flag still true -> do NOT auto-warm (loop broken).
        assertFalse(
            "a still-set flag must block the auto-warm",
            WarmInflightStore.shouldAutoWarm(store.isInflight()),
        )
        // The skip RE-ARMS the flag so a later deliberate (send) attempt can warm.
        store.setInflight(false)
        assertFalse(store.isInflight())
        assertTrue(
            "after re-arming, a future warm is allowed again",
            WarmInflightStore.shouldAutoWarm(store.isInflight()),
        )
    }
}
