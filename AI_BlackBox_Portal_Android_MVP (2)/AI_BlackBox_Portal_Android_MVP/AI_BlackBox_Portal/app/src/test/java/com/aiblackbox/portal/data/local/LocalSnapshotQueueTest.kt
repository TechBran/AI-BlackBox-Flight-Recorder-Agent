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

    @Test
    fun `save is atomic round-trip (no tmp sibling left behind)`() = runTest {
        // An offline enqueue persists through the real atomic save path (temp +
        // renameTo). We can't deterministically simulate a crash mid-write, so this
        // asserts the happy path: the queue file holds the items AND the dir is left
        // clean — no leftover ".tmp" sibling from the atomic write.
        val sender = FakeSender(failIf = { true }) // stay offline so the item persists
        val file = File(tmp.root, "local_snapshot_queue.json")
        val queue = LocalSnapshotQueue(sender, JsonFileQueueStore(file))

        val r1 = req("alpha")
        val r2 = req("beta")
        queue.enqueue(r1)
        queue.enqueue(r2)

        // Correct content via a fresh store over the same file (round-trips disk).
        assertEquals("queue file holds both items in order", listOf(r1, r2), JsonFileQueueStore(file).load())

        // Clean dir: the queue file exists, and NO ".tmp" sibling remains.
        val leftovers = tmp.root.listFiles()?.map { it.name }?.toSet() ?: emptySet()
        assertTrue("queue file present", leftovers.contains(file.name))
        assertFalse("no leftover .tmp from the atomic write", leftovers.contains(file.name + ".tmp"))
    }

    // ── A09-data-local-model-04: a permanent rejection must not wedge the ledger ──

    /** A [SnapshotSender] that fails each request with the exception [failWith] picks. */
    private class ScriptedSender(var failWith: (SaveRequest) -> IOException? = { null }) : SnapshotSender {
        val sent = mutableListOf<SaveRequest>()
        val attempted = mutableListOf<SaveRequest>()
        override suspend fun send(request: SaveRequest) {
            attempted += request
            failWith(request)?.let { throw it }
            sent += request
        }
    }

    private fun http(code: Int) = com.aiblackbox.portal.data.api.ApiHttpException("rejected", code)

    @Test
    fun `a permanent 4xx is dead-lettered and the turns behind it still reach the ledger`() = runTest {
        val file = File(tmp.root, "local_snapshot_queue.json")
        val store = JsonFileQueueStore(file)
        val poison = req("too big")
        val next = req("ordinary turn")
        store.save(listOf(poison, next))
        val sender = ScriptedSender(failWith = { if (it == poison) http(422) else null })

        LocalSnapshotQueue(sender, store).flush()

        assertEquals("the turn behind the poison pill was sent", listOf(next), sender.sent)
        assertTrue("the live queue drained", store.load().isEmpty())
        val dead = store.deadLetters()
        assertEquals("the rejected turn is parked, not dropped", listOf(poison), dead.map { it.request })
        assertTrue("the reason names the status", dead.single().reason.contains("422"))
    }

    @Test
    fun `408 and 429 are retryable and keep the head in place`() = runTest {
        for (code in listOf(408, 429)) {
            val store = JsonFileQueueStore(File(tmp.root, "q$code.json"))
            val head = req("retry-$code")
            store.save(listOf(head, req("behind-$code")))
            val sender = ScriptedSender(failWith = { http(code) })

            LocalSnapshotQueue(sender, store).flush()

            assertEquals("$code stops the drain at the head", 1, sender.attempted.size)
            assertEquals("$code keeps both turns queued", 2, store.load().size)
            assertTrue("$code is never dead-lettered on the first try", store.deadLetters().isEmpty())
        }
    }

    @Test
    fun `a head the box keeps refusing is dead-lettered after the attempt cap`() = runTest {
        val store = JsonFileQueueStore(File(tmp.root, "q.json"))
        val head = req("server keeps failing")
        val next = req("fine")
        store.save(listOf(head, next))
        val sender = ScriptedSender(failWith = { if (it == head) http(500) else null })
        val queue = LocalSnapshotQueue(sender, store)

        repeat(LocalSnapshotQueue.MAX_REFUSED_ATTEMPTS - 1) { queue.flush() }
        assertEquals("below the cap the head stays queued", listOf(head, next), store.load())
        assertTrue(store.deadLetters().isEmpty())

        queue.flush()
        assertEquals("at the cap the head is parked and the drain continues", listOf(next), sender.sent)
        assertTrue(store.load().isEmpty())
        assertEquals(listOf(head), store.deadLetters().map { it.request })
    }

    @Test
    fun `an unreachable box never counts toward the cap`() = runTest {
        val store = JsonFileQueueStore(File(tmp.root, "q.json"))
        val head = req("offline for a week")
        store.save(listOf(head))
        val sender = ScriptedSender(failWith = { IOException("unreachable") })
        val queue = LocalSnapshotQueue(sender, store)

        repeat(LocalSnapshotQueue.MAX_REFUSED_ATTEMPTS * 3) { queue.flush() }

        assertEquals("an offline turn is kept however long the outage", listOf(head), store.load())
        assertTrue(store.deadLetters().isEmpty())
    }

    @Test
    fun `the dead-letter file survives a restart and stays bounded`() = runTest {
        val file = File(tmp.root, "local_snapshot_queue.json")
        repeat(JsonFileQueueStore.MAX_DEAD_LETTERS + 5) { i ->
            JsonFileQueueStore(file).deadLetter(req("dead-$i"), "HTTP 422")
        }
        val reread = JsonFileQueueStore(file).deadLetters()
        assertEquals(JsonFileQueueStore.MAX_DEAD_LETTERS, reread.size)
        assertEquals("the newest are kept", "dead-${JsonFileQueueStore.MAX_DEAD_LETTERS + 4}", reread.last().request.userMessage)
    }
}
