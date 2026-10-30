package com.aiblackbox.portal.data.wellness

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

/**
 * Turns Health Connect aggregates and records into the payload's pieces (spec §3.2, "What
 * the phone sends", decision D6, the minimum necessary grain):
 *
 *  - heart rate: 5-minute buckets `[t, avg, min, max, n]` from ONE chosen origin per local
 *    day, plus 1-minute buckets inside exercise sessions;
 *  - steps, distance, active and total calories: hourly `[t, value]` from Health Connect's
 *    priority-deduplicated merge;
 *  - sleep and exercise as whole sessions (sleep with stages, exercise with its per-session
 *    aggregate), point readings as readings.
 *
 * Every bucket object carries `replace_day`: the box swaps that metric's whole day for these
 * rows, so a re-aggregated day (after an edit or deletion) can never double-count, and a day
 * that lost all its data arrives as an empty `rows` list. A slice with no data is a missing
 * row, never a zero. PURE: time and zone are inputs.
 */
object BucketBuilder {

    const val MINUTE_MS = 60_000L
    const val FIVE_MIN_MS = 5 * MINUTE_MS
    const val HOUR_MS = 60 * MINUTE_MS
    const val DAY_MS = 24 * HOUR_MS

    val HR_KEYS: Set<AggKey> = linkedSetOf(AggKey.BPM_AVG, AggKey.BPM_MIN, AggKey.BPM_MAX, AggKey.BPM_COUNT)

    /** The hourly activity aggregate key for each bucketed activity type. */
    val ACTIVITY_KEYS: Map<HcType, AggKey> = linkedMapOf(
        HcType.STEPS to AggKey.STEPS,
        HcType.DISTANCE to AggKey.DISTANCE_M,
        HcType.ACTIVE_CALORIES to AggKey.ACTIVE_KCAL,
        HcType.TOTAL_CALORIES to AggKey.TOTAL_KCAL,
    )

    fun localDay(ms: Long, zone: ZoneId): String = Instant.ofEpochMilli(ms).atZone(zone).toLocalDate().toString()

    /** [start, end) of a local day in epoch ms (23 or 25 hours across a DST change). */
    fun dayRange(day: String, zone: ZoneId): Pair<Long, Long> {
        val date = LocalDate.parse(day)
        val start = date.atStartOfDay(zone).toInstant().toEpochMilli()
        val end = date.plusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()
        return start to end
    }

    /**
     * The zone offset (seconds east of UTC) at the start of local [day]: what a bucket group
     * sends as `offset_s`, so the box replaces exactly that local day (a DST day starts on the
     * offset before the change).
     */
    fun dayOffsetS(day: String, zone: ZoneId): Int =
        zone.rules.getOffset(Instant.ofEpochMilli(dayRange(day, zone).first)).totalSeconds

    /** Every local day [startMs, endMs) touches; a point (start == end) touches its own day. */
    fun daysTouched(startMs: Long, endMs: Long, zone: ZoneId): List<String> {
        val first = Instant.ofEpochMilli(startMs).atZone(zone).toLocalDate()
        val lastMs = if (endMs > startMs) endMs - 1 else startMs
        val last = Instant.ofEpochMilli(lastMs).atZone(zone).toLocalDate()
        val out = mutableListOf<String>()
        var d = first
        while (!d.isAfter(last)) {
            out += d.toString()
            d = d.plusDays(1)
        }
        return out
    }

    /** The trailing [days] local days ending with [nowMs]'s day, oldest first. */
    fun trailingDays(nowMs: Long, days: Int, zone: ZoneId): List<String> {
        val today = Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate()
        return (days - 1 downTo 0).map { today.minusDays(it.toLong()).toString() }
    }

    /** Heart-rate rows `[t, avg, min, max, n]`, skipping slices with no measurement. */
    fun hrRows(buckets: List<HcAggBucket>): List<List<Number>> = buckets
        .sortedBy { it.startMs }
        .mapNotNull { b ->
            val n = b.values[AggKey.BPM_COUNT]?.toLong() ?: 0L
            val avg = b.values[AggKey.BPM_AVG]
            val min = b.values[AggKey.BPM_MIN]
            val max = b.values[AggKey.BPM_MAX]
            if (n <= 0L || avg == null || min == null || max == null) null
            else listOf<Number>(b.startMs, avg, min, max, n)
        }

    /** `[t, value]` rows for one activity [key], skipping empty slices. */
    fun sumRows(buckets: List<HcAggBucket>, key: AggKey): List<List<Number>> = buckets
        .sortedBy { it.startMs }
        .mapNotNull { b -> b.values[key]?.let { listOf<Number>(b.startMs, it) } }

