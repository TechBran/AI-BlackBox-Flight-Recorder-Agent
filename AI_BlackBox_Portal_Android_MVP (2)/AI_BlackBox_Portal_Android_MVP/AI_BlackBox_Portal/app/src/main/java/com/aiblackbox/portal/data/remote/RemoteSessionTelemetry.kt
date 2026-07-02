package com.aiblackbox.portal.data.remote

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * (M8.3) The telemetry SINK the action dispatcher records one step per actuated action into.
 * A tiny seam so [PhoneActionDispatcher] is unit-testable with a fake sink, and so the
 * production sink ([RemoteSessionTelemetry]) can be swapped without touching the dispatcher.
 *
 * ## No-secret contract (enforced by the signature)
 * The sink accepts ONLY non-sensitive fields — the action NAME ([action], a dispatch/variant name
 * like `tap` / `open_app` / `coordinate_swipe`), the outcome ([success]), the actuation
 * [latencyMs], and how the follow-on screen was observed ([captureType]: `screenshot` / `tree_only`
 * / `none`). It CANNOT be handed screen text, typed text, node content, coordinates, or any action
 * argument — so a telemetry export can never leak content.
 */
interface TelemetrySink {
    fun record(
        taskId: String,
        operator: String,
        action: String,
        success: Boolean,
        latencyMs: Long,
        captureType: String,
    )
}

/** A no-op [TelemetrySink] for tests / callers that don't record. */
object NoopTelemetrySink : TelemetrySink {
    override fun record(
        taskId: String,
        operator: String,
        action: String,
        success: Boolean,
        latencyMs: Long,
        captureType: String,
    ) { /* no-op */ }
}

/**
 * (M8.3 / I1) PERSISTENT, retention-bounded per-step telemetry for remote device-control sessions —
 * exposed via `GET /telemetry/{taskId}?operator=` (the steps of one task) and
 * `GET /telemetry/summary?operator=` (avg latency + success rate for an operator). Both are
 * OPERATOR-SCOPED at the HTTP boundary (authorize) AND here (every query filters by operator), so
 * one operator can never read another's telemetry.
 *
 * ## Persistent (survives an FGS restart) — plan 8.3
 * Backed by a small SQLite DB (a single table `remote_step`) once [init] wires the app context
 * (done at process start in `PortalApplication`). The DB file lives under the app's private
 * databases dir (`/data/data/<pkg>/databases/[DB_NAME]`) — an app-WRITABLE equivalent of the
 * plan's `Manifest/telemetry.db` (an installed app can't write the repo `Manifest/` dir). Because
 * it is on disk, the last N steps SURVIVE a foreground-service restart / process kill, so a
 * `/telemetry` read after a restart still returns the session's history.
 *
 * Until [init] is called (a fresh process before `PortalApplication.onCreate`, or a JVM unit test)
 * the sink falls back to an in-memory store using the SAME pure retention/query logic
 * ([TelemetryRetention]) — nothing is lost, it just isn't durable.
 *
 * ## No screen text / secrets — by construction
 * A [Step] holds ONLY: task id, operator, the action NAME, a success bool, the actuation latency,
 * the capture kind, and a timestamp. It NEVER holds screen text, typed text, node content,
 * coordinates, or action arguments — the [TelemetrySink] signature can't even carry them, and the
 * DB columns mirror the [Step] fields exactly. So a telemetry dump is safe to surface over the wire.
 *
 * ## Retention-bounded (7-day + hard cap)
 * On every write the store prunes rows older than [RETENTION_DAYS] days AND caps the table to the
 * newest [MAX_STEPS] rows overall (oldest-first eviction) — a long-lived foreground service (and its
 * on-disk DB) can't grow without bound. The prune cutoff + summary aggregation + operator-scope
 * filter are pure functions ([TelemetryRetention]) shared by the DB and the in-memory fallback, so
 * they are JVM-unit-tested directly; the thin SQLite [SqliteTelemetryStore] layer is verified on a
 * device / under Robolectric (plain-JVM unit tests have no SQLite engine).
 */
object RemoteSessionTelemetry : TelemetrySink {

