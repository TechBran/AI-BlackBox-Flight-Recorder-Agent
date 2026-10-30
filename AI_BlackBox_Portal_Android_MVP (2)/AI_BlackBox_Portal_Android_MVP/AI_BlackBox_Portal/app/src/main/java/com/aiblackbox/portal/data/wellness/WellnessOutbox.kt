package com.aiblackbox.portal.data.wellness

import android.util.Log
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.Collections
import java.util.zip.GZIPInputStream
import java.util.zip.GZIPOutputStream

/**
 * The durable outbox between Health Connect and the box (spec §3.2).
 *
 * `noBackupFilesDir/wellness/outbox/<seq>.json.gz`, each written atomically (temp + fsync +
 * rename) BEFORE any Changes token or backfill cursor moves ([enqueue] mints the
 * [OutboxReceipt] that [ChangesTokenStore.advance] demands). Pushed OLDEST FIRST by [drain],
 * which applies the response table in [WellnessRules.outboxReaction].
 *
 * Capped at [maxBytes] (64 MB) or [maxBatches] (10,000). At the cap [enqueue] refuses and
 * the sync engine stops advancing tokens; Health Connect still holds the data, so nothing is
 * lost while it does. A 422 batch is moved to `quarantine/` and counted in the next report.
 *
 * File names sort as integer paths: `12.json.gz`, and a 413 split of it becomes
 * `12-1.json.gz` and `12-2.json.gz`, which still sort before `13.json.gz`.
 */
