package com.aiblackbox.portal.data.wellness

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

class WellnessOutboxTest {

    private val dir = WellnessFixtures.tempDir("outbox")
    private val outbox = WellnessOutbox(File(dir, "outbox"))

    private fun batch(id: String, readings: Int = 1): String = IngestPayloadBuilder.toJson(
        IngestPayloadBuilder.Batch(
            batchId = id, enrollmentId = "en_0badc0de", epoch = 1,
            window = IngestPayloadBuilder.Window("changes", 0, 1),
            readings = (1..readings).map {
                BucketBuilder.reading(WellnessFixtures.spo2("r$id-$it", 3, 3, 95.0 + it % 3))
            },
        ),
    )

    private fun idOf(bytes: ByteArray): String =
        Json.parseToJsonElement(WellnessOutbox.gunzip(bytes)).jsonObject.getValue("batch_id").jsonPrimitive.content

    @Test
    fun `batches are durable gzip files that survive a reopen, oldest first`() {
        outbox.enqueue(batch("a"))
        outbox.enqueue(batch("b"))
        outbox.enqueue(batch("c"))
        val reopened = WellnessOutbox(File(dir, "outbox"))
        val pending = reopened.pending()
        assertEquals(listOf("1.json.gz", "2.json.gz", "3.json.gz"), pending.map { it.name })
        assertEquals(listOf("a", "b", "c"), pending.map { idOf(reopened.read(it)) })
        val raw = pending[0].file.readBytes()
        assertTrue("gzip magic", raw[0] == 0x1f.toByte() && raw[1] == 0x8b.toByte())
        assertFalse(File(dir, "outbox/1.json.gz.tmp").exists())
    }

    @Test
    fun `200 deletes in order and collects pending notices`() = runBlocking {
        listOf("a", "b").forEach { outbox.enqueue(batch(it)) }
        val sentIds = mutableListOf<String>()
        val result = outbox.drain(send = { bytes ->
            sentIds += idOf(bytes)
            WellnessOutbox.SendResult(200, """{"accepted":1,"dropped":0,"next_min_interval_s":900,
                "pending_notices":[{"id":"n1","kind":"setup","title":"t","body":"b","created_at":"x"}]}""")
        }, newBatchId = { "x" })
        assertEquals(listOf("a", "b"), sentIds)
        assertTrue(result is WellnessOutbox.DrainResult.Drained)
        assertEquals(2, result.sent)
        assertEquals(2, result.notices.size)
        assertEquals(0, outbox.size())
    }

    @Test
    fun `401 stops and keeps everything for after a reconnect`() = runBlocking {
        listOf("a", "b").forEach { outbox.enqueue(batch(it)) }
        val result = outbox.drain(send = { WellnessOutbox.SendResult(401, """{"error":"bad_signature"}""") }, newBatchId = { "x" })
        assertTrue(result is WellnessOutbox.DrainResult.StopReconnect)
        assertEquals(2, outbox.size())
    }

    @Test
    fun `409 and 410 stop and report the box's reason`() = runBlocking {
        outbox.enqueue(batch("a"))
        for ((status, error) in listOf(409 to "owner_changed", 410 to "source_moved", 410 to "revoked")) {
            val result = outbox.drain(send = { WellnessOutbox.SendResult(status, """{"error":"$error"}""") }, newBatchId = { "x" })
            result as WellnessOutbox.DrainResult.StopAndClear
            assertEquals(status, result.status)
            assertEquals(error, result.error)
        }
        // The outbox itself is cleared by the caller (binding.clearAll), not by the drain.
        assertEquals(1, outbox.size())
    }

