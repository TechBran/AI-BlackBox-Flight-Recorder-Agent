package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Collections
import java.util.concurrent.CountDownLatch
import kotlin.concurrent.thread

/**
 * `sync_state.json` is written by the listener threads, the view model and three workers,
 * through more than one store instance. No update may be lost and no writer may fail: the
 * lock is per file across the process, and every atomic write has its own temp file.
 */
class WellnessSyncStateStoreTest {

    private val files = WellnessFiles(WellnessFixtures.tempDir("state"))

    private fun hammer(threads: Int, perThread: Int, write: (Int) -> Unit): List<Throwable> {
        val errors = Collections.synchronizedList(mutableListOf<Throwable>())
        val start = CountDownLatch(1)
        val workers = (0 until threads).map { t ->
            thread {
                start.await()
                repeat(perThread) {
                    try {
                        write(t)
                    } catch (e: Throwable) {
                        errors += e
                    }
                }
            }
        }
        start.countDown()
        workers.forEach { it.join() }
        return errors
    }

    @Test
    fun `concurrent updates through separate store instances lose nothing and never throw`() {
        val stores = List(4) { WellnessSyncStateStore(files.syncState) }
        val errors = hammer(threads = 8, perThread = 150) { t ->
            stores[t % stores.size].update { it.copy(deletionsSeen = it.deletionsSeen + 1) }
        }
        assertEquals(emptyList<Throwable>(), errors)
        assertEquals(8 * 150, WellnessSyncStateStore(files.syncState).load().deletionsSeen)
    }

    @Test
    fun `unlocked atomic writes of one file never share a temp file and leave none behind`() {
        val file = files.statusCache
        val errors = hammer(threads = 8, perThread = 100) { t -> WellnessAtomic.writeText(file, "writer-$t".repeat(200)) }
        assertEquals(emptyList<Throwable>(), errors)
        val text = file.readText()
        assertTrue("the file is one whole write, never a torn mix", Regex("(writer-[0-7])\\1{199}").matches(text))
        assertEquals(listOf(file.name), file.parentFile!!.listFiles()!!.map { it.name }.filter { it.startsWith(file.name) })
    }
}
