package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessFixtures.ZONE
import com.aiblackbox.portal.data.wellness.WellnessFixtures.at
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * FROZEN CONTRACTS: `bbx.wellness.ingest/1` payloads emitted from synthetic Health Connect
 * records must equal the goldens under `src/test/resources/wellness/` byte for byte. The box
 * replays these same files through its real ingest route (the Kotlin-to-Python golden
 * contract test), so a change here is a wire change and needs the box side to move with it.
 *
 * On a mismatch the actual output is written to `build/wellness-goldens/` for review; a golden
 * is only ever replaced deliberately, by copying that file over it.
 */
class IngestPayloadBuilderGoldenTest {

    private val enr = "en_0badc0de"

    private fun golden(name: String): String {
        val url = javaClass.classLoader!!.getResource("wellness/$name")
            ?: throw AssertionError("missing golden wellness/$name")
        return url.readText()
    }

    private fun assertGolden(name: String, actual: String) {
        val expected = runCatching { golden(name) }.getOrNull()
        if (expected != actual) {
            val out = File("build/wellness-goldens").apply { mkdirs() }
            File(out, name).writeText(actual)
            throw AssertionError("golden $name differs; actual written to ${File(out, name).absolutePath}")
        }
    }

    /**
     * A golden is the EXACT wire bytes plus one trailing newline: what the phone gzips and signs
     * is what the box replays (`jq . <file>` to read one).
     */
    private fun goldenForm(json: String): String = json + "\n"

    private fun sleepHeartBatch(): IngestPayloadBuilder.Batch = runBlocking {
        val gw = FakeHealthConnectGateway()
        val night = WellnessFixtures.sleepNight("sleep-20261103", 3)
        gw.upsert(night)
        gw.addHeartRate(WellnessFixtures.meta("hr-night"), WellnessFixtures.OFFSET_EST,
            WellnessFixtures.hrSeries(at(3, 2, 0), at(3, 2, 20), 60, 52.0))
        val run = WellnessFixtures.run("run-20261103", 3)
        gw.upsert(run)
        gw.addHeartRate(WellnessFixtures.meta("hr-run", WellnessFixtures.WATCH_ACTIVE), WellnessFixtures.OFFSET_EST,
            WellnessFixtures.hrSeries(at(3, 18, 0), at(3, 18, 3), 20, 138.0))
        val (ds, de) = BucketBuilder.dayRange("2026-11-03", ZONE)
        val pkg = WellnessCatalog.SAMSUNG_HEALTH_PACKAGE
        val five = gw.aggregateByDuration(BucketBuilder.HR_KEYS, ds, de, BucketBuilder.FIVE_MIN_MS, pkg)
        val one = BucketBuilder.exerciseWindows(listOf(run), ds, de).flatMap { (a, b) ->
            gw.aggregateByDuration(BucketBuilder.HR_KEYS, a, b, BucketBuilder.MINUTE_MS, pkg)
        }
        IngestPayloadBuilder.Batch(
            batchId = "00000000000000000000000000000001",
            enrollmentId = enr,
            epoch = 1,
            window = IngestPayloadBuilder.Window("changes", at(3, 0), at(3, 19)),
            buckets = BucketBuilder.heartRateBuckets("2026-11-03", WellnessFixtures.WATCH, five, one, ZONE),
            sessions = listOf(BucketBuilder.sleepSession(night)),
            readings = listOf(
                BucketBuilder.reading(WellnessFixtures.spo2("spo2-1", 3, 3, 96.0)),
                BucketBuilder.reading(HcRecord.Reading(HcType.RESTING_HEART_RATE, WellnessFixtures.meta("rhr-1"),
                    at(3, 7), WellnessFixtures.OFFSET_EST, 54.0)),
                BucketBuilder.reading(HcRecord.Reading(HcType.HRV, WellnessFixtures.meta("hrv-1"),
                    at(3, 6, 30), WellnessFixtures.OFFSET_EST, 41.25)),
            ),
        )
    }

