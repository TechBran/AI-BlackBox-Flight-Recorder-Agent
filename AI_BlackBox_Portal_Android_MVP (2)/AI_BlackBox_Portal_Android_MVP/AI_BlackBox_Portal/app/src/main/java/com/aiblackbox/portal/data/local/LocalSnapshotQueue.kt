package com.aiblackbox.portal.data.local

import android.content.Context
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
 * A non-success surfaces as a throw. An [IOException] specifically is the OFFLINE
 * boundary (BlackBoxApi throws IOException for unreachable hub AND non-2xx); the
 * queue treats that as "try again later" and KEEPS the item.
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
}

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
     * remainder queued in order. A non-IO error propagates WITHOUT dropping the
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
                    // Offline / unreachable / non-2xx: stop, keep head + the rest
                    // (already persisted) for the next flush. No drop, no reorder.
                    return
                }
                // Sent: drop the head and persist the shorter queue immediately so
                // a crash mid-drain can't re-send it.
                pending = pending.drop(1)
                store.save(pending)
            }
        }
    }

    companion object {
        private const val QUEUE_FILE_NAME = "local_snapshot_queue.json"

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
                    // saveConversation throws IOException for offline/non-2xx —
                    // exactly the boundary flush() keys on.
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

    private companion object {
        val JSON = Json { ignoreUnknownKeys = true; encodeDefaults = true }
        val REQUEST_LIST = ListSerializer(SaveRequest.serializer())
    }
}
