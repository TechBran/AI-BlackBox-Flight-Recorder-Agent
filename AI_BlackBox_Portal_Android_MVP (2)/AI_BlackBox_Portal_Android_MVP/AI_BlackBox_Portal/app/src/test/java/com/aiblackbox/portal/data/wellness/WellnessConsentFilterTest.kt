package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessFixtures.at
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
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
 * Every push is filtered by the consent of THAT moment ([IngestPayloadBuilder.withoutCategories]):
 * a batch read while a category was on never delivers it after the person turned it off,
 * here or on any other surface. Deletions always go; unknown items never do.
 */
class WellnessConsentFilterTest {

    private fun hrRow(t: Long): List<Number> = listOf(t, 52.0, 50.0, 55.0, 5L)

    /** One batch with something of every category, plus deletions. */
    private val mixed: String = IngestPayloadBuilder.toJson(
        IngestPayloadBuilder.Batch(
            batchId = "00000000000000000000000000000009",
            enrollmentId = "en_0badc0de",
            epoch = 1,
            window = IngestPayloadBuilder.Window("changes", at(3, 0), at(3, 19)),
            buckets = listOf(
                IngestPayloadBuilder.Bucket("heart_rate", IngestPayloadBuilder.Grain.MIN5, WellnessFixtures.WATCH,
                    "2026-11-03", -18000, listOf(hrRow(at(3, 2)))),
                IngestPayloadBuilder.Bucket("steps", IngestPayloadBuilder.Grain.HOUR, null, "2026-11-03", -18000,
                    listOf(listOf<Number>(at(3, 9), 1600.0))),
            ),
            sessions = listOf(
                BucketBuilder.sleepSession(WellnessFixtures.sleepNight("sleep-1", 3)),
                BucketBuilder.exerciseSession(WellnessFixtures.run("run-1", 3),
                    mapOf(AggKey.DISTANCE_M to 5012.0, AggKey.BPM_AVG to 148.0, AggKey.BPM_MAX to 171.0)),
            ),
            readings = listOf(
                BucketBuilder.reading(WellnessFixtures.spo2("spo2-1", 3, 3, 96.0)),
                BucketBuilder.reading(WellnessFixtures.bp("bp-1", 3, 7)),
            ),
            deletedIds = listOf("gone-1"),
        ),
    )

    private fun parse(json: String): JsonObject = Json.parseToJsonElement(json).jsonObject

    private fun ids(obj: JsonObject, section: String, key: String) =
        obj.getValue(section).jsonArray.map { it.jsonObject.getValue(key).jsonPrimitive.content }

    @Test
    fun `nothing to take out returns null so the stored bytes go as they are`() {
        assertNull(IngestPayloadBuilder.withoutCategories(mixed, emptySet()))
    }

    @Test
    fun `heart off takes heart buckets and readings out and the heart summary off exercise`() {
        val out = IngestPayloadBuilder.withoutCategories(mixed, setOf(WellnessCategory.HEART))
        assertNotNull(out)
        assertFalse(out!!.isEmpty)
        val obj = parse(out.json)
        assertEquals(listOf("steps"), ids(obj, "buckets", "metric"))
        assertEquals(listOf("blood_pressure"), ids(obj, "readings", "metric"))
        assertEquals(listOf("sleep-1", "run-1"), ids(obj, "sessions", "hc_id"))
        val attrs = obj.getValue("sessions").jsonArray[1].jsonObject.getValue("attrs").jsonObject
        assertEquals(JsonNull, attrs.getValue("avg_hr"))
        assertEquals(JsonNull, attrs.getValue("max_hr"))
        assertEquals("5012", attrs.getValue("distance_m").jsonPrimitive.content)
        assertEquals(listOf("gone-1"), obj.getValue("deleted_ids").jsonArray.map { it.jsonPrimitive.content })
        // Same batch id (the box deduplicates by it); same field order as the builder.
        assertEquals("00000000000000000000000000000009", obj.getValue("batch_id").jsonPrimitive.content)
        assertEquals(parse(mixed).keys.toList(), obj.keys.toList())
    }

    @Test
    fun `origins no remaining item uses are dropped`() {
        val obj = parse(IngestPayloadBuilder.withoutCategories(mixed,
            setOf(WellnessCategory.HEART, WellnessCategory.SLEEP, WellnessCategory.ACTIVITY))!!.json)
        val used = ids(obj, "readings", "origin").toSet()
        assertEquals(used, ids(obj, "origins", "key").toSet())
        assertEquals(listOf("com.example.cuffapp"), ids(obj, "origins", "package"))
    }

    @Test
    fun `sleep, activity and blood pressure each take only their own items`() {
        fun left(off: WellnessCategory) = parse(IngestPayloadBuilder.withoutCategories(mixed, setOf(off))!!.json)
        assertEquals(listOf("run-1"), ids(left(WellnessCategory.SLEEP), "sessions", "hc_id"))
        left(WellnessCategory.ACTIVITY).let {
            assertEquals(listOf("heart_rate"), ids(it, "buckets", "metric"))
            assertEquals(listOf("sleep-1"), ids(it, "sessions", "hc_id"))
        }
        assertEquals(listOf("spo2"), ids(left(WellnessCategory.BLOOD_PRESSURE), "readings", "metric"))
    }

    @Test
    fun `everything off leaves only the deletions, and without them the batch is empty`() {
        val all = WellnessCategory.entries.toSet()
        val withDeletions = IngestPayloadBuilder.withoutCategories(mixed, all)!!
        assertFalse(withDeletions.isEmpty)
        assertEquals(listOf("gone-1"), parse(withDeletions.json).getValue("deleted_ids").jsonArray.map { it.jsonPrimitive.content })

        val bpOnly = IngestPayloadBuilder.toJson(IngestPayloadBuilder.Batch("b".repeat(32), "en_0badc0de", 1,
            IngestPayloadBuilder.Window("changes", at(3, 0), at(3, 1)),
            readings = listOf(BucketBuilder.reading(WellnessFixtures.bp("bp-1", 3, 7)))))
        assertTrue(IngestPayloadBuilder.withoutCategories(bpOnly, setOf(WellnessCategory.BLOOD_PRESSURE))!!.isEmpty)
    }

    @Test
    fun `an item of a kind this build does not know is never sent (default deny)`() {
        val unknown = mixed.replaceFirst("\"metric\":\"steps\"", "\"metric\":\"skin_temperature\"")
        val obj = parse(IngestPayloadBuilder.withoutCategories(unknown, emptySet())!!.json)
        assertEquals(listOf("heart_rate"), ids(obj, "buckets", "metric"))
    }

    @Test
    fun `every catalog bucket and reading metric maps to its category`() {
        for (type in HcType.entries) {
            val item = when (type.shape) {
                HcShape.BUCKETED -> "buckets" to """{"metric":"${IngestPayloadBuilder.bucketMetric(type)}"}"""
                HcShape.READING -> "readings" to """{"metric":"${IngestPayloadBuilder.readingMetric(type)}"}"""
                HcShape.SESSION -> "sessions" to """{"kind":"${type.wire}"}"""
            }
            assertEquals(type.wire, WellnessCatalog.categoryOf(type),
                IngestPayloadBuilder.categoryOf(item.first, parse(item.second)))
        }
    }
}