    private fun activityBatch(): IngestPayloadBuilder.Batch = runBlocking {
        val gw = FakeHealthConnectGateway()
        gw.upsert(WellnessFixtures.steps("steps-1", at(3, 8, 0), at(3, 8, 45)), 3000.0)
        gw.upsert(WellnessFixtures.steps("steps-2", at(3, 18, 0), at(3, 18, 30)), 4100.0, )
        gw.upsert(HcRecord.Interval(HcType.DISTANCE, WellnessFixtures.meta("dist-1"), at(3, 18, 0), at(3, 18, 30),
            WellnessFixtures.OFFSET_EST), 4012.5)
        gw.upsert(HcRecord.Interval(HcType.ACTIVE_CALORIES, WellnessFixtures.meta("kcal-1"), at(3, 18, 0), at(3, 18, 30),
            WellnessFixtures.OFFSET_EST), 310.4)
        val run = WellnessFixtures.run("run-20261103", 3)
        gw.upsert(run)
        val types = listOf(HcType.STEPS, HcType.DISTANCE, HcType.ACTIVE_CALORIES, HcType.TOTAL_CALORIES, HcType.EXERCISE)
        val (ds, de) = BucketBuilder.dayRange("2026-11-03", ZONE)
        val keys = types.mapNotNull { BucketBuilder.ACTIVITY_KEYS[it] }.toSet()
        val hourly = gw.aggregateByDuration(keys, ds, de, BucketBuilder.HOUR_MS, null)
        val agg = gw.aggregate(BucketBuilder.exerciseAggKeys(types), run.startMs, run.endMs, null)
        IngestPayloadBuilder.Batch(
            batchId = "00000000000000000000000000000002",
            enrollmentId = enr,
            epoch = 1,
            window = IngestPayloadBuilder.Window("backfill", ds, de),
            buckets = BucketBuilder.activityBuckets("2026-11-03", hourly, types, ZONE),
            sessions = listOf(BucketBuilder.exerciseSession(run, agg)),
        )
    }

    private fun bloodPressureBatch() = IngestPayloadBuilder.Batch(
        batchId = "00000000000000000000000000000003",
        enrollmentId = enr,
        epoch = 2,
        window = IngestPayloadBuilder.Window("changes", at(2, 0), at(2, 23)),
        readings = listOf(
            BucketBuilder.reading(WellnessFixtures.bp("bp-am", 2, 7)),
            BucketBuilder.reading(WellnessFixtures.bp("bp-pm", 2, 21, sys = 121.0, dia = 80.5)),
        ),
    )

    private fun deletionsBatch() = IngestPayloadBuilder.Batch(
        batchId = "00000000000000000000000000000004",
        enrollmentId = enr,
        epoch = 2,
        window = IngestPayloadBuilder.Window("changes", at(3, 12), at(3, 13)),
        buckets = BucketBuilder.heartRateBuckets("2026-11-03", WellnessFixtures.WATCH, emptyList(), emptyList(), ZONE),
        deletedIds = listOf("sleep-20261103", "never-indexed-id"),
    )

    @Test fun `sleep and heart golden`() = assertGolden("ingest_sleep_heart.json", goldenForm(IngestPayloadBuilder.toJson(sleepHeartBatch())))

    @Test fun `activity golden`() = assertGolden("ingest_activity.json", goldenForm(IngestPayloadBuilder.toJson(activityBatch())))

    @Test fun `blood pressure golden`() = assertGolden("ingest_blood_pressure.json", goldenForm(IngestPayloadBuilder.toJson(bloodPressureBatch())))

    @Test fun `deletions golden`() = assertGolden("ingest_deletions.json", goldenForm(IngestPayloadBuilder.toJson(deletionsBatch())))

    @Test
    fun `the same records always give the same bytes`() {
        assertEquals(IngestPayloadBuilder.toJson(sleepHeartBatch()), IngestPayloadBuilder.toJson(sleepHeartBatch()))
        assertEquals(IngestPayloadBuilder.toJson(activityBatch()), IngestPayloadBuilder.toJson(activityBatch()))
    }

