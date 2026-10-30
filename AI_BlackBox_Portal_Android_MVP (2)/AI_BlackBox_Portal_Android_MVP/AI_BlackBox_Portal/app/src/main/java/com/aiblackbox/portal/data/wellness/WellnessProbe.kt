package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import java.time.Instant
import java.time.ZoneId

/**
 * The M7.1 device tests (spec §2.4, T-1 to T-11), as far as they can run on the phone. It
 * SENDS NOTHING to the box: the result is a JSON report the owner exports from the debug
 * Probe screen and reviews. It holds no health values, only statuses, counts, dates and
 * gaps; T-6 and T-7 are manual checks and say how to do them.
 */
class WellnessProbe(
    private val gateway: HealthConnectGateway,
    private val packages: PackageInfoSource,
    private val phone: () -> PhoneInfo,
    private val state: () -> WellnessSyncState,
    private val env: Environment,
    private val clock: () -> Long = System::currentTimeMillis,
    private val zone: () -> ZoneId = { ZoneId.systemDefault() },
) {
    /** The few platform facts the probe needs that are not Health Connect's. */
    interface Environment {
        /** T-8: can an app read Settings.Global.DEVICE_NAME on this build? */
        fun deviceNameReadable(): Boolean

        /** T-9: is BlackBox exempt from battery optimisation (null = unknown)? */
        fun batteryExempt(): Boolean?

        /** T-10: this phone's tailnet address, to match the box's registry row. */
        fun tailnetIp(): String?
    }

    suspend fun run(windowDays: Int = CapabilityProbe.WINDOW_DAYS): JsonObject {
        val now = clock()
        val z = zone()
        val info = phone()
        val st = state()
        val sdk = gateway.sdkStatus()
        val available = sdk == HcSdkStatus.AVAILABLE
        val granted = if (available) runCatching { gateway.grantedPermissions() }.getOrDefault(emptySet()) else emptySet()
        suspend fun feature(f: HcFeature) = if (available) runCatching { gateway.featureStatus(f) }
            .getOrDefault(HcFeatureStatus.UNAVAILABLE) else HcFeatureStatus.UNAVAILABLE

        // Earliest record per granted type (T-1, T-2), and the 14-day window (T-3, T-4, T-5).
        val earliest = linkedMapOf<HcType, HcRecord?>()
        val window = linkedMapOf<HcType, List<HcRecord>>()
        if (available) for (type in HcType.entries) {
            if (type.readPermission !in granted) continue
            earliest[type] = runCatching { gateway.readRecords(type, 0L, now, pageSize = 1, ascending = true) }
                .getOrNull()?.records?.firstOrNull()
            window[type] = readWindow(type, now - windowDays * BucketBuilder.DAY_MS, now)
        }
        fun day(ms: Long) = Instant.ofEpochMilli(ms).atZone(z).toLocalDate().toString()
        fun isSamsung(r: HcRecord) = r.meta.origin.packageName == WellnessCatalog.SAMSUNG_HEALTH_PACKAGE
        fun earliestSamsung(type: HcType): JsonElement {
            // The oldest record overall may be another app's; Samsung's own oldest is the answer.
            val candidates = window[type].orEmpty().filter(::isSamsung).map { it.startMs } +
                listOfNotNull(earliest[type]?.takeIf(::isSamsung)?.startMs)
            return candidates.minOrNull()?.let { JsonPrimitive(day(it)) } ?: JsonNull
        }

        val runs = st.runLog
        val gaps = runs.zipWithNext { a, b -> (b.atMs - a.atMs) / 60_000L }
        val hrRecords = window[HcType.HEART_RATE].orEmpty().filterIsInstance<HcRecord.HeartRate>()
        val sleep = window[HcType.SLEEP].orEmpty().filterIsInstance<HcRecord.Sleep>()
        val bp = window[HcType.BLOOD_PRESSURE].orEmpty().filterIsInstance<HcRecord.Reading>()
        val oldestMs = earliest.values.filterNotNull().minOfOrNull { it.startMs }

        return buildJsonObject {
            put("schema", JsonPrimitive("bbx.wellness.probe/1"))
            put("generated_at", JsonPrimitive(IngestPayloadBuilder.iso(now)))
            put("sent_to_box", JsonPrimitive(false))
            put("window_days", JsonPrimitive(windowDays))
            put("t1_grants_and_background", buildJsonObject {
                put("sdk_status", JsonPrimitive(sdk.wire))
                put("background_feature", JsonPrimitive(feature(HcFeature.BACKGROUND_READ).wire))
                put("history_feature", JsonPrimitive(feature(HcFeature.HISTORY_READ).wire))
                put("background_granted", JsonPrimitive(WellnessCatalog.PERMISSION_BACKGROUND in granted))
                put("history_granted", JsonPrimitive(WellnessCatalog.PERMISSION_HISTORY in granted))
                put("granted_types", buildJsonArray { WellnessCatalog.ALL_READ_PERMISSIONS.filter { it in granted }.forEach {
                    add(JsonPrimitive(WellnessCatalog.typeForPermission(it)?.wire ?: it)) } })
                put("earliest_record_day", buildJsonObject {
                    for ((type, r) in earliest) put(type.wire, r?.let { JsonPrimitive(day(it.startMs)) } ?: JsonNull)
                })
                put("data_older_than_31_days", JsonPrimitive(oldestMs != null && now - oldestMs > 31 * BucketBuilder.DAY_MS))
                put("periodic_runs_logged", JsonPrimitive(runs.size))
                put("periodic_runs_screen_off", JsonPrimitive(runs.count { !it.interactive }))
                put("periodic_runs_ok", JsonPrimitive(runs.count { it.outcome == "ok" }))
            })
            put("t2_backfill_depth", buildJsonObject {
                put("earliest_samsung_heart_rate", earliestSamsung(HcType.HEART_RATE))
                put("earliest_samsung_sleep", earliestSamsung(HcType.SLEEP))
                put("earliest_samsung_blood_pressure", earliestSamsung(HcType.BLOOD_PRESSURE))
            })
            put("t3_heart_rate_density", buildJsonObject {
                put("origins", buildJsonArray {
                    for ((origin, recs) in hrRecords.groupBy { it.meta.origin.packageName to it.meta.origin.deviceType }) {
                        val times = recs.flatMap { it.sampleTimesMs }
                        val perBucket = times.groupingBy { it / BucketBuilder.FIVE_MIN_MS }.eachCount().values
                        add(buildJsonObject {
                            put("package", JsonPrimitive(origin.first))
                            put("device_type", origin.second?.let { JsonPrimitive(it) } ?: JsonNull)
                            put("samples", JsonPrimitive(times.size))
                            put("median_gap_s", CapabilityProbe.medianGapS(times)?.let { JsonPrimitive(it) } ?: JsonNull)
                            put("hr_mode", JsonPrimitive(WellnessRules.hrMode(CapabilityProbe.medianGapS(times), times.size)))
                            put("buckets_5min_by_samples", buildJsonObject {
                                put("1", JsonPrimitive(perBucket.count { it == 1 }))
                                put("2-4", JsonPrimitive(perBucket.count { it in 2..4 }))
                                put("5+", JsonPrimitive(perBucket.count { it >= 5 }))
                            })
                        })
                    }
                })
                val allHr = hrRecords.flatMap { it.sampleTimesMs }.sorted()
                put("sleep_sessions", JsonPrimitive(sleep.size))
                put("sleep_sessions_with_hr", JsonPrimitive(sleep.count { s -> allHr.any { it >= s.startMs && it < s.endMs } }))
            })
            put("t4_metadata", buildJsonObject {
                put("device_filled", buildJsonObject {
                    for ((type, recs) in window) put(type.wire, JsonPrimitive("${recs.count { it.meta.origin.deviceType != null }}/${recs.size}"))
                })
                put("samsung_writes", buildJsonArray {
                    for ((type, recs) in window) if (recs.any { it.meta.origin.packageName == WellnessCatalog.SAMSUNG_HEALTH_PACKAGE })
                        add(JsonPrimitive(type.wire))
                })
                put("bp_measurement_location_filled",
                    JsonPrimitive("${bp.count { (it.attrs["measurement_location"] ?: "unknown") != "unknown" }}/${bp.size}"))
                val installed = packages.isInstalled(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE)
                put("samsung_package", JsonPrimitive(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE))
                put("samsung_installed", JsonPrimitive(installed))
                put("samsung_version", packages.versionName(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE)?.let { JsonPrimitive(it) } ?: JsonNull)
            })
            put("t5_blood_pressure_chain", buildJsonObject {
                put("origins", buildJsonArray {
                    for ((key, recs) in bp.groupBy { it.meta.origin.packageName to it.meta.origin.recordingMethod }) add(buildJsonObject {
                        put("package", JsonPrimitive(key.first))
                        put("label", JsonPrimitive(packages.label(key.first) ?: key.first))
                        put("recording_method", JsonPrimitive(key.second))
                        put("count_window", JsonPrimitive(recs.size))
                    })
                })
            })
            put("t6_samsung_export", manual("In Samsung Health: Settings, Download personal data. Note whether it exists and the file layout."))
            put("t7_grant_labels", manual("Write down the exact Health Connect labels for the past-data and background grants."))
            put("t8_device_name_and_deletions", buildJsonObject {
                put("device_name_readable", JsonPrimitive(env.deviceNameReadable()))
                put("deletion_changes_seen", JsonPrimitive(st.deletionsSeen))
            })
            put("t9_scheduling", buildJsonObject {
                put("battery_exempt", env.batteryExempt()?.let { JsonPrimitive(it) } ?: JsonNull)
                put("interval_min", JsonPrimitive(st.intervalMin))
                put("periodic_gaps_min", JsonArray(gaps.takeLast(48).map { JsonPrimitive(it) }))
                put("max_gap_min", gaps.maxOrNull()?.let { JsonPrimitive(it) } ?: JsonNull)
                put("last_nudge_at", if (st.lastNudgeMs > 0) JsonPrimitive(IngestPayloadBuilder.iso(st.lastNudgeMs)) else JsonNull)
            })
            put("t10_this_phone", buildJsonObject {
                put("manufacturer", JsonPrimitive(info.manufacturer))
                put("model", JsonPrimitive(info.model))
                put("tailnet_ip", env.tailnetIp()?.let { JsonPrimitive(it) } ?: JsonNull)
            })
            put("t11_sleep_arrival", buildJsonObject {
                put("first_seen_after_end_min", JsonArray(st.sleepLatencyMin.map { JsonPrimitive(it) }))
            })
        }
    }

    private suspend fun readWindow(type: HcType, start: Long, end: Long): List<HcRecord> {
        val out = mutableListOf<HcRecord>()
        var token: String? = null
        var pages = 0
        do {
            val page = runCatching { gateway.readRecords(type, start, end, token) }.getOrNull() ?: break
            out += page.records
            token = page.nextPageToken
        } while (token != null && ++pages < CapabilityProbe.MAX_PAGES_PER_TYPE)
        return out
    }

    private fun manual(how: String) = buildJsonObject {
        put("automatable", JsonPrimitive(false))
        put("how", JsonPrimitive(how))
    }
}