    /** The exercise sessions' windows clipped to [dayStart, dayEnd). */
    fun exerciseWindows(sessions: List<HcRecord.Exercise>, dayStart: Long, dayEnd: Long): List<Pair<Long, Long>> =
        sessions.mapNotNull { s ->
            val a = maxOf(s.startMs, dayStart)
            val b = minOf(s.endMs, dayEnd)
            if (b > a) a to b else null
        }.sortedBy { it.first }

    /**
     * The heart-rate bucket objects for one day: the 5-minute set and the 1-minute set (rows
     * from [oneMinute], which the caller aggregated over the day's exercise windows only).
     * Both always present for a processed day, so an emptied day replaces to nothing.
     */
    fun heartRateBuckets(
        day: String,
        origin: HcOrigin?,
        fiveMinute: List<HcAggBucket>,
        oneMinute: List<HcAggBucket>,
        zone: ZoneId,
    ): List<IngestPayloadBuilder.Bucket> {
        val offset = dayOffsetS(day, zone)
        return listOf(
            IngestPayloadBuilder.Bucket("heart_rate", IngestPayloadBuilder.Grain.MIN5, origin, day, offset, hrRows(fiveMinute)),
            IngestPayloadBuilder.Bucket("heart_rate", IngestPayloadBuilder.Grain.MIN1, origin, day, offset, hrRows(oneMinute)),
        )
    }

    /** Hourly activity bucket objects for one day, one per readable activity type. */
    fun activityBuckets(day: String, hourly: List<HcAggBucket>, types: List<HcType>, zone: ZoneId): List<IngestPayloadBuilder.Bucket> {
        val offset = dayOffsetS(day, zone)
        return ACTIVITY_KEYS.filterKeys { it in types }.map { (type, key) ->
            IngestPayloadBuilder.Bucket(
                IngestPayloadBuilder.bucketMetric(type), IngestPayloadBuilder.Grain.HOUR, null, day, offset,
                sumRows(hourly, key),
            )
        }
    }

    fun sleepSession(r: HcRecord.Sleep): IngestPayloadBuilder.Session = IngestPayloadBuilder.Session(
        kind = "sleep",
        hcId = r.meta.id,
        origin = r.meta.origin,
        startMs = r.startMs,
        endMs = r.endMs,
        offsetS = r.offsetS,
        lastModifiedMs = r.meta.lastModifiedMs,
        stages = r.stages.sortedBy { it.startMs },
    )

    /** An exercise session with its per-session aggregate ([agg] from one `aggregate` call). */
    fun exerciseSession(r: HcRecord.Exercise, agg: Map<AggKey, Double?>): IngestPayloadBuilder.Session =
        IngestPayloadBuilder.Session(
            kind = "exercise",
            hcId = r.meta.id,
            origin = r.meta.origin,
            startMs = r.startMs,
            endMs = r.endMs,
            offsetS = r.offsetS,
            lastModifiedMs = r.meta.lastModifiedMs,
            subtype = r.exerciseType,
            attrs = linkedMapOf(
                "distance_m" to agg[AggKey.DISTANCE_M],
                "active_kcal" to agg[AggKey.ACTIVE_KCAL],
                "total_kcal" to agg[AggKey.TOTAL_KCAL],
                "avg_hr" to agg[AggKey.BPM_AVG],
                "max_hr" to agg[AggKey.BPM_MAX],
            ),
        )

    /** The aggregate keys an exercise session is summarised with, given what is readable. */
    fun exerciseAggKeys(readable: Collection<HcType>): Set<AggKey> = buildSet {
        if (HcType.DISTANCE in readable) add(AggKey.DISTANCE_M)
        if (HcType.ACTIVE_CALORIES in readable) add(AggKey.ACTIVE_KCAL)
        if (HcType.TOTAL_CALORIES in readable) add(AggKey.TOTAL_KCAL)
        if (HcType.HEART_RATE in readable) { add(AggKey.BPM_AVG); add(AggKey.BPM_MAX) }
    }

    fun reading(r: HcRecord.Reading): IngestPayloadBuilder.Reading {
        val bp = r.type == HcType.BLOOD_PRESSURE
        return IngestPayloadBuilder.Reading(
            metric = IngestPayloadBuilder.readingMetric(r.type),
            hcId = r.meta.id,
            origin = r.meta.origin,
            timeMs = r.timeMs,
            offsetS = r.offsetS,
            value = r.value,
            lastModifiedMs = r.meta.lastModifiedMs,
            value2 = if (bp) r.value2 else null,
            attrs = if (bp) (r.attrs + ("recording_method" to r.meta.origin.recordingMethod)) else null,
        )
    }
}
