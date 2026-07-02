package com.aiblackbox.portal.data.remote

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * (M8.3 / I1) Unit tests for the PERSISTENT [RemoteSessionTelemetry] store.
 *
 * The durable [SqliteTelemetryStore] layer needs a real SQLite engine (a device / Robolectric —
 * plain-JVM unit tests have none), so it is verified there. What IS JVM-unit-tested here is the
 * pure retention/query logic every store shares ([TelemetryRetention]): the 7-day prune cutoff,
 * the operator-scope filter, the summary aggregation, and the retention bound — plus the NO-SECRET
 * shape of a [RemoteSessionTelemetry.Step], and the write→reopen→read PERSISTENCE CONTRACT the DB
 * must satisfy (exercised with a fake persistent store whose rows outlive a store instance).
 *
 * The behavioral tests (record / stepsFor / summary / bounding) run against the in-memory fallback
 * store, which routes through the SAME pure logic the DB does.
 */
class RemoteSessionTelemetryTest {

    @Before fun reset() = RemoteSessionTelemetry.resetForTest()
    @After fun cleanup() = RemoteSessionTelemetry.resetForTest()

    private fun step(
        taskId: String = "t1", operator: String = "op", action: String = "tap",
        success: Boolean = true, latencyMs: Long = 1L, captureType: String = "tree_only",
        atMs: Long = 0L,
    ) = RemoteSessionTelemetry.Step(taskId, operator, action, success, latencyMs, captureType, atMs)

    // ======================= behavioral (in-memory fallback → shared pure logic) =======

    @Test fun `records per-step non-sensitive fields`() {
        RemoteSessionTelemetry.record("t1", "Brandon", "tap", true, 42L, "tree_only")
        val steps = RemoteSessionTelemetry.stepsFor("t1", "Brandon")
        assertEquals(1, steps.size)
        val s = steps[0]
        assertEquals("t1", s.taskId)
        assertEquals("Brandon", s.operator)
        assertEquals("tap", s.action)
        assertTrue(s.success)
        assertEquals(42L, s.latencyMs)
        assertEquals("tree_only", s.captureType)
    }

    @Test fun `stepsFor is operator-scoped — never leaks another operators steps`() {
        RemoteSessionTelemetry.record("t1", "Brandon", "tap", true, 10L, "tree_only")
        RemoteSessionTelemetry.record("t1", "Mallory", "type", true, 10L, "tree_only")
        // Same task id, different operator → Brandon sees only his step; Mallory can't read it.
        assertEquals(1, RemoteSessionTelemetry.stepsFor("t1", "Brandon").size)
        assertEquals("tap", RemoteSessionTelemetry.stepsFor("t1", "Brandon")[0].action)
        assertEquals(1, RemoteSessionTelemetry.stepsFor("t1", "Mallory").size)
        assertTrue(RemoteSessionTelemetry.stepsFor("t1", "Nobody").isEmpty())
    }

    @Test fun `blank task id is not recorded`() {
        RemoteSessionTelemetry.record("", "Brandon", "tap", true, 1L, "none")
        assertTrue(RemoteSessionTelemetry.stepsFor("", "Brandon").isEmpty())
        assertEquals(0, RemoteSessionTelemetry.summary("Brandon").stepCount)
    }

    @Test fun `summary aggregates avg latency and success rate per operator`() {
        RemoteSessionTelemetry.record("t1", "Brandon", "tap", true, 100L, "tree_only")
        RemoteSessionTelemetry.record("t1", "Brandon", "type", false, 200L, "screenshot")
        RemoteSessionTelemetry.record("t2", "Brandon", "open_app", true, 300L, "none")
        // A different operator's step must not pollute Brandon's aggregate.
        RemoteSessionTelemetry.record("t9", "Sarah", "tap", false, 9999L, "tree_only")

        val sum = RemoteSessionTelemetry.summary("Brandon")
        assertEquals("Brandon", sum.operator)
        assertEquals(3, sum.stepCount)
        assertEquals(2, sum.successCount)
        assertEquals(200L, sum.avgLatencyMs)                 // (100+200+300)/3
        assertEquals(2.0 / 3.0, sum.successRate, 1e-9)
    }

