package com.aiblackbox.portal.data.wellness

import android.util.Log
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.json.JsonObject
import java.time.ZoneId

/**
 * The collector (spec §3.2): Health Connect -> buckets, sessions, readings -> outbox -> box.
 * Driven by WellnessSyncWorker (incremental, hourly and on demand) and WellnessBackfillWorker
 * (history, newest to oldest), and JVM-tested end to end over the fake gateway.
 *
 * Commit order, always: read -> write the outbox (fsync) -> advance the token or cursor ->
 * push. A crash anywhere repeats at worst a batch the box drops by batch id and HC id.
 *
 * One run at a time per process ([mutex]): the periodic worker, "Sync now" and a box nudge
 * can all land together.
 *
 * Logs carry counts only, never a value, a key or a payload.
 */
class WellnessSyncEngine(
    private val gateway: HealthConnectGateway,
    private val binding: WellnessBinding,
    private val tokens: ChangesTokenStore,
    private val dayIndex: DayIndex,
    private val outbox: WellnessOutbox,
    private val state: WellnessSyncStateStore,
    private val send: suspend (ByteArray, OriginSigner) -> WellnessOutbox.SendResult,
    private val clock: () -> Long = System::currentTimeMillis,
    private val zone: () -> ZoneId = { ZoneId.systemDefault() },
    private val newBatchId: () -> String = ::randomBatchId,
    private val onNotices: (List<JsonObject>) -> Unit = {},
    private val onStopped: (status: Int, error: String?) -> Unit = { _, _ -> },
) {

    data class SyncReport(
        val outcome: String,
        val batchesQueued: Int = 0,
        val rowsQueued: Int = 0,
        val batchesSent: Int = 0,
        val pending: Int = 0,
    )

    sealed class BackfillStep {
        data class Progress(val pagesDone: Int, val pagesTotal: Int, val rowsQueued: Int) : BackfillStep()
        data object Done : BackfillStep()
        data class Skipped(val reason: String) : BackfillStep()
        data class QuotaBackoff(val delayMs: Long) : BackfillStep()
    }

    // ── incremental sync ───────────────────────────────────────────────────────────────────

    suspend fun sync(inForeground: Boolean): SyncReport = RUN_LOCK.withLock {
        val record = binding.current() ?: return@withLock SyncReport("not_enrolled")
        val now = clock()
        val st = state.load()
        val sdk = gateway.sdkStatus()
        val granted = if (sdk == HcSdkStatus.AVAILABLE) runCatching { gateway.grantedPermissions() }
            .getOrDefault(emptySet()) else emptySet()
        val readable = WellnessRules.readableTypes(st.consent(), granted)
        val decision = WellnessRules.maySync(
            WellnessRules.SyncInputs(
                bound = true,
                state = st.enrollmentState().let {
                    if (it == WellnessRules.EnrollmentState.NOT_SET_UP) WellnessRules.EnrollmentState.ACTIVE else it
                },
                sdk = sdk,
                grantedReadTypes = readable.size,
                inForeground = inForeground,
                backgroundGranted = WellnessCatalog.PERMISSION_BACKGROUND in granted,
                stopped = st.needsReconnect,
                outboxFull = outbox.isFull(),
                backoffUntilMs = st.backoffUntilMs,
                nowMs = now,
            ),
        )
        if (decision is WellnessRules.SyncDecision.Skip) {
            Log.d(TAG, "sync skipped (${decision.reason})")
            return@withLock SyncReport("skipped:${decision.reason}", pending = outbox.size())
        }
        var queued = 0
        var rows = 0
        if ((decision as WellnessRules.SyncDecision.Run).collect) {
            try {
                val (b, r) = collectIncremental(record, readable, st, now)
                queued = b
                rows = r
                state.update { it.copy(quotaAttempt = 0) }
            } catch (e: HcQuotaException) {
                backOff()
            } catch (e: OutboxFullException) {
                Log.w(TAG, "outbox full; tokens held until it drains")
            } catch (e: HcPermissionException) {
                Log.w(TAG, "a health permission was withdrawn mid-run")
            }
        }
        val drained = drain(record)
        Log.i(TAG, "sync: queued=$queued rows=$rows sent=${drained.batchesSent} pending=${outbox.size()}")
        drained.copy(batchesQueued = queued, rowsQueued = rows)
    }

    private suspend fun collectIncremental(
        record: WellnessBindingRecord,
        readable: List<HcType>,
        st: WellnessSyncState,
        now: Long,
    ): Pair<Int, Int> {
        val z = zone()
        val upserts = mutableListOf<HcRecord>()
        val deletions = mutableListOf<Pair<HcType, String>>()
        val nextTokens = linkedMapOf<HcType, String>()
        val reReadFrom = linkedMapOf<HcType, Long>()
        var windowFrom = now

        for (type in readable) {
            val entry = tokens.get(type)
            if (entry == null) {
                // First sight of this type (consent or a grant just widened): take a token now
                // and let the backfill cover what came before it.
                tokens.seed(type, gateway.changesToken(type), now)
                continue
            }
            windowFrom = minOf(windowFrom, entry.syncedUntilMs)
            if (tokens.isExpired(entry, now)) {
                reReadFrom[type] = tokens.reReadFromMs(entry)
                nextTokens[type] = gateway.changesToken(type)
                continue
            }
            var token = entry.token
            var expired = false
            var guard = 0
            while (guard++ < MAX_CHANGE_PAGES) {
                val changes = gateway.getChanges(token)
                if (changes.tokenExpired) {
                    expired = true
                    break
                }
                for (c in changes.changes) when (c) {
                    is HcChange.Upsert -> if (c.record.type == type) upserts += c.record
                    is HcChange.Deletion -> deletions += type to c.id
                }
                token = changes.nextToken
                if (!changes.hasMore) break
            }
            if (expired) {
                reReadFrom[type] = tokens.reReadFromMs(entry)
                nextTokens[type] = gateway.changesToken(type)
            } else {
                nextTokens[type] = token
            }
        }

        // Probe evidence (T-11): how long after a night ends does it first reach this phone?
        val sleepLatencies = upserts.filterIsInstance<HcRecord.Sleep>()
            .filter { dayIndex.lookup(it.meta.id) == null }
            .map { ((now - it.endMs) / 60_000L).coerceAtLeast(0L) }
        val work = DayWork(z)
        for (r in upserts) work.addRecord(r)
        for ((type, id) in deletions) {
            work.sessions.remove(id)
            work.readings.remove(id)
            when (val res = dayIndex.resolveDeletion(id)) {
                is DayIndex.Resolution.Known -> {
                    work.markDay(res.entry.type, res.entry.day)
                    if (res.entry.type.shape != HcShape.BUCKETED) work.deleted += id
                }
                is DayIndex.Resolution.Unknown -> {
                    if (type.shape == HcShape.BUCKETED) {
                        BucketBuilder.trailingDays(now, DayIndex.UNKNOWN_REAGGREGATE_DAYS, z).forEach { work.markDay(type, it) }
                    } else {
                        work.deleted += id
                    }
                }
            }
        }
        for ((type, from) in reReadFrom) {
            if (type.shape == HcShape.BUCKETED) {
                BucketBuilder.daysTouched(from, now, z).forEach { work.markDay(type, it) }
            } else {
                readAll(type, from, now).forEach { work.addRecord(it) }
            }
            windowFrom = minOf(windowFrom, from)
        }
        val reconcile = now - st.lastReconcileMs >= RECONCILE_EVERY_MS && st.lastReconcileMs > 0L
        if (reconcile) {
            for (type in readable.filter { it.shape == HcShape.BUCKETED }) {
                BucketBuilder.trailingDays(now, DayIndex.RECONCILE_DAYS, z).forEach { work.markDay(type, it) }
            }
        }

        val batches = buildBatches(record, readable, st, work, IngestPayloadBuilder.Window("changes", windowFrom, now))
        var last: OutboxReceipt? = null
        for (b in batches) last = outbox.enqueue(IngestPayloadBuilder.toJson(b))
        dayIndex.pruneBucketed(BucketBuilder.trailingDays(now, DayIndex.BUCKETED_KEEP_DAYS, z).first())
        dayIndex.save()
        val proof = last?.let { CommitProof.Committed(it) } ?: CommitProof.NothingToCommit
        for ((type, token) in nextTokens) tokens.advance(type, token, now, proof)
        state.update {
            it.copy(
                lastReconcileMs = if (reconcile || it.lastReconcileMs == 0L) now else it.lastReconcileMs,
                deletionsSeen = it.deletionsSeen + deletions.size,
                sleepLatencyMin = (it.sleepLatencyMin + sleepLatencies).takeLast(MAX_LATENCIES),
            )
        }
        return batches.size to batches.sumOf { it.rowCount }
    }

    // ── backfill ───────────────────────────────────────────────────────────────────────────

    /**
     * One 7-day page of history, newest first. Tokens are taken for every readable type
     * BEFORE the first page, so edits made during a long backfill are caught by the next
     * incremental run. The cursor moves only after the page's batches are in the outbox.
     */
    suspend fun backfillPage(): BackfillStep = RUN_LOCK.withLock {
        val record = binding.current() ?: return@withLock BackfillStep.Skipped("not_enrolled")
        val now = clock()
        val z = zone()
        val st = state.load()
        if (st.enrollmentState() == WellnessRules.EnrollmentState.PAUSED) return@withLock BackfillStep.Skipped("paused")
        if (st.backfill.done) return@withLock BackfillStep.Done
        if (now < st.backoffUntilMs) return@withLock BackfillStep.QuotaBackoff(st.backoffUntilMs - now)
        if (gateway.sdkStatus() != HcSdkStatus.AVAILABLE) return@withLock BackfillStep.Skipped("hc_unavailable")
        val granted = runCatching { gateway.grantedPermissions() }.getOrDefault(emptySet())
        val readable = WellnessRules.readableTypes(st.consent(), granted)
        if (readable.isEmpty()) return@withLock BackfillStep.Skipped("no_permissions")
        try {
            for (type in readable) if (tokens.get(type) == null) tokens.seed(type, gateway.changesToken(type), now)

            val today = BucketBuilder.localDay(now, z)
            val cursor = st.backfill.cursorMs ?: BucketBuilder.dayRange(today, z).second
            val floor = st.backfill.floorMs ?: computeFloor(readable, granted, now, z)
            if (cursor <= floor) {
                state.update { it.copy(backfill = it.backfill.copy(done = true)) }
                return@withLock BackfillStep.Done
            }
            val total = st.backfill.pagesTotal.takeIf { it > 0 }
                ?: (((cursor - floor) + PAGE_MS - 1) / PAGE_MS).toInt().coerceAtLeast(1)
            // Seven whole local days (a DST change makes the page 167 or 169 hours).
            val pageStart = maxOf(BucketBuilder.dayRange(BucketBuilder.localDay(cursor - PAGE_MS, z), z).first, floor)

            val work = DayWork(z)
            for (type in readable.filter { it.shape != HcShape.BUCKETED }) {
                readAll(type, pageStart, cursor).forEach { work.addRecord(it) }
            }
            for (day in BucketBuilder.daysTouched(pageStart, cursor, z)) {
                for (type in readable.filter { it.shape == HcShape.BUCKETED }) work.markDay(type, day)
            }
            val batches = buildBatches(record, readable, st, work, IngestPayloadBuilder.Window("backfill", pageStart, cursor))
            for (b in batches) outbox.enqueue(IngestPayloadBuilder.toJson(b))
            dayIndex.save()
            val done = pageStart <= floor
            val next = state.update {
                it.copy(
                    quotaAttempt = 0,
                    earliestAvailableMs = floor,
                    backfill = BackfillCursor(
                        cursorMs = pageStart, floorMs = floor, pagesDone = it.backfill.pagesDone + 1,
                        pagesTotal = total, done = done,
                    ),
                )
            }
            Log.i(TAG, "backfill page ${next.backfill.pagesDone}/$total: batches=${batches.size}")
            drain(record)
            if (done) BackfillStep.Done
            else BackfillStep.Progress(next.backfill.pagesDone, total, batches.sumOf { it.rowCount })
        } catch (e: HcQuotaException) {
            BackfillStep.QuotaBackoff(backOff())
        }
    }

    /** The earliest readable data across [readable] (bounded), aligned to a local day start. */
    private suspend fun computeFloor(readable: List<HcType>, granted: Set<String>, now: Long, z: ZoneId): Long {
        val hardFloor = now - MAX_HISTORY_MS
        val window = if (WellnessCatalog.PERMISSION_HISTORY in granted) hardFloor else now - THIRTY_DAYS_MS
        var earliest: Long? = null
        for (type in readable) {
            val first = runCatching { gateway.readRecords(type, window, now, pageSize = 1, ascending = true) }
                .getOrNull()?.records?.firstOrNull()?.startMs ?: continue
            earliest = minOf(earliest ?: first, first)
        }
        val start = (earliest ?: (now - THIRTY_DAYS_MS)).coerceAtLeast(window)
        return BucketBuilder.dayRange(BucketBuilder.localDay(start, z), z).first
    }

    // ── shared ─────────────────────────────────────────────────────────────────────────────

    /** Records, dirty days and deletions gathered for one run. */
    private inner class DayWork(val zone: ZoneId) {
        val heartDays = sortedSetOf<String>()
        val activityDays = sortedSetOf<String>()
        val sessions = LinkedHashMap<String, HcRecord>()
        val readings = LinkedHashMap<String, HcRecord.Reading>()
        val deleted = linkedSetOf<String>()

        fun addRecord(r: HcRecord) {
            dayIndex.record(r.meta.id, r.type, BucketBuilder.localDay(r.startMs, zone))
            when (r) {
                is HcRecord.Sleep, is HcRecord.Exercise -> sessions[r.meta.id] = r
                is HcRecord.Reading -> readings[r.meta.id] = r
                else -> Unit
            }
            for (day in BucketBuilder.daysTouched(r.startMs, r.endMs, zone)) markDay(r.type, day)
        }

        fun markDay(type: HcType, day: String) {
            when (type) {
                // An exercise session also owns the day's 1-minute heart-rate rows.
                HcType.HEART_RATE, HcType.EXERCISE -> heartDays += day
                else -> if (type in BucketBuilder.ACTIVITY_KEYS) activityDays += day
            }
        }
    }

    private suspend fun buildBatches(
        record: WellnessBindingRecord,
        readable: List<HcType>,
        st: WellnessSyncState,
        work: DayWork,
        window: IngestPayloadBuilder.Window,
    ): List<IngestPayloadBuilder.Batch> {
        val z = work.zone
        val hrPackage = st.heartRateOrigin()
        val hrOrigin = hrPackage?.let { HcOrigin(it, st.hrOriginDeviceType, st.hrOriginModel) }
        val activityTypes = readable.filter { it in BucketBuilder.ACTIVITY_KEYS }
        val exerciseCache = HashMap<String, List<HcRecord.Exercise>>()

        suspend fun exercisesOn(day: String): List<HcRecord.Exercise> = exerciseCache.getOrPut(day) {
            if (HcType.EXERCISE !in readable) return@getOrPut emptyList()
            val (ds, de) = BucketBuilder.dayRange(day, z)
            readAll(HcType.EXERCISE, ds - BucketBuilder.DAY_MS, de).filterIsInstance<HcRecord.Exercise>()
                .filter { it.endMs > ds && it.startMs < de }
        }

        val bucketsByDay = sortedMapOf<String, MutableList<IngestPayloadBuilder.Bucket>>()
        if (HcType.HEART_RATE in readable) {
            for (day in work.heartDays) {
                val (ds, de) = BucketBuilder.dayRange(day, z)
                val five = gateway.aggregateByDuration(BucketBuilder.HR_KEYS, ds, de, BucketBuilder.FIVE_MIN_MS, hrPackage)
                val one = BucketBuilder.exerciseWindows(exercisesOn(day), ds, de).flatMap { (a, b) ->
                    gateway.aggregateByDuration(BucketBuilder.HR_KEYS, a, b, BucketBuilder.MINUTE_MS, hrPackage)
                }
                bucketsByDay.getOrPut(day) { mutableListOf() } += BucketBuilder.heartRateBuckets(day, hrOrigin, five, one)
            }
        }
        if (activityTypes.isNotEmpty()) {
            val keys = activityTypes.map { BucketBuilder.ACTIVITY_KEYS.getValue(it) }.toSet()
            for (day in work.activityDays) {
                val (ds, de) = BucketBuilder.dayRange(day, z)
                val hourly = gateway.aggregateByDuration(keys, ds, de, BucketBuilder.HOUR_MS, null)
                bucketsByDay.getOrPut(day) { mutableListOf() } += BucketBuilder.activityBuckets(day, hourly, activityTypes)
            }
        }

        val exerciseKeys = BucketBuilder.exerciseAggKeys(readable)
        val sessions = work.sessions.values.sortedWith(compareBy({ it.startMs }, { it.meta.id })).map { r ->
            when (r) {
                is HcRecord.Sleep -> BucketBuilder.sleepSession(r)
                is HcRecord.Exercise -> BucketBuilder.exerciseSession(
                    r, if (exerciseKeys.isEmpty()) emptyMap() else gateway.aggregate(exerciseKeys, r.startMs, r.endMs, null),
                )
                else -> error("not a session")
            }
        }
        val readings = work.readings.values.sortedWith(compareBy({ it.timeMs }, { it.meta.id })).map(BucketBuilder::reading)
        val deleted = work.deleted.toList()

        // Chunk: at most DAYS_PER_BATCH days of buckets and ITEMS_PER_BATCH sessions + readings
        // per batch; a 413 on the box splits further.
        val dayChunks = bucketsByDay.values.toList().chunked(DAYS_PER_BATCH).map { it.flatten() }
        val sessionChunks = sessions.chunked(ITEMS_PER_BATCH)
        val readingChunks = readings.chunked(ITEMS_PER_BATCH)
        val deletedChunks = deleted.chunked(ITEMS_PER_BATCH)
        val n = maxOf(dayChunks.size, sessionChunks.size, readingChunks.size, deletedChunks.size)
        return (0 until n).map { i ->
            IngestPayloadBuilder.Batch(
                batchId = newBatchId(),
                enrollmentId = record.enrollmentId,
                epoch = record.epoch,
                window = window,
                buckets = dayChunks.getOrElse(i) { emptyList() },
                sessions = sessionChunks.getOrElse(i) { emptyList() },
                readings = readingChunks.getOrElse(i) { emptyList() },
                deletedIds = deletedChunks.getOrElse(i) { emptyList() },
            )
        }.filterNot { it.isEmpty }
    }

    private suspend fun readAll(type: HcType, startMs: Long, endMs: Long): List<HcRecord> {
        val out = mutableListOf<HcRecord>()
        var token: String? = null
        var pages = 0
        do {
            val page = gateway.readRecords(type, startMs, endMs, token)
            out += page.records
            token = page.nextPageToken
        } while (token != null && ++pages < MAX_READ_PAGES)
        return out
    }

    /** Push the outbox; react to the box's answers. */
    private suspend fun drain(record: WellnessBindingRecord): SyncReport {
        val signer = OriginSigner(record.enrollmentId, record.k)
        val result = outbox.drain(send = { bytes -> send(bytes, signer) }, newBatchId = newBatchId)
        if (result.notices.isNotEmpty()) runCatching { onNotices(result.notices) }
        val now = clock()
        return when (result) {
            is WellnessOutbox.DrainResult.Drained -> {
                if (result.sent > 0) state.update { it.copy(lastPushMs = now) }
                SyncReport("ok", batchesSent = result.sent, pending = outbox.size())
            }
            is WellnessOutbox.DrainResult.StopReconnect -> {
                state.update { it.copy(needsReconnect = true) }
                SyncReport("reconnect", batchesSent = result.sent, pending = outbox.size())
            }
            is WellnessOutbox.DrainResult.StopAndClear -> {
                Log.w(TAG, "box ended this enrollment (${result.status} ${result.error ?: ""}); clearing")
                binding.clearAll()
                runCatching { onStopped(result.status, result.error) }
                SyncReport("stopped:${result.error ?: result.status}", batchesSent = result.sent)
            }
            is WellnessOutbox.DrainResult.BackOff -> {
                val delay = (result.retryAfterS?.times(1000L)) ?: WellnessRules.quotaBackoffMs(0)
                state.update { it.copy(backoffUntilMs = now + delay.coerceAtMost(MAX_BACKOFF_MS)) }
                SyncReport("backoff", batchesSent = result.sent, pending = outbox.size())
            }
            is WellnessOutbox.DrainResult.RetryLater ->
                SyncReport("retry_later", batchesSent = result.sent, pending = outbox.size())
        }
    }

    /** Record a quota back-off; returns the delay. */
    private fun backOff(): Long {
        val now = clock()
        var delay = 0L
        state.update {
            delay = WellnessRules.quotaBackoffMs(it.quotaAttempt)
            it.copy(backoffUntilMs = now + delay, quotaAttempt = it.quotaAttempt + 1)
        }
        Log.w(TAG, "health connect quota: backing off ${delay / 1000}s")
        return delay
    }

    companion object {
        private const val TAG = "WellnessSync"
        private val RUN_LOCK = Mutex()
        const val PAGE_MS = 7 * BucketBuilder.DAY_MS
        const val THIRTY_DAYS_MS = 30 * BucketBuilder.DAY_MS
        const val MAX_HISTORY_MS = 10L * 366 * BucketBuilder.DAY_MS
        const val RECONCILE_EVERY_MS = 30 * BucketBuilder.DAY_MS
        const val MAX_BACKOFF_MS = 2 * 60 * 60_000L
        const val DAYS_PER_BATCH = 7
        const val ITEMS_PER_BATCH = 2000
        const val MAX_CHANGE_PAGES = 200
        const val MAX_READ_PAGES = 200
        const val MAX_LATENCIES = 30

        fun randomBatchId(): String = java.util.UUID.randomUUID().toString().replace("-", "")
    }
}
