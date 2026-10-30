package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/**
 * What this phone can tell the box about its health data (spec §3.1, the capability report):
 * the phone, Health Connect's status and grants, Samsung Health, and every DATA ORIGIN seen in
 * the last 14 days, grouped by writing app x recording device x recording method, with
 * COUNTS ONLY: how many, the last sample time and the median gap. Never a value.
 *
 * App labels come from the package manager ([PackageInfoSource]), never from a hard-coded
 * list. The report is sent at enrollment, on app start, daily and whenever permissions change
 * (`POST /wellness/capabilities`, signed).
 */
class CapabilityProbe(
    private val gateway: HealthConnectGateway,
    private val packages: PackageInfoSource,
    private val phone: () -> PhoneInfo,
    private val clock: () -> Long = System::currentTimeMillis,
) {

    data class Result(
        val report: CapabilityReport,
        /** HR mode per heart-rate origin package (continuous / every_10_min / manual / unknown). */
        val hrModeByPackage: Map<String, String>,
        /** The "auto" heart-rate origin ([WellnessRules.autoOrigin]). */
        val autoHeartRateOrigin: String?,
        /** The device type and model that origin's heart rate mostly carried (labels the buckets). */
        val autoHeartRateDevice: HcOrigin? = null,
        /** True when Samsung Health is installed and at least one record carries its origin. */
        val samsungWritesToHc: Boolean,
    )

    suspend fun run(windowDays: Int = WINDOW_DAYS, maxPagesPerType: Int = MAX_PAGES_PER_TYPE): Result {
        val now = clock()
        val info = phone()
        val sdk = gateway.sdkStatus()
        val available = sdk == HcSdkStatus.AVAILABLE
        val background = if (available) runCatching { gateway.featureStatus(HcFeature.BACKGROUND_READ) }
            .getOrDefault(HcFeatureStatus.UNAVAILABLE) else HcFeatureStatus.UNAVAILABLE
        val history = if (available) runCatching { gateway.featureStatus(HcFeature.HISTORY_READ) }
            .getOrDefault(HcFeatureStatus.UNAVAILABLE) else HcFeatureStatus.UNAVAILABLE
        val granted = if (available) runCatching { gateway.grantedPermissions() }.getOrDefault(emptySet()) else emptySet()

        // origin -> type -> sample times (times only; this map never holds a value)
        val seen = LinkedHashMap<HcOrigin, MutableMap<HcType, MutableList<Long>>>()
        val sleepWithStages = HashMap<HcOrigin, Int>()
        if (available) {
            val start = now - windowDays * BucketBuilder.DAY_MS
            for (type in HcType.entries) {
                if (type.readPermission !in granted) continue
                var token: String? = null
                var pages = 0
                do {
                    val page = runCatching { gateway.readRecords(type, start, now, token) }.getOrNull() ?: break
                    for (r in page.records) {
                        val origin = r.meta.origin
                        val times = seen.getOrPut(origin) { LinkedHashMap() }.getOrPut(type) { mutableListOf() }
                        when (r) {
                            is HcRecord.HeartRate -> times += r.sampleTimesMs.ifEmpty { listOf(r.startMs) }
                            is HcRecord.Sleep -> {
                                times += r.startMs
                                if (r.stages.isNotEmpty()) sleepWithStages.merge(origin, 1, Int::plus)
                            }
                            else -> times += r.startMs
                        }
                    }
                    token = page.nextPageToken
                    pages++
                } while (token != null && pages < maxPagesPerType)
            }
        }

        val origins = seen.entries
            .sortedWith(compareBy({ it.key.packageName }, { it.key.deviceType ?: "" }, { it.key.model ?: "" },
                { it.key.recordingMethod }))
            .map { (origin, byType) ->
                OriginReport(
                    packageName = origin.packageName,
                    label = packages.label(origin.packageName) ?: origin.packageName,
                    deviceType = origin.deviceType,
                    model = origin.model,
                    manufacturer = origin.manufacturer,
                    recordingMethod = origin.recordingMethod,
                    metrics = byType.entries.sortedBy { it.key.ordinal }.flatMap { (type, times) ->
                        metricsFor(type, times, sleepWithStages[origin] ?: 0)
                    },
                )
            }

        val hrByPackage = seen.entries
            .filter { HcType.HEART_RATE in it.value }
            .groupBy({ it.key.packageName }, { it.value.getValue(HcType.HEART_RATE) })
            .mapValues { (_, lists) ->
                val times = lists.flatten()
                WellnessRules.hrMode(medianGapS(times), times.size)
            }
        val hrCounts = seen.entries
            .filter { HcType.HEART_RATE in it.value }
            .associate { it.key to it.value.getValue(HcType.HEART_RATE).size }
        val samsungInstalled = packages.isInstalled(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE)

        val report = CapabilityReport(
            phone = PhoneReport(info.manufacturer, info.model, info.deviceName, info.sdkInt, info.appVersion),
            healthConnect = HcReport(sdk.wire, background.wire, history.wire, granted.sorted()),
            samsungHealth = SamsungHealthReport(
                samsungInstalled,
                if (samsungInstalled) packages.versionName(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE) else null,
            ),
            origins = origins,
            reportedAt = IngestPayloadBuilder.iso(now),
        )
        val auto = WellnessRules.autoOrigin(hrCounts)
        return Result(
            report = report,
            hrModeByPackage = hrByPackage,
            autoHeartRateOrigin = auto,
            autoHeartRateDevice = hrCounts.entries.filter { it.key.packageName == auto }
                .sortedWith(compareByDescending<Map.Entry<HcOrigin, Int>> { it.value }
                    .thenBy { it.key.deviceType ?: "" }.thenBy { it.key.model ?: "" })
                .firstOrNull()?.key,
            samsungWritesToHc = seen.keys.any { it.packageName == WellnessCatalog.SAMSUNG_HEALTH_PACKAGE },
        )
    }

    private fun metricsFor(type: HcType, times: List<Long>, sessionsWithStages: Int): List<OriginMetric> {
        val sorted = times.sorted()
        val last = sorted.lastOrNull()?.let { IngestPayloadBuilder.iso(it) }
        val gap = medianGapS(sorted)
        fun m(id: String, count: Int) = OriginMetric(id, count, last, gap)
        return when (type) {
            HcType.SLEEP -> listOf(m("sleep_duration", sorted.size), m("sleep_stages", sessionsWithStages))
            HcType.BLOOD_PRESSURE -> listOf(m("bp_systolic", sorted.size), m("bp_diastolic", sorted.size))
            HcType.HEART_RATE -> listOf(m("heart_rate", sorted.size))
            HcType.RESTING_HEART_RATE -> listOf(m("resting_hr", sorted.size))
            else -> listOf(m(type.wire, sorted.size))
        }
    }

    companion object {
        const val WINDOW_DAYS = 14
        const val MAX_PAGES_PER_TYPE = 50

        /** Median gap between consecutive timestamps, in whole seconds; null under 2 samples. */
        fun medianGapS(times: List<Long>): Long? {
            if (times.size < 2) return null
            val sorted = times.sorted()
            val gaps = (1 until sorted.size).map { sorted[it] - sorted[it - 1] }.filter { it > 0 }.sorted()
            if (gaps.isEmpty()) return 0L
            val mid = gaps.size / 2
            val ms = if (gaps.size % 2 == 1) gaps[mid] else (gaps[mid - 1] + gaps[mid]) / 2
            return Math.round(ms / 1000.0)
        }

        private val JSON = Json { encodeDefaults = true; explicitNulls = true }

        fun toJson(report: CapabilityReport): String = JSON.encodeToString(report)
    }
}

