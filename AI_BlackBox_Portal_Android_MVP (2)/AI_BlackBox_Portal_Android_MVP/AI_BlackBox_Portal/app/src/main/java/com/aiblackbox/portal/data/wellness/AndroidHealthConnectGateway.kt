package com.aiblackbox.portal.data.wellness

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.aggregate.AggregateMetric
import androidx.health.connect.client.aggregate.AggregationResult
import androidx.health.connect.client.changes.DeletionChange
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.records.ActiveCaloriesBurnedRecord
import androidx.health.connect.client.records.BloodPressureRecord
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.OxygenSaturationRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.RespiratoryRateRecord
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Device
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.request.AggregateGroupByDurationRequest
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.health.connect.client.units.Energy
import androidx.health.connect.client.units.Length
import java.time.Duration
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.reflect.KClass

/**
 * The real [HealthConnectGateway] over `androidx.health.connect:connect-client`. Deliberately
 * thin: it maps SDK types onto the plain ones in HealthConnectGateway.kt and translates two
 * failure kinds ([HcPermissionException], [HcQuotaException]). Every decision lives in code
 * that is JVM-tested against the fake.
 *
 * Read only: the only calls are getSdkStatus, features, getGrantedPermissions, aggregate*,
 * readRecords, getChangesToken and getChanges. Nothing here can write.
 *
 * NOT verifiable without a device (listed in the unit's notes): the exact exception type
 * Health Connect throws on quota (matched by message), whether empty duration slices are
 * returned or omitted (both are handled), and whether Samsung fills `metadata.device` (T-4).
 */
class AndroidHealthConnectGateway(context: Context) : HealthConnectGateway {

    private val appContext = context.applicationContext
    private val client: HealthConnectClient by lazy { HealthConnectClient.getOrCreate(appContext) }

    override fun sdkStatus(): HcSdkStatus = when (HealthConnectClient.getSdkStatus(appContext)) {
        HealthConnectClient.SDK_AVAILABLE -> HcSdkStatus.AVAILABLE
        HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> HcSdkStatus.NEEDS_UPDATE
        else -> HcSdkStatus.UNAVAILABLE
    }

    override suspend fun featureStatus(feature: HcFeature): HcFeatureStatus = guarded(null) {
        val id = when (feature) {
            HcFeature.BACKGROUND_READ -> HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND
            HcFeature.HISTORY_READ -> HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_HISTORY
            HcFeature.SKIN_TEMPERATURE -> HealthConnectFeatures.FEATURE_SKIN_TEMPERATURE
        }
        if (client.features.getFeatureStatus(id) == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE)
            HcFeatureStatus.AVAILABLE
        else HcFeatureStatus.UNAVAILABLE
    }

    override suspend fun grantedPermissions(): Set<String> =
        guarded(null) { client.permissionController.getGrantedPermissions() }

    override suspend fun aggregateByDuration(
        keys: Set<AggKey>,
        startMs: Long,
        endMs: Long,
        sliceMs: Long,
        originPackage: String?,
    ): List<HcAggBucket> = guarded(null) {
        val request = AggregateGroupByDurationRequest(
            metrics = keys.map { metricOf(it) }.toSet(),
            timeRangeFilter = between(startMs, endMs),
            timeRangeSlicer = Duration.ofMillis(sliceMs),
            dataOriginFilter = originPackage?.let { setOf(DataOrigin(it)) } ?: emptySet(),
        )
        client.aggregateGroupByDuration(request).map { group ->
            HcAggBucket(
                startMs = group.startTime.toEpochMilli(),
                endMs = group.endTime.toEpochMilli(),
                offsetS = group.zoneOffset.totalSeconds,
                values = keys.associateWith { valueOf(group.result, it) },
            )
        }
    }

