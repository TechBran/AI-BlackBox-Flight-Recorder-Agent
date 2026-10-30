package com.aiblackbox.portal.data.wellness

/**
 * An in-memory Health Connect for JVM tests. Records are added per type; aggregates are
 * computed from what was added (heart rate from per-sample bpm, activity from interval
 * values spread evenly over each interval); the Changes API replays what [upsert] and
 * [delete] logged after each token was taken. Nothing here touches Android.
 */
class FakeHealthConnectGateway(
    var sdk: HcSdkStatus = HcSdkStatus.AVAILABLE,
    val features: MutableMap<HcFeature, HcFeatureStatus> = mutableMapOf(
        HcFeature.BACKGROUND_READ to HcFeatureStatus.AVAILABLE,
        HcFeature.HISTORY_READ to HcFeatureStatus.AVAILABLE,
        HcFeature.SKIN_TEMPERATURE to HcFeatureStatus.UNAVAILABLE,
    ),
    var granted: Set<String> = WellnessCatalog.ALL_READ_PERMISSIONS + WellnessCatalog.PERMISSION_BACKGROUND +
        WellnessCatalog.PERMISSION_HISTORY,
) : HealthConnectGateway {

    /** Heart-rate samples: time -> bpm, per origin package. */
    data class HrSample(val timeMs: Long, val bpm: Double, val origin: HcOrigin)

    /** An activity interval with its value (steps, metres, kcal). */
    data class ActivityInterval(val record: HcRecord.Interval, val value: Double)

    val records = linkedMapOf<String, HcRecord>()
    val hrSamples = mutableListOf<HrSample>()
    val activity = linkedMapOf<String, ActivityInterval>()

    private val log = mutableListOf<Pair<HcType, HcChange>>()
    private val tokens = mutableMapOf<String, Pair<HcType, Int>>()
    private var tokenSeq = 0
    var expireTokens = false
    var quotaAfterCalls: Int? = null
    var calls = 0
    var pageSize: Int? = null

    // ── test setup ─────────────────────────────────────────────────────────────────────────

    fun upsert(record: HcRecord, value: Double? = null) {
        records[record.meta.id] = record
        if (record is HcRecord.Interval && value != null) activity[record.meta.id] = ActivityInterval(record, value)
        log += record.type to HcChange.Upsert(record)
    }

    fun addHeartRate(meta: HcMeta, offsetS: Int, samples: List<Pair<Long, Double>>) {
        val times = samples.map { it.first }
        upsert(HcRecord.HeartRate(meta, times.min(), times.max(), offsetS, times))
        samples.forEach { hrSamples += HrSample(it.first, it.second, meta.origin) }
    }

    fun delete(type: HcType, id: String) {
        // Heart-rate samples drop out of aggregates once their record is gone (see aggregateRange).
        records.remove(id)
        activity.remove(id)
        log += type to HcChange.Deletion(id)
    }

    // ── gateway ────────────────────────────────────────────────────────────────────────────

    private fun tick() {
        calls++
        quotaAfterCalls?.let { if (calls > it) throw HcQuotaException("quota") }
    }

    override fun sdkStatus(): HcSdkStatus = sdk

    override suspend fun featureStatus(feature: HcFeature): HcFeatureStatus = features[feature] ?: HcFeatureStatus.UNAVAILABLE

    override suspend fun grantedPermissions(): Set<String> = granted

    override suspend fun aggregateByDuration(
        keys: Set<AggKey>,
        startMs: Long,
        endMs: Long,
        sliceMs: Long,
        originPackage: String?,
    ): List<HcAggBucket> {
        tick()
        val out = mutableListOf<HcAggBucket>()
        var t = startMs
        while (t < endMs) {
            val e = minOf(t + sliceMs, endMs)
            val values = aggregateRange(keys, t, e, originPackage)
            if (values.values.any { it != null }) out += HcAggBucket(t, e, 0, values)
            t = e
        }
        return out
    }

    override suspend fun aggregate(keys: Set<AggKey>, startMs: Long, endMs: Long, originPackage: String?): Map<AggKey, Double?> {
        tick()
        return aggregateRange(keys, startMs, endMs, originPackage)
    }

    private fun aggregateRange(keys: Set<AggKey>, startMs: Long, endMs: Long, originPackage: String?): Map<AggKey, Double?> {
        val hr = hrSamples.filter { it.timeMs in startMs until endMs && (originPackage == null || it.origin.packageName == originPackage) }
            .filter { s -> records.values.any { r -> r is HcRecord.HeartRate && s.timeMs in r.sampleTimesMs && r.meta.origin == s.origin } }
        fun activitySum(type: HcType): Double? {
            val parts = activity.values.filter { it.record.type == type }.mapNotNull { a ->
                val r = a.record
                val overlap = minOf(r.endMs, endMs) - maxOf(r.startMs, startMs)
                if (overlap <= 0) null else a.value * overlap / (r.endMs - r.startMs).coerceAtLeast(1)
            }
            return if (parts.isEmpty()) null else parts.sum()
        }
        return keys.associateWith { key ->
            when (key) {
                AggKey.BPM_AVG -> if (hr.isEmpty()) null else Math.round(hr.map { it.bpm }.average()).toDouble()
                AggKey.BPM_MIN -> hr.minOfOrNull { it.bpm }
                AggKey.BPM_MAX -> hr.maxOfOrNull { it.bpm }
                AggKey.BPM_COUNT -> if (hr.isEmpty()) null else hr.size.toDouble()
                AggKey.STEPS -> activitySum(HcType.STEPS)?.let { Math.round(it).toDouble() }
                AggKey.DISTANCE_M -> activitySum(HcType.DISTANCE)
                AggKey.ACTIVE_KCAL -> activitySum(HcType.ACTIVE_CALORIES)
                AggKey.TOTAL_KCAL -> activitySum(HcType.TOTAL_CALORIES)
            }
        }
    }

    override suspend fun readRecords(
        type: HcType,
        startMs: Long,
        endMs: Long,
        pageToken: String?,
        pageSize: Int,
        ascending: Boolean,
    ): HcPage {
        tick()
        if (type.readPermission !in granted) throw HcPermissionException(type, "not granted")
        val all = records.values.filter { it.type == type && it.startMs >= startMs && it.startMs < endMs }
            .let { list -> if (ascending) list.sortedBy { it.startMs } else list.sortedByDescending { it.startMs } }
        val size = this.pageSize ?: pageSize
        val from = pageToken?.toInt() ?: 0
        val page = all.drop(from).take(size)
        val next = if (from + size < all.size) (from + size).toString() else null
        return HcPage(page, next)
    }

    override suspend fun changesToken(type: HcType): String {
        tick()
        val token = "tok-${++tokenSeq}"
        tokens[token] = type to log.size
        return token
    }

    override suspend fun getChanges(token: String): HcChanges {
        tick()
        val (type, at) = tokens[token] ?: return HcChanges(emptyList(), token, false, tokenExpired = true)
        if (expireTokens) return HcChanges(emptyList(), token, false, tokenExpired = true)
        val changes = log.drop(at).filter { it.first == type }.map { it.second }
        val next = "tok-${++tokenSeq}"
        tokens[next] = type to log.size
        return HcChanges(changes, next, hasMore = false, tokenExpired = false)
    }
}