    /** One recorded step. Serializes to the `GET /telemetry/{taskId}` wire shape; its fields are
     *  exactly the `remote_step` columns. */
    @Serializable
    data class Step(
        @SerialName("task_id") val taskId: String,
        val operator: String,
        val action: String,
        val success: Boolean,
        @SerialName("latency_ms") val latencyMs: Long,
        @SerialName("capture_type") val captureType: String,
        @SerialName("at_ms") val atMs: Long,
    )

    /** The aggregate for `GET /telemetry/summary?operator=`. */
    @Serializable
    data class Summary(
        val operator: String,
        @SerialName("step_count") val stepCount: Int,
        @SerialName("success_count") val successCount: Int,
        @SerialName("success_rate") val successRate: Double,
        @SerialName("avg_latency_ms") val avgLatencyMs: Long,
    )

    /** Total retained per-step records before oldest-first eviction (bounds the on-disk table). */
    internal const val MAX_STEPS = 512

    /** Retention window: rows older than this are pruned on every write (plan 8.3). */
    const val RETENTION_DAYS = 7L
    internal const val RETENTION_MS = RETENTION_DAYS * 24L * 60L * 60L * 1000L

    /** The persistent store, or the in-memory fallback until [init] wires a Context. Guarded by
     *  `this`. Volatile so a read on the listener's worker thread sees the initialized store. */
    @Volatile
    private var store: TelemetryStore = InMemoryTelemetryStore()

    /**
     * Wire the PERSISTENT SQLite store (idempotent). Called once at process start
     * (`PortalApplication.onCreate`) so telemetry survives an FGS restart. Safe to call before any
     * record. Never throws — a store-open failure leaves the in-memory fallback in place.
     */
    @Synchronized
    fun init(context: Context) {
        if (store is SqliteTelemetryStore) return
        store = try {
            SqliteTelemetryStore(context.applicationContext)
        } catch (e: Exception) {
            Log.w(TAG, "telemetry DB open failed (${e.javaClass.simpleName}); using in-memory fallback")
            InMemoryTelemetryStore()
        }
    }

    @Synchronized
    override fun record(
        taskId: String,
        operator: String,
        action: String,
        success: Boolean,
        latencyMs: Long,
        captureType: String,
    ) {
        if (taskId.isBlank()) return
        store.record(
            Step(
                taskId = taskId,
                operator = operator,
                action = action,
                success = success,
                latencyMs = latencyMs,
                captureType = captureType,
                atMs = System.currentTimeMillis(),
            ),
        )
    }

    /**
     * The steps recorded for [taskId] belonging to [operator] (OPERATOR-SCOPED — a mismatched
     * operator sees an empty list, never another operator's steps). Chronological order.
     */
    @Synchronized
    fun stepsFor(taskId: String, operator: String): List<Step> = store.stepsFor(taskId, operator)

    /** Aggregate for [operator]: step count, success count, success rate, avg latency (ms).
     *  Empty operator → an all-zero summary (never divides by zero). */
    @Synchronized
    fun summary(operator: String): Summary = store.summary(operator)

    /** TEST-ONLY reset so unit tests don't leak telemetry into one another. */
    @Synchronized
    internal fun resetForTest() { store = InMemoryTelemetryStore() }

    /** TEST-ONLY store injection (e.g. a fake persistent store to exercise reopen semantics). */
    @Synchronized
    internal fun useStoreForTest(s: TelemetryStore) { store = s }

    private const val TAG = "RemoteTelemetry"
}

/**
 * (M8.3 / I1) The storage seam behind [RemoteSessionTelemetry]. Two impls: the durable
 * [SqliteTelemetryStore] (production) and the [InMemoryTelemetryStore] (fallback / tests). Both
 * apply the SAME pure retention/query logic ([TelemetryRetention]).
 */
internal interface TelemetryStore {
    fun record(step: RemoteSessionTelemetry.Step)
    fun stepsFor(taskId: String, operator: String): List<RemoteSessionTelemetry.Step>
    fun summary(operator: String): RemoteSessionTelemetry.Summary
}

/**
 * (M8.3 / I1) PURE retention + query logic shared by both [TelemetryStore] impls, so the
 * prune-cutoff, operator-scope filter, and summary aggregation are framework-free and
 * JVM-unit-tested (the SQLite layer just wires SQL to these same rules).
 */
