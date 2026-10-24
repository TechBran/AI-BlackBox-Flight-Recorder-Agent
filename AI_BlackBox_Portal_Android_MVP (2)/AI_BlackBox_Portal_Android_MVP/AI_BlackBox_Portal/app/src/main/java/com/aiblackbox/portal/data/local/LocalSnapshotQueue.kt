package com.aiblackbox.portal.data.local

import android.content.Context
import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.repository.ChatRepository
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import java.io.File
import java.io.IOException

/**
 * Sends a single completed on-device turn to the BlackBox (POST /chat/save). The
 * seam that lets [LocalSnapshotQueue] be unit-tested without a network: production
 * wires it to [ChatRepository.saveConversation] (see [LocalSnapshotQueue.fromContext]),
 * tests substitute a fake.
 *
 * A non-success surfaces as a throw. A plain [IOException] is the OFFLINE boundary
 * (unreachable hub): the queue KEEPS the item and tries again later. A non-2xx arrives
 * as [ApiHttpException] (also an IOException) carrying the status, which
 * [LocalSnapshotQueue.failureKind] reads to tell "later" from "never".
 */
interface SnapshotSender {
    suspend fun send(request: SaveRequest)
}

/**
 * Persists the pending-snapshot queue across process death. The production
 * implementation ([JsonFileQueueStore]) is plain JSON-on-disk; the seam keeps the
 * queue's core free of any storage policy and lets a fresh instance over the same
 * backing store recover the queue after a restart.
 *
 * [SaveRequest] is already `@Serializable`, so the on-disk form is just a JSON
 * array of the requests in FIFO order — no separate record type needed.
 */
interface QueueStore {
    /** The persisted queue (FIFO order), or empty if nothing was ever saved. */
    fun load(): List<SaveRequest>

    /** Replace the persisted queue with [items] (the new full FIFO list). */
    fun save(items: List<SaveRequest>)

    /**
     * Park [item] OUTSIDE the live queue because the box refused it for good ([reason]
     * names the status). Kept, not dropped, so a refused turn can still be recovered by
     * hand; the default (test stores) only forgets it.
     */
    fun deadLetter(item: SaveRequest, reason: String) {}
}

/** A turn the box refused for good, parked by [QueueStore.deadLetter]. */
@kotlinx.serialization.Serializable
data class DeadLetter(val reason: String, val request: SaveRequest)

/**
 * Offline-resilient, ordered write-back of on-device chat turns to the BlackBox
 * memory ledger.
 *
 * On-device generation works OFFLINE, but every turn must still land in the
 * searchable ledger (POST /chat/save, tagged provider=local upstream by Task 2.4).
 * This queue guarantees the ledger never has gaps OR reordering:
 *  - [enqueue] persists the turn IMMEDIATELY (so it survives process death), then
 *    attempts an inline [flush].
 *  - [flush] drains the persisted queue in strict FIFO order; each item is removed
 *    from disk only after its [SnapshotSender.send] succeeds. The FIRST
 *    [IOException] (offline / unreachable) STOPS the drain, leaving that item and
 *    everything after it queued, in order, for the next flush. Nothing is dropped
 *    or reordered.
 *  - A turn the box REFUSES for good (a 4xx other than 408/429, e.g. 413 or 422) is
 *    moved to the dead-letter file and the drain goes on (A09-data-local-model-04).
 *    Before, every non-2xx was read as "offline", so one rejected head blocked every
 *    later on-device turn forever. A 5xx, 408 or 429 is the box failing NOW, not a
 *    verdict on the turn: it stops the drain like an unreachable box and is retried,
 *    however often, so a server outage never costs a turn (the box answers 500 for
 *    any handler error, so a count-based cap would drop turns during a bad deploy).
 *
 * Concurrency: a [Mutex] serializes flushes so a fire-and-forget app-open flush
 * can't interleave with an enqueue-triggered flush and double-send / reorder.
 *
 * Capacity: the queue is UNBOUNDED by design — it is bounded in practice by the
 * offline window (turns generated while disconnected), and a connectivity-driven
 * cap is deliberately deferred (YAGNI) until a real unbounded-growth case appears.
 *
 * Android (Context / files dir) is confined to [fromContext] + [JsonFileQueueStore];
 * the core depends only on the [SnapshotSender] and [QueueStore] seams, so it is
 * plain-JUnit testable over a temp file.
 *
 * Wired into ChatViewModel.persistLocalSave (enqueue) + initialize (app-open flush).
 */
