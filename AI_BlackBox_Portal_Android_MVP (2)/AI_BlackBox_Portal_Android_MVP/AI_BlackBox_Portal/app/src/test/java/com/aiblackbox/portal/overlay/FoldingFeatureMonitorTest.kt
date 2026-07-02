package com.aiblackbox.portal.overlay

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (M5.5) Unit tests for the PURE parts of [FoldingFeatureMonitor]: the posture-change decision
 * ([postureInvalidates]), the monitor's state machine ([FoldingFeatureMonitor.update] /
 * [FoldingFeatureMonitor.currentPosture] / [FoldingFeatureMonitor.consumePostureChanged]) driven
 * with a FAKE posture (what a fake FoldingFeature feeds), and [DevicePosture] wire serialization.
 * The framework [FoldingFeatureMonitor.start] (WindowInfoTracker collection) is device-verified.
 */
class FoldingFeatureMonitorTest {

    private val wire = Json { encodeDefaults = true; explicitNulls = false }

    private val flatV = DevicePosture(PostureState.FLAT, HingeOrientation.VERTICAL)
    private val halfV = DevicePosture(PostureState.HALF_OPENED, HingeOrientation.VERTICAL)

    // ---- postureInvalidates: the change decision (pure) --------------------

    @Test fun `entering or leaving a foldable posture invalidates`() {
        assertTrue("null -> posture invalidates", postureInvalidates(null, flatV))
        assertTrue("posture -> null invalidates", postureInvalidates(flatV, null))
    }

    @Test fun `a state transition invalidates`() {
        assertTrue("FLAT -> HALF_OPENED invalidates", postureInvalidates(flatV, halfV))
    }

    @Test fun `an identical posture does not invalidate`() {
        assertFalse(postureInvalidates(flatV, DevicePosture(PostureState.FLAT, HingeOrientation.VERTICAL)))
        assertFalse("two nulls never invalidate", postureInvalidates(null, null))
    }

    // ---- monitor state machine: current posture + change flag --------------

    @Test fun `a fresh monitor has no posture and no pending change`() {
        val m = FoldingFeatureMonitor()
        assertNull(m.currentPosture())
        assertFalse(m.consumePostureChanged())
    }

    @Test fun `a posture change updates current and raises the flag once`() {
        val m = FoldingFeatureMonitor()
        m.update(halfV)
        assertEquals(halfV, m.currentPosture())
        // The flag is read-and-cleared: true once, then false until the next change.
        assertTrue("posture-change flag surfaced on first read", m.consumePostureChanged())
        assertFalse("flag cleared after being consumed", m.consumePostureChanged())
    }

    @Test fun `an identical update raises no change flag`() {
        val m = FoldingFeatureMonitor()
        m.update(halfV)
        assertTrue(m.consumePostureChanged())     // consume the first change
        m.update(DevicePosture(PostureState.HALF_OPENED, HingeOrientation.VERTICAL)) // same
        assertFalse("no spurious re-observe on an identical posture", m.consumePostureChanged())
        assertEquals(halfV, m.currentPosture())
    }

    @Test fun `a subsequent distinct posture re-raises the flag`() {
        val m = FoldingFeatureMonitor()
        m.update(flatV)
        m.consumePostureChanged()                  // clear
        m.update(halfV)                            // FLAT -> HALF_OPENED
        assertTrue("a real posture change re-arms the re-observe flag", m.consumePostureChanged())
        assertEquals(halfV, m.currentPosture())
    }

    // ---- (I1) collection survives an Activity-recreation scope cancel+restart ----

    @Test fun `posture collection re-arms after a scope cancel and restart (re-collectable)`() = runBlocking {
        val m = FoldingFeatureMonitor()

        // First collection (a live Activity's lifecycleScope) sees a posture, then completes.
        val scope1 = CoroutineScope(Dispatchers.Unconfined + Job())
        m.restartCollection(scope1, flowOf(halfV)).join()
        assertEquals(halfV, m.currentPosture())
        scope1.cancel() // Activity destroyed → its lifecycleScope (and collector) is cancelled.

        // A FRESH scope (Activity recreated after a Fold cover<->main display switch) STILL
        // re-collects — proving the removal of the old process-wide `started` latch, which would
        // otherwise have left posture tracking permanently dead after the first Activity died.
        val scope2 = CoroutineScope(Dispatchers.Unconfined + Job())
        m.restartCollection(scope2, flowOf(flatV)).join()
        assertEquals("posture tracking re-arms across Activity recreation", flatV, m.currentPosture())
        scope2.cancel()
    }

    @Test fun `restarting collection cancels the prior collector (no double-collection)`() = runBlocking {
        val m = FoldingFeatureMonitor()
        val scope = CoroutineScope(Dispatchers.Unconfined + Job())

        // A never-completing source (a hot StateFlow) keeps the first collector alive...
        val job1 = m.restartCollection(scope, MutableStateFlow<DevicePosture?>(null))
        assertTrue("first collector is live", job1.isActive)

        // ...restarting must cancel it and hand back a fresh, live collector — exactly one at a time.
        val job2 = m.restartCollection(scope, MutableStateFlow<DevicePosture?>(null))
        job1.join()
        assertTrue("prior collector cancelled on restart", job1.isCancelled)
        assertTrue("new collector is live", job2.isActive)
        scope.cancel()
    }

    // ---- DevicePosture serialization matches device_capability.json --------

    @Test fun `DevicePosture serializes to lowercase state and orientation`() {
        val s = wire.encodeToString(halfV)
        assertTrue(s, s.contains("\"state\":\"half_opened\""))
        assertTrue(s, s.contains("\"orientation\":\"vertical\""))
    }

    @Test fun `DevicePosture omits an unknown orientation`() {
        val s = wire.encodeToString(DevicePosture(PostureState.FLAT))
        assertTrue(s, s.contains("\"state\":\"flat\""))
        assertFalse("unknown orientation dropped from the wire", s.contains("orientation"))
    }
}
