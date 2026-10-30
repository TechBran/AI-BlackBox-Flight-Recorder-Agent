package com.aiblackbox.portal.data.wellness

import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.Log
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.aiblackbox.portal.BlackBoxNotificationManager
import com.aiblackbox.portal.NotificationIds
import com.aiblackbox.portal.overlay.AppForegroundState
import java.util.concurrent.TimeUnit

/**
 * When the collector runs (spec §3.2, "Scheduling"; decision D17):
 *  - `wellness-sync`: unique periodic work every 60 minutes (flex 15, network required;
 *    adjustable 15-120);
 *  - an expedited one-off when the app comes to the foreground (debounced to 10 minutes),
 *    on a box nudge, on "Sync now", on enrollment and on a permission change;
 *  - `wellness-backfill`: the history import, a VISIBLE dataSync foreground worker with a
 *    progress notification. It never runs headless: if Android refuses the foreground start,
 *    it stops and the next app open resumes it from the persisted cursor.
 *
 * Without READ_HEALTH_DATA_IN_BACKGROUND the engine itself refuses background reads
 * (foreground-only mode), so the periodic worker then only drains the outbox.
 */
object WellnessScheduler {

    const val PERIODIC = "wellness-sync"
    const val NOW = "wellness-sync-now"
    const val BACKFILL = "wellness-backfill"
    const val TAG_PERIODIC = "wellness-periodic"
    const val KEY_FOREGROUND = "foreground"

    private val network = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    fun schedulePeriodic(context: Context, intervalMin: Int = WellnessRules.INTERVAL_DEFAULT) {
        val minutes = WellnessRules.clampInterval(intervalMin).toLong()
        val request = PeriodicWorkRequestBuilder<WellnessSyncWorker>(
            minutes, TimeUnit.MINUTES, WellnessRules.INTERVAL_FLEX.toLong(), TimeUnit.MINUTES,
        ).setConstraints(network).addTag(TAG_PERIODIC).build()
        runCatching {
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE, request)
        }
    }

    fun syncNow(context: Context, foreground: Boolean) {
        val request = OneTimeWorkRequestBuilder<WellnessSyncWorker>()
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .setConstraints(network)
            .setInputData(workDataOf(KEY_FOREGROUND to foreground))
            .build()
        runCatching { WorkManager.getInstance(context).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, request) }
    }

    /** Start (or resume) the history import; [delayMs] > 0 after a quota back-off. */
    fun startBackfill(context: Context, delayMs: Long = 0L) {
        val builder = OneTimeWorkRequestBuilder<WellnessBackfillWorker>().setConstraints(network)
        if (delayMs > 0) builder.setInitialDelay(delayMs, TimeUnit.MILLISECONDS)
        val policy = if (delayMs > 0) ExistingWorkPolicy.APPEND_OR_REPLACE else ExistingWorkPolicy.KEEP
        runCatching { WorkManager.getInstance(context).enqueueUniqueWork(BACKFILL, policy, builder.build()) }
    }

    fun cancelAll(context: Context) {
        runCatching {
            val wm = WorkManager.getInstance(context)
            wm.cancelUniqueWork(PERIODIC)
            wm.cancelUniqueWork(NOW)
            wm.cancelUniqueWork(BACKFILL)
        }
    }

    /** A bind landed: first sync, the periodic job, and the history import. */
    fun onEnrolled(context: Context) {
        val st = WellnessGraph.state(context).load()
        schedulePeriodic(context, st.intervalMin)
        syncNow(context, foreground = AppForegroundState.isForeground())
        startBackfill(context)
    }

    /**
     * The app came to the foreground: an expedited sync at most every 10 minutes, and resume
     * an unfinished backfill (it may have been refused a foreground start while backgrounded).
     */
    fun onAppForeground(context: Context, nowMs: Long = System.currentTimeMillis()) {
        val app = context.applicationContext
        if (WellnessGraph.binding(app).current() == null) return
        val store = WellnessGraph.state(app)
        val st = store.load()
        if (WellnessRules.shouldSyncOnForeground(st.lastForegroundSyncMs, nowMs)) {
            store.update { it.copy(lastForegroundSyncMs = nowMs) }
            syncNow(app, foreground = true)
        }
        if (!st.backfill.done) startBackfill(app)
    }
}

