package com.aiblackbox.portal.data.wellness

/**
 * M7 health ("wellness" in code, "Health" in the UI): what the collector may read and how it
 * maps onto Health Connect.
 *
 * The wire contract shared with the box fixes four consent categories and the v1 metric list;
 * this file maps them onto Health Connect record types and READ permission strings. It is
 * mirrored byte for byte by `res/raw/wellness_catalog.json` (WellnessCatalogTest compares the
 * two), so the box, the setup copy and the permission request all read one list.
 *
 * READ ONLY. There is no write permission here and never `READ_EXERCISE_ROUTE` (routes are
 * location history). WellnessManifestPermissionsTest pins the manifest to exactly
 * [ALL_READ_PERMISSIONS] plus the two special grants.
 */

/** The four consent categories. [wire] is the contract id. */
enum class WellnessCategory(val wire: String, val label: String, val example: String) {
    SLEEP("sleep", "Sleep", "How did I sleep last night?"),
    HEART("heart", "Heart", "What was my heart rate during yesterday's run?"),
    ACTIVITY("activity", "Activity", "How many steps so far today?"),
    BLOOD_PRESSURE("blood_pressure", "Blood pressure", "Show my blood pressure readings this week"),
    ;

    companion object {
        fun fromWire(id: String): WellnessCategory? = entries.firstOrNull { it.wire == id }
    }
}

/** Consent per category: Off, Ask (answer when asked), Proactive (may bring it up). */
enum class WellnessConsent(val wire: String, val label: String) {
    OFF("off", "Off"),
    ASK("ask", "Ask"),
    PROACTIVE("proactive", "Proactive"),
    ;

    val isOn: Boolean get() = this != OFF

    companion object {
        fun fromWire(id: String?): WellnessConsent = entries.firstOrNull { it.wire == id } ?: OFF
    }
}

/** The Health Connect record types the collector reads, with their READ permission. */
enum class HcType(val wire: String, val recordName: String, val readPermission: String) {
    SLEEP("sleep", "SleepSessionRecord", "android.permission.health.READ_SLEEP"),
    HEART_RATE("heart_rate", "HeartRateRecord", "android.permission.health.READ_HEART_RATE"),
    RESTING_HEART_RATE("resting_heart_rate", "RestingHeartRateRecord", "android.permission.health.READ_RESTING_HEART_RATE"),
    HRV("hrv", "HeartRateVariabilityRmssdRecord", "android.permission.health.READ_HEART_RATE_VARIABILITY"),
    SPO2("spo2", "OxygenSaturationRecord", "android.permission.health.READ_OXYGEN_SATURATION"),
    RESPIRATORY_RATE("respiratory_rate", "RespiratoryRateRecord", "android.permission.health.READ_RESPIRATORY_RATE"),
    STEPS("steps", "StepsRecord", "android.permission.health.READ_STEPS"),
    DISTANCE("distance", "DistanceRecord", "android.permission.health.READ_DISTANCE"),
    ACTIVE_CALORIES("active_calories", "ActiveCaloriesBurnedRecord", "android.permission.health.READ_ACTIVE_CALORIES_BURNED"),
    TOTAL_CALORIES("total_calories", "TotalCaloriesBurnedRecord", "android.permission.health.READ_TOTAL_CALORIES_BURNED"),
    EXERCISE("exercise", "ExerciseSessionRecord", "android.permission.health.READ_EXERCISE"),
    BLOOD_PRESSURE("blood_pressure", "BloodPressureRecord", "android.permission.health.READ_BLOOD_PRESSURE"),
    ;

    /** How the collector ships this type: aggregated buckets, whole sessions, or single readings. */
    val shape: HcShape
        get() = when (this) {
            HEART_RATE, STEPS, DISTANCE, ACTIVE_CALORIES, TOTAL_CALORIES -> HcShape.BUCKETED
            SLEEP, EXERCISE -> HcShape.SESSION
            RESTING_HEART_RATE, HRV, SPO2, RESPIRATORY_RATE, BLOOD_PRESSURE -> HcShape.READING
        }

    companion object {
        fun fromWire(id: String): HcType? = entries.firstOrNull { it.wire == id }
    }
}

enum class HcShape { BUCKETED, SESSION, READING }

/** How a contract metric is produced. DERIVED ones are computed on the box from other data. */
enum class MetricKind(val wire: String) { BUCKET("bucket"), SESSION("session"), READING("reading"), DERIVED("derived") }

data class WellnessMetric(
    val id: String,
    val category: WellnessCategory,
    val kind: MetricKind,
    val sources: List<HcType>,
)

object WellnessCatalog {

    const val SCHEMA = "bbx.wellness.catalog/1"

    /** Health Connect's own package (the `<queries>` entry, and the provider for getSdkStatus). */
    const val HEALTH_CONNECT_PACKAGE = "com.google.android.apps.healthdata"

    /** Samsung Health's package (spec T-4 confirms it on the device). */
    const val SAMSUNG_HEALTH_PACKAGE = "com.sec.android.app.shealth"

    /** Past data (older than 30 days before the first grant). Its own user grant. */
    const val PERMISSION_HISTORY = "android.permission.health.READ_HEALTH_DATA_HISTORY"

    /** Reads while the app is not visible (decision D1: a separate grant, never pre-selected). */
    const val PERMISSION_BACKGROUND = "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"