    override suspend fun aggregate(
        keys: Set<AggKey>,
        startMs: Long,
        endMs: Long,
        originPackage: String?,
    ): Map<AggKey, Double?> = guarded(null) {
        val result = client.aggregate(
            AggregateRequest(
                metrics = keys.map { metricOf(it) }.toSet(),
                timeRangeFilter = between(startMs, endMs),
                dataOriginFilter = originPackage?.let { setOf(DataOrigin(it)) } ?: emptySet(),
            ),
        )
        keys.associateWith { valueOf(result, it) }
    }

    override suspend fun readRecords(
        type: HcType,
        startMs: Long,
        endMs: Long,
        pageToken: String?,
        pageSize: Int,
        ascending: Boolean,
    ): HcPage = guarded(type) {
        val response = client.readRecords(
            ReadRecordsRequest(
                recordType = recordClass(type),
                timeRangeFilter = between(startMs, endMs),
                ascendingOrder = ascending,
                pageSize = pageSize,
                pageToken = pageToken,
            ),
        )
        HcPage(response.records.mapNotNull { toHc(it) }, response.pageToken?.takeIf { it.isNotEmpty() })
    }

    override suspend fun changesToken(type: HcType): String = guarded(type) {
        client.getChangesToken(ChangesTokenRequest(recordTypes = setOf(recordClass(type))))
    }

    override suspend fun getChanges(token: String): HcChanges = guarded(null) {
        val response = client.getChanges(token)
        HcChanges(
            changes = response.changes.mapNotNull { change ->
                when (change) {
                    is UpsertionChange -> toHc(change.record)?.let { HcChange.Upsert(it) }
                    is DeletionChange -> HcChange.Deletion(change.recordId)
                    else -> null
                }
            },
            nextToken = response.nextChangesToken,
            hasMore = response.hasMore,
            tokenExpired = response.changesTokenExpired,
        )
    }

    // ── mapping ────────────────────────────────────────────────────────────────────────────

    private fun between(startMs: Long, endMs: Long): TimeRangeFilter =
        TimeRangeFilter.between(Instant.ofEpochMilli(startMs), Instant.ofEpochMilli(endMs))

    private fun metricOf(key: AggKey): AggregateMetric<*> = when (key) {
        AggKey.BPM_AVG -> HeartRateRecord.BPM_AVG
        AggKey.BPM_MIN -> HeartRateRecord.BPM_MIN
        AggKey.BPM_MAX -> HeartRateRecord.BPM_MAX
        AggKey.BPM_COUNT -> HeartRateRecord.MEASUREMENTS_COUNT
        AggKey.STEPS -> StepsRecord.COUNT_TOTAL
        AggKey.DISTANCE_M -> DistanceRecord.DISTANCE_TOTAL
        AggKey.ACTIVE_KCAL -> ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL
        AggKey.TOTAL_KCAL -> TotalCaloriesBurnedRecord.ENERGY_TOTAL
    }

    private fun valueOf(result: AggregationResult, key: AggKey): Double? = when (key) {
        AggKey.BPM_AVG -> result[HeartRateRecord.BPM_AVG]?.toDouble()
        AggKey.BPM_MIN -> result[HeartRateRecord.BPM_MIN]?.toDouble()
        AggKey.BPM_MAX -> result[HeartRateRecord.BPM_MAX]?.toDouble()
        AggKey.BPM_COUNT -> result[HeartRateRecord.MEASUREMENTS_COUNT]?.toDouble()
        AggKey.STEPS -> result[StepsRecord.COUNT_TOTAL]?.toDouble()
        AggKey.DISTANCE_M -> (result[DistanceRecord.DISTANCE_TOTAL] as Length?)?.inMeters
        AggKey.ACTIVE_KCAL -> (result[ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL] as Energy?)?.inKilocalories
        AggKey.TOTAL_KCAL -> (result[TotalCaloriesBurnedRecord.ENERGY_TOTAL] as Energy?)?.inKilocalories
    }

