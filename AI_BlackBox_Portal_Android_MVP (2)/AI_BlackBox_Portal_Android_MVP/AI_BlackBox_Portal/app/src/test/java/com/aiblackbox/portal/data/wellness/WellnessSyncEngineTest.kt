package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessFixtures.ZONE
import com.aiblackbox.portal.data.wellness.WellnessFixtures.at
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
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

    /** Signed status reads the engine made, and what the stand-in box answers them. */
    private var statusCalls = 0

    /** Settings PUTs the engine made (the body, and whether the signature verified). */
    private val settingsPuts = mutableListOf<Pair<String, Boolean>>()
    private var settingsRefusedNotices = 0

    private fun newEngine(
        put: (suspend (String) -> WellnessOutbox.SendResult)? = null,
        fetch: (suspend () -> WellnessOutbox.SendResult)? = null,
    ) = WellnessSyncEngine(
        gateway = gw, binding = binding, tokens = tokens, dayIndex = DayIndex(files.dayIndex), outbox = outbox,
        state = state,
        send = { bytes, signer ->
            val obj = Json.parseToJsonElement(WellnessOutbox.gunzip(bytes)).jsonObject
            val enr = obj.getValue("enrollment_id").jsonPrimitive.content
            val header = signer.header("POST", "/wellness/ingest", bytes, now / 1000, "a".repeat(32))
            val sig = header.substringAfter("sig=")
            signaturesOk += sig == OriginSigner.signature(OriginSigner.decodeKey(keyFor(enr)), enr, now / 1000,
                "a".repeat(32), "POST", "/wellness/ingest", bytes)
            received += obj
            reply(obj)
        },
        clock = { now },
        zone = { ZONE },
        newBatchId = WellnessFixtures.ids("e"),
        onStopped = { status, error -> stopped = status to error },
        onReconnectNeeded = { reconnectNotices++ },
        fetchStatus = fetch?.let { f -> { _, _ -> statusCalls++; f() } },
        putSettings = put?.let { p ->
            { body, signer ->
                val bytes = body.toByteArray()
                val header = signer.header("PUT", "/wellness/settings", bytes, now / 1000, "b".repeat(32))
                val enr = header.substringAfter("enr=").substringBefore(';')
                settingsPuts += body to (header.substringAfter("sig=") == OriginSigner.signature(OriginSigner.decodeKey(keyFor(enr)),
                    enr, now / 1000, "b".repeat(32), "PUT", "/wellness/settings", bytes))
                p(body)
            }
        },
        onSetupSettingsRefused = { settingsRefusedNotices++ },
    )

    private val engine = newEngine()
    private var reconnectNotices = 0

    /** A second enrollment's key (a re-enroll mints a new one). */
    private val key2 = OriginSigner.encodeKey(ByteArray(32) { (it * 11 + 1).toByte() })

    private fun keyFor(enr: String) = if (enr == "en_12ab34cd") key2 else key

    /** The box re-enrolls this phone (Reconnect Health, or replace): a new enrollment and key. */
    private fun reEnroll() {
        val intent = binding.beginIntent(WellnessFixtures.OPERATOR)
        assertTrue(binding.acceptBind(WellnessBindRequest(WellnessFixtures.OPERATOR, "en_12ab34cd", 2, intent.nonce, key2))
            is BindDecision.Accepted)
    }

    /** A credentialed `GET /wellness/status` body. */
    private fun statusBody(categories: Map<String, String>, state: String = "active"): String = buildJsonObject {
        put("state", JsonPrimitive(state))
        put("enrolled", JsonPrimitive(true))
        put("troubles", JsonArray(emptyList()))
        put("categories", JsonObject(categories.mapValues { JsonPrimitive(it.value) }))
        put("local_only", JsonObject(emptyMap()))
        put("providers_allowed", JsonArray(emptyList()))
    }.toString()

    private val allAsk = mapOf("sleep" to "ask", "heart" to "ask", "activity" to "ask", "blood_pressure" to "ask")

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
        // One notice per refusal: a retry ("Sync now" clears the flag) that is refused again
        // notifies again; runs that are skipped while stopped never do.
        assertEquals(1, reconnectNotices)
        state.update { it.copy(needsReconnect = false) }
        assertEquals("reconnect", engine.sync(false).outcome)
        assertEquals(2, reconnectNotices)
        assertEquals("skipped:stopped", engine.sync(false).outcome)
        assertEquals(2, reconnectNotices)
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
        val first = engine.backfillPage(inForeground = false)
        assertTrue(first is WellnessSyncEngine.BackfillStep.Progress)
        assertEquals(12, tokens.all().size)
        first as WellnessSyncEngine.BackfillStep.Progress
        assertEquals(1, first.pagesDone)
        assertTrue(first.pagesTotal >= 3)
        assertEquals("backfill", received.first().getValue("window").jsonObject.getValue("type").jsonPrimitive.content)
        assertTrue(all("sessions").any { it.getValue("hc_id").jsonPrimitive.content == "sleep-new" })
        var steps = 1
        while (engine.backfillPage(inForeground = false) is WellnessSyncEngine.BackfillStep.Progress) steps++
        assertEquals(WellnessSyncEngine.BackfillStep.Done, engine.backfillPage(inForeground = false))
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
        while (engine.backfillPage(inForeground = false) is WellnessSyncEngine.BackfillStep.Progress) Unit
        val floor = state.load().backfill.floorMs!!
        assertTrue(floor >= now - 31 * BucketBuilder.DAY_MS)
        assertFalse(all("readings").any { it.getValue("hc_id").jsonPrimitive.content == "bp-old" })
    }

    @Test
    fun `a category turned on after the import finished runs the import again`() = runBlocking {
        enroll()
        state.update { it.copy(categories = it.categories + ("blood_pressure" to "off")) }
        while (engine.backfillPage(inForeground = false) is WellnessSyncEngine.BackfillStep.Progress) Unit
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
        assertTrue(e.backfillPage(inForeground = false) is WellnessSyncEngine.BackfillStep.Progress)
        val cursor = state.load().backfill.cursorMs
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("outbox_full"), e.backfillPage(inForeground = false))
        assertEquals(cursor, state.load().backfill.cursorMs)
    }

    @Test
    fun `paused enrollments neither sync nor backfill`() = runBlocking {
        enroll()
        state.update { it.copy(state = "paused") }
        assertEquals("skipped:paused", engine.sync(true).outcome)
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("paused"), engine.backfillPage(inForeground = false))
    }

    // ── review fixes: binding changes mid-run (the generation fence) ──────────────────────

    @Test
    fun `a re-enroll while a push is in flight keeps the new binding (the old key's 410 clears nothing)`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        // The box re-enrolls this phone and tombstones the old enrollment while its push is on the wire.
        reply = { p ->
            if (p.getValue("enrollment_id").jsonPrimitive.content == "en_0badc0de") {
                reEnroll()
                WellnessOutbox.SendResult(410, """{"error":"revoked"}""")
            } else WellnessOutbox.SendResult(200, "{}")
        }
        assertEquals("stale", engine.sync(false).outcome)
        assertEquals("en_12ab34cd", binding.current()!!.enrollmentId)
        assertNull("no 'turned off from your box' notice for a phone that was just set up again", stopped)
        // The new enrollment collects and signs with its own key.
        received.clear()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-2", 3, 8))
        assertEquals("ok", engine.sync(false).outcome)
        assertEquals(listOf("en_12ab34cd"), received.map { it.getValue("enrollment_id").jsonPrimitive.content }.distinct())
        assertTrue(signaturesOk.all { it })
    }

    @Test
    fun `a re-enroll during the reads of a sync writes nothing for the old enrollment`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.steps("st-1", at(3, 9), at(3, 10)), 1500.0)
        gw.duringNextCall = { reEnroll() }
        assertEquals("stale", engine.sync(false).outcome)
        assertTrue(received.isEmpty())
        assertEquals(0, outbox.size())
        // The re-enroll reset the tokens; the old run advanced none of them afterwards.
        assertTrue(tokens.all().isEmpty())
        assertEquals("en_12ab34cd", binding.current()!!.enrollmentId)
    }

    @Test
    fun `a re-enroll during a history page keeps the new binding and stops the old import`() = runBlocking {
        enroll()
        gw.upsert(WellnessFixtures.sleepNight("sleep-1", 3))
        gw.duringNextCall = { reEnroll() }
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("stale"), engine.backfillPage(inForeground = false))
        assertEquals("en_12ab34cd", binding.current()!!.enrollmentId)
        assertTrue(received.isEmpty())
        assertEquals(0, outbox.size())
        assertFalse(state.load().backfill.done)
        assertNull(state.load().backfill.cursorMs)
    }

    @Test
    fun `an unbind during a run keeps nothing (no outbox, tokens or index come back)`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.steps("st-1", at(3, 9), at(3, 10)), 1500.0)
        gw.addHeartRate(WellnessFixtures.meta("hr-1"), WellnessFixtures.OFFSET_EST,
            WellnessFixtures.hrSeries(at(3, 1), at(3, 2), 60, 54.0))
        reply = { WellnessOutbox.SendResult(-1) }
        gw.duringNextCall = { binding.clearAll() }
        assertEquals("stale", engine.sync(false).outcome)
        assertNull(binding.current())
        assertEquals(listOf("install_id"), files.root.listFiles()!!.map { it.name }.sorted())
    }

    @Test
    fun `an unbind while a push is in flight keeps nothing either`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { binding.clearAll(); WellnessOutbox.SendResult(413, "") }
        assertEquals("stale", engine.sync(false).outcome)
        assertEquals(listOf("install_id"), files.root.listFiles()!!.map { it.name }.sorted())
    }

    // ── review fixes: the history import obeys the same stops as the sync ─────────────────

    private fun historyOverWeeks() {
        gw.upsert(WellnessFixtures.sleepNight("sleep-new", 3))
        gw.upsert(WellnessFixtures.sleepNight("sleep-old", 3).copy(startMs = at(1, 23, month = 9), endMs = at(2, 6, month = 9)))
    }

    @Test
    fun `the history import stops at a 401 and reads no further page`() = runBlocking {
        enroll()
        historyOverWeeks()
        reply = { WellnessOutbox.SendResult(401, """{"error":"bad_signature"}""") }
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("reconnect"), engine.backfillPage(inForeground = false))
        assertTrue(state.load().needsReconnect)
        val reads = gw.reads
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("stopped"), engine.backfillPage(inForeground = false))
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("stopped"), engine.backfillPage(inForeground = false))
        assertEquals("no Health Connect read after the refusal", reads, gw.reads)
        assertEquals("one refused push (and its one retry), not one per page", 2, received.size)
        assertEquals(1, reconnectNotices)
        val st = state.load()
        assertFalse(WellnessRules.shouldStartBackfill(st.backfill.done, st.needsReconnect, st.localHold != null, st.enrollmentState()))
    }

    @Test
    fun `without the background grant the import reads only while the app is visible`() = runBlocking {
        enroll()
        gw.granted = gw.granted - WellnessCatalog.PERMISSION_BACKGROUND
        historyOverWeeks()
        val reads = gw.reads
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("foreground_only"), engine.backfillPage(inForeground = false))
        assertEquals(reads, gw.reads)
        assertTrue(received.isEmpty())
        assertTrue(engine.backfillPage(inForeground = true) is WellnessSyncEngine.BackfillStep.Progress)
    }

    @Test
    fun `a quota back-off re-queues a hidden retry only with the background grant`() = runBlocking {
        enroll()
        historyOverWeeks()
        gw.granted = gw.granted - WellnessCatalog.PERMISSION_BACKGROUND
        gw.quotaAfterCalls = gw.calls
        val fg = engine.backfillPage(inForeground = true) as WellnessSyncEngine.BackfillStep.QuotaBackoff
        assertFalse(fg.requeue)
        gw.granted = gw.granted + WellnessCatalog.PERMISSION_BACKGROUND
        val bg = engine.backfillPage(inForeground = false) as WellnessSyncEngine.BackfillStep.QuotaBackoff
        assertTrue(bg.requeue)
    }

    @Test
    fun `a background run in foreground-only mode still delivers what the outbox holds`() = runBlocking {
        enroll()
        engine.sync(true)
        gw.granted = gw.granted - WellnessCatalog.PERMISSION_BACKGROUND
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(-1) }
        engine.sync(inForeground = true)            // read while open; the box was unreachable
        assertEquals(1, outbox.size())
        reply = { WellnessOutbox.SendResult(200, "{}") }
        val reads = gw.reads
        val report = engine.sync(inForeground = false)
        assertEquals("skipped:foreground_only", report.outcome)
        assertEquals(1, report.batchesSent)
        assertEquals(0, outbox.size())
        assertEquals("delivered without reading Health Connect", reads, gw.reads)
    }

    // ── review fixes: narrowing on another surface reaches the collector ──────────────────

    @Test
    fun `a category turned off on another surface stops its reads and is never delivered from the outbox`() = runBlocking {
        var boxCategories = allAsk
        val e = newEngine { WellnessOutbox.SendResult(200, statusBody(boxCategories)) }
        enroll()
        e.sync(false)
        gw.upsert(WellnessFixtures.sleepNight("sleep-1", 3))
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(-1) }
        now += 61_000
        e.sync(false)                               // read with consent; the box was unreachable
        assertEquals(1, outbox.size())
        // On the Portal: blood pressure off (narrowing needs no signature there).
        boxCategories = allAsk + ("blood_pressure" to "off")
        reply = { WellnessOutbox.SendResult(200, "{}") }
        gw.upsert(WellnessFixtures.bp("bp-2", 3, 8))
        now += 61_000
        received.clear()
        assertEquals("ok", e.sync(false).outcome)
        assertEquals("off", state.load().categories["blood_pressure"])
        assertTrue("no blood pressure leaves the phone after it was turned off", all("readings").isEmpty())
        assertEquals(listOf("sleep-1"), all("sessions").map { it.getValue("hc_id").jsonPrimitive.content })
        assertEquals(0, outbox.size())
    }

    @Test
    fun `a batch with nothing left after the consent filter is dropped without a request`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(-1) }
        engine.sync(false)
        assertEquals(1, outbox.size())
        state.update { it.copy(categories = it.categories + ("blood_pressure" to "off")) }
        received.clear()
        reply = { WellnessOutbox.SendResult(200, "{}") }
        val report = engine.sync(false)
        assertTrue(received.isEmpty())
        assertEquals(0, report.batchesSent)
        assertEquals(0, outbox.size())
        assertEquals(0L, state.load().lastPushMs)
    }

    @Test
    fun `a pause on another surface stops the collector, from the status read or a 503`() = runBlocking {
        val e = newEngine { WellnessOutbox.SendResult(200, statusBody(allAsk, state = "paused")) }
        enroll()
        val reads = gw.reads
        assertEquals("skipped:paused", e.sync(true).outcome)
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("paused"), e.backfillPage(inForeground = true))
        assertEquals(reads, gw.reads)

        // Without a status read the push says it: 503 paused.
        state.update { it.copy(state = "active") }
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(503, """{"error":"paused"}""", retryAfterS = 3600) }
        assertEquals("paused", engine.sync(false).outcome)
        assertEquals("skipped:paused", engine.sync(false).outcome)
        assertEquals(1, outbox.size())
    }

    @Test
    fun `an ended enrollment is learned from the status read even with nothing to push`() = runBlocking {
        val revoked = newEngine { WellnessOutbox.SendResult(410, """{"error":"revoked"}""") }
        enroll()
        assertEquals("stopped:revoked", revoked.sync(false).outcome)
        assertNull(binding.current())
        assertEquals(410 to "revoked", stopped)

        val refused = newEngine { WellnessOutbox.SendResult(401, """{"error":"bad_signature"}""") }
        enroll()
        assertEquals("reconnect", refused.sync(false).outcome)
        assertTrue(state.load().needsReconnect)
        assertEquals(1, reconnectNotices)
    }

    // ── review fix: a replacement's setup choices wait for the approval ──────────────────

    private val waiting = PendingSetupSettings(localOnly = mapOf("sleep" to false), providersAllowed = listOf("anthropic"))

    @Test
    fun `a replacement's setup choices go in one signed PUT once the old phone approved, then are cleared`() = runBlocking {
        var approved = false
        val e = newEngine(
            fetch = {
                if (approved) WellnessOutbox.SendResult(200, statusBody(allAsk))
                // Waiting: this phone reads what anyone reads (four fields, the old enrollment's state).
                else WellnessOutbox.SendResult(200, """{"state":"active","enrolled":true,"source_label":"Old phone","last_sync":null}""")
            },
            put = { WellnessOutbox.SendResult(200, statusBody(allAsk)) },
        )
        enroll()
        state.update { it.copy(state = "replace_pending", pendingSettings = waiting) }
        e.sync(false)
        assertTrue("nothing is sent while the replacement waits", settingsPuts.isEmpty())
        assertEquals(waiting, state.load().pendingSettings)

        approved = true
        now += 61_000
        e.sync(false)
        assertEquals(1, settingsPuts.size)
        val (body, signed) = settingsPuts.single()
        assertTrue("signed with this phone's key", signed)
        val sent = Json.parseToJsonElement(body).jsonObject
        assertEquals(listOf("anthropic"), sent.getValue("providers_allowed").jsonArray.map { it.jsonPrimitive.content })
        assertEquals("false", sent.getValue("local_only").jsonObject.getValue("sleep").jsonPrimitive.content)
        assertNull(state.load().pendingSettings)
        assertEquals("active", state.load().state)

        now += 61_000
        e.sync(false)
        assertEquals("sent once", 1, settingsPuts.size)
    }

    @Test
    fun `no answer keeps the waiting choices, a refusal drops them and tells the person`() = runBlocking {
        var answer = WellnessOutbox.SendResult(-1)
        val e = newEngine(fetch = { WellnessOutbox.SendResult(200, statusBody(allAsk)) }, put = { answer })
        enroll()
        state.update { it.copy(pendingSettings = waiting) }
        e.sync(false)
        assertEquals(1, settingsPuts.size)
        assertEquals(waiting, state.load().pendingSettings)
        assertEquals(0, settingsRefusedNotices)

        answer = WellnessOutbox.SendResult(422, """{"error":"invalid_provider"}""")
        now += 61_000
        e.sync(false)
        assertEquals(2, settingsPuts.size)
        assertNull(state.load().pendingSettings)
        assertEquals(1, settingsRefusedNotices)
    }

    @Test
    fun `an unreachable box costs one status attempt a minute, and the run carries on`() = runBlocking {
        val e = newEngine { WellnessOutbox.SendResult(-1) }
        enroll()
        assertEquals("ok", e.sync(false).outcome)
        assertEquals("ok", e.sync(false).outcome)
        assertEquals(1, statusCalls)
        now += 61_000
        e.sync(false)
        assertEquals(2, statusCalls)
    }

    @Test
    fun `a narrowing held on this phone stops every read and push`() = runBlocking {
        enroll()
        engine.sync(false)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        reply = { WellnessOutbox.SendResult(-1) }
        engine.sync(false)
        state.update { it.copy(localHold = WellnessRules.HOLD_DISCONNECT) }
        reply = { WellnessOutbox.SendResult(200, "{}") }
        received.clear()
        val reads = gw.reads
        assertEquals("skipped:held", engine.sync(true).outcome)
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped("held"), engine.backfillPage(inForeground = true))
        assertTrue(received.isEmpty())
        assertEquals(reads, gw.reads)
        assertEquals(1, outbox.size())
    }

    // ── the privacy policy re-prompt (contract (g)) ───────────────────────────────────────

    /** A credentialed status whose `policy_version` is [policy]. */
    private fun statusWithPolicy(policy: String): String =
        JsonObject(Json.parseToJsonElement(statusBody(allAsk)).jsonObject + ("policy_version" to JsonPrimitive(policy))).toString()

    @Test
    fun `a new policy in the signed status holds every read and push until the person agrees`() = runBlocking {
        var policy = "2026-10-29"
        val e = newEngine { WellnessOutbox.SendResult(200, statusWithPolicy(policy)) }
        enroll()
        state.update { WellnessRules.enrolledUnderPolicy(it, "2026-10-29") }
        e.sync(false)
        gw.upsert(WellnessFixtures.sleepNight("sleep-1", 3))
        reply = { WellnessOutbox.SendResult(-1) }
        now += 61_000
        e.sync(false)                               // read under the agreed policy; the box was unreachable
        assertEquals(1, outbox.size())

        policy = "2026-11-15"
        reply = { WellnessOutbox.SendResult(200, "{}") }
        received.clear()
        now += 61_000
        gw.upsert(WellnessFixtures.sleepNight("sleep-2", 4))
        val reads = gw.reads
        assertEquals(WellnessSyncEngine.POLICY_HELD, e.sync(true).outcome)
        assertEquals(WellnessSyncEngine.BackfillStep.Skipped(WellnessRules.POLICY_CHANGED), e.backfillPage(inForeground = true))
        assertTrue("nothing is pushed", received.isEmpty())
        assertEquals("nothing is read", reads, gw.reads)
        assertEquals(1, outbox.size())
        assertEquals("2026-11-15", state.load().policyVersionBox)
        assertTrue(WellnessRules.policyHold(state.load()))

        // "I agree" on the card records the version of the text it showed, and the collector runs again.
        state.update { WellnessRules.agreePolicy(it, "2026-11-15") }
        now += 61_000
        assertEquals("ok", e.sync(false).outcome)
        assertTrue(received.isNotEmpty())
        assertEquals(0, outbox.size())
    }

    @Test
    fun `an ingest answer naming a new policy stops the push there, and the rest waits on the phone`() = runBlocking {
        enroll()
        engine.sync(false)
        reply = { WellnessOutbox.SendResult(-1) }
        for (i in 1..3) {
            gw.upsert(WellnessFixtures.bp("bp-$i", 3, 6 + i))
            engine.sync(false)
        }
        assertEquals(3, outbox.size())
        // The version every enrollment before acked versions agreed to: no hold, all three go.
        reply = { WellnessOutbox.SendResult(200, "{}", policyVersion = WellnessRules.LEGACY_POLICY_VERSION) }
        received.clear()
        assertEquals("ok", engine.sync(false).outcome)
        assertEquals(3, received.size)

        reply = { WellnessOutbox.SendResult(-1) }
        for (i in 4..6) {
            gw.upsert(WellnessFixtures.bp("bp-$i", 3, 6 + i))
            engine.sync(false)
        }
        assertEquals(3, outbox.size())
        reply = { WellnessOutbox.SendResult(200, "{}", policyVersion = "2026-11-15") }
        received.clear()
        val report = engine.sync(false)
        assertEquals(WellnessSyncEngine.POLICY_HELD, report.outcome)
        assertEquals("the answer that named it was the last push", 1, received.size)
        assertEquals(1, report.batchesSent)
        assertEquals(2, outbox.size())
        assertTrue(WellnessRules.policyHold(state.load()))
        gw.upsert(WellnessFixtures.bp("bp-7", 3, 14))
        assertEquals(WellnessSyncEngine.POLICY_HELD, engine.sync(false).outcome)
        assertEquals(1, received.size)
        assertEquals(2, outbox.size())
    }

    // ── review fix: bucket groups say which local day they replace ────────────────────────

    @Test
    fun `every bucket group carries the zone offset of its local day, DST days included`() = runBlocking {
        now = at(2, 12)
        enroll()
        engine.sync(false)
        // 1 Nov 2026 is the day New York leaves daylight time: it starts at -4 h.
        gw.addHeartRate(WellnessFixtures.meta("hr-dst"), -14400,
            WellnessFixtures.hrSeries(at(1, 0, 30), at(1, 0, 50), 60, 50.0))
        gw.upsert(WellnessFixtures.steps("st-2", at(2, 9), at(2, 10)), 800.0)
        engine.sync(false)
        val offsets = all("buckets").associate {
            (it.getValue("metric").jsonPrimitive.content + "@" + it.getValue("replace_day").jsonPrimitive.content + "/" +
                it.getValue("grain").jsonPrimitive.content) to it.getValue("offset_s").jsonPrimitive.content.toInt()
        }
        assertEquals(-14400, offsets.getValue("heart_rate@2026-11-01/5min"))
        assertEquals(-14400, offsets.getValue("heart_rate@2026-11-01/1min"))
        assertEquals(-18000, offsets.getValue("steps@2026-11-02/1h"))
    }

    @Test
    fun `a single 401 from a drifted clock is retried and never asks the person to reconnect`() = runBlocking {
        var refusals = 1
        val e = newEngine {
            if (refusals-- > 0) WellnessOutbox.SendResult(401, """{"error":"bad_signature"}""")
            else WellnessOutbox.SendResult(200, statusBody(allAsk))
        }
        enroll()
        e.sync(false)
        assertFalse(state.load().needsReconnect)
        assertEquals(2, statusCalls)
        gw.upsert(WellnessFixtures.bp("bp-1", 3, 7))
        var pushRefusals = 1
        reply = { if (pushRefusals-- > 0) WellnessOutbox.SendResult(401, """{"error":"bad_signature"}""") else WellnessOutbox.SendResult(200, "{}") }
        assertEquals("ok", e.sync(false).outcome)
        assertFalse(state.load().needsReconnect)
        assertEquals(0, reconnectNotices)
        assertEquals(0, outbox.size())
    }
}
