package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.SaveRequest
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.IOException

/**
 * Unit tests for [LocalSnapshotQueue] — the offline-resilient, ordered memory
 * write-back for on-device (provider=local) chat turns.
 *
 * Pure JVM, no Android: the network side is faked behind the [SnapshotSender]
 * seam and persistence is driven through a real temp [File] dir (the production
 * [QueueStore] is just JSON-on-disk, so a real tmp file is the honest fake — it
 * also lets the RESTART test construct a FRESH queue over the SAME file).
 *
 * Coverage mirrors the task's 6 cases:
 *   1. ONLINE      → enqueue sends once, queue ends empty (file empty/absent).
 *   2. OFFLINE     → IOException leaves the item persisted; send was attempted.
 *   3. RECONNECT   → after an offline enqueue, a succeeding flush() drains it.
 *   4. ORDER       → 3 enqueued offline drain in FIFO order on flush.
 *   5. PARTIAL     → succeed #1, throw on #2 → #1 removed, #2+#3 remain in order;
 *                    a later flush sends #2 then #3.
 *   6. RESTART     → a fresh queue over the same file sees + flushes the item.
 */
class LocalSnapshotQueueTest {

    @get:Rule
    val tmp = TemporaryFolder()

    /** A scriptable [SnapshotSender]: records every request, fails per a predicate. */
    private class FakeSender(
        /** Returns true to make send() throw IOException for that request. */
        var failIf: (SaveRequest) -> Boolean = { false },
    ) : SnapshotSender {
        val sent = mutableListOf<SaveRequest>()
        val attempted = mutableListOf<SaveRequest>()
        override suspend fun send(request: SaveRequest) {
            attempted += request
            if (failIf(request)) throw IOException("offline")
            sent += request
        }
    }

    private fun req(user: String, assistant: String = "a"): SaveRequest =
        SaveRequest(operator = "Brandon", userMessage = user, assistantResponse = assistant)

    private fun newQueue(sender: SnapshotSender, dir: File): LocalSnapshotQueue =
        LocalSnapshotQueue(sender, JsonFileQueueStore(File(dir, "local_snapshot_queue.json")))

    @Test
    fun `online enqueue sends once and leaves the queue empty`() = runTest {
        val sender = FakeSender() // succeeds
        val store = JsonFileQueueStore(File(tmp.root, "q.json"))
        val queue = LocalSnapshotQueue(sender, store)

        val r = req("hi")
        queue.enqueue(r)

        assertEquals("sent exactly once", 1, sender.sent.size)
        assertEquals("sent the request", r, sender.sent.first())
        assertTrue("queue drained empty", store.load().isEmpty())
    }

    @Test
    fun `offline enqueue keeps the item persisted and attempts the send`() = runTest {
        val sender = FakeSender(failIf = { true }) // always offline
        val store = JsonFileQueueStore(File(tmp.root, "q.json"))
        val queue = LocalSnapshotQueue(sender, store)

        val r = req("offline-turn")
        queue.enqueue(r)

        assertEquals("send was attempted", 1, sender.attempted.size)
        assertTrue("nothing actually sent", sender.sent.isEmpty())
        val persisted = store.load()
        assertEquals("item still queued", 1, persisted.size)
        assertEquals("the same item, undamaged", r, persisted.first())
    }

    @Test
    fun `reconnect flush drains a previously offline item`() = runTest {
        val sender = FakeSender(failIf = { true })
        val store = JsonFileQueueStore(File(tmp.root, "q.json"))
        val queue = LocalSnapshotQueue(sender, store)

        val r = req("queued-while-offline")
        queue.enqueue(r) // stays queued
        assertEquals(1, store.load().size)

        // Back online.
        sender.failIf = { false }
        queue.flush()

        assertEquals("now sent", listOf(r), sender.sent)
        assertTrue("queue drained", store.load().isEmpty())
    }

    @Test
    fun `flush sends queued items in FIFO order`() = runTest {
        val sender = FakeSender(failIf = { true })
        val store = JsonFileQueueStore(File(tmp.root, "q.json"))
        val queue = LocalSnapshotQueue(sender, store)

        val r1 = req("first")
        val r2 = req("second")
        val r3 = req("third")
        queue.enqueue(r1)
        queue.enqueue(r2)
        queue.enqueue(r3)
        assertEquals("all three queued", 3, store.load().size)

        sender.failIf = { false }
        queue.flush()

        assertEquals("drained in FIFO order", listOf(r1, r2, r3), sender.sent)
        assertTrue("queue empty", store.load().isEmpty())
    }

    @Test
    fun `partial failure removes the sent prefix and keeps the rest in order`() = runTest {
        // Queue 3 while offline.
        val sender = FakeSender(failIf = { true })
        val store = JsonFileQueueStore(File(tmp.root, "q.json"))
        val queue = LocalSnapshotQueue(sender, store)
        val r1 = req("one")
        val r2 = req("two")
        val r3 = req("three")
        queue.enqueue(r1)
        queue.enqueue(r2)
        queue.enqueue(r3)

        // Now succeed on #1, throw on #2 (and would on #3): flush stops at #2.
        sender.failIf = { it == r2 || it == r3 }
        queue.flush()

        assertEquals("only #1 made it out", listOf(r1), sender.sent)
        val remaining = store.load()
        assertEquals("#2 and #3 remain", listOf(r2, r3), remaining)

        // Later, fully online: #2 then #3 drain, still in order.
        sender.failIf = { false }
        queue.flush()
        assertEquals("eventually all three, in order", listOf(r1, r2, r3), sender.sent)
        assertTrue("queue empty", store.load().isEmpty())
    }

    @Test
    fun `a fresh queue over the same store sees and flushes the persisted item`() = runTest {
        val file = File(tmp.root, "shared_queue.json")

        // First queue: enqueue offline, then die.
        val offlineSender = FakeSender(failIf = { true })
        val first = LocalSnapshotQueue(offlineSender, JsonFileQueueStore(file))
        val r = req("survives-restart")
        first.enqueue(r)
        assertFalse("file persisted", JsonFileQueueStore(file).load().isEmpty())

        // Simulate process death + restart: a brand-new queue over the SAME file.
        val onlineSender = FakeSender(failIf = { false })
        val second = LocalSnapshotQueue(onlineSender, JsonFileQueueStore(file))
        second.flush()

        assertEquals("restarted queue flushed the survivor", listOf(r), onlineSender.sent)
        assertTrue("queue drained after restart", JsonFileQueueStore(file).load().isEmpty())
    }
}