/** App labels and versions, from PackageManager in production and a fake in tests. */
interface PackageInfoSource {
    fun label(packageName: String): String?
    fun versionName(packageName: String): String?
    fun isInstalled(packageName: String): Boolean
}

data class PhoneInfo(
    val manufacturer: String,
    val model: String,
    val deviceName: String,
    val sdkInt: Int,
    val appVersion: String,
)

@Serializable
data class CapabilityReport(
    val phone: PhoneReport,
    @SerialName("health_connect") val healthConnect: HcReport,
    @SerialName("samsung_health") val samsungHealth: SamsungHealthReport,
    val origins: List<OriginReport>,
    @SerialName("reported_at") val reportedAt: String,
)

@Serializable
data class PhoneReport(
    val manufacturer: String,
    val model: String,
    @SerialName("device_name") val deviceName: String,
    @SerialName("sdk_int") val sdkInt: Int,
    @SerialName("app_version") val appVersion: String,
)

@Serializable
data class HcReport(
    @SerialName("sdk_status") val sdkStatus: String,
    @SerialName("background_feature") val backgroundFeature: String,
    @SerialName("history_feature") val historyFeature: String,
    val granted: List<String>,
)

@Serializable
data class SamsungHealthReport(val installed: Boolean, val version: String?)

@Serializable
data class OriginReport(
    @SerialName("package") val packageName: String,
    val label: String,
    @SerialName("device_type") val deviceType: String?,
    val model: String?,
    val manufacturer: String?,
    @SerialName("recording_method") val recordingMethod: String,
    val metrics: List<OriginMetric>,
)

@Serializable
data class OriginMetric(
    val metric: String,
    @SerialName("count_14d") val count14d: Int,
    @SerialName("last_sample") val lastSample: String?,
    @SerialName("median_gap_s") val medianGapS: Long?,
)