class LocalSnapshotQueue(
    private val sender: SnapshotSender,
    private val store: QueueStore,
) {
    // Serializes mutations of the persisted queue across concurrent flushes.
    private val mutex = Mutex()

    /**
     * Append [request] to the on-disk queue (persisted before we attempt any send,
     * so an offline turn survives even an immediate process kill), then try to
     * drain. Online: the item goes out and the queue ends empty. Offline: the item
     * stays queued for the next [flush].
     */
    suspend fun enqueue(request: SaveRequest) {
        mutex.withLock {
            store.save(store.load() + request)
        }
        flush()
    }

    /**
     * Drain the persisted queue in FIFO order. Removes each item from disk only on
     * a successful send; stops at the first [IOException] (offline), leaving the
     * remainder queued in order; a RETRYABLE answer (5xx, 408, 429) stops it the same
     * way. A PERMANENT refusal ([failureKind]) parks the head in the dead-letter
     * store and drains on. A non-IO error propagates WITHOUT dropping the
     * item — the queue is left exactly as far as it got, so no turn is silently
     * lost. Concurrent flushes are serialized by [the mutex] (kotlinx [Mutex] is
     * non-reentrant — this guarantees non-overlapping acquisition, not reentry).
     */
    suspend fun flush() {
        mutex.withLock {
            var pending = store.load()
            while (pending.isNotEmpty()) {
                val head = pending.first()
                try {
                    sender.send(head)
                } catch (e: IOException) {
                    if (failureKind(e) != FailureKind.PERMANENT) {
                        // Offline / unreachable, or the box failing for now: stop, keep
                        // head + the rest (already persisted) for the next flush. No
                        // drop, no reorder.
                        return
                    }
                    // The box will not take this turn: park it so the turns behind it
                    // still reach the ledger.
                    store.deadLetter(head, "HTTP ${(e as ApiHttpException).code}: ${e.message}")
                }
                // Sent (or parked): drop the head and persist the shorter queue
                // immediately so a crash mid-drain can't re-send it.
                pending = pending.drop(1)
                store.save(pending)
            }
        }
    }

    /** How a failed send is treated (see [failureKind]). */
    enum class FailureKind {
        /** No HTTP answer (dead socket, timeout, DNS): keep the head and retry later. */
        OFFLINE,
        /** The box answered but may accept later (5xx, 408, 429): keep the head and retry later. */
        RETRYABLE,
        /** The box refused this request for good (any other 4xx): dead-lettered at once. */
        PERMANENT,
    }

    companion object {
        private const val QUEUE_FILE_NAME = "local_snapshot_queue.json"

        /**
         * Classify a send failure. BlackBoxApi maps every non-2xx to [ApiHttpException]
         * (an [IOException]); a code of 0 means no status was seen, so it is transport.
         */
        fun failureKind(e: IOException): FailureKind {
            val code = (e as? ApiHttpException)?.code ?: 0
            return when {
                code == 0 -> FailureKind.OFFLINE
                code == 408 || code == 429 -> FailureKind.RETRYABLE
                code in 400..499 -> FailureKind.PERMANENT
                else -> FailureKind.RETRYABLE
            }
        }

        /**
         * Build a [LocalSnapshotQueue] wired to a file in the app's private files
         * dir and to [ChatRepository.saveConversation] as the sender. The only
         * entry point that touches Android; the resulting queue's core stays
         * framework-free.
         */
        fun fromContext(context: Context, repository: ChatRepository): LocalSnapshotQueue {
            val file = File(context.applicationContext.filesDir, QUEUE_FILE_NAME)
            val sender = object : SnapshotSender {
                override suspend fun send(request: SaveRequest) {
                    // saveConversation throws IOException when offline and
                    // ApiHttpException (with the status) for a non-2xx — exactly
                    // what flush() classifies.
                    repository.saveConversation(request)
                }
            }
            return LocalSnapshotQueue(sender, JsonFileQueueStore(file))
        }
    }
}