    private fun recordClass(type: HcType): KClass<out Record> = when (type) {
        HcType.SLEEP -> SleepSessionRecord::class
        HcType.HEART_RATE -> HeartRateRecord::class
        HcType.RESTING_HEART_RATE -> RestingHeartRateRecord::class
        HcType.HRV -> HeartRateVariabilityRmssdRecord::class
        HcType.SPO2 -> OxygenSaturationRecord::class
        HcType.RESPIRATORY_RATE -> RespiratoryRateRecord::class
        HcType.STEPS -> StepsRecord::class
        HcType.DISTANCE -> DistanceRecord::class
        HcType.ACTIVE_CALORIES -> ActiveCaloriesBurnedRecord::class
        HcType.TOTAL_CALORIES -> TotalCaloriesBurnedRecord::class
        HcType.EXERCISE -> ExerciseSessionRecord::class
        HcType.BLOOD_PRESSURE -> BloodPressureRecord::class
    }

    private fun offsetOf(offset: ZoneOffset?, at: Instant): Int =
        (offset ?: ZoneId.systemDefault().rules.getOffset(at)).totalSeconds

    private fun metaOf(metadata: Metadata): HcMeta {
        val device = metadata.device
        return HcMeta(
            id = metadata.id,
            origin = HcOrigin(
                packageName = metadata.dataOrigin.packageName,
                deviceType = device?.let { deviceTypeName(it.type) },
                manufacturer = device?.manufacturer?.takeIf { it.isNotBlank() },
                model = device?.model?.takeIf { it.isNotBlank() },
                recordingMethod = recordingMethodName(metadata.recordingMethod),
            ),
            lastModifiedMs = metadata.lastModifiedTime.toEpochMilli(),
        )
    }

    private fun toHc(record: Record): HcRecord? = when (record) {
        is SleepSessionRecord -> HcRecord.Sleep(
            meta = metaOf(record.metadata),
            startMs = record.startTime.toEpochMilli(),
            endMs = record.endTime.toEpochMilli(),
            offsetS = offsetOf(record.startZoneOffset, record.startTime),
            stages = record.stages.map {
                HcStage(it.startTime.toEpochMilli(), it.endTime.toEpochMilli(), it.stage)
            },
        )
        is ExerciseSessionRecord -> HcRecord.Exercise(
            meta = metaOf(record.metadata),
            startMs = record.startTime.toEpochMilli(),
            endMs = record.endTime.toEpochMilli(),
            offsetS = offsetOf(record.startZoneOffset, record.startTime),
            exerciseType = record.exerciseType,
        )
        is HeartRateRecord -> HcRecord.HeartRate(
            meta = metaOf(record.metadata),
            startMs = record.startTime.toEpochMilli(),
            endMs = record.endTime.toEpochMilli(),
            offsetS = offsetOf(record.startZoneOffset, record.startTime),
            sampleTimesMs = record.samples.map { it.time.toEpochMilli() },
        )
        is StepsRecord -> interval(HcType.STEPS, record.metadata, record.startTime, record.endTime, record.startZoneOffset)
        is DistanceRecord -> interval(HcType.DISTANCE, record.metadata, record.startTime, record.endTime, record.startZoneOffset)
        is ActiveCaloriesBurnedRecord ->
            interval(HcType.ACTIVE_CALORIES, record.metadata, record.startTime, record.endTime, record.startZoneOffset)
        is TotalCaloriesBurnedRecord ->
            interval(HcType.TOTAL_CALORIES, record.metadata, record.startTime, record.endTime, record.startZoneOffset)
        is RestingHeartRateRecord -> reading(
            HcType.RESTING_HEART_RATE, record.metadata, record.time, record.zoneOffset, record.beatsPerMinute.toDouble(),
        )
        is HeartRateVariabilityRmssdRecord -> reading(
            HcType.HRV, record.metadata, record.time, record.zoneOffset, record.heartRateVariabilityMillis,
        )
        is OxygenSaturationRecord -> reading(
            HcType.SPO2, record.metadata, record.time, record.zoneOffset, record.percentage.value,
        )
        is RespiratoryRateRecord -> reading(
            HcType.RESPIRATORY_RATE, record.metadata, record.time, record.zoneOffset, record.rate,
        )
        is BloodPressureRecord -> HcRecord.Reading(
            type = HcType.BLOOD_PRESSURE,
            meta = metaOf(record.metadata),
            timeMs = record.time.toEpochMilli(),
            offsetS = offsetOf(record.zoneOffset, record.time),
            value = record.systolic.inMillimetersOfMercury,
            value2 = record.diastolic.inMillimetersOfMercury,
            attrs = linkedMapOf(
                "measurement_location" to measurementLocationName(record.measurementLocation),
                "body_position" to bodyPositionName(record.bodyPosition),
            ),
        )
        else -> null
    }