internal object TelemetryRetention {

    /** Rows with `at_ms` STRICTLY below this (older than [retentionMs]) are expired. */
    fun cutoffMs(nowMs: Long, retentionMs: Long = RemoteSessionTelemetry.RETENTION_MS): Long =
        nowMs - retentionMs

    /**
     * Apply retention to a chronological [steps] list: drop rows older than [retentionMs]
     * (as of [nowMs]) THEN keep only the newest [maxSteps] survivors (oldest-first eviction).
     */
    fun retain(
        steps: List<RemoteSessionTelemetry.Step>,
        nowMs: Long,
        maxSteps: Int = RemoteSessionTelemetry.MAX_STEPS,
        retentionMs: Long = RemoteSessionTelemetry.RETENTION_MS,
    ): List<RemoteSessionTelemetry.Step> {
        val cutoff = nowMs - retentionMs
        val fresh = steps.filter { it.atMs >= cutoff }
        return if (fresh.size <= maxSteps) fresh else fresh.takeLast(maxSteps)
    }

    /** OPERATOR-SCOPED task filter (a mismatched operator → empty). */
    fun filterStepsFor(
        steps: List<RemoteSessionTelemetry.Step>,
        taskId: String,
        operator: String,
    ): List<RemoteSessionTelemetry.Step> =
        steps.filter { it.taskId == taskId && it.operator == operator }

    /** Aggregate [operator]'s rows: count, success count, success rate, avg latency. Empty → zeros. */
    fun summaryOf(
        steps: List<RemoteSessionTelemetry.Step>,
        operator: String,
    ): RemoteSessionTelemetry.Summary {
        val mine = steps.filter { it.operator == operator }
        val n = mine.size
        val ok = mine.count { it.success }
        val avg = if (n == 0) 0L else mine.sumOf { it.latencyMs } / n
        val rate = if (n == 0) 0.0 else ok.toDouble() / n
        return RemoteSessionTelemetry.Summary(
            operator = operator, stepCount = n, successCount = ok,
            successRate = rate, avgLatencyMs = avg,
        )
    }
}

/**
 * (M8.3 / I1) In-memory [TelemetryStore] — the fallback used before [RemoteSessionTelemetry.init]
 * wires the DB, and the store JVM unit tests exercise. Applies [TelemetryRetention.retain] on every
 * write so its bounding/pruning matches the DB. NOT durable (lost on process death) — that is the
 * whole reason the production path uses [SqliteTelemetryStore]. Guarded by `this`.
 */
internal class InMemoryTelemetryStore : TelemetryStore {
    private var steps: List<RemoteSessionTelemetry.Step> = emptyList()

    @Synchronized
    override fun record(step: RemoteSessionTelemetry.Step) {
        steps = TelemetryRetention.retain(steps + step, System.currentTimeMillis())
    }

    @Synchronized
    override fun stepsFor(taskId: String, operator: String) =
        TelemetryRetention.filterStepsFor(steps, taskId, operator)

    @Synchronized
    override fun summary(operator: String) = TelemetryRetention.summaryOf(steps, operator)
}

/**
 * (M8.3 / I1) The DURABLE [TelemetryStore]: a single-table SQLite DB (via [SQLiteOpenHelper], no
 * Room dependency). One row per actuated step; the columns are EXACTLY the [RemoteSessionTelemetry.Step]
 * fields, so nothing sensitive can be stored (no screen/typed text, node content, coordinates, or
 * args). On every write it prunes rows older than [RemoteSessionTelemetry.RETENTION_DAYS] days and
 * caps the table to the newest [RemoteSessionTelemetry.MAX_STEPS] rows.
 *
 * THIN by design: the retention cutoff, operator-scope filter, and summary aggregation are the pure
 * [TelemetryRetention] functions (JVM-unit-tested). This class only maps them to SQL, so it needs a
 * device / Robolectric to verify (plain-JVM unit tests have no SQLite engine). Every DB call is
 * wrapped so a failure degrades to a benign no-op / empty read rather than crashing the listener.
 */
internal class SqliteTelemetryStore(context: Context) : TelemetryStore {

