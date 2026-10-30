package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * The collector's small bookkeeping (no health values): what the box last said about consent,
 * the chosen heart-rate origin, the sync interval, stop flags, back-off and run times, and the
 * backfill cursor. Lives in [WellnessFiles] (no-backup storage).
 */
@Serializable
data class WellnessSyncState(
    /** The box's per-category consent as last read from a credentialed status (wire ids). */
    val categories: Map<String, String> = WellnessCatalog.DEFAULT_CONSENT.mapKeys { it.key.wire }.mapValues { it.value.wire },
    /** The box's enrollment state as last seen (wire id). */
    val state: String = WellnessRules.EnrollmentState.NOT_SET_UP.wire,
    @SerialName("interval_min") val intervalMin: Int = WellnessRules.INTERVAL_DEFAULT,
    /** "auto" or a package name (the status screen's "Use heart rate from" override). */
    @SerialName("hr_origin_choice") val hrOriginChoice: String = "auto",
    /** The package "auto" resolved to at the last probe. */
    @SerialName("hr_origin_auto") val hrOriginAuto: String? = null,
    /** The device type and model the chosen origin's heart rate carried at the last probe. */
    @SerialName("hr_origin_device_type") val hrOriginDeviceType: String? = null,
    @SerialName("hr_origin_model") val hrOriginModel: String? = null,
    /** Notice ids already posted (pending_notices can repeat until the box sees them land). */
    @SerialName("posted_notice_ids") val postedNoticeIds: List<String> = emptyList(),
    /** A 401 was seen: stop until the user reconnects. */
    @SerialName("needs_reconnect") val needsReconnect: Boolean = false,
    @SerialName("backoff_until_ms") val backoffUntilMs: Long = 0L,
    @SerialName("quota_attempt") val quotaAttempt: Int = 0,
    @SerialName("last_push_ms") val lastPushMs: Long = 0L,
    @SerialName("last_periodic_run_ms") val lastPeriodicRunMs: Long = 0L,
    @SerialName("last_foreground_sync_ms") val lastForegroundSyncMs: Long = 0L,
    @SerialName("last_capabilities_ms") val lastCapabilitiesMs: Long = 0L,
    @SerialName("last_reconcile_ms") val lastReconcileMs: Long = 0L,
    /** Earliest record the phone could read (ms), for the "Past data: from ..." line. */
    @SerialName("earliest_available_ms") val earliestAvailableMs: Long? = null,
    val backfill: BackfillCursor = BackfillCursor(),
) {
    fun consent(): Map<WellnessCategory, WellnessConsent> =
        WellnessCategory.entries.associateWith { WellnessConsent.fromWire(categories[it.wire]) }

    fun enrollmentState(): WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.fromWire(state)

    /** The origin heart rate is read from: the user's override, else the probe's auto pick. */
    fun heartRateOrigin(): String? = if (hrOriginChoice != "auto") hrOriginChoice else hrOriginAuto
}

/** Backfill progress, newest to oldest in 7-day pages. */
@Serializable
data class BackfillCursor(
    /** Next page ends here (exclusive); null = not started. */
    @SerialName("cursor_ms") val cursorMs: Long? = null,
    /** Stop at this time (the earliest readable data). */
    @SerialName("floor_ms") val floorMs: Long? = null,
    @SerialName("pages_done") val pagesDone: Int = 0,
    @SerialName("pages_total") val pagesTotal: Int = 0,
    val done: Boolean = false,
)

class WellnessSyncStateStore(private val file: File) {
    private val lock = Any()

    fun load(): WellnessSyncState = synchronized(lock) {
        val raw = WellnessAtomic.readTextOrNull(file) ?: return WellnessSyncState()
        runCatching { JSON.decodeFromString<WellnessSyncState>(raw) }.getOrDefault(WellnessSyncState())
    }

    fun update(change: (WellnessSyncState) -> WellnessSyncState): WellnessSyncState = synchronized(lock) {
        val next = change(load())
        WellnessAtomic.writeText(file, JSON.encodeToString(next))
        next
    }

    fun clear() = synchronized(lock) { file.delete(); Unit }

    companion object {
        private val JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    }
}
