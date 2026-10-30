package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessFixtures.ZONE
import com.aiblackbox.portal.data.wellness.WellnessFixtures.at
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BucketBuilderTest {

    @Test
    fun `local days follow the zone, including the 25-hour DST day`() {
        assertEquals("2026-11-03", BucketBuilder.localDay(at(3, 0, 0), ZONE))
        assertEquals("2026-11-02", BucketBuilder.localDay(at(3, 0, 0) - 1, ZONE))
        val (s, e) = BucketBuilder.dayRange("2026-11-01", ZONE) // US DST ends: 25 hours
        assertEquals(25 * BucketBuilder.HOUR_MS, e - s)
        assertEquals(24 * BucketBuilder.HOUR_MS, BucketBuilder.dayRange("2026-11-03", ZONE).let { it.second - it.first })
    }

    @Test
    fun `a bucket group's offset is the zone offset at the start of its local day`() {
        // Daylight time ends on 1 Nov 2026 at 02:00 and starts on 8 Mar 2026 at 02:00 (New York).
        assertEquals(-14400, BucketBuilder.dayOffsetS("2026-11-01", ZONE))
        assertEquals(-18000, BucketBuilder.dayOffsetS("2026-11-02", ZONE))
        assertEquals(-18000, BucketBuilder.dayOffsetS("2026-03-08", ZONE))
        assertEquals(-14400, BucketBuilder.dayOffsetS("2026-03-09", ZONE))
        assertEquals(0, BucketBuilder.dayOffsetS("2026-11-01", java.time.ZoneOffset.UTC))
        assertTrue(BucketBuilder.heartRateBuckets("2026-11-01", WellnessFixtures.WATCH, emptyList(), emptyList(), ZONE)
            .all { it.offsetS == -14400 })
        assertTrue(BucketBuilder.activityBuckets("2026-11-02", emptyList(), listOf(HcType.STEPS), ZONE)
            .all { it.offsetS == -18000 })
    }

    @Test
    fun `days touched - an overnight session touches two days, a point its own`() {
        assertEquals(listOf("2026-11-02", "2026-11-03"), BucketBuilder.daysTouched(at(2, 23), at(3, 6), ZONE))
        assertEquals(listOf("2026-11-03"), BucketBuilder.daysTouched(at(3, 8), at(3, 8), ZONE))
        // An end exactly at midnight does not touch the next day.
        assertEquals(listOf("2026-11-02"), BucketBuilder.daysTouched(at(2, 22), at(3, 0), ZONE))
        assertEquals(listOf("2026-11-01", "2026-11-02", "2026-11-03"),
            BucketBuilder.trailingDays(at(3, 12), 3, ZONE))
    }

    @Test
    fun `5-minute heart-rate buckets carry avg min max n and skip empty slices`() = runBlocking {
        val gw = FakeHealthConnectGateway()
        val start = at(3, 10, 0)
        gw.addHeartRate(WellnessFixtures.meta("hr-1"), WellnessFixtures.OFFSET_EST,
            WellnessFixtures.hrSeries(start, start + 10 * BucketBuilder.MINUTE_MS, 60, 60.0))
        val (ds, de) = BucketBuilder.dayRange("2026-11-03", ZONE)
        val buckets = gw.aggregateByDuration(BucketBuilder.HR_KEYS, ds, de, BucketBuilder.FIVE_MIN_MS,
            WellnessCatalog.SAMSUNG_HEALTH_PACKAGE)
        val rows = BucketBuilder.hrRows(buckets)
        assertEquals(2, rows.size)
        assertEquals(listOf<Number>(start, 59.0, 57.0, 61.0, 5L), rows[0])
        assertEquals(start + BucketBuilder.FIVE_MIN_MS, rows[1][0])
        // A slice with values but n = 0 is a gap, not a zero row.
        assertTrue(BucketBuilder.hrRows(listOf(HcAggBucket(0, 1, 0, mapOf(AggKey.BPM_COUNT to 0.0, AggKey.BPM_AVG to 0.0,
            AggKey.BPM_MIN to 0.0, AggKey.BPM_MAX to 0.0)))).isEmpty())
    }

    @Test
    fun `1-minute rows only inside exercise windows clipped to the day`() = runBlocking {
        val gw = FakeHealthConnectGateway()
        val run = WellnessFixtures.run("run-1", 3)
        gw.upsert(run)
        gw.addHeartRate(WellnessFixtures.meta("hr-run"), WellnessFixtures.OFFSET_EST,
            WellnessFixtures.hrSeries(at(3, 17, 50), at(3, 18, 40), 10, 140.0))
        val (ds, de) = BucketBuilder.dayRange("2026-11-03", ZONE)
        val windows = BucketBuilder.exerciseWindows(listOf(run), ds, de)
        assertEquals(listOf(run.startMs to run.endMs), windows)
        val one = windows.flatMap { (a, b) ->
            gw.aggregateByDuration(BucketBuilder.HR_KEYS, a, b, BucketBuilder.MINUTE_MS, WellnessCatalog.SAMSUNG_HEALTH_PACKAGE)
        }
        val rows = BucketBuilder.hrRows(one)
        assertEquals(30, rows.size)                       // 18:00-18:30, one row per minute
        assertTrue(rows.all { (it[0] as Long) in run.startMs until run.endMs })
        assertTrue(rows.all { it[4] == 6L })              // 10-second samples: six per minute
        // A session crossing midnight is clipped to each day.
        val late = HcRecord.Exercise(WellnessFixtures.meta("late"), at(3, 23, 40), at(4, 0, 20), WellnessFixtures.OFFSET_EST, 56)
        assertEquals(listOf(at(3, 23, 40) to at(4, 0, 0)), BucketBuilder.exerciseWindows(listOf(late), ds, de))
    }

    @Test
    fun `heart rate always yields both grains for a processed day so an emptied day replaces to nothing`() {
        val buckets = BucketBuilder.heartRateBuckets("2026-11-03", WellnessFixtures.WATCH, emptyList(), emptyList(), ZONE)
        assertEquals(listOf("5min", "1min"), buckets.map { it.grain.wire })
        assertTrue(buckets.all { it.replaceDay == "2026-11-03" && it.rows.isEmpty() && it.metric == "heart_rate" })
    }

    @Test
    fun `hourly activity uses the merged origin and replace_day, one object per readable type`() = runBlocking {
        val gw = FakeHealthConnectGateway()
        gw.upsert(WellnessFixtures.steps("st-1", at(3, 9, 0), at(3, 9, 30)), 1200.0)
        gw.upsert(WellnessFixtures.steps("st-2", at(3, 9, 30), at(3, 10, 30)), 800.0)
        val (ds, de) = BucketBuilder.dayRange("2026-11-03", ZONE)
        val hourly = gw.aggregateByDuration(setOf(AggKey.STEPS), ds, de, BucketBuilder.HOUR_MS, null)
        val buckets = BucketBuilder.activityBuckets("2026-11-03", hourly, listOf(HcType.STEPS, HcType.EXERCISE), ZONE)
        assertEquals(1, buckets.size)
        val steps = buckets.single()
        assertEquals("steps", steps.metric)
        assertEquals("1h", steps.grain.wire)
        assertNull(steps.origin)
        assertEquals("2026-11-03", steps.replaceDay)
        assertEquals(listOf(listOf<Number>(at(3, 9), 1600.0), listOf<Number>(at(3, 10), 400.0)), steps.rows)
    }

    @Test
    fun `sessions keep their stages sorted and exercise carries its aggregate with nulls for gaps`() {
        val night = WellnessFixtures.sleepNight("sleep-1", 3)
        val shuffled = night.copy(stages = night.stages.reversed())
        val s = BucketBuilder.sleepSession(shuffled)
        assertEquals("sleep", s.kind)
        assertEquals(night.stages, s.stages)
        assertNull(s.subtype)
        val ex = BucketBuilder.exerciseSession(WellnessFixtures.run("run-1", 3),
            mapOf(AggKey.DISTANCE_M to 4012.5, AggKey.BPM_AVG to 141.0, AggKey.BPM_MAX to 162.0))
        assertEquals(56, ex.subtype)
        assertEquals(listOf("distance_m", "active_kcal", "total_kcal", "avg_hr", "max_hr"), ex.attrs!!.keys.toList())
        assertNull(ex.attrs!!["total_kcal"])
        assertEquals(setOf(AggKey.DISTANCE_M, AggKey.BPM_AVG, AggKey.BPM_MAX),
            BucketBuilder.exerciseAggKeys(listOf(HcType.DISTANCE, HcType.HEART_RATE)))
    }

    @Test
    fun `blood pressure readings carry diastolic and the cuff attributes, other readings do not`() {
        val bp = BucketBuilder.reading(WellnessFixtures.bp("bp-1", 2, 7))
        assertEquals("blood_pressure", bp.metric)
        assertEquals(118.0, bp.value, 0.0)
        assertEquals(79.0, bp.value2!!, 0.0)
        assertEquals("left_upper_arm", bp.attrs!!["measurement_location"])
        assertEquals("manual", bp.attrs!!["recording_method"])
        val spo2 = BucketBuilder.reading(WellnessFixtures.spo2("o2-1", 3, 3, 96.0))
        assertEquals("spo2", spo2.metric)
        assertNull(spo2.value2)
        assertNull(spo2.attrs)
    }
}
