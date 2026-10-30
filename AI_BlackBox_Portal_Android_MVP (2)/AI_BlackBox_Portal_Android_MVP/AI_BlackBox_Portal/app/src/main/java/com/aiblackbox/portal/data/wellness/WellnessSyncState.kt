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
    /** The source phone's label as the last credentialed status gave it (the chat footer's source). */
    @SerialName("source_label") val sourceLabel: String? = null,
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
    /**
     * A narrowing the person asked for on THIS phone (Pause, Disconnect, Stop & erase) that
     * the box has not confirmed yet: nothing is read or sent meanwhile, whatever the box's
     * view says. Null when there is none. Values: [WellnessRules.HOLD_PAUSE], [WellnessRules.HOLD_DISCONNECT],
     * [WellnessRules.HOLD_ERASE].
     */
    @SerialName("local_hold") val localHold: String? = null,
    /** When the box's view of consent and state was last read by the collector (signed status). */
    @SerialName("last_status_ms") val lastStatusMs: Long = 0L,
    @SerialName("backoff_until_ms") val backoffUntilMs: Long = 0L,
    @SerialName("quota_attempt") val quotaAttempt: Int = 0,
    @SerialName("last_push_ms") val lastPushMs: Long = 0L,
    @SerialName("last_periodic_run_ms") val lastPeriodicRunMs: Long = 0L,
    @SerialName("last_foreground_sync_ms") val lastForegroundSyncMs: Long = 0L,
    @SerialName("last_capabilities_ms") val lastCapabilitiesMs: Long = 0L,
    /** The health permissions held when the last capability report went out (a change re-sends it). */
    @SerialName("last_reported_grants") val lastReportedGrants: List<String> = emptyList(),
    @SerialName("last_reconcile_ms") val lastReconcileMs: Long = 0L,
    /** Earliest record the phone could read (ms), for the "Past data: from ..." line. */
    @SerialName("earliest_available_ms") val earliestAvailableMs: Long? = null,
    val backfill: BackfillCursor = BackfillCursor(),
    // ── M7.1 probe evidence (values-free: times of runs, counts, latencies) ────────────
    /** The last periodic worker runs (spec T-1 / T-9: does One UI defer it overnight?). */
    @SerialName("run_log") val runLog: List<RunLogEntry> = emptyList(),
    /** Deletion changes seen from Health Connect (T-8: does auto-delete emit them?). */
    @SerialName("deletions_seen") val deletionsSeen: Int = 0,
    /** Minutes between a sleep session's end and the phone first seeing it (T-11). */
    @SerialName("sleep_latency_min") val sleepLatencyMin: List<Long> = emptyList(),
    /** When the box last nudged this phone (T-9: does a nudge reach a dozing phone?). */
    @SerialName("last_nudge_ms") val lastNudgeMs: Long = 0L,
    /**
     * The AI choices setup made on a phone whose enrollment waits for the old phone's approval
     * ([WellnessRules.EnrollmentState.REPLACE_PENDING]). The box takes no settings from this
     * phone until then, and the approval keeps the OLD enrollment's; so they wait here and go in
     * one signed PUT once this phone's own signed status is the credentialed one
     * ([WellnessRules.pendingSettingsBody]), then are cleared. Null when there are none.
     */
    @SerialName("pending_settings") val pendingSettings: PendingSetupSettings? = null,
    /**
     * The privacy policy version the person agreed to: the enroll reply's at enrollment, then the
     * one of the text they agreed to on the "The privacy policy changed" card. Null in a state
     * written before it was kept ([WellnessRules.LEGACY_POLICY_VERSION]).
     */
    @SerialName("policy_version_acked") val policyVersionAcked: String? = null,
    /**
     * The newest version the box named (a credentialed status, an enroll reply, an
     * X-Policy-Version header). While it is not the agreed one the collector holds
     * ([WellnessRules.policyHold]); kept here so the hold survives a restart.
     */
    @SerialName("policy_version_box") val policyVersionBox: String? = null,
) {
    fun consent(): Map<WellnessCategory, WellnessConsent> =
        WellnessCategory.entries.associateWith { WellnessConsent.fromWire(categories[it.wire]) }

    fun enrollmentState(): WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.fromWire(state)

    /** The origin heart rate is read from: the user's override, else the probe's auto pick. */
    fun heartRateOrigin(): String? = if (hrOriginChoice != "auto") hrOriginChoice else hrOriginAuto
}

/** The setup's AI choices waiting for a replacement's approval (wire ids; no health values). */
@Serializable
data class PendingSetupSettings(
    @SerialName("local_only") val localOnly: Map<String, Boolean> = emptyMap(),
    @SerialName("providers_allowed") val providersAllowed: List<String> = emptyList(),
    @SerialName("google_paid_key_confirmed") val googlePaidKeyConfirmed: Boolean = false,
)

/** One periodic run: when, whether the screen was on, and how it ended (no data). */
@Serializable
data class RunLogEntry(
    @SerialName("at_ms") val atMs: Long,
    val interactive: Boolean,
    val outcome: String,
) {
    companion object {
        const val MAX = 96

        fun append(log: List<RunLogEntry>, entry: RunLogEntry): List<RunLogEntry> = (log + entry).takeLast(MAX)
    }
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

/**
 * Read-modify-write access to `sync_state.json`. The listener threads, the view model and
 * three workers all update it, through more than one instance; the lock is therefore shared
 * PER FILE across the process ([lockFor]), never per instance, so no update is lost and no
 * two writes interleave.
 */
class WellnessSyncStateStore(private val file: File) {
    private val lock: Any = lockFor(file)

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
        private val LOCKS = java.util.concurrent.ConcurrentHashMap<String, Any>()

        /** One lock per state file for the whole process. */
        internal fun lockFor(file: File): Any = LOCKS.computeIfAbsent(file.absoluteFile.normalize().path) { Any() }
    }
}
