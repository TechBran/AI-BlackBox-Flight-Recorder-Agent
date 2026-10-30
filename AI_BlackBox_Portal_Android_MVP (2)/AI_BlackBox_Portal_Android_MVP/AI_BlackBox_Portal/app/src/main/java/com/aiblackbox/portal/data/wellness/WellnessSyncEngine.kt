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
 * One run at a time per process ([RUN_LOCK]): the periodic worker, "Sync now" and a box
 * nudge can all land together.
 *
 * Every run is FENCED to the binding it started with ([WellnessBinding.runIfCurrent]): if a
 * re-enroll, unbind or erase lands while it reads Health Connect or waits on the box, it
 * stops at its next write, never recreating a deleted file, never clearing a newer binding.
 *
 * The box owns consent and state. Each run first reads them back (a signed status, at most
 * once a minute, [fetchStatus]) so a category turned off or a Pause made on ANY surface stops
 * the reads here, and every push is filtered by the consent of that moment
 * ([IngestPayloadBuilder.withoutCategories]), so a batch read earlier never delivers a
 * category that is now off.
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
    private val onReconnectNeeded: () -> Unit = {},
    /** Signed `GET /wellness/status?operator=` for the live binding; null = never ask (some tests). */
    private val fetchStatus: (suspend (operator: String, signer: OriginSigner) -> WellnessOutbox.SendResult)? = null,
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
        /** Wait [delayMs]; [requeue] false = do not schedule a hidden retry (no background grant). */
        data class QuotaBackoff(val delayMs: Long, val requeue: Boolean = true) : BackfillStep()
    }

    /** The binding a run started with changed under it (re-enroll, unbind, erase). */
    private class StaleRunException : Exception("wellness binding changed during the run")

    /** Do [block] only while the run's binding is still current; otherwise end the run. */
    private fun write(gen: Long, block: () -> Unit) {
        if (!binding.runIfCurrent(gen, block)) throw StaleRunException()
    }

    /** The gate both kinds of run share (WellnessRules.maySync), from what the phone holds now. */
    private fun gate(st: WellnessSyncState, sdk: HcSdkStatus, granted: Set<String>, readable: List<HcType>,
                     inForeground: Boolean, now: Long): WellnessRules.SyncDecision = WellnessRules.maySync(
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
            held = st.localHold != null,
        ),
    )

    private suspend fun grantedNow(sdk: HcSdkStatus): Set<String> =
        if (sdk == HcSdkStatus.AVAILABLE) runCatching { gateway.grantedPermissions() }.getOrDefault(emptySet()) else emptySet()

    // ── incremental sync ───────────────────────────────────────────────────────────────────

    /**
     * One incremental run. [inForeground] must be decided when the run STARTS (the app has a
     * visible activity right now), never when the work was queued.
     */
    suspend fun sync(inForeground: Boolean): SyncReport = RUN_LOCK.withLock {
        val (record, gen) = binding.snapshot() ?: return@withLock SyncReport("not_enrolled")
        try {
            dayIndex.reload()
            refreshFromBox(record, gen)?.let { return@withLock SyncReport(it, pending = outbox.size()) }
            val now = clock()
            val st = state.load()
            val sdk = gateway.sdkStatus()
            val granted = grantedNow(sdk)
            val readable = WellnessRules.readableTypes(st.consent(), granted)
            val decision = gate(st, sdk, granted, readable, inForeground, now)
            if (decision is WellnessRules.SyncDecision.Skip) {
                Log.d(TAG, "sync skipped (${decision.reason})")
                // Reading is not allowed now, but what was read earlier may still be delivered.
                if (WellnessRules.mayDrainWhenSkipped(decision.reason) && outbox.size() > 0) {
                    val drained = drain(record, gen)
                    return@withLock if (drained.outcome == "ok") drained.copy(outcome = "skipped:${decision.reason}") else drained
                }
                return@withLock SyncReport("skipped:${decision.reason}", pending = outbox.size())
            }
            var queued = 0
            var rows = 0
            if ((decision as WellnessRules.SyncDecision.Run).collect) {
                try {
                    val (b, r) = collectIncremental(record, gen, readable, st, now)
                    queued = b
                    rows = r
                    write(gen) { state.update { it.copy(quotaAttempt = 0) } }
                } catch (e: HcQuotaException) {
                    backOff(gen)
                } catch (e: OutboxFullException) {
                    Log.w(TAG, "outbox full; tokens held until it drains")
                } catch (e: HcPermissionException) {
                    Log.w(TAG, "a health permission was withdrawn mid-run")
                }
            }
            val drained = drain(record, gen)
            Log.i(TAG, "sync: queued=$queued rows=$rows sent=${drained.batchesSent} pending=${outbox.size()}")
            drained.copy(batchesQueued = queued, rowsQueued = rows)
        } catch (e: StaleRunException) {
            Log.i(TAG, "sync ended: the health binding changed during the run")
            SyncReport("stale")
        }
    }

    private suspend fun collectIncremental(
        record: WellnessBindingRecord,
        gen: Long,
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

        var newlySeen = false
        for (type in readable) {
            val entry = tokens.get(type)
            if (entry == null) {
                // First sight of this type (consent or a grant just widened): take a token now
                // and let the backfill cover what came before it.
                val fresh = gateway.changesToken(type)
                write(gen) { tokens.seed(type, fresh, now) }
                newlySeen = true
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
        // The commit (outbox, index, tokens, bookkeeping) happens whole under the fence, or
        // not at all: an unbind that landed during the reads keeps nothing.
        write(gen) {
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
                    // A type seen for the first time after the history import finished (a category
                    // turned on, a grant added) has no history yet: run the import again. What is
                    // already on the box is replaced, never doubled (replace_day, hc_id upserts).
                    backfill = if (newlySeen && it.backfill.done) BackfillCursor() else it.backfill,
                )
            }
        }
        return batches.size to batches.sumOf { it.rowCount }
    }

    // ── backfill ───────────────────────────────────────────────────────────────────────────

    /**
     * One 7-day page of history, newest first. Tokens are taken for every readable type
     * BEFORE the first page, so edits made during a long backfill are caught by the next
     * incremental run. The cursor moves only after the page's batches are in the outbox.
     *
     * Gated exactly like [sync] (WellnessRules.maySync): never after the box refused the key,
     * never while paused or held, and without the background grant only while the app is
     * visible ([inForeground], decided per page). A page whose push the box refuses ends the
     * import (Skipped), so the worker reads no further.
     */
    suspend fun backfillPage(inForeground: Boolean): BackfillStep = RUN_LOCK.withLock {
        val (record, gen) = binding.snapshot() ?: return@withLock BackfillStep.Skipped("not_enrolled")
        try {
            backfillPageLocked(record, gen, inForeground)
        } catch (e: StaleRunException) {
            Log.i(TAG, "history import ended: the health binding changed during the page")
            BackfillStep.Skipped("stale")
        }
    }

    private suspend fun backfillPageLocked(record: WellnessBindingRecord, gen: Long, inForeground: Boolean): BackfillStep {
        dayIndex.reload()
        refreshFromBox(record, gen)?.let { return BackfillStep.Skipped(it) }
        val now = clock()
        val z = zone()
        val st = state.load()
        if (st.backfill.done) return BackfillStep.Done
        val sdk = gateway.sdkStatus()
        val granted = grantedNow(sdk)
        val readable = WellnessRules.readableTypes(st.consent(), granted)
        val backgroundGranted = WellnessCatalog.PERMISSION_BACKGROUND in granted
        when (val decision = gate(st, sdk, granted, readable, inForeground, now)) {
            is WellnessRules.SyncDecision.Skip -> return if (decision.reason == "backoff")
                BackfillStep.QuotaBackoff(st.backoffUntilMs - now, requeue = backgroundGranted)
            else BackfillStep.Skipped(decision.reason)
            is WellnessRules.SyncDecision.Run -> if (!decision.collect) return BackfillStep.Skipped("outbox_full")
        }
        return try {
            for (type in readable) if (tokens.get(type) == null) {
                val fresh = gateway.changesToken(type)
                write(gen) { tokens.seed(type, fresh, now) }
            }

            val today = BucketBuilder.localDay(now, z)
            val cursor = st.backfill.cursorMs ?: BucketBuilder.dayRange(today, z).second
            val floor = st.backfill.floorMs ?: computeFloor(readable, granted, now, z)
            if (cursor <= floor) {
                write(gen) { state.update { it.copy(backfill = it.backfill.copy(done = true)) } }
                return BackfillStep.Done
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
            val done = pageStart <= floor
            var next = st
            write(gen) {
                for (b in batches) outbox.enqueue(IngestPayloadBuilder.toJson(b))
                dayIndex.save()
                next = state.update {
                    it.copy(
                        quotaAttempt = 0,
                        earliestAvailableMs = floor,
                        backfill = BackfillCursor(
                            cursorMs = pageStart, floorMs = floor, pagesDone = it.backfill.pagesDone + 1,
                            pagesTotal = total, done = done,
                        ),
                    )
                }
            }
            Log.i(TAG, "backfill page ${next.backfill.pagesDone}/$total: batches=${batches.size}")
            val drained = drain(record, gen)
            // The box said stop (key refused, enrollment over, paused): read no further pages.
            if (drained.outcome in STOPPING_OUTCOMES || drained.outcome.startsWith("stopped:")) {
                return BackfillStep.Skipped(drained.outcome)
            }
            if (done) BackfillStep.Done
            else BackfillStep.Progress(next.backfill.pagesDone, total, batches.sumOf { it.rowCount })
        } catch (e: HcQuotaException) {
            BackfillStep.QuotaBackoff(backOff(gen), requeue = backgroundGranted)
        } catch (e: OutboxFullException) {
            // The cursor did not move; the page is read again once the outbox drains.
            BackfillStep.Skipped("outbox_full")
        } catch (e: HcPermissionException) {
            BackfillStep.Skipped("permission_revoked")
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
                bucketsByDay.getOrPut(day) { mutableListOf() } += BucketBuilder.heartRateBuckets(day, hrOrigin, five, one, z)
            }
        }
        if (activityTypes.isNotEmpty()) {
            val keys = activityTypes.map { BucketBuilder.ACTIVITY_KEYS.getValue(it) }.toSet()
            for (day in work.activityDays) {
                val (ds, de) = BucketBuilder.dayRange(day, z)
                val hourly = gateway.aggregateByDuration(keys, ds, de, BucketBuilder.HOUR_MS, null)
                bucketsByDay.getOrPut(day) { mutableListOf() } += BucketBuilder.activityBuckets(day, hourly, activityTypes, z)
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

    // ── the box's view: consent and state ──────────────────────────────────────────────────

    /**
     * Read the box's consent and state back (signed status; at most one attempt per
     * [STATUS_EVERY_MS], so an unreachable box does not cost every history page a timeout)
     * before the run reads anything. A narrowing made on another surface reaches the
     * collector here; a refused key or an ended enrollment stops the run the same way a
     * refused push does. No answer leaves the phone's last view in place. Returns the outcome
     * that ends the run, or null to carry on.
     */
    private suspend fun refreshFromBox(record: WellnessBindingRecord, gen: Long): String? {
        val fetch = fetchStatus ?: return null
        val now = clock()
        val st = state.load()
        // Stopped (a refused key) or held here: the run ends at the gate anyway, so ask nothing.
        if (st.needsReconnect || st.localHold != null) return null
        if (st.lastStatusMs in (now - STATUS_EVERY_MS + 1)..now) return null
        val signer = OriginSigner(record.enrollmentId, record.k)
        val reply = signedCall { fetch(record.operator, signer) }
        val error = WellnessOutbox.parseError(reply.body)
        return when (WellnessRules.statusReaction(reply.status, error)) {
            WellnessRules.StatusReaction.APPLY -> {
                val box = WellnessStatusParser.parse(reply.body)
                val categories = box?.takeIf { it.credentialed }?.categories
                write(gen) {
                    state.update { st ->
                        if (box == null || categories == null) st.copy(lastStatusMs = now)
                        else st.copy(
                            categories = categories.mapKeys { it.key.wire }.mapValues { it.value.wire },
                            state = box.state.wire,
                            lastStatusMs = now,
                        )
                    }
                }
                null
            }
            WellnessRules.StatusReaction.PAUSED -> {
                write(gen) { state.update { it.copy(state = WellnessRules.EnrollmentState.PAUSED.wire, lastStatusMs = now) } }
                null
            }
            WellnessRules.StatusReaction.STOP_RECONNECT -> stopForReconnect(gen)
            WellnessRules.StatusReaction.STOP_AND_CLEAR -> stopAndClear(gen, reply.status, error)
            WellnessRules.StatusReaction.KEEP_LOCAL -> {
                write(gen) { state.update { it.copy(lastStatusMs = now) } }
                null
            }
        }
    }

    /** A 401: stop until the person reconnects; tell them once, not on every retry. */
    private fun stopForReconnect(gen: Long): String {
        var first = false
        write(gen) {
            state.update {
                first = !it.needsReconnect
                it.copy(needsReconnect = true)
            }
        }
        if (first) runCatching { onReconnectNeeded() }
        return "reconnect"
    }

    /**
     * A 409/410: the box ended THIS enrollment. Clear only if it is still the live one; a
     * re-enroll that replaced it while the refused request was in flight stays.
     */
    private fun stopAndClear(gen: Long, status: Int, error: String?): String {
        if (!binding.clearIfCurrent(gen)) throw StaleRunException()
        Log.w(TAG, "box ended this enrollment ($status ${error ?: ""}); cleared")
        runCatching { onStopped(status, error) }
        return "stopped:${error ?: status}"
    }

    // ── push ───────────────────────────────────────────────────────────────────────────────

    /**
     * A signed call with one retry on 401. The box refuses a signature more than 120 s off its
     * own clock with the same "bad_signature" as a key it no longer knows; the refusal's Date
     * header has just corrected a drifted phone clock (IngestClient), so the retry tells the
     * two apart and a drifted clock never asks the person to reconnect.
     */
    private suspend fun signedCall(call: suspend () -> WellnessOutbox.SendResult): WellnessOutbox.SendResult {
        val first = call()
        return if (first.status == 401) call() else first
    }

    /**
     * Send one stored batch with the consent of THIS moment: the categories that are off
     * now are taken out first (a batch with nothing left is dropped without a request). A
     * batch the phone cannot read is set aside like a 422, never sent unchecked.
     */
    private suspend fun sendWithConsent(bytes: ByteArray, signer: OriginSigner): WellnessOutbox.SendResult {
        val off = state.load().consent().filterValues { !it.isOn }.keys
        val filtered = try {
            IngestPayloadBuilder.withoutCategories(WellnessOutbox.gunzip(bytes), off)
        } catch (e: Exception) {
            return WellnessOutbox.SendResult(422, local = true)
        }
        return when {
            filtered == null -> signedCall { send(bytes, signer) }
            filtered.isEmpty -> WellnessOutbox.SendResult(200, local = true)
            else -> WellnessOutbox.gzip(filtered.json).let { out -> signedCall { send(out, signer) } }
        }
    }

    /** Push the outbox; react to the box's answers. */
    private suspend fun drain(record: WellnessBindingRecord, gen: Long): SyncReport {
        val signer = OriginSigner(record.enrollmentId, record.k)
        val result = outbox.drain(
            send = { bytes -> sendWithConsent(bytes, signer) },
            newBatchId = newBatchId,
            fence = { block -> binding.runIfCurrent(gen, block) },
        )
        if (result.notices.isNotEmpty()) runCatching { onNotices(result.notices) }
        val now = clock()
        return when (result) {
            is WellnessOutbox.DrainResult.Drained -> {
                if (result.sent > 0) write(gen) { state.update { it.copy(lastPushMs = now) } }
                SyncReport("ok", batchesSent = result.sent, pending = outbox.size())
            }
            is WellnessOutbox.DrainResult.StopReconnect ->
                SyncReport(stopForReconnect(gen), batchesSent = result.sent, pending = outbox.size())
            is WellnessOutbox.DrainResult.StopAndClear ->
                SyncReport(stopAndClear(gen, result.status, result.error), batchesSent = result.sent)
            is WellnessOutbox.DrainResult.BackOff -> {
                if (result.error == "paused") {
                    // Paused on another surface: stop reading here until the box says otherwise.
                    write(gen) { state.update { it.copy(state = WellnessRules.EnrollmentState.PAUSED.wire) } }
                    return SyncReport("paused", batchesSent = result.sent, pending = outbox.size())
                }
                val delay = (result.retryAfterS?.times(1000L)) ?: WellnessRules.quotaBackoffMs(0)
                write(gen) { state.update { it.copy(backoffUntilMs = now + delay.coerceAtMost(MAX_BACKOFF_MS)) } }
                SyncReport("backoff", batchesSent = result.sent, pending = outbox.size())
            }
            is WellnessOutbox.DrainResult.RetryLater ->
                SyncReport("retry_later", batchesSent = result.sent, pending = outbox.size())
            is WellnessOutbox.DrainResult.Stale -> throw StaleRunException()
        }
    }

    /** Record a quota back-off; returns the delay. */
    private fun backOff(gen: Long): Long {
        val now = clock()
        var delay = 0L
        write(gen) {
            state.update {
                delay = WellnessRules.quotaBackoffMs(it.quotaAttempt)
                it.copy(backoffUntilMs = now + delay, quotaAttempt = it.quotaAttempt + 1)
            }
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
        /** The collector reads the box's consent and state back at most this often. */
        const val STATUS_EVERY_MS = 60_000L
        /** Push outcomes after which the history import reads no further page. */
        private val STOPPING_OUTCOMES = setOf("reconnect", "paused", "stale")

        fun randomBatchId(): String = java.util.UUID.randomUUID().toString().replace("-", "")
    }
}