    private fun interval(type: HcType, metadata: Metadata, start: Instant, end: Instant, offset: ZoneOffset?) =
        HcRecord.Interval(type, metaOf(metadata), start.toEpochMilli(), end.toEpochMilli(), offsetOf(offset, start))

    private fun reading(type: HcType, metadata: Metadata, time: Instant, offset: ZoneOffset?, value: Double) =
        HcRecord.Reading(type, metaOf(metadata), time.toEpochMilli(), offsetOf(offset, time), value)

    /**
     * Run one SDK call, translating failures. A SecurityException is a missing or revoked
     * grant. Quota refusals are matched by message: their exception type differs between the
     * platform (Android 14+) and the APK provider and could not be pinned without a device.
     */
    private suspend fun <T> guarded(type: HcType?, block: suspend () -> T): T = try {
        block()
    } catch (e: SecurityException) {
        throw HcPermissionException(type, "health permission missing")
    } catch (e: kotlinx.coroutines.CancellationException) {
        throw e
    } catch (e: Exception) {
        val message = e.message.orEmpty().lowercase()
        if ("rate limit" in message || "quota" in message) throw HcQuotaException("health connect quota")
        throw e
    }

    companion object {
        fun deviceTypeName(type: Int): String? = when (type) {
            Device.TYPE_WATCH -> "WATCH"
            Device.TYPE_PHONE -> "PHONE"
            Device.TYPE_SCALE -> "SCALE"
            Device.TYPE_RING -> "RING"
            Device.TYPE_HEAD_MOUNTED -> "HEAD_MOUNTED"
            Device.TYPE_FITNESS_BAND -> "FITNESS_BAND"
            Device.TYPE_CHEST_STRAP -> "CHEST_STRAP"
            Device.TYPE_SMART_DISPLAY -> "SMART_DISPLAY"
            else -> null
        }

        fun recordingMethodName(method: Int): String = when (method) {
            Metadata.RECORDING_METHOD_ACTIVELY_RECORDED -> HcOrigin.RECORDING_ACTIVE
            Metadata.RECORDING_METHOD_AUTOMATICALLY_RECORDED -> HcOrigin.RECORDING_AUTOMATIC
            Metadata.RECORDING_METHOD_MANUAL_ENTRY -> HcOrigin.RECORDING_MANUAL
            else -> HcOrigin.RECORDING_UNKNOWN
        }

        fun measurementLocationName(location: Int): String = when (location) {
            BloodPressureRecord.MEASUREMENT_LOCATION_LEFT_WRIST -> "left_wrist"
            BloodPressureRecord.MEASUREMENT_LOCATION_RIGHT_WRIST -> "right_wrist"
            BloodPressureRecord.MEASUREMENT_LOCATION_LEFT_UPPER_ARM -> "left_upper_arm"
            BloodPressureRecord.MEASUREMENT_LOCATION_RIGHT_UPPER_ARM -> "right_upper_arm"
            else -> "unknown"
        }

        fun bodyPositionName(position: Int): String = when (position) {
            BloodPressureRecord.BODY_POSITION_STANDING_UP -> "standing_up"
            BloodPressureRecord.BODY_POSITION_SITTING_DOWN -> "sitting_down"
            BloodPressureRecord.BODY_POSITION_LYING_DOWN -> "lying_down"
            BloodPressureRecord.BODY_POSITION_RECLINING -> "reclining"
            else -> "unknown"
        }
    }
}
