package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * One Health Connect Changes token per record type (spec §3.2).
 *
 * The rule that keeps nothing from being lost: a token ADVANCES only with proof that what it
 * covers is already durable in the outbox ([CommitProof.Committed], which only
 * [WellnessOutbox.enqueue] can mint), or that there was nothing to commit. A crash between
 * the outbox write and the advance re-reads the same changes next run; the box drops the
 * duplicate by batch id and HC id.
 *
 * Tokens expire after 30 days unused. An expired token (by age here, or because
 * `getChanges` said so) is re-read from `synced_until` minus 48 hours ([reReadFromMs]).
 */
class ChangesTokenStore(
    private val file: File,
    private val clock: () -> Long = System::currentTimeMillis,
) {
    private val lock = Any()

    @Serializable
    data class Entry(
        val token: String,
        @SerialName("last_used_ms") val lastUsedMs: Long,
        @SerialName("synced_until_ms") val syncedUntilMs: Long,
    )

    fun get(type: HcType): Entry? = synchronized(lock) { load()[type.wire] }

    fun all(): Map<HcType, Entry> = synchronized(lock) {
        load().mapNotNull { (k, v) -> HcType.fromWire(k)?.let { it to v } }.toMap()
    }

    /**
     * Record a token taken BEFORE a backfill (or on first sight of a type). Nothing has been
     * read past it yet, so no commit is needed: history up to [syncedUntilMs] is the
     * backfill's job.
     */
    fun seed(type: HcType, token: String, syncedUntilMs: Long = clock()) = synchronized(lock) {
        val all = load().toMutableMap()
        all[type.wire] = Entry(token, clock(), syncedUntilMs)
        save(all)
    }

    /** Move [type] to [nextToken] covering data up to [syncedUntilMs]. See the class doc. */
    fun advance(type: HcType, nextToken: String, syncedUntilMs: Long, proof: CommitProof) = synchronized(lock) {
        if (proof is CommitProof.Committed) {
            check(proof.receipt.isDurable()) { "outbox batch ${proof.receipt.name} is not on disk" }
        }
        val all = load().toMutableMap()
        val previous = all[type.wire]?.syncedUntilMs ?: 0L
        all[type.wire] = Entry(nextToken, clock(), maxOf(previous, syncedUntilMs))
        save(all)
    }

    fun isExpired(entry: Entry, nowMs: Long = clock()): Boolean = nowMs - entry.lastUsedMs >= TOKEN_LIFE_MS

    /** Where an expired type is re-read from. */
    fun reReadFromMs(entry: Entry): Long = (entry.syncedUntilMs - REREAD_OVERLAP_MS).coerceAtLeast(0L)

    fun drop(type: HcType) = synchronized(lock) {
        val all = load().toMutableMap()
        if (all.remove(type.wire) != null) save(all)
    }

    fun clear() = synchronized(lock) { file.delete(); Unit }

    private fun load(): Map<String, Entry> {
        val raw = WellnessAtomic.readTextOrNull(file) ?: return emptyMap()
        return runCatching { JSON.decodeFromString<Map<String, Entry>>(raw) }.getOrDefault(emptyMap())
    }

    private fun save(all: Map<String, Entry>) {
        val sorted: Map<String, Entry> = all.entries.sortedBy { it.key }.associate { it.key to it.value }
        WellnessAtomic.writeText(file, JSON.encodeToString(sorted))
    }

    companion object {
        const val TOKEN_LIFE_MS = 30L * 24 * 60 * 60_000L
        const val REREAD_OVERLAP_MS = 48L * 60 * 60_000L
        private val JSON = Json { ignoreUnknownKeys = true }
    }
}

/** Why a token may move. */
sealed class CommitProof {
    data class Committed(val receipt: OutboxReceipt) : CommitProof()

    /** The changes were empty: there is nothing a crash could lose. */
    data object NothingToCommit : CommitProof()
}