    @Test fun `empty operator summary is all-zero (no divide by zero)`() {
        val sum = RemoteSessionTelemetry.summary("Nobody")
        assertEquals(0, sum.stepCount)
        assertEquals(0, sum.successCount)
        assertEquals(0L, sum.avgLatencyMs)
        assertEquals(0.0, sum.successRate, 1e-9)
    }

    @Test fun `the store is retention-bounded (oldest evicts first)`() {
        val over = RemoteSessionTelemetry.MAX_STEPS + 50
        for (i in 0 until over) {
            RemoteSessionTelemetry.record("t$i", "op", "tap", true, 1L, "tree_only")
        }
        // Total retained is capped; the newest are kept, the oldest evicted.
        assertEquals(RemoteSessionTelemetry.MAX_STEPS, RemoteSessionTelemetry.summary("op").stepCount)
        assertTrue(RemoteSessionTelemetry.stepsFor("t${over - 1}", "op").isNotEmpty())   // newest kept
        assertTrue(RemoteSessionTelemetry.stepsFor("t0", "op").isEmpty())                // oldest evicted
    }

    @Test fun `no secret — the recorded shape can only carry non-sensitive fields`() {
        // Even if a caller passed a value that LOOKED sensitive as the action name, the store
        // holds ONLY {task_id, operator, action, success, latency, capture, at} — there is no
        // field for screen text, typed text, node content, coordinates, or arguments. Prove the
        // serialized record contains none of the sensitive markers a leak would produce.
        RemoteSessionTelemetry.record("t1", "Brandon", "type", true, 5L, "tree_only")
        val step = RemoteSessionTelemetry.stepsFor("t1", "Brandon")[0]
        val json = kotlinx.serialization.json.Json.encodeToString(
            RemoteSessionTelemetry.Step.serializer(), step)
        // The only text is the fixed action NAME + capture kind + operator — no content keys.
        // (captureType is a fixed enum-like token, never screen text, so it is safe.)
        assertFalse(json, json.contains("text"))
        assertFalse(json, json.contains("password"))
        assertFalse(json, json.contains("resource_id"))
        assertFalse(json, json.contains("\"x\""))
        assertFalse(json, json.contains("node"))
    }

    // ======================= pure retention/query logic (TelemetryRetention) ===========

    @Test fun `cutoffMs is now minus the 7-day retention window`() {
        val now = 5_000_000_000L
        assertEquals(now - RemoteSessionTelemetry.RETENTION_MS, TelemetryRetention.cutoffMs(now))
        // sanity: the window is exactly 7 days of millis.
        assertEquals(7L * 24 * 60 * 60 * 1000, RemoteSessionTelemetry.RETENTION_MS)
    }

    @Test fun `retain drops rows older than the 7-day window`() {
        val now = 1_000_000_000_000L
        val old = step(taskId = "old", atMs = now - RemoteSessionTelemetry.RETENTION_MS - 1)  // just outside
        val onEdge = step(taskId = "edge", atMs = now - RemoteSessionTelemetry.RETENTION_MS)   // exactly at cutoff — kept
        val fresh = step(taskId = "fresh", atMs = now - 1000L)                                 // inside
        val kept = TelemetryRetention.retain(listOf(old, onEdge, fresh), now)
        assertEquals(listOf("edge", "fresh"), kept.map { it.taskId })   // the >7-day row is pruned
    }

    @Test fun `retain caps to the newest MAX_STEPS (oldest first)`() {
        val now = 1_000_000_000_000L
        val n = RemoteSessionTelemetry.MAX_STEPS + 10
        // ascending timestamps (all within the window) so takeLast keeps the newest.
        val steps = (0 until n).map { step(taskId = "t$it", atMs = now - (n - it)) }
        val kept = TelemetryRetention.retain(steps, now)
        assertEquals(RemoteSessionTelemetry.MAX_STEPS, kept.size)
        assertEquals("t10", kept.first().taskId)                                  // oldest 10 evicted
        assertEquals("t${RemoteSessionTelemetry.MAX_STEPS + 9}", kept.last().taskId)   // newest kept
    }

