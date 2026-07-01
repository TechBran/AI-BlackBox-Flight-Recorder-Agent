package com.aiblackbox.portal.data.remote

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * (M1.4) Unit tests for the pure [RemoteSessionBus] — the session start/stop signal the
 * action channel drives + the kill switch. The Android surfaces that react (overlay banner
 * / notification) are device-verified; the lifecycle + kill semantics are proven here.
 */
class RemoteSessionBusTest {

    @Before fun reset() = RemoteSessionBus.resetForTest()
    @After fun cleanup() = RemoteSessionBus.resetForTest()

    @Test fun `starts idle`() {
        assertFalse(RemoteSessionBus.isActive())
        assertNull(RemoteSessionBus.current())
    }

    @Test fun `start marks the session active and returns transition true`() {
        val started = RemoteSessionBus.start("t1", "Brandon", clock = { 42L })
        assertTrue(started)
        assertTrue(RemoteSessionBus.isActive())
        assertEquals("t1", RemoteSessionBus.current()?.taskId)
        assertEquals("Brandon", RemoteSessionBus.current()?.operator)
        assertEquals(42L, RemoteSessionBus.current()?.startedAtMs)
    }

    @Test fun `re-starting the same task is idempotent (no transition)`() {
        assertTrue(RemoteSessionBus.start("t1", "Brandon"))
        assertFalse(RemoteSessionBus.start("t1", "Brandon"))  // same task -> no new transition
        assertTrue(RemoteSessionBus.isActive())
    }

    @Test fun `stop aborts the session and reports the aborted one`() {
        RemoteSessionBus.start("t1", "Brandon")
        val aborted = RemoteSessionBus.stop()
        assertEquals("t1", aborted?.taskId)
        assertFalse(RemoteSessionBus.isActive())
        assertNull(RemoteSessionBus.current())
    }

    @Test fun `stop with no session returns null`() {
        assertNull(RemoteSessionBus.stop())
    }

    // ---- the kill switch has teeth ----

    @Test fun `killed task stays killed and is not resurrected by start`() {
        RemoteSessionBus.start("t1", "Brandon")
        RemoteSessionBus.stop()
        assertTrue(RemoteSessionBus.isKilled("t1"))
        // A stale re-start of the SAME task does not bring it back.
        assertFalse(RemoteSessionBus.start("t1", "Brandon"))
        assertFalse(RemoteSessionBus.isActive())
    }

    @Test fun `a new task after a kill starts normally`() {
        RemoteSessionBus.start("t1", "Brandon")
        RemoteSessionBus.stop()
        assertTrue(RemoteSessionBus.start("t2", "Brandon"))  // different task -> fresh session
        assertTrue(RemoteSessionBus.isActive())
        assertFalse(RemoteSessionBus.isKilled("t2"))
        assertTrue(RemoteSessionBus.isKilled("t1"))  // old one still blocked
    }

    @Test fun `blank task id is never killed`() {
        assertFalse(RemoteSessionBus.isKilled(""))
    }

    // ---- I3: the killed set remembers MULTIPLE kills ----

    @Test fun `killing task B does not forget task A (I3)`() {
        RemoteSessionBus.start("A", "Brandon"); RemoteSessionBus.stop()
        RemoteSessionBus.start("B", "Brandon"); RemoteSessionBus.stop()
        // With a single-slot killedTaskId, killing B would forget A and a stale A frame could
        // resurrect it. The bounded SET keeps BOTH refused.
        assertTrue(RemoteSessionBus.isKilled("A"))
        assertTrue(RemoteSessionBus.isKilled("B"))
        assertFalse(RemoteSessionBus.isKilled("C"))
        // And neither stale task can re-start.
        assertFalse(RemoteSessionBus.start("A", "Brandon"))
        assertFalse(RemoteSessionBus.start("B", "Brandon"))
    }

    @Test fun `the killed set is bounded and evicts oldest-first (I3)`() {
        // Kill more tasks than the 64-cap; the recent ones stay refused, the oldest evict, and
        // the set never grows without bound.
        for (i in 0 until 100) { RemoteSessionBus.start("t$i", "op"); RemoteSessionBus.stop() }
        assertTrue(RemoteSessionBus.isKilled("t99"))   // newest kill still refused
        assertTrue(RemoteSessionBus.isKilled("t40"))   // within the last 64
        assertFalse(RemoteSessionBus.isKilled("t0"))   // oldest (past the cap) evicted
    }

    // ---- I4: atomic session state under concurrency ----

    @Test fun `concurrent start-stop never leaves the banner visible (I4)`() {
        val lastSeen = java.util.concurrent.atomic.AtomicReference<RemoteSessionBus.Session?>(null)
        RemoteSessionBus.addListener { lastSeen.set(it) }
        val pool = java.util.concurrent.Executors.newFixedThreadPool(8)
        val n = 500
        val latch = java.util.concurrent.CountDownLatch(n)
        repeat(n) { i ->
            pool.submit {
                try {
                    RemoteSessionBus.start("t$i", "op")
                    RemoteSessionBus.stop()
                } finally { latch.countDown() }
            }
        }
        latch.await(10, java.util.concurrent.TimeUnit.SECONDS)
        pool.shutdown()
        // Every start was paired with a stop; notifications are serialized under the monitor.
        // A final deterministic stop settles the state — the listener's LAST-seen must equal
        // current() (both null): the consent banner is never left up after a stop.
        RemoteSessionBus.stop()
        assertFalse(RemoteSessionBus.isActive())
        assertNull(RemoteSessionBus.current())
        assertNull(lastSeen.get())
    }

    // ---- listeners ----

    @Test fun `listener gets current state on register then transitions`() {
        RemoteSessionBus.start("t1", "Brandon")
        val seen = mutableListOf<String?>()
        val listener = RemoteSessionBus.Listener { seen.add(it?.taskId) }
        RemoteSessionBus.addListener(listener)   // immediate delivery of current state
        RemoteSessionBus.stop()                  // -> null
        RemoteSessionBus.start("t2", "Sarah")    // -> t2
        assertEquals(listOf("t1", null, "t2"), seen)
    }

    @Test fun `removed listener stops receiving`() {
        val seen = mutableListOf<String?>()
        val listener = RemoteSessionBus.Listener { seen.add(it?.taskId) }
        RemoteSessionBus.addListener(listener)   // delivers null (idle)
        RemoteSessionBus.removeListener(listener)
        RemoteSessionBus.start("t1", "Brandon")  // not seen
        assertEquals(listOf<String?>(null), seen)
    }

    @Test fun `a throwing listener never breaks the signal to others`() {
        val seen = mutableListOf<String?>()
        RemoteSessionBus.addListener { throw RuntimeException("boom") }
        RemoteSessionBus.addListener { seen.add(it?.taskId) }
        RemoteSessionBus.start("t1", "Brandon")
        assertTrue(seen.contains("t1"))
    }
}