    /** The v1 metric list of the wire contract, in contract order. */
    val METRICS: List<WellnessMetric> = listOf(
        WellnessMetric("sleep_duration", WellnessCategory.SLEEP, MetricKind.SESSION, listOf(HcType.SLEEP)),
        WellnessMetric("sleep_stages", WellnessCategory.SLEEP, MetricKind.SESSION, listOf(HcType.SLEEP)),
        // Sleeping heart rate is heart data measured during sleep: the box joins the heart-rate
        // buckets with the sleep sessions, so it needs both categories on.
        WellnessMetric("sleeping_hr_median", WellnessCategory.HEART, MetricKind.DERIVED, listOf(HcType.HEART_RATE, HcType.SLEEP)),
        WellnessMetric("sleeping_hr_p10", WellnessCategory.HEART, MetricKind.DERIVED, listOf(HcType.HEART_RATE, HcType.SLEEP)),
        WellnessMetric("resting_hr", WellnessCategory.HEART, MetricKind.READING, listOf(HcType.RESTING_HEART_RATE)),
        WellnessMetric("heart_rate", WellnessCategory.HEART, MetricKind.BUCKET, listOf(HcType.HEART_RATE)),
        WellnessMetric("hrv", WellnessCategory.HEART, MetricKind.READING, listOf(HcType.HRV)),
        WellnessMetric("spo2", WellnessCategory.HEART, MetricKind.READING, listOf(HcType.SPO2)),
        WellnessMetric("respiratory_rate", WellnessCategory.HEART, MetricKind.READING, listOf(HcType.RESPIRATORY_RATE)),
        WellnessMetric("steps", WellnessCategory.ACTIVITY, MetricKind.BUCKET, listOf(HcType.STEPS)),
        WellnessMetric("distance", WellnessCategory.ACTIVITY, MetricKind.BUCKET, listOf(HcType.DISTANCE)),
        WellnessMetric("active_calories", WellnessCategory.ACTIVITY, MetricKind.BUCKET, listOf(HcType.ACTIVE_CALORIES)),
        WellnessMetric("total_calories", WellnessCategory.ACTIVITY, MetricKind.BUCKET, listOf(HcType.TOTAL_CALORIES)),
        WellnessMetric("exercise", WellnessCategory.ACTIVITY, MetricKind.SESSION, listOf(HcType.EXERCISE)),
        WellnessMetric("bp_systolic", WellnessCategory.BLOOD_PRESSURE, MetricKind.READING, listOf(HcType.BLOOD_PRESSURE)),
        WellnessMetric("bp_diastolic", WellnessCategory.BLOOD_PRESSURE, MetricKind.READING, listOf(HcType.BLOOD_PRESSURE)),
    )

    /** The record types each category reads. A type belongs to exactly one category. */
    val TYPES_BY_CATEGORY: Map<WellnessCategory, List<HcType>> = linkedMapOf(
        WellnessCategory.SLEEP to listOf(HcType.SLEEP),
        WellnessCategory.HEART to listOf(
            HcType.HEART_RATE, HcType.RESTING_HEART_RATE, HcType.HRV, HcType.SPO2, HcType.RESPIRATORY_RATE,
        ),
        WellnessCategory.ACTIVITY to listOf(
            HcType.STEPS, HcType.DISTANCE, HcType.ACTIVE_CALORIES, HcType.TOTAL_CALORIES, HcType.EXERCISE,
        ),
        WellnessCategory.BLOOD_PRESSURE to listOf(HcType.BLOOD_PRESSURE),
    )

    /** Setup step 3: Sleep, Heart and Activity pre-selected as Ask; blood pressure off. */
    val DEFAULT_CONSENT: Map<WellnessCategory, WellnessConsent> = linkedMapOf(
        WellnessCategory.SLEEP to WellnessConsent.ASK,
        WellnessCategory.HEART to WellnessConsent.ASK,
        WellnessCategory.ACTIVITY to WellnessConsent.ASK,
        WellnessCategory.BLOOD_PRESSURE to WellnessConsent.OFF,
    )

    /** Decision D9: blood pressure is local-AI-only by default; the rest may use an allowed cloud AI. */
    val DEFAULT_LOCAL_ONLY: Map<WellnessCategory, Boolean> = linkedMapOf(
        WellnessCategory.SLEEP to false,
        WellnessCategory.HEART to false,
        WellnessCategory.ACTIVITY to false,
        WellnessCategory.BLOOD_PRESSURE to true,
    )

    /** Every READ permission the app may ever request (the manifest declares exactly these). */
    val ALL_READ_PERMISSIONS: Set<String> = HcType.entries.map { it.readPermission }.toSortedSet()

    fun categoryOf(type: HcType): WellnessCategory =
        TYPES_BY_CATEGORY.entries.first { type in it.value }.key

    fun typesFor(categories: Collection<WellnessCategory>): List<HcType> =
        HcType.entries.filter { categoryOf(it) in categories }

    fun metric(id: String): WellnessMetric? = METRICS.firstOrNull { it.id == id }

    /** The record READ permissions for the [categories] that are on. Ordered, stable. */
    fun readPermissionsFor(categories: Collection<WellnessCategory>): List<String> =
        typesFor(categories).map { it.readPermission }

    fun typeForPermission(permission: String): HcType? = HcType.entries.firstOrNull { it.readPermission == permission }
}
