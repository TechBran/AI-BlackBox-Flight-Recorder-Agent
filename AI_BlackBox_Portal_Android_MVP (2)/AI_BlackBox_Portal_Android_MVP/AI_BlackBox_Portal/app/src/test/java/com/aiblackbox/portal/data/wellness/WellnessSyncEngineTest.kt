package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessFixtures.ZONE
import com.aiblackbox.portal.data.wellness.WellnessFixtures.at
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The collector end to end over the fake Health Connect: Changes API -> dirty days ->
 * buckets, sessions, readings -> outbox -> a signed push, with the box's answers. Time and
 * zone are injected; nothing touches a device or the network.
 */
class WellnessSyncEngineTest {

    private var now = at(3, 12)
    private val files = WellnessFiles(WellnessFixtures.tempDir("engine"))
    private val binding = WellnessBinding(files, clock = { now })
    private val gw = FakeHealthConnectGateway()
    private val tokens = ChangesTokenStore(files.tokens) { now }
    private val outbox = WellnessOutbox(files.outbox, files.quarantine)
    private val state = WellnessSyncStateStore(files.syncState)
    private val key = OriginSigner.encodeKey(ByteArray(32) { (it * 7).toByte() })

    /** What the "box" received: decoded payloads, and whether each signature verified. */
    private val received = mutableListOf<JsonObject>()
    private val signaturesOk = mutableListOf<Boolean>()
    var reply: (JsonObject) -> WellnessOutbox.SendResult = { WellnessOutbox.SendResult(200, """{"accepted":1}""") }
    private var stopped: Pair<Int, String?>? = null

    private val engine = WellnessSyncEngine(
        gateway = gw, binding = binding, tokens = tokens, dayIndex = DayIndex(files.dayIndex), outbox = outbox,
        state = state,
        send = { bytes, signer ->
            val header = signer.header("POST", "/wellness/ingest", bytes, now / 1000, "a".repeat(32))
            val sig = header.substringAfter("sig=")
            signaturesOk += sig == OriginSigner.signature(OriginSigner.decodeKey(key), "en_0badc0de", now / 1000,
                "a".repeat(32), "POST", "/wellness/ingest", bytes)
            val obj = Json.parseToJsonElement(WellnessOutbox.gunzip(bytes)).jsonObject
            received += obj
            reply(obj)
        },
        clock = { now },
        zone = { ZONE },
        newBatchId = WellnessFixtures.ids("e"),
        onStopped = { status, error -> stopped = status to error },
    )

    private fun enroll() {
        binding.installId()
        val intent = binding.beginIntent(WellnessFixtures.OPERATOR)
        binding.acceptBind(WellnessBindRequest(WellnessFixtures.OPERATOR, "en_0badc0de", 1, intent.nonce, key))
        state.update {
            it.copy(state = "active", hrOriginAuto = WellnessCatalog.SAMSUNG_HEALTH_PACKAGE, hrOriginDeviceType = "WATCH",
                hrOriginModel = "SM-R960", categories = it.categories + ("blood_pressure" to "ask"))
        }
    }

    private fun all(section: String) = received.flatMap { it.getValue(section).jsonArray.map { e -> e.jsonObject } }

    @Test
    fun `nothing happens before enrollment`() = runBlocking {
        assertEquals("not_enrolled", engine.sync(inForeground = true).outcome)
        assertEquals(0, gw.calls)
    }