class WellnessOutbox(
    private val dir: File,
    private val quarantineDir: File = File(dir, "quarantine"),
    private val maxBytes: Long = MAX_BYTES,
    private val maxBatches: Int = MAX_BATCHES,
) {
    private val lock = Any()
    private val delivered: MutableSet<String> = Collections.synchronizedSet(LinkedHashSet())

    data class Entry(val name: String, val file: File, val key: List<Long>)

    /** Put [json] in the outbox. Throws [OutboxFullException] at the cap. */
    fun enqueue(json: String): OutboxReceipt = synchronized(lock) {
        if (isFullLocked()) throw OutboxFullException()
        val next = (entriesLocked().maxOfOrNull { it.key.first() } ?: 0L) + 1L
        val name = "$next$SUFFIX"
        val file = File(dir, name)
        WellnessAtomic.writeBytes(file, gzip(json))
        OutboxReceipt(name, file, this)
    }

    /** Pending batches, oldest first. */
    fun pending(): List<Entry> = synchronized(lock) { entriesLocked() }

    fun size(): Int = pending().size

    fun totalBytes(): Long = synchronized(lock) { entriesLocked().sumOf { it.file.length() } }

    fun isFull(): Boolean = synchronized(lock) { isFullLocked() }

    fun quarantinedCount(): Int = quarantineDir.listFiles()?.count { it.name.endsWith(SUFFIX) } ?: 0

    fun read(entry: Entry): ByteArray = entry.file.readBytes()

    internal fun wasDelivered(name: String): Boolean = name in delivered

    fun clear() = synchronized(lock) {
        dir.deleteRecursively()
        delivered.clear()
        Unit
    }

    /** What a drain ended with. */
    sealed class DrainResult {
        abstract val sent: Int
        abstract val notices: List<JsonObject>

        data class Drained(override val sent: Int, override val notices: List<JsonObject>) : DrainResult()
        data class StopReconnect(override val sent: Int, override val notices: List<JsonObject>) : DrainResult()
        data class StopAndClear(
            override val sent: Int,
            override val notices: List<JsonObject>,
            val status: Int,
            val error: String?,
        ) : DrainResult()
        data class BackOff(
            override val sent: Int,
            override val notices: List<JsonObject>,
            val retryAfterS: Long?,
            /** The box's error code (503 "paused" means the person paused Health elsewhere). */
            val error: String? = null,
        ) : DrainResult()
        data class RetryLater(override val sent: Int, override val notices: List<JsonObject>) : DrainResult()
        /** The binding changed under the drain (re-enroll, unbind): it stopped touching anything. */
        data class Stale(override val sent: Int, override val notices: List<JsonObject>) : DrainResult()
    }

    /**
     * One push attempt's answer: HTTP status (-1 = no answer) and body. [local] marks an
     * answer the phone gave itself without a request (a batch with nothing left to send after
     * the consent filter: 200, deleted and not counted; one it could not read: 422).
     */
    data class SendResult(val status: Int, val body: String = "", val retryAfterS: Long? = null, val local: Boolean = false)

    /**
     * Push every pending batch, oldest first, through [send] (which signs and POSTs the gzip
     * bytes). Stops at the first answer that says stop or wait. [newBatchId] names 413 halves.
     *
     * [fence] runs each change to the outbox (and a no-op check before each send); it returns
     * false when the binding the drain started with is gone, and the drain then stops without
     * touching another file ([DrainResult.Stale]): an unbind keeps nothing, and a re-enroll's
     * fresh outbox is never written by a drain of the old one.
     */
    suspend fun drain(
        send: suspend (ByteArray) -> SendResult,
        newBatchId: () -> String,
        maxSends: Int = 500,
        fence: (() -> Unit) -> Boolean = { it(); true },
    ): DrainResult {
        var sent = 0
        val notices = mutableListOf<JsonObject>()
        var budget = maxSends
        while (budget-- > 0) {
            if (!fence {}) return DrainResult.Stale(sent, notices)
            val entry = pending().firstOrNull() ?: return DrainResult.Drained(sent, notices)
            val bytes = try {
                read(entry)
            } catch (e: Exception) {
                if (!fence { quarantine(entry) }) return DrainResult.Stale(sent, notices)
                continue
            }
            val result = send(bytes)
            when (WellnessRules.outboxReaction(result.status)) {
                WellnessRules.OutboxReaction.DELETE -> {
                    if (!fence { delete(entry) }) return DrainResult.Stale(sent, notices)
                    if (!result.local) {
                        sent++
                        notices += parseNotices(result.body)
                    }
                }
                WellnessRules.OutboxReaction.STOP_RECONNECT -> return DrainResult.StopReconnect(sent, notices)
                WellnessRules.OutboxReaction.STOP_AND_CLEAR ->
                    return DrainResult.StopAndClear(sent, notices, result.status, parseError(result.body))
                WellnessRules.OutboxReaction.SPLIT -> {
                    val halves = runCatching { IngestPayloadBuilder.split(gunzip(bytes), newBatchId) }.getOrNull()
                    val ok = fence { if (halves == null) quarantine(entry) else replaceWithHalves(entry, halves) }
                    if (!ok) return DrainResult.Stale(sent, notices)
                }
                WellnessRules.OutboxReaction.QUARANTINE ->
                    if (!fence { quarantine(entry) }) return DrainResult.Stale(sent, notices)
                WellnessRules.OutboxReaction.BACK_OFF ->
                    return DrainResult.BackOff(sent, notices, result.retryAfterS, parseError(result.body))
                WellnessRules.OutboxReaction.RETRY_LATER -> return DrainResult.RetryLater(sent, notices)
            }
        }
        return DrainResult.RetryLater(sent, notices)
    }

    private fun delete(entry: Entry) {
        synchronized(lock) {
            delivered += entry.name
            entry.file.delete()
        }
    }

    private fun quarantine(entry: Entry) {
        synchronized(lock) {
            quarantineDir.mkdirs()
            val dest = File(quarantineDir, entry.name)
            if (!entry.file.renameTo(dest)) entry.file.delete()
            Log.w(TAG, "batch quarantined (422 or unreadable); ${quarantinedCount()} set aside")
        }
    }

    /** Write both halves (as `<name>-1`, `<name>-2`) BEFORE removing the original. */
    private fun replaceWithHalves(entry: Entry, halves: Pair<String, String>) {
        synchronized(lock) {
            val base = entry.name.removeSuffix(SUFFIX)
            WellnessAtomic.writeBytes(File(dir, "$base-1$SUFFIX"), gzip(halves.first))
            WellnessAtomic.writeBytes(File(dir, "$base-2$SUFFIX"), gzip(halves.second))
            entry.file.delete()
        }
    }

    private fun isFullLocked(): Boolean {
        val entries = entriesLocked()
        return entries.size >= maxBatches || entries.sumOf { it.file.length() } >= maxBytes
    }

    private fun entriesLocked(): List<Entry> =
        (dir.listFiles() ?: emptyArray())
            .filter { it.isFile && it.name.endsWith(SUFFIX) }
            .mapNotNull { f -> sortKey(f.name)?.let { Entry(f.name, f, it) } }
            .sortedWith { a, b -> compareKeys(a.key, b.key) }

    companion object {
        private const val TAG = "WellnessOutbox"
        const val SUFFIX = ".json.gz"
        const val MAX_BYTES = 64L * 1024 * 1024
        const val MAX_BATCHES = 10_000
        private val NAME_RE = Regex("\\d+(-\\d+)*")
        private val JSON = Json { ignoreUnknownKeys = true }

        fun sortKey(name: String): List<Long>? {
            val stem = name.removeSuffix(SUFFIX)
            if (!NAME_RE.matches(stem)) return null
            return stem.split('-').map { it.toLongOrNull() ?: return null }
        }

        fun compareKeys(a: List<Long>, b: List<Long>): Int {
            for (i in 0 until minOf(a.size, b.size)) {
                val c = a[i].compareTo(b[i])
                if (c != 0) return c
            }
            return a.size.compareTo(b.size)
        }

        fun gzip(text: String): ByteArray {
            val out = ByteArrayOutputStream()
            GZIPOutputStream(out).use { it.write(text.toByteArray(Charsets.UTF_8)) }
            return out.toByteArray()
        }

        fun gunzip(bytes: ByteArray): String =
            GZIPInputStream(ByteArrayInputStream(bytes)).use { it.readBytes().toString(Charsets.UTF_8) }

        /** `pending_notices` from a 200 ingest reply (each {id, kind, title, body, created_at}). */
        fun parseNotices(body: String): List<JsonObject> = runCatching {
            val arr = JSON.parseToJsonElement(body).jsonObject["pending_notices"] as? JsonArray ?: return emptyList()
            arr.mapNotNull { it as? JsonObject }
        }.getOrDefault(emptyList())

        fun parseError(body: String): String? = runCatching {
            JSON.parseToJsonElement(body).jsonObject["error"]?.jsonPrimitive?.contentOrNull
        }.getOrNull()

        fun parseLong(body: String, key: String): Long? = runCatching {
            JSON.parseToJsonElement(body).jsonObject[key]?.jsonPrimitive?.longOrNull
        }.getOrNull()
    }
}

/** Proof that a batch is durable in the outbox (or already delivered from it). */
class OutboxReceipt internal constructor(
    val name: String,
    private val file: File,
    private val outbox: WellnessOutbox,
) {
    fun isDurable(): Boolean = file.exists() || outbox.wasDelivered(name)
}

class OutboxFullException : Exception("wellness outbox is full")