/** The incremental sync: Changes API -> outbox -> box. */
class WellnessSyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext
        val engine = WellnessGraph.engine(app) ?: return Result.success()
        if (WellnessScheduler.TAG_PERIODIC in tags) {
            WellnessGraph.state(app).update { it.copy(lastPeriodicRunMs = System.currentTimeMillis()) }
        }
        val foreground = inputData.getBoolean(WellnessScheduler.KEY_FOREGROUND, false) || AppForegroundState.isForeground()
        return try {
            val report = engine.sync(inForeground = foreground)
            Log.d(TAG, "sync worker: ${report.outcome}")
            runCatching { WellnessGraph.pushCapabilities(app) }
            Result.success()
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "sync worker failed (${e.javaClass.simpleName})")
            Result.retry()
        }
    }

    /** Expedited work on Android 11 and lower runs as a foreground service; say so quietly. */
    override suspend fun getForegroundInfo(): ForegroundInfo =
        wellnessForegroundInfo(applicationContext, NotificationIds.WELLNESS_SYNC, "Syncing health data", null)

    private companion object {
        const val TAG = "WellnessSyncWorker"
    }
}

/** The history import, newest to oldest, always with a visible progress notification. */
class WellnessBackfillWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext
        val engine = WellnessGraph.engine(app) ?: return Result.success()
        // W36: no hidden reads. If Android will not let this run as a visible foreground
        // worker right now, stop; the next app open resumes from the cursor.
        val visible = runCatching {
            setForeground(wellnessForegroundInfo(app, NotificationIds.WELLNESS_BACKFILL, "Importing health history", 0))
        }.isSuccess
        if (!visible) {
            Log.w(TAG, "backfill could not run in the foreground; waiting for the next app open")
            return Result.success()
        }
        while (!isStopped) {
            when (val step = engine.backfillPage()) {
                is WellnessSyncEngine.BackfillStep.Progress -> {
                    val pct = if (step.pagesTotal > 0) (step.pagesDone * 100 / step.pagesTotal).coerceIn(0, 99) else 0
                    runCatching {
                        setForeground(wellnessForegroundInfo(app, NotificationIds.WELLNESS_BACKFILL,
                            "Importing health history $pct%", pct))
                    }
                    setProgress(workDataOf("pages_done" to step.pagesDone, "pages_total" to step.pagesTotal))
                }
                WellnessSyncEngine.BackfillStep.Done -> return Result.success()
                is WellnessSyncEngine.BackfillStep.Skipped -> {
                    Log.d(TAG, "backfill skipped (${step.reason})")
                    return Result.success()
                }
                is WellnessSyncEngine.BackfillStep.QuotaBackoff -> {
                    WellnessScheduler.startBackfill(app, step.delayMs)
                    return Result.success()
                }
            }
        }
        return Result.retry()
    }

    override suspend fun getForegroundInfo(): ForegroundInfo =
        wellnessForegroundInfo(applicationContext, NotificationIds.WELLNESS_BACKFILL, "Importing health history", 0)

    private companion object {
        const val TAG = "WellnessBackfill"
    }
}

/** A dataSync ForegroundInfo on the private wellness channel. No health values in the text. */
internal fun wellnessForegroundInfo(context: Context, id: Int, title: String, progressPct: Int?): ForegroundInfo {
    val notification = BlackBoxNotificationManager(context).buildWellnessProgress(title, progressPct)
    return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
        ForegroundInfo(id, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
    } else {
        ForegroundInfo(id, notification)
    }
}