    @Test
    fun `first run takes tokens, the next ships the changes signed, and tokens move after the outbox`() = runBlocking {
        enroll()
        assertEquals("ok", engine.sync(inForeground = false).outcome)
        assertTrue(received.isEmpty())
        assertEquals(12, tokens.all().size)

        val night = WellnessFixtures.sleepNight("sleep-1", 3)
        gw.upsert(night)
        gw.addHeartRate(WellnessFixtures.meta("hr-1"), WellnessFixtures.OFFSET_EST,
            WellnessFixtures.hrSeries(at(3, 1), at(3, 2), 60, 54.0))
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        gw.upsert(WellnessFixtures.steps("st-1", at(3, 9), at(3, 10)), 1500.0)
        val before = tokens.get(HcType.SLEEP)!!.token
        now = at(3, 13)
        val report = engine.sync(inForeground = false)
        assertEquals("ok", report.outcome)
        assertEquals(1, report.batchesQueued)
        assertEquals(1, report.batchesSent)
        assertEquals(0, report.pending)
        assertTrue(signaturesOk.all { it } && signaturesOk.isNotEmpty())
        assertFalse(before == tokens.get(HcType.SLEEP)!!.token)

        val payload = received.single()
        assertEquals("en_0badc0de", payload.getValue("enrollment_id").jsonPrimitive.content)
        assertEquals("changes", payload.getValue("window").jsonObject.getValue("type").jsonPrimitive.content)
        assertEquals(listOf("sleep-1"), all("sessions").map { it.getValue("hc_id").jsonPrimitive.content })
        assertEquals(listOf("bp-1"), all("readings").map { it.getValue("hc_id").jsonPrimitive.content })
        val buckets = all("buckets").map {
            Triple(it.getValue("metric").jsonPrimitive.content, it.getValue("grain").jsonPrimitive.content,
                it.getValue("replace_day").jsonPrimitive.content)
        }
        // The overnight session dirties both of its days for heart rate? No: sleep marks no
        // bucket day; the heart-rate record does (Nov 3), the steps record does (Nov 3).
        assertTrue(Triple("heart_rate", "5min", "2026-11-03") in buckets)
        assertTrue(Triple("heart_rate", "1min", "2026-11-03") in buckets)
        assertTrue(Triple("steps", "1h", "2026-11-03") in buckets)
        val hr5 = all("buckets").first { it.getValue("grain").jsonPrimitive.content == "5min" }
        assertEquals(12, hr5.getValue("rows").jsonArray.size)
        // Heart rate is keyed to the chosen origin (the watch), whatever key it sorts to.
        val hrOrigin = payload.getValue("origins").jsonArray.map { it.jsonObject }
            .single { it.getValue("key").jsonPrimitive.content == hr5.getValue("origin").jsonPrimitive.content }
        assertEquals(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE, hrOrigin.getValue("package").jsonPrimitive.content)
        assertEquals("WATCH", hrOrigin.getValue("device_type").jsonPrimitive.content)
        // A third run with no changes sends nothing new.
        received.clear()
        engine.sync(inForeground = false)
        assertTrue(received.isEmpty())
    }