    @Test
    fun `413 splits into halves that keep their place in the queue`() = runBlocking {
        outbox.enqueue(batch("big", readings = 4))
        outbox.enqueue(batch("next"))
        val ids = WellnessFixtures.ids("h")
        val seen = mutableListOf<Pair<String, Int>>()
        val result = outbox.drain(send = { bytes ->
            val obj = Json.parseToJsonElement(WellnessOutbox.gunzip(bytes)).jsonObject
            val n = obj.getValue("readings").jsonArray.size
            seen += obj.getValue("batch_id").jsonPrimitive.content to n
            if (n > 2) WellnessOutbox.SendResult(413) else WellnessOutbox.SendResult(200, "{}")
        }, newBatchId = ids)
        assertTrue(result is WellnessOutbox.DrainResult.Drained)
        assertEquals(listOf(4, 2, 2, 1), seen.map { it.second })
        assertEquals("next", seen.last().first)
        assertEquals(0, outbox.size())
        assertEquals(listOf(listOf(1L, 1L), listOf(1L, 2L), listOf(2L)),
            listOf("1-1.json.gz", "1-2.json.gz", "2.json.gz").map { WellnessOutbox.sortKey(it) })
    }

    @Test
    fun `a 413 on a single item cannot split and is quarantined`() = runBlocking {
        outbox.enqueue(batch("one"))
        outbox.enqueue(batch("two"))
        var calls = 0
        outbox.drain(send = { calls++; if (calls == 1) WellnessOutbox.SendResult(413) else WellnessOutbox.SendResult(200, "{}") },
            newBatchId = { "x" })
        assertEquals(1, outbox.quarantinedCount())
        assertEquals(0, outbox.size())
    }

    @Test
    fun `422 quarantines, counts, and the next batch still goes`() = runBlocking {
        listOf("bad", "good").forEach { outbox.enqueue(batch(it)) }
        val result = outbox.drain(send = { bytes ->
            if (idOf(bytes) == "bad") WellnessOutbox.SendResult(422, """{"error":"invalid","detail":"x"}""")
            else WellnessOutbox.SendResult(200, "{}")
        }, newBatchId = { "x" })
        assertEquals(1, result.sent)
        assertEquals(1, outbox.quarantinedCount())
        assertEquals(0, outbox.size())
        assertTrue(File(dir, "outbox/quarantine/1.json.gz").exists())
    }

    @Test
    fun `429 and 503 back off with Retry-After, a network failure retries later, nothing is lost`() = runBlocking {
        outbox.enqueue(batch("a"))
        val r429 = outbox.drain(send = { WellnessOutbox.SendResult(429, "", retryAfterS = 120) }, newBatchId = { "x" })
        assertEquals(120L, (r429 as WellnessOutbox.DrainResult.BackOff).retryAfterS)
        assertTrue(outbox.drain(send = { WellnessOutbox.SendResult(503) }, newBatchId = { "x" }) is WellnessOutbox.DrainResult.BackOff)
        assertTrue(outbox.drain(send = { WellnessOutbox.SendResult(-1) }, newBatchId = { "x" }) is WellnessOutbox.DrainResult.RetryLater)
        assertEquals(1, outbox.size())
    }

    @Test
    fun `the cap is a batch count or a byte size, and a full outbox refuses`() {
        val small = WellnessOutbox(File(dir, "capped"), maxBatches = 2)
        small.enqueue(batch("a"))
        small.enqueue(batch("b"))
        assertTrue(small.isFull())
        try {
            small.enqueue(batch("c"))
            fail("a full outbox must refuse")
        } catch (e: OutboxFullException) {
            // expected
        }
        val tiny = WellnessOutbox(File(dir, "bytes"), maxBytes = 10)
        tiny.enqueue(batch("a"))
        assertTrue(tiny.isFull())
        assertEquals(WellnessOutbox.MAX_BYTES, 64L * 1024 * 1024)
        assertEquals(WellnessOutbox.MAX_BATCHES, 10_000)
    }

    @Test
    fun `stray files are ignored and clear empties the outbox`() {
        File(dir, "outbox").mkdirs()
        File(dir, "outbox/notes.txt").writeText("x")
        File(dir, "outbox/abc.json.gz").writeText("x")
        File(dir, "outbox/5.json.gz.tmp").writeText("x")
        outbox.enqueue(batch("a"))
        assertEquals(listOf("1.json.gz"), outbox.pending().map { it.name })
        outbox.clear()
        assertEquals(0, outbox.size())
    }
}
