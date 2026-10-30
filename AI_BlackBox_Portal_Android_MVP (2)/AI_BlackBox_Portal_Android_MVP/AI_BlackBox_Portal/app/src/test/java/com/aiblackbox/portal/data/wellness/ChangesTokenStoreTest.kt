package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

class ChangesTokenStoreTest {

    private var now = 1_000_000_000L
    private val dir = WellnessFixtures.tempDir("tokens")
    private val store = ChangesTokenStore(File(dir, "tokens.json")) { now }
    private val outbox = WellnessOutbox(File(dir, "outbox"))

    @Test
    fun `one token per type, persisted`() {
        store.seed(HcType.SLEEP, "s1")
        store.seed(HcType.STEPS, "t1")
        val reopened = ChangesTokenStore(File(dir, "tokens.json")) { now }
        assertEquals("s1", reopened.get(HcType.SLEEP)!!.token)
        assertEquals("t1", reopened.get(HcType.STEPS)!!.token)
        assertNull(reopened.get(HcType.HEART_RATE))
        assertEquals(setOf(HcType.SLEEP, HcType.STEPS), reopened.all().keys)
    }

    @Test
    fun `a token advances only with an outbox receipt that is on disk`() {
        store.seed(HcType.SLEEP, "s1", syncedUntilMs = 10)
        val receipt = outbox.enqueue("""{"schema":"bbx.wellness.ingest/1"}""")
        store.advance(HcType.SLEEP, "s2", 20, CommitProof.Committed(receipt))
        assertEquals("s2", store.get(HcType.SLEEP)!!.token)
        assertEquals(20, store.get(HcType.SLEEP)!!.syncedUntilMs)
    }

    @Test
    fun `a receipt whose batch vanished undelivered refuses the advance`() {
        store.seed(HcType.SLEEP, "s1")
        val receipt = outbox.enqueue("{}")
        File(File(dir, "outbox"), receipt.name).delete()
        try {
            store.advance(HcType.SLEEP, "s2", 20, CommitProof.Committed(receipt))
            fail("advance must refuse")
        } catch (e: IllegalStateException) {
            // expected
        }
        assertEquals("s1", store.get(HcType.SLEEP)!!.token)
    }

    @Test
    fun `a delivered batch still counts as committed`() = kotlinx.coroutines.runBlocking {
        store.seed(HcType.SLEEP, "s1")
        val receipt = outbox.enqueue("{}")
        outbox.drain(send = { WellnessOutbox.SendResult(200, "{}") }, newBatchId = { "x" })
        store.advance(HcType.SLEEP, "s2", 20, CommitProof.Committed(receipt))
        assertEquals("s2", store.get(HcType.SLEEP)!!.token)
    }

    @Test
    fun `nothing to commit may advance, and synced-until never goes backwards`() {
        store.seed(HcType.HRV, "h1", syncedUntilMs = 500)
        store.advance(HcType.HRV, "h2", 100, CommitProof.NothingToCommit)
        assertEquals("h2", store.get(HcType.HRV)!!.token)
        assertEquals(500, store.get(HcType.HRV)!!.syncedUntilMs)
    }

    @Test
    fun `tokens expire after 30 days unused and re-read from synced-until minus 48 hours`() {
        store.seed(HcType.SLEEP, "s1", syncedUntilMs = now)
        val entry = store.get(HcType.SLEEP)!!
        assertFalse(store.isExpired(entry, now + ChangesTokenStore.TOKEN_LIFE_MS - 1))
        assertTrue(store.isExpired(entry, now + ChangesTokenStore.TOKEN_LIFE_MS))
        assertEquals(now - 48 * 60 * 60_000L, store.reReadFromMs(entry))
    }

    @Test
    fun `a corrupt file reads as empty and drop or clear removes tokens`() {
        File(dir, "tokens.json").writeText("{not json")
        assertNull(store.get(HcType.SLEEP))
        store.seed(HcType.SLEEP, "s1")
        store.seed(HcType.STEPS, "t1")
        store.drop(HcType.SLEEP)
        assertNull(store.get(HcType.SLEEP))
        store.clear()
        assertTrue(store.all().isEmpty())
    }
}