    @Test
    fun `deletions - a known session id ships in deleted_ids, an unknown heart-rate id re-aggregates 30 days`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.sleepNight("sleep-1", 3))
        engine.sync(false)
        received.clear()

        gw.delete(HcType.SLEEP, "sleep-1")
        engine.sync(false)
        assertEquals(listOf("sleep-1"), received.flatMap { it.getValue("deleted_ids").jsonArray.map { e -> e.jsonPrimitive.content } })
        assertTrue(all("buckets").isEmpty())
        received.clear()

        gw.delete(HcType.HEART_RATE, "hr-never-indexed")
        engine.sync(false)
        val days = all("buckets").filter { it.getValue("metric").jsonPrimitive.content == "heart_rate" }
            .map { it.getValue("replace_day").jsonPrimitive.content }.toSet()
        assertEquals(30, days.size)
        assertTrue("2026-11-03" in days && "2026-10-05" in days)
        // Bucketed ids are never sent as deleted ids.
        assertTrue(received.all { it.getValue("deleted_ids").jsonArray.isEmpty() })
    }

    @Test
    fun `foreground-only mode - no background grant means no background read, but a visible app reads`() = runBlocking {
        enroll()
        gw.granted = gw.granted - WellnessCatalog.PERMISSION_BACKGROUND
        assertEquals("skipped:foreground_only", engine.sync(inForeground = false).outcome)
        assertEquals("ok", engine.sync(inForeground = true).outcome)
    }

    @Test
    fun `revoked permissions narrow what is read and consent off means not read`() = runBlocking {
        enroll()
        state.update { it.copy(categories = it.categories + ("sleep" to "off")) }
        engine.sync(false)
        assertNull(tokens.get(HcType.SLEEP))
        assertNotNull(tokens.get(HcType.HEART_RATE))
        gw.granted = emptySet()
        assertEquals("skipped:no_permissions", engine.sync(false).outcome)
    }

    @Test
    fun `401 stops until reconnect and keeps the batch`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(401, """{"error":"bad_signature"}""") }
        assertEquals("reconnect", engine.sync(false).outcome)
        assertTrue(state.load().needsReconnect)
        assertEquals(1, outbox.size())
        assertEquals("skipped:stopped", engine.sync(false).outcome)
    }

    @Test
    fun `410 source_moved clears the phone's wellness state`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(410, """{"error":"source_moved"}""") }
        val report = engine.sync(false)
        assertEquals("stopped:source_moved", report.outcome)
        assertEquals(410 to "source_moved", stopped)
        assertNull(binding.current())
        assertEquals(0, outbox.size())
        assertTrue(tokens.all().isEmpty())
        // The install id survives (it names the install, not the person's data).
        assertTrue(files.installId.exists())
    }

    @Test
    fun `429 with Retry-After backs off and skips until then`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(429, "", retryAfterS = 600) }
        assertEquals("backoff", engine.sync(false).outcome)
        assertEquals(now + 600_000, state.load().backoffUntilMs)
        assertEquals("skipped:backoff", engine.sync(false).outcome)
        now += 600_001
        reply = { WellnessOutbox.SendResult(200, "{}") }
        assertEquals("ok", engine.sync(false).outcome)
        assertEquals(0, outbox.size())
    }

    @Test
    fun `a quota refusal backs off and leaves the tokens where they were`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.sleepNight("sleep-1", 3))
        val before = tokens.all().mapValues { it.value.token }
        gw.quotaAfterCalls = gw.calls + 2
        engine.sync(false)
        assertEquals(before, tokens.all().mapValues { it.value.token })
        assertEquals(1, state.load().quotaAttempt)
        assertEquals(now + 60_000, state.load().backoffUntilMs)
    }

    @Test
    fun `a full outbox holds the tokens (nothing is lost while Health Connect has it)`() = runBlocking {
        val tinyOutbox = WellnessOutbox(files.outbox, files.quarantine, maxBatches = 1)
        val e = WellnessSyncEngine(gw, binding, tokens, DayIndex(files.dayIndex), tinyOutbox, state,
            send = { _, _ -> WellnessOutbox.SendResult(-1) }, clock = { now }, zone = { ZONE })
        enroll()
        e.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        e.sync(false)          // queues one batch; the box is unreachable
        assertEquals(1, tinyOutbox.size())
        gw.upsert(WellnessFixtures.bp("bp-2", 3, 8))
        val before = tokens.get(HcType.BLOOD_PRESSURE)!!.token
        e.sync(false)          // full: collection is skipped, tokens stay
        assertEquals(before, tokens.get(HcType.BLOOD_PRESSURE)!!.token)
        assertEquals(1, tinyOutbox.size())
    }

    @Test
    fun `an expired token re-reads sessions from synced_until minus 48 hours`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.sleepNight("sleep-1", 3))
        received.clear()
        gw.expireTokens = true
        engine.sync(false)
        assertEquals(listOf("sleep-1"), all("sessions").map { it.getValue("hc_id").jsonPrimitive.content })
        gw.expireTokens = false
    }

    @Test
    fun `backfill takes tokens first, pages newest to oldest, and persists the cursor per page`() = runBlocking {
        enroll()
        // History: a night and a reading three weeks ago, and last night.
        gw.upsert(WellnessFixtures.sleepNight("sleep-old", 12).copy(
            startMs = at(12, 23, month = 10), endMs = at(13, 6, month = 10), stages = emptyList()))
        gw.upsert(WellnessFixtures.bp("bp-old", 13, 7).copy(timeMs = at(13, 7, month = 10)))
        gw.upsert(WellnessFixtures.sleepNight("sleep-new", 3))
        val first = engine.backfillPage()
        assertTrue(first is WellnessSyncEngine.BackfillStep.Progress)
        assertEquals(12, tokens.all().size)
        first as WellnessSyncEngine.BackfillStep.Progress
        assertEquals(1, first.pagesDone)
        assertTrue(first.pagesTotal >= 3)
        assertEquals("backfill", received.first().getValue("window").jsonObject.getValue("type").jsonPrimitive.content)
        assertTrue(all("sessions").any { it.getValue("hc_id").jsonPrimitive.content == "sleep-new" })
        var steps = 1
        while (engine.backfillPage() is WellnessSyncEngine.BackfillStep.Progress) steps++
        assertEquals(WellnessSyncEngine.BackfillStep.Done, engine.backfillPage())
        val cursor = state.load().backfill
        assertTrue(cursor.done)
        assertEquals(cursor.pagesTotal, cursor.pagesDone)
        val ids = all("sessions").map { it.getValue("hc_id").jsonPrimitive.content } +
            all("readings").map { it.getValue("hc_id").jsonPrimitive.content }
        assertTrue("sleep-old" in ids && "bp-old" in ids)
        // Newest first: the last page shipped holds the oldest data.
        assertTrue(all("sessions").first().getValue("hc_id").jsonPrimitive.content == "sleep-new")
        assertEquals(state.load().earliestAvailableMs, cursor.floorMs)
    }

    @Test
    fun `without the history grant the backfill stops at 30 days`() = runBlocking {
        enroll()
        gw.granted = gw.granted - WellnessCatalog.PERMISSION_HISTORY
        gw.upsert(WellnessFixtures.bp("bp-old", 1, 7).copy(timeMs = at(1, 7, month = 9)))
        gw.upsert(WellnessFixtures.sleepNight("sleep-new", 3))
        while (engine.backfillPage() is WellnessSyncEngine.BackfillStep.Progress) Unit
        val floor = state.load().backfill.floorMs!!
        assertTrue(floor >= now - 31 * BucketBuilder.DAY_MS)
        assertFalse(all("readings").any { it.getValue("hc_id").jsonPrimitive.content == "bp-old" })
    }

    @Test
    fun `a category turned on after the import finished runs the import again`() = runBlocking {
        enroll()
        state.update { it.copy(categories = it.categories + ("blood_pressure" to "off")) }
        while (engine.backfillPage() is WellnessSyncEngine.BackfillStep.Progress) Unit
        assertTrue(state.load().backfill.done)
        engine.sync(false)
        assertTrue(state.load().backfill.done)
        // Blood pressure turned on: its type is seen for the first time, so history is due.
        state.update { it.copy(categories = it.categories + ("blood_pressure" to "ask")) }
        engine.sync(false)
        assertFalse(state.load().backfill.done)
        assertNotNull(tokens.get(HcType.BLOOD_PRESSURE))
    }

    @Test
    fun `a full outbox holds the backfill cursor`() = runBlocking {
        val tiny = WellnessOutbox(files.outbox, files.quarantine, maxBatches = 1)
        val e = WellnessSyncEngine(gw, binding, tokens, DayIndex(files.dayIndex), tiny, state,
            send = { _, _ -> WellnessOutbox.SendResult(-1) }, clock = { now }, zone = { ZONE })
        enroll()
        gw.upsert(WellnessFixtures.sleepNight("sleep-1", 3))
        // Older history, so the import has more than one page.
        gw.upsert(WellnessFixtures.sleepNight("sleep-old", 3).copy(startMs = at(10, 23, month = 10), endMs = at(11, 6, month = 10)))
        assertTrue(e.backfillPage() is WellnessSyncEngine.BackfillStep.Progress)
        val cursor = state.load().backfill.cursorMs
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("outbox_full"), e.backfillPage())
        assertEquals(cursor, state.load().backfill.cursorMs)
    }

    @Test
    fun `paused enrollments neither sync nor backfill`() = runBlocking {
        enroll()
        state.update { it.copy(state = "paused") }
        assertEquals("skipped:paused", engine.sync(true).outcome)
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("paused"), engine.backfillPage())
    }
}
