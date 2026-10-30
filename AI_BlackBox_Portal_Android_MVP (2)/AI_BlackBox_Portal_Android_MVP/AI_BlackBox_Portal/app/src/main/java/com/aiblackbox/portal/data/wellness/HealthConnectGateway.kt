package com.aiblackbox.portal.data.wellness

/**
 * The collector's only door into Health Connect, as an INTERFACE so everything that decides
 * (probe grouping, buckets, payloads, the sync engine) is JVM-tested against
 * `FakeHealthConnectGateway` with no device. [AndroidHealthConnectGateway] is the thin real
 * implementation over `androidx.health.connect:connect-client`.
 *
 * Every type here is plain Kotlin: no SDK class crosses this boundary. Times are epoch
 * milliseconds; zone offsets are seconds east of UTC, as Health Connect records carry them.
 */
interface HealthConnectGateway {
    /** Is the Health Connect provider installed and current? Cheap; no IPC. */
    fun sdkStatus(): HcSdkStatus

    /** A feature flag (background reads, history reads, skin temperature). */
    suspend fun featureStatus(feature: HcFeature): HcFeatureStatus

    /** The health permissions this app holds right now (record READ_* plus the two specials). */
    suspend fun grantedPermissions(): Set<String>

    /**
     * `aggregateGroupByDuration` over [startMs, endMs) in [sliceMs] slices. [originPackage]
     * restricts to one data origin (heart rate and SpO2 combine every origin otherwise);
     * null asks Health Connect for its priority-deduplicated merge (activity, sleep).
     * Empty slices are still returned, with null values: a gap, never a zero.
     */
    suspend fun aggregateByDuration(
        keys: Set<AggKey>,
        startMs: Long,
        endMs: Long,
        sliceMs: Long,
        originPackage: String? = null,
    ): List<HcAggBucket>

    /** One `aggregate` over [startMs, endMs) (per-session distance, kcal, HR). */
    suspend fun aggregate(
        keys: Set<AggKey>,
        startMs: Long,
        endMs: Long,
        originPackage: String? = null,
    ): Map<AggKey, Double?>

    /** One page of [type] records whose time falls in [startMs, endMs). */
    suspend fun readRecords(
        type: HcType,
        startMs: Long,
        endMs: Long,
        pageToken: String? = null,
        pageSize: Int = DEFAULT_PAGE_SIZE,
        ascending: Boolean = true,
    ): HcPage

    /** A fresh Changes token for one record type. */
    suspend fun changesToken(type: HcType): String

    /** The changes since [token]; loop while [HcChanges.hasMore]. */
    suspend fun getChanges(token: String): HcChanges

    companion object {
        const val DEFAULT_PAGE_SIZE = 1000
    }
}

enum class HcSdkStatus(val wire: String) {
    AVAILABLE("available"),
    NEEDS_UPDATE("needs_update"),
    UNAVAILABLE("unavailable"),
}

enum class HcFeature { BACKGROUND_READ, HISTORY_READ, SKIN_TEMPERATURE }

enum class HcFeatureStatus(val wire: String) {
    AVAILABLE("available"),
    UNAVAILABLE("unavailable"),
}

/** Aggregate metrics the collector asks for. Values come back as Double (Long counts widened). */
enum class AggKey { BPM_AVG, BPM_MIN, BPM_MAX, BPM_COUNT, STEPS, DISTANCE_M, ACTIVE_KCAL, TOTAL_KCAL }

/**
 * Where a record came from: the writing app, the device Health Connect says recorded it (often
 * not filled; spec T-4), and how it was recorded. [deviceType] uses the contract's upper-case
 * names (WATCH, RING, PHONE, ...) or null when Health Connect does not say.
 */
data class HcOrigin(
    val packageName: String,
    val deviceType: String? = null,
    val manufacturer: String? = null,
    val model: String? = null,
    val recordingMethod: String = RECORDING_UNKNOWN,
) {
    companion object {
        const val RECORDING_UNKNOWN = "unknown"
        const val RECORDING_ACTIVE = "active"
        const val RECORDING_AUTOMATIC = "automatic"
        const val RECORDING_MANUAL = "manual"

        /** Device types that are worn: the "auto" heart-rate origin prefers these. */
        val WEARABLE_TYPES = setOf("WATCH", "RING", "FITNESS_BAND", "CHEST_STRAP")
    }
}

data class HcMeta(val id: String, val origin: HcOrigin, val lastModifiedMs: Long)

/** One record, reduced to what the collector needs. Values are only what the payload carries. */
sealed class HcRecord {
    abstract val type: HcType
    abstract val meta: HcMeta
    abstract val startMs: Long
    abstract val endMs: Long
    abstract val offsetS: Int

    data class Sleep(
        override val meta: HcMeta,
        override val startMs: Long,
        override val endMs: Long,
        override val offsetS: Int,
        val stages: List<HcStage>,
    ) : HcRecord() {
        override val type: HcType get() = HcType.SLEEP
    }

    data class Exercise(
        override val meta: HcMeta,
        override val startMs: Long,
        override val endMs: Long,
        override val offsetS: Int,
        val exerciseType: Int,
    ) : HcRecord() {
        override val type: HcType get() = HcType.EXERCISE
    }

    /** A heart-rate series record. Only the sample times are kept (the probe counts gaps). */
    data class HeartRate(
        override val meta: HcMeta,
        override val startMs: Long,
        override val endMs: Long,
        override val offsetS: Int,
        val sampleTimesMs: List<Long>,
    ) : HcRecord() {
        override val type: HcType get() = HcType.HEART_RATE
    }

    /** Steps / distance / calories intervals: their values arrive through aggregates. */
    data class Interval(
        override val type: HcType,
        override val meta: HcMeta,
        override val startMs: Long,
        override val endMs: Long,
        override val offsetS: Int,
    ) : HcRecord()

    /**
     * A point reading: resting HR (bpm), HRV (RMSSD ms), SpO2 (%), respiratory rate (/min), or
     * blood pressure ([value] systolic, [value2] diastolic, mmHg, plus [attrs]).
     */
    data class Reading(
        override val type: HcType,
        override val meta: HcMeta,
        val timeMs: Long,
        override val offsetS: Int,
        val value: Double,
        val value2: Double? = null,
        val attrs: Map<String, String> = emptyMap(),
    ) : HcRecord() {
        override val startMs: Long get() = timeMs
        override val endMs: Long get() = timeMs
    }
}

/** A sleep stage. [code] is Health Connect's stage code (2, 4, 5, 6 asleep; 1, 3, 7 awake). */
data class HcStage(val startMs: Long, val endMs: Long, val code: Int)

data class HcAggBucket(
    val startMs: Long,
    val endMs: Long,
    val offsetS: Int,
    val values: Map<AggKey, Double?>,
)

data class HcPage(val records: List<HcRecord>, val nextPageToken: String?)

sealed class HcChange {
    data class Upsert(val record: HcRecord) : HcChange()
    data class Deletion(val id: String) : HcChange()
}

data class HcChanges(
    val changes: List<HcChange>,
    val nextToken: String,
    val hasMore: Boolean,
    val tokenExpired: Boolean,
)

/** Health Connect refused for quota: back off ([WellnessRules.quotaBackoffMs]). */
class HcQuotaException(message: String) : Exception(message)

/** A read was refused because the type is not granted (or was revoked mid-run). */
class HcPermissionException(val type: HcType?, message: String) : Exception(message)