/**
 * Production [QueueStore]: a JSON array of [SaveRequest] on disk. The ONLY
 * file-touching code path. A missing/empty/corrupt file reads as an empty queue
 * (never throws on load — a corrupt queue must not wedge the app); [save] writes
 * the whole list ATOMICALLY (temp sibling + renameTo on the app-private filesDir),
 * so a crash mid-write can't truncate the file and silently drop the whole queue,
 * and deletes the file when the queue empties.
 */
class JsonFileQueueStore(private val file: File) : QueueStore {

    override fun load(): List<SaveRequest> {
        if (!file.exists()) return emptyList()
        return try {
            val text = file.readText()
            if (text.isBlank()) emptyList()
            else JSON.decodeFromString(REQUEST_LIST, text)
        } catch (e: Exception) {
            // Corrupt/unreadable persisted queue: treat as empty rather than
            // crashing every flush forever. (A genuinely offline send still keeps
            // its item via the in-memory path; this only guards a damaged file.)
            emptyList()
        }
    }

    override fun save(items: List<SaveRequest>) {
        if (items.isEmpty()) {
            // Empty queue → no file (so load() short-circuits and the "drained"
            // state is observably file-absent).
            if (file.exists()) file.delete()
            return
        }
        file.parentFile?.mkdirs()
        val json = JSON.encodeToString(REQUEST_LIST, items)
        // Atomic write: serialize to a temp sibling, then renameTo() the real file.
        // renameTo is atomic within the app-private filesDir (same filesystem), so a
        // crash/kill mid-write can never leave a half-written queue file — load()
        // either sees the complete OLD queue or the complete NEW one, never a
        // truncated one that would read as empty and silently lose the WHOLE queue.
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(json)
        if (!tmp.renameTo(file)) {
            // Some filesystems refuse rename onto an existing target: drop it, retry.
            file.delete()
            if (!tmp.renameTo(file)) {
                // Last resort (rename still failing): write directly so the data
                // lands; tmp is left on disk but harmless (load() only reads file).
                file.writeText(json)
            }
        }
    }

    /**
     * Append [item] to the dead-letter sibling (`<queue>.dead.json`), keeping only the
     * newest [MAX_DEAD_LETTERS] so a box that refuses everything cannot fill the disk.
     * Written atomically like the queue; a damaged dead-letter file starts over.
     */
    override fun deadLetter(item: SaveRequest, reason: String) {
        val kept = (deadLetters() + DeadLetter(reason, item)).takeLast(MAX_DEAD_LETTERS)
        writeAtomically(deadFile, JSON.encodeToString(DEAD_LIST, kept))
    }

    /** The parked turns, oldest first (empty when none or unreadable). */
    fun deadLetters(): List<DeadLetter> {
        if (!deadFile.exists()) return emptyList()
        return try {
            val text = deadFile.readText()
            if (text.isBlank()) emptyList() else JSON.decodeFromString(DEAD_LIST, text)
        } catch (e: Exception) {
            emptyList()
        }
    }

    private val deadFile: File get() = File(file.parentFile, file.nameWithoutExtension + ".dead.json")

    private fun writeAtomically(target: File, json: String) {
        target.parentFile?.mkdirs()
        val tmp = File(target.parentFile, target.name + ".tmp")
        tmp.writeText(json)
        if (!tmp.renameTo(target)) {
            target.delete()
            if (!tmp.renameTo(target)) target.writeText(json)
        }
    }

    companion object {
        /** Parked turns kept on disk; older ones are discarded first. */
        const val MAX_DEAD_LETTERS = 50

        private val JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true }
        private val REQUEST_LIST = ListSerializer(SaveRequest.serializer())
        private val DEAD_LIST = ListSerializer(DeadLetter.serializer())
    }
}