    @Test fun `filterStepsFor is operator-scoped`() {
        val steps = listOf(step(taskId = "t1", operator = "A"), step(taskId = "t1", operator = "B"))
        assertEquals(1, TelemetryRetention.filterStepsFor(steps, "t1", "A").size)
        assertEquals("A", TelemetryRetention.filterStepsFor(steps, "t1", "A")[0].operator)
        assertTrue(TelemetryRetention.filterStepsFor(steps, "t1", "C").isEmpty())
    }

    @Test fun `summaryOf aggregates per operator and never divides by zero`() {
        val steps = listOf(
            step(operator = "A", success = true, latencyMs = 100L),
            step(operator = "A", success = false, latencyMs = 200L),
            step(operator = "B", success = true, latencyMs = 9999L),
        )
        val s = TelemetryRetention.summaryOf(steps, "A")
        assertEquals(2, s.stepCount)
        assertEquals(1, s.successCount)
        assertEquals(150L, s.avgLatencyMs)
        assertEquals(0.5, s.successRate, 1e-9)
        val z = TelemetryRetention.summaryOf(steps, "Z")   // no rows → all-zero
        assertEquals(0, z.stepCount)
        assertEquals(0L, z.avgLatencyMs)
        assertEquals(0.0, z.successRate, 1e-9)
    }

    // ======================= persistence contract (write → reopen → read) ==============

    /**
     * A FAKE persistent store backed by a shared list whose rows OUTLIVE a store instance —
     * modelling a durable DB. "Reopening" the store = a NEW instance over the same backing (a
     * process/FGS restart re-opening the same DB file). This proves the write→reopen→read contract
     * the real [SqliteTelemetryStore] must satisfy; the SQLite impl is device/Robolectric-verified.
     */
    private class FakePersistentStore(
        private val backing: MutableList<RemoteSessionTelemetry.Step>,
    ) : TelemetryStore {
        override fun record(step: RemoteSessionTelemetry.Step) {
            backing.add(step)
            val kept = TelemetryRetention.retain(backing.toList(), System.currentTimeMillis())
            backing.clear(); backing.addAll(kept)
        }
        override fun stepsFor(taskId: String, operator: String) =
            TelemetryRetention.filterStepsFor(backing, taskId, operator)
        override fun summary(operator: String) = TelemetryRetention.summaryOf(backing, operator)
    }

    @Test fun `records survive a store reopen and stay operator-scoped`() {
        val disk = mutableListOf<RemoteSessionTelemetry.Step>()     // the durable "DB file"
        RemoteSessionTelemetry.useStoreForTest(FakePersistentStore(disk))
        RemoteSessionTelemetry.record("t1", "Brandon", "tap", true, 42L, "tree_only")
        RemoteSessionTelemetry.record("t1", "Mallory", "type", true, 10L, "tree_only")

        // Simulate an FGS restart: a NEW store instance over the SAME backing (reopen the DB file).
        RemoteSessionTelemetry.useStoreForTest(FakePersistentStore(disk))

        val steps = RemoteSessionTelemetry.stepsFor("t1", "Brandon")
        assertEquals(1, steps.size)                       // Brandon's row survived the reopen
        assertEquals("tap", steps[0].action)
        assertEquals(42L, steps[0].latencyMs)
        // still operator-scoped after reopen — Brandon never sees Mallory's row.
        assertTrue(RemoteSessionTelemetry.stepsFor("t1", "Nobody").isEmpty())
        assertEquals(1, RemoteSessionTelemetry.summary("Brandon").stepCount)
        assertEquals(1, RemoteSessionTelemetry.summary("Mallory").stepCount)
    }
}
