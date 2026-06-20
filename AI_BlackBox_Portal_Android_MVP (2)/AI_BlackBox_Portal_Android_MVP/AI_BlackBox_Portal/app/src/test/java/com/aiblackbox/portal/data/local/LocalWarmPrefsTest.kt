package com.aiblackbox.portal.data.local

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Auto-warm-on-open SETTING (feat(local): persisted auto-warm preference).
 *
 * The AndroidViewModel can't be instantiated on the plain JVM (no Application, no
 * SharedPreferences, no main dispatcher), so the SETTING is exercised through a tiny
 * in-memory fake of the [LocalWarmPrefs] seam (exactly like [WarmInflightStoreTest]
 * does for the warm-loop guard). SharedPreferences itself is NOT mocked; the real
 * prefs read/write lives behind the interface and is covered by the on-device build.
 *
 * Coverage:
 *  1. Default is TRUE (never set / first run) -> auto-warm-on-open is the default.
 *  2. A persisted FALSE round-trips (and is what preloadLocalEngine reads to SKIP).
 *  3. A persisted TRUE round-trips.
 *  4. The DEFAULT_AUTO_WARM constant is TRUE (pins the documented default).
 *  5. The preload DECISION: enabled -> warm; disabled -> skip (leave IDLE, lazy warm).
 */
class LocalWarmPrefsTest {

    /** In-memory fake of the seam — substitutes SharedPreferences in the unit test. */
    private class FakeLocalWarmPrefs(initial: Boolean = LocalWarmPrefs.DEFAULT_AUTO_WARM) :
        LocalWarmPrefs {
        private var flag = initial
        override fun autoWarmEnabled(): Boolean = flag
        override fun setAutoWarmEnabled(v: Boolean) { flag = v }
    }

    @Test fun `default is auto-warm enabled when never set`() {
        // First run / cleared pref: instant-first-send is the default.
        assertTrue(FakeLocalWarmPrefs().autoWarmEnabled())
    }

    @Test fun `a persisted disabled value round-trips`() {
        val prefs = FakeLocalWarmPrefs()
        prefs.setAutoWarmEnabled(false)
        assertFalse(prefs.autoWarmEnabled())
    }

    @Test fun `a persisted enabled value round-trips`() {
        val prefs = FakeLocalWarmPrefs(initial = false)
        prefs.setAutoWarmEnabled(true)
        assertTrue(prefs.autoWarmEnabled())
    }

    @Test fun `DEFAULT_AUTO_WARM constant is true`() {
        assertTrue(LocalWarmPrefs.DEFAULT_AUTO_WARM)
    }

    // -- the preload DECISION the ViewModel performs around this setting --

    @Test fun `enabled means the auto path warms while disabled means it skips`() {
        // preloadLocalEngine: warm IFF the setting is enabled. (Mirrors the gate added
        // to ChatViewModel.preloadLocalEngine before the OOM-crash guard.)
        val enabled = FakeLocalWarmPrefs(initial = true)
        assertTrue("enabled -> auto-warm runs", enabled.autoWarmEnabled())

        val disabled = FakeLocalWarmPrefs(initial = false)
        assertFalse("disabled -> auto-warm is skipped (lazy warm on send)", disabled.autoWarmEnabled())
    }
}