    private val helper = object : SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {
        override fun onCreate(db: SQLiteDatabase) {
            db.execSQL(
                "CREATE TABLE IF NOT EXISTS $TABLE (" +
                    "task_id TEXT NOT NULL, operator TEXT NOT NULL, action TEXT NOT NULL, " +
                    "success INTEGER NOT NULL, latency_ms INTEGER NOT NULL, " +
                    "capture_type TEXT NOT NULL, at_ms INTEGER NOT NULL)",
            )
            db.execSQL("CREATE INDEX IF NOT EXISTS idx_${TABLE}_operator ON $TABLE(operator)")
            db.execSQL("CREATE INDEX IF NOT EXISTS idx_${TABLE}_task ON $TABLE(task_id, operator)")
        }

        override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
            db.execSQL("DROP TABLE IF EXISTS $TABLE")
            onCreate(db)
        }
    }

    init {
        // Prune once on open too (retention applies even if no new write arrives this session).
        runCatching { prune(helper.writableDatabase, System.currentTimeMillis()) }
    }

    override fun record(step: RemoteSessionTelemetry.Step) {
        try {
            val db = helper.writableDatabase
            val cv = ContentValues().apply {
                put("task_id", step.taskId)
                put("operator", step.operator)
                put("action", step.action)
                put("success", if (step.success) 1 else 0)
                put("latency_ms", step.latencyMs)
                put("capture_type", step.captureType)
                put("at_ms", step.atMs)
            }
            db.insert(TABLE, null, cv)
            prune(db, step.atMs)
        } catch (e: Exception) {
            Log.w(TAG, "telemetry insert failed (${e.javaClass.simpleName})")
        }
    }

    /** 7-day retention + hard cap. [nowMs] anchors the cutoff (the just-written step's time). */
    private fun prune(db: SQLiteDatabase, nowMs: Long) {
        db.delete(TABLE, "at_ms < ?", arrayOf(TelemetryRetention.cutoffMs(nowMs).toString()))
        // Keep only the newest MAX_STEPS rows overall (MAX_STEPS is an internal int constant — no
        // injection risk from inlining it into the LIMIT).
        db.execSQL(
            "DELETE FROM $TABLE WHERE rowid NOT IN " +
                "(SELECT rowid FROM $TABLE ORDER BY at_ms DESC, rowid DESC " +
                "LIMIT ${RemoteSessionTelemetry.MAX_STEPS})",
        )
    }

    override fun stepsFor(taskId: String, operator: String): List<RemoteSessionTelemetry.Step> =
        query("task_id = ? AND operator = ?", arrayOf(taskId, operator))

    override fun summary(operator: String): RemoteSessionTelemetry.Summary =
        TelemetryRetention.summaryOf(query("operator = ?", arrayOf(operator)), operator)

    /** Read rows matching [where] into [RemoteSessionTelemetry.Step]s, chronological. Never throws. */
    private fun query(where: String, args: Array<String>): List<RemoteSessionTelemetry.Step> {
        val out = ArrayList<RemoteSessionTelemetry.Step>()
        try {
            val db = helper.readableDatabase
            db.query(
                TABLE,
                arrayOf("task_id", "operator", "action", "success", "latency_ms", "capture_type", "at_ms"),
                where, args, null, null, "at_ms ASC, rowid ASC",
            ).use { c ->
                while (c.moveToNext()) {
                    out.add(
                        RemoteSessionTelemetry.Step(
                            taskId = c.getString(0),
                            operator = c.getString(1),
                            action = c.getString(2),
                            success = c.getInt(3) != 0,
                            latencyMs = c.getLong(4),
                            captureType = c.getString(5),
                            atMs = c.getLong(6),
                        ),
                    )
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "telemetry query failed (${e.javaClass.simpleName})")
        }
        return out
    }

    companion object {
        private const val TAG = "RemoteTelemetryDb"

        /** App-private DB file (`/data/data/<pkg>/databases/[DB_NAME]`) — survives FGS restart. */
        const val DB_NAME = "blackbox_remote_telemetry.db"
        const val DB_VERSION = 1
        const val TABLE = "remote_step"
    }
}