    @Test
    fun `the wire shape - field order, origins, keys and types`() {
        val obj = Json.parseToJsonElement(IngestPayloadBuilder.toJson(sleepHeartBatch())).jsonObject
        assertEquals(
            listOf("schema", "batch_id", "enrollment_id", "epoch", "window", "origins", "buckets", "sessions", "readings", "deleted_ids"),
            obj.keys.toList(),
        )
        assertEquals("bbx.wellness.ingest/1", obj.getValue("schema").jsonPrimitive.content)
        assertEquals(listOf("type", "from", "to"), obj.getValue("window").jsonObject.keys.toList())
        assertEquals("2026-11-03T05:00:00Z", obj.getValue("window").jsonObject.getValue("from").jsonPrimitive.content)
        val origins = obj.getValue("origins").jsonArray.map { it.jsonObject }
        assertEquals(listOf("o1"), origins.map { it.getValue("key").jsonPrimitive.content })
        assertEquals(listOf("key", "package", "device_type", "model"), origins[0].keys.toList())
        val bucket = obj.getValue("buckets").jsonArray[0].jsonObject
        assertEquals(listOf("metric", "grain", "origin", "replace_day", "offset_s", "rows"), bucket.keys.toList())
        assertEquals("o1", bucket.getValue("origin").jsonPrimitive.content)
        // The zone offset at the start of the local replace_day (New York, standard time).
        assertEquals("-18000", bucket.getValue("offset_s").toString())
        for (row in bucket.getValue("rows").jsonArray) assertEquals(5, row.jsonArray.size)
        val session = obj.getValue("sessions").jsonArray[0].jsonObject
        assertEquals(listOf("kind", "hc_id", "origin", "start", "end", "offset_s", "stages", "last_modified"), session.keys.toList())
        val reading = obj.getValue("readings").jsonArray[0].jsonObject
        assertEquals(listOf("metric", "hc_id", "origin", "t", "offset_s", "v", "last_modified"), reading.keys.toList())
        // Integral values are integers on the wire.
        assertEquals("96", reading.getValue("v").toString())
    }

    @Test
    fun `merged activity has no origin entry and exercise carries subtype and attrs`() {
        val obj = Json.parseToJsonElement(IngestPayloadBuilder.toJson(activityBatch())).jsonObject
        val buckets = obj.getValue("buckets").jsonArray.map { it.jsonObject }
        assertTrue(buckets.all { it.getValue("origin").jsonPrimitive.content == IngestPayloadBuilder.ORIGIN_MERGED })
        assertEquals(listOf("steps", "distance", "active_calories", "total_calories"),
            buckets.map { it.getValue("metric").jsonPrimitive.content })
        // total calories had no records: its day replaces to empty, never to zeros.
        assertEquals(JsonArray(emptyList()), buckets[3].getValue("rows"))
        val session = obj.getValue("sessions").jsonArray[0].jsonObject
        assertEquals(listOf("kind", "hc_id", "origin", "start", "end", "offset_s", "subtype", "attrs", "last_modified"),
            session.keys.toList())
        assertEquals(JsonNull, session.getValue("attrs").jsonObject.getValue("total_kcal"))
        val origins = obj.getValue("origins").jsonArray.map { it.jsonObject.getValue("key").jsonPrimitive.content }
        assertEquals(listOf("o1"), origins) // only the exercise session's origin
    }

    @Test
    fun `numbers are canonical`() {
        assertEquals(JsonPrimitive(58L), IngestPayloadBuilder.num(58.0))
        assertEquals(JsonPrimitive(58.2), IngestPayloadBuilder.num(58.2))
        assertEquals(JsonPrimitive(0.333), IngestPayloadBuilder.num(1.0 / 3.0))
        assertEquals(JsonPrimitive(7L), IngestPayloadBuilder.num(7))
    }

    @Test
    fun `a 413 split halves the items, renames the batches and keeps only used origins`() {
        val json = IngestPayloadBuilder.toJson(sleepHeartBatch())
        val ids = WellnessFixtures.ids("s")
        val (a, b) = IngestPayloadBuilder.split(json, ids)!!
        val oa = Json.parseToJsonElement(a).jsonObject
        val ob = Json.parseToJsonElement(b).jsonObject
        assertNotEquals(oa.getValue("batch_id"), ob.getValue("batch_id"))
        val countA = listOf("buckets", "sessions", "readings", "deleted_ids").sumOf { oa.getValue(it).jsonArray.size }
        val countB = listOf("buckets", "sessions", "readings", "deleted_ids").sumOf { ob.getValue(it).jsonArray.size }
        assertEquals(6, countA + countB)
        assertEquals(3, countA)
        // Every origin a half references is listed in that half.
        for (half in listOf(oa, ob)) {
            val listed = half.getValue("origins").jsonArray.map { it.jsonObject.getValue("key").jsonPrimitive.content }.toSet()
            for (s in listOf("buckets", "sessions", "readings")) for (item in half.getValue(s).jsonArray) {
                val origin = item.jsonObject.getValue("origin").jsonPrimitive.content
                assertTrue(origin == IngestPayloadBuilder.ORIGIN_MERGED || origin in listed)
            }
        }
        assertNull(IngestPayloadBuilder.split(IngestPayloadBuilder.toJson(bloodPressureBatch().copy(readings =
            bloodPressureBatch().readings.take(1))), ids))
    }
}
